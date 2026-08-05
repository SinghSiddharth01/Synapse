import re
from datetime import datetime, timezone

from synapse_contracts import Attribution, Finding, FindingStatus, Provenance
from synapse_providers import FakeProvider

from synapse_service.store import InMemoryStore
from synapse_service.synthesis import CANDIDATE_WINDOW, Synthesizer

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


class _RecordingProvider(FakeProvider):
    """A FakeProvider that also records the finding ids listed in each
    merge prompt, so a test can assert on WHICH candidates synthesis was
    offered rather than only on the verdict it returned."""

    def __init__(self, scripts):
        super().__init__(scripts=scripts)
        self.seen: list[list[str]] = []
        self.prompts: list[str] = []          # the RAW prompt body

    async def complete(self, messages, response_schema=None):
        listing = messages[-1]["content"]
        self.seen.append(re.findall(r"\[([^\]]+)\]", listing))
        self.prompts.append(listing)
        return await super().complete(messages, response_schema)


def _pair() -> list[Finding]:
    """The seg-005 shape. If E1 has landed, prefer:
        from synapse_distiller.fixtures import load_goldens
        return load_goldens("seg-005a") + load_goldens("seg-005b")
    The inline version below is byte-equivalent in every field a test asserts on.
    """
    a = Finding(id="f-005a-01", type="learning",
                text="The decode failure is a timing window: it occurs only when the gap "
                     "between the two DMA writes exceeds roughly 40 ms.",
                attributions=[Attribution(contributor="aditya",
                                          agent_session="as-fixture-005a", agent="claude-code")],
                ts=TS)
    b = Finding(id="f-005b-01", type="learning",
                text="The decode failure reproduces only under load, when background "
                     "traffic pushes the delay past about 40 ms.",
                attributions=[Attribution(contributor="akhil",
                                          agent_session="as-fixture-005b", agent="codex")],
                ts=TS)
    return [a, b]


MERGE_SCRIPT = {
    "working_memory": "Team is chasing a decode failure: a ~40 ms timing window, load-dependent.",
    "merges": [{
        "source_ids": ["f-005a-01", "f-005b-01"],
        "text": "The decode failure is a ~40 ms timing window between the two DMA "
                "writes, and it only manifests under load.",
        "type": "learning",
    }],
    "trivial_ids": [],
    "conflicts": [],
}


async def test_semantic_merge_produces_synthesized_finding_and_tombstones():
    store = InMemoryStore()
    sid = store.create_session(purpose="fec decode failure", created_by="siddsing").shared_id
    synth = Synthesizer(FakeProvider(scripts=[MERGE_SCRIPT]))

    ctx = await synth.merge(store, sid, _pair())

    [merged] = store.retrievable(sid)                       # only the synthesized one
    assert merged.provenance == Provenance.SYNTHESIZED
    assert sorted(merged.merged_from) == ["f-005a-01", "f-005b-01"]
    assert {a.contributor for a in merged.attributions} == {"aditya", "akhil"}
    assert "under load" in merged.text                       # the pooled half survives

    for fid in ("f-005a-01", "f-005b-01"):                   # tombstones, not deletions
        tomb = store.get(sid, fid)
        assert tomb.merged_into == merged.id
        assert tomb.text                                     # text retained (ADR 0002)

    assert ctx.memory_version == 1
    assert ctx.working_memory.startswith("Team is chasing")


async def test_trivia_verdict_and_conflict():
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    trivial = Finding(id="f-triv", type="learning", text="Ran the linter.",
                      attributions=a.attributions, ts=TS)
    script = {"working_memory": "wm", "merges": [],
              "trivial_ids": ["f-triv"],
              "conflicts": [{"a": "f-005a-01", "b": "f-005b-01",
                             "description": "disagree on the exact threshold"}]}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, [a, b, trivial])

    assert store.get(sid, "f-triv").status == FindingStatus.TRIVIAL
    assert "f-triv" not in [f.id for f in store.retrievable(sid)]
    [conflict] = ctx.conflicts
    assert (conflict.finding_a, conflict.finding_b) == ("f-005a-01", "f-005b-01")


