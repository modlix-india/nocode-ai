"""The Python reading of the page-routing resolver, against the same cases the
TypeScript one is tested on.

The contract lives in `nocode-ui/ui-app/client/src/util/pageRouting.ts` and its
jest suite; this file is deliberately a walk of the same ground, so a change
there that is not mirrored here shows up as a failure rather than as a
simulation that confidently reports the wrong page.
"""

from __future__ import annotations

import pytest

from app.agents.appbuilder.tools.modlix._page_route_eval import (
    consent_trap_reasons,
    explain,
    numeric,
    validate_condition,
)


def req(**kwargs):
    return {"pageName": "pricing", **kwargs}


def pinned(value: float):
    return lambda: value


CAMPAIGN = {
    "pricing": {
        "rules": {
            "r1": {
                "order": 0,
                "type": "PERSONALIZATION",
                "page": "pricing_dentists",
                "conditions": {
                    "c": {"source": "QUERY", "field": "utm_campaign",
                          "operator": "EQUALS", "value": "dentists"},
                },
            },
        },
    },
}


class TestTheRequestedName:
    def test_no_routing_returns_the_requested_page(self):
        assert explain(None, None, req()).page_name == "pricing"
        assert explain({}, None, req()).page_name == "pricing"

    def test_default_page_substitutes_for_empty_and_index(self):
        for name in ("", "index", None):
            assert explain({}, "home", req(pageName=name)).page_name == "home"

    def test_no_default_leaves_the_name_alone(self):
        assert explain({}, None, req(pageName="")).page_name == ""


class TestRoutingOutranksTheName:
    def test_a_matching_rule_replaces_the_page(self):
        out = explain(CAMPAIGN, None, req(query={"utm_campaign": "dentists"}))
        assert out.page_name == "pricing_dentists"
        assert out.rule_key == "r1"

    def test_a_route_can_name_a_page_that_does_not_exist(self):
        # The whole point: `summer-offer` is a campaign address, not a page.
        routing = {
            "summer-offer": {
                "rules": {
                    "r": {"type": "PERSONALIZATION", "page": "landing_summer",
                          "conditions": {"c": {"source": "DEVICE", "operator": "EXISTS"}}},
                },
            },
        }
        out = explain(routing, None, req(pageName="summer-offer", device="MOBILE"))
        assert out.page_name == "landing_summer"

    def test_resolution_is_a_single_hop(self):
        routing = {
            "a": {"rules": {"r": {"type": "PERSONALIZATION", "page": "b",
                                  "conditions": {"c": {"source": "DEVICE", "operator": "EXISTS"}}}}},
            "b": {"rules": {"r": {"type": "PERSONALIZATION", "page": "c",
                                  "conditions": {"c": {"source": "DEVICE", "operator": "EXISTS"}}}}},
        }
        assert explain(routing, None, req(pageName="a", device="MOBILE")).page_name == "b"

    def test_a_route_on_the_default_page_still_applies(self):
        routing = {
            "home": {"rules": {"r": {"type": "PERSONALIZATION", "page": "home_member",
                                     "conditions": {"c": {"source": "AUTH", "operator": "EQUALS",
                                                          "value": "true"}}}}},
        }
        assert explain(routing, "home", req(pageName="", authenticated=True)).page_name == "home_member"

    def test_a_disabled_route_decides_nothing(self):
        routing = {"pricing": {"enabled": False, **CAMPAIGN["pricing"]}}
        assert explain(routing, None, req(query={"utm_campaign": "dentists"})).page_name == "pricing"


