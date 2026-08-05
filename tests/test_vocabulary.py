"""CONTEXT.md is the one document every plan is written against.

Plan E Task E.4. This exists because a `git merge` resolution can delete a
definition with no error anywhere: the brain branch forked before E2, so taking
its CONTEXT.md wholesale silently un-says `adr/0003` by deleting **Triage** and
**Distiller**. Cheap test, expensive failure.
"""

from __future__ import annotations

import pathlib
import re

# Imported, not hardcoded: the topic-lane default is set by a MEASUREMENT in
# Task 7 Step 5, and this file is written after it. Importing the constant is
# also what makes running this task early an ImportError rather than a subtly
# false document (Plan E revision 2, ordering).
from synapse_service.lanes import DEFAULT_TOPIC_LANE

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTEXT = ROOT / "CONTEXT.md"

# Every term this repo's plans use in prose must exist as a bolded defined term.
# The E2-era four are listed FIRST because they are the ones a bad merge deletes.
REQUIRED_TERMS = [
    "Triage",
    "Distiller",
    "Edge Worker",
    "Tombstone",
    "View",
    "Lane",
    "Candidate",
    "Lane yield",
    "Fold",
    "Topic",
]


def _defined_terms(text: str) -> set[str]:
    """A defined term is a line of the form `**Term**:` or `**Term** (note):`."""
    return {m.group(1).strip()
            for m in re.finditer(r"^\*\*(.+?)\*\*\s*(?:\(.*?\))?\s*:", text, re.MULTILINE)}


def test_every_term_the_plans_use_is_defined_in_context_md():
    defined = _defined_terms(CONTEXT.read_text(encoding="utf-8"))
    missing = [term for term in REQUIRED_TERMS if term not in defined]
    assert missing == [], (
        f"CONTEXT.md is missing bolded definitions for {missing}. "
        "If a merge resolution deleted one, restore it — do not delete it from "
        "this list.")


def test_the_edge_worker_still_triages():
    """`adr/0003` splits durability judgment between triage upstream and
    synthesis downstream. The brain branch's Edge Worker definition predates
    that and does not mention triage at all."""
    text = CONTEXT.read_text(encoding="utf-8")
    edge_worker = text.split("**Edge Worker**:", 1)[1].split("**", 1)[0]
    assert "triages" in edge_worker


# The two sentences that describe the projection question as still open. They
# are DIFFERENT strings in the two files, which is why one grep for one phrase
# is not enough:
#   adr/0004 Consequences : "a **team decision, not a resolved one**"
#   CONTEXT.md Notes      : "until the team decides whether the ingest API
#                            projects them on egress"
_STALE_PHRASES = (
    "team decision, not a resolved one",
    "until the team decides whether the ingest API projects them on egress",
)


def test_the_projection_question_is_no_longer_described_as_open():
    """adr/0004 left Option A vs Option B open; Plan E.3 closed it as Option A.
    A doc still calling it undecided is how someone re-opens a settled call two
    days before a demo.

    Both phrases are checked. Checking only the ADR's wording passes VACUOUSLY
    against CONTEXT.md, whose Notes bullet says it a different way -- and
    CONTEXT.md is the file this test exists for."""
    stale = []
    for path in sorted((ROOT / "docs").rglob("*.md")) + [CONTEXT]:
        text = path.read_text(encoding="utf-8")
        # The ADR itself keeps its original sentence -- that is the sentence its
        # own Amendment quotes and closes -- as do the plans that record the
        # closure. Everywhere else, it is stale.
        exempt = (path.name.startswith("0004-") or "plan-e-brain" in path.name
                  or "brain-integration" in path.name or "e5-brain" in path.name)
        if exempt:
            continue
        for phrase in _STALE_PHRASES:
            if phrase in text:
                stale.append(f"{path.relative_to(ROOT)}: {phrase!r}")
    assert stale == [], f"these still call the merged_into/status question open: {stale}"


def _entry(text: str, term: str) -> str:
    """The body of one bolded definition, up to the next one."""
    return text.split(f"**{term}**:", 1)[1].split("\n**", 1)[0]


def test_the_tombstone_ENTRY_says_derived_condition_not_just_the_notes():
    """Scoped to the Tombstone ENTRY on purpose. Task 2's auto-merge already
    brought a Notes bullet containing 'derived condition', so a whole-file
    substring check passes before this task edits anything -- vacuous, and
    vacuous in the exact place the plan claims to have changed something."""
    entry = _entry(CONTEXT.read_text(encoding="utf-8"), "Tombstone")
    assert "derived condition" in entry
    assert "adr/0004" in entry
    assert "_Avoid_: deleted, dropped, duplicate" in entry   # kept as it stands


