# Debug dashboard design system

**Where the design actually lives:** there is no build step and no shared CSS
file. Each page in `packages/service/src/synapse_service/debug.py` (`_HOME_PAGE`,
`_BRAIN_PAGE`, `_PAGE` for the log, `_MEMORY_PAGE`) carries its own `<style>`
block whose first rule is a `:root { --token: value }` set. **Editing this file
does nothing by itself**: it documents the system; to change the look, change
the token blocks in `debug.py` (they are kept byte-identical across all four
pages, so a global find-and-replace on a token line updates every page at once).

## The system (2026-08-07: HashiCorp-inspired dark)

The language is derived from `DESIGN-hashicorp.md` at the repo root: a
near-black canvas, charcoal surface lift instead of shadow elevation, 1px
translucent hairlines that are felt more than seen, and a set of **saturated
per-meaning identity hues** doing the work a single accent used to do. Display
type is large and tight (up to 80px at -0.03em on the home hero); body type is
smaller and relaxed. Elevation is expressed by surface change
(canvas → surface-1 → surface-2), never by drop shadow; the only shadows in the
system are the moderated colored glows on showcase-tier cards.

## Tokens (identical `:root` block on all four pages)

| token | value | role |
|---|---|---|
| `--canvas` | `#000000` | page ground; also the sunken inset inside cards |
| `--surface-1` | `#15181e` | default card / panel surface |
| `--surface-2` | `#1f232b` | table header bands, hover fills, secondary buttons |
| `--surface-3` | `#2a2e37` | pressed / third-step lift |
| `--hairline` | `rgba(178,182,189,0.14)` | card borders |
| `--hairline-soft` | `rgba(178,182,189,0.07)` | row separators, shell dividers |
| `--ink` | `#ffffff` | headlines, primary values |
| `--ink-muted` | `#b2b6bd` | body copy, labels |
| `--ink-subtle` | `#656a76` | timestamps, footnotes, empty states |
| `--cyan` / `--cyan-deep` | `#14c6cb` / `#12b6bb` | THE service hue: cloud side, selection, active tab, fold showcase |
| `--green` | `#00ca8e` | merged / synthesized / success / active |
| `--amber` | `#ffcf25` | trivial / warning |
| `--red` / `--red-text` | `#e62b1e` / `#f5564a` | failure, throttled, ended; `-text` is the same hue lifted for small text on dark |
| `--purple` / `--purple-bright` / `--purple-text` | `#7b42bc` / `#911ced` / `#b78ae8` | queries, listening, contributed |
| `--blue` / `--blue-text` | `#1868f2` / `#6ea6ff` | topics, finding types |
| `--copper` / `--copper-dim` | `#e09a5a` / `#8a5a2d` | the edge side of the device boundary (home network + Edge showcase card) |
| `--radius` / `--radius-sm` | `12px` / `8px` | containers / buttons, inputs, inner chips |

Semantic colors are **data encodings, not decoration**: green means merged,
amber means trivial, purple means query, blue means topic, red means something
is wrong, cyan means the service itself. Do not reassign them per-page. The
base hue paints washes, borders and left edges; the `-text` variant exists
because `#7b42bc`-class hues do not clear contrast for 12px text on black.

## Card tiers

1. **Default**: `--surface-1` + 1px `--hairline` + `--radius`. No shadow.
2. **Showcase** (one per composition, never a whole row): the default card
   lifted by a chromatic gradient in its identity hue, a ~0.4-alpha colored
   border, and a moderated glow, e.g. the fold stat card:
   `linear-gradient(135deg, rgba(20,198,203,0.26), rgba(20,198,203,0.05) 52%, ...), var(--surface-1)`
   with `box-shadow: 0 0 60px -24px rgba(20,198,203,0.45)`.
   Home showcase tier: the `0.1% variance` hero stat (cyan, spans two rows),
   the Edge/Cloud split cards (copper/cyan), the aha panel (green, radius 24).
3. **Inset**: `--canvas` panels inside cards (expanded details, terminal).

Composition rule: kill equal boxes-in-a-row. One card per row is the hero
(grid-row/column spans); support cards stay at tier 1.

## Typography

- System sans (`--sans`) everywhere; mono (`--mono`) only for numbers, IDs,
  timestamps, enum tags: this is an in-product surface, mono is data voice.
- Home hero: `clamp(52px, 5.8vw, 80px)/1.15`, weight 700, -0.031em.
- Section h2 (home): `clamp(28px, 2.8vw, 38px)`, 650, -0.024em.
- Brain purpose / memory h1: `clamp(24px, 2.3vw, 32px)`, 650, -0.022em.
- Stat values: 30px mono 650 (home hero stat up to 68px).
- Eyebrows and table headers: 11-12px, 600, uppercase, +0.04-0.06em tracking.
- Body: 13.5-16px at 1.55-1.7 line-height. The display-tight / body-relaxed
  contrast is the voice; keep it.

