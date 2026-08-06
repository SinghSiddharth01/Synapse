# packages/service/src/synapse_service/cli.py
"""synapse-service — the remote half. FakeProvider by default so it boots anywhere;
flip SYNAPSE_SYNTHESIZER=aic100 for the real thing (needs INFERENCE_CLOUD_API_KEY)."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from synapse_service.api import build_app


MODES = ("fake", "aic100", "npu", "anthropic")

NPU_PROBE_TIMEOUT_S = 3.0


def _npu_answers(base_url: str) -> bool:
    """One zero-cost GET against the seam's metadata route.

    Same probe the supervisor uses (scripts/serve_local.py), for the same
    reason: /v1/models is the only GET every seam serves, it runs no
    inference, and a seam that cannot answer it cannot answer anything.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/models",
                                    timeout=NPU_PROBE_TIMEOUT_S) as response:
            response.read()
            return (getattr(response, "status", None) or response.getcode()) == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _provider():
    """The synthesizer, chosen at boot — and every way of choosing it WRONG
    made loud here rather than hours later, per finding (decision 008's
    sibling problem).

    Each arm below used to fail silently in its own way: a typo in the mode
    fell through to FakeProvider; `aic100` with no key booted and 401'd on
    every call; `npu` with nothing on :18181 booted and failed every call. All
    three produced a service that looked up and answered nothing, which is the
    exact failure class this workstream exists to close. `SYNAPSE_SYNTHESIZER`
    is read once, at startup, so these checks cost one probe at most.
    """
    mode = os.environ.get("SYNAPSE_SYNTHESIZER")
    if mode is not None and mode not in MODES:
        # A typo is not a default. `SYNAPSE_SYNTHESIZER=aic-100` silently ran
        # the FakeProvider, so an operator who believed they were on the cloud
        # 70B got an empty-handed service that booted cleanly and said
        # "synthesizer: aic-100" on its own startup line.
        raise SystemExit(
            f"SYNAPSE_SYNTHESIZER={mode!r} is not a synthesizer this service "
            f"knows. Valid values: {', '.join(MODES)}.\n"
            f"Leave it unset to boot on the FakeProvider (tests, and any "
            f"machine with no model at all).")
    if mode == "aic100":
        from synapse_providers import AIC100Provider   # Task 5
        # Checked HERE and not left to the first call: without a key the
        # provider builds fine, boots fine, and 401s on every synthesis and
        # every retrieval ranking -- so the failure surfaces as a service that
        # is up and knows nothing, hours after whoever started it walked away.
        if not (os.environ.get("INFERENCE_CLOUD_API_KEYS")
                or os.environ.get("INFERENCE_CLOUD_API_KEY")):
            raise SystemExit(
                "SYNAPSE_SYNTHESIZER=aic100 but no key resolved. Set "
                "INFERENCE_CLOUD_API_KEY, or INFERENCE_CLOUD_API_KEYS for a "
                "comma-separated rotation pool.\n"
                "scripts/serve_local.py reads the `inference_cloud` block of "
                "secrets.jsonc and passes the key through for you; a "
                "hand-started service has to be given one.")
        return AIC100Provider()
    if mode == "npu":
        # Synthesis against a local `geniex serve`. AIC100Provider CANNOT be
        # pointed at one: it deliberately uses POST /completions (Cirrascale's
        # /chat/completions eats emitted JSON into empty tool_calls -- see
        # aic100.py), and GenieX serves only /chat/completions, /v1/models and
        # /v1/models/{model}. So aic100-against-GenieX is a 410 Gone on every
        # synthesis call. Until decision 008 that also came back as an empty
        # result with a 200 -- a host whose queries returned nothing while its
        # own dashboard showed the findings. It is a 503 naming the provider
        # now, but the right arm is still the point: a loud failure is a
        # better bug report, not a substitute for being wired correctly.
        from synapse_providers import NPUProvider
        kwargs = {}
        if base_url := os.environ.get("SYNAPSE_BASE_URL"):
            kwargs["base_url"] = base_url
        if model := os.environ.get("SYNAPSE_MODEL"):
            kwargs["model"] = model
        provider = NPUProvider(**kwargs)
        # One probe, at boot. scripts/serve_local.py verifies the seam before
        # it ever spawns this process, so this catches the OTHER path: a
        # service someone started by hand, pointed at a GenieX that is not
        # there or has already gone idle-dead. Without it that service comes
        # up, reports "synthesizer: npu", and answers every query with a 503
        # for as long as it runs.
        if not _npu_answers(provider.base_url):
            raise SystemExit(
                f"SYNAPSE_SYNTHESIZER=npu but nothing answers at "
                f"{provider.base_url.rstrip('/')}/models "
                f"(waited {NPU_PROBE_TIMEOUT_S:g}s).\n"
                f"Start `geniex serve`, or run "
                f"`uv run python scripts/serve_local.py --npu`, which now "
                f"launches and supervises it for you.")
        return provider
    if mode == "anthropic":
        from synapse_providers import AnthropicProvider
        return AnthropicProvider()
    from synapse_providers import FakeProvider
    # Warn and BOOT, deliberately not exit. A service that needs a model just
    # to start cannot be brought up on a bare laptop, and
    # `test_unset_still_boots_on_the_fake` pins that property because it is
    # correct. What was wrong was the SILENCE: nothing said out loud that the
    # brain was a stub. Decision 008 closed the runtime half (every model call
    # is now a 503 that names FakeProvider); this closes the boot half.
    headline = ("SYNAPSE_SYNTHESIZER is not set"
                if mode is None else "SYNAPSE_SYNTHESIZER=fake")
    print(
        f"\n"
        f"  ***********************************************************\n"
        f"  * {headline}\n"
        f"  * Running on FakeProvider(scripts=[]). Every model-backed\n"
        f"  * call will fail LOUDLY: query -> 503 retrieval_unavailable.\n"
        f"  * Fine for tests; WRONG for a demo -- set\n"
        f"  * SYNAPSE_SYNTHESIZER=aic100|npu|anthropic.\n"
        f"  ***********************************************************\n",
        file=sys.stderr, flush=True)
    return FakeProvider(scripts=[])   # boots; any model call fails loudly, honestly


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _resolve_debug(args: argparse.Namespace) -> bool:
    """--debug/--no-debug wins; else SYNAPSE_SERVICE_DEBUG (0/false disables);
    else on by default. Mirrors the worker's `--debug-port 0` off switch
    (`cli.py`'s `_resolve_debug_port`) -- the service has no separate port to
    turn off, so this gates whether `/debug` is mounted on the API listener
    at all (`build_app`'s `debug=` parameter)."""
    if getattr(args, "debug", None) is not None:
        return args.debug
    env = os.environ.get("SYNAPSE_SERVICE_DEBUG")
    if env is not None:
        return env.strip().lower() not in ("0", "false", "")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synapse-service", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument(
        "--no-debug", dest="debug", action="store_false", default=None,
        help="disable the /debug dashboard (on by default; overridable via "
             "SYNAPSE_SERVICE_DEBUG=0) -- /debug shares this listener and "
             "carries no auth, so this is required before binding --host to "
             "anything other than localhost",
    )
    args = parser.parse_args(argv)
    debug = _resolve_debug(args)
    print(f"synapse-service on http://{args.host}:{args.port} "
          f"(synthesizer: {os.environ.get('SYNAPSE_SYNTHESIZER', 'fake')}, "
          f"debug: {'on' if debug else 'off'})")
    if debug and args.host not in _LOCAL_HOSTS:
        print(
            f"WARNING: /debug is enabled while --host is {args.host!r} -- "
            "the dashboard has no auth and is reachable by anyone who can "
            "reach the API. Pass --no-debug or set SYNAPSE_SERVICE_DEBUG=0.",
            file=sys.stderr,
        )
    uvicorn.run(build_app(_provider(), debug=debug), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
