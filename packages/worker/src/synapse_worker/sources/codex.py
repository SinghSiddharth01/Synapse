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
  `reasoning`, `function_call`, `function_call_output`, `custom_tool_call`,
  `custom_tool_call_output` — mapped onto AgentEvent.kind's four members.
  `custom_tool_call(_output)` is the wire shape of Codex's `apply_patch` —
  the freeform-grammar tool that is the primary file-edit path in current
  Codex (`ToolSpec::Freeform`, `codex-rs/core/src/tools/handlers/
  apply_patch_spec.rs`), not a JSON `function_call` the way `shell` is —
  and it maps onto `tool_use`/`tool_result` exactly the way `function_call`/
  `function_call_output` does: same call_id-keyed name resolution, same
  "string or content-item list" output shape. Everything else
  (`local_shell_call`, `web_search_call`, `image_generation_call`,
  `tool_search_call(_output)`, `compaction`/`context_compaction`, ...) is
  logged and skipped, same policy `ClaudeCodeSource` applies to an unknown
  content-block type.
* `compacted` — a `RolloutItem::Compacted(CompactedItem)` line, NOT a
  `response_item` payload type (`protocol.rs`'s `RolloutItem` enum has it as
  a sibling tag to `response_item`, not a variant inside it — confusingly
  adjacent to the *different*, still-skipped `response_item` payload type
  named `"compaction"` above). `payload.message` is the compaction summary
  the agent itself wrote, replacing
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

**Codex's own machine-injected context shares the wire role "user" with a
real developer turn, and this adapter must not confuse the two.** Unlike
Claude Code, where injected context is flagged `isMeta: true`
(`ClaudeCodeSource` skips those records — see its module docstring), Codex's
`response_item` `message` items carry no such marker: world-state/
environment/AGENTS.md/skill/subagent/plugin/hook bundles are built by
`build_contextual_user_message`/`merge_contextual_fragments`
(`codex-rs/core/src/context_manager/updates.rs:15-45`) as plain
`ResponseItem::Message { role: "user", ... }` and persisted via
`record_conversation_items` -> `persist_rollout_response_items`
(`codex-rs/core/src/session/mod.rs`, `build_initial_context_with_world_state`
around line 3395 and `build_turn_context_contribution_items` around line
3341) exactly like a typed prompt. A fresh session's *first* `response_item`
message is this bundle, not anything the developer wrote.

