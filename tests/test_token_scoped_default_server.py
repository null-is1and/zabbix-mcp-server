#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""A token scoped to one server should not have to be told which one (#74).

Give each person their own `[zabbix.<user>]` and a `[tokens.<user>]`
restricted to it: queries run under their Zabbix identity. That only
works if a call omitting `server` lands on the one server the token
can reach - the global default is the first server in the config, which
produced "Token 'bob' is not authorized for server 'alice'" for everyone
but the first person.
"""

from __future__ import annotations

import unittest

from zabbix_mcp.server import _default_server_for_caller
from zabbix_mcp.token_store import TokenInfo, current_token_info


class _CM:
    def __init__(self, names, default):
        self.server_names = list(names)
        self.default_server = default


def _token(allowed):
    return TokenInfo(id="t", name="t", token_hash="sha256:x", allowed_servers=allowed)


class TestDefaultServerForCaller(unittest.TestCase):

    def setUp(self):
        current_token_info.set(None)

    def tearDown(self):
        current_token_info.set(None)

    def _cm(self, default="prod"):
        return _CM(("prod", "alice", "bob"), default)

    def test_single_allowed_server_becomes_the_default(self):
        current_token_info.set(_token(["bob"]))
        self.assertEqual(_default_server_for_caller(self._cm()), "bob")

    def test_wildcard_keeps_the_global_default(self):
        current_token_info.set(_token(["*"]))
        self.assertEqual(_default_server_for_caller(self._cm()), "prod")

    def test_several_allowed_servers_stay_ambiguous(self):
        current_token_info.set(_token(["alice", "bob"]))
        self.assertEqual(_default_server_for_caller(self._cm()), "prod")

    def test_unknown_server_name_falls_back(self):
        # The authorization check reports it; this must not short-circuit
        # into an error the operator cannot read.
        current_token_info.set(_token(["ghost"]))
        self.assertEqual(_default_server_for_caller(self._cm()), "prod")

    def test_no_token_context_falls_back(self):
        # stdio, and resource registration at startup.
        self.assertEqual(_default_server_for_caller(self._cm()), "prod")

    def test_empty_allowed_list_falls_back(self):
        current_token_info.set(_token([]))
        self.assertEqual(_default_server_for_caller(self._cm()), "prod")

    def test_works_without_a_global_default(self):
        current_token_info.set(_token(["alice"]))
        self.assertEqual(_default_server_for_caller(self._cm(default=None)), "alice")

    def test_no_token_and_no_global_default_is_none(self):
        self.assertIsNone(_default_server_for_caller(self._cm(default=None)))


if __name__ == "__main__":
    unittest.main()
