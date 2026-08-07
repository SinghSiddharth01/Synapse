#!/bin/sh
# Synapse installer — macOS / Linux. INSTALLS AND NOTHING ELSE.
#
#   client:  curl -LsSf https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.sh | sh
#   server:  curl -LsSf https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.sh | sh -s -- server
#
# The lifecycle is three separate stages, and this script is ONLY the first:
#
#   install     this script — puts the `synapse` (or `synapse-server`)
#               command on the machine. Writes no config, asks no questions,
#               registers nothing, STARTS NOTHING.
#   configure   `synapse configure` / `synapse config set` — service URL,
#               contributor, keys file. Re-runnable, one key at a time.
#   run         `synapse up` / `synapse-server up` — starts the services, in
#               the foreground, only when you ask. Nothing is a daemon.
#
# No git, no clone: the wheels come from the GitHub Release bundle built by
# CI (.github/workflows/release.yml). Inside a checkout the wheels are built
# from the tree instead, so this same script serves developers.
#
# POSIX sh, deliberately: runs under dash. Safe under `curl … | sh`: stdin is
# the script, nothing here reads from it, and every child runs < /dev/null.
set -eu
unset CDPATH 2>/dev/null || true

REPO_SLUG="SinghSiddharth01/Synapse"
CLIENT_ASSET="synapse-client-wheels.zip"
SERVER_ASSET="synapse-server-wheels.zip"
MIN_UV_MINOR=6            # warn below uv 0.6 — old uvs mishandle workspaces

COMPONENT="client"
TAG=""
LOCAL_DIR=""
DO_UPDATE=0
KEEP_CONFIG=0
UV="uv"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<USAGE
Synapse installer — installs, and only installs.

  install.sh [client|server|uninstall] [options]

  client (default)  the 'synapse' CLI: orchestrator, MCP server, edge worker
  server            the 'synapse-server' CLI: the shared context service
  uninstall         stop Synapse processes, remove both installed halves AND
                    ~/.synapse (config + state) — a reinstall that inherits a
                    stale service.url is worse than re-running configure.
                    Pack/MCP leftovers are listed, never deleted.

Options:
  --tag <tag>    install this release instead of the latest
  --local <dir>  install from a local bundle/checkout instead of GitHub
  --update       reinstall over an existing install (picks up new versions)
  --keep-config  (uninstall) preserve ~/.synapse for a later reinstall
  -h, --help     this

One-liners (no clone, no flags — install is just install):
  curl -LsSf https://raw.githubusercontent.com/$REPO_SLUG/main/install.sh | sh
  curl -LsSf https://raw.githubusercontent.com/$REPO_SLUG/main/install.sh | sh -s -- server

Windows (PowerShell):
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/$REPO_SLUG/main/install.ps1)))
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/$REPO_SLUG/main/install.ps1))) -Component server

After installing:
  synapse configure           # client: service URL (ping-tested), contributor
  synapse-server configure    # server: API keys file (one key per line), model
  synapse up | synapse-server up      # start things — only ever on demand
  synapse health | synapse-server health
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    client|server|uninstall) COMPONENT=$1 ;;
    --tag)    [ $# -gt 1 ] || die "--tag needs a value";  TAG=$2; shift ;;
    --local)  [ $# -gt 1 ] || die "--local needs a path"; LOCAL_DIR=$2; shift ;;
    --update) DO_UPDATE=1 ;;
    --keep-config) KEEP_CONFIG=1 ;;
    -h|--help) usage; exit 0 ;;
    --service-url|--shared-id|--purpose|--contributor)
      die "'$1' is not an installer flag any more. Install and configuration
are separate stages now:
  1) finish this install (no flags needed)
  2) synapse config set service.url http://<host>:8899
  3) synapse up --shared-id <id>        # joining happens at RUN time" ;;
    *) die "unknown flag '$1' (see --help)" ;;
  esac
  shift
done

[ "$KEEP_CONFIG" -eq 1 ] && [ "$COMPONENT" != "uninstall" ] && \
  die "--keep-config only applies to 'uninstall'"