class TestConditions:
    @pytest.mark.parametrize(
        "operator,value,actual,expected",
        [
            ("EQUALS", "dentists", "dentists", True),
            ("EQUALS", "dentists", "clinics", False),
            ("EQUALS", "DENTISTS", "dentists", True),          # folds case by default
            ("NOT_EQUALS", "dentists", "clinics", True),
            ("CONTAINS", "dent", "dentists", True),
            ("NOT_CONTAINS", "dent", "clinics", True),
            ("STARTS_WITH", "dent", "dentists", True),
            ("ENDS_WITH", "ists", "dentists", True),
            ("MATCHES", "^dent", "dentists", True),
            ("MATCHES", "[", "dentists", False),               # unparseable never matches
            ("IN", "dentists, clinics", "clinics", True),
            ("NOT_IN", "dentists, clinics", "vets", True),
            ("EXISTS", None, "anything", True),
        ],
    )
    def test_operators(self, operator, value, actual, expected):
        cond = {"source": "QUERY", "field": "c", "operator": operator}
        if value is not None:
            cond["value"] = value
        routing = {"pricing": {"rules": {"r": {"type": "PERSONALIZATION", "page": "hit",
                                               "conditions": {"c": cond}}}}}
        out = explain(routing, None, req(query={"c": actual}))
        assert (out.page_name == "hit") is expected

    def test_a_missing_value_satisfies_only_the_negative_operators(self):
        for operator, expected in [("EQUALS", False), ("NOT_EQUALS", True),
                                   ("CONTAINS", False), ("NOT_CONTAINS", True),
                                   ("IN", False), ("NOT_IN", True),
                                   ("EXISTS", False), ("NOT_EXISTS", True)]:
            routing = {"pricing": {"rules": {"r": {
                "type": "PERSONALIZATION", "page": "hit",
                "conditions": {"c": {"source": "QUERY", "field": "utm_campaign",
                                     "operator": operator, "value": "dentists"}}}}}}
            out = explain(routing, None, req(query={}))
            assert (out.page_name == "hit") is expected, operator

    def test_case_sensitive_when_asked(self):
        routing = {"pricing": {"rules": {"r": {
            "type": "PERSONALIZATION", "page": "hit",
            "conditions": {"c": {"source": "QUERY", "field": "c", "operator": "EQUALS",
                                 "value": "Dentists", "caseSensitive": True}}}}}}
        assert explain(routing, None, req(query={"c": "dentists"})).page_name == "pricing"
        assert explain(routing, None, req(query={"c": "Dentists"})).page_name == "hit"

    def test_header_field_is_lower_cased_to_match(self):
        routing = {"pricing": {"rules": {"r": {
            "type": "PERSONALIZATION", "page": "hit",
            "conditions": {"c": {"source": "HEADER", "field": "Referer",
                                 "operator": "CONTAINS", "value": "google"}}}}}}
        assert explain(routing, None, req(headers={"referer": "https://google.com/"})).page_name == "hit"

    def test_all_versus_any(self):
        conditions = {
            "a": {"source": "QUERY", "field": "utm_campaign", "operator": "EQUALS", "value": "dentists"},
            "b": {"source": "DEVICE", "operator": "EQUALS", "value": "MOBILE"},
        }
        for mode, expected in [("ALL", False), ("ANY", True)]:
            routing = {"pricing": {"rules": {"r": {
                "type": "PERSONALIZATION", "page": "hit",
                "conditionMatch": mode, "conditions": conditions}}}}
            out = explain(routing, None, req(query={"utm_campaign": "dentists"}, device="DESKTOP"))
            assert (out.page_name == "hit") is expected, mode

    def test_a_targeting_rule_with_no_conditions_never_matches(self):
        routing = {"pricing": {"rules": {"r": {"type": "PERSONALIZATION", "page": "hit"}}}}
        out = explain(routing, None, req())
        assert out.page_name == "pricing"
        assert any("never matches" in line for line in out.trace)

    def test_auth_undetermined_is_not_false(self):
        routing = {"pricing": {"rules": {"r": {
            "type": "PERSONALIZATION", "page": "hit",
            "conditions": {"c": {"source": "AUTH", "operator": "EXISTS"}}}}}}
        assert explain(routing, None, req()).page_name == "pricing"
        assert explain(routing, None, req(authenticated=False)).page_name == "hit"


def split(variants, **extra):
    return {"pricing": {"rules": {"exp1": {"type": "SPLIT", "variants": variants, **extra}}}}


THREE_WAY = {
    "a": {"page": "pricing", "weight": 50, "order": 0},
    "b": {"page": "pricing_b", "weight": 30, "order": 1},
    "c": {"page": "pricing_c", "weight": 20, "order": 2},
}


