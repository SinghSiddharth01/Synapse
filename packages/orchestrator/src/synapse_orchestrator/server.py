"""The Orchestrator's MCP surface — Plan D Task D.3, implemented.

Six tools, registered via `register_tools`: `query(nl)` (retrieval through the
Synapse Service, suppression-aware), `contribute(text)` (round-trips through
the same local distiller as the passive path, provenance CONTRIBUTED), and the
four lifecycle tools `create_session` / `join_session` / `leave_session` /
`end_session` (2026-08-06 — see the lifecycle section below). The arrival
briefing rides the `instructions` field of the initialize response, composed
per connection by `briefing.build_briefing` — fail-open, hard-capped.

WHAT USED TO BE HERE. An earlier version of this file exposed a `start` MCP
prompt that let a user type something like `/mcp__synapse__start <id>` inside
a Claude Code conversation to bind that conversation to a Shared Session. That
directly contradicts Plan D Task D.3:

    "There is no attach(shared_id). At initialize the orchestrator already
    knows the product, the Agent Session and therefore the binding and
    shared_id — the agent never needs to be told which Shared Session it is
    in."

The plan's actual mechanism is `synapse join <shared_id>` (Plan A.7 / Plan
D.2) — a command run from a terminal, never from inside the agent
conversation, that binds whatever the worker's own detection currently finds
live. It does not let a human pick a specific transcript file, and accepts the
documented ambiguity of two windows of the same Agent product both being live
rather than resolving it with an explicit per-conversation pin. That command
lives in `synapse_worker.cli` (`synapse-worker join <shared_id>`), since it
only needs the worker's own detection plus the shared `SessionBinding`
read/write helpers in `synapse_contracts` — no MCP server, running or
otherwise, is on the path for join to work.

#### Session lifecycle (2026-08-06) — D.3's tool list, amended out loud

The paragraph above is now HISTORY for the "no attach" half, and deliberately
so: docs/superpowers/specs/2026-08-06-session-lifecycle-design.md amends D.3's
tool list rather than violating it silently. Two facts forced it. There was no
way to CREATE a Shared Session from anywhere a user actually sits (`POST
/v1/sessions` had no caller outside tests), and detection alone binds the wrong
conversation whenever the orchestrator's cwd differs from the conversation's or
two windows of one product are live — verified 2026-08-06 on this machine, a
session started in `/Users/siddharthsingh` lives under the slug
`-Users-siddharthsingh` and is invisible to a resolver looking at
`-Users-siddharthsingh-Dev-synapse`. What D.3 was protecting against was a
HUMAN guessing at a transcript file; `agent_session_id` is the calling agent
reporting its own `CLAUDE_CODE_SESSION_ID`, which is a fact about the
conversation, not a guess. Lifecycle lives here rather than in the worker CLI
because the worker may not open its own connection to the service (see
`synapse_worker.discovery.join_session`) and the orchestrator is the single
egress. `synapse-worker join` keeps working unchanged as the headless path.

EVERY lifecycle tool result names the Shared Session id AND the transcript it
bound. That is not decoration: silent binding is the defect the spec exists to
fix, so a result the agent can read back to the user is the fix. The binding
itself is written by `synapse_worker.discovery.join_session` — the SAME writer
`synapse-worker join` uses, one binding format, one code path. This module
resolves nothing about transcripts itself and must never grow its own
`SessionBinding(...)` construction.

TRANSPORT IS HTTP, NOT STDIO — ADR 0001. stdio spawns one server process per
client, which would give one Orchestrator per Agent and dissolve the
single-egress property the Orchestrator exists to create.

#### Post-review amendment (2026-08-04), round 3

`register_tools` (below) used to close over a single `binding` resolved once
by `cli.main` at boot, and `cli.main` only called it `if binding is not
None`. Two consequences, both round 3 review findings: (1) an orchestrator
started before any `synapse-worker join` served a permanently tool-less MCP
server — the default `_DEFAULT_INSTRUCTIONS` below tells the agent to join
in a terminal, but nothing ever registered `query`/`contribute` for it to
use once it had; (2) even when a binding existed at boot, a LATER
`synapse-worker join <different_id>` was invisible to `query`/`contribute`
— they kept using the boot-time binding forever, while the producer
endpoint (app.py) re-resolved live, so contribute() could push a Finding to
one Shared Session while query() kept reading another, in the same process
and the same MCP session.

Fixed by making `register_tools` itself resolve nothing: it now takes
`resolve_binding`, a callable invoked fresh at the start of every
`query`/`contribute` call, and `cli.main` calls `register_tools`
unconditionally, once, at boot — never gated on whether a binding exists
yet. An unbound `query`/`contribute` call returns a plain "not joined" tool
result (`_NOT_JOINED` below) instead of not existing at all; the very next
call, in the same MCP session, after a `synapse-worker join`, picks up the
new binding with no restart. See `test_tools.py` for the exact
reproduction and `app.py`'s round 3 amendment note for the equivalent fix
on the producer path.
"""

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from synapse_orchestrator.ended import is_session_ended, record_ended
from synapse_orchestrator.session_meta import record_session

logger = logging.getLogger(__name__)

# Stable marker asserted by scripts/verify_instructions.py through a REAL MCP
# client. If a client ever fails to surface it, amendment F Q11's tier
# assignment (briefing = agent-agnostic floor) is wrong and must be revisited
# BEFORE more briefing work is built.
SENTINEL = "[synapse-briefing]"

_DEFAULT_INSTRUCTIONS = (
    f"{SENTINEL} Synapse passively distils this coding session into shared "
    "team memory. No session is bound yet — run `synapse-worker join "
    "<shared_id>` in a terminal to connect one."
)


def create_mcp(instructions: str | None = None) -> FastMCP:
    return FastMCP(name="synapse", instructions=instructions or _DEFAULT_INSTRUCTIONS)


mcp = create_mcp()

# No tools or prompts registered on the module-level `mcp`. A connecting
# client that never joined a session sees a named server with an empty
# capability set, which is honest: there is nothing usable without a binding.
# `register_tools` below is what the CLI calls once a binding exists.


_NOT_JOINED = (
    "Not joined to a Shared Session yet — call `create_session` to start one, "
    "`join_session <shared_id>` to attach to a teammate's, or run `synapse-worker "
    "join <shared_id>` in a terminal. No restart needed once you have: this tool "
    "re-checks the binding on every call."
)

# The prose the spec asks for, verbatim in its first sentence ("This Shared
# Session has ended.") so an agent reading it out to a user says the same thing
# every time. Everything after it is disposition: what the agent should do now,
# because a tool result that only states a fact mid-investigation gets treated
# as one more piece of evidence rather than as an instruction to stop.
#
# Split in three (2026-08-06 review) because the middle clause is a CLAIM about
# what this process just did, and it is false in one reachable configuration:
# `register_tools(state_dir=None)` gives `_forget_ended` nothing to clear, so it
# returned having cleared nothing while this text said the binding was gone. See
# `_ended_text` below. `_SESSION_ENDED` itself is unchanged, character for
# character, and stays the constant tests assert on for the ordinary path.
_SESSION_ENDED_HEAD = (
    "This Shared Session has ended. Its memory is closed for reading and writing "
    "— the log is kept for audit only. "
)
_SESSION_ENDED_CLEARED = (
    "The local binding has been cleared, so "
    "the next call will say you are not joined rather than retrying a dead "
    "session. "
)
_SESSION_ENDED_UNCLEARED = (
    "The local binding could NOT be cleared: this orchestrator was started "
    "without a state directory, so every later call will keep reporting this "
    "same dead session until it is restarted with `--state-dir <dir>`. "
)
_SESSION_ENDED_TAIL = (
    "Start a new one with `create_session`, or ask a teammate for the "
    "id of the one they moved to and `join_session` it."
)
_SESSION_ENDED = _SESSION_ENDED_HEAD + _SESSION_ENDED_CLEARED + _SESSION_ENDED_TAIL

