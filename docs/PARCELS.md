# Anchorage Parcels MCP (`anchorage_parcels` plugin)

Read-only MCP server over the Municipality of Anchorage
property/assessment records — the public MOA **PropertyInformation**
Feature Layer (~98k parcels plus Lease and Economic-unit overlays,
republished frequently by the assessor), with the **Addresses** and
**Subdivisions** layers as supporting data. No auth, no writes.

The layer schema is baked into the tools, so a model answers "who owns
144 W 15th Ave and what's it assessed at?" in one call — no schema
pre-flight. For **any other MOA layer** (zoning polygons, trails,
flood zones) or spatial analysis, use the **Anchorage GIS MCP**
server; the tool descriptions and server instructions route models
there automatically.

## Tools

All tools are read-only (`readOnlyHint`) and return provenance headers
(source URL, query, `Retrieved:` timestamp). Field names are
case-sensitive everywhere.

| Tool | What it does |
| ---- | ------------ |
| `find_parcel(parcel_id, category, out_fields, limit)` | Looks a parcel up by number in **any** of the four MOA formats (`002-151-32`, `00215132`, `00215132000`, `002-151-32-000`), exact-matching across all five stored ID columns; falls back to clearly-labeled fuzzy candidates on a miss. |
| `get_parcel_details(parcel_id)` | Full record in sections: identity/legal, situs, owner mailing address, valuation (current + 2 prior years), exemptions, deed/plat, misc — ending with the assessor **Datalet** deep link. Lists condo units and asks which one when the ID matches several records. |
| `search_by_owner(name, limit, category)` | Substring owner search (input uppercased automatically), ordered by appraised value. Public-record assessment data. |
| `search_by_address(address, limit)` | Situs-address search; on zero hits it resolves the address via the municipal address-point layer and runs point-in-polygon, reporting which path was used. |
| `parcels_at_point(lat, lon)` | Every property polygon containing a WGS84 point, across **all** categories (Parcel/Lease/Economic polygons can stack); each hit labeled. |
| `query_parcels(where, out_fields, category, limit, offset, order_by)` | Escape hatch: validated SQL WHERE with pagination (`resultOffset`), a TOTAL COUNT line, and next-offset hints. |
| `parcel_stats(stat_type, stat_field, group_by, where, category, percentile)` | `count/sum/avg/min/max/stddev/var/percentile_cont` with optional grouping. Median = `percentile_cont` with `percentile=0.5` (e.g. median `Appraised_Total_Value` by `Zoning_District`). |

Data notes baked into the tools: owner names/addresses are stored
UPPERCASE; records carry a `GIS_Category` of `Parcel`, `Lease`, or
`Economic` (most tools default to `Parcel`; pass `category='All'` to
widen); `Lot_Size` is sq ft and `CAMA_Acreage` acres (prefer these
over Web-Mercator `Shape__Area`).

## Configuration

`config-anchorage-parcels.yaml` is the reference deployment config:
it enables only this plugin, carries the model-facing `instructions:`
block, and names its own Lambda
(`anchorage-parcels-mcp-staging`, us-west-2, 512 MB / 60 s).

```yaml
plugins:
  anchorage_parcels:
    enabled: true
    property_layer_url: "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/PropertyInformation_Hosted/FeatureServer/0"
    addresses_layer_url: "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/Addresses_Hosted/FeatureServer/0"
    subdivisions_layer_url: "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/Subdivisions_Hosted/FeatureServer/0"
    city_name: "Municipality of Anchorage"
    field_map: {}   # logical->physical overrides; empty for MOA
    timeout: 30
```

### Schema drift check

The live layer schema is vendored at
`plugins/anchorage_parcels/schema/propertyinformation.json`. On every
cold start the plugin diffs live field names against this snapshot and
logs a loud structured warning (`SCHEMA DRIFT DETECTED`, with
`missing_fields` / `added_fields`) — but keeps serving (degraded >
down). To refresh the snapshot:

```powershell
curl.exe -s "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/PropertyInformation_Hosted/FeatureServer/0?f=json" -o plugins/anchorage_parcels/schema/propertyinformation.json
```

## Run and test locally (Windows / PowerShell)

```powershell
# Serve the parcels config on http://localhost:8000/mcp
$env:OPENCONTEXT_CONFIG = "config-anchorage-parcels.yaml"
$env:PYTHONUTF8 = "1"   # local_server.py prints emoji; avoids cp1252 errors
python scripts/local_server.py

# Unit tests (mocked HTTP)
python -m pytest tests/test_anchorage_parcels_plugin.py -v

# Live smoke test against the real MOA layer (network; skips offline)
python scripts/smoke_parcels.py

# Full MCP lifecycle over streamable HTTP (Git Bash; requires jq)
./scripts/test_streamable_http.sh http://localhost:8000/mcp \
  anchorage_parcels__find_parcel '{"parcel_id": "002-151-32"}'
```

On macOS/Linux use `export OPENCONTEXT_CONFIG=config-anchorage-parcels.yaml`
instead of the `$env:` lines.

## Fork this for your city

The plugin is city-agnostic; the MOA specifics live in exactly two
places:

1. **Layer URLs** — point `property_layer_url` (and optionally the
   addresses/subdivisions URLs) at your assessor's ArcGIS Feature
   Layer in your config.
2. **Field map** — `DEFAULT_FIELD_MAP` in
   `plugins/anchorage_parcels/plugin.py` maps logical names
   (`owner_name`, `appraised_total`, …) to the MOA column names. Either
   edit that one block in your fork, or override individual keys via
   the `field_map:` config option (e.g. `owner_name: "OWNER"`).

Then re-vendor the schema snapshot (command above, with your layer
URL) so the drift check guards *your* schema. If your layer has no
`GIS_Category`-style discriminator, have clients pass
`category='All'` (or edit `CATEGORY_VALUES` in the same file). The
parcel-ID normalizer assumes the MOA 8+3-digit format; cities with a
different parcel-number shape should adapt
`_normalize_parcel_variants` (shared with the `anchorage_gis` plugin).

## Deploying to AWS Lambda (steps left for a human)

`scripts/deploy.sh` packages whatever is in the repo-root
`config.yaml`, and this repo's Terraform state currently belongs to
the **Anchorage GIS** deployment. Before deploying the parcels server
you must:

1. **Copy the config into place** (in the deployment fork/checkout):
   `Copy-Item config-anchorage-parcels.yaml config.yaml`
2. **Give Terraform its own state/workspace** — do *not* `terraform
   apply` against the GIS deployment's state or it will rename/replace
   that Lambda. Either fork the repo (the intended one-fork-one-server
   model) or create a separate workspace/backend key under
   `terraform/aws` (see `scripts/setup-backend.sh`), and set the
   tfvars this config implies: `lambda_name =
   "anchorage-parcels-mcp-staging"`, `region = "us-west-2"`,
   `config_file = "config.yaml"`.
3. **Decide the domain** (e.g. `anchorage-parcels.codeforanchorage.org`)
   and its DNS/certificate before wiring API Gateway to it.
4. Then: `./scripts/deploy.sh --environment staging`

Nothing under `terraform/` needed changes for this plugin; only the
state/workspace and domain decisions block a deploy.

Because it is an ordinary OpenContext plugin, `anchorage_parcels` can
also be enabled *inside* the Anchorage GIS deployment later by config
toggle (still one plugin per deployment — swap which one is enabled).
