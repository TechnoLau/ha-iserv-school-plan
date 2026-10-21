"""IServ-Login und Abruf des Stundenplans.

Der Login erfolgt formularbasiert ueber die IServ-Anmeldeseite
(``/iserv/auth/login``): Die Formularfelder (inkl. CSRF-Token) werden aus der
HTML-Seite gelesen, damit unterschiedliche IServ-Versionen funktionieren.
Der eigentliche Plan wird von der konfigurierten ``plan_url`` geladen - das
Format wird automatisch erkannt (ICS, JSON oder HTML-Tabelle).
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPSHandler,
    Request,
    build_opener,
)

from .parsers import (
    detect_format,
    json_summary,
    parse_payload,
    parse_portal_json,
    table_summary,
)
from .plan import TZ, PlanDataset

_LOGGER = logging.getLogger(__name__)
USER_AGENT = (
    "Mozilla/5.0 (compatible; HA-IServ-Addon/1.0; "
    "+https://github.com/TechnoLau/ha-iserv-school-plan)"
)
# Pfade, unter denen IServ die klassische Plantabelle ausliefert. Der erste
# Eintrag ist gleichzeitig der Standard, wenn keine ``plan_url`` gesetzt ist.
# Die weiteren werden probiert, falls der erste keine Tabelle liefert (z. B.
# weil ``plan_url`` auf die JavaScript-Web-App zeigt).
PLAN_PATHS = ("/iserv/plan", "/iserv/timetable")
# JSON-Schnittstelle der neuen Stundenplanansicht: Der Browser laedt die Woche
# mit ``/iserv/timetable/data?filter={"startDate":"19.10.2026",...}`` nach - die
# HTML-Seite selbst enthaelt oft nur die Kopfzeilen der Tabelle.
TIMETABLE_DATA_PATH = "timetable/data"
# Zeitfenster fuer den Filter (heute rueckwaerts/vorwaerts in Tagen). Der
# Vorlauf deckt die Vorschau des Add-ons (max. 14 Tage) mit Reserve ab.
PORTAL_DAYS_BACK = 1
PORTAL_DAYS_AHEAD = 21

# HTTP-Status, mit denen IServ antwortet, wenn ein Modulpfad auf dieser
# Installation nicht (mehr) existiert. Solche Antworten duerfen den Abruf nicht
# beenden - der naechste bekannte Plan-Pfad wird probiert (z. B. liefert die
# eine Schule die Tabelle unter /iserv/plan, die andere unter /iserv/timetable).
PLAN_PATH_MISSING_CODES = (404, 405, 410)
# Erkennungsmerkmale der IServ-Anmeldeseite (klassische Seite und neue Web-App).
# Bewusst nur sehr spezifische Marker, damit normale Seiten nicht als Login gelten.
LOGIN_PAGE_MARKERS = ('id="login-form"', 'id="password_login"')
# Merkmale des IServ-Wartungsmodus (eigener Webserver, eigener Hinweistext).
MAINTENANCE_MARKERS = ("iserv maintenance web server", "wartungsarbeiten", "maintenance")
# Merkmale der IServ-Auth-Zwischenseite (OIDC). Nach dem Login antwortet IServ
# mit einer Seite, die per Meta-Refresh zu
# ``/iserv/app/authentication/redirect?code=...`` weiterleitet. urllib folgt nur
# HTTP-Redirects - ohne dieses Nachfassen bleibt die Sitzung der Web-Oberflaeche
# unvollstaendig und jede Planseite zeigt wieder die Anmeldung.
AUTH_REDIRECT_MARKERS = (
    'id="redirect-text"',
    "/auth/static/js/redirect",
    "authentication/redirect",
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT_RE = re.compile(r"<script[^>]+src=\"([^\"]+)\"", re.IGNORECASE)
# iCal-Export der IServ-Stundenplanansicht ("Stundenplan exportieren"). Der
# Export enthaelt Uhrzeiten und funktioniert auch dann, wenn die HTML-Ansicht
# nur ein JavaScript-Geruest zeigt.
_LINK_RE = re.compile(
    r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_ICAL_HREF_HINTS = (".ics", "/ical", "ical/", "format=ical", "export=ical")
_WAIT_RE = re.compile(r'data-seconds="(\d+)"')
_META_REFRESH_RE = re.compile(r"<meta[^>]*http-equiv=[\"']?refresh[\"']?[^>]*>", re.IGNORECASE)
_META_CONTENT_RE = re.compile(
    r"content\s*=\s*\"([^\"]*)\"|content\s*=\s*'([^']*)'", re.IGNORECASE
)
_META_URL_RE = re.compile(r"url\s*=\s*['\"]?([^'\">\s]+)", re.IGNORECASE)
_JS_REDIRECT_RE = re.compile(
    r"location(?:\.href)?\s*=\s*[\"']([^\"']+)[\"']"
    r"|location\.(?:replace|assign)\(\s*[\"']([^\"']+)[\"']"
    r"|fetch\(\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)
# Typische Fehlertexte der IServ-Anmeldeseite (Abgleich auf dem Sichttext).
LOGIN_ERROR_HINTS = (
    "Ungültige Anmeldedaten",
    "Ungueltige Anmeldedaten",
    "Benutzername oder Passwort",
    "Anmeldung fehlgeschlagen",
    "gesperrt",
    "Bitte warten Sie",
)


def _iserv_url(base_url: str, path: str) -> str:
    """Bildet eine IServ-URL - auch wenn ``base_url`` schon auf ``/iserv`` endet.

    Nutzer tragen die Adresse mal als ``https://schule.de`` und mal als
    ``https://schule.de/iserv`` ein. Ohne diese Normalisierung wuerde daraus
    ``/iserv/iserv/auth/login`` und der Login laeuft ins Leere.
    """
    base = (base_url or "").rstrip("/")
    clean = path.lstrip("/")
    if base.lower().endswith("/iserv"):
        return f"{base}/{clean}"
    return f"{base}/iserv/{clean}"


def _origin(url: str) -> str:
    """Schema + Host einer URL (ohne Pfad) - Wert fuer den ``Origin``-Header.

    Der Header muss nach RFC 6454 nur Schema, Host und Port enthalten. Die
    fruehere Variante schickte den kompletten Pfad (``.../iserv``) mit, was
    IServ bei der CSRF-Pruefung des Login-Formulars ablehnen kann.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return (url or "").rstrip("/")
    return f"{parts.scheme}://{parts.netloc}"


