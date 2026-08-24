"""Anchorage Parcels plugin implementation for OpenContext.

Read-only, domain-shaped access to the Municipality of Anchorage
property/assessment records (PropertyInformation, Addresses and
Subdivisions Feature Layers on MOAGIS / ArcGIS Online). The schema is
baked into the tools so a model can answer "who owns 144 W 15th Ave and
what's it assessed at?" in one call, with no schema pre-flight.

For arbitrary MOA GIS layers (zoning polygons, trails, flood zones,
spatial overlays), use the Anchorage GIS MCP server instead -- this
server only wraps the assessment/property layers.

Forkability: every layer URL comes from plugin config
(config_schema.py) and every field name funnels through
``DEFAULT_FIELD_MAP`` below. To point this plugin at another city's
assessor layer, override the URLs in config and the differing field
names via the ``field_map`` config key -- no other code changes.
"""

import asyncio
import difflib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

import httpx

from core.interfaces import (
    MCPPlugin,
    PluginType,
    ToolDefinition,
    ToolInputError,
    ToolResult,
)
from plugins.anchorage_gis.plugin import AnchorageGISPlugin
from plugins.anchorage_parcels.config_schema import AnchorageParcelsPluginConfig
from plugins.arcgis.where_validator import (
    OrderByValidator,
    OutFieldsValidator,
    WhereValidator,
)

logger = logging.getLogger(__name__)

# Parcel-ID normalization is shared with the Anchorage GIS plugin
# (plugins/anchorage_gis/plugin.py). It turns any of the four MOA parcel
# formats ('002-151-32', '00215132', '00215132000', '002-151-32-000')
# into the full variant set for a WHERE ... IN (...) lookup. The import
# is intentionally one-directional (parcels -> gis); anchorage_gis knows
# nothing about this plugin.
_normalize_parcel_variants = AnchorageGISPlugin._normalize_parcel_variants

# Logical -> physical field-name map for the property layer (plus the
# two supporting layers at the bottom). THIS IS THE ONE BLOCK a forker
# edits (or overrides per-key via the `field_map` config option) to
# adapt the plugin to another city's assessor layer.
DEFAULT_FIELD_MAP: Dict[str, str] = {
    # Identity -- the parcel number in its four stored variants.
    "parcel_id": "Parcel_ID",  # 11-digit unformatted, e.g. 00215132000
    "parcel8": "GIS_ParcelNum8",
    "parcel8_formatted": "GIS_ParcelNum8Formatted",  # e.g. 002-151-32
    "parcel11": "GIS_ParcelNum11",
    "parcel11_formatted": "GIS_ParcelNum11Formatted",  # 002-151-32-000
    "category": "GIS_Category",  # 'Parcel' | 'Lease' | 'Economic'
    "parcel_id_count": "Parcel_ID_Count",
    "condo_unit": "Condo_Unit_Number",
    "datalet_url": "Parcel_ID_URL",  # assessor Datalet deep link
    # Situs / legal
    "situs_address": "Parcel_Address",
    "legal_description": "Legal_Description",
    "zoning": "Zoning_District",
    "land_use": "Land_Use",
    "property_class": "Class",
    "property_type": "Property_Type",
    "lot_size": "Lot_Size",  # sq ft
    "acreage": "CAMA_Acreage",
    "living_units": "Total_Living_Units",
    "year_built": "YearBuilt",
    "grid_map": "Grid_Map",
    "tax_district": "Tax_District",
    # Owner (mailing)
    "owner_name": "Owner_Name",
    "owner_address": "Owner_Address",
    "owner_city": "Owner_City",
    "owner_state": "Owner_State",
    "owner_zip": "Owner_Zip",
    # Valuation (current + two prior years)
    "appraisal_year": "Appraisal_Year",
    "appraised_land": "Appraised_Land_Value",
    "appraised_building": "Appraised_Building_Value",
    "appraised_total": "Appraised_Total_Value",
    "taxable_value": "Taxable_Value",
    "net_taxable": "NetTaxableValue",
    "land_prev": "Land_Value_Previous",
    "building_prev": "Building_Value_Previous",
    "total_prev": "Total_Value_Previous",
    "land_prev2": "Land_Value_Previous_2",
    "building_prev2": "Building_Value_Previous_2",
    "total_prev2": "Total_Value_Previous_2",
    # Exemptions
    "total_exemptions": "Total_Exemptions",
    "exemption_types": "Exemption_Types_All",
    "exemption_1_type": "Exemption_1_Type",
    "exemption_1_amount": "Exemption_1_Amount",
    "exemption_2_type": "Exemption_2_Type",
    "exemption_2_amount": "Exemption_2_Amount",
    "exemption_5_type": "Exemption_5_Type",
    "exemption_5_amount": "Exemption_5_Amount",
    "exemption_6_type": "Exemption_6_Type",
    "exemption_6_amount": "Exemption_6_Amount",
    # Deed / plat
    "deed_book": "Deed_Book",
    "deed_page": "Deed_Page",
    "deed_date": "Deed_Date",
    "plat_number": "Plat_Number",
    # Misc
    "economic_unit": "GIS_Economic_Unit",
    "mean_slope": "GIS_MeanPercentSlope",
    "pub_date": "PUBDATE",
    # Addresses supporting layer (address-point fallback)
    "address_full": "FULL_ADDRESS",
    # Subdivisions supporting layer
    "subdivision_name": "SUBDIVISION_NAME",
}

# Logical names whose physical fields make up the SUMMARY out_fields set
# used by every list-style tool.
SUMMARY_LOGICAL_FIELDS = (
    "parcel_id",
    "parcel8_formatted",
    "owner_name",
    "situs_address",
    "legal_description",
    "zoning",
    "land_use",
    "lot_size",
    "acreage",
    "appraised_total",
    "taxable_value",
    "year_built",
    "category",
    "datalet_url",
)

# GIS_Category discriminator values in the MOA layer. A forker whose
# layer has no category concept can leave these; category='All' skips
# the filter entirely.
CATEGORY_VALUES = ("Parcel", "Lease", "Economic")

CASE_SENSITIVE_NOTE = "Field names are case-sensitive."

# Stable machine-readable codes for the warnings a tool response carries.
# A caller branches on `code` instead of pattern-matching prose that may
# be reworded. The prose banner and the structured entry are generated
# from ONE list (see `_Caveats`), so the two cannot drift apart.
CAVEAT_CODES = (
    "limit_clamped",  # requested limit exceeded the tool maximum
    "results_truncated",  # more records match than were returned
    "list_truncated",  # the disambiguation list itself was cut short
    "more_pages_available",  # page on with offset=
    "no_results",  # nothing matched
    "fuzzy_match",  # rows are LIKE candidates, NOT exact matches
    "no_fuzzy_candidates",  # not even a fuzzy fallback hit
    "multiple_records",  # the ID resolves to several records (condos)
    "address_point_fallback",  # resolved via address layer + point-in-polygon
    "no_address_match",  # neither situs nor address-point matched
    "point_outside_parcels",  # the point falls in no property polygon
    "stacked_categories",  # Parcel/Lease/Economic polygons overlap here
    "unassessed_included",  # geometry-only shell records were NOT filtered out
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Caveats:
    """Warnings for one tool response, rendered into BOTH output forms.

    Every warning is added here exactly once. The prose banner and the
    ``caveats`` array in structuredContent are both derived from this
    list, which is what stops the human-readable text and the
    machine-readable contract from drifting apart as either is edited.
    """

    def __init__(self) -> None:
        self._items: List[Dict[str, str]] = []

    def add(self, code: str, message: Optional[str]) -> None:
        """Record a warning. A falsy message is ignored, so callers can
        pass an optional notice straight through."""
        if message:
            self._items.append({"code": code, "message": message})

    def add_first(self, code: str, message: Optional[str]) -> None:
        """Like `add`, but placed ahead of everything already recorded."""
        if message:
            self._items.insert(0, {"code": code, "message": message})

    @property
    def messages(self) -> List[str]:
        """The prose lines, in order."""
        return [item["message"] for item in self._items]

    def as_list(self) -> List[Dict[str, str]]:
        return [dict(item) for item in self._items]

    def __len__(self) -> int:
        return len(self._items)


class _ToolOutput(NamedTuple):
    """What a tool handler returns: prose for the model, data for code."""

    text: str
    structured: Optional[Dict[str, Any]] = None


# ── Output schemas ────────────────────────────────────────────────────
#
# A declared outputSchema is BINDING -- the spec says servers MUST return
# conforming structured results. These are deliberately loose where the
# real data is loose: rows carry whatever out_fields the caller asked
# for, statistic values can come back as strings (min/max over a text
# field) or null, and record counts are null when the count endpoint
# fails. A schema written from the happy path would make the server
# violate its own contract on live data.

_CAVEATS_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "description": (
        "Warnings about this result. Branch on `code` rather than "
        "parsing the prose; every entry here also appears verbatim in "
        "the text content."
    ),
    "items": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "enum": list(CAVEAT_CODES)},
            "message": {"type": "string"},
        },
        "required": ["code", "message"],
        "additionalProperties": False,
    },
}

_ROW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "One record: RAW layer attributes keyed by physical field name. "
        "Which fields are present depends on out_fields. Date fields are "
        "epoch milliseconds -- summary.date_fields_epoch_ms names them."
    ),
    "additionalProperties": True,
}

_SUMMARY_BASE_PROPS: Dict[str, Any] = {
    "date_fields_epoch_ms": {
        "type": "array",
        "items": {"type": "string"},
        "description": (
            "Fields in `rows`/`result` whose raw value is epoch "
            "milliseconds. The prose renders these as ISO dates; the "
            "structured values are left raw."
        ),
    },
}

_NULLABLE_COUNT: Dict[str, Any] = {
    "type": ["integer", "null"],
    "description": (
        "Null when the count could not be established (the count "
        "endpoint failed or was not consulted) -- which is NOT the same "
        "as zero."
    ),
}


