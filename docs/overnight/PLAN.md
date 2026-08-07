# Overnight plan — 2026-08-06

Everything in the brain-dump, cleaned up, grouped, and sequenced for autonomous
execution. **Nothing here is implemented yet.** Review and correct in the
morning; I will have been working from it.

Artifacts you will find when you wake up, all under `docs/overnight/`:

| file | what it is |
|---|---|
| `PLAN.md` | this — the agreed scope |
| `TLDR.md` | **read this first.** One page: what shipped, what broke, what needs you |
| `STATE.md` | living status per workstream, updated as each lands |
| `LOG.md` | chronological, timestamped, appended as I go |
| `decisions/NNN-*.md` | one per A-vs-B call I made without you, incl. how to reverse it |
| `parked/*.md` | dead ends, with full state so they can be resumed cold |

---

## ⚠️ Conflict with tonight's shipped work — read first

**W2 (suppression scoping) reverses part of `6d6779b`.**

Tonight we re-keyed **both** the watermark and self-suppression from
`agent_session` to `contributor`, because a new Claude Code conversation is a new
`agent_session_id` and that silently reset your place on rejoin.

Your dump now says:

> different sessions running on the same host don't get suppressed under the same
> flag. Each suppression should only be limited to that session for that user.
> Other sessions by that user on the same machine should be allowed even if
> they're the same agent

Those are incompatible as a single key. Two windows for the same human must
**share** a watermark but **not** share suppression.

**My call, executing unless you say otherwise:** split them — which is the option
I offered originally and you declined in favour of reusing `contributor`. Your
new requirement forces it.

| concern | key | rationale |
|---|---|---|
| watermark (`last_seen`) | `contributor` | must survive a new conversation, else rejoin replays everything |
| suppression (`visible_to`) | `agent_session` | window A's findings must be visible in window B |

Cost: three call sites and a contract note. It restores the pre-`6d6779b`
suppression behaviour while keeping the rejoin fix. Documented in
`decisions/001-split-suppression-from-watermark.md`.

---

## ✅ Already fixed by the team tonight — do not redo

Five commits landed after the dump was written. They cover most of what W6 was
for. Revised scope is folded into each workstream below; recorded here so no
agent re-derives a fix that already shipped.

| commit | what it closed |
|---|---|
| `7c42e96` | **The chunking bug.** Chunking could only split *between* events and admitted the first unconditionally, so one assistant message longer than the budget produced an unfittable Segment, failed twice, and the whole turn was dropped — silently. `_split_oversized()` now cuts at the nearest preceding newline. Also: the retry was resending byte-identical text to a temperature-0 model, which cannot decode differently — attempt 2 now appends a corrective instruction. |
| `7418a63` | **The context-out-of-bounds bug you described.** `max_tokens` 900 against a `response_reserve` of 500 meant a full segment came to 4496 against a 4096 ceiling; near the ceiling the model degenerates into repetition before truncating, so it read as "unparseable" rather than "too big". `config.effective_max_tokens` now clamps rather than raising, so existing configs still boot. Also raised the producer flush timeout 30s → 120s; every batch had been paying a full timeout and landing only on the WAL retry. |
| `c077a51` | A GenieX 400 for `context_length_exceeded` **carries partial findings in the body**; `raise_for_status()` was discarding them, so the JSON-repair path could never run on the one case it was written for. Now salvaged. `finish_reason == length` logged distinctly from malformed. |
| `282fd07` | Hosting with `--npu` gave the service a synthesizer that **could never work** — `AIC100Provider` needs `/completions`, GenieX serves only `/chat/completions`, so every synthesis was a 410. Deterministic, not flaky, and invisible because the stand-in path works. |
| `f5794b8` | ADR 0005 — synthesis budget derived, merge rate governed. |

**Three open items those commits explicitly name, now adopted into this plan:**

1. **`retrieval.py`'s bare `except Exception: return []`** — named in `282fd07` as
   *"what made this silent rather than loud"*, deliberately not fixed there
   because it changes the query contract. This one line is why a dead
   synthesizer looked like an empty result set. **It is also exactly what will
   mask a dead GenieX**, so it moves into W1 rather than waiting.
2. **Round-robin is dead code at both layers** (ADR 0005).
3. **~10 keys needed to hold 60s latency** (ADR 0005) — a capacity fact for the
   demo, not a code change. Goes to `HUMAN-TODO.md`.

---

## Workstreams

Ordered by demo risk, not by size. Each is independently mergeable.

