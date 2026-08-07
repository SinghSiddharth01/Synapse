"""User-level configuration for the installed CLIs — git-config style.

One file, ``~/.synapse/config.toml``, holding flat ``section.key = "value"``
pairs. Installation never writes it; ``synapse configure`` / ``synapse config
set`` do, and every value is re-settable at any time (the service URL changes
whenever the host machine changes networks, so it MUST be cheap to change).

Lives in ``synapse_contracts`` because both halves read it: the client CLI
(``synapse``) and the server CLI (``synapse-server``) are separate packages
that must not import each other, and contracts is the one package both
already depend on.

All values are stored as strings, exactly like ``git config``. Interpretation
(ints, booleans) belongs to the reader. Writes are atomic (tmp + rename) so a
killed ``synapse config set`` never leaves a half-written file.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

# The keys the CLIs know about, with the one-line meaning `synapse config
# list --help` shows. Unknown keys are still stored (forward compatibility —
# an older CLI must not destroy a newer CLI's setting), but the CLI warns.
KNOWN_KEYS: dict[str, str] = {
    "service.url": "base URL of the Synapse Service this client talks to",
    "user.contributor": "the name findings from this machine are attributed to",
    "client.distiller": "which model distils here: npu | anthropic | claude-cli | listen",
    "client.claude_model": "model override for the anthropic / claude-cli distiller arms",
    "client.worker": "on | off — start the passive Edge Worker with `synapse up`",
    "server.keys_file": "path to a file of inference-cloud API keys, one per line",
    "server.base_url": "inference-cloud base URL the server synthesizes against",
    "server.model": "synthesis model name (e.g. Llama-3.3-70B)",
    "server.synthesizer": "aic100 | npu | anthropic | fake",
    "server.host": "interface synapse-server binds (default 0.0.0.0)",
    "server.port": "port synapse-server binds (default 8899)",
}


def synapse_home() -> Path:
    """``$SYNAPSE_HOME`` or ``~/.synapse``. Never created here — readers must
    tolerate its absence, because a machine that was only ever installed has
    no configuration yet, and that is a state the CLIs report, not an error."""
    return Path(os.environ.get("SYNAPSE_HOME") or Path.home() / ".synapse")


def config_path() -> Path:
    return synapse_home() / "config.toml"


def state_dir() -> Path:
    """Where the installed client keeps bindings, WAL and logs.

    ``$SYNAPSE_STATE_DIR`` wins so the repo's own ``.synapse`` convention and
    the tests can relocate it; the default sits under the same home as the
    config so `rm -rf ~/.synapse` is a genuine full reset.
    """
    return Path(os.environ.get("SYNAPSE_STATE_DIR") or synapse_home() / "state")


def load() -> dict[str, str]:
    """The whole config as a flat ``{"section.key": "value"}`` mapping.

    A missing or unparseable file is an empty config, not an exception: every
    caller's next line is "is the thing I need set?", and a corrupt file
    should surface as "not configured — run `synapse configure`" plus the
    parse warning, never a traceback.
    """
    path = config_path()
    if not path.is_file():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    flat: dict[str, str] = {}
    for section, body in raw.items():
        if isinstance(body, dict):
            for key, value in body.items():
                flat[f"{section}.{key}"] = str(value)
        else:
            flat[section] = str(body)
    return flat


def get(key: str) -> str | None:
    return load().get(key)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _dump(flat: dict[str, str]) -> str:
    """Emit the flat mapping as TOML, grouped by section.

    A deliberate ~20-line emitter instead of a dependency: the file holds
    only string values in two-level ``section.key`` shape, and the escaping
    (backslash for Windows paths, quotes) is the entire hard part.
    """
    sections: dict[str, dict[str, str]] = {}
    for dotted, value in sorted(flat.items()):
        section, _, key = dotted.partition(".")
        if not key:
            section, key = "misc", section
        sections.setdefault(section, {})[key] = value
    lines: list[str] = []
    for section in sorted(sections):
        lines.append(f"[{section}]")
        for key, value in sorted(sections[section].items()):
            lines.append(f'{key} = "{_escape(value)}"')
        lines.append("")
    return "\n".join(lines)


def _write(flat: dict[str, str]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(_dump(flat), encoding="utf-8")
    tmp.replace(path)
    return path


def set_value(key: str, value: str) -> Path:
    """Set one ``section.key``. Returns the path written."""
    if "." not in key or key.startswith(".") or key.endswith("."):
        raise ValueError(f"config keys are 'section.key', got {key!r}")
    flat = load()
    flat[key] = value
    return _write(flat)


def unset(key: str) -> bool:
    """Remove a key. True if it was present."""
    flat = load()
    if key not in flat:
        return False
    del flat[key]
    _write(flat)
    return True