async def test_findings_survive_a_synthesis_failure():
    """Upsert-before-model: a model blowup degrades quality, never durability."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    exploding = FakeProvider(scripts=[])                     # any call raises
    ctx = await Synthesizer(exploding).merge(store, sid, _pair())
    assert len(store.all_findings(sid)) == 2                 # landed anyway
    assert ctx.memory_version == 0                           # honestly un-merged


async def test_non_dict_verdicts_do_not_crash_merge():
    """Both real providers can hand back non-dict data on their documented
    failure path: AIC100Provider returns data=None with schema_valid=False
    after its retry is exhausted, and OpenAICompatibleProvider/NPUProvider
    return the raw text string (schema_valid=False) when tolerant parsing
    fails. FakeProvider can't produce schema_valid=False (it always reports
    True), so this pins the isinstance(data, dict) half of the guard --
    merge() must survive whatever shape complete() hands back, not just
    what FakeProvider happens to script."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    ctx = await Synthesizer(FakeProvider(scripts=[None])).merge(store, sid, _pair())
    assert len(store.all_findings(sid)) == 2                 # landed anyway
    assert ctx.memory_version == 0                            # honestly un-merged


async def test_text_verdicts_do_not_crash_merge():
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    ctx = await Synthesizer(FakeProvider(scripts=["not json, just prose"])).merge(
        store, sid, _pair())
    assert len(store.all_findings(sid)) == 2
    assert ctx.memory_version == 0


async def test_conflicts_are_deduplicated_across_merge_rounds():
    """The bounded candidate window re-shows still-retrievable findings on
    every push, so the model can (and does) report the same conflict again
    on a later round. ADR 0002 names dedup as a correctness property, not an
    audit nicety."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    conflict_script = {"working_memory": "wm", "merges": [], "trivial_ids": [],
                       "conflicts": [{"a": "f-005a-01", "b": "f-005b-01",
                                      "description": "disagree on the exact threshold"}]}
    synth = Synthesizer(FakeProvider(scripts=[conflict_script, conflict_script]))
    await synth.merge(store, sid, [a, b])
    ctx = await synth.merge(store, sid, [])          # second round, same verdict again
    assert len(ctx.conflicts) == 1


async def test_conflict_on_a_finding_merged_in_the_same_round_resolves_forward():
    """A verdict can both merge a finding away AND report a conflict on it in
    the same round. The stored Conflict must follow merged_into forward to
    the synthesized finding rather than dangle at the tombstone -- ADR 0002
    and the Finding contract docstring both require this."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    third = Finding(id="f-c", type="learning", text="an unrelated third finding",
                    attributions=a.attributions, ts=TS)
    script = {
        "working_memory": "wm",
        "merges": [{"source_ids": ["f-005a-01", "f-005b-01"], "text": "merged text",
                   "type": "learning"}],
        "trivial_ids": [],
        "conflicts": [{"a": "f-005a-01", "b": "f-c", "description": "disagree with the third"}],
    }
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, [a, b, third])

    [synthesized] = [f for f in store.all_findings(sid) if f.provenance == Provenance.SYNTHESIZED]
    assert store.get(sid, "f-005a-01").merged_into == synthesized.id   # tombstoned this round

    [conflict] = ctx.conflicts
    assert conflict.finding_a == synthesized.id        # resolved forward, not dangling
    assert conflict.finding_b == "f-c"


