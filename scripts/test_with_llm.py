#!/usr/bin/env python3
"""End-to-end smoke test driving the MCP server through a real LLM.

This is the "real-world" complement to ``test_all_tools.py``. Instead of
calling each tool with hand-crafted arguments, we feed the LLM a natural
operator question (``"Which hosts are currently down?"``) plus the full
~230-tool catalog, then let the model pick and chain tools the way it
would during an actual chat session.

Per-scenario validation:

1. The LLM made at least one tool call (didn't hallucinate the answer).
2. None of the tool calls returned ``isError: True``.
3. The final assistant message does not contain refusal phrases like
   "I cannot" / "I do not have access" / "I am unable to" - those signal
   that the LLM gave up on the tool path.

LLM provider: OpenAI Chat Completions (gpt-4o-mini by default for cost,
override with ``--model gpt-4o`` for the strongest run). API key comes
from ``[admin.ai].api_key`` in the running server's config.

Output: markdown report with one row per scenario - tools called,
final answer excerpt, pass / fail status.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


REFUSAL_PATTERNS = [
    r"i cannot\b",
    r"i can't\b",
    r"i am unable\b",
    r"i'm unable\b",
    r"i do not have access\b",
    r"i don't have access\b",
    r"i don't have the ability\b",
    r"unable to retrieve\b",
    r"could not access\b",
    r"failed to access\b",
]

SYSTEM_PROMPT = (
    "You are an experienced Zabbix monitoring engineer assisting an operator. "
    "You have direct access to the Zabbix MCP tool catalog. For every question "
    "you MUST use the appropriate tool(s) to fetch real data - do not answer "
    "from general knowledge. Chain tools when needed (e.g. resolve host name "
    "to id with host_get, then call item_get). Default to the configured "
    "default Zabbix server unless the user names a specific server. After tool "
    "calls succeed, summarise the result for the operator in plain English; do "
    "not paste raw JSON. If a tool returns an error, say so explicitly."
)


# Tool buckets. OpenAI Chat Completions caps the ``tools`` array at 128
# entries; the catalog has ~230. Each scenario picks one bucket; the
# LLM still freely chooses among everything in the bucket so it remains
# tool-agnostic within its operational area. Buckets are tuned so the
# obvious tool path is always reachable from inside the bucket.
TOOLSETS = {
    # General read + light triage. Covers ~110 tools.
    "monitoring": [
        # Hosts & topology
        "host_get", "host_create", "host_update", "host_delete",
        "hostgroup_get", "hostgroup_create", "hostgroup_update", "hostgroup_delete",
        "hostinterface_get", "hostinterface_create", "hostinterface_update", "hostinterface_delete",
        # Items / triggers / events
        "item_get", "item_create", "item_update", "item_delete",
        "item_threshold_search",
        "trigger_get", "trigger_create", "trigger_update", "trigger_delete",
        "problem_get", "problem_active_get", "event_get", "event_acknowledge",
        # History / trends / graphs
        "history_get", "trend_get",
        "graph_get", "graphitem_get", "graph_render",
        # Templates
        "template_get", "templategroup_get",
        # Discovery
        "drule_get", "dservice_get", "dhost_get", "dcheck_get",
        "discoveryrule_get", "httptest_get",
        # Maintenance / SLA
        "maintenance_get", "maintenance_create", "maintenance_update", "maintenance_delete",
        "sla_get",
        # Extensions
        "anomaly_detect", "capacity_forecast", "report_generate",
        "health_check", "zabbix_raw_api_call",
    ],
    # Administration & users.
    "admin": [
        "settings_get", "housekeeping_get", "authentication_get", "autoregistration_get",
        "task_get", "auditlog_get",
        "user_get", "usergroup_get", "role_get",
        "mediatype_get", "script_get", "script_getscriptsbyhosts",
        "valuemap_get", "regexp_get",
        "proxy_get", "proxygroup_get", "iconmap_get", "image_get",
        "dashboard_get", "templatedashboard_get",
        "host_get", "hostgroup_get",  # context lookups
        "health_check", "zabbix_raw_api_call",
    ],
    # CRUD lifecycle - exposes both reads (so the LLM can confirm work)
    # and the relevant write tools.
    "crud": [
        "host_get", "host_create", "host_update", "host_delete",
        "hostgroup_get", "hostgroup_create", "hostgroup_update", "hostgroup_delete",
        "hostinterface_get", "hostinterface_create", "hostinterface_update", "hostinterface_delete",
        "item_get", "item_create", "item_update", "item_delete",
        "trigger_get", "trigger_create", "trigger_update", "trigger_delete",
        "template_get", "templategroup_get",
        "health_check", "zabbix_raw_api_call",
    ],
}


# Tool-agnostic, realistic operator questions. LLM picks tools itself.
# Each scenario picks a TOOLSETS bucket; the LLM still freely chooses
# among all tools in the bucket.
SCENARIOS: list[dict[str, Any]] = [
    # === Monitoring / triage ===
    {"id": "hosts_listing", "tools": "monitoring", "prompt": "List the first 5 hosts being monitored, with their status."},
    {"id": "problems_now", "tools": "monitoring", "prompt": "What problems are currently active on the Zabbix server? Show severity and host."},
    {"id": "problems_active_only", "tools": "monitoring", "prompt": "I only want REAL problems right now - skip anything from disabled triggers or hosts that are no longer being monitored. Give me 5 with host name, severity label, and when they fired."},
    {"id": "hosts_down", "tools": "monitoring", "prompt": "Which hosts are currently offline or unreachable?"},
    {"id": "host_groups", "tools": "monitoring", "prompt": "What host groups exist? Just the names."},
    {"id": "templates_list", "tools": "monitoring", "prompt": "List 5 templates configured on this Zabbix instance."},
    {"id": "events_recent", "tools": "monitoring", "prompt": "Show me the 5 most recent events of any kind."},
    {"id": "history_for_host", "tools": "monitoring", "prompt": "Pick any monitored host and show me its CPU utilization values for the last 30 minutes."},
    {"id": "triggers_high_severity", "tools": "monitoring", "prompt": "List all triggers with high severity (severity 4 or 5)."},
    {"id": "items_high_value", "tools": "monitoring", "prompt": "Find all items where the current lastvalue is greater than 50, limit to 5 results."},

    # === Discovery / topology ===
    {"id": "host_interfaces", "tools": "monitoring", "prompt": "Pick a host and tell me what network interfaces it has configured."},
    {"id": "graphs_per_host", "tools": "monitoring", "prompt": "Pick any host and list its graphs."},
    {"id": "discovery_rules", "tools": "monitoring", "prompt": "Are there any network discovery rules defined?"},
    {"id": "httptest_list", "tools": "monitoring", "prompt": "List configured HTTP web scenarios (httptest)."},

    # === Administration ===
    {"id": "settings_global", "tools": "admin", "prompt": "What is the configured server name (global setting)?"},
    {"id": "housekeeping_history", "tools": "admin", "prompt": "How long is history kept according to housekeeping settings?"},
    {"id": "auth_method", "tools": "admin", "prompt": "What is the default authentication method on this Zabbix server?"},
    {"id": "user_list", "tools": "admin", "prompt": "List the user accounts on the Zabbix server."},
    {"id": "user_groups", "tools": "admin", "prompt": "Which user groups are defined?"},
    {"id": "media_types", "tools": "admin", "prompt": "What media types (notification channels) are configured?"},
    {"id": "scripts_list", "tools": "admin", "prompt": "What scripts can be run from the Zabbix frontend?"},
    {"id": "audit_recent", "tools": "admin", "prompt": "Show me the 5 most recent audit log entries."},
    {"id": "maintenance_active", "tools": "monitoring", "prompt": "Are there any active maintenance windows right now?"},

    # === Analytics extensions ===
    {"id": "capacity_forecast", "tools": "monitoring", "prompt": "Pick a host and forecast when its CPU utilization (system.cpu.util) might reach 90%."},
    {"id": "anomaly_check", "tools": "monitoring", "prompt": "Detect any anomalous hosts in the largest host group based on CPU usage in the last 7 days."},
    {"id": "graph_render", "tools": "monitoring", "prompt": "Render any one graph as PNG and tell me the dimensions in your reply."},
    {"id": "health_check", "tools": "monitoring", "prompt": "Run the MCP health check and report the connectivity status."},

    # === Reporting (may skip if weasyprint missing) ===
    {"id": "report_availability", "tools": "monitoring", "prompt": "Generate an availability report for the last 7 days for any host group. Tell me roughly how big the resulting PDF is."},

    # === Write-side: full CRUD lifecycle in ONE conversation ===
    # Each scenario has its own messages array, so multi-step CRUD must
    # live inside a single prompt. The LLM chains 5 calls in sequence.
    {"id": "crud_full_lifecycle", "tools": "crud", "prompt": (
        "Walk through this full CRUD lifecycle as a single chain of tool calls, "
        "without asking me to confirm between steps:\n"
        "1. Create a host group called 'llm-smoke-temp'.\n"
        "2. Create a host called 'llm-smoke-host' in that newly-created group, "
        "with a default Zabbix agent interface on 127.0.0.1 port 10050.\n"
        "3. Update the host's description to 'created by LLM smoke test'.\n"
        "4. Delete the host.\n"
        "5. Delete the host group.\n"
        "Tell me at the end what worked and what (if anything) failed, and "
        "include the IDs you saw along the way."
    )},
]


@dataclass
class ScenarioResult:
    id: str
    prompt: str
    status: str  # "ok" | "error" | "refused" | "no_tool"
    tool_calls: list[str] = field(default_factory=list)
    tool_errors: list[str] = field(default_factory=list)
    final_text: str = ""
    elapsed_ms: int = 0
    detail: str = ""


def mcp_tool_to_openai(tool) -> dict[str, Any]:
    """Map mcp.types.Tool -> OpenAI ``tools`` array entry."""
    schema = tool.inputSchema or {"type": "object", "properties": {}}
    # OpenAI requires properties to be a dict (even if empty) and disallows
    # certain top-level fields the MCP schema includes (e.g. $defs, allOf).
    schema = {**schema, "additionalProperties": False} if schema.get("type") == "object" else schema
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "")[:1024],
            "parameters": schema,
        },
    }


def parse_payload(text: str) -> Any:
    """Parse an MCP tool result body, tolerating the security preamble."""
    if text.startswith("[System:"):
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


async def run_scenario(
    s: ClientSession,
    openai_client: OpenAI,
    model: str,
    tools_by_name: dict[str, dict[str, Any]],
    scenario: dict[str, Any],
    server_name: str,
    max_iterations: int = 12,
) -> ScenarioResult:
    """Drive one operator question end-to-end through the LLM."""
    res = ScenarioResult(id=scenario["id"], prompt=scenario["prompt"], status="ok")
    t0 = time.time()

    # Resolve the bucket name to actual tool definitions. Tools missing
    # from the registered catalog (e.g. weasyprint not installed -> no
    # ``report_generate``) are silently dropped from the bucket.
    bucket_name = scenario.get("tools", "monitoring")
    bucket_names = TOOLSETS.get(bucket_name, [])
    bucket = [tools_by_name[n] for n in bucket_names if n in tools_by_name]

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT
            + f"\n\nThe configured Zabbix server name to pass as the `server` "
              f"parameter on each tool call is: {server_name!r}."},
        {"role": "user", "content": scenario["prompt"]},
    ]

    for _ in range(max_iterations):
        try:
            response = openai_client.chat.completions.create(
                model=model,
                messages=messages,
                tools=bucket,
                tool_choice="auto",
                parallel_tool_calls=False,
                temperature=0,
            )
        except Exception as e:
            res.status = "error"
            res.detail = f"OpenAI API error: {type(e).__name__}: {e}"
            res.elapsed_ms = int((time.time() - t0) * 1000)
            return res

        choice = response.choices[0]
        msg = choice.message

        # If the model produced text and no tool calls, we're done.
        if not msg.tool_calls:
            res.final_text = (msg.content or "").strip()
            break

        # Append the assistant message with the tool_calls it requested.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            res.tool_calls.append(name)

            try:
                tool_result = await asyncio.wait_for(
                    s.call_tool(name, args), timeout=30,
                )
            except asyncio.TimeoutError:
                tool_text = '{"error": "timeout 30s"}'
                res.tool_errors.append(f"{name}: timeout")
            else:
                tool_text = tool_result.content[0].text if tool_result.content else ""
                if tool_result.isError:
                    res.tool_errors.append(f"{name}: {tool_text[:120]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_text[:4000],
            })
    else:
        res.status = "error"
        res.detail = f"hit max_iterations={max_iterations}"
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res

    res.elapsed_ms = int((time.time() - t0) * 1000)

    # ---- Validation ----
    if not res.tool_calls:
        res.status = "no_tool"
        res.detail = "LLM answered without calling any tool"
    elif any(re.search(p, res.final_text, re.IGNORECASE) for p in REFUSAL_PATTERNS):
        res.status = "refused"
        res.detail = "Final answer contained a refusal phrase - LLM gave up on the tool path"
    elif res.tool_errors:
        # Tool errors reported but model still produced a final answer:
        # mark "error" if every tool failed, "ok" if at least one succeeded.
        if len(res.tool_errors) >= len(res.tool_calls):
            res.status = "error"
            res.detail = res.tool_errors[0]

    return res


async def main_async(args: argparse.Namespace) -> int:
    # Load OpenAI key from config.toml [admin.ai].api_key unless overridden.
    api_key = args.openai_key
    if not api_key:
        import tomllib
        with open(args.config, "rb") as f:
            cfg = tomllib.load(f)
        api_key = cfg.get("admin", {}).get("ai", {}).get("api_key")
    if not api_key:
        print("ERROR: no OpenAI API key. Pass --openai-key or set [admin.ai].api_key in config.")
        return 2

    openai_client = OpenAI(api_key=api_key)

    headers = {"Authorization": f"Bearer {args.token}"}
    print(f"=== Connecting to {args.url} (Zabbix server: {args.server}) ===")
    async with streamablehttp_client(args.url, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            tools = (await s.list_tools()).tools
            print(f"  protocol={init.protocolVersion}  tools={len(tools)}")

            openai_tools_by_name = {t.name: mcp_tool_to_openai(t) for t in tools}

            results: list[ScenarioResult] = []
            scenarios = SCENARIOS if not args.only else [s for s in SCENARIOS if s["id"] in args.only.split(",")]
            for sc in scenarios:
                print(f"\n--- {sc['id']}: {sc['prompt'][:80]} ---")
                r = await run_scenario(
                    s, openai_client, args.model, openai_tools_by_name, sc, args.server,
                )
                marker = {"ok": "\033[32m✓\033[0m", "error": "\033[31m✗\033[0m",
                          "refused": "\033[33m!\033[0m", "no_tool": "\033[33m?\033[0m"}.get(r.status, "?")
                tools_summary = ", ".join(r.tool_calls[:5]) + (" ..." if len(r.tool_calls) > 5 else "")
                print(f"  {marker} tools=[{tools_summary or 'NONE'}]  ({r.elapsed_ms}ms)")
                if r.detail:
                    print(f"     detail: {r.detail[:160]}")
                if r.final_text:
                    print(f"     final: {r.final_text[:160]}")
                results.append(r)

    # ---- Report ----
    ok = sum(1 for r in results if r.status == "ok")
    err = sum(1 for r in results if r.status == "error")
    ref = sum(1 for r in results if r.status == "refused")
    no_tool = sum(1 for r in results if r.status == "no_tool")

    print(f"\n=== Summary: {ok} ok | {err} error | {ref} refused | {no_tool} no_tool / {len(results)} total ===")

    lines = [
        "# LLM-driven smoke test report",
        "",
        f"- Zabbix server: `{args.server}`",
        f"- LLM model: `{args.model}`",
        f"- Total scenarios: {len(results)}",
        f"- OK: {ok}  |  Error: {err}  |  Refused: {ref}  |  No tool used: {no_tool}",
        f"- Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        "",
        "## Per-scenario results",
        "",
        "| ID | Status | Tools used | Final answer (excerpt) |",
        "|---|---|---|---|",
    ]
    for r in results:
        marker = {"ok": "✓ OK", "error": "✗ ERROR", "refused": "! REFUSED", "no_tool": "? NO_TOOL"}.get(r.status, r.status)
        tools_cell = ", ".join(f"`{t}`" for t in r.tool_calls) if r.tool_calls else "_(none)_"
        excerpt = (r.final_text[:200].replace("|", "\\|").replace("\n", " ")
                   if r.final_text else r.detail[:200].replace("|", "\\|"))
        lines.append(f"| `{r.id}` | {marker} | {tools_cell} | {excerpt} |")

    if any(r.tool_errors for r in results):
        lines.append("")
        lines.append("## Tool-level errors observed")
        lines.append("")
        for r in results:
            for err_msg in r.tool_errors:
                lines.append(f"- **{r.id}**: {err_msg}")

    with open(args.report, "w") as f:
        f.write("\n".join(lines))
    print(f"\n  report -> {args.report}")
    return 0 if err == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://127.0.0.1:18081/mcp")
    p.add_argument("--token", required=True, help="MCP bearer token")
    p.add_argument("--server", default="Wiki-topics")
    p.add_argument("--config", default="config.toml", help="Path to config.toml (for OpenAI key)")
    p.add_argument("--openai-key", default=None, help="Override OpenAI API key")
    p.add_argument("--model", default="gpt-4o-mini")
    p.add_argument("--report", default="llm_smoke_report.md")
    p.add_argument("--only", default=None, help="Comma-separated scenario ids to run")
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
