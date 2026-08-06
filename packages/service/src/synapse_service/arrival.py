"""The arrival summary (W5) — what a LATE JOINER is told, assembled here.

The problem this exists for: a session may be hours old when somebody joins,
and neither of the two obvious answers works. Dumping the whole Finding Log
buries the joiner in a wall of text that costs their whole context window to
read; the pre-W5 arrival briefing (orchestrator/briefing.py) told them only
COUNTS — "team memory holds 6 findings" — which is honest and says nothing
they can act on.

So the answer here is two parts, and keeping them SEPARATE is the point:

  * **ACCUMULATED** — a compact, grouped picture of everything that has piled
    up: the synthesized working memory, the Topic labels, the counts by type,
    and a handful of the most consequential individual findings. Bounded, so
    it does not grow with the session.
  * **NEW SINCE** — only what has landed since this person last read the
    memory. Per CONTRIBUTOR, never per conversation (decisions/001): a second
    Claude Code window is the same person, and opening one must not replay a
    memory they have already read.

For a first-ever joiner the second part is empty by construction and says so,
rather than repeating the first part under a different heading — "everything
is new" is not information.

#### No model call (decisions/004)

Every line below is assembled DETERMINISTICALLY from findings, topic labels
and the working memory that synthesis already wrote. Nothing here calls the
AI-100 or any other provider. That is a decision, not an omission: an arrival
summary is on the critical path of `join_session`, it must answer in
milliseconds, it must never fail a join because a model was down or rate
limited, and the prose the model would be asked to write already exists —
`SessionContext.working_memory` IS the model-written summary of this session,
rewritten on every merge. Summarising a summary would spend a scarce hosted
token budget to restate it. See docs/overnight/decisions/004.

#### Bounds

Every list here is capped and every interpolated string is truncated, for the
same reason `briefing.py` is hard-capped: this text is composed from Finding
prose, which is model output derived from a user's transcript, and it is
handed to another agent as a tool result. Control characters are collapsed out
of every interpolated value so that one finding's text cannot forge an extra
bullet or what looks like a new section header.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace

from synapse_contracts import Conflict, Finding

from synapse_service.retrieval import visible_to

# Bounds. The whole rendered text is capped last, after every part has already
# been capped, so the cap is a backstop rather than the mechanism.
MAX_ARRIVAL_CHARS = 2800
MAX_ITEM_CHARS = 200
MAX_WORKING_MEMORY_CHARS = 600
MAX_PURPOSE_CHARS = 160
MAX_LABEL_CHARS = 90
MAX_MEMBERS = 12
MAX_TOPICS = 5
MAX_HIGHLIGHTS = 6
MAX_NEW_ITEMS = 8

# A Topic is only worth NAMING when it has grouped something. Its label is the
# text of its medoid member (memory.topic_summaries), so a one-member topic
# renders as that finding's own words -- and since the same finding is very
# likely also in `highlights` below, printing it would spend the joiner's
# attention twice to say one thing. Two is the smallest size at which "thread"
# is true rather than decorative.
MIN_TOPIC_SIZE = 2

# Which kinds of Finding a joiner most needs from a session they were not in.
# A decision they would otherwise re-litigate and a dead end they would
# otherwise re-walk are worth more than one more learning, and this ordering is
# the only editorial judgement in the module. Anything not named here sorts
# last, so adding a FindingType member degrades to "shown, but after these"
# rather than to a KeyError (schemas.FindingType's own note asks for exactly
# that: no tolerance machinery, just do not break).
_TYPE_PRIORITY = {"decision": 0, "dead_end": 1, "open_question": 2, "learning": 3}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def _clean(value: object, limit: int) -> str:
    """Collapse control characters out of an interpolated value and cap it.

    Same rule as `briefing._clean`, for the same reason and one layer earlier:
    a newline inside a Finding's text would let that finding's author (a model,
    reading somebody's transcript) write what reads like a new bullet or a new
    section heading in an agent-facing summary.
    """
    text = _CONTROL_CHARS.sub(" ", str(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _who(finding: Finding) -> str:
    """Every contributor on a finding, deduplicated, first-seen order.

    Not `attributions[0]`: a Synthesized Finding carries every source it
    merged from, and crediting only the first turns a pooled insight into one
    person's — the same rule `server.query` already applies when it renders a
    result.
    """
    names = dict.fromkeys(a.contributor for a in finding.attributions)
    return ", ".join(_clean(n, 40) for n in names) or "unattributed"


def _bullet(finding: Finding) -> str:
    return f"- [{finding.type.value}] {_clean(finding.text, MAX_ITEM_CHARS)} — {_who(finding)}"


def _rank(finding: Finding, order: dict[str, int]) -> tuple[int, int]:
    """Most consequential type first, most recent first within a type.

    `order` is arrival position, so negating it is "newest first". Fully
    deterministic — no ties are broken by dict iteration or by clock.
    """
    return (_TYPE_PRIORITY.get(finding.type.value, len(_TYPE_PRIORITY)),
            -order.get(finding.id, 0))


@dataclass(frozen=True)
class ArrivalSummary:
    """The two-part answer, as data. `text` is the rendered form the
    orchestrator hands to the joining agent; the structured fields are what a
    dashboard or a future non-prose consumer would read instead."""

    shared_id: str
    purpose: str
    members: tuple[str, ...]
    version: int
    new_since: int
    first_look: bool
    total: int
    by_type: dict[str, int]
    conflicts: int
    working_memory: str
    topics: tuple[tuple[str, int], ...]
    highlights: tuple[str, ...]
    new_count: int
    new_by_type: dict[str, int]
    new_items: tuple[str, ...]
    text: str = ""
    truncated_members: int = 0

    def to_json(self) -> dict:
        return {
            "purpose": self.purpose,
            "members": list(self.members),
            "version": self.version,
            "new_since": self.new_since,
            "first_look": self.first_look,
            "accumulated": {
                "total": self.total,
                "by_type": dict(self.by_type),
                "conflicts": self.conflicts,
                "working_memory": self.working_memory,
                "topics": [{"label": label, "size": size}
                           for label, size in self.topics],
                "highlights": list(self.highlights),
            },
            "new": {
                "count": self.new_count,
                "by_type": dict(self.new_by_type),
                "items": list(self.new_items),
            },
            "text": self.text,
        }


def _counts(by_type: dict[str, int]) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))


def render(summary: ArrivalSummary) -> str:
    """The two sections, as prose, hard-capped.

    Written as one string with explicit newlines rather than as a list of
    lines because the cap has to be applied to the WHOLE thing: capping each
    part separately still lets the total grow with the number of parts.
    """
    members = ", ".join(summary.members) or "nobody registered yet"
    if summary.truncated_members:
        members += f" (+{summary.truncated_members} more)"
    head = (f"Shared Session {summary.shared_id} — purpose: “{summary.purpose}”. "
            f"Members: {members}.")

    if summary.total == 0:
        accumulated = ("ACCUMULATED — nothing yet. No findings have been recorded in "
                       "this session, so there is no history to catch up on.")
    else:
        lines = [f"ACCUMULATED — what the team has built up here: {summary.total} "
                 f"finding(s) ({_counts(summary.by_type)}), {summary.conflicts} "
                 f"conflict(s), working memory at v{summary.version}."]
        if summary.working_memory:
            lines.append(f"Working memory: {summary.working_memory}")
        if summary.topics:
            lines.append("Threads: " + "; ".join(
                f"“{label}” ({size})" for label, size in summary.topics))
        if summary.highlights:
            lines.append("Most worth knowing:")
            lines.extend(summary.highlights)
        accumulated = "\n".join(lines)

    if summary.first_look:
        new = ("NEW SINCE YOU LAST LOOKED — this is your first look at this session, "
               "so all of the above is new to you.")
    elif summary.new_count == 0:
        new = (f"NEW SINCE YOU LAST LOOKED — nothing new. The memory has moved "
               f"{summary.new_since} version(s) since you last read it, but no "
               "finding you have not already seen came with it.")
    else:
        # `new_since` is a VERSION delta and `new_count` is a FINDING count, and
        # they genuinely disagree: a push is queryable immediately and only bumps
        # the version when synthesis gets round to it. "0 version(s) of movement"
        # next to "1 finding you have not seen" reads like a contradiction, so
        # the zero case says what is actually true instead — the findings are
        # here, the narrative has not caught up. (That this case exists at all is
        # why the new-since slice counts arrivals rather than versions; a
        # version-derived list would have reported nothing new.)
        movement = (f", across {summary.new_since} version(s) of movement"
                    if summary.new_since > 0 else
                    " — pushed since your last read and not yet folded into the "
                    "working memory")
        new = "\n".join(
            [f"NEW SINCE YOU LAST LOOKED — {summary.new_count} finding(s) you have not "
             f"seen ({_counts(summary.new_by_type)}){movement}:"]
            + list(summary.new_items))

    text = f"{head}\n\n{accumulated}\n\n{new}"
    if len(text) > MAX_ARRIVAL_CHARS:
        text = text[: MAX_ARRIVAL_CHARS - 1].rstrip() + "…"
    return text


def compute(store, shared_id: str, *, contributor: str,
            agent_session: str | None) -> ArrivalSummary:
    """Assemble the summary for ONE asker, from the store, with no model call.

    Suppression is applied exactly as `/watermark` and `/query` apply it
    (`retrieval.visible_to`), so the counts a joiner reads here and the counts
    the arrival briefing quotes at them cannot disagree — a joiner told "12
    findings" by one surface and "9" by another has no way to tell which is
    lying. `topics`, `by_type` and `conflicts` are CONTENT fields and are
    suppression-scoped; `version` and `new_since` are CHANGE fields and stay
    global. That split is E3's round-2 adjudication and this does not touch it.
    """
    ctx = store.get_context(shared_id)
    session = store.get_session(shared_id)
    members = tuple(session.members) if session is not None else ()
    truncated_members = max(0, len(members) - MAX_MEMBERS)

    visible = visible_to(store.retrievable(shared_id),
                         asking_agent_session=agent_session,
                         asking_contributor=contributor)
    visible_ids = frozenset(f.id for f in visible)
    by_type = Counter(f.type.value for f in visible)
    conflicts = _conflicts_touching(ctx.conflicts, visible_ids)

    topics = tuple(
        (_clean(t.label, MAX_LABEL_CHARS), t.size)
        for t in store.topic_summaries(shared_id, limit=MAX_TOPICS, only=visible_ids)
        if t.size >= MIN_TOPIC_SIZE)

    # WHAT IS NEW, keyed by CONTRIBUTOR (decisions/001). `seen_count` is how
    # many findings had ever entered this memory when this person last read it;
    # everything recorded after that is new to them, whether or not a synthesis
    # round has run over it yet. Deliberately NOT `new_since` (a version delta):
    # a push is visible immediately and only bumps the version when synthesis
    # gets round to it, so a version-derived list would omit exactly the
    # findings a joiner most wants — the ones that just landed.
    first_look = not store.has_looked(shared_id, contributor)
    new_findings = ([] if first_look
                    else [f for f in store.arrived_after(shared_id,
                                                         store.seen_count(shared_id,
                                                                          contributor))
                          if f.id in visible_ids])
    new_by_type = Counter(f.type.value for f in new_findings)

    order = {fid: i for i, fid in enumerate(f.id for f in visible)}
    new_ids = {f.id for f in new_findings}
    # The accumulated section deliberately EXCLUDES what the new section is
    # about to list. The two sections are separate answers to separate
    # questions, and a finding printed under both headings costs twice and
    # reads as two findings.
    backlog = sorted((f for f in visible if f.id not in new_ids),
                     key=lambda f: _rank(f, order))
    highlights = tuple(_bullet(f) for f in backlog[:MAX_HIGHLIGHTS])
    new_items = tuple(_bullet(f) for f in
                      sorted(new_findings, key=lambda f: _rank(f, order))[:MAX_NEW_ITEMS])

    summary = ArrivalSummary(
        shared_id=shared_id,
        purpose=_clean(ctx.purpose, MAX_PURPOSE_CHARS),
        members=tuple(_clean(m, 40) for m in members[:MAX_MEMBERS]),
        version=ctx.memory_version,
        new_since=ctx.memory_version - store.last_seen(shared_id, contributor),
        first_look=first_look,
        total=len(visible),
        by_type=dict(by_type),
        conflicts=conflicts,
        working_memory=_clean(ctx.working_memory, MAX_WORKING_MEMORY_CHARS),
        topics=topics,
        highlights=highlights,
        new_count=len(new_findings),
        new_by_type=dict(new_by_type),
        new_items=new_items,
        truncated_members=truncated_members,
    )
    return replace(summary, text=render(summary))


def _conflicts_touching(conflicts: list[Conflict], visible_ids: frozenset) -> int:
    """A Conflict counts if EITHER side is visible to this asker — the same
    rule `/watermark` applies, spelled here so the two cannot drift."""
    return sum(1 for c in conflicts
               if c.finding_a in visible_ids or c.finding_b in visible_ids)


class SummaryCache:
    """Memoises `compute` against a FINGERPRINT of the session's content.

    Invalidation is structural rather than hooked. A cache invalidated by an
    explicit call from `push_findings` and `synthesize` would be correct today
    and wrong the moment a fifth route mutates a session and forgets to call it
    — the same failure mode `api._unavailable`'s docstring describes for the
    liveness gate, and the reason that one is a single function too. Here the
    key CONTAINS the two numbers that move whenever the memory changes:

      * the log's entry count, which advances on every push (and on every
        merge, trivia verdict and topic assignment), and
      * `memory_version`, which advances on every verdict round applied.

    A cached entry can therefore only be served while both are unchanged,
    which is exactly "nothing has happened to this session since". No caller
    can forget to invalidate, because no caller invalidates.

    Also in the key: the asker's identity (suppression is per conversation) and
    their watermark (the NEW SINCE section is per person and moves when they
    read). Bounded by insertion order — this is a memo for a handful of live
    sessions, not a store.
    """

    def __init__(self, limit: int = 64) -> None:
        self._limit = limit
        self._entries: dict[tuple, ArrivalSummary] = {}

    def fingerprint(self, store, shared_id: str) -> tuple[int, int]:
        return (len(store.log_entries(shared_id)),
                store.get_context(shared_id).memory_version)

    def get(self, store, shared_id: str, *, contributor: str,
            agent_session: str | None) -> ArrivalSummary:
        key = (shared_id, self.fingerprint(store, shared_id), contributor,
               agent_session, store.seen_count(shared_id, contributor),
               store.has_looked(shared_id, contributor))
        hit = self._entries.get(key)
        if hit is not None:
            return hit
        summary = compute(store, shared_id, contributor=contributor,
                          agent_session=agent_session)
        if len(self._entries) >= self._limit:
            # Oldest insertion first. An LRU would need every read to write,
            # and the population here is small and short-lived.
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = summary
        return summary
