<#
.SYNOPSIS
Synapse — one-command install and run, Windows PowerShell.

.DESCRIPTION
The phase-for-phase mirror of install.sh. Same phase names (P0-P8), same
flags with PowerShell casing, same refusals. If the two ever disagree about
what a phase does, install.sh is the one that has been run on hardware.

HOST a session (from nothing):
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.ps1))) -Purpose "demo session"

JOIN someone's session (from nothing, one paste):
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main/install.ps1))) -ServiceUrl http://192.168.4.44:8899 -SharedId sh-bbe76a56 -Contributor akhil

From a checkout:
  .\install.ps1 -Purpose "demo session"
  .\install.bat -Purpose "demo session"        # double-clickable; no execution-policy prompt

The `[scriptblock]::Create` wrapper is load-bearing and is the exact
counterpart of `sh -s --` on the POSIX side: `irm ... | iex` cannot take
arguments, so a joining teammate would have to hand-transcribe a URL, a
session id and their own name. This form takes them through the pipe.

.NOTES
UNVERIFIED ON HARDWARE. Written and reviewed on macOS, where no `pwsh` is
installed, so nothing here has been executed or even parsed by a real
PowerShell. Every claim below is a static one. The first real Windows run is
a human step and it is the largest residual risk in this workstream. Mitigate
it by running `-DoctorOnly` first: that path starts nothing, registers
nothing, and writes nothing into the checkout.
#>

[CmdletBinding()]
param(
    # Pass-through to scripts/serve_local.py. Mapped here only so PowerShell's
    # own parser gives them a name; the values are forwarded verbatim.
    [string]$Purpose,
    [string]$SharedId,
    [string]$ServiceUrl,
    [string]$Contributor,
    # No -Host parameter on purpose: $Host is a PowerShell automatic variable
    # and a parameter of that name is a trap for the reader, not a convenience.
    # serve_local.py's --host reaches it through $Rest, verbatim:
    #   .\install.ps1 --host 0.0.0.0
    [string]$Distiller,
    [string]$ClaudeModel,
    [string]$DistillerModel,
    [string]$SynthesizerModel,
    [switch]$Npu,
    [switch]$Listen,
    [switch]$Live,

    # Installer-consumed.
    [string]$Project,
    [string]$Dir = ".\Synapse",
    [switch]$Clean,
    [switch]$Update,
    [switch]$DoctorOnly,
    [switch]$NoStart,
    [switch]$SkipMcp,
    [switch]$Force,

    # The pass-through channel for anything the installer has never heard of.
    # This is what keeps install.ps1 from having to enumerate serve_local.py's
    # flags, and therefore from drifting away from them.
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

# ---------------------------------------------------------------------------
# Encoding, before anything else runs.
#
# PYTHONUTF8=1 fixes two different things at once: the encoding of a piped or
# redirected stdout, and the default encoding of open()/read_text() — which is
# why secrets.jsonc and the transcript writers need it as much as the console
# does. `chcp 65001` fixes the *display* in the interactive window, which is
# the half that matters on a projector. Both are needed; neither substitutes
# for the other.
# ---------------------------------------------------------------------------
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { chcp 65001 > $null } catch { }
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new() } catch { }
$ErrorActionPreference = "Stop"

$RepoUrl  = "https://github.com/SinghSiddharth01/Synapse.git"
$RawUrl   = "https://raw.githubusercontent.com/SinghSiddharth01/Synapse/main"
# Kept in lockstep with packages/orchestrator/pyproject.toml:12 ("mcp==1.9.4"),
# scripts/doctor.py:REQUIRED_MCP and install.sh:REQUIRED_MCP.
$RequiredMcp = "1.9.4"
$Uv = "uv"

function Say  { param([string]$Text) Write-Output $Text }
function Step { param([string]$Text) Write-Output ""; Write-Output "==> $Text" }
function Die  { param([string]$Text) Write-Error "install.ps1: $Text"; exit 1 }

# ---------------------------------------------------------------------------
# Pass-through assembly. Mapped flags first, in a stable order, then $Rest
# verbatim. Names on the left are PowerShell's; names on the right are
# serve_local.py's, and this table is the ONLY place the two are related.
# ---------------------------------------------------------------------------
$Forward = New-Object System.Collections.Generic.List[string]
function Add-Value { param([string]$Flag, [string]$Value)
    if ($Value) { $Forward.Add($Flag); $Forward.Add($Value) } }
