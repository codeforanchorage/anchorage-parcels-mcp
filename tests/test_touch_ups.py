"""Small corrections made before the server was shared more widely.

None changes what a correct call returns. Each fixes a place where the
response, the schema or the defaults said something that was not so:

- the last page of ``query_parcels`` was "TRUNCATED ... a SAMPLE";
- ``search_by_owner`` showed a full page as the whole answer when the
  count could not be fetched;
- the address-point fallback printed the caller's limit beside a result
  capped by a different one;
- grouping by ``Zoning_District`` listed 'B2C' and 'B-2C' apart, unsaid;
- a limit below 1 became 1 without a word;
- the geometry-only records were described as having a null parcel
  number (it is the empty string);
- the default plugin timeout (30s) was above the Lambda's (28s).
"""

from pathlib import Path

import pytest
import yaml

from plugins.anchorage_parcels.config_schema import AnchorageParcelsPluginConfig
from plugins.anchorage_parcels.plugin import AnchorageParcelsPlugin
from tests.test_anchorage_parcels_plugin import (
    SAMPLE_RECORD,
    SNAPSHOT_FIELDS,
    assert_conforms,
    codes,
    feat,
    install_client,
    is_count,
    is_feature_query,
    run_tool,
    tool_text,
)

REPO = Path(__file__).resolve().parent.parent
API_GATEWAY_TIMEOUT_S = 29


def is_stats(url, params):
    return "outStatistics" in params


def page(n, exceeded=False):
    payload = {
        "features": [
            feat(**{**SAMPLE_RECORD, "Parcel_ID": f"{i:011d}"}) for i in range(n)
        ]
    }
    if exceeded:
        payload["exceededTransferLimit"] = True
    return payload


@pytest.fixture
def plugin():
    p = AnchorageParcelsPlugin({})
    p.plugin_config = AnchorageParcelsPluginConfig()
    p._live_fields = set(SNAPSHOT_FIELDS)
    p._layer_meta_due = float("inf")
    p._initialized = True
    return p


async def _no_count(where):
    return None


class TestQueryParcelsLastPage:
    @pytest.mark.asyncio
    async def test_the_last_page_is_not_a_sample(self, plugin):
        install_client(
            plugin,
            routes=[(is_count, {"count": 1050}), (is_feature_query, page(50))],
        )
        result = await run_tool(
            plugin, "query_parcels", {"where": "1=1", "limit": 1000, "offset": 1000}
        )
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["summary"]["truncated"] is False
        assert structured["summary"]["next_offset"] is None
        assert structured["summary"]["total_count"] == 1050
        assert codes(structured) == []
        text = tool_text(result)
        assert "TRUNCATED" not in text and "SAMPLE" not in text
        # The total is still stated.
        assert "50 of 1,050 total" in text

    @pytest.mark.asyncio
    async def test_a_middle_page_still_is(self, plugin):
        install_client(
            plugin,
            routes=[(is_count, {"count": 1050}), (is_feature_query, page(100, True))],
        )
        result = await run_tool(
            plugin, "query_parcels", {"where": "1=1", "limit": 100, "offset": 100}
        )
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["summary"]["truncated"] is True
        assert structured["summary"]["next_offset"] == 200
        assert codes(structured) == ["results_truncated", "more_pages_available"]

    @pytest.mark.asyncio
    async def test_truncated_and_next_offset_agree_without_a_count(self, plugin):
        install_client(plugin, routes=[(is_feature_query, page(100, True))])
        plugin._fetch_count = _no_count
        result = await run_tool(plugin, "query_parcels", {"where": "1=1", "limit": 100})
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["summary"]["total_count"] is None
        assert structured["summary"]["truncated"] is True
        assert structured["summary"]["next_offset"] == 100


class TestOwnerSearchWithoutACount:
    @pytest.mark.asyncio
    async def test_a_full_page_says_more_exist(self, plugin):
        install_client(plugin, routes=[(is_feature_query, page(20, True))])
        plugin._fetch_count = _no_count
        result = await run_tool(plugin, "search_by_owner", {"name": "smith"})
        structured = assert_conforms(plugin, "search_by_owner", result)
        assert structured["summary"]["total_count"] is None
        assert structured["summary"]["truncated"] is True
        assert codes(structured) == ["results_truncated"]
        text = tool_text(result)
        assert "MORE EXIST" in text
        assert "the total could not be counted" in text

    @pytest.mark.asyncio
    async def test_a_short_page_is_the_whole_answer(self, plugin):
        install_client(plugin, routes=[(is_feature_query, page(3))])
        plugin._fetch_count = _no_count
        result = await run_tool(plugin, "search_by_owner", {"name": "smith"})
        structured = assert_conforms(plugin, "search_by_owner", result)
        assert structured["summary"]["truncated"] is False
        assert codes(structured) == []

    @pytest.mark.asyncio
    async def test_a_counted_page_reads_as_before(self, plugin):
        install_client(
            plugin,
            routes=[(is_count, {"count": 364}), (is_feature_query, page(20, True))],
        )
        result = await run_tool(plugin, "search_by_owner", {"name": "smith"})
        structured = assert_conforms(plugin, "search_by_owner", result)
        assert structured["summary"]["truncated"] is True
        assert "returned 20 of 364 matching records" in tool_text(result)


