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

"""Write tools and ``tools/list`` on a read-only server (#83).

Two layers: ``_filter_tools_by_server_mode`` drops the write tools from a
caller's catalog when every server it can reach is ``read_only`` (stdio,
the legacy auth_token and read-write tokens included), and
``[server].register_write_tools = false`` (Clay Curtis) leaves them out
of the registry altogether."""

import sys
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, "src")

from zabbix_mcp import server as srv  # noqa: E402
from zabbix_mcp.client import ClientManager  # noqa: E402
from zabbix_mcp.config import AppConfig, ServerConfig, ZabbixServerConfig  # noqa: E402
from zabbix_mcp.server import _filter_tools_by_server_mode, _register_tools  # noqa: E402
from zabbix_mcp.token_store import current_token_info  # noqa: E402

TOOLS = [NS(name=n) for n in ("host_get", "host_create", "host_update", "host_delete", "problem_get",
                               "action_prepare", "action_confirm", "zabbix_raw_api_call", "health_check",
                               "service_get", "service_update", "device_get", "device_offboard")]
READ = ["host_get", "problem_get", "health_check", "service_get", "device_get"]


def _manager(**servers):
    cfg = AppConfig(server=ServerConfig(), zabbix_servers={
        name: ZabbixServerConfig(name=name, url=f"http://{name}", api_token="t" * 64, read_only=ro)
        for name, ro in servers.items()})
    return ClientManager(cfg)


class TestServerModeFilter(unittest.TestCase):
    def setUp(self):
        srv._ensure_write_tools_set()

    def _names(self, mgr, token=None):
        tok = current_token_info.set(token) if token is not None else None
        try:
            return [t.name for t in _filter_tools_by_server_mode(TOOLS, mgr)]
        finally:
            if tok is not None:
                current_token_info.reset(tok)

    def test_single_read_only_server_hides_write_tools_without_a_token(self):
        self.assertEqual(self._names(_manager(z=True)), READ)

    def test_read_write_server_keeps_everything(self):
        self.assertEqual(self._names(_manager(z=False)), [t.name for t in TOOLS])

    def test_one_writable_server_among_read_only_ones_keeps_everything(self):
        self.assertEqual(self._names(_manager(a=True, b=False, c=True)), [t.name for t in TOOLS])

    def test_token_restricted_to_read_only_servers_hides_them(self):
        mgr = _manager(a=True, b=False)
        token = NS(allowed_servers=["a"], scopes=["*"], read_only=False)
        self.assertEqual(self._names(mgr, token), READ)
        wildcard = NS(allowed_servers=["*"], scopes=["*"], read_only=False)
        self.assertEqual(self._names(mgr, wildcard), [t.name for t in TOOLS])

    def test_unknown_server_in_token_does_not_hide(self):
        mgr = _manager(a=False)
        token = NS(allowed_servers=["ghost"], scopes=["*"], read_only=False)
        self.assertEqual(self._names(mgr, token), [t.name for t in TOOLS])


class TestRegisterWriteTools(unittest.TestCase):
    def _names(self, **kw):
        from mcp.server.mcpserver import MCPServer
        mcp = MCPServer(name="test")
        _register_tools(mcp, _manager(z=False), **kw)
        return {t.name for t in mcp._tool_manager.list_tools()}

    def test_default_registers_write_tools(self):
        names = self._names()
        for n in ("host_create", "host_delete", "service_update", "device_offboard", "zabbix_raw_api_call", "action_prepare"):
            self.assertIn(n, names)

    def test_false_leaves_every_write_tool_out(self):
        names = self._names(register_write_tools=False)
        for n in ("host_create", "host_update", "host_delete", "host_massadd", "service_update",
                  "device_offboard", "zabbix_raw_api_call", "action_prepare", "action_confirm"):
            self.assertNotIn(n, names)
        for n in ("host_get", "problem_get", "service_get", "device_get", "apiinfo_version", "health_check"):
            self.assertIn(n, names)
        self.assertTrue(names.isdisjoint(srv._build_write_tools_set()))


class TestToolGroups(unittest.TestCase):
    def test_every_registered_tool_belongs_to_a_group(self):
        """A prefix missing from TOOL_GROUPS is unreachable through a ``tools``
        allowlist or a scoped token (the v1.41 objects were, once)."""
        from zabbix_mcp.api import ALL_METHODS
        from zabbix_mcp.config import TOOL_GROUPS
        grouped = {p for prefixes in TOOL_GROUPS.values() for p in prefixes}
        missing = sorted({m.tool_name.rsplit("_", 1)[0] for m in ALL_METHODS} - grouped)
        self.assertEqual(missing, [])
        for prefix, group in (("service", "monitoring"), ("apmtrace", "monitoring"), ("device", "administration"),
                              ("serverinfo", "administration"), ("apiinfo", "administration"),
                              ("ceprule", "alerts"), ("hostdashboard", "monitoring")):
            self.assertIn(prefix, TOOL_GROUPS[group])


if __name__ == "__main__":
    unittest.main()