# ---------------------------------------------------------------------------
# U — uninstall: the mirror of install, plus config removal. In the INSTALLER
# and not the CLI so it works when the installed CLI is broken (the decided
# shape, docs/plans/2026-08-06-uninstall-mechanism.md). One amendment to that
# plan (2026-08-06, by the owner): ~/.synapse is REMOVED by default — after
# `uv tool uninstall`, a reinstall silently inherited the previous machine's
# service.url from the surviving config, which is exactly the class of stale
# state an uninstall exists to end. --keep-config preserves it for upgrades.
# ---------------------------------------------------------------------------
if [ "$COMPONENT" = "uninstall" ]; then
  say "Synapse uninstaller"
  SYN_HOME="${SYNAPSE_HOME:-$HOME/.synapse}"

  step "U1  stop running Synapse processes"
  # By OUR ports only (8787 orchestrator, 8790 worker dashboard, 8899 local
  # service) — never by name-substring, which could match an editor with a
  # synapse file open, and never geniex: the model seam is not ours to stop.
  STOPPED=0
  if command -v lsof >/dev/null 2>&1; then
    for SPEC in "8787 orchestrator" "8790 edge-worker" "8899 service"; do
      PORT=${SPEC%% *}; LABEL=${SPEC#* }
      PIDS=$(lsof -ti "tcp:$PORT" 2>/dev/null || true)
      if [ -n "$PIDS" ]; then
        # shellcheck disable=SC2086
        kill $PIDS 2>/dev/null || true
        say "  stopped   $LABEL (:$PORT)"
        STOPPED=1
      fi
    done
    [ "$STOPPED" -eq 0 ] && say "  nothing running on :8787/:8790/:8899"
  else
    say "  lsof not available — if 'synapse up' or 'synapse-server up' is"
    say "  running, stop it with Ctrl-C; this script will not guess at PIDs."
  fi

  step "U2  remove installed tools"
  REMOVED=0
  # ANSI is stripped before matching. `uv tool list` colours its output, and
  # the anchored ^ below cannot match past an escape sequence: the line
  # arrives as ESC[1msynapse-cli v0.1.0ESC[0m and the tool is never seen.
  # Reproduced on the PowerShell side 2026-08-07, where this exact pattern
  # reported "no synapse uv tools installed" against two installed tools,
  # removed nothing, and still printed "Uninstalled."
  #
  # uv drops colour when it detects a pipe, so this path is usually already
  # plain -- but "usually" is doing load-bearing work there: CLICOLOR_FORCE,
  # a future default, or a wrapper on PATH each put it back, and the failure
  # is silent success. Costs one sed; removes the assumption.
  # CSI is the whole ESC-bracket prefix, built with printf so that neither the
  # escape byte nor a doubled bracket appears literally in this file: the
  # POSIX check forbids `[[` (a bashism) and would flag `\[[0-9;]*m`.
  CSI=$(printf '\033[')
  if command -v "$UV" >/dev/null 2>&1; then
    for TOOL in synapse-cli synapse-service; do
      if "$UV" tool list 2>/dev/null | sed "s/${CSI}[0-9;]*m//g" | grep -q "^$TOOL "; then
        "$UV" tool uninstall "$TOOL" < /dev/null
        say "  removed   $TOOL"
        REMOVED=1
      fi
    done
  fi
  [ "$REMOVED" -eq 0 ] && say "  no synapse uv tools installed — nothing to remove"

  step "U3  config and state"
  if [ -d "$SYN_HOME" ]; then
    if [ "$KEEP_CONFIG" -eq 1 ]; then
      say "  kept      $SYN_HOME (--keep-config)"
    else
      say "  removing  $SYN_HOME — config.toml, session bindings, the"
      say "            write-ahead log, and logs go with it. (--keep-config"
      say "            preserves it for a reinstall/upgrade.)"
      rm -rf "$SYN_HOME"
      say "  removed   $SYN_HOME"
    fi
  else
    say "  no $SYN_HOME — nothing to remove"
  fi

  step "U4  left in place — yours, listed with the removal command"
  CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  LEFT=0
  for KIND in skills commands agents; do
    for ENTRY in "$CLAUDE_DIR/$KIND"/synapse*; do
      [ -e "$ENTRY" ] || continue
      say "  pack      $ENTRY"
      say "            rm -r '$ENTRY'"
      LEFT=1
    done
  done
  if command -v claude >/dev/null 2>&1; then
    say "  mcp       if 'synapse' is registered (user scope or per project):"
    say "            claude mcp remove synapse"
    LEFT=1
  fi
  [ "$LEFT" -eq 0 ] && say "  none found"

  say ""
  say "Uninstalled."
  exit 0
fi

say "Synapse installer — $COMPONENT"
say "  os        $(uname -s) $(uname -m)"
say "  this script only installs. It will not ask questions, write config,"
say "  or start anything."

# ---------------------------------------------------------------------------
# P1 — prerequisites. Use what exists; install what is missing; warn on old.
# ---------------------------------------------------------------------------
step "P1  prerequisites"

if command -v uv >/dev/null 2>&1; then
  UV_VERSION=$(uv --version 2>/dev/null | awk '{print $2}')
  say "  uv        $UV_VERSION (already installed — using it)"
  UV_MINOR=$(printf '%s' "$UV_VERSION" | awk -F. '{print $2}')
  case "$UV_MINOR" in
    ''|*[!0-9]*) ;;
    *) if [ "${UV_VERSION%%.*}" -eq 0 ] && [ "$UV_MINOR" -lt "$MIN_UV_MINOR" ]; then
         say "  WARNING: uv $UV_VERSION is older than 0.$MIN_UV_MINOR — consider 'uv self update'"
       fi ;;
  esac
