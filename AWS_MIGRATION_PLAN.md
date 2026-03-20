# AWS Migration Plan — voice-text-minutes

## Context

將現有嘅本地廣東話會議錄音工具遷移到 AWS。Frontend 畀同事 upload 會議錄影 + 填寫 output requirements（語言、字數、格式等），Submit 之後全自動跑，完成後用 SES 發 email 將結果送畀 user。

Multi-region 架構：
- **ap-east-1 (Hong Kong)**: S3 上傳（同事 upload 快）+ AWS Batch g4dn.xlarge Spot 做所有 processing
- **ap-southeast-1 (Singapore)**: Bedrock DeepSeek V3.2 summarization + SES email（cross-region API call）

所有 infrastructure（Lambda / Step Functions / DynamoDB / API Gateway / Batch）集中喺 HK，只有 Bedrock 同 SES 做 cross-region call。

---

## Architecture Overview

```
Frontend (CloudFront → S3 ap-east-1)
  → User 填寫 requirements (語言/字數/格式/email) + upload 錄影
  → API Gateway (ap-east-1)
    → Lambda: submit_job (presigned URL + job_id → DynamoDB)

S3 Upload Event (ap-east-1)
  → Lambda: start_pipeline
    → Step Functions (ap-east-1)
      ├─ Lambda: extract_audio (ffmpeg, MP4→WAV)
      │
      ├─ AWS Batch (g4dn.xlarge Spot, ap-east-1) — 單一 container 做晒：
      │   ├─ Whisper STT (GPU, ~7min)
      │   ├─ pyannote diarization (CPU, 同 Whisper 並行, ~15min)
      │   └─ GOT-OCR2.0 (GPU, Whisper 完接住跑, ~2min)
      │   → 輸出: whisper_result.json + diarize_result.json + ocr_result.json → S3
      │
      ├─ Lambda: merge_transcript
      ├─ Lambda: summarize
      │   → cross-region call Bedrock DeepSeek V3.2 (ap-southeast-1)
      │   → 按 DynamoDB requirements 決定語言、字數、格式
      │   → 輸出: meeting_minutes.md → S3
      └─ Lambda: notify
          → cross-region call SES (ap-southeast-1)
          → email output 畀 user + DynamoDB update → SUCCESS

  [Any failure] → Lambda: notify_failure → SES error email → FAILED
```

---

## Regions & Cross-Region Strategy

| 服務 | Region | 原因 |
|---|---|---|
| S3, Lambda, Step Functions, DynamoDB, API Gateway, Batch, ECR | **ap-east-1 (HK)** | 主 region，upload 快，g4dn Spot 可用 |
| Bedrock DeepSeek V3.2 | **ap-southeast-1 (Singapore)** | Bedrock + SES 集中同一個 region |
| SES | **ap-southeast-1 (Singapore)** | HK 無 SES |
| CloudFront | Global | CDN，全球加速 |

Cross-region call 方法：Lambda 用 `boto3.client('bedrock-runtime', region_name='ap-southeast-1')` 同 `boto3.client('ses', region_name='ap-southeast-1')`。額外延遲 ~30-50ms，對 async pipeline 無影響。

---

## Project Structure

```
voice-text-minutes/
├── transcribe.py                  # 保留原有本地版本
├── infra/                         # CDK (Python)
│   ├── app.py
│   ├── cdk.json
│   ├── requirements.txt
│   └── stacks/
│       ├── storage_stack.py       # S3 buckets (ap-east-1)
│       ├── api_stack.py           # API Gateway + DynamoDB + auth
│       ├── processing_stack.py    # Step Functions + Batch + Lambdas
│       └── frontend_stack.py      # CloudFront distribution
├── lambdas/
│   ├── submit_job/handler.py      # presigned URL + job_id + requirements → DynamoDB
│   ├── start_pipeline/handler.py  # S3 event → 啟動 Step Functions
│   ├── extract_audio/handler.py   # ffmpeg MP4→WAV
│   ├── merge_transcript/handler.py# 合併 whisper + diarization + OCR results
│   ├── summarize/handler.py       # cross-region Bedrock DeepSeek V3.2 call
│   ├── notify/handler.py          # cross-region SES email + DynamoDB 更新
│   └── get_status/handler.py      # 查詢 job 狀態（debug 用）
├── containers/
│   └── processor/
│       ├── Dockerfile             # nvidia/cuda + faster-whisper + pyannote + GOT-OCR2.0
│       └── entrypoint.py          # S3 download → Whisper∥Diarize → OCR → S3 upload
├── frontend/
│   ├── index.html                 # 單頁應用：requirements 表單 + upload
│   ├── style.css
│   └── app.js                     # Upload + submit，完成後等 email 通知
└── tests/
    ├── test_merge_transcript.py
    └── test_summarize.py
```

