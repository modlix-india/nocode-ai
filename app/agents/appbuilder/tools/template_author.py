"""author_template — generate email/PDF/message template content from a prompt.

Lets the appbuilder agent author a template's content (inline-styled, email/PDF-safe HTML with
FreeMarker ${...} merge fields). Returns {subject, html, message}; the agent then persists it with
create_object / update_object (object_type="template"), placing html under
templateParts.<lang>.body and subject under templateParts.<lang>.subject.

Shares the generation logic with the editor's AI tab (POST /api/ai/appbuilder/template) via
app.services.template_ai.generate_template_content.
"""

from __future__ import annotations

from app.core.tools.base import ToolDefinition, ToolParameter, ToolResult
from app.services.template_ai import generate_template_content


async def _execute_author_template(params: dict, context: dict) -> ToolResult:
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return ToolResult(success=False, error="`prompt` is required")

    template_type = params.get("template_type") or "email"
    result = await generate_template_content(
        prompt=prompt,
        template_type=template_type,
        current_html=params.get("current_html") or "",
        current_subject=params.get("current_subject") or "",
        language=params.get("language") or "en",
    )
    html_len = len(result.get("html", "") or "")
    return ToolResult(
        success=True,
        data=result,
        model_summary=(
            f"Authored {template_type} template content ({html_len} chars of html"
            f"{', subject set' if result.get('subject') else ''}). "
            "Persist via create_object/update_object (object_type='template'): put html at "
            "templateParts.<lang>.body and subject at templateParts.<lang>.subject."
        ),
    )


author_template_tool = ToolDefinition(
    name="author_template",
    display_name="Author Template",
    description=(
        "Generate or revise a notification template's content (inline-styled, email/PDF-safe HTML "
        "with FreeMarker ${...} merge fields) from a natural-language prompt. Returns "
        "{subject, html, message}. Use this to author template bodies, then persist with "
        "create_object/update_object (object_type='template'): html goes under "
        "templateParts.<lang>.body, subject under templateParts.<lang>.subject. Supports email, pdf, "
        "inapp, whatsapp and sms."
    ),
    parameters=[
        ToolParameter(
            name="prompt",
            type="string",
            description="What the template should say/look like, or the change to make.",
        ),
        ToolParameter(
            name="template_type",
            type="string",
            description="Template channel/type.",
            required=False,
            default="email",
            enum=["email", "pdf", "inapp", "whatsapp", "sms"],
        ),
        ToolParameter(
            name="current_html",
            type="string",
            description="Existing body HTML to revise. Omit to create from scratch.",
            required=False,
        ),
        ToolParameter(
            name="current_subject",
            type="string",
            description="Existing subject line to revise (email only).",
            required=False,
        ),
        ToolParameter(
            name="language",
            type="string",
            description="Language code, e.g. 'en'.",
            required=False,
            default="en",
        ),
    ],
    execute=_execute_author_template,
)


TEMPLATE_AUTHOR_TOOLS = [author_template_tool]
