"""IServ-Login und Abruf des Stundenplans.

Der Login erfolgt formularbasiert ueber die IServ-Anmeldeseite
(``/iserv/auth/login``): Die Formularfelder (inkl. CSRF-Token) werden aus der
HTML-Seite gelesen, damit unterschiedliche IServ-Versionen funktionieren.
Der eigentliche Plan wird von der konfigurierten ``plan_url`` geladen - das
Format wird automatisch erkannt (ICS, JSON oder HTML-Tabelle).
"""

from __future__ import annotations

import logging
import re
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import (
    HTTPCookieProcessor,
    HTTPSHandler,
    Request,
    build_opener,
)

from .parsers import parse_payload
from .plan import PlanDataset

_LOGGER = logging.getLogger(__name__)
LOGIN_PATH = "/iserv/auth/login"
USER_AGENT = "HA-IServ-Addon/1.0"
# Wichtig: Die neue IServ-Web-App behandelt Anfragen, deren ``Accept``-Header
# ``application/json`` enthaelt, als API-Aufruf und antwortet ohne API-Token mit
# HTTP 401 - auch bei gueltiger Sitzung. Darum wird der Header je Endpunkt
# getrennt gesetzt (HTML / Kalender / JSON) und nie gemischt.
ACCEPT_HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_CALENDAR = "text/calendar,text/plain;q=0.9,*/*;q=0.8"
ACCEPT_JSON = "application/json,text/plain;q=0.9,*/*;q=0.8"
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
    plan_url: str
    username: str = ""
    password: str = ""
    plan_format: str = "auto"
    klasse: str = ""
    timeout: int = 30
    verify_ssl: bool = True

    @property
    def login_url(self) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", LOGIN_PATH.lstrip("/"))

    @property
    def resolved_plan_url(self) -> str:
        return urljoin(self.base_url.rstrip("/") + "/", self.plan_url)


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
            raise IservError(
                f"Loginseite nicht erreichbar (HTTP {err.code}) - iserv_url pruefen"
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
                    "Origin": self.options.base_url.rstrip("/"),
                },
            ) as response:
                self._read(response)
                final_url = response.geturl()
        except HTTPError as err:
            raise AuthError(
                f"Login fehlgeschlagen (HTTP {err.code}) - Benutzername/Passwort pruefen"
            ) from err
        except URLError as err:
            raise IservError(f"Login-Request fehlgeschlagen: {err.reason}") from err

        if re.search(r"/auth/login\b", final_url):
            raise AuthError("Login fehlgeschlagen - IServ zeigt erneut die Anmeldeseite")

        self._logged_in = True
        _LOGGER.info("IServ-Login erfolgreich (%s)", final_url)
        return True

    def _ensure_login(self) -> None:
        if self.options.username and not self._logged_in:
            self.login()

    def invalidate_session(self) -> None:
        """Erzwingt beim naechsten Abruf einen neuen Login."""
        self._logged_in = False

    def _plan_request(self, url: str, accept: str) -> tuple[str, str, str]:
        """Holt die Plan-URL mit einem bestimmten ``Accept``-Header."""
        with self._request(url, accept=accept) as response:
            text = self._read(response)
            return text, response.headers.get_content_type(), response.geturl()

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
        body = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        if body:
            details.append(body[:200])
        suffix = f" [{'; '.join(details)}]" if details else ""
        return AuthError(
            f"Zugriff auf den Plan verweigert (HTTP {err.code}) - Login/Rechte pruefen "
            f"(URL: {url}, Accept: {accept}){suffix}"
        )

    def fetch_plan_text(self) -> tuple[str, str]:
        if not self.options.plan_url:
            raise FetchError("plan_url ist nicht konfiguriert")
        self._ensure_login()

        url = self.options.resolved_plan_url
        accept = _accept_for(url, self.options.plan_format)
        # Manche IServ-Instanzen liefern fuer Kalender-/JSON-Pfade HTML aus -
        # deshalb bei 401/403 einmal mit Browser-Accept nachfassen.
        candidates = [accept] if accept == ACCEPT_HTML else [accept, ACCEPT_HTML]
        _LOGGER.debug("Plan-Abruf: %s (Accept: %s)", url, accept)

        text = content_type = final_url = ""
        self._format_override = None
        for index, candidate in enumerate(candidates):
            try:
                text, content_type, final_url = self._plan_request(url, candidate)
                if candidate != accept:
                    _LOGGER.info(
                        "Plan wurde als HTML statt als %s ausgeliefert - Format wird automatisch erkannt",
                        self.options.plan_format,
                    )
                    self._format_override = "auto"
                break
            except HTTPError as err:
                if err.code not in (401, 403):
                    raise FetchError(f"Plan-Abruf fehlgeschlagen (HTTP {err.code})") from err
                if index < len(candidates) - 1:
                    _LOGGER.warning(
                        "Plan-Abruf mit Accept '%s' abgelehnt (HTTP %s) - neuer Versuch mit Browser-Accept",
                        candidate,
                        err.code,
                    )
                    continue
                raise self._plan_error(err, url, candidate) from err
            except URLError as err:
                raise FetchError(f"Plan-Abruf fehlgeschlagen: {err.reason}") from err

        if re.search(r"/auth/login\b", final_url):
            raise AuthError("Sitzung abgelaufen - IServ leitet auf die Anmeldeseite um")

        self.last_raw = text
        self.last_content_type = content_type
        self.last_url = final_url
        return text, content_type

    def fetch_dataset(self, default_year: int | None = None) -> PlanDataset:
        text, content_type = self.fetch_plan_text()
        dataset = parse_payload(
            text,
            fmt=self._format_override or self.options.plan_format,
            source=self.options.plan_url,
            klasse=self.options.klasse or None,
            default_year=default_year,
            content_type=content_type,
        )
        if not dataset.days:
            raise FetchError(
                "Plan enthielt keine verwertbaren Daten: "
                + ("; ".join(dataset.warnings) or "unbekanntes Format")
            )
        return dataset

