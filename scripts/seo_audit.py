#!/usr/bin/env python3
"""
seo_audit.py — crawl a website, score its SEO health, and write an HTML + PDF report.

Design goals:
  * Standard library only for crawling/parsing (urllib, html.parser, json, ssl, gzip).
    Pillow is used for image dimensions if present, with a manual fallback so the
    script still runs without it.
  * Every finding carries a severity, a category, an impact/effort estimate, what was
    observed, and a concrete fix — so the report tells the reader what to DO, not just
    what's wrong.
  * Scoring is transparent: each category starts at 100 and loses points per finding by
    severity. The overall score is a weighted average. See references/scoring.md.

Usage:
    python3 seo_audit.py https://example.com [--max-pages N] [--out DIR]
                                             [--no-pdf] [--pagespeed]

The companion module report.py renders the HTML/PDF; this file does the analysis and
can be imported (run_audit) or called from the CLI.
"""

import argparse
import concurrent.futures
import datetime as _dt
import gzip
import io
import json
import os
import re
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

# A descriptive UA: honest about being an auditor, but browser-shaped so servers that
# sniff for "Mozilla" still serve the real page.
VERSION = "2.0.0"   # bump on every behavior change; shown in the report footer and --version
USER_AGENT = "Mozilla/5.0 (compatible; SEO-Audit-Skill/2.0; +https://claude.com/claude-code)"
# A plain desktop-browser UA: the baseline an AI-crawler probe is compared against, and
# what font/CSS endpoints need to see before they serve woff2.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = 20
socket.setdefaulttimeout(TIMEOUT)

# ----- severity model -------------------------------------------------------------
# Points deducted from a category's 100 when a finding of this severity appears.
SEVERITY_WEIGHT = {"CRITICAL": 40, "HIGH": 20, "MEDIUM": 10, "LOW": 4, "INFO": 0}
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

# Relative importance of each category in the overall score.
CATEGORY_WEIGHTS = {
    "crawlability": 0.20,
    "on page": 0.17,
    "performance": 0.15,
    "ai search": 0.12,
    "schema": 0.10,
    "trust": 0.10,
    "mobile": 0.08,
    "social": 0.08,
}
CATEGORIES = list(CATEGORY_WEIGHTS.keys())

# CTA verbs we look for in titles/descriptions — a clear action lifts click-through.
CTA_VERBS = {
    "book", "try", "get", "shop", "learn", "download", "explore", "compare",
    "start", "discover", "find", "create", "build", "join", "buy", "order",
    "read", "see", "watch", "request", "claim", "save", "grow", "boost",
    "sign up", "subscribe",
}

# AI crawlers that do NOT execute JavaScript — content missing from raw HTML is invisible
# to them, which increasingly matters for visibility in AI answers.
AI_CRAWLERS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User",           # OpenAI (training / search / live browse)
    "ClaudeBot", "Claude-Web", "anthropic-ai",           # Anthropic
    "PerplexityBot", "Perplexity-User",                  # Perplexity
    "Google-Extended",                                   # Gemini training opt-out token
    "Applebot-Extended",                                 # Apple Intelligence
    "Amazonbot", "meta-externalagent", "Bytespider",     # Amazon / Meta / ByteDance
    "CCBot", "cohere-ai", "DuckAssistBot",               # Common Crawl / Cohere / DuckDuckGo
]


# =================================================================================
# Fetching
# =================================================================================
class Response:
    def __init__(self, url, final_url, status, headers, body, elapsed_ms, error=None,
                 chain=None):
        self.url = url
        self.final_url = final_url
        self.status = status
        self.headers = headers or {}
        self.body = body or b""
        self.elapsed_ms = elapsed_ms
        self.error = error
        self.chain = chain or []   # [(url, status), …] one entry per redirect hop

    @property
    def text(self):
        charset = "utf-8"
        ctype = self.header("content-type")
        m = re.search(r"charset=([\w\-]+)", ctype or "", re.I)
        if m:
            charset = m.group(1)
        try:
            return self.body.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            return self.body.decode("utf-8", errors="replace")

    def header(self, name, default=""):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surface 3xx as HTTPError so fetch() can record the hop


_OPENER = urllib.request.build_opener(_NoRedirect())


def _decode_body(raw, enc):
    enc = (enc or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError):  # EOFError: body truncated by max_bytes
            return raw
    if "deflate" in enc:
        import zlib
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return raw
    return raw


