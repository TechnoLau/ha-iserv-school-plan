# Changelog

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
