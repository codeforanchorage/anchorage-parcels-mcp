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
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.interfaces import MCPPlugin, PluginType, ToolDefinition, ToolResult
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

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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

    def _category_clause(self, category: Optional[str]) -> Optional[str]:
        """Build the GIS_Category filter; None means no filter."""
        if category is None:
            category = "Parcel"
        normalized = str(category).strip().title()
        if normalized == "All" or normalized == "":
            return None
        if normalized not in CATEGORY_VALUES:
            raise ValueError(
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
        requested = int(args.get("limit", default))
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
            raise ValueError(
                f"{arg_name} must be a single field name "
                f"(got {field!r}). {CASE_SENSITIVE_NOTE}"
            )
        if self._live_fields and field not in self._live_fields:
            suggestion = difflib.get_close_matches(
                field, sorted(self._live_fields), n=1, cutoff=0.6
            )
            hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
            raise ValueError(
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
        notices: Optional[List[str]] = None,
    ) -> str:
        """List records with provenance header, TOTAL COUNT line, and a
        truncation banner (conventions from anchorage_gis
        `_format_query_results`). Extra caller banners (e.g. the
        LIMIT CLAMPED notice) go in `notices`, right after TRUNCATED."""
        lines = self._provenance(where=where, out_fields=out_fields, limit=limit)
        truncated = (
            total_count is not None and len(records) > 0 and total_count > len(records)
        )
        if truncated:
            lines.append(
                f"**TRUNCATED:** returned {len(records)} of "
                f"{total_count:,} matching records (limit={limit}). The "
                f"records below are a SAMPLE -- do not generalize counts "
                f"or totals from them. Use the TOTAL COUNT line below for "
                f"'how many?' questions, or page with offset / narrow the "
                f"WHERE clause."
            )
        if notices:
            lines.extend(notices)
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

    async def _find_parcel(self, args: Dict[str, Any]) -> str:
        parcel_id = str(args.get("parcel_id") or "").strip()
        if not parcel_id:
            raise ValueError("parcel_id is required")
        limit, clamp_note = self._clamp_limit(args, default=10, maximum=100)
        out_fields = self._validate_out_fields(args.get("out_fields"))
        category_clause = self._category_clause(args.get("category"))

        variants = _normalize_parcel_variants(parcel_id)
        if not variants:
            raise ValueError(
                f"Could not extract a parcel number from {parcel_id!r}. "
                f"Provide an ID like '002-151-32', '00215132', "
                f"'00215132000', or '002-151-32-000'."
            )
        in_list = ",".join(self._sql_quote(v) for v in variants)
        exact_clause = " OR ".join(
            f"{f} IN ({in_list})" for f in self._parcel_id_fields
        )
        where = self._combine_where(f"({exact_clause})", category_clause)

        records, _ = await self._query_property_layer(where, out_fields, limit)
        if records:
            heading = (
                f"## Parcel lookup: `{parcel_id}` -> "
                f"{len(records)} record(s) (exact match)\n"
                f"Variants tried ({len(variants)}): "
                + ", ".join(f"`{v}`" for v in variants)
                + "\n"
            )
            return self._format_records(
                records,
                limit,
                where=where,
                out_fields=out_fields,
                heading=heading,
                notices=[clamp_note] if clamp_note else None,
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
            lines.append(
                f"**FUZZY candidates** (LIKE '%{substring}%' on "
                f"{self._f('parcel_id')} -- these are NOT exact matches, "
                f"verify before using):"
            )
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
        else:
            lines.append(
                "No fuzzy candidates either. The parcel may not exist, "
                "or it may be filed under a different category -- retry "
                "with category='All'."
            )
        return "\n".join(lines)

    # ── Tool: get_parcel_details ──────────────────────────────────────

    async def _get_parcel_details(self, args: Dict[str, Any]) -> str:
        parcel_id = str(args.get("parcel_id") or "").strip()
        if not parcel_id:
            raise ValueError("parcel_id is required")
        variants = _normalize_parcel_variants(parcel_id)
        if not variants:
            raise ValueError(
                f"Could not extract a parcel number from {parcel_id!r}. "
                f"Provide an ID like '002-151-32' or '00215132000'."
            )
        in_list = ",".join(self._sql_quote(v) for v in variants)
        where = (
            "("
            + " OR ".join(f"{f} IN ({in_list})" for f in self._parcel_id_fields)
            + ")"
        )

        records, _ = await self._query_property_layer(where, "*", 2)
        if not records:
            return (
                f"No property record found for `{parcel_id}`. "
                f"Try `find_parcel('{parcel_id}')` -- it adds a fuzzy "
                f"fallback and can search Lease/Economic records with "
                f"category='All'."
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
            lines = [
                f"`{parcel_id}` matches {total} records (condo "
                f"units and lease/economic overlays share parcel roots). "
                f"Which unit do you want? Pass the exact 11-digit "
                f"{self._f('parcel_id')} to get_parcel_details:",
                "",
            ]
            for r in records:
                lines.append(
                    f"- `{r.get(self._f('parcel_id'))}` "
                    f"[{r.get(self._f('category'))}] "
                    f"unit={r.get(self._f('condo_unit')) or '--'} -- "
                    f"{r.get(self._f('situs_address'))} -- "
                    f"{r.get(self._f('owner_name'))}"
                )
            if exceeded or (isinstance(total, int) and total > len(records)):
                lines.append("")
                lines.append(
                    f"**LIST TRUNCATED:** showing the first "
                    f"{len(records)} of {total} matching records. Page "
                    f"through the rest with query_parcels using the "
                    f"same parcel number and an offset."
                )
            return "\n".join(lines)

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
        return "\n".join(lines)

    # ── Tool: search_by_owner ─────────────────────────────────────────

    async def _search_by_owner(self, args: Dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("name is required")
        limit, clamp_note = self._clamp_limit(args, default=20, maximum=self.MAX_LIMIT)
        category_clause = self._category_clause(args.get("category"))
        owner_field = self._f("owner_name")
        needle = name.upper().replace("'", "''")
        where = self._combine_where(
            f"UPPER({owner_field}) LIKE '%{needle}%'", category_clause
        )
        order_by = f"{self._f('appraised_total')} DESC"
        records_task = self._query_property_layer(
            where, self._summary_fields, limit, order_by=order_by
        )
        (records, _), total = await asyncio.gather(
            records_task, self._fetch_count(where)
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
            notices=[clamp_note] if clamp_note else None,
        )
        if not records:
            text += self._no_data_hint(where)
        return text

    # ── Tool: search_by_address ───────────────────────────────────────

    async def _search_by_address(self, args: Dict[str, Any]) -> str:
        address = str(args.get("address") or "").strip()
        if not address:
            raise ValueError("address is required")
        limit, clamp_note = self._clamp_limit(args, default=10, maximum=100)
        needle = address.upper().replace("'", "''")
        situs_field = self._f("situs_address")
        where = f"{situs_field} LIKE '%{needle}%'"

        records, _ = await self._query_property_layer(
            where, self._summary_fields, limit
        )
        if records:
            return self._format_records(
                records,
                limit,
                where=where,
                out_fields=self._summary_fields,
                heading=(
                    f"## Parcels matching address `{address}` "
                    f"(matched directly on {situs_field})\n"
                ),
                notices=[clamp_note] if clamp_note else None,
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
            return (
                f"No parcel has a situs address matching `{address}`, and "
                f"the address-point layer has no match either.\n"
                f"Hints: use the abbreviated street type ('AVE', 'ST', "
                f"'DR'); drop unit/apartment numbers; addresses are "
                f"stored UPPERCASE (handled automatically); try just the "
                f"house number + street name (e.g. '144 W 15TH')."
            )

        point = addr_feats[0].get("geometry") or {}
        matched_addr = (addr_feats[0].get("attributes") or {}).get(addr_field)
        lon, lat = point.get("x"), point.get("y")
        if lon is None or lat is None:
            raise RuntimeError(
                "Address point found but no usable geometry returned; "
                "retry, or use parcels_at_point with known coordinates."
            )
        pip_records = await self._parcels_intersecting_point(lat, lon)
        lines = [
            f"## Parcels at address `{address}`",
            f"No direct {situs_field} match; resolved via the "
            f"address-point layer (matched `{matched_addr}`, point "
            f"lon={lon:.6f} lat={lat:.6f}) -> point-in-polygon on the "
            f"property layer.",
        ]
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
            lines.append(
                "The address point falls inside no property polygon -- "
                "it may sit on a right-of-way or unassessed land."
            )
            return "\n".join(lines)
        body = self._format_records(
            pip_records,
            limit,
            out_fields=self._summary_fields,
            heading=None,
            notices=[clamp_note] if clamp_note else None,
        )
        return "\n".join(lines) + "\n" + body

    # ── Tool: parcels_at_point ────────────────────────────────────────

    async def _parcels_intersecting_point(
        self, lat: float, lon: float
    ) -> List[Dict[str, Any]]:
        """Point-in-polygon on the property layer, all categories."""
        data = await self._layer_query(
            f"{self.plugin_config.property_layer_url}/query",
            {
                "f": "json",
                "where": "1=1",
                "outFields": self._summary_fields,
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "returnGeometry": "false",
                "resultRecordCount": "20",
            },
        )
        return [f.get("attributes") or {} for f in data.get("features") or []]

    async def _parcels_at_point(self, args: Dict[str, Any]) -> str:
        try:
            lat = float(args["lat"])
            lon = float(args["lon"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(
                "lat and lon are required numbers (WGS84 decimal degrees, "
                "e.g. lat=61.209, lon=-149.894)"
            )
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise ValueError(
                f"lat/lon out of range (got lat={lat}, lon={lon}). "
                f"lat must be in [-90, 90] and lon in [-180, 180]; "
                f"Anchorage is roughly lat 61, lon -149."
            )
        records = await self._parcels_intersecting_point(lat, lon)
        lines = self._provenance(
            where=f"point intersect lon={lon}, lat={lat} (all categories)",
            out_fields=self._summary_fields,
            limit=20,
        )
        lines.append("")
        if not records:
            lines.append(
                f"No property polygons contain point (lat={lat}, "
                f"lon={lon}). The point may be on a right-of-way, water, "
                f"or outside the Municipality of Anchorage."
            )
            return "\n".join(lines)
        cat_field = self._f("category")
        counts: Dict[str, int] = {}
        for r in records:
            counts[str(r.get(cat_field))] = counts.get(str(r.get(cat_field)), 0) + 1
        lines.append(
            f"## {len(records)} record(s) contain point (lat={lat}, lon={lon})"
        )
        lines.append(
            "Parcel, Lease, and Economic polygons can STACK at one "
            "point -- each hit is labeled with its category. By category: "
            + ", ".join(f"{k}={n}" for k, n in sorted(counts.items()))
        )
        lines.append("")
        for i, r in enumerate(records, 1):
            lines.append(f"Record {i} [{r.get(cat_field)}]:")
            for key, value in r.items():
                lines.append(f"  {key}: {self._render_value(key, value)}")
            lines.append("")
        return "\n".join(lines)

    # ── Tool: query_parcels ───────────────────────────────────────────

    async def _query_parcels(self, args: Dict[str, Any]) -> str:
        raw_where = str(args.get("where") or "").strip()
        if not raw_where:
            raise ValueError("where is required (use '1=1' to match everything)")
        where = WhereValidator.validate(raw_where)
        WhereValidator.validate_against_schema(where, self._live_fields)
        out_fields = self._validate_out_fields(args.get("out_fields"))
        order_by = OrderByValidator.validate(str(args.get("order_by") or ""))
        limit, clamp_note = self._clamp_limit(args, default=100, maximum=self.MAX_LIMIT)
        if clamp_note:
            clamp_note += " Page with offset= for more."
        offset = max(0, int(args.get("offset", 0)))
        category_clause = self._category_clause(args.get("category"))
        full_where = self._combine_where(where, category_clause)

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
        text = self._format_records(
            records,
            limit,
            total_count=total,
            where=full_where,
            out_fields=out_fields,
            notices=[clamp_note] if clamp_note else None,
        )
        remaining = None
        if total is not None:
            remaining = total - (offset + len(records))
        if (remaining is not None and remaining > 0) or (
            remaining is None and exceeded
        ):
            next_offset = offset + len(records)
            text += (
                f"\n**MORE PAGES AVAILABLE:** call query_parcels again "
                f"with offset={next_offset} (same where/order_by) for the "
                f"next page."
            )
        if not records:
            text += self._no_data_hint(full_where)
        return text

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

    async def _parcel_stats(self, args: Dict[str, Any]) -> str:
        stat_type = str(args.get("stat_type") or "").strip().lower()
        if stat_type not in self.STAT_TYPES:
            raise ValueError(
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
        full_where = self._combine_where(where, category_clause)

        out_name = f"{stat_type}_{stat_field}"
        stat_entry: Dict[str, Any] = {
            "statisticType": stat_type,
            "onStatisticField": stat_field,
            "outStatisticFieldName": out_name,
        }
        percentile = None
        if stat_type == "percentile_cont":
            percentile = float(args.get("percentile", 0.5))
            if not (0.0 <= percentile <= 1.0):
                raise ValueError(
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
        lines = self._provenance(where=full_where)
        lines += [
            "",
            f"## {stat_label} of {stat_field}"
            + (f" by {group_by}" if group_by else ""),
            "",
        ]
        if not rows:
            lines.append("No statistics returned (0 matching records).")
            return "\n".join(lines) + self._no_data_hint(full_where)
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
        return "\n".join(lines)

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
        return [
            ToolDefinition(
                name="find_parcel",
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
                title="Search Parcels by Owner",
                description=(
                    f"Find {city} parcels by owner name (substring "
                    f"match, case-insensitive), ordered by appraised "
                    f"value (highest first). Results are public-record "
                    f"assessment data published by the Municipality. "
                    f"Example: search_by_owner(name='municipality of "
                    f"anchorage') lists MOA-owned parcels. {summary_note}"
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
                    },
                    "required": ["name"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="search_by_address",
                title="Search Parcels by Address",
                description=(
                    f"Find {city} parcels by street address. Tries the "
                    f"parcel situs address first; on zero hits it "
                    f"resolves the address via the municipal address-"
                    f"point layer and runs point-in-polygon against the "
                    f"property layer (the response says which path was "
                    f"used). Example: search_by_address(address='144 W "
                    f"15th Ave'). Use abbreviated street types (AVE, "
                    f"ST, DR). {summary_note}"
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
                    },
                    "required": ["address"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="parcels_at_point",
                title="Parcels at Coordinates",
                description=(
                    f"Find every {city} property record whose polygon "
                    f"contains a WGS84 point -- Parcel, Lease, and "
                    f"Economic polygons can stack at one spot, and each "
                    f"hit is labeled with its category. Example: "
                    f"parcels_at_point(lat=61.2091, lon=-149.8944). "
                    f"{CASE_SENSITIVE_NOTE} {routing_note}"
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
                    },
                    "required": ["lat", "lon"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="query_parcels",
                title="Query Parcels",
                description=(
                    f"Escape hatch: run a SQL WHERE clause against the "
                    f"{city} property layer with pagination. Example: "
                    f"query_parcels(where=\"Zoning_District='RO' AND "
                    f'Appraised_Total_Value > 1000000", order_by='
                    f"'Appraised_Total_Value DESC'). Text values are "
                    f"stored UPPERCASE; {CASE_SENSITIVE_NOTE} "
                    f"{summary_note} {routing_note}"
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
                    f"{routing_note}"
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
            text = await handler(arguments)
            return ToolResult(
                content=[{"type": "text", "text": self._with_retrieved_footer(text)}],
                success=True,
            )
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )
