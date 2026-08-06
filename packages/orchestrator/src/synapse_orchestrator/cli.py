"""synapse-orchestrator — the MCP server a coding agent connects to.

    uv run synapse-orchestrator
    uv run synapse-orchestrator resync

One Starlette app serves both the MCP surface (`/mcp`) and the producer
endpoint (`/producer/findings`, Plan D.1) on a single process/port — ADR
0001's single-egress property. The `Relay` (Plan D.4) is the sole path
onward to the Synapse Service; `resync` re-pushes its entire retained log,
the recovery path for a service restart.

Session binding is `synapse-worker join <shared_id>`, not anything through
this server. See that command's help.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

import httpx
import uvicorn

from synapse_contracts import LocalBinding
from synapse_contracts.binding import read_binding
# The worker's per-session binding resolver, not a second implementation of
# it: `bindings/<agent>/<session>.json` has one writer (discovery._bind) and
# must have one reader, or the two halves silently disagree about which
# conversation is bound. Same one-way edge server.py's import comment records
# — the orchestrator may depend on the worker, never the reverse.
from synapse_worker.discovery import resolve_agent_binding
from synapse_orchestrator.app import build_app
from synapse_orchestrator.briefing import (
    DEFAULT_REFRESH_SECONDS,
    attach_briefing_refresher,
    compose_instructions,
)
from synapse_orchestrator.ended import ended_session_ids, record_ended
from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import create_mcp, register_tools
from synapse_orchestrator.session_meta import (UNKNOWN_PURPOSE, SessionMeta,
                                               retained_sessions)

logger = logging.getLogger(__name__)

# UNKNOWN_PURPOSE — the purpose `resync` recreates a session with when this
# machine has no record of what it was for. Unchanged wording from before
# session_meta existed, on purpose: it is still exactly what it says, and it is
# now reached only in the case it was always honest about — a session recreated
# from a retained log by a machine that never created or joined it through the
# lifecycle tools. It MOVED to session_meta.py (2026-08-06) because
# `record_session` has to recognise it coming back off the wire and must not
# import this module to do so; re-exported by this import so `cli.UNKNOWN_PURPOSE`
# still resolves for every existing reader.


def _bindings_dir(state_dir: Path) -> Path:
    return state_dir / "bindings"


def _resolve_binding(state_dir: Path) -> LocalBinding | None:
    """The orchestrator's current "primary" binding, read fresh from disk.

    One file per Agent PRODUCT (`bindings/claude-code.json`, `bindings/codex.json`
    — Plan D.2), never a single hardcoded path: `synapse_worker.discovery` writes
    one binding per product a user joins, and a Codex-only join must not be
    invisible to an orchestrator that only ever looked for `claude-code.json`.
    When more than one product is bound, the most recently joined one wins —
    this process serves one Shared Session context at a time for the
    surfaces that need exactly one "current" binding: the arrival briefing,
    the startup banner, and `query`/`contribute` (server.py), which speak
    for whichever conversation is actually attached to this MCP connection.

    Called fresh (not cached) by every caller that needs "the binding right
    now": a `synapse-worker join` run after this process started must take
    effect without a restart (see server.py's `register_tools` and its round
    3 amendment note).

    NOT what the producer endpoint uses to decide where an incoming Finding
    is routed — see `_resolve_binding_for_agent` below. Using this "most
    recently joined, across every product" pick to route Findings was round
    3 review's residual blocker: a Finding correctly attributed to codex
    still egressed to whatever session claude-code happened to be joined
    to, whenever both were joined at once.

    ⟨W2, 2026-08-06⟩ BOTH layouts are read: the legacy mirrors
    `bindings/<agent>.json` and the per-session `bindings/<agent>/<session>.json`
    files. Globbing only the top level was correct exactly while a bind always
    refreshed the mirror — and `leave_session(agent_session_id=…)` deliberately
    breaks that, deleting one conversation's binding and the mirror it happened
    to own while other conversations stay bound. Read one level only, this
    returned None right after a sibling's leave and every tool answered "not
    joined" to a conversation that was still joined and still feeding the
    session (reproduced, 2026-08-06 review).
    """
    bindings_dir = _bindings_dir(state_dir)
    if not bindings_dir.is_dir():
        return None
    paths = sorted(bindings_dir.glob("*.json")) + sorted(bindings_dir.glob("*/*.json"))
    found = [b for b in (read_binding(p) for p in paths) if b is not None]
    if not found:
        return None
    return max(found, key=lambda b: _pinned_at_key(b)).to_local_binding()


def _pinned_at_key(binding) -> datetime:
    """`pinned_at`, always comparable — mirrors
    `synapse_worker.discovery._pinned_at_key`. A hand-authored fixture may
    carry a naive datetime and Python raises TypeError rather than answering
    when a naive and an aware stamp are compared; everything this codebase
    writes is aware UTC, so a naive stamp is read as UTC."""
    return (binding.pinned_at if binding.pinned_at.tzinfo is not None
            else binding.pinned_at.replace(tzinfo=timezone.utc))


def _resolve_binding_for_agent(state_dir: Path, agent: str,
                               agent_session: str | None = None) -> LocalBinding | None:
    """The binding for exactly ONE Agent product, optionally for exactly ONE
    of its conversations, read fresh from disk — never "most recently joined
    across every product" like `_resolve_binding` above.

    This is what lets the producer endpoint (app.py) route each incoming
    Finding to the Shared Session ITS OWN Agent product is joined to,
    rather than to whichever product happens to be the most recently
    joined overall. Added in round 3 review's fix pass: matching per-Finding
    on `attributions[0].agent` is what closes the two-products-joined-to-
    two-different-sessions leak that surviving the round 2 fix pass left
    open (round 2 preserved attribution content but still routed via
    `_resolve_binding`'s single pick).

    ⟨W2, 2026-08-06⟩ `agent_session` closes the same leak one level down, on
    the axis W2 opened: two windows of ONE product can now be joined to two
    different Shared Sessions, and `attributions[0].agent` alone cannot tell
    them apart. Given, it selects that conversation's own binding; when that
    conversation has no binding of its own, the answer falls back to the
    product-level most-recent one, which is what this function always
    returned and keeps every pre-W2 producer routing exactly as it did.
    Reproduced before the fix: window A bound to sh-1, window B to sh-2, a
    Finding attributed to A's conversation egressed to sh-2 because the
    mirror named B.
    """
    binding = resolve_agent_binding(state_dir, agent, agent_session)
    if binding is None and agent_session is not None:
        binding = resolve_agent_binding(state_dir, agent)
    return binding.to_local_binding() if binding is not None else None


def build_npu_distiller(binding: LocalBinding):
    """Same config, same pack as synapse_worker.cli's run command — the "one
    distiller" property: contribute()'s round trip uses the identical prompt
    pack (and, on the default NPU arm, the identical model) as the passive
    path.

    SYNAPSE_DISTILLER selects the provider the same way worker/cli.py's
    build_distiller does: "npu" (default, unchanged), "anthropic" (Claude
    Opus 5 via the Messages API, one API key per developer), or "claude-cli"
    (Claude through the local `claude` binary on the developer's own
    subscription — no credential to distribute at all). The last two exist so
    several people can run the full loop at once instead of queueing for the
    single NPU box. Unset, this behaves exactly as before.
    """
    from synapse_distiller import Distiller, load_config, load_pack_by_name
    from synapse_providers import NPUProvider

    config = load_config()
    arm = os.environ.get("SYNAPSE_DISTILLER", "npu")
    if arm == "anthropic":
        from synapse_providers import AnthropicProvider

        provider = AnthropicProvider()
    elif arm == "claude-cli":
        # No credential at all: Claude through the local `claude` binary on the
        # developer's own subscription. Lets several people exercise the full
        # loop at once with no key to distribute and no NPU box to queue for.
        from synapse_providers import ClaudeCliProvider

        provider = ClaudeCliProvider(max_tokens=config.provider.max_tokens)
    else:
        # Capped at the capability record's response_reserve, which is the
        # output room the segment budget was derived by subtracting. Asking for
        # more overruns the context on a full-budget segment. Clamped in BOTH
        # places that build this provider -- the "one distiller" property above
        # is about the pack and the model, and it would be quietly untrue of
        # max_tokens if contribute() asked for more than the passive path.
        max_tokens = config.effective_max_tokens
        provider = NPUProvider(
            base_url=config.provider.base_url,
            model=config.model,
            max_tokens=max_tokens,
            temperature=config.provider.temperature,
            timeout=config.provider.timeout_s,
        )
    return Distiller(
        provider,
        binding,
        load_pack_by_name(config.prompt_pack_name),
        config.distil_kinds,
        config.render_style,
    )


def _default_contributor() -> str | None:
    """The OS login name, or None on a host that has no answer.

    Resolved HERE rather than as an argparse default (see `--contributor`
    below) so a host where `getpass.getuser()` raises still runs every
    subcommand. None is a usable outcome: `create_session`/`join_session`
    already return prose telling the user to pass `--contributor` when the
    identity is unset, which is a far better failure than a traceback out of
    argument parsing on a command that never needed an identity.
    """
    try:
        return getpass.getuser()
    except (OSError, KeyError):
        # KeyError is what the pwd lookup raises on some platforms; OSError is
        # what CPython documents. Neither is worth crashing over.
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synapse-orchestrator", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--state-dir", default=".synapse")
    parser.add_argument("--service-url", default="http://127.0.0.1:8899")
    # Who the lifecycle MCP tools act AS when nothing is bound yet — i.e. for
    # `create_session` and `join_session`, the two calls made from exactly that
    # state. Once a binding exists, `binding.contributor` wins (server.py's
    # `_identity`), so this never overrides an identity the service has already
    # seen. Default: SYNAPSE_CONTRIBUTOR, else the OS login name. Rejected
    # alternative: mirroring `synapse-worker join`'s hardcoded demo default
    # ("aditya") — a create_session that silently attributes a new Shared
    # Session to a teammate makes `end_session`'s creator-only check refuse the
    # person who actually created it, with no clue why.
    #
    # The default is the ENV VAR ALONE and the OS login name is resolved
    # lazily in `main` (see `_default_contributor`). `build_parser()` runs for
    # every invocation including ones that never read this value, and
    # `getpass.getuser()` RAISES on a host with no passwd entry for the uid and
    # no USER/LOGNAME/LNAME/USERNAME set -- a plain `docker run -u 1001 ...`.
    # Evaluated here, that killed `synapse-orchestrator resync` (which never
    # touches `args.contributor`) with an unhandled OSError before parsing even
    # finished.
    parser.add_argument("--contributor",
                        default=os.environ.get("SYNAPSE_CONTRIBUTOR"),
                        help="Contributor identity for create_session/join_session "
                             "when no binding exists yet (default: "
                             "$SYNAPSE_CONTRIBUTOR, else the OS login name)")
    parser.add_argument("--briefing-refresh", type=float,
                        default=DEFAULT_REFRESH_SECONDS, metavar="SECONDS",
                        help="how often to recompose the arrival briefing so "
                             "agents connecting later see current numbers "
                             "(0 disables)")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command")
    resync = sub.add_parser(
        "resync", help="re-push the Relay's entire retained log to the service"
    )
    # Mirrors the worker's argparse pattern (synapse_worker.cli: each
    # subcommand declares its own options) — but unlike the worker, this
    # program also has a no-subcommand default action (serve) that shares
    # these same two flags, so `--state-dir`/`--service-url` must work BOTH
    # before and after `resync`. default=SUPPRESS is what makes both orders
    # work: when the flag is omitted after `resync`, argparse's subparser
    # leaves the namespace attribute alone (already set — from the flag or
    # from the top-level default) rather than stomping it with a second
    # default. Without SUPPRESS, `--state-dir X resync` silently reverts to
    # the top-level default the moment a subcommand is present.
    resync.add_argument("--state-dir", default=argparse.SUPPRESS)
    resync.add_argument("--service-url", default=argparse.SUPPRESS)

    return parser


def _warn_if_identity_was_not_taken(shared_id: str, meta: SessionMeta, resp) -> None:
    """Say so when this machine KNEW the session's identity and the service
    kept a different one (2026-08-06 review).

    `store.create_session` is create-or-return, which is exactly what makes the
    recreate above safe to issue unconditionally — and it also means the send
    is a no-op whenever the session is already there. So if another machine
    resynced first with no record, sh-X is already back OWNERLESS and this
    machine's correct `created_by` is silently discarded, with no code path
    left that can restore it: the session falls to the membership gate for the
    rest of its life even though somebody knew the owner the whole time.

    Reported, not repaired. A service-side "null may be upgraded to a name"
    rule would fix it outright and is refused: with no auth anywhere in
    Synapse, that is a hijack primitive — any caller could claim an ownerless
    session and lock the team out of ending it, which is strictly worse than
    the membership fallback it would replace. An operator who sees this line
    can end the session by joining it, which is the sanctioned remedy.

    Never raises and never blocks the resync: this is a note about ownership,
    and the findings are what the command is actually for. `logger.warning`
    rather than `print` deliberately — the printed lines are this command's
    result contract (`resync: ...`), read by eyes and by CI.
    """
    try:
        body = resp.json()
    except ValueError:
        return
    if not isinstance(body, dict):
        return
    served = body.get("created_by")
    if meta.created_by is not None and served != meta.created_by:
        logger.warning(
            "Resync recreated %s but the service reports created_by=%r, not the %r "
            "this machine has on record — another orchestrator recreated it first "
            "with a different (or no) record of the owner. The session cannot be "
            "re-owned; whoever needs to end it must be a current member of it.",
            shared_id, served, meta.created_by)


async def cmd_resync(args: argparse.Namespace, *,
                     transport: httpx.AsyncBaseTransport | None = None) -> int:
    """`resync()` itself is a bare int — the plan's documented signature,
    restored post-review (relay.py's module docstring). The distinction
    between "nothing to push" and "push failed" that a prior pass had baked
    into `resync()`'s return type instead comes from comparing it against
    `retained_count()` — the count of everything in the log that COULD be
    pushed (i.e. was ever recorded under a real Shared Session, across every
    session in the backlog, not just the one currently bound)."""
    state_dir = Path(args.state_dir)
    binding = _resolve_binding(state_dir)
    shared_id = binding.shared_id if binding is not None else None
    # STOPGAP (lifecycle spec, "Durability caveat"): the service's store is
    # in-memory, so a restart un-ends an ended session — and Step 1 below is
    # create-or-return, which would cheerfully bring it back and refill it with
    # the entire retained log. Until service-side log persistence lands
    # (STATE.md's first post-demo entry), the locally retained set is the only
    # thing that remembers the team closed it. Seeded into the Relay too, so
    # the push loop skips those groups as well as the recreate loop here.
    ended = ended_session_ids(state_dir)
    relay = Relay(state_dir / "relay", args.service_url, shared_id, transport=transport,
                  ended_sessions=ended,
                  on_session_ended=partial(record_ended, state_dir))
    total = relay.retained_count()
    recorded = relay.recorded_session_ids()
    known_sessions = sorted(recorded - ended)
    skipped = sorted(recorded & ended)
    base = args.service_url.rstrip("/")

    # 1. RECREATE, before the push. After a real restart the sh-... does not
    #    exist, so the push 404s. `POST /v1/sessions` with a known id is
    #    create-or-return: it returns a live session UNCHANGED, so this is
    #    safe to call every time.
    #
    #    WHAT WE SEND AS THE SESSION'S IDENTITY (corrected 2026-08-06). This
    #    used to post `{"purpose": "(recovered by resync)", "created_by":
    #    "resync"}` because the retained log carries findings and nothing about
    #    the session. Against a live service that is inert -- create-or-return
    #    hands back the existing session unchanged -- so the only run in which
    #    it did anything was the one it was written for: after a genuine
    #    restart the store is empty, that POST is a real CREATE, and the
    #    session's creator BECAME the string "resync". `api.end_session` is
    #    creator-only, so the human who actually created the session was then
    #    refused ("only resync can end this session") while anyone who had read
    #    this file could close it by sending `{"ended_by": "resync"}`.
    #
    #    So we no longer invent one. `synapse_orchestrator.session_meta` retains
    #    `shared_id -> {created_by, purpose}` for every session THIS machine
    #    created or joined, and that record is what goes back on the wire -- the
    #    purpose included, so it is no longer lost on a recreate of a session
    #    this machine knows. When there is no record, `created_by` is None: an
    #    honest unknown the service answers with a membership check, never a
    #    sentinel string that locks the session against its owner. `purpose`
    #    keeps a placeholder because the contract still requires a string, and
    #    an unknown purpose costs a label rather than a permission.
    #
    #    STOPGAP, same as `ended.json` above and for the same underlying reason
    #    (the SERVICE's log is in-memory); another machine's resync knows
    #    nothing about this file. See session_meta.py.
    metadata = retained_sessions(state_dir)
    async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
        for sid in known_sessions:
            meta = metadata.get(sid, SessionMeta())
            try:
                resp = await client.post(
                    f"{base}/v1/sessions",
                    json={"purpose": (meta.purpose if meta.purpose is not None
                                      else UNKNOWN_PURPOSE),
                          "created_by": meta.created_by, "shared_id": sid})
                _warn_if_identity_was_not_taken(sid, meta, resp)
            except (httpx.HTTPError, OSError):
                pass          # the push below reports the real failure, loudly

    pushed_by_session = await relay.resync_sessions()
    pushed = sum(pushed_by_session.values())

    # RECOMPUTE THE DENOMINATOR, after the push and before comparing against
    # it. `total` above was taken while `relay._ended` held only what
    # `ended.json` already knew, so a session this machine had NOT yet observed
    # to be closed -- ended by a teammate, ended by the service directly, or
    # ended while this orchestrator was down -- was still counted in it. The
    # push then learns the truth from a live `409 {"error": "session_ended"}`,
    # correctly drops those findings and correctly does NOT count them as
    # pushed, and the comparison below fired "FAILED — 0 of 3 re-pushed … is
    # the service reachable, and is a Shared Session joined?" with exit code 1
    # for a resync that behaved exactly right, sending an operator to debug
    # connectivity and bindings that were both fine. Running the identical
    # command a second time then succeeded, because by then `ended.json` named
    # it -- an asymmetry with no defensible meaning, and one that would fail
    # any CI step gating on resync's status.
    #
    # `retained_count()` reads the relay's OWN `_ended`, which
    # `resync_sessions` has just grown, so re-reading it here is the whole fix;
    # `skipped` and `known_sessions` are recomputed off the same post-run set
    # so the reported counts and the "skipped N ended session(s)" note describe
    # the same run rather than two different instants.
    ended = ended | set(relay.ended_session_ids())
    total = relay.retained_count()
    skipped = sorted(recorded & ended)
    known_sessions = sorted(recorded - ended)

    label = shared_id or "unbound"
    # The loud-failure branch stays exactly where main has it and fires BEFORE
    # any re-synthesis: there is nothing to re-synthesize if the findings did
    # not land, and `test_resync_fails_loudly_when_the_push_does_not_succeed`
    # (test_cli.py:343) reaches it through a `down` transport -- the recreate
    # POST above raises and is swallowed, resync() returns 0, and this fires.
    if total and pushed < total:
        print(f"resync: FAILED — {pushed} of {total} finding(s) re-pushed across the "
              f"retained log (current session: {label!r}); is the service reachable, "
              "and is a Shared Session joined (`synapse-worker join <shared_id>`)?")
        return 1

    # 2. SYNTHESIZE. push_findings gates the model on accepted > 0, so a resync
    #    into a store that already holds these findings never re-synthesizes,
    #    and the recovery returns findings with no Working Memory, no conflicts
    #    and no merges. Per session in the BACKLOG, not per binding.
    #
    #    COST, named: this is the SECOND model call per session. The push's own
    #    merge already ran over the whole re-pushed batch; this one runs over at
    #    most CANDIDATE_WINDOW (20) retrievable findings and REWRITES
    #    working_memory from those 20. On a large session the re-derived prose
    #    is therefore a summary of the last twenty, not of everything -- which
    #    is what "recomputed, not restored" means concretely, and is why the
    #    docs say so rather than leaving it for the demo to reveal.
    synthesized: list[str] = []
    async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
        for sid in sorted(pushed_by_session):         # only the ones that converged
            try:
                resp = await client.post(f"{base}/v1/sessions/{sid}/synthesize")
                resp.raise_for_status()
                if resp.json().get("synthesized"):
                    synthesized.append(sid)
            except (httpx.HTTPError, OSError, ValueError) as exc:
                logger.warning("Resync pushed to %s but re-synthesis failed (%s)",
                               sid, exc.__class__.__name__)

    ended_note = f"; skipped {len(skipped)} ended session(s): {skipped}" if skipped else ""
    print(f"resync: re-pushed {pushed} finding(s) across {len(known_sessions)} session(s) "
          f"(current session: {label!r}; synthesized: {synthesized}){ended_note}")
    return 0


def main(argv: list[str] | None = None, *,
         transport: httpx.AsyncBaseTransport | None = None) -> int:
    # argv=None reads sys.argv, matching the old parser.parse_args() call
    # exactly — real invocation is unchanged. Tests pass an explicit list.
    # `transport` is likewise test-only: it never comes from argv, and is
    # threaded into every httpx call this CLI makes (Relay, build_briefing,
    # register_tools) so tests never open a real socket to the default
    # --service-url — see test_cli.py.
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if getattr(args, "command", None) == "resync":
        return asyncio.run(cmd_resync(args, transport=transport))

    state_dir = Path(args.state_dir)

    def resolve_binding() -> LocalBinding | None:
        return _resolve_binding(state_dir)

    def resolve_binding_for_agent(agent: str) -> LocalBinding | None:
        return _resolve_binding_for_agent(state_dir, agent)

    def resolve_binding_for_session(agent: str,
                                    agent_session: str | None) -> LocalBinding | None:
        return _resolve_binding_for_agent(state_dir, agent, agent_session)

    binding = resolve_binding()
    shared_id = binding.shared_id if binding is not None else None
    # Constructed with `shared_id` possibly None rather than the string
    # "unbound" — Relay refuses to egress when unbound instead of inventing a
    # session id to post real Findings to (see relay.py). The producer
    # endpoint additionally re-resolves per Finding via
    # `resolve_binding_for_agent` below, so a `join` run after this process
    # started is picked up without a restart on that path.
    relay = Relay(state_dir / "relay", args.service_url, shared_id, transport=transport,
                  # Seeded from disk and written back through the callback: a
                  # session that ended while this process was down must not be
                  # re-POSTed on every tick, and one that ends while it is up
                  # must survive into the next `resync`. See relay.py's
                  # termination note and `synapse_orchestrator.ended`.
                  ended_sessions=ended_session_ids(state_dir),
                  on_session_ended=partial(record_ended, state_dir))

    # `compose_instructions`, not `build_briefing` (finding #1): on the path
    # docs/JOIN.md documents, this string is the ONLY thing the joining agent is
    # handed — serve_local.py writes the binding itself, so `join_session` (the
    # other place the arrival body is delivered) is never called at all.
    briefing = asyncio.run(compose_instructions(binding, args.service_url,
                                                transport=transport))
    server = create_mcp(briefing)
    # Registered UNCONDITIONALLY, even when nothing is joined yet — round 3
    # review's fix for the tools-frozen-at-boot blocker. `resolve_binding` is
    # called fresh inside every `query`/`contribute` invocation (server.py),
    # so a `synapse-worker join` run after this process started takes effect
    # on the very next tool call, no restart, same MCP session — matching
    # what the producer endpoint already did. `build_npu_distiller` already
    # takes a `LocalBinding` positionally, so it IS the per-call factory
    # `register_tools` expects — no wrapping lambda needed.
    #
    # The lifecycle tools (2026-08-06) need three more things, all of them
    # process-level facts rather than per-call arguments: `state_dir` so a bind
    # lands in the SAME `bindings/` directory `synapse-worker join` writes to
    # and `_resolve_binding` above reads from; `cwd` as the scope for transcript
    # detection when a caller does not name its own session id; and
    # `contributor` as the identity to create/join AS while no binding exists
    # to read one from.
    register_tools(server, resolve_binding=resolve_binding, service_url=args.service_url,
                   relay=relay, distiller_factory=build_npu_distiller, transport=transport,
                   state_dir=state_dir, cwd=Path.cwd(),
                   contributor=args.contributor or _default_contributor())
    app = build_app(relay, server,
                    resolve_binding_for_agent=resolve_binding_for_agent,
                    # Wins over the per-agent resolver above: since W2 one
                    # Agent product can hold several conversations bound to
                    # different Shared Sessions, and only the Finding's own
                    # `agent_session` picks between them (app.py).
                    resolve_binding_for_session=resolve_binding_for_session)
    # The briefing above is a snapshot of this instant. Keep it true for
    # agents that arrive later, and for a `join` that happens after this
    # process started — see briefing.py's "Keeping the briefing TRUE after
    # boot". Hung off the app lifespan, so it lives exactly as long as the
    # server does.
    attach_briefing_refresher(app, server, resolve_binding, args.service_url,
                              interval=args.briefing_refresh, transport=transport)
    print(f"synapse-orchestrator on http://{args.host}:{args.port} "
          f"(mcp at /mcp, producer at /producer/findings, "
          f"session: {shared_id or 'unbound'})")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
