# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

`uv` is NOT installed on every dev machine here, and Windows has `python`
rather than `python3`. Every command below is given in the form that works
without `uv`; prefix with `uv run` if you have it.

```bash
# Install dependencies
pip install -r requirements.txt      # or: uv sync
pip install -r requirements-dev.txt  # pytest, jsonschema, pre-commit

# Run local MCP server (no Lambda needed). Routes through the SAME
# UniversalHTTPHandler as the Lambda adapter, so the Origin and
# MCP-Protocol-Version checks are exercised locally.
python scripts/local_server.py       # Serves on http://localhost:8000/mcp
# Or: python local_server.py         # Alternate entry point, serves on / and /mcp
# On Windows set PYTHONUTF8=1 first -- the startup banner prints emoji.

# Validate config
python -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"

# Tests
python -m pytest tests/ -q                                           # All tests
python -m pytest tests/test_ckan_plugin.py -v                        # Single file
python -m pytest tests/test_ckan_plugin.py::TestClass::test_name -v  # Single test
python -m pytest tests/ --cov=core --cov=plugins --cov-report=term-missing
# No coverage threshold is configured -- the report is informational.

# Linting (ruff)
python -m ruff check core/ plugins/ server/ tests/       # Check
python -m ruff check core/ plugins/ server/ tests/ --fix # Auto-fix
python -m ruff format core/ plugins/ server/ tests/      # Format
# NOTE: several files carry pre-existing format drift under newer ruff than
# the pinned pre-commit version. Do NOT run a wholesale `ruff format` --
# format only the files you touched.

# Smoke test a deployed endpoint (55 HTTP-level checks)
python scripts/smoke_http.py [prod|staging|<url>]
# Works against the local server too:
#   python scripts/smoke_http.py http://localhost:8000/mcp

# Pre-commit hooks
pre-commit run --all-files

# Go client (requires Go 1.21+)
cd client && make build

# Deploy to AWS (repackages, plans, applies)
bash ./scripts/deploy.sh --environment <staging|prod> --yes
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin. This is enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). To deploy multiple MCP servers, fork the repo per plugin.

**Request flow:**
```
Claude (stdio) → Go client (client/) or stdio_bridge.py → HTTP POST /mcp
  → Lambda (server/adapters/aws_lambda.py) or local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → Plugin → External API
```

**Key modules:**
- `core/interfaces.py` — Abstract bases: `MCPPlugin`, `DataPlugin`, plus `ToolDefinition` (name / title / description / input_schema / output_schema / annotations), `ToolResult` (content + structured_content), `ToolInputError`, `PluginType` enum
- `core/plugin_manager.py` — Discovers plugins by scanning `plugins/` and `custom_plugins/` for `plugin.py` files. Registers tools with `pluginname__toolname` prefix. Routes `tools/call` to the correct plugin.
- `core/mcp_server.py` — Handles MCP JSON-RPC methods: `initialize`, `tools/list`, `tools/call`, `ping`. Also defines `JsonRpcError`, which carries a caller-facing error code and is logged at WARNING with no traceback.
- `core/validators.py` — Loads and validates `config.yaml`. Enforces the single-plugin rule.
- `server/adapters/aws_lambda.py` — AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`). Also `server/lambda_handler.py` as legacy entry point.
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server (alternative to Go client)

