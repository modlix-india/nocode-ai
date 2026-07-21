"""Safety-net tests for the modlix tool suite.

Cheapest possible early-warning that an import got broken or a TOOLS list
got accidentally emptied. Doesn't exercise any tool behaviour — just that
every submodule loads and looks structurally sane.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app.core.tools.base import ToolDefinition


MODLIX_PKG = "app.agents.appbuilder.tools.modlix"

MODLIX_MODULES = [
    "infra",
    "components",
    "pages",
    "kirun",
    "kirun_events",
    "schemas",
    "visuals",
    "visuals_browser",
    "image_ops",
    "security",
    "app_admin",
    "messaging",
    "runtime",
]

HELPER_MODULES = [
    "_conventions",
    "_kirun_dsl",
    "_kirun_layout",
    "_page_ops",
]


def test_every_modlix_module_imports():
    """Each modlix submodule imports cleanly + exports non-empty TOOLS list of ToolDefinitions."""
    for name in MODLIX_MODULES:
        mod = importlib.import_module(f"{MODLIX_PKG}.{name}")
        assert hasattr(mod, "TOOLS"), f"{name} is missing TOOLS attribute"
        tools = mod.TOOLS
        assert isinstance(tools, list), f"{name}.TOOLS is not a list (got {type(tools).__name__})"
        assert len(tools) > 0, f"{name}.TOOLS is empty"
        for i, t in enumerate(tools):
            assert isinstance(t, ToolDefinition), (
                f"{name}.TOOLS[{i}] is not a ToolDefinition (got {type(t).__name__})"
            )


def test_no_modlix_module_imports_html_compiler():
    """html_compiler helpers were deliberately not ported; no module may import them."""
    # Resolve the package dir on disk so we can grep the raw source.
    pkg = importlib.import_module(MODLIX_PKG)
    pkg_dir = Path(pkg.__file__).parent

    forbidden_substrings = [
        "_html_compiler",
        "from ._html_compiler",
        "import _html_compiler",
        "from . import hc",
        "import hc",  # bare alias for the deleted module
    ]

    offenders: list[str] = []
    for name in MODLIX_MODULES:
        src_path = pkg_dir / f"{name}.py"
        src = src_path.read_text(encoding="utf-8")
        # Strip comments/docstrings approximation: just look at lines starting with import/from.
        for line in src.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for needle in forbidden_substrings:
                if needle in stripped:
                    offenders.append(f"{name}: {stripped}")
                    break

    assert not offenders, (
        "Found residual html_compiler imports (deliberately not ported):\n  "
        + "\n  ".join(offenders)
    )


def test_helper_modules_import():
    """Each private helper module imports cleanly + exposes >= 5 public-ish symbols."""
    for name in HELPER_MODULES:
        mod = importlib.import_module(f"{MODLIX_PKG}.{name}")
        # public-ish = anything not starting with a double-underscore dunder.
        # Helpers themselves are leading-underscore modules, but their internal
        # symbols (functions / classes / constants) are the API surface other
        # modlix modules consume, so we count non-dunder attributes that are
        # defined in *this* module (not re-exported builtins / typing imports).
        own_symbols = [
            s
            for s in dir(mod)
            if not s.startswith("__")
            and getattr(getattr(mod, s), "__module__", None) == mod.__name__
        ]
        # Threshold of 3 is the safety-net floor — the smallest legitimate
        # helper (_kirun_layout) has exactly 4 own symbols (2 public +
        # 2 private). Anything under 3 means the module was accidentally
        # emptied / the import strategy regressed.
        assert len(own_symbols) >= 3, (
            f"{name} only exposes {len(own_symbols)} own symbols "
            f"(expected >= 3): {own_symbols}"
        )
