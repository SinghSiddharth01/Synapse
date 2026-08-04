"""Producer tests — write-ahead durability and the egress rule."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from synapse_contracts import Attribution, Finding, FindingType

from synapse_worker.producer import FileSink, FindingSink, Producer

TS = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


def finding(fid: str, text: str = "a finding") -> Finding:
    return Finding(
        id=fid,
        type=FindingType.LEARNING,
        text=text,
        attributions=[
            Attribution(contributor="aditya", agent_session="sess-1", agent="claude-code")
        ],
        ts=TS,
    )


class FailingSink(FindingSink):
    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, findings: list[Finding]) -> bool:
        self.attempts += 1
        return False


class RecordingSink(FindingSink):
    def __init__(self) -> None:
        self.batches: list[list[Finding]] = []

    async def send(self, findings: list[Finding]) -> bool:
        self.batches.append(list(findings))
        return True


async def test_findings_are_on_disk_before_any_send(tmp_path) -> None:
    """The core ordering. Distillation is ~13 tok/s and unrepeatable, so a
    finding lost between distillation and a failed POST is gone for good."""
    sink = FailingSink()
    producer = Producer(tmp_path, sink)

    producer.record([finding("f-1")])

    assert producer.findings_path.is_file()
    assert sink.attempts == 0  # nothing sent yet
    assert [f.id for f in producer.unsent()] == ["f-1"]


async def test_unsent_replays_after_a_restart(tmp_path) -> None:
    producer = Producer(tmp_path, FailingSink())
    producer.record([finding("f-1"), finding("f-2")])
    await producer.flush()

    restarted = Producer(tmp_path, RecordingSink())
    sent, pending = await restarted.flush()

    assert (sent, pending) == (2, 0)


async def test_delivered_findings_are_not_resent(tmp_path) -> None:
    sink = RecordingSink()
    producer = Producer(tmp_path, sink)
    producer.record([finding("f-1")])
    await producer.flush()

    producer.record([finding("f-2")])
    await producer.flush()

    assert [[f.id for f in batch] for batch in sink.batches] == [["f-1"], ["f-2"]]


async def test_findings_are_retained_after_sending(tmp_path) -> None:
    """Retention is what makes a service restart a resync rather than a loss."""
    producer = Producer(tmp_path, RecordingSink())
    producer.record([finding("f-1")])
    await producer.flush()

    lines = producer.findings_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert producer.unsent() == []


async def test_failed_send_leaves_everything_queued(tmp_path) -> None:
    producer = Producer(tmp_path, FailingSink())
    producer.record([finding("f-1"), finding("f-2")])

    sent, pending = await producer.flush()

    assert (sent, pending) == (0, 2)
    assert len(producer.unsent()) == 2


async def test_replay_carries_identical_ids(tmp_path) -> None:
    """Finding.id is stamped at distil time, so ingest upserts and a duplicated
    send is harmless."""
    producer = Producer(tmp_path, FailingSink())
    producer.record([finding("f-1")])
    await producer.flush()

    sink = RecordingSink()
    retried = Producer(tmp_path, sink)
    await retried.flush()

    assert [f.id for f in sink.batches[0]] == ["f-1"]


async def test_only_findings_reach_the_sink(tmp_path) -> None:
    """The egress rule, at the last place it could be broken. No segment ids, no
    events, no raw transcript text."""
    sink_file = tmp_path / "upstream.jsonl"
    producer = Producer(tmp_path, FileSink(sink_file))
    producer.record([finding("f-1", "an abstracted finding")])
    await producer.flush()

    payload = json.loads(sink_file.read_text(encoding="utf-8").strip())

    assert set(payload) == set(Finding.model_fields)
    assert payload["text"] == "an abstracted finding"


async def test_recording_nothing_is_a_no_op(tmp_path) -> None:
    producer = Producer(tmp_path, RecordingSink())
    producer.record([])

    assert producer.unsent() == []


async def test_malformed_log_entry_is_skipped_not_fatal(tmp_path) -> None:
    producer = Producer(tmp_path, RecordingSink())
    producer.record([finding("f-1")])
    with producer.findings_path.open("a", encoding="utf-8") as handle:
        handle.write("{ torn write\n")

    assert [f.id for f in producer.unsent()] == ["f-1"]
