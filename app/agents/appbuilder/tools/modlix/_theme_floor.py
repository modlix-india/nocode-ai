"""A floor under every theme the agent writes.

Two problems this exists to stop, both found on a generated site (2026-09-16):

1. **Invented variable names.** The agent wrote `{"primaryColor": "#00D9FF",
   "secondaryColor": ...}`. No such variables exist -- the platform's primary is
   `colorOne` -- and nothing in the whole style surface reads them, so the theme
   was inert. The site then rendered on platform defaults while *looking* themed,
   because the agent had also pasted literals into per-component style leaves.

2. **Unset families fall back to platform defaults that clash.** The dropdown's
   hover background resolves to `<backgroundHoverColorOne>`, whose stock value is
   `#A8DEC9`, a mint green. On an electric-blue site the open dropdown highlighted
   rows in green. Same variable also drives ButtonBar's primary hover.

The floor does not overrule the agent: anything it sets explicitly is kept. It
fills the gaps that otherwise resolve to stock values from a different palette.

`APP_THEME_VARIABLES` mirrors `nocode-ui/ui-app/client/src/App/appStyleProperties.ts`.
Regenerate with `scripts/gen_theme_vars.py` when that file gains variables.
"""

from __future__ import annotations

import re
from typing import Any

# The 122 app-level theme variables. Component-level variables (dropdownBackground
# HoverDesign1Primary, gapBetween, ...) are NOT here: there are thousands, they
# carry <designType>/<colorScheme> placeholders, and they are validated by the
# server. This set exists to catch invented *app* names, which is where the model
# actually goes wrong.
APP_THEME_VARIABLES: frozenset[str] = frozenset({
    'backgroundColorEight', 'backgroundColorFive', 'backgroundColorFour', 'backgroundColorNine',
    'backgroundColorOne', 'backgroundColorSeven', 'backgroundColorSix', 'backgroundColorTen',
    'backgroundColorThree', 'backgroundColorTwo', 'backgroundDarkerColorEight', 'backgroundDarkerColorFive',
    'backgroundDarkerColorFour', 'backgroundDarkerColorNine', 'backgroundDarkerColorOne', 'backgroundDarkerColorSeven',
    'backgroundDarkerColorSix', 'backgroundDarkerColorThree', 'backgroundDarkerColorTwo', 'backgroundHoverColorEight',
    'backgroundHoverColorFive', 'backgroundHoverColorFour', 'backgroundHoverColorNine', 'backgroundHoverColorOne',
    'backgroundHoverColorSeven', 'backgroundHoverColorSix', 'backgroundHoverColorThree', 'backgroundHoverColorTwo',
    'bodyBackground', 'bodyFont', 'bodyMargin', 'borderColorFive',
    'borderColorFour', 'borderColorOne', 'borderColorSeven', 'borderColorSix',
    'borderColorThree', 'borderColorTwo', 'colorEight', 'colorEleven',
    'colorFifteen', 'colorFive', 'colorFour', 'colorFourteen',
    'colorNine', 'colorOne', 'colorSeven', 'colorSix',
    'colorTen', 'colorThirteen', 'colorThree', 'colorTwelve',
    'colorTwo', 'errorColor', 'fontColorEight', 'fontColorFive',
    'fontColorFour', 'fontColorNine', 'fontColorOne', 'fontColorSeven',
    'fontColorSix', 'fontColorThree', 'fontColorTwo', 'gradientColorEight',
    'gradientColorFive', 'gradientColorFour', 'gradientColorNine', 'gradientColorOne',
    'gradientColorSeven', 'gradientColorSix', 'gradientColorTen', 'gradientColorThree',
    'gradientColorTwo', 'iconBlackWeight', 'iconBoldWeight', 'iconExtraBoldWeight',
    'iconLightWeight', 'iconMediumWeight', 'iconRegularWeight', 'iconSemiBoldWeight',
    'iconSize1', 'iconSize2', 'iconSize3', 'iconSize4',
    'iconSize5', 'iconSize6', 'iconSize7', 'iconSize8',
    'iconThinWeight', 'iconThinnerWeight', 'informationColor', 'italicFontStyle',
    'letterSpacingOne', 'letterSpacingTwo', 'normalFontStyle', 'obliqueFontStyle',
    'primaryFont', 'quaternaryFont', 'quinaryFont', 'scrollBarHeight',
    'scrollBarHoverWidth', 'scrollBarThumbBg', 'scrollBarThumbBorderRadius', 'scrollBarThumbHoverBg',
    'scrollBarWidth', 'secondaryFont', 'senaryFont', 'successColor',
    'tertiaryFont', 'validationMessageBackgroundColor', 'validationMessageBorder', 'validationMessageBorderRadius',
    'validationMessageFont', 'validationMessageFontColor', 'validationMessageGap', 'validationMessageInset',
    'validationMessageMargin', 'validationMessagePadding', 'validationMessageShadow', 'validationMessageTextAlign',
    'validationMessageWidth', 'warningColor',
})

