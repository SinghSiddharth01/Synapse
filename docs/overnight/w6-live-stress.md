# W6 — stressing the Haiku arm and the chunking bound

Written 2026-08-06 overnight (W6). Harness: `scripts/w6_live_stress.py`.
Reproduce with `uv run python scripts/w6_live_stress.py --offline`.

**Headline: the live half did not run. 0 of the 30 permitted API calls were
spent, because this checkout has no Anthropic credential** (see §1). Everything
below §2 is a real measurement taken in-process against the shipped config, not
an estimate and not a projection — but it is measurement of *our* arithmetic,
not of the endpoint. §5 is the finding that mattered most; it has since been
fixed, and that section now records the fix rather than the open question.

Offline result: **70 checks, 70 passed, 0 API calls.**

---

## 1. Why there are no live numbers

The Anthropic arm needs a key. There is none reachable from this checkout:

| Source | State |
|---|---|
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` env | unset |
| `secrets.jsonc` → `anthropic.api_key` | present as a key, **empty string** |
| `ant` CLI OAuth profile (`~/.config/anthropic`) | `ant` not installed, no profile |
| `~/.anthropic`, `~/.claude/.credentials.json` | absent |

So the `anthropic` block in the team credentials file is a placeholder, not a
credential. `scripts/serve_local.py:114-145` reads exactly that block, which
means **`--distiller anthropic` cannot work for anyone whose checkout looks like
this one** — it fails at the SDK with "could not resolve authentication method"
rather than at a check that names the cause. `scripts/doctor.py` validates that
`secrets.jsonc` parses and is gitignored; it does not assert that any particular
block is non-empty.

The harness treats an empty string as absent rather than handing `""` to the
SDK, and falls back to the offline phases with a stated mode line, so a run on a
credentialled machine is a single command away and needs no edit.

**To finish this work, put a key in `secrets.jsonc`'s `anthropic` block (or
export `ANTHROPIC_API_KEY`) and run `uv run python scripts/w6_live_stress.py`.**
It will spend at most 30 calls, mostly small.

---

## 2. Effective configuration under test

Read from the committed `config/synapse.toml` at run time, not typed in here:

| Quantity | Value |
|---|---|
| `segment_budget` | **2787** tokens |
| Max chars in one event before pre-split (`budget × 3.5`) | **9754** |
| Prompt pack | `v4-condense` |
| `distil_kinds` | `("text",)` |
| Resolved Haiku output cap | **4096** |

---

## 3. The Haiku pin, through the production construction path

All four assertions run against `synapse_worker.cli._build_distiller_provider`
— the function the worker's `run` path actually calls, which passes **no
constructor arguments at all**. That is the point: a pin only reachable from
Python is not a pin.

| Assertion | Result |
|---|---|
| `SYNAPSE_DISTILLER=anthropic` builds an `AnthropicProvider` | pass |
| `max_tokens` resolves to 4096 with no arguments | pass |
| The pin is **strictly below** the 16000 default | pass |
| `SYNAPSE_ANTHROPIC_MAX_TOKENS=512` tightens it | pass |
| `SYNAPSE_ANTHROPIC_MAX_TOKENS=8000` loosens it above the pin | pass |

The third row is the one that would catch a pin edited to equal the default:
every other check here passes with `HAIKU_MAX_TOKENS = 16000`. The fifth is
there because a pin nobody can loosen is a ceiling, not a default.

**Not established:** that 4096 is what the *endpoint* enforces. That needs §6.

---

## 4. The chunking bound under load — measured

Every row is one blob pushed through the real `Segmenter` at
`budget_tokens=2787`, drained with `flush_incomplete=True`. `headroom` is
`2787 − estimate_tokens(segment.events)` for the tightest segment produced.

**These numbers reproduce.** The first version of this table did not, and it is
worth saying why rather than quietly correcting it: the harness seeded its
generated prose with `hash(label) % 97`, and `str.__hash__` is salted per
interpreter. Every run produced different prose from the same label, so every
multi-segment row's tightest/headroom pair was a number from one particular
process — `one-over-seam` came out at 38, 38, 45, 43 and 36 across five runs of
the command this page tells you to run. Two rows here (`one-over-seam`, `3×`)
are therefore different from what this file said before; the seam rows, `small`
and `12×` are unchanged, because they were never seed-dependent. The seed is now
`zlib.crc32(label)` — a checksum, not a hash-table hash — and the table above is
one run of `--offline`, verified byte-identical under `PYTHONHASHSEED` 0, 1 and
42.

| Input | Chars | Segments | Events | Tightest segment | Headroom |
|---|---:|---:|---:|---:|---:|
| small | 1 200 | 1 | 1 | 343 tok | 2444 |
| just-under-seam | 9 753 | 1 | 1 | **2787 tok** | **0** |
| at-seam | 9 754 | 1 | 1 | **2787 tok** | **0** |
| one-over-seam | 9 755 | 2 | 2 | 2728 tok | 59 |
| 3× budget | 29 262 | 4 | 4 | **2785 tok** | **2** |
| 12× budget | 117 048 | 13 | 13 | **2786 tok** | **1** |
| mixed turn (4 events, one oversized) | 15 409 | 3 | 5 | 2785 tok | 2 |

Assertions that held on **every** segment of **every** row:

- `estimate_tokens(segment.events) <= 2787` — the token bound at the effective
  budget, not at a budget chosen to make the test pass.
- `max(len(event.content)) <= 9754` — the char bound.
- Rejoining every event of every segment reproduces the input **byte for byte**
  — the split is lossless.

Two seam cases are pinned separately, because they are where an off-by-one
lives: an event of **exactly** 9754 chars is **not** split (1 segment, 1 event),
and **9755** chars **is**, into exactly two events.

### What the headroom column is telling you

**Zero.** At 9753 and 9754 chars the estimate lands on 2787 exactly, and at
117 048 chars the tightest of thirteen segments lands on 2786. The bound holds
everywhere tested, and it holds with nothing to spare.

That is not luck, it is arithmetic, and it is fragile in a specific way. The
estimator is `int(chars / 3.5) + 1`, so the largest surviving chunk always lands
within a token of the budget. It holds **at 2787 because 2787 is odd**:
`int(int(b × 3.5) / 3.5) + 1` exceeds `b` for every even `b` (verified for 2..4000:
all 2000 even values violate, no odd value does). The shipped budget is safe;
the general bound is `<= budget + 1`, not `<= budget`.

**So a future change of `segment_budget` to an even number silently breaks the
bound this table asserts** — the pin at `config/synapse.toml:96` is load-bearing
in a way its comment does not say. Anything asserting the strong form must use
2787 or assert `<= budget + 1`.

---

## 5. The finding that mattered most — since fixed

`packages/providers/src/synapse_providers/anthropic_provider.py` sent, on
**every** request, on **every** model:

```python
"output_config": {"effort": self._effort},   # DEFAULT_EFFORT = "low"
```

`effort` is an Opus-tier / Sonnet-5-tier parameter. On **Claude Haiku 4.5 it is
an error**, alongside Sonnet 4.5 — the same row of the same table that records
`effort` working on Opus 4.5. Nothing in the provider made the field conditional
on the model, and the model is chosen independently by the free-text
`SYNAPSE_ANTHROPIC_MODEL`.

So **the Haiku arm 400d on its first call and had never made a successful
request** — the 4096 pin added in `5066f2a` was a cap on an arm that could not
run, and `claude-haiku-4-5` is the demo's distiller arm. Every test in
`packages/providers/tests/test_anthropic_provider.py` passed regardless, because
they all inject a fake client and assert on the resolved `max_tokens`, never on
whether the endpoint would accept the body. The ADR-0005 trap in its exact
shape: green because it asserts the thing that is easy to assert.

**Now fixed.** `supports_effort()` gates the field on the same kind of substring
match the cap uses, and for the same reason — the model arrives as free text, so
an exact-match table would miss a spelling. The table falls back the *opposite*
way to the max-tokens one, which is the part worth remembering: max-tokens falls
back to the generous default so an unlisted model keeps working, `effort` falls
back to **omitting** the field, because omitting it is never an error while
sending it to a model that rejects it always is.

Two things the fix is careful about:

- **The gate is on the key, not the container.** Structured output *is*
  supported on Haiku 4.5, so `output_config.format` still goes out; dropping
  `output_config` wholesale would have fixed the 400 by removing schema
  enforcement from the one arm this provider pins a cap for.
- **Both directions are tested**, on the outbound request body rather than on
  an attribute: no `effort` for either Haiku spelling or for Sonnet 4.5, and
  `effort` still present for Opus 4.5/5, Sonnet 5 and Fable 5. A fix that just
  deleted the field would pass the first half and silently stop keeping
  thinking shallow on the default arm.

Still not established here: that the endpoint agrees. This is a documentation-
grounded fix verified against the request body, not against a live 200 — the
call in §6 remains the thing that would settle it.

---

## 6. What a live run would add, and what it costs

Phases 4–7 of the harness, ~16–20 calls of the 30 permitted:

| Phase | Assertion | Calls |
|---|---|---:|
| 4 | Ask for ~8000 tokens of output; assert `output_tokens == 4096` exactly — the cap **binds**, rather than merely being unreached | 1 |
| 4 | Same prompt at `SYNAPSE_ANTHROPIC_MAX_TOKENS=512`; assert it returns 512 | 1 |
| 5 | Four real post-split segments through the real `Distiller`; assert output stays under the cap and `input_tokens > 1` (the dropped-prompt guard) | ~4 |
| 6 | Compare measured chars/token against the 3.5 estimator across two segment sizes; assert the estimator **over**-counts. If it under-counts on Haiku, §4's bound is decorative | 0 |
| 7 | Starve the arm to 200 tokens on a real segment; assert the truncated response classifies `over-budget` and **not** `degenerate-repetition` | 2 |
| 7 | Elicit a real repetition loop; assert it classifies the other way and the two are separated by the measure | 1 |

Phase 6 is the one with no offline substitute. §4 asserts our arithmetic against
our own estimator; only a real tokenizer can say whether 3.5 chars/token
over-counts on Haiku, and the bound is only meaningful if it does.

---

## 7. Over-budget vs degenerate repetition — offline

Asserted as a **gap**, never against the threshold. A test pinned to the
threshold passes even after the threshold is moved somewhere that separates
nothing.

| Shape | `distinct_shingle_ratio` |
|---|---:|
| Truncated findings object (cut mid-string) | **1.000** |
| Repetition loop (`"retrying the segment now" × 200`) | **0.005** |

Gap: **0.995**, against a `_DEGENERATE_DISTINCT_RATIO` of 0.5 — the two shapes
are not near the threshold, they are at opposite ends of the measure.

Also pinned: a loop that ran **to the cap** classifies `over-budget`, not
`degenerate` (the cap truncated the evidence, so "raise the budget" is the right
next move); and the same text with no cap knowable classifies `degenerate`.

---

## 8. Housekeeping

- No ports opened. The harness runs in-process, because `serve_local.py` never
  spawns a worker (FLOW.md §2) and the worker is where the seam under test
  lives — driving the arm through `serve_local` would exercise the MCP
  `contribute()` path, which hand-builds a one-event Segment and deliberately
  skips `synapse_worker.segmenter`.
- No processes left running. Nothing was started.
- `secrets.jsonc` was copied into the worktree, confirmed ignored by
  `git check-ignore`, never read into any committed artifact, and never printed.
- Machine-readable numbers: `.synapse/w6-live-stress.json` (gitignored).
