FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8010 \
    WHITE_MARKET_DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py README.md ./
COPY static ./static

RUN useradd --create-home --uid 10001 white-market \
    && mkdir -p /app/data/backups /app/static/profiles /app/static/product_uploads /app/static/chat_uploads \
    && chown -R white-market:white-market /app

USER white-market
EXPOSE 8010

CMD ["python", "app.py"]
