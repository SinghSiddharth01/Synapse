# Session decoupling — the runtime layer must not know about sessions

Decision (2026-08-06, from Sidd's review of the install/startup rework):
`synapse up`/`down` and `synapse-server up` are INFRASTRUCTURE commands.
Sessions are a separate artifact that comes and goes, owned entirely by the
MCP tools an agent calls. Nothing at process start/stop time may know
anything about sessions.

## The layer rule

| layer | commands | knows about |
|---|---|---|
| install | install.sh / install.ps1 | binaries only |
| configure | synapse configure / config set | service URL, contributor, distiller |
| runtime | synapse up / down, synapse-server up | processes and ports only |
| session | MCP tools (create/join/leave/end) | Shared Sessions, bindings |

## 1. `synapse up` / `synapse down` (packages/cli/src/synapse_cli/up.py)

- DELETE `_bootstrap_session` and the `--shared-id` / `--purpose` flags.
  `up` = ping service.url, start model seam (per configured arm), start
  orchestrator, start worker. No POST /v1/sessions, no member registration,
  no `record_session`, and NO machine-scoped binding write — that binding is
  what made every conversation silently "already in a session".
- Banner: point the user at their agent — "connect Claude Code, then use
  create_session / join_session from there".
- NEW `synapse down`: stops what `up` started from any terminal. `up` writes
  pidfiles under `<state>/run/`; `down` terminates those pids (worker →
  orchestrator → seam order), falls back to killing the LISTEN owner of
  :8787 (and :18181 only when the pidfile says we launched geniex).
- Worker with no binding: it must NOT push under the `local-dev` fallback id.
  Each tick, skip transcripts whose agent session has no binding (W2
  per-session bindings are already on disk); a note in the tick summary, not
  an error. Findings start flowing the moment the agent calls
  create/join_session — no worker restart needed.
- `synapse health` grows a "sessions" line: list `bindings/<agent>/*.json`
  (which conversation → which sh-id), replacing the old single-binding check.

## 2. MCP session tools (packages/orchestrator/src/synapse_orchestrator/server.py)

- `create_session`: already always creates (never adopts). ADD to the return:
  the dashboard URL to share — `<service.url>/debug` (per-session anchor if
  debug.py supports it; add one if cheap). RECORD the creating
  agent_session_id: locally via `_remember`, and pass
  `created_by_agent_session` to the service so end-time checks survive a
  local wipe. One-create-per-agent-session falls out of the existing `_bind`
  refusal (a bound conversation must `leave_session` first) — keep, verify.
- `join_session`: unchanged semantics, verify the matrix: same human two
  windows same session OK; two windows two different sessions OK; one window
  two sessions REFUSED (bound → leave first); rejoin keeps the contributor
  watermark (beat 8d).
- `leave_session`: unchanged; anybody can leave and rejoin.
- `end_session`: REMOVE the layer-3 "others are still members" refusal — the
  creator ends for everyone, that is the spec. Keep the service's
  creator-only 403 (layer 2) and rewrite the client text to name the owner:
  "Only <creator> can end this session — talk to the owner." NEW same-window
  check: if the calling agent_session_id != the recorded creating one, do
  not end; return "this session was created from a different conversation —
  call end_session(confirm=true) if you are sure", so the agent asks the
  human yes/no. `confirm=true` bypasses only that check, never the 403.
- Service (packages/service): accept + store `created_by_agent_session` on
  create; keep POST /end creator-only. VERIFY multiple concurrent sessions
  are first-class everywhere (list endpoints, dashboard).

## 3. Server side

- `synapse-server up` already creates no session — pin with a test: boot,
  assert GET sessions list is empty.
- Dashboard lists all sessions; create_session's returned URL must land
  somewhere useful for "share with the team".

## 4. Install → configure handoff

- install.sh/ps1: final line becomes one unmissable next step:
  "NEXT: synapse configure". (Auto-running configure is out — `curl | sh`
  has no stdin to prompt on.)
- Any `synapse` command on an unconfigured machine already points at
  configure; keep.
- `synapse configure`: contributor prompt pre-filled with the OS username
  (already does — verify override works non-interactively too).

## 5. Tests / rehearsal to update

- up parser: `--shared-id`/`--purpose` REJECTED (mirror of the installer's
  refused-flags test — the same coupling, one layer up).
- create_session return includes the dashboard URL.
- end_session: same-window clean end; other-window needs confirm; non-creator
  gets the owner-naming refusal; the old "others still members" refusal test
  is DELETED (spec reversed).
- Binding matrix tests (2 windows × same/different sessions; 1 window 2
  sessions refused).
- rehearse_demo beats: 8f/8g keep (creator-only), any beat asserting the
  members-refusal flips to the confirm flow.
- serve_local.py keeps its one-command demo behavior for the repo, but its
  binding write moves behind an explicit flag (`--bind-machine`) so the
  default path stops pre-joining conversations.
