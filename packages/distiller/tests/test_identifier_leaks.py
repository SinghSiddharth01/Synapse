# packages/distiller/tests/test_identifier_leaks.py
"""The red-team test the old metric failed: single-token leaks."""
from datetime import datetime, timezone

from synapse_contracts import AgentEvent, Attribution, Finding, FindingType, Segment
from synapse_distiller.evaluation import (
    DEFAULT_ALLOWLIST,
    identifier_leaks,
    score_fixture,
    verbatim_overlap,
)

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


def test_paraphrased_key_value_leak_is_still_caught():
    """The byte-identical case above is not the realistic one: a compression
    pack pulls wording toward the source without necessarily preserving a
    literal `key=value` fragment. The key=value alternative in _IDENTIFIER_RE
    used to fuse the whole assignment into one token ('default_pool_size=25'),
    so a paraphrase that drops the '=25' (or that keeps only the value) never
    matched the segment's fused token and the leak went undetected — exactly
    the blind spot this detector was commissioned to close."""
    seg = _segment("set default_pool_size=25 in the pgbouncer config")
    assert identifier_leaks("Raised default_pool_size to handle load.", seg) == ["default_pool_size"]
    assert identifier_leaks("Raised the default_pool_size setting.", seg) == ["default_pool_size"]

    seg_upper = _segment("set default_pool_size=25 and MAX_RETRIES=3 in the pgbouncer config")
    assert identifier_leaks("MAX_RETRIES was bumped from 3.", seg_upper) == ["MAX_RETRIES"]


def test_bare_key_in_segment_leaks_when_finding_fuses_it_with_its_value():
    """Symmetric blind spot: the segment states the key bare, and the finding
    is the one that writes it as key=value. Both sides must independently
    surface the bare key for the intersection to catch it."""
    seg = _segment("the default_pool_size setting controls connection reuse")
    assert identifier_leaks("Raised default_pool_size=25 to handle load.", seg) == ["default_pool_size"]


def test_shapes_snake_camel_dotted_path_and_fileext():
    seg = _segment("in auth_helper we call TokenValidator via api.internal.example "
                   "reading config/settings.py and /etc/synapse/keys")
    finding = ("auth_helper uses TokenValidator against api.internal.example, "
               "configured in config/settings.py under /etc/synapse/keys")
    leaks = identifier_leaks(finding, seg)
    for expected in ("auth_helper", "TokenValidator", "api.internal.example",
                     "config/settings.py", "/etc/synapse/keys"):
        assert expected in leaks


def test_plain_words_are_never_extracted_as_identifiers():
    """pgbouncer/asyncpg/ruff/pytest/redis/uv/json/jsonl/codex are plain
    lowercase words with no internal structure (no separator, no case-hump),
    so `_identifier_tokens` never extracts them as identifier-shaped in the
    first place — they do not need an allowlist entry to stay unflagged.
    They used to sit in DEFAULT_ALLOWLIST anyway; that was dead weight the
    allowlist filter never actually exercised (see
    test_allowlist_actually_suppresses_a_reachable_entry for a token the
    filter genuinely has to work to suppress), so they were pruned."""
    seg = _segment("switched from pgbouncer to asyncpg after running ruff under pytest and uv")
    finding = "Switched from pgbouncer to asyncpg after a ruff pass under pytest and uv."
    assert identifier_leaks(finding, seg) == []
    for word in ("pgbouncer", "asyncpg", "ruff", "pytest", "redis", "uv",
                 "jsonl", "json", "codex"):
        assert word not in DEFAULT_ALLOWLIST, (
            f"{word!r} is a plain, separator-free word; the tokenizer can "
            "never emit it as an identifier, so it must not sit in the "
            "allowlist as if it needed one"
        )


def test_allowlist_actually_suppresses_a_reachable_entry():
    """`pgbouncer`/`asyncpg`/`ruff` above are plain lowercase words with no
    separator, so `_identifier_tokens` never extracts them as identifier-
    shaped in the first place — that test passes whether or not the allowlist
    filter runs at all, and stays green even if the filter is deleted
    entirely. `claude-code` IS identifier-shaped (kebab-case) and is also in
    DEFAULT_ALLOWLIST, so comparing it with and without the allowlist proves
    the filter itself does the suppressing, on a token that actually reaches
    it."""
    seg = _segment("this session ran under claude-code the whole time")
    finding = "Observed under claude-code the whole time."
    assert "claude-code" in DEFAULT_ALLOWLIST
    assert identifier_leaks(finding, seg, allowlist=frozenset()) == ["claude-code"]
    assert identifier_leaks(finding, seg) == []


