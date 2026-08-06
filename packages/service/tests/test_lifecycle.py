"""Session lifecycle at the service: end, leave, and the identity re-key.

Executes the Testing section of
docs/superpowers/specs/2026-08-06-session-lifecycle-design.md, items 1, 2, 4
and 5 -- the four that are the service's to answer. Items 3, 6, 7 and 8 are
binding/transcript/relay behaviour and belong to the orchestrator and worker
suites.

Same discipline as test_api.py: in-process ASGI, an injected httpx transport,
a scripted FakeProvider, zero real sockets.
"""
import httpx
from synapse_providers import FakeProvider

from synapse_service.api import build_app

MERGE_NOOP = {"working_memory": "wm-1", "merges": [], "trivial_ids": [], "conflicts": []}


def _finding_json(fid: str, contributor: str = "aditya", agent_session: str = "as-1",
                  text: str | None = None) -> dict:
    return {"id": fid, "type": "learning", "text": text or f"insight {fid}",
            "attributions": [{"contributor": contributor, "agent_session": agent_session,
                              "agent": "claude-code"}],
            "ts": "2026-08-04T12:00:00Z", "refs": [],
            "provenance": "distilled", "status": "kept",
            "merged_from": [], "merged_into": None}


def _client(provider) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(provider)),
                             base_url="http://svc")


async def _session(client, *, created_by: str = "siddsing") -> str:
    r = await client.post("/v1/sessions", json={"purpose": "fec decode",
                                                "created_by": created_by})
    return r.json()["shared_id"]


# ── item 1: creator-only termination ────────────────────────────────────────

async def test_end_by_a_non_creator_is_rejected_and_the_session_stays_live():
    """Spec Testing item 1, negative half. Layer 2 of the three gates around
    end_session, and the only one an agent cannot satisfy by retrying: the
    harness prompt is human-side and the "others are still members" refusal is
    client-side, so if this check is not in the SERVICE it is not a check.

    The 403 names the creator rather than saying "forbidden" -- the caller is
    an agent relaying this to a human who now knows who to ask.

    Asserting the 403 alone would pass against an implementation that returned
    403 AFTER closing the session, so the query at the end is what says the
    memory is still there."""
    async with _client(FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])) as client:
        sid = await _session(client, created_by="siddsing")
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding_json("f-1")]})

        r = await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "akhil"})
        assert r.status_code == 403
        assert r.json() == {"error": "only siddsing can end this session"}

        # ...and nothing was closed on the way out.
        live = await client.post(f"/v1/sessions/{sid}/query",
                                 json={"query": "what do we know",
                                       "contributor": "akhil"})
        assert live.status_code == 200
        assert [f["id"] for f in live.json()["findings"]] == ["f-1"]


async def test_end_by_the_creator_succeeds():
    """Spec Testing item 1, positive half."""
    async with _client(FakeProvider(scripts=[])) as client:
        sid = await _session(client, created_by="siddsing")

        r = await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "siddsing"})

        assert r.status_code == 200
        assert r.json() == {"shared_id": sid, "status": "ended", "ended_by": "siddsing"}


async def test_end_without_ended_by_is_a_422_not_a_500():
    """The contract `_missing` exists for, applied to the new route: E4's
    Relay treats 5xx as retryable, so a client bug that raises here becomes an
    infinite retry loop against a request that can never succeed. Includes the
    NO-BODY case, which is the likely shape of the bug (an MCP tool POSTing
    with no JSON at all) and which `await request.json()` raises on."""
    async with _client(FakeProvider(scripts=[])) as client:
        sid = await _session(client)

        r = await client.post(f"/v1/sessions/{sid}/end", json={})
        assert r.status_code == 422 and "error" in r.json()

        no_body = await client.post(f"/v1/sessions/{sid}/end")
        assert no_body.status_code == 422 and "error" in no_body.json()


