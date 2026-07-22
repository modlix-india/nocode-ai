"""Wrapper around kirun-py's DSLCompiler with normalization for the platform.

Ported verbatim from modlix-mcp/modlix_mcp/kirun_dsl.py — no internal deps
to rewrite, so this is a direct copy.

The platform stores schema `type` fields as single-element lists (`["INTEGER"]`)
while kirun-py's compiler emits/expects scalars (`"INTEGER"`). We normalize on
the round trip so agents can author functions as readable text and round-trip
through the platform without corruption.

Public API:
    compile_text(text) -> dict       — DSL text → FunctionDefinition (platform form)
    decompile_json(defn) -> str      — FunctionDefinition (platform form) → DSL text
    validate_text(text) -> (bool, str|None)
    format_text(text) -> str         — pretty-print round trip
"""

from __future__ import annotations

import copy
from typing import Any

from kirun_py.dsl.dsl_compiler import DSLCompiler


_TYPE_KEYS = ("type",)  # the field we round-trip


def _to_list_types(obj: Any) -> Any:
    """Recursive: replace every scalar `type: "X"` with `type: ["X"]`."""
    if isinstance(obj, dict):
        for k in _TYPE_KEYS:
            v = obj.get(k)
            if isinstance(v, str):
                obj[k] = [v]
        for v in obj.values():
            _to_list_types(v)
    elif isinstance(obj, list):
        for v in obj:
            _to_list_types(v)
    return obj


def _to_scalar_types(obj: Any) -> Any:
    """Recursive: replace every `type: ["X"]` (one entry) with `type: "X"`."""
    if isinstance(obj, dict):
        for k in _TYPE_KEYS:
            v = obj.get(k)
            if isinstance(v, list) and len(v) == 1 and isinstance(v[0], str):
                obj[k] = v[0]
        for v in obj.values():
            _to_scalar_types(v)
    elif isinstance(obj, list):
        for v in obj:
            _to_scalar_types(v)
    return obj


def compile_text(text: str) -> dict[str, Any]:
    """Compile DSL text into the platform's FunctionDefinition shape (list-form types)."""
    raw = DSLCompiler.compile(text)
    if not isinstance(raw, dict):
        raise ValueError(f"Compiler returned non-dict: {type(raw).__name__}")
    return _to_list_types(copy.deepcopy(raw))


async def decompile_json(definition: dict[str, Any]) -> str:
    """Decompile a platform FunctionDefinition into DSL text.

    Accepts list-form `type` fields and normalizes to scalar before handing
    to the underlying decompiler (which currently crashes on list-form).

    NOTE: this is `async` because DSLCompiler.decompile is itself a coroutine.
    Sync callers (scripts/CLI) should wrap with `asyncio.run(decompile_json(...))`.
    """
    normalized = _to_scalar_types(copy.deepcopy(definition))
    return await DSLCompiler.decompile(normalized)


def validate_text(text: str) -> tuple[bool, str | None]:
    """Syntax check the DSL. Returns (ok, error_or_None)."""
    result = DSLCompiler.validate(text)
    if result.valid:
        return True, None
    err_msgs = []
    for e in result.errors:
        loc = ""
        if getattr(e, "line", None) is not None:
            loc = f" (line {e.line}, col {e.column})"
        err_msgs.append(f"{e.message}{loc}")
    return False, "; ".join(err_msgs)


async def format_text(text: str) -> str:
    """Pretty-print: compile then decompile to get canonical formatting."""
    json_obj = compile_text(text)
    return await decompile_json(json_obj)
