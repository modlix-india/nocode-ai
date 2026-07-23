"""AI template authoring — generate email / PDF / message template content from a prompt.

Shared by the ``POST /api/ai/appbuilder/template`` endpoint (the editor's AI tab) and the
``author_template`` agent tool. Produces inline-styled, email/PDF-safe HTML with FreeMarker
``${...}`` merge fields preserved, plus a subject line for emails.

The model is asked to return a strict JSON object ``{subject, html, message}`` so both callers get a
uniform result; parsing is defensive (code fences / surrounding prose are tolerated).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from app.services.llm_provider import get_llm_provider

logger = logging.getLogger(__name__)

# The Modlix template editor represents content as a flat, ordered list of BLOCKS (defined in the
# nocode-ui block model). Generated HTML must be built from these block patterns so it maps cleanly
# back into editable blocks (via the editor's HTML->blocks import), not a single opaque Raw HTML blob.
_BLOCK_CATALOG = """Modlix template blocks — build the content from these so it stays editable in the \
visual builder (each maps to one block):
- Heading  -> <h1>..<h4> with inline font-size / color / text-align. Section titles.
- Text     -> <p> paragraph; may contain inline <a>, <strong>/<b>, <em>, <br> and ${merge fields}.
- List     -> <ul> or <ol> with <li> items.
- Button   -> an <a> styled as a button (inline-block; background-color; padding; border-radius; \
color). Primary calls to action; href may be a ${url}.
- Link     -> a plain text <a> (color, optional underline) for inline/standalone links.
- Image    -> <img> with src / alt / width; wrap in <a href> for a clickable image.
- Divider  -> <hr> / a thin horizontal rule.
- Spacer   -> vertical whitespace between sections.
- Raw HTML -> anything that does not fit the above (kept verbatim, but NOT visually editable).

Layout rules that keep the output block-mappable:
- Lay content out as a SINGLE vertical column of the blocks above, in reading order.
- Prefer the semantic elements above (h1..h4, p, ul/ol, img, hr, styled <a>) over generic <div>/<span> \
wrappers.
- AVOID multi-column layouts, nested layout tables and CSS grid/flex — they cannot be represented as \
blocks and collapse into one Raw HTML block. A single outer wrapper table for the email frame is fine; \
just keep the inner content a linear column.
"""

_SYSTEM_PROMPT = f"""You are an expert template designer for the Modlix platform. You produce a single \
self-contained template that renders reliably in email clients and, when the type is pdf, in \
OpenHTMLtoPDF.

{_BLOCK_CATALOG}

Hard rules:
- INLINE styles only. No external stylesheets, no <script>, no remote CSS/JS. Images may reference \
absolute https URLs.
- Use FreeMarker merge fields for dynamic values, e.g. ${{name}}, ${{email}}, ${{actionUrl}}. PRESERVE \
any ${{...}} already present in the current content unless the user asks to change it. Do NOT invent a \
templating syntax other than FreeMarker.
- Return a COMPLETE, valid HTML document starting with <!DOCTYPE html> (for email and pdf). For pdf \
you may include <style>@page {{ size: A4; margin: 20mm; }}</style> in the head.
- For whatsapp / sms, "html" is plain message text (short), and subject is "".
- Keep copy concise and professional unless the user asks otherwise.

Output format — return ONLY a single JSON object, no prose and no code fences:
{{"subject": "<email subject, or empty string for non-email>", "html": "<the template html or text>", \
"message": "<one short sentence describing what you did>"}}
"""


def _build_user_message(
    prompt: str,
    template_type: str,
    current_html: str,
    current_subject: str,
    language: str,
) -> str:
    parts = [f"Template type: {template_type}", f"Language: {language or 'en'}"]
    if current_subject:
        parts.append(f"Current subject:\n{current_subject}")
    if current_html and current_html.strip():
        parts.append("Current content (modify this; keep what still applies):\n" + current_html)
    else:
        parts.append("There is no existing content — create it from scratch.")
    parts.append("Request:\n" + prompt)
    return "\n\n".join(parts)


def _extract_json(text: str) -> Dict[str, Any] | None:
    """Parse the model output into a dict, tolerating code fences / surrounding prose."""
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


async def generate_template_content(
    *,
    prompt: str,
    template_type: str = "email",
    current_html: str = "",
    current_subject: str = "",
    language: str = "en",
) -> Dict[str, Any]:
    """Generate/revise template content for a prompt. Returns {subject, html, message}."""
    provider = get_llm_provider()
    user_msg = _build_user_message(prompt, template_type, current_html, current_subject, language)

    result = await provider.create_completion(
        system_prompt=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        model_tier="balanced",
        max_tokens=8192,
        use_cache=True,
    )
    content = (result or {}).get("content", "") or ""
    parsed = _extract_json(content)

    if not parsed:
        # The model didn't return JSON — treat the whole response as the HTML body.
        logger.warning("template_ai: response was not valid JSON; using raw content as html")
        return {
            "subject": current_subject or "",
            "html": content.strip(),
            "message": "Generated template content.",
        }

    return {
        "subject": (parsed.get("subject") or "") if template_type == "email" else "",
        "html": parsed.get("html") or "",
        "message": parsed.get("message") or "Template updated.",
    }
