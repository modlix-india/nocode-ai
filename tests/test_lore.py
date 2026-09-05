"""Lore: the parts that must not be wrong.

Lore is written to unattended by an LLM and read months later as if it were
documentation. The two failure modes that would make it worse than nothing are:

  1. it records something it should not (a secret, an invented provenance link,
     a claim attributed to an entry that does not exist), and
  2. it presents stale knowledge with the same confidence as current knowledge.

These tests pin both, plus the parsing that stands between a small model's
output and the store.

No database and no LLM: everything here exercises the pure layer or stubs the
store, which is the point of keeping models.py free of I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.lore import curator, ingest, retrieval, store
from app.services.lore.access import LoreAccessError, LoreScope
from app.services.lore.models import (
    ENTRY_KINDS,
    Entry,
    Observation,
    body_hash,
    effective_confidence,
    is_subject_stale,
    fingerprint,
    normalise_subject,
    subject_type,
)


def _scope(
    *, app="fieldops", client="SYSTEM", chain=None, can_read=True, can_write=True,
) -> LoreScope:
    """A resolved scope, without going near the security service."""
    return LoreScope(
        app_code=app,
        client_code=client,
        read_chain=tuple(chain or (client,)),
        can_read=can_read,
        can_write=can_write,
    )


# ── Subjects ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, "app"),
        ("", "app"),
        ("app", "app"),
        ("APP", "app"),
        ("page:jobsToday", "page:jobsToday"),
        ("  Page : jobsToday ", "page:jobsToday"),
        ("storage:job", "storage:job"),
        ("function:CoreServices.Storage/ReadPage", "function:CoreServices.Storage/ReadPage"),
        # Junk degrades to app-level rather than losing the observation.
        ("not a subject at all", "app"),
        ("page:", "app"),
        (":jobsToday", "app"),
    ],
)
def test_normalise_subject(raw, expected):
    assert normalise_subject(raw) == expected


def test_subject_type():
    assert subject_type("page:jobsToday") == "page"
    assert subject_type("app") == "app"


# ── Fingerprints ─────────────────────────────────────────────────────────


def test_fingerprint_ignores_whitespace_and_case():
    """Re-observing the same fact must collapse, not duplicate."""
    a = fingerprint("chat", "app", "The SLA is  four   hours")
    b = fingerprint("chat", "app", "the sla is four hours\n")
    assert a == b


def test_fingerprint_separates_kind_and_subject():
    body = "The SLA is four hours"
    assert fingerprint("chat", "app", body) != fingerprint("manual", "app", body)
    assert fingerprint("chat", "app", body) != fingerprint("chat", "page:jobs", body)


def test_body_hash_covers_title_and_body():
    assert body_hash("A", "x") != body_hash("B", "x")
    assert body_hash("A", "x") != body_hash("A", "y")
    assert body_hash("A ", " x") == body_hash("a", "X")


# ── Effective confidence ─────────────────────────────────────────────────
# Age is NOT the mechanism here. An entry loses standing when something
# supersedes or contradicts it, or when the object it describes changes under
# it. Only `status` and `owner` expire with time.


def _ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_fresh_confidence_is_unchanged():
    assert effective_confidence("decision", 80, _ago(0)) == 80


def test_time_alone_does_not_erode_a_durable_kind():
    """The central claim: a five-year-old convention nobody contradicted stands.

    The old model halved this every 365 days, so it read as 3 instead of 80 —
    knowledge deleted for the crime of being old.
    """
    for kind in ("purpose", "decision", "convention", "constraint",
                 "glossary", "gotcha", "howto", "integration"):
        assert effective_confidence(kind, 80, _ago(1825)) == 80, kind


def test_status_and_owner_still_expire_with_time():
    """These two are claims about *now*, so age really is the signal."""
    assert effective_confidence("status", 90, _ago(30)) < 25   # 14-day half-life
    assert effective_confidence("owner", 90, _ago(180)) == pytest.approx(45, abs=1)


def test_contradiction_is_what_actually_demotes_an_entry():
    plain = effective_confidence("convention", 80, _ago(0))
    contested = effective_confidence("convention", 80, _ago(0), contradicted_by=1)
    assert contested == pytest.approx(plain / 2, abs=1)
    assert effective_confidence("convention", 80, _ago(0), contradicted_by=2) < contested


def test_contradiction_penalty_is_bounded():
    """A pile-on cannot drive an entry below the floor and stay there."""
    assert effective_confidence("convention", 80, _ago(0), contradicted_by=99) >= 0
    assert effective_confidence("convention", 80, _ago(0), contradicted_by=99) == \
        effective_confidence("convention", 80, _ago(0), contradicted_by=4)


def test_a_changed_subject_marks_an_entry_unverified_not_wrong():
    confirmed = _ago(10)
    fresh = effective_confidence("convention", 80, confirmed)
    stale = effective_confidence(
        "convention", 80, confirmed, subject_changed_at=_ago(2),
    )
    assert stale < fresh
    assert stale > fresh / 2, "a changed subject is a flag, not a contradiction"


def test_a_subject_changed_before_confirmation_is_not_stale():
    """We confirmed the entry AFTER the edit, so the edit is already accounted for."""
    assert not is_subject_stale(_ago(2), _ago(10))
    assert is_subject_stale(_ago(10), _ago(2))


def test_unknown_subject_change_is_not_stale():
    assert not is_subject_stale(_ago(10), None)
    assert not is_subject_stale(None, _ago(10))


def test_pinned_entries_ignore_every_adjustment():
    """A person answered the question all of these are guessing at."""
    assert effective_confidence("status", 90, _ago(3650), pinned=True) == 90
    assert effective_confidence(
        "convention", 90, _ago(3650), pinned=True,
        contradicted_by=3, subject_changed_at=_ago(1),
    ) == 90


def test_corroboration_is_capped():
    """Ten repetitions of the same claim must not outrank a confirmed fact."""
    one = effective_confidence("convention", 60, _ago(0), source_count=1)
    many = effective_confidence("convention", 60, _ago(0), source_count=64)
    assert many > one
    assert many <= 75, "corroboration bonus must stay bounded"
    assert effective_confidence("convention", 95, _ago(0), source_count=64) <= 100


def test_confidence_is_clamped():
    assert effective_confidence("purpose", 500, _ago(0)) == 100
    assert effective_confidence("purpose", -10, _ago(0)) == 0


def test_missing_timestamp_does_not_decay():
    """A row with no LAST_CONFIRMED_AT must not silently read as ancient."""
    assert effective_confidence("status", 70, None) == 70


def test_entry_standing_names_the_reason():
    from app.services.lore.models import Entry

    contested = Entry(
        id=1, client_code="C", app_code="a", kind="convention", subject="page:x",
        title="t", body="b", last_confirmed_at=_ago(1), contradicted_by=1,
    )
    assert contested.standing == "contested"
    assert contested.to_dict()["standing"] == "contested"

    unverified = Entry(
        id=2, client_code="C", app_code="a", kind="convention", subject="page:x",
        title="t", body="b", last_confirmed_at=_ago(10), subject_changed_at=_ago(1),
    )
    assert unverified.standing == "unverified"

    settled = Entry(
        id=3, client_code="C", app_code="a", kind="convention", subject="page:x",
        title="t", body="b", last_confirmed_at=_ago(10),
    )
    assert settled.standing is None


# ── Redaction ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u",
        "the key is sk-abcdefghijklmnopqrstuvwxyz123456",
        "password: hunter2correcthorse",
        "api_key = rzp_live_ABCDEFGHIJKLMNOP",
    ],
)
def test_redact_removes_credentials(text):
    cleaned = curator.redact(text)
    assert "[redacted]" in cleaned
    # None of the secret-looking runs survive intact.
    for token in text.split():
        if len(token) > 20 and token not in ("password:", "api_key"):
            assert token not in cleaned


def test_redact_leaves_ordinary_text_alone():
    text = "The SLA is four hours and the manager is notified at 08:00."
    assert curator.redact(text) == text


# ── Parsing the model's response ─────────────────────────────────────────


def test_parse_operations_plain_json():
    ops = curator.parse_operations('{"operations":[{"op":"confirm","id":3}]}')
    assert ops == [{"op": "confirm", "id": 3}]


def test_parse_operations_code_fenced():
    raw = 'Sure, here you go:\n```json\n{"operations":[{"op":"confirm","id":7}]}\n```\n'
    assert curator.parse_operations(raw) == [{"op": "confirm", "id": 7}]


def test_parse_operations_with_prose_prefix():
    raw = 'I looked at the observations.\n{"operations":[{"op":"retire","id":9}]}'
    assert curator.parse_operations(raw) == [{"op": "retire", "id": 9}]


def test_parse_operations_bare_list():
    assert curator.parse_operations('[{"op":"confirm","id":1}]') == []


@pytest.mark.parametrize("raw", ["", "   ", "no json here", "{broken", "null"])
def test_parse_operations_survives_garbage(raw):
    """An unparseable response yields no operations and leaves work pending."""
    assert curator.parse_operations(raw) == []


def test_parse_operations_drops_non_dict_items():
    assert curator.parse_operations('{"operations":["nope",{"op":"confirm","id":2}]}') == [
        {"op": "confirm", "id": 2}
    ]


# ── Applying operations ──────────────────────────────────────────────────


class _FakeStore:
    """Records what the curator asked for, so the tests can assert on intent."""

    def __init__(self):
        self.added: list[dict] = []
        self.confirmed: list[int] = []
        self.revised: list[int] = []
        self.status_changes: list[tuple[int, str]] = []
        self.links: list[tuple[int, int, str]] = []
        self._next_id = 100
        self.pinned_ids: set[int] = set()

    async def add_entry(self, client, app, **kw):
        self.added.append(kw)
        self._next_id += 1
        return {"id": self._next_id, "created": True}

    async def confirm_entry(self, entry_id, *, source_ids=(), confidence=None):
        self.confirmed.append(entry_id)

    async def revise_entry(self, entry_id, *, force=False, **kw):
        if entry_id in self.pinned_ids and not force:
            return None
        self.revised.append(entry_id)
        return {"id": entry_id, "version": 2}

    async def set_entry_status(self, entry_id, status, *, superseded_by=None, updated_by=0, force=False):
        if entry_id in self.pinned_ids and status in ("retired", "superseded") and not force:
            return False
        self.status_changes.append((entry_id, status))
        return True

    async def add_link(self, a, b, rel):
        self.links.append((a, b, rel))


@pytest.fixture
def fake_store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(curator, "store", fake)
    return fake


async def _apply(ops, *, batch_ids={1, 2}, known={41, 42}):
    return await curator.apply_operations(
        "SYSTEM", "fieldops", ops, batch_ids=set(batch_ids), known_entry_ids=set(known),
    )


@pytest.mark.asyncio
async def test_add_operation_creates_an_entry(fake_store):
    counters = await _apply([{
        "op": "add", "kind": "convention", "subject": "page:jobsToday",
        "title": "Filters live in Page.filters",
        "body": "Every filter on this page binds under Page.filters so the reset button can clear them in one write.",
        "confidence": 70, "sources": [1],
    }])
    assert counters["added"] == 1
    assert fake_store.added[0]["kind"] == "convention"
    assert fake_store.added[0]["subject"] == "page:jobsToday"
    assert fake_store.added[0]["source_ids"] == [1]


@pytest.mark.asyncio
async def test_add_rejects_unknown_kind(fake_store):
    counters = await _apply([{
        "op": "add", "kind": "vibes", "title": "Something",
        "body": "A body long enough to pass the length check.", "sources": [1],
    }])
    assert counters["rejected"] == 1
    assert not fake_store.added


@pytest.mark.asyncio
async def test_add_rejects_thin_content(fake_store):
    counters = await _apply([
        {"op": "add", "kind": "gotcha", "title": "x", "body": "long enough body here", "sources": [1]},
        {"op": "add", "kind": "gotcha", "title": "A real title", "body": "short", "sources": [1]},
    ])
    assert counters["rejected"] == 2
    assert not fake_store.added


@pytest.mark.asyncio
async def test_confirm_rejects_an_entry_id_that_does_not_exist(fake_store):
    """A hallucinated id must not silently confirm someone else's entry."""
    counters = await _apply([{"op": "confirm", "id": 999, "sources": [1]}])
    assert counters["rejected"] == 1
    assert not fake_store.confirmed


