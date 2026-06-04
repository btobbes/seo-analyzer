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
USER_AGENT = "Mozilla/5.0 (compatible; SEO-Audit-Skill/1.0; +https://claude.com/claude-code)"
TIMEOUT = 20
socket.setdefaulttimeout(TIMEOUT)

# ----- severity model -------------------------------------------------------------
# Points deducted from a category's 100 when a finding of this severity appears.
SEVERITY_WEIGHT = {"CRITICAL": 40, "HIGH": 20, "MEDIUM": 10, "LOW": 4, "INFO": 0}
SEVERITY_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}

# Relative importance of each category in the overall score.
CATEGORY_WEIGHTS = {
    "crawlability": 0.20,
    "on page": 0.18,
    "performance": 0.15,
    "schema": 0.10,
    "trust": 0.10,
    "social": 0.10,
    "mobile": 0.10,
    "ai search": 0.07,
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
AI_CRAWLERS = ["GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended", "CCBot"]


# =================================================================================
# Fetching
# =================================================================================
class Response:
    def __init__(self, url, final_url, status, headers, body, elapsed_ms, error=None):
        self.url = url
        self.final_url = final_url
        self.status = status
        self.headers = headers or {}
        self.body = body or b""
        self.elapsed_ms = elapsed_ms
        self.error = error

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


def fetch(url, method="GET", max_bytes=None):
    """Fetch a URL, following redirects, decoding gzip/deflate. Never raises."""
    ctx = ssl.create_default_context()
    start = _dt.datetime.now()
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=TIMEOUT) as r:
            raw = r.read(max_bytes) if max_bytes else r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if "gzip" in enc:
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
            elif "deflate" in enc:
                import zlib
                try:
                    raw = zlib.decompress(raw)
                except zlib.error:
                    try:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                    except zlib.error:
                        pass
            elapsed = (_dt.datetime.now() - start).total_seconds() * 1000
            return Response(url, r.geturl(), r.status, dict(r.headers), raw, elapsed)
    except urllib.error.HTTPError as e:
        elapsed = (_dt.datetime.now() - start).total_seconds() * 1000
        body = b""
        try:
            body = e.read()
        except Exception:
            pass
        return Response(url, url, e.code, dict(e.headers or {}), body, elapsed)
    except (urllib.error.URLError, ssl.SSLError, socket.timeout, ConnectionError,
            ValueError, OSError) as e:
        elapsed = (_dt.datetime.now() - start).total_seconds() * 1000
        return Response(url, url, 0, {}, b"", elapsed, error=str(e))


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

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in SKIP_TEXT_TAGS:
            self._skip_depth += 1
            self._open_skip.append(tag)
        self.element_count += 1
        if tag == "head":
            self._in_head = True
        elif tag == "script":
            self.scripts.append({"src": bool(a.get("src")), "async": "async" in a,
                                 "defer": "defer" in a, "in_head": self._in_head})
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
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img":
            self.img_total += 1
            if not (a.get("alt") or "").strip():
                self.img_missing_alt += 1
            if not (a.get("width") and a.get("height")):
                self.img_missing_dim += 1
        elif tag in HEADING_TAGS:
            self._heading_stack.append([tag, []])
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


def parse_robots(text):
    sitemaps = []
    ai_blocked = []
    cur_agents = []
    blocks_all = False
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            continue
        field, _, val = line.partition(":")
        field = field.strip().lower()
        val = val.strip()
        if field == "sitemap":
            sitemaps.append(val)
        elif field == "user-agent":
            cur_agents = [val]
        elif field == "disallow" and val == "/":
            for agent in cur_agents:
                if agent == "*":
                    blocks_all = True
                if agent in AI_CRAWLERS:
                    ai_blocked.append(agent)
    return {"sitemaps": sitemaps, "ai_blocked": ai_blocked, "blocks_all": blocks_all}


