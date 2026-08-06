# Synapse

> **Shared Working Memory for AI-Assisted Teams**

## The Problem

Every engineer now works alongside an AI coding agent, but each agent is blind to the team. When several people work toward the same goal, their agents repeat the same explorations and duplicate hours of work. What teams lack is a passive listener — running on the Copilot+ PC each member already has — that quietly captures what every agent learns and turns it into shared knowledge.

## The Solution

Synapse is that passive listener: it observes any coding agent's session unmodified and turns isolated agents into a shared team intelligence.

A teammate creates an opt-in shared session from their Copilot+ PC, declaring its purpose; teammates join with one command. Each member holds a different slice of context for the same task, and today those slices never meet. Synapse pools what each agent learns so the team builds on shared knowledge rather than rediscovering it.

## How It Works

**Edge (Snapdragon X Elite).** The Copilot+ PC acts as the control surface. A lightweight worker on each PC observes the local agent's activity, and a small language model distills it into structured findings — key learnings, decisions, dead ends, and open questions — each tagged with contributor and time. Raw work stays on the device. Only distilled findings stream to the shared context service.

**Cloud (Qualcomm Cloud AI 100).** The shared context service runs a large model that merges everyone's findings into one shared working memory: deduplicating, flagging conflicts, and organizing against the session's purpose.

**Retrieval (MCP).** Agents retrieve on demand through simple MCP commands; nothing is injected unprompted. Queries are natural language, and the service returns only relevant, ranked results. When one member's agent learns something, every teammate's agent can build on it minutes later rather than arriving at it independently.

## Why Qualcomm's Connected Ecosystem

Per-user distillation runs constantly, so it must be local, private, and power-efficient. The Snapdragon X Elite's Hexagon NPU runs that always-on distillation off the critical path — leaving CPU and GPU free for the developer's own compile/test loop — with models served locally through GenieX. Cross-team synthesis needs a large model serving many users at low cost — the sustained-inference workload Cloud AI 100 is built for.

Edge distillation on Snapdragon plus cloud synthesis on Cloud AI 100 is the division of labor this hardware was designed for.

## Technologies

- **On-device SLM inference** on the Snapdragon X Elite NPU
- **Qualcomm Cloud AI 100** for synthesis and retrieval
- **MCP** (Model Context Protocol) for the agent-facing interface
- **Claude Code and Codex** as the demo pair — the worker auto-detects which agent is running; the design is agent-agnostic

## Five-Day Plan

| Days | Focus |
|------|-------|
| 1–3 | Build and validate each component independently — on-device distillation, cloud synthesis, memory, and retrieval — so every piece is proven before anything depends on it |
| 4–5 | Integrate into the end-to-end pipeline and prepare the demo |

## Stretch Goals

- Mobile contributions (photos and voice notes)
- Knowledge persisting across sessions
- A team dashboard

## Impact

Our demo highlights one concrete use case: a team debugging a shared issue, combining a lab engineer's on-target context with a developer's code context. The architecture applies to any collaborative work where AI agents operate alone — from incident response to code review to design exploration.

## Getting Started

### Prerequisites

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) for dependency management and running the workspace
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) as the agent to connect (the worker auto-detects the agent product; Codex support is unbuilt — see Stretch Goals)
- (Optional) a Snapdragon X Elite machine running `geniex serve` on `:18181` for real on-device NPU inference, and Cirrascale/Cloud AI 100 credentials for real cloud synthesis. Without either, `scripts/serve_local.py` (below) starts a model stand-in automatically, so the full pipeline is runnable on any machine.

### Setup (from scratch)

```bash
git clone https://github.com/SinghSiddharth01/Synapse.git
cd Synapse
uv sync
```

> **Windows on ARM64:** point `uv` at the ARM64 interpreter explicitly (e.g. `uv venv --python <path-to-arm64-python.exe>` before `uv sync`) — a bare `uv venv` can silently provision an emulated x86_64 environment, which breaks NPU wheel installs. Also pin `mcp==1.9.4`; newer `mcp` releases pull a `cryptography` dependency with no ARM64 Windows wheel.

### Run it

The fastest way to see Synapse end to end, with no NPU or cloud credentials required, is `scripts/serve_local.py`. It starts the Synapse Service, the Orchestrator, and a model stand-in, then creates (or joins) a Shared Session:

```bash
uv run python scripts/serve_local.py --purpose "my first shared session"
```

This prints a Shared Session id and an MCP URL. Connect Claude Code to it:

```bash
claude mcp add --transport http --scope project synapse http://127.0.0.1:8787/mcp
```

Start a **new** Claude Code session in that project and approve the `synapse` server when prompted — the `mcp__synapse__query` and `mcp__synapse__contribute` tools then become available (verify with `/mcp` inside the session). A second teammate joins the same Shared Session by re-running `serve_local.py --shared-id <the id printed above>` from their own machine, or by running `synapse-worker join <shared_id>` to passively observe an existing Claude Code transcript instead of calling `contribute` directly.

Add `--npu` to use a real GenieX NPU seam — `serve_local.py` starts `geniex serve` itself if `:18181` is free, adopts one that is already running otherwise, and supervises it either way: it probes `GET /v1/models` every 15s and restarts the seam if it stops answering, because `geniex serve` is known to keep its process alive while its HTTP server goes silent. Every transition prints to your terminal and to `.synapse/logs/supervisor.log`. Add `--live` to proxy the model seam to a real cloud model instead of the stand-in, or `--npu --live` together for the split production topology: distil on the local NPU, synthesize on the real 70B.

### Run the test suite

```bash
uv run pytest
```

### Further reading

- [`docs/demo-script.md`](docs/demo-script.md) — the full scripted walkthrough (multiple contributors, cross-teammate retrieval, and recovery after a service restart)
- [`packs/claude-code/INSTALL.md`](packs/claude-code/INSTALL.md) — optional awareness hooks that make shared-memory updates surface proactively inside a Claude Code session, rather than only on demand via `query`
- [`docs/architecture.html`](docs/architecture.html) — architecture deep-dive
- [`CONTEXT.md`](CONTEXT.md) — vocabulary and design invariants

## Team

| Name | Email |
|---|---|
| Aditya Thagarthi Arun | adityata98@gmail.com |
| Siddharth Singh | sid17011998@gmail.com |
| Akhil Agrawal | agrawal.akhil14@gmail.com |

## License

MIT — see [`LICENSE`](LICENSE).
