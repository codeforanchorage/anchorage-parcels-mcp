# Anchorage Parcels MCP

[![CI](https://github.com/codeforanchorage/anchorage-parcels-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/codeforanchorage/anchorage-parcels-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A read-only [Model Context Protocol](https://modelcontextprotocol.io/)
server over the Municipality of Anchorage's public property and
assessment records: who owns a parcel, what it is assessed at, and what
is at an address. It reads the Municipality's **PropertyInformation**
ArcGIS layer (about 98,000 parcels, plus lease and economic-unit
records) live on every call. No login, no API key, no writes.

**Endpoint:** `https://anchorage-parcels.codeforanchorage.org/mcp`

This is a Code for Anchorage project. It is not run by, or an official
service of, the Municipality of Anchorage.

## Connect

**Claude (claude.ai or Claude Desktop)**

1. Open **Settings → Connectors** and choose **Add custom connector**.
2. Name it (for example "Anchorage Parcels") and enter the endpoint
   above.
3. Turn the connector on in a conversation.

**Claude Code**

```bash
claude mcp add --transport http anchorage-parcels https://anchorage-parcels.codeforanchorage.org/mcp
```

Any other MCP client that speaks Streamable HTTP can use the same URL.
For a client that only speaks stdio, see [`client/`](client/README.md)
(Go) or `stdio_bridge.py` (Python).

## What you can ask

- "Who owns 632 W 6th Ave, and what is it assessed at?"
- "Show me parcel 002-151-32."
- "Which parcels does the Municipality of Anchorage own in the B2C
  zoning district?"
- "What is the median assessed value by zoning district?"
- "Which ten owners hold the most parcels?"
- "What parcel is at 61.2176, -149.8997?"

## Tools

| Tool | Use it for |
| ---- | ---------- |
| `find_parcel` | A parcel by number, in any of the four formats the Municipality uses (`002-151-32`, `00215132`, `00215132000`, `002-151-32-000`). |
| `get_parcel_details` | The full record for one parcel: owner and mailing address, three years of valuation, exemptions, deed and plat, and a link to the assessor's own page. |
| `search_by_address` | Parcels at a street address or on a street. |
| `search_by_owner` | Parcels by owner name. |
| `parcels_at_point` | Every property record at a latitude and longitude. |
| `query_parcels` | Anything else: a SQL `WHERE` clause over the layer's fields, with paging. |
| `parcel_stats` | Counts, sums, averages, medians and rankings, optionally grouped by a field. |

Every tool is read-only. Arguments, defaults and behaviour are in
[docs/PARCELS.md](docs/PARCELS.md).

## What to know about the data

- **It is the assessor's record, not a title search.** Values are the
  Municipality's assessment figures. For anything that matters, follow
  the Datalet link `get_parcel_details` returns to the assessor's page.
- **Owner names are as recorded.** One owner can appear under several
  spellings, so a count "by owner" counts spellings. `search_by_owner`
  and `parcel_stats` say so when it applies.
- **Records name people.** Owner names and mailing addresses are public
  record and the Municipality publishes them; treat them with the care
  personal data deserves. See [docs/SECURITY.md](docs/SECURITY.md).
- **About 1,000 records are outlines only**, with no assessment data
  joined. They are left out unless `include_unassessed=true` is passed.
- **Zoning codes are not normalized.** `B2C` and `B-2C` are the same
  district under two spellings; the tools match both and say so.
- **Read the caveats.** Every response carries a `caveats` list
  (truncated results, fuzzy matches, spelling variants) in the text and
  in the structured output. They are part of the answer.

This server covers the assessment records only. For other municipal
layers (zoning polygons, flood zones, trails, parks) or spatial
analysis, use the
[Anchorage GIS MCP](https://github.com/codeforanchorage/anchorage-gis-mcp).

## Develop

```bash
pip install -r requirements.txt -r requirements-dev.txt

python -m pytest tests/ -q                 # unit tests, no network
python -m ruff check plugins/ tests/

cp config-anchorage-parcels.yaml config.yaml
python scripts/local_server.py             # http://localhost:8000/mcp
python scripts/smoke_http.py http://localhost:8000/mcp
```

On Windows set `PYTHONUTF8=1` before starting the local server.

| Doc | What is in it |
| --- | ------------- |
| [docs/PARCELS.md](docs/PARCELS.md) | Every tool and argument, configuration, the schema drift check, forking for another city |
| [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) | Connecting a client, running locally, deploying your own |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How a request travels and where the code lives |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | AWS Lambda, API Gateway and Terraform |
| [docs/TESTING.md](docs/TESTING.md) | Unit tests, smoke tests, MCP Inspector |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model and what the endpoint does and does not protect |
| [CLAUDE.md](CLAUDE.md) | Conventions and invariants for working in this repository |

Issues and pull requests are welcome at
<https://github.com/codeforanchorage/anchorage-parcels-mcp/issues>.

## Credits and license

Built on [OpenContext](https://github.com/thealphacubicle/OpenContext),
an MCP framework for civic data by Srihari Raman at the City of Boston
Department of Innovation and Technology. The server core is the
[`opencontext-core`](https://github.com/codeforanchorage/opencontext-core)
package; the parcels plugin and this deployment are maintained by Code
for Anchorage.

MIT. See [LICENSE](LICENSE).