@pytest.mark.asyncio
async def test_confirm_accepts_a_known_entry(fake_store):
    counters = await _apply([{"op": "confirm", "id": 41, "sources": [2]}])
    assert counters["confirmed"] == 1
    assert fake_store.confirmed == [41]


@pytest.mark.asyncio
async def test_invented_source_ids_are_dropped_not_stored(fake_store):
    """Provenance is what makes lore arguable; it must never be fabricated."""
    await _apply([{
        "op": "add", "kind": "decision", "title": "Chose Mongo for jobs",
        "body": "Job documents vary by trade, so the storage is document-shaped rather than tabular.",
        "sources": [1, 7777, 2],
    }])
    assert fake_store.added[0]["source_ids"] == [1, 2]


@pytest.mark.asyncio
async def test_supersede_creates_new_and_marks_old(fake_store):
    counters = await _apply([{
        "op": "supersede", "id": 42, "kind": "decision", "subject": "storage:job",
        "title": "Jobs moved to SQL",
        "body": "The trade-specific fields settled down, so job storage moved to SQL for reporting.",
        "sources": [1],
    }])
    assert counters["added"] == 1
    assert (42, "superseded") in fake_store.status_changes


@pytest.mark.asyncio
async def test_supersede_of_a_pinned_entry_records_a_contradiction(fake_store):
    """A person's pinned entry survives; the disagreement is recorded instead."""
    fake_store.pinned_ids.add(42)
    await _apply([{
        "op": "supersede", "id": 42, "kind": "decision", "subject": "storage:job",
        "title": "Jobs moved to SQL",
        "body": "The trade-specific fields settled down, so job storage moved to SQL for reporting.",
        "sources": [1],
    }])
    assert not fake_store.status_changes
    assert fake_store.links and fake_store.links[0][2] == "contradicts"


