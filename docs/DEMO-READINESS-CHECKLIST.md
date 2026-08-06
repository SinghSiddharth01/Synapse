# Demo readiness checklist

Checked against the repo on 2026-08-06. Items marked *external* couldn't be verified
against anything in this repo — confirm them against the hackathon's own instructions
before acting on them.

## Confirm before the demo

- [ ] **Reconcile the rehearsal status.** One account says the Aug 6 rehearsal is only
      half-done (`SYNTHESIZER` unset falls back to a `FakeProvider` that raises and gets
      swallowed into an empty `/query` result); another says the Cirrascale Indonesia 70B
      rehearsal passed all beats with an unscripted flagship merge. Figure out which is
      current.
- [ ] **Power measurement.** Nothing measured yet — don't claim energy efficiency until
      it does. Run `scripts/run_npu_eval.py` on real GenieX NPU hardware.
- [ ] **A/B local-vs-cloud latency numbers.** Not started.
- [ ] **Real two-machine run.** All current end-to-end tests are in-process (zero real
      sockets). Never tried worker → orchestrator → teammate-hosted service over real
      HTTP. Watch for the `mcp==1.9.4` ARM64-Windows pin trap.
- [ ] **Golden fixture co-review sign-off.** All 8 fixture goldens are still solo-authored
      and provisional — need a second team member's sign-off before quoting any
      recall/quality number.


## Post-demo / parked (lower priority for now)

- [ ] Service-side log persistence (real restart fix, not resync-recompute).
- [ ] Un-pruned topic membership (`TopicHealth` lies about collapsed topics).
- [ ] Compaction — unbuilt, parked.

## Unverified — not found anywhere in this repo (external, confirm independently)

- [ ] Feedback survey / Microsoft Form submission deadlines.
- [ ] A presentation/arrival webpage for the session URL.
- [ ] A two-tab demo site (slides + "Global View" of active sessions).
- [ ] Docs hosted/rendered (GitHub Wiki or docs site) instead of raw `.md`, plus a CI
      badge on the README — confirmed CI exists (`.github/workflows/ci.yml`) but the
      README has no badge yet.
- [ ] Competitive-differentiation due diligence ("nothing like this runs on local NPUs").
