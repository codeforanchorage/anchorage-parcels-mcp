# Security Model — Anchorage Parcels MCP

This document describes the threat model and the defenses in place for
the public deployment at
`https://anchorage-parcels.codeforanchorage.org/mcp`. Read it before
changing `plugins/anchorage_parcels/`, `terraform/aws/`, or the pinned
versions of `opencontext-core` and `opencontext-arcgis-hub`.

The request handling (`core/`, `server/`) is the `opencontext-core`
package and the WHERE / `out_fields` / `order_by` validators are
`opencontext-arcgis-hub`; neither is code in this repository. A change
to their behaviour is made there and arrives here by moving a pin.

## Threat model

- **Endpoint:** publicly reachable, unauthenticated, read-only over
  HTTPS. Anyone can issue JSON-RPC calls. Browsers, native MCP clients
  and arbitrary HTTP clients all reach the same Lambda.
- **Data:** the server reads three ArcGIS Feature Layers published by
  the Municipality of Anchorage: PropertyInformation, Addresses and
  Subdivisions. They are public without a login. No write paths exist
  and no secrets are stored.
- **The records name people.** Assessment records carry the owner's
  name and mailing address. They are public record and the Municipality
  publishes them, but they are personal data all the same: this server
  makes them easier to search by name (`search_by_owner`) than the
  source does, and that is a deliberate feature, not an accident.
- **Realistic risks:**
  1. Denial of wallet: Lambda invocations, and load on the upstream
     Feature Service.
  2. Bulk harvesting of owner names and mailing addresses.
  3. Owner names typed by callers, and the records returned, landing
     in CloudWatch logs.
  4. A crafted WHERE clause reaching the Feature Service.
  5. Text from the layer (owner names, legal descriptions) reaching the
     calling model as if it were instructions.

## Defenses in place

### Network and request shape

- **WAF per-IP rate limit** of 2000 requests per 5 minutes, blocking.
  Prod uses the shared fleet ACL (`use_shared_waf = true` in
  `prod.tfvars`), which counts this server separately on `(IP, Host)`.
  The effective limit is in the mcp-stats repo, `fleet_waf_members`
  under `anchorage-parcels`, not here; `waf_rate_limit_per_5min` in this
  repo is only the value a dedicated ACL would use. It is sized for
  Claude.ai, whose traffic arrives from a small pool of shared Anthropic
  egress IPs.
- **AWS managed rule sets** on that ACL (`KnownBadInputsRuleSet`,
  `CommonRuleSet`) in block mode. `CommonRuleSet` refuses a request body
  over 8 KB; this server is not one of the members exempted from that.
- **API Gateway throttling** of 15 requests per second, burst 30, in
  prod: stage-wide through `aws_api_gateway_method_settings`, reading
  `api_rate_limit` / `api_burst_limit` from `prod.tfvars`. This is the
  only throttle on the keyless `/mcp` route. The 3000 requests/day
  usage-plan quota applies only to API-key requests (the disabled
  `/mcp-gcc` route).
- **Lambda reserved concurrency** capped at 30 in prod, which bounds
  cost and load on the upstream if the WAF is bypassed.
- **Time limits:** a tool call has 20 seconds in total for every
  upstream request and retry it makes, the Lambda 28, and API Gateway
  29. See "Timeout ladder" in `CLAUDE.md`.
- **HTTP methods:** only `POST /mcp` and `OPTIONS /mcp` reach the
  plugin. `GET` and `DELETE` return 405; other methods are refused by
  API Gateway.

### Cross-origin and session

- **The Origin allowlist is enforced**, not only reflected in CORS
  headers: a request with an Origin that is not on the list gets HTTP
  403 before any routing or plugin code runs. A request with no Origin
  is allowed, because DNS rebinding is a browser attack and native
  clients send none. The list is the core's default (claude.ai,
  claude.com, console.anthropic.com, and MCP Inspector on localhost
  port 6274); `server_allowed_origins` in the config would replace it.
- **`Mcp-Session-Id` is a tracing identifier**, not a credential. No
  authorization decision depends on it, and none should without a real
  signing step. Requests in the 2026-07-28 protocol era carry no
  session at all.

### Outbound calls

- **The server calls only the three layer URLs in its config**
  (`property_layer_url`, `addresses_layer_url`,
  `subdivisions_layer_url`). No tool takes a URL, a host, an item ID or
  a layer name from the caller, so there is no request a caller can
  steer to another host. This is what makes the tenant and SSRF checks
  of the general GIS servers unnecessary here; it stays true only as
  long as no tool accepts a location to fetch from.
- The config schema requires each URL to be http(s) with a host and
  refuses unknown keys. It does not restrict the host: whoever can
  change the deployed config can point the server anywhere, which is
  the same trust as being able to deploy it.