@pytest.mark.asyncio
async def test_revise_refuses_a_pinned_entry(fake_store):
    fake_store.pinned_ids.add(41)
    counters = await _apply([{
        "op": "revise", "id": 41, "body": "A revised body that is long enough.", "sources": [1],
    }])
    assert counters["revised"] == 0
    assert counters["rejected"] == 1


@pytest.mark.asyncio
async def test_unknown_operation_is_rejected(fake_store):
    counters = await _apply([{"op": "delete_everything", "id": 41}])
    assert counters["rejected"] == 1


@pytest.mark.asyncio
async def test_add_body_is_redacted_before_storage(fake_store):
    await _apply([{
        "op": "add", "kind": "integration", "title": "Meta ads connection",
        "body": "The connection uses api_key = sk-abcdefghijklmnopqrstuvwxyz123456 for now.",
        "sources": [1],
    }])
    assert "sk-abcdefghij" not in fake_store.added[0]["body"]


# ── Document splitting ───────────────────────────────────────────────────


def test_split_markdown_by_heading():
    doc = "intro text\n\n## First\nbody one\n\n### Second\nbody two"
    sections = ingest._split_markdown(doc)
    assert [h for h, _ in sections] == ["", "First", "Second"]
    assert sections[1][1] == "body one"


def test_split_markdown_without_headings_is_one_section():
    sections = ingest._split_markdown("just a paragraph")
    assert len(sections) == 1
    assert sections[0] == ("", "just a paragraph")


def test_split_markdown_ignores_bare_hashes():
    """A line that is only '#' is not a heading and must not split the doc."""
    sections = ingest._split_markdown("before\n#\nafter")
    assert len(sections) == 1


# ── Briefings ────────────────────────────────────────────────────────────


def _entry(entry_id: int, kind: str, title: str, *, days_old: float = 0,
           confidence: int = 80, subject: str = "app", pinned: bool = False) -> Entry:
    return Entry(
        id=entry_id, client_code="SYSTEM", app_code="fieldops", kind=kind,
        subject=subject, title=title, body=f"Body of {title}.",
        confidence=confidence, pinned=pinned,
        last_confirmed_at=_ago(days_old), first_seen_at=_ago(days_old),
    )


@pytest.mark.asyncio
async def test_brief_on_an_unknown_app_says_so(monkeypatch):
    async def _none(*a, **kw):
        return []
    monkeypatch.setattr(retrieval.store, "list_entries", _none)
    result = await retrieval.brief(_scope())
    assert result["entry_count"] == 0
    assert "nothing recorded" in result["markdown"].lower()


@pytest.mark.asyncio
async def test_brief_orders_constraints_before_history(monkeypatch):
    entries = [
        _entry(1, "decision", "Chose Mongo"),
        _entry(2, "constraint", "SLA is four hours"),
        _entry(3, "purpose", "Dispatches field technicians"),
    ]

    async def _all(*a, **kw):
        return entries
    monkeypatch.setattr(retrieval.store, "list_entries", _all)

    md = (await retrieval.brief(_scope()))["markdown"]
    assert md.index("Dispatches field technicians") < md.index("SLA is four hours")
    assert md.index("SLA is four hours") < md.index("Chose Mongo")


