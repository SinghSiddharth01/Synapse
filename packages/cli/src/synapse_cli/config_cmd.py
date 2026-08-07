"""`synapse configure` and `synapse config` — configuration, decoupled from install.

`configure` is the guided first-run step: it asks for the service URL (pinging
it the moment it is entered), the contributor name, and the distiller arm, and
offers MCP registration. Every answer is just a `config set`, so anything can
be changed later, one key at a time, exactly like `git config`:

    synapse config set service.url http://192.168.4.44:8899
    synapse config get service.url
    synapse config list
    synapse config unset client.claude_model

Setting `service.url` always runs a ping test and says whether the service
answered — but an unreachable URL is still SAVED (warn, don't refuse): teams
routinely configure the client before the host has started the server, and a
config tool that refuses to store the truth about the future is useless.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

from synapse_contracts import userconfig

from synapse_cli.net import ping_service

DISTILLER_ARMS = ("npu", "anthropic", "claude-cli", "listen")


def _say(line: str = "") -> None:
    print(line, flush=True)


def _ping_and_report(url: str) -> bool:
    ok, detail = ping_service(url)
    if ok:
        _say(f"  ping      {url} — reachable ({detail})")
    else:
        _say(f"  ping      {url} — NOT reachable ({detail})")
        _say("            Saved anyway. Start the server there with "
             "`synapse-server up`, or fix the URL with "
             "`synapse config set service.url <url>`.")
    return ok


def _suggest_distiller() -> str:
    """The arm this machine can actually run, best first.

    geniex present → npu; `claude` present → claude-cli (no key needed);
    ANTHROPIC_API_KEY set → anthropic; otherwise listen (read-only).
    """
    if shutil.which("geniex"):
        return "npu"
    if shutil.which("claude"):
        return "claude-cli"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "listen"


def _prompt(question: str, default: str) -> str:
    reply = input(f"{question} [{default}]: ").strip()
    return reply or default


def cmd_configure(args: argparse.Namespace) -> int:
    """Guided setup. Flags answer questions non-interactively; whatever is
    neither flagged nor already configured is asked for (or defaulted under
    --yes / a non-TTY, so scripts and CI never hang on stdin)."""
    current = userconfig.load()
    interactive = sys.stdin.isatty() and not args.yes

    url = args.service_url or current.get("service.url", "")
    if not url and interactive:
        _say("Where is the Synapse Service this machine should talk to?")
        _say("(Whoever hosts it ran `synapse-server up` and can read the URL "
             "off that terminal — e.g. http://192.168.4.44:8899)")
        url = input("service.url: ").strip()
    if not url:
        _say("no service.url given (non-interactive). Set it later with:")
        _say("  synapse config set service.url http://<host>:8899")
    else:
        url = url.rstrip("/")
        userconfig.set_value("service.url", url)
        _ping_and_report(url)

    contributor = args.contributor or current.get("user.contributor", "")
    if not contributor:
        default = os.environ.get("USER") or os.environ.get("USERNAME") or "me"
        contributor = _prompt("user.contributor — findings are attributed to",
                              default) if interactive else default
    userconfig.set_value("user.contributor", contributor)

    distiller = args.distiller or current.get("client.distiller", "")
    if not distiller:
        suggested = _suggest_distiller()
        if interactive:
            distiller = _prompt(
                f"client.distiller ({' | '.join(DISTILLER_ARMS)})", suggested)
        else:
            distiller = suggested
    if distiller not in DISTILLER_ARMS:
        _say(f"synapse configure: {distiller!r} is not a distiller arm "
             f"(valid: {', '.join(DISTILLER_ARMS)})")
        return 2
    userconfig.set_value("client.distiller", distiller)

    # MCP registration — a configure-time concern, never an install-time one:
    # it writes into the PROJECT the user wants shared memory in, which only
    # they can name.
    project = args.project
    if project is None and interactive and shutil.which("claude"):
        answer = input("Register the synapse MCP server in a project directory? "
                       "(path, or empty to skip): ").strip()
        project = answer or None
    if project:
        rc = register_mcp(project)
        if rc != 0:
            return rc

    _say()
    _say("Configured. Next:")
    _say("  synapse up        # start the client stack (foreground, Ctrl-C stops)")
    _say("  synapse health    # see what is configured and what is running")
    path = userconfig.config_path()
    _say(f"  config file: {path}  (edit any time: synapse config set <key> <value>)")
    return 0


def register_mcp(project: str) -> int:
    """`claude mcp add` in `project`, idempotently. Always the LOCAL
    orchestrator: pointing at someone else's :8787 would stamp their
    attribution onto this machine's findings."""
    claude = shutil.which("claude")
    if claude is None:
        _say("synapse: `claude` is not on PATH — install Claude Code, then run:")
        _say("  claude mcp add --transport http --scope project synapse "
             "http://127.0.0.1:8787/mcp")
        return 1
    mcp_url = "http://127.0.0.1:8787/mcp"
    try:
        listed = subprocess.run([claude, "mcp", "list"], cwd=project,
                                capture_output=True, text=True, timeout=30)
        if listed.returncode == 0 and any(
                line.split(":", 1)[0].strip() == "synapse"
                for line in listed.stdout.splitlines()):
            _say(f"  mcp       already registered in {project}")
            return 0
        done = subprocess.run(
            [claude, "mcp", "add", "--transport", "http", "--scope", "project",
             "synapse", mcp_url],
            cwd=project, capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL)
        if done.returncode != 0:
            _say(f"  mcp       registration failed: {(done.stderr or done.stdout).strip()}")
            return 1
        _say(f"  mcp       registered 'synapse' ({mcp_url}) in {project}")
        return 0
    except (OSError, subprocess.SubprocessError) as exc:
        _say(f"  mcp       registration failed: {exc}")
        return 1


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "list":
        flat = userconfig.load()
        if not flat:
            _say(f"(empty — {userconfig.config_path()} does not exist yet; "
                 f"run `synapse configure`)")
            return 0
        for key in sorted(flat):
            _say(f"{key}={flat[key]}")
        return 0

    if args.action == "get":
        value = userconfig.get(args.key)
        if value is None:
            return 1
        _say(value)
        return 0

    if args.action == "unset":
        return 0 if userconfig.unset(args.key) else 1

    # set
    key, value = args.key, args.value
    if value is None:
        _say("synapse config set needs a value: synapse config set <key> <value>")
        return 2
    if key not in userconfig.KNOWN_KEYS:
        _say(f"  note: {key!r} is not a key this CLI reads "
             f"(known: {', '.join(sorted(userconfig.KNOWN_KEYS))}). Stored anyway.")
    if key == "service.url":
        value = value.rstrip("/")
    if key == "client.distiller" and value not in DISTILLER_ARMS:
        _say(f"synapse config: client.distiller must be one of "
             f"{', '.join(DISTILLER_ARMS)}, got {value!r}")
        return 2
    userconfig.set_value(key, value)
    if key == "service.url":
        # The ping test the user asked for lives at SET time, where the
        # answer is actionable — not buried in the next `up`.
        _ping_and_report(value)
    return 0


def add_parsers(sub: argparse._SubParsersAction) -> None:
    configure = sub.add_parser(
        "configure",
        help="guided setup: service URL (with ping test), contributor, "
             "distiller arm, optional MCP registration")
    configure.add_argument("--service-url", metavar="URL")
    configure.add_argument("--contributor", metavar="NAME")
    configure.add_argument("--distiller", choices=DISTILLER_ARMS)
    configure.add_argument("--project", metavar="DIR",
                           help="register the MCP server in this project directory")
    configure.add_argument("--yes", "-y", action="store_true",
                           help="never prompt; take flags, existing config and "
                                "defaults")
    configure.set_defaults(func=cmd_configure)

    config = sub.add_parser("config", help="get/set/list/unset one key, git-style")
    config_sub = config.add_subparsers(dest="action", required=True)
    c_get = config_sub.add_parser("get")
    c_get.add_argument("key")
    c_set = config_sub.add_parser("set")
    c_set.add_argument("key")
    c_set.add_argument("value", nargs="?")
    c_unset = config_sub.add_parser("unset")
    c_unset.add_argument("key")
    config_sub.add_parser("list")
    config.set_defaults(func=cmd_config)
