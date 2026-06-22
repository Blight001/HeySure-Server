FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/python:3.11-slim

WORKDIR /app

ENV PYTHONPATH=/app/main:/app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY . .

# Single image, four runtimes. The compose file picks which entrypoint to
# run per service via ``command:`` so we don't carry separate Dockerfiles.
EXPOSE 3000 3001 3002

# Default keeps existing monolith behavior: serve api-gateway on :3000.
CMD ["uvicorn", "gateway.app:sio_app", "--host", "0.0.0.0", "--port", "3000"]
