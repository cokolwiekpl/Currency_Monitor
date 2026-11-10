FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY main.py models.py storage.py /app/

RUN addgroup -S app && adduser -S app -G app && \
    mkdir /data && chown app:app /data

USER app
CMD ["python", "main.py"]
