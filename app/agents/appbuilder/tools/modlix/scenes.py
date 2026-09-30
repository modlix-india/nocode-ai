"""three.js scenes and scroll-driven animation — authoring tools.

The platform ships four real WebGL components (`ShaderBackground`,
`ParticleField`, `ModelViewer`, `ScrollScene`) and an `Animator` whose
animations can be scrubbed by scroll position. These tools let the agent author
all of that: pick a preset, read and write the scene document behind it, compile
GLSL, and -- the part that matters most -- CHECK that what it wrote actually
renders something.

Three things shape every tool here.

**The contract is published, not mirrored.** `nocode-ui` emits the scene
enumerations, limits and preset registry into `component-catalog.json`, parsed
out of the real TypeScript. Everything below validates against THAT. A Python
copy of a TypeScript type is a drift bug with a date on it: the same mistake was
already made with COMMON_COMPONENT_PROPERTIES, where 13 of 27 properties
silently went missing until the catalog started parsing the real table. If the
catalog has no `scenes` block, `validate_scene` says so rather than guessing.

**A scene document is big.** A resolved document carrying GLSL runs to several
KB, well past the 4000-char `DEFAULT_MAX_RESULT_CHARS` cap. `get_scene` is
therefore paged, and silently truncating it would be worse than useless because
the agent would then write back a document missing its tail.

**Compiling is not rendering.** The failure mode of free-form GLSL is a shader
that compiles cleanly and renders black. `render_scene_check` therefore asserts
three separate things about real pixels, and -- this is the important part --
distinguishes "this browser has no WebGL" from "the scene rendered blank". Told
the wrong one, an agent in a retry loop rewrites working code until it breaks.

  Measured 2026-09-25: Playwright's bundled Chromium already renders WebGL
  through SwiftShader with NO launch flags (renderer string: "ANGLE (Google,
  Vulkan 1.3.0 (SwiftShader Device ...))", a clear-colour readback came back
  correct, and a shader compiled). The flags are unnecessary here. The check
  still probes for a context first and reports its absence as a distinct
  outcome, because that is measured on one machine and not a guarantee.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult

from . import _conventions as c
from . import _page_ops as p_ops


_SCENE_COMPONENTS = ("ShaderBackground", "ParticleField", "ModelViewer", "ScrollScene")

_DESC_APP_CODE = "appCode; defaults to the app this session is working in"
_DESC_PAGE = "Page name"
_DESC_KEY = "Component key on that page"


# ═════════════════════════════════════════════════════════════════════════
#  Shared helpers
# ═════════════════════════════════════════════════════════════════════════


def _client_and_headers(context: dict[str, Any]) -> tuple[Any, dict[str, str]]:
    from app.agents.appbuilder.tools._shared import get_saas_client
    return get_saas_client(), context.get("headers") or {}


def _resolve_app_code(params: dict[str, Any], context: dict[str, Any]) -> tuple[str, ToolResult | None]:
    from app.agents.appbuilder.tools._shared import resolve_app_code
    ac = resolve_app_code(params, context)
    if not ac:
        return "", ToolResult(success=False, error="No appCode set. Pass `app_code` or set it on the chat request.")
    return ac, None


def _scene_contract() -> dict[str, Any]:
    """The published contract, or {} when the catalog predates it."""
    from app.agents.appbuilder.catalog import get_catalog
    cat = get_catalog()
    raw = getattr(cat, "_catalog", None) or {}
    return raw.get("scenes") or {}


def _page_fetch_component(
    page: dict[str, Any], component_key: str
) -> tuple[dict[str, Any] | None, str | None]:
    comp = (page.get("componentDefinition") or {}).get(component_key)
    if not isinstance(comp, dict):
        return None, f"Component '{component_key}' not found on this page."
    if comp.get("type") not in _SCENE_COMPONENTS:
        return None, (
            f"Component '{component_key}' is a {comp.get('type')}, which has no scene. "
            f"Scene components are: {', '.join(_SCENE_COMPONENTS)}."
        )
    return comp, None


def _paged(body: str, params: dict[str, Any]) -> str:
    """Slice a long body, telling the caller exactly how to get the rest.

    A scene document with GLSL in it exceeds the default 4000-char result cap,
    and a silent truncation is worse than useless here: the agent would write
    back a document missing its tail.
    """
    total = len(body)
    try:
        offset = max(0, int(params.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    raw_max = params.get("max_chars")
    if raw_max in (None, ""):
        return body if offset == 0 else body[offset:]
    try:
        max_chars = int(raw_max)
    except (TypeError, ValueError):
        return body[offset:]
    # Honoured as asked. Silently rounding a small request up to some floor
    # means the continuation offset the caller is told to use does not match
    # the slice they asked for, and a caller paging in a loop then skips text.
    if max_chars <= 0:
        return body[offset:]
    shown = body[offset : offset + max_chars]
    if offset + max_chars < total:
        shown += (
            f"\n\n... [showing chars {offset}-{offset + max_chars} of {total}; "
            f"call again with offset={offset + max_chars}]"
        )
    return shown


# ═════════════════════════════════════════════════════════════════════════
#  list_scene_presets
# ═════════════════════════════════════════════════════════════════════════


async def _execute_list_scene_presets(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    contract = _scene_contract()
    presets = contract.get("presets") or []
    if not presets:
        return ToolResult(
            success=False,
            error=(
                "The component catalog carries no `scenes` block, so the preset list is "
                "unknown. The catalog is loaded ONCE at startup with no TTL: regenerate it "
                "in nocode-ui (`npm run generate-catalog`) and RESTART this service."
            ),
        )

    kind = (params.get("kind") or "").strip()
    rows = [p for p in presets if not kind or p.get("kind") == kind]
    if kind and not rows:
        kinds = sorted({p.get("kind", "?") for p in presets})
        return ToolResult(success=False, error=f"No presets of kind {kind!r}. Kinds: {kinds}")

    lines = ["Built-in scene presets (component → kind):", ""]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for p in rows:
        by_kind.setdefault(p.get("kind", "?"), []).append(p)

    component_for = {
        "background": "ShaderBackground",
        "particles": "ParticleField",
        "model": "ModelViewer",
        "scroll": "ScrollScene",
    }
    for k, items in by_kind.items():
        lines.append(f"### {k}  (use with {component_for.get(k, '?')})")
        for p in items:
            lines.append(f"  - {p['name']}  ({p.get('displayName')}): {p.get('description')}")
        lines.append("")

    lines.append(
        "Set the component's `preset` property to one of these names. That keeps the page "
        "definition to a single string and is the right default. Reach for `set_scene` only "
        "when the scene needs something no preset offers -- a stored document is several KB "
        "of page JSON, and once written the page owns it and later preset changes cannot "
        "reach it."
    )
    return ToolResult(success=True, summary="\n".join(lines))


list_scene_presets_tool = ToolDefinition(
    name="list_scene_presets",
    description=(
        "List the built-in three.js scene presets with their kind and a one-line description. "
        "Start here: setting a component's `preset` is almost always the right move, and is far "
        "cheaper than authoring a scene document."
    ),
    parameters=[
        ToolParameter(
            name="kind", type="string", required=False,
            description="Filter: background | particles | model | scroll",
        ),
    ],
    execute=_execute_list_scene_presets,
)


# ═════════════════════════════════════════════════════════════════════════
#  get_scene
# ═════════════════════════════════════════════════════════════════════════


async def _execute_get_scene(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    if not page_name or not component_key:
        return ToolResult(success=False, error="`page_name` and `component_key` are required")

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp, cerr = _page_fetch_component(page, component_key)
    if cerr:
        return ToolResult(success=False, error=cerr)
    assert comp is not None

    props = comp.get("properties") or {}
    scene = (props.get("scene") or {}).get("value")
    preset = (props.get("preset") or {}).get("value")

    if not scene:
        return ToolResult(
            success=True,
            summary=(
                f"'{component_key}' ({comp.get('type')}) has NO stored scene document.\n"
                f"It is using the preset {preset!r}, which the runtime rebuilds on every render.\n\n"
                "That is the normal, cheap state. Writing a document with `set_scene` makes the "
                "page own its scene from then on -- later changes to the built-in preset will not "
                "reach it, which is usually what you want once it has been tuned, and a needless "
                "cost before that."
            ),
        )

    body = json.dumps(scene, indent=2, default=str)
    head = (
        f"Scene document on '{component_key}' ({comp.get('type')}), {len(body)} chars.\n"
        f"objects={len(scene.get('objects') or [])} lights={len(scene.get('lights') or [])} "
        f"shaders={len(scene.get('shaders') or [])} "
        f"tracks={len((scene.get('timeline') or {}).get('tracks') or [])}\n\n"
    )
    return ToolResult(success=True, summary=head + _paged(body, params))


get_scene_tool = ToolDefinition(
    name="get_scene",
    description=(
        "Read the scene document stored on a WebGL component. PAGED: a document carrying GLSL "
        "runs well past the default result cap, so pass `offset`/`max_chars` and reassemble. "
        "Reports plainly when the component is still on a preset and has no document at all."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", required=True, description=_DESC_PAGE),
        ToolParameter(name="component_key", type="string", required=True, description=_DESC_KEY),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="offset", type="integer", required=False, default=0, description="Start character"),
        ToolParameter(
            name="max_chars", type="integer", required=False,
            description="Characters to return (omit for all; the result cap still applies)",
        ),
    ],
    execute=_execute_get_scene,
)


# ═════════════════════════════════════════════════════════════════════════
#  validate_scene
# ═════════════════════════════════════════════════════════════════════════


def _enum_hint(field: str, value: Any, allowed: list[str]) -> str:
    return (
        f"{field} is {value!r}, which is not one of {allowed}. "
        f"An unrecognised value is coerced to a default at load, so the scene renders "
        f"something other than what you wrote, with no error."
    )


def validate_scene_document(doc: Any) -> list[str]:
    """Problems with a scene document, as sentences an agent can act on.

    Deliberately not a schema dump. The audience is a model deciding what to
    change next, and "expected one of ['gltf','primitive','points','quad']"
    without the consequence attached does not tell it why the canvas is black.

    Ordered worst-first: a scene with no objects renders nothing at all, and
    saying so before complaining about an easing name is the difference between
    a useful result and a wall of text.
    """
    if not isinstance(doc, dict):
        return ["The scene is not an object, so nothing can be read from it."]

    contract = _scene_contract()
    enums = contract.get("enums") or {}
    limits = contract.get("limits") or {}
    shared = set(contract.get("sharedUniforms") or [])
    problems: list[str] = []

    if not enums:
        problems.append(
            "The component catalog carries no `scenes` block, so only structural checks ran. "
            "Regenerate the catalog in nocode-ui and RESTART this service to get the full "
            "validation -- catalog.load() runs once at startup with no TTL."
        )

    objects = doc.get("objects")
    if not isinstance(objects, list) or not objects:
        problems.append(
            "The scene has no objects, so the canvas renders empty. Every scene needs at "
            "least one object; a shader backdrop uses one of kind 'quad'."
        )
        objects = []

    visible = [o for o in objects if isinstance(o, dict) and o.get("visible", True)]
    if objects and not visible:
        problems.append("Every object is invisible, so the canvas renders empty.")

    shader_ids = {
        s.get("id") for s in (doc.get("shaders") or []) if isinstance(s, dict) and s.get("id")
    }
    object_ids: set[str] = set()

    for o in objects:
        if not isinstance(o, dict):
            problems.append("An entry in `objects` is not an object and will be dropped.")
            continue
        oid = o.get("id")
        if not oid:
            problems.append(
                "An object has no id. Timeline tracks and interactions address objects BY id, "
                "so one without an id can never be animated or clicked."
            )
        elif oid in object_ids:
            problems.append(
                f"Two objects share the id {oid!r}. A track addressing it reaches only the "
                f"first, and the second silently never animates."
            )
        else:
            object_ids.add(oid)

        src = o.get("source") or {}
        kind = src.get("kind")
        if enums.get("objectSourceKind") and kind not in enums["objectSourceKind"]:
            problems.append(_enum_hint(f"objects[{oid}].source.kind", kind, enums["objectSourceKind"]))
        if kind == "gltf" and not src.get("url"):
            problems.append(
                f"Object {oid!r} is a gltf source with no url, so nothing loads and the space "
                f"where the model should be stays empty."
            )
        if kind == "primitive" and enums.get("primitiveShape"):
            shape = src.get("shape")
            if shape is not None and shape not in enums["primitiveShape"]:
                problems.append(_enum_hint(f"objects[{oid}].source.shape", shape, enums["primitiveShape"]))
        if kind == "points":
            cap = limits.get("maxPointCount")
            count = src.get("count")
            if cap and isinstance(count, (int, float)) and count > cap:
                problems.append(
                    f"Object {oid!r} asks for {int(count):,} points; the runtime caps this at "
                    f"{cap:,} and will clamp it. Every point costs a fragment pass, and a large "
                    f"count slows the WHOLE page, not just this component."
                )
            if enums.get("pointDistribution"):
                dist = src.get("distribution")
                if dist is not None and dist not in enums["pointDistribution"]:
                    problems.append(
                        _enum_hint(f"objects[{oid}].source.distribution", dist, enums["pointDistribution"])
                    )

        mat = o.get("material") or {}
        sid = mat.get("shaderId")
        if sid and sid not in shader_ids:
            problems.append(
                f"Object {oid!r} names the shader {sid!r}, which this scene does not define. "
                f"It will render with the default material instead, which usually reads as the "
                f"shader silently not working."
            )

    lit = bool(doc.get("lights")) or bool((doc.get("environment") or {}).get("preset")) \
        or bool((doc.get("environment") or {}).get("hdriUrl"))
    needs_light = [
        o for o in visible
        if isinstance(o, dict)
        and (o.get("source") or {}).get("kind") not in ("points", "quad")
        and not (o.get("material") or {}).get("shaderId")
    ]
    if needs_light and not lit:
        problems.append(
            "Nothing lights this scene: it has no lights and no environment preset, so every "
            "standard-material object renders BLACK. Add a light, or set environment.preset."
        )

    for l in doc.get("lights") or []:
        if not isinstance(l, dict):
            continue
        if enums.get("lightType") and l.get("type") not in enums["lightType"]:
            problems.append(_enum_hint(f"lights[{l.get('id')}].type", l.get("type"), enums["lightType"]))

    cam = doc.get("camera") or {}
    if enums.get("cameraType") and cam.get("type") and cam["type"] not in enums["cameraType"]:
        problems.append(_enum_hint("camera.type", cam["type"], enums["cameraType"]))

    timeline = doc.get("timeline") or {}
    driver = timeline.get("driver")
    if enums.get("timelineDriver") and driver and driver not in enums["timelineDriver"]:
        problems.append(_enum_hint("timeline.driver", driver, enums["timelineDriver"]))

    for i, t in enumerate(timeline.get("tracks") or []):
        if not isinstance(t, dict):
            continue
        target = t.get("target") or ""
        parts = target.split(".")
        root = parts[0] if parts else ""
        if root not in ("objects", "shaders", "camera"):
            problems.append(
                f"Track {i} targets {target!r}. Only 'objects.<id>.<path>', "
                f"'shaders.<id>.<uniform>' and 'camera.<path>' can be addressed; anything else "
                f"is dropped in silence, so the track stays in the document and animates nothing."
            )
        elif root == "objects" and len(parts) > 1 and object_ids and parts[1] not in object_ids:
            problems.append(
                f"Track {i} targets object {parts[1]!r}, which this scene has no object for. "
                f"It will animate nothing."
            )
        keys = t.get("keys") or []
        if len(keys) < 2:
            problems.append(
                f"Track {i} ({target}) has {len(keys)} key(s). Two are the minimum that define "
                f"an interpolation; below that it holds a constant and animates nothing while "
                f"still appearing to be a track."
            )
        times = [k.get("t") for k in keys if isinstance(k, dict)]
        if times and times != sorted(times):
            problems.append(
                f"Track {i} ({target}) has keys out of order by `t`. They are sampled in order "
                f"and assumed to ascend, so part of the range will interpolate BACKWARDS, which "
                f"reads as the easing being wrong rather than the keys being wrong."
            )
        if enums.get("easing") and t.get("ease") and t["ease"] not in enums["easing"]:
            problems.append(_enum_hint(f"tracks[{i}].ease", t["ease"], enums["easing"]))

    for s in doc.get("shaders") or []:
        if not isinstance(s, dict):
            continue
        frag = s.get("fragment") or ""
        if not frag.strip():
            problems.append(f"Shader {s.get('id')!r} has an empty fragment source and cannot compile.")
            continue
        if "gl_FragColor" not in frag and "out " not in frag:
            problems.append(
                f"Shader {s.get('id')!r} never writes gl_FragColor. It will compile and render "
                f"nothing, which is indistinguishable from the component being broken."
            )
        declared = {u.get("name") for u in (s.get("uniforms") or []) if isinstance(u, dict)}
        for name in declared:
            if name in shared:
                problems.append(
                    f"Shader {s.get('id')!r} declares a document uniform named {name!r}, which "
                    f"the runtime already binds for every shader. Yours SHADOWS the live value "
                    f"with a frozen one, so the effect stops responding. Remove it."
                )
        import re as _re
        for m in _re.finditer(r"^\s*uniform\s+\w+\s+(\w+)\s*;", frag, _re.M):
            name = m.group(1)
            if name not in declared and name not in shared:
                problems.append(
                    f"Shader {s.get('id')!r} declares uniform {name!r} in its GLSL but the scene "
                    f"gives it no value, so it stays at zero -- which usually renders black."
                )

    for i, it in enumerate(doc.get("interactions") or []):
        if not isinstance(it, dict):
            continue
        if enums.get("interactionGesture") and it.get("on") not in enums["interactionGesture"]:
            problems.append(_enum_hint(f"interactions[{i}].on", it.get("on"), enums["interactionGesture"]))
        if not it.get("event"):
            problems.append(f"Interaction {i} names no page event function, so it does nothing.")
        tid = it.get("targetId")
        if tid and object_ids and tid not in object_ids:
            problems.append(
                f"Interaction {i} targets object {tid!r}, which this scene has no object for. "
                f"It can never fire."
            )

    return problems


async def _execute_validate_scene(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    scene = params.get("scene")
    if isinstance(scene, str):
        try:
            scene = json.loads(scene)
        except json.JSONDecodeError as e:
            return ToolResult(success=False, error=f"`scene` is not valid JSON: {e}")
    if scene is None:
        return ToolResult(success=False, error="`scene` (object or JSON string) is required")

    problems = validate_scene_document(scene)
    if not problems:
        return ToolResult(
            success=True,
            summary=(
                "The scene document validates clean.\n\n"
                "Validation is structural. It cannot tell you the scene LOOKS right: a shader "
                "that compiles and renders black passes every check here. Run "
                "`render_scene_check` once it is on a page."
            ),
        )
    body = "\n".join(f"  {i + 1}. {p}" for i, p in enumerate(problems))
    return ToolResult(success=True, summary=f"{len(problems)} problem(s):\n\n{body}")


validate_scene_tool = ToolDefinition(
    name="validate_scene",
    description=(
        "Check a scene document against the contract the platform publishes, and return the "
        "problems as sentences with their CONSEQUENCE attached ('renders black', 'animates "
        "nothing') rather than as a schema dump. Run before `set_scene`."
    ),
    parameters=[
        ToolParameter(
            name="scene", type="object", required=True,
            description="The scene document, as an object or a JSON string",
        ),
    ],
    execute=_execute_validate_scene,
)


TOOLS: list[ToolDefinition] = [
    list_scene_presets_tool,
    get_scene_tool,
    validate_scene_tool,
]


# ═════════════════════════════════════════════════════════════════════════
#  set_scene / patch_scene
# ═════════════════════════════════════════════════════════════════════════


async def _write_scene(
    params: dict[str, Any], context: dict[str, Any], scene: dict[str, Any],
    page_name: str, component_key: str, note: str,
) -> ToolResult:
    """Write a scene onto a component, clearing `preset` in the same change.

    Both set at once is ambiguous -- the runtime prefers the document, but an
    author reading the definition cannot tell which is live -- so the preset
    name is removed rather than left behind. That mirrors what the Scene Editor
    does on its first commit, and what SvgContentEditor does for src/svgContent.
    """
    from .pages import _patch_component_on_server  # local: avoids a cycle at import

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp, cerr = _page_fetch_component(page, component_key)
    if cerr:
        return ToolResult(success=False, error=cerr)
    assert comp is not None

    updated = dict(comp)
    props = dict(updated.get("properties") or {})
    props["scene"] = {"value": scene}
    dropped = props.pop("preset", None)
    updated["properties"] = props

    ok, perr = await _patch_component_on_server(
        page_name, component_key, updated, context, params.get("message") or note
    )
    if not ok:
        return ToolResult(success=False, error=perr)

    problems = validate_scene_document(scene)
    lines = [f"Wrote the scene onto '{component_key}' ({comp.get('type')})."]
    if dropped:
        lines.append(
            f"Cleared `preset` (was {(dropped or {}).get('value')!r}): the page now OWNS this "
            f"scene, so later changes to the built-in preset will not reach it."
        )
    lines.append(
        f"objects={len(scene.get('objects') or [])} lights={len(scene.get('lights') or [])} "
        f"shaders={len(scene.get('shaders') or [])} "
        f"tracks={len((scene.get('timeline') or {}).get('tracks') or [])}"
    )
    if problems:
        lines.append("")
        lines.append("It was written, but validation still reports:")
        lines.extend(f"  - {p}" for p in problems)
    lines.append("")
    lines.append("Now run `render_scene_check` — validation cannot tell you it renders.")
    return ToolResult(success=True, summary="\n".join(lines))


async def _execute_set_scene(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    if not page_name or not component_key:
        return ToolResult(success=False, error="`page_name` and `component_key` are required")

    scene = params.get("scene")
    if isinstance(scene, str):
        try:
            scene = json.loads(scene)
        except json.JSONDecodeError as e:
            return ToolResult(success=False, error=f"`scene` is not valid JSON: {e}")
    if not isinstance(scene, dict):
        return ToolResult(success=False, error="`scene` must be an object (or a JSON string of one)")

    problems = validate_scene_document(scene)
    blocking = [p for p in problems if "renders empty" in p or "renders BLACK" in p or "cannot compile" in p]
    if blocking and not params.get("force"):
        body = "\n".join(f"  - {p}" for p in problems)
        return ToolResult(
            success=False,
            error=(
                "Refused: this scene would render nothing. Fix these, or pass force=true if "
                f"you mean it:\n\n{body}"
            ),
        )
    return await _write_scene(params, context, scene, page_name, component_key, "Set scene via CFA")


set_scene_tool = ToolDefinition(
    name="set_scene",
    description=(
        "Write a whole scene document onto a WebGL component, clearing its `preset` in the same "
        "change so the two can never disagree. Validates first and REFUSES a scene that would "
        "render nothing at all (no objects, nothing lit, a shader that cannot compile) unless "
        "force=true.\n\n"
        "Prefer `preset` when a built-in fits: a stored document is several KB of page JSON and, "
        "once written, the page owns it."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", required=True, description=_DESC_PAGE),
        ToolParameter(name="component_key", type="string", required=True, description=_DESC_KEY),
        ToolParameter(name="scene", type="object", required=True, description="The scene document"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="force", type="boolean", required=False, default=False,
                      description="Write even when the scene would render nothing"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_set_scene,
)


def _apply_scene_patch(scene: dict[str, Any], patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Merge a targeted patch into a scene document.

    Objects, lights and shaders are merged BY ID rather than replaced
    wholesale, which is the whole point of having this separate from
    `set_scene`: changing one material colour should not mean round-tripping a
    document carrying kilobytes of GLSL through a capped tool result.
    """
    import copy
    out = copy.deepcopy(scene)
    notes: list[str] = []

    for coll in ("objects", "lights", "shaders"):
        incoming = patch.get(coll)
        if not isinstance(incoming, list):
            continue
        existing = out.setdefault(coll, [])
        by_id = {e.get("id"): e for e in existing if isinstance(e, dict)}
        for entry in incoming:
            if not isinstance(entry, dict) or not entry.get("id"):
                notes.append(f"An entry in patch.{coll} has no id and was skipped; merges are by id.")
                continue
            target = by_id.get(entry["id"])
            if target is None:
                existing.append(entry)
                notes.append(f"Added {coll[:-1]} {entry['id']!r}.")
                continue
            for k, v in entry.items():
                if isinstance(v, dict) and isinstance(target.get(k), dict):
                    target[k] = {**target[k], **v}
                else:
                    target[k] = v
            notes.append(f"Merged into {coll[:-1]} {entry['id']!r}: {sorted(entry.keys() - {'id'})}.")

    for key in ("camera", "environment", "renderer", "timeline"):
        incoming = patch.get(key)
        if isinstance(incoming, dict):
            out[key] = {**(out.get(key) or {}), **incoming}
            notes.append(f"Merged into {key}: {sorted(incoming.keys())}.")

    if isinstance(patch.get("interactions"), list):
        out["interactions"] = patch["interactions"]
        notes.append("Replaced interactions wholesale (they have no ids to merge by).")

    return out, notes


