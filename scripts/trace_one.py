"""Full trace of one fixture: input events -> exact prompt -> raw NPU output -> findings.

    geniex serve
    uv run python scripts/trace_one.py [fixture]

Calls the provider WITHOUT a response schema so the raw string the model emitted
is visible verbatim, then parses it with the same tolerant parser the distiller
uses. Temperature is 0, so this is the same output the distiller would get.
"""

from __future__ import annotations

import asyncio
import sys

from synapse_providers import NPUProvider
from synapse_providers.openai_compat import _parse_json_tolerantly

from synapse_distiller import load_config, load_pack_by_name
from synapse_distiller.fixtures import load_goldens, load_segment
from synapse_distiller.prompt import build_messages

# Same Windows/cp1252 reason as scripts/run_npu_eval.py: RULE below is U+2550,
# which the locale codepage cannot encode, so any redirected run of this script
# died with UnicodeEncodeError before printing anything.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

RULE = "═" * 78


def banner(title: str) -> None:
    print(f"\n{RULE}\n  {title}\n{RULE}")


async def main() -> int:
    fixture = sys.argv[1] if len(sys.argv) > 1 else "seg-001"
    config = load_config()
    pack = load_pack_by_name(config.prompt_pack_name)
    segment = load_segment(fixture)
    goldens = load_goldens(fixture)
    kept = set(config.distil_kinds)

    banner(f"1. INPUT AGENT EVENTS — {fixture} ({len(segment.events)} events)")
    print(f"  distil_kinds = {list(config.distil_kinds)}   render_style = {config.render_style}\n")
    for i, event in enumerate(segment.events):
        mark = "SENT " if event.kind in kept else "  -  "
        body = event.content.replace("\n", " ")[:88]
        tool = f" [{event.tool_name}]" if event.tool_name else ""
        print(f"  {mark} [{i:>2}] {event.role:<9} {event.kind:<12}{tool}")
        print(f"          {body}")
    sent = sum(1 for e in segment.events if e.kind in kept)
    print(f"\n  -> {sent} of {len(segment.events)} events reach the model")

    messages = build_messages(segment, pack, config.distil_kinds, config.render_style)

    banner(f"2. EXACT PROMPT SENT TO THE NPU ({len(messages)} messages)")
    for i, message in enumerate(messages):
        print(f"\n  ┌─ [{i}] role={message['role']} " + "─" * 40)
        for line in message["content"].splitlines():
            print(f"  │ {line}")
        print("  └" + "─" * 60)

    banner("3. RAW OUTPUT FROM THE NPU (verbatim, unparsed)")
    provider = NPUProvider(
        base_url=config.provider.base_url,
        model=config.model,
        max_tokens=config.provider.max_tokens,
        temperature=config.provider.temperature,
        timeout=config.provider.timeout_s,
    )
    result = await provider.complete(messages=messages)
    raw = str(result.data)
    print(f"\n  prompt_tokens={result.usage.input_tokens}  "
          f"completion_tokens={result.usage.output_tokens}  "
          f"latency={result.latency_ms/1000:.1f}s\n")
    for line in raw.splitlines():
        print(f"  │ {line}")

    banner("4. PARSED FINDINGS vs GOLDENS")
    parsed = _parse_json_tolerantly(raw)
    produced = parsed.get("findings", []) if isinstance(parsed, dict) else []
    print(f"\n  parsed cleanly: {parsed is not None}")
    print(f"  produced {len(produced)}   golden {len(goldens)}\n")

    print("  PRODUCED:")
    for f in produced:
        print(f"    [{f.get('type')}] {f.get('text')}")
    print("\n  GOLDEN:")
    for g in goldens:
        print(f"    [{g.type.value}] {g.text}")
    if not goldens:
        print("    (empty — this fixture is all noise; the correct answer is no findings)")

    produced_types = {str(f.get("type")) for f in produced}
    golden_types = {g.type.value for g in goldens}
    print(f"\n  types produced {sorted(produced_types) or '—'}")
    print(f"  types golden   {sorted(golden_types) or '—'}")
    print(f"  MISSED         {sorted(golden_types - produced_types) or '—'}")
    print(f"  EXTRA          {sorted(produced_types - golden_types) or '—'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
