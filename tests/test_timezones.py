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

"""Time zones for rendered and parsed timestamps (GitHub issue #75).

A UK operator asked for "the last hour of problems" at 20:00 BST and got
18:00-19:00: the server returned bare epochs, read a zone-less input as
UTC and never told the model what time it was where the operator sat.
These tests cover the zone resolution chain (token > Zabbix entry >
[server] > Zabbix user > Zabbix frontend default > host), the
``<field>_local`` siblings, the time-zone note on every response, the
zone-less input rule, the Zabbix-side lookup with its cache, and the
config / token validation.
"""

import asyncio
import json
import os
import sys
import unittest
from datetime import timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from zabbix_mcp import timezones  # noqa: E402
from zabbix_mcp.client import ClientManager  # noqa: E402
from zabbix_mcp.config import AppConfig, ConfigError, ServerConfig, ZabbixServerConfig  # noqa: E402
from zabbix_mcp.server import _build_zabbix_params, _format_result, _register_tools, _try_parse_timestamp  # noqa: E402
from zabbix_mcp.token_store import TokenInfo, current_token_info  # noqa: E402

T0 = 1775030400  # 2026-04-01T08:00:00Z = 09:00 BST = 17:00 JST
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")


def _config(server_tz="", zabbix_tz=""):
    return AppConfig(server=ServerConfig(timezone=server_tz), zabbix_servers={"t": ZabbixServerConfig(
        name="t", url="http://localhost", api_token="x", read_only=False, timezone=zabbix_tz)})


