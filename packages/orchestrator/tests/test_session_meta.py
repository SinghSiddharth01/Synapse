"""`sessions.json` — the locally retained `shared_id -> {created_by, purpose}`.

The module's whole contract is "answer honestly, and never raise": every
caller is either an MCP tool (nothing may raise out of one) or `cmd_resync`,
which runs before a demo recovery and must not die because a file on disk got
mangled. So the failure paths get as much attention here as the happy one —
they are the ones a `try/except` narrowed by a later edit would quietly lose.

Real files in a `tmp_path`, no mocks: the thing under test IS the disk
round trip.
"""

from __future__ import annotations

import json

from synapse_orchestrator.session_meta import (SessionMeta, record_session,
                                               retained_sessions, sessions_path)


def test_a_recorded_session_round_trips(tmp_path) -> None:
    record_session(tmp_path, "sh-1", created_by="siddsing", purpose="fec decode")

    assert retained_sessions(tmp_path) == {
        "sh-1": SessionMeta(created_by="siddsing", purpose="fec decode")}


def test_recording_creates_the_state_dir_and_leaves_no_temp_file(tmp_path) -> None:
    """The write is temp-file + `os.replace`, so a crash mid-write cannot leave
    a truncated `sessions.json` — an unreadable one degrades every session on
    this machine to "creator unknown". Asserting the temp file is gone is what
    distinguishes a real atomic swap from a write that merely happens to work."""
    state_dir = tmp_path / "not-created-yet"

    record_session(state_dir, "sh-1", created_by="siddsing", purpose="p")

    assert retained_sessions(state_dir) == {"sh-1": SessionMeta("siddsing", "p")}
    assert [p.name for p in state_dir.iterdir()] == ["sessions.json"]


def test_a_missing_file_reads_as_empty_and_never_raises(tmp_path) -> None:
    """`cmd_resync` calls this on a state dir that may hold nothing but a relay
    log, and every MCP tool path reaches it too. Empty is the answer; an
    exception is not one."""
    assert retained_sessions(tmp_path) == {}
    assert retained_sessions(tmp_path / "no" / "such" / "dir") == {}


def test_a_corrupt_file_reads_as_empty_and_never_raises(tmp_path) -> None:
    """Four ways `sessions.json` can be unreadable, all of which must degrade to
    "this machine knows nothing" rather than to a traceback.

    The non-utf-8 case is the one `ended.py` learned from an observed failure
    and records in its own comment: `read_text` runs the decode BEFORE json
    sees the bytes, so the error is a `UnicodeDecodeError` — a `ValueError`,
    NOT a `json.JSONDecodeError`. A handler narrowed to `JSONDecodeError`
    passes every other case here and dies on this one.
    """
    path = sessions_path(tmp_path)

    path.write_text("{not json at all", encoding="utf-8")
    assert retained_sessions(tmp_path) == {}

    path.write_bytes(b'{"sh-1": {"created_by": "\xff\xfe"}}')      # not utf-8
    assert retained_sessions(tmp_path) == {}

    path.write_text('["sh-1"]', encoding="utf-8")                  # a list, not a map
    assert retained_sessions(tmp_path) == {}

    path.write_text('""', encoding="utf-8")                        # valid json, wrong shape
    assert retained_sessions(tmp_path) == {}


def test_recording_over_a_corrupt_file_replaces_it_instead_of_raising(tmp_path) -> None:
    """`record_session` READS before it writes, so an unreadable file reaches it
    through `retained_sessions`. It must neither raise (its callers are MCP
    tools) nor refuse to write: the newly-learned session is worth more than
    whatever the mangled bytes were."""
    sessions_path(tmp_path).write_text("{ truncated", encoding="utf-8")

    record_session(tmp_path, "sh-1", created_by="siddsing", purpose="p")

    assert retained_sessions(tmp_path) == {"sh-1": SessionMeta("siddsing", "p")}


def test_an_entry_of_the_wrong_shape_is_dropped_without_losing_its_neighbours(
    tmp_path
) -> None:
    """Half a file written by an older or newer version is still worth the half
    that parses — one bad entry must not blank out the sessions either side of
    it, because the cost of forgetting a good one is a session resynced
    ownerless."""
    sessions_path(tmp_path).write_text(json.dumps({
        "sh-good": {"created_by": "siddsing", "purpose": "fec decode"},
        "sh-bad": "not an object",
        "sh-partial": {"created_by": 17, "purpose": None},
    }), encoding="utf-8")

    assert retained_sessions(tmp_path) == {
        "sh-good": SessionMeta("siddsing", "fec decode"),
        "sh-partial": SessionMeta(None, None),
    }


def test_a_none_never_overwrites_something_this_machine_already_knows(tmp_path) -> None:
    """The merge rule, and the reason it is not a plain replace.

    `join_session` records what the `/members` response tells it, and against a
    service that has not been upgraded to report `created_by` that is nothing.
    A replace would let a later join of a session this machine CREATED erase
    the creator it recorded at creation — putting resync straight back to
    recreating it ownerless, which is the failure this whole file exists to
    prevent.
    """
    record_session(tmp_path, "sh-1", created_by="siddsing", purpose="fec decode")

    record_session(tmp_path, "sh-1", created_by=None, purpose=None)

    assert retained_sessions(tmp_path) == {"sh-1": SessionMeta("siddsing", "fec decode")}


def test_a_later_observation_fills_in_what_was_unknown(tmp_path) -> None:
    """The other half of the merge: a join that learned nothing writes an entry
    with both fields None, and a later one that DOES learn the creator fills it
    in rather than being ignored as "already recorded"."""
    record_session(tmp_path, "sh-1", created_by=None, purpose=None)
    assert retained_sessions(tmp_path) == {"sh-1": SessionMeta(None, None)}

    record_session(tmp_path, "sh-1", created_by="aditya", purpose="fec decode")

    assert retained_sessions(tmp_path) == {"sh-1": SessionMeta("aditya", "fec decode")}


def test_recording_a_second_session_keeps_the_first(tmp_path) -> None:
    """One machine joins several sessions over its life, and `cmd_resync`
    recreates EVERY session its retained log names — so a second record that
    dropped the first would silently un-own every session but the newest."""
    record_session(tmp_path, "sh-1", created_by="siddsing", purpose="fec decode")
    record_session(tmp_path, "sh-2", created_by="aditya", purpose="npu timing")

    assert retained_sessions(tmp_path) == {
        "sh-1": SessionMeta("siddsing", "fec decode"),
        "sh-2": SessionMeta("aditya", "npu timing"),
    }
