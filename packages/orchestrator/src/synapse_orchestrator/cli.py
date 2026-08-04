"""synapse-orchestrator — the MCP server a coding agent will eventually connect to.

    uv run synapse-orchestrator

Currently a transport shell only — see server.py's module docstring for what
it used to do and why that was removed. Boots and serves streamable-http per
ADR 0001, but exposes no tools or prompts until Plan D Task D.3's `query` and
`contribute` exist, which in turn need the Synapse Service (Plan C).

Session binding is `synapse-worker join <shared_id>`, not anything through
this server. See that command's help.
"""

from __future__ import annotations

import argparse
import logging
import sys

from synapse_orchestrator.server import mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="synapse-orchestrator", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    # argv=None reads sys.argv, matching the old parser.parse_args() call
    # exactly — real invocation is unchanged. Tests pass an explicit list.
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    print(f"synapse-orchestrator listening on http://{args.host}:{args.port}/mcp")
    print("No tools or prompts exposed yet — see server.py. Use "
          "`synapse-worker join <shared_id>` to bind a session.")
    mcp.run(transport="streamable-http")
    return 0


if __name__ == "__main__":
    sys.exit(main())
