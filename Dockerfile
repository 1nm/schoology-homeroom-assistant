FROM python:3.11-alpine

# Unbuffered so logs reach cron/journald as the run progresses, not at exit.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt

RUN apk update && \
    apk add --no-cache chromium chromium-chromedriver gcc g++ libc-dev make && \
    pip install --upgrade pip --no-cache-dir && \
    pip install -r /app/requirements.txt --no-cache-dir && \
    rm -f /app/requirements.txt

COPY app /app
RUN chmod +x /app/main.py

VOLUME /downloads
WORKDIR /downloads

ENTRYPOINT ["python3", "/app/main.py"]
