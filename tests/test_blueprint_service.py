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

    async def fake_describe(document, kind, app_code, connections="", meter=None):
        if document["name"] == fail_on:
            raise BlueprintGenerationError("the model said nothing")
        return {"describes": {"cHero": "a hero"}}

    async def fake_generate(prompt, kind, app_code, context=None, existing=None, meter=None):
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

    async def fake_generate(prompt, kind, app_code, context=None, existing=None, meter=None):
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


def test_a_uripath_is_described_by_the_methods_it_answers():
    """A URI path holds no steps. Its parts are its HTTP methods.

    `pathDefinitions` is PLURAL and maps a method to a handler whose
    `kiRunFxDefinition` NAMES the function that runs rather than containing one.
    The earlier version of this test asserted against a singular
    `pathDefinition` with inline steps, which is a shape the platform never
    writes — so the test passed while every real URI path on the board came back
    with nothing to describe.
    """
    document = {
        "name": "/api/open/enquiry", "pathString": "/api/open/enquiry",
        "pathDefinitions": {
            "POST": {"uriType": "KIRUN_FUNCTION",
                     "kiRunFxDefinition": {"namespace": "crumbco", "name": "takeEnquiry"}},
            "GET": {"uriType": "KIRUN_FUNCTION",
                    "kiRunFxDefinition": {"namespace": "crumbco", "name": "listEnquiries"}},
        },
    }
    parts = service.summarise_definition(document, "uripath")
    assert set(parts) == {"POST", "GET"}
    # Which function answers it is the whole of what a path is for.
    assert "crumbco.takeEnquiry" in parts["POST"]


def test_a_template_and_a_notification_are_described_by_their_own_parts():
    template = {"name": "welcome", "templateParts": {"SUBJECT": {}, "BODY": {}}}
    assert set(service.summarise_definition(template, "template")) == {"SUBJECT", "BODY"}

    # `channelTemplates`, not `channelDetails`. The wording lives inline under
    # `templateParts` keyed by language, and the title is the useful line.
    notification = {"name": "newEnquiry", "channelTemplates": {
        "inapp": {"templateParts": {"en": {"title": "New enquiry"}}},
        "email": {"templateParts": {"en": {"description": "Somebody asked about a box."}}},
    }}
    parts = service.summarise_definition(notification, "notification")
    assert set(parts) == {"inapp", "email"}
    assert "New enquiry" in parts["inapp"]
    assert "box" in parts["email"]


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


# ── The brand: what the site is actually set in ──────────────────────────
#
# None of this is in the object lists and none of it is in any object's
# definition. The typeface is a variable on the theme; the favicon is a <link>
# on the application's own properties. A plan written from the lists alone
# could say anything at all about the look, and did.


def _theme_doc():
    return {
        "name": "crumb",
        "variables": {"ALL": {
            "fontFamily": "Figtree, system-ui",
            "primaryFont": "14px/14px <fontFamily>",
            "colorOne": "#2C2E32",
            "customColor0": "#FEF4DB",
            "textColor0": "#FF6E3D",
            "errorColor": "#E02020",
            # Per-component tokens. Two hundred of these say how one widget is
            # drawn, not what the site looks like.
            "textBoxActiveRightIconColorDefaultPrimary": "<colorOne>88",
            "buttonFontDefaultQuaternary": "500 14px/14px <fontFamily>",
            "textBoxHeightDefaultTertiary": "38px",
            "backgroundHoverColorOne": "#A3ACB6",
        }},
    }


def test_the_look_is_the_brand_variables_and_not_the_widget_tokens():
    found = bp_compose.brand_variables(_theme_doc())
    assert "fontFamily" in found and found["fontFamily"].startswith("Figtree")
    assert {"primaryFont", "colorOne", "customColor0", "textColor0", "errorColor"} <= set(found)
    # Anchored on the name, which is what keeps the component tokens out.
    assert not any(k.startswith("textBox") or k.startswith("button") for k in found)


def test_a_theme_is_described_by_what_the_site_is_set_in():
    # It came back as one card reading "the theme called Crumb", which answers
    # nothing. Somebody reading a plan wants to know the typeface.
    parts = service.summarise_definition(_theme_doc(), "theme")
    assert "fontFamily" in parts
    assert "primaryFont" in parts


def test_a_theme_with_no_brand_variables_still_says_something():
    parts = service.summarise_definition({"name": "bare", "title": "Bare"}, "theme")
    assert list(parts) == ["bare"]


def test_the_icon_the_site_already_has_lands_on_the_plan():
    # A picture is never a document, so an icon that exists appears nowhere at
    # all unless something puts it there.
    blueprint = bp_compose.index_assets({}, {"icons": {"icon": "/files/favicon.png"}})
    assets = [
        e for e in blueprint["plan"]["objects"].values() if e.get("kind") == "asset"
    ]
    assert len(assets) == 1
    assert assets[0]["asset"]["assetId"] == "/files/favicon.png"
    # It exists, so it is not something still to be made.
    assert assets[0]["status"] == "built"


def test_indexing_an_icon_never_overwrites_what_somebody_asked_for():
    # The file on the site today is not an answer to "a cinnamon roll in a
    # circle, flat, two colours" — it may be exactly what they want replaced.
    blueprint = {"plan": {"objects": {"a1": {
        "order": 1000, "kind": "asset", "name": "icon",
        "asset": {"use": "Favicon", "intent": "A cinnamon roll in a circle",
                  "assetId": None},
    }}}}
    bp_compose.index_assets(blueprint, {"icons": {"icon": "/files/old.png"}})
    asset = blueprint["plan"]["objects"]["a1"]["asset"]
    assert asset["intent"] == "A cinnamon roll in a circle"
    assert asset["use"] == "Favicon"
    assert asset["assetId"] == "/files/old.png"


def test_a_site_with_no_icon_gets_no_asset_entry():
    blueprint = bp_compose.index_assets({}, {"icons": "none — this site has no favicon set"})
    assert blueprint == {}


@pytest.mark.asyncio
async def test_generation_is_told_what_the_site_looks_like(monkeypatch):
    """The model invented a palette because nothing ever showed it one."""
    async def fake_list(kind, app_code, headers, size=1000):
        if kind == "theme":
            return [{"kind": "theme", "name": "crumb", "title": "", "description": ""}]
        return []

    async def fake_read(kind, app_code, name, headers):
        if kind == "theme":
            return _theme_doc()
        return {"properties": {"links": {
            "l1": {"rel": "icon", "href": "/files/favicon.png"},
            "l2": {"rel": "stylesheet", "href": "/x.css"},
        }}}

    monkeypatch.setattr(bp_compose.objects, "list_objects", fake_list)
    monkeypatch.setattr(bp_compose.objects, "read_object", fake_read)

    context = await bp_compose.build_app_context("crumbco", {})
    brand = context["brand"]
    assert brand["icons"] == {"icon": "/files/favicon.png"}
    assert brand["look"]["fontFamily"].startswith("Figtree")
    assert brand["theme"] == "crumb"


