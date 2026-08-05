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


async def test_unknown_ids_from_the_model_are_ignored_not_fatal():
    store = InMemoryStore()
    sid = store.create_session(purpose="p", created_by="s").shared_id
    script = {"working_memory": "wm",
              "merges": [{"source_ids": ["f-005a-01", "f-GHOST"], "text": "x", "type": "learning"}],
              "trivial_ids": ["f-ALSO-GHOST"], "conflicts": []}
    await Synthesizer(FakeProvider(scripts=[script])).merge(store, sid, _pair())
    merged = [f for f in store.all_findings(sid) if f.provenance == Provenance.SYNTHESIZED]
    assert len(merged) == 1 and merged[0].merged_from == ["f-005a-01"]
