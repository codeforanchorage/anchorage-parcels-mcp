"""Tests for the Anchorage Parcels plugin.

Verifies plugin initialization (including the schema-drift warning
path), tool definitions, parcel-ID normalization across all four MOA
input formats, WHERE construction (category filter, owner uppercasing),
pagination math, statistics parameter mapping (including percentiles),
and error handling. All HTTP is mocked; see scripts/smoke_parcels.py
for the live end-to-end checks.
"""

import json
import logging
from unittest.mock import AsyncMock, Mock, patch

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from core.interfaces import PluginType
from core.plugin_manager import PluginManager
from plugins.anchorage_parcels.config_schema import (
    DEFAULT_ADDRESSES_LAYER_URL,
    DEFAULT_PROPERTY_LAYER_URL,
    AnchorageParcelsPluginConfig,
)
from plugins.anchorage_parcels.plugin import (
    CAVEAT_CODES,
    DEFAULT_FIELD_MAP,
    AnchorageParcelsPlugin,
    _normalize_parcel_variants,
)

SNAPSHOT_PATH = AnchorageParcelsPlugin.SCHEMA_SNAPSHOT_PATH

with open(SNAPSHOT_PATH, encoding="utf-8") as _fh:
    _SNAPSHOT = json.load(_fh)
SNAPSHOT_FIELDS = {f["name"] for f in _SNAPSHOT["fields"]}


def make_response(payload, status=200):
    resp = Mock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {"content-type": "application/json"}
    resp.text = json.dumps(payload)
    return resp


def feat(**attrs):
    return {"attributes": attrs}


SAMPLE_RECORD = dict(
    Parcel_ID="00215132000",
    GIS_ParcelNum8Formatted="002-151-32",
    Owner_Name="ALASKA HANA METHODIST CHURCH",
    Parcel_Address="144 W 15TH AVE",
    Legal_Description="THIRD ADDITION BLK 32B LT6",
    Zoning_District="RO",
    Land_Use="Office Bldg - low rise 1-4 lvls",
    Lot_Size=7000,
    CAMA_Acreage=0.16069789,
    Appraised_Total_Value=471300,
    Taxable_Value=0,
    YearBuilt="1962",
    GIS_Category="Parcel",
    Parcel_ID_URL="https://property.muni.org/Datalets/Datalet.aspx?pin=00215132000",
)


def install_client(plugin, routes=None, default=None):
    """Install a fake httpx client that routes by URL/params.

    ``routes`` is a list of (predicate(url, params) -> bool, payload).
    The first matching payload is returned; ``default`` otherwise.
    Returns the list of (url, params) calls for assertions.
    """
    calls = []
    default_payload = default if default is not None else {"features": []}

    async def fake_get(url, params=None):
        params = dict(params or {})
        calls.append((url, params))
        for predicate, payload in routes or []:
            if predicate(url, params):
                if isinstance(payload, Exception):
                    raise payload
                return make_response(payload)
        return make_response(default_payload)

    client = Mock()
    client.get = AsyncMock(side_effect=fake_get)
    client.aclose = AsyncMock()
    plugin.client = client
    return calls


def is_count(url, params):
    return params.get("returnCountOnly") == "true"


def is_feature_query(url, params):
    return url.endswith("/query") and "returnCountOnly" not in params


@pytest.fixture
def plugin():
    """Initialized-state plugin with live fields from the vendored snapshot."""
    p = AnchorageParcelsPlugin({})
    p.plugin_config = AnchorageParcelsPluginConfig()
    p._live_fields = set(SNAPSHOT_FIELDS)
    p._date_fields = {"PUBDATE", "Deed_Date"}
    p._initialized = True
    return p


async def run_tool(plugin, name, args):
    result = await plugin.execute_tool(name, args)
    return result


def tool_text(result):
    assert result.success, result.error_message
    return result.content[0]["text"]


# ── Plugin attributes & tool definitions ───────────────────────────────


class TestPluginAttributes:
    def test_plugin_attributes(self):
        p = AnchorageParcelsPlugin({})
        assert p.plugin_name == "anchorage_parcels"
        assert p.plugin_type == PluginType.OPEN_DATA
        assert p.plugin_version == "1.0.0"

    def test_field_map_covers_summary_and_snapshot(self):
        """Every property-layer field in the map exists in the snapshot."""
        for logical, physical in DEFAULT_FIELD_MAP.items():
            if logical in ("address_full", "subdivision_name"):
                continue
            assert physical in SNAPSHOT_FIELDS, (logical, physical)


class TestGetTools:
    def test_get_tools_returns_all_seven(self, plugin):
        tools = plugin.get_tools()
        assert len(tools) == 7
        names = [t.name for t in tools]
        assert names == [
            "find_parcel",
            "get_parcel_details",
            "search_by_owner",
            "search_by_address",
            "parcels_at_point",
            "query_parcels",
            "parcel_stats",
        ]

    def test_all_tools_marked_read_only(self, plugin):
        tools = plugin.get_tools()
        for t in tools:
            assert t.annotations is not None, f"{t.name} missing annotations"
            assert t.annotations.get("readOnlyHint") is True, t.name
            assert t.annotations.get("openWorldHint") is True, t.name

    def test_descriptions_carry_example_and_case_note(self, plugin):
        for t in plugin.get_tools():
            assert "case-sensitive" in t.description.lower(), t.name
            # Every description carries a worked example (an example
            # call or example value).
            assert "example" in t.description.lower(), t.name

    def test_get_tools_works_before_initialize(self):
        # tools/list must not require a live layer fetch.
        p = AnchorageParcelsPlugin({})
        assert len(p.get_tools()) == 7


class TestToolMetadata:
    """tools/list metadata: title, annotations, deterministic ordering."""

    @staticmethod
    def _manager(plugin):
        manager = PluginManager({})
        manager.plugins = {"anchorage_parcels": plugin}
        return manager

    def test_every_tool_declares_a_title(self, plugin):
        """The wire `name` is plugin-prefixed and reads badly in a picker."""
        for t in plugin.get_tools():
            assert t.title, f"{t.name} has no title"
            assert t.title != t.name

    def test_title_is_emitted_top_level_not_as_an_annotation(self, plugin):
        """`title` is a top-level Tool field via BaseMetadata.

        Display precedence is title -> annotations.title -> name, so a
        title buried in annotations would be ignored by any client
        reading the top-level field.
        """
        for tool in self._manager(plugin).get_all_tools():
            assert "title" in tool, tool["name"]
            assert "title" not in tool.get("annotations", {}), tool["name"]

    def test_idempotent_hint_is_not_set(self, plugin):
        """Documented as meaningful only when readOnlyHint is false, so
        setting it on these read-only tools would be noise."""
        for t in plugin.get_tools():
            assert "idempotentHint" not in t.annotations, t.name

    def test_tools_list_ordering_is_deterministic(self, plugin):
        """Stable ordering lets clients cache tools/list and keeps
        prompt-cache hits alive."""
        manager = self._manager(plugin)
        first = [t["name"] for t in manager.get_all_tools()]
        for _ in range(3):
            assert [t["name"] for t in manager.get_all_tools()] == first

    def test_tool_names_are_prefixed(self, plugin):
        for tool in self._manager(plugin).get_all_tools():
            assert tool["name"].startswith("anchorage_parcels__")