async def test_malformed_merge_entry_invalidates_the_whole_verdict_not_just_itself():
    """A schema_valid verdict can still carry malformed nested entries -- the
    real AIC100Provider's schema gate only inspects TOP-level required keys,
    never array items (packages/providers/src/synapse_providers/aic100.py's
    _satisfies_schema). Round-2 adjudication: merge() validates the WHOLE
    verdict as one pydantic model before mutating anything. A malformed
    entry anywhere means NONE of the verdict applies -- not a per-entry
    skip -- so findings stay exactly as durably landed by step 1 and
    memory_version stays exactly as it was. Skipping only the bad entry
    (the previous approach) is partial application: which entries land
    becomes an accident of list order rather than a decision, and that
    contradicts this module's own "zero loss" docstring and invariant 5
    (merge is the only irreversible action)."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    script = {"working_memory": "wm",
              "merges": [{"source_ids": ["f-005a-01", "f-005b-01"]}],  # missing text/type
              "trivial_ids": [], "conflicts": []}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, [a, b])

    assert ctx.memory_version == 0                      # whole verdict rejected, not applied
    assert store.get(sid, "f-005a-01").merged_into is None   # never tombstoned
    assert store.get(sid, "f-005b-01").merged_into is None


async def test_merges_of_the_wrong_top_level_type_does_not_crash():
    """AIC100Provider's schema gate only checks that the OBJECT has the
    right top-level keys, not that each key holds the right TYPE -- a
    string where 'merges' should be a list is schema_valid=True per the
    provider's own gate. The whole verdict fails structural validation and
    is rejected wholesale rather than crashing merge()."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    script = {"working_memory": "wm", "merges": "not-a-list",
              "trivial_ids": [], "conflicts": []}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, [a, b])
    assert ctx.memory_version == 0


async def test_conflict_entry_missing_a_key_invalidates_the_whole_verdict():
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    script = {"working_memory": "wm", "merges": [], "trivial_ids": [],
              "conflicts": [{"a": "f-005a-01", "description": "missing b"}]}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, [a, b])
    assert ctx.memory_version == 0
    assert ctx.conflicts == []


async def test_a_malformed_entry_invalidates_a_second_otherwise_valid_merge_too():
    """The all-or-nothing failure mode, the other direction: a malformed
    entry must veto a LATER, well-formed entry in the same verdict too --
    not just the entries before it. Applying the well-formed one while
    dropping the malformed one would be partial application by another
    name (a "does this entry look fine on its own" pass), which is exactly
    what the round-2 adjudication rules out."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    third = Finding(id="f-c", type="learning", text="an unrelated third finding",
                    attributions=a.attributions, ts=TS)
    fourth = Finding(id="f-d", type="learning", text="a fourth finding, same idea as f-c",
                     attributions=a.attributions, ts=TS)
    script = {
        "working_memory": "wm",
        "merges": [
            {"source_ids": ["f-005a-01", "f-005b-01"]},          # malformed: no text/type
            {"source_ids": ["f-c", "f-d"], "text": "merged", "type": "learning"},
        ],
        "trivial_ids": [], "conflicts": [],
    }
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(
        store, sid, [a, b, third, fourth])

    assert store.get(sid, "f-005a-01").merged_into is None     # neither entry applied
    assert store.get(sid, "f-c").merged_into is None
    assert ctx.memory_version == 0                              # whole verdict rejected


async def test_conflict_recorded_in_an_earlier_round_resolves_forward_when_merged_later():
    """A Conflict recorded in round 1 must not dangle at a tombstone once a
    LATER round merges that finding away. ADR 0002 names 'Conflicts must
    follow it forward' as a reason tombstones exist at all -- that isn't
    optional just because the merge happened in a different round than the
    conflict was recorded in. _resolve_forward is only useful if it's ever
    applied to conflicts already sitting in ctx.conflicts, not just to a
    verdict's own newly-reported pairs."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    c = Finding(id="f-c", type="learning", text="a third finding",
               attributions=a.attributions, ts=TS)

    round1 = {"working_memory": "wm", "merges": [], "trivial_ids": [],
             "conflicts": [{"a": "f-005a-01", "b": "f-c", "description": "disagree"}]}
    round2 = {"working_memory": "wm",
             "merges": [{"source_ids": ["f-005a-01", "f-005b-01"], "text": "merged",
                        "type": "learning"}],
             "trivial_ids": [], "conflicts": []}

    synth = Synthesizer(FakeProvider(scripts=[round1, round2]))
    await synth.merge(store, sid, [a, b, c])            # round 1: records the conflict
    ctx = await synth.merge(store, sid, [])              # round 2: merges f-005a-01 away

    [synthesized] = [f for f in store.all_findings(sid) if f.provenance == Provenance.SYNTHESIZED]
    [conflict] = ctx.conflicts
    assert conflict.finding_a == synthesized.id     # resolved forward, not left at the tombstone
    assert conflict.finding_b == "f-c"


