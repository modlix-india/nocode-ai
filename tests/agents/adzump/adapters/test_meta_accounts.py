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

    def test_rows(self):
        for label, graph, ids in [
            # the live loop: two client pages have no token for this user
            ("untokenable client pages are never offered",
             {"/me/accounts": [{"id": "100", "name": "Modlix"}], "owned_pages": [{"id": "100"}],
              "client_pages": [{"id": "200"}, {"id": "300"}]}, ["100"]),
            # the tool then shows the "page you can post from" copy
            ("no tokenable page: nothing offered",
             {"/me/accounts": [], "owned_pages": [{"id": "1"}], "client_pages": [{"id": "2"}]}, []),
            # a token-backed page outside this business is still usable
            ("no overlap with the business: every tokenable page",
             {"/me/accounts": [{"id": "999", "name": "Other"}], "owned_pages": [{"id": "1"}],
              "client_pages": [{"id": "2"}]}, ["999"]),
        ]:
            with self.subTest(label):
                self.assertEqual([page["id"] for page in self._run(graph)], ids)


if __name__ == "__main__":
    unittest.main()
