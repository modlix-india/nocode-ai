"""The committed lore seed files must stay valid as the taxonomy moves.

These files are hand-authored knowledge about real apps, and they are the
reason the seeds live in this repository rather than beside the documentation
they were written from: a change to `ENTRY_KINDS`, `SUBJECT_TYPES` or
`normalise_subject` would otherwise silently invalidate them with nothing to
catch it.

The subject check is the one that earns its keep. `normalise_subject` degrades
an unrecognised subject to "app" without complaining, so a typo in a subject
does not fail — it quietly files the entry where `lore_about` and the per-turn
push will never look for it. `transport.parse` refuses that, and this asserts
it stays refused.

No database, no LLM, consistent with the rest of the lore suite.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from app.services.lore import transport
from app.services.lore.models import (
    ENTRY_KINDS,
    MAX_TITLE,
    SUBJECT_TYPES,
    normalise_subject,
)

SEEDS_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "lore" / "seeds"
SEED_FILES = sorted(SEEDS_DIR.glob("*.yaml"))

# The apps deliberately seeded by hand. cxapp is absent on purpose: its only
# documentation is a v2 specification for an app code that does not exist, and
# seeding a plan as fact into a live app is worse than leaving it empty.
EXPECTED_APPS = {"appbuilder", "leadzump", "marketingai", "sitezump"}


def test_the_expected_apps_are_all_seeded():
    assert {p.stem for p in SEED_FILES} == EXPECTED_APPS


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_parses(path: Path):
    doc = transport.parse(path.read_text(encoding="utf-8"))
    assert doc.app_code == path.stem
    assert doc.client_code
    assert doc.entries
    # A resolved document has its inheritance chain flattened; importing one
    # turns every inherited row into an owned copy. A seed must never be one.
    assert doc.resolved is False


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_source_is_stamped_and_versioned(path: Path):
    doc = transport.parse(path.read_text(encoding="utf-8"))
    assert doc.source.startswith("seed:"), (
        "source is what a sync-mode import uses to tell a seeded row from one a "
        "person wrote; it must be recognisable"
    )
    assert doc.app_code in doc.source


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_keys_are_unique_and_stable(path: Path):
    doc = transport.parse(path.read_text(encoding="utf-8"))
    keys = [e.key for e in doc.entries]
    assert len(keys) == len(set(keys))
    # Every entry carries an EXPLICIT key. A derived key changes when the title
    # is reworded, which would orphan the row and import a duplicate beside it.
    raw = path.read_text(encoding="utf-8")
    for e in doc.entries:
        assert f"key: {e.key}" in raw, f"{e.key} looks derived, not declared"


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_subjects_are_real_types(path: Path):
    doc = transport.parse(path.read_text(encoding="utf-8"))
    for e in doc.entries:
        assert normalise_subject(e.subject) == e.subject
        if ":" in e.subject:
            assert e.subject.split(":", 1)[0] in SUBJECT_TYPES


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_kinds_and_shapes(path: Path):
    doc = transport.parse(path.read_text(encoding="utf-8"))
    for e in doc.entries:
        assert e.kind in ENTRY_KINDS
        assert 4 <= len(e.title) <= MAX_TITLE
        assert not e.title.endswith("."), f"{e.key}: a title is a label, not a sentence"
        assert len(e.body) >= 40, f"{e.key}: body is too thin to be worth an entry"
        assert 0 <= e.confidence <= 100


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_has_a_purpose_and_some_rules(path: Path):
    """Every app must say what it is for and what must hold.

    These two kinds are the ones the agent's briefing renders first and under
    their own budget, so an app seeded without them wastes the mechanism.
    """
    doc = transport.parse(path.read_text(encoding="utf-8"))
    kinds = Counter(e.kind for e in doc.entries)
    assert kinds["purpose"] >= 1
    assert kinds["constraint"] >= 2


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_pinning_is_sparing(path: Path):
    """Pinning is for claims an edit cannot falsify, and it has a real cost.

    A pinned entry never reports `standing`, so a contradiction against it is
    recorded and then rendered invisible, and `gaps` cannot surface it either.
    Pinning everything also makes the `pinned` sort key stop discriminating.
    """
    doc = transport.parse(path.read_text(encoding="utf-8"))
    pinned = [e for e in doc.entries if e.pinned]
    assert pinned, "at least the app's purpose should be pinned"
    assert len(pinned) <= max(4, len(doc.entries) // 3), (
        f"{len(pinned)} of {len(doc.entries)} pinned — too many to be meaningful"
    )


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_records_no_owner_claims(path: Path):
    """`owner` carries a 180-day half-life, so an invented one decays into a puzzle.

    Nothing in the source documentation supports an owner claim, so none should
    be here. If a real one is ever added, delete this test rather than weaken it.
    """
    doc = transport.parse(path.read_text(encoding="utf-8"))
    assert not [e for e in doc.entries if e.kind == "owner"]


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_holds_no_definition_restatement(path: Path):
    """Ids and component counts belong to the definitions, not to lore.

    They are already recorded, they go stale on the next edit, and they crowd
    out the claims that cannot be derived. This catches the most common way a
    seed goes wrong.
    """
    import re

    doc = transport.parse(path.read_text(encoding="utf-8"))
    for e in doc.entries:
        text = f"{e.title}\n{e.body}"
        assert not re.search(r"\b[0-9a-f]{24}\b", text), f"{e.key}: looks like a Mongo id"
        assert not re.search(
            r"\b(page|component|root ?component|event)[ _]?id\b[:=\s]*[A-Za-z0-9]{8,}",
            text, re.I,
        ), f"{e.key}: names a definition id"


@pytest.mark.parametrize("path", SEED_FILES, ids=lambda p: p.stem)
def test_seed_leaks_no_secret_values(path: Path):
    """A seed is read by every agent working on the app. It is not a vault.

    One entry deliberately records that a hardcoded secret EXISTS and where,
    which is the durable fact someone needs. The value must not be here.
    """
    import re

    raw = path.read_text(encoding="utf-8")
    for pattern in (
        r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*[\"']?[A-Za-z0-9@!$%^&*_\-]{6,}",
        r"\beyJ[A-Za-z0-9_\-]{10,}",   # a JWT
    ):
        hits = [
            m.group(0) for m in re.finditer(pattern, raw)
            # `primaryVerifyToken: "token@123"` style prose is what we are
            # guarding against; a bare mention of the word token is fine.
            if "token@" in m.group(0) or "eyJ" in m.group(0)
        ]
        assert not hits, f"{path.name}: possible secret value {hits}"
