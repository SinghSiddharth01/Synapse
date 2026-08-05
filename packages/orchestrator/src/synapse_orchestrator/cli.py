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
import logging
import sys
from pathlib import Path

import httpx
import uvicorn

from synapse_contracts import LocalBinding
from synapse_contracts.binding import read_binding
from synapse_orchestrator.app import build_app
from synapse_orchestrator.briefing import build_briefing
from synapse_orchestrator.relay import Relay
from synapse_orchestrator.server import create_mcp, register_tools


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
    """Same config, same pack, same model as synapse_worker.cli's run
    command — the "one distiller" property: contribute()'s round trip
    uses the identical NPU model and prompt pack as the passive path."""
    from synapse_distiller import Distiller, load_config, load_pack_by_name
    from synapse_providers import NPUProvider

    config = load_config()
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synapse-orchestrator", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--state-dir", default=".synapse")
    parser.add_argument("--service-url", default="http://127.0.0.1:8899")
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
    relay = Relay(state_dir / "relay", args.service_url, shared_id, transport=transport)
    total = relay.retained_count()
    pushed = await relay.resync()
    label = shared_id or "unbound"
    # total==0 means nothing was ever recorded — trivially "successful".
    # pushed < total means something WAS recorded but didn't fully make it
    # out — collapsing both into one number reported a failed resync
    # (service down, or the recorded session unreachable) as indistinguishable
    # from success.
    if total and pushed < total:
        print(f"resync: FAILED — {pushed} of {total} finding(s) re-pushed across the "
              f"retained log (current session: {label!r}); is the service reachable, "
              "and is a Shared Session joined (`synapse-worker join <shared_id>`)?")
        return 1
    print(f"resync: re-pushed {pushed} finding(s) (current session: {label!r})")
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
    relay = Relay(state_dir / "relay", args.service_url, shared_id, transport=transport)

    briefing = asyncio.run(build_briefing(binding, args.service_url, transport=transport))
    server = create_mcp(briefing)
    if binding is not None:
        register_tools(server, binding=binding, service_url=args.service_url, relay=relay,
                       distiller_factory=lambda: build_npu_distiller(binding),
                       transport=transport)
    app = build_app(relay, server, resolve_binding_for_agent=resolve_binding_for_agent)
    print(f"synapse-orchestrator on http://{args.host}:{args.port} "
          f"(mcp at /mcp, producer at /producer/findings, "
          f"session: {shared_id or 'unbound'})")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
