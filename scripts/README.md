# Pre-release smoke runners

The two smoke runners target the running MCP server's HTTP transport
via the official `mcp` Python SDK; they are part of the pre-release ritual
documented in `CONTRIBUTING.md`. Wire-compatible with any Zabbix MCP
deployment, but the canonical target is the `Wiki-topics` test instance
(`https://student-postgresql-01.initmax.cz`).

## `test_all_tools.py` - synthetic CRUD coverage

Calls every tool listed by `tools/list` with hand-crafted minimal arguments
derived from sample IDs probed up front. Read-only tools (`*_get`,
`*_export`, ...) get `output=extend` queries; write tools get a
parent-before-child create / update / delete lifecycle. Reports pass /
fail / skip per tool to a markdown file.

```bash
.venv/bin/python scripts/test_all_tools.py \
  --token "<bearer>" \
  --server "Wiki-topics" \
  --report tools_test_report.md
```

What it catches: schema drift between Zabbix versions, ordering bugs in
the wrapper, broken `_make_tool_handler` glue, output normalisation
issues. What it does **not** catch: anything that depends on how an LLM
reads the tool description (see below).

## `test_with_llm.py` - real-LLM driven coverage

Drives the MCP server through OpenAI's Chat Completions API. For each of
~28 realistic operator scenarios ("List the first 5 hosts being
monitored", "Walk through this full CRUD lifecycle"), the LLM picks tools
from a curated bucket and chains them; we validate the final answer is
not a refusal and that no tool returned `isError: true`.

```bash
.venv/bin/python scripts/test_with_llm.py \
  --token "<bearer>" \
  --server "Wiki-topics" \
  --model "gpt-4o" \
  --report llm_smoke_report.md
```

OpenAI key is read from `[admin.ai].api_key` in the running server's
config (override with `--openai-key` or by editing `--config`). One full
run on `gpt-4o` is roughly $0.50 and takes 5-10 minutes.

What it catches: tool description / UX bugs that fool LLMs even when the
synthetic runner passes. v1.26 caught that the original `CREATE_PARAMS`
description lacked the explicit `{"params": {...}}` wrap example, which
made gpt-4o-mini fail every write call before the description fix landed.

## `build_get_contracts.py` / `audit_get_contracts_live.py` - get-method contracts

`src/zabbix_mcp/api/get_contracts.json` (what every `*_get` tool offers in
`output`, `filter`, `search`, `sortfield` and `extra_params`, and what the
server refuses before the call) is generated, never edited by hand:

```bash
# merge the per-page catalogs collected from the API reference (5.0 ... 8.0)
# and the live observations into the shipped file
python3 scripts/build_get_contracts.py scripts/get_contracts_src/catalog_*.json \
  --observed scripts/get_contracts_src/observed_*.json

# check the catalog against a real server (every property, sort field and
# select of every method, plus a filter / search probe per property);
# writes get_contracts_observed_<version>.json into the current directory
python3 scripts/audit_get_contracts_live.py https://zabbix.example.com <api-token>
python3 scripts/audit_get_contracts_live.py https://zabbix.example.com Admin <password>
```

The audit ends with `0 problems` on every release the catalog claims to
know; run it against each one before shipping a catalog change, then
re-run the builder with the fresh `observed_*.json` files so what a server
returns beyond the documentation (and what it refuses or ignores) is
marked in the shipped file. Releases before 6.4 are logged in with the
body `auth` field automatically.

## Pre-release ritual (recap)

Both runners must report green before tagging a release. Plus the OS
matrix:

```bash
cd tests/installer && ./run_all.sh   # 18 distros, ~30 min on first run
```

See `CONTRIBUTING.md` and the `feedback_release_checklist` memory for the
full checklist.
