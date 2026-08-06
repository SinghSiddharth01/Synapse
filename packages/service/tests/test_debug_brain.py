"""The brain page — `/debug` and `/debug/brain.json` (W4a).

Over ASGI, no sockets, matching `test_debug.py`'s style. The requirement this
suite pins is one line — *"the top level shows the state of the brain, not the
log"* — and the property that makes it trustworthy is narrower: every number on
that page comes from something already in the process, and everything the
service cannot measure is ABSENT rather than approximated.

So the tests come in two halves. One half asserts the page reports what
happened. The other half asserts it reports NOTHING where nothing is known:
`behind` is null for someone who never read, revisions are empty (and labelled)
before any rewrite is observed, and no row anywhere claims a participant is
connected.
"""

from __future__ import annotations

import httpx
from synapse_providers import FakeProvider

from synapse_service.api import build_app

# A structurally valid verdict that merges nothing and rewrites the prose.
# Version bumps once per VERDICT ROUND, merges or not.
def _verdict(working_memory: str | None = "the team is chasing a decode failure",
             merges: list | None = None) -> dict:
    verdict: dict = {"merges": merges or [], "trivial_ids": [], "conflicts": []}
    if working_memory is not None:
        verdict["working_memory"] = working_memory
    return verdict


# The seg-005 golden pair's merge, as test_debug.py scripts it.
MERGE_SCRIPT = {
    "working_memory": "Team is chasing a decode failure: a ~40 ms timing window, load-dependent.",
    "merges": [{
        "source_ids": ["f-005a-01", "f-005b-01"],
        "text": "The decode failure is a ~40 ms timing window between the two DMA "
                "writes, and it only manifests under load.",
        "type": "learning",
    }],
    "trivial_ids": [],
    "conflicts": [],
}


def _finding(fid: str, contributor: str, agent_session: str, *,
             agent: str = "claude-code", text: str = "a thing we learned",
             provenance: str = "distilled",
             ts: str = "2026-08-04T12:00:00Z") -> dict:
    return {"id": fid, "type": "learning", "text": text,
            "attributions": [{"contributor": contributor,
                              "agent_session": agent_session, "agent": agent}],
            "ts": ts, "refs": [], "provenance": provenance,
            "status": "kept", "merged_from": [], "merged_into": None}


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://svc")


async def _new_session(client, purpose: str = "fec decode",
                       created_by: str | None = "sid") -> str:
    r = await client.post("/v1/sessions",
                          json={"purpose": purpose, "created_by": created_by})
    return r.json()["shared_id"]


async def _brain(client, sid: str | None = None) -> dict:
    params = {"session": sid} if sid else {}
    r = await client.get("/debug/brain.json", params=params)
    assert r.status_code == 200
    return r.json()


def _row(payload: dict, contributor: str, agent_session: str | None):
    for row in payload["session"]["participants"]:
        if row["contributor"] == contributor and row["agent_session"] == agent_session:
            return row
    raise AssertionError(
        f"no participant row for {contributor}/{agent_session} in "
        f"{payload['session']['participants']}")


# ---------------------------------------------------------------------------
# Routing and mounting
# ---------------------------------------------------------------------------

async def test_brain_json_lists_sessions_and_defaults_to_the_first() -> None:
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        first = await _new_session(client, "first purpose")
        second = await _new_session(client, "second purpose")

        no_param = await _brain(client)
        assert [s["shared_id"] for s in no_param["sessions"]] == [first, second]
        assert no_param["session"]["sid"] == first

        # An unknown id falls back to the first rather than 404ing -- same
        # rule stats.json has always had.
        unknown = await _brain(client, "sh-nope")
        assert unknown["session"]["sid"] == first

        assert (await _brain(client, second))["session"]["sid"] == second
        assert (await _brain(client, second))["session"]["purpose"] == "second purpose"


async def test_brain_json_with_no_sessions_is_a_null_session() -> None:
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        payload = await _brain(client)
        assert payload["sessions"] == []
        assert payload["session"] is None


