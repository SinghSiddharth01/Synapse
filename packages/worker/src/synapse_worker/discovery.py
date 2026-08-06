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
root(s) → transcript dialect → Source class" — `find_live_transcript`,
`resolve_transcript`, and `join_session` all dispatch through it, so a third
adapter needs a registry entry and a `find_*_transcripts` function, not a
rewrite of any of them.

Detection is the default, not the only path. Since 2026-08-06 a caller that
knows its own session id (Claude Code exports `CLAUDE_CODE_SESSION_ID`, and it
is exactly the transcript stem) can hand it to `join_session` and get an exact
bind via `find_transcript_by_session_id`, which searches every project slug and
never looks at mtime. See docs/superpowers/specs/
2026-08-06-session-lifecycle-design.md. Detection remains what runs when nobody
says — and `find_live_transcript_candidates` now reports when detection is
guessing between near-simultaneous transcripts instead of quietly picking one.

`join_session` loops over `AGENT_REGISTRY`, binding every currently-live
Agent Session it finds, one per Agent product — Plan D.2's "one laptop holds
several bindings ... Claude Code and Codex can sit in different Shared
Sessions." An earlier pass here left this loop out, reasoning that the
existing `join` tests monkeypatch `find_live_transcript` as a whole-function
substitute and looping would mean probing the real `~/.codex/sessions` by
default under test. That reasoning did not hold up: those tests already
override `find_live_transcript` in full (not per-agent), so looping never
touches disk under test either — it only needed the same substitute
function to accept the `agent` keyword the real signature already has
(`agent: str = "claude-code"`, keyword-only, so every un-widened lambda in
this codebase already breaks on it the same way).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from synapse_contracts import LocalBinding, SessionBinding, read_binding, write_binding

from synapse_worker.sources.claude_code import ClaudeCodeSource
from synapse_worker.sources.codex import SESSION_META_TYPE, CodexSource

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

# A transcript untouched for longer than this is a finished conversation, not a
# live one. Generous, because a developer reading code can easily leave an agent
# idle for minutes mid-session.
LIVE_WINDOW_SECONDS = 30 * 60

# Two live transcripts whose last writes land this close together are, for
# binding purposes, indistinguishable: whichever is "most recent" is decided by
# a race between two agent windows that are both being typed into right now, and
# it can flip between the moment we look and the moment we bind. Observed
# 2026-08-06 on this machine with two Claude Code windows open against
# ~/.claude/projects/-Users-siddharthsingh: alternating replies rewrote the two
# .jsonl files a second or two apart, so mtime ordering swapped repeatedly
# within one minute. 5s is deliberately generous — the cost of a false
# "ambiguous" is one extra `agent_session_id` argument from the caller; the cost
# of a false "unambiguous" is a whole conversation distilled into the wrong
# Shared Session, silently. See
# docs/superpowers/specs/2026-08-06-session-lifecycle-design.md, "Requirement:
# bind the session we started from".
AMBIGUITY_WINDOW_SECONDS = 5


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


