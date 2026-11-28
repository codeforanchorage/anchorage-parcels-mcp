"""Four small faults found reading the server before it was shared more widely.

Each was reproduced on the live endpoint on 2026-11-28:

- ``include_unassessed="false"`` (a string) switched the geometry-only
  records ON, because the argument was read with ``bool()``;
- ``parcel_stats`` refused ``offset=0`` on an ungrouped statistic, the
  default its own schema advertises;
- ``query_parcels`` with an offset past the last record said "No records
  matched the WHERE clause" beside a total_count of 760;
- ``Zoning_District = 'ro'`` was corrected to the stored 'RO', then
  described as a hyphenation twin that had been folded in.
"""

import pytest

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
    is_distinct_query,
    is_feature_query,
    run_tool,
    tool_text,
)

ASSESSED = "Parcel_ID IS NOT NULL"
UNGROUPED = {"stat_type": "count", "stat_field": "Parcel_ID"}


def is_stats(url, params):
    return "outStatistics" in params


@pytest.fixture
def plugin():
    p = AnchorageParcelsPlugin({})
    p.plugin_config = AnchorageParcelsPluginConfig()
    p._live_fields = set(SNAPSHOT_FIELDS)
    p._layer_meta_due = float("inf")
    p._initialized = True
    return p


class TestIncludeUnassessedIsReadAsWritten:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "False", " FALSE ", "0", "no", 0, ""])
    async def test_a_false_spelling_keeps_the_filter(self, plugin, value):
        calls = install_client(
            plugin, routes=[(is_stats, {"features": [feat(count_Parcel_ID=97368)]})]
        )
        result = await run_tool(
            plugin, "parcel_stats", {**UNGROUPED, "include_unassessed": value}
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["query"]["include_unassessed"] is False
        assert ASSESSED in calls[-1][1]["where"]
        assert "unassessed_included" not in codes(structured)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [True, "true", "TRUE", "1", 1])
    async def test_a_true_spelling_drops_it(self, plugin, value):
        calls = install_client(
            plugin, routes=[(is_stats, {"features": [feat(count_Parcel_ID=98413)]})]
        )
        result = await run_tool(
            plugin, "parcel_stats", {**UNGROUPED, "include_unassessed": value}
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["query"]["include_unassessed"] is True
        assert ASSESSED not in calls[-1][1]["where"]
        assert "unassessed_included" in codes(structured)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool, args",
        [
            ("search_by_owner", {"name": "smith"}),
            ("search_by_address", {"address": "144 W 15th"}),
            ("parcels_at_point", {"lat": 61.2, "lon": -149.9}),
            ("query_parcels", {"where": "1=1"}),
            ("parcel_stats", dict(UNGROUPED)),
        ],
    )
    @pytest.mark.parametrize("value", ["maybe", 2, ["true"]])
    async def test_anything_else_is_a_caller_error(self, plugin, tool, args, value):
        calls = install_client(plugin)
        result = await run_tool(plugin, tool, {**args, "include_unassessed": value})
        assert result.success is False
        assert "include_unassessed must be true or false" in result.error_message
        assert calls == []


class TestUngroupedStatisticAcceptsTheDefaults:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra",
        [
            {"offset": 0},
            {"limit": 2000},
            {"order_by": "group"},
            {"offset": 0, "limit": 2000, "order_by": "group"},
            # Above the page size is the page size: still nothing to page.
            {"limit": 50000},
        ],
    )
    async def test_the_advertised_defaults_are_not_refused(self, plugin, extra):
        calls = install_client(
            plugin, routes=[(is_stats, {"features": [feat(count_Parcel_ID=97368)]})]
        )
        result = await run_tool(plugin, "parcel_stats", {**UNGROUPED, **extra})
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["rows"] == [{"group": None, "value": 97368}]
        params = calls[-1][1]
        assert "resultRecordCount" not in params and "resultOffset" not in params
        # No "limit clamped" about a limit that applies to nothing.
        assert codes(structured) == []

    def test_the_defaults_tested_are_the_ones_advertised(self, plugin):
        tool = {t.name: t for t in plugin.get_tools()}["parcel_stats"]
        props = tool.input_schema["properties"]
        assert props["offset"]["default"] == 0
        assert props["limit"]["default"] == 2000
        assert props["order_by"]["default"] == "group"


