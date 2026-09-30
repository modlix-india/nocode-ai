"""What an unrecognised `draft_mode` means on the wire.

The field used to be a bool that defaulted to off, and the caller that sent
nothing wrote live. It is now a three-value scope that defaults to DRAFT, and
ANYTHING it cannot read -- a leftover `true`/`false`, a null, a typo -- also
reads as DRAFT.

That direction is the whole point and is worth pinning. A caller confused about
the spelling gets a change it can look at and publish, which is recoverable. The
other default hands it a live change it never agreed to, in front of real users,
and there is no undo for that. So live has to be asked for by name.

The cost is that a caller can no longer say "live" by sending `false`; it has to
send the string "LIVE". Every caller was updated to send an explicit value for
exactly this reason.
"""

from __future__ import annotations

import pytest

from app.agents.appbuilder.router import ChatRequest
from app.core.tools.draft_registry import DraftScope, to_scope


@pytest.mark.parametrize(
    "value",
    ["LIVE", "live", "Live", " live ", DraftScope.LIVE],
)
def test_live_is_the_one_thing_that_has_to_be_spelled_out(value):
    assert to_scope(value) is DraftScope.LIVE


@pytest.mark.parametrize(
    "value",
    ["DRAFT", "draft", DraftScope.DRAFT, None, True, False, 0, 1, "", "NONSENSE", {}],
)
def test_everything_else_reads_as_draft(value):
    assert to_scope(value) is DraftScope.DRAFT


@pytest.mark.parametrize(
    "value",
    ["PAGE_ONLY_DRAFT", "page_only_draft", DraftScope.PAGE_ONLY_DRAFT],
)
def test_page_only_survives_the_round_trip(value):
    assert to_scope(value) is DraftScope.PAGE_ONLY_DRAFT


# ── The same rule, through the request model ─────────────────────────────────


def test_a_request_that_says_nothing_drafts():
    assert ChatRequest(message="hi").draft_mode is DraftScope.DRAFT


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        ("LIVE", DraftScope.LIVE),
        ("DRAFT", DraftScope.DRAFT),
        ("PAGE_ONLY_DRAFT", DraftScope.PAGE_ONLY_DRAFT),
        (True, DraftScope.DRAFT),
        (False, DraftScope.DRAFT),
        ("typo", DraftScope.DRAFT),
        (None, DraftScope.DRAFT),
    ],
)
def test_the_request_never_rejects_a_scope_it_cannot_read(sent, expected):
    """A 422 here would break the chat outright; falling back to review does not."""
    assert ChatRequest(message="hi", draft_mode=sent).draft_mode is expected


def test_the_session_carries_the_plain_string():
    """session.context is persisted to CONTEXT_JSON, which holds no enums."""
    value = ChatRequest(message="hi", draft_mode="PAGE_ONLY_DRAFT").draft_mode.value
    assert value == "PAGE_ONLY_DRAFT"
    assert isinstance(value, str)