def test_the_real_typeface_and_palette_are_written_into_the_plan():
    # The brand block was pure invention: nothing ever read the theme, so a
    # made-up palette sat beside the one actually in use and nothing told them
    # apart.
    brand = {"theme": "crumb", "look": bp_compose.brand_variables(_theme_doc())}
    blueprint = bp_compose.index_brand({}, brand)
    block = blueprint["plan"]["brand"]
    assert block["typeface"].startswith("Figtree")
    assert block["theme"] == "crumb"
    assert block["palette"]["colorOne"] == "#2C2E32"
    assert "primaryFont" in block["typeScale"]
    # A colour is a value, not a font.
    assert "primaryFont" not in block["palette"]


def test_reading_the_theme_never_rewrites_what_a_person_judged():
    # Tone is a judgement about the brand. No amount of re-reading the theme
    # produces it, and a sweep that flattened it would erase the only sentence
    # anybody wrote about how the site should feel.
    blueprint = {"plan": {"brand": {"tone": "Warm, printed, unhurried.",
                                    "motion": "Almost none."}}}
    bp_compose.index_brand(blueprint, {"look": bp_compose.brand_variables(_theme_doc())})
    assert blueprint["plan"]["brand"]["tone"] == "Warm, printed, unhurried."
    assert blueprint["plan"]["brand"]["motion"] == "Almost none."
    assert blueprint["plan"]["brand"]["typeface"].startswith("Figtree")


def test_a_theme_that_could_not_be_read_leaves_the_brand_alone():
    blueprint = {"plan": {"brand": {"tone": "Warm."}}}
    bp_compose.index_brand(blueprint, {})
    assert blueprint["plan"]["brand"] == {"tone": "Warm."}


# ── A storage's fields are usually in a different document ───────────────


def test_a_referenced_storage_schema_is_resolved_on_read(monkeypatch):
    """The bug that marked a working form's fields "not on the site yet".

    A storage's `schema` is usually a REF to a schema document, not a schema.
    Everything read `schema.properties`, found nothing, and concluded the
    storage had no fields — so every planned field looked unbuilt while the live
    site was busy collecting them.
    """
    calls: list[tuple[str, str]] = []

    class FakeResult:
        success = True
        error = ""

        def __init__(self, data):
            self.data = data

    class FakeClient:
        async def get(self, path, headers=None, params=None):
            calls.append((path, (params or {}).get("name", "")))
            if path.startswith("/api/core/storages/"):
                return FakeResult({
                    "name": "orderRequest",
                    "schema": {"ref": "crumbco.orderRequest", "type": ["OBJECT"]},
                })
            if path.startswith("/api/core/schemas/"):
                return FakeResult({
                    "name": "crumbco.orderRequest",
                    "required": ["customerName"],
                    "properties": {"customerName": {"type": ["STRING"]},
                                   "email": {"type": ["STRING"]}},
                })
            name = (params or {}).get("name")
            return FakeResult({"content": [{"id": "1", "name": name, "clientCode": "C"}]})

    monkeypatch.setattr(objects, "_client", lambda: FakeClient())

    document = asyncio.run(objects.read_object("storage", "crumbco", "orderRequest", {}))
    assert set(document["schema"]["properties"]) == {"customerName", "email"}
    assert document["schema"]["required"] == ["customerName"]
    # The ref is followed, not guessed at.
    assert any(path.startswith("/api/core/schemas") for path, _ in calls)


def test_an_inline_storage_schema_is_left_exactly_alone(monkeypatch):
    class FakeResult:
        success = True
        error = ""

        def __init__(self, data):
            self.data = data

    class FakeClient:
        async def get(self, path, headers=None, params=None):
            assert "schemas" not in path, "an inline schema needs no second read"
            if path.startswith("/api/core/storages/"):
                return FakeResult({"name": "s", "schema": {"properties": {"a": {}}}})
            return FakeResult({"content": [{"id": "1", "name": "s", "clientCode": "C"}]})

    monkeypatch.setattr(objects, "_client", lambda: FakeClient())
    document = asyncio.run(objects.read_object("storage", "crumbco", "s", {}))
    assert set(document["schema"]["properties"]) == {"a"}


def test_an_object_with_nothing_new_to_say_still_reports_what_it_has():
    """The count must come from the PLAN, not from the describe call.

    A page whose sections are all described already gives a describer nothing to
    add, and the sweep took that branch reporting `parts=0` — for a page whose
    plan held three sections. Measured on a real sweep: `blogList parts=0` next
    to a plan with three. A confident wrong number is worse than the blank it
    replaced, because the blank at least read as "not known yet".
    """
    from app.services.blueprint.plan_job import _counts

    plan = {"plan": {"sections": {
        "s1": {"order": 1000, "name": "header", "componentKey": "header"},
        "s2": {"order": 2000, "name": "postList", "componentKey": "postList"},
        "s3": {"order": 3000, "name": "emptyState", "componentKey": None},
    }}}
    pending, parts = _counts(plan, "page")
    assert parts == 3
    assert pending == 1


def test_the_count_follows_the_kind_to_its_own_collection():
    # A storage keeps `fields`, a function `steps`, a page `sections`. Counting
    # `sections` on a storage would report nothing for every storage there is.
    from app.services.blueprint.plan_job import _counts

    assert _counts({"plan": {"fields": {"a": {}, "b": {}}}}, "storage")[1] == 2
    assert _counts({"plan": {"steps": {"a": {}}}}, "function")[1] == 1
    assert _counts({}, "page") == (0, 0)


# ── Keeping the index true the moment a plan changes ─────────────────────
#
# The board reads `pending` and `parts` per object out of the APP plan, because
# that is the only place it can read them without opening every object. So when
# the plan agent added two sections to `home`, the app plan still said `home`
# had nothing outstanding — and the board said "Nothing to build" over two
# planned sections until somebody opened that column by hand, at which point
# Build suddenly had work. The count has to move when the plan moves.


def test_the_index_learns_what_a_plan_write_changed():
    page_plan = {"plan": {"sections": {
        "s1": {"order": 1000, "name": "hero", "componentKey": "cHero"},
        "s2": {"order": 2000, "name": "ingredients", "componentKey": None},
        "s3": {"order": 3000, "name": "pricing", "componentKey": None},
    }}}
    from app.services.blueprint.build_job import pending_sections
    from app.services.blueprint.compose import collection_for, index_objects

    collection, _ = collection_for("page")
    seen = [(
        "page", "home", "",
        len(pending_sections(page_plan, "page")),
        len(page_plan["plan"][collection]),
    )]
    app = index_objects({"plan": {"objects": {"o1": {
        "order": 1000, "kind": "page", "name": "home",
        "summary": "the storefront", "pending": 0, "parts": 1,
    }}}}, seen)

    entry = app["plan"]["objects"]["o1"]
    assert entry["pending"] == 2
    # How many it HAS, which is a different question and answered nowhere else
    # cheaply: a column nobody has opened has no cards to count.
    assert entry["parts"] == 3
    # An empty summary means "keep what is there". This write is not the thing
    # that derives summaries and must not blank one.
    assert entry["summary"] == "the storefront"


def test_an_object_the_index_never_mentioned_is_added_not_dropped():
    from app.services.blueprint.compose import index_objects

    app = index_objects({}, [("storage", "orderRequest", "what gets kept", 0, 4)])
    entry = next(iter(app["plan"]["objects"].values()))
    assert entry["name"] == "orderRequest"
    assert entry["parts"] == 4
    assert entry["status"] == "built"


