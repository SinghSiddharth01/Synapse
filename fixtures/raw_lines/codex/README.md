# Codex CLI on-disk session format — research trail

Recorded 2026-08-05, Chain A (`docs/plans/exec/2026-08-05-e7-demo-window.md`).
No live `codex` install was available on the machine that built this adapter
(`which codex` → not found, `~/.codex` does not exist), so this is confirmed
from primary source — the actual Rust structs and their `Serialize`/
`Deserialize` impls, plus literal JSON the source tree's own tests write and
read back — not from a running transcript the way `ClaudeCodeSource` was.
That is a real, lower confidence than the Claude Code adapter's, not a
formality; it is why every claim below is a link, not a paraphrase.

## What was checked

`git clone --depth 1 https://github.com/openai/codex.git`, HEAD
`fa5d5ae047d1891a2f816c22d9ed926a0728ba47`, 2026-08-05. `gh release list`
confirms `0.146.1` was tagged **"Latest"** ~2 hours before this clone and
`0.147.0-alpha.*` prereleases were landing same-day — main is what a fresh
install gets right now, not a preview of something months out.

## Path layout

    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<YYYY-MM-DDThh-mm-ss>-<uuid>.jsonl

Confirmed by [`codex-rs/rollout/src/lib.rs`](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/rollout/src/lib.rs)
(`SESSIONS_SUBDIR = "sessions"`), the day-dir join in
[`codex-rs/rollout/src/recorder.rs:1553`](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/rollout/src/recorder.rs#L1553)
("Resolve ~/.codex/sessions/YYYY/MM/DD path"), and the filename parser
[`parse_timestamp_uuid_from_filename`](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/rollout/src/list.rs#L967-L983),
which is also where the UUID-from-filename extraction this adapter's
`discovery.find_codex_transcripts` does was copied from — Codex's own list
view derives session identity from the filename the same way, not by
opening the file. A web search corroborates the path independently:
[PixelPaw-Labs/codex-trace](https://github.com/PixelPaw-Labs/codex-trace) (a
third-party session viewer) and
[Inventive HQ's config-location page](https://inventivehq.com/knowledge-base/openai/where-configuration-files-are-stored)
both describe the same `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` tree.

**Unlike Claude Code, this is not partitioned by working directory.** Claude
Code's `<slug>` directory encodes the cwd; Codex's day-tree does not — every
project's sessions land in the same `YYYY/MM/DD` folder, distinguished only
by the `cwd` field inside each file's `session_meta` line. `discovery.
find_codex_transcripts` does not open files to filter by cwd (documented
limitation in its own docstring); this is the one place detection is
intentionally weaker for Codex than for Claude Code, not an oversight.

## Line format

One JSON object per line, an **adjacently-tagged envelope**
(`RolloutLine`, [`codex-rs/protocol/src/protocol.rs:3401`](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/protocol.rs#L3401)
flattening a `#[serde(tag = "type", content = "payload")]` `RolloutItem`,
[line 3207](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/protocol.rs#L3207)):

```json
{"timestamp": "<rfc3339>", "ordinal": 1, "type": "<tag>", "payload": {...}}
```

`ordinal` is sometimes absent (`#[serde(default, skip_serializing_if =
"Option::is_none")]`). `type` is one of: `session_meta`, `response_item`,
`event_msg`, `turn_context`, `world_state`, `compacted`,
`inter_agent_communication`, `inter_agent_communication_metadata`. The
literal shape was cross-checked against a test helper that writes real
files byte-for-byte —
[`write_session_file` in `recorder_tests.rs`](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/rollout/src/recorder_tests.rs#L108-L134) —
not only against the struct definitions, which is why the two fixtures
below use the same shape that test does.

### `session_meta` — once, near the top of the file

`payload` is `SessionMeta` flattened plus an optional nested `git` object
(`SessionMetaLine`, [protocol.rs:3169](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/protocol.rs#L3169)).
Relevant fields: `session_id`, `id`, `cwd`, `originator`, `cli_version`,
`source`, and `git.branch`. **`session_id` is preferred over `id`** because
Codex's own custom `Deserialize` for `SessionMetaLine`
([protocol.rs:3176-3203](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/protocol.rs#L3176-L3203))
back-fills `session_id` from `id` whenever the key is missing — i.e. the
source's own precedence treats `id` (`ThreadId`) as authoritative and
`session_id` as a derived alias kept for older readers. Reading
`session_id or id` mirrors that back-fill.

Unlike Claude Code, where `cwd`/`gitBranch` repeat on every record,
`session_meta` carries them **once**. `CodexSource` is therefore stateful
across lines for `cwd`/`git_branch` the same way it already has to be
(mirroring `ClaudeCodeSource`) for tool-name resolution.

### `response_item` — the actual model-visible conversation

`payload` is itself tagged (`ResponseItem`,
[models.rs:806-1052](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/models.rs#L806-L1052),
`#[serde(tag = "type", rename_all = "snake_case")]`). This adapter handles
four variants, chosen to mirror the four `AgentEvent.kind` values Claude
Code's adapter already produces:

| `payload.type` | `AgentEvent.kind` | notes |
|---|---|---|
| `message` | `text` | `payload.role` is `user`/`assistant`(/`system`); `payload.content` is a list of `ContentItem`, tagged `input_text`/`output_text`/`input_image`/`input_audio` ([models.rs:713-731](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/models.rs#L713-L731)) — only the two text variants carry readable text |
| `reasoning` | `thinking` | `payload.content` (`ReasoningItemContent`: `reasoning_text`/`text`) or `payload.summary` (`ReasoningItemReasoningSummary`: `summary_text`) — both variants carry a `text` field regardless of tag ([models.rs:1716-1725](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/models.rs#L1716-L1725)); `content` is often absent (`encrypted_content` only) when the provider hides reasoning, so `summary` is the realistic fallback |
| `function_call` | `tool_use` | `payload.name`, `payload.arguments` (a **JSON-encoded string**, not a parsed object — the Responses API convention, noted directly in the struct's own comment), `payload.call_id` |
| `function_call_output` | `tool_result` | `payload.call_id`, `payload.output` — encoded as **either a plain string or an array of content items**, confirmed by `FunctionCallOutputPayload`'s hand-written `Serialize`/`Deserialize` ([models.rs:1999-2023](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/models.rs#L1999-L2023)) and its own round-trip tests ([models.rs:3247-3300](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/protocol/src/models.rs#L3247-L3300)) — the exact same "string or block list" shape Claude Code's `tool_result.content` already has |

Every other `response_item` variant (`local_shell_call`,
`custom_tool_call(_output)`, `tool_search_call/output`, `web_search_call`,
`image_generation_call`, `compaction`, ...) is logged and skipped — same
policy `ClaudeCodeSource` already applies to an unknown content-block type.
Codex's format is under active, fast-moving development (a community
session-log viewer, [PixelPaw-Labs/codex-trace](https://github.com/PixelPaw-Labs/codex-trace),
documents "new (≥0.44), mid, and oldest (2025/08)" session-metadata shapes
coexisting across installed CLI versions — corroborated first-hand by the
`SessionMetaLine` back-fill above, which exists specifically to keep reading
older files); skip-and-log is not a hedge, it is the only sustainable
posture against a format that keeps changing shape.

**`tool_use`/`tool_result` role convention is this adapter's judgment call,
not something the wire format states.** Unlike Claude Code (where
`tool_result` literally arrives under top-level `type: "user"`), neither
`function_call` nor `function_call_output` carries a `role` field at all —
there is no wire signal to read. This adapter stamps `function_call` as the
assistant's turn and `function_call_output` as the user's, mirroring
`ClaudeCodeSource`'s resolved convention exactly so the two adapters agree
on what "assistant did something, the environment answered" means. Recorded
as a deviation, not asserted as confirmed fact.

### Deliberately not modelled: `event_msg`

`event_msg` (`UserMessageEvent`/`AgentMessageEvent`/...) is also persisted
to the same rollout file, alongside `response_item`, for Codex's own TUI
history — confirmed by
[`Session::record_conversation_items`](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/core/src/session/mod.rs#L2968-L2990)
(writes `response_item`s, the replay-authoritative path — see
`reconstruct_history_from_rollout`,
[rollout_reconstruction.rs](https://github.com/openai/codex/blob/fa5d5ae047d1891a2f816c22d9ed926a0728ba47/codex-rs/core/src/session/rollout_reconstruction.rs),
which rebuilds model history from `response_item` lines and only reads
`EventMsg::UserMessage` to mark turn boundaries, never as content). A real
rollout file therefore likely carries **both** a `response_item` line and an
`event_msg` line for the same user turn. Parsing both would double every
turn; this adapter parses only `response_item` and skips `event_msg` as
bookkeeping, on the same reasoning Claude Code's adapter already skips its
own UI-only record types.

## Residual risk

This is source-code-and-test-literal confirmed, not transcript-confirmed —
the one meaningful gap next to `ClaudeCodeSource`'s live-transcript
provenance. If a real `~/.codex/sessions/*.jsonl` file becomes available
before the demo, re-verify this adapter against it and update this file
(and `ClaudeCodeSource`'s docstring precedent: "format confirmed against a
live transcript") the same way.

## Fixtures in this directory

| File | What it exercises |
|---|---|
| `basic_turn.jsonl` | `session_meta` + a user `message` + an assistant `message` — the plain-text case, Codex's counterpart to Claude Code's `test_parses_assistant_text` |
| `tool_call_and_result.jsonl` | `session_meta` + `function_call` + `function_call_output` sharing one `call_id` — the tool-name-resolution case |
| `malformed.jsonl` | a valid `session_meta` line, one line of garbage JSON, then a valid `response_item` line — proves a torn/bad line doesn't take the rest of the file down with it |

Hand-authored to the shapes above, not captured from a real run (see
"Residual risk").