function Add-Switch { param([string]$Flag, [bool]$On)
    if ($On) { $Forward.Add($Flag) } }

Add-Value  "--purpose"           $Purpose
Add-Value  "--shared-id"         $SharedId
Add-Value  "--service-url"       $ServiceUrl
Add-Value  "--contributor"       $Contributor
Add-Value  "--distiller"         $Distiller
Add-Value  "--claude-model"      $ClaudeModel
Add-Value  "--distiller-model"   $DistillerModel
Add-Value  "--synthesizer-model" $SynthesizerModel
Add-Switch "--npu"               $Npu.IsPresent
Add-Switch "--listen"            $Listen.IsPresent
Add-Switch "--live"              $Live.IsPresent
if ($Rest) { foreach ($token in $Rest) { $Forward.Add($token) } }

# A joiner run is one that points at somebody else's service. Observed, never
# consumed: serve_local.py still needs the flag itself.
$Joining = [bool]$ServiceUrl -or ($Forward -contains "--service-url")

# ---------------------------------------------------------------------------
# P0 — preamble
# ---------------------------------------------------------------------------
Say "Synapse installer (PowerShell)"
Say ("  os        {0} {1}" -f [System.Environment]::OSVersion.VersionString, $env:PROCESSOR_ARCHITECTURE)
if ($Forward.Count -gt 0) {
    Say "  forward   $($Forward.Count) argument(s) to scripts/serve_local.py:"
    # One argument per line, never joined into a string: -Purpose "the DMA
    # timing bug" must be shown with the same boundaries it will be passed with.
    foreach ($token in $Forward) { Say "      | $token" }
} else {
    Say "  forward   (no serve_local.py flags given)"
}
# No secret can appear above: credentials are never flags in this project,
# they live in secrets.jsonc.

# ---------------------------------------------------------------------------
# P1 — prerequisites
# ---------------------------------------------------------------------------
Step "P1  prerequisites"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Die "git is required. Install it from https://git-scm.com/download/win (or ``winget install Git.Git``), then re-run."
}
Say "  git       $(git --version)"

if (Get-Command uv -ErrorAction SilentlyContinue) {
    Say "  uv        $(uv --version)"
} else {
    Say "  uv        not found — installing from https://astral.sh/uv"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    # PATH in THIS process is stale: the installer edited the user PATH, which
    # only new processes see. Re-probe the known location and use the absolute
    # path from here.
    $candidate = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path -LiteralPath $candidate) {
        $Uv = $candidate
    } elseif (Get-Command uv -ErrorAction SilentlyContinue) {
        $Uv = "uv"
    } else {
        Die "uv installed but is not on PATH and not at $candidate. Open a new PowerShell and re-run."
    }
    Say "  uv        $(& $Uv --version)  ($Uv)"
}

# An absolute $Uv is not enough. `uv run` prepends only <repo>\.venv\Scripts to
# the child's PATH, so scripts/doctor.py check 1 (`shutil.which("uv")`) would
# report "FAIL uv not on PATH" on the box this installer just provisioned, and
# P8 would then abort the run over its own success. Same fix as install.sh.
$UvCommand = Get-Command $Uv -ErrorAction SilentlyContinue
if ($UvCommand -and $UvCommand.Source) {
    $UvDir = Split-Path -Parent $UvCommand.Source
    if ($UvDir -and (($env:PATH -split ";") -notcontains $UvDir)) {
        $env:PATH = "$UvDir;$env:PATH"
        Say "  path      added $UvDir to PATH for this run"
    }
}

if (Get-Command claude -ErrorAction SilentlyContinue) {
    Say "  claude    present"
} else {
    Say "  claude    not found — the stack still runs; you just have no agent to"
    Say "            connect yet. Install Claude Code, then run:"
    Say "              claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp"
    $SkipMcp = [switch]$true
}

# ---------------------------------------------------------------------------
# P2 — repo. Never clobber, never switch branch, never pull unless asked.
# ---------------------------------------------------------------------------
Step "P2  repository"
$top = $null
try { $top = (git rev-parse --show-toplevel 2>$null) } catch { $top = $null }
if ($top -and (Test-Path -LiteralPath (Join-Path $top "scripts\serve_local.py"))) {
    Set-Location -LiteralPath $top
    Say "  using this checkout: $top"
} elseif (Test-Path -LiteralPath (Join-Path $Dir "scripts\serve_local.py")) {
    Set-Location -LiteralPath $Dir
    Say "  using existing checkout: $((Get-Location).Path)"
} else {
    Say "  cloning $RepoUrl -> $Dir"
    git clone $RepoUrl $Dir
    if ($LASTEXITCODE -ne 0) { Die "git clone failed" }
    Set-Location -LiteralPath $Dir
}