# ── Reconciling what the builder actually built ──────────────────────────
#
# The most expensive mistake in this whole feature, and it was a read of the
# wrong surface. The AppBuilder agent is draft-first: it authors onto a DRAFT
# and publishes nothing. Reconciliation read the LIVE object, found an empty
# page, and reported "the builder finished without putting anything on the
# page" — about a page carrying eleven components and every planned section on
# its draft. Every fill run looked like a total failure, and the conclusion
# drawn from that (that the authoring half had never once worked) was false.


def _page_surfaces(live_components, draft_children):
    """(live, draft) documents for one page, as the platform returns them."""
    definition = {"root": {"key": "root", "name": "rootGrid", "type": "Grid",
                           "children": {k: True for k in draft_children}}}
    for key, name in draft_children.items():
        definition[key] = {"key": key, "name": name, "type": "Grid"}
    live = {
        "name": "blogList", "rootComponent": "root",
        "componentDefinition": {"root": {"key": "root", "children": {}}},
        "blueprint": {"plan": {"sections": {
            "sHeader": {"order": 1000, "name": "header", "componentKey": None},
            "sList": {"order": 2000, "name": "postList", "componentKey": None},
        }}},
    }
    draft = {"name": "blogList", "rootComponent": "root",
             "componentDefinition": definition, "componentVersions": {}}
    return live, draft


def _reconcile(monkeypatch, live, draft):
    from app.services.blueprint import build_job

    saved: list[dict] = []
    # Bound here rather than read from the enclosing scope, because the stub's
    # own `draft` FLAG shadows the `draft` DOCUMENT otherwise — and the symptom
    # is "'bool' object is not iterable", which points nowhere near the cause.
    live_doc, draft_doc = live, draft

    async def fake_read(kind, app_code, name, headers, draft=False):
        return dict(draft_doc if draft else live_doc)

    async def fake_write(kind, app_code, name, blueprint, headers, client_code, message=""):
        saved.append(blueprint)
        return {"version": 2}

    monkeypatch.setattr(build_job.objects, "read_object", fake_read)
    monkeypatch.setattr(build_job.objects, "write_blueprint", fake_write)
    result = asyncio.run(build_job._reconcile_built("crumbco", "blogList", {}, "SYSTEM"))
    return result, saved


def test_what_the_builder_wrote_to_the_draft_is_found(monkeypatch):
    # The live page is empty and that is NORMAL: nothing is published until a
    # person says so. The built work is on the draft and that is where the
    # match has to be made.
    live, draft = _page_surfaces({}, {"header": "headerGrid", "postList": "postList"})
    (matched, untouched), saved = _reconcile(monkeypatch, live, draft)
    assert matched == 2
    assert untouched == []


def test_a_section_is_matched_by_its_KEY_not_only_its_name(monkeypatch):
    """Asked for `header`, the builder makes a component KEYED `header` and
    NAMED `headerGrid`.

    Matching on the name alone found two of three sections on a real page and
    missed the third, so a page that was correctly and completely built still
    reported a failure. The key is the better identifier anyway: it is what
    `componentKey` stores and what the plan points at for the rest of its life.
    """
    live, draft = _page_surfaces({}, {"header": "headerGrid", "postList": "postList"})
    (matched, _), saved = _reconcile(monkeypatch, live, draft)
    keys = {uid: e["componentKey"]
            for uid, e in saved[-1]["plan"]["sections"].items()}
    assert keys == {"sHeader": "header", "sList": "postList"}


def test_the_plan_is_read_from_live_even_though_the_build_is_on_the_draft(monkeypatch):
    # A draft of the CONTENT is not a draft of the INTENT. The plan lives on the
    # live object and is written back there, or an unpublished page would carry
    # a plan nothing else can see.
    live, draft = _page_surfaces({}, {"header": "headerGrid", "postList": "postList"})
    draft["blueprint"] = {"plan": {"sections": {}}}  # the draft has no plan at all
    (matched, _), saved = _reconcile(monkeypatch, live, draft)
    assert matched == 2
    assert set(saved[-1]["plan"]["sections"]) == {"sHeader", "sList"}


def test_nothing_built_is_still_nothing(monkeypatch):
    # The fix must not turn an empty draft into a false success — that was the
    # failure in the other direction and it is the worse one.
    live, draft = _page_surfaces({}, {})
    (matched, untouched), saved = _reconcile(monkeypatch, live, draft)
    assert matched == 0
    assert saved == []


# ── Metering the calls the agent loop never sees ─────────────────────────
#
# The sweep talks to a provider directly, so nothing in the agent path meters
# it. For a while nothing did: forty model calls against a SUSPENDED wallet,
# billed for none of them, while the chat on the same screen correctly refused
# to answer. The two halves of one screen disagreed about whether the customer
# had any money.


class _FakeAuth:
    client_code = "FIN"
    access_app_code = "sitezump"
    token = "Bearer x"
    path_prefix = ""

    def to_headers(self):
        return {}


def _meter(monkeypatch, *, allowed=True):
    """A CallMeter whose gate and debit are recorded rather than sent."""
    from app.services import billing

    charged: list[dict] = []
    gates: list[int] = []

    async def fake_status(auth):
        gates.append(1)
        return allowed

    async def fake_charge(auth, usage, model, request_id, session_id):
        charged.append({
            "usage": usage, "model": model,
            "requestId": request_id, "sessionId": session_id,
        })

    monkeypatch.setattr(billing, "check_serving_status", fake_status)
    monkeypatch.setattr(billing, "charge_llm_call", fake_charge)
    return billing.CallMeter(_FakeAuth(), session_id="test"), charged, gates


def _provider(monkeypatch, responses):
    """Stand in for the LLM provider, handing back `responses` in order."""
    from app.services.blueprint import service as svc

    handed = iter(responses)

    class Fake:
        async def create_completion(self, **kwargs):
            return next(handed)

    monkeypatch.setattr(svc, "get_llm_provider", lambda: Fake())


_OK = {
    "content": '{"describes": {}}',
    "usage": {"input_tokens": 1000, "output_tokens": 500},
    "model": "claude-sonnet-5",
    "stop_reason": "end_turn",
}


def test_a_direct_model_call_is_charged(monkeypatch):
    meter, charged, _ = _meter(monkeypatch)
    _provider(monkeypatch, [_OK])

    asyncio.run(service._complete_json(
        system_prompt="s", user_message="u", label="test", meter=meter,
    ))

    assert len(charged) == 1
    # The model comes from the RESPONSE, not the requested tier: a tier that
    # resolves to something heavier must be weighted as what actually ran.
    assert charged[0]["model"] == "claude-sonnet-5"
    # Weighted, in the same unit the wallet is debited in. Sonnet is 3x.
    assert meter.tokens == 1500 * 3
    assert meter.calls == 1