def _looks_like_login_page(url: str, body: str) -> bool:
    """Erkennt die Anmeldeseite an URL oder Inhalt."""
    if re.search(r"/auth/login\b", url):
        return True
    return any(marker in body for marker in LOGIN_PAGE_MARKERS)


def _clean_base_url(base_url: str) -> str:
    """Entfernt Modulpfade aus der IServ-Adresse.

    Nutzer tragen auch die komplette Planseite ein (z. B.
    ``https://schule.de/iserv/timetable``). Login und Plan liegen aber immer
    unterhalb von ``/iserv`` - deshalb wird alles nach dem ersten ``/iserv/``
    abgeschnitten. Sonst entsteht ``/iserv/timetable/iserv/auth/login``.
    """
    raw = (base_url or "").strip().rstrip("/")
    match = re.match(r"^(https?://[^/]+)(/.*)?$", raw, re.IGNORECASE)
    if not match:
        return raw
    host = match.group(1)
    path = match.group(2) or ""
    index = path.lower().find("/iserv/")
    if index >= 0:
        return host + path[: index + len("/iserv")]
    return raw


def _meta_refresh_target(html: str, base_url: str) -> str:
    """Liest das Ziel eines ``<meta http-equiv="refresh">``.

    IServ nutzt beide Schreibweisen: ``content="0;url='/pfad'"`` und
    ``content="5; /pfad"`` (ohne ``url=``).
    """
    for tag in _META_REFRESH_RE.findall(html):
        content_match = _META_CONTENT_RE.search(tag)
        if not content_match:
            continue
        content = next(
            (group for group in content_match.groups() if group is not None), ""
        ).strip()
        url_match = _META_URL_RE.search(content)
        target = url_match.group(1) if url_match else ""
        if not target and ";" in content:
            target = content.split(";", 1)[1]
        target = target.strip().strip("'\"")
        if target:
            return urljoin(base_url, unescape(target))
    return ""


def _auth_redirect_target(html: str, base_url: str) -> str:
    """Sucht das Ziel der IServ-Auth-Weiterleitung (Meta-Refresh oder Script)."""
    target = _meta_refresh_target(html, base_url)
    if target:
        return target
    for match in _JS_REDIRECT_RE.finditer(html):
        candidate = next((group for group in match.groups() if group), "").strip()
        if candidate and not candidate.lower().startswith(("javascript:", "#")):
            return urljoin(base_url, unescape(candidate))
    return ""


def _needs_auth_redirect(url: str, body: str) -> bool:
    """Erkennt die Auth-Zwischenseite, die ohne JavaScript stehen bleibt."""
    if "<table" in body.lower():
        return False
    if any(marker in body for marker in AUTH_REDIRECT_MARKERS):
        return True
    if "/auth/" in url.lower() and _META_REFRESH_RE.search(body):
        return True
    return "/auth/auth" in url.lower()


