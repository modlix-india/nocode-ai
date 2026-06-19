"""Tools for the v4 agent. v0 ships exactly one: `code_run`.

Add tools here one at a time, only when a bench scenario fails for lack
of one. Every addition gets a numbered line in ../CLAUDE.md explaining
WHY it was added (which scenario failed without it).
"""

from app.agents.appbuilderv4.tools.code_run import code_run_tool
from app.agents.appbuilderv4.tools.compare_to_source import compare_to_source_tool
from app.agents.appbuilderv4.tools.extract_site_assets import extract_site_assets_tool
from app.agents.appbuilderv4.tools.platform_kb import TOOLS as _PLATFORM_KB_TOOLS

# Per-app KB tools — re-exported VERBATIM from v3 (same `cfa_app_kb`
# table, same propose-then-commit flow, same repo layer at
# `app.services.app_kb`). v4 doesn't need its own implementation.
from app.agents.appbuilder.tools.kb_app import (
    kb_app_get_tool,
    kb_app_history_tool,
    kb_app_search_tool,
    kb_app_list_sections_tool,
    propose_kb_update_tool,
    commit_kb_update_tool,
)

TOOLS = [
    code_run_tool,
    # Clone-loop tools. extract_site_assets is the unified recon: it does
    # screenshots + asset harvest + font harvest + section/hover/animation
    # recon in ONE Playwright pass. The standalone screenshot_external_url
    # and extract_site_fonts tools were folded into it (2026-06-18).
    extract_site_assets_tool,
    compare_to_source_tool,
    # Platform KB (file-backed, refreshed on deploy).
    *_PLATFORM_KB_TOOLS,
    # Per-app KB (MySQL, propose-then-commit).
    kb_app_get_tool,
    kb_app_history_tool,
    kb_app_search_tool,
    kb_app_list_sections_tool,
    propose_kb_update_tool,
    commit_kb_update_tool,
]
