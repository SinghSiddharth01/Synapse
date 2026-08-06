"""The pack's own few-shots must never come back as a teammate's finding.

W7 F1, measured live on 2026-08-06 against a real orchestrator, a real service
and a real `claude -p` subprocess (docs/overnight/w7-live-evidence.md §3, §5):
one contributed paragraph produced NINE findings, and SIX of them were
v4-condense's two few-shot outputs paraphrased. Durable brokers, in-memory
queues, template caches — none of it in the contributed prose; `grep -rl
"durable broker"` over the whole worktree returned exactly one file, the prompt
pack. They reached shared memory as `Provenance.CONTRIBUTED`, attributed to a
real engineer, in a store whose entire promise is "this is a teammate's verified
experience, not a hypothesis".

WHY NOTHING CAUGHT IT. `guards.assert_prompt_conditioned` asks whether the model
read its prompt; it had, far too well. `test_fixture_contamination.py` checks
that no FIXTURE duplicates a PACK — the inverse direction — and was green
throughout. The direction that failed had no test at all. This file is that
direction: pack -> output, replayed from the recorded payload.

The nine texts below are quoted verbatim from the evidence file's §3, in the
order the live `query` returned them. They are frozen evidence: if the pack's
few-shots are reworded, these must keep dropping (the guard measures similarity,
not equality) and the three genuine ones must keep surviving.
"""

from __future__ import annotations

import json
import logging

import pytest
from synapse_contracts import LocalBinding
from synapse_providers import FakeProvider

from synapse_distiller import Distiller
from synapse_distiller.distiller import (
    DROP_EXAMPLE_ECHO,
    ECHO_MIN_WORDS,
    ECHO_SIMILARITY_THRESHOLD,
    example_echo_match,
    is_example_echo,
)
from synapse_distiller.fixtures import available_fixtures, load_goldens, load_segment
from synapse_distiller.promptpack import available_packs, load_pack_by_name

BINDING = LocalBinding(
    agent_session_id="79e2ca77-71d4-46cd-8088-1e9288928971",
    shared_id="sh-d5397a07",
    contributor="sid",
    agent="claude-code",
)

PACK = load_pack_by_name("v4-condense")

# --- the recorded W7 run 2 payload ------------------------------------------------
#
# Nine findings, verbatim, in returned order. The classification is the evidence
# file's (§3), arrived at by reading each against the pack and against the prose
# that was actually contributed.

ECHOED = [
    # few-shot 1, output 1
    "Message queue was moved to a durable broker so messages are not lost under load.",
    # few-shot 2, output 4
    "The impact of the added network round trip on overall page latency has not been measured.",
    # few-shot 2, output 1
    "Caching rendered templates in each worker's local memory fails across multiple "
    "workers: copies drift after deploys, and users get stale pages depending on which "
    "worker serves them.",
    # few-shot 1, output 2
    "The durable broker's throughput is roughly one-third that of the in-memory queue, "
    "because each message waits for a disk write before being acknowledged.",
    # few-shot 2, output 2
    "Template cache was moved to a shared store that every worker reads from, replacing "
    "per-worker in-memory caching.",
    # few-shot 2, output 3
    "Accessing the template cache from a shared store adds a network round trip to every "
    "render.",
]

GENUINE = [
    "Use the local command-line distiller as a workaround on this machine instead of the "
    "primary distiller.",
    "Authentication credential field can be present but contain an empty string, causing "
    "silent authentication failures when the credential provider does not validate the "
    "field and defers to the SDK, which reports a generic error.",
    "An alternative distiller implementation that shells out to a local command-line tool "
    "requires no separate credential configuration.",
]

# The response as the parse path received it, with the types the live run reported
# and the interleaving the live run returned.
CONTAMINATED_RESPONSE = {
    "findings": [
        {"type": "decision", "text": GENUINE[0]},
        {"type": "learning", "text": GENUINE[1]},
        {"type": "decision", "text": ECHOED[0]},
        {"type": "learning", "text": GENUINE[2]},
        {"type": "open_question", "text": ECHOED[1]},
        {"type": "dead_end", "text": ECHOED[2]},
        {"type": "learning", "text": ECHOED[3]},
        {"type": "decision", "text": ECHOED[4]},
        {"type": "learning", "text": ECHOED[5]},
    ]
}

