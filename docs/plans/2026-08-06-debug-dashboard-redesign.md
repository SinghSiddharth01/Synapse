# Debug dashboard redesign — shadcn design language

**Date:** 2026-08-06 · **Scope:** `packages/service/src/synapse_service/debug.py` (`/debug` brain page, `/debug/log` log page)

## Why

Both service debug pages read as AI slop: glowing status dots, uppercase-tracked
micro-labels above every section, all-mono typography, ad-hoc teal-on-teal
panels, arrow-linked stat rail. The data model underneath is good; the skin is
not.

## Design read

Internal operator dashboard for a developer tool. shadcn/ui "new-york" dark
language, hand-rolled as CSS tokens (these pages must make **zero external
requests** — no CDN, no font, no npm build — so the shadcn look is encoded as
tokens, not imported as React components).

- **Base palette:** zinc dark (`--background #0b0b0d`, `--card #141417`,
  `--border #27272d`, `--muted-foreground #a3a3ac`).
- **One accent:** the existing Synapse teal (`#56c8cf`). It is the device
  boundary's brand (teal = cloud/service, copper = worker) and is preserved.
- **Semantic data colors** (merged green, trivial amber, topic blue, query
  purple, failed red) are data encodings, not decoration — kept, recalibrated
  to sit on zinc.
- **Typography:** system sans for UI text; mono reserved for numbers, IDs,
  timestamps, and enum tags. No more everything-is-mono.
- **Shape:** 10px radius for cards/containers, 6–7px for badges, inputs,
  inset blocks. One scale.
- **Density:** compact dashboard (cards `14–16px` padding, 12px grid gap).
- **Motion:** hover states and 120ms background transitions only.

## Component plan (shadcn vocabulary)

| Region | Treatment |
|---|---|
| Header | 56px app bar: brand mark + wordmark, nav as pill links (Brain / Log / Memory-soon), session Select on the right |
| Unreachable banner | destructive Alert strip (id/behavior contract unchanged) |
| Stat rail | Card grid (log: 5-up with the fold card widest; brain: 4-up). Label sentence-case `text-xs muted`, value `22px mono semibold` |
| Topics | secondary Badges |
| Working memory | Card with header row (title + meta), prose body, revisions as hoverable table rows |
| Participants | shadcn Table styling: medium-weight normal-case headers, hairline rows, row hover, state as outline Badges (the dotted-underline "not a member" caveat styling is deliberate and stays) |
| Log tail / Activity feed | Card of rows; the 2px left color-key per kind/tag stays (data encoding); expanded detail as inset panel |
| Filter chips | toggle Badges with semantic-color dots |
| Footnote | `text-xs` muted prose, em-dash prose rewritten (the `—` null-glyph in data cells stays — it is the standard tabular null marker and the footnote documents it) |

## Hard constraints

1. **JS is untouched.** `test_service_debug_page_js.py` executes the real
   script under a minidom; every element ID (`banner`, `chips`, `feed`,
   `log-tail`, `session-select`, `stat-*`, `topics`, `wm-body`, `purpose`,
   `ident`, `wm-meta`, `rev-count`, `revisions`, `participants`, `recent`)
   and every JS-emitted class (`.entry`, `.detail`, `.expanded`, `.chip`,
   `.topic-chip`, `.rev`, `.row`, `.pill`, `.state`, `.adot`, `table.roster`,
   `.empty`, …) keeps its name and semantics.
2. **Zero external requests** — the offline-demo property is load-bearing.
3. Routes, JSON payloads, and mount points unchanged.

## Verification

- `pytest packages/service/tests/test_debug.py packages/service/tests/test_service_debug_page_js.py`
- Boot the app with a seeded session, screenshot both pages in the browser.

## Follow-up (not in this change)

- `packages/worker/src/synapse_worker/debug_server.py` — same treatment on
  the copper side, once this lands.
