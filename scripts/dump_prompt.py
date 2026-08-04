"""Print the exact message list sent to the NPU. Offline.

    uv run python scripts/dump_prompt.py [fixture] [pack]

Uses the live config for distil_kinds and render_style, so what this prints is
what the distiller would actually send. (It previously defaulted to all kinds
regardless of config, which made it show a prompt no run would ever produce.)
"""

from __future__ import annotations

import sys

from synapse_distiller import load_config, load_pack_by_name
from synapse_distiller.fixtures import load_segment
from synapse_distiller.prompt import build_messages

config = load_config()
fixture = sys.argv[1] if len(sys.argv) > 1 else "seg-001"
pack = load_pack_by_name(sys.argv[2] if len(sys.argv) > 2 else config.prompt_pack_name)

print(
    f"# fixture={fixture}  pack={pack.name}  kinds={list(config.distil_kinds)}  "
    f"style={config.render_style}  overhead~{pack.overhead_tokens} tokens"
)

messages = build_messages(load_segment(fixture), pack, config.distil_kinds, config.render_style)
for i, message in enumerate(messages):
    print(f"\n{'=' * 78}")
    print(f"[{i}] role={message['role']}")
    print("=" * 78)
    print(message["content"])
