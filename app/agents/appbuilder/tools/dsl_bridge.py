"""KIRun DSL ↔ JSON conversion bridge.

Uses kirun-py's JSONToTextTransformer and DSLCompiler to convert between
the raw JSON function definition format and the LLM-friendly DSL text.

The bridge is lazy-loaded at first use.  If kirun-py is not installed,
all methods gracefully return None / raise ImportError.

Example DSL output:
    FUNCTION handleLogin
        LOGIC
            validate: System.If(condition = `Store.email != '' and Store.password != ''`)
                true
                    doLogin: UIEngine.Login(username = `Store.email`, password = `Store.password`)
                false
                    showError: UIEngine.Message(message = "Please fill in all fields")
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Module-level singleton
_bridge_instance: DSLBridge | None = None


class DSLBridge:
    """Convert between KIRun function JSON and LLM-friendly DSL text."""

    def __init__(self) -> None:
        self._transformer = None
        self._compiler = None

    def _ensure_transformer(self):
        if self._transformer is None:
            from kirun_py.dsl.transformer.json_to_text import JSONToTextTransformer
            self._transformer = JSONToTextTransformer()
        return self._transformer

    def _ensure_compiler(self):
        if self._compiler is None:
            from kirun_py.dsl.dsl_compiler import DSLCompiler
            self._compiler = DSLCompiler()
        return self._compiler

    async def json_to_dsl(self, function_json: dict[str, Any]) -> str:
        """Convert a function/event definition JSON to DSL text.

        Args:
            function_json: The function definition dict (with steps, parameters, events, etc.)

        Returns:
            DSL text representation.

        Raises:
            Exception if conversion fails.
        """
        transformer = self._ensure_transformer()
        return await transformer.transform(function_json)

    async def dsl_to_json(self, dsl_text: str) -> dict[str, Any]:
        """Parse DSL text back to function definition JSON.

        Args:
            dsl_text: The DSL text starting with 'FUNCTION ...'

        Returns:
            Function definition dict.

        Raises:
            Exception if parsing fails.
        """
        compiler = self._ensure_compiler()
        return await compiler.compile(dsl_text)

    def is_dsl_text(self, input_val: Any) -> bool:
        """Detect if input is DSL text (vs JSON dict).

        A simple heuristic: string that starts with 'FUNCTION' after stripping.
        """
        if not isinstance(input_val, str):
            return False
        stripped = input_val.strip()
        return stripped.startswith("FUNCTION") or stripped.startswith("function")


def get_dsl_bridge() -> DSLBridge:
    """Get or create the module-level DSLBridge singleton.

    Raises ImportError if kirun-py is not installed.
    """
    global _bridge_instance
    if _bridge_instance is None:
        # Verify kirun-py is importable
        import kirun_py  # noqa: F401
        _bridge_instance = DSLBridge()
    return _bridge_instance