async def test_ending_twice_over_the_route_is_idempotent():
    """A retried POST /end -- the ordinary outcome of a dropped response on a
    call nobody wants to leave ambiguous -- must not be an error.

    This test deliberately claims NOTHING about first-vs-last closer any more.
    It used to, and could not see it: the creator-only 403 makes any other
    `ended_by` impossible over HTTP, so both requests necessarily carry the
    same name and the assertion just echoed the request back. VERIFIED BY
    MUTATION 2026-08-06 -- `store.end_session` rewritten to `return ended_by`
    (dropping the fold's first-write-wins read entirely) left all 824 tests
    passing. The fold semantics are pinned at the storage seam instead, in
    `test_the_fold_keeps_the_first_closer_not_the_last` below, which is the one
    layer where two different closers are expressible."""
    async with _client(FakeProvider(scripts=[])) as client:
        sid = await _session(client, created_by="siddsing")

        first = await client.post(f"/v1/sessions/{sid}/end",
                                  json={"ended_by": "siddsing"})
        second = await client.post(f"/v1/sessions/{sid}/end",
                                   json={"ended_by": "siddsing"})

        assert first.status_code == second.status_code == 200
        assert second.json()["ended_by"] == "siddsing"


async def test_the_fold_keeps_the_first_closer_not_the_last():
    """`store.end_session` returns the ORIGINAL closer on every repeat, and the
    rebuilt fold agrees -- the same first-write-wins rule that makes a replayed
    finding inert, reaching termination for free (store.end_session's own
    docstring, and adr/0004's "the log is append-only and state is a fold").

    Driven at the STORAGE seam because that is the only place the two closers
    can differ: the route's creator-only gate rejects anyone but `created_by`,
    so no HTTP-level test can distinguish "keeps the first" from "takes the
    last". Nothing anywhere in the suite covered it -- grep for `SessionEnded`
    or `ended_by` across test_fold/test_memory/test_store/test_log returned
    nothing -- and the mutation named in the sibling test above is what that
    cost.

    `rebuild()` is the second assertion and not decoration: a `SessionEnded`
    handled correctly on the live path but folded wrongly on replay would come
    back as the WRONG closer after the restart+resync path this system's whole
    recovery story runs through."""
    app = build_app(FakeProvider(scripts=[]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://svc") as client:
        sid = await _session(client, created_by="siddsing")
    store = app.state.store

    assert store.end_session(sid, "siddsing") == "siddsing"
    # A second, DIFFERENT closer -- unreachable over the route, which is
    # exactly why this is here.
    assert store.end_session(sid, "akhil") == "siddsing"

    store._memories[sid].rebuild()
    assert store._memories[sid].view().ended_by == "siddsing"


async def test_end_on_an_unknown_session_is_404_not_403():
    """404 before the creator check: an unknown session has no creator, and
    reporting "only <someone> can end this session" for a typo'd id would be
    both wrong and confusing."""
    async with _client(FakeProvider(scripts=[])) as client:
        r = await client.post("/v1/sessions/sh-nope/end", json={"ended_by": "siddsing"})
        assert r.status_code == 404


# ── item 2: an ended session is fully closed ────────────────────────────────

async def test_an_ended_session_409s_on_every_route_that_serves_or_extends_it():
    """Spec Testing item 2, and the reason `_unavailable` is ONE helper.

    All four routes in one test on purpose: the failure this guards against is
    not "the check is wrong", it is "the check is missing from one of them" --
    and per-route tests scattered through the suite are how a fifth route gets
    added later with no guard and nothing goes red. `push_findings` matters
    most: a 200 there would accept findings into a session the team has closed
    and lose them in a log nothing will ever read again.

    The FakeProvider is scripted EMPTY: an exhausted script raises on any
    call, so if a guard were missing, that route would 500 rather than
    silently pass -- either way it is not the 409 asserted here, and the
    provider count is a second witness that nothing reached the model."""
    provider = FakeProvider(scripts=[])
    async with _client(provider) as client:
        sid = await _session(client, created_by="siddsing")
        await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "siddsing"})

        responses = {
            "query": await client.post(f"/v1/sessions/{sid}/query",
                                       json={"query": "anything",
                                             "contributor": "akhil"}),
            "push_findings": await client.post(f"/v1/sessions/{sid}/findings",
                                               json={"findings": [_finding_json("f-1")]}),
            "synthesize": await client.post(f"/v1/sessions/{sid}/synthesize"),
            "watermark": await client.get(f"/v1/sessions/{sid}/watermark",
                                          params={"contributor": "akhil"}),
        }

    assert {name: r.status_code for name, r in responses.items()} == {
        "query": 409, "push_findings": 409, "synthesize": 409, "watermark": 409}
    assert all(r.json() == {"error": "session_ended"} for r in responses.values())
    assert provider.calls == 0