# ── Config schema ──────────────────────────────────────────────────────


class TestConfigSchema:
    def test_defaults_point_at_moa(self):
        cfg = AnchorageParcelsPluginConfig()
        assert cfg.property_layer_url == DEFAULT_PROPERTY_LAYER_URL
        assert cfg.addresses_layer_url == DEFAULT_ADDRESSES_LAYER_URL
        assert cfg.city_name == "Municipality of Anchorage"
        assert cfg.timeout == 30
        assert cfg.field_map == {}

    def test_rejects_bad_url(self):
        with pytest.raises(ValidationError):
            AnchorageParcelsPluginConfig(property_layer_url="not-a-url")
        with pytest.raises(ValidationError):
            AnchorageParcelsPluginConfig(property_layer_url="ftp://x.example/0")

    def test_rejects_unknown_keys(self):
        with pytest.raises(ValidationError):
            AnchorageParcelsPluginConfig(bogus_key=True)

    def test_strips_trailing_slash(self):
        cfg = AnchorageParcelsPluginConfig(
            property_layer_url="https://x.example/FeatureServer/0/"
        )
        assert cfg.property_layer_url.endswith("/FeatureServer/0")


# ── Initialization & schema drift ──────────────────────────────────────


def _layer_meta(field_names):
    return {
        "name": "PropertyInformation",
        "fields": [
            {
                "name": n,
                "type": (
                    "esriFieldTypeDate"
                    if n in ("PUBDATE", "Deed_Date")
                    else "esriFieldTypeString"
                ),
            }
            for n in field_names
        ],
    }


class TestInitialization:
    @pytest.mark.asyncio
    async def test_initialize_success_no_drift(self, caplog):
        p = AnchorageParcelsPlugin({"timeout": 5})
        with patch("httpx.AsyncClient") as client_cls:
            client = Mock()
            client.get = AsyncMock(
                return_value=make_response(_layer_meta(SNAPSHOT_FIELDS))
            )
            client_cls.return_value = client
            with caplog.at_level(logging.WARNING):
                assert await p.initialize() is True
        assert p._initialized is True
        assert p._live_fields == SNAPSHOT_FIELDS
        assert p._date_fields == {"PUBDATE", "Deed_Date"}
        assert "SCHEMA DRIFT" not in caplog.text

    @pytest.mark.asyncio
    async def test_initialize_warns_on_schema_drift_but_starts(self, caplog):
        live = set(SNAPSHOT_FIELDS) - {"Owner_Name"} | {"Owner_Name_Renamed"}
        p = AnchorageParcelsPlugin({})
        with patch("httpx.AsyncClient") as client_cls:
            client = Mock()
            client.get = AsyncMock(return_value=make_response(_layer_meta(live)))
            client_cls.return_value = client
            with caplog.at_level(logging.WARNING):
                assert await p.initialize() is True
        assert "SCHEMA DRIFT" in caplog.text
        drift_records = [r for r in caplog.records if "SCHEMA DRIFT" in r.getMessage()]
        assert drift_records[0].missing_fields == ["Owner_Name"]
        assert drift_records[0].added_fields == ["Owner_Name_Renamed"]
        # The field map references Owner_Name, which is now missing.
        assert any(
            "field map references fields missing" in r.getMessage()
            for r in caplog.records
        )
        # Degraded, not down.
        assert p._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_unreachable_layer_still_starts(self, caplog):
        import httpx

        p = AnchorageParcelsPlugin({})
        with patch("httpx.AsyncClient") as client_cls:
            client = Mock()
            client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
            client_cls.return_value = client
            with caplog.at_level(logging.WARNING):
                assert await p.initialize() is True
        assert p._initialized is True
        assert p._live_fields is None
        assert "unreachable at startup" in caplog.text

    @pytest.mark.asyncio
    async def test_initialize_bad_config_fails_fast(self):
        p = AnchorageParcelsPlugin({"property_layer_url": "not-a-url"})
        assert await p.initialize() is False
        assert p._initialized is False

    @pytest.mark.asyncio
    async def test_field_map_override_applied(self):
        p = AnchorageParcelsPlugin({"field_map": {"owner_name": "OWNER"}})
        with patch("httpx.AsyncClient") as client_cls:
            client = Mock()
            client.get = AsyncMock(
                return_value=make_response(_layer_meta(SNAPSHOT_FIELDS))
            )
            client_cls.return_value = client
            assert await p.initialize() is True
        assert p._f("owner_name") == "OWNER"
        # Untouched keys keep their defaults.
        assert p._f("parcel_id") == "Parcel_ID"


# ── Parcel-ID normalization / find_parcel ──────────────────────────────


class TestFindParcel:
    ALL_VARIANTS = {"00215132", "002-151-32", "00215132000", "002-151-32-000"}

    @pytest.mark.parametrize(
        "raw",
        ["002-151-32", "00215132", "00215132000", "002-151-32-000"],
    )
    def test_normalize_variants_all_four_input_formats(self, raw):
        variants = set(_normalize_parcel_variants(raw))
        assert self.ALL_VARIANTS <= variants

    @pytest.mark.parametrize(
        "raw", ["002-151-32", "00215132", "00215132000", "002-151-32-000"]
    )
    @pytest.mark.asyncio
    async def test_where_construction_for_each_input_format(self, plugin, raw):
        calls = install_client(
            plugin,
            routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})],
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": raw})
        text = tool_text(result)
        url, params = calls[0]
        where = params["where"]
        # Exact-match OR across all five parcel-number columns.
        for field in (
            "Parcel_ID",
            "GIS_ParcelNum8",
            "GIS_ParcelNum8Formatted",
            "GIS_ParcelNum11",
            "GIS_ParcelNum11Formatted",
        ):
            assert f"{field} IN (" in where
        # All four canonical variants inside the IN lists.
        for v in self.ALL_VARIANTS:
            assert f"'{v}'" in where
        # Default category filter applies.
        assert "GIS_Category = 'Parcel'" in where
        assert params["returnGeometry"] == "false"
        assert "144 W 15TH AVE" in text

    @pytest.mark.asyncio
    async def test_category_all_drops_filter(self, plugin):
        calls = install_client(
            plugin,
            routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})],
        )
        await run_tool(
            plugin, "find_parcel", {"parcel_id": "00215132000", "category": "All"}
        )
        assert "GIS_Category" not in calls[0][1]["where"]

    @pytest.mark.asyncio
    async def test_garbage_input_is_an_error(self, plugin):
        install_client(plugin)
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "abc"})
        assert result.success is False
        assert "Could not extract" in result.error_message

    @pytest.mark.asyncio
    async def test_short_digits_are_padded_not_rejected(self, plugin):
        # 5+ digits is enough for the normalizer (left-padded to 8).
        calls = install_client(
            plugin,
            routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})],
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "215132"})
        assert result.success is True
        assert "'00215132000'" in calls[0][1]["where"]

    @pytest.mark.asyncio
    async def test_invalid_category_is_an_error(self, plugin):
        install_client(plugin)
        result = await run_tool(
            plugin,
            "find_parcel",
            {"parcel_id": "00215132000", "category": "Bogus"},
        )
        assert result.success is False
        assert "category must be one of" in result.error_message

    @pytest.mark.asyncio
    async def test_zero_hits_falls_back_to_fuzzy_like(self, plugin):
        candidate = feat(
            Parcel_ID="00215132999",
            Parcel_Address="146 W 15TH AVE",
            Owner_Name="SOMEONE ELSE",
        )

        def is_like_query(url, params):
            return "LIKE" in params.get("where", "")

        calls = install_client(
            plugin,
            routes=[
                (is_like_query, {"features": [candidate]}),
                (is_feature_query, {"features": []}),
            ],
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "002-151-32"})
        text = tool_text(result)
        assert "no exact match" in text
        assert "FUZZY" in text
        assert "00215132999" in text
        # The LIKE fallback searched a distinctive substring (leading
        # zeros stripped, first six digits).
        like_call = calls[-1]
        assert "Parcel_ID LIKE '%215132%'" in like_call[1]["where"]

    @pytest.mark.asyncio
    async def test_zero_hits_and_no_candidates(self, plugin):
        install_client(plugin, routes=[(is_feature_query, {"features": []})])
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "002-151-32"})
        text = tool_text(result)
        assert "no exact match" in text
        assert "category='All'" in text


