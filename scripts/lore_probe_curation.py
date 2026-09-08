#!/usr/bin/env python3
"""Diagnose why a lore curation pass produces no entries. Writes NOTHING.

Runs the real curation pass end to end — the same pending observations, the
same inheritance chain, the same system prompt, the same provider and tier —
but stops short of every write. No run row is opened, no observation is marked
curated, no entry is created.

It exists because a curation run currently records `added=0, error=NULL`
whether the model returned an empty list, returned prose we could not parse, or
returned operations that were all rejected. Those are three different bugs with
three different fixes and the stored row cannot tell them apart.

Usage:
    ./venv/bin/python scripts/lore_probe_curation.py --client FIN --app benchsbx
    ./venv/bin/python scripts/lore_probe_curation.py --client FIN --app benchsbx --only 277,285

Read the outcome as:
    raw empty or non-JSON        -> the provider path is broken
    parses to {"operations": []} -> the input is starved of durable knowledge
    ops present, all rejected    -> the validation gate, and it names which one
    rendered < pending           -> the render budget is dropping observations
                                    that are nonetheless marked curated
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app.config import settings  # noqa: E402
from app.db.connection import init_db_pool, close_db_pool  # noqa: E402
from app.services.llm_provider import get_llm_provider  # noqa: E402
from app.services.lore import curator, store  # noqa: E402

RULE = "=" * 78


class _NullStore:
    """Stands in for `store` so apply_operations decides without writing.

    Mirrors tests/test_lore.py::_FakeStore. Every method records intent and
    returns what the real store would, so the operation-by-operation verdict is
    the same one the live pass would have reached.
    """

    def __init__(self, pinned_ids=()):
        self.added, self.confirmed, self.revised = [], [], []
        self.status_changes, self.links = [], []
        self._next_id = 900000
        self.pinned_ids = set(pinned_ids)

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

    async def set_entry_status(self, entry_id, status, *, superseded_by=None,
                               updated_by=0, force=False):
        if entry_id in self.pinned_ids and status in ("retired", "superseded") and not force:
            return False
        self.status_changes.append((entry_id, status))
        return True

    async def add_link(self, a, b, rel):
        self.links.append((a, b, rel))


async def probe(client_code: str, app_code: str, batch_size: int, only: set[int] | None) -> int:
    print(f"\n{RULE}\nlore curation probe — {client_code}/{app_code}  (no writes)\n{RULE}")

    pending = await store.pending_observations(client_code, app_code, limit=batch_size)
    if only:
        pending = [o for o in pending if o.id in only]
    if not pending:
        print("\nNo pending observations for this app. Nothing to diagnose.")
        print("(A burned observation has CURATED_AT set and will not appear here.)")
        return 1

    by_kind: dict[str, int] = {}
    for o in pending:
        by_kind[o.kind] = by_kind.get(o.kind, 0) + 1
    print(f"\nPending considered : {len(pending)}  {by_kind}")
    print(f"Total body chars   : {sum(len(o.body or '') for o in pending):,}")

    # ── the render budget ────────────────────────────────────────────────
    rendered_text, got = curator._render_observations(pending)
    print(f"\n--- render budget (24,000 chars) ---")
    print(f"Rendered to the model : {len(got)} of {len(pending)} observations")
    if len(got) < len(pending):
        dropped = [o.id for o in pending if o.id not in set(got)]
        print(f"DROPPED but still marked curated by the live pass: {len(dropped)} -> {dropped}")
    else:
        print("Whole batch fitted.")

    chain = await curator._read_chain(client_code, app_code)
    existing = await store.list_entries(chain, app_code, status="active",
                                        limit=curator.CONTEXT_ENTRIES)
    print(f"\nInheritance chain  : {chain}")
    print(f"Existing entries   : {len(existing)}")
    writable = {e.id for e in existing if e.client_code == client_code}
    print(f"Writable entry ids : {len(writable)}"
          + ("   <-- EMPTY: only 'add' ops can ever succeed" if not writable else ""))

    # ── the one LLM call ─────────────────────────────────────────────────
    prompt, _ = curator.build_user_prompt(app_code, pending, existing)
    provider_name = settings.APPBUILDER_PROVIDER
    provider = get_llm_provider(provider_name)
    tier = settings.LORE_CURATOR_TIER
    max_tokens = settings.LORE_CURATOR_MAX_TOKENS
    print(f"\n--- model call ---\nProvider/tier      : {provider_name} / {tier}")
    print(f"max_tokens         : {max_tokens:,}")
    print(f"Prompt chars       : system {len(curator.SYSTEM_PROMPT):,} + user {len(prompt):,}")

    response = await provider.create_completion(
        system_prompt=curator.SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        model_tier=tier,
        max_tokens=max_tokens,
    )
    raw = response.get("content") or ""
    reasoning = response.get("reasoning_content") or ""
    print(f"Model             : {response.get('model')}")
    print(f"stop_reason       : {response.get('stop_reason')}")
    print(f"usage             : {response.get('usage')}")
    print(f"Reasoning chars   : {len(reasoning):,}")
    print(f"Response chars    : {len(raw):,}"
          + ("   <-- EMPTY RESPONSE: the budget went on reasoning"
             if not raw.strip() else ""))
    print(f"\n--- raw response, verbatim ---\n{raw if raw else '(nothing)'}\n--- end ---")

    # ── parse ────────────────────────────────────────────────────────────
    ops, reason = curator.parse_response(raw)
    print(f"\n--- parse ---\nOperations parsed  : {len(ops)}   reason={reason or '(ok)'}")
    for op in ops:
        print(f"  op={op.get('op')!r:12} kind={op.get('kind')!r:14} "
              f"id={op.get('id')!r:6} title={str(op.get('title'))[:52]!r}")

    # ── apply, against a store that cannot write ─────────────────────────
    real_store = curator.store
    null_store = _NullStore(pinned_ids={e.id for e in existing if e.pinned})
    curator.store = null_store
    try:
        counters = await curator.apply_operations(
            client_code, app_code, ops,
            batch_ids={o.id for o in pending},
            known_entry_ids=writable,
        )
    finally:
        curator.store = real_store

    print(f"\n--- apply (dry) ---\n{counters}")
    for kw in null_store.added:
        print(f"  WOULD ADD  [{kw.get('kind')}] {kw.get('subject')} :: {kw.get('title')}")
    for eid in null_store.revised:
        print(f"  WOULD REVISE #{eid}")
    for eid in null_store.confirmed:
        print(f"  WOULD CONFIRM #{eid}")
    for eid, st in null_store.status_changes:
        print(f"  WOULD SET #{eid} -> {st}")

    # ── verdict ──────────────────────────────────────────────────────────
    print(f"\n{RULE}\nVERDICT\n{RULE}")
    produced = counters.get("added", 0) + counters.get("revised", 0) + counters.get("confirmed", 0)
    if not raw.strip():
        print("The provider returned nothing. The model call is the bug.")
    elif not ops:
        print("The model returned no usable operations.")
        print("If the JSON was valid and empty, the input carries no durable knowledge")
        print("(which the system prompt explicitly invites) and the fix is upstream at ingest.")
        print("If it was unparseable, the fix is a JSON-mode / repair retry in the curator.")
    elif produced == 0:
        print(f"The model proposed {len(ops)} operation(s) and ALL were rejected.")
        print(f"rejected={counters.get('rejected')}. The validation gate is the bug.")
        if not writable:
            print("Note: no writable entries exist, so every confirm/revise/retire/supersede")
            print("op is auto-rejected. Only 'add' can succeed on a cold app.")
    else:
        print(f"The pass WOULD have produced {produced} entry change(s).")
        print("The curator works on this input; a live pass here should not be empty.")
    if len(got) < len(pending):
        print(f"\nSeparately CONFIRMED: the render budget dropped {len(pending) - len(got)} "
              f"observation(s)\nthat a live pass would still have marked curated.")
    print()
    return 0


async def main() -> int:
    ap = argparse.ArgumentParser(description="Diagnose a lore curation pass. Writes nothing.")
    ap.add_argument("--client", required=True, help="Client code, e.g. FIN")
    ap.add_argument("--app", required=True, help="App code, e.g. benchsbx")
    ap.add_argument("--batch-size", type=int, default=curator.BATCH_SIZE)
    ap.add_argument("--only", default="", help="Comma-separated observation ids to probe alone")
    args = ap.parse_args()

    only = {int(x) for x in args.only.split(",") if x.strip()} or None
    await init_db_pool()
    try:
        return await probe(args.client, args.app, args.batch_size, only)
    finally:
        await close_db_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