class TestQueryParcelsPastTheLastPage:
    @pytest.mark.asyncio
    async def test_it_says_how_many_there_are(self, plugin):
        install_client(
            plugin,
            routes=[(is_count, {"count": 760}), (is_feature_query, {"features": []})],
        )
        result = await run_tool(
            plugin,
            "query_parcels",
            {"where": "Land_Use = 'Religious'", "offset": 99999},
        )
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["summary"]["total_count"] == 760
        assert structured["summary"]["next_offset"] is None
        assert codes(structured) == ["no_results"]
        text = tool_text(result)
        assert "No records at offset=99,999: only 760 record(s) match" in text
        assert "No records matched the WHERE clause" not in text
        # The clause was fine: no advice on how to repair it.
        assert "If you expected matches" not in text

    @pytest.mark.asyncio
    async def test_an_uncounted_total_is_not_called_zero(self, plugin):
        install_client(plugin, routes=[(is_feature_query, {"features": []})])
        plugin._fetch_count = _no_count
        result = await run_tool(
            plugin, "query_parcels", {"where": "Land_Use = 'Religious'", "offset": 50}
        )
        structured = assert_conforms(plugin, "query_parcels", result)
        assert structured["summary"]["total_count"] is None
        text = tool_text(result)
        assert "No records at offset=50" in text
        assert "could not be counted" in text
        assert "No records matched the WHERE clause" not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("offset", [0, 50])
    async def test_nothing_matching_still_says_so(self, plugin, offset):
        install_client(
            plugin,
            routes=[(is_count, {"count": 0}), (is_feature_query, {"features": []})],
        )
        result = await run_tool(
            plugin,
            "query_parcels",
            {"where": "Land_Use = 'Nonesuch'", "offset": offset},
        )
        assert_conforms(plugin, "query_parcels", result)
        text = tool_text(result)
        assert "No records matched the WHERE clause" in text
        assert "If you expected matches" in text


async def _no_count(where):
    return None


class TestZoningValueThatIsNotAStoredSpelling:
    DISTINCT = {
        "features": [
            {"attributes": {"Zoning_District": v}} for v in ("B2C", "B-2C", "RO", "R1")
        ]
    }

    async def _query(self, plugin, where):
        calls = install_client(
            plugin,
            routes=[
                (is_distinct_query, self.DISTINCT),
                (is_count, {"count": 1}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)]}),
            ],
        )
        result = await run_tool(plugin, "query_parcels", {"where": where})
        sent = [p for _, p in calls if is_feature_query("/query", p)][0]["where"]
        structured = assert_conforms(plugin, "query_parcels", result)
        notes = [
            c["message"]
            for c in structured["caveats"]
            if c["code"] == "zoning_variants_matched"
        ]
        return sent, notes

    @pytest.mark.asyncio
    async def test_a_case_slip_is_called_a_correction(self, plugin):
        sent, notes = await self._query(plugin, "Zoning_District = 'ro'")
        assert "Zoning_District IN ('RO')" in sent
        assert len(notes) == 1
        assert "VALUE ADJUSTED" in notes[0]
        assert "nothing is stored as 'ro'" in notes[0]
        assert "stored spelling 'RO'" in notes[0]
        # It is not a twin, and no records were at risk of being missed.
        assert "also matched" not in notes[0]
        assert "silently missed" not in notes[0]

    @pytest.mark.asyncio
    async def test_a_case_slip_onto_twins_names_both(self, plugin):
        sent, notes = await self._query(plugin, "Zoning_District = 'b2c'")
        assert "Zoning_District IN ('B-2C','B2C')" in sent
        assert len(notes) == 1
        assert "stored spellings 'B-2C', 'B2C'" in notes[0]

    @pytest.mark.asyncio
    async def test_a_stored_spelling_with_a_twin_reads_as_before(self, plugin):
        sent, notes = await self._query(plugin, "Zoning_District = 'B2C'")
        assert "Zoning_District IN ('B-2C','B2C')" in sent
        assert len(notes) == 1
        assert "IS NOT NORMALIZED" in notes[0]
        assert "also matched 'B-2C'" in notes[0]

    @pytest.mark.asyncio
    async def test_a_stored_spelling_alone_is_left_alone(self, plugin):
        sent, notes = await self._query(plugin, "Zoning_District = 'RO'")
        assert "Zoning_District = 'RO'" in sent
        assert notes == []
