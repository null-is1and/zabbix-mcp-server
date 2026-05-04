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

"""MCP extension tools providing server-side analytics beyond the Zabbix API.

These functions are standalone — each receives a ClientManager instance and
server name, performs its work via Zabbix API calls, and returns a JSON string.
They are registered as MCP tools directly via FastMCP decorators in server.py.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import re
import ssl
import time
import http.cookiejar
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from zabbix_mcp.client import ClientManager

logger = logging.getLogger("zabbix_mcp.extensions")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PERIOD_RE = re.compile(r"^(\d+)([hdmHDM])$")

_PERIOD_MULTIPLIERS = {
    "h": 3600,
    "d": 86400,
    "m": 2592000,  # 30 days
}


def _parse_period(period: str) -> int:
    """Convert a human period string like '7d' or '6h' to seconds.

    Supported suffixes: h (hours), d (days), m (months ≈ 30 days).

    Raises:
        ValueError: If the period string is not in a recognised format.
    """
    m = _PERIOD_RE.match(period.strip())
    if not m:
        raise ValueError(
            f"Invalid period format: '{period}'. "
            f"Use a number followed by h/d/m (e.g. '1h', '7d', '30d')."
        )
    value = int(m.group(1))
    unit = m.group(2).lower()
    if value <= 0:
        raise ValueError(f"Period value must be positive, got {value}.")
    return value * _PERIOD_MULTIPLIERS[unit]


def _error_json(error: str) -> str:
    """Return a standardised JSON error response."""
    return json.dumps({"error": error})


# ---------------------------------------------------------------------------
# 1. Graph Image Export
# ---------------------------------------------------------------------------

# Cache: server_name -> signed ``Cookie`` value for chart2.php.
# Populated by graph_render's user.login fallback when frontend
# credentials are present. Cookie validity is bounded by Zabbix's
# session ttl (default 14 days), so caching for the lifetime of the
# MCP server process is fine.
_GRAPH_SESSION_CACHE: dict[str, str] = {}


def graph_render(
    client_manager: ClientManager,
    server_name: str,
    **kwargs: Any,
) -> str:
    """Render a Zabbix graph as a base64-encoded PNG image.

    Connects to the Zabbix frontend to fetch a pre-rendered chart image using
    the same authentication credentials as the API connection.

    Args:
        client_manager: The ClientManager instance for Zabbix API access.
        server_name: Name of the target Zabbix server.
        **kwargs: Must include ``graphid`` (str).  Optional: ``period`` (str,
            default ``"1h"``), ``width`` (int, default 800), ``height`` (int,
            default 200).

    Returns:
        JSON string with ``image`` (data-URI), ``graphid``, and ``period``
        keys, or an ``error`` key on failure.
    """
    try:
        # --- Parameter extraction & validation ---
        graphid = str(kwargs.get("graphid", ""))
        if not graphid or not graphid.isdigit():
            return _error_json("graphid is required and must be numeric.")

        period = str(kwargs.get("period", "1h"))
        # Validate period format
        _parse_period(period)

        width = int(kwargs.get("width", 800))
        height = int(kwargs.get("height", 200))

        if not 100 <= width <= 4096:
            return _error_json(f"width must be between 100 and 4096, got {width}.")
        if not 50 <= height <= 2048:
            return _error_json(f"height must be between 50 and 2048, got {height}.")

        # --- Resolve Zabbix frontend URL ---
        srv_config = client_manager.get_server_config(server_name)
        base_url = srv_config.url.rstrip("/")
        # Strip /api_jsonrpc.php if present — we need the frontend root.
        base_url = re.sub(r"/api_jsonrpc\.php$", "", base_url)

        chart_url = (
            f"{base_url}/chart2.php"
            f"?graphid={graphid}"
            f"&from=now-{period}"
            f"&to=now"
            f"&width={width}"
            f"&height={height}"
            f"&profileIdx=web.graphs.filter"
        )

        # --- Build HTTP request with auth ---
        # Auth strategy:
        #   1. Bearer ``Authorization`` (Zabbix 5.4 - legacy frontend
        #      that still honoured tokens on chart2.php).
        #   2. ``zbx_sessionid=<api_token>`` cookie (older path -
        #      Zabbix 5.x stored the API token straight as the session
        #      id).
        #   3. NEW (Zabbix 6.0+): a signed session produced by
        #      ``user.login`` with a username + password set in
        #      ``[zabbix.<server>].frontend_username/_password``. The
        #      token-only paths above are quietly ignored by 6.0+, so
        #      this is the only path that actually returns a PNG on
        #      modern installs.
        req = urllib.request.Request(chart_url)

        api_token = srv_config.api_token
        if api_token:
            req.add_header("Authorization", f"Bearer {api_token}")

        client = client_manager._get_client(server_name)
        session_id = getattr(client, "_ZabbixAPI__session_id", None)
        if session_id:
            req.add_header("Cookie", f"zbx_sessionid={session_id}")

        # If the operator gave us frontend credentials, log in through
        # the BROWSER login form (``index.php?login=1``). This is the
        # only way to get the signed ``zbx_session`` cookie that Zabbix
        # 6.0+ requires on chart2.php - the ``user.login`` JSON-RPC
        # method only returns a bare session id string, while the
        # frontend session needs the HMAC-signed wrapped cookie that
        # the PHP login flow produces. Cache the cookie per server in
        # a module-level dict (the zabbix_utils client overrides
        # __setattr__ for attribute caching, so we cannot stash extra
        # state on it directly).
        signed_cookie = _GRAPH_SESSION_CACHE.get(server_name)
        fe_user = getattr(srv_config, "frontend_username", "") or ""
        fe_pass = getattr(srv_config, "frontend_password", "") or ""
        # Refuse to send the frontend password over plain HTTP. The
        # Zabbix login form posts ``name``/``password`` as form-data;
        # without TLS it is recoverable from any LAN sniffer.
        if fe_user and fe_pass and not base_url.lower().startswith("https://"):
            logger.warning(
                "graph_render: refusing to send frontend credentials "
                "over plain HTTP for server %r; configure https:// or "
                "leave frontend_username/_password empty.",
                server_name,
            )
            fe_user = fe_pass = ""
        if fe_user and fe_pass and not signed_cookie:
            try:
                cj = http.cookiejar.CookieJar()
                login_handlers: list = [urllib.request.HTTPCookieProcessor(cj)]
                if not srv_config.verify_ssl:
                    skip_ssl = ssl.create_default_context()
                    skip_ssl.check_hostname = False
                    skip_ssl.verify_mode = ssl.CERT_NONE
                    login_handlers.append(urllib.request.HTTPSHandler(context=skip_ssl))
                login_opener = urllib.request.build_opener(*login_handlers)
                login_form = urllib.parse.urlencode({
                    "name": fe_user,
                    "password": fe_pass,
                    "autologin": "1",
                    "enter": "Sign in",
                }).encode("utf-8")
                login_url = f"{base_url}/index.php?login=1"
                with login_opener.open(
                    urllib.request.Request(login_url, data=login_form),
                    timeout=10,
                ) as _r:
                    pass
                cookie_pairs = []
                for c in cj:
                    if c.name in ("zbx_session", "zbx_sessionid"):
                        cookie_pairs.append(f"{c.name}={c.value}")
                if cookie_pairs:
                    signed_cookie = "; ".join(cookie_pairs)
                    _GRAPH_SESSION_CACHE[server_name] = signed_cookie
            except Exception as _login_err:
                logger.debug("frontend login fallback failed: %s", _login_err)
        if signed_cookie:
            req.add_header("Cookie", signed_cookie)

        # --- SSL context ---
        ssl_ctx: ssl.SSLContext | None = None
        if not srv_config.verify_ssl:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        # --- Fetch the image ---
        logger.debug("Fetching graph image: %s", chart_url)
        response = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
        data = response.read()

        content_type = response.headers.get("Content-Type", "")
        if "image" not in content_type:
            # Probably an HTML error / login page. Drop the cached
            # frontend cookie so the next call re-logs-in instead of
            # reusing a session that the Zabbix frontend has already
            # invalidated (rotated cookie, idle timeout, password
            # change, ...).
            _GRAPH_SESSION_CACHE.pop(server_name, None)
            snippet = data[:500].decode("utf-8", errors="replace")
            logger.warning(
                "Graph fetch returned non-image Content-Type '%s': %s",
                content_type,
                snippet,
            )
            return _error_json(
                f"Zabbix frontend did not return an image (Content-Type: {content_type}). "
                f"Check that the graphid exists and frontend auth is valid."
            )

        encoded = base64.b64encode(data).decode("ascii")
        return json.dumps({
            "image": f"data:image/png;base64,{encoded}",
            "graphid": graphid,
            "period": period,
        })

    except urllib.error.HTTPError as exc:
        # 401 / 403 typically means the cached frontend cookie is no
        # longer valid; drop it so the next call re-logs-in instead
        # of looping on the same dead cookie.
        if exc.code in (401, 403):
            _GRAPH_SESSION_CACHE.pop(server_name, None)
        logger.error("HTTP error fetching graph: %s %s", exc.code, exc.reason)
        return _error_json(
            f"HTTP {exc.code} from Zabbix frontend: {exc.reason}. "
            f"Check graphid and authentication."
        )
    except urllib.error.URLError as exc:
        logger.error("URL error fetching graph: %s", exc.reason)
        return _error_json(f"Cannot connect to Zabbix frontend: {exc.reason}")
    except Exception as exc:
        logger.exception("Unexpected error in graph_render")
        return _error_json(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# 2. Anomaly Detection
# ---------------------------------------------------------------------------


def anomaly_detect(
    client_manager: ClientManager,
    server_name: str,
    **kwargs: Any,
) -> str:
    """Detect anomalous hosts by comparing item trend data across a group.

    For each host matching the criteria, fetches trend data for the specified
    item key and computes the z-score relative to the group mean.  Hosts whose
    average value deviates by more than *threshold* standard deviations are
    reported as anomalies.

    Args:
        client_manager: The ClientManager instance for Zabbix API access.
        server_name: Name of the target Zabbix server.
        **kwargs: ``item_key`` (str, required), plus either ``hostgroupid``
            (str) or ``hostid`` (str).  Optional: ``period`` (str, default
            ``"7d"``), ``threshold`` (float, default 2.0).

    Returns:
        JSON string with ``anomalies`` list, ``hosts_analyzed`` count, and
        ``period``, or an ``error`` key on failure.
    """
    try:
        # --- Parameter extraction & validation ---
        item_key = str(kwargs.get("item_key", ""))
        if not item_key:
            return _error_json("item_key is required (e.g. 'system.cpu.util').")

        hostgroupid = kwargs.get("hostgroupid")
        hostid = kwargs.get("hostid")
        if not hostgroupid and not hostid:
            return _error_json(
                "Either hostgroupid or hostid is required."
            )

        period = str(kwargs.get("period", "7d"))
        period_sec = _parse_period(period)

        threshold = float(kwargs.get("threshold", 2.0))
        if threshold <= 0:
            return _error_json("threshold must be positive.")

        now = int(time.time())
        time_from = now - period_sec

        # --- Resolve hosts ---
        host_params: dict[str, Any] = {
            "output": ["hostid", "host", "name"],
            "filter": {"status": 0},  # enabled hosts only
        }
        if hostgroupid:
            host_params["groupids"] = [str(hostgroupid)]
        if hostid:
            host_params["hostids"] = [str(hostid)]

        hosts = client_manager.call(server_name, "host.get", host_params)
        if not hosts:
            return _error_json("No enabled hosts found matching the criteria.")

        host_map: dict[str, dict[str, str]] = {
            h["hostid"]: {"host": h["host"], "name": h.get("name", h["host"])}
            for h in hosts
        }
        host_ids = list(host_map.keys())

        # --- Fetch matching items ---
        items = client_manager.call(server_name, "item.get", {
            "output": ["itemid", "hostid", "key_", "name"],
            "hostids": host_ids,
            "search": {"key_": item_key},
            "searchByAny": True,
            "filter": {"status": 0},
        })
        if not items:
            return _error_json(
                f"No enabled items matching key '{item_key}' found on the selected hosts."
            )

        # Map hostid -> itemid (take first match per host)
        host_item: dict[str, str] = {}
        item_names: dict[str, str] = {}
        for item in items:
            hid = item["hostid"]
            if hid not in host_item:
                host_item[hid] = item["itemid"]
                item_names[hid] = item.get("name", item["key_"])

        if not host_item:
            return _error_json("No items matched after filtering.")

        # --- Fetch trend data per host ---
        host_averages: dict[str, float] = {}
        for hid, iid in host_item.items():
            trends = client_manager.call(server_name, "trend.get", {
                "output": ["clock", "value_avg", "num"],
                "itemids": [iid],
                "time_from": time_from,
                "time_till": now,
                "limit": 10000,
            })
            if not trends:
                continue

            # Weighted average across trend entries
            total_value = 0.0
            total_count = 0
            for t in trends:
                num = int(t.get("num", 1))
                avg = float(t["value_avg"])
                total_value += avg * num
                total_count += num

            if total_count > 0:
                host_averages[hid] = total_value / total_count

        if len(host_averages) < 2:
            return _error_json(
                f"Need at least 2 hosts with trend data to detect anomalies "
                f"(got {len(host_averages)})."
            )

        # --- Compute global statistics ---
        values = list(host_averages.values())
        n = len(values)
        global_mean = sum(values) / n
        variance = sum((v - global_mean) ** 2 for v in values) / n
        global_stddev = math.sqrt(variance) if variance > 0 else 0.0

        # --- Identify anomalies ---
        anomalies: list[dict[str, Any]] = []
        if global_stddev > 0:
            for hid, avg_val in host_averages.items():
                z_score = (avg_val - global_mean) / global_stddev
                if abs(z_score) >= threshold:
                    anomalies.append({
                        "hostid": hid,
                        "host": host_map[hid]["host"],
                        "name": host_map[hid]["name"],
                        "item": item_names.get(hid, item_key),
                        "avg_value": round(avg_val, 4),
                        "z_score": round(z_score, 4),
                        "global_mean": round(global_mean, 4),
                        "global_stddev": round(global_stddev, 4),
                    })

        anomalies.sort(key=lambda a: abs(a["z_score"]), reverse=True)

        return json.dumps({
            "anomalies": anomalies,
            "hosts_analyzed": len(host_averages),
            "period": period,
            "global_mean": round(global_mean, 4),
            "global_stddev": round(global_stddev, 4),
            "threshold": threshold,
        })

    except ValueError as exc:
        return _error_json(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in anomaly_detect")
        return _error_json(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# 3. Capacity Forecast
# ---------------------------------------------------------------------------


def capacity_forecast(
    client_manager: ClientManager,
    server_name: str,
    **kwargs: Any,
) -> str:
    """Forecast when a monitored metric will reach a given threshold.

    Uses linear regression on historical trend data to project future values
    and estimate the date when the threshold will be breached.

    Args:
        client_manager: The ClientManager instance for Zabbix API access.
        server_name: Name of the target Zabbix server.
        **kwargs: ``hostid`` (str, required), ``item_key`` (str, required).
            Optional: ``threshold`` (float, default 90.0), ``period`` (str,
            default ``"30d"``).

    Returns:
        JSON string with forecast details including ``days_until_threshold``,
        ``forecast_date``, ``daily_change``, ``r_squared``, etc., or an
        ``error`` key on failure.
    """
    try:
        # --- Parameter extraction & validation ---
        hostid = str(kwargs.get("hostid", ""))
        if not hostid:
            return _error_json("hostid is required.")

        item_key = str(kwargs.get("item_key", ""))
        if not item_key:
            return _error_json("item_key is required (e.g. 'vfs.fs.size[/,pused]').")

        threshold = float(kwargs.get("threshold", 90.0))
        period = str(kwargs.get("period", "30d"))
        period_sec = _parse_period(period)

        now = int(time.time())
        time_from = now - period_sec

        # --- Resolve host ---
        hosts = client_manager.call(server_name, "host.get", {
            "output": ["hostid", "host", "name"],
            "hostids": [hostid],
        })
        if not hosts:
            return _error_json(f"Host with id '{hostid}' not found.")
        host_info = hosts[0]

        # --- Resolve item ---
        items = client_manager.call(server_name, "item.get", {
            "output": ["itemid", "key_", "name", "lastvalue", "units"],
            "hostids": [hostid],
            "search": {"key_": item_key},
            "searchByAny": True,
            "filter": {"status": 0},
            "limit": 1,
        })
        if not items:
            return _error_json(
                f"No enabled item matching key '{item_key}' found on host '{hostid}'."
            )
        item = items[0]

        # --- Fetch trend data ---
        trends = client_manager.call(server_name, "trend.get", {
            "output": ["clock", "value_avg", "num"],
            "itemids": [item["itemid"]],
            "time_from": time_from,
            "time_till": now,
            "limit": 50000,
        })
        if not trends:
            return _error_json(
                f"No trend data available for item '{item_key}' on host "
                f"'{host_info['host']}' in the last {period}."
            )

        # --- Build data points (x = time in days from first point, y = value) ---
        raw_points = [
            (int(t["clock"]), float(t["value_avg"]))
            for t in trends
            if t.get("value_avg") is not None
        ]
        if len(raw_points) < 2:
            return _error_json(
                f"Insufficient trend data points ({len(raw_points)}). "
                f"Need at least 2 for regression."
            )

        raw_points.sort(key=lambda p: p[0])
        t0 = raw_points[0][0]
        # Normalise time to days from first data point
        points: list[tuple[float, float]] = [
            ((clock - t0) / 86400.0, value)
            for clock, value in raw_points
        ]

        # --- Linear regression (least squares) ---
        n = len(points)
        sum_x = sum(x for x, _y in points)
        sum_y = sum(y for _x, y in points)
        sum_xy = sum(x * y for x, y in points)
        sum_x2 = sum(x * x for x, _y in points)

        denom = n * sum_x2 - sum_x * sum_x
        if abs(denom) < 1e-12:
            return _error_json(
                "Cannot compute regression — all data points have the same timestamp."
            )

        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n

        # --- R-squared (coefficient of determination) ---
        mean_y = sum_y / n
        ss_tot = sum((y - mean_y) ** 2 for _x, y in points)
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
        r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

        # --- Current value & daily change ---
        total_days = (now - t0) / 86400.0
        current_value = slope * total_days + intercept
        daily_change = slope  # slope is already per day

        # Prefer the actual last value from Zabbix if available
        lastvalue = item.get("lastvalue")
        if lastvalue is not None:
            try:
                current_value = float(lastvalue)
            except (ValueError, TypeError):
                pass  # keep the regression estimate

        # --- Days until threshold ---
        days_until: float | None = None
        forecast_date: str | None = None

        if abs(slope) > 1e-12 and slope > 0:
            # Extrapolate from current point
            days_until = (threshold - current_value) / slope
            if days_until > 0:
                forecast_ts = now + int(days_until * 86400)
                forecast_date = datetime.fromtimestamp(
                    forecast_ts, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            else:
                # Already past threshold or decreasing toward it
                days_until = None
        elif abs(slope) > 1e-12 and slope < 0:
            # Value is decreasing — threshold for a high value will not be hit
            days_until = None

        return json.dumps({
            "hostid": hostid,
            "host": host_info["host"],
            "name": host_info.get("name", host_info["host"]),
            "item_key": item["key_"],
            "item_name": item.get("name", item["key_"]),
            "current_value": round(current_value, 4),
            "daily_change": round(daily_change, 6),
            "threshold": threshold,
            "days_until_threshold": round(days_until, 1) if days_until is not None else None,
            "forecast_date": forecast_date,
            "data_points": n,
            "r_squared": round(r_squared, 6),
            "period": period,
        })

    except ValueError as exc:
        return _error_json(str(exc))
    except Exception as exc:
        logger.exception("Unexpected error in capacity_forecast")
        return _error_json(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# 4. Item Threshold Search
# ---------------------------------------------------------------------------


def item_threshold_search(
    client_manager: ClientManager,
    server_name: str,
    **kwargs: Any,
) -> str:
    """Search items whose current lastvalue satisfies numeric threshold conditions.

    Fetches all items matching the query (via ``item.get``), then filters
    client-side by ``lastvalue``.  Items with non-numeric lastvalues (strings,
    empty, None) are silently skipped.  Results are sorted by lastvalue
    descending (highest first) by default.

    Use this instead of ``item_get`` + manual post-processing when you want to
    find items above or below a threshold — e.g. "SNAT pool usage >= 50%",
    "interface discard counter > 0", or "disk usage >= 80%".

    Args:
        client_manager: The ClientManager instance for Zabbix API access.
        server_name: Name of the target Zabbix server.
        **kwargs:
            Threshold conditions (at least one recommended):

            - ``lastvalue_gt`` (float): keep items where lastvalue > X.
            - ``lastvalue_ge`` (float): keep items where lastvalue >= X.
            - ``lastvalue_lt`` (float): keep items where lastvalue < X.
            - ``lastvalue_le`` (float): keep items where lastvalue <= X.

            Item query (all optional):

            - ``search`` (dict): Zabbix substring search, e.g.
              ``{"key_": "discards"}`` or ``{"key_": ".usage"}``.
              Zabbix matches substrings by default, so ``"discards"`` matches
              ``net.if.in.discards[eth0]``.  For wildcard matching, set
              ``extra_params={"searchWildcardsEnabled": True}`` and use
              ``{"key_": "*.usage"}``-style patterns.
            - ``filter`` (dict): exact-match filter, e.g. ``{"type": 0}``.
            - ``hostids`` (list[str]): restrict to these host IDs.
            - ``groupids`` (list[str]): restrict to these host group IDs.
            - ``output`` (str): fields to include per result item.
              Default: ``"itemid,name,key_,lastvalue"``.  ``"extend"`` returns
              all fields.  ``lastvalue`` is always injected when missing,
              as it is required for threshold filtering.
            - ``extra_params`` (dict): additional Zabbix item.get parameters,
              e.g. ``{"selectHosts": ["host"]}`` to embed the host name.

            Result control:

            - ``sort_desc`` (bool): sort by lastvalue descending (default:
              True — highest values first).
            - ``result_limit`` (int): max matched items to return after
              filtering and sorting.

    Returns:
        JSON string ``{"scanned": N, "matched": M, "returned": R, "items": [...]}``,
        where *scanned* is the total items from ``item.get``, *matched* is the
        count passing the threshold filter, and *returned* is the number of items
        actually included (equal to *matched* unless ``result_limit`` is set).
        On error, returns ``{"error": "..."}``.
    """
    try:
        sort_desc = bool(kwargs.get("sort_desc", True))
        result_limit = kwargs.get("result_limit")
        extra_params: dict[str, Any] = kwargs.get("extra_params") or {}

        # extra_params merged first so explicit arguments take precedence on conflict
        params: dict[str, Any] = dict(extra_params)

        output = kwargs.get("output") or "itemid,name,key_,lastvalue"
        if output == "count":
            return _error_json(
                "output='count' is not supported by item_threshold_search. "
                "The tool reports scanned/matched counts separately in its response."
            )
        if output != "extend":
            fields = [f.strip() for f in output.split(",") if f.strip()]
            if "lastvalue" not in fields:
                fields.append("lastvalue")
            params["output"] = fields
        else:
            params["output"] = output

        for key in ("filter", "search", "hostids", "groupids"):
            val = kwargs.get(key)
            if val is not None:
                params[key] = val

        items: list[dict[str, Any]] = client_manager.call(server_name, "item.get", params)
        scanned = len(items)

        raw_gt = kwargs.get("lastvalue_gt")
        raw_ge = kwargs.get("lastvalue_ge")
        raw_lt = kwargs.get("lastvalue_lt")
        raw_le = kwargs.get("lastvalue_le")
        gt = float(raw_gt) if raw_gt is not None else None
        ge = float(raw_ge) if raw_ge is not None else None
        lt = float(raw_lt) if raw_lt is not None else None
        le = float(raw_le) if raw_le is not None else None

        matched_with_vals: list[tuple[float, dict[str, Any]]] = []
        for item in items:
            raw = item.get("lastvalue")
            if raw is None or raw == "":
                continue
            try:
                val = float(raw)
            except (ValueError, TypeError):
                continue
            if gt is not None and val <= gt:
                continue
            if ge is not None and val < ge:
                continue
            if lt is not None and val >= lt:
                continue
            if le is not None and val > le:
                continue
            matched_with_vals.append((val, item))

        matched_with_vals.sort(key=lambda x: x[0], reverse=sort_desc)
        matched = [item for _, item in matched_with_vals]
        total_matched = len(matched)

        if result_limit is not None:
            matched = matched[: int(result_limit)]

        return json.dumps({
            "scanned": scanned,
            "matched": total_matched,
            "returned": len(matched),
            "items": matched,
        }, ensure_ascii=False)

    except Exception as exc:
        logger.exception("Unexpected error in item_threshold_search")
        return _error_json(f"Unexpected error: {exc}")


# ---------------------------------------------------------------------------
# 5. Active problems view
# ---------------------------------------------------------------------------
#
# Original concept and implementation by @fenbays
# (https://github.com/fenbays/zabbix-mcp-server, commit b38eeb2).
# Ported and adapted to initMAX style: English-only, hostgroup/severity
# overrides, no Zabbix-version compatibility fallbacks (we target 6.4+),
# reuses _filter_active_problems so problem_get(monitored=True) and
# problem_active_get share the same filter pass.

_SEVERITY_LABELS: dict[str, str] = {
    "0": "not classified",
    "1": "information",
    "2": "warning",
    "3": "average",
    "4": "high",
    "5": "disaster",
}


def _filter_active_problems(
    problems: list[dict[str, Any]],
    client_manager: ClientManager,
    server_name: str,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]]]:
    """Drop problems whose trigger or host is disabled.

    Used by both ``problem_get(monitored=True)`` and ``problem_active_get``.

    Returns a tuple of:
        - kept: the subset of *problems* whose objectid (triggerid) maps to
          an enabled trigger AND has at least one enabled host.
        - host_by_trigger: mapping triggerid -> {"host": <name>, "hostid": <id>}
          for the enabled host of each kept trigger. Empty for callers that do
          not need it (the auto-handler ignores it).
    """
    if not problems:
        return [], {}

    trigger_ids = sorted({str(p["objectid"]) for p in problems if p.get("objectid")})
    if not trigger_ids:
        return [], {}

    triggers = client_manager.call(server_name, "trigger.get", {
        "output": ["triggerid", "status"],
        "triggerids": trigger_ids,
        "selectHosts": ["hostid", "host", "name", "status"],
        "filter": {"status": 0},  # enabled triggers only
    })

    host_by_trigger: dict[str, dict[str, str]] = {}
    for t in triggers:
        enabled_hosts = [h for h in t.get("hosts", []) if str(h.get("status", "1")) == "0"]
        if enabled_hosts:
            h = enabled_hosts[0]
            host_by_trigger[str(t["triggerid"])] = {
                "host": h.get("name") or h.get("host", ""),
                "hostid": str(h.get("hostid", "")),
            }

    kept = [p for p in problems if str(p.get("objectid", "")) in host_by_trigger]
    return kept, host_by_trigger


def _ts_to_human(ts: Any) -> str:
    """Render a Zabbix Unix timestamp as 'YYYY-MM-DD HH:MM UTC'."""
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError, OSError):
        return ""


def problem_active_get(
    client_manager: ClientManager,
    server_name: str,
    **kwargs: Any,
) -> str:
    """Active problems on enabled triggers and hosts, with LLM-friendly fields.

    Wraps ``problem.get`` with three opinionated filters baked in:

    1. Drop problems whose trigger is disabled.
    2. Drop problems whose host is disabled.
    3. Default ``severities = [2, 3, 4, 5]`` - Warning and above only.

    Returns each problem augmented with ``host``, ``hostid``, ``time`` (human
    readable UTC), and ``severity_label``.

    Args:
        client_manager: ClientManager instance.
        server_name: Target Zabbix server name.
        **kwargs:
            - ``severities`` (list[int]): Override the default severity floor.
              Default: [2, 3, 4, 5]. Pass [0,1,2,3,4,5] to include Information.
            - ``hostids`` (list[str]): Restrict to these hosts.
            - ``groupids`` (list[str]): Restrict to these host groups.
            - ``limit`` (int): Max problems to return after filtering.
              Default: 50.
            - ``sortfield`` (str): Sort key (default "eventid").
            - ``sortorder`` (str): "ASC" or "DESC" (default "DESC").

    Returns:
        JSON ``{"problems": [...], "count": N, "filtered_out": M}`` where
        *count* is kept problems and *filtered_out* is the number dropped due
        to disabled trigger/host. On error, ``{"error": "..."}``.
    """
    try:
        severities = kwargs.get("severities") or [2, 3, 4, 5]
        limit = int(kwargs.get("limit", 50))
        sortfield = kwargs.get("sortfield", "eventid")
        sortorder = kwargs.get("sortorder", "DESC")

        params: dict[str, Any] = {
            "output": "extend",
            "selectAcknowledges": "count",
            "severities": severities,
            "sortfield": sortfield,
            "sortorder": sortorder,
            # Pull a wider set than `limit` so the post-filter still has room
            # to return `limit` valid rows when a chunk is on disabled hosts.
            "limit": max(limit * 4, limit + 50),
        }
        for k in ("hostids", "groupids"):
            if kwargs.get(k):
                params[k] = kwargs[k]

        problems: list[dict[str, Any]] = client_manager.call(server_name, "problem.get", params)
        total = len(problems)

        kept, host_map = _filter_active_problems(problems, client_manager, server_name)

        enriched = []
        for p in kept[:limit]:
            tid = str(p.get("objectid", ""))
            sev = str(p.get("severity", "0"))
            host_info = host_map.get(tid, {"host": "", "hostid": ""})
            enriched.append({
                "eventid": p.get("eventid", ""),
                "triggerid": tid,
                "host": host_info["host"],
                "hostid": host_info["hostid"],
                "name": p.get("name", ""),
                "severity": sev,
                "severity_label": _SEVERITY_LABELS.get(sev, sev),
                "clock": p.get("clock", ""),
                "time": _ts_to_human(p.get("clock")),
                "acknowledged": int(p.get("acknowledged", 0) or 0),
            })

        return json.dumps({
            "problems": enriched,
            "count": len(enriched),
            "filtered_out": total - len(kept),
        }, ensure_ascii=False)

    except Exception as exc:
        logger.exception("Unexpected error in problem_active_get")
        return _error_json(f"Unexpected error: {exc}")
