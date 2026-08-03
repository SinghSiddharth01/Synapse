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

Per-user distillation runs constantly, so it must be local, private, and power-efficient. The Snapdragon X Elite's Hexagon NPU makes always-on observation viable without stealing CPU cycles from the developer's work, with pre-optimized models from Qualcomm AI Hub. Cross-team synthesis needs a large model serving many users at low cost — the sustained-inference workload Cloud AI 100 is built for.

Edge distillation on Snapdragon plus cloud synthesis on Cloud AI 100 is the division of labor this hardware was designed for.

## Technologies

- **On-device SLM inference** on the Snapdragon X Elite NPU
- **Qualcomm Cloud AI 100** for synthesis and retrieval
- **MCP** (Model Context Protocol) for the agent-facing interface
- **Claude Code** as the demo vehicle — the design is agent-agnostic

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