def test_identifier_only_in_finding_is_not_a_leak():
    # Invented by the model, not copied from the session — a fidelity problem,
    # not a privacy one. This metric must not conflate the two.
    seg = _segment("the request handler is slow")
    assert identifier_leaks("slowness in fast_path_v2 handler", seg) == []


def test_plain_prose_never_flags():
    seg = _segment("the connection pool was exhausted under load")
    assert identifier_leaks("The connection pool was exhausted under load.", seg) == []


def test_score_fixture_wires_leaked_identifiers():
    """The actual Task 5 deliverable is `FixtureScore.leaked_identifiers`
    being populated by `score_fixture()` — `identifier_leaks()` working in
    isolation (every test above) says nothing about that wiring. Before this
    test, deleting the `leaked_identifiers=leaks` line in `score_fixture`
    left the whole suite green and the field silently defaulted to `[]`,
    which for a privacy metric is a permanently clean-looking report."""
    seg = _segment("set default_pool_size=25 in the pgbouncer config")
    finding = Finding(
        id="f-test-01",
        type=FindingType.LEARNING,
        text="Raised default_pool_size=25 to handle the connection load.",
        attributions=[
            Attribution(contributor="aditya", agent_session="as-leak-001", agent="claude-code")
        ],
        ts=TS,
    )
    score = score_fixture(
        "leak-001",
        seg,
        [finding],
        goldens=[],
        attempts=1,
        latency_ms=100,
        input_tokens=10,
        output_tokens=10,
    )
    assert score.leaked_identifiers == ["default_pool_size", "default_pool_size=25"]


def test_identifier_at_end_of_sentence_is_still_caught():
    """Findings are one or two sentences, so an identifier is very often the
    last token before the full stop. The trailing lookahead used to forbid a
    following '.', which made a leak invisible exactly where it is most
    likely to occur."""
    assert identifier_leaks(
        "We raised default_pool_size.",
        _segment("set default_pool_size to fix it"),
    ) == ["default_pool_size"]
    assert identifier_leaks(
        "Uses TokenValidator.",
        _segment("the class TokenValidator handles it"),
    ) == ["TokenValidator"]


def test_path_leak_at_end_of_sentence_is_still_caught():
    """Mirror-image bug: a path-shaped alternative greedily consumes a
    trailing sentence period as a path character, so the segment-side token
    ('config/settings.py') and the finding-side token ('config/settings.py.')
    used to differ by exactly that period and never intersect."""
    seg = _segment("we edited config/settings.py in the repo")
    assert identifier_leaks("Edited config/settings.py.", seg) == ["config/settings.py"]


def test_ip_address_copy_is_caught():
    seg = _segment("the db at 10.4.221.17 refused the connection")
    finding = "Postgres at 10.4.221.17 refused connections."
    assert identifier_leaks(finding, seg) == ["10.4.221.17"]


def test_case_swapped_kebab_copy_is_still_caught():
    """A trivial evasion: change the case of a copied identifier. Structural
    separators (hyphens here) survive a case change even when the letters
    don't, so comparing case-insensitively closes this for kebab/snake-case
    identifiers. (A bare camelCase identifier flattened to one unstructured
    lowercase run, e.g. TokenValidator -> tokenvalidator, has no separator
    left to key on and stays a documented gap — see the module docstring.)"""
    seg = _segment("service Auth-Gateway-V2 timed out during the deploy")
    finding = "The auth-gateway-v2 service timed out."
    assert identifier_leaks(finding, seg) == ["auth-gateway-v2"]


def test_long_hex_string_copy_is_caught():
    """A commit sha, hash, or key fragment has no separator at all, so none
    of the snake/kebab/dotted/camelCase alternatives can see it — only a raw
    run of hex digits long enough (16+) to be structure rather than an
    ordinary short number."""
    seg = _segment("bisected the regression to commit a1b2c3d4e5f6a789 in the log")
    finding = "Bisected the regression to a1b2c3d4e5f6a789."
    assert identifier_leaks(finding, seg) == ["a1b2c3d4e5f6a789"]


def test_short_hex_like_number_is_not_flagged():
    """Below the 16-char floor this is indistinguishable from an ordinary
    short number or id fragment; the detector deliberately does not guess."""
    seg = _segment("worker 1042 failed with code deadbeef")
    assert identifier_leaks("Worker 1042 failed with code deadbeef.", seg) == []
