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

"""Discussion #76 (MSP setups, one ``[zabbix.*]`` per customer): a token
restricted with ``allowed_servers`` must not learn the other instance names
from the ``server`` parameter description in ``tools/list`` or from the
"Unknown Zabbix server" message."""

import asyncio
import sys
import unittest
from types import SimpleNamespace as NS

sys.path.insert(0, "src")

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import Tool  # noqa: E402

from zabbix_mcp.client import ClientManager  # noqa: E402
from zabbix_mcp.config import AppConfig, ServerConfig, ZabbixServerConfig  # noqa: E402
from zabbix_mcp.server import _register_tools, _resolve_server_for_caller, _scope_server_param  # noqa: E402
from zabbix_mcp.token_store import current_token_info  # noqa: E402


def _manager(*names):
    cfg = AppConfig(server=ServerConfig(), zabbix_servers={
        n: ZabbixServerConfig(name=n, url=f"http://{n}", api_token="t" * 64, read_only=False) for n in names})
    return ClientManager(cfg)


def _with_token(token, fn):
    tok = current_token_info.set(token)
    try:
        return fn()
    finally:
        current_token_info.reset(tok)


class TestSchemaScoping(unittest.TestCase):
    def setUp(self):
        self.mgr = _manager("acme", "globex", "initech")
        mcp = MCPServer(name="test")
        _register_tools(mcp, self.mgr)
        self.tools = [Tool(name=t.name, description=t.description, inputSchema=t.parameters)
                      for t in mcp._tool_manager.list_tools()]

    def _server_desc(self, tools, name="host_get"):
        tool = next(t for t in tools if t.name == name)
        return tool.input_schema["properties"]["server"]["description"]

    def test_registration_names_every_server(self):
        self.assertIn("acme, globex, initech", self._server_desc(self.tools))

    def test_no_token_keeps_the_full_enumeration(self):
        out = _scope_server_param(self.tools, self.mgr)
        self.assertIn("acme, globex, initech", self._server_desc(out))

    def test_restricted_token_sees_only_its_servers_and_its_default(self):
        token = NS(allowed_servers=["globex"], scopes=["*"], read_only=False)
        out = _with_token(token, lambda: _scope_server_param(self.tools, self.mgr))
        desc = self._server_desc(out)
        self.assertEqual(desc, "Target Zabbix server. Available: globex. Defaults to 'globex' if omitted.")
        for t in out:
            props = t.input_schema.get("properties", {})
            if "server" in props:
                self.assertNotIn("acme", props["server"]["description"], t.name)
                self.assertNotIn("initech", props["server"]["description"], t.name)
        # the originals are untouched
        self.assertIn("acme, globex, initech", self._server_desc(self.tools))

    def test_two_of_three_defaults_to_the_first_allowed(self):
        token = NS(allowed_servers=["globex", "initech"], scopes=["*"], read_only=False)
        out = _with_token(token, lambda: _scope_server_param(self.tools, self.mgr))
        self.assertEqual(self._server_desc(out), "Target Zabbix server. Available: globex, initech. Defaults to 'globex' if omitted.")

    def test_wildcard_token_is_unchanged(self):
        token = NS(allowed_servers=["*"], scopes=["*"], read_only=False)
        out = _with_token(token, lambda: _scope_server_param(self.tools, self.mgr))
        self.assertIs(out, self.tools)


class TestUnknownServerMessage(unittest.TestCase):
    def setUp(self):
        self.mgr = _manager("acme", "globex")

    def test_without_a_token_every_server_is_named(self):
        with self.assertRaises(ToolError) as ctx:
            _resolve_server_for_caller(self.mgr, "nope")
        self.assertIn("acme, globex", str(ctx.exception))

    def test_restricted_token_is_told_only_its_own(self):
        token = NS(allowed_servers=["globex"], scopes=["*"], read_only=False)
        with self.assertRaises(ToolError) as ctx:
            _with_token(token, lambda: _resolve_server_for_caller(self.mgr, "acme"))
        self.assertEqual(str(ctx.exception), "Unknown Zabbix server 'acme'. Available: globex")
        self.assertEqual(_with_token(token, lambda: _resolve_server_for_caller(self.mgr, "globex")), "globex")
        self.assertEqual(_with_token(token, lambda: _resolve_server_for_caller(self.mgr, None)), "acme")  # resolve_server alone: global default; the tools pass _default_server_for_caller first

    def test_through_a_tool_call(self):
        mcp = MCPServer(name="test")
        _register_tools(mcp, self.mgr)
        token = NS(id="t", name="t", allowed_servers=["globex"], scopes=["*"], read_only=False, revoked=False,
                   expires_at=None, allowed_ips=None, is_legacy=False)
        with self.assertRaises(Exception) as ctx:
            _with_token(token, lambda: asyncio.run(mcp.call_tool("host_get", {"server": "acme", "limit": 1})))
        self.assertIn("Available: globex", str(ctx.exception))
        self.assertNotIn("acme, globex", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