# Print what you are actually about to run. A checkout parked on a fallback
# branch is a legitimate state -- the demo may depend on it -- so this reports
# rather than corrects.
$branch = (git rev-parse --abbrev-ref HEAD 2>$null)
$sha    = (git rev-parse --short HEAD 2>$null)
Say "  branch    $branch  $sha"

if ($Update) {
    Say "  updating (-Update): git pull --ff-only on $branch"
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { Die "git pull --ff-only failed; resolve it by hand and re-run" }
    Say "  now at    $(git rev-parse --short HEAD)"
} else {
    Say "  not pulling (pass -Update if you want to)"
}

# ---------------------------------------------------------------------------
# P3 — sync, then assert the one pin that silently ruins Windows-on-ARM
# ---------------------------------------------------------------------------
Step "P3  dependencies"

# ARM64 native-interpreter resolution.
#
# uv will happily provision an x86_64 interpreter that runs under Prism
# emulation on an ARM64 box. Nothing complains at sync time. The failure
# surfaces much later as a Rust build error out of `cryptography`, which reads
# like a packaging problem and is not one — and the NPU wheels never load at
# all. So on ARM64 the interpreter is chosen explicitly, and each candidate is
# VERIFIED by asking Python itself what it is. A directory called `Python312`
# under Programs\Python is not evidence of anything.
$pythonArg = @()
if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
    Say "  host      ARM64 — resolving a native Python before syncing"
    $roots = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        (Join-Path $env:ProgramFiles "")
    )
    $candidates = New-Object System.Collections.Generic.List[string]
    foreach ($root in $roots) {
        if (-not $root -or -not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like "Python3*" } |
            ForEach-Object {
                $exe = Join-Path $_.FullName "python.exe"
                if (Test-Path -LiteralPath $exe) { $candidates.Add($exe) }
            }
    }
    $native = $null
    foreach ($exe in $candidates) {
        $reported = $null
        try {
            $reported = (& $exe -c "import platform;print(platform.machine())" 2>$null)
        } catch { $reported = $null }
        if ($reported) { $reported = ([string]$reported).Trim() }
        Say "  candidate $exe -> $(if ($reported) { $reported } else { '(did not answer)' })"
        if ($reported -eq "ARM64") { $native = $exe; break }
    }
    if ($native) {
        Say "  python    $native  (verified ARM64 by platform.machine())"
        $pythonArg = @("--python", $native)
    } else {
        # Do not guess. A bare `uv sync` here provisions the emulated venv this
        # whole block exists to avoid, and the operator finds out an hour later.
        Die @"
no native ARM64 Python found, and this is an ARM64 machine.
Install Python 3.12+ (ARM64 build) from https://www.python.org/downloads/windows/
— pick the installer whose name ends in ``-arm64.exe`` — then re-run.
Refusing to run ``uv sync`` first: it would provision an x86_64 interpreter under
Prism emulation, and the failure surfaces much later as a Rust build error out of
``cryptography`` that reads like an unrelated packaging problem.
"@
    }
}

& $Uv sync @pythonArg
if ($LASTEXITCODE -ne 0) { Die "uv sync failed" }

# The probe must never raise: version() on a venv where mcp did not install
# throws PackageNotFoundError, and the traceback would replace the one message
# that says "do not upgrade mcp".
$foundMcp = "absent"
try {
    $probe = & $Uv run python -c @"
import importlib.metadata as m
try:
    print(m.version('mcp'))
except Exception:
    print('absent')
"@ 2>$null
    if ($probe) { $foundMcp = ([string]$probe).Trim() }
} catch { $foundMcp = "absent" }

