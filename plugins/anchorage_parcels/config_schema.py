"""Pydantic configuration schema for the Anchorage Parcels plugin."""

from typing import Dict
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Municipality of Anchorage defaults (muniorg.maps.arcgis.com / MOAGIS,
# org Ce3DhLRthdwbHlfF). All public, no auth. Override any of these in
# config.yaml to point this plugin at another city's assessor layers.
DEFAULT_PROPERTY_LAYER_URL = (
    "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/"
    "PropertyInformation_Hosted/FeatureServer/0"
)
DEFAULT_ADDRESSES_LAYER_URL = (
    "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/"
    "Addresses_Hosted/FeatureServer/0"
)
DEFAULT_SUBDIVISIONS_LAYER_URL = (
    "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/"
    "Subdivisions_Hosted/FeatureServer/0"
)


class AnchorageParcelsPluginConfig(BaseModel):
    """Configuration schema for the Anchorage Parcels plugin.

    Validates plugin configuration for the MOA property/assessment
    Feature Layers. To fork this plugin for another city, override the
    three layer URLs and ``field_map`` here -- no code changes needed
    beyond the field map (see ``DEFAULT_FIELD_MAP`` in plugin.py).
    """

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    property_layer_url: str = Field(
        default=DEFAULT_PROPERTY_LAYER_URL,
        description=(
            "Feature Layer URL for the parcel/assessment polygon layer "
            "(ArcGIS FeatureServer layer endpoint, .../FeatureServer/<n>)"
        ),
    )
    addresses_layer_url: str = Field(
        default=DEFAULT_ADDRESSES_LAYER_URL,
        description=(
            "Feature Layer URL for the address-point layer used as the "
            "address->parcel resolution fallback"
        ),
    )
    subdivisions_layer_url: str = Field(
        default=DEFAULT_SUBDIVISIONS_LAYER_URL,
        description="Feature Layer URL for the subdivisions polygon layer",
    )
    city_name: str = Field(
        default="Municipality of Anchorage",
        description="Name of the city/municipality (used in tool text)",
    )
    field_map: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Overrides for the logical->physical field-name map. Keys are "
            "the logical names in DEFAULT_FIELD_MAP (plugin.py); values are "
            "your layer's field names. Only list the fields that differ."
        ),
    )
    timeout: int = Field(
        default=30, ge=1, le=300, description="HTTP request timeout in seconds"
    )

    @field_validator(
        "property_layer_url", "addresses_layer_url", "subdivisions_layer_url"
    )
    @classmethod
    def validate_layer_url(cls, v: str) -> str:
        """Validate that a layer URL is a well-formed http(s) URL."""
        if not v:
            raise ValueError("layer URL cannot be empty")
        try:
            result = urlparse(v)
            if not result.scheme or not result.netloc:
                raise ValueError("URL must include scheme (http/https) and hostname")
            if result.scheme not in ("http", "https"):
                raise ValueError("URL scheme must be http or https")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"Invalid URL format: {e}")
        return v.rstrip("/")

    model_config = ConfigDict(extra="forbid")