class TestZoneHelpers(unittest.TestCase):
    def test_validate_accepts_iana_utc_and_system(self):
        self.assertEqual(timezones.validate_timezone("Europe/London"), "Europe/London")
        self.assertEqual(timezones.validate_timezone(" utc "), "UTC")
        self.assertEqual(timezones.validate_timezone("System"), "system")

    def test_validate_rejects_garbage(self):
        for bad in ("", "   ", "Europe/Londn", "GMT+1 but wrong", "+01:00", "../etc/passwd"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                timezones.validate_timezone(bad)

    def test_load_zone(self):
        self.assertIs(timezones.load_zone(None), timezone.utc)
        self.assertIs(timezones.load_zone("UTC"), timezone.utc)
        self.assertEqual(timezones.zone_name(timezones.load_zone("Europe/London")), "Europe/London")
        self.assertIsNotNone(timezones.load_zone("system"))
        self.assertIs(timezones.load_zone("Nowhere/Nothing"), timezone.utc)  # logged, never raises

    def test_host_zone_follows_tz_env(self):
        with patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
            self.assertEqual(timezones.zone_name(timezones.host_zone()), "Asia/Tokyo")

    def test_format_local(self):
        self.assertEqual(timezones.format_local(T0, LONDON), "2026-04-01T09:00:00+01:00")
        self.assertEqual(timezones.format_local("1775030400", TOKYO), "2026-04-01T17:00:00+09:00")
        self.assertEqual(timezones.format_local(T0, timezone.utc), "2026-04-01T08:00:00+00:00")
        for not_an_epoch in (0, "0", "", None, "abc", [], {}, True):
            with self.subTest(value=not_an_epoch):
                self.assertIsNone(timezones.format_local(not_an_epoch, LONDON))

    def test_add_local_times_at_depth_without_mutation(self):
        data = [{"eventid": "1", "clock": "1775030400", "r_clock": "0", "ns": "123",
                 "tags": [{"tag": "clock", "value": "1775030400"}],
                 "trigger": {"lastchange": T0, "description": "x"}}]
        snapshot = json.loads(json.dumps(data))
        out = timezones.add_local_times(data, LONDON)
        self.assertEqual(data, snapshot)
        row = out[0]
        self.assertEqual(row["clock_local"], "2026-04-01T09:00:00+01:00")
        self.assertNotIn("r_clock_local", row)          # 0 = never
        self.assertNotIn("ns_local", row)
        self.assertNotIn("value_local", row["tags"][0])  # "clock" as a tag *name* is not a field
        self.assertEqual(row["trigger"]["lastchange_local"], "2026-04-01T09:00:00+01:00")
        self.assertEqual(list(row)[:3], ["eventid", "clock", "clock_local"])  # sibling sits next to the epoch

    def test_add_local_times_keeps_identity_when_nothing_matches(self):
        data = {"hostids": ["1"], "hosts": [{"host": "a", "status": "0"}]}
        self.assertIs(timezones.add_local_times(data, LONDON), data)

    def test_existing_local_key_is_left_alone(self):
        data = {"clock": T0, "clock_local": "custom"}
        self.assertIs(timezones.add_local_times(data, LONDON), data)

    def test_epoch_field_list_excludes_non_epoch_time_fields(self):
        for name in ("start_time", "period", "ns", "delay", "every", "dayofweek"):
            self.assertNotIn(name, timezones.EPOCH_FIELDS)
        for name in ("clock", "r_clock", "lastchange", "lastclock", "active_since", "expires_at", "start_date"):
            self.assertIn(name, timezones.EPOCH_FIELDS)


class TestResolveZone(unittest.TestCase):
    """The chain, level by level, with a stand-in client manager."""

    class _Mgr:
        def __init__(self, cfg, zabbix_zone="", fail=False):
            self.config = cfg
            self._zone = zabbix_zone
            self._fail = fail
            self.asked = 0

        def get_zabbix_timezone(self, server):
            self.asked += 1
            if self._fail:
                raise RuntimeError("boom")
            return self._zone

    def tearDown(self):
        current_token_info.set(None)

    def test_token_wins(self):
        current_token_info.set(TokenInfo(id="t", name="t", token_hash="sha256:x", timezone="Asia/Tokyo"))
        mgr = self._Mgr(_config(server_tz="UTC", zabbix_tz="Europe/London"), zabbix_zone="America/New_York")
        self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "Asia/Tokyo")
        self.assertEqual(mgr.asked, 0)

    def test_zabbix_entry_beats_server_default(self):
        mgr = self._Mgr(_config(server_tz="UTC", zabbix_tz="Europe/London"), zabbix_zone="America/New_York")
        self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "Europe/London")
        self.assertEqual(mgr.asked, 0)

    def test_server_default_beats_zabbix_lookup(self):
        mgr = self._Mgr(_config(server_tz="Europe/Prague"), zabbix_zone="America/New_York")
        self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "Europe/Prague")
        self.assertEqual(mgr.asked, 0)

    def test_zabbix_lookup_when_nothing_configured(self):
        mgr = self._Mgr(_config(), zabbix_zone="America/New_York")
        self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "America/New_York")
        self.assertEqual(mgr.asked, 1)

    def test_host_zone_when_zabbix_has_nothing(self):
        with patch.dict(os.environ, {"TZ": "Australia/Sydney"}):
            mgr = self._Mgr(_config(), zabbix_zone="")
            self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "Australia/Sydney")

    def test_lookup_failure_never_fails_the_call(self):
        with patch.dict(os.environ, {"TZ": "Australia/Sydney"}):
            mgr = self._Mgr(_config(), fail=True)
            self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "Australia/Sydney")

    def test_browser_reported_zone_ranks_below_the_operator_pins(self):
        """An OAuth token carrying only the browser's zone (implicit) must not beat [zabbix.X] / [server]."""
        current_token_info.set(TokenInfo(id="t", name="t", token_hash="sha256:x", timezone="Asia/Tokyo", timezone_implicit=True))
        mgr = self._Mgr(_config(zabbix_tz="Europe/London"), zabbix_zone="America/New_York")
        self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "Europe/London")
        mgr = self._Mgr(_config(server_tz="UTC"), zabbix_zone="America/New_York")
        self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "UTC")
        # ... but above what Zabbix reports and above the host zone
        mgr = self._Mgr(_config(), zabbix_zone="America/New_York")
        self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "Asia/Tokyo")
        self.assertEqual(mgr.asked, 0)

    def test_token_without_zone_inherits(self):
        current_token_info.set(TokenInfo(id="t", name="t", token_hash="sha256:x"))
        mgr = self._Mgr(_config(zabbix_tz="Europe/London"))
        self.assertEqual(timezones.zone_name(timezones.resolve_zone(mgr, "t")), "Europe/London")


