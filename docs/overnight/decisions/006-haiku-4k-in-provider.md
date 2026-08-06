# 006 — Haiku's 4K output cap lives in the provider, not in config

**Status:** Decided, implemented (2026-08-06)
**Workstream:** W6 — Regression cover, bounds, and the Haiku arm
**Author:** decision agent, per the Decision Agent Protocol (PLAN.md)

## Question

The Anthropic arm should ask Claude Haiku for at most **4096** output tokens
instead of the 16000 it inherits today. Where does that number live — in
`config/synapse.toml` as a `[capability."claude-haiku-4-5"]` record, alongside
every other budget number in this system, or in `AnthropicProvider` as a
per-model default?

The question is not rhetorical. Every other bound in this repo is config:
`segment_budget`, `usable_context`, `response_reserve`, `provider.max_tokens`,
`upstream_timeout_s`. Putting this one in code is the exception, and an
exception needs a reason.

## Options

### Option A — a `[capability."claude-haiku-4-5"]` record in `config/synapse.toml`

**Pros**
- Consistent with where every other budget number in this system lives, and
  with the two capability records already in the file (`claude-opus-5`,
  `claude-cli-sonnet`).
- Editable without a redeploy by anyone who can edit the config file, and
  reviewable as a config diff rather than a code diff.
- `SynapseConfig.effective_max_tokens` already exists and already does exactly
  this arithmetic — `min(provider.max_tokens, record.response_reserve)`.

**Cons**
- **It would be inert on the arm it was written for.** Two independent reasons,
  and each alone is fatal:
  1. `config.record` resolves the record for `config.model` — the model named in
     `[distiller]`, which stays the **NPU** model no matter what
     `SYNAPSE_DISTILLER=anthropic` says. Nothing in the loading path ever looks
     up a record for the Anthropic model id, so a Haiku record would sit in the
     file unread.
  2. The Anthropic branch of `_build_distiller_provider` returns **before** the
     clamp (`worker/cli.py`, `orchestrator/cli.py`) — deliberately, so a
     1M-context model is not capped at the NPU's 500-token reserve. Even if the
     record resolved, `effective_max_tokens` is never consulted on this arm.
- Synthesis would not see it at all. `SynthesisBudget.for_provider` reads
  `provider.max_tokens` off whichever provider object is wired in; it never
  reads a capability record. A config-only cap leaves the synthesis word budget
  derived from 16000 while the distiller believes it is capped at 4096 — two
  numbers, one of them wrong, and nothing to say which.
- The 2787 `[distiller] segment_budget` pin already overrides every record's own
  derivation, so a Haiku record would be half-inert even on the paths that do
  read records.

### Option B — a per-model default in `AnthropicProvider`, with an env override

**Pros**
- It reaches the arm. All three call sites construct `AnthropicProvider()` with
  **no arguments** and select the model through `SYNAPSE_ANTHROPIC_MODEL`, so a
  default resolved from `self._model` is the only place a cap can land without
  touching three files.
- `SynthesisBudget.for_provider` reads `provider.max_tokens`, so the synthesis
  word budget re-derives from the pinned number automatically. One number,
  read by both consumers.
- Matches `AIC100Provider`, which already carries per-provider defaults plus
  `INFERENCE_CLOUD_MAX_TOKENS` / `INFERENCE_CLOUD_TIMEOUT` overrides, and whose
  comment records why: *"these are the numbers an operator must be able to
  change on a RUNNING deployment, and until 2026-08-06 both were constructor
  defaults reachable only from Python."* Same provider package, same problem,
  same shape.
- The override (`SYNAPSE_ANTHROPIC_MAX_TOKENS`) restores the property Option A
  was wanted for — changeable without a redeploy — without the inertness.

**Cons**
- A number in code rather than in the config file, which is the exception this
  repo otherwise avoids. Mitigated by the override existing and by this record.
- `SYNAPSE_*` env overrides are a second configuration surface next to
  `config/synapse.toml`, and the two can disagree. Accepted: it is the surface
  `AIC100Provider` and `SYNAPSE_ANTHROPIC_MODEL` already use, so this adds a
  variable to an existing mechanism rather than inventing one.
- Substring matching on `haiku` will match a future model that happens to carry
  the word and should not be capped. Accepted deliberately — the alternative
  fails in the more expensive direction (see below).

## Transcript alignment

