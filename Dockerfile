FROM python:3.12.13-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --requirement /tmp/requirements.txt


FROM python:3.12.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH" \
    PROFIT_AGENT_HOST=0.0.0.0 \
    PROFIT_AGENT_PORT=8011 \
    PROFIT_AGENT_WORKERS=1 \
    PROFIT_AGENT_LOG_LEVEL=INFO

RUN groupadd --gid 10001 profit \
    && useradd --uid 10001 --gid profit --no-create-home --shell /usr/sbin/nologin profit

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=profit:profit app ./app

USER 10001:10001

EXPOSE 8011

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('PROFIT_AGENT_PORT', '8011')}/health\", timeout=3).read()"]

STOPSIGNAL SIGTERM

CMD ["python", "-m", "app.runner"]