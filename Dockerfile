FROM python:3.11-slim

LABEL org.opencontainers.image.title="coldchain-audit" \
      org.opencontainers.image.description="冷链温度合规审计 API（报告封存 seal/verify + 批次人工复核 review）" \
      org.opencontainers.image.version="1.2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COLDCHAIN_DB_PATH=/data/coldchain.db

WORKDIR /app

# 先装依赖，利用镜像层缓存。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

COPY app ./app

USER appuser

EXPOSE 8000

# 容器启动即完成数据库初始化（lifespan 中幂等建表），无需外部账号。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
