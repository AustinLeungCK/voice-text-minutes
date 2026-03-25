import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "ap-northeast-1"))

JOBS_TABLE = os.environ["JOBS_TABLE"]

# DeepSeek V3.2 context is 164K tokens. Most 1-hour meetings produce
# 50K-80K tokens, so chunking is rarely needed.
MAX_TRANSCRIPT_CHARS = 400_000  # ~100K tokens, safe for 164K context
CHUNK_SIZE_CHARS = 200_000

# max_tokens by summary_length — "detailed" needs more room for tables/subsections
MAX_TOKENS_BY_LENGTH = {
    "short": 4096,
    "brief": 4096,
    "concise": 4096,
    "medium": 4096,
    "detailed": 8192,
}


def lambda_handler(event, context):
    job_id = event["job_id"]
    bucket = event["s3_bucket"]
    requirements = _parse_requirements(event.get("requirements", {}))

    # Read merged transcript
    prefix = f"jobs/{job_id}"
    try:
        resp = s3_client.get_object(Bucket=bucket, Key=f"{prefix}/merged_transcript.txt")
        transcript = resp["Body"].read().decode("utf-8")
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            raise RuntimeError(
                f"Transcript not found for job {job_id} — merge step may have failed"
            ) from e
        raise

    system_prompt = _build_system_prompt(requirements)
    summary_length = requirements.get("summary_length", "medium")
    max_tokens = MAX_TOKENS_BY_LENGTH.get(summary_length, 4096)
    custom = requirements.get("custom_instructions", "").strip()

    if len(transcript) <= MAX_TRANSCRIPT_CHARS:
        minutes = _call_deepseek(system_prompt, transcript, max_tokens, custom)
    else:
        minutes = _chunked_summarize(system_prompt, transcript, requirements, max_tokens, custom)

    s3_client.put_object(
        Bucket=bucket,
        Key=f"{prefix}/meeting_minutes.md",
        Body=minutes.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )

    return {
        "status": "summarized",
        "s3_key": f"{prefix}/meeting_minutes.md",
        "length": len(minutes),
    }


def _parse_requirements(req):
    """Parse DynamoDB map format or plain dict."""
    if not req:
        return {
            "output_language": "繁體中文",
            "summary_length": "medium",
            "output_format": "minutes",
            "custom_instructions": "",
        }
    parsed = {}
    for key, val in req.items():
        if isinstance(val, dict) and "S" in val:
            parsed[key] = val["S"]
        else:
            parsed[key] = val
    return parsed


def _build_system_prompt(req):
    lang = req.get("output_language", "繁體中文")
    length = req.get("summary_length", "medium")
    fmt = req.get("output_format", "minutes")

    length_guide = {"short": "約300字", "medium": "約800字", "detailed": "約1500字"}
    length_text = length_guide.get(length, "約800字")

    format_guide = {
        "minutes": "會議紀錄（包含討論要點、決議事項）",
        "action_items": "行動項目清單（每項包含負責人、截止日期）",
        "both": "會議紀錄 + 行動項目清單",
    }
    format_text = format_guide.get(fmt, "會議紀錄")

    prompt = f"""你是一個專業的會議紀錄生成助手。請根據以下會議轉錄稿生成結構化的會議紀錄。

輸出語言：{lang}
輸出長度：{length_text}

請嚴格按照以下結構輸出（使用 Markdown 格式）：

# 會議記錄：[會議主題]

## 1. 會議摘要
用 2-3 句概括整個會議嘅核心內容同目的。

## 2. 與會者
列出所有與會者嘅真實姓名。如果轉錄稿有 PARTICIPANTS 列表，以此為準。
即使某人全程無發言，只要出現喺與會者名單，都要列出。
如果只有 SPEAKER_XX 標籤而無法確認真名，保留標籤但嘗試從對話上下文推斷身份。

## 3. 討論內容
按主題分 subsection（### 3.1, 3.2, ...）。每個要點必須標明係邊個提出。
保留所有關鍵數字、金額、日期、帳戶編號、伺服器型號等具體資訊。

## 4. 決議事項
用 numbered list 列出所有已確認嘅決定。

## 5. 行動事項
用 Markdown table，三欄：| 負責人 | 行動內容 | 截止日期/備註 |
每個行動必須有明確負責人。如果轉錄稿有提到截止日期就填，冇就寫「待定」。

## 6. 下次會議
如有提及下次會議時間或跟進事項，列出。冇就寫「待定」。

額外規則：
- 如有投影片內容（SLIDE CONTENTS），整合到相關討論段落中
- 唔好虛構任何資訊，所有內容必須來自轉錄稿
- 保持專業、客觀嘅語氣"""

    return prompt


def _call_deepseek(system_prompt, transcript, max_tokens=4096, custom_instructions=""):
    user_content = f"以下是會議轉錄稿，請生成會議紀錄：\n\n{transcript}"
    if custom_instructions:
        user_content += f"\n\n---\n用戶額外要求：{custom_instructions}"

    try:
        response = bedrock.invoke_model(
            modelId="deepseek.v3.2",
            body=json.dumps(
                {
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": 0.1,
                }
            ),
            contentType="application/json",
            accept="application/json",
        )
        result = json.loads(response["body"].read())
        return result["choices"][0]["message"]["content"]
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        logger.error("Bedrock ClientError [%s]: %s", error_code, e)
        raise RuntimeError(
            f"Bedrock API error ({error_code}): {e}"
        ) from e
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        logger.error("Bedrock response parsing error: %s", e)
        raise RuntimeError(
            f"Bedrock returned malformed response: {e}"
        ) from e
    except Exception as e:
        logger.error("Unexpected Bedrock error: %s", e)
        raise RuntimeError(
            f"Unexpected error calling Bedrock: {e}"
        ) from e


def _chunked_summarize(system_prompt, transcript, requirements, max_tokens=4096, custom_instructions=""):
    """Parent-child chunking for very long transcripts."""
    lines = transcript.split("\n")
    chunks = []
    current_chunk = []
    current_size = 0

    for line in lines:
        current_chunk.append(line)
        current_size += len(line) + 1
        if current_size >= CHUNK_SIZE_CHARS:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_size = 0

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # Step 1: Generate outline from first and last chunks (short output, 4096 is fine)
    outline_text = chunks[0][:50000] + "\n...\n" + chunks[-1][-50000:]
    outline = _call_deepseek(
        "根據以下會議轉錄稿的開頭和結尾，生成一個簡要大綱（5-10個要點）。",
        outline_text,
    )

    # Step 2: Summarize each chunk with outline context
    chunk_summaries = []
    for i, chunk in enumerate(chunks):
        summary = _call_deepseek(
            f"{system_prompt}\n\n參考大綱：\n{outline}\n\n"
            f"這是第 {i + 1}/{len(chunks)} 部分。請總結此部分的要點。",
            chunk,
            max_tokens=max_tokens,
            custom_instructions=custom_instructions,
        )
        chunk_summaries.append(summary)

    # Step 3: Merge all summaries into final minutes
    combined = "\n\n---\n\n".join(chunk_summaries)
    final = _call_deepseek(
        f"{system_prompt}\n\n"
        "以下是分段總結，請合併成一份完整的會議紀錄。去除重複內容，保持結構一致。",
        combined,
        max_tokens=max_tokens,
        custom_instructions=custom_instructions,
    )

    return final
