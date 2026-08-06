"""`SYNAPSE_SYNTHESIS_KEYS` must not claim capacity the key pool does not have.

ADR 0005 records multi-key round-robin as "dead code at both layers" and W3b
was asked to wire it or delete it. Grepped before touching anything, it is
neither dead nor unwired:

  * `AIC100Provider._post_rotating` is the ONLY POST helper `complete()` calls
    — both the `/chat/completions` branch and the `/completions` schema branch
    go through it — and `test_aic100.py::test_key_pool_rotates_on_429` covers
    the rotation itself.
  * `SYNTHESIS_KEYS` is read by `_affordable()` on every push, and
    `test_merge_debounce.py::test_more_keys_buy_more_rounds` covers the
    scaling.

Both are live reads. Deleting either would delete a POST path and a passing
test to fix a sentence in an ADR. What IS true, and is worse than dead code
because it is live code that lies, is that the two numbers are never compared:
`SYNAPSE_SYNTHESIS_KEYS=10` multiplies the governor's ceiling by ten whether or
not ten keys exist, and the excess does not defer — it 429s inside the
provider, where a one-key pool cannot rotate out of it. "Findings landed,
memory unchanged", by the third road.

These tests pin the comparison, which is what a limiter's neighbour owes the
next reader about headroom.
"""

import logging

from synapse_providers import AIC100Provider, FakeProvider

from synapse_service.api import build_app


def _boot(monkeypatch, caplog, *, keys: int, pool: str | None):
    monkeypatch.setattr("synapse_service.api.SYNTHESIS_KEYS", keys)
    if pool is None:
        monkeypatch.delenv("INFERENCE_CLOUD_API_KEYS", raising=False)
        monkeypatch.setenv("INFERENCE_CLOUD_API_KEY", "one-key")
    else:
        monkeypatch.setenv("INFERENCE_CLOUD_API_KEYS", pool)
    provider = AIC100Provider()
    with caplog.at_level(logging.INFO, logger="synapse_service.api"):
        build_app(provider, debug=False)
    return caplog.records


def test_claiming_more_keys_than_the_pool_holds_is_loud(monkeypatch, caplog):
    """The footgun, named at boot with the arithmetic in it. Without this the
    only evidence is a 429 inside a provider whose sole mitigation — rotation —
    a one-key pool cannot perform."""
    records = _boot(monkeypatch, caplog, keys=10, pool=None)

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert warnings, "the governor claimed 10 keys against 1 and said nothing"
    message = warnings[0].getMessage()
    assert "SYNAPSE_SYNTHESIS_KEYS=10" in message
    assert "holds 1 key(s)" in message
    assert "250000 tokens" in message      # 25,000 x 10, the claim spelled out
    assert "200 requests" in message


def test_a_matching_pool_is_silent(monkeypatch, caplog):
    """A check that fires when nothing is wrong trains people to ignore it."""
    records = _boot(monkeypatch, caplog, keys=3, pool="a,b,c")
    assert [r for r in records if r.levelno >= logging.WARNING] == []


def test_unclaimed_keys_are_reported_the_other_way(monkeypatch, caplog):
    """The harmless direction still costs something real: merges deferred that
    the pool could have paid for. INFO, not WARNING — nothing is broken."""
    records = _boot(monkeypatch, caplog, keys=1, pool="a,b,c,d")
    infos = [r for r in records
             if r.levelno == logging.INFO and "SYNAPSE_SYNTHESIS_KEYS" in r.getMessage()]
    assert infos
    assert "Raise it to 4" in infos[0].getMessage()


def test_a_provider_with_no_readable_pool_says_nothing(monkeypatch, caplog):
    """FakeProvider, NPUProvider and anything else without an `api_keys` list.
    The only claim worth making here is a checked one; guessing that a provider
    the service cannot introspect has one key would fire this warning through
    the whole test suite and mean nothing."""
    monkeypatch.setattr("synapse_service.api.SYNTHESIS_KEYS", 10)
    with caplog.at_level(logging.INFO, logger="synapse_service.api"):
        build_app(FakeProvider(scripts=[]), debug=False)
    assert caplog.records == []


def test_rotation_is_still_the_only_post_path_in_the_provider():
    """The grep this decision rests on, kept executable. If `complete()` ever
    stops routing through `_post_rotating`, "round-robin is dead code" becomes
    true and the decision in decisions/002 has to be re-made rather than
    silently inherited."""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(AIC100Provider.complete))
    tree = ast.parse(source)
    posts = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr in {"post", "_post_rotating"}]
    assert posts, "AIC100Provider.complete makes no POST at all"
    assert all(p.func.attr == "_post_rotating" for p in posts), (
        "a POST in AIC100Provider.complete bypasses the key pool")
