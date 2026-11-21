FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BROWSER_PATH=/usr/bin/chromium \
    CONTAINER_PORT=8000 \
    DATA_DIR=/data \
    SOURCE_ARCHIVE=/app/source.tar.gz

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium ffmpeg ca-certificates fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 app

WORKDIR /app
COPY pyproject.toml requirements.txt ./
# Cache third-party dependencies independently of parser edits.
RUN --mount=type=cache,target=/root/.cache/pip python -c \
    "import subprocess, sys, tomllib; p=tomllib.load(open('pyproject.toml','rb')); subprocess.check_call([sys.executable,'-m','pip','install','setuptools>=68','wheel',*p['project']['dependencies'],*p['project']['optional-dependencies']['api']])"
COPY yunx_downloader ./yunx_downloader
RUN pip install --no-cache-dir --no-deps --no-build-isolation .
COPY LICENSE NOTICE README.md README.en.md Dockerfile compose.yaml .env.example .dockerignore .gitignore ./
COPY docs ./docs
COPY vendor ./vendor
COPY tests ./tests
RUN tar -czf /app/source.tar.gz LICENSE NOTICE README.md README.en.md Dockerfile compose.yaml \
    .env.example .dockerignore .gitignore pyproject.toml requirements.txt yunx_downloader docs vendor tests \
    && mkdir -p /data && chown -R app:app /data /app

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from yunx_downloader.serve import check_health; check_health()"
CMD ["python", "-m", "yunx_downloader.serve"]
