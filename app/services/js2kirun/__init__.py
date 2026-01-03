"""
JavaScript to KIRun Converter (Bidirectional)

This library provides bidirectional conversion between JavaScript code
and KIRun function definitions used in the nocode UI system.

- JS2KIRunConverter: Converts JavaScript code to KIRun function definitions
- KIRun2JSConverter: Converts KIRun function definitions back to JavaScript

Key Assumption: JavaScript code operates on Page/Store/Url/Parent objects
through component bindings. There are no local variables - all state is
managed through these store objects.
"""

from .converter import JS2KIRunConverter
from .kirun2js import KIRun2JSConverter
from .step_matcher import (
    preserve_step_names,
    extract_step_names_from_js,
    extract_step_name_from_comment,
    remap_step_names,
)
from .types import (
    KIRunFunctionDefinition,
    KIRunStatement,
    KIRunParameter,
    KIRunEvent,
    ParameterReference,
    ConversionOptions,
    ConversionContext,
    ConvertedResult,
)

__all__ = [
    "JS2KIRunConverter",
    "KIRun2JSConverter",
    "preserve_step_names",
    "extract_step_names_from_js",
    "extract_step_name_from_comment",
    "remap_step_names",
    "KIRunFunctionDefinition",
    "KIRunStatement",
    "KIRunParameter",
    "KIRunEvent",
    "ParameterReference",
    "ConversionOptions",
    "ConversionContext",
    "ConvertedResult",
]

__version__ = "1.1.0"

