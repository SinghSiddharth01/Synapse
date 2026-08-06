# 005 — The GenieX supervisor lives in the app, owned by serve_local.py

## Question

After X minutes idle, `geniex serve` stays alive as a process but its HTTP
server stops serving (W1's symptom). Something must notice and restart it
without a human. Where does that something live: inside the stack every
teammate already runs, or in a wrapper script / OS service manager a teammate
would have to know about?

## Options

**A. In-app: `scripts/serve_local.py` supervises the model seam.**
It already spawns, health-checks, logs, and reaps every other process
(`spawn`/`wait_for`/`stop_all`), and its steady state is a `time.sleep(3600)`
loop — free real estate for a 15s probe tick. Under `--npu` it also becomes
the *launcher* of `geniex serve` (adopting and, on death, replacing a
pre-started one), because the observed failure leaves a live process holding
the port: only something willing to kill by port can recover it.
- Pros: zero new runbook steps — anyone who can run the one documented command
  gets supervision; restart mechanics reuse tested machinery; the supervisor's
  log lines land in the same terminal the operator is already watching; works
  identically for the stand-in, the `--live` proxy, and real GenieX.
- Cons: serve_local grows real responsibility (it was a convenience script);
  a teammate who runs `geniex serve` by hand and *not* serve_local gets no
  supervision; killing an unowned process by port is platform-conditional
  code (lsof / netstat+taskkill).

**B. A wrapper script (`supervise_geniex.py` / systemd / Task Scheduler).**
- Pros: single-purpose, testable in isolation; survives serve_local itself
  dying; the classic ops answer.
- Cons: it is a second thing to start, on the one machine (the NPU box) most
  likely to be driven by a teammate mid-walkthrough who never read the
  runbook. A supervisor that isn't running supervises nothing — and the
  failure it guards against is precisely the kind discovered live. OS service
  managers are per-platform setup on a Windows-on-Snapdragon box nobody has
  automated.

**C. Inside the worker/service processes (probe from NPUProvider callers).**
- Pros: closest to the actual consumers of the seam.
- Cons: two processes would race to restart one child neither owns; the
  service may legitimately run on a different machine from GenieX
  (`--service-url` topology), where restarting is impossible by design.

## Transcript alignment

The meeting commits to "keep any live portion to the most basic/robust
interactions only" and to an informal open-house round where "the same asset
(video + one-command install + live global view) needs to work informally" —
i.e. the stack is stood up casually, repeatedly, by whoever is at the laptop.
That rules out B (a second command nobody will run) and favors the
one-command path owning its own robustness. The competitive slide's whole
differentiator is "nothing else runs this on local NPUs specifically" — the
NPU seam dying quietly mid-walkthrough is the single worst way to lose that
claim. PLAN.md already leans 005 in-app "so it works for teammates who never
read the runbook"; this confirms it and adds the launcher role under `--npu`.

## Decision

A. `serve_local.py` supervises the model seam in its main loop: probe
`GET :18181/v1/models` every 15s, 5s timeout, 4 consecutive failures = dead
(~45-60s of continuous silence; see the correction below for why that is
enough and why the original justification was not). Under `--npu` serve_local launches
`geniex serve` itself when :18181 is free, adopts an existing listener
otherwise, and on death kills the port owner before respawning as an owned
child. Restarts back off 0s/30s/120s with a hard cap of 3 per rolling 10
minutes, then GIVE UP loudly with fallback commands — a broken NPU must not
become a restart loop on stage. Every transition (SUSPECT, DEAD, RESTARTING,
RESTORED, GAVE_UP) prints to the operator's terminal AND
`.synapse/logs/supervisor.log`; RESTORED prints even when nobody noticed the
outage, because a silent self-heal hides a dying box until it dies on stage.
Hand-started `geniex serve` without serve_local remains unsupervised —
accepted; that path is a developer probing the box, not the demo.

### As-built notes

- The stand-in and the `--live` proxy are supervised by the same machinery,
  not only real GenieX. They are what every teammate without an NPU runs, so
  they carry most of the demo; supervising only the NPU arm would have left
  the common path unproven as well as unprotected.
- **CORRECTION (post-review), on the threshold's justification.** The
  original text above defended 4×15s as "45s, longer than the seam's 30s
  `max_seconds_per_call` design point, so slow is never punished as dead."
  That reasoning is wrong and does not survive checking. `max_seconds_per_call`
  (config/synapse.toml:73-75) is a segment-SIZING budget — "bounds how much
  prompt can be prefilled per call… combined with the model's measured prefill
  rate, the second of the two limits on segment size" — not a wall-clock bound
  on a seam call. The real per-call bound on this seam is
  `OpenAICompatibleProvider.__init__(timeout=300.0)` (openai_compat.py:35),
  inherited unchanged by `NPUProvider`. The claimed "50% margin" does not
  exist. The threshold is **unchanged** — it errs safe, and the honest reason
  is narrower: `/models` is metadata, runs no inference, and is answered off
  the accept loop, so a seam that cannot produce it for a minute is not busy,
  it is not serving. Recorded rather than quietly corrected because the error
  originated in the decided design note, not in the implementation.
- **CORRECTION (post-review), on the reported duration.** The DEAD line used
  to print the constant `(DEATH_STRIKES - 1) * PROBE_INTERVAL_S` = 45s. That
  is only the span when probes return instantly. A probe that fails by TIMING
  OUT spends its own `PROBE_TIMEOUT_S` (5s) first, so on the hung socket this
  supervisor exists for, one cycle is 20s and the measured span is **60s** —
  and detection latency from the last healthy answer is one cadence longer
  again (~80s worst case). The line now reports the span the supervisor
  actually measured between the first strike and the fourth, because it is the
  figure an operator quotes when deciding whether the box is failing more
  often than it used to. Pinned by
  `test_the_dead_line_reports_the_span_it_measured_not_a_constant`.
- The backoff delay is served by TICKS THAT STILL PROBE, and a seam that
  recovers during one cancels the pending restart. The first cut returned
  early for the whole delay and then restarted unconditionally: a seam that
  came back at second 3 of a 120s delay was killed at second 120 — a healthy
  seam torn down, a self-inflicted outage, and a restart spent on the ledger
  that shortened the leash for the next real death. The cancelled attempt
  still counts on the ledger: a seam that dies and self-heals repeatedly is
  still a dying seam.
- GIVING UP caps the RESTARTS, not the watching. The supervisor keeps probing
  in `GAVE_UP` and announces the seam answering again, because otherwise the
  last word on screen is a banner describing an outage the operator has
  already fixed.
- A restart that RAISED (no `geniex` on PATH, a permission error) does not get
  the credit for a later recovery. `RESTORED after restart 1` is a claim about
  causation; when no restart ran, the line says so and tells the operator the
  box is effectively unsupervised until the error is fixed.
- `kill_port_owner` looks up the listener with `lsof -ti tcp:PORT
  -sTCP:LISTEN`, not `lsof -ti :PORT`. The unfiltered form matches sockets
  with that port at EITHER end, so it returns the CLIENTS too — and the
  service child holds an established connection to :18181 for the whole of a
  generation, i.e. precisely during the window in which the seam has gone
  silent and the supervisor is about to kill "the port owner". The unfiltered
  form would have SIGKILLed the service the recovery exists to keep working.
- A supervisor restart APPENDS to the child's log rather than truncating it.
  The dying seam's last words are the only artefact of the idle death anyone
  on the NPU box will ever have, and the replacement's first line landed on
  top of them within a second of the crash. A fresh serve_local run still
  truncates.
- A child that has EXITED is declared dead on the tick that notices, with no
  strikes and without consulting the probe at all — and its log line says
  "process exited (rc=…)" rather than the probe-failure wording, which would
  have sent the reader hunting for a network problem. Believing the exit over
  a healthy-looking probe matters: a port freed by an exit can be grabbed by
  anything.
- A restart is judged by the NEXT probe, not by the restart call returning.
  `wait_for` inside the restarter raises `SystemExit` when a child dies during
  startup; the supervisor swallows that and lets the probe count the failure,
  because its whole job is to outlive a seam that will not come up.
- `tests/test_seam_supervisor.py` states plainly in its module docstring that
  it proves the supervisor and NOT the GenieX bug: the hanging endpoint
  reproduces the observable signature (socket accepted, zero bytes, forever),
  which is the contract the supervisor is written against, but the bug itself
  lives in a closed binary on a box CI cannot reach.

## How to undo

**⟨post-review correction⟩ The one-line revert this section used to promise
does not apply, and an undo instruction that fails when someone reaches for it
is worse than none.** Two later commits — `w1: boot preflights and the --npu
--live split config` and `w1: review fixes` — edit the same region of
`scripts/serve_local.py`, so reverting the supervisor commit on its own gives
`CONFLICT (content): Merge conflict in scripts/serve_local.py`. Verified by
running it.

Back out all of W1, newest commit first (verified to apply cleanly):

    git revert --no-edit $(git log --format=%H --grep="^w1: " <base>..HEAD)

where `<base>` is whatever this branch forked from — `origin/main` while W1 is
still unmerged, and the merge commit's first parent afterwards. Check the list
first with `git log --oneline --grep="^w1: " <base>..HEAD`: four commits, and
if it prints more or fewer than four, read them before reverting anything.

That restores today's behaviour on every W1 front at once: `--npu` requires a
pre-started `geniex serve`, nothing restarts anything, and retrieval failure
goes back to an empty 200 (decision 008 — read its undo block before you run
this, because this command takes that with it).

To keep the rest of W1 and remove only the supervisor, do it by hand — it is
still confined to one file plus one test module: delete `probe_seam`,
`port_owner_pids`, `kill_port_owner`, `_terminate_child`, `SeamSupervisor`,
`standin_restarter`, `geniex_restarter`, `start_or_adopt_geniex` and the
supervisor constants from `scripts/serve_local.py`; restore `main()`'s tail to
`while True: time.sleep(3600)`; under `--npu` go back to requiring a
pre-started seam; delete `tests/test_seam_supervisor.py`.
