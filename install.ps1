# Synapse installer -- Windows (PowerShell). INSTALLS AND NOTHING ELSE.
#
#   client:  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.ps1)))
#   server:  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.ps1))) -Component server
#
# The lifecycle is three separate stages, and this script is ONLY the first:
#
#   install     this script -- puts the `synapse` (or `synapse-server`)
#               command on the machine. Writes no config, asks no questions,
#               registers nothing, STARTS NOTHING.
#   configure   `synapse configure` / `synapse config set` -- service URL,
#               contributor, keys file. Re-runnable, one key at a time.
#   run         `synapse up` / `synapse-server up` -- starts the services, in
#               the foreground, only when you ask. Nothing is a daemon.
#
# No git, no clone: wheels come from the GitHub Release bundle built by CI
# (.github/workflows/release.yml). Inside a checkout the wheels are built
# from the tree instead, so this same script serves developers.
#
# The `[scriptblock]::Create((irm ...))` wrapper is what lets -Component
# through the pipe; `irm ... | iex` cannot take arguments.

[CmdletBinding()]
param(
    [ValidateSet('client', 'server', 'uninstall')]
    [string]$Component = 'client',
    [string]$Tag = '',
    [string]$Local = '',
    [switch]$Update,
    [switch]$KeepConfig
)

$ErrorActionPreference = 'Stop'
$RepoSlug = 'SinghSiddharth01/Synapse'

function Say([string]$Line) { Write-Host $Line }
function Step([string]$Line) { Write-Host "`n==> $Line" }
function Die([string]$Line) { Write-Error "install.ps1: $Line"; exit 1 }

if ($KeepConfig -and $Component -ne 'uninstall') {
    Die "-KeepConfig only applies to -Component uninstall"
}

# ---------------------------------------------------------------------------
# U -- uninstall: the mirror of install, plus config removal. Same shape and
# same amendment as install.sh's uninstall (see that script and
# docs/plans/2026-08-06-uninstall-mechanism.md): the state dir is REMOVED by
# default -- a reinstall that silently inherits a stale service.url from a
# surviving config.toml is the failure this exists to end. -KeepConfig
# preserves it for an upgrade. Pack/MCP entries are listed, never deleted.
# ---------------------------------------------------------------------------
if ($Component -eq 'uninstall') {
    Say "Synapse uninstaller"
    $SynHome = if ($env:SYNAPSE_HOME) { $env:SYNAPSE_HOME } else { Join-Path $env:USERPROFILE '.synapse' }

    Step "U1  stop running Synapse processes"
    # By OUR ports only (8787 orchestrator, 8790 worker dashboard, 8899 local
    # service) -- never by name, and never geniex (:18181).
    $stopped = $false
    foreach ($spec in @(@(8787, 'orchestrator'), @(8790, 'edge-worker'), @(8899, 'service'))) {
        $port, $label = $spec
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($pid_ in ($conns | Select-Object -ExpandProperty OwningProcess -Unique)) {
            Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
            Say "  stopped   $label (:$port)"
            $stopped = $true
        }
    }
    if (-not $stopped) { Say "  nothing running on :8787/:8790/:8899" }

    Step "U2  remove installed tools"
    $removed = $false
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        # ANSI IS STRIPPED BEFORE MATCHING. `uv tool list` colours its output
        # even when that output is captured rather than written to a console,
        # so each line arrives as ESC[1msynapse-cli v0.1.0ESC[0m and the
        # anchored ^ below can never match past the escape sequence.
        #
        # Observed 2026-08-07 on Windows: this step reported "no synapse uv
        # tools installed" while `uv tool list` plainly listed both, removed
        # nothing, and the script still printed "Uninstalled." A user
        # following the documented uninstall keeps both tools and all three
        # shims, and is told the opposite -- the failure is invisible unless
        # you go and check.
        #
        # Stripping rather than `--color never`: that flag exists in current
        # uv, but an older one would reject it, and an unknown flag fails
        # exactly the same silent way this comment exists to describe.
        # [char]27 rather than a literal escape keeps this file pure ASCII,
        # which test_install_ps1_is_pure_ascii requires.
        $tools = ((& uv tool list 2>$null) -join "`n") -replace "$([char]27)\[[0-9;]*m", ""
        foreach ($tool in @('synapse-cli', 'synapse-service')) {
            if ($tools -match "(?m)^$([regex]::Escape($tool)) ") {
                & uv tool uninstall $tool
                Say "  removed   $tool"
                $removed = $true
            }
        }
    }
    if (-not $removed) { Say "  no synapse uv tools installed -- nothing to remove" }

    Step "U3  config and state"
    if (Test-Path $SynHome) {
        if ($KeepConfig) {
            Say "  kept      $SynHome (-KeepConfig)"
        } else {
            Say "  removing  $SynHome -- config.toml, session bindings, the"
            Say "            write-ahead log, and logs go with it. (-KeepConfig"
            Say "            preserves it for a reinstall/upgrade.)"
            Remove-Item -Recurse -Force $SynHome
            Say "  removed   $SynHome"
        }
    } else {
        Say "  no $SynHome -- nothing to remove"
    }

    Step "U4  left in place -- yours, listed with the removal command"
    $claudeDir = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
    $left = $false
    foreach ($kind in @('skills', 'commands', 'agents')) {
        $entries = Get-ChildItem -Path (Join-Path $claudeDir $kind) -Filter 'synapse*' -ErrorAction SilentlyContinue
        foreach ($entry in $entries) {
            Say "  pack      $($entry.FullName)"
            Say "            Remove-Item -Recurse '$($entry.FullName)'"
            $left = $true
        }
    }
    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Say "  mcp       if 'synapse' is registered (user scope or per project):"
        Say "            claude mcp remove synapse"
        $left = $true
    }
    if (-not $left) { Say "  none found" }

    Say ""
    Say "Uninstalled."
    exit 0
}

