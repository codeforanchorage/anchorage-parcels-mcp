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
import urllib.request

URL = "https://anchorage-parcels.codeforanchorage.org/mcp"
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

print("\n=== SUMMARY ===")
n_pass = sum(1 for _, ok in results if ok)
print(f"{n_pass}/{len(results)} checks passed")
sys.exit(0 if n_pass == len(results) else 1)
