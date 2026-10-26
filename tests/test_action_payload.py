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

"""An action as action.get returns it goes back through action.update (#99).

The fixtures are the two stock actions of a Zabbix 7.4.14 as the API returns
them with every select; the expected payloads are what that server, 7.0.30 and
6.0.48 accepted after stripping one "unexpected parameter" at a time."""

import asyncio
import copy
import json
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, "src")

from mcp.server.mcpserver import MCPServer  # noqa: E402

from zabbix_mcp.api.action_payload import (  # noqa: E402
    FILTER_CONTRACT, strip_correlation_for_write, strip_filter, strip_for_write,
)
from zabbix_mcp.client import ClientManager  # noqa: E402
from zabbix_mcp.config import AppConfig, ServerConfig, ZabbixServerConfig  # noqa: E402
from zabbix_mcp.server import _register_tools  # noqa: E402

DISCOVERY_ACTION = json.loads('''{"actionid": "2", "name": "Auto discovery. Linux servers.", "eventsource": "1",
 "status": "1", "esc_period": "0", "pause_suppressed": "1", "notify_if_canceled": "1", "pause_symptoms": "1",
 "filter": {"evaltype": "0", "formula": "", "eval_formula": "A and B and C", "conditions": [
   {"conditiontype": "10", "operator": "0", "value": "0", "value2": "", "formulaid": "A"},
   {"conditiontype": "12", "operator": "2", "value": "Linux", "value2": "", "formulaid": "B"},
   {"conditiontype": "8", "operator": "0", "value": "9", "value2": "", "formulaid": "C"}]},
 "operations": [
   {"operationid": "1", "actionid": "2", "operationtype": "6", "esc_period": "0", "esc_step_from": "1",
    "esc_step_to": "1", "evaltype": "0", "opconditions": [], "optemplate": [{"templateid": "10001"}]},
   {"operationid": "2", "actionid": "2", "operationtype": "4", "esc_period": "0", "esc_step_from": "1",
    "esc_step_to": "1", "evaltype": "0", "opconditions": [], "opgroup": [{"groupid": "2"}]}],
 "recovery_operations": [], "update_operations": []}''')

TRIGGER_ACTION = json.loads('''{"actionid": "3", "name": "Report problems to Zabbix administrators",
 "eventsource": "0", "status": "1", "esc_period": "1h", "pause_suppressed": "1", "notify_if_canceled": "1",
 "pause_symptoms": "1", "filter": {"evaltype": "0", "formula": "", "eval_formula": "", "conditions": []},
 "operations": [{"operationid": "3", "actionid": "3", "operationtype": "0", "esc_period": "0",
   "esc_step_from": "1", "esc_step_to": "1", "evaltype": "0", "opconditions": [],
   "opmessage": {"default_msg": "1", "subject": "", "message": "", "mediatypeid": "0"},
   "opmessage_grp": [{"usrgrpid": "7"}], "opmessage_usr": []}],
 "recovery_operations": [{"operationid": "7", "actionid": "3", "operationtype": "11", "evaltype": "0",
   "opconditions": [], "opmessage": {"default_msg": "1", "subject": "", "message": "", "mediatypeid": "0"}}],
 "update_operations": []}''')


