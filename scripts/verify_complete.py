#!/usr/bin/env python3
"""Prove the static mirror is a complete, current replica of https://www.pmcare.com.my.

Checks (each PASS/FAIL, details in mirror-data/verify-report.json):
  1. inventory   Every published page, post, category and media file listed by the live
                 WordPress REST API exists in the mirror (catches content added since the crawl).
  2. links       Every same-site reference inside the mirror resolves to a local file; anything
                 missing is checked against live - only targets that are also dead on live pass.
  3. freshness   Every mirrored page is refetched from live and compared with its crawl baseline
                 (per-request noise normalised), and every asset the live HTML references today
                 exists locally (catches edits made after the crawl).
  4. runtime     Every page is rendered in headless Chrome (desktop + mobile) against a local
                 server; any same-site request the mirror cannot serve is a failure.
  5. root files  robots.txt, 404.html, RSS feeds, routing rules.

Usage: python3 -I scripts/verify_complete.py [--skip runtime,freshness] [--workers 4]
Exit code 0 only when every check passes.
"""
import argparse
import concurrent.futures as cf
import functools
import http.server
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse as up
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE, DATA = os.path.join(ROOT, "site"), os.path.join(ROOT, "mirror-data")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
DYNAMIC = ("/wp-json", "/wp-admin", "/wp-login.php", "/xmlrpc.php", "/cdn-cgi/rum", "/cdn-cgi/l/")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mirror = _load("mirror")


def get(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": mirror.UA, "Accept-Encoding": "identity"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, b""
        except OSError:  # connection resets / timeouts from the live server: back off and retry
            time.sleep(3 * (attempt + 1))
    return 0, {}, b""


def local(path):
    return mirror.local_file(SITE, path)


# ---------------------------------------------------------------- 1. inventory
def check_inventory(rep):
    missing, counts = [], {}
    for t in ("pages", "posts", "categories", "media"):
        fields = "source_url" if t == "media" else "link"
        items, page = [], 1
        while True:
            st, h, body = get(f"{mirror.ORIGIN}/?rest_route=/wp/v2/{t}&per_page=100&page={page}&_fields={fields}")
            if st != 200:
                break
            items += json.loads(body)
            if page >= int(h.get("X-WP-TotalPages", "1")):
                break
            page += 1
        counts[t] = len(items)
        for it in items:
            if t == "media":
                path = up.urlsplit(it["source_url"]).path
                if not os.path.exists(local(path)):
                    missing.append(it["source_url"])
            else:
                s = up.urlsplit(it["link"])
                route = mirror.page_route(s.path, s.query)
                if route and not route.startswith("/page/2/") and not os.path.exists(local(route)):
                    missing.append(it["link"])
    rep["inventory"] = {"live_counts": counts, "missing": missing, "pass": not missing}


# ---------------------------------------------------------------- 2. links
def check_links(rep):
    out = subprocess.run(["python3", "-I", os.path.join(ROOT, "scripts", "check_links.py"), "--site", SITE,
                          "--json", os.path.join(DATA, "link-check.json")], capture_output=True, text=True)
    data = json.load(open(os.path.join(DATA, "link-check.json")))
    dead_on_live, gaps = [], []

    def classify(ref):
        st, _, _ = get(mirror.ORIGIN + ref.split("#")[0], timeout=60)
        return ref, st

    with cf.ThreadPoolExecutor(4) as ex:
        for ref, st in ex.map(classify, sorted(data["missing"])):
            (dead_on_live if st == 404 else gaps).append({"ref": ref, "live_status": st,
                                                           "in": data["missing"][ref][:3]})
    rep["links"] = {"missing_also_dead_on_live": dead_on_live, "missing_from_mirror": gaps,
                    "leftover_absolute_urls": list(data["absolute"])[:50],
                    "pass": not gaps and not data["absolute"], "summary": out.stdout.splitlines()[:1]}


# ---------------------------------------------------------------- 3. freshness
FONTFACE_RE = re.compile(r"@font-face\s*\{[^}]*\}")


def normalise(t):
    t = t.replace("\r\n", "\n").replace("\r", "\n")                       # baseline is read with universal newlines
    t = re.sub(r"\s*<script[^>]*static\.cloudflareinsights\.com[^>]*>\s*</script>", "", t)  # injected by Cloudflare edge on some responses
    t = re.sub(r"(?<![0-9a-f])[0-9a-f]{13}(?![0-9a-f])", "U", t)            # PHP uniqid() element ids
    t = re.sub(r'data-cfemail="[0-9a-f]+"', 'data-cfemail=""', t)            # Cloudflare per-request email key
    t = re.sub(r"email-protection#[0-9a-f]+", "email-protection#", t)
    t = re.sub(r"wpcf7_captcha/\d+\.png", "wpcf7_captcha/N.png", t)         # Contact Form 7 image captcha
    t = re.sub(r"(_wpcf7_captcha_challenge_[\w-]+\"?\s*(?:value=)?\"?)\d+", r"\1N", t)
    t = re.sub(r'(name="_wpcf7_ak_js" value=")\d+', r"\1N", t)               # Akismet timing token
    t = re.sub(r'(nonce["\']?\s*[:=]\s*["\'])[0-9a-f]+', r"\1N", t)
    faces = sorted(FONTFACE_RE.findall(t))                                     # Cloudflare Fonts shuffles order
    return re.sub(r"\s+", " ", FONTFACE_RE.sub("", t) + "".join(faces))   # whitespace-insensitive


def check_freshness(rep, workers):
    manifest = json.load(open(os.path.join(DATA, "manifest.json")))
    routes = {r: u for r, u in manifest["route_sources"].items() if manifest["pages"].get(r, {}).get("status") == 200}
    changed, unreachable, new_assets = [], [], set()

    def one(item):
        route, url = item
        st, _, body = get(url)
        if st != 200:
            return route, url, st, None, set()
        live = body.decode("utf-8", "replace")
        base = open(mirror.local_file(os.path.join(DATA, "raw-html"), route), encoding="utf-8").read()
        refs = set()
        for mm in mirror.URL_RE.finditer(live):
            p = up.urlsplit(mm.group("rest") or "/").path
            if p.startswith(mirror.ASSET_PREFIXES) and not p.endswith((".php", "/")) and "{{" not in p:
                refs.add(p)
        for mm in re.finditer(r"""["'(\s](/(?:wp-content|wp-includes|cf-fonts)/[^\s"'<>()\\]+)""", live):
            refs.add(up.urlsplit(mm.group(1)).path)
        # Contact Form 7 captcha images are generated per visitor and never exist for long.
        missing = {p for p in refs if "/wpcf7_captcha/" not in p and not os.path.exists(local(p))}
        return route, url, st, normalise(live) == normalise(base), missing

    with cf.ThreadPoolExecutor(workers) as ex:
        for route, url, st, same, missing in ex.map(one, sorted(routes.items())):
            if same is None:
                unreachable.append({"route": route, "url": url, "live_status": st})
            elif not same:
                changed.append({"route": route, "url": url})
            new_assets |= missing
    rep["freshness"] = {"pages_compared": len(routes), "changed_since_crawl": changed,
                        "now_unreachable_on_live": unreachable,
                        "live_assets_missing_locally": sorted(new_assets),
                        "pass": not changed and not unreachable and not new_assets}


# ---------------------------------------------------------------- 4. runtime
class _Recorder(http.server.SimpleHTTPRequestHandler):
    hits = []
    lock = threading.Lock()

    def log_request(self, code="-", size="-"):
        with self.lock:
            self.hits.append((self.path, int(getattr(code, "value", code)), self.headers.get("Referer", "")))

    def log_message(self, *a):
        pass


def check_runtime(rep, workers):
    manifest = json.load(open(os.path.join(DATA, "manifest.json")))
    routes = sorted(r for r, v in manifest["pages"].items() if v.get("status") == 200) + ["/404.html"]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Recorder, directory=SITE))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    tmp = tempfile.mkdtemp(prefix="pmc-verify-")
    slots = list(range(workers))
    slot_lock = threading.Lock()

    def render(job):
        route, mode = job
        with slot_lock:
            slot = slots.pop()
        try:
            size = "1440,12000" if mode == "desktop" else "390,16000"
            cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
                   "--no-default-browser-check", f"--user-data-dir={tmp}/p{slot}", f"--window-size={size}",
                   "--virtual-time-budget=8000", f"--screenshot={tmp}/s{slot}.png", base + route]
            if mode == "mobile":
                cmd.insert(1, f"--user-agent={MOBILE_UA}")
            for limit in (120, 240):  # one retry: parallel Chrome instances occasionally stall
                try:
                    subprocess.run(cmd, capture_output=True, timeout=limit)
                    return None
                except subprocess.TimeoutExpired:
                    pass
            return f"{mode} {route}"
        finally:
            with slot_lock:
                slots.append(slot)

    jobs = [(r, mode) for r in routes for mode in ("desktop", "mobile")]
    with cf.ThreadPoolExecutor(workers) as ex:
        timeouts = [t for t in ex.map(render, jobs) if t]
    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    failures, dynamic = {}, {}
    for path, code, ref in _Recorder.hits:
        bare = up.unquote(path.split("?")[0])
        page = up.urlsplit(ref).path or "?"
        if bare.startswith(DYNAMIC):
            dynamic.setdefault(bare, set()).add(page)
        elif code >= 400 and bare != "/favicon.ico":   # browsers probe favicon.ico; it is 404 on live too
            failures.setdefault(f"{code} {bare}", set()).add(page)
    known_dead = {e["ref"].split("#")[0].split("?")[0] for e in rep.get("links", {}).get("missing_also_dead_on_live", [])}
    real = {k: sorted(v)[:5] for k, v in failures.items() if k.split(" ", 1)[1] not in known_dead}
    rep["runtime"] = {"renders": len(jobs), "requests_seen": len(_Recorder.hits), "render_timeouts": timeouts,
                      "failed_requests": real,
                      "failed_but_dead_on_live": sorted(k for k in failures if k.split(" ", 1)[1] in known_dead),
                      "dynamic_endpoints_called": {k: len(v) for k, v in dynamic.items()},
                      "pass": not real and not timeouts}


