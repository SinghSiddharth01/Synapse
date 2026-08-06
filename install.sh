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
# from it. Every interactive decision is a flag instead. That is only half the
# guarantee: a CHILD that reads stdin eats the rest of THIS SCRIPT out of the
# pipe, and the shell then dies mid-file with "unexpected EOF" having skipped
# every line the child swallowed. So every child below is run with
# `< /dev/null`, and git is told never to prompt.
set -eu
GIT_TERMINAL_PROMPT=0
export GIT_TERMINAL_PROMPT
# Every `cd` below means "this literal path", never "search CDPATH for it".
unset CDPATH 2>/dev/null || true

REPO_URL="https://github.com/SinghSiddharth01/Synapse.git"
RAW_URL="https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main"
# Kept in lockstep with packages/orchestrator/pyproject.toml:12 ("mcp==1.9.4")
# and scripts/doctor.py:REQUIRED_MCP. Three copies is two too many, but the
# installer must be able to say the number before a venv exists to ask.
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
  --update          git pull --ff-only, and replace already-installed awareness
                    pack entries (the old copy is moved aside, not deleted).
                    The ONLY flag that pulls. Never switches branch.
  --clean           kill orphaned synapse processes before starting
  --project <path>  where to run 'claude mcp add' (default: your cwd, unless
                    that is inside the Synapse checkout)
  --doctor-only     inspect this machine, then stop. Clones and syncs if there
                    is nothing to inspect yet, but creates no secrets.jsonc,
                    registers no MCP server, copies no pack, writes no log
                    into the checkout, and starts nothing.
  --no-start        do everything except start the stack
  --skip-mcp        do not register the MCP server or copy the awareness pack
  --force           start even if the doctor reports a FAIL, and downgrade the
                    mcp version mismatch from an abort to a warning
  --                end of installer flags; everything after it is forwarded
                    verbatim, even if it looks like one of the above
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
#
# One hazard the rotation cannot see on its own: an installer-consumed flag
# sitting where a pass-through flag wanted its VALUE. `--purpose --update` is
# not "purpose, then update" -- it is a missing purpose -- but a flat scan
# consumes --update and forwards a bare --purpose. The installer is forbidden
# to enumerate serve_local.py's flags (that is the whole point of the
# pass-through rule), so it cannot know which take a value; instead it NOTICES
# the shape and says so, and offers `--` for the case where the shape is
# intended. PREV_KEPT is the last token that was forwarded.
# ---------------------------------------------------------------------------
ARGC=$#
KEPT=0
PREV_KEPT=""
ENDOPTS=0
while [ "$ARGC" -gt 0 ]; do
  ARG=$1
  shift
  ARGC=$((ARGC - 1))
  if [ "$ENDOPTS" -eq 1 ]; then
    case "$ARG" in --service-url|--service-url=*) JOINING=1 ;; esac
    set -- "$@" "$ARG"
    KEPT=$((KEPT + 1))
    continue
  fi
  case "$ARG" in
    --dir|--project|--update|--clean|--doctor-only|--no-start|--skip-mcp|--force)
      case "$PREV_KEPT" in
        --*=*|"") ;;
        --*) say "  WARNING: '$ARG' was consumed by the installer, but it follows"
             say "           '$PREV_KEPT', which may have wanted it as a value. Put"
             say "           installer flags first, or use -- to force pass-through:"
             say "             ./install.sh $ARG -- $PREV_KEPT <value>" ;;
      esac ;;
  esac
  case "$ARG" in
    --)        ENDOPTS=1 ;;
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
      PREV_KEPT=$ARG
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

