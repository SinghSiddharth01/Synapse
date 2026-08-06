"""Pre-flight check for a machine that is about to run Synapse.

    uv run python scripts/doctor.py
    uv run python scripts/doctor.py --json

WHAT THIS IS: eight checks that answer "will `scripts/serve_local.py` work on
this box, and if not, which line do I fix?" — asked BEFORE anything is started.
It reads the filesystem, the environment, and three TCP ports. It starts
nothing, stops nothing, and makes no HTTP request of any kind.

WHY PRE-FLIGHT ONLY. The obvious next checks — service reachable, MCP
`initialize`, tool listing, a round-trip query — are deliberately absent,
because they are already asserted by things that exist:

  * `serve_local.py` refuses to print its banner unless the service answered
    `/debug`, the orchestrator answered `/mcp`, the Shared Session was created
    or adopted, and `.synapse/bindings/claude-code.json` was written. A live
    banner IS the post-flight check, and it is the one an operator already
    watches for.
  * `scripts/verify_instructions.py` already probes the SENTINEL end to end,
    standalone, and is the right place for that to live.

Duplicating either here would mean two code paths that can disagree about
whether the stack is healthy, and the doctor's copy would be the one nobody
maintains. So: ONE code path, and `--doctor-only` on the installers runs the
whole of it rather than a subset.

WHY PYTHON AND NOT sh + PowerShell. The alternative is writing every check
twice and letting the two drift; the interpreter check, the `mcp` version
check, and the Unicode canary are all things only Python can honestly answer
about the venv the stack will actually run in.

Exit code is 1 if any check FAILs. WARN never fails the run — a WARN is
"this machine can still run the demo, but you should know".
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Windows writes piped stdout in the locale codepage (cp1252), not UTF-8, and
# check 4 below exists precisely to print characters cp1252 cannot map. The
# installers run this with output teed to a file, which is exactly the
# redirection that turns that into a UnicodeEncodeError — so the doctor would
# die inside the check meant to diagnose the problem. Same guard as
# scripts/run_npu_eval.py:36-40.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent

PORTS = (8787, 8899, 18181)
PORT_ROLES = {8787: "orchestrator", 8899: "service", 18181: "model seam"}
REQUIRED_MCP = "1.9.4"
PACK_SKILL = "synapse-shared-memory"


@dataclass
class Result:
    name: str
    status: str          # "PASS" | "WARN" | "FAIL"
    detail: str
    remedy: str = ""


def _run(argv: list[str], timeout: float = 10.0) -> tuple[int, str]:
    """Best-effort subprocess. Never raises: a missing binary is data, not an error."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              cwd=str(REPO))
    except (OSError, subprocess.SubprocessError):
        return (127, "")
    return (done.returncode, (done.stdout or "") + (done.stderr or ""))


