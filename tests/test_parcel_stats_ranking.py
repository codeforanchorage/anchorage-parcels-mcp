"""parcel_stats can rank groups and page through them.

Grouped statistics came back in group_by order, one page of 2,000 at
most. With ~82,000 distinct owner names that made "which owners hold the
most parcels" unanswerable: the page was the first 2,000 names
alphabetically, and there was no way to ask for another or for the
largest. The layer can order groups by the statistic and page them
(verified live 2026-11-20), so the tool now passes that through.
"""

import pytest

from plugins.anchorage_parcels.config_schema import AnchorageParcelsPluginConfig
from plugins.anchorage_parcels.plugin import AnchorageParcelsPlugin
from tests.test_anchorage_parcels_plugin import (
    SNAPSHOT_FIELDS,
    assert_conforms,
    codes,
    feat,
    install_client,
    is_count,
    run_tool,
    tool_text,
)

BY_OWNER = {"stat_type": "count", "stat_field": "Parcel_ID", "group_by": "Owner_Name"}
TOP = [
    ("MUNICIPALITY OF ANCHORAGE PARKS & RECREATION", 1097),
    ("PROPERTY OWNER OF RECORD", 501),
    ("EKLUTNA INC", 467),
]


def is_stats(url, params):
    return "outStatistics" in params


def stats_payload(pairs, exceeded):
    payload = {
        "features": [feat(Owner_Name=name, count_Parcel_ID=n) for name, n in pairs]
    }
    if exceeded:
        payload["exceededTransferLimit"] = True
    return payload


def stats_params(calls):
    return [p for u, p in calls if is_stats(u, p)][-1]


@pytest.fixture
def plugin():
    p = AnchorageParcelsPlugin({})
    p.plugin_config = AnchorageParcelsPluginConfig()
    p._live_fields = set(SNAPSHOT_FIELDS)
    p._layer_meta_due = float("inf")
    p._initialized = True
    return p


