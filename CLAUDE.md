# CLAUDE.md — Development Guide for voice-text-minutes

## Project Overview

This tool converts Cantonese/Chinese meeting recordings into structured meeting minutes.
Primary use case: Teams meeting recordings (mp4 with screen share) → Markdown meeting notes.

## Architecture

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

## Key Technical Decisions

### Why soundfile instead of torchaudio?
pyannote 4.x depends on torchcodec which requires FFmpeg DLLs. On Windows, this is unreliable.
We use `soundfile` to load WAV files and pass waveform tensors directly to pyannote, bypassing torchcodec.

### Why av instead of pydub?
Python 3.14 removed the `audioop` module. pydub depends on it and breaks.
`av` (PyAV) is already a dependency of faster-whisper, so no extra install needed.

### Why parent-child chunking?
Long meetings (1+ hours) produce transcripts of 1000+ lines (~280K tokens).
Local LLMs typically have 8K-131K context windows.
Parent-child chunking preserves global context while staying within token limits.
Chunk size is 400 lines (~66K tokens), tuned for 131K context window (Qwen 9B).

### Why DirectML for AMD GPU?
CUDA is NVIDIA-only. On Windows with AMD GPUs, onnxruntime-directml provides GPU acceleration
for faster-whisper's CTranslate2 backend. PyTorch (pyannote) still runs on CPU.

### Offline mode
`HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are set at module level.
All models must be pre-downloaded before first use. No internet calls at runtime.

## Environment

- **Python**: 3.14 (also works with 3.10+)
- **OS**: Windows 11 (primary), should work on Linux/macOS with minor adjustments
- **GPU**: AMD Radeon RX 6800 XT (16GB VRAM) via DirectML. No CUDA.
- **LLM**: LM Studio running locally (localhost:1234), currently using Qwen 3.5 9B
- **LM Studio context**: 131072 tokens

## Common Issues

### `torchcodec` warnings
Harmless. pyannote 4.x tries to load torchcodec on import and prints a long warning.
Does not affect functionality since we bypass it with soundfile.

### `I/O operation on closed file`
Caused by double-wrapping sys.stdout when importing transcribe.py from another script.
Fixed with `isinstance(sys.stdout, io.TextIOWrapper)` guard.

### Machine overheating
Running all 4 parallel tasks (whisper GPU + pyannote CPU + 2x easyocr CPU) can cause thermal throttling.
Consider reducing `max_workers` to 2-3 if this is an issue.

## Commands

```bash
# Run on a recording
python transcribe.py recordings/meeting.mp4 --output output/meeting_minutes.md

# Run tests (none yet)
# python -m pytest tests/

# Update dependencies
pip freeze > requirements.txt
```

## File Conventions

- Recordings go in `recordings/` (gitignored)
- Output files go in `output/` (gitignored)
- Only `transcribe.py`, `requirements.txt`, `README.md`, `CLAUDE.md`, `.gitignore` are tracked in git

## Future Improvements

- Add `--language` flag (currently hardcoded to "yue" for Cantonese)
- Add `--chunk-size` flag to tune parent-child chunking
- Reduce easyocr memory footprint (loads full model even for small regions)
- Consider switching to whisper.cpp with Vulkan for better AMD GPU support
- Add retry logic for LLM API calls
- Support Anthropic API / OpenAI API as alternative to local LLM
