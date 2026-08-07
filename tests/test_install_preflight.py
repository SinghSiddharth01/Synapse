"""Regression cover for the install path: `install.sh` and `scripts/doctor.py`.

Both shipped with zero automated coverage — `git grep -l doctor -- tests
packages` and `git grep -l install.sh -- tests packages .github` both returned
nothing — while being the first code a joining teammate runs and the only code
whose failure mode is "the demo does not start". The gaps this file closes are
the ones that were found by hand and would otherwise be found again:

  * the doctor's exit status is the installer's gate, and the installer used to
    read a log instead of the status;
  * the secrets check must never put a credential on stdout;
  * the credential resolver in `rehearse_demo.py` used to match `anthropic`'s
    key and resurrect commented-out ones;
  * `install.sh` must stay POSIX and must not grow a default `git pull`.

Nothing here binds a port, starts a server, or makes a network request.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO / "install.sh"


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves sys.modules[cls.__module__]
    # while building the class, and a module absent from sys.modules gives it
    # None to take __dict__ off.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def doctor():
    return _load("_doctor_under_test", "scripts/doctor.py")


@pytest.fixture(scope="module")
def rehearse():
    return _load("_rehearse_under_test", "scripts/rehearse_demo.py")


# --------------------------------------------------------------------------
# doctor: the result model and the exit contract
# --------------------------------------------------------------------------

def test_doctor_runs_exactly_the_eight_declared_checks(doctor):
    """The scope caps this at ~8 pre-flight checks, one code path.

    A ninth arriving unnoticed is how `--doctor-only` stops being the whole
    doctor, which is the property the installers depend on.
    """
    assert len(doctor.CHECKS) == 8
    assert [c.__name__ for c in doctor.CHECKS] == [
        "check_uv", "check_interpreter", "check_mcp_version", "check_unicode",
        "check_ports", "check_secrets", "check_pack", "check_claude_mcp",
    ]


def test_warn_never_fails_the_run_but_fail_does(doctor, monkeypatch, capsys):
    """WARN is "you should know"; FAIL is "this box is not ready". Only FAIL exits 1.

    The installer aborts on a non-zero doctor, so a WARN that leaked into the
    exit status would block every machine that merely has a stack running.
    """
    warn = doctor.Result("w", "WARN", "d")
    monkeypatch.setattr(doctor, "CHECKS", (lambda: warn,))
    assert doctor.main([]) == 0

    fail = doctor.Result("f", "FAIL", "d")
    monkeypatch.setattr(doctor, "CHECKS", (lambda: warn, lambda: fail))
    assert doctor.main([]) == 1
    capsys.readouterr()


def test_json_mode_is_parseable_and_keeps_the_canary_off_stdout(doctor, capsys):
    """`--json` owns stdout for its document.

    The Unicode canary writes to a real stream on purpose — that is the check —
    so in JSON mode it has to go to stderr or it corrupts the document a caller
    is about to parse.
    """
    rc = doctor.main(["--json"])
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert rc in (0, 1)
    assert len(payload["results"]) == 8
    assert {"repo", "branch", "sha", "tree"} <= set(payload["repo"])
    assert "canary" not in out.out
    assert "canary" in out.err


def test_secrets_check_reports_booleans_and_never_a_value(doctor, tmp_path, monkeypatch):
    """The one check that touches credentials must not be able to print one."""
    secret = "NOTAREALKEY-test_secrets_check-1234567890"
    (tmp_path / "secrets.jsonc").write_text(
        '{\n'
        '  // "api_key": "NOTAREALKEY-commented-out"\n'
        f'  "inference_cloud": {{"api_key": "{secret}", "base_url": "https://x"}},\n'
        '  "anthropic": {"api_key": ""}\n'
        '}\n', encoding="utf-8")
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    result = doctor.check_secrets()

    blob = f"{result.status} {result.name} {result.detail} {result.remedy}"
    assert secret not in blob
    assert secret[:8] not in blob
    assert str(len(secret)) not in blob
    assert "inference_cloud.api_key=set" in result.detail
    assert "anthropic.api_key=empty" in result.detail


def test_secrets_check_fails_on_unparseable_jsonc_with_line_number_only(doctor, tmp_path,
                                                                        monkeypatch):
    """The offending text is very likely the credential, so only the line may be shown."""
    (tmp_path / "secrets.jsonc").write_text(
        '{\n  "inference_cloud": {"api_key": "NOTAREALKEY-broken"\n}\n', encoding="utf-8")
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    result = doctor.check_secrets()
    assert result.status == "FAIL"
    assert "NOTAREALKEY" not in result.detail
    assert re.search(r"line \d+", result.detail)


def test_absent_secrets_is_a_warn_not_a_fail(doctor, tmp_path, monkeypatch):
    """`--doctor-only` must be able to reach this branch.

    It could not while `install.sh` created secrets.jsonc from the template
    before running the doctor: the WARN was true of the machine and false of
    every machine the installer had touched.
    """
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    result = doctor.check_secrets()
    assert result.status == "WARN"
    assert "absent" in result.detail


def test_mcp_registration_is_read_from_the_project_scope_file(doctor, tmp_path, monkeypatch):
    """`.mcp.json` is what `claude mcp add --scope project` writes.

    `claude mcp list` does not report this repo's own tracked `.mcp.json`, so a
    correctly configured checkout used to be told it had no server registered.
    """
    monkeypatch.setattr(doctor, "REPO", tmp_path)
    assert doctor._project_mcp_json() == ""
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"synapse": {"url": "http://127.0.0.1:8787/mcp"}}}),
        encoding="utf-8")
    assert doctor._project_mcp_json() == "http://127.0.0.1:8787/mcp"


def test_doctor_makes_no_http_request(doctor):
    """Pre-flight only: no network, no MCP client, nothing started."""
    source = (REPO / "scripts" / "doctor.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(urllib|requests|httpx)\b", source)


# --------------------------------------------------------------------------
# rehearse_demo: which key --live picks up
# --------------------------------------------------------------------------

@pytest.mark.parametrize("body,expected", [
    # An empty inference_cloud key must NOT fall through to anthropic's.
    ('{"inference_cloud": {"api_key": ""}, "anthropic": {"api_key": "sk-ant-NOTAREAL"}}',
     None),
    # A commented-out key is not a credential.
    ('{\n  // "api_key": "NOTAREALKEY-commented"\n  "inference_cloud": {"api_key": ""}\n}',
     None),
    # The real one, from the block the file documents.
    ('{"inference_cloud": {"api_key": "NOTAREALKEY-cloud"}, '
     '"anthropic": {"api_key": "sk-ant-NOTAREAL"}}',
     "NOTAREALKEY-cloud"),
    # The bare top-level shape local_model_server._credentials also accepts.
    ('{"api_key": "NOTAREALKEY-bare"}', "NOTAREALKEY-bare"),
])
def test_live_key_comes_from_the_inference_cloud_block(rehearse, tmp_path, monkeypatch,
                                                       body, expected):
    (tmp_path / "secrets.jsonc").write_text(body, encoding="utf-8")
    monkeypatch.setattr(rehearse, "ROOT", tmp_path)
    assert rehearse._inference_cloud_key() == expected


# --------------------------------------------------------------------------
# install.sh: the properties that make it safe to pipe into sh
# --------------------------------------------------------------------------

def test_install_sh_is_executable_and_posix():
    assert INSTALL_SH.is_file()
    assert os.access(INSTALL_SH, os.X_OK)
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    # dash has none of these. Each one has been a real portability bug. Read
    # code only: the header comment names all three in order to forbid them.
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "[[" not in code
    assert "pipefail" not in code
    assert not re.search(r"^\s*local\s", code, flags=re.MULTILINE)


@pytest.mark.skipif(shutil.which("dash") is None and shutil.which("sh") is None,
                    reason="no POSIX shell to parse with")
def test_install_sh_parses_under_a_posix_shell():
    shell = shutil.which("dash") or shutil.which("sh")
    done = subprocess.run([shell, "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_install_sh_never_touches_git():
    """Install is INSTALL: wheels come from the release bundle (or are built
    from the checkout the script already sits in). No clone, no pull, no
    switch — the user never needs git to run Synapse."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert not re.search(r"^\s*git\b", code, flags=re.MULTILINE)
    assert "git clone" not in code


