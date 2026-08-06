# Contributing

For a new contributor: how the repo is laid out, how to get the tests running, what the tests expect of you, the docs discipline this repo enforces (some of it with a failing test, not just a convention), the commit style visible in `git log`, and where to plug in a new agent adapter or a new model provider.

## Repo layout

Synapse is a `uv` workspace: one root `pyproject.toml` at the repo root with `[tool.uv.workspace] members = ["packages/*"]`, and six packages under `packages/`, each its own installable distribution with its own `pyproject.toml` and its own `tests/` directory:

| Package | Distribution name | What it owns |
|---|---|---|
| `packages/contracts` | `synapse-contracts` | The frozen cross-track schemas (`Finding`, `Attribution`, `LocalBinding`, …) every other package imports. Change here ripples everywhere — see "Docs discipline" below. |
| `packages/providers` | `synapse-providers` | `ModelProvider` implementations — the abstraction every distiller/synthesizer call goes through (`packages/providers/src/synapse_providers/base.py`). |
| `packages/distiller` | *(no console script; imported by `worker`)* | Segment → `Finding[]` distillation and the capability/budget derivation (`capability.py`). |
| `packages/worker` | `synapse-worker` | The Edge Worker: follows a live transcript, segments it, triages, distils on-device, pushes upstream. Owns the agent-adapter registry (`discovery.py`). |
| `packages/orchestrator` | `synapse-orchestrator` | The local hub: MCP server, single egress/ingress point per machine, session lifecycle tools. |
| `packages/service` | `synapse-service` | The remote Shared Memory: ingest, fold, synthesis, retrieval, HTTP API. |

Outside `packages/`:

- `scripts/` — operational entry points, chiefly `scripts/serve_local.py` (boots the service, orchestrator, and — unless `--npu` — a local model stand-in, in one process).
- `packs/` — content shipped to a contributor's own tooling, e.g. `packs/claude-code/` (the MCP install doc and the `synapse-shared-memory` skill).
- `fixtures/` — recorded transcript corpora used by the distiller/worker test suites and by manual evals.
- `config/synapse.toml` — every runtime knob (model choice, prompt pack, segment budget, provider capability records), each overridable by an environment variable named in the file next to the value it overrides.
- `docs/` — see "Docs discipline" below; `docs/adr/` for architecture decisions, `docs/plans/` for the original design documents (historical once superseded — check a plan's own status note before trusting it).
- `tests/` — repo-level tests that don't belong to one package: vocabulary/doc-truth checks and the awareness-pack content tests (see below).

There are no package-level `README.md` files today; the docstring at the top of each package's main module (e.g. `packages/worker/src/synapse_worker/discovery.py:1`) is that package's entry-point documentation.

## uv workspace basics

```bash
uv sync              # installs the workspace and all six packages, editable
uv run pytest        # the whole suite — packages/ and tests/, per testpaths below
uv run pytest -q     # quiet form; used as the "are we green" check before pushing
```

`uv sync` and `uv run` operate on the whole workspace from the repo root — you do not `cd` into a package to work on it. `[tool.uv.sources]` in the root `pyproject.toml` pins each `synapse-*` name to `{ workspace = true }`, so an edit to `packages/contracts` is immediately visible to every other package's imports without a reinstall.

**Windows on ARM64:** point `uv` at the ARM64 interpreter explicitly before `uv sync` (a bare `uv venv` can silently provision an emulated x86_64 environment, which breaks NPU wheel installs), and pin `mcp==1.9.4` — newer `mcp` releases pull in a `cryptography` dependency with no ARM64 Windows wheel. See `docs/JOIN-WINDOWS.md`.

### Running a single package's tests

`[tool.pytest.ini_options] testpaths = ["packages", "tests"]` in the root `pyproject.toml` is what makes a bare `uv run pytest` collect all six packages plus the repo-level `tests/` directory. To scope to one package or one file:

```bash
uv run pytest packages/service -q
uv run pytest packages/worker/tests/test_triage.py -v
uv run pytest packages/service/tests/test_synthesis.py -v -k merge
```

This is the pattern used throughout the plan execution logs in `docs/plans/exec/` — narrow while iterating on one file, then widen to the owning package, then the full suite, before calling a change done.

## Test conventions

- **Framework:** `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"` — async test functions need no decorator), `pytest-httpserver` for HTTP-boundary tests, `pytest-cov` for coverage.
- **Location:** each package's tests live in its own `packages/<name>/tests/` directory, mirroring the package's `src/` modules by filename (`test_store.py` tests `store.py`, etc.). Repo-level tests that check documentation rather than one package's code live in the top-level `tests/`.
- **Scale:** the suite is large — four figures short of a thousand at the time of writing, and this page deliberately does not say which. Do not hand-copy a test count into a doc; it drifts every session, and a static `def test_*` grep and pytest's own collection do not even agree (parametrised cases multiply). Derive it when you need it (`uv run pytest --collect-only -q | tail -1`) rather than typing a remembered figure — stale test-count claims are a recurring, specifically-flagged failure mode in this repo's docs (see `docs/overnight/w10a-docs-audit.md` item 14).
- **Coverage exclusion:** the `if __name__ == "__main__":` guard in every CLI module is excluded from coverage (`[tool.coverage.report] exclude_lines` in `pyproject.toml`) because it only runs on direct invocation, not under pytest — `scripts/verify_orchestrator.py` is what actually exercises it live. This keeps 100% a reachable target instead of a number nobody can hit.
- **Tests as a doc-truth mechanism:** two files in the top-level `tests/` exist specifically to stop documentation from silently going wrong:
  - `tests/test_vocabulary.py` asserts that every term the plans use in prose (`Triage`, `Distiller`, `Edge Worker`, `Tombstone`, `View`, `Lane`, `Candidate`, `Lane yield`, `Fold`, `Topic`) still has a bolded definition in `CONTEXT.md`. It exists because a `git merge` resolution can delete a definition with no error anywhere else. It checks *presence*, not *truth* — a definition can exist and still assert something the code no longer does, which a doc audit has to catch by reading code, not by running this test.
  - `tests/test_awareness_pack_content.py` pins the content of `packs/claude-code/skills/synapse-shared-memory/SKILL.md` and its install doc — the exact MCP tool names it may claim to exist, the trigger-voiced description, the install path. If you edit that skill file, expect this test to tell you what broke.

  If you're editing `CONTEXT.md` or anything under `packs/claude-code/`, run these two files first; they are cheap and catch the class of mistake most likely to bite a docs change.