- **Read-only by construction:** every request is a GET to a layer's
  metadata or its `/query` endpoint. Nothing calls `applyEdits`, an
  admin path, or any other write.

### Input validation

- **WHERE clauses** (`query_parcels`, `parcel_stats`) pass
  `arcgis_hub.where_validator.WhereValidator`: at most 2000 characters;
  DML and DDL keywords, `UNION`, `EXEC` and the like refused; `;`,
  `--`, `/*` refused; quoted strings masked before the scan so a value
  cannot hide a keyword. Every identifier must then be a field the
  live layer has.
- **`out_fields` and `order_by`** are checked for syntax by
  `OutFieldsValidator` and `OrderByValidator`; `stat_field` and
  `group_by` must be single identifiers that exist on the layer.
- **Text the caller supplies as a value** (owner name, address) is
  never spliced in raw: quotes are doubled, and `%`, `_` and `\` are
  escaped so they match literally instead of acting as wildcards.
- **Parcel numbers** are reduced to digits by a strict parser before
  they are used; anything that is not one parcel number is refused.
- **Coordinates** must be finite and within WGS84 ranges; limits and
  offsets must be integers and are clamped.
- A caller's mistake is a `ToolInputError`: it is returned as a tool
  error and logged at WARNING with no traceback.

### Output

- Everything a tool returns comes from the layer or from fixed text in
  the plugin. Field values (owner names, legal descriptions, addresses)
  are written by the assessor's office, not by the public, which makes
  injected instructions unlikely but not impossible; a client should
  treat tool output as data.
- Each tool declares an `outputSchema`, and tests validate real output
  against it.

### Logs

- **Bodies are not logged.** The HTTP log lines carry sizes
  (`request_bytes`, `response_bytes`). The JSON-RPC lines carry the
  method, the tool name and its arguments, and the result; a value over
  8 KB is logged as a summary (`logging.max_value_bytes`, core 1.5.0).
- **Keys that look like credentials are redacted** by name
  (`api_key`, `authorization`, `token`, `password`, `secret`, `cookie`
  and similar) in `core/logging_utils.py`. Nothing redacts by value.
- **Retention** is 14 days for Lambda logs and 30 days for API Gateway
  access logs (`terraform/aws/main.tf`, `terraform/aws/access_logs.tf`).

### Configuration and secrets

- **There are no secrets.** The layers are public and the server holds
  no credential for them.
- The config ships inside the Lambda package and is read from
  `$LAMBDA_TASK_ROOT/config.yaml`; Terraform sets `OPENCONTEXT_CONFIG`
  to the empty string. Anyone with read access to the function can read
  it, so **no secret may ever be put in `config.yaml`**. A future plugin
  secret belongs in Secrets Manager or SSM Parameter Store, read from
  the environment in `initialize()`.

## Invariants

Enforced by code or tests today. Do not relax one without discussion.

1. **One fork, one MCP server.** Exactly one plugin is enabled; the
   core refuses anything else at config validation and at startup.
2. **No write paths.** If a tool ever needs to write, that is a
   security-review change.
3. **No caller-supplied fetch target.** See "Outbound calls".
4. **The Origin allowlist is enforced with 403.**
5. **No secrets in `config.yaml`.**

## Known gaps

| Issue | Status |
|---|---|
| `/mcp` is unauthenticated; the per-IP rate limit can be spread across addresses | accepted, given public data |
| Owner names and mailing addresses can be searched and paged through in bulk (up to 1,000 records per call) | accepted: the same records are public at the source; the rate limits and the concurrency cap bound the speed |
| Tool arguments and results are logged, so an owner name a caller searched for, and records up to 8 KB, sit in CloudWatch for 14 days | open |
| Access-log `sourceIp` is stored raw, not hashed | open |
| The Origin allowlist includes MCP Inspector on localhost in prod | accepted; revisit if a non-public tool is added |
| Layer URLs in the config are not restricted to a host | accepted; equivalent to deploy access |

## Verifying a change

Before merging anything that touches the plugin, `terraform/aws/`, or
either pin:

1. `python -m pytest tests/ -q`, `python -m ruff check plugins/ tests/`
   and `python -m ruff format --check plugins/ tests/` all pass.
2. Run the local server (`python scripts/local_server.py`, which goes
   through the same HTTP handler as the Lambda) and the HTTP suite
   against it: `python scripts/smoke_http.py http://localhost:8000/mcp`.
   It covers the Origin 403, the protocol checks and every tool.
3. For a change to WHERE handling or to how caller text is quoted, add
   a test with the hostile input, and try it against the live layer.
4. Deploy to staging, run `python scripts/smoke_http.py staging`, then
   prod. `scripts/deploy.sh` plans first and asks before applying unless
   `--yes` is passed.

## Reporting issues

Email `brendanbabb@gmail.com` for sensitive reports. Public issues can
go to <https://github.com/codeforanchorage/anchorage-parcels-mcp/issues>.