def _is_work_tree() -> bool:
    code, out = _run(["git", "rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


# --------------------------------------------------------------------------
# preamble — not a check. An operator parked on a fallback branch needs to see
# that before they read anything else, because every other line here is about
# a checkout whose identity they have just assumed.
# --------------------------------------------------------------------------

def preamble() -> dict[str, str]:
    info = {"repo": str(REPO), "branch": "?", "sha": "?", "tree": "?"}
    if not _is_work_tree():
        info["branch"] = info["sha"] = "(not a git checkout)"
        info["tree"] = "unknown"
        return info
    _, branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _, sha = _run(["git", "rev-parse", "--short", "HEAD"])
    code, status = _run(["git", "status", "--porcelain"])
    info["branch"] = branch.strip() or "?"
    info["sha"] = sha.strip() or "?"
    info["tree"] = "dirty" if (code == 0 and status.strip()) else "clean"
    return info


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_uv() -> Result:
    path = shutil.which("uv")
    if not path:
        line = ('powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'
                if os.name == "nt" else
                "curl -LsSf https://astral.sh/uv/install.sh | sh")
        return Result("uv", "FAIL", "not on PATH", f"install it: {line}")
    _, out = _run([path, "--version"])
    return Result("uv", "PASS", f"{out.strip() or 'present'}  ({path})")


def check_interpreter() -> Result:
    version = sys.version.split()[0]
    machine = platform.machine()
    detail = f"python {version}  {machine}  ({sys.executable})"

    if sys.version_info < (3, 12):
        return Result("interpreter", "FAIL", detail,
                      "Synapse needs Python 3.12+; re-create the venv with "
                      "`uv sync --python 3.12` or newer")

    # ARM64 Windows: uv will happily provision an x86_64 interpreter that runs
    # under Prism emulation. Nothing complains at sync time. The failure
    # surfaces much later as a Rust build error out of `cryptography`, which
    # reads like a packaging problem and is not one.
    if os.name == "nt":
        host_arm = "ARM64" in (
            (os.environ.get("PROCESSOR_ARCHITECTURE") or "").upper(),
            (os.environ.get("PROCESSOR_ARCHITEW6432") or "").upper(),
        )
        if host_arm and machine.upper() != "ARM64":
            return Result(
                "interpreter", "FAIL", detail + "  [host is ARM64]",
                "this venv is emulated x86_64 under Prism; re-create with "
                "`uv sync --python <ARM64 python.exe>` — NPU wheels and "
                "`cryptography` fail here with a Rust build error that looks "
                "unrelated.")
    return Result("interpreter", "PASS", detail)


def check_mcp_version() -> Result:
    try:
        found = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        return Result("mcp version", "FAIL", "mcp is not installed in this venv",
                      "run `uv sync --frozen` from the repo root")
    if found != REQUIRED_MCP:
        return Result("mcp version", "FAIL", f"{found} (expected {REQUIRED_MCP})",
                      "do not upgrade mcp; re-sync from uv.lock "
                      "(`uv sync --frozen`). Newer mcp pulls a `cryptography` "
                      "with no ARM64 Windows wheel.")
    return Result("mcp version", "PASS", found)


# Where the canary is written. stdout is the stream that matters, but `--json`
# owns stdout for its document, so that mode writes the canary to stderr — the
# same console, the same codepage, the same tee (`2>&1`), just not inside the
# JSON. Set by main() before the checks run.
_CANARY_STREAM = sys.stdout


def check_unicode() -> Result:
    """Write the characters the stack really emits, through a real stream.

    Only proves something when run through the SAME redirection the operator
    will use — a bare TTY on macOS passes trivially, because the failure is a
    Windows codepage problem that appears when stdout is a pipe or a file. The
    installers therefore tee this output; that tee is the actual test.
    """
    stream = _CANARY_STREAM
    encoding = getattr(stream, "encoding", None) or "?"
    utf8_env = os.environ.get("PYTHONUTF8") or "unset"
    where = "stdout" if stream is sys.stdout else "stderr"
    detail = f"{where}={encoding}  PYTHONUTF8={utf8_env}"
    try:
        stream.write("      canary: ←  →  ─  ⋯  ⚠"
                     "   (meaningful only when redirected/teed, as the "
                     "installer does)\n")
        stream.flush()
    except UnicodeEncodeError as exc:
        return Result("unicode", "FAIL", f"{detail} — {exc.__class__.__name__}",
                      "set PYTHONUTF8=1 (and `chcp 65001` for the console) "
                      "before running; install.ps1 does both")
    return Result("unicode", "PASS", detail)


def _port_owner(port: int) -> str:
    if os.name == "nt":
        code, out = _run(["powershell", "-NoProfile", "-Command",
                          f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                          f"-ErrorAction SilentlyContinue).OwningProcess"])
    else:
        code, out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    if code != 0:
        return ""              # no lsof / no powershell is not an error
    pids = " ".join(sorted(set(out.split())))
    return f" pid {pids}" if pids else ""


def check_ports() -> Result:
    """Bind-probe 127.0.0.1 the way serve_local.py does before it starts anything.

    Copied rather than imported — importing serve_local pulls its whole module
    body and this file promises to start nothing. The authority for this
    behaviour is `claim_ports` at scripts/serve_local.py:147-177; if that
    changes, change this.
    """
    taken, free = [], []
    for port in PORTS:
        probe = socket.socket()
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
            free.append(port)
        except OSError:
            taken.append(port)
        finally:
            probe.close()

    if not taken:
        return Result("ports", "PASS",
                      "  ".join(f"{p} free ({PORT_ROLES[p]})" for p in free))

    held = "  ".join(f"{p} LISTENING ({PORT_ROLES[p]}){_port_owner(p)}" for p in taken)
    rest = "  ".join(f"{p} free" for p in free)
    return Result(
        "ports", "WARN", (held + ("  |  " + rest if rest else "")),
        "a Synapse is already running on this machine — that is a normal "
        "state, not a fault. serve_local.py will refuse with \"ports already "
        "in use\"; stop the old one, or re-run the installer with --clean. "
        "If you are trying to listen alongside a host you already run, you "
        "do not need a second one.")


def check_secrets() -> Result:
    secrets = REPO / "secrets.jsonc"
    if not secrets.exists():
        return Result("secrets", "WARN", "secrets.jsonc absent",
                      "the stand-in arm works without it; install.sh creates "
                      "it from secrets.example.jsonc, then you paste keys in")

    stripped = re.sub(r"^\s*//.*$", "", secrets.read_text(encoding="utf-8"),
                      flags=re.MULTILINE)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        # Line number only. The offending text is very likely a credential.
        return Result("secrets", "FAIL",
                      f"secrets.jsonc is not valid JSONC (line {exc.lineno})",
                      "fix the syntax; note only `//` line comments are "
                      "stripped — `/* */` is not supported")

    # Booleans only, forever. Never a value, never a prefix, never a length.
    cloud = data.get("inference_cloud") or {}
    anthropic = data.get("anthropic") or {}
    flags = [
        f"inference_cloud.api_key={'set' if (cloud.get('api_key') or data.get('api_key')) else 'empty'}",
        f"inference_cloud.base_url={'set' if cloud.get('base_url') else 'empty'}",
        f"anthropic.api_key={'set' if anthropic.get('api_key') else 'empty'}",
    ]
    detail = "parses; " + "  ".join(flags)

    if not _is_work_tree():
        return Result("secrets", "WARN", detail,
                      "not a git checkout (zip download?) — cannot verify "
                      "secrets.jsonc is ignored; do not commit it")
    code, _ = _run(["git", "check-ignore", "-q", "secrets.jsonc"])
    if code != 0:
        return Result("secrets", "FAIL", detail + " — NOT gitignored",
                      "secrets.jsonc must never be committed. Restore the "
                      "`secrets.jsonc` line in .gitignore before you commit "
                      "anything.")
    return Result("secrets", "PASS", detail + "; gitignored")


def _claude_home() -> Path:
    if os.name == "nt":
        base = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return Path(base) / ".claude"
    return Path(os.path.expanduser("~")) / ".claude"


def check_pack() -> Result:
    home = _claude_home()
    skill = home / "skills" / PACK_SKILL
    extras = []
    for name in ("commands", "agents"):
        try:
            if (home / name).is_dir() and any((home / name).iterdir()):
                extras.append(name)
        except OSError:
            pass
    seen = f"; also present: {', '.join(extras)}" if extras else ""
    if skill.is_dir():
        return Result("awareness", "PASS", f"{skill}{seen}")
    return Result("awareness", "WARN", f"pack not installed at {skill}{seen}",
                  "re-run install.sh, or copy packs/claude-code/skills/ into "
                  f"{home / 'skills'} yourself. Optional: without it, shared "
                  "memory still works on demand via `query`, it just does not "
                  "surface proactively.")


def check_claude_mcp() -> Result:
    binary = shutil.which("claude")
    if not binary:
        return Result("claude mcp", "WARN", "`claude` not on PATH",
                      "install Claude Code to connect an agent; the stack "
                      "itself runs without it")
    code, out = _run([binary, "mcp", "list"], timeout=15.0)
    if code == 127:
        return Result("claude mcp", "WARN", "`claude mcp list` did not run",
                      "check your Claude Code install")
    line = next((ln for ln in out.splitlines() if "synapse" in ln.lower()), "")
    if not line:
        return Result("claude mcp", "WARN", "no `synapse` server registered here",
                      "run `claude mcp add --transport http --scope project "
                      "synapse http://127.0.0.1:8787/mcp` from the project you "
                      "want shared memory in")
    if "failed" in line.lower():
        return Result("claude mcp", "WARN", line.strip(),
                      "registered but not connected — start the stack, then "
                      "pick Reconnect in `/mcp`")
    return Result("claude mcp", "PASS", line.strip())


CHECKS = (
    check_uv,
    check_interpreter,
    check_mcp_version,
    check_unicode,
    check_ports,
    check_secrets,
    check_pack,
    check_claude_mcp,
)


def run_all() -> list[Result]:
    return [check() for check in CHECKS]


def main(argv: list[str] | None = None) -> int:
    global _CANARY_STREAM

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true",
                        help="emit the results as JSON for machine reading")
    parser.add_argument("--quiet", action="store_true",
                        help="print only WARN and FAIL lines")
    args = parser.parse_args(argv)

    head = preamble()

    if args.json:
        _CANARY_STREAM = sys.stderr        # keep the JSON document clean
        results = run_all()
        print(json.dumps({"repo": head, "results": [asdict(r) for r in results]},
                         indent=2))
        return 1 if any(r.status == "FAIL" for r in results) else 0

    print(f"repo   {head['repo']}")
    print(f"branch {head['branch']}  {head['sha']}  ({head['tree']})")
    print()

    results = []
    for check in CHECKS:
        result = check()
        results.append(result)
        if args.quiet and result.status == "PASS":
            continue
        print(f"{result.status:<5} {result.name:<13} {result.detail}")
        if result.remedy:
            print(f"      -> remedy: {result.remedy}")

    failed = [r for r in results if r.status == "FAIL"]
    warned = [r for r in results if r.status == "WARN"]
    print()
    print(f"{len(results) - len(failed) - len(warned)} pass, "
          f"{len(warned)} warn, {len(failed)} fail")
    if failed:
        print("this machine is not ready — fix the FAIL lines above.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
