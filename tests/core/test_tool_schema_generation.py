"""tool_params_from_model - pydantic model → LLM tool schema, honestly.

Regression for PR #91 S3: the generator silently dropped enum/items/
properties (a Literal reached the LLM as a bare string) and defaulted an
unresolvable type to "string". The model is the single source of truth
only if everything it declares actually reaches the schema.
"""
from __future__ import annotations

import unittest
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.core.tools.base import tool_params_from_model


# Optional[...] rather than `X | None`: pydantic EVALUATES these annotations
# at class-creation time, and `X | None` on typing constructs / model classes
# needs Python 3.10+ — on 3.9 (the local venv) the whole module failed to
# collect. Optional[] produces the identical anyOf-with-null schema.
class _Params(BaseModel):
    name: str = Field(description="required plain string")
    kind: Optional[Literal["a", "b"]] = Field(None, description="optional enum")
    tags: list[str] = Field(default_factory=list, description="string list")
    count: Optional[int] = None


class _Unresolvable(BaseModel):
    nested: Optional["_Params"] = None  # $ref inside anyOf - no plain type


class ToolParamsFromModelTests(unittest.TestCase):
    def setUp(self):
        self.params = {p.name: p for p in tool_params_from_model(_Params)}

    def test_types_and_required(self):
        self.assertEqual(self.params["name"].type, "string")
        self.assertTrue(self.params["name"].required)
        self.assertEqual(self.params["count"].type, "integer")  # anyOf flattened
        self.assertFalse(self.params["count"].required)

    def test_literal_enum_passes_through(self):
        self.assertEqual(self.params["kind"].enum, ["a", "b"])
        self.assertEqual(self.params["kind"].type, "string")
        self.assertEqual(self.params["kind"].description, "optional enum")

    def test_list_items_pass_through(self):
        self.assertEqual(self.params["tags"].type, "array")
        self.assertEqual(self.params["tags"].items, {"type": "string"})

    def test_unresolvable_type_raises_instead_of_lying(self):
        with self.assertRaises(ValueError) as caught:
            tool_params_from_model(_Unresolvable)
        self.assertIn("nested", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
