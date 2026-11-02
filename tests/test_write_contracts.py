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

"""The measured write contract (api/write_contracts.py): what the server
drops from a payload copied out of a get tool before X.update / X.create,
and the hand-written state-dependent rules next to it."""

import asyncio
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from zabbix_mcp.api import ALL_METHODS  # noqa: E402
from zabbix_mcp.api import write_contracts as wc  # noqa: E402
from zabbix_mcp.api.contracts import CONTRACTS, OBJECTS  # noqa: E402
from zabbix_mcp.client import ClientManager  # noqa: E402
from zabbix_mcp.config import AppConfig, ServerConfig, ZabbixServerConfig  # noqa: E402
from zabbix_mcp.server import _register_tools, _sanitize_create_params  # noqa: E402

SYNTHETIC = {
    "item.update": (
        wc.StripRule(("templateid",)), wc.StripRule(("flags",)), wc.StripRule(("hosts",)),
        wc.StripRule(("hostid",)), wc.StripRule(("lastclock",), releases=("7.0", "7.2", "7.4")),
        wc.StripRule(("tags[]", "automatic")),
        wc.StripRule(("preprocessing[]", "item_preprocid"), releases=("6.0", "6.2", "6.4")),
    ),
    "item.create": (wc.StripRule(("templateid",)), wc.StripRule(("flags",)), wc.StripRule(("hosts",))),
    "usergroup.update": (wc.StripRule(("users[]", "name"),), wc.StripRule(("users[]", "surname"),),
                         wc.StripRule(("rights",), kind="deprecated", only_with="hostgroup_rights")),
}


class TestStrip(unittest.TestCase):
    def setUp(self):
        self._patch = patch.dict(wc.WRITE_CONTRACTS, SYNTHETIC, clear=True)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_drops_top_level_nested_and_array_keys_in_place(self):
        payload = {"itemid": "1", "name": "x", "templateid": "0", "flags": "0", "hostid": "10",
                   "hosts": [{"hostid": "10", "name": "h"}],
                   "tags": [{"tag": "a", "value": "1", "automatic": "0"}, {"tag": "b", "value": "2", "automatic": "1"}],
                   "preprocessing": [{"type": "1", "params": "2", "item_preprocid": "5"}]}
        dropped = wc.strip_for_write("item.update", payload, "6.4.21")
        self.assertEqual(payload, {"itemid": "1", "name": "x",
                                   "tags": [{"tag": "a", "value": "1"}, {"tag": "b", "value": "2"}],
                                   "preprocessing": [{"type": "1", "params": "2"}]})
        self.assertEqual(dropped, ["templateid", "flags", "hosts", "hostid", "tags[].automatic",
                                   "preprocessing[].item_preprocid"])

    def test_release_markers(self):
        payload = {"itemid": "1", "lastclock": "0", "preprocessing": [{"type": "1", "item_preprocid": "5"}]}
        wc.strip_for_write("item.update", copy.deepcopy(payload), "6.4.21")
        p = copy.deepcopy(payload)
        wc.strip_for_write("item.update", p, "6.4.21")
        self.assertIn("lastclock", p)                              # refused from 7.0 only
        self.assertNotIn("item_preprocid", p["preprocessing"][0])  # refused up to 6.4
        p = copy.deepcopy(payload)
        wc.strip_for_write("item.update", p, "7.4.14")
        self.assertNotIn("lastclock", p)
        self.assertIn("item_preprocid", p["preprocessing"][0])
        p = copy.deepcopy(payload)
        wc.strip_for_write("item.update", p, None)                 # version unknown: universal rules only
        self.assertIn("lastclock", p)
        self.assertIn("item_preprocid", p["preprocessing"][0])
        p = copy.deepcopy(payload)
        wc.strip_for_write("item.update", p, "8.0.0")              # unmeasured release: the same
        self.assertIn("lastclock", p)

    def test_create_keeps_the_fields_only_an_update_refuses(self):
        payload = {"name": "x", "hostid": "10", "templateid": "0", "hosts": []}
        wc.strip_for_write("item.create", payload, "7.4.14")
        self.assertEqual(payload, {"name": "x", "hostid": "10"})

    def test_bulk_list_payload_and_foreign_shapes(self):
        payload = [{"itemid": "1", "flags": "0"}, "not a dict", {"itemid": "2", "flags": "4", "tags": "oops"}]
        wc.strip_for_write("item.update", payload, "7.4.14")
        self.assertEqual(payload, [{"itemid": "1"}, "not a dict", {"itemid": "2", "tags": "oops"}])
        self.assertEqual(wc.strip_for_write("host.update", {"flags": "0"}, "7.4.14"), [])
        self.assertEqual(wc.strip_for_write("item.update", None, "7.4.14"), [])

    def test_same_key_in_many_rows_is_reported_once(self):
        payload = {"usrgrpid": "7", "users": [{"userid": "1", "name": "a", "surname": "b"},
                                               {"userid": "2", "name": "c", "surname": "d"}]}
        dropped = wc.strip_for_write("usergroup.update", payload, "7.4.14")
        self.assertEqual(dropped, ["users[].name", "users[].surname"])
        self.assertEqual(payload["users"], [{"userid": "1"}, {"userid": "2"}])

    def test_deprecated_twin_goes_only_next_to_its_successor(self):
        both = {"usrgrpid": "7", "rights": [{"id": "2", "permission": 3}], "hostgroup_rights": [{"id": "2", "permission": 3}]}
        self.assertEqual(wc.strip_for_write("usergroup.update", both, "7.0.30"), ["rights"])
        self.assertNotIn("rights", both)
        alone = {"usrgrpid": "7", "rights": [{"id": "2", "permission": 3}]}
        self.assertEqual(wc.strip_for_write("usergroup.update", alone, "7.0.30"), [])
        self.assertIn("rights", alone)                       # the old name alone is still accepted
        alone = {"usrgrpid": "7", "rights": []}
        wc.strip_for_write("usergroup.update", alone, "6.0.48")
        self.assertIn("rights", alone)                       # 6.0 has no successor at all

    def test_describe(self):
        self.assertIn("templateid", wc.describe("item.update"))
        self.assertIsNone(wc.describe("host.update"))