@pytest.mark.asyncio
async def test_brief_marks_stale_entries_as_unverified(monkeypatch):
    entries = [_entry(1, "status", "Building the invoice screen", days_old=120)]

    async def _all(*a, **kw):
        return entries
    monkeypatch.setattr(retrieval.store, "list_entries", _all)

    md = (await retrieval.brief(_scope()))["markdown"]
    assert "unverified" in md


@pytest.mark.asyncio
async def test_brief_marks_pinned_entries_as_human_confirmed(monkeypatch):
    entries = [_entry(1, "constraint", "Technicians never see pricing", pinned=True)]

    async def _all(*a, **kw):
        return entries
    monkeypatch.setattr(retrieval.store, "list_entries", _all)

    md = (await retrieval.brief(_scope()))["markdown"]
    assert "confirmed by a person" in md


@pytest.mark.asyncio
async def test_brief_respects_its_character_budget(monkeypatch):
    entries = [
        _entry(i, kind, f"{kind} number {i}")
        for kind in ("purpose", "constraint", "convention", "decision", "gotcha")
        for i in range(1, 12)
    ]

    async def _all(*a, **kw):
        return entries
    monkeypatch.setattr(retrieval.store, "list_entries", _all)

    result = await retrieval.brief(_scope(), budget=700)
    assert len(result["markdown"]) < 1200  # budget plus the truncation notice
    assert result["truncated"] is True
    assert "not shown" in result["markdown"]


@pytest.mark.asyncio
async def test_brief_admits_what_the_per_kind_cap_left_out(monkeypatch):
    """A cap that silently trims reads as 'this is everything'. It must not."""
    entries = [_entry(i, "convention", f"Convention number {i}") for i in range(1, 40)]

    async def _all(*a, **kw):
        return entries
    monkeypatch.setattr(retrieval.store, "list_entries", _all)

    result = await retrieval.brief(_scope(), budget=100000)
    assert result["truncated"] is True
    assert result["rendered"] == retrieval.BRIEF_CAPS["convention"]
    assert "more, lower confidence" in result["markdown"]
    assert "31 of 39 entries not shown" in result["markdown"]


# ── Row mapping ──────────────────────────────────────────────────────────


def test_entry_from_row_parses_json_tags_stored_as_text():
    entry = Entry.from_row({
        "ID": 5, "CLIENT_CODE": "SYSTEM", "APP_CODE": "fieldops", "KIND": "convention",
        "SUBJECT": "app", "TITLE": "t", "BODY": "b", "TAGS": '["naming","theme"]',
        "CONFIDENCE": 70, "STATUS": "active", "SUPERSEDED_BY": None, "SOURCE_COUNT": 2,
        "VERSION": 1, "PINNED": 0, "FIRST_SEEN_AT": None, "LAST_CONFIRMED_AT": None,
        "UPDATED_AT": None,
    })
    assert entry.tags == ["naming", "theme"]
    assert entry.pinned is False


def test_observation_from_row_survives_bad_json_meta():
    obs = Observation.from_row({
        "ID": 1, "CLIENT_CODE": "SYSTEM", "APP_CODE": "fieldops", "KIND": "chat",
        "SOURCE": "s", "SUBJECT": "app", "BODY": "b", "META": "{not json",
        "SEEN_COUNT": 1, "OBSERVED_BY": 0, "OBSERVED_AT": None, "LAST_SEEN_AT": None,
        "CURATED_AT": None,
    })
    assert obs.meta == {}


def test_only_time_bound_kinds_have_a_half_life():
    """Adding a half-life to a durable kind reintroduces silent erosion."""
    from app.services.lore.models import TIME_BOUND_HALF_LIFE_DAYS
    assert set(TIME_BOUND_HALF_LIFE_DAYS) == {"status", "owner"}
    assert set(TIME_BOUND_HALF_LIFE_DAYS) <= set(ENTRY_KINDS)


def test_all_entry_kinds_are_placed_in_the_brief_order():
    from app.services.lore.models import BRIEF_ORDER
    assert set(ENTRY_KINDS) == set(BRIEF_ORDER)


# ── Direct authoring (the tribal-knowledge path) ─────────────────────────


class _AuthoringStore(_FakeStore):
    """Adds the bits `lore_add` and `lore_correct` touch."""

    def __init__(self):
        super().__init__()
        self.pin_calls: list[tuple[int, bool]] = []
        self.entries: dict[int, Entry] = {}
        self.duplicate_of: int | None = None

    async def add_entry(self, client, app, **kw):
        self.added.append(kw)
        if self.duplicate_of is not None:
            return {"id": self.duplicate_of, "created": False}
        self._next_id += 1
        return {"id": self._next_id, "created": True}

    async def set_pinned(self, entry_id, pinned, *, updated_by=0):
        self.pin_calls.append((entry_id, pinned))
        if pinned:
            self.pinned_ids.add(entry_id)
        else:
            self.pinned_ids.discard(entry_id)
        return True

    async def get_entry(self, entry_id):
        return self.entries.get(entry_id)


@pytest.fixture
def authoring_store(monkeypatch):
    from app.services.lore import tools as lore_tools
    fake = _AuthoringStore()
    monkeypatch.setattr(lore_tools, "store", fake)

    async def _fake_resolve(auth, app_code, **kw):
        return _scope(client="SYSTEM", chain=("SYSTEM",))
    monkeypatch.setattr(lore_tools.access, "resolve_scope", _fake_resolve)
    return fake


_CTX = {"app_code": "fieldops", "headers": {"clientCode": "SYSTEM"}}


async def _add(**params):
    from app.services.lore import tools as lore_tools
    return await lore_tools._add(params, _CTX)


