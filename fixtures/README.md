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
