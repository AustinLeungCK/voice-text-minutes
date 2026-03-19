# AWS Migration Plan — voice-text-minutes

## Context

將現有嘅本地廣東話會議錄音工具遷移到 AWS，加一個簡單 frontend 畀 PM upload video，用 Step Functions 串連整個 pipeline。選用方案 B（Whisper on GPU + Bedrock Claude）。

---

## Architecture Overview

```
Frontend (S3 + CloudFront)
  → API Gateway (presigned URL / status / download)
    → DynamoDB (job status tracking)

S3 Upload Event
  → Lambda (start_pipeline)
    → Step Functions
      ├─ Lambda: extract_audio (ffmpeg, MP4→WAV)
      │
      ├─ [Parallel]
      │   ├─ AWS Batch (g5.xlarge Spot): Whisper STT
      │   ├─ ECS Fargate: pyannote diarization
      │   └─ Lambda: extract_frames → Lambda: Bedrock Haiku Vision OCR
      │
      ├─ Lambda: merge_transcript
      ├─ Lambda: summarize (Bedrock Claude Sonnet)
      └─ Lambda: notify (SNS + DynamoDB update)
        → S3 (output .md)
```

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
│       ├── storage_stack.py       # S3 buckets
│       ├── api_stack.py           # API Gateway + DynamoDB + auth
│       ├── processing_stack.py    # Step Functions + Batch + Fargate + Lambdas
│       ├── notification_stack.py  # SNS
│       └── frontend_stack.py      # S3 static hosting + CloudFront
├── lambdas/
│   ├── presign_upload/handler.py  # 生成 presigned URL + job_id
│   ├── start_pipeline/handler.py  # S3 event → 啟動 Step Functions
│   ├── extract_audio/handler.py   # ffmpeg MP4→WAV
│   ├── extract_frames/handler.py  # ffmpeg 抽 video frames
│   ├── ocr_vision/handler.py      # Bedrock Claude Haiku Vision OCR
│   ├── merge_transcript/handler.py# 合併 whisper + diarization
│   ├── summarize/handler.py       # Bedrock Claude Sonnet 生成 minutes
│   ├── notify/handler.py          # SNS 通知 + DynamoDB 更新
│   └── get_status/handler.py      # 查詢 job 狀態
├── containers/
│   ├── whisper/
│   │   ├── Dockerfile             # nvidia/cuda + faster-whisper + 模型 baked in
│   │   └── entrypoint.py          # S3 download → transcribe → S3 upload
│   └── diarize/
│       ├── Dockerfile             # python-slim + torch CPU + pyannote + 模型 baked in
│       └── entrypoint.py          # S3 download → diarize → S3 upload
├── frontend/
│   ├── index.html                 # 單頁應用
│   ├── style.css
│   └── app.js                     # Upload + status polling + download
└── tests/
    ├── test_merge_transcript.py
    └── test_summarize.py