def _envelope_schema(
    description: str,
    query_props: Dict[str, Any],
    summary_props: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Build one tool's output schema around the shared envelope.

    `payload` names the result-carrying keys (rows, or result +
    candidates) so a model learns one shape across the whole server.
    """
    properties: Dict[str, Any] = {
        "query": {
            "type": "object",
            "description": "What was asked, as the server resolved it.",
            "properties": query_props,
            "additionalProperties": True,
        },
        "summary": {
            "type": "object",
            "description": "Counts and outcome flags for this result.",
            "properties": {**_SUMMARY_BASE_PROPS, **summary_props},
            "additionalProperties": True,
        },
        "caveats": _CAVEATS_SCHEMA,
    }
    properties.update(payload)
    return {
        "type": "object",
        "description": description,
        "properties": properties,
        # Every declared key is required on every code path; extra keys
        # stay legal so a later addition is not a contract violation.
        "required": ["query", "summary", "caveats", *payload],
        "additionalProperties": True,
    }


class AnchorageParcelsPlugin(MCPPlugin):
    """Plugin for Municipality of Anchorage property/assessment records.

    Wraps the public MOA PropertyInformation Feature Layer (parcels,
    leases, economic units; ~99.7k polygons, republished frequently)
    plus the Addresses and Subdivisions layers, exposing 7 read-only
    domain tools with the layer schema baked in.
    """

    plugin_name = "anchorage_parcels"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    # Retry policy for transient ArcGIS failures (ported from
    # plugins/anchorage_gis/plugin.py `_request_json_with_retry`).
    ARCGIS_MAX_ATTEMPTS = 3
    ARCGIS_RETRY_BACKOFF_S = 0.5

    # Server maxRecordCount on the MOA layers (verified). A single
    # query page can never exceed this; query_parcels pages with
    # resultOffset when the caller's limit is larger.
    SERVER_PAGE_SIZE = 2000
    # Hard cap on records in one tool response.
    MAX_LIMIT = 1000
    # Above this many records, _format_records switches from per-record
    # blocks to a compact pipe-delimited table.
    COMPACT_FORMAT_THRESHOLD = 20
    # Max stacked polygons returned for one point-in-polygon lookup.
    POINT_QUERY_LIMIT = 20

    SCHEMA_SNAPSHOT_PATH = Path(__file__).parent / "schema" / "propertyinformation.json"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[AnchorageParcelsPluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None
        self.field_map: Dict[str, str] = dict(DEFAULT_FIELD_MAP)
        # Live layer metadata captured at initialize(); None when the
        # startup fetch failed (plugin still starts -- degraded > down).
        self._live_fields: Optional[set] = None
        self._date_fields: set = set()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        try:
            self.plugin_config = AnchorageParcelsPluginConfig(**self.config)
        except Exception as e:
            # Misconfiguration is fatal -- fail fast so the deploy is
            # fixed rather than serving broken tools.
            logger.error(
                f"Failed to validate anchorage_parcels config: {e}",
                exc_info=True,
            )
            return False

        self.field_map = dict(DEFAULT_FIELD_MAP)
        self.field_map.update(self.plugin_config.field_map)
        self.client = httpx.AsyncClient(timeout=self.plugin_config.timeout)

        # Reachability + schema-drift check. Failures here are logged
        # loudly but do NOT block startup: the layer is republished
        # frequently and a transient blip at cold start must not take
        # the whole server down (degraded > down).
        try:
            meta = await self._layer_query(
                self.plugin_config.property_layer_url, {"f": "json"}
            )
            self._capture_layer_meta(meta)
            self._check_schema_drift(meta)
        except Exception as e:
            logger.warning(
                "anchorage_parcels: property layer unreachable at startup; "
                "starting anyway (queries will retry per call)",
                extra={"error": str(e)},
            )

        self._initialized = True
        logger.info(
            f"Anchorage Parcels plugin initialized for {self.plugin_config.city_name}"
        )
        return True

    def _capture_layer_meta(self, meta: Dict[str, Any]) -> None:
        fields = meta.get("fields") or []
        self._live_fields = {f.get("name") for f in fields if f.get("name")}
        self._date_fields = {
            f.get("name")
            for f in fields
            if f.get("type") == "esriFieldTypeDate" and f.get("name")
        }

    def _check_schema_drift(self, meta: Dict[str, Any]) -> None:
        """Diff live field names against the vendored schema snapshot.

        On drift, log a loud structured warning naming missing/renamed
        fields -- but keep serving (degraded > down). Also verify that
        every physical field in the active field map still exists.
        """
        live = self._live_fields or set()
        try:
            with open(self.SCHEMA_SNAPSHOT_PATH, encoding="utf-8") as fh:
                snapshot = json.load(fh)
            snapshot_fields = {
                f.get("name") for f in snapshot.get("fields", []) if f.get("name")
            }
        except Exception as e:
            logger.warning(
                "anchorage_parcels: could not load vendored schema snapshot; "
                "skipping drift check",
                extra={"path": str(self.SCHEMA_SNAPSHOT_PATH), "error": str(e)},
            )
            return

        missing = sorted(snapshot_fields - live)
        added = sorted(live - snapshot_fields)
        if missing or added:
            logger.warning(
                "anchorage_parcels: SCHEMA DRIFT DETECTED on the property "
                "layer -- live fields differ from the vendored snapshot. "
                "Tools referencing missing fields will fail; refresh the "
                "snapshot and review DEFAULT_FIELD_MAP.",
                extra={
                    "layer_url": self.plugin_config.property_layer_url,
                    "missing_fields": missing,
                    "added_fields": added,
                },
            )

        unmapped = sorted(
            f"{logical} -> {physical}"
            for logical, physical in self.field_map.items()
            # Supporting-layer fields aren't on the property layer.
            if logical not in ("address_full", "subdivision_name")
            and physical not in live
        )
        if unmapped:
            logger.warning(
                "anchorage_parcels: field map references fields missing "
                "from the live property layer",
                extra={"unmapped_fields": unmapped},
            )

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self._initialized = False
        logger.info("Anchorage Parcels plugin shut down")

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get(
                self.plugin_config.property_layer_url, params={"f": "json"}
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # ── Field-map helpers ─────────────────────────────────────────────

    def _f(self, logical: str) -> str:
        """Physical field name for a logical field-map key."""
        return self.field_map[logical]

    @property
    def _summary_fields(self) -> str:
        return ",".join(self._f(k) for k in SUMMARY_LOGICAL_FIELDS)

    @property
    def _parcel_id_fields(self) -> List[str]:
        """The five columns a parcel number may be stored in."""
        return [
            self._f(k)
            for k in (
                "parcel_id",
                "parcel8",
                "parcel8_formatted",
                "parcel11",
                "parcel11_formatted",
            )
        ]

    # ── WHERE helpers ─────────────────────────────────────────────────

    @staticmethod
    def _sql_quote(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def _assessed_clause(self, include_unassessed: Any) -> Optional[str]:
        """Exclude geometry-only shell records unless asked not to.

        ~1,000 features on this layer carry geometry and a formatted
        parcel number but NO assessment join: null Parcel_ID,
        Legal_Description, Land_Use and Appraised_Total_Value. They are
        indistinguishable from real parcels in a count, so every
        "how many parcels" answer was inflated by ~1% (98,391 -> 97,368
        for GIS_Category='Parcel'). Filtered out by default; pass
        include_unassessed=True to get them back.
        """
        if include_unassessed:
            return None
        field = self._f("parcel_id")
        return f"{field} IS NOT NULL AND {field} <> ''"

    @staticmethod
    def _unassessed_caveat(include_unassessed: Any) -> Optional[str]:
        """Banner shown when the shell records are deliberately included."""
        if not include_unassessed:
            return None
        return (
            "**UNASSESSED RECORDS INCLUDED:** results contain "
            "geometry-only records with no assessment data (null "
            "parcel number, legal description, land use and valuation). "
            "They inflate counts by ~1% and their assessment fields "
            "will be empty. Omit include_unassessed to exclude them."
        )

    def _category_clause(self, category: Optional[str]) -> Optional[str]:
        """Build the GIS_Category filter; None means no filter."""
        if category is None:
            category = "Parcel"
        normalized = str(category).strip().title()
        if normalized == "All" or normalized == "":
            return None
        if normalized not in CATEGORY_VALUES:
            raise ToolInputError(
                f"category must be one of {list(CATEGORY_VALUES) + ['All']} "
                f"(got {category!r}). 'Parcel' is regular taxable parcels; "
                f"'Lease' is leased government land; 'Economic' is "
                f"multi-parcel economic units; 'All' searches everything."
            )
        return f"{self._f('category')} = {self._sql_quote(normalized)}"

    @staticmethod
    def _combine_where(*clauses: Optional[str]) -> str:
        parts = [c for c in clauses if c and c.strip() and c.strip() != "1=1"]
        if not parts:
            return "1=1"
        if len(parts) == 1:
            return parts[0]
        return " AND ".join(f"({p})" for p in parts)

    def _validate_out_fields(self, out_fields: Optional[str]) -> str:
        if not out_fields or not str(out_fields).strip():
            return self._summary_fields
        return OutFieldsValidator.validate(str(out_fields))

    def _clamp_limit(
        self, args: Dict[str, Any], default: int, maximum: int
    ) -> Tuple[int, Optional[str]]:
        """Clamp the limit argument to [1, maximum].

        Returns (limit, notice); the notice is a visible banner when the
        request exceeded the maximum -- silent clamping would leave the
        caller with no signal that records were withheld.
        """
        raw_limit = args.get("limit", default)
        try:
            requested = int(raw_limit)
        except (TypeError, ValueError):
            raise ToolInputError(
                f"limit must be an integer (got {raw_limit!r})."
            ) from None
        limit = max(1, min(requested, maximum))
        note = None
        if requested > maximum:
            note = (
                f"**LIMIT CLAMPED:** requested limit={requested} exceeds "
                f"the maximum of {maximum}; returning at most {maximum}."
            )
        return limit, note

    def _check_field_exists(self, field: str, arg_name: str) -> str:
        """Validate a single field-name argument against the live schema."""
        field = (field or "").strip()
        if not _IDENT_RE.match(field):
            raise ToolInputError(
                f"{arg_name} must be a single field name "
                f"(got {field!r}). {CASE_SENSITIVE_NOTE}"
            )
        if self._live_fields and field not in self._live_fields:
            suggestion = difflib.get_close_matches(
                field, sorted(self._live_fields), n=1, cutoff=0.6
            )
            hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
            raise ToolInputError(
                f"{arg_name} {field!r} is not a field on the property "
                f"layer.{hint} {CASE_SENSITIVE_NOTE}"
            )
        return field

    # ── HTTP / query plumbing ─────────────────────────────────────────

    @staticmethod
    def _arcgis_error_text(err: Any) -> str:
        """Non-empty message from an ArcGIS error object (ported from
        plugins/anchorage_gis/plugin.py)."""
        if not isinstance(err, dict):
            return str(err) or "unknown ArcGIS error"
        parts: List[str] = []
        code = err.get("code")
        if code is not None:
            parts.append(f"code {code}")
        msg = (err.get("message") or "").strip()
        if msg:
            parts.append(msg)
        details = [str(d) for d in (err.get("details") or []) if d]
        if details:
            parts.append("; ".join(details))
        return " -- ".join(parts) or (
            "ArcGIS returned an error with no message (usually a transient "
            "server blip -- retry the request)"
        )

    @classmethod
    def _is_transient_arcgis_error(cls, err: Any) -> bool:
        """5xx-class codes and empty-message errors are transient."""
        if not isinstance(err, dict):
            return False
        if err.get("code") in (500, 502, 503, 504):
            return True
        return not (err.get("message") or "").strip()

    async def _layer_query(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET a layer endpoint, retrying transient upstream failures.

        Ported from plugins/anchorage_gis/plugin.py
        `_request_json_with_retry`: retries httpx transport/timeout
        errors, HTTP 5xx, and transient ArcGIS error bodies; raises
        immediately (with a rewritten, actionable message) on real
        errors.
        """
        last_desc = "unknown error"
        attempt = 0
        while attempt < self.ARCGIS_MAX_ATTEMPTS:
            attempt += 1
            transient = False
            try:
                resp = await self.client.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                transient, last_desc = True, f"network error: {e!r}"
            else:
                status = resp.status_code
                if status >= 500:
                    transient, last_desc = True, f"upstream HTTP {status}"
                elif status >= 400:
                    raise RuntimeError(
                        f"Feature Service error (HTTP {status}): {resp.text[:200]}"
                    )
                else:
                    try:
                        payload = resp.json()
                    except Exception as e:
                        raise ValueError(
                            "Feature Service returned non-JSON "
                            f"(content-type "
                            f"{resp.headers.get('content-type', '?')})"
                        ) from e
                    err = payload.get("error") if isinstance(payload, dict) else None
                    if not err:
                        return payload
                    if self._is_transient_arcgis_error(err):
                        transient = True
                        last_desc = self._arcgis_error_text(err)
                    else:
                        raise RuntimeError(
                            self._rewrite_arcgis_error(
                                self._arcgis_error_text(err),
                                has_where="where" in params,
                                has_out_fields="outFields" in params,
                            )
                        )
            if not transient or attempt >= self.ARCGIS_MAX_ATTEMPTS:
                break
            await asyncio.sleep(self.ARCGIS_RETRY_BACKOFF_S * attempt)
        raise RuntimeError(
            f"Feature Service request failed after {attempt} attempt(s): {last_desc}"
        )

    def _rewrite_arcgis_error(
        self,
        full: str,
        has_where: bool = False,
        has_out_fields: bool = False,
    ) -> str:
        """Turn raw ArcGIS REST errors into actionable instructions.

        Same pattern as plugins/anchorage_gis/plugin.py
        `_rewrite_arcgis_error`, adapted to this plugin's fixed layer:
        there is no get_layer_schema tool here, so recovery hints name
        the closest real field (difflib) and point at out_fields='*'.
        """
        m = re.search(r"[Ii]nvalid\s+field\s*:?\s*([A-Za-z0-9_]+)", full)
        if m:
            bad = m.group(1)
            suggestion = ""
            if self._live_fields:
                close = difflib.get_close_matches(
                    bad, sorted(self._live_fields), n=1, cutoff=0.6
                )
                if close:
                    suggestion = f" Did you mean '{close[0]}'?"
            return (
                f"Field '{bad}' does not exist on the property layer."
                f"{suggestion} Field names are CASE-SENSITIVE. The "
                f"summary fields are: {self._summary_fields}. Use "
                f"query_parcels with out_fields='*' and limit=1 to see "
                f"every field. (Underlying error: {full})"
            )
        if "Invalid query parameters" in full:
            hint_parts = []
            if has_out_fields:
                hint_parts.append(
                    "out_fields may reference a field that does not exist "
                    "(ArcGIS does not name it in the error). Try "
                    "out_fields='*' to confirm, then narrow."
                )
            if has_where:
                hint_parts.append(
                    "the WHERE clause may reference a missing field or use "
                    "the wrong type (string values must be single-quoted)."
                )
            hint_parts.append(
                "Field names are CASE-SENSITIVE. The summary fields are: "
                f"{self._summary_fields}."
            )
            return f"{full}\n\nLikely cause: " + " ".join(hint_parts)
        return full

    async def _fetch_count(self, where: str) -> Optional[int]:
        """Total records matching a WHERE clause (returnCountOnly)."""
        try:
            data = await self._layer_query(
                f"{self.plugin_config.property_layer_url}/query",
                {"f": "json", "where": where, "returnCountOnly": "true"},
            )
            return data.get("count")
        except Exception as e:
            logger.warning(f"count query failed for where={where!r}: {e}")
            return None

    async def _query_property_layer(
        self,
        where: str,
        out_fields: str,
        limit: int,
        offset: int = 0,
        order_by: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Fetch up to `limit` attribute rows, paging past the server's
        maxRecordCount with resultOffset.

        Returns (records, exceeded_transfer_limit) where the flag is
        True when the server reported more rows remain past the last
        page fetched.
        """
        records: List[Dict[str, Any]] = []
        exceeded = False
        while len(records) < limit:
            want = min(self.SERVER_PAGE_SIZE, limit - len(records))
            params: Dict[str, Any] = {
                "f": "json",
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "false",
                "resultRecordCount": str(want),
                "resultOffset": str(offset + len(records)),
            }
            if order_by:
                params["orderByFields"] = order_by
            if extra_params:
                params.update(extra_params)
            data = await self._layer_query(
                f"{self.plugin_config.property_layer_url}/query", params
            )
            feats = data.get("features") or []
            records.extend(f.get("attributes") or {} for f in feats)
            exceeded = bool(data.get("exceededTransferLimit"))
            if not feats or (len(feats) < want and not exceeded):
                break
        return records[:limit], exceeded

    # ── Formatting (conventions from plugins/anchorage_gis/plugin.py) ──

    @staticmethod
    def _with_retrieved_footer(text: str) -> str:
        # Stamp every tool response with a UTC retrieval timestamp so
        # models can tell stale outputs from fresh ones. Skip when a
        # provenance header already carries a Retrieved: line.
        if not text or "Retrieved:" in text:
            return text
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{text}\n\n_Retrieved: {retrieved_at}_"

    @staticmethod
    def _ms_to_iso_smart(ms: Any) -> Any:
        # Midnight UTC -> date-only; non-midnight -> full ISO; on
        # failure return the raw value (losing data is worse than
        # showing epoch ms). Ported from anchorage_gis.
        if ms is None or ms == "":
            return ms
        try:
            dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return ms
        if dt.hour == dt.minute == dt.second == dt.microsecond == 0:
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _provenance(
        self,
        where: Optional[str] = None,
        out_fields: Optional[str] = None,
        limit: Optional[int] = None,
        source_url: Optional[str] = None,
    ) -> List[str]:
        lines = [f"Source: {source_url or self.plugin_config.property_layer_url}"]
        query_parts = []
        if where is not None:
            query_parts.append(f"where={where!r}")
        if out_fields is not None:
            query_parts.append(f"outFields={out_fields!r}")
        if limit is not None:
            query_parts.append(f"resultRecordCount={limit}")
        if query_parts:
            lines.append(f"Query: {', '.join(query_parts)}")
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(f"Retrieved: {retrieved_at}")
        return lines

    def _date_field_names(self) -> List[str]:
        """Fields whose RAW value is epoch milliseconds.

        Structured output carries raw values; this is the decode map a
        consumer needs to turn them into dates. The prose rendering
        converts them to ISO, the structured rows deliberately do not.
        """
        if self._date_fields:
            return sorted(self._date_fields)
        return sorted({self._f("deed_date"), self._f("pub_date")})

    def _envelope(
        self,
        query: Dict[str, Any],
        summary: Dict[str, Any],
        caveats: "_Caveats",
        **payload: Any,
    ) -> Dict[str, Any]:
        """Assemble the {query, summary, <payload>, caveats} envelope.

        One shape across every tool, so a model that learns it once can
        read any of them. `payload` is `rows=` for list tools, or
        `result=` + `candidates=` for the single-record tool.
        """
        envelope: Dict[str, Any] = {
            "query": query,
            "summary": {
                "date_fields_epoch_ms": self._date_field_names(),
                **summary,
            },
            "caveats": caveats.as_list(),
        }
        envelope.update(payload)
        return envelope

    @staticmethod
    def _render_caveats(lines: List[str], caveats: "_Caveats") -> None:
        """Render every caveat into the prose.

        Called on EVERY return path so that anything in structured
        `caveats` is also visible to a model reading the text.
        """
        lines.extend(caveats.messages)

    def _render_value(self, key: str, value: Any) -> Any:
        if value is None or value == "":
            return value
        if key in self._date_fields or (
            not self._date_fields and key in (self._f("deed_date"), self._f("pub_date"))
        ):
            return self._ms_to_iso_smart(value)
        return value

    def _format_records(
        self,
        records: List[Dict[str, Any]],
        limit: int,
        total_count: Optional[int] = None,
        where: Optional[str] = None,
        out_fields: Optional[str] = None,
        heading: Optional[str] = None,
        caveats: Optional["_Caveats"] = None,
    ) -> str:
        """List records with provenance header, TOTAL COUNT line, and a
        truncation banner (conventions from anchorage_gis
        `_format_query_results`).

        All banners -- the truncation notice raised here and any the
        caller already recorded (LIMIT CLAMPED, MORE PAGES, ...) -- come
        from the single `caveats` list, which is also what structured
        output serialises. Text and structuredContent therefore cannot
        disagree about what went wrong."""
        caveats = caveats if caveats is not None else _Caveats()
        lines = self._provenance(where=where, out_fields=out_fields, limit=limit)
        truncated = (
            total_count is not None and len(records) > 0 and total_count > len(records)
        )
        if truncated:
            caveats.add_first(
                "results_truncated",
                f"**TRUNCATED:** returned {len(records)} of "
                f"{total_count:,} matching records (limit={limit}). The "
                f"records below are a SAMPLE -- do not generalize counts "
                f"or totals from them. Use the TOTAL COUNT line below for "
                f"'how many?' questions, or page with offset / narrow the "
                f"WHERE clause.",
            )
        self._render_caveats(lines, caveats)
        lines.append("")
        if heading:
            lines.append(heading)
        if not records:
            lines.append("No records returned.")
            return "\n".join(lines)
        count_part = f"{len(records)}"
        if total_count is not None:
            count_part += f" of {total_count:,} total"
        lines.append(f"Returned {count_part} record(s) (limit: {limit}).")
        if total_count is not None:
            lines.append(
                f"TOTAL COUNT (records matching the WHERE clause): "
                f"{total_count:,}. This is the answer to 'how many?' -- "
                f"use it directly instead of counting the records below."
            )
        lines.append("")
        if len(records) > self.COMPACT_FORMAT_THRESHOLD:
            # Large result sets: pipe-delimited table (header row +
            # one row per record) instead of per-record blocks, which
            # cost ~3x the bytes of the data they carry.
            columns: List[str] = list(records[0].keys())
            seen = set(columns)
            for record in records[1:]:
                for key in record:
                    if key not in seen:
                        seen.add(key)
                        columns.append(key)
            lines.append(
                f"(Compact format: {len(records)} records, one "
                f"pipe-delimited row each; the first row is the header.)"
            )
            lines.append(" | ".join(columns))
            for record in records:
                lines.append(
                    " | ".join(
                        self._table_cell(self._render_value(k, record.get(k)))
                        for k in columns
                    )
                )
            lines.append("")
        else:
            for i, record in enumerate(records, 1):
                lines.append(f"Record {i}:")
                for key, value in record.items():
                    lines.append(f"  {key}: {self._render_value(key, value)}")
                lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _table_cell(value: Any) -> str:
        if value is None:
            return ""
        return str(value).replace("|", "\\|")

    def _no_data_hint(self, where_clause: str) -> str:
        """Recovery hints appended after an empty result (pattern from
        anchorage_gis `_no_data_hint`, specialized for this layer)."""
        normalized = (where_clause or "").strip()
        if not normalized or normalized == "1=1":
            return ""
        return (
            "\n\n_If you expected matches:_\n"
            f"- Records are split by {self._f('category')} "
            f"('Parcel', 'Lease', 'Economic') -- the default filter is "
            f"'Parcel'; retry with category='All'.\n"
            f"- Parcel IDs: {self._f('parcel_id')} stores 11 digits with "
            f"no hyphens (e.g. '00215132000'); "
            f"{self._f('parcel8_formatted')} stores '002-151-32'. Try "
            f"dropping/adding hyphens, or use find_parcel which tries "
            f"all four formats automatically.\n"
            f"- Owner names and addresses are stored UPPERCASE (e.g. "
            f"'MUNICIPALITY OF ANCHORAGE', '144 W 15TH AVE').\n"
            "- For TEXT fields, exact `=` is strict; try "
            "`Field LIKE '%substring%'` (% is the wildcard).\n"
            f"- {CASE_SENSITIVE_NOTE}"
        )

    @staticmethod
    def _fmt_money(value: Any) -> str:
        if value is None or value == "":
            return "--"
        try:
            return f"${float(value):,.0f}"
        except (TypeError, ValueError):
            return str(value)

    # ── Tool: find_parcel ─────────────────────────────────────────────

    async def _find_parcel(self, args: Dict[str, Any]) -> _ToolOutput:
        parcel_id = str(args.get("parcel_id") or "").strip()
        if not parcel_id:
            raise ToolInputError("parcel_id is required")
        limit, clamp_note = self._clamp_limit(args, default=10, maximum=100)
        out_fields = self._validate_out_fields(args.get("out_fields"))
        category_clause = self._category_clause(args.get("category"))

        variants = _normalize_parcel_variants(parcel_id)
        if not variants:
            raise ToolInputError(
                f"Could not extract a parcel number from {parcel_id!r}. "
                f"Provide an ID like '002-151-32', '00215132', "
                f"'00215132000', or '002-151-32-000'."
            )
        in_list = ",".join(self._sql_quote(v) for v in variants)
        exact_clause = " OR ".join(
            f"{f} IN ({in_list})" for f in self._parcel_id_fields
        )
        where = self._combine_where(f"({exact_clause})", category_clause)

        caveats = _Caveats()
        caveats.add("limit_clamped", clamp_note)
        query = {
            "parcel_id": parcel_id,
            "category": args.get("category"),
            "out_fields": out_fields,
            "limit": limit,
            "where": where,
            "variants_tried": variants,
        }

        records, _ = await self._query_property_layer(where, out_fields, limit)
        if records:
            heading = (
                f"## Parcel lookup: `{parcel_id}` -> "
                f"{len(records)} record(s) (exact match)\n"
                f"Variants tried ({len(variants)}): "
                + ", ".join(f"`{v}`" for v in variants)
                + "\n"
            )
            text = self._format_records(
                records,
                limit,
                where=where,
                out_fields=out_fields,
                heading=heading,
                caveats=caveats,
            )
            return _ToolOutput(
                text,
                self._envelope(
                    query,
                    {"returned_count": len(records), "exact_match": True},
                    caveats,
                    rows=records,
                ),
            )

        # Zero exact hits -> LIKE fallback on the 11-digit column with a
        # distinctive digit substring, clearly labeled as fuzzy.
        digits = "".join(c for c in parcel_id if c.isdigit())
        stripped = digits.lstrip("0")
        substring = stripped[:6] if len(stripped) >= 6 else stripped
        candidates: List[Dict[str, Any]] = []
        if substring:
            like_where = self._combine_where(
                f"{self._f('parcel_id')} LIKE "
                f"'%{substring.replace(chr(39), chr(39) * 2)}%'",
                category_clause,
            )
            try:
                candidates, _ = await self._query_property_layer(
                    like_where, self._summary_fields, 5
                )
            except Exception:
                candidates = []

        lines = [
            f"## Parcel lookup: no exact match for `{parcel_id}`",
            f"Variants tried ({len(variants)}): "
            + ", ".join(f"`{v}`" for v in variants),
            "",
        ]
        if candidates:
            caveats.add(
                "fuzzy_match",
                f"**FUZZY candidates** (LIKE '%{substring}%' on "
                f"{self._f('parcel_id')} -- these are NOT exact matches, "
                f"verify before using):",
            )
        else:
            caveats.add(
                "no_fuzzy_candidates",
                "No fuzzy candidates either. The parcel may not exist, "
                "or it may be filed under a different category -- retry "
                "with category='All'.",
            )
        # Render every caveat, so nothing in structured output is absent
        # from the text a model actually reads.
        self._render_caveats(lines, caveats)
        if candidates:
            lines.append("")
            for c in candidates:
                pid = c.get(self._f("parcel_id"))
                addr = c.get(self._f("situs_address"))
                owner = c.get(self._f("owner_name"))
                lines.append(f"- `{pid}` -- {addr} -- {owner}")
            lines.append("")
            lines.append(
                "_Pick the right candidate and call get_parcel_details "
                "with its exact ID._"
            )
        return _ToolOutput(
            "\n".join(lines),
            self._envelope(
                query,
                {"returned_count": len(candidates), "exact_match": False},
                caveats,
                rows=candidates,
            ),
        )

    # ── Tool: get_parcel_details ──────────────────────────────────────

    async def _get_parcel_details(self, args: Dict[str, Any]) -> _ToolOutput:
        parcel_id = str(args.get("parcel_id") or "").strip()
        if not parcel_id:
            raise ToolInputError("parcel_id is required")
        variants = _normalize_parcel_variants(parcel_id)
        if not variants:
            raise ToolInputError(
                f"Could not extract a parcel number from {parcel_id!r}. "
                f"Provide an ID like '002-151-32' or '00215132000'."
            )
        in_list = ",".join(self._sql_quote(v) for v in variants)
        where = (
            "("
            + " OR ".join(f"{f} IN ({in_list})" for f in self._parcel_id_fields)
            + ")"
        )

        caveats = _Caveats()
        query = {
            "parcel_id": parcel_id,
            "where": where,
            "variants_tried": variants,
        }

        records, _ = await self._query_property_layer(where, "*", 2)
        if not records:
            # Zero-result branch: it MUST still emit conforming
            # structured content, not short-circuit past it.
            caveats.add(
                "no_results",
                f"No property record found for `{parcel_id}`. "
                f"Try `find_parcel('{parcel_id}')` -- it adds a fuzzy "
                f"fallback and can search Lease/Economic records with "
                f"category='All'.",
            )
            return _ToolOutput(
                "\n".join(caveats.messages),
                self._envelope(
                    query,
                    {
                        "match_count": 0,
                        "match_count_is_lower_bound": False,
                        "resolved": False,
                    },
                    caveats,
                    result=None,
                    candidates=[],
                ),
            )
        if len(records) > 1:
            # Condos share a parcel root; Parcel_ID_Count > 1 marks it.
            # Refetch slim columns so every unit is listed -- large
            # complexes have far more units than one detail page's worth.
            slim_fields = ",".join(
                self._f(k)
                for k in (
                    "parcel_id",
                    "category",
                    "condo_unit",
                    "situs_address",
                    "owner_name",
                )
            )
            (records, exceeded), total = await asyncio.gather(
                self._query_property_layer(
                    where,
                    slim_fields,
                    self.MAX_LIMIT,
                    order_by=self._f("parcel_id"),
                ),
                self._fetch_count(where),
            )
            if total is None:
                total = f"{len(records)}+" if exceeded else len(records)
            # `total` is a "N+" string when the count endpoint failed and
            # the page was capped. Structured output keeps the number and
            # the fact that it is only a lower bound in separate fields
            # rather than shipping a string where a count belongs.
            if isinstance(total, int):
                match_count, is_lower_bound = total, False
            else:
                match_count, is_lower_bound = len(records), True
            caveats.add(
                "multiple_records",
                f"`{parcel_id}` matches {total} records (condo "
                f"units and lease/economic overlays share parcel roots). "
                f"Which unit do you want? Pass the exact 11-digit "
                f"{self._f('parcel_id')} to get_parcel_details:",
            )
            lines: List[str] = []
            self._render_caveats(lines, caveats)
            lines.append("")
            for r in records:
                lines.append(
                    f"- `{r.get(self._f('parcel_id'))}` "
                    f"[{r.get(self._f('category'))}] "
                    f"unit={r.get(self._f('condo_unit')) or '--'} -- "
                    f"{r.get(self._f('situs_address'))} -- "
                    f"{r.get(self._f('owner_name'))}"
                )
            if exceeded or (isinstance(total, int) and total > len(records)):
                truncation = (
                    f"**LIST TRUNCATED:** showing the first "
                    f"{len(records)} of {total} matching records. Page "
                    f"through the rest with query_parcels using the "
                    f"same parcel number and an offset."
                )
                caveats.add("list_truncated", truncation)
                lines.append("")
                lines.append(truncation)
            return _ToolOutput(
                "\n".join(lines),
                self._envelope(
                    query,
                    {
                        "match_count": match_count,
                        "match_count_is_lower_bound": is_lower_bound,
                        "resolved": False,
                    },
                    caveats,
                    result=None,
                    candidates=records,
                ),
            )

        r = records[0]

        def v(logical: str) -> Any:
            physical = self._f(logical)
            return self._render_value(physical, r.get(physical))

        def line(label: str, logical: str, money: bool = False) -> str:
            raw = v(logical)
            shown = self._fmt_money(raw) if money else raw
            if shown is None or shown == "":
                shown = "--"
            return f"- {label}: {shown}"

        appraisal_year = v("appraisal_year")
        prev_year = prev2_year = ""
        try:
            prev_year = f" ({int(appraisal_year) - 1})"
            prev2_year = f" ({int(appraisal_year) - 2})"
        except (TypeError, ValueError):
            pass

        lines = self._provenance(where=where, out_fields="*", limit=1)
        lines += [
            "",
            f"# Parcel {v('parcel_id')} -- {v('situs_address') or 'no situs address'}",
            "",
            "## Identity & legal",
            line("Parcel ID (11-digit)", "parcel_id"),
            line("Parcel ID (formatted)", "parcel8_formatted"),
            line("Category", "category"),
            line("Legal description", "legal_description"),
            line("Property type", "property_type"),
            line("Class", "property_class"),
            line("Condo unit", "condo_unit"),
            "",
            "## Situs & land",
            line("Address", "situs_address"),
            line("Zoning district", "zoning"),
            line("Land use", "land_use"),
            line("Lot size (sq ft)", "lot_size"),
            line("Acreage (CAMA)", "acreage"),
            line("Living units", "living_units"),
            line("Year built", "year_built"),
            "",
            "## Owner (mailing address of record)",
            line("Owner", "owner_name"),
            f"- Mailing: {v('owner_address') or '--'}, "
            f"{v('owner_city') or '--'}, {v('owner_state') or '--'} "
            f"{v('owner_zip') or ''}".rstrip(),
            "",
            f"## Valuation (appraisal year {appraisal_year or '--'})",
            line("Appraised land", "appraised_land", money=True),
            line("Appraised building", "appraised_building", money=True),
            line("Appraised total", "appraised_total", money=True),
            line("Taxable value", "taxable_value", money=True),
            line("Net taxable value", "net_taxable", money=True),
            f"- Prior year{prev_year}: land "
            f"{self._fmt_money(v('land_prev'))}, building "
            f"{self._fmt_money(v('building_prev'))}, total "
            f"{self._fmt_money(v('total_prev'))}",
            f"- Two years prior{prev2_year}: land "
            f"{self._fmt_money(v('land_prev2'))}, building "
            f"{self._fmt_money(v('building_prev2'))}, total "
            f"{self._fmt_money(v('total_prev2'))}",
            "",
            "## Exemptions",
            line("Total exemptions", "total_exemptions", money=True),
            line("Exemption types", "exemption_types"),
        ]
        for slot in ("1", "2", "5", "6"):
            ex_type = v(f"exemption_{slot}_type")
            ex_amount = v(f"exemption_{slot}_amount")
            if ex_type or ex_amount:
                lines.append(
                    f"- Exemption {slot}: {ex_type or '--'} "
                    f"({self._fmt_money(ex_amount)})"
                )
        lines += [
            "",
            "## Deed & plat",
            line("Deed book", "deed_book"),
            line("Deed page", "deed_page"),
            line("Deed date", "deed_date"),
            line("Plat number", "plat_number"),
            "",
            "## Misc",
            line("Tax district", "tax_district"),
            line("Grid map", "grid_map"),
            line("Economic unit", "economic_unit"),
            line("Mean slope (%)", "mean_slope"),
            line("Assessment published", "pub_date"),
        ]
        datalet = r.get(self._f("datalet_url"))
        if datalet:
            lines += [
                "",
                f"**Full assessor record (Datalet):** {datalet}",
            ]
        return _ToolOutput(
            "\n".join(lines),
            self._envelope(
                query,
                {
                    "match_count": 1,
                    "match_count_is_lower_bound": False,
                    "resolved": True,
                },
                caveats,
                # The RAW record, every field the layer returned -- not
                # the subset the prose above chose to render.
                result=r,
                candidates=[],
            ),
        )

    # ── Tool: search_by_owner ─────────────────────────────────────────

    async def _search_by_owner(self, args: Dict[str, Any]) -> _ToolOutput:
        name = str(args.get("name") or "").strip()
        if not name:
            raise ToolInputError("name is required")
        limit, clamp_note = self._clamp_limit(args, default=20, maximum=self.MAX_LIMIT)
        category_clause = self._category_clause(args.get("category"))
        owner_field = self._f("owner_name")
        include_unassessed = bool(args.get("include_unassessed", False))
        assessed_clause = self._assessed_clause(include_unassessed)
        needle = name.upper().replace("'", "''")
        where = self._combine_where(
            f"UPPER({owner_field}) LIKE '%{needle}%'",
            category_clause,
            assessed_clause,
        )
        order_by = f"{self._f('appraised_total')} DESC"
        records_task = self._query_property_layer(
            where, self._summary_fields, limit, order_by=order_by
        )
        (records, _), total = await asyncio.gather(
            records_task, self._fetch_count(where)
        )
        caveats = _Caveats()
        caveats.add("limit_clamped", clamp_note)
        caveats.add("unassessed_included", self._unassessed_caveat(include_unassessed))
        if not records:
            caveats.add(
                "no_results",
                f"No parcel has an owner name matching `{name}`.",
            )
        text = self._format_records(
            records,
            limit,
            total_count=total,
            where=where,
            out_fields=self._summary_fields,
            heading=(
                f"## Parcels with owner matching `{name}` "
                f"(ordered by appraised value, highest first)\n"
            ),
            caveats=caveats,
        )
        if not records:
            text += self._no_data_hint(where)
        return _ToolOutput(
            text,
            self._envelope(
                {
                    "name": name,
                    "category": args.get("category"),
                    "include_unassessed": include_unassessed,
                    "where": where,
                    "out_fields": self._summary_fields,
                    "order_by": order_by,
                    "limit": limit,
                },
                {
                    "returned_count": len(records),
                    "total_count": total,
                    "truncated": bool(
                        total is not None and records and total > len(records)
                    ),
                },
                caveats,
                rows=records,
            ),
        )

    # ── Tool: search_by_address ───────────────────────────────────────

    async def _search_by_address(self, args: Dict[str, Any]) -> _ToolOutput:
        address = str(args.get("address") or "").strip()
        if not address:
            raise ToolInputError("address is required")
        limit, clamp_note = self._clamp_limit(args, default=10, maximum=100)
        needle = address.upper().replace("'", "''")
        situs_field = self._f("situs_address")
        include_unassessed = bool(args.get("include_unassessed", False))
        assessed_clause = self._assessed_clause(include_unassessed)
        where = self._combine_where(f"{situs_field} LIKE '%{needle}%'", assessed_clause)

        caveats = _Caveats()
        caveats.add("limit_clamped", clamp_note)
        caveats.add("unassessed_included", self._unassessed_caveat(include_unassessed))
        query = {
            "address": address,
            "include_unassessed": include_unassessed,
            "where": where,
            "out_fields": self._summary_fields,
            "limit": limit,
        }

        records, _ = await self._query_property_layer(
            where, self._summary_fields, limit
        )
        if records:
            text = self._format_records(
                records,
                limit,
                where=where,
                out_fields=self._summary_fields,
                heading=(
                    f"## Parcels matching address `{address}` "
                    f"(matched directly on {situs_field})\n"
                ),
                caveats=caveats,
            )
            return _ToolOutput(
                text,
                self._envelope(
                    query,
                    {
                        "returned_count": len(records),
                        "match_method": "situs",
                        "matched_address_point": None,
                        "address_point": None,
                    },
                    caveats,
                    rows=records,
                ),
            )

        # Fallback: resolve via the address-point layer, then
        # point-in-polygon against the property layer.
        addr_field = self._f("address_full")
        addr_where = f"{addr_field} LIKE '%{needle}%'"
        addr_data = await self._layer_query(
            f"{self.plugin_config.addresses_layer_url}/query",
            {
                "f": "json",
                "where": addr_where,
                "outFields": addr_field,
                "returnGeometry": "true",
                "outSR": "4326",
                "resultRecordCount": "5",
            },
        )
        addr_feats = addr_data.get("features") or []
        if not addr_feats:
            caveats.add(
                "no_address_match",
                f"No parcel has a situs address matching `{address}`, and "
                f"the address-point layer has no match either.\n"
                f"Hints: use the abbreviated street type ('AVE', 'ST', "
                f"'DR'); drop unit/apartment numbers; addresses are "
                f"stored UPPERCASE (handled automatically); try just the "
                f"house number + street name (e.g. '144 W 15TH').",
            )
            return _ToolOutput(
                "\n".join(caveats.messages),
                self._envelope(
                    query,
                    {
                        "returned_count": 0,
                        "match_method": "none",
                        "matched_address_point": None,
                        "address_point": None,
                    },
                    caveats,
                    rows=[],
                ),
            )

        point = addr_feats[0].get("geometry") or {}
        matched_addr = (addr_feats[0].get("attributes") or {}).get(addr_field)
        lon, lat = point.get("x"), point.get("y")
        if lon is None or lat is None:
            raise RuntimeError(
                "Address point found but no usable geometry returned; "
                "retry, or use parcels_at_point with known coordinates."
            )
        pip_records = await self._parcels_intersecting_point(lat, lon, assessed_clause)
        caveats.add(
            "address_point_fallback",
            f"No direct {situs_field} match; resolved via the "
            f"address-point layer (matched `{matched_addr}`, point "
            f"lon={lon:.6f} lat={lat:.6f}) -> point-in-polygon on the "
            f"property layer.",
        )
        summary = {
            "returned_count": len(pip_records),
            "match_method": "address_point",
            "matched_address_point": matched_addr,
            "address_point": {"lon": lon, "lat": lat},
        }
        lines = [f"## Parcels at address `{address}`"]
        self._render_caveats(lines, caveats)
        if len(addr_feats) > 1:
            others = [
                (f.get("attributes") or {}).get(addr_field) for f in addr_feats[1:]
            ]
            lines.append(
                f"(Other address-point candidates not used: "
                f"{', '.join(str(o) for o in others)})"
            )
        lines.append("")
        if not pip_records:
            outside = (
                "The address point falls inside no property polygon -- "
                "it may sit on a right-of-way or unassessed land."
            )
            caveats.add("point_outside_parcels", outside)
            lines.append(outside)
            return _ToolOutput(
                "\n".join(lines),
                self._envelope(query, summary, caveats, rows=[]),
            )
        body = self._format_records(
            pip_records,
            limit,
            out_fields=self._summary_fields,
            heading=None,
            # Caveats are already rendered above; pass a fresh list so
            # the shared formatter does not repeat them.
            caveats=_Caveats(),
        )
        return _ToolOutput(
            "\n".join(lines) + "\n" + body,
            self._envelope(query, summary, caveats, rows=pip_records),
        )

    # ── Tool: parcels_at_point ────────────────────────────────────────

    async def _parcels_intersecting_point(
        self,
        lat: float,
        lon: float,
        assessed_clause: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Point-in-polygon on the property layer, all categories."""
        data = await self._layer_query(
            f"{self.plugin_config.property_layer_url}/query",
            {
                "f": "json",
                "where": self._combine_where(assessed_clause),
                "outFields": self._summary_fields,
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "returnGeometry": "false",
                "resultRecordCount": str(self.POINT_QUERY_LIMIT),
            },
        )
        return [f.get("attributes") or {} for f in data.get("features") or []]

    async def _parcels_at_point(self, args: Dict[str, Any]) -> _ToolOutput:
        try:
            lat = float(args["lat"])
            lon = float(args["lon"])
        except (KeyError, TypeError, ValueError):
            raise ToolInputError(
                "lat and lon are required numbers (WGS84 decimal degrees, "
                "e.g. lat=61.209, lon=-149.894)"
            )
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise ToolInputError(
                f"lat/lon out of range (got lat={lat}, lon={lon}). "
                f"lat must be in [-90, 90] and lon in [-180, 180]; "
                f"Anchorage is roughly lat 61, lon -149."
            )
        include_unassessed = bool(args.get("include_unassessed", False))
        assessed_clause = self._assessed_clause(include_unassessed)
        records = await self._parcels_intersecting_point(lat, lon, assessed_clause)
        caveats = _Caveats()
        caveats.add("unassessed_included", self._unassessed_caveat(include_unassessed))
        query = {
            "lat": lat,
            "lon": lon,
            "include_unassessed": include_unassessed,
            "out_fields": self._summary_fields,
            "limit": self.POINT_QUERY_LIMIT,
        }
        lines = self._provenance(
            where=f"point intersect lon={lon}, lat={lat} (all categories)",
            out_fields=self._summary_fields,
            limit=self.POINT_QUERY_LIMIT,
        )
        lines.append("")
        if not records:
            caveats.add(
                "point_outside_parcels",
                f"No property polygons contain point (lat={lat}, "
                f"lon={lon}). The point may be on a right-of-way, water, "
                f"or outside the Municipality of Anchorage.",
            )
            self._render_caveats(lines, caveats)
            return _ToolOutput(
                "\n".join(lines),
                self._envelope(
                    query,
                    {"returned_count": 0, "by_category": {}},
                    caveats,
                    rows=[],
                ),
            )
        cat_field = self._f("category")
        counts: Dict[str, int] = {}
        for r in records:
            counts[str(r.get(cat_field))] = counts.get(str(r.get(cat_field)), 0) + 1
        lines.append(
            f"## {len(records)} record(s) contain point (lat={lat}, lon={lon})"
        )
        caveats.add(
            "stacked_categories",
            "Parcel, Lease, and Economic polygons can STACK at one "
            "point -- each hit is labeled with its category. By category: "
            + ", ".join(f"{k}={n}" for k, n in sorted(counts.items())),
        )
        self._render_caveats(lines, caveats)
        lines.append("")
        for i, r in enumerate(records, 1):
            lines.append(f"Record {i} [{r.get(cat_field)}]:")
            for key, value in r.items():
                lines.append(f"  {key}: {self._render_value(key, value)}")
            lines.append("")
        return _ToolOutput(
            "\n".join(lines),
            self._envelope(
                query,
                {"returned_count": len(records), "by_category": counts},
                caveats,
                rows=records,
            ),
        )

    # ── Tool: query_parcels ───────────────────────────────────────────

    async def _query_parcels(self, args: Dict[str, Any]) -> _ToolOutput:
        raw_where = str(args.get("where") or "").strip()
        if not raw_where:
            raise ToolInputError("where is required (use '1=1' to match everything)")
        where = WhereValidator.validate(raw_where)
        WhereValidator.validate_against_schema(where, self._live_fields)
        out_fields = self._validate_out_fields(args.get("out_fields"))
        order_by = OrderByValidator.validate(str(args.get("order_by") or ""))
        limit, clamp_note = self._clamp_limit(args, default=100, maximum=self.MAX_LIMIT)
        if clamp_note:
            clamp_note += " Page with offset= for more."
        raw_offset = args.get("offset", 0)
        try:
            offset = max(0, int(raw_offset))
        except (TypeError, ValueError):
            raise ToolInputError(
                f"offset must be an integer (got {raw_offset!r})."
            ) from None
        category_clause = self._category_clause(args.get("category"))
        include_unassessed = bool(args.get("include_unassessed", False))
        full_where = self._combine_where(
            where, category_clause, self._assessed_clause(include_unassessed)
        )

        records_task = self._query_property_layer(
            full_where,
            out_fields,
            limit,
            offset=offset,
            order_by=order_by or None,
        )
        (records, exceeded), total = await asyncio.gather(
            records_task, self._fetch_count(full_where)
        )
        caveats = _Caveats()
        caveats.add("limit_clamped", clamp_note)
        caveats.add("unassessed_included", self._unassessed_caveat(include_unassessed))

        remaining = None
        if total is not None:
            remaining = total - (offset + len(records))
        has_more = (remaining is not None and remaining > 0) or (
            remaining is None and exceeded
        )
        next_offset = offset + len(records) if has_more else None
        if has_more:
            caveats.add(
                "more_pages_available",
                f"**MORE PAGES AVAILABLE:** call query_parcels again "
                f"with offset={next_offset} (same where/order_by) for the "
                f"next page.",
            )
        if not records:
            caveats.add(
                "no_results",
                "No records matched the WHERE clause.",
            )

        text = self._format_records(
            records,
            limit,
            total_count=total,
            where=full_where,
            out_fields=out_fields,
            caveats=caveats,
        )
        if not records:
            text += self._no_data_hint(full_where)
        return _ToolOutput(
            text,
            self._envelope(
                {
                    "where": full_where,
                    "requested_where": raw_where,
                    "category": args.get("category"),
                    "include_unassessed": include_unassessed,
                    "out_fields": out_fields,
                    "order_by": order_by or None,
                    "limit": limit,
                    "offset": offset,
                },
                {
                    "returned_count": len(records),
                    "total_count": total,
                    "truncated": bool(
                        total is not None and records and total > len(records)
                    ),
                    "next_offset": next_offset,
                    "exceeded_transfer_limit": bool(exceeded),
                },
                caveats,
                rows=records,
            ),
        )

    # ── Tool: parcel_stats ────────────────────────────────────────────

    STAT_TYPES = (
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "stddev",
        "var",
        "percentile_cont",
    )

    async def _parcel_stats(self, args: Dict[str, Any]) -> _ToolOutput:
        stat_type = str(args.get("stat_type") or "").strip().lower()
        if stat_type not in self.STAT_TYPES:
            raise ToolInputError(
                f"stat_type must be one of {list(self.STAT_TYPES)} "
                f"(got {stat_type!r}). Median = percentile_cont with "
                f"percentile=0.5."
            )
        stat_field = self._check_field_exists(
            str(args.get("stat_field") or ""), "stat_field"
        )
        group_by = str(args.get("group_by") or "").strip()
        if group_by:
            group_by = self._check_field_exists(group_by, "group_by")
        raw_where = str(args.get("where") or "").strip()
        where = WhereValidator.validate(raw_where or "1=1")
        WhereValidator.validate_against_schema(where, self._live_fields)
        category_clause = self._category_clause(args.get("category"))
        include_unassessed = bool(args.get("include_unassessed", False))
        full_where = self._combine_where(
            where, category_clause, self._assessed_clause(include_unassessed)
        )

        out_name = f"{stat_type}_{stat_field}"
        stat_entry: Dict[str, Any] = {
            "statisticType": stat_type,
            "onStatisticField": stat_field,
            "outStatisticFieldName": out_name,
        }
        percentile = None
        if stat_type == "percentile_cont":
            raw_percentile = args.get("percentile", 0.5)
            try:
                percentile = float(raw_percentile)
            except (TypeError, ValueError):
                raise ToolInputError(
                    f"percentile must be a number between 0 and 1 "
                    f"(got {raw_percentile!r}); 0.5 is the median."
                ) from None
            if not (0.0 <= percentile <= 1.0):
                raise ToolInputError(
                    f"percentile must be between 0 and 1 "
                    f"(got {percentile}); 0.5 is the median"
                )
            stat_entry["statisticParameters"] = {"value": percentile}

        params: Dict[str, Any] = {
            "f": "json",
            "where": full_where,
            "outStatistics": json.dumps([stat_entry], separators=(",", ":")),
            "returnGeometry": "false",
        }
        if group_by:
            params["groupByFieldsForStatistics"] = group_by
            params["orderByFields"] = group_by
        data = await self._layer_query(
            f"{self.plugin_config.property_layer_url}/query", params
        )
        rows = [f.get("attributes") or {} for f in data.get("features") or []]

        stat_label = stat_type
        if percentile is not None:
            stat_label = f"percentile_cont({percentile})"
        caveats = _Caveats()
        caveats.add("unassessed_included", self._unassessed_caveat(include_unassessed))
        query = {
            "stat_type": stat_type,
            "include_unassessed": include_unassessed,
            "stat_field": stat_field,
            "group_by": group_by or None,
            "percentile": percentile,
            "where": full_where,
            "category": args.get("category"),
        }
        lines = self._provenance(where=full_where)
        lines += [
            "",
            f"## {stat_label} of {stat_field}"
            + (f" by {group_by}" if group_by else ""),
            "",
        ]
        if not rows:
            # Zero-result branch: still emits conforming structured
            # content rather than short-circuiting past the envelope.
            caveats.add("no_results", "No statistics returned (0 matching records).")
            self._render_caveats(lines, caveats)
            return _ToolOutput(
                "\n".join(lines) + self._no_data_hint(full_where),
                self._envelope(query, {"group_count": 0}, caveats, rows=[]),
            )
        # Render caveats on the SUCCESS path too. The zero-result branch
        # above already does; without this, a caveat could exist in
        # structuredContent and be invisible in the text.
        self._render_caveats(lines, caveats)
        if len(caveats):
            lines.append("")

        # Structured rows carry the RAW statistic value; the prose below
        # applies thousands separators and float->int tidying, which
        # would be lossy for a caller doing arithmetic.
        structured_rows = [
            {
                "group": row.get(group_by) if group_by else None,
                "value": row.get(out_name),
            }
            for row in rows
        ]
        for row in rows:
            value = row.get(out_name)
            if isinstance(value, float) and value == int(value):
                value = int(value)
            shown = f"{value:,}" if isinstance(value, (int, float)) else value
            if group_by:
                lines.append(f"- {row.get(group_by)}: {shown}")
            else:
                lines.append(f"- {shown}")
        if group_by:
            lines += [
                "",
                f"({len(rows)} group(s); groups are ordered by {group_by}.)",
            ]
        return _ToolOutput(
            "\n".join(lines),
            self._envelope(
                query,
                {"group_count": len(structured_rows)},
                caveats,
                rows=structured_rows,
            ),
        )

    # ── Tool definitions ──────────────────────────────────────────────

    def get_tools(self) -> List[ToolDefinition]:
        # Every tool here is a read-only wrapper over an external ArcGIS
        # service: readOnlyHint lets a client skip per-call confirmation,
        # openWorldHint says the result set is not a closed domain.
        # idempotentHint is deliberately ABSENT -- the schema documents it
        # as meaningful only when readOnlyHint is false, so setting it
        # here would be noise.
        annotations = {"readOnlyHint": True, "openWorldHint": True}
        city = (
            self.plugin_config.city_name
            if self.plugin_config
            else "Municipality of Anchorage"
        )
        summary_note = (
            f"Returns the SUMMARY fields by default ({self._summary_fields}); "
            f"pass out_fields='*' for everything. {CASE_SENSITIVE_NOTE}"
        )
        routing_note = (
            "This server covers ONLY property/assessment records; for "
            "other MOA layers (zoning polygons, trails, flood zones, "
            "spatial analysis), use the Anchorage GIS MCP server."
        )
        unassessed_note = (
            "Excludes ~1,000 geometry-only records with no assessment "
            "data; pass include_unassessed=True to include them."
        )
        include_unassessed_schema = {
            "type": "boolean",
            "default": False,
            "description": (
                "Include geometry-only records that carry NO assessment "
                "data (null parcel number, legal description, land use "
                "and valuation). ~1,000 such shells exist and are "
                "EXCLUDED by default because they inflate every count by "
                "about 1%. Pass true only when you specifically want the "
                "unjoined geometry."
            ),
        }
        category_schema = {
            "type": "string",
            "enum": list(CATEGORY_VALUES) + ["All"],
            "default": "Parcel",
            "description": (
                "Record category: 'Parcel' (regular parcels, the "
                "default), 'Lease' (leased government land), 'Economic' "
                "(multi-parcel economic units), or 'All'."
            ),
        }
        rows_payload = {
            "rows": {
                "type": "array",
                "description": "Matching records, RAW layer attributes.",
                "items": _ROW_SCHEMA,
            }
        }

        find_parcel_output = _envelope_schema(
            "Parcels matching a parcel number -- or FUZZY candidates when "
            "nothing matched exactly (check summary.exact_match).",
            {
                "parcel_id": {"type": "string"},
                "category": {"type": ["string", "null"]},
                "out_fields": {"type": "string"},
                "limit": {"type": "integer"},
                "where": {"type": "string"},
                "variants_tried": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Every parcel-number format tried.",
                },
            },
            {
                "returned_count": {"type": "integer"},
                "exact_match": {
                    "type": "boolean",
                    "description": (
                        "False when `rows` are fuzzy LIKE candidates "
                        "rather than exact hits -- verify before using."
                    ),
                },
            },
            rows_payload,
        )

        details_output = _envelope_schema(
            "One parcel's full assessment record, or the candidate list "
            "when the ID is ambiguous (condo units share parcel roots).",
            {
                "parcel_id": {"type": "string"},
                "where": {"type": "string"},
                "variants_tried": {"type": "array", "items": {"type": "string"}},
            },
            {
                "match_count": {"type": "integer"},
                "match_count_is_lower_bound": {
                    "type": "boolean",
                    "description": (
                        "True when the true total is unknown and "
                        "match_count is only the number listed."
                    ),
                },
                "resolved": {
                    "type": "boolean",
                    "description": "True only when `result` is populated.",
                },
            },
            {
                "result": {
                    "type": ["object", "null"],
                    "description": (
                        "The full RAW assessment record -- every field the "
                        "layer returned, not the subset the prose renders. "
                        "Null when the ID matched nothing or was ambiguous."
                    ),
                    "additionalProperties": True,
                },
                "candidates": {
                    "type": "array",
                    "items": _ROW_SCHEMA,
                    "description": (
                        "Records sharing this parcel root when the ID is "
                        "ambiguous; empty on every other path."
                    ),
                },
            },
        )

        owner_output = _envelope_schema(
            "Parcels whose owner name matches, highest appraised value first.",
            {
                "name": {"type": "string"},
                "category": {"type": ["string", "null"]},
                "where": {"type": "string"},
                "out_fields": {"type": "string"},
                "order_by": {"type": "string"},
                "limit": {"type": "integer"},
            },
            {
                "returned_count": {"type": "integer"},
                "total_count": _NULLABLE_COUNT,
                "truncated": {"type": "boolean"},
            },
            rows_payload,
        )

        address_output = _envelope_schema(
            "Parcels at an address, matched on situs or via the "
            "address-point layer (check summary.match_method).",
            {
                "address": {"type": "string"},
                "where": {"type": "string"},
                "out_fields": {"type": "string"},
                "limit": {"type": "integer"},
            },
            {
                "returned_count": {"type": "integer"},
                "match_method": {
                    "type": "string",
                    "enum": ["situs", "address_point", "none"],
                    "description": (
                        "'situs' = direct match on the parcel's own "
                        "address; 'address_point' = resolved through the "
                        "address layer then point-in-polygon, so the "
                        "result is inferred; 'none' = no match at all."
                    ),
                },
                "matched_address_point": {"type": ["string", "null"]},
                "address_point": {
                    "type": ["object", "null"],
                    "properties": {
                        "lon": {"type": "number"},
                        "lat": {"type": "number"},
                    },
                    "additionalProperties": True,
                },
            },
            rows_payload,
        )

        point_output = _envelope_schema(
            "Every property polygon containing a coordinate. Parcel, "
            "Lease and Economic polygons STACK, so one point can return "
            "several records for the same ground.",
            {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "out_fields": {"type": "string"},
                "limit": {"type": "integer"},
            },
            {
                "returned_count": {"type": "integer"},
                "by_category": {
                    "type": "object",
                    "description": "Record count per GIS_Category.",
                    "additionalProperties": {"type": "integer"},
                },
            },
            rows_payload,
        )

        query_output = _envelope_schema(
            "Records matching an arbitrary WHERE clause, with the total "
            "match count and paging state.",
            {
                "where": {"type": "string"},
                "requested_where": {"type": "string"},
                "category": {"type": ["string", "null"]},
                "out_fields": {"type": "string"},
                "order_by": {"type": ["string", "null"]},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            {
                "returned_count": {"type": "integer"},
                "total_count": _NULLABLE_COUNT,
                "truncated": {"type": "boolean"},
                "next_offset": {
                    "type": ["integer", "null"],
                    "description": "Null when there is no further page.",
                },
                "exceeded_transfer_limit": {"type": "boolean"},
            },
            rows_payload,
        )

        stats_output = _envelope_schema(
            "One statistic over the matching parcels, optionally grouped.",
            {
                "stat_type": {"type": "string"},
                "stat_field": {"type": "string"},
                "group_by": {"type": ["string", "null"]},
                "percentile": {"type": ["number", "null"]},
                "where": {"type": "string"},
                "category": {"type": ["string", "null"]},
            },
            {"group_count": {"type": "integer"}},
            {
                "rows": {
                    "type": "array",
                    "description": "One entry per group (one entry total when ungrouped).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "group": {
                                "type": ["string", "number", "boolean", "null"],
                                "description": (
                                    "The group_by value, null when "
                                    "ungrouped. Its type follows the "
                                    "grouped field -- grouping on a "
                                    "numeric field yields numbers."
                                ),
                            },
                            "value": {
                                "type": ["number", "string", "boolean", "null"],
                                "description": (
                                    "RAW statistic value, unformatted. A "
                                    "STRING for min/max over a text field, "
                                    "and null when the statistic is "
                                    "undefined for the group (e.g. avg "
                                    "over no non-null values) -- which is "
                                    "NOT the same as zero."
                                ),
                            },
                        },
                        "required": ["group", "value"],
                        "additionalProperties": True,
                    },
                }
            },
        )

        return [
            ToolDefinition(
                name="find_parcel",
                output_schema=find_parcel_output,
                title="Find Parcel by Number",
                description=(
                    f"Look up {city} parcels by parcel number in ANY of "
                    f"the four MOA formats -- '002-151-32', '00215132', "
                    f"'00215132000', or '002-151-32-000' -- with a "
                    f"fuzzy fallback when nothing matches exactly. "
                    f"Example: find_parcel(parcel_id='002-151-32') "
                    f"returns the parcel at 144 W 15TH AVE with owner "
                    f"and assessed value. {summary_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "parcel_id": {
                            "type": "string",
                            "description": (
                                "Parcel number in any format (hyphens "
                                "optional, 8 or 11 digits)"
                            ),
                        },
                        "category": category_schema,
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names or '*'; "
                                "defaults to the SUMMARY set. " + CASE_SENSITIVE_NOTE
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "default": 10,
                            "description": "Max records (1-100)",
                        },
                    },
                    "required": ["parcel_id"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="get_parcel_details",
                output_schema=details_output,
                title="Parcel Detail Report",
                description=(
                    f"Full assessment record for one {city} parcel: "
                    f"identity/legal, situs, owner mailing address, "
                    f"valuation (current + 2 prior years), exemptions, "
                    f"deed/plat, and the assessor Datalet link. If the "
                    f"ID matches multiple records (condo units share "
                    f"parcel roots) it lists them so you can pick the "
                    f"unit. Example: "
                    f"get_parcel_details(parcel_id='00215132000'). "
                    f"{CASE_SENSITIVE_NOTE}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "parcel_id": {
                            "type": "string",
                            "description": (
                                "Parcel number in any format (hyphens "
                                "optional, 8 or 11 digits)"
                            ),
                        },
                    },
                    "required": ["parcel_id"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="search_by_owner",
                output_schema=owner_output,
                title="Search Parcels by Owner",
                description=(
                    f"Find {city} parcels by owner name (substring "
                    f"match, case-insensitive), ordered by appraised "
                    f"value (highest first). Results are public-record "
                    f"assessment data published by the Municipality. "
                    f"Example: search_by_owner(name='municipality of "
                    f"anchorage') lists MOA-owned parcels. {summary_note} "
                    f"{unassessed_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Owner name or fragment (case-"
                                "insensitive; stored UPPERCASE)"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "default": 20,
                            "description": "Max records (1-1000)",
                        },
                        "category": category_schema,
                        "include_unassessed": include_unassessed_schema,
                    },
                    "required": ["name"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="search_by_address",
                output_schema=address_output,
                title="Search Parcels by Address",
                description=(
                    f"Find {city} parcels by street address. Tries the "
                    f"parcel situs address first; on zero hits it "
                    f"resolves the address via the municipal address-"
                    f"point layer and runs point-in-polygon against the "
                    f"property layer (the response says which path was "
                    f"used). Example: search_by_address(address='144 W "
                    f"15th Ave'). Use abbreviated street types (AVE, "
                    f"ST, DR). {summary_note} {unassessed_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "address": {
                            "type": "string",
                            "description": (
                                "Street address or fragment (case-"
                                "insensitive; stored UPPERCASE, e.g. "
                                "'144 W 15TH AVE')"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "default": 10,
                            "description": "Max records (1-100)",
                        },
                        "include_unassessed": include_unassessed_schema,
                    },
                    "required": ["address"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="parcels_at_point",
                output_schema=point_output,
                title="Parcels at Coordinates",
                description=(
                    f"Find every {city} property record whose polygon "
                    f"contains a WGS84 point -- Parcel, Lease, and "
                    f"Economic polygons can stack at one spot, and each "
                    f"hit is labeled with its category. Example: "
                    f"parcels_at_point(lat=61.2091, lon=-149.8944). "
                    f"{CASE_SENSITIVE_NOTE} {unassessed_note} {routing_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "lat": {
                            "type": "number",
                            "description": "Latitude, WGS84 decimal degrees",
                        },
                        "lon": {
                            "type": "number",
                            "description": (
                                "Longitude, WGS84 decimal degrees "
                                "(negative in Anchorage, ~-149.9)"
                            ),
                        },
                        "include_unassessed": include_unassessed_schema,
                    },
                    "required": ["lat", "lon"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="query_parcels",
                output_schema=query_output,
                title="Query Parcels",
                description=(
                    f"Escape hatch: run a SQL WHERE clause against the "
                    f"{city} property layer with pagination. Example: "
                    f"query_parcels(where=\"Zoning_District='RO' AND "
                    f'Appraised_Total_Value > 1000000", order_by='
                    f"'Appraised_Total_Value DESC'). Text values are "
                    f"stored UPPERCASE; {CASE_SENSITIVE_NOTE} "
                    f"{summary_note} {unassessed_note} {routing_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause ('1=1' matches "
                                "everything). " + CASE_SENSITIVE_NOTE
                            ),
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names or '*'; "
                                "defaults to the SUMMARY set."
                            ),
                        },
                        "category": category_schema,
                        "include_unassessed": include_unassessed_schema,
                        "limit": {
                            "type": "integer",
                            "default": 100,
                            "description": "Max records this call (1-1000)",
                        },
                        "offset": {
                            "type": "integer",
                            "default": 0,
                            "description": ("Skip this many records (pagination)"),
                        },
                        "order_by": {
                            "type": "string",
                            "description": ("e.g. 'Appraised_Total_Value DESC'"),
                        },
                    },
                    "required": ["where"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="parcel_stats",
                output_schema=stats_output,
                title="Parcel Statistics",
                description=(
                    f"Aggregate statistics over {city} property records: "
                    f"count, sum, avg, min, max, stddev, var, or "
                    f"percentile_cont (median = percentile_cont with "
                    f"percentile=0.5), optionally grouped. Example -- "
                    f"median assessed value by zoning district: "
                    f"parcel_stats(stat_type='percentile_cont', "
                    f"stat_field='Appraised_Total_Value', "
                    f"group_by='Zoning_District'). {CASE_SENSITIVE_NOTE} "
                    f"{unassessed_note} {routing_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "stat_type": {
                            "type": "string",
                            "enum": list(self.STAT_TYPES),
                            "description": (
                                "Statistic to compute; percentile_cont "
                                "uses the `percentile` parameter "
                                "(default 0.5 = median)"
                            ),
                        },
                        "stat_field": {
                            "type": "string",
                            "description": (
                                "Field to aggregate, e.g. "
                                "'Appraised_Total_Value'. " + CASE_SENSITIVE_NOTE
                            ),
                        },
                        "group_by": {
                            "type": "string",
                            "description": (
                                "Optional field to group by, e.g. "
                                "'Zoning_District' or 'GIS_Category'"
                            ),
                        },
                        "where": {
                            "type": "string",
                            "description": (
                                "Optional SQL WHERE filter applied before aggregating"
                            ),
                        },
                        "category": category_schema,
                        "include_unassessed": include_unassessed_schema,
                        "percentile": {
                            "type": "number",
                            "default": 0.5,
                            "description": (
                                "For percentile_cont only: 0-1 (0.5 = median)"
                            ),
                        },
                    },
                    "required": ["stat_type", "stat_field"],
                },
                annotations=annotations,
            ),
        ]

    # ── Dispatch ──────────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        handlers = {
            "find_parcel": self._find_parcel,
            "get_parcel_details": self._get_parcel_details,
            "search_by_owner": self._search_by_owner,
            "search_by_address": self._search_by_address,
            "parcels_at_point": self._parcels_at_point,
            "query_parcels": self._query_parcels,
            "parcel_stats": self._parcel_stats,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                content=[],
                success=False,
                error_message=f"Unknown tool: {tool_name}",
            )
        try:
            output = await handler(arguments)
            return ToolResult(
                content=[
                    {
                        "type": "text",
                        "text": self._with_retrieved_footer(output.text),
                    }
                ],
                structured_content=output.structured,
                success=True,
            )
        except ToolInputError as e:
            # The caller passed something invalid. WARNING, no traceback:
            # a stack trace here is noise that buries real faults, and the
            # message alone already tells the caller how to fix the call.
            logger.warning(f"Invalid arguments for tool {tool_name}: {e}")
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Invalid tool arguments",
            )
        except Exception as e:
            # Everything else IS a server or upstream fault -- keep the
            # traceback, that is what these logs are for.
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )
