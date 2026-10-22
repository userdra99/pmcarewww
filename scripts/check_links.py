#!/usr/bin/env python3
"""Offline integrity check for the static mirror.

Scans every HTML/CSS/JS file under <site> for same-site references (root-relative, and any
remaining absolute pmcare.com.my URLs) and reports those whose target file is missing.

Usage: python3 -I scripts/check_links.py --site site [--json mirror-data/link-check.json]
"""
import argparse
import collections
import html
import json
import os
import re
import urllib.parse as up

REF_RE = re.compile(r"""(?:\b(?:href|src|action|data-[\w-]+|content)=["']|url\(\s*["']?)(/(?!/)[^"'()\s>]*)""")
SRCSET_RE = re.compile(r"""\bsrcset=["']([^"']+)["']""")
ABS_RE = re.compile(r"""(?:https?:)?//(?:www\.)?pmcare\.com\.my(/[^\s"'<>()\\]*)?""")
DYNAMIC = ("/wp-json", "/wp-admin", "/xmlrpc.php", "/cdn-cgi/l/", "/cdn-cgi/rum", "/feed")


def target(site, ref):
    p = up.unquote(html.unescape(ref).split("#", 1)[0].split("?", 1)[0])
    if p.endswith("/"):
        p += "index.html"
    return os.path.join(site, p.lstrip("/"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", default="site")
    ap.add_argument("--json")
    a = ap.parse_args()
    missing = collections.defaultdict(set)
    absolute = collections.defaultdict(set)
    files = 0
    for root, _, names in os.walk(a.site):
        for n in names:
            if not n.endswith((".html", ".css", ".js")):
                continue
            fn = os.path.join(root, n)
            rel = "/" + os.path.relpath(fn, a.site)
            text = open(fn, encoding="utf-8", errors="replace").read()
            files += 1
            refs = REF_RE.findall(text)
            for s in SRCSET_RE.findall(text):
                refs += [part.strip().split(" ")[0] for part in s.split(",") if part.strip().startswith("/")]
            for ref in refs:
                if ref.startswith(DYNAMIC) or "{{" in ref or ref.startswith("/?"):
                    continue
                if not os.path.exists(target(a.site, ref)):
                    missing[ref.split("?")[0]].add(rel)
            if n.endswith(".html"):
                body = re.sub(r'<link\b[^>]*\brel=["\'](?:canonical|shortlink|alternate|EditURI|pingback|https://api\.w\.org/)["\'][^>]*>'
                              r'|<meta\b[^>]*\bproperty=["\']og:url["\'][^>]*>', "", text, flags=re.I)
                for m in ABS_RE.finditer(body):
                    absolute[m.group(0)].add(rel)
    print(f"scanned {files} files; missing targets: {len(missing)}; leftover absolute site URLs: {len(absolute)}")
    for ref, where in sorted(missing.items())[:60]:
        print(f"  MISSING {ref}  (in {len(where)} files, e.g. {sorted(where)[0]})")
    for ref, where in sorted(absolute.items())[:20]:
        print(f"  ABSOLUTE {ref}  (in {len(where)} files, e.g. {sorted(where)[0]})")
    if a.json:
        json.dump({"missing": {k: sorted(v) for k, v in missing.items()},
                   "absolute": {k: sorted(v) for k, v in absolute.items()}}, open(a.json, "w"), indent=1)


if __name__ == "__main__":
    main()
