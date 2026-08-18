"""Unit tests for the audience_targeting orchestrator
(app/agents/adzump/agents/campaign/tools/google/audience_targeting.py) and the panel block
it emits (craft.audience_review_block).

Covers the seams a wrong campaign would slip through: the channel gate, the idempotency key
that decides whether the sub-agent runs again, and the breadcrumb/label rendering the user
reads before approving.
"""

from __future__ import annotations

import asyncio
import unittest
from unittest import mock

from app.agents.adzump.agents.campaign.craft import audience_review_block
from app.agents.adzump.agents.campaign.google.audience.models import (
    AudienceSignal,
    AudienceTargetingResult,
    DemographicSpec,
    SignalKind,
    SignalSource,
)
from app.agents.adzump.agents.campaign.models import (
    audience,
    build_dump,
    is_build_complete,
    keyword_research,
    set_audience,
    set_build,
    set_keyword_research,
)
from app.agents.adzump.agents.campaign.tools.google import audience_targeting as at


def _signal(label, kind=SignalKind.IN_MARKET, ref=None, **kw):
    return AudienceSignal(
        kind=kind,
        ref=ref or f"customers/1/userInterests/{abs(hash(label)) % 10000}",
        label=label,
        source=SignalSource.TAXONOMY,
        **kw,
    )


def _result(*signals, demographics=None):
    return AudienceTargetingResult(
        signals=list(signals),
        demographics=demographics or DemographicSpec(),
        dimension_groups=[[s.ref for s in signals if not s.negative]]
        if signals
        else [],
    )


class _Harness:
    """Drives _audience_targeting with stubbed IO, capturing what the panel receives."""

    def __init__(self, channel="Demand Gen"):
        self.emits: list[tuple[str, list]] = []
        self.suggest_calls = 0
        self.suggest_kwargs: dict = {}
        self.session_ctx = {
            "campaign_spec": {
                "platform": "GOOGLE",
                "account": "1",
                "channel": channel,
            },
            "product_data": {
                "product_name": "P",
                "business_type": "T",
                "place": {"country_code": "IN"},
            },
        }

    def run(self, result):
        async def suggest(**kw):
            self.suggest_calls += 1
            self.suggest_kwargs = kw
            if isinstance(result, BaseException):
                raise result
            return result

        async def cap_craft(stream, cid, sctx):
            block = audience_review_block(audience(sctx) or {})
            self.emits.append(("full", [s["key"] for s in block["sections"]]))

        async def cap_section(stream, cid, block):
            self.emits.append(("section", [s["key"] for s in block["sections"]]))

        ctx = {
            "session_context": self.session_ctx,
            "auth": mock.MagicMock(),
            "event_stream": mock.AsyncMock(),
        }
        with (
            mock.patch.object(at, "emit_campaign_craft", new=cap_craft),
            mock.patch.object(at, "emit_section_update", new=cap_section),
            mock.patch.object(at, "get_audience_agent") as agent,
        ):
            agent.return_value.suggest = suggest
            return asyncio.run(at._audience_targeting({}, ctx))


class ChannelGateTests(unittest.TestCase):
    """A Search campaign has no audience step; skipping is a success, not a failure —
    the orchestrator offers every build tool and each one decides whether it applies."""

    def test_search_skips_without_running_the_agent(self):
        h = _Harness(channel="Search")
        res = h.run(_result(_signal("A")))
        self.assertTrue(res.success)
        self.assertTrue(res.data["skipped"])
        self.assertEqual(h.suggest_calls, 0)
        self.assertIsNone(audience(h.session_ctx))

    def test_an_unset_channel_is_search(self):
        h = _Harness(channel="")
        res = h.run(_result(_signal("A")))
        self.assertEqual(res.data["skipped"], True)

    def test_demand_gen_runs_and_persists(self):
        h = _Harness()
        res = h.run(_result(_signal("A"), _signal("B")))
        self.assertTrue(res.success)
        self.assertEqual(h.suggest_calls, 1)
        stored = audience(h.session_ctx)
        self.assertEqual(len(stored["signals"]), 2)
        self.assertEqual(h.emits, [("full", ["in_market", "demographics"])])


