"""W7 — drive the WHOLE Shared Session lifecycle through a REAL MCP client.

WHY THIS EXISTS, AND WHY IT IS NOT A TEST
-----------------------------------------
Every lifecycle tool has unit cover in `packages/orchestrator/tests/`, driven
through an in-process `httpx` transport against a fake service. That proves the
tools' logic. It cannot prove the thing a demo actually stands on: that a real
MCP client, speaking streamable-http to a real `synapse-orchestrator` on
:8787, against a real `synapse-service` on :8899, with a real model seam on
:18181, can walk

    create_session -> join_session (2nd conversation) -> contribute -> query
    -> leave_session -> REJOIN under a new agent session id -> end_session

and get truthful answers at every step. That is a live-stack proof, so it is a
script — the same reasoning as `scripts/w6_live_stress.py` and
`scripts/verify_orchestrator.py`: a test that needs three listening ports and a
subscription-billed `claude -p` call either never runs in CI or spends money on
every `uv run pytest -q`.

WHAT IT ASSUMES IS ALREADY RUNNING
----------------------------------
`scripts/serve_local.py` (service + model seam + orchestrator). This script
starts nothing and stops nothing: the ports are the operator's to own, and a
driver that could kill a stack it did not start is a driver that eventually
kills the wrong one.

THE TRANSCRIPTS, AND WHY THEY ARE REAL FILES
--------------------------------------------
`agent_session_id` is not a label the orchestrator takes on trust — `_bind`
resolves it through `synapse_worker.discovery.find_transcript_by_session_id`,
which searches every project slug under `~/.claude/projects` for a transcript
whose id matches EXACTLY, and refuses (rather than falling back to mtime) when
nothing matches. So three conversations means three transcript files. They are
written under a throwaway project slug whose "cwd" is a temp directory created
for this run only — it cannot collide with a real project — and removed on the
way out, success or failure. Same pattern, and the same real
`~/.claude/projects`, as `scripts/verify_orchestrator.py`.

WHAT "A SECOND CONTRIBUTOR" CAN AND CANNOT MEAN HERE
----------------------------------------------------
One orchestrator stamps ONE contributor: `server._identity()` reads
`binding.contributor`, and `serve_local.py` prints "one orchestrator per
laptop, or their findings get stamped as yours" for exactly this reason. A
genuinely second *contributor* is therefore a second orchestrator on a second
port. What W2 landed, and what this drives, is a second *participant*: a second
Agent Session, with its own `agent_session_id`, its own binding file, its own
suppression key. The evidence file says which of the two it is rather than
letting the word "contributor" do the eliding.

RUN
    uv run python scripts/_w7_drive.py --contributor sid --json-out /tmp/w7.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from synapse_worker.discovery import CLAUDE_PROJECTS, project_slug

# Same Windows/cp1252 reason as scripts/verify_orchestrator.py:38-44 — this
# script's output carries box-drawing marks and the briefing's curly quotes.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ORCHESTRATOR_URL = "http://127.0.0.1:8787/mcp"
SERVICE_URL = "http://127.0.0.1:8899"

# The briefing is recomposed on a timer (briefing.DEFAULT_REFRESH_SECONDS = 10)
# and read fresh by `create_initialization_options()` on every NEW connection.
# So "what does an arriving agent see after the rejoin?" is only answerable by
# waiting out one refresh and then connecting again — which is exactly what a
# real Claude Code window does when it starts.
BRIEFING_REFRESH_WAIT = 12.0

# The prose contributed at step 3. Written as a teammate would write it: a
# concrete cause, the observable it explains, and the disposition. A distiller
# given decorative filler returns nothing durable and the step would prove only
# that the guard works.
FINDING_PROSE = (
    "The Anthropic distiller arm is unusable on this machine and the failure is "
    "silent: the `anthropic.api_key` field in secrets.jsonc is present but holds "
    "an EMPTY STRING, so `serve_local.py --distiller anthropic` gets past its own "
    "key check only if the environment supplies one, and otherwise dies with "
    "'Could not resolve authentication method' from deep inside the SDK rather "
    "than from the flag the operator actually chose. Use "
    "`--distiller claude-cli --claude-model haiku` instead: it shells out to the "
    "local `claude` binary on the subscription and needs no credential at all. "
    "Checked 2026-08-06 while driving the live lifecycle arc."
)

QUESTION = "Which distiller arm should I use on this machine, and why not anthropic?"


class Recorder:
    """Every step's request, response, status and wall time, in order."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.t0 = time.perf_counter()

    def record(self, name: str, request: Any, response: Any, *,
               elapsed_s: float, status: str = "ok", note: str = "") -> dict[str, Any]:
        step = {
            "step": len(self.steps) + 1,
            "name": name,
            "at_s": round(time.perf_counter() - self.t0, 3),
            "elapsed_s": round(elapsed_s, 3),
            "status": status,
            "request": request,
            "response": response,
            "note": note,
        }
        self.steps.append(step)
        print(f"\n── {step['step']}. {name}  (+{step['at_s']}s, took {step['elapsed_s']}s) "
              + "─" * 8, flush=True)
        print(f"  request:  {json.dumps(request, ensure_ascii=False)}", flush=True)
        body = response if isinstance(response, str) else json.dumps(response,
                                                                     ensure_ascii=False)
        for line in str(body).splitlines() or [""]:
            print(f"  response: {line}", flush=True)
        if note:
            print(f"  note:     {note}", flush=True)
        return step


