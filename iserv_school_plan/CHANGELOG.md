# Changelog

## 1.0.9

- Neue Datenquelle: die JSON-Schnittstelle der IServ-Stundenplanansicht
  (`/iserv/timetable/data?filter={"startDate":"19.10.2026","endDate":"…"}`).
  Die neue Oberflaeche liefert unter `/iserv/timetable` nur noch ein
  JavaScript-Geruest (Tabelle mit einer Kopfzeile, Stunden kommen per `portal.js`
  nach) - bisher blieb der Abruf dort ohne Daten. Das Add-on fragt jetzt - nach
  der HTML-Auswertung und dem iCal-Export - diese Schnittstelle mit der
  bestehenden Anmeldung ab (`X-Requested-With: XMLHttpRequest`,
  `Accept: application/json`). Der Filter wird aus einem Zeitfenster um heute
  gebaut (1 Tag zurueck, 21 Tage voraus, Format `TT.MM.JJJJ`); ohne Filter wird
  zusaetzlich probiert
- Die JSON-Antwort wird tolerant gelesen: erkannt wird eine Stunde an Datum +
  Fach + (Stunde oder Beginn). Feldnamen duerfen deutsch oder englisch sein
  (`date`/`datum`, `start`/`beginn`, `subject`/`fach`, `room`/`raum`,
  `period`/`stunde`), Werte duerfen Objekte sein (`{"name": "Mathematik"}`),
  Datum darf am Eintrag oder am Tageszweig haengen (`"19.10.2026": [...]`),
  Datumsformate ISO, `19.10.2026`, `19.10.` und Zeitstempel. Ausfall wird aus
  `cancelled`/`ausfall`/`art`/`typ` erkannt, Lehrkraft und Hinweis landen in der
  Notiz. Konfigurations- und Zeitraster-Eintraege werden ignoriert
- Fehlermeldungen enthalten die Struktur der JSON-Antwort (nur Schluessel,
  Datums-/Zeitwerte im Klartext, keine Namen), z. B.
  `[JSON: {"data": [0x …], "config": dict[2]}]` - damit ist ohne `debug_dump`
  erkennbar, wie die Instanz antwortet
- Bei Erfolg sagt das Log `Stundenplan aus der IServ-JSON-Schnittstelle gelesen:
  …/iserv/timetable/data?filter=…`

## 1.0.8

- IServ-Stundenplanansicht wird ausgewertet: Liefert `/iserv/timetable` die
  Wochenansicht (Spalten = Wochentage mit Datum, Zeilen = Stunden) statt einer
  Vertretungsplan-Tabelle, liest das Add-on jetzt dieses Gitter - auch wenn die
  Spalten nur ein Datum tragen (`19.10.`). Bisher endete der Abruf dort mit
  „Tabelle ohne erkannte Spaltenueberschrift ignoriert“ und „Plan enthielt keine
  verwertbaren Daten“
- Verbundene Zellen (`rowspan`/`colspan`, z. B. Doppelstunden) werden korrekt
  aufgeloest - ohne das waeren alle folgenden Tage um eine Spalte verschoben
- Fach, Raum und Lehrkraft werden aus den Zeilen der Gitterzelle gelesen
  (Fach = erste Zeile, Raum = Zeile mit Raummuster, Rest = Notiz); Uhrzeiten
  kommen aus der Stundenspalte, sonst greift `period_times`
- Wochengitter ohne Datum in der Spaltenueberschrift wird bewusst **nicht**
  ausgewertet (sonst wuerden Stunden einem falschen Tag zugeordnet); das Log
  sagt das jetzt ausdruecklich
- iCal-Export als Ausweichweg: Enthaelt die Planseite einen Export-Link
  (`.ics`, „Stundenplan exportieren“), nutzt das Add-on ihn automatisch, sobald
  die HTML-Auswertung keine Zeilen liefert. Der Export enthaelt Uhrzeiten und
  funktioniert auch bei einer JavaScript-Ansicht. Es werden nur Links auf dem
  eigenen Host verfolgt
- Fehlermeldungen nennen die Tabellenstruktur, z. B.
  `2 Tabellen: 1) 14 Zeilen x 6 Spalten, Kopf: 'Stunde | Montag 19.10. | …',
  Wochengitter mit Datum`. Damit ist ohne `debug_dump` erkennbar, was die Seite
  tatsaechlich liefert
