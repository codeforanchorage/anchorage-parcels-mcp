"""Live smoke test against a DEPLOYED MCP endpoint over HTTP.

The HTTP-level counterpart to smoke_parcels.py: where that script
exercises the plugin in-process against the ArcGIS layer, this one
exercises the full deployed stack (API Gateway -> Lambda ->
http_handler -> mcp_server -> plugin -> ArcGIS) via MCP JSON-RPC,
including protocol-version negotiation, tools/list, every tool, the
error paths, and CORS preflight.

Network required -- the script SKIPs (exit 0) when the endpoint is
unreachable, so it is safe in offline CI runs.

Upstream rate limits: every Lambda invocation egresses from one IP, so
running this repeatedly in quick succession can trip the ArcGIS
per-IP quota. A failure whose text mentions a quota/429 is flagged as
an upstream rate limit rather than a server regression -- re-run after
a pause before concluding a deploy broke something.

Run:  python scripts/smoke_http.py            # prod (custom domain)
      python scripts/smoke_http.py staging    # staging stack
      python scripts/smoke_http.py <url>      # any /mcp endpoint
"""

import json
import re
import sys

import httpx

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - optional for offline runs
    Draft202012Validator = None

# An upstream quota rejection looks like a tool failure but is not a
# regression in this server.
_QUOTA_RE = re.compile(r"quota exceeded|rate limit|\b429\b", re.IGNORECASE)

# 'staging' tracks the current staging stack and must be updated if that
# stack is ever recreated (terraform output api_gateway_url is
# authoritative). The prod custom domain is stable.
URLS = {
    "prod": "https://anchorage-parcels.codeforanchorage.org/mcp",
    "staging": "https://9jlxcze2wc.execute-api.us-west-2.amazonaws.com/staging/mcp",
}

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        hint = ""
        if _QUOTA_RE.search(detail or ""):
            hint = (
                " -- this looks like an UPSTREAM RATE LIMIT (all Lambda "
                "traffic egresses from one IP), not necessarily a server "
                "regression; pause and re-run before investigating"
            )
        raise AssertionError(f"{label}: {detail}{hint}")
    PASSED += 1