async def test_conflicts_dedup_after_resolving_forward_not_before():
    """The bounded candidate window makes re-reporting the same disagreement
    the expected case (per test_conflicts_are_deduplicated_across_merge_rounds's
    own docstring). If the model re-reports a conflict using the ORIGINAL id
    after it has since been merged away, dedup keyed on the RAW (unresolved)
    ids misses the collision with the already-resolved entry and the same
    disagreement is recorded twice."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    c = Finding(id="f-c", type="learning", text="a third finding",
               attributions=a.attributions, ts=TS)

    round1 = {"working_memory": "wm", "merges": [], "trivial_ids": [],
             "conflicts": [{"a": "f-005a-01", "b": "f-c", "description": "disagree"}]}
    round2 = {"working_memory": "wm",
             "merges": [{"source_ids": ["f-005a-01", "f-005b-01"], "text": "merged",
                        "type": "learning"}],
             "trivial_ids": [], "conflicts": []}
    round3 = {"working_memory": "wm", "merges": [], "trivial_ids": [],
             # the model re-reports the SAME disagreement, still naming the
             # now-tombstoned original id -- the bounded window re-shows it
             "conflicts": [{"a": "f-005a-01", "b": "f-c", "description": "disagree"}]}

    synth = Synthesizer(FakeProvider(scripts=[round1, round2, round3]))
    await synth.merge(store, sid, [a, b, c])
    await synth.merge(store, sid, [])
    ctx = await synth.merge(store, sid, [])

    assert len(ctx.conflicts) == 1                        # not doubled


async def test_a_push_larger_than_the_candidate_window_is_not_starved():
    """CANDIDATE_WINDOW bounds the merge prompt against LOG GROWTH (Plan
    C.4's fixed-cost property) -- it must never bound it against the
    CURRENT push. A worker flushing a backlog after being offline is the
    normal case for the write-ahead log this system is built around, and
    can trivially exceed the window in a single push. Before the fix,
    candidates were a pure recency slice of the whole Log
    (store.retrievable(...)[-CANDIDATE_WINDOW:]) that never consulted
    new_findings at all, so the oldest findings in an over-window push
    were never offered to synthesis in that round OR any later one (they
    age out of the window as soon as anything newer arrives)."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    batch_size = CANDIDATE_WINDOW + 5
    findings = [Finding(id=f"f-{i:02d}", type="learning", text=f"finding {i}",
                        attributions=[Attribution(contributor="a", agent_session="as-1",
                                                  agent="claude-code")], ts=TS)
               for i in range(batch_size)]
    noop = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}
    provider = _RecordingProvider(scripts=[noop])

    await Synthesizer(provider).merge(store, sid, findings)

    assert set(provider.seen[0]) == {f.id for f in findings}   # every one offered, none starved


