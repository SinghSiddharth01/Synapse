"""The `synapse` entry point. Subcommands live in their own modules."""

from __future__ import annotations

import argparse
import os
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synapse",
        description="Synapse client — configure once, `synapse up` to run, "
                    "`synapse health` to inspect. Install never configures; "
                    "configure never starts; `up` starts and Ctrl-C stops.")
    from synapse_cli import __version__
    parser.add_argument("--version", action="version",
                        version=f"synapse-cli {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    from synapse_cli import config_cmd, health, pack, up
    config_cmd.add_parsers(sub)
    up.add_parser(sub)
    health.add_parser(sub)
    pack.add_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows writes piped stdout in the locale codepage (cp1252), not UTF-8;
    # banner content (contributor names, session purposes) is runtime-supplied.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        # Ctrl-C at a prompt or mid-command is a normal way to leave, not a
        # crash: a bare newline to move past the echoed ^C, then the shell's
        # 128+SIGINT convention. Never a traceback.
        print(file=sys.stderr)
        return 130
    except BrokenPipeError:
        # `synapse … | head` closing the pipe early is routine. Point stdout
        # at devnull so interpreter shutdown does not add "Exception ignored"
        # noise, and exit 141 (128+SIGPIPE) like any piped Unix tool.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141


if __name__ == "__main__":
    sys.exit(main())