async def test_brain_json_rejects_writes() -> None:
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        assert (await client.post("/debug/brain.json", json={})).status_code == 405


async def test_brain_routes_are_absent_when_debug_is_disabled() -> None:
    app = build_app(FakeProvider(scripts=[]), debug=False)
    async with _client(app) as client:
        assert (await client.get("/debug")).status_code == 404
        assert (await client.get("/debug/log")).status_code == 404
        assert (await client.get("/debug/brain.json")).status_code == 404
    # Not merely unreachable -- absent. A disabled dashboard must not retain a
    # ring of Working Memory prose that nothing can ever read.
    assert app.state.working_memory_log is None


async def test_brain_page_has_required_ids() -> None:
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        r = await client.get("/debug")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        for element_id in ("session-select", "wm-body", "revisions",
                           "participants", "recent"):
            assert f'id="{element_id}"' in r.text


async def test_the_log_page_moved_intact() -> None:
    # Verbatim: same constant, same script, different mount. If this page ever
    # stops carrying its own two ids, the move stopped being a move.
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        r = await client.get("/debug/log")
        assert r.status_code == 200
        assert 'id="log-tail"' in r.text
        assert 'id="feed"' in r.text


async def test_the_brain_page_makes_zero_external_requests() -> None:
    # The whole document in one response: no CDN, no font, no image. This is
    # what lets the page be opened on a demo machine with no network.
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        body = (await client.get("/debug")).text
    assert "<script src" not in body
    assert "http://" not in body
    assert "https://" not in body
    assert 'href="data:,"' in body


# ---------------------------------------------------------------------------
# The roster — the W2 properties, which became reachable only tonight
# ---------------------------------------------------------------------------

async def test_two_agent_sessions_of_one_contributor_are_two_rows() -> None:
    """THE reason this page exists now. Before W2, a second window of the same
    agent overwrote the first's binding and silently BECAME the same
    participant; the state this test sets up was unreachable."""
    async with _client(build_app(FakeProvider(scripts=[_verdict()]))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "sid"})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-window-1")]})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-2", "sid", "as-window-2")]})

        payload = await _brain(client, sid)
        session = payload["session"]

        rows = [r for r in session["participants"] if r["agent_session"] is not None]
        assert len(rows) == 2
        assert {r["agent_session"] for r in rows} == {"as-window-1", "as-window-2"}
        assert all(r["contributor"] == "sid" for r in rows)
        assert all(r["state"] == "active" for r in rows)

        # One human, two conversations -- and the page says both numbers
        # rather than picking one.
        assert session["counts"]["contributors"] == 1
        assert session["counts"]["contributors_in_log"] == 1
        assert session["counts"]["conversations"] == 2


async def test_a_merge_does_not_inflate_anybody_s_contributions() -> None:
    """A Synthesized Finding carries EVERY source's attributions. Counting
    `Merged` entries in the roster would make every author look busier after
    each merge than before, and would invent no new participant while doing
    it."""
    provider = FakeProvider(scripts=[MERGE_SCRIPT])
    async with _client(build_app(provider)) as client:
        sid = await _new_session(client)
        push = await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding("f-005a-01", "aditya", "as-a", text="timing window, ~40 ms"),
            _finding("f-005b-01", "akhil", "as-b", agent="codex",
                     text="only reproduces under load"),
        ]})
        assert push.json()["synthesized"] is True

        session = (await _brain(client, sid))["session"]
        rows = {r["agent_session"]: r for r in session["participants"]}

        assert set(rows) == {"as-a", "as-b"}          # no third participant
        assert rows["as-a"]["contributions"] == 1
        assert rows["as-b"]["contributions"] == 1
        assert session["counts"]["conversations"] == 2
        # The merge really did happen -- this is not a green test on a session
        # where nothing occurred.
        assert session["counts"]["superseded"] == 2