async def test_the_window_still_bounds_established_candidates_not_in_this_push():
    """The other half of the same property: CANDIDATE_WINDOW must still
    cap the OTHER (already-established) candidates, or the fixed-cost
    guarantee the window exists for is gone."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    old = [Finding(id=f"old-{i:02d}", type="learning", text=f"old {i}",
                   attributions=[Attribution(contributor="a", agent_session="as-1",
                                             agent="claude-code")], ts=TS)
          for i in range(CANDIDATE_WINDOW + 10)]
    noop = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}
    provider = _RecordingProvider(scripts=[noop, noop])
    synth = Synthesizer(provider)
    await synth.merge(store, sid, old)                  # round 1: lands them all as "new"

    new_one = Finding(id="new-1", type="learning", text="the newly pushed one",
                      attributions=old[0].attributions, ts=TS)
    await synth.merge(store, sid, [new_one])             # round 2: nothing special about `old` now

    seen = provider.seen[1]
    assert "new-1" in seen
    # Still BOUNDED: the fixed-cost property this window exists for. The exact
    # count is now min(CANDIDATE_WINDOW, DEFAULT_TOP_K) + 1 rather than
    # CANDIDATE_WINDOW + 1, because candidate selection is a lane lookup rather
    # than a recency slice -- so the property is asserted, not the arithmetic.
    assert len(seen) <= CANDIDATE_WINDOW + 1
    assert len(seen) < len(old) + 1        # genuinely capped, not "it all fit"
    # ...and not vacuously capped by returning nothing: the RECENT lane has a
    # reserved floor of max(1, top_k // RESERVE_DIVISOR) == 2, so an unrelated
    # query still brings established candidates back. `assert len(seen) <= N`
    # alone would pass on an empty `others`, which is the failure this whole
    # task is about, inverted.
    assert len(seen) >= 2


async def test_unknown_ids_from_the_model_are_ignored_not_fatal():
    """Plan semantics (docs/plans/exec/2026-08-04-e3-service.md L379-387,
    L495-499), restored per the round-2 adjudication: an unknown id in a
    verdict must not crash the merge, AND must not veto the merge of the
    ids that DID resolve. The plan's implementation logs a warning and
    "appl[ies] to the N valid source(s)"; it does not require >= 2 live
    sources. A prior round inverted this test (asserting merged == []) and
    changed synthesis.py to match, undisclosed -- that inversion is not an
    adaptation, the plan is the spec."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    script = {"working_memory": "wm",
              "merges": [{"source_ids": ["f-005a-01", "f-GHOST"], "text": "x", "type": "learning"}],
              "trivial_ids": ["f-ALSO-GHOST"], "conflicts": []}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, _pair())

    merged = [f for f in store.all_findings(sid) if f.provenance == Provenance.SYNTHESIZED]
    assert len(merged) == 1 and merged[0].merged_from == ["f-005a-01"]
    assert ctx.memory_version == 1                                # merge() still completed


async def test_a_merge_verdict_with_ALL_ghost_source_ids_lands_nothing():
    """The `if not sources: continue` guard's own regression pin -- the
    sibling above (`..._are_ignored_not_fatal`) covers one-valid-plus-one-
    ghost, which still has a live source and so never exercises the empty
    branch. Delete the `continue` (fall through with `sources == []`) and
    this landed a phantom `Finding(attributions=[], merged_from=[])`:
    unattributed, untraceable to any Contributor, and -- because
    `retrieval.visible_to`'s suppression guard is `f.attributions and ...`
    -- un-suppressible for every asker forever. Exactly the adr/0003
    invented-finding failure mode, one `continue` away from Shared Memory."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    script = {"working_memory": "wm",
              "merges": [{"source_ids": ["f-GHOST-1", "f-GHOST-2"], "text": "invented",
                         "type": "learning"}],
              "trivial_ids": [], "conflicts": []}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, _pair())

    merged = [f for f in store.all_findings(sid) if f.provenance == Provenance.SYNTHESIZED]
    assert merged == []
    assert ctx.memory_version == 1                                # verdict round still applied


async def test_an_old_near_duplicate_outside_the_last_twenty_is_a_merge_candidate():
    """THE product claim. Impossible on main today: `others` was a pure
    recency slice, so in a 40-finding session findings 1-20 were permanently
    unmergeable against anything new and the ADR 0002 merge simply stopped
    happening as the session grew."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    noop = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}
    provider = _RecordingProvider(scripts=[noop, noop])
    synth = Synthesizer(provider)

    old = Finding(id="f-old", type="learning",
                  text="the decode failure is a timing window above 40 ms",
                  attributions=[Attribution(contributor="a", agent_session="as-1",
                                            agent="claude-code")], ts=TS)
    filler = [Finding(id=f"f-fill-{i:02d}", type="learning",
                      text=f"unrelated note about the build script {i}",
                      attributions=old.attributions, ts=TS)
              for i in range(30)]
    await synth.merge(store, sid, [old] + filler)          # round 1: everything lands

    twin = Finding(id="f-twin", type="learning",
                   text="the decode failure reproduces above 40 ms under load",
                   attributions=old.attributions, ts=TS)
    await synth.merge(store, sid, [twin])                   # round 2

    assert "f-old" in provider.seen[1], (
        "the near-duplicate that arrived 30 findings ago never reached the merge prompt")


