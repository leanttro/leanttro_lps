FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
RUN useradd -m -u 1000 app && mkdir -p /data && chown -R app:app /data /app && chmod +x docker-entrypoint.sh
USER app

VOLUME ["/data"]
EXPOSE 8000
ENTRYPOINT ["./docker-entrypoint.sh"]