async def test_the_ended_guard_does_not_fire_before_the_session_is_ended():
    """The other half of the guard, and not a tautology: a `_unavailable` that
    returned 409 unconditionally would pass every assertion in the test above.
    All four routes answer normally while the session is live."""
    provider = FakeProvider(scripts=[MERGE_NOOP, MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = await _session(client)

        assert (await client.post(f"/v1/sessions/{sid}/findings",
                                  json={"findings": [_finding_json("f-1")]})
                ).status_code == 200
        assert (await client.post(f"/v1/sessions/{sid}/synthesize")).status_code == 200
        assert (await client.get(f"/v1/sessions/{sid}/watermark",
                                 params={"contributor": "akhil"})).status_code == 200
        assert (await client.post(f"/v1/sessions/{sid}/query",
                                  json={"query": "anything", "contributor": "akhil"})
                ).status_code == 200


async def test_a_member_can_still_leave_a_session_that_ended_underneath_them():
    """DELETE .../members/{c} is deliberately OUTSIDE the ended guard: it is
    exactly what a client does after seeing a 409, and gating it would trap
    every member inside a dead session with a binding they cannot clear."""
    async with _client(FakeProvider(scripts=[])) as client:
        sid = await _session(client, created_by="siddsing")
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "akhil"})
        await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "siddsing"})

        r = await client.delete(f"/v1/sessions/{sid}/members/akhil")

        assert r.status_code == 200
        assert r.json() == {"members": []}


async def test_termination_survives_a_rebuild_from_the_log_alone():
    """Terminate is an EVENT, not a flag, and this is what that buys (adr/0004
    and the spec's Durability caveat). `rebuild()` discards every index and
    replays the log; a session that came back ACTIVE would mean the closure
    was living somewhere the log is not -- which is precisely the state that
    would not survive the restart + resync path this system recovers through.
    """
    app = build_app(FakeProvider(scripts=[MERGE_NOOP]))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://svc") as client:
        sid = await _session(client, created_by="siddsing")
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding_json("f-1")]})
        await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "siddsing"})

        app.state.store._memories[sid].rebuild()

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "anything", "contributor": "akhil"})

    assert r.status_code == 409
    assert app.state.store.get_context(sid).status == "ended"


# ── item 4: re-join keeps your place ────────────────────────────────────────

async def test_rejoining_from_a_new_agent_session_preserves_last_seen():
    """Spec Testing item 4 -- THE regression the identity re-key exists for.

    aditya reads the memory from one conversation, leaves, and comes back in a
    different one: a new Claude Code window, therefore a new
    `agent_session_id` (it is the transcript filename stem,
    worker/discovery.py:112), same human. Keyed on the conversation, the
    second window was an unknown key with `last_seen` 0, so `new_since`
    reported the whole Shared Memory as new to someone who had just read it
    and the briefing opened by telling them about their own findings.

    The teammate's finding is what makes `new_since` non-zero to begin with
    (aditya's own would be suppressed), and `agent_session` is varied on the
    REQUEST while `contributor` is held fixed -- if anything re-keys onto the
    conversation id again, this goes to 1."""
    provider = FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = await _session(client)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "aditya"})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding_json("f-akhil",
                                                           contributor="akhil")]})

        before = (await client.get(f"/v1/sessions/{sid}/watermark",
                                   params={"contributor": "aditya",
                                           "agent_session": "as-window-1"})).json()
        assert before["new_since"] == 1                # a verdict round they have not seen

        # aditya reads it, in window 1
        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "what do we know", "contributor": "aditya",
                                "agent_session": "as-window-1"})

        # ...leaves, and re-joins from a DIFFERENT conversation
        await client.delete(f"/v1/sessions/{sid}/members/aditya")
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "aditya"})

        after = (await client.get(f"/v1/sessions/{sid}/watermark",
                                  params={"contributor": "aditya",
                                          "agent_session": "as-window-2"})).json()

    assert after["new_since"] == 0                     # their place was kept
    assert after["members"] == ["aditya"]              # and the re-join landed


