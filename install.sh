#!/bin/sh
# Synapse — one-command install and run, macOS / Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.sh | sh -s -- --purpose "demo session"
#
# `sh -s --` is load-bearing: it is what lets arguments through the pipe, so a
# teammate joining a session pastes ONE line instead of transcribing a URL and
# a session id by hand.
#
# POSIX sh, deliberately: this runs under `dash` on Debian-family boxes where
# /bin/sh is not bash. No arrays, no [[ ]], no `local`, no `set -o pipefail`.
#
# Safe under `curl … | sh`: stdin IS the script, so nothing here ever reads
# from it. Every interactive decision is a flag instead.
set -eu

REPO_URL="https://github.com/SinghSiddharth01/Synapse.git"
RAW_URL="https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main"
REQUIRED_MCP="1.9.4"

DIR="./Synapse"
PROJECT=""
DO_UPDATE=0
DO_CLEAN=0
DOCTOR_ONLY=0
NO_START=0
SKIP_MCP=0
FORCE=0
JOINING=0          # set when --service-url is seen; observed, never consumed
UV="uv"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# One argument per line. Deliberately NOT "$*": collapsing argv into a string
# renders --purpose "the DMA timing bug" as four words, which is exactly the
# bug someone would come here to diagnose. What execs is "$@", so what is
# shown has to have the same boundaries.
show_args() {
  for SHOW_ARG in "$@"; do
    printf '      | %s\n' "$SHOW_ARG"
  done
}

usage() {
  cat <<USAGE
Synapse installer — clones, syncs, checks, and starts the stack.

HOST a session (from nothing):
  curl -LsSf $RAW_URL/install.sh | sh -s -- --purpose "demo session"

JOIN someone's session (from nothing, one paste):
  curl -LsSf $RAW_URL/install.sh | sh -s -- --service-url http://192.168.4.44:8899 --shared-id sh-bbe76a56 --contributor akhil

Windows (PowerShell) does the same thing:
  & ([scriptblock]::Create((irm $RAW_URL/install.ps1))) -ServiceUrl http://192.168.4.44:8899 -SharedId sh-bbe76a56 -Contributor akhil

From a checkout:
  ./install.sh --purpose "demo session"

Installer flags:
  --dir <path>      where to clone (default $DIR); ignored inside a checkout
  --update          git pull --ff-only. The ONLY flag that pulls. Never
                    switches branch.
  --clean           kill orphaned synapse processes before starting
  --project <path>  where to run 'claude mcp add' (default: your cwd)
  --doctor-only     run prerequisites + doctor, then stop. Starts nothing.
  --no-start        do everything except start the stack
  --skip-mcp        do not register the MCP server or copy the awareness pack
  --force           start even if the doctor reports a FAIL
  -h, --help        this

EVERY OTHER FLAG is passed straight through to scripts/serve_local.py --
--purpose, --shared-id, --service-url, --contributor, --host, --npu, --listen,
--live, --distiller, --distiller-model, --synthesizer-model, --claude-model,
and anything added later. The installer never enumerates them, so the two
cannot drift. Run 'uv run python scripts/serve_local.py --help' for that list.
USAGE
}

# ---------------------------------------------------------------------------
# argument parsing
#
# POSIX sh has no arrays. The idiom is to rotate "$@": consume from the front,
# append what we keep to the back, and shift past the originals. That preserves
# each argument EXACTLY, spaces and all -- which matters, because --purpose
# "the DMA timing bug" must arrive at serve_local.py as one token.
# ---------------------------------------------------------------------------
ARGC=$#
KEPT=0
while [ "$ARGC" -gt 0 ]; do
  ARG=$1
  shift
  ARGC=$((ARGC - 1))
  case "$ARG" in
    --dir)     [ "$ARGC" -gt 0 ] || die "--dir needs a path"
               DIR=$1; shift; ARGC=$((ARGC - 1)) ;;
    --project) [ "$ARGC" -gt 0 ] || die "--project needs a path"
               PROJECT=$1; shift; ARGC=$((ARGC - 1)) ;;
    --update)      DO_UPDATE=1 ;;
    --clean)       DO_CLEAN=1 ;;
    --doctor-only) DOCTOR_ONLY=1 ;;
    --no-start)    NO_START=1 ;;
    --skip-mcp)    SKIP_MCP=1 ;;
    --force)       FORCE=1 ;;
    -h|--help)     usage; exit 0 ;;
    *)
      # Observed, not consumed: P7 needs to know this is a joiner run, and
      # serve_local.py still needs the flag itself.
      case "$ARG" in --service-url|--service-url=*) JOINING=1 ;; esac
      set -- "$@" "$ARG"
      KEPT=$((KEPT + 1))
      ;;
  esac
done
# Everything still in "$@" is now the pass-through list, in original order.

# ---------------------------------------------------------------------------
# P0 — preamble
# ---------------------------------------------------------------------------
say "Synapse installer"
say "  os        $(uname -s) $(uname -m)"
if [ "$KEPT" -gt 0 ]; then
  say "  forward   $KEPT argument(s) to scripts/serve_local.py:"
  show_args "$@"