# Only reachable through a `register_tools(state_dir=None)` call — i.e. a test
# fixture written before the lifecycle tools existed. `cli.main` always passes
# one. Spelled out rather than left as an AttributeError because nothing may
# raise out of an MCP tool.
_NO_STATE_DIR = (
    "This orchestrator was started without a state directory, so it cannot write "
    "a session binding. Restart it with `--state-dir <dir>`."
)

# ⟨decision 008, 2026-08-06⟩ The sentence that makes a dead brain VISIBLY dead.
#
# Before this, a retrieval backend that was not answering produced an empty
# 200 and the agent said "Team memory has nothing relevant to that. (Checked —
# not skipped.)" — the single worst live outcome, because everything looks
# fine while the memory is unreadable. The instruction not to report "no
# relevant findings" is in the text on purpose: an agent that reads only the
# first clause and paraphrases is the failure this is guarding against.
_RETRIEVAL_DOWN = (
    "Shared memory is DOWN, not empty: its model backend ({provider}) is not "
    "answering, so the team's findings exist but cannot be searched right now. "
    "Tell the user this is an outage — do NOT report 'no relevant findings'. "
    "The stack self-heals within ~2 minutes; retry after that."
)


def _retrieval_down_text(response) -> str | None:
    """The outage sentence for THE 503 that means "the retriever's model is
    dead", or None for anything else.

    Both halves are checked for the same reason `is_session_ended` checks
    both: a 503 from an intermediary — a proxy, a load balancer, the LAN host
    being restarted — is an ordinary outage and must keep falling through to
    the generic handler, which says something true about it. Only the
    service's own typed body earns this text.
    """
    if response.status_code != 503:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("error") != "retrieval_unavailable":
        return None
    return _RETRIEVAL_DOWN.format(provider=body.get("provider") or "unknown")


