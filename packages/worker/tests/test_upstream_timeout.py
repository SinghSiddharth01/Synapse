"""`upstream_timeout_s` — 120s, and the two wirings that deliver it (7418a63).

This setting had ZERO test references anywhere in the repo. Six hits across
`packages/`, `config/` and `scripts/`, all of them in source: the dataclass
default, the loader, and the two `HttpSink(...)` call sites.

That matters more than an ordinary coverage gap, because `HttpSink.__init__`
still defaults to **30.0**. The fix is not a changed default — it is two
keyword arguments at two call sites. Delete either one and the 30s bug comes
back silently, with a green suite and no changed default to notice in review.

The bug: the producer endpoint flushes to the Service before it answers, and
that flush includes synthesis — a model call on the HOST's machine. At 30s a
normal push timed out on the first try on EVERY batch and only landed on the
write-ahead log's retry. Nothing was lost, so nothing looked broken; every
batch just paid a full 30-second timeout first.

The TOML key is absent from config/synapse.toml entirely — the effective 120.0
comes from the dataclass default, which is why it is asserted here against the
real committed file rather than a fixture.
"""

from __future__ import annotations

import asyncio

import pytest
from synapse_distiller.config import load_config
from synapse_distiller.guards import CanaryResult

import synapse_worker.cli as cli
from synapse_worker.producer import HttpSink

SHIPPED_UPSTREAM_TIMEOUT = 120.0
SINK_DEFAULT_TIMEOUT = 30.0

PASSING_CANARY = CanaryResult(
    passed=True, answer="api.internal", input_tokens=49, detail="ok"
)

CONFIG = """
[distiller]
model = "test-model"
prompt_pack = "v2-hardened"
max_seconds_per_call = 30.0

[worker]
sink = "{sink}"
upstream_url = "http://127.0.0.1:10787/producer/findings"
{timeout_line}

[capability."test-model"]
usable_context = 4096
prefill_toks_per_sec = 250.0
response_reserve = 500
"""


@pytest.fixture(autouse=True)
def _isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SYNAPSE_UPSTREAM_TIMEOUT", raising=False)
    monkeypatch.delenv("SYNAPSE_SINK", raising=False)
    return tmp_path


class _RecordingSink:
    """Stands in for HttpSink and records exactly what the wiring handed it."""

    calls: list[tuple[str, float]] = []

    def __init__(self, url: str, timeout: float = SINK_DEFAULT_TIMEOUT, **kwargs):
        _RecordingSink.calls.append((url, timeout))

    async def send(self, findings) -> bool:
        return True


def _config(tmp_path, *, sink: str = "http", timeout: float | None = None):
    line = "" if timeout is None else f"upstream_timeout_s = {timeout}"
    path = tmp_path / "synapse.toml"
    path.write_text(CONFIG.format(sink=sink, timeout_line=line), encoding="utf-8")
    return load_config(path)


@pytest.fixture
def recording_sink(monkeypatch):
    _RecordingSink.calls = []
    monkeypatch.setattr(cli, "HttpSink", _RecordingSink)
    return _RecordingSink


# --- the value itself ------------------------------------------------------------


def test_the_shipped_config_gives_the_worker_120_seconds(monkeypatch) -> None:
    """Against the real committed config/synapse.toml. There is no
    `upstream_timeout_s` key in it, so this pins the dataclass default as the
    EFFECTIVE value — the number the demo actually runs with."""
    monkeypatch.delenv("SYNAPSE_UPSTREAM_TIMEOUT", raising=False)

    assert load_config().worker.upstream_timeout_s == SHIPPED_UPSTREAM_TIMEOUT


def test_the_sink_still_defaults_to_the_short_timeout_on_its_own() -> None:
    """THE assertion that makes the two wiring tests below load-bearing.

    The fix was not raising this default — deliberately, since a bare HttpSink
    is a generic HTTP poster with no opinion about synthesis latency. It is two
    keyword arguments at two call sites, and this is what says so: if this ever
    reads 120.0, the wiring tests stop distinguishing a correct wiring from a
    deleted one, and this file quietly stops proving anything.
    """
    assert HttpSink("http://127.0.0.1:10787/x").timeout == SINK_DEFAULT_TIMEOUT
    assert SHIPPED_UPSTREAM_TIMEOUT != SINK_DEFAULT_TIMEOUT


def test_the_toml_key_is_honoured_when_someone_does_set_it(tmp_path) -> None:
    assert _config(tmp_path, timeout=45.5).worker.upstream_timeout_s == 45.5


def test_the_env_override_wins_over_the_file(tmp_path, monkeypatch) -> None:
    """So a slow host can be worked around without editing a tracked file."""
    monkeypatch.setenv("SYNAPSE_UPSTREAM_TIMEOUT", "7.5")

    assert _config(tmp_path, timeout=45.5).worker.upstream_timeout_s == 7.5


