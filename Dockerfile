FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TEMP_DIR=/data/tmp

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY .env.example ./.env.example

RUN mkdir -p /data/tmp

CMD ["python", "-m", "bot.main"]