class TestZabbixSideLookup(unittest.TestCase):
    """``ClientManager.get_zabbix_timezone``: user zone, frontend default, refusal, cache."""

    class _User:
        def __init__(self, tz):
            self._tz = tz
            self.calls = []

        def checkAuthentication(self, **kw):
            self.calls.append(kw)
            return {"userid": "1", "timezone": self._tz}

    class _Client:
        _ZabbixAPI__session_id = "tok"

        def api_version(self):
            return "7.4.0"

        def __init__(self, user_tz, default_tz, refuse=False):
            self.user = TestZabbixSideLookup._User(user_tz)
            self._default = default_tz
            self._refuse = refuse
            self.settings_calls = 0

    def _mgr(self, client, legacy=False):
        cfg = AppConfig(server=ServerConfig(), zabbix_servers={"t": ZabbixServerConfig(
            name="t", url="http://localhost", api_token="x", legacy_auth=legacy,
            username="u" if legacy else "", password="p" if legacy else "")})
        mgr = ClientManager(cfg)
        mgr._clients["t"] = client

        def do_call(c, method, params):
            self.assertEqual(method, "settings.get")
            c.settings_calls += 1
            if c._refuse:
                from zabbix_utils.exceptions import ProcessingError
                raise ProcessingError("No permissions to call \"settings.get\".")
            return {"default_timezone": c._default}
        mgr._do_call = do_call
        return mgr

    def test_user_zone_wins_and_is_cached(self):
        client = self._Client("Europe/London", "Asia/Tokyo")
        mgr = self._mgr(client)
        self.assertEqual(mgr.get_zabbix_timezone("t"), "Europe/London")
        self.assertEqual(mgr.get_zabbix_timezone("t"), "Europe/London")
        self.assertEqual(len(client.user.calls), 1)
        self.assertEqual(client.user.calls[0], {"token": "tok"})
        self.assertEqual(client.settings_calls, 0)

    def test_legacy_session_uses_sessionid(self):
        client = self._Client("Europe/London", "system")
        mgr = self._mgr(client, legacy=True)
        mgr.get_zabbix_timezone("t")
        self.assertEqual(client.user.calls[0], {"sessionid": "tok"})

    def test_user_default_falls_to_frontend_default(self):
        mgr = self._mgr(self._Client("default", "Asia/Tokyo"))
        self.assertEqual(mgr.get_zabbix_timezone("t"), "Asia/Tokyo")

    def test_frontend_system_means_nothing_explicit(self):
        mgr = self._mgr(self._Client("default", "system"))
        self.assertEqual(mgr.get_zabbix_timezone("t"), "")

    def test_refusal_is_cached_as_empty(self):
        client = self._Client("default", "Asia/Tokyo", refuse=True)
        mgr = self._mgr(client)
        self.assertEqual(mgr.get_zabbix_timezone("t"), "")
        self.assertEqual(mgr.get_zabbix_timezone("t"), "")
        self.assertEqual(client.settings_calls, 1)

    def test_library_wrapped_connection_failure_is_not_cached(self):
        """zabbix_utils wraps URLError into ProcessingError('Unable to connect ...')."""
        from zabbix_utils.exceptions import ProcessingError
        cfg = AppConfig(server=ServerConfig(), zabbix_servers={"t": ZabbixServerConfig(
            name="t", url="http://localhost", api_token="x")})
        mgr = ClientManager(cfg)
        with patch.object(mgr, "_get_client", side_effect=ProcessingError("Unable to connect to http://localhost:", "x")):
            self.assertEqual(mgr.get_zabbix_timezone("t"), "")
        self.assertNotIn("t", mgr._zabbix_zones)
        # ... and the same when the connection dies inside settings.get
        client = self._Client("default", "Asia/Tokyo")
        mgr = self._mgr(client)
        mgr._do_call = lambda c, m, p: (_ for _ in ()).throw(ProcessingError("Unable to connect to http://localhost:", "x"))
        self.assertEqual(mgr.get_zabbix_timezone("t"), "")
        self.assertNotIn("t", mgr._zabbix_zones)

    def test_connection_failure_is_not_cached(self):
        cfg = AppConfig(server=ServerConfig(), zabbix_servers={"t": ZabbixServerConfig(
            name="t", url="http://localhost", api_token="x")})
        mgr = ClientManager(cfg)
        with patch.object(mgr, "_get_client", side_effect=ConnectionError("down")):
            self.assertEqual(mgr.get_zabbix_timezone("t"), "")
        self.assertNotIn("t", mgr._zabbix_zones)

    def test_reconnect_drops_the_cache(self):
        client = self._Client("Europe/London", "system")
        mgr = self._mgr(client)
        mgr.get_zabbix_timezone("t")
        self.assertIn("t", mgr._zabbix_zones)
        with patch.object(mgr, "_connect", return_value=client):
            mgr._reconnect("t")
        self.assertNotIn("t", mgr._zabbix_zones)