### W1 — Geniex idle death *(demo-critical)*

**Symptom:** after X minutes idle, the process is alive but the HTTP server stops
serving; the URL goes unreachable.

⟨EXPANDED 2026-08-06 — `282fd07` handed W1 its missing half.⟩

**The observability half comes first.** `282fd07` names
`retrieval.py`'s bare `except Exception: return []` as *"what made this silent
rather than loud"* — a dead synthesizer became an empty findings list and a 200.
That same swallow will hide a dead GenieX behind "no results", which is the
worst possible demo failure: everything looks fine and the brain is simply
empty. Fixing the supervisor without fixing this means the supervisor might work
and nobody could tell.

So: distinguish *"the model call failed"* from *"nothing matched"* at that seam.
`282fd07` deferred it because it changes the query contract — that is a real
cost, so it goes in `decisions/008` with the contract change written out.

**Deliverable:** the app survives it without human intervention.
- Health probe on the model seam, with a liveness definition that distinguishes
  "slow" from "dead" (a hung socket is not a crash).
- Supervised restart from the app itself, with backoff and a cap, so a genuinely
  broken NPU doesn't become a restart loop on stage.
- Every restart is logged loudly — a silent self-heal that hides a dying box is
  worse than the crash.

**CI:** you asked for it if testable. It is, at the seam: a fake endpoint that
accepts a connection then stops responding, asserting the supervisor notices and
recovers. That does not prove the real Geniex bug, and the test will say so.

**Blockage risk:** medium — reproducing the real idle timeout may need the NPU
box. If so, I build and test the supervisor against the fake and park the
real-hardware confirmation.

**Models:** Opus (dev) · Fable (design of the liveness rule).

---

### W2 — Multiple concurrent agent sessions *(correctness + capability)*

⟨REVISED 2026-08-06 after clarification — this is now the second-largest
workstream, not a three-call-site fix.⟩

**The real requirement.** One agent invocation = one session. A second Claude
Code window on the same machine is a **different worker** with its own entry, and
neither window's findings are hidden from the other. The use case is two sessions
debugging the same problem from different angles and sharing what they learn.

**What blocks it today.** `binding_path_for_agent` writes
`bindings/<agent>.json` — **one file per Agent PRODUCT**, not per session. Its
own docstring cites Plan D's limitation: *"one active Agent Session per Agent
product per machine"*. So a second window overwrites the first's binding and
becomes the same participant.

**What unblocks it.** Tonight's lifecycle work already added explicit
`agent_session_id` (from `CLAUDE_CODE_SESSION_ID`, which *is* the transcript
stem). That is precisely the identity MCP itself cannot supply. The enabler
exists; it is not yet threaded through.

**Scope:**
1. **Per-session bindings** — `bindings/<agent>/<agent_session_id>.json`, with a
   migration path from the single-file layout.
2. **Every tool call carries its session** — extend the optional
   `agent_session_id` argument from create/join to `query` and `contribute`, so
   the orchestrator can tell which conversation is speaking on every call. Falls
   back to the current single-binding behaviour when absent, so nothing breaks.
3. **The awareness pack teaches agents to pass it**, or the capability exists and
   nobody uses it.
4. **Suppression on `agent_session`; watermark on `contributor`** — the split
   from the conflict box, now with a concrete reason beyond rejoin.
5. **One orchestrator, N MCP clients.** No extra worker process — the
   orchestrator already runs as one HTTP server; it must stop assuming one
   binding.
6. Generalise across products, not just Claude Code — `AGENT_REGISTRY` already
   has Codex.

**Why this matters beyond correctness.** If it works, the workflow agents I run
tonight can talk to each other through Synapse — genuinely shared context between
agents working different angles of the same problem. **I will dogfood it:** once
it lands, later workstreams' agents contribute and query as distinct
participants, and whether that actually helps goes in `TLDR.md` as a finding
either way. That is also the strongest possible demo material.

**Dependency to note:** W4 Page 1 wants *"all our workers who joined, when they
left, where they are in their session"* — that is only meaningful once
participants are per-session. **W2 lands before W4 Page 1.**

**Blockage risk:** medium. The binding layout change touches worker,
orchestrator, and the pack. Mitigation: land per-session bindings behind
backward-compatible resolution first, so a half-finished state still runs.

**Models:** Fable (identity model + migration design) · Opus (dev) · full review
shape.

---

### W3 — Worker rate limiting + provider queueing

