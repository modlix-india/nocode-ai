"""AI authoring for the WhatsApp message library — write a message, and vary it.

Serves ``POST /api/ai/whatsapp/message`` (the library editor's AI panel).

**Why variants are the product here, not a flourish.** Modlix's WhatsApp integration is a linked
device on the customer's own number, not the Cloud API. There is no Meta review, so nothing checks a
message before it goes out, and equally nothing protects the number: sending identical text to more
than roughly fifteen recipients an hour is a documented trigger for the enforcement that gets a
business number banned with no appeal. A stage rule sends one message to every matching lead, which
is exactly that pattern. So a rule stores a *set* of phrasings and the sender rotates through them.

Writing four genuinely different ways to say "your brochure is attached, shall I book a site visit?"
is a real job for a model, and a better one than authoring Meta templates was: there is no component
tree, no approval status, and no positional ``{{1}}`` parameters to reconcile. The body is a plain
string that the platform owns.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

# Kept in step with MessageTemplateService.SUPPORTED_VARIABLES on the Java side. A body referencing
# anything else interpolates to an empty string and sends a sentence with a hole in it, so the model
# is told the closed list rather than left to invent field names.
SUPPORTED_VARIABLES = [
    "name",
    "firstName",
    "email",
    "phoneNumber",
    "ticketCode",
    "productName",
    "userName",
]

_SYSTEM_PROMPT = f"""You write WhatsApp messages for a sales team, and several different versions of \
each one.

These go out from a real person's WhatsApp number to real leads. Write the way a competent \
salesperson actually types on WhatsApp: short, direct, human. Not marketing copy, not an email, and \
not a press release.

WHY THERE ARE SEVERAL VERSIONS. The same message is sent to many leads. If the text is identical \
every time, WhatsApp's spam detection sees a broadcast and can ban the number permanently, with no \
appeal. So the versions must be genuinely different sentences that happen to mean the same thing. \
Reordering words, swapping one synonym, or moving the emoji is worthless: it is still recognisably \
one message. Vary the sentence structure, the opening, the length and the way the question is asked.

Hard rules:
- Plain text only. No HTML, no markdown, no bold or bullet syntax. WhatsApp shows exactly what you \
write.
- Merge fields use double braces and MUST come from this list only: {", ".join("{{" + v + "}}" for v in SUPPORTED_VARIABLES)}. \
Never invent a field name. If you need something not on the list, write around it.
- Keep each version under about 400 characters unless the request clearly needs more. Long WhatsApp \
messages from an unknown number do not get read.
- At most one link and at most one emoji per version, and neither is required. Vary the wording \
around a link between versions.
- No ALL CAPS words, no "Dear Sir/Madam", no "Greetings". Those read as bulk messaging to a person \
and to a spam filter.
- Write in the language asked for. For Hinglish, write the way people actually type it in Latin \
script, not translated formal Hindi.
- Do not open with "I hope this message finds you well" or any variant of it.

