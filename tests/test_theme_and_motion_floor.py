"""The floors under a generated theme, style and screenshot.

Each test here corresponds to a defect seen on a real generated site on
2026-09-16: an inert theme, a mint-green dropdown, white seams between sections,
no animation anywhere, and a draft session screenshotting the live page.
"""

import pytest

from app.agents.appbuilder.tools.modlix._motion_floor import (
    MOTION_MARKER,
    needs_motion,
    with_motion_floor,
)
from app.agents.appbuilder.tools.modlix._font_floor import (
    FONT_SLOTS,
    PAIRINGS,
    apply_font_floor,
    font_packs_for,
    pairing_from_families,
)
from app.agents.appbuilder.tools.modlix._theme_floor import (
    APP_THEME_VARIABLES,
    _mix_toward_white,
    apply_theme_floor,
)


class TestThemeFloorNames:
    def test_invented_names_are_renamed_to_real_ones(self):
        """The exact theme a real run produced. Every name in it was inert."""
        out, notes = apply_theme_floor(
            {"ALL": {"primaryColor": "#00D9FF", "secondaryColor": "#7B61FF"}}
        )
        assert out["ALL"]["colorOne"] == "#00D9FF"
        assert out["ALL"]["colorTwo"] == "#7B61FF"
        assert "primaryColor" not in out["ALL"]
        assert any("primaryColor" in n for n in notes)

    def test_real_names_pass_through_untouched(self):
        out, _ = apply_theme_floor({"ALL": {"colorOne": "#123456"}})
        assert out["ALL"]["colorOne"] == "#123456"

    def test_unknown_name_is_kept_but_flagged(self):
        """Arbitrary names are legal (Theme. reads them); the agent is told."""
        out, notes = apply_theme_floor({"ALL": {"myOwnToken": "12px"}})
        assert out["ALL"]["myOwnToken"] == "12px"
        assert any("myOwnToken" in n for n in notes)

    def test_component_variables_are_not_flagged(self):
        """Component names carry placeholders and are validated server-side."""
        name = "dropdownBackgroundHover<designType><colorScheme>"
        _, notes = apply_theme_floor({"ALL": {name: "#fff"}})
        assert not any(name in n for n in notes)

    def test_alias_table_points_only_at_real_variables(self):
        from app.agents.appbuilder.tools.modlix._theme_floor import KNOWN_ALIASES

        for target in KNOWN_ALIASES.values():
            assert target in APP_THEME_VARIABLES, target


class TestThemeFloorFill:
    def test_hover_wash_is_derived_from_the_chosen_primary(self):
        out, _ = apply_theme_floor({"ALL": {"colorOne": "#00D9FF"}})
        assert out["ALL"]["backgroundHoverColorOne"] == "#73EAFF"

    def test_hover_wash_is_not_overwritten_when_set(self):
        out, _ = apply_theme_floor(
            {"ALL": {"colorOne": "#00D9FF", "backgroundHoverColorOne": "#ABCDEF"}}
        )
        assert out["ALL"]["backgroundHoverColorOne"] == "#ABCDEF"

    @pytest.mark.parametrize(
        "source,stock",
        [
            ("#52BD94", "#A8DEC9"),
            ("#08705C", "#81B7AB"),
            ("#FFC728", "#FFE193"),
            ("#EC6B5F", "#F3B4AE"),
            ("#4D7FEE", "#A5BDF6"),
        ],
    )
    def test_derivation_reproduces_platform_stock_values(self, source, stock):
        """The 45%-toward-white rule is the platform's own, not a guess.

        Every stock backgroundHoverColorX is its colorX lightened by that much,
        so a derived theme stays consistent with a hand-built one.
        """
        got = _mix_toward_white(source, 0.45)
        deltas = [abs(int(got[i:i + 2], 16) - int(stock[i:i + 2], 16)) for i in (1, 3, 5)]
        assert max(deltas) <= 10

    def test_gap_is_zeroed_so_sections_do_not_show_seams(self):
        out, notes = apply_theme_floor({"ALL": {"colorOne": "#000000"}})
        assert out["ALL"]["gapBetween"] == "0px"
        assert any("gapBetween" in n for n in notes)

    def test_explicit_gap_is_respected(self):
        out, _ = apply_theme_floor({"ALL": {"gapBetween": "16px"}})
        assert out["ALL"]["gapBetween"] == "16px"

    def test_update_mode_renames_but_adds_nothing(self):
        """update_theme replaces the map wholesale, so filling would resurrect
        variables the caller deliberately dropped."""
        out, _ = apply_theme_floor(
            {"ALL": {"primaryColor": "#00D9FF"}}, fill_gaps=False
        )
        assert out["ALL"] == {"colorOne": "#00D9FF"}

    def test_non_hex_colour_is_left_alone(self):
        out, _ = apply_theme_floor({"ALL": {"colorOne": "var(--x)"}})
        assert "backgroundHoverColorOne" not in out["ALL"]

    def test_garbage_input_does_not_raise(self):
        assert apply_theme_floor(None) == ({}, [])
        assert apply_theme_floor({"ALL": "not-a-dict"})[0]["ALL"] == "not-a-dict"