async def test_a_member_who_has_pushed_nothing_is_listening() -> None:
    """The demo's teammate #2, before their first finding. One row, no
    conversation: a member list carries no Agent Session, and inventing one
    would be the exact fabrication this page refuses."""
    async with _client(build_app(FakeProvider(scripts=[_verdict()]))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "sid"})
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "aditya"})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-window-1")]})

        session = (await _brain(client, sid))["session"]
        listening = _row({"session": session}, "aditya", None)
        assert listening["state"] == "listening"
        assert listening["contributions"] == 0
        assert listening["agent_session"] is None
        assert listening["last_contribution_iso"] is None
        assert session["counts"]["contributors"] == 2
        assert session["counts"]["conversations"] == 1


async def test_a_member_who_left_keeps_their_row_and_their_findings() -> None:
    """Leaving is not ending: their findings stay in the log attributed to
    them, so the row stays too -- as `left`."""
    async with _client(build_app(FakeProvider(scripts=[_verdict()]))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "aditya"})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "aditya", "as-a")]})

        assert _row(await _brain(client, sid), "aditya", "as-a")["state"] == "active"

        assert (await client.delete(
            f"/v1/sessions/{sid}/members/aditya")).status_code == 200

        session = (await _brain(client, sid))["session"]
        left = _row({"session": session}, "aditya", "as-a")
        assert left["state"] == "left"
        assert left["contributions"] == 1
        assert session["counts"]["contributors"] == 0        # membership is gone
        assert session["counts"]["contributors_in_log"] == 1  # the log is not
        assert session["recent"][0]["authors"] == ["aditya"]


async def test_a_contributor_who_never_registered_is_not_reported_as_left() -> None:
    """`left` asserts a human action. Absence from `members` does not: nothing
    on the ingest path calls `add_member`, so a raw `POST /findings` -- which
    is exactly what `docs/demo-script.md`'s Beats 1-6 do -- produces a
    contributor who is in the log and was never in the member list. Calling
    that "left" puts a false claim about a teammate on a page that may be on
    camera, directly above a Contributors tile reading 0."""
    async with _client(build_app(FakeProvider(scripts=[_verdict()]))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "aditya", "as-a")]})

        session = (await _brain(client, sid))["session"]
        row = _row({"session": session}, "aditya", "as-a")
        assert row["state"] == "unregistered"
        assert row["contributions"] == 1
        # Both halves of what the tile shows, so the 0 is never alone.
        assert session["counts"]["contributors"] == 0
        assert session["counts"]["contributors_in_log"] == 1


async def test_re_joining_retracts_the_departure() -> None:
    """A member who leaves and comes back is present, not `left` -- and the
    stale marker must not survive to make some later absence read as a second
    departure that never happened."""
    async with _client(build_app(FakeProvider(scripts=[_verdict()]))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "aditya"})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "aditya", "as-a")]})
        await client.delete(f"/v1/sessions/{sid}/members/aditya")
        assert _row(await _brain(client, sid), "aditya", "as-a")["state"] == "left"

        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "aditya"})
        assert _row(await _brain(client, sid), "aditya", "as-a")["state"] == "active"

        # And after leaving again it is a departure once more, not a stuck one.
        await client.delete(f"/v1/sessions/{sid}/members/aditya")
        assert _row(await _brain(client, sid), "aditya", "as-a")["state"] == "left"


async def test_behind_tracks_the_contributor_s_watermark() -> None:
    provider = FakeProvider(scripts=[_verdict("first"), {"ranked": [0]},
                                     _verdict("second")])
    async with _client(build_app(provider)) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "sid"})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-a")]})

        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "what do we know", "contributor": "sid",
                                "agent_session": "as-a"})
        brain = await _brain(client, sid)
        row = _row(brain, "sid", "as-a")
        assert row["behind"] == 0
        # The watermark itself, not just the derived gap: `behind == 0` also
        # holds when both sides are 0, which is the exact ambiguity the row
        # below this one exists to refuse.
        assert brain["session"]["memory_version"] == 1
        assert row["last_seen_version"] == 1

        # One more verdict round moves the memory out from under them.
        await client.post(f"/v1/sessions/{sid}/synthesize")
        row_after = _row(await _brain(client, sid), "sid", "as-a")
        assert row_after["behind"] == 1


