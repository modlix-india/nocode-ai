"""Regression tests for the defects the chitfundb one-shot build exposed.

The rerun produced an app that was 100% inert at runtime for three independent
reasons, none of which any tool or validator caught:

  1. All 22 event props referenced the event function's NAME; the runtime
     resolves by map KEY, so every handler was dead.
  2. No page had properties.onLoadEvent set, so six written `onLoad` functions
     never ran and no list ever fetched.
  3. All 16 dynamic props used {type: EXPRESSION, value: ...}; the runtime reads
     location.expression under EXPRESSION, so every computed value rendered
     blank and registered no listener.

Plus two provisioning/reporting defects:

  4. create_profile read an `arrangement` param it never declared, so every
     profile it made was empty, which made its app-scoped roles unassignable
     (assign_role -> hasAccessToRoles -> 403) and locked every user out.
  5. create_pages reported "Created 0 of 8" while all 8 pages had persisted,
     because the platform's read-back 404s on a freshly created app.
"""
from __future__ import annotations

import pytest

from app.agents.appbuilder.tools.modlix import _conventions as c
from app.agents.appbuilder.tools.modlix import _page_ops as p_ops
from app.agents.appbuilder.tools.modlix.pages import _check_property_value
from app.agents.appbuilder.tools.modlix.security import _build_arrangement


# ── 3. EXPRESSION locations must carry `expression`, not `value` ─────────────

def test_make_expression_prop_uses_expression_key():
    assert c.make_expression_prop("Page.x") == {
        "location": {"type": "EXPRESSION", "expression": "Page.x"}
    }


def test_bare_path_string_becomes_expression_location():
    out = c.coerce_property_value("Page.greeting")
    assert out == {"location": {"type": "EXPRESSION", "expression": "Page.greeting"}}


def test_computed_expression_is_not_emitted_as_a_literal():
    # Previously this failed the anchored bare-path regex and fell through to
    # {"value": "..."}, so the raw expression text rendered on the page.
    expr = "(Page.origInstall ?? 0) - (Page.auctionDividend ?? 0)"
    out = c.coerce_property_value(expr)
    assert out == {"location": {"type": "EXPRESSION", "expression": expr}}


def test_plain_literal_stays_literal():
    assert c.coerce_property_value("Save") == {"value": "Save"}


@pytest.mark.parametrize(
    "bad,expected",
    [
        (
            {"location": {"type": "EXPRESSION", "value": "Page.a"}},
            {"location": {"type": "EXPRESSION", "expression": "Page.a"}},
        ),
        (
            {"location": {"type": "VALUE", "expression": "Page.a"}},
            {"location": {"type": "VALUE", "value": "Page.a"}},
        ),
    ],
)
def test_mismatched_location_key_is_repaired_on_passthrough(bad, expected):
    # The agent hand-built this shape 16 times; repairing beats rejecting.
    assert c.coerce_property_value(bad) == expected


def test_correct_locations_pass_through_untouched():
    good = {"location": {"type": "EXPRESSION", "expression": "Page.a + Page.b"}}
    assert c.coerce_property_value(good) == good


def test_validate_flags_expression_location_holding_value():
    v = _check_property_value("btn", "text", {"location": {"type": "EXPRESSION", "value": "Page.a"}})
    assert len(v) == 1
    assert "renders blank" in v[0]


def test_validate_accepts_correct_expression_location():
    assert _check_property_value(
        "btn", "text", {"location": {"type": "EXPRESSION", "expression": "Page.a"}}
    ) == []


# ── 1. Event props must be resolved to the eventFunctions key ────────────────

def _page_with_event(name: str = "handleLogin", key: str = "abc123") -> dict:
    return {
        "componentDefinition": {
            "root": {"key": "root", "type": "Grid", "children": {}},
        },
        "eventFunctions": {key: {"name": name, "steps": {}}},
    }


def test_event_prop_name_is_resolved_to_key():
    page = _page_with_event()
    props, notes = c.resolve_event_prop_refs(page, {"onClick": "handleLogin"})
    assert props["onClick"] == "abc123"
    assert notes and "handleLogin" in notes[0]


def test_event_prop_already_a_key_is_left_alone():
    page = _page_with_event()
    props, notes = c.resolve_event_prop_refs(page, {"onClick": "abc123"})
    assert props["onClick"] == "abc123"
    assert notes == []


