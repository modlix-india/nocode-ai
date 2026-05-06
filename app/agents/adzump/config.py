"""Agent-level credential config for adzump.

Sourced from ``ai.adzump.*`` in the nocode-saas config server at startup,
with env-var overrides for local dev. Per-user OAuth tokens are NOT here —
those come from the connection service per request.

Access via ``get_adzump_config()``. Models are frozen; ``load_from_config_server``
replaces the singleton wholesale on reload.
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict


class GoogleAdsCreds(BaseModel):
    model_config = ConfigDict(frozen=True)

    developer_token: str | None = None
    # Direct access token (short-lived). Overrides the refresh/connection-service
    # path entirely. Useful for local dev when you want to paste a fresh token.
    access_token: str | None = None
    refresh_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None


class MetaCreds(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str | None = None


class AdzumpConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    google_ads: GoogleAdsCreds = GoogleAdsCreds()
    meta: MetaCreds = MetaCreds()


_adzump_config = AdzumpConfig()


def get_adzump_config() -> AdzumpConfig:
    return _adzump_config


def load_from_config_server(raw: dict) -> None:
    """Rebuild the singleton from the ``ai.adzump.*`` block of the config payload.

    Env-var overrides win over config-server values (local-dev escape hatch).
    Empty strings and missing keys both resolve to None.
    """
    global _adzump_config
    adzump = (raw or {}).get("adzump") or {}
    gads = adzump.get("googleAds") or {}
    meta = adzump.get("meta") or {}

    _adzump_config = AdzumpConfig(
        google_ads=GoogleAdsCreds(
            developer_token=os.getenv("ADZUMP_GOOGLE_ADS_DEVELOPER_TOKEN") or gads.get("developerToken") or None,
            access_token=os.getenv("ADZUMP_GOOGLE_ADS_ACCESS_TOKEN") or gads.get("accessToken") or None,
            refresh_token=os.getenv("ADZUMP_GOOGLE_ADS_REFRESH_TOKEN") or gads.get("refreshToken") or None,
            client_id=os.getenv("ADZUMP_GOOGLE_ADS_CLIENT_ID") or gads.get("clientId") or None,
            client_secret=os.getenv("ADZUMP_GOOGLE_ADS_CLIENT_SECRET") or gads.get("clientSecret") or None,
        ),
        meta=MetaCreds(
            access_token=os.getenv("ADZUMP_META_ACCESS_TOKEN") or meta.get("accessToken") or None,
        ),
    )
