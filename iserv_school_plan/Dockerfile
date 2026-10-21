# Basis ist bewusst das offizielle Python-Image:
# Ein eigenes build.yaml ist beim Supervisor deprecated, und ohne build.yaml
# baut der Supervisor mit ghcr.io/home-assistant/base – dort ist kein pip
# enthalten ("/bin/ash: pip: not found"). python:3.12-alpine bringt Python und
# pip mit und ist fuer amd64, aarch64 und armv7 verfuegbar.
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin

LABEL org.opencontainers.image.title="IServ Stundenplan" \
      org.opencontainers.image.description="IServ timetable to Home Assistant via MQTT" \
      org.opencontainers.image.source="https://github.com/TechnoLau/ha-iserv-school-plan"

# tzdata liefert die Zeitzonendaten fuer zoneinfo (Europe/Berlin)
RUN apk add --no-cache tzdata ca-certificates

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

CMD ["python", "-m", "app.main"]
