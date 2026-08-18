FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "gunicorn --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-10000} app:app"]
