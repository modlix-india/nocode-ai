"""Explain, in plain language, what separates a saved version from the current one.

Backs the workspace version-history "Compare" step: before anybody loads an old version
over their work, they get told what that would actually change.

The comparison itself is done here in Python, not by the model. A definition document is
mostly machine-generated UUIDs and nested style maps; asking a model to eyeball two of
those is both expensive and unreliable. So we compute an exact structural diff first and
hand the model only that list, which makes the narration cheap, bounded, and grounded in
something that cannot be hallucinated. When the two documents match, no model is called
at all.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Tuple

from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

# Audit and concurrency fields. They differ on EVERY pair by construction, so reporting
# them would bury the one line that matters. `message` is the save note, which the UI
# already shows on the row.
_NOISE_KEYS = {
    "id",
    "_id",
    "version",
    "createdAt",
    "createdBy",
    "updatedAt",
    "updatedBy",
    "message",
    "_class",
}

# Hard stops so a page with a thousand components cannot blow up the request.
_MAX_DIFFS = 400
_MAX_DIFFS_TO_MODEL = 120
_MAX_VALUE_CHARS = 160

_SYSTEM_PROMPT = """You explain changes to Modlix low-code definition documents to the \
person who is about to overwrite their current work with an older version.

You are given an exact, machine-computed list of differences. Do not invent any change \
that is not in that list, and do not omit one that clearly matters.

Answer with a single JSON object and nothing else:

{
  "summary": "One or two sentences: what this older version is, compared with what is live now.",
  "changes": ["short plain-language line", "..."],
  "caution": "What loading this version would undo that the reader probably wants to keep, or \"\" if nothing stands out."
}

Rules for the lines in "changes":
- Write what a builder would say, not what a diff tool says. "The Save button says Submit \
instead of Save" beats "componentDefinition.abc.properties.label.value changed".
- Group repetitive differences. Twenty style leaves under one component is one line about \
that component's styling, not twenty lines.
- Order by how much it matters. Behaviour and data first, wording next, styling last.
- At most 8 lines. Fewer is better if fewer will do.
- Say "would" or "loading this version", because nothing has happened yet.
- No markdown, no bullet characters, no trailing full stops on fragments.
"""


def _friendly_segment(segment: str, current: Dict[str, Any], older: Dict[str, Any]) -> str:
    """Swap a component/event UUID for its human name where one exists.

    Definition documents key their children by UUID, so a raw path is unreadable and gives
    the model nothing to reason with. The name lives on the child itself in both documents;
    prefer the current one, since that is the thing the reader is looking at.
    """
    for doc in (current, older):
        for holder in ("componentDefinition", "eventFunctions"):
            child = (doc.get(holder) or {}).get(segment) if isinstance(doc.get(holder), dict) else None
            if isinstance(child, dict):
                name = child.get("name")
                if name:
                    return str(name)
    return segment


def _readable_path(path: List[str], current: Dict[str, Any], older: Dict[str, Any]) -> str:
    out: List[str] = []
    for segment in path:
        if re.fullmatch(r"[0-9a-fA-F]{16,}", segment):
            out.append(_friendly_segment(segment, current, older))
        else:
            out.append(segment)
    return ".".join(out)


def _brief(value: Any) -> str:
    """A short, printable rendering of any leaf, list or subtree."""
    if value is None:
        return "nothing"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        text = str(value)
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:  # noqa: BLE001 - unserialisable values still deserve a mention
            text = repr(value)
    text = " ".join(text.split())
    if len(text) > _MAX_VALUE_CHARS:
        text = text[:_MAX_VALUE_CHARS] + " ..."
    return text


def _entries(count: int) -> str:
    return "1 entry" if count == 1 else f"{count} entries"


def _walk(
    live: Any,
    old: Any,
    path: List[str],
    out: List[Tuple[List[str], str, Any, Any]],
) -> None:
    """Collect (path, kind, liveValue, oldValue) triples. Kind is added/removed/changed.

    "added" and "removed" are stated relative to the OLDER version, because the reader is
    deciding whether to load it: something the older version lacks would be removed.
    """
    if len(out) >= _MAX_DIFFS:
        return

    if isinstance(live, dict) and isinstance(old, dict):
        for key in sorted(set(live) | set(old)):
            if key in _NOISE_KEYS:
                continue
            if key not in old:
                out.append((path + [key], "removed", live.get(key), None))
            elif key not in live:
                out.append((path + [key], "added", None, old.get(key)))
            else:
                _walk(live[key], old[key], path + [key], out)
        return

    if isinstance(live, list) and isinstance(old, list):
        if len(live) != len(old):
            out.append((path, "changed", _entries(len(live)), _entries(len(old))))
            return
        for index, (a, b) in enumerate(zip(live, old)):
            _walk(a, b, path + [str(index)], out)
        return

    if live != old:
        out.append((path, "changed", live, old))


def compute_diff(current: Dict[str, Any], older: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The exact structural difference, as a flat list of readable entries."""
    raw: List[Tuple[List[str], str, Any, Any]] = []
    _walk(current or {}, older or {}, [], raw)

    return [
        {
            "path": _readable_path(path, current or {}, older or {}),
            "kind": kind,
            "now": _brief(live),
            "was": _brief(old),
        }
        for path, kind, live, old in raw
    ]


