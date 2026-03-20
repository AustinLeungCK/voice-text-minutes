# CLAUDE.md — Development Guide for voice-text-minutes

## Project Overview

This tool converts Cantonese/Chinese meeting recordings into structured meeting minutes.
Two modes: local (transcribe.py + LM Studio) and AWS (SAM + Batch + Bedrock).

## Architecture

### Local Mode (transcribe.py)

```
transcribe.py (single file, ~450 lines)
├── convert_to_wav()              — Audio extraction (av library)
├── extract_participant_names()   — OCR names from Teams UI (easyocr)
├── extract_slides()              — OCR slide content from video (easyocr)
├── transcribe_audio()            — Speech-to-text (faster-whisper, large-v3-turbo)
├── diarize_audio()               — Speaker diarization (pyannote-audio 4.x)
├── merge_transcript()            — Combine whisper segments + speaker labels
├── generate_minutes()            — LLM summarization (LM Studio, parent-child chunking)
│   ├── _llm_call()               — Single LLM API call wrapper
│   ├── Step 1: Parent outline
│   ├── Step 2: Child chunk summaries
│   └── Step 3: Final merge
└── main()                        — CLI entry point, orchestrates parallel execution
```

### AWS Mode (SAM)

```
Frontend (CloudFront → S3)
  → API Gateway (ap-east-1) → Lambda: submit_job → DynamoDB + S3 presigned URL

S3 upload event → Lambda: start_pipeline → Step Functions:
  ├─ Batch g4dn.xlarge Spot (ap-east-1):
  │   ├─ Whisper STT (GPU)              — faster-whisper large-v3-turbo
  │   ├─ pyannote diarization (CPU)     — parallel with Whisper
  │   └─ GOT-OCR2.0 (GPU)              — after Whisper completes
  ├─ Lambda: merge_transcript
  ├─ Lambda: summarize                  — cross-region Bedrock DeepSeek V3.2 (ap-southeast-1)
  └─ Lambda: notify                     — cross-region SES email (ap-southeast-1)
```

### Multi-Region Strategy

| Service | Region | Why |
|---|---|---|
| S3, Batch, Lambda, Step Functions, DynamoDB, API GW, CodeCommit, CodeBuild, ECR | ap-east-1 (HK) | Upload fast for team, g4dn.xlarge Spot available |
| Bedrock DeepSeek V3.2, SES | ap-southeast-1 (SG) | HK has no SES; DeepSeek available in SG |
| CloudFront | Global | CDN |

Cross-region calls via `boto3.client('bedrock-runtime', region_name='ap-southeast-1')`.

## Key Technical Decisions

### Why soundfile instead of torchaudio?
pyannote 4.x depends on torchcodec which requires FFmpeg DLLs. On Windows, this is unreliable.
We use `soundfile` to load WAV files and pass waveform tensors directly to pyannote, bypassing torchcodec.

### Why av instead of pydub?
Python 3.14 removed the `audioop` module. pydub depends on it and breaks.
`av` (PyAV) is already a dependency of faster-whisper, so no extra install needed.

### Why parent-child chunking?
Long meetings (1+ hours) produce transcripts of 1000+ lines (~280K tokens).
AWS mode: DeepSeek V3.2 has 164K context, so most meetings don't need chunking.
Local mode: Qwen 9B has 131K context, chunk size is 400 lines (~66K tokens).

### Why g4dn.xlarge (not g5)?
g5.xlarge has no Spot capacity in ap-east-1 (HK). g4dn.xlarge (T4 16GB) is sufficient
since we only run Whisper (~2GB VRAM) + GOT-OCR2.0 (~3GB VRAM). No self-hosted LLM on GPU.

### Why GOT-OCR2.0 (not easyocr)?
GOT-OCR2.0 has F1 ~98% on Chinese text, significantly better than easyocr.
Only 580M params (~1.5GB), fits easily on T4 alongside Whisper.

### Why Bedrock DeepSeek V3.2 (not self-hosted Qwen)?
DeepSeek V3.2 (685B MoE, 37B active) produces much better Chinese meeting minutes.
164K context eliminates chunking for most meetings.
No need for GPU for summarization — runs as Lambda calling Bedrock API.

### Why not Claude on Bedrock?
Account restriction: billing address / consolidated billing through partner prevents
access to Anthropic models on Bedrock. DeepSeek V3.2 is the best alternative for Chinese.

### Offline mode (local only)
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are set at module level.
All models must be pre-downloaded before first use. No internet calls at runtime.

## Environment

### Local
- **Python**: 3.14 (also works with 3.10+)
- **OS**: Windows 11 (primary)
- **GPU**: AMD Radeon RX 6800 XT (16GB VRAM) via DirectML
- **LLM**: LM Studio running locally (localhost:1234), currently using Qwen 3.5 9B

### AWS
- **Region**: ap-east-1 (HK) primary, ap-southeast-1 (SG) for Bedrock + SES
- **GPU**: g4dn.xlarge Spot (T4 16GB VRAM)
- **IaC**: AWS SAM (template.yaml)
- **CI/CD**: CodeBuild (builds processor container → ECR)
- **Profile**: `--profile 0362`

## Common Issues

### `torchcodec` warnings
Harmless. pyannote 4.x tries to load torchcodec on import and prints a long warning.
Does not affect functionality since we bypass it with soundfile.

### `I/O operation on closed file`
Caused by double-wrapping sys.stdout when importing transcribe.py from another script.
Fixed with `isinstance(sys.stdout, io.TextIOWrapper)` guard.

### Batch compute environment name changes
CloudFormation cannot update a custom-named Batch compute environment that requires replacing.
Batch compute env names are auto-generated by CF to avoid this issue.

## Commands

```bash
# === Local ===
python transcribe.py recordings/meeting.mp4 --output output/meeting_minutes.md

# === AWS SAM ===
sam build --profile 0362 --region ap-east-1
sam deploy --profile 0362 --region ap-east-1
sam validate --profile 0362 --region ap-east-1

# === CodeBuild (build processor container) ===
aws codebuild start-build --project-name prod-voice-meeting-minutes-build --profile 0362 --region ap-east-1
```

## File Conventions

- Recordings go in `recordings/` (gitignored)
- Output files go in `output/` (gitignored)
- `.pem` files gitignored
- `.aws-sam/` build artifacts gitignored

## Tagging Convention

All AWS resources must have these tags:

| Key | Value |
|---|---|
| Project | voice-meeting-minutes |
| ManagedBy | AWS SAM |
| Env | PROD |
| created-by | austin.leung@ecloudvalley.com |

## Naming Convention

All resources prefixed with `prod-voice-meeting-minutes-{resource}`. Kebab-case.
Exception: Batch compute env / job queue / job definition use CF auto-generated names
to avoid replacement conflicts on updates.