⟨NARROWED 2026-08-06 — the service-side half already shipped.⟩

ADR 0005 records that **merge rate is already governed** service-side
(`MERGE_MIN_INTERVAL_S`, plus a synthesis token/request governor). That is not
what you asked for. Yours is the **worker → provider** side: *"not keep
accumulating requests, and also not spawn infinite sessions in parallel"*. Those
are different seams and the shipped one does not cover it.

**Deliverable:** the worker bounds what it sends to a provider.
- Rate limit inbound work rather than accumulating an unbounded backlog.
- Bounded concurrency — no unbounded parallel provider calls.
- Shed or defer beyond the bound, visibly, rather than queueing forever.
- **Close the metering hole the audit already found:** `/query` shares one
  provider object and one hourly key ceiling with synthesis but is never charged
  to `_spend`, so 20 queries and no pushes exhaust the key while `_affordable()`
  still returns `True`. That reproduces the exact "findings landed, memory
  unchanged" symptom the governor exists to make visible.
- **Round-robin is dead code at both layers** (ADR 0005) — either wire it or
  delete it; leaving dead capacity code next to a rate limiter is how the next
  person concludes there is more headroom than there is.

**Paired investigation** — you asked three questions; these are answered in
writing as `docs/overnight/FLOW.md`, with file:line citations, not prose:

1. **The MCP `contribute` path** — end to end: what houses it, how it is wired,
   where the model call happens, what is bounded and what is not.
2. **Listener mode** — how output is batched/chunked today, and the actual limits
   in the current config (not the defaults in the code — the effective values).
3. **AI-100 config** — prompt size, context window, how much is spent on working
   memory, and where each number is set.

That investigation runs **first** because W6 depends on its answers.

**Blockage risk:** low for investigation, medium for the limiter (needs a policy
choice — see decisions).

**Models:** Fable (investigation, needs judgment) · Opus (limiter).

---

### W4 — Dashboard *(largest; demo-facing)*

Your requirement in one line: **the top level shows the state of the brain, not
the log.**

**Page 1 — Memory dashboard (default)**
- Workers: who is connected, joined/left, where they are in their session, last
  query time, health.
- Count of connected users.
- Working memory: current content, when last updated, last 5–10 revisions.
- Only top-level relevant state. Production-grade feel.

**Page 2 — Memory**
- The findings actually *in* memory now — what can be queried or appended to.
- Clean, filterable.

**Page 3 — History / raw**
- Everything the current dashboard shows today, moved here.

**Cross-cutting**
- Structured log rows, not raw JSON dumps: **author · text · contributed vs
  listened**.
- Every row **inspectable** — click to expand properties and values. Today it is
  flat text with no affordance.
- Basic filters (author, type, contributed/listened, time).
- Modern, polished, industry-standard layout. This may end up in the demo video.

**Constraint you set, which I will hold:** *don't invent new things* — build on
what the current plumbing already exposes. Where the data doesn't exist, I note
it rather than fabricating a metric.

**⟨SCOPE CUT 2026-08-06⟩ Page 1 only, and it does not get the night.** Your
words: the dashboard is *"a sink, not a source of any information"*. So:

- **W4a — Page 1 basics.** Runs after W2 (it needs per-session participants to
  have anything true to show). Ships or does not; either way it does not block.
- **W4b — Pages 2 and 3, filters, inspectable rows.** Only started once every
  other workstream is done and time remains. If the night runs out here, nothing
  of value is lost.

Splitting there because Page 1 is the only part that shows something we cannot
already see, and the rest is a nicer view of data we already have.

**Blockage risk:** low technically, high on scope — hence the cut.

**Models:** Fable (IA + visual direction) · Opus (implementation) · frontend
design skill.

---

### W5 — MCP arrival summary

**Problem:** a late joiner has no idea what has happened. Dumping everything is
wrong; the session may be hours old.

**Deliverable:** on join, a *summary* of what has accumulated, plus what is new
since. Add the endpoint if none exists. Summarisation may run on the AI-100
backend.

**Interaction with existing work:** the arrival briefing already exists
(`briefing.py`) and already carries a watermark. This is an upgrade to it, not a
new mechanism — I will extend rather than duplicate.

**Blockage risk:** low.

**Models:** Fable (what belongs in a summary) · Opus (endpoint + wiring).

---

### W6 — Stress testing: chunking and context bounds

⟨RETARGETED 2026-08-06 — the bugs are fixed; the **tests** are the gap.⟩

