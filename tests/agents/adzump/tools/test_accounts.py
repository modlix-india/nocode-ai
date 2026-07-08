"""accounts._remember_names - populates the launch cross-platform gate's
allow-list: account_names (id shown to user) + account_platforms (v3 F2:
launch refuses a cross-platform id).

Run:
    cd nocode-ai && ./venv/bin/python -m unittest tests.agents.adzump.tools.test_accounts -v
"""
from __future__ import annotations

import unittest

from app.agents.adzump.tools.accounts import _remember_names


class RememberNamesTests(unittest.TestCase):
    def test_populates_names_and_platforms(self):
        ctx = {"session_context": {}}
        _remember_names(ctx, [
            {"id": "act_123", "name": "Purva Realty Ads"},
            {"id": "act_456", "name": ""},       # blank name → "" (show id only)
            {"name": "no id here"},              # missing id key → skipped
        ], id_key="id", platform="meta")
        sc = ctx["session_context"]
        self.assertEqual(sc["account_names"],
                         {"act_123": "Purva Realty Ads", "act_456": ""})
        self.assertEqual(sc["account_platforms"],
                         {"act_123": "meta", "act_456": "meta"})

    def test_edges_are_noops(self):
        ctx = {"session_context": {}}
        _remember_names(ctx, [{"customer_id": "789", "name": "Sobha Google"}],
                        id_key="customer_id")           # no platform given
        self.assertNotIn("account_platforms", ctx["session_context"])
        empty: dict = {}
        _remember_names(empty, [{"id": "act_1"}], id_key="id", platform="meta")
        self.assertEqual(empty, {})                     # no session_context → untouched


if __name__ == "__main__":
    unittest.main()
