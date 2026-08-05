"""StatsBuffer — the in-memory feed the worker's /debug page polls.

Bounded ring buffers only; nothing here holds raw transcript content — that
boundary belongs to the distiller, not the dashboard. Every value here is
already JSON-safe (str/int/bool/dict/list) so `snapshot()` can go straight to
`json.dumps` with no serialization step in the HTTP handler.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Any

from synapse_providers import CallLog

MAX_EVENTS = 200
MAX_TICKS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StatsBuffer:
    """Collects everything the debug page shows: the tagged feed, the tick
    history, the in-flight distil, and the LLM call log it was handed."""

    def __init__(self, llm: CallLog) -> None:
        self.llm = llm
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._ticks: deque[dict[str, Any]] = deque(maxlen=MAX_TICKS)
        self.current: dict[str, Any] | None = None

    def event(self, tag: str, summary: str, **detail: Any) -> None:
        self._events.append(
            {
                "ts_iso": _now_iso(),
                "tag": tag,
                "summary": summary,
                "detail": detail,
            }
        )

    def tick(self, result: dict[str, Any]) -> None:
        self._ticks.append({"ts_iso": _now_iso(), "result": result})

    def distil_started(self, segment_id: str, events: int) -> None:
        self.current = {
            "segment_id": segment_id,
            "started_iso": _now_iso(),
            "events": events,
        }

    def distil_finished(self) -> None:
        self.current = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "now": _now_iso(),
            "current": self.current,
            "ticks": list(self._ticks),
            "events": list(self._events),
            "llm": self.llm.snapshot(),
        }
