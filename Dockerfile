FROM python:3.12-slim

# Byg-identifikation. Sæt med:
#   docker build --build-arg FAMILY_DASHBOARD_BUILD="$(git rev-parse --short HEAD)" ...
ARG FAMILY_DASHBOARD_BUILD=""

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FAMILY_DASHBOARD_HOST=0.0.0.0 \
    FAMILY_DASHBOARD_PORT=8080 \
    FAMILY_DASHBOARD_DATA_DIR=/data \
    FAMILY_DASHBOARD_STATIC_DIR=/app/web \
    FAMILY_DASHBOARD_BUILD=${FAMILY_DASHBOARD_BUILD}

WORKDIR /app
COPY requirements.txt pyproject.toml VERSION ./
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --upgrade pip && pip install -r requirements.txt
COPY app ./app
COPY web ./web
RUN useradd --system --create-home --uid 10001 dashboard && mkdir -p /data && chown -R dashboard:dashboard /app /data
USER dashboard
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"
ENTRYPOINT ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