class TestInputInZone(unittest.TestCase):
    def test_naive_input_is_read_in_the_zone(self):
        self.assertEqual(_try_parse_timestamp("2026-04-01 09:00:00", LONDON), T0)
        self.assertEqual(_try_parse_timestamp("2026-04-01T17:00", TOKYO), T0)
        self.assertEqual(_try_parse_timestamp("2026-04-01 08:00:00"), T0)  # no zone given -> UTC, as before

    def test_explicit_zone_and_epoch_are_untouched_by_the_zone(self):
        self.assertEqual(_try_parse_timestamp("2026-04-01T08:00:00Z", TOKYO), T0)
        self.assertEqual(_try_parse_timestamp("2026-04-01T10:00:00+02:00", TOKYO), T0)
        md = _MD("problem.get", "problem_get", read_only=True)
        out = _build_zabbix_params(md, {"time_from": 1775030400, "time_till": "1775030400"}, tz=TOKYO)
        self.assertEqual((out["time_from"], out["time_till"]), (T0, "1775030400"))

    def test_local_siblings_are_stripped_from_update_payloads(self):
        """A get result edited and sent back must not carry the rendered *_local fields."""
        md = _MD("maintenance.update", "maintenance_update", read_only=False, params_dict=True)
        out = _build_zabbix_params(md, {"params": {
            "maintenanceid": "1", "active_since": "1775030400", "active_since_local": "2026-04-01T09:00:00+01:00",
            "timeperiods": [{"start_date": "1775030400", "start_date_local": "2026-04-01T09:00:00+01:00", "period": 3600}],
            "name_local": "keep: not an epoch sibling"}}, tz=LONDON)
        self.assertNotIn("active_since_local", out)
        self.assertNotIn("start_date_local", out["timeperiods"][0])
        self.assertEqual(out["timeperiods"][0]["period"], 3600)
        self.assertEqual(out["name_local"], "keep: not an epoch sibling")

    def test_build_params_threads_the_zone_everywhere(self):
        md = _MD("maintenance.create", "maintenance_create", read_only=False, params_dict=True)
        out = _build_zabbix_params(md, {"params": {
            "active_since": "2026-04-01 09:00:00", "timeperiods": [{"start_date": "2026-04-01 09:00"}]}}, tz=LONDON)
        self.assertEqual(out["active_since"], T0)
        self.assertEqual(out["timeperiods"][0]["start_date"], T0)
        md = _MD("trigger.get", "trigger_get", read_only=True)
        out = _build_zabbix_params(md, {"extra_params": {"lastChangeSince": "2026-04-01 17:00:00"}}, tz=TOKYO)
        self.assertEqual(out["lastChangeSince"], T0)


