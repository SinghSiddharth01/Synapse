# Synapse

Shared working memory for AI-assisted teams. Synapse passively observes each engineer's coding agent, distils what it learns into structured findings on-device, and merges those findings into one team-wide memory that every member's agent can query.

## Language

### Sessions and identity

**Agent Session**:
One conversation with a coding agent, identified by that agent's own id and backed by a single transcript on disk. The unit Synapse observes.
_Avoid_: session (unqualified), chat, conversation, local session

**Shared Session**:
The collaboration unit — a stated purpose plus the members who joined it. Findings from many Agent Sessions merge into exactly one Shared Session.
_Avoid_: session (unqualified), room, channel, team session

**Agent Run**:
The lifetime of a single coding-agent process. A resumed or compacted conversation is the same Agent Session but a new Agent Run. Exists only for the awareness layer and never appears in a contract.
_Avoid_: session start, session, process, instance

**Contributor**:
The human a Finding is attributed to. Members of a Shared Session are Contributors.
_Avoid_: user, member, author, participant

**Attribution**:
Where a Finding came from, at three levels: the Contributor, the Agent Session, and the Agent that produced it. Carried as one value so the levels cannot drift apart. A Finding carries a list of them — a Synthesized Finding has several.
_Avoid_: provenance (means something else here), source, origin, owner

**Agent**:
A coding-agent product Synapse can observe — Claude Code, Codex, or any other. Detected, never configured.
_Avoid_: client, tool, assistant, IDE

### Components

**Edge Worker**:
The local process holding read-only access to transcripts. It detects Agents, segments what they write, and distils it. The only component that ever sees raw transcript content.
_Avoid_: agent, collector, watcher, daemon

**Orchestrator**:
The local hub, one per machine. The single place Findings enter Synapse from this machine, whoever produced them, and the single place anything leaves it. Hosts the MCP server and owns the LocalBinding.
_Avoid_: gateway, proxy, broker, hub

**Producer**:
Anything that emits Findings into the Orchestrator — the Edge Worker, or an Agent via `contribute`. All Producers post the same shape.
_Avoid_: source (means a transcript adapter), emitter, client

**Synapse Service**:
The remote service holding one Shared Memory per Shared Session: ingest, synthesis, retrieval. Runs on any machine and reaches Cloud AI 100 over HTTPS.
_Avoid_: server, backend, cloud, hub

### Shared memory

**Finding**:
One unit of shared knowledge distilled from an Agent Session. Provisional name and provisional taxonomy — see the note below.
_Avoid_: insight, note, memory, fact, observation

**Synthesized Finding**:
A Finding written by synthesis that captures the essence of two or more Findings it judged semantically the same. Carries every source's Attribution and its lineage.
_Avoid_: merged finding, combined finding, summary

**Tombstone**:
A Finding whose essence now lives in a Synthesized Finding. It keeps its text and Attribution but is excluded from retrieval. Not deleted — ingest must recognise its id on retry, Conflicts must follow it forward, and the merge that created it was a small model's judgement.
_Avoid_: deleted, dropped, duplicate, superseded, archived

**Working Memory**:
The bounded prose summary of a Shared Session, rewritten on each merge. Exists to keep the merge prompt fixed-cost regardless of how much the session has accumulated.
_Avoid_: context, summary, memory, digest

**Finding Log**:
The growing curated collection of every Finding pushed to a Shared Session, each carrying synthesis's verdict. What retrieval ranks over.
_Avoid_: store, database, history, archive

**Shared Memory**:
Umbrella for the Working Memory and the Finding Log together — what the product means by "one shared memory".
_Avoid_: the context, the memory (unqualified)

**Conflict**:
Two Findings synthesis judged to disagree, surfaced rather than silently resolved.
_Avoid_: contradiction, disagreement, discrepancy

### Storage and retrieval

**View**:
What Shared Memory currently looks like, obtained by folding the log in order. Always derived, never stored — discard it and fold again at any time.
_Avoid_: state, snapshot, the database, current findings

**Lane**:
One way of finding Candidates — symbols, term overlap, vectors, topic, recency. Lanes are unioned, never intersected. A Finding records which Lanes surfaced it, which is what makes lane yield measurable.
_Avoid_: retriever, index, strategy, search

**Candidate**:
An existing Finding offered to the model for comparison against a new one, or against a teammate's question. Candidates are the *only* thing the model sees of the Finding Log — bounded regardless of how large the log is.
_Avoid_: result, hit, match, neighbour

**Lane yield**:
Of the Candidates a Lane surfaced, the fraction that ended in an accepted merge. The only honest measure of whether a Lane earns its cost.
_Avoid_: precision, accuracy, hit rate

## Notes

- **The shape of Shared Memory is a deliberate first pass and is expected to evolve.** It is where most of the system's future value lives, so it sits behind a storage seam and should not be over-frozen. Splitting Working Memory from Finding Log is the current best structure, not a settled one.
- **"Finding" is provisional, and its taxonomy is open.** The current four types (`learning`, `decision`, `dead_end`, `open_question`) are all *epistemic*; real output may need referential, procedural or artifactual kinds that none of them fit. Neither the term nor the enum should be treated as closed.

- `LocalBinding` maps one Agent Session to one Shared Session, carrying the Contributor.
- Attribution is read at different levels for different jobs: `contributor` for attribution and conflicts, `agent_session` for awareness suppression, `agent` for the cross-agent story.
- Suppression is scoped to the Agent Session, not the Contributor — the justification is "already in that context window", so one Contributor's two agents still learn from each other. A Finding is suppressed only when *every* Attribution on it is that same Agent Session; a Synthesized Finding carrying a teammate's contribution is always shown.
- Semantic sameness is synthesis's judgement, not an id comparison. Ids exist for idempotent ingest and for referencing (`merged_from`, `merged_into`, `Conflict`) — never for deciding whether two Findings mean the same thing.
- `Finding.provenance` (`distilled | contributed`) is a separate axis from Attribution: it records *how* a Finding was produced, not *who* by.
- **A Tombstone is a derived condition, not a written field** (`adr/0004`). A Finding leaves the View because a later `Merged` entry names it as a source — nothing is written onto the original. Everything the term means is unchanged: text and Attribution retained, excluded from retrieval, reachable by id, reversible. `Finding.merged_into` and `Finding.status` are **not written by the service**; read visibility from the View until the team decides whether the ingest API projects them on egress.
- **The Finding Log is append-only.** This is why a Producer may safely re-push its entire durable log after a service restart: a replayed append is inert rather than a write that resurrects merged-away Findings. "Idempotent ingest" is still the external property; it is now guaranteed by structure rather than by careful write logic.
