"""Durable write-ahead log and the push upstream.

Ordering is the whole point: **findings are persisted the moment the distiller
returns, before any send is attempted.** Distillation is the most expensive step
in the system — roughly 13 tok/s on the NPU — and it is unrepeatable, because
the follower never re-reads a transcript position. A finding lost between
distillation and a failed POST is device work permanently gone. This is
write-ahead, not fallback buffering.

Both files are append-only, which is what makes them crash-safe without locking:

    findings.jsonl   every Finding ever produced, in order
    sent.jsonl       the id of every Finding confirmed delivered

Unsent = the difference. Marking something sent never rewrites history, so a
crash mid-write costs at most one duplicated send — and duplicates are harmless
because `Finding.id` is client-assigned at distil time and ingest upserts by id.

Findings are retained after sending so a service restart can be answered with a
resync rather than a shrug.

THE EGRESS RULE. Only `Finding` objects reach a sink. Segments, AgentEvents and
raw transcript text never enter this module — that is the property the whole
architecture rests on, and this is the last place it could be broken.

#### The re-join envelope (STATE.md trap #8, closing the gap `relay.py`'s
#### round-3 amendment left open one hop upstream)

`synapse_orchestrator.relay.Relay` partitions its own write-ahead log by the
Shared Session bound WHEN each Finding was recorded, so a `rebind()` between
a failed send and the next retry can never retarget a queued Finding to the
wrong session. Round 3 of that review found the fix stopped there: a Finding
still sitting in THIS module's log at the moment of a `synapse-worker join
<new_id>` carried no Shared Session identity at all (`{produced_at, finding}`
— see git history), so a retried send after a re-join was indistinguishable
from a fresh Finding produced under the new session. Both arrive with the
same `agent_session_id` (a re-join does not start a new Agent Session), and
`record()` necessarily tags a retry with whatever binding resolves *now*.

Fixed the same way, one hop upstream: every line is now an envelope,
`{"produced_at": ..., "shared_id": <the Shared Session bound WHEN record()
was called>, "finding": {...}}`. `flush()` reads that back and sends ONLY
findings whose recorded `shared_id` matches the CURRENT binding
(`self.shared_id`, set at construction or by `rebind()`) — never to whatever
is bound now if that differs from what was bound at record time. A mismatch
is HELD, not retargeted and not dropped: it simply waits for a later
`rebind()` back to the session it was actually produced under.

This is deliberately NOT the same rule `Relay` uses. `Relay` sends each group
to ITS OWN recorded session, because it knows the destination URL for every
session (`/v1/sessions/{id}/...`). This module's sink is a single upstream
endpoint with no per-session addressing on the wire (the producer endpoint
resolves routing itself, live, from `Attribution.agent_session` — see
`relay.py`'s round-3 note on `app.py`), so there is nowhere to "send it to
its own session" from here. Holding until the binding matches again is the
only safe option that is neither a wrong guess nor a silent drop.

A line with no `shared_id` key at all — every line written before this
envelope existed — reads back as `None`, and `None` always matches: a
pre-existing WAL keeps draining exactly as it did before this change,
instead of being silently stranded by a partitioning rule it predates. A
Finding explicitly recorded while unbound (`self.shared_id is None`) gets
the same treatment for the same reason — it can never be mistaken for a
wrong real session, so it stays deliverable under whatever binds next.

AMENDMENT (fixer pass, major): the envelope above assumes something else
knows what "the current binding" is when a fresh `Producer` is constructed
with no `WorkerLoop` around it (`synapse-worker status` / `replay`'s plain
path — `cli._current_shared_id`). Before this fix, that CLI helper only ever
consulted the `join`ed binding file, falling back to a hardcoded
`DEFAULT_SHARED_ID` ("local-dev") when nothing was joined. But `run
--shared-id X` with no prior `join` still tags every envelope it writes with
`X` (`WorkerLoop.__init__` -> `rebind(binding.shared_id)`) — that binding
lives only in THAT process's memory, not on disk anywhere `_current_shared_id`
looked. A later `status`/`replay` in a fresh process would then resolve
"current" to "local-dev", see every entry tagged `X` as a mismatch, and
report the run's own findings as `held (other session)` — held from itself,
undeliverable until the user happened to `join X` even though nothing was
ever actually re-targeted.

A first attempt at this fix (re-deriving "current" from the `shared_id` tag
on the WAL's own most recently WRITTEN envelope) turned out to just move the
leak rather than close it: it only ever updated when a NEW finding was
recorded, so a SECOND un-joined `run --shared-id Y` that never happened to
produce a finding of its own left the WAL's last line — and therefore
"current" — reading as the FIRST run's `X`, even though `Y` was the actually
live session. A `replay` in a third process would then read "current" as
`X`, treat `X`-tagged entries as deliverable, and ship them — while the
live Shared Session was really `Y`. That is a real wrong-session delivery,
worse than the cosmetic mis-report it replaced, and STATE.md trap #8 names
exactly this class of bug.

`rebind()` now persists the binding it is called with to a small marker file
in `log_dir` (`_last_bound_path`), and `read_last_bound_shared_id()` below
reads it back. This closes the gap `last_recorded_shared_id` (removed in
this pass) did not: `rebind()` runs every time a real binding goes live
(`WorkerLoop.__init__`, once per `run`), whether or not that run's segments
ever produce a finding worth `record()`ing, so the marker always reflects
the most recently LIVE binding rather than the most recently WRITTEN one.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import httpx
from synapse_contracts import Finding

logger = logging.getLogger(__name__)


def _last_bound_path(log_dir: Path) -> Path:
    return Path(log_dir) / "last-bound.json"


def read_last_bound_shared_id(log_dir: Path) -> str | None:
    """The `shared_id` the most recent real `rebind()` on this WAL directory
    was actually bound to — durable across processes, unlike the in-memory
    binding itself. `None` if `rebind()` has never run against this
    `log_dir`, or the marker is corrupt.

    See the module docstring's AMENDMENT: this is `cli._current_shared_id`'s
    fallback of last resort when nothing has been `join`ed.
    """
    path = _last_bound_path(log_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("Last-bound marker at %s is unreadable; treating as unset", path)
        return None
    return data.get("shared_id")


def _write_last_bound_shared_id(log_dir: Path, shared_id: str | None) -> None:
    """Atomic, mirroring `synapse_contracts.binding.write_binding`'s
    temp-then-replace pattern — a crash mid-write must never leave a corrupt
    marker behind for `read_last_bound_shared_id` to trip over."""
    path = _last_bound_path(log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"shared_id": shared_id}), encoding="utf-8")
    tmp.replace(path)


class FindingSink(ABC):
    """Where condensed findings go. The orchestrator, in production."""

    @abstractmethod
    async def send(self, findings: list[Finding]) -> bool:
        """True if delivered. False is normal — the caller retries later."""


class HttpSink(FindingSink):
    """POSTs to the orchestrator's producer endpoint."""

    def __init__(self, url: str, timeout: float = 30.0, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.url = url
        self.timeout = timeout
        self._transport = transport

    async def send(self, findings: list[Finding]) -> bool:
        payload = {"findings": [f.model_dump(mode="json") for f in findings]}
        try:
            async with httpx.AsyncClient(timeout=self.timeout,
                                         transport=self._transport) as client:
                response = await client.post(self.url, json=payload)
                response.raise_for_status()
            return True
        except (httpx.HTTPError, OSError) as exc:
            # Expected whenever the orchestrator is down. Not an error: the log
            # holds the findings and the next tick tries again.
            logger.info("Upstream unavailable (%s); %d findings stay queued",
                        exc.__class__.__name__, len(findings))
            return False


class FileSink(FindingSink):
    """Appends to a local JSONL file.

    The orchestrator does not exist yet, so this is what "upstream" means for
    now. It exercises the same interface and the same serialization, which is
    the part worth testing early.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    async def send(self, findings: list[Finding]) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for finding in findings:
                handle.write(json.dumps(finding.model_dump(mode="json")) + "\n")
        return True


class Producer:
    """Write-ahead log plus delivery."""

    _UNSET = object()

    def __init__(self, log_dir: Path, sink: FindingSink, shared_id: str | None = None) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.findings_path = self.log_dir / "findings.jsonl"
        self.sent_path = self.log_dir / "sent.jsonl"
        self.sink = sink
        self.shared_id = shared_id

    def rebind(self, shared_id: str | None) -> None:
        """Point future `record()` calls — and `flush()`'s notion of "the
        current binding" — at a different Shared Session.

        `WorkerLoop.__init__` calls this once, syncing a freshly-built
        Producer to the binding it was constructed with; a re-join between
        two `synapse-worker run` invocations sharing this on-disk WAL (or
        `cmd_status`/`cmd_replay`, which build a Producer with no binding of
        their own) call it too.

        Mirrors `Relay.rebind`: does NOT retarget anything already written —
        every envelope in the log keeps the `shared_id` it was recorded
        under forever (see `record()`). That is what makes the module
        docstring's re-join guarantee hold: a still-queued Finding stays
        addressed to the session it was actually produced under, not
        whatever's bound when `flush()` next runs.

        AMENDMENT (fixer pass, major): also persists `shared_id` to this
        `log_dir`'s last-bound marker (`_write_last_bound_shared_id`) — see
        the module docstring's AMENDMENT for why this, and not the WAL's own
        content, is what `cli._current_shared_id` must read as its un-joined
        fallback.
        """
        self.shared_id = shared_id
        _write_last_bound_shared_id(self.log_dir, shared_id)

    def record(self, findings: list[Finding], *, shared_id: str | None = _UNSET) -> None:
        """Persist before any send is attempted. Call this first, always.

        Each line is now an envelope, `{produced_at, shared_id, finding}` —
        see the module docstring's re-join amendment. `shared_id`, when
        given, tags EVERY Finding in this call with that exact value instead
        of `self.shared_id`, mirroring `Relay.record`'s same parameter and
        the same race it closes (a caller that already resolved its own
        target should never have this read a `self.shared_id` a concurrent
        `rebind()` changed out from under it). Omitted, this call falls back
        to `self.shared_id` — every caller today.
        """
        if not findings:
            return
        target = self.shared_id if shared_id is self._UNSET else shared_id
        produced_at = datetime.now(timezone.utc).isoformat()
        with self.findings_path.open("a", encoding="utf-8") as handle:
            for finding in findings:
                handle.write(
                    json.dumps(
                        {
                            "produced_at": produced_at,
                            "shared_id": target,
                            "finding": finding.model_dump(mode="json"),
                        }
                    )
                    + "\n"
                )

    def _sent_ids(self) -> set[str]:
        if not self.sent_path.is_file():
            return set()
        ids: set[str] = set()
        for line in self.sent_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                ids.add(line)
        return ids

    def _entries(self) -> list[tuple[str | None, Finding]]:
        """Every `(shared_id, Finding)` envelope in the log, in order. A
        missing `shared_id` key (a line written before this envelope
        existed) reads as `None` — see the module docstring's re-join
        amendment for why that's always deliverable."""
        if not self.findings_path.is_file():
            return []
        out: list[tuple[str | None, Finding]] = []
        for line in self.findings_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                finding = Finding.model_validate(record["finding"])
            except (json.JSONDecodeError, KeyError, ValueError):
                logger.warning("Skipping malformed entry in the write-ahead log")
                continue
            out.append((record.get("shared_id"), finding))
        return out

    def unsent(self) -> list[Finding]:
        """Everything recorded but not yet confirmed delivered, oldest first
        — deliverable and held alike. See `pending_count()` to split the
        two, or `flush()`, which only ever attempts the deliverable half."""
        sent = self._sent_ids()
        return [finding for _sid, finding in self._entries() if finding.id not in sent]

    def _deliverable_and_held(self) -> tuple[list[Finding], list[Finding]]:
        """Split `unsent()` by whether each entry's recorded `shared_id`
        matches the current binding (`self.shared_id`, see `rebind()`). A
        `None`-tagged entry (unbound at record time, or a pre-envelope
        legacy line) always counts as deliverable — see the module
        docstring."""
        sent = self._sent_ids()
        deliverable: list[Finding] = []
        held: list[Finding] = []
        for sid, finding in self._entries():
            if finding.id in sent:
                continue
            if sid is None or sid == self.shared_id:
                deliverable.append(finding)
            else:
                held.append(finding)
        return deliverable, held

    def pending_count(self) -> tuple[int, int]:
        """(deliverable, held) — how much of `unsent()` a `flush()` right
        now would actually attempt to send, versus how much is queued under
        a Shared Session that isn't the current binding. Never retargeted,
        never dropped — a held entry just waits for a re-join back to the
        session it was actually produced under. What `synapse-worker
        status` and the debug page's PUSH node report."""
        deliverable, held = self._deliverable_and_held()
        return len(deliverable), len(held)

    def _mark_sent(self, findings: list[Finding]) -> None:
        with self.sent_path.open("a", encoding="utf-8") as handle:
            for finding in findings:
                handle.write(f"{finding.id}\n")

    async def flush(self) -> tuple[int, int]:
        """Try to deliver everything deliverable. Returns (sent,
        still_pending) — the same two-tuple shape this method has always
        had, which `Producer`'s callers (including the closed-loop
        end-to-end test) depend on.

        Counts ONLY findings this call actually attempted: those whose
        recorded `shared_id` matched the current binding. A HELD finding
        (mismatch — see the module docstring's re-join amendment) is never
        attempted and never counted in either number here; call
        `pending_count()` for the (deliverable, held) breakdown.

        Replays across restarts for free: `unsent` is derived from the log,
        so a worker that crashed mid-send picks up exactly where it left
        off.
        """
        deliverable, _held = self._deliverable_and_held()
        if not deliverable:
            return 0, 0
        if await self.sink.send(deliverable):
            self._mark_sent(deliverable)
            return len(deliverable), 0
        return 0, len(deliverable)
