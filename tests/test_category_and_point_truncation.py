"""Two lookups that answered "nothing" or "that is all" when neither was true.

find_parcel filters to category 'Parcel' unless told otherwise. A Lease
or Economic record has its own number, so `01030203001` -- a Lease --
came back "no exact match" to a caller who had the right number.

parcels_at_point returns at most 20 records. A condominium stacks one
polygon per unit on the same ground (207 at one complex), and the first
20 came back as "20 record(s) contain point" with nothing to say there
were more.
"""

import pytest

from tests.test_anchorage_parcels_plugin import (
    SAMPLE_RECORD,
    assert_conforms,
    codes,
    feat,
    install_client,
    is_count,
    is_feature_query,
    plugin,  # noqa: F401  (fixture)
    run_tool,
    tool_text,
)

LEASE = dict(
    SAMPLE_RECORD,
    Parcel_ID="01030203001",
    GIS_Category="Lease",
    Owner_Name="A LESSEE",
)


def _in_category(category):
    def predicate(url, params):
        return is_feature_query(url, params) and (
            f"GIS_Category = '{category}'" in params.get("where", "")
        )

    return predicate


def _any_category(url, params):
    return is_feature_query(url, params) and "GIS_Category" not in params.get(
        "where", ""
    )


class TestFindParcelLooksPastTheDefaultCategory:
    @pytest.mark.asyncio
    async def test_a_lease_number_is_found_and_labelled(self, plugin):  # noqa: F811
        calls = install_client(
            plugin,
            routes=[
                (_in_category("Parcel"), {"features": []}),
                (_any_category, {"features": [feat(**LEASE)]}),
            ],
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "01030203001"})
        structured = assert_conforms(plugin, "find_parcel", result)
        assert structured["summary"]["returned_count"] == 1
        assert structured["summary"]["exact_match"] is True
        assert structured["rows"][0]["Parcel_ID"] == "01030203001"
        assert "other_category_match" in codes(structured)
        assert "GIS_Category" not in structured["query"]["where"]
        text = tool_text(result)
        assert "no exact match" not in text
        assert "NOT A 'Parcel' RECORD" in text
        assert "'Lease'" in text
        # The number itself in every category is tried BEFORE the base
        # number in the default one: two queries, no LIKE search.
        wheres = [c[1]["where"] for c in calls]
        assert len(wheres) == 2
        assert "01030203001" in wheres[1] and "LIKE" not in wheres[1]

    @pytest.mark.asyncio
    async def test_a_parcel_match_is_not_widened(self, plugin):  # noqa: F811
        calls = install_client(
            plugin, routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})]
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "00215132000"})
        structured = assert_conforms(plugin, "find_parcel", result)
        assert "other_category_match" not in codes(structured)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_an_explicit_category_is_widened_and_named(self, plugin):  # noqa: F811
        install_client(
            plugin,
            routes=[
                (_in_category("Economic"), {"features": []}),
                (_any_category, {"features": [feat(**LEASE)]}),
            ],
        )
        result = await run_tool(
            plugin,
            "find_parcel",
            {"parcel_id": "01030203001", "category": "Economic"},
        )
        structured = assert_conforms(plugin, "find_parcel", result)
        assert "other_category_match" in codes(structured)
        text = tool_text(result)
        assert "NOT A 'Economic' RECORD" in text
        assert "(the default category)" not in text

    @pytest.mark.asyncio
    async def test_category_all_has_nothing_to_widen(self, plugin):  # noqa: F811
        calls = install_client(plugin, routes=[(is_feature_query, {"features": []})])
        result = await run_tool(
            plugin, "find_parcel", {"parcel_id": "010-302-03", "category": "All"}
        )
        assert "no exact match" in tool_text(result)
        # One lookup, one fuzzy search.
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_base_number_fallback_also_looks_in_every_category(self, plugin):  # noqa: F811
        """Sub-parcel 009 does not exist anywhere; the base number does,
        as a Lease only."""

        def base_any_category(url, params):
            where = params.get("where", "")
            return _any_category(url, params) and "'01030203'" in where

        install_client(
            plugin,
            routes=[
                (base_any_category, {"features": [feat(**LEASE)]}),
                (is_feature_query, {"features": []}),
            ],
        )
        result = await run_tool(plugin, "find_parcel", {"parcel_id": "01030203009"})
        structured = assert_conforms(plugin, "find_parcel", result)
        assert structured["summary"]["exact_match"] is False
        assert {"base_number_match", "other_category_match"} <= set(codes(structured))


def _stack(n):
    return [
        feat(**dict(SAMPLE_RECORD, Parcel_ID=f"012182040{i:02d}")) for i in range(n)
    ]


class TestPointLookupsSayWhenThereIsMore:
    @pytest.mark.asyncio
    async def test_a_full_page_reports_the_total(self, plugin):  # noqa: F811
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 207}),
                (
                    is_feature_query,
                    {"features": _stack(plugin.POINT_QUERY_LIMIT)},
                ),
            ],
        )
        result = await run_tool(
            plugin, "parcels_at_point", {"lat": 61.2091, "lon": -149.8944}
        )
        structured = assert_conforms(plugin, "parcels_at_point", result)
        summary = structured["summary"]
        assert summary["returned_count"] == 20
        assert summary["total_count"] == 207
        assert summary["truncated"] is True
        assert codes(structured)[0] == "results_truncated"
        text = tool_text(result)
        assert "20 of 207 record(s) contain point" in text
        assert "of the records SHOWN" in text
        # The count is of the same point, not of the whole layer.
        count_params = next(c[1] for c in calls if is_count(*c))
        assert count_params["geometry"] == "-149.8944,61.2091"
        assert count_params["spatialRel"] == "esriSpatialRelIntersects"

    @pytest.mark.asyncio
    async def test_a_short_page_is_complete_and_not_counted(self, plugin):  # noqa: F811
        calls = install_client(
            plugin, routes=[(is_feature_query, {"features": _stack(3)})]
        )
        result = await run_tool(
            plugin, "parcels_at_point", {"lat": 61.2091, "lon": -149.8944}
        )
        structured = assert_conforms(plugin, "parcels_at_point", result)
        assert structured["summary"]["total_count"] == 3
        assert structured["summary"]["truncated"] is False
        assert "results_truncated" not in codes(structured)
        assert not any(is_count(*c) for c in calls)

    @pytest.mark.asyncio
    async def test_exactly_the_limit_is_not_called_truncated(self, plugin):  # noqa: F811
        install_client(
            plugin,
            routes=[
                (is_count, {"count": plugin.POINT_QUERY_LIMIT}),
                (
                    is_feature_query,
                    {"features": _stack(plugin.POINT_QUERY_LIMIT)},
                ),
            ],
        )
        result = await run_tool(
            plugin, "parcels_at_point", {"lat": 61.2091, "lon": -149.8944}
        )
        structured = assert_conforms(plugin, "parcels_at_point", result)
        assert structured["summary"]["truncated"] is False

    @pytest.mark.asyncio
    async def test_a_failed_count_still_says_more_may_exist(self, plugin):  # noqa: F811
        install_client(
            plugin,
            routes=[
                (is_count, RuntimeError("count down")),
                (
                    is_feature_query,
                    {"features": _stack(plugin.POINT_QUERY_LIMIT)},
                ),
            ],
        )
        result = await run_tool(
            plugin, "parcels_at_point", {"lat": 61.2091, "lon": -149.8944}
        )
        structured = assert_conforms(plugin, "parcels_at_point", result)
        assert structured["summary"]["total_count"] is None
        assert structured["summary"]["truncated"] is True
        assert "MORE EXIST" in tool_text(result)