def test_event_prop_resolution_preserves_wrapped_shape():
    page = _page_with_event()
    props, _ = c.resolve_event_prop_refs(page, {"onClick": {"value": "handleLogin"}})
    assert props["onClick"] == {"value": "abc123"}


def test_unknown_event_name_is_left_for_the_validator():
    page = _page_with_event()
    props, notes = c.resolve_event_prop_refs(page, {"onClick": "nope"})
    assert props["onClick"] == "nope"
    assert notes == []


def test_add_component_resolves_event_name_to_key():
    page = _page_with_event()
    err = p_ops.add_component(
        page, parent_key="root", component_key="btn", component_type="Button",
        properties={"label": "Sign In", "onClick": "handleLogin"},
    )
    assert err is None
    assert page["componentDefinition"]["btn"]["properties"]["onClick"] == {"value": "abc123"}


# ── 4. create_profile must render role_ids into an arrangement ───────────────

def test_role_ids_render_into_arrangement():
    arr = _build_arrangement({"role_ids": [295, 296]})
    assert sorted(v["roleId"] for v in arr.values()) == [295, 296]
    assert all(v["assignable"] is True for v in arr.values())


def test_role_ids_accepts_strings_and_a_scalar():
    assert list(_build_arrangement({"role_ids": ["295"]}).values())[0]["roleId"] == 295
    assert list(_build_arrangement({"role_ids": 295}).values())[0]["roleId"] == 295


def test_explicit_arrangement_wins_over_role_ids():
    explicit = {"g1": {"roleId": 1, "assignable": True}}
    assert _build_arrangement({"arrangement": explicit, "role_ids": [295]}) == explicit


def test_no_roles_yields_empty_arrangement():
    # Still allowed, but create_profile warns loudly: such a profile is inert.
    assert _build_arrangement({}) == {}


def test_create_profile_declares_the_params_it_reads():
    from app.agents.appbuilder.tools.modlix.security import create_profile_tool

    declared = {p.name for p in create_profile_tool.parameters}
    # These were read by _build_profile_body but never declared, so the
    # unknown-parameter check actively rejected them.
    assert {"role_ids", "arrangement", "client_id"} <= declared


# ── 1 + 2. validate_page must catch dead handlers and unset onLoadEvent ──────

def _validate(page: dict, monkeypatch) -> str:
    """Run validate_page against a stubbed page fetch; return the error text."""
    import asyncio

    from app.agents.appbuilder.tools.modlix import pages as pages_mod

    async def _fake_fetch(client, page_name, app_code, headers):
        return page, None

    monkeypatch.setattr(pages_mod.p_ops, "fetch_page_by_name", _fake_fetch)
    monkeypatch.setattr(
        pages_mod, "_client_and_headers", lambda ctx: (object(), {}),
    )
    res = asyncio.run(
        pages_mod._execute_validate_page({"name": "p", "app_code": "a"}, {"app_code": "a"})
    )
    return "" if res.success else (res.error or "")


def _wired_page(on_click: str, *, on_load: str | None = None) -> dict:
    page = {
        "rootComponent": "root",
        "componentDefinition": {
            "root": {"key": "root", "type": "Grid", "children": {"btn": True}},
            "btn": {
                "key": "btn", "type": "Button", "children": {},
                "properties": {"onClick": {"value": on_click}},
            },
        },
        "eventFunctions": {"k1": {"name": "handleLogin", "steps": {}}},
        "properties": {},
    }
    if on_load is not None:
        page["properties"]["onLoadEvent"] = on_load
    return page


def test_validate_rejects_event_prop_holding_a_name(monkeypatch):
    err = _validate(_wired_page("handleLogin"), monkeypatch)
    assert "is the event function's NAME" in err
    assert "'k1'" in err  # tells the agent the key to use


def test_validate_accepts_event_prop_holding_the_key(monkeypatch):
    assert _validate(_wired_page("k1"), monkeypatch) == ""


def test_validate_flags_unset_onload_event(monkeypatch):
    page = _wired_page("k1")
    page["eventFunctions"]["k2"] = {"name": "onLoad", "steps": {}}
    err = _validate(page, monkeypatch)
    assert "onLoadEvent is not set" in err
    assert "'k2'" in err


def test_validate_flags_onload_event_holding_a_name(monkeypatch):
    page = _wired_page("k1", on_load="onLoad")
    page["eventFunctions"]["k2"] = {"name": "onLoad", "steps": {}}
    err = _validate(page, monkeypatch)
    assert "onLoadEvent" in err and "'k2'" in err