class TestStripForWrite(unittest.TestCase):
    def test_discovery_action_round_trip(self):
        out = strip_for_write(copy.deepcopy(DISCOVERY_ACTION))
        self.assertEqual(out, {
            "actionid": "2", "name": "Auto discovery. Linux servers.", "eventsource": "1", "status": "1",
            "filter": {"evaltype": "0", "conditions": [
                {"conditiontype": "10", "operator": "0", "value": "0"},
                {"conditiontype": "12", "operator": "2", "value": "Linux"},
                {"conditiontype": "8", "operator": "0", "value": "9"}]},
            "operations": [
                {"operationtype": "6", "optemplate": [{"templateid": "10001"}]},
                {"operationtype": "4", "opgroup": [{"groupid": "2"}]}],
        })

    def test_trigger_action_round_trip(self):
        out = strip_for_write(copy.deepcopy(TRIGGER_ACTION))
        self.assertEqual(out, {
            "actionid": "3", "name": "Report problems to Zabbix administrators", "eventsource": "0",
            "status": "1", "esc_period": "1h", "pause_suppressed": "1", "notify_if_canceled": "1",
            "pause_symptoms": "1",
            "filter": {"evaltype": "0", "conditions": []},
            "operations": [{"operationtype": "0", "esc_period": "0", "esc_step_from": "1", "esc_step_to": "1",
                            "evaltype": "0", "opconditions": [],
                            "opmessage": {"default_msg": "1", "mediatypeid": "0"},
                            "opmessage_grp": [{"usrgrpid": "7"}], "opmessage_usr": []}],
            "recovery_operations": [{"operationtype": "11", "opmessage": {"default_msg": "1"}}],
            "update_operations": [],
        })

    def test_eventsource_from_the_caller_when_payload_lacks_it(self):
        payload = copy.deepcopy(DISCOVERY_ACTION)
        del payload["eventsource"]
        out = strip_for_write(payload, eventsource="1")
        self.assertNotIn("esc_period", out)
        self.assertNotIn("recovery_operations", out)
        payload = copy.deepcopy(DISCOVERY_ACTION)
        del payload["eventsource"]
        out = strip_for_write(payload, eventsource=None)   # unknown source: generic rules only
        self.assertIn("esc_period", out)
        self.assertIn("recovery_operations", out)
        self.assertNotIn("eval_formula", out["filter"])
        self.assertNotIn("operationid", out["operations"][0])

    def test_custom_expression_keeps_formula_and_formulaid(self):
        filt = {"evaltype": 3, "formula": "A and (B or C)", "eval_formula": "A and (B or C)",
                "conditions": [{"conditiontype": 0, "value": "4", "formulaid": "A", "value2": ""},
                               {"conditiontype": 26, "value": "env", "value2": "prod", "formulaid": "B"}]}
        out = strip_filter(filt)
        self.assertEqual(out["formula"], "A and (B or C)")
        self.assertNotIn("eval_formula", out)
        self.assertEqual([c["formulaid"] for c in out["conditions"]], ["A", "B"])
        self.assertNotIn("value2", out["conditions"][0])
        self.assertEqual(out["conditions"][1]["value2"], "prod")

    def test_what_a_caller_meant_stays(self):
        out = strip_for_write({"actionid": "3", "eventsource": 0, "recovery_operations": [],
                               "pause_suppressed": 0, "esc_period": "30m",
                               "operations": [{"operationtype": 0, "esc_period": "10m", "evaltype": 1,
                                               "opconditions": [{"conditiontype": 14, "value": "0"}],
                                               "opmessage": {"default_msg": 0, "subject": "s", "message": "m",
                                                             "mediatypeid": "1"}}]})
        self.assertEqual(out["recovery_operations"], [])
        self.assertEqual(out["pause_suppressed"], 0)
        self.assertEqual(out["operations"][0]["opmessage"], {"default_msg": 0, "subject": "s", "message": "m", "mediatypeid": "1"})
        self.assertEqual(out["operations"][0]["opconditions"], [{"conditiontype": 14, "value": "0"}])

    def test_internal_and_service_sources(self):
        internal = strip_for_write({"eventsource": 3, "esc_period": "1h", "pause_symptoms": "1",
                                    "recovery_operations": [], "update_operations": []})
        self.assertEqual(internal, {"eventsource": 3, "esc_period": "1h", "recovery_operations": []})
        service = strip_for_write({"eventsource": "4", "esc_period": "1h", "notify_if_canceled": "1",
                                   "recovery_operations": [], "update_operations": []})
        self.assertEqual(service, {"eventsource": "4", "esc_period": "1h", "recovery_operations": [], "update_operations": []})

    def test_status_only_update_untouched(self):
        self.assertEqual(strip_for_write({"actionid": "3", "status": 0}, "0"), {"actionid": "3", "status": 0})
        self.assertEqual(strip_for_write("not a dict"), "not a dict")

    def test_correlation(self):
        out = strip_correlation_for_write({"correlationid": "2", "filter": {
            "evaltype": "0", "formula": "", "eval_formula": "A and B",
            "conditions": [{"type": "0", "tag": "x", "formulaid": "A"}, {"type": "1", "tag": "y", "formulaid": "B"}]},
            "operations": [{"type": "0", "corr_operationid": "5"}]})
        self.assertEqual(out["filter"], {"evaltype": "0", "conditions": [{"type": "0", "tag": "x"}, {"type": "1", "tag": "y"}]})
        self.assertEqual(out["operations"], [{"type": "0"}])


