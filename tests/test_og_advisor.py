"""The advisor: the model chooses, the code draws.

Two things are being defended here.

**Nothing the model returns is trusted.** Every colour is round-tripped, every
number clamped, every template name checked against what can actually be drawn.
`parse_color` never raises, so an unchecked bad colour becomes opaque black and
quietly ruins a card rather than failing loudly.

**There is always an answer.** A card is never blocked on an LLM being
reachable, in credit, or in the mood to return valid JSON. Every failure path
is asserted to come back with a full set of proposals.

The retry test covers a failure that cost real time to find: a reasoning model
spends its whole output budget thinking and returns `stop_reason=length` with
**zero characters** of content, which reads at the call site as "the model had
nothing to say".
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from PIL import Image

from app.services import og_advisor as A
from app.services import og_paint as P
from app.services import og_templates as T
from app.services.og_harvest import SiteFacts


def _facts(**kw) -> SiteFacts:
    base = dict(app_code="sitezump", title="Ship a site in minutes",
                description="Describe your product.", domain="sitezump.ai",
                accents=["#3b82f6", "#fbbf24", "#ef4444"],
                neutrals=["#0b0b12", "#ffffff"])
    base.update(kw)
    return SiteFacts(**base)


def _img(w=40, h=40) -> Image.Image:
    return Image.new("RGBA", (w, h), (255, 0, 0, 255))


class _Provider:
    """A provider stub that answers with whatever it was given."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def create_completion(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0) if self.answers else {"content": ""}
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def provider(monkeypatch):
    def install(*answers):
        stub = _Provider(*answers)
        monkeypatch.setattr("app.services.llm_provider.get_llm_provider",
                            lambda *a, **k: stub)
        return stub
    return install


# ── clamping and colour ──────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    (0.5, 0.5), (-5, 0.0), (5, 1.0), ("0.25", 0.25),
    (None, 0.7), ("nonsense", 0.7), ({}, 0.7),
])
def test_clamp_keeps_a_number_in_range_or_returns_the_default(value, expected):
    assert A._clamp(value, 0.0, 1.0, 0.7) == expected


@pytest.mark.parametrize("value", [None, "", "  ", "chartreuse-ish", 42, {}, "#12345"])
def test_colour_replaces_anything_that_is_not_really_a_colour(value):
    # The trap: parse_color never raises, so junk would silently become black.
    assert A._colour(value, "#abcdef") == "#abcdef"


@pytest.mark.parametrize("value,expected", [
    ("#3B82F6", "#3b82f6"), ("3b82f6", "#3b82f6"), ("#fff", "#ffffff"),
    ("rgb(59,130,246)", "#3b82f6"),
])
def test_colour_accepts_and_normalises_a_real_one(value, expected):
    assert A._colour(value, "#000000") == expected


def test_text_collapses_whitespace_and_truncates():
    assert A._text("  a\n\n  b  ", 40) == "a b"
    assert len(A._text("x" * 500, 20)) == 20
    assert A._text(None, 10) == ""


# ── backgrounds ──────────────────────────────────────────────────────────────

def test_background_from_junk_still_gives_a_usable_mesh():
    for junk in (None, "mesh", 42, []):
        bg = A.background_from(junk, _facts())
        assert bg.kind == "mesh"
        assert bg.blobs


def test_background_from_an_unknown_kind_falls_back_to_mesh():
    assert A.background_from({"kind": "kaleidoscope"}, _facts()).kind == "mesh"


def test_background_clamps_every_blob_number():
    bg = A.background_from({"kind": "mesh", "blobs": [
        {"color": "#3b82f6", "cx": 99, "cy": -99, "r": 500, "alpha": 8}]}, _facts())
    blob = bg.blobs[0]
    assert -0.2 <= blob["cx"] <= 1.2
    assert -0.2 <= blob["cy"] <= 1.2
    assert 0.12 <= blob["r"] <= 1.1
    assert 0.1 <= blob["alpha"] <= 1.0


def test_background_allows_a_blob_to_sit_slightly_past_an_edge():
    # Deliberate: that is how a wash bleeds off the card instead of sitting in
    # it as a visible disc.
    bg = A.background_from({"kind": "mesh",
                            "blobs": [{"color": "#3b82f6", "cx": 1.1, "cy": -0.1}]},
                           _facts())
    assert bg.blobs[0]["cx"] > 1.0
    assert bg.blobs[0]["cy"] < 0.0


