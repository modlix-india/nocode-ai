"""AssetPickerAgent — vision BaseAgent wrapping the logo/creative picker call.

Replaces the direct ``openai.beta.chat.completions.parse(...)`` call in
``agents/product/product_assets.py`` with a properly-named agent so vision
picks show up in trace/cost/observability surfaces alongside ProductAgent.
"""

from app.agents.adzump.agents.asset_picker.agent import (
    AssetPickerAgent,
    get_asset_picker_agent,
)

__all__ = ["AssetPickerAgent", "get_asset_picker_agent"]