## Docs discipline

This repo has been burned by docs that say the opposite of what the code does, in places a new contributor reads first (see `docs/overnight/w10a-docs-audit.md` for the catalogue). The rules that exist to prevent a repeat:

- **`CONTEXT.md` is the canonical vocabulary.** It defines every term (`**Term**:` with an `_Avoid_:` list of words not to use instead) that the rest of the docs and the plans are written against. If you introduce a new concept, define it there first — `tests/test_vocabulary.py` enforces the definitions stay present, though not that they stay accurate.
- **ADRs live beside the decisions they record, in `docs/adr/`, numbered sequentially** (`0001`…`0005` today). Each has a `**Status:**` line and, where relevant, a `**Amends:**` line naming the ADR it supersedes — the amended ADR's *intent* is typically preserved while its *mechanism* changes (see `docs/adr/0004`'s header). When new information corrects or extends an already-Accepted ADR, the convention is to **append a dated, attributed amendment section** rather than silently rewrite the original text — `docs/adr/0004-the-log-is-append-only-and-state-is-a-fold.md`'s `## Amendment (2026-08-05) — <author>` section is the pattern to follow: the original text stays unedited above it, the amendment is a separate section, and it says explicitly what it adopts vs. corrects vs. closes.
- **Dated evidence, not remembered evidence.** Numbers (test counts, measured latencies, token budgets) go in docs next to the command or the date that produced them, not as a bare figure — see how `config/synapse.toml` annotates every capability record with when and how it was measured versus which are "PROVISIONAL" or "illustrative, not measured." A number with no provenance is the pattern that goes stale first.
- **No AI slop.** Terse, evidence-driven prose; no filler, no restating the obvious, no aspirational claims about what a component "will" or "should" do — describe current behaviour, cite the file and line that shows it.
- **Historical documents stay historical.** Point-in-time reports (`docs/2026-08-04-implementation-report.md`, `docs/2026-08-05-service-implementation-report.md`) and completed execution logs (`docs/plans/exec/*.md`) are dated and describe a past state on purpose — don't "fix" them to match current code; if one is actively misleading, add a dated correction note rather than rewriting history.

## Commit and branch conventions

Commit subjects in this repo follow `git log --oneline`:

```
<type>(<scope>): <description, present tense, no trailing period>
```

Types observed: `feat`, `fix`, `docs`, `test`. Scope is almost always the package or area touched — `fix(worker)`, `feat(lifecycle)`, `docs(adr)`, `docs(join)`, `test(ci)`, `fix(providers)` — matching a `packages/` directory name, a doc area, or a cross-cutting concern (`lifecycle`, `scripts`, `awareness`). Descriptions favor the *reason* over the *mechanism* where the two would otherwise both fit — e.g. `fix(mcp): tell the agent what a hit MEANS, not just when to ask`, `fix(service): the small-session bypass was skipping ORDER, not just selection` — a habit worth keeping: a reviewer reading `git log` should learn why something changed, not just that it did.

## Adding a provider

A `ModelProvider` is the one abstraction every distiller/synthesizer call goes through (`packages/providers/src/synapse_providers/base.py`): implement `capabilities` (a `ProviderCapabilities` — chiefly whether the provider guarantees schema-valid structured output or needs tolerant parsing) and `async complete(messages, response_schema=None) -> ModelResult`. Existing implementations in `packages/providers/src/synapse_providers/` — `aic100.py`, `npu.py`, `anthropic_provider.py`, `claude_cli_provider.py`, `openai_compat.py`, `fake.py`, `recording.py` — are the range of shapes to model a new one on (cloud HTTP API, local CLI subprocess, OpenAI-compatible endpoint, and test doubles). A new provider also needs a measured `CapabilityRecord` in `config/synapse.toml`'s `[capability."<model-id>"]` table — `usable_context` and `prefill_toks_per_sec` must be *measured on the target*, not taken from a model card (`packages/distiller/src/synapse_distiller/capability.py` explains why the QAIRT bundle specifically burned this once).

## Adding an agent adapter

The worker detects which coding agent it's watching rather than being told; `AGENT_REGISTRY` in `packages/worker/src/synapse_worker/discovery.py` is the dispatch table — one entry per agent, each an `AgentRegistration` of `roots` (where that agent writes transcripts), a `finder` function, a `source_class`, and a transcript `dialect`. The two existing entries (`claude-code`, `codex`) are the template: a third adapter needs a new registry entry and a `find_*_transcripts` function, not a change to `find_live_transcript`, `resolve_transcript`, or `join_session`, all of which already dispatch through the registry.