async def test_someone_who_never_queried_reports_unknown_not_maximally_stale() -> None:
    """`store.last_seen` defaults to 0, which is indistinguishable from
    genuinely sitting at v0. Reporting `behind: 7` for someone who has simply
    never asked is the confident-wrong-number failure this page is built to
    avoid."""
    async with _client(build_app(FakeProvider(scripts=[_verdict()]))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "sid"})
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-a")]})

        session = (await _brain(client, sid))["session"]
        assert session["memory_version"] == 1         # the memory HAS moved
        row = _row({"session": session}, "sid", "as-a")
        assert row["last_query_iso"] is None
        assert row["last_query_scope"] == "none"
        assert row["behind"] is None
        assert row["last_seen_version"] is None


async def test_last_query_scope_says_whether_it_is_the_conversation_or_the_person() -> None:
    provider = FakeProvider(scripts=[_verdict(), {"ranked": []}, {"ranked": []}])
    async with _client(build_app(provider)) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/members", json={"contributor": "sid"})
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding("f-1", "sid", "as-window-1"),
            _finding("f-2", "sid", "as-window-2"),
        ]})

        # Named its conversation: a fact about THIS window.
        await client.post(f"/v1/sessions/{sid}/query",
                          json={"query": "q1", "contributor": "sid",
                                "agent_session": "as-window-1"})
        payload = await _brain(client, sid)
        first = _row(payload, "sid", "as-window-1")
        assert first["last_query_scope"] == "conversation"
        assert first["last_query_iso"] is not None

        # The other window named no conversation of its own, so it falls back
        # to the person -- and the payload SAYS it fell back rather than
        # presenting a per-person time as a per-window one.
        second = _row(payload, "sid", "as-window-2")
        assert second["last_query_scope"] == "contributor"
        assert second["last_query_iso"] == first["last_query_iso"]


# ---------------------------------------------------------------------------
# Working memory and its revisions
# ---------------------------------------------------------------------------