class TestSplits:
    @pytest.mark.parametrize("point,expected", [
        (0.0, "pricing"), (0.49, "pricing"), (0.5, "pricing_b"),
        (0.79, "pricing_b"), (0.8, "pricing_c"), (0.999, "pricing_c"),
    ])
    def test_draws_by_weight_across_any_number_of_arms(self, point, expected):
        assert explain(split(THREE_WAY), None, req(), pinned(point)).page_name == expected

    def test_reports_the_arm_so_the_caller_can_store_it(self):
        out = explain(split(THREE_WAY), None, req(), pinned(0.6))
        assert out.variant_key == "b"
        assert out.new_assignment == ("exp1", "b")

    def test_a_stored_arm_is_reused_and_writes_nothing(self):
        out = explain(split(THREE_WAY), None, req(assignments={"exp1": "c"}), pinned(0.0))
        assert out.page_name == "pricing_c"
        assert out.new_assignment is None

    def test_a_deleted_arm_is_redrawn(self):
        out = explain(split(THREE_WAY), None, req(assignments={"exp1": "gone"}), pinned(0.0))
        assert out.page_name == "pricing"
        assert out.new_assignment == ("exp1", "a")

    def test_missing_weight_counts_as_one_and_zero_drops_out(self):
        routing = split({"a": {"page": "page_a", "order": 0}, "b": {"page": "page_b", "order": 1}})
        assert explain(routing, None, req(), pinned(0.4)).page_name == "page_a"
        assert explain(routing, None, req(), pinned(0.6)).page_name == "page_b"

        zeroed = split({"a": {"page": "page_a", "weight": 0, "order": 0},
                        "b": {"page": "page_b", "weight": 5, "order": 1}})
        assert explain(zeroed, None, req(), pinned(0.0)).page_name == "page_b"

    # A text input writes a string, and several writers reach this document.
    def test_string_weights_are_summed_as_numbers(self):
        routing = split({"a": {"page": "page_a", "weight": "2", "order": 0},
                         "b": {"page": "page_b", "weight": "1", "order": 1}})
        assert explain(routing, None, req(), pinned(0.0)).page_name == "page_a"
        assert explain(routing, None, req(), pinned(0.66)).page_name == "page_a"
        assert explain(routing, None, req(), pinned(0.67)).page_name == "page_b"

    def test_string_orders_sort_numerically(self):
        routing = split({"a": {"page": "page_tenth", "weight": 1, "order": "10"},
                         "b": {"page": "page_ninth", "weight": 1, "order": "9"}})
        assert explain(routing, None, req(), pinned(0.0)).page_name == "page_ninth"

    def test_a_split_with_no_conditions_includes_everyone(self):
        assert explain(split(THREE_WAY), None, req(), pinned(0.0)).page_name == "pricing"

    def test_conditions_narrow_who_is_in_the_test(self):
        routing = split(THREE_WAY, conditions={
            "c": {"source": "DEVICE", "operator": "EQUALS", "value": "MOBILE"}})
        assert explain(routing, None, req(device="DESKTOP"), pinned(0.6)).page_name == "pricing"
        assert explain(routing, None, req(device="MOBILE"), pinned(0.6)).page_name == "pricing_b"

    def test_a_split_that_cannot_serve_falls_through_rather_than_shadowing(self):
        routing = {"pricing": {"rules": {
            "exp1": {"order": 0, "type": "SPLIT", "variants": {}},
            "r2": {"order": 1, "type": "PERSONALIZATION", "page": "pricing_in",
                   "conditions": {"c": {"source": "GEO", "operator": "EQUALS", "value": "IN"}}},
        }}}
        assert explain(routing, None, req(country="IN")).page_name == "pricing_in"

    def test_a_disabled_rule_is_skipped(self):
        routing = split(THREE_WAY)
        routing["pricing"]["rules"]["exp1"]["enabled"] = False
        assert explain(routing, None, req(), pinned(0.0)).page_name == "pricing"


class TestEveryoneIsDrawn:
    """Consent used to gate the draw. It does not any more.

    The old rule was: drawing means storing the assignment, so a visitor who
    refused cookies was served a flagged arm without a draw. The effect was that
    a site with no working consent banner -- most of them -- ran no test at all
    and never once rendered the second page. Kiran's call 2026-09-20: the split
    runs for everyone; `modlix_page_variant` carries no identifier, and
    measurement stays gated separately.
    """

    def test_a_refusal_no_longer_changes_the_draw(self):
        routing = split({
            "a": {"page": "pricing_a", "weight": 1, "order": 0},
            "b": {"page": "pricing_b", "weight": 1, "order": 1},
        })
        assert explain(routing, None, req(consentGranted=False), pinned(0.1)).page_name == "pricing_a"
        assert explain(routing, None, req(consentGranted=False), pinned(0.9)).page_name == "pricing_b"

    def test_the_assignment_is_recorded_so_the_arm_survives_the_next_click(self):
        out = explain(split(THREE_WAY), None, req(consentGranted=False), pinned(0.9))
        assert out.new_assignment is not None

    def test_a_stored_arm_is_honoured_without_a_redraw(self):
        out = explain(split(THREE_WAY), None, req(assignments={"exp1": "c"}), pinned(0.0))
        assert out.page_name == "pricing_c"
        assert out.new_assignment is None

    def test_consent_is_not_read_at_all(self):
        # Whatever the caller says, the answer is the draw.
        for answer in (True, False, None):
            out = explain(split(THREE_WAY), None, req(consentGranted=answer), pinned(0.0))
            assert out.page_name == "pricing"
            assert out.new_assignment is not None


