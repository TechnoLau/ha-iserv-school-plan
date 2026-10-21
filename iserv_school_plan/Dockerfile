ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin

# tzdata liefert die Zeitzonendaten fuer zoneinfo (Europe/Berlin)
RUN apk add --no-cache tzdata ca-certificates

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

CMD ["python", "-m", "app.main"]