Codex itself has to solve the identical problem for its own TUI history and
does so by content-matching, not a wire flag: `event_mapping.rs`'s
`parse_user_message` returns `None` — drops the *entire* message, not just
the matching fragment — whenever
`is_contextual_user_message_content`/`is_contextual_user_fragment`
(`codex-rs/core/src/context/contextual_user_message.rs`) matches any one of
its content items against a closed table of known fragment shapes
(`CONTEXTUAL_USER_FRAGMENT_MATCHERS`). This adapter mirrors that same
predicate (`_is_contextual_user_fragment` below) against the same primary
sources, and applies it the same all-or-nothing way Codex's own
`parse_user_message` does. The matched shapes, each cited at its own
constant/helper below: `UserInstructions` ("# AGENTS.md instructions" ...
"</INSTRUCTIONS>"), `EnvironmentsState` (`<environment_context>` ...
`</environment_context>`, via `codex-rs/core/src/context/world_state/
environment.rs`'s `environment_context_markers`), `SkillInstructions`
(`<skill>`), `UserShellCommand` (`<user_shell_command>`), `TurnAborted`
(`<turn_aborted>`), `SubagentNotification` (`<subagent_notification>`),
`RecommendedPluginsInstructions` (`<recommended_plugins>`) — all seven via
the shared default `matches_text` = trim, then case-insensitive start/end
marker match (`codex-rs/context-fragments/src/fragment.rs`'s
`matches_marked_text`); plus four with their own hand-rolled `matches_text`:
`AdditionalContextUserFragment` (`<external_KEY>...</external_KEY>`,
`codex-rs/context-fragments/src/additional_context.rs`),
`InternalModelContextFragment` (`<codex_internal_context source="...">...
</codex_internal_context>`, or the legacy `<goal_context>` wrapper,
`codex-rs/core/src/context/internal_model_context.rs`), and the three
`Legacy*Warning` fragments (fixed prefix/suffix text, kept only so old
sessions still parse) — plus a hook's injected prompt fragment
(`<hook_prompt hook_run_id="...">...</hook_prompt>`,
`codex-rs/protocol/src/items.rs`'s `parse_hook_prompt_fragment`, approximated
here as a tag check rather than reimplementing its XML round-trip). This is
the same class of gap `ClaudeCodeSource`'s `isMeta` check closes for Claude
Code, solved the way Codex's own source solves it for itself, not invented
here. See `fixtures/raw_lines/codex/README.md` for the full research trail.
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
# that have a home in the contract. custom_tool_call(_output) is apply_patch's
# wire shape (a Freeform tool, not a JSON function_call) — see module
# docstring. Everything else is logged and skipped.
RESPONSE_ITEM_KINDS = {
    "message": "text",
    "reasoning": "thinking",
    "function_call": "tool_use",
    "function_call_output": "tool_result",
    "custom_tool_call": "tool_use",
    "custom_tool_call_output": "tool_result",
}

_KNOWN_ROLES = {"user", "assistant", "system"}

# ContentItem tags that carry readable text (message.content).
_TEXT_CONTENT_TYPES = {"input_text", "output_text"}

# Marker pairs for the seven ContextualUserFragment shapes that rely on the
# shared default `matches_text` (trim, then case-insensitive start/end marker
# match — `matches_marked_text` in `codex-rs/context-fragments/src/
# fragment.rs`). Each entry is (fragment name, start marker, end marker); the
# name is only for readability, never matched against.
_MARKED_INJECTED_CONTEXT_FRAGMENTS: tuple[tuple[str, str, str], ...] = (
    ("UserInstructions", "# AGENTS.md instructions", "</INSTRUCTIONS>"),
    ("EnvironmentsState", "<environment_context>", "</environment_context>"),
    ("SkillInstructions", "<skill>", "</skill>"),
    ("UserShellCommand", "<user_shell_command>", "</user_shell_command>"),
    ("TurnAborted", "<turn_aborted>", "</turn_aborted>"),
    ("SubagentNotification", "<subagent_notification>", "</subagent_notification>"),
    ("RecommendedPluginsInstructions", "<recommended_plugins>", "</recommended_plugins>"),
)

_ADDITIONAL_CONTEXT_PREFIX = "<external_"
_INTERNAL_CONTEXT_START = "<codex_internal_context"
_INTERNAL_CONTEXT_END = "</codex_internal_context>"
_LEGACY_GOAL_CONTEXT_START = "<goal_context>"
_LEGACY_GOAL_CONTEXT_END = "</goal_context>"
_SOURCE_ATTR_START = ' source="'
_SOURCE_ATTR_END = '">'

# Fixed-text legacy warning fragments (`Legacy*Warning` in
# codex-rs/core/src/context/), kept only so old rollouts still parse; Codex
# itself no longer produces them. Two are pure prefix checks, one needs a
# suffix too.
_LEGACY_WARNING_PREFIXES = (
    "Warning: The maximum number of unified exec processes you can keep open is",
    "Warning: Your account was flagged for potentially high-risk cyber activity",
)
_LEGACY_APPLY_PATCH_PREFIX = "Warning: apply_patch was requested via "
_LEGACY_APPLY_PATCH_SUFFIX = "Use the apply_patch tool instead of exec_command."


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
        self.skipped_injected_context = 0
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
            content = payload.get("content")
            if role == "user" and _is_injected_context_message(content):
                # Codex's own machine-injected context (world-state,
                # environment, AGENTS.md, skills, ...) arrives under the same
                # role="user" a real developer turn does — see the module
                # docstring's "Codex's own machine-injected context" section.
                # Mirrors Codex's own parse_user_message: the whole message
                # is dropped, not just the matching fragment.
                self.skipped_injected_context += 1
                return None
            body = _stringify_content_items(content)
        elif item_type == "reasoning":
            body = _stringify_reasoning(payload)
        elif item_type == "function_call":
            tool_name = str(payload.get("name", "")) or None
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and tool_name:
                self._tool_names[call_id] = tool_name
            body = _stringify(payload.get("arguments"))
        elif item_type == "custom_tool_call":
            # apply_patch's wire shape (ToolSpec::Freeform) — same call_id ->
            # name bookkeeping as function_call, but the argument field is
            # named "input" and is the raw grammar text, not JSON.
            tool_name = str(payload.get("name", "")) or None
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and tool_name:
                self._tool_names[call_id] = tool_name
            body = _stringify(payload.get("input"))
        elif item_type == "function_call_output":
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                tool_name = self._tool_names.get(call_id)
            body = _stringify(payload.get("output"))
        else:  # custom_tool_call_output
            # Unlike function_call_output, CustomToolCallOutput carries its
            # own optional `name` directly on the wire; the call_id map is
            # only a fallback for when that's absent.
            call_id = payload.get("call_id")
            name = payload.get("name")
            tool_name = str(name) if name else None
            if tool_name is None and isinstance(call_id, str):
                tool_name = self._tool_names.get(call_id)
            body = _stringify(payload.get("output"))

        # Neither function_call/function_call_output nor
        # custom_tool_call/custom_tool_call_output carries a role on the
        # wire — see the module docstring's "Judgment call" note.
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


def _is_injected_context_message(content: object) -> bool:
    """True when ANY text content item of a `user`-role message is one of
    Codex's own injected-context fragments (world-state/environment/
    AGENTS.md/skill/subagent/plugin/hook bundles) rather than something the
    developer typed. Mirrors Codex's own `is_contextual_user_message_content`
    (`codex-rs/core/src/event_mapping.rs`) — see the module docstring's
    "Codex's own machine-injected context" section for the full citation
    trail — including its all-or-nothing behavior: one matching fragment
    disqualifies the whole message, the same way `parse_user_message` there
    returns `None` for it rather than only dropping the matched item."""
    if not isinstance(content, list):
        return False
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in _TEXT_CONTENT_TYPES:
            continue
        text = item.get("text")
        if isinstance(text, str) and _is_contextual_user_fragment(text):
            return True
    return False


def _is_contextual_user_fragment(text: str) -> bool:
    """Port of Codex's own `is_contextual_user_fragment`
    (`codex-rs/core/src/context/contextual_user_message.rs`) against a single
    `ContentItem`'s text. See the module docstring for what each branch
    mirrors and why."""
    if not text:
        return False
    if _matches_hook_prompt_fragment(text):
        return True
    if _matches_additional_context_fragment(text):
        return True
    if _matches_internal_model_context_fragment(text):
        return True
    if _matches_legacy_warning(text):
        return True
    return any(
        _matches_marked_text(text, start, end)
        for _name, start, end in _MARKED_INJECTED_CONTEXT_FRAGMENTS
    )


def _matches_marked_text(text: str, start_marker: str, end_marker: str) -> bool:
    """`matches_marked_text` in `codex-rs/context-fragments/src/fragment.rs`:
    trim, then case-insensitive start/end marker match on the trimmed text
    (not merely "contains") — both markers must be present or neither
    matches."""
    if not start_marker or not end_marker:
        return False
    stripped = text.strip()
    return (
        stripped[: len(start_marker)].lower() == start_marker.lower()
        and stripped[len(stripped) - len(end_marker) :].lower() == end_marker.lower()
    )


def _matches_additional_context_fragment(text: str) -> bool:
    """`AdditionalContextUserFragment::matches_text`
    (`codex-rs/context-fragments/src/additional_context.rs`):
    `<external_KEY>...</external_KEY>` — hook- or extension-supplied
    out-of-band context, keyed so the close tag must echo the same key."""
    stripped = text.strip()
    if not stripped.startswith(_ADDITIONAL_CONTEXT_PREFIX):
        return False
    rest = stripped[len(_ADDITIONAL_CONTEXT_PREFIX) :]
    if ">" not in rest:
        return False
    key, _, value_and_close = rest.partition(">")
    return value_and_close.endswith(f"</external_{key}>")


def _matches_internal_model_context_fragment(text: str) -> bool:
    """`InternalModelContextFragment::matches_text`
    (`codex-rs/core/src/context/internal_model_context.rs`): the current
    `<codex_internal_context source="...">...</codex_internal_context>` shape
    (source restricted to `[a-z][a-z0-9_]*`, matching the Rust source's own
    validator), or its legacy `<goal_context>...</goal_context>` predecessor."""
    trimmed = text.strip()
    if trimmed.startswith(_LEGACY_GOAL_CONTEXT_START) and trimmed.endswith(_LEGACY_GOAL_CONTEXT_END):
        return True
    if not trimmed.startswith(_INTERNAL_CONTEXT_START):
        return False
    rest = trimmed[len(_INTERNAL_CONTEXT_START) :]
    if not rest.startswith(_SOURCE_ATTR_START):
        return False
    rest = rest[len(_SOURCE_ATTR_START) :]
    if _SOURCE_ATTR_END not in rest:
        return False
    source, _, body_and_close = rest.partition(_SOURCE_ATTR_END)
    return _is_valid_internal_context_source(source) and body_and_close.endswith(_INTERNAL_CONTEXT_END)


def _is_valid_internal_context_source(source: str) -> bool:
    if not source:
        return False
    first, rest = source[0], source[1:]
    if not ("a" <= first <= "z"):
        return False
    return all(("a" <= ch <= "z") or ("0" <= ch <= "9") or ch == "_" for ch in rest)


def _matches_legacy_warning(text: str) -> bool:
    """The three `Legacy*Warning` fragments — fixed text Codex no longer
    produces but that old rollouts may still carry
    (`codex-rs/core/src/context/legacy_*_warning.rs`)."""
    trimmed = text.strip()
    if trimmed.startswith(_LEGACY_APPLY_PATCH_PREFIX) and trimmed.endswith(_LEGACY_APPLY_PATCH_SUFFIX):
        return True
    return any(trimmed.startswith(prefix) for prefix in _LEGACY_WARNING_PREFIXES)


def _matches_hook_prompt_fragment(text: str) -> bool:
    """A hook's injected prompt fragment,
    `<hook_prompt hook_run_id="...">...</hook_prompt>`
    (`codex-rs/protocol/src/items.rs`'s `parse_hook_prompt_fragment`, which
    round-trips through real XML parsing). Approximated here as a tag check
    rather than reimplementing that XML round-trip — precise enough to catch
    the shape without pulling in an XML parser for one narrow marker."""
    trimmed = text.strip()
    if not trimmed.startswith("<hook_prompt ") or not trimmed.endswith("</hook_prompt>"):
        return False
    opening_tag = trimmed.split(">", 1)[0]
    return 'hook_run_id="' in opening_tag


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
