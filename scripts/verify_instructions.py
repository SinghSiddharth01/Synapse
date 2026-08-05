"""Does a real MCP client see the server's `instructions`? One question, live.

    uv run synapse-orchestrator          # terminal 1
    uv run python scripts/verify_instructions.py   # terminal 2 -> PROVEN / DISPROVEN
"""
from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from synapse_orchestrator.server import SENTINEL

URL = "http://127.0.0.1:8787/mcp"


async def main() -> int:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            got = init.instructions or ""
            if SENTINEL in got:
                print(f"PROVEN: client received instructions ({len(got)} chars).")
                return 0
            print("DISPROVEN: instructions missing or empty over the wire.")
            print(f"  received: {got!r}")
            print("  -> amendment F Q11's floor-tier briefing does NOT work; "
                  "fall back to a per-agent pack and update the working notes.")
            return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