Say "Synapse installer -- $Component"
$arch = $env:PROCESSOR_ARCHITECTURE
Say "  os        Windows $arch"
Say "  this script only installs. It will not ask questions, write config,"
Say "  or start anything."

# ---------------------------------------------------------------------------
# P1 -- prerequisites. Use what exists; install what is missing; warn on old.
# ---------------------------------------------------------------------------
Step "P1  prerequisites"

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    $uvVersion = ((& uv --version) -split '\s+')[1]
    Say "  uv        $uvVersion (already installed -- using it)"
    $parts = $uvVersion -split '\.'
    if ($parts[0] -eq '0' -and [int]$parts[1] -lt 6) {
        Say "  WARNING: uv $uvVersion is old -- consider 'uv self update'"
    }
} else {
    Say "  uv        not found -- installing from https://astral.sh/uv"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Die "uv installed but not on PATH. Open a new terminal and re-run."
    }
    Say "  uv        $((& uv --version))"
}

# The ARM64 interpreter trap (docs/NPU-RUNBOOK.md): on Windows-on-ARM, uv's
# managed CPython can be x86_64-under-Prism, which breaks NPU wheel installs
# later. Prefer a NATIVE arm64 interpreter when one exists.
$pythonArg = @()
if ($arch -eq 'ARM64') {
    $arm64Py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312-arm64\python.exe'
    if (Test-Path $arm64Py) {
        Say "  python    $arm64Py (native ARM64 -- pinned)"
        $pythonArg = @('--python', $arm64Py)
    } else {
        Say "  python    no native ARM64 CPython at $arm64Py"
        Say "            uv's managed interpreter may run x86-under-Prism -- fine for"
        Say "            the listen/claude-cli arms, but it breaks NPU wheels. For"
        Say "            NPU work, install ARM64 Python 3.12 from python.org first."
    }
}
if ($pythonArg.Count -eq 0) {
    $found = & uv python find '>=3.12' 2>$null
    if ($LASTEXITCODE -eq 0 -and $found) {
        Say "  python    $found (already present -- using it)"
    } else {
        Say "  python    no 3.12+ found -- uv will download one (managed, isolated)"
        & uv python install 3.12
        if ($LASTEXITCODE -ne 0) { Die "uv python install 3.12 failed" }
    }
}

if ($Component -eq 'client') {
    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Say "  claude    present (needed later for MCP registration / the claude-cli arm)"
    } else {
        Say "  claude    not found -- fine for install; 'synapse configure' will say"
        Say "            what to do when an agent needs to connect"
    }
    # Hardware-dependent, deliberately: GenieX serves the Snapdragon X Elite
    # NPU. Only relevant on ARM64 Windows; never suggested where it cannot run.
    if ($arch -eq 'ARM64') {
        if (Get-Command geniex -ErrorAction SilentlyContinue) {
            Say "  geniex    present -- the NPU distiller arm is available"
        } else {
            Say "  geniex    not found. This Snapdragon machine can run the NPU arm"
            Say "            once GenieX is installed and a model is pulled"
            Say "            ('geniex pull ...' -- see docs/NPU-RUNBOOK.md). Until then"
            Say "            the claude-cli / anthropic / listen arms all work."
        }
    } else {
        Say "  geniex    skipped -- NPU distillation is Snapdragon (ARM64) hardware;"
        Say "            this machine uses claude-cli / anthropic / listen"
    }
}

