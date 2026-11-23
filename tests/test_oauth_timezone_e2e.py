#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""End-to-end: an OAuth client's tool responses use the zone resolved at consent.

Boots the real server against a tiny fake Zabbix JSON-RPC endpoint
(``apiinfo.version`` -> 7.0.0, everything else -> ``[]``), runs the
real register / authorize / login / consent / token flow posting
``browser_timezone=Europe/London`` with the consent form, then calls
``host_get`` and reads the zone line off the response (issue #75).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from tests.test_oauth_e2e import (
    REPO_ROOT, _NoRedirectHandler, _free_port, _hash_password, _http_post_form,
    _http_post_json, _wait_for_health,
)
from tests.test_oauth_role_e2e import _rpc


class _FakeZabbix(BaseHTTPRequestHandler):
    """Answers every JSON-RPC call; just enough for the client to connect."""

    def do_POST(self):  # noqa: N802 - http.server API
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            req = {}
        method = req.get("method", "")
        result = "7.0.0" if method == "apiinfo.version" else []
        body = json.dumps({"jsonrpc": "2.0", "result": result, "id": req.get("id", 1)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the test output quiet
        pass


class TestOAuthTimezoneEndToEnd(unittest.TestCase):
    proc = None
    cfg_dir = None
    zabbix = None
    password = "Operator_Pass_2026!"

    @classmethod
    def setUpClass(cls):
        cls.zabbix = ThreadingHTTPServer(("127.0.0.1", 0), _FakeZabbix)
        threading.Thread(target=cls.zabbix.serve_forever, daemon=True).start()
        zbx_port = cls.zabbix.server_address[1]

        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.cfg_dir = tempfile.TemporaryDirectory(prefix="zmcp-oauth-tz-")
        cfg = Path(cls.cfg_dir.name) / "config.toml"
        h = _hash_password(cls.password)
        cfg.write_text(f'''[server]
host = "127.0.0.1"
port = {cls.port}
transport = "http"
public_url = "{cls.base}"
log_level = "warning"

[admin]
enabled = false

[admin.users.plain]
password_hash = "{h}"
role = "operator"

[admin.users.zoned]
password_hash = "{h}"
role = "operator"
timezone = "Asia/Tokyo"

[oauth]
enabled = true

[zabbix.fake]
url = "http://127.0.0.1:{zbx_port}"
api_token = "fake"
read_only = true
''')
        env = os.environ.copy(); env["PYTHONUNBUFFERED"] = "1"
        cls.proc = subprocess.Popen(
            [sys.executable, "-c", "from zabbix_mcp.cli import main; main()", "--config", str(cfg)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(REPO_ROOT), env=env,
        )
        try:
            _wait_for_health(f"{cls.base}/health", timeout=20)
        except Exception:
            cls.proc.terminate()
            out = cls.proc.stdout.read().decode(errors="replace") if cls.proc.stdout else ""
            cls.tearDownClass()
            raise RuntimeError(f"server did not start:\n{out[-2000:]}")

    @classmethod
    def tearDownClass(cls):
        if cls.proc is not None:
            cls.proc.terminate()
            with contextlib.suppress(Exception):
                cls.proc.wait(timeout=5)
        if cls.zabbix is not None:
            cls.zabbix.shutdown()
            cls.zabbix.server_close()
        if cls.cfg_dir is not None:
            cls.cfg_dir.cleanup()

    # -- the real flow ---------------------------------------------------

    def _tokens_for(self, username, browser_timezone="Europe/London"):
        status, client_info, _ = _http_post_json(f"{self.base}/register", {
            "redirect_uris": ["http://localhost:8765/callback"], "client_name": f"tz e2e {username}",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        })
        self.assertIn(status, (200, 201), client_info)
        client_id = client_info["client_id"]
        verifier = secrets.token_urlsafe(64)[:64]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        qs = urlencode({"response_type": "code", "client_id": client_id,
                        "redirect_uri": "http://localhost:8765/callback",
                        "code_challenge": challenge, "code_challenge_method": "S256", "state": "s"})
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            opener.open(f"{self.base}/authorize?{qs}", timeout=5); resp = None
        except urllib.error.HTTPError as exc:
            resp = exc
        request_id = parse_qs(urlparse(resp.headers.get("Location")).query)["request_id"][0]
        status, body, _ = _http_post_form(f"{self.base}/oauth/login", {
            "request_id": request_id, "username": username, "password": self.password})
        self.assertEqual(status, 200, body[:200])
        self.assertIn('name="browser_timezone"', body, "consent form carries the hidden zone input")
        consent = [("request_id", request_id), ("step", "consent"), ("action", "allow"), ("scope", "monitoring")]
        if browser_timezone is not None:
            consent.append(("browser_timezone", browser_timezone))
        status, _b, headers = _http_post_form(f"{self.base}/oauth/login", consent)
        self.assertEqual(status, 302, _b[:300])
        code = parse_qs(urlparse(headers.get("location") or headers.get("Location")).query)["code"][0]
        status, token_body, _ = _http_post_form(f"{self.base}/token", {
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": "http://localhost:8765/callback", "code_verifier": verifier}, follow=True)
        tok = json.loads(token_body)
        self.assertTrue(tok.get("access_token"), token_body)
        return client_id, tok

    def _session(self, access_token):
        bearer = {"Authorization": f"Bearer {access_token}", "Accept": "application/json, text/event-stream"}
        status, body, headers = _http_post_json(f"{self.base}/mcp", {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "tz-e2e", "version": "1"}}}, headers=bearer)
        self.assertEqual(status, 200, body)
        sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
        h = {**bearer, "mcp-session-id": sid}
        _http_post_json(f"{self.base}/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, headers=h)
        return h

    def _host_get(self, h):
        status, body, _ = _http_post_json(f"{self.base}/mcp", {
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": "host_get", "arguments": {"limit": 1}}}, headers=h)
        self.assertEqual(status, 200, body)
        res = _rpc(body).get("result", {})
        text = "".join(c.get("text", "") for c in res.get("content", []))
        self.assertFalse(res.get("isError"), text)
        return text

    # -- tests -----------------------------------------------------------

    def test_browser_zone_posted_at_consent_rides_the_token(self):
        client_id, tok = self._tokens_for("plain", browser_timezone="Europe/London")
        text = self._host_get(self._session(tok["access_token"]))
        self.assertIn("Time zone: Europe/London", text)

        # The zone survives a refresh-token rotation.
        status, body, _ = _http_post_form(f"{self.base}/token", {
            "grant_type": "refresh_token", "client_id": client_id,
            "refresh_token": tok["refresh_token"]}, follow=True)
        refreshed = json.loads(body)
        self.assertNotEqual(refreshed["access_token"], tok["access_token"])
        text = self._host_get(self._session(refreshed["access_token"]))
        self.assertIn("Time zone: Europe/London", text)

    def test_portal_user_zone_beats_the_browser_zone(self):
        _, tok = self._tokens_for("zoned", browser_timezone="Europe/London")
        text = self._host_get(self._session(tok["access_token"]))
        self.assertIn("Time zone: Asia/Tokyo", text)
        self.assertNotIn("Europe/London", text)


if __name__ == "__main__":
    unittest.main()
