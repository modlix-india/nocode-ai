"""J5 creative taxonomy + slot specs — the field-level source of truth for A4.

The J5 vertical registry is a P1 dependency owned Java-side; until it lands, A4
carries a local, versioned copy of the **creative attribute taxonomy** (axes +
closed vocab) and the **platform slot specs** (RSA / Meta pool sizes + length
limits). Both are drawn verbatim from CONTRACT.md §1.3 (the real-estate axis/value
set, the draft CREATIVE §2 references) and §6 rule 5/9 (RSA minimums; attribute
axes/values known to the registry — *warn*, not hard-fail, for novel values the
loop is exploring).

TODO(J5): fetch this from the Java vertical registry instead of the local copy
once J5 ships; keep `real_estate` + `generic` as the offline fallback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── attribute taxonomy ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Axis:
    """One creative-attribute axis.

    closed=True  → values are a fixed vocab (novel values are kept but flagged,
                   per CONTRACT §6 rule 9: warn, don't hard-fail).
    closed=False → free text (e.g. the ``hook`` line).
    multi=True   → the axis holds a list of values (e.g. ``copyAttributes``).
    """

    name: str
    closed: bool
    multi: bool = False
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerticalTaxonomy:
    """The creative-attribute taxonomy + lead-form defaults for one vertical."""

    code: str
    axes: dict[str, Axis]
    default_angles: tuple[str, ...]
    lead_form_fields: tuple[dict[str, Any], ...]
    thankyou: str

    def axis(self, name: str) -> Axis | None:
        return self.axes.get(name)


def _axes(*axes: Axis) -> dict[str, Axis]:
    return {a.name: a for a in axes}


# Real-estate axes/values — CONTRACT.md §1.3 (angles) + the draft CREATIVE §2 set.
_REAL_ESTATE = VerticalTaxonomy(
    code="real_estate",
    axes=_axes(
        Axis(
            "angle",
            closed=True,
            values=(
                "price_emi",
                "location",
                "amenities",
                "investment_roi",
                "lifestyle",
                "ready_to_move",
                "scarcity",
                "trust_rera",
            ),
        ),
        Axis("hook", closed=False),  # free-text (e.g. "12% assured ROI")
        Axis(
            "visualSubject",
            closed=True,
            values=(
                "interior_render",
                "exterior",
                "amenity",
                "clubhouse",
                "floor_plan",
                "lifestyle",
                "location_map",
                "aerial",
                "sample_flat",
            ),
        ),
        Axis(
            "offer",
            closed=True,
            values=(
                "pre_launch_price",
                "launch_offer",
                "limited_units",
                "assured_returns",
                "ready_to_move_in",
                "no_offer",
            ),
        ),
        Axis(
            "cta",
            closed=True,
            values=(
                "book_now",
                "enquire_now",
                "download_brochure",
                "schedule_visit",
                "call_now",
            ),
        ),
        Axis(
            "audiencePairing",
            closed=True,
            values=(
                "nri_investors",
                "end_users",
                "first_time_buyers",
                "investors",
                "upgraders",
                "luxury_seekers",
            ),
        ),
        Axis(
            "copyAttributes",
            closed=True,
            multi=True,
            values=(
                "number_led",
                "urgency",
                "question_led",
                "benefit_led",
                "social_proof",
                "location_led",
            ),
        ),
    ),
    default_angles=("investment_roi", "location", "amenities"),
    lead_form_fields=(
        {"key": "FULL_NAME", "type": "FULL_NAME", "required": True},
        {"key": "PHONE", "type": "PHONE", "required": True},
        {"key": "EMAIL", "type": "EMAIL", "required": True},
        {
            "key": "budget",
            "type": "CHOICE",
            "label": "Your budget",
            "options": ["<80L", "80L-1.2Cr", ">1.2Cr"],
            "required": True,
        },
    ),
    thankyou="Thanks! We'll call you to schedule a site visit.",
)


# Generic fallback — a small closed set that works for any vertical the loop
# hasn't studied. Deduced-vertical == "generic" (A2) routes here.
_GENERIC = VerticalTaxonomy(
    code="generic",
    axes=_axes(
        Axis(
            "angle",
            closed=True,
            values=(
                "value",
                "quality",
                "convenience",
                "trust",
                "offer",
                "social_proof",
                "urgency",
            ),
        ),
        Axis("hook", closed=False),
        Axis(
            "visualSubject",
            closed=True,
            values=("product", "lifestyle", "people", "logo", "before_after"),
        ),
        Axis(
            "offer",
            closed=True,
            values=("discount", "free_trial", "bundle", "limited_time", "no_offer"),
        ),
        Axis(
            "cta",
            closed=True,
            values=("shop_now", "learn_more", "sign_up", "get_quote", "contact_us"),
        ),
        Axis(
            "audiencePairing",
            closed=True,
            values=("new_customers", "returning", "value_seekers", "premium"),
        ),
        Axis(
            "copyAttributes",
            closed=True,
            multi=True,
            values=(
                "number_led",
                "urgency",
                "question_led",
                "benefit_led",
                "social_proof",
            ),
        ),
    ),
    default_angles=("value", "quality", "trust"),
    lead_form_fields=(
        {"key": "FULL_NAME", "type": "FULL_NAME", "required": True},
        {"key": "PHONE", "type": "PHONE", "required": True},
        {"key": "EMAIL", "type": "EMAIL", "required": True},
    ),
    thankyou="Thanks! We'll be in touch shortly.",
)


_TAXONOMIES: dict[str, VerticalTaxonomy] = {
    _REAL_ESTATE.code: _REAL_ESTATE,
    _GENERIC.code: _GENERIC,
}


def get_taxonomy(vertical: str | None) -> VerticalTaxonomy:
    """Return the taxonomy for a vertical code, falling back to ``generic``."""
    if not vertical:
        return _GENERIC
    return _TAXONOMIES.get(str(vertical).strip().lower(), _GENERIC)


def validate_attributes(
    attrs: dict[str, Any], taxonomy: VerticalTaxonomy
) -> tuple[dict[str, Any], list[str]]:
    """Coerce a proposed attribute bag onto the taxonomy axes.

    Returns ``(clean, warnings)``. Guarantees ``set(clean) ⊆ set(axes)`` — an
    unknown *axis* is dropped (J20 can't attribute it). An unknown *value* on a
    closed axis is KEPT and flagged (CONTRACT §6 rule 9: novel values are the
    loop exploring, warn not hard-fail). Multi axes are normalized to a list.
    """
    clean: dict[str, Any] = {}
    warnings: list[str] = []

    for key, val in (attrs or {}).items():
        axis = taxonomy.axis(key)
        if axis is None:
            warnings.append(f"dropped unknown attribute axis '{key}'")
            continue

        if axis.multi:
            vals = val if isinstance(val, list) else ([val] if val is not None else [])
            out: list[str] = []
            for v in vals:
                sv = str(v).strip()
                if not sv:
                    continue
                if axis.closed and sv not in axis.values:
                    warnings.append(f"novel value '{sv}' on axis '{key}' (explore)")
                out.append(sv)
            if out:
                clean[key] = out
            continue

        sv = str(val).strip()
        if not sv:
            continue
        if axis.closed and sv not in axis.values:
            warnings.append(f"novel value '{sv}' on axis '{key}' (explore)")
        clean[key] = sv

    return clean, warnings


# ── platform slot specs (RSA / Meta pools + length limits) ────────────────────


@dataclass(frozen=True)
class SlotSpec:
    """One copy pool: which field, how many entries, and per-entry char limit."""

    field: str  # Copy field name: headlines | primary_texts | descriptions
    min: int
    max: int
    char_limit: int


# RSA = Google responsive search ad (text-only; up to 15 headlines / 4 desc).
# Meta = the visual-format family (primary text / headline / description).
_META_SLOTS = (
    SlotSpec("primary_texts", 1, 5, 125),
    SlotSpec("headlines", 1, 5, 40),
    SlotSpec("descriptions", 0, 5, 30),
)

FORMAT_SLOTS: dict[str, tuple[SlotSpec, ...]] = {
    "RSA": (
        SlotSpec("headlines", 3, 15, 30),
        SlotSpec("descriptions", 2, 4, 90),
    ),
    "IMAGE": _META_SLOTS,
    "VIDEO": _META_SLOTS,
    "CAROUSEL": _META_SLOTS,
    "DEMAND_GEN": (
        SlotSpec("headlines", 1, 5, 40),
        SlotSpec("descriptions", 1, 5, 90),
        SlotSpec("primary_texts", 0, 5, 90),
    ),
}

VISUAL_FORMATS: frozenset[str] = frozenset({"IMAGE", "VIDEO", "CAROUSEL", "DEMAND_GEN"})
KNOWN_FORMATS: frozenset[str] = frozenset(FORMAT_SLOTS)

# All Copy pool fields, so normalize can zero out the ones a format doesn't use.
_ALL_POOL_FIELDS = ("headlines", "primary_texts", "descriptions")


def _dedupe_trim(lines: Any, char_limit: int, cap: int) -> list[str]:
    """Strip, drop empties + over-limit, case-insensitive dedupe, cap length."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines if isinstance(lines, list) else []:
        s = str(raw).strip()
        if not s or len(s) > char_limit:
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= cap:
            break
    return out


def normalize_pools(
    copy: dict[str, Any], fmt: str
) -> tuple[dict[str, list[str] | str], list[str]]:
    """Clean copy pools to a format's slot spec.

    Returns ``(pools, shortfalls)`` where ``pools`` has the three list fields
    plus ``cta``. Fields not used by the format are emptied. A pool below its
    ``min`` is reported as a shortfall (never fabricated — the critic/repair loop
    owns fixing it; A4 does not invent facts).
    """
    specs = {s.field: s for s in FORMAT_SLOTS.get(fmt, ())}
    pools: dict[str, list[str] | str] = {f: [] for f in _ALL_POOL_FIELDS}
    shortfalls: list[str] = []

    for f in _ALL_POOL_FIELDS:
        spec = specs.get(f)
        if spec is None:
            pools[f] = []
            continue
        cleaned = _dedupe_trim(copy.get(f), spec.char_limit, spec.max)
        pools[f] = cleaned
        if len(cleaned) < spec.min:
            shortfalls.append(
                f"{fmt}.{f}: {len(cleaned)}/{spec.min} min "
                f"(<= {spec.char_limit} chars each)"
            )

    cta = str(copy.get("cta") or "").strip()
    pools["cta"] = cta
    return pools, shortfalls