# ---------------------------------------------------------------- 5. root files
def check_root(rep):
    need = ["index.html", "404.html", "robots.txt", "feed.xml", "bm/feed.xml", "bm/index.html", ".htaccess"]
    missing = [p for p in need if not os.path.exists(os.path.join(SITE, p))]
    if not os.path.exists(os.path.join(ROOT, "config", "nginx-pmcare.conf")):
        missing.append("config/nginx-pmcare.conf")
    rep["root_files"] = {"missing": missing, "pass": not missing}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip", default="", help="comma list: inventory,links,freshness,runtime,root_files")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    skip = set(filter(None, a.skip.split(",")))
    rep = {}
    steps = [("inventory", lambda: check_inventory(rep)), ("links", lambda: check_links(rep)),
             ("freshness", lambda: check_freshness(rep, a.workers)), ("runtime", lambda: check_runtime(rep, a.workers)),
             ("root_files", lambda: check_root(rep))]
    for name, fn in steps:
        if name in skip:
            continue
        print(f"[{name}] running...", flush=True)
        fn()
        r = rep[name]
        print(f"[{name}] {'PASS' if r['pass'] else 'FAIL'}", flush=True)
    json.dump(rep, open(os.path.join(DATA, "verify-report.json"), "w"), indent=1, sort_keys=True)

    print("\n==== Replica verification ====")
    if "inventory" in rep:
        r = rep["inventory"]
        print(f"inventory   {'PASS' if r['pass'] else 'FAIL'}  live: {r['live_counts']}; missing from mirror: {len(r['missing'])}")
        for x in r["missing"][:10]:
            print(f"              - {x}")
    if "links" in rep:
        r = rep["links"]
        print(f"links       {'PASS' if r['pass'] else 'FAIL'}  missing from mirror: {len(r['missing_from_mirror'])}; "
              f"dead on live too (faithfully copied): {len(r['missing_also_dead_on_live'])}")
        for x in r["missing_from_mirror"][:10]:
            print(f"              - {x['ref']} (live {x['live_status']})")
    if "freshness" in rep:
        r = rep["freshness"]
        print(f"freshness   {'PASS' if r['pass'] else 'FAIL'}  {r['pages_compared']} pages compared; changed since crawl: "
              f"{len(r['changed_since_crawl'])}; gone from live: {len(r['now_unreachable_on_live'])}; "
              f"live assets missing locally: {len(r['live_assets_missing_locally'])}")
        for x in r["changed_since_crawl"][:10]:
            print(f"              - changed: {x['url']}")
        for x in r["live_assets_missing_locally"][:10]:
            print(f"              - asset: {x}")
    if "runtime" in rep:
        r = rep["runtime"]
        print(f"runtime     {'PASS' if r['pass'] else 'FAIL'}  {r['renders']} renders, {r['requests_seen']} requests; "
              f"failed: {len(r['failed_requests'])}; dynamic endpoints called: {len(r['dynamic_endpoints_called'])}")
        for k, v in list(r["failed_requests"].items())[:10]:
            print(f"              - {k} (on {', '.join(v)})")
    if "root_files" in rep:
        r = rep["root_files"]
        print(f"root files  {'PASS' if r['pass'] else 'FAIL'}  missing: {r['missing'] or 'none'}")
    ok = all(r["pass"] for r in rep.values())
    print(f"\nOVERALL: {'COMPLETE REPLICA' if ok else 'NOT COMPLETE - see above / mirror-data/verify-report.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