class TestAddressPointFallbackLimit:
    @pytest.mark.asyncio
    async def test_the_limit_printed_is_the_one_that_applied(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    lambda u, p: p.get("returnGeometry") == "true",
                    {
                        "features": [
                            {
                                "attributes": {"FULL_ADDRESS": "4640 Reka Dr"},
                                "geometry": {"x": -149.79, "y": 61.2},
                            }
                        ]
                    },
                ),
                (is_count, {"count": 15}),
                (lambda u, p: p.get("geometryType") == "esriGeometryPoint", page(15)),
            ],
        )
        result = await run_tool(
            plugin, "search_by_address", {"address": "4640 Reka", "limit": 10}
        )
        structured = assert_conforms(plugin, "search_by_address", result)
        assert structured["summary"]["match_method"] == "address_point"
        assert structured["summary"]["returned_count"] == 15
        text = tool_text(result)
        assert "Returned 15 record(s) (limit: 20)" in text
        assert "(limit: 10)" not in text

    def test_the_schema_says_which_limit_is_which(self, plugin):
        tool = {t.name: t for t in plugin.get_tools()}["search_by_address"]
        assert "up to 20" in tool.input_schema["properties"]["limit"]["description"]


class TestZoningGroupsUnderTwoSpellings:
    BY_ZONING = {
        "stat_type": "count",
        "stat_field": "Parcel_ID",
        "group_by": "Zoning_District",
    }

    async def _run(self, plugin, groups):
        install_client(
            plugin,
            routes=[
                (
                    is_stats,
                    {
                        "features": [
                            feat(Zoning_District=z, count_Parcel_ID=n)
                            for z, n in groups
                        ]
                    },
                )
            ],
        )
        result = await run_tool(plugin, "parcel_stats", dict(self.BY_ZONING))
        return assert_conforms(plugin, "parcel_stats", result), tool_text(result)

    @pytest.mark.asyncio
    async def test_twins_are_named(self, plugin):
        structured, text = await self._run(
            plugin,
            [("B-2C", 1), ("B2C", 651), ("I-2", 2), ("I2", 522), ("RO", 760)],
        )
        assert codes(structured) == ["zoning_variants_split"]
        message = structured["caveats"][0]["message"]
        assert "'B-2C' and 'B2C'; 'I-2' and 'I2'" in message
        assert "RO" not in message
        assert message in text
        # The groups themselves are left as the layer has them.
        assert [r["group"] for r in structured["rows"]] == [
            "B-2C",
            "B2C",
            "I-2",
            "I2",
            "RO",
        ]

    @pytest.mark.asyncio
    async def test_no_twins_no_note(self, plugin):
        structured, _ = await self._run(plugin, [("B2C", 652), ("RO", 760), (None, 4)])
        assert codes(structured) == []

    @pytest.mark.asyncio
    async def test_another_grouping_is_not_examined(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    is_stats,
                    {
                        "features": [
                            feat(Land_Use=v, count_Parcel_ID=1) for v in ("A-B", "AB")
                        ]
                    },
                )
            ],
        )
        result = await run_tool(
            plugin, "parcel_stats", {**self.BY_ZONING, "group_by": "Land_Use"}
        )
        assert codes(assert_conforms(plugin, "parcel_stats", result)) == []


class TestLimitBelowOne:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, -5])
    async def test_it_is_said(self, plugin, limit):
        calls = install_client(
            plugin, routes=[(is_count, {"count": 9}), (is_feature_query, page(1, True))]
        )
        result = await run_tool(
            plugin, "query_parcels", {"where": "1=1", "limit": limit}
        )
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["query"]["limit"] == 1
        assert "limit_clamped" in codes(structured)
        assert f"requested limit={limit} is below the minimum of 1" in tool_text(result)
        sent = [p for u, p in calls if is_feature_query(u, p)][0]
        assert sent["resultRecordCount"] == "1"


class TestWording:
    def test_shells_are_not_described_as_null_numbered(self, plugin):
        """`Parcel_ID IS NULL` finds none of them: it is ''."""
        for tool in plugin.get_tools():
            flag = tool.input_schema["properties"].get("include_unassessed")
            if flag:
                assert "null parcel number" not in flag["description"]
                assert "empty string" in flag["description"]
        assert "null parcel number" not in plugin._unassessed_caveat(True)

    def test_parcel_number_lengths_agree_with_what_is_accepted(self, plugin):
        tools = {t.name: t for t in plugin.get_tools()}
        for name in ("find_parcel", "get_parcel_details"):
            text = tools[name].input_schema["properties"]["parcel_id"]["description"]
            assert "leading zeros optional" in text
            assert "8 or 11 digits)" not in text

    def test_address_search_says_what_it_matches(self, plugin):
        text = {t.name: t for t in plugin.get_tools()}["search_by_address"].description
        assert "must start a word" in text
        assert "EVERY category" in text


class TestTimeoutDefaults:
    """A deployment that leaves a timeout out must still have the ladder
    in order: plugin < Lambda < API Gateway's fixed 29s."""

    def test_the_plugin_default_is_under_the_lambda(self):
        tracked = yaml.safe_load(
            (REPO / "config-anchorage-parcels.yaml").read_text(encoding="utf-8")
        )
        default = AnchorageParcelsPluginConfig().timeout
        assert default < tracked["aws"]["lambda_timeout"] < API_GATEWAY_TIMEOUT_S

    def test_the_example_config_keeps_the_ladder(self):
        example = yaml.safe_load(
            (REPO / "config-example.yaml").read_text(encoding="utf-8")
        )
        plugin_timeout = example["plugins"]["anchorage_parcels"]["timeout"]
        lambda_timeout = example["aws"]["lambda_timeout"]
        assert plugin_timeout < lambda_timeout < API_GATEWAY_TIMEOUT_S
