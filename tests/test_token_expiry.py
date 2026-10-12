#
# Zabbix MCP Server
# Copyright (C) 2026 initMAX s.r.o.
# Licensed under the GNU Affero General Public License v3.
# See LICENSE for details.
#

"""Token expiry must be read the same way on every supported Python, and
an expiry the server cannot read must refuse the token, not ignore it.

The admin portal accepts ``2026-12-31T23:59:59Z`` and stores it as typed.
``datetime.fromisoformat`` only accepts the ``Z`` suffix from Python 3.11;
on 3.10 it raised, the runtime check logged "invalid expires_at" and let
the token through - so an expired token kept working on the interpreter
Ubuntu 22.04 ships. Spotted via a downstream fork's CI notes.
"""

from __future__ import annotations

import hashlib
import secrets
import unittest
from datetime import datetime, timezone

from zabbix_mcp.token_store import TokenInfo, TokenStore, parse_expiry


def _raw_and_hash():
    raw = secrets.token_urlsafe(24)
    return raw, "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


class TestParseExpiry(unittest.TestCase):

    def test_z_suffix_is_utc(self):
        self.assertEqual(parse_expiry("2020-01-01T00:00:00Z"),
                         datetime(2020, 1, 1, tzinfo=timezone.utc))

    def test_naive_timestamp_is_read_as_utc(self):
        self.assertEqual(parse_expiry("2020-01-01T00:00:00"),
                         datetime(2020, 1, 1, tzinfo=timezone.utc))

    def test_date_only(self):
        self.assertEqual(parse_expiry("2020-01-01"), datetime(2020, 1, 1, tzinfo=timezone.utc))

    def test_explicit_offset_is_kept(self):
        self.assertEqual(parse_expiry("2020-01-01T02:00:00+02:00"),
                         datetime(2020, 1, 1, tzinfo=timezone.utc))

    def test_garbage_raises(self):
        for bad in ("next week", "", "2020-13-45", "1700000000"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_expiry(bad)


class TestStoreRefusesWhatItCannotRead(unittest.TestCase):

    def setUp(self):
        self.store = TokenStore()

    def _load(self, expires_at):
        raw, h = _raw_and_hash()
        self.store.load_from_config({"t": {"name": "T", "token_hash": h, "expires_at": expires_at}})
        return raw

    def test_expired_with_z_suffix_is_refused(self):
        raw = self._load("2020-01-01T00:00:00Z")
        self.assertIsNone(self.store.verify(raw))

    def test_future_with_z_suffix_is_accepted(self):
        raw = self._load("2099-12-31T23:59:59Z")
        self.assertIsNotNone(self.store.verify(raw))

    def test_expired_naive_is_refused(self):
        raw = self._load("2020-01-01 00:00:00")
        self.assertIsNone(self.store.verify(raw))

    def test_unreadable_expiry_fails_closed(self):
        raw = self._load("next week")
        with self.assertLogs("zabbix_mcp.token_store", level="WARNING") as logs:
            self.assertIsNone(self.store.verify(raw))
        self.assertTrue(any("unreadable expires_at" in line for line in logs.output))

    def test_no_expiry_is_still_fine(self):
        raw, h = _raw_and_hash()
        self.store.load_from_config({"t": {"name": "T", "token_hash": h}})
        self.assertIsNotNone(self.store.verify(raw))


class TestIsExpiredBadge(unittest.TestCase):

    def _info(self, expires_at):
        return TokenInfo(id="t", name="T", token_hash="sha256:x", expires_at=expires_at)

    def test_future_z_is_not_expired(self):
        self.assertFalse(self._info("2099-01-01T00:00:00Z").is_expired)

    def test_past_z_is_expired(self):
        self.assertTrue(self._info("2020-01-01T00:00:00Z").is_expired)

    def test_unreadable_shows_as_expired(self):
        # The runtime refuses it, so the badge must not say Active.
        self.assertTrue(self._info("next week").is_expired)

    def test_unset_is_not_expired(self):
        self.assertFalse(self._info(None).is_expired)


if __name__ == "__main__":
    unittest.main()
