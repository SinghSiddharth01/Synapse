"""Producer tests — write-ahead durability and the egress rule."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from synapse_contracts import Attribution, Finding, FindingType

from synapse_worker.producer import FileSink, FindingSink, Producer, read_last_bound_shared_id

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


# ---------------------------------------------------------------------------
# The re-join envelope (STATE.md trap #8 / relay.py's round-3 note, applied
# one hop upstream). Every line is now `{produced_at, shared_id, finding}` --
# `shared_id` is the Shared Session THIS Producer was bound to (`rebind()`)
# at the moment `record()` ran, captured once and carried forever. `flush()`
# only ever sends findings whose recorded `shared_id` matches the CURRENT
# binding; a mismatch is held, never retargeted, never dropped, and never
# counted in `flush()`'s own two-tuple (that return shape is a public
# contract other callers, including the closed-loop end-to-end test,
# depend on unchanged) -- `pending_count()` is where held is exposed.
# ---------------------------------------------------------------------------

async def test_rejoin_holds_a_queued_finding_rather_than_sending_it_under_the_new_session(
    tmp_path,
) -> None:
    """The re-join scenario itself: record while bound to session-A, then
    re-join to a DIFFERENT session (session-B) before the finding ever sends.
    `flush()` must not silently ship it to session-B -- that's exactly the
    leak trap #8 describes one hop downstream of this module."""
    sink = RecordingSink()
    producer = Producer(tmp_path, sink, "session-A")
    producer.record([finding("f-1")])

    producer.rebind("session-B")                    # re-join to a DIFFERENT session
    sent, pending = await producer.flush()

    assert (sent, pending) == (0, 0)                 # nothing DELIVERABLE was attempted
    assert sink.batches == []                        # never sent to session-B
    assert producer.pending_count() == (0, 1)         # held is visible here, not lost


async def test_rejoin_back_to_the_original_session_drains_the_held_finding(tmp_path) -> None:
    """The other half of the scenario: re-joining BACK to the session a
    held finding was actually produced under must drain it, not strand it
    forever."""
    sink = RecordingSink()
    producer = Producer(tmp_path, sink, "session-A")
    producer.record([finding("f-1")])
    producer.rebind("session-B")
    await producer.flush()                            # held, confirmed above

    producer.rebind("session-A")                       # re-join BACK to the original
    sent, pending = await producer.flush()

    assert (sent, pending) == (1, 0)
    assert [f.id for f in sink.batches[0]] == ["f-1"]
    assert producer.pending_count() == (0, 0)


async def test_legacy_bare_line_wal_still_drains(tmp_path) -> None:
    """A line written before the `shared_id` envelope existed has no
    `shared_id` key at all. It must read as `None`, and `None` matches
    whatever session is CURRENTLY bound -- a pre-existing WAL keeps draining
    exactly as it did before this change, rather than being silently
    stranded by a partitioning rule it predates."""
    sink = RecordingSink()
    producer = Producer(tmp_path, sink, "session-A")
    legacy_line = json.dumps(
        {"produced_at": "2026-08-01T00:00:00+00:00", "finding": finding("f-legacy").model_dump(mode="json")}
    )
    producer.findings_path.write_text(legacy_line + "\n", encoding="utf-8")

    sent, pending = await producer.flush()

    assert (sent, pending) == (1, 0)
    assert [f.id for f in sink.batches[0]] == ["f-legacy"]


async def test_pending_count_splits_deliverable_from_held(tmp_path) -> None:
    producer = Producer(tmp_path, FailingSink(), "session-A")
    producer.record([finding("f-a")])
    producer.rebind("session-B")
    producer.record([finding("f-b")])

    assert producer.pending_count() == (1, 1)


async def test_a_finding_recorded_while_unbound_matches_any_binding(tmp_path) -> None:
    """`record()` with no explicit override tags the CURRENT binding --
    `None` if this Producer was never bound to anything. That's a stronger
    version of the legacy-line guarantee: it can never be mistaken for a
    real session id, so it stays deliverable under whatever binds next
    rather than being invented a wrong target."""
    sink = RecordingSink()
    producer = Producer(tmp_path, sink)              # never bound (shared_id=None)
    producer.record([finding("f-1")])

    producer.rebind("session-A")
    sent, pending = await producer.flush()

    assert (sent, pending) == (1, 0)


