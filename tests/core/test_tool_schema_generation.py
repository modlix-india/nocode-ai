"""tool_params_from_model - pydantic model → LLM tool schema, honestly.

Regression for PR #91 S3: the generator silently dropped enum/items/
properties (a Literal reached the LLM as a bare string) and defaulted an
unresolvable type to "string". The model is the single source of truth
only if everything it declares actually reaches the schema.
"""
from __future__ import annotations

import unittest
from typing import Literal

from pydantic import BaseModel, Field

from app.core.tools.base import tool_params_from_model


class _Params(BaseModel):
    name: str = Field(description="required plain string")
    kind: Literal["a", "b"] | None = Field(None, description="optional enum")
    tags: list[str] = Field(default_factory=list, description="string list")
    count: int | None = None


class _Unresolvable(BaseModel):
    nested: "_Params | None" = None  # $ref inside anyOf - no plain type


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
