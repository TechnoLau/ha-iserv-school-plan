# Dokumentation

Dieses Add-on liest den IServ-Stundenplan und stellt die erste tatsächlich
stattfindende Unterrichtsstunde per MQTT Discovery in Home Assistant bereit.

## Schnellstart

1. MQTT-Broker (Mosquitto-Add-on) und MQTT-Integration einrichten. Bei
   `mqtt_host: core-mosquitto` werden die Zugangsdaten automatisch beim
   Supervisor abgefragt.
2. `iserv_url` auf die Schuladresse setzen, z. B. `https://ebg.kiel.eu` –
   mit oder ohne `/iserv` am Ende, beides funktioniert. Das ist die einzige
   Pflichtangabe: die Plantabelle sucht das Add-on selbst. Wird versehentlich
   die Planseite eingetragen (`…/iserv/timetable`), kürzt das Add-on den
   Modulpfad automatisch (ab 1.0.6).
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
- Bei „Plan-Abruf fehlgeschlagen (HTTP 404)“: Der Pfad existiert auf dieser
  IServ-Instanz nicht (z. B. `/iserv/plan` abgeschaltet, Tabelle liegt unter
  `/iserv/timetable`). Ab Version 1.0.7 probiert das Add-on alle bekannten Pfade
  und listet in `last_error` jede versuchte URL mit HTTP-Status auf. Stehen dort
  nur 404er, fehlt die Freischaltung des Moduls für das Konto oder `iserv_url`
  zeigt auf den falschen Host; `iserv_url` bleibt dabei die Basisadresse
  (`https://schule.de`), `plan_url` bleibt leer.
- Bei „Plan enthielt keine verwertbaren Daten“: Ab Version 1.0.8 nennt die
  Meldung die Tabellenstruktur (`Tabellen: 1) 14 Zeilen x 6 Spalten, Kopf:
  'Stunde | Montag 19.10. | …', Wochengitter mit Datum`) und wertet die
  Stundenplan-Wochenansicht (Wochentage als Spalten, Stunden als Zeilen,
  verbundene Zellen für Doppelstunden) aus. Zusätzlich wird ein auf der Seite
  verlinkter iCal-Export genutzt, wenn die HTML-Auswertung keine Zeilen liefert.
  Meldet das Add-on „Wochengitter ohne Datum und ohne Wochenanker“, enthalten die
  Spaltenköpfe nur Wochentage und die Seite liefert keinen Anker: Seit **1.0.12**
  wertet das Add-on solche Gitter aus, wenn es einen Wochenanfang findet – das
  Filter-JSON der Ansicht (`"startDate": "19.10.2026"`), einen Zeitraum in der
  Überschrift (`19.10. - 25.10.2026`) oder `KW 43`. Die Warnung nennt Zeilenzahl
  und Kopfzeile, `table_summary` zusätzlich `Wochenanker 19.10.2026` bzw.
  `kein Wochenanker`. Bleibt es bei `kein Wochenanker`: die persönliche
  iCal-Export-Adresse aus IServ als `plan_url` mit `plan_format: ics` eintragen.
  Zeigen die Tabellen in der Meldung nur
  **eine** Zeile (z. B. `1) 1 Zeilen x 6 Spalten, Kopf: 'Std., Montag, …'`),
  stehen im HTML überhaupt keine Unterrichtszeilen – IServ lädt sie in der neuen
  Oberfläche per JavaScript nach (`portal.js`). Ab Version 1.0.9 holt das Add-on
  die Stunden deshalb selbst über die JSON-Schnittstelle der Ansicht
  (`/iserv/timetable/data?filter={"startDate":"19.10.2026","endDate":"…","classes":[],"teachers":[],"rooms":[]}`,
  mit der bestehenden Anmeldung, `X-Requested-With: XMLHttpRequest` und
  `Accept: */*` – `application/json` wertet IServ als API-Aufruf und antwortet
  mit HTTP 401, deshalb seit 1.0.10 nicht mehr). Der Filter umfasst seit 1.0.11
  **genau eine Woche** (Montag bis Sonntag), weil das Modul größere Zeiträume
  mit HTTP 400 („Fehler beim Verarbeiten der Anfrage“) abweist; die Antworten
  der Wochen werden zusammengefasst. Seit **1.0.12** stehen zusätzlich
  `classes`, `teachers` und `rooms` im Filter – ohne sie antwortet das Modul mit
  HTTP 400 und „Die erforderliche Eigenschaft classes fehlt.“ (der Fehler aus
  1.0.11). Bleibt eine Schreibweise ohne Stunden, probiert das Add-on `["%"]`
  als Platzhalter und merkt sich die funktionierende; ein abgelehnter Filter
  beendet den Abruf nach der ersten Woche. Erkennt der Parser die Antwort
  nicht, steht ihre Struktur als `[JSON: …]` in `last_error` (nur Schlüssel,
  keine Namen); seit 1.0.10 zusätzlich der HTTP-Status je Versuch. Alternativ
  bleibt der iCal-Export als `plan_url` mit `plan_format: ics` (Adresse muss
  `BEGIN:VCALENDAR` liefern). Sonst
  `debug_dump: true` setzen, Add-on neu starten und `/data/debug/plan_raw.*`
  prüfen. Meldet das Add-on „keine
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