def test_a_retry_is_charged_too(monkeypatch):
    """Otherwise the malformed-JSON path is free — and it is the common path.

    A retry is a second real call to a real provider. The tokens are spent
    whether or not the answer parsed, and charging only the successful attempt
    would make a cheap model's favourite failure mode cost nothing.
    """
    meter, charged, _ = _meter(monkeypatch)
    garbage = {**_OK, "content": "I think the page is nice."}
    _provider(monkeypatch, [garbage, _OK])

    asyncio.run(service._complete_json(
        system_prompt="s", user_message="u", label="test", meter=meter,
    ))

    assert len(charged) == 2
    assert meter.calls == 2


def test_a_call_that_produced_garbage_is_still_charged(monkeypatch):
    # Charged as soon as it lands, before anything decides the answer was
    # unusable. Provider time was spent either way.
    meter, charged, _ = _meter(monkeypatch)
    garbage = {**_OK, "content": "no json here"}
    _provider(monkeypatch, [garbage, garbage])

    with pytest.raises(BlueprintGenerationError):
        asyncio.run(service._complete_json(
            system_prompt="s", user_message="u", label="test", meter=meter,
        ))
    assert len(charged) == 2


def test_an_empty_wallet_refuses_before_the_call(monkeypatch):
    meter, charged, _ = _meter(monkeypatch, allowed=False)

    from app.services.blueprint import service as svc

    class Forbidden:
        async def create_completion(self, **kwargs):
            raise AssertionError("no completion may be requested on an empty wallet")

    monkeypatch.setattr(svc, "get_llm_provider", Forbidden)

    with pytest.raises(BlueprintGenerationError) as caught:
        asyncio.run(service._complete_json(
            system_prompt="s", user_message="u", label="test", meter=meter,
        ))
    assert caught.value.reason == "out-of-tokens"
    # The same sentence the chat uses, so the two halves of the screen agree.
    assert "out of tokens" in caught.value.message
    assert charged == []


def test_the_gate_is_not_asked_once_per_call_but_is_asked_again(monkeypatch):
    """A sweep is one action to a person and sixty calls to a wallet.

    Gating once at the start lets somebody who runs out on object four spend
    through object sixty; asking before every call would be sixty HTTP calls in
    a row. So a YES is trusted briefly and then re-asked.
    """
    meter, _, gates = _meter(monkeypatch)
    _provider(monkeypatch, [_OK, _OK])

    asyncio.run(service._complete_json(
        system_prompt="s", user_message="u", label="a", meter=meter,
    ))
    asyncio.run(service._complete_json(
        system_prompt="s", user_message="u", label="b", meter=meter,
    ))
    assert len(gates) == 1

    # Once the window passes, it is asked again rather than trusted for ever.
    meter._allowed_until = 0.0
    _provider(monkeypatch, [_OK])
    asyncio.run(service._complete_json(
        system_prompt="s", user_message="u", label="c", meter=meter,
    ))
    assert len(gates) == 2


def test_a_no_is_never_cached(monkeypatch):
    # Somebody who tops up mid-sweep must be able to carry on. Caching a no
    # leaves the job dead after the money arrives.
    from app.services import billing

    answers = iter([False, True])
    asked: list[int] = []

    async def fake_status(auth):
        asked.append(1)
        return next(answers)

    monkeypatch.setattr(billing, "check_serving_status", fake_status)
    meter = billing.CallMeter(_FakeAuth())
    assert asyncio.run(meter.allowed()) is False
    assert asyncio.run(meter.allowed()) is True
    assert len(asked) == 2


def test_the_sweep_reports_what_it_spent(monkeypatch):
    # A sweep that silently costs money is the thing the meter exists to stop,
    # so the cost rides on the same poll the progress bar reads.
    plan_job, _ = _sweep_stubs(monkeypatch, pages=("home",))
    meter, _, _ = _meter(monkeypatch)
    job = asyncio.run(_run_sweep(plan_job))
    job.meter = meter
    meter.calls, meter.tokens = 3, 4500.4
    progress = job.progress()
    assert progress["calls"] == 3
    assert progress["tokens"] == 4500


def test_a_sweep_with_no_auth_charges_nothing_and_still_runs(monkeypatch):
    # Tests and internal callers pass no auth. They must not crash, and they
    # must not silently look like a charged run either.
    plan_job, _ = _sweep_stubs(monkeypatch, pages=("home",))
    job = asyncio.run(_run_sweep(plan_job))
    assert job.state == "done"
    assert job.meter is None
    assert job.progress()["calls"] == 0


# ── Which object reaches which ───────────────────────────────────────────
#
# The reason to plan before building is that a model can hold a graph of forty
# nodes and cannot hold forty definitions. These pin the graph: that every edge
# is a reference actually written in a definition, that a name appearing in
# prose is not one, and that the uids are stable so a sweep does not look like
# it deleted the whole graph and wrote a new one.


def _known(**names):
    from app.services.blueprint.relations import Known
    return Known(names)


def test_a_page_records_the_storage_its_own_logic_reads():
    from app.services.blueprint import relations

    document = {
        "name": "orderForm",
        "eventFunctions": {"e1": {"name": "onLoad", "steps": {
            "readOrders": {
                "statementName": "readOrders",
                "namespace": "CoreServices", "name": "Storage.ReadPage",
                "parameterMap": {"storageName": {"u1": {"type": "VALUE", "value": "orderRequest"}}},
            },
        }}},
    }
    edges = relations.edges_of(document, "page", _known(storage=["orderRequest", "enquiry"]))
    assert len(edges) == 1
    edge = edges[0]
    assert (edge.to_kind, edge.to_name) == ("storage", "orderRequest")
    # The verb comes from the primitive. Reading a list and destroying one are
    # not the same relationship and collapsing them loses the part that matters.
    assert edge.how == "reads"
    # And where it was found, so a person can check it rather than trust it.
    assert edge.where == "onLoad/readOrders"


def test_a_storage_named_in_prose_is_not_a_connection():
    """The failure this whole module is shaped around.

    The first version matched storage names against the page's JSON on a word
    boundary and linked a page to the `post` storage because a heading read
    "Post an enquiry". A derived edge that is wrong is worse than no edge: the
    next thing to read it cannot tell it from one somebody drew by hand.
    """
    from app.services.blueprint import relations

    document = {
        "name": "home",
        "componentDefinition": {"t1": {
            "name": "headline", "type": "Text",
            "properties": {"text": {"value": "Post an enquiry about orderRequest"}},
        }},
    }
    assert relations.edges_of(document, "page", _known(storage=["post", "orderRequest"])) == []


def test_a_reference_to_something_this_app_does_not_have_is_dropped():
    # An address outside this app is a true thing to know nothing about, and
    # inventing a node for it would put a page on the board that does not exist.
    from app.services.blueprint import relations

    document = {"name": "home", "componentDefinition": {"l1": {
        "name": "toDocs", "type": "Link",
        "properties": {"linkPath": {"value": "/somebodyElsesPage"}},
    }}}
    assert relations.edges_of(document, "page", _known(page=["home", "about"])) == []


