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
from functools import partial
from pathlib import Path

import httpx
import uvicorn

from synapse_contracts import LocalBinding
from synapse_contracts.binding import read_binding
from synapse_orchestrator.app import build_app
from synapse_orchestrator.briefing import (
    DEFAULT_REFRESH_SECONDS,
    attach_briefing_refresher,
    build_briefing,
)
from synapse_orchestrator.ended import ended_session_ids, record_ended
from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import create_mcp, register_tools

logger = logging.getLogger(__name__)


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
    """
    bindings_dir = _bindings_dir(state_dir)
    if not bindings_dir.is_dir():
        return None
    found = [b for b in (read_binding(p) for p in sorted(bindings_dir.glob("*.json")))
             if b is not None]
    if not found:
        return None
    return max(found, key=lambda b: b.pinned_at).to_local_binding()


def _resolve_binding_for_agent(state_dir: Path, agent: str) -> LocalBinding | None:
    """The binding for exactly ONE Agent product (`bindings/<agent>.json`),
    read fresh from disk — never "most recently joined across every
    product" like `_resolve_binding` above.

    This is what lets the producer endpoint (app.py) route each incoming
    Finding to the Shared Session ITS OWN Agent product is joined to,
    rather than to whichever product happens to be the most recently
    joined overall. Added in round 3 review's fix pass: matching per-Finding
    on `attributions[0].agent` is what closes the two-products-joined-to-
    two-different-sessions leak that surviving the round 2 fix pass left
    open (round 2 preserved attribution content but still routed via
    `_resolve_binding`'s single pick).
    """
    binding = read_binding(_bindings_dir(state_dir) / f"{agent}.json")
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
        provider = NPUProvider(
            base_url=config.provider.base_url,
            model=config.model,
            max_tokens=config.provider.max_tokens,
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
    #    safe to call every time. The purpose is lost on a genuine recreate --
    #    the retained log does not carry it -- which is why it says so.
    async with httpx.AsyncClient(transport=transport, timeout=10.0) as client:
        for sid in known_sessions:
            try:
                await client.post(f"{base}/v1/sessions",
                                  json={"purpose": "(recovered by resync)",
                                        "created_by": "resync", "shared_id": sid})
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

    briefing = asyncio.run(build_briefing(binding, args.service_url, transport=transport))
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
    app = build_app(relay, server, resolve_binding_for_agent=resolve_binding_for_agent)
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