def test_validate_accepts_onload_event_holding_the_key(monkeypatch):
    page = _wired_page("k1", on_load="k2")
    page["eventFunctions"]["k2"] = {"name": "onLoad", "steps": {}}
    assert _validate(page, monkeypatch) == ""


# ── Reachability: created-but-never-wired, in both directions ────────────────

def test_unreferenced_event_function_is_a_violation(monkeypatch):
    # The direction with no other signal: a dangling function looks exactly
    # like a finished one.
    page = _wired_page("k1")
    page["eventFunctions"]["k9"] = {"name": "saveGroup", "steps": {}}
    err = _validate(page, monkeypatch)
    assert "nothing references this function" in err
    assert "saveGroup" in err


def test_event_function_referenced_by_name_is_not_also_called_unreachable(monkeypatch):
    # It must be reported once, as a name/key error, not twice.
    err = _validate(_wired_page("handleLogin"), monkeypatch)
    assert "is the event function's NAME" in err
    assert "nothing references this function" not in err


def test_event_function_reached_via_onload_is_not_flagged(monkeypatch):
    page = _wired_page("k1", on_load="k2")
    page["eventFunctions"]["k2"] = {"name": "onLoad", "steps": {}}
    assert _validate(page, monkeypatch) == ""


def test_input_component_without_binding_is_a_violation(monkeypatch):
    page = _wired_page("k1")
    page["componentDefinition"]["root"]["children"]["email"] = True
    page["componentDefinition"]["email"] = {"key": "email", "type": "TextBox", "children": {}}
    err = _validate(page, monkeypatch)
    assert "no bindingPath" in err and "TextBox" in err


def test_input_component_with_binding_is_fine(monkeypatch):
    page = _wired_page("k1")
    page["componentDefinition"]["root"]["children"]["email"] = True
    page["componentDefinition"]["email"] = {
        "key": "email", "type": "TextBox", "children": {},
        "bindingPath": {"type": "VALUE", "value": "Page.email"},
    }
    assert _validate(page, monkeypatch) == ""


def test_layout_components_are_never_warned_about(monkeypatch):
    # Grid/Text/Icon are 0% bound in real apps; warning here would train the
    # model to ignore the whole channel.
    page = _wired_page("k1")
    for k, t in (("g", "Grid"), ("t", "Text"), ("i", "Icon")):
        page["componentDefinition"]["root"]["children"][k] = True
        page["componentDefinition"][k] = {"key": k, "type": t, "children": {}}
    assert _validate(page, monkeypatch) == ""


def test_button_without_handler_warns_but_does_not_fail(monkeypatch):
    import asyncio

    from app.agents.appbuilder.tools.modlix import pages as pages_mod

    page = _wired_page("k1")
    page["componentDefinition"]["root"]["children"]["dead"] = True
    page["componentDefinition"]["dead"] = {"key": "dead", "type": "Button", "children": {}}

    async def _fake_fetch(client, page_name, app_code, headers):
        return page, None

    monkeypatch.setattr(pages_mod.p_ops, "fetch_page_by_name", _fake_fetch)
    monkeypatch.setattr(pages_mod, "_client_and_headers", lambda ctx: (object(), {}))
    res = asyncio.run(
        pages_mod._execute_validate_page({"name": "p", "app_code": "a"}, {"app_code": "a"})
    )
    assert res.success is True
    assert "clicking it does nothing" in (res.summary or "")


# ── 6. Per-field expressions inside an object literal never evaluate ─────────

def test_inline_expression_markers_are_detected():
    from app.agents.appbuilder.tools.modlix.kirun_events import (
        _find_unresolved_inline_expressions,
    )

    steps = {
        "save": {
            "parameterMap": {
                "payload": {
                    "uuid1": {
                        "type": ["VALUE"],
                        "value": {
                            "name": {"isExpression": True, "value": "Page.newGroupName"},
                            "amount": {"isExpression": True, "value": "Page.newInstallment"},
                        },
                    }
                }
            }
        }
    }
    hits = _find_unresolved_inline_expressions(steps)
    assert len(hits) == 2
    assert any("Page.newGroupName" in h for h in hits)


def test_single_expression_payload_is_accepted():
    from app.agents.appbuilder.tools.modlix.kirun_events import (
        _find_unresolved_inline_expressions,
    )

    # The shape every working app uses: one EXPRESSION entry, one path.
    steps = {
        "save": {
            "parameterMap": {
                "payload": {
                    "uuid1": {"type": "EXPRESSION", "expression": "Page.newGroup", "order": 1}
                }
            }
        }
    }
    assert _find_unresolved_inline_expressions(steps) == []