@pytest.mark.asyncio
async def test_lore_add_creates_a_pinned_entry(authoring_store):
    """A stated fact must be recorded now, not queued behind a curation pass."""
    result = await _add(
        kind="constraint", subject="app",
        title="Technicians never see customer pricing",
        body="A hard rule from the client; the pricing column is gated on the Manager role.",
    )
    assert result.success
    assert authoring_store.added[0]["pinned"] is True
    assert authoring_store.added[0]["kind"] == "constraint"
    # Pinned explicitly as well, so a duplicate-collapse cannot lose the pin.
    assert authoring_store.pin_calls == [(result.data["entry_id"], True)]


@pytest.mark.asyncio
async def test_lore_add_pins_even_when_the_entry_already_existed(authoring_store):
    """add_entry collapses an identical body into a confirmation, which would
    otherwise silently drop the pin the author asked for."""
    authoring_store.duplicate_of = 41
    result = await _add(
        kind="convention", title="Filters live under Page.filters",
        body="Every filter on every page binds under Page.filters so reset can clear them in one write.",
    )
    assert result.success
    assert result.data["created"] is False
    assert (41, True) in authoring_store.pin_calls
    assert "already recorded" in result.summary


@pytest.mark.asyncio
async def test_lore_add_rejects_an_unknown_kind(authoring_store):
    result = await _add(kind="vibes", title="Something true", body="A body long enough to pass.")
    assert not result.success
    assert "kind must be one of" in result.error
    assert not authoring_store.added


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title,body",
    [("ab", "a body that is long enough to pass"), ("A real title", "short")],
)
async def test_lore_add_rejects_thin_content(authoring_store, title, body):
    result = await _add(kind="gotcha", title=title, body=body)
    assert not result.success
    assert not authoring_store.added


@pytest.mark.asyncio
async def test_lore_add_redacts_secrets(authoring_store):
    await _add(
        kind="integration", title="Meta ads connection",
        body="It authenticates with api_key = sk-abcdefghijklmnopqrstuvwxyz123456 for now.",
    )
    assert "sk-abcdefghij" not in authoring_store.added[0]["body"]


@pytest.mark.asyncio
async def test_lore_add_strips_a_trailing_full_stop_from_the_title(authoring_store):
    await _add(
        kind="glossary", title="A job is one technician visit.",
        body="Not one work order. A work order can produce several jobs.",
    )
    assert authoring_store.added[0]["title"] == "A job is one technician visit"


@pytest.mark.asyncio
async def test_lore_add_needs_tenant_context():
    from app.services.lore import tools as lore_tools
    result = await lore_tools._add(
        {"kind": "purpose", "title": "Something", "body": "A long enough body here."},
        {},
    )
    assert not result.success
    assert "tenant context" in result.error


# ── Pinning protects against the curator, not against people ─────────────


@pytest.mark.asyncio
async def test_curator_cannot_revise_a_pinned_entry(fake_store):
    fake_store.pinned_ids.add(41)
    counters = await _apply([{
        "op": "revise", "id": 41, "body": "A revised body that is long enough.", "sources": [1],
    }])
    assert counters["revised"] == 0


@pytest.mark.asyncio
async def test_a_person_can_revise_a_pinned_entry(fake_store):
    """The human edit path passes force=True; the curator never does."""
    fake_store.pinned_ids.add(41)
    assert await fake_store.revise_entry(41, force=True, body="new") is not None


@pytest.mark.asyncio
async def test_a_person_can_retire_a_pinned_entry(fake_store):
    fake_store.pinned_ids.add(41)
    assert await fake_store.set_entry_status(41, "retired") is False
    assert await fake_store.set_entry_status(41, "retired", force=True) is True


# ══════════════════════════════════════════════════════════════════════════
# Multi-tenancy: who may change lore, and whose knowledge they see.
#
# The scenario throughout: SYSTEM owns app `fieldops`; CLIENTA has edit access.
# CLIENTA users must see SYSTEM's knowledge, must be able to change what THEY
# know without changing what SYSTEM knows, and must never write a SYSTEM row.
# ══════════════════════════════════════════════════════════════════════════


def _entry_at(
    entry_id: int, client: str, *, kind: str = "constraint", title: str = "t",
    base: int | None = None, status: str = "active", subject: str = "app",
) -> Entry:
    return Entry(
        id=entry_id, client_code=client, app_code="fieldops", kind=kind,
        subject=subject, title=title, body=f"Body of {title}.",
        status=status, base_entry_id=base, last_confirmed_at=_ago(0),
    )


CHAIN = ("SYSTEM", "CLIENTA")


# ── Rule 1: writes land under the logged-in user's client ────────────────


def test_scope_write_client_is_the_caller_never_the_owner():
    scope = _scope(client="CLIENTA", chain=CHAIN)
    assert scope.client_code == "CLIENTA"
    assert scope.base_client == "SYSTEM"
    assert scope.is_override is True


def test_scope_of_the_owner_has_no_base():
    scope = _scope(client="SYSTEM", chain=("SYSTEM",))
    assert scope.base_client is None
    assert scope.is_override is False


def test_scope_owns_only_its_own_entries():
    scope = _scope(client="CLIENTA", chain=CHAIN)
    assert scope.owns("CLIENTA") is True
    assert scope.owns("SYSTEM") is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        (["SYSTEM"], ("SYSTEM", "CLIENTA")),          # caller appended
        (["SYSTEM", "CLIENTA"], ("SYSTEM", "CLIENTA")),
        (["CLIENTA"], ("CLIENTA",)),                   # caller only
        (["SYSTEM", "CLIENTA", "SYSTEM"], ("SYSTEM", "CLIENTA")),  # deduped
        (["SYSTEM", "", None, "CLIENTA"], ("SYSTEM", "CLIENTA")),  # blanks dropped
        (["CLIENTA", "SYSTEM"], ("CLIENTA", "SYSTEM", "CLIENTA")[:2] + ("CLIENTA",)),
    ],
)
def test_chain_always_ends_with_the_caller(raw, expected):
    """A caller must always be able to see and write their own lore, whatever
    shape security returns."""
    from app.services.lore.access import _normalise_chain
    assert _normalise_chain([r for r in raw if r is not None], "CLIENTA")[-1] == "CLIENTA"


