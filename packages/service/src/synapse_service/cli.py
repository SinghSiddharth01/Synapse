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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synapse-service", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    args = parser.parse_args(argv)
    print(f"synapse-service on http://{args.host}:{args.port} "
          f"(synthesizer: {os.environ.get('SYNAPSE_SYNTHESIZER', 'fake')})")
    uvicorn.run(build_app(_provider()), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
