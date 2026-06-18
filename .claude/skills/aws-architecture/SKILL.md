---
name: aws-architecture
description: Use when the user asks to create, update, or render an AWS architecture diagram (or a cloud/infrastructure diagram using AWS service icons) — e.g. "diagram our AWS setup", "architecture diagram with CloudFront/S3/EKS/RDS", "update docs/image.png", "make an AWS infra diagram". Produces a clean, poster-style diagram with OFFICIAL AWS icons as a self-contained SVG, rendered to PNG. Not for generic flowcharts/ER/UML (use the drawio skill for those).
user-invocable: true
disable-model-invocation: false
---

# AWS Architecture Diagram Skill

Build clean, AWS-poster-style architecture diagrams using the **official AWS
Architecture Icons**, authored as a hand-laid-out **SVG** (inside an HTML
wrapper) and rendered to PNG via headless Chromium. This is the approach that
reliably avoids the two things that wreck these diagrams: **crossing arrows**
and **lines cutting through text**.

## Why SVG (not draw.io, not HTML/CSS divs)

- The hard part of an architecture diagram is the **connectors** (routed arrows
  between nodes). SVG `<path>` with explicit waypoints + a `<marker>` arrowhead
  is the only medium where you control every bend, so lines route through gaps
  and never cross labels.
- draw.io's auto-router makes uncontrollable diagonal crossings; CSS has no line
  primitive (you'd fake arrows with rotated divs — looks bad).
- One SVG coordinate system means icons and line endpoints align exactly, and
  paint order (lines first, icons second) tucks arrows neatly behind icons.

## Workflow

1. **Fetch icons (once):** `bash scripts/fetch_icons.sh` — downloads + caches
   the official AWS icon package under `cache/aws-icons/`. Idempotent.
2. **Resolve each service icon:** `python3 scripts/find_icon.py cache <name>`
   prints the best-match SVG path (e.g. `cloudfront`, `cognito`, `elastic
   kubernetes`, `rds`, `bedrock`, `simple storage`, `elastic load balancing`).
   Use `--list <name>` to see candidates. **Gotcha:** general/resource icons
   ship in `_Light` (dark glyph) and `_Dark` (white glyph) variants — on a white
   canvas always use **`_Light`** (the resolver already prefers it). The "User"
   actor icon is `Res_User_48_Light.svg`.
3. **Author the SVG** (see Template + Layout rules below). Reference icons with
   `<image href="file:///abs/path/icon.svg" .../>` while iterating.
4. **Render:** `python3 scripts/render.py diagram.html out.png` (screenshots the
   `<svg id="d">`). Add `--scale 2` for crisp output (default 2).
5. **CRITICAL — self-critique loop:** actually **Read the rendered PNG**, list
   the worst flaw (a line crossing text, a label collision, clipping, an
   invisible/white icon, an off-center trunk), fix that ONE thing, re-render.
   Repeat until clean. This loop is the skill — do not stop after one render.
   Typically 4-6 passes.
6. **Finalize self-contained:** once good, run
   `python3 scripts/render.py diagram.html out.png --embed` to inline the icon
   SVGs as base64 data URIs (so the source has no `file://` deps), then render
   the final PNG. Commit the `.html` source + PNG together.

## Layout rules (these prevent the mess)

- **Snap nodes to a coordinate grid.** Pick a few column x's and row y's; place
  every icon on them. Aligned bands = the AWS-poster look. Icons 48-56px square,
  label centered ~20px below, sub-label ~15px under that (smaller, grey #687078).
- **Draw connectors as orthogonal `<path>` UNDER the icons** (earlier in
  document order). Use right-angle segments with explicit waypoints, e.g.
  `d="M380,326 V186 H440"` (up, then right). Shared arrowhead via `<marker>`.
- **Route lines through gaps between nodes — never across a label.** If a line
  must pass a node, route it around the side (e.g. drop an auth line down the
  LEFT edge of a node, clear of its centered labels).
- **Edge labels sit BESIDE lines, not on them.** Use `text-anchor="end"` /
  `"start"` and place them in negative space. Keep labels few (the AWS style
  uses almost none — let position convey flow).
- **Boundaries:** an outer "AWS Cloud" box (solid dark #232F3E border, filled
  title tab top-left). Sub-groups (e.g. "EKS Cluster") as dashed rounded rects
  (#d97706 orange dashed) with a light fill.
- **Leave bottom/edge margin** so labels under the lowest row aren't clipped by
  the boundary box (a recurring bug — give the box ~30px below the last label).
- **Junctions:** when one line splits (e.g. CloudFront → S3 and → ALB), draw a
  small filled `<circle r="4">` at the split point.

## Template

A minimal, working skeleton (icons via file:// while iterating; `--embed` later):

```html
<!DOCTYPE html><html><head><meta charset="utf-8">
<style>body{margin:0;background:#fff;font-family:-apple-system,"Segoe UI",Arial,sans-serif;}</style>
</head><body>
<svg id="d" xmlns="http://www.w3.org/2000/svg" width="1080" height="580" viewBox="0 0 1080 580">
  <defs>
    <marker id="arr" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="#5b6770"/>
    </marker>
  </defs>
  <text x="540" y="42" text-anchor="middle" font-size="21" font-weight="700" fill="#232f3e">Title</text>

  <rect x="300" y="110" width="720" height="440" rx="10" fill="none" stroke="#232f3e" stroke-width="2"/>
  <rect x="300" y="110" width="118" height="25" rx="6" fill="#232f3e"/>
  <text x="316" y="127" font-size="12" font-weight="600" fill="#fff">AWS Cloud</text>

  <g stroke="#5b6770" fill="none">
    <path d="M122,326 H180" stroke-width="2" marker-end="url(#arr)"/>
    <path d="M380,326 V186 H440" stroke-width="2" marker-end="url(#arr)"/>
  </g>
  <circle cx="380" cy="326" r="4" fill="#5b6770"/>

  <g font-size="12" fill="#232f3e" text-anchor="middle">
    <image href="file:///path/_user.svg" x="70" y="300" width="52" height="52"/>
    <text x="96" y="372">User</text><text x="96" y="387" font-size="10" fill="#687078">browser</text>
  </g>
</svg>
</body></html>
```

## Palette

- Text: `#232f3e` (dark), sub-labels `#687078` (grey)
- Connectors: `#5b6770`
- AWS Cloud boundary: `#232f3e`; dashed sub-group: `#d97706` on `#fff8f0`
- Don't recolor the icons — the official SVGs carry AWS's service colors.

## Requirements

- `scripts/fetch_icons.sh` needs `curl` + `unzip`.
- `scripts/render.py` needs Playwright Chromium (`pip install playwright &&
  playwright install chromium`). If Playwright is unavailable, `rsvg-convert`
  can render a standalone `.svg` (with embedded icons) to PNG as a fallback.

## Licensing

Official AWS Architecture Icons are provided by AWS for use in architecture
diagrams, documentation, whitepapers, and presentations. Shipping the rendered
PNG in a repo's docs is within those terms.