# ── get_parcel_details ─────────────────────────────────────────────────


class TestGetParcelDetails:
    @pytest.mark.asyncio
    async def test_single_record_sections_and_datalet_link(self, plugin):
        record = dict(SAMPLE_RECORD)
        record.update(
            Appraisal_Year=2026,
            Appraised_Land_Value=161900,
            Appraised_Building_Value=309400,
            Deed_Date=1572264000000,  # 2019-10-28T12:00:00Z
            PUBDATE=1783984751000,
            Owner_Address="PO BOX 231330",
            Owner_City="ANCHORAGE",
            Owner_State="AK",
            Owner_Zip="99523",
        )
        install_client(
            plugin, routes=[(is_feature_query, {"features": [feat(**record)]})]
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "002-151-32"}
        )
        text = tool_text(result)
        for section in (
            "## Identity & legal",
            "## Situs & land",
            "## Owner (mailing address of record)",
            "## Valuation (appraisal year 2026)",
            "## Exemptions",
            "## Deed & plat",
            "## Misc",
        ):
            assert section in text, section
        assert "$161,900" in text
        assert "Prior year (2025)" in text
        assert "Two years prior (2024)" in text
        # Epoch-ms deed date rendered as ISO, not a raw number.
        assert "2019-10-28" in text
        # Datalet link surfaced at the end.
        assert "Datalet" in text
        assert record["Parcel_ID_URL"] in text

    @pytest.mark.asyncio
    async def test_multiple_matches_lists_units_and_asks(self, plugin):
        condo1 = dict(SAMPLE_RECORD, Parcel_ID="00215132001", Condo_Unit_Number="1")
        condo2 = dict(SAMPLE_RECORD, Parcel_ID="00215132002", Condo_Unit_Number="2")
        install_client(
            plugin,
            routes=[(is_feature_query, {"features": [feat(**condo1), feat(**condo2)]})],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "002-151-32"}
        )
        text = tool_text(result)
        assert "matches 2 records" in text
        assert "Which unit" in text
        assert "00215132001" in text and "00215132002" in text

    @pytest.mark.asyncio
    async def test_multiple_matches_lists_all_units_not_just_12(self, plugin):
        """Regression: a 21-unit condo root must surface every unit.

        The disambiguation list used to be capped at 12 rows, so units
        13+ were silently dropped and the reported total was wrong.
        """
        units = [
            dict(
                SAMPLE_RECORD,
                Parcel_ID=f"002151320{n:02d}",
                Condo_Unit_Number=str(n),
            )
            for n in range(1, 22)
        ]
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 21}),
                (is_feature_query, {"features": [feat(**u) for u in units]}),
            ],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "002-151-32"}
        )
        text = tool_text(result)
        assert "matches 21 records" in text
        assert "00215132021" in text
        assert "TRUNCATED" not in text
        # The disambiguation refetch asks for all units in ID order.
        slim_call = calls[-1] if "orderByFields" in calls[-1][1] else calls[-2]
        assert slim_call[1]["orderByFields"] == "Parcel_ID"
        assert int(slim_call[1]["resultRecordCount"]) >= 21

    @pytest.mark.asyncio
    async def test_multiple_matches_truncation_banner(self, plugin):
        """When the true count exceeds the rows returned, say so."""
        units = [
            dict(
                SAMPLE_RECORD,
                Parcel_ID=f"002151320{n:02d}",
                Condo_Unit_Number=str(n),
            )
            for n in range(1, 13)
        ]
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 30}),
                (is_feature_query, {"features": [feat(**u) for u in units]}),
            ],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "002-151-32"}
        )
        text = tool_text(result)
        assert "matches 30 records" in text
        assert "LIST TRUNCATED" in text
        assert "first 12 of 30" in text

    @pytest.mark.asyncio
    async def test_no_match_points_at_find_parcel(self, plugin):
        install_client(plugin, routes=[(is_feature_query, {"features": []})])
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "002-151-32"}
        )
        text = tool_text(result)
        assert "No property record found" in text
        assert "find_parcel" in text


# ── search_by_owner ────────────────────────────────────────────────────


class TestSearchByOwner:
    @pytest.mark.asyncio
    async def test_where_uppercases_and_orders_by_value(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 1234}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)]}),
            ],
        )
        result = await run_tool(
            plugin, "search_by_owner", {"name": "municipality of anchorage"}
        )
        text = tool_text(result)
        feature_calls = [c for c in calls if is_feature_query(*c)]
        where = feature_calls[0][1]["where"]
        assert "UPPER(Owner_Name) LIKE '%MUNICIPALITY OF ANCHORAGE%'" in where
        assert "GIS_Category = 'Parcel'" in where
        assert feature_calls[0][1]["orderByFields"] == "Appraised_Total_Value DESC"
        # Total count from the parallel returnCountOnly query.
        assert "TOTAL COUNT" in text and "1,234" in text

    @pytest.mark.asyncio
    async def test_owner_quote_is_escaped(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 0}),
                (is_feature_query, {"features": []}),
            ],
        )
        result = await run_tool(plugin, "search_by_owner", {"name": "o'malley"})
        assert result.success is True
        feature_calls = [c for c in calls if is_feature_query(*c)]
        assert "O''MALLEY" in feature_calls[0][1]["where"]

    @pytest.mark.asyncio
    async def test_empty_result_carries_no_data_hint(self, plugin):
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 0}),
                (is_feature_query, {"features": []}),
            ],
        )
        result = await run_tool(plugin, "search_by_owner", {"name": "nobody"})
        text = tool_text(result)
        assert "No records returned" in text
        assert "If you expected matches" in text
        assert "UPPERCASE" in text

    @pytest.mark.asyncio
    async def test_name_required(self, plugin):
        install_client(plugin)
        result = await run_tool(plugin, "search_by_owner", {"name": "  "})
        assert result.success is False
        assert "name is required" in result.error_message


