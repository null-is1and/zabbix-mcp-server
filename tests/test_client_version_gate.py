#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, version 3.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
# details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#

"""zabbix-utils refuses a Zabbix release newer than the one it was tested
with (2.0.4 stops at 7.4). The server carries its own 5.0 ... 8.0 catalog,
so ``ClientManager._connect`` retries such a server with the version check
off and a warning; a release below the library's minimum is still refused
as before (legacy_auth is the only way past that gate)."""

import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "src")

from zabbix_utils.exceptions import APINotSupported  # noqa: E402

from zabbix_mcp.client import ClientManager, _newer_than_library  # noqa: E402
from zabbix_mcp.config import AppConfig, ServerConfig, ZabbixServerConfig  # noqa: E402

NEWER = ("Version of Zabbix API [8.0.0] was not tested with the library. The latest tested "
         "version is 7.4.0. If you're sure zabbix_utils will work properly with your current "
         "Zabbix version you can skip this check by specifying skip_version_check=True")
OLDER = ("Version of Zabbix API [5.0.47] is not supported by the library. The oldest supported "
         "version is 6.0.0. If you're sure zabbix_utils will work properly with your current "
         "Zabbix version you can skip this check by specifying skip_version_check=True")


class TestNewerThanLibrary(unittest.TestCase):
    def test_newer_release_is_recognised(self):
        self.assertTrue(_newer_than_library(APINotSupported(NEWER)))
        self.assertTrue(_newer_than_library(APINotSupported(NEWER.replace("8.0.0", "9.2.1"))))

    def test_older_release_is_not(self):
        self.assertFalse(_newer_than_library(APINotSupported(OLDER)))

    def test_unknown_message_is_not(self):
        self.assertFalse(_newer_than_library(APINotSupported("something else")))
        self.assertFalse(_newer_than_library(RuntimeError(NEWER)))


class _FakeApi:
    version = 8.0

    def __init__(self, **kw):
        self.kw = kw

    def login(self, **kw):
        pass

    def api_version(self):
        return "8.0.0"


class TestConnectRetries(unittest.TestCase):
    def _manager(self):
        cfg = AppConfig(server=ServerConfig(), zabbix_servers={
            "z": ZabbixServerConfig(name="z", url="http://zabbix.example", api_token="t" * 64)})
        return ClientManager(cfg)

    def test_newer_server_connects_with_the_check_off_and_a_warning(self):
        calls = []

        def fake(**kw):
            calls.append(kw)
            if not kw.get("skip_version_check"):
                raise APINotSupported(NEWER)
            return _FakeApi(**kw)

        with patch("zabbix_mcp.client.ZabbixAPI", side_effect=fake), \
             self.assertLogs("zabbix_mcp.client", level="WARNING") as logs:
            api = self._manager()._connect("z")
        self.assertIsInstance(api, _FakeApi)
        self.assertEqual([c.get("skip_version_check") for c in calls], [False, True])
        self.assertIn("newer than the zabbix-utils library", logs.output[0])

    def test_older_server_is_still_refused(self):
        with patch("zabbix_mcp.client.ZabbixAPI", side_effect=APINotSupported(OLDER)):
            with self.assertRaises(APINotSupported):
                self._manager()._connect("z")


if __name__ == "__main__":
    unittest.main()