# An absolute $UV is not enough. `uv run` prepends ONLY <repo>/.venv/bin to the
# child's PATH, so scripts/doctor.py's check 1 (`shutil.which("uv")`) reports
# "FAIL uv not on PATH" on the very box this installer just provisioned -- and
# P8's `grep '^FAIL'` then aborts the run over its own success. Put uv's own
# directory on PATH for every child from here on.
UV_BIN=$(command -v "$UV" 2>/dev/null || printf '%s' "$UV")
case "$UV_BIN" in
  */*) UV_DIR=$(cd -- "$(dirname -- "$UV_BIN")" && pwd)
       case ":$PATH:" in
         *":$UV_DIR:"*) ;;
         *) PATH="$UV_DIR:$PATH"; export PATH
            say "  path      added $UV_DIR to PATH for this run" ;;
       esac ;;
esac

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
  git clone "$REPO_URL" "$DIR" < /dev/null
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
  git pull --ff-only < /dev/null
  say "  now at    $(git rev-parse --short HEAD)"
else
  say "  not pulling (pass --update if you want to)"
fi

# ---------------------------------------------------------------------------
# P3 — sync, then assert the one pin that silently ruins Windows-on-ARM
# ---------------------------------------------------------------------------
step "P3  dependencies"
"$UV" sync < /dev/null
# The probe must never raise. `m.version('mcp')` on a venv where mcp did not
# install throws PackageNotFoundError; under `set -eu` that traceback killed
# the script at this line and the carefully worded `die` below -- the only
# place that says "do not upgrade mcp" -- never printed.
FOUND_MCP=$("$UV" run python -c '
import importlib.metadata as m
try:
    print(m.version("mcp"))
except Exception:
    print("absent")
' < /dev/null 2>/dev/null) || FOUND_MCP="absent"
[ -n "$FOUND_MCP" ] || FOUND_MCP="absent"
if [ "$FOUND_MCP" != "$REQUIRED_MCP" ]; then
  MCP_MSG="mcp is $FOUND_MCP, expected $REQUIRED_MCP.
Do not upgrade mcp; re-sync from uv.lock:  $UV sync --frozen
Newer mcp pulls a cryptography release with no ARM64 Windows wheel, and the
build failure it produces reads like an unrelated Rust error."
  # --force is documented as "start even if the doctor reports a FAIL". The
  # doctor reports this same mismatch as a FAIL, so an ungated abort here made
  # --force unable to do the one thing it promises.
  if [ "$FORCE" -eq 1 ]; then
    say "  WARNING (--force): $MCP_MSG"
  else
    die "$MCP_MSG"
  fi
else
  say "  mcp       $FOUND_MCP  (pinned)"
fi

# ---------------------------------------------------------------------------
# P4 — secrets. Created from the template, NEVER overwritten, never echoed.
# ---------------------------------------------------------------------------
step "P4  credentials"
if [ -f secrets.jsonc ]; then
  say "  secrets.jsonc already exists — left untouched"
elif [ "$DOCTOR_ONLY" -eq 1 ]; then
  # --doctor-only is an INSPECTION. Creating the file here would answer the
  # doctor's own question before it is asked: check 6 reports "secrets.jsonc
  # absent" as a WARN, and that WARN was unreachable through the installer
  # because P4 had already made it false. An inspection that changes what it
  # inspects is not one.
  say "  secrets.jsonc absent — NOT creating it (--doctor-only changes nothing)"
  say "  the doctor below reports the real state of this machine"
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
# P6 — port hygiene. Only ever under --clean. Before the doctor, so the
# doctor's port check reports the state --clean actually left behind.
# ---------------------------------------------------------------------------
if [ "$DO_CLEAN" -eq 1 ] && [ "$DOCTOR_ONLY" -eq 0 ]; then
  step "P6  clearing orphaned processes (--clean)"
  pkill -f synapse-service || true
  pkill -f synapse-orchestrator || true
  pkill -f local_model_server || true
  # These three patterns are Synapse's own processes and nothing else's. The
  # model seam on 18181 can equally be held by `geniex serve` (see
  # scripts/serve_local.py:68), which --clean deliberately does NOT kill: an
  # NPU server is somebody's deliberate setup, not an orphan. So report what
  # survived rather than leaving the operator to discover it at bind time.
  if command -v lsof >/dev/null 2>&1; then
    for PORT in 8787 8899 18181; do
      HOLDER=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | tr '\n' ' ')
      [ -n "$HOLDER" ] || continue
      say "  still held: $PORT by pid ${HOLDER% }  (--clean does not kill"
      say "              non-Synapse holders such as \`geniex serve\`)"
    done
  fi
  say "  done"
fi

# ---------------------------------------------------------------------------
# P8 — doctor. Runs BEFORE anything is started AND before P5 mutates
# ~/.claude, so a box with the wrong interpreter or the wrong mcp is not
# half-configured on the way to failing. Its output is teed, because the
# Unicode canary only proves something through a redirection.
# ---------------------------------------------------------------------------
step "P8  pre-flight doctor"
DOCTOR_RC_FILE=$(mktemp "${TMPDIR:-/tmp}/synapse-doctor-rc.XXXXXX")
printf '0\n' > "$DOCTOR_RC_FILE"
if [ "$DOCTOR_ONLY" -eq 1 ]; then
  # An inspection writes nothing into the checkout. The tee still happens --
  # it is the redirection that gives check 4 its meaning -- just into a temp
  # file instead of creating .synapse/logs/ on a machine nobody asked to change.
  DOCTOR_LOG=$(mktemp "${TMPDIR:-/tmp}/synapse-doctor.XXXXXX")
else
  mkdir -p .synapse/logs
  DOCTOR_LOG=".synapse/logs/doctor.log"
fi
# `set -o pipefail` is a bashism we cannot use, and grepping the log for
# '^FAIL' misses every way the doctor can fail WITHOUT printing a FAIL line --
# an unhandled traceback, an OSError from the port probe, an ImportError in
# the venv. So record the real exit status out of the pipeline and use it.
# `|| echo` keeps `set -e` out of this: the status is data, not a failure.
{ "$UV" run python scripts/doctor.py < /dev/null 2>&1 || echo "$?" > "$DOCTOR_RC_FILE"; } \
  | tee "$DOCTOR_LOG"
DOCTOR_RC=$(cat "$DOCTOR_RC_FILE" 2>/dev/null || echo 1)
rm -f "$DOCTOR_RC_FILE"
case "$DOCTOR_RC" in ''|*[!0-9]*) DOCTOR_RC=1 ;; esac
if [ "$DOCTOR_RC" -ne 0 ]; then
  say ""
  say "  the doctor exited $DOCTOR_RC"
fi
# Belt and braces: a FAIL line with a zero exit would be a doctor bug, and
# this is the cheaper half of catching it.
if grep -q '^FAIL' "$DOCTOR_LOG"; then
  DOCTOR_RC=1
fi
say ""
if [ "$DOCTOR_ONLY" -eq 1 ]; then
  say "  full log: $DOCTOR_LOG  (temporary — --doctor-only writes nothing here)"
else
  say "  full log: $(pwd)/$DOCTOR_LOG"
fi

if [ "$DOCTOR_ONLY" -eq 1 ]; then
  say ""
  say "--doctor-only: nothing was started, nothing was registered, nothing was"
  say "written into this checkout."
  exit "$DOCTOR_RC"
fi

if [ "$DOCTOR_RC" -ne 0 ] && [ "$FORCE" -eq 0 ]; then
  die "the doctor reported a FAIL (above). Fix it, or re-run with --force."
fi

# A held port is a WARN, not a FAIL, and correctly so -- a host already
# running is a normal state. But serve_local.py's claim_ports
# (scripts/serve_local.py:159-189) does NOT treat it as normal: it SystemExits
# on the first port it cannot bind. Without this line the operator reads
# "0 fail", the installer proceeds, and the stack dies seconds later on
# something the doctor already knew and graded as harmless.
if grep -q '^WARN  ports' "$DOCTOR_LOG"; then
  say ""
  say "  NOTE: the doctor WARNed about a held port. serve_local.py will REFUSE"
  say "        to start on one it cannot bind — which ports it needs depends on"
  say "        your flags (--service-url drops 8899; --listen/--npu drop 18181;"
  say "        see scripts/serve_local.py:277-278). If P7 below exits with"
  say "        \"ports already in use\", re-run with --clean."
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
  REPO_ABS=$(pwd)
  PROJECT_ABS=$(cd -- "$PROJECT" 2>/dev/null && pwd) || PROJECT_ABS=""
  # `!= "$(pwd)"` was an equality test, and the case it had to catch is
  # containment: run the one-liner from $HOME and the clone lands in
  # $HOME/Synapse, so OLDPWD ($HOME) is not equal to the checkout but every
  # `docs/`, `packs/`, `scripts/` under it IS inside it. Registering a
  # project-scoped server in a subdirectory of the Synapse checkout writes a
  # .mcp.json into the repo the operator is about to `git status`.
  INSIDE_CHECKOUT=0
  case "$PROJECT_ABS" in
    "$REPO_ABS"|"$REPO_ABS"/*) INSIDE_CHECKOUT=1 ;;
    "") INSIDE_CHECKOUT=1 ;;
  esac
  # Always YOUR OWN 127.0.0.1:8787, never the host's. One orchestrator per
  # laptop is what keeps attribution honest: point your agent at someone
  # else's and your findings are stamped with their contributor.
  MCP_URL="http://127.0.0.1:8787/mcp"
  if [ "$INSIDE_CHECKOUT" -eq 1 ]; then
    say "  no project directory to register in (you are inside the Synapse"
    say "  checkout itself, or it no longer exists). From the project you want"
    say "  shared memory in, run:"
    say "    claude mcp add --transport http --scope project synapse $MCP_URL"
  # --scope project is per-directory: the registration lives in $PROJECT's own
  # .mcp.json. Asking `claude mcp list` from the Synapse checkout therefore
  # answered a question about the WRONG directory, and the idempotency skip
  # never fired for anyone whose project was somewhere else. Ask from there.
  elif ( cd "$PROJECT_ABS" && claude mcp list 2>/dev/null ) | grep -q '^synapse'; then
    say "  already registered in $PROJECT_ABS — if /mcp shows it failed, pick"
    say "  Reconnect there"
  else
    ( cd "$PROJECT_ABS" && claude mcp add --transport http --scope project synapse "$MCP_URL" < /dev/null )
    say "  registered 'synapse' in $PROJECT_ABS"
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
        # Move aside, never delete. What sits at $DEST/$NAME may be a skill the
        # operator edited by hand; --update is documented as "git pull
        # --ff-only", and an unprompted, unbacked-up rm -rf under that
        # description is a surprise nobody consented to.
        if [ -e "$DEST/$NAME" ]; then
          BAK="$DEST/$NAME.synapse-bak.$(date +%Y%m%d%H%M%S)"
          mv -- "$DEST/$NAME" "$BAK"
          say "  pack      previous $KIND/$NAME moved aside -> $BAK"
        fi
        cp -R "$ENTRY" "$DEST/$NAME"
        say "  pack      $KIND/$NAME -> $DEST/$NAME"
      fi
    done
  done
  say "  the doctor's 'awareness' line above described this machine BEFORE the"
  say "  copy; re-run './install.sh --doctor-only' to see it settle."
  if [ -f packs/claude-code/settings-snippet.json ]; then
    say "  Optional hooks: merge packs/claude-code/settings-snippet.json into"
    say "  your ~/.claude/settings.json by hand — this script will not rewrite"
    say "  a settings file it does not own. See packs/claude-code/INSTALL.md."
    # The snippet's command is
    #   python3 "$CLAUDE_PROJECT_DIR/.claude/synapse-pack/hooks/freshness_pointer.py"
    # and NOTHING above puts a file there: the pack loop copies skills/,
    # commands/ and agents/ into ~/.claude, and hooks/ is neither. Merging the
    # snippet alone yields a hook that cannot find its script on every prompt.
    if [ -d packs/claude-code/hooks ]; then
      say "  The snippet also needs the hook script itself, per project:"
      say "    mkdir -p <your-project>/.claude/synapse-pack"
      say "    cp -R $(pwd)/packs/claude-code/hooks <your-project>/.claude/synapse-pack/"
    fi
  fi
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
