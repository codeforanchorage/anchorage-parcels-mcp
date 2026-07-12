"""Live smoke test for the anchorage_parcels plugin against the real
MOA PropertyInformation layer.

Network required -- the script SKIPs (exit 0) when the ArcGIS endpoint
is unreachable, so it is safe in offline CI runs.

Run:  python scripts/smoke_parcels.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from plugins.anchorage_parcels.plugin import AnchorageParcelsPlugin  # noqa: E402

CONFIG: dict = {"timeout": 30}  # all-default MOA config

PASSED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail else ""))
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    PASSED += 1


async def tool_text(plugin: AnchorageParcelsPlugin, name: str, args: dict) -> str:
    result = await plugin.execute_tool(name, args)
    if not result.success:
        raise AssertionError(f"{name}{args} failed: {result.error_message}")
    return result.content[0]["text"] if result.content else ""


async def main() -> int:
    plugin = AnchorageParcelsPlugin(CONFIG)
    try:
        ok = await plugin.initialize()
    except Exception as e:
        print(f"SKIP: could not initialize plugin ({e}); network required.")
        return 0
    if not ok:
        print("SKIP: plugin initialize() returned False; network required.")
        return 0
    if plugin._live_fields is None:
        print("SKIP: MOA property layer unreachable; network required.")
        await plugin.shutdown()
        return 0

    try:
        # 1. find_parcel with the 8-digit hyphenated form.
        text = await tool_text(plugin, "find_parcel", {"parcel_id": "002-151-32"})
        check("find_parcel 002-151-32: Parcel_ID", "00215132000" in text)
        check("find_parcel 002-151-32: address", "144 W 15TH AVE" in text)
        check(
            "find_parcel 002-151-32: zoning RO",
            "Zoning_District: RO" in text,
        )

        # 2. find_parcel with the 11-digit unformatted form.
        text = await tool_text(plugin, "find_parcel", {"parcel_id": "00215420000"})
        check("find_parcel 00215420000: formatted", "002-154-20" in text)
        check("find_parcel 00215420000: address", "620 W 15TH AVE" in text)

        # 3. search_by_owner returns rows.
        text = await tool_text(
            plugin,
            "search_by_owner",
            {"name": "municipality of anchorage", "limit": 5},
        )
        check(
            "search_by_owner MOA: >0 rows",
            "Record 1:" in text,
            text.splitlines()[0] if text else "(empty)",
        )

        # 4. Round-trip: fixture parcel centroid -> parcels_at_point ->
        #    same Parcel_ID. Centroid requested straight from the layer
        #    in WGS84 (outSR=4326).
        fixture = "00215132000"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{plugin.plugin_config.property_layer_url}/query",
                params={
                    "f": "json",
                    "where": f"Parcel_ID = '{fixture}'",
                    "outFields": "Parcel_ID",
                    "returnGeometry": "true",
                    "returnCentroid": "true",
                    "outSR": "4326",
                    "resultRecordCount": "1",
                },
            )
            resp.raise_for_status()
            feats = resp.json().get("features") or []
        check("round-trip: fixture parcel fetched", len(feats) == 1)
        centroid = feats[0].get("centroid") or {}
        lon, lat = centroid.get("x"), centroid.get("y")
        check(
            "round-trip: centroid present",
            lon is not None and lat is not None,
            f"lon={lon}, lat={lat}",
        )
        text = await tool_text(plugin, "parcels_at_point", {"lat": lat, "lon": lon})
        check(
            "round-trip: parcels_at_point returns fixture parcel",
            fixture in text,
        )

        # 5. parcel_stats: count by GIS_Category across ALL categories.
        text = await tool_text(
            plugin,
            "parcel_stats",
            {
                "stat_type": "count",
                "stat_field": "Parcel_ID",
                "group_by": "GIS_Category",
                "category": "All",
            },
        )
        for cat in ("Parcel", "Lease", "Economic"):
            check(f"parcel_stats: category {cat} present", f"- {cat}:" in text)
        parcel_count = 0
        for line in text.splitlines():
            if line.strip().startswith("- Parcel:"):
                parcel_count = int(line.split(":", 1)[1].strip().replace(",", ""))
        check(
            "parcel_stats: Parcel count > 90,000",
            parcel_count > 90_000,
            f"count={parcel_count:,}",
        )

        # 6. Bonus: median assessed value by zoning (the worked example
        #    from the tool description) -- just has to succeed.
        text = await tool_text(
            plugin,
            "parcel_stats",
            {
                "stat_type": "percentile_cont",
                "stat_field": "Appraised_Total_Value",
                "group_by": "Zoning_District",
            },
        )
        check(
            "parcel_stats: median by zoning returns groups",
            "percentile_cont(0.5)" in text and "- " in text,
        )

        print(f"\nAll {PASSED} smoke checks passed.")
        return 0
    finally:
        await plugin.shutdown()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
