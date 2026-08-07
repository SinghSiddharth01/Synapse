# Uninstall mechanism — design, parked for later

Status (2026-08-06): **no first-class uninstall exists.** Because both halves
install as isolated uv tools, removal of the software itself already works:

```bash
uv tool uninstall synapse-cli        # client: synapse + its orchestrator/worker env
uv tool uninstall synapse-service    # server: synapse-server + synapse-service
```

That removes the venvs and the PATH entries and nothing else. Four things are
left behind, and they are the reason an uninstall command should exist:

1. **`~/.synapse/`** — config.toml and state (bindings, WAL, logs). Keeping it
   is right for a reinstall/upgrade; purging it is right for "remove Synapse".
   Those are different intents and need different flags.
2. **MCP registrations** — the `synapse` entry in each project's `.mcp.json`.
   A stale one makes every future Claude Code session in that project try to
   connect to an orchestrator that no longer exists.
3. **Awareness pack entries** under `~/.claude/` (skills/commands the pack
   copied — this surface is growing with the E9 pack work).
4. **Running processes** — uninstalling while `synapse up` is running leaves
   orphans holding :8787/:18181.

## Decided shape

`install.sh uninstall` / `install.ps1 -Component uninstall` — in the
INSTALLER, not the CLI, because an uninstaller must work when the installed
CLI is broken, and the installer script is already the lifecycle boundary
(install is install; uninstall is its mirror, and neither touches config).

Default behaviour:

- stop running Synapse processes first (same discipline as `--clean` used to
  have: only our own process names/ports, never `geniex serve`);
- `uv tool uninstall` whichever halves are installed (absent-is-fine);
- **keep `~/.synapse/`**, and say so plus its path;
- **never silently touch user/project space**: list every found MCP
  registration and pack entry with the exact removal command
  (`claude mcp remove synapse` in <project>, `rm -r ~/.claude/skills/…`)
  rather than deleting files the user may have edited.

Flags:

- `--purge` — also delete `~/.synapse/` (config, state, logs). Destructive;
  named accordingly in the output.
- Refuse the same configure-stage flags install refuses, for symmetry.

Acceptance:

- fresh machine → install client+server → configure → up → Ctrl-C →
  `install.sh uninstall` leaves no `synapse*` on PATH, no listeners on
  8787/8899/18181, `~/.synapse` intact; `--purge` leaves no `~/.synapse`;
- uninstall with nothing installed exits 0 and says so (idempotent);
- CI: extend the install-path job with an uninstall leg (install → uninstall
  → assert clean), mirroring how the install legs are executed, not linted;
- README: one "Uninstall" subsection under Getting Started.

Estimated: ~1 hour including tests. Nothing blocks it; parked by choice
(2026-08-06 review: "save it in docs for later").