# ---------------------------------------------------------------------------
# P2 -- obtain wheels: a local dir, this checkout, or the GitHub Release.
# ---------------------------------------------------------------------------
Step "P2  packages"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("synapse-install-" + [System.IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $tmp | Out-Null
$wheels = ''

function Build-FromCheckout([string]$Root) {
    $out = Join-Path $tmp 'wheels'
    Push-Location $Root
    try {
        & uv build --all-packages --out-dir $out
        if ($LASTEXITCODE -ne 0) { Die "uv build failed in $Root" }
    } finally {
        Pop-Location
    }
    return $out
}

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { '' }

if ($Local) {
    if (-not (Test-Path $Local -PathType Container)) { Die "-Local $Local is not a directory" }
    if (Get-ChildItem (Join-Path $Local 'synapse_*.whl') -ErrorAction SilentlyContinue) {
        $wheels = $Local
        Say "  using wheels already in $Local"
    } elseif (Test-Path (Join-Path $Local 'pyproject.toml')) {
        Say "  building wheels from the checkout at $Local"
        $wheels = Build-FromCheckout $Local
    } else {
        Die "-Local $Local holds neither wheels nor a checkout"
    }
} elseif ($scriptDir -and (Test-Path (Join-Path $scriptDir 'pyproject.toml')) -and (Test-Path (Join-Path $scriptDir 'scripts\serve_local.py'))) {
    # Running from inside a checkout (developer path): what you install is
    # what you are looking at, not last week's release.
    Say "  building wheels from this checkout ($scriptDir)"
    $wheels = Build-FromCheckout $scriptDir
} else {
    $asset = if ($Component -eq 'server') { 'synapse-server-wheels.zip' } else { 'synapse-client-wheels.zip' }
    $url = if ($Tag) { "https://github.com/$RepoSlug/releases/download/$Tag/$asset" }
           else { "https://github.com/$RepoSlug/releases/latest/download/$asset" }
    Say "  downloading $url"
    $zip = Join-Path $tmp $asset
    try {
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    } catch {
        Die ("could not download $asset. Either no release is published yet (run the " +
             "'release' GitHub Actions workflow, or push a v* tag), or this machine " +
             "is offline. From a checkout, '.\install.ps1 $Component' builds locally instead.")
    }
    $wheels = Join-Path $tmp 'wheels'
    Expand-Archive -Path $zip -DestinationPath $wheels
    $versionFile = Join-Path $wheels 'VERSION'
    if (Test-Path $versionFile) { Say "  bundle    $(Get-Content $versionFile)" }
}

# ---------------------------------------------------------------------------
# P3 -- install. `uv tool install` gives each half its own isolated venv and a
# command on PATH. Every synapse-* dependency is pinned to the bundle's own
# wheel BY PATH (--with <file>), so nothing that happens to share a name on
# PyPI can shadow the release; PyPI serves only the third-party deps. The
# list is filtered per component so the client env can never quietly grow the
# service, or vice versa -- the halves stay decoupled by construction.
# ---------------------------------------------------------------------------
Step "P3  install"
$toolGlob = if ($Component -eq 'server') { 'synapse_service' } else { 'synapse_cli' }
$toolName = if ($Component -eq 'server') { 'synapse-service' } else { 'synapse-cli' }
$toolWheel = Get-ChildItem (Join-Path $wheels "$toolGlob-*.whl") | Select-Object -First 1
if (-not $toolWheel) { Die "no $toolName wheel in $wheels -- wrong bundle for '$Component'?" }

$keep = if ($Component -eq 'server') {
    @('synapse_contracts', 'synapse_providers')
} else {
    @('synapse_contracts', 'synapse_providers', 'synapse_distiller', 'synapse_worker', 'synapse_orchestrator')
}
$withArgs = @()
foreach ($wheel in Get-ChildItem (Join-Path $wheels 'synapse_*.whl')) {
    if ($wheel.FullName -eq $toolWheel.FullName) { continue }
    foreach ($name in $keep) {
        if ($wheel.Name -like "$name-*") { $withArgs += @('--with', $wheel.FullName); break }
    }
}
$installArgs = @('tool', 'install')
if ($Update) { $installArgs += '--reinstall' }
$installArgs += $pythonArg + $withArgs + @($toolWheel.FullName)
& uv @installArgs
if ($LASTEXITCODE -ne 0) { Die "uv tool install failed" }
Say "  installed $toolName into an isolated environment"

# ---------------------------------------------------------------------------
# P4 -- verify, then say what the NEXT stage is (without doing it).
# ---------------------------------------------------------------------------
Step "P4  verify"
$cmd = if ($Component -eq 'server') { 'synapse-server' } else { 'synapse' }
if (Get-Command $cmd -ErrorAction SilentlyContinue) {
    Say "  $cmd is on PATH"
} else {
    Say "  $cmd is installed but not on PATH yet. Run:"
    Say "    uv tool update-shell"
    Say "  then open a new terminal. (uv puts tools in %USERPROFILE%\.local\bin.)"
}

Say ""
Say "Installed. Nothing was configured and nothing was started. Next:"
if ($Component -eq 'server') {
    Say "  synapse-server configure --keys C:\path\to\keys.txt   # one API key per line"
    Say "  synapse-server up        # health-checks the keys, then serves (Ctrl-C stops)"
    Say "  synapse-server health    # configuration + key check, any time"
} else {
    Say "  synapse configure        # service URL (ping-tested), contributor, distiller"
    Say "  synapse up               # start orchestrator + worker (Ctrl-C stops)"
    Say "  synapse health           # what is configured, what is running"
}
