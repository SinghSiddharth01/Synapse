---
name: synapse-shared-memory
description: Use when starting work in a joined Synapse Shared Session — before
  exploring an unfamiliar subsystem, when debugging an issue a teammate may
  also be on, when an error looks like it could already be known, when a
  build, deploy, credential or API call fails, when something worked
  yesterday and does not today, or before concluding something is a dead
  end. Applies alongside any other skill already running, including
  debugging, investigation and root-cause procedures — run before their
  evidence-gathering, not after. Checks what the team already learned.
---

# Synapse shared memory

This conversation is joined to a Synapse Shared Session — memory distilled
from every teammate's coding sessions, not just this one. Before spending
time on something a teammate may already have been through:

**This runs alongside other skills, not instead of them.** If a debugging or
investigation skill is already driving, its evidence-gathering phase starts
here: a teammate who already hit this is the cheapest evidence available, and
the only kind that can make the remaining phases unnecessary. Observed
2026-08-05: a session loaded a debugging skill, followed its procedure, and
searched the filesystem for the cause of a failing API key that shared memory
could have named in one call. Query first, then proceed with whatever the
other skill asked for.

1. Call `mcp__synapse__query` with a short description of what you're about
   to do — the subsystem, the error text, the question — not the whole
   task. Retrieval matches on what you give it.
2. If it returns findings, read them before you start. Each one names who
   found it and how many teammates' work it draws on.
3. If it returns nothing, say so plainly: "checked team memory, nothing
   relevant" rather than staying silent about having looked at all. An
   agent that quietly finds nothing reads exactly like one that never
   looked — and the second is the failure this skill exists to prevent.

When you learn something a teammate would benefit from — a root cause, a
dead end and why it was one, a decision and its reasoning — call
`mcp__synapse__contribute` with a few sentences of plain prose. It goes
through the same distiller as everything else Synapse learns, so a
careless contribution is caught at the same check as everything else.

## Say which conversation you are

Pass `agent_session_id` on every `mcp__synapse__query` and
`mcp__synapse__contribute` call, set to **this conversation's own session
id** — Claude Code puts it in `CLAUDE_CODE_SESSION_ID`, and it is the same
id that appears in this conversation's transcript filename. Pass it to
`mcp__synapse__create_session` and `mcp__synapse__join_session` too; their
tool descriptions already ask for it.

This is not bookkeeping. One machine can have several Claude Code windows
open at once, and each of them is a **different participant** in the Shared
Session — a teammate to the others, with its own findings to share and its
own place in the memory. The session id is the only thing that tells them
apart:

- **Attribution.** A finding you contribute is stamped with the
  conversation that found it, so the other window sees it as something
  learned elsewhere rather than as its own echo.
- **Suppression.** `query` hides findings whose every contribution came
  from the conversation asking — they are already in this context window.
  Without the id the server falls back to the machine's most recently
  joined binding, which is a guess the moment a second window is open, and
  a wrong guess either hides a sibling window's work or replays your own
  back at you as team knowledge.

Omit it and nothing errors — the server uses that most-recent binding,
which is exactly right when only one window is open.

`leave_session` takes it too, and there it is the difference between
detaching this conversation and detaching every conversation on the machine
that is in the same Shared Session. Without it there is nothing to tell them
apart, so it detaches all of them and says so in its result.

There is otherwise nothing to attach to: the server already knows which
Shared Session this conversation is in from the local binding
(`synapse-worker join`) made before this conversation connected. If this
machine hasn't joined one yet, both tools say so plainly and tell you the
command to run, rather than failing or not existing.
