# Security Model — Anchorage GIS MCP

> This document was inherited from the Anchorage GIS deployment and
> describes that server. The GIS plugin it refers to is no longer in
> this repository: it lives in `anchorage-gis-mcp`, on top of the
> `opencontext-arcgis-hub` package. What applies here is the network
> and request-shape section, the WHERE / `out_fields` / `order_by`
> validation (`arcgis_hub.where_validator`), and the fact that this
> server only ever calls the three layer URLs in its config.

This document describes the threat model and the defenses currently
enforced for the public deployment at
`https://anchorage-gis.codeforanchorage.org/mcp`. Future contributors
should read this before changing anything in `plugins/anchorage_gis/`,
`server/`, or `terraform/aws/`.

## Threat model

- **Endpoint:** publicly reachable, unauthenticated, read-only over
  HTTPS. Anyone can issue JSON-RPC calls. Browsers, native MCP clients,
  and arbitrary HTTP clients all reach the same Lambda.
- **Data:** the MCP only proxies to the Municipality of Anchorage's
  ArcGIS Online portal (`muniorg.maps.arcgis.com`) and on-prem GIS
  hosts (`*.muni.org`). No write paths exist; no secrets are stored.
- **Realistic risks:**
  1. Denial of wallet (Lambda invocations, upstream Esri API spam).
  2. Tenant-scope creep — using the MCP as an open proxy for arbitrary
     ArcGIS Online tenants.
  3. Prompt injection — attacker-controlled item descriptions reaching
     the calling LLM as context.
  4. Accidental PII exposure when a publisher misconfigures a layer.
  5. Sensitive values landing in CloudWatch logs in plaintext.

## Defenses in place

### Network and request shape

- **WAF per-IP rate limit** at 2000 req / 5 min, blocking mode. Prod
  uses the **shared fleet ACL** (`use_shared_waf = true` in
  `prod.tfvars`), which keeps this MCP's limit as its own counter
  aggregated on `(IP, Host)`. The effective limit lives in mcp-stats'
  `fleet_waf_members` under the key `anchorage-parcels`, not in this
  repo. It is sized for Claude.ai, whose traffic arrives from a small
  pool of shared Anthropic egress IPs.
- **API Gateway throttling** at 15 rps / 30 burst in prod, stage-wide via
  `aws_api_gateway_method_settings` reading `api_rate_limit` /
  `api_burst_limit` from `prod.tfvars`. The 3000 req/day usage-plan quota
  applies only to API-key requests (the disabled `/mcp-gcc` route), not
  to the keyless public `/mcp` route.
- **AWS managed rule sets** (`KnownBadInputsRuleSet`,
  `CommonRuleSet`) in block mode (`waf.tf`).
- **Lambda reserved concurrency** capped at 30 in prod to bound cost and blast
  radius if WAF is bypassed (`main.tf`, `prod.tfvars`).
- **HTTP method allowlist:** only `POST /mcp` and `OPTIONS /mcp`
  reach plugin code; `GET`/`DELETE` return 405 (`server/http_handler.py`).

### Cross-origin and session

- **CORS allowlist** is enforced in Lambda (not API Gateway) so the
  preflight check honors the same list as the actual request
  (`server/http_handler.py:_get_cors_headers`,
  `terraform/aws/api_gateway.tf` — OPTIONS uses AWS_PROXY, not MOCK).
- **`mcp-session-id` is a logging/tracing identifier**, not an auth
  token (`server/http_handler.py`). Do not extend authorization
  decisions to depend on it without adding a real signing/verification
  step.

### Outbound calls (the load-bearing checks)

These two checks together close the SSRF and tenant-scope-creep
surface, and must stay in sync:

- **Service-URL host allowlist**
  (`plugins/anchorage_gis/plugin.py::_validate_service_url`):
  - `*.muni.org` is accepted by suffix.
  - For `*.arcgis.com`, the URL must either match the configured portal
    host (`muniorg.maps.arcgis.com`) or carry the configured `org_id`
    (`Ce3DhLRthdwbHlfF`) as the first path segment. This rejects
    `services.arcgis.com/<other-org>/...`,
    `tiles.arcgis.com/<other-org>/...`, etc.
  - All other hosts are refused.