class PersistenceTests(unittest.TestCase):
    def test_an_empty_audience_is_not_stored(self):
        # Grouped mode has no untargeted fallback, so an empty result is a failed run
        # rather than a campaign that reaches everyone.
        h = _Harness()
        res = h.run(_result())
        self.assertFalse(res.success)
        self.assertIsNone(audience(h.session_ctx))
        self.assertEqual(h.emits, [])

    def test_a_failed_run_reports_rather_than_raises(self):
        h = _Harness()
        res = h.run(RuntimeError("upstream 500"))
        self.assertFalse(res.success)
        self.assertIsNone(audience(h.session_ctx))

    def test_a_cancelled_run_stays_cancelled(self):
        # Reporting cancellation as a retryable failure would let the loop continue after
        # the client hung up.
        h = _Harness()
        with self.assertRaises(asyncio.CancelledError):
            h.run(asyncio.CancelledError())

    def test_same_inputs_reuse_the_saved_set(self):
        h = _Harness()
        h.run(_result(_signal("A")))
        h.emits.clear()
        res = h.run(_result(_signal("B")))
        self.assertTrue(res.success)
        self.assertEqual(h.suggest_calls, 1)  # not run again
        self.assertEqual(
            [s["label"] for s in audience(h.session_ctx)["signals"]], ["A"]
        )
        self.assertEqual(h.emits, [("section", ["in_market", "demographics"])])

    def test_a_changed_business_rebuilds(self):
        h = _Harness()
        h.run(_result(_signal("A")))
        h.session_ctx["product_data"]["product_name"] = "Q"
        h.emits.clear()
        res = h.run(_result(_signal("B")))
        self.assertTrue(res.success)
        self.assertEqual(h.suggest_calls, 2)
        self.assertEqual(
            [s["label"] for s in audience(h.session_ctx)["signals"]], ["B"]
        )
        # the panel is already on screen, so the block is replaced rather than repainted
        self.assertEqual(h.emits, [("section", ["in_market", "demographics"])])

    def test_a_map_pinned_place_still_resolves_its_country(self):
        """A place confirmed by map pin carries coordinates and no country code.

        resolve_coordinates returns early on cached coords and only stamps the country on a
        fresh geocode, so reusing it here left the country empty — silently dropping every
        country-scoped segment from the catalogue.
        """
        h = _Harness()
        h.session_ctx["product_data"]["place"] = {
            "address": "Bangalore, India",
            "lat": 12.9,
            "lng": 77.5,
        }
        with mock.patch.object(
            at,
            "resolve_country_code",
            new=mock.AsyncMock(return_value="IN"),
        ) as resolver:
            res = h.run(_result(_signal("A")))
        self.assertTrue(res.success)
        self.assertEqual(resolver.await_count, 1)
        # and it reaches the run, which is what decides whether a segment can serve
        self.assertEqual(h.suggest_kwargs["country_code"], "IN")

    def test_a_changed_country_rebuilds(self):
        # Segment availability is country-scoped: the same segments are not necessarily
        # targetable elsewhere.
        h = _Harness()
        h.run(_result(_signal("A")))
        h.session_ctx["product_data"]["place"]["country_code"] = "GB"
        res = h.run(_result(_signal("B")))
        self.assertTrue(res.success)
        self.assertEqual(h.suggest_calls, 2)


class BuildHandoffTests(unittest.TestCase):
    """The sub-session -> main-session hop. Build tools run in a throwaway session; what
    survives is what ``create()`` hands back, and it must not be one channel's slot — the
    main session is where launch, next_action and the manage agent all read from.
    """

    def _hop(self, sub_ctx):
        main_ctx = {}
        set_build(main_ctx, build_dump(sub_ctx))
        return main_ctx

    def test_a_demand_gen_build_survives_the_hop(self):
        sub = {}
        set_audience(sub, {"signals": [{"label": "A"}]})
        main = self._hop(sub)
        self.assertEqual(audience(main), {"signals": [{"label": "A"}]})
        self.assertTrue(is_build_complete(main))

    def test_a_search_build_survives_the_hop(self):
        sub = {}
        set_keyword_research(sub, {"themes": {"brand": {}}})
        main = self._hop(sub)
        self.assertEqual(keyword_research(main), {"themes": {"brand": {}}})
        self.assertTrue(is_build_complete(main))

    def test_a_channel_that_produced_nothing_hands_back_nothing(self):
        self.assertIsNone(build_dump({}))


