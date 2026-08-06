"""Write-ahead durable log + the sole egress to the Synapse Service (Plan D.4).

Deliberately mirrors synapse_worker.producer's file discipline rather than
importing it — the packages share contracts only, and the two logs guard
different hops. Same posture: findings.jsonl append-only, sent.jsonl marks
delivery, unsent = the difference, RETAINED after ack so `resync` can answer
a service restart (amendment F Q5). Replay is safe because ingest upserts by
Finding.id (first-write-wins, E3 Task 1).

#### Plan E amendment (2026-08-05) — what the recovery path actually does

The service's Log is in-memory and dies with a restart; nothing here changes
that. What this module's `resync()`/`resync_sessions()` DO buy, paired with
`InMemoryStore.create_session`'s create-or-return (Plan E Task 11): every
Shared Session this retained log names gets RECREATED (the sh-... 404s after a
restart otherwise, and cannot be recreated by construction), each group is
re-pushed to its own session, and `cli.cmd_resync` then calls `/synthesize` on
every session that converged. **That is TWO model calls per session** — the
push's own merge over the whole re-pushed batch, then `/synthesize` over at
most `CANDIDATE_WINDOW` (20) retrievable findings, which REWRITES
`working_memory` from those twenty. What is recomputed, not restored:
synthesized findings get new ids; Working Memory and Conflicts are re-derived
by a fresh model call rather than replayed, so on a large session the result
is a summary of a slice rather than of everything; `purpose` is lost on a
genuine recreate (the retained log does not carry it); and any contributor who
does not resync is gone entirely.

`_post`'s 404-is-retryable rule exists for this: the overwhelmingly likely 404
here is "the service restarted and no longer knows this session", which stops
being true the moment the recreate pass above runs — so those findings stay
queued and flush themselves with no human involved. Only 400–499 excluding 404
is terminal (`dropped.jsonl`); a 5xx or a transport error is always retried.

#### Post-review amendment (2026-08-04)

Round 2 found a blocker: the log was one undifferentiated stream and
`flush()`/`resync()` sent EVERYTHING pending to `self.shared_id` — whatever
Shared Session happened to be bound at *send* time, not at *record* time. A
`synapse-worker join <other_shared_id>` between a failed flush and the next
attempt silently retargeted the whole backlog, including Findings produced
(and durably queued) under a completely different, unrelated Shared Session.

Fixed by partitioning: every line in `findings.jsonl` is now an envelope,
`{"shared_id": <the Shared Session bound WHEN record() was called>, "finding":
{...}}`. `flush()` and `resync()` group pending/retained entries by that
recorded `shared_id` and POST each group to *its own* `/v1/sessions/{id}/...`
— never to whatever `self.shared_id` (or `rebind()`) currently holds. A
Finding recorded while genuinely unbound (`shared_id` is `None`) has no
session to go to and stays queued forever; that's a stronger version of the
same guarantee ("nothing egresses without a real binding") than a wrong
guess would be. See test_relay.py's re-join tests and `resync()` below,
whose return type was also corrected in this pass (see its docstring).

#### Post-review amendment (2026-08-04), round 3

Round 3 review found the partition above only closes the leak for whatever
reaches `record()` — it does not, and structurally cannot, close it for a
Finding still sitting in the WORKER's own write-ahead log
(`synapse_worker.producer.Producer`) at the moment of a re-join. That log's
envelope (`producer.py`) carries `{produced_at, finding}` — no Shared
Session identity — so when the worker retries a POST after `synapse-worker
join <new_id>`, this process has no way to tell "queued under the OLD
session" apart from "produced fresh under the NEW one": both arrive as the
same `agent_session_id` (a re-join does not start a new Agent Session) with
no `shared_id` anywhere on the wire. `record()` necessarily tags such a
retry with whatever binding resolves *now*, which is the new session —
correct for a genuinely new Finding, wrong for a stale retry, and this
module cannot distinguish the two from the payload alone.

Closing this fully needs the PRODUCER to declare its Shared Session on the
wire (or tag its own WAL envelope with one, mirroring this file's
partitioning one hop upstream) — `synapse_worker.producer` territory, Task
5, an authorized skip on this branch. What this pass *did* fix in scope:
the sibling case where two Agent products are joined to two different
Shared Sessions at once — see `app.py`'s amendment note and
`_resolve_binding_for_agent` (cli.py) — since that one is resolvable
entirely from information already on the wire (`Attribution.agent`) without
any worker change. The single-product re-join gap above is not closed by
this pass and should not be read as closed by the paragraph above it.

#### Session lifecycle (2026-08-06) — an ended session is a DEAD END, not a redirect

THIS IS THE ROUND 3 SHARED-SESSION DISCIPLINE ABOVE, APPLIED TO TERMINATION,
and it is written down here because the tempting shortcut is the exact defect
that amendment fixed. When the service answers `409 {"error":
"session_ended"}` for a Finding's recorded Shared Session, that Finding is
DROPPED (`dropped.jsonl`) with a log line naming the session. It is never
re-addressed to whatever session is bound now — not on the next `flush()`, not
on an operator's `resync`. The round 2/3 leak was "send everything pending to
`self.shared_id`", i.e. to whatever happened to be bound at *send* time; a
"the target session closed, so put these somewhere they can land" fallback
would be that same leak wearing a helpful face, and worse than the original
because a closed session is precisely when a human is most likely to have
decided the work is over. Every envelope keeps the `shared_id` it was recorded
under forever (`record()`); an envelope whose session is closed simply has
nowhere to go, which is the same guarantee an envelope recorded while unbound
already has.

Dropped rather than left pending, because "ended" is the one 4xx that can
never stop being true — unlike the 404 above, which `cmd_resync` is designed
to make false. Leaving them queued would re-POST a dead session on every tick
forever.

Ended session ids observed this way are also reported outward twice: in-memory
via `ended_session_ids()` (so `contribute()` can tell the agent its note went
nowhere, rather than silently reporting "queued") and through the
`on_session_ended` callback, which `cli.main` points at the retained
`ended.json` stopgap so a later `resync` does not re-create what the team
closed (`synapse_orchestrator.ended`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import httpx

from synapse_contracts import Finding

from synapse_orchestrator.ended import is_session_ended

logger = logging.getLogger(__name__)


# `POST /findings` SYNTHESIZES SYNCHRONOUSLY before it answers (api.push_findings
# calls merge() inside the request), so this timeout must cover a full synthesis
# model call, not just a network round trip. Measured live 2026-08-05 against
# Llama-3.3-70B on Cloud AI 100: 12.6, 16.6, 30.9, 33.2, 33.8, 45.0, 52.8 s.
#
# The previous 10.0 s was under EVERY one of those. The failure was silent and
# expensive rather than loud: the service received, stored and synthesized the
# findings, then the relay gave up waiting, never wrote sent.jsonl, and re-pushed
# the identical batch on the next tick -- one more 70B call each time, against a
# key budgeted at ~20 requests/hour. Ingest is idempotent so nothing corrupted;
# it just never converged, and `members` stayed empty because registration
# follows a push this code never saw succeed.
#
# 120 s is ~2x the slowest observed call. A FakeProvider answers instantly, which
# is why no offline test could have caught this.
_SYNTHESIS_AWARE_TIMEOUT = 120.0


class Relay:
    def __init__(self, state_dir: Path, service_url: str, shared_id: str | None, *,
                 timeout: float = _SYNTHESIS_AWARE_TIMEOUT,
                 transport: httpx.AsyncBaseTransport | None = None,
                 ended_sessions: set[str] | None = None,
                 on_session_ended: Callable[[str], None] | None = None) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.findings_path = self.state_dir / "findings.jsonl"
        self.sent_path = self.state_dir / "sent.jsonl"
        self.dropped_path = self.state_dir / "dropped.jsonl"
        self.service_url = service_url.rstrip("/")
        self.shared_id = shared_id
        self.timeout = timeout
        self._transport = transport
        # (shared_id, contributor) pairs already registered with the service by
        # THIS process -- see _register_members. Deliberately in-memory: after a
        # service restart the store forgets its members, and a relay restart
        # re-registering everything it pushes is exactly the recovery wanted.
        self._registered: set[tuple[str, str]] = set()
        # Shared Sessions known to be CLOSED. Seeded from the caller's retained
        # stopgap set (cli.main passes `ended.ended_session_ids(state_dir)`) and
        # grown by any 409 this relay observes. Unlike `_registered` above this
        # one must survive a restart, because "ended" -- unlike "not registered
        # yet" -- can never become false again while the log this relay holds
        # still names the session.
        self._ended: set[str] = set(ended_sessions or ())
        self._on_session_ended = on_session_ended

    def rebind(self, shared_id: str | None) -> None:
        """Point future `record()` calls at a different (or no) Shared Session.

        Lets a long-lived Relay track a binding that changes after boot —
        e.g. the producer endpoint re-resolving on every request — without
        losing the durable log already on disk.

        Deliberately does NOT retarget anything already written: each
        envelope in the log keeps the `shared_id` it was recorded under
        forever (see `record()`). Rebinding only changes what NEW records
        get tagged with — that is the fix for the cross-Shared-Session leak
        described in the module docstring's amendment note.
        """
        self.shared_id = shared_id

    # ── termination ─────────────────────────────────────────────────────────
    def ended_session_ids(self) -> frozenset[str]:
        """Which Shared Sessions this relay knows are closed.

        Read by `contribute()` (server.py) straight after a flush: the relay
        swallows every HTTP outcome by design, so a tool that only sees
        `(sent, pending)` would report "queued (1 pending)" for a note that
        was in fact dropped and can never be delivered. Frozen so a caller
        cannot mutate this relay's idea of what is closed."""
        return frozenset(self._ended)

    def note_ended(self, shared_id: str) -> None:
        """Tell this relay a session closed, from an observation it did not make.

        `end_session` (server.py) and `_forget_ended` see the closure on a
        route this relay never touches -- `POST /end`, or the 409 from
        `query`'s own request -- and both wrote it only to the on-disk stopgap
        (`ended.record_ended`). `_ended` is otherwise seeded once at boot
        (cli.main) and grown only by 409s the relay observes ITSELF, so between
        those two an ended session stayed live in this process's memory:
        findings already queued under it were re-POSTed on every flush, and
        after a service restart forgot the session that is a 404, classified
        "retry", requeued, forever -- for a session THIS process had already
        written into `ended.json`. That is exactly the loop the known-ended
        skip in `flush()` exists to prevent, missing only because the two sets
        disagreed.

        Public alias for `_note_ended`, not a second mechanism: same
        first-observation-wins fold, same callback, same swallowing.
        """
        self._note_ended(shared_id)

    def _note_ended(self, shared_id: str) -> None:
        """First observation of a closure wins; the rest are no-ops.

        The callback fires ONCE per session rather than per dropped batch, and
        anything it raises is swallowed -- it is a durability convenience
        (`ended.record_ended`), and "fail open, always" (Global Constraints)
        outranks it on the egress path."""
        if shared_id in self._ended:
            return
        self._ended.add(shared_id)
        if self._on_session_ended is None:
            return
        try:
            self._on_session_ended(shared_id)
        except Exception as exc:            # noqa: BLE001 -- fail open, always
            logger.warning("Could not persist ended session %r (%s: %s)",
                           shared_id, exc.__class__.__name__, exc)

    # ── write-ahead ─────────────────────────────────────────────────────────
    _UNSET = object()

    def record(self, findings: list[Finding], *, shared_id: str | None = _UNSET) -> None:
        """Append each Finding, tagged with a Shared Session.

        That tag travels with the Finding for the rest of its life in this
        log. `flush()`/`resync()` read it back and send each Finding only to
        the Shared Session it carries — never to whatever is live/current at
        send time. That is what makes a `rebind()` between `record()` and a
        later `flush()` safe: a still-queued Finding stays addressed to the
        session it was actually produced under.

        `shared_id`, when given, tags EVERY Finding in this call with that
        exact value instead of `self.shared_id` — for a caller that already
        resolved the right target itself (e.g. `contribute()` in server.py
        passing the binding it just re-resolved live, or the producer
        endpoint in app.py passing the binding matched to each Finding's
        `attributions[0].agent`). That avoids a second, independent hazard:
        `self.shared_id` is mutable (`rebind()`), so two coroutines sharing
        one `Relay` could otherwise have one's `record()` observe the
        other's `rebind()` between call and `await flush()` (round 2/3
        review). Passing `shared_id` explicitly means this call never reads
        `self.shared_id` at all. Omitted, this call falls back to
        `self.shared_id` exactly as before — every existing caller/test that
        doesn't know its own target session yet.
        """
        target = self.shared_id if shared_id is self._UNSET else shared_id
        with self.findings_path.open("a", encoding="utf-8") as fh:
            for f in findings:
                envelope = {"shared_id": target, "finding": f.model_dump(mode="json")}
                fh.write(json.dumps(envelope) + "\n")

    def _load(self, path: Path) -> list[str]:
        if not path.is_file():
            return []
        return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def _all_entries(self) -> list[tuple[str | None, Finding]]:
        """Every `(shared_id, Finding)` envelope ever recorded, in order."""
        out: list[tuple[str | None, Finding]] = []
        for line in self._load(self.findings_path):
            try:
                envelope = json.loads(line)
                finding = Finding.model_validate(envelope["finding"])
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("Skipping corrupt relay line (%s)", exc)
                continue
            out.append((envelope.get("shared_id"), finding))
        return out

    def _sent_ids(self) -> set[str]:
        return set(self._load(self.sent_path))

    def _dropped_ids(self) -> set[str]:
        return set(self._load(self.dropped_path))

    def _pending(self) -> list[tuple[str | None, Finding]]:
        excluded = self._sent_ids() | self._dropped_ids()
        return [(sid, f) for sid, f in self._all_entries() if f.id not in excluded]

    def pending_count(self) -> int:
        return len(self._pending())

    def retained_count(self) -> int:
        """How many retained Findings `resync()` could possibly deliver:
        every entry ever recorded (sent or not) that carries a real Shared
        Session. A Finding recorded while unbound (`shared_id` is `None`)
        can never be delivered — there is nowhere to send it — so it never
        counts here. Lets a caller (`cli.cmd_resync`) tell "resync pushed
        everything there was to push" apart from "resync pushed less than
        the eligible total" even though `resync()` itself returns a bare
        int (see its docstring).

        An ENDED session's findings are excluded for the same reason an
        unbound Finding is: there is nowhere they can ever land. Counting them
        would make `cmd_resync` print "FAILED — 0 of 3 re-pushed" for a resync
        that did exactly the right thing by refusing to resurrect a session the
        team closed."""
        return sum(1 for sid, _ in self._all_entries()
                   if sid is not None and sid not in self._ended)

    def recorded_session_ids(self) -> set[str]:
        """Every Shared Session the retained log names.

        Findings are partitioned by the session each was RECORDED under (see
        `record()`), so a backlog routinely spans several -- and `cmd_resync`
        runs when nothing may be bound at all. Reading the ids from the LOG
        rather than from a binding is what makes recovery work for a machine
        that never re-joined."""
        return {sid for sid in self._group(self._all_entries()) if sid is not None}

    @staticmethod
    def _group(entries: list[tuple[str | None, Finding]]) -> dict[str, list[Finding]]:
        """Findings recorded under no session (`shared_id is None`) are
        dropped from every group — there is no session to post them to."""
        groups: dict[str, list[Finding]] = {}
        for sid, finding in entries:
            if sid is None:
                continue
            groups.setdefault(sid, []).append(finding)
        return groups

    # ── egress ──────────────────────────────────────────────────────────────
    async def _register_members(self, client: httpx.AsyncClient, shared_id: str,
                                findings: list[Finding]) -> None:
        """Register each Finding's Contributor with the service (Plan D.2's
        "registers the Contributor with the service (POST /members)").

        This step existed nowhere: `synapse_worker.discovery.join_session`
        documented it as NOT DONE because "no Synapse Service exists yet to
        register with", and the service's own `POST /v1/sessions/{sid}/members`
        route was built with no caller anywhere in the tree. The observable
        consequence was `members: []` on every session however many people had
        joined.

        It belongs HERE rather than in `synapse-worker join` for the reason the
        whole architecture rests on: the orchestrator is the single egress, and
        the worker must not open its own connection to the service. Driving it
        off `Finding.attributions` rather than off the binding also means the
        registration follows the findings — every Contributor whose work
        actually reaches a Shared Session is a member of it, including one
        arriving through `contribute()` or a resync of someone else's log.

        Best-effort by construction: a failure here is swallowed, because a
        Contributor list is metadata and the findings in the same request are
        not. `store.add_member` is idempotent, and `_registered` caches what
        this process has already sent so a steady push loop costs one extra
        request per (session, contributor) rather than one per flush.
        """
        wanted = {(shared_id, a.contributor)
                  for f in findings for a in f.attributions}
        for key in sorted(wanted - self._registered):
            try:
                resp = await client.post(
                    f"{self.service_url}/v1/sessions/{shared_id}/members",
                    json={"contributor": key[1]})
                resp.raise_for_status()
            except (httpx.HTTPError, OSError) as exc:
                # Not escalated: the findings POST that follows is the payload
                # that matters, and a 404 here just means the session is not
                # recreated yet -- the next flush retries.
                logger.info("Member registration for %r on %r deferred (%s)",
                            key[1], shared_id, exc.__class__.__name__)
                continue
            self._registered.add(key)

    async def _post(self, shared_id: str, findings: list[Finding]) -> str:
        """'ok' | 'retry' | 'terminal' | 'ended'.

        `httpx.HTTPError` includes `HTTPStatusError`, so the single except
        below used to make a permanent 422 indistinguishable from a transient
        outage: the relay looped forever, logging 'Service unavailable' about a
        request that could never succeed.

        404 IS NOT TERMINAL. The likeliest 404 here is 'the service restarted
        and no longer knows this session', which stops being true the moment
        `cmd_resync` recreates it (Step 2) -- so the queue flushes itself with
        no human involved. Only 4xx that cannot become true are terminal.

        'ended' (2026-08-06) is a THIRD kind of 4xx and is checked before the
        generic terminal branch: the session is closed forever, so these
        findings are dropped like a terminal 422 -- but unlike a 422 the id is
        also remembered, because nothing about this session can ever succeed
        again and `resync` must not re-create it. Split out rather than folded
        into 'terminal' so the caller can log the real reason (a closed
        session is a team decision; a 422 is a bug) and so the id is recorded
        exactly once.

        Returns a STRING, not a bool. All three non-ok values are truthy;
        every caller must compare against 'ok' explicitly.
        """
        payload = {"findings": [f.model_dump(mode="json") for f in findings]}
        url = f"{self.service_url}/v1/sessions/{shared_id}/findings"
        try:
            async with httpx.AsyncClient(transport=self._transport,
                                         timeout=self.timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                # AFTER the findings land, never before: registration is
                # metadata about a push that succeeded, so a queued or
                # rejected batch must not cost a second request per retry
                # against a service that is down.
                await self._register_members(client, shared_id, findings)
            return "ok"
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if is_session_ended(exc.response):
                # Status AND body, via the one shared reader -- an unrelated
                # 409 from an intermediary must keep falling through to the
                # terminal branch below rather than marking a live session dead
                # (see synapse_orchestrator.ended).
                self._note_ended(shared_id)
                logger.warning(
                    "Session %r has ended; DROPPING %d finding(s) recorded under it. "
                    "They are NOT re-addressed to any other Shared Session -- an ended "
                    "session is a dead end, not a redirect (see this module's "
                    "termination note).", shared_id, len(findings))
                return "ended"
            if code == 404:
                logger.warning(
                    "404 from %s — the service does not know session %r. %d finding(s) "
                    "stay queued; `synapse-orchestrator resync` recreates the session and "
                    "they flush themselves.", url, shared_id, len(findings))
                return "retry"
            if 400 <= code < 500:
                logger.warning(
                    "Terminal %d from %s; dropping %d finding(s) for session %r from the "
                    "retry queue. `synapse-orchestrator resync` re-offers them.",
                    code, url, len(findings), shared_id)
                return "terminal"
            logger.info("Service error %d; %d finding(s) for session %r stay queued",
                        code, len(findings), shared_id)
            return "retry"
        except (httpx.HTTPError, OSError) as exc:
            logger.info("Service unavailable (%s); %d finding(s) for session %r stay queued",
                        exc.__class__.__name__, len(findings), shared_id)
            return "retry"

    async def flush(self) -> tuple[int, int]:
        """Push every pending Finding, each to the Shared Session it was
        RECORDED under (see `record()`) — never to whatever `self.shared_id`
        happens to be right now. Returns `(sent, still-pending)`, totalled
        across every session present in the backlog. A Finding recorded
        while unbound counts toward `still-pending` but is never attempted
        (there is nowhere to send it).

        A `retry` result (a 404, an outage, a 5xx) leaves the finding
        pending — it is re-attempted on the next flush. A `terminal` result
        (400/422/…) writes its ids to `dropped.jsonl` and counts it as
        neither sent nor pending: no amount of re-attempting can make a
        permanently malformed payload succeed. An `ended` result is dropped
        the same way and for a stronger version of the same reason — see the
        termination note in this module's docstring."""
        pending = self._pending()
        if not pending:
            return (0, 0)
        groups = self._group(pending)
        pending_total = sum(1 for sid, _ in pending if sid is None)
        sent_total = 0
        newly_sent_ids: list[str] = []
        newly_dropped_ids: list[str] = []
        for shared_id, findings in groups.items():
            if shared_id in self._ended:
                # ALREADY known closed -- from a previous 409 this process saw,
                # or from the retained stopgap set seeded at construction. Drop
                # without a request: the answer cannot be anything but another
                # 409, and a session that ended while the service was down (so
                # this relay never saw the 409 itself) would otherwise be
                # re-POSTed on every tick forever.
                logger.warning(
                    "Session %r is ended; DROPPING %d queued finding(s) without "
                    "attempting delivery. Never retargeted to another session.",
                    shared_id, len(findings))
                newly_dropped_ids.extend(f.id for f in findings)
                continue
            outcome = await self._post(shared_id, findings)
            if outcome == "ok":
                sent_total += len(findings)
                newly_sent_ids.extend(f.id for f in findings)
            elif outcome in ("terminal", "ended"):
                newly_dropped_ids.extend(f.id for f in findings)
            else:                                       # "retry"
                pending_total += len(findings)
        if newly_sent_ids:
            with self.sent_path.open("a", encoding="utf-8") as fh:
                for fid in newly_sent_ids:
                    fh.write(fid + "\n")
        if newly_dropped_ids:
            with self.dropped_path.open("a", encoding="utf-8") as fh:
                for fid in newly_dropped_ids:
                    fh.write(fid + "\n")
        return (sent_total, pending_total)

    async def resync_sessions(self) -> dict[str, int]:
        """Re-push the entire retained log — sent or not — and report WHICH
        sessions converged. `{shared_id: count}`, successes only.

        `resync()` keeps its documented bare-int signature and is now one line
        over this. The caller needs the ids: findings are partitioned by the
        Shared Session each was RECORDED under (see `record()`), so a backlog
        routinely spans several, and re-synthesizing only the currently-bound
        one leaves every other session with its findings back and no Working
        Memory, no conflicts and no merges.

        `dropped.jsonl` is deliberately IGNORED here: this is the
        operator-invoked recovery path, and a session recreated by
        create-or-return should get those findings offered again.

        An ENDED session is the one exception, and the reason `_ended` is
        seeded from disk rather than living only in this process's memory: the
        recovery path's own create-or-return would otherwise re-create a
        session the team closed and refill it (the lifecycle spec's "Durability
        caveat"). Skipped entirely — not posted, not counted, not dropped —
        because `cmd_resync` reports "pushed N of M retained" and a session
        that must never be pushed is not part of M either (`retained_count`).
        """
        groups = self._group(self._all_entries())
        pushed: dict[str, int] = {}
        for shared_id, findings in groups.items():
            if shared_id in self._ended:
                logger.info("resync: skipping ended session %r (%d retained finding(s) "
                            "stay where they were recorded)", shared_id, len(findings))
                continue
            if await self._post(shared_id, findings) == "ok":     # never truthiness
                pushed[shared_id] = pushed.get(shared_id, 0) + len(findings)
        return pushed

    async def resync(self) -> int:
        """Total re-pushed across every session. The plan's documented `-> int`
        (Task 2 Interfaces), unchanged."""
        return sum((await self.resync_sessions()).values())
