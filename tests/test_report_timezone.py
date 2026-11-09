#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""PDF reports render in the zone resolved per call, not always in UTC.

Since v1.38 every tool resolves a zone (``resolve_zone``); the report
tool kept stamping its period boundaries, the backup day grid and the
"generated at" footer in UTC. The fetchers now take ``tz`` and the engine
reads it from the context; without one both fall back to UTC.

weasyprint is optional in the test venv, so these tests check the
context and the rendered HTML, never the PDF bytes.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from zabbix_mcp.reporting import data_fetcher, engine
from zabbix_mcp.reporting.data_fetcher import (
    _ts_to_str,
    fetch_availability_data,
    fetch_backup_data,
    fetch_capacity_host_data,
)

PRAGUE = ZoneInfo("Europe/Prague")
# 2026-10-01 00:00 Europe/Prague (CEST, UTC+2) == 2026-09-30 22:00 UTC
PRAGUE_MIDNIGHT = int(datetime(2026, 10, 1, 0, 0, tzinfo=PRAGUE).timestamp())


class _FakeZabbix:
    """Minimal client_manager double: canned answers per API method."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def call(self, server, method, params):
        self.calls.append((method, params))
        if method in self.answers:
            value = self.answers[method]
            return value(params) if callable(value) else value
        return []


class TestTsToStr(unittest.TestCase):

    def test_default_is_utc_as_before(self):
        self.assertEqual(_ts_to_str(PRAGUE_MIDNIGHT), "2026-09-30 22:00 UTC")

    def test_zone_shifts_the_clock_and_names_the_zone(self):
        self.assertEqual(_ts_to_str(PRAGUE_MIDNIGHT, PRAGUE), "2026-10-01 00:00 Europe/Prague")

    def test_explicit_utc_zone_prints_utc(self):
        self.assertEqual(_ts_to_str(PRAGUE_MIDNIGHT, timezone.utc), "2026-09-30 22:00 UTC")


class TestFetchersCarryTheZone(unittest.TestCase):

    def test_availability_period_labels_in_zone(self):
        zbx = _FakeZabbix({"host.get": [{"hostid": "10", "host": "web01", "name": "web01"}]})
        ctx = fetch_availability_data(
            zbx, "prod",
            {"hostids": ["10"], "period_from": PRAGUE_MIDNIGHT, "period_to": PRAGUE_MIDNIGHT + 86400},
            tz=PRAGUE,
        )
        self.assertEqual(ctx["period_from"], "2026-10-01 00:00 Europe/Prague")
        self.assertEqual(ctx["period_to"], "2026-10-02 00:00 Europe/Prague")
        self.assertEqual(ctx["timezone"], "Europe/Prague")

    def test_availability_without_zone_stays_utc(self):
        zbx = _FakeZabbix({"host.get": [{"hostid": "10", "host": "web01", "name": "web01"}]})
        ctx = fetch_availability_data(
            zbx, "prod",
            {"hostids": ["10"], "period_from": PRAGUE_MIDNIGHT, "period_to": PRAGUE_MIDNIGHT + 86400},
        )
        self.assertEqual(ctx["period_from"], "2026-09-30 22:00 UTC")
        self.assertEqual(ctx["timezone"], "UTC")

    def test_capacity_host_period_labels_in_zone(self):
        zbx = _FakeZabbix({"host.get": [{"hostid": "10", "host": "web01", "name": "web01"}]})
        ctx = fetch_capacity_host_data(
            zbx, "prod",
            {"hostids": ["10"], "period_from": PRAGUE_MIDNIGHT, "period_to": PRAGUE_MIDNIGHT + 3600},
            tz=PRAGUE,
        )
        self.assertTrue(ctx["period_from"].endswith(" Europe/Prague"), ctx["period_from"])
        self.assertEqual(ctx["timezone"], "Europe/Prague")

    def _backup_fixture(self):
        # One backup run at 23:30 Prague on Oct 1 == 21:30 UTC on Oct 1,
        # one at 00:30 Prague on Oct 3 == 22:30 UTC on Oct 2.
        run_a = PRAGUE_MIDNIGHT + 23 * 3600 + 30 * 60
        run_b = PRAGUE_MIDNIGHT + 2 * 86400 + 30 * 60
        return _FakeZabbix({
            "host.get": [{"hostid": "10", "host": "nas01", "name": "nas01"}],
            "item.get": [{"itemid": "500", "key_": "backup.status", "name": "Backup", "value_type": "3"}],
            "history.get": [
                {"clock": str(run_a), "value": "1"},
                {"clock": str(run_b), "value": "0"},
            ],
        })

    def test_backup_groups_days_in_zone(self):
        ctx = fetch_backup_data(
            self._backup_fixture(), "prod",
            {"hostids": ["10"], "period_from": PRAGUE_MIDNIGHT,
             "period_to": PRAGUE_MIDNIGHT + 3 * 86400, "period_label": "October"},
            tz=PRAGUE,
        )
        self.assertEqual(ctx["days"], [1, 2, 3])
        (row,) = ctx["backup_matrix"]
        self.assertEqual(row["statuses"], {1: True, 3: False})
        self.assertEqual(ctx["timezone"], "Europe/Prague")
        self.assertEqual(ctx["period_from"], "2026-10-01 00:00 Europe/Prague")

    def test_backup_groups_days_in_utc_without_zone(self):
        # The same runs seen from UTC: both land on different days
        # (Sep 30 - Oct 3 window, runs on Oct 1 and Oct 2).
        ctx = fetch_backup_data(
            self._backup_fixture(), "prod",
            {"hostids": ["10"], "period_from": PRAGUE_MIDNIGHT,
             "period_to": PRAGUE_MIDNIGHT + 3 * 86400, "period_label": "October"},
        )
        self.assertEqual(ctx["days"], [1, 2, 3, 30])
        (row,) = ctx["backup_matrix"]
        self.assertEqual(row["statuses"], {1: True, 2: False})
        self.assertEqual(ctx["timezone"], "UTC")


class TestEngineGeneratedAt(unittest.TestCase):

    def test_generated_at_defaults_to_utc(self):
        stamp = engine._generated_at()
        self.assertTrue(stamp.endswith(" UTC"), stamp)
        self.assertNotIn("Europe", stamp)

    def test_generated_at_names_the_zone(self):
        stamp = engine._generated_at(PRAGUE)
        self.assertTrue(stamp.endswith(" Europe/Prague"), stamp)
        now_local = datetime.now(PRAGUE).strftime("%Y-%m-%d %H:")
        self.assertTrue(stamp.startswith(now_local), stamp)

    def _engine(self):
        # jinja2 is a hard dependency of the package; weasyprint is not
        # installed in every venv, so only the HTML side is exercised.
        with mock.patch.object(engine, "REPORTING_AVAILABLE", True):
            return engine.ReportEngine(company_name="initMAX")

    def test_context_carries_zone_name_and_drops_tzinfo(self):
        eng = self._engine()
        common = eng.build_context({"period_from": "x", "tz": PRAGUE})
        self.assertNotIn("tz", common)
        self.assertEqual(common["timezone"], "Europe/Prague")
        self.assertTrue(common["generated_at"].endswith(" Europe/Prague"))

    def test_context_without_zone_says_utc(self):
        eng = self._engine()
        common = eng.build_context({"period_from": "x"})
        self.assertEqual(common["timezone"], "UTC")
        self.assertTrue(common["generated_at"].endswith(" UTC"))

    def test_rendered_html_header_names_the_zone(self):
        eng = self._engine()
        zbx = _FakeZabbix({"host.get": [{"hostid": "10", "host": "web01", "name": "web01"}]})
        data = fetch_availability_data(
            zbx, "prod",
            {"hostids": ["10"], "period_from": PRAGUE_MIDNIGHT, "period_to": PRAGUE_MIDNIGHT + 86400},
            tz=PRAGUE,
        )
        data["gauge_arc_path"] = engine._compute_gauge_arc_path(data["availability_pct"])
        html = eng.render_html("availability.html", dict(data, tz=PRAGUE))
        self.assertIn("2026-10-01 00:00 Europe/Prague", html)
        self.assertNotIn(" UTC", html)

    def test_generate_report_passes_tz_option_through(self):
        # generate_report must hand the tz option down to render_pdf
        # unchanged - that is the hook the server uses.
        eng = self._engine()
        seen = {}

        def fake_render_pdf(template_name, context):
            seen.update(context)
            return b"%PDF"

        with mock.patch.object(eng, "render_pdf", side_effect=fake_render_pdf):
            eng.generate_report("capacity_host", {"metrics": []}, tz=PRAGUE)
        self.assertIs(seen["tz"], PRAGUE)


if __name__ == "__main__":
    unittest.main()
