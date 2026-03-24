FROM public.ecr.aws/amazonlinux/amazonlinux:2023-minimal

# Slim container — Python packages + ML models live on host AMI
# and are volume-mounted into the container at /opt/processor
RUN dnf install -y libsndfile && dnf clean all

COPY entrypoint.py /app/entrypoint.py
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENTRYPOINT ["/app/start.sh"]
