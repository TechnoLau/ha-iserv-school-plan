# Dokumentation

Dieses Add-on liest den IServ-Stundenplan und stellt die erste tatsächlich
stattfindende Unterrichtsstunde per MQTT Discovery in Home Assistant bereit.

## Schnellstart

1. MQTT-Broker (Mosquitto-Add-on) und MQTT-Integration einrichten. Bei
   `mqtt_host: core-mosquitto` werden die Zugangsdaten automatisch beim
   Supervisor abgefragt.
2. `iserv_url` auf die Schuladresse setzen, z. B. `https://ebg.kiel.eu` –
   mit oder ohne `/iserv` am Ende, beides funktioniert. Das ist die einzige
   Pflichtangabe: die Plantabelle sucht das Add-on selbst.
3. `plan_url` **leer lassen** (Standard). Nur für Sonderfälle eintragen, z. B.
   den Kalender-Export (`/iserv/plan/export/…`) oder einen JSON-Endpunkt.
4. Benutzername und Passwort eintragen, falls der Plan nicht öffentlich ist.
5. Optional `free_days` (Ferien/Feiertage) und `period_times` (Stundenzeiten)
   ergänzen.
6. Add-on starten und im Log prüfen, ob der Abruf erfolgreich war.

## Entities

- `sensor.stundenbeginn` (Timestamp)
- `sensor.erste_unterrichtsstunde`
- `sensor.naechster_schultag` (Datum)
- `binary_sensor.schulwecker_aktiv`
- `sensor.stundenplan_status`

Die IDs sind Vorschläge von MQTT Discovery und gelten beim ersten Anlegen einer
Entity; bereits umbenannte Entities behalten ihren Namen. Das Add-on sendet
sowohl `default_entity_id` (Home Assistant ab 2026.4) als auch `object_id`
(ältere Versionen).

## Regeln

- Mo–Do vor 12:00 Uhr: heutiger Tag, ab 12:00 Uhr: Folgetag
- Fr/Sa: kein Wecker, nur Info zum nächsten Schultag
- So: Montag (bzw. nächster Schultag) vorbereiten
- Wochenenden, Ferien/Feiertage und Tage ohne stattfindende Stunde werden
  übersprungen
- Ohne belastbare Daten wird kein Wecker gestellt (`status: error`)

## Optionen

Die vollständige Beschreibung aller Optionen (inkl. Formate für `free_days` und
`period_times`) sowie Beispiele für Automationen stehen in der `README.md` des
Repositories.

## Fehlersuche

- Add-on-Log prüfen (`log_level: debug` für mehr Details).
- Bei „Plan enthielt keine verwertbaren Daten“: `debug_dump: true` setzen, Add-on
  neu starten und `/data/debug/plan_raw.*` prüfen. Meldet das Add-on „keine
  HTML-Tabelle in der Antwort“, zeigt `plan_url` auf die JavaScript-Web-App von
  IServ – ab Version 1.0.4 werden automatisch `/iserv/plan` und
  `/iserv/timetable` als klassische Tabelle probiert; das Log nennt die URL, die
  Daten geliefert hat. Ab Version 1.0.5 genügt `iserv_url`, `plan_url` bleibt
  leer.
- „IServ ist im Wartungsmodus“: IServ liefert HTTP 503 vom Wartungs-Webserver
  (Header `X-IServ-Maintenance`, Seite „Wartungsarbeiten“). Das Add-on versucht
  es automatisch erneut; keine Konfigurationsänderung nötig.
- Erscheinen keine Entities: MQTT-Verbindung, Discovery-Prefix und
  Broker-Zugangsdaten kontrollieren.
- „MQTT-Verbindung abgelehnt: Not authorized“: Die Ports in der
  Mosquitto-Network-Karte und der Discovery-Prefix sind nicht die Ursache.
  Mosquitto 7 nutzt den Dienst-Benutzer `addons` (`homeassistant` und `addons`
  sind reserviert) und akzeptiert Home-Assistant-Benutzer sowie eigene `logins`.
  Ab Version 1.0.3 lädt das Add-on die Zugangsdaten nach einem Fehler erneut
  beim Supervisor nach; zusätzlich hilft ein Neustart des Mosquitto-Add-ons.