# ---------------------------------------------------------------------------
# The last-bound marker (fixer-major regression). `cli._current_shared_id`'s
# un-joined fallback needs a durable trace of what an un-joined `run
# --shared-id X`'s in-memory binding actually was -- `rebind()` is the only
# place that ever changes it, so it is the only place that can durably
# record it. See producer.py's module-docstring AMENDMENT for why the first
# attempt (re-deriving "current" from the WAL's own last WRITTEN envelope)
# reopened trap #8 rather than closing it.
# ---------------------------------------------------------------------------

async def test_rebind_persists_the_live_binding_for_a_fresh_producer_to_read(tmp_path) -> None:
    """A fresh `Producer` built with no `shared_id` (simulating a new
    process -- `synapse-worker status`/`replay`) must be able to read back
    what the last real `rebind()` on this WAL directory bound to, without
    needing anything to have been `record()`ed."""
    producer = Producer(tmp_path, RecordingSink())
    producer.rebind("team-a")

    assert read_last_bound_shared_id(tmp_path) == "team-a"


async def test_last_bound_marker_survives_a_rebind_that_records_nothing(tmp_path) -> None:
    """The exact fixer-major scenario this closes. Two sequential un-joined
    `run --shared-id ...` invocations share one WAL directory; the SECOND
    run produces no finding of its own. A log-inspection fallback (the
    superseded `last_recorded_shared_id`) would still read the FIRST run's
    tag off the WAL's last-written line, since nothing new was ever written
    under the second session -- a later `replay` would then flush the first
    run's queued finding as though it were still current, while the live
    session had actually moved on to the second. `rebind()` persisting on
    every call, whether or not a `record()` follows it, is what fixes it."""
    producer_a = Producer(tmp_path, FailingSink(), "team-a")
    producer_a.rebind("team-a")             # WorkerLoop.__init__, run 1
    producer_a.record([finding("f-1")])     # queued -- sink is down
    await producer_a.flush()                # still queued after this

    producer_b = Producer(tmp_path, FailingSink())  # a second process/run
    producer_b.rebind("team-b")             # WorkerLoop.__init__, run 2
    # Run 2 produces nothing durable of its own -- no record() call.

    # A THIRD process (e.g. `synapse-worker replay`), un-joined, must read
    # "team-b" -- the most recently LIVE binding -- not "team-a", the most
    # recently WRITTEN one.
    current = read_last_bound_shared_id(tmp_path)
    assert current == "team-b"

    replaying = Producer(tmp_path, RecordingSink(), current)
    sent, pending = await replaying.flush()

    assert (sent, pending) == (0, 0)          # f-1 was NOT wrongly delivered
    assert replaying.pending_count() == (0, 1)  # visible as held, not lost or misrouted


async def test_last_bound_marker_absent_when_nothing_has_ever_rebound(tmp_path) -> None:
    """No `rebind()` call at all (e.g. a Producer built and used entirely
    unbound) leaves no marker -- `cli._current_shared_id` must fall through
    to `DEFAULT_SHARED_ID` in this case, not crash or invent a value."""
    Producer(tmp_path, RecordingSink())  # constructor alone never calls rebind()

    assert read_last_bound_shared_id(tmp_path) is None


async def test_last_bound_marker_survives_a_process_restart(tmp_path) -> None:
    """The marker is what makes `rebind()`'s binding durable ACROSS
    processes, not just across calls on the same Producer instance."""
    first = Producer(tmp_path, RecordingSink())
    first.rebind("team-a")
    del first  # simulate the process exiting

    restarted = Producer(tmp_path, RecordingSink())  # a fresh process's Producer
    assert read_last_bound_shared_id(tmp_path) == "team-a"
    assert restarted.shared_id is None  # the constructor itself doesn't auto-read it --
    # `cli._current_shared_id` is what wires the two together
