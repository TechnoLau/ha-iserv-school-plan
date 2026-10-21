"""Tests fuer den IServ-Client (Login-Flow gegen einen lokalen Testserver)."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

import app.iserv as iserv_module
from app.iserv import (
    ACCEPT_CALENDAR,
    ACCEPT_HTML,
    ACCEPT_JSON,
    PORTAL_FILTER_WILDCARD,
    AuthError,
    FetchError,
    IservClient,
    IservError,
    IservOptions,
    _LoginFormParser,
    _accept_for,
    _auth_redirect_target,
    _calendar_links,
    _clean_base_url,
    _detect_fields,
    _html_summary,
    _looks_like_login_page,
    _meta_refresh_target,
    _origin,
)

LOGIN_PAGE = """
<html><body>
<form action="/iserv/auth/login" method="post">
  <input type="hidden" name="_csrf" value="token123">
  <input type="text" name="_username" value="">
  <input type="password" name="_password" value="">
  <button type="submit" name="submit" value="Anmelden">Anmelden</button>
</form>
</body></html>
"""

PLAN_PAGE = """
<html><body>
<h2>Montag, 19.10.2026</h2>
<table>
  <tr><th>Stunde</th><th>Zeit</th><th>Fach</th><th>Raum</th><th>Art</th></tr>
  <tr><td>1</td><td>07:45 - 08:30</td><td>Mathematik</td><td>A1</td><td></td></tr>
  <tr><td>2</td><td>08:35 - 09:20</td><td>Deutsch</td><td>B2</td><td>faellt aus</td></tr>
