"""MetaAccountsAdapter.list_fb_pages - only offer pages the user can POST FROM.

Regression for the live Gremlin finding (gremlin-meta-fbonly-loop, 2026-06-24):
fetch_meta_fb_pages listed the business's CLIENT pages (no Page Access Token for
the connected user) → fetch_meta_ig_accounts 400 → loop. The page list must come
from /me/accounts (token-backed pages), scoped to the business, never the
untokenable client pages. Below the model - meta_client.get is mocked."""
import asyncio
import unittest
from unittest.mock import patch

from app.agents.adzump.adapters.meta import accounts as accounts_mod
from app.agents.adzump.adapters.meta.accounts import MetaAccountsAdapter


def _fake_graph(by_path: dict[str, list]):
    """Return an async stub for meta_client.get that matches the path by substring."""
    async def _get(path, **_kw):
        for key, data in by_path.items():
            if key in path:
                return {"data": data}
        return {"data": []}
    return _get


class ListFbPagesTests(unittest.TestCase):
    def _run(self, by_path):
        with patch.object(accounts_mod.meta_client, "get", side_effect=_fake_graph(by_path)):
            return asyncio.run(MetaAccountsAdapter().list_fb_pages("BIZ", "CC", {}))

    def test_excludes_untokenable_client_pages(self):
        # /me/accounts has only the owned page (has a token); the two client pages
        # have no token for this user → must NOT be offered (the live loop).
        pages = self._run({
            "/me/accounts": [{"id": "100", "name": "Modlix"}],
            "owned_pages": [{"id": "100"}],
            "client_pages": [{"id": "200"}, {"id": "300"}],
        })
        self.assertEqual([p["id"] for p in pages], ["100"])
        self.assertEqual(pages[0]["name"], "Modlix")

    def test_no_tokenable_pages_returns_empty(self):
        # User manages no page → [] (tool surfaces the "page you can post from" copy),
        # never the untokenable business pages.
        pages = self._run({
            "/me/accounts": [],
            "owned_pages": [{"id": "1"}],
            "client_pages": [{"id": "2"}],
        })
        self.assertEqual(pages, [])

    def test_falls_back_to_all_tokenable_when_no_business_overlap(self):
        # A token-backed page that isn't under this business is still usable -
        # don't over-filter to empty.
        pages = self._run({
            "/me/accounts": [{"id": "999", "name": "Other"}],
            "owned_pages": [{"id": "1"}],
            "client_pages": [{"id": "2"}],
        })
        self.assertEqual([p["id"] for p in pages], ["999"])


if __name__ == "__main__":
    unittest.main()
