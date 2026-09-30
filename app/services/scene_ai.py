"""Building a 3D scene document from a description.

Backs the Scene Editor's AI pane in nocode-ui (``POST /api/ai/appbuilder/scene``)
and is available to the agent through the ``scenes`` tool family. Stateless: the
whole current document is sent, so an unsaved scene can be revised without
anything being written first -- the same arrangement the template editor's AI tab
already uses.

Why it exists at all: a scene document is fifty-odd fields of nested JSON plus
GLSL, and the editor's panels are the wrong instrument for "make this look like
a sunrise". Typing coordinates is fine for nudging one object and hopeless for
describing a scene.

**The schema is read, never re-typed.** Everything the prompt says about the
document's shape -- the interfaces, the closed enumerations, the point-count cap,
the preset list, the uniforms the runtime binds for free -- comes out of the
``scenes`` block that nocode-ui publishes into ``component-catalog.json``. A
hand-kept Python copy of a TypeScript interface does not fail loudly when it
drifts: the model writes a field that was renamed last month, normalisation drops
it without a word, and the scene quietly comes back wrong.

One operational consequence, and it bites every time: ``catalog.load()`` runs once
at startup with no TTL. Regenerating the catalog in nocode-ui does nothing here
until the service restarts.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from typing import Any, Dict, List

from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

# What a single pasted reference is allowed to weigh. A screenshot is a few
# hundred KB; anything past this is a mistake or an attempt to fill the window,
# and either way the useful response is to say so rather than to send it.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENTS = 4
MAX_TEXT_ATTACHMENT_CHARS = 20_000

_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# Text a model can usefully read. A .glb is a real attachment for this feature
# but its bytes say nothing in a prompt -- it is named, not inlined, and the
# URL is what the scene ends up carrying.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIMES = {"application/json", "application/xml", "image/svg+xml"}


def _contract() -> Dict[str, Any]:
    """The published scene contract, or {} on a catalog that predates it."""
    from app.agents.appbuilder.catalog import get_catalog

    cat = get_catalog()
    raw = getattr(cat, "_catalog", None) or {}
    return raw.get("scenes") or {}


def _render_shape(shape: Dict[str, Dict[str, str]]) -> str:
    """The published interfaces, back as TypeScript.

    Re-emitted rather than translated into prose because the model has seen far
    more TypeScript than it has seen any vocabulary invented here, and because a
    field's type is load-bearing: `position` being a `Vec3` and not a number is
    the difference between an object moving and an object vanishing.
    """
    if not shape:
        return ""
    lines = ["type Vec3 = [number, number, number];"]
    # SceneDocument first: it is the root, and a reader that meets the leaves
    # first has to hold them all before learning what they hang off.
    names = sorted(shape, key=lambda n: (n != "SceneDocument", n))
    for name in names:
        lines.append(f"interface {name} {{")
        for field, ftype in shape[name].items():
            lines.append(f"    {field}: {ftype};")
        lines.append("}")
    return "\n".join(lines)


def _render_contract(contract: Dict[str, Any]) -> str:
    parts: List[str] = []
    shape = _render_shape(contract.get("shape") or {})
    if shape:
        parts.append("The document shape, exactly as the runtime defines it:\n\n" + shape)

    enums = contract.get("enums") or {}
    if enums:
        rows = "\n".join(f"- {k}: {', '.join(v)}" for k, v in sorted(enums.items()))
        parts.append(
            "Closed lists. A value outside one of these is silently replaced with a default "
            "at load, so the scene renders something other than what you wrote and nothing "
            "reports an error:\n" + rows
        )

    limits = contract.get("limits") or {}
    if limits:
        parts.append(
            "Hard limits:\n"
            + "\n".join(f"- {k}: {v}" for k, v in sorted(limits.items()))
        )

    shared = contract.get("sharedUniforms") or []
    if shared:
        parts.append(
            "Uniforms the runtime binds to every shader for you: "
            + ", ".join(shared)
            + ".\nDECLARE each one you read (`uniform float uTime;`) -- three binds values to "
            "names the shader source declares, it does not add the declarations. But do NOT "
            "put them in the document's `uniforms` array: that pins a frozen value over the "
            "live one, and the usual symptom is a shader that compiles and never moves."
        )

    presets = contract.get("presets") or []
    if presets:
        rows = "\n".join(
            f"- {p.get('name')} ({p.get('kind')}): {p.get('description')}" for p in presets
        )
        parts.append("Built-in presets, useful as a starting point:\n" + rows)

    if not parts:
        # Said out loud rather than swallowed. Without the contract the model is
        # writing a schema it has only been told about in prose, and the caller
        # deserves to know that is what happened.
        return (
            "WARNING: the component catalog carries no `scenes` block, so the exact schema "
            "could not be supplied. Write the document conservatively and keep to fields you "
            "are confident about."
        )
    return "\n\n".join(parts)


_RULES = """\
Hard rules:
- Return ONE complete scene document. It replaces what is there; it is not a patch.
- Every scene needs at least one visible object, or the canvas renders empty with no error.
- Object ids are how timeline tracks and interactions address objects. Keep the ids that are
  already in the document unless the user asks for something removed, or their animations and
  click handlers stop working.
