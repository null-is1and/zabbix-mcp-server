#!/usr/bin/env python3

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

"""ISO 8601 -> Unix timestamp conversion, on every path a value can take.

GitHub issue #94: ``_try_parse_timestamp`` only knew six ``strptime``
formats, so ``2026-04-01T08:00:00.000Z`` (JavaScript ``toISOString``),
``2026-04-01T08:00:00.123456`` (Python ``isoformat``) and
``2026-04-01 08:00:00+02:00`` went to Zabbix as strings.  Verifying the
report showed the conversion was also unreachable on most paths: typed
``time_from`` / ``time_till`` parameters were declared ``int`` so the
schema validation refused every ISO string before the normaliser ran,
``extra_params`` were merged after it, ``zabbix_raw_api_call`` never
called it, and nested fields (``timeperiods[].start_date``,
``excluded_downtimes[].period_from``, ``history.push`` ``clock``) were
never visited.  The tests below drive the real registered tools through
``MCPServer.call_tool`` so the schema validation is part of what is
tested, and assert on the parameters that reach the Zabbix client.
"""

import asyncio
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "src")

from zabbix_mcp.config import AppConfig, ServerConfig, ZabbixServerConfig  # noqa: E402
from zabbix_mcp.client import ClientManager  # noqa: E402
from zabbix_mcp.server import (  # noqa: E402
    _TIMESTAMP_FIELDS,
    _normalize_timestamps,
    _register_tools,
    _try_parse_timestamp,
)

T0 = 1775030400  # 2026-04-01T08:00:00Z


class TestParser(unittest.TestCase):
    """``_try_parse_timestamp`` on the shapes LLMs and SDKs actually produce."""

    def test_reporter_cases_from_issue_94(self):
        for value in (
            "2026-04-01T08:00:00Z",
            "2026-04-01T08:00:00.000Z",
            "2026-04-01T08:00:00.000+00:00",
            "2026-04-01T08:00:00.123456",
            "2026-04-01T08:00:00.123456Z",
            "2026-04-01 08:00:00",
            "2026-04-01T08:00:00",
            "2026-04-01T08:00",
            "2026-04-01 08:00",
            "2026-04-01t08:00:00z",
        ):
            with self.subTest(value=value):
                self.assertEqual(_try_parse_timestamp(value), T0)

    def test_offsets_are_honoured(self):
        self.assertEqual(_try_parse_timestamp("2026-04-01 08:00:00+02:00"), T0 - 7200)
        self.assertEqual(_try_parse_timestamp("2026-04-01T08:00:00+0200"), T0 - 7200)
        self.assertEqual(_try_parse_timestamp("2026-04-01T08:00:00-05:00"), T0 + 18000)

    def test_naive_values_are_utc(self):
        # Keep the pre-existing contract: no zone means UTC, not the server's zone.
        self.assertEqual(_try_parse_timestamp("2026-04-01"), 1775001600)
        self.assertEqual(_try_parse_timestamp("2026-04-01T08:00:00"), T0)

    def test_whitespace_is_tolerated(self):
        self.assertEqual(_try_parse_timestamp("  2026-04-01T08:00:00Z\n"), T0)

    def test_fractional_seconds_are_truncated_not_rounded(self):
        self.assertEqual(_try_parse_timestamp("2026-04-01T08:00:00.999Z"), T0)

    def test_non_dates_return_none(self):
        for value in ("", "   ", "not-a-date", "now", "now-1h", "1d", "-1h",
                      "April 1st 2026", "Z", "T08:00:00Z", "2026-13-01", "2026-04-31"):
            with self.subTest(value=value):
                self.assertIsNone(_try_parse_timestamp(value))


