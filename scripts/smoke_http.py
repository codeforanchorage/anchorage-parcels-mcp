"""Live smoke test against a DEPLOYED MCP endpoint over HTTP.

The HTTP-level counterpart to smoke_parcels.py: where that script
exercises the plugin in-process against the ArcGIS layer, this one
exercises the full deployed stack (API Gateway -> Lambda ->
http_handler -> mcp_server -> plugin -> ArcGIS) via MCP JSON-RPC,
including protocol-version negotiation, tools/list, every tool, the
error paths, and CORS preflight.

Network required -- the script SKIPs (exit 0) when the endpoint is
unreachable, so it is safe in offline CI runs.

Run:  python scripts/smoke_http.py            # prod (custom domain)
      python scripts/smoke_http.py staging    # staging stack
      python scripts/smoke_http.py <url>      # any /mcp endpoint
"""

import json
import sys

import httpx

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
        raise AssertionError(f"{label}: {detail}")
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

        # 2. ping.
        r = rpc(client, url, "ping")
        check("ping: ok", r.json().get("result", {}).get("status") == "ok")

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
        result, data = call_tool(client, url, "anchorage_parcels__nope", {})
        check(
            "unknown tool: surfaced as error",
            bool(result.get("isError") or "error" in data),
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

        # 15. CORS preflight allows the MCP request headers.
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
