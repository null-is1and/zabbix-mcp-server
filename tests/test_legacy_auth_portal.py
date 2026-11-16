#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""The admin portal enforces the same legacy-auth rules as the loader.

Drives the real `server_create` / `server_edit` handlers with a stub
request, so what the form can write is exactly what `load_config` will
accept at the next boot - and so a password never travels back to the
browser on a validation error.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zabbix_mcp.admin.config_writer import load_config_document
from zabbix_mcp.admin.views import servers as views
from zabbix_mcp.config import load_config

_BASE = """
[server]
transport = "http"
host = "127.0.0.1"
port = 8080

[zabbix.main]
url = "https://zabbix.example.com"
api_token = "dummy"

[zabbix.old]
url = "http://zabbix5.example.com"
legacy_auth = true
username = "mcp"
password = "stored-secret"
"""


class _Req:
    """Just enough of a Starlette Request for the handlers."""

    def __init__(self, admin_app, form: dict, method="POST", path_params=None):
        self.app = SimpleNamespace(state=SimpleNamespace(admin_app=admin_app))
        self._form = form
        self.method = method
        self.path_params = path_params or {}
        self.client = SimpleNamespace(host="127.0.0.1")

    async def form(self):
        return self._form


def _admin_app(path: str):
    from zabbix_mcp.admin.app import AdminApp
    app = AdminApp(config=load_config(path), config_path=path,
                   client_manager=MagicMock(), token_store=MagicMock(), oauth_provider=None)
    app.client_manager.server_names = ["main", "old"]
    app.client_manager.skipped_servers = {}
    app.require_auth = lambda request: SimpleNamespace(role="admin", user="tester")
    app.render = MagicMock(return_value="rendered")   # capture the re-render context
    return app


def _run(coro):
    return asyncio.run(coro)


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        tmp.write(_BASE); tmp.close()
        self.path = tmp.name
        self.app = _admin_app(self.path)
        self._audit = patch.object(views, "write_audit"); self._audit.start()

    def tearDown(self):
        self._audit.stop()
        Path(self.path).unlink(missing_ok=True)

    def _entry(self, name):
        return dict(load_config_document(self.path).get("zabbix", {}).get(name, {}))

    def _create(self, **form):
        base = {"name": "new", "url": "http://zabbix5.example.com", "api_token": "", "username": "",
                "password": "", "request_timeout": "300"}
        base.update(form)
        _run(views.server_create(_Req(self.app, base)))
        return self.app.render.call_args.args[2] if self.app.render.called else None

    def _edit(self, name, **form):
        base = {"name": name, "url": "", "api_token": "", "username": "", "password": "",
                "request_timeout": "300", "frontend_username": "", "frontend_password": "",
                "_cfg_mtime": ""}
        base.update(form)
        return _run(views.server_edit(_Req(self.app, base, path_params={"server_name": name})))


class TestCreate(_Base):

    def test_legacy_entry_is_written_without_a_token(self):
        self._create(legacy_auth="on", username="mcp", password="s3cret")
        e = self._entry("new")
        self.assertTrue(e.get("legacy_auth"))
        self.assertEqual((e["username"], e["password"]), ("mcp", "s3cret"))
        self.assertNotIn("api_token", e)
        load_config(self.path)   # and the loader accepts what the form wrote

    def test_token_entry_carries_no_credentials(self):
        self._create(api_token="tok")
        e = self._entry("new")
        self.assertEqual(e["api_token"], "tok")
        for k in ("legacy_auth", "username", "password"):
            self.assertNotIn(k, e)

    def test_password_without_the_checkbox_is_refused(self):
        ctx = self._create(username="mcp", password="s3cret")
        self.assertIn("Legacy auth checkbox", ctx["add_form_error"])
        self.assertEqual(self._entry("new"), {})

    def test_checkbox_with_a_token_is_refused(self):
        ctx = self._create(legacy_auth="on", api_token="tok", username="mcp", password="s3cret")
        self.assertIn("Clear the API token", ctx["add_form_error"])
        self.assertEqual(self._entry("new"), {})

    def test_checkbox_without_a_password_is_refused(self):
        ctx = self._create(legacy_auth="on", username="mcp")
        self.assertIn("both a username and a password", ctx["add_form_error"])

    def test_password_is_never_echoed_into_the_re_render(self):
        # A validation error re-renders the form with the typed values -
        # except the password, which would otherwise land in the DOM.
        ctx = self._create(legacy_auth="on", username="mcp", password="s3cret", api_token="tok")
        self.assertNotIn("form_password", ctx)
        self.assertEqual(ctx["form_username"], "mcp")
        self.assertTrue(ctx["form_legacy_auth"])


class TestEdit(_Base):

    def test_empty_password_keeps_the_stored_one(self):
        self._edit("old", url="http://zabbix5.example.com", legacy_auth="on", username="mcp")
        self.assertEqual(self._entry("old")["password"], "stored-secret")

    def test_unticking_legacy_removes_the_credentials(self):
        self._edit("old", url="http://zabbix5.example.com", api_token="newtok")
        e = self._entry("old")
        self.assertEqual(e["api_token"], "newtok")
        for k in ("legacy_auth", "username", "password"):
            self.assertNotIn(k, e)
        load_config(self.path)

    def test_unticking_legacy_without_a_token_is_refused(self):
        resp = self._edit("old", url="http://zabbix5.example.com")
        self.assertEqual(resp.status_code, 303)      # redirected back with the error flash
        self.assertEqual(self._entry("old")["password"], "stored-secret", "nothing changed")

    def test_ticking_legacy_on_a_token_server_drops_the_token(self):
        self._edit("main", url="https://zabbix.example.com", legacy_auth="on",
                   username="mcp", password="s3cret")
        e = self._entry("main")
        self.assertTrue(e["legacy_auth"]); self.assertNotIn("api_token", e)
        load_config(self.path)


if __name__ == "__main__":
    unittest.main()
