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
(45s — longer than the seam's 30s `max_seconds_per_call` design point, so
slow is never punished as dead). Under `--npu` serve_local launches
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
- The DEAD line reports the outage as **45s**, i.e.
  `(DEATH_STRIKES - 1) * PROBE_INTERVAL_S` — the strikes land at t+0/15/30/45
  from the first failure, so 45s is the span of continuous unresponsiveness
  they actually measure, not 60s.
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

The supervisor is confined to `scripts/serve_local.py` plus its tests:

    git revert --no-edit $(git log --format=%H -1 --grep="w1: in-app model-seam supervisor")

(One commit carries this decision; the subject line above is its grep key.)
Reverting restores today's behavior: `--npu` requires a pre-started
`geniex serve` and nothing restarts anything.
