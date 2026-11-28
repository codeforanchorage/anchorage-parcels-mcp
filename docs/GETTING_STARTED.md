# Getting Started

Three things you might want to do, shortest first.

## 1. Use the public server

Nothing to install. Point an MCP client at

```
https://anchorage-parcels.codeforanchorage.org/mcp
```

- **Claude (claude.ai or Claude Desktop):** Settings → Connectors →
  Add custom connector, enter a name and the URL, then turn the
  connector on in a conversation.
- **Claude Code:**
  `claude mcp add --transport http anchorage-parcels https://anchorage-parcels.codeforanchorage.org/mcp`

Then ask something like "who owns 632 W 6th Ave?". The tools are listed
in [PARCELS.md](PARCELS.md).

## 2. Run it locally

Requires Python 3.11 or later. The Lambda runs 3.14, and so does CI.

```bash
pip install -r requirements.txt -r requirements-dev.txt

# config.yaml is gitignored; its tracked source is config-anchorage-parcels.yaml
cp config-anchorage-parcels.yaml config.yaml

python scripts/local_server.py     # serves http://localhost:8000/mcp
```

On Windows use `python` (not `python3`) and set `PYTHONUTF8=1` first:
the startup banner prints emoji.

Check it in another terminal:

```bash
python scripts/smoke_http.py http://localhost:8000/mcp
```

The local server goes through the same HTTP handler as the Lambda, and
it reads the live municipal layer, so it needs network access. Claude
Desktop and Claude Code can connect to `http://localhost:8000/mcp`;
claude.ai cannot reach localhost.

If the server refuses to start, the config did not validate. Exactly
one plugin must be enabled, and `config.yaml` must exist in the
repository root.

## 3. Deploy your own

The server runs on AWS Lambda behind API Gateway, deployed with
Terraform by one script:

```bash
bash ./scripts/deploy.sh --environment staging --yes
python scripts/smoke_http.py staging
```

Read [DEPLOYMENT.md](DEPLOYMENT.md) first. A fork must set its own
Lambda name and Terraform state before the first deploy, or it will
try to replace this deployment's. To point the server at another
city's parcel layer, see "Fork this for your city" in
[PARCELS.md](PARCELS.md).

## Where next

- [PARCELS.md](PARCELS.md): tools, configuration, data notes
- [ARCHITECTURE.md](ARCHITECTURE.md): how a request travels
- [TESTING.md](TESTING.md): unit and smoke tests
- [SECURITY.md](SECURITY.md): what the public endpoint protects