else
  say "  uv        not found — installing from https://astral.sh/uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
  elif command -v uv >/dev/null 2>&1; then
    UV="uv"
  else
    die "uv installed but not on PATH and not at \$HOME/.local/bin/uv. Open a new terminal and re-run."
  fi
  say "  uv        $("$UV" --version | awk '{print $2}')  ($UV)"
fi
# Put uv's own directory on PATH for every child of this run.
UV_BIN=$(command -v "$UV" 2>/dev/null || printf '%s' "$UV")
case "$UV_BIN" in
  */*) UV_DIR=$(cd -- "$(dirname -- "$UV_BIN")" && pwd)
       case ":$PATH:" in
         *":$UV_DIR:"*) ;;
         *) PATH="$UV_DIR:$PATH"; export PATH ;;
       esac ;;
esac

if PY=$("$UV" python find '>=3.12' 2>/dev/null); then
  say "  python    $PY (already present — using it)"
else
  say "  python    no 3.12+ found — uv will download one (managed, isolated)"
  "$UV" python install 3.12 < /dev/null
  say "  python    $("$UV" python find '>=3.12')"
fi

if [ "$COMPONENT" = "client" ]; then
  if command -v claude >/dev/null 2>&1; then
    say "  claude    present (needed later for MCP registration / the claude-cli arm)"
  else
    say "  claude    not found — fine for install; 'synapse configure' will say"
    say "            what to do when an agent needs to connect"
  fi
  # Hardware-dependent, deliberately: GenieX serves the Snapdragon X Elite
  # NPU, which is Windows-on-ARM hardware. On macOS/Linux there is nothing to
  # install — the client uses the claude-cli / anthropic / listen arms here.
  case "$(uname -s)" in
    Darwin|Linux)
      say "  geniex    skipped — NPU distillation (GenieX) is Snapdragon/Windows"
      say "            hardware; this machine uses claude-cli / anthropic / listen" ;;
  esac
fi

# ---------------------------------------------------------------------------
# P2 — obtain wheels: a local dir, this checkout, or the GitHub Release.
# ---------------------------------------------------------------------------
step "P2  packages"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/synapse-install.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
WHEELS=""

SCRIPT_DIR=""
case "$0" in
  */*) SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || SCRIPT_DIR="" ;;
esac

if [ -n "$LOCAL_DIR" ]; then
  [ -d "$LOCAL_DIR" ] || die "--local $LOCAL_DIR is not a directory"
  if ls "$LOCAL_DIR"/synapse_*.whl >/dev/null 2>&1; then
    WHEELS="$LOCAL_DIR"
    say "  using wheels already in $LOCAL_DIR"
  elif [ -f "$LOCAL_DIR/pyproject.toml" ]; then
    say "  building wheels from the checkout at $LOCAL_DIR"
    (cd "$LOCAL_DIR" && "$UV" build --all-packages --out-dir "$TMP/wheels" < /dev/null)
    WHEELS="$TMP/wheels"
  else
    die "--local $LOCAL_DIR holds neither wheels nor a checkout"
  fi
elif [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -f "$SCRIPT_DIR/scripts/serve_local.py" ]; then
  # Running from inside a checkout (developer path): build from the tree —
  # what you install is what you are looking at, not last week's release.
  say "  building wheels from this checkout ($SCRIPT_DIR)"
  (cd "$SCRIPT_DIR" && "$UV" build --all-packages --out-dir "$TMP/wheels" < /dev/null)
  WHEELS="$TMP/wheels"