def test_a_uripath_reaches_the_function_it_delegates_to():
    """The most valuable edge in an app and the easiest to miss.

    A URI path contains no logic. `pathDefinitions.<METHOD>.kiRunFxDefinition`
    is a REFERENCE naming the function that runs. These are the addresses the
    outside world calls, and what happens when it does is invisible from the
    site, from the pages and from the path itself.
    """
    from app.services.blueprint import relations

    document = {
        "name": "/api/open/enquiry", "pathString": "/api/open/enquiry",
        "pathDefinitions": {
            "POST": {"kiRunFxDefinition": {"namespace": "crumbco", "name": "takeEnquiry"}},
            "GET": {"kiRunFxDefinition": {"namespace": "crumbco", "name": "listEnquiries"}},
        },
    }
    edges = relations.edges_of(
        document, "uripath",
        _known(function=["crumbco.takeEnquiry", "crumbco.listEnquiries"]),
    )
    assert {(e.to_name, e.where) for e in edges} == {
        ("crumbco.takeEnquiry", "POST"), ("crumbco.listEnquiries", "GET"),
    }


def test_a_storage_foreign_key_is_a_connection():
    # Kept in a TOP-LEVEL `relations` map, not in `schema.properties.<f>.ref`,
    # which is a reference to a schema document and a different thing entirely.
    from app.services.blueprint import relations

    document = {"name": "blogs", "relations": {"category": {
        "storageName": "blogCategories", "relationType": "TO_MANY",
    }}}
    edges = relations.edges_of(document, "storage", _known(storage=["blogs", "blogCategories"]))
    assert len(edges) == 1
    assert edges[0].how == "links to many"
    assert edges[0].where == "category"


def test_an_expression_is_never_read_as_a_reference():
    # An EXPRESSION is computed at run time and its text is not an address.
    # Reading one would put a guess into a fact a build acts on.
    from app.services.blueprint import relations

    document = {"name": "p", "eventFunctions": {"e": {"steps": {"s": {
        "statementName": "s", "namespace": "CoreServices", "name": "Storage.Read",
        "parameterMap": {"storageName": {"u": {"type": "EXPRESSION", "expression": "orderRequest"}}},
    }}}}}
    assert relations.edges_of(document, "page", _known(storage=["orderRequest"])) == []


def test_the_graph_keys_are_the_same_every_sweep():
    """A fresh uid per sweep would detach every tenant override of the graph.

    The blueprint is diffed by the platform's override machinery, so a map whose
    keys change on every write reads as "everything deleted, everything added".
    """
    from app.services.blueprint.relations import Edge, index_relations

    edges = [
        Edge("page", "home", "storage", "orderRequest", "reads", "onLoad/read"),
        Edge("page", "home", "page", "about", "goes to", "nav"),
    ]
    first = index_relations({}, edges)["plan"]["relations"]
    second = index_relations({}, list(reversed(edges)))["plan"]["relations"]
    assert set(first) == set(second)
    assert all(key[0].isalpha() for key in first)


def test_an_app_with_no_connections_says_so_rather_than_keeping_a_stale_graph():
    from app.services.blueprint.relations import index_relations

    had = {"plan": {"relations": {"rold": {"order": 1000, "from": "page:gone"}}}}
    assert "relations" not in index_relations(had, [])["plan"]


def test_an_object_stops_claiming_a_connection_it_no_longer_has():
    # `uses` is DERIVED, so it is replaced whole. A merge would keep a dead edge
    # forever, and a plan listing a connection nobody can find is worse than one
    # listing none.
    had = {"plan": {"uses": {"rold": {"order": 1000, "kind": "storage", "name": "gone"}}}}
    assert "uses" not in bp_compose.set_uses(had, {})["plan"]


def test_what_reaches_an_object_is_answerable_from_the_graph():
    """The question the plan exists to answer: can we drop this?

    No object can compute its own incoming edges — only something holding every
    document at once can — which is why the graph lives on the application and
    not spread across the objects it describes.
    """
    from app.services.blueprint.relations import Edge, summarise

    edges = [
        Edge("page", "orderForm", "storage", "orderRequest", "writes to", "submit"),
        Edge("function", "crumbco.dailyDigest", "storage", "orderRequest", "reads", "read"),
    ]
    line = summarise(edges, "storage", "orderRequest")
    assert "orderForm" in line and "crumbco.dailyDigest" in line
    assert "Reached from" in line



def test_the_connections_survive_a_describe_that_fails(monkeypatch):
    """The free half of the work must not be thrown away when the paid half breaks.

    The connections are read off the definition: no model, no tokens, no way to
    fail the way a provider can. A sweep against an empty wallet or a dead
    provider should still leave the app knowing what reaches what, because that
    is the half that answers "can we drop this".
    """
    plan_job, written = _sweep_stubs(monkeypatch, pages=("orderForm",))

    async def read_with_logic(kind, app_code, name, headers):
        if kind != "page":
            return {"name": name, "blueprint": {}}
        return {"name": name, "blueprint": {}, "eventFunctions": {"e": {
            "name": "submit", "steps": {"save": {
                "statementName": "save", "namespace": "CoreServices", "name": "Storage.Create",
                "parameterMap": {"storageName": {
                    "u": {"type": "VALUE", "value": "orderRequest"}}},
            }},
        }}}

    async def list_with_a_storage(kind, app_code, headers, size=1000):
        if kind == "page":
            return [{"kind": "page", "name": "orderForm", "title": "Order", "description": ""}]
        if kind == "storage":
            return [{"kind": "storage", "name": "orderRequest", "title": "", "description": ""}]
        return []

    async def out_of_tokens(document, kind, app_code, connections="", meter=None):
        raise BlueprintGenerationError("You're out of tokens.")

    saved: list[dict] = []

    async def capture(kind, app_code, name, blueprint, headers, client_code, message=""):
        saved.append({"kind": kind, "name": name, "blueprint": blueprint, "message": message})
        return {"version": 2}

    monkeypatch.setattr(plan_job.objects, "read_object", read_with_logic)
    monkeypatch.setattr(plan_job.objects, "list_objects", list_with_a_storage)
    monkeypatch.setattr(plan_job.service, "describe", out_of_tokens)
    monkeypatch.setattr(plan_job.objects, "write_blueprint", capture)

    job = asyncio.run(_run_sweep(plan_job))

    # The page's own plan records what it writes to, despite the failure.
    page = next(w for w in saved if w["kind"] == "page")
    uses = page["blueprint"]["plan"]["uses"]
    assert [e["name"] for e in uses.values()] == ["orderRequest"]
    assert [e["how"] for e in uses.values()] == ["writes to"]

    # And the app-level graph was still written, because the index runs anyway.
    app = [w for w in saved if w["kind"] == "application"][-1]
    relations = app["blueprint"]["plan"]["relations"]
    assert [e["to"] for e in relations.values()] == ["storage:orderRequest"]

    # The step still says it failed. Half the work landing is not success.
    describe_step = next(s for s in job.steps if s.name == "orderForm")
    assert describe_step.state == "failed"
    assert "out of tokens" in describe_step.detail
    assert "1 connections were still recorded" in describe_step.detail