# --- wiring 1: replay ------------------------------------------------------------


async def test_replay_builds_its_sink_with_the_configured_timeout(
    tmp_path, monkeypatch, recording_sink, capsys
) -> None:
    """`cmd_replay`'s HttpSink. Drop the `timeout=` keyword here and this test
    is the only thing that notices."""
    config = _config(tmp_path, timeout=99.0)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)

    await cli.cmd_replay(_ns(skipped=False))

    [(url, timeout)] = recording_sink.calls
    assert timeout == 99.0
    assert timeout != SINK_DEFAULT_TIMEOUT
    assert url == config.worker.upstream_url


async def test_replay_passes_the_shipped_120_not_the_sinks_own_30(
    tmp_path, monkeypatch, recording_sink, capsys
) -> None:
    """The default path, which is the one that actually runs — no TOML key, so
    the dataclass default has to survive all the way to the sink."""
    config = _config(tmp_path, timeout=None)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)

    await cli.cmd_replay(_ns(skipped=False))

    assert recording_sink.calls == [
        (config.worker.upstream_url, SHIPPED_UPSTREAM_TIMEOUT)
    ]


async def test_a_file_sink_never_constructs_an_http_sink_at_all(
    tmp_path, monkeypatch, recording_sink, capsys
) -> None:
    """The mirror: the timeout is an HTTP concern, and the file sink — the
    default for a developer with no orchestrator running — must not acquire
    one, or a config error would surface as a mysterious hang."""
    config = _config(tmp_path, sink="file")
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)

    await cli.cmd_replay(_ns(skipped=False))

    assert recording_sink.calls == []


# --- wiring 2: run ---------------------------------------------------------------


async def test_run_builds_its_sink_with_the_configured_timeout(
    tmp_path, monkeypatch, recording_sink, capsys
) -> None:
    """`cmd_run`'s HttpSink — the second call site, and the one that carries
    every live batch. Both wirings are asserted separately because they are
    two independent lines: fixing one and missing the other is precisely the
    failure mode a single test would hide."""
    config = _config(tmp_path, timeout=99.0)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: config)
    monkeypatch.setattr(cli, "check_canary", _async_canary)
    transcript = tmp_path / "sess.jsonl"
    transcript.write_text("", encoding="utf-8")

    await cli.cmd_run(
        _ns(transcript=str(transcript), interval=0.01, ticks=1, from_start=False)
    )

    assert recording_sink.calls == [(config.worker.upstream_url, 99.0)]


# --- and the timeout is real, not just stored ------------------------------------


async def test_the_timeout_is_actually_enforced_on_the_wire() -> None:
    """Stored is not honoured. `HttpSink.send` puts `self.timeout` into the
    httpx client, and a push that overruns it must return False — the WAL's
    "not delivered, retry later" answer — rather than raising and taking the
    worker down with it.

    Real sockets, no server: an unroutable address in TEST-NET-1 (RFC 5737)
    that will never answer. Bounded by the timeout itself, so it is fast, and
    the wall-clock assertion is what proves the number was used rather than
    ignored in favour of httpx's own default.

    The bound is 1.0s, and NOT the 5.0s it was, because `httpx.AsyncClient()`'s
    own default timeout is exactly 5.0. Measured, all three shapes: correct
    wiring returns in 0.076s; `self.timeout` dropped on the floor returns in
    5.007s; the constructor's 30.0 default returns in 30.009s. Against a 5.0s
    bound the dropped-timeout case -- the failure this test exists for -- missed
    by 7ms in 5000, which is jitter, not signal. 1.0s keeps a 13x margin over
    the real 0.076s and separates both broken shapes cleanly.
    """
    sink = HttpSink("http://192.0.2.1:10787/producer/findings", timeout=0.05)

    started = asyncio.get_running_loop().time()
    delivered = await sink.send([])
    elapsed = asyncio.get_running_loop().time() - started

    assert delivered is False, "a timeout is a retry, never an exception"
    assert elapsed < 1.0, (
        "a 0.05s timeout that takes seconds means self.timeout never reached "
        "the client — the same way the two wirings above could be deleted"
    )


# --- helpers ---------------------------------------------------------------------


async def _async_canary(provider):
    return PASSING_CANARY


class _Namespace:
    def __init__(self, **kwargs):
        self.verbose = False
        self.shared_id = "local-dev"
        self.contributor = "aditya"
        self.transcript = None
        self.interval = None
        self.ticks = 1
        self.from_start = False
        self.skipped = False
        self.debug_port = 0
        for key, value in kwargs.items():
            setattr(self, key, value)


def _ns(**kwargs) -> _Namespace:
    return _Namespace(**kwargs)
