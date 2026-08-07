# 007 — Docs stay in-repo; no GitHub wiki

**Status:** Decided, executing (2026-08-06)
**Workstream:** W10 — Documentation
**Author:** decision agent, per the Decision Agent Protocol (PLAN.md)

## Question

Where does published documentation live: the GitHub wiki, or `/docs` in this
repository?

## Options

### Option A — GitHub wiki

**Pros**
- Zero build tooling; edits are live immediately through GitHub's own UI.
- Familiar surface for casual contributors who don't want to clone.

**Cons**
- A wiki is a **separate git repository** (`<repo>.wiki.git`), with its own
  clone, its own history, and no shared commit with the code it documents.
- Docs stop moving in the same commit as the code change that motivates them,
  and leave PR review — nobody reviews a wiki edit the way they review a diff.
- Leaves `grep`/`ripgrep` — a doc claim can drift from the code with nothing
  in the repo pointing at it.
- Structurally incompatible with this repo's actual discipline: `CONTEXT.md`
  as canonical invariants, ADRs beside the decisions they record, dated
  evidence next to the thing it explains.

### Option B — `/docs` in-repo, optionally published as a static site

**Pros**
- Docs and code land in the **same commit**, reviewed in the **same PR**.
- `grep`-able, versioned with `git log`/`git blame`, diffable.
- Matches every existing convention in this repo (ADRs in `docs/adr/`, plans
  in `docs/plans/`, this very decision file).
- Can still be published somewhere polished — GitHub Pages, built from
  `/docs` in CI (see decision 009) — without giving up any of the above.

**Cons**
- Needs a build step (mkdocs) to get a browsable site; a raw GitHub wiki has
  none. Mitigated: the build is one `uv run mkdocs build` in CI, not a
  per-contributor burden.
- If a wiki is ever wanted for pure discoverability, it must be *generated*
  from `/docs` rather than hand-edited, or the two silently diverge — one
  extra CI job if that's ever taken up. Not built tonight; not needed while
  Pages exists.

## Decision

**Option B.** Docs stay in `/docs`, in this repository, and if a formal
published face is wanted, GitHub Pages is built from it in CI (decision 009).

## Why — the failure this avoids, observed tonight

This is not hypothetical. Tonight's docs audit
(`docs/overnight/w10a-docs-audit.md`) found `CONTEXT.md` asserting the
*opposite* of shipped suppression behaviour — `docs/plans/README.md:71` still
said suppression is scoped to Agent Session, in bold, when the code had been
re-keyed to Contributor. The fix landed safely only because the doc
correction and the code re-key were reviewable in the **same commit**, in the
**same repo**, against the **same diff**. A wiki structurally prevents that:
the two would have been two separate edits in two separate repositories, with
nothing forcing them to move together, and nothing catching it when they
didn't.

## How to undo it

Reversible in one PR: `git mv docs docs-archive`, seed a wiki from the
archived tree, and point `mkdocs.yml`'s `docs_dir` — or delete it entirely — at
whatever's left. Nothing about this decision writes to anything outside
`/docs`, `mkdocs.yml`, and `.github/workflows/docs.yml`, so undoing it is
deleting those and restoring the wiki, not unwinding application code.
