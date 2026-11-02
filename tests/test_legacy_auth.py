#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""Legacy password login for Zabbix 5.0 / 5.2 (#77, PR #78 by @rspadim).

Those releases have no API tokens. Password login is therefore allowed,
but only behind an explicit `legacy_auth = true`, never as a fallback,
never next to a token, and never against a Zabbix that has tokens
(5.4+). The tests pin each of those edges because each one is a way the
"workaround" could quietly become the normal path.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from zabbix_mcp.client import ClientManager
from zabbix_mcp.config import AppConfig, ConfigError, ZabbixServerConfig, load_config


def _load(body: str) -> AppConfig:
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
        f.write(body); path = f.name
    try:
        return load_config(path)
    finally:
        os.unlink(path)


SRV = '[zabbix.old]\nurl = "http://zabbix5.example.com"\n'


class TestConfigRules(unittest.TestCase):

    def test_legacy_entry_is_valid(self):
        cfg = _load(SRV + 'legacy_auth = true\nusername = "mcp"\npassword = "secret"\n')
        srv = cfg.zabbix_servers["old"]
        self.assertTrue(srv.legacy_auth)
        self.assertEqual((srv.username, srv.password), ("mcp", "secret"))
        self.assertEqual(srv.api_token, "", "a legacy entry carries no token")

    def test_password_without_the_flag_is_refused(self):
        # The point of the flag: password login is never a silent fallback.
        with self.assertRaises(ConfigError) as ctx:
            _load(SRV + 'username = "mcp"\npassword = "secret"\n')
        self.assertIn("legacy_auth", str(ctx.exception))

    def test_flag_together_with_a_token_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            _load(SRV + 'legacy_auth = true\napi_token = "tok"\nusername = "mcp"\npassword = "secret"\n')
        self.assertIn("pick one", str(ctx.exception))

    def test_flag_without_credentials_is_refused(self):
        for partial in ('username = "mcp"\n', 'password = "secret"\n', ''):
            with self.subTest(partial=partial):
                with self.assertRaises(ConfigError):
                    _load(SRV + 'legacy_auth = true\n' + partial)

    def test_non_boolean_flag_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            _load(SRV + 'legacy_auth = "true"\nusername = "mcp"\npassword = "secret"\n')
        self.assertIn("non-boolean", str(ctx.exception))

    def test_env_vars_are_expanded(self):
        with patch.dict(os.environ, {"T_USER": "mcp", "T_PASS": "s3cret"}):
            cfg = _load(SRV + 'legacy_auth = true\nusername = "${T_USER}"\npassword = "${T_PASS}"\n')
        srv = cfg.zabbix_servers["old"]
        self.assertEqual((srv.username, srv.password), ("mcp", "s3cret"))

    def test_empty_after_expansion_is_refused(self):
        with patch.dict(os.environ, {"T_EMPTY": ""}):
            with self.assertRaises(ConfigError):
                _load(SRV + 'legacy_auth = true\nusername = "mcp"\npassword = "${T_EMPTY}"\n')

    def test_token_path_is_unchanged(self):
        cfg = _load(SRV + 'api_token = "tok"\n')
        srv = cfg.zabbix_servers["old"]
        self.assertFalse(srv.legacy_auth)
        self.assertEqual(srv.api_token, "tok")

    def test_missing_token_is_still_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            _load(SRV)
        self.assertIn("api_token", str(ctx.exception))


class TestClientLogin(unittest.TestCase):

    def _manager(self, **kw) -> ClientManager:
        srv = ZabbixServerConfig(name="t", url="http://zabbix.example.com", **kw)
        return ClientManager(AppConfig(zabbix_servers={"t": srv}))

    def test_legacy_logs_in_with_the_password_on_5_0(self):
        mgr = self._manager(api_token="", legacy_auth=True, username="mcp", password="secret")
        with patch("zabbix_mcp.client.ZabbixAPI") as cls:
            api = cls.return_value
            api.version = 5.0
            api.api_version.return_value = "5.0.47"
            with self.assertLogs("zabbix_mcp.client", level="WARNING") as logs:
                mgr._connect("t")
            api.login.assert_called_once_with(user="mcp", password="secret")
        # The library's >= 6.0 gate is relaxed for this entry only.
        self.assertTrue(cls.call_args.kwargs["skip_version_check"])
        self.assertTrue(any("LEGACY" in m and "security risk" in m for m in logs.output),
                        "the choice must stay visible in the operational log")

    def test_legacy_is_refused_against_a_zabbix_that_has_tokens(self):
        mgr = self._manager(api_token="", legacy_auth=True, username="mcp", password="secret")
        for version in (5.4, 6.0, 7.4, 8.0):
            with self.subTest(version=version):
                with patch("zabbix_mcp.client.ZabbixAPI") as cls:
                    api = cls.return_value
                    api.version = version
                    with self.assertRaises(ValueError) as ctx:
                        mgr._connect("t")
                    api.login.assert_not_called()
                self.assertIn("api_token", str(ctx.exception))

    def test_token_path_keeps_the_version_gate(self):
        mgr = self._manager(api_token="tok")
        with patch("zabbix_mcp.client.ZabbixAPI") as cls:
            api = cls.return_value
            api.version = 7.4
            api.api_version.return_value = "7.4.0"
            mgr._connect("t")
            api.login.assert_called_once_with(token="tok")
        self.assertFalse(cls.call_args.kwargs["skip_version_check"],
                         "token auth must not inherit the relaxed gate")

    def test_token_path_honours_the_operator_gate_setting(self):
        mgr = self._manager(api_token="tok", skip_version_check=True)
        with patch("zabbix_mcp.client.ZabbixAPI") as cls:
            cls.return_value.version = 8.0
            cls.return_value.api_version.return_value = "8.0.0"
            mgr._connect("t")
        self.assertTrue(cls.call_args.kwargs["skip_version_check"])


if __name__ == "__main__":
    unittest.main()
