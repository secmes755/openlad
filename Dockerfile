# syntax=docker/dockerfile:1
# OpenLAD single-container image.
#
# Design (0.4.0):
#   - The container runs ONLY the OpenLAD API. Model services (llama-server /
#     vLLM / Ollama, or a cloud endpoint) stay OUTSIDE the container and are
#     reached via OPENLAD_LLM_URL / OPENLAD_EMB_URL (OpenAI-compatible).
#   - The API is CPU-only; no GPU passthrough or nvidia-container-toolkit is
#     needed.
#   - Data lives in OPENLAD_DATA_DIR (default /app/data) -> mount a volume.

# ---------- builder: install full dependency set ----------
FROM python:3.11-slim AS builder

# PyPI mirror for builds outside the public internet (e.g. China): pass
# --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple/
ENV PIP_NO_CACHE_DIR=1
ENV PIP_INDEX_URL=$PIP_INDEX_URL

# System libs needed by opencv / pdf rendering at build time.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libgl1 libglib2.0-0 libgomp1 \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt && \
    find /usr/local/lib/python3.11/site-packages -type d -name "__pycache__" -prune -exec rm -rf {} \;

# ---------- runtime: slim image with deps + app ----------
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libgomp1 \
        poppler-utils \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

WORKDIR /app
COPY . /app
RUN mkdir -p /app/data && chmod +x /app/docker/entrypoint.sh

ENV OPENLAD_DATA_DIR=/app/data
EXPOSE 11296

# /api/v1/health always returns HTTP 200 even when a model endpoint is
# down (it reports "degraded" in the JSON body), so parse the status field.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import json,urllib.request;d=json.load(urllib.request.urlopen('http://127.0.0.1:11296/api/v1/health',timeout=5));assert d['status']=='ok',d" || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["uvicorn", "core.api.main:app", "--host", "0.0.0.0", "--port", "11296", "--log-level", "info"]
