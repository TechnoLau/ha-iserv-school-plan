# Dokumentation

Dieses Add-on liest den IServ-Stundenplan und stellt die erste tatsächlich
stattfindende Unterrichtsstunde sowie einen „Wecker morgen“ per MQTT Discovery
in Home Assistant bereit.

## Schnellstart

1. MQTT-Broker (Mosquitto-Add-on) und MQTT-Integration einrichten. Das Add-on
   nutzt `mqtt:want` und übernimmt Host, Port, TLS und Zugangsdaten automatisch
   vom Supervisor.
2. `iserv_url` auf die Schuladresse setzen, z. B. `https://ebg.kiel.eu` – mit
   oder ohne `/iserv` am Ende. Das ist die einzige Pflichtangabe: die
   Plantabelle sucht das Add-on selbst.
3. `bundesland` wählen (Standard: Schleswig-Holstein) – daraus werden Ferien und
   Feiertage automatisch geladen.
4. Benutzername und Passwort eintragen, falls der Plan nicht öffentlich ist.
5. Optional `period_times` (Stundenzeiten) ergänzen.
6. Add-on starten und im Status prüfen, ob der Abruf erfolgreich war
   (`verbunden`, `plan_status: ok`).

## Optionen

| Option | Standard | Bedeutung |
| --- | --- | --- |
| `iserv_url` | `https://schule.example.org` | Basisadresse der Schule (Pflicht) |
| `username` | *(leer)* | IServ-Benutzername |
| `password` | *(leer)* | IServ-Passwort |
| `bundesland` | `Schleswig-Holstein` | Ferien/Feiertage (alle 16 Länder) |
| `period_times` | 1–6 | `1=07:45-08:30;2=08:35-09:20;…` |
| `refresh_minutes` | `30` | Abrufintervall (5–720 Minuten) |
| `debug_dump` | `aus` | Rohantwort in `/data/debug` + Debug-Logging |

Plantabelle, Format, SSL-Prüfung, Log-Level und Gerätename werden automatisch
bestimmt. Optionen aus 1.0.x (`plan_url`, `plan_format`, `klasse`, `free_days`,
`lookahead_days`, `mqtt_*`, `verify_ssl`, `log_level`, `device_name`) werden
ignoriert; Details zur Migration stehen im `CHANGELOG.md`.

## Entities

- `sensor.stundenplan_status` – `verbinden`, `verbunden` oder
  `verbindung fehlgeschlagen` (Attribute u. a. `plan_status`, `bundesland`,
  `holidays_updated`, `last_error`, `skipped`, `warnings`)
- `sensor.erste_unterrichtsstunde` – Stundennummer; Attribute `start` (07:45),
  `start_iso`, `hours_until`, `target_date`, `subject`, `room`,
  `cancelled_periods`
- `sensor.naechster_schultag` – Datum des nächsten Schultags
- `binary_sensor.wecker_morgen` – `on` nur, wenn morgen ein Schultag mit
  stattfindender Stunde ist (Attribute `date`, `start`, `period`, `subject`,
  `room`, `reason`, `plan_status`)

Die IDs sind Vorschläge von MQTT Discovery und gelten beim ersten Anlegen einer
Entity; bereits umbenannte Entities behalten ihren Namen. Das Add-on sendet
sowohl `default_entity_id` (Home Assistant ab 2026.4) als auch `object_id`
(ältere Versionen).

## Ferien und Feiertage

Ferien und Feiertage kommen automatisch von openholidaysapi.org (kostenlos,
ohne API-Key) und werden sieben Tage in `/data/holidays.json` zwischen-
gespeichert. Ohne Internet gelten die gespeicherten Daten weiter (Hinweis im
Attribut `warnings`). Liefert IServ für einen Tag Stunden, haben diese Vorrang
vor dem Kalender.

## Regeln

- Mo–Do vor 12:00 Uhr: heutiger Tag, ab 12:00 Uhr: Folgetag
- Fr/Sa/So: der nächste Schultag (Montag) wird vorbereitet;
  `binary_sensor.wecker_morgen` ist am Wochenende `off`
- Wochenenden, Ferien/Feiertage und Tage ohne stattfindende Stunde werden
  übersprungen
- Ohne belastbare Daten wird kein Wecker gestellt (`plan_status: error`)

## Fehlersuche

- Add-on-Log prüfen; für mehr Details `debug_dump: true` setzen.
- Status `verbindung fehlgeschlagen`: `last_error` im Status-Attribut nennt den
  Grund (Login, Wartungsmodus, HTTP-Status).
- „Plan enthielt keine verwertbaren Daten“: Die Meldung nennt die gefundene
  Tabellen-/JSON-Struktur. Das Add-on wertet HTML (auch die Wochenansicht),
  einen verlinkten iCal-Export und die JSON-Schnittstelle der Stundenplan-
  ansicht aus. Sonst `debug_dump: true` setzen und `/data/debug/plan_raw.*`
  prüfen.
- „Wochengitter ohne Datum und ohne Wochenanker“: Die Seite nennt nur
  Wochentage und keinen Wochenanfang (Filter-JSON, `19.10. - 25.10.2026`,
  `KW 43`). Ohne Datum werden keine Stunden geraten.
- „IServ ist im Wartungsmodus“: IServ liefert HTTP 503; das Add-on versucht es
  automatisch erneut.
- Ferien fehlen: `bundesland` prüfen und `warnings`/`holidays_updated` im
  Status-Attribut kontrollieren (die API braucht Internet).
- Erscheinen keine Entities: MQTT-Verbindung und Discovery-Prefix
  (`homeassistant`) kontrollieren. Nach dem Update auf 1.1.0 wurden
  `sensor.stundenbeginn` und `binary_sensor.schulwecker_aktiv` entfernt.
- „MQTT-Verbindung abgelehnt: Not authorized“: Die Ports in der
  Mosquitto-Network-Karte sind nicht die Ursache. Mosquitto 7 nutzt den
  Dienst-Benutzer `addons`; das Add-on lädt die Zugangsdaten nach einem Fehler
  erneut beim Supervisor nach, zusätzlich hilft ein Neustart des
  Mosquitto-Add-ons.
