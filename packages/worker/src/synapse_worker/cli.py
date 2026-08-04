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
import sys
from pathlib import Path

from synapse_contracts import LocalBinding
from synapse_distiller import Distiller, check_canary, load_config, load_pack_by_name
from synapse_providers import NPUProvider

from synapse_worker.discovery import (
    binding_path_for_agent,
    find_claude_code_transcripts,
    join_session,
    resolve_transcript,
)
from synapse_worker.loop import WorkerLoop
from synapse_worker.producer import FileSink, HttpSink, Producer

DEFAULT_AGENT = "claude-code"  # the only Source adapter that exists (Plan A.3)


def _build(args: argparse.Namespace):
    config = load_config()
    worker_cfg = config.worker
    state_dir = Path(worker_cfg.state_dir)

    resolved = None
    if getattr(args, "transcript", None):
        transcript = Path(args.transcript)
    else:
        resolved = resolve_transcript(Path.cwd(), state_dir) or _no_transcript()
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
            agent=DEFAULT_AGENT,
        )
    provider = NPUProvider(
        base_url=config.provider.base_url,
        model=config.model,
        max_tokens=config.provider.max_tokens,
        temperature=config.provider.temperature,
        timeout=config.provider.timeout_s,
    )
    distiller = Distiller(
        provider,
        binding,
        load_pack_by_name(config.prompt_pack_name),
        config.distil_kinds,
        config.render_style,
    )
    loop = WorkerLoop(
        transcript=transcript,
        distiller=distiller,
        producer=producer,
        binding=binding,
        state_dir=state_dir,
        budget_tokens=config.segment_budget,
        idle_flush_seconds=worker_cfg.idle_flush_seconds,
    )
    source = resolved.source if resolved is not None else "explicit --transcript"
    return config, loop, transcript, producer, source


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
    config, loop, transcript, _, source = _build(args)
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
    print(f"state            {config.worker.state_dir}\n")

    # The prompt-drop guard, once, before any transcript content is processed.
    # A model that has stopped reading its prompt would otherwise write invented
    # findings straight into the write-ahead log.
    canary = await check_canary(loop.distiller.provider)
    if not canary:
        print(f"CANARY FAILED: {canary.detail}", file=sys.stderr)
        print("Refusing to distil — findings from this model would be invented.",
              file=sys.stderr)
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
    """Drain anything the sink rejected earlier. Idempotent by Finding.id."""
    config = load_config()
    state_dir = Path(config.worker.state_dir)
    sink = (
        HttpSink(config.worker.upstream_url)
        if config.worker.sink == "http"
        else FileSink(Path(config.worker.sink_file))
    )
    producer = Producer(state_dir / "wal", sink)
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
        "--from-start",
        action="store_true",
        help="condense the whole transcript, not just new content (expensive)",
    )
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show config, transcripts and queue depth")
    status.set_defaults(func=cmd_status)

    replay = sub.add_parser("replay", help="retry undelivered findings")
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