# Findings about genuine queue, broker, cache, render and latency work — the
# vocabulary the examples own. Every one of these is the kind of thing a real
# session produces, and every one must survive. A guard that clears F1 by
# refusing to record anything about a message queue has not fixed F1.
GENUINE_ON_THE_EXAMPLES_TOPICS = [
    "The ingest queue was moved onto the shared broker after the in-memory one lost work "
    "whenever a worker restarted.",
    "Throughput dropped once every write was acknowledged synchronously, but the team "
    "accepted it because losing a record was worse.",
    "Caching the compiled ranking model in each process was abandoned; the copies "
    "disagreed after a redeploy and answers differed by host.",
    "Reading the session index from the shared store adds a round trip, which shows up on "
    "the first render of a cold page.",
    "The template renderer was replaced with a streaming one, which every worker now uses.",
    "The durable broker is already in production for billing, so reusing it saved standing "
    "up new infrastructure.",
]


# --- the replay ------------------------------------------------------------------


async def test_the_recorded_w7_payload_loses_its_six_echoes_and_keeps_its_three(
    caplog,
) -> None:
    """THE test that was missing on the night F1 was found.

    The exact nine findings the live arm returned, replayed through the parse
    path with the shipped pack. Six out, three in — the arithmetic §5 of the
    evidence rests on.
    """
    provider = FakeProvider(scripts=[CONTAMINATED_RESPONSE])
    distiller = Distiller(provider, BINDING, pack=PACK)

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        findings, stats = await distiller.distil(load_segment("seg-001"))

    assert [f.text for f in findings] == GENUINE
    assert stats.dropped_example_echo == 6
    assert stats.example_echoes == ECHOED
    assert stats.dropped_malformed == 0, "the response parsed fine; only findings were dropped"


async def test_every_dropped_echo_says_reason_example_echo_and_names_what_it_echoed(
    caplog,
) -> None:
    """Loudly, and with the evidence attached. An operator who sees a finding
    count fall must be able to read WHICH example was echoed and how close the
    call was, without re-running anything — the same standard the three
    unparseable-response reasons are held to."""
    provider = FakeProvider(scripts=[CONTAMINATED_RESPONSE])

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        await Distiller(provider, BINDING, pack=PACK).distil(load_segment("seg-001"))

    drops = [r.getMessage() for r in caplog.records if "dropping finding" in r.getMessage()]

    assert len(drops) == 6
    for message in drops:
        assert f"reason={DROP_EXAMPLE_ECHO}" in message
        assert "similarity=" in message
        assert "threshold=0.60" in message
        assert "pack=v4-condense" in message
        assert "echoed_example=" in message


async def test_a_clean_response_is_untouched_and_records_no_echo_drop() -> None:
    """The guard has to be invisible when it is not firing, or every count and
    every log downstream becomes unreadable."""
    clean = {"findings": [{"type": "learning", "text": t} for t in GENUINE]}
    provider = FakeProvider(scripts=[clean])

    findings, stats = await Distiller(provider, BINDING, pack=PACK).distil(
        load_segment("seg-001")
    )

    assert [f.text for f in findings] == GENUINE
    assert stats.dropped_example_echo == 0
    assert stats.example_echoes == []


# --- the other side of the bar ----------------------------------------------------


@pytest.mark.parametrize("text", GENUINE_ON_THE_EXAMPLES_TOPICS)
async def test_real_work_on_the_examples_own_topics_survives(text: str) -> None:
    """The expensive direction. A finding lost here is device work permanently
    gone — the worker never re-reads a transcript position — so the guard eating
    a real finding is worse than the echo it was aiming at, which at least shows
    up in the log and in the next query."""
    provider = FakeProvider(scripts=[{"findings": [{"type": "learning", "text": text}]}])

    findings, stats = await Distiller(provider, BINDING, pack=PACK).distil(
        load_segment("seg-001")
    )

    assert [f.text for f in findings] == [text]
    assert stats.dropped_example_echo == 0


async def test_a_finding_about_real_queue_work_is_not_eaten_by_the_queue_example() -> None:
    """Stated on its own rather than only inside the sweep above, because it is
    the specific worry: the pack's loudest example is about a message queue moved
    to a durable broker, and a team that genuinely does that work must still be
    able to record it."""
    text = (
        "The ingest queue was moved onto the shared broker after the in-memory one lost "
        "work whenever a worker restarted."
    )
    provider = FakeProvider(scripts=[{"findings": [{"type": "decision", "text": text}]}])

    findings, stats = await Distiller(provider, BINDING, pack=PACK).distil(
        load_segment("seg-001")
    )

    assert [f.text for f in findings] == [text]
    assert stats.dropped_example_echo == 0


