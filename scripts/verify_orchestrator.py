"""One-off live proof of the actual join mechanism (Plan A.7 / Plan D.2/D.3).

Not part of the pytest suite deliberately — it launches real subprocesses and
briefly writes into the REAL `~/.claude/projects/` directory (under a
throwaway project slug that cannot collide with a real project, since the
"cwd" it corresponds to is a scratch temp directory created for this run
only). Cleans up on the way out, including on failure.

    uv run python scripts/verify_orchestrator.py

Proves, over real subprocesses rather than in-process function calls:
  1. `synapse-worker join <shared_id>` binds the currently-live transcript
  2. a second, more-recently-written transcript does NOT steal the binding
  3. `synapse-worker status` (a fresh process) reports the join
  4. `synapse-orchestrator` boots and serves streamable-http per ADR 0001, and
     a real MCP client sees a named server with zero registered capabilities
     -- honest, not a placeholder pretending to do more
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from synapse_worker.discovery import CLAUDE_PROJECTS, project_slug

# Same Windows/cp1252 reason as scripts/run_npu_eval.py — this script's status
# marks are non-cp1252 codepoints, so a redirected run died before its first
# line rather than reporting anything.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)


async def verify_join(scratch_cwd: Path, real_project_dir: Path) -> None:
    real_project_dir.mkdir(parents=True, exist_ok=True)
    joined_transcript = real_project_dir / "sess-at-join-time.jsonl"
    joined_transcript.write_text('{"type": "user", "message": {"content": "hi"}}\n',
                                  encoding="utf-8")

    env = {**os.environ, "SYNAPSE_STATE_DIR": str(scratch_cwd / ".synapse")}

    print("── join: real subprocess, real ~/.claude/projects " + "─" * 10)
    result = subprocess.run(
        [sys.executable, "-m", "synapse_worker.cli", "join", "verify-session",
         "--contributor", "ci"],
        cwd=scratch_cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    print(f"  exit code: {result.returncode}")
    print(f"  stdout:\n" + "\n".join(f"    {line}" for line in result.stdout.splitlines()))
    assert result.returncode == 0, result.stderr
    assert "verify-session" in result.stdout

    binding_file = scratch_cwd / ".synapse" / "bindings" / "claude-code.json"
    assert binding_file.is_file(), f"no binding written at {binding_file}"
    binding = json.loads(binding_file.read_text(encoding="utf-8"))
    print(f"\n  binding written: {binding_file}")
    print(f"  {binding}")
    assert binding["shared_id"] == "verify-session"
    assert binding["transcript_path"] == str(joined_transcript)

    print("\n── a newer transcript must not steal the join " + "─" * 12)
    newer = real_project_dir / "sess-appeared-later.jsonl"
    newer.write_text('{"type": "user", "message": {"content": "later"}}\n', encoding="utf-8")

    status = subprocess.run(
        [sys.executable, "-m", "synapse_worker.cli", "status"],
        cwd=scratch_cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    print(f"  synapse-worker status (fresh process):")
    for line in status.stdout.splitlines():
        if "joined" in line.lower() or "transcript" in line.lower():
            print(f"    {line}")
    assert "verify-session" in status.stdout
    assert str(joined_transcript) in status.stdout or joined_transcript.name in status.stdout
    print("\n  PASS — join beats the newer transcript.")


async def verify_shell(port: int) -> None:
    print(f"\n── orchestrator: real HTTP subprocess on :{port} " + "─" * 8)
    proc = subprocess.Popen(
        [sys.executable, "-m", "synapse_orchestrator.cli", "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("orchestrator did not open its port in time")
        print(f"  listening on 127.0.0.1:{port}")

        url = f"http://127.0.0.1:{port}/mcp"
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                prompts = await session.list_prompts()
                tools = await session.list_tools()
                print(f"  server name:  {init.serverInfo.name}")
                print(f"  prompts:      {[p.name for p in prompts.prompts]}")
                print(f"  tools:        {[t.name for t in tools.tools]}")
                assert init.serverInfo.name == "synapse"
                assert prompts.prompts == []
                assert tools.tools == []
        print("  PASS — shell boots over real HTTP, honestly empty.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def main() -> int:
    scratch_cwd = Path(tempfile.mkdtemp(prefix="synapse-join-verify-"))
    real_project_dir = CLAUDE_PROJECTS / project_slug(scratch_cwd)
    print(f"scratch cwd       {scratch_cwd}")
    print(f"claude project    {real_project_dir}  (real ~/.claude/projects, throwaway slug)\n")

    try:
        await verify_join(scratch_cwd, real_project_dir)
        await verify_shell(_free_port())
        print("\nALL CHECKS PASSED.")
        return 0
    finally:
        shutil.rmtree(scratch_cwd, ignore_errors=True)
        shutil.rmtree(real_project_dir, ignore_errors=True)
        print(f"\ncleaned up {scratch_cwd} and {real_project_dir}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