- Shader source is GLSL ES 1.0, the WebGL 1 dialect: `varying`, not `in`/`out`, and write to
  `gl_FragColor`. `varying vec2 vUv` is the 0..1 position across the surface.
- Colours are CSS colour strings (`#1e1b4b`, `rgb(94, 192, 164)`).
- Rotations are DEGREES, not radians.
- A track target is a dotted path: `objects.<id>.rotation.y`, `shaders.<id>.<uniformName>`.
- Keyframe `t` runs 0..1 across the timeline, whatever drives it.
- Prefer editing what is there over replacing it. Someone asking for a warmer colour wants
  their scene warmer, not a different scene.

Output format -- return ONLY a single JSON object, no prose and no code fences:
{"scene": <the whole scene document>, "message": "<one short sentence on what you changed>", \
"notes": ["<anything the user should know, e.g. an asset they still need to supply>"]}
"""


def build_system_prompt(contract: Dict[str, Any] | None = None) -> str:
    contract = _contract() if contract is None else contract
    return (
        "You author 3D scene documents for the Modlix platform. A scene document is pure JSON "
        "that a three.js runtime renders directly.\n\n"
        f"{_render_contract(contract)}\n\n{_RULES}"
    )


def _describe_component(component_type: str) -> str:
    """What this component's scene is FOR.

    The same document renders in all four, but what makes a good one differs
    completely: a full-bleed shader backdrop behind a headline and a product on
    a turntable are not the same brief, and a model told only "a scene" writes
    the average of them.
    """
    return {
        "ShaderBackground": (
            "ShaderBackground: a full-bleed backdrop that sits BEHIND page content. One "
            "'quad' object with a fragment shader on it, a 'fullscreen' camera, no lights. "
            "Text is read over it, so keep contrast low and motion slow."
        ),
        "ParticleField": (
            "ParticleField: a cloud of points. One 'points' object with a shader material. "
            "Point count is the main cost -- tens of thousands, not millions."
        ),
        "ModelViewer": (
            "ModelViewer: one loaded glTF model the visitor turns. A 'gltf' object with a "
            "url, a perspective camera with controls on, and lights. Never invent a model "
            "URL: if none was given, leave the url empty and say so in notes."
        ),
        "ScrollScene": (
            "ScrollScene: geometry scrubbed by the page scroll. `timeline.driver` MUST be "
            "'scroll'. Keyframes at t=0 and t=1 are the start and end of the scroll range, "
            "so every track needs both or the scene sits still at one end."
        ),
    }.get(component_type, f"Component type: {component_type}.")


def _attachment_blocks(attachments: List[Dict[str, Any]] | None) -> tuple[list, list[str]]:
    """Attachments as content blocks, plus what could not be used and why.

    The refusals are returned rather than logged. Someone who pastes a 12MB
    reference and gets a scene that plainly ignores it should be told the image
    never reached the model, not left to wonder whether it was understood and
    disregarded.
    """
    blocks: list = []
    notes: list[str] = []
    if not attachments:
        return blocks, notes

    for att in attachments[:MAX_ATTACHMENTS]:
        if not isinstance(att, dict):
            continue
        name = str(att.get("name") or "attachment")
        mime = str(att.get("mime_type") or att.get("mimeType") or "").lower()
        data = att.get("data")

        if not data:
            notes.append(f"{name} arrived with no content, so it was not read.")
            continue
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            notes.append(f"{name} could not be decoded, so it was not read.")
            continue
        if len(raw) > MAX_ATTACHMENT_BYTES:
            notes.append(
                f"{name} is {len(raw):,} bytes, over the {MAX_ATTACHMENT_BYTES:,} byte cap, "
                f"so it was not read."
            )
            continue

        if mime in _IMAGE_MIMES:
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": data},
                }
            )
            continue

        if mime in _TEXT_MIMES or mime.startswith(_TEXT_MIME_PREFIXES):
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                notes.append(f"{name} is not readable as text, so it was not read.")
                continue
            if len(text) > MAX_TEXT_ATTACHMENT_CHARS:
                text = text[:MAX_TEXT_ATTACHMENT_CHARS] + "\n... [truncated]"
            blocks.append({"type": "text", "text": f"Attached file {name}:\n{text}"})
            continue

        # A .glb or .hdr: real, useful, and nothing a prompt can do with its
        # bytes. Named so the model can refer to it and ask for its URL.
        blocks.append(
            {
                "type": "text",
                "text": (
                    f"The user attached {name} ({mime or 'unknown type'}, {len(raw):,} bytes). "
                    f"Its contents are binary and are not included. If the scene needs it, "
                    f"reference it by name and say in notes that its URL is still needed."
                ),
            }
        )

    if attachments and len(attachments) > MAX_ATTACHMENTS:
        notes.append(
            f"Only the first {MAX_ATTACHMENTS} of {len(attachments)} attachments were read."
        )
    return blocks, notes


def _build_user_blocks(
    *,
    prompt: str,
    scene: Any,
    component_type: str,
    attachments: List[Dict[str, Any]] | None,
) -> tuple[list, list[str]]:
    parts = [_describe_component(component_type)]
    if isinstance(scene, dict) and scene:
        parts.append(
            "The scene as it stands. Change it; keep what still applies:\n"
            + json.dumps(scene, separators=(",", ":"))
        )
    else:
        parts.append("There is no scene yet -- build one from scratch.")
    parts.append("Request:\n" + prompt)

    att_blocks, notes = _attachment_blocks(attachments)
    blocks: list = [{"type": "text", "text": "\n\n".join(parts)}]
    blocks.extend(att_blocks)
    return blocks, notes


def _extract_json(text: str) -> Dict[str, Any] | None:
    """Parse the model's reply, tolerating code fences and surrounding prose."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(t[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None


