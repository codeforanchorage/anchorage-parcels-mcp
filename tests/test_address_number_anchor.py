"""An address fragment that starts with a number must start a word.

``search_by_address`` matched any substring of the stored address, so on
the live layer (2026-11-28) '44 W 15TH AVE', an address that does not
exist, returned 144 and 444 W 15TH AVE as if they were it, and '15TH
AVE' counted 169 records of which 20 were on 115TH AVE. The same clause
drove the address-point fallback, which would have resolved the missing
address to a neighbour's point.

Neither layer stores a leading or doubled space (checked on all 94,117
situs addresses and 129,696 address points), so the rule is: at the
start of the address, or straight after a space.
"""

import httpx
import pytest

from plugins.anchorage_parcels.config_schema import (
    DEFAULT_ADDRESSES_LAYER_URL,
    AnchorageParcelsPluginConfig,
)
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

MISSING = "44 W 15TH AVE"


def is_addr_layer(url, params):
    return DEFAULT_ADDRESSES_LAYER_URL in url


def is_substring_search(url, params):
    """The near-miss lookup: the fragment anywhere in the address."""
    return f"LIKE '%{MISSING}%'" in params.get("where", "")


def neighbours(*addresses):
    return {"features": [feat(Parcel_Address=a) for a in addresses]}


@pytest.fixture
def plugin():
    p = AnchorageParcelsPlugin({})
    p.plugin_config = AnchorageParcelsPluginConfig()
    p._live_fields = set(SNAPSHOT_FIELDS)
    p._layer_meta_due = float("inf")
    p._initialized = True
    return p


class TestTheClause:
    @pytest.mark.parametrize(
        "needle, clause",
        [
            (
                "44 W 15TH AVE",
                "(F LIKE '44 W 15TH AVE%' OR F LIKE '% 44 W 15TH AVE%')",
            ),
            # A numbered street is a word too: not '115TH AVE'.
            ("15TH AVE", "(F LIKE '15TH AVE%' OR F LIKE '% 15TH AVE%')"),
            ("4640", "(F LIKE '4640%' OR F LIKE '% 4640%')"),
        ],
    )
    def test_a_leading_number_starts_a_word(self, needle, clause):
        assert AnchorageParcelsPlugin._address_like("F", needle) == (clause, True)

    @pytest.mark.parametrize("needle", ["W 15TH AVE", "REKA", "O'MALLEY RD"])
    def test_a_leading_letter_is_still_a_substring(self, needle):
        quoted = needle.replace("'", "''")
        assert AnchorageParcelsPlugin._address_like("F", needle) == (
            f"F LIKE '%{quoted}%'",
            False,
        )


class TestSearchByAddress:
    @pytest.mark.asyncio
    async def test_a_street_fragment_searches_as_before(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (is_count, {"count": 1}),
                (is_feature_query, {"features": [feat(**SAMPLE_RECORD)]}),
            ],
        )
        result = await run_tool(plugin, "search_by_address", {"address": "w 15th ave"})
        assert_conforms(plugin, "search_by_address", result)
        assert "Parcel_Address LIKE '%W 15TH AVE%'" in calls[0][1]["where"]

    @pytest.mark.asyncio
    async def test_the_assessed_filter_still_applies_to_both_halves(self, plugin):
        calls = install_client(
            plugin, routes=[(is_feature_query, {"features": [feat(**SAMPLE_RECORD)]})]
        )
        await run_tool(plugin, "search_by_address", {"address": "144 W 15th Ave"})
        assert calls[0][1]["where"] == (
            "((Parcel_Address LIKE '144 W 15TH AVE%' "
            "OR Parcel_Address LIKE '% 144 W 15TH AVE%')) "
            "AND (Parcel_ID IS NOT NULL AND Parcel_ID <> '')"
        )

    @pytest.mark.asyncio
    async def test_a_missing_number_is_not_answered_with_its_neighbours(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (
                    is_substring_search,
                    # Two records at one address read as one address.
                    neighbours("444 W 15TH AVE", "144 W 15TH AVE", "144 W 15TH AVE"),
                ),
            ],
        )
        result = await run_tool(plugin, "search_by_address", {"address": MISSING})
        structured = assert_conforms(plugin, "search_by_address", result)
        assert structured["rows"] == []
        assert structured["summary"]["match_method"] == "none"
        assert structured["summary"]["returned_count"] == 0
        assert codes(structured) == ["no_address_match"]
        message = structured["caveats"][0]["message"]
        assert "DIFFERENT addresses" in message
        assert "'144 W 15TH AVE', '444 W 15TH AVE'" in message
        assert message in tool_text(result)
        # Situs, then address points, then the near-miss lookup: the
        # address-point search was word-anchored as well.
        addr_call = next(c for c in calls if is_addr_layer(*c))
        assert addr_call[1]["where"] == (
            f"(FULL_ADDRESS LIKE '{MISSING}%' OR FULL_ADDRESS LIKE '% {MISSING}%')"
        )

    @pytest.mark.asyncio
    async def test_at_most_five_neighbours_are_named(self, plugin):
        install_client(
            plugin,
            routes=[
                (
                    is_substring_search,
                    neighbours(*(f"{n}44 W 15TH AVE" for n in range(1, 9))),
                )
            ],
        )
        result = await run_tool(plugin, "search_by_address", {"address": MISSING})
        message = assert_conforms(plugin, "search_by_address", result)["caveats"][0][
            "message"
        ]
        assert message.count("44 W 15TH AVE'") == plugin.NEAR_ADDRESS_LIMIT

    @pytest.mark.asyncio
    async def test_no_neighbours_no_note(self, plugin):
        install_client(plugin)
        result = await run_tool(plugin, "search_by_address", {"address": MISSING})
        structured = assert_conforms(plugin, "search_by_address", result)
        assert codes(structured) == ["no_address_match"]
        assert "DIFFERENT addresses" not in tool_text(result)

    @pytest.mark.asyncio
    async def test_a_failed_neighbour_lookup_leaves_the_answer_standing(self, plugin):
        install_client(
            plugin,
            routes=[(is_substring_search, httpx.ConnectError("boom"))],
        )
        result = await run_tool(plugin, "search_by_address", {"address": MISSING})
        structured = assert_conforms(plugin, "search_by_address", result)
        assert codes(structured) == ["no_address_match"]
        assert "DIFFERENT addresses" not in tool_text(result)

    @pytest.mark.asyncio
    async def test_a_street_fragment_miss_asks_nothing_more(self, plugin):
        calls = install_client(plugin)
        result = await run_tool(plugin, "search_by_address", {"address": "Nowhere Ln"})
        assert_conforms(plugin, "search_by_address", result)
        # Situs and address points only; it was a substring search already.
        assert len(calls) == 2
