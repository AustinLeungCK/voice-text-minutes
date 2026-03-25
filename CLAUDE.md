# CLAUDE.md — Precis（精記）開發指引

## 項目簡介

Precis（精記）— 將廣東話/中文會議錄音自動轉成結構化會議紀錄嘅 SaaS 產品。
兩種模式：本地（transcribe.py + LM Studio）同 AWS（SAM + Batch + Bedrock）。
Frontend: https://minutes.msphk.info

## 架構

### 本地模式（transcribe.py）

```
transcribe.py（單一檔案，~450 行）
├── convert_to_wav()              — 音頻提取（av library）
├── extract_participant_names()   — 從 Teams UI OCR 參與者名字（easyocr）
├── extract_slides()              — 從影片 OCR slide 內容（easyocr）
├── transcribe_audio()            — 語音轉文字（faster-whisper, large-v3-turbo）
├── diarize_audio()               — 講者分辨（pyannote-audio 4.x）
├── merge_transcript()            — 合併 whisper segments + 講者標記
├── generate_minutes()            — LLM 摘要（LM Studio, parent-child chunking）
│   ├── _llm_call()               — 單次 LLM API call
│   ├── Step 1: 全局大綱（Parent）
│   ├── Step 2: 逐段摘要（Child）
│   └── Step 3: 合併最終結果
└── main()                        — CLI 入口，並行處理
```

### AWS 模式（SAM）

```
Frontend（CloudFront + minutes.msphk.info → S3）
  → Cognito 登入（自訂 UI，admin-only 開帳號）
  → API Gateway (ap-east-1) → Lambda: submit_job → DynamoDB + S3 presigned URL

S3 upload event (EventBridge) → Lambda: start_pipeline → Step Functions:
  ├─ Batch g4dn.xlarge Spot (ap-east-1)：
  │   ├─ Whisper STT（GPU）           — faster-whisper large-v3-turbo
  │   ├─ pyannote diarization（GPU）  — Whisper 完再跑（sequential GPU）
  │   └─ GOT-OCR2.0（GPU）           — diarization 完再跑
  │   每步 checkpoint 到 S3，Spot 中斷後可 resume
  ├─ Lambda: merge_transcript
  ├─ Lambda: summarize              — Bedrock DeepSeek V3.2（ap-northeast-1 Tokyo）
  └─ Lambda: notify                 — SES email + DOCX 附件（ap-southeast-1，0362 account）

用戶收到 email 後可以喺 frontend 查看 + AI 調整：
  → API Gateway: POST /jobs/{id}/refine → Lambda: refine → Bedrock → 更新 S3
```

### Multi-Region 策略

| 服務 | Region | 原因 |
|---|---|---|
| S3, Batch, Lambda, Step Functions, DynamoDB, API GW, CodeCommit, CodeBuild, ECR, Image Builder | ap-east-1（HK） | 同事 upload 快，g4dn Spot 有容量 |
| Bedrock DeepSeek V3.2 | ap-northeast-1（Tokyo） | DeepSeek 喺 SG/HK 無，Tokyo 有 |
| SES | ap-southeast-1（SG） | HK 無 SES；0362 account 已 verify msphk.info |
| CloudFront | Global | CDN，custom domain minutes.msphk.info |

### SES 設定（0362 account 直接 send）

SES 喺 account 036293420772（profile: 0362），region ap-southeast-1。
Domain `msphk.info` 已 verify（DKIM）。Sender 地址：`minutes@msphk.info`。
仲係 sandbox mode，recipient 需要逐個 verify 或者 verify domain。
Email 附件用 DOCX format（python-docx），唔係 inline markdown。

### Cognito 認證

User Pool: `ECVHK_internal`（ap-east-1_6BilvzaAu）
Client ID: `1q6id8nmmri2olooak025gscd5`
Auth flow: `USER_PASSWORD_AUTH`（自訂 login UI，唔用 Hosted UI）
帳號由 admin 用 `admin-create-user` + `admin-set-user-password --permanent` 建立。
Frontend 支援：登入、忘記密碼（ForgotPassword + ConfirmForgotPassword）、設定頁改密碼（ChangePassword）。

### Custom AMI（EC2 Image Builder）

Base：ECS-optimized AL2023 GPU AMI（已有 NVIDIA drivers、ECS agent、Docker）。
Image Builder 加裝：Python 3.11、venv `/opt/processor/venv/`（torch+CUDA、faster-whisper、
pyannote、GOT-OCR2.0、transformers、matplotlib、accelerate）。ML models 預下載到 `/opt/processor/models/`。
ffmpeg static binary 放 `/usr/local/bin/`。
當前 AMI: `ami-031e7d8cb5001a0b4`（v1.0.9），EBS 35GB。

