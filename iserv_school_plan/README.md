# IServ Stundenplan – Home-Assistant-Add-on

[![Open your Home Assistant instance and show the add add-on repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FTechnoLau%2Fha-iserv-school-plan)
[![Tests](https://github.com/TechnoLau/ha-iserv-school-plan/actions/workflows/ci.yml/badge.svg)](https://github.com/TechnoLau/ha-iserv-school-plan/actions/workflows/ci.yml)

Liest den IServ-Stundenplan ein und stellt die **erste tatsächlich stattfindende
Unterrichtsstunde** sowie einen **„Wecker morgen“** als Home-Assistant-Entities
bereit – gedacht als Grundlage für einen Schulwecker (z. B. Wecker 45 Minuten vor
Stundenbeginn, nur an Schultagen).

Die Entities werden per **MQTT Discovery** angelegt; es sind keine manuellen
YAML-Einträge in Home Assistant nötig. MQTT-Broker, Port, TLS und Zugangsdaten
kommen automatisch vom Supervisor (Mosquitto-Add-on).

## Funktionen

- Abruf des IServ-Stundenplans per HTTP(S) inklusive Login (Cookie-Session)
- Unterstützte Formate: **HTML-Tabelle**, **JSON** (auch die JSON-Schnittstelle
  der neuen IServ-Stundenplanansicht) und **iCalendar (ICS)**,
  automatische Formaterkennung
- **Ferien und Feiertage automatisch** aus openholidaysapi.org für das
  eingestellte Bundesland (kein API-Key, sieben Tage Cache)
- Regeln:
  - **Montag–Donnerstag:** vor 12:00 Uhr gilt der heutige Tag, ab 12:00 Uhr der Folgetag
  - **Freitag/Samstag/Sonntag:** der nächste Schultag (Montag) wird vorbereitet –
    ohne Sonderregel, aber auch ohne Wecker am Wochenende
  - Wochenenden, Ferien/Feiertage und Tage ohne stattfindende Stunde werden übersprungen
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
- **Internet für die Ferien-/Feiertags-API** (openholidaysapi.org). Ohne
  Internet läuft das Add-on mit den zuletzt gespeicherten Feriendaten weiter.

## Konfiguration

Für den Normalfall genügt **eine** Angabe: `iserv_url` (plus Benutzername,
Passwort und Bundesland). Alles andere ermittelt das Add-on selbst.

| Option | Typ | Standard | Bedeutung |
| --- | --- | --- | --- |
| `iserv_url` | URL | `https://schule.example.org` | **Die einzige Pflichtangabe:** Basisadresse der Schule, z. B. `https://ebg.kiel.eu` – mit oder ohne `/iserv` am Ende. Auch die komplette Planseite (`…/iserv/timetable`, `…/iserv/plan`) wird akzeptiert; der Modulpfad wird automatisch abgeschnitten |
| `username` | Text | *(leer)* | IServ-Benutzername (leer = ohne Login abrufen) |
| `password` | Passwort | *(leer)* | IServ-Passwort |
| `bundesland` | Auswahl | `Schleswig-Holstein` | Bundesland für Ferien und Feiertage (alle 16 Länder) |
| `period_times` | Text | 1–6 | Stundenzeiten, siehe Format unten |
| `refresh_minutes` | Zahl | `30` | Abrufintervall in Minuten (5–720) |
| `debug_dump` | Schalter | `aus` | Rohantwort unter `/data/debug/plan_raw.*` speichern und Debug-Logging aktivieren |

Die Plantabelle, das Format, die SSL-Prüfung, das Log-Level und das Gerät
(`IServ Stundenplan`) werden automatisch bestimmt. **Alte Optionen** aus 1.0.x
(`plan_url`, `plan_format`, `klasse`, `free_days`, `lookahead_days`, `mqtt_*`,
`verify_ssl`, `log_level`, `device_name`) werden ignoriert – siehe
[CHANGELOG.md](CHANGELOG.md) für die Migration.

### MQTT

Das Add-on nutzt `mqtt:want`: Der Supervisor liefert Host, Port, TLS und
Zugangsdaten des Mosquitto-Add-ons. Ein externer Broker oder eigene
MQTT-Zugangsdaten lassen sich nicht mehr eintragen; läuft der Broker als
eigenes Add-on, ist nichts weiter zu tun.

Meldet das Log `MQTT-Verbindung abgelehnt: Not authorized`, sind die
Supervisor-Zugangsdaten veraltet. Ab 1.1.0 fragt das Add-on sie nach einem
Fehlschlag erneut ab; hilft das nicht, das Mosquitto-Add-on neu starten (es
schreibt Benutzer und Passwort neu).

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

## Ferien und Feiertage

Das Add-on lädt Schulferien und gesetzliche Feiertage automatisch für das
eingestellte `bundesland` von **openholidaysapi.org** (kostenlos, ohne API-Key):

- Abruf beim Start und danach alle sieben Tage, gespeichert in
  `/data/holidays.json`
- Bei einem Bundesland-Wechsel wird sofort neu geladen
- Ist die API nicht erreichbar, gelten die gespeicherten Daten weiter; im
  Status-Attribut `warnings` steht dann ein Hinweis
- **IServ-Daten haben Vorrang:** Liefert IServ für einen Tag Stunden, ist das
  ein Schultag – auch wenn der Kalender Ferien meldet (z. B. regionale
  Ausnahmen wie Insel-Ferien)

Damit entfällt die frühere Option `free_days`.


## Entities

Alle Entities hängen am Gerät „IServ Stundenplan“.

| Entity | Typ | Bedeutung |
| --- | --- | --- |
| `sensor.stundenplan_status` | Sensor | `verbinden` (Start/keine Daten), `verbunden` oder `verbindung fehlgeschlagen` |
| `sensor.erste_unterrichtsstunde` | Sensor | Stundennummer der ersten stattfindenden Stunde; `unknown`, wenn keine berechnet werden kann |
| `sensor.naechster_schultag` | Datum | Datum des nächsten Schultags mit stattfindender Stunde |
| `binary_sensor.wecker_morgen` | Binary Sensor | `on` nur, wenn **morgen** ein Schultag mit stattfindender Stunde ist (Sa/So immer `off`) |

Die Entity-IDs sind Vorschläge von MQTT Discovery: Sie gelten beim ersten
Anlegen der Entity und bleiben danach erhalten, auch wenn du sie in Home
Assistant umbenennst. Das Add-on sendet dafür `default_entity_id` (ab Home
Assistant 2026.4) und zusätzlich `object_id` (ältere Versionen).

Zusatzinformationen als Attribute:

- **Erste Unterrichtsstunde:** `start` (`07:45`), `start_iso` (ISO 8601 mit
  Zeitzone), `hours_until` (ganze Stunden ab jetzt), `target_date`, `subject`,
  `room`, `planned_first_period`, `cancelled_periods`
- **Nächster Schultag:** `reason`, `status`, `cancelled_periods`
- **Wecker morgen:** `date`, `start`, `period`, `subject`, `room`, `reason`
  (z. B. `Wochenende`, `Herbstferien`, `keine Daten`) und `plan_status`
- **Status:** `reason`, `plan_status` (`ok`, `ferien`, `kein_unterricht`,
  `error`), `bundesland`, `holidays_updated`, `fetched_at`, `age_minutes`,
  `mqtt_connected`, `iserv_url`, `last_error`, `skipped`, `warnings`

`skipped` listet die übersprungenen Tage mit Grund auf (Wochenende, Ferien,
kein Unterricht, keine Daten) – hilfreich, wenn der nächste Schultag
unerwartet weit in der Zukunft liegt.

## MQTT-Topics

| Topic | Inhalt |
| --- | --- |
| `homeassistant/sensor/iserv_status/config` | Discovery (analog für die übrigen Entities) |
| `iserv_school_plan/state` | Zustand als JSON (retained) |
| `iserv_school_plan/availability` | `online` / `offline` (retained, Last Will) |

Beim Verbinden löscht das Add-on die Discovery-Topics der Entities aus 1.0.x
(`iserv_stundenbeginn`, `iserv_schulwecker`) mit einem leeren retained Payload.
Der Zustand wird nur bei Änderungen gesendet, bleibt aber retained – nach einem
Neustart von Home Assistant sind die Werte sofort wieder da.

## Beispiel: Schulwecker-Automation

Der Wecker wird gestellt, sobald `binary_sensor.wecker_morgen` auf `on` geht
(also am Nachmittag/Abend vor einem Schultag):

```yaml
automation:
  - alias: "Schulwecker stellen"
    mode: single
    triggers:
      - trigger: state
        entity_id: binary_sensor.wecker_morgen
        to: "on"
    actions:
      - action: notify.mobile_app_mein_handy
        data:
          title: "Schule"
          message: >-
            Morgen ({{ state_attr('binary_sensor.wecker_morgen', 'date') }})
            {{ state_attr('binary_sensor.wecker_morgen', 'start') }} Uhr,
            {{ state_attr('binary_sensor.wecker_morgen', 'period') }}. Stunde
            {{ state_attr('binary_sensor.wecker_morgen', 'subject') or '' }}
            in Raum {{ state_attr('binary_sensor.wecker_morgen', 'room') or '?' }}.
```

Klingeln 45 Minuten vor der ersten Stunde (nur wenn der Wecker aktiv ist):

```yaml
automation:
  - alias: "Schulwecker klingeln"
    mode: single
    triggers:
      - trigger: template
        value_template: >-
          {% set start = state_attr('sensor.erste_unterrichtsstunde', 'start_iso') %}
          {% if not start %}
            false
          {% else %}
            {{ now() >= as_datetime(start) - timedelta(minutes=45) }}
          {% endif %}
    conditions:
      # Nur am tatsaechlichen Unterrichtstag klingeln.
      - condition: template
        value_template: >-
          {{ state_attr('sensor.erste_unterrichtsstunde', 'target_date') == now().strftime('%Y-%m-%d') }}
    actions:
      - action: notify.mobile_app_mein_handy
        data:
          title: "Schule"
          message: >-
            Erste Stunde: {{ state_attr('sensor.erste_unterrichtsstunde', 'subject') }}
            um {{ state_attr('sensor.erste_unterrichtsstunde', 'start') }} Uhr
            in Raum {{ state_attr('sensor.erste_unterrichtsstunde', 'room') or '?' }}.
```

`hours_until` eignet sich für Anzeigen („in 9 Stunden“).


## Verhalten im Detail

- **Zeitzone:** fest `Europe/Berlin` (Sommer-/Winterzeit wird korrekt behandelt),
  unabhängig von der Zeitzone des Containers.
- **12-Uhr-Regel:** Montag–Donnerstag gilt vor 12:00 Uhr der heutige Tag, ab
  12:00 Uhr der Folgetag. Freitag ist bis 12:00 Uhr ein normaler Schultag.
- **Wochenende:** Samstag/Sonntag wird der Montag (bzw. der nächste Schultag)
  vorbereitet. `binary_sensor.wecker_morgen` ist samstags und sonntags immer
  `off`.
- **Keine Daten:** Liegt für einen Tag kein Eintrag vor (z. B. weil IServ den
  Plan noch nicht veröffentlicht hat), wird nicht geraten: Status `error`,
  `reason` nennt den Grund.
- **Ferien/Feiertage:** kommen aus dem automatisch geladenen Kalender. Liefert
  IServ für einen Ferientag Daten, gelten die IServ-Daten.
- **Abrufe:** Start des Add-ons, danach alle `refresh_minutes`. Nach einem Fehler
  wird frühestens nach 5 Minuten erneut versucht; bei weiteren Fehlversuchen
  verdoppelt sich die Wartezeit (10, 20, 40, höchstens 60 Minuten). Lehnt IServ
  den Filter der JSON-Schnittstelle ab (HTTP 400, „Die erforderliche Eigenschaft
  classes fehlt.“), gilt sofort das normale Intervall – schnelles Wiederholen
  ändert daran nichts. Nachts (22–5 Uhr) wird nur geladen, wenn die Daten älter
  als 6 Stunden sind. Der Zustand wird alle 30 Sekunden neu berechnet
  (12-Uhr-Regel, Tageswechsel).
- **Cache:** Zuletzt erfolgreich geladene Daten liegen in `/data/cache.json` und
  werden nach einem Neustart sofort verwendet, bis der erste Abruf gelingt.

## Fehlersuche

| Symptom | Ursache / Lösung |
| --- | --- |
| `sensor.stundenplan_status` = `verbindung fehlgeschlagen`, `last_error: iserv_url muss …` | `iserv_url` auf die eigene Schuladresse setzen – mehr ist nicht nötig |
| `Login fehlgeschlagen …` | Benutzername/Passwort prüfen; Schul-URL prüfen (`/iserv/auth/login`). Der IServ-Hinweis steht dabei (z. B. „Bitte warten Sie … Sekunden“ = Brute-Force-Schutz nach mehreren Starts) |
| `IServ ist im Wartungsmodus …` | IServ ist selbst offline – später erneut versuchen |
| `Plan enthielt keine verwertbaren Daten` | IServ liefert HTML/JSON, das kein Parser kennt: `debug_dump: true` setzen, neu starten, `/data/debug/plan_raw.*` ansehen (per SSH/Samba) und ggf. einen Parser in `app/parsers.py` ergänzen |
| `Wochengitter ohne Datum und ohne Wochenanker ignoriert` | Die Seite zeigt nur Wochentage und keinen Anker (Filter-JSON, `19.10. - 25.10.2026`, `KW 43`). Ohne Datum werden keine Stunden geraten; `table_summary` im Log nennt die gefundene Struktur |
| HTML enthält nur eine Kopfzeile, keine Unterrichtszeilen | Die neue IServ-Oberfläche lädt die Zeilen per JavaScript nach. Das Add-on fragt deshalb zusätzlich die JSON-Schnittstelle ab (`/iserv/timetable/data?filter=…` mit `startDate`, `endDate`, `classes`, `teachers`, `rooms`) und wertet einen verlinkten iCal-Export aus |
| `Login erfolgreich, aber die IServ-Sitzung wurde nicht übernommen` | IServ leitete per Meta-Refresh/JavaScript weiter; das Add-on führt diese Weiterleitung selbst aus. Bleibt die Meldung, ist meist eine **2FA-Pflicht** oder ein Sonderlogin der Schule die Ursache |
| `MQTT-Verbindung abgelehnt: Not authorized` | Broker-Zugangsdaten veraltet: Mosquitto-Add-on neu starten (schreibt Benutzer/Passwort neu); das Add-on fragt sie zusätzlich selbst erneut ab |
| Entities erscheinen nicht | MQTT-Integration und Broker prüfen, Add-on-Log ansehen. Nach dem Update auf 1.1.0 sind `sensor.stundenbeginn` und `binary_sensor.schulwecker_aktiv` bewusst entfernt – Automatisierungen auf `sensor.erste_unterrichtsstunde` und `binary_sensor.wecker_morgen` umstellen |
| Ferien fehlen oder sind falsch | `bundesland` prüfen; `warnings`/`holidays_updated` im Status-Attribut kontrollieren. Ohne Internet gelten die gespeicherten Daten (max. sieben Tage alt) |
| Wecker bleibt aus, obwohl Unterricht ist | `sensor.erste_unterrichtsstunde` (Attribute `start`, `target_date`) und `sensor.stundenplan_status` prüfen; ggf. Startzeiten über `period_times` ergänzen |
| `SSL: CERTIFICATE_VERIFY_FAILED` | Das Schul-Zertifikat ist im Container nicht vertrauenswürdig. Die Prüfung ist immer aktiv; die Schule muss ein gültiges Zertifikat (z. B. Let's Encrypt) ausliefern |

Log des Add-ons: **Einstellungen → Add-ons → IServ Stundenplan → Log**. Für mehr
Details `debug_dump: true` setzen (aktiviert zusätzlich Debug-Logging).

## Sicherheit

- Das IServ-Passwort liegt in der Add-on-Konfiguration (`/data/options.json` im
  Container) im Klartext. Wenn möglich, ein eigenes Konto bzw. separates Passwort
  verwenden.
- Zugangsdaten niemals in Git einchecken.
- `debug_dump` kann personenbezogene Daten (Klassen, Räume, Lehrkräfte) enthalten
  – nur temporär aktivieren.
- TLS-Zertifikate werden immer geprüft (kein Abschalt-Schalter mehr).


## Entwicklung

```text
iserv_school_plan/
├── app/
│   ├── main.py      # Schleife, Cache, Statusaufbau
│   ├── config.py    # Add-on-Optionen (Whitelist)
│   ├── holidays.py  # Ferien/Feiertage (openholidaysapi.org + Cache)
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

Tests lokal ausführen (Python ≥ 3.11, `pytest`, `pyyaml` und `paho-mqtt` installiert):

```powershell
cd iserv_school_plan
python -m pytest -q
```

Der Login-Ablauf wird in `tests/test_iserv.py` gegen einen lokalen HTTP-Server
getestet; die Parser-Tests arbeiten mit anonymisierten Ausschnitten, die
Ferien-Tests mit Beispielantworten der API (ohne Netzwerk).

### Anpassung an die eigene IServ-Installation

Das Add-on sucht die Plantabelle selbst (`/iserv/plan`, `/iserv/timetable`,
`/iserv/time-table`) und erkennt HTML, JSON und ICS automatisch. Die Parser sind
tolerant gegenüber Spaltennamen („Fach“, „Raum“, „Zeit“, „Art“, „Klasse“, …) und
Uhrzeitformaten (`07:45 - 08:30`, `7:45`, `745`). Gelesen werden auch die
Wochenansicht (Spalten = Wochentage mit Datum, verbundene Zellen für
Doppelstunden; Wochenanker aus Filter-JSON, `19.10. - 25.10.2026` oder `KW 43`)
und – wenn die HTML-Auswertung leer bleibt – ein auf der Seite verlinkter
iCal-Export sowie die JSON-Schnittstelle der Stundenplanansicht
(`/iserv/timetable/data?filter=` mit `startDate`, `endDate`, `classes`,
`teachers`, `rooms`; Woche für Woche, weil größere Zeiträume mit HTTP 400
abgewiesen werden). Bei einem unbekannten Format:

1. `debug_dump: true` setzen und das Add-on neu starten.
2. `/data/debug/plan_raw.*` und `plan_raw.meta.json` prüfen; die Fehlermeldung
   nennt zusätzlich die Tabellenstruktur (`Tabellen: … Kopf: … Wochengitter …`)
   bzw. die JSON-Struktur (`[JSON: …]`).
3. Bei Bedarf in `app/parsers.py` einen weiteren Parser ergänzen, in
   `detect_format`/`parse_payload` registrieren und in `tests/test_parsers.py`
   testen.
