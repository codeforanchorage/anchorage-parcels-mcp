"""Ad-hoc production smoke test for the Anchorage Parcels MCP server.

Exercises the JSON-RPC surface and the core tool chain end-to-end
against the live Lambda. Read-only; paces calls to stay under the
API Gateway rate limit (5 rps) and WAF per-IP cap (300/5min).

The fuller, assert-on-first-failure suite is scripts/smoke_http.py;
this one keeps going and prints a pass/fail line per check so a
partial outage is visible at a glance.
"""

import json
import sys
import time
import urllib.error
import urllib.request

# Prod by default; pass any /mcp URL (e.g. the local server) to retarget.
URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://anchorage-parcels.codeforanchorage.org/mcp"
)
SERVER_NAME = "Anchorage Parcels MCP"
TOOL_PREFIX = "anchorage_parcels__"
N_TOOLS = 7
# Known fixtures (see scripts/smoke_http.py): a single-record parcel and
# a 24-unit condo root whose details call must list every unit.
FIXTURE_PARCEL = "002-151-32"
CONDO_ROOT = "012-182-04"

_id = 0
PASS = "PASS"
FAIL = "FAIL"
results = []


def rpc(method, params=None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method}
    if params is not None:
        payload["params"] = params
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read().decode())
    time.sleep(0.4)  # pace under 5 rps
    return body


def call_tool(name, args):
    return rpc("tools/call", {"name": f"{TOOL_PREFIX}{name}", "arguments": args})


def text_of(resp):
    return resp["result"]["content"][0]["text"]


def check(label, ok, detail=""):
    results.append((label, ok))
    mark = PASS if ok else FAIL
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))


# 1. ping: the spec result is an empty object; the response IS the signal
try:
    r = rpc("ping")
    check("ping", r.get("result") == {}, str(r.get("result")))
except Exception as e:
    check("ping", False, repr(e))

# 2. initialize: configured serverInfo name, requested version echoed
try:
    r = rpc(
        "initialize",
        {"protocolVersion": "2025-03-26", "capabilities": {},
         "clientInfo": {"name": "smoke", "version": "1.0"}},
    )
    si = r["result"]["serverInfo"]
    pv = r["result"]["protocolVersion"]
    check(
        "initialize",
        si["name"] == SERVER_NAME and pv == "2025-03-26",
        json.dumps({"serverInfo": si, "protocolVersion": pv}),
    )
except Exception as e:
    check("initialize", False, repr(e))

# 3. tools/list
try:
    r = rpc("tools/list")
    tools = [t["name"] for t in r["result"]["tools"]]
    check("tools/list", len(tools) == N_TOOLS, f"{len(tools)} tools")
except Exception as e:
    check("tools/list", False, repr(e))

# 4. find_parcel: fixture record, hyphenated 8-digit form
try:
    r = call_tool("find_parcel", {"parcel_id": FIXTURE_PARCEL})
    t = text_of(r)
    check("find_parcel", "00215132000" in t and "144 W 15TH AVE" in t,
          f"{len(t)} chars")
except Exception as e:
    check("find_parcel", False, repr(e))

# 5. get_parcel_details: single record renders valuation + Datalet link
try:
    r = call_tool("get_parcel_details", {"parcel_id": FIXTURE_PARCEL})
    t = text_of(r)
    check("get_parcel_details", "## Valuation" in t and "Datalet" in t,
          f"{len(t)} chars")
except Exception as e:
    check("get_parcel_details", False, repr(e))

# 6. get_parcel_details on a condo root: every unit, no truncation
try:
    r = call_tool("get_parcel_details", {"parcel_id": CONDO_ROOT})
    t = text_of(r)
    ok = "matches 24 records" in t and "01218204024" in t and "TRUNCATED" not in t
    check("get_parcel_details (condo root, 24 units)", ok,
          t.splitlines()[0][:80] if t else "(empty)")
except Exception as e:
    check("get_parcel_details (condo root, 24 units)", False, repr(e))

# 7. search_by_address
try:
    r = call_tool("search_by_address", {"address": "632 W 6TH AVE", "limit": 5})
    t = text_of(r)
    check("search_by_address", "Record 1:" in t, f"{len(t)} chars")
except Exception as e:
    check("search_by_address", False, repr(e))