</table>
</body></html>
"""


SEEN_ACCEPTS: list[str] = []

SPA_PAGE = """
<html><head><title>IServ</title></head><body>
<div id="app"></div>
<script src="/iserv/app/static/js/main.1a2b3c.js"></script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(
        self,
        status: int,
        body: str,
        content_type: str = "text/html; charset=utf-8",
        extra_headers: dict | None = None,
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path in ("/iserv/auth/login", "/iserv/"):
            self._send(200, LOGIN_PAGE)
        elif self.path == "/plan":
            if "session=ok" not in (self.headers.get("Cookie") or ""):
                self.send_response(302)
                self.send_header("Location", "/iserv/auth/login")
                self.end_headers()
                return
            self._send(200, PLAN_PAGE)
        elif self.path == "/timetable":
            # Neue IServ-Web-App: HTML-Huelle ohne Tabellen.
            if "session=ok" not in (self.headers.get("Cookie") or ""):
                self.send_response(302)
                self.send_header("Location", "/iserv/auth/login")
                self.end_headers()
                return
            self._send(200, SPA_PAGE)
        elif self.path == "/iserv/plan":
            # Klassische Plantabelle als Ausweichziel.
            if "session=ok" not in (self.headers.get("Cookie") or ""):
                self.send_response(302)
                self.send_header("Location", "/iserv/auth/login")
                self.end_headers()
                return
            self._send(200, PLAN_PAGE)
        elif self.path == "/plan-api":
            # Verhaelt sich wie eine neue IServ-Instanz: ``application/json`` im
            # Accept-Header wird als API-Aufruf ohne Token mit 401 quittiert.
            accept = self.headers.get("Accept") or ""
            SEEN_ACCEPTS.append(accept)
            if "session=ok" not in (self.headers.get("Cookie") or ""):
                self._send(401, "keine Sitzung", extra_headers={"WWW-Authenticate": 'Bearer realm="iserv"'})
            elif "application/json" in accept:
                self._send(401, '{"error":"unauthorized"}', "application/json")
            else:
                self._send(200, PLAN_PAGE)
        elif self.path == "/plan-noauth":
            self._send(
                401,
                '{"error":"token required"}',
                "application/json",
                {"WWW-Authenticate": 'Basic realm="iserv"'},
            )
        else:
            self._send(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        data = parse_qs(self.rfile.read(length).decode("utf-8"))
        valid = (
            data.get("_username") == ["test"]
            and data.get("_password") == ["geheim"]
            and data.get("_csrf") == ["token123"]
        )
        if valid:
            self.send_response(302)
            self.send_header("Location", "/iserv/")
            self.send_header("Set-Cookie", "session=ok; Path=/")
            self.end_headers()
        else:
            self._send(200, LOGIN_PAGE)

    def log_message(self, *args) -> None:  # Testausgabe ruhig halten
        return


MAINTENANCE_PAGE = """
<!-- maintenance mode template for IServ -->
<html lang="de"><head><title>Wartungsarbeiten / Maintenance</title></head>
<body><h1>Wartungsarbeiten</h1>
<p>Auf diesem IServ finden gerade Wartungsarbeiten statt.</p></body></html>
"""


class MaintenanceHandler(BaseHTTPRequestHandler):
    """Bildet den IServ-Wartungsmodus nach (eigener Webserver, HTTP 503)."""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        payload = MAINTENANCE_PAGE.encode("utf-8")
        self.send_response(503)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-IServ-Maintenance", "1")
        self.send_header("Server", "IServ Maintenance Web Server")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.do_GET()

    def log_message(self, *args) -> None:
        return


@pytest.fixture()
def maintenance_url() -> str:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MaintenanceHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def server_url() -> str:
    SEEN_ACCEPTS.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- Neue IServ-Version: Login endet auf einer OIDC-Zwischenseite ------------

OIDC_LOGIN_PAGE = """
<html><body>
<form id="login-form" action="/iserv/auth/login" method="post">
  <input type="hidden" name="_csrf" value="token123">
  <input type="text" name="_username" value="">
  <input type="password" name="_password" value="">
</form>
</body></html>
"""

OIDC_HOME_PAGE = "<html><body><h1>Startseite</h1></body></html>"

OIDC_META_PAGE = """
<html><head>
<meta http-equiv="refresh" content="0;url='/iserv/app/authentication/redirect?code=meta123'">
<script type="text/javascript" src="/iserv/auth/static/js/redirect.b6702d62.js"></script>
<title>IServ - ebg-kiel.eu</title></head>
<body><div id="redirect-text" class="hidden">Sie werden weitergeleitet ...</div></body></html>
"""

OIDC_JS_PAGE = """
<html><head>
<script type="text/javascript" src="/iserv/auth/static/js/redirect.b6702d62.js"></script>
<script>window.location.replace("/iserv/app/authentication/redirect?code=js123");</script>
<title>IServ - ebg-kiel.eu</title></head>
<body><div id="redirect-text" class="hidden">Sie werden weitergeleitet ...</div></body></html>
"""

OIDC_STUCK_PAGE = """
<html><head>
<script type="text/javascript" src="/iserv/auth/static/js/redirect.b6702d62.js"></script>
</head><body><div id="redirect-text">Bitte warten ...</div></body></html>
"""

OIDC_MODE = "meta"
OIDC_HITS: list[str] = []


class OidcHandler(BaseHTTPRequestHandler):
    """Bildet den IServ-Login der neuen Web-Oberflaeche nach (OIDC-Kette)."""

    def _send(
        self,
        status: int,
        body: str,
        content_type: str = "text/html; charset=utf-8",
        extra_headers: dict | None = None,
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _has_session(self) -> bool:
        return "iserv_session=ok" in (self.headers.get("Cookie") or "")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        OIDC_HITS.append(self.path)
        if self.path in ("/iserv/auth/login", "/iserv/"):
            self._send(200, OIDC_LOGIN_PAGE)
        elif self.path.startswith("/iserv/auth/home"):
            self._send(200, OIDC_HOME_PAGE)
        elif self.path.startswith("/iserv/plan"):
            if not self._has_session():
                self._redirect("/iserv/auth/auth?_iserv_app_url=%2Fiserv%2Fplan&client_id=abc")
                return
            self._send(200, PLAN_PAGE)
        elif self.path.startswith("/iserv/auth/auth"):
            page = {
                "meta": OIDC_META_PAGE,
                "js": OIDC_JS_PAGE,
                "stuck": OIDC_STUCK_PAGE,
            }[OIDC_MODE]
            self._send(200, page)
        elif self.path.startswith("/iserv/app/authentication/redirect"):
            # Erst dieser Aufruf setzt die Sitzung der Web-Oberflaeche.
            self._redirect("/iserv/plan", cookie="iserv_session=ok; Path=/")
        else:
            self._send(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        data = parse_qs(self.rfile.read(length).decode("utf-8"))
        valid = (
            data.get("_username") == ["test"]
            and data.get("_password") == ["geheim"]
            and data.get("_csrf") == ["token123"]
        )
        if valid:
            self._redirect("/iserv/auth/auth?_iserv_app_url=%2Fiserv%2Fplan", "classic=ok; Path=/")
        else:
            self._send(200, OIDC_LOGIN_PAGE)

    def log_message(self, *args) -> None:
        return


@pytest.fixture()
def oidc_url() -> str:
    global OIDC_MODE
    OIDC_MODE = "meta"
    OIDC_HITS.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), OidcHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_login_form_detection() -> None:
    parser = _LoginFormParser()
    parser.feed(LOGIN_PAGE)
    form = parser.login_form()
    assert form is not None
    username, password, constants = _detect_fields(form)
    assert username == "_username"
    assert password == "_password"
    assert constants == {"_csrf": "token123"}


def test_urls_are_resolved() -> None:
    options = IservOptions(base_url="https://ebg.kiel.eu/", plan_url="/iserv/plan/export")
    assert options.login_url == "https://ebg.kiel.eu/iserv/auth/login"
    assert options.resolved_plan_url == "https://ebg.kiel.eu/iserv/plan/export"


def test_base_url_with_iserv_suffix_is_normalized() -> None:
    # Nutzer tragen die Adresse oft als ".../iserv" ein - daraus darf kein
    # doppelter Pfad ".../iserv/iserv/auth/login" werden.
    options = IservOptions(base_url="https://ebg.kiel.eu/iserv/", plan_url="/iserv/plan")
    assert options.login_url == "https://ebg.kiel.eu/iserv/auth/login"
    assert options.site_root == "https://ebg.kiel.eu"
    assert options.resolved_plan_url == "https://ebg.kiel.eu/iserv/plan"


def test_plan_url_is_optional_and_defaults_to_classic_path() -> None:
    # Nur die IServ-Adresse angeben - der Planpfad wird automatisch gesetzt.
    assert (
        IservOptions(base_url="https://ebg.kiel.eu", plan_url="").resolved_plan_url
        == "https://ebg.kiel.eu/iserv/plan"
    )
    assert (
        IservOptions(base_url="https://ebg.kiel.eu/iserv", plan_url="").resolved_plan_url
        == "https://ebg.kiel.eu/iserv/plan"
    )


def test_relative_plan_url_is_placed_under_iserv() -> None:
    options = IservOptions(base_url="https://ebg.kiel.eu", plan_url="plan")
    assert options.resolved_plan_url == "https://ebg.kiel.eu/iserv/plan"
    exported = IservOptions(base_url="https://ebg.kiel.eu", plan_url="https://ebg.kiel.eu/plan/export.ics")
    assert exported.resolved_plan_url == "https://ebg.kiel.eu/plan/export.ics"


def test_fetch_without_plan_url_uses_default_path(server_url: str) -> None:
    options = IservOptions(base_url=server_url, username="test", password="geheim")
    client = IservClient(options)
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days[date(2026, 10, 19)].lessons
    assert client.last_url.endswith("/iserv/plan")


def test_maintenance_mode_is_reported(maintenance_url: str) -> None:
    options = IservOptions(
        base_url=maintenance_url,
        username="test",
        password="geheim",
    )
    client = IservClient(options)
    with pytest.raises(IservError) as error:
        client.fetch_dataset(default_year=2026)
    assert "Wartungsmodus" in str(error.value)


def test_maintenance_mode_on_plan_request_is_reported(maintenance_url: str) -> None:
    client = IservClient(IservOptions(base_url=maintenance_url))
    with pytest.raises(FetchError) as error:
        client.fetch_dataset(default_year=2026)
    assert "Wartungsmodus" in str(error.value)


def test_fetch_falls_back_to_classic_plan_path(server_url: str) -> None:
    """plan_url zeigt auf die JS-Web-App - das Add-on nutzt /iserv/plan."""
    options = IservOptions(
        base_url=server_url + "/iserv",
        plan_url="/timetable",
        username="test",
        password="geheim",
    )
    client = IservClient(options)
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days[date(2026, 10, 19)].lessons
    assert client.last_url.endswith("/iserv/plan")


def test_web_app_without_fallback_reports_response_summary(server_url: str) -> None:
    options = IservOptions(
        base_url=server_url,
        plan_url="/timetable",
        username="test",
        password="geheim",
    )
    client = IservClient(options)
    client._fallback_plan_urls = lambda: []  # type: ignore[assignment]
    with pytest.raises(FetchError) as error:
        client.fetch_dataset(default_year=2026)
    message = str(error.value)
    assert "keine <table>" in message
    assert "main.1a2b3c.js" in message


def test_login_waiting_time_is_reported() -> None:
    page = '<html><body><div id="waiting-time" data-seconds="30"></div></body></html>'
    message = IservClient._login_failure_message(page)
    assert "30 Sekunden" in message
    assert "Brute-Force-Schutz" in message


def test_login_alert_text_is_reported() -> None:
    page = (
        '<html><body><div class="alert alert-danger">'
        "Ungueltige Anmeldedaten</div></body></html>"
    )
    message = IservClient._login_failure_message(page)
    assert "Ungueltige Anmeldedaten" in message


def test_login_page_in_body_is_detected() -> None:
    real_login = '<html><body><form id="login-form" method="post"></form></body></html>'
    assert _looks_like_login_page("https://schule.de/iserv/plan", real_login) is True
    assert _looks_like_login_page("https://schule.de/iserv/plan", PLAN_PAGE) is False


def test_fetch_html_plan_with_login(server_url: str) -> None:
    options = IservOptions(
        base_url=server_url,
        plan_url="/plan",
        username="test",
        password="geheim",
    )
    client = IservClient(options)
    dataset = client.fetch_dataset(default_year=2026)
    day = dataset.days[date(2026, 10, 19)]
    assert day.lessons[0].start == time(7, 45)
    assert day.lessons[1].cancelled is True
    assert day.held_lessons == [day.lessons[0]]
    assert client.last_content_type == "text/html"


def test_login_rejects_wrong_password(server_url: str) -> None:
    options = IservOptions(
        base_url=server_url,
        plan_url="/plan",
        username="test",
        password="falsch",
    )
    client = IservClient(options)
    with pytest.raises(AuthError):
        client.fetch_dataset(default_year=2026)


def test_missing_login_redirects_to_auth_error(server_url: str) -> None:
    options = IservOptions(base_url=server_url, plan_url="/plan")
    client = IservClient(options)
    with pytest.raises(AuthError):
        client.fetch_dataset(default_year=2026)


def test_accept_header_is_chosen_per_format() -> None:
    assert _accept_for("https://schule.de/iserv/plan") == ACCEPT_HTML
    assert _accept_for("https://schule.de/iserv/plan/", plan_format="auto") == ACCEPT_HTML
    assert _accept_for("https://schule.de/iserv/plan/export/plan.ics") == ACCEPT_CALENDAR
    assert _accept_for("https://schule.de/iserv/plan/export", plan_format="ics") == ACCEPT_CALENDAR
    assert _accept_for("https://schule.de/iserv/plan/api/plan", plan_format="json") == ACCEPT_JSON
    assert _accept_for("https://schule.de/iserv/plan/plan.json") == ACCEPT_JSON


def test_html_and_calendar_accept_never_request_json() -> None:
    # Regression: IServ (neue Web-App) antwortet mit HTTP 401, sobald
    # ``application/json`` im Accept-Header steht - auch bei gueltiger Sitzung.
    assert "application/json" not in ACCEPT_HTML
    assert "application/json" not in ACCEPT_CALENDAR


def test_json_plan_falls_back_to_html_accept(server_url: str) -> None:
    options = IservOptions(
        base_url=server_url,
        plan_url="/plan-api",
        username="test",
        password="geheim",
        plan_format="json",
    )
    client = IservClient(options)
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days[date(2026, 10, 19)].lessons
    assert len(SEEN_ACCEPTS) == 2
    assert "application/json" in SEEN_ACCEPTS[0]
    assert "application/json" not in SEEN_ACCEPTS[1]
    assert client.last_content_type == "text/html"


def test_unauthorized_plan_reports_details(server_url: str) -> None:
    options = IservOptions(base_url=server_url, plan_url="/plan-noauth")
    client = IservClient(options)
    with pytest.raises(AuthError) as info:
        client.fetch_dataset(default_year=2026)
    message = str(info.value)
    assert "401" in message
    assert "/plan-noauth" in message
    assert "WWW-Authenticate" in message
    assert "token required" in message


# --- OIDC-Login der neuen IServ-Version -------------------------------------


def test_login_follows_oidc_redirect_page(oidc_url: str) -> None:
    """Login endet auf der Auth-Zwischenseite (Meta-Refresh) - sie wird ausgefuehrt."""
    client = IservClient(
        IservOptions(base_url=oidc_url, username="test", password="geheim")
    )
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days[date(2026, 10, 19)].lessons
    assert client.last_url.endswith("/iserv/plan")
    assert any("authentication/redirect" in hit for hit in OIDC_HITS)


def test_login_follows_oidc_redirect_from_script(oidc_url: str) -> None:
    global OIDC_MODE
    OIDC_MODE = "js"
    client = IservClient(
        IservOptions(base_url=oidc_url, username="test", password="geheim")
    )
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days[date(2026, 10, 19)].lessons
    assert client.last_url.endswith("/iserv/plan")


def test_stuck_oidc_redirect_reports_auth_error(oidc_url: str) -> None:
    global OIDC_MODE
    OIDC_MODE = "stuck"
    client = IservClient(
        IservOptions(base_url=oidc_url, username="test", password="geheim")
    )
    with pytest.raises(AuthError) as error:
        client.fetch_dataset(default_year=2026)
    assert "Sitzung wurde nicht uebernommen" in str(error.value)


def test_plan_page_auth_redirect_is_completed(oidc_url: str) -> None:
    """Ohne Sitzung der Web-Oberflaeche antwortet der Plan mit der Auth-Seite."""
    client = IservClient(
        IservOptions(base_url=oidc_url, username="test", password="geheim")
    )
    client.login()
    for cookie in list(client._jar):
        if cookie.name == "iserv_session":
            client._jar.clear(cookie.domain, cookie.path, cookie.name)
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days[date(2026, 10, 19)].lessons
    assert client.last_url.endswith("/iserv/plan")


def test_meta_refresh_variants_are_parsed() -> None:
    quoted = (
        '<meta http-equiv="refresh" '
        "content=\"0;url='/iserv/app/authentication/redirect?code=abc'\">"
    )
    assert _meta_refresh_target(quoted, "https://schule.de/x") == (
        "https://schule.de/iserv/app/authentication/redirect?code=abc"
    )
    plain = '<meta http-equiv="refresh" content="5; /iserv/auth/logout?finished=1">'
    assert _meta_refresh_target(plain, "https://schule.de/iserv/") == (
        "https://schule.de/iserv/auth/logout?finished=1"
    )
    escaped = (
        '<meta http-equiv="refresh" '
        'content="0;url=/iserv/app/authentication/redirect?code=abc&amp;state=xyz">'
    )
    assert _meta_refresh_target(escaped, "https://schule.de/iserv/auth/auth") == (
        "https://schule.de/iserv/app/authentication/redirect?code=abc&state=xyz"
    )
    assert _meta_refresh_target("<html><body>nichts</body></html>", "https://schule.de") == ""


def test_auth_redirect_target_from_script() -> None:
    page = '<html><script>window.location.href = "/iserv/app/authentication/redirect?code=x";</script></html>'
    assert _auth_redirect_target(page, "https://schule.de/iserv/auth/auth") == (
        "https://schule.de/iserv/app/authentication/redirect?code=x"
    )


def test_base_url_with_module_path_is_normalized() -> None:
    # Nutzer tragen auch die Planseite selbst als iserv_url ein.
    assert _clean_base_url("https://ebg-kiel.eu/iserv/timetable") == "https://ebg-kiel.eu/iserv"
    assert _clean_base_url("https://ebg-kiel.eu/iserv/plan/") == "https://ebg-kiel.eu/iserv"
    assert _clean_base_url("https://ebg-kiel.eu/iserv") == "https://ebg-kiel.eu/iserv"
    assert _clean_base_url("https://ebg-kiel.eu") == "https://ebg-kiel.eu"
    assert _clean_base_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    options = IservOptions(base_url="https://ebg-kiel.eu/iserv/timetable", plan_url="")
    assert options.login_url == "https://ebg-kiel.eu/iserv/auth/login"
    assert options.resolved_plan_url == "https://ebg-kiel.eu/iserv/plan"



# --- Fehlender Planpfad (HTTP 404) ------------------------------------------

MISSING_HITS: list[str] = []


class MissingPathHandler(BaseHTTPRequestHandler):
    """IServ, bei dem der klassische Planpfad nicht (mehr) existiert.

    Die eine IServ-Instanz liefert die Plantabelle unter ``/iserv/plan``, die
    andere unter ``/iserv/timetable``. Fehlt ein Pfad, antwortet IServ mit 404.
    """

    missing: tuple[str, ...] = ("/iserv/plan",)
    login_status: int = 200

    def _send(self, status: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _has_session(self) -> bool:
        return "session=ok" in (self.headers.get("Cookie") or "")

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        MISSING_HITS.append(self.path)
        if self.path.startswith("/iserv/auth/login"):
            self._send(self.login_status, LOGIN_PAGE if self.login_status == 200 else "not found")
        elif self.path in self.missing:
            self._send(404, "not found")
        elif self.path in ("/iserv/", "/iserv"):
            self._send(200, "<html><body><h1>Startseite</h1></body></html>")
        elif self.path == "/iserv/timetable":
            if not self._has_session():
                self.send_response(302)
                self.send_header("Location", "/iserv/auth/login")
                self.end_headers()
                return
            self._send(200, PLAN_PAGE)
        else:
            # /iserv/plan und alles andere: Pfad existiert nicht.
            self._send(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        data = parse_qs(self.rfile.read(length).decode("utf-8"))
        valid = (
            data.get("_username") == ["test"]
            and data.get("_password") == ["geheim"]
            and data.get("_csrf") == ["token123"]
        )
        if not valid:
            self._send(200, LOGIN_PAGE)
            return
        self.send_response(302)
        self.send_header("Location", "/iserv/")
        self.send_header("Set-Cookie", "session=ok; Path=/")
        self.end_headers()

    def log_message(self, *args) -> None:
        return


@pytest.fixture()
def missing_path_url():
    """Startet Testserver mit waehlbaren fehlenden Pfaden (404)."""
    servers: list[ThreadingHTTPServer] = []

    def start(
        missing: tuple[str, ...] = ("/iserv/plan",), login_status: int = 200
    ) -> str:
        handler = type(
            "MissingPathHandlerInstance",
            (MissingPathHandler,),
            {"missing": missing, "login_status": login_status},
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    MISSING_HITS.clear()
    yield start
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()


def test_missing_plan_path_falls_back_to_next_known_path(missing_path_url) -> None:
    """Regression: HTTP 404 auf /iserv/plan brach den Abruf komplett ab."""
    base = missing_path_url()
    client = IservClient(
        IservOptions(base_url=base, username="test", password="geheim")
    )
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days[date(2026, 10, 19)].lessons
    assert client.last_url.endswith("/iserv/timetable")
    assert "/iserv/plan" in MISSING_HITS
    assert "/iserv/timetable" in MISSING_HITS


def test_all_missing_plan_paths_report_attempted_urls(missing_path_url) -> None:
    base = missing_path_url(missing=("/iserv/plan", "/iserv/timetable"))
    client = IservClient(
        IservOptions(base_url=base, username="test", password="geheim")
    )
    with pytest.raises(FetchError) as error:
        client.fetch_dataset(default_year=2026)
    message = str(error.value)
    assert "HTTP 404" in message
    assert "/iserv/plan" in message
    assert "/iserv/timetable" in message


def test_ics_format_still_accepts_html_fallback(missing_path_url) -> None:
    """plan_format ics darf den HTML-Ausweichpfad nicht unlesbar machen."""
    base = missing_path_url()
    client = IservClient(
        IservOptions(
            base_url=base,
            plan_url="/iserv/plan",
            plan_format="ics",
            username="test",
            password="geheim",
        )
    )
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days[date(2026, 10, 19)].lessons
    assert client.last_url.endswith("/iserv/timetable")


def test_missing_login_page_reports_url(missing_path_url) -> None:
    base = missing_path_url(login_status=404)
    client = IservClient(IservOptions(base_url=base, username="test", password="geheim"))
    with pytest.raises(IservError) as error:
        client.login()
    message = str(error.value)
    assert "HTTP 404" in message
    assert "/iserv/auth/login" in message


def test_origin_header_uses_scheme_and_host() -> None:
    assert _origin("https://schule.de/iserv") == "https://schule.de"
    assert _origin("https://schule.de/iserv/timetable") == "https://schule.de"
    assert _origin("http://127.0.0.1:8080/iserv") == "http://127.0.0.1:8080"


# --- iCal-Export der IServ-Stundenplanansicht --------------------------------

GRID_WITHOUT_DATE_PAGE = """
<html><head><title>Stundenplan - IServ</title></head><body>
<table>
  <tr><th>Stunde</th><th>Montag</th><th>Dienstag</th></tr>
  <tr><td>1</td><td>Mathematik</td><td>Deutsch</td></tr>
</table>
<a href="/iserv/timetable/export/abc.ics">Stundenplan exportieren (iCal)</a>
</body></html>
"""

EXPORT_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//IServ//Stundenplan//DE
BEGIN:VEVENT
UID:1@iserv
DTSTART;TZID=Europe/Berlin:20261019T074500
DTEND;TZID=Europe/Berlin:20261019T083000
SUMMARY:Mathematik
LOCATION:A1
END:VEVENT
END:VCALENDAR
"""

EXPORT_HITS: list[str] = []


class ExportHandler(BaseHTTPRequestHandler):
    """Testserver: /iserv/plan fehlt, /iserv/timetable bietet einen iCal-Export."""

    def _send(
        self, status: int, body: str, content_type: str = "text/html; charset=utf-8"
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        EXPORT_HITS.append(self.path)
        if self.path in ("/iserv/auth/login", "/iserv/"):
            self._send(200, LOGIN_PAGE)
        elif self.path == "/iserv/plan":
            self._send(404, "not found")
        elif self.path == "/iserv/timetable":
            self._send(200, GRID_WITHOUT_DATE_PAGE)
        elif self.path == "/iserv/timetable/export/abc.ics":
            self._send(200, EXPORT_ICS, "text/calendar; charset=utf-8")
        else:
            self._send(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        data = parse_qs(self.rfile.read(length).decode("utf-8"))
        valid = (
            data.get("_username") == ["test"]
            and data.get("_password") == ["geheim"]
            and data.get("_csrf") == ["token123"]
        )
        if not valid:
            self._send(200, LOGIN_PAGE)
            return
        self.send_response(302)
        self.send_header("Location", "/iserv/")
        self.send_header("Set-Cookie", "session=ok; Path=/")
        self.end_headers()

    def log_message(self, *args) -> None:
        return


@pytest.fixture()
def export_url():
    """Startet den Testserver mit iCal-Export-Link."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ExportHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    EXPORT_HITS.clear()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_calendar_export_link_is_used_when_page_has_no_rows(export_url: str) -> None:
    """Ohne Datum im Wochengitter liefert der iCal-Export die Planzeiten."""
    client = IservClient(
        IservOptions(base_url=export_url, username="test", password="geheim")
    )
    dataset = client.fetch_dataset(default_year=2026)
    lesson = dataset.days[date(2026, 10, 19)].lessons[0]
    assert lesson.subject == "Mathematik"
    assert lesson.start == time(7, 45)
    assert client.last_url.endswith("/iserv/timetable/export/abc.ics")
    assert "/iserv/timetable/export/abc.ics" in EXPORT_HITS


def test_calendar_links_ignore_foreign_hosts_and_other_links() -> None:
    html = (
        '<a href="/iserv/timetable/export/x.ics">iCal</a>'
        '<a href="https://fremd.example/x.ics">fremd</a>'
        '<a href="/iserv/auth/logout">Abmelden</a>'
    )
    assert _calendar_links(html, "https://schule.de/iserv/timetable") == [
        "https://schule.de/iserv/timetable/export/x.ics"
    ]


def test_html_summary_reports_table_structure() -> None:
    summary = _html_summary(
        "<table><tr><th>Montag<br>19.10.</th><th>Dienstag<br>20.10.</th></tr>"
        "<tr><td>Mathematik</td><td>Deutsch</td></tr></table>"
    )
    assert "Wochengitter mit Datum" in summary
    assert "Montag" in summary


# --- JSON-Schnittstelle der neuen Stundenplanansicht ------------------------

PORTAL_PAGE = """
<html><head><title>Stundenplan - IServ</title></head><body>
<table>
  <tr><th>Std.</th><th>Montag</th><th>Dienstag</th><th>Mittwoch</th>
      <th>Donnerstag</th><th>Freitag</th></tr>
</table>
<script src="/iserv/static/js/portal.js"></script>
</body></html>
"""

PORTAL_JSON = json.dumps(
    {
        "data": [
            {
                "date": "2026-10-19",
                "start": "07:45",
                "end": "08:30",
                "subject": "Mathematik",
                "room": "A1",
                "period": 1,
            }
        ]
    }
)

PORTAL_STATE: dict[str, object] = {
    "body": PORTAL_JSON,
    "deny": False,
    "echo": False,
    # Simuliert eine Instanz, die den Filter des Add-ons ablehnt (Zustand 1.0.11).
    "reject": False,
    # Simuliert eine Instanz, die nur mit Platzhalter-Filter Stunden liefert.
    "wildcard_only": False,
    # Simuliert eine Instanz ohne ``/iserv/timetable/data`` (nur Bindestrich-Pfad).
    "alias_only": False,
    "filter_error": "Fehler beim Verarbeiten der Anfrage",
}
PORTAL_HITS: list[tuple[str, str, str]] = []
# Filter, die der Testserver als gueltig akzeptiert hat (in Reihenfolge).
PORTAL_FILTERS: list[dict] = []


def iserv_error(text: str) -> str:
    """Fehlerantwort des Stundenplanmoduls (echter Aufbau)."""
    return json.dumps(
        {"status": "error", "messages": [{"type": "error", "text": text}]}
    )


# So antwortet das IServ-Modul, wenn der Filter nicht passt (echter Wortlaut).
ISERV_BAD_REQUEST = iserv_error("Fehler beim Verarbeiten der Anfrage")
# Wortlaut aus dem Fehlerbericht zu 1.0.11: ohne ``classes`` keine Antwort.
ISERV_MISSING_CLASSES = iserv_error("Die erforderliche Eigenschaft classes fehlt.")


class PortalHandler(BaseHTTPRequestHandler):
    """Testserver: HTML nur Geruest, Stunden kommen aus ``timetable/data``.

    Der Server verhaelt sich wie IServ:

    * ``Accept`` mit ``application/json`` gilt als API-Aufruf ohne Token
      (HTTP 401), unabhaengig von der Sitzung.
    * ``timetable/data`` verlangt einen ``filter`` und akzeptiert darin nur
      genau eine Woche (Montag bis Sonntag) - ein groesserer Zeitraum wird mit
      HTTP 400 und „Fehler beim Verarbeiten der Anfrage“ abgewiesen.
    """

    def _send(
        self, status: int, body: str, content_type: str = "text/html; charset=utf-8"
    ) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _filter_window(self) -> tuple[date, date] | None:
        """Wochenfilter wie IServ pruefen - ``None`` heisst HTTP 400.

        Wie das echte Modul verlangt der Server ``classes``, ``teachers`` und
        ``rooms`` im Filter. Fehlt eine davon, antwortet er mit „Die
        erforderliche Eigenschaft classes fehlt.“ - genau der Fehler, den
        1.0.11 mit einem Filter aus nur startDate/endDate ausgeloest hat.
        """
        if PORTAL_STATE["reject"]:
            PORTAL_STATE["filter_error"] = "Die erforderliche Eigenschaft classes fehlt."
            return None
        query = parse_qs(urlsplit(self.path).query)
        raw = query.get("filter")
        if not raw:
            PORTAL_STATE["filter_error"] = "Die erforderliche Eigenschaft filter fehlt."
            return None
        try:
            payload = json.loads(raw[0])
        except (TypeError, ValueError):
            PORTAL_STATE["filter_error"] = "Fehler beim Verarbeiten der Anfrage"
            return None
        for key in ("classes", "teachers", "rooms"):
            if key not in payload:
                PORTAL_STATE["filter_error"] = (
                    f"Die erforderliche Eigenschaft {key} fehlt."
                )
                return None
        try:
            start = datetime.strptime(payload["startDate"], "%d.%m.%Y").date()
            end = datetime.strptime(payload["endDate"], "%d.%m.%Y").date()
        except (KeyError, TypeError, ValueError):
            PORTAL_STATE["filter_error"] = "Fehler beim Verarbeiten der Anfrage"
            return None
        if start.weekday() != 0 or (end - start).days != 6:
            PORTAL_STATE["filter_error"] = "Fehler beim Verarbeiten der Anfrage"
            return None
        PORTAL_STATE["filter_error"] = "Fehler beim Verarbeiten der Anfrage"
        PORTAL_FILTERS.append(payload)
        return start, end

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        accept = self.headers.get("Accept") or ""
        PORTAL_HITS.append(
            (self.path, self.headers.get("X-Requested-With") or "", accept)
        )
        data_paths = ("/iserv/timetable/data", "/iserv/time-table/data")
        if PORTAL_STATE["deny"] and self.path.startswith(data_paths):
            self._send(401, "{}", "application/json; charset=utf-8")
            return
        if PORTAL_STATE["alias_only"] and self.path.startswith(data_paths[0]):
            self._send(404, "not found")
            return
        if "application/json" in accept.lower():
            self._send(
                401,
                json.dumps({"message": "API token required"}),
                "application/json; charset=utf-8",
            )
            return
        if self.path in ("/iserv/auth/login", "/iserv/"):
            self._send(200, LOGIN_PAGE)
        elif self.path == "/iserv/plan":
            self._send(404, "not found")
        elif self.path == "/iserv/timetable":
            self._send(200, PORTAL_PAGE)
        elif self.path.startswith(data_paths):
            window = self._filter_window()
            if window is None:
                self._send(
                    400,
                    iserv_error(str(PORTAL_STATE["filter_error"])),
                    "application/json; charset=utf-8",
                )
                return
            payload = PORTAL_FILTERS[-1] if PORTAL_FILTERS else {}
            if PORTAL_STATE["wildcard_only"] and payload.get("classes") != ["%"]:
                # Leere Klassenliste ist gueltig, liefert aber keine Stunden -
                # so verhaelt sich manch eine Instanz.
                self._send(
                    200,
                    json.dumps({"data": {"timetable": [], "plain-timetable": []}}),
                    "application/json; charset=utf-8",
                )
                return
            body = str(PORTAL_STATE["body"])
            if PORTAL_STATE["echo"]:
                # Jede Woche eine eigene Stunde - so laesst sich das
                # Zusammenfassen der Wochen pruefen.
                body = json.dumps(
                    {
                        "data": [
                            {
                                "date": window[0].isoformat(),
                                "period": 1,
                                "start": "07:45",
                                "end": "08:30",
                                "subject": "Mathematik",
                                "room": "A1",
                            }
                        ]
                    }
                )
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(404, "not found")

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        data = parse_qs(self.rfile.read(length).decode("utf-8"))
        valid = (
            data.get("_username") == ["test"]
            and data.get("_password") == ["geheim"]
            and data.get("_csrf") == ["token123"]
        )
        if not valid:
            self._send(200, LOGIN_PAGE)
            return
        self.send_response(302)
        self.send_header("Location", "/iserv/")
        self.send_header("Set-Cookie", "session=ok; Path=/")
        self.end_headers()

    def log_message(self, *args) -> None:
        return


@pytest.fixture()
def portal_url():
    """Startet den Testserver mit JSON-Schnittstelle."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), PortalHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    PORTAL_HITS.clear()
    PORTAL_FILTERS.clear()
    PORTAL_STATE["body"] = PORTAL_JSON
    PORTAL_STATE["deny"] = False
    PORTAL_STATE["echo"] = False
    PORTAL_STATE["reject"] = False
    PORTAL_STATE["wildcard_only"] = False
    PORTAL_STATE["alias_only"] = False
    PORTAL_STATE["filter_error"] = "Fehler beim Verarbeiten der Anfrage"
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_portal_json_is_used_when_page_is_javascript_shell(portal_url: str) -> None:
    """Die HTML-Seite enthaelt nur Kopfzeilen - die Stunden liefert ``data``."""
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    dataset = client.fetch_dataset(default_year=2026)
    lesson = dataset.days[date(2026, 10, 19)].lessons[0]
    assert lesson.subject == "Mathematik"
    assert lesson.room == "A1"
    assert lesson.period == 1
    assert lesson.start == time(7, 45)
    assert "/iserv/timetable/data" in client.last_url
    data_hits = [hit for hit in PORTAL_HITS if hit[0].startswith("/iserv/timetable/data")]
    assert data_hits, "JSON-Schnittstelle wurde nicht abgefragt"
    assert "filter=" in data_hits[0][0]
    assert data_hits[0][1] == "XMLHttpRequest"
    # IServ weist ``application/json`` als API-Aufruf ab (HTTP 401) - der
    # Abruf muss deshalb wie der Browser mit ``*/*`` kommen.
    assert data_hits[0][2] == "*/*"
    assert not [hit for hit in data_hits if "application/json" in hit[2].lower()]


def test_portal_json_filter_is_one_week() -> None:
    """Der Filter umfasst genau eine Woche (Mo-So) im Format ``TT.MM.JJJJ``.

    Ein groesserer Zeitraum wird von IServ mit HTTP 400 abgewiesen - die
    Ansicht laedt immer nur eine Woche.
    """
    client = IservClient(
        IservOptions(base_url="https://schule.de/iserv", username="", password="")
    )
    urls = client._portal_data_urls()
    assert urls[0].startswith("https://schule.de/iserv/timetable/data?filter=")
    assert urls[-1].endswith("/iserv/timetable/data")
    windows = []
    for url in urls[:-1]:
        query = unquote(url.split("filter=", 1)[1])
        payload = json.loads(query)
        # Pflichtfelder des Moduls (seit 1.0.12) - ohne sie: HTTP 400.
        assert payload["classes"] == []
        assert payload["teachers"] == [] and payload["rooms"] == []
        start = datetime.strptime(payload["startDate"], "%d.%m.%Y").date()
        end = datetime.strptime(payload["endDate"], "%d.%m.%Y").date()
        assert start.weekday() == 0
        assert end.weekday() == 6
        assert (end - start).days == 6
        windows.append((start, end))
    assert len(windows) >= 2, "Vorschaufenster braucht mehrere Wochen"
    assert windows[0][1] + timedelta(days=1) == windows[1][0]
    assert windows[0][0] <= date.today() <= windows[-1][1]


def test_portal_json_merges_weeks(portal_url: str) -> None:
    """Wochenweise Antworten werden zu einem Datensatz zusammengefasst."""
    PORTAL_STATE["echo"] = True
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    dataset = client.fetch_dataset(default_year=2026)
    assert len(dataset.days) >= 2
    assert all(len(day.lessons) == 1 for day in dataset.days.values())
    data_hits = [hit for hit in PORTAL_HITS if hit[0].startswith("/iserv/timetable/data?")]
    assert len(data_hits) >= len(dataset.days)


def test_portal_json_without_lessons_reports_structure(portal_url: str) -> None:
    """Leere Antwort: Fehlermeldung nennt die Schluessel der JSON-Struktur."""
    PORTAL_STATE["body"] = json.dumps({"data": [], "config": {"week": "A"}})
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    with pytest.raises(FetchError) as error:
        client.fetch_dataset(default_year=2026)
    message = str(error.value)
    assert "/iserv/timetable/data" in message
    assert "[JSON:" in message
    assert "config" in message


def test_portal_json_denied_by_server_reports_status(portal_url: str) -> None:
    """HTTP 401 auf ``data`` landet sichtbar in der Fehlermeldung.

    Vor 1.0.10 stand bei einer 401-Antwort nur "keine verwertbaren Daten" im
    Log - der eigentliche Grund war nicht erkennbar.
    """
    PORTAL_STATE["deny"] = True
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    with pytest.raises(FetchError) as error:
        client.fetch_dataset(default_year=2026)
    message = str(error.value)
    assert "[JSON:" in message
    assert "HTTP 401" in message
    assert "data?filter" in message





def test_portal_json_filter_has_required_properties() -> None:
    """``classes``/``teachers``/``rooms`` sind Pflicht - 1.0.11 liess sie weg.

    Ohne sie antwortet IServ mit HTTP 400 und „Die erforderliche Eigenschaft
    classes fehlt.“; darum steht der Filter seit 1.0.12 immer vollstaendig da.
    """
    client = IservClient(
        IservOptions(base_url="https://schule.de/iserv", username="", password="")
    )
    urls = client._portal_data_urls()
    for url in urls[:-1]:
        payload = json.loads(unquote(url.split("filter=", 1)[1]))
        assert payload["classes"] == []
        assert payload["teachers"] == []
        assert payload["rooms"] == []
        assert payload["startDate"] and payload["endDate"]

    wildcard = client._portal_filter_payload(
        date(2026, 10, 19), date(2026, 10, 25), PORTAL_FILTER_WILDCARD
    )
    assert wildcard["classes"] == ["%"]
    assert wildcard["teachers"] == ["%"]
    assert wildcard["rooms"] == ["%"]


def test_portal_json_sends_required_filter_to_server(portal_url: str) -> None:
    """Der Testserver prueft den Filter wie IServ - ohne ``classes``: HTTP 400."""
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days
    assert PORTAL_FILTERS
    first = PORTAL_FILTERS[0]
    assert first["classes"] == []
    assert first["teachers"] == [] and first["rooms"] == []
    assert first["startDate"] == "19.10.2026"
    assert first["endDate"] == "25.10.2026"


def test_portal_json_switches_to_wildcard_filter(portal_url: str) -> None:
    """Leere Klassenliste ohne Stunden: die ``%``-Schreibweise wird probiert."""
    PORTAL_STATE["wildcard_only"] = True
    PORTAL_STATE["echo"] = True
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    dataset = client.fetch_dataset(default_year=2026)
    assert len(dataset.days) >= 2
    assert client._portal_filter_choice == PORTAL_FILTER_WILDCARD
    # Nur die erste Woche wurde mit der leeren Liste gefragt; danach gilt die
    # funktionierende Schreibweise - sonst kostet jede Woche zwei Anfragen.
    assert sum(1 for payload in PORTAL_FILTERS if payload["classes"] == []) == 1
    assert any(payload["classes"] == ["%"] for payload in PORTAL_FILTERS)


def test_portal_json_stops_after_filter_error(portal_url: str) -> None:
    """Ein abgelehnter Filter beendet den Abruf nach der ersten Woche.

    Vor 1.0.12 wurden alle Wochen und beide Header-Varianten abgefragt - bei
    konstantem HTTP 400 waren das ueber ein Dutzend Anfragen pro Lauf.
    """
    PORTAL_STATE["reject"] = True
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    with pytest.raises(FetchError) as error:
        client.fetch_dataset(default_year=2026)
    message = str(error.value)
    assert "Eigenschaft classes" in message
    data_hits = [hit for hit in PORTAL_HITS if "/timetable/data" in hit[0]]
    # Beide Schreibweisen des Filters, aber nur fuer die erste Woche.
    assert len(data_hits) == 2
    assert not [hit for hit in PORTAL_HITS if "time-table/data" in hit[0]]


def test_portal_json_uses_dash_path_when_standard_path_is_missing(portal_url: str) -> None:
    """Fehlt ``/iserv/timetable/data``, wird ``/iserv/time-table/data`` probiert."""
    PORTAL_STATE["alias_only"] = True
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    dataset = client.fetch_dataset(default_year=2026)
    assert dataset.days
    assert any("time-table/data" in hit[0] for hit in PORTAL_HITS)




def test_portal_json_prefers_data_timetable_over_plain(portal_url: str) -> None:
    """``data.timetable`` und ``plain-timetable`` enthalten dieselben Stunden.

    Beide zu lesen erzeugt Duplikate - gelesen wird nur ``data.timetable``.
    """
    PORTAL_STATE["body"] = json.dumps(
        {
            "meta": {"filter": {"startDate": "19.10.2026", "endDate": "25.10.2026"}},
            "data": {
                "timetable": [
                    {
                        "date": "2026-10-19",
                        "period": 1,
                        "subject": "Mathematik",
                        "room": "A1",
                    }
                ],
                "plain-timetable": [
                    {
                        "date": "2026-10-19",
                        "period": 1,
                        "subject": "Mathematik",
                        "room": "A1",
                    },
                    {
                        "date": "2026-10-19",
                        "period": 2,
                        "subject": "Englisch",
                        "room": "B2",
                    },
                ],
            },
            "plain-changes": [],
        }
    )
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    dataset = client.fetch_dataset(default_year=2026)
    monday = dataset.days[date(2026, 10, 19)]
    assert [lesson.subject for lesson in monday.lessons] == ["Mathematik"]


def test_portal_json_handles_students_entries_with_weekday(portal_url: str) -> None:
    """DieSchulApp-Schnittstelle: ``students[].entries[]`` mit ``weekday`` (0=Mo)."""
    PORTAL_STATE["body"] = json.dumps(
        {
            "students": [
                {
                    "entries": [
                        {
                            "weekday": 1,
                            "timeTableSlot": {
                                "number": 3,
                                "startTime": "09:50",
                                "endTime": "10:35",
                            },
                            "courseSubject": {
                                "subject": {"name": "Mathematik", "acronym": "M"},
                                "course": {"name": "10a"},
                                "teachers": [{"surname": "Muster"}],
                            },
                            "room": {"name": "A1"},
                        }
                    ]
                }
            ],
            "meta": {"filter": {"startDate": "19.10.2026"}},
        }
    )
    client = IservClient(IservOptions(base_url=portal_url, username="test", password="geheim"))
    dataset = client.fetch_dataset(default_year=2026)
    lesson = dataset.days[date(2026, 10, 20)].lessons[0]
    assert lesson.period == 3
    assert lesson.subject == "Mathematik"
    assert lesson.room == "A1"
    assert lesson.start == time(9, 50)
    assert lesson.end == time(10, 35)


def test_portal_weeks_follow_berlin_time_across_dst(monkeypatch) -> None:
    """Die Wochenrechnung nutzt die Berliner Zeit - auch in der Nacht der Umstellung.

    Am 25.10.2026 endet die Sommerzeit um 03:00. Um 00:30 gilt noch CEST; die
    Woche muss trotzdem am Montag 19.10. beginnen und am Sonntag 25.10. enden.
    """

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - Testdouble
            moment = datetime(2026, 10, 25, 0, 30, tzinfo=iserv_module.TZ)
            return moment.astimezone(tz) if tz else moment

    monkeypatch.setattr(iserv_module, "datetime", FixedDatetime)
    client = IservClient(
        IservOptions(base_url="https://schule.de/iserv", username="", password="")
    )
    weeks = client._portal_weeks()
    assert weeks[0] == (date(2026, 10, 19), date(2026, 10, 25))
    assert weeks[1][0] == date(2026, 10, 26)
    payload = client._portal_filter_payload(*weeks[0])
    assert payload["startDate"] == "19.10.2026"
    assert payload["endDate"] == "25.10.2026"