def test_a_reworked_section_is_asked_to_be_CHANGED_not_added():
    """Otherwise a reworked hero becomes two heroes.

    Making a re-planned section `pending` was the right half of the fix and
    dangerous on its own: the fill prompt said "they are planned and not yet
    built... add them to the page's root component", which for something that
    already exists means append a second one beside it.
    """
    from app.services.blueprint import build_job

    fresh = [("u1", {"order": 1000, "name": "Pricing"})]
    rework = [("u2", {"order": 2000, "name": "Hero", "componentKey": "cHero",
                      "purpose": "say what it costs"})]

    both = build_job._FILL_PROMPT.format(
        name="home", app_code="crumbco",
        to_build=build_job._TO_BUILD.format(sections=build_job._brief_list(fresh)),
        to_rework=build_job._TO_REWORK.format(sections=build_job._brief_list(rework)),
    )
    add_half, change_half = both.split("CHANGE these sections")
    # Each section appears under its own instruction and not the other's.
    assert "Pricing" in add_half
    assert "Pricing" not in change_half
    assert "Hero" in change_half
    assert "Hero" not in add_half
    # And the instruction says the thing that prevents the duplicate.
    assert "do not duplicate" in change_half

    # A page with only new sections is never told to change anything, and the
    # other way round. An empty heading invites the model to invent work.
    only_new = build_job._FILL_PROMPT.format(
        name="home", app_code="crumbco",
        to_build=build_job._TO_BUILD.format(sections=build_job._brief_list(fresh)),
        to_rework="",
    )
    assert "CHANGE these sections" not in only_new
    assert "ADD these sections" in only_new


# ── Changing something that already exists ───────────────────────────────
#
# The gap that made the plan unusable for the common case. `pending` meant
# `componentKey` was null, so the moment a section was built it could never be
# planned again: you could ADD to a page and DESCRIBE a page, and you could not
# CHANGE one. Every conversation about reworking an existing page ended in a
# plan nothing could act on.


def _built_page(*, purpose, agreed=None, versions=None):
    from app.services.blueprint import objects as bp_objects

    entry = {"order": 1000, "name": "Hero", "componentKey": "cHero", "purpose": purpose}
    blueprint = {"plan": {"sections": {"s1": entry}}}
    if agreed is not None:
        blueprint["agreed"] = {"s1": agreed}
    blueprint["reconciled"] = {"s1": (versions or {}).get("cHero", 3)}
    document = {
        "name": "home", "blueprint": blueprint,
        "rootComponent": "root",
        "componentDefinition": {"root": {"children": {"cHero": True}}, "cHero": {"name": "Hero"}},
        "componentVersions": versions or {"cHero": 3},
    }
    return bp_objects, document, entry, blueprint


def test_reworking_a_built_section_puts_it_back_up_for_building():
    from app.services.blueprint import build_job

    bp_objects, document, entry, blueprint = _built_page(purpose="sell the boxes")
    # Stamped at what was built, then the plan is changed.
    blueprint["agreed"] = {"s1": bp_objects.plan_fingerprint(entry)}
    assert bp_objects.drift_of(document)["status"]["s1"] == "clean"
    assert build_job.pending_sections(blueprint, "page") == []

    entry["purpose"] = "sell the boxes AND take a booking"
    assert bp_objects.drift_of(document)["status"]["s1"] == "pending"
    assert [uid for uid, _ in build_job.pending_sections(blueprint, "page")] == ["s1"]


def test_an_existing_site_is_not_put_up_for_rebuild_by_being_looked_at():
    """The installed base. Every section seeded from a real site has no stamp.

    Treating an unstamped entry as changed would mark every card on every
    existing site "to build" the first time anybody opened the board, which is
    both wrong and the most expensive possible wrong.
    """
    from app.services.blueprint import build_job

    bp_objects, document, _, blueprint = _built_page(purpose="whatever it says")
    assert "agreed" not in blueprint
    assert bp_objects.drift_of(document)["status"]["s1"] == "clean"
    assert build_job.pending_sections(blueprint, "page") == []


def test_rewording_a_description_asks_for_nothing():
    # `describes` is DERIVED and rewritten on every sweep. Counting it as intent
    # would make each sweep look like the whole plan had changed.
    from app.services.blueprint import build_job

    bp_objects, _, entry, blueprint = _built_page(purpose="sell the boxes")
    blueprint["agreed"] = {"s1": bp_objects.plan_fingerprint(entry)}
    entry["describes"] = "a grid with four things in it"
    entry["order"] = 9000
    assert build_job.pending_sections(blueprint, "page") == []


def test_a_plan_change_and_a_hand_edit_are_not_confused():
    """They move in OPPOSITE directions and the whole model rests on the split.

    The plan moving ahead is resolved by BUILDING. The definition moving ahead
    is resolved by UPDATING THE PLAN. Collapsing them is how an update ends up
    overwriting the work it was meant to record.
    """
    bp_objects, document, entry, blueprint = _built_page(purpose="sell the boxes")
    blueprint["agreed"] = {"s1": bp_objects.plan_fingerprint(entry)}

    # Somebody edited the page by hand: the component version moved.
    document["componentVersions"]["cHero"] = 9
    assert bp_objects.drift_of(document)["status"]["s1"] == "drifted"

    # And the plan moved too. The plan wins: there is work to do, and doing it
    # is what settles both.
    entry["purpose"] = "something else entirely"
    assert bp_objects.drift_of(document)["status"]["s1"] == "pending"


# ── Building: turning the plan into objects that exist ───────────────────
#
# The mockup's promise, in its own words: "If one card fails it is one card,
# not the site." These pin that, and the two rules that make a build safe to
# press twice.


def _app_plan(**objects_map):
    return {"schemaVersion": 1, "plan": {"objects": objects_map}}


def test_only_what_is_marked_planned_counts_as_work():
    from app.services.blueprint import build_job

    blueprint = _app_plan(
        o1={"order": 1000, "kind": "page", "name": "blogList", "status": "planned"},
        o2={"order": 2000, "kind": "page", "name": "home", "status": "built"},
        # No status at all. A plan written before status existed describes a
        # site that already stands, and building those would try to create
        # every page the site already has.
        o3={"order": 3000, "kind": "page", "name": "about"},
    )
    assert [e["name"] for e in build_job.planned_objects(blueprint)] == ["blogList"]


def test_storages_are_made_before_the_pages_that_post_into_them():
    from app.services.blueprint import build_job

    blueprint = _app_plan(
        o1={"order": 1000, "kind": "page", "name": "contact", "status": "planned"},
        o2={"order": 2000, "kind": "storage", "name": "enquiry", "status": "planned"},
    )
    # A page filled before its storage exists is a form wired to nothing, and
    # nobody notices until a customer's first enquiry vanishes.
    assert [e["kind"] for e in build_job.planned_objects(blueprint)] == ["storage", "page"]


def test_a_section_with_a_component_key_is_not_pending():
    from app.services.blueprint import build_job

    blueprint = {"plan": {"sections": {
        "s1": {"order": 1000, "name": "Hero", "componentKey": "cHero"},
        "s2": {"order": 2000, "name": "Services", "componentKey": None},
        "s3": {"order": 500, "name": "Nav"},
    }}}
    pending = build_job.pending_sections(blueprint, "page")
    # In plan order, and only the ones nothing answers to. A section that HAS a
    # key is built or drifted, and drift is never resolved by building over
    # somebody's edit.
    assert [uid for uid, _ in pending] == ["s3", "s2"]