Note: 只有一個 container image (`processor`)，包含所有 GPU/CPU 工作。唔需要 Fargate。

---

## DynamoDB Schema

```
Table: meeting-jobs (ap-east-1)
  PK: job_id (UUID)

  # User input
  email: string                    # user email，用嚟 SES 發結果
  requirements: map
    output_language: string        # "繁體中文" | "简体中文" | "English"
    summary_length: string         # "short" (~300字) | "medium" (~800字) | "detailed" (~1500字)
    output_format: string          # "minutes" (會議紀錄) | "action_items" (行動項目) | "both"
    custom_instructions: string    # 自由填寫額外要求（optional）

  # Job tracking
  status: string                   # "uploaded" | "processing" | "completed" | "failed"
  created_at: string (ISO 8601)
  completed_at: string (ISO 8601)
  error_message: string            # 如果 failed
  output_s3_key: string            # 完成後嘅 output path
```

---

## S3 Key Structure

```
minutes-data/  (ap-east-1, 單一 bucket)
  jobs/{job_id}/input.mp4
  jobs/{job_id}/audio.wav
  jobs/{job_id}/whisper_result.json
  jobs/{job_id}/diarize_result.json
  jobs/{job_id}/ocr_result.json
  jobs/{job_id}/merged_transcript.txt
  jobs/{job_id}/meeting_minutes.md
```

Lifecycle rules: 自動刪除 30 日前嘅 jobs（input + intermediate files）。Output 保留 90 日。

---

## Step Functions State Machine

```
StartPipeline (讀 DynamoDB 攞 user requirements + email)
  → ExtractAudio (Lambda, 10min timeout)
    → RunProcessor (Batch g4dn.xlarge Spot .sync, 30min timeout, retry 2x)
      │  單一 container 並行跑：
      │  ├─ Whisper STT (GPU)
      │  ├─ pyannote diarization (CPU, parallel)
      │  └─ GOT-OCR2.0 (GPU, after Whisper)
      │  → 寫 3 個 result JSON 去 S3
    → MergeTranscript (Lambda, 5min timeout)
      → Summarize (Lambda, 15min timeout, retry 3x)
        │  ← Bedrock DeepSeek V3.2 (ap-southeast-1)
        │  ← 用 DynamoDB requirements 決定語言、字數、格式
        → SendEmail (Lambda: SES ap-southeast-1)
          │  email .md 內容 + presigned download link
          → UpdateStatus (DynamoDB → "completed")
            → SUCCESS

  [Any failure] → NotifyFailure (Lambda: SES error email) → FAILED
```

States 之間傳 S3 keys（唔傳 data），避免 Step Functions 256KB payload limit。

---

## Key Implementation Details

### Container: Processor (containers/processor/)

單一 Docker image 包含所有 processing models：

```dockerfile
FROM nvidia/cuda:12.1-runtime-ubuntu22.04

# Models baked in (避免 runtime download):
# - faster-whisper large-v3-turbo (~3GB)
# - pyannote segmentation + embedding models (~500MB, 需要 HF token)
# - GOT-OCR2.0 (~1.5GB)
# Total image size: ~8-10GB
```

**entrypoint.py 邏輯：**
```
1. Download audio.wav from S3
2. 並行啟動:
   ├─ Thread A (GPU): Whisper STT → whisper_result.json
   └─ Thread B (CPU): pyannote diarization → diarize_result.json
3. Whisper 完成後 (GPU 空出):
   └─ GOT-OCR2.0: extract frames + OCR → ocr_result.json
4. Upload 3 個 JSON 去 S3
```

