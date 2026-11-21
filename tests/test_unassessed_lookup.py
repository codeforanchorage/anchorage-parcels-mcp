"""get_parcel_details and geometry-only records; the owner-grouping caveat.

About 1,070 features carry a parcel number and geometry but no
assessment data (Parcel_ID is ''). Measured live 2026-11-20: 292 base
numbers have one, 88 of those also have assessed records (82 exactly
one), and 27 full numbers are shared by several such features.

get_parcel_details('010-381-30') found one of them beside a Lease record
and asked "which record?", listing a candidate with an empty ID that
could not be passed back.
"""

import pytest

from plugins.anchorage_parcels.config_schema import AnchorageParcelsPluginConfig
from plugins.anchorage_parcels.plugin import AnchorageParcelsPlugin
from tests.test_anchorage_parcels_plugin import (
    ASSESSED,
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

LEASE = dict(
    SAMPLE_RECORD,
    Parcel_ID="01038130001",
    GIS_ParcelNum8Formatted="010-381-30",
    GIS_Category="Lease",
    Parcel_Address="3 UNKNOWN ST",
    Owner_Name="FEDERAL AVIATION ADMIN",
)
SHELL = dict(
    Parcel_ID="",
    GIS_ParcelNum8Formatted="010-381-30",
    GIS_ParcelNum11Formatted="010-381-30-000",
    GIS_Category="Parcel",
    Parcel_Address=None,
    Owner_Name=None,
)


def assessed_only(url, params):
    return is_feature_query(url, params) and ASSESSED in params["where"]


def any_record(url, params):
    return is_feature_query(url, params) and ASSESSED not in params["where"]


@pytest.fixture
def plugin():
    p = AnchorageParcelsPlugin({})
    p.plugin_config = AnchorageParcelsPluginConfig()
    p._live_fields = set(SNAPSHOT_FIELDS)
    p._layer_meta_due = float("inf")
    p._initialized = True
    return p


class TestGeometryOnlyRecords:
    @pytest.mark.asyncio
    async def test_a_shell_beside_one_record_does_not_ask_which(self, plugin):
        """The layer returns only the assessed record, because that is
        all the lookup asks for; the answer is that record."""
        calls = install_client(
            plugin,
            routes=[
                (assessed_only, {"features": [feat(**LEASE)]}),
                (any_record, {"features": [feat(**SHELL), feat(**LEASE)]}),
            ],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "010-381-30"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["summary"]["resolved"] is True
        assert structured["summary"]["match_count"] == 1
        assert structured["result"]["Parcel_ID"] == "01038130001"
        assert structured["candidates"] == []
        assert "multiple_records" not in codes(structured)
        assert "FEDERAL AVIATION ADMIN" in tool_text(result)
        assert len(calls) == 1  # no extra request on the usual path
        assert ASSESSED in structured["query"]["where"]

    @pytest.mark.asyncio
    async def test_candidates_never_include_an_empty_id(self, plugin):
        units = [
            feat(**dict(LEASE, Parcel_ID=f"0103813000{i}", GIS_Category="Parcel"))
            for i in (1, 2)
        ]
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 2}),
                (assessed_only, {"features": units}),
                (any_record, {"features": [feat(**SHELL), *units]}),
            ],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "010-381-30"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["summary"]["resolved"] is False
        assert structured["summary"]["match_count"] == 2
        assert all(c["Parcel_ID"] for c in structured["candidates"])
        assert "- `` " not in tool_text(result)

    @pytest.mark.asyncio
    async def test_a_number_with_only_a_shell_says_what_it_is(self, plugin):
        """Its own number must not fall through to the base number and
        come back as "no such record, here is the neighbour"."""
        calls = install_client(
            plugin,
            routes=[
                (assessed_only, {"features": []}),
                (any_record, {"features": [feat(**SHELL)]}),
            ],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "010-381-30-000"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["summary"]["resolved"] is True
        assert structured["summary"]["match_count"] == 1
        assert codes(structured) == ["unassessed_record"]
        assert "base_number_match" not in codes(structured)
        text = tool_text(result)
        assert "**NO ASSESSMENT RECORD:** `010-381-30-000`" in text
        assert "find_parcel('010-381-30', category='All')" in text
        assert len(calls) == 2
        assert ASSESSED not in structured["query"]["where"]

    @pytest.mark.asyncio
    async def test_several_shells_under_one_number_are_shown_once(self, plugin):
        """27 numbers are carried by more than one geometry-only feature.
        With no ID to tell them apart they were listed as unselectable
        candidates."""
        install_client(
            plugin,
            routes=[
                (is_count, {"count": 3}),
                (assessed_only, {"features": []}),
                (any_record, {"features": [feat(**SHELL), feat(**SHELL)]}),
            ],
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "010-381-30-000"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["summary"]["resolved"] is True
        assert structured["summary"]["match_count"] == 3
        assert structured["candidates"] == []
        assert "(3 polygons carry it)" in tool_text(result)

    @pytest.mark.asyncio
    async def test_a_number_that_names_nothing_is_still_not_found(self, plugin):
        install_client(plugin, default={"features": []})
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "999-999-99"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["summary"]["resolved"] is False
        assert codes(structured) == ["no_results"]

    @pytest.mark.asyncio
    async def test_base_fallback_still_applies_after_the_shell_check(self, plugin):
        """An 11-digit number nothing carries, assessed or not, falls
        back to its base number as before."""

        def by_base(url, params):
            return assessed_only(url, params) and "'01038130'" in params["where"]

        install_client(
            plugin,
            routes=[(by_base, {"features": [feat(**LEASE)]})],
            default={"features": []},
        )
        result = await run_tool(
            plugin, "get_parcel_details", {"parcel_id": "010-381-30-777"}
        )
        structured = assert_conforms(plugin, "get_parcel_details", result)
        assert structured["summary"]["resolved"] is True
        assert "base_number_match" in codes(structured)
        assert "unassessed_record" not in codes(structured)


class TestFuzzyFallbackWhere:
    @pytest.mark.asyncio
    async def test_no_hyphenated_search_of_an_unhyphenated_column(self, plugin):
        calls = install_client(plugin, default={"features": []})
        await run_tool(plugin, "find_parcel", {"parcel_id": "002-151-32-999"})
        where = calls[-1][1]["where"]
        assert "Parcel_ID LIKE '%215132%'" in where
        assert where.count("LIKE") == 1
        assert "2-151-32" not in where


class TestOwnerGroupingCaveat:
    STATS = {
        "features": [
            feat(Owner_Name="MUNICIPALITY OF ANCHORAGE PARKS & RECREATION", n=1097),
            feat(Owner_Name="MUNICIPALITY OF ANCHORAGE HERITAGE LAND BANK", n=258),
        ]
    }

    @staticmethod
    def is_stats(url, params):
        return "outStatistics" in params

    @pytest.mark.asyncio
    @pytest.mark.parametrize("extra", [{}, {"order_by": "value_desc", "limit": 2}])
    async def test_grouping_by_owner_says_names_are_spellings(self, plugin, extra):
        install_client(plugin, routes=[(self.is_stats, self.STATS)])
        result = await run_tool(
            plugin,
            "parcel_stats",
            {
                "stat_type": "count",
                "stat_field": "Parcel_ID",
                "group_by": "Owner_Name",
                **extra,
            },
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert "owner_names_not_normalized" in codes(structured)
        text = tool_text(result)
        assert "**OWNER NAMES ARE NOT NORMALIZED:**" in text
        assert "search_by_owner" in text

    @pytest.mark.asyncio
    async def test_other_groupings_do_not_carry_it(self, plugin):
        payload = {"features": [feat(Zoning_District="R1", n=5)]}
        install_client(plugin, routes=[(self.is_stats, payload)])
        result = await run_tool(
            plugin,
            "parcel_stats",
            {
                "stat_type": "count",
                "stat_field": "Parcel_ID",
                "group_by": "Zoning_District",
            },
        )
        structured = assert_conforms(plugin, "parcel_stats", result)
        assert "owner_names_not_normalized" not in codes(structured)