async def test_the_last_finding_of_a_large_push_still_gets_its_own_partners():
    """The failure a dict-insertion-order cap reintroduces. Each candidates()
    call returns up to DEFAULT_TOP_K = 14, so under `list(gathered.values())
    [:CANDIDATE_WINDOW]` the FIRST TWO pushed findings fill all 20 slots and
    pushed findings 3..N contribute nothing -- and a WAL backlog flush after
    the worker was offline routinely pushes more than 20 at once.

    Here the old partner matches only the LAST pushed finding."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    noop = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}
    provider = _RecordingProvider(scripts=[noop, noop])
    synth = Synthesizer(provider)
    attrs = [Attribution(contributor="a", agent_session="as-1", agent="claude-code")]

    established = Finding(id="f-partner", type="learning",
                          text="qairt refuses to allocate the context binary above 2 GB",
                          attributions=attrs, ts=TS)
    noise = [Finding(id=f"f-noise-{i:02d}", type="learning",
                     text=f"the build script sets flag {i}", attributions=attrs, ts=TS)
             for i in range(25)]
    await synth.merge(store, sid, [established] + noise)

    pushed = [Finding(id=f"f-new-{i:02d}", type="learning",
                      text=f"the build script sets a different flag {i}",
                      attributions=attrs, ts=TS) for i in range(4)]
    pushed.append(Finding(id="f-new-LAST", type="learning",
                          text="qairt will not allocate a context binary that large",
                          attributions=attrs, ts=TS))
    await synth.merge(store, sid, pushed)

    assert "f-partner" in provider.seen[1], (
        "the last pushed finding's only merge partner never reached the prompt — "
        "the union was capped by insertion order, not by relevance")


async def test_a_multi_subsystem_backlog_flush_shares_the_window_by_rank_not_arrival():
    """The `others = [f for _, f in sorted(best.values(), key=...)][:CANDIDATE_WINDOW]`
    line's own comment: dict-insertion-order truncation "lets the first two
    pushed findings fill all 20 slots, so findings 3..N of a backlog flush
    contribute no partners at all." The sibling test above pins that a
    SINGLE late pushed finding still finds its ONE partner -- but a dict with
    fewer than CANDIDATE_WINDOW unique entries never actually gets truncated
    at all, insertion order or not, so it cannot tell "capped by rank" apart
    from "never capped." Reverting the sort to bare dict-insertion order
    (`[f for _, f in best.values()][:CANDIDATE_WINDOW]`) leaves that test, and
    the rest of the suite, green.

    This scenario forces real truncation: 5 subsystems x 12 established
    findings each (60, comfortably over CANDIDATE_WINDOW=20 on its own) and
    one backlog flush of 5 pushed findings, one per subsystem, each
    lexically anchored to only its own subsystem's vocabulary -- so each of
    the 5 candidate() calls returns mostly same-subsystem matches, and the
    union across all 5 easily exceeds 20 unique ids. Rank-sorted truncation
    splits the 20-slot window close to evenly across subsystems regardless
    of which pushed finding was processed first; insertion-order truncation
    lets whichever subsystems' searches ran FIRST in the `for finding in
    pushed:` loop fill the window before later ones get a look, starving
    them down to as little as their own pushed finding and nothing else."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    noop = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}
    provider = _RecordingProvider(scripts=[noop, noop])
    synth = Synthesizer(provider)
    attrs = [Attribution(contributor="a", agent_session="as-1", agent="claude-code")]
    subsystems = ["pool", "tokenizer", "gateway", "decoder", "scheduler"]

    established = [
        Finding(id=f"f-{sub}-est-{i:02d}", type="learning",
               text=f"the {sub} subsystem trips a fault when allocation attempt {i} "
                    "exceeds its ceiling",
               attributions=attrs, ts=TS)
        for sub in subsystems for i in range(12)
    ]
    await synth.merge(store, sid, established)

    pushed = [
        Finding(id=f"f-{sub}-new", type="learning",
               text=f"the {sub} subsystem trips a fault under sustained allocation pressure",
               attributions=attrs, ts=TS)
        for sub in subsystems
    ]
    await synth.merge(store, sid, pushed)

    ids = provider.seen[1]
    for sub in subsystems:
        partners = sum(1 for i in ids if i.startswith(f"f-{sub}-est-"))
        assert partners >= 3, (
            f"subsystem {sub!r} got only {partners} established partner(s) in the merge "
            f"prompt out of {len(ids)} total candidates — the 20-slot window was not "
            "shared by rank across subsystems (candidate ids seen: " + ", ".join(ids) + ")")


async def test_a_shared_symbol_is_marked_on_the_candidate_line_the_model_reads():
    """The flagship mechanism, put in front of the model rather than only in
    front of the ranker.

    `Candidate.shared_symbols` is the branch's own strongest idea -- its
    docstring calls a shared rare identifier "a strong, cheap hint that costs a
    few tokens per line" -- and in this integration it reached NO prompt,
    because synthesis rebuilds its own listing from `candidate.finding` and
    throws the provenance away. A review called that a discarded design with no
    decision recorded. This is the decision: the symbols ride along.

    The marker uses PARENTHESES, not brackets, on purpose. `_RecordingProvider.
    seen` is `re.findall(r"\\[([^\\]]+)\\]", listing)`, so a bracketed marker
    would show up as a phantom finding id in every existing `set(provider.seen
    [n]) == {ids}` assertion. Asserted against `prompts`, which is the raw text."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    noop = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}
    provider = _RecordingProvider(scripts=[noop, noop])
    synth = Synthesizer(provider)
    attrs = [Attribution(contributor="aditya", agent_session="as-1", agent="claude-code")]

    await synth.merge(store, sid, [Finding(
        id="f-aditya", type="learning",
        text="the decode drifts once the DMA writes are 40 ms apart",
        attributions=attrs, ts=TS)])

    await synth.merge(store, sid, [Finding(
        id="f-akhil", type="learning",
        text="anything past 40 ms and the pipeline stalls",
        attributions=attrs, ts=TS)])

    prompt = provider.prompts[1]
    assert "f-aditya" in provider.seen[1]              # it was selected at all
    assert "(shares:" in prompt, (
        "the shared symbol that surfaced this candidate never reached the model")
    # symbols.extract normalises "40 ms" -> "40ms" (symbols.py's own docstring:
    # "so that `40 ms`, `40ms` and `40 MS` are one symbol"), so the marker
    # carries the normalised form, not the surface text -- adapted from the
    # plan's literal "40 ms" against symbols.py's real normalisation.
    assert "40ms" in prompt.split("(shares:", 1)[1].split(")", 1)[0]
    # ...and the marker did not leak into the id parser.
    assert all("shares" not in token for token in provider.seen[1])


async def test_the_self_heal_path_still_gets_candidates_without_a_push():
    """POST /v1/sessions/{sid}/synthesize passes new_findings=[]. There is no
    text to search WITH, so the lanes have no query; the recency slice is the
    only rule available and must stay on that path."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    noop = {"working_memory": "wm", "merges": [], "trivial_ids": [], "conflicts": []}
    provider = _RecordingProvider(scripts=[noop, noop])
    synth = Synthesizer(provider)
    landed = [Finding(id=f"f-{i:02d}", type="learning", text=f"finding {i}",
                      attributions=[Attribution(contributor="a", agent_session="as-1",
                                                agent="claude-code")], ts=TS)
              for i in range(3)]
    await synth.merge(store, sid, landed)

    await synth.merge(store, sid, [])                       # the self-heal call

    assert set(provider.seen[1]) == {f.id for f in landed}
