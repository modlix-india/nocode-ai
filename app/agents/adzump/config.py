"""Adzump's agent-level ad-platform credentials.

Each field resolves env var first (local-dev override), then the config
server's ``ai.adzump.<platform>`` block, else None. Both names derive from
the field name:

    GoogleAdsCredentials.developer_token
        env     ADZUMP_GOOGLE_ADS_DEVELOPER_TOKEN
        config  ai.adzump.googleAds.developerToken

Per-user OAuth tokens are not here - they come from the connection service
per request. Read via ``get_adzump_config()``; ``load_adzump_config()`` swaps
the frozen singleton wholesale.
"""

from __future__ import annotations

import os
from typing import ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _PlatformCredentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    ENV_PREFIX: ClassVar[str]
    CONFIG_KEY: ClassVar[str]

    @classmethod
    def resolve(cls, adzump_block: dict) -> Self:
        """Env var, then config value, per field; blank counts as unset."""
        block = adzump_block.get(cls.CONFIG_KEY) or {}
        return cls(**{
            field: os.getenv(cls.ENV_PREFIX + field.upper()) or block.get(to_camel(field)) or None
            for field in cls.model_fields
        })


class GoogleAdsCredentials(_PlatformCredentials):
    ENV_PREFIX = "ADZUMP_GOOGLE_ADS_"
    CONFIG_KEY = "googleAds"

    developer_token: str | None = Field(default=None, repr=False)
    # Short-lived token pasted for local dev; skips the refresh + connection-service path.
    access_token: str | None = Field(default=None, repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    client_id: str | None = None
    client_secret: str | None = Field(default=None, repr=False)


class MetaCredentials(_PlatformCredentials):
    ENV_PREFIX = "ADZUMP_META_"
    CONFIG_KEY = "meta"

    access_token: str | None = Field(default=None, repr=False)


class AdzumpConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    google_ads: GoogleAdsCredentials = GoogleAdsCredentials()
    meta: MetaCredentials = MetaCredentials()


_adzump_config = AdzumpConfig()


def get_adzump_config() -> AdzumpConfig:
    return _adzump_config


def load_adzump_config(server_config: dict | None) -> None:
    """Rebuild from the config server payload's ``adzump`` block ({} = env only)."""
    global _adzump_config
    adzump_block = (server_config or {}).get("adzump") or {}
    _adzump_config = AdzumpConfig(
        google_ads=GoogleAdsCredentials.resolve(adzump_block),
        meta=MetaCredentials.resolve(adzump_block),
    )