def _MD(api_method, tool_name, *, read_only, params_dict=False):
    from zabbix_mcp.api.types import MethodDef, ParamDef
    if params_dict:
        params = [ParamDef("params", "dict", "d", required=True)]
    else:
        params = [ParamDef("output", "str", "d"), ParamDef("time_from", "timestamp", "d"),
                  ParamDef("time_till", "timestamp", "d"), ParamDef("extra_params", "dict", "d")]
    return MethodDef(api_method, tool_name, "d", read_only=read_only, params=params)


class TestResponseShape(unittest.TestCase):
    def test_note_follows_the_preamble_and_is_dropped_for_raw_json(self):
        out = _format_result("[]", False, tz=LONDON)
        lines = out.split("\n")
        # One bracketed line, so a consumer stripping the first "[System: ...]"
        # line keeps parsing; the zone note rides inside it.
        self.assertTrue(lines[0].startswith("[System: The following is raw data from Zabbix."))
        self.assertIn(" Time zone: Europe/London; now 20", lines[0])
        self.assertTrue(lines[0].endswith("date-times sent without a zone use this zone.]"))
        self.assertEqual(lines[1], "[]")
        self.assertEqual(_format_result("[]", True, tz=LONDON), "[]")
        self.assertNotIn("Time zone", _format_result("[]", False))


class TestThroughRegisteredTools(unittest.TestCase):
    """The whole chain through ``MCPServer.call_tool``: the reporter's case."""

    def tearDown(self):
        current_token_info.set(None)

    def _server(self, cfg, rows=None):
        from mcp.server.mcpserver import MCPServer
        mgr = ClientManager(cfg)
        mcp = MCPServer(name="test")
        _register_tools(mcp, mgr)
        sent = []

        def fake_call(server, method, params, *a, **k):
            sent.append((method, params))
            return rows if rows is not None else [{"eventid": "9", "clock": str(T0), "r_clock": "0", "name": "disk"}]

        async def call(tool, args):
            sent.clear()
            with patch.object(mgr, "call", side_effect=fake_call), \
                    patch.object(mgr, "get_version", return_value="7.4.0"), \
                    patch.object(mgr, "get_zabbix_timezone", return_value="Europe/London"), \
                    patch.object(mgr, "check_connection", return_value={}):
                res = await mcp.call_tool(tool, args)
            text = "\n".join(c.text for c in res.content if hasattr(c, "text"))
            return (sent[-1][1] if sent else None), text
        return call

    def test_uk_operator_last_hour(self):
        """Zone comes from Zabbix (the user's own Europe/London); 'from 09:00' means 09:00 BST."""
        call = self._server(_config())
        params, text = asyncio.run(call("problem_get", {"time_from": "2026-04-01 09:00:00", "limit": 5}))
        self.assertEqual(params["time_from"], T0)
        lines = text.split("\n")
        self.assertIn("Time zone: Europe/London; now ", lines[0])
        body = json.loads("\n".join(lines[1:]))
        self.assertEqual(body[0]["clock"], str(T0))
        self.assertEqual(body[0]["clock_local"], "2026-04-01T09:00:00+01:00")
        self.assertNotIn("r_clock_local", body[0])

    def test_token_zone_overrides_everything(self):
        current_token_info.set(TokenInfo(id="t", name="t", token_hash="sha256:x", timezone="Asia/Tokyo"))
        call = self._server(_config(server_tz="UTC", zabbix_tz="Europe/London"))
        params, text = asyncio.run(call("problem_get", {"time_from": "2026-04-01 17:00:00"}))
        self.assertEqual(params["time_from"], T0)
        self.assertIn("Time zone: Asia/Tokyo; now ", text)
        self.assertIn('"clock_local": "2026-04-01T17:00:00+09:00"', text)

    def test_zabbix_entry_zone(self):
        call = self._server(_config(zabbix_tz="America/New_York"))
        params, text = asyncio.run(call("event_get", {"time_from": "2026-04-01T04:00:00"}))
        self.assertEqual(params["time_from"], T0)
        self.assertIn('"clock_local": "2026-04-01T04:00:00-04:00"', text)

    def test_raw_api_call_gets_the_same_treatment(self):
        call = self._server(_config(server_tz="Europe/London"))
        params, text = asyncio.run(call("zabbix_raw_api_call", {"method": "event.get", "params": {"time_from": "2026-04-01 09:00"}}))
        self.assertEqual(params["time_from"], T0)
        self.assertIn("Time zone: Europe/London; now ", text)
        self.assertIn("clock_local", text)

    def test_nested_objects_get_local_fields(self):
        rows = [{"maintenanceid": "1", "active_since": str(T0), "timeperiods": [{"start_date": str(T0), "start_time": "3600"}]}]
        call = self._server(_config(server_tz="Europe/London"), rows=rows)
        _, text = asyncio.run(call("maintenance_get", {}))
        body = json.loads("\n".join(text.split("\n")[1:]))
        self.assertEqual(body[0]["active_since_local"], "2026-04-01T09:00:00+01:00")
        self.assertEqual(body[0]["timeperiods"][0]["start_date_local"], "2026-04-01T09:00:00+01:00")
        self.assertNotIn("start_time_local", body[0]["timeperiods"][0])

    def test_extension_tool_gets_local_fields_and_the_note(self):
        rows = [{"eventid": "9", "objectid": "1", "clock": str(T0), "name": "disk", "severity": "4", "acknowledged": "0"}]
        call = self._server(_config(server_tz="Europe/London"), rows=rows)
        _, text = asyncio.run(call("problem_active_get", {}))
        self.assertIn("Time zone: Europe/London; now ", text.split("\n")[0])
        body = json.loads("\n".join(text.split("\n")[1:]))
        self.assertIn("problems", body)

    def test_health_check_reports_zone_and_local_time(self):
        call = self._server(_config(server_tz="Asia/Tokyo"))
        _, text = asyncio.run(call("health_check", {}))
        body = json.loads(text)
        srv = body["zabbix_servers"]["server_1"]
        self.assertEqual(srv["timezone"], "Asia/Tokyo")
        self.assertRegex(srv["now_local"], r"\+09:00$")


