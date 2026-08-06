"""The stand-in's honesty properties, pinned.

`scripts/local_model_server.py` stands where the NPU and the cloud stand, so
every one of its shortcuts is a chance to make the system look like it works
when it does not. Each property below was verified by hand when the file was
written and then had no test — which is exactly how the "best first" header
survived while findings came back in arrival order.

Loaded by path because `scripts/` is not a package.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = REPO / "fixtures"


def _load():
    spec = importlib.util.spec_from_file_location(
        "local_model_server", REPO / "scripts" / "local_model_server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def stub():
    return _load()


@pytest.fixture(scope="module")
def replay(stub):
    return stub.Replay(stub.Corpus())


def _prompt_for(stub, segment_id: str) -> str:
    """A fixture rendered the way the distiller renders it — only the event
    kinds `distil_kinds` actually sends."""
    segment = json.loads((FIXTURES / "segments" / f"{segment_id}.json").read_text())
    kinds = stub._distil_kinds()
    return "\n".join(e["content"] for e in segment["events"] if e["kind"] in kinds)


# --- the canary must be READ, never remembered ----------------------------

def test_canary_answer_is_extracted_from_the_prompt_not_memorised(replay):
    """A stub that recognised the question and replied with a memorised
    `api.internal` would pass `check_canary` while proving nothing — and the
    canary's entire job is to prove the prompt was read. Change the hostname
    and the answer must change with it."""
    kind, answer = replay.answer(
        "The deployment failed because the TLS certificate for the host "
        "totally-different.example expired. Which hostname's certificate "
        "expired? Answer with the hostname and nothing else.")
    assert kind == "canary"
    assert answer == "totally-different.example"


def test_tokens_report_zero_for_an_empty_prompt(stub):
    """`_tokens` used to floor at 2 — exactly enough to clear
    `assert_prompt_conditioned`'s `input_tokens > 1`, including for an EMPTY
    prompt, which made the one guard a stand-in can honestly exercise
    structurally unable to fire."""
    assert stub._tokens("") == 0
    assert stub._tokens("x" * 400) > 1


# --- distillation must never invent ---------------------------------------

def test_every_corpus_fixture_matches_what_the_distiller_would_send(stub, replay):
    """`distil_kinds` is ["text"], so tool_use/tool_result content never
    reaches a prompt. A matcher requiring every event's text declared healthy
    segments unmatched and answered empty — seg-003 carries 6 KB of
    tool_result alone."""
    for path in sorted((FIXTURES / "segments").glob("*.json")):
        segment_id = path.stem
        kind, _ = replay.answer(_prompt_for(stub, segment_id))
        assert kind == f"distil/{segment_id}", (
            f"{segment_id} did not match itself — the stand-in would answer "
            f"empty for a segment the distiller sends in full")


def test_a_truncated_segment_answers_empty_rather_than_guessing(stub, replay):
    """The masking path this file exists to close: returning a fixture's whole
    golden set for a prompt that carried only part of it would hide a
    compaction or segmenter regression behind plausible findings."""
    full = _prompt_for(stub, "seg-003")
    kind, body = replay.answer(full.splitlines()[0])
    assert kind == "distil/unmatched"
    assert json.loads(body)["findings"] == []


def test_unknown_content_yields_no_findings(replay):
    kind, body = replay.answer("a sentence that appears in no fixture anywhere")
    assert kind == "distil/unmatched"
    assert json.loads(body)["findings"] == []


# --- the merge rule fires on the pair it was written for, and nothing else -

def _synthesis_prompt(*texts: str) -> str:
    listing = "\n".join(f"[id-{i}] (learning) {t}" for i, t in enumerate(texts))
    return ("PURPOSE: p\n\nCURRENT WORKING MEMORY:\n(empty)\n\n"
            f"FINDINGS:\n{listing}")


def _goldens(segment_id: str) -> str:
    return json.loads((FIXTURES / "findings" / f"{segment_id}.findings.json")
                      .read_text())[0]["text"]


def test_the_corpus_duplicate_pair_merges(replay):
    """seg-005a and seg-005b are deliberately worded apart — "about 40 ms" vs
    "roughly 40 milliseconds" — which is the difficulty a real synthesizer
    exists to handle."""
    verdict = json.loads(replay._synthesis(
        _synthesis_prompt(_goldens("seg-005a"), _goldens("seg-005b"))))
    assert len(verdict["merges"]) == 1
    assert sorted(verdict["merges"][0]["source_ids"]) == ["id-0", "id-1"]


def test_the_merged_text_is_built_from_the_sources_not_written_by_the_stub(replay):
    """An earlier version returned a hand-authored sentence here, which landed
    in a real SYNTHESIZED Finding and read as model output. The merged text
    must be traceable to its sources, word for word."""
    a, b = _goldens("seg-005a"), _goldens("seg-005b")
    merged = json.loads(replay._synthesis(_synthesis_prompt(a, b)))["merges"][0]["text"]
    assert a in merged and b in merged


@pytest.mark.parametrize("intruder", [
    "the retry backoff in the client is 140 ms",
    "the dma path takes 1440 ms end to end",
])
def test_a_nearby_number_is_not_swept_into_the_merge(replay, intruder):
    """`"40 ms" in text` also matches 140 ms and 1440 ms. The demo deliberately
    stays running and invites more input, so a teammate contributing one of
    these would have had it silently merged into the DMA finding — a wrong
    merge that tombstones a real one, live."""
    verdict = json.loads(replay._synthesis(
        _synthesis_prompt(_goldens("seg-005a"), _goldens("seg-005b"), intruder)))
    # The pair still merges — excluding the intruder is the property, not
    # refusing to work. `id-2` is the intruder; it must not be a source.
    assert len(verdict["merges"]) == 1
    assert sorted(verdict["merges"][0]["source_ids"]) == ["id-0", "id-1"]
    assert intruder not in verdict["merges"][0]["text"]


def test_no_merge_when_only_one_source_carries_the_signature(replay):
    verdict = json.loads(replay._synthesis(
        _synthesis_prompt(_goldens("seg-005a"), "an unrelated finding about logging")))
    assert verdict["merges"] == []


# --- retrieval ordering must not be dressed up as ranking ------------------

def test_ranking_is_labelled_identity_because_that_is_what_it_is(replay):
    """The stand-in returns every candidate in the order it was handed them,
    with the query unread. Naming the call `rank/identity` is what stops an
    operator reading the log as evidence of relevance."""
    kind, body = replay.answer(
        'return {"ranked": [..]}\nFINDINGS:\n[0] (learning) a\n[1] (learning) b')
    assert kind == "rank/identity"
    assert json.loads(body)["ranked"] == [0, 1]