- **Item-ID ownership check**
  (`plugins/anchorage_gis/plugin.py::_assert_owned_by_configured_org`):
  - `get_dataset` rejects any item whose `orgId` does not match the
    configured org (case-insensitive). This is the choke point for
    every tool that resolves an item by ID, including
    `get_item_details`, `query_data`, `spatial_query_*`,
    `aggregate_by_polygon`, and `filter_by_polygon`.
  - **Fail-closed:** items missing `orgId` are also rejected.
  - `_search_org_layers` post-filters its results by `orgId` as
    defense-in-depth in case Esri ever returns matches that bypass the
    `orgid:` query clause.
  - `_search_gallery` is intentionally *not* org-filtered — the
    gallery is curator-scoped by group and may contain MOA-curated
    cross-org items in the listing. Any drill-down still goes through
    `get_dataset` and is rejected if non-MOA.

### Input validation

- **WHERE clauses** validated against a SQL-injection denylist
  (`arcgis_hub.where_validator`). Forbidden keywords include
  `INSERT/UPDATE/DELETE/DROP/UNION/EXEC/...`; forbidden substrings
  include `;`, `--`, `/*`, `0x`, `xp_`, `sp_`. Max length 2000 chars.
- **`out_fields` and `order_by`** validated for syntax via
  `OutFieldsValidator` and `OrderByValidator`.
- **Item IDs** must be 32-char hex (`_validate_item_id`).
- **Coordinates** validated to WGS84 ranges (`_validate_lonlat`).

### Logs

- **Sensitive headers and request-body keys redacted** by name match
  in `core/logging_utils.py` (`api_key`, `authorization`, `cookie`,
  `password`, `secret`, `token`, etc.).
- **CloudWatch retention** is 14 days (Lambda logs) and 30 days (API
  Gateway access logs) — `terraform/aws/main.tf`,
  `terraform/aws/access_logs.tf`.

### Configuration and secrets

- **No secrets in `config.yaml`.** Config is serialized into the
  `OPENCONTEXT_CONFIG` Lambda environment variable in plaintext
  (`terraform/aws/main.tf`). This is an **invariant**: any future
  plugin secret must go via AWS Secrets Manager or SSM Parameter Store
  with KMS, never the env var. CI should enforce this if a secret-
  bearing plugin is ever added.

## Architectural invariants

These are enforced by code today; do not relax them without explicit
discussion.

1. **One fork = one MCP server.** The single-plugin rule is enforced
   at config validation (`core/validators.py`) and at runtime
   (`PluginManager.load_plugins()`). Multiple enabled plugins is a
   hard error.
2. **No write paths.** No tool calls `applyEdits`, `/admin/`, or any
   non-`/query` Feature Service path. `WhereValidator` blocks DML
   keywords. If a plugin ever needs writes, treat it as a security-
   review-required change.
3. **Tenant scope = the configured org.** See `_validate_service_url`
   and `_assert_owned_by_configured_org`. These two checks together
   keep the MCP from being a free public proxy for other ArcGIS
   Online tenants.

## Deferred / known gaps

These are documented in the security review and not yet shipped.

| # | Issue | Status |
|---|---|---|
| 3 | No field-level PII denylist on returned records (e.g. emails, phones, applicant names) | open — proposal drafted |
| 4 | Response bodies dumped to CloudWatch by name-key redaction only — no value-pattern scrub for emails/phones | open |
| 5 | Access-log `sourceIp` is raw; not hashed | open |
| 6 | `/mcp` is fully unauthenticated; rate-limit bypass via rotating IPs is feasible | accepted given public-data scope |
| 7 | CORS allowlist includes `localhost:6274` (MCP Inspector) in prod | accepted; revisit when non-public tools land |
| 9 | Config in plaintext env var | accepted as invariant; see above |
| 10 | No request-body size cap at API Gateway (Lambda's 6 MB ceiling applies) | open |

## Verifying a change

Before merging anything that touches `plugins/anchorage_gis/`,
`server/`, `terraform/aws/`, or `core/`:

1. `pytest tests/test_anchorage_gis_plugin.py -q` — all must pass,
   including `TestValidateServiceUrl`, `TestItemOwnership`, and
   `TestSearchOrgLayersFilter`.
2. Smoke-test locally:
   ```bash
   PYTHONIOENCODING=utf-8 venv/Scripts/python scripts/local_server.py
   ```
   Then issue `query_data` against a known MOA item
   (e.g. `858fddc3012e4cd5b4e48d44dc84f4e0`). Records should return.
3. For changes to `_validate_service_url` or `_assert_owned_*`, also
   confirm a non-MOA item ID is rejected with a clear error.
4. Deploy with `./scripts/deploy.sh --environment prod`; this script
   plans first and requires explicit `yes` before applying.

## Reporting issues

Email `brendanbabb@gmail.com` for sensitive reports. Public issues
can go to <https://github.com/codeforanchorage/anchorage-gis-mcp/issues>.
