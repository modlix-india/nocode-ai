"""Location widget message parsing.

The craft-panel map search widget sends machine-readable messages:

  "add targeting location {"name":"<name>","lat":<lat>,"lng":<lng>,...}"  (JSON payload)
  "delete targeting location index <n>"                                   (1-based)

These are NOT natural language — they carry all the parameters needed to
call modify_targeting_location directly, with no LLM required. Parsing
lives here (geo layer) so the orchestration driver stays unaware of it.
"""

from __future__ import annotations

import json as _json
import re
from typing import Any

_ADD_PREFIXES = ("add targeting location ", "adding location ")
_DELETE_PREFIXES = (
    "remove targeting location ",
    "delete targeting location ",
    "removing location ",
    "deleting location ",
)

_JSON_FIELD_MAP = {
    "name": str,
    "lat": float,
    "lng": float,
    "place_id": str,
    "pincode": str,
    "city": str,
    "state": str,
    "google_id": str,
    "meta_key": str,
}


def parse_location_widget_message(msg: str) -> dict[str, Any] | None:
    """Return parsed params if msg is a location widget message, else None.

    On match returns a dict with 'action' set to "add" or "delete" plus any
    of: name, lat, lng, place_id, city, state, pincode, google_id, meta_key,
    index.  Returns None for natural-language messages that should go through
    the normal agent loop.
    """
    lower = msg.strip().lower()
    if any(lower.startswith(p) for p in _ADD_PREFIXES):
        return {"action": "add", **_parse_params(msg)}
    if any(lower.startswith(p) for p in _DELETE_PREFIXES):
        return {"action": "delete", **_parse_params(msg)}
    return None


def _parse_params(msg: str) -> dict[str, Any]:
    # Primary format: JSON payload  e.g. add targeting location {"name":"...","lat":...}
    json_start = msg.find('{')
    if json_start != -1:
        try:
            payload = _json.loads(msg[json_start:])
            params: dict[str, Any] = {}
            for field, cast in _JSON_FIELD_MAP.items():
                val = payload.get(field)
                if val is not None:
                    try:
                        params[field] = cast(val)
                    except (TypeError, ValueError):
                        pass
            if "index" in payload:
                try:
                    params["index"] = int(payload["index"])
                except (TypeError, ValueError):
                    pass
            return params
        except _json.JSONDecodeError:
            pass

    # Fallback: key=value or "index <n>" formats
    params = {}

    for key in ("name", "place_id", "city", "state", "pincode", "google_id", "meta_key"):
        m = re.search(rf'{key}="([^"]*)"', msg)
        if m:
            params[key] = m.group(1)
        else:
            m = re.search(rf'{key}=(\S+)', msg)
            if m:
                params[key] = m.group(1)

    for key in ("lat", "lng"):
        m = re.search(rf'{key}=([+-]?\d+\.?\d*)', msg)
        if m:
            try:
                params[key] = float(m.group(1))
            except ValueError:
                pass

    # Accept both "index=5" and "index 5" (frontend sends the latter for delete)
    m = re.search(r'index[= ](\d+)', msg)
    if m:
        try:
            params["index"] = int(m.group(1))
        except ValueError:
            pass

    return params
