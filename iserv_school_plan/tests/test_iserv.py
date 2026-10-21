"""Tests fuer den IServ-Client (Login-Flow gegen einen lokalen Testserver)."""

from __future__ import annotations

import threading
from datetime import date, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from app.iserv import (
    AuthError,
    IservClient,
    IservOptions,
    _LoginFormParser,
    _detect_fields,
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


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
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
