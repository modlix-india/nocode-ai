"""Transport: the format, and the per-client merge rules.

The merge itself is `store.edit_in_scope` and `store.retire_in_scope`, which
are already tested. What is tested here is the layer above: given a document
row and what is in the database, which of those does the importer call, and
does it refuse the things that would quietly destroy knowledge.

No database and no LLM, like the rest of the lore suite.
"""

from __future__ import annotations

import pytest

from app.services.lore import transport
from app.services.lore.models import Entry


def _doc(**over) -> dict:
    base = {
        "format": "lore_transport/v1",
        "app_code": "fieldops",
        "client_code": "SYSTEM",
        "source": "seed:fieldops/v1",
        "entries": [{
            "key": "rule-one",
            "kind": "constraint",
            "subject": "app",
            "title": "A technician never sees pricing",
            "body": "Enforced on the storage, not in the page.",
        }],
    }
    base.update(over)
    return base


def _entry(**over) -> Entry:
    kw = dict(
        id=1, client_code="SYSTEM", app_code="fieldops", kind="constraint",
        subject="app", title="A technician never sees pricing",
        body="Enforced on the storage, not in the page.",
        seed_key="rule-one", seed_source="seed:fieldops/v1",
    )
    kw.update(over)
    return Entry(**kw)


class _Scope:
    """Stands in for LoreScope with the two behaviours plan() reads."""

    def __init__(self, client_code="SYSTEM", chain=("SYSTEM",)):
        self.client_code = client_code
        self.app_code = "fieldops"
        self.read_chain = tuple(chain)

    def owns(self, entry_client_code: str) -> bool:
        return entry_client_code == self.client_code

    @property
    def is_override(self) -> bool:
        return len(self.read_chain) > 1


@pytest.fixture
def rows(monkeypatch):
    """Control what store.list_entries returns, with no database."""
    holder: list[Entry] = []

    async def _list_entries(chain, app_code, **kw):
        return list(holder)

    monkeypatch.setattr(transport.store, "list_entries", _list_entries)
    return holder


# ── Format ───────────────────────────────────────────────────────────────


def test_parse_accepts_a_minimal_document():
    doc = transport.parse(_doc())
    assert doc.app_code == "fieldops"
    assert len(doc.entries) == 1
    assert doc.entries[0].key == "rule-one"


def test_parse_reads_yaml_and_json_with_one_parser():
    yaml_text = (
        "format: lore_transport/v1\napp_code: fieldops\nclient_code: SYSTEM\n"
        "entries:\n  - kind: purpose\n    title: What it is\n"
        "    body: A field service application.\n"
    )
    assert len(transport.parse(yaml_text).entries) == 1
    import json
    assert len(transport.parse(json.dumps(_doc())).entries) == 1


def test_parse_rejects_an_unknown_format():
    with pytest.raises(transport.TransportError, match="unrecognised format"):
        transport.parse(_doc(format="lore_transport/v99"))


def test_parse_rejects_an_unknown_kind():
    d = _doc()
    d["entries"][0]["kind"] = "vibe"
    with pytest.raises(transport.TransportError, match="kind"):
        transport.parse(d)


def test_parse_rejects_a_subject_that_would_silently_degrade():
    """The failure this prevents is the quiet one.

    normalise_subject turns an unrecognised type into "app" without
    complaining, so a typo files the entry where lore_about will never look.
    """
    d = _doc()
    d["entries"][0]["subject"] = "form:TextBox"
    with pytest.raises(transport.TransportError, match="not a recognised subject"):
        transport.parse(d)


def test_parse_rejects_duplicate_keys():
    d = _doc()
    d["entries"].append(dict(d["entries"][0]))
    with pytest.raises(transport.TransportError, match="duplicate key"):
        transport.parse(d)


def test_parse_rejects_a_link_relation_that_is_not_portable():
    """`supersedes` carries a local SUPERSEDED_BY pointer.

    Importing one would assert a supersession event that never happened here.
    """
    d = _doc()
    d["links"] = [{"from_key": "rule-one", "to_key": "rule-one", "rel": "supersedes"}]
    with pytest.raises(transport.TransportError, match="not portable"):
        transport.parse(d)


def test_parse_rejects_an_overrides_key_that_is_not_in_the_document():
    d = _doc()
    d["entries"][0]["overrides_key"] = "nowhere"
    with pytest.raises(transport.TransportError, match="not in this document"):
        transport.parse(d)