async def test_a_rewrite_appends_one_revision() -> None:
    async with _client(build_app(FakeProvider(scripts=[_verdict("the first prose")]))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-a")]})

        wm = (await _brain(client, sid))["session"]["working_memory"]
        assert wm["text"] == "the first prose"
        assert wm["words"] == 3
        assert wm["version"] == 1
        assert len(wm["revisions"]) == 1
        revision = wm["revisions"][0]
        assert revision["version"] == 1
        assert revision["words"] == 3
        assert revision["text"] == "the first prose"
        assert revision["ts_iso"]
        assert wm["updated_iso"] == revision["ts_iso"]


async def test_a_round_that_omits_the_rewrite_books_no_revision() -> None:
    """`store.set_context` treats None as LEAVE ALONE and the verdict schema
    makes `working_memory` optional, so a version bump carrying the same prose
    is ordinary. A version changed; a revision did not."""
    provider = FakeProvider(scripts=[_verdict("the only prose"),
                                     _verdict(working_memory=None)])
    async with _client(build_app(provider)) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-a")]})
        await client.post(f"/v1/sessions/{sid}/synthesize")

        session = (await _brain(client, sid))["session"]
        assert session["memory_version"] == 2            # the round DID happen
        assert session["working_memory"]["text"] == "the only prose"
        assert len(session["working_memory"]["revisions"]) == 1


async def test_the_revision_ring_is_capped_at_ten_newest_first() -> None:
    rounds = [_verdict(f"prose revision {i}") for i in range(12)]
    async with _client(build_app(FakeProvider(scripts=rounds))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-a")]})
        for _ in range(11):
            await client.post(f"/v1/sessions/{sid}/synthesize")

        wm = (await _brain(client, sid))["session"]["working_memory"]
        assert len(wm["revisions"]) == 10
        texts = [r["text"] for r in wm["revisions"]]
        assert texts[0] == "prose revision 11"          # newest first
        assert texts[-1] == "prose revision 2"
        assert "prose revision 0" not in texts          # the two oldest are gone
        assert "prose revision 1" not in texts
        # Versions descend with the list; the oldest RETAINED revision reports
        # a null delta rather than a delta against a revision that is gone.
        assert [r["version"] for r in wm["revisions"]] == list(range(12, 2, -1))
        assert wm["revisions"][-1]["delta_words"] is None
        assert wm["revisions"][0]["delta_words"] == 0


async def test_before_any_synthesis_there_are_no_revisions_and_no_update_time() -> None:
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        sid = await _new_session(client)
        wm = (await _brain(client, sid))["session"]["working_memory"]
        assert wm["text"] == ""
        assert wm["revisions"] == []
        # Null, never a timestamp: the page must say the history is not
        # persisted rather than render an empty list as "nothing changed".
        assert wm["updated_iso"] is None


async def test_updated_iso_never_dates_prose_no_revision_matches() -> None:
    """`_record_synthesis_feed` is the only recorder, so any rewrite reaching
    `store.set_context` by another path leaves the newest revision stale --
    and dating the CURRENT prose by an observation of DIFFERENT prose is the
    confident-wrong-timestamp this page exists to refuse. Latent today (both
    merge call sites run the recorder), pinned so it stays latent."""
    app = build_app(FakeProvider(scripts=[_verdict("the observed prose")]))
    async with _client(app) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-a")]})
        assert (await _brain(client, sid))["session"]["working_memory"]["updated_iso"]

        # A rewrite that goes around the recorder, which is the whole scenario.
        app.state.store.set_context(sid, working_memory="prose nobody watched arrive")

        wm = (await _brain(client, sid))["session"]["working_memory"]
        assert wm["text"] == "prose nobody watched arrive"
        assert wm["updated_iso"] is None
        # The observed revision is still shown -- it happened -- it just no
        # longer claims to be the timestamp of what is on screen.
        assert wm["revisions"][0]["text"] == "the observed prose"


# ---------------------------------------------------------------------------
# Session identity
# ---------------------------------------------------------------------------

async def test_status_is_reported_on_both_endpoints() -> None:
    """The routing note's open item: session status was computed on every
    `get_context` and visible on neither debug payload."""
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        sid = await _new_session(client, created_by="sid")

        assert (await _brain(client, sid))["session"]["status"] == "active"
        stats = await client.get("/debug/stats.json", params={"session": sid})
        assert stats.json()["session"]["status"] == "active"

        assert (await client.post(f"/v1/sessions/{sid}/end",
                                  json={"ended_by": "sid"})).status_code == 200

        brain = await _brain(client, sid)
        assert brain["session"]["status"] == "ended"
        assert brain["sessions"][0]["status"] == "ended"
        stats_after = await client.get("/debug/stats.json", params={"session": sid})
        assert stats_after.json()["session"]["status"] == "ended"


async def test_created_by_passes_through_including_the_honest_unknown() -> None:
    async with _client(build_app(FakeProvider(scripts=[]))) as client:
        owned = await _new_session(client, "owned", created_by="sid")
        assert (await _brain(client, owned))["session"]["created_by"] == "sid"

        # None means exactly one thing: recreated after a restart and nobody
        # knows who created it. The page says so; it does not invent a name.
        unknown = await _new_session(client, "recreated", created_by=None)
        assert (await _brain(client, unknown))["session"]["created_by"] is None


# ---------------------------------------------------------------------------
# The rows
# ---------------------------------------------------------------------------

async def test_recent_labels_contributed_listened_and_merged() -> None:
    provider = FakeProvider(scripts=[MERGE_SCRIPT])
    async with _client(build_app(provider)) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding("f-005a-01", "aditya", "as-a", text="timing window, ~40 ms"),
            _finding("f-005b-01", "akhil", "as-b", provenance="contributed",
                     text="only reproduces under load"),
        ]})

        recent = (await _brain(client, sid))["session"]["recent"]
        by_id = {row["id"]: row for row in recent}

        assert by_id["f-005a-01"]["provenance"] == "distilled"    # the worker listened
        assert by_id["f-005b-01"]["provenance"] == "contributed"  # the agent called contribute

        # The merge result is in the log ONLY as a `Merged` entry -- it is
        # never a FindingAppended -- so a "latest into memory" list that
        # walked appends alone would omit the thing synthesis does.
        merged = [row for row in recent if row["provenance"] == "synthesized"]
        assert len(merged) == 1
        assert merged[0]["status"] == "kept"

        # Both sources are tombstones now, and the fold is what says so --
        # not the producer's defaults, which said `kept`.
        assert by_id["f-005a-01"]["merged_into"] == merged[0]["id"]
        assert by_id["f-005b-01"]["merged_into"] == merged[0]["id"]


