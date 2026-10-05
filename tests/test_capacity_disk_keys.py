#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""The host capacity report must find disk usage on current templates (#84).

"Linux by Zabbix agent" and "Windows by Zabbix agent" (6.0+) collect
filesystem usage as dependent items keyed vfs.fs.dependent.size[...].
The report only searched for vfs.fs.size[...], and item.get "search" is
a substring match, so every host on a current template came back with
an empty Disk Usage table.
"""

from __future__ import annotations

import unittest

from zabbix_mcp.reporting.data_fetcher import fetch_capacity_host_data


class _FakeZabbix:
    """Enough of item.get / trend.get / host.get to drive the report.

    item.get's ``search`` is emulated the way Zabbix does it: a
    case-insensitive substring match on key_.
    """

    def __init__(self, item_keys):
        self.items = [
            {"itemid": str(100 + i), "key_": k, "name": k} for i, k in enumerate(item_keys)
        ]
        self.searched = []

    def call(self, server, method, params):
        if method == "host.get":
            return [{"hostid": "10", "host": "web01", "name": "web01"}]
        if method == "item.get":
            needle = params["search"]["key_"].lower()
            self.searched.append(params["search"]["key_"])
            hits = [i for i in self.items if needle in i["key_"].lower()]
            return hits[: params.get("limit", len(hits))]
        if method == "trend.get":
            return [{"value_avg": "40", "value_min": "35", "value_max": "55"}]
        raise AssertionError(f"unexpected API call {method}")


def _disk_rows(zbx):
    ctx = fetch_capacity_host_data(
        zbx, "prod", {"hostids": ["10"], "period_from": 0, "period_to": 3600}
    )
    (disk,) = [m for m in ctx["metrics"] if m["label"] == "Disk Usage"]
    return disk["rows"]


class TestCapacityDiskKeys(unittest.TestCase):

    def test_linux_dependent_item_is_found(self):
        zbx = _FakeZabbix(["system.cpu.util", "vm.memory.utilization",
                           "vfs.fs.dependent.size[/,pused]", "vfs.fs.dependent.size[/boot,pused]"])
        rows = _disk_rows(zbx)
        self.assertEqual([r["endpoint"] for r in rows], ["web01"])
        self.assertEqual(rows[0]["avg"], 40.0)

    def test_windows_dependent_item_is_found(self):
        zbx = _FakeZabbix(["vfs.fs.dependent.size[C:,pused]", "vfs.fs.dependent.size[D:,pused]"])
        self.assertEqual(len(_disk_rows(zbx)), 1)

    def test_root_is_picked_not_another_mount(self):
        # "[/,pused]" must not loosely match "/boot" or "/var".
        zbx = _FakeZabbix(["vfs.fs.dependent.size[/var,pused]", "vfs.fs.dependent.size[/,pused]"])
        _disk_rows(zbx)
        hit = [i for i in zbx.items if "[/,pused]" in i["key_"]][0]
        self.assertEqual(hit["key_"], "vfs.fs.dependent.size[/,pused]")

    def test_plain_key_on_an_old_template_still_works(self):
        zbx = _FakeZabbix(["vfs.fs.size[/,pused]"])
        self.assertEqual(len(_disk_rows(zbx)), 1)

    def test_host_without_any_disk_item_gives_no_row(self):
        zbx = _FakeZabbix(["system.cpu.util"])
        self.assertEqual(_disk_rows(zbx), [])
        # All four spellings were tried before giving up.
        self.assertEqual(
            [k for k in zbx.searched if k.startswith("vfs.fs")],
            ["vfs.fs.dependent.size[/,pused]", "vfs.fs.dependent.size[C:,pused]",
             "vfs.fs.size[/,pused]", "vfs.fs.size[C:,pused]"],
        )


if __name__ == "__main__":
    unittest.main()
