FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal

# Only libsndfile needed — Python, torch, ML packages, ffmpeg all come from host AMI via volume mount
RUN dnf install -y libsndfile && dnf clean all

COPY entrypoint.py /app/entrypoint.py

# Python comes from the mounted venv at /opt/processor/venv/bin/python3
ENTRYPOINT ["/opt/processor/venv/bin/python3", "/app/entrypoint.py"]
