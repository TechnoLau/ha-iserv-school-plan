# Changelog

## 1.0.4

- Fix: `iserv_url` darf jetzt mit und ohne `/iserv` eingetragen werden. Bisher
  entstand daraus beim Login ein doppelter Pfad (`/iserv/iserv/auth/login`), der
  je nach Server zu „Login fehlgeschlagen – IServ zeigt erneut die Anmeldeseite“
  führte
- Neu: Liefert `plan_url` keine Tabelle (typisch für die neue JavaScript-Web-App
  unter `/iserv/timetable`), probiert das Add-on automatisch `/iserv/plan` und
  `/iserv/timetable` als klassische Plantabelle und nutzt die erste Antwort mit
  Daten. Im Log steht, welche URL funktioniert hat
- Login-Fehler nennen jetzt den Grund von IServ: Brute-Force-Wartezeit
  (`Bitte warten Sie … Sekunden`) oder den Hinweistext der Anmeldeseite
- Fehlermeldung „keine verwertbaren Daten“ enthält eine Kurzfassung der Antwort
  (Länge, `<title>`, vorhandene `<table>`, erste Skript-URLs) – damit lässt sich
  die neue IServ-Web-App im Log erkennen, ohne Debug-Dump zu kopieren
- Antworten, die inhaltlich die Anmeldeseite sind, werden auch ohne
  `/auth/login` in der URL als Sitzungsablauf erkannt
- `debug_dump: true` speichert jetzt auch fehlgeschlagene Antworten unter
  `/data/debug/plan_raw.*`

## 1.0.3

- Diagnose: Bei `MQTT-Verbindung abgelehnt` steht jetzt der verwendete
  MQTT-Benutzer im Log, dazu ein Hinweis auf die gültigen Zugangsdaten
  (Mosquitto 7 akzeptiert Home-Assistant-Benutzer und die `logins` im
  Mosquitto-Add-on; `homeassistant` und `addons` sind reserviert)
- Fix: Nach einem MQTT-Auth-Fehler werden die Zugangsdaten erneut beim
  Supervisor abgefragt und für den nächsten Reconnect übernommen. Bisher wurde
  `/services/mqtt` nur beim Start gelesen – nach einem Neustart des
  Mosquitto-Add-ons konnte das Add-on deshalb dauerhaft mit veralteten
  Zugangsdaten („Not authorized“) hängen
- MQTT-Fehler werden nicht mehr endlos wiederholt geloggt (max. 3×, danach
  `debug`), inkl. einmaligem Hinweis auf die Optionen
- Start-Log nennt den MQTT-Benutzer
- Klarere Meldung, wenn die Plan-URL auf die JavaScript-Web-App von IServ zeigt
  (keine HTML-Tabelle in der Antwort) – dann die klassische Plantabelle
  (`/iserv/plan`) verwenden

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