# ── Rule 2: app edit access is required ─────────────────────────────────


def test_read_only_access_refuses_writes():
    scope = _scope(client="CLIENTA", chain=CHAIN, can_read=True, can_write=False)
    scope.require_read()                      # fine
    with pytest.raises(LoreAccessError) as exc:
        scope.require_write()
    assert exc.value.status == 403
    assert "edit access" in exc.value.message


def test_no_read_access_refuses_reads_and_writes():
    scope = _scope(client="OTHER", chain=("SYSTEM", "OTHER"), can_read=False, can_write=False)
    with pytest.raises(LoreAccessError):
        scope.require_read()
    with pytest.raises(LoreAccessError):
        scope.require_write()


@pytest.mark.asyncio
async def test_tools_refuse_to_write_without_edit_access(monkeypatch, authoring_store):
    """The hole this closes: before access checks, any authenticated user could
    write knowledge into any app whose code they could guess."""
    from app.services.lore import tools as lore_tools

    async def _read_only(auth, app_code, **kw):
        return _scope(client="CLIENTA", chain=CHAIN, can_write=False)
    monkeypatch.setattr(lore_tools.access, "resolve_scope", _read_only)

    result = await lore_tools._add(
        {"kind": "constraint", "title": "Something true",
         "body": "A body long enough to be recorded."},
        _CTX,
    )
    assert not result.success
    assert "edit access" in result.error
    assert not authoring_store.added


@pytest.mark.asyncio
async def test_tools_still_allow_reads_without_edit_access(monkeypatch):
    from app.services.lore import tools as lore_tools

    async def _read_only(auth, app_code, **kw):
        return _scope(client="CLIENTA", chain=CHAIN, can_write=False)
    monkeypatch.setattr(lore_tools.access, "resolve_scope", _read_only)

    async def _none(*a, **kw):
        return []
    monkeypatch.setattr(retrieval.store, "list_entries", _none)

    result = await lore_tools._brief({}, _CTX)
    assert result.success


@pytest.mark.asyncio
async def test_a_failure_to_reach_security_fails_closed(monkeypatch):
    """Guessing that the caller probably has access is not an acceptable
    default for knowledge about somebody else's application."""
    from app.services.lore import access as lore_access
    lore_access.invalidate()

    async def _boom(app_code, client_code):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(lore_access, "_fetch_scope", _boom)

    with pytest.raises(LoreAccessError) as exc:
        await lore_access.resolve_scope(_scope(client="CLIENTA"), "fieldops")
    assert exc.value.status == 503
    lore_access.invalidate()


# ── Rule 3: overrides. CLIENTA sees SYSTEM's knowledge plus its own ──────


def test_inherited_entries_are_visible_and_marked():
    resolved = store.resolve_overrides(
        [_entry_at(1, "SYSTEM", title="Owner rule"), _entry_at(2, "CLIENTA", title="Our rule")],
        CHAIN,
    )
    by_title = {e.title: e for e in resolved}
    assert by_title["Owner rule"].inherited is True
    assert by_title["Our rule"].inherited is False


def test_an_override_shadows_the_base_entry():
    base = _entry_at(1, "SYSTEM", title="SLA is four hours")
    fork = _entry_at(2, "CLIENTA", title="SLA is two hours", base=1)
    resolved = store.resolve_overrides([base, fork], CHAIN)
    assert [e.id for e in resolved] == [2]
    assert resolved[0].title == "SLA is two hours"


def test_a_retired_override_hides_the_base_for_that_client_only():
    """The tombstone. CLIENTA stops seeing it; SYSTEM is untouched."""
    base = _entry_at(1, "SYSTEM", title="Applies to everyone")
    tombstone = _entry_at(2, "CLIENTA", base=1, status="retired")

    for_clienta = store.resolve_overrides([base, tombstone], CHAIN)
    assert [e.id for e in for_clienta] == [2]
    assert for_clienta[0].status == "retired"   # filtered out by the status filter

    for_system = store.resolve_overrides([base], ("SYSTEM",))
    assert [e.id for e in for_system] == [1]
    assert for_system[0].status == "active"


def test_a_base_client_cannot_override_a_later_clients_entry():
    """Inheritance only flows downward. A row from the OWNER claiming to
    override a CLIENT's row is nonsense and must be ignored."""
    theirs = _entry_at(1, "CLIENTA", title="CLIENTA's own")
    bogus = _entry_at(2, "SYSTEM", title="Owner trying to shadow it", base=1)
    resolved = store.resolve_overrides([theirs, bogus], CHAIN)
    assert 1 in {e.id for e in resolved}


def test_an_override_whose_base_is_out_of_scope_still_stands():
    """A filtered query can return the fork without its base. Dropping it would
    silently lose the client's own knowledge."""
    fork = _entry_at(2, "CLIENTA", title="Ours", base=999)
    resolved = store.resolve_overrides([fork], CHAIN)
    assert [e.id for e in resolved] == [2]


def test_entries_with_no_override_pass_through_unchanged():
    a, b = _entry_at(1, "SYSTEM"), _entry_at(2, "CLIENTA")
    assert {e.id for e in store.resolve_overrides([a, b], CHAIN)} == {1, 2}


def test_resolve_overrides_on_an_empty_set():
    assert store.resolve_overrides([], CHAIN) == []


