#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""A broken weasyprint install must disable reports, not the server.

Reported in PR #85 (@azullus): when weasyprint's Python package is
present but a native library it dlopen()s at import time is missing
(libpangoft2, GDK-PixBuf), ``import weasyprint`` raises ``OSError``,
not ``ImportError``. Both guards only caught ``ImportError``, so the
exception escaped module import and took the whole MCP server down at
startup - for a feature that is optional.

Both guards are driven here as the real code paths: the engine module
is re-imported under a failing import, and ``_register_tools`` is run
against an engine module whose attribute access raises.
"""

from __future__ import annotations

import builtins
import importlib
import sys
import types
import unittest
from unittest import mock

from tests.test_smoke import _make_config
from zabbix_mcp.client import ClientManager
from zabbix_mcp.server import _register_tools

ENGINE = "zabbix_mcp.reporting.engine"


class TestEngineImportGuard(unittest.TestCase):

    def _reload_engine_with(self, exc: BaseException | None):
        real_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if exc is not None and name == "weasyprint":
                raise exc
            return real_import(name, *args, **kwargs)

        sys.modules.pop(ENGINE, None)
        with mock.patch.object(builtins, "__import__", side_effect=failing_import):
            return importlib.import_module(ENGINE)

    def tearDown(self):
        # Leave the real module behind for every other test.
        sys.modules.pop(ENGINE, None)
        importlib.import_module(ENGINE)

    def test_missing_native_library_disables_reporting_instead_of_raising(self):
        # The bug: this used to propagate out of the import.
        with self.assertLogs("zabbix_mcp.reporting", level="WARNING") as logs:
            engine = self._reload_engine_with(
                OSError("cannot load library 'libpangoft2-1.0-0'"))
        self.assertFalse(engine.REPORTING_AVAILABLE)
        self.assertTrue(any("PDF reporting disabled" in m for m in logs.output))
        self.assertTrue(any("libpangoft2" in m for m in logs.output),
                        "the operator needs to see WHICH library is missing")

    def test_missing_package_still_disables_reporting(self):
        # The pre-existing path must keep working alongside the new one.
        engine = self._reload_engine_with(ImportError("No module named 'weasyprint'"))
        self.assertFalse(engine.REPORTING_AVAILABLE)


class TestServerImportGuard(unittest.TestCase):

    def test_register_tools_survives_an_engine_that_raises_oserror(self):
        from mcp.server.mcpserver import MCPServer

        class _BrokenEngine(types.ModuleType):
            def __getattr__(self, name):
                raise OSError("cannot load library 'libgobject-2.0-0'")

        broken = _BrokenEngine(ENGINE)
        with mock.patch.dict(sys.modules, {ENGINE: broken}):
            mcp = MCPServer(name="guard-test")
            # Before #85 this raised OSError straight out of registration.
            count = _register_tools(mcp, ClientManager(_make_config()))
        names = {t.name for t in mcp._tool_manager.list_tools()}
        self.assertGreater(count, 200, "the Zabbix tools must still be registered")
        self.assertNotIn("report_generate", names,
                         "reporting must be disabled, not half-registered")


if __name__ == "__main__":
    unittest.main()