class TestConfigValidation(unittest.TestCase):
    def test_server_and_zabbix_timezone_are_validated(self):
        from zabbix_mcp.config import _validate_timezone_setting
        self.assertEqual(_validate_timezone_setting("", "x"), "")
        self.assertEqual(_validate_timezone_setting(None, "x"), "")
        self.assertEqual(_validate_timezone_setting("Europe/London", "x"), "Europe/London")
        with self.assertRaises(ConfigError) as ctx:
            _validate_timezone_setting("Mars/Olympus", "[zabbix.prod].timezone")
        self.assertIn("[zabbix.prod].timezone", str(ctx.exception))
        with self.assertRaises(ConfigError):
            _validate_timezone_setting(123, "x")

    def test_token_timezone_parsed_and_bad_value_ignored(self):
        from zabbix_mcp.token_store import TokenStore
        store = TokenStore()
        with self.assertLogs("zabbix_mcp.token_store", level="WARNING") as logs:
            store.load_from_config({
                "good": {"name": "g", "token_hash": "sha256:" + "a" * 64, "timezone": "Europe/London"},
                "bad": {"name": "b", "token_hash": "sha256:" + "b" * 64, "timezone": "Nowhere/Nothing"},
                "none": {"name": "n", "token_hash": "sha256:" + "c" * 64},
            })
        by_id = {t.id: t for t in store.list_tokens()}
        self.assertEqual(by_id["good"].timezone, "Europe/London")
        self.assertIsNone(by_id["bad"].timezone)
        self.assertIsNone(by_id["none"].timezone)
        self.assertTrue(any("Nowhere/Nothing" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