class TestNormalizer(unittest.TestCase):
    """``_normalize_timestamps`` is recursive and never mutates its input."""

    def test_field_list_covers_the_zabbix_timestamp_parameters(self):
        for name in ("active_since", "active_till", "time_from", "time_till",
                     "period_from", "period_to", "lastChangeSince", "lastChangeTill",
                     "expires_at", "effective_date", "start_date", "clock"):
            self.assertIn(name, _TIMESTAMP_FIELDS)

    def test_passthrough_values(self):
        params = {"time_from": T0, "time_till": "1775030400", "clock": True,
                  "active_since": "not-a-date", "active_till": None, "name": "2026-04-01"}
        self.assertIs(_normalize_timestamps(params), params)

    def test_float_and_stringified_float(self):
        out = _normalize_timestamps({"time_from": 1775030400.0, "time_till": "1775030400.0"})
        self.assertEqual(out, {"time_from": T0, "time_till": T0})
        self.assertIsInstance(out["time_from"], int)
        self.assertIsInstance(out["time_till"], int)

    def test_input_is_not_mutated(self):
        params = {"active_since": "2026-04-01T08:00:00.000Z",
                  "timeperiods": [{"start_date": "2026-04-01T08:00:00Z"}]}
        snapshot = json.loads(json.dumps(params))
        out = _normalize_timestamps(params)
        self.assertEqual(params, snapshot)
        self.assertEqual(out["active_since"], T0)
        self.assertEqual(out["timeperiods"][0]["start_date"], T0)
        self.assertIsNot(out["timeperiods"], params["timeperiods"])

    def test_untouched_branches_keep_identity(self):
        groups = [{"groupid": "2"}]
        out = _normalize_timestamps({"active_since": "2026-04-01T08:00:00Z", "groups": groups})
        self.assertIs(out["groups"], groups)

    def test_nested_maintenance_timeperiods(self):
        out = _normalize_timestamps({
            "name": "m", "active_since": "2026-04-01T08:00:00.000Z", "active_till": "2026-04-02T08:00:00.000Z",
            "timeperiods": [
                {"timeperiod_type": 0, "start_date": "2026-04-01T08:00:00Z", "period": 3600},
                {"timeperiod_type": 2, "every": 1, "start_time": 3600, "period": 3600},
            ],
        })
        self.assertEqual(out["active_since"], T0)
        self.assertEqual(out["active_till"], T0 + 86400)
        self.assertEqual(out["timeperiods"][0]["start_date"], T0)
        self.assertEqual(out["timeperiods"][1]["start_time"], 3600)  # not a timestamp field

    def test_nested_sla_excluded_downtimes(self):
        out = _normalize_timestamps({
            "effective_date": "2026-04-01",
            "excluded_downtimes": [{"name": "x", "period_from": "2026-04-01T08:00:00Z",
                                    "period_to": "2026-04-01T09:00:00Z"}],
        })
        self.assertEqual(out["effective_date"], 1775001600)
        self.assertEqual(out["excluded_downtimes"][0]["period_from"], T0)
        self.assertEqual(out["excluded_downtimes"][0]["period_to"], T0 + 3600)

    def test_top_level_list_history_push(self):
        out = _normalize_timestamps([{"itemid": "1", "clock": "2026-04-01T08:00:00.000Z", "ns": 0, "value": "1"},
                                     {"itemid": "1", "clock": T0, "ns": 0, "value": "2"}])
        self.assertEqual([e["clock"] for e in out], [T0, T0])

    def test_list_under_a_timestamp_key(self):
        out = _normalize_timestamps({"filter": {"clock": ["2026-04-01T08:00:00Z", "1775030400", T0]}})
        self.assertEqual(out["filter"]["clock"], [T0, "1775030400", T0])

    def test_output_and_sortfield_named_clock_are_not_values(self):
        params = {"output": ["clock", "value"], "sortfield": "clock", "search": {"name": "2026-04-01"}}
        self.assertIs(_normalize_timestamps(params), params)


