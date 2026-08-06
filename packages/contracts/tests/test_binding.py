"""SessionBinding round-trip and durability."""

from __future__ import annotations

from datetime import datetime, timezone

from synapse_contracts import SessionBinding, clear_binding, read_binding, write_binding
from synapse_contracts.schemas import LocalBinding

TS = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _binding(**overrides) -> SessionBinding:
    base = dict(
        agent_session_id="as-1",
        shared_id="team-standup",
        contributor="aditya",
        agent="claude-code",
        transcript_path="/repo/transcript.jsonl",
        pinned_at=TS,
    )
    return SessionBinding(**{**base, **overrides})


def test_round_trips_through_disk(tmp_path) -> None:
    path = tmp_path / "bindings" / "active.json"
    original = _binding()

    write_binding(path, original)
    restored = read_binding(path)

    assert restored == original


def test_absent_file_reads_as_none(tmp_path) -> None:
    assert read_binding(tmp_path / "nope.json") is None


def test_corrupt_file_reads_as_none_not_raise(tmp_path) -> None:
    path = tmp_path / "active.json"
    path.write_text("{ not json", encoding="utf-8")

    assert read_binding(path) is None


def test_write_is_atomic(tmp_path) -> None:
    path = tmp_path / "active.json"
    write_binding(path, _binding())

    assert path.is_file()
    assert not path.with_suffix(".json.tmp").exists()
    # Nothing at all is left beside it — the temp file is unlinked on the
    # failure path and renamed away on the success path.
    assert [p.name for p in path.parent.iterdir()] == ["active.json"]


def test_two_writers_racing_for_one_path_neither_raise_nor_tear(tmp_path) -> None:
    """2026-08-06 review, reproduced. The temp file used to be `<path>.tmp` —
    ONE fixed name per destination — and since W2 every bind refreshes the
    shared mirror `bindings/<agent>.json`. Two windows joining at once both
    wrote that one temp file and both then `os.replace`d it; the loser got
    `FileNotFoundError` out of `os.replace`, which nothing up the call stack
    catches, so a concurrent `synapse-worker join` died with a traceback
    (measured across 4000 iterations x 2 processes).

    Threads rather than processes because the failure is a filesystem race on
    one path, not a GIL-sensitive one: `os.replace` and the write are both
    syscalls that release it. Both halves are asserted — no exception escaped,
    and every read of the destination parsed, since a torn write is the OTHER
    thing a shared temp file can produce."""
    import threading

    path = tmp_path / "bindings" / "claude-code.json"
    write_binding(path, _binding(shared_id="seed"))
    errors: list[BaseException] = []
    torn: list[str] = []

    def writer(shared_id: str) -> None:
        for _ in range(200):
            try:
                write_binding(path, _binding(shared_id=shared_id))
            except BaseException as exc:            # noqa: BLE001 — that IS the finding
                errors.append(exc)
                return
            if read_binding(path) is None:
                torn.append(shared_id)
                return

    threads = [threading.Thread(target=writer, args=(sid,))
               for sid in ("window-a", "window-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert torn == []
    assert read_binding(path).shared_id in {"window-a", "window-b"}
    # And no temp files survived the storm.
    assert [p.name for p in path.parent.iterdir()] == ["claude-code.json"]


def test_clear_removes_the_pin(tmp_path) -> None:
    path = tmp_path / "active.json"
    write_binding(path, _binding())

    clear_binding(path)

    assert read_binding(path) is None


def test_clear_on_absent_binding_does_not_raise(tmp_path) -> None:
    clear_binding(tmp_path / "never-existed.json")  # must not raise


def test_converts_to_local_binding_without_the_disk_only_fields() -> None:
    session = _binding()

    local = session.to_local_binding()

    assert isinstance(local, LocalBinding)
    assert local.agent_session_id == session.agent_session_id
    assert local.shared_id == session.shared_id
    assert local.contributor == session.contributor
    assert local.agent == session.agent
