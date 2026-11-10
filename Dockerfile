FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_PATH=/app/config/alerts.json \
    STATE_PATH=/data/state.json

WORKDIR /app
COPY main.py models.py storage.py /app/
COPY config/alerts.json /app/config/alerts.json

RUN addgroup -S app && adduser -S app -G app && \
    mkdir /data && chown app:app /data

USER app
CMD ["python", "main.py"]