def test_the_topic_entry_agrees_with_the_shipped_default():
    """The definition must describe the system that SHIPS, whichever way the
    measurement went.

    Revision 1 asserted the literal substring 'measured at zero yield'. That
    hardcodes one outcome of a measurement taken in Task 7 Step 5 -- and in the
    lane-ON outcome CONTEXT.md would have shipped a false statement about the
    shipped system with this test still green, which is the exact failure shape
    this repo kills everywhere else. So: the entry must NAME the flag, and it
    must AGREE with the constant, and this test does not care which way the
    constant went."""
    entry = _entry(CONTEXT.read_text(encoding="utf-8"), "Topic")
    assert "topic_lane" in entry, "the Topic entry must name the flag that governs it"
    if DEFAULT_TOPIC_LANE:
        assert "the governing lane is ON" in entry
    else:
        assert "the governing lane is off" in entry


def test_the_notes_state_option_a_as_closed():
    assert "Option A, closed 2026-08-05" in CONTEXT.read_text(encoding="utf-8")


def test_no_document_still_glosses_memory_version_as_merges_completed():
    """`SessionContext.memory_version` counts VERDICT ROUNDS APPLIED. The
    'merges completed' gloss was wrong in the design memo, in Plan E, and in
    revision 0 of the exec plan; Task 3 Step 2b corrects the two spec documents
    and this is what stops it coming back.

    Same shape as test_the_projection_question_is_no_longer_described_as_open,
    which is the one mechanism in this repo that has actually caught this class
    of drift. A line MAY carry the phrase if it also carries a correction
    marker (that is how a correction records what it corrected) -- checked
    line-by-line, not file-by-file, because `plan-e-brain.md` and
    `brain-integration-design.md` are exactly the two documents Task 3 Step 2b
    corrects, and a filename exemption for either one would un-pin the
    correction it exists to hold: verified 2026-08-05, every "merges
    completed" occurrence in this worktree's `docs/` tree already carries its
    `CORRECTION`/`corrected 2026-08-05` marker on the same line, so the
    line-level rule alone is green with ZERO filename exemptions here.

    The one file that needs a name-based exemption is `e5-brain`: the exec
    plan doc (`docs/plans/exec/2026-08-05-e5-brain-integration.md`) NAMES and
    QUOTES the wrong gloss at length while fixing it, on lines its own prose
    does not mark with `CORRECTION` (it is not itself a correction, it is
    the plan that ORDERS one) -- true the moment this branch reaches `main`,
    which already carries that file. Do not widen this back to `plan-e-brain`
    or `brain-integration`: if this guard is red on either of those two
    files, Task 3 Step 2b was skipped or dropped -- fix the documents, do not
    re-add the exemption."""
    offenders = []
    for path in sorted((ROOT / "docs").rglob("*.md")):
        exempt = "e5-brain" in path.name
        if exempt:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "merges completed" not in line:
                continue
            if "CORRECTION" in line or "corrected 2026-08-05" in line:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert offenders == [], (
        "these still gloss memory_version as 'merges completed'; it counts verdict "
        f"rounds applied (synthesis.py:273 bumps unconditionally): {offenders}")


# The test above globs `docs/**/*.md` for one literal string. Neither catches
# the canonical contract: SessionContext lives in packages/contracts, not
# docs/, and its docstring said "increments once per merge" -- true-sounding,
# still wrong, and a different string than "merges completed". A reader who
# opens the type that OWNS the field is exactly the reader this guard exists
# to protect, so it gets its own narrow, targeted check rather than a stretch
# of the glob above into `packages/` (which would need its own exemption
# logic for every docstring elsewhere that legitimately says "merge").
_SCHEMAS = (ROOT / "packages" / "contracts" / "src" / "synapse_contracts" / "schemas.py")


def test_the_canonical_sessioncontext_docstring_does_not_gloss_it_either():
    """`SessionContext.memory_version` (schemas.py) is the field's one
    canonical definition -- every track reads this docstring, not a doc under
    `docs/`. Same conflation as the test above, different file, different
    wording ('increments once per merge' vs. 'merges completed'), so it needs
    its own check: fixing the docs/ prose does not fix this one, and this one
    is what a reader actually hits when they go look up what the field on the
    Pydantic model means."""
    text = _SCHEMAS.read_text(encoding="utf-8")
    entry = text.split("class SessionContext", 1)[1].split('"""', 2)[1]
    # Line-scoped and CORRECTION-aware, same rule as the docs/ guard above:
    # the corrected docstring itself legitimately says "not once per merge"
    # while explaining the fix, and a bare substring check would flag that
    # line too.
    offenders = [line for line in entry.splitlines()
                 if "once per merge" in line and "CORRECTION" not in line]
    assert offenders == [], (
        "SessionContext's own docstring still glosses memory_version as incrementing "
        f"once per merge (uncorrected): {offenders}; it counts verdict rounds applied, "
        "merges or not (synthesis.py bumps unconditionally on every structurally-valid verdict)")