def collect_sitemap_urls(url, seen=None, depth=0):
    """Recursively pull <loc> URLs from a sitemap or sitemap index."""
    if seen is None:
        seen = set()
    if depth > 3 or url in seen:
        return []
    seen.add(url)
    r = fetch(url)
    if r.status >= 400 or not r.body:
        return []
    body = r.body
    if url.endswith(".gz") or r.header("content-type", "").endswith("gzip"):
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    text = body.decode("utf-8", errors="replace")
    locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", text, re.I | re.S)
    out = []
    if "<sitemapindex" in text.lower():
        for loc in locs:
            out.extend(collect_sitemap_urls(loc.strip(), seen, depth + 1))
    else:
        out = [l.strip() for l in locs]
    return out


def discover_pages(base_url, max_pages, robots_info):
    """Return an ordered list of page URLs to audit, homepage first."""
    home = base_url
    pages = [home]
    # Prefer the sitemap — it's the site's own statement of what matters.
    sm_urls = []
    for sm in robots_info.get("sitemaps", []):
        sm_urls.extend(collect_sitemap_urls(sm))
    if not sm_urls:
        sm_urls = collect_sitemap_urls(normalize(base_url, "/sitemap.xml"))
    for u in sm_urls:
        if u not in pages and same_site(base_url, u):
            pages.append(u)
        if len(pages) >= max_pages:
            break
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
                if u in pages or not same_site(base_url, u):
                    continue
                if re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|css|js|webp|ico)(\?|$)", u, re.I):
                    continue
                pages.append(u)
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
    page = {
        "url": url, "status": r.status, "final_url": r.final_url,
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
    if not same_site(url, r.final_url) or r.final_url.rstrip("/") != url.rstrip("/"):
        if r.final_url != url:
            findings.append(f("LOW", "Page redirects", "crawlability", 2, 2,
                              f"{url} → {r.final_url}",
                              "Link directly to the final URL to avoid redirect latency and lost link equity."))

    # Client-render detection: raw HTML has almost no words but ships scripts.
    raw_words = p.word_count
    script_heavy = r.text.lower().count("<script") >= 3
    if raw_words < 20 and script_heavy and r.status < 400:
        findings.append(f("CRITICAL",
                          "Page may be client-rendered — content missing from raw HTML",
                          "crawlability", 5, 4,
                          f"Raw HTML has {raw_words} words, title {'set' if p.title else 'missing'}, "
                          f"{'no H1' if not p.headings['h1'] else 'H1 present'}",
                          "Enable prerendering or static export. AI crawlers (GPTBot, ClaudeBot, "
                          "PerplexityBot) and Bing's first pass do not run JavaScript. For SPAs use "
                          "SSR/SSG, react-snap, or a prerender service."))
        findings.append(f("HIGH", "Content invisible to AI crawlers", "ai search", 4, 4,
                          f"{raw_words} words in raw HTML",
                          "Serve meaningful content in the initial HTML so AI answer engines can cite this page."))

    if raw_words == 0 and r.status == 200 and not p.title:
        findings.append(f("HIGH", "Possible soft 404 (200 OK with empty body)", "crawlability", 4, 2,
                          "0 words, no title",
                          "Return a real 404 status for missing pages, or fix the missing content."))

    if "noindex" in p.meta_robots:
        findings.append(f("HIGH", "Page set to noindex", "crawlability", 4, 1,
                          f'meta robots="{p.meta_robots}"',
                          "Remove noindex if this page should appear in search results."))

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
    if len(h1s) == 0:
        findings.append(f("HIGH", "Missing H1 tag", "on page", 5, 1, "No H1 element on the page",
                          "Add one H1 describing the page's main topic, aligned with the title and primary keyword."))
    elif len(h1s) > 1:
        findings.append(f("LOW", "Multiple H1 tags", "on page", 2, 1, f"{len(h1s)} H1 elements",
                          "Use a single H1 per page; demote the others to H2/H3 to keep a clear hierarchy."))

    if p.word_count < 300 and r.status == 200:
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

    if p.img_total and p.img_missing_alt:
        sev = "MEDIUM" if p.img_missing_alt > p.img_total / 2 else "LOW"
        findings.append(f(sev, "Images missing alt text", "on page", 2, 2,
                          f"{p.img_missing_alt}/{p.img_total} images without alt",
                          "Add descriptive alt text for accessibility and image search."))

    # ---- schema ----
    types, valid, invalid = parse_jsonld_types(p.jsonld)
    page["schema_types"] = sorted(types)
    if not p.jsonld:
        findings.append(f("LOW", "No structured data (JSON-LD)", "schema", 2, 2, "No JSON-LD found",
                          "Add schema.org JSON-LD (Organization, WebSite, BreadcrumbList, and page-appropriate types)."))
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
            "findings": findings}


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
           "?strategy=mobile&category=performance&url=" + urllib.parse.quote(url, safe=""))
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
        findings = []

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
        deduction = sum(SEVERITY_WEIGHT[fi["severity"]] for fi in unique if fi["category"] == cat)
        scores[cat] = max(0, 100 - deduction)
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
def run_audit(start_url, max_pages=5, use_pagespeed=False):
    if not start_url.startswith("http"):
        start_url = "https://" + start_url
    parsed = urllib.parse.urlparse(start_url)
    base = f"{parsed.scheme}://{parsed.netloc}/"
    domain = parsed.hostname or start_url

    # robots + sitemap
    robots_resp = fetch(normalize(base, "/robots.txt"))
    robots_found = robots_resp.status == 200 and robots_resp.body
    robots_info = parse_robots(robots_resp.text) if robots_found else {"sitemaps": [], "ai_blocked": [], "blocks_all": False}
    sitemap_found = bool(robots_info["sitemaps"]) or fetch(normalize(base, "/sitemap.xml")).status == 200

    # llms.txt — an emerging convention for giving AI crawlers a curated content index.
    # We report it, but only as INFO: large-scale studies have found no measurable link
    # between having one and being cited by LLMs, so it shouldn't move the score.
    llms_resp = fetch(normalize(base, "/llms.txt"))
    llms_found = (llms_resp.status == 200 and bool(llms_resp.body)
                  and b"<html" not in llms_resp.body[:2000].lower())

    pages_urls = discover_pages(base, max_pages, robots_info)

    site_findings = []
    if not robots_found:
        site_findings.append(f("MEDIUM", "robots.txt not found", "crawlability", 3, 1, "/robots.txt missing",
                               "Add a robots.txt that allows crawling and points to your sitemap."))
    if not sitemap_found:
        site_findings.append(f("MEDIUM", "XML sitemap not found", "crawlability", 3, 2, "no sitemap.xml",
                               "Publish an XML sitemap and reference it from robots.txt."))
    if robots_info.get("blocks_all"):
        site_findings.append(f("CRITICAL", "robots.txt blocks all crawlers", "crawlability", 5, 1,
                               "User-agent: * Disallow: /",
                               "Remove the site-wide Disallow so search engines can index the site."))
    for agent in robots_info.get("ai_blocked", []):
        site_findings.append(f("LOW", f"robots.txt blocks {agent}", "ai search", 2, 1,
                               f"{agent} disallowed",
                               f"Allow {agent} if you want this content cited in AI answers."))
    if not llms_found:
        site_findings.append(f("INFO", "No llms.txt file", "ai search", 1, 1, "/llms.txt not found",
                               "Optional/emerging: an llms.txt at the site root offers AI crawlers a curated "
                               "content index. Evidence that it affects AI citations is so far inconclusive, so "
                               "treat it as low priority — server-rendered content and schema matter far more."))

    # Analyze pages concurrently — each is mostly network wait, so a small thread pool
    # cuts wall-clock roughly linearly. ThreadPoolExecutor.map preserves input order, so
    # the homepage stays first and the report order is deterministic.
    workers = min(8, max(1, len(pages_urls)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        pages = [pg for pg, _ in ex.map(analyze_page, pages_urls)]

    descriptions = {}
    titles = {}
    for page in pages:
        if page.get("description"):
            descriptions.setdefault(page["description"], []).append(page["url"])
        if page.get("title"):
            titles.setdefault(page["title"], []).append(page["url"])

    # cross-page duplicate checks (added to each offending page)
    for desc, urls in descriptions.items():
        if len(urls) > 1:
            for page in pages:
                if page.get("description") == desc:
                    page["findings"].append(f("MEDIUM", "Duplicate meta description across pages",
                                              "on page", 3, 2, f"Same description on {len(urls)} pages",
                                              "Write a page-specific description reflecting this page's unique content."))
    for title, urls in titles.items():
        if len(urls) > 1:
            for page in pages:
                if page.get("title") == title:
                    page["findings"].append(f("MEDIUM", "Duplicate title across pages",
                                              "on page", 3, 2, f"Same title on {len(urls)} pages",
                                              "Give each page a distinct, descriptive title."))

    # Homepage entity schema — Organization + WebSite JSON-LD is foundational for both
    # rich results and for search/AI engines to identify the entity behind the site
    # (an E-E-A-T and knowledge-graph signal that carries more weight in 2026).
    if pages:
        home_types = set(pages[0].get("schema_types", []))
        if not ({"Organization", "WebSite", "LocalBusiness"} & home_types):
            pages[0]["findings"].append(f(
                "LOW", "No Organization/WebSite schema on homepage", "schema", 3, 2,
                "Neither Organization nor WebSite JSON-LD found on the homepage",
                "Add Organization (with logo and sameAs links to your social/profile URLs) and WebSite "
                "JSON-LD so search and AI engines can identify and trust the entity behind the site."))

    # PageSpeed on the homepage
    psi = pagespeed(pages[0]["final_url"] if pages else base, use_pagespeed)

    social = analyze_social(pages[0]) if pages else {"metrics": [], "findings": []}

    # keyword focus + footprint — must run while page parsers are still attached
    keywords = extract_keywords(pages)
    footprint = extract_footprint(pages)

    # gather every finding for scoring
    all_findings = (list(site_findings) + list(social["findings"])
                    + list(footprint["findings"]) + list(psi.get("findings", [])))
    for page in pages:
        all_findings.extend(page["findings"])

    scores, overall = score_categories(all_findings, psi, robots_info)
    conf = confidence_level(psi, pages, robots_info.get("ai_blocked"))

    # sort each page's findings by severity then impact
    for page in pages:
        page["findings"].sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], -x["impact"]))
        # drop heavy parser/response objects before returning (not JSON serializable)
        page.pop("parser", None)
        page.pop("response", None)
    social["findings"].sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], -x["impact"]))
    site_findings.sort(key=lambda x: (SEVERITY_ORDER[x["severity"]], -x["impact"]))

    return {
        "url": base, "domain": domain,
        "generated": _dt.datetime.now(),
        "page_count": len(pages),
        "confidence": conf,
        "overall": overall,
        "scores": scores,
        "robots_found": bool(robots_found),
        "sitemap_found": bool(sitemap_found),
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
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument("--out", default=".", help="Output directory")
    ap.add_argument("--no-pdf", action="store_true", help="Skip PDF rendering")
    ap.add_argument("--pagespeed", action="store_true", help="Query PageSpeed Insights (best effort)")
    ap.add_argument("--json", action="store_true", help="Also write the raw report JSON")
    args = ap.parse_args()

    print(f"Auditing {args.url} (up to {args.max_pages} pages)…", file=sys.stderr)
    report = run_audit(args.url, args.max_pages, args.pagespeed)

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

    print(f"\nOverall score: {report['overall']}/100  ·  {report['confidence']} confidence")
    print(f"HTML: {html_path}")


if __name__ == "__main__":
    main()