class ReviewBlockTests(unittest.TestCase):
    def test_sections_group_by_kind_and_carry_breadcrumbs(self):
        dump = _result(
            _signal(
                "Apartments (For Sale)",
                path=["Real Estate", "Residential Properties", "Apartments (For Sale)"],
                rationale="ready to buy",
            ),
            _signal("Cooking Enthusiasts", kind=SignalKind.AFFINITY, path=["Food"]),
        ).model_dump(mode="json")
        sections = {s["key"]: s for s in audience_review_block(dump)["sections"]}
        self.assertEqual(sections["in_market"]["label"], "In-Market (1)")
        self.assertEqual(
            sections["in_market"]["rows"][0]["category"],
            "Real Estate > Residential Properties",
        )
        # a root-level entry has no ancestors, not a breadcrumb of itself
        self.assertEqual(sections["affinity"]["rows"][0]["category"], "")

    def test_every_dimension_shows_and_an_unset_one_reads_as_everyone(self):
        # Omitting the unset ones hid that the filter exists: the user could not tell
        # "not narrowed" from "not offered", and only ever saw the one that was set.
        dump = _result(
            _signal("A"),
            demographics=DemographicSpec(genders=["FEMALE"]),
        ).model_dump(mode="json")
        rows = next(
            s
            for s in audience_review_block(dump)["sections"]
            if s["key"] == "demographics"
        )["rows"]
        self.assertEqual(
            [(r["attribute"], r["value"]) for r in rows],
            [
                ("Age", "Everyone"),
                ("Gender", "Female"),
                ("Household income", "Everyone"),
                ("Parental status", "Everyone"),
            ],
        )

    def test_each_dimension_carries_the_reason_it_was_set_that_way(self):
        # "Everyone" with no reason reads as a step nobody took. The agent has to say why it
        # left one open, and a panel edit must not blank that.
        dump = _result(
            _signal("A"),
            demographics=DemographicSpec(
                genders=["FEMALE"],
                rationales={"age_ranges": "anyone who can afford it buys it"},
            ),
        ).model_dump(mode="json")
        rows = next(
            s
            for s in audience_review_block(dump)["sections"]
            if s["key"] == "demographics"
        )["rows"]
        by_attr = {r["attribute"]: r["rationale"] for r in rows}
        self.assertEqual(by_attr["Age"], "anyone who can afford it buys it")
        self.assertEqual(by_attr["Gender"], "")

    def test_income_shows_the_percentile_not_the_api_value(self):
        dump = _result(
            _signal("A"),
            demographics=DemographicSpec(income_ranges=["INCOME_RANGE_90_UP"]),
        ).model_dump(mode="json")
        rows = next(
            s
            for s in audience_review_block(dump)["sections"]
            if s["key"] == "demographics"
        )["rows"]
        income = next(r for r in rows if r["attribute"] == "Household income")
        self.assertEqual(income["value"], "Top 10%")

    def test_an_open_ended_age_range_reads_as_a_floor(self):
        dump = _result(
            _signal("A"),
            demographics=DemographicSpec(age_ranges=[{"min_age": 65}]),
        ).model_dump(mode="json")
        rows = next(
            s
            for s in audience_review_block(dump)["sections"]
            if s["key"] == "demographics"
        )["rows"]
        self.assertEqual(rows[0]["value"], "65+")

    def test_demographics_carries_the_raw_spec_the_editor_seeds_from(self):
        # set_demographics REPLACES, so the editor has to open on what is already saved.
        # The rows are prose ("25-44", "Top 10%") and cannot be parsed back into a spec -
        # without this key the panel opens blank and a gender edit wipes age and income.
        dump = _result(
            _signal("A"),
            demographics=DemographicSpec(
                age_ranges=[{"min_age": 25, "max_age": 44}],
                genders=["FEMALE"],
                income_ranges=["INCOME_RANGE_90_UP"],
            ),
        ).model_dump(mode="json")
        section = next(
            s
            for s in audience_review_block(dump)["sections"]
            if s["key"] == "demographics"
        )
        self.assertEqual(section["values"], dump["demographics"])

    def test_rows_carry_the_ref_the_panel_deletes_by(self):
        # A label is not an identity: the mutation matches on resource name or bare id, so a
        # row without `ref` makes every panel delete fail.
        dump = _result(_signal("A", ref="customers/1/userInterests/1")).model_dump(
            mode="json"
        )
        row = audience_review_block(dump)["sections"][0]["rows"][0]
        self.assertEqual(row["ref"], "customers/1/userInterests/1")

    def test_exclusions_get_their_own_section(self):
        dump = _result(
            _signal("A"),
            _signal(
                "Existing customers",
                kind=SignalKind.USER_LIST,
                ref="customers/1/userLists/9",
                negative=True,
            ),
        ).model_dump(mode="json")
        sections = {s["key"]: s for s in audience_review_block(dump)["sections"]}
        self.assertEqual(sections["excluded"]["label"], "Excluded (1)")
        # the excluded list must not also appear as something the campaign targets
        self.assertNotIn("user_list", sections)


if __name__ == "__main__":
    unittest.main()
