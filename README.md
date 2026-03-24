# voice-text-minutes

將廣東話/中文會議錄音自動轉成結構化 Meeting Minutes。

支援 Teams 會議錄影（mp4 含畫面）同純錄音（mp4/m4a/wav）。

## 兩種運行模式

### 1. 本地模式（Local）

用 `transcribe.py` 喺本地電腦運行，適合開發同測試。

### 2. AWS 雲端模式（Production）

同事透過 Web frontend upload 錄影 + 填 requirements，全自動處理，完成後 email 通知。

```
Frontend (CloudFront)
  → 填 requirements (語言/字數/格式) + upload 錄影
  → API Gateway (ap-east-1)

S3 Upload Event (ap-east-1)
  → Step Functions Pipeline:
    ├─ AWS Batch (g4dn.xlarge Spot, custom AMI):
    │   ├─ Whisper STT (GPU)
    │   ├─ pyannote diarization (CPU, parallel)
    │   └─ GOT-OCR2.0 (GPU)
    ├─ Lambda: merge transcript
    ├─ Lambda: Bedrock DeepSeek V3.2 summarize (ap-northeast-1)
    └─ Lambda: SES email results (ap-southeast-1, cross-account)
```

## 功能

- **語音辨識** — faster-whisper (large-v3-turbo)，支援廣東話 (yue)
- **講者分辨** — pyannote-audio speaker diarization
- **OCR** — GOT-OCR2.0 (AWS) / easyocr (local)，從 Teams 錄影擷取 slide 文字 + 參與者名字
- **Meeting Minutes** — Bedrock DeepSeek V3.2 (AWS) / LM Studio (local) 整理成結構化會議紀錄
- **User Requirements** — 同事可揀輸出語言、字數、格式，加自訂要求
- **Email 通知** — 完成後自動 SES email 結果畀 user（sender: minutes@msphk.info）
- **Parent-Child Chunking** — 長 transcript 自動分段摘要，保留全局 context

## AWS 架構

| 組件 | Region | 服務 |
|---|---|---|
| Upload + Processing | ap-east-1 (HK) | S3, Batch g4dn.xlarge Spot, Lambda, Step Functions, DynamoDB, API Gateway |
| Custom AMI | ap-east-1 (HK) | EC2 Image Builder (AL2023 ECS GPU + ML models pre-baked) |
| Summarization | ap-northeast-1 (Tokyo) | Bedrock DeepSeek V3.2 (cross-region call) |
| Email 通知 | ap-southeast-1 (SG) | SES cross-account (5070 account, domain msphk.info) |
| Frontend | Global | CloudFront → S3 |
| Source Code | ap-east-1 (HK) | CodeCommit + GitHub |
| Container Build | ap-east-1 (HK) | CodeBuild → ECR |

### Custom AMI + Thin Docker

ML models 同 Python packages 預裝喺 custom AMI（via EC2 Image Builder），唔係放喺 Docker image。
Docker image 只有 ~50MB（AL2023-minimal + libsndfile + entrypoint.py）。
Batch job definition 用 volume mounts 將 AMI 上嘅 `/opt/processor` 掛入 container。
好處：cold start 快，唔使每次 pull 15GB image。

## 本地使用（開發/測試）

```bash
# 1. Clone
git clone https://github.com/AustinLeungCK/voice-text-minutes.git
cd voice-text-minutes

# 2. 建立虛擬環境
python -m venv venv
source venv/Scripts/activate  # Windows (Git Bash)

# 3. 安裝依賴
pip install -r requirements.txt

# 4. 運行
python transcribe.py recordings/meeting.mp4 --output output/meeting_minutes.md
```

### LM Studio 設定（本地模式）

1. 開啟 LM Studio，載入中文模型（推薦 Qwen 系列）
2. 啟動 Local Server（預設 localhost:1234）
3. Context Length: 盡量大（如 131072）

## AWS 部署

```bash
# Build + Deploy
sam build --profile 0362 --region ap-east-1
sam deploy --profile 0362 --region ap-east-1

# Build custom AMI (first time + when updating ML models)
aws imagebuilder start-image-pipeline-execution \
  --image-pipeline-arn <pipeline-arn> --profile 0362 --region ap-east-1

# Build processor container (after code changes)
aws codebuild start-build \
  --project-name prod-voice-meeting-minutes-build --profile 0362 --region ap-east-1
```

詳細部署計劃見 [AWS_MIGRATION_PLAN.md](AWS_MIGRATION_PLAN.md)。

## 檔案結構

```
voice-text-minutes/
├── transcribe.py              # 本地版主程式
├── template.yaml              # SAM template（所有 AWS resources）
├── samconfig.toml             # SAM deploy config
├── buildspec.yml              # CodeBuild buildspec
├── statemachine/
│   └── pipeline.asl.json      # Step Functions state machine
├── lambdas/
│   ├── submit_job/            # API: 接收 requirements + presigned URL
│   ├── start_pipeline/        # EventBridge S3 event → 啟動 Step Functions
│   ├── merge_transcript/      # 合併 Whisper + diarization + OCR
│   ├── summarize/             # Bedrock DeepSeek V3.2 摘要 (ap-northeast-1)
│   ├── notify/                # SES email 通知 (cross-account 5070)
│   └── get_status/            # 查詢 job 狀態
├── containers/
│   └── processor/
│       ├── Dockerfile         # AL2023-minimal + libsndfile（~50MB）
│       └── entrypoint.py      # Batch job 入口（packages from host AMI mount）
├── imagebuilder/
│   ├── component.json         # Image Builder component（ML stack install）
│   └── component.yml          # Component YAML reference（unused, JSON required）
├── frontend/
│   ├── index.html             # Requirements 表單 + upload
│   ├── style.css
│   └── app.js
├── recordings/                # 本地錄音檔（gitignored）
└── output/                    # 本地輸出結果（gitignored）
```

## 技術細節

### Parent-Child Chunking

長會議 transcript 超出 LLM context window 時自動啟用：

1. **Parent（大綱）**：取頭尾各 5 行，生成全局大綱
2. **Child（逐段）**：每段 + 大綱 context，分別摘要
3. **合併**：將所有摘要合併成最終 meeting minutes

### 並行處理（AWS Batch）

單一 g4dn.xlarge instance 內並行：

| 步驟 | 運算資源 |
|---|---|
| Whisper STT | GPU (CUDA) |
| pyannote diarization | CPU (parallel with Whisper) |
| GOT-OCR2.0 | GPU (after Whisper) |

ML packages 同 models 預裝喺 custom AMI，Docker container 透過 volume mount 存取。