**The kicker device:** every section heading carries a short gradient bar
(`h2::before`, 34-46px wide, 3-4px tall, `linear-gradient(90deg, <identity hue>,
transparent)`). On the home page each section sets `--sec-c` to its identity
hue (`sec-why` red, `sec-pipe`/`sec-split` copper, `sec-cases` blue, `sec-aha`
green, everything else cyan): this is the eyebrow system without adding words.
The brain identity strip uses the vertical form via `border-image`.

## Motion

- **Home page: AnimeJS v4.5** (`anime.umd.min.js`, 118KB, inlined verbatim in
  its own `<script>` block ahead of the page script; the pages may make zero
  external requests, so the library must live in the markup). The UMD wrapper
  exposes the `anime` namespace global; the page uses `anime.animate`,
  `anime.stagger`, `anime.svg.createMotionPath`.
  - Hero entrance: staggered rise/fade of h1 → tagline → CTAs → live row → net.
  - Network: the SMIL impulse dots were replaced by anime motion-path
    animations along the four axon paths (forward dots white, return dots cyan
    with `reversed: true`, staggered durations/delays); the hub breathes
    (`r` attribute tween, `alternate: true`) and the ring pulse is an anime
    loop on `r` + opacity.
  - "Measured, not claimed": count-ups animate a plain `{v}` object and write
    `textContent` on `onUpdate` (`.cu` spans carry `data-n` / `data-dec` /
    `data-sep`; the real numbers stay in the markup so no-JS renders complete).
  - Scroll reveals: IntersectionObserver drives anime rise/fades per
    `[data-reveal]` section, implemented as a **sweep**: every observer tick
    and scroll event reveals ALL still-hidden sections whose top has entered
    the viewport. (IO coalesces entries during fast jumps; a per-entry
    `isIntersecting` check permanently lost sections. A missed event may only
    delay a reveal, never lose one.)
  - Everything is gated: `prefers-reduced-motion: reduce` or a failed library
    load skips all of it, and initial styles are set from JS so the static
    page is complete without it.
- **Brain / log / memory: CSS only.** The one `<script>` block on the brain
  and log pages is executed by the minidom contract tests, so the anime
  library must NOT be added there. Each page has a one-shot entrance
  (`@keyframes rise` on `main > *` with nth-child delay steps, `backwards`
  fill, wrapped in `@media (prefers-reduced-motion: no-preference)`): it runs
  once at load on the static containers, so the 1-2s polling rebuilds never
  re-trigger it. Everything else is 120-140ms hover/border transitions.

## Depth and selection rules

- Selection carries cyan, never another grey: active tab and active sidebar
  session use `rgba(20,198,203,0.10-0.12)` fills with a 0.4-alpha cyan border.
- Badge tints sit at ~12% alpha with 0.4-0.45-alpha borders of the same hue;
  pills are fully rounded (999px), buttons and inputs are `--radius-sm` 8px.
- Color-keyed left edges on rows are 3px; `Merged` and `query_failed` rows add
  an 8%-alpha wash of their hue across the whole row.
- The home primary CTA is white-on-black (`--ink` ground, black text), the
  HashiCorp move; ghost buttons are `--surface-2` with a hairline.

## Hard constraints (test-enforced; run before believing anything)

1. **Zero external requests**: no CDN, font, or image; `http://` / `https://`
   may not appear anywhere in the served home/brain/memory pages (including
   SVG `xmlns`, so inline SVG omits it) and `<script src` is banned. The
   `href="data:,"` favicon line must stay.
2. **Scripts are contract**: `test_service_brain_page_js.py` and
   `test_service_debug_page_js.py` extract `<script>(.*)</script>` (greedy,
   DOTALL) from `/debug` and `/debug/log` and execute it under
   `tests/support/minidom.js`: those two pages must keep exactly ONE
   attribute-less script block, and every id / emitted class keeps its name.
   Restyle freely; rename nothing the scripts touch.
3. Required ids: home `sessions`; brain `session-list`, `wm-body`,
   `revisions`, `participants`, `recent` (plus every unguarded
   `getElementById` in its script); log `log-tail`, `feed`; memory
   `session-list`, `mem-body`, `mem-search`, `status-chips`, `prov-chips`.
4. Session selection belongs to the sidebar (`#session-list`); tabs are
   `tab-brain` / `tab-log` / `tab-memory` and `?session=` deep links must keep
   working.

## How to retune again

The safest edit path (the page constants are Python triple-quoted strings and
the inlined anime bundle is full of backslashes): write each full page as a
standalone HTML file, then splice it over the constant span from `NAME = """`
to the closing `"""`, escaping backslashes (`content.replace("\\", "\\\\")`)
and asserting `'"""' not in content`. Then
`.venv/bin/python -m py_compile packages/service/src/synapse_service/debug.py`,
run `pytest packages/service/tests/test_debug*.py
packages/service/tests/test_service_*_js.py`, and eyeball all four pages with
`.venv/bin/python scripts/demo_debug_gui.py` (free port 8991 first; stale
servers serve stale pages).
The worker debug page (copper side) has not been migrated to this system yet.