async def test_recent_carries_every_attribution_of_a_merged_finding() -> None:
    provider = FakeProvider(scripts=[MERGE_SCRIPT])
    async with _client(build_app(provider)) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding("f-005a-01", "aditya", "as-a", text="timing window, ~40 ms"),
            _finding("f-005b-01", "akhil", "as-b", agent="codex",
                     text="only reproduces under load"),
        ]})

        recent = (await _brain(client, sid))["session"]["recent"]
        merged = next(row for row in recent if row["provenance"] == "synthesized")

        # Never attributions[0]: a merged finding belongs to everyone it was
        # merged from, which is the rule the query route enforces on egress.
        assert merged["authors"] == ["aditya", "akhil"]
        assert {a["agent_session"] for a in merged["attributions"]} == {"as-a", "as-b"}
        assert {a["agent"] for a in merged["attributions"]} == {"claude-code", "codex"}


async def test_recent_is_newest_first_and_bounded() -> None:
    async with _client(build_app(FakeProvider(scripts=[_verdict()]))) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings", json={"findings": [
            _finding(f"f-{i:02d}", "sid", "as-a", text=f"finding {i}",
                     ts=f"2026-08-04T12:{i:02d}:00Z")
            for i in range(15)
        ]})

        recent = (await _brain(client, sid))["session"]["recent"]
        assert len(recent) == 12
        assert recent[0]["id"] == "f-14"
        # The COUNT is the true one, not the bounded row list -- the log page
        # is where the whole thing lives.
        assert (await _brain(client, sid))["session"]["counts"]["log_entries"] >= 15


# ---------------------------------------------------------------------------
# Read-only by construction
# ---------------------------------------------------------------------------

async def test_polling_brain_json_changes_nothing_and_calls_no_provider() -> None:
    provider = FakeProvider(scripts=[_verdict("settled prose")])
    app = build_app(provider)
    async with _client(app) as client:
        sid = await _new_session(client)
        await client.post(f"/v1/sessions/{sid}/findings",
                          json={"findings": [_finding("f-1", "sid", "as-a")]})

        store = app.state.store
        calls_before = provider.calls
        version_before = store.get_context(sid).memory_version
        entries_before = len(store.log_entries(sid))
        revisions_before = len(
            (await _brain(client, sid))["session"]["working_memory"]["revisions"])

        for _ in range(5):
            await _brain(client, sid)
            await client.get("/debug")

        assert provider.calls == calls_before
        assert store.get_context(sid).memory_version == version_before
        assert len(store.log_entries(sid)) == entries_before
        # The revision ring is written by the merge path, never by a read.
        assert len((await _brain(client, sid))["session"]["working_memory"]
                   ["revisions"]) == revisions_before