# ── search_by_address ──────────────────────────────────────────────────


class TestSearchByAddress:
    @pytest.mark.asyncio
    async def test_direct_situs_match_path(self, plugin):
        calls = install_client(
            plugin,
            routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})],
        )
        result = await run_tool(
            plugin, "search_by_address", {"address": "144 w 15th ave"}
        )
        text = tool_text(result)
        assert "matched directly on Parcel_Address" in text
        where = calls[0][1]["where"]
        assert "Parcel_Address LIKE '%144 W 15TH AVE%'" in where
        # Only the property layer was queried.
        assert all(DEFAULT_PROPERTY_LAYER_URL in c[0] for c in calls)

    @pytest.mark.asyncio
    async def test_fallback_via_address_points_and_pip(self, plugin):
        def is_addr_layer(url, params):
            return DEFAULT_ADDRESSES_LAYER_URL in url

        def is_pip(url, params):
            return params.get("geometryType") == "esriGeometryPoint"

        addr_point = {
            "attributes": {"FULL_ADDRESS": "26800 Eklutna Village Rd"},
            "geometry": {"x": -149.375648, "y": 61.465525},
        }
        calls = install_client(
            plugin,
            routes=[
                (is_addr_layer, {"features": [addr_point]}),
                (is_pip, {"features": [feat(**SAMPLE_RECORD)]}),
                (is_feature_query, {"features": []}),
            ],
        )
        result = await run_tool(
            plugin, "search_by_address", {"address": "26800 eklutna village rd"}
        )
        text = tool_text(result)
        assert "resolved via the address-point layer" in text
        assert "point-in-polygon" in text
        assert SAMPLE_RECORD["Parcel_ID"] in text
        addr_calls = [c for c in calls if is_addr_layer(*c)]
        assert addr_calls[0][1]["outSR"] == "4326"
        assert (
            "FULL_ADDRESS LIKE '%26800 EKLUTNA VILLAGE RD%'"
            in addr_calls[0][1]["where"]
        )
        pip_calls = [c for c in calls if is_pip(*c)]
        assert pip_calls[0][1]["inSR"] == "4326"
        assert pip_calls[0][1]["geometry"] == "-149.375648,61.465525"
        assert pip_calls[0][1]["spatialRel"] == "esriSpatialRelIntersects"

    @pytest.mark.asyncio
    async def test_both_paths_miss_gives_hints(self, plugin):
        install_client(plugin, routes=[(is_feature_query, {"features": []})])
        result = await run_tool(
            plugin, "search_by_address", {"address": "1 NOWHERE LN"}
        )
        text = tool_text(result)
        assert "no match either" in text
        assert "abbreviated street type" in text


# ── parcels_at_point ───────────────────────────────────────────────────


class TestParcelsAtPoint:
    @pytest.mark.asyncio
    async def test_point_query_params_and_category_labels(self, plugin):
        lease = dict(SAMPLE_RECORD, GIS_Category="Lease", Parcel_ID="00215132001")
        calls = install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {"features": [feat(**SAMPLE_RECORD), feat(**lease)]},
                )
            ],
        )
        result = await run_tool(
            plugin, "parcels_at_point", {"lat": 61.2091, "lon": -149.8944}
        )
        text = tool_text(result)
        params = calls[0][1]
        assert params["geometry"] == "-149.8944,61.2091"
        assert params["geometryType"] == "esriGeometryPoint"
        assert params["inSR"] == "4326"
        assert params["spatialRel"] == "esriSpatialRelIntersects"
        # ALL categories: no GIS_Category filter.
        assert params["where"] == "1=1"
        # Each hit labeled with its category; stacked categories counted.
        assert "Record 1 [Parcel]" in text
        assert "Record 2 [Lease]" in text
        assert "Lease=1" in text and "Parcel=1" in text

    @pytest.mark.asyncio
    async def test_out_of_range_coordinates_rejected(self, plugin):
        install_client(plugin)
        result = await run_tool(
            plugin, "parcels_at_point", {"lat": 161.2, "lon": -149.9}
        )
        assert result.success is False
        assert "out of range" in result.error_message

    @pytest.mark.asyncio
    async def test_missing_args_rejected(self, plugin):
        install_client(plugin)
        result = await run_tool(plugin, "parcels_at_point", {"lat": 61.2})
        assert result.success is False
        assert "lat and lon are required" in result.error_message

    @pytest.mark.asyncio
    async def test_no_hits_message(self, plugin):
        install_client(plugin, routes=[(is_feature_query, {"features": []})])
        result = await run_tool(
            plugin, "parcels_at_point", {"lat": 61.2, "lon": -149.9}
        )
        text = tool_text(result)
        assert "No property polygons contain point" in text


# ── query_parcels ──────────────────────────────────────────────────────


