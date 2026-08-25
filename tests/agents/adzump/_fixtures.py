"""Shared test fixtures for Adzump agent tests.

ONE place to build the scaffolding every test needs - a session stand-in, the
`set_campaign_spec` context pair, a `CampaignContext`, and a fake event stream -
so test files stop re-rolling their own `types.SimpleNamespace` + `RE`/`SAAS`
constants + `_ctx`/`_session` helpers (the duplication that made the suite read
as "generated on the fly").

NOT a test module (no `test_` prefix → unittest discovery skips it). Import it:

    from tests.agents.adzump._fixtures import make_session, spec_context, RE

Conventions: tests/README.md.
"""
from __future__ import annotations

import types
from typing import Any

from app.agents.adzump.agent import CampaignContext

# Canonical product_data shapes. Override fields per test:
#   make_session(product={**RE, "product_name": "Foo"})
RE: dict[str, Any] = {
    "business_type": "real estate",
    "product_name": "Skyline Villas",
    "summary": "Luxury 3 & 4 BHK apartments.",
}
SAAS: dict[str, Any] = {"business_type": "saas", "product_name": "Acme"}


def make_session(
    *,
    last_user: str = "",
    spec: dict | None = None,
    product: dict | None = None,
    pending_elicitation: dict | None = None,
    turn: int = 1,
    **context: Any,
) -> types.SimpleNamespace:
    """A BaseSession-shaped stand-in: ``.context`` / ``.messages`` / ``._turn_count``.

    `spec`/`product`/`pending_elicitation` are shortcuts into `.context`; any
    extra kwargs are merged into `.context` too (e.g. `_captured_this_turn=...`,
    `_pending_location_confirm=True`).
    """
    ctx: dict[str, Any] = {
        "campaign_spec": dict(spec or {}),
        "_spec_set_at": {},
        "product_data": dict(product if product is not None else RE),
    }
    if pending_elicitation is not None:
        ctx["_pending_elicitation"] = pending_elicitation
    ctx.update(context)
    s = types.SimpleNamespace()
    s.context = ctx
    s.messages = [{"role": "user", "content": last_user}]
    s._turn_count = turn
    return s


def spec_context(
    spec: dict | None = None,
    last_user: str = "",
    *,
    product: dict | None = None,
    turn: int = 7,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The ``(context, session_ctx)`` pair `_set_campaign_spec` expects.

    `context` is ``{"session_context": <ctx>, "_session": <session>}``; the second
    return is the same `session_ctx` dict for post-call assertions on stored spec.
    """
    session = make_session(last_user=last_user, spec=spec, product=product, turn=turn)
    return {"session_context": session.context, "_session": session}, session.context


def make_cctx(
    spec: dict,
    *,
    product: dict | None = None,
    last_user: str = "",
    account_names: dict | None = None,
    competitor_names: list | None = None,
    attempted: bool = False,
    ig_offered: bool = False,
    awaiting: str | None = None,
    turn: int = 1,
    build_done: bool = False,
    build_gaps: tuple[str, ...] = (),
    review_items: tuple[str, ...] = (),
    summary_confirmed: bool = False,
    awaiting_tool: str | None = None,
) -> CampaignContext:
    """A `CampaignContext` for `_next_action` / prescription tests."""
    return CampaignContext(
        product=dict(product if product is not None else RE),
        product_profile={},
        competitor_names=competitor_names or [],
        competitor_analysis_attempted=attempted,
        spec=dict(spec),
        account_names=account_names or {},
        set_at={},
        current_turn=turn,
        last_user=last_user,
        pending_location=None,
        ig_offered=ig_offered,
        awaiting_custom_field=awaiting,
        build_done=build_done,
        build_gaps=build_gaps,
        review_items=review_items,
        summary_confirmed=summary_confirmed,
        awaiting_tool=awaiting_tool,
    )


def elicitation(field: str, answers: dict | None = None, *, expects: str = "single",
                tool: str = "present_options", **extra: Any) -> dict[str, Any]:
    """A `_pending_elicitation` dict (tagged-capture tests)."""
    return {"tool": tool, "expects": expects, "field": field,
            "answers": answers or {"No": "true"}, **extra}


class FakeStream:
    """Captures `emit_text` calls - the happy path most tool tests need."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    async def emit_text(self, text: str) -> None:
        self.texts.append(text)