class TestNumeric:
    @pytest.mark.parametrize("value,expected", [
        (3, 3.0), (2.5, 2.5), ("4", 4.0), (" 4 ", 4.0),
        ("lots", 1.0), ("", 1.0), (None, 1.0), (True, 1.0), ([], 1.0),
    ])
    def test_coercion(self, value, expected):
        assert numeric(value, 1) == expected


class TestValidation:
    def test_accepts_a_well_formed_condition(self):
        assert validate_condition(
            {"source": "QUERY", "field": "utm_campaign", "operator": "EQUALS", "value": "x"}
        ) is None

    def test_rejects_a_source_the_resolver_does_not_implement(self):
        why = validate_condition({"source": "REFERRER", "operator": "EQUALS", "value": "x"})
        assert why and "source must be one of" in why

    def test_rejects_a_query_condition_with_no_field(self):
        why = validate_condition({"source": "QUERY", "operator": "EQUALS", "value": "x"})
        assert why and "needs `field`" in why

    def test_rejects_an_operator_that_needs_a_value_and_has_none(self):
        why = validate_condition({"source": "DEVICE", "operator": "EQUALS"})
        assert why and "needs a `value`" in why

    def test_allows_exists_without_a_value(self):
        assert validate_condition({"source": "DEVICE", "operator": "EXISTS"}) is None

    def test_rejects_a_pattern_that_does_not_compile(self):
        why = validate_condition({"source": "QUERY", "field": "c", "operator": "MATCHES", "value": "["})
        assert why and "never matches" in why


class TestTheTrace:
    def test_says_which_rule_decided(self):
        out = explain(CAMPAIGN, None, req(query={"utm_campaign": "dentists"}))
        assert any("APPLIES" in line for line in out.trace)

    def test_says_why_a_rule_was_skipped_and_what_the_request_actually_held(self):
        out = explain(CAMPAIGN, None, req(query={"utm_campaign": "clinics"}))
        joined = "\n".join(out.trace)
        assert "SKIPPED" in joined
        assert "clinics" in joined

    def test_says_when_there_are_no_rules_at_all(self):
        out = explain({}, None, req())
        assert any("no rules are attached" in line for line in out.trace)


class TestTheLiveCrumbcoShape:
    """The split that never once showed its second page.

    crumbco ran 50/50 between `home` and `homeTwo` and served `home` to
    everybody, because `analytics.consentRequired` defaults to REQUIRED, the
    site had no working consent surface, and a withheld visitor was never drawn.
    The weights were stored as STRINGS too, which is a separate trap.
    """

    ROUTING = {
        "home": {"rules": {"r": {"type": "SPLIT", "variants": {
            "a": {"page": "home", "weight": "50", "order": 0},
            "b": {"page": "homeTwo", "weight": "50", "order": 1},
        }}}}
    }

    def test_it_draws_for_a_visitor_with_no_consent_record_at_all(self):
        request = {"pageName": "home"}
        assert explain(self.ROUTING, "home", dict(request), pinned(0.1)).page_name == "home"
        assert explain(self.ROUTING, "home", dict(request), pinned(0.9)).page_name == "homeTwo"

    def test_string_weights_still_split_evenly(self):
        # '0' + '50' + '50' would be '05050' in JavaScript, drawing from 5050.
        seen = {explain(self.ROUTING, "home", {"pageName": "home"}, pinned(p)).page_name
                for p in (0.0, 0.49, 0.51, 0.99)}
        assert seen == {"home", "homeTwo"}


class TestTheConsentTrap:
    """A routing rule must not be used as a cookie banner.

    Seen live on `crumbco`: a rule at order 0 sent everyone without a consent
    cookie to the consent page, which REPLACED home rather than overlaying it.
    Its buttons carried the event NAME where `onClick` needs the event KEY, so
    nothing could set the cookie and no visitor ever reached the site.
    """

    def test_the_rule_does_exactly_what_it_says(self):
        routing = {
            "home": {"rules": {
                "gate": {"order": 0, "type": "PERSONALIZATION", "page": "cookieConsent",
                         "conditions": {"c": {"source": "COOKIE", "operator": "NOT_EXISTS",
                                              "field": "modlix_analytics_consent"}}},
                "split": {"order": 10, "type": "SPLIT", "variants": {
                    "a": {"page": "home", "weight": 50, "order": 0},
                    "b": {"page": "homeTwo", "weight": 50, "order": 1}}},
            }}
        }
        # No consent cookie: the gate wins and the site is never reached.
        assert explain(routing, "home", {"pageName": "home"}, pinned(0.9)).page_name == "cookieConsent"

        # With the cookie, the gate falls through and the test runs.
        withCookie = {"pageName": "home", "cookies": {"modlix_analytics_consent": "x"}}
        assert explain(routing, "home", withCookie, pinned(0.9)).page_name == "homeTwo"

    def test_the_gate_outranks_the_split_by_order(self):
        # Even a visitor already assigned to an arm is sent to the gate first.
        routing = {
            "home": {"rules": {
                "gate": {"order": 0, "type": "PERSONALIZATION", "page": "cookieConsent",
                         "conditions": {"c": {"source": "COOKIE", "operator": "NOT_EXISTS",
                                              "field": "modlix_analytics_consent"}}},
                "split": {"order": 10, "type": "SPLIT", "variants": {
                    "a": {"page": "home", "weight": 50, "order": 0},
                    "b": {"page": "homeTwo", "weight": 50, "order": 1}}},
            }}
        }
        out = explain(routing, "home", {"pageName": "home", "assignments": {"split": "b"}}, pinned(0.1))
        assert out.page_name == "cookieConsent"


