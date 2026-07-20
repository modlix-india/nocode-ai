"""Local image manipulation — crop, pad, convert, trim, composite, recolor,
favicon, filter.

Pillow-based "edit in place" — you give a local file path, the tool writes
to another local file path, you upload via upload_static_asset when ready.
Nothing inline — keeps bytes off the conversation context.

8 tools ported from modlix-mcp/modlix_mcp/tools/image_ops.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult


# Shared constants — keeps Sonar quiet about literal duplication.
_DESC_LOCAL_PATH = "Path to source image on local filesystem"
_DESC_OUTPUT_DEFAULT = "Where to write the result. Defaults next to source."


def _resolve_paths(local_path: str, output_path: str | None, default_suffix: str) -> tuple[Path | None, Path | None, str]:
    """Validate input + pick output path. Returns (in, out, error)."""
    src = Path(local_path).expanduser().resolve()
    if not src.exists():
        return None, None, f"local file not found: {src}"
    if not src.is_file():
        return None, None, f"not a regular file: {src}"
    if output_path:
        out = Path(output_path).expanduser().resolve()
    else:
        out = src.with_name(f"{src.stem}.{default_suffix}{src.suffix}")
    out.parent.mkdir(parents=True, exist_ok=True)
    return src, out, ""


def _parse_color(c: str) -> tuple[int, int, int, int]:
    """Accept hex / rgb() / rgba() / Pillow color names → (R, G, B, A)."""
    from PIL import ImageColor
    try:
        rgb = ImageColor.getrgb(c.strip())
    except ValueError as e:
        raise ValueError(f"unrecognized color {c!r}: {e}") from e
    if len(rgb) == 3:
        return (*rgb, 255)
    return rgb


def _format_for_path(path: Path) -> str:
    ext = path.suffix.lower().lstrip(".")
    return {"jpg": "jpeg"}.get(ext, ext) or "png"


# ── crop_image ───────────────────────────────────────────────────────────


async def _execute_crop_image(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    src, out, err = _resolve_paths(local_path, params.get("output_path"), "cropped")
    if err:
        return ToolResult(success=False, error=err)
    assert src and out

    from PIL import Image
    try:
        img = Image.open(src)
        img.load()
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"opening {src}: {e}")

    left, top, right, bottom = params.get("left"), params.get("top"), params.get("right"), params.get("bottom")
    aspect_ratio = (params.get("aspect_ratio") or "").strip() or None
    rect_set = any(v is not None for v in (left, top, right, bottom))
    if rect_set and aspect_ratio:
        return ToolResult(success=False, error="Pass EITHER rect (left/top/right/bottom) OR aspect_ratio, not both.")

    if rect_set:
        l = int(left or 0)
        t = int(top or 0)
        r = int(right) if right is not None else img.width
        b = int(bottom) if bottom is not None else img.height
        if l < 0 or t < 0 or r > img.width or b > img.height or r <= l or b <= t:
            return ToolResult(success=False, error=f"invalid rect ({l},{t},{r},{b}) for image {img.width}x{img.height}")
        box = (l, t, r, b)
    elif aspect_ratio:
        try:
            aw, ah = (int(x.strip()) for x in aspect_ratio.split(":"))
            if aw <= 0 or ah <= 0:
                raise ValueError("non-positive")
        except (ValueError, IndexError):
            return ToolResult(success=False, error=f"aspect_ratio must be 'W:H' with positive ints, got {aspect_ratio!r}")
        src_ratio = img.width / img.height
        tgt_ratio = aw / ah
        if tgt_ratio > src_ratio:
            new_h = round(img.width / tgt_ratio)
            off = (img.height - new_h) // 2
            box = (0, off, img.width, off + new_h)
        else:
            new_w = round(img.height * tgt_ratio)
            off = (img.width - new_w) // 2
            box = (off, 0, off + new_w, img.height)
    else:
        return ToolResult(success=False, error="must specify EITHER rect (left/top/right/bottom) OR aspect_ratio")

    cropped = img.crop(box)
    cropped.save(out, format=_format_for_path(out).upper())
    return ToolResult(success=True, summary=f"Cropped {src.name} {img.width}x{img.height} → {cropped.width}x{cropped.height}\n  rect: {box}\n  → {out}")


crop_image_tool = ToolDefinition(
    name="crop_image",
    description="Crop an image to an explicit rect (left/top/right/bottom) OR center-crop to a target aspect_ratio. Mutually exclusive.",
    parameters=[
        ToolParameter(name="local_path", type="string", description=_DESC_LOCAL_PATH),
        ToolParameter(name="output_path", type="string", required=False, description=_DESC_OUTPUT_DEFAULT),
        ToolParameter(name="left", type="integer", required=False, description="Rect left (px)"),
        ToolParameter(name="top", type="integer", required=False, description="Rect top (px)"),
        ToolParameter(name="right", type="integer", required=False, description="Rect right (px)"),
        ToolParameter(name="bottom", type="integer", required=False, description="Rect bottom (px)"),
        ToolParameter(name="aspect_ratio", type="string", required=False, description="'W:H' e.g. '16:9' — center-crops to this ratio"),
    ],
    execute=_execute_crop_image,
)


# ── pad_image_canvas ─────────────────────────────────────────────────────


_PAD_POSITIONS = ("center", "top-left", "top-right", "bottom-left", "bottom-right", "top", "bottom", "left", "right")


def _pad_position_xy(position: str, new_w: int, new_h: int, w: int, h: int) -> tuple[int, int]:
    positions = {
        "center":       ((new_w - w) // 2, (new_h - h) // 2),
        "top-left":     (0, 0),
        "top-right":    (new_w - w, 0),
        "bottom-left":  (0, new_h - h),
        "bottom-right": (new_w - w, new_h - h),
        "top":          ((new_w - w) // 2, 0),
        "bottom":       ((new_w - w) // 2, new_h - h),
        "left":         (0, (new_h - h) // 2),
        "right":        (new_w - w, (new_h - h) // 2),
    }
    return positions[position]


async def _execute_pad_image_canvas(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    src, out, err = _resolve_paths(local_path, params.get("output_path"), "padded")
    if err:
        return ToolResult(success=False, error=err)
    assert src and out
    from PIL import Image
    img = Image.open(src).convert("RGBA")

    tw, th = params.get("target_width"), params.get("target_height")
    target_aspect = (params.get("target_aspect") or "").strip() or None
    if target_aspect and (tw or th):
        return ToolResult(success=False, error="Pass EITHER target_aspect OR target_width+target_height")
    if target_aspect:
        try:
            aw, ah = (int(x.strip()) for x in target_aspect.split(":"))
        except (ValueError, IndexError):
            return ToolResult(success=False, error=f"target_aspect must be 'W:H', got {target_aspect!r}")
        src_ratio = img.width / img.height
        tgt_ratio = aw / ah
        if tgt_ratio > src_ratio:
            new_w, new_h = round(img.height * tgt_ratio), img.height
        else:
            new_w, new_h = img.width, round(img.width / tgt_ratio)
    elif tw and th:
        new_w, new_h = int(tw), int(th)
    else:
        return ToolResult(success=False, error="must specify target_aspect OR (target_width AND target_height)")

    if new_w < img.width or new_h < img.height:
        return ToolResult(success=False, error=f"target {new_w}x{new_h} smaller than source {img.width}x{img.height} in at least one dim; use crop_image instead")

    try:
        bg = _parse_color(params.get("background") or "transparent")
    except ValueError as e:
        return ToolResult(success=False, error=str(e))
    canvas = Image.new("RGBA", (new_w, new_h), bg)

    position = (params.get("position") or "center").strip()
    if position not in _PAD_POSITIONS:
        return ToolResult(success=False, error=f"position must be one of {_PAD_POSITIONS}, got {position!r}")
    canvas.paste(img, _pad_position_xy(position, new_w, new_h, img.width, img.height), img)

    if _format_for_path(out) in ("jpeg", "jpg"):
        flat = Image.new("RGB", canvas.size, bg[:3])
        flat.paste(canvas, mask=canvas.split()[3])
        canvas = flat
    canvas.save(out, format=_format_for_path(out).upper())
    return ToolResult(success=True, summary=f"Padded {src.name} {img.width}x{img.height} → {canvas.width}x{canvas.height} (pos={position}, bg={params.get('background')})\n  → {out}")


pad_image_canvas_tool = ToolDefinition(
    name="pad_image_canvas",
    description="Extend the canvas around an image, filling with a background color. Doesn't scale — only adds canvas. Use to put a 1:1 generation onto 16:9 with matching bg.",
    parameters=[
        ToolParameter(name="local_path", type="string", description=_DESC_LOCAL_PATH),
        ToolParameter(name="output_path", type="string", required=False, description=_DESC_OUTPUT_DEFAULT),
        ToolParameter(name="target_width", type="integer", required=False, description="Target canvas width (px)"),
        ToolParameter(name="target_height", type="integer", required=False, description="Target canvas height (px)"),
        ToolParameter(name="target_aspect", type="string", required=False, description="Alternative: 'W:H' aspect — pads shorter side to match"),
        ToolParameter(name="background", type="string", required=False, default="transparent", description="Pad color: hex, rgb(), rgba(), or Pillow name"),
        ToolParameter(name="position", type="string", required=False, default="center", description=f"Placement: one of {_PAD_POSITIONS}"),
    ],
    execute=_execute_pad_image_canvas,
)


# ── convert_image_format ─────────────────────────────────────────────────


async def _execute_convert_image_format(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    output_path = (params.get("output_path") or "").strip()
    if not local_path or not output_path:
        return ToolResult(success=False, error="`local_path` and `output_path` are required")
    src, out, err = _resolve_paths(local_path, output_path, "converted")
    if err:
        return ToolResult(success=False, error=err)
    assert src and out
    try:
        quality = max(1, min(int(params.get("quality") or 85), 100))
    except (TypeError, ValueError):
        quality = 85

    from PIL import Image
    img = Image.open(src)
    target = _format_for_path(out)
    if target not in ("png", "jpeg", "webp"):
        return ToolResult(success=False, error=f"output extension must be .png/.jpg/.jpeg/.webp (got {out.suffix})")

    if target == "jpeg":
        try:
            bg = _parse_color(params.get("background") or "#FFFFFF")[:3]
        except ValueError as e:
            return ToolResult(success=False, error=str(e))
        flat = Image.new("RGB", img.size, bg)
        if img.mode == "RGBA":
            flat.paste(img, mask=img.split()[3])
        else:
            flat.paste(img.convert("RGB"))
        flat.save(out, format="JPEG", quality=quality, optimize=True)
    elif target == "webp":
        img.save(out, format="WEBP", quality=quality, method=6)
    else:
        img.save(out, format="PNG", optimize=True)

    src_size, out_size = src.stat().st_size, out.stat().st_size
    return ToolResult(success=True, summary=f"Converted {src.name} → {out.name} ({target.upper()}, q={quality})\n  size: {src_size:,}B → {out_size:,}B ({(out_size/src_size)*100:.0f}%)")


convert_image_format_tool = ToolDefinition(
    name="convert_image_format",
    description="Convert between PNG/JPEG/WebP. WebP shrinks hero images by 60-80% at similar quality.",
    parameters=[
        ToolParameter(name="local_path", type="string", description=_DESC_LOCAL_PATH),
        ToolParameter(name="output_path", type="string", description="Output path with target extension"),
        ToolParameter(name="quality", type="integer", required=False, default=85, description="JPEG/WebP quality 1-100; PNG ignores"),
        ToolParameter(name="background", type="string", required=False, description="Flat bg for transparent→JPEG (default '#FFFFFF')"),
    ],
    execute=_execute_convert_image_format,
)


# ── trim_transparent_borders ─────────────────────────────────────────────


async def _execute_trim_transparent_borders(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    src, out, err = _resolve_paths(local_path, params.get("output_path"), "trimmed")
    if err:
        return ToolResult(success=False, error=err)
    assert src and out
    try:
        padding = max(0, min(int(params.get("padding") or 0), 200))
        threshold = max(0, min(int(params.get("threshold") or 0), 255))
    except (TypeError, ValueError):
        padding, threshold = 0, 0

    from PIL import Image
    img = Image.open(src).convert("RGBA")
    alpha = img.split()[3]
    if threshold > 0:
        alpha = alpha.point(lambda a: 0 if a <= threshold else 255)
    bbox = alpha.getbbox()
    if not bbox:
        return ToolResult(success=False, error="image is fully transparent — nothing to trim")
    l, t, r, b = bbox
    if padding:
        l = max(0, l - padding); t = max(0, t - padding)
        r = min(img.width, r + padding); b = min(img.height, b + padding)
    cropped = img.crop((l, t, r, b))
    cropped.save(out, format="PNG")
    return ToolResult(success=True, summary=f"Trimmed: {img.width}x{img.height} → {cropped.width}x{cropped.height}\n  bbox: ({l},{t},{r},{b}) padding: {padding}\n  → {out}")


trim_transparent_borders_tool = ToolDefinition(
    name="trim_transparent_borders",
    description="Crop a PNG to its non-transparent bounding box. Cleans up generated artwork with empty borders.",
    parameters=[
        ToolParameter(name="local_path", type="string", description="Source PNG with transparency"),
        ToolParameter(name="output_path", type="string", required=False, description=_DESC_OUTPUT_DEFAULT),
        ToolParameter(name="padding", type="integer", required=False, default=0, description="Px of transparent border to leave (0-200)"),
        ToolParameter(name="threshold", type="integer", required=False, default=0, description="Alpha ≤ this = transparent (0-255)"),
    ],
    execute=_execute_trim_transparent_borders,
)


# ── composite_images ─────────────────────────────────────────────────────


async def _execute_composite_images(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    bg_path = (params.get("background_path") or "").strip()
    ov_path = (params.get("overlay_path") or "").strip()
    out_path = (params.get("output_path") or "").strip()
    if not all([bg_path, ov_path, out_path]):
        return ToolResult(success=False, error="`background_path`, `overlay_path`, `output_path` are required")
    bg_src = Path(bg_path).expanduser().resolve()
    ov_src = Path(ov_path).expanduser().resolve()
    if not bg_src.exists():
        return ToolResult(success=False, error=f"background not found: {bg_src}")
    if not ov_src.exists():
        return ToolResult(success=False, error=f"overlay not found: {ov_src}")
    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    x = int(params.get("x") or 0)
    y = int(params.get("y") or 0)
    try:
        overlay_scale = float(params.get("overlay_scale") or 1.0)
        opacity = max(0.0, min(float(params.get("opacity") or 1.0), 1.0))
    except (TypeError, ValueError):
        overlay_scale, opacity = 1.0, 1.0
    if overlay_scale <= 0 or overlay_scale > 10.0:
        return ToolResult(success=False, error="overlay_scale must be 0 < x ≤ 10")

    from PIL import Image
    bg = Image.open(bg_src).convert("RGBA")
    ov = Image.open(ov_src).convert("RGBA")
    if overlay_scale != 1.0:
        ov = ov.resize((max(1, round(ov.width * overlay_scale)), max(1, round(ov.height * overlay_scale))), Image.LANCZOS)
    if opacity < 1.0:
        r, g, b_chan, a = ov.split()
        a = a.point(lambda v: round(v * opacity))
        ov = Image.merge("RGBA", (r, g, b_chan, a))
    bg.alpha_composite(ov, dest=(x, y))
    bg.save(out, format=_format_for_path(out).upper())
    return ToolResult(success=True, summary=f"Composited {ov_src.name} ({ov.width}x{ov.height}) onto {bg_src.name} ({bg.width}x{bg.height}) at ({x},{y}), opacity={opacity}\n  → {out}")


composite_images_tool = ToolDefinition(
    name="composite_images",
    description="Paste an overlay onto a background at (x,y). Honors alpha; supports overlay_scale and opacity.",
    parameters=[
        ToolParameter(name="background_path", type="string", description="Base image (drawn first)"),
        ToolParameter(name="overlay_path", type="string", description="Image to draw on top"),
        ToolParameter(name="output_path", type="string", description="Where to write the composited result"),
        ToolParameter(name="x", type="integer", required=False, default=0, description="Overlay top-left X (negative offsets allowed)"),
        ToolParameter(name="y", type="integer", required=False, default=0, description="Overlay top-left Y"),
        ToolParameter(name="overlay_scale", type="number", required=False, default=1.0, description="Scale overlay (1.0 = original; >0, ≤10)"),
        ToolParameter(name="opacity", type="number", required=False, default=1.0, description="Multiply overlay alpha (0-1)"),
    ],
    execute=_execute_composite_images,
)


# ── recolor_image ────────────────────────────────────────────────────────


async def _execute_recolor_image(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    src, out, err = _resolve_paths(local_path, params.get("output_path"), "recolored")
    if err:
        return ToolResult(success=False, error=err)
    assert src and out
    tint = (params.get("tint") or "").strip() or None
    replace_color = (params.get("replace_color") or "").strip() or None
    replace_with = (params.get("replace_with") or "").strip() or None
    if not tint and not replace_color:
        return ToolResult(success=False, error="pass `tint` OR `replace_color`+`replace_with`")
    if tint and replace_color:
        return ToolResult(success=False, error="pass tint OR replace_color, not both")

    from PIL import Image
    img = Image.open(src).convert("RGBA")
    if tint:
        try:
            t = _parse_color(tint)[:3]
        except ValueError as e:
            return ToolResult(success=False, error=str(e))
        r, g, b_chan, a = img.split()
        r = r.point(lambda v: (v * t[0]) // 255)
        g = g.point(lambda v: (v * t[1]) // 255)
        b_chan = b_chan.point(lambda v: (v * t[2]) // 255)
        img = Image.merge("RGBA", (r, g, b_chan, a))
        note = f"tinted with {tint}"
    else:
        if not replace_with:
            return ToolResult(success=False, error="replace_color requires replace_with")
        try:
            src_rgb = _parse_color(replace_color)[:3]
            tgt_rgba = _parse_color(replace_with)
        except ValueError as e:
            return ToolResult(success=False, error=str(e))
        try:
            tol = max(0, min(int(params.get("replace_tolerance") or 24), 441))
        except (TypeError, ValueError):
            tol = 24
        pixels = img.load()
        tol2 = tol ** 2
        changed = 0
        for px in range(img.width):
            for py in range(img.height):
                r, g, b_chan, a = pixels[px, py]
                d2 = (r - src_rgb[0])**2 + (g - src_rgb[1])**2 + (b_chan - src_rgb[2])**2
                if d2 <= tol2:
                    pixels[px, py] = tgt_rgba
                    changed += 1
        note = f"replaced {replace_color} → {replace_with} (tol={tol}, {changed:,} px changed)"

    img.save(out, format=_format_for_path(out).upper())
    return ToolResult(success=True, summary=f"Recolored {src.name}: {note}\n  → {out}")


recolor_image_tool = ToolDefinition(
    name="recolor_image",
    description="Tint (multiply pixels by color) OR swap one color for another. Lightweight palette ops, no AI.",
    parameters=[
        ToolParameter(name="local_path", type="string", description=_DESC_LOCAL_PATH),
        ToolParameter(name="output_path", type="string", required=False, description=_DESC_OUTPUT_DEFAULT),
        ToolParameter(name="tint", type="string", required=False, description="Multiply pixels by this color"),
        ToolParameter(name="replace_color", type="string", required=False, description="Source color to swap"),
        ToolParameter(name="replace_with", type="string", required=False, description="Target color (with replace_color)"),
        ToolParameter(name="replace_tolerance", type="integer", required=False, default=24, description="Max RGB distance for match (0-441)"),
    ],
    execute=_execute_recolor_image,
)


# ── make_favicon ─────────────────────────────────────────────────────────


async def _execute_make_favicon(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    output_path = (params.get("output_path") or "").strip()
    if not local_path or not output_path:
        return ToolResult(success=False, error="`local_path` and `output_path` are required")
    from PIL import Image
    src = Path(local_path).expanduser().resolve()
    if not src.exists() or not src.is_file():
        return ToolResult(success=False, error=f"local file not a regular file: {src}")
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.suffix.lower() != ".ico":
        return ToolResult(success=False, error=f"output_path must end in '.ico' (got {out.suffix!r})")

    sizes = params.get("sizes") or [16, 32, 48, 64, 128, 256]
    try:
        resolutions = sorted({int(s) for s in sizes})
    except (TypeError, ValueError):
        return ToolResult(success=False, error="`sizes` must be a list of integers")
    for s in resolutions:
        if s < 8 or s > 256:
            return ToolResult(success=False, error=f"ICO size must be 8-256, got {s}")

    img = Image.open(src).convert("RGBA")
    if img.width != img.height:
        if not bool(params.get("pad_to_square", True)):
            return ToolResult(success=False, error=f"source is {img.width}x{img.height}, not square; pad_to_square=true or crop first")
        side = max(img.width, img.height)
        try:
            bg = _parse_color(params.get("background") or "transparent")
        except ValueError as e:
            return ToolResult(success=False, error=str(e))
        canvas = Image.new("RGBA", (side, side), bg)
        canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2), img)
        img = canvas

    if max(resolutions) > img.width:
        return ToolResult(success=False, error=f"requested size {max(resolutions)} exceeds source dim {img.width}; use higher-res source")
    img.save(out, format="ICO", sizes=[(s, s) for s in resolutions])
    return ToolResult(
        success=True,
        summary=(
            f"Generated multi-res favicon: {out.name}\n"
            f"  sizes embedded: {resolutions}\n"
            f"  source: {src.name} ({img.width}x{img.height})\n"
            f"  output size: {out.stat().st_size:,} bytes\n"
            f"  → {out}\n\n"
            f"Publish with: upload_static_asset(local_path={str(out)!r}, page_name='global', folder='favicon', filename='favicon.ico')."
        ),
    )


make_favicon_tool = ToolDefinition(
    name="make_favicon",
    description="Generate a multi-resolution .ico favicon from a SQUARE source image. Embeds multiple sizes so browsers/OSes pick the right one.",
    parameters=[
        ToolParameter(name="local_path", type="string", description="Source image (PNG recommended, ≥256x256)"),
        ToolParameter(name="output_path", type="string", description="Where to write the .ico"),
        ToolParameter(name="sizes", type="array", required=False, description="Sizes to embed (default [16,32,48,64,128,256])", items={"type": "integer"}),
        ToolParameter(name="background", type="string", required=False, default="transparent", description="Color used when padding non-square sources"),
        ToolParameter(name="pad_to_square", type="boolean", required=False, default=True, description="Auto-pad non-square sources to square"),
    ],
    execute=_execute_make_favicon,
)


# ── apply_image_filter ───────────────────────────────────────────────────


async def _execute_apply_image_filter(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    local_path = (params.get("local_path") or "").strip()
    if not local_path:
        return ToolResult(success=False, error="`local_path` is required")
    src, out, err = _resolve_paths(local_path, params.get("output_path"), "filtered")
    if err:
        return ToolResult(success=False, error=err)
    assert src and out

    from PIL import Image, ImageFilter, ImageEnhance
    img = Image.open(src).convert("RGBA")
    applied: list[str] = []

    if bool(params.get("grayscale")):
        r, g, b, a = img.split()
        gray = Image.merge("RGB", (r, g, b)).convert("L").convert("RGB")
        r, g, b = gray.split()
        img = Image.merge("RGBA", (r, g, b, a))
        applied.append("grayscale")
    try:
        blur_radius = max(0.0, min(float(params.get("blur_radius") or 0), 50.0))
        brightness = float(params.get("brightness") or 1.0)
        contrast = float(params.get("contrast") or 1.0)
        saturation = max(0.0, min(float(params.get("saturation") or 1.0), 5.0))
    except (TypeError, ValueError):
        return ToolResult(success=False, error="numeric filter params must be numbers")
    if not (0.0 < brightness <= 5.0) or not (0.0 < contrast <= 5.0):
        return ToolResult(success=False, error="brightness and contrast must be in (0, 5]")
    if blur_radius > 0:
        img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        applied.append(f"blur({blur_radius}px)")
    if brightness != 1.0:
        img = ImageEnhance.Brightness(img).enhance(brightness)
        applied.append(f"brightness({brightness})")
    if contrast != 1.0:
        img = ImageEnhance.Contrast(img).enhance(contrast)
        applied.append(f"contrast({contrast})")
    if saturation != 1.0:
        img = ImageEnhance.Color(img).enhance(saturation)
        applied.append(f"saturation({saturation})")

    if not applied:
        return ToolResult(success=True, summary="No-op: no filter flags set.")
    img.save(out, format=_format_for_path(out).upper())
    return ToolResult(success=True, summary=f"Filtered {src.name}: {', '.join(applied)}\n  → {out}")


apply_image_filter_tool = ToolDefinition(
    name="apply_image_filter",
    description="Apply one or more visual filters: grayscale, gaussian blur, brightness, contrast, saturation. Stackable.",
    parameters=[
        ToolParameter(name="local_path", type="string", description=_DESC_LOCAL_PATH),
        ToolParameter(name="output_path", type="string", required=False, description=_DESC_OUTPUT_DEFAULT),
        ToolParameter(name="grayscale", type="boolean", required=False, default=False, description="Convert to grayscale (preserves alpha)"),
        ToolParameter(name="blur_radius", type="number", required=False, default=0, description="Gaussian blur radius px (0-50)"),
        ToolParameter(name="brightness", type="number", required=False, default=1.0, description="Multiplier (1.0=none, 0.5=darker, 1.5=brighter)"),
        ToolParameter(name="contrast", type="number", required=False, default=1.0, description="Multiplier (1.0=none)"),
        ToolParameter(name="saturation", type="number", required=False, default=1.0, description="Multiplier (0=grayscale, 1=original, 2=vivid)"),
    ],
    execute=_execute_apply_image_filter,
)


# ── Module export ────────────────────────────────────────────────────────


TOOLS: list[ToolDefinition] = [
    crop_image_tool,
    pad_image_canvas_tool,
    convert_image_format_tool,
    trim_transparent_borders_tool,
    composite_images_tool,
    recolor_image_tool,
    make_favicon_tool,
    apply_image_filter_tool,
]
