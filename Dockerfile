FROM python:3.11-alpine

# Unbuffered so logs reach cron/journald as the run progresses, not at exit.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/requirements.txt

# The compiler is only needed to build wheels that musl has none of (PyMuPDF).
# Installing it as a virtual package and deleting it in the same layer keeps
# ~180MB of gcc, g++, binutils and musl-dev out of the finished image.
RUN apk add --no-cache chromium chromium-chromedriver && \
    apk add --no-cache --virtual .build-deps gcc g++ libc-dev make && \
    pip install --upgrade pip --no-cache-dir && \
    pip install -r /app/requirements.txt --no-cache-dir && \
    apk del .build-deps && \
    find /usr/local/lib/python3.11 -name '__pycache__' -type d -prune -exec rm -rf {} + && \
    rm -f /app/requirements.txt

COPY app /app
RUN chmod +x /app/main.py

VOLUME /downloads
WORKDIR /downloads

ENTRYPOINT ["python3", "/app/main.py"]
