"""synapse-server — install, configure, run: the server half, decoupled.

The server package is the Synapse Service and nothing else. A machine that
hosts it never needs the worker, the orchestrator, or an NPU. Lifecycle:

  install     puts this package on the machine (install.sh --server /
              install.ps1 -Server). Writes nothing, starts nothing.
  configure   `synapse-server configure --keys /path/to/keys.txt` — the keys
              FILE holds one inference-cloud API key per line (blank lines
              and #-comments ignored). The path is stored; the file is
              re-read on every `up`, so rotating keys is editing the file.
  run         `synapse-server up` — assembles the environment the service
              reads, health-checks every key against the inference cloud
              FIRST (a key that 401s must fail the boot, not the first
              synthesis an hour later), then serves in the foreground.
  inspect     `synapse-server health` — configuration + key check + port.

Configuration lives in the same ~/.synapse/config.toml the client uses
(`server.*` keys) — one file, one `config set` grammar, per machine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from synapse_contracts import userconfig

DEFAULT_HOST = "0.0.0.0"     # a decoupled server exists to be reached over LAN
DEFAULT_PORT = 8899
KEY_PROBE_TIMEOUT_S = 10.0

# The ADR-0005 operating point scripts/serve_local.py always injected. A
# hand-started service used to lose it and truncate every synthesis; the
# server CLI is now the one place that sets it (ambient env still wins, so an
# operator override survives).
SYNTHESIS_MAX_TOKENS = "1600"
SYNTHESIS_TIMEOUT_S = "180"


def _say(line: str = "") -> None:
    print(line, flush=True)


def _ask(prompt: str) -> str:
    """input(), with Ctrl-D meaning "no answer" — the caller's skip path
    applies, same as an empty Enter. Ctrl-C is deliberately NOT caught here:
    abandoning the whole command is main()'s job (exit 130), never a
    traceback."""
    try:
        return input(prompt).strip()
    except EOFError:
        _say()          # move past the prompt line Ctrl-D left open
        return ""


def read_keys_file(path: Path) -> list[str]:
    """One key per line; blanks and #-comments ignored; order preserved
    (rotation starts at the first). Loud, specific errors — this is the one
    piece of server configuration a human types."""
    if not path.is_file():
        raise SystemExit(
            f"synapse-server: keys file not found: {path}\n"
            f"Create it with one API key per line, then:\n"
            f"  synapse-server configure --keys {path}")
    keys: list[str] = []
    for lineno, raw in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line or " " in line:
            raise SystemExit(
                f"synapse-server: {path}:{lineno} does not look like one key "
                f"per line (found a comma/space). Fix the file — keys are "
                f"joined with commas internally, so a comma inside one would "
                f"silently split it in two.")
        keys.append(line)
    if not keys:
        raise SystemExit(
            f"synapse-server: {path} holds no keys (only blanks/comments).")
    return keys


def check_key(base_url: str, key: str,
              timeout: float = KEY_PROBE_TIMEOUT_S) -> tuple[str, str]:
    """(verdict, detail) for one key: ok | unauthorized | rate-limited |
    unreachable. GET /models — zero tokens, zero spend against the shared
    credit pool, and the same route every seam already answers."""
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
            status = getattr(response, "status", None) or response.getcode()
        return ("ok", f"HTTP {status}") if status == 200 else \
               ("unreachable", f"HTTP {status}")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "unauthorized", f"HTTP {exc.code}"
        if exc.code == 429:
            # Rate-limited proves the key is VALID — the pool is just busy.
            return "rate-limited", "HTTP 429 (key is valid; pool is busy)"
        return "unreachable", f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return "unreachable", f"{type(reason).__name__ if isinstance(reason, BaseException) else 'error'}: {reason}"


def _mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) > 12 else "…" + key[-2:]


def preflight_keys(base_url: str, keys: list[str]) -> bool:
    """Check every key, say what each one is, and return whether the pool is
    usable (at least one ok/rate-limited key). Runs on every `up` — it costs
    one metadata GET per key and catches the revoked-key case the day it
    happens rather than mid-demo."""
    _say(f"  key check  {len(keys)} key(s) against {base_url}")
    usable = 0
    for index, key in enumerate(keys, start=1):
        verdict, detail = check_key(base_url, key)
        marker = "ok " if verdict in ("ok", "rate-limited") else "BAD"
        if verdict in ("ok", "rate-limited"):
            usable += 1
        _say(f"    [{marker}] key {index} ({_mask(key)}): {verdict} — {detail}")
    if usable == 0:
        _say("  key check  NO usable key — every synthesis and every ranked "
             "query would fail.")
        return False
    if usable < len(keys):
        _say(f"  key check  {usable}/{len(keys)} usable — the dead ones are "
             f"skipped by rotation, but fix the file when you can.")
    else:
        _say(f"  key check  all {len(keys)} usable")
    return True


def _resolve_synthesizer(cfg: dict[str, str]) -> str:
    configured = cfg.get("server.synthesizer")
    if configured:
        return configured
    # Unset: infer the obvious arm rather than booting a stub by surprise —
    # a machine with a keys file configured wants the cloud.
    return "aic100" if cfg.get("server.keys_file") else "fake"


def cmd_configure(args: argparse.Namespace) -> int:
    interactive = sys.stdin.isatty() and not args.yes

    keys_path = args.keys
    if keys_path is None and not userconfig.get("server.keys_file") and interactive:
        _say("Path to your API keys file (one key per line — the same file "
             "can hold the whole team's pool):")
        keys_path = _ask("server.keys_file: ") or None
    if keys_path:
        resolved = Path(keys_path).expanduser().resolve()
        keys = read_keys_file(resolved)      # validates before storing
        userconfig.set_value("server.keys_file", str(resolved))
        userconfig.set_value("server.synthesizer",
                             args.synthesizer or "aic100")
        _say(f"  keys      {len(keys)} key(s) read from {resolved} — the file "
             f"is re-read on every `synapse-server up`, never copied.")
    elif args.synthesizer:
        userconfig.set_value("server.synthesizer", args.synthesizer)

    if args.base_url:
        userconfig.set_value("server.base_url", args.base_url.rstrip("/"))
    if args.model:
        userconfig.set_value("server.model", args.model)
    if args.host:
        userconfig.set_value("server.host", args.host)
    if args.port:
        userconfig.set_value("server.port", str(args.port))

    _say()
    _say("Configured. Next:")
    _say("  synapse-server up       # serve (foreground; Ctrl-C stops)")
    _say("  synapse-server health   # configuration + key check")
    _say(f"  config file: {userconfig.config_path()}")
    return 0


def _assemble_env(cfg: dict[str, str], *, skip_key_check: bool,
                  force: bool) -> dict[str, str]:
    """The environment synapse_service.cli reads, from config — with the
    first-start health checks in front of it."""
    env: dict[str, str] = {}
    synthesizer = _resolve_synthesizer(cfg)
    env["SYNAPSE_SYNTHESIZER"] = synthesizer

    if synthesizer == "aic100":
        keys_file = cfg.get("server.keys_file")
        if not keys_file and not (os.environ.get("INFERENCE_CLOUD_API_KEYS")
                                  or os.environ.get("INFERENCE_CLOUD_API_KEY")):
            raise SystemExit(
                "synthesizer aic100 needs keys. Point the server at your "
                "keys file (one key per line):\n"
                "  synapse-server configure --keys /path/to/keys.txt")
        if keys_file:
            keys = read_keys_file(Path(keys_file))
            env["INFERENCE_CLOUD_API_KEYS"] = ",".join(keys)
        base_url = (cfg.get("server.base_url")
                    or os.environ.get("INFERENCE_CLOUD_BASE_URL"))
        if base_url:
            env["INFERENCE_CLOUD_BASE_URL"] = base_url
        if cfg.get("server.model"):
            env["INFERENCE_CLOUD_MODEL"] = cfg["server.model"]
        env.setdefault("INFERENCE_CLOUD_MAX_TOKENS",
                       os.environ.get("INFERENCE_CLOUD_MAX_TOKENS",
                                      SYNTHESIS_MAX_TOKENS))
        env.setdefault("INFERENCE_CLOUD_TIMEOUT",
                       os.environ.get("INFERENCE_CLOUD_TIMEOUT",
                                      SYNTHESIS_TIMEOUT_S))
        if keys_file and not skip_key_check:
            from synapse_providers.aic100 import DEFAULT_BASE_URL
            effective = (base_url or DEFAULT_BASE_URL).rstrip("/")
            if not preflight_keys(effective, keys) and not force:
                raise SystemExit(
                    "synapse-server: refusing to start with zero usable keys "
                    "(--force overrides; --skip-key-check skips the probe).")
    return env


def cmd_up(args: argparse.Namespace) -> int:
    cfg = userconfig.load()
    env = _assemble_env(cfg, skip_key_check=args.skip_key_check,
                        force=args.force)
    os.environ.update(env)

    host = args.host or cfg.get("server.host") or DEFAULT_HOST
    port = int(args.port or cfg.get("server.port") or DEFAULT_PORT)

    _say(f"synapse-server: synthesizer {env.get('SYNAPSE_SYNTHESIZER')}, "
         f"binding {host}:{port}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        lan = _lan_ip() or "<this-machine-lan-ip>"
        _say(f"  clients on the LAN reach this service at http://{lan}:{port}")
        _say("  a teammate connects with:")
        _say(f"    synapse config set service.url http://{lan}:{port}")
        _say("    synapse up")
        _say("  (no auth on this port — anyone who can reach it can read "
             "and write the team's memory)")

    from synapse_service import cli as service_cli
    return service_cli.main(["--host", host, "--port", str(port)])


def _lan_ip() -> str | None:
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def cmd_health(args: argparse.Namespace) -> int:
    cfg = userconfig.load()
    synthesizer = _resolve_synthesizer(cfg)
    results: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str, remedy: str = "") -> None:
        results.append({"name": name, "status": status, "detail": detail,
                        "remedy": remedy})

    add("config server.synthesizer", "PASS", synthesizer
        + ("" if cfg.get("server.synthesizer") else " (inferred — set it with "
           "`synapse config set server.synthesizer …`)"))

    keys_file = cfg.get("server.keys_file")
    if synthesizer == "aic100":
        if not keys_file:
            add("keys file", "FAIL", "unset",
                "synapse-server configure --keys /path/to/keys.txt")
        else:
            path = Path(keys_file)
            if not path.is_file():
                add("keys file", "FAIL", f"{path} is missing",
                    "restore the file or re-run `synapse-server configure --keys …`")
            else:
                try:
                    keys = read_keys_file(path)
                except SystemExit as exc:
                    add("keys file", "FAIL", str(exc))
                    keys = []
                if keys:
                    add("keys file", "PASS", f"{len(keys)} key(s) in {path}")
                    from synapse_providers.aic100 import DEFAULT_BASE_URL
                    base = (cfg.get("server.base_url") or DEFAULT_BASE_URL).rstrip("/")
                    usable = 0
                    for index, key in enumerate(keys, start=1):
                        verdict, detail = check_key(base, key)
                        ok = verdict in ("ok", "rate-limited")
                        usable += ok
                        add(f"key {index} ({_mask(key)})",
                            "PASS" if ok else "FAIL", f"{verdict} — {detail}")
                    add("key pool", "PASS" if usable else "FAIL",
                        f"{usable}/{len(keys)} usable")

    import socket
    port = int(cfg.get("server.port") or DEFAULT_PORT)
    probe = socket.socket()
    probe.settimeout(0.5)
    try:
        listening = probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()
    add(f"service :{port}",
        "PASS" if listening else "WARN",
        "answering" if listening else "not running",
        "" if listening else "`synapse-server up` starts it (nothing runs at "
                             "install time, by design)")

    if args.json:
        print(json.dumps({"config": cfg, "results": results}, indent=2))
    else:
        width = max(len(r["name"]) for r in results)
        for r in results:
            _say(f"{r['status']:<5} {r['name']:<{width}}  {r['detail']}")
            if r["remedy"]:
                _say(f"      {'':<{width}}  -> {r['remedy']}")
        fails = sum(r["status"] == "FAIL" for r in results)
        _say()
        _say(f"{sum(r['status'] == 'PASS' for r in results)} pass, "
             f"{sum(r['status'] == 'WARN' for r in results)} warn, {fails} fail")
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        prog="synapse-server",
        description="Synapse Service lifecycle: configure (keys file, model), "
                    "up (serve, with a key health check first), health.")
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser(
        "configure", help="store server settings — keys file path, base URL, "
                          "model, synthesizer, host/port")
    configure.add_argument("--keys", metavar="FILE",
                           help="file with one inference-cloud API key per line")
    configure.add_argument("--base-url", metavar="URL")
    configure.add_argument("--model", metavar="NAME")
    configure.add_argument("--synthesizer",
                           choices=("aic100", "npu", "anthropic", "fake"))
    configure.add_argument("--host")
    configure.add_argument("--port", type=int)
    configure.add_argument("--yes", "-y", action="store_true",
                           help="never prompt")
    configure.set_defaults(func=cmd_configure)

    up = sub.add_parser(
        "up", help="health-check the keys, then serve in the foreground "
                   "(Ctrl-C stops)")
    up.add_argument("--host")
    up.add_argument("--port", type=int)
    up.add_argument("--skip-key-check", action="store_true",
                    help="do not probe the keys before serving")
    up.add_argument("--force", action="store_true",
                    help="serve even when no key is usable")
    up.set_defaults(func=cmd_up)
    run = sub.add_parser("run", help="alias of `up`")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--skip-key-check", action="store_true")
    run.add_argument("--force", action="store_true")
    run.set_defaults(func=cmd_up)

    health = sub.add_parser("health",
                            help="configuration + key check + port state")
    health.add_argument("--json", action="store_true")
    health.set_defaults(func=cmd_health)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Ctrl-C at the configure prompt or during the key preflight probes
        # is a normal way to leave, not a crash: newline past the echoed ^C,
        # exit 130 (128+SIGINT), never a traceback. Once uvicorn is serving
        # (`up`), SIGINT is its graceful shutdown and returns here normally.
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `synapse-server health --json | jq .` closing the pipe early is
        # routine, not an error: exit 141 (128+SIGPIPE), quietly.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141


if __name__ == "__main__":
    sys.exit(main())
