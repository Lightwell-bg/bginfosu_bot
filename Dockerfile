FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml config.ini ./
COPY src ./src
RUN pip install . \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data && chown app:app /data

ENV DB_PATH=/data/app.db CONFIG_INI=/app/config.ini
USER app
VOLUME ["/data"]
