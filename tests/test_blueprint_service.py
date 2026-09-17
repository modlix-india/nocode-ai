"""Blueprint: the parts that must not be wrong.

Three writers put JSON into the `blueprint` field — this service, the
BlueprintEditor component, and an agent — and only one of them goes through the
TypeScript gate. So the rules are pinned here, on the Python side, at the grain
of the failure each one prevents:

  1. an array reaches the field, and every tenant that touches it is silently
     detached from base corrections from then on;
  2. a key is minted that a binding path cannot address;
  3. a model emits no content at all and every symptom reads as "it had nothing
     to say" — the failure that cost the lore curator three weeks;
  4. `pending` and `drifted` get conflated, and an "update" overwrites the work
     it was meant to record.

No database and no LLM: the provider is stubbed and everything else is the pure
layer.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.blueprint import context as bp_context
from app.services.blueprint import objects, service
from app.services.blueprint import compose as bp_compose
from app.services.blueprint import router as bp_router
from app.services.blueprint.service import BlueprintGenerationError, parse_json_object
from app.services.blueprint.validate import (
    ORDER_GAP,
    UID_PATTERN,
    coerce_lists,
    mint_uid,
    next_order,
    validate_blueprint,
)


# ── Rule 1: no arrays, at any depth ──────────────────────────────────────


def test_a_top_level_array_is_refused():
    issues = validate_blueprint({"plan": {"sections": [{"name": "Hero"}]}})
    assert issues
    assert issues[0].path == "plan.sections"
    assert "opaque" in issues[0].message


def test_an_array_buried_four_levels_down_is_still_refused():
    # The shallow case is the one people remember to check. This is the one that
    # ships.
    blueprint = {
        "plan": {
            "objects": {
                "aHome": {
                    "order": 1000,
                    "route": {"redirects": ["/old", "/older"]},
                }
            }
        }
    }
    issues = validate_blueprint(blueprint)
    assert [i.path for i in issues] == ["plan.objects.aHome.route.redirects"]


def test_every_array_is_reported_not_just_the_first():
    # A generator handed one problem per round trip never converges.
    blueprint = {"plan": {"a": [1], "b": [2], "c": {"d": [3]}}}
    issues = validate_blueprint(blueprint)
    assert len(issues) == 3


def test_a_clean_plan_passes():
    blueprint = {
        "schemaVersion": 1,
        "intent": "Book consultations.",
        "plan": {
            "sections": {
                "hHero": {"order": 1000, "purpose": "Convert a stranger"},
                "hForm": {"order": 2000, "purpose": "Take the booking"},
            }
        },
    }
    assert validate_blueprint(blueprint) == []


# ── Rule 2: keys a binding path can address ──────────────────────────────


@pytest.mark.parametrize("key", ["7abc", "my-key", "a.b", "", "café"])
def test_unaddressable_keys_are_refused_in_a_uid_keyed_map(key):
    issues = validate_blueprint({"plan": {"sections": {key: {"order": 1000}}}})
    assert issues, f"{key!r} should have been refused"
    assert "letter-first" in issues[0].message


def test_reserved_words_are_allowed_as_keys_in_a_uid_keyed_map():
    # `order` sits alongside the uids in these maps and is not one.
    assert validate_blueprint({"plan": {"sections": {"order": 1000}}}) == []


def test_a_hyphen_is_only_refused_where_keys_are_uids():
    # brand.variables keys are CSS custom property names, not minted uids.
    assert validate_blueprint({"plan": {"brand": {"variables": {"--bp-ink": "x"}}}}) == []


def test_minted_uids_are_always_addressable():
    # shortUUID is base62 with the digits leading, so about one key in six would
    # start with a digit if this used the platform minter.
    assert all(UID_PATTERN.match(mint_uid()) for _ in range(200))


def test_order_must_be_a_whole_number():
    assert validate_blueprint({"plan": {"sections": {"a": {"order": 1.5}}}})
    # True is an int in Python and is not an order.
    assert validate_blueprint({"plan": {"sections": {"a": {"order": True}}}})
    assert validate_blueprint({"plan": {"sections": {"a": {"order": 1000}}}}) == []


def test_next_order_leaves_room_to_insert():
    assert next_order({}) == ORDER_GAP
    assert next_order({"a": {"order": 1000}, "b": {"order": 2000}}) == 3000


# ── Rule 3: refuse, never truncate ───────────────────────────────────────


def test_an_oversized_plan_is_refused_whole():
    fat = {"plan": {"sections": {f"s{i}": {"order": i, "purpose": "x" * 400}
                                 for i in range(1000)}}}
    issues = validate_blueprint(fat)
    assert any("budget" in i.message for i in issues)
    assert any("truncated" in i.message for i in issues)


# ── Coercion, which is for generation only ───────────────────────────────


def test_coercion_turns_a_list_into_an_ordered_keyed_map():
    out = coerce_lists({"sections": [{"name": "Hero"}, {"name": "Form"}]})
    sections = out["sections"]
    assert isinstance(sections, dict)
    assert [v["name"] for v in sorted(sections.values(), key=lambda v: v["order"])] == [
        "Hero", "Form",
    ]
    assert validate_blueprint({"plan": out}) == []


def test_coercion_preserves_an_order_the_model_supplied():
    out = coerce_lists([{"name": "b", "order": 5}, {"name": "a", "order": 1}])
    assert sorted(v["order"] for v in out.values()) == [1, 5]


# ── Rule 4: the model call, and its silent failure ───────────────────────


class _Provider:
    """A stub provider that returns a scripted list of responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def create_completion(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("the service made more calls than were scripted")
        return self.responses.pop(0)


@pytest.fixture
def provider(monkeypatch):
    def install(*responses):
        stub = _Provider(*responses)
        monkeypatch.setattr(service, "get_llm_provider", lambda: stub)
        return stub
    return install


@pytest.mark.asyncio
async def test_empty_content_with_stop_length_retries_with_double_the_budget(provider):
    # THE bug. A reasoning model spends its whole budget thinking and emits
    # nothing; every symptom looks like "the model decided there was nothing to
    # say". Doubling the budget is the only thing that fixes it.
    stub = provider(
        {"content": "", "reasoning_content": "x" * 900, "stop_reason": "length"},
        {"content": '{"schemaVersion": 1, "plan": {}}', "stop_reason": "end_turn"},
    )
    result = await service.generate(prompt="a dental site")

    assert result["valid"]
    assert len(stub.calls) == 2
    assert stub.calls[1]["max_tokens"] == stub.calls[0]["max_tokens"] * 2


@pytest.mark.asyncio
async def test_prose_gets_one_retry_telling_it_to_return_json(provider):
    stub = provider(
        {"content": "Sure! Here is a plan for you.", "stop_reason": "end_turn"},
        {"content": '{"plan": {}}', "stop_reason": "end_turn"},
    )
    await service.generate(prompt="a dental site")

    assert len(stub.calls) == 2
    assert "not valid JSON" in stub.calls[1]["messages"][-1]["content"]
    # The budget is NOT doubled here: the model had plenty of room and used it
    # on the wrong thing.
    assert stub.calls[1]["max_tokens"] == stub.calls[0]["max_tokens"]


@pytest.mark.asyncio
async def test_two_failures_raise_rather_than_return_an_empty_plan(provider):
    provider(
        {"content": "", "stop_reason": "end_turn"},
        {"content": "", "stop_reason": "end_turn"},
    )
    with pytest.raises(BlueprintGenerationError) as caught:
        await service.generate(prompt="a dental site")
    assert caught.value.reason == "empty-response"


@pytest.mark.asyncio
async def test_a_hung_provider_is_cut_off_rather_than_held_forever(provider, monkeypatch):
    class _Hang:
        async def create_completion(self, **kwargs):
            await asyncio.sleep(10)

    monkeypatch.setattr(service, "get_llm_provider", lambda: _Hang())
    monkeypatch.setattr(service, "_timeout", lambda: 0.05)

    with pytest.raises(BlueprintGenerationError) as caught:
        await service.generate(prompt="a dental site")
    assert caught.value.reason == "timeout"


@pytest.mark.asyncio
async def test_generated_arrays_are_coerced_rather_than_wasting_the_generation(provider):
    provider({
        "content": '{"plan": {"sections": [{"name": "Hero"}]}}',
        "stop_reason": "end_turn",
    })
    result = await service.generate(prompt="a dental site")

    assert result["valid"], result["issues"]
    assert isinstance(result["blueprint"]["plan"]["sections"], dict)


@pytest.mark.asyncio
async def test_generation_records_what_produced_it(provider):
    provider({"content": '{"plan": {}}', "stop_reason": "end_turn"})
    result = await service.generate(prompt="  a dental site  ")
    origin = result["blueprint"]["origin"]

    assert origin["prompt"] == "a dental site"
    assert origin["generatedBy"] == "blueprint-service"
    assert origin["generatedAt"]


@pytest.mark.asyncio
async def test_refining_shows_the_model_the_current_plan_and_says_to_keep_it(provider):
    stub = provider({"content": '{"plan": {}}', "stop_reason": "end_turn"})
    await service.generate(
        prompt="add a blog", existing={"plan": {"objects": {"aHome": {"order": 1000}}}},
    )
    sent = stub.calls[0]["messages"][0]["content"]

    assert "refinement" in sent
    assert "aHome" in sent


@pytest.mark.parametrize(
    "raw,reason",
    [
        ("", "empty-response"),
        ("   ", "empty-response"),
        ("no json at all", "no-json"),
        ("{not json}", "json-error"),
        # Valid JSON, wrong shape. Distinct from no-json on purpose: the fix is
        # a different instruction to the model.
        ("[1, 2]", "not-an-object"),
    ],
)
def test_parse_says_which_way_it_failed(raw, reason):
    # Four different failures collapsing into one bare None is what made the
    # curator's silence unreadable for weeks.
    parsed, got = parse_json_object(raw)
    assert parsed is None
    assert got == reason


def test_parse_tolerates_a_code_fence():
    parsed, reason = parse_json_object('```json\n{"a": 1}\n```')
    assert reason == ""
    assert parsed == {"a": 1}


# ── describe ─────────────────────────────────────────────────────────────


def _page(*sections):
    definition = {"root": {"key": "root", "name": "root", "type": "Grid", "children": {}}}
    for index, (key, name, kind) in enumerate(sections):
        definition["root"]["children"][key] = True
        definition[key] = {"key": key, "name": name, "type": kind, "displayOrder": index}
    return {"name": "home", "rootComponent": "root", "componentDefinition": definition}


def test_describe_only_summarises_the_top_level_sections():
    # A page can carry nine hundred components. The board draws the root's
    # direct children and that is what needs describing.
    document = _page(("a", "Hero", "Grid"), ("b", "Form", "Grid"))
    document["componentDefinition"]["deep"] = {"key": "deep", "name": "Buried", "type": "Text"}

    parts = service.summarise_definition(document, "page")
    assert set(parts) == {"a", "b"}


@pytest.mark.asyncio
async def test_describe_drops_keys_the_model_invented(provider):
    # A description against a section that does not exist renders as a card
    # nobody can click.
    provider({
        "content": '{"describes": {"a": "The first thing a visitor sees", '
                   '"ghost": "A section that is not there"}}',
        "stop_reason": "end_turn",
    })
    result = await service.describe(document=_page(("a", "Hero", "Grid")), kind="page")

    assert result["describes"] == {"a": "The first thing a visitor sees"}


@pytest.mark.asyncio
async def test_describe_makes_no_model_call_when_there_is_nothing_to_describe(monkeypatch):
    def explode():
        raise AssertionError("the customer is metered; do not call the model for nothing")

    monkeypatch.setattr(service, "get_llm_provider", explode)
    result = await service.describe(document=_page(), kind="page")
    assert result["describes"] == {}


# ── suggest_features ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_features_point_at_objects_rather_than_containing_them(provider):
    provider({
        "content": '{"features": {"fBlog": {"name": "Blog", "intent": "Show we know"}},'
                   ' "assignments": {"blogList": "fBlog", "blogPost": "fBlog"}}',
        "stop_reason": "end_turn",
    })
    result = await service.suggest_features(objects=[
        {"name": "blogList", "kind": "page"},
        {"name": "blogPost", "kind": "page"},
        {"name": "home", "kind": "page"},
    ])

    assert result["assignments"] == {"blogList": "fBlog", "blogPost": "fBlog"}
    # Nesting would make moving an object between features a delete plus an add
    # under the differ, orphaning any override of it.
    assert "objects" not in result["features"]["fBlog"]


@pytest.mark.asyncio
async def test_a_feature_of_one_object_is_dropped(provider):
    # It is a name for an object, which the object already has.
    provider({
        "content": '{"features": {"fSolo": {"name": "Contact"}},'
                   ' "assignments": {"contact": "fSolo"}}',
        "stop_reason": "end_turn",
    })
    result = await service.suggest_features(objects=[
        {"name": "contact", "kind": "page"}, {"name": "home", "kind": "page"},
    ])
    assert result["features"] == {}


@pytest.mark.asyncio
async def test_objects_the_model_invented_are_not_placed(provider):
    provider({
        "content": '{"features": {"fBlog": {"name": "Blog"}},'
                   ' "assignments": {"blogList": "fBlog", "imaginary": "fBlog",'
                   ' "alsoFake": "fBlog"}}',
        "stop_reason": "end_turn",
    })
    result = await service.suggest_features(objects=[
        {"name": "blogList", "kind": "page"}, {"name": "home", "kind": "page"},
    ])
    # Only one real member survives, so the feature does not stand.
    assert result["features"] == {}
    assert result["assignments"] == {}


@pytest.mark.asyncio
async def test_a_bad_feature_key_is_reminted_and_the_assignment_follows(provider):
    provider({
        "content": '{"features": {"1blog": {"name": "Blog"}},'
                   ' "assignments": {"blogList": "1blog", "blogPost": "1blog"}}',
        "stop_reason": "end_turn",
    })
    result = await service.suggest_features(objects=[
        {"name": "blogList", "kind": "page"}, {"name": "blogPost", "kind": "page"},
    ])

    assert "1blog" not in result["features"]
    minted = next(iter(result["features"]))
    assert UID_PATTERN.match(minted)
    assert set(result["assignments"].values()) == {minted}


@pytest.mark.asyncio
async def test_one_object_is_never_worth_a_model_call(monkeypatch):
    def explode():
        raise AssertionError("nothing to group")

    monkeypatch.setattr(service, "get_llm_provider", explode)
    result = await service.suggest_features(objects=[{"name": "home", "kind": "page"}])
    assert result["features"] == {}


# ── Drift: the two directions ────────────────────────────────────────────


def _planned_page(reconciled=None, component_versions=None, sections=None):
    return {
        "name": "home",
        "rootComponent": "root",
        "componentDefinition": {
            "root": {"key": "root", "children": {"cHero": True, "cExtra": True}},
            "cHero": {"key": "cHero", "type": "Grid"},
            "cExtra": {"key": "cExtra", "type": "Grid"},
        },
        "componentVersions": component_versions or {"cHero": 3},
        "blueprint": {
            "reconciled": reconciled or {},
            "plan": {"sections": sections if sections is not None else {
                "sHero": {"order": 1000, "componentKey": "cHero"},
            }},
        },
    }


def test_a_plan_entry_nothing_was_built_for_is_pending_not_drifted():
    # Pending is resolved by BUILDING. Calling it drifted would point the fix at
    # the plan, which is already right.
    document = _planned_page(sections={"sGhost": {"order": 1000}})
    assert objects.drift_of(document)["status"]["sGhost"] == "pending"


def test_a_section_edited_since_the_plan_was_agreed_is_drifted_not_pending():
    # Drifted is resolved by UPDATING THE PLAN. Treating it as pending would
    # rebuild over somebody's edit.
    document = _planned_page(reconciled={"sHero": 1}, component_versions={"cHero": 4})
    assert objects.drift_of(document)["status"]["sHero"] == "drifted"


def test_an_agreed_section_that_has_not_moved_is_clean():
    document = _planned_page(reconciled={"sHero": 3}, component_versions={"cHero": 3})
    assert objects.drift_of(document)["status"]["sHero"] == "clean"


def test_never_reconciled_reads_as_drifted_and_needs_no_fourth_word():
    # In both cases the definition is what is real and the plan has not been
    # checked against it.
    assert objects.drift_of(_planned_page())["status"]["sHero"] == "drifted"


def test_something_built_that_no_plan_entry_claims_is_reported():
    assert objects.drift_of(_planned_page())["unplanned"] == ["cExtra"]


def test_an_object_with_no_plan_reports_nothing_rather_than_failing():
    result = objects.drift_of({"name": "home", "rootComponent": "root"})
    assert result["status"] == {}
    assert result["counts"] == {"clean": 0, "pending": 0, "drifted": 0}


# ── Seeding a plan for a site that already exists ────────────────────────


def test_seeding_creates_an_entry_per_described_part():
    # The day-one case for every existing site: no plan at all, so a derived
    # description has nothing to land on and would be charged for and discarded.
    document = _planned_page(sections={})
    document["blueprint"] = {}

    seeded = bp_compose.seed_entries(
        {}, document, "page", {"cHero": "The first thing a visitor sees"},
    )
    sections = seeded["plan"]["sections"]

    assert len(sections) == 1
    entry = next(iter(sections.values()))
    assert entry["componentKey"] == "cHero"
    assert entry["describes"] == "The first thing a visitor sees"
    assert validate_blueprint(seeded) == []


def test_a_seeded_entry_carries_no_purpose():
    # `purpose` is what a PERSON said the thing is for. Inventing one here would
    # later read as their own decision.
    seeded = bp_compose.seed_entries({}, _planned_page(sections={}), "page", {"cHero": "x"})
    assert "purpose" not in next(iter(seeded["plan"]["sections"].values()))


def test_a_seeded_entry_starts_clean_rather_than_drifted():
    # It was derived from exactly this definition, so it agrees with it. Without
    # the stamp every freshly described card would appear already drifted.
    document = _planned_page(sections={}, component_versions={"cHero": 7})
    document["blueprint"] = {}
    seeded = bp_compose.seed_entries({}, document, "page", {"cHero": "x"})

    document["blueprint"] = seeded
    uid = next(iter(seeded["plan"]["sections"]))
    assert objects.drift_of(document)["status"][uid] == "clean"


def test_seeding_leaves_an_entry_that_already_claims_the_component():
    document = _planned_page()          # sHero already claims cHero
    seeded = bp_compose.seed_entries(
        document["blueprint"], document, "page", {"cHero": "x", "cExtra": "y"},
    )
    sections = seeded["plan"]["sections"]

    assert set(sections) == {"sHero", next(k for k in sections if k != "sHero")}
    assert len(sections) == 2
    assert [e["componentKey"] for e in sections.values()].count("cHero") == 1


def test_describes_land_on_the_entries_that_claim_those_components():
    blueprint = bp_compose.apply_describes(
        _planned_page()["blueprint"], {"cHero": "The opening pitch"},
    )
    assert blueprint["plan"]["sections"]["sHero"]["describes"] == "The opening pitch"


def test_a_description_for_something_with_no_plan_entry_is_skipped_not_invented():
    # Creating entries in _apply_describes would silently turn a description
    # pass into a planning pass. That is what `seed` is for, and it is opt-in.
    blueprint = bp_compose.apply_describes(
        _planned_page()["blueprint"], {"cExtra": "Built but unplanned"},
    )
    assert len(blueprint["plan"]["sections"]) == 1


# ── The pushed brief ─────────────────────────────────────────────────────


def test_an_app_with_no_plan_says_nothing_every_turn():
    # Saying "this app has no plan" on every request would spend the budget on a
    # non-event, and day one is every existing app.
    assert bp_context.render_brief({}) == ""
    assert bp_context.render_brief({"plan": {}}) == ""


def test_the_brief_names_the_features_and_counts_the_objects():
    text = bp_context.render_brief({
        "intent": "Book consultations.",
        "plan": {
            "audience": "Nervous first-timers.",
            "features": {"fBlog": {"order": 1000, "name": "Blog", "status": "planned"}},
            "objects": {"a": {"order": 1000}, "b": {"order": 2000}},
        },
    })
    assert "Book consultations." in text
    assert "Blog (planned)" in text
    assert "2 objects are planned" in text
    assert "blueprint_get" in text


def test_the_brief_cache_can_be_dropped_for_one_app():
    # A plan the agent just wrote must not be shadowed by the one it replaced.
    bp_context.invalidate()
    bp_context._cache[("CLIENTA", "dental")] = (9e12, "stale")
    bp_context._cache[("CLIENTA", "other")] = (9e12, "kept")

    assert bp_context.invalidate("CLIENTA", "dental") == 1
    assert ("CLIENTA", "other") in bp_context._cache
    bp_context.invalidate()


def test_the_brief_stays_inside_its_budget():
    text = bp_context.render_brief(
        {"intent": "x" * 5000, "plan": {"objects": {"a": {"order": 1}}}}, budget=200,
    )
    assert "truncated" in text


# ── The kind table ───────────────────────────────────────────────────────


def test_every_kind_resolves_and_an_unknown_one_says_what_is_valid():
    for name in objects.KIND_NAMES:
        assert objects.resolve_kind(name).name == name
    with pytest.raises(objects.BlueprintObjectError) as caught:
        objects.resolve_kind("wishful")
    assert "page" in caught.value.message


# ── The sweep ────────────────────────────────────────────────────────────
#
# The job's whole reason to exist is that it survives what a single request
# cannot: many objects, minutes of work, and one of them failing. These pin
# that promise rather than the arithmetic.


def _sweep_stubs(monkeypatch, *, fail_on: str = "", pages: tuple[str, ...] = ("home", "about")):
    from app.services.blueprint import plan_job

    written: list[tuple[str, str]] = []

    async def fake_list(kind, app_code, headers, size=1000):
        if kind == "page":
            return [{"kind": "page", "name": n, "title": n.title(), "description": ""} for n in pages]
        return []

    async def fake_read_object(kind, app_code, name, headers):
        return {"name": name, "componentVersions": {"cHero": 3}, "blueprint": {}}

    async def fake_read_blueprint(kind, app_code, name, headers):
        return {"blueprint": {}}

    async def fake_describe(document, kind, app_code):
        if document["name"] == fail_on:
            raise BlueprintGenerationError("the model said nothing")
        return {"describes": {"cHero": "a hero"}}

    async def fake_generate(prompt, kind, app_code, context=None, existing=None):
        return {"valid": True, "blueprint": {"schemaVersion": 1, "plan": {}}, "issues": []}

    async def fake_write(kind, app_code, name, blueprint, headers, client_code, message=""):
        written.append((kind, name))
        return {"version": 2}

    monkeypatch.setattr(plan_job.objects, "list_objects", fake_list)
    monkeypatch.setattr(plan_job.objects, "read_object", fake_read_object)
    monkeypatch.setattr(plan_job.objects, "read_blueprint", fake_read_blueprint)
    monkeypatch.setattr(plan_job.objects, "write_blueprint", fake_write)
    monkeypatch.setattr(plan_job.service, "describe", fake_describe)
    monkeypatch.setattr(plan_job.service, "generate", fake_generate)

    async def fake_context(app_code, headers):
        return {}

    monkeypatch.setattr(plan_job, "build_app_context", fake_context)
    return plan_job, written


async def _run_sweep(plan_job, **kwargs):
    job = await plan_job.start(
        app_code="crumbco", prompt="read it", headers={}, client_code="SYSTEM", **kwargs
    )
    await job.task
    return job


def test_the_step_list_is_real_before_any_model_runs(monkeypatch):
    """The first poll must answer "what is being processed" with names.

    A sweep that only reports a count is a spinner with extra steps.
    """
    plan_job, _ = _sweep_stubs(monkeypatch)

    async def go():
        job = await plan_job.start(
            app_code="crumbco", prompt="p", headers={}, client_code="SYSTEM"
        )
        first = job.progress()
        await job.task
        return first

    first = asyncio.run(go())
    assert first["total"] == 4
    assert [s["label"] for s in first["steps"]] == [
        "the site as a whole", "Home", "About", "the list of what it is made of",
    ]


def test_every_object_is_written_not_just_the_app_plan(monkeypatch):
    """The shallow plan bug: the app plan is written and nothing else is."""
    plan_job, written = _sweep_stubs(monkeypatch)
    job = asyncio.run(_run_sweep(plan_job))
    assert job.state == "done"
    # The app document twice on purpose: once for the plan, and once at the end
    # for the index, which cannot be written before the objects are read.
    assert written == [
        ("application", ""), ("page", "home"), ("page", "about"), ("application", ""),
    ]


def test_one_object_failing_is_one_object(monkeypatch):
    """A failed step carries its reason and the sweep finishes the rest.

    The alternative — abandoning the sweep — throws away the objects that were
    going to succeed, and charges for the ones already done.
    """
    plan_job, written = _sweep_stubs(monkeypatch, fail_on="home")
    job = asyncio.run(_run_sweep(plan_job))
    progress = job.progress()
    assert job.state == "done"
    assert progress["failed"] == 1
    assert [s["state"] for s in progress["steps"]] == ["done", "failed", "done", "done"]
    assert "the model said nothing" in progress["steps"][1]["detail"]
    assert ("page", "about") in written


def test_a_second_press_joins_the_running_sweep(monkeypatch):
    """Two sweeps on one app would race on the same objects and bill twice."""
    plan_job, _ = _sweep_stubs(monkeypatch)

    async def go():
        job = await plan_job.start(
            app_code="crumbco", prompt="p", headers={}, client_code="SYSTEM"
        )
        same = plan_job.running_for("crumbco")
        await job.task
        return job, same

    job, same = asyncio.run(go())
    assert same is job
    assert plan_job.running_for("crumbco") is None


def test_skipping_the_app_plan_still_describes_the_objects(monkeypatch):
    """Regenerating descriptions must not overwrite wording a person edited."""
    plan_job, written = _sweep_stubs(monkeypatch)

    generated: list[str] = []

    async def fake_generate(prompt, kind, app_code, context=None, existing=None):
        generated.append(kind)
        return {"valid": True, "blueprint": {"schemaVersion": 1, "plan": {}}, "issues": []}

    monkeypatch.setattr(plan_job.service, "generate", fake_generate)

    job = asyncio.run(_run_sweep(plan_job, seed_app_plan=False))
    assert job.state == "done"
    # Nothing was GENERATED for the app: the wording stands.
    assert generated == []
    assert [w for w in written if w[0] == "page"] == [("page", "home"), ("page", "about")]
    # The index still lands on the app document. It adds a line and a status per
    # object and touches no wording, which is the difference that matters.
    assert written[-1] == ("application", "")


# ── Every kind, not just the two that are easy to picture ────────────────
#
# A plan that covers the pages and the storages and nothing else describes a
# site that does nothing: the functions are where the work happens, the
# uripaths are the addresses other systems call, and the templates are what the
# customer actually receives. Those are also the parts nobody can reconstruct
# by looking at the site, which is exactly why they belong in a written plan.


def test_the_sweep_covers_every_kind_the_board_draws():
    from app.services.blueprint import plan_job

    assert plan_job.SWEPT_KINDS == objects.BOARD_KINDS
    for kind in objects.BOARD_KINDS:
        assert kind in objects.KINDS
    # The app is planned once, as the thing that owns the objects. Sweeping it
    # as one more object beside them would plan it twice and disagree with
    # itself.
    assert "application" not in objects.BOARD_KINDS


def test_a_function_is_described_by_its_steps():
    document = {
        "name": "sendEnquiry",
        "definition": {"steps": {
            "load": {"statementName": "load", "namespace": "Core", "name": "Read"},
            "mail": {"statementName": "mail", "namespace": "Message", "name": "Send"},
        }},
    }
    parts = service.summarise_definition(document, "function")
    assert set(parts) == {"load", "mail"}
    assert "Message.Send" in parts["mail"]


def test_a_uripath_is_described_by_the_steps_behind_it():
    # Its parts are its steps, not its path: the path is the object's own name
    # on the board and a card for it would describe its own column.
    document = {"name": "enquiry", "pathString": "/enquiry",
                "pathDefinition": {"steps": {"save": {"namespace": "Core", "name": "Create"}}}}
    assert set(service.summarise_definition(document, "uripath")) == {"save"}


def test_a_template_and_a_notification_are_described_by_their_own_parts():
    template = {"name": "welcome", "templateParts": {"SUBJECT": {}, "BODY": {}}}
    assert set(service.summarise_definition(template, "template")) == {"SUBJECT", "BODY"}

    notification = {"name": "newEnquiry", "channelDetails": {"EMAIL": {}, "IN_APP": {}}}
    assert set(service.summarise_definition(notification, "notification")) == {"EMAIL", "IN_APP"}


def test_a_theme_is_one_card_titled_by_its_own_name():
    # A theme carries a couple of hundred variables. A card per variable is two
    # hundred cards saying what their own names say.
    parts = service.summarise_definition({"name": "crumb", "title": "Crumb"}, "theme")
    assert list(parts) == ["crumb"]
    assert "Crumb" in parts["crumb"]


def test_each_kind_seeds_entries_into_its_own_collection():
    # The link field is never an index: a step is matched by its statement name,
    # which survives the step being moved.
    seeded = bp_compose.seed_entries({}, {"name": "sendEnquiry"}, "function", {"mail": "sends it"})
    steps = seeded["plan"]["steps"]
    assert [e["step"] for e in steps.values()] == ["mail"]
    assert [e["describes"] for e in steps.values()] == ["sends it"]

    seeded = bp_compose.seed_entries({}, {"name": "welcome"}, "template", {"SUBJECT": "the line"})
    assert [e["part"] for e in seeded["plan"]["parts"].values()] == ["SUBJECT"]

    # A kind with no table of its own still plans somewhere real rather than
    # dropping its description on the floor.
    seeded = bp_compose.seed_entries({}, {"name": "crumb"}, "theme", {"crumb": "warm and printed"})
    assert [e["part"] for e in seeded["plan"]["parts"].values()] == ["crumb"]


def test_describes_land_on_the_right_collection_for_the_kind():
    blueprint = {"plan": {"steps": {"s1": {"order": 1000, "step": "mail"}}}}
    bp_compose.apply_describes(blueprint, {"mail": "sends the enquiry on"}, "function")
    assert blueprint["plan"]["steps"]["s1"]["describes"] == "sends the enquiry on"

    # With no kind every known collection is walked, for a caller holding a
    # blueprint without the kind it came off.
    blueprint = {"plan": {"channels": {"c1": {"order": 1000, "channel": "EMAIL"}}}}
    bp_compose.apply_describes(blueprint, {"EMAIL": "mails the owner"})
    assert blueprint["plan"]["channels"]["c1"]["describes"] == "mails the owner"


def test_the_sweep_names_objects_of_every_kind(monkeypatch):
    """The shallow-plan complaint, one level up: pages and storages only.

    Each kind is one step in the same list, so a failure in the functions does
    not cost the pages and the progress reads as one sweep rather than nine.
    """
    plan_job, written = _sweep_stubs(monkeypatch)

    stock = {
        "page": ("home",),
        "function": ("sendEnquiry",),
        "template": ("welcome",),
        "theme": ("crumb",),
    }

    async def fake_list(kind, app_code, headers, size=1000):
        return [
            {"kind": kind, "name": n, "title": n.title(), "description": ""}
            for n in stock.get(kind, ())
        ]

    monkeypatch.setattr(plan_job.objects, "list_objects", fake_list)

    job = asyncio.run(_run_sweep(plan_job))
    assert job.state == "done"
    assert written == [
        ("application", ""), ("page", "home"), ("function", "sendEnquiry"),
        ("template", "welcome"), ("theme", "crumb"), ("application", ""),
    ]


def test_a_kind_that_cannot_be_listed_does_not_cost_the_others(monkeypatch):
    """An older core build with no notifications route is not a broken plan."""
    plan_job, written = _sweep_stubs(monkeypatch)

    async def fake_list(kind, app_code, headers, size=1000):
        if kind == "notification":
            raise objects.BlueprintObjectError("no such route", status=404)
        if kind == "page":
            return [{"kind": kind, "name": "home", "title": "Home", "description": ""}]
        return []

    monkeypatch.setattr(plan_job.objects, "list_objects", fake_list)

    job = asyncio.run(_run_sweep(plan_job))
    assert job.state == "done"
    assert ("page", "home") in written
