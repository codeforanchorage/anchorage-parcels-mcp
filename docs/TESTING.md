# Testing

## Unit tests

No network, no AWS. HTTP is mocked.

```bash
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest tests/ -q
python -m pytest tests/test_anchorage_parcels_plugin.py -v
python -m pytest tests/ --cov=plugins --cov-report=term-missing

python -m ruff check plugins/ tests/
python -m ruff format --check plugins/ tests/
```

CI runs these on every push and pull request, with ruff pinned to
0.15.1 (the formatter's output changes between releases). Coverage is
reported, not enforced.

Several tests are contracts, not examples: real tool output is
validated against each declared `outputSchema`, every structured caveat
must appear word for word in the text, and the timeout order is
asserted. If one fails after a change, decide whether the behaviour or
the test is wrong before touching either.

New behaviour gets its own `tests/test_<topic>.py`, importing the
helpers (`install_client`, `run_tool`, `assert_conforms`, ...) from
`tests.test_anchorage_parcels_plugin`.

## Against the live layer

```bash
python scripts/smoke_parcels.py
```

Runs the plugin in-process against the Municipality's ArcGIS service.
It skips (exit 0) when the service is unreachable.

## Against a running server

Start one locally (see [GETTING_STARTED.md](GETTING_STARTED.md)), or
use a deployed one:

```bash
python scripts/smoke_http.py http://localhost:8000/mcp
python scripts/smoke_http.py staging
python scripts/smoke_http.py              # prod
```

56 checks over HTTP: protocol negotiation in both eras, `tools/list`,
every tool, the error paths and CORS preflight. Run back to back it
can trip the upstream's per-IP quota; a failure that mentions a quota
or 429 is that, not a regression.

`scripts/smoke_prod.py` is a shorter run that keeps going after a
failure and prints one line per check.

### By hand

```bash
curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl -s -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"anchorage_parcels__find_parcel","arguments":{"parcel_id":"002-151-32"}}}'
```

`scripts/test_streamable_http.sh [url]` walks initialize, `tools/list`
and one tool call, printing each exchange. It needs `jq`.

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector
```

Choose the Streamable HTTP transport and enter the server's `/mcp`
URL.

### Claude

Claude Desktop and Claude Code can connect to
`http://localhost:8000/mcp`. claude.ai cannot reach localhost; use the
staging URL.