# ── W2: two windows of one human are two participants ───────────────────────

async def test_two_windows_of_one_contributor_see_each_others_findings():
    """⟨INVERTED 2026-08-06 by the split, `docs/overnight/decisions/001`⟩ This
    test asserted the opposite until today, and the assertion it made was the
    defect W2 exists to remove.

    aditya has two Claude Code windows open on the same problem — which is the
    demo, not an edge case. Under the contributor-keyed rule every Attribution
    on window 1's findings named `aditya`, so window 2 was shown NONE of them:
    the two windows were the same participant and could not learn from each
    other at all, silently. One agent invocation is one Agent Session, so what
    window 2 already has in its context window is what window 2 produced —
    that, and only that, is what suppression may hide from it.

    The rejoin fix the contributor key was introduced for is kept whole, one
    layer over: the watermark is still keyed by Contributor
    (`store.last_seen`), pinned by
    `test_rejoining_as_the_same_contributor_keeps_their_watermark` above.

    The retriever ranks every index it could possibly be offered rather than
    just `[0]`: `[0]` returns whatever the lanes ordered first, so a re-key
    regression could slip through on ordering alone. Out-of-range indices are
    dropped by `query_findings`."""
    provider = FakeProvider(scripts=[MERGE_NOOP, {"ranked": list(range(21))}])
    async with _client(provider) as client:
        sid = await _session(client)
        mine_window_1 = [_finding_json(f"f-w1-{i:02d}", contributor="aditya",
                                       agent_session="as-window-1") for i in range(10)]
        mine_window_2 = [_finding_json(f"f-w2-{i:02d}", contributor="aditya",
                                       agent_session="as-window-2") for i in range(10)]
        theirs = [_finding_json("f-akhil", contributor="akhil", agent_session="as-akhil",
                                text="akhil-marker: the decode fails past 40 ms")]
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": mine_window_1 + mine_window_2 + theirs})

        # aditya asks from window 2, about work aditya did in window 1
        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "insight", "contributor": "aditya",
                                    "agent_session": "as-window-2"})

    returned = [f["id"] for f in r.json()["findings"]]
    # window 1's work reaches window 2, exactly like a teammate's...
    assert {fid for fid in returned if fid.startswith("f-w1-")} == {
        f"f-w1-{i:02d}" for i in range(10)}
    assert "f-akhil" in returned
    # ...and window 2's own findings are still suppressed for window 2, which
    # is what suppression is FOR: they are already in the context window asking.
    assert not any(fid.startswith("f-w2-") for fid in returned)


async def test_the_watermark_splits_its_two_halves_between_the_two_keys():
    """The split, on the one route that reads BOTH keys at once.

    `by_type`/`conflicts`/`topics` are CONTENT — "what would this asker
    actually see" — so they suppress by the asking CONVERSATION. `new_since`
    is CHANGE — "how much has the memory moved since I last looked" — and
    stays keyed by the CONTRIBUTOR, which is what keeps a new conversation
    from replaying the entire memory at the same human.

    Both halves in one test on purpose: they are only correct together, and
    keying either one the other way is a silent behaviour change with no
    error attached."""
    provider = FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = await _session(client)
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-w1", contributor="aditya", agent_session="as-window-1"),
            _finding_json("f-akhil", contributor="akhil", agent_session="as-akhil"),
        ]})

        # aditya reads, from window 1.
        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "what do we know", "contributor": "aditya",
                                "agent_session": "as-window-1"})

        w1 = (await client.get(f"/v1/sessions/{sid}/watermark",
                               params={"contributor": "aditya",
                                       "agent_session": "as-window-1"})).json()
        w2 = (await client.get(f"/v1/sessions/{sid}/watermark",
                               params={"contributor": "aditya",
                                       "agent_session": "as-window-2"})).json()

    # CHANGE: one person, one place in the memory — window 2 is not told the
    # whole session is new just because it is a different conversation.
    assert w1["new_since"] == 0 and w2["new_since"] == 0
    # CONTENT: window 1 is not shown its own finding; window 2 is, because it
    # has never seen it.
    assert w1["by_type"] == {"learning": 1}
    assert w2["by_type"] == {"learning": 2}


