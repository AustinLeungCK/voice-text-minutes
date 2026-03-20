FROM nvidia/cuda:12.1.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 python3.11-venv python3-pip \
    ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf /usr/bin/python3.11 /usr/bin/python3

WORKDIR /app

# PyTorch with CUDA 12.1 (must be separate to use --index-url)
RUN pip3 install --no-cache-dir torch==2.4.0 --index-url https://download.pytorch.org/whl/cu121

# Python dependencies
RUN pip3 install --no-cache-dir \
    faster-whisper==1.1.0 \
    pyannote.audio==3.3.2 \
    soundfile==0.12.1 \
    transformers==4.45.0 \
    tiktoken \
    boto3

# Download and bake in models at build time
# faster-whisper large-v3-turbo
RUN python3 -c "from faster_whisper import WhisperModel; WhisperModel('large-v3-turbo', device='cpu', compute_type='int8')"

# GOT-OCR2.0
RUN pip3 install --no-cache-dir got-ocr2==0.1.0 || true
RUN python3 -c "from transformers import AutoModel, AutoTokenizer; \
    AutoTokenizer.from_pretrained('stepfun-ai/GOT-OCR-2.0-hf', trust_remote_code=True); \
    AutoModel.from_pretrained('stepfun-ai/GOT-OCR-2.0-hf', trust_remote_code=True)" || true

# pyannote models require HF_TOKEN at build time
ARG HF_TOKEN
RUN if [ -n "$HF_TOKEN" ]; then \
    python3 -c "from pyannote.audio import Pipeline; \
    Pipeline.from_pretrained('pyannote/speaker-diarization-3.1', use_auth_token='${HF_TOKEN}')"; \
    fi

COPY entrypoint.py /app/entrypoint.py

ENTRYPOINT ["python3", "/app/entrypoint.py"]