# 8. search_by_owner (names are stored UPPERCASE; the tool handles case)
try:
    r = call_tool(
        "search_by_owner", {"name": "municipality of anchorage", "limit": 3}
    )
    t = text_of(r)
    check("search_by_owner", "Record 1:" in t, f"{len(t)} chars")
except Exception as e:
    check("search_by_owner", False, repr(e))

# 9. parcels_at_point: downtown Anchorage
try:
    r = call_tool("parcels_at_point", {"lat": 61.2163, "lon": -149.8949})
    t = text_of(r)
    check("parcels_at_point", "Parcel_ID" in t, t.split("\n")[0][:80])
except Exception as e:
    check("parcels_at_point", False, repr(e))

# 10. parcel_stats: assessed Parcel count is ~97k
try:
    r = call_tool("parcel_stats", {"stat_type": "count", "stat_field": "Parcel_ID"})
    t = text_of(r)
    count = 0
    for line in t.splitlines():
        digits = line.strip().lstrip("- ").replace(",", "")
        if digits.isdigit():
            count = int(digits)
    check("parcel_stats count > 90,000", count > 90_000, f"{count:,}")
except Exception as e:
    check("parcel_stats count > 90,000", False, repr(e))

# 11. query_parcels: WHERE plus TOTAL COUNT metadata
try:
    r = call_tool("query_parcels", {"where": "Zoning_District = 'RO'", "limit": 2})
    t = text_of(r)
    check("query_parcels", "TOTAL COUNT" in t, f"{len(t)} chars")
except Exception as e:
    check("query_parcels", False, repr(e))

# 12. error handling: unknown tool is a -32602 protocol error, not -32603
try:
    r = call_tool("nope", {})
    err = r.get("error", {})
    ok = err.get("code") == -32602 and "available_tools" in (err.get("data") or {})
    check("error handling (unknown tool -> -32602)", ok, json.dumps(err)[:80])
except Exception as e:
    check("error handling (unknown tool -> -32602)", False, repr(e))

# 13. error handling: bad field -> readable tool error naming the field
try:
    r = call_tool("query_parcels", {"where": "Nonexistent_Field='x'", "limit": 1})
    t = text_of(r)
    ok = bool(r["result"].get("isError")) and "Nonexistent_Field" in t
    check("error handling (bad field -> named in error)", ok, t[:90])
except Exception as e:
    check("error handling (bad field -> named in error)", False, repr(e))


# -- modern era (MCP 2026-07-28, stateless, per-request _meta) ------------
# Everything above rides the legacy initialize handshake. claude.ai and
# Claude Code open with this era instead, so a regression here is invisible
# to the legacy checks yet breaks every real session.