```

---

## S3 Key Structure

```
minutes-uploads/
  jobs/{job_id}/input.mp4
  jobs/{job_id}/audio.wav
  jobs/{job_id}/frames/names/*.jpg
  jobs/{job_id}/frames/slides/*.jpg
  jobs/{job_id}/whisper_result.json
  jobs/{job_id}/diarize_result.json
  jobs/{job_id}/ocr_result.json

minutes-output/
  jobs/{job_id}/meeting_minutes.md
  jobs/{job_id}/transcript.txt
```

Lifecycle rules: uploads bucket 刪 7 日後、output bucket 保留 90 日。

---

## Step Functions State Machine

```
StartPipeline
  → ExtractAudio (Lambda, 10min timeout)
    → Parallel:
      ├─ Branch A: RunWhisper (Batch .sync, 30min timeout, retry 2x)
      ├─ Branch B: RunDiarize (ECS .sync, 30min timeout, retry 2x)
      └─ Branch C: Parallel:
      │     ├─ ExtractFramesNames (Lambda) → OcrNames (Lambda)
      │     └─ ExtractFramesSlides (Lambda) → OcrSlides (Lambda)
    → MergeTranscript (Lambda)
      → Summarize (Lambda, 15min timeout, retry 3x for Bedrock throttling)
        → Notify (Lambda)
          → SUCCESS

  [Any failure] → NotifyFailure (Lambda) → FAILED
```

States 之間傳 S3 keys（唔傳 data），避免 Step Functions 256KB payload limit。

---

## Key Implementation Details

### Container: Whisper (containers/whisper/)
- Base: `nvidia/cuda:12.1-runtime-ubuntu22.04`
- 模型 `large-v3-turbo` baked into image（~3GB，避免 runtime download）
- `language="yue"`, `beam_size=5`, `device="cuda"`, `compute_type="float16"`
- AWS Batch: g5.xlarge Spot, max 2 instances, auto-terminate

### Container: Diarize (containers/diarize/)
- Base: `python:3.11-slim` + torch CPU-only
- pyannote models baked into image（需要 HF token build-time download）
- 用 soundfile 載入 WAV → waveform tensor（同現有 code 一樣）
- ECS Fargate Spot: 4 vCPU, 16GB RAM

### Lambda: OCR Vision (lambdas/ocr_vision/)
- 用 Bedrock Claude Haiku Vision 取代 easyocr
- Names mode: 分析 Teams UI screenshot，識別參與者名字
- Slides mode: 提取 slide 文字內容
- Batch frames 5-10 張一次 API call

### Lambda: Summarize (lambdas/summarize/)
- 改用 `boto3.client('bedrock-runtime')` 叫 Claude Sonnet
- chunk_lines 由 400 提高到 2000（Claude 200K context）
- 保留 parent-child chunking 但大部分會議唔需要 chunk
- System prompt 同現有完全一樣（繁中）

### Frontend (frontend/)
- 純 HTML/CSS/JS，無 framework
- Upload: presigned URL + XHR progress bar（支援 500MB+ 檔案）
- Status: 每 10 秒 poll GET /status/{job_id}
- Download: presigned URL 下載 .md + .txt
- Auth: API key（internal use），之後可以加 Cognito

### API Gateway
- `POST /upload` → presigned URL + job_id
- `GET /status/{job_id}` → job 狀態
- `GET /download/{job_id}` → presigned download URL
- API key validation, throttle 10 req/min

---

## Code Mapping (transcribe.py → AWS)

| 原有函數 | AWS 對應 | 重用程度 |
|---|---|---|
| `convert_to_wav()` | `lambdas/extract_audio/` | 改用 ffmpeg subprocess |
| `transcribe_audio()` | `containers/whisper/` | 幾乎原封不動，改 CUDA |
| `diarize_audio()` | `containers/diarize/` | 幾乎原封不動 |
| `extract_participant_names()` | `lambdas/extract_frames/` + `ocr_vision/` | 改用 Claude Vision |
| `extract_slides()` | `lambdas/extract_frames/` + `ocr_vision/` | 改用 Claude Vision |
| `merge_transcript()` | `lambdas/merge_transcript/` | 直接搬 |
| `format_time()` | `lambdas/merge_transcript/` | 直接搬 |
| `_llm_call()` | `lambdas/summarize/` | 改用 Bedrock API |
| `generate_minutes()` | `lambdas/summarize/` | 改 chunk size + Bedrock |
| `main()` | Step Functions | 完全重寫 |

---

## Implementation Phases

### Phase 1: Foundation
- CDK project setup
- StorageStack (S3 buckets)
- ApiStack (API Gateway + DynamoDB + presign/status Lambdas)
- **驗證**: curl POST /upload，確認 presigned URL 可用

### Phase 2: Audio Extraction
- extract_audio Lambda + ffmpeg layer
- **驗證**: 上傳 MP4 到 S3，手動 invoke Lambda，檢查 WAV output

### Phase 3: Whisper Container + Batch
- Build Docker image，本地測試
- Push to ECR，設定 Batch compute env + job queue
- **驗證**: 手動 submit job，確認 JSON output 格式正確
- **風險最高**：GPU instance availability, CUDA driver, model loading time

### Phase 4: Pyannote Container + Fargate
- Build Docker image，本地測試
- Push to ECR，設定 Fargate task definition
- **驗證**: 手動 run task，確認 JSON output

### Phase 5: OCR via Bedrock
- extract_frames Lambda
- ocr_vision Lambda + Bedrock Haiku access
- **驗證**: 用 Teams screenshot 測試 OCR 質量

### Phase 6: Merge + Summarize
- Port merge_transcript() 到 Lambda
- Port generate_minutes() 到 Lambda + Bedrock Sonnet
- **驗證**: 用真實 transcript 測試 summarize output

### Phase 7: Step Functions
- 定義 state machine，串連所有 components
- 加 S3 trigger Lambda
- **驗證**: 端對端測試 — upload MP4 → 等 → 檢查 output

### Phase 8: Notifications + Frontend
- SNS topic + email subscription
- Frontend HTML/CSS/JS → S3 + CloudFront
- **驗證**: 完整 user journey 測試

### Phase 9: Production Readiness
- CloudWatch alarms + dashboard
- IAM least privilege review
- AWS Budget alert ($50/month)
- S3 lifecycle rules

---

## Cost Estimate (1 meeting/day, ~1 hour)

| 服務 | 每次 | 每月 (×30) |
|---|---|---|
| AWS Batch g5.xlarge Spot (~20min) | $0.33 | $9.90 |
| ECS Fargate 4vCPU/16GB (~15min) | $0.05 | $1.50 |
| Bedrock Haiku (OCR) | $0.02 | $0.60 |
| Bedrock Sonnet (summarize) | $0.30 | $9.00 |
| Lambda (all) | $0.01 | $0.30 |
| S3 | — | $0.50 |
| Step Functions | $0.01 | $0.30 |
| CloudFront + API Gateway | — | $1.00 |
| **Total** | **~$0.72** | **~$23/月** |

---

## Prerequisites (手動)
1. AWS Account 開通 Bedrock model access（Claude Haiku + Sonnet）
2. HuggingFace token（build pyannote container 時需要下載 gated models）
3. 設定 AWS Budget alert