if ($foundMcp -ne $RequiredMcp) {
    $mcpMsg = @"
mcp is $foundMcp, expected $RequiredMcp.
Do not upgrade mcp; re-sync from uv.lock:  $Uv sync --frozen
Newer mcp pulls a cryptography release with no ARM64 Windows wheel, and the
build failure it produces reads like an unrelated Rust error.
"@
    # -Force is documented as "start even if the doctor reports a FAIL", and the
    # doctor reports this same mismatch as a FAIL. An ungated abort here would
    # make -Force unable to do the one thing it promises.
    if ($Force) { Say "  WARNING (-Force): $mcpMsg" } else { Die $mcpMsg }
} else {
    Say "  mcp       $foundMcp  (pinned)"
}

# ---------------------------------------------------------------------------
# P4 — secrets. Created from the template, NEVER overwritten, never echoed.
# ---------------------------------------------------------------------------
Step "P4  credentials"
$secrets = Join-Path (Get-Location).Path "secrets.jsonc"
$template = Join-Path (Get-Location).Path "secrets.example.jsonc"
if (Test-Path -LiteralPath $secrets) {
    Say "  secrets.jsonc already exists — left untouched"
} elseif ($DoctorOnly) {
    # -DoctorOnly is an INSPECTION. Creating the file here would answer the
    # doctor's own question before it is asked: its check 6 reports
    # "secrets.jsonc absent" as a WARN, and that WARN is unreachable through an
    # installer that has already made it false.
    Say "  secrets.jsonc absent — NOT creating it (-DoctorOnly changes nothing)"
    Say "  the doctor below reports the real state of this machine"
} elseif (Test-Path -LiteralPath $template) {
    # Explicit UTF-8 without a BOM: the JSONC readers (scripts/doctor.py,
    # scripts/local_model_server.py, scripts/serve_local.py) strip `//` line
    # comments, not a byte-order mark, and a BOM makes json.loads fail on
    # character 1 with a message that says nothing about a BOM.
    $text = [IO.File]::ReadAllText($template, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($secrets, $text, [Text.UTF8Encoding]::new($false))
    Say "  created $secrets from the template"
} else {
    Say "  secrets.example.jsonc missing from this checkout — skipping"
}
Say "  Nothing in it is required for a first run: the stand-in arm needs no key."
Say "  Paste real keys there only when you want -Live or -Distiller anthropic."

$insideWorkTree = $false
try {
    $insideWorkTree = ((git rev-parse --is-inside-work-tree 2>$null) -eq "true")
} catch { $insideWorkTree = $false }
if ($insideWorkTree) {
    if (Test-Path -LiteralPath $secrets) {
        git check-ignore -q secrets.jsonc 2>$null
        if ($LASTEXITCODE -ne 0) {
            Die @"
secrets.jsonc is NOT gitignored in this checkout. Refusing to continue —
restore the 'secrets.jsonc' line in .gitignore first, or you will commit a key.
"@
        }
    }
} else {
    # A zip download has no .git. That is a legitimate way to get the code, and
    # aborting on it would be a false failure -- there is no index to commit to.
    Say "  WARNING: not a git checkout (zip download?) — cannot verify that"
    Say "           secrets.jsonc is ignored. Do not commit it."
}

# ---------------------------------------------------------------------------
# P6 — port hygiene. Only ever under -Clean. Before the doctor, so the
# doctor's port check reports the state -Clean actually left behind.
# ---------------------------------------------------------------------------
if ($Clean -and -not $DoctorOnly) {
    Step "P6  clearing orphaned processes (-Clean)"
    # PowerShell-native throughout. Never shell out to `rm -rf`/`pkill`: that
    # regression is on record at scripts/rehearse_demo.py:585-587, where a
    # shelled-out removal raised FileNotFoundError out of a `finally` and
    # replaced the real failure with a traceback.
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'synapse-service|synapse-orchestrator|local_model_server' } |
        ForEach-Object {
            Say "  killing pid $($_.ProcessId)"
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    # These patterns are Synapse's own processes and nothing else's. The model
    # seam on 18181 can equally be held by `geniex serve` (see
    # scripts/serve_local.py:68), which -Clean deliberately does NOT kill: an
    # NPU server is somebody's deliberate setup, not an orphan. Report what
    # survived rather than leaving it to be discovered at bind time.
    $held = Get-NetTCPConnection -LocalPort 8787, 8899, 18181 -State Listen -ErrorAction SilentlyContinue
    foreach ($conn in $held) {
        Say "  still held: $($conn.LocalPort) by pid $($conn.OwningProcess)  (-Clean does not kill non-Synapse holders such as ``geniex serve``)"
    }
    Say "  done"
}

# ---------------------------------------------------------------------------
# P8 — doctor. Runs BEFORE anything is started AND before P5 mutates
# %USERPROFILE%\.claude, so a box with the wrong interpreter or the wrong mcp
# is not half-configured on the way to failing. Its output is teed, because the
# Unicode canary only proves something through a redirection — and on Windows
# that redirection is the whole point of check 4.
# ---------------------------------------------------------------------------
Step "P8  pre-flight doctor"
if ($DoctorOnly) {
    # An inspection writes nothing into the checkout. The tee still happens; it
    # just lands in the temp directory.
    $doctorLog = Join-Path ([IO.Path]::GetTempPath()) ("synapse-doctor-" + [Guid]::NewGuid().ToString("N") + ".log")
} else {
    New-Item -ItemType Directory -Force -Path ".synapse\logs" | Out-Null
    $doctorLog = ".synapse\logs\doctor.log"
}
& $Uv run python scripts/doctor.py 2>&1 | Tee-Object -FilePath $doctorLog
# Read the real exit status, not just the log. Grepping for a FAIL line misses
# every way the doctor can fail WITHOUT printing one — an unhandled traceback,
# an OSError from the port probe, an ImportError in the venv.
$doctorRc = $LASTEXITCODE
if ($null -eq $doctorRc) { $doctorRc = 0 }
if ($doctorRc -ne 0) { Say ""; Say "  the doctor exited $doctorRc" }
# Belt and braces: a FAIL line with a zero exit would be a doctor bug.
if (Select-String -Path $doctorLog -Pattern '^FAIL' -Quiet -ErrorAction SilentlyContinue) { $doctorRc = 1 }

Say ""
if ($DoctorOnly) {
    Say "  full log: $doctorLog  (temporary — -DoctorOnly writes nothing here)"
    Say ""
    Say "-DoctorOnly: nothing was started, nothing was registered, nothing was"
    Say "written into this checkout."
    exit $doctorRc
}
Say "  full log: $((Get-Location).Path)\$doctorLog"

if ($doctorRc -ne 0 -and -not $Force) {
    Die "the doctor reported a FAIL (above). Fix it, or re-run with -Force."
}

# A held port is a WARN, not a FAIL, and correctly so. But claim_ports
# (scripts/serve_local.py:159-189) does not treat it as normal: it SystemExits
# on the first port it cannot bind.
if (Select-String -Path $doctorLog -Pattern '^WARN  ports' -Quiet -ErrorAction SilentlyContinue) {
    Say ""
    Say "  NOTE: the doctor WARNed about a held port. serve_local.py will REFUSE"
    Say "        to start on one it cannot bind — which ports it needs depends on"
    Say "        your flags (-ServiceUrl drops 8899; -Listen/-Npu drop 18181;"
    Say "        see scripts/serve_local.py:277-278). If P7 below exits with"
    Say "        ""ports already in use"", re-run with -Clean."
}

# ---------------------------------------------------------------------------
# P5 — MCP registration + awareness pack. Idempotent.
# ---------------------------------------------------------------------------
if ($DoctorOnly) {
    Step "P5  MCP registration — skipped (-DoctorOnly changes nothing)"
} elseif ($SkipMcp) {
    Step "P5  MCP registration — skipped (-SkipMcp)"
} else {
    Step "P5  MCP registration"
    $repoAbs = (Get-Location).Path
    if (-not $Project) { $Project = $PWD.Path }
    $projectAbs = ""
    try { $projectAbs = (Resolve-Path -LiteralPath $Project -ErrorAction Stop).Path } catch { $projectAbs = "" }
    # Containment, not equality: run the one-liner from %USERPROFILE% and the
    # clone lands in %USERPROFILE%\Synapse, so the starting directory is not
    # EQUAL to the checkout but every directory under it IS inside it.
    # Registering a project-scoped server inside the Synapse checkout writes an
    # .mcp.json into the repo the operator is about to `git status`.
    $insideCheckout = $true
    if ($projectAbs) {
        $insideCheckout = ($projectAbs -eq $repoAbs) -or $projectAbs.StartsWith($repoAbs + [IO.Path]::DirectorySeparatorChar)
    }
    # Always YOUR OWN 127.0.0.1:8787, never the host's. One orchestrator per
    # laptop is what keeps attribution honest: point your agent at someone
    # else's and your findings are stamped with their contributor.
    $mcpUrl = "http://127.0.0.1:8787/mcp"
    if ($insideCheckout) {
        Say "  no project directory to register in (you are inside the Synapse"
        Say "  checkout itself, or it no longer exists). From the project you want"
        Say "  shared memory in, run:"
        Say "    claude mcp add --transport http --scope project synapse $mcpUrl"
    } else {
        # --scope project is per-directory: the registration lives in the
        # project's own .mcp.json. Asking from the Synapse checkout answers a
        # question about the wrong directory.
        Push-Location -LiteralPath $projectAbs
        try {
            $listed = ""
            try { $listed = (claude mcp list 2>$null | Out-String) } catch { $listed = "" }
            if ($listed -match '(?m)^synapse') {
                Say "  already registered in $projectAbs — if /mcp shows it failed, pick"
                Say "  Reconnect there"
            } else {
                claude mcp add --transport http --scope project synapse $mcpUrl
                Say "  registered 'synapse' in $projectAbs"
            }
        } finally { Pop-Location }
    }

    # Awareness pack. A glob over whatever the pack actually ships, so a
    # commands/ or agents/ directory landing later needs no edit here.
    $claudeHome = Join-Path $env:USERPROFILE ".claude"
    foreach ($kind in @("skills", "commands", "agents")) {
        $src = Join-Path "packs\claude-code" $kind
        if (-not (Test-Path -LiteralPath $src)) { continue }
        $dest = Join-Path $claudeHome $kind
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        foreach ($entry in (Get-ChildItem -LiteralPath $src -ErrorAction SilentlyContinue)) {
            $target = Join-Path $dest $entry.Name
            if ((Test-Path -LiteralPath $target) -and (-not $Update)) {
                Say "  pack      $kind/$($entry.Name) already installed — left alone (-Update to replace)"
            } else {
                # Move aside, never delete. What sits there may be something the
                # operator edited by hand, and -Update is documented as "git pull
                # --ff-only" — an unprompted, unbacked-up removal under that
                # description is a surprise nobody consented to.
                if (Test-Path -LiteralPath $target) {
                    $backup = "$target.synapse-bak." + (Get-Date -Format "yyyyMMddHHmmss")
                    Move-Item -LiteralPath $target -Destination $backup -Force
                    Say "  pack      previous $kind/$($entry.Name) moved aside -> $backup"
                }
                Copy-Item -LiteralPath $entry.FullName -Destination $target -Recurse -Force
                Say "  pack      $kind/$($entry.Name) -> $target"
            }
        }
    }
    Say "  the doctor's 'awareness' line above described this machine BEFORE the"
    Say "  copy; re-run '.\install.ps1 -DoctorOnly' to see it settle."
    if (Test-Path -LiteralPath "packs\claude-code\settings-snippet.json") {
        Say "  Optional hooks: merge packs\claude-code\settings-snippet.json into"
        Say "  your %USERPROFILE%\.claude\settings.json by hand — this script will"
        Say "  not rewrite a settings file it does not own. See"
        Say "  packs\claude-code\INSTALL.md."
        # The snippet's command points at
        #   $CLAUDE_PROJECT_DIR/.claude/synapse-pack/hooks/freshness_pointer.py
        # and nothing above puts a file there: the pack loop copies skills/,
        # commands/ and agents/, and hooks/ is none of those. Merging the
        # snippet alone yields a hook that cannot find its script.
        if (Test-Path -LiteralPath "packs\claude-code\hooks") {
            Say "  The snippet also needs the hook script itself, per project:"
            Say "    New-Item -ItemType Directory -Force <your-project>\.claude\synapse-pack"
            Say "    Copy-Item -Recurse packs\claude-code\hooks <your-project>\.claude\synapse-pack\"
        }
    }
}

# ---------------------------------------------------------------------------
# P7 — start, in the foreground, so Ctrl-C works and the banner is live.
# ---------------------------------------------------------------------------
if ($NoStart) {
    Step "P7  start — skipped (-NoStart)"
    Say "  would have run: $Uv run python scripts/serve_local.py"
    foreach ($token in $Forward) { Say "      | $token" }
    exit 0
}

Step "P7  starting Synapse"
if ($Joining) { Say "  joining a service someone else is hosting" }
Say ""
& $Uv run python scripts/serve_local.py @Forward
exit $LASTEXITCODE
