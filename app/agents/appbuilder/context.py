"""AppBuilder context — loads aicontext docs and builds system prompt.

Loads all documentation from nocode-ui/ui-app/aicontext/ at startup,
caches it via BaseContext, and builds system prompt blocks with
Anthropic prompt caching.

The static prefix contains the agent persona and critical rules.
Dynamic context adds per-session info (clientCode, appCode, etc.)
and the component catalog (when available).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.context import BaseContext
from app.core.session import BaseSession

logger = logging.getLogger(__name__)

# Agent persona and rules prepended to all system prompts
AGENT_PERSONA = """\
You are an expert application builder for the Modlix no-code platform.
You build complete applications through multi-turn conversation.

When asked to build something, you:
1. Plan the application architecture
2. Create the application if needed
3. Build methodically: theme → pages → layout → components → event functions → routing
4. Use fine-grained tools — one tool call per operation
5. Explain what you're doing at each step

Critical rules:
- componentDefinition is a FLAT map (string key → component object). Never nested.
- rootComponent is a STRING key (e.g. "root"), not an object.
- Children are stored as: {"childKey": true} in the parent's children map.
- Event functions cannot receive arguments — they read from Store.
- onClick value format: {"value": "eventFunctionName"}, never a plain string.
- Valid component types: Grid, Flex, Text, Button, TextBox, TextArea, Image, \
Icon, Dropdown, CheckBox, RadioButton, ToggleButton, Calendar, Table, Tabs, \
Stepper, Menu, and others from the component catalog. \
Never use Box, Container, Div, Input, Select — these are not valid types.
- Always use Grid or Flex as layout containers.
- Style properties are responsive: { "ALL": { "default": { ... } } }.
"""

# Ordered list of aicontext doc files to include in system prompt.
# Priority: must-have first, then important, then reference.
AICONTEXT_DOCS = [
    "00-critical-rules.md",
    "02-application-and-page-definitions.md",
    "03-component-system.md",
    "05-style-system.md",
    "07-event-system.md",
    "08-functions-and-actions.md",
    "22-component-reference.md",
    "04-property-system.md",
    "06-state-management.md",
    "21-kirun-system-functions.md",
    "11-data-binding.md",
    "15-examples-and-patterns.md",
    "17-theme-definitions.md",
    "18-style-definitions.md",
    "19-function-definitions.md",
    "20-filler-and-uripath.md",
    "16-schema-definitions.md",
]


def build_appbuilder_context(aicontext_path: str) -> BaseContext:
    """Create and return a BaseContext for the AppBuilder agent.

    Args:
        aicontext_path: Path to the aicontext directory
            (e.g. "../nocode-ui/ui-app/aicontext").

    Returns:
        BaseContext ready to be loaded via await ctx.load()
    """
    base = Path(aicontext_path)
    doc_paths = []

    for filename in AICONTEXT_DOCS:
        path = base / filename
        if path.exists():
            doc_paths.append(path)
        else:
            logger.warning(f"aicontext doc not found: {path}")

    logger.info(f"AppBuilder context: {len(doc_paths)}/{len(AICONTEXT_DOCS)} docs from {base}")

    return BaseContext(
        doc_paths=doc_paths,
        static_prefix=AGENT_PERSONA,
    )
