FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal

# System deps
RUN dnf install -y python3.11 python3.11-pip libsndfile gcc && dnf clean all

# Python packages
RUN python3.11 -m pip install --no-cache-dir \
    torch==2.4.0 --index-url https://download.pytorch.org/whl/cu121 && \
    python3.11 -m pip install --no-cache-dir \
    faster-whisper==1.1.0 \
    pyannote.audio==3.3.2 \
    soundfile==0.12.1 \
    transformers==4.45.0 \
    tiktoken \
    boto3

COPY entrypoint.py /app/entrypoint.py

ENTRYPOINT ["python3.11", "/app/entrypoint.py"]