class TestShippedContract(unittest.TestCase):
    """The generated file itself: every method exists, every path is well
    formed, every release marker is a known release, and a few facts the
    live audits established on 6.0 through 7.4."""

    def test_methods_and_paths_are_well_formed(self):
        known = {m.api_method for m in ALL_METHODS}
        for method, rules in wc.WRITE_CONTRACTS.items():
            self.assertIn(method, known, method)
            self.assertTrue(method.endswith((".update", ".create")), method)
            for rule in rules:
                self.assertTrue(all(seg and seg != "[]" for seg in rule.path), (method, rule.path))
                for marker in rule.releases:
                    self.assertRegex(marker, r"^\d+\.\d+$")

    def test_a_create_never_drops_a_documented_create_field(self):
        for method, rules in wc.WRITE_CONTRACTS.items():
            if not method.endswith(".create"):
                continue
            obj = method.split(".")[0]
            contract = CONTRACTS.get(f"{obj}.get")
            if not contract:
                continue
            props = OBJECTS[contract.object].props
            for rule in rules:
                if len(rule.path) == 1 and not rule.path[0].endswith("[]"):
                    spec = props.get(rule.path[0])
                    if spec is not None:
                        self.assertTrue(spec.readonly, (method, rule.path[0]))

    def test_measured_facts(self):
        if not wc.WRITE_CONTRACTS:
            self.skipTest("contract not built")
        paths = {m: {".".join(r.path) for r in rules} for m, rules in wc.WRITE_CONTRACTS.items()}
        # (#101 / #102, the nested action identifiers, live in api/action_payload.py)
        # read-only item columns and the select echoes
        for p in ("templateid", "flags", "state", "error", "lastclock", "hosts", "triggers", "discoveryRule"):
            self.assertIn(p, paths["item.update"], p)
        self.assertNotIn("hostid", paths.get("item.create", set()))     # a create needs it
        self.assertIn("hostid", paths["item.update"])
        # the interface quartet Zabbix refuses with a bare "Incorrect arguments"
        for p in ("available", "error", "errors_from", "disable_until"):
            self.assertIn(p, paths["hostinterface.update"], p)
        self.assertIn("interfaces[].available", paths["host.update"])
        self.assertIn("tags[].automatic", paths["host.update"])
        # a user group echoes its users, only userid goes back
        self.assertIn("users[].name", paths["usergroup.update"])
        self.assertNotIn("users[].userid", paths["usergroup.update"])
        # the interface quartet is refused from 7.0 on, 6.x ignores it
        rules = {".".join(r.path): r for r in wc.WRITE_CONTRACTS["hostinterface.update"]}
        self.assertEqual(rules["available"].releases, ("7.0", "7.2", "7.4", "8.0"))
        # a documented writable property is never stripped on an "unexpected" alone
        self.assertNotIn("username", paths.get("script.update", set()))
        self.assertNotIn("filter", paths["discoveryrule.update"])
        # the 7.0 deprecated twins are conditional
        rules = {".".join(r.path): r for r in wc.WRITE_CONTRACTS["mediatype.update"]}
        self.assertEqual(rules["content_type"].only_with, "message_format")   # message_format exists from 7.0 only
        rules = {".".join(r.path): r for r in wc.WRITE_CONTRACTS["usergroup.update"]}
        self.assertEqual(rules["rights"].only_with, "hostgroup_rights")
        # a maintenance time period's fields depend on the period type: never stripped
        self.assertFalse(any(p.startswith("timeperiods[].") for p in paths["maintenance.update"]))
        # the hand-ruled objects are left to api/action_payload.py
        self.assertNotIn("action.update", paths)
        self.assertNotIn("correlation.update", paths)


