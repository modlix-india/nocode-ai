"""Tests for app/agents/appbuilder/tools/modlix/_kirun_layout.py."""

from __future__ import annotations

import copy

import pytest

from app.agents.appbuilder.tools.modlix._kirun_layout import (
    auto_layout_definition,
    auto_layout_steps,
)


def _step(deps: list[str] | None = None, **extra) -> dict:
    """Helper to build a step with `dependentStatements` for the given step names."""
    dep_map = {}
    if deps:
        for d in deps:
            dep_map[f"Steps.{d}.output"] = True
    s: dict = {"statementName": extra.get("name", ""), "namespace": "ns", "name": "fn"}
    if dep_map:
        s["dependentStatements"] = dep_map
    return s


def test_empty_steps_returns_zero():
    steps: dict = {}
    assert auto_layout_steps(steps) == 0
    assert steps == {}


def test_non_dict_input_returns_zero():
    # The helper guards against non-dict input gracefully.
    assert auto_layout_steps(None) == 0  # type: ignore[arg-type]
    assert auto_layout_steps([]) == 0  # type: ignore[arg-type]


def test_single_step_gets_position_assigned():
    steps = {"a": _step()}
    count = auto_layout_steps(steps)
    assert count == 1
    assert "position" in steps["a"]
    pos = steps["a"]["position"]
    assert "left" in pos and "top" in pos
    assert isinstance(pos["left"], (int, float))
    assert isinstance(pos["top"], (int, float))


def test_linear_chain_monotonic_x():
    # A -> B -> C
    steps = {
        "a": _step(),
        "b": _step(["a"]),
        "c": _step(["b"]),
    }
    count = auto_layout_steps(steps)
    assert count == 3

    ax = steps["a"]["position"]["left"]
    bx = steps["b"]["position"]["left"]
    cx = steps["c"]["position"]["left"]

    # Layer index drives X — strictly increasing along the chain.
    assert ax < bx < cx


def test_diamond_dependency_layer_ordering():
    # A -> {B, C} -> D
    steps = {
        "a": _step(),
        "b": _step(["a"]),
        "c": _step(["a"]),
        "d": _step(["b", "c"]),
    }
    count = auto_layout_steps(steps)
    assert count == 4

    ax = steps["a"]["position"]["left"]
    bx = steps["b"]["position"]["left"]
    cx = steps["c"]["position"]["left"]
    dx = steps["d"]["position"]["left"]

    # B and C share a layer between A and D.
    assert ax < bx == cx < dx


def test_deterministic_repeated_runs():
    base = {
        "a": _step(),
        "b": _step(["a"]),
        "c": _step(["a"]),
        "d": _step(["b", "c"]),
    }
    first = copy.deepcopy(base)
    second = copy.deepcopy(base)

    n1 = auto_layout_steps(first)
    n2 = auto_layout_steps(second)
    assert n1 == n2 == 4

    for name in first:
        assert first[name]["position"] == second[name]["position"]


def test_dependency_via_parameter_map_expression():
    # No dependentStatements — only an expression reference. Should still
    # treat B as depending on A.
    steps = {
        "a": _step(),
        "b": {
            "namespace": "ns",
            "name": "fn",
            "parameterMap": {
                "input": {
                    "ref1": {"expression": "Steps.a.output + 1"},
                },
            },
        },
    }
    count = auto_layout_steps(steps)
    assert count == 2
    assert steps["a"]["position"]["left"] < steps["b"]["position"]["left"]


def test_self_dependency_ignored():
    # A step referencing itself shouldn't create a cycle or stall the layout.
    steps = {
        "a": {
            "namespace": "ns",
            "name": "fn",
            "dependentStatements": {"Steps.a.output": True},
        },
    }
    count = auto_layout_steps(steps)
    assert count == 1
    assert "position" in steps["a"]


def test_dependency_to_unknown_step_ignored():
    # References to step names not in this function are dropped — no crash,
    # and the step lands in layer 0.
    steps = {
        "a": _step(["ghost"]),
    }
    count = auto_layout_steps(steps)
    assert count == 1
    assert steps["a"]["position"]["left"] == pytest.approx(50.0)


def test_disabled_dependency_via_false_value():
    # dependentStatements entry set to False is skipped, so B has no real
    # dep on A and they share layer 0 (same X).
    steps = {
        "a": _step(),
        "b": {
            "namespace": "ns",
            "name": "fn",
            "dependentStatements": {"Steps.a.output": False},
        },
    }
    count = auto_layout_steps(steps)
    assert count == 2
    assert steps["a"]["position"]["left"] == steps["b"]["position"]["left"]


def test_layer0_packs_vertically_without_overlap():
    # Three independent steps all land in layer 0 with increasing Y.
    steps = {"a": _step(), "b": _step(), "c": _step()}
    auto_layout_steps(steps)
    ys = sorted(steps[n]["position"]["top"] for n in ("a", "b", "c"))
    # Strictly increasing — no two nodes share a Y in layer 0.
    assert ys[0] < ys[1] < ys[2]
    # All share the same X (layer 0).
    xs = {steps[n]["position"]["left"] for n in ("a", "b", "c")}
    assert len(xs) == 1


def test_auto_layout_definition_top_level_steps():
    defn = {
        "steps": {
            "a": _step(),
            "b": _step(["a"]),
        }
    }
    total = auto_layout_definition(defn)
    assert total == 2
    assert "position" in defn["steps"]["a"]
    assert "position" in defn["steps"]["b"]
    assert defn["steps"]["a"]["position"]["left"] < defn["steps"]["b"]["position"]["left"]


def test_auto_layout_definition_event_functions():
    defn = {
        "steps": {"root": _step()},
        "eventFunctions": {
            "onClick": {
                "steps": {
                    "x": _step(),
                    "y": _step(["x"]),
                }
            },
            "onLoad": {
                "steps": {"z": _step()},
            },
        },
    }
    total = auto_layout_definition(defn)
    # 1 (root) + 2 (onClick) + 1 (onLoad) = 4
    assert total == 4
    assert "position" in defn["steps"]["root"]
    assert "position" in defn["eventFunctions"]["onClick"]["steps"]["x"]
    assert "position" in defn["eventFunctions"]["onClick"]["steps"]["y"]
    assert "position" in defn["eventFunctions"]["onLoad"]["steps"]["z"]


def test_auto_layout_definition_non_dict_input():
    assert auto_layout_definition(None) == 0  # type: ignore[arg-type]
    assert auto_layout_definition("nope") == 0  # type: ignore[arg-type]
    assert auto_layout_definition({}) == 0