MODERN = "2026-07-28"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientInfo": {"name": "smoke", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def raw_post(payload, extra_headers=None, http_method="POST"):
    """POST and return (status, parsed body or None) without raising on 4xx."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(extra_headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(URL, data=data, headers=headers, method=http_method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            status, raw = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()
    time.sleep(0.4)
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw


def modern(method, params=None, rid=None, name=None, version=MODERN):
    global _id
    _id += 1
    params = dict(params or {})
    params["_meta"] = {**MODERN_META, "io.modelcontextprotocol/protocolVersion": version}
    headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if name:
        headers["Mcp-Name"] = name
    return raw_post(
        {"jsonrpc": "2.0", "id": rid if rid is not None else _id,
         "method": method, "params": params},
        headers,
    )


# 20. server/discover: the modern replacement for initialize
try:
    status, r = modern("server/discover")
    res = r.get("result", {})
    ok = (
        status == 200
        and res.get("supportedVersions", [None])[0] == MODERN
        and "2025-03-26" in res.get("supportedVersions", [])
        and res.get("resultType") == "complete"
        and res.get("cacheScope") == "public"
        and isinstance(res.get("ttlMs"), int)
        and res.get("_meta", {}).get("io.modelcontextprotocol/serverInfo", {}).get("name")
        == SERVER_NAME
        and bool(res.get("instructions"))
    )
    check("modern: server/discover", ok,
          f"status={status} versions={res.get('supportedVersions')}")
except Exception as e:
    check("modern: server/discover", False, repr(e))

# 21. tools/list carries the CacheableResult hints and resultType
try:
    status, r = modern("tools/list")
    res = r.get("result", {})
    names = [t["name"] for t in res.get("tools", [])]
    ok = (
        status == 200
        and len(names) == N_TOOLS
        and res.get("resultType") == "complete"
        and res.get("ttlMs") == 300_000
        and res.get("cacheScope") == "public"
        and all("title" in t and "annotations" in t for t in res.get("tools", []))
    )
    check("modern: tools/list cache hints + titles", ok,
          f"status={status} n={len(names)} ttlMs={res.get('ttlMs')}")
    # Deterministic order (spec SHOULD): a second listing must match.
    status2, r2 = modern("tools/list")
    names2 = [t["name"] for t in r2.get("result", {}).get("tools", [])]
    check("modern: tools/list order is deterministic", names == names2,
          "identical" if names == names2 else "order differs between calls")
except Exception as e:
    check("modern: tools/list cache hints + titles", False, repr(e))

# 22. tools/call with mirrored Mcp-Name header; result stamped resultType
try:
    tool = f"{TOOL_PREFIX}find_parcel"
    status, r = modern(
        "tools/call", {"name": tool, "arguments": {"parcel_id": FIXTURE_PARCEL}},
        name=tool,
    )
    res = r.get("result", {})
    ok = (
        status == 200
        and res.get("resultType") == "complete"
        and not res.get("isError")
        and "00215132000" in res["content"][0]["text"]
        and "io.modelcontextprotocol/serverInfo" in res.get("_meta", {})
    )
    check("modern: tools/call find_parcel", ok, f"status={status}")
except Exception as e:
    check("modern: tools/call find_parcel", False, repr(e))

# 23. ping is gone in this era: 404 + -32601, not a silent {}
try:
    status, r = modern("ping")
    ok = status == 404 and r["error"]["code"] == -32601
    check("modern: ping -> 404/-32601", ok, f"status={status} body={json.dumps(r)[:80]}")
except Exception as e:
    check("modern: ping -> 404/-32601", False, repr(e))

# 24. unsupported version -> 400 / -32022 listing what we serve, id echoed
try:
    status, r = modern("tools/list", rid=4242, version="2099-01-01")
    err = r.get("error", {})
    ok = (
        status == 400
        and err.get("code") == -32022
        and err.get("data", {}).get("requested") == "2099-01-01"
        and MODERN in err.get("data", {}).get("supported", [])
        and r.get("id") == 4242
    )
    check("modern: unsupported version -> 400/-32022 (id echoed)", ok,
          f"status={status} id={r.get('id')} code={err.get('code')}")
except Exception as e:
    check("modern: unsupported version -> 400/-32022 (id echoed)", False, repr(e))

# 25. header/body disagreement -> 400 / -32020 HeaderMismatch
try:
    status, r = raw_post(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
         "params": {"_meta": MODERN_META}},
        {"MCP-Protocol-Version": MODERN, "Mcp-Method": "ping"},
    )
    ok = status == 400 and r["error"]["code"] == -32020
    check("modern: header mismatch -> 400/-32020", ok, f"status={status}")
except Exception as e:
    check("modern: header mismatch -> 400/-32020", False, repr(e))

# 26. missing clientCapabilities -> malformed, 400 / -32602
try:
    status, r = raw_post(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
         "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": MODERN}}},
        {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"},
    )
    ok = status == 400 and r["error"]["code"] == -32602
    check("modern: missing clientCapabilities -> 400/-32602", ok, f"status={status}")
except Exception as e:
    check("modern: missing clientCapabilities -> 400/-32602", False, repr(e))

# 27. transport hygiene: GET is 405 (no standalone SSE stream in 2026-07-28),
#     a disallowed Origin is 403
try:
    status, _ = raw_post(None, {"Accept": "text/event-stream"}, http_method="GET")
    check("transport: GET /mcp -> 405", status == 405, f"status={status}")
    status, _ = raw_post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                         {"Origin": "https://evil.example"})
    check("transport: bad Origin -> 403", status == 403, f"status={status}")
except Exception as e:
    check("transport hygiene", False, repr(e))


print("\n=== SUMMARY ===")
n_pass = sum(1 for _, ok in results if ok)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
