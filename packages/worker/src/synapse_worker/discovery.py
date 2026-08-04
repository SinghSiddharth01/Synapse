"""Find the transcript of a live Agent Session.

Detection, never configuration — Plan A.1. The worker is not told which agent is
running; it looks at where agents write and picks the transcript that is being
appended to right now.

Claude Code's layout, confirmed 2026-08-04:

    ~/.claude/projects/<slug>/<session-uuid>.jsonl

`<slug>` is the working directory with drive colons and path separators replaced
by dashes, e.g. C:\\QC_multiverse_hackathon\\Synapse becomes
C--QC-multiverse-hackathon-Synapse.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

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


def find_live_transcript(
    cwd: Path | None = None, projects_root: Path | None = None
) -> DiscoveredTranscript | None:
    """The one transcript currently being written, if any."""
    for transcript in find_claude_code_transcripts(cwd, projects_root):
        if transcript.age_seconds <= LIVE_WINDOW_SECONDS:
            return transcript
    return None