class TestQueryParcels:
    @pytest.mark.asyncio
    async def test_pagination_math_across_server_pages(self, plugin, monkeypatch):
        """limit > server page size -> multiple page fetches with
        correct resultOffset / resultRecordCount arithmetic."""
        monkeypatch.setattr(plugin, "SERVER_PAGE_SIZE", 100, raising=False)

        def page_response(url, params):
            n = int(params["resultRecordCount"])
            start = int(params["resultOffset"])
            feats = [
                feat(**dict(SAMPLE_RECORD, Parcel_ID=f"{start + i:011d}"))
                for i in range(n)
            ]
            return {"features": feats, "exceededTransferLimit": True}

        calls = []

        async def fake_get(url, params=None):
            params = dict(params or {})
            calls.append((url, params))
            if params.get("returnCountOnly") == "true":
                return make_response({"count": 5000})
            return make_response(page_response(url, params))

        client = Mock()
        client.get = AsyncMock(side_effect=fake_get)
        plugin.client = client

        result = await run_tool(
            plugin,
            "query_parcels",
            {"where": "1=1", "limit": 250, "offset": 37},
        )
        text = tool_text(result)
        pages = [c[1] for c in calls if "resultOffset" in c[1]]
        assert [
            (int(p["resultOffset"]), int(p["resultRecordCount"])) for p in pages
        ] == [(37, 100), (137, 100), (237, 50)]
        assert "Returned 250 of 5,000 total record(s)" in text
        assert "TRUNCATED" in text
        # Next-page hint uses offset + records returned.
        assert "offset=287" in text

    @pytest.mark.asyncio
    async def test_single_page_when_limit_below_page_size(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 1}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)]}),
            ],
        )
        result = await run_tool(
            plugin, "query_parcels", {"where": "Zoning_District='RO'"}
        )
        text = tool_text(result)
        feature_calls = [c for c in calls if is_feature_query(*c)]
        assert len(feature_calls) == 1
        where = feature_calls[0][1]["where"]
        # Category filter AND'd around the caller's clause.
        assert where == "(Zoning_District='RO') AND (GIS_Category = 'Parcel')"
        assert "MORE PAGES" not in text

    @pytest.mark.asyncio
    async def test_limit_clamped_to_max(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 0}),
                (is_feature_query, {"features": []}),
            ],
        )
        await run_tool(plugin, "query_parcels", {"where": "1=1", "limit": 999999})
        feature_calls = [c for c in calls if is_feature_query(*c)]
        assert (
            int(feature_calls[0][1]["resultRecordCount"])
            <= AnchorageParcelsPlugin.SERVER_PAGE_SIZE
        )

    @pytest.mark.asyncio
    async def test_limit_clamp_is_surfaced(self, plugin):
        """A limit above MAX_LIMIT must announce the clamp, not hide it."""
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 8000}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)]}),
            ],
        )
        result = await run_tool(
            plugin, "query_parcels", {"where": "1=1", "limit": 5000}
        )
        text = tool_text(result)
        assert "**LIMIT CLAMPED:**" in text
        assert "requested limit=5000" in text
        assert "maximum of 1000" in text
        assert "offset=" in text
        feature_calls = [c for c in calls if is_feature_query(*c)]
        assert int(feature_calls[0][1]["resultRecordCount"]) <= 1000

    @pytest.mark.asyncio
    async def test_in_range_limit_has_no_clamp_note(self, plugin):
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 1}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)]}),
            ],
        )
        result = await run_tool(
            plugin, "query_parcels", {"where": "1=1", "limit": 1000}
        )
        assert "LIMIT CLAMPED" not in tool_text(result)

    @pytest.mark.asyncio
    async def test_union_inside_string_literal_allowed(self, plugin):
        """Regression: 'CREDIT UNION DR' parcels must be queryable by
        address -- UNION inside a quoted literal is data, not SQL."""
        where = "Parcel_Address LIKE '%UNION%'"
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 8}),
                (
                    is_feature_query,
                    {
                        "features": [
                            feat(
                                **dict(
                                    SAMPLE_RECORD,
                                    Parcel_Address="4301 CREDIT UNION DR",
                                )
                            )
                        ]
                    },
                ),
            ],
        )
        result = await run_tool(plugin, "query_parcels", {"where": where})
        text = tool_text(result)
        assert "4301 CREDIT UNION DR" in text
        # The ORIGINAL clause (not a masked copy) is forwarded upstream.
        feature_calls = [c for c in calls if is_feature_query(*c)]
        assert where in feature_calls[0][1]["where"]

    @pytest.mark.asyncio
    async def test_unbalanced_quote_rejected(self, plugin):
        install_client(plugin)
        result = await run_tool(plugin, "query_parcels", {"where": "Owner_Name = 'ABC"})
        assert result.success is False
        assert "Unbalanced quote" in result.error_message

    @pytest.mark.asyncio
    async def test_injection_where_rejected(self, plugin):
        install_client(plugin)
        result = await run_tool(
            plugin,
            "query_parcels",
            {"where": "1=1; DROP TABLE parcels--"},
        )
        assert result.success is False

    @pytest.mark.asyncio
    async def test_unknown_field_in_where_gets_suggestion(self, plugin):
        install_client(plugin)
        result = await run_tool(
            plugin, "query_parcels", {"where": "Zoning_Distric='RO'"}
        )
        assert result.success is False
        assert "Zoning_District" in result.error_message

    @pytest.mark.asyncio
    async def test_bad_order_by_rejected(self, plugin):
        install_client(plugin)
        result = await run_tool(
            plugin,
            "query_parcels",
            {"where": "1=1", "order_by": "Lot_Size DESC; DROP"},
        )
        assert result.success is False


# ── Compact multi-record format ────────────────────────────────────────


class TestCompactRecordFormat:
    @pytest.mark.asyncio
    async def test_large_result_uses_pipe_table(self, plugin):
        """Above the threshold, records render as one pipe-delimited
        table (header + rows) instead of per-record blocks."""
        n = AnchorageParcelsPlugin.COMPACT_FORMAT_THRESHOLD + 5
        feats = [feat(**dict(SAMPLE_RECORD, Parcel_ID=f"{i:011d}")) for i in range(n)]
        install_client(
            plugin,
            routes=[
                (is_count, {"count": n}),
                (is_feature_query, {"features": feats}),
            ],
        )
        result = await run_tool(plugin, "query_parcels", {"where": "1=1", "limit": 100})
        text = tool_text(result)
        assert "Record 1:" not in text
        # Header row lists the columns in record order.
        assert "Parcel_ID | GIS_ParcelNum8Formatted | Owner_Name" in text
        assert f"Returned {n}" in text
        # Provenance and count conventions are untouched.
        assert text.startswith("Source: ")
        assert "TOTAL COUNT" in text

    @pytest.mark.asyncio
    async def test_small_result_keeps_record_blocks(self, plugin):
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 2}),
                (
                    is_feature_query,
                    {"features": [feat(**SAMPLE_RECORD), feat(**SAMPLE_RECORD)]},
                ),
            ],
        )
        result = await run_tool(plugin, "query_parcels", {"where": "1=1", "limit": 100})
        text = tool_text(result)
        assert "Record 1:" in text
        assert "Record 2:" in text

    def test_pipe_in_value_is_escaped(self, plugin):
        assert plugin._table_cell("A | B") == "A \\| B"
        assert plugin._table_cell(None) == ""


# ── parcel_stats ───────────────────────────────────────────────────────


