"""MOA conventions shared with the Anchorage GIS server (moa.py).

Ported from anchorage-gis-mcp's tests for the same rules, so the two
servers read a parcel number, and measure a layer's coverage, alike.
"""

import pytest

from plugins.anchorage_parcels.moa import (
    anchorage_coverage_pct,
    normalize_parcel_variants,
    parcel_base_and_sub,
    parcel_digits,
)


class TestNormalizeParcelVariants:
    """Generates the four canonical formats (8-digit compact + hyphenated,
    11-digit compact + hyphenated) from any common input form."""

    @pytest.mark.parametrize("raw", ["001-213-29", "00121329"])
    def test_base_number_gets_the_default_sub(self, raw):
        assert normalize_parcel_variants(raw) == [
            "001-213-29",
            "001-213-29-000",
            "00121329",
            "00121329000",
        ]

    def test_compact_11digit_input_preserves_sub(self):
        out = normalize_parcel_variants("00121329001")
        assert "00121329001" in out
        assert "001-213-29-001" in out
        # The base forms too, for layers that drop the sub.
        assert "00121329" in out
        assert "001-213-29" in out

    def test_input_with_leading_zero_dropped(self):
        out = normalize_parcel_variants("1-213-29")
        assert "001-213-29" in out
        assert "00121329" in out

    @pytest.mark.parametrize("raw", ["12", "", None, "no digits here"])
    def test_nothing_readable_returns_empty(self, raw):
        assert normalize_parcel_variants(raw) == []

    @pytest.mark.parametrize(
        "raw",
        [
            "Parcel 003-184-87",
            "Lot 5 Block 12, 003-184-87",
            "003-184-87 (Lot 5, Block 12)",
            "PID: 00318487",
            "parcel #003-184-87-000.",
        ],
    )
    def test_surrounding_text_does_not_change_the_id(self, raw):
        """Digits from around the id must not be run into it."""
        assert normalize_parcel_variants(raw) == [
            "003-184-87",
            "003-184-87-000",
            "00318487",
            "00318487000",
        ]

    @pytest.mark.parametrize(
        "raw",
        [
            "1820 Parkside Dr 99501",  # an address is not a parcel id
            "1820 Parkside Dr, Anchorage, AK 99501-1234",
            "003-184-87 or 001-213-29",  # two ids: which one?
            "Lot 5 Block 12",
            "0031848700099",  # 13 digits: not a parcel number
            "Parcel 1-213-29",  # short form only when it stands alone
            "A00318487",  # part of a longer identifier
        ],
    )
    def test_unreadable_input_is_refused(self, raw):
        assert normalize_parcel_variants(raw) == []
        assert parcel_digits(raw) is None
        assert parcel_base_and_sub(raw) is None

    @pytest.mark.parametrize(
        "raw, base, full",
        [
            ("003 184 87", "00318487", "00318487000"),
            ("003.184.87", "00318487", "00318487000"),
            ("318487000", "00318487", "00318487000"),
            ("3-184-87-001", "00318487", "00318487001"),
        ],
    )
    def test_id_alone_in_any_spelling(self, raw, base, full):
        out = normalize_parcel_variants(raw)
        assert base in out and full in out
        assert parcel_base_and_sub(raw) == (base, full[8:])

    def test_plain_id_keeps_its_own_spelling(self):
        out = normalize_parcel_variants("003 184 87")
        assert "003 184 87" in out and "00318487" in out


class TestParcelDigits:
    def test_tells_a_base_number_from_a_named_sub_parcel(self):
        assert parcel_digits("003-184-87") == "00318487"
        assert parcel_digits("003-184-87-000") == "00318487000"
        assert parcel_base_and_sub("003-184-87") == ("00318487", "000")
        assert parcel_base_and_sub("003-184-87-000") == ("00318487", "000")


class TestAnchorageCoveragePct:
    def test_the_parcel_layer_covers_the_municipality(self):
        """PropertyInformation_Hosted's extent, measured 2026-11-18. It
        scored 48% against the earlier, looser bounding box."""
        extent = {
            "xmin": -150.288,
            "ymin": 60.768,
            "xmax": -148.802,
            "ymax": 61.486,
            "spatialReference": {"wkid": 4326},
        }
        assert 0.9 < anchorage_coverage_pct(extent) <= 1.0

    def test_larger_than_the_municipality_is_full_coverage(self):
        extent = {
            "xmin": -180,
            "ymin": 50,
            "xmax": -130,
            "ymax": 72,
            "spatialReference": {"wkid": 4326},
        }
        assert anchorage_coverage_pct(extent) == pytest.approx(1.0)

    def test_wgs84_partial_overlap(self):
        # A small slice of downtown Anchorage.
        extent = {
            "xmin": -149.95,
            "ymin": 61.18,
            "xmax": -149.85,
            "ymax": 61.22,
            "spatialReference": {"wkid": 4326},
        }
        assert 0 < anchorage_coverage_pct(extent) < 0.05

    def test_wgs84_no_overlap(self):
        extent = {
            "xmin": -82,
            "ymin": 25,
            "xmax": -80,
            "ymax": 27,
            "spatialReference": {"wkid": 4326},
        }
        assert anchorage_coverage_pct(extent) == 0.0

    def test_web_mercator_matches_wgs84(self):
        """The same box in both projections gives the same figure."""
        mercator = {
            "xmin": -16729973.187121626,
            "ymin": 8572850.312372128,
            "xmax": -16564590.02643105,
            "ymax": 8738290.529313235,
            "spatialReference": {"wkid": 102100, "latestWkid": 3857},
        }
        wgs84 = {
            "xmin": -150.28795,
            "ymin": 60.76795,
            "xmax": -148.80229,
            "ymax": 61.48625,
            "spatialReference": {"wkid": 4326},
        }
        assert anchorage_coverage_pct(mercator) == pytest.approx(
            anchorage_coverage_pct(wgs84), abs=1e-3
        )

    def test_unhandled_sr_returns_none(self):
        # Alaska Albers is not converted.
        extent = {
            "xmin": 100000,
            "ymin": 1200000,
            "xmax": 200000,
            "ymax": 1300000,
            "spatialReference": {"wkid": 3338},
        }
        assert anchorage_coverage_pct(extent) is None

    @pytest.mark.parametrize("extent", [None, {}, {"xmin": "bad"}])
    def test_malformed_extent_returns_none(self, extent):
        assert anchorage_coverage_pct(extent) is None

    @pytest.mark.parametrize(
        "xmin, ymin, xmax, ymax",
        [
            (-149.9, 61.2, -149.9, 61.2),  # a single point
            (-149.9, 61.0, -149.9, 61.3),  # zero width
            (-150.0, 61.2, -149.0, 61.2),  # zero height
        ],
    )
    def test_degenerate_bbox_returns_none(self, xmin, ymin, xmax, ymax):
        """A one-point layer inside Anchorage is not "0% of Anchorage"."""
        extent = {
            "xmin": xmin,
            "ymin": ymin,
            "xmax": xmax,
            "ymax": ymax,
            "spatialReference": {"wkid": 4326},
        }
        assert anchorage_coverage_pct(extent) is None
