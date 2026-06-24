"""parse_html goldens over real captured scrapes. Golden <site>.parse.expected.json
= blessed [{src,source}]; locks no-trunc, dedup, svg-drop, network-merge, order.
Bless: BLESS_FIXTURES=1 venv/bin/python -m unittest <this module>"""

from __future__ import annotations

import unittest

from tests.agents.adzump import fixtures


class ScrapeGoldenTests(unittest.TestCase):
    pass  # one test per fixture, added below


def _add_tests() -> None:
    for fx_path in fixtures.inputs():
        name = fixtures.name(fx_path)

        def test(self, fx_path=fx_path):
            page = fixtures.parsed(fx_path)
            got = [{"src": i.src, "source": i.source} for i in page.images]
            fixtures.check(self, got, fx_path, "parse")
            # invariants on any real page (also guards a bad bless)
            srcs = [i["src"] for i in got]
            self.assertEqual(len(srcs), len(set(srcs)), f"{name}: duplicate srcs leaked")
            self.assertFalse(
                any(s.split("?", 1)[0].lower().endswith(".svg") for s in srcs),
                f"{name}: an svg leaked into candidates",
            )

        setattr(ScrapeGoldenTests, f"test_{name}", test)


_add_tests()


if __name__ == "__main__":
    unittest.main()
