# CLAUDE.md — voice-text-minutes 開發指引

## 項目簡介

將廣東話/中文會議錄音自動轉成結構化會議紀錄。
兩種模式：本地（transcribe.py + LM Studio）同 AWS（SAM + Batch + Bedrock）。

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
Frontend（CloudFront → S3）
  → API Gateway (ap-east-1) → Lambda: submit_job → DynamoDB + S3 presigned URL

S3 upload event (EventBridge) → Lambda: start_pipeline → Step Functions:
  ├─ Batch g4dn.xlarge Spot (ap-east-1)：
  │   ├─ Whisper STT（GPU）         — faster-whisper large-v3-turbo
  │   ├─ pyannote diarization（CPU） — 同 Whisper 並行
  │   └─ GOT-OCR2.0（GPU）          — Whisper 完再跑
  ├─ Lambda: merge_transcript
  ├─ Lambda: summarize              — 跨 region Bedrock DeepSeek V3.2（ap-northeast-1 Tokyo）
  └─ Lambda: notify                 — 跨 account SES email（ap-southeast-1，5070 account）
```

### Multi-Region 策略

| 服務 | Region | 原因 |
|---|---|---|
| S3, Batch, Lambda, Step Functions, DynamoDB, API GW, CodeCommit, CodeBuild, ECR, Image Builder | ap-east-1（HK） | 同事 upload 快，g4dn Spot 有容量 |
| Bedrock DeepSeek V3.2 | ap-northeast-1（Tokyo） | DeepSeek 喺 SG/HK 無，Tokyo 有 |
| SES（跨 account，5070） | ap-southeast-1（SG） | HK 無 SES；5070 account 已出 sandbox |
| CloudFront | Global | CDN |

跨 region call：
- Bedrock：`boto3.client('bedrock-runtime', region_name='ap-northeast-1')`
- SES：`boto3.client('sesv2', region_name='ap-southeast-1')` + `FromEmailAddressIdentityArn`

### 跨 Account SES（5070 → 0362）

SES 喺 account 507088713162（profile: default），domain `msphk.info`。
5070 嘅 SES identity 上面加咗 authorization policy 畀 account 036293420772 send email。
Lambda 用 SESv2 API，指定 `FromEmailAddressIdentityArn=arn:aws:ses:ap-southeast-1:507088713162:identity/msphk.info`。
Sender 地址：`minutes@msphk.info`。

### Custom AMI（EC2 Image Builder）

Base：ECS-optimized AL2023 GPU AMI（已有 NVIDIA drivers、ECS agent、Docker）。
Image Builder 加裝：Python 3.11、venv `/opt/processor/venv/`（torch+CUDA、faster-whisper、
pyannote、GOT-OCR2.0、transformers）。ML models 預下載到 `/opt/processor/models/`。
ffmpeg static binary 放 `/usr/local/bin/`。

Docker image 好細（~50MB，AL2023-minimal + libsndfile）。Python packages 同 models
從 host AMI 透過 Batch job definition volumes 掛入 container（`/opt/processor`、`/usr/local/bin`）。

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

### 點解用 g4dn.xlarge 唔用 g5？
g5.xlarge 喺 ap-east-1（HK）無 Spot 容量。g4dn.xlarge（T4 16GB）夠用，
因為只跑 Whisper（~2GB VRAM）+ GOT-OCR2.0（~3GB VRAM），唔使自己 host LLM。

### 點解用 GOT-OCR2.0 唔用 easyocr？
GOT-OCR2.0 中文 F1 ~98%，比 easyocr 好好多。
只有 580M params（~1.5GB），T4 上面同 Whisper 一齊跑完全冇問題。

### 點解用 Bedrock DeepSeek V3.2 唔自己 host Qwen？
DeepSeek V3.2（685B MoE，37B active）中文會議紀錄質素好好多。
164K context 大部分會議唔使 chunking。
唔使 GPU 做 summarization — Lambda call Bedrock API 搞掂。

### 點解唔用 Claude on Bedrock？
Account 限制：billing address / consolidated billing 經 partner，
Anthropic 唔畀用 Claude models on Bedrock。DeepSeek V3.2 係中文最佳替代。

### 點解用 custom AMI + 細 Docker 唔用大 Docker？
大 Docker image（~15GB）每次 Batch cold start 要 pull 幾分鐘，白燒 Spot 時間。
Custom AMI 預裝晒 packages + models。Docker image 只有 ~50MB，幾秒 pull 完。
Host 嘅 `/opt/processor` 透過 volume mount 掛入 container。

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
- **Bedrock**：ap-northeast-1（Tokyo）— DeepSeek V3.2
- **SES**：ap-southeast-1（SG）— 跨 account 5070（507088713162）
- **GPU**：g4dn.xlarge Spot（T4 16GB VRAM）
- **AMI**：EC2 Image Builder 自建（AL2023 ECS GPU + ML stack）
- **IaC**：AWS SAM（template.yaml）
- **CI/CD**：CodeBuild（build processor container → ECR）
- **Source**：CodeCommit（ap-east-1）+ GitHub（origin）
- **Profile**：`--profile 0362`（account 036293420772）

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

## 指令

```bash
# === 本地 ===
python transcribe.py recordings/meeting.mp4 --output output/meeting_minutes.md

# === AWS SAM ===
sam build --profile 0362 --region ap-east-1
sam deploy --profile 0362 --region ap-east-1
sam validate --profile 0362 --region ap-east-1

# === CodeBuild（build processor container）===
aws codebuild start-build --project-name prod-voice-meeting-minutes-build --profile 0362 --region ap-east-1

# === Image Builder（build custom AMI）===
aws imagebuilder start-image-pipeline-execution --image-pipeline-arn <pipeline-arn> --profile 0362 --region ap-east-1

# === 測試 API ===
API_KEY=$(aws apigateway get-api-keys --include-values --profile 0362 --region ap-east-1 --query 'items[0].value' --output text)
curl -X POST https://<api-id>.execute-api.ap-east-1.amazonaws.com/prod/jobs \
  -H "Content-Type: application/json" -H "x-api-key: $API_KEY" \
  -d '{"email":"you@company.com","output_language":"繁體中文","summary_length":"medium","output_format":"minutes"}'
```

## 檔案慣例

- 錄音放 `recordings/`（gitignored）
- 輸出放 `output/`（gitignored）
- `.pem` 檔案 gitignored
- `.aws-sam/` build artifacts gitignored

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
| 036293420772 | `0362` | 主 account：所有 infrastructure |
| 507088713162 | `default` | 只做 SES（跨 account，domain msphk.info） |