def test_inline_expression_error_names_the_working_pattern():
    from app.agents.appbuilder.tools.modlix.kirun_events import _inline_expression_error

    msg = _inline_expression_error(["save.payload.name = 'Page.x'"])
    assert "Nothing was saved" in msg
    assert "CoreServices.Storage.Create" in msg


# ── 7. Nothing may navigate to the configured login page ────────────────────

def _nav_page(link: str) -> dict:
    return {
        "eventFunctions": {
            "k1": {
                "name": "handleLogout",
                "steps": {
                    "logout": {"namespace": "UIEngine", "name": "Logout", "parameterMap": {}},
                    "nav": {
                        "namespace": "UIEngine", "name": "Navigate",
                        "parameterMap": {"linkPath": {"u1": {"type": ["VALUE"], "value": link}}},
                    },
                },
            }
        }
    }


class _AppClient:
    """Minimal stub returning one app doc with a configured loginPage."""

    def __init__(self, login_page: str = "login"):
        self._login_page = login_page

    async def get(self, url, headers=None, params=None):
        class R:
            success = True
            data = {
                "content": [
                    {"appCode": "chitfundb", "properties": {"loginPage": self._login_page}}
                ]
            }
        return R()


def _login_nav_violations(link: str) -> list:
    import asyncio

    from app.agents.appbuilder.tools.modlix.pages import _check_login_page_navigation

    return asyncio.run(
        _check_login_page_navigation(_AppClient(), {}, "chitfundb", _nav_page(link))
    )


def test_navigating_to_login_page_is_rejected():
    v = _login_nav_violations("/login")
    assert len(v) == 1
    assert "configured loginPage" in v[0]


def test_navigating_to_a_normal_page_is_fine():
    assert _login_nav_violations("/home") == []


def test_login_page_check_matches_unprefixed_and_page_prefixed_forms():
    assert len(_login_nav_violations("login")) == 1
    assert len(_login_nav_violations("/page/login")) == 1


# ── 8. Pages must reach data through KIRun, with relative URLs ──────────────

def _fetch_page(url: str, fname: str = "FetchData") -> dict:
    return {
        "eventFunctions": {
            "k1": {
                "name": "onLoad",
                "steps": {
                    "load": {
                        "namespace": "UIEngine", "name": fname,
                        "parameterMap": {"url": {"u1": {"type": ["VALUE"], "value": url}}},
                    }
                },
            }
        }
    }


def _data_violations(url: str, fname: str = "FetchData") -> list:
    from app.agents.appbuilder.tools.modlix.pages import _check_data_access_urls

    return _check_data_access_urls(_fetch_page(url, fname))


def test_raw_data_api_from_a_page_is_rejected():
    v = _data_violations("/api/core/data/member")
    assert len(v) == 1
    assert "BLOCK used as a STEP" in v[0]
    assert 'storageName = "member"' in v[0]


def test_posting_to_a_coreservices_primitive_is_rejected():
    # Storage primitives are KIRun function BLOCKS for use as steps inside a
    # server function, not an endpoint a page may POST to.
    v = _data_violations("api/core/function/execute/CoreServices.Storage/ReadPage")
    assert len(v) == 1
    assert "BLOCKS you call as a STEP" in v[0]


def test_raw_data_api_rejected_even_when_relative():
    assert len(_data_violations("api/core/data/member?page=0")) == 1


def test_absolute_service_url_is_rejected_for_escaping_the_app_path():
    v = _data_violations("/api/security/users")
    assert len(v) == 1
    assert "escapes the app path" in v[0]
    assert "'api/security/users'" in v[0]


def test_app_server_function_over_http_is_also_rejected():
    # Even your own server function is a block to call as a step, not an endpoint.
    v = _data_violations("api/core/function/execute/chitfundb/listMembers")
    assert len(v) == 1
    assert "BLOCKS you call as a STEP" in v[0]


def test_non_data_service_url_is_still_accepted():
    assert _data_violations("api/security/users/query", "SendData") == []


