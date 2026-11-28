# Scripts

Run every script from the repository root.

| Script | What it does |
| ------ | ------------ |
| `deploy.sh` | Validates `config.yaml`, builds the Lambda package and applies Terraform. `bash ./scripts/deploy.sh --environment <staging\|prod> [--tfworkspace <name>] [--yes] [--allow-dirty]`. See [docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md). |
| `local_server.py` | Serves the MCP endpoint on `http://localhost:8000/mcp` through the same HTTP handler the Lambda uses. Needs `config.yaml` and `aiohttp` (in `requirements-dev.txt`). On Windows set `PYTHONUTF8=1`. |
| `smoke_http.py` | 56 checks against a running endpoint over HTTP. `python scripts/smoke_http.py [prod\|staging\|<url>]`; prod when no argument is given. |
| `smoke_prod.py` | A shorter end-to-end run that keeps going after a failure and prints one line per check. Prod by default; takes a URL. |
| `smoke_parcels.py` | Runs the plugin in-process against the live ArcGIS layer, with no server. |
| `test_streamable_http.sh` | Walks initialize, `tools/list` and one tool call with `curl`, printing each exchange. `./scripts/test_streamable_http.sh [url] [tool] [json-arguments]`. Needs `jq`. |
| `setup-backend.sh` | Writes a Terraform `backend.tf` for an S3 state bucket. Inherited from upstream OpenContext and not used by this deployment; a fork should read it before running it. |

The smoke scripts skip (exit 0) when the endpoint is unreachable, and
they read the live municipal layer, so results depend on it being up.