class TestMotionFloor:
    def test_added_when_stylesheet_says_nothing_about_motion(self):
        css, added = with_motion_floor("@import url('https://fonts.example/x');")
        assert added and MOTION_MARKER in css

    def test_authors_own_motion_is_left_alone(self):
        original = ".x { transition: opacity 1s; }"
        css, added = with_motion_floor(original)
        assert not added and css == original

    def test_is_idempotent(self):
        once, _ = with_motion_floor("")
        twice, added = with_motion_floor(once)
        assert not added and twice == once

    def test_honours_reduced_motion(self):
        css, _ = with_motion_floor("")
        assert "prefers-reduced-motion" in css

    def test_never_animates_geometry(self):
        """The appbuilderstyle bug: transitioning width/height/padding on every
        element makes the page slosh when a scrollbar appears."""
        css, _ = with_motion_floor("")
        transitions = [
            ln for ln in css.splitlines()
            if "transition:" in ln or ln.strip().startswith(("width", "height", "padding"))
        ]
        joined = " ".join(transitions)
        for prop in ("width", "height", "padding", "all"):
            assert f"transition: {prop}" not in joined
            assert f" {prop} " not in joined.replace("transition:", "")

    def test_does_not_use_the_universal_selector_for_transitions(self):
        css, _ = with_motion_floor("")
        before_media = css.split("@media")[0]
        assert "* {" not in before_media

    def test_needs_motion_on_empty_input(self):
        assert needs_motion("") and needs_motion(None)


class TestFontFloor:
    """Every generated site rendered in the stock face: no pack, no tokens."""

    def test_silent_theme_gets_a_real_pairing_and_a_pack(self):
        v, packs, notes = apply_font_floor({"ALL": {"colorOne": "#C75B39"}})
        assert v["ALL"]["bodyFont"].startswith("16px/24px")
        assert packs, "a font nobody downloads is not a font"
        assert "fonts.googleapis.com/css2" in next(iter(packs.values()))["code"]
        assert notes

    def test_every_token_carries_a_size_because_font_is_a_shorthand(self):
        # A family-only value is invalid CSS shorthand and the whole
        # declaration is dropped -- the quiet way a theme stays on the default.
        v, _, _ = apply_font_floor({"ALL": {}})
        for slot in ("bodyFont", *FONT_SLOTS):
            assert "/" in v["ALL"][slot].split()[0], f"{slot} has no size"

    def test_headings_and_body_use_different_faces(self):
        v, _, _ = apply_font_floor({"ALL": {}}, pairing=PAIRINGS["editorial"])
        assert "Fraunces" in v["ALL"]["primaryFont"]
        # Small text stays on the body face or every button becomes a serif.
        assert "Inter" in v["ALL"]["quinaryFont"]

    def test_explicit_fonts_are_never_overwritten(self):
        mine = "18px/28px 'Georgia', serif"
        v, packs, _ = apply_font_floor({"ALL": {"primaryFont": mine}})
        assert v["ALL"]["primaryFont"] == mine
        # We cannot know which pack they meant, so we must not guess one.
        assert packs is None

    def test_family_only_value_is_flagged(self):
        _, _, notes = apply_font_floor({"ALL": {"primaryFont": "Inter, sans-serif"}})
        assert any("SHORTHAND" in n for n in notes)

    def test_pack_code_is_html_the_runtime_can_inject(self):
        # processFontPacks trims `code`; a non-string crashes it.
        packs = font_packs_for(PAIRINGS["modern"])
        entry = next(iter(packs.values()))
        assert isinstance(entry["code"], str) and entry["code"].startswith("<link")
        assert isinstance(entry["name"], str) and entry["name"]

    def test_single_family_pairing_requests_one_family(self):
        code = next(iter(font_packs_for(PAIRINGS["neutral"]).values()))["code"]
        assert code.count("family=") == 1, "Inter+Inter should not be requested twice"

    def test_custom_families_are_url_encoded(self):
        packs = font_packs_for(pairing_from_families("Space Grotesk", "Source Sans 3"))
        code = next(iter(packs.values()))["code"]
        assert "family=Space+Grotesk" in code and "family=Source+Sans+3" in code

    def test_garbage_input_does_not_raise(self):
        assert apply_font_floor(None)[0] == {}
        assert apply_font_floor({"ALL": "nonsense"})[1] is None