- g4dn.xlarge: T4 16GB VRAM, 4 vCPU, 16GB RAM
- Whisper (~2GB VRAM) + GOT-OCR (~3GB VRAM) 輕鬆放入 T4 16GB
- pyannote 用 CPU + RAM，同 GPU 唔衝突
- AWS Batch Spot, max 2 concurrent instances, auto-terminate on completion

### Lambda: Summarize (lambdas/summarize/)

```python
bedrock = boto3.client('bedrock-runtime', region_name='ap-southeast-1')

# DeepSeek V3.2: 164K context, 685B MoE (37B active)
response = bedrock.invoke_model(
    modelId='deepseek.v3.2',
    body=json.dumps({
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096
    })
)
```

- 164K context window — 大部分 1 小時會議嘅 transcript (~50K-80K tokens) 唔需要 chunking
- 超長會議仍保留 parent-child chunking 作為 fallback
- **從 DynamoDB 讀取 user requirements，動態調整 system prompt**：
  - `output_language` → 指定輸出語言（繁中/簡中/英文）
  - `summary_length` → 控制生成字數（short ~300 / medium ~800 / detailed ~1500）
  - `output_format` → 會議紀錄 / 行動項目 / 兩者都要
  - `custom_instructions` → 附加到 system prompt 尾
- DeepSeek V3.2 中文能力極強，benchmark 超越大部分模型

### Lambda: Notify (lambdas/notify/)

```python
ses = boto3.client('ses', region_name='ap-southeast-1')
```

- **成功 email 內容**：
  - Subject: 「你嘅會議紀錄已準備好 — {job_id 前 8 位}」
  - Body: meeting minutes 全文（inline in email body）
  - Presigned download link for .md 檔案（7 日有效）
- **失敗 email 內容**：
  - Subject: 「會議紀錄處理失敗」
  - Body: error message + 建議重新上傳
- Sender: SES verified domain（例如 minutes@yourcompany.com）

### Frontend (frontend/)

- 純 HTML/CSS/JS，無 framework
- **Step 1 — Requirements 表單**：
  - Email 地址（必填，用嚟收結果）
  - 輸出語言：繁體中文 / 簡體中文 / English（dropdown）
  - Summary 長度：簡短 ~300字 / 標準 ~800字 / 詳細 ~1500字（dropdown）
  - 輸出格式：會議紀錄 / 行動項目 / 兩者都要（radio）
  - 額外要求：free text（optional，例如「重點列出技術決策」「忽略閒聊部分」）
- **Step 2 — Upload 錄影**：
  - Presigned URL + XHR progress bar（支援 500MB+ 檔案）
  - Upload 完成後顯示確認頁面：「已提交，完成後會 email 通知你」
- **唔需要 status polling / download 頁面** — 全部由 email 送達
- Auth: API key（internal use），之後可以加 Cognito

### API Gateway

- `POST /jobs` → 接收 requirements + email，生成 presigned URL + job_id，寫入 DynamoDB
- `GET /jobs/{job_id}` → job 狀態（optional，主要畀 debug 用）
- API key validation, throttle 10 req/min

---

## Code Mapping (transcribe.py → AWS)

| 原有函數 | AWS 對應 | 重用程度 |
|---|---|---|
| `convert_to_wav()` | `lambdas/extract_audio/` | 改用 ffmpeg subprocess |
| `transcribe_audio()` | `containers/processor/` (Whisper) | 幾乎原封不動，改 CUDA |
| `diarize_audio()` | `containers/processor/` (pyannote) | 幾乎原封不動 |
| `extract_participant_names()` | `containers/processor/` (GOT-OCR) | 改用 GOT-OCR2.0 |
| `extract_slides()` | `containers/processor/` (GOT-OCR) | 改用 GOT-OCR2.0 |
| `merge_transcript()` | `lambdas/merge_transcript/` | 直接搬 |
| `format_time()` | `lambdas/merge_transcript/` | 直接搬 |
| `_llm_call()` | `lambdas/summarize/` | 改用 Bedrock DeepSeek V3.2 API |
| `generate_minutes()` | `lambdas/summarize/` | 簡化（164K context 大部分唔使 chunk） |
| `main()` | Step Functions | 完全重寫 |
| N/A (new) | `lambdas/submit_job/` | 新：requirements 表單 + presigned URL |
| N/A (new) | `lambdas/notify/` | 新：cross-region SES email |