async def _execute_patch_scene(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    patch = params.get("patch")
    if isinstance(patch, str):
        try:
            patch = json.loads(patch)
        except json.JSONDecodeError as e:
            return ToolResult(success=False, error=f"`patch` is not valid JSON: {e}")
    if not page_name or not component_key or not isinstance(patch, dict):
        return ToolResult(success=False, error="`page_name`, `component_key` and `patch` (object) are required")

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp, cerr = _page_fetch_component(page, component_key)
    if cerr:
        return ToolResult(success=False, error=cerr)
    assert comp is not None

    scene = ((comp.get("properties") or {}).get("scene") or {}).get("value")
    if not isinstance(scene, dict):
        preset = ((comp.get("properties") or {}).get("preset") or {}).get("value")
        return ToolResult(
            success=False,
            error=(
                f"'{component_key}' has no stored scene document to patch — it is still on the "
                f"preset {preset!r}. Change the component's own properties with "
                f"`patch_component_props`, or write a full document with `set_scene` first. "
                f"Patching a preset is not possible: it is rebuilt from source on every render."
            ),
        )

    merged, notes = _apply_scene_patch(scene, patch)
    result = await _write_scene(params, context, merged, page_name, component_key, "Patched scene via CFA")
    if result.success and notes:
        result.summary = "\n".join(notes) + "\n\n" + (result.summary or "")
    return result


patch_scene_tool = ToolDefinition(
    name="patch_scene",
    description=(
        "Change part of a stored scene without round-tripping the whole document. Objects, "
        "lights and shaders merge BY ID; camera, environment, renderer and timeline merge "
        "key-by-key. Use this to change a material colour, move a light or retime a track — "
        "reading and rewriting a document full of GLSL just to edit one number is how a tool "
        "result gets truncated and the tail of a scene lost."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", required=True, description=_DESC_PAGE),
        ToolParameter(name="component_key", type="string", required=True, description=_DESC_KEY),
        ToolParameter(
            name="patch", type="object", required=True,
            description="Partial scene. Entries in objects/lights/shaders need an `id` to merge by.",
        ),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="force", type="boolean", required=False, default=False,
                      description="Write even when the result would render nothing"),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_patch_scene,
)


TOOLS.extend([set_scene_tool, patch_scene_tool])


# ═════════════════════════════════════════════════════════════════════════
#  compile_shader
# ═════════════════════════════════════════════════════════════════════════


_DEFAULT_VERTEX = """
varying vec2 vUv;
void main() {
	vUv = uv;
	gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
"""

# three does NOT inject uniform declarations. It binds VALUES to names the
# source already declares, so a harness that declares them for you reports
# "redefinition" on a perfectly good shader -- which is exactly how the first
# version of this probe lied. What three DOES prepend is the built-in attribute
# and matrix set, reproduced here so a shader written for the real runtime
# compiles the same way in the probe.
_THREE_PREAMBLE_VERTEX = """
precision highp float;
uniform mat4 modelViewMatrix;
uniform mat4 projectionMatrix;
uniform mat3 normalMatrix;
attribute vec3 position;
attribute vec3 normal;
attribute vec2 uv;
"""

_THREE_PREAMBLE_FRAGMENT = """
precision highp float;
"""

_COMPILE_JS = """(args) => {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2') || c.getContext('webgl');
    if (!gl) return {noContext: true};
    const out = {noContext: false, stages: {}};
    for (const [stage, type, src] of [
        ['vertex', gl.VERTEX_SHADER, args.vertex],
        ['fragment', gl.FRAGMENT_SHADER, args.fragment],
    ]) {
        const sh = gl.createShader(type);
        gl.shaderSource(sh, src);
        gl.compileShader(sh);
        out.stages[stage] = {
            ok: !!gl.getShaderParameter(sh, gl.COMPILE_STATUS),
            log: gl.getShaderInfoLog(sh) || '',
        };
        gl.deleteShader(sh);
    }
    out.renderer = (() => {
        const d = gl.getExtension('WEBGL_debug_renderer_info');
        return d ? String(gl.getParameter(d.UNMASKED_RENDERER_WEBGL)) : 'unknown';
    })();
    return out;
}"""


def _annotate_compile_log(log: str, source: str, preamble_lines: int) -> str:
    """Re-point line numbers at the author's source and quote the offending line.

    The driver counts from the top of what it was handed, which includes the
    preamble the runtime prepends. Reporting those raw numbers sends the agent
    editing a line it never wrote.
    """
    import re as _re
    src_lines = source.split("\n")
    out: list[str] = []
    for line in log.strip().split("\n"):
        if not line.strip():
            continue
        m = _re.match(r"^(ERROR|WARNING):\s*(\d+):(\d+):\s*(.*)$", line.strip())
        if not m:
            out.append(line.strip())
            continue
        kind, _col, raw_line, msg = m.groups()
        n = int(raw_line) - preamble_lines
        if 1 <= n <= len(src_lines):
            out.append(f"{kind} on line {n}: {msg}")
            out.append(f"    {src_lines[n - 1].strip()}")
        else:
            out.append(f"{kind}: {msg}  [line {raw_line} is inside the runtime preamble]")
    return "\n".join(out)


async def _execute_compile_shader(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from app.services import browser_pool
    from app.services.browser_pool import EXTERNAL, BrowserUnavailable

    fragment = params.get("fragment") or ""
    vertex = params.get("vertex") or ""
    if not fragment.strip() and not vertex.strip():
        return ToolResult(success=False, error="Pass `fragment` and/or `vertex` GLSL to compile.")

    full_vertex = _THREE_PREAMBLE_VERTEX.lstrip("\n") + (vertex or _DEFAULT_VERTEX.lstrip("\n"))
    full_fragment = _THREE_PREAMBLE_FRAGMENT.lstrip("\n") + fragment
    v_preamble = len(_THREE_PREAMBLE_VERTEX.lstrip("\n").split("\n")) - 1
    f_preamble = len(_THREE_PREAMBLE_FRAGMENT.lstrip("\n").split("\n")) - 1

    try:
        # EXTERNAL profile: nothing here touches a Modlix page or an end-user
        # token, and a compile probe has no business in a context that carries
        # one.
        ctx = await browser_pool.open_context(EXTERNAL, persistent=False, viewport={"width": 8, "height": 8})
    except BrowserUnavailable as e:
        return ToolResult(success=False, error=f"No browser available to compile against: {e}")
    try:
        page = await ctx.new_page()
        await page.set_content("<html><body></body></html>")
        res = await page.evaluate(_COMPILE_JS, {"vertex": full_vertex, "fragment": full_fragment})
    except Exception as e:  # noqa: BLE001
        return ToolResult(success=False, error=f"Compile probe failed: {type(e).__name__}: {e}")
    finally:
        await browser_pool.close_context(ctx)

    if res.get("noContext"):
        return ToolResult(
            success=False,
            error=(
                "This browser gave no WebGL context, so NOTHING was compiled. That is a problem "
                "with the checker, not with your shader — do not rewrite it on the strength of "
                "this result."
            ),
        )

    lines = [f"Compiled against: {res.get('renderer')}", ""]
    failed = False
    for stage, src, preamble in (
        ("vertex", vertex or _DEFAULT_VERTEX, v_preamble),
        ("fragment", fragment, f_preamble),
    ):
        info = (res.get("stages") or {}).get(stage) or {}
        if stage == "vertex" and not vertex.strip():
            if info.get("ok"):
                continue  # The stock vertex shader; nothing to report.
        if info.get("ok"):
            lines.append(f"{stage}: OK")
            if info.get("log", "").strip():
                lines.append(_annotate_compile_log(info["log"], src, preamble))
        else:
            failed = True
            lines.append(f"{stage}: FAILED")
            lines.append(_annotate_compile_log(info.get("log", ""), src, preamble))
        lines.append("")

    if not failed:
        lines.append(
            "Compiling is NOT rendering. A shader that compiles cleanly and writes a constant, "
            "or never writes gl_FragColor at all, renders black and passes this check. Put it on "
            "a page and run `render_scene_check`."
        )
    else:
        lines.append(
            "Note: uTime, uResolution, uPointer, uPointerActive and uProgress are bound for you "
            "at runtime, but their DECLARATIONS are not added for you. Declare each one you use."
        )
    return ToolResult(success=not failed, summary="\n".join(lines).strip())


compile_shader_tool = ToolDefinition(
    name="compile_shader",
    description=(
        "Compile GLSL in a real WebGL driver and return errors with line numbers pointing at "
        "YOUR source (the runtime preamble is subtracted) and the offending line quoted. "
        "Headless Chrome is the only faithful GLSL ES compiler available, so this is the same "
        "compiler the page will use.\n\n"
        "A clean compile does NOT mean the shader draws anything — use `render_scene_check` for "
        "that."
    ),
    parameters=[
        ToolParameter(name="fragment", type="string", required=False, description="Fragment shader source"),
        ToolParameter(
            name="vertex", type="string", required=False,
            description="Vertex shader source; omit to compile against the stock one",
        ),
    ],
    execute=_execute_compile_shader,
)


TOOLS.append(compile_shader_tool)


# ═════════════════════════════════════════════════════════════════════════
#  render_scene_check
# ═════════════════════════════════════════════════════════════════════════
#
# Sampled from the SCREENSHOT, not from gl.readPixels.
#
# readPixels returns all zeros on a live page: the renderer is created without
# preserveDrawingBuffer, so the drawing buffer is cleared before any read from
# outside the frame callback can reach it. Measured 2026-09-25 against a working
# ModelViewer -- the model was plainly visible in the screenshot while readPixels
# reported a uniformly black canvas. A checker built on readPixels would have
# called every working scene blank, which for an agent in a retry loop is the
# worst possible failure: it rewrites correct code until it breaks.


_WEBGL_CAPABILITY_JS = """() => {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2') || c.getContext('webgl');
    if (!gl) return {ok: false};
    const d = gl.getExtension('WEBGL_debug_renderer_info');
    return {ok: true, renderer: d ? String(gl.getParameter(d.UNMASKED_RENDERER_WEBGL)) : 'unknown'};
}"""


async def _browser_can_webgl() -> tuple[bool, str]:
    """Can the CHECKER's browser do WebGL at all?

    Asked separately from the page, and this is the whole reason the tool can be
    trusted. Without it, a browser with no GPU stack reports every scene as
    blank, the agent believes its shader is broken, and it rewrites working code
    until it stops working. The answer has to be attributable to the browser or
    to the scene, never to "something".
    """
    from app.services import browser_pool
    from app.services.browser_pool import EXTERNAL, BrowserUnavailable
    try:
        ctx = await browser_pool.open_context(EXTERNAL, persistent=False, viewport={"width": 8, "height": 8})
    except BrowserUnavailable as e:
        return False, f"no browser available: {e}"
    try:
        page = await ctx.new_page()
        await page.set_content("<html><body></body></html>")
        res = await page.evaluate(_WEBGL_CAPABILITY_JS)
        return bool(res.get("ok")), str(res.get("renderer") or "")
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        await browser_pool.close_context(ctx)


def _image_stats(png: bytes) -> dict[str, Any] | None:
    """How varied is this frame, and how bright?

    Measured at FULL resolution, not on a downsample.

    The first version resized to 64x64 first, which was wrong in a way that
    only a real capture showed: resizing averages, and a sparse particle field
    -- thousands of 1px motes on a dark background -- averages straight back
    into its background. A working ParticleField measured 2 distinct colours
    and would have been reported as a flat fill. That is precisely the outcome
    this whole tool exists to avoid: telling an agent that working code is
    broken.

    `getcolors` and `getextrema` are C-level, so full resolution is cheap.
    """
    try:
        import io
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:  # noqa: BLE001
        return None

    # Capped: the count is only used as "more than a couple", so an image with
    # tens of thousands of colours does not need an exact answer.
    colours = im.getcolors(maxcolors=65536)
    distinct = len(colours) if colours is not None else 65536

    extrema = im.getextrema()  # ((rmin, rmax), (gmin, gmax), (bmin, bmax))
    spread = max(hi - lo for lo, hi in extrema)

    w, h = im.size
    px = im.load()
    # A strided sample for the frame comparison, taken from full resolution so
    # a moving mote is not averaged away the way a resize would do.
    step = max(1, int(((w * h) / 8192) ** 0.5))
    sample = [px[x, y] for y in range(0, h, step) for x in range(0, w, step)]
    mean = [round(sum(p[i] for p in sample) / max(1, len(sample))) for i in range(3)]

    return {
        "distinctColours": distinct,
        "spread": spread,
        "mean": mean,
        "raw": sample,
    }


def _frames_differ(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Fraction of sampled pixels that changed between two frames."""
    pa, pb = a.get("raw") or [], b.get("raw") or []
    if not pa or len(pa) != len(pb):
        return 0.0
    moved = sum(1 for x, y in zip(pa, pb) if sum(abs(i - j) for i, j in zip(x, y)) > 24)
    return moved / len(pa)


def _webgl_console_errors(console: Any) -> list[str]:
    lines: list[str] = []
    for entry in console or []:
        text = entry if isinstance(entry, str) else str((entry or {}).get("text") or entry)
        low = text.lower()
        if any(k in low for k in ("webgl", "shader", "glsl", "three", "context lost", "compile")):
            lines.append(text[:300])
    return lines[:8]


async def _shoot(params: dict[str, Any], context: dict[str, Any], **extra: Any):
    from .visuals_browser import _execute_screenshot_page
    import base64 as _b64

    shot_params = {
        k: v for k, v in params.items()
        if k in ("app_code", "client_code", "page_name", "path_segments",
                 "width", "height", "draft", "query")
    }
    shot_params.update(extra)
    shot_params["capture_console"] = True
    # Not full_page: the check is about what a visitor SEES, and a tall page
    # stitched into one image dilutes a 420px hero into a strip of background
    # that reads as flat no matter what the shader is doing.
    shot_params["full_page"] = False
    res = await _execute_screenshot_page(shot_params, context)
    if not res.success:
        return None, None, res.error
    data = res.data or {}
    b64 = data.get("image_base64")
    if not b64:
        return None, data, "the screenshot returned no image"
    try:
        return _b64.b64decode(b64), data, None
    except Exception as e:  # noqa: BLE001
        return None, data, f"could not decode the screenshot: {e}"


async def _execute_render_scene_check(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    page_name = (params.get("page_name") or "").strip()
    if not page_name:
        return ToolResult(success=False, error="`page_name` is required")
    expect_motion = params.get("expect_motion")
    if expect_motion is None:
        expect_motion = True

    lines = [f"Render check on '{page_name}':", ""]

    # 1. The browser, before the page. This is what makes a failure below
    #    attributable to the scene rather than to the checker.
    can, renderer = await _browser_can_webgl()
    if not can:
        lines.append(
            f"The CHECKER's browser cannot create a WebGL context ({renderer}). This check can "
            f"tell you NOTHING about your scene. Do not rewrite it on the strength of this "
            f"result — fix the browser environment instead."
        )
        return ToolResult(success=False, summary="\n".join(lines))
    lines.append(f"  checker WebGL: available ({renderer})")

    # 2. Is a canvas on the page at all? A missing one means the component
    #    never mounted, which is a different problem from a blank shader.
    png_a, data_a, err = await _shoot(
        params, context,
        page_name=page_name,
        wait_for_selector="canvas",
        wait_ms=int(params.get("wait_ms") or 6000),
    )
    if err and "not found within" in err:
        lines.append("")
        lines.append(
            "No <canvas> appeared on the page within 10s. The WebGL component either is not on "
            "this page, or failed before mounting. three.js loads as a SEPARATE CHUNK, so a "
            "slow first load is a real possibility — retry with a larger wait_ms before "
            "concluding anything. Check the component's onError event too."
        )
        return ToolResult(success=False, summary="\n".join(lines))
    if err:
        return ToolResult(success=False, error=f"Could not screenshot the page: {err}")

    png_b, data_b, err_b = await _shoot(
        params, context, page_name=page_name, wait_ms=int(params.get("gap_ms") or 900),
    )
    lines.append("  canvas: present")
    lines.append("")

    console_hits = _webgl_console_errors((data_a or {}).get("console"))

    a = _image_stats(png_a) if png_a else None
    b = _image_stats(png_b) if png_b else None
    if not a:
        lines.append("Pillow is unavailable, so the pixel checks could not run.")
        return ToolResult(success=False, summary="\n".join(lines))

    # `spread` as well as the colour count: a sparse particle field is mostly
    # dark, so its MEAN is low and a mean-only check would fail it, but its
    # brightest pixels are far from its darkest.
    verdicts: list[tuple[bool, str]] = [
        # ONE colour is a flat fill. Two is not: a stark two-tone render is
        # unusual but perfectly legitimate, and a higher threshold would fail
        # it for being simple.
        (a["distinctColours"] > 1,
         f"not a flat fill (distinct colours: {a['distinctColours']})"),
        (sum(a["mean"]) > 12 or a["spread"] > 40,
         f"something is drawn (mean RGB {a['mean']}, brightest-to-darkest spread "
         f"{a['spread']})"),
    ]
    moved = _frames_differ(a, b) if b else 0.0
    if expect_motion and b:
        verdicts.append((moved > 0.005,
                         f"the two frames differ ({moved * 100:.1f}% of sampled pixels moved)"))
    elif not expect_motion:
        lines.append("  (motion not checked: expect_motion=false)")

    for ok, text in verdicts:
        lines.append(f"  {'PASS' if ok else 'FAIL'}  {text}")

    if console_hits:
        lines.append("")
        lines.append("Browser console, WebGL-related:")
        lines.extend(f"  {h}" for h in console_hits)

    failures = [t for ok, t in verdicts if not ok]
    if not failures:
        lines.append("")
        lines.append("The scene renders" + (" and it moves." if expect_motion else "."))
        return ToolResult(success=True, summary="\n".join(lines))

    lines.append("")
    lines.append("What each failure usually means:")
    if a["distinctColours"] <= 1:
        lines.append(
            "  - A flat fill is the classic 'compiles and renders black'. Check the shader "
            "writes gl_FragColor from something that VARIES; check every uniform it reads has "
            "a value in the scene, because an undeclared one stays at zero; and check the "
            "scene has a light if any object uses a standard material."
        )
    if sum(a["mean"]) <= 12 and a["spread"] <= 40:
        lines.append(
            "  - A black frame with a live context usually means nothing was drawn: no visible "
            "objects, a camera pointed away from them, or a model that never loaded. "
            "`validate_scene` catches most of these before they reach a page."
        )
    if expect_motion and moved <= 0.005:
        lines.append(
            "  - Two identical frames mean the scene is static. If its timeline driver is "
            "'scroll' that is CORRECT — nothing has scrolled — so pass expect_motion=false, or "
            "use `drive_page` to scroll and compare the shots with your own eyes."
        )
    lines.append(
        "  - The screenshot is attached to the earlier tool result. LOOK at it before changing "
        "anything: these are heuristics on downsampled pixels, and your own eyes outrank them."
    )
    return ToolResult(success=False, summary="\n".join(lines))


render_scene_check_tool = ToolDefinition(
    name="render_scene_check",
    description=(
        "Verify that a WebGL component on a page actually RENDERS. Probes the checker's own "
        "browser for WebGL FIRST, then takes two frames a moment apart and asserts three things "
        "about real pixels: the canvas is not a flat fill, it is not black, and the two frames "
        "differ.\n\n"
        "This is the check that catches the failure that actually happens — a shader that "
        "compiles cleanly and draws nothing. Because it probes the browser separately, a "
        "failure here is attributable: it says whether the problem is your scene or the "
        "environment, so it is never a reason to start rewriting working code.\n\n"
        "For a scroll-driven scene pass expect_motion=false: nothing has scrolled, so two "
        "identical frames are correct."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", required=True, description=_DESC_PAGE),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False, description="clientCode; defaults to session"),
        ToolParameter(name="wait_ms", type="integer", required=False, default=6000,
                      description="Wait before the first frame; three.js loads as a separate chunk"),
        ToolParameter(name="gap_ms", type="integer", required=False, default=900,
                      description="Gap between the two frames"),
        ToolParameter(name="expect_motion", type="boolean", required=False, default=True,
                      description="False for a scroll-driven or deliberately static scene"),
        ToolParameter(name="width", type="integer", required=False, description="Viewport width"),
        ToolParameter(name="height", type="integer", required=False, description="Viewport height"),
        ToolParameter(name="path_segments", type="array", required=False,
                      description="Path parts after /page/<name>/", items={"type": "string"}),
    ],
    execute=_execute_render_scene_check,
)


TOOLS.append(render_scene_check_tool)


# ═════════════════════════════════════════════════════════════════════════
#  set_scroll_animation
# ═════════════════════════════════════════════════════════════════════════


def _animation_field_enum(field: str) -> list[str]:
    """Legal values for one field of an animation entry, from the catalog."""
    from app.agents.appbuilder.catalog import get_catalog
    info = get_catalog().get_component("Animator") or {}
    for prop in info.get("properties") or []:
        if prop.get("name") != "animation":
            continue
        for sub in prop.get("subProperties") or []:
            if sub.get("name") == field:
                return [e.get("name") for e in (sub.get("enumValues") or []) if e.get("name")]
    return []


async def _execute_set_scroll_animation(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    from .pages import _patch_component_on_server

    page_name = (params.get("page_name") or "").strip()
    component_key = (params.get("component_key") or "").strip()
    name = (params.get("animation_name") or "").strip()
    if not page_name or not component_key or not name:
        return ToolResult(
            success=False,
            error="`page_name`, `component_key` and `animation_name` are required",
        )

    # The underscore-prefix trap, checked here rather than left to fail
    # silently. An unprefixed name matches no @keyframes block, so the element
    # renders un-animated and nothing anywhere reports a problem -- the single
    # most common way a scroll animation "does not work".
    known = _animation_field_enum("animationName")
    if known and name not in known:
        suggestion = f"_{name}" if f"_{name}" in known else None
        hint = (
            f" Did you mean {suggestion!r}? Every keyframes name is UNDERSCORE-PREFIXED."
            if suggestion
            else f" Known names start with an underscore, e.g. {known[:6]}."
        )
        return ToolResult(
            success=False,
            error=(
                f"{name!r} is not a keyframes name this platform defines, so the element would "
                f"render un-animated with no error anywhere.{hint}"
            ),
        )
    if not known:
        # Said out loud rather than skipped: an unchecked name is exactly the
        # failure this guard exists for.
        pass

    timeline = (params.get("timeline") or "view").strip()
    for field, value in (("timeline", timeline), ("axis", params.get("axis") or "block"),
                         ("scroller", params.get("scroller") or "nearest")):
        allowed = _animation_field_enum(field)
        if allowed and value not in allowed:
            return ToolResult(success=False, error=f"{field} must be one of {allowed}, got {value!r}")

    try:
        start = float(params.get("range_start") if params.get("range_start") is not None else 0)
        end = float(params.get("range_end") if params.get("range_end") is not None else 1)
    except (TypeError, ValueError):
        return ToolResult(success=False, error="`range_start` and `range_end` must be numbers between 0 and 1")
    if not (0 <= start <= 1 and 0 <= end <= 1):
        return ToolResult(success=False, error="`range_start` and `range_end` must be between 0 and 1")
    if start >= end:
        return ToolResult(
            success=False,
            error=(
                f"range_start ({start}) must be less than range_end ({end}); an empty or "
                f"inverted range leaves the animation pinned at one frame."
            ),
        )

    duration = int(params.get("duration_ms") or 800)

    rule: dict[str, Any] = {
        "animationName": name,
        "animationDuration": duration,
        "animationTimingFunction": params.get("timing_function") or "linear",
        # `both`, so a scrubbed animation HOLDS at each end of its range instead
        # of snapping back to the un-animated state at 0 and 1.
        "animationFillMode": "both",
        # A string, not a number. The property is declared as a string and a
        # numeric 1 is not what the shorthand builder expects.
        "animationIterationCount": "1",
        "animationDelay": 0,
        "animationDirection": params.get("direction") or "normal",
        "condition": True,
        # Kept at 'none': observation SWITCHES an animation on at a threshold,
        # timeline SCRUBS it. Setting both would have the observer withhold the
        # animation from the very scrubbing that is supposed to drive it.
        "observation": "none",
        "timeline": timeline,
        "axis": params.get("axis") or "block",
        "scroller": params.get("scroller") or "nearest",
        "rangeStart": start,
        "rangeEnd": end,
    }

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    client, headers = _client_and_headers(context)
    page, err = await p_ops.fetch_page_by_name(client, page_name, ac, headers)
    if err:
        return ToolResult(success=False, error=err)
    assert page is not None
    comp = (page.get("componentDefinition") or {}).get(component_key)
    if not isinstance(comp, dict):
        return ToolResult(success=False, error=f"Component '{component_key}' not found on '{page_name}'.")

    ctype = comp.get("type") or ""
    existing_props = dict(comp.get("properties") or {})
    replace = params.get("replace")
    if replace is None:
        replace = True

    rules: list[dict[str, Any]] = [rule]
    if not replace:
        # Keep what is there. The existing entries are already in stored shape,
        # so they pass through wrap_multi_valued untouched.
        current = existing_props.get("animation")
        if isinstance(current, dict) and current:
            merged = dict(current)
            wrapped_new = c.wrap_props_catalog_aware(ctype, {"animation": rules}, {})["animation"]
            merged.update(wrapped_new)
            wrapped = {"animation": merged}
        else:
            wrapped = c.wrap_props_catalog_aware(ctype, {"animation": rules}, existing_props)
    else:
        wrapped = c.wrap_props_catalog_aware(ctype, {"animation": rules}, existing_props)

    # The nesting is the thing that fails silently, so it is asserted rather
    # than trusted: make.ts reads `each.property.value`, and a rule landing one
    # level out resolves EVERY field to its default -- _bounce for 0ms, which
    # reads as the property being ignored.
    entries = wrapped.get("animation") or {}
    bad = [
        k for k, e in entries.items()
        if not isinstance((e or {}).get("property"), dict)
        or not isinstance(e["property"].get("value"), dict)
    ]
    if bad:
        return ToolResult(
            success=False,
            error=(
                "Refused: the animation entries came out in the wrong shape "
                f"(entries {bad} have no property.value map). The runtime reads "
                "`each.property.value`, and a rule one level out resolves every field to its "
                "default, so the element animates _bounce for 0ms. This is a bug in the "
                "wrapping, not in your input."
            ),
        )

    existing_props.update(wrapped)
    updated = dict(comp)
    updated["properties"] = existing_props
    ok, perr = await _patch_component_on_server(
        page_name, component_key, updated, context,
        params.get("message") or "Set scroll animation via CFA",
    )
    if not ok:
        return ToolResult(success=False, error=perr)

    lines = [
        f"Set a scroll-driven {name} on '{component_key}' ({ctype}).",
        f"  driven by: {timeline}   axis: {rule['axis']}   range: {start} → {end}",
    ]
    if ctype != "Animator":
        lines.append("")
        lines.append(
            f"NOTE: '{component_key}' is a {ctype}, not an Animator. The `animation` property "
            f"is read by the Animator component; on anything else it is stored and ignored. "
            f"Wrap the element in an Animator and set it there."
        )
    lines.append("")
    lines.append(
        "The runtime picks the implementation: where the browser supports animation-timeline "
        "the animation is handed to it and runs off the main thread, elsewhere it is scrubbed "
        "from JS. Both produce the same frames.\n"
        "Verify by SCROLLING: `drive_page` with scroll actions and a screenshot at each stop. "
        "A single screenshot cannot tell a scroll-driven animation from a broken one."
    )
    return ToolResult(success=True, summary="\n".join(lines))


set_scroll_animation_tool = ToolDefinition(
    name="set_scroll_animation",
    description=(
        "Attach a scroll-driven animation to an Animator component: the animation is SCRUBBED "
        "to scroll position rather than played on a clock, so it runs backwards when the "
        "visitor scrolls back.\n\n"
        "Validates the keyframes name against the catalog, which catches the underscore-prefix "
        "trap (`_fadeInUp`, NOT `fadeInUp`) that otherwise leaves the element un-animated with "
        "no error anywhere. Builds the multiValued nesting correctly — getting that wrong "
        "resolves every field to its default and looks exactly like the property being ignored.\n\n"
        "axis='inline' is driven by a HORIZONTAL scroller (a Carousel, a Grid with overflow-x), "
        "which there is no CSS-only way to do here."
    ),
    parameters=[
        ToolParameter(name="page_name", type="string", required=True, description=_DESC_PAGE),
        ToolParameter(name="component_key", type="string", required=True,
                      description="Key of the Animator component"),
        ToolParameter(name="animation_name", type="string", required=True,
                      description="Keyframes name, underscore-prefixed (e.g. '_fadeInUp')"),
        ToolParameter(name="timeline", type="string", required=False, default="view",
                      description="'view' (this element crossing the screen) or 'scroll' (the whole scroller)"),
        ToolParameter(name="axis", type="string", required=False, default="block",
                      description="'block' (vertical) or 'inline' (a horizontal scroller)"),
        ToolParameter(name="scroller", type="string", required=False, default="nearest",
                      description="'nearest' | 'root' | 'self'. A Modlix page scrolls in its own container, so 'nearest' is almost always right."),
        ToolParameter(name="range_start", type="number", required=False, default=0,
                      description="Where in the 0..1 travel the animation begins"),
        ToolParameter(name="range_end", type="number", required=False, default=1,
                      description="Where in the 0..1 travel it completes"),
        ToolParameter(name="duration_ms", type="integer", required=False, default=800,
                      description="Animation length; under a scroll timeline this sets the shape, not the speed"),
        ToolParameter(name="timing_function", type="string", required=False, default="linear",
                      description="'linear' reads best when scrubbing; easing on top of scroll easing tends to feel wrong"),
        ToolParameter(name="direction", type="string", required=False, default="normal",
                      description="normal | reverse | alternate | alternate-reverse"),
        ToolParameter(name="replace", type="boolean", required=False, default=True,
                      description="Replace existing animations on the component, or add to them"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="message", type="string", required=False, description="Commit message"),
    ],
    execute=_execute_set_scroll_animation,
)


TOOLS.append(set_scroll_animation_tool)


# ═════════════════════════════════════════════════════════════════════════
#  fetch_external_asset
# ═════════════════════════════════════════════════════════════════════════


_ASSET_KINDS = {
    "model": ("glb", "model/gltf-binary", "models"),
    "hdri": ("hdr", "image/vnd.radiance", "hdri"),
    "image": ("png", "image/png", "images"),
}


async def _execute_fetch_external_asset(params: dict[str, Any], context: dict[str, Any]) -> ToolResult:
    """Copy a public asset into the app's own files and return the platform URL.

    INGESTED, never hotlinked. A page pointing straight at someone else's URL
    breaks when they move it, breaks on CORS, breaks on CSP, and bypasses the
    CDN entirely. The copy lives in the app's static space like any other asset.
    """
    from . import _safe_fetch as sf
    from .clone_ops import _upload_bytes_as_static

    url = (params.get("url") or "").strip()
    kind = (params.get("kind") or "model").strip()
    if not url:
        return ToolResult(success=False, error="`url` is required")
    if kind not in _ASSET_KINDS:
        return ToolResult(success=False, error=f"`kind` must be one of {sorted(_ASSET_KINDS)}")

    ac, err_result = _resolve_app_code(params, context)
    if err_result:
        return err_result
    cc = params.get("client_code") or context.get("client_code") or "SYSTEM"
    _client, headers = _client_and_headers(context)

    try:
        max_mb = float(params.get("max_mb") or 32)
    except (TypeError, ValueError):
        max_mb = 32.0

    try:
        got = await sf.fetch_public_url(url, max_bytes=int(max_mb * 1024 * 1024))
    except sf.BlockedURL as e:
        return ToolResult(success=False, error=str(e))

    ok, detail = sf.VALIDATORS[kind](got.content)
    if not ok:
        return ToolResult(
            success=False,
            error=(
                f"{got.final_url} did not return a usable {kind}: {detail}\n"
                f"It was {len(got.content):,} bytes of {got.content_type or 'unknown type'}. "
                f"The HEADER is checked, not the file extension, because a .glb that is really "
                f"an HTML error page loads as nothing and leaves an empty canvas with no clue."
            ),
        )

    default_ext, default_mime, folder = _ASSET_KINDS[kind]
    from urllib.parse import urlsplit
    tail = (urlsplit(got.final_url).path or "").rsplit("/", 1)[-1]
    filename = (params.get("filename") or tail or f"asset.{default_ext}").strip()
    if "." not in filename:
        filename = f"{filename}.{default_ext}"

    ok_up, public_url, up_err = await _upload_bytes_as_static(
        ac=ac, cc=cc, headers=headers, payload=got.content,
        filename=filename, mime=got.content_type or default_mime,
        page_name="global", folder=folder,
    )
    if not ok_up:
        return ToolResult(success=False, error=f"Fetched, but the upload failed: {up_err}")

    import datetime as _dt
    fetched_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")

    lines = [
        f"Ingested {len(got.content):,} bytes ({detail}) into this app's files.",
        "",
        f"  use this URL:  {public_url}",
        "",
        "Provenance:",
        f"  source:     {url}",
    ]
    if got.final_url != url:
        lines.append(f"  fetched:    {got.final_url}  (after {len(got.redirects)} redirect(s))")
    lines.extend([
        f"  fetched at: {fetched_at}",
        "",
        "LICENSING IS UNVERIFIED. Nothing here checked whether this asset may be used in "
        "this app, and a downloaded file carries no permission with it. Record the source "
        "and the licence in the app's knowledge base (`propose_kb_update`, decisions_log) so "
        "whoever ships this can see what came from where.",
        "",
        "Wire it in with "
        f"`patch_component_props(component_key=..., properties={{\"modelUrl\": \"{public_url}\"}})` "
        "on a ModelViewer, or as `hdriUrl` for an environment.",
    ])
    return ToolResult(success=True, summary="\n".join(lines))


fetch_external_asset_tool = ToolDefinition(
    name="fetch_external_asset",
    description=(
        "Download a 3D model (.glb/.gltf), an HDRI (.hdr) or an image from any PUBLIC URL and "
        "copy it into this app's own files, returning the platform URL to use.\n\n"
        "INGESTS rather than hotlinks: a page pointing at someone else's URL breaks when they "
        "move it, breaks on CORS and CSP, and bypasses the CDN.\n\n"
        "Validates the file HEADER, not its extension — a .glb that is really an HTML login "
        "page loads as nothing and leaves an empty canvas with no clue why.\n\n"
        "Addresses inside the server's own network are refused (loopback, private ranges, and "
        "the cloud metadata endpoint). That is not a restriction on which asset libraries you "
        "may use: every genuinely public URL works.\n\n"
        "Licensing is NOT verified. Record the source and licence in the app's knowledge base."
    ),
    parameters=[
        ToolParameter(name="url", type="string", required=True, description="Public URL of the asset"),
        ToolParameter(name="kind", type="string", required=False, default="model",
                      description="'model' (.glb/.gltf) | 'hdri' (.hdr) | 'image'"),
        ToolParameter(name="filename", type="string", required=False,
                      description="Name to store it under; defaults to the name in the URL"),
        ToolParameter(name="max_mb", type="number", required=False, default=32,
                      description="Size cap in MB; a model over ~10MB is a slow page"),
        ToolParameter(name="app_code", type="string", required=False, description=_DESC_APP_CODE),
        ToolParameter(name="client_code", type="string", required=False,
                      description="clientCode; defaults to session"),
    ],
    execute=_execute_fetch_external_asset,
)


TOOLS.append(fetch_external_asset_tool)