class _Harness(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = AppConfig(server=ServerConfig(timezone="UTC"), zabbix_servers={"t": ZabbixServerConfig(
            name="t", url="http://localhost", api_token="x", read_only=False)})
        cls.mgr = ClientManager(cfg)
        cls.mcp = MCPServer(name="test")
        _register_tools(cls.mcp, cls.mgr)

    def _call(self, tool, args, version="7.4.14", result=None):
        sent = []

        def fake_call(server, method, params, *a, **k):
            sent.append((method, copy.deepcopy(params)))
            return result if result is not None else {"itemids": ["1"]}

        with patch.object(self.mgr, "call", side_effect=fake_call), \
                patch.object(self.mgr, "get_version", return_value=version):
            asyncio.run(self.mcp.call_tool(tool, args))
        return sent


class TestThroughRegisteredTools(_Harness):
    def setUp(self):
        self._patch = patch.dict(wc.WRITE_CONTRACTS, SYNTHETIC, clear=True)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_update_tool_drops_the_measured_keys(self):
        sent = self._call("item_update", {"params": {
            "itemid": "1", "name": "x", "templateid": "0", "flags": "0", "lastclock": "1",
            "hosts": [{"hostid": "10"}], "tags": [{"tag": "a", "value": "1", "automatic": "0"}]}})
        self.assertEqual(sent, [("item.update", {"itemid": "1", "name": "x", "tags": [{"tag": "a", "value": "1"}]})])

    def test_release_decides_through_the_tool(self):
        sent = self._call("item_update", {"params": {"itemid": "1", "lastclock": "1"}}, version="6.4.21")
        self.assertEqual(sent[0][1], {"itemid": "1", "lastclock": "1"})

    def test_raw_api_call_is_never_touched(self):
        sent = self._call("zabbix_raw_api_call", {"method": "item.update",
                                                  "params": {"itemid": "1", "templateid": "0"}}, result=[])
        self.assertEqual(sent, [("item.update", {"itemid": "1", "templateid": "0"})])

    def test_switch_off(self):
        cfg = AppConfig(server=ServerConfig(timezone="UTC", strip_write_params=False),
                        zabbix_servers={"t": ZabbixServerConfig(name="t", url="http://localhost", api_token="x",
                                                                read_only=False)})
        mgr = ClientManager(cfg)
        mcp = MCPServer(name="off")
        _register_tools(mcp, mgr, strip_write=cfg.server.strip_write_params)
        sent = []
        with patch.object(mgr, "call", side_effect=lambda s, m, p, *a, **k: sent.append((m, p)) or {"itemids": ["1"]}), \
                patch.object(mgr, "get_version", return_value="7.4.14"):
            asyncio.run(mcp.call_tool("item_update", {"params": {"itemid": "1", "templateid": "0"}}))
        self.assertEqual(sent[0][1], {"itemid": "1", "templateid": "0"})


class TestHandRules(_Harness):
    def test_trigger_get_expands_the_expression_by_default(self):
        sent = self._call("trigger_get", {"triggerids": ["1"]}, result=[])
        self.assertTrue(sent[0][1].get("expandExpression"))
        sent = self._call("trigger_get", {"triggerids": ["1"], "extra_params": {"expandExpression": False}}, result=[])
        self.assertFalse(sent[0][1]["expandExpression"])
        sent = self._call("triggerprototype_get", {"discoveryids": ["1"]}, result=[])
        self.assertTrue(sent[0][1].get("expandExpression"))
        sent = self._call("item_get", {"itemids": ["1"]}, result=[])
        self.assertNotIn("expandExpression", sent[0][1])

    def test_lld_filter_formulaid_only_without_custom_expression(self):
        params = {"itemid": "1", "filter": {"evaltype": "0", "eval_formula": "A and B", "formula": "",
                                            "conditions": [{"macro": "{#A}", "value": "x", "formulaid": "A"}]},
                  "overrides": [{"name": "o", "step": "1", "filter": {
                      "evaltype": "3", "formula": "A", "eval_formula": "A",
                      "conditions": [{"macro": "{#B}", "value": "y", "formulaid": "A"}]}}]}
        _sanitize_create_params(params, "discoveryrule.update")
        self.assertEqual(params["filter"], {"evaltype": "0", "formula": "",
                                            "conditions": [{"macro": "{#A}", "value": "x"}]})
        self.assertEqual(params["overrides"][0]["filter"]["conditions"][0]["formulaid"], "A")
        self.assertNotIn("eval_formula", params["overrides"][0]["filter"])

    def test_regexp_delimiter_only_on_delimited_type(self):
        params = {"regexpid": "1", "expressions": [
            {"expression": "a", "expression_type": "0", "exp_delimiter": ","},
            {"expression": "b", "expression_type": "3", "exp_delimiter": ","},
            {"expression": "c", "expression_type": 1, "exp_delimiter": "."}]}
        _sanitize_create_params(params, "regexp.update")
        self.assertNotIn("exp_delimiter", params["expressions"][0])
        self.assertNotIn("exp_delimiter", params["expressions"][1])
        self.assertEqual(params["expressions"][2]["exp_delimiter"], ".")   # type 1 is the delimited one

    def test_maintenance_time_periods_keep_their_own_fields(self):
        returned = {"every": "1", "month": "0", "dayofweek": "0", "day": "0", "start_time": "0",
                    "period": "3600", "start_date": "1577836800"}
        params = {"maintenanceid": "9", "timeperiods": [
            dict(returned, timeperiod_type="0"), dict(returned, timeperiod_type="2"),
            dict(returned, timeperiod_type="3", dayofweek="5"), dict(returned, timeperiod_type="4", month="5", day="3"),
            dict(returned, timeperiod_type="4", month="5", dayofweek="1", every="2"), {"timeperiod_type": "9", "x": 1}]}
        _sanitize_create_params(params, "maintenance.update")
        p = params["timeperiods"]
        self.assertEqual(p[0], {"timeperiod_type": "0", "start_date": "1577836800", "period": "3600"})
        self.assertEqual(p[1], {"timeperiod_type": "2", "every": "1", "start_time": "0", "period": "3600"})
        self.assertEqual(p[2], {"timeperiod_type": "3", "every": "1", "dayofweek": "5", "start_time": "0", "period": "3600"})
        self.assertEqual(p[3], {"timeperiod_type": "4", "every": "1", "month": "5", "day": "3", "dayofweek": "0",
                                "start_time": "0", "period": "3600"})      # 6.0 wants dayofweek present
        self.assertEqual(p[4], {"timeperiod_type": "4", "every": "2", "month": "5", "day": "0", "dayofweek": "1",
                                "start_time": "0", "period": "3600"})
        self.assertEqual(p[5], {"timeperiod_type": "9", "x": 1})      # unknown kind: untouched

    def test_script_fields_follow_type_and_scope(self):
        returned = {"scriptid": "1", "name": "Ping", "command": "ping", "host_access": "2", "usrgrpid": "0",
                    "confirmation": "", "menu_path": "", "execute_on": "2", "timeout": "30s", "port": "",
                    "authtype": "0", "username": "", "password": "", "publickey": "", "privatekey": "",
                    "parameters": [], "url": "", "new_window": "1"}
        p = dict(returned, type="0", scope="1")
        _sanitize_create_params(p, "script.update")
        self.assertEqual(p, {"scriptid": "1", "name": "Ping", "command": "ping", "execute_on": "2", "type": "0", "scope": "1"})
        p = dict(returned, type="2", scope="2", username="root", password="x")
        _sanitize_create_params(p, "script.update")
        self.assertEqual(p["username"], "root")
        self.assertNotIn("timeout", p)
        self.assertEqual(p["host_access"], "2")                       # manual scope keeps its fields
        p = dict(returned)                                            # no type, no scope: nothing assumed
        _sanitize_create_params(p, "script.update")
        self.assertEqual(p, returned)

    def test_usergroup_userdirectoryid_zero_and_authentication_blanks(self):
        p = {"usrgrpid": "7", "userdirectoryid": "0", "gui_access": "1"}
        _sanitize_create_params(p, "usergroup.update")
        self.assertNotIn("userdirectoryid", p)
        p = {"usrgrpid": "7", "userdirectoryid": "3", "gui_access": "2"}
        _sanitize_create_params(p, "usergroup.update")
        self.assertEqual(p["userdirectoryid"], "3")
        p = {"ldap_configured": "0", "ldap_host": "", "ldap_port": "389", "saml_auth_enabled": "0",
             "saml_idp_entityid": "", "saml_sso_url": "", "authentication_type": "0"}
        _sanitize_create_params(p, "authentication.update")
        self.assertEqual(p, {"ldap_configured": "0", "ldap_port": "389", "saml_auth_enabled": "0", "authentication_type": "0"})
        p = {"ldap_configured": "1", "ldap_host": "", "saml_auth_enabled": "1", "saml_sso_url": ""}
        _sanitize_create_params(p, "authentication.update")
        self.assertEqual(p, {"ldap_configured": "1", "ldap_host": "", "saml_auth_enabled": "1", "saml_sso_url": ""})

    def test_empty_interface_details_and_host_echoes(self):
        p = {"hostid": "1", "interfaces": [{"type": "1", "details": []}, {"type": "2", "details": {"version": "2"}}],
             "items": [{"itemid": "9"}], "parentTemplates": [{"templateid": "1"}], "groups": [{"groupid": "2"}],
             "macros": [], "inventory": {"os": "x"}}
        _sanitize_create_params(p, "host.update")
        self.assertEqual(p, {"hostid": "1", "interfaces": [{"type": "1"}, {"type": "2", "details": {"version": "2"}}],
                             "groups": [{"groupid": "2"}], "macros": [], "inventory": {"os": "x"}})
        p = {"interfaceid": "5", "details": []}
        _sanitize_create_params(p, "hostinterface.update")
        self.assertEqual(p, {"interfaceid": "5"})
        p = {"hostid": "7", "custom_interfaces": "0", "interfaces": []}
        _sanitize_create_params(p, "hostprototype.update")
        self.assertEqual(p, {"hostid": "7", "custom_interfaces": "0"})
        p = {"hostid": "7", "custom_interfaces": "1", "interfaces": []}       # an explicit "no interfaces" stays
        _sanitize_create_params(p, "hostprototype.update")
        self.assertEqual(p["interfaces"], [])

    def test_discard_steps_keep_the_default_error_handler(self):
        sent = self._call("item_update", {"params": {"itemid": "1", "preprocessing": [
            {"type": 20, "params": "1d", "error_handler": "0", "error_handler_params": ""},
            {"type": "19", "params": ""}, {"type": 12, "params": "$.a"}]}})
        steps = sent[0][1]["preprocessing"]
        self.assertEqual(steps[0], {"type": 20, "params": "1d", "error_handler": 0, "error_handler_params": ""})
        self.assertEqual(steps[1], {"type": "19", "params": "", "error_handler": 0, "error_handler_params": ""})
        self.assertEqual(steps[2], {"type": 12, "params": "$.a", "error_handler": 0, "error_handler_params": ""})

    def test_report_dates_stay_strings(self):
        sent = self._call("report_update", {"params": {"reportid": "8", "active_since": "2020-01-01",
                                                       "active_till": "2020-12-31"}}, result={"reportids": ["8"]})
        self.assertEqual(sent[0][1]["active_since"], "2020-01-01")
        self.assertEqual(sent[0][1]["active_till"], "2020-12-31")
        sent = self._call("maintenance_update", {"params": {"maintenanceid": "9", "active_since": "2020-01-01T00:00:00Z"}},
                          result={"maintenanceids": ["9"]})
        self.assertEqual(sent[0][1]["active_since"], 1577836800)       # maintenance keeps the epoch conversion


class TestConfig(unittest.TestCase):
    def test_switch_parsed_and_validated(self):
        from zabbix_mcp.config import ConfigError, load_config
        import tempfile
        base = ('[server]\ntransport = "stdio"\n{line}\n[zabbix.t]\nurl = "http://localhost"\napi_token = "x"\n')
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text(base.format(line="strip_write_params = false"), encoding="utf-8")
            self.assertFalse(load_config(str(p)).server.strip_write_params)
            p.write_text(base.format(line=""), encoding="utf-8")
            self.assertTrue(load_config(str(p)).server.strip_write_params)
            p.write_text(base.format(line='strip_write_params = "no"'), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(str(p))


if __name__ == "__main__":
    unittest.main()
