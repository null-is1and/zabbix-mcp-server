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

"""Time zone handling for the timestamps the server reads and renders.

Zabbix stores and returns every timestamp as a Unix epoch; the frontend
renders it in the zone of the user (or the frontend default). An LLM
client sees only the epoch and, without a zone to anchor on, answers
"last hour" questions an hour off (issue #75). This module resolves
*one* zone per call and the server uses it in both directions:

- every epoch field in a result gets a ``<field>_local`` sibling
  rendered in that zone;
- a date-time the caller sends *without* a zone is read in that zone.

Resolution order, first hit wins:

1. ``timezone`` on the calling MCP token (``[tokens.X]``, or the portal
   user's own zone on an OAuth token)
2. ``timezone`` on the Zabbix entry (``[zabbix.X]``)
3. ``timezone`` in ``[server]``
3b. the zone a browser reported at OAuth consent (implicit: below the
   operator's pins, above what Zabbix says)
4. the zone of the Zabbix user the API token belongs to
   (``user.checkAuthentication`` -> ``timezone``, unless ``default``)
5. the Zabbix frontend default (``settings.get`` -> ``default_timezone``,
   unless ``system``)
6. ``system``: the zone of the host this MCP server runs on - the best
   available stand-in for the Zabbix web server's own zone, which a
   remote caller cannot read
7. UTC

Zone names are IANA keys (``Europe/London``); ``UTC`` and ``system``
are accepted as spelled.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("zabbix_mcp.timezones")

SYSTEM = "system"
UTC = "UTC"

# Zabbix spellings for "no explicit zone, inherit the next level".
ZABBIX_INHERIT: frozenset[str] = frozenset({"", "default", "system"})

# Result fields that hold a Unix epoch. Checked against the Zabbix API
# reference per object; fields that look like time but are not epochs
# (maintenance ``start_time`` = seconds from midnight, ``period`` =
# duration, ``ns`` = nanoseconds, ``delay`` = interval) are not here.
EPOCH_FIELDS: frozenset[str] = frozenset({
    "clock", "r_clock",                      # event, problem, history, trend, alert, auditlog
    "lastchange",                            # trigger
    "lastclock",                             # item
    "lastaccess",                            # proxy, hanode, token
    "lastup", "lastdown",                    # dhost, dservice
    "active_since", "active_till",           # maintenance, report
    "start_date",                            # maintenance one-time period
    "effective_date",                        # sla
    "period_from", "period_to",              # sla excluded downtime
    "created_at", "expires_at",              # token, service
    "lastsent",                              # report
    "disable_until", "errors_from",          # interface, host
    "maintenance_from",                      # host
    "attempt_clock", "ts_provisioned",       # user
    "suppress_until",                        # event suppression
})


def validate_timezone(name: str) -> str:
    """Return the normalised zone name, or raise ``ValueError``.

    Accepts an IANA key, ``UTC`` or ``system`` (any case for the last two).
    """
    text = (name or "").strip()
    if not text:
        raise ValueError("time zone must not be empty")
    if text.lower() == UTC.lower():
        return UTC
    if text.lower() == SYSTEM:
        return SYSTEM
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(
            f"'{text}' is not a known time zone; use an IANA name such as "
            f"'Europe/London', or 'UTC', or 'system'"
        ) from e
    return text


def host_zone() -> tzinfo:
    """The zone of the host this process runs on, as a real ``ZoneInfo`` when
    ``TZ`` or ``/etc/localtime`` names one (so DST is right across the year),
    else the fixed offset in force right now."""
    candidates = [os.environ.get("TZ", "")]
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    except OSError:
        pass
    for key in candidates:
        key = (key or "").strip().lstrip(":")
        if not key:
            continue
        try:
            return ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return datetime.now().astimezone().tzinfo or timezone.utc


def load_zone(name: str | None) -> tzinfo:
    """``tzinfo`` for a validated zone name; ``None``/``UTC`` -> UTC, ``system`` -> :func:`host_zone`."""
    text = (name or "").strip()
    if not text or text.lower() == UTC.lower():
        return timezone.utc
    if text.lower() == SYSTEM:
        return host_zone()
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown time zone %r, using UTC", text)
        return timezone.utc


def zone_name(tz: tzinfo) -> str:
    """Display name of *tz*: the IANA key when it has one, else its abbreviation."""
    key = getattr(tz, "key", None)
    if key:
        return str(key)
    if tz is timezone.utc:
        return UTC
    return tz.tzname(datetime.now(tz)) or UTC


def format_local(ts: Any, tz: tzinfo) -> str | None:
    """Render epoch *ts* in *tz* as ``YYYY-MM-DDTHH:MM:SS+HH:MM``; ``None`` if it is not an epoch > 0."""
    if isinstance(ts, bool):
        return None
    try:
        value = int(float(ts))
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None  # Zabbix uses 0 for "never" / "not set"
    try:
        return datetime.fromtimestamp(value, tz).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def now_local(tz: tzinfo) -> str:
    return datetime.now(tz).isoformat(timespec="seconds")


def add_local_times(data: Any, tz: tzinfo, fields: frozenset[str] = EPOCH_FIELDS) -> Any:
    """Return *data* with a ``<field>_local`` sibling next to every epoch field.

    Walks dicts and lists at any depth; the input is not mutated, and
    a branch without epoch fields keeps its identity. A ``<field>_local``
    key already present (for example from a nested Zabbix object that
    happens to use the name) is left alone.
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        changed = False
        for key, value in data.items():
            new_value = add_local_times(value, tz, fields) if isinstance(value, (dict, list)) else value
            out[key] = new_value
            if new_value is not value:
                changed = True
            if key in fields and not isinstance(value, (dict, list)) and f"{key}_local" not in data:
                rendered = format_local(value, tz)
                if rendered is not None:
                    out[f"{key}_local"] = rendered
                    changed = True
        return out if changed else data
    if isinstance(data, list):
        out_list: list[Any] | None = None
        for index, value in enumerate(data):
            new_value = add_local_times(value, tz, fields)
            if new_value is not value:
                if out_list is None:
                    out_list = list(data)
                out_list[index] = new_value
        return data if out_list is None else out_list
    return data


def resolve_zone(client_manager: Any, server_name: str) -> tzinfo:
    """The zone for one call against *server_name* (see the module docstring).

    Does blocking I/O on the first call per server (Zabbix lookup, cached
    by the client manager), so call it from a worker thread.
    """
    from zabbix_mcp.token_store import current_token_info

    token = current_token_info.get()
    token_zone = (getattr(token, "timezone", None) or "").strip() if token else ""
    implicit = bool(getattr(token, "timezone_implicit", False)) if token else False
    # A zone the operator wrote on the token wins; one merely reported by
    # a browser at OAuth consent ranks below the operator's server pins.
    name = token_zone if not implicit else ""
    if not name:
        cfg = getattr(client_manager, "config", None)
        srv = cfg.zabbix_servers.get(server_name) if cfg else None
        name = (getattr(srv, "timezone", "") or "").strip()
        if not name and cfg is not None:
            name = (getattr(cfg.server, "timezone", "") or "").strip()
    if not name and implicit:
        name = token_zone
    if not name:
        getter = getattr(client_manager, "get_zabbix_timezone", None)
        if getter is not None:
            try:
                name = (getter(server_name) or "").strip()
            except Exception as e:  # noqa: BLE001 - never let a zone lookup fail a call
                logger.warning("Time zone lookup for '%s' failed: %s", server_name, e)
                name = ""
    if not name:
        name = SYSTEM
    return load_zone(name)
