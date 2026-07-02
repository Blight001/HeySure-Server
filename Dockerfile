FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app/main:/app

RUN sed -i \
        -e 's|http://deb.debian.org/debian-security|http://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
        -e 's|http://deb.debian.org/debian|http://mirrors.tuna.tsinghua.edu.cn/debian|g' \
        /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= http_proxy= https_proxy= all_proxy= \
        apt-get update -o Acquire::Retries=5 \
    && HTTP_PROXY= HTTPS_PROXY= ALL_PROXY= http_proxy= https_proxy= all_proxy= \
        apt-get install -y --no-install-recommends -o Acquire::Retries=5 git curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . .

# Single image, four runtimes. The compose file picks which entrypoint to
# run per service via ``command:`` so we don't carry separate Dockerfiles.
EXPOSE 3000 3001 3002

# Default keeps existing monolith behavior: serve api-gateway on :3000.
CMD ["uvicorn", "gateway.app:sio_app", "--host", "0.0.0.0", "--port", "3000"]
