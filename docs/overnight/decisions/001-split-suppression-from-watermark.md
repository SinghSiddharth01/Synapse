# 001 — Split suppression from the watermark: two identities, two keys

**Status:** decided and IMPLEMENTED (W2, 2026-08-06). Partially reverses commit
`6d6779b`. The behaviour change is one service-side commit, subject
"feat(service): suppression by agent_session, watermark by contributor",
carrying the CONTEXT.md contract note with it — see "How to undo" below.
Two tests were INVERTED by it rather than added around it
(`test_lifecycle.py`'s two-windows and both-fields cases): they asserted the
behaviour this decision reverses, so leaving them green would have meant the
split had not landed.

## Question
`visible_to` (self-suppression) and `last_seen` (the watermark) both need an
identity for "the asker". `6d6779b` keyed BOTH by Contributor, fixing rejoin
(a new conversation no longer reset your place or echoed your own findings
back). The overnight dump then required: two windows on one machine are two
participants, and neither's findings are hidden from the other. One key cannot
satisfy both — two windows of one human must SHARE a watermark but NOT share
suppression. Which key does each concern get?

## Options
**A. Both by Contributor** (status quo, `6d6779b`).
- + Rejoin works: watermark survives a new conversation; your old findings never echo back.
- − Window B never sees window A's findings: every attribution names the same
  contributor, so `visible_to` hides them. Kills W2's whole use case (two
  sessions on the same problem sharing what they learn), invisibly.

**B. Both by Agent Session** (pre-`6d6779b`).
- + Windows see each other.
- − Rejoin replays everything: a new conversation is a new `agent_session_id`
  (transcript filename stem), `last_seen` falls to 0, the briefing reports the
  entire memory as new, and your own earlier findings return as team knowledge.
  This is the exact defect `6d6779b` fixed; restoring it is not on the table.

**C. Split: suppression → `agent_session`, watermark → `contributor`.**
- + Both requirements hold: the watermark is a fact about a PERSON ("how much
  have I not seen"), suppression is a fact about a CONVERSATION ("is this
  already in the context window asking"). Each keyed by what it is about.
- + Restores pre-`6d6779b` suppression while KEEPING its rejoin fix.
- + Wire already carries both fields (additive since `6d6779b`); un-upgraded
  clients that send only `agent_session` get the agent-session comparison
  natively — the old escape hatch (`_legacy_agent_session`) becomes the main
  path and its special-case code is deleted.
- − Two keys to explain instead of one; CONTEXT.md invariant note must be
  rewritten (done in the same commit as the behaviour change).
- − One Contributor's two agents "learning from each other" now includes
  seeing each other's findings — which is the requirement, not a leak.

## Transcript alignment
The dump: "different sessions running on the same host don't get suppressed
under the same flag… Other sessions by that user on the same machine should be
allowed even if they're the same agent." PLAN.md's conflict box pre-decided
the split ("My call, executing unless you say otherwise") and W2 scope item 4
restates it. The split was offered before `6d6779b` and declined for
simplicity; the new requirement forces it.

## Decision
**C.** `retrieval.visible_to` compares `Attribution.agent_session` against the
asking conversation (contributor comparison only as fallback for a client that
sends no `agent_session`). `store.last_seen`/`mark_seen` stay keyed
`(shared_id, contributor)`. Three `visible_to` call sites updated
(api.py watermark, api.py query, retrieval.query_findings); `new_since`/
`mark_seen` untouched. This PARTIALLY REVERSES `6d6779b`: its watermark
re-key is kept, its suppression re-key is undone — the commit bundled two
decisions under one key, and only one of them was right.

## How to undo
The split lands as one service-side commit on `overnight/w2-multisession`
(subject: "feat(service): suppression by agent_session, watermark by
contributor"). To restore contributor-keyed suppression exactly:

    git log --oneline origin/overnight/w2-multisession -- \
        packages/service/src/synapse_service/retrieval.py   # find <split-sha>
    git revert <split-sha>
    uv run pytest -q                                        # split tests will fail; delete or invert
    git push origin HEAD:refs/heads/overnight/w2-multisession

Reverting ONLY this commit keeps per-session bindings and the
`agent_session_id` threading (separate commits, independently revertible);
it returns suppression to `6d6779b` behaviour without touching rejoin.
