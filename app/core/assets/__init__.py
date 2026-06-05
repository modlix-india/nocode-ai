"""Asset normalization subsystem for vision-LLM agents.

Replaces ad-hoc per-agent image-fetch code (e.g.
`agents/adzump/agents/product/product_assets._fetch_one`) with a uniform
pipeline: AssetRef → AssetAdapter → AssetView → multimodal content blocks.

The invariant at the model boundary: the LLM only ever receives
`{jpeg, png, gif, webp}` regardless of what the source format was.
SVGs are rasterized; everything else is normalized through PIL.

Public surface:

- `AssetRef`         · opaque pointer to source bytes (URL / bytes / local)
- `AssetView`        · prepared form (raw bytes + display block + metadata)
- `AssetAdapter`     · async protocol: `prepare(ref, target) -> AssetView`
- `AssetPipeline`    · `prepare_many(refs, target) -> (views, dropped)`
- `AssetStore`       · sha256-keyed cache of prepared views
- `MultimodalAdapter`· provider-specific block-shape generation

See `plans/llm-tracing/asset-subsystem-design.html` for the full design.
"""

from app.core.assets.refs import AssetRef, RemoteUrl, Bytes, Local
from app.core.assets.views import AssetView, RenderTarget
from app.core.assets.pipeline import AssetPipeline, DroppedAsset
from app.core.assets.store import AssetStore
from app.core.assets.adapters.base import AssetAdapter, register_adapter

__all__ = [
    "AssetRef",
    "RemoteUrl",
    "Bytes",
    "Local",
    "AssetView",
    "RenderTarget",
    "AssetPipeline",
    "DroppedAsset",
    "AssetStore",
    "AssetAdapter",
    "register_adapter",
]
