FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# 离线环境构建时可改为内网 wheel 源：pip install --index-url http://your-mirror/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data/packages
VOLUME ["/data/packages"]

EXPOSE 8000

# 多进程 worker；求解为 CPU 型任务，worker 数建议与核数匹配
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