def http_get(url: str, timeout: float = 10.0) -> Any:
    """A plain GET against the SERVICE, for corroboration only.

    The arc itself is driven entirely through MCP. These reads exist so the
    evidence file can quote the watermark the briefing was composed FROM, which
    is otherwise only visible as already-rendered prose.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read() or b"null")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError,
            ValueError) as exc:
        return {"_unreachable": f"{exc.__class__.__name__}: {exc}"}


def text_of(result) -> str:
    """The tool result as the agent would see it.

    `isError` is surfaced rather than swallowed: a tool that raised and a tool
    that returned prose saying it refused are different outcomes, and this
    script's whole job is to report which one happened.
    """
    parts = [getattr(block, "text", "") for block in (result.content or [])]
    body = "\n".join(p for p in parts if p)
    return f"[isError] {body}" if getattr(result, "isError", False) else body


async def call(session: ClientSession, rec: Recorder, tool: str,
               args: dict[str, Any], note: str = "") -> str:
    started = time.perf_counter()
    try:
        result = await session.call_tool(tool, args)
        body = text_of(result)
        status = "error" if getattr(result, "isError", False) else "ok"
    except Exception as exc:                                   # noqa: BLE001
        body = f"{exc.__class__.__name__}: {exc}"
        status = "exception"
    rec.record(f"{tool}()", {"tool": tool, "arguments": args}, body,
               elapsed_s=time.perf_counter() - started, status=status, note=note)
    return body


async def connect_and_read_briefing(rec: Recorder, label: str, note: str = "") -> str:
    """Open a FRESH MCP connection and record what `initialize` handed back.

    The arrival briefing is not a tool result — it rides the `instructions`
    field of the initialize response (server.py's module docstring), which is
    why capturing it needs a new connection rather than another tool call.
    """
    started = time.perf_counter()
    async with streamablehttp_client(ORCHESTRATOR_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            instructions = init.instructions or ""
    rec.record(f"initialize() — {label}",
               {"transport": "streamable-http", "url": ORCHESTRATOR_URL},
               {"serverInfo.name": init.serverInfo.name,
                "tools": sorted(t.name for t in tools.tools),
                "instructions": instructions},
               elapsed_s=time.perf_counter() - started, note=note)
    return instructions


def write_transcripts(slug_dir: Path, ids: list[str]) -> list[Path]:
    """One .jsonl per conversation, in the shape the finder reads.

    `find_claude_code_transcripts` takes the session id from the FILENAME stem
    and never opens the file, so the single line inside is for a human reading
    the evidence, not for the resolver.
    """
    slug_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for session_id in ids:
        path = slug_dir / f"{session_id}.jsonl"
        path.write_text(
            json.dumps({"type": "user", "message": {"content":
                        f"w7 live lifecycle drive — conversation {session_id}"}}) + "\n",
            encoding="utf-8")
        written.append(path)
    return written


async def drive(rec: Recorder, contributor: str, ids: dict[str, str]) -> dict[str, Any]:
    facts: dict[str, Any] = {"contributor": contributor, "agent_session_ids": ids}

    # ── the connection an agent would arrive on, BEFORE any session exists ──
    facts["briefing_before"] = await connect_and_read_briefing(
        rec, "first arrival (before create_session)",
        note="Whatever binding serve_local.py wrote at boot is what this reflects.")

    async with streamablehttp_client(ORCHESTRATOR_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1 ─ create, as conversation A
            purpose = "w7 overnight live evidence — full lifecycle arc on real ports"
            created = await call(session, rec, "create_session",
                                 {"purpose": purpose, "agent_session_id": ids["A"]},
                                 note="conversation A creates the Shared Session")
            facts["create_result"] = created
            shared_id = next((tok.strip(" ,.") for tok in created.split()
                              if tok.startswith("sh-")), None)
            facts["shared_id"] = shared_id
            if shared_id is None:
                facts["fatal"] = "create_session returned no sh-… id; the arc stops here."
                return facts

            # 2 ─ join, as conversation B: a SECOND participant on one machine
            facts["join_result"] = await call(
                session, rec, "join_session",
                {"shared_id": shared_id, "agent_session_id": ids["B"]},
                note="conversation B — its own agent_session_id, its own binding file")

            # 3 ─ contribute real prose, as B
            facts["contribute_result"] = await call(
                session, rec, "contribute",
                {"text": FINDING_PROSE, "agent_session_id": ids["B"]},
                note="distilled by the claude-cli arm (haiku) on the subscription")

            # 4 ─ query, as A. Deliberately the OTHER conversation: suppression
            #     is keyed on agent_session (decisions/001), so asking as B
            #     could legitimately hide B's own finding and would prove
            #     nothing about attribution.
            facts["query_result"] = await call(
                session, rec, "query",
                {"question": QUESTION, "agent_session_id": ids["A"]},
                note="asked as conversation A, so B's finding is a teammate's, not an echo")

            # 4b ─ the SAME question asked by the conversation that wrote the
            #      finding. Suppression is keyed on the asking conversation
            #      (decisions/001), so B must NOT be handed its own work back.
            #      This is the half of the invariant step 4 cannot show.
            facts["query_as_author"] = await call(
                session, rec, "query",
                {"question": QUESTION, "agent_session_id": ids["B"]},
                note="asked as B, the author — its own findings should be suppressed")

            # Both spellings of the watermark, deliberately. The CONTENT fields
            # (`by_type`, `topics`) suppress by the asking CONVERSATION, so a
            # read that names only the contributor sees an EMPTY by_type while
            # the briefing — which sends both — sees all eight. That is
            # api.py's documented split, and quoting only the first read would
            # look like a discrepancy in the evidence.
            wm = f"{SERVICE_URL}/v1/sessions/{shared_id}/watermark"
            rec.record("GET /watermark?contributor=… (no agent_session)",
                       {"url": f"{wm}?contributor={contributor}"},
                       http_get(f"{wm}?contributor={contributor}"), elapsed_s=0.0,
                       note="every finding is this contributor's, so CONTENT fields "
                            "suppress to empty — by design, not a gap")
            rec.record("GET /watermark?contributor=…&agent_session=C",
                       {"url": f"{wm}?contributor={contributor}"
                               f"&agent_session={ids['C']}"},
                       http_get(f"{wm}?contributor={contributor}"
                                f"&agent_session={ids['C']}"), elapsed_s=0.0,
                       note="what the rejoining conversation's briefing is composed from")

            # 5 ─ leave, as B only. A must survive it — that is the W2 fix.
            facts["leave_result"] = await call(
                session, rec, "leave_session", {"agent_session_id": ids["B"]},
                note="only B detaches; A stays bound")

            # 6 ─ REJOIN under a NEW conversation id, same contributor
            facts["rejoin_result"] = await call(
                session, rec, "join_session",
                {"shared_id": shared_id, "agent_session_id": ids["C"]},
                note="the storyboard's awareness beat: same human, new conversation")

    # 7 ─ what the rejoined conversation is TOLD on arrival. New connection,
    #     after one refresh interval, because that is the only way instructions
    #     are re-read.
    print(f"\n  (waiting {BRIEFING_REFRESH_WAIT}s for one briefing refresh interval)",
          flush=True)
    await asyncio.sleep(BRIEFING_REFRESH_WAIT)
    facts["briefing_after_rejoin"] = await connect_and_read_briefing(
        rec, "arrival after REJOIN",
        note="this is the arrival briefing the rejoining conversation receives")

    # 8 ─ end, and 9 ─ prove the memory is really closed
    async with streamablehttp_client(ORCHESTRATOR_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            facts["end_result"] = await call(session, rec, "end_session", {},
                                             note="creator-only; refuses if others remain")
            facts["query_after_end"] = await call(
                session, rec, "query",
                {"question": QUESTION, "agent_session_id": ids["A"]},
                note="proof the close is real, not cosmetic")

    return facts


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--contributor", required=True,
                        help="the identity serve_local.py was started with — used "
                             "only to address the service's own watermark route")
    parser.add_argument("--json-out", type=Path,
                        help="where to dump the full recorded transcript")
    args = parser.parse_args(argv)

    scratch_cwd = Path(tempfile.mkdtemp(prefix="synapse-w7-drive-"))
    slug_dir = CLAUDE_PROJECTS / project_slug(scratch_cwd)
    ids = {"A": str(uuid.uuid4()), "B": str(uuid.uuid4()), "C": str(uuid.uuid4())}

    print(f"started      {datetime.now(timezone.utc).isoformat()}")
    print(f"orchestrator {ORCHESTRATOR_URL}")
    print(f"service      {SERVICE_URL}")
    print(f"scratch cwd  {scratch_cwd}")
    print(f"transcripts  {slug_dir}  (real ~/.claude/projects, throwaway slug)")
    for label, session_id in ids.items():
        print(f"  conversation {label}: {session_id}")

    rec = Recorder()
    facts: dict[str, Any] = {}
    try:
        paths = write_transcripts(slug_dir, list(ids.values()))
        print(f"wrote        {len(paths)} transcript(s)\n")
        facts = await drive(rec, args.contributor, ids)
        return 0
    finally:
        shutil.rmtree(scratch_cwd, ignore_errors=True)
        shutil.rmtree(slug_dir, ignore_errors=True)
        print(f"\ncleaned up   {scratch_cwd} and {slug_dir}", flush=True)
        if args.json_out:
            args.json_out.write_text(json.dumps(
                {"started_utc": datetime.now(timezone.utc).isoformat(),
                 "facts": facts, "steps": rec.steps}, indent=2, ensure_ascii=False),
                encoding="utf-8")
            print(f"wrote        {args.json_out}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