def test_a_storage_schema_is_built_from_the_planned_fields():
    from app.services.blueprint import build_job

    schema = build_job.schema_from_spec({"fields": {
        "f1": {"order": 1000, "name": "customerName", "type": "string", "required": True},
        "f2": {"order": 2000, "name": "quantity", "type": "number"},
        "f3": {"order": 3000, "name": "wantsCall", "type": "boolean"},
        "f4": {"order": 4000, "name": "pickupDate", "type": "date"},
    }})
    assert schema["properties"]["quantity"] == {"type": ["INTEGER"]}
    assert schema["properties"]["wantsCall"] == {"type": ["BOOLEAN"]}
    assert schema["required"] == ["customerName"]
    # A type nobody recognises defaults to STRING rather than being guessed at:
    # a number stored as a string sorts 10 before 9 and nobody sees it until a
    # customer does.
    assert build_job.schema_from_spec(
        {"fields": {"f1": {"order": 1000, "name": "x", "type": "wishful"}}}
    )["properties"]["x"] == {"type": ["STRING"]}


def test_a_field_order_survives_into_the_schema():
    from app.services.blueprint import build_job

    schema = build_job.schema_from_spec({"fields": {
        "b": {"order": 2000, "name": "second"},
        "a": {"order": 1000, "name": "first"},
    }})
    assert list(schema["properties"]) == ["first", "second"]


def test_a_plan_with_no_fields_is_not_a_storage():
    from app.services.blueprint import build_job

    assert build_job.schema_from_spec({})["properties"] == {}


def test_an_entry_seeded_before_the_naming_rule_is_named_on_the_next_sweep():
    """Otherwise those cards read "Untitled" for ever.

    A re-sweep skips anything already claimed, so entries seeded before there
    was a naming rule were never revisited — no number of sweeps would fix them.
    """
    document = {"name": "home", "componentDefinition": {
        "features": {"key": "features", "name": "grid", "type": "Grid"},
    }}
    had = {"plan": {"sections": {"s1": {
        "order": 1000, "componentKey": "features", "name": None, "describes": "a strip",
    }}}}
    seeded = bp_compose.seed_entries(had, document, "page", {"features": "a strip"})
    assert seeded["plan"]["sections"]["s1"]["name"] == "features"
    # And still exactly one entry: filling a name is not seeding a second card.
    assert len(seeded["plan"]["sections"]) == 1


def test_a_name_somebody_typed_is_never_recomputed():
    document = {"name": "home", "componentDefinition": {
        "features": {"key": "features", "name": "grid", "type": "Grid"},
    }}
    had = {"plan": {"sections": {"s1": {
        "order": 1000, "componentKey": "features", "name": "Why people buy",
    }}}}
    seeded = bp_compose.seed_entries(had, document, "page", {"features": "a strip"})
    assert seeded["plan"]["sections"]["s1"]["name"] == "Why people buy"


def test_a_seeded_entry_is_named_after_what_it_describes():
    # Every card read "Untitled" with its description underneath: a whole board
    # of anonymous cards, each confidently describing itself.
    document = {
        "name": "home",
        "componentDefinition": {
            "cHero": {"key": "cHero", "name": "Hero", "type": "Grid"},
            "cGrid2": {"key": "cGrid2", "name": "_grid2", "type": "Grid"},
            # A hand-built page names every section `grid` and keys them by what
            # they are. Falling to the TYPE titled twelve cards "Grid" while
            # `hero` and `features` sat unused in the keys.
            "features": {"key": "features", "name": "grid", "type": "Grid"},
        },
    }
    seeded = bp_compose.seed_entries(
        {}, document, "page",
        {"cHero": "the first thing", "cGrid2": "a strip", "features": "what it does"},
    )
    names = {e["componentKey"]: e["name"] for e in seeded["plan"]["sections"].values()}
    assert names["cHero"] == "Hero"
    # A key the editor minted is not worth showing over the type.
    assert names["cGrid2"] == "Grid"
    # A key somebody chose is worth more than either.
    assert names["features"] == "features"


# ── The build, end to end ────────────────────────────────────────────────


class _FakePlatform:
    """The platform, as far as a build can tell.

    Enough of it to prove the two things that matter: that MAKE ROOM creates the
    right rows and moves each spec onto the object it describes, and that the
    app plan is left saying "built" with no spec behind it.
    """

    def __init__(self, app_blueprint):
        self.documents = {"application": {"crumbco": {
            "id": "app1", "name": "crumbco", "blueprint": app_blueprint,
        }}}
        self.created: list[tuple[str, str]] = []
        self.patched: list[tuple[str, str]] = []

    # --- the bits objects.py and build_job.py actually call ---

    class Result:
        def __init__(self, data, success=True, error=""):
            self.data, self.success, self.error = data, success, error

    def _kind_of(self, path):
        for kind in ("page", "storage", "application"):
            if objects.resolve_kind(kind).api in path:
                return kind
        return ""

    async def get(self, path, headers=None, params=None):
        kind = self._kind_of(path)
        rows = self.documents.get(kind) or {}
        if path.endswith(objects.resolve_kind(kind).api) if kind else False:
            name = (params or {}).get("name")
            found = [r for n, r in rows.items() if not name or n == name]
            return self.Result({"content": found})
        # A detail read by id.
        for name, row in rows.items():
            if str(row.get("id")) in path:
                return self.Result(row)
        return self.Result(None, success=False, error="not found")

    async def post(self, path, headers=None, json=None):
        kind = self._kind_of(path)
        name = (json or {}).get("name") or ""
        self.created.append((kind, name))
        self.documents.setdefault(kind, {})[name] = {
            "id": f"{kind}-{name}", "name": name, **(json or {}),
        }
        return self.Result({"id": f"{kind}-{name}"})

    async def patch(self, path, headers=None, json=None):
        kind = self._kind_of(path)
        for name, row in (self.documents.get(kind) or {}).items():
            if str(row.get("id")) in path:
                row["blueprint"] = json
                self.patched.append((kind, name))
                return self.Result({"version": 2, "blueprint": json})
        return self.Result(None, success=False, error="not found")