class TestThroughRegisteredTools(unittest.TestCase):
    """Every path through ``MCPServer.call_tool`` - schema validation included."""

    @classmethod
    def setUpClass(cls):
        from mcp.server.mcpserver import MCPServer

        # Pin the zone to UTC: since v1.38 a date-time without a zone is read
        # in the resolved zone (tests/test_timezones.py covers that); the
        # expectations below are about the parsing, so keep them in UTC.
        cfg = AppConfig(server=ServerConfig(timezone="UTC"), zabbix_servers={"t": ZabbixServerConfig(
            name="t", url="http://localhost", api_token="x", read_only=False)})
        cls.mgr = ClientManager(cfg)
        cls.mcp = MCPServer(name="test")
        _register_tools(cls.mcp, cls.mgr)

    def _call(self, tool, args):
        """Call *tool* through the server; return the params that reached the client."""
        sent = []

        def fake_call(server, method, params, *a, **k):
            sent.append((method, params))
            return []

        with patch.object(self.mgr, "call", side_effect=fake_call), \
                patch.object(self.mgr, "get_version", return_value="7.4.0"):
            asyncio.run(self.mcp.call_tool(tool, args))
        self.assertEqual(len(sent), 1, sent)
        return sent[0][1]

    def _schema(self, tool, param):
        tool_def = next(t for t in self.mcp._tool_manager.list_tools() if t.name == tool)
        return tool_def.parameters["properties"][param]

    def test_typed_timestamp_params_accept_int_and_string(self):
        for tool in ("history_get", "trend_get", "event_get", "problem_get", "alert_get", "auditlog_get"):
            for param in ("time_from", "time_till"):
                with self.subTest(tool=tool, param=param):
                    types = {opt.get("type") for opt in self._schema(tool, param)["anyOf"]}
                    self.assertEqual(types, {"integer", "string", "null"})
        for param in ("period_from", "period_to"):
            types = {opt.get("type") for opt in self._schema("sla_getsli", param)["anyOf"]}
            self.assertEqual(types, {"integer", "string", "null"})

    def test_typed_get_params_convert(self):
        """Before: ``Input should be a valid integer`` for any ISO string, even ``...Z``."""
        cases = {
            "history_get": ({"itemids": ["1"], "time_from": "2026-04-01T08:00:00.000Z",
                             "time_till": "2026-04-01 08:00:00+02:00"}, T0, T0 - 7200),
            "trend_get": ({"itemids": ["1"], "time_from": "2026-04-01T08:00:00Z", "time_till": T0}, T0, T0),
            "event_get": ({"time_from": "2026-04-01T08:00:00.123456", "time_till": "2026-04-01T08:00:00Z"}, T0, T0),
            "problem_get": ({"time_from": "2026-04-01T08:00", "time_till": 1775030400.0}, T0, T0),
            "alert_get": ({"time_from": "2026-04-01T08:00:00+00:00", "time_till": "2026-04-01"}, T0, 1775001600),
            "auditlog_get": ({"time_from": "2026-04-01T08:00:00Z", "time_till": "2026-04-01T06:00:00Z"}, T0, T0 - 7200),
        }
        for tool, (args, want_from, want_till) in cases.items():
            with self.subTest(tool=tool):
                sent = self._call(tool, args)
                self.assertEqual(sent["time_from"], want_from)
                self.assertEqual(sent["time_till"], want_till)
                self.assertIsInstance(sent["time_from"], int)
                self.assertIsInstance(sent["time_till"], int)

    def test_sla_getsli_period_params_convert(self):
        sent = self._call("sla_getsli", {"slaid": "1", "period_from": "2026-04-01T08:00:00Z",
                                         "period_to": "2026-04-02T08:00:00.000Z"})
        self.assertEqual((sent["period_from"], sent["period_to"]), (T0, T0 + 86400))

    def test_numeric_string_stays_numeric_string(self):
        sent = self._call("problem_get", {"time_from": "1775030400"})
        self.assertEqual(sent["time_from"], "1775030400")

    def test_unparseable_string_is_left_for_zabbix(self):
        sent = self._call("problem_get", {"time_from": "not-a-date"})
        self.assertEqual(sent["time_from"], "not-a-date")

    def test_extra_params_convert(self):
        """Before: extra_params were merged after the normaliser ran - nothing converted."""
        sent = self._call("trigger_get", {"extra_params": {
            "lastChangeSince": "2026-04-01T08:00:00Z", "lastChangeTill": "2026-04-01T08:00:00.000Z"}})
        self.assertEqual((sent["lastChangeSince"], sent["lastChangeTill"]), (T0, T0))
        sent = self._call("event_get", {"extra_params": {"time_from": "2026-04-01T08:00"}})
        self.assertEqual(sent["time_from"], T0)

    def test_typed_param_wins_over_extra_params(self):
        sent = self._call("problem_get", {"time_from": "2026-04-01T08:00:00Z",
                                          "extra_params": {"time_from": "2000-01-01"}})
        self.assertEqual(sent["time_from"], T0)

    def test_create_params_convert_at_depth(self):
        sent = self._call("maintenance_create", {"params": {
            "name": "m", "active_since": "2026-04-01T08:00:00.000Z", "active_till": "2026-04-02T08:00:00.000Z",
            "groups": [{"groupid": "2"}],
            "timeperiods": [{"timeperiod_type": 0, "start_date": "2026-04-01T08:00:00Z", "period": 3600}]}})
        self.assertEqual((sent["active_since"], sent["active_till"]), (T0, T0 + 86400))
        self.assertEqual(sent["timeperiods"][0]["start_date"], T0)

        sent = self._call("token_create", {"params": {"name": "t", "userid": "1",
                                                      "expires_at": "2026-04-01 08:00:00+02:00"}})
        self.assertEqual(sent["expires_at"], T0 - 7200)

        sent = self._call("sla_create", {"params": {
            "name": "s", "slo": 99.9, "period": 1, "timezone": "UTC", "effective_date": "2026-04-01",
            "service_tags": [{"tag": "a"}],
            "excluded_downtimes": [{"name": "x", "period_from": "2026-04-01T08:00:00Z",
                                    "period_to": "2026-04-01T09:00:00Z"}]}})
        self.assertEqual(sent["effective_date"], 1775001600)
        self.assertEqual(sent["excluded_downtimes"][0]["period_to"], T0 + 3600)

    def test_history_push_clock_converts(self):
        sent = self._call("history_push", {"items": [
            {"itemid": "1", "clock": "2026-04-01T08:00:00.000Z", "ns": 0, "value": "1"}]})
        self.assertEqual(sent, [{"itemid": "1", "clock": T0, "ns": 0, "value": "1"}])

    def test_raw_api_call_converts(self):
        """Before: zabbix_raw_api_call forwarded params untouched."""
        sent = self._call("zabbix_raw_api_call", {"method": "problem.get", "params": {
            "time_from": "2026-04-01T08:00:00.000Z", "filter": {"clock": ["2026-04-01T08:00:00Z", "1"]}}})
        self.assertEqual(sent, {"time_from": T0, "filter": {"clock": [T0, "1"]}})


if __name__ == "__main__":
    unittest.main()
