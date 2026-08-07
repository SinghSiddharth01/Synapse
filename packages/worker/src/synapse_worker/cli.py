"""synapse-worker — follow a live agent conversation and condense it periodically.

    synapse-worker join <shared_id>                 # bind, once per session — see below
    synapse-worker join <shared_id> --agent-session-id $CLAUDE_CODE_SESSION_ID
    geniex serve                                    # terminal 1
    uv run synapse-worker run                       # terminal 2
    uv run synapse-worker run --agent-session-id $CLAUDE_CODE_SESSION_ID
    uv run synapse-worker run --interval 15 --ticks 4
    uv run synapse-worker status
    uv run synapse-worker replay                    # drain the write-ahead log

`join` is Plan A.7 / Plan D.2's `synapse join <shared_id>`, run from a terminal
— never from inside the agent conversation. Plan D Task D.3 is explicit that
there is no `attach(shared_id)` surfaced to the agent: "the agent never needs
to be told which Shared Session it is in." Without `--agent-session-id`, `join`
binds whatever Agent Session detection currently finds live, the same heuristic
`run` falls back to when nothing has been joined.

`--agent-session-id` (on `join`, `run` and `replay`) names ONE conversation
exactly, and every command that takes it refuses rather than falling back to
another window's binding. It is not a human hand-picking a transcript file —
the thing D.3 protects against — it is a conversation stating a fact about
itself, from its own environment (`CLAUDE_CODE_SESSION_ID`). Since W2
(2026-08-06) that is also what makes two windows of one product two separate
participants: each writes its own `bindings/<agent>/<session>.json` instead of
overwriting the other's, retiring Plan D's "one active Agent Session per Agent
product per machine" limitation. Omit it and every one of these commands
behaves exactly as it did before the flag existed — the most recently pinned
binding, which is what reading the single file always meant.

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

from synapse_contracts import LocalBinding, Segment
from synapse_distiller import (
    Distiller,
    PromptDropError,
    check_canary,
    load_config,
    load_pack_by_name,
)
from synapse_distiller.promptpack import PromptPackError
from synapse_providers import CallLog, NPUProvider, RecordingProvider

from synapse_worker.debug_server import DebugServer
from synapse_worker.discovery import (
    AGENT_REGISTRY,
    ResolvedTranscript,
    binding_dir_for_agent,
    binding_path_for_agent,
    join_session,
    read_bindings_for_agent,
    resolve_agent_binding,
    resolve_transcript,
    session_dirname,
)
from synapse_worker.limiter import SeamLimiter
from synapse_worker.loop import WorkerLoop
from synapse_worker.supervisor import WorkerSupervisor
from synapse_worker.producer import FileSink, HttpSink, Producer, read_last_bound_shared_id
from synapse_worker.stats import StatsBuffer
from synapse_worker.triage_log import TriageLog

logger = logging.getLogger(__name__)

DEFAULT_AGENT = "claude-code"  # `_build`'s fallback for an explicit --transcript
# given WITHOUT --agent, whose dialect nothing infers; `replay --skipped`
# also still only ever looks at the claude-code binding -- a narrower,
# still-disclosed gap `run`'s multi-agent resolution
# (_resolve_agent_and_transcript) does not share, since `run` is the half
# that had to reach CodexSource at all.
DEFAULT_SHARED_ID = "local-dev"  # `run`'s un-joined fallback; also what
# `_current_shared_id` treats as "current" when NEITHER a join binding NOR
# the WAL's last-bound marker have ever recorded anything — a genuinely
# fresh install.
DEFAULT_DEBUG_PORT = 8790


def _current_shared_id(state_dir: Path, agent_session_id: str | None = None) -> str:
    """The Shared Session a Producer built OUTSIDE a WorkerLoop should treat
    as "current" for the re-join envelope's held/deliverable split
    (producer.py's module docstring, STATE.md trap #8): the pinned binding
    if `join` has been run for this agent; else whatever `rebind()` last
    bound this WAL to LIVE (`producer.py`'s `read_last_bound_shared_id` —
    an un-joined `run --shared-id X` calls `rebind("X")` once at startup
    regardless of whether that run ever produces a finding worth `record()`ing,
    so this reflects the most recently LIVE binding, not merely the most
    recently WRITTEN one — see producer.py's AMENDMENT for why that
    distinction is load-bearing); else `DEFAULT_SHARED_ID`, for a WAL never
    bound to anything at all. `WorkerLoop.__init__` handles the
    joined/running case itself (`producer.rebind(binding.shared_id)`);
    `cmd_status` and `cmd_replay` build their own Producer with no loop
    around it, so they call this directly.

    `agent_session_id`, when the caller knows which conversation it is
    (`replay --agent-session-id`), picks that conversation's own binding out of
    the several this machine may now hold. Omitted — `status`, and every
    pre-W2 caller — resolves the most recently pinned binding, which is what
    reading the single `bindings/<agent>.json` file always meant."""
    joined = resolve_agent_binding(state_dir, DEFAULT_AGENT, agent_session_id)
    if joined is not None:
        return joined.shared_id
    last = read_last_bound_shared_id(state_dir / "wal")
    return last if last is not None else DEFAULT_SHARED_ID


def build_distiller(config, binding: LocalBinding) -> Distiller:
    """One construction path for a config-driven Distiller.

    Shared by `cmd_run` (via `_build`) and `cmd_replay --skipped` (Task 3) so
    there is exactly one place that turns a `SynapseConfig` into a live
    NPU-backed `Distiller` — the binding is the only thing that varies.

    SYNAPSE_DISTILLER selects the provider: "npu" (default, unchanged — the
    demo is the NPU story), "anthropic" (Claude Opus 5 via the Messages API,
    an API key per developer), or "claude-cli" (Claude through the local
    `claude` binary, on the developer's own subscription, with no credential
    to distribute). The last two exist so the full loop can be run in
    parallel instead of queueing for the single NPU box or sharing the
    Cirrascale key's ~20-req/hour ceiling. Nobody who leaves the env var
    unset sees any change in behaviour.
    """
    provider = _build_distiller_provider(config)
    return Distiller(
        provider,
        binding,
        load_pack_by_name(config.prompt_pack_name),
        config.distil_kinds,
        config.render_style,
    )


def _build_distiller_provider(config):
    arm = os.environ.get("SYNAPSE_DISTILLER", "npu")
    if arm == "anthropic":
        from synapse_providers import AnthropicProvider

        return AnthropicProvider()
    if arm == "claude-cli":
        # No credential at all: runs Claude through the local `claude` binary on
        # the developer's own subscription. The arm that lets several people
        # exercise the full loop at once without a key to distribute or the one
        # NPU box to queue for.
        from synapse_providers import ClaudeCliProvider

        return ClaudeCliProvider(max_tokens=config.provider.max_tokens)
    # The segment budget is derived as `usable_context - overhead -
    # response_reserve`, so the reserve is a PROMISE about how much output room
    # the segmenter left behind. Asking the model for more than that overruns
    # the context on exactly the segments that used their full budget: with
    # max_tokens 900 against a reserve of 500, a full segment came to
    # 2787 + 809 + 900 = 4496 against a 4096 ceiling, and the response was
    # truncated or degenerate. Clamping here (rather than lowering the shared
    # [provider] value) keeps the cloud arms above, which have their own much
    # larger reserves, untouched.
    max_tokens = config.effective_max_tokens
    if max_tokens < config.provider.max_tokens:
        logger.info(
            "provider.max_tokens %d exceeds %s's response_reserve %d; clamping "
            "to the reserve the segment budget was derived from",
            config.provider.max_tokens, config.model, config.record.response_reserve,
        )
    return NPUProvider(
        base_url=config.provider.base_url,
        model=config.model,
        max_tokens=max_tokens,
        temperature=config.provider.temperature,
        timeout=config.provider.timeout_s,
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

    `--agent-session-id` (W2) narrows all of that to one conversation: which
    registered agent owns the id is still not something the caller has to
    know, so every candidate is probed, but each probe demands that exact
    binding and `resolve_transcript` refuses to answer with anything else. Two
    windows on one machine are therefore two `run` processes, each following
    its own transcript, instead of two processes racing for one binding file.
    """
    requested = getattr(args, "agent", None)
    candidates = [requested] if requested else list(AGENT_REGISTRY)
    agent_session_id = getattr(args, "agent_session_id", None)

    first_heuristic: tuple[str, ResolvedTranscript] | None = None
    for agent in candidates:
        resolved = resolve_transcript(
            Path.cwd(), state_dir, agent=agent, agent_session_id=agent_session_id
        )
        if resolved is None:
            continue
        if resolved.source == "pinned":
            return agent, resolved
        if first_heuristic is None:
            first_heuristic = (agent, resolved)
    return first_heuristic if first_heuristic is not None else (None, None)


def _other_bound_agents(state_dir: Path, followed_agent: str) -> list[str]:
    """Which OTHER registered agents currently resolve to something `run`
    could have followed instead -- a `join`-pinned binding, or a live
    heuristic transcript -- but isn't, because one `WorkerLoop` follows
    exactly one Source. `join` can bind several agents in one call
    (`join_session` loops over `AGENT_REGISTRY`); without this, a
    Codex-and-Claude-Code join prints two bound agents and `run` then acts on
    only the first it resolves to, with nothing telling the operator the
    second binding is sitting there unfollowed.
    """
    others = []
    for other in AGENT_REGISTRY:
        if other == followed_agent:
            continue
        if resolve_transcript(Path.cwd(), state_dir, agent=other) is not None:
            others.append(other)
    return others


def _build(args: argparse.Namespace, debug_port: int = 0,
           stats: StatsBuffer | None = None):
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
        HttpSink(worker_cfg.upstream_url, timeout=worker_cfg.upstream_timeout_s)
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
    # what the distiller does. cmd_run may have built the buffer already (the
    # dashboard has to serve while the worker idles under --wait-for-binding,
    # long before this function runs) -- stats.llm is its CallLog either way.
    if debug_port and stats is None:
        stats = StatsBuffer(CallLog())
    if stats is not None:
        distiller.provider = RecordingProvider(distiller.provider, "distiller", stats.llm)

    loop = WorkerLoop(
        transcript=transcript,
        distiller=distiller,
        producer=producer,
        binding=binding,
        state_dir=state_dir,
        budget_tokens=config.segment_budget,
        idle_flush_seconds=worker_cfg.idle_flush_seconds,
        stats=stats,
        # The WORKER -> PROVIDER bounds, from [worker] in config/synapse.toml.
        # Passed rather than defaulted so `synapse-worker run` is governed by
        # the config file an operator can actually edit -- the loop's own
        # default exists for direct construction, not for the product.
        limiter=SeamLimiter(
            max_calls_per_tick=worker_cfg.max_calls_per_tick,
            max_concurrent_calls=worker_cfg.max_concurrent_calls,
            max_deferred_segments=worker_cfg.max_deferred_segments,
        ),
        # binding.agent is authoritative regardless of which branch above set
        # it -- a pinned binding's own `.agent`, the detected `agent` from
        # heuristic resolution, or DEFAULT_AGENT for an explicit --transcript
        # path whose dialect nothing here inferred.
        agent=binding.agent,
        # False for exactly one branch above: `--transcript <path>`, where
        # `agent_session_id` is the file's STEM and nothing has verified that
        # it is an Agent Session id at all (for Codex it certainly is not --
        # `rollout-<ts>-<uuid>.jsonl`). The loop uses this to decide whether
        # "no per-session binding for my id" means "another window's join,
        # not mine" (identified) or "I have no id to match with, so read the
        # machine's single answer exactly as before W2" (not identified).
        session_identified=resolved is not None,
    )
    source = resolved.source if resolved is not None else "explicit --transcript"
    return config, loop, transcript, producer, source, stats


def _build_siblings(args: argparse.Namespace, config, primary, stats) -> list:
    """A `WorkerLoop` for every OTHER conversation of this agent that is bound.

    `_build` above answers "which ONE transcript", because that is all a
    `WorkerLoop` follows. That made `synapse up` — which starts exactly one
    worker — silently blind to every window except whichever bound first: the
    second Claude Code window on a machine was never distilled, and the only
    evidence was absence.

    Skipped entirely when the caller named a conversation (`--transcript`,
    `--agent-session-id`). Those two flags mean "follow exactly this one", and
    quietly following three would contradict them.

    `scope == "session"` only: a `machine`-scoped binding is the
    `serve_local.py` stand-in that speaks for whatever conversation is here, so
    it is already the primary — adding it again would follow one transcript
    twice, double-distilling every segment.

    Each sibling gets its OWN `Producer` over its own WAL directory. A shared
    one would be retargeted by whichever loop ticked last (`rebind` in
    `WorkerLoop.__init__` and every `_sync_binding_from_disk`), tagging
    findings with another conversation's Shared Session. The `SeamLimiter` IS
    shared, deliberately, and is the reason one process beats N — see
    supervisor.py.
    """
    if getattr(args, "transcript", None) or getattr(args, "agent_session_id", None):
        return []
    worker_cfg = config.worker
    state_dir = Path(worker_cfg.state_dir)
    siblings = []
    for record in read_bindings_for_agent(state_dir, primary.binding.agent):
        if record.agent_session_id == primary.binding.agent_session_id:
            continue
        if record.scope != "session":
            continue
        transcript = Path(record.transcript_path)
        if not transcript.is_file():
            # Named but gone: the same refusal `resolve_transcript` makes for
            # the primary. Falling back to detection here would follow a
            # different conversation than the binding names.
            print(f"note             bound conversation {record.agent_session_id} "
                  f"names {transcript}, which does not exist — not following it. "
                  f"Re-run `synapse-worker join` from that window.")
            continue
        binding = record.to_local_binding()
        sink = (
            HttpSink(worker_cfg.upstream_url, timeout=worker_cfg.upstream_timeout_s)
            if worker_cfg.sink == "http"
            else FileSink(Path(worker_cfg.sink_file))
        )
        siblings.append(WorkerLoop(
            transcript=transcript,
            distiller=build_distiller(config, binding),
            producer=Producer(
                state_dir / "wal" / session_dirname(record.agent_session_id), sink),
            binding=binding,
            state_dir=state_dir,
            budget_tokens=config.segment_budget,
            idle_flush_seconds=worker_cfg.idle_flush_seconds,
            stats=stats,
            # THE shared bound. One process exists to make the NPU's
            # concurrency ceiling a real ceiling again.
            limiter=primary.limiter,
            agent=binding.agent,
            session_identified=True,
        ))
    return siblings


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

    bindings = join_session(
        args.shared_id,
        args.contributor,
        Path.cwd(),
        state_dir,
        agent_session_id=getattr(args, "agent_session_id", None),
    )

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
    # Plain ASCII: this line is printed to a Windows console/pipe whose default
    # codepage is cp1252, and an em dash here came out mangled on the X Elite.
    print(
        "\nYou are registered with the Synapse Service as a Contributor on the "
        "first push (the orchestrator does it, as the single egress)."
    )
    return 0


def _wait_for_binding(args: argparse.Namespace) -> None:
    """Block until this machine holds a `join`-pinned binding `run` can follow.

    `synapse up` starts this worker at PROCESS-start time, but sessions are a
    separate lifecycle: they are created or joined later, from inside an
    agent, by the MCP tools (or `synapse-worker join`). Until then there is
    nothing this worker could honestly distil into — the un-joined fallback
    would stamp findings into the `local-dev` placeholder nobody reads — and
    exiting instead would mean the worker needs a restart at join time, which
    is exactly the start/session coupling `synapse up` no longer has. So
    under `--wait-for-binding` it idles on a cheap re-probe and falls through
    to the normal path the moment a binding exists.
    """
    import time

    config = load_config()
    state_dir = Path(config.worker.state_dir)
    interval = args.interval or config.worker.poll_interval_seconds
    announced = False
    while True:
        _, resolved = _resolve_agent_and_transcript(args, state_dir)
        if resolved is not None and resolved.local_binding is not None:
            if announced:
                print(f"bound to {resolved.local_binding.shared_id} — following "
                      f"{resolved.path}", flush=True)
            return
        if not announced:
            print("waiting for a Shared Session: nothing is distilled or pushed "
                  "until one is created or joined (create_session/join_session "
                  "from your agent, or `synapse-worker join <shared_id>`). "
                  f"Re-checking every {interval:g}s.", flush=True)
            announced = True
        time.sleep(interval)


async def cmd_run(args: argparse.Namespace) -> int:
    debug_port = _resolve_debug_port(args)

    # The dashboard binds BEFORE any waiting: /debug/stats.json is the only
    # liveness signal the worker has (net.ping_worker — what `synapse health`
    # parses), and under --wait-for-binding the wait below can last forever.
    # Binding it only after a session joined made `synapse health` report a
    # healthy, deliberately-idle worker as "died after start" (2026-08-06).
    # The dashboard is optional instrumentation; the transcript work is not.
    # Binding can fail with a plain OSError -- e.g. two `run`s on the same
    # machine racing for the default 8790, or a stale worker still holding it
    # -- and that must not abort the core command.
    stats: StatsBuffer | None = None
    debug_server: DebugServer | None = None
    if debug_port:
        stats = StatsBuffer(CallLog())
        debug_server = DebugServer(stats, debug_port)
        try:
            bound_port = debug_server.start()
        except OSError as exc:
            print(f"debug            disabled -- failed to bind port {debug_port}: {exc}",
                  file=sys.stderr)
            debug_server = None
        else:
            print(f"debug            http://127.0.0.1:{bound_port}/debug")
    else:
        print("debug            disabled (--debug-port 0)")

    if getattr(args, "wait_for_binding", False) and not args.transcript:
        if stats is not None:
            stats.phase = "waiting for a session"
        _wait_for_binding(args)
        if stats is not None:
            stats.phase = "following"

    config, loop, transcript, _, source, stats = _build(args, debug_port, stats=stats)
    if debug_server is not None:
        debug_server.transcript = str(transcript)
    interval = args.interval or config.worker.poll_interval_seconds

    print(config.describe())
    print(f"transcript       {transcript}")
    if source == "heuristic":
        print("selection        HEURISTIC — most recently written transcript in "
              "this directory. Run `synapse-worker join <shared_id>` to bind "
              "exactly one instead.")
    else:
        print(f"selection        {source}")
    ignored_agents = _other_bound_agents(Path(config.worker.state_dir), loop.agent)
    if ignored_agents:
        print(f"note             also bound: {', '.join(ignored_agents)} -- this "
              f"process follows only {loop.agent!r} (one Source per `run`). "
              f"Start a separate `synapse-worker run --agent <name>` for "
              f"each other agent to follow it too.")
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
        if debug_server is not None:
            debug_server.stop()
        return 1
    print(f"canary           ok (prompt_tokens={canary.input_tokens})\n")

    # Every OTHER conversation of this agent that is bound on this machine.
    # `synapse up` starts one worker, so without this the second window to bind
    # is never distilled at all — silently, since the process stays healthy and
    # keeps ticking on the first window's transcript.
    siblings = _build_siblings(args, config, loop, stats)
    for sibling in siblings:
        print(f"also following   {sibling.transcript} "
              f"[{sibling.binding.agent_session_id}] -> {sibling.binding.shared_id}")

    if args.from_start:
        print("Starting from the beginning of the transcript.\n")
    elif config.worker.attach_at_end:
        for followed in (loop, *siblings):
            followed.attach_at_end()
        print("Attached at the end; only new conversation will be condensed.\n")

    runner = WorkerSupervisor([loop, *siblings]) if siblings else loop
    try:
        await runner.run(interval_seconds=interval, max_ticks=args.ticks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Ctrl-C. On Python 3.11+ asyncio.Runner delivers it by CANCELLING
        # the main task, so it arrives here as CancelledError, not
        # KeyboardInterrupt; catching only the latter skipped the graceful
        # shutdown below and let a traceback escape.
        pass
    outcome = await runner.shutdown()
    for result in (outcome if siblings else [outcome]):
        print(f"\nshutdown — {result.summary()}")
    if debug_server is not None:
        debug_server.stop()
    return 0


async def cmd_status(args: argparse.Namespace) -> int:
    """`join` now loops over `AGENT_REGISTRY` and can bind several agents in
    one call (Plan D.2) -- this must too, for both halves below, or a
    Codex-only join (Claude Code never live) leaves `status` printing
    "joined session none" right after `join` printed the opposite.
    """
    config = load_config()
    print(config.describe())
    print(f"\npoll interval    {config.worker.poll_interval_seconds}s")

    state_dir = Path(config.worker.state_dir)
    cwd = Path.cwd()
    print()
    joined_any = False
    for agent in AGENT_REGISTRY:
        resolved = resolve_transcript(cwd, state_dir, agent=agent)
        primary_session_id = None
        if resolved is not None and resolved.source == "pinned":
            binding = resolved.local_binding
            primary_session_id = binding.agent_session_id
            print(f"joined session   [{agent}] shared_id={binding.shared_id!r} "
                  f"agent_session_id={binding.agent_session_id} "
                  f"transcript={resolved.path} (exists)")
            joined_any = True
        # Since W2 one product can hold several bindings at once — one per
        # conversation. `resolve_transcript` still answers with exactly one
        # ("the most recently pinned"), which is the one `run` would follow
        # without `--agent-session-id`; the rest are real joins too and a
        # status that hid them would describe a two-window machine as if it
        # were a one-window machine, which is the entire defect W2 fixes.
        for other in read_bindings_for_agent(state_dir, agent):
            if other.agent_session_id == primary_session_id:
                continue
            exists = "exists" if Path(other.transcript_path).is_file() else "MISSING"
            print(f"also joined      [{agent}] shared_id={other.shared_id!r} "
                  f"agent_session_id={other.agent_session_id} "
                  f"transcript={other.transcript_path} ({exists})")
            joined_any = True
    if not joined_any:
        checked = ", ".join(
            f"{binding_dir_for_agent(state_dir, agent)}/, "
            f"{binding_path_for_agent(state_dir, agent)}"
            for agent in AGENT_REGISTRY
        )
        print(f"joined session   none (checked {checked}) — run "
              f"`synapse-worker join <shared_id>`, or the worker falls back to "
              f"the most-recently-active-transcript heuristic")

    print(f"\ntranscripts for {cwd}:")
    any_transcripts = False
    for agent, registration in AGENT_REGISTRY.items():
        for t in registration.finder(cwd, None):
            any_transcripts = True
            live = "LIVE" if t.age_seconds <= 1800 else "idle"
            print(f"  [{live}] [{agent}] {t.path.name}  {t.size/1e6:.1f} MB  "
                  f"{t.age_seconds/60:.0f} min since last write")
    if not any_transcripts:
        print("  none found")

    state_dir = Path(config.worker.state_dir)
    producer = Producer(
        state_dir / "wal", FileSink(Path(config.worker.sink_file)), _current_shared_id(state_dir)
    )
    deliverable, held = producer.pending_count()
    print(f"\nwrite-ahead log  {producer.findings_path}")
    print(f"unsent findings  {deliverable + held}")
    if held:
        print(f"  held (other session)  {held}")
    return 0


async def cmd_replay(args: argparse.Namespace) -> int:
    """Drain anything the sink rejected earlier. Idempotent by Finding.id.

    `--skipped` re-distils every segment triage skipped and archives the skip
    log — a wrong triage skip is recoverable, not permanent loss.
    """
    config = load_config()
    state_dir = Path(config.worker.state_dir)
    sink = (
        HttpSink(config.worker.upstream_url, timeout=config.worker.upstream_timeout_s)
        if config.worker.sink == "http"
        else FileSink(Path(config.worker.sink_file))
    )
    # The re-join envelope (producer.py's module docstring, STATE.md trap #8)
    # needs a "current" to split held from deliverable -- this Producer has
    # no WorkerLoop around it to supply one, so read whatever's joined now
    # (or the same un-joined fallback `_build` uses) exactly once, up front.
    # Both branches below share it: `--skipped` records new findings under
    # it, and the plain drain-the-log path needs it too, or a WAL populated
    # by a normal (possibly un-joined) `run` would read every entry as held.
    agent_session_id = getattr(args, "agent_session_id", None)
    producer = Producer(
        state_dir / "wal", sink, _current_shared_id(state_dir, agent_session_id)
    )

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
        joined = resolve_agent_binding(state_dir, DEFAULT_AGENT, agent_session_id)
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
        held = producer.pending_count()[1]
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
        if held:
            message += f" {held} held (other session)."
        if failed:
            message += f" {len(failed)} segment(s) failed and were re-queued."
        if archived is not None:
            message += f" Log archived to {archived.name}."
        print(message)
        return 0 if not failed else 1

    sent, still_pending = await producer.flush()
    held = producer.pending_count()[1]
    print(f"sent {sent}, still pending {still_pending}"
          + (f", {held} held (other session)" if held else ""))
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
    join.add_argument(
        "--agent-session-id", default=None,
        help="bind EXACTLY this Agent Session instead of whatever detection "
             "finds live (Claude Code exports it as CLAUDE_CODE_SESSION_ID). "
             "Required to tell two windows of the same agent apart; an id that "
             "matches no transcript binds nothing rather than guessing",
    )
    join.set_defaults(func=cmd_join)

    run = sub.add_parser("run", help="follow and condense periodically")
    run.add_argument("--transcript", help="path to a transcript (default: auto-detect)")
    run.add_argument("--interval", type=float, help="seconds between checks")
    run.add_argument("--ticks", type=int, help="stop after N ticks (default: forever)")
    run.add_argument(
        "--wait-for-binding", action="store_true",
        help="idle until a join-pinned binding exists instead of exiting or "
             "falling back to the un-joined shared id. What `synapse up` "
             "passes: processes start at up time, sessions bind later, from "
             "the agent")
    run.add_argument("--contributor", default="aditya")
    run.add_argument("--shared-id", default=DEFAULT_SHARED_ID)
    run.add_argument(
        "--agent", choices=list(AGENT_REGISTRY), default=None,
        help="which Agent product to follow (default: auto-detect -- a "
             "`join`-pinned binding for any registered agent wins, else the "
             "first agent's live-transcript heuristic, tried in registry order)",
    )
    run.add_argument(
        "--agent-session-id", default=None,
        help="follow the binding for EXACTLY this Agent Session (one window of "
             "one agent). Without it, the most recently pinned binding for the "
             "product wins, which is a guess when two windows are open; with "
             "it, no binding for that id means this refuses to start rather "
             "than following a different conversation",
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
    replay.add_argument(
        "--agent-session-id", default=None,
        help="read Attribution (contributor/shared_id) from EXACTLY this Agent "
             "Session's binding instead of the most recently pinned one",
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
    try:
        return asyncio.run(args.func(args))
    except PromptPackError as exc:
        # A broken INSTALL (no packaged prompt data), not a broken run. The
        # error's own message names the remedy; a traceback buried it —
        # 2026-08-06, this killed the worker with a raw traceback in
        # worker.log the moment a session was joined. Exit 2: misconfigured,
        # not crashed.
        print(f"synapse-worker: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # Ctrl-C outside cmd_run's own handler (status, replay, the
        # wait-for-binding idle loop): newline past the echoed ^C, then
        # exit 130 (128+SIGINT). Never a traceback.
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `synapse-worker status | head` closing the pipe early is routine:
        # exit 141 (128+SIGPIPE), quietly.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141


if __name__ == "__main__":
    sys.exit(main())
