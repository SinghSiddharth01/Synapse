"""The verdict write path, and the guard that keeps it the ONLY one.

Plan E Task E.1. Every verdict synthesis applies must go through an explicit
store method, because a store swap that returns COPIES (Task 6's Option A
projection does exactly that) silently discards every merge, tombstone, trivia
mark and conflict applied by mutating a returned reference -- while every
API-level test still passes.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timezone

import synapse_service
from synapse_contracts import (Attribution, Conflict, Finding, FindingStatus,
                               Provenance)
from synapse_service.store import InMemoryStore

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _finding(fid: str, text: str = "x") -> Finding:
    return Finding(id=fid, type="learning", text=text,
                   attributions=[Attribution(contributor="aditya",
                                             agent_session="as-1", agent="claude-code")],
                   ts=TS)


def _synth(fid: str, sources: list[str]) -> Finding:
    return Finding(id=fid, type="learning", text="merged",
                   attributions=[Attribution(contributor="aditya",
                                             agent_session="as-1", agent="claude-code")],
                   ts=TS, provenance=Provenance.SYNTHESIZED, merged_from=sources)


def _store() -> tuple[InMemoryStore, str]:
    store = InMemoryStore()
    return store, store.create_session(purpose="p", created_by="s").shared_id


def test_supersede_tombstones_every_live_source_and_lands_the_result():
    store, sid = _store()
    store.upsert(sid, [_finding("f-1"), _finding("f-2")])

    store.supersede(sid, ["f-1", "f-2"], _synth("syn-1", ["f-1", "f-2"]))

    assert store.get(sid, "f-1").merged_into == "syn-1"
    assert store.get(sid, "f-2").merged_into == "syn-1"
    assert store.get(sid, "syn-1") is not None
    assert [f.id for f in store.retrievable(sid)] == ["syn-1"]
    assert len(store.all_findings(sid)) == 3            # nothing deleted


def test_supersede_leaves_an_already_superseded_source_pointing_at_its_first_successor():
    """A merge is the only irreversible act in the system; re-superseding an
    already-merged source would silently rewrite lineage that a human may need
    to read back.

    Task 6 keeps this property through a DIFFERENT mechanism -- the registry
    pre-filters `live` before calling `SharedMemory.merge`, because the fold
    itself is last-merge-wins. That divergence is stated and pinned in Task 6
    Step 2; this test is the reason it had to be."""
    store, sid = _store()
    store.upsert(sid, [_finding("f-1")])
    store.supersede(sid, ["f-1"], _synth("syn-1", ["f-1"]))

    store.supersede(sid, ["f-1"], _synth("syn-2", ["f-1"]))

    assert store.get(sid, "f-1").merged_into == "syn-1"


def test_mark_trivial_skips_a_source_supersede_already_tombstoned():
    """The `finding.merged_into is None` guard synthesis.py:235 carries today,
    preserved at the seam."""
    store, sid = _store()
    store.upsert(sid, [_finding("f-1"), _finding("f-2")])
    store.supersede(sid, ["f-1"], _synth("syn-1", ["f-1"]))

    store.mark_trivial(sid, ["f-1", "f-2", "f-GHOST"])   # unknown id is ignored, not fatal

    assert store.get(sid, "f-1").status is FindingStatus.KEPT     # tombstoned, not trivia
    assert store.get(sid, "f-2").status is FindingStatus.TRIVIAL


def test_set_context_writes_only_what_it_is_given():
    store, sid = _store()
    store.set_context(sid, working_memory="the team is chasing a timing window")

    store.set_context(sid, conflicts=[Conflict(finding_a="f-1", finding_b="f-2",
                                               description="disagree")])

    ctx = store.get_context(sid)
    assert ctx.working_memory == "the team is chasing a timing window"   # untouched
    assert len(ctx.conflicts) == 1


# ── the guard that matters most ────────────────────────────────────────────
_VERDICT_FIELDS = {"merged_into", "status", "conflicts", "working_memory"}
_MUTATORS = {"append", "extend", "insert", "clear", "pop", "remove", "sort", "reverse"}
_ALLOWED = {"store.py"}          # the ONE module allowed to write a verdict field
_PKG = pathlib.Path(synapse_service.__file__).parent


def _violations(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and target.attr in _VERDICT_FIELDS:
                found.append(f"{path.name}:{node.lineno} assigns .{target.attr}")
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATORS
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr in _VERDICT_FIELDS):
            found.append(f"{path.name}:{node.lineno} mutates "
                         f".{node.func.value.attr}.{node.func.attr}()")
    return found


def test_no_verdict_field_is_written_outside_the_store():
    """Written as a SOURCE-READING test on purpose: the failure mode is a line
    somebody adds back later, and by then every behavioural test still passes
    (it passes on a mutable store, and fails silently on a projecting one).

    If this goes red, the fix is to route the write through
    store.supersede / store.mark_trivial / store.set_context -- never to add
    the file to _ALLOWED."""
    offenders: list[str] = []
    for path in sorted(_PKG.glob("*.py")):
        if path.name in _ALLOWED:
            continue
        offenders += _violations(path)

    assert offenders == [], (
        "verdict fields must be written only through the storage seam "
        f"(store.supersede / store.mark_trivial / store.set_context); found: {offenders}")
