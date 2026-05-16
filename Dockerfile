FROM python:3.12-slim

# Don't buffer logs; surface them in `docker logs` immediately.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first so they get cached separately from source changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Run as non-root for safety. /data is mounted from the host (compose.yml).
RUN useradd -r -u 1001 -m -s /usr/sbin/nologin agent && \
    mkdir -p /data && chown -R agent:agent /data /app
USER agent

CMD ["python", "-m", "app.main"]
