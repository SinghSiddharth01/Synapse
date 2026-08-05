"""synapse-worker — follow a live agent conversation and condense it periodically.

    synapse-worker join <shared_id>                 # bind, once per session — see below
    geniex serve                                    # terminal 1
    uv run synapse-worker run                       # terminal 2
    uv run synapse-worker run --interval 15 --ticks 4
    uv run synapse-worker status
    uv run synapse-worker replay                    # drain the write-ahead log

`join` is Plan A.7 / Plan D.2's `synapse join <shared_id>`, run from a terminal
— never from inside the agent conversation. Plan D Task D.3 is explicit that
there is no `attach(shared_id)` surfaced to the agent: "the agent never needs
to be told which Shared Session it is in." `join` binds whatever Agent Session
detection currently finds live, the same heuristic `run` falls back to when
nothing has been joined. It does not let you hand-pick a specific transcript
file — two windows of the same agent product open at once is a documented
ambiguity (Plan D, "one active Agent Session per Agent product per machine"),
not something this CLI tries to resolve for you.

`run` attaches at the END of the transcript by default. Pass --from-start only
deliberately: a live transcript is routinely several megabytes, and re-reading
one at ~13 tok/s is hours of NPU time.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from synapse_contracts import LocalBinding, Segment, read_binding
from synapse_distiller import (
    Distiller,
    PromptDropError,
    check_canary,
    load_config,
    load_pack_by_name,
)
from synapse_providers import CallLog, NPUProvider, RecordingProvider

from synapse_worker.debug_server import DebugServer
from synapse_worker.discovery import (
    AGENT_REGISTRY,
    ResolvedTranscript,
    binding_path_for_agent,
    find_claude_code_transcripts,
    join_session,
    resolve_transcript,
)
from synapse_worker.loop import WorkerLoop
from synapse_worker.producer import FileSink, HttpSink, Producer
from synapse_worker.stats import StatsBuffer
from synapse_worker.triage_log import TriageLog

logger = logging.getLogger(__name__)

DEFAULT_AGENT = "claude-code"  # `_build`'s fallback for an explicit --transcript
# given WITHOUT --agent, whose dialect nothing infers; `replay --skipped`
# also still only ever looks at the claude-code binding -- a narrower,
# still-disclosed gap `run`'s multi-agent resolution
# (_resolve_agent_and_transcript) does not share, since `run` is the half
# that had to reach CodexSource at all.
DEFAULT_DEBUG_PORT = 8790


def build_distiller(config, binding: LocalBinding) -> Distiller:
    """One construction path for a config-driven Distiller.

    Shared by `cmd_run` (via `_build`) and `cmd_replay --skipped` (Task 3) so
    there is exactly one place that turns a `SynapseConfig` into a live
    NPU-backed `Distiller` — the binding is the only thing that varies.
    """
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


def _resolve_debug_port(args: argparse.Namespace) -> int:
    """--debug-port wins; else SYNAPSE_DEBUG_PORT; else the default. 0 disables
    the dashboard entirely -- it must not fall back to the default, so this
    is `is not None`, not `or` (0 is falsy and `or` would silently discard it).
    """
    if getattr(args, "debug_port", None) is not None:
        return args.debug_port
    env = os.environ.get("SYNAPSE_DEBUG_PORT")
    if env is not None:
        return int(env)
    return DEFAULT_DEBUG_PORT


def _resolve_agent_and_transcript(
    args: argparse.Namespace, state_dir: Path
) -> tuple[str | None, ResolvedTranscript | None]:
    """Which Agent product's transcript to follow, and which registered
    Source it therefore needs.

    Without this, `resolve_transcript` was always called with its
    `agent="claude-code"` default, so `run` could never reach a hand-authored
    `bindings/codex.json` even after `WorkerLoop` learned to read
    `AGENT_REGISTRY` -- nothing upstream ever asked for anything but
    Claude Code. An explicit `--agent` wins outright; otherwise every
    registered agent is tried, in `AGENT_REGISTRY` order (Claude Code first,
    preserving today's behavior when it is the only agent in play): a
    `join`-pinned binding for ANY agent wins over every agent's heuristic,
    and the heuristic is only consulted once nothing anywhere is pinned.
    """
    requested = getattr(args, "agent", None)
    candidates = [requested] if requested else list(AGENT_REGISTRY)

    first_heuristic: tuple[str, ResolvedTranscript] | None = None
    for agent in candidates:
        resolved = resolve_transcript(Path.cwd(), state_dir, agent=agent)
        if resolved is None:
            continue
        if resolved.source == "pinned":
            return agent, resolved
        if first_heuristic is None:
            first_heuristic = (agent, resolved)
    return first_heuristic if first_heuristic is not None else (None, None)


def _build(args: argparse.Namespace, debug_port: int = 0):
    config = load_config()
    worker_cfg = config.worker
    state_dir = Path(worker_cfg.state_dir)

    resolved = None
    if getattr(args, "transcript", None):
        transcript = Path(args.transcript)
        # --agent still selects the Source even with an explicit --transcript
        # -- without this, `--agent codex --transcript <rollout>` silently
        # parsed a Codex file with ClaudeCodeSource (every line skipped as
        # bookkeeping, zero events, no error) because this branch used to
        # hard-set DEFAULT_AGENT and never looked at args.agent at all. Only
        # when --agent is itself omitted does the dialect stay unguessed.
        agent = getattr(args, "agent", None) or DEFAULT_AGENT
    else:
        agent, resolved = _resolve_agent_and_transcript(args, state_dir)
        if resolved is None:
            _no_transcript()
        transcript = resolved.path

    sink = (
        HttpSink(worker_cfg.upstream_url)
        if worker_cfg.sink == "http"
        else FileSink(Path(worker_cfg.sink_file))
    )
    producer = Producer(state_dir / "wal", sink)

    # A joined binding carries its own shared_id/contributor/agent. --shared-id
    # and --contributor only apply when nothing was joined, since typing them
    # fresh on every invocation is exactly the un-joined state `join` replaces.
    if resolved is not None and resolved.local_binding is not None:
        binding = resolved.local_binding
    else:
        binding = LocalBinding(
            agent_session_id=resolved.agent_session_id if resolved else transcript.stem,
            shared_id=args.shared_id,
            contributor=args.contributor,
            agent=agent,
        )
    distiller = build_distiller(config, binding)

    # Debug instrumentation only exists when the dashboard is enabled -- an
    # untouched provider stays untouched, and RecordingProvider is transparent
    # (same result, exceptions re-raised) so this changes nothing else about
    # what the distiller does.
    stats: StatsBuffer | None = None
    if debug_port:
        call_log = CallLog()
        stats = StatsBuffer(call_log)
        distiller.provider = RecordingProvider(distiller.provider, "distiller", call_log)

    loop = WorkerLoop(
        transcript=transcript,
        distiller=distiller,
        producer=producer,
        binding=binding,
        state_dir=state_dir,
        budget_tokens=config.segment_budget,
        idle_flush_seconds=worker_cfg.idle_flush_seconds,
        stats=stats,
        # binding.agent is authoritative regardless of which branch above set
        # it -- a pinned binding's own `.agent`, the detected `agent` from
        # heuristic resolution, or DEFAULT_AGENT for an explicit --transcript
        # path whose dialect nothing here inferred.
        agent=binding.agent,
    )
    source = resolved.source if resolved is not None else "explicit --transcript"
    return config, loop, transcript, producer, source, stats


def _no_transcript():
    print(
        "No live agent transcript found for this directory.\n"
        "Start a coding-agent session here, run `synapse-worker join <shared_id>`, "
        "or pass --transcript <path> directly.",
        file=sys.stderr,
    )
    raise SystemExit(2)


async def cmd_join(args: argparse.Namespace) -> int:
    config = load_config()
    state_dir = Path(config.worker.state_dir)

    bindings = join_session(args.shared_id, args.contributor, Path.cwd(), state_dir)

    if not bindings:
        print(
            "No live Agent Session detected for this directory — nothing bound.\n"
            "Start writing a prompt in a coding-agent session here and try again.",
            file=sys.stderr,
        )
        return 1

    print(f"Detected agents: {', '.join(b.agent for b in bindings)}")
    for binding in bindings:
        print(f"  bound {binding.agent_session_id} -> {binding.shared_id!r} "
              f"(contributor={binding.contributor!r})")
    print(
        "\nContributor registration with the Synapse Service was skipped — "
        "no service exists yet to register with."
    )
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    debug_port = _resolve_debug_port(args)
    config, loop, transcript, _, source, stats = _build(args, debug_port)
    interval = args.interval or config.worker.poll_interval_seconds

    print(config.describe())
    print(f"transcript       {transcript}")
    if source == "heuristic":
        print("selection        HEURISTIC — most recently written transcript in "
              "this directory. Run `synapse-worker join <shared_id>` to bind "
              "exactly one instead.")
    else:
        print(f"selection        {source}")
    print(f"poll interval    {interval}s")
    print(f"idle flush       {config.worker.idle_flush_seconds}s")
    print(f"sink             {config.worker.sink}")
    print(f"state            {config.worker.state_dir}")

    debug_server: DebugServer | None = None
    if debug_port:
        debug_server = DebugServer(stats, debug_port, transcript=str(transcript))
        # The dashboard is optional instrumentation; the transcript work is
        # not. Binding can fail with a plain OSError -- e.g. two `run`s on
        # the same machine racing for the default 8790, or a stale worker
        # still holding it (the multi-agent case `resolve_binding_for_agent`
        # exists for is real) -- and that must not abort the core command.
        try:
            bound_port = debug_server.start()
        except OSError as exc:
            print(f"debug            disabled -- failed to bind port {debug_port}: {exc}\n",
                  file=sys.stderr)
            debug_server = None
        else:
            print(f"debug            http://127.0.0.1:{bound_port}/debug\n")
    else:
        print("debug            disabled (--debug-port 0)\n")

    # The prompt-drop guard, once, before any transcript content is processed.
    # A model that has stopped reading its prompt would otherwise write invented
    # findings straight into the write-ahead log.
    canary = await check_canary(loop.distiller.provider)
    if not canary:
        print(f"CANARY FAILED: {canary.detail}", file=sys.stderr)
        print("Refusing to distil — findings from this model would be invented.",
              file=sys.stderr)
        if debug_server is not None:
            debug_server.stop()
        return 1
    print(f"canary           ok (prompt_tokens={canary.input_tokens})\n")

    if args.from_start:
        print("Starting from the beginning of the transcript.\n")
    elif config.worker.attach_at_end:
        loop.attach_at_end()
        print("Attached at the end; only new conversation will be condensed.\n")

    try:
        await loop.run(interval_seconds=interval, max_ticks=args.ticks)
    except KeyboardInterrupt:
        pass
    result = await loop.shutdown()
    print(f"\nshutdown — {result.summary()}")
    if debug_server is not None:
        debug_server.stop()
    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    config = load_config()
    print(config.describe())
    print(f"\npoll interval    {config.worker.poll_interval_seconds}s")

    resolved = resolve_transcript(Path.cwd(), Path(config.worker.state_dir))
    print()
    if resolved is not None and resolved.source == "pinned":
        binding = resolved.local_binding
        print(f"joined session   shared_id={binding.shared_id!r} "
              f"agent_session_id={binding.agent_session_id} "
              f"transcript={resolved.path} (exists)")
    else:
        binding_path = binding_path_for_agent(Path(config.worker.state_dir), DEFAULT_AGENT)
        print(f"joined session   none (checked {binding_path}) — run "
              f"`synapse-worker join <shared_id>`, or the worker falls back to "
              f"the most-recently-active-transcript heuristic")

    transcripts = find_claude_code_transcripts(Path.cwd())
    print(f"\ntranscripts for {Path.cwd()}:")
    if not transcripts:
        print("  none found")
    for t in transcripts:
        live = "LIVE" if t.age_seconds <= 1800 else "idle"
        print(f"  [{live}] {t.path.name}  {t.size/1e6:.1f} MB  "
              f"{t.age_seconds/60:.0f} min since last write")

    state_dir = Path(config.worker.state_dir)
    producer = Producer(state_dir / "wal", FileSink(Path(config.worker.sink_file)))
    pending = producer.unsent()
    print(f"\nwrite-ahead log  {producer.findings_path}")
    print(f"unsent findings  {len(pending)}")
    return 0


async def cmd_replay(args: argparse.Namespace) -> int:
    """Drain anything the sink rejected earlier. Idempotent by Finding.id.

    `--skipped` re-distils every segment triage skipped and archives the skip
    log — a wrong triage skip is recoverable, not permanent loss.
    """
    config = load_config()
    state_dir = Path(config.worker.state_dir)
    sink = (
        HttpSink(config.worker.upstream_url)
        if config.worker.sink == "http"
        else FileSink(Path(config.worker.sink_file))
    )
    producer = Producer(state_dir / "wal", sink)

    if args.skipped:
        log = TriageLog(state_dir)
        skipped = log.load_skipped()
        if not skipped:
            print("No triage-skipped segments to replay.")
            return 0

        # Unlike `run`/`_build`, there is no un-joined fallback here: a
        # triage-skipped segment's own Attribution.agent_session already
        # round-trips through the skip log (see the grouping below), and the
        # only missing piece is contributor/shared_id. `run`'s --contributor/
        # --shared-id defaults exist because a first run before any `join`
        # is a normal flow; replaying OLD skips with today's CLI defaults
        # is not the same thing -- it would silently attribute (and route)
        # recovered findings to whatever "aditya"/"local-dev" happen to
        # default to right now, which may not be who or what was active
        # when the segment was actually skipped. Refuse rather than invent.
        joined = read_binding(binding_path_for_agent(state_dir, DEFAULT_AGENT))
        if joined is None:
            print(
                "No joined Shared Session -- refusing to replay skipped segments, "
                "since Attribution (contributor/shared_id) would have to be "
                "invented rather than read from a real binding.\n"
                "Run `synapse-worker join <shared_id>` first, then retry "
                "`synapse-worker replay --skipped`.",
                file=sys.stderr,
            )
            return 1
        contributor, shared_id, agent = joined.contributor, joined.shared_id, joined.agent

        # Group by the segment's OWN agent_session_id — it round-trips through
        # the skip log exactly (TriageLog serializes the whole Segment), so
        # replay must not overwrite it with a sentinel: Attribution.agent_session
        # is what awareness suppression keys on, and a finding stamped with a
        # value that matches no real Agent Session can never be suppressed for
        # the agent that produced it. One Distiller per group, since Attribution
        # is stamped from the binding at construction time.
        groups: dict[str, list[tuple[Segment, str]]] = {}
        for segment, reason in skipped:
            groups.setdefault(segment.agent_session_id, []).append((segment, reason))

        distillers = {
            agent_session_id: build_distiller(
                config,
                LocalBinding(
                    agent_session_id=agent_session_id,
                    shared_id=shared_id,
                    contributor=contributor,
                    agent=agent,
                ),
            )
            for agent_session_id in groups
        }

        # The prompt-drop guard, once, before any segment is distilled — the
        # same refusal `cmd_run` applies and for the same reason: a model that
        # has stopped reading its prompt would otherwise write invented
        # findings straight into the write-ahead log. Every group shares one
        # NPU endpoint, so one check covers the whole batch.
        canary = await check_canary(next(iter(distillers.values())).provider)
        if not canary:
            print(f"CANARY FAILED: {canary.detail}", file=sys.stderr)
            print("Refusing to distil — findings from this model would be invented.",
                  file=sys.stderr)
            return 1

        replayed = 0
        failed: list[tuple[Segment, str]] = []
        for agent_session_id, group in groups.items():
            distiller = distillers[agent_session_id]
            for segment, reason in group:
                try:
                    findings, stats = await distiller.distil(segment)
                except PromptDropError as exc:
                    logger.error(
                        "Prompt-drop guard tripped replaying %s: %s", segment.id, exc
                    )
                    failed.append((segment, reason))
                    continue
                except Exception:  # noqa: BLE001 - one bad segment must not sink the batch
                    logger.exception("Replay distillation failed for %s", segment.id)
                    failed.append((segment, reason))
                    continue
                if not stats.skipped_empty and findings:
                    # Write-ahead, same as tick(): on disk before any send.
                    producer.record(findings)
                    replayed += len(findings)

        sent, pending = await producer.flush()
        archived = log.archive()
        if failed:
            # Idempotent retry: only what failed goes back to the (now fresh,
            # post-archive) skip log, so re-running `replay --skipped` does not
            # re-distil — and re-count — segments that already succeeded.
            for segment, reason in failed:
                log.record_skip(segment, reason)

        succeeded = len(skipped) - len(failed)
        message = (
            f"Replayed {succeeded} skipped segments -> {replayed} findings "
            f"({sent} sent, {pending} queued)."
        )
        if failed:
            message += f" {len(failed)} segment(s) failed and were re-queued."
        if archived is not None:
            message += f" Log archived to {archived.name}."
        print(message)
        return 0 if not failed else 1

    sent, still_pending = await producer.flush()
    print(f"sent {sent}, still pending {still_pending}")
    return 0 if still_pending == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synapse-worker", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    join = sub.add_parser(
        "join", help="bind the currently-detected Agent Session(s) to a Shared Session"
    )
    join.add_argument("shared_id")
    join.add_argument("--contributor", default="aditya")
    join.set_defaults(func=cmd_join)

    run = sub.add_parser("run", help="follow and condense periodically")
    run.add_argument("--transcript", help="path to a transcript (default: auto-detect)")
    run.add_argument("--interval", type=float, help="seconds between checks")
    run.add_argument("--ticks", type=int, help="stop after N ticks (default: forever)")
    run.add_argument("--contributor", default="aditya")
    run.add_argument("--shared-id", default="local-dev")
    run.add_argument(
        "--agent", choices=list(AGENT_REGISTRY), default=None,
        help="which Agent product to follow (default: auto-detect -- a "
             "`join`-pinned binding for any registered agent wins, else the "
             "first agent's live-transcript heuristic, tried in registry order)",
    )
    run.add_argument(
        "--from-start",
        action="store_true",
        help="condense the whole transcript, not just new content (expensive)",
    )
    run.add_argument(
        "--debug-port", type=int, default=None,
        help=f"port for the live /debug dashboard (default {DEFAULT_DEBUG_PORT}; "
             f"0 disables; overridable via SYNAPSE_DEBUG_PORT)",
    )
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show config, transcripts and queue depth")
    status.set_defaults(func=cmd_status)

    replay = sub.add_parser("replay", help="retry undelivered findings")
    replay.add_argument(
        "--skipped", action="store_true",
        help="re-distil segments triage skipped, then archive the skip log "
             "(requires a joined Shared Session — see `synapse-worker join`)",
    )
    replay.set_defaults(func=cmd_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    # argv=None makes argparse read sys.argv itself, same as calling
    # parser.parse_args() with no arguments — real invocation is unchanged.
    # Tests pass an explicit list instead of touching sys.argv.
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
