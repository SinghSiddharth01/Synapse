"""Write-ahead durable log + the sole egress to the Synapse Service (Plan D.4).

Deliberately mirrors synapse_worker.producer's file discipline rather than
importing it — the packages share contracts only, and the two logs guard
different hops. Same posture: findings.jsonl append-only, sent.jsonl marks
delivery, unsent = the difference, RETAINED after ack so `resync` can answer
a service restart (amendment F Q5). Replay is safe because ingest upserts by
Finding.id (first-write-wins, E3 Task 1)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from synapse_contracts import Finding

logger = logging.getLogger(__name__)


class Relay:
    def __init__(self, state_dir: Path, service_url: str, shared_id: str, *,
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

    # ── write-ahead ─────────────────────────────────────────────────────────
    def record(self, findings: list[Finding]) -> None:
        with self.findings_path.open("a", encoding="utf-8") as fh:
            for f in findings:
                fh.write(f.model_dump_json() + "\n")

    def _load(self, path: Path) -> list[str]:
        if not path.is_file():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _all_findings(self) -> list[Finding]:
        out = []
        for line in self._load(self.findings_path):
            try:
                out.append(Finding.model_validate_json(line))
            except ValueError as exc:
                logger.warning("Skipping corrupt relay line (%s)", exc)
        return out

    def _sent_ids(self) -> set[str]:
        return set(self._load(self.sent_path))

    def _pending(self) -> list[Finding]:
        sent = self._sent_ids()
        return [f for f in self._all_findings() if f.id not in sent]

    def pending_count(self) -> int:
        return len(self._pending())

    # ── egress ──────────────────────────────────────────────────────────────
    async def _post(self, findings: list[Finding]) -> bool:
        payload = {"findings": [f.model_dump(mode="json") for f in findings]}
        url = f"{self.service_url}/v1/sessions/{self.shared_id}/findings"
        try:
            async with httpx.AsyncClient(transport=self._transport,
                                         timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            return True
        except (httpx.HTTPError, OSError) as exc:
            logger.info("Service unavailable (%s); %d findings stay queued",
                        exc.__class__.__name__, len(findings))
            return False

    async def flush(self) -> tuple[int, int]:
        pending = self._pending()
        if not pending:
            return (0, 0)
        if not await self._post(pending):
            return (0, len(pending))
        with self.sent_path.open("a", encoding="utf-8") as fh:
            for f in pending:
                fh.write(f.id + "\n")
        return (len(pending), 0)

    async def resync(self) -> int:
        """Re-push the entire retained log. The recovery path for a service restart."""
        everything = self._all_findings()
        if everything and await self._post(everything):
            return len(everything)
        return 0