def test_the_bar_sits_in_a_measured_gap_not_next_to_either_side() -> None:
    """Asserted as a gap rather than as a threshold, following test_drop_reasons:
    a measure that scored the echoes at 0.61 and real work at 0.59 would pass
    every test above that only checks its own side, while classifying live
    traffic at a coin flip."""
    examples = PACK.example_finding_texts
    echo_scores = [example_echo_match(t, examples)[0] for t in ECHOED]
    real_scores = [
        example_echo_match(t, examples)[0] for t in GENUINE + GENUINE_ON_THE_EXAMPLES_TOPICS
    ]

    assert min(echo_scores) > ECHO_SIMILARITY_THRESHOLD
    assert max(real_scores) < ECHO_SIMILARITY_THRESHOLD
    assert min(echo_scores) - max(real_scores) > 0.15, (
        f"echoes bottom out at {min(echo_scores):.3f} and real work tops out at "
        f"{max(real_scores):.3f} — too close to call the bar honest"
    )


def test_no_golden_finding_in_the_corpus_reads_as_an_echo_of_any_pack() -> None:
    """The broadest false-positive check available offline: every hand-written
    golden finding in fixtures/, scored against every shipped pack's examples.
    These are the closest thing the repo has to known-good distiller output."""
    for pack_name in available_packs():
        examples = load_pack_by_name(pack_name).example_finding_texts
        if not examples:
            continue
        for fixture_id in available_fixtures():
            try:
                goldens = load_goldens(fixture_id)
            except FileNotFoundError:
                continue
            for golden in goldens:
                score, nearest = example_echo_match(golden.text, examples)
                assert score < ECHO_SIMILARITY_THRESHOLD, (
                    f"{fixture_id} golden {golden.text[:60]!r} scores {score:.3f} "
                    f"against {pack_name}'s example {nearest[:60]!r}"
                )


# --- the measure itself -----------------------------------------------------------


def test_an_example_returned_verbatim_is_always_caught() -> None:
    """The floor case. If this ever fails, the corpus is not being read at all."""
    examples = PACK.example_finding_texts

    assert len(examples) == 6, "v4-condense teaches six notes across its two few-shots"
    for example in examples:
        score, nearest = example_echo_match(example, examples)
        assert score == 1.0 and nearest == example
        assert is_example_echo(example, examples)


def test_a_short_note_is_exempt_because_short_strings_collide_by_accident() -> None:
    """"Page latency has not been measured." scores 0.600 against an example it
    shares no intent with, and "The queue was switched." is a subsequence of
    one. Both are far shorter than the one-or-two sentences the pack asks for,
    and both could be real. Under the floor the guard does not get a vote."""
    examples = PACK.example_finding_texts

    for short in ("The queue was switched.", "Page latency has not been measured."):
        assert len(short.split()) < ECHO_MIN_WORDS
        assert example_echo_match(short, examples) == (0.0, "")
        assert not is_example_echo(short, examples)


def test_the_corpus_follows_the_pack_rather_than_a_list_kept_beside_it(tmp_path) -> None:
    """The property that makes this survive the next prompt edit: an example
    added to a pack's TOML is guarded the moment it is added. A hand-kept list
    would have been stale at exactly the edit that most needed it."""
    path = tmp_path / "invented.toml"
    path.write_text(
        'name = "invented"\nsystem = "s"\n\n'
        "[[few_shots]]\n"
        'input = "agent: something happened"\n'
        "output = '''"
        + json.dumps(
            {
                "findings": [
                    {
                        "type": "learning",
                        "text": (
                            "The nightly reindex was rewritten to stream rows instead of "
                            "buffering the whole table, because the box ran out of memory "
                            "at about four million rows."
                        ),
                    }
                ]
            }
        )
        + "'''\n",
        encoding="utf-8",
    )
    from synapse_distiller.promptpack import load_pack

    pack = load_pack(path)
    invented = pack.example_finding_texts[0]

    assert is_example_echo(invented, pack.example_finding_texts)
    # ...and it is not in v4-condense's corpus, so the guard is genuinely reading
    # the pack it was handed rather than a constant.
    assert not is_example_echo(invented, PACK.example_finding_texts)


def test_a_pack_teaching_a_non_findings_shape_contributes_no_corpus(tmp_path) -> None:
    """`load_pack` guarantees a few-shot output is valid JSON, not that it is the
    findings shape. A future pack teaching something else must yield an empty
    corpus — which disables the guard for that pack — rather than raise on the
    first call to distil()."""
    from synapse_distiller.promptpack import load_pack

    path = tmp_path / "other.toml"
    path.write_text(
        'name = "other"\nsystem = "s"\n\n'
        '[[few_shots]]\ninput = "agent: hello"\noutput = \'["not", "the", "shape"]\'\n',
        encoding="utf-8",
    )

    assert load_pack(path).example_finding_texts == ()


def test_every_shipped_pack_can_produce_its_corpus_without_raising() -> None:
    for pack_name in available_packs():
        assert isinstance(load_pack_by_name(pack_name).example_finding_texts, tuple)