class TestRanking:
    @pytest.mark.asyncio
    async def test_top_n_orders_by_the_statistic(self, plugin):
        calls = install_client(
            plugin,
            routes=[(is_stats, stats_payload(TOP, True)), (is_count, {"count": 82382})],
        )
        result = await run_tool(
            plugin, "parcel_stats", {**BY_OWNER, "order_by": "value_desc", "limit": 3}
        )
        params = stats_params(calls)
        # The group breaks ties, so equal values keep one order across pages.
        assert params["orderByFields"] == "count_Parcel_ID DESC,Owner_Name ASC"
        assert params["resultRecordCount"] == "3"
        assert "resultOffset" not in params

        structured = assert_conforms(plugin, "parcel_stats", result)
        assert [r["group"] for r in structured["rows"]] == [name for name, _ in TOP]
        assert structured["query"]["order_by"] == "value_desc"
        assert structured["summary"]["truncated"] is True
        assert structured["summary"]["next_offset"] == 3
        assert structured["summary"]["total_group_count"] == 82382
        # A cut ranking is still a ranking: it is not reported as the
        # alphabetical truncation that could not be trusted.
        assert "more_pages_available" in codes(structured)
        assert "results_truncated" not in codes(structured)
        text = tool_text(result)
        assert "ranks 1-3 of about 82,382 groups, largest count first" in text
        assert "offset=3" in text
        assert "largest first" in text

    @pytest.mark.asyncio
    async def test_smallest_first(self, plugin):
        calls = install_client(plugin, routes=[(is_stats, stats_payload(TOP, False))])
        result = await run_tool(
            plugin, "parcel_stats", {**BY_OWNER, "order_by": "value_asc"}
        )
        assert stats_params(calls)["orderByFields"] == (
            "count_Parcel_ID ASC,Owner_Name ASC"
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["summary"]["truncated"] is False
        assert structured["summary"]["next_offset"] is None
        assert structured["summary"]["total_group_count"] == 3
        assert "smallest first" in tool_text(result)

    @pytest.mark.asyncio
    async def test_default_order_is_unchanged(self, plugin):
        calls = install_client(plugin, routes=[(is_stats, stats_payload(TOP, False))])
        result = await run_tool(plugin, "parcel_stats", dict(BY_OWNER))
        params = stats_params(calls)
        assert params["orderByFields"] == "Owner_Name"
        assert params["resultRecordCount"] == "2000"
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["query"]["order_by"] == "group"
        assert "groups are ordered by Owner_Name" in tool_text(result)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_alphabetical_truncation_points_at_the_ranking(self, plugin):
        install_client(
            plugin,
            routes=[(is_stats, stats_payload(TOP, True)), (is_count, {"count": 82382})],
        )
        result = await run_tool(plugin, "parcel_stats", {**BY_OWNER, "limit": 3})
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert "results_truncated" in codes(structured)
        text = tool_text(result)
        assert "NOT the largest" in text
        assert "order_by='value_desc'" in text
        assert "offset=3" in text


class TestPaging:
    @pytest.mark.asyncio
    async def test_a_later_page(self, plugin):
        calls = install_client(
            plugin,
            routes=[(is_stats, stats_payload(TOP, True)), (is_count, {"count": 82382})],
        )
        result = await run_tool(
            plugin,
            "parcel_stats",
            {**BY_OWNER, "order_by": "value_desc", "limit": 3, "offset": 3},
        )
        assert stats_params(calls)["resultOffset"] == "3"
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["query"]["offset"] == 3
        assert structured["summary"]["next_offset"] == 6
        assert "ranks 4-6 of about 82,382 groups" in tool_text(result)

    @pytest.mark.asyncio
    async def test_the_last_page_knows_the_total(self, plugin):
        """The distinct count leaves out the null group, so it can be one
        short; the total is never reported below what was seen."""
        install_client(
            plugin,
            routes=[(is_stats, stats_payload(TOP, False)), (is_count, {"count": 132})],
        )
        result = await run_tool(plugin, "parcel_stats", {**BY_OWNER, "offset": 130})
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["summary"]["truncated"] is False
        assert structured["summary"]["next_offset"] is None
        assert structured["summary"]["total_group_count"] == 133
        # Nothing about truncation; the owner-spelling note is separate.
        assert codes(structured) == ["owner_names_not_normalized"]

    @pytest.mark.asyncio
    async def test_an_offset_past_the_end_is_not_no_matching_records(self, plugin):
        install_client(
            plugin,
            routes=[(is_stats, {"features": []}), (is_count, {"count": 134})],
        )
        result = await run_tool(plugin, "parcel_stats", {**BY_OWNER, "offset": 500})
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["summary"]["group_count"] == 0
        assert structured["summary"]["total_group_count"] == 134
        text = tool_text(result)
        assert "No groups at offset=500: there are only 134" in text
        assert "0 matching records" not in text

    @pytest.mark.asyncio
    async def test_no_matching_records_still_says_so(self, plugin):
        install_client(plugin, routes=[(is_stats, {"features": []})])
        result = await run_tool(plugin, "parcel_stats", dict(BY_OWNER))
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["summary"]["total_group_count"] == 0
        assert "0 matching records" in tool_text(result)

    @pytest.mark.asyncio
    async def test_limit_above_the_page_size_is_clamped_and_said(self, plugin):
        calls = install_client(plugin, routes=[(is_stats, stats_payload(TOP, False))])
        result = await run_tool(plugin, "parcel_stats", {**BY_OWNER, "limit": 50000})
        assert stats_params(calls)["resultRecordCount"] == "2000"
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert "limit_clamped" in codes(structured)


class TestArguments:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra, named",
        [
            ({"order_by": "value_desc"}, "order_by"),
            ({"limit": 10}, "limit"),
            ({"offset": 5}, "offset"),
        ],
    )
    async def test_ranking_arguments_need_group_by(self, plugin, extra, named):
        """Refused, not ignored: the caller meant to group."""
        calls = install_client(plugin)
        result = await run_tool(
            plugin,
            "parcel_stats",
            {"stat_type": "count", "stat_field": "Parcel_ID", **extra},
        )
        assert result.success is False
        assert named in result.error_message
        assert "group_by" in result.error_message
        assert calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "extra, named",
        [
            ({"order_by": "biggest"}, "order_by"),
            ({"limit": "ten"}, "limit"),
            ({"offset": "x"}, "offset"),
        ],
    )
    async def test_bad_values_are_caller_errors(self, plugin, extra, named):
        calls = install_client(plugin)
        result = await run_tool(plugin, "parcel_stats", {**BY_OWNER, **extra})
        assert result.success is False
        assert named in result.error_message
        assert calls == []

    @pytest.mark.asyncio
    async def test_ungrouped_statistic_sends_no_paging(self, plugin):
        calls = install_client(
            plugin, routes=[(is_stats, {"features": [feat(count_Parcel_ID=97368)]})]
        )
        result = await run_tool(
            plugin, "parcel_stats", {"stat_type": "count", "stat_field": "Parcel_ID"}
        )
        params = stats_params(calls)
        assert "resultRecordCount" not in params and "orderByFields" not in params
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert structured["query"]["order_by"] is None
        assert structured["summary"]["next_offset"] is None

    def test_the_schema_advertises_the_ranking(self, plugin):
        tool = {t.name: t for t in plugin.get_tools()}["parcel_stats"]
        props = tool.input_schema["properties"]
        assert props["order_by"]["enum"] == ["group", "value_desc", "value_asc"]
        assert props["limit"]["maximum"] == plugin.SERVER_PAGE_SIZE
        assert "order_by='value_desc', limit=10" in tool.description