def _build_user_message(
    *,
    object_type: str,
    name: str,
    current_version: Any,
    version_number: Any,
    version_message: str,
    diffs: List[Dict[str, Any]],
    truncated: bool,
) -> str:
    lines = [
        f"Object: {object_type or 'definition'} named {name or '(unnamed)'}",
        f"Live now: version {current_version}",
        f"The version being considered: version {version_number}"
        + (f', saved with the note "{version_message}"' if version_message else ", saved with no note"),
        "",
        f"{len(diffs)} differences" + (" (list truncated)" if truncated else "") + ":",
    ]
    for entry in diffs[:_MAX_DIFFS_TO_MODEL]:
        if entry["kind"] == "added":
            lines.append(f"- {entry['path']}: only in the older version, value {entry['was']}")
        elif entry["kind"] == "removed":
            lines.append(f"- {entry['path']}: only in the live version, value {entry['now']}")
        else:
            lines.append(f"- {entry['path']}: live {entry['now']} / older {entry['was']}")
    return "\n".join(lines)


def _extract_json(text: str) -> Dict[str, Any] | None:
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[A-Za-z]*\n?", "", stripped)
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
        return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001 - fall through to a brace scan
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(stripped[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


def _fallback_changes(diffs: List[Dict[str, Any]]) -> List[str]:
    """Something readable when the model is unavailable or returns nonsense.

    Grouping by the first two path segments is crude, but it still tells the reader which
    part of the document moved, which is the question they asked.
    """
    buckets: Dict[str, int] = {}
    for entry in diffs:
        head = ".".join(entry["path"].split(".")[:2]) or "the document"
        buckets[head] = buckets.get(head, 0) + 1
    ordered = sorted(buckets.items(), key=lambda pair: -pair[1])[:8]
    return [f"{count} change{'s' if count > 1 else ''} under {head}" for head, count in ordered]


async def summarise_version_diff(
    *,
    object_type: str = "",
    name: str = "",
    current_version: Any = None,
    version_number: Any = None,
    version_message: str = "",
    current: Dict[str, Any] | None = None,
    older: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Compare two snapshots and describe the older one. Returns a UI-ready dict."""

    diffs = compute_diff(current or {}, older or {})
    truncated = len(diffs) >= _MAX_DIFFS

    if not diffs:
        return {
            "identical": True,
            "summary": f"Version {version_number} is identical to what is live now. Loading it would change nothing.",
            "changes": [],
            "caution": "",
            "diffCount": 0,
            "truncated": False,
        }

    try:
        # The appbuilder's own provider, not the global default: this is the appbuilder
        # explaining its own definitions, and the two are configured separately.
        from app.config import settings

        provider = get_llm_provider(settings.APPBUILDER_PROVIDER)
        result = await provider.create_completion(
            system_prompt=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_message(
                        object_type=object_type,
                        name=name,
                        current_version=current_version,
                        version_number=version_number,
                        version_message=version_message,
                        diffs=diffs,
                        truncated=truncated,
                    ),
                }
            ],
            model_tier="fast",
            max_tokens=2048,
            use_cache=False,
        )
        parsed = _extract_json((result or {}).get("content", "") or "")
    except Exception as exc:  # noqa: BLE001 - a dead model must not hide the diff
        logger.warning("version_diff: model call failed (%s); falling back to the raw diff", exc)
        parsed = None

    if not parsed:
        return {
            "identical": False,
            "summary": f"Version {version_number} differs from what is live now in {len(diffs)} places.",
            "changes": [{"text": line} for line in _fallback_changes(diffs)],
            "caution": "This is the raw difference. The plain-language summary was unavailable.",
            "diffCount": len(diffs),
            "truncated": truncated,
        }

    changes = parsed.get("changes") or []
    if not isinstance(changes, list):
        changes = []

    return {
        "identical": False,
        "summary": parsed.get("summary") or f"Version {version_number} differs in {len(diffs)} places.",
        "changes": [{"text": str(line).strip()} for line in changes if str(line).strip()][:8],
        "caution": str(parsed.get("caution") or "").strip(),
        "diffCount": len(diffs),
        "truncated": truncated,
    }
