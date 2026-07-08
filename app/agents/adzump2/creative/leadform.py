"""A4 lead-form builder — fields from the profile + vertical, with the legacy
retry-on-malformed idea (ported from ``meta/lead_form.txt``).

The LLM (M3) proposes the form structure; a bad/malformed structure is retried a
bounded number of times, then the builder falls back to the vertical's default
form (never returns nothing). Meta native form vs landing-page form is the
campaign type's call (J7/A6); A4 produces the spec either way.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.agents.adzump2.creative.models import LeadForm, LeadFormField
from app.agents.adzump2.creative.taxonomy import VerticalTaxonomy

logger = logging.getLogger(__name__)

# Field types the builder accepts from the model; anything else is malformed.
_FIELD_TYPES = {"FULL_NAME", "PHONE", "EMAIL", "SHORT_TEXT", "CITY", "CHOICE"}

# initial attempt + this many retries before falling back
DEFAULT_LEADFORM_RETRIES = 2


class MalformedLeadForm(ValueError):
    """Raised internally when a model lead-form payload can't be normalized."""


def _normalize_fields(raw_fields: Any) -> list[LeadFormField]:
    """Coerce a model ``fields`` payload into ``LeadFormField[]``.

    Accepts a bare-string field (``"FULL_NAME"``) or an object
    (``{key,type,label?,options?,required?}``). Raises ``MalformedLeadForm`` when
    the payload isn't a non-empty list, or any field lacks a usable type, or a
    CHOICE field carries no options.
    """
    if not isinstance(raw_fields, list) or not raw_fields:
        raise MalformedLeadForm("fields must be a non-empty list")

    out: list[LeadFormField] = []
    for item in raw_fields:
        if isinstance(item, str):
            ftype = item.strip().upper()
            if ftype not in _FIELD_TYPES:
                raise MalformedLeadForm(f"unknown field type '{item}'")
            out.append(LeadFormField(key=ftype, type=ftype))
            continue
        if isinstance(item, dict):
            ftype = str(item.get("type") or "").strip().upper()
            if ftype not in _FIELD_TYPES:
                raise MalformedLeadForm(f"unknown field type '{item.get('type')}'")
            options = item.get("options") or []
            if ftype == "CHOICE" and (not isinstance(options, list) or not options):
                raise MalformedLeadForm("CHOICE field needs a non-empty options list")
            key = str(item.get("key") or ftype).strip() or ftype
            out.append(
                LeadFormField(
                    key=key,
                    type=ftype,
                    label=str(item.get("label") or "").strip(),
                    options=[str(o) for o in options] if isinstance(options, list) else [],
                    required=bool(item.get("required", True)),
                )
            )
            continue
        raise MalformedLeadForm(f"field is neither a string nor an object: {item!r}")
    return out


def _parse_lead_form(payload: Any, *, default_thankyou: str, default_privacy: str) -> LeadForm:
    """Turn a model payload into a validated ``LeadForm`` (or raise)."""
    if not isinstance(payload, dict):
        raise MalformedLeadForm("lead form payload is not an object")
    fields = _normalize_fields(payload.get("fields"))
    privacy = str(payload.get("privacyUrl") or payload.get("privacy_url") or default_privacy or "").strip()
    thankyou = str(
        payload.get("thankyou") or payload.get("thankYouMessage") or default_thankyou or ""
    ).strip()
    return LeadForm(fields=fields, privacy_url=privacy, thankyou=thankyou, source="GENERATED")


def fallback_lead_form(profile: dict[str, Any], taxonomy: VerticalTaxonomy) -> LeadForm:
    """Deterministic vertical-default lead form — used when the LLM keeps
    returning a malformed structure. Never fails."""
    fields = [
        LeadFormField(
            key=str(f.get("key") or f.get("type")),
            type=str(f.get("type")),
            label=str(f.get("label") or ""),
            options=[str(o) for o in (f.get("options") or [])],
            required=bool(f.get("required", True)),
        )
        for f in taxonomy.lead_form_fields
    ]
    privacy = str((profile or {}).get("privacy_url") or (profile or {}).get("brand_url") or "").strip()
    return LeadForm(
        fields=fields,
        privacy_url=privacy,
        thankyou=taxonomy.thankyou,
        source="FALLBACK",
    )


def _build_prompt(profile: dict[str, Any], taxonomy: VerticalTaxonomy) -> str:
    allowed = ", ".join(sorted(_FIELD_TYPES))
    profile_json = json.dumps(
        {
            "name": profile.get("name"),
            "pitch": profile.get("pitch"),
            "offerings": profile.get("offerings"),
            "price_band": profile.get("price_band"),
            "geo": profile.get("geo"),
        },
        default=str,
    )[:2000]
    return (
        "Design a lead-capture form for the product below "
        f"(vertical: {taxonomy.code}).\n"
        f"Product profile: {profile_json}\n\n"
        "Rules:\n"
        f"- Each field type MUST be one of: {allowed}.\n"
        "- Always include FULL_NAME, PHONE, and EMAIL. Add at most 2 qualifying "
        "fields relevant to this vertical (e.g. a CHOICE for budget/timeline).\n"
        "- A CHOICE field MUST carry a non-empty options list.\n"
        "- Keep it short — a long form kills conversion.\n\n"
        "Output ONLY this JSON (no prose, no code fences):\n"
        '{"fields":[{"key":"...","type":"...","label":"...","options":["..."],'
        '"required":true}],"privacyUrl":"...","thankyou":"..."}'
    )


async def build_lead_form(
    agent: Any,
    profile: dict[str, Any],
    taxonomy: VerticalTaxonomy,
    *,
    retries: int = DEFAULT_LEADFORM_RETRIES,
    auth: Any = None,
    event_stream: Any = None,
) -> LeadForm:
    """Build a ``LeadForm`` via M3, retrying on a malformed structure, then
    falling back to the vertical default.

    ``agent`` must expose ``_llm_json(task, purpose=..., auth=, event_stream=)``
    (the CreativeAgent seam). Offline tests monkeypatch that seam.
    """
    prompt = _build_prompt(profile, taxonomy)
    attempts = max(1, retries + 1)
    last_err = ""

    for attempt in range(attempts):
        try:
            payload = await agent._llm_json(
                prompt, purpose="leadform", auth=auth, event_stream=event_stream
            )
            form = _parse_lead_form(
                payload,
                default_thankyou=taxonomy.thankyou,
                default_privacy=str(
                    (profile or {}).get("privacy_url") or (profile or {}).get("brand_url") or ""
                ),
            )
            if attempt:
                logger.info("build_lead_form: recovered on attempt %d", attempt + 1)
            return form
        except MalformedLeadForm as e:
            last_err = str(e)
            logger.warning(
                "build_lead_form: malformed structure (attempt %d/%d): %s",
                attempt + 1, attempts, last_err,
            )

    logger.warning("build_lead_form: exhausted %d attempts (%s) — using vertical fallback",
                   attempts, last_err)
    return fallback_lead_form(profile, taxonomy)
