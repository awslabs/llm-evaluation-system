#!/usr/bin/env python3
"""Resolve an AWS service name to its official icon SVG path.

Usage:
  find_icon.py <cache_dir> <query>          # print best-match SVG path
  find_icon.py <cache_dir> --list <query>   # print top 10 matches

Searches the cached AWS icon package. Prefers the clean
Architecture-Service-Icons set and, for general/resource icons that ship in
Light/Dark variants, the *Light* variant (dark glyph — visible on white).

Examples:
  find_icon.py ./cache cloudfront
  find_icon.py ./cache "elastic kubernetes"
  find_icon.py ./cache cognito
"""
import sys, os, glob, re

def norm(s): return re.sub(r"[^a-z0-9]", "", s.lower())

def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(2)
    cache = sys.argv[1]
    list_mode = sys.argv[2] == "--list"
    query = sys.argv[3] if list_mode else sys.argv[2]
    q = norm(query)

    roots = glob.glob(os.path.join(cache, "aws-icons", "*"))
    svgs = []
    for r in roots:
        svgs += glob.glob(os.path.join(r, "**", "*.svg"), recursive=True)

    # "user" means the actor glyph, not a service named *User*
    if q == "user":
        q_eff = "resuser48light"
    else:
        q_eff = q

    def score(path):
        base = os.path.basename(path)
        nb = norm(base)
        # strip the canonical prefixes/suffixes to compare the core name length
        core = re.sub(r"^(arch|res)", "", nb)
        core = re.sub(r"(48|light|svg)$", "", core)
        s = 0
        if q_eff in nb: s += 100
        # exactness: the closer the core name is to the query, the better
        # (so "simple storage service" beats "...-glacier")
        extra = max(0, len(core) - len(q_eff))
        s -= extra  # fewer trailing chars = higher score
        # prefer 48px, the standard service size
        if "_48" in base or "/48/" in path: s += 20
        # prefer the clean Architecture-Service-Icons set
        if "Architecture-Service-Icons" in path: s += 30
        # for general icons, prefer Light (visible on white), penalize Dark
        if base.endswith("_Light.svg"): s += 8
        if "Dark" in base: s -= 60
        return s

    cand = [p for p in svgs if q_eff in norm(os.path.basename(p))]
    if not cand:
        cand = [p for p in svgs if q in norm(os.path.basename(p))]
    cand.sort(key=score, reverse=True)

    if not cand:
        print(f"NO MATCH for '{query}'", file=sys.stderr); sys.exit(1)
    if list_mode:
        for p in cand[:10]:
            print(f"{score(p):4d}  {p}")
    else:
        print(cand[0])

if __name__ == "__main__":
    main()