# What the model reaches for, and what it actually wanted. Every key here has
# been seen in a real run or is the obvious sibling of one that has.
KNOWN_ALIASES: dict[str, str] = {
    'primarycolor': 'colorOne',
    'primary': 'colorOne',
    'accentcolor': 'colorOne',
    'accent': 'colorOne',
    'secondarycolor': 'colorTwo',
    'secondary': 'colorTwo',
    'tertiarycolor': 'colorThree',
    'textcolor': 'fontColorOne',
    'fontcolor': 'fontColorOne',
    'backgroundcolor': 'backgroundColorOne',
    'background': 'bodyBackground',
    'surfacecolor': 'backgroundColorSeven',
    'bordercolor': 'borderColorOne',
    'fontfamily': 'primaryFont',
    'font': 'primaryFont',
}

# colorX -> the hover wash that pairs with it. Seven/Eight/Nine are neutral
# chrome greys in stock and are deliberately left alone.
_HOVER_PAIRS: dict[str, str] = {
    'backgroundHoverColorOne': 'colorOne',
    'backgroundHoverColorTwo': 'colorTwo',
    'backgroundHoverColorThree': 'colorThree',
    'backgroundHoverColorFour': 'colorFour',
    'backgroundHoverColorFive': 'colorFive',
}

# Stock `backgroundHoverColorOne` (#A8DEC9) is stock `colorOne` (#52BD94) mixed
# ~45% toward white, and the same ratio reproduces Two and Three. Deriving at the
# platform's own ratio keeps a generated theme consistent with a hand-built one.
_HOVER_MIX = 0.45

_HEX_RE = re.compile(r'^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$')


def _parse_hex(value: str) -> tuple[int, int, int] | None:
    v = (value or '').strip()
    if not _HEX_RE.match(v):
        return None
    h = v[1:]
    if len(h) == 3:
        h = ''.join(ch * 2 for ch in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix_toward_white(value: str, t: float) -> str | None:
    """Lighten a hex colour by mixing it t of the way to white."""
    rgb = _parse_hex(value)
    if rgb is None:
        return None
    r, g, b = (round(c + (255 - c) * t) for c in rgb)
    return f'#{r:02X}{g:02X}{b:02X}'


def _canonical(name: str) -> str | None:
    """Map a variable the model invented onto the real one, if we can tell."""
    if name in APP_THEME_VARIABLES:
        return name
    return KNOWN_ALIASES.get(name.replace('_', '').replace('-', '').lower())


def _looks_app_level(name: str) -> bool:
    """Is this meant to be an app variable rather than a component one?

    Component variables are named `<component><Property>[<designType><colorScheme>]`
    and so carry a component prefix or a placeholder. Anything short and generic
    that we do not recognise is far more likely to be an invented app name.
    """
    return '<' not in name and len(name) < 40


def apply_theme_floor(
    variables: Any, *, fill_gaps: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Normalise names and, optionally, fill the gaps that fall back to stock.

    `fill_gaps=False` renames and warns but adds nothing. That is what an UPDATE
    wants: it replaces the variable map wholesale, so seeding a value there would
    resurrect a variable someone had deliberately removed.

    Returns (variables, notes). Notes are surfaced in the tool result so the
    agent learns what it got wrong rather than silently shipping an inert theme.
    Never raises: a theme that cannot be understood is passed through untouched.
    """
    notes: list[str] = []
    if not isinstance(variables, dict):
        return {}, notes

    out: dict[str, Any] = {}
    for bp, vars_ in variables.items():
        if not isinstance(vars_, dict):
            out[bp] = vars_
            continue

        renamed: dict[str, Any] = {}
        for raw_name, value in vars_.items():
            name = str(raw_name)
            canon = _canonical(name)
            if canon and canon != name:
                notes.append(
                    f"'{name}' is not a theme variable; wrote '{canon}' instead "
                    f"(nothing reads '{name}', so it would have done nothing)."
                )
                name = canon
            elif canon is None and _looks_app_level(name):
                notes.append(
                    f"'{name}' is not a known app theme variable and may do nothing. "
                    f"App colours are colorOne..colorFifteen; text is fontColorOne.."
                )
            # A later real name wins over an alias that resolved onto it.
            if name not in renamed or raw_name == name:
                renamed[name] = value
        out[bp] = renamed

    if not fill_gaps:
        return out, notes

    all_vars = out.get('ALL')
    if 'ALL' not in out:
        all_vars = {}
        out['ALL'] = all_vars
    elif not isinstance(all_vars, dict):
        # Malformed, but it is the caller's data. Replacing it to make room for
        # defaults would destroy whatever they meant; let the server reject it.
        return out, notes

    # 1. Hover washes, derived from whatever palette the agent chose. Without
    #    these the dropdown, buttonbar and friends hover mint green.
    for hover_name, source_name in _HOVER_PAIRS.items():
        if hover_name in all_vars:
            continue
        source = all_vars.get(source_name)
        if not isinstance(source, str):
            continue
        mixed = _mix_toward_white(source, _HOVER_MIX)
        if mixed:
            all_vars[hover_name] = mixed
            notes.append(f"Set {hover_name}={mixed}, derived from {source_name}.")

    # 2. Grid gap. The platform default is 5px on EVERY grid, which paints the
    #    page background as a seam between full-bleed sections -- glaring on a
    #    dark site. Sections that want space use padding; a layout that wants a
    #    gap sets one per instance.
    if 'gapBetween' not in all_vars:
        all_vars['gapBetween'] = '0px'
        notes.append(
            "Set gapBetween=0px (platform default is 5px, which shows the page "
            "background as a seam between sections)."
        )

    return out, notes
