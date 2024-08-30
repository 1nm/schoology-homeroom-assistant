FROM python:3.11-alpine

COPY requirements.txt /app/requirements.txt

RUN apk update && \
    apk add --no-cache chromium chromium-chromedriver gcc g++ libc-dev && \
    pip install --upgrade pip --no-cache-dir && \
    pip install -r /app/requirements.txt --no-cache-dir && \
    rm -f /app/requirements.txt

COPY app /app

VOLUME /downloads
WORKDIR /downloads

ENTRYPOINT ["python3", "/app/main.py"]