- `extract_period` liest auch kombinierte Kopfzellen wie `1 07:45 - 08:30`

## 1.0.7

- Fix fuer `Plan-Abruf fehlgeschlagen (HTTP 404)`: Antwortet der erste Plan-Pfad
  mit HTTP 404/405/410 (Modul auf dieser IServ-Instanz nicht vorhanden), probiert
  das Add-on jetzt den naechsten bekannten Pfad (`/iserv/plan` bzw.
  `/iserv/timetable`), statt den Abruf abzubrechen. Bisher wurde der Ausweich-Pfad
  nur nach einer 200-Antwort ohne Tabelle versucht - eine 404 beendete alles
- Fehlermeldungen nennen die tatsaechlich aufgerufene URL samt HTTP-Status
  (`[Versuche: https://.../iserv/plan: HTTP 404; ...]`); bei Login-Fehlern steht
  die Anmeldeadresse dabei. `Plan-Abruf fehlgeschlagen (HTTP 404)` allein liess
  nicht erkennen, welcher Pfad fehlt. "Plan enthielt keine verwertbaren Daten"
  listet jetzt zusaetzlich die geprueften URLs
- `Origin`-Header beim Login enthaelt nur noch Schema und Host (vorher inklusive
  `/iserv`-Pfad) - IServ wertet ihn als CSRF-Schutz aus
- `iserv_url`/`plan_url` unveraendert: eine `iserv_url` mit Modulpfad wird
  weiterhin auf `https://schule.de/iserv` gekuerzt
- Ein Abruffehler ueberschreibt keine vorhandenen Daten: der letzte gute Stand
  bleibt erhalten und es wird nichts faelschlich als "faellt aus" gemeldet
  (jetzt durch einen Test abgesichert)

## 1.0.6

- Fix für IServ mit neuer Web-Oberfläche (OIDC): Nach dem Login antwortet IServ
  mit einer Zwischenseite, die per `<meta http-equiv="refresh">` zu
  `/iserv/app/authentication/redirect?code=…` weiterleitet. Diese Weiterleitung
  führt das Add-on jetzt selbst aus (Meta-Refresh und JavaScript-Varianten),
  damit die Sitzung vollständig ist. Bisher blieb die Sitzung unvollständig und
  jede Planseite zeigte wieder die Anmeldung („Plan enthielt keine
  verwertbaren Daten … keine `<table>`“)
- Die Weiterleitung wird auch beim Plan-Abruf nachgeholt (abgelaufene Sitzung)
  und der Plan danach erneut geladen
- `iserv_url` darf jetzt auch die komplette Planseite sein, z. B.
  `https://schule.de/iserv/timetable` oder `…/iserv/plan`: Modulpfade werden
  abgeschnitten, verwendet wird `https://schule.de/iserv`; das Log nennt die
  korrigierte Basis
- Bleibt die Auth-Weiterleitung ohne Ziel stehen, gibt es eine klare Meldung
  („IServ-Sitzung wurde nicht übernommen“) statt „keine `<table>`“
- Benutzerkennung des Add-ons nennt sich jetzt `Mozilla/5.0 (compatible; …)`,
  damit IServ die Anfrage nicht als veralteten Browser behandelt

## 1.0.5

- Vereinfachung: es genügt jetzt **eine** Angabe – `iserv_url`. `plan_url` ist
  optional und standardmäßig leer; das Add-on nutzt dann automatisch
  `/iserv/plan` und probiert bei Bedarf `/iserv/timetable`
- `plan_url` versteht absolute URLs, `/iserv/...`-Pfade und nackte Pfade wie
  `plan` (wird unter `/iserv/` einsortiert)
- Wartungsmodus wird erkannt: HTTP 503 mit `X-IServ-Maintenance` bzw. der
  „Wartungsarbeiten“-Seite ergibt eine klare Meldung („IServ ist im
  Wartungsmodus“) statt eines Tracebacks mit „iserv_url prüfen“
- `IservError` wird im Betrieb sauber als Warnung geloggt (kein Traceback mehr
  bei vorübergehenden Serverfehlern)
- Kein Fehler mehr, wenn `plan_url` fehlt (`plan_url` ist im Schema optional)

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
