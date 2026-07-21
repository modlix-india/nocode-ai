"""Tests for app/agents/appbuilder/tools/modlix/_kirun_dsl.py.

This module is a thin shim around kirun-py's DSLCompiler. We assert:
  - the shim imports cleanly
  - validate_text returns (True, None) / (False, "<msg>")
  - compile_text round-trips a known-good function into the expected dict shape
  - compile_text on invalid input raises (rather than silently emitting garbage)
  - type-field normalization happens (scalar "STRING" becomes ["STRING"])

Deep DSL coverage lives in kirun-py's own test suite. If kirun-py is not
installed, the whole file skips cleanly.
"""

from __future__ import annotations

import pytest

pytest.importorskip("kirun_py", reason="kirun-py not installed in this environment")

from app.agents.appbuilder.tools.modlix._kirun_dsl import (  # noqa: E402
    compile_text,
    validate_text,
)


# A minimal, syntactically-valid DSL function used across tests.
# Real DSL grammar: FUNCTION <name> [NAMESPACE <ns>] [PARAMETERS ...] LOGIC <steps>.
VALID_DSL = """FUNCTION greet
NAMESPACE test
PARAMETERS
    name AS STRING
LOGIC
    out: System.Identity(value = Arguments.name)
"""


def test_validate_accepts_valid_dsl():
    ok, err = validate_text(VALID_DSL)
    assert ok is True
    assert err is None


def test_validate_rejects_empty_with_message():
    ok, err = validate_text("")
    assert ok is False
    assert isinstance(err, str) and err  # non-empty error message


def test_validate_rejects_garbage_with_message():
    ok, err = validate_text("this is not dsl")
    assert ok is False
    assert isinstance(err, str) and err


def test_compile_returns_function_definition_shape():
    out = compile_text(VALID_DSL)

    # Must be a dict carrying the core keys a Kirun function definition needs.
    assert isinstance(out, dict)
    assert out.get("name") == "greet"
    assert out.get("namespace") == "test"

    # Steps and parameters should both be present (dicts keyed by name).
    assert "steps" in out
    assert "parameters" in out
    assert isinstance(out["parameters"], dict)
    assert "name" in out["parameters"]


def test_compile_normalizes_type_to_list_form():
    """The platform stores `type` as a single-element list; the shim normalizes."""
    out = compile_text(VALID_DSL)
    param = out["parameters"]["name"]

    # schema.type should be a list (platform form), not a scalar string.
    schema_type = param["schema"]["type"]
    assert isinstance(schema_type, list)
    assert schema_type == ["STRING"]


def test_compile_raises_on_empty_input():
    with pytest.raises(Exception):
        compile_text("")


def test_compile_raises_on_invalid_input():
    # We don't pin the exception type (kirun-py owns it); we just assert it
    # surfaces rather than silently returning a malformed dict.
    with pytest.raises(Exception):
        compile_text("not a valid dsl program")
