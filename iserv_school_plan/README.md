# IServ Stundenplan – Home-Assistant-Add-on

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FTechnoLau%2Fha-iserv-school-plan)
[![Tests](https://github.com/TechnoLau/ha-iserv-school-plan/actions/workflows/ci.yml/badge.svg)](https://github.com/TechnoLau/ha-iserv-school-plan/actions/workflows/ci.yml)

Liest den IServ-Stundenplan ein und stellt die **erste tatsächlich stattfindende
Unterrichtsstunde** als Home-Assistant-Entities bereit – gedacht als Grundlage für
einen Schulwecker (z. B. Wecker 45 Minuten vor Stundenbeginn, nur an Schultagen).

Die Entities werden per **MQTT Discovery** angelegt; es sind keine manuellen
YAML-Einträge in Home Assistant nötig.

## Funktionen

- Abruf des IServ-Stundenplans per HTTP(S) inklusive Login (Cookie-Session)
- Unterstützte Formate: **HTML-Tabelle**, **JSON** und **iCalendar (ICS)**,
  automatische Formaterkennung
- Regeln für den Schulwecker:
  - **Montag–Donnerstag:** vor 12:00 Uhr gilt der heutige Tag, ab 12:00 Uhr der Folgetag
  - **Freitag/Samstag:** kein Wecker (Wochenendregel), es wird nur der nächste Schultag angezeigt
  - **Sonntag:** der Montag (bzw. nächste Schultag) wird vorbereitet
  - Wochenenden, Ferien/Feiertage (konfigurierbar) und Tage ohne stattfindende Stunde
    werden übersprungen
  - Bei **fehlenden Daten** wird nie ein Wecker gestellt (`status: error`), damit
    kein falscher Alarm entsteht
- Entfallene Stunden werden erkannt („fällt aus“, „Entfall“, …) und bei der
  Ermittlung der ersten Stunde übersprungen
- Cache in `/data` (übersteht Neustarts), Neuberechnung alle 30 Sekunden,
  Plan-Abruf standardmäßig alle 30 Minuten; nachts wird deutlich seltener geladen
- Optionale Rohdaten-Ablage für die Fehlersuche (`/data/debug`)

## Installation

### Variante A: Lokales Add-on (ohne GitHub)

1. Ordner `iserv_school_plan` aus diesem Repository in das Add-on-Verzeichnis
   von Home Assistant kopieren, z. B. per Samba/SSH nach `/addons/iserv_school_plan`.
2. In Home Assistant: **Einstellungen → Add-ons → Add-on Store → ⋮ →
   Nach Updates suchen**. Das Add-on erscheint als „IServ Stundenplan“.
3. Add-on öffnen, **Konfiguration** ausfüllen (siehe unten), **Installieren**,
   danach **Starten**.

### Variante B: Als Add-on-Repository

1. Dieses Repository zu GitHub pushen (oder forken).
2. Die URLs in `repository.yaml` (Wurzelverzeichnis, eine Ebene über
   `iserv_school_plan/`) und `iserv_school_plan/config.yaml` zeigen auf
   `https://github.com/TechnoLau/ha-iserv-school-plan`. Bei einem Fork unter
   anderem Account- oder Repo-Namen diese beiden Stellen anpassen.
3. Home Assistant: **Einstellungen → Add-ons → Add-on Store → ⋮ → Repositories →
   Repository hinzufügen** und die Repository-URL eintragen.
4. Add-on installieren und starten.

### Voraussetzungen

- Ein laufender MQTT-Broker (z. B. das offizielle **Mosquitto**-Add-on).
  Die MQTT-Integration muss in Home Assistant eingerichtet sein, damit die
  Discovery-Nachrichten verarbeitet werden.
- Erreichbarkeit des IServ-Servers aus dem Heimnetz (bzw. VPN), falls der Plan
  nicht öffentlich erreichbar ist.

## Konfiguration

Für den Normalfall genügt **eine** Angabe: `iserv_url` (plus Benutzername und
Passwort). `plan_url` bleibt leer – das Add-on sucht die Plantabelle selbst.

| Option | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `iserv_url` | URL | `https://schule.example.org` | **Die einzige Pflichtangabe:** Basisadresse der Schule, z. B. `https://ebg.kiel.eu` – mit oder ohne `/iserv` am Ende. Auch die komplette Planseite (`…/iserv/timetable`, `…/iserv/plan`) wird akzeptiert; ab 1.0.6 wird der Modulpfad automatisch abgeschnitten |
| `plan_url` | Text | *(leer)* | Optional. Nur nötig für Sonderfälle (Kalender-Export/JSON-Endpunkt). Leer = automatische Suche (`/iserv/plan`, dann `/iserv/timetable`). Nackte Pfade wie `plan` werden unter `/iserv/` einsortiert |
| `username` | Text | *(leer)* | IServ-Benutzername (leer = ohne Login abrufen) |
| `password` | Passwort | *(leer)* | IServ-Passwort |
| `plan_format` | Auswahl | `auto` | `auto`, `html`, `json` oder `ics` |
| `klasse` | Text | *(leer)* | Nur Zeilen dieser Klasse berücksichtigen (z. B. `7a`), falls der Plan mehrere Klassen enthält |
| `free_days` | Text | *(leer)* | Ferien/Feiertage, siehe Format unten |
| `period_times` | Text | 1–6 | Stundenzeiten, siehe Format unten |
| `lookahead_days` | Zahl | `21` | Wie viele Tage vorausgeschaut wird |
| `refresh_minutes` | Zahl | `30` | Abrufintervall in Minuten (5–720) |
| `mqtt_host` | Text | `core-mosquitto` | Broker-Hostname (Mosquitto-Add-on) |
| `mqtt_port` | Port | `1883` | Broker-Port |
| `mqtt_ssl` | Schalter | `aus` | TLS für die Broker-Verbindung (Port dann meist `8883`) |
| `mqtt_username` | Text | *(leer)* | MQTT-Benutzer (optional) |
| `mqtt_password` | Passwort | *(leer)* | MQTT-Passwort (optional) |
| `mqtt_discovery_prefix` | Text | `homeassistant` | Discovery-Prefix der MQTT-Integration |
| `device_name` | Text | `IServ Stundenplan` | Anzeigename des Geräts in Home Assistant |
| `verify_ssl` | Schalter | `an` | TLS-Zertifikat prüfen (nur bei Problemen mit Schullizenzen deaktivieren) |
| `debug_dump` | Schalter | `aus` | Rohantwort unter `/data/debug/plan_raw.*` speichern |
| `log_level` | Auswahl | `info` | `debug`, `info`, `warning`, `error` |

Solange `mqtt_host` auf `core-mosquitto` (Standard) steht, holt sich das Add-on
die Broker-Zugangsdaten beim Supervisor (`mqtt:want`) – MQTT-Benutzer und
-passwort müssen dann nicht eingetragen werden. Für einen externen Broker
`mqtt_host`, `mqtt_port` (und ggf. `mqtt_ssl`) sowie Zugangsdaten selbst setzen.

### MQTT-Benutzer (Mosquitto-Add-on, Version 7)

- Add-ons erreichen den Broker über das interne Docker-Netz
  (`core-mosquitto:1883`). Die Ports in der **Network**-Karte des
  Mosquitto-Add-ons (1883/1884/8883/8884) gelten nur für Geräte **außerhalb**
  von Home Assistant – sie haben keinen Einfluss auf die Verbindung dieses
  Add-ons.
- Der Supervisor liefert die Zugangsdaten über `mqtt:provide`. Mosquitto 7
  erzeugt dafür den Dienst-Benutzer `addons`; `homeassistant` und `addons` sind
  reservierte Namen.
- Seit Mosquitto 7 hängt der Broker an der Home-Assistant-Benutzerverwaltung:
  MQTT-Clients können **Home-Assistant-Benutzer** verwenden (Einstellungen →
  Personen → Benutzer) oder eigene `logins` in der Mosquitto-Konfiguration.
- `mqtt_discovery_prefix` (`homeassistant`) betrifft nur die Topics und hat
  nichts mit der Anmeldung zu tun.

Meldet das Log `MQTT-Verbindung abgelehnt: Not authorized`, sind die
Zugangsdaten veraltet oder ungültig. Ab Version 1.0.3 fragt das Add-on sie nach
einem Fehlschlag erneut beim Supervisor ab (und nennt den verwendeten Benutzer).
Hilft das nicht: Mosquitto-Add-on neu starten (schreibt Benutzer/Passwort neu)
und – falls weiterhin nötig – einen eigenen Benutzer vergeben:

1. Mosquitto-Add-on → Konfiguration → `logins` ergänzen:
   ```yaml
   logins:
     - username: iserv
       password: geheim
   ```
2. Diese Werte in den Optionen des IServ-Add-ons als `mqtt_username` und
   `mqtt_password` eintragen.


### Ferien und Feiertage (`free_days`)

Ein Eintrag pro Zeitraum, getrennt durch `;`:

```text
2026-07-06..2026-08-14:Sommerferien;2026-10-03:Tag der Deutschen Einheit;2026-10-12..2026-10-24:Herbstferien
```

- `START..ENDE:Name` für Zeiträume, `DATUM:Name` für einzelne Tage
- Datum als `YYYY-MM-DD` oder `DD.MM.YYYY`
- Fehlt der Name, wird „schulfrei“ angezeigt

### Stundenzeiten (`period_times`)

```text
1=07:45-08:30;2=08:35-09:20;3=09:50-10:35;4=10:40-11:25;5=11:30-12:15;6=12:20-13:05
```

- Format: `STUNDE=START-ENDE`, getrennt durch `;`
- Die Endzeit ist optional (`7=13:15`)
- Diese Zeiten gelten nur für Stunden, bei denen die Quelle **keine** Uhrzeit
  liefert. Liefert IServ Zeiten (HTML-Spalte „Zeit“ oder ICS), haben diese Vorrang.
- Wichtig: Ohne Startzeit kann kein Wecker berechnet werden – der Status ist dann
  `error` und der Wecker bleibt aus.

## Entities

Alle Entities hängen am Gerät `device_name` (Standard: „IServ Stundenplan“).

| Entity | Typ | Bedeutung |
| --- | --- | --- |
| `sensor.stundenbeginn` | Timestamp | Beginn der ersten stattfindenden Stunde (ISO 8601 mit Zeitzone) |
| `sensor.erste_unterrichtsstunde` | Sensor | Stundennummer der ersten stattfindenden Stunde |
| `sensor.naechster_schultag` | Datum | Datum, auf das sich die Werte beziehen |
| `binary_sensor.schulwecker_aktiv` | Binary Sensor | `on`, wenn ein Schulwecker gestellt werden darf |
| `sensor.stundenplan_status` | Sensor | `ok`, `ferien`, `kein_unterricht`, `wochenende` oder `error` |

Die Entity-IDs sind Vorschläge von MQTT Discovery: Sie gelten beim ersten
Anlegen der Entity und bleiben danach erhalten, auch wenn du sie in Home
Assistant umbenennst. Das Add-on sendet dafür `default_entity_id` (ab Home
Assistant 2026.4) und zusätzlich `object_id` (ältere Versionen); unbekannte
Schlüssel ignoriert Home Assistant.

Zusatzinformationen als Attribute:

- **Stundenbeginn:** `period`, `subject`, `room`, `reason`
- **Erste Unterrichtsstunde:** `subject`, `room`, `planned_first_period`,
  `cancelled_periods`
- **Nächster Schultag:** `reason`, `status`
- **Schulwecker aktiv:** `reason`, `start`, `target_date`
- **Status:** `reason`, `fetched_at`, `age_minutes`, `skipped`, `warnings`,
  `last_error`

`skipped` listet die übersprungenen Tage mit Grund auf (Wochenende, Ferien,
kein Unterricht, keine Daten) – hilfreich, wenn der nächste Schultag
unerwartet weit in der Zukunft liegt.

## MQTT-Topics

| Topic | Inhalt |
| --- | --- |
| `homeassistant/sensor/iserv_stundenbeginn/config` | Discovery (analog für die übrigen Entities) |
| `iserv_school_plan/state` | Zustand als JSON (retained) |
| `iserv_school_plan/availability` | `online` / `offline` (retained, Last Will) |

Der Zustand wird nur bei Änderungen gesendet, bleibt aber retained – nach einem
Neustart von Home Assistant sind die Werte sofort wieder da. Die Altersangabe
`data_age_minutes` wird minütlich aktualisiert.

## Beispiel: Schulwecker-Automation

45 Minuten vor Stundenbeginn benachrichtigen, aber nur wenn der Wecker aktiv ist:

```yaml
automation:
  - alias: "Schulwecker klingeln"
    mode: single
    triggers:
      - trigger: template
        value_template: >-
          {% set start = states('sensor.stundenbeginn') %}
          {% if start in ['unknown', 'unavailable', 'none', ''] %}
            false
          {% else %}
            {{ now() >= as_datetime(start) - timedelta(minutes=45) }}
          {% endif %}
    conditions:
      - condition: state
        entity_id: binary_sensor.schulwecker_aktiv
        state: "on"
    actions:
      - action: notify.mobile_app_mein_handy
        data:
          title: "Schule"
          message: >-
            Erste Stunde: {{ state_attr('sensor.stundenbeginn', 'subject') }}
            um {{ as_timestamp(states('sensor.stundenbeginn')) | timestamp_custom('%H:%M', true) }}
            in Raum {{ state_attr('sensor.stundenbeginn', 'room') or '?' }}.
```

Der Template-Trigger wird nur einmal „wahr“, wenn der Zeitpunkt erreicht ist.

## Verhalten im Detail

- **Zeitzone:** fest `Europe/Berlin` (Sommer-/Winterzeit wird korrekt behandelt),
  unabhängig von der Zeitzone des Containers.
- **12-Uhr-Regel:** Der Wecker wird morgens für den aktuellen Tag gestellt; ab
  12:00 Uhr für den nächsten Schultag. So kann am Nachmittag schon der nächste
  Morgen geprüft werden, ohne einen bereits vergangenen Termin zu melden.
- **Freitag/Samstag:** Es wird kein Wecker gestellt (`alarm: false`), auch wenn
  Daten für Montag vorliegen. `sensor.naechster_schultag` zeigt trotzdem den
  nächsten bekannten Schultag.
- **Sonntag:** Es wird der Montag (bzw. der nächste Schultag) vorbereitet – der
  Wecker ist aktiv, sofern Unterrichtsdaten vorliegen.
- **Keine Daten:** Liegt für einen Tag kein Eintrag vor (z. B. weil IServ den Plan
  noch nicht veröffentlicht hat), wird nicht geraten: Status `error`,
  `alarm: false`, `reason` nennt den Grund.
- **Planabdeckung:** Feiertage außerhalb der Datenabdeckung erkennt das Add-on
  nur über `free_days`.
- **Abrufe:** Start des Add-ons, danach alle `refresh_minutes`. Nach einem Fehler
  wird frühestens nach 5 Minuten erneut versucht; nachts (22–5 Uhr) wird nur
  geladen, wenn die Daten älter als 6 Stunden sind. Der Zustand wird alle
  30 Sekunden neu berechnet (12-Uhr-Regel, Tageswechsel).
- **Cache:** Zuletzt erfolgreich geladene Daten liegen in `/data/cache.json` und
  werden nach einem Neustart sofort verwendet, bis der erste Abruf gelingt.

## Fehlersuche

| Symptom | Ursache / Lösung |
| --- | --- |
| Status `error`, `last_error: iserv_url muss …` | `iserv_url` auf die eigene Schuladresse setzen – mehr ist nicht nötig (`plan_url` darf leer bleiben) |
| `Login fehlgeschlagen …` | Benutzername/Passwort prüfen; Schul-URL prüfen (`/iserv/auth/login`). Ab 1.0.4 steht der IServ-Hinweis dabei (z. B. „Bitte warten Sie … Sekunden“ = Brute-Force-Schutz nach mehreren Starts) |
| `IServ ist im Wartungsmodus …` | IServ ist selbst offline (HTTP 503 mit `X-IServ-Maintenance`, Seite „Wartungsarbeiten“). Ab 1.0.5 erkennt das Add-on das und versucht es beim nächsten Intervall erneut – nichts umkonfigurieren |
| `Zugriff auf den Plan verweigert (HTTP 403)` | Konto darf den Plan nicht sehen, oder die URL ist falsch |
| `Zugriff auf den Plan verweigert (HTTP 401)` **nach** erfolgreichem Login | Ab Version 1.0.2 behoben: Ursache war `application/json` im `Accept`-Header (IServ wertet das als API-Aufruf). Meldung nennt URL, `Accept` und Serverantwort – damit lässt sich prüfen, ob der Pfad ein API-/Export-Endpunkt ist, der ein Token braucht |
| `Plan-Abruf fehlgeschlagen (HTTP 404)` | Einer der Plan-Pfade existiert auf dieser IServ-Instanz nicht (typisch: Tabelle liegt unter `/iserv/timetable`, `/iserv/plan` ist abgeschaltet – oder umgekehrt). Ab 1.0.7 probiert das Add-on alle bekannten Pfade durch und nennt in `last_error` jede versuchte URL mit Status (`[Versuche: …/iserv/plan: HTTP 404; …/iserv/timetable: HTTP 200]`). Stehen dort ausschließlich 404er, ist das Modul für dieses Konto nicht freigeschaltet oder `iserv_url` zeigt auf den falschen Host |
| `Plan enthielt keine verwertbaren Daten` | Falsches Format/URL. Zeigt `plan_url` auf die JavaScript-Web-App (`/iserv/timetable`), probiert das Add-on ab 1.0.4 automatisch `/iserv/plan` und nennt im Log die URL, die Daten geliefert hat. Die Meldung enthält eine Kurzfassung der Antwort (Länge, `<title>`, `<table>`, erste Skript-URLs). Sonst `debug_dump: true` aktivieren und `/data/debug/plan_raw.*` ansehen (per SSH/Samba) |
| `Login erfolgreich, aber die IServ-Sitzung wurde nicht übernommen` | IServ (neue Web-Oberfläche) leitete nach dem Login per Meta-Refresh/JavaScript weiter; ab 1.0.6 führt das Add-on diese Weiterleitung selbst aus. Bleibt die Meldung, `log_level: debug` setzen – dort steht das gefundene Weiterleitungsziel. Hilft das nicht, ist meist eine **2FA-Pflicht** oder ein Sonderlogin der Schule die Ursache |
| `MQTT-Verbindung abgelehnt: Not authorized` | Broker-Zugangsdaten ungültig/veraltet. Mosquitto-Add-on neu starten (ab 1.0.3 holt das Add-on die Zugangsdaten selbst neu), sonst eigenes `logins`-Konto im Mosquitto-Add-on anlegen und als `mqtt_username`/`mqtt_password` eintragen. Die Ports in der Network-Karte und `mqtt_discovery_prefix` sind dabei nicht die Ursache |
| Entities erscheinen nicht | MQTT-Integration und Broker prüfen; `mqtt_host`/Zugangsdaten kontrollieren; Add-on-Log ansehen |
| Zeiten wirken verschoben | Prüfen, ob IServ lokale Zeiten liefert; `period_times` ist nur ein Fallback |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Schul-Zertifikat ist im Container nicht vertrauenswürdig; `verify_ssl: false` (nur wenn vertretbar) |
| Wecker bleibt aus, obwohl Unterricht ist | `sensor.stundenplan_status` und `last_error` prüfen; ggf. Startzeiten über `period_times` ergänzen |

Log des Add-ons: **Einstellungen → Add-ons → IServ Stundenplan → Log**. Für mehr
Details `log_level: debug` setzen.

## Sicherheit

- Das IServ-Passwort liegt in der Add-on-Konfiguration (`/data/options.json` im
  Container) im Klartext. Wenn möglich, ein eigenes Konto bzw. separates Passwort
  verwenden.
- Zugangsdaten niemals in Git einchecken.
- `debug_dump` kann personenbezogene Daten (Klassen, Räume, Lehrkräfte) enthalten
  – nur temporär aktivieren.
- `verify_ssl: false` schwächt die Transportsicherheit und sollte Ausnahme bleiben.

## Entwicklung

```text
iserv_school_plan/
├── app/
│   ├── main.py      # Schleife, Cache, Statusaufbau
│   ├── config.py    # Add-on-Optionen
│   ├── iserv.py     # Login + HTTP-Abruf
│   ├── parsers.py   # HTML/JSON/ICS-Parser
│   ├── plan.py      # Datenmodell + Auswahlregeln
│   └── mqtt.py      # Discovery + Publish
├── tests/           # pytest-Suite (ohne Netzwerk, mit lokalem HTTP-Testserver)
├── config.yaml      # Add-on-Metadaten und Optionsschema
├── DOCS.md          # Kurzanleitung für die Add-on-Seite
└── Dockerfile       # python:3.12-alpine + Abhängigkeiten
```

Eine Ebene darüber liegt `repository.yaml` (Metadaten des Add-on-Repositories).
Für die lokale Installation (Variante A) wird nur der Ordner
`iserv_school_plan/` benötigt.

Tests lokal ausführen (Python ≥ 3.11, `pytest` und `paho-mqtt` installiert):

```powershell
cd iserv_school_plan
python -m pytest -q
```

Der Login-Ablauf wird in `tests/test_iserv.py` gegen einen lokalen HTTP-Server
getestet; die Parser-Tests arbeiten mit anonymisierten Ausschnitten.

### Anpassung an die eigene IServ-Installation

Da IServ je nach Version und Schulkonfiguration unterschiedliche Export-Formate
anbietet, ist `plan_url` frei konfigurierbar (bleibt sie leer, probiert das
Add-on `/iserv/plan` und `/iserv/timetable`). Die Parser sind tolerant gegenüber
Spaltennamen („Fach“, „Raum“, „Zeit“, „Art“, „Klasse“, …) und Uhrzeitformaten
(`07:45 - 08:30`, `7:45`, `745`). Bei einem unbekannten Format:

1. `debug_dump: true` setzen und das Add-on neu starten.
2. `/data/debug/plan_raw.*` und `plan_raw.meta.json` prüfen.
3. Bei Bedarf in `app/parsers.py` einen weiteren Parser ergänzen, in
   `detect_format`/`parse_payload` registrieren und in `tests/test_parsers.py`
   testen.


