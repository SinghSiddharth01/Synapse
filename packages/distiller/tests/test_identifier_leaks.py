# packages/distiller/tests/test_identifier_leaks.py
"""The red-team test the old metric failed: single-token leaks."""
from datetime import datetime, timezone

from synapse_contracts import AgentEvent, Segment
from synapse_distiller.evaluation import DEFAULT_ALLOWLIST, identifier_leaks, verbatim_overlap

TS = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _segment(content: str) -> Segment:
    event = AgentEvent(role="assistant", kind="text", content=content,
                       ts=TS, agent_session_id="as-leak-001")
    return Segment(id="leak-001", agent_session_id="as-leak-001",
                   events=[event], started_at=TS, ended_at=TS)


def test_the_documented_failure_case_is_now_caught():
    seg = _segment("set default_pool_size=25 in the pgbouncer config")
    finding = "Raised default_pool_size=25 to handle the connection load."
    assert verbatim_overlap(finding, seg) == 0.0          # the old metric stays blind
    assert "default_pool_size=25" in identifier_leaks(finding, seg)  # the new one is not


def test_shapes_snake_camel_dotted_path_and_fileext():
    seg = _segment("in auth_helper we call TokenValidator via api.internal.example "
                   "reading config/settings.py and /etc/synapse/keys")
    finding = ("auth_helper uses TokenValidator against api.internal.example, "
               "configured in config/settings.py under /etc/synapse/keys")
    leaks = identifier_leaks(finding, seg)
    for expected in ("auth_helper", "TokenValidator", "api.internal.example",
                     "config/settings.py", "/etc/synapse/keys"):
        assert expected in leaks


def test_public_vocabulary_is_not_a_leak():
    seg = _segment("switched from pgbouncer to asyncpg after running ruff")
    finding = "Switched from pgbouncer to asyncpg after a ruff pass."
    assert identifier_leaks(finding, seg) == []
    assert {"pgbouncer", "asyncpg", "ruff"} <= DEFAULT_ALLOWLIST


def test_identifier_only_in_finding_is_not_a_leak():
    # Invented by the model, not copied from the session — a fidelity problem,
    # not a privacy one. This metric must not conflate the two.
    seg = _segment("the request handler is slow")
    assert identifier_leaks("slowness in fast_path_v2 handler", seg) == []


def test_plain_prose_never_flags():
    seg = _segment("the connection pool was exhausted under load")
    assert identifier_leaks("The connection pool was exhausted under load.", seg) == []
