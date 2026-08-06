# Overnight state — per workstream

Rewritten to current truth at every transition. Times PDT, 2026-08-06.
Last rewrite: **05:40**.

| ws | what | status | branch | notes |
|---|---|---|---|---|
| W11/W9 | transcript → two checklists | **✅ merged to main** (10783f4) | overnight/w11-transcript | checklist now agentic-only; HUMAN-TODO.md new, deadline-ordered |
| W3a | flow investigation → FLOW.md | **✅ done** | overnight/journal | 195 lines, file:line cited; feeds W3b + W6 |
| W10a | docs audit | **✅ done** | overnight/journal | `w10a-docs-audit.md`; contradiction list + writer plan |
| W8a | install scoping + review | **✅ done** | overnight/journal | reviewer verdict NEEDS-CHANGES, 8 corrections → W8b spec |
| W1 | GenieX idle death | **🔄 running** (wf_25bd0232) | overnight/w1-geniex | Fable design → Opus dev → review→verify→fix→audit; merges itself when green |
| W2 | multi-session | **🔄 running** (wf_8355bbbd) | overnight/w2-multisession | 3 dev passes; also fixes serve_local `as-<contributor>` identity mismatch |
| W8b | install scripts dev | **🔄 running** (wf_ab238776) | overnight/w8-install | install.sh/.ps1, doctor pre-flight, secrets.example.jsonc, README surgical |
| W6 | stress/regression tests | **🔄 running** (wf_5eca61cf) | overnight/w6-stress | cover 7c42e96/7418a63/c077a51/282fd07; Haiku 4K pin; bounded live stress |
| W10b | doc writers + consistency | **🔄 running** (wf_b2a1872d) | overnight/w10-docs | MkDocs+Material+Pages; 6 areas; consistency reviewer |
| W3b | worker rate limiter | queued — **after W1 merges** | overnight/w3-limiter | avoids service-seam collision |
| W5 | arrival summary | queued — **after W2 merges** | overnight/w5-arrival | retargeted: fire at join_session + carry purpose (wave-1 finding) |
| W7 | live lifecycle E2E | next to launch | overnight/w7-lifecycle | rehearse port-hardcode fix + real-port live smoke (host is free) |
| W4a | dashboard Page 1 | queued — after W2 | overnight/w4-dashboard | scope grew: global-view roster items from wave-1 routing |
| W4b | dashboard Pages 2+3 | expendable | — | only if time remains |
| slides | HTML deck / two-tab site | **decision agent running** | — | unowned 5-of-7-minutes gap found by W11; decisions/010 will rule |

**Invariants:** baseline 888 green at f56d6f0; main now 10783f4 (docs-only), suite
re-verified green at merge. Tags `overnight-20260806-start` and `demo-fallback`
both on origin. Nothing on ports 8899/8787/18181; workstreams use shifted ports.

**Merged to main so far:** 10783f4 (W11 checklists).
