"""A synthetic corpus of known duplicate pairs, for measuring candidate recall.

Recall is the number that gates everything: if candidate selection does not put
a finding's true partner in front of the model, the merge never happens, nothing
reports it, and the knowledge is lost silently. It is also measurable with **no
model in the loop**, which makes it the one quality signal that can be a
deterministic test rather than an eval.

READ THIS BEFORE QUOTING A NUMBER FROM IT
-----------------------------------------
These pairs were authored by the same author as the lanes they measure. Your own
2026-08-03 trap says it plainly: *do not let one person write both the prompt and
the eval target* — a few-shot that duplicated a fixture produced a "fix" that was
pattern-matching and survived a full measurement cycle. The same failure is
available here, and this file is more susceptible to it than most, because it is
easy to write a pair whose partner the lanes were always going to find.

So this corpus is a **regression harness, not evidence**. It tells you that a
change made recall worse. It does not tell you the lanes work on real distilled
findings, and no number from it belongs in a demo.

The real corpus — genuine two-people-found-the-same-thing pairs from actual
sessions — does not exist yet, and building it is the blocking dependency for
this whole track. Whoever writes the merge logic should not author it.

FOUR DIFFICULTY BANDS
---------------------
Separated so that a single aggregate number cannot hide which lane is carrying
the result:

    symbol      a shared number-with-unit or identifier      symbols lane
    lexical     reworded, overlapping vocabulary             lexical, vector
    paraphrase  same meaning, almost no shared words         vector only
    governing   a decision that constrains the finding       topic only

`governing` is the band that matters most and is least likely to work. It is the
case the whole design was extended for, and under the offline `HashingEmbedder`
it has no signal at all — see semantic.py. Expect it to fail, and treat a pass
as suspicious until a real embedder is wired in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from synapse_contracts import Attribution, Finding, FindingType


class Band(StrEnum):
    SYMBOL = "symbol"
    LEXICAL = "lexical"
    PARAPHRASE = "paraphrase"
    GOVERNING = "governing"


@dataclass(frozen=True)
class Pair:
    """Two findings that should be retrieved for each other, and why."""

    band: Band
    a: str
    b: str


# Deliberately small and hand-written. A generated corpus would measure the
# generator's regularities, which is worse than measuring nothing.
PAIRS: tuple[Pair, ...] = (
    Pair(
        Band.SYMBOL,
        "The timing window is 40 ms.",
        "It fails when the delay exceeds ~40 ms under load.",
    ),
    Pair(
        Band.SYMBOL,
        "default_pool_size is left at its shipped value.",
        "Nobody has changed default_pool_size, which is why the ceiling is fixed.",
    ),
    Pair(
        Band.SYMBOL,
        "The qairt bundle caps usable context at 4096 tokens.",
        "Prompts over 4096 tokens are silently truncated by the qairt runtime.",
    ),
    Pair(
        Band.SYMBOL,
        "mcp must be pinned to 1.9.4 on this machine.",
        "Anything past 1.9.4 pulls a dependency with no ARM64 wheel.",
    ),
    Pair(
        Band.LEXICAL,
        "The connection pool is created per worker rather than shared.",
        "Each worker builds its own connection pool instead of sharing one.",
    ),
    Pair(
        Band.LEXICAL,
        "Retries are capped in the gateway configuration.",
        "The gateway config puts a cap on how many times a request retries.",
    ),
    Pair(
        Band.LEXICAL,
        "Disabling keepalive made the failure rate worse.",
        "Turning keepalive off increased how often it failed.",
    ),
    Pair(
        Band.PARAPHRASE,
        "The service runs out of available connections when traffic spikes.",
        "Under a sudden burst there is nothing left in the pool to hand out.",
    ),
    Pair(
        Band.PARAPHRASE,
        "Requests queue up behind a single slow downstream call.",
        "One sluggish dependency backs everything else up behind it.",
    ),
    Pair(
        Band.GOVERNING,
        "We decided not to tune pool sizing; it is not the cause.",
        "Raising the pool to 50 changed nothing about the failure rate.",
    ),
    Pair(
        Band.GOVERNING,
        "Treat the NPU as a background path, never the latency-critical one.",
        "Decode on the accelerator measured slower than the CPU at this size.",
    ),
)

# Padding. Distractors exist to make retrieval *hard* — a corpus of eleven pairs
# would score perfectly on any lane and tell you nothing about the regime this
# design is for, which is a log far larger than any context window.
_DISTRACTOR_TEMPLATES: tuple[str, ...] = (
    "The {thing} was rebuilt after the {event} and now reports cleanly.",
    "Nobody has verified whether {thing} survives a restart.",
    "{thing} is configured through an environment variable, not a flag.",
    "Reading {thing} twice in one pass produced inconsistent results.",
    "The {event} left {thing} in a state the tests do not cover.",
    "Documentation for {thing} describes behaviour that no longer matches.",
    "{thing} is only reachable once the {event} has completed.",
    "Switching {thing} off removed the warning but not the underlying problem.",
)

_THINGS: tuple[str, ...] = (
    "follower", "segmenter", "prompt pack", "capability record", "watermark",
    "binding file", "transport shell", "eval harness", "sink", "registry",
    "discovery pass", "replay buffer", "canary check", "token budget",
)

_EVENTS: tuple[str, ...] = (
    "compaction", "restart", "rotation", "resync", "cold start",
    "reconnect", "migration", "first run",
)


@dataclass(frozen=True)
class CorpusEntry:
    finding: Finding
    band: Band | None
    partner_id: str | None


def build(*, distractors: int = 0, contributor: str = "aditya") -> list[CorpusEntry]:
    """Findings for every pair, plus `distractors` unrelated ones.

    Both halves of a pair are attributed to *different* contributors, because
    same-contributor duplicates are the easy case and cross-contributor pooling
    is what the product exists to do.
    """
    base = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
    entries: list[CorpusEntry] = []

    for index, pair in enumerate(PAIRS):
        id_a, id_b = f"pair-{index:02d}-a", f"pair-{index:02d}-b"
        entries.append(
            CorpusEntry(
                finding=_finding(id_a, pair.a, contributor, base, index * 2),
                band=pair.band,
                partner_id=id_b,
            )
        )
        entries.append(
            CorpusEntry(
                finding=_finding(id_b, pair.b, "akhil", base, index * 2 + 1),
                band=pair.band,
                partner_id=id_a,
            )
        )

    for index in range(distractors):
        template = _DISTRACTOR_TEMPLATES[index % len(_DISTRACTOR_TEMPLATES)]
        text = template.format(
            thing=_THINGS[(index * 7) % len(_THINGS)],
            event=_EVENTS[(index * 5) % len(_EVENTS)],
        )
        entries.append(
            CorpusEntry(
                finding=_finding(
                    f"noise-{index:05d}", text, "akhil", base, 1000 + index
                ),
                band=None,
                partner_id=None,
            )
        )

    return entries


def _finding(
    finding_id: str, text: str, contributor: str, base: datetime, offset: int
) -> Finding:
    return Finding(
        id=finding_id,
        type=FindingType.LEARNING,
        text=text,
        attributions=[
            Attribution(
                contributor=contributor,
                agent_session=f"sess-{contributor}",
                agent="claude-code",
            )
        ],
        ts=base + timedelta(minutes=offset),
    )
