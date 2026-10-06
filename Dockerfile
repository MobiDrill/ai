# Build on an approved connected preparation machine; import the resulting image
# on the offline machine. Runtime never builds, pulls, or installs dependencies.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-impress python3 fonts-noto-cjk fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/worker.py /opt/converter/worker.py
COPY deploy/lo_process.py /opt/converter/lo_process.py
ENV HOME=/tmp TMPDIR=/tmp LANG=C.UTF-8
USER 65532:65532
EXPOSE 8001
ENTRYPOINT ["/usr/bin/python3", "/opt/converter/worker.py"]
