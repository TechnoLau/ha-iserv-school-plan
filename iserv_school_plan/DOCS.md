# Dokumentation

Dieses Add-on liest den IServ-Stundenplan und stellt die erste tatsächlich
stattfindende Unterrichtsstunde per MQTT Discovery in Home Assistant bereit.

## Schnellstart

1. MQTT-Broker (Mosquitto-Add-on) und MQTT-Integration einrichten. Bei
   `mqtt_host: core-mosquitto` werden die Zugangsdaten automatisch beim
   Supervisor abgefragt.
2. `iserv_url` auf die Schuladresse setzen, z. B. `https://ebg.kiel.eu`.
3. `plan_url` auf den Stundenplan/Export setzen, z. B. `/iserv/plan`.
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
  neu starten und `/data/debug/plan_raw.*` prüfen.
- Erscheinen keine Entities: MQTT-Verbindung, Discovery-Prefix und
  Broker-Zugangsdaten kontrollieren.
