"""Codex CLI rollout adapter: raw JSONL line -> AgentEvent[].

Format confirmed 2026-08-05 from primary source — github.com/openai/codex
(`codex-rs/rollout`, `codex-rs/protocol/src/protocol.rs`,
`codex-rs/protocol/src/models.rs`), HEAD `fa5d5ae047d1891a2f816c22d9ed926a0728ba47`,
with `0.146.1` tagged "Latest" ~2h before that clone. No Codex install was
available on this machine to verify against a *running* transcript (`which
codex` -> not found), so this reads the struct definitions and the source
tree's own test fixtures (which write real files byte-for-byte), not a live
session — a materially lower-confidence provenance than `ClaudeCodeSource`'s
"confirmed against a live 2.9 MB transcript". See
`fixtures/raw_lines/codex/README.md` for the full trail with links; that gap
is recorded there deliberately, not glossed over.

    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<uuid>.jsonl

Each line is one adjacently-tagged `RolloutLine`:

    {"timestamp": <rfc3339>, "ordinal": <int, sometimes omitted>,
     "type": <tag>, "payload": {...}}

`type` is one of: session_meta, response_item, event_msg, turn_context,
world_state, compacted, inter_agent_communication(_metadata). Three are
handled here:

* `session_meta` — once, near the top of the file. `payload.cwd` and
  `payload.git.branch` are NOT repeated on every subsequent line the way
  Claude Code repeats `cwd`/`gitBranch` on every record, so this adapter is
  stateful across lines for them the same way it already has to be for
  tool-name resolution: whatever `session_meta` last said is what every
  later event carries. `payload.session_id` is preferred over `payload.id`
  because Codex's own `SessionMetaLine` deserializer back-fills `session_id`
  from `id` whenever it's missing — this mirrors that precedence rather than
  inventing a new one.
* `response_item` — the actual model-visible conversation (Responses-API
  shaped): `payload` is itself tagged, and this adapter handles `message`,
  `reasoning`, `function_call`, `function_call_output` — the four that map
  onto AgentEvent.kind's four members. Everything else (`local_shell_call`,
  `custom_tool_call`, `web_search_call`, `image_generation_call`, ...) is
  logged and skipped, same policy `ClaudeCodeSource` applies to an unknown
  content-block type.
* `compacted` — a `RolloutItem::Compacted(CompactedItem)` line, NOT a
  `response_item` payload type (`protocol.rs`'s `RolloutItem` enum has it as
  a sibling tag to `response_item`, not a variant inside it). `payload.
  message` is the compaction summary the agent itself wrote, replacing
  everything compacted away. Plan A.3 is
  explicit that this is ingested, not dropped: "Compaction summaries the
  agent writes are ingested as events — they are high-density signal, not
  noise." Codex's own `impl From<CompactedItem> for ResponseItem`
  (`protocol.rs`) treats the summary as assistant output — `role: "assistant"`,
  wrapped in `ContentItem::OutputText` — for exactly this reason (it is what
  gets spliced back into the model's own history in place of what was
  compacted); this adapter mirrors that and stamps it `kind="text"`,
  `role="assistant"`. That `From` impl is in-memory history reconstruction
  only, never itself persisted as a second `response_item` line — the
  `compacted` line is the *only* place this text exists on disk, which is
  why skipping it (the adapter's original behavior) was a real data loss,
  not a bookkeeping skip.

`event_msg` is deliberately NOT parsed even though it is real content
(`UserMessageEvent`/`AgentMessageEvent`): Codex persists it alongside
`response_item` for its own TUI history, duplicating the same turn.
Parsing both would double every turn; `response_item` is the
replay-authoritative one (Codex's own `reconstruct_history_from_rollout`
rebuilds model history from it, not from `event_msg`), so `event_msg` is
bookkeeping here the same way Claude Code's own UI-only record types are.

**Judgment call, not a wire fact:** neither `function_call` nor
`function_call_output` carries a `role` field — there is nothing on the wire
to read. This adapter stamps a call as the assistant's turn and its result
as the user's, mirroring `ClaudeCodeSource`'s resolved convention exactly
(where `tool_result` really does arrive under `type: "user"`) so the two
adapters agree on what "assistant acted, the environment answered" means.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from synapse_contracts import AgentEvent

logger = logging.getLogger(__name__)

AGENT_NAME = "codex"

SESSION_META_TYPE = "session_meta"
RESPONSE_ITEM_TYPE = "response_item"
COMPACTED_TYPE = "compacted"

# response_item payload "type" values mapped onto AgentEvent.kind — the four
# that have a home in the contract. Everything else is logged and skipped.
RESPONSE_ITEM_KINDS = {
    "message": "text",
    "reasoning": "thinking",
    "function_call": "tool_use",
    "function_call_output": "tool_result",
}

_KNOWN_ROLES = {"user", "assistant", "system"}

# ContentItem tags that carry readable text (message.content).
_TEXT_CONTENT_TYPES = {"input_text", "output_text"}


class CodexSource:
    """Stateful across lines for two reasons, both shared with `ClaudeCodeSource`:

    `session_meta` carries `cwd`/`git.branch` exactly once, near the top of
    the file, rather than on every record, so those are remembered and
    stamped onto every event that follows. `function_call_output` carries
    only `call_id`, not the tool's name, so the name is resolved from a map
    of previously-seen `function_call` ids — the same shape as Claude Code's
    `tool_use_id -> name` map.
    """

    agent = AGENT_NAME

    def __init__(self) -> None:
        self._tool_names: dict[str, str] = {}
        self._session_id: str = ""
        self._cwd: str | None = None
        self._git_branch: str | None = None
        self.skipped_bookkeeping = 0
        self.skipped_unknown_response_item = 0
        self.malformed_lines = 0

    def parse_line(self, line: str) -> list[AgentEvent]:
        """One JSONL line -> zero or one AgentEvent. Never raises.

        Unlike Claude Code, where one line can carry several content blocks,
        a Codex `response_item` line wraps exactly one item — so this never
        returns more than one event.
        """
        line = line.strip()
        if not line:
            return []

        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            # A torn write or a line caught mid-append — the follower only
            # hands over complete lines, so this is rare but not impossible.
            self.malformed_lines += 1
            logger.debug("Skipping unparseable rollout line")
            return []

        if not isinstance(record, dict):
            self.malformed_lines += 1
            return []

        line_type = record.get("type")
        payload = record.get("payload")

        if line_type == SESSION_META_TYPE:
            if isinstance(payload, dict):
                self._ingest_session_meta(payload)
            return []

        if line_type == COMPACTED_TYPE:
            if not isinstance(payload, dict):
                return []
            ts = _parse_ts(record.get("timestamp"))
            event = self._compacted_to_event(payload, ts)
            return [event] if event is not None else []

        if line_type != RESPONSE_ITEM_TYPE:
            self.skipped_bookkeeping += 1
            return []

        if not isinstance(payload, dict):
            return []

        ts = _parse_ts(record.get("timestamp"))
        event = self._response_item_to_event(payload, ts)
        return [event] if event is not None else []

    def _ingest_session_meta(self, payload: dict[str, Any]) -> None:
        session_id = payload.get("session_id") or payload.get("id")
        if session_id:
            self._session_id = str(session_id)
        cwd = payload.get("cwd")
        if cwd is not None:
            self._cwd = str(cwd)
        git = payload.get("git")
        if isinstance(git, dict):
            branch = git.get("branch")
            if branch is not None:
                self._git_branch = str(branch)

    def _compacted_to_event(self, payload: dict[str, Any], ts: datetime) -> AgentEvent | None:
        """`RolloutItem::Compacted` — the compaction summary the agent wrote,
        the only place that text is persisted (see module docstring). Plan
        A.3: high-density signal, ingested like any other event, not
        bookkeeping."""
        body = str(payload.get("message") or "")
        if not body.strip():
            return None
        return AgentEvent(
            role="assistant",  # type: ignore[arg-type]
            kind="text",  # type: ignore[arg-type]
            content=body,
            tool_name=None,
            ts=ts,
            agent_session_id=self._session_id,
            cwd=self._cwd,
            git_branch=self._git_branch,
        )

    def _response_item_to_event(self, payload: dict[str, Any], ts: datetime) -> AgentEvent | None:
        item_type = payload.get("type")
        kind = RESPONSE_ITEM_KINDS.get(str(item_type))
        if kind is None:
            self.skipped_unknown_response_item += 1
            logger.debug("Skipping unhandled response_item type %r", item_type)
            return None

        role = "assistant"
        tool_name: str | None = None

        if item_type == "message":
            role = str(payload.get("role", ""))
            if role not in _KNOWN_ROLES:
                # e.g. "developer" — a Responses API role AgentEvent has no
                # slot for. Logged as an unknown variant, not raised.
                self.skipped_unknown_response_item += 1
                return None
            body = _stringify_content_items(payload.get("content"))
        elif item_type == "reasoning":
            body = _stringify_reasoning(payload)
        elif item_type == "function_call":
            tool_name = str(payload.get("name", "")) or None
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and tool_name:
                self._tool_names[call_id] = tool_name
            body = _stringify(payload.get("arguments"))
        else:  # function_call_output
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                tool_name = self._tool_names.get(call_id)
            body = _stringify(payload.get("output"))

        # Neither function_call nor function_call_output carries a role on
        # the wire — see the module docstring's "Judgment call" note.
        if kind == "tool_use":
            role = "assistant"
        elif kind == "tool_result":
            role = "user"

        if not body.strip():
            return None

        return AgentEvent(
            role=role,  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            content=body,
            tool_name=tool_name,
            ts=ts,
            agent_session_id=self._session_id,
            cwd=self._cwd,
            git_branch=self._git_branch,
        )


def _parse_ts(raw: object) -> datetime:
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _stringify_content_items(value: object) -> str:
    """`message.content`: a list of typed `ContentItem` blocks (`input_text`
    / `output_text` / `input_image` / `input_audio`). Only the text variants
    carry anything a distiller can read; image/audio blocks are noted by
    type rather than silently dropped."""
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in _TEXT_CONTENT_TYPES:
            parts.append(str(item.get("text", "")))
        elif item_type in ("input_image", "input_audio"):
            parts.append(f"[{item_type}]")
    return "\n".join(p for p in parts if p)


def _stringify_reasoning(payload: dict[str, Any]) -> str:
    """`reasoning.content` (full chain-of-thought) is preferred; it is often
    absent — `encrypted_content` only — when the provider hides reasoning,
    so `summary` (the provider-authored short version) is the realistic
    fallback. Both `ReasoningItemContent` and `ReasoningItemReasoningSummary`
    variants carry a `text` field regardless of their own tag, so this reads
    `text` directly rather than branching on it."""
    content = payload.get("content")
    if isinstance(content, list):
        text = "\n".join(
            str(item["text"]) for item in content if isinstance(item, dict) and item.get("text")
        )
        if text:
            return text
    summary = payload.get("summary")
    if isinstance(summary, list):
        return "\n".join(
            str(item["text"]) for item in summary if isinstance(item, dict) and item.get("text")
        )
    return ""


def _stringify(value: object) -> str:
    """Flatten a payload value to text.

    `function_call.arguments` is a JSON-encoded *string* (the Responses API
    convention — the model returns arguments as a string containing JSON,
    not an already-parsed object), so this parses it back before flattening,
    the same way Claude Code flattens `tool_use.input`. `function_call_
    output.output` is either a plain string or a list of content items
    (confirmed via `FunctionCallOutputPayload`'s hand-written Serialize) —
    the same "string or block list" shape Claude Code's `tool_result.
    content` already has.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        parsed = _try_parse_json_object(value)
        if parsed is not None:
            return _stringify(parsed)
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
                elif item.get("type") == "encrypted_content":
                    parts.append("[encrypted_content]")
                else:
                    parts.append(str(item.get("image_url") or ""))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_stringify(v)}" for k, v in value.items())
    return str(value)


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