NESTED_OPERATION = json.loads('''{"operationid": "42", "actionid": "3", "operationtype": "1", "esc_period": "0",
 "esc_step_from": "1", "esc_step_to": "1", "evaltype": "0",
 "opconditions": [{"opconditionid": "71", "operationid": "42", "conditiontype": "14", "operator": "0", "value": "0"}],
 "opcommand": {"scriptid": "7"},
 "opcommand_hst": [{"opcommand_hstid": "81", "hostid": "0"}, {"opcommand_hstid": "82", "hostid": "10101"}],
 "opcommand_grp": [{"opcommand_grpid": "91", "groupid": "20"}]}''')


class TestNestedOperationIds(unittest.TestCase):
    """#101 / PR #102 (@alexpostnik): the row identifiers inside operation
    conditions and command targets, measured refused on 6.0.48, 7.0.30 and
    7.4.14 (``unexpected parameter "opconditionid"``)."""

    def test_nested_ids_go_values_stay(self):
        out = strip_for_write({"actionid": "3", "eventsource": "0", "operations": [copy.deepcopy(NESTED_OPERATION)]})
        self.assertEqual(out["operations"][0], {
            "operationtype": "1", "esc_period": "0", "esc_step_from": "1", "esc_step_to": "1", "evaltype": "0",
            "opconditions": [{"conditiontype": "14", "operator": "0", "value": "0"}],
            "opcommand": {"scriptid": "7"},
            "opcommand_hst": [{"hostid": "0"}, {"hostid": "10101"}],
            "opcommand_grp": [{"groupid": "20"}]})

    def test_only_those_keys_and_tolerant_of_odd_shapes(self):
        out = strip_for_write({"eventsource": "0", "operations": [
            {"opconditions": [None, 0, {"opconditionid": "1", "custom": "keep"}],
             "opcommand_hst": "not a list", "opcommand_grp": [{"opcommand_grpid": "9", "groupid": "4", "x": 1}]},
            None, "text"]})
        self.assertEqual(out["operations"][0], {"opconditions": [None, 0, {"custom": "keep"}],
                                                "opcommand_hst": "not a list",
                                                "opcommand_grp": [{"groupid": "4", "x": 1}]})
        self.assertEqual(out["operations"][1:], [None, "text"])

    def test_explicit_empty_arrays_stay(self):
        out = strip_for_write({"eventsource": "0", "operations": [
            {"operationtype": "1", "opconditions": [], "opcommand_hst": [], "opcommand_grp": []}]})
        self.assertEqual(out["operations"][0], {"operationtype": "1", "opconditions": [], "opcommand_hst": [],
                                                "opcommand_grp": []})


