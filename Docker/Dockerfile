# syntax=docker/dockerfile:1.6
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        curl \
        ca-certificates \
        tzdata \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖以利用层缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install -r /app/requirements.txt

ARG INSTALL_PLAYWRIGHT=true
RUN if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
        playwright install --with-deps chromium || echo "playwright install skipped"; \
    fi

COPY . /app

RUN mkdir -p /app/Logs /app/SessionDB /app/Knowledge/chunks /app/Knowledge/origin_file

RUN mkdir -p /app/.venv/bin && ln -sf "$(command -v python)" /app/.venv/bin/python

EXPOSE 8000

CMD ["uvicorn", "agent_api:app", "--host", "0.0.0.0", "--port", "8000"]
