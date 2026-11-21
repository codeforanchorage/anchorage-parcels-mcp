"""What the plugin learns about the layer has a lifetime.

opencontext-core 1.5.0 keeps one plugin object for as long as a Lambda
container stays warm; before it, a new one was built for every request.
Three things on the object were only safe while it lived for one request:

- the layer metadata read in initialize() (field names for validation,
  the extent for the coverage caveat) was read once and never again, so a
  failed read at cold start switched both off until the container died;
- the same read had no time limit;
- a failed fetch of the zoning spellings was remembered as "there are
  none".
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from plugins.anchorage_parcels.config_schema import AnchorageParcelsPluginConfig
from plugins.anchorage_parcels.plugin import AnchorageParcelsPlugin
from tests.test_anchorage_parcels_plugin import (
    SAMPLE_RECORD,
    SNAPSHOT_FIELDS,
    _layer_meta,
    feat,
    install_client,
    is_count,
    is_distinct_query,
    is_feature_query,
    make_response,
    run_tool,
)


def is_meta(url, params):
    return not url.endswith("/query")


def meta_calls(calls):
    return [c for c in calls if is_meta(*c)]


FEATURES = {"features": [feat(**SAMPLE_RECORD)]}
BAD_WHERE = {"where": "Nonexistent_Field = 'x'"}
DISTINCT = {
    "features": [{"attributes": {"Zoning_District": v}} for v in ("B2C", "B-2C", "R1")]
}


@pytest.fixture
def cold_plugin():
    """A plugin whose startup metadata read has not happened yet."""
    p = AnchorageParcelsPlugin({})
    p.plugin_config = AnchorageParcelsPluginConfig()
    p._initialized = True
    return p


async def start_with_unreachable_layer(plugin):
    install_client(plugin, routes=[(is_meta, httpx.ConnectError("refused"))])
    plugin.ARCGIS_RETRY_BACKOFF_S = 0
    await plugin._refresh_layer_meta()
    assert plugin._live_fields is None


class TestLayerMetadataIsReadAgain:
    @pytest.mark.asyncio
    async def test_a_failed_startup_read_is_retried_by_a_later_call(self, cold_plugin):
        """Field validation used to stay off for the container's life."""
        plugin = cold_plugin
        await start_with_unreachable_layer(plugin)

        # Inside the retry interval a call goes ahead without it...
        calls = install_client(
            plugin,
            routes=[(is_meta, _layer_meta(SNAPSHOT_FIELDS)), (is_count, {"count": 1})],
            default=FEATURES,
        )
        result = await run_tool(plugin, "query_parcels", {"where": "1=1"})
        assert result.success is True
        assert meta_calls(calls) == []

        # ...and after it, the next call reads the metadata first.
        plugin._layer_meta_due = time.monotonic() - 1
        result = await run_tool(plugin, "query_parcels", BAD_WHERE)
        assert len(meta_calls(calls)) == 1
        assert plugin._live_fields == SNAPSHOT_FIELDS
        assert result.success is False
        assert "Nonexistent_Field" in result.error_message

    @pytest.mark.asyncio
    async def test_fresh_metadata_is_not_fetched_per_call(self, cold_plugin):
        plugin = cold_plugin
        calls = install_client(
            plugin,
            routes=[(is_meta, _layer_meta(SNAPSHOT_FIELDS)), (is_count, {"count": 1})],
            default=FEATURES,
        )
        await plugin._refresh_layer_meta()
        for _ in range(3):
            await run_tool(plugin, "query_parcels", {"where": "1=1"})
        assert len(meta_calls(calls)) == 1

    @pytest.mark.asyncio
    async def test_a_republished_layer_is_noticed_after_the_ttl(
        self, cold_plugin, caplog
    ):
        plugin = cold_plugin
        install_client(plugin, routes=[(is_meta, _layer_meta(SNAPSHOT_FIELDS))])
        await plugin._refresh_layer_meta()

        renamed = set(SNAPSHOT_FIELDS) - {"Owner_Name"} | {"Owner_Name_Renamed"}
        install_client(
            plugin,
            routes=[(is_meta, _layer_meta(renamed)), (is_count, {"count": 1})],
            default=FEATURES,
        )
        plugin._layer_meta_due = time.monotonic() - 1
        with caplog.at_level(logging.WARNING):
            result = await run_tool(
                plugin, "query_parcels", {"where": "Owner_Name_Renamed = 'X'"}
            )
        assert result.success is True
        assert "Owner_Name_Renamed" in plugin._live_fields
        assert "SCHEMA DRIFT" in caplog.text

        # The drift is reported when the fields change, not on every
        # refresh that finds them still changed.
        caplog.clear()
        plugin._layer_meta_due = time.monotonic() - 1
        with caplog.at_level(logging.WARNING):
            await plugin._refresh_layer_meta()
        assert "SCHEMA DRIFT" not in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_answer",
        [httpx.ConnectError("refused"), {"name": "not layer metadata"}],
        ids=["unreachable", "no-field-list"],
    )
    async def test_a_failed_refresh_keeps_what_was_known(self, cold_plugin, bad_answer):
        plugin = cold_plugin
        install_client(plugin, routes=[(is_meta, _layer_meta(SNAPSHOT_FIELDS))])
        await plugin._refresh_layer_meta()

        install_client(
            plugin,
            routes=[(is_meta, bad_answer), (is_count, {"count": 1})],
            default=FEATURES,
        )
        plugin.ARCGIS_RETRY_BACKOFF_S = 0
        plugin._layer_meta_due = time.monotonic() - 1
        result = await run_tool(plugin, "query_parcels", BAD_WHERE)
        assert plugin._live_fields == SNAPSHOT_FIELDS
        assert result.success is False
        assert "Nonexistent_Field" in result.error_message
        # Not due again at once: a slow upstream is not asked per call.
        assert plugin._layer_meta_due > time.monotonic()

    @pytest.mark.asyncio
    async def test_a_hung_metadata_read_leaves_time_for_the_query(self, cold_plugin):
        """The read is a convenience; it must not spend the call's budget."""
        plugin = cold_plugin
        plugin.LAYER_META_BUDGET_S = 0.05

        async def fake_get(url, params=None):
            if is_meta(url, params or {}):
                await asyncio.sleep(30)
            if is_count(url, params or {}):
                return make_response({"count": 1})
            return make_response(FEATURES)

        plugin.client = Mock()
        plugin.client.get = AsyncMock(side_effect=fake_get)
        started = time.monotonic()
        result = await run_tool(plugin, "query_parcels", {"where": "1=1"})
        assert time.monotonic() - started < 2
        assert result.success is True

    @pytest.mark.asyncio
    async def test_startup_does_not_wait_past_its_budget(self):
        """initialize() used to wait a full HTTP timeout per attempt."""
        plugin = AnchorageParcelsPlugin({})
        plugin.LAYER_META_BUDGET_S = 0.05

        async def hang(url, params=None):
            await asyncio.sleep(30)

        client = Mock()
        client.get = AsyncMock(side_effect=hang)
        started = time.monotonic()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(httpx, "AsyncClient", Mock(return_value=client))
            assert await plugin.initialize() is True
        assert time.monotonic() - started < 2
        assert plugin._live_fields is None