def _visible_text(html: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _html_summary(html: str) -> str:
    """Kurzfassung einer HTML-Antwort fuer aussagekraeftige Fehlermeldungen."""
    title_match = _TITLE_RE.search(html)
    title = title_match.group(1).strip() if title_match else ""
    scripts = _SCRIPT_RE.findall(html)[:3]
    parts = [f"{len(html)} Zeichen"]
    if title:
        parts.append(f"Titel {title!r}")
    if "<table" in html.lower():
        parts.append(table_summary(html))
    else:
        parts.append("keine <table>")
    if scripts:
        parts.append("Skripte: " + ", ".join(scripts))
    return "; ".join(parts)


def _payload_summary(text: str, content_type: str = "") -> str:
    """Kurzfassung der letzten Antwort - HTML-Tabellen oder JSON-Struktur."""
    try:
        fmt = detect_format(text, content_type)
    except ValueError:  # pragma: no cover - detect_format wirft nicht
        fmt = "html"
    if fmt == "json":
        return "JSON " + json_summary(text)
    return _html_summary(text)


def _short_body(text: str, limit: int = 120) -> str:
    """Einzeiliger Anfang einer Antwort - fuer Fehlermeldungen im Log."""
    flat = re.sub(r"<[^>]+>", " ", text or "")
    flat = re.sub(r"\s+", " ", flat).strip()
    return flat[:limit]


def _calendar_links(html: str, base_url: str, limit: int = 3) -> list[str]:
    """Findet iCal-Export-Links auf einer Planseite.

    Nur Links mit iCal-Merkmal in der Adresse (``.ics``, ``/ical``, ...) und nur
    auf demselben Host werden zurueckgegeben - die Seite kann beliebige Links
    enthalten (Abmelden, Hilfe, ...).
    """
    host = urlsplit(base_url).netloc.lower()
    links: list[str] = []
    for match in _LINK_RE.finditer(html or ""):
        href = unescape(match.group(1)).strip()
        if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
            continue
        if not any(hint in href.lower() for hint in _ICAL_HREF_HINTS):
            continue
        url = urljoin(base_url, href)
        if urlsplit(url).netloc.lower() != host or url in links:
            continue
        links.append(url)
        if len(links) >= limit:
            break
    return links


def _server_hint(err: HTTPError, body: str | None = None) -> str:
    """Klartext-Hinweis fuer 503/429 (Wartungsmodus, Drosselung).

    IServ antwortet im Wartungsmodus mit HTTP 503 von einem eigenen Webserver
    (``Server: IServ Maintenance Web Server``, Header ``X-IServ-Maintenance: 1``).
    Ohne diese Erkennung sieht das wie ein Konfigurationsfehler aus.
    """
    headers = getattr(err, "headers", None)
    if headers is not None and headers.get("x-iserv-maintenance"):
        return "IServ ist im Wartungsmodus (Wartungsarbeiten)"
    if body is None:
        try:
            body = err.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - Body nicht lesbar
            body = ""
    lowered = body.lower()
    if any(marker in lowered for marker in MAINTENANCE_MARKERS):
        return "IServ ist im Wartungsmodus (Wartungsarbeiten)"
    if err.code == 429:
        return "IServ drosselt die Anfragen (HTTP 429) - bitte spaeter erneut versuchen"
    return ""


# Wichtig: Die neue IServ-Web-App behandelt Anfragen, deren ``Accept``-Header
# ``application/json`` enthaelt, als API-Aufruf und antwortet ohne API-Token mit
# HTTP 401 - auch bei gueltiger Sitzung. Darum wird der Header je Endpunkt
# getrennt gesetzt (HTML / Kalender / JSON) und nie gemischt.
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_CALENDAR = "text/calendar,text/plain;q=0.9,*/*;q=0.8"
ACCEPT_JSON = "application/json,text/plain;q=0.9,*/*;q=0.8"
# Fuer die JSON-Schnittstelle der Stundenplanansicht ist ``application/json``
# genau falsch herum: IServ haelt das fuer einen API-Aufruf und antwortet ohne
# API-Token mit HTTP 401 (siehe Kommentar oben). Der Browser fragt die
# Schnittstelle mit ``Accept: */*`` ab - JSON wird am Inhalt erkannt, nicht am
# Content-Type. Ohne diese Trennung blieb der Abruf stumm bei 401 haengen.
ACCEPT_ANY = "*/*"
USERNAME_HINTS = ("user", "login", "email", "mail", "name", "kennung")
DEFAULT_USER_FIELD = "_username"
DEFAULT_PASSWORD_FIELD = "_password"


class IservError(Exception):
    """Basisfehler fuer Login/Abruf."""


class AuthError(IservError):
    """Login fehlgeschlagen oder Sitzung abgelaufen."""


class FetchError(IservError):
    """Plan konnte nicht geladen werden."""


@dataclass
class IservOptions:
    base_url: str
    plan_url: str = ""
    username: str = ""
    password: str = ""
    plan_format: str = "auto"
    klasse: str = ""
    timeout: int = 30
    verify_ssl: bool = True

    def __post_init__(self) -> None:
        raw = (self.base_url or "").strip().rstrip("/")
        cleaned = _clean_base_url(raw)
        if cleaned != raw:
            _LOGGER.info(
                "iserv_url enthielt einen Modulpfad (%s) - verwendet wird %s", raw, cleaned
            )
        self.base_url = cleaned
        self.plan_url = (self.plan_url or "").strip()

    @property
    def login_url(self) -> str:
        return _iserv_url(self.base_url, "auth/login")

    @property
    def site_root(self) -> str:
        """IServ-Basis ohne ``/iserv``-Suffix (fuer Fallback-Pfade)."""
        base = self.base_url.rstrip("/")
        if base.lower().endswith("/iserv"):
            return base[: -len("/iserv")]
        return base

    @property
    def resolved_plan_url(self) -> str:
        """Plantabelle - ohne eigene ``plan_url`` der klassische IServ-Pfad.

        Damit genuegt in den Optionen die IServ-Adresse; ``plan_url`` ist nur
        noch fuer Sonderfaelle (Kalender-Export, JSON-Endpunkt) noetig.
        """
        target = (self.plan_url or "").strip()
        root = self.site_root
        if not target:
            return root + PLAN_PATHS[0]
        if target.lower().startswith(("http://", "https://")):
            return target
        if target.startswith("/iserv/"):
            return root + target
        if target.startswith("/"):
            # Absoluter Pfad auf demselben Host (z. B. "/plan" in Tests).
            return root + target
        # Nackter Pfad wie "plan" oder "plan/export" -> unter /iserv/ einsortieren.
        return root + "/iserv/" + target.lstrip("/")


class _LoginFormParser(HTMLParser):
    """Sammelt Formulare inkl. Felder, damit der Login robust bleibt."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict] = []
        self._current: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag == "form":
            self._current = {
                "action": attributes.get("action", ""),
                "method": attributes.get("method", "post").lower(),
                "fields": {},
                "has_password": False,
            }
            self.forms.append(self._current)
        elif tag == "input" and self._current is not None:
            name = attributes.get("name", "")
            if not name:
                return
            input_type = attributes.get("type", "text").lower()
            self._current["fields"][name] = {
                "value": attributes.get("value", ""),
                "type": input_type,
            }
            if input_type == "password":
                self._current["has_password"] = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None

    def login_form(self) -> dict | None:
        for form in self.forms:
            if form["has_password"]:
                return form
        return None


def _detect_fields(form: dict) -> tuple[str, str, dict[str, str]]:
    """Liefert (Benutzername-Feld, Passwort-Feld, konstante Formularwerte)."""
    fields: dict[str, dict] = form["fields"]
    password = next(
        (name for name, info in fields.items() if info["type"] == "password"),
        DEFAULT_PASSWORD_FIELD,
    )
    username = DEFAULT_USER_FIELD
    for name, info in fields.items():
        if name == password or info["type"] in ("hidden", "submit", "checkbox", "radio"):
            continue
        if any(hint in name.lower() for hint in USERNAME_HINTS):
            username = name
            break

    constants = {
        name: info["value"]
        for name, info in fields.items()
        if info["type"] == "hidden" and info["value"]
    }
    return username, password, constants


def _accept_for(url: str, plan_format: str = "auto") -> str:
    """Waehlt den ``Accept``-Header passend zu Format und Endung der URL."""
    fmt = (plan_format or "auto").strip().lower()
    path = url.split("?", 1)[0].lower()
    if fmt == "json" or path.endswith((".json", ".jsonl")):
        return ACCEPT_JSON
    if fmt == "ics" or path.endswith((".ics", ".ical")) or "/ical" in path:
        return ACCEPT_CALENDAR
    return ACCEPT_HTML


class IservClient:
    """Kapselt Session, Login und Plan-Abruf."""

    def __init__(self, options: IservOptions) -> None:
        self.options = options
        self.last_raw = ""
        self.last_content_type = ""
        self.last_url = ""
        self._format_override: str | None = None
        self._jar = CookieJar()
        handlers = [HTTPCookieProcessor(self._jar)]
        if not options.verify_ssl:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(HTTPSHandler(context=context))
        self._opener = build_opener(*handlers)
        self._logged_in = False
        # Pfade, die in diesem Durchlauf tatsaechlich geantwortet haben - damit
        # der Ausweich-Pfad sie nicht doppelt abfragt.
        self._tried_urls: set[str] = set()
        # Struktur der letzten JSON-Antwort (falls sie keine Stunden enthielt) -
        # landet in der Fehlermeldung, damit die Schnittstelle nachvollziehbar ist.
        self._portal_hint = ""

    def _request(
        self,
        url: str,
        data: bytes | None = None,
        headers: dict | None = None,
        accept: str | None = None,
    ):
        request = Request(
            url,
            data=data,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": accept or ACCEPT_HTML,
                "Accept-Language": "de-DE,de;q=0.9",
                **(headers or {}),
            },
        )
        return self._opener.open(request, timeout=self.options.timeout)

    @staticmethod
    def _read(response) -> str:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError:  # unbekannter Charset
            return raw.decode("utf-8", errors="replace")

    def login(self) -> bool:
        if not self.options.username:
            return False

        try:
            with self._request(self.options.login_url) as response:
                page = self._read(response)
                page_url = response.geturl()
        except HTTPError as err:
            hint = _server_hint(err)
            if hint:
                raise IservError(
                    f"{hint} - Login gerade nicht moeglich, bitte spaeter erneut versuchen"
                ) from err
            if err.code == 404:
                raise IservError(
                    "Loginseite nicht gefunden (HTTP 404) - iserv_url pruefen "
                    f"(aufgerufen: {self.options.login_url})"
                ) from err
            raise IservError(
                f"Loginseite nicht erreichbar (HTTP {err.code}) - iserv_url pruefen "
                f"(aufgerufen: {self.options.login_url})"
            ) from err
        except URLError as err:
            raise IservError(f"IServ nicht erreichbar: {err.reason}") from err

        parser = _LoginFormParser()
        parser.feed(page)
        form = parser.login_form()
        if form is None:
            raise AuthError(
                "Loginformular nicht gefunden. Pruefe iserv_url (z. B. https://schule.de) "
                "und ob die Anmeldung unter /iserv/auth/login liegt."
            )

        username_field, password_field, constants = _detect_fields(form)
        payload = dict(constants)
        payload[username_field] = self.options.username
        payload[password_field] = self.options.password
        action = urljoin(page_url, form["action"] or page_url)

        try:
            with self._request(
                action,
                data=urlencode(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": page_url,
                    "Origin": _origin(self.options.base_url),
                },
            ) as response:
                body = self._read(response)
                final_url = response.geturl()
        except HTTPError as err:
            if err.code == 404:
                raise AuthError(
                    "Login-POST lief ins Leere (HTTP 404) - IServ hat die "
                    f"Anmeldeadresse nicht angenommen ({action}). iserv_url ohne "
                    "Modulpfad eintragen, z. B. https://schule.de"
                ) from err
            raise AuthError(
                f"Login fehlgeschlagen (HTTP {err.code}) - Benutzername/Passwort pruefen "
                f"(Anmeldeadresse: {action})"
            ) from err
        except URLError as err:
            raise IservError(f"Login-Request fehlgeschlagen: {err.reason}") from err

        if _looks_like_login_page(final_url, body):
            raise AuthError(self._login_failure_message(body))

        # Die neue IServ-Anmeldung endet auf einer OIDC-Zwischenseite, die per
        # Meta-Refresh weiterleitet. Ohne dieses Nachfassen fehlt die Sitzung
        # der Web-Oberflaeche - der Plan zeigt dann wieder die Anmeldung.
        body, final_url, _ = self._complete_auth_redirects(response, body)

        if _looks_like_login_page(final_url, body):
            raise AuthError(self._login_failure_message(body))
        if _needs_auth_redirect(final_url, body):
            raise AuthError(
                "Login erfolgreich, aber die IServ-Sitzung wurde nicht uebernommen - "
                f"die Auth-Weiterleitung blieb stehen [{_html_summary(body)}]"
            )

        self._logged_in = True
        _LOGGER.info("IServ-Login erfolgreich (%s)", final_url)
        return True

    def _complete_auth_redirects(
        self, response, body: str, accept: str | None = None
    ) -> tuple[str, str, str]:
        """Fuehrt Auth-Weiterleitungen aus, die per Meta-Refresh laufen.

        IServ antwortet nach dem Login (und bei jeder Anfrage ohne vollstaendige
        Sitzung) mit einer Zwischenseite, die per ``<meta http-equiv="refresh">``
        zu ``/iserv/app/authentication/redirect?code=...`` schickt. Erst dieser
        Aufruf setzt die Sitzungs-Cookies der Web-Oberflaeche. ``urllib`` folgt
        nur HTTP-Redirects, darum wird hier nachgefasst.
        """
        url = response.geturl()
        content_type = response.headers.get_content_type()
        for _ in range(3):
            if not _needs_auth_redirect(url, body):
                break
            target = _auth_redirect_target(body, url)
            if not target:
                _LOGGER.debug(
                    "Auth-Weiterleitung ohne erkennbares Ziel (%s): %s",
                    _html_summary(body),
                    _visible_text(body)[:160],
                )
                break
            _LOGGER.debug("IServ-Auth-Weiterleitung wird ausgefuehrt: %s", target)
            try:
                with self._request(target, accept=accept or ACCEPT_HTML) as redirect:
                    body = self._read(redirect)
                    url = redirect.geturl()
                    content_type = redirect.headers.get_content_type()
            except HTTPError as err:
                raise AuthError(
                    f"IServ-Login-Weiterleitung fehlgeschlagen (HTTP {err.code}) - "
                    "Zugangsdaten/Rechte pruefen"
                ) from err
            except URLError as err:
                raise IservError(
                    f"IServ-Login-Weiterleitung fehlgeschlagen: {err.reason}"
                ) from err
        return body, url, content_type

    @staticmethod
    def _login_failure_message(body: str) -> str:
        """Baut aus der erneut gelieferten Anmeldeseite eine klare Meldung."""
        wait = _WAIT_RE.search(body)
        if wait and int(wait.group(1)) > 0:
            return (
                "Login abgelehnt - IServ verlangt "
                f"{wait.group(1)} Sekunden Wartezeit vor dem naechsten Versuch "
                "(Brute-Force-Schutz); bitte Add-on spaeter neu starten"
            )
        text = _visible_text(body)
        for hint in LOGIN_ERROR_HINTS:
            index = text.find(hint)
            if index >= 0:
                excerpt = text[max(0, index - 40) : index + 100].strip()
                return (
                    "Login fehlgeschlagen - IServ zeigt erneut die Anmeldeseite "
                    f"({excerpt})"
                )
        return "Login fehlgeschlagen - IServ zeigt erneut die Anmeldeseite"

    def _ensure_login(self) -> None:
        if self.options.username and not self._logged_in:
            self.login()

    def invalidate_session(self) -> None:
        """Erzwingt beim naechsten Abruf einen neuen Login."""
        self._logged_in = False

    def _plan_request(
        self, url: str, accept: str, headers: dict | None = None
    ) -> tuple[str, str, str]:
        """Holt die Plan-URL mit einem bestimmten ``Accept``-Header."""
        with self._request(url, headers=headers, accept=accept) as response:
            text = self._read(response)
            text, final_url, content_type = self._complete_auth_redirects(
                response, text, accept
            )
        if final_url.split("?", 1)[0] != url.split("?", 1)[0]:
            # Die Auth-Weiterleitung hat eine andere Seite geliefert (z. B. die
            # OIDC-Bestaetigung) - jetzt ist die Sitzung vollstaendig, also die
            # eigentliche Plan-URL erneut abrufen.
            _LOGGER.debug("Plan wird nach der Login-Weiterleitung erneut geladen: %s", url)
            with self._request(url, headers=headers, accept=accept) as response:
                text = self._read(response)
                text, final_url, content_type = self._complete_auth_redirects(
                    response, text, accept
                )
        return text, content_type, final_url

    @staticmethod
    def _plan_error(err: HTTPError, url: str, accept: str) -> AuthError:
        """Baut eine aussagekraeftige Fehlermeldung inkl. Server-Hinweisen."""
        details: list[str] = []
        challenge = err.headers.get("WWW-Authenticate") if err.headers else None
        if challenge:
            details.append(f"WWW-Authenticate: {challenge}")
        try:
            body = err.read().decode("utf-8", errors="replace")
        except (OSError, ValueError):  # pragma: no cover - Body nicht lesbar
            body = ""
        hint = _server_hint(err, body)
        if hint:
            details.append(hint)
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        if body:
            details.append(body[:200])
        suffix = f" [{'; '.join(details)}]" if details else ""
        return AuthError(
            f"Zugriff auf den Plan verweigert (HTTP {err.code}) - Login/Rechte pruefen "
            f"(URL: {url}, Accept: {accept}){suffix}"
        )

    def _plan_urls(self) -> list[str]:
        """Konfigurierte Plan-URL plus alle bekannten Ausweich-Pfade."""
        urls: list[str] = []
        seen: set[str] = set()
        for url in [self.options.resolved_plan_url, *self._fallback_plan_urls()]:
            key = url.rstrip("/").lower()
            if key in seen:
                continue
            seen.add(key)
            urls.append(url)
        return urls

    @staticmethod
    def _attempt_suffix(problems: list[str]) -> str:
        """Nennt die bereits versuchten URLs samt HTTP-Status.

        Ohne diese Angabe stand im Log nur ``HTTP 404`` - damit war nicht
        erkennbar, welcher Pfad fehlt (``/iserv/plan`` oder ``/iserv/timetable``).
        """
        return f" [Versuche: {'; '.join(problems)}]" if problems else ""

    def fetch_plan_text(self) -> tuple[str, str]:
        """Holt die Plantabelle und probiert dabei alle bekannten Plan-Pfade.

        IServ-Instanzen unterscheiden sich: Die servergerenderte Tabelle liegt
        je nach Installation unter ``/iserv/plan`` (Vertretungsplan) oder unter
        ``/iserv/timetable`` (Stundenplan/"veraltete" Ansicht). Antwortet ein
        Pfad mit 404/405/410, existiert das Modul dort nicht - dann wird der
        naechste bekannte Pfad probiert, statt den kompletten Abruf abzubrechen.
        """
        self._ensure_login()

        if not self.options.plan_url:
            _LOGGER.debug(
                "plan_url nicht gesetzt - Standardpfad %s wird verwendet",
                self.options.resolved_plan_url,
            )

        self._format_override = None
        self._tried_urls = set()
        problems: list[str] = []
        urls = self._plan_urls()

        for position, url in enumerate(urls):
            # Ausweich-Pfade werden immer im Auto-Format gelesen: Ein fuer die
            # eigene plan_url gewaehltes Format (z. B. ics) gilt dort nicht.
            primary = position == 0
            url_format = self.options.plan_format if primary else "auto"
            accept = _accept_for(url, url_format)
            # Manche IServ-Instanzen liefern fuer Kalender-/JSON-Pfade HTML aus -
            # deshalb bei 401/403 einmal mit Browser-Accept nachfassen.
            candidates = [accept] if accept == ACCEPT_HTML else [accept, ACCEPT_HTML]
            _LOGGER.debug("Plan-Abruf: %s (Accept: %s)", url, accept)

            for index, candidate in enumerate(candidates):
                try:
                    text, content_type, final_url = self._plan_request(url, candidate)
                except HTTPError as err:
                    if err.code in (401, 403):
                        if index < len(candidates) - 1:
                            _LOGGER.warning(
                                "Plan-Abruf mit Accept '%s' abgelehnt (HTTP %s) - "
                                "neuer Versuch mit Browser-Accept",
                                candidate,
                                err.code,
                            )
                            continue
                        raise self._plan_error(err, url, candidate) from err
                    # Erst hier den Body lesen: _server_hint() verbraucht ihn,
                    # _plan_error() braucht ihn fuer die Fehlermeldung.
                    hint = _server_hint(err)
                    if hint:
                        raise FetchError(
                            f"Plan-Abruf fehlgeschlagen (HTTP {err.code}) - {hint}"
                        ) from err
                    problems.append(f"{url}: HTTP {err.code}")
                    if err.code in PLAN_PATH_MISSING_CODES or position < len(urls) - 1:
                        _LOGGER.warning(
                            "%s ist auf dieser IServ-Instanz nicht abrufbar "
                            "(HTTP %s) - naechster bekannter Plan-Pfad wird probiert",
                            url,
                            err.code,
                        )
                        break
                    raise FetchError(
                        f"Plan-Abruf fehlgeschlagen (HTTP {err.code}) fuer {url}"
                        + self._attempt_suffix(problems)
                    ) from err
                except URLError as err:
                    raise FetchError(f"Plan-Abruf fehlgeschlagen: {err.reason}") from err

                self._tried_urls.add(url)
                if not primary:
                    _LOGGER.info(
                        "Plantabelle von Ausweich-Pfad %s (Format wird automatisch erkannt)",
                        url,
                    )
                    self._format_override = "auto"
                elif candidate != accept:
                    _LOGGER.info(
                        "Plan wurde als HTML statt als %s ausgeliefert - Format wird automatisch erkannt",
                        self.options.plan_format,
                    )
                    self._format_override = "auto"

                self.last_raw = text
                self.last_content_type = content_type
                self.last_url = final_url

                if _looks_like_login_page(final_url, text):
                    raise AuthError(
                        "Sitzung abgelaufen - IServ leitet auf die Anmeldeseite um"
                    )

                return text, content_type

        raise FetchError(
            "Plan-Abruf fehlgeschlagen - keiner der bekannten Plan-Pfade war abrufbar"
            + self._attempt_suffix(problems)
        )

    def _fallback_plan_urls(self) -> list[str]:
        """Alle bekannten Plan-Pfade ausser dem bereits versuchten."""
        configured = self.options.resolved_plan_url.rstrip("/").lower()
        urls: list[str] = []
        for path in PLAN_PATHS:
            url = self.options.site_root + path
            if url.rstrip("/").lower() == configured or url in urls:
                continue
            urls.append(url)
        return urls

    def _parse_dataset(
        self,
        text: str,
        content_type: str,
        default_year: int | None,
        fmt: str | None = None,
    ) -> PlanDataset:
        return parse_payload(
            text,
            fmt=fmt or self._format_override or self.options.plan_format,
            source=self.options.plan_url,
            klasse=self.options.klasse or None,
            default_year=default_year,
            content_type=content_type,
        )

    def _try_fallback(self, default_year: int | None) -> PlanDataset | None:
        """Probiert klassische Plan-Pfade, wenn ``plan_url`` keine Tabelle liefert.

        Die neue IServ-Web-App liefert unter ``/iserv/timetable`` nur eine
        JavaScript-Huelle. Die klassische Tabelle liegt oft weiterhin unter
        ``/iserv/plan`` - das wird hier einmalig getestet.
        """
        for url in self._fallback_plan_urls():
            if url in self._tried_urls:
                _LOGGER.debug("Ausweich-Pfad %s wurde bereits versucht", url)
                continue
            try:
                text, content_type, final_url = self._plan_request(url, ACCEPT_HTML)
            except (HTTPError, URLError, IservError) as err:
                _LOGGER.debug("Ausweich-Pfad %s nicht abrufbar: %s", url, err)
                continue
            if _looks_like_login_page(final_url, text):
                _LOGGER.debug("Ausweich-Pfad %s liefert die Anmeldeseite", url)
                continue
            dataset = self._parse_dataset(text, content_type, default_year, fmt="auto")
            if dataset.days:
                self.last_raw = text
                self.last_content_type = content_type
                self.last_url = final_url
                if self.options.plan_url:
                    _LOGGER.warning(
                        "plan_url lieferte keine Tabelle - Daten kommen von %s. "
                        "Tipp: plan_url leer lassen, dann sucht das Add-on selbst.",
                        url,
                    )
                else:
                    _LOGGER.info("Plantabelle automatisch gefunden: %s", url)
                return dataset
            export = self._try_calendar_export(text, final_url, default_year)
            if export is not None:
                return export
            _LOGGER.debug("Auch %s ohne Tabelle (%s)", url, _html_summary(text))
        return None

    def _try_calendar_export(
        self, page: str, page_url: str, default_year: int | None
    ) -> PlanDataset | None:
        """Nutzt den iCal-Export-Link der Planseite als Datenquelle.

        Die IServ-Stundenplanansicht bietet "Stundenplan exportieren" als iCal
        an. Dieser Export enthaelt Uhrzeiten und funktioniert damit auch dann,
        wenn die HTML-Ansicht nur ein JavaScript-Geruest oder ein Gitter ohne
        Datumsangaben zeigt.
        """
        for link in _calendar_links(page, page_url):
            if link in self._tried_urls:
                continue
            self._tried_urls.add(link)
            try:
                text, content_type, final_url = self._plan_request(link, ACCEPT_CALENDAR)
            except (HTTPError, URLError, IservError) as err:
                _LOGGER.debug("iCal-Export %s nicht abrufbar: %s", link, err)
                continue
            if detect_format(text, content_type) != "ics":
                _LOGGER.debug("iCal-Export %s lieferte kein iCal (%s)", link, content_type)
                continue
            dataset = parse_payload(
                text,
                fmt="ics",
                source=link,
                klasse=self.options.klasse or None,
                default_year=default_year,
            )
            if dataset.days:
                self.last_raw = text
                self.last_content_type = content_type
                self.last_url = final_url
                _LOGGER.info("Stundenplan aus dem iCal-Export gelesen: %s", link)
                return dataset
            _LOGGER.debug("iCal-Export %s enthielt keine Termine", link)
        return None

    def _portal_data_urls(self) -> list[str]:
        """Kandidaten der JSON-Schnittstelle der Stundenplanansicht.

        Der Browser ruft ``/iserv/timetable/data?filter={"startDate":"19.10.2026",
        "endDate":"25.10.2026"}`` auf. Der Filter wird hier fuer ein Fenster um
        heute gebaut (``TT.MM.JJJJ``), damit die Vorschau des Add-ons abgedeckt
        ist. Zusaetzlich wird die Adresse ohne Filter probiert - aeltere
        Installationen liefern die Woche dann ungefiltert.
        """
        base = _iserv_url(self.options.base_url, TIMETABLE_DATA_PATH)
        today = datetime.now(TZ).date()
        filter_json = json.dumps(
            {
                "startDate": (today - timedelta(days=PORTAL_DAYS_BACK)).strftime(
                    "%d.%m.%Y"
                ),
                "endDate": (today + timedelta(days=PORTAL_DAYS_AHEAD)).strftime(
                    "%d.%m.%Y"
                ),
            },
            separators=(",", ":"),
        )
        return [f"{base}?{urlencode({'filter': filter_json})}", base]

    def _portal_variants(self) -> list[tuple[str, str, dict, str]]:
        """Varianten des JSON-Abrufs: ``(Label, URL, Header, Accept)``.

        IServ-Instanzen reagieren unterschiedlich auf ``X-Requested-With``:
        Der Browser-Aufruf der Ansicht kommt je nach Version mit oder ohne
        diesem Header. Beide Formen werden probiert; ``Accept`` bleibt immer
        ``*/*`` (siehe :data:`ACCEPT_ANY`).
        """
        referer = self.last_url or self.options.resolved_plan_url
        xhr = {"X-Requested-With": "XMLHttpRequest"}
        if referer:
            xhr["Referer"] = referer
        plain = {"Referer": referer} if referer else {}
        variants: list[tuple[str, str, dict, str]] = []
        for position, url in enumerate(self._portal_data_urls()):
            label = "data?filter" if position == 0 else "data"
            variants.append((label, url, xhr, ACCEPT_ANY))
            if position == 0:
                variants.append((f"{label} ohne XHR", url, plain, ACCEPT_ANY))
        return variants

    def _try_portal_json(self, default_year: int | None) -> PlanDataset | None:
        """Liest den Plan aus der JSON-Schnittstelle der Web-Oberflaeche.

        Die HTML-Ansicht der neuen IServ-Oberflaeche enthaelt oft nur die
        Kopfzeilen der Wochentabelle - die Stunden laedt das Portal-JavaScript
        als JSON nach. Ohne diesen Weg sieht der Plan im Browser vollstaendig
        aus, das Add-on erhaelt aber keine einzige Zeile.

        Jeder Fehlversuch (HTTP-Status, Content-Type, Antwortanfang) landet in
        ``_portal_hint`` und damit in der Fehlermeldung - sonst sieht ein 401
        aus wie "keine Daten".
        """
        notes: list[str] = []
        seen: set[tuple[str, str]] = set()
        for label, url, headers, accept in self._portal_variants():
            key = (url, headers.get("X-Requested-With") or "")
            if key in seen:
                continue
            seen.add(key)
            self._tried_urls.add(url)
            try:
                text, content_type, final_url = self._plan_request(url, accept, headers)
            except HTTPError as err:
                body = ""
                try:
                    body = err.read().decode("utf-8", errors="replace")
                except (OSError, ValueError):  # pragma: no cover - Body nicht lesbar
                    body = ""
                notes.append(f"{label}: HTTP {err.code} {_short_body(body, 80)}".strip())
                _LOGGER.debug(
                    "IServ-JSON %s nicht abrufbar: HTTP %s %s", url, err.code, body[:200]
                )
                continue
            except (URLError, IservError) as err:
                notes.append(f"{label}: {err}")
                _LOGGER.debug("IServ-JSON %s nicht abrufbar: %s", url, err)
                continue
            if detect_format(text, content_type) != "json":
                notes.append(
                    f"{label}: {content_type or 'ohne Content-Type'} "
                    f"({len(text)} Zeichen: {_short_body(text, 60)})"
                )
                _LOGGER.debug(
                    "IServ-JSON %s lieferte kein JSON (Content-Type %s)", url, content_type
                )
                continue
            dataset = PlanDataset(source=url)
            try:
                dataset = self._parse_dataset(text, content_type, default_year, fmt="json")
            except (ValueError, TypeError):
                dataset = PlanDataset(source=url)
            if not dataset.days:
                dataset = parse_portal_json(
                    text, source=url, default_year=default_year
                )
            if dataset.days:
                self.last_raw = text
                self.last_content_type = content_type
                self.last_url = final_url
                _LOGGER.info(
                    "Stundenplan aus der IServ-JSON-Schnittstelle gelesen: %s", url
                )
                return dataset
            notes.append(f"{label}: JSON {json_summary(text)}")
            _LOGGER.debug("IServ-JSON %s ohne Stunden (%s)", url, notes[-1])
        if notes:
            self._portal_hint = "; ".join(notes)[:600]
        return None

    def fetch_dataset(self, default_year: int | None = None) -> PlanDataset:
        self._portal_hint = ""
        text, content_type = self.fetch_plan_text()
        dataset = self._parse_dataset(text, content_type, default_year)
        if dataset.days:
            return dataset

        export = self._try_calendar_export(
            text, self.last_url or self.options.resolved_plan_url, default_year
        )
        if export is not None:
            return export

        portal = self._try_portal_json(default_year)
        if portal is not None:
            return portal

        fallback = self._try_fallback(default_year)
        if fallback is not None:
            return fallback

        warnings = "; ".join(dataset.warnings) or "unbekanntes Format"
        checked = ", ".join(sorted(self._tried_urls)) or self.options.resolved_plan_url
        hint = f" [JSON: {self._portal_hint}]" if self._portal_hint else ""
        raise FetchError(
            "Plan enthielt keine verwertbaren Daten: "
            f"{warnings} [geprueft: {checked}]{hint} "
            f"[Antwort: {_payload_summary(self.last_raw, self.last_content_type)}]"
        )