async def test_a_window_still_never_reads_its_own_findings_back():
    """The other side of the split, on the smallest possible fixture, so the
    inversion above cannot be read as "suppression was switched off"."""
    provider = FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0, 1]}])
    async with _client(provider) as client:
        sid = await _session(client)
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-mine", contributor="aditya", agent_session="as-window-1"),
            _finding_json("f-theirs", contributor="akhil", agent_session="as-akhil"),
        ]})

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "what do we know", "contributor": "aditya",
                                    "agent_session": "as-window-1"})

    assert [f["id"] for f in r.json()["findings"]] == ["f-theirs"]


# ── the wire change is additive ─────────────────────────────────────────────

async def test_an_old_shaped_request_carrying_only_agent_session_still_works():
    """The contract change had to be ADDITIVE, and this is what says so.

    `orchestrator/server.py:141` and `briefing.py:80` send `agent_session` and
    run as separate processes on other people's laptops. A hard rename would
    not have errored -- it would have made every un-upgraded client anonymous,
    silently switching their suppression off and resetting their watermark.

    So an old-shaped request keeps EXACTLY today's behaviour: its own findings
    are still suppressed and its watermark still advances.

    ⟨STRENGTHENED 2026-08-06 review⟩ This fixture used to attribute the asker's
    own finding `contributor="as-legacy"` -- an Agent Session id sitting in the
    Contributor field, a shape no real client produces -- and so it green-lit
    an additivity claim that did not hold on real data. On REAL data
    (`contributor="aditya"`, `agent_session="as-legacy"`, which is what every
    Attribution actually looks like) the contributor re-key compared the old
    client's `agent_session` value against `a.contributor`, never matched, and
    switched that client's suppression off entirely: it got its OWN finding
    back as team knowledge, credited to itself.

    ⟨UNCHANGED BY THE SPLIT, 2026-08-06⟩ That defect needed an explicit hatch
    (`api._legacy_agent_session`) only while the Contributor was the key. Since
    the split the old client's field IS the key, so this passes through the
    primary path with no special case at all, and the hatch is deleted. The
    assertions did not move: an old-shaped request still has its own findings
    suppressed and its watermark still advances.

    The retriever is scripted to rank EVERY index it could be offered, not just
    `[0]`. With `[0]` the assertion depended on which finding the lanes happened
    to order first, and passed against an un-guarded build by luck -- verified
    by mutation 2026-08-06. Ranking both indices means the test sees the whole
    visible set, so a finding that should have been suppressed cannot hide
    behind an ordering. Index 1 is simply skipped when only one finding is
    visible -- `query_findings` bounds every index against `len(visible)`."""
    provider = FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0, 1]}])
    async with _client(provider) as client:
        sid = await _session(client)
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-mine", contributor="aditya", agent_session="as-legacy"),
            _finding_json("f-theirs", contributor="akhil", agent_session="as-akhil"),
        ]})

        # No `contributor` key anywhere in this exchange -- the old shape.
        before = (await client.get(f"/v1/sessions/{sid}/watermark",
                                   params={"agent_session": "as-legacy"})).json()
        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "what do we know",
                                    "agent_session": "as-legacy"})
        after = (await client.get(f"/v1/sessions/{sid}/watermark",
                                  params={"agent_session": "as-legacy"})).json()

    assert r.status_code == 200
    assert [f["id"] for f in r.json()["findings"]] == ["f-theirs"]   # own one suppressed
    assert before["new_since"] == 1 and after["new_since"] == 0      # watermark advanced


