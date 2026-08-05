# Fixtures

## Status — 2026-08-04

8 fixtures. seg-001/seg-004 predate the corpus completion; the rest landed with plan E1.

**Golden sign-off (co-authoring gate — Plan 0.3):**

| fixture | authored by | signed off by |
|---|---|---|
| seg-001 | aditya | — PROVISIONAL |
| seg-002…seg-007 | (E1 author) | — PROVISIONAL |

Goldens are the eval target and the quality bar. Until each row has all three
names, treat judge scores as directional. `fixtures/triage.json` records what
triage should do per fixture, including two ACCEPTED FALSE POSITIVE entries.
`test_fixture_contamination.py` enforces zero six-gram overlap with prompt packs.

## What is here

| Fixture | Purpose |
|---|---|
| `segments/seg-001.json` | Ordinary turn. Golden covers all four `FindingType`s |
| `segments/seg-004.json` | **All noise.** Golden is an **empty array** |

`seg-004` is the load-bearing one. Small models invent findings for boring
input — it is failure mode #3 in Plan B's calibration table — and an empty
golden is the only thing that catches it.

## How goldens are compared

Goldens carry `id` and `ts` placeholders so they parse as `Finding[]`, but the
eval never compares those: `Finding.id` is a UUID stamped at distil time and
`ts` is wall-clock. Comparison is on `type` and on the *meaning* of `text`.
Verbatim-copy rate is measured against the source Segment, not against the
golden — it is a privacy metric, not a quality one.

## Rule: eval targets are never edited to satisfy the metric

A golden is the target a run is measured against. If a golden trips a guard
(the identifier-leak detector, the six-gram contamination check, anything
else), the fix is to reword the golden to abstract rather than quote — or,
if the flagged token is genuinely public engineering vocabulary rather than
a private identifier, to add it to `evaluation.DEFAULT_ALLOWLIST` with a
comment explaining why. What must never happen is quietly rewording a golden
*away from* the plan's specified text so the metric reads clean while the
target itself drifts undocumented — that inverts the point of having a
frozen target at all. (Post-review amendment, 2026-08-04: an earlier pass on
this branch reworded seg-002's `f-002-01` and seg-003's `f-003-01` to dodge
the identifier-leak detector's `refresh-on-401` / `mid-stream` matches
instead of allowlisting them. Both goldens are restored to the plan's
original text; both tokens are now reviewed, allowlisted public vocabulary.)