def _unwrap_scene(parsed: Dict[str, Any]) -> Any:
    """The document out of the reply, whether or not it was wrapped as asked.

    A model that returns the bare document instead of `{"scene": ...}` has done
    the work correctly and formatted the envelope wrongly. Throwing that away
    over the envelope would be a failure the user cannot act on -- they did not
    write the prompt and cannot fix it.
    """
    scene = parsed.get("scene")
    if isinstance(scene, dict):
        return scene
    if any(k in parsed for k in ("objects", "camera", "shaders", "timeline")):
        return parsed
    return None


async def generate_scene(
    *,
    prompt: str,
    scene: Any = None,
    component_type: str = "ShaderBackground",
    attachments: List[Dict[str, Any]] | None = None,
    max_tokens: int = 16384,
) -> Dict[str, Any]:
    """Build or revise a scene document. Returns ``{scene, message, warnings}``.

    ``warnings`` is the honest part of the result. The document comes back
    through the same validator the agent's ``validate_scene`` tool uses, and
    anything it finds is reported ALONGSIDE the scene rather than instead of it.
    Refusing to hand back a scene with a flaw in it would leave the user with
    nothing; handing one back silently would leave them with a scene that does
    not do what they asked and no idea why.
    """
    from app.agents.appbuilder.tools.modlix.scenes import validate_scene_document

    if not prompt or not prompt.strip():
        raise ValueError("prompt is required")

    provider = get_llm_provider()
    blocks, warnings = _build_user_blocks(
        prompt=prompt, scene=scene, component_type=component_type, attachments=attachments
    )

    if not provider.supports_vision():
        images = [b for b in blocks if b.get("type") == "image"]
        if images:
            blocks = [b for b in blocks if b.get("type") != "image"]
            warnings.append(
                f"{len(images)} image(s) were dropped: the configured model cannot read images."
            )

    result = await provider.create_completion(
        system_prompt=build_system_prompt(),
        messages=[{"role": "user", "content": blocks}],
        model_tier="balanced",
        max_tokens=max_tokens,
        use_cache=True,
    )
    content = (result or {}).get("content", "") or ""
    parsed = _extract_json(content)
    if not parsed:
        logger.warning("scene_ai: response was not JSON (%d chars)", len(content))
        return {
            "scene": None,
            "message": "",
            "warnings": warnings
            + ["The model did not return a scene document. Try rewording the request."],
        }

    doc = _unwrap_scene(parsed)
    if doc is None:
        return {
            "scene": None,
            "message": str(parsed.get("message") or ""),
            "warnings": warnings + ["The reply carried no scene document."],
        }

    warnings.extend(str(n) for n in (parsed.get("notes") or []) if n)
    warnings.extend(validate_scene_document(doc))

    return {
        "scene": doc,
        "message": str(parsed.get("message") or "Scene updated."),
        "warnings": warnings,
    }