else
  say "  forward   (no serve_local.py flags given)"
fi
# No secret can appear above: credentials are never flags in this project,
# they live in secrets.jsonc.

# ---------------------------------------------------------------------------
# P1 — prerequisites
# ---------------------------------------------------------------------------
step "P1  prerequisites"
command -v git >/dev/null 2>&1 || die "git is required. Install Xcode command line tools (xcode-select --install) or your distro's git package, then re-run."
say "  git       $(git --version)"

if command -v uv >/dev/null 2>&1; then
  say "  uv        $(uv --version)"
else
  say "  uv        not found — installing from https://astral.sh/uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # PATH in THIS shell is stale: the installer edited a profile we already
  # sourced. Re-probe the known location and use the absolute path from here.
  if [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
  elif command -v uv >/dev/null 2>&1; then
    UV="uv"
  else
    die "uv installed but is not on PATH and not at \$HOME/.local/bin/uv. Open a new terminal and re-run."
  fi
  say "  uv        $("$UV" --version)  ($UV)"
fi

if command -v claude >/dev/null 2>&1; then
  say "  claude    present"
else
  say "  claude    not found — the stack still runs; you just have no agent to"
  say "            connect yet. Install Claude Code, then run:"
  say "              claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp"
  SKIP_MCP=1
fi

# ---------------------------------------------------------------------------
# P2 — repo. Never clobber, never switch branch, never pull unless asked.
# ---------------------------------------------------------------------------
step "P2  repository"
TOP=""
if TOP=$(git rev-parse --show-toplevel 2>/dev/null) && [ -f "$TOP/scripts/serve_local.py" ]; then
  cd "$TOP"
  say "  using this checkout: $TOP"
elif [ -f "$DIR/scripts/serve_local.py" ]; then
  cd "$DIR"
  say "  using existing checkout: $(pwd)"
else
  say "  cloning $REPO_URL -> $DIR"
  git clone "$REPO_URL" "$DIR"
  cd "$DIR"
fi

# Print what you are actually about to run. A checkout parked on a fallback
# branch is a legitimate state -- the demo may depend on it -- so this reports
# rather than corrects.
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')
SHA=$(git rev-parse --short HEAD 2>/dev/null || echo '?')
say "  branch    $BRANCH  $SHA"

if [ "$DO_UPDATE" -eq 1 ]; then
  say "  updating (--update): git pull --ff-only on $BRANCH"
  git pull --ff-only
  say "  now at    $(git rev-parse --short HEAD)"
else
  say "  not pulling (pass --update if you want to)"
fi

# ---------------------------------------------------------------------------
# P3 — sync, then assert the one pin that silently ruins Windows-on-ARM
# ---------------------------------------------------------------------------
step "P3  dependencies"
"$UV" sync
FOUND_MCP=$("$UV" run python -c "import importlib.metadata as m; print(m.version('mcp'))")
if [ "$FOUND_MCP" != "$REQUIRED_MCP" ]; then
  die "mcp is $FOUND_MCP, expected $REQUIRED_MCP.
Do not upgrade mcp; re-sync from uv.lock:  $UV sync --frozen
Newer mcp pulls a cryptography release with no ARM64 Windows wheel, and the
build failure it produces reads like an unrelated Rust error."
fi
say "  mcp       $FOUND_MCP  (pinned)"

# ---------------------------------------------------------------------------
# P4 — secrets. Created from the template, NEVER overwritten, never echoed.
# ---------------------------------------------------------------------------
step "P4  credentials"
if [ -f secrets.jsonc ]; then
  say "  secrets.jsonc already exists — left untouched"
elif [ -f secrets.example.jsonc ]; then
  cp secrets.example.jsonc secrets.jsonc
  say "  created $(pwd)/secrets.jsonc from the template"
else
  say "  secrets.example.jsonc missing from this checkout — skipping"
fi
say "  Nothing in it is required for a first run: the stand-in arm needs no key."
say "  Paste real keys there only when you want --live or --distiller anthropic."

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [ -f secrets.jsonc ] && ! git check-ignore -q secrets.jsonc; then
    die "secrets.jsonc is NOT gitignored in this checkout. Refusing to continue —
restore the 'secrets.jsonc' line in .gitignore first, or you will commit a key."
  fi
else
  # A zip download has no .git. That is a legitimate way to get the code, and
  # aborting on it would be a false failure -- there is no index to commit to.
  say "  WARNING: not a git checkout (zip download?) — cannot verify that"
  say "           secrets.jsonc is ignored. Do not commit it."
fi

# ---------------------------------------------------------------------------
# P5 — MCP registration + awareness pack. Idempotent.
# ---------------------------------------------------------------------------
# --doctor-only is a machine INSPECTION. It must not register a server or
# copy a pack into ~/.claude as a side effect of someone asking "is this box
# ready?" -- that is a change, and they did not ask for one.
if [ "$DOCTOR_ONLY" -eq 1 ]; then
  step "P5  MCP registration — skipped (--doctor-only changes nothing)"
elif [ "$SKIP_MCP" -eq 1 ]; then
  step "P5  MCP registration — skipped (--skip-mcp)"
else
  step "P5  MCP registration"
  # OLDPWD is where the operator was standing before P2's cd. Defaulted for
  # `set -u`: under `curl | sh` it can be unset if no cd ever happened.
  [ -n "$PROJECT" ] || PROJECT=${OLDPWD:-$PWD}
  # Always YOUR OWN 127.0.0.1:8787, never the host's. One orchestrator per
  # laptop is what keeps attribution honest: point your agent at someone
  # else's and your findings are stamped with their contributor.
  MCP_URL="http://127.0.0.1:8787/mcp"
  if claude mcp list 2>/dev/null | grep -q '^synapse'; then
    say "  already registered — if /mcp shows it failed, pick Reconnect there"
  elif [ -d "$PROJECT" ] && [ "$(cd "$PROJECT" && pwd)" != "$(pwd)" ]; then
    ( cd "$PROJECT" && claude mcp add --transport http --scope project synapse "$MCP_URL" )
    say "  registered 'synapse' in $PROJECT"
  else
    say "  no project directory to register in (you are inside the Synapse"
    say "  checkout itself). From the project you want shared memory in, run:"
    say "    claude mcp add --transport http --scope project synapse $MCP_URL"
  fi

  # Awareness pack. A glob over whatever the pack actually ships, so a
  # commands/ or agents/ directory landing later needs no edit here.
  for KIND in skills commands agents; do
    SRC="packs/claude-code/$KIND"
    [ -d "$SRC" ] || continue
    DEST="$HOME/.claude/$KIND"
    mkdir -p "$DEST"
    for ENTRY in "$SRC"/*; do
      [ -e "$ENTRY" ] || continue
      NAME=$(basename "$ENTRY")
      if [ -e "$DEST/$NAME" ] && [ "$DO_UPDATE" -eq 0 ]; then
        say "  pack      $KIND/$NAME already installed — left alone (--update to replace)"
      else
        rm -rf "${DEST:?}/${NAME:?}"
        cp -R "$ENTRY" "$DEST/$NAME"
        say "  pack      $KIND/$NAME -> $DEST/$NAME"
      fi
    done
  done
  if [ -f packs/claude-code/settings-snippet.json ]; then
    say "  Optional hooks: merge packs/claude-code/settings-snippet.json into"
    say "  your ~/.claude/settings.json by hand — this script will not rewrite"
    say "  a settings file it does not own. See packs/claude-code/INSTALL.md."
  fi
fi

# ---------------------------------------------------------------------------
# P6 — port hygiene. Only ever under --clean.
# ---------------------------------------------------------------------------
if [ "$DO_CLEAN" -eq 1 ] && [ "$DOCTOR_ONLY" -eq 0 ]; then
  step "P6  clearing orphaned processes (--clean)"
  pkill -f synapse-service || true
  pkill -f synapse-orchestrator || true
  pkill -f local_model_server || true
  say "  done"
fi

# ---------------------------------------------------------------------------
# P8 — doctor. Runs BEFORE anything starts, and its output is teed, because
# the Unicode canary only proves something through a redirection.
# ---------------------------------------------------------------------------
step "P8  pre-flight doctor"
mkdir -p .synapse/logs
DOCTOR_RC=0
"$UV" run python scripts/doctor.py 2>&1 | tee .synapse/logs/doctor.log || DOCTOR_RC=1
# `set -o pipefail` is a bashism we cannot use, so read the doctor's verdict
# back out of the log it just wrote instead of trusting the pipeline's status.
if grep -q '^FAIL' .synapse/logs/doctor.log; then
  DOCTOR_RC=1
fi
say ""
say "  full log: $(pwd)/.synapse/logs/doctor.log"

if [ "$DOCTOR_ONLY" -eq 1 ]; then
  say ""
  say "--doctor-only: nothing was started."
  exit "$DOCTOR_RC"
fi

if [ "$DOCTOR_RC" -ne 0 ] && [ "$FORCE" -eq 0 ]; then
  die "the doctor reported a FAIL (above). Fix it, or re-run with --force."
fi

# ---------------------------------------------------------------------------
# P7 — start, in the foreground, so Ctrl-C works and the banner is live.
# ---------------------------------------------------------------------------
if [ "$NO_START" -eq 1 ]; then
  step "P7  start — skipped (--no-start)"
  say "  would have exec'd: $UV run python scripts/serve_local.py"
  show_args "$@"
  exit 0
fi

step "P7  starting Synapse"
if [ "$JOINING" -eq 1 ]; then
  say "  joining a service someone else is hosting"
fi
say ""
exec "$UV" run python scripts/serve_local.py "$@"
