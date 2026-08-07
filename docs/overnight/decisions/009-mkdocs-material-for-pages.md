# 009 — MkDocs + Material + mkdocstrings for the published site

**Status:** Decided, executing (2026-08-06)
**Workstream:** W10 — Documentation
**Depends on:** decision 007 (docs stay in-repo)
**Author:** decision agent, per the Decision Agent Protocol (PLAN.md)

## Question

Given docs stay in-repo (decision 007) and a published, browsable face is
wanted, what toolchain builds it — and specifically, how does the repo get an
API reference out of Python docstrings without hand-maintaining one?

## Options

### Option A — Sphinx + autodoc

**Pros**
- The traditional, most widely deployed answer for Python API docs.
- `autodoc`/`napoleon` are mature and well understood.

**Cons**
- reStructuredText-first. This repo's entire `/docs` corpus is Markdown —
  every existing file (`JOIN.md`, the ADRs, the plans) would need MyST plus
  configuration just to render at all. Real, immediate cost for zero gain on
  content that already exists and is fine.
- Heavier config surface for a hackathon-timescale docs pass.

### Option B — pdoc

**Pros**
- Zero-config, genuinely pleasant output for pure API reference.
- Fastest to stand up of the three.

**Cons**
- No home for hand-written, high-level material — and that is *most* of what
  this repo has (architecture, troubleshooting, first-run story, ADRs,
  install guides for two OSes). pdoc is an API-reference tool, not a docs
  site; adopting it would mean either a second tool for everything else, or
  losing the prose docs.

### Option C — MkDocs + Material theme + mkdocstrings[python]

**Pros**
- Markdown-native — every existing `/docs` file (and the six writer files
  landing tonight) works unchanged; no format migration.
- `mkdocstrings[python]` renders Python docstrings into an API reference
  directly from `packages/*/src`, via a `::: module.path` directive in a
  plain Markdown page — six thin stub pages here
  (`docs/reference/api/*.md`), one per package.
- `mkdocs gh-deploy` / `actions/deploy-pages` publishes to GitHub Pages in one
  CI job, no extra hosting to provision.
- Material is the theme most Python projects in this space now use — search,
  dark/light mode, and a nav structure that scales to this repo's size, with
  no custom CSS required to look production-grade.
- This repo's docstrings are unusually good source material: they already
  explain *why*, name rejected alternatives, and carry dates (e.g.
  `anthropic_provider.py`'s "four structural breaks",
  `packages/orchestrator/src/synapse_orchestrator/app.py`'s post-review
  amendment). They render as real documentation, not restated signatures.

**Cons**
- One more dependency group (`docs`) and one more CI job. Scoped tightly:
  `mkdocs`, `mkdocs-material`, `mkdocstrings[python]`, isolated to a
  `dependency-groups.docs` entry that touches nothing else in `pyproject.toml`.
- The six writer files (`index.md`, `first-run.md`, `architecture.md`,
  `reference/service-http.md`, `troubleshooting.md`, `contributing.md`) don't
  exist yet at SETUP time, so `mkdocs.yml` ships `strict: false` with
  `validation.nav.not_found: warn` tonight — flip both once they land (the
  flip instruction is a comment directly in `mkdocs.yml`).

## Decision

**Option C — MkDocs + Material + mkdocstrings[python].**

## Transcript / requirement alignment

The requirement was explicit: "the standard Python way to pull code-level
docs out of docstrings, with high-level docs alongside." That is precisely
what autodoc-style tools solve, and precisely where Sphinx's RST-first
assumption and pdoc's API-only scope each fail one half of the ask. MkDocs +
mkdocstrings is the only option of the three that keeps the existing Markdown
corpus intact **and** ships a real API reference from the same docstrings the
codebase already writes carefully.

## How to undo it

Reversible without touching application code: delete `mkdocs.yml`, the
`docs` dependency-group in `pyproject.toml`, `.github/workflows/docs.yml`,
and the `docs/reference/api/*.md` stub pages. `/docs` itself is untouched —
every prose file still exists and reads fine as plain Markdown with no
toolchain at all. Switching toolchains later (e.g. to Option A or B) means
replacing only those same four artifacts.

## Repo settings this needs, once

GitHub → Settings → Pages → "Build and deployment" → Source: **GitHub
Actions** (not "Deploy from a branch"). Documented alongside the workflow
itself in `.github/workflows/docs.yml`.