else
  if [ "$COMPONENT" = "server" ]; then ASSET="$SERVER_ASSET"; else ASSET="$CLIENT_ASSET"; fi
  if [ -n "$TAG" ]; then
    URL="https://github.com/$REPO_SLUG/releases/download/$TAG/$ASSET"
  else
    URL="https://github.com/$REPO_SLUG/releases/latest/download/$ASSET"
  fi
  say "  downloading $URL"
  if ! curl -fLsS -o "$TMP/$ASSET" "$URL" < /dev/null; then
    die "could not download $ASSET. Either no release is published yet (run the
'release' GitHub Actions workflow, or push a v* tag), or this machine is
offline. From a checkout, './install.sh $COMPONENT' builds locally instead."
  fi
  mkdir -p "$TMP/wheels"
  if command -v unzip >/dev/null 2>&1; then
    unzip -q "$TMP/$ASSET" -d "$TMP/wheels"
  else
    PYBIN=$("$UV" python find '>=3.12')
    "$PYBIN" -m zipfile -e "$TMP/$ASSET" "$TMP/wheels"
  fi
  WHEELS="$TMP/wheels"
  [ -f "$WHEELS/VERSION" ] && say "  bundle    $(cat "$WHEELS/VERSION")"
fi

# ---------------------------------------------------------------------------
# P3 — install. `uv tool install` gives each half its own isolated venv and a
# command on PATH; local wheels satisfy the synapse-* pins, PyPI the rest.
# ---------------------------------------------------------------------------
step "P3  install"
if [ "$COMPONENT" = "server" ]; then TOOL_GLOB="synapse_service"; TOOL="synapse-service"
else TOOL_GLOB="synapse_cli"; TOOL="synapse-cli"; fi
TOOL_WHEEL=""
for WHEEL in "$WHEELS/$TOOL_GLOB"-*.whl; do
  [ -e "$WHEEL" ] || continue
  TOOL_WHEEL=$WHEEL
  break
done
[ -n "$TOOL_WHEEL" ] || die "no $TOOL wheel in $WHEELS — wrong bundle for '$COMPONENT'?"
# Every synapse-* dependency is pinned to the bundle's own wheel by explicit
# path (--with <file>), so nothing that happens to share a name on PyPI can
# ever shadow the release. PyPI serves only the third-party deps. The list is
# filtered per component — a checkout build produces EVERY wheel, and the
# client env must not quietly grow the service (or vice versa): the halves
# stay decoupled precisely because neither can accidentally contain the other.
if [ "$COMPONENT" = "server" ]; then
  KEEP="synapse_contracts synapse_providers"
else
  KEEP="synapse_contracts synapse_providers synapse_distiller synapse_worker synapse_orchestrator"
fi
WITH_ARGS=""
for WHEEL in "$WHEELS"/synapse_*.whl; do
  [ "$WHEEL" = "$TOOL_WHEEL" ] && continue
  BASE=$(basename "$WHEEL")
  WANTED=0
  for NAME in $KEEP; do
    case "$BASE" in "$NAME"-*) WANTED=1 ;; esac
  done
  [ "$WANTED" -eq 1 ] && WITH_ARGS="$WITH_ARGS --with $WHEEL"
done
REINSTALL=""
[ "$DO_UPDATE" -eq 1 ] && REINSTALL="--reinstall"
# shellcheck disable=SC2086
"$UV" tool install $REINSTALL $WITH_ARGS "$TOOL_WHEEL" < /dev/null
say "  installed $TOOL into an isolated environment"

# ---------------------------------------------------------------------------
# P4 — verify, then say what the NEXT stage is (without doing it).
# ---------------------------------------------------------------------------
step "P4  verify"
if [ "$COMPONENT" = "server" ]; then CMD="synapse-server"; else CMD="synapse"; fi
if command -v "$CMD" >/dev/null 2>&1; then
  say "  $CMD is on PATH"
else
  say "  $CMD is installed but not on PATH yet. Run:"
  say "    $UV tool update-shell"
  say "  then open a new terminal. (uv puts tools in ~/.local/bin.)"
fi

say ""
say "Installed. Nothing was configured and nothing was started. Next:"
if [ "$COMPONENT" = "server" ]; then
  say "  synapse-server configure --keys /path/to/keys.txt   # one API key per line"
  say "  synapse-server up        # health-checks the keys, then serves (Ctrl-C stops)"
  say "  synapse-server health    # configuration + key check, any time"
else
  say "  synapse configure        # service URL (ping-tested), contributor, distiller"
  say "  synapse up               # start orchestrator + worker (Ctrl-C stops)"
  say "  synapse health           # what is configured, what is running"
fi
