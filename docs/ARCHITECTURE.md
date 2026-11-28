# Architecture

## One fork, one server

A deployment runs exactly one plugin. This repository's is
`anchorage_parcels`. The rule is checked when the config is loaded and
again when plugins are loaded, so a config that enables two plugins,
or none, does not start. A second data source is a second repository
and a second deployment.

## What is in this repository, and what is not

| Piece | Where it lives |
| ----- | -------------- |
| The seven tools, and everything they know about the parcel layer | `plugins/anchorage_parcels/plugin.py` |
| Parcel-number and coverage rules shared with the Anchorage GIS server | `plugins/anchorage_parcels/moa.py` |
| Plugin settings and their defaults | `plugins/anchorage_parcels/config_schema.py` |
| The vendored layer schema the drift check compares against | `plugins/anchorage_parcels/schema/propertyinformation.json` |
| MCP protocol, JSON-RPC, HTTP handling, the Lambda adapter (`core/`, `server/`) | The [`opencontext-core`](https://github.com/codeforanchorage/opencontext-core) package, pinned in `requirements.txt` and `pyproject.toml` |
| WHERE, `order_by` and `out_fields` validation | The [`opencontext-arcgis-hub`](https://github.com/codeforanchorage/opencontext-arcgis-hub) package, pinned the same way |
| Lambda, API Gateway, WAF association, alarms | `terraform/aws/` |

There is no `core/` or `server/` directory here, and there must not be
one: a local copy would shadow the installed package. To change
protocol or transport behaviour, change it in `opencontext-core`, tag a
release, and move the pin in both files.

## Request flow

```
MCP client
  → HTTPS POST /mcp
  → API Gateway (throttling) and WAF (per-IP rate limit)
  → Lambda: server.adapters.aws_lambda.lambda_handler
  → server/http_handler.py   Origin allowlist, protocol-version checks
  → core/mcp_server.py       JSON-RPC: initialize, tools/list, tools/call, ...
  → core/plugin_manager.py   routes anchorage_parcels__<tool> to the plugin
  → plugins/anchorage_parcels/plugin.py
  → the Municipality's ArcGIS Feature Service
```

`scripts/local_server.py` enters the same chain at `http_handler`, so
the Origin and protocol-version checks run locally as well. A client
that only speaks stdio reaches the HTTP endpoint through the Go client
in `client/` or through `stdio_bridge.py`.

Tool names are prefixed by the plugin manager: the plugin defines
`find_parcel`, and clients see `anchorage_parcels__find_parcel`.

## Things that shape the plugin

- **Nothing is cached between calls except layer facts.** Every tool
  call queries the live layer. The plugin object lives as long as its
  warm Lambda container, so the few things kept on it (the field list,
  date fields, coverage, zoning spellings) are re-read after an hour.
- **One time budget per call.** A tool call has 20 seconds for every
  upstream request and retry it makes. That sits under the Lambda's 28
  seconds, which sits under API Gateway's fixed 29, so a slow upstream
  produces a readable tool error and not a dropped connection.
- **Output schemas are binding.** Each tool declares an `outputSchema`,
  and every return path emits conforming structured content, including
  the empty, not-found and truncated ones.
- **Caveats are written once.** The warnings in the text and the
  `caveats` array in the structured output come from one list, so they
  cannot disagree.
- **Caller mistakes are not server errors.** A bad argument raises
  `ToolInputError`, which reaches the caller as a tool error and is
  logged without a traceback.

The tests enforce each of these. [`CLAUDE.md`](../CLAUDE.md) has the
full list of invariants, and [SECURITY.md](SECURITY.md) covers the
public endpoint.

## Configuration

`config.yaml` in the repository root is read at startup; it is
gitignored, and `config-anchorage-parcels.yaml` is its tracked source.
On Lambda the file ships inside the deployment package. It does not
support `${ENV_VAR}` substitution. See [PARCELS.md](PARCELS.md) for the
plugin's settings and [DEPLOYMENT.md](DEPLOYMENT.md) for the `aws:`
block.
