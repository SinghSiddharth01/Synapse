# 3. The on-device distiller compresses; it does not judge

**Status:** Accepted (2026-08-04)

## Context

Plan B Task B.1 gave the distiller four jobs at once: abstract the content so raw work stays on the device, decide which content is durable enough to keep, choose how finely to split it, and classify each result into one of four `FindingType`s. All four were asked of a 4B model over a single prompt.

Measured on the X1E80100 (Hexagon NPU v73, GenieX v0.3.18, `qualcomm/Qwen3-4B-Instruct-2507:W4A16`) across six prompt and rendering configurations, it did exactly one of them reliably:

| Job | Result |
|---|---|
| Abstraction | verbatim n-gram overlap 0.00–0.07 throughout | ✅ |
| JSON conformance | clean, first attempt, zero retries across every run | ✅ |
| Durability judgment | invented a finding from an all-noise segment, **6 of 6 configurations** | ❌ |
| Granularity | merged a `dead_end` into a `decision` as a subordinate clause, despite a rule forbidding exactly that in those words | ❌ |
| Factual fidelity | **reversed a comparison stated twice in its own prompt** | ❌ |

The last one is the reason this is an ADR rather than a prompt tweak. The fixture says *"the reuse ratio is much lower than transaction mode"*, and an earlier turn in the same prompt independently says the other mode *"gives the highest connection reuse."* Two consistent statements. Two of three tested configurations emitted findings claiming the opposite — that the chosen mode gave *higher* or *sustained* reuse.

The model was overriding explicit context with a prior it holds about persistent connections, and which way it landed flipped under trivial perturbations (a render-style change was enough). Every guard passed while it happened: canary ok, `prompt_tokens` correct, schema-valid on the first attempt, verbatim overlap 0.00.

An invented trivial finding is noise a reader discards. An inverted one is misinformation that reaches a teammate's agent looking identical to a correct finding, and the architecture's whole premise is that teammates build on these.

## Decision

The on-device distiller's job is **faithful compression and abstraction only**. It does not decide what is worth keeping.

That judgment moves to the two places already equipped for it:

- **Upstream — triage**, deterministic code in the worker, decides whether a segment reaches the NPU at all.
- **Downstream — synthesis**, service-side and running a larger model, dedups, merges, and filters triviality. `FindingStatus.TRIVIAL` was already documented as synthesis-written, so this reclaims a filter the design always had rather than inventing one.

The prompt carries an explicit fidelity rule, which is what fixed the inversion:

> *Never reverse a comparison. Never turn a drawback into a benefit. Where what the session says conflicts with what you would expect to be true, follow the session.*

Prompts are versioned packs in `config/prompts/*.toml`, and each declares `judges_durability` so the eval harness does not score a compression pack against a job it was never asked to do.

## Consequences

**Good.** On the same fixture, same model, same settings, the reframe moved type coverage from 2 of 4 to **4 of 4**, stopped the `dead_end` being absorbed into the `decision`, and produced *"session pooling has a lower reuse ratio than transaction pooling"* — the correct direction. Each component now does what it measurably can do.

**Bad.** Triage does not exist yet, so nothing currently filters triviality on the device. Trivia flows to the sink today; the first real run condensed an API rate-limit notice into a `learning`. That is correct behaviour for this decision and useless output, and it stays useless until triage is built.

**Cost went up.** One note per distinct point produces more findings than one merged narrative: 1853 prompt tokens against 1673, and more output per segment. On an always-on background distiller that is acceptable, but it is a real increase.

**Identifier leakage got worse.** Verbatim overlap rose from 0.00 to 0.10 and two filenames were copied into findings. Compression pulls wording toward the source, which is the opposite pressure from "state it in your own words." The existing 8-gram metric cannot see single-token leaks, so the privacy property is currently unverified.

**`Finding.type` from the distiller is now best-effort, not authoritative.** It labelled *"No other changes were made"* a `decision`. If synthesis re-types, this is harmless; if anything downstream trusts the distiller's label, that is a cross-track conversation.

**A fixture changed meaning.** `seg-004`'s golden empty array encoded *"the distiller should judge this worthless."* Under this decision the distiller should emit notes for it, and triage should never have sent it. That fixture now tests triage.

## Alternatives considered

**Keep tuning the prompt.** Rejected on evidence. Four independent attempts — rewording, hardening, restricting event kinds, changing render style — moved empty-segment discipline exactly zero, and the rules being ignored named the offending activity explicitly (*"linting, sorting imports"*). A fifth wording was not going to reach a prior that beats two explicit statements in context.

**A second verification call** asking "is this finding supported by the segment?" Would catch inversions directly, but doubles the cost of the slowest step in the system (~13 tok/s) and asks the same model that just got it wrong to check itself. Worth revisiting once there is a corpus large enough to measure whether it helps.

**A larger local model.** Qwen3-8B is a candidate on paper, but the QAIRT bundle context ceiling is already 4096 and a larger model makes the always-on power argument worse — which is the argument the NPU placement rests on, and which is still unmeasured.

**Accept the rates and report them.** Honest, and still the fallback if compression alone proves insufficient on a larger corpus. Rejected as a first move because the reframe was cheap to test and worked.
