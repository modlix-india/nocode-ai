"""AssetRef — opaque pointer to source bytes.

A discriminated union of three pointer kinds. The pipeline + adapters
operate on `AssetRef` so callers don't have to special-case where bytes
come from (URL fetch vs already-in-memory vs on-disk).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


@dataclass(frozen=True, slots=True)
class RemoteUrl:
    """Remote URL the pipeline will fetch via httpx."""
    url: str
    # Free-form provenance string for logs/dropped reasons. Mirrors the
    # `SiteImage.source` axis used today (jsonld / og / img / network / …).
    origin: str = ""
    # Optional caller-supplied metadata that flows through to AssetView.meta.
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Bytes:
    """Already-in-memory bytes. content_type required (no sniffing here)."""
    data: bytes
    content_type: str
    origin: str = ""
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Local:
    """Local filesystem path. content_type optional; pipeline sniffs from suffix."""
    path: str
    content_type: str = ""
    origin: str = ""
    meta: dict = field(default_factory=dict)


AssetRef = Union[RemoteUrl, Bytes, Local]
