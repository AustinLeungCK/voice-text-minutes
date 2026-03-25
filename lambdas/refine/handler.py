import json
import os

import boto3

s3_client = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION", "ap-northeast-2"))

JOBS_TABLE = os.environ["JOBS_TABLE"]
DATA_BUCKET = os.environ["DATA_BUCKET"]

SYSTEM_PROMPT = (
    "你是會議紀錄調整助手。根據用戶指令修改現有會議紀錄。"
    "保持 Markdown 格式。唔好虛構資訊，所有內容必須來自原始轉錄稿。"
)


def lambda_handler(event, context):
    # --- Extract job_id from path parameters ---
    job_id = (event.get("pathParameters") or {}).get("job_id")
    if not job_id:
        return _response(400, {"error": "job_id is required"})

    # --- Extract caller email from Lambda authorizer context ---
    authorizer = (event.get("requestContext") or {}).get("authorizer", {})
    caller_email = authorizer.get("email")
    if not caller_email:
        return _response(401, {"error": "unauthorized"})

    # --- Verify ownership: job email must match caller email ---
    table = dynamodb.Table(JOBS_TABLE)
    result = table.get_item(Key={"job_id": job_id})
    item = result.get("Item")
    if not item or item.get("email") != caller_email:
        return _response(404, {"error": "Job not found"})

    # --- Validate job is in a completed state ---
    if item.get("status") not in ("completed", "refined"):
        return _response(409, {"error": "Job is not yet completed"})

    # --- Parse instruction from request body ---
    try:
        body = json.loads(event.get("body") or "{}")
    except (json.JSONDecodeError, TypeError):
        return _response(400, {"error": "Invalid JSON body"})

    instruction = body.get("instruction", "").strip()
    if not instruction:
        return _response(400, {"error": "instruction is required"})

    if len(instruction) > 2000:
        return _response(400, {"error": "instruction too long (max 2000 chars)"})

    # --- Read transcript and current minutes from S3 ---
    prefix = f"jobs/{job_id}"

    try:
        transcript_resp = s3_client.get_object(
            Bucket=DATA_BUCKET, Key=f"{prefix}/merged_transcript.txt"
        )
        transcript = transcript_resp["Body"].read().decode("utf-8")
    except s3_client.exceptions.NoSuchKey:
        return _response(404, {"error": "Transcript not found"})

    try:
        minutes_resp = s3_client.get_object(
            Bucket=DATA_BUCKET, Key=f"{prefix}/meeting_minutes.md"
        )
        current_minutes = minutes_resp["Body"].read().decode("utf-8")
    except s3_client.exceptions.NoSuchKey:
        return _response(404, {"error": "Meeting minutes not found"})

    # --- Call Bedrock Claude Sonnet 4.6 ---
    user_message = (
        f"原始轉錄稿：\n{transcript}\n\n"
        f"現有會議紀錄：\n{current_minutes}\n\n"
        f"用戶指令：{instruction}\n\n"
        f"請根據指令調整會議紀錄，輸出完整嘅新版本。"
    )

    try:
        response = bedrock.invoke_model(
            modelId="anthropic.claude-sonnet-4-6-20250514-v1:0",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
                "max_tokens": 8192,
                "temperature": 0.1,
            }),
            contentType="application/json",
            accept="application/json",
        )
        result_body = json.loads(response["body"].read())
        refined_minutes = result_body["content"][0]["text"]
    except Exception as e:
        print(f"Bedrock error for job {job_id}: {e}")
        return _response(502, {"error": "Failed to refine minutes"})

    # --- Save refined minutes to S3 (overwrite) ---
    s3_client.put_object(
        Bucket=DATA_BUCKET,
        Key=f"{prefix}/meeting_minutes.md",
        Body=refined_minutes.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )

    # --- Update DynamoDB status to refined ---
    table.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "refined"},
    )

    return _response(200, {"job_id": job_id, "minutes": refined_minutes})


def _response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }
