FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal

# Slim container — Python packages + ML models live on host AMI
# and are volume-mounted into the container at /opt/processor.
# Must install python3.11 because venv symlinks resolve to /usr/bin/python3.11.
RUN dnf install -y python3.11 libsndfile && dnf clean all

COPY entrypoint.py /app/entrypoint.py

ENTRYPOINT ["/opt/processor/venv/bin/python3", "/app/entrypoint.py"]