@pytest.mark.asyncio
async def test_editing_an_inherited_entry_forks_it(monkeypatch):
    """CLIENTA correcting a SYSTEM rule must not touch SYSTEM's row."""
    calls: dict[str, Any] = {}

    async def _add_entry(client, app, **kw):
        calls["add"] = {"client": client, **kw}
        return {"id": 500, "created": True}

    async def _revise(*a, **kw):
        calls["revised"] = True
        return {"id": a[0], "version": 2}

    async def _query(*a, **kw):
        return []

    async def _link(*a, **kw):
        return None

    monkeypatch.setattr(store, "add_entry", _add_entry)
    monkeypatch.setattr(store, "revise_entry", _revise)
    monkeypatch.setattr(store, "execute_query", _query)
    monkeypatch.setattr(store, "add_link", _link)

    scope = _scope(client="CLIENTA", chain=CHAIN)
    outcome = await store.edit_in_scope(
        _entry_at(1, "SYSTEM", title="SLA is four hours"), scope,
        body="Actually two hours for us.", updated_by=7,
    )
    assert outcome["action"] == "forked"
    assert outcome["overrides"] == 1
    assert "revised" not in calls, "must not revise the owner's row"
    assert calls["add"]["client"] == "CLIENTA"
    assert calls["add"]["base_entry_id"] == 1


@pytest.mark.asyncio
async def test_editing_your_own_entry_revises_it_in_place(monkeypatch):
    calls: dict[str, Any] = {}

    async def _revise(entry_id, **kw):
        calls["revised"] = entry_id
        return {"id": entry_id, "version": 3}

    async def _add_entry(*a, **kw):
        calls["added"] = True
        return {"id": 999, "created": True}

    monkeypatch.setattr(store, "revise_entry", _revise)
    monkeypatch.setattr(store, "add_entry", _add_entry)

    scope = _scope(client="CLIENTA", chain=CHAIN)
    outcome = await store.edit_in_scope(
        _entry_at(4, "CLIENTA"), scope, body="Updated body here.", updated_by=7,
    )
    assert outcome["action"] == "revised"
    assert calls["revised"] == 4
    assert "added" not in calls


@pytest.mark.asyncio
async def test_retiring_an_inherited_entry_writes_a_tombstone(monkeypatch):
    captured: dict[str, Any] = {}

    async def _add_entry(client, app, **kw):
        captured.update({"client": client, **kw})
        return {"id": 501, "created": True}

    async def _status(*a, **kw):
        captured["status_touched"] = a
        return True

    async def _query(*a, **kw):
        return []

    monkeypatch.setattr(store, "add_entry", _add_entry)
    monkeypatch.setattr(store, "set_entry_status", _status)
    monkeypatch.setattr(store, "execute_query", _query)

    scope = _scope(client="CLIENTA", chain=CHAIN)
    outcome = await store.retire_in_scope(_entry_at(1, "SYSTEM"), scope, updated_by=7)
    assert outcome["action"] == "hidden"
    assert captured["client"] == "CLIENTA"
    assert captured["status"] == "retired"
    assert captured["base_entry_id"] == 1
    assert "status_touched" not in captured, "must not retire the owner's row"


# ── Rule 4: the agent supplies lore to the LLM ──────────────────────────


class _FakeSession:
    def __init__(self, client="CLIENTA", app="fieldops"):
        self.auth = type("A", (), {"client_code": client, "app_code": app})()
        self.context: dict[str, Any] = {"app_code": app}


@pytest.mark.asyncio
async def test_big_picture_is_pushed_into_the_system_prompt(monkeypatch):
    """Only when LORE_PUSH_BRIEF is on, which it is not by default.

    The push is off because it delivered a ranked fraction: measured on two
    seeded apps a 3,800-character briefing rendered 5 of 21 entries and 7 of
    21. The model reaches all of it through `lore_index` instead. The push is
    kept behind a flag rather than deleted, so this asserts the rendering still
    works when it is switched on.
    """
    from app.config import settings as _settings
    from app.services.lore import context as lore_context

    monkeypatch.setattr(_settings, "LORE_PUSH_BRIEF", True)

    async def _resolve(auth, app_code, **kw):
        return _scope(client="CLIENTA", chain=CHAIN)

    async def _brief(scope, **kw):
        return {"entry_count": 3, "markdown": "# fieldops\n\n## Rules\n- Never show pricing"}

    monkeypatch.setattr(lore_context.access, "resolve_scope", _resolve)
    monkeypatch.setattr(lore_context.retrieval, "brief", _brief)

    out = await lore_context.big_picture(_FakeSession())
    assert "Never show pricing" in out
    assert "already known about this app" in out
    # An overriding client is told whose knowledge it is looking at.
    assert "inherited from SYSTEM" in out
    assert "saved as CLIENTA's" in out


@pytest.mark.asyncio
async def test_big_picture_is_empty_when_nothing_is_known(monkeypatch):
    from app.services.lore import context as lore_context

    async def _resolve(auth, app_code, **kw):
        return _scope(client="SYSTEM")

    async def _brief(scope, **kw):
        return {"entry_count": 0, "markdown": "nothing here"}

    monkeypatch.setattr(lore_context.access, "resolve_scope", _resolve)
    monkeypatch.setattr(lore_context.retrieval, "brief", _brief)
    assert await lore_context.big_picture(_FakeSession()) == ""


@pytest.mark.asyncio
async def test_big_picture_is_silent_without_read_access(monkeypatch):
    """An agent must not narrate an app's knowledge to a user who cannot see it."""
    from app.services.lore import context as lore_context

    async def _resolve(auth, app_code, **kw):
        return _scope(client="OTHER", can_read=False)
    monkeypatch.setattr(lore_context.access, "resolve_scope", _resolve)
    assert await lore_context.big_picture(_FakeSession()) == ""


@pytest.mark.asyncio
async def test_big_picture_survives_lore_being_broken(monkeypatch):
    from app.services.lore import context as lore_context

    async def _boom(auth, app_code, **kw):
        raise RuntimeError("database on fire")
    monkeypatch.setattr(lore_context.access, "resolve_scope", _boom)
    assert await lore_context.big_picture(_FakeSession()) == ""