class TestConsentTrapIsRefused:
    """`set_page_route_rule` will not write the shape at all any more.

    Kiran, after clearing it off `crumbco` by hand: "consentPage cannot be part
    of the page routing". So this is a refusal, not a warning — there is no
    adjustment that makes a consent page work as a routing destination.
    """

    GATE = {"source": "COOKIE", "operator": "NOT_EXISTS", "field": "modlix_analytics_consent"}

    def test_the_crumbco_rule_is_refused(self):
        rule = {"type": "PERSONALIZATION", "page": "cookieConsent",
                "conditions": {"c": dict(self.GATE)}}
        refusals, notes = consent_trap_reasons(rule)
        assert len(refusals) == 1
        assert "properties.consentPage" in refusals[0]
        assert notes == []

    def test_refused_even_when_the_target_is_innocuous(self):
        # The condition alone is the trap: routing cannot make anyone decide.
        rule = {"type": "PERSONALIZATION", "page": "pricing",
                "conditions": {"c": dict(self.GATE)}}
        assert consent_trap_reasons(rule)[0]

    def test_the_apps_own_cookie_name_is_honoured(self):
        rule = {"type": "PERSONALIZATION", "page": "pricing",
                "conditions": {"c": {"source": "COOKIE", "operator": "NOT_EXISTS",
                                     "field": "cc_ok"}}}
        assert consent_trap_reasons(rule) == ([], [])
        assert consent_trap_reasons(rule, consent_cookie="cc_ok")[0]

    def test_pointing_at_the_slot_is_refused_with_no_condition_at_all(self):
        rule = {"type": "PERSONALIZATION", "page": "cookieConsent",
                "conditions": {"c": {"source": "DEVICE", "operator": "EQUALS", "value": "MOBILE"}}}
        refusals, _ = consent_trap_reasons(rule, consent_page="cookieConsent")
        assert len(refusals) == 1
        assert "already shows it over every page" in refusals[0]

    def test_a_split_arm_pointing_at_the_slot_is_refused_too(self):
        rule = {"type": "SPLIT", "variants": {
            "a": {"page": "home", "weight": 1}, "b": {"page": "cookieConsent", "weight": 1}}}
        assert consent_trap_reasons(rule, consent_page="cookieConsent")[0]

    def test_a_consent_looking_name_is_only_a_note(self):
        # Nothing proves `cookiePolicy` is the consent overlay, and a plain
        # cookie-policy page is a perfectly ordinary routing target.
        rule = {"type": "PERSONALIZATION", "page": "cookieConsentPolicy",
                "conditions": {"c": {"source": "DEVICE", "operator": "EQUALS", "value": "MOBILE"}}}
        refusals, notes = consent_trap_reasons(rule)
        assert refusals == []
        assert len(notes) == 1
        assert "reads like a consent page" in notes[0]

    def test_ordinary_rules_are_left_alone(self):
        rule = {"type": "PERSONALIZATION", "page": "pricing_dentists",
                "conditions": {"c": {"source": "QUERY", "field": "utm_campaign",
                                     "operator": "EQUALS", "value": "dentists"}}}
        assert consent_trap_reasons(rule, consent_page="cookieConsent") == ([], [])

    def test_reading_the_consent_cookie_positively_is_allowed(self):
        # "people who have already decided" is a legitimate audience; it is the
        # NOT_EXISTS direction that has no way out.
        rule = {"type": "PERSONALIZATION", "page": "pricing",
                "conditions": {"c": {"source": "COOKIE", "operator": "EXISTS",
                                     "field": "modlix_analytics_consent"}}}
        assert consent_trap_reasons(rule) == ([], [])