`7c42e96`, `7418a63` and `c077a51` fixed the chunking and context-overflow
defects. Writing more fixes would produce contradictory behaviour. What is
actually missing is regression cover, and one hard limit you asked for:

1. **Do the fixes have tests?** Audit first. `282fd07` admits `_provider()` *"had
   no test coverage at all, which is how a synthesizer that could not work
   against the NPU shipped"* — so absence of cover is a live pattern here, not a
   hypothetical. Every fix without a test gets one.
2. **Haiku pinned to 4K** — untouched by any commit, still open. Enforced in the
   provider with a config override, per `decisions/006`.
3. **Stress the seams with large blobs**, using Haiku locally as you asked, and
   the AI-100 arm. Assert bounds *hold* rather than observing that they did.
4. **Property-style bound assertions** rather than one-off fixtures. ADR 0005
   records that the truncation-guard test *"fabricated a fixture the host never
   sends"* — a test that cannot fail is worse than no test, and that is the
   failure mode to design against here.
5. The degenerate-repetition case specifically: near the ceiling the model
   repeats before it truncates, so "unparseable" and "over budget" look
   identical. Cover both, distinctly.

**Depends on:** W3's `FLOW.md` for the *effective* current limits — which the
new `effective_max_tokens` clamp changes, so the investigation must read
post-`7418a63` config, not the values in the dump.

**Blockage risk:** low.

**Models:** Opus (tests) · Fable (deciding what "out of bounds" means per seam,
and auditing which fixes lack cover).

---

### W7 — Confirm lifecycle actually works live

`create_session` / `join_session` / `leave_session` / `end_session` are shipped
and unit-tested, and the four tools appeared in a live MCP session tonight — but
the **end-to-end arc has never been driven against a real running stack**.

Deliverable: drive it, record the result, fix what breaks.

**Constraint:** the rehearsal script hardcodes `8899`/`8787` and silently
measures whatever is already running — that is how test fixtures got into your
real session tonight. **I will not run it against a live stack.** Port-shifted
copy only. Fixing that hardcoding is folded in here.

**Blockage risk:** medium — needs a stack up. I control that on a shifted port.

---

### W8 — Install scripts, Windows + macOS *(your explicit agent pipeline)*

You specified the shape; I will follow it exactly:

1. **Scoping agent** — what the install scripts must cover, both OSes.
2. **Review agent** — compares the scope against the **demo transcript** to
   confirm alignment with the transcript's goals.
3. **Planner agent** — specs, test cases, acceptance criteria.
4. **Opus developer agents** — implementation, parallel where separable.
5. **Review → fix → verify agents**, as in the workflows we have been running.

**Includes:** the Windows Unicode encoder fix — special characters fail to decode
until an env var is set; that becomes part of the install rather than a footnote.

**Blockage risk:** low. Highest parallelism of any workstream.

---

### W9 / W11 — Transcript analysis and the two checklists

Both documents are now pulled: `docs/demo-transcripts.txt` (268 lines — notes
extracted from recording-1, recording-2, **and the hackathon rules PDF**) and
`docs/DEMO-READINESS-CHECKLIST.md`, freshly trimmed to 4 open items.

The transcript is short enough that parallelism buys **angles, not throughput**.
Five readers, each with a different lens, then one synthesiser:

| lens | looking for |
|---|---|
| narrative | what the demo must *show*, and every claim the slides commit us to |
| technical commitments | behaviour promised in the recording that must exist in code |
| hackathon rules | judging criteria and compliance obligations from the PDF |
| gap analysis | claimed vs what actually ships today, with file:line |
| todo extraction | every actionable item, each classified **agentic** or **human** |

**Two output files, per your instruction:**

- `docs/DEMO-READINESS-CHECKLIST.md` — **agentic only.** Things I can finish.
  Stays the working checklist.
- `docs/HUMAN-TODO.md` — **new.** Everything needing a person: hardware, power
  measurement, judging submissions, recording, anything needing a decision or a
  body in a room. Restored from the transcript rather than left dropped.

The current 4 open checklist items look predominantly *human* to me (power
measurement, A/B latency numbers, a real two-machine run, competitive due
diligence), so expect that file to move most of them across and the agentic
checklist to be repopulated from the transcript.

Also folds in: any transcript item that belongs to an existing workstream gets
routed there rather than duplicated, and the gap-analysis lens feeds W10's
consistency review.

**Blockage risk:** none. Pure read + write.

**Models:** Fable (all five lenses — every one of them is judgment) · Fable
(synthesis).