---

## Implementation Phases

### Phase 1: Foundation
- CDK project setup (ap-east-1)
- StorageStack (S3 bucket)
- ApiStack (API Gateway + DynamoDB + submit_job Lambda)
- DynamoDB table schema（包含 requirements + email）
- **驗證**: curl POST /jobs with requirements JSON，確認 presigned URL + job_id 可用

### Phase 2: Processor Container + Batch
- Build 單一 Docker image（Whisper + pyannote + GOT-OCR2.0）
- 本地測試：輸入 WAV → 輸出 3 個 JSON
- Push to ECR (ap-east-1)
- 設定 Batch compute env (g4dn.xlarge Spot) + job queue
- **驗證**: 手動 submit job，確認所有 output 正確
- **風險最高**：Spot instance availability, CUDA driver, container image size

### Phase 3: Audio Extraction
- extract_audio Lambda + ffmpeg layer
- **驗證**: 上傳 MP4 到 S3，手動 invoke Lambda，檢查 WAV output

### Phase 4: Merge + Summarize
- Port merge_transcript() 到 Lambda
- summarize Lambda + cross-region Bedrock DeepSeek V3.2 (ap-southeast-1)
- 開通 Bedrock model access for DeepSeek in ap-southeast-1
- **驗證**: 用真實 merged transcript 測試 DeepSeek output 質量（中文 + 英文）

### Phase 5: Step Functions
- 定義 state machine，串連所有 components
- 加 S3 trigger Lambda
- **驗證**: 端對端測試 — upload MP4 → 等 → 檢查 S3 output

### Phase 6: SES Email + Frontend
- SES domain/email verification (ap-southeast-1)
- SES 申請 production access
- notify Lambda（cross-region SES call，成功 + 失敗 email）
- Frontend HTML/CSS/JS（requirements 表單 + upload）→ S3 + CloudFront
- **驗證**: 完整 user journey — 填 requirements → upload → 等 email → 收到結果

### Phase 7: Production Readiness
- CloudWatch alarms + dashboard
- IAM least privilege review（注意 cross-region IAM policy）
- AWS Budget alert ($50/month)
- S3 lifecycle rules

---

## Cost Estimate (1 meeting/day, ~1 hour)

| 服務 | 每次 | 每月 (×30) |
|---|---|---|
| Batch g4dn.xlarge Spot (~15min, Whisper+Diarize+OCR) | $0.08 | $2.40 |
| Bedrock DeepSeek V3.2 (summarize, ~80K input + ~2K output) | $0.06 | $1.80 |
| Lambda (all) | $0.01 | $0.30 |
| S3 (ap-east-1) | — | $0.50 |
| Step Functions | $0.01 | $0.30 |
| SES email | ~$0 | ~$0.01 |
| CloudFront + API Gateway | — | $1.00 |
| **Total** | **~$0.16** | **~$6/月** |

g4dn.xlarge Spot (ap-east-1) 估計 ~$0.30/hr。
DeepSeek V3.2: $0.74/1M input + $2.22/1M output。
SES 首 62,000 封 email/月免費（從 Lambda 發送）。

---

## Prerequisites (手動)

1. 確認 ap-east-1 有 g4dn.xlarge Spot 容量（Service Quotas → EC2 Spot）
2. 開通 Bedrock model access for DeepSeek V3.2（ap-southeast-1 Tokyo）
3. HuggingFace token（build container 時下載 pyannote gated models + GOT-OCR2.0）
4. 設定 AWS Budget alert
5. SES (ap-southeast-1)：verify sender email/domain
6. SES (ap-southeast-1)：申請 production access（移出 sandbox）
7. Cross-region IAM policy：Lambda 要有權限 call Tokyo Bedrock + Singapore SES
