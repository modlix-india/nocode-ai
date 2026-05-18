from app.core.tools.base import ToolDefinition, ToolResult

from app.services.llm_provider import get_llm_provider

from app.agents.adzump.agents.creative.models import (
    CallToAction,
)

from app.agents.adzump.agents.creative.utils import (
    load_prompt,
    validate_creative_response,
)


async def _generate_creative_text(params, context):
    session = context.get("session_context", {})

    product_profile = session.get("product_profile", {})
    summary = product_profile.get("summary")

    if not summary:
        return ToolResult(
            success=False,
            error=(
                "Business summary not found. Please complete product analysis first."
            ),
        )

    llm_provider = get_llm_provider("openai")

    prompt_template = load_prompt("creative_text.txt")

    valid_ctas = "\n".join([cta.value for cta in CallToAction])

    prompt = prompt_template.format(
        summary=summary,
        valid_ctas=valid_ctas,
    )

    response = await llm_provider.create_completion(
        system_prompt=(
            "You are a Meta Ads Creative Strategist and Copywriter. "
            "Return only valid JSON."
        ),
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
        model_tier="balanced",
    )

    if not response or "content" not in response:
        return ToolResult(
            success=False,
            error="Invalid LLM response for creative generation.",
        )

    raw_output = response["content"]

    creative_data = validate_creative_response(raw_output)

    session.setdefault("campaign_spec", {})["creative"] = creative_data

    stream = context.get("event_stream")

    if stream:
        await stream.emit_data(
            "creative_text",
            creative_data,
        )

    primary = creative_data.get("primary_texts") or []
    headlines = creative_data.get("headlines") or []
    descs = creative_data.get("descriptions") or []
    cta = creative_data.get("cta", "")

    def fmt_list(lst):
        if not lst:
            return "—"
        return "\n  ".join(f"{i + 1}. {item.replace(chr(10), chr(10) + '     ')}" for i, item in enumerate(lst))

    response_summary = (
        f"Creative text generated successfully:\n\n"
        f"**Primary Texts:**\n  {fmt_list(primary)}\n\n"
        f"**Headlines:**\n  {fmt_list(headlines)}\n\n"
        f"**Descriptions:**\n  {fmt_list(descs)}\n\n"
        f"**CTA:** {cta}"
    )

    return ToolResult(
        success=True,
        data=creative_data,
        summary=response_summary,
    )


generate_creative_text = ToolDefinition(
    name="generate_creative_text",
    description=(
        "Generate Meta ad creative text including "
        "primary texts, headlines, descriptions, and CTA."
    ),
    display_name="Generate Creative Text",
    parameters=[],
    execute=_generate_creative_text,
)