class TestParcelStats:
    @pytest.mark.asyncio
    async def test_count_grouped_param_mapping(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {
                        "features": [
                            feat(GIS_Category="Economic", count_Parcel_ID=780),
                            feat(GIS_Category="Lease", count_Parcel_ID=596),
                            feat(GIS_Category="Parcel", count_Parcel_ID=98348),
                        ]
                    },
                )
            ],
        )
        result = await run_tool(
            plugin,
            "parcel_stats",
            {
                "stat_type": "count",
                "stat_field": "Parcel_ID",
                "group_by": "GIS_Category",
                "category": "All",
            },
        )
        text = tool_text(result)
        params = calls[0][1]
        stats = json.loads(params["outStatistics"])
        assert stats == [
            {
                "statisticType": "count",
                "onStatisticField": "Parcel_ID",
                "outStatisticFieldName": "count_Parcel_ID",
            }
        ]
        assert params["groupByFieldsForStatistics"] == "GIS_Category"
        assert params["orderByFields"] == "GIS_Category"
        assert params["where"] == "1=1"
        assert "- Parcel: 98,348" in text
        assert "- Lease: 596" in text
        assert "3 group(s)" in text

    @pytest.mark.asyncio
    async def test_percentile_mapping_includes_parameters(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {
                        "features": [
                            feat(**{"percentile_cont_Appraised_Total_Value": 425000.0})
                        ]
                    },
                )
            ],
        )
        result = await run_tool(
            plugin,
            "parcel_stats",
            {
                "stat_type": "percentile_cont",
                "stat_field": "Appraised_Total_Value",
                "percentile": 0.9,
            },
        )
        text = tool_text(result)
        stats = json.loads(calls[0][1]["outStatistics"])
        assert stats[0]["statisticType"] == "percentile_cont"
        assert stats[0]["statisticParameters"] == {"value": 0.9}
        # Ungrouped: no groupBy params sent.
        assert "groupByFieldsForStatistics" not in calls[0][1]
        assert "percentile_cont(0.9)" in text
        assert "425,000" in text

    @pytest.mark.asyncio
    async def test_percentile_default_is_median(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {"features": [feat(percentile_cont_Lot_Size=6000)]},
                )
            ],
        )
        result = await run_tool(
            plugin,
            "parcel_stats",
            {"stat_type": "percentile_cont", "stat_field": "Lot_Size"},
        )
        stats = json.loads(calls[0][1]["outStatistics"])
        assert stats[0]["statisticParameters"] == {"value": 0.5}
        assert "percentile_cont(0.5)" in tool_text(result)

    @pytest.mark.asyncio
    async def test_where_and_category_combined(self, plugin):
        calls = install_client(
            plugin,
            routes=[(is_feature_query, {"features": [feat(sum_Lot_Size=1)]})],
        )
        await run_tool(
            plugin,
            "parcel_stats",
            {
                "stat_type": "sum",
                "stat_field": "Lot_Size",
                "where": "Zoning_District='RO'",
            },
        )
        assert (
            calls[0][1]["where"]
            == "(Zoning_District='RO') AND (GIS_Category = 'Parcel')"
        )

    @pytest.mark.asyncio
    async def test_invalid_stat_type_rejected(self, plugin):
        install_client(plugin)
        result = await run_tool(
            plugin,
            "parcel_stats",
            {"stat_type": "median", "stat_field": "Lot_Size"},
        )
        assert result.success is False
        assert "percentile_cont" in result.error_message

    @pytest.mark.asyncio
    async def test_out_of_range_percentile_rejected(self, plugin):
        install_client(plugin)
        result = await run_tool(
            plugin,
            "parcel_stats",
            {
                "stat_type": "percentile_cont",
                "stat_field": "Lot_Size",
                "percentile": 1.5,
            },
        )
        assert result.success is False
        assert "percentile must be between" in result.error_message

    @pytest.mark.asyncio
    async def test_unknown_stat_field_gets_suggestion(self, plugin):
        install_client(plugin)
        result = await run_tool(
            plugin,
            "parcel_stats",
            {"stat_type": "avg", "stat_field": "Lot_Sizes"},
        )
        assert result.success is False
        assert "Lot_Size" in result.error_message


# ── Error handling & plumbing ──────────────────────────────────────────


# ── Structured output (outputSchema is a binding contract) ─────────────


def declared_schema(plugin, tool_name):
    for t in plugin.get_tools():
        if t.name == tool_name:
            assert t.output_schema, f"{tool_name} declares no output_schema"
            return t.output_schema
    raise AssertionError(f"no such tool: {tool_name}")


def assert_conforms(plugin, tool_name, result):
    """Validate a real tool result against the schema the server itself
    advertises, and check the two output forms agree.

    A declared outputSchema is BINDING: the spec says servers MUST return
    conforming structured results and clients SHOULD validate them.
    """
    assert result.success, result.error_message
    structured = result.structured_content
    assert structured is not None, (
        f"{tool_name} declares an outputSchema but returned no "
        f"structuredContent on this path"
    )
    Draft202012Validator(declared_schema(plugin, tool_name)).validate(structured)

    # Prose and structure are generated from ONE caveat list, so every
    # structured message must be visible in the text a model reads.
    text = result.content[0]["text"]
    for caveat in structured["caveats"]:
        assert caveat["code"] in CAVEAT_CODES, caveat
        assert caveat["message"] in text, (
            f"{tool_name}: caveat {caveat['code']} is in structuredContent "
            f"but not in the rendered text"
        )
    return structured


class TestOutputSchemaDeclarations:
    def test_every_tool_declares_a_valid_output_schema(self, plugin):
        for t in plugin.get_tools():
            assert t.output_schema, f"{t.name} has no output_schema"
            Draft202012Validator.check_schema(t.output_schema)

    def test_output_schema_is_advertised_on_the_wire(self, plugin):
        manager = PluginManager({})
        manager.plugins = {"anchorage_parcels": plugin}
        for tool in manager.get_all_tools():
            assert "outputSchema" in tool, tool["name"]

    def test_every_schema_uses_the_shared_envelope(self, plugin):
        """One shape across the server, so a model learns it once."""
        for t in plugin.get_tools():
            required = set(t.output_schema["required"])
            assert {"query", "summary", "caveats"} <= required, t.name
            assert "rows" in required or "result" in required, t.name

    def test_caveat_enum_matches_the_code_constant(self, plugin):
        """The schema's enum and the code list are the same single source;
        an emitted code outside the enum would violate our own contract."""
        for t in plugin.get_tools():
            enum = t.output_schema["properties"]["caveats"]["items"]["properties"][
                "code"
            ]["enum"]
            assert enum == list(CAVEAT_CODES), t.name


