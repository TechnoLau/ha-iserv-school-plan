# Changelog

## 1.0.2

- Fix: Plan-Abruf endete nach erfolgreichem Login mit `HTTP 401`. Ursache war
  der `Accept`-Header: Enthielt er `application/json`, behandelt die neue
  IServ-Web-App die Anfrage als API-Aufruf ohne Token und antwortet mit 401 –
  auch bei gültiger Sitzung. Der Header wird jetzt je Ziel getrennt gesetzt
  (HTML, Kalender, JSON)
- Bei 401/403 auf Kalender-/JSON-Pfaden wird einmal mit Browser-`Accept`
  nachgefasst und das Format anschließend automatisch erkannt
- Fehlermeldung nennt jetzt Plan-URL, verwendeten `Accept`-Header,
  `WWW-Authenticate` und einen Auszug der Serverantwort
- Start-Log zeigt die aufgelöste Plan-URL; der Debug-Dump speichert sie mit

## 1.0.1

- Build-Fix: `build.yaml` entfernt (beim Supervisor deprecated); der Dockerfile
  nutzt jetzt direkt `python:3.12-alpine`, weil der Supervisor ohne
  `build_from` mit `ghcr.io/home-assistant/base` baut – dort fehlt `pip`
  (Fehler `pip: not found`, Exit-Code 127)

## 1.0.0

- Erste Version des Add-ons „IServ Stundenplan“
- Login an IServ (`/iserv/auth/login`) inkl. CSRF-Token-Erkennung
- Plan-Abruf mit automatischer Formaterkennung (HTML, JSON, ICS)
- MQTT Discovery für fünf Entities: Stundenbeginn, erste Stunde,
  nächster Schultag, Schulwecker (binary) und Status
- Feste Entity-IDs über `default_entity_id` (Home Assistant ab 2026.4) und
  `object_id` (ältere Versionen)
- Regeln: 12-Uhr-Grenze, Freitag/Samstag ohne Wecker, Sonntag → Montag,
  Überspringen von Wochenenden, Ferien/Feiertagen und Tagen ohne Unterricht
- Ferien-/Feiertagsliste und Stundenzeiten als Optionen
- Cache in `/data`, konfigurierbares Abrufintervall, nächtliche Ruhephase
- Optionaler Rohdaten-Dump zur Fehlersuche (`/data/debug`)
- MQTT-Zugangsdaten werden beim Mosquitto-Add-on automatisch vom Supervisor
  übernommen; TLS (`mqtt_ssl`) für externe Broker möglich
