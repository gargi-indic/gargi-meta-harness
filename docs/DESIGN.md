# Design System: Gargi Labs

Single source of truth for how every Gargi Labs page looks. Tickets say *what* to
build; this file says *how it looks*. When a mockup in `design/reflex/` disagrees with
this file on colour, type, radius or rules, **this file wins**. The mockups still own
content, section order and layout intent.

Implementation lives in `web/styles/tokens.css` (values) and `web/styles/lab.css`
(components). Components reference tokens only; no hex values outside `tokens.css`.

---

## 1. Visual Theme & Atmosphere

**Swiss modernist poster meets lab notebook.** Heavy grotesk headlines set tight, a
single hard red, thick ink rules, square corners, and grayscale photography, carrying
the calm precision of a research log: every number is monospaced and carries its
conditions underneath it.

- **Density:** Daily-app balanced (4). Sections breathe; data rows are tight.
- **Variance:** Offset asymmetric (6). Split heroes, uneven two-column grids, one
  full-bleed band per page at most.
- **Motion:** Restrained (3). Motion only explains a state change (a call flipping from
  LLM to REFLEX, a chart drawing, a copy button confirming). The launch film carries
  the cinematic load so the page doesn't have to.

It should feel like a well-printed technical poster: confident, exact, a little dry.
Not a SaaS template, not an AI-gradient site.

## 2. Color Palette & Roles

One palette, cool-neutral greys throughout. Never mix in warm creams.

### Light (default)
| Token | Name | Hex | Role |
|---|---|---|---|
| `--paper` | Poster Grey | `#f3f2f2` | Page background |
| `--raised` | Proof Grey | `#eae9e9` | Emphasised panels (e.g. the Reflex product cell) |
| `--ink` | Press Ink | `#201e1d` | Headlines, body, rules, primary borders |
| `--ink-2` | Soft Ink | `#3b3836` | Long-form body copy |
| `--muted` | Graphite | `#6f6b69` | Secondary text, captions, caveat lines |
| `--faint` | Pencil | `#9b9797` | Numbering, inactive glyphs |
| `--line` | Rule | `#201e1d` | 2px structural rules |
| `--line-soft` | Hairline | `#d2cfcf` | 1px inner dividers (table rows, stat separators) |
| `--accent` | Signal Red | `#ec3013` | The one accent: primary CTA, the [ഗ] mark, focus rings, links on hover |
| `--on-accent` | | `#f3f2f2` | Text on red |
| `--inverse` | Press Ink | `#201e1d` | Code blocks, `pip install` pill, dark bands |
| `--on-inverse` | | `#f3f2f2` | Text on inverse |

### Dark (`prefers-color-scheme: dark`)
| Token | Hex |
|---|---|
| `--paper` | `#151413` |
| `--raised` | `#1e1c1b` |
| `--ink` / `--line` | `#eeeceb` |
| `--ink-2` | `#cfcbc9` |
| `--muted` | `#9b9797` |
| `--faint` | `#6f6b69` |
| `--line-soft` | `#343130` |
| `--accent` | `#ff563c` |
| `--on-accent` | `#151413` |
| `--inverse` | `#0c0b0b` |
| `--on-inverse` | `#eeeceb` |

### Semantic data roles (Reflex)
These are **not** extra accents. They appear only inside data: the call stream, loop
diagram, charts, stat numbers and route tags. They mean the same thing on every page.

| Token | Light | Dark | Meaning |
|---|---|---|---|
| `--student` | `#ec3013` | `#ff563c` | Reflex / local model / served locally (same as accent, on purpose: Reflex *is* the brand colour) |
| `--teacher` | `#2a5bd7` | `#6f94f0` | LLM / teacher / slow path |
| `--holdout` | `#9b9797` | `#9b9797` | Holdout slice / reference / ceilings |

Never use `--teacher` blue for a button, link or decoration.

## 3. Typography Rules

| Role | Font | Spec |
|---|---|---|
| **Display** | Archivo 800 | letter-spacing `-0.035em`, line-height `0.98` (hero) / `1.04` (sections). Scale via `clamp()`: hero `clamp(44px, 6.4vw, 92px)`, section `clamp(34px, 4.2vw, 56px)`, card title `30px` |
| **Body** | Archivo 400/500 | `17px/1.55`, lede `20px`, long-form max `65ch`, colour `--ink-2` |
| **Mono** | JetBrains Mono 400/500/600 | **Every number**, code, CLI commands, route tags, caveat lines (`12px/1.5`, `--muted`), status tags (`11px`, uppercase, `0.08em`) |
| **Malayalam & Indic scripts** | Noto Sans Malayalam 500–800 | The ഗ mark and all Malayalam text (`.ml`). Other scripts fall back to system Noto |

- Load all fonts with `next/font/google`. No `<link>` tags.
- Hierarchy through weight and colour before size. Emphasis inside a headline = the same
  Archivo 800 in `--accent`, never italic, never a second family.
- Newsreader, Hanken Grotesk, Noto Serif Devanagari from the mockups are **not** used.

## 4. Brand Mark

