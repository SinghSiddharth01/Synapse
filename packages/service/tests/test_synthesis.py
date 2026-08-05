from datetime import datetime, timezone

from synapse_contracts import Attribution, Finding, FindingStatus, Provenance
from synapse_providers import FakeProvider

from synapse_service.store import InMemoryStore
from synapse_service.synthesis import Synthesizer

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


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


async def test_malformed_merge_entry_is_skipped_not_fatal():
    """A schema_valid verdict can still carry malformed nested entries -- the
    real AIC100Provider's schema gate only inspects TOP-level required keys,
    never array items (packages/providers/src/synapse_providers/aic100.py's
    _satisfies_schema). merge() must skip the bad entry and still complete
    the round, rather than crash mid-application: leaving tombstones written
    but memory_version un-bumped would contradict this module's own
    docstring ("a model failure ... leaves findings landed and version
    unchanged") and invariant 5 (merge is the only irreversible action)."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    script = {"working_memory": "wm",
              "merges": [{"source_ids": ["f-005a-01", "f-005b-01"]}],  # missing text/type
              "trivial_ids": [], "conflicts": []}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, [a, b])

    assert ctx.memory_version == 1                     # round still completed
    assert store.get(sid, "f-005a-01").merged_into is None   # never tombstoned
    assert store.get(sid, "f-005b-01").merged_into is None


async def test_merges_of_the_wrong_top_level_type_does_not_crash():
    """AIC100Provider's schema gate only checks that the OBJECT has the
    right top-level keys, not that each key holds the right TYPE -- a
    string where 'merges' should be a list is schema_valid=True per the
    provider's own gate."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    script = {"working_memory": "wm", "merges": "not-a-list",
              "trivial_ids": [], "conflicts": []}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, [a, b])
    assert ctx.memory_version == 1


async def test_conflict_entry_missing_a_key_is_skipped_not_fatal():
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    a, b = _pair()
    script = {"working_memory": "wm", "merges": [], "trivial_ids": [],
              "conflicts": [{"a": "f-005a-01", "description": "missing b"}]}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, [a, b])
    assert ctx.memory_version == 1
    assert ctx.conflicts == []


async def test_a_second_valid_merge_still_applies_after_a_malformed_one_before_it():
    """The partial-application failure mode: a crash on entry N must not
    leave entries before N half-applied while entries after N never run."""
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

    assert store.get(sid, "f-005a-01").merged_into is None     # malformed entry: skipped
    assert store.get(sid, "f-c").merged_into is not None       # valid entry: still applied
    assert ctx.memory_version == 1


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


async def test_unknown_ids_from_the_model_are_ignored_not_fatal():
    """An unknown id in a verdict must not crash the merge -- but a verdict
    that resolves to a single live source must not mint a Synthesized
    Finding either. ADR 0002 defines a Synthesized Finding as capturing
    "two or more Findings it judged semantically the same"; a merge of one
    is applied from a verdict the code has just proven corrupt (it named a
    nonexistent id), so it must be skipped, not honored with one source."""
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    script = {"working_memory": "wm",
              "merges": [{"source_ids": ["f-005a-01", "f-GHOST"], "text": "x", "type": "learning"}],
              "trivial_ids": ["f-ALSO-GHOST"], "conflicts": []}
    ctx = await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, _pair())

    merged = [f for f in store.all_findings(sid) if f.provenance == Provenance.SYNTHESIZED]
    assert merged == []                                          # skipped, not merged-of-one
    original = store.get(sid, "f-005a-01")
    assert original.merged_into is None                          # author's finding survives
    assert original in store.retrievable(sid)
    assert ctx.memory_version == 1                                # merge() still completed