class TestThroughRegisteredTools(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = AppConfig(server=ServerConfig(timezone="UTC"), zabbix_servers={"t": ZabbixServerConfig(
            name="t", url="http://localhost", api_token="x", read_only=False)})
        cls.mgr = ClientManager(cfg)
        cls.mcp = MCPServer(name="test")
        _register_tools(cls.mcp, cls.mgr)

    def _call(self, tool, args, lookup=None):
        sent = []

        def fake_call(server, method, params, *a, **k):
            sent.append((method, params))
            if method == "action.get":
                return lookup if lookup is not None else []
            return {"actionids": ["3"]}

        with patch.object(self.mgr, "call", side_effect=fake_call), \
                patch.object(self.mgr, "get_version", return_value="7.4.14"):
            asyncio.run(self.mcp.call_tool(tool, args))
        return sent

    def test_update_without_eventsource_looks_it_up_and_strips(self):
        payload = copy.deepcopy(DISCOVERY_ACTION)
        del payload["eventsource"]
        sent = self._call("action_update", {"params": payload}, lookup=[{"eventsource": "1"}])
        self.assertEqual(sent[0], ("action.get", {"output": ["eventsource"], "actionids": ["2"]}))
        method, params = sent[1]
        self.assertEqual(method, "action.update")
        self.assertNotIn("esc_period", params)
        self.assertNotIn("recovery_operations", params)
        self.assertNotIn("eval_formula", params["filter"])
        self.assertNotIn("formulaid", params["filter"]["conditions"][0])
        self.assertNotIn("operationid", params["operations"][0])

    def test_update_with_eventsource_needs_no_lookup(self):
        sent = self._call("action_update", {"params": copy.deepcopy(TRIGGER_ACTION)})
        self.assertEqual([m for m, _ in sent], ["action.update"])
        self.assertEqual(sent[0][1]["recovery_operations"], [{"operationtype": "11", "opmessage": {"default_msg": "1"}}])

    def test_create_with_symbolic_source(self):
        sent = self._call("action_create", {"params": {
            "name": "x", "eventsource": "DISCOVERY", "esc_period": "0", "pause_symptoms": "1",
            "filter": {"evaltype": 0, "conditions": [{"conditiontype": 10, "value": "0", "formulaid": "A", "value2": ""}]},
            "operations": [{"operationtype": 6, "optemplate": [{"templateid": "1"}]}]}})
        params = sent[0][1]
        self.assertEqual(params["eventsource"], 1)
        self.assertNotIn("esc_period", params)
        self.assertNotIn("pause_symptoms", params)
        self.assertEqual(params["filter"]["conditions"], [{"conditiontype": 10, "value": "0"}])

    def test_lookup_failure_keeps_generic_rules(self):
        payload = copy.deepcopy(DISCOVERY_ACTION)
        del payload["eventsource"]
        sent = []

        def fake_call(server, method, params, *a, **k):
            sent.append((method, params))
            if method == "action.get":
                raise RuntimeError("boom")
            return {"actionids": ["2"]}

        with patch.object(self.mgr, "call", side_effect=fake_call), \
                patch.object(self.mgr, "get_version", return_value="7.4.14"):
            asyncio.run(self.mcp.call_tool("action_update", {"params": payload}))
        params = sent[-1][1]
        self.assertIn("esc_period", params)                 # source unknown: not touched
        self.assertNotIn("eval_formula", params["filter"])  # generic rules still applied

    def test_nested_ids_through_the_tools(self):
        for tool, source in (("action_update", "0"), ("action_update", None), ("action_create", "TRIGGER")):
            payload = {"name": "n", "operations": [copy.deepcopy(NESTED_OPERATION)],
                       "recovery_operations": [copy.deepcopy(NESTED_OPERATION)]}
            if tool == "action_update":
                payload["actionid"] = "3"
            if source is not None:
                payload["eventsource"] = source
            sent = self._call(tool, {"params": payload}, lookup=[{"eventsource": "0"}])
            method, params = sent[-1]
            self.assertEqual(method, tool.replace("_", "."))
            for op in params["operations"] + params["recovery_operations"]:
                self.assertEqual(op["opconditions"] if "opconditions" in op else [], [] if "opconditions" not in op
                                 else [{"conditiontype": "14", "operator": "0", "value": "0"}])
                self.assertEqual(op["opcommand_hst"], [{"hostid": "0"}, {"hostid": "10101"}])
                self.assertEqual(op["opcommand_grp"], [{"groupid": "20"}])
                self.assertNotIn("operationid", op)
            self.assertNotIn("opconditions", params["recovery_operations"][0])   # recovery: no conditions

    def test_descriptions_carry_the_filter_contract(self):
        tools = {t.name: t for t in self.mcp._tool_manager.list_tools()}
        for name in ("action_create", "action_update"):
            self.assertIn(FILTER_CONTRACT, tools[name].description)
        self.assertIn("formulaid", tools["correlation_update"].description)


if __name__ == "__main__":
    unittest.main()