@pytest.mark.parametrize(
    "tool,params,expected",
    [
        ("get_page", {"page_name": "jobsToday"}, "page:jobsToday"),
        ("update_storage", {"storage_name": "job"}, "storage:job"),
        ("save_function", {"functionName": "notifyLateJobs"}, "function:notifyLateJobs"),
        ("list_pages", {}, None),
        ("get_page", {"page_name": "   "}, None),
        ("get_page", {"page_name": 42}, None),
        ("weird", {"page_name": "has spaces and (parens)"}, None),
    ],
)
def test_focus_is_derived_from_tool_inputs(tool, params, expected):
    from app.services.lore import context as lore_context
    assert lore_context.subject_from_tool_call(tool, params) == expected


def test_focus_prefers_the_page_when_a_tool_names_several_objects():
    from app.services.lore import context as lore_context
    subject = lore_context.subject_from_tool_call(
        "bind", {"storage_name": "job", "page_name": "jobsToday"},
    )
    assert subject == "page:jobsToday"


def test_small_picture_is_pushed_once_per_subject():
    """Repeating the same block every turn would spend the whole reminder
    budget restating what was said three turns ago."""
    from app.services.lore import context as lore_context
    session = _FakeSession()

    lore_context.note_focus(session, "get_page", {"page_name": "jobsToday"})
    assert lore_context.take_unsent_focus(session) == "page:jobsToday"
    assert lore_context.take_unsent_focus(session) is None

    lore_context.note_focus(session, "get_storage", {"storage_name": "job"})
    assert lore_context.take_unsent_focus(session) == "storage:job"


@pytest.mark.asyncio
async def test_small_picture_says_nothing_for_the_app_subject(monkeypatch):
    """The big picture already covers app level; repeating it is waste."""
    from app.services.lore import context as lore_context
    assert await lore_context.small_picture(_FakeSession(), "app") == ""


@pytest.mark.asyncio
async def test_nothing_is_pushed_by_default(monkeypatch):
    """The default is silence: an app's knowledge is pulled, not pushed."""
    from app.services.lore import context as lore_context

    async def _boom(*a, **kw):  # must not even be reached
        raise AssertionError("big_picture resolved scope while the push was off")

    monkeypatch.setattr(lore_context.access, "resolve_scope", _boom)
    assert await lore_context.big_picture(_FakeSession()) == ""


# ── search says what it did NOT match ────────────────────────────────────


def test_question_words_are_not_content_terms():
    """"how are phone numbers stored" was answered with an entry about partner
    onboarding, matching entirely on `stored`. The question words are what let
    a full-text match drift onto whatever shares them."""
    from app.services.lore.retrieval import _content_terms

    assert _content_terms("how are phone numbers stored") == ["phone", "numbers", "stored"]
    assert _content_terms("what did we decide about stages") == ["decide", "stages"]
    assert _content_terms("") == []
    # Identifiers survive intact; they are the most identifying thing a query has.
    assert "dealsoptimized" in _content_terms("where is dealsOptimized used")


def test_term_coverage_reads_title_body_and_subject():
    from app.services.lore.models import Entry
    from app.services.lore.retrieval import _term_coverage

    entry = Entry(
        id=1, client_code="SYSTEM", app_code="leadzump", kind="gotcha",
        subject="page:dealsOptimized", title="A fork that shares component UUIDs",
        body="It is deals with the overlays pruned.",
    )
    assert _term_coverage(entry, ["fork", "uuids"]) == ["fork", "uuids"]
    assert _term_coverage(entry, ["dealsoptimized"]) == ["dealsoptimized"]   # subject
    assert _term_coverage(entry, ["whatsapp"]) == []


@pytest.mark.asyncio
async def test_search_drops_entries_matching_no_content_term(monkeypatch):
    """A search about pipeline stages returned an entry on forked pages at
    4.76 with zero terms in common. Whatever it scored, it is not an answer."""
    from app.services.lore import retrieval
    from app.services.lore.models import Entry

    good = Entry(id=1, client_code="SYSTEM", app_code="a", kind="glossary",
                 subject="app", title="Stages and statuses",
                 body="A pipeline is ordered stages.")
    unrelated = Entry(id=2, client_code="SYSTEM", app_code="a", kind="gotcha",
                      subject="app", title="A fork sharing component keys",
                      body="Nothing to do with the question.")

    async def _search_entries(*a, **kw):
        return [(unrelated, 9.9), (good, 1.0)]   # the noise scores higher

    async def _annotate(entries):
        return list(entries)

    monkeypatch.setattr(retrieval.store, "search_entries", _search_entries)
    monkeypatch.setattr(retrieval.store, "annotate_standing", _annotate)

    result = await retrieval.search(_scope(client="SYSTEM", chain=("SYSTEM",)), "stages")
    assert [r["id"] for r in result["results"]] == [1]
    assert result["missing_terms"] == []


@pytest.mark.asyncio
async def test_search_reports_terms_no_entry_mentions(monkeypatch):
    """The signal that results are about something else, however good the
    scores look. This is what the tool leads its summary with."""
    from app.services.lore import retrieval
    from app.services.lore.models import Entry

    entry = Entry(id=1, client_code="SYSTEM", app_code="a", kind="integration",
                  subject="app", title="Onboarding is stored separately",
                  body="Partner config lives in its own storage.")

    async def _search_entries(*a, **kw):
        return [(entry, 4.76)]

    async def _annotate(entries):
        return list(entries)

    monkeypatch.setattr(retrieval.store, "search_entries", _search_entries)
    monkeypatch.setattr(retrieval.store, "annotate_standing", _annotate)

    result = await retrieval.search(
        _scope(client="SYSTEM", chain=("SYSTEM",)), "how are phone numbers stored")
    assert result["count"] == 1
    assert result["results"][0]["matched_terms"] == ["stored"]
    assert result["missing_terms"] == ["phone", "numbers"]