def fetch(url, method="GET", max_bytes=None, max_redirects=10, ua=None, extra_headers=None):
    """Fetch a URL, following redirects manually so the hop chain is recorded
    (Response.chain). Decodes gzip/deflate. Never raises."""
    start = _dt.datetime.now()
    headers = {
        "User-Agent": ua or USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra_headers:
        headers.update(extra_headers)
    chain = []
    current = url
    if not str(url).lower().startswith(("http://", "https://")):
        # data:, mailto:, javascript:, file: — urllib would "open" some of these and hand
        # back a response with no status. They are never fetchable web resources.
        return Response(url, url, 0, {}, b"", 0, error="not an http(s) URL")

    def elapsed():
        return (_dt.datetime.now() - start).total_seconds() * 1000

    for _ in range(max_redirects + 1):
        try:
            req = urllib.request.Request(current, method=method, headers=headers)
        except ValueError as e:
            return Response(url, current, 0, {}, b"", elapsed(), error=str(e), chain=chain)
        try:
            with _OPENER.open(req, timeout=TIMEOUT) as r:
                raw = r.read(max_bytes) if max_bytes else r.read()
                raw = _decode_body(raw, r.headers.get("Content-Encoding"))
                return Response(url, current, r.status, dict(r.headers), raw, elapsed(),
                                chain=chain)
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if 300 <= e.code < 400 and loc and len(chain) < max_redirects:
                chain.append((current, e.code))
                nxt = urllib.parse.urljoin(current, loc.strip())
                if nxt == current:  # self-redirect loop
                    return Response(url, current, 0, {}, b"", elapsed(),
                                    error="redirect loop", chain=chain)
                current = nxt
                continue
            body = b""
            try:
                body = e.read()
            except Exception:
                pass
            return Response(url, current, e.code, dict(e.headers or {}), body, elapsed(),
                            chain=chain)
        except (urllib.error.URLError, ssl.SSLError, socket.timeout, ConnectionError,
                ValueError, OSError) as e:
            return Response(url, current, 0, {}, b"", elapsed(), error=str(e), chain=chain)
    return Response(url, current, 0, {}, b"", elapsed(), error="too many redirects", chain=chain)


# =================================================================================
# HTML parsing
# =================================================================================
SKIP_TEXT_TAGS = {"script", "style", "noscript", "template", "head", "svg"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self._in_title = False
        self.metas = []          # list of dicts: {name/property, content}
        self.headings = {h: [] for h in HEADING_TAGS}
        self._heading_stack = []
        self.links = []          # hrefs
        self.link_tags = []      # <link rel=...> dicts
        self.jsonld = []         # raw json-ld strings
        self._in_jsonld = False
        self._jsonld_buf = []
        self.lang = None
        self.text_parts = []
        self._skip_depth = 0
        self.img_total = 0
        self.img_missing_alt = 0
        self.img_missing_dim = 0
        self.has_viewport_tag = False
        self._open_skip = []
        self.element_count = 0          # DOM size proxy (count of start tags)
        self.scripts = []               # {src, async, defer, in_head}
        self._in_head = False
        self.heading_sequence = []      # heading tags in document order, e.g. ["h1","h2"]
        self.imgs = []                  # {src, loading, width, height, fetchpriority, srcset}
        self._seq = 0                   # document-order counter for head resources
        self.stylesheet_seq = []        # order index of each <link rel=stylesheet>
        self.time_tags = 0              # <time datetime=…> elements (visible freshness signal)
        self.nosnippet_attrs = 0        # elements carrying data-nosnippet
        self.address_tags = 0           # <address> elements
        self.table_count = 0
        self.list_count = 0
        self.iframes = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP_TEXT_TAGS:
            self._skip_depth += 1
            self._open_skip.append(tag)
        self.element_count += 1
        self._seq += 1
        if "data-nosnippet" in a:
            self.nosnippet_attrs += 1
        if tag == "time" and a.get("datetime"):
            self.time_tags += 1
        elif tag == "address":
            self.address_tags += 1
        elif tag == "table":
            self.table_count += 1
        elif tag in ("ul", "ol"):
            self.list_count += 1
        elif tag == "iframe" and a.get("src"):
            self.iframes.append(a["src"].strip())
        if tag == "head":
            self._in_head = True
        elif tag == "script":
            self.scripts.append({"src": (a.get("src") or "").strip(), "async": "async" in a,
                                 "defer": "defer" in a, "in_head": self._in_head,
                                 "seq": self._seq, "nonce": bool(a.get("nonce")),
                                 "type": (a.get("type") or "").lower()})
        if tag == "title":
            self._in_title = True
        elif tag == "html" and a.get("lang"):
            self.lang = a.get("lang")
        elif tag == "meta":
            self.metas.append(a)
            if (a.get("name") or "").lower() == "viewport":
                self.has_viewport_tag = True
        elif tag == "link":
            self.link_tags.append(a)
            if "stylesheet" in (a.get("rel") or "").lower():
                self.stylesheet_seq.append(self._seq)
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img":
            self.img_total += 1
            self.imgs.append({"src": (a.get("src") or a.get("data-src") or "").strip(),
                              "loading": (a.get("loading") or "").lower(),
                              "width": a.get("width") or "", "height": a.get("height") or "",
                              "fetchpriority": (a.get("fetchpriority") or "").lower(),
                              "srcset": bool(a.get("srcset"))})
            if not (a.get("alt") or "").strip():
                self.img_missing_alt += 1
            if not (a.get("width") and a.get("height")):
                self.img_missing_dim += 1
        elif tag in HEADING_TAGS:
            self._heading_stack.append([tag, []])
            self.heading_sequence.append(tag)
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_buf = []

    def handle_endtag(self, tag):
        if tag == "head":
            self._in_head = False
        if tag == "title":
            self._in_title = False
        elif tag in HEADING_TAGS and self._heading_stack:
            htag, parts = self._heading_stack.pop()
            self.headings[htag].append(" ".join("".join(parts).split()))
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.append("".join(self._jsonld_buf))
        if tag in SKIP_TEXT_TAGS and self._open_skip and self._open_skip[-1] == tag:
            self._skip_depth -= 1
            self._open_skip.pop()

    def handle_data(self, data):
        if self._in_jsonld:
            self._jsonld_buf.append(data)
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._heading_stack:
            self._heading_stack[-1][1].append(data)
        if self._skip_depth == 0 and data.strip():
            self.text_parts.append(data)

    # convenience -----------------------------------------------------------------
    @property
    def title(self):
        return " ".join("".join(self.title_parts).split())

    def meta_content(self, key, value):
        value = value.lower()
        for m in self.metas:
            if (m.get("name") or "").lower() == value or (m.get("property") or "").lower() == value:
                return (m.get("content") or "").strip()
        return ""

    @property
    def meta_description(self):
        return self.meta_content("name", "description")

    @property
    def meta_robots(self):
        return self.meta_content("name", "robots").lower()

    @property
    def canonical(self):
        for lt in self.link_tags:
            if "canonical" in (lt.get("rel") or "").lower():
                return (lt.get("href") or "").strip()
        return ""

    @property
    def hreflangs(self):
        return [((lt.get("hreflang") or "").strip(), (lt.get("href") or "").strip())
                for lt in self.link_tags
                if "alternate" in (lt.get("rel") or "").lower() and lt.get("hreflang")]

    @property
    def meta_refresh(self):
        for m in self.metas:
            if (m.get("http-equiv") or "").lower() == "refresh":
                return (m.get("content") or "").strip()
        return ""

    @property
    def favicon_href(self):
        best = ""
        for lt in self.link_tags:
            rel = (lt.get("rel") or "").lower()
            if "icon" in rel:
                best = (lt.get("href") or "").strip() or best
        return best

    @property
    def stylesheet_count(self):
        return sum(1 for lt in self.link_tags if "stylesheet" in (lt.get("rel") or "").lower())

    @property
    def script_src_count(self):
        return sum(1 for s in self.scripts if s.get("src"))

    @property
    def render_blocking_scripts(self):
        return sum(1 for s in self.scripts
                   if s.get("src") and s.get("in_head") and not s.get("async") and not s.get("defer"))

    @property
    def visible_text(self):
        return " ".join("".join(self.text_parts).split())

    @property
    def word_count(self):
        return len(self.visible_text.split())

    def og(self, prop):
        return self.meta_content("property", prop)

    def twitter(self, name):
        return self.meta_content("name", name)


def parse_jsonld_types(raw_blocks):
    """Return the set of @type values present across all JSON-LD blocks (best effort)."""
    types = set()
    valid = 0
    invalid = 0
    for raw in raw_blocks:
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            valid += 1
        except json.JSONDecodeError:
            invalid += 1
            continue

        def walk(node):
            if isinstance(node, dict):
                t = node.get("@type")
                if isinstance(t, str):
                    types.add(t)
                elif isinstance(t, list):
                    types.update(str(x) for x in t)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
    return types, valid, invalid


# =================================================================================
# Image inspection (for social preview)
# =================================================================================
def image_info(resp):
    """Return (width, height, size_bytes, mime) for an image Response, best effort."""
    if not resp or resp.status >= 400 or not resp.body:
        return None
    size = len(resp.body)
    mime = resp.header("content-type", "").split(";")[0].strip()
    w = h = None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(resp.body)) as im:
            w, h = im.size
            if not mime:
                mime = Image.MIME.get(im.format, "")
    except Exception:
        dims = _manual_dims(resp.body)
        if dims:
            w, h = dims
    return {"width": w, "height": h, "size": size, "mime": mime}


def _manual_dims(b):
    """Minimal PNG/GIF/JPEG dimension reader for when Pillow is unavailable."""
    import struct
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", b[16:24])
        return w, h
    if b[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", b[6:10])
        return w, h
    if b[:2] == b"\xff\xd8":  # JPEG
        i = 2
        while i < len(b) - 9:
            if b[i] != 0xFF:
                i += 1
                continue
            marker = b[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", b[i + 5:i + 9])
                return w, h
            seglen = struct.unpack(">H", b[i + 2:i + 4])[0]
            i += 2 + seglen
    return None


# =================================================================================
# Page discovery
# =================================================================================
def normalize(base, href):
    try:
        u = urllib.parse.urljoin(base, href)
        u, _frag = urllib.parse.urldefrag(u)
        return u
    except ValueError:
        return None


def same_site(a, b):
    ha = urllib.parse.urlparse(a).hostname or ""
    hb = urllib.parse.urlparse(b).hostname or ""
    ha = ha[4:] if ha.startswith("www.") else ha
    hb = hb[4:] if hb.startswith("www.") else hb
    return ha == hb


def _trivial_redirect(a, b):
    """True when a→b is mere scheme/www/trailing-slash normalization — expected
    behavior, not an SEO issue worth a finding."""
    pa, pb = urllib.parse.urlparse(a), urllib.parse.urlparse(b)
    return (same_site(a, b)
            and pa.path.rstrip("/") == pb.path.rstrip("/")
            and pa.query == pb.query)


def parse_robots(text):
    """RFC 9309-style evaluation of root ('/') access per agent.

    Real robots.txt files often contain MULTIPLE groups for the same agent (e.g.
    Cloudflare's managed 'Disallow: /' block followed by the operator's own
    'Allow: /' block). Groups for the same agent merge, and on an allow/disallow
    tie of equal specificity, Allow wins — so a naive 'saw Disallow: /' scan
    reports crawlers as blocked that are actually allowed."""
    groups = []                     # (agents_lower, rules) — rules: [(is_allow, path)]
    cur_agents, cur_rules = [], []
    prev_was_ua = False
    sitemaps = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, val = line.partition(":")
        field = field.strip().lower()
        val = val.strip()
        if field == "user-agent":
            if not prev_was_ua:      # a new group starts
                if cur_agents:
                    groups.append((cur_agents, cur_rules))
                cur_agents, cur_rules = [], []
            cur_agents.append(val.lower())
            prev_was_ua = True
            continue
        prev_was_ua = False
        if field == "sitemap":
            sitemaps.append(val)
        elif field in ("allow", "disallow") and cur_agents and val:
            cur_rules.append((field == "allow", val))
    if cur_agents:
        groups.append((cur_agents, cur_rules))

    def rules_for(agent_lower):
        if any(agent_lower in agents for agents, _ in groups):
            return [r for agents, rules in groups if agent_lower in agents for r in rules]
        return [r for agents, rules in groups if "*" in agents for r in rules]

    def root_blocked(rules):
        dis = any(not allow and p.rstrip("*") == "/" for allow, p in rules)
        alw = any(allow and p.rstrip("*") == "/" for allow, p in rules)
        return dis and not alw

    blocks_all = root_blocked(rules_for("*"))
    ai_blocked = [c for c in AI_CRAWLERS if root_blocked(rules_for(c.lower()))]
    return {"sitemaps": sitemaps, "ai_blocked": ai_blocked, "blocks_all": blocks_all}


def discover_pages(base_url, max_pages, sm_urls):
    """Return an ordered list of (url, source) to audit, homepage first. Sources:
    'homepage', 'sitemap', 'link' — the report shows how each page was discovered."""
    home = base_url
    pages = [(home, "homepage")]
    seen = {home.rstrip("/")}
    # Prefer the sitemap — it's the site's own statement of what matters — but sample
    # ACROSS its templates instead of taking the first N entries (see stratified_sample).
    sm_same = [u.strip() for u in sm_urls if u.strip() and same_site(base_url, u.strip())]
    for u in stratified_sample(sm_same, max_pages - 1, exclude=[home]):
        seen.add(u.rstrip("/"))
        pages.append((u, "sitemap"))
    # Fall back to following homepage links if the sitemap was thin.
    if len(pages) < max_pages:
        r = fetch(home)
        if r.status < 400:
            p = PageParser()
            try:
                p.feed(r.text)
            except Exception:
                pass
            for href in p.links:
                u = normalize(home, href)
                if not u or not u.startswith("http"):
                    continue
                if u.rstrip("/") in seen or not same_site(base_url, u):
                    continue
                if re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|css|js|webp|ico)(\?|$)", u, re.I):
                    continue
                seen.add(u.rstrip("/"))
                pages.append((u, "link"))
                if len(pages) >= max_pages:
                    break
    return pages[:max_pages]


# =================================================================================
# Analysis
# =================================================================================
def f(severity, title, category, impact, effort, observed, fix):
    return {
        "severity": severity, "title": title, "category": category,
        "impact": impact, "effort": effort, "observed": observed, "fix": fix,
    }


def analyze_page(url):
    """Fetch and analyze a single page. Returns (page_dict, findings_list)."""
    findings = []
    r = fetch(url)
    ua_fallback = False
    if r.status in (401, 403, 406, 429):
        # Bot protection often refuses an honest auditor UA. Retry as a browser so we can
        # still audit the page; run_audit reports that this happened.
        r2 = fetch(url, ua=BROWSER_UA)
        if r2.status == 200:
            r, ua_fallback = r2, True
    page = {
        "url": url, "status": r.status, "final_url": r.final_url, "ua_fallback": ua_fallback,
        "error": r.error, "elapsed_ms": round(r.elapsed_ms),
        "title": "", "description": "", "word_count": 0, "h1": [],
    }
    if r.error or r.status == 0:
        findings.append(f("CRITICAL", "Page unreachable", "crawlability", 5, 3,
                          r.error or "no response",
                          "Confirm the URL resolves and the server responds. Check DNS, TLS, and firewall rules."))
        page["findings"] = findings
        return page, findings

    if r.status >= 500:
        findings.append(f("CRITICAL", f"Server error {r.status}", "crawlability", 5, 3,
                          f"HTTP {r.status}", "Fix the server-side error returning this status."))
    elif r.status >= 400:
        findings.append(f("HIGH", f"Client error {r.status}", "crawlability", 4, 2,
                          f"HTTP {r.status}", "Return 200 for valid pages, or remove links pointing here."))

    p = PageParser()
    try:
        p.feed(r.text)
    except Exception:
        pass

    page.update({
        "title": p.title, "description": p.meta_description,
        "word_count": p.word_count, "h1": p.headings["h1"],
        "canonical": p.canonical, "lang": p.lang,
        "robots": p.meta_robots,
    })

    # ---- crawlability ----
    page["chain_hops"] = len(r.chain)
    if r.chain:
        hops = len(r.chain)
        if hops >= 2:
            chain_str = " → ".join([u for u, _ in r.chain] + [r.final_url])
            findings.append(f("MEDIUM", f"Redirect chain ({hops} hops)", "crawlability", 3, 2,
                              trunc(chain_str, 180),
                              "Collapse the chain into a single 301 straight to the final URL — every "
                              "extra hop adds latency and leaks crawl budget and link equity."))
        elif not _trivial_redirect(url, r.final_url):
            findings.append(f("LOW", "Page redirects", "crawlability", 2, 2,
                              f"{url} → {r.final_url}",
                              "Link directly to the final URL to avoid redirect latency and lost link equity."))

    # Client-render detection: raw HTML has almost no words but ships scripts.
    # When this fires, downstream content checks (H1, word count, alt text…) are
    # SUPPRESSED — the content exists after JS runs, we just can't see it, and
    # reporting "missing H1 / thin content" on top would be double-counting one
    # root cause and likely wrong about the rendered page.
    raw_words = p.word_count
    script_heavy = r.text.lower().count("<script") >= 3
    client_rendered = raw_words < 20 and script_heavy and r.status < 400
    page["client_rendered"] = client_rendered
    if client_rendered:
        findings.append(f("CRITICAL",
                          "Page may be client-rendered — content missing from raw HTML",
                          "crawlability", 5, 4,
                          f"Raw HTML has {raw_words} words, title {'set' if p.title else 'missing'}, "
                          f"{'no H1' if not p.headings['h1'] else 'H1 present'}",
                          "Enable prerendering or static export. AI crawlers (GPTBot, ClaudeBot, "
                          "PerplexityBot) and Bing's first pass do not run JavaScript. For SPAs use "
                          "SSR/SSG, react-snap, or a prerender service. (On-page content checks are "
                          "skipped for this page — they can't be measured from empty HTML.)"))
        findings.append(f("HIGH", "Content invisible to AI crawlers", "ai search", 4, 4,
                          f"{raw_words} words in raw HTML",
                          "Serve meaningful content in the initial HTML so AI answer engines can cite this page."))

    if raw_words == 0 and r.status == 200 and not p.title:
        findings.append(f("HIGH", "Possible soft 404 (200 OK with empty body)", "crawlability", 4, 2,
                          "0 words, no title",
                          "Return a real 404 status for missing pages, or fix the missing content."))

    if "noindex" in p.meta_robots or p.meta_robots.strip() == "none":
        findings.append(f("HIGH", "Page set to noindex", "crawlability", 4, 1,
                          f'meta robots="{p.meta_robots}"',
                          "Remove noindex if this page should appear in search results."))

    xrobots = r.header("x-robots-tag", "").lower()
    if "noindex" in xrobots or xrobots.strip() == "none":
        findings.append(f("HIGH", "Noindexed via X-Robots-Tag header", "crawlability", 4, 1,
                          f"X-Robots-Tag: {xrobots}",
                          "Remove the noindex directive from the HTTP response header if this page "
                          "should appear in search results (check CDN/server config)."))

    if "url=" in p.meta_refresh.lower():
        findings.append(f("HIGH", "Meta-refresh redirect", "crawlability", 4, 2,
                          f"meta refresh: {trunc(p.meta_refresh, 80)}",
                          "Replace the meta refresh with a server-side 301 — meta refreshes are slow, "
                          "pass less link equity, and can be treated as sneaky redirects."))

    # ---- on page ----
    if not p.title:
        findings.append(f("HIGH", "Missing title tag", "on page", 4, 1, "No <title>",
                          "Add a unique, descriptive <title> (≈50–60 chars) with the primary keyword."))
    else:
        tl = len(p.title)
        if tl > 65:
            findings.append(f("LOW", "Title too long", "on page", 2, 1, f"{tl} chars",
                             "Trim to ≈60 chars so it isn't truncated in search results."))
        elif tl < 25:
            findings.append(f("LOW", "Title very short", "on page", 2, 1, f"{tl} chars",
                             "Expand the title to better describe the page (≈50–60 chars)."))

    if not p.meta_description:
        findings.append(f("MEDIUM", "Missing meta description", "on page", 3, 1, "No meta description",
                          "Add a 140–160 char description summarizing the page to improve click-through."))
    else:
        dl = len(p.meta_description)
        if dl > 170:
            findings.append(f("LOW", "Meta description too long", "on page", 2, 1, f"{dl} chars",
                             "Trim to ≈155 chars to avoid truncation."))
        elif dl < 70:
            findings.append(f("LOW", "Meta description short", "on page", 2, 1, f"{dl} chars",
                             "Expand toward ≈150 chars to use the available SERP space."))
        if not has_cta(p.meta_description):
            findings.append(f("LOW", "Meta description lacks a CTA verb", "on page", 2, 1,
                             trunc(p.meta_description, 90),
                             "Add a verb like 'Learn', 'Explore', 'Get', or 'Compare' to invite the click."))

    h1s = p.headings["h1"]
    if len(h1s) == 0 and not client_rendered:
        findings.append(f("HIGH", "Missing H1 tag", "on page", 5, 1, "No H1 element on the page",
                          "Add one H1 describing the page's main topic, aligned with the title and primary keyword."))
    elif len(h1s) > 1:
        findings.append(f("LOW", "Multiple H1 tags", "on page", 2, 1, f"{len(h1s)} H1 elements",
                          "Use a single H1 per page; demote the others to H2/H3 to keep a clear hierarchy."))

    prev_lvl = 0
    for htag in p.heading_sequence:
        lvl = int(htag[1])
        if prev_lvl and lvl > prev_lvl + 1:
            findings.append(f("LOW", "Heading levels skip", "on page", 1, 1,
                              f"h{prev_lvl} followed directly by h{lvl}",
                              "Keep a sequential heading outline (H1 → H2 → H3) — a clean structure helps "
                              "search engines and AI answer engines parse and cite sections."))
            break
        prev_lvl = lvl

    if p.word_count < 300 and r.status == 200 and not client_rendered:
        findings.append(f("MEDIUM", "Thin content (<300 words)", "on page", 3, 4, f"{p.word_count} words",
                          "Add depth: examples, definitions, FAQ, or related links. Or noindex if it's a thin utility page."))

    # Long content with no subheadings is hard for readers AND for AI engines to
    # extract and cite specific passages from — well-structured sections matter in 2026.
    subheads = sum(len(p.headings[h]) for h in ("h2", "h3", "h4"))
    if p.word_count >= 600 and subheads == 0 and r.status == 200:
        findings.append(f("LOW", "Long content without subheadings", "on page", 2, 1,
                          f"{p.word_count} words, no H2/H3",
                          "Break the content into sections with descriptive H2/H3 headings — improves "
                          "readability and lets AI answer engines extract and cite specific passages."))

    if not p.canonical and r.status == 200 and p.word_count > 0:
        findings.append(f("LOW", "Missing canonical tag", "on page", 2, 1, "No rel=canonical link",
                          "Add <link rel=\"canonical\"> pointing to the preferred URL to consolidate "
                          "duplicate or parameter variants and focus ranking signals."))
    elif p.canonical and r.status == 200:
        can = normalize(r.final_url, p.canonical)
        if can and can.startswith("http"):
            cu, fu = urllib.parse.urlparse(can), urllib.parse.urlparse(r.final_url)
            if not same_site(can, r.final_url):
                findings.append(f("MEDIUM", "Canonical points to another domain", "crawlability", 3, 1,
                                  f"{r.final_url} → canonical {can}",
                                  "Unless this is deliberate syndication, point rel=canonical at this "
                                  "page's own URL — a cross-domain canonical hands your ranking signals "
                                  "to the other site."))
            elif cu.scheme == "http" and fu.scheme == "https":
                findings.append(f("MEDIUM", "Canonical points at the http:// version", "crawlability", 3, 1,
                                  f"canonical {can}",
                                  "Update rel=canonical to the https:// URL so signals consolidate on "
                                  "the secure version."))
            elif cu.path.rstrip("/") != fu.path.rstrip("/"):
                findings.append(f("MEDIUM", "Canonicalized to a different URL", "crawlability", 3, 1,
                                  f"{fu.path or '/'} → canonical {cu.path or '/'}",
                                  "This page tells search engines to index a different URL instead of "
                                  "itself. If that's not intentional, set the canonical to this page's "
                                  "own URL."))

    upath = urllib.parse.urlparse(url).path
    if re.search(r"[A-Z]|_|%20", upath):
        findings.append(f("LOW", "URL slug not clean", "on page", 1, 3, upath,
                          "Prefer short, lowercase, hyphen-separated URL slugs (301-redirect old URLs "
                          "if you rename)."))

    if p.hreflangs:
        alt_urls = {(normalize(r.final_url, h) or "").rstrip("/") for _, h in p.hreflangs if h}
        if r.final_url.rstrip("/") not in alt_urls:
            findings.append(f("LOW", "hreflang set lacks a self-reference", "crawlability", 2, 1,
                              f"{len(p.hreflangs)} hreflang link(s), none pointing at this URL",
                              "Every page must include an hreflang link to itself, or search engines may "
                              "ignore the whole hreflang set."))

    internal_links = 0
    for href in p.links:
        u2 = normalize(url, href)
        if u2 and u2.startswith("http") and same_site(u2, url):
            internal_links += 1
    page["internal_links"] = internal_links
    if r.status == 200 and p.word_count > 100:
        if internal_links == 0:
            findings.append(f("LOW", "Dead-end page (no internal links)", "on page", 2, 1,
                              "0 internal links on the page",
                              "Link to related pages so users and crawlers can continue — dead ends "
                              "strand the authority this page earns."))
        elif internal_links > 300:
            findings.append(f("LOW", "Very high internal link count", "on page", 1, 3,
                              f"{internal_links} internal links",
                              "Trim navigation/footer link bloat — hundreds of links per page dilute "
                              "the value each one passes."))

    if p.img_total and p.img_missing_alt and not client_rendered:
        sev = "MEDIUM" if p.img_missing_alt > p.img_total / 2 else "LOW"
        findings.append(f(sev, "Images missing alt text", "on page", 2, 2,
                          f"{p.img_missing_alt}/{p.img_total} images without alt",
                          "Add descriptive alt text for accessibility and image search."))

    # ---- schema ----
    types, valid, invalid = parse_jsonld_types(p.jsonld)
    page["schema_types"] = sorted(types)
    if not p.jsonld:
        obs = ("No JSON-LD in the raw HTML (JS may inject some at runtime, but AI crawlers "
               "and Bing's first pass won't see it)") if client_rendered else "No JSON-LD found"
        findings.append(f("LOW", "No structured data (JSON-LD)", "schema", 2, 2, obs,
                          "Add schema.org JSON-LD (Organization, WebSite, BreadcrumbList, and page-appropriate types) "
                          "to the server-rendered HTML."))
    if invalid:
        findings.append(f("MEDIUM", "Invalid JSON-LD", "schema", 3, 1, f"{invalid} block(s) failed to parse",
                          "Fix the JSON syntax; validate with Google's Rich Results Test."))
    if "FAQPage" in types:
        text_l = p.visible_text.lower()
        if p.visible_text.count("?") < 2 or p.word_count < 80:
            findings.append(f("MEDIUM", "FAQPage schema without visible FAQ content", "schema", 3, 2,
                              "FAQPage schema present but little/no visible FAQ on the page",
                              "Either remove the FAQPage schema or add a real FAQ section matching it. "
                              "Mismatches can trigger manual actions."))

    # ---- trust ----
    if urllib.parse.urlparse(r.final_url).scheme != "https":
        findings.append(f("CRITICAL", "Not served over HTTPS", "trust", 5, 2, r.final_url,
                          "Serve the site over HTTPS and redirect HTTP to HTTPS."))
    else:
        # Mixed content: an HTTPS page pulling subresources over http:// gets the
        # padlock stripped and the resources blocked by modern browsers.
        insecure = re.findall(r'\bsrc\s*=\s*["\']http://[^"\']+', r.text, re.I)
        insecure += re.findall(r'<link\b[^>]*\bhref\s*=\s*["\']http://[^"\']+', r.text, re.I)
        if insecure:
            findings.append(f("MEDIUM", "Mixed content on an HTTPS page", "trust", 3, 2,
                              f"{len(insecure)} subresource(s) loaded over http://",
                              "Load all scripts, styles, images, and iframes over https:// — browsers "
                              "block mixed content and it breaks the padlock."))
    missing_headers = missing_security_headers(r)
    if missing_headers:
        sev = "MEDIUM" if "Content-Security-Policy" in missing_headers else "LOW"
        findings.append(f(sev, f"Missing {len(missing_headers)} security header(s)", "trust", 2, 2,
                          ", ".join(missing_headers),
                          "Add the missing headers at your CDN or origin. CSP at minimum: default-src 'self'."))

    # ---- mobile ----
    if not p.has_viewport_tag:
        findings.append(f("HIGH", "Missing viewport meta tag", "mobile", 4, 1, "No responsive viewport",
                          'Add <meta name="viewport" content="width=device-width, initial-scale=1">.'))
    else:
        vp = p.meta_content("name", "viewport").lower()
        if "width=device-width" not in vp:
            findings.append(f("MEDIUM", "Viewport not responsive", "mobile", 3, 1, vp,
                             "Use width=device-width, initial-scale=1 for proper mobile scaling."))
        if "user-scalable=no" in vp or "maximum-scale=1" in vp.replace(" ", ""):
            findings.append(f("LOW", "Viewport disables zoom", "mobile", 2, 1, vp,
                             "Remove user-scalable=no / maximum-scale to keep the page accessible."))

    # ---- performance (observable from HTML + headers; no PageSpeed needed) ----
    if r.status == 200:
        html_kb = len(r.body) // 1024

        enc = (r.header("content-encoding") or "").lower()
        ctype = r.header("content-type", "").lower()
        if ("html" in ctype and len(r.body) > 30 * 1024
                and not any(c in enc for c in ("gzip", "br", "deflate", "zstd"))):
            findings.append(f("MEDIUM", "HTML served without compression", "performance", 3, 1,
                              f"no gzip/Brotli Content-Encoding · {html_kb} KB",
                              "Enable gzip or Brotli for text responses at your server/CDN — usually cuts "
                              "transfer size 60–80% and speeds first paint."))

        if html_kb > 300:
            findings.append(f("MEDIUM", "Large HTML document", "performance", 3, 3, f"{html_kb} KB of HTML",
                              "Trim inlined JSON/markup; heavy HTML slows parsing and delays LCP."))
        elif html_kb > 120:
            findings.append(f("LOW", "Heavy HTML document", "performance", 2, 3, f"{html_kb} KB of HTML",
                              "Consider trimming inlined data/markup to speed first render."))

        rbs = p.render_blocking_scripts
        if rbs:
            sev = "MEDIUM" if rbs >= 3 else "LOW"
            findings.append(f(sev, "Render-blocking scripts in <head>", "performance", 3, 2,
                              f"{rbs} blocking <script src> in head (no async/defer)",
                              "Add async or defer to head scripts, or move them before </body>, so they "
                              "don't block first paint."))

        reqs = p.script_src_count + p.stylesheet_count + p.img_total
        if reqs > 100:
            findings.append(f("MEDIUM", "Many resource requests", "performance", 2, 3,
                              f"~{reqs} (js {p.script_src_count}, css {p.stylesheet_count}, img {p.img_total})",
                              "Bundle/code-split JS, combine CSS, and lazy-load offscreen images to cut requests."))
        elif reqs > 50:
            findings.append(f("LOW", "High resource count", "performance", 2, 3,
                              f"~{reqs} requests", "Reduce the number of scripts/stylesheets/images to speed load."))

        if p.element_count > 3000:
            findings.append(f("MEDIUM", "Very large DOM", "performance", 2, 3, f"{p.element_count} elements",
                              "Large DOMs slow style/layout and hurt INP. Simplify markup or virtualize long lists."))
        elif p.element_count > 1500:
            findings.append(f("LOW", "Large DOM", "performance", 2, 3, f"{p.element_count} elements",
                              "Keep the DOM under ~1,500 nodes where practical for faster rendering."))

        if p.img_total and p.img_missing_dim:
            sev = "MEDIUM" if (p.img_missing_dim > p.img_total / 2 and p.img_total >= 4) else "LOW"
            findings.append(f(sev, "Images without width/height (layout-shift risk)", "performance", 3, 2,
                              f"{p.img_missing_dim}/{p.img_total} images lack explicit dimensions",
                              "Set width and height (or CSS aspect-ratio) so the browser reserves space — "
                              "prevents Cumulative Layout Shift (CLS)."))

        lazy = sum(1 for im in p.imgs if im["loading"] == "lazy")
        if p.img_total >= 8 and lazy == 0:
            findings.append(f("LOW", "No lazy-loading on images", "performance", 2, 1,
                              f'0 of {p.img_total} <img> tags use loading="lazy"',
                              'Add loading="lazy" to below-the-fold images so they don\'t compete with '
                              "the hero/LCP element for bandwidth."))

        legacy_imgs = sum(1 for im in p.imgs if re.search(r"\.(jpe?g|png)(\?|$)", im["src"], re.I))
        modern_imgs = sum(1 for im in p.imgs
                          if re.search(r"\.(webp|avif)(\?|$)|f(ormat)?[=_](webp|avif|auto)", im["src"], re.I))
        if legacy_imgs >= 5 and modern_imgs == 0:
            findings.append(f("LOW", "No modern image formats detected", "performance", 2, 2,
                              f"{legacy_imgs} JPEG/PNG <img> sources, no WebP/AVIF "
                              "(srcset/CDN content negotiation not counted)",
                              "Serve WebP or AVIF via <picture>, srcset, or a CDN that auto-negotiates — "
                              "typically 30–60% smaller than JPEG/PNG at the same quality."))

        if r.elapsed_ms > 4000:
            findings.append(f("MEDIUM", "Slow server response", "performance", 3, 3,
                              f"~{round(r.elapsed_ms)} ms to fetch HTML (approx, network-dependent)",
                              "Reduce TTFB: cache HTML at the edge, use a CDN, and cut backend work."))
        elif r.elapsed_ms > 2000:
            findings.append(f("LOW", "Server response is slow", "performance", 2, 3,
                              f"~{round(r.elapsed_ms)} ms to fetch HTML (approx, network-dependent)",
                              "Aim for a sub-second response; cache HTML / serve via a CDN."))

    # ---- ai search ----
    if not p.lang:
        findings.append(f("LOW", "Missing html lang attribute", "ai search", 2, 1, "<html> has no lang",
                          "Set <html lang=\"en\"> (or the correct language) to aid parsers and AI engines."))

    page["findings"] = findings
    page["parser"] = p
    page["response"] = r
    return page, findings


def has_cta(text):
    t = " " + text.lower() + " "
    return any((" " + v + " ") in t or t.strip().startswith(v + " ") for v in CTA_VERBS)


def trunc(s, n):
    s = s.strip()
    return s if len(s) <= n else s[:n].rstrip() + "…"


def cwv_fix(metric):
    return {
        "LCP": "Speed up the largest element: optimize/preload the hero image, cut "
               "render-blocking CSS/JS, and serve from a CDN.",
        "INP": "Reduce main-thread work: break up long JavaScript tasks, defer "
               "non-critical JS, and trim heavy third-party scripts.",
        "CLS": "Reserve space for images/ads/embeds (set width & height), and avoid "
               "inserting content above what the user is already viewing.",
    }.get(metric, "Address the Lighthouse opportunities for this metric.")


def missing_security_headers(resp):
    wanted = ["Strict-Transport-Security", "Content-Security-Policy",
              "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy",
              "Permissions-Policy"]
    present = {k.lower() for k in resp.headers}
    return [w for w in wanted if w.lower() not in present]


# =================================================================================
# Keyword focus — which terms stand out in the places that matter to SEO
# =================================================================================
# A keyword's SEO weight comes from WHERE it appears, not just how often. A term in
# the title/H1/headings signals topical intent far more than one buried in body copy.
# So we score each candidate phrase by weighted prominence across zones, capping each
# zone's contribution so sheer repetition can't outrank deliberate placement.
ZONE_WEIGHT = {"title": 5, "h1": 4, "heading": 3, "meta": 3, "url": 2, "schema": 2, "body": 1}
ZONE_CAP = {"body": 6, "heading": 3}  # max occurrences counted per zone-instance; others -> 2
ZONE_LABEL = {"title": "Title", "h1": "H1", "heading": "H2–H3", "meta": "Meta",
              "url": "URL", "schema": "Schema", "body": "Body"}
ZONE_ORDER = ["title", "h1", "heading", "meta", "url", "schema", "body"]

STOPWORDS = set("""
a an and are as at be been being but by for from had has have he her here hers him his
how i if in into is it its just me my no nor not now of off on once only or other our
ours out over own re s same she should so some such t than that the their theirs them
then there these they this those through to too under until up us very was we were what
when where which while who whom why will with you your yours about above after again
against all am any because before below between both did do does doing down during each
few more most can cannot could would get got also may might must shall etc via per vs
com www http https i'm it's we're you're don't your you they're new use using used make
makes made one two three set let see way ways thing things lot like want need know
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'+-]*")


def _keyphrases(text):
    """Yield clean 1–3 word keyphrases from text: lowercased, stopword-free, no pure
    numbers. Multi-word phrases never contain a stopword, so they read as real phrases."""
    toks = _TOKEN_RE.findall((text or "").lower())
    n = len(toks)
    out = []
    for i, t in enumerate(toks):
        if len(t) > 2 and not t.isdigit() and t not in STOPWORDS:
            out.append(t)
        if i + 1 < n:
            a, b = toks[i], toks[i + 1]
            if (a not in STOPWORDS and b not in STOPWORDS
                    and len(a) > 1 and len(b) > 1 and not (a.isdigit() and b.isdigit())):
                out.append(f"{a} {b}")
        if i + 2 < n:
            a, b, c = toks[i], toks[i + 1], toks[i + 2]
            if (all(x not in STOPWORDS for x in (a, b, c))
                    and all(len(x) > 1 for x in (a, b, c))):
                out.append(f"{a} {b} {c}")
    return out


def _schema_text(jsonld_blocks):
    """Pull human-readable text values (name, description, headline, etc.) out of the
    JSON-LD — these are deliberate entity/topic signals worth weighting."""
    SKIP_KEYS = {"@context", "@type", "@id", "url", "image", "logo", "sameas",
                 "contenturl", "thumbnailurl", "pricecurrency", "datepublished"}
    parts = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str):
                    if k.lower() not in SKIP_KEYS and not v.startswith("http") and len(v) < 300:
                        parts.append(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for raw in jsonld_blocks or []:
        try:
            walk(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            continue
    return " ".join(parts)


def extract_keywords(pages, top_n=12):
    """The keyword-focus process: rank phrases by weighted prominence across SEO zones.

    Returns {"terms": [...], "primary": str, "insights": [str], "available": bool}.
    Each term carries its score, body frequency, and the set of zones it appears in —
    so the report can show WHERE a keyword's SEO strength comes from."""
    score, zones_of, body_count = {}, {}, {}

    def add_zone_text(zone, text):
        if not text:
            return
        counts = {}
        for term in _keyphrases(text):
            counts[term] = counts.get(term, 0) + 1
        cap = ZONE_CAP.get(zone, 2)
        w = ZONE_WEIGHT[zone]
        for term, c in counts.items():
            score[term] = score.get(term, 0) + w * min(c, cap)
            zones_of.setdefault(term, set()).add(zone)
            if zone == "body":
                body_count[term] = body_count.get(term, 0) + c

    analyzed = 0
    for page in pages:
        p = page.get("parser")
        if not p or page.get("word_count", 0) == 0:
            continue
        analyzed += 1
        add_zone_text("title", p.title)
        for h in p.headings.get("h1", []):
            add_zone_text("h1", h)
        for lvl in ("h2", "h3"):
            for h in p.headings.get(lvl, []):
                add_zone_text("heading", h)
        add_zone_text("meta", p.meta_description)
        path = urllib.parse.urlparse(page["url"]).path
        add_zone_text("url", path.replace("/", " ").replace("-", " ").replace("_", " "))
        add_zone_text("schema", _schema_text(p.jsonld))
        add_zone_text("body", p.visible_text)

    if not analyzed or not score:
        return {"available": False, "terms": [], "primary": "", "insights": [],
                "note": "No crawlable on-page text to analyze (the pages render their "
                        "content client-side, so keyword prominence can't be measured)."}

    ranked = sorted(score.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    # Light de-duplication: drop a shorter phrase if a higher-ranked longer phrase
    # contains it and scores at least as high (avoids "romance" + "ai romance" + "ai
    # romance story" all crowding the table with the same signal).
    kept = []
    for term, sc in ranked:
        words = set(term.split())
        if any(words < set(k.split()) for k, _ in kept):
            continue
        kept.append((term, sc))
        if len(kept) >= top_n:
            break

    top_score = kept[0][1] if kept else 1
    terms = [{
        "term": term,
        "score": sc,
        "prominence": round(100 * sc / top_score),
        "body_count": body_count.get(term, 0),
        "zones": [z for z in ZONE_ORDER if z in zones_of.get(term, set())],
        "n": len(term.split()),
    } for term, sc in kept]

    return {"available": True, "terms": terms, "primary": terms[0]["term"],
            "insights": _keyword_insights(terms), "analyzed_pages": analyzed}


def _keyword_insights(terms):
    """Turn the ranked keywords into a few SEO-relevant observations."""
    out = []
    if not terms:
        return out
    prim = terms[0]
    pz = set(prim["zones"])
    if {"title", "h1"} <= pz:
        out.append(f"Your standout keyword “{prim['term']}” is well-aligned — it appears "
                   f"in the title, H1, and body, which is exactly what you want for your primary term.")
    elif pz & {"title", "h1"}:
        miss = "H1" if "title" in pz else "title"
        out.append(f"“{prim['term']}” is your most prominent keyword but is missing from the "
                   f"{miss} — add it there so your strongest signals agree.")
    else:
        out.append(f"“{prim['term']}” dominates the body copy but appears in neither the title "
                   f"nor the H1. If it's your target keyword, put it in both.")
    # Body themes that never reach the title/H1 — content you're not getting credit for.
    title_h1 = {"title", "h1"}
    buried = [t["term"] for t in terms[1:]
              if t["body_count"] >= 2 and not (set(t["zones"]) & title_h1)]
    if buried:
        out.append("Themes present in your content but absent from any title or H1: "
                   + ", ".join(f"“{b}”" for b in buried[:4])
                   + ". Work the most relevant of these into headings to rank for them.")
    phrases = [t["term"] for t in terms if t["n"] >= 2][:3]
    if phrases:
        out.append("Strongest multi-word phrases (usually higher-intent, easier to rank than single "
                   "words): " + ", ".join(f"“{p}”" for p in phrases) + ".")
    return out


# =================================================================================
# Social & local footprint — which profiles the site links, and its local presence
# =================================================================================
# (substring, platform) — order matters: more specific hosts first. First match wins.
SOCIAL_PLATFORMS = [
    ("facebook.com", "Facebook"), ("fb.com", "Facebook"), ("fb.me", "Facebook"),
    ("instagram.com", "Instagram"),
    ("twitter.com", "X / Twitter"), ("x.com", "X / Twitter"),
    ("linkedin.com", "LinkedIn"),
    ("youtube.com", "YouTube"), ("youtu.be", "YouTube"),
    ("tiktok.com", "TikTok"),
    ("pinterest.", "Pinterest"),
    ("yelp.com", "Yelp"),
    ("tripadvisor.", "TripAdvisor"),
    ("threads.net", "Threads"), ("threads.com", "Threads"),
    ("reddit.com", "Reddit"),
    ("snapchat.com", "Snapchat"),
    ("t.me", "Telegram"), ("telegram.me", "Telegram"),
    ("wa.me", "WhatsApp"), ("whatsapp.com", "WhatsApp"),
    ("discord.gg", "Discord"), ("discord.com", "Discord"),
    ("medium.com", "Medium"),
    ("github.com", "GitHub"),
    ("bsky.app", "Bluesky"),
]
# URL fragments that mean "share/login button", not "the business's profile".
_SOCIAL_NOISE = ("/sharer", "/share?", "/share/", "/intent/", "/dialog/", "sharer.php",
                 "/plugins/", "/login", "/signup", "/oauth", "share.php")


def _norm_url(u):
    pu = urllib.parse.urlparse(u)
    host = (pu.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return f"{host}{pu.path.rstrip('/')}".lower(), host


def _social_platform(host):
    for frag, name in SOCIAL_PLATFORMS:
        if frag in host:
            return name
    return None


def extract_footprint(pages):
    """Collect social profiles linked from the site (page links + schema sameAs) and any
    local / Google Business Profile signals. Returns a dict for the report."""
    links, sameas, nodes = [], set(), []

    def collect(node):
        if isinstance(node, dict):
            nodes.append(node)
            sa = node.get("sameAs")
            for v in ([sa] if isinstance(sa, str) else (sa or [])):
                if isinstance(v, str):
                    sameas.add(v)
            for v in node.values():
                collect(v)
        elif isinstance(node, list):
            for v in node:
                collect(v)

    for page in pages:
        p = page.get("parser")
        if not p:
            continue
        for href in p.links:
            u = normalize(page["url"], href)
            if u and u.startswith("http"):
                links.append(u)
        for raw in p.jsonld:
            try:
                collect(json.loads(raw))
            except (json.JSONDecodeError, ValueError):
                continue

    sameas_norm = {_norm_url(u)[0] for u in sameas}

    # ---- social profiles ----
    social, seen = [], set()
    for u in list(sameas) + links:
        norm, host = _norm_url(u)
        plat = _social_platform(host)
        if not plat:
            continue
        low = u.lower()
        if any(frag in low for frag in _SOCIAL_NOISE):
            continue
        if not urllib.parse.urlparse(u).path.strip("/"):  # bare homepage, not a profile
            continue
        key = (plat, norm)
        if key in seen:
            continue
        seen.add(key)
        social.append({"platform": plat, "url": u, "in_sameas": norm in sameas_norm})
    social.sort(key=lambda s: (s["platform"], s["url"]))

    # ---- Google / local presence ----
    google = _google_signals(links, nodes)

    findings = []
    if social and not any(s["in_sameas"] for s in social):
        findings.append(f("LOW", "Social profiles not declared in sameAs schema", "schema", 2, 1,
                          f"{len(social)} profile(s) linked, none in Organization sameAs",
                          "Add the profile URLs to your Organization/LocalBusiness sameAs array so "
                          "search and AI engines connect them to your entity (a knowledge-graph signal)."))

    return {"social": social, "sameas_present": bool(sameas), "google": google,
            "no_links": not links and not sameas, "findings": findings}


def _google_signals(links, nodes):
    maps_links, gbp_links = [], []
    for u in links:
        host = (urllib.parse.urlparse(u).hostname or "").lower()
        low = u.lower()
        if ("g.page" in host or "g.co/kgs" in low or "business.google.com" in host
                or "maps.app.goo.gl" in host or ("goo.gl" in host and "maps" in low)):
            gbp_links.append(u)
        elif ("google." in host and "/maps" in low) or host.startswith("maps.google"):
            maps_links.append(u)

    # self-declared local-business data from schema
    LOCAL_HINTS = ("LocalBusiness", "Restaurant", "Store", "Hotel", "TouristAttraction",
                   "TouristInformationCenter", "Place", "Organization")

    def types_of(n):
        t = n.get("@type")
        return {t} if isinstance(t, str) else (set(t) if isinstance(t, list) else set())

    best = None
    for n in nodes:
        ts = types_of(n)
        looks_local = (any(any(h in t for h in LOCAL_HINTS) for t in ts)
                       and (n.get("address") or n.get("geo") or n.get("telephone")
                            or n.get("aggregateRating")))
        if not looks_local:
            continue
        # prefer the richest node (most of the fields we care about)
        rank = sum(bool(n.get(k)) for k in ("address", "telephone", "geo",
                   "aggregateRating", "openingHoursSpecification", "openingHours"))
        if best is None or rank > best[0]:
            best = (rank, n)

    local = _local_fields(best[1]) if best else None
    return {"maps_links": maps_links[:5], "gbp_links": gbp_links[:5], "local": local}


def _fmt_address(addr):
    if isinstance(addr, str):
        return addr
    if isinstance(addr, dict):
        parts = [addr.get(k) for k in ("streetAddress", "addressLocality",
                 "addressRegion", "postalCode", "addressCountry")]
        return ", ".join(str(p) for p in parts if p)
    return ""


def _local_fields(n):
    out = {}
    out["name"] = n.get("name") or ""
    out["telephone"] = n.get("telephone") or ""
    out["address"] = _fmt_address(n.get("address"))
    out["price_range"] = n.get("priceRange") or ""
    geo = n.get("geo")
    if isinstance(geo, dict) and geo.get("latitude") and geo.get("longitude"):
        out["geo"] = f"{geo['latitude']}, {geo['longitude']}"
    ar = n.get("aggregateRating")
    if isinstance(ar, dict):
        rv = ar.get("ratingValue")
        rc = ar.get("reviewCount") or ar.get("ratingCount")
        if rv:
            out["rating"] = f"{rv}" + (f" from {rc} reviews" if rc else "")
    hours = n.get("openingHours")
    if isinstance(hours, str):
        out["hours"] = hours
    elif isinstance(hours, list) and hours:
        out["hours"] = "; ".join(str(h) for h in hours if isinstance(h, str))
    elif isinstance(n.get("openingHoursSpecification"), list):
        specs = []
        for s in n["openingHoursSpecification"]:
            if not isinstance(s, dict):
                continue
            days = s.get("dayOfWeek")
            days = ", ".join(d.split("/")[-1] for d in days) if isinstance(days, list) else str(days or "")
            if s.get("opens") and s.get("closes"):
                specs.append(f"{days} {s['opens']}–{s['closes']}".strip())
        if specs:
            out["hours"] = "; ".join(specs[:7])
    return {k: v for k, v in out.items() if v}


# =================================================================================
# Site-wide checks: host variants, 404 handling, TLS, trust pages, broken links,
# asset caching, analytics — the checks a professional audit runs once per site.
# =================================================================================
_SKIP_LINK_RE = re.compile(
    r"\.(pdf|jpe?g|png|gif|svg|webp|avif|ico|css|js|mjs|zip|gz|mp3|mp4|webm|woff2?|xml)(\?|$)"
    r"|add-to-cart|logout|wp-login|/cart(/|$|\?)|/checkout(/|$|\?)|replytocom=", re.I)


def check_host_variants(base, canonical_final_url):
    """http/https × www/non-www should all 301 to one canonical origin. Serving the
    site on two hosts splits link equity; serving plain http is a trust problem."""
    pu = urllib.parse.urlparse(base)
    host = pu.hostname or ""
    alt = host[4:] if host.startswith("www.") else "www." + host
    canon_host = urllib.parse.urlparse(canonical_final_url).hostname or host
    findings, health, results = [], [], []
    for v in (f"http://{host}/", f"https://{alt}/", f"http://{alt}/"):
        r = fetch(v, max_bytes=2048)
        results.append((v, r))
        if r.status == 0:
            health.append((v, f"unreachable ({trunc(r.error or 'no response', 60)})"))
        else:
            fu = urllib.parse.urlparse(r.final_url)
            arrow = f"→ {fu.scheme}://{fu.hostname or ''}" if r.chain else "no redirect"
            health.append((v, f"{arrow} (HTTP {r.status})"))

    http_live = [v for v, r in results if r.status == 200
                 and urllib.parse.urlparse(r.final_url).scheme == "http"]
    if http_live:
        findings.append(f("HIGH", "HTTP version not redirected to HTTPS", "trust", 4, 1,
                          "; ".join(http_live) + " serves 200 over plain http",
                          "301-redirect all http:// traffic to the https:// canonical host — a duplicate "
                          "insecure copy of the site splits signals and erodes trust."))
    dup_hosts = [v for v, r in results if r.status == 200
                 and urllib.parse.urlparse(r.final_url).scheme == "https"
                 and (urllib.parse.urlparse(r.final_url).hostname or "") != canon_host]
    if dup_hosts:
        findings.append(f("MEDIUM", "Duplicate host serves the site without redirect", "crawlability", 3, 1,
                          "; ".join(f"{v} answers 200 without redirecting to {canon_host}" for v in dup_hosts),
                          f"301-redirect the alternate hostname to https://{canon_host}/ so indexing and "
                          "link equity consolidate on one host (a canonical tag helps, but the redirect "
                          "is the robust fix)."))
    unreach = [v for v, r in results if r.status == 0]
    if unreach:
        findings.append(f("LOW", "Some host variants unreachable", "crawlability", 2, 2,
                          "; ".join(unreach),
                          "Make every variant (http/https, www/non-www) resolve and 301 to the canonical "
                          "URL so no typed or linked variant dead-ends."))
    return findings, health


def check_custom_404(base):
    """Probe a URL that can't exist and verify the site answers with a real 404."""
    probe = normalize(base, "/seo-audit-404-probe-x9q3z7/")
    r = fetch(probe, max_bytes=4096)
    findings = []
    if r.status == 200 and r.final_url.rstrip("/") == base.rstrip("/"):
        findings.append(f("MEDIUM", "Missing pages redirect to the homepage", "crawlability", 3, 2,
                          f"GET {probe} redirects to the homepage",
                          "Return a 404 status with a helpful not-found page instead — Google treats "
                          "blanket redirects to the homepage as soft 404s."))
        health = ("404 handling", "redirects to homepage (soft 404)")
    elif r.status == 200:
        findings.append(f("HIGH", "Missing pages return HTTP 200 (soft 404)", "crawlability", 4, 2,
                          f"GET {probe} → 200",
                          "Return a real 404 status for unknown URLs — soft 404s waste crawl budget and "
                          "can get junk URLs indexed."))
        health = ("404 handling", "returns 200 for missing pages (soft 404)")
    elif r.status in (404, 410):
        health = ("404 handling", f"correct (HTTP {r.status})")
    elif r.status == 0:
        health = ("404 handling", "could not test")
    else:
        health = ("404 handling", f"returns HTTP {r.status}")
    return findings, health


def tls_certificate(host):
    """Inspect the TLS certificate: expiry runway and issuer. Best effort."""
    findings = []
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        exp = ssl.cert_time_to_seconds(cert["notAfter"])
        days = int((exp - _dt.datetime.now(_dt.timezone.utc).timestamp()) // 86400)
        issuer = ""
        for rdn in cert.get("issuer", ()):
            for k, v in rdn:
                if k == "organizationName":
                    issuer = v
        if days < 0:
            findings.append(f("CRITICAL", "TLS certificate expired", "trust", 5, 2,
                              f"expired {-days} day(s) ago",
                              "Renew the certificate immediately — browsers are blocking the site."))
        elif days <= 14:
            findings.append(f("HIGH", "TLS certificate expires very soon", "trust", 4, 2,
                              f"{days} day(s) left",
                              "Renew now and enable auto-renewal before browsers start showing "
                              "security errors."))
        elif days <= 30:
            findings.append(f("MEDIUM", "TLS certificate expires within 30 days", "trust", 3, 2,
                              f"{days} day(s) left", "Confirm certificate auto-renewal is working."))
        health = ("TLS certificate", f"valid · {days} days left" + (f" · {issuer}" if issuer else ""))
    except Exception as e:
        health = ("TLS certificate", f"could not inspect ({e.__class__.__name__})")
    return findings, health


_TRUST_PATTERNS = {
    "contact": re.compile(r"/(contact|contact-?us|kontakt|get-?in-?touch|book-?now)(/|$|\.|\?)", re.I),
    "about": re.compile(r"/(about|about-?us|our-?story|who-?we-?are|team|meet-)", re.I),
    "privacy": re.compile(r"privacy|datenschutz|privacybeleid", re.I),
    "terms": re.compile(r"/(terms|tos$|terms-?of|conditions|legal|imprint|impressum|refund|cancellation)", re.I),
}


def check_trust_pages(pages):
    """E-E-A-T basics: can a visitor (or a quality rater) reach contact info and
    policy pages from the crawled pages? Returns (findings, health_row, signals)."""
    paths, has_tel, has_mailto = set(), False, False
    for page in pages:
        p = page.get("parser")
        if not p:
            continue
        for href in p.links:
            low = href.strip().lower()
            if low.startswith("tel:"):
                has_tel = True
            elif low.startswith("mailto:"):
                has_mailto = True
            else:
                u = normalize(page["url"], href)
                if u and u.startswith("http") and same_site(u, page["url"]):
                    paths.add(urllib.parse.urlparse(u).path.lower())
    found = {k: any(rx.search(pt) for pt in paths) for k, rx in _TRUST_PATTERNS.items()}
    findings = []
    if not paths and not (has_tel or has_mailto):
        # Nothing crawlable to judge (fully client-rendered site) — don't claim pages
        # are missing when we simply can't see any links.
        return findings, ("Contact & trust pages",
                          "could not evaluate — no crawlable links in the raw HTML"), \
               {"has_tel": has_tel, "has_mailto": has_mailto, "found": found}
    if not (found["contact"] or has_tel or has_mailto):
        findings.append(f("MEDIUM", "No visible contact route", "trust", 3, 2,
                          "No contact page link, tel: or mailto: found on the crawled pages",
                          "Add a contact page and put phone/email in the footer — contactability is a "
                          "core trust/E-E-A-T signal for search engines and customers alike."))
    missing = [k for k in ("privacy", "terms", "about") if not found[k]]
    if missing:
        findings.append(f("LOW", "Trust pages not found: " + ", ".join(missing), "trust", 2, 3,
                          f"No link to {', '.join(missing)} page(s) on the crawled pages",
                          "Publish and footer-link About, Privacy Policy, and Terms pages — E-E-A-T "
                          "assessments look for them. If they exist but aren't linked, just link them."))
    contact_bits = [b for b, ok in (("contact page", found["contact"]),
                                    ("tel:", has_tel), ("mailto:", has_mailto)) if ok]
    health = ("Contact & trust pages",
              (", ".join(contact_bits) or "no contact signals")
              + " · " + " ".join(f"{k} {'✓' if found[k] else '✗'}" for k in ("about", "privacy", "terms")))
    return findings, health, {"has_tel": has_tel, "has_mailto": has_mailto, "found": found}


def check_asset_caching(home_page):
    """Sample the first same-site stylesheet, script, and image and check for
    long-lived Cache-Control — repeat-visit speed and a cheap CWV win."""
    p = home_page.get("parser")
    if not p:
        return []
    base_url = home_page["url"]
    cands = []
    for lt in p.link_tags:
        if "stylesheet" in (lt.get("rel") or "").lower() and lt.get("href"):
            cands.append(lt["href"])
            break
    for s in p.scripts:
        if s.get("src"):
            cands.append(s["src"])
            break
    for im in p.imgs:
        if im.get("src"):
            cands.append(im["src"])
            break
    checked, stale = [], []
    for href in cands[:3]:
        u = normalize(base_url, href)
        if not u or not u.startswith("http") or not same_site(u, base_url):
            continue
        ar = fetch(u, max_bytes=256)
        if ar.status != 200:
            continue
        cc = ar.header("cache-control", "").lower()
        m = re.search(r"max-age=(\d+)", cc)
        age = int(m.group(1)) if m else 0
        checked.append(u)
        if "immutable" not in cc and age < 86400:
            stale.append(f"{u.rsplit('/', 1)[-1][:40] or u} (cache-control: {cc or 'none'})")
    findings = []
    if checked and len(stale) == len(checked):
        findings.append(f("LOW", "Static assets lack long-lived caching", "performance", 2, 1,
                          trunc("; ".join(stale), 200),
                          "Serve CSS/JS/images with Cache-Control: max-age=31536000, immutable (use "
                          "fingerprinted filenames) so repeat visits render instantly."))
    return findings


_ANALYTICS_SIGNS = [
    ("GA4 (gtag.js)", r"googletagmanager\.com/gtag/js|gtag\("),
    ("Google Tag Manager", r"googletagmanager\.com/gtm\.js|GTM-[A-Z0-9]{4,}"),
    ("Universal Analytics (deprecated)", r"google-analytics\.com/analytics\.js|['\"]UA-\d{4,}-\d"),
    ("Plausible", r"plausible\.io/js"),
    ("Fathom", r"usefathom\.com"),
    ("Matomo", r"matomo\.js|matomo\.php|piwik\.js"),
    ("Microsoft Clarity", r"clarity\.ms"),
    ("Hotjar", r"static\.hotjar\.com"),
    ("Meta Pixel", r"connect\.facebook\.net/[^\"']*fbevents"),
]


def detect_analytics(home_page):
    """Which measurement stack is installed? Not scored (INFO) — but an audit that
    doesn't mention a missing analytics setup isn't finished."""
    r = home_page.get("response")
    if not r:
        return [], ("Analytics", "not checked")
    txt = r.text[:400_000]
    hits = [name for name, rx in _ANALYTICS_SIGNS if re.search(rx, txt)]
    findings = []
    ua_only = any(h.startswith("Universal") for h in hits) and not any(
        h.startswith(("GA4", "Google Tag Manager", "Plausible", "Fathom", "Matomo",
                      "Microsoft", "Hotjar")) for h in hits)
    if not hits:
        findings.append(f("INFO", "No analytics detected", "on page", 1, 1,
                          "No GA4/GTM/Plausible/Matomo/Clarity/Hotjar snippet in the homepage HTML",
                          "Install analytics (GA4 or a privacy-friendly alternative) and verify the site "
                          "in Google Search Console — you can't improve what you don't measure."))
    elif ua_only:
        findings.append(f("LOW", "Only deprecated Universal Analytics found", "on page", 2, 1,
                          "; ".join(hits),
                          "Universal Analytics stopped processing data in July 2023 — migrate to GA4."))
    return findings, ("Analytics", ", ".join(hits) or "none detected")


# =================================================================================
# Template clustering & stratified sampling
# =================================================================================
# A sitemap lists URLs in whatever order the generator emits them — usually the static
# pages first, then one section alphabetically. Auditing "the first N" therefore audits
# the same corner of the site every time and can miss the template that holds most of
# the pages (a 250-page directory hiding behind 11 static pages). So we cluster URLs by
# path shape and sample across clusters, spreading picks evenly inside each one.
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def url_template(u):
    """A coarse path-shape signature: '/blog/*', '/{year}/*/*/*/', '/about' → '/*'."""
    path = urllib.parse.urlparse(u).path or "/"
    segs = [s for s in path.split("/") if s]
    if not segs:
        return "/"
    first = "{year}" if _YEAR_RE.match(segs[0]) else segs[0]
    if len(segs) == 1:
        return "/*" + ("/" if path.endswith("/") else "")
    return "/" + first + "/*" * (len(segs) - 1) + ("/" if path.endswith("/") else "")


def cluster_urls(urls):
    clusters = {}
    for u in urls:
        clusters.setdefault(url_template(u), []).append(u)
    return clusters


def _spread(items, k):
    """k items evenly spaced through the list (not the first k)."""
    if k <= 0:
        return []
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def stratified_sample(urls, n, exclude=()):
    """Pick up to n URLs across template clusters: every cluster gets one slot first
    (largest clusters first), remaining slots go round-robin by cluster size."""
    ex = {e.rstrip("/") for e in exclude}
    pool = [u for u in dict.fromkeys(urls) if u.rstrip("/") not in ex]
    if n <= 0 or not pool:
        return []
    clusters = sorted(cluster_urls(pool).items(), key=lambda kv: -len(kv[1]))
    quota = {t: 0 for t, _ in clusters}
    left = min(n, len(pool))
    while left > 0:
        progressed = False
        for t, members in clusters:
            if left and quota[t] < len(members):
                quota[t] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    out = []
    for t, members in clusters:
        out.extend(_spread(members, quota[t]))
    return out


MAX_SITEMAP_FILES = 20        # sub-sitemaps fetched per audit (spread across the index)
MAX_SITEMAP_URLS = 50_000


def read_sitemaps(sitemap_files):
    """One capped pass over the sitemap(s): URLs plus the metadata a <loc> scrape drops
    (lastmod coverage, image extension). Large publishers expose an index of thousands of
    sub-sitemaps; we read an evenly spread subset rather than hang on all of them."""
    meta = {"urls": [], "total": 0, "with_lastmod": 0, "lastmods": [], "has_images": False,
            "files_read": 0, "files_listed": 0, "truncated": False}
    seen = set()

    def parse(url, depth=0):
        if url in seen or depth > 3 or meta["files_read"] >= MAX_SITEMAP_FILES:
            return
        seen.add(url)
        r = fetch(url, max_bytes=60_000_000)
        if r.status >= 400 or not r.body:
            return
        meta["files_read"] += 1
        body = r.body
        if url.endswith(".gz") or body[:2] == b"\x1f\x8b":
            try:
                body = gzip.decompress(body)
            except (OSError, EOFError):
                pass
        text = body.decode("utf-8", errors="replace")
        if "<sitemapindex" in text[:4000].lower():
            children = [c.strip() for c in re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.I | re.S)]
            meta["files_listed"] += len(children)
            budget = MAX_SITEMAP_FILES - meta["files_read"]
            if len(children) > budget:
                meta["truncated"] = True
            for c in _spread(children, max(0, budget)):
                parse(c, depth + 1)
            return
        if "image:image" in text:
            meta["has_images"] = True
        for block in re.findall(r"<url\b.*?</url>", text, re.I | re.S):
            if len(meta["urls"]) >= MAX_SITEMAP_URLS:
                meta["truncated"] = True
                break
            loc = re.search(r"<loc>\s*(.*?)\s*</loc>", block, re.I | re.S)
            if not loc:
                continue
            meta["urls"].append(_html_unescape(loc.group(1).strip()))
            meta["total"] += 1
            m = re.search(r"<lastmod>\s*(.*?)\s*</lastmod>", block, re.I | re.S)
            if m:
                meta["with_lastmod"] += 1
                meta["lastmods"].append(m.group(1)[:10])

    for u in sitemap_files:
        parse(u)
    meta["urls"] = list(dict.fromkeys(meta["urls"]))
    return meta


def _html_unescape(u):
    import html as _h
    return _h.unescape(u)


def sitemap_meta_findings(meta):
    out = []
    total, wl = meta["total"], meta["with_lastmod"]
    if total >= 10 and wl < total * 0.8:
        out.append(f("LOW", "Many sitemap URLs carry no <lastmod>", "crawlability", 2, 2,
                     f"{wl} of {total} URLs have a lastmod",
                     "Emit an accurate <lastmod> for every URL. Google and Bing both use it to "
                     "schedule recrawls when it is consistently truthful; pages without one give "
                     "crawlers no signal that they changed."))
    if wl >= 20 and len(set(meta["lastmods"])) == 1:
        out.append(f("LOW", "Every sitemap <lastmod> is the same date", "crawlability", 2, 2,
                     f"all {wl} entries say {meta['lastmods'][0]}",
                     "Set lastmod from each page's real last significant change. A sitemap that "
                     "stamps every URL with the build date teaches crawlers to ignore the field."))
    today = _dt.date.today().isoformat()
    future = [d for d in meta["lastmods"] if d > today]
    if future:
        out.append(f("LOW", "Sitemap <lastmod> dates in the future", "crawlability", 1, 1,
                     f"{len(future)} entries, e.g. {future[0]}",
                     "Fix the date source — future lastmod values are ignored as untrustworthy."))
    return out


# =================================================================================
# AI crawler access — what the EDGE does, not just what robots.txt says
# =================================================================================
# robots.txt is a statement of intent. CDNs and WAFs (Cloudflare's "Block AI bots", bot
# fight modes, custom rules) refuse AI crawlers at the network edge regardless of it, and
# some platforms rewrite robots.txt in flight. The only way to know is to ask: request the
# homepage with each crawler's documented user-agent and compare with a browser.
#
# Caveat, stated in every finding: this probes from a non-crawler IP. A WAF that verifies
# crawler IPs may refuse a spoofed UA while admitting the real bot, so a block here means
# "refused for this UA from here" — confirm in the CDN/WAF dashboard.
#   role: "search" = builds an answer engine's search index · "user" = fetches a page
#   live when a person asks · "train" = collects model-training data.
AI_AGENT_PROBES = [
    ("GPTBot", "train", "OpenAI",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"),
    ("OAI-SearchBot", "search", "OpenAI",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; +https://openai.com/searchbot"),
    ("ChatGPT-User", "user", "OpenAI",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot"),
    ("ClaudeBot", "train", "Anthropic",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
    ("Claude-SearchBot", "search", "Anthropic",
     "Mozilla/5.0 (compatible; Claude-SearchBot/1.0; +https://www.anthropic.com)"),
    ("Claude-User", "user", "Anthropic",
     "Mozilla/5.0 (compatible; Claude-User/1.0; +Claude-User@anthropic.com)"),
    ("PerplexityBot", "search", "Perplexity",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"),
    ("Perplexity-User", "user", "Perplexity",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)"),
    ("Applebot", "search", "Apple",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.1.1 Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)"),
    ("meta-externalagent", "train", "Meta",
     "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)"),
    ("Amazonbot", "search", "Amazon",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Amazonbot/0.1; +https://developer.amazon.com/support/amazonbot) Chrome/119.0.6045.214 Safari/537.36"),
    ("CCBot", "train", "Common Crawl",
     "CCBot/2.0 (https://commoncrawl.org/faq/)"),
    ("DuckAssistBot", "user", "DuckDuckGo",
     "DuckAssistBot/1.2; (+http://duckduckgo.com/duckassistbot.html)"),
    ("MistralAI-User", "user", "Mistral",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; MistralAI-User/1.0; +https://docs.mistral.ai/robots)"),
    ("Bytespider", "train", "ByteDance",
     "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 (compatible; Bytespider; spider-feedback@bytedance.com)"),
]
_CHALLENGE_MARKERS = (b"just a moment", b"attention required", b"cf-chl", b"captcha",
                      b"request was blocked", b"access denied", b"bot detected")


def probe_ai_agents(home_url, robots_text=""):
    """Request the homepage as each AI crawler. Returns (findings, health_rows, matrix)."""
    base = fetch(home_url, max_bytes=8192, ua=BROWSER_UA)
    if base.status != 200:
        return [], [("AI crawler edge access", "not tested (browser baseline was not 200)")], []

    def one(spec):
        token, role, op, ua = spec
        r = fetch(home_url, max_bytes=8192, ua=ua)
        body = (r.body or b"")[:4096].lower()
        if r.status == 200 and not any(m in body for m in _CHALLENGE_MARKERS[:3]):
            verdict = "ok"
        elif r.status == 402:
            verdict = "pay-per-crawl (402)"
        elif r.status in (401, 403, 406, 429, 503) or any(m in body for m in _CHALLENGE_MARKERS):
            verdict = f"refused ({r.status})"
        elif r.status == 0:
            verdict = "no response"
        else:
            verdict = f"HTTP {r.status}"
        return {"agent": token, "role": role, "operator": op, "status": r.status,
                "verdict": verdict, "mitigated": r.header("cf-mitigated", "")}

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        matrix = list(ex.map(one, AI_AGENT_PROBES))

    refused = [m for m in matrix if m["verdict"] != "ok"]
    findings = []
    if refused and len(refused) == len(matrix):
        # Everything non-browser is refused — that is generic bot protection, and it
        # almost certainly verifies crawler IPs, so a spoof test can't tell us more.
        findings.append(f("MEDIUM", "Site refuses every non-browser user-agent we tried", "ai search", 3, 2,
                          f"all {len(matrix)} AI crawler user-agents refused; browser UA got 200",
                          "Generic bot protection is active. It may admit verified crawler IPs, which "
                          "this test cannot imitate — check the CDN/WAF bot settings and confirm "
                          "verified AI search crawlers are allowed."))
    elif refused:
        named = {a.lower() for a in re.findall(r"(?im)^\s*user-agent\s*:\s*([^\s#]+)", robots_text)}
        live = [m for m in refused if m["role"] in ("search", "user")]
        train = [m for m in refused if m["role"] == "train"]
        invited = [m["agent"] for m in refused if m["agent"].lower() in named]
        obs = "; ".join(f"{m['agent']} ({m['role']}) → {m['verdict']}" for m in refused)
        obs += f" · {len(matrix) - len(refused)} other agents and a browser got 200"
        tail = (" robots.txt names " + ", ".join(invited) + " in its own rules, so the file and "
                "the edge disagree." if invited else "")
        if live:
            findings.append(f("HIGH", "AI search / assistant crawlers are refused at the edge", "ai search", 5, 1,
                              trunc(obs, 260),
                              "These agents fetch pages to answer people's questions and to build answer-engine "
                              "indexes; refusing them removes the site from those answers. This is a CDN/WAF "
                              "setting (e.g. Cloudflare Security → Bots → AI bots, or a custom rule), not "
                              "robots.txt. Allow them, then re-run." + tail +
                              " (Tested by user-agent from a non-crawler IP — confirm in the dashboard.)"))
        elif train:
            findings.append(f("HIGH" if invited else "MEDIUM",
                              "AI training crawlers are refused at the edge", "ai search", 4, 1,
                              trunc(obs, 260),
                              "Blocking training crawlers is a legitimate policy choice, but make it a "
                              "deliberate one: these crawls (and Common Crawl, which most open models "
                              "train on) are how models come to know a brand exists without being "
                              "prompted. Many CDNs now enable this block by default. If you want them in, "
                              "lift the CDN/WAF block; if not, say so in robots.txt so the two agree." + tail +
                              " (Tested by user-agent from a non-crawler IP — confirm in the dashboard.)"))
    ok_n = len(matrix) - len(refused)
    health = [("AI crawler edge access",
               f"{ok_n}/{len(matrix)} agents served 200"
               + (" · refused: " + ", ".join(m["agent"] for m in refused) if refused else ""))]
    return findings, health, matrix


def robots_platform_signals(text):
    """Things in robots.txt that are not allow/disallow: CDN-managed blocks and
    Content-Signal (AI usage preference) lines."""
    low = text.lower()
    managed = "cloudflare managed" in low
    signals = re.findall(r"(?im)^\s*content-signal\s*:\s*(.+)$", text)
    parsed = {}
    for line in signals:
        for part in line.split(","):
            k, _, v = part.strip().partition("=")
            if k and v:
                parsed.setdefault(k.strip().lower(), set()).add(v.strip().lower())
    findings = []
    if "no" in parsed.get("search", ()):
        findings.append(f("HIGH", "robots.txt Content-Signal says search=no", "crawlability", 4, 1,
                          "Content-Signal: " + "; ".join(signals)[:160],
                          "search=no asks crawlers not to build a search index from the site. Unless "
                          "that is intended, change it to search=yes (CDN-managed robots.txt settings "
                          "often add this line)."))
    if "no" in parsed.get("ai-input", ()):
        findings.append(f("MEDIUM", "robots.txt Content-Signal says ai-input=no", "ai search", 3, 1,
                          "Content-Signal: " + "; ".join(signals)[:160],
                          "ai-input=no asks AI systems not to use the content in generated answers "
                          "(retrieval / grounding). Set ai-input=yes if you want to be cited."))
    row = ("robots.txt platform signals",
           (("CDN-managed block present · " if managed else "")
            + ("Content-Signal: " + "; ".join(f"{k}={'/'.join(sorted(v))}" for k, v in parsed.items())
               if parsed else "no Content-Signal line")))
    return findings, row


# =================================================================================
# Link architecture — where internal links actually point
# =================================================================================
def _head_signals(resp):
    """canonical / robots from a (possibly truncated) HTML response."""
    p = PageParser()
    try:
        p.feed(resp.text[:200_000])
    except Exception:
        pass
    noindex = ("noindex" in p.meta_robots or "noindex" in resp.header("x-robots-tag", "").lower())
    can = normalize(resp.final_url, p.canonical) if p.canonical else ""
    return can or "", noindex, p


def check_link_architecture(pages, crawled_urls, sm_urls, limit=40):
    """Sample internal link targets (stratified by template) and classify each:
    broken · server error · redirecting · canonicalized elsewhere · noindexed.
    Then ask the structural questions a page-by-page audit can't:
      * do internal links point at the URLs the site wants indexed?
      * does a canonical hand indexing to a thinner page than the one carrying it?
      * is a whole sitemap template reachable only through the sitemap?"""
    seen = {u.rstrip("/") for u in crawled_urls}
    link_set, candidates = set(), []
    for page in pages:
        p = page.get("parser")
        if not p:
            continue
        for href in p.links:
            u = normalize(page["url"], href)
            if not u or not u.startswith("http") or not same_site(u, page["url"]):
                continue
            if u.rstrip("/") != (page.get("final_url") or page["url"]).rstrip("/") \
                    and u.rstrip("/") != page["url"].rstrip("/"):
                link_set.add(u.rstrip("/"))      # self-links don't count as inlinks
            if _SKIP_LINK_RE.search(u) or u.rstrip("/") in seen:
                continue
            seen.add(u.rstrip("/"))
            candidates.append(u)
    findings, summary = [], {}
    if not candidates:
        return findings, [("Internal links checked", "no uncrawled internal links found")], summary
    sample = stratified_sample(candidates, limit)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda u: fetch(u, max_bytes=200_000), sample))

    broken, errors5, redirs, slash_redirs, noncanon, noindexed = [], [], [], [], [], []
    for u, r in zip(sample, results):
        if r.status in (404, 410):
            broken.append(u)
            continue
        if r.status >= 500:
            errors5.append((u, r.status))
            continue
        if r.chain:
            (slash_redirs if _trivial_redirect(u, r.final_url) else redirs).append(u)
        if r.status == 200 and "html" in r.header("content-type", "").lower():
            can, noindex, _ = _head_signals(r)
            if noindex:
                noindexed.append(u)
            elif can and same_site(can, u) and (urllib.parse.urlparse(can).path.rstrip("/")
                                                != urllib.parse.urlparse(r.final_url).path.rstrip("/")):
                # path differs (a ?query variant canonicalizing to its clean URL is fine)
                noncanon.append((u, can))

    n = len(sample)
    if broken:
        findings.append(f("HIGH", f"Broken internal links ({len(broken)} of {n} sampled)",
                          "crawlability", 4, 2, trunc("; ".join(broken[:4]), 220),
                          "Fix or remove links to these URLs (or 301 them to the right page) — broken "
                          "links leak crawl budget, link equity, and user trust."))
    if errors5:
        findings.append(f("MEDIUM", f"Internal links hit server errors ({len(errors5)})",
                          "crawlability", 3, 3,
                          trunc("; ".join(f"{u} ({s})" for u, s in errors5[:4]), 220),
                          "Investigate the 5xx responses on these linked URLs."))
    if len(redirs) >= 5:
        findings.append(f("LOW", f"Internal links point at redirects ({len(redirs)} of {n})",
                          "crawlability", 2, 2, trunc("; ".join(redirs[:4]), 200),
                          "Update internal links to the final URLs so crawlers and users skip the extra hop."))
    if len(slash_redirs) >= 3:
        findings.append(f("LOW", f"Internal links use URL variants that 301 ({len(slash_redirs)} of {n})",
                          "crawlability", 2, 1, trunc("; ".join(slash_redirs[:4]), 200),
                          "These links differ from the final URL only by trailing slash, scheme, or "
                          "www, and each costs a redirect hop. Link the exact canonical form."))

    if len(noncanon) >= 3 and len(noncanon) >= n * 0.15:
        targets = {c.rstrip("/") for _, c in noncanon}
        linked = [c for c in targets if c in link_set]
        orphaned = not linked
        findings.append(f(
            "HIGH" if orphaned else "MEDIUM",
            "Internal links point at non-canonical URLs", "crawlability", 4, 2,
            f"{len(noncanon)} of {n} sampled link targets declare a different canonical, e.g. "
            f"{urllib.parse.urlparse(noncanon[0][0]).path} → {urllib.parse.urlparse(noncanon[0][1]).path}"
            + (". None of those canonical URLs is linked from any crawled page." if orphaned else ""),
            "Point internal links (navigation, cards, breadcrumbs, llms.txt, feeds) at the canonical "
            "URL itself. Links to a page that says 'index that other URL instead' pass their signals "
            "through a hint Google may or may not honor, and leave the canonical pages with no "
            "internal links of their own."))

    # Does the canonical hand indexing to a thinner page? Compare up to 4 pairs in full.
    thinner = []
    for u, can in noncanon[:4]:
        a, b = fetch(u), fetch(can)
        if a.status != 200 or b.status != 200:
            continue
        _, _, pa = _head_signals(a)
        _, _, pb = _head_signals(b)
        ta, _, _ = parse_jsonld_types(pa.jsonld)
        tb, _, _ = parse_jsonld_types(pb.jsonld)
        if pa.word_count - pb.word_count >= 150 and pb.word_count < pa.word_count * 0.75:
            thinner.append((u, can, pa.word_count, pb.word_count, sorted(ta - tb)))
    if thinner:
        u, can, wa, wb, lost = thinner[0]
        findings.append(f(
            "HIGH", "Canonical points at a thinner page than the one carrying it", "crawlability", 5, 3,
            f"{urllib.parse.urlparse(u).path} ({wa} words) → canonical "
            f"{urllib.parse.urlparse(can).path} ({wb} words)"
            + (f"; schema only on the non-canonical page: {', '.join(lost)}" if lost else "")
            + (f" · same pattern on {len(thinner)} of {min(4, len(noncanon))} pairs checked" if len(thinner) > 1 else ""),
            "rel=canonical tells search engines to index the target and drop this page, so content "
            "that exists only here is never indexed. Move the unique content and structured data "
            "onto the canonical URL (or make this page the canonical one)."))

    if len(noindexed) >= 5 or (n >= 10 and len(noindexed) >= n * 0.25):
        findings.append(f("MEDIUM" if len(noindexed) >= n * 0.4 else "LOW",
                          f"Internal links lead to noindexed pages ({len(noindexed)} of {n} sampled)",
                          "crawlability", 3, 2, trunc("; ".join(noindexed[:4]), 220),
                          "Every crawlable link to a noindex page spends crawl budget on a dead end. "
                          "Render empty/utility destinations as plain text, remove the links, or give "
                          "the pages real content and let them be indexed."))

    # Sitemap templates that no crawled page links to (sitemap-only = weakly discoverable,
    # no internal PageRank). Only meaningful for sizeable templates.
    orphan_templates = []
    for tmpl, members in cluster_urls(sm_urls).items():
        if len(members) >= 10 and not any(m.rstrip("/") in link_set for m in members):
            orphan_templates.append((tmpl, len(members)))
    # Only meaningful when the crawl saw a real share of the site: on a 50,000-URL
    # sitemap, "none of our 100 pages links to it" says nothing.
    if orphan_templates and len(pages) >= 8 and len(sm_urls) <= 40 * len(pages):
        orphan_templates.sort(key=lambda x: -x[1])
        findings.append(f(
            "MEDIUM", "A sitemap template gets no internal links from any crawled page", "crawlability", 4, 3,
            "; ".join(f"{t} ({c} URLs)" for t, c in orphan_templates[:3])
            + f" — 0 linked from the {len(pages)} pages crawled (hubs included)",
            "Pages reachable only via the sitemap are crawled late, ranked weakly, and invisible to "
            "crawlers that ignore sitemaps. Link them from hub, category, and related-item pages."))

    summary = {"sampled": n, "broken": len(broken), "redirecting": len(redirs) + len(slash_redirs),
               "non_canonical": len(noncanon), "noindexed": len(noindexed),
               "orphan_templates": orphan_templates}
    health = [("Internal links checked",
               f"{n} sampled across templates · {len(broken)} broken · "
               f"{len(redirs) + len(slash_redirs)} redirecting · {len(noncanon)} non-canonical · "
               f"{len(noindexed)} noindexed")]
    return findings, health, summary


def check_head_parity(home_page, extra_urls):
    """Some frameworks route only GET. Link checkers, uptime monitors, several social
    unfurlers and CDN validators send HEAD first and conclude the resource is gone."""
    urls = [home_page["final_url"] or home_page["url"]] + [u for u in extra_urls if u]
    bad = []
    for u in list(dict.fromkeys(urls))[:8]:
        g = fetch(u, max_bytes=512)
        if g.status != 200:
            continue
        h = fetch(u, method="HEAD")
        if h.status >= 400:
            bad.append(f"{urllib.parse.urlparse(u).path or '/'} GET 200 / HEAD {h.status}")
    if bad:
        return [f("MEDIUM", "HEAD requests fail where GET succeeds", "crawlability", 3, 1,
                  trunc("; ".join(bad), 220),
                  "Answer HEAD exactly like GET minus the body. Link checkers, monitors and some "
                  "preview bots send HEAD first and will report these URLs (often images) as broken.")]
    return []


# =================================================================================
# Static performance checks beyond the HTML document
# =================================================================================
def _content_length(resp):
    try:
        return int(resp.header("content-length") or 0) or len(resp.body)
    except ValueError:
        return len(resp.body)


def check_hero_and_thumbs(page):
    """The likely LCP image and the small images, judged by bytes on the wire."""
    p, findings = page.get("parser"), []
    if not p or page.get("status") != 200:
        return findings
    url = page["url"]
    hero = next((im for im in p.imgs if im["fetchpriority"] == "high" and im["src"]), None)
    if not hero:
        for lt in p.link_tags:
            if "preload" in (lt.get("rel") or "").lower() and (lt.get("as") or "").lower() == "image" and lt.get("href"):
                hero = {"src": lt["href"], "loading": "", "srcset": bool(lt.get("imagesrcset"))}
                break
    if not hero:
        for im in p.imgs[:6]:
            w = int(im["width"]) if str(im["width"]).isdigit() else 0
            if im["src"] and not im["src"].startswith("data:") and im["loading"] != "lazy" and w >= 600:
                hero = im
                break
    if hero:
        hu = normalize(url, hero["src"])
        if hu and hu.startswith("http"):
            r = fetch(hu, max_bytes=3_000_000, ua=BROWSER_UA)
            if r.status == 200:
                kb = _content_length(r) // 1024
                cross = not same_site(hu, url)
                if hero.get("loading") == "lazy":
                    findings.append(f("MEDIUM", "Priority image is lazy-loaded", "performance", 3, 1,
                                      trunc(hu, 120),
                                      'Remove loading="lazy" from the hero/LCP image — lazy-loading the '
                                      "largest above-the-fold image delays LCP."))
                if cross and r.chain:
                    findings.append(f("MEDIUM", "Hero image is hotlinked cross-origin through redirects", "performance", 4, 2,
                                      f"{urllib.parse.urlparse(hu).hostname} · {len(r.chain)} redirect hop(s) · {kb} KB"
                                      + (" — also far too heavy for an LCP image" if kb > 300 else ""),
                                      "Host the hero image on your own origin/CDN at the displayed size (AVIF/WebP, srcset). A "
                                      "third-party host adds DNS+TLS setup and redirect round trips before the "
                                      "first image byte, can set third-party cookies, and can change or vanish."))
                elif cross:
                    findings.append(f("LOW", "Hero image is served from a third-party origin", "performance", 3, 2,
                                      f"{urllib.parse.urlparse(hu).hostname} · {kb} KB",
                                      "Self-host the LCP image (or preconnect to its origin) to cut connection setup from LCP."))
                if kb > 300 and not (cross and r.chain):   # size already reported above
                    findings.append(f("MEDIUM", "Hero image is very heavy", "performance", 4, 2,
                                      f"{kb} KB · {trunc(hu, 100)}",
                                      "Serve the LCP image as AVIF/WebP at the displayed width with srcset/sizes; "
                                      "aim for well under 150 KB on mobile."))
                elif kb > 150 and not hero.get("srcset"):
                    findings.append(f("LOW", "Hero image is heavy and has no srcset", "performance", 3, 2,
                                      f"{kb} KB · {trunc(hu, 100)}",
                                      "Add srcset/sizes and a modern format so phones don't download the desktop file."))
    # small displayed images that are large files
    small = [im for im in p.imgs if str(im["width"]).isdigit() and 0 < int(im["width"]) <= 160
             and im["src"] and not im["src"].startswith("data:")]
    checked, heavy = 0, []
    for im in _spread(small, 6):
        iu = normalize(url, im["src"])
        if not iu or not iu.startswith("http"):
            continue
        r = fetch(iu, max_bytes=400_000, ua=BROWSER_UA)
        if r.status != 200:
            continue
        checked += 1
        b = _content_length(r)
        if b > 25 * 1024:
            heavy.append((b, int(im["width"]), iu))
    if checked >= 3 and len(heavy) >= max(2, checked // 2):
        avg = sum(b for b, _, _ in heavy) // len(heavy) // 1024
        findings.append(f("MEDIUM" if len(small) >= 12 else "LOW",
                          "Small images are served from large files", "performance", 3, 2,
                          f"{len(heavy)} of {checked} sampled ≤160px images average {avg} KB each; "
                          f"{len(small)} such images on the page (≈{avg * len(small)} KB if typical)",
                          "Generate thumbnails at 2× the displayed size (WebP/AVIF). A 60–160px logo "
                          "should be a few KB, not tens."))
    return findings


def check_fonts(home_page):
    p = home_page.get("parser")
    if not p:
        return []
    findings = []
    gf = [lt.get("href") for lt in p.link_tags
          if "stylesheet" in (lt.get("rel") or "").lower() and "fonts.googleapis.com" in (lt.get("href") or "")]
    if not gf:
        return findings
    preconnects = " ".join((lt.get("href") or "") for lt in p.link_tags if "preconnect" in (lt.get("rel") or "").lower())
    if "fonts.gstatic.com" not in preconnects:
        findings.append(f("LOW", "Google Fonts without preconnect to fonts.gstatic.com", "performance", 2, 1,
                          "stylesheet from fonts.googleapis.com, no <link rel=preconnect href=https://fonts.gstatic.com crossorigin>",
                          "Add the preconnect (with crossorigin), or self-host the fonts and preload them."))
    if any("display=" not in (h or "") for h in gf):
        findings.append(f("LOW", "Google Fonts requested without display=swap", "performance", 2, 1,
                          trunc(gf[0], 120), "Append &display=swap so text renders immediately in a fallback font."))
    total, files = 0, 0
    for href in gf[:2]:
        css = fetch(normalize(home_page["url"], href), ua=BROWSER_UA)
        if css.status != 200:
            continue
        blocks = re.findall(r"/\*\s*([\w-]+)\s*\*/\s*@font-face\s*{[^}]*?url\((https:[^)]+)\)", css.text)
        urls = [u for name, u in blocks if name == "latin"] or [u for _, u in blocks[:6]]
        for fu in list(dict.fromkeys(urls))[:8]:
            fr = fetch(fu, ua=BROWSER_UA)
            if fr.status == 200:
                total += len(fr.body)
                files += 1
    if files and total > 150 * 1024:
        findings.append(f("MEDIUM" if total > 250 * 1024 else "LOW", "Web font payload is heavy", "performance", 3, 2,
                          f"≈{total // 1024} KB across {files} Latin woff2 file(s) from Google Fonts",
                          "Drop unused families/axes (italic variable axes are often the largest file), "
                          "narrow weight/optical-size ranges, or self-host a subset with preload and "
                          "immutable caching. On text-heavy pages fonts can be the largest download."))
    return findings


def _csp_allows(src_list, script_url, page_url):
    su = urllib.parse.urlparse(script_url)
    host, scheme = (su.hostname or "").lower(), su.scheme
    for tok in src_list:
        t = tok.strip().lower()
        if t in ("*",):
            return True
        if t == "'self'" and same_site(script_url, page_url) and \
                (su.hostname or "") == (urllib.parse.urlparse(page_url).hostname or ""):
            return True
        if t in ("https:", "http:") and scheme + ":" == t:
            return True
        if t.startswith("'"):
            continue
        th = re.sub(r"^[a-z]+://", "", t).split("/")[0].split(":")[0]
        if th.startswith("*."):
            if host.endswith(th[1:]) and host != th[2:]:
                return True
        elif th == host:
            return True
    return False


def check_csp_blocks(page):
    """A CSP that forbids a script the page itself ships (classic: a CDN-injected RUM /
    analytics beacon) means that script silently never runs."""
    r, p = page.get("response"), page.get("parser")
    if not r or not p:
        return []
    csp = r.header("content-security-policy")
    if not csp:
        return []
    directives = {}
    for part in csp.split(";"):
        bits = part.strip().split()
        if bits:
            directives[bits[0].lower()] = bits[1:]
    srcs = directives.get("script-src-elem") or directives.get("script-src") or directives.get("default-src")
    if srcs is None or "'strict-dynamic'" in [s.lower() for s in srcs]:
        return []
    blocked = []
    for s in p.scripts:
        if not s["src"] or s["nonce"] or s["type"] == "application/ld+json":
            continue
        su = normalize(page["url"], s["src"])
        if su and su.startswith("http") and not _csp_allows(srcs, su, page["final_url"] or page["url"]):
            blocked.append(urllib.parse.urlparse(su).hostname or su)
    blocked = sorted(set(blocked))
    if blocked:
        return [f("MEDIUM", "Content-Security-Policy blocks a script the page loads", "performance", 3, 1,
                  "script-src does not allow: " + ", ".join(blocked[:4]),
                  "Either add the host to script-src or stop loading the script. A blocked real-user "
                  "monitoring/analytics beacon means no field Core Web Vitals or traffic data, plus a "
                  "console error on every page view.")]
    return []


def check_head_order(page):
    p = page.get("parser")
    if not p or not p.stylesheet_seq:
        return []
    first_css = min(p.stylesheet_seq)
    early = [s for s in p.scripts if s["src"] and s["in_head"] and s["seq"] < first_css
             and not same_site(normalize(page["url"], s["src"]) or "", page["url"])]
    if early:
        host = urllib.parse.urlparse(normalize(page["url"], early[0]["src"])).hostname
        return [f("LOW", "Third-party script is requested before the stylesheet", "performance", 2, 1,
                  f"{host} <script> precedes the first stylesheet in <head>",
                  "Put charset/viewport/title, preconnects and CSS first; move analytics and tag "
                  "scripts after the stylesheet. Even async scripts compete for bandwidth with "
                  "render-critical CSS and fonts on slow connections.")]
    return []


def check_html_caching(page):
    r = page.get("response")
    if not r or page.get("status") != 200:
        return []
    cc = r.header("cache-control", "").lower()
    if "no-store" in cc:
        return [f("LOW", "HTML is served Cache-Control: no-store", "performance", 2, 2,
                  f"cache-control: {cc}",
                  "no-store forbids every cache and can make the page ineligible for the browser's "
                  "back/forward cache, so Back re-fetches it. For personalized pages prefer "
                  "'private, max-age=0, must-revalidate'; if the page varies by visitor location, "
                  "note that crawlers only ever see one variant.")]
    return []


# =================================================================================
# Trust & launch hygiene beyond headers
# =================================================================================
def _doh(name, rtype):
    """DNS-over-HTTPS JSON lookup (stdlib only). Returns list of answer strings, or None
    when the lookup itself failed (so callers never report a false 'missing')."""
    for ep in ("https://cloudflare-dns.com/dns-query", "https://dns.google/resolve"):
        r = fetch(f"{ep}?name={urllib.parse.quote(name)}&type={rtype}",
                  extra_headers={"Accept": "application/dns-json"})
        if r.status == 200:
            try:
                data = json.loads(r.text)
                return [a.get("data", "") for a in data.get("Answer", []) or []]
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _registrable_candidates(host):
    parts = host.lower().lstrip(".").split(".")
    if parts and parts[0] == "www":
        parts = parts[1:]
    return [".".join(parts[i:]) for i in range(0, max(1, len(parts) - 1))]


def check_domain_expiry(host):
    """Registration expiry via RDAP (rdap.org bootstrap redirector). A lapsed domain is
    the one failure that takes every ranking with it."""
    for cand in _registrable_candidates(host)[:3]:
        r = fetch("https://rdap.org/domain/" + cand, extra_headers={"Accept": "application/rdap+json"})
        if r.status != 200:
            continue
        try:
            data = json.loads(r.text)
        except (json.JSONDecodeError, ValueError):
            continue
        exp = next((e.get("eventDate") for e in data.get("events", [])
                    if e.get("eventAction") == "expiration"), None)
        if not exp:
            return [], ("Domain registration", f"{cand}: RDAP gives no expiry date")
        try:
            d = _dt.date.fromisoformat(exp[:10])
        except ValueError:
            return [], ("Domain registration", f"{cand}: unreadable expiry {exp}")
        days = (d - _dt.date.today()).days
        findings = []
        if days <= 30:
            findings.append(f("HIGH", "Domain registration expires within 30 days", "trust", 5, 1,
                              f"{cand} expires {d.isoformat()} ({days} days)",
                              "Renew now and turn on auto-renew with a valid payment method. An expired "
                              "domain drops out of search and can be re-registered by someone else."))
        elif days <= 60:
            findings.append(f("MEDIUM", "Domain registration expires within 60 days", "trust", 4, 1,
                              f"{cand} expires {d.isoformat()} ({days} days)",
                              "Confirm auto-renew is on and the card on file is valid."))
        return findings, ("Domain registration", f"{cand} expires {d.isoformat()} ({days} days)")
    return [], ("Domain registration", "could not read RDAP record")


def check_contact_domains(pages, site_host, security_txt=""):
    """Every mailto: domain the site publishes (and security.txt's Contact) should
    actually receive mail. Stale addresses on a retired domain are common after a rebrand."""
    domains = {}
    for page in pages:
        p = page.get("parser")
        if not p:
            continue
        for href in p.links:
            if href.lower().startswith("mailto:"):
                addr = urllib.parse.unquote(href[7:].split("?")[0]).strip()
                if "@" in addr:
                    domains.setdefault(addr.rsplit("@", 1)[1].lower(), set()).add(page["url"])
    for m in re.findall(r"(?im)^contact:\s*mailto:([^\s]+)", security_txt or ""):
        if "@" in m:
            domains.setdefault(m.rsplit("@", 1)[1].lower(), set()).add("/.well-known/security.txt")
    dead = []
    for dom, where in list(domains.items())[:6]:
        mx = _doh(dom, "MX")
        if mx is None:
            continue
        if not mx:
            a = _doh(dom, "A")
            if a is not None and not a:
                dead.append(f"{dom} (no MX or A record; used on {sorted(where)[0]})")
            elif a is not None:
                dead.append(f"{dom} (no MX record; used on {sorted(where)[0]})")
    findings = []
    if dead:
        findings.append(f("MEDIUM", "Published contact address uses a domain that cannot receive mail", "trust", 3, 1,
                          trunc("; ".join(dead), 220),
                          "Replace the address with one on a live mail domain. A contact route that "
                          "bounces is a trust failure for customers, quality raters, and security researchers."))
    return findings, sorted(domains)


def check_email_auth(site_host, mail_domains):
    dom = _registrable_candidates(site_host)[-1] if _registrable_candidates(site_host) else site_host
    if dom not in mail_domains and not any(d.endswith(dom) for d in mail_domains):
        return [], None
    dmarc = _doh("_dmarc." + dom, "TXT")
    spf = _doh(dom, "TXT")
    if dmarc is None or spf is None:
        return [], ("Email authentication", "could not query DNS")
    has_dmarc = any("v=dmarc1" in t.lower() for t in dmarc)
    has_spf = any("v=spf1" in t.lower() for t in spf)
    findings = []
    if not has_dmarc or not has_spf:
        missing = [n for n, ok in (("SPF", has_spf), ("DMARC", has_dmarc)) if not ok]
        findings.append(f("LOW", "Sending domain lacks " + " and ".join(missing), "trust", 2, 1,
                          f"{dom}: " + ", ".join(missing) + " record not found",
                          "Publish SPF and a DMARC policy. Without them, mail from the address you "
                          "publish is easy to spoof and more likely to land in spam."))
    return findings, ("Email authentication", f"{dom}: SPF {'✓' if has_spf else '✗'} · DMARC {'✓' if has_dmarc else '✗'}")


def check_security_txt(base):
    r = fetch(normalize(base, "/.well-known/security.txt"), max_bytes=20_000)
    if r.status != 200 or b"<html" in r.body[:500].lower() or b"contact:" not in r.body.lower():
        return [], ("security.txt", "not published (optional)"), ""
    text = r.text
    findings = []
    m = re.search(r"(?im)^expires:\s*(\S+)", text)
    if m:
        try:
            if _dt.date.fromisoformat(m.group(1)[:10]) < _dt.date.today():
                findings.append(f("LOW", "security.txt has expired", "trust", 1, 1, f"Expires: {m.group(1)}",
                                  "Update the Expires field (RFC 9116 requires a future date)."))
        except ValueError:
            pass
    else:
        findings.append(f("LOW", "security.txt has no Expires field", "trust", 1, 1, "Expires: missing",
                          "RFC 9116 requires Contact and Expires."))
    return findings, ("security.txt", "published"), text


_EXPOSED = [("/.git/HEAD", rb"^ref:\s|^[0-9a-f]{40}\s*$", "Git repository metadata"),
            ("/.env", rb"(?m)^[A-Z][A-Z0-9_]{2,}\s*=", "environment file"),
            ("/.DS_Store", rb"^\x00\x00\x00\x01Bud1", "macOS directory listing")]


def check_exposed_files(base):
    hits = []
    for path, sig, label in _EXPOSED:
        r = fetch(normalize(base, path), max_bytes=2048)
        if r.status == 200 and b"<html" not in r.body[:300].lower() and re.search(sig, r.body[:2048]):
            hits.append(f"{path} ({label})")
    if hits:
        return [f("CRITICAL", "Private files are publicly downloadable", "trust", 5, 1, "; ".join(hits),
                  "Block these paths at the server/CDN and rotate any credentials they contain. An "
                  "exposed .git or .env lets anyone rebuild your source and secrets.")]
    return []


def check_hsts_quality(home_page):
    r = home_page.get("response")
    if not r:
        return None
    hsts = r.header("strict-transport-security")
    if not hsts:
        return None
    m = re.search(r"max-age=(\d+)", hsts)
    age = int(m.group(1)) if m else 0
    notes = []
    if age < 15552000:
        notes.append("max-age under 6 months")
    if "includesubdomains" not in hsts.lower():
        notes.append("no includeSubDomains")
    return ("HSTS", hsts + (" · " + ", ".join(notes) if notes else " · strong"))


# =================================================================================
# Structured data depth
# =================================================================================
# Types whose Google rich results were withdrawn or restricted. Markup is harmless, but
# nobody should expect a SERP feature from it. (Dates per Google Search Central.)
RETIRED_RICH_RESULTS = {
    "HowTo": "rich result removed (Sept 2023)",
    "FAQPage": "rich result limited to well-known government and health sites (Aug 2023)",
    "SpecialAnnouncement": "rich result retired (2025)",
    "ClaimReview": "rich result retired (2025)",
    "VehicleListing": "rich result retired (2025)",
    "EstimatedSalary": "rich result retired (2025)",
    "LearningVideo": "rich result retired (2025)",
    "CourseInfo": "rich result retired (2025)",
    "BookAction": "rich result retired (2025)",
}
_SELF_SERVING = {"LocalBusiness", "Organization", "Restaurant", "Store", "Corporation",
                 "ProfessionalService", "MedicalBusiness", "LodgingBusiness", "TouristAttraction"}


def _jsonld_nodes(parser):
    nodes = []

    def walk(n):
        if isinstance(n, dict):
            nodes.append(n)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    for raw in parser.jsonld:
        try:
            walk(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            pass
    return nodes


def _types(node):
    t = node.get("@type")
    return set([t] if isinstance(t, str) else [str(x) for x in (t or [])])


def check_schema_depth(page, is_home):
    p = page.get("parser")
    if not p or not p.jsonld:
        return []
    findings, nodes = [], _jsonld_nodes(p)
    site_host = re.sub(r"^www\.", "", urllib.parse.urlparse(page["url"]).hostname or "")
    for n in nodes:
        ts = _types(n)
        if ts & {"Article", "BlogPosting", "NewsArticle"}:
            missing = [k for k in ("headline", "image", "datePublished", "dateModified", "author") if not n.get(k)]
            auth = n.get("author")
            auth = auth[0] if isinstance(auth, list) and auth else auth
            if isinstance(auth, dict) and not (auth.get("url") or auth.get("sameAs")):
                missing.append("author.url")
            if missing:
                findings.append(f("LOW", "Article schema is missing recommended properties", "schema", 2, 1,
                                  "missing: " + ", ".join(missing),
                                  "Add them: image is needed for article rich results and Discover, dates are the "
                                  "freshness signal engines read, and author.url/sameAs is how an author is "
                                  "resolved to a real entity."))
            break
    for n in nodes:
        ts = _types(n)
        if ts & _SELF_SERVING and (n.get("aggregateRating") or n.get("review")):
            nurl = str(n.get("url") or n.get("@id") or "")
            own = (not nurl) or site_host in nurl
            if own:
                findings.append(f("MEDIUM", "Self-serving review markup on the business's own entity", "schema", 3, 1,
                                  f"{'/'.join(sorted(ts & _SELF_SERVING))} with aggregateRating/review on its own site",
                                  "Google ignores review stars for LocalBusiness/Organization when the site "
                                  "controls the reviews about itself, and ratings copied from Google/Yelp/"
                                  "Tripadvisor violate the review-snippet guidelines. Remove it, or mark up "
                                  "first-party reviews of products/services you sell instead."))
                break
    if is_home:
        org = next((n for n in nodes if _types(n) & {"Organization", "Corporation", "LocalBusiness", "NGO"}), None)
        if org:
            want = [("logo", "logo"), ("sameAs", "sameAs"), ("legalName", "legalName"),
                    ("address", "address"), ("foundingDate", "foundingDate")]
            missing = [lbl for k, lbl in want if not org.get(k)]
            if not (org.get("contactPoint") or org.get("email") or org.get("telephone")):
                missing.append("contactPoint/email/telephone")
            if len(missing) >= 2:
                findings.append(f("LOW", "Homepage Organization schema is thin", "schema", 3, 1,
                                  "missing: " + ", ".join(missing),
                                  "Put the full entity definition on the homepage node: legalName, logo, "
                                  "sameAs, contact, address, foundingDate (and founder). For a new or "
                                  "little-known brand this is the cheapest way to tell search and answer "
                                  "engines who stands behind the site."))
    all_types = set().union(*[_types(n) for n in nodes]) if nodes else set()
    retired = sorted(all_types & set(RETIRED_RICH_RESULTS))
    if retired:
        findings.append(f("INFO", "Schema types that no longer earn a Google rich result", "schema", 1, 1,
                          "; ".join(f"{t}: {RETIRED_RICH_RESULTS[t]}" for t in retired),
                          "No action required — the markup still describes the page to machines. Just "
                          "don't count on a SERP feature from it, and keep it matching visible content."))
    return findings


# =================================================================================
# Answer-engine readiness (what AI search can extract, date, and attribute)
# =================================================================================
# Plumbing types sort last so the table shows what the page is ABOUT first.
_STRUCTURAL_TYPES = {"BreadcrumbList", "ListItem", "ItemList", "ImageObject", "WebSite", "WebPage",
                     "SearchAction", "EntryPoint", "PostalAddress", "Answer", "Question",
                     "ContactPoint", "GeoCoordinates", "OpeningHoursSpecification", "ReadAction"}
_QUESTION_RE = re.compile(r"^(who|what|when|where|why|how|which|can|does|do|is|are|should|will)\b.*\?$|\?$", re.I)
_DATE_TEXT_RE = re.compile(
    r"\b(updated|last reviewed|last updated|published|reviewed|as of)\b[^.]{0,40}\b(19|20)\d{2}\b", re.I)
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3}\s+"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Hwy|Highway|Pkwy|Suite|Ste)\b\.?")


def check_answer_readiness(page):
    """Per-page extractability signals. Snippet restrictions are scored; the rest is
    recorded for the readiness table because evidence for each is correlational."""
    p, r = page.get("parser"), page.get("response")
    if not p or not r or page.get("status") != 200:
        return [], None
    findings = []
    robots = p.meta_robots + " " + r.header("x-robots-tag", "").lower()
    if "nosnippet" in robots or re.search(r"max-snippet\s*:\s*0\b", robots):
        findings.append(f("HIGH", "Snippets are disabled for this page", "ai search", 4, 1,
                          trunc(robots.strip(), 120),
                          "nosnippet / max-snippet:0 removes the text snippet in Google Search and also "
                          "keeps the page out of AI Overviews and AI Mode, which honor the same controls. "
                          "Remove it unless that is the goal."))
    elif (m := re.search(r"max-snippet\s*:\s*(\d+)", robots)) and 0 < int(m.group(1)) < 50:
        findings.append(f("LOW", "Snippet length is tightly capped", "ai search", 2, 1, m.group(0),
                          "A very small max-snippet limits how much of the page search and AI features can quote."))
    words = p.word_count
    if p.nosnippet_attrs and words and p.nosnippet_attrs >= 5:
        findings.append(f("LOW", "Many elements are marked data-nosnippet", "ai search", 2, 1,
                          f"{p.nosnippet_attrs} elements", "Check that the passages you want quoted aren't excluded."))
    nodes = _jsonld_nodes(p)
    has_date_schema = any(n.get("dateModified") or n.get("datePublished") for n in nodes)
    visible_date = bool(p.time_tags or _DATE_TEXT_RE.search(p.visible_text[:20000]))
    article_like = any(_types(n) & {"Article", "BlogPosting", "NewsArticle"} for n in nodes)
    if article_like and not visible_date:
        findings.append(f("LOW", "Article shows no visible publish/update date", "ai search", 2, 1,
                          "date only in schema" if has_date_schema else "no date in schema or text",
                          "Show a human-readable published/updated date (ideally in a <time datetime> "
                          "element) that matches dateModified. Answer engines favor content they can date, "
                          "and a schema-only date can be treated as unverified."))
    heads = p.headings["h2"] + p.headings["h3"]
    stats = {
        "url": page["url"], "words": words,
        "question_headings": sum(1 for h in heads if _QUESTION_RE.search(h.strip())),
        "subheadings": len(heads), "lists": p.list_count, "tables": p.table_count,
        "dated": "visible" if visible_date else ("schema only" if has_date_schema else "none"),
        "schema": ", ".join(sorted(page.get("schema_types", []),
                                   key=lambda t: (t in _STRUCTURAL_TYPES, t))[:4]) or "none",
    }
    return findings, stats


def check_entity_transparency(pages, home_page):
    """Who is behind the site? An address normally lives on the about/contact/legal
    pages, so fetch those (they are rarely in a sample) before saying it is missing."""
    have = {pg["url"].rstrip("/") for pg in pages}
    wanted = {}
    for pg in pages:
        p = pg.get("parser")
        for href in (p.links if p else []):
            u = normalize(pg["url"], href)
            if not u or not u.startswith("http") or not same_site(u, pg["url"]) or u.rstrip("/") in have:
                continue
            path = urllib.parse.urlparse(u).path
            for kind, rx in _TRUST_PATTERNS.items():
                if rx.search(path):
                    wanted.setdefault(kind, u)
    extra_text, extra_nodes, extra_addr = [], [], False
    for u in list(wanted.values())[:4]:
        r = fetch(u, max_bytes=400_000)
        if r.status == 200:
            _, _, ep = _head_signals(r)
            extra_text.append(ep.visible_text[:30000])
            extra_nodes.extend(_jsonld_nodes(ep))
            extra_addr = extra_addr or bool(ep.address_tags)
    saw_trust_page = bool(extra_text) or any(
        rx.search(urllib.parse.urlparse(pg["url"]).path) for pg in pages for rx in _TRUST_PATTERNS.values())
    text = " ".join([(pg.get("parser").visible_text[:30000] if pg.get("parser") else "") for pg in pages]
                    + extra_text)
    nodes = [n for pg in pages if pg.get("parser") for n in _jsonld_nodes(pg["parser"])] + extra_nodes
    has_postal = (any(isinstance(n.get("address"), (dict, str)) and n.get("address")
                      for n in nodes if _types(n) & {"Organization", "Corporation", "NGO"})
                  or extra_addr
                  or any(pg.get("parser") and pg["parser"].address_tags for pg in pages)
                  or bool(_STREET_RE.search(text))
                  or bool(re.search(r"\bP\.?\s?O\.?\s+Box\s+\d+", text, re.I)))
    legal = bool(re.search(r"\b(LLC|L\.L\.C\.|Inc\.?|Ltd\.?|GmbH|Corp\.?|PLC|Pty|S\.A\.|B\.V\.)\b", text))
    findings = []
    if not has_postal and saw_trust_page:
        findings.append(f("LOW", "No postal address found anywhere on the crawled pages", "trust", 2, 1,
                          "no PostalAddress in Organization schema, no <address>, no street-address text "
                          "on the crawled pages or the about/contact/legal pages",
                          "Publish a real mailing address (footer, contact or about page, and Organization "
                          "schema). An organization reachable only by email is a classic low-trust pattern "
                          "for quality raters and for assistants asked 'is this company legitimate?'."))
    return findings, ("Entity transparency",
                      f"postal address {'✓' if has_postal else '✗'} · legal entity named {'✓' if legal else '✗'}")


def check_llms_txt(base, llms_resp):
    """Validate llms.txt against the llmstxt.org shape and test the URLs it advertises.
    Everything here is LOW/INFO: no answer engine has confirmed it reads the file."""
    text = llms_resp.text
    lines = [l for l in text.splitlines() if l.strip()]
    findings, notes = [], []
    if not lines or not lines[0].startswith("# "):
        notes.append("first line is not an H1 title")
    if not any(l.startswith("> ") for l in lines[:6]):
        notes.append("no blockquote summary")
    links = re.findall(r"\[[^\]]+\]\((https?://[^)\s]+)\)", text)
    if not links:
        notes.append("no markdown links")
    ctype = llms_resp.header("content-type", "").lower()
    if "text/plain" not in ctype and "markdown" not in ctype:
        notes.append(f"served as {ctype or 'unknown type'}")
    bad, noncanon = [], []
    own = [u for u in dict.fromkeys(links) if same_site(u, base)][:12]
    for u in own:
        r = fetch(u, max_bytes=200_000)
        if r.status >= 400 or r.status == 0:
            bad.append(f"{u} ({r.status})")
        elif r.chain and not _trivial_redirect(u, r.final_url):
            noncanon.append(u + " (redirects)")
        elif "html" in r.header("content-type", "").lower():
            can, noindex, _ = _head_signals(r)
            if noindex:
                noncanon.append(u + " (noindex)")
            elif can and can.rstrip("/") != r.final_url.rstrip("/"):
                noncanon.append(u + " (canonical elsewhere)")
    if bad:
        findings.append(f("LOW", "llms.txt lists URLs that don't resolve", "ai search", 2, 1,
                          trunc("; ".join(bad[:4]), 200), "Fix or remove the dead links."))
    if noncanon:
        findings.append(f("LOW", "llms.txt points AI agents at non-canonical URLs", "ai search", 2, 1,
                          trunc("; ".join(noncanon[:4]), 200),
                          "List the same canonical URLs you want search engines to index, so citations "
                          "and search results agree on one address per page."))
    if notes:
        findings.append(f("INFO", "llms.txt deviates from the llmstxt.org format", "ai search", 1, 1,
                          "; ".join(notes), "Optional: H1 title, '>' summary, then H2 sections of markdown links."))
    full = fetch(normalize(base, "/llms-full.txt"), max_bytes=2048)
    return findings, ("llms.txt", f"found · {len(links)} links"
                      + (" · llms-full.txt present" if full.status == 200 and b"<html" not in full.body[:500].lower() else ""))


def check_markdown_negotiation(home_url):
    r = fetch(home_url, max_bytes=4096, extra_headers={"Accept": "text/markdown, text/html;q=0.5"})
    ok = r.status == 200 and "markdown" in r.header("content-type", "").lower()
    return ("Markdown for agents", "serves text/markdown on request" if ok
            else "HTML only (content negotiation for text/markdown not offered — optional)")


# =================================================================================
# Share images across templates, duplicate passages, near-duplicate pages
# =================================================================================
def check_page_share_image(page, home_url):
    """The homepage share image is checked in depth elsewhere; here we make sure every
    OTHER audited template has one that loads and isn't just a logo."""
    p = page.get("parser")
    if not p or page.get("status") != 200 or page["url"].rstrip("/") == home_url.rstrip("/"):
        return []
    og = p.og("og:image") or p.twitter("twitter:image")
    if not og:
        return [f("LOW", "Page has no share image of its own", "social", 2, 2, "og:image missing",
                  "Give each template an og:image (1200×630). Links shared from inner pages otherwise unfurl bare.")]
    iu = normalize(page["url"], og)
    r = fetch(iu, max_bytes=3_000_000, ua=BROWSER_UA)      # GET, never HEAD — see check_head_parity
    if r.status != 200:
        return [f("MEDIUM", "Share image does not load", "social", 3, 1, f"{trunc(iu, 140)} → HTTP {r.status or r.error}",
                  "Fix the og:image URL. A broken share image unfurls as a blank card everywhere the link is posted.")]
    info = image_info(r)
    if info and info["width"] and info["height"]:
        w, h = info["width"], info["height"]
        if w < 600 or abs((w / h) - 1.91) / 1.91 > 0.35:
            return [f("LOW", "Share image is small or not landscape", "social", 2, 2,
                      f"{w}×{h} · {trunc(iu, 100)}",
                      "Use a 1200×630 landscape image. Logos and square images are cropped or shrunk to a "
                      "thumbnail card on most platforms.")]
    return []


def _sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 12]


def check_repeated_passages(page):
    p = page.get("parser")
    if not p or p.word_count < 150:
        return []
    seen, dup = set(), []
    for s in _sentences(p.visible_text[:40000]):
        k = s.lower()
        if k in seen and s not in dup:
            dup.append(s)
        seen.add(k)
    if dup:
        return [f("LOW", "The same sentence appears more than once on the page", "on page", 1, 1,
                  trunc(dup[0], 140) + (f" (+{len(dup) - 1} more)" if len(dup) > 1 else ""),
                  "Remove the repeated block — usually a template partial rendered twice.")]
    return []


def _shingles(text, k=6):
    w = re.findall(r"[a-z0-9']+", text.lower())
    return {" ".join(w[i:i + k]) for i in range(0, max(0, len(w) - k + 1))}


def check_template_duplication(pages):
    """Within each template: how much of a page is unique, and are any two pages
    near-copies? Uses 6-word shingles over visible text."""
    findings, rows = [], []
    by_t = {}
    for pg in pages:
        p = pg.get("parser")
        if p and pg.get("status") == 200 and p.word_count >= 80:
            by_t.setdefault(url_template(pg["url"]), []).append((pg["url"], _shingles(p.visible_text[:60000]), p.word_count))
    for tmpl, items in by_t.items():
        if len(items) < 3:
            continue
        counts = {}
        for _, sh, _ in items:
            for s in sh:
                counts[s] = counts.get(s, 0) + 1
        common = {s for s, c in counts.items() if c >= max(2, int(len(items) * 0.8))}
        uniq_words = sorted(int(wc * (1 - len(sh & common) / max(1, len(sh)))) for _, sh, wc in items)
        med = uniq_words[len(uniq_words) // 2]
        near = 0
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i][1], items[j][1]
                if a and b and len(a & b) / len(a | b) >= 0.8:
                    near += 1
        rows.append({"template": tmpl, "pages": len(items), "median_unique_words": med, "near_duplicate_pairs": near})
        if near:
            findings.append(f("MEDIUM", "Near-duplicate pages within a template", "on page", 3, 3,
                              f"{tmpl}: {near} page pair(s) share ≥80% of their text",
                              "Differentiate, consolidate with a 301/canonical, or noindex the copies. "
                              "Mass-produced near-identical pages are what Google's scaled-content and "
                              "doorway policies target."))
        elif med < 120:
            findings.append(f("LOW", "Template pages are mostly boilerplate", "on page", 3, 3,
                              f"{tmpl}: median ≈{med} unique words per page across {len(items)} sampled",
                              "Add page-specific substance (facts, evidence, local detail). When most of a "
                              "page is shared template text, engines have little reason to index each one."))
    return findings, rows


# =================================================================================
# Social preview
# =================================================================================
def analyze_social(home_page):
    """Build the social-share-preview block from the homepage parser + fetched assets."""
    p = home_page.get("parser")
    r = home_page.get("response")
    out = {"metrics": [], "findings": []}
    if not p:
        return out

    og_image = p.og("og:image") or p.twitter("twitter:image")
    title = p.og("og:title") or p.title
    desc = p.og("og:description") or p.meta_description
    tw_card = p.twitter("twitter:card")

    metrics = out["metrics"]
    if og_image:
        img_url = normalize(home_page["url"], og_image)
        info = image_info(fetch(img_url))
        if info:
            if info["width"] and info["height"]:
                metrics.append(("Image dimensions", f"{info['width']}x{info['height']}"))
                ratio = info["width"] / info["height"] if info["height"] else 0
                if ratio and abs(ratio - 1.91) / 1.91 > 0.15:
                    out["findings"].append(f(
                        "MEDIUM", f"Aspect ratio {ratio:.2f}:1 differs from the 1.91:1 social standard",
                        "social", 3, 2,
                        f"{info['width']}x{info['height']} ({ratio:.2f}:1)",
                        "Use a 1200x630 (1.91:1) image so it isn't cropped on Facebook/LinkedIn."))
            metrics.append(("Image size", f"{info['size'] // 1024} KB"))
            metrics.append(("Image type", info["mime"] or "unknown"))
            if info["size"] > 1024 * 1024:
                out["findings"].append(f("LOW", "Social image over 1 MB", "social", 2, 1,
                                         f"{info['size'] // 1024} KB",
                                         "Compress the share image; large files slow link unfurling."))
    else:
        out["findings"].append(f("MEDIUM", "No Open Graph image", "social", 3, 2, "og:image missing",
                                 "Add og:image (1200x630) so shared links show a rich preview."))

    if title:
        metrics.append(("Title length", f"{len(title)} chars"))
    if desc:
        metrics.append(("Description length", f"{len(desc)} chars"))
    metrics.append(("twitter:card", tw_card or "missing"))
    if not tw_card:
        out["findings"].append(f("LOW", "Missing twitter:card", "social", 2, 1, "no twitter:card",
                                 "Add <meta name=\"twitter:card\" content=\"summary_large_image\">."))

    if not p.og("og:title"):
        out["findings"].append(f("LOW", "Missing Open Graph title", "social", 2, 1, "og:title missing",
                                 "Add og:title for control over how shared links read."))
    elif og_image:
        # they've adopted OG — check the set is complete
        missing_og = [t for t in ("og:description", "og:url", "og:type", "og:site_name")
                      if not p.og(t)]
        if missing_og:
            out["findings"].append(f("LOW", "Open Graph tags incomplete", "social", 2, 1,
                                     "missing " + ", ".join(missing_og),
                                     "Add the missing OG tags so shared links render consistently on "
                                     "every platform (and messaging apps)."))

    # favicon
    fav_href = p.favicon_href or "/favicon.ico"
    fav_url = normalize(home_page["url"], fav_href)
    fav = image_info(fetch(fav_url))
    if fav:
        if fav["width"] and fav["height"]:
            metrics.append(("Favicon dimensions", f"{fav['width']}x{fav['height']}"))
        metrics.append(("Favicon size", f"{fav['size'] / 1024:.2f} KB" if fav['size'] < 1024*1024
                        else f"{fav['size'] / (1024*1024):.2f} MB"))
        metrics.append(("Favicon type", fav["mime"] or "unknown"))
        if fav["size"] > 100 * 1024:
            out["findings"].append(f("LOW", f"Favicon is {fav['size'] // 1024} KB", "social", 1, 1,
                                     f"{fav['size'] // 1024} KB",
                                     "Large favicons slow first paint; keep under 100 KB."))

    if (title and not has_cta(title)) and (desc and not has_cta(desc)):
        out["findings"].append(f("LOW", "No call-to-action verb in title or description", "social", 1, 1,
                                 "No CTA verb (Book, Try, Get, Shop, Learn…)",
                                 "Add an action verb to the social title/description to lift click-through."))
    return out


# =================================================================================
# PageSpeed Insights (best effort)
# =================================================================================
def pagespeed(url, enabled):
    if not enabled:
        return {"status": "skipped", "findings": []}
    api = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
           "?strategy=mobile&category=performance&category=seo"
           "&category=accessibility&category=best-practices"
           "&url=" + urllib.parse.quote(url, safe=""))
    key = os.environ.get("PAGESPEED_API_KEY")
    if key:
        api += "&key=" + urllib.parse.quote(key)
    r = fetch(api)
    if r.status == 429:
        return {"status": "rate-limited", "findings": [
            f("INFO", "PageSpeed Insights rate-limited", "performance", 1, 1, "PSI 429",
              "Get a free key at developers.google.com/speed and set PAGESPEED_API_KEY to avoid limits.")]}
    if r.status >= 400 or r.error:
        return {"status": "unavailable", "findings": [
            f("INFO", "PageSpeed Insights unavailable", "performance", 1, 1,
              f"PSI {r.status or r.error}", "Re-run later or add a PAGESPEED_API_KEY.")]}
    try:
        data = json.loads(r.text)
        lh = data["lighthouseResult"]
        score = round(lh["categories"]["performance"]["score"] * 100)
        audits = lh["audits"]
        # INP replaced FID as a Core Web Vital in March 2024 and is the most-failed
        # metric on the web — surface it alongside LCP/CLS.
        metrics = {
            "Performance score": str(score),
            "LCP (lab)": audits.get("largest-contentful-paint", {}).get("displayValue", "—"),
            "INP (lab)": audits.get("interaction-to-next-paint", {}).get("displayValue", "—"),
            "CLS (lab)": audits.get("cumulative-layout-shift", {}).get("displayValue", "—"),
            "TBT": audits.get("total-blocking-time", {}).get("displayValue", "—"),
            "FCP": audits.get("first-contentful-paint", {}).get("displayValue", "—"),
            "Speed Index": audits.get("speed-index", {}).get("displayValue", "—"),
        }
        # Lighthouse also grades SEO, accessibility, and best practices when asked —
        # free second opinions from a real rendering browser (it executes JS, we don't).
        for ckey, label in (("seo", "Lighthouse SEO"), ("accessibility", "Accessibility"),
                            ("best-practices", "Best practices")):
            c = lh["categories"].get(ckey)
            if c and c.get("score") is not None:
                metrics[label] = f"{round(c['score'] * 100)}/100"
        findings = []

        # Curated Lighthouse audit failures we can't measure without a browser —
        # mobile legibility/tap targets and crawlability of JS-driven links.
        LH_CHECKS = [
            ("font-size", "MEDIUM", "Text too small to read on mobile", "mobile", 3, 2,
             "Use a base font size of ≥16px so mobile users don't have to zoom."),
            ("tap-targets", "MEDIUM", "Tap targets too small or too close together", "mobile", 3, 2,
             "Make buttons and links at least 48x48px with spacing so they're easily tappable."),
            ("crawlable-anchors", "LOW", "Links not crawlable (no proper href)", "crawlability", 2, 2,
             'Use <a href="…"> for navigation instead of JavaScript click handlers so crawlers '
             "can follow the links."),
            ("canonical", "MEDIUM", "Invalid rel=canonical (Lighthouse)", "crawlability", 3, 1,
             "Fix the canonical URL Lighthouse flagged as invalid."),
            ("hreflang", "LOW", "Invalid hreflang (Lighthouse)", "crawlability", 2, 1,
             "Fix the hreflang values/links Lighthouse flagged as invalid."),
        ]
        for aid, sev, title, cat, imp, eff, fix in LH_CHECKS:
            a = audits.get(aid)
            if a and a.get("score") == 0:
                findings.append(f(sev, title, cat, imp, eff,
                                  a.get("displayValue") or "failed Lighthouse audit "
                                  + repr(aid), fix))

        # Top load-time opportunities, as INFO: the perf score already caps the category,
        # so these add the "what to actually do" without double-counting.
        opps = []
        for a in audits.values():
            det = a.get("details") or {}
            if isinstance(det, dict) and det.get("type") == "opportunity":
                ms = det.get("overallSavingsMs") or 0
                if ms >= 300 and (a.get("score") is None or a.get("score") < 0.9):
                    opps.append((ms, a.get("title", "opportunity")))
        for ms, title in sorted(opps, reverse=True)[:3]:
            findings.append(f("INFO", f"Speed opportunity: {title}", "performance", 3, 3,
                              f"~{ms / 1000:.1f} s potential saving (Lighthouse estimate)",
                              "This is one of the levers behind the performance score above — details "
                              "in PageSpeed Insights."))

        # Real-user field data (CrUX) — this is what Google actually uses to assess
        # page experience, measured at the 75th percentile. Present only for sites with
        # enough traffic; absent for low-traffic pages (we say so rather than guess).
        field = (data.get("loadingExperience") or {}).get("metrics") or {}
        FIELD_SPECS = [  # (CrUX key, display label, divisor, unit, decimals)
            ("LARGEST_CONTENTFUL_PAINT_MS", "LCP", 1000.0, "s", 2),
            ("INTERACTION_TO_NEXT_PAINT", "INP", 1.0, "ms", 0),
            ("CUMULATIVE_LAYOUT_SHIFT_SCORE", "CLS", 100.0, "", 2),
        ]
        CAT_WORD = {"FAST": "good", "AVERAGE": "needs improvement", "SLOW": "poor"}
        for key, nice, div, unit, dec in FIELD_SPECS:
            m = field.get(key) or {}
            pct, cat = m.get("percentile"), m.get("category", "")
            if pct is None:
                continue
            disp = f"{pct / div:.{dec}f}{unit}"
            metrics[f"{nice} (field)"] = f"{disp} · {CAT_WORD.get(cat, cat.lower())}"
            if cat == "SLOW":
                findings.append(f("HIGH", f"{nice} fails Core Web Vitals (real users)", "performance", 4, 4,
                                  f"{nice} {disp} at the 75th percentile (CrUX field data)", cwv_fix(nice)))
            elif cat == "AVERAGE":
                findings.append(f("MEDIUM", f"{nice} needs improvement (real users)", "performance", 3, 3,
                                  f"{nice} {disp} at the 75th percentile (CrUX field data)", cwv_fix(nice)))
        if not field:
            metrics["Field data"] = "no CrUX real-user data (low traffic)"

        if score < 50:
            findings.append(f("HIGH", "Poor PageSpeed performance score", "performance", 4, 4,
                              f"score {score}/100", "Optimize images, defer JS, and reduce main-thread work."))
        elif score < 90:
            findings.append(f("MEDIUM", "PageSpeed performance below 90", "performance", 3, 3,
                              f"score {score}/100", "Address Lighthouse opportunities to reach the green band."))
        return {"status": "ok", "score": score, "metrics": metrics, "findings": findings}
    except (KeyError, json.JSONDecodeError):
        return {"status": "unavailable", "findings": [
            f("INFO", "PageSpeed Insights unavailable", "performance", 1, 1, "unparseable PSI response",
              "Re-run later.")]}


# =================================================================================
# Scoring
# =================================================================================
def score_categories(all_findings, psi, robots_info):
    scores = {}
    # Deduplicate by (category, title) before scoring. A single site-wide problem (e.g.
    # missing security headers, client-rendering, a shared duplicate title) surfaces on
    # every crawled page; without this it would be deducted once per page, making the
    # score swing with --max-pages. Counting each distinct issue type once keeps the
    # score stable and comparable across runs. The per-page tables still show every
    # instance, so nothing is hidden — only the double-counting is removed.
    seen = set()
    unique = []
    for fi in all_findings:
        key = (fi["category"], fi["title"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(fi)
    for cat in CATEGORIES:
        # Diminishing returns: the two heaviest findings in a category count in full, the
        # next two at 75%, the rest at 50%. With ~130 checks a category would otherwise hit
        # the floor on a pile of LOWs and stop distinguishing a bad site from a terrible one.
        weights = sorted((SEVERITY_WEIGHT[fi["severity"]] for fi in unique if fi["category"] == cat),
                         reverse=True)
        deduction = sum(w * (1.0 if i < 2 else 0.75 if i < 4 else 0.5) for i, w in enumerate(weights))
        scores[cat] = max(0, round(100 - deduction))
    # Performance: the base score already reflects observable signals from the HTML and
    # headers (compression, render-blocking JS, page weight, DOM size, image dimensions,
    # response time) — those are things we DID observe, so they always count. When
    # PageSpeed also returns a measured score, additionally cap the category by it. When
    # PSI is unavailable we simply don't apply that extra cap (we no longer reset to 100,
    # so the observable findings are no longer silently discarded).
    if psi.get("status") == "ok":
        scores["performance"] = min(scores["performance"], psi["score"])
    overall = round(sum(scores[c] * CATEGORY_WEIGHTS[c] for c in CATEGORIES))
    return scores, overall


def confidence_level(psi, pages, ai_blocked):
    if not pages or all(p["status"] == 0 for p in pages):
        return "low"
    if psi.get("status") != "ok":
        return "medium"
    return "high"


# =================================================================================
# Orchestration
# =================================================================================
def run_audit(start_url, max_pages=15, use_pagespeed=False, sweep=100, probe_ai=True,
              network_checks=True):
    if not start_url.startswith("http"):
        start_url = "https://" + start_url
    parsed = urllib.parse.urlparse(start_url)
    base = f"{parsed.scheme}://{parsed.netloc}/"
    domain = parsed.hostname or start_url

    # robots + sitemap
    robots_resp = fetch(normalize(base, "/robots.txt"))
    robots_found = robots_resp.status == 200 and robots_resp.body
    robots_info = parse_robots(robots_resp.text) if robots_found else {"sitemaps": [], "ai_blocked": [], "blocks_all": False}

    sm_files = list(robots_info["sitemaps"])
    sitemap_exists = bool(sm_files)
    sm_meta = read_sitemaps(sm_files) if sm_files else None
    if not sm_meta or not sm_meta["urls"]:
        default_sm = normalize(base, "/sitemap.xml")
        r_sm = fetch(default_sm, max_bytes=200_000)
        head = r_sm.body[:4096].lower()
        if r_sm.status == 200 and (b"<urlset" in head or b"<sitemapindex" in head):
            sitemap_exists = True
            sm_files = [default_sm]
            sm_meta = read_sitemaps(sm_files)
    sm_urls = sm_meta["urls"] if sm_meta else []
    sitemap_found = sitemap_exists and bool(sm_urls)

    llms_resp = fetch(normalize(base, "/llms.txt"))
    llms_found = (llms_resp.status == 200 and bool(llms_resp.body)
                  and b"<html" not in llms_resp.body[:2000].lower())

    page_specs = discover_pages(base, max_pages, sm_urls)
    pages_urls = [u for u, _ in page_specs]

    site_findings = []
    if robots_resp.status >= 500:
        site_findings.append(f("CRITICAL", "robots.txt returns a server error", "crawlability", 5, 2,
                               f"GET /robots.txt → HTTP {robots_resp.status}",
                               "Google treats a robots.txt that answers 5xx as 'disallow everything' and "
                               "stops crawling the site until it recovers. Serve 200 (or 404 if you have none)."))
    elif not robots_found:
        site_findings.append(f("MEDIUM", "robots.txt not found", "crawlability", 3, 1, "/robots.txt missing",
                               "Add a robots.txt that allows crawling and points to your sitemap."))
    elif sitemap_exists and not robots_info["sitemaps"]:
        site_findings.append(f("LOW", "Sitemap not referenced in robots.txt", "crawlability", 2, 1,
                               "robots.txt exists but has no Sitemap: line",
                               "Add 'Sitemap: <absolute sitemap URL>' to robots.txt so every crawler "
                               "finds it without guessing."))
    if not sitemap_exists:
        site_findings.append(f("MEDIUM", "XML sitemap not found", "crawlability", 3, 2, "no sitemap.xml",
                               "Publish an XML sitemap and reference it from robots.txt."))
    elif not sm_urls:
        site_findings.append(f("MEDIUM", "Sitemap contains no URLs", "crawlability", 3, 2,
                               "A sitemap exists but no <loc> entries could be read from it",
                               "Fix or regenerate the sitemap — an empty one gives crawlers nothing."))
    if robots_info.get("blocks_all"):
        site_findings.append(f("CRITICAL", "robots.txt blocks all crawlers", "crawlability", 5, 1,
                               "User-agent: * Disallow: /",
                               "Remove the site-wide Disallow so search engines can index the site."))
    if robots_info.get("ai_blocked"):
        agents = sorted(set(robots_info["ai_blocked"]))
        site_findings.append(f("LOW", "robots.txt blocks AI crawlers", "ai search", 2, 1,
                               ", ".join(agents) + " disallowed",
                               "Allow these crawlers if you want this content cited in AI answers "
                               "(ChatGPT, Claude, Perplexity…); keep the block only if it's deliberate policy."))
    if not llms_found:
        site_findings.append(f("INFO", "No llms.txt file", "ai search", 1, 1, "/llms.txt not found",
                               "Optional/emerging: an llms.txt at the site root offers AI agents a curated "
                               "content index. No major answer engine has confirmed it reads the file, so "
                               "treat it as low priority — crawler access, server-rendered content and "
                               "clear entity information matter far more."))
    if sitemap_found:
        site_findings.extend(sitemap_meta_findings(sm_meta))

    # ---- deep pages (full per-page tables) ----
    workers = min(8, max(1, len(pages_urls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        pages = [pg for pg, _ in ex.map(analyze_page, pages_urls)]
    for pg, (_, src) in zip(pages, page_specs):
        pg["source"] = src

    # ---- sweep: a wider stratified sample of the sitemap, analyzed the same way but
    # reported in aggregate. This is what catches a problem that lives on the 250-page
    # template rather than on the dozen pages that get a full table. ----
    sweep_urls = stratified_sample([u for u in sm_urls if same_site(base, u)], max(0, sweep),
                                   exclude=pages_urls) if sweep else []
    sweep_pages = []
    if sweep_urls:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            sweep_pages = [pg for pg, _ in ex.map(analyze_page, sweep_urls)]
        for pg in sweep_pages:
            pg["source"] = "sitemap"
    all_pages = pages + sweep_pages

    # Sitemap hygiene, judged on every sitemap URL we fetched.
    from_sm = [p for p in all_pages if p.get("source") == "sitemap"]
    sm_redirects = [p["url"] for p in from_sm if p.get("chain_hops")]
    if len(sm_redirects) >= 2:
        site_findings.append(f("LOW", f"Sitemap lists redirecting URLs ({len(sm_redirects)})",
                               "crawlability", 2, 1, trunc("; ".join(sm_redirects[:4]), 200),
                               "Regenerate the sitemap with final canonical URLs — redirecting entries "
                               "waste crawl budget."))
    sm_dead = [p["url"] for p in from_sm if p.get("status", 0) in (404, 410) or p.get("status", 0) >= 500]
    if sm_dead:
        site_findings.append(f("HIGH" if len(sm_dead) >= max(2, len(from_sm) * 0.1) else "MEDIUM",
                               f"Sitemap lists URLs that return errors ({len(sm_dead)} of {len(from_sm)} checked)",
                               "crawlability", 4, 1, trunc("; ".join(sm_dead[:4]), 200),
                               "Remove dead URLs from the sitemap or restore the pages."))
    sm_noindex = [p["url"] for p in from_sm if "noindex" in (p.get("robots") or "")]
    if sm_noindex:
        site_findings.append(f("MEDIUM", f"Sitemap lists noindexed URLs ({len(sm_noindex)} of {len(from_sm)} checked)",
                               "crawlability", 3, 1, trunc("; ".join(sm_noindex[:4]), 200),
                               "A sitemap should list only indexable URLs. Drop these, or remove the noindex."))
    sm_noncanon = []
    for p in from_sm:
        can = normalize(p.get("final_url") or p["url"], p.get("canonical") or "") if p.get("canonical") else ""
        if can and same_site(can, p["url"]) and can.rstrip("/") != (p.get("final_url") or p["url"]).rstrip("/"):
            sm_noncanon.append(p["url"])
    if len(sm_noncanon) >= 2:
        site_findings.append(f("MEDIUM", f"Sitemap lists non-canonical URLs ({len(sm_noncanon)} of {len(from_sm)} checked)",
                               "crawlability", 3, 1, trunc("; ".join(sm_noncanon[:4]), 200),
                               "List the canonical URL of each page only; a sitemap and a canonical tag "
                               "that disagree send search engines conflicting signals."))

    fell_back = [p["url"] for p in all_pages if p.get("ua_fallback")]
    still_blocked = [p["url"] for p in all_pages if p.get("status") in (401, 403, 406, 429)]
    if fell_back or still_blocked:
        site_findings.append(f(
            "INFO", "Site refuses non-browser user-agents", "crawlability", 2, 2,
            f"{len(fell_back)} page(s) were only readable with a browser user-agent; "
            f"{len(still_blocked)} stayed blocked (401/403/429)",
            "Bot protection is refusing automated clients. Make sure it admits verified search "
            "and AI crawlers. Pages that stayed blocked could not be audited, so treat the score "
            "as partial."))

    # cross-page duplicates — across deep AND swept pages
    descriptions, titles = {}, {}
    for page in all_pages:
        if page.get("description"):
            descriptions.setdefault(page["description"], []).append(page["url"])
        if page.get("title"):
            titles.setdefault(page["title"], []).append(page["url"])
    for desc, urls in descriptions.items():
        if len(urls) > 1:
            for page in all_pages:
                if page.get("description") == desc:
                    page["findings"].append(f("MEDIUM", "Duplicate meta description across pages",
                                              "on page", 3, 2, f"Same description on {len(urls)} pages",
                                              "Write a page-specific description reflecting this page's unique content."))
    for title, urls in titles.items():
        if len(urls) > 1:
            for page in all_pages:
                if page.get("title") == title:
                    page["findings"].append(f("MEDIUM", "Duplicate title across pages",
                                              "on page", 3, 2, f"Same title on {len(urls)} pages",
                                              "Give each page a distinct, descriptive title."))

    if pages:
        home_types = set(pages[0].get("schema_types", []))
        if not ({"Organization", "WebSite", "LocalBusiness"} & home_types):
            pages[0]["findings"].append(f(
                "LOW", "No Organization/WebSite schema on homepage", "schema", 3, 2,
                "Neither Organization nor WebSite JSON-LD found on the homepage",
                "Add Organization (with logo and sameAs links to your social/profile URLs) and WebSite "
                "JSON-LD so search and AI engines can identify and trust the entity behind the site."))

    any_rendered = any(p.get("word_count", 0) > 0 for p in pages)
    if (any_rendered and len(pages) >= 3
            and not any("BreadcrumbList" in (p.get("schema_types") or []) for p in all_pages)):
        site_findings.append(f("LOW", "No BreadcrumbList schema on any crawled page", "schema", 2, 2,
                               f"0 of {len(all_pages)} pages declare breadcrumb structured data",
                               "Add BreadcrumbList JSON-LD (and visible breadcrumbs) so search results "
                               "show your site hierarchy instead of a raw URL."))

    # ---- per-page deep checks that need extra requests: run on every deep page, but
    # image/byte checks only on the first page of each template to bound traffic ----
    home_url = pages[0]["url"] if pages else base
    readiness = []
    seen_templates = set()
    for i, page in enumerate(pages):
        extra = []
        extra += check_csp_blocks(page) + check_head_order(page) + check_html_caching(page)
        extra += check_schema_depth(page, is_home=(i == 0)) + check_repeated_passages(page)
        ar_f, ar_row = check_answer_readiness(page)
        extra += ar_f
        if ar_row:
            readiness.append(ar_row)
        t = url_template(page["url"])
        if t not in seen_templates or i == 0:
            seen_templates.add(t)
            extra += check_hero_and_thumbs(page) + check_page_share_image(page, home_url)
        page["findings"].extend(extra)
    for page in sweep_pages:   # cheap, no extra requests
        page["findings"].extend(check_schema_depth(page, False) + check_repeated_passages(page)
                                + check_answer_readiness(page)[0])

    # ---- site-wide probes ----
    health = []
    hv_f, hv_h = check_host_variants(base, pages[0]["final_url"] if pages else base)
    site_findings.extend(hv_f)
    health.extend(hv_h)
    n4_f, n4_h = check_custom_404(base)
    site_findings.extend(n4_f)
    health.append(n4_h)
    tls_f, tls_h = tls_certificate(parsed.hostname or domain)
    site_findings.extend(tls_f)
    health.append(tls_h)
    tr_f, tr_h, tr_sig = check_trust_pages(all_pages)
    site_findings.extend(tr_f)
    health.append(tr_h)
    crawled = {p["url"] for p in all_pages} | {p["final_url"] for p in all_pages if p.get("final_url")}
    la_f, la_h, link_summary = check_link_architecture(all_pages, crawled, sm_urls)
    site_findings.extend(la_f)
    health.extend(la_h)
    dup_f, dup_rows = check_template_duplication(all_pages)
    site_findings.extend(dup_f)
    et_f, et_h = check_entity_transparency(all_pages, pages[0] if pages else None)
    site_findings.extend(et_f)
    health.append(et_h)

    ai_matrix = []
    if pages:
        site_findings.extend(check_asset_caching(pages[0]))
        site_findings.extend(check_fonts(pages[0]))
        an_f, an_h = detect_analytics(pages[0])
        site_findings.extend(an_f)
        health.append(an_h)
        hp = pages[0].get("parser")
        og0 = normalize(home_url, hp.og("og:image")) if hp and hp.og("og:image") else ""
        by_prefix = {}       # one same-site image per top-level directory (/assets, /media, …):
        for pg in all_pages:  # static files and app-routed media often behave differently
            pr = pg.get("parser")
            for im in (pr.imgs if pr else []):
                u = normalize(pg["url"], im["src"]) if im["src"] and not im["src"].startswith("data:") else ""
                if u and same_site(u, base):
                    by_prefix.setdefault(urllib.parse.urlparse(u).path.split("/")[1], u)
        site_findings.extend(check_head_parity(
            pages[0], [og0] + list(by_prefix.values())[:4] + [sm_files[0] if sm_files else ""]))
        hsts = check_hsts_quality(pages[0])
        if hsts:
            health.append(hsts)
        if pages[0].get("status") == 200:
            health.append(check_markdown_negotiation(home_url))

    if robots_found:
        rp_f, rp_h = robots_platform_signals(robots_resp.text)
        site_findings.extend(rp_f)
        health.append(rp_h)
    if llms_found:
        ll_f, ll_h = check_llms_txt(base, llms_resp)
        site_findings.extend(ll_f)
        health.append(ll_h)
    else:
        health.append(("llms.txt", "not found (optional)"))
    health.append(("AI crawler robots.txt rules",
                   ("blocked: " + ", ".join(sorted(set(robots_info["ai_blocked"]))))
                   if robots_info.get("ai_blocked") else "no AI crawlers disallowed"))
    if probe_ai and pages and pages[0].get("status") == 200:
        ai_f, ai_h, ai_matrix = probe_ai_agents(home_url, robots_resp.text if robots_found else "")
        site_findings.extend(ai_f)
        health.extend(ai_h)

    site_findings.extend(check_exposed_files(base))
    st_f, st_h, st_text = check_security_txt(base)
    site_findings.extend(st_f)
    health.append(st_h)
    if network_checks:      # third-party lookups: DNS-over-HTTPS and RDAP
        cd_f, mail_domains = check_contact_domains(all_pages, parsed.hostname or domain, st_text)
        site_findings.extend(cd_f)
        ea_f, ea_h = check_email_auth(parsed.hostname or domain, mail_domains)
        site_findings.extend(ea_f)
        if ea_h:
            health.append(ea_h)
        de_f, de_h = check_domain_expiry(parsed.hostname or domain)
        site_findings.extend(de_f)
        health.append(de_h)

    verif = []
    if pages and pages[0].get("parser"):
        hp = pages[0]["parser"]
        verif = [n for n, k in (("Google", "google-site-verification"), ("Bing", "msvalidate.01"),
                                ("Yandex", "yandex-verification")) if hp.meta_content("name", k)]
    health.append(("Webmaster verification tags",
                   ", ".join(verif) if verif else "none in the homepage HTML (DNS/file verification not visible here)"))

    psi = pagespeed(pages[0]["final_url"] if pages else base, use_pagespeed)
    social = analyze_social(pages[0]) if pages else {"metrics": [], "findings": []}
    keywords = extract_keywords(pages)
    footprint = extract_footprint(all_pages)

    local = (footprint.get("google") or {}).get("local")
    has_local_signals = (tr_sig.get("has_tel")
                         or (footprint.get("google") or {}).get("gbp_links")
                         or (footprint.get("google") or {}).get("maps_links"))
    if local:
        missing_local = [lbl for k, lbl in (("telephone", "telephone"), ("address", "address"),
                                            ("geo", "geo coordinates"), ("hours", "opening hours"))
                         if not local.get(k)]
        if missing_local:
            site_findings.append(f("LOW", "LocalBusiness schema incomplete", "schema", 2, 1,
                                   "missing: " + ", ".join(missing_local),
                                   "Fill in telephone, full address, geo, and openingHours in the "
                                   "LocalBusiness JSON-LD — complete data feeds the map pack and AI answers."))
    elif has_local_signals:
        site_findings.append(f("MEDIUM", "No LocalBusiness schema for a local business", "schema", 3, 2,
                               "Site shows local signals (phone / Google Maps links) but no "
                               "LocalBusiness JSON-LD",
                               "Add LocalBusiness (or a specific subtype like TouristAttraction or "
                               "Restaurant) with name, address, telephone, geo, openingHours, and sameAs — "
                               "the backbone of local SEO."))

    # ---- aggregate the sweep: one site-wide finding per issue type, with counts ----
    deep_keys = {(fi["category"], fi["title"]) for p in pages for fi in p["findings"]}
    agg = {}
    for sp in sweep_pages:
        for fi in sp["findings"]:
            if fi["severity"] == "INFO":
                continue
            agg.setdefault((fi["category"], fi["title"]), {"f": fi, "urls": []})["urls"].append(sp["url"])
    sweep_summary = []
    for (cat, title), v in sorted(agg.items(), key=lambda kv: (SEVERITY_ORDER[kv[1]["f"]["severity"]], -len(kv[1]["urls"]))):
        n_aff = len(v["urls"])
        tmpls = sorted({url_template(u) for u in v["urls"]})
        sweep_summary.append({"severity": v["f"]["severity"], "title": title, "category": cat,
                              "count": n_aff, "templates": tmpls, "example": v["urls"][0]})
        if (cat, title) not in deep_keys:
            fi = dict(v["f"])
            fi["observed"] = (f"{n_aff} of {len(sweep_pages)} swept sitemap pages "
                              f"(templates: {', '.join(tmpls[:3])}) · e.g. {urllib.parse.urlparse(v['urls'][0]).path} "
                              f"· {trunc(str(v['f']['observed']), 90)}")
            site_findings.append(fi)

    # ---- coverage: which templates exist, and how much of each did we look at ----
    clusters = cluster_urls([u for u in sm_urls if same_site(base, u)])
    looked = {}
    for p in all_pages:
        looked[url_template(p["url"])] = looked.get(url_template(p["url"]), 0) + 1
    coverage = [{"template": t, "urls": len(m), "audited": looked.get(t, 0)}
                for t, m in sorted(clusters.items(), key=lambda kv: -len(kv[1]))]
    unseen = [c for c in coverage if c["urls"] >= 5 and c["audited"] == 0]

    all_findings = (list(site_findings) + list(social["findings"])
                    + list(footprint["findings"]) + list(psi.get("findings", [])))
    for page in pages:
        all_findings.extend(page["findings"])

    scores, overall = score_categories(all_findings, psi, robots_info)
    conf = confidence_level(psi, pages, robots_info.get("ai_blocked"))
    if conf == "high" and unseen:
        conf = "medium"

    for page in pages:
        page["findings"].sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], -x["impact"]))
    for page in all_pages:
        page.pop("parser", None)
        page.pop("response", None)
    social["findings"].sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], -x["impact"]))
    site_findings.sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], -x["impact"]))

    return {
        "url": base, "domain": domain, "version": VERSION,
        "generated": _dt.datetime.now(),
        "page_count": len(pages),
        "sweep_count": len(sweep_pages),
        "sitemap_url_count": len(sm_urls),
        "sitemap_truncated": bool(sm_meta and sm_meta["truncated"]),
        "sitemap_files": (sm_meta or {}).get("files_read", 0),
        "confidence": conf,
        "overall": overall,
        "scores": scores,
        "robots_found": bool(robots_found),
        "sitemap_found": bool(sitemap_found),
        "health": health,
        "coverage": coverage,
        "coverage_gaps": [c["template"] for c in unseen],
        "ai_matrix": ai_matrix,
        "readiness": readiness,
        "duplication": dup_rows,
        "link_summary": link_summary,
        "sweep_summary": sweep_summary,
        "psi": psi,
        "social": social,
        "keywords": keywords,
        "footprint": footprint,
        "site_findings": site_findings,
        "pages": pages,
    }


def main():
    ap = argparse.ArgumentParser(description="Audit a website's SEO and write an HTML+PDF report.")
    ap.add_argument("url", help="Site URL, e.g. https://example.com")
    ap.add_argument("--max-pages", type=int, default=15,
                    help="Pages that get a full per-page table, sampled across URL templates (default 15)")
    ap.add_argument("--sweep", type=int, default=100,
                    help="Additional sitemap URLs analyzed and reported in aggregate (default 100, 0 = off)")
    ap.add_argument("--no-ai-probe", action="store_true",
                    help="Skip requesting the homepage with AI-crawler user-agents")
    ap.add_argument("--offline-dns", action="store_true",
                    help="Skip third-party lookups (RDAP domain expiry, DNS-over-HTTPS mail checks)")
    ap.add_argument("--version", action="version", version="seo-audit " + VERSION)
    ap.add_argument("--out", default=".", help="Output directory")
    ap.add_argument("--no-pdf", action="store_true", help="Skip PDF rendering")
    ap.add_argument("--pagespeed", action="store_true", help="Query PageSpeed Insights (best effort)")
    ap.add_argument("--json", action="store_true", help="Also write the raw report JSON")
    args = ap.parse_args()

    print(f"seo-audit {VERSION} · auditing {args.url} ({args.max_pages} deep pages + "
          f"{args.sweep} swept)…", file=sys.stderr)
    report = run_audit(args.url, args.max_pages, args.pagespeed, sweep=args.sweep,
                       probe_ai=not args.no_ai_probe, network_checks=not args.offline_dns)

    # import the renderer (sibling module)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import report as report_mod

    date = report["generated"].strftime("%Y-%m-%d")
    stem = f"seo-report-{report['domain']}-{date}"
    os.makedirs(args.out, exist_ok=True)
    html_path = os.path.join(args.out, stem + ".html")
    html = report_mod.render_html(report)
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"Wrote {html_path}", file=sys.stderr)

    if args.json:
        json_path = os.path.join(args.out, stem + ".json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, default=str, indent=2)
        print(f"Wrote {json_path}", file=sys.stderr)

    if not args.no_pdf:
        pdf_path = os.path.join(args.out, stem + ".pdf")
        ok = report_mod.html_to_pdf(html_path, pdf_path)
        if ok:
            print(f"Wrote {pdf_path}", file=sys.stderr)
        else:
            print("PDF rendering unavailable; HTML report written.", file=sys.stderr)

    print(f"\nOverall score: {report['overall']}/100  ·  {report['confidence']} confidence  ·  "
          f"{report['page_count']} deep + {report['sweep_count']} swept of "
          f"{report['sitemap_url_count']} sitemap URLs")
    if report.get("coverage_gaps"):
        print("Templates never sampled: " + ", ".join(report["coverage_gaps"]))
    print(f"HTML: {html_path}")


if __name__ == "__main__":
    main()
