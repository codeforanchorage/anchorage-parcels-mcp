"""Pydantic configuration schema for Anchorage GIS plugin."""

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnchorageGISPluginConfig(BaseModel):
    """Configuration schema for Anchorage GIS plugin.

    This schema validates plugin configuration for accessing the
    Municipality of Anchorage ArcGIS Portal REST API.
    """

    enabled: bool = Field(default=False, description="Whether plugin is enabled")
    portal_base_url: str = Field(
        ...,
        description=(
            "Base URL of ArcGIS Portal REST API "
            "(e.g., https://muniorg.maps.arcgis.com/sharing/rest)"
        ),
    )
    gallery_group_id: str = Field(
        ..., description="ArcGIS group ID for the curated public gallery"
    )
    org_id: str = Field(..., description="ArcGIS organization ID")
    city_name: str = Field(..., description="Name of the city/municipality")
    gallery_url: str = Field(..., description="Public URL for the GIS gallery app")
    timeout: int = Field(
        default=30, ge=1, le=300, description="HTTP request timeout in seconds"
    )

    @field_validator("portal_base_url")
    @classmethod
    def validate_portal_url(cls, v: str) -> str:
        """Validate that portal URL is well-formed."""
        if not v:
            raise ValueError("portal_base_url cannot be empty")
        try:
            result = urlparse(v)
            if not result.scheme or not result.netloc:
                raise ValueError("URL must include scheme (http/https) and hostname")
            if result.scheme not in ("http", "https"):
                raise ValueError("URL scheme must be http or https")
        except Exception as e:
            raise ValueError(f"Invalid URL format: {e}")
        return v.rstrip("/")

    @field_validator("gallery_url")
    @classmethod
    def validate_gallery_url(cls, v: str) -> str:
        """Validate that gallery URL is well-formed."""
        if not v:
            raise ValueError("gallery_url cannot be empty")
        try:
            result = urlparse(v)
            if not result.scheme or not result.netloc:
                raise ValueError("URL must include scheme (http/https) and hostname")
        except Exception as e:
            raise ValueError(f"Invalid gallery URL format: {e}")
        return v

    model_config = ConfigDict(extra="forbid")
