"""Municipality of Anchorage conventions shared with the Anchorage GIS server.

Two things both servers must agree on, because they answer about the same
PropertyInformation layer:

- how a caller's parcel number is read (``parcel_digits``,
  ``parcel_base_and_sub``, ``normalize_parcel_variants``);
- how much of the municipality a layer's extent covers
  (``anchorage_coverage_pct``).

These are Anchorage-specific, so they are not in the shared
``opencontext-arcgis-hub`` package. They live in the GIS deployment
(anchorage-gis-mcp, ``plugins/anchorage_gis/plugin.py``) and are copied
here from it at ffc46d0. This repo used to carry a whole copy of that
plugin just to import them. When the rules change there, change them here
too: a parcel number must not resolve differently on the two servers.
"""

import math
import re
from typing import Any, List, Optional, Tuple

# ── Parcel numbers ────────────────────────────────────────────────────

# A token with the full shape of a parcel number, as picked out of
# surrounding text.
_PARCEL_STRICT_RE = re.compile(
    r"[0-9]{3}-[0-9]{3}-[0-9]{2}(?:-[0-9]{3})?|[0-9]{8}|[0-9]{11}"
)
_PARCEL_TOKEN_RE = re.compile(r"(?<![0-9A-Za-z-])[0-9]+(?:-[0-9]+)*(?![0-9A-Za-z-])")
# An id on its own: digits with hyphens, spaces or dots between them.
_PARCEL_ALONE_RE = re.compile(r"[0-9][0-9 \t.\-]*")


def parcel_digits(raw: Any) -> Optional[str]:
    """The 5 to 11 digits of a caller's parcel id, or None.

    Every digit in the input used to be run together, so "Lot 5 Block 12,
    003-184-87" became 51200318487 and an address with a ZIP code became
    a parcel number: a lookup of a DIFFERENT parcel, answered as if it
    were the one asked for. Now:

    - an id on its own (digits with hyphens, spaces or dots) is read
      whole, leading zeros optional;
    - in free text, only a token with the full shape of a parcel number
      counts (XXX-XXX-XX, XXX-XXX-XX-XXX, 8 or 11 digits), and there must
      be exactly one;
    - more than 11 digits is not a parcel number (the extra ones used to
      be dropped silently).
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if _PARCEL_ALONE_RE.fullmatch(text):
        digits = re.sub(r"[^0-9]", "", text)
    else:
        strict = [
            t for t in _PARCEL_TOKEN_RE.findall(text) if _PARCEL_STRICT_RE.fullmatch(t)
        ]
        if len(set(strict)) != 1:
            return None
        digits = strict[0].replace("-", "")
    if len(digits) < 5 or len(digits) > 11:
        return None
    return digits


def parcel_base_and_sub(raw: Any) -> Optional[Tuple[str, str]]:
    """(8-digit base, 3-digit sub) read from a caller's parcel id.

    The sub is "000" when the caller gave only a base number; use
    ``parcel_digits`` to tell that from a sub-parcel they named.
    """
    digits = parcel_digits(raw)
    if digits is None:
        return None
    if len(digits) >= 9:
        # 9-11 digits: the 11-digit form, leading zeros dropped.
        padded = digits.rjust(11, "0")
        return padded[:8], padded[8:]
    # 5-8 digits: the 8-digit base, no sub-parcel.
    return digits.rjust(8, "0"), "000"


def normalize_parcel_variants(raw: Any) -> List[str]:
    """Every stored spelling of a parcel number, for ``WHERE f IN (...)``.

    MOA parcel IDs are stored in two related canonical forms:

    - 8-digit base: ``XXXXXXXX`` (compact) or ``XXX-XXX-XX`` (hyphenated),
      e.g. ``00318487`` / ``003-184-87``;
    - 11-digit extended: the base plus a 3-digit sub-parcel suffix (``000``
      means no sub), e.g. ``00318487000`` / ``003-184-87-000``.

    PropertyInformation has all four in separate columns, and a caller
    rarely knows which form they hold, so all four are generated. Returns
    [] when no single ID can be read (see ``parcel_digits``).
    """
    parts = parcel_base_and_sub(raw)
    if parts is None:
        return []
    base8, sub3 = parts
    hyphenated = f"{base8[0:3]}-{base8[3:6]}-{base8[6:8]}"
    variants = {base8, hyphenated, base8 + sub3, f"{hyphenated}-{sub3}"}
    # Also the caller's own spelling, in case the layer stores a form not
    # anticipated here -- but only when the input IS an id. For an id
    # picked out of free text ("Lot 5, 003-184-87") the whole sentence is
    # not a form any layer stores.
    literal = str(raw).strip()
    if literal and _PARCEL_ALONE_RE.fullmatch(literal):
        variants.add(literal)
    return sorted(variants)


# ── Coverage of the municipality ──────────────────────────────────────

# Anchorage muni bbox in WGS84 (xmin, ymin, xmax, ymax). Used to flag a
# layer that clearly covers only part of the municipality, not to filter;
# exact boundary fidelity is not needed.
#
# Sized to the municipality-wide parcel layer (PropertyInformation_Hosted:
# -150.288..-148.802, 60.768..61.486, measured 2026-11-18) plus a small
# margin. The earlier, looser box (-150.5, 60.5, -148.5, 61.6) was 2.2x
# that area, so the parcel layer itself scored 48% and every aggregate
# answer about it carried a false LIMITED COVERAGE warning.
ANCHORAGE_BBOX_WGS84 = (-150.3, 60.75, -148.8, 61.5)

# Below this share of the municipality, warn that the layer does not cover
# all of it. Same threshold as the GIS server.
COVERAGE_THRESHOLD = 0.5

_EARTH_RADIUS_M = 6378137.0  # per EPSG:3857


def _webmerc_to_wgs84(x: float, y: float) -> Tuple[float, float]:
    lon = math.degrees(x / _EARTH_RADIUS_M)
    lat = math.degrees(math.atan(math.exp(y / _EARTH_RADIUS_M)) * 2.0 - math.pi / 2.0)
    return lon, lat


def anchorage_coverage_pct(extent: Any) -> Optional[float]:
    """(layer extent intersect muni) / muni area, in [0, 1].

    None when coverage cannot be computed honestly (unhandled spatial
    reference, malformed or zero-area extent), so the caveat stays silent
    rather than guessing.
    """
    if not isinstance(extent, dict):
        return None
    coords = [extent.get(k) for k in ("xmin", "ymin", "xmax", "ymax")]
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in coords):
        return None
    xmin, ymin, xmax, ymax = coords
    # A single point or a zero-width extent has no area to compare: it is
    # not "outside Anchorage", it just carries no coverage signal.
    if xmin == xmax or ymin == ymax:
        return None
    sr = extent.get("spatialReference") or {}
    wkid = sr.get("wkid") or sr.get("latestWkid")
    if wkid in (4326, 4269):
        pass
    elif wkid in (102100, 3857, 102113):
        xmin, ymin = _webmerc_to_wgs84(xmin, ymin)
        xmax, ymax = _webmerc_to_wgs84(xmax, ymax)
    else:
        return None
    muni = ANCHORAGE_BBOX_WGS84
    ix, iy = max(xmin, muni[0]), max(ymin, muni[1])
    ax, ay = min(xmax, muni[2]), min(ymax, muni[3])
    if ix >= ax or iy >= ay:
        return 0.0
    muni_area = (muni[2] - muni[0]) * (muni[3] - muni[1])
    return (ax - ix) * (ay - iy) / muni_area
