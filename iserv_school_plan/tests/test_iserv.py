"""Tests fuer den IServ-Client (Login-Flow gegen einen lokalen Testserver)."""

from __future__ import annotations

import threading
from datetime import date, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from app.iserv import (
    ACCEPT_CALENDAR,
    ACCEPT_HTML,
    ACCEPT_JSON,
    AuthError,
    FetchError,
    IservClient,
    IservOptions,
    _LoginFormParser,
    _accept_for,
    _detect_fields,
    _looks_like_login_page,
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