def test_install_sh_refuses_configure_flags():
    """Install and configuration are SEPARATE STAGES. The old installer took
    --service-url/--shared-id/--purpose and started the stack; every one of
    those must now be a loud refusal that names the right stage."""
    for flag in ("--service-url", "--shared-id", "--purpose", "--contributor"):
        done = subprocess.run([str(INSTALL_SH), flag, "x"], capture_output=True,
                              text=True, cwd=str(REPO), stdin=subprocess.DEVNULL)
        assert done.returncode == 1, flag
        assert "not an installer flag" in done.stderr, flag
        assert "synapse config set service.url" in done.stderr, flag


def test_install_sh_advertises_both_one_liners():
    """The curl and scriptblock one-liners are the deliverable: a teammate
    installs with one paste and NO arguments — joining happens later, at run
    time, through `synapse config set` + `synapse up`."""
    done = subprocess.run([str(INSTALL_SH), "--help"], capture_output=True, text=True,
                          cwd=str(REPO))
    assert done.returncode == 0
    assert "install.sh | sh" in done.stdout
    assert "scriptblock]::Create" in done.stdout
    # and the next stage is named, not performed
    assert "synapse configure" in done.stdout


def test_windows_entry_points_exist():
    """README, install.sh --help, doctor.py and serve_local.py all name these.

    They pointed at nothing for the whole of the W8 chain: DEV-2 never ran, so
    every Windows instruction in the repo resolved to a 404.
    """
    ps1 = REPO / "install.ps1"
    bat = REPO / "install.bat"
    assert ps1.is_file()
    assert bat.is_file()

    ps1_text = ps1.read_text(encoding="utf-8")
    # Install-only, mirrored: the same component grammar as install.sh…
    assert "ValidateSet('client', 'server')" in ps1_text
    # …the ARM64 interpreter trap stays handled (uv's managed CPython can be
    # x86-under-Prism on Snapdragon, which breaks NPU wheels)…
    assert "Python312-arm64" in ps1_text
    # …geniex is hardware-gated, never installed where it cannot run…
    assert "ARM64" in ps1_text and "geniex" in ps1_text
    # …and none of the CONFIGURE-stage flags survived the rewrite.
    for gone in ("-ServiceUrl", "-SharedId", "-Purpose", "$Contributor"):
        assert gone not in ps1_text, gone
    # PowerShell-native removal only (scripts/rehearse_demo.py's shelled-out
    # removal once replaced a real failure with a FileNotFoundError traceback).
    assert "rm -rf" not in ps1_text.replace("`rm -rf`", "")

    # Two lines, CRLF, and %~dp0 quoted so a path with a space still works.
    raw = bat.read_bytes()
    assert raw.count(b"\r\n") == 2
    assert b'"%~dp0install.ps1"' in raw


