FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Brussels \
    BUTLER_CONFIG=/config/config.yaml \
    BUTLER_DB=/data/butler.db

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd -r -u 1000 butler && mkdir -p /data && chown butler /data
USER butler

EXPOSE 8088
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8088/health')" || exit 1
# de gateway: aanmelden, en per gebruiker een eigen Secretaresse (app.main) op 127.0.0.1
CMD ["python", "-m", "app.gateway"]