def register_tools(server: FastMCP, *, resolve_binding, service_url: str, relay,
                   distiller_factory, transport=None, state_dir=None, cwd=None,
                   contributor: str | None = None, projects_root=None) -> None:
    """Tools speak trigger-voice; bodies stay small. `transport` is test-only.

    `resolve_binding` is called fresh at the START of every invocation of
    every tool — never captured once at registration time. See the module
    docstring's round 3 amendment note for why: this is what lets
    `register_tools` be called unconditionally, once, at boot, and still
    have a `synapse-worker join` run afterwards take effect on the very
    next tool call, in the SAME MCP session, with no restart. The four
    lifecycle tools added 2026-08-06 obey the same rule for a second
    reason: they CHANGE the binding, so a captured one would be stale the
    moment `create_session` succeeded.
    `distiller_factory` mirrors this — it takes the freshly resolved
    binding as its argument, so `contribute()`'s Distiller (and the
    Attribution it stamps) always matches whichever Shared Session the
    Finding is about to be recorded under, never a binding captured at
    registration time.

    `state_dir` is where bindings and the retained ended-session set live —
    the SAME directory `synapse-worker join` writes to, since the two must
    agree about what "joined" means. `cwd` (default: the process's) is what
    transcript detection is scoped to when a caller does not pass an
    explicit `agent_session_id`. `contributor` is the identity to use when
    NOTHING is bound yet and there is therefore no `binding.contributor` to
    read — `create_session`/`join_session` are precisely the calls made from
    that state. `projects_root` is test-only, threaded into the worker's
    finders so a fixture tree can stand in for `~/.claude/projects`.
    """
    import httpx as _httpx

    from synapse_contracts import Provenance, Segment
    from synapse_contracts.binding import clear_binding, read_binding

    # Function-local because `briefing` imports THIS module at import time
    # (`SENTINEL`, `_DEFAULT_INSTRUCTIONS`); at module level the two would be a
    # cycle. By the time `register_tools` runs, this module is fully loaded.
    from synapse_orchestrator.briefing import fetch_arrival_summary

    # The ONE import of the worker from the orchestrator, and it is here on
    # purpose: the spec requires lifecycle binding writes to go through the
    # worker's existing writer ("one code path — the orchestrator must not
    # invent its own binding format"), and transcript discovery lives there
    # too. Rejected alternative: re-implementing the two calls locally to keep
    # relay.py's "the packages share contracts only" posture intact. That
    # posture is about the write-ahead LOGS, which guard different hops and
    # genuinely should not share code; a binding file has exactly one format
    # and a second writer for it is how the two halves silently disagree about
    # which conversation is bound. Imported inside the function so importing
    # this module (app.py does, for `create_mcp`) never drags the worker in.
    from synapse_worker.discovery import (
        AGENT_REGISTRY,
        AMBIGUITY_WINDOW_SECONDS,
        bindings_dir,
        find_live_transcript_candidates,
        resolve_agent_binding,
    )
    from synapse_worker.discovery import join_session as _worker_join_session

    base = service_url.rstrip("/")
    here = Path(cwd) if cwd is not None else Path.cwd()

    # 15s for the lifecycle routes: create/join/leave/end run no model and a
    # laptop on the same LAN answers them in milliseconds, so a longer timeout
    # would only make a wedged host look like a slow one.
    #
    # `/query` is the exception and had been getting 15s by inheritance. It
    # RANKS, which is a model call — docs/overnight/FLOW.md measures real
    # rankings at 12.6-52.8s — so 15s cut off the majority of honest answers
    # and delivered them as "Shared memory is unreachable right now
    # (ReadTimeout)". That is the empty-200 failure wearing a different mask:
    # the memory is fine, the answer exists, and the agent is told it does
    # not. 90s clears the measured range with margin and still sits under the
    # service's own synthesis timeout, so a genuinely dead backend surfaces as
    # the typed 503 (decision 008) rather than as this client giving up first.
    _LIFECYCLE_TIMEOUT_S = 15.0
    _QUERY_TIMEOUT_S = 90.0

    def _client(timeout: float = _LIFECYCLE_TIMEOUT_S):
        return _httpx.AsyncClient(transport=transport, timeout=timeout)

    def _effective_binding(agent_session_id: str | None):
        """The binding the CALLING CONVERSATION speaks under — the whole of
        W2's one-orchestrator-N-clients resolution, used by every tool that
        acts on behalf of a conversation.

        MCP has no per-call identity of its own: one orchestrator serves every
        Claude Code window on the machine over one HTTP transport, and nothing
        in the protocol says which window a `query` came from. So the caller
        says, by passing `agent_session_id` (Claude Code exports it as
        `CLAUDE_CODE_SESSION_ID`; SKILL.md instructs it), and this turns that
        id into a binding:

        1. **Absent** — exactly today's answer, `resolve_binding()`: the most
           recently pinned binding on this machine. Byte-identical to the
           pre-W2 behaviour, which is what keeps every existing caller and
           every existing test correct without modification, and which is
           right whenever only one conversation is open.
        2. **Matches a per-session binding** for ANY registered agent — that
           conversation's own binding, whatever any other window has done
           since. This is the fix: before it, window A's `query` was routed by
           whichever window joined LAST, so A read and wrote B's Shared
           Session (reproduced, 2026-08-06 review).
        3. **Matches nothing** — the machine's binding, with the CALLER'S REAL
           id substituted as the acting identity. That is what makes the
           documented demo path correct with no extra setup: `scripts/
           serve_local.py` writes one machine-scope binding before any
           conversation exists, so no per-session file can exist for the
           window that then connects, and substituting its real id is what
           gives suppression and attribution a true conversation to key on
           rather than the `as-<contributor>` placeholder.

        Which agent owns the id is never something the caller has to know —
        every registered agent is probed, same discipline as `_bind`.
        """
        if agent_session_id is None:
            return resolve_binding()
        if state_dir is not None:
            for agent in AGENT_REGISTRY:
                found = resolve_agent_binding(Path(state_dir), agent, agent_session_id)
                if found is not None:
                    return found.to_local_binding()
        fallback = resolve_binding()
        if fallback is None:
            return None
        return fallback.model_copy(update={"agent_session_id": agent_session_id})

    def _identity() -> str | None:
        """Who this conversation is, for the service.

        The live binding wins: it is the identity that has already reached the
        service and that `Attribution.contributor` is stamped with, so a
        `leave_session` must address the same string the findings did. The
        configured `contributor` is only the seed for the state where no
        binding exists yet — which is exactly when `create_session` and
        `join_session` are called."""
        binding = resolve_binding()
        if binding is not None:
            return binding.contributor
        return contributor

    def _unexpected(tool: str, exc: BaseException) -> str:
        """Fail-open text for anything not anticipated by name.

        The long comment inside `contribute()` below is the reason this
        exists for all six tools and not just that one: FastMCP surfaces an
        escaping exception to the agent as a raw internal error string, and
        an agent that gets one mid-conversation has no idea whether its
        session is now joined, half-joined or ended."""
        logger.warning("%s: unexpected failure (%s: %s)", tool, exc.__class__.__name__, exc)
        return (f"`{tool}` couldn't complete ({exc.__class__.__name__}). Nothing was "
                "changed that this tool can see — check `synapse-worker status` in a "
                "terminal before trying again.")

    def _error_text(resp) -> str:
        """The service's own `{"error": ...}` prose, or a fallback.

        Passed through rather than re-worded because the service writes it for
        a human: the 403 from `POST /end` names the creator, which is the whole
        point of surfacing it (spec's error table)."""
        try:
            body = resp.json()
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                return body["error"]
        except ValueError:
            pass
        return f"the service answered {resp.status_code}"

    def _remember(shared_id: str, *, created_by: str | None, purpose: str | None) -> None:
        """Retain who owns `shared_id` and what it is for, on THIS machine.

        Called by `create_session` and `join_session` — the two tools that
        attach this machine to a session — so that `cmd_resync` can send the
        REAL creator and purpose after a service restart instead of inventing
        `created_by="resync"`, which used to leave the recreated session owned
        by a string nobody can be and its creator-only end gate refusing the
        person who created it. See `synapse_orchestrator.session_meta`, which
        is a STOPGAP for service-side log persistence exactly as `ended.py` is.

        `record_session` never raises, so this is not wrapped: it swallows and
        logs, because nothing may raise out of an MCP tool and a lost record
        degrades to "resync recreates this session ownerless", never to a
        failed create or join.
        """
        if state_dir is None:
            return
        record_session(state_dir, shared_id, created_by=created_by, purpose=purpose)

    def _session_identity(resp) -> tuple[str | None, str | None]:
        """`(created_by, purpose)` as the service reported them, or `(None, None)`.

        `POST /v1/sessions/{sid}/members` returns these alongside `members`
        (2026-08-06) precisely so a joining orchestrator can record who owns the
        session it just joined. Every step is tolerant because an orchestrator
        and a service are separate processes on separate laptops and deploy in
        either order: against a service that has not been upgraded the body is
        still `{"members": [...]}`, and "learned nothing" must read as None
        rather than as an exception out of a join that in fact succeeded.
        `created_by` is legitimately null on a session recreated after a
        restart by a machine with no record — that null is the answer, and it
        is stored as one.
        """
        try:
            body = resp.json()
        except ValueError:
            return None, None
        if not isinstance(body, dict):
            return None, None
        created_by, purpose = body.get("created_by"), body.get("purpose")
        return (created_by if isinstance(created_by, str) else None,
                purpose if isinstance(purpose, str) else None)

    def _bindings_on(shared_id: str) -> list:
        """EVERY Agent product on this machine bound to `shared_id`.

        NOT `binding_path_for_agent(state_dir, binding.agent)`, which is one
        file — the one `_resolve_binding` happened to pick as "most recently
        joined across every product". `_worker_join_session`'s detection path
        loops the whole `AGENT_REGISTRY` and writes a binding for EVERY live
        product (discovery.py), so a `create_session` on a machine with Claude
        Code and Codex both open leaves two files pointing at the same
        `shared_id` — that is the documented path, not an exotic one.

        BOTH binding layouts are swept (W2, 2026-08-06): the legacy
        `bindings/<agent>.json` and the per-session
        `bindings/<agent>/<agent_session_id>.json`. `_unbind` is this
        function's only caller, and a leave/end that cleared only the legacy
        mirror would leave the per-session file behind still naming the
        session — the worker would resolve it and keep distilling into a
        Shared Session the conversation was just told it had left, which is
        the exact class of silent falsehood the multi-file `_unbind` fix
        existed to end.

        Never raises: `read_binding` already answers None for an absent or
        corrupt file, and `glob` on a bindings dir that does not exist yields
        nothing. Callers here are MCP tools.
        """
        if state_dir is None:
            return []
        root = bindings_dir(Path(state_dir))
        paths = sorted(root.glob("*.json")) + sorted(root.glob("*/*.json"))
        return [(path, b)
                for path, b in ((p, read_binding(p)) for p in paths)
                if b is not None and b.shared_id == shared_id]

    def _unbind_paths(attached: list) -> list:
        """Delete the named binding files. Returns the bindings they held.

        `attached` is `[(path, binding)]` as `_bindings_on` produces it, so a
        caller can clear a SUBSET — which is what `leave_session
        (agent_session_id=…)` does: one conversation detaches while its
        siblings on the same machine stay bound. Splitting this out of
        `_unbind` below is the whole mechanism; `_unbind` is now "all of
        them", spelled once.
        """
        for path, _ in attached:
            try:
                clear_binding(path)
            except OSError as exc:
                # Nothing may raise out of an MCP tool, and a binding we could
                # not delete is worth naming rather than crashing on: the
                # conversation stays attached and the user needs to know.
                logger.warning("Could not clear binding %s (%s)", path, exc)
        return [b for _, b in attached]

    def _unbind(shared_id: str) -> list | None:
        """Clear EVERY binding file pointing at `shared_id`. Returns the
        bindings it cleared, or None when there is no state dir to clear in.

        Clearing only one was a 2026-08-06 review finding, and the reason the
        return type carries the whole list: with two products bound, the old
        one-file version deleted whichever `_resolve_binding` picked and left
        the other still naming the session, so `leave_session` told the user
        "nothing more from here will reach sh-1" while the very next `query`
        resolved the surviving binding, queried sh-1, and the worker kept
        distilling into it — with `Relay._register_members` silently re-adding
        the contributor the DELETE had just removed. Every claim in that result
        text was false, and none of it was visible from inside the
        conversation.

        None (rather than []) for the no-state-dir case so callers can say so
        instead of asserting a clear that did not happen — `_bind` already
        handles that state honestly via `_NO_STATE_DIR` and these did not.
        """
        if state_dir is None:
            return None
        return _unbind_paths(_bindings_on(shared_id))

    def _forget_ended(binding) -> bool:
        """React to an observed close: clear the local bindings, remember the id.

        Clearing is the spec's "Local binding | Cleared on the first 409
        observed" — without it every subsequent call retries a session that can
        never answer, and the agent is told "unreachable" forever instead of
        "not joined". Recording the id is the resync stopgap
        (`synapse_orchestrator.ended`): the service's store is in-memory, so a
        restart would otherwise un-end this session and `resync` would refill
        it. Only ever called behind `is_session_ended`, never behind a bare
        409.

        `relay.note_ended` (2026-08-06 review) is the third half: the Relay's
        own `_ended` set is seeded at boot and grown only by 409s it observes
        ITSELF, so a closure seen here — on `query`'s request, not the relay's
        — left the running process still re-POSTing that session's queued
        findings on every tick, which is precisely the loop relay.py's
        known-ended skip exists to prevent.

        False means there was no state dir to clear in; the caller says so
        rather than claiming a clear that did not happen.
        """
        if state_dir is None:
            return False
        _unbind(binding.shared_id)
        record_ended(state_dir, binding.shared_id)
        relay.note_ended(binding.shared_id)
        return True

    def _ended_text(cleared: bool, suffix: str = "") -> str:
        """The spec's "This Shared Session has ended." prose, honest about
        whether the binding actually went away. See `_SESSION_ENDED` above."""
        middle = _SESSION_ENDED_CLEARED if cleared else _SESSION_ENDED_UNCLEARED
        return _SESSION_ENDED_HEAD + middle + _SESSION_ENDED_TAIL + suffix

    def _summarize(bindings) -> str:
        return "; ".join(
            f"{b.agent} conversation {b.agent_session_id} ({b.transcript_path})"
            for b in bindings)

    def _bind(shared_id: str, who: str, agent_session_id: str | None):
        """Bind this conversation to `shared_id`. Returns (bindings, refusal).

        `refusal` is prose and non-None exactly when we would otherwise have
        GUESSED which conversation this is. Two guesses are refused:

        - ambiguity, per the spec ("if two or more transcripts are inside the
          live window and their mtimes are within AMBIGUITY_WINDOW_SECONDS of
          each other, refuse and list the candidates"). The worker's
          `find_live_transcript` deliberately still returns the newest in that
          case, so `synapse-worker run` keeps working on a two-window machine;
          the refusal belongs here, to the caller who can act on it by passing
          `agent_session_id`.
        - nothing bound at all, which for an explicit `agent_session_id` means
          the id matched no transcript under any registered agent's root — the
          worker logs and returns [] rather than falling back to mtime, and
          quietly binding "the newest thing instead" would defeat the argument.

        Every registered agent is checked for ambiguity, not just claude-code:
        `_worker_join_session`'s detection path loops the whole registry and
        binds each product it finds live, so an ambiguous codex pair would be
        mis-bound just as silently.
        """
        if state_dir is None:
            return [], _NO_STATE_DIR
        if agent_session_id is None:
            for agent in AGENT_REGISTRY:
                live = find_live_transcript_candidates(here, projects_root, agent=agent)
                if not live.ambiguous:
                    continue
                listing = ", ".join(f"{t.session_id} ({t.path})" for t in live.candidates)
                return [], (
                    f"Refusing to guess which {agent} conversation this is: "
                    f"{len(live.candidates)} are live and were last written within "
                    f"{AMBIGUITY_WINDOW_SECONDS}s of each other — {listing}. Call this "
                    "tool again passing agent_session_id set to your own session id "
                    "(Claude Code exports it as CLAUDE_CODE_SESSION_ID), which is "
                    "exact and never consults modification times.")
        bindings = _worker_join_session(shared_id, who, here, Path(state_dir),
                                        projects_root=projects_root,
                                        agent_session_id=agent_session_id)
        if not bindings:
            if agent_session_id is not None:
                return [], (
                    f"No transcript anywhere matches agent_session_id "
                    f"{agent_session_id!r}, so nothing was bound — and this tool will "
                    "not fall back to picking the most recently modified conversation, "
                    "because that would bind a different one than you asked for. Check "
                    "the id you passed.")
            return [], (
                "No live agent conversation was detected under "
                f"{here}, so nothing was bound. If this conversation was started from "
                "a different directory, call this tool again passing agent_session_id "
                "(Claude Code exports it as CLAUDE_CODE_SESSION_ID) — that is matched "
                "by filename across every project, not by directory.")
        return bindings, None

    @server.tool(description=(
        "Search the team's shared memory. Call BEFORE exploring an unfamiliar "
        "subsystem, when debugging something a teammate may also be working on, "
        "or before concluding something is a dead end. "
        # Trigger-voice told the agent WHEN to call this and nothing about what
        # a result means, so a hit was treated as a lead to check rather than
        # an answer to deliver: observed 2026-08-05, a session that retrieved
        # the exact cause of a 401 and then spent three minutes rediscovering
        # it from the filesystem before saying anything. A returned Finding is
        # a teammate's own verified experience, and saying so is the point of
        # the system -- credit is what makes the collaboration visible.
        "A result is a teammate's verified experience, not a hypothesis: if one "
        "explains what you are looking at, say so to the user immediately and "
        "name who found it, before investigating further. "
        "Pass agent_session_id set to your own session id (Claude Code exports "
        "it as CLAUDE_CODE_SESSION_ID): one machine can have several "
        "conversations joined at once, and it is what says which one is asking. "
        "Without it this falls back to the most recently joined conversation on "
        "the machine, which is a guess as soon as a second window is open."))
    async def query(question: str, agent_session_id: str | None = None) -> str:
        binding = _effective_binding(agent_session_id)
        if binding is None:
            return _NOT_JOINED
        url = f"{base}/v1/sessions/{binding.shared_id}/query"
        try:
            # The one route here that runs a model — see _QUERY_TIMEOUT_S.
            async with _client(_QUERY_TIMEOUT_S) as client:
                # BOTH identity fields, and they now answer different questions
                # (decisions/001, 2026-08-06): `agent_session` is what
                # suppression is keyed on — "is this already in the context
                # window asking?" is a fact about ONE conversation — while
                # `contributor` keys the watermark, because "how much have I not
                # seen?" is a fact about one person across their conversations.
                # `binding.agent_session_id` is the CALLER's own id whenever it
                # passed one (`_effective_binding`), which is what makes two
                # windows of one human teammates rather than one participant.
                # Sending both also keeps this orchestrator correct against a
                # service that has not been upgraded yet — the two are separate
                # processes on separate laptops and deploy in either order.
                resp = await client.post(url, json={
                    "query": question,
                    "agent_session": binding.agent_session_id,
                    "contributor": binding.contributor})
                # BEFORE raise_for_status: an ended session is not an outage
                # and must not be reported as one. `is_session_ended` checks
                # the body as well as the 409, so an unrelated conflict from
                # anything in between still falls through to the generic
                # handler below and does NOT clear the binding.
                if is_session_ended(resp):
                    return _ended_text(_forget_ended(binding))
                # ALSO before raise_for_status, and for the same reason the
                # ended-session check is (decision 008): a 503 whose body says
                # `retrieval_unavailable` is not a generic outage, it is the
                # one outage this agent must not paper over. Left to
                # raise_for_status it would render "Shared memory is
                # unreachable right now (HTTPStatusError)" — honest but
                # unactionable, and indistinguishable from the service itself
                # being down.
                if (down := _retrieval_down_text(resp)) is not None:
                    return down
                resp.raise_for_status()
                data = resp.json()
            # A well-formed empty result ({"findings": []}) and a response that
            # doesn't even have a "findings" list are different situations —
            # the first is a true "nothing relevant", the second is E3's
            # contract not matching what this reads, and must not be rendered
            # as the same confident "checked, found nothing" answer.
            if not isinstance(data, dict) or "findings" not in data:
                raise ValueError(f"response had no 'findings' list: {data!r}")
            findings = data["findings"]
            if not isinstance(findings, list):
                raise ValueError(f"'findings' was not a list: {findings!r}")
            lines = []
            for f in findings:
                attributions = f["attributions"]
                if not attributions:
                    raise ValueError(f"finding {f.get('id')!r} has no attributions")
                # A Synthesized Finding carries every source it merged from
                # (CONTEXT.md) — crediting only attributions[0] would silently
                # turn a pooled, teammate-derived insight into what reads as
                # one person's, in the common case the asking agent's own.
                contributors = dict.fromkeys(a["contributor"] for a in attributions)
                lines.append(f"- [{f['type']}] {f['text']} — {', '.join(contributors)}")
        except (_httpx.HTTPError, OSError) as exc:
            return f"Shared memory is unreachable right now ({exc.__class__.__name__})."
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("query: response from %s didn't match the expected shape (%s)",
                           url, exc)
            return ("Shared memory returned something `query` couldn't parse — try again, "
                    "or ask a teammate directly for now.")
        except Exception as exc:                  # noqa: BLE001 — nothing raises out
            return _unexpected("query", exc)      # of an MCP tool; see contribute()
        if not lines:
            return "Team memory has nothing relevant to that. (Checked — not skipped.)"
        # "best first" was a claim this process cannot make. Ordering comes
        # from the service's retriever; when that is a stand-in returning
        # everything in log order, the header asserted a ranking that was not
        # there -- observed 2026-08-05 with the one relevant finding sitting
        # LAST under a header promising it would be first.
        #
        # The closing line is disposition: what to DO with a hit. Without it
        # the agent has a tool result of unknown authority in the middle of an
        # investigation, and its default is to keep investigating until certain
        # rather than to speak. Answer first, then verify what genuinely needs
        # verifying -- and say which part you are checking, so the user knows
        # what is known versus what is being confirmed.
        return (
            "What the team already knows about this — each line is one "
            "teammate's finding, and who found it:\n"
            + "\n".join(lines)
            + "\n\nIf one of these explains the problem in front of you, tell the "
            "user now and credit whoever found it, rather than re-deriving it. "
            "Then verify only what your particular situation could change, and "
            "say what you are checking and why."
        )

    @server.tool(description=(
        "Push an insight to the team's shared memory. Call when you have learned "
        "something non-obvious a teammate would benefit from — a root cause, a "
        "dead end, a decision and its why. A few sentences of plain prose. "
        "Pass agent_session_id set to your own session id (Claude Code exports "
        "it as CLAUDE_CODE_SESSION_ID) so the finding is attributed to THIS "
        "conversation: that is what lets another window of yours read it as "
        "something learned elsewhere rather than as its own echo."))
    async def contribute(text: str, agent_session_id: str | None = None) -> str:
        binding = _effective_binding(agent_session_id)
        if binding is None:
            return _NOT_JOINED
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc)
        event = {"role": "assistant", "kind": "text", "content": text,
                 "ts": ts.isoformat(), "agent_session_id": binding.agent_session_id}
        segment = Segment(id=f"contrib-{ts.strftime('%H%M%S')}",
                          agent_session_id=binding.agent_session_id,
                          events=[event], started_at=ts, ended_at=ts)
        # Post-review amendment (2026-08-04): "Fail open, always" (Global
        # Constraints) applies to contribute() exactly as it does to query()
        # above — an unreachable NPU, a tripped prompt-drop guard
        # (synapse_distiller.guards.PromptDropError), or a bad on-disk config
        # (missing synapse.toml, an unknown prompt pack — build_npu_distiller,
        # cli.py) are all real, expected failure modes of a laptop-local
        # model. Both `distiller_factory()` itself and `distil()` are inside
        # this guard: a bad config can raise from the factory before distil()
        # is ever called. Nothing may raise out of this MCP tool — FastMCP
        # would otherwise wrap it as a raw internal exception string handed
        # to the agent, and the contributed prose would simply be gone.
        try:
            distiller = distiller_factory(binding)
            findings, stats = await distiller.distil(segment)
        except Exception as exc:
            logger.warning("contribute: distillation failed (%s: %s)",
                           exc.__class__.__name__, exc)
            # ⟨decision 008's sibling, post-review⟩ The write half gets the
            # same treatment the read half got. "Couldn't process that right
            # now" named nothing: not the seam, not that this is an OUTAGE
            # rather than a problem with the prose, and not the ~2-minute
            # self-heal `_RETRIEVAL_DOWN` promises on the query side. An agent
            # reading it rephrases and retries — burning the user's turn on a
            # rewrite that cannot help, because the model that would have read
            # it is not answering.
            return (
                f"Your note was NOT recorded, and the reason is on THIS "
                f"machine, not in what you wrote: the local model seam that "
                f"distils prose into findings failed "
                f"({exc.__class__.__name__}). Tell the user this is a local "
                f"outage — do NOT rewrite the note and retry, the same seam "
                f"would read the rewrite. If `scripts/serve_local.py` is "
                f"running it supervises that seam and restarts it within "
                f"~2 minutes, so retrying after that is worth one attempt; "
                f"check `.synapse/logs/supervisor.log` if it keeps failing. "
                f"Otherwise say the insight to your teammates directly.")
        for f in findings:
            f.provenance = Provenance.CONTRIBUTED
        if not findings:
            return "Nothing durable extracted from that — try stating the insight directly."
        # `shared_id=binding.shared_id` (relay.py's per-call override, round 3
        # amendment) rather than relaying on `relay.shared_id`: this is the
        # SAME live-resolved binding query() would use right now, so a
        # contribute() and a query() issued back-to-back never disagree about
        # which Shared Session they are talking to, even if the producer
        # endpoint has rebound `relay.shared_id` in between (round 2/3 review).
        try:
            relay.record(findings, shared_id=binding.shared_id)
            sent, pending = await relay.flush()
        except Exception as exc:                  # noqa: BLE001 — see the comment above
            return _unexpected("contribute", exc)
        # The Relay swallows every HTTP outcome by design, so "did the session
        # this was addressed to turn out to be closed?" has to be ASKED. Without
        # this the agent is told "queued (1 pending)" about a note the relay has
        # already dropped and can never deliver — the most misleading answer
        # available, because it reads as "it will land later".
        if binding.shared_id in relay.ended_session_ids():
            return _ended_text(
                _forget_ended(binding),
                " Your note was NOT recorded — say it to your teammates directly, or "
                "start a new session and contribute it there.")
        state = "shared with the team" if sent else f"queued ({pending} pending)"
        return f"{len(findings)} finding(s) {state}."

    # ── lifecycle (2026-08-06) ─────────────────────────────────────────────
    # Registered exactly like query/contribute: unconditionally, at boot,
    # resolving the binding fresh inside every call. `create_session` in
    # particular MUST exist while nothing is joined — it is the tool whose job
    # is to make something joined — so gating registration on a binding would
    # be the round 3 tools-frozen-at-boot blocker all over again, in the one
    # place where it is unrecoverable rather than merely annoying.

    @server.tool(description=(
        "Start a NEW Shared Session for the team and attach this conversation to "
        "it. Call when the user wants to begin sharing what this session learns — "
        "starting a piece of work several people are on, or when `query` says you "
        "are not joined and there is no existing session to join. "
        "Pass agent_session_id if you know your own session id (Claude Code "
        "exports it as CLAUDE_CODE_SESSION_ID); without it this falls back to "
        "detecting the most recently active conversation, which is a guess when "
        "more than one is open. "
        "The result names the Shared Session id AND the transcript file it bound: "
        "read both back to the user, because if the transcript is not this "
        "conversation's then nothing said here will reach the team, and that is "
        "invisible from the inside."))
    async def create_session(purpose: str, agent_session_id: str | None = None) -> str:
        who = _identity()
        if who is None:
            return ("No contributor identity is configured, so there is nobody to "
                    "create the session as. Restart the orchestrator with "
                    "`--contributor <your name>`.")
        try:
            async with _client() as client:
                resp = await client.post(f"{base}/v1/sessions",
                                         json={"purpose": purpose, "created_by": who})
                resp.raise_for_status()
                shared_id = resp.json()["shared_id"]
                # WHO owns this session and WHAT it is for, retained locally
                # before anything else can go wrong. `who`/`purpose` as sent
                # rather than as echoed back: this call never passes a
                # `shared_id`, so it is always a fresh create and the service
                # cannot have returned anyone else's — and reading the echo
                # would make the record depend on a response field an
                # un-upgraded service may not send.
                #
                # Recorded even if the bind below is refused. The session
                # exists from the line above onwards whatever happens to the
                # binding, and it is the SESSION's identity being retained;
                # dropping it on a refused bind would leave a live session this
                # machine created with no record of who created it, which is
                # the exact hole this closes.
                _remember(shared_id, created_by=who, purpose=purpose)
                # The creator is a MEMBER of what they just created. The
                # service's `create_session` starts `members: []` and never
                # adds `created_by` (api.py), and `POST /v1/sessions` had no
                # caller outside tests until this tool existed — so on the new
                # primary path for starting a session, the person sitting in it
                # was absent from `members` until their first Finding reached
                # the service and `Relay._register_members` registered them.
                # That is the same `members: []` symptom that docstring records
                # as the observable bug it was written to fix, and here it also
                # blinds `end_session`'s layer-3 gate: a teammate who joined but
                # has not yet produced a finding is invisible to it, so ending
                # would silently close a session somebody else is in.
                #
                # Best-effort and swallowed exactly like `join_session`'s: a
                # member list is metadata, and the session itself exists and is
                # about to be bound whatever this returns.
                try:
                    member = await client.post(
                        f"{base}/v1/sessions/{shared_id}/members",
                        json={"contributor": who})
                    member.raise_for_status()
                except (_httpx.HTTPError, OSError) as exc:
                    logger.info("create_session: member registration for %r on %r "
                                "deferred (%s)", who, shared_id, exc.__class__.__name__)
        except (_httpx.HTTPError, OSError) as exc:
            return (f"Shared memory is unreachable right now ({exc.__class__.__name__}) "
                    "— no session was created. Is `synapse-service` running?")
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("create_session: unreadable response (%s)", exc)
            return ("Shared memory answered something `create_session` couldn't read "
                    "— a session may or may not have been created; check with a teammate.")
        except Exception as exc:                       # noqa: BLE001
            return _unexpected("create_session", exc)

        # The session EXISTS from here on, whatever happens to the binding. So
        # the id is reported either way: telling the agent only "binding failed"
        # would strand a live Shared Session nobody can name.
        try:
            bindings, refusal = _bind(shared_id, who, agent_session_id)
        except Exception as exc:                       # noqa: BLE001
            return (f"Shared Session {shared_id} was created, but binding this "
                    f"conversation to it failed. {_unexpected('create_session', exc)}")
        if refusal is not None:
            return (f"Shared Session {shared_id} was created (purpose: {purpose!r}), but "
                    f"this conversation was NOT bound to it — nothing here will reach the "
                    f"team until it is. {refusal}")
        return (f"Created Shared Session {shared_id} (purpose: {purpose!r}) as {who}, and "
                f"bound {_summarize(bindings)} to it. Tell the user that id — it is what "
                "teammates pass to `join_session`. Findings from this conversation now go "
                f"to {shared_id} and nowhere else.")

    @server.tool(description=(
        "Attach this conversation to an EXISTING Shared Session a teammate has "
        "already started. Call when the user gives you a session id (sh-…), or "
        "when they name a session someone else is working in. "
        "Pass agent_session_id if you know your own session id (Claude Code "
        "exports it as CLAUDE_CODE_SESSION_ID) — it binds exactly that "
        "conversation instead of the most recently active one. "
        "The result names the Shared Session id AND the transcript file it bound. "
        "Read both back to the user: a join that bound the wrong transcript looks "
        "identical from inside this conversation to one that worked. "
        # W5. The result now also carries the session's purpose and a summary
        # of what the team has already established — the whole point of
        # joining a session that is hours old. Said here as well as in the
        # result itself because a tool description is read before the call and
        # sets the expectation that there IS something to relay.
        "The result also carries what this session is FOR and what the team has "
        "already worked out in it, including anything new since you last looked. "
        "Summarise that for the user in your own words as soon as you get it — "
        "they cannot see tool results, and a join they hear nothing about is "
        "indistinguishable from one that did not happen."))
    async def join_session(shared_id: str, agent_session_id: str | None = None) -> str:
        who = _identity()
        if who is None:
            return ("No contributor identity is configured, so there is nobody to join "
                    "as. Restart the orchestrator with `--contributor <your name>`.")
        try:
            async with _client() as client:
                # Liveness FIRST, on a route that is gated (`/members` is not,
                # deliberately — a member of a closed session must still be able
                # to leave it). Without this probe, joining a session that has
                # already ended succeeds, writes a binding, and the mistake only
                # surfaces on the next `query` — after the worker has begun
                # distilling this conversation towards a dead session.
                probe = await client.get(f"{base}/v1/sessions/{shared_id}/watermark",
                                         params={"contributor": who})
                if is_session_ended(probe):
                    return (f"Shared Session {shared_id} has ended, so there is nothing "
                            "to join — its memory is closed for reading and writing. Ask "
                            "the team which session replaced it, or call `create_session`.")
                if probe.status_code == 404:
                    return (f"No Shared Session {shared_id!r} exists. Check the id with "
                            "whoever gave it to you — ids look like `sh-1a2b3c4d`.")
                probe.raise_for_status()
                resp = await client.post(f"{base}/v1/sessions/{shared_id}/members",
                                         json={"contributor": who})
                resp.raise_for_status()
                # The creator and purpose of the session we just joined, from
                # the one response that carries them. Retained so that if THIS
                # machine is the one that resyncs after a service restart, the
                # session comes back owned by whoever really owns it rather
                # than by "resync" — a create-or-return recreate is issued by
                # whichever orchestrator has the findings, not by the creator.
                created_by, purpose = _session_identity(resp)
                _remember(shared_id, created_by=created_by, purpose=purpose)
        except (_httpx.HTTPError, OSError) as exc:
            return (f"Shared memory is unreachable right now ({exc.__class__.__name__}) "
                    f"— not joined to {shared_id}. Is `synapse-service` running?")
        except Exception as exc:                       # noqa: BLE001
            return _unexpected("join_session", exc)

        try:
            bindings, refusal = _bind(shared_id, who, agent_session_id)
        except Exception as exc:                       # noqa: BLE001
            return _unexpected("join_session", exc)
        if refusal is not None:
            return (f"Registered as a member of {shared_id}, but this conversation was "
                    f"NOT bound to it — nothing here will reach the team until it is. "
                    f"{refusal}")
        joined = (f"Joined Shared Session {shared_id} as {who}, and bound "
                  f"{_summarize(bindings)} to it. Findings from this conversation now go "
                  f"to {shared_id}.")
        # THE ARRIVAL SUMMARY, delivered here rather than in `instructions` (W5).
        #
        # `instructions` is composed at MCP CONNECTION INIT, not at join
        # (briefing.py's W5 amendment), so a conversation that connected before
        # it joined — the demo's exact ordering, and the ordinary one for
        # anybody who joins mid-conversation — was briefed about no session at
        # all, and this result carried no memory content to make up for it. The
        # join was silent: nothing the agent could read, therefore nothing it
        # could say. Returning the summary IN THE TOOL RESULT is what makes the
        # storyboard's awareness moment possible, because a tool result is the
        # one surface an agent has just been handed and will speak from.
        #
        # AFTER the bind, deliberately. This is the only place the summary can
        # be fetched with the identity the conversation actually holds, and a
        # summary is worth nothing attached to a join that did not bind.
        summary = await fetch_arrival_summary(
            base, shared_id, contributor=who,
            agent_session=bindings[0].agent_session_id if bindings else agent_session_id,
            transport=transport)
        if summary is None:
            # Fail open, and say what to do instead: the join WORKED, so the
            # agent must not report it as broken — but "call `query`" is the
            # advice that was in this string before the summary existed, and it
            # is exactly what recovers the missing context by hand.
            return (f"{joined} Call `query` before investigating anything — the team may "
                    "already know it.")
        return f"{joined}\n\n{summary}"

    @server.tool(description=(
        "Detach THIS conversation from the Shared Session it is in, leaving the "
        "session open for everyone else. Call when the user says they are done "
        "with this piece of shared work, or wants this conversation to stop "
        "feeding team memory. This is not how a session is closed — use "
        "`end_session` for that, and only for a session nobody else is in. "
        "Pass agent_session_id set to your own session id (Claude Code exports "
        "it as CLAUDE_CODE_SESSION_ID) to detach ONLY this conversation: "
        "without it every conversation on this machine that is in the same "
        "Shared Session is detached, because there is nothing else to tell "
        "them apart. "
        "The result names the session left and the transcript unbound; after it, "
        "`query` and `contribute` report that you are not joined."))
    async def leave_session(agent_session_id: str | None = None) -> str:
        binding = _effective_binding(agent_session_id)
        if binding is None:
            return _NOT_JOINED
        shared_id = binding.shared_id
        # EVERY identity this machine has attached to `shared_id`, not only the
        # one `_resolve_binding` picked by `pinned_at`. Two products bound to
        # one session can carry two contributors (a `synapse-worker join` writes
        # its own), and removing only the picked one left the other still a
        # member of a session this tool reported having left. The union with
        # `binding.contributor` keeps the single-binding case byte-identical and
        # covers the no-state-dir case, where `_bindings_on` can see nothing.
        attached = _bindings_on(shared_id)
        # WHICH of them this call detaches (W2, 2026-08-06). With an explicit
        # `agent_session_id`, exactly the one binding that names it: two windows
        # of one machine are two participants, and one of them leaving must not
        # silently unbind the other — the pre-W2 behaviour, where `leave` in
        # window A cleared window B's binding too and B kept feeding a session
        # it had been told nothing more would reach. Without the argument there
        # is nothing to tell the conversations apart, so it stays what it has
        # always been: this machine leaves, whole.
        if agent_session_id is not None:
            mine = [(p, b) for p, b in attached
                    if b.agent_session_id == agent_session_id]
        else:
            mine = []
        # An id that matches no binding on this machine is the machine-scope
        # case (`serve_local.py`'s stand-in, which no conversation's id can
        # ever match) — there is no per-conversation binding to clear, so what
        # this call can honestly detach is the machine, and the result says so.
        detaching_all = not mine
        leaving = attached if detaching_all else mine
        leaving_paths = {p for p, _ in leaving}
        # WHAT THE SERVICE IS TOLD, and it is two different sentences depending
        # on how wide the detach is (2026-08-06).
        #
        # A specific window detaching sends `?agent_session=<window>`, so the
        # service marks THAT participant departed and leaves the person on the
        # roster while any sibling window is still bound. The contributor alone
        # cannot say this: two windows of one person are one string, which is
        # why leaving in one window used to flip both of that person's rows on
        # /debug to LEFT.
        #
        # The whole machine detaching (`detaching_all`) still sends the
        # contributor with NO window, which is the honest reading of it —
        # every window here is going, so the person is going. Unchanged wire
        # shape, and the path every pre-W2 caller takes.
        #
        # The old "another conversation still holds this contributor, so send
        # NOTHING" suppression is gone. It existed to stop the member list
        # flickering when the DELETE could only be person-wide, and it is now
        # exactly backwards: the case it suppressed — siblings remain — is
        # precisely the case a window-scoped DELETE is FOR, and suppressing it
        # left the departed window reading ACTIVE on /debug forever.
        #
        # What it was protecting is real, though, and is protected below
        # instead. `end_session`'s layer-3 gate reads `members` to decide
        # whether anybody else is still here, so a contributor a surviving
        # window still speaks as must stay on the roster. The service infers
        # that where it can (`store.remove_member` counts a contributor's
        # windows from the log and retires them only when the last has gone),
        # but its only evidence is findings pushed — a sibling window that has
        # joined and pushed NOTHING is invisible to it. This end knows better:
        # it is holding that window's binding. So it re-registers the
        # contributor after the DELETE, which restores the roster without
        # disturbing the departure, since a windowless `add_member` retracts
        # only the person-level marker and never a window's.
        staying = {b.contributor for p, b in attached if p not in leaving_paths}
        if detaching_all:
            departures = [(b.contributor, None) for _, b in leaving]
            departures.append((binding.contributor, None))
        else:
            departures = [(b.contributor, b.agent_session_id) for _, b in leaving]
            departures.append((binding.contributor, binding.agent_session_id))
        note = ""
        try:
            async with _client() as client:
                # `or ""` in the key, not a bare sort: a windowless pair and a
                # windowed one for the same contributor would compare None
                # against a str and raise. The branches above cannot produce
                # both today, and a TypeError out of a leave is not the way to
                # find out if that ever changes.
                for who, window in sorted(set(departures),
                                          key=lambda pair: (pair[0], pair[1] or "")):
                    resp = await client.delete(
                        f"{base}/v1/sessions/{shared_id}/members/{who}",
                        params=None if window is None else {"agent_session": window})
                    # A 404 means the service has already forgotten this session
                    # (its store is in-memory and dies with a restart). That is
                    # not a reason to keep the local binding — there is even
                    # less to stay attached to.
                    if resp.status_code != 404:
                        resp.raise_for_status()
                # Put back the contributors a surviving window still speaks
                # as. Only reachable when a specific window left — a whole
                # machine detaching has no surviving window by construction,
                # and `staying` is empty there.
                for who in sorted(staying):
                    resp = await client.post(
                        f"{base}/v1/sessions/{shared_id}/members",
                        json={"contributor": who})
                    if resp.status_code != 404:
                        resp.raise_for_status()
        except (_httpx.HTTPError, OSError) as exc:
            # UNBIND ANYWAY, deliberately. Rejected alternative: keep the
            # binding until the service confirms the departure, so the two
            # never disagree. That trades a metadata inconsistency (the member
            # list still lists you, and any later push re-registers you anyway
            # — `Relay._register_members` is idempotent) for a much worse one:
            # a user who asked to leave, was told the service is down, and
            # whose conversation keeps being distilled into the session they
            # believe they left.
            note = (f" The service could not be reached ({exc.__class__.__name__}), so it "
                    "may still list you as a member — nothing further from this "
                    "conversation will be sent there regardless.")
        except Exception as exc:                       # noqa: BLE001
            return _unexpected("leave_session", exc)
        cleared = None if state_dir is None else _unbind_paths(leaving)
        if cleared is None:
            # Nothing was unbound and nothing can be, so do not say it was.
            return (f"Removed you from Shared Session {shared_id} at the service, but the "
                    f"local binding is untouched. {_NO_STATE_DIR} Until then this "
                    f"conversation is still bound to {shared_id} and still feeding it."
                    f"{note}")
        where = (" (was following "
                 + ", ".join(b.transcript_path for b in cleared) + ")") if cleared else ""
        # Say how wide the detach actually was. Told "left", a user reasonably
        # assumes "this conversation" — and without an `agent_session_id` there
        # was no way for this tool to do anything narrower, so every sibling
        # window on this machine has just been detached too and is entitled to
        # be named rather than to find out by its next `query`.
        siblings = ""
        if detaching_all and len(cleared) > 1:
            siblings = (f" This detached ALL {len(cleared)} conversations bound here "
                        f"({_summarize(cleared)}) — with no agent_session_id there is "
                        "nothing to tell them apart. Pass your own session id to "
                        "detach only this conversation.")
        remaining = len(attached) - len(cleared)
        if remaining > 0:
            siblings = (f" {remaining} other conversation(s) on this machine are still "
                        f"bound to {shared_id} and still feeding it.")
        return (f"Left Shared Session {shared_id}{where}. This conversation is no longer "
                f"bound to it and nothing more from here will reach {shared_id}. `query` "
                f"and `contribute` will say you are not joined until you join another."
                f"{siblings}{note}")

    @server.tool(description=(
        "CLOSE a Shared Session for everyone, permanently. Its memory stops "
        "accepting reads and writes for every member, not just you. Call only "
        "when the user explicitly asks to end the shared session — to stop your "
        "own conversation feeding it, use `leave_session` instead. "
        "Only the session's creator can end it, and this refuses while other "
        "contributors are still members, naming them. The result names the "
        "session it closed and the transcript it unbound."))
    async def end_session() -> str:
        binding = resolve_binding()
        if binding is None:
            return _NOT_JOINED
        shared_id = binding.shared_id
        # Same "every product bound here, not just the picked one" rule as
        # leave_session. It matters for the layer-3 refusal below: with two
        # products bound under two contributors, subtracting only
        # `binding.contributor` from `members` leaves this same human's OTHER
        # identity in `others`, and the tool refuses to end the session because
        # the user is still in it.
        mine = {b.contributor for _, b in _bindings_on(shared_id)} | {binding.contributor}
        try:
            async with _client() as client:
                # Layer 3 of the spec's three ("refuse when others are still
                # members"). Layer 1 is the harness permission prompt, layer 2
                # is creator-only in the SERVICE — a client-side check is not a
                # gate, so this one is a courtesy that stops the honest mistake,
                # not a security boundary.
                wm = await client.get(f"{base}/v1/sessions/{shared_id}/watermark",
                                      params={"contributor": binding.contributor})
                if is_session_ended(wm):
                    return _ended_text(_forget_ended(binding))
                if wm.status_code == 404:
                    return (f"Shared memory does not know session {shared_id} — it was "
                            "probably lost in a service restart. Nothing to end; "
                            "`leave_session` clears the local binding.")
                wm.raise_for_status()
                members = wm.json().get("members", [])
                others = sorted(m for m in members if m not in mine)
                if others:
                    # Deliberately NOT overridable from here. Nothing an agent
                    # can pass as an argument is evidence that a human agreed,
                    # and this is the one call that destroys everyone's memory
                    # at once. The override is the members leaving.
                    who_is = "is" if len(others) == 1 else "are"
                    return (f"Not ending {shared_id}: {', '.join(others)} {who_is} still "
                            "a member of it, and ending closes the memory for them too. "
                            "Ask them to call `leave_session` first, then try again. To "
                            "detach only yourself, call `leave_session`.")
                resp = await client.post(f"{base}/v1/sessions/{shared_id}/end",
                                         json={"ended_by": binding.contributor})
                if resp.status_code == 403:
                    # The service's message names the creator; pass it through
                    # rather than re-wording it to "forbidden", because the
                    # agent's next useful act is telling the user who to ask.
                    return (f"Refused: {_error_text(resp)}. The session is untouched. "
                            "To detach only this conversation, call `leave_session`.")
                resp.raise_for_status()
        except (_httpx.HTTPError, OSError) as exc:
            return (f"Shared memory is unreachable right now ({exc.__class__.__name__}) "
                    f"— {shared_id} was NOT ended and is unchanged.")
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("end_session: unreadable response (%s)", exc)
            return (f"Shared memory answered something `end_session` couldn't read — "
                    f"{shared_id} may or may not be closed; check with `query`.")
        except Exception as exc:                       # noqa: BLE001
            return _unexpected("end_session", exc)

        # Wrapped, and AFTER the close has already succeeded: `POST /end` is
        # done, the session is closed for everyone, and no filesystem problem
        # here can undo that — but an OSError escaping an MCP tool would hand
        # the agent a raw internal exception string about an operation that in
        # fact worked (the spec's "nothing may raise out of an MCP tool").
        try:
            cleared = _unbind(shared_id)
            # Recorded locally even though the closure is now in the service's
            # log: that log is in-memory (store.py) and a restart un-ends the
            # session, at which point `resync` would re-create it and push the
            # whole retained backlog back in. STOPGAP for the service-side log
            # persistence item — see `synapse_orchestrator.ended`. `relay
            # .note_ended` is the in-memory half of the same fact: without it
            # this very process keeps re-POSTing anything still queued under
            # `shared_id` on every tick, against a session it just closed.
            if state_dir is not None:
                record_ended(state_dir, shared_id)
            relay.note_ended(shared_id)
        except Exception as exc:                       # noqa: BLE001
            return (f"Shared Session {shared_id} is now ended for everyone, but the local "
                    f"cleanup afterwards failed. {_unexpected('end_session', exc)}")
        if cleared is None:
            return (f"Shared Session {shared_id} is now ended for everyone. Its memory is "
                    f"closed for reading and writing; the log is kept for audit only. The "
                    f"local binding could NOT be cleared: {_NO_STATE_DIR}")
        where = (" (was following "
                 + ", ".join(b.transcript_path for b in cleared) + ")") if cleared else ""
        return (f"Shared Session {shared_id} is now ended for everyone{where}. Its memory "
                "is closed for reading and writing; the log is kept for audit only. This "
                "conversation is unbound — `create_session` starts a fresh one.")