The transcript's Haiku ask was for a cheap arm to run the loop often, not for a
different quality bar. That is what makes 4096 the right shape rather than a
compromise: the deliverable is a small findings object, and 16000 is sized for a
model whose `max_tokens` must also cover always-on thinking — which Haiku does
not have. The cap is not being lowered to make Haiku worse; it is being lowered
to stop paying Opus-shaped headroom on an arm chosen for being cheap.

The transcript also asked for the arm to be *usable by other people*, not just
runnable here — which is precisely the property Option A loses. A capability
record that reads correctly and does nothing is worse than no record: the next
person to tune the Haiku arm edits it, redeploys, measures no change, and has no
way to tell whether the number was wrong or the wiring was.

## Decision

**Option B.** `AnthropicProvider` carries `HAIKU_MAX_TOKENS = 4096` in a
per-model table resolved from `self._model`, with `SYNAPSE_ANTHROPIC_MAX_TOKENS`
overriding it. `config/synapse.toml` gets **no** Haiku record — adding one that
nothing reads is the specific failure this decision avoids.

Resolution order is `env > explicit argument > per-model pin > 16000`. Env beats
an explicit argument, matching `AIC100Provider`'s
`int(os.environ.get(...) or max_tokens)`: the whole purpose of the variable is
to change the number on a deployment whose code is not being edited, and the
call sites that most need overriding are exactly the ones passing arguments.

The table matches a **substring** (`haiku`), not an exact id. Both
`claude-haiku-4-5` and `claude-haiku-4-5-20251001` are already in this repo, the
model is selected by free-text env var, and an exact-match table would fall back
to 16000 for whichever spelling it did not list — failing **open**, on the
number that costs money. Matching too widely caps a model that did not need
capping, which is recoverable with one env var; matching too narrowly restores
the bug silently.

**4096 is ours, not the endpoint's.** `claude-haiku-4-5` serves a 200K context
and will return up to 64K output tokens. This is stated in the code comment, in
the tests, and here, because a number that looks like a platform limit stops
being questioned — and this is the one an operator is most likely to want to
move.

## Why — the failure this avoids, observed tonight

Tonight's coverage audit found the same shape twice, in code that had already
shipped: a fix applied to the arm that was easy to reach and not to the arm that
runs. `worker/cli.py`'s `effective_max_tokens` clamp was executed by a test that
asserted nothing about it, so reverting it left the suite green while the NPU box
went back to asking for 900 against a 500-token reserve. `_provider()`'s `npu`
arm shipped speaking an endpoint GenieX does not serve, and queries came back
empty with a **200** — no error anywhere.

Option A is that failure written down in advance. The record would be correct,
reviewed, committed, and read by nothing: `config.record` resolves against the
NPU model, the Anthropic branch returns before the clamp, and synthesis reads
`provider.max_tokens` rather than any record. A cap that is present, plausible,
and inert is worse than an absent one, because it answers the question "is Haiku
capped?" with a confident yes.

The regression tests landed with this change are written against that specific
failure rather than against the happy path: the pinned value is asserted to be
strictly below the default (a pin edited to equal 16000 would otherwise pass
every other test while doing nothing), the pin is asserted to fire when the model
arrives via `SYNAPSE_ANTHROPIC_MODEL` (the only path production uses), and every
non-Haiku model is asserted to keep 16000 (a blanket lowering would truncate
Opus 5 mid-object, and would look identical from the Haiku tests alone).

## How to undo it

Three edits, no data migration, nothing outside the providers package:

1. Delete `HAIKU_MAX_TOKENS`, `_MODEL_MAX_TOKENS`, `MAX_TOKENS_ENV`, and
   `default_max_tokens_for` from
   `packages/providers/src/synapse_providers/anthropic_provider.py`.
2. Restore the signature to `max_tokens: int = DEFAULT_MAX_TOKENS` and the body
   to `self.max_tokens = max_tokens`.
3. Delete the two `--- the Haiku output-cap pin ---` / `--- the config
   override ---` sections and the `_no_anthropic_env` fixture from
   `packages/providers/tests/test_anthropic_provider.py`, and drop the added
   imports.

No call site changes: all three construct with no arguments and are unaffected
either way. `test_max_tokens_is_generous_not_the_npu_tuned_default` asserts the
16000 default on the default model and passes before and after, so it is the
check that the undo is clean.

To neutralise the pin **without** a code change — the likelier need — set
`SYNAPSE_ANTHROPIC_MAX_TOKENS=16000` on the deployment. That is the property
this option was chosen for.