Docker image ~54MB（AL2023-minimal + python3.11 + libsndfile）。Python packages 同 models
從 host AMI 透過 Batch job definition volumes 掛入 container（`/opt/processor`、`/usr/local/bin`）。
Container 要裝 python3.11 因為 venv symlink 指向 `/usr/bin/python3.11`。

### Spot 中斷處理

- SIGTERM handler：收到 Spot 2 分鐘 warning 時標記 shutdown
- Checkpoint：每步（whisper → diarize → ocr）完成即 upload 結果到 S3
- Resume：restart 時 check S3 有冇 checkpoint，有就 skip
- EvaluateOnExit：只 Spot interruption（"Host EC2*"）才 retry，app bug 唔 retry
- Retry attempts: 3，timeout: 60 分鐘

### 安全措施

- Cognito JWT 認證所有 API endpoint
- CORS lock 到 `https://minutes.msphk.info`
- CloudFront security headers（HSTS, CSP, X-Frame-Options, X-Content-Type-Options）
- Server-side input validation（file_name sanitize, enum validation）
- Frontend XSS prevention（textContent 代替 innerHTML）
- get_status ownership check（防 IDOR）
- 禁止自助註冊

## 技術決策

### 點解用 soundfile 唔用 torchaudio？
pyannote 4.x 依賴 torchcodec，要 FFmpeg DLLs。Windows 上面唔穩定。
用 `soundfile` 載入 WAV → waveform tensor，繞過 torchcodec。

### 點解用 av 唔用 pydub？
Python 3.14 移除咗 `audioop` module。pydub 依賴佢會壞。
`av`（PyAV）已經係 faster-whisper 嘅依賴，唔使額外裝。

### 點解用 parent-child chunking？
長會議（1 小時+）嘅 transcript 有 1000+ 行（~280K tokens）。
AWS：DeepSeek V3.2 有 164K context，大部分會議唔使 chunk。
本地：Qwen 9B 有 131K context，chunk size 400 行（~66K tokens）。

### 點解全部 GPU sequential 唔用 CPU parallel？
Diarization on CPU 要 25-40 分鐘（1 小時會議），超過 timeout。
GPU sequential（Whisper → diarize → OCR）只要 ~12 分鐘。
T4 16GB VRAM 夠用（peak ~3GB per stage，每步之間 clear GPU）。

### 點解用 g4dn.xlarge 唔用 g5？
g5.xlarge 喺 ap-east-1（HK）無 Spot 容量。g4dn.xlarge（T4 16GB）夠用。

### 點解用 GOT-OCR2.0 唔用 easyocr？
GOT-OCR2.0 中文 F1 ~98%，比 easyocr 好好多。
用 HF-native API：`AutoProcessor` + `model.generate()`（唔係 `.chat()`）。

### 點解用 Bedrock DeepSeek V3.2 唔自己 host Qwen？
DeepSeek V3.2（685B MoE，37B active）中文會議紀錄質素好好多。
164K context 大部分會議唔使 chunking。
唔使 GPU 做 summarization — Lambda call Bedrock API 搞掂。

### 點解唔用 Claude on Bedrock？
Account 限制：billing address / consolidated billing 經 partner，
Anthropic 唔畀用 Claude models on Bedrock。DeepSeek V3.2 係中文最佳替代。

### 點解用 custom AMI + 細 Docker 唔用大 Docker？
大 Docker image（~15GB）每次 Batch cold start 要 pull 幾分鐘，白燒 Spot 時間。
Custom AMI 預裝晒 packages + models。Docker image 只有 ~54MB，幾秒 pull 完。
Host 嘅 `/opt/processor` 透過 volume mount 掛入 container。
AMI 好少改（只有加新 ML model 或升級 PyTorch），container code 改動只需 CodeBuild ~30 秒。

### 離線模式（只限本地）
`HF_HUB_OFFLINE=1` 同 `TRANSFORMERS_OFFLINE=1` 喺 module level 設定。
所有 models 預先下載，運行時唔使 internet。

## 環境

### 本地
- **Python**：3.14（3.10+ 都得）
- **OS**：Windows 11
- **GPU**：AMD Radeon RX 6800 XT（16GB VRAM）via DirectML
- **LLM**：LM Studio（localhost:1234），用 Qwen 3.5 9B

### AWS
- **主 Region**：ap-east-1（HK）
- **Bedrock**：ap-northeast-1（Tokyo）— DeepSeek V3.2，temperature 0.1
- **SES**：ap-southeast-1（SG）— 0362 account，domain msphk.info
- **GPU**：g4dn.xlarge Spot（T4 16GB VRAM），SPOT_CAPACITY_OPTIMIZED
- **AMI**：`ami-031e7d8cb5001a0b4`（v1.0.9，AL2023 ECS GPU + ML stack）
- **IaC**：AWS SAM（template.yaml）
- **CI/CD**：CodeBuild（build processor container → ECR）
- **Source**：CodeCommit（ap-east-1）+ GitHub（origin）
- **Profile**：`--profile 0362`（account 036293420772）
- **Frontend**：CloudFront + S3，custom domain minutes.msphk.info
- **Auth**：Cognito User Pool `ECVHK_internal`

