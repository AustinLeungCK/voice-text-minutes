# Precis（精記）

將廣東話/中文會議錄音自動轉成結構化會議紀錄。

支援 Teams 會議錄影（mp4 含畫面）同純錄音（mp4/m4a/wav）。
Frontend：https://minutes.msphk.info

## 功能

- **語音辨識** — faster-whisper (large-v3-turbo)，支援廣東話 (yue)
- **講者分辨** — pyannote-audio speaker diarization（GPU）
- **OCR** — GOT-OCR2.0 從 Teams 錄影擷取 slide 文字 + 參與者名字
- **會議紀錄** — Bedrock DeepSeek V3.2 整理成結構化紀錄（6 段式：摘要→與會者→討論→決議→行動表→下次會議）
- **AI 調整** — 用戶收到結果後可喺 frontend 輸入指令調整（例如「加多啲 action items」「翻譯做英文」）
- **DOCX 輸出** — Email 附件用 Word 格式，唔係 raw markdown
- **Cognito 認證** — Admin-only 帳號管理，自訂 login UI，忘記密碼 + 改密碼
- **Spot 中斷處理** — 每步 checkpoint 到 S3，中斷後自動 resume
- **Parent-Child Chunking** — 長 transcript 自動分段摘要，保留全局 context

## 兩種運行模式

### 1. 本地模式（Local）

用 `transcribe.py` 喺本地電腦運行，適合開發同測試。

### 2. AWS 雲端模式（Production）

同事透過 Web frontend 登入 → upload 錄影 + 填 requirements → 全自動處理 → email 通知（DOCX 附件）→ 可喺 frontend AI 調整。

```
Frontend（CloudFront + minutes.msphk.info）
  → Cognito 登入
  → 填 requirements（語言/字數/格式）+ upload 錄影
  → API Gateway (ap-east-1)

S3 Upload Event (ap-east-1)
  → Step Functions Pipeline:
    ├─ AWS Batch (g4dn.xlarge Spot, custom AMI):
    │   ├─ Whisper STT (GPU)              ~8 min
    │   ├─ pyannote diarization (GPU)     ~3 min
    │   └─ GOT-OCR2.0 (GPU)              ~3 min
    │   每步 checkpoint 到 S3（Spot resume）
    ├─ Lambda: merge transcript
    ├─ Lambda: Bedrock DeepSeek V3.2 summarize
    └─ Lambda: SES email + DOCX 附件

用戶可喺 frontend 查看 + AI 調整：
  → POST /jobs/{id}/refine → Bedrock → 更新紀錄
```

## AWS 架構

| 組件 | Region | 服務 |
|---|---|---|
| Upload + Processing | ap-east-1 (HK) | S3, Batch g4dn.xlarge Spot, Lambda, Step Functions, DynamoDB, API Gateway |
| Custom AMI | ap-east-1 (HK) | EC2 Image Builder (AL2023 ECS GPU + ML models pre-baked) |
| Summarization + Refine | ap-northeast-1 (Tokyo) | Bedrock DeepSeek V3.2 |
| Email 通知 | ap-southeast-1 (SG) | SES（0362 account，domain msphk.info） |
| Frontend | Global | CloudFront → S3，custom domain minutes.msphk.info |
| Auth | ap-east-1 (HK) | Cognito User Pool（admin-only 開帳號） |
| Source Code | ap-east-1 (HK) | CodeCommit + GitHub |
| Container Build | ap-east-1 (HK) | CodeBuild → ECR |

### Custom AMI + Slim Docker

ML models 同 Python packages 預裝喺 custom AMI（via EC2 Image Builder），唔係放喺 Docker image。
Docker image 只有 ~54MB（AL2023-minimal + python3.11 + libsndfile + entrypoint.py）。
Batch job definition 用 volume mounts 將 AMI 上嘅 `/opt/processor` 掛入 container。
好處：cold start 快，唔使每次 pull 大 image。AMI 好少改，code 改動只需 CodeBuild ~30 秒。

### 安全

- Cognito JWT 認證所有 API endpoint
- CORS lock 到 `https://minutes.msphk.info`
- CloudFront security headers（HSTS, CSP, X-Frame-Options）
- Server-side input validation + XSS prevention
- API endpoint ownership check（防 IDOR）
- 禁止自助註冊

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
export PATH="$LOCALAPPDATA/Programs/Python/Python312:$PATH"
sam build --profile 0362 --region ap-east-1
sam deploy --profile 0362 --region ap-east-1 --parameter-overrides "ProcessorAmiId=ami-031e7d8cb5001a0b4"

# Build processor container (after code changes)
aws codebuild start-build \
  --project-name prod-voice-meeting-minutes-build --profile 0362 --region ap-east-1

# Build custom AMI (only when updating ML models/packages)
aws imagebuilder start-image-pipeline-execution \
  --image-pipeline-arn <pipeline-arn> --profile 0362 --region ap-east-1

# Create new user
aws cognito-idp admin-create-user --user-pool-id ap-east-1_6BilvzaAu \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --temporary-password "TempPass123!" --profile 0362 --region ap-east-1
aws cognito-idp admin-set-user-password --user-pool-id ap-east-1_6BilvzaAu \
  --username user@example.com --password "FinalPass123!" --permanent \
  --profile 0362 --region ap-east-1
```

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
│   ├── list_jobs/             # API: 列出用戶嘅 job 歷史（Cognito email filter）
│   ├── get_status/            # API: 查詢 job 狀態（ownership check）
│   ├── merge_transcript/      # 合併 Whisper + diarization + OCR
│   ├── summarize/             # Bedrock DeepSeek V3.2 摘要
│   ├── refine/                # Bedrock AI 調整會議紀錄
│   ├── notify/                # SES email + DOCX 附件
│   └── cleanup_orphans/       # 每日清理 orphan records
├── containers/
│   └── processor/
│       ├── Dockerfile         # AL2023-minimal + python3.11 + libsndfile（~54MB）
│       └── entrypoint.py      # Batch job：sequential GPU pipeline + Spot checkpoint
├── imagebuilder/
│   └── component.json         # Image Builder component（ML stack install）
├── frontend/
│   ├── index.html             # Login + sidebar + wizard + history + detail + settings
│   ├── style.css              # Design system（teal-slate + dark/light theme）
│   ├── app.js                 # Cognito auth + i18n + upload + refine
│   └── config.json            # API URL + Cognito config（gitignored）
├── recordings/                # 本地錄音檔（gitignored）
└── output/                    # 本地輸出結果（gitignored）
```

## 處理時間（1 小時會議，467MB MP4）

| 階段 | 時間 |
|---|---|
| S3 download + FFmpeg extract | ~2 min |
| Whisper STT (GPU) | ~8 min |
| pyannote diarization (GPU) | ~3 min |
| GOT-OCR2.0 (GPU) | ~3 min |
| Merge + Summarize + Notify | ~1 min |
| **Total** | **~15 min** |