class TestStructuredOutputConformance:
    """Every code path of a schema-declaring tool must emit conforming
    structured content -- especially the awkward ones."""

    @pytest.mark.asyncio
    async def test_find_parcel_exact_match(self, plugin):
        install_client(
            plugin, routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})]
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "002-151-32"})
        structured = assert_conforms(plugin, "find_parcel", result)
        assert structured["summary"]["exact_match"] is True
        assert structured["summary"]["returned_count"] == 1

    @pytest.mark.asyncio
    async def test_find_parcel_fuzzy_fallback(self, plugin):
        """Zero exact hits: rows are LIKE candidates, not matches."""
        install_client(
            plugin,
            routes=[
                (
                    lambda u, p: "LIKE" in p.get("where", ""),
                    {"features": [feat(**SAMPLE_RECORD)]},
                ),
            ],
            default={"features": []},
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "002-151-32"})
        structured = assert_conforms(plugin, "find_parcel", result)
        assert structured["summary"]["exact_match"] is False
        assert any(c["code"] == "fuzzy_match" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_find_parcel_no_match_at_all(self, plugin):
        install_client(plugin, default={"features": []})
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "999-999-99"})
        structured = assert_conforms(plugin, "find_parcel", result)
        assert structured["rows"] == []
        assert structured["summary"]["returned_count"] == 0
        assert any(c["code"] == "no_fuzzy_candidates" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_find_parcel_limit_clamped(self, plugin):
        install_client(
            plugin, routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})]
        )
        result = await run_tool(
            plugin, "find_parcel", {"parcel_id": "002-151-32", "limit": 5000}
        )
        structured = assert_conforms(plugin, "find_parcel", result)
        assert any(c["code"] == "limit_clamped" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_get_parcel_details_found(self, plugin):
        install_client(
            plugin, routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})]
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "00215132000"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["summary"]["resolved"] is True
        assert structured["candidates"] == []
        # The RAW record, not the subset the prose renders.
        assert structured["result"]["Parcel_ID"] == "00215132000"

    @pytest.mark.asyncio
    async def test_get_parcel_details_not_found_still_emits_structure(self, plugin):
        """The zero-result branch must NOT short-circuit past the envelope
        -- a schema-declaring tool that returns nothing here is the exact
        bug this contract is meant to catch."""
        install_client(plugin, default={"features": []})
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "999-999-99"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["result"] is None
        assert structured["candidates"] == []
        assert structured["summary"]["resolved"] is False
        assert structured["summary"]["match_count"] == 0

    @pytest.mark.asyncio
    async def test_get_parcel_details_ambiguous_condo(self, plugin):
        units = [
            feat(
                Parcel_ID=f"0121820400{i}",
                GIS_Category="Parcel",
                Condo_Unit_Number=str(i),
                Parcel_Address="HUNTSMEN CIR",
                Owner_Name=f"OWNER {i}",
            )
            for i in range(1, 25)
        ]
        install_client(
            plugin,
            routes=[(is_count, {"count": 24}), (is_feature_query, {"features": units})],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "012-182-04"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["result"] is None
        assert structured["summary"]["match_count"] == 24
        assert structured["summary"]["match_count_is_lower_bound"] is False
        assert len(structured["candidates"]) == 24
        assert any(c["code"] == "multiple_records" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_get_parcel_details_ambiguous_count_unavailable(self, plugin):
        """Count endpoint fails -> match_count is a LOWER BOUND, not a
        string smuggled into an integer field."""
        units = [
            feat(Parcel_ID=f"0121820400{i}", GIS_Category="Parcel") for i in range(1, 4)
        ]
        install_client(
            plugin,
            routes=[
                (is_count, {"error": {"message": "boom"}}),
                (is_feature_query, {"features": units, "exceededTransferLimit": True}),
            ],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "012-182-04"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert isinstance(structured["summary"]["match_count"], int)
        assert structured["summary"]["match_count_is_lower_bound"] is True

    @pytest.mark.asyncio
    async def test_search_by_owner_with_and_without_total(self, plugin):
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 500}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)]}),
            ],
        )
        result = await run_tool(plugin, "search_by_owner", {"name": "municipality"})
        structured = assert_conforms(plugin, "search_by_owner", result)
        assert structured["summary"]["total_count"] == 500
        assert structured["summary"]["truncated"] is True

    @pytest.mark.asyncio
    async def test_search_by_owner_null_total_count_is_not_zero(self, plugin):
        """A failed count is null, which a caller must not read as 0."""
        install_client(
            plugin,
            routes=[
                (is_count, {"error": {"message": "boom"}}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)]}),
            ],
        )
        result = await run_tool(plugin, "search_by_owner", {"name": "municipality"})
        structured = assert_conforms(plugin, "search_by_owner", result)
        assert structured["summary"]["total_count"] is None
        assert structured["summary"]["returned_count"] == 1

    @pytest.mark.asyncio
    async def test_search_by_owner_empty(self, plugin):
        install_client(
            plugin,
            routes=[(is_count, {"count": 0})],
            default={"features": []},
        )
        result = await run_tool(plugin, "search_by_owner", {"name": "nobody"})
        structured = assert_conforms(plugin, "search_by_owner", result)
        assert structured["rows"] == []
        assert any(c["code"] == "no_results" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_search_by_address_situs_match(self, plugin):
        install_client(
            plugin, routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})]
        )
        result = await run_tool(plugin, "search_by_address", {"address": "144 W 15TH"})
        structured = assert_conforms(plugin, "search_by_address", result)
        assert structured["summary"]["match_method"] == "situs"
        assert structured["summary"]["address_point"] is None

    @pytest.mark.asyncio
    async def test_search_by_address_no_match_anywhere(self, plugin):
        install_client(plugin, default={"features": []})
        result = await run_tool(plugin, "search_by_address", {"address": "NOWHERE"})
        structured = assert_conforms(plugin, "search_by_address", result)
        assert structured["summary"]["match_method"] == "none"
        assert structured["rows"] == []
        assert any(c["code"] == "no_address_match" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_search_by_address_point_fallback(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    lambda u, p: p.get("returnGeometry") == "true",
                    {
                        "features": [
                            {
                                "attributes": {"FULL_ADDRESS": "144 W 15TH AVE"},
                                "geometry": {"x": -149.8944, "y": 61.2091},
                            }
                        ]
                    },
                ),
                (
                    lambda u, p: p.get("geometryType") == "esriGeometryPoint",
                    {"features": [feat(**SAMPLE_RECORD)]},
                ),
            ],
            default={"features": []},
        )
        result = await run_tool(plugin, "search_by_address", {"address": "144 W 15TH"})
        structured = assert_conforms(plugin, "search_by_address", result)
        assert structured["summary"]["match_method"] == "address_point"
        assert structured["summary"]["address_point"]["lat"] == 61.2091
        assert any(c["code"] == "address_point_fallback" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_parcels_at_point_stacked_and_empty(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {
                        "features": [
                            feat(Parcel_ID="1", GIS_Category="Parcel"),
                            feat(Parcel_ID="2", GIS_Category="Lease"),
                        ]
                    },
                )
            ],
        )
        result = await run_tool(
            plugin, "parcels_at_point", {"lat": 61.2091, "lon": -149.8944}
        )
        structured = assert_conforms(plugin, "parcels_at_point", result)
        assert structured["summary"]["by_category"] == {"Parcel": 1, "Lease": 1}
        assert any(c["code"] == "stacked_categories" for c in structured["caveats"])

        install_client(plugin, default={"features": []})
        empty = await run_tool(plugin, "parcels_at_point", {"lat": 61.2, "lon": -149.9})
        structured = assert_conforms(plugin, "parcels_at_point", empty)
        assert structured["rows"] == []
        assert structured["summary"]["by_category"] == {}

    @pytest.mark.asyncio
    async def test_query_parcels_paging_state(self, plugin):
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 500}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)] * 10}),
            ],
        )
        result = await run_tool(
            plugin, "query_parcels", {"where": "1=1", "limit": 10, "offset": 0}
        )
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["summary"]["next_offset"] == 10
        assert structured["summary"]["truncated"] is True
        assert any(c["code"] == "more_pages_available" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_query_parcels_last_page_has_null_next_offset(self, plugin):
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 2}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)] * 2}),
            ],
        )
        result = await run_tool(plugin, "query_parcels", {"where": "1=1", "limit": 10})
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["summary"]["next_offset"] is None

    @pytest.mark.asyncio
    async def test_query_parcels_empty(self, plugin):
        install_client(
            plugin, routes=[(is_count, {"count": 0})], default={"features": []}
        )
        result = await run_tool(
            plugin, "query_parcels", {"where": "Zoning_District = 'NOPE'"}
        )
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["rows"] == []
        assert structured["summary"]["total_count"] == 0
        assert any(c["code"] == "no_results" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_parcel_stats_grouped_raw_values(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {
                        "features": [
                            feat(GIS_Category="Parcel", count_Parcel_ID=98348),
                            feat(GIS_Category="Lease", count_Parcel_ID=596),
                        ]
                    },
                )
            ],
        )
        result = await run_tool(
            plugin,
            "parcel_stats",
            {
                "stat_type": "count",
                "stat_field": "Parcel_ID",
                "group_by": "GIS_Category",
                "category": "All",
            },
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        # RAW values: the prose says "98,348", the structure says 98348.
        assert structured["rows"][0] == {"group": "Parcel", "value": 98348}
        assert structured["summary"]["group_count"] == 2

    @pytest.mark.asyncio
    async def test_parcel_stats_ungrouped_has_null_group(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {"features": [feat(avg_Appraised_Total_Value=425000.5)]},
                )
            ],
        )
        result = await run_tool(
            plugin,
            "parcel_stats",
            {"stat_type": "avg", "stat_field": "Appraised_Total_Value"},
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["rows"] == [{"group": None, "value": 425000.5}]

    @pytest.mark.asyncio
    async def test_parcel_stats_null_value_is_not_zero(self, plugin):
        """avg over no non-null values comes back null, and null is NOT
        zero -- a schema requiring a number here would be violated."""
        install_client(
            plugin,
            routes=[
                (is_feature_query, {"features": [feat(avg_Appraised_Total_Value=None)]})
            ],
        )
        result = await run_tool(
            plugin,
            "parcel_stats",
            {"stat_type": "avg", "stat_field": "Appraised_Total_Value"},
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["rows"] == [{"group": None, "value": None}]

    @pytest.mark.asyncio
    async def test_parcel_stats_string_value_from_text_field(self, plugin):
        """min/max over a TEXT field returns a string, not a number."""
        install_client(
            plugin,
            routes=[
                (is_feature_query, {"features": [feat(max_Owner_Name="ZZZ HOLDINGS")]})
            ],
        )
        result = await run_tool(
            plugin, "parcel_stats", {"stat_type": "max", "stat_field": "Owner_Name"}
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["rows"] == [{"group": None, "value": "ZZZ HOLDINGS"}]

    @pytest.mark.asyncio
    async def test_parcel_stats_numeric_group_key(self, plugin):
        """Grouping on a numeric field yields numeric group keys."""
        install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {"features": [feat(YearBuilt=1962, count_Parcel_ID=12)]},
                )
            ],
        )
        result = await run_tool(
            plugin,
            "parcel_stats",
            {
                "stat_type": "count",
                "stat_field": "Parcel_ID",
                "group_by": "YearBuilt",
            },
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["rows"] == [{"group": 1962, "value": 12}]

    @pytest.mark.asyncio
    async def test_parcel_stats_empty(self, plugin):
        install_client(plugin, default={"features": []})
        result = await run_tool(
            plugin,
            "parcel_stats",
            {"stat_type": "sum", "stat_field": "Appraised_Total_Value"},
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["rows"] == []
        assert structured["summary"]["group_count"] == 0
        assert any(c["code"] == "no_results" for c in structured["caveats"])

    @pytest.mark.asyncio
    async def test_date_fields_decode_map_is_present(self, plugin):
        """Structured rows keep RAW epoch-ms dates; the decode map names
        which fields those are."""
        install_client(
            plugin,
            routes=[
                (
                    is_feature_query,
                    {"features": [feat(Parcel_ID="1", Deed_Date=1609459200000)]},
                )
            ],
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "002-151-32"})
        structured = assert_conforms(plugin, "find_parcel", result)
        assert "Deed_Date" in structured["summary"]["date_fields_epoch_ms"]
        # Raw in the structure...
        assert structured["rows"][0]["Deed_Date"] == 1609459200000
        # ...ISO in the prose.
        assert "2021-01-01" in result.content[0]["text"]


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_unknown_tool(self, plugin):
        result = await plugin.execute_tool("bogus_tool", {})
        assert result.success is False
        assert "Unknown tool" in result.error_message

    @pytest.mark.asyncio
    async def test_transient_500_retried_then_succeeds(self, plugin):
        responses = [
            make_response({"error": {"code": 500, "message": ""}}),
            make_response({"features": [feat(**SAMPLE_RECORD)]}),
        ]
        client = Mock()
        client.get = AsyncMock(side_effect=responses)
        plugin.client = client
        plugin.ARCGIS_RETRY_BACKOFF_S = 0
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "00215132000"})
        assert result.success is True
        assert client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_invalid_field_error_rewritten_with_suggestion(self, plugin):
        payload = {
            "error": {
                "code": 400,
                "message": "Invalid field: Zoning_Distric",
                "details": [],
            }
        }
        install_client(plugin, routes=[(is_feature_query, payload)])
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "00215132000"})
        assert result.success is False
        assert "does not exist on the property layer" in result.error_message
        assert "Zoning_District" in result.error_message
        assert "CASE-SENSITIVE" in result.error_message

    @pytest.mark.asyncio
    async def test_invalid_query_parameters_error_gets_hints(self, plugin):
        payload = {
            "error": {
                "code": 400,
                "message": "Cannot perform query. Invalid query parameters.",
                "details": ["'Invalid query parameters'"],
            }
        }
        install_client(plugin, routes=[(is_feature_query, payload)])
        result = await run_tool(plugin, "query_parcels", {"where": "Lot_Size > 5000"})
        assert result.success is False
        assert "Likely cause" in result.error_message

    @pytest.mark.asyncio
    async def test_http_4xx_is_immediate_error(self, plugin):
        client = Mock()
        client.get = AsyncMock(return_value=make_response({"nope": True}, status=404))
        plugin.client = client
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "00215132000"})
        assert result.success is False
        assert "HTTP 404" in result.error_message
        assert client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_success_response_carries_retrieved_stamp(self, plugin):
        install_client(
            plugin,
            routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})],
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "00215132000"})
        assert "Retrieved:" in tool_text(result)

    @pytest.mark.asyncio
    async def test_health_check(self, plugin):
        client = Mock()
        client.get = AsyncMock(return_value=make_response({"name": "x"}))
        plugin.client = client
        assert await plugin.health_check() is True
        client.get = AsyncMock(side_effect=RuntimeError("down"))
        assert await plugin.health_check() is False

    @pytest.mark.asyncio
    async def test_shutdown_closes_client(self, plugin):
        client = Mock()
        client.aclose = AsyncMock()
        plugin.client = client
        await plugin.shutdown()
        client.aclose.assert_awaited_once()
        assert plugin.is_initialized is False
