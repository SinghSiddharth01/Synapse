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


class FindingSink(ABC):
    """Where condensed findings go. The orchestrator, in production."""

    @abstractmethod
    async def send(self, findings: list[Finding]) -> bool:
        """True if delivered. False is normal — the caller retries later."""


class HttpSink(FindingSink):
    """POSTs to the orchestrator's producer endpoint."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout

    async def send(self, findings: list[Finding]) -> bool:
        payload = {"findings": [f.model_dump(mode="json") for f in findings]}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
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

    def __init__(self, log_dir: Path, sink: FindingSink) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.findings_path = self.log_dir / "findings.jsonl"
        self.sent_path = self.log_dir / "sent.jsonl"
        self.sink = sink

    def record(self, findings: list[Finding]) -> None:
        """Persist before any send is attempted. Call this first, always."""
        if not findings:
            return
        produced_at = datetime.now(timezone.utc).isoformat()
        with self.findings_path.open("a", encoding="utf-8") as handle:
            for finding in findings:
                handle.write(
                    json.dumps(
                        {"produced_at": produced_at, "finding": finding.model_dump(mode="json")}
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

    def unsent(self) -> list[Finding]:
        """Everything recorded but not yet confirmed delivered, oldest first."""
        if not self.findings_path.is_file():
            return []
        sent = self._sent_ids()
        pending: list[Finding] = []
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
            if finding.id not in sent:
                pending.append(finding)
        return pending

    def _mark_sent(self, findings: list[Finding]) -> None:
        with self.sent_path.open("a", encoding="utf-8") as handle:
            for finding in findings:
                handle.write(f"{finding.id}\n")

    async def flush(self) -> tuple[int, int]:
        """Try to deliver everything outstanding. Returns (sent, still_pending).

        Replays across restarts for free: `unsent` is derived from the log, so a
        worker that crashed mid-send picks up exactly where it left off.
        """
        pending = self.unsent()
        if not pending:
            return 0, 0
        if await self.sink.send(pending):
            self._mark_sent(pending)
            return len(pending), 0
        return 0, len(pending)