def test_generate_event_expression_markers_are_not_flagged():
    """System.GenerateEvent declares Parameter.EXPRESSION, so the marker shape is
    correct there. Every real server function returns its result that way; a
    guard that flagged it would reject valid code."""
    from app.agents.appbuilder.tools.modlix.kirun_events import (
        _find_unresolved_inline_expressions,
    )

    steps = {
        "generateEvent": {
            "namespace": "System", "name": "GenerateEvent",
            "parameterMap": {
                "results": {
                    "u1": {
                        "type": ["VALUE"],
                        "value": {
                            "name": "result",
                            "value": {"isExpression": True, "value": "Steps.readPage.output.result"},
                        },
                    }
                }
            },
        }
    }
    assert _find_unresolved_inline_expressions(steps) == []


def test_send_data_markers_still_flagged_alongside_generate_event():
    from app.agents.appbuilder.tools.modlix.kirun_events import (
        _find_unresolved_inline_expressions,
    )

    steps = {
        "generateEvent": {
            "namespace": "System", "name": "GenerateEvent",
            "parameterMap": {"results": {"u1": {"value": {"value": {"isExpression": True, "value": "Steps.a.output"}}}}},
        },
        "save": {
            "namespace": "UIEngine", "name": "SendData",
            "parameterMap": {"payload": {"u2": {"value": {"name": {"isExpression": True, "value": "Page.x"}}}}},
        },
    }
    hits = _find_unresolved_inline_expressions(steps)
    assert len(hits) == 1
    assert hits[0].startswith("save")


# ── 9. A byte-identical repeat of a rejected call is refused ─────────────────

def _agent_cls():
    from app.core.agent import BaseAgent

    return BaseAgent


def test_repeat_of_a_rejected_call_is_refused():
    A = _agent_cls()
    ctx: dict = {}
    A._remember_failed_call(ctx, "get_page", {"name": "replace_page_definition"})
    res = A._reject_repeat_of_failed_call(ctx, "get_page", {"name": "replace_page_definition"})
    assert res is not None
    assert res.success is False
    assert "already rejected this" in (res.error or "")


def test_key_order_does_not_defeat_the_guard():
    A = _agent_cls()
    ctx: dict = {}
    A._remember_failed_call(ctx, "t", {"a": 1, "b": 2})
    assert A._reject_repeat_of_failed_call(ctx, "t", {"b": 2, "a": 1}) is not None


def test_different_arguments_are_allowed_through():
    A = _agent_cls()
    ctx: dict = {}
    A._remember_failed_call(ctx, "get_page", {"name": "a"})
    assert A._reject_repeat_of_failed_call(ctx, "get_page", {"name": "b"}) is None


def test_same_arguments_to_a_different_tool_are_allowed():
    A = _agent_cls()
    ctx: dict = {}
    A._remember_failed_call(ctx, "get_page", {"name": "x"})
    assert A._reject_repeat_of_failed_call(ctx, "get_storage", {"name": "x"}) is None


def test_a_first_time_call_is_never_blocked():
    A = _agent_cls()
    assert A._reject_repeat_of_failed_call({}, "anything", {"p": 1}) is None


def test_unserializable_args_do_not_crash_the_guard():
    A = _agent_cls()
    ctx: dict = {}
    A._remember_failed_call(ctx, "t", {"x": object()})
    # default=str makes it serializable, so this is a repeat; the point is no raise.
    A._reject_repeat_of_failed_call(ctx, "t", {"x": object()})


# ── 10. A literal that is really an expression is caught ────────────────────

def test_literal_containing_a_modlix_path_is_rejected():
    # Rendered the expression SOURCE on the dashboard: "Page.membersCount -
    # Page.paidThisMonth" appeared as the Pending Dues figure.
    v = _check_property_value(
        "penValue", "text", {"value": "Page.membersCount - Page.paidThisMonth"}
    )
    assert len(v) == 1
    assert "render as text rather than evaluate" in v[0]


def test_literal_visibility_expression_is_rejected():
    # Worse as a visibility: a non-empty string is truthy, so both the PAID and
    # PENDING badges showed at once and Collect never hid.
    v = _check_property_value("paidBadge", "visibility", {"value": 'Parent.status = "PAID"'})
    assert len(v) == 1
    assert "always truthy" in v[0]


def test_ordinary_literal_text_is_untouched():
    assert _check_property_value("t", "text", {"value": "Record Auction"}) == []
    assert _check_property_value("t", "text", {"value": "Rs. 5000 due"}) == []


def test_literal_with_a_location_override_is_allowed():
    ok = {"value": "fallback", "location": {"type": "EXPRESSION", "expression": "Page.x"}}
    assert _check_property_value("t", "text", ok) == []