---

## ⚠️ Overlap with the other session's just-landed work

Pulling brought in four commits from the concurrent session that sit **directly
on top of W6's territory**:

| commit | overlaps |
|---|---|
| `7c42e96` fix(worker): split an event too big for the budget, and make the retry a retry | chunking — W6 |
| `7418a63` fix(config): honour the response_reserve, and stop timing out on a live push | context budget — W6 |
| `282fd07` fix(service): a synthesizer that speaks the endpoint GenieX actually serves | model seam — W1, W6 |
| `c077a51` fix(providers): a 400 that carries findings is not a 400 with nothing in it | provider error handling — W3 |

**W6 therefore starts by reading these four commits**, not by writing tests. Some
of what I planned to find may already be fixed, and duplicating it would be worse
than useless — it would produce contradictory tests. Same discipline for W1 and
W3.

---

### W12 — Release bundles and one-command install

⟨ADDED 2026-08-06. Absorbs and extends W8 — same pipeline, bigger destination.
W8 was "install scripts"; this is "a release a stranger can install".⟩

There is **no release mechanism at all** today. `ci.yml` has exactly two jobs,
`tests` and `rehearsal`, and neither produces an artifact. Every install so far
has been `git clone` + `uv sync` + tribal knowledge.

**Two bundles:**

| bundle | contains | for |
|---|---|---|
| `synapse-client` | worker · orchestrator · providers · contracts · distiller | a teammate joining someone else's service. **Explicitly NOT the service** — per your instruction. |
| `synapse-server` | the service, plus the client packages | the host, who also participates |

**One command, both platforms:**

```
curl -fsSL https://<release-url>/install.sh | bash        # macOS / Linux
irm https://<release-url>/install.ps1 | iex               # Windows / ARM64
```

**Assume nothing is present.** That is the requirement that decides whether this
is real or theatre:
- Bootstrap `uv` itself if absent — do not require it.
- Windows/ARM64: pin the ARM64 interpreter, or `uv sync` silently builds an
  emulated x86 venv where the NPU wheels cannot install and the error surfaces as
  an unrelated Rust build failure.
- Pin `mcp==1.9.4` — anything newer pulls `cryptography`, which has no
  ARM64-Windows wheel.
- The Windows Unicode/codepage fix from W8 belongs here, set by the installer
  rather than documented as a step someone will skip.
- Verify the installed thing actually runs, and say so, rather than exiting 0
  because the files landed.

**Three platforms, not two:** macOS · Linux · Windows/ARM64. `install.sh` covers
the first two, `install.ps1` the third.

**Hardware detection drives what gets installed.** GenieX is Snapdragon-only —
pulling it onto a Mac is wasted bandwidth and a confusing failure. So the
installer detects first, then offers:

- Windows/ARM64 + Snapdragon → NPU is real. Offer GenieX, install it, offer to
  fetch the model.
- macOS / x86 Linux → **never** fetch GenieX. Offer the Claude arms
  (`anthropic` on a key, `claude-cli` on a subscription) or `--listen`.
- Detection must be *reported*, not silent: "Snapdragon X Elite detected — NPU
  path available" or "no compatible NPU — configuring the Claude arm". Someone
  whose NPU was missed needs to see that it was missed.

**`curl | bash` has no stdin — this breaks naive prompting.** Piping to bash
gives bash the *script* on stdin, so `read` returns nothing and every prompt
silently takes its default. Either read from `/dev/tty` when it exists, or detect
non-interactive and drive everything from flags/env with the chosen configuration
printed. Getting this wrong produces an installer that appears to ask and never
listens.

**MCP registration is part of install**, since otherwise every user hand-copies a
command from `JOIN.md`:
`claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp`
Check the `claude` CLI exists first, offer rather than assume, and state which
scope was used — a silent `--scope user` edit to someone's global config is the
kind of thing that erodes trust in an installer.

**The 4B model — researched 2026-08-06, and the slow default is avoidable.**

*Where it comes from.* Two canonical sources, both Qualcomm's:

- **Hugging Face** — [`huggingface.co/qualcomm/Qwen3-4B`](https://huggingface.co/qualcomm/Qwen3-4B)
  carries **pre-exported per-device bundles**, including
  `GENIEX_QAIRT w4a16 Snapdragon® X Elite` — exactly our target. It also ships
  `GENIEX_LLAMACPP q4_0` variants at context 512 / 1024 / **4096**.
- **Qualcomm AI Hub** — [`aihub.qualcomm.com/models/qwen3_4b`](https://aihub.qualcomm.com/models/qwen3_4b),
  which is what `geniex pull ai-hub-models/Qwen3-4B-Instruct-2507` reaches.

*Why the default is slow, and the fix.* `geniex pull` already accepts a local
source — this is the whole answer:

```
geniex pull <model>[:<precision>] --model-hub {aihub|hf|localfs}
geniex pull local/<name> --local-path /path/to/dir-or-zip
```

and the docs state: *"pull copies files into the GenieX cache. After a successful
pull you can safely delete the source to avoid keeping two copies."* So we
**fetch once, from wherever is fastest, then feed GenieX from disk** — no
teammate ever waits on the aihub path.

*Hosting, in order:*

1. **For the demo — stage once and serve on the LAN.** Three people on one
   network, and the host already exposes a LAN service. One WAN download instead
   of three, and it raises no redistribution question at all. This is the
   recommended path and the one to build first.
2. **For public release — do NOT mirror yet.** The HF page lists the licence as
   **"other"**, deferring to the original Qwen3-4B licence, and redistribution
   rights are **not established on that page**. Reading the upstream licence is a
   prerequisite, not a formality. Until then the installer pulls from the
   canonical source and documents `--model-hub localfs` as the fast path.
3. **If the licence permits mirroring** — GitHub Release assets (2 GB/file, no
   LFS bandwidth quota). Split with a checksum manifest if it exceeds that.

*Two compatibility gotchas the installer must handle:*

- **The QAIRT SDK version on the device must match the one published alongside
  the assets.** A mismatch is a real failure mode, so check it and say so rather
  than letting `geniex serve` fail obscurely.
- `config/synapse.toml`'s commented GGUF fallback assumes
  `usable_context = 8192`, but the published llama.cpp variants stop at **4096**.
  That placeholder is already marked UNMEASURED — this is why. Correct or delete
  it; do not let a number that has no published bundle behind it sit in config.

Whichever path: checksum-verify after download, resume on interrupt, and never
re-download when the file is already present and valid.

**CI:** a `release` job on tag — build both bundles, attach to a GitHub Release,
publish `install.sh` / `install.ps1` from the tag so the curl URL is stable.

**⚠️ API keys in CI reverses a decision already recorded in `ci.yml`.** Its
header says, in those words:

> Live-smoke runs stay manual and local — do NOT wire `INFERENCE_CLOUD_*` secrets
> into Actions.

To answer the question directly: **GitHub Actions encrypted secrets exist and
work — but only inside Actions.** They are deliberately not fetchable by an
installer on a user's machine, so they solve CI testing and nothing about
distribution. Two further limits worth knowing: secrets are **not** exposed to
workflows triggered by pull requests from forks, and every live run spends the
shared key budget that ADR 0005 says needs ~10 keys to hold 60s latency.

If we do it, do it as a **protected environment with required reviewers** on a
manually-dispatched workflow — not plain repo secrets on every push, which is
what the existing header is guarding against. Written up as
`decisions/010-live-secrets-in-ci.md`, including that it overturns a prior
decision, so the reversal is one revert.

Keys are also **being rotated after the 7th** (`JOIN.md`), so anything wired now
needs re-wiring then. That belongs in `HUMAN-TODO.md`.

**Idempotent, and it re-runs like a modern tool.** Running the installer twice
must be safe, boring, and fast. The convention `rustup`, `uv` and `brew` all
share, and what we follow:

- **Detect before installing.** For every dependency — `uv`, Python, the packages,
  GenieX, the model file, the MCP registration — check whether it is present and
  what version. Never install over something already correct.
- **Report per component, not as one opaque blob:** `already current`,
  `upgraded 1.2 → 1.4`, `installed`, `skipped (no NPU)`. A user re-running after a
  failure needs to see which step actually did something.
- **Never silently downgrade**, and never clobber a user's newer version because
  our pin is older. Say what the mismatch is and let them choose — except
  `mcp==1.9.4`, which is an exact pin for a real ARM64 reason and must be stated
  as such when it is enforced.
- **Ask before touching anything outside our own tree** — global config, the
  Claude MCP registry, PATH. Non-interactive runs take the conservative default
  and print what they skipped.
- `--force` / `-Force` to reinstall regardless, because every tool eventually
  needs the escape hatch.
- End with a summary of what changed and what to run next. A successful re-run
  should print roughly nothing and exit fast.

**The verification that makes this trustworthy, and the thing most projects get
wrong:** install must be tested in a **clean environment** — a container, or a
runner with nothing preinstalled — never on a dev box that already has `uv`,
Python and a warm cache. An installer verified only on the author's machine
tests the author's machine. If a clean Windows/ARM64 runner is not available in
CI, that gap goes in `TLDR.md` in those words rather than being papered over.

**Blockage risk:** low for the bundles, medium for clean-room Windows/ARM64
verification, which may not be reachable in CI tonight. Park that half if so —
the macOS/Linux path is independently valuable.

**Models:** Fable (bundle boundary — what a client genuinely needs without the
service) · Opus (installers, CI job) · Sonnet (packaging mechanics).

---

### W10 — Documentation

**Publishing: keep it in-repo, do NOT use the GitHub wiki.** Reasoning, since
you asked:

A wiki is a *separate git repository*. Docs stop moving in the same commit as
the code they describe, they leave PR review, and they leave `grep`. This repo's
whole discipline is the opposite — dated evidence next to the thing it explains,
`CONTEXT.md` as canonical invariants, ADRs beside the decisions. **We hit exactly
this failure tonight:** `CONTEXT.md` asserted the opposite of shipped suppression
behaviour, and the fix was only safe because the doc and the code landed in one
commit. A wiki structurally prevents that.

**Recommendation:** stay in `/docs`, and if you want a formal published face, add
**GitHub Pages** built from `/docs`. Same files, versioned, PR-reviewable, and it
renders as a real site. If a wiki is genuinely wanted for discoverability,
*generate* it from `/docs` in CI so the repo stays the single source of truth —
never hand-edit it. Recorded as `decisions/007-docs-in-repo-not-wiki.md`.

**Toolchain: MkDocs + Material + mkdocstrings.** You asked for the standard
Python way to pull code-level docs out of docstrings, with high-level docs
alongside. The three real candidates:

| tool | verdict |
|---|---|
| **MkDocs + Material + mkdocstrings** | **chosen.** Markdown-native, so every existing `/docs` file works unchanged. `mkdocstrings` renders Python docstrings into an API reference. `mkdocs gh-deploy` publishes to Pages in one command. Material is the look most Python projects now use. |
| Sphinx + autodoc | the traditional answer, and the wrong one here. reStructuredText-first, so our entire Markdown corpus needs MyST plus config to work at all — real cost, no gain. |
| pdoc | zero-config and genuinely nice for pure API docs, but no home for the hand-written high-level material, which is most of what this repo has. |

Worth saying: **this repo's docstrings will make an unusually good API reference.**
They already explain *why*, name rejected alternatives, and carry dates — that is
the thing most autodoc output lacks. `anthropic_provider.py`'s "four structural
breaks" or `ended.py`'s "status AND body, or it is not an ended session" render
as real documentation rather than restated signatures.

Ships as: `mkdocs.yml`, a nav that reflects the real structure, an API section
generated from the packages, and a CI job that builds on PR and deploys from
`main`. Recorded as `decisions/009-mkdocs-material-for-pages.md`.

**Pipeline:**
1. **Audit agent** — inventory every doc; flag stale, contradicted-by-code, or
   orphaned. Specifically hunt the `CONTEXT.md` failure class: a doc asserting
   something the code no longer does.
2. **Writer agents, parallel by area** — README and first-run story · architecture
   (current, not aspirational) · service HTTP reference · MCP tool reference
   (now ten tools, documented nowhere in one place) · troubleshooting ·
   contributor guide.
3. **Consistency reviewer** — cross-checks every claim against code, with
   file:line. This is the agent that would have caught `CONTEXT.md`, and it is
   the one worth keeping permanently.

**Blockage risk:** very low. Highest parallelism of anything here, and no
dependency on a running stack.

**Models:** Fable (audit + consistency review, needs judgment) · Opus/Sonnet
(writers).

---

## How I will work while you sleep

**Branch per workstream, merge when green.** Not direct to main. Another session
is also committing there, and tonight our two workstreams got tangled into each
other's commits twice. Branches make every piece independently reviewable and
revertable — which is your "if it's redoable, do it that way".

**Ordering** ⟨revised — W4 demoted, W2 promoted⟩:

| wave | workstreams | why |
|---|---|---|
| 1 | **W11 transcript** (5 lenses) · **W3 investigation** · **W10 docs audit** · **W8/W12 scoping** | all pure-read, zero dependencies, start everything at once |
| 2 | **W1 Geniex** · **W2 multi-session** · **W12 bundles+installers** · **W10 writers** | different subsystems, fully parallel |
| 3 | **W5** · **W6** · **W7** · **W3 limiter** | W6 reads the other session's four commits first; W7 needs a stack on a shifted port |
| 4 | **W4a** dashboard Page 1 | needs W2's per-session participants to show anything true |
| 5 | **W4b** — *only if time remains* | explicitly expendable |

W11 lands first on purpose: it may add or re-prioritise items in every later
wave, and discovering that at 3am is cheaper than at 7am.

W9 (demo-readiness sweep) runs continuously, feeding items into whichever wave
fits.

**Model assignment.** Fable 5 orchestrates and takes frontier/judgment work.
Opus for development. Smaller models for mechanical passes. Every workstream gets
the review → adversarial-verify → fix → audit shape we used tonight, because it
caught real defects both times.

**Decision rules you gave me, which I will apply literally:**

1. Easy and modular → take the modular option, so tomorrow you can swap it.
2. Big A vs B → my recommended choice, fully documented, with the reversal noted.
3. Prefer whatever is redoable in git.
4. Dead end → **park it**: write `parked/<name>.md` with full state, move on. No
   workstream blocks another.
5. Never wait on you. Anything needing you goes in `TLDR.md` under "needs your
   call" and I proceed with the reversible option meanwhile.

**Logging cadence.** `LOG.md` appended at every workstream transition and every
merge. `STATE.md` rewritten to current truth at the same points. `TLDR.md`
written last, and rewritten if anything material changes after.

---

## Invariants — these are not goals, they are conditions

1. **The suite stays green.** Nothing merges red. Full run before every merge.
2. **End-to-end passes, not just unit.** Every backend now runs on this host, so
   there is no excuse for in-process-only evidence. Each wave ends with: full
   suite · `rehearse_demo.py` ALL BEATS PASS **on shifted ports** · a live stack
   smoke through the Haiku arm — create → contribute → query → leave → rejoin →
   end. Never against the live stack on 8899/8787; that is how fixtures reached
   the real session tonight.
3. **Every risky change is recoverable.** Branch per workstream. A tag before
   anything structural. If an experiment fails it gets reverted or abandoned on
   its branch — never left half-applied on `main`. The revert command goes in the
   decision file, not in my head.
4. **Nothing breaks.** If a change cannot be made without breaking something, it
   gets parked with state, not forced.

## Decision Agent Protocol

Replaces asking you. **Any time I would otherwise raise a question or flag a
choice, I spawn a decision agent instead**, and it:

1. Reads `docs/demo-transcripts.txt` and the demo goals **first** — the point is
   to find the direction you would have gone, not the direction I prefer.
2. Enumerates the real options with honest pros and cons, including the one I
   dislike.
3. Recommends, explicitly matched against the transcript: *"the transcript
   commits us to X, so B."*
4. Names the reversal.

Output is `decisions/NNN-<name>.md`: question · options · pros/cons · transcript
alignment · decision · **how to undo it**. Then I act. I do not wait.

Borrowed from wayfinder's decision-ticket shape — one decision lives in exactly
one file, the TLDR indexes and links, never restates.

## Things I already know I will have to decide without you

Listed now so nothing is a surprise. Each gets a `decisions/` file.

| # | decision | leaning |
|---|---|---|
| 001 | Split suppression from watermark (see conflict box) | split; suppression → `agent_session` |
| 002 | Rate limiter policy: shed vs defer vs block | defer with a visible bound; shedding loses work silently |
| 003 | Dashboard stack — extend current `/debug` vs new app | extend, per your "don't invent too many new things" |
| 004 | Arrival summary computed where — service, or AI-100 | service-side, cached; AI-100 only if quality demands |
| 005 | Geniex supervisor lives in app vs a wrapper script | in-app, so it works for teammates who never read the runbook |
| 006 | Haiku 4K cap enforced in provider vs config | provider, with config override — config alone is too easy to miss |

---

## Not in scope tonight

- Service-side log persistence. It is the real fix behind two stopgaps
  (`ended.py`, `session_meta.py`) but it is a substantial change and not
  demo-blocking.
- The `_warn_if_identity_was_not_taken` concurrency race — reported, deliberately
  not repaired, correct as-is.
- Removing the two stale `created_by: "resync"` mock bodies in `test_cli.py`.
  Cosmetic; I will sweep it if a workstream lands nearby.
