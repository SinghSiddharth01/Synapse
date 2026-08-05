# packages/service/src/synapse_service/cli.py
"""synapse-service — the remote half. FakeProvider by default so it boots anywhere;
flip SYNAPSE_SYNTHESIZER=aic100 for the real thing (needs INFERENCE_CLOUD_API_KEY)."""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from synapse_service.api import build_app


def _provider():
    mode = os.environ.get("SYNAPSE_SYNTHESIZER", "fake")
    if mode == "aic100":
        from synapse_providers import AIC100Provider   # Task 5
        return AIC100Provider()
    from synapse_providers import FakeProvider
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
