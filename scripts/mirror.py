#!/usr/bin/env python3
"""Mirror https://www.pmcare.com.my into a self-contained static site.

Pages are saved at clean paths so any static host can serve them:
    /                      -> index.html
    /?lang=bm              -> bm/index.html
    /?page_id=N            -> page/N/index.html
    /?p=N                  -> post/N/index.html
    /?cat=N                -> category/N/index.html
    ...&paged=K            -> <route>/paged/K/index.html
Assets keep their original paths (/wp-content/..., /wp-includes/..., /cf-fonts/...).
Legacy query URLs are served by the generated nginx/Apache rules (see legacy_rules.py).

Usage: python3 -I scripts/mirror.py --out site --data mirror-data [--workers 4]
Stdlib only. Resumable: existing asset files are reused, pages are always refetched.
"""
import argparse
import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse as up
import urllib.request

ORIGIN = "https://www.pmcare.com.my"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
ASSET_PREFIXES = ("/wp-content/", "/wp-includes/", "/cf-fonts/", "/cdn-cgi/scripts/")
PAGE_KEYS = {"page_id", "p", "cat", "lang", "paged", "preview"}
SKIP_TYPES = {"ohio_portfolio"}          # theme demo content, not PMCare pages
SKIP_PAGE_IDS = {"2"}                    # WordPress "Sample Page"

HOST = r"(?:https?:)?//(?:www\.)?pmcare\.com\.my"
URL_RE = re.compile(HOST + r"(?P<rest>/[^\s\"'<>()\\]*)?(?![\w.-])")
ESC_RE = re.compile(r"(?:https?:)?\\/\\/(?:www\.)?pmcare\.com\.my(?P<rest>(?:\\/[^\s\"'<>()\\]*)+)?")
ROOTREL_ATTR_RE = re.compile(r"""(?P<pre>\b(?:href|src|action|srcset|data-[\w-]+|content)=(?P<q>["']))(?P<url>/(?!/)[^"']*)(?P=q)""")
# Document-relative asset refs resolve against "/" on the live site (every page lives at the root path);
# pages move to /page/N/ here, so they are made root-relative.
DOCREL_RE = re.compile(r"""(?P<pre>(?:\b(?:href|src|data-[\w-]+)=["']|url\(\s*["']?))(?P<path>(?:wp-content|wp-includes)/)""")
CSS_URL_RE = re.compile(r"""url\(\s*(?P<q>["']?)(?P<url>.*?)(?P=q)\s*\)""")
CSS_IMPORT_RE = re.compile(r"""@import\s+(?P<q>["'])(?P<url>.*?)(?P=q)""")
LAZY_RE = re.compile(r'data-lazy-pages-count="(\d+)"[^>]*data-lazy-load-url-pattern="([^"]+)"')
CFEMAIL_HREF_RE = re.compile(r'(?:https?://www\.pmcare\.com\.my)?/cdn-cgi/l/email-protection#([0-9a-fA-F]+)')
CFEMAIL_SPAN_RE = re.compile(r'<(span|a)[^>]*class="__cf_email__"[^>]*data-cfemail="([0-9a-fA-F]+)"[^>]*>.*?</\1>', re.S)
# Head tags whose absolute URLs must stay absolute (SEO identity, language alternates, REST discovery).
PROTECT_RE = re.compile(r'<link\b[^>]*\brel=["\'](?:canonical|shortlink|alternate|EditURI|pingback|https://api\.w\.org/)["\'][^>]*>'
                        r'|<meta\b[^>]*\bproperty=["\']og:url["\'][^>]*>', re.I)


def cf_decode(hexstr):
    key = int(hexstr[:2], 16)
    return "".join(chr(int(hexstr[i:i + 2], 16) ^ key) for i in range(2, len(hexstr), 2))