## 常見問題

### `torchcodec` warnings
無害。pyannote 4.x import 時會 print 長 warning。用 soundfile 繞過，唔影響功能。

### `I/O operation on closed file`
多重 wrap sys.stdout 引起。用 `isinstance(sys.stdout, io.TextIOWrapper)` guard 修正。

### Batch compute environment 改名問題
CloudFormation 唔畀 update custom-named Batch compute environment（要 replace）。
所以 Batch compute env / job queue / job definition 用 CF auto-generated names。

### Image Builder component 只接受 JSON
Image Builder 唔接受 YAML 格式嘅 `phases` 定義，要用 JSON。
Component file 放 S3，template 用 `Uri` 引用。

### Docker image `amazonlinux:2023-minimal`
只喺 ECR Public 有（`public.ecr.aws/amazonlinux/amazonlinux:2023-minimal`），
Docker Hub 無。Docker Hub 只有 `amazonlinux:2023`。

### SAM build 需要 Python 3.12
`lambdas/notify/` 有 `requirements.txt`（python-docx），SAM build 需要 Python 3.12。
本機裝咗 Python 3.12 喺 `%LOCALAPPDATA%/Programs/Python/Python312/`。
Build 前 export PATH：`export PATH="$LOCALAPPDATA/Programs/Python/Python312:$PATH"`

### Container image cache
Batch EC2 instance 會 cache Docker image。更新 entrypoint.py 後要：
1. CodeBuild rebuild container
2. Terminate cached instance（或等 Batch scale down）
3. 新 instance 先會 pull 新 image

### GOT-OCR2.0 API
HF-native `GotOcr2ForConditionalGeneration` 冇 `.chat()` method。
要用 `AutoProcessor` + `model.generate()` + `processor.decode()`。

### Venv symlink 問題
AMI 嘅 venv 用 symlink（`python3 → python3.11 → /usr/bin/python3.11`）。
Container 必須裝 `python3.11` 等 symlink resolve 到。

## 指令

```bash
# === 本地 ===
python transcribe.py recordings/meeting.mp4 --output output/meeting_minutes.md

# === AWS SAM ===
export PATH="$LOCALAPPDATA/Programs/Python/Python312:$PATH"
sam build --profile 0362 --region ap-east-1
sam deploy --profile 0362 --region ap-east-1
sam validate --profile 0362 --region ap-east-1

# === CodeBuild（build processor container）===
aws codebuild start-build --project-name prod-voice-meeting-minutes-build --profile 0362 --region ap-east-1

# === Image Builder（build custom AMI）===
aws imagebuilder start-image-pipeline-execution --image-pipeline-arn <pipeline-arn> --profile 0362 --region ap-east-1

# === Cognito 用戶管理 ===
# 建立新用戶
aws cognito-idp admin-create-user --user-pool-id ap-east-1_6BilvzaAu \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true \
  --temporary-password "TempPass123!" --profile 0362 --region ap-east-1
# 設定永久密碼（跳過 FORCE_CHANGE_PASSWORD）
aws cognito-idp admin-set-user-password --user-pool-id ap-east-1_6BilvzaAu \
  --username user@example.com --password "FinalPass123!" --permanent \
  --profile 0362 --region ap-east-1
# 新用戶 email 要喺 0362 SES (ap-southeast-1) verify 先收到通知
```

## 檔案慣例

- 錄音放 `recordings/`（gitignored）
- 輸出放 `output/`（gitignored）
- `frontend/config.json` gitignored（含 API URL + Cognito config）
- `.pem` 檔案 gitignored
- `.aws-sam/` build artifacts gitignored
- `tests/` 測試錄音（gitignored）

## Tag 規範

所有 AWS resources 必須有以下 tags：

| Key | Value |
|---|---|
| Project | voice-meeting-minutes |
| ManagedBy | AWS SAM |
| Env | PROD |
| created-by | austin.leung@ecloudvalley.com |

## 命名規範

所有 resources 前綴 `prod-voice-meeting-minutes-{resource}`，用 kebab-case。
例外：Batch compute env / job queue / job definition 用 CF auto-generated names，
避免 update 時嘅 replacement 衝突。

## AWS Accounts

| Account | Profile | 用途 |
|---|---|---|
| 036293420772 | `0362` | 主 account：所有 infrastructure + SES |
| 507088713162 | `default` | DNS（Route 53，domain msphk.info） |

## S3 Lifecycle

| Rule | 內容 |
|---|---|
| Incomplete multipart upload | 1 日自動清除 |
| Job files（video/audio/results） | 14 日後自動刪除 |
| Orphan cleanup Lambda | 每日掃描 status=uploaded + >24hr 嘅 record，刪 S3 + DynamoDB |