def test_building_creates_the_objects_and_moves_each_plan_onto_its_own(monkeypatch):
    from app.services.blueprint import build_job

    platform = _FakePlatform({
        "schemaVersion": 1,
        "plan": {"objects": {
            "o1": {"order": 1000, "kind": "storage", "name": "enquiry",
                   "purpose": "Where enquiries land", "status": "planned",
                   "spec": {"entity": "Enquiry", "fields": {
                       "f1": {"order": 1000, "name": "fullName", "required": True},
                       "f2": {"order": 2000, "name": "email"},
                   }}},
            "o2": {"order": 2000, "kind": "page", "name": "contact",
                   "purpose": "Somewhere to get in touch", "status": "planned",
                   "spec": {"role": "form", "sections": {
                       "s1": {"order": 1000, "name": "The form", "componentKey": None},
                   }}},
        }},
    })
    monkeypatch.setattr(objects, "_client", lambda: platform)
    monkeypatch.setattr(
        "app.agents.appbuilder.tools._shared.get_saas_client", lambda: platform,
    )

    class Auth:
        client_code = "SYSTEM"

    async def go():
        job = await build_job.start(
            app_code="crumbco", headers={}, auth=Auth(), fill=False,
        )
        await job.task
        return job

    job = asyncio.run(go())
    progress = job.progress()
    assert job.state == "done", progress["error"]
    assert progress["failed"] == 0
    # The storage first, because a page's form posts into it.
    assert platform.created == [("storage", "enquiry"), ("page", "contact")]

    # Each spec became that object's OWN plan.
    page = platform.documents["page"]["contact"]["blueprint"]
    assert page["plan"]["sections"]["s1"]["name"] == "The form"
    assert page["intent"] == "Somewhere to get in touch"

    # And the schema was built from the planned fields.
    storage = platform.documents["storage"]["enquiry"]
    assert list(storage["schema"]["properties"]) == ["fullName", "email"]
    assert storage["schema"]["required"] == ["fullName"]

    # The app plan now says built, and the spec is GONE: two copies of a plan
    # is two plans, and the next edit lands on one of them.
    entries = platform.documents["application"]["crumbco"]["blueprint"]["plan"]["objects"]
    assert [e["status"] for e in entries.values()] == ["built", "built"]
    assert all("spec" not in e for e in entries.values())


def test_building_twice_does_not_create_a_second_copy(monkeypatch):
    """Pressing Build again, or after a half-failed run, must adopt not duplicate."""
    from app.services.blueprint import build_job

    platform = _FakePlatform({
        "plan": {"objects": {"o1": {
            "order": 1000, "kind": "page", "name": "contact", "status": "planned",
            "spec": {"sections": {}},
        }}},
    })
    # The page is already there, from a run that got this far and then died.
    platform.documents["page"] = {"contact": {"id": "page-contact", "name": "contact"}}
    monkeypatch.setattr(objects, "_client", lambda: platform)
    monkeypatch.setattr(
        "app.agents.appbuilder.tools._shared.get_saas_client", lambda: platform,
    )

    class Auth:
        client_code = "SYSTEM"

    async def go():
        job = await build_job.start(app_code="crumbco", headers={}, auth=Auth(), fill=False)
        await job.task
        return job

    job = asyncio.run(go())
    assert job.state == "done"
    assert platform.created == []
    assert job.steps[0].detail == "already there, adopted"


def test_a_kind_the_build_cannot_make_is_skipped_and_named(monkeypatch):
    """A theme in the plan is not a failure. Nothing went wrong; it is not made."""
    from app.services.blueprint import build_job

    platform = _FakePlatform({
        "plan": {"objects": {"o1": {
            "order": 1000, "kind": "theme", "name": "crumb", "status": "planned",
        }}},
    })
    monkeypatch.setattr(objects, "_client", lambda: platform)

    class Auth:
        client_code = "SYSTEM"

    async def go():
        job = await build_job.start(app_code="crumbco", headers={}, auth=Auth(), fill=False)
        await job.task
        return job

    job = asyncio.run(go())
    progress = job.progress()
    assert progress["failed"] == 0
    assert progress["steps"][0]["state"] == "skipped"
    assert "theme" in progress["steps"][0]["detail"]


def test_a_storage_with_no_planned_fields_is_refused_not_created(monkeypatch):
    from app.services.blueprint import build_job

    platform = _FakePlatform({
        "plan": {"objects": {"o1": {
            "order": 1000, "kind": "storage", "name": "empty", "status": "planned",
            "spec": {},
        }}},
    })
    monkeypatch.setattr(objects, "_client", lambda: platform)
    monkeypatch.setattr(
        "app.agents.appbuilder.tools._shared.get_saas_client", lambda: platform,
    )

    class Auth:
        client_code = "SYSTEM"

    async def go():
        job = await build_job.start(app_code="crumbco", headers={}, auth=Auth(), fill=False)
        await job.task
        return job

    job = asyncio.run(go())
    progress = job.progress()
    # A table with no columns is worse than a refusal somebody can read.
    assert progress["failed"] == 1
    assert "names no fields" in progress["steps"][0]["detail"]
    assert platform.created == []


def test_a_name_the_derivation_offered_beats_the_editor_default():
    # The page editor leaves most sections called "Grid", and a board of
    # "Grid 1" through "Grid 9" is exactly as useless as a board of "Untitled".
    document = {
        "name": "home",
        "componentDefinition": {
            "cHero": {"key": "cHero", "name": "Grid", "type": "Grid"},
            "cNav": {"key": "cNav", "name": "nav", "type": "Grid"},
        },
    }
    seeded = bp_compose.seed_entries(
        {}, document, "page",
        {"cHero": "the opening pitch", "cNav": "the top bar"},
        {"cHero": "Hero"},
    )
    names = {e["componentKey"]: e["name"] for e in seeded["plan"]["sections"].values()}
    assert names["cHero"] == "Hero"
    # Nothing offered for this one, and its own name is usable, so it stands.
    assert names["cNav"] == "nav"


@pytest.mark.asyncio
async def test_describe_returns_a_name_per_part(provider):
    provider({
        "content": '{"summary": "the front door", "names": {"a": "Hero", "ghost": "X"}, '
                   '"describes": {"a": "the first thing a visitor sees"}}',
        "stop_reason": "end_turn",
    })
    result = await service.describe(document=_page(("a", "Hero", "Grid")), kind="page")
    assert result["names"] == {"a": "Hero"}
    assert result["summary"] == "the front door"
    # A name against a part nobody asked about would title a card that is not
    # there, so it goes the same way an invented description does.
    assert "ghost" not in result["names"]


# ── Updating the plan from the site, for the whole site ──────────────────


def test_updating_the_plan_from_the_site_walks_every_page(monkeypatch):
    """The banner names no single card, so the request must not need one.

    It sent no name, `name` was required, the request failed validation, and
    the button reported an unknown error.
    """
    seen: list[str] = []

    async def fake_scope(auth, app_code, *, write):
        return None

    async def fake_list(kind, app_code, headers, size=1000):
        return [
            {"kind": kind, "name": n, "title": n, "description": ""}
            for n in ("home", "about", "contact")
        ]

    async def fake_one(body, auth):
        seen.append(body.name)
        if body.name == "about":
            # No plan entries at all — the ordinary state of most pages, and
            # not a failure of the sweep.
            raise bp_router.HTTPException(status_code=400, detail="no entries")
        if body.name == "contact":
            raise bp_router.HTTPException(status_code=502, detail="could not read it")
        return {"saved": True, "reconciled": ["s1", "s2"]}

    monkeypatch.setattr(bp_router, "_scope", fake_scope)
    monkeypatch.setattr(bp_router.objects, "list_objects", fake_list)
    monkeypatch.setattr(bp_router, "post_reconcile", fake_one)

    body = bp_router.ReconcileRequest(app_code="crumbco", kind="page", name="")
    result = asyncio.run(bp_router._reconcile_app(body, object(), {}))

    assert seen == ["home", "about", "contact"]
    assert result["reconciled"] == 2
    # A page with nothing to reconcile is not reported as broken.
    assert "about" not in result["failed"]
    # One that genuinely failed is named, and the rest were still done.
    assert "contact" in result["failed"]
    assert [o["name"] for o in result["objects"]] == ["home"]
