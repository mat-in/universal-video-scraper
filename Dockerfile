FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System deps: ffmpeg for HLS/DASH joining + Playwright browser deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        libnss3 \
        libnspr4 \
        libdbus-1-3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libatspi2.0-0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY telegram_bot/requirements.txt /tmp/tg-reqs.txt
COPY pyproject.toml /app/pyproject.toml
COPY video_scraper /app/video_scraper
COPY telegram_bot /app/telegram_bot

RUN pip install --no-cache-dir /app --extra-index-url https://pypi.org/simple \
    && pip install --no-cache-dir -r /tmp/tg-reqs.txt \
    && python -m playwright install --with-deps chromium

ENV JARVIS_BOT_DOWNLOAD_DIR=/data/downloads \
    JARVIS_BOT_CONCURRENCY=1

RUN mkdir -p /data/downloads

VOLUME ["/data"]

CMD ["python", "-m", "telegram_bot.bot"]