def page_route(path, query):
    """Clean static path for a WordPress query URL, or None if it is not a mirrored page."""
    if path not in ("", "/", "/index.php"):
        return None
    q = up.parse_qs(query, keep_blank_values=True)
    if set(q) - PAGE_KEYS:
        return None
    g = lambda k: q[k][0] if k in q else None
    ok = lambda v: v is not None and re.fullmatch(r"[\w{}-]+", v)
    if ok(g("page_id")):
        base = f"/page/{g('page_id')}/"
    elif ok(g("p")):
        base = f"/post/{g('p')}/"
    elif ok(g("cat")):
        base = f"/category/{g('cat')}/"
    elif any(g(k) for k in ("page_id", "p", "cat")):
        return None
    elif g("lang") == "bm":
        base = "/bm/"
    else:
        base = "/"
    paged = g("paged")
    if ok(paged) and paged != "1":
        base += f"paged/{paged}/"
    return base


def local_file(out, route_or_path):
    p = up.unquote(route_or_path.split("?", 1)[0].split("#", 1)[0])
    if p.endswith("/"):
        p += "index.html"
    return os.path.join(out, p.lstrip("/"))


class Mirror:
    def __init__(self, out, data, workers):
        self.out, self.data, self.workers = out, data, workers
        self.lock = threading.Lock()
        self.pages = {}        # route -> fetch URL
        self.assets = {}       # path (as referenced, %-encoded, no query) -> fetch URL
        self.queued_pages, self.queued_assets = set(), set()
        self.manifest = {"pages": {}, "assets": {}, "unmapped_links": {}, "errors": []}
        self.sema = threading.Semaphore(workers)

    # ---------- network ----------
    def fetch(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity",
                                                   "Accept": "*/*"})
        for attempt in range(4):
            try:
                with self.sema:
                    time.sleep(0.15)
                    with urllib.request.urlopen(req, timeout=90) as r:
                        return r.status, r.headers.get("Content-Type", ""), r.read()
            except urllib.error.HTTPError as e:
                if e.code in (404, 410, 403):
                    return e.code, "", b""
                err = e
            except Exception as e:  # timeouts, resets
                err = e
            time.sleep(2 * (attempt + 1))
        return 0, str(err), b""

    # ---------- discovery ----------
    def add_page(self, url):
        s = up.urlsplit(html.unescape(url))
        if "ohio_portfolio" in s.query:
            return None
        route = page_route(s.path, s.query)
        if route is None or "{{" in route:
            return route
        if re.search(r"/page/(\d+)/", route) and re.search(r"/page/(\d+)/", route).group(1) in SKIP_PAGE_IDS:
            return route
        with self.lock:
            if route not in self.pages:
                self.pages[route] = ORIGIN + s.path + ("?" + s.query if s.query else "")
                self.queued_pages.add(route)
        return route

    def add_asset(self, path):
        path = path.split("#", 1)[0]
        bare = path.split("?", 1)[0]
        if not bare.startswith(ASSET_PREFIXES) or bare.endswith("/") or bare.endswith(".php"):
            return
        if "{{" in bare or "${" in bare or "'+" in bare:
            return
        with self.lock:
            if bare not in self.assets:
                self.assets[bare] = ORIGIN + up.quote(up.unquote(bare), safe="/%:@!$&'()*+,;=~-._") + \
                    ("?" + path.split("?", 1)[1] if "?" in path else "")
                self.queued_assets.add(bare)

    def note_unmapped(self, url, where):
        with self.lock:
            self.manifest["unmapped_links"].setdefault(url, set()).add(where)

    # ---------- rewriting ----------
    def map_url(self, rest, where):
        """rest: path+query+fragment after the host, possibly HTML-entity encoded."""
        raw = html.unescape(rest or "/")
        s = up.urlsplit(raw)
        if s.path.startswith(("/page/", "/post/", "/category/", "/bm/")):
            return rest  # already a mirrored route
        route = page_route(s.path, s.query)
        if route is not None:
            if "{{" not in route:
                self.add_page(ORIGIN + raw)
            return route + ("#" + s.fragment if s.fragment else "")
        if s.path.startswith(ASSET_PREFIXES):
            self.add_asset(s.path + ("?" + s.query if s.query else ""))
            return rest or "/"
        if not s.path.startswith(("/wp-json", "/wp-admin", "/xmlrpc.php", "/cdn-cgi/")):
            self.note_unmapped(raw, where)
        return rest or "/"

    def rewrite_text(self, text, where, base_url=None, css=False):
        def sub_abs(m):
            return self.map_url(m.group("rest"), where)

        def sub_esc(m):
            rest = (m.group("rest") or "\\/").replace("\\/", "/")
            return self.map_url(rest, where).replace("/", "\\/")

        text = URL_RE.sub(sub_abs, text)
        text = ESC_RE.sub(sub_esc, text)
        if css and base_url:
            for m in list(CSS_URL_RE.finditer(text)) + list(CSS_IMPORT_RE.finditer(text)):
                ref = m.group("url").strip()
                if not ref or ref.startswith(("data:", "#", "http:", "https:", "//")):
                    continue
                absu = up.urlsplit(up.urljoin(base_url, ref))
                if absu.netloc.endswith("pmcare.com.my"):
                    self.add_asset(absu.path + ("?" + absu.query if absu.query else ""))
        return text

    def rewrite_html(self, text, where):
        protected = []

        def protect(m):
            protected.append(m.group(0))
            return f"\x00P{len(protected) - 1}\x00"

        text = PROTECT_RE.sub(protect, text)
        # Cloudflare email obfuscation -> plain addresses (no dependency on Cloudflare at serve time)
        text = CFEMAIL_SPAN_RE.sub(lambda m: html.escape(cf_decode(m.group(2))), text)
        text = CFEMAIL_HREF_RE.sub(lambda m: "mailto:" + html.escape(cf_decode(m.group(1))), text)
        text = self.rewrite_text(text, where, base_url=ORIGIN + "/", css=True)

        def sub_rel(m):
            url = m.group("url")
            if m.group("pre").startswith("srcset"):
                for part in url.split(","):
                    self.add_asset(part.strip().split(" ")[0])
                return m.group(0)
            return m.group("pre") + self.map_url(url, where) + m.group("q")

        text = DOCREL_RE.sub(lambda m: m.group("pre") + "/" + m.group("path"), text)
        text = ROOTREL_ATTR_RE.sub(sub_rel, text)
        for count, pattern in LAZY_RE.findall(text):
            # pattern already rewritten to /<route>/paged/{{page}}/ ; enqueue concrete pages
            src = self.pages_src_for_pattern(pattern)
            if src:
                for k in range(2, int(count) + 1):
                    self.add_page(src.replace("{{page}}", str(k)))
        return re.sub(r"\x00P(\d+)\x00", lambda m: protected[int(m.group(1))], text)

    def pages_src_for_pattern(self, pattern):
        m = re.fullmatch(r"/(page|post|category)/([\w-]+)/paged/\{\{page\}\}/", pattern)
        if m:
            key = {"page": "page_id", "post": "p", "category": "cat"}[m.group(1)]
            return f"{ORIGIN}/?{key}={m.group(2)}&paged={{{{page}}}}"
        if pattern == "/paged/{{page}}/":
            return f"{ORIGIN}/?paged={{{{page}}}}"
        return None

    # ---------- workers ----------
    def do_page(self, route):
        url = self.pages[route]
        status, ctype, body = self.fetch(url)
        rec = {"url": url, "status": status, "file": None}
        if status == 200 and "html" in ctype:
            raw = body.decode("utf-8", "replace")
            raw_file = local_file(os.path.join(self.data, "raw-html"), route)
            os.makedirs(os.path.dirname(raw_file), exist_ok=True)
            with open(raw_file, "w", encoding="utf-8") as f:
                f.write(raw)
            out = self.rewrite_html(raw, route)
            fn = local_file(self.out, route)
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            with open(fn, "w", encoding="utf-8") as f:
                f.write(out)
            rec.update(file=os.path.relpath(fn, self.out), bytes=len(body),
                       sha256_raw=hashlib.sha256(body).hexdigest(),
                       title=html.unescape((re.search(r"<title>(.*?)</title>", raw, re.S) or [None, ""])[1]).strip())
        else:
            with self.lock:
                self.manifest["errors"].append({"kind": "page", "url": url, "status": status, "info": ctype})
        with self.lock:
            self.manifest["pages"][route] = rec

    def do_asset(self, path):
        url = self.assets[path]
        fn = local_file(self.out, path)
        rec = {"url": url}
        if os.path.exists(fn) and os.path.getsize(fn) > 0:
            body, status, ctype = open(fn, "rb").read(), 200, "cached"
        else:
            status, ctype, body = self.fetch(url)
        if status != 200:
            rec["status"] = status
            with self.lock:
                self.manifest["errors"].append({"kind": "asset", "url": url, "status": status, "info": ctype})
        else:
            low = path.lower()
            if low.endswith(".css") or low.endswith(".js") or "text/css" in ctype or "javascript" in ctype:
                text = body.decode("utf-8", "replace")
                new = self.rewrite_text(text, path, base_url=url, css=low.endswith(".css") or "css" in ctype)
                if new != text:
                    body = new.encode("utf-8")
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            with open(fn, "wb") as f:
                f.write(body)
            rec.update(status=200, file=os.path.relpath(fn, self.out), bytes=len(body),
                       sha256=hashlib.sha256(body).hexdigest())
        with self.lock:
            self.manifest["assets"][path] = rec

    def drain(self, queue_attr, fn, label):
        done = 0
        with cf.ThreadPoolExecutor(self.workers) as ex:
            while True:
                with self.lock:
                    batch = list(getattr(self, queue_attr))
                    getattr(self, queue_attr).clear()
                if not batch:
                    break
                for _ in ex.map(fn, batch):
                    done += 1
                    if done % 50 == 0:
                        print(f"  {label}: {done} done, {len(getattr(self, queue_attr))} queued", flush=True)
        return done

    def run(self, inventory):
        for t, items in inventory.items():
            if t in SKIP_TYPES or t.startswith("_"):
                continue
            for it in items:
                if "link" in it:
                    self.add_page(it["link"])
        self.add_page(ORIGIN + "/")
        self.add_page(ORIGIN + "/?lang=bm")
        for src in inventory.get("_media", []):
            self.add_asset(up.urlsplit(src).path)
        print(f"seed: {len(self.queued_pages)} pages, {len(self.queued_assets)} media", flush=True)
        while self.queued_pages or self.queued_assets:
            n = self.drain("queued_pages", self.do_page, "pages")
            print(f"pages pass: {n}", flush=True)
            n = self.drain("queued_assets", self.do_asset, "assets")
            print(f"assets pass: {n}", flush=True)
        self.manifest["unmapped_links"] = {k: sorted(v) for k, v in sorted(self.manifest["unmapped_links"].items())}
        self.manifest["route_sources"] = self.pages
        with open(os.path.join(self.data, "manifest.json"), "w") as f:
            json.dump(self.manifest, f, indent=1, sort_keys=True)
        ok_p = sum(1 for r in self.manifest["pages"].values() if r["status"] == 200)
        ok_a = sum(1 for r in self.manifest["assets"].values() if r.get("status") == 200)
        print(f"done: pages {ok_p}/{len(self.manifest['pages'])}, assets {ok_a}/{len(self.manifest['assets'])}, "
              f"errors {len(self.manifest['errors'])}, unmapped links {len(self.manifest['unmapped_links'])}")


def load_inventory(data):
    """Pages/posts/categories and the full media library from the public WP REST API."""
    path = os.path.join(data, "inventory.json")
    inv = json.load(open(path)) if os.path.exists(path) else {}
    if "_media" not in inv:
        media, page = [], 1
        while True:
            u = f"{ORIGIN}/?rest_route=/wp/v2/media&per_page=100&page={page}&_fields=id,source_url"
            r = urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=90)
            media += [m["source_url"] for m in json.load(r)]
            if page >= int(r.headers.get("X-WP-TotalPages", "1")):
                break
            page += 1
        inv["_media"] = media
        json.dump(inv, open(path, "w"), indent=1)
    return inv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site")
    ap.add_argument("--data", default="mirror-data")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    os.makedirs(a.data, exist_ok=True)
    Mirror(a.out, a.data, a.workers).run(load_inventory(a.data))


if __name__ == "__main__":
    sys.exit(main())