async def test_agent_session_decides_suppression_when_a_request_carries_both_fields():
    """⟨INVERTED 2026-08-06 by the split, `docs/overnight/decisions/001`⟩ Both
    fields on one request is the shape every upgraded orchestrator sends, and
    the two are now read for two different jobs: `agent_session` decides
    suppression, `contributor` keys the watermark and attribution. If the
    precedence were the other way round the split would ship dead — an
    upgraded client would get the contributor-keyed behaviour and two windows
    would still be one participant.

    Same person, two windows: aditya asks from window 2 about two findings,
    one produced in window 1 and one by a teammate. Both are visible, because
    neither is in the asking context window."""
    provider = FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0, 1]}])
    async with _client(provider) as client:
        sid = await _session(client)
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding_json("f-w1", contributor="aditya", agent_session="as-1"),
            _finding_json("f-theirs", contributor="akhil", agent_session="as-akhil"),
        ]})

        r = await client.post(f"/v1/sessions/{sid}/query",
                              json={"query": "what do we know", "contributor": "aditya",
                                    "agent_session": "as-2"})

    assert {f["id"] for f in r.json()["findings"]} == {"f-w1", "f-theirs"}


# ── an UNKNOWN creator, 2026-08-06 ──────────────────────────────────────────
#
# `SynapseSession.created_by` became `str | None`. The defect that forced it:
# `cli.cmd_resync` POSTed `created_by="resync"` when recreating a retained
# session, which is inert against a LIVE service (create-or-return) and a real
# CREATE against the empty store of a RESTARTED one -- so after a restart the
# session belonged to the string "resync", the creator-only gate refused the
# human who owned it, and anyone who read the source could close it by sending
# `{"ended_by": "resync"}`. It refused the legitimate owner and admitted
# everyone else, which is worse than no gate at all.
#
# None is the honest "we do not know", and these six tests are the contract:
# a member may end it, a stranger may not, a session with a REAL creator is
# untouched, and recovery can never downgrade a live session's ownership.


async def test_a_session_with_no_known_creator_can_be_ended_by_a_current_member():
    """The None arm's positive half. This is the human whose session it
    actually is: after the restart their orchestrator re-joins, so they are a
    member, and the alternative to letting them close it is a Shared Session
    that can never be closed by anyone -- the team punished for the service's
    lack of durability.

    The 409 at the end is what says it really closed, rather than returning
    200 while leaving the session live."""
    async with _client(FakeProvider(scripts=[])) as client:
        sid = await _session(client, created_by=None)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "siddsing"})

        r = await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "siddsing"})

        assert r.status_code == 200
        assert r.json()["ended_by"] == "siddsing"
        after = await client.post(f"/v1/sessions/{sid}/query",
                                  json={"query": "anything", "contributor": "siddsing"})
        assert after.status_code == 409


async def test_a_session_with_no_known_creator_refuses_a_non_member():
    """The None arm's negative half, and the reason the fallback is MEMBERSHIP
    rather than "anyone" -- without this, losing the creator would silently
    turn the gate off.

    Both halves of the message are asserted because both are load-bearing: an
    agent told only "membership is required" reports a permissions problem to
    its human, when what happened is that a restart lost the owner and joining
    is the remedy.

    `provider.calls == 0` and the surviving query are the second witness that
    the refusal happened BEFORE anything was closed -- a 403 returned after
    `store.end_session` would pass the status assertion alone."""
    provider = FakeProvider(scripts=[MERGE_NOOP, {"ranked": [0]}])
    async with _client(provider) as client:
        sid = await _session(client, created_by=None)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "siddsing"})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding_json("f-1")]})

        r = await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "a-stranger"})

        assert r.status_code == 403
        message = r.json()["error"]
        assert "member" in message and "restart" in message
        # ...and the memory is still there.
        still = await client.post(f"/v1/sessions/{sid}/query",
                                  json={"query": "anything", "contributor": "siddsing"})
        assert still.status_code == 200


