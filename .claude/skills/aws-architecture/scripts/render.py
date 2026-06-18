#!/usr/bin/env python3
"""Render an SVG/HTML architecture diagram to PNG (and optionally embed icons).

Usage:
  render.py <input.html|input.svg> <output.png> [--scale N]

- Renders via headless Chromium (Playwright). Screenshots the #d element if
  present (the <svg id="d">), else the full page.
- For a self-contained source, embed icons as data: URIs BEFORE rendering with
  embed_icons() below, or call this with --embed to inline file:// icon refs.

Requires: pip install playwright && playwright install chromium
"""
import sys, base64, pathlib, re

def embed_icons(html: str, base_dir: pathlib.Path) -> str:
    """Replace <image href="...svg"> file refs with base64 data URIs."""
    def repl(m):
        href = m.group(2)
        path = None
        if href.startswith("file://"):
            path = pathlib.Path(href[7:])
        elif not href.startswith("data:") and href.endswith(".svg"):
            path = (base_dir / href).resolve()
        if path and path.is_file():
            b64 = base64.b64encode(path.read_bytes()).decode()
            return f'{m.group(1)}"data:image/svg+xml;base64,{b64}"'
        return m.group(0)
    return re.sub(r'(href=)"([^"]+)"', repl, html)

def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(2)
    src = pathlib.Path(sys.argv[1]); out = sys.argv[2]
    scale = 2
    if "--scale" in sys.argv:
        scale = int(sys.argv[sys.argv.index("--scale")+1])
    embed = "--embed" in sys.argv

    html = src.read_text()
    if embed:
        html = embed_icons(html, src.parent)
        src.write_text(html)
        print(f"embedded icons into {src}")

    from playwright.sync_api import sync_playwright
    url = src.resolve().as_uri()
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(device_scale_factor=scale)
        pg.goto(url); pg.wait_for_timeout(350)
        target = pg.query_selector("#d") or pg.query_selector("svg")
        (target or pg).screenshot(path=out)
        b.close()
    print("rendered", out)

if __name__ == "__main__":
    main()