def test_install_ps1_is_pure_ascii():
    """The UTF-8 preamble cannot save the script's OWN literals.

    `install.bat` launches `powershell` -- Windows PowerShell 5.1, not pwsh --
    and 5.1 decodes a script file with no byte-order mark using the system ANSI
    codepage, NOT UTF-8. That decision is made by the parser before line 1 runs,
    so `chcp 65001`, `[Console]::OutputEncoding` and `$env:PYTHONUTF8` -- all of
    which govern OUTPUT -- are powerless over it: every em dash in a `Say`
    string reaches the screen as `a<TM>"`. install.ps1 shipped with 33 of them.

    A BOM would fix the file path and put a U+FEFF at the front of the string
    that `irm ... | iex` hands to [scriptblock]::Create. ASCII-only fixes every
    path at once and cannot be undone by an encoding decision anywhere.

    install.sh is deliberately NOT held to this: `sh` copies bytes to stdout
    without re-decoding them, so its UTF-8 prose renders correctly.
    """
    ps1_text = (REPO / "install.ps1").read_text(encoding="utf-8")
    offenders = sorted({c for c in ps1_text if ord(c) > 127})
    assert not offenders, (
        f"install.ps1 must be pure ASCII; found {offenders!r}. "
        "Windows PowerShell 5.1 reads a BOM-less script as ANSI."
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_install_sh_help_needs_no_stdin():
    """`curl … | sh` gives every child the SCRIPT as stdin.

    A child that reads it eats the rest of the file and the shell dies mid-run
    with "unexpected EOF", having silently skipped whatever it swallowed.
    """
    done = subprocess.run([str(INSTALL_SH), "--help"], capture_output=True, text=True,
                          cwd=str(REPO), stdin=subprocess.DEVNULL)
    assert done.returncode == 0
