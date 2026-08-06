"""What the distiller SAYS when it drops a segment (7c42e96).

The drop path is the one place a lost segment is visible at all — the worker
never re-reads a transcript position, so a segment dropped here is device work
permanently gone. `raw[:200]` was added to that log because truncated-mid-object,
prose-wrapped-JSON, and near-ceiling repetition are three different problems
that produce the identical `dropped_malformed` counter, and the raw text is the
only thing that tells them apart. It was already being captured into
`DistillStats.raw_outputs` and then never shown to anyone.

Nothing asserted the log line, so reverting it to a bare "unparseable output"
left the suite green — `caplog` appears nowhere else in this package's tests.
"""

from __future__ import annotations

import logging

from synapse_contracts import LocalBinding
from synapse_providers import FakeProvider

from synapse_distiller import Distiller
from synapse_distiller.distiller import MAX_ATTEMPTS
from synapse_distiller.fixtures import load_segment

BINDING = LocalBinding(
    agent_session_id="as-fixture-001",
    shared_id="shared-1",
    contributor="aditya",
    agent="claude-code",
)


async def test_the_drop_log_carries_the_raw_text_that_explains_the_drop(caplog) -> None:
    """The three causes are indistinguishable from the counter alone.

    A response cut off at max_tokens, one wrapped in prose, and one that
    degenerated into repetition all increment `dropped_malformed` and all log
    the same sentence. What separates them is the first 200 characters of what
    the model actually said, so those characters have to be IN the record.
    """
    segment = load_segment("seg-001")
    cut_off = '{"findings": [{"type": "learning", "text": "the reserve is a prom'
    provider = FakeProvider(scripts=[cut_off, cut_off])

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        findings, stats = await Distiller(provider, BINDING).distil(segment)

    assert findings == [] and stats.attempts == MAX_ATTEMPTS
    drops = [r for r in caplog.records if "unparseable" in r.getMessage()]
    assert len(drops) == MAX_ATTEMPTS, "one line per attempt, not one per segment"
    for record in drops:
        message = record.getMessage()
        assert cut_off in message, (
            "the raw text is the whole diagnosis; without it the log cannot "
            "distinguish truncation from prose-wrapping from repetition"
        )
        assert segment.id in message, "and it has to name which segment was lost"


async def test_the_drop_log_truncates_the_raw_text_it_quotes(caplog) -> None:
    """200 chars, not the whole response.

    The raw output can be the full `max_tokens` worth of text, and this line
    fires twice per dropped segment. Quoting all of it would push the rest of
    the run's logs out of any bounded buffer at exactly the moment they matter.
    """
    segment = load_segment("seg-001")
    noise = "not json " * 200
    provider = FakeProvider(scripts=[noise, noise])

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        await Distiller(provider, BINDING).distil(segment)

    drops = [r for r in caplog.records if "unparseable" in r.getMessage()]
    assert drops
    for record in drops:
        assert noise[:200] in record.getMessage()
        assert noise not in record.getMessage(), "the full response must not be quoted"


async def test_a_segment_dropped_after_both_attempts_is_logged_as_an_error(caplog) -> None:
    """The per-attempt line is a WARNING; losing the segment is an ERROR. They
    are different severities because they are different events — the first is
    recoverable by the retry, the second is permanent."""
    segment = load_segment("seg-001")
    provider = FakeProvider(scripts=["garbage", "garbage again"])

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        await Distiller(provider, BINDING).distil(segment)

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert segment.id in errors[0].getMessage()
    assert "dropping" in errors[0].getMessage()


async def test_a_segment_that_parses_on_the_retry_logs_no_drop(caplog) -> None:
    """The mirror. A recovered segment must not leave an ERROR behind, or the
    log stops being usable as a count of what was actually lost."""
    segment = load_segment("seg-001")
    provider = FakeProvider(
        scripts=["garbage", {"findings": [{"type": "learning", "text": "recovered"}]}]
    )

    with caplog.at_level(logging.WARNING, logger="synapse_distiller.distiller"):
        findings, _ = await Distiller(provider, BINDING).distil(segment)

    assert [f.text for f in findings] == ["recovered"]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