def test_a_derived_key_is_stable_across_wording_of_whitespace():
    a = transport.derive_key("constraint", "app", "A  technician never sees   pricing")
    b = transport.derive_key("constraint", "app", "a technician never sees pricing")
    assert a == b


def test_a_derived_key_changes_with_the_subject():
    assert (transport.derive_key("constraint", "app", "t")
            != transport.derive_key("constraint", "page:x", "t"))


# ── Refusals that protect knowledge ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_resolved_document_is_refused(rows):
    """A flattened export imported into a client would turn every inherited
    row into an owned copy and break the override model for that app."""
    doc = transport.parse(_doc(resolved=True))
    with pytest.raises(transport.TransportError, match="resolved=true"):
        await transport.plan(_Scope(), doc, mode="merge")


@pytest.mark.asyncio
async def test_replace_mode_is_refused(rows):
    doc = transport.parse(_doc())
    with pytest.raises(transport.TransportError, match="not supported"):
        await transport.plan(_Scope(), doc, mode="replace")


# ── The merge table ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nothing_here_yet_is_an_add(rows):
    plan = await transport.plan(_Scope(), transport.parse(_doc()), mode="merge")
    assert [a.action for a in plan.actions] == ["add"]


@pytest.mark.asyncio
async def test_my_own_changed_row_is_revised(rows):
    rows.append(_entry(body="Something older."))
    plan = await transport.plan(_Scope(), transport.parse(_doc()), mode="merge")
    assert [a.action for a in plan.actions] == ["revise"]
    assert plan.actions[0].entry_id == 1


@pytest.mark.asyncio
async def test_my_own_identical_row_is_skipped(rows):
    rows.append(_entry())
    plan = await transport.plan(_Scope(), transport.parse(_doc()), mode="merge")
    assert [a.action for a in plan.actions] == ["skip"]


@pytest.mark.asyncio
async def test_an_inherited_row_that_differs_forks(rows):
    rows.append(_entry(id=7, client_code="SYSTEM", body="The owner's older wording."))
    scope = _Scope(client_code="CLIENTA", chain=("SYSTEM", "CLIENTA"))
    plan = await transport.plan(scope, transport.parse(_doc()), mode="merge")
    assert [a.action for a in plan.actions] == ["fork"]
    assert plan.actions[0].base_entry_id == 7


@pytest.mark.asyncio
async def test_an_inherited_row_that_is_identical_is_skipped_not_forked(rows):
    """Forking an identical body gives this client a private copy of something
    it already inherits, and the owner's later corrections stop reaching it.
    That is how importing one shared seed into every client destroys the
    inheritance it was meant to use."""
    rows.append(_entry(id=7, client_code="SYSTEM"))
    scope = _Scope(client_code="CLIENTA", chain=("SYSTEM", "CLIENTA"))
    plan = await transport.plan(scope, transport.parse(_doc()), mode="merge")
    assert [a.action for a in plan.actions] == ["skip"]
    assert "already inherited" in plan.actions[0].reason


@pytest.mark.asyncio
async def test_an_already_forked_row_revises_the_fork_not_the_base(rows):
    rows.append(_entry(id=7, client_code="SYSTEM", body="The owner's wording."))
    rows.append(_entry(id=8, client_code="CLIENTA", base_entry_id=7, body="Our wording."))
    scope = _Scope(client_code="CLIENTA", chain=("SYSTEM", "CLIENTA"))
    plan = await transport.plan(scope, transport.parse(_doc()), mode="merge")
    assert [a.action for a in plan.actions] == ["revise"]
    assert plan.actions[0].entry_id == 8, "must target the fork, never the base"
    # And the caller is told their override now shadows a moved base.
    assert len(plan.shadowed) == 1
    assert plan.shadowed[0].base_entry_id == 7


@pytest.mark.asyncio
async def test_a_tombstone_for_a_missing_base_is_skipped_not_fabricated(rows):
    """resolve_overrides treats an override with an absent base as standing on
    its own, so a dangling retired override hides nothing while looking like a
    deliberate retirement."""
    d = _doc()
    d["entries"][0]["status"] = "retired"
    plan = await transport.plan(_Scope(), transport.parse(d), mode="merge")
    assert [a.action for a in plan.actions] == ["skip"]
    assert "not here" in plan.actions[0].reason


