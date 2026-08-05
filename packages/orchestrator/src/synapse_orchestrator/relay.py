"""Write-ahead durable log + the sole egress to the Synapse Service (Plan D.4).

Deliberately mirrors synapse_worker.producer's file discipline rather than
importing it — the packages share contracts only, and the two logs guard
different hops. Same posture: findings.jsonl append-only, sent.jsonl marks
delivery, unsent = the difference, RETAINED after ack so `resync` can answer
a service restart (amendment F Q5). Replay is safe because ingest upserts by
Finding.id (first-write-wins, E3 Task 1).

#### Post-review amendment (2026-08-04)

Round 2 found a blocker: the log was one undifferentiated stream and
`flush()`/`resync()` sent EVERYTHING pending to `self.shared_id` — whatever
Shared Session happened to be bound at *send* time, not at *record* time. A
`synapse-worker join <other_shared_id>` between a failed flush and the next
attempt silently retargeted the whole backlog, including Findings produced
(and durably queued) under a completely different, unrelated Shared Session.

Fixed by partitioning: every line in `findings.jsonl` is now an envelope,
`{"shared_id": <the Shared Session bound WHEN record() was called>, "finding":
{...}}`. `flush()` and `resync()` group pending/retained entries by that
recorded `shared_id` and POST each group to *its own* `/v1/sessions/{id}/...`
— never to whatever `self.shared_id` (or `rebind()`) currently holds. A
Finding recorded while genuinely unbound (`shared_id` is `None`) has no
session to go to and stays queued forever; that's a stronger version of the
same guarantee ("nothing egresses without a real binding") than a wrong
guess would be. See test_relay.py's re-join tests and `resync()` below,
whose return type was also corrected in this pass (see its docstring).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from synapse_contracts import Finding

logger = logging.getLogger(__name__)


class Relay:
    def __init__(self, state_dir: Path, service_url: str, shared_id: str | None, *,
                 timeout: float = 10.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.findings_path = self.state_dir / "findings.jsonl"
        self.sent_path = self.state_dir / "sent.jsonl"
        self.service_url = service_url.rstrip("/")
        self.shared_id = shared_id
        self.timeout = timeout
        self._transport = transport

    def rebind(self, shared_id: str | None) -> None:
        """Point future `record()` calls at a different (or no) Shared Session.

        Lets a long-lived Relay track a binding that changes after boot —
        e.g. the producer endpoint re-resolving on every request — without
        losing the durable log already on disk.

        Deliberately does NOT retarget anything already written: each
        envelope in the log keeps the `shared_id` it was recorded under
        forever (see `record()`). Rebinding only changes what NEW records
        get tagged with — that is the fix for the cross-Shared-Session leak
        described in the module docstring's amendment note.
        """
        self.shared_id = shared_id

    # ── write-ahead ─────────────────────────────────────────────────────────
    def record(self, findings: list[Finding]) -> None:
        """Append each Finding, tagged with the Shared Session bound RIGHT NOW.

        That tag travels with the Finding for the rest of its life in this
        log. `flush()`/`resync()` read it back and send each Finding only to
        the Shared Session it carries — never to whatever is live/current at
        send time. That is what makes a `rebind()` between `record()` and a
        later `flush()` safe: a still-queued Finding stays addressed to the
        session it was actually produced under.
        """
        with self.findings_path.open("a", encoding="utf-8") as fh:
            for f in findings:
                envelope = {"shared_id": self.shared_id, "finding": f.model_dump(mode="json")}
                fh.write(json.dumps(envelope) + "\n")

    def _load(self, path: Path) -> list[str]:
        if not path.is_file():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _all_entries(self) -> list[tuple[str | None, Finding]]:
        """Every `(shared_id, Finding)` envelope ever recorded, in order."""
        out: list[tuple[str | None, Finding]] = []
        for line in self._load(self.findings_path):
            try:
                envelope = json.loads(line)
                finding = Finding.model_validate(envelope["finding"])
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("Skipping corrupt relay line (%s)", exc)
                continue
            out.append((envelope.get("shared_id"), finding))
        return out

    def _sent_ids(self) -> set[str]:
        return set(self._load(self.sent_path))

    def _pending(self) -> list[tuple[str | None, Finding]]:
        sent = self._sent_ids()
        return [(sid, f) for sid, f in self._all_entries() if f.id not in sent]

    def pending_count(self) -> int:
        return len(self._pending())

    def retained_count(self) -> int:
        """How many retained Findings `resync()` could possibly deliver:
        every entry ever recorded (sent or not) that carries a real Shared
        Session. A Finding recorded while unbound (`shared_id` is `None`)
        can never be delivered — there is nowhere to send it — so it never
        counts here. Lets a caller (`cli.cmd_resync`) tell "resync pushed
        everything there was to push" apart from "resync pushed less than
        the eligible total" even though `resync()` itself returns a bare
        int (see its docstring)."""
        return sum(1 for sid, _ in self._all_entries() if sid is not None)

    @staticmethod
    def _group(entries: list[tuple[str | None, Finding]]) -> dict[str, list[Finding]]:
        """Findings recorded under no session (`shared_id is None`) are
        dropped from every group — there is no session to post them to."""
        groups: dict[str, list[Finding]] = {}
        for sid, finding in entries:
            if sid is None:
                continue
            groups.setdefault(sid, []).append(finding)
        return groups

    # ── egress ──────────────────────────────────────────────────────────────
    async def _post(self, shared_id: str, findings: list[Finding]) -> bool:
        payload = {"findings": [f.model_dump(mode="json") for f in findings]}
        url = f"{self.service_url}/v1/sessions/{shared_id}/findings"
        try:
            async with httpx.AsyncClient(transport=self._transport,
                                         timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            return True
        except (httpx.HTTPError, OSError) as exc:
            logger.info("Service unavailable (%s); %d finding(s) for session %r stay queued",
                        exc.__class__.__name__, len(findings), shared_id)
            return False

    async def flush(self) -> tuple[int, int]:
        """Push every pending Finding, each to the Shared Session it was
        RECORDED under (see `record()`) — never to whatever `self.shared_id`
        happens to be right now. Returns `(sent, still-pending)`, totalled
        across every session present in the backlog. A Finding recorded
        while unbound counts toward `still-pending` but is never attempted
        (there is nowhere to send it)."""
        pending = self._pending()
        if not pending:
            return (0, 0)
        groups = self._group(pending)
        pending_total = sum(1 for sid, _ in pending if sid is None)
        sent_total = 0
        newly_sent_ids: list[str] = []
        for shared_id, findings in groups.items():
            if await self._post(shared_id, findings):
                sent_total += len(findings)
                newly_sent_ids.extend(f.id for f in findings)
            else:
                pending_total += len(findings)
        if newly_sent_ids:
            with self.sent_path.open("a", encoding="utf-8") as fh:
                for fid in newly_sent_ids:
                    fh.write(fid + "\n")
        return (sent_total, pending_total)

    async def resync(self) -> int:
        """Re-push the entire retained log — sent or not — the recovery path
        for a service restart (amendment F Q5). Each Finding goes only to
        the Shared Session recorded alongside it (see `record()`); a Finding
        recorded while unbound has no session to resync to and is skipped
        (see `retained_count()`).

        Returns the total count of Findings successfully re-pushed, across
        every Shared Session in the backlog — the plan's documented `-> int`
        signature (Task 2 Interfaces). A prior pass changed this to
        `tuple[int, int]` to let a caller distinguish "nothing to push" from
        "push failed"; that distinction is still available, just from
        `retained_count()` compared against this return value, rather than
        baked into the return type itself — see `cli.cmd_resync`.
        """
        groups = self._group(self._all_entries())
        pushed = 0
        for shared_id, findings in groups.items():
            if await self._post(shared_id, findings):
                pushed += len(findings)
        return pushed
