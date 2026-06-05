"""Adapters registry — one adapter per source MIME family.

Adapters self-register at import time via `register_adapter(mime, adapter)`.
The pipeline resolves MIME → adapter at dispatch time; unknown MIME =
asset dropped with reason `unsupported_format`.
"""

from app.core.assets.adapters.base import AssetAdapter, register_adapter

# Side-effecting imports: each module calls `register_adapter` on import.
from app.core.assets.adapters import raster  # noqa: F401
from app.core.assets.adapters import svg  # noqa: F401

__all__ = ["AssetAdapter", "register_adapter"]