@pytest.mark.asyncio
async def test_a_tombstone_retires_an_inherited_row_for_this_client_only(rows):
    rows.append(_entry(id=7, client_code="SYSTEM"))
    d = _doc()
    d["entries"][0]["status"] = "retired"
    scope = _Scope(client_code="CLIENTA", chain=("SYSTEM", "CLIENTA"))
    plan = await transport.plan(scope, transport.parse(d), mode="merge")
    assert [a.action for a in plan.actions] == ["retire"]
    assert "this client only" in plan.actions[0].reason


# ── What the document does not mention ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_row_a_person_wrote_is_never_touched(rows):
    """A row with no SEED_KEY was authored here, by a person or the curator.
    No mode may retire it — the file is not authoritative over local work."""
    rows.append(_entry(id=9, seed_key=None, seed_source=None,
                       title="Something a person wrote", body="Local knowledge."))
    for mode in ("merge", "sync"):
        plan = await transport.plan(_Scope(), transport.parse(_doc()), mode=mode)
        keep = [o for o in plan.orphans if o.entry_id == 9]
        assert keep and keep[0].action == "keep", mode


@pytest.mark.asyncio
async def test_merge_mode_leaves_a_dropped_seed_row_alone(rows):
    rows.append(_entry(id=9, seed_key="rule-two", title="A rule since deleted",
                       body="No longer in the file."))
    plan = await transport.plan(_Scope(), transport.parse(_doc()), mode="merge")
    dropped = [o for o in plan.orphans if o.entry_id == 9]
    assert dropped and dropped[0].action == "keep"


@pytest.mark.asyncio
async def test_sync_mode_retires_a_row_this_file_used_to_carry(rows):
    rows.append(_entry(id=9, seed_key="rule-two", title="A rule since deleted",
                       body="No longer in the file."))
    plan = await transport.plan(_Scope(), transport.parse(_doc()), mode="sync")
    dropped = [o for o in plan.orphans if o.entry_id == 9]
    assert dropped and dropped[0].action == "retire"


@pytest.mark.asyncio
async def test_sync_mode_does_not_touch_a_row_from_a_different_file(rows):
    rows.append(_entry(id=9, seed_key="rule-two", seed_source="seed:other/v1",
                       title="From another document", body="Not ours to retire."))
    plan = await transport.plan(_Scope(), transport.parse(_doc()), mode="sync")
    dropped = [o for o in plan.orphans if o.entry_id == 9]
    assert dropped and dropped[0].action == "keep"


# ── Export hygiene ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_carries_no_instance_local_fields(rows):
    """Observation ids, row ids, versions and timestamps are local to an
    instance. Importing `sources` would attach an entry to an unrelated
    observation, which is the provenance corruption _clean_sources prevents."""
    rows.append(_entry(id=5, version=4, source_count=9))
    doc = await transport.export(_Scope(), resolved=False)
    assert doc["format"] == transport.FORMAT
    assert doc["resolved"] is False
    banned = {"id", "version", "source_count", "sources", "created_by",
              "updated_by", "first_seen_at", "last_confirmed_at"}
    for row in doc["entries"]:
        assert not (set(row) & banned), set(row) & banned


@pytest.mark.asyncio
async def test_an_export_round_trips_through_parse(rows):
    rows.append(_entry())
    doc = await transport.export(_Scope())
    parsed = transport.parse(doc)
    assert len(parsed.entries) == 1
    assert parsed.entries[0].key == "rule-one"


# ── committed knowledge is not the curator's to rewrite ──────────────────


def test_a_seeded_entry_is_excluded_from_what_the_curator_may_write():
    """The reliability of a hand-authored seed rests on nothing rewriting it.

    Pinning would also protect it, but pinning silences `standing` — a
    contradiction against a pinned entry is recorded and then rendered
    invisible — so most seeded rows are deliberately left unpinned. This is the
    exclusion that makes that safe. The curator can still add alongside a
    seeded entry, and still contradict it, which is the honest disagreement.
    """
    import inspect

    from app.services.lore import curator as _curator

    src = inspect.getsource(_curator.curate)
    assert "not e.seed_source" in src, (
        "curate() must exclude seeded entries from known_entry_ids, or an "
        "unattended pass can revise or retire hand-authored knowledge"
    )


def test_seeded_and_inherited_are_both_excluded():
    """Two exclusions, two reasons: an inherited row belongs to the app owner,
    a seeded row was written by a person."""
    import inspect

    from app.services.lore import curator as _curator

    src = inspect.getsource(_curator.curate)
    assert "e.client_code == client_code" in src
    assert "not e.seed_source" in src
