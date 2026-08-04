"""synapse-worker — follow a live agent conversation and condense it periodically.

    geniex serve                                    # terminal 1
    uv run synapse-worker run                       # terminal 2
    uv run synapse-worker run --interval 15 --ticks 4
    uv run synapse-worker status
    uv run synapse-worker replay                    # drain the write-ahead log

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

from synapse_worker.discovery import find_claude_code_transcripts, find_live_transcript
from synapse_worker.loop import WorkerLoop
from synapse_worker.producer import FileSink, HttpSink, Producer


def _build(args: argparse.Namespace):
    config = load_config()
    worker_cfg = config.worker

    transcript = (
        Path(args.transcript)
        if getattr(args, "transcript", None)
        else (find_live_transcript(Path.cwd()) or _no_transcript()).path
    )

    state_dir = Path(worker_cfg.state_dir)
    sink = (
        HttpSink(worker_cfg.upstream_url)
        if worker_cfg.sink == "http"
        else FileSink(Path(worker_cfg.sink_file))
    )
    producer = Producer(state_dir / "wal", sink)

    binding = LocalBinding(
        agent_session_id=transcript.stem,
        shared_id=args.shared_id,
        contributor=args.contributor,
        agent="claude-code",
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
    return config, loop, transcript, producer


def _no_transcript():
    print(
        "No live agent transcript found for this directory.\n"
        "Start a coding-agent session here, or pass --transcript <path>.",
        file=sys.stderr,
    )
    raise SystemExit(2)


async def cmd_run(args: argparse.Namespace) -> int:
    config, loop, transcript, _ = _build(args)
    interval = args.interval or config.worker.poll_interval_seconds

    print(config.describe())
    print(f"transcript       {transcript}")
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="synapse-worker", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

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

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