**Plugins** (`plugins/`): `anchorage_parcels` (the one this fork deploys), `anchorage_gis`, plus the `ckan`, `arcgis` and `socrata` templates, which implement `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`. `plugins/arcgis/where_validator.py` is shared — `anchorage_parcels`, `anchorage_gis` and `arcgis` all import its WHERE / order_by / out_fields validators, so a change there affects all three. Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-example.yaml` to `config.yaml`. Enable exactly one plugin. Config supports `${ENV_VAR}` substitution.

`config.yaml` is gitignored; the tracked source for this fork is
`config-anchorage-parcels.yaml`. Keep the two in sync.

On Lambda the config ships **inside the deployment package** and is read from
`$LAMBDA_TASK_ROOT/config.yaml`, NOT from the `OPENCONTEXT_CONFIG` env var --
Terraform deliberately sets that variable to the empty string, because AWS caps
env vars at 4KB and this config exceeds it. `OPENCONTEXT_CONFIG` is still
honoured when set, either as serialized JSON or as a path to a YAML file (the
local-server convention).

**Timeout ladder**, which must stay ordered: REST API Gateway enforces a hard,
non-adjustable **29s** integration timeout > `aws.lambda_timeout` **28s** > the
enabled plugin's own HTTP `timeout` **20s**. A plugin timeout above the Lambda's
means a hung upstream kills the Lambda mid-flight instead of returning a
readable tool error.

**Config precedence is easy to get wrong and fails silently:**
`aws.lambda_timeout` and `aws.lambda_memory` in `config.yaml` OVERRIDE
`terraform/aws/*.tfvars`, while `lambda_name` works the opposite way.
`terraform/aws/config.yaml` and `terraform/aws/lambda-deployment.zip` are build
artifacts that `deploy.sh` overwrites — editing them directly is a no-op, and a
`terraform plan` run without repackaging reads the stale copies and shows only a
code-hash change, hiding config edits. Plugin-level settings live inside the zip
and NEVER appear as a Terraform attribute diff, so `deploy.sh` verifies the
packaged config against the source on every run.

## MCP conformance invariants

These are contracts, not preferences — each is enforced by a test. If you change
the behaviour, change the test deliberately rather than deleting it.

- **Protocol negotiation** — supported revisions are a tuple in
  `MCPServer.SUPPORTED_PROTOCOL_VERSIONS`. `initialize` echoes the client's version
  when recognized, otherwise answers with the newest supported. An absent
  `MCP-Protocol-Version` header means the spec-defined 2025-03-26 and is NOT an
  error; an unsupported one is HTTP 400 with `-32600` — deliberately not `-32022`,
  so a dual-era client reads it as a legacy server and falls back to the handshake.
  `initialize` itself is exempt from that header check, or a client naming an
  unsupported revision could never reach the handshake that would settle on a
  supported one.
- **Error codes** — `ping` returns `{}`. Unknown method is `-32601`. Unknown tool and
  malformed `tools/call` (non-object `params`, missing `name`, non-object
  `arguments`) are `-32602`.
- **Caller errors never log a traceback.** Raise `ToolInputError` (a `ValueError`
  subclass) for anything the caller got wrong; it logs at WARNING. Genuine server or
  upstream faults keep `ERROR` plus `exc_info` — that is what those logs are for.
  Static drift guards in `tests/test_anchorage_parcels_plugin.py` pin the count of
  plain `raise ValueError(` and AST-sweep for unguarded `int()`/`float()` over
  caller input, because a line-oriented search misses inline coercions.
- **A declared `outputSchema` is BINDING.** Every return path of a tool that declares
  one must emit conforming `structuredContent` — including the empty, truncated,
  not-found and ambiguous branches. Tests validate real tool output against each
  declared schema with `jsonschema`. Do not over-constrain: statistic values can be
  strings or null, counts can be null, and rows carry whatever `out_fields` asked for.
- **Prose and `caveats` are generated from one list** (`_Caveats`), so the
  human-readable text and the machine-readable array cannot drift. A test asserts
  every structured caveat message appears verbatim in the rendered text.
- **The Origin allowlist is enforced** with HTTP 403 before any routing or plugin
  code, not merely reflected in CORS headers. Requests with NO Origin are allowed —
  DNS rebinding is browser-only and every native client sends none.

## CI

`.github/workflows/ci.yml` runs on push to main/develop and on every PR:

- **Lint & test (Python 3.11, the Lambda runtime)** — `ruff check`, `ruff format
  --check`, a config-validation step that asserts the timeout ladder still holds
  (plugin < lambda < API Gateway's hard 29s), then the full pytest suite with
  coverage. This is what enforces the conformance invariants above.
- **pip-audit** on `requirements.txt` — the runtime deps that ship in the zip.
- **Go client** — `go vet` and `go test` in `client/`.

Two things to know before editing it:

- **Ruff is pinned to 0.15.1** to match `.pre-commit-config.yaml`. The formatter
  is not stable across releases, so an unpinned CI install would disagree with
  the hook developers run locally.
- **The format step excludes eight files** whose drift predates the gate. That
  is what lets the gate block NEW drift without a repo-wide reformat first. The
  list should only ever shrink — delete an entry once its file is formatted, and
  do not add to it.

Coverage is reported, not enforced; no threshold is configured.