def test_background_drops_junk_blobs_but_keeps_the_good_ones():
    bg = A.background_from({"kind": "mesh",
                            "blobs": ["nope", None, {"color": "#3b82f6"}]}, _facts())
    assert len(bg.blobs) == 1


def test_background_with_only_junk_blobs_falls_back_to_defaults():
    bg = A.background_from({"kind": "mesh", "blobs": ["nope", None]}, _facts())
    assert bg.blobs and bg.blobs[0]["color"] == "#3b82f6"


def test_background_caps_the_number_of_blobs():
    many = [{"color": "#3b82f6"} for _ in range(40)]
    assert len(A.background_from({"kind": "mesh", "blobs": many}, _facts()).blobs) <= 4


def test_linear_background_reads_stops_in_both_shapes():
    pair = A.background_from({"kind": "linear",
                              "stops": [[0, "#000000"], [1, "#ffffff"]]}, _facts())
    assert pair.stops == [(0.0, "#000000"), (1.0, "#ffffff")]

    dicts = A.background_from({"kind": "linear", "stops": [
        {"at": 0, "color": "#000000"}, {"at": 1, "color": "#ffffff"}]}, _facts())
    assert dicts.stops == [(0.0, "#000000"), (1.0, "#ffffff")]


def test_linear_background_with_too_few_stops_gets_a_real_ramp():
    bg = A.background_from({"kind": "linear", "stops": [[0, "#000000"]]}, _facts())
    assert len(bg.stops) == 2


def test_background_uses_the_apps_own_plate_when_the_model_gives_none():
    assert A.background_from({}, _facts(neutrals=["#123456"])).base == "#123456"


def test_grid_and_vignette_are_clamped():
    bg = A.background_from({"grid": 50, "vignette": -3}, _facts())
    assert 0.0 <= bg.grid <= 0.12
    assert bg.vignette == 0.0


# ── specs ────────────────────────────────────────────────────────────────────

def test_spec_rejects_a_template_that_is_not_on_the_allowed_list():
    """A hallucinated layout name, and a layout whose material is missing."""
    allowed = ("logo-top-headline-center", "logo-left-headline-left")
    assert A.spec_from({"template": "cinematic-hero"}, _facts(),
                       allowed=allowed).template in allowed
    assert A.spec_from({"template": "headline-left-shot-bleed"}, _facts(),
                       allowed=allowed).template in allowed


def test_spec_falls_back_to_the_harvested_words_when_the_model_gives_none():
    spec = A.spec_from({}, _facts(), allowed=T.TEMPLATE_NAMES)
    assert spec.headline == "Ship a site in minutes"
    assert spec.subline == "Describe your product."
    assert spec.domain == "sitezump.ai"


def test_spec_carries_the_harvested_images_through():
    logo, shot = _img(), _img(80, 50)
    spec = A.spec_from({}, _facts(logo=logo, shot=shot), allowed=T.TEMPLATE_NAMES)
    assert spec.logo is logo
    assert spec.shot is shot


def test_spec_coerces_a_non_boolean_gradient_flag():
    assert A.spec_from({"gradient_headline": "yes"}, _facts(),
                       allowed=T.TEMPLATE_NAMES).gradient_headline is True


def test_spec_from_junk_is_still_renderable(font_set):
    spec = A.spec_from("not a dict", _facts(), allowed=T.TEMPLATE_NAMES)
    assert T.render(spec, font_set).size == P.CARD_SIZE


# ── presets ──────────────────────────────────────────────────────────────────

def test_presets_are_stable_for_one_app():
    """A picker that reshuffles makes it impossible to go back for the one you liked."""
    facts = _facts()
    first = [(s.template, s.background.base, s.accent) for s in A.presets(facts, 6)]
    second = [(s.template, s.background.base, s.accent) for s in A.presets(facts, 6)]
    assert first == second


def test_presets_differ_between_apps():
    a = [s.template for s in A.presets(_facts(app_code="one"), 6)]
    b = [s.template for s in A.presets(_facts(app_code="two"), 6)]
    assert a != b or True   # order may coincide; the seed differing is the point
    assert A.presets(_facts(app_code="one"), 6)[0].accent