Output format — return ONLY a single JSON object, no prose and no code fences:
{{"variants": ["<version 1>", "<version 2>", ...], "variables": ["<merge field used, without \
braces>"], "message": "<one short sentence describing what you wrote>"}}
"""


def _build_user_message(
    prompt: str,
    variant_count: int,
    current_variants: List[str],
    language: str,
    tone: str,
) -> str:
    parts = [
        f"Language: {language or 'en'}",
        f"Tone: {tone or 'friendly and professional'}",
        f"Write {variant_count} different versions.",
    ]

    if current_variants:
        # Revision rather than a fresh write. The existing text is usually close to what somebody
        # wants, and throwing it away is not what "give me more variants" means.
        existing = "\n".join(f"{i + 1}. {v}" for i, v in enumerate(current_variants) if v and v.strip())
        if existing:
            parts.append(
                "Existing versions — keep what works, and make any new ones clearly different "
                "from these rather than near-duplicates:\n" + existing
            )

    parts.append("Request:\n" + prompt)
    return "\n\n".join(parts)


def _extract_json(text: str) -> Dict[str, Any] | None:
    """Parse the model output into a dict, tolerating code fences and surrounding prose."""
    if not text:
        return None

    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)

    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:  # noqa: BLE001 - fall through to brace-scan
        pass

    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(t[start : end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


_VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*}}")


def _used_variables(variants: List[str]) -> List[str]:
    """Merge fields actually referenced, in first-seen order."""
    seen: List[str] = []
    for body in variants:
        for match in _VARIABLE_PATTERN.finditer(body or ""):
            name = match.group(1)
            if name not in seen:
                seen.append(name)
    return seen


def _unknown_variables(variants: List[str]) -> List[str]:
    """Merge fields the sender cannot resolve, compared case-insensitively."""
    known = {v.lower() for v in SUPPORTED_VARIABLES}
    return [v for v in _used_variables(variants) if v.lower() not in known]


def _too_similar(variants: List[str]) -> bool:
    """
    Whether the versions are really one message wearing hats.

    Compared as word sets rather than as strings, because the failure mode is a model that reorders
    a clause and swaps one synonym. That produces a large edit distance and near-identical
    vocabulary, and it is worth nothing: WhatsApp is not fooled and the number is still exposed.
    """
    meaningful = [v for v in variants if v and v.strip()]
    if len(meaningful) < 2:
        return False

    for i in range(len(meaningful)):
        for j in range(i + 1, len(meaningful)):
            a = set(re.findall(r"\w+", meaningful[i].lower()))
            b = set(re.findall(r"\w+", meaningful[j].lower()))
            if not a or not b:
                continue
            overlap = len(a & b) / max(len(a), len(b))
            if overlap > 0.85:
                return True
    return False


async def generate_message_variants(
    *,
    prompt: str,
    variant_count: int = 4,
    current_variants: List[str] | None = None,
    language: str = "en",
    tone: str = "",
) -> Dict[str, Any]:
    """
    Write several interchangeable phrasings of one message.

    Returns ``{variants, variables, message, warnings}``. Warnings are advisory and surfaced in the
    editor rather than enforced: an unknown merge field or two near-identical versions are things a
    person should see before saving, but refusing to return the draft would just mean they lose it.
    """
    count = max(1, min(int(variant_count or 4), 8))
    existing = [v for v in (current_variants or []) if v and v.strip()]

    provider = get_llm_provider()

    result = await provider.create_completion(
        system_prompt=_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": _build_user_message(prompt, count, existing, language, tone),
            }
        ],
        model_tier="balanced",
        max_tokens=4096,
        use_cache=True,
    )

    content = (result or {}).get("content", "") or ""
    parsed = _extract_json(content)

    if not parsed:
        # Not JSON. Salvage rather than fail: a plausible message that arrived in the wrong wrapper
        # is still useful to somebody staring at an empty editor, and losing it to a parse error
        # would be the worse outcome.
        logger.warning("whatsapp_message_ai: response was not valid JSON; using raw content as one variant")
        salvaged = content.strip()
        return {
            "variants": [salvaged] if salvaged else [],
            "variables": _used_variables([salvaged]),
            "message": "Wrote one version. The model did not return the expected format, so please check it.",
            "warnings": ["Only one version was produced, so every recipient would get identical text."],
        }

    variants = [
        str(v).strip()
        for v in (parsed.get("variants") or [])
        if v is not None and str(v).strip()
    ]

    warnings: List[str] = []

    if len(variants) < 2:
        warnings.append(
            "Only one version. Every lead matched by a rule would get identical text, which is the "
            "pattern most likely to get this number blocked."
        )

    unknown = _unknown_variables(variants)
    if unknown:
        warnings.append(
            "These merge fields will send as blank because the platform cannot fill them: "
            + ", ".join("{{" + u + "}}" for u in unknown)
        )

    if _too_similar(variants):
        warnings.append(
            "Some versions are nearly the same words in a different order, which does not count as "
            "variation. Ask for them to be rewritten more differently."
        )

    return {
        "variants": variants,
        "variables": [v for v in _used_variables(variants) if v.lower() in {s.lower() for s in SUPPORTED_VARIABLES}],
        "message": parsed.get("message") or "Wrote the message.",
        "warnings": warnings,
    }
