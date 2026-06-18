"""Lock #6 — `_remember_names` (tools/accounts.py), populates the launch
cross-platform gate's allow-list.

Every fetched account id must land in `account_names` (so set_campaign_spec can
confirm the id was shown to the user) and, when a platform is given, in
`account_platforms` (so the launch boundary refuses a cross-platform id — v3 F2).
Pure dict-mutation seam; lock the shape it writes.

Run:
    cd nocode-ai && ./venv/bin/python -m unittest \\
        tests.agents.adzump.test_accounts_remember -v
"""

from __future__ import annotations

import unittest

from app.agents.adzump.tools.accounts import _remember_names


def _ctx() -> dict:
    return {"session_context": {}}


class RememberNamesLock(unittest.TestCase):

    def test_populates_names_and_platforms(self):
        ctx = _ctx()
        # Meta ad accounts for a real-estate advertiser.
        accounts = [
            {"id": "act_123", "name": "Purva Realty Ads"},
            {"id": "act_456", "name": ""},          # blank name → "" (show id only)
        ]
        _remember_names(ctx, accounts, id_key="id", platform="meta")
        sc = ctx["session_context"]
        self.assertEqual(sc["account_names"], {"act_123": "Purva Realty Ads", "act_456": ""})
        self.assertEqual(sc["account_platforms"], {"act_123": "meta", "act_456": "meta"})

    def test_no_platform_skips_platform_map(self):
        ctx = _ctx()
        _remember_names(ctx, [{"customer_id": "789", "name": "Sobha Google"}],
                        id_key="customer_id")
        sc = ctx["session_context"]
        self.assertEqual(sc["account_names"], {"789": "Sobha Google"})
        self.assertNotIn("account_platforms", sc)   # only created when platform given

    def test_missing_id_key_is_skipped(self):
        ctx = _ctx()
        _remember_names(ctx, [{"name": "no id here"}, {"id": "act_1", "name": "ok"}],
                        id_key="id", platform="meta")
        self.assertEqual(ctx["session_context"]["account_names"], {"act_1": "ok"})

    def test_no_session_context_is_noop(self):
        ctx: dict = {}                       # no session_context
        _remember_names(ctx, [{"id": "act_1"}], id_key="id", platform="meta")
        self.assertEqual(ctx, {})            # untouched, no crash


if __name__ == "__main__":
    unittest.main()
