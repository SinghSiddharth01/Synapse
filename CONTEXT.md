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
The local process holding read-only access to transcripts. It detects Agents, segments what they write, triages, and distils. The only component that ever sees raw transcript content.
_Avoid_: agent, collector, watcher, daemon

**Triage**:
The deterministic decision, made in the Edge Worker, about whether a Segment is worth sending to the model at all. Judges durability; the Distiller does not.
_Avoid_: filter, gate, screening, pre-processing

**Distiller**:
The on-device model step that turns a Segment into Findings. Its job is faithful compression and abstraction — it does not decide what is worth keeping.
_Avoid_: summarizer, extractor, condenser, analyzer

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
A Finding whose essence now lives in a Synthesized Finding. It keeps its text and Attribution but is excluded from retrieval. Not deleted — ingest must recognise its id on retry, Conflicts must follow it forward, and the merge that created it was a small model's judgement. It is a **derived condition, not a written field**: a Finding leaves the View because a later `Merged` entry names it as a source, and nothing is written onto the original (`adr/0004`).
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

**Fold**:
The pure function that replays the Finding Log in order and produces the View. Deterministic, no model, cached on log version and discardable. A fold is the *only* way current state is obtained; nothing derives visibility any other way.
_Avoid_: reduce, replay (the recovery path is a resync, not a fold), rebuild (that is re-deriving the indexes), projection

**Topic**:
A cluster of Findings grouped by cosine against a centroid — geometry decides membership, and a label only ever describes it. Topics exist to reach a decision that *governs* a Finding it shares no vocabulary with. A Topic is never an input to what is durable. That governing path is a retrieval lane behind the `topic_lane` flag (`lanes.DEFAULT_TOPIC_LANE`), and **as shipped, the governing lane is off**: it was measured at zero yield (0 partners, 0 uniquely, at 422 findings and at 2,022) and re-measured on this tree, so topics currently earn their place as *labels* in the arrival briefing, not as a retrieval lane.
_Avoid_: cluster, category, tag, label (a label is a Topic's name, not the Topic), theme

## Notes

- **The shape of Shared Memory is a deliberate first pass and is expected to evolve.** It is where most of the system's future value lives, so it sits behind a storage seam and should not be over-frozen. Splitting Working Memory from Finding Log is the current best structure, not a settled one.
- **"Finding" is provisional, and its taxonomy is open.** The current four types (`learning`, `decision`, `dead_end`, `open_question`) are all *epistemic*; real output may need referential, procedural or artifactual kinds that none of them fit. Neither the term nor the enum should be treated as closed.

- `LocalBinding` maps one Agent Session to one Shared Session, carrying the Contributor.
- **One agent invocation is one Agent Session, and the machine holds one binding per Session ⟨CHANGED 2026-08-06, W2⟩.** On disk that is `bindings/<agent>/<agent_session_id>.json`; `bindings/<agent>.json` is still written on every bind, as a mirror of the most recently joined Session, and is what an un-upgraded reader sees. This retires the earlier documented limitation of *one active Agent Session per Agent product per machine*, which was enforced by the single-file path rather than by anything real: a second window of the same product overwrote the first's binding and silently became the same participant. Resolution takes an optional `agent_session_id` — given, it matches that Session exactly and refuses rather than falling back to another window's binding; omitted, it answers with the most recently pinned binding, which is what reading the single file always meant.
- Attribution is read at different levels for different jobs: `agent_session` for **awareness suppression**; `contributor` for attribution, conflicts and **the watermark**; `agent` for the cross-agent story.
- **Suppression is scoped to the Agent Session; the watermark is scoped to the Contributor ⟨SPLIT 2026-08-06, `docs/overnight/decisions/001` — partially reverses the same day's contributor re-key⟩.** A Finding is suppressed only when *every* Attribution on it names the asking Agent Session; a Finding carrying any other conversation's contribution — a teammate's, or *the same human's other window* — is always shown. The `f.attributions and` guard is unchanged: a zero-attribution Finding is visible to everyone (invariant 3). `last_seen`/`new_since` stay keyed by Contributor, so ending a conversation and starting another does not reset your place in the memory.

  The two keys answer different questions. "Is this already in the context window asking?" is a property of ONE CONVERSATION; "how much have I not seen?" is a property of ONE PERSON. The earlier single-key answer had to be wrong for one of them, and was: keyed on `agent_session` alone, leave-and-rejoin replayed the whole memory and handed your own earlier findings back as team knowledge (`last_seen` fell to its `0` default); keyed on `contributor` alone — the fix for that — one human's second window could never see the first's, which is exactly what W2 exists to allow. Each concern is now keyed by what it is about.

  A client that sends only `agent_session` (every pre-re-key one) gets the agent-session comparison natively — the legacy case is the primary key again, and `api._legacy_agent_session` is deleted with the split. A client sending only `contributor` falls back to the contributor comparison. A client that names neither suppresses nothing, because an anonymous asker owns no Finding.
- Semantic sameness is synthesis's judgement, not an id comparison. Ids exist for idempotent ingest and for referencing (`merged_from`, `merged_into`, `Conflict`) — never for deciding whether two Findings mean the same thing.
- `Finding.provenance` (`distilled | contributed`) is a separate axis from Attribution: it records *how* a Finding was produced, not *who* by.
- **A Tombstone is a derived condition, not a written field** (`adr/0004`). A Finding leaves the View because a later `Merged` entry names it as a source — nothing is written onto the original. Everything the term means is unchanged: text and Attribution retained, excluded from retrieval, reachable by id, reversible. `Finding.merged_into` and `Finding.status` are projected onto egress from the View (`adr/0004`, **Option A, closed 2026-08-05**); they are never written by a producer and never read to decide visibility.
- **The Finding Log is append-only.** This is why a Producer may safely re-push its entire durable log after a service restart: a replayed append is inert rather than a write that resurrects merged-away Findings. "Idempotent ingest" is still the external property; it is now guaranteed by structure rather than by careful write logic.
