# Demo readiness checklist

Checked against the repo on 2026-08-06. Items marked *external* couldn't be verified
against anything in this repo — confirm them against the hackathon's own instructions
before acting on them.

## Confirm before the demo

- [ ] **Power measurement.** Nothing measured yet — figure out a way to quantify power efficiency because we are using local NPU.
- [ ] **A/B local-vs-cloud latency numbers.** Not started.
- [ ] **Real two-machine run.** All current end-to-end tests are in-process (zero real
      sockets). Never tried worker → orchestrator → teammate-hosted service over real
      HTTP. Watch for the `mcp==1.9.4` ARM64-Windows pin trap. make sure the plumbing fro this is okay. Review and make sure the flow from npu provider to ai100 looks okay.
- [ ] Competitive-differentiation due diligence ("nothing like this runs on local NPUs").
