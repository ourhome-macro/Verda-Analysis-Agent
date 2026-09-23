FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    VERDA_DB_PATH=/data/verda.db

WORKDIR /app

# 根目录 requirements.txt 是实际运行依赖；Uvicorn 仅由容器入口使用。
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --disable-pip-version-check -r /tmp/requirements.txt 'uvicorn[standard]==0.32.1'

COPY backend/app /app/app
COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN groupadd --gid 10001 verda \
    && useradd --uid 10001 --gid verda --no-create-home --shell /usr/sbin/nologin verda \
    && install -d -o verda -g verda /data \
    && chmod 0755 /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

# SQLite 和进程内 Trace 只运行一个 worker；横向扩容需要先改任务与存储架构。
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
