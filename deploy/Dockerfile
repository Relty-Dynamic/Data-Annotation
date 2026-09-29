FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONNOUSERSITE=1 \
    PIP_REQUIRE_VIRTUALENV=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv ffmpeg \
    && python3 -c "import sys; assert sys.version_info >= (3, 13), sys.version" \
    && ffmpeg -hide_banner -h filter=scale 2>&1 | grep -q reset_sar \
    && python3 -m venv /opt/venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.lock.txt /app/requirements.lock.txt
RUN timeout --signal=TERM --kill-after=10s 600s /opt/venv/bin/python -m pip install \
    --no-cache-dir --timeout 30 --retries 2 --progress-bar off -r /app/requirements.lock.txt
COPY backend /app/backend
COPY deploy/healthcheck.py /app/deploy/healthcheck.py
RUN chmod -R a+rX /app
USER 1000:1000
CMD ["/opt/venv/bin/python", "-m", "uvicorn", "backend.remote_worker:create_worker", "--factory", "--host", "0.0.0.0", "--port", "18120"]
