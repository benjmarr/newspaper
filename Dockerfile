# Personal daily newspaper: one small container, Python + SQLite.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    TZ=Australia/Melbourne

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Unprivileged user. /data is created here owned by that user so a fresh named volume inherits it.
RUN groupadd --system --gid 10001 paper \
 && useradd --system --uid 10001 --gid paper --no-create-home --shell /usr/sbin/nologin paper \
 && mkdir -p /data/backups && chown -R paper:paper /data

COPY app ./app

USER paper
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# One worker on purpose: the scheduler lives inside the app process.
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--no-server-header"]