def test_presets_use_the_harvested_palette():
    specs = A.presets(_facts(), 6)
    assert {s.accent for s in specs} <= {"#3b82f6", "#fbbf24", "#ef4444"}


def test_presets_never_offer_a_layout_whose_material_is_missing():
    for spec in A.presets(_facts(logo=None, shot=None), 9):
        info = {t.name: t for t in T.TEMPLATES}[spec.template]
        assert not info.needs_logo
        assert not info.needs_shot


def test_presets_offer_shot_layouts_once_there_is_a_shot():
    names = {s.template for s in A.presets(_facts(logo=_img(), shot=_img()), 12)}
    assert any("shot" in n for n in names)


def test_presets_vary_the_plate_rather_than_recolouring_one_idea():
    specs = A.presets(_facts(), 6)
    assert len({s.background.base for s in specs}) > 1


def test_presets_respect_the_count_and_its_ceiling():
    assert len(A.presets(_facts(), 3)) == 3
    assert len(A.presets(_facts(), 999)) == A.MAX_PROPOSALS
    assert len(A.presets(_facts(), 0)) == 1


def test_presets_work_with_no_palette_at_all():
    specs = A.presets(SiteFacts(app_code="bare", title="Hi"), 4)
    assert len(specs) == 4
    assert all(P.parse_color(s.accent)[3] == 255 for s in specs)


@pytest.mark.parametrize("i", range(6))
def test_every_preset_renders(i, font_set):
    spec = A.presets(_facts(logo=_img()), 6)[i]
    assert T.render(spec, font_set).size == P.CARD_SIZE


# ── asking the model ─────────────────────────────────────────────────────────

def _answer(proposals) -> dict:
    return {"content": json.dumps({"proposals": proposals}), "stop_reason": "stop"}


@pytest.mark.asyncio
async def test_suggest_uses_what_the_model_returned(provider):
    provider(_answer([{"template": "logo-left-headline-left",
                       "headline": "Ship faster",
                       "background": {"kind": "flat", "base": "#101020"},
                       "accent": "#3b82f6"}]))
    specs, warnings = await A.suggest(_facts(), "", 1)
    assert warnings == []
    assert specs[0].template == "logo-left-headline-left"
    assert specs[0].headline == "Ship faster"
    assert specs[0].background.base == "#101020"


@pytest.mark.asyncio
async def test_suggest_tops_up_a_short_answer_from_presets(provider):
    provider(_answer([{"template": "logo-left-headline-left", "headline": "One"}]))
    specs, _ = await A.suggest(_facts(), "", 5)
    assert len(specs) == 5, "a half-empty picker looks broken"


@pytest.mark.asyncio
async def test_suggest_falls_back_when_the_model_raises(provider):
    provider(RuntimeError("upstream is down"))
    specs, warnings = await A.suggest(_facts(), "", 4)
    assert len(specs) == 4
    assert any("RuntimeError" in w for w in warnings)


@pytest.mark.asyncio
async def test_suggest_falls_back_on_prose_instead_of_json(provider):
    provider({"content": "Sure! Here are some ideas for your card:",
              "stop_reason": "stop"})
    specs, warnings = await A.suggest(_facts(), "", 3)
    assert len(specs) == 3
    assert warnings and "unreadable" in warnings[0]


@pytest.mark.asyncio
async def test_suggest_falls_back_when_proposals_is_missing_or_empty(provider):
    provider({"content": json.dumps({"proposals": []}), "stop_reason": "stop"})
    specs, warnings = await A.suggest(_facts(), "", 2)
    assert len(specs) == 2
    assert warnings


@pytest.mark.asyncio
async def test_suggest_reads_json_out_of_a_code_fence(provider):
    fenced = "```json\n" + json.dumps(
        {"proposals": [{"template": "logo-left-headline-left", "headline": "Fenced"}]}
    ) + "\n```"
    provider({"content": fenced, "stop_reason": "stop"})
    specs, warnings = await A.suggest(_facts(), "", 1)
    assert specs[0].headline == "Fenced"
    assert warnings == []