def _codex_transcript_cwd(path: Path) -> str | None:
    """The working directory a Codex rollout was recorded under, read from its
    own `session_meta` line — the only place that lives, since (unlike Claude
    Code's `<slug>` directory) Codex's day-tree does not partition by project.
    `session_meta` is written once, near the top of the file (see module
    docstring), so this scans forward from the top until it finds one rather
    than assuming line 0. Returns `None` when no readable `session_meta` line
    exists — a torn write, a line caught mid-append, or a file this adapter's
    own parser would also skip — and the caller treats `None` as "does not
    match", never as "matches everything": a transcript we cannot positively
    identify must not be attributed to a directory we merely hope it belongs
    to.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != SESSION_META_TYPE:
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    return None
                cwd = payload.get("cwd")
                return str(cwd) if cwd is not None else None
    except OSError:
        return None
    return None


def _codex_transcript_matches_cwd(path: Path, cwd: Path) -> bool:
    recorded = _codex_transcript_cwd(path)
    if recorded is None:
        return False
    try:
        return Path(recorded).resolve() == Path(cwd).resolve()
    except (OSError, RuntimeError):
        # A recorded cwd that cannot be resolved on this machine (e.g. a path
        # from a different OS or a since-deleted mount) still deserves a
        # literal comparison rather than being discarded outright.
        return str(recorded) == str(cwd)


def find_codex_transcripts(
    cwd: Path | None = None, projects_root: Path | None = None
) -> list[DiscoveredTranscript]:
    """Transcripts for the Codex agent, most recently written first.

    Confirmed against github.com/openai/codex (codex-rs/rollout), 2026-08-05
    — see fixtures/raw_lines/codex/README.md. Layout:

        ~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<timestamp>-<uuid>.jsonl

    Unlike Claude Code, whose `<slug>` directory encodes the working
    directory for free, Codex's day-tree does not partition by project at
    all — every project's rollouts land in the same `YYYY/MM/DD` folder,
    distinguished only by the `cwd` field inside each file's `session_meta`
    line. So when `cwd` is given, this opens each candidate to read that
    line (`_codex_transcript_cwd`) and excludes anything that does not
    resolve to the same directory — including anything whose `session_meta`
    cannot be read at all. `cwd=None` (the low-level, unscoped query some
    callers deliberately want) returns every candidate, unfiltered, exactly
    as before.
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
        if cwd is not None and not _codex_transcript_matches_cwd(path, cwd):
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

    Ambiguity is NOT surfaced here. This still returns the most recently
    modified live transcript even when a second one was written a moment ago;
    callers who need to know that use `find_live_transcript_candidates`.
    Rejected alternative: widening this return type to carry the candidate
    list. `join_session` and `resolve_transcript` both call it, both want
    exactly "the one to follow", and `resolve_transcript` feeds a
    `ResolvedTranscript` the CLI's `run`/`status` already render — changing
    the type would have rippled into all of them to serve one new caller (the
    orchestrator's bind step), which is the caller that can actually *do*
    something with a refusal. Cheapest correct split: leave the workhorse
    alone, add the richer query beside it.
    """
    finder = AGENT_REGISTRY[agent].finder
    for transcript in finder(cwd, projects_root):
        if transcript.age_seconds <= LIVE_WINDOW_SECONDS:
            return transcript
    return None


@dataclass(frozen=True)
class LiveTranscriptCandidates:
    """Every live transcript for one agent, plus whether picking one is a guess.

    `candidates` is most-recently-written first, so `chosen` is exactly what
    `find_live_transcript` would have returned. `ambiguous` is the whole point
    of this type: it tells the caller that `chosen` is a coin toss and that it
    should refuse and ask for an explicit `agent_session_id` rather than bind.
    """

    candidates: tuple[DiscoveredTranscript, ...]
    ambiguous: bool

    @property
    def chosen(self) -> DiscoveredTranscript | None:
        return self.candidates[0] if self.candidates else None


def find_live_transcript_candidates(
    cwd: Path | None = None,
    projects_root: Path | None = None,
    *,
    agent: str = "claude-code",
) -> LiveTranscriptCandidates:
    """`find_live_transcript`, but showing its work — spec item 3.

    Ambiguous means: two or more transcripts are inside `LIVE_WINDOW_SECONDS`
    AND some pair of them has `modified_at` values within
    `AMBIGUITY_WINDOW_SECONDS`. The list is sorted newest-first, so it suffices
    to compare neighbours — if any pair is within the window then some adjacent
    pair is too.

    Rejected alternative: comparing only the newest against the runner-up, on
    the grounds that a tie further down the list cannot change which transcript
    gets picked. That is true of this instant and false of the next one: a pair
    of files being written seconds apart anywhere in the live set is direct
    evidence that more than one agent window is actively producing output on
    this machine, and any of them can become the newest before the binding is
    written. The spec states the rule in the any-pair form and this implements
    it as stated.
    """
    finder = AGENT_REGISTRY[agent].finder
    live = tuple(t for t in finder(cwd, projects_root) if t.age_seconds <= LIVE_WINDOW_SECONDS)

    ambiguous = any(
        abs(newer.modified_at - older.modified_at) <= AMBIGUITY_WINDOW_SECONDS
        for newer, older in zip(live, live[1:])
    )
    return LiveTranscriptCandidates(candidates=live, ambiguous=ambiguous)


def find_transcript_by_session_id(
    session_id: str,
    agent: str = "claude-code",
    *,
    projects_root: Path | None = None,
) -> DiscoveredTranscript | None:
    """The transcript whose session id is EXACTLY `session_id`, anywhere.

    "Anywhere" is load-bearing, and is the entire reason this exists next to
    `find_live_transcript`. Verified 2026-08-06: a Claude Code session started
    with cwd `/Users/siddharthsingh` writes to
    `~/.claude/projects/-Users-siddharthsingh/<session-id>.jsonl`, so an
    orchestrator resolving the same session against the repo it is serving
    (`~/Dev/synapse` → slug `-Users-siddharthsingh-Dev-synapse`) finds nothing
    at all — not the wrong file, no file. So this searches every project slug
    directory under the agent's root, by passing `cwd=None` to the agent's own
    finder, which is each finder's documented unscoped mode.

    mtime is never consulted, not even the live window. An explicit session id
    is the caller telling us which conversation it IS; a session idle for an
    hour is still that conversation, and refusing to find it because it went
    quiet would defeat the point.

    Dispatch is through `AGENT_REGISTRY` so BOTH registered agents work, and
    matching is on `DiscoveredTranscript.session_id` rather than `path.stem`.
    That distinction is not cosmetic: Codex names its rollouts
    `rollout-<timestamp>-<uuid>.jsonl` and its session id is the embedded uuid
    (`_codex_session_id_from_filename`, mirroring openai/codex's own
    `parse_timestamp_uuid_from_filename`), so a stem comparison would never
    match a Codex id and would silently return None for every Codex caller.
    Claude Code's stem happens to equal its session id; Codex's does not, and
    a third adapter is free to differ again.

    On the pathological duplicate — the same id in two slug directories — the
    newest wins, because the finders sort most-recently-written first and this
    takes the first match. Not worth a refusal path: ids are uuids, and a
    collision means the same conversation was copied, not that two exist.
    """
    for candidate in AGENT_REGISTRY[agent].finder(None, projects_root):
        if candidate.session_id == session_id:
            return candidate
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


def _bind(
    transcript: DiscoveredTranscript,
    shared_id: str,
    contributor: str,
    state_dir: Path,
) -> SessionBinding:
    """Write one `SessionBinding` for one detected transcript.

    Extracted so the explicit-`agent_session_id` path and the detection path
    produce byte-identical binding files — one binding format, one writer.
    Same discipline the spec applies to the orchestrator ("the orchestrator
    must not invent its own binding format"), applied one layer down.
    """
    binding = SessionBinding(
        agent_session_id=transcript.session_id,
        shared_id=shared_id,
        contributor=contributor,
        agent=transcript.agent,
        transcript_path=str(transcript.path),
        pinned_at=datetime.now(timezone.utc),
    )
    write_binding(binding_path_for_agent(state_dir, transcript.agent), binding)
    return binding


def join_session(
    shared_id: str,
    contributor: str,
    cwd: Path,
    state_dir: Path,
    *,
    projects_root: Path | None = None,
    agent_session_id: str | None = None,
) -> list[SessionBinding]:
    """`synapse join <shared_id>` — Plan A.7 / Plan D.2.

    Without an explicit `agent_session_id` (see below), binds every
    currently-detected LIVE Agent Session to `shared_id`, one binding per Agent
    product — looped over `AGENT_REGISTRY`, so a third registered agent needs
    no reshape here. Matches Plan D.2's first failing test: "joining with two
    agents detected produces two bindings."

    `projects_root`, when given, is passed to every registered agent's finder
    the same way — it exists for tests that isolate a single product's
    fixture tree (in which case every *other* agent's finder simply finds
    nothing there, since each dialect's directory layout differs) and for
    real callers there is no such override, so each finder falls back to its
    own default root (`CLAUDE_PROJECTS`/`CODEX_SESSIONS`) independently.

    `agent_session_id`, when given, binds EXACTLY that Agent Session and
    consults mtime nowhere — `find_transcript_by_session_id` searches every
    project slug under the agent's root and matches the id exactly.

    That SUPERSEDES what this docstring said until 2026-08-06, which was:
    "Detection is unchanged: this does not let a human pick a specific
    transcript file. Plan D.3 is explicit that there is no `attach(shared_id)`
    exposed to the agent, and the corresponding design choice here is that
    `join` does not let the *developer* hand-pick one either." That constraint
    is deliberately lifted by
    docs/superpowers/specs/2026-08-06-session-lifecycle-design.md
    ("Requirement: bind the session we started from"), which amends D.3's tool
    list rather than violating it silently: detection alone binds the wrong
    conversation whenever the resolver's `cwd` differs from the conversation's
    (verified 2026-08-06 — a session whose cwd is /Users/siddharthsingh lives
    under the slug `-Users-siddharthsingh`, invisible to a resolver looking at
    `-Users-siddharthsingh-Dev-synapse`), or whenever two windows of the same
    product are live. The caller supplies the id from its own environment
    (`CLAUDE_CODE_SESSION_ID`), which is a fact about the conversation, not a
    human guessing at a filename — the thing D.3 was protecting against.
    Omitting it keeps the old detection behaviour exactly.

    With an explicit `agent_session_id` this binds ONE agent: the one whose
    transcripts contain that id. Rejected alternative: bind that one AND keep
    looping the rest of `AGENT_REGISTRY` by mtime, on the grounds that Plan
    D.2's "joining with two agents detected produces two bindings" should hold
    regardless. It should not hold here — a caller that names a session is
    asking for precision, and quietly attaching a second binding chosen by the
    very heuristic this argument exists to bypass would reintroduce the defect
    one product over, where it is harder to see. A caller wanting both agents
    bound calls `join` again without the argument, or once per session id.

    Contributor registration with the service (Plan D.2's "registers the
    Contributor with the service (POST /members)") deliberately does NOT
    happen here. It was previously recorded as NOT DONE because "no Synapse
    Service exists yet"; one exists now, and the step lives in
    `synapse_orchestrator.relay.Relay._register_members` instead — the
    orchestrator is the single egress and the worker must not open its own
    connection to the service. Registration follows the findings, so every
    Contributor whose work reaches a Shared Session becomes a member of it.
    """
    bound: list[SessionBinding] = []

    if agent_session_id is not None:
        # Which agent owns the id is not something the caller has to know: an
        # agent session id is unique enough to identify itself, and asking a
        # caller that already knows its own CLAUDE_CODE_SESSION_ID to also
        # name its product is one more thing to get wrong. So probe each
        # registered agent's own extraction until one claims it.
        for agent in AGENT_REGISTRY:
            transcript = find_transcript_by_session_id(
                agent_session_id, agent, projects_root=projects_root
            )
            if transcript is not None:
                bound.append(_bind(transcript, shared_id, contributor, state_dir))
                break
        else:
            # Returning [] rather than raising: this is the same "nothing
            # bound" outcome the detection path already has, the CLI already
            # turns it into exit code 1, and the orchestrator's MCP tools may
            # not let an exception escape (spec: "Nothing may raise out of an
            # MCP tool"). The log line names the id because a typo'd or
            # stale id is by far the likeliest cause.
            logger.warning(
                "join_session: no transcript found for agent_session_id %s in any "
                "registered agent's root; nothing bound. NOT falling back to "
                "mtime detection — an explicit id that matches nothing means the "
                "caller is wrong about which session it is, and guessing would "
                "bind a different conversation than the one it asked for.",
                agent_session_id,
            )
        return bound

    for agent in AGENT_REGISTRY:
        transcript = find_live_transcript(cwd, projects_root, agent=agent)
        if transcript is None:
            continue
        bound.append(_bind(transcript, shared_id, contributor, state_dir))

    if not bound:
        logger.warning(
            "join_session: no live Agent Session detected for %s; nothing bound", cwd
        )

    logger.info(
        "join_session: bound %d Agent Session(s); Contributor registration is "
        "the orchestrator's (Relay._register_members), not the worker's",
        len(bound),
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