async def test_a_real_creator_is_still_the_only_one_who_can_end_it():
    """The UNCHANGED arm, driven by a MEMBER who is not the creator -- which is
    the case the new code could plausibly have broken and the existing
    non-creator test could not see (its `akhil` never joined, so a gate that
    fell through to membership would still have refused him).

    Ending is creator-only whenever there IS a creator; membership is the
    fallback only when there is not."""
    async with _client(FakeProvider(scripts=[])) as client:
        sid = await _session(client, created_by="siddsing")
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "akhil"})

        r = await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "akhil"})

        assert r.status_code == 403
        assert r.json() == {"error": "only siddsing can end this session"}
        assert (await client.post(f"/v1/sessions/{sid}/end",
                                  json={"ended_by": "siddsing"})).status_code == 200


async def test_created_by_may_be_null_but_the_key_is_still_required():
    """The two halves of "required KEY, nullable VALUE", together in one test
    because it is the DISTINCTION that matters and each half alone is
    satisfiable by the wrong implementation: a required non-null field passes
    the second assertion, and an optional field with a `None` default passes
    the first.

    Omitting the key must stay a 422 so that a typo, or a half-written client,
    cannot quietly mint a session nobody owns. Not knowing who created a
    session has to be ASSERTED."""
    async with _client(FakeProvider(scripts=[])) as client:
        explicit_null = await client.post("/v1/sessions",
                                          json={"purpose": "p", "created_by": None})
        absent = await client.post("/v1/sessions", json={"purpose": "p"})

    assert explicit_null.status_code == 201
    assert explicit_null.json()["created_by"] is None
    assert absent.status_code == 422 and "created_by" in absent.json()["error"]


async def test_joining_returns_the_session_identity_a_joiner_must_record():
    """`created_by` and `purpose` on the join response (2026-08-06).

    A machine that JOINED a session rather than creating it never saw either
    field anywhere: `POST .../members` returned `{"members": [...]}` and
    nothing else. So when the service restarted, that machine's `resync` had
    no record to restore from and invented a creator instead -- the defect
    this section exists for. The joiner can only retain what the service tells
    it, and this call is the one it is guaranteed to make.

    `members` is asserted unchanged in the same breath: the change is additive
    and an un-upgraded client must keep reading exactly what it read before."""
    async with _client(FakeProvider(scripts=[])) as client:
        r = await client.post("/v1/sessions", json={"purpose": "fec decode",
                                                    "created_by": "siddsing"})
        sid = r.json()["shared_id"]

        joined = await client.post(f"/v1/sessions/{sid}/members",
                                   json={"contributor": "aditya"})

    assert joined.status_code == 200
    assert joined.json() == {"members": ["aditya"], "created_by": "siddsing",
                             "purpose": "fec decode"}


async def test_recreating_a_live_session_with_created_by_none_does_not_downgrade_it():
    """THE important one. Recovery must never make a live session's ownership
    WORSE than it already is.

    `cmd_resync` is create-or-return and safe to call unconditionally, so it
    fires against a service that never restarted just as often as against one
    that did. If a resync with no local record (`created_by: null`) overwrote
    the creator of a session that is perfectly alive, this change would have
    replaced "the gate names the wrong person" with "the gate silently
    downgrades to membership", which is the same defect wearing a different
    hat.

    The last assertion is the real one: a member who is NOT the creator is
    still refused afterwards, which is only true if the None arm never
    engaged. Reading `created_by` back would not prove that on its own -- a
    response can be right while the gate reads something else."""
    async with _client(FakeProvider(scripts=[])) as client:
        sid = await _session(client, created_by="siddsing")
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "akhil"})

        again = await client.post("/v1/sessions",
                                  json={"purpose": "(recovered by resync)",
                                        "created_by": None, "shared_id": sid})

        assert again.status_code == 200                       # existed already
        assert again.json()["created_by"] == "siddsing"       # unchanged
        assert again.json()["purpose"] == "fec decode"        # unchanged

        r = await client.post(f"/v1/sessions/{sid}/end", json={"ended_by": "akhil"})
        assert r.status_code == 403
        assert r.json() == {"error": "only siddsing can end this session"}