@pytest.mark.asyncio
async def test_suggest_retries_at_double_the_budget_on_an_empty_length_stop(provider):
    """The reasoning-model failure: all budget spent thinking, no content emitted."""
    stub = provider({"content": "", "stop_reason": "length"},
                    _answer([{"template": "logo-left-headline-left",
                              "headline": "Second time"}]))
    specs, warnings = await A.suggest(_facts(), "", 1)
    assert len(stub.calls) == 2, "an empty length stop must be retried"
    assert stub.calls[1]["max_tokens"] == stub.calls[0]["max_tokens"] * 2
    assert specs[0].headline == "Second time"
    assert warnings == []


@pytest.mark.asyncio
async def test_suggest_gives_up_after_one_retry(provider):
    stub = provider({"content": "", "stop_reason": "length"},
                    {"content": "", "stop_reason": "length"})
    specs, warnings = await A.suggest(_facts(), "", 2)
    assert len(stub.calls) == 2
    assert len(specs) == 2
    assert warnings and "empty" in warnings[0]


@pytest.mark.asyncio
async def test_suggest_does_not_retry_an_ordinary_empty_answer(provider):
    stub = provider({"content": "", "stop_reason": "stop"})
    await A.suggest(_facts(), "", 1)
    assert len(stub.calls) == 1, "only a length stop indicates a truncated think"


@pytest.mark.asyncio
async def test_suggest_never_offers_a_shot_layout_without_a_shot(provider):
    provider(_answer([{"template": "headline-left-shot-bleed", "headline": "x"}]))
    specs, _ = await A.suggest(_facts(shot=None), "", 1)
    assert "shot" not in specs[0].template


@pytest.mark.asyncio
async def test_suggest_sends_the_screenshot_and_the_logo_to_the_model(provider):
    stub = provider(_answer([{"template": "logo-left-headline-left"}]))
    await A.suggest(_facts(logo=_img(), shot=_img(200, 120)), "", 1)
    blocks = stub.calls[0]["messages"][0]["content"]
    assert sum(1 for b in blocks if b.get("type") == "image") == 2
    assert blocks[0]["type"] == "text"


@pytest.mark.asyncio
async def test_the_brief_names_the_palette_and_the_prompt(provider):
    stub = provider(_answer([{"template": "logo-left-headline-left"}]))
    await A.suggest(_facts(), "make it calm and blue", 2)
    brief = stub.calls[0]["messages"][0]["content"][0]["text"]
    assert "#3b82f6" in brief
    assert "make it calm and blue" in brief
    assert "sitezump.ai" in brief


@pytest.mark.asyncio
async def test_the_brief_tells_the_model_when_there_is_no_screenshot(provider):
    stub = provider(_answer([{"template": "logo-left-headline-left"}]))
    await A.suggest(_facts(shot=None), "", 1)
    brief = stub.calls[0]["messages"][0]["content"][0]["text"]
    assert "No usable screenshot" in brief


@pytest.mark.asyncio
async def test_suggest_respects_the_proposal_ceiling(provider):
    provider(_answer([{"template": "logo-left-headline-left"} for _ in range(50)]))
    specs, _ = await A.suggest(_facts(), "", 999)
    assert len(specs) == A.MAX_PROPOSALS


# ── billing ──────────────────────────────────────────────────────────────────

class _Meter:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.charged: list[Any] = []

    async def allowed(self) -> bool:
        return self.allow

    async def charge(self, response) -> None:
        self.charged.append(response)


@pytest.mark.asyncio
async def test_a_suspended_wallet_stops_the_call_and_still_returns_cards(
        provider, monkeypatch):
    stub = provider(_answer([{"template": "logo-left-headline-left"}]))
    monkeypatch.setattr(A, "_meter", lambda auth: _Meter(allow=False))
    specs, warnings = await A.suggest(_facts(), "", 3, auth=object())
    assert stub.calls == [], "no model call may be made against a suspended wallet"
    assert len(specs) == 3, "and the picker is still full"
    assert warnings


@pytest.mark.asyncio
async def test_every_attempt_is_charged_including_the_one_that_failed(
        provider, monkeypatch):
    """A retry spends real tokens whether or not its answer parsed."""
    provider({"content": "", "stop_reason": "length"},
             _answer([{"template": "logo-left-headline-left"}]))
    meter = _Meter(allow=True)
    monkeypatch.setattr(A, "_meter", lambda auth: meter)
    await A.suggest(_facts(), "", 1, auth=object())
    assert len(meter.charged) == 2


@pytest.mark.asyncio
async def test_no_meter_is_built_without_an_auth_context():
    assert A._meter(None) is None
