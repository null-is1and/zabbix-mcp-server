#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""The portal's connection probes carry the same legacy rules as the client.

Review of v1.37 found all three probe paths untested: the version lock in
`_probe_user_login` and `server_test_new` could have been deleted with
the suite still green, the new-server probe had started refusing a valid
token against Zabbix 8.0, and the saved-server probe sent `${ENV_VAR}`
passwords to Zabbix literally.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from zabbix_mcp.admin.views import servers as views
from zabbix_mcp.config import load_config

_CFG = """
[server]
transport = "http"
host = "127.0.0.1"
port = 8080

[zabbix.old]
url = "http://zabbix5.example.com"
legacy_auth = true
username = "${T_USER}"
password = "${T_PASS}"
"""


def _api(version: float, version_str: str):
    api = MagicMock()
    api.version = version
    api.api_version.return_value = version_str
    return api


class TestSavedServerProbe(unittest.TestCase):

    def test_refuses_a_zabbix_that_has_tokens_before_logging_in(self):
        for v, vs in ((5.4, "5.4.0"), (6.0, "6.0.0"), (7.4, "7.4.8")):
            with self.subTest(version=vs), patch.object(views, "ZabbixAPI", create=True) as cls, \
                    patch("zabbix_utils.ZabbixAPI", return_value=_api(v, vs)) as real:
                with self.assertRaises(ValueError) as ctx:
                    views._probe_user_login("http://z", "u", "p", False)
                real.return_value.login.assert_not_called()
            self.assertIn("5.4 and newer", str(ctx.exception))

    def test_logs_in_on_5_0(self):
        with patch("zabbix_utils.ZabbixAPI", return_value=_api(5.0, "5.0.47")) as real:
            ok, version = views._probe_user_login("http://z", "u", "p", False)
        real.return_value.login.assert_called_once_with(user="u", password="p")
        self.assertEqual((ok, version), (True, "5.0.47"))


class TestServerTestResolvesEnvVars(unittest.TestCase):
    """`${ENV_VAR}` credentials must reach the probe resolved, as the loader resolves them."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
        tmp.write(_CFG); tmp.close(); self.path = tmp.name

    def tearDown(self):
        Path(self.path).unlink(missing_ok=True)

    def test_env_backed_password_is_resolved_before_probing(self):
        from zabbix_mcp.admin.app import AdminApp
        with patch.dict(os.environ, {"T_USER": "mcp", "T_PASS": "s3cret"}):
            app = AdminApp(config=load_config(self.path), config_path=self.path,
                           client_manager=MagicMock(), token_store=MagicMock(), oauth_provider=None)
            app.require_auth = lambda r: SimpleNamespace(role="admin", user="t")
            req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(admin_app=app)),
                                  path_params={"server_name": "old"})
            with patch.object(views, "_probe_user_login", return_value=(True, "5.0.47")) as probe:
                asyncio.run(views.server_test(req))
        probe.assert_called_once()
        args = probe.call_args.args
        self.assertEqual(args[1:3], ("mcp", "s3cret"), "the literal ${...} must never be sent as a password")


class TestNewServerProbe(unittest.TestCase):

    def _req(self, **form):
        base = {"url": "https://zabbix.example.com", "api_token": "", "username": "", "password": "",
                "verify_ssl": "1", "legacy_auth": "0"}
        base.update(form)
        app = MagicMock()
        app.require_auth = lambda r: SimpleNamespace(role="admin", user="t")
        req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(admin_app=app)))
        async def form_(): return base
        req.form = form_
        return req

    def _run(self, req, api):
        # The SSRF guard resolves the hostname; keep it off the network.
        with patch("zabbix_utils.ZabbixAPI", return_value=api) as real, \
                patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("203.0.113.10", 0))]):
            resp = asyncio.run(views.server_test_new(req))
        return real, resp.body.decode()

    def test_token_test_against_8_0_is_not_refused_by_the_library_gate(self):
        # Regression: the probe had started passing skip_version_check=False
        # for token auth, and zabbix-utils' max-supported gate (7.4) then
        # turned a valid 8.0 token test red.
        real, body = self._run(self._req(api_token="tok"), _api(8.0, "8.0.0"))
        self.assertTrue(real.call_args.kwargs["skip_version_check"])
        real.return_value.login.assert_called_once_with(token="tok")
        self.assertIn("Connected", body)

    def test_legacy_against_7_4_is_refused_before_login(self):
        real, body = self._run(self._req(legacy_auth="1", username="u", password="p"), _api(7.4, "7.4.8"))
        real.return_value.login.assert_not_called()
        self.assertIn("refused on 5.4 and newer", body)

    def test_legacy_against_5_0_logs_in(self):
        real, body = self._run(self._req(legacy_auth="1", username="u", password="p"), _api(5.0, "5.0.47"))
        real.return_value.login.assert_called_once_with(user="u", password="p")
        self.assertIn("Connected", body)

    def test_credentials_without_the_checkbox_are_refused(self):
        real, body = self._run(self._req(username="u", password="p"), _api(5.0, "5.0.47"))
        real.assert_not_called()
        self.assertIn("Legacy auth checkbox", body)


if __name__ == "__main__":
    unittest.main()
