# Fixtures — ⚠️ PROVISIONAL, SOLO-AUTHORED

**These are not yet the contract. Do not treat them as the quality bar.**

Plan 0 Task 0.3 is explicit about why:

> **Why together:** the fixtures encode a quality bar and a segmentation boundary
> that two different tracks must agree on. Written by one person, they become
> that person's opinion; written together, they are the contract.

These two fixtures were authored solo on 2026-08-04 to unblock the Plan B
device slice — the distiller, the guards, and the NPU provider needed *something*
to run against on real hardware. They are scaffolding for the mechanism, not a
settled eval target.

## Before these harden

- [ ] Co-author `seg-001` and `seg-004` with all three track owners
- [ ] Add the remaining three from Plan 0 Task 0.3: `seg-002` (second ordinary
      turn, different shape), `seg-003` (oversized `tool_result`), `seg-005`
      (two near-duplicate findings across two Contributors)
- [ ] Confirm the segmentation boundary with Plan A's owner — the segmenter must
      reproduce these Segments *exactly*, and that test is the anti-drift gate
      between Plans A and B
- [ ] Delete this warning once the above is done

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