- **Lockup:** `[ഗ]` + `GARGI LABS`. Brackets in Archivo 800, ഗ in Noto Sans Malayalam 800,
  both `--accent`. Wordmark Archivo 800 uppercase, `letter-spacing: 0.02em`.
- **Hero mark:** the bracketed ഗ at poster scale is allowed once per page as the hero visual.
- **Ghost mark:** a large ഗ at 14% opacity may sit behind one dark or red panel per page.
  It must be positioned so the full glyph is legible, never cropped into an ambiguous shape.
- Never use Devanagari गार्गी. Product names: "Gargi Reflex" (never "Swappable"),
  "Coding harness & Meta harness", "Indic Language SLMs".

## 5. Component Stylings

- **Radius:** `0` everywhere. Buttons, inputs, cards, tags, code blocks, images. No exceptions.
- **Rules:** structural rules are `2px solid var(--line)`; inner dividers `1px solid var(--line-soft)`.
  Use one or the other on a list, never both on every row.
- **Buttons:** Primary = `--accent` fill, `--on-accent` text. Secondary = transparent with
  `2px` ink border. `12px 18px` padding, Archivo 600 15px, single line always.
  Active state `translateY(1px)`. Focus ring `2px solid var(--accent)` offset `2px`.
  No glows, no gradients, no shadows.
- **`pip install` pill:** `--inverse` block, mono 15–17px, `$` in `--accent`, a real copy
  button that switches to "Copied" for 1.5s.
- **Code blocks:** `--inverse` background, mono 15px/1.7, scroll inside the block on small
  screens, copy button top-right. "Before" blocks use `--raised` with `--ink-2` text.
- **Status tags:** mono 11px uppercase, `1.5px` border in `currentColor`, square.
  Live/launching = `--accent`; in development/planned = `--muted`.
- **Route tags (REFLEX / LLM / HOLDOUT):** REFLEX = solid `--student`; LLM = 2px outline
  `--teacher`; HOLDOUT = 2px outline `--holdout`. Mono 600 11–12px.
- **Stat tiles:** big mono number (`clamp(30px, 3.4vw, 50px)`), one-line label in body
  font, caveat line in mono 12px `--muted` directly beneath. A number without its caveat
  does not ship.
- **Product cells:** grouped in one bordered grid (2px outer rule, shared inner rules),
  not floating cards. Unequal sizes: the featured product spans more.
- **Forms:** label above input (mono 12px uppercase), error text below in `--accent`.
  Inputs `--paper` fill on coloured panels, `2px` ink border on paper. No placeholder-as-label.
  Choice chips are square toggle buttons with `aria-pressed`.
- **Photography:** grayscale (`filter: grayscale(1) contrast(1.08)`), full-bleed inside
  its grid cell, never overlaid with text or tags. The portrait `web/public/access-portrait.png`
  is the house image.
- **Video (launch film):** 16:9, square frame with a 2px ink rule, `muted` + `playsInline`,
  poster frame shown until play, visible controls. Autoplay only when
  `prefers-reduced-motion: no-preference`, and only while in view.

## 6. Layout Principles

- Max width `1240px`, side padding `48px` desktop / `16px` below `860px`.
- Section padding `clamp(56px, 8vw, 88px)` vertical. Sections are separated by a 2px ink rule.
- Header: sticky, `68px` tall, one line at desktop, 2px bottom rule; below `860px` the
  links collapse into a disclosure menu.
- Heroes are split (text left, visual right), never centred.
- CSS Grid for all multi-column layouts. Every grid collapses to one column below `860px`.
- No horizontal scroll at `375px`. Touch targets at least `44px`.
- Small uppercase mono labels above headlines: at most one per three sections.

## 7. Motion & Interaction

- Transitions `150–300ms`, `cubic-bezier(0.16, 1, 0.3, 1)`, `transform` and `opacity` only.
- Allowed: call-stream rows entering, chart lines drawing once on first view, tab
  switches, copy confirmation, menu open/close.
- No perpetual loops, marquees, parallax or scroll-jacking.
- Everything respects `prefers-reduced-motion: reduce` (show the end state instantly).

## 8. Voice

Calm, exact, a little dry. Short sentences. Every number carries its conditions.
Limits are stated plainly. No "elevate / seamless / unleash / next-gen", no testimonials,
logos or user counts (there are none), no em-dashes in new copy. Never "distill" or
"distillation": Reflex is *autonomous model caching* (see PLAN.md "Terminology").

## 9. Anti-Patterns (Banned)

- Rounded corners, drop shadows, glows, gradients, glassmorphism (the sticky header's
  light background blur is the only blur allowed).
- Warm cream/beige backgrounds, serif display type, italic headline emphasis.
- A second accent colour. Blue outside data.
- Three identical cards in a row; a label above every section.
- Fake UI built from divs presented as a screenshot (the call stream is a real,
  data-driven component, not a picture of one).
- Numbers invented for effect, or shown without their caveat.
- Emojis, scroll cues, version stamps, decorative status dots.
- Pure black `#000` or pure white `#fff`.
