"""Claude Code transcript adapter: raw JSONL line -> AgentEvent[].

Format confirmed 2026-08-04 against a live 2.9 MB transcript (1199 lines, zero
unparseable) at:

    ~/.claude/projects/<slug>/<session-uuid>.jsonl

where <slug> is the working directory with separators replaced, e.g.
C:\\QC_multiverse_hackathon\\Synapse -> C--QC-multiverse-hackathon-Synapse

What that transcript actually contains, and why this adapter is shaped the way
it is:

* Only `type` in {"user", "assistant"} carries a `message`. The rest is Claude
  Code's own bookkeeping — mode, permission-mode, file-history-snapshot,
  file-history-delta, attachment, system, last-prompt, ai-title,
  queue-operation — and is skipped. In the sample that is 437 of 1199 lines, so
  skipping is the common path, not an edge case.
* `message.content` is EITHER a plain string OR a list of typed blocks. Both
  occur (43 string, 719 block-bearing). Handling only the list shape silently
  drops user turns.
* `tool_result` blocks arrive under `type: "user"`, not "assistant" — Claude
  Code records the tool's reply as the user's turn. AgentEvent.role therefore
  says "user" for them, which is correct and matches the fixtures.
* `tool_result` carries `tool_use_id` but not the tool's name, so the name is
  resolved from a map of previously-seen `tool_use` blocks.

Unknown block types are logged and skipped, never raised: a transcript is
written by someone else's process and its format will change without notice.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from synapse_contracts import AgentEvent

logger = logging.getLogger(__name__)

AGENT_NAME = "claude-code"

# Line types that carry a conversation turn. Everything else is bookkeeping.
MESSAGE_TYPES = frozenset({"user", "assistant"})

# Block types we map onto AgentEvent.kind, which has exactly these four members.
BLOCK_KINDS = {
    "text": "text",
    "thinking": "thinking",
    "tool_use": "tool_use",
    "tool_result": "tool_result",
}


class ClaudeCodeSource:
    """Stateful across lines, because tool_result must look up its tool_use.

    One instance per followed transcript. The tool-name map grows with the
    session; at a few hundred tool calls that is negligible, and dropping it
    would lose the tool name on every result.
    """

    agent = AGENT_NAME

    def __init__(self) -> None:
        self._tool_names: dict[str, str] = {}
        self.skipped_bookkeeping = 0
        self.skipped_sidechain = 0
        self.skipped_meta = 0
        self.skipped_unknown_block = 0
        self.malformed_lines = 0

    def parse_line(self, line: str) -> list[AgentEvent]:
        """One JSONL line -> zero or more AgentEvents. Never raises."""
        line = line.strip()
        if not line:
            return []

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A torn write or a line we caught mid-append. The follower only
            # hands over complete lines, so this is rare but not impossible.
            self.malformed_lines += 1
            logger.debug("Skipping unparseable transcript line")
            return []

        if not isinstance(record, dict):
            self.malformed_lines += 1
            return []

        if record.get("type") not in MESSAGE_TYPES:
            self.skipped_bookkeeping += 1
            return []

        # Sidechains are subagent conversations. They are real agent work, but
        # they belong to a different conversation than the one being followed,
        # and mixing them would attribute a subagent's findings to the parent
        # Agent Session. Excluded deliberately, counted so the cost is visible.
        if record.get("isSidechain"):
            self.skipped_sidechain += 1
            return []

        # Claude Code's own injected context, not something the developer or the
        # agent said.
        if record.get("isMeta"):
            self.skipped_meta += 1
            return []

        message = record.get("message")
        if not isinstance(message, dict):
            return []

        role = record["type"]  # "user" | "assistant"
        ts = _parse_ts(record.get("timestamp"))
        session_id = str(record.get("sessionId") or record.get("session_id") or "")
        cwd = record.get("cwd")
        git_branch = record.get("gitBranch")

        content = message.get("content")
        blocks: list[dict[str, Any]]
        if isinstance(content, str):
            blocks = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
        else:
            return []

        events: list[AgentEvent] = []
        for block in blocks:
            event = self._block_to_event(
                block,
                role=role,
                ts=ts,
                session_id=session_id,
                cwd=cwd,
                git_branch=git_branch,
            )
            if event is not None:
                events.append(event)
        return events

    def _block_to_event(
        self,
        block: dict[str, Any],
        *,
        role: str,
        ts: datetime,
        session_id: str,
        cwd: str | None,
        git_branch: str | None,
    ) -> AgentEvent | None:
        block_type = block.get("type")
        kind = BLOCK_KINDS.get(str(block_type))
        if kind is None:
            self.skipped_unknown_block += 1
            logger.debug("Skipping unknown content block type %r", block_type)
            return None

        tool_name: str | None = None

        if kind == "text":
            body = str(block.get("text", ""))
        elif kind == "thinking":
            body = str(block.get("thinking", ""))
        elif kind == "tool_use":
            tool_name = str(block.get("name", "")) or None
            use_id = block.get("id")
            if isinstance(use_id, str) and tool_name:
                self._tool_names[use_id] = tool_name
            body = _stringify(block.get("input"))
        else:  # tool_result
            use_id = block.get("tool_use_id")
            if isinstance(use_id, str):
                tool_name = self._tool_names.get(use_id)
            body = _stringify(block.get("content"))

        if not body.strip():
            return None

        return AgentEvent(
            role=role,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            content=body,
            tool_name=tool_name,
            ts=ts,
            agent_session_id=session_id,
            cwd=cwd,
            git_branch=git_branch,
        )


def _parse_ts(raw: object) -> datetime:
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _stringify(value: object) -> str:
    """Flatten a block payload to text.

    tool_use.input is a dict of arguments; tool_result.content is a string or a
    list of blocks. Both need to become the plain text a distiller can read.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        # Tool arguments read better as "key: value" lines than as raw JSON,
        # and the distiller is told not to quote them either way.
        return "\n".join(f"{k}: {_stringify(v)}" for k, v in value.items())
    return str(value)
