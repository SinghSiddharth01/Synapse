"""synapse — the client CLI.

Lifecycle, strictly separated (each stage does ONLY its own job):

  install      puts binaries on the machine (install.sh / install.ps1 — or
               `uv tool install synapse-cli`). Writes no config, starts nothing.
  configure    `synapse configure` / `synapse config set` — service URL,
               contributor, distiller arm. Re-runnable any time.
  run          `synapse up` — spins up the orchestrator, the Edge Worker and
               (if configured) the model seam, in the foreground. Nothing is a
               daemon; Ctrl-C stops the lot.
  inspect      `synapse health` — what is configured, what is running.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
