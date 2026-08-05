"""Find the transcript of a live Agent Session.

Detection, never configuration — Plan A.1. The worker is not told which agent is
running; it looks at where agents write and picks the transcript that is being
appended to right now.

Claude Code's layout, confirmed 2026-08-04:

    ~/.claude/projects/<slug>/<session-uuid>.jsonl

`<slug>` is the working directory with drive colons and path separators replaced
by dashes, e.g. C:\\QC_multiverse_hackathon\\Synapse becomes
C--QC-multiverse-hackathon-Synapse.

Codex's layout, confirmed 2026-08-05 (see fixtures/raw_lines/codex/README.md
for the full trail against github.com/openai/codex primary source):

    ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<uuid>.jsonl

`AGENT_REGISTRY` is Task A.1's "static registry: agent name → transcript
root(s) → transcript dialect → Source class" — `find_live_transcript` and
`resolve_transcript` dispatch through it, so a third adapter needs a registry
entry and a `find_*_transcripts` function, not a rewrite of either.

**`join_session` is deliberately NOT wired through the registry.** Its
existing tests monkeypatch `find_live_transcript` as a single, whole-function
substitute (no `agent` param) to keep `synapse-worker join` deterministic
under test without touching real machine state; looping it over every
registered agent would mean probing the *real* `~/.codex/sessions` by
default on every `join` in a test that never overrides it, which is a live,
not hypothetical, flakiness risk on any developer machine that actually uses
Codex. `join_session` binding only Claude Code today is a scoped, recorded
gap (Chain A deviations), not silent — `resolve_transcript`/
`find_live_transcript` are fully agent-aware and tested per agent below, and
that is the seam a future, deliberate `join_session` change would extend.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import LocalBinding, SessionBinding, read_binding, write_binding

from synapse_worker.sources.claude_code import ClaudeCodeSource
from synapse_worker.sources.codex import CodexSource

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

# A transcript untouched for longer than this is a finished conversation, not a
# live one. Generous, because a developer reading code can easily leave an agent
# idle for minutes mid-session.
LIVE_WINDOW_SECONDS = 30 * 60


def project_slug(cwd: Path) -> str:
    """C:\\QC_multiverse_hackathon\\Synapse -> C--QC-multiverse-hackathon-Synapse"""
    text = str(Path(cwd).resolve())
    return text.replace(":", "-").replace("\\", "-").replace("/", "-").replace("_", "-")


@dataclass(frozen=True)
class DiscoveredTranscript:
    path: Path
    agent: str
    session_id: str
    modified_at: float
    size: int

    @property
    def age_seconds(self) -> float:
        return time.time() - self.modified_at


def find_claude_code_transcripts(
    cwd: Path | None = None, projects_root: Path | None = None
) -> list[DiscoveredTranscript]:
    """Transcripts for one working directory, most recently written first."""
    root = Path(projects_root) if projects_root else CLAUDE_PROJECTS
    if not root.is_dir():
        return []

    candidates: list[Path] = []
    if cwd is not None:
        slug_dir = root / project_slug(cwd)
        if slug_dir.is_dir():
            candidates = list(slug_dir.glob("*.jsonl"))
    else:
        candidates = list(root.glob("*/*.jsonl"))

    found: list[DiscoveredTranscript] = []
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append(
            DiscoveredTranscript(
                path=path,
                agent="claude-code",
                session_id=path.stem,
                modified_at=stat.st_mtime,
                size=stat.st_size,
            )
        )
    return sorted(found, key=lambda t: t.modified_at, reverse=True)


# rollout-YYYY-MM-DDThh-mm-ss-<uuid>.jsonl -- mirrors openai/codex's own
# parse_timestamp_uuid_from_filename (codex-rs/rollout/src/list.rs): the
# session/thread id is embedded in the filename by construction, so it is
# read from there rather than by opening the file.
_CODEX_ROLLOUT_FILENAME = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"\.jsonl$"
)


def _codex_session_id_from_filename(path: Path) -> str | None:
    match = _CODEX_ROLLOUT_FILENAME.match(path.name)
    return match.group("uuid") if match else None


def find_codex_transcripts(
    cwd: Path | None = None, projects_root: Path | None = None
) -> list[DiscoveredTranscript]:
    """Transcripts for the Codex agent, most recently written first.

    Confirmed against github.com/openai/codex (codex-rs/rollout), 2026-08-05
    — see fixtures/raw_lines/codex/README.md. Layout:

        ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<uuid>.jsonl

    **`cwd` is accepted for interface parity with `find_claude_code_
    transcripts` but is NOT used to filter here.** Unlike Claude Code, whose
    `<slug>` directory encodes the working directory, Codex's day-tree does
    not partition by project at all — every project's rollouts land in the
    same `YYYY/MM/DD` folder, distinguished only by the `cwd` field inside
    each file's `session_meta` line. Filtering correctly would mean opening
    every candidate file to read that line; this pass does not do that.
    Documented limitation, not a silent one — a developer with several Codex
    projects open sees all of their transcripts here, not just the one under
    `cwd`.
    """
    root = Path(projects_root) if projects_root else CODEX_SESSIONS
    if not root.is_dir():
        return []

    candidates = list(root.glob("*/*/*/rollout-*.jsonl"))
    found: list[DiscoveredTranscript] = []
    for path in candidates:
        try:
            stat = path.stat()
        except OSError:
            continue
        found.append(
            DiscoveredTranscript(
                path=path,
                agent="codex",
                session_id=_codex_session_id_from_filename(path) or path.stem,
                modified_at=stat.st_mtime,
                size=stat.st_size,
            )
        )
    return sorted(found, key=lambda t: t.modified_at, reverse=True)


@dataclass(frozen=True)
class AgentRegistration:
    """One entry in the static agent registry — Task A.1.

    The worker is never *configured* for an agent; it is *detected* by
    walking `roots` for whichever dialect is registered. `finder` lists that
    agent's transcripts, most-recently-written first, with the exact
    `(cwd, projects_root) -> list[DiscoveredTranscript]` signature regardless
    of which agent it is — `find_live_transcript`/`resolve_transcript`
    dispatch through this table instead of hard-coding one agent's layout.
    """

    roots: tuple[Path, ...]
    finder: Callable[[Path | None, Path | None], list[DiscoveredTranscript]]
    source_class: type
    dialect: str


AGENT_REGISTRY: dict[str, AgentRegistration] = {
    "claude-code": AgentRegistration(
        roots=(CLAUDE_PROJECTS,),
        finder=find_claude_code_transcripts,
        source_class=ClaudeCodeSource,
        dialect="claude-code-jsonl",
    ),
    "codex": AgentRegistration(
        roots=(CODEX_SESSIONS,),
        finder=find_codex_transcripts,
        source_class=CodexSource,
        dialect="codex-rollout-jsonl",
    ),
}


def find_live_transcript(
    cwd: Path | None = None,
    projects_root: Path | None = None,
    *,
    agent: str = "claude-code",
) -> DiscoveredTranscript | None:
    """The one transcript currently being written, if any, for `agent`.

    Dispatches through `AGENT_REGISTRY`. `agent` is keyword-only and defaults
    to "claude-code" so every pre-registry call site — `join_session`, and
    this function's own old two-positional-argument shape — keeps working
    unchanged.
    """
    finder = AGENT_REGISTRY[agent].finder
    for transcript in finder(cwd, projects_root):
        if transcript.age_seconds <= LIVE_WINDOW_SECONDS:
            return transcript
    return None


def bindings_dir(state_dir: Path) -> Path:
    return Path(state_dir) / "bindings"


def binding_path_for_agent(state_dir: Path, agent: str) -> Path:
    """One file per Agent PRODUCT, not per Agent Session.

    Plan D.2: "one laptop holds several bindings — one per Agent Session;
    Claude Code and Codex can sit in different Shared Sessions." Combined with
    Plan D's documented limitation ("one active Agent Session per Agent product
    per machine"), a binding is uniquely identified by which product it is —
    `claude-code.json`, `codex.json` — never by a single fixed `active.json`.
    """
    return bindings_dir(state_dir) / f"{agent}.json"


def join_session(
    shared_id: str,
    contributor: str,
    cwd: Path,
    state_dir: Path,
    *,
    projects_root: Path | None = None,
) -> list[SessionBinding]:
    """`synapse join <shared_id>` — Plan A.7 / Plan D.2.

    Binds every currently-detected LIVE Agent Session to `shared_id`, one
    binding per Agent product. Still only ever probes `claude-code` today —
    `CodexSource` and its registry entry exist (Chain A), but this function
    was deliberately not looped over `AGENT_REGISTRY` in that pass; see the
    module docstring's "`join_session` is deliberately NOT wired through the
    registry" note for why, and Chain A's deviations for the record of the
    call. The shape (returning a *list* of bindings, one per detected agent)
    is already ready for that loop without a reshape, matching Plan D.2's
    first failing test: "joining with two agents detected produces two
    bindings" — only the detection loop itself is still single-agent.

    Detection is unchanged: this does not let a human pick a specific
    transcript file. Plan D.3 is explicit that there is no `attach(shared_id)`
    exposed to the agent, and the corresponding design choice here is that
    `join` does not let the *developer* hand-pick one either — it binds
    whatever detection finds live right now, and accepts the documented
    ambiguity if two windows of the same product are both live.

    NOT DONE: Plan D.2 also says join "registers the Contributor with the
    service (POST /members)". No Synapse Service exists yet to register with,
    so that step is skipped — logged, not silently dropped.
    """
    bound: list[SessionBinding] = []

    transcript = find_live_transcript(cwd, projects_root)
    if transcript is not None:
        binding = SessionBinding(
            agent_session_id=transcript.session_id,
            shared_id=shared_id,
            contributor=contributor,
            agent=transcript.agent,
            transcript_path=str(transcript.path),
            pinned_at=datetime.now(timezone.utc),
        )
        write_binding(binding_path_for_agent(state_dir, transcript.agent), binding)
        bound.append(binding)

    if not bound:
        logger.warning(
            "join_session: no live Agent Session detected for %s; nothing bound", cwd
        )

    logger.info(
        "join_session: Contributor registration with the Synapse Service "
        "skipped (no service exists yet)"
    )
    return bound


@dataclass(frozen=True)
class ResolvedTranscript:
    """Which transcript to follow, and how that was decided.

    `source` is surfaced to the operator deliberately — "pinned" means
    `synapse join` bound this Agent Session explicitly; "heuristic" means
    nobody has, and the worker is guessing based on which file was written to
    most recently. Those are different confidence levels and the CLI says so.
    """

    path: Path
    agent_session_id: str
    source: str  # "pinned" | "heuristic"
    local_binding: LocalBinding | None = None  # set only when source == "pinned"


def resolve_transcript(
    cwd: Path,
    state_dir: Path,
    *,
    agent: str = "claude-code",
    projects_root: Path | None = None,
) -> ResolvedTranscript | None:
    """Prefer an explicit `synapse join` binding over the live-transcript heuristic.

    A binding is honoured only while its transcript file still exists — a
    session that has since been deleted or moved must not silently keep
    steering the worker at a path that is no longer there. In that case this
    falls through to the heuristic exactly as if `join` had never been run.

    `agent` governs BOTH halves, not just the binding lookup: the heuristic
    fallback dispatches through `AGENT_REGISTRY` too, so
    `resolve_transcript(..., agent="codex")` actually looks for a live Codex
    transcript rather than silently reusing Claude Code's finder.
    """
    pinned: SessionBinding | None = read_binding(binding_path_for_agent(state_dir, agent))

    if pinned is not None:
        transcript_path = Path(pinned.transcript_path)
        if transcript_path.is_file():
            return ResolvedTranscript(
                path=transcript_path,
                agent_session_id=pinned.agent_session_id,
                source="pinned",
                local_binding=pinned.to_local_binding(),
            )
        logger.warning(
            "Bound transcript %s no longer exists; falling back to detection. "
            "Run `synapse-worker join <shared_id>` again to re-bind.",
            transcript_path,
        )

    heuristic = find_live_transcript(cwd, projects_root, agent=agent)
    if heuristic is None:
        return None
    return ResolvedTranscript(
        path=heuristic.path, agent_session_id=heuristic.session_id, source="heuristic"
    )