def rpc(client: httpx.Client, url: str, method: str, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(url, json=body, headers={"Accept": "application/json"})


def call_tool(client: httpx.Client, url: str, name: str, args: dict):
    data = rpc(client, url, "tools/call", {"name": name, "arguments": args}).json()
    return data.get("result", {}), data


def tool_text(result: dict) -> str:
    content = result.get("content") or []
    return content[0].get("text", "") if content else ""


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "prod"
    url = URLS.get(target, target)
    if not url.startswith("http"):
        print(f"Unknown target {target!r}. Use 'prod', 'staging', or a URL.")
        return 1
    print(f"Target: {url}\n")

    with httpx.Client(timeout=60) as client:
        # 1. initialize / protocol-version negotiation.
        try:
            r = rpc(
                client,
                url,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "smoke_http", "version": "0"},
                },
            )
        except httpx.HTTPError as e:
            print(f"SKIP: endpoint unreachable ({e}); network required.")
            return 0
        res = r.json()["result"]
        check("initialize: HTTP 200", r.status_code == 200, str(r.status_code))
        check(
            "initialize: echoes 2025-11-25",
            res.get("protocolVersion") == "2025-11-25",
            str(res.get("protocolVersion")),
        )
        check(
            "initialize: serverInfo present",
            "serverInfo" in res,
            json.dumps(res.get("serverInfo", {})),
        )
        check("initialize: instructions present", bool(res.get("instructions")))
        check(
            "initialize: session header",
            "mcp-session-id" in {k.lower() for k in r.headers},
        )

        r = rpc(client, url, "initialize", {"protocolVersion": "2026-07-28"})
        check(
            "initialize: unknown version negotiates to newest supported",
            r.json()["result"]["protocolVersion"] == "2025-11-25",
        )

        # 2. ping -- the result MUST be an empty object; the liveness
        #    signal is the response itself, not its body.
        r = rpc(client, url, "ping")
        check(
            "ping: result is {}",
            r.json().get("result") == {},
            json.dumps(r.json().get("result")),
        )

        # 3. tools/list: the 7 prefixed tools, all read-only annotated.
        tools = rpc(client, url, "tools/list").json()["result"]["tools"]
        names = {t["name"] for t in tools}
        expected = {
            f"anchorage_parcels__{n}"
            for n in (
                "find_parcel",
                "get_parcel_details",
                "search_by_owner",
                "search_by_address",
                "parcels_at_point",
                "query_parcels",
                "parcel_stats",
            )
        }
        check("tools/list: all 7 tools", names == expected, f"got {sorted(names)}")
        check(
            "tools/list: readOnlyHint on every tool",
            all(t.get("annotations", {}).get("readOnlyHint") for t in tools),
        )
        check(
            "tools/list: top-level title on every tool",
            all(t.get("title") for t in tools),
            str([t["name"] for t in tools if not t.get("title")]),
        )
        check(
            "tools/list: idempotentHint not set on read-only tools",
            not any("idempotentHint" in t.get("annotations", {}) for t in tools),
        )
        check(
            "tools/list: outputSchema on every tool",
            all(t.get("outputSchema") for t in tools),
            str([t["name"] for t in tools if not t.get("outputSchema")]),
        )
        second = rpc(client, url, "tools/list").json()["result"]["tools"]
        check(
            "tools/list: ordering is deterministic",
            [t["name"] for t in second] == [t["name"] for t in tools],
        )
        schemas = {t["name"]: t.get("outputSchema") for t in tools}

        # 4. find_parcel: known fixture parcel, hyphenated 8-digit form.
        result, _ = call_tool(
            client, url, "anchorage_parcels__find_parcel", {"parcel_id": "002-151-32"}
        )
        text = tool_text(result)
        check(
            "find_parcel: fixture record",
            "00215132000" in text and "144 W 15TH AVE" in text,
        )

        # 5. get_parcel_details: single record renders sections + Datalet.
        result, _ = call_tool(
            client,
            url,
            "anchorage_parcels__get_parcel_details",
            {"parcel_id": "002-151-32"},
        )
        text = tool_text(result)
        check("get_parcel_details: valuation section", "## Valuation" in text)
        check("get_parcel_details: Datalet link", "Datalet" in text)

        # 6. get_parcel_details: condo root lists EVERY unit (regression
        #    guard for the former 12-row cap).
        result, _ = call_tool(
            client,
            url,
            "anchorage_parcels__get_parcel_details",
            {"parcel_id": "012-182-04"},
        )
        text = tool_text(result)
        check(
            "get_parcel_details condo root: all 24 units, no truncation",
            "matches 24 records" in text
            and "01218204024" in text
            and "TRUNCATED" not in text,
            text.splitlines()[0] if text else "(empty)",
        )

        # 7-9. The search tools return rows.
        result, _ = call_tool(
            client,
            url,
            "anchorage_parcels__search_by_address",
            {"address": "632 W 6TH AVE", "limit": 5},
        )
        check("search_by_address: rows returned", "Record 1:" in tool_text(result))

        result, _ = call_tool(
            client,
            url,
            "anchorage_parcels__search_by_owner",
            {"name": "municipality of anchorage", "limit": 3},
        )
        check("search_by_owner: rows returned", "Record 1:" in tool_text(result))

        result, _ = call_tool(
            client,
            url,
            "anchorage_parcels__parcels_at_point",
            {"lat": 61.2163, "lon": -149.8949},
        )
        check("parcels_at_point: downtown hit", "Parcel_ID" in tool_text(result))

        # 10. parcel_stats: full-layer count is ~98k parcels.
        result, _ = call_tool(
            client,
            url,
            "anchorage_parcels__parcel_stats",
            {"stat_type": "count", "stat_field": "Parcel_ID"},
        )
        text = tool_text(result)
        count = 0
        for line in text.splitlines():
            digits = line.strip().lstrip("- ").replace(",", "")
            if digits.isdigit():
                count = int(digits)
        check("parcel_stats: Parcel count > 90,000", count > 90_000, f"{count:,}")

        # 11. query_parcels: WHERE plus pagination metadata.
        result, _ = call_tool(
            client,
            url,
            "anchorage_parcels__query_parcels",
            {"where": "Zoning_District = 'RO'", "limit": 2},
        )
        check("query_parcels: TOTAL COUNT line", "TOTAL COUNT" in tool_text(result))

        # 12-14. Error paths.
        _, data = call_tool(client, url, "anchorage_parcels__nope", {})
        err = data.get("error", {})
        check(
            "unknown tool: -32602 (not -32603)",
            err.get("code") == -32602,
            json.dumps(err)[:200],
        )
        check(
            "unknown tool: message names the tool",
            err.get("message") == "Unknown tool: anchorage_parcels__nope",
            str(err.get("message")),
        )
        check(
            "unknown tool: data lists the available tools",
            bool((err.get("data") or {}).get("available_tools")),
        )

        data = rpc(client, url, "no/such/method").json()
        check(
            "unknown method: -32601 Method not found",
            data.get("error", {}).get("code") == -32601,
            json.dumps(data.get("error", {}))[:200],
        )

        data = rpc(
            client,
            url,
            "tools/call",
            {"name": "anchorage_parcels__find_parcel", "arguments": "not-an-object"},
        ).json()
        check(
            "non-object arguments: -32602, not a raw Python error",
            data.get("error", {}).get("code") == -32602,
            json.dumps(data.get("error", {}))[:200],
        )

        result, _ = call_tool(client, url, "anchorage_parcels__find_parcel", {})
        check(
            "missing required arg: isError names the field",
            bool(result.get("isError")) and "parcel_id" in tool_text(result),
        )

        r = client.post(
            url, content="{not json", headers={"Content-Type": "application/json"}
        )
        check(
            "malformed JSON: parse error -32700",
            r.json().get("error", {}).get("code") == -32700,
        )

        # 15. Origin allowlist (DNS-rebinding defence).
        r = client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Origin": "https://evil.example.com"},
        )
        check("disallowed Origin: 403", r.status_code == 403, str(r.status_code))

        r = client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Origin": "https://claude.ai"},
        )
        check("allowlisted Origin: 200", r.status_code == 200, str(r.status_code))

        # 16. MCP-Protocol-Version header validation.
        r = client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"MCP-Protocol-Version": "1999-01-01"},
        )
        check("bad protocol version: HTTP 400", r.status_code == 400, str(r.status_code))
        err = r.json().get("error", {})
        check("bad protocol version: -32600", err.get("code") == -32600, str(err))
        check(
            "bad protocol version: NOT -32022 (so dual-era clients fall back)",
            err.get("code") != -32022,
        )
        check(
            "bad protocol version: data lists supported revisions",
            bool((err.get("data") or {}).get("supported")),
            json.dumps(err.get("data")),
        )

        r = client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"MCP-Protocol-Version": "2025-06-18"},
        )
        check("supported protocol version: 200", r.status_code == 200)

        # 17. Structured output really conforms to the schema the server
        #     itself advertises -- validated live, not against a local copy.
        if Draft202012Validator is None:
            print("[SKIP] structuredContent validation (jsonschema not installed)")
        else:
            live_cases = [
                ("anchorage_parcels__find_parcel", {"parcel_id": "002-151-32"}),
                ("anchorage_parcels__get_parcel_details", {"parcel_id": "002-151-32"}),
                # Ambiguous condo root: the branch that returns candidates.
                ("anchorage_parcels__get_parcel_details", {"parcel_id": "012-182-04"}),
                # A miss: the zero-result branch must still emit structure.
                ("anchorage_parcels__find_parcel", {"parcel_id": "999-999-99"}),
                (
                    "anchorage_parcels__query_parcels",
                    {"where": "Zoning_District = 'RO'", "limit": 2},
                ),
                (
                    "anchorage_parcels__parcel_stats",
                    {
                        "stat_type": "count",
                        "stat_field": "Parcel_ID",
                        "group_by": "GIS_Category",
                        "category": "All",
                    },
                ),
            ]
            for name, args in live_cases:
                result, _ = call_tool(client, url, name, args)
                structured = result.get("structuredContent")
                label = f"{name}({json.dumps(args)[:40]})"
                check(
                    f"structuredContent present: {label}",
                    structured is not None,
                    tool_text(result)[:200],
                )
                errors = sorted(
                    Draft202012Validator(schemas[name]).iter_errors(structured),
                    key=lambda e: list(e.path),
                )
                check(
                    f"structuredContent conforms to advertised schema: {label}",
                    not errors,
                    "; ".join(
                        f"{list(e.path)}: {e.message}" for e in errors[:3]
                    ),
                )
                text = tool_text(result)
                missing = [
                    c["code"]
                    for c in structured.get("caveats", [])
                    if c["message"] not in text
                ]
                check(
                    f"every structured caveat appears in the text: {label}",
                    not missing,
                    str(missing),
                )

        # 18. CORS preflight allows the MCP request headers.
        r = client.options(
            url,
            headers={
                "Origin": "https://claude.ai",
                "Access-Control-Request-Method": "POST",
            },
        )
        allow = r.headers.get("access-control-allow-headers", "")
        check(
            "CORS preflight: mcp-* request headers allowed",
            "mcp-protocol-version" in allow and "mcp-method" in allow,
            allow,
        )

    print(f"\nAll {PASSED} smoke checks passed against {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