class TestZoningSpellingsAreReadAgain:
    WHERE = {"where": "Zoning_District = 'B2C'"}

    @staticmethod
    def feature_where(calls):
        return [p for u, p in calls if is_feature_query(u, p)][-1]["where"]

    @pytest.fixture
    def plugin(self, cold_plugin):
        cold_plugin._live_fields = set(SNAPSHOT_FIELDS)
        cold_plugin._layer_meta_due = float("inf")
        cold_plugin.ARCGIS_RETRY_BACKOFF_S = 0
        return cold_plugin

    @pytest.mark.asyncio
    async def test_a_failed_fetch_is_not_remembered_as_no_variants(self, plugin):
        calls = install_client(
            plugin,
            routes=[
                (is_distinct_query, httpx.ConnectError("refused")),
                (is_count, {"count": 1}),
            ],
            default=FEATURES,
        )
        result = await run_tool(plugin, "query_parcels", self.WHERE)
        assert result.success is True  # degraded, not broken
        assert "Zoning_District = 'B2C'" in self.feature_where(calls)

        calls = install_client(
            plugin,
            routes=[(is_distinct_query, DISTINCT), (is_count, {"count": 1})],
            default=FEATURES,
        )
        # Not asked again inside the retry interval...
        await run_tool(plugin, "query_parcels", self.WHERE)
        assert "Zoning_District = 'B2C'" in self.feature_where(calls)
        # ...but asked again after it, where it used to stay off.
        plugin._zoning_variants_due = time.monotonic() - 1
        await run_tool(plugin, "query_parcels", self.WHERE)
        assert "Zoning_District IN ('B-2C','B2C')" in self.feature_where(calls)

    @pytest.mark.asyncio
    async def test_spellings_are_cached_then_refreshed(self, plugin):
        calls = install_client(
            plugin,
            routes=[(is_distinct_query, DISTINCT), (is_count, {"count": 1})],
            default=FEATURES,
        )
        for _ in range(3):
            await run_tool(plugin, "query_parcels", self.WHERE)
        assert len([c for c in calls if is_distinct_query(*c)]) == 1

        plugin._zoning_variants_due = time.monotonic() - 1
        await run_tool(plugin, "query_parcels", self.WHERE)
        assert len([c for c in calls if is_distinct_query(*c)]) == 2

    @pytest.mark.asyncio
    async def test_a_failed_refresh_keeps_the_known_spellings(self, plugin):
        install_client(
            plugin,
            routes=[(is_distinct_query, DISTINCT), (is_count, {"count": 1})],
            default=FEATURES,
        )
        await run_tool(plugin, "query_parcels", self.WHERE)

        calls = install_client(
            plugin,
            routes=[
                (is_distinct_query, httpx.ConnectError("refused")),
                (is_count, {"count": 1}),
            ],
            default=FEATURES,
        )
        plugin._zoning_variants_due = time.monotonic() - 1
        await run_tool(plugin, "query_parcels", self.WHERE)
        assert "Zoning_District IN ('B-2C','B2C')" in self.feature_where(calls)
