"""In-memory fixture web server for the seo_audit regression tests.

Standard library only. A ``FixtureServer`` binds 127.0.0.1 on an ephemeral port and
serves a route table held in memory. Routes control status, headers, body, and can
behave differently for GET vs HEAD; a site-level ``ua_policy`` hook lets a test refuse
particular user-agents the way a CDN bot rule would.

Two builders are provided:

* :func:`build_broken_site` - reproduces every defect the new checks look for.
* :func:`build_clean_site`  - a well-formed equivalent on which none of those
  findings should fire (the false-positive guard).

Note on hostnames: ``localhost`` and ``127.0.0.1`` reach the same server, but
``seo_audit.same_site`` compares hostnames, so ``localhost`` is how a fixture gets a
genuinely cross-origin resource without leaving the machine.
"""

import http.server
import random
import socketserver
import struct
import threading
import urllib.parse
import zlib

__all__ = ["FixtureServer", "route", "make_png", "build_broken_site", "build_clean_site",
           "AI_TRAIN_REFUSED", "AI_SEARCH_REFUSED", "AI_ALL_NON_BROWSER_REFUSED",
           "BROWSER_UA"]

# Kept in sync with seo_audit.BROWSER_UA; the fixture treats exactly this string as
# "a real browser" and everything else as an automated client.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# =====================================================================================
# Server
# =====================================================================================
def _as_bytes(body):
    if isinstance(body, bytes):
        return body
    return str(body).encode("utf-8")


def route(body=b"", status=200, ctype="text/html; charset=utf-8", headers=None,
          head_status=None, location=None):
    """Build a route entry.

    head_status: status to answer HEAD with instead of ``status`` (the "HEAD 404 where
    GET 200" defect). location: sets a Location header for redirect statuses.
    """
    h = {}
    if ctype:
        h["Content-Type"] = ctype
    if location:
        h["Location"] = location
    h.update(headers or {})
    return {"status": status, "body": _as_bytes(body), "headers": h,
            "head_status": head_status}


_NOT_FOUND_HTML = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                   "<title>Page not found on the fixture site</title>"
                   "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                   "</head><body><h1>Not found</h1><p>No such page.</p></body></html>")


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_handler(site):
    class _Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._respond("GET")

        def do_HEAD(self):
            self._respond("HEAD")

        def _respond(self, method):
            path = urllib.parse.urlsplit(self.path).path
            ua = self.headers.get("User-Agent", "")
            site.hits.append((method, self.path, ua))
            r = None
            if site.ua_policy is not None:
                r = site.ua_policy(ua, path)
            if r is None:
                r = site.resolve(path)
            if r is None:
                r = route(_NOT_FOUND_HTML, status=404)
            status = r["status"]
            if method == "HEAD" and r.get("head_status"):
                status = r["head_status"]
            payload = r["body"]
            try:
                self.send_response(status)
                for k, v in r["headers"].items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if method == "GET" and payload:
                    self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True

        def log_message(self, *args):   # keep the test output clean
            pass

    return _Handler


class FixtureServer:
    """A threaded HTTP server serving an in-memory route table."""

    def __init__(self):
        self.routes = {}
        self.prefix_rules = []     # [(prefix, fn(path) -> route|None)]
        self.ua_policy = None      # fn(ua, path) -> route|None
        self.hits = []
        self.port = None
        self._httpd = None
        self._thread = None

    # -- lifecycle ------------------------------------------------------------------
    def start(self):
        self._httpd = _ThreadedHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- addressing -----------------------------------------------------------------
    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def alt_base(self):
        """The same server under a different hostname, i.e. cross-origin."""
        return "http://localhost:%d" % self.port

    def url(self, path="/"):
        return self.base + path

    def alt_url(self, path="/"):
        return self.alt_base + path

    # -- routing --------------------------------------------------------------------
    def add(self, path, r):
        self.routes[path] = r
        return self

    def add_prefix(self, prefix, fn):
        self.prefix_rules.append((prefix, fn))
        return self

    def resolve(self, path):
        r = self.routes.get(path)
        if r is not None:
            return r
        for prefix, fn in self.prefix_rules:
            if path.startswith(prefix):
                r = fn(path)
                if r is not None:
                    return r
        return None

    def reset_hits(self):
        self.hits = []


# =====================================================================================
# Binary fixtures
# =====================================================================================
def _png_chunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


def make_png(width, height, pad_bytes=0):
    """A real, parseable 8-bit RGB PNG. ``pad_bytes`` inflates the file with a tEXt
    chunk so a test can serve a legitimately heavy image without a huge raster."""
    out = b"\x89PNG\r\n\x1a\n"
    out += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw = b"".join(b"\x00" + b"\x9a\xb2\xc8" * width for _ in range(height))
    out += _png_chunk(b"IDAT", zlib.compress(raw, 6))
    if pad_bytes > 0:
        out += _png_chunk(b"tEXt", b"Comment\x00" + b"A" * pad_bytes)
    out += _png_chunk(b"IEND", b"")
    return out


# =====================================================================================
# Text fixtures
# =====================================================================================
# Deliberately boring, lowercase, digit-free vocabulary: no token can accidentally
# look like a street address (_STREET_RE), a date (_DATE_TEXT_RE) or a CTA verb.
_VOCAB = (
    "harbor lantern meadow ridge copper willow granite cedar hollow amber quartz "
    "thicket ember pebble marsh birch clover cobalt drift fennel gable heather "
    "indigo juniper kelp linen mirth nutmeg opal parsley quiver rosemary sable "
    "tallow umber velvet walnut xenon yarrow zephyr bramble cinder dune elm "
    "fjord glade hemlock inlet jetty knoll loam mesa nettle orchard prairie "
    "quarry reef sedge tundra upland vale wharf yonder alcove basalt canyon "
    "delta escarpment foothill gorge highland isthmus lagoon moraine oasis "
    "plateau ravine savanna terrace valley woodland bayou coulee esker fen"
).split()


def words(n, seed):
    rnd = random.Random(seed)
    return [rnd.choice(_VOCAB) for _ in range(n)]


def prose(n, seed, per_sentence=14):
    """``n`` words of filler laid out as sentences of ``per_sentence`` words each."""
    ws = words(n, seed)
    chunks = [ws[i:i + per_sentence] for i in range(0, len(ws), per_sentence)]
    return " ".join(c[0].capitalize() + " " + " ".join(c[1:]) + "." if len(c) > 1
                    else c[0].capitalize() + "." for c in chunks)


def prose_from(ws, per_sentence=14):
    chunks = [ws[i:i + per_sentence] for i in range(0, len(ws), per_sentence)]
    return " ".join(c[0].capitalize() + " " + " ".join(c[1:]) + "." if len(c) > 1
                    else c[0].capitalize() + "." for c in chunks)


def page_html(title, body, *, description=None, canonical=None, robots=None,
              og_image=None, og_url=None, jsonld=None, lang="en", head_extra="",
              viewport=True, og=True, twitter=True, h1=None):
    """Build a page that is clean by default; pass arguments to introduce a defect."""
    parts = ["<!doctype html>", '<html lang="%s">' % lang if lang else "<html>", "<head>",
             '<meta charset="utf-8">', "<title>%s</title>" % title]
    if viewport:
        parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    if description:
        parts.append('<meta name="description" content="%s">' % description)
    if robots:
        parts.append('<meta name="robots" content="%s">' % robots)
    if canonical:
        parts.append('<link rel="canonical" href="%s">' % canonical)
    if og:
        parts.append('<meta property="og:title" content="%s">' % title)
        parts.append('<meta property="og:type" content="website">')
        parts.append('<meta property="og:site_name" content="Fixture Site">')
        if description:
            parts.append('<meta property="og:description" content="%s">' % description)
        if og_url or canonical:
            parts.append('<meta property="og:url" content="%s">' % (og_url or canonical))
        if og_image:
            parts.append('<meta property="og:image" content="%s">' % og_image)
    if twitter:
        parts.append('<meta name="twitter:card" content="summary_large_image">')
    parts.append(head_extra)
    if jsonld:
        parts.append('<script type="application/ld+json">%s</script>' % jsonld)
    parts.append("</head><body>")
    parts.append("<h1>%s</h1>" % (h1 if h1 is not None else title))
    parts.append(body)
    parts.append("</body></html>")
    return "".join(parts)


# =====================================================================================
# User-agent policies (CDN-style bot rules)
# =====================================================================================
_FORBIDDEN_HTML = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                   "<title>Forbidden</title></head><body><h1>Access denied</h1>"
                   "</body></html>")


def _refuse(tokens):
    lowered = [t.lower() for t in tokens]

    def policy(ua, path):
        low = (ua or "").lower()
        if any(t in low for t in lowered):
            return route(_FORBIDDEN_HTML, status=403)
        return None
    return policy


#: Refuse the two documented training crawlers only.
AI_TRAIN_REFUSED = _refuse(["gptbot", "claudebot"])
#: Refuse an answer-engine search crawler (the HIGH path).
AI_SEARCH_REFUSED = _refuse(["oai-searchbot"])
#: Refuse GPTBot alone (paired with a robots.txt that names it).
AI_GPTBOT_REFUSED = _refuse(["gptbot"])


def AI_ALL_NON_BROWSER_REFUSED(ua, path):
    """Refuse anything that is not byte-for-byte the desktop browser UA."""
    if ua != BROWSER_UA:
        return route(_FORBIDDEN_HTML, status=403)
    return None


# =====================================================================================
# Shared site furniture
# =====================================================================================
def _sitemap(entries):
    """entries: [(loc, lastmod|None)]"""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod in entries:
        out.append("<url><loc>%s</loc>%s</url>"
                   % (loc, "<lastmod>%s</lastmod>" % lastmod if lastmod else ""))
    out.append("</urlset>")
    return "\n".join(out)


def _sitemap_index(children):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for c in children:
        out.append("<sitemap><loc>%s</loc></sitemap>" % c)
    out.append("</sitemapindex>")
    return "\n".join(out)


XML_CTYPE = "application/xml; charset=utf-8"
TEXT_CTYPE = "text/plain; charset=utf-8"
PNG_CTYPE = "image/png"


# =====================================================================================
# The broken site
# =====================================================================================
def build_broken_site():
    """A site that reproduces every defect the audit's newer checks look for."""
    s = FixtureServer().start()
    B = s.base                      # http://127.0.0.1:PORT
    X = s.alt_base                  # http://localhost:PORT  (cross-origin)

    nav = ('<nav><a href="/">Home</a> <a href="/page">Catalogue</a> '
           '<a href="/contact">Contact the desk</a></nav>')
    footer = '<footer><p>Fixture footer text for the broken site.</p></footer>'

    def body(text_html):
        return nav + "<main>" + text_html + "</main>" + footer

    # ---- images ------------------------------------------------------------------
    heavy_hero = make_png(1200, 600, pad_bytes=330 * 1024)      # ~330 KB+
    cross_hero = make_png(1200, 600, pad_bytes=340 * 1024)
    thumb = make_png(80, 80, pad_bytes=30 * 1024)               # ~30 KB thumbnail
    square_social = make_png(200, 200)                          # small, not landscape
    ok_social = make_png(1200, 630)                             # correct share image
    huge_social = make_png(200, 200, pad_bytes=700 * 1024)      # square AND over 600 KB

    # /img/* answers HEAD with 404 while GET succeeds.
    for name in ("t1", "t2", "t3", "t4"):
        s.add("/img/%s.png" % name, route(thumb, ctype=PNG_CTYPE, head_status=404))
    s.add("/img/social.png", route(ok_social, ctype=PNG_CTYPE, head_status=404))
    s.add("/img/small.png", route(square_social, ctype=PNG_CTYPE, head_status=404))
    s.add("/img/social-big.png", route(huge_social, ctype=PNG_CTYPE, head_status=404))
    s.add("/hero-assets/big.png", route(heavy_hero, ctype=PNG_CTYPE))
    s.add("/favicon.ico", route(make_png(32, 32), ctype="image/x-icon"))

    # cross-origin hero reached through two redirect hops
    s.add("/cdn/hero-a.jpg", route(b"", status=302, ctype=None, location="/cdn/hero-b.jpg"))
    s.add("/cdn/hero-b.jpg", route(b"", status=302, ctype=None, location="/cdn/hero.png"))
    s.add("/cdn/hero.png", route(cross_hero, ctype=PNG_CTYPE))

    s.add("/assets/site.css", route(b"body{margin:0}", ctype="text/css"))
    s.add("/vendor/tag.js", route(b"/* third party */", ctype="application/javascript"))

    # ---- homepage ----------------------------------------------------------------
    home_head = (
        '<script src="%s/vendor/tag.js"></script>'          # cross-origin, before CSS
        '<link rel="stylesheet" href="/assets/site.css">'
        # 310 KB of inlined CSS pushes </head> past the 300 KB unfurlers read.
        '<style>/*%s*/</style>' % (X, "A" * (310 * 1024))
    )
    hero_img = ('<img src="%s/cdn/hero-a.jpg" fetchpriority="high" loading="lazy" '
                'width="1200" height="600" alt="Fixture hero image">' % X)
    thumbs = "".join('<img src="/img/t%d.png" width="80" height="80" alt="Thumb %d">'
                     % (i, i) for i in range(1, 5))
    hidden_links = " ".join('<a href="/hidden/%d">Hidden %d</a>' % (i, i) for i in range(1, 6))
    dup_links = " ".join('<a href="/dup/%s">Duplicate %s</a>' % (c, c) for c in "abc")
    home_body = body(
        hero_img + thumbs
        + "<h2>Sections of the fixture</h2><p>" + prose(320, seed=1) + "</p>"
        + "<p>" + hidden_links + " " + dup_links
        + ' <a href="/page?ref=x">Catalogue with a tracking parameter</a></p>'
    )
    s.add("/", route(page_html(
        "Broken fixture homepage for audit regression tests",
        home_body,
        description=("Explore the deliberately broken fixture homepage used by the SEO "
                     "auditor regression suite to reproduce known defects."),
        canonical=B + "/",
        og_image="/img/social-big.png",
        head_extra=home_head,
        jsonld='{"@context":"https://schema.org","@type":"Organization","name":"Broken Fixture"}',
    ), headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self' https://cdn.allowed.example",
    }))

    # ---- /page (+ its ?ref= variant shares this route) ---------------------------
    s.add("/page", route(page_html(
        "Catalogue of fixture sections and their defects",
        body("<p>" + prose(300, seed=2) + "</p>"),
        description=("Read the catalogue of fixture sections so you can find the defect "
                     "each page in this regression corpus reproduces."),
        canonical=B + "/page",
        og_image="/img/social.png",
    )))

    # ---- /contact: a trust page with no postal address --------------------------
    s.add("/contact", route(page_html(
        "Contact the fixture site operations desk",
        body("<p>" + prose(240, seed=3) + "</p>"
             '<p>Reach us by email at <a href="mailto:desk@fixture.invalid">'
             "desk@fixture.invalid</a>.</p>"),
        description=("Get in touch with the fixture operations desk by email; no postal "
                     "address is published anywhere on this site."),
        canonical=B + "/contact",
        og_image="/img/social.png",
    )))

    # ---- link-architecture targets (deliberately NOT in the sitemap) ------------
    for i in range(1, 6):
        s.add("/hidden/%d" % i, route(page_html(
            "Hidden utility page number %d" % i,
            body("<p>" + prose(120, seed=10 + i) + "</p>"),
            description="A noindexed utility page kept out of the search index entirely.",
            canonical="%s/hidden/%d" % (B, i),
            robots="noindex, follow",
        )))
    for c in "abc":
        s.add("/dup/%s" % c, route(page_html(
            "Rich duplicate page %s with the real content" % c,
            body("<p>" + prose(400, seed=ord(c)) + "</p>"),
            description=("Read the rich version of fixture page %s, which canonicalises "
                         "to a much thinner URL." % c),
            canonical="%s/canon/%s" % (B, c),
            og_image="/img/social.png",
            jsonld=('{"@context":"https://schema.org","@type":"Article",'
                    '"headline":"Rich duplicate %s","datePublished":"2026-01-05"}' % c),
        )))
        s.add("/canon/%s" % c, route(page_html(
            "Thin canonical target %s" % c,
            body("<p>" + prose(60, seed=100 + ord(c)) + "</p>"),
            description="The thin canonical target that outranks the page holding the content.",
            canonical="%s/canon/%s" % (B, c),
        )))

    # ---- orphan sitemap template: 12 URLs nothing links to ----------------------
    orphan_nav = '<nav><a href="/">Home</a></nav>'
    for i in range(1, 13):
        s.add("/orphan/%d" % i, route(page_html(
            "Orphan directory entry number %d" % i,
            orphan_nav + "<main><p>" + prose(200, seed=200 + i) + "</p></main>",
            description=("Browse orphan directory entry %d, reachable only through the "
                         "XML sitemap and nothing else." % i),
            canonical="%s/orphan/%d" % (B, i),
            og_image="/img/social.png",
        )))

    # ---- near-duplicate template ------------------------------------------------
    twin_words = words(400, seed=501)
    twin_b = list(twin_words)
    twin_b[40] = "dune"
    twin_b[300] = "quarry"
    for name, ws in (("1", twin_words), ("2", twin_b), ("3", words(400, seed=777))):
        s.add("/twins/%s" % name, route(page_html(
            "Twin page %s in the near duplicate template" % name,
            body("<p>" + prose_from(ws) + "</p>"),
            description=("Compare twin page %s against its sibling; the pair shares "
                         "almost all of its wording." % name),
            canonical="%s/twins/%s" % (B, name),
            og_image="/img/social.png",
        )))

    # ---- boilerplate template ---------------------------------------------------
    boiler = prose(150, seed=601)
    for i in range(1, 5):
        s.add("/places/%d" % i, route(page_html(
            "Boilerplate location page number %d" % i,
            body("<p>" + boiler + "</p><p>" + prose(55, seed=610 + i) + "</p>"),
            description=("Look at boilerplate location page %d, which is mostly shared "
                         "template wording with little of its own." % i),
            canonical="%s/places/%d" % (B, i),
            og_image="/img/social.png",
        )))

    # ---- sitemap hygiene victims ------------------------------------------------
    for i in (1, 2):
        s.add("/sm-noindex/%d" % i, route(page_html(
            "Sitemap listed but noindexed page %d" % i,
            body("<p>" + prose(200, seed=700 + i) + "</p>"),
            description="A page listed in the XML sitemap while telling robots not to index it.",
            canonical="%s/sm-noindex/%d" % (B, i),
            robots="noindex",
            og_image="/img/social.png",
        )))
        s.add("/sm-noncanon/%d" % i, route(page_html(
            "Sitemap listed non canonical page %d" % i,
            body("<p>" + prose(200, seed=710 + i) + "</p>"),
            description="A sitemap entry whose canonical tag points at a completely different URL.",
            canonical=B + "/twins/1",
            og_image="/img/social.png",
        )))
    for name, status in (("404a", 404), ("404b", 404), ("410a", 410), ("410b", 410),
                         ("500a", 500), ("500b", 500)):
        s.add("/sm-dead/%s" % name,
              route("<!doctype html><html><head><title>Gone</title></head><body>"
                    "<h1>Gone</h1></body></html>", status=status))
    # A 403 must NOT be counted as a dead sitemap URL.
    s.add("/sm-403/a", route(_FORBIDDEN_HTML, status=403))

    # ---- one-off defect pages ---------------------------------------------------
    s.add("/hero/big", route(page_html(
        "Page with a very heavy same origin hero image",
        body('<img src="/hero-assets/big.png" fetchpriority="high" width="1200" '
             'height="600" alt="A heavy hero"><p>' + prose(240, seed=801) + "</p>"),
        description=("See how a single oversized hero image on your own origin delays "
                     "the largest contentful paint."),
        canonical=B + "/hero/big",
        og_image="/img/social.png",
    )))
    s.add("/news/broken-image", route(page_html(
        "News item whose share image is missing",
        body("<p>" + prose(240, seed=802) + "</p>"),
        description=("Read the news item whose Open Graph image URL returns a 404 to "
                     "every unfurler that asks for it."),
        canonical=B + "/news/broken-image",
        og_image="/img/missing-og.png",
    )))
    s.add("/guides/small-image", route(page_html(
        "Guide illustrated with a small square share image",
        body("<p>" + prose(240, seed=803) + "</p>"),
        description=("Learn why a small square Open Graph image is cropped to a "
                     "thumbnail card on most social platforms."),
        canonical=B + "/guides/small-image",
        og_image="/img/small.png",
    )))
    s.add("/nosnippet/page", route(page_html(
        "Page that forbids search result snippets",
        body("<p>" + prose(240, seed=804) + "</p>"),
        description=("Understand what nosnippet costs a page in search results and in "
                     "AI answers that honour the same control."),
        canonical=B + "/nosnippet/page",
        robots="nosnippet, noarchive",
        og_image="/img/social.png",
    )))
    s.add("/article/undated", route(page_html(
        "Undated article about the fixture corpus",
        body("<p>" + prose(260, seed=805) + "</p>"),
        description=("Read the undated fixture article whose only publication date "
                     "lives in its structured data."),
        canonical=B + "/article/undated",
        og_image="/img/social.png",
        jsonld=('{"@context":"https://schema.org","@type":"Article",'
                '"headline":"Undated article about the fixture corpus",'
                '"image":"%s/img/social.png","datePublished":"2026-02-02",'
                '"dateModified":"2026-02-03",'
                '"author":{"@type":"Person","name":"Fixture Author",'
                '"url":"%s/contact"}}' % (B, B)),
    )))
    repeated = ("Harbor lantern meadow ridge copper willow granite cedar hollow amber "
                "quartz thicket ember pebble.")
    # Both copies live inside ONE text node, with filler on each side: see
    # test_units.test_repeated_passage_across_block_elements_is_missed for why a
    # sentence repeated in two separate <p> elements is not detected today.
    s.add("/repeat/twice", route(page_html(
        "Page that prints the same sentence twice",
        body("<p>" + prose(90, seed=806) + " " + repeated + " "
             + prose(90, seed=807) + " " + repeated + " "
             + prose(90, seed=808) + "</p>"),
        description=("Spot the template partial that renders the same sentence twice on "
                     "a single fixture page."),
        canonical=B + "/repeat/twice",
        og_image="/img/social.png",
    )))

    # A document past Googlebot's 2 MB fetch limit (the bulk is one HTML comment).
    s.add("/bigdoc/huge", route(page_html(
        "Enormous document that exceeds the two megabyte fetch limit",
        body("<p>" + prose(240, seed=809) + "</p><!--" + "B" * (2200 * 1024) + "-->"),
        description=("Read the enormous fixture document whose markup runs well past the "
                     "two megabyte limit a crawler will fetch."),
        canonical=B + "/bigdoc/huge",
        og_image="/img/social.png",
    )))
    # Answers with Brotli although the request only offered gzip/deflate.
    s.add("/brotli/forced", route(page_html(
        "Page returned with a content encoding nobody asked for",
        body("<p>" + prose(240, seed=810) + "</p>"),
        description=("See what happens when a server answers with Brotli although the "
                     "client only offered gzip and deflate."),
        canonical=B + "/brotli/forced",
        og_image="/img/social.png",
    ), headers={"Content-Encoding": "br"}))

    # ---- robots, sitemap, llms.txt, well-known ----------------------------------
    s.add("/robots.txt", route(
        "User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % B, ctype=TEXT_CTYPE))

    entries = [(B + "/", "2026-03-01")]
    entries += [("%s/orphan/%d" % (B, i), None) for i in range(1, 13)]
    entries += [("%s/sm-dead/%s" % (B, n), None)
                for n in ("404a", "404b", "410a", "410b", "500a", "500b")]
    entries += [("%s/places/%d" % (B, i), None) for i in range(1, 5)]
    entries += [("%s/twins/%s" % (B, n), None) for n in ("1", "2", "3")]
    entries += [("%s/sm-noindex/%d" % (B, i), "2026-03-02") for i in (1, 2)]
    entries += [("%s/sm-noncanon/%d" % (B, i), "2026-03-03") for i in (1, 2)]
    entries += [(B + "/sm-403/a", None),
                (B + "/hero/big", None),
                (B + "/news/broken-image", None),
                (B + "/guides/small-image", None),
                (B + "/nosnippet/page", None),
                (B + "/article/undated", None),
                (B + "/repeat/twice", None),
                (B + "/bigdoc/huge", None),
                (B + "/brotli/forced", None),
                (B + "/page", None)]
    s.add("/sitemap.xml", route(_sitemap(entries), ctype=XML_CTYPE))

    # Extra sitemaps used by focused read_sitemaps tests (not referenced by robots).
    s.add("/sitemap-same-lastmod.xml", route(_sitemap(
        [("%s/places/%d" % (B, i % 4 + 1), "2026-04-04") for i in range(25)]),
        ctype=XML_CTYPE))
    s.add("/sitemap-empty.xml", route(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>',
        ctype=XML_CTYPE))
    s.add("/sitemap-future.xml", route(_sitemap(
        [(B + "/page", "2099-01-01"), (B + "/contact", "2026-01-01")]), ctype=XML_CTYPE))
    children = ["%s/sitemap-child-%d.xml" % (B, i) for i in range(30)]
    s.add("/sitemap-big-index.xml", route(_sitemap_index(children), ctype=XML_CTYPE))
    s.add_prefix("/sitemap-child-", lambda path: route(
        _sitemap([(B + "/page", None)]), ctype=XML_CTYPE))

    s.add("/llms.txt", route(
        "# Broken Fixture\n\n"
        "> The fixture corpus used by the SEO auditor regression suite.\n\n"
        "## Pages\n\n"
        "- [A page that no longer exists](%s/llms-dead): removed\n"
        "- [A page that canonicalises elsewhere](%s/sm-noncanon/1): moved\n"
        % (B, B), ctype=TEXT_CTYPE))
    s.add("/.well-known/security.txt", route(
        "Contact: mailto:security@fixture.invalid\n"
        "Expires: 2020-01-01T00:00:00.000Z\n", ctype=TEXT_CTYPE))
    s.add("/.git/HEAD", route("ref: refs/heads/main\n", ctype=TEXT_CTYPE))
    return s


# =====================================================================================
# The clean site
# =====================================================================================
def build_clean_site():
    """A well-formed site: none of the newer findings should fire against it."""
    s = FixtureServer().start()
    B = s.base

    guide_links = " ".join('<a href="/guides/%d">Guide %d</a>' % (i, i)
                           for i in range(1, 13))
    nav = ('<nav><a href="/">Home</a> <a href="/about">About the team</a> '
           '<a href="/contact">Contact</a> <a href="/page">Catalogue</a> '
           '<a href="/legal/privacy">Privacy</a> <a href="/legal/terms">Terms</a></nav>')
    footer = ('<footer><p>Call <a href="tel:+15550000000">+1 555 000 0000</a> or write to '
              '<a href="mailto:hello@fixture.invalid">hello@fixture.invalid</a>.</p></footer>')

    def body(text_html, extra_links=""):
        return nav + "<main>" + text_html + extra_links + "</main>" + footer

    hero = make_png(1200, 600, pad_bytes=8 * 1024)          # small, fast hero
    social = make_png(1200, 630, pad_bytes=4 * 1024)        # correct landscape share image
    small = make_png(64, 64)                                # genuinely small file
    asset_headers = {"Cache-Control": "public, max-age=31536000, immutable"}
    s.add("/media/hero.png", route(hero, ctype=PNG_CTYPE, headers=asset_headers))
    s.add("/media/social.png", route(social, ctype=PNG_CTYPE, headers=asset_headers))
    s.add("/media/icon-a.png", route(small, ctype=PNG_CTYPE, headers=asset_headers))
    s.add("/media/icon-b.png", route(small, ctype=PNG_CTYPE, headers=asset_headers))
    s.add("/favicon.ico", route(make_png(32, 32), ctype="image/x-icon",
                                headers=asset_headers))
    s.add("/assets/site.css", route(b"body{margin:0}", ctype="text/css",
                                    headers=asset_headers))
    s.add("/assets/app.js", route(b"/* first party */", ctype="application/javascript",
                                  headers=asset_headers))

    html_headers = {
        "Cache-Control": "public, max-age=300",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'",
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=()",
    }
    head_extra = ('<link rel="stylesheet" href="/assets/site.css">'
                  '<script src="/assets/app.js" defer></script>')

    def add_page(path, title, description, text, *, jsonld=None, extra_body="",
                 hero_img=False):
        inner = ""
        if hero_img:
            inner += ('<img src="/media/hero.png" fetchpriority="high" width="1200" '
                      'height="600" srcset="/media/hero.png 1200w" alt="The clean hero">'
                      '<img src="/media/icon-a.png" width="64" height="64" alt="Icon A">'
                      '<img src="/media/icon-b.png" width="64" height="64" alt="Icon B">')
        inner += "<h2>What this page covers</h2><p>" + text + "</p>"
        s.add(path, route(page_html(
            title, body(inner, extra_body),
            description=description,
            canonical=B + path,
            og_image=B + "/media/social.png",
            jsonld=jsonld,
            head_extra=head_extra,
        ), headers=html_headers))

    add_page("/", "Clean fixture homepage for audit regression tests",
             ("Explore the clean fixture homepage, the false positive guard for the SEO "
              "auditor regression suite."),
             prose(340, seed=1001),
             jsonld=('{"@context":"https://schema.org","@type":"Organization",'
                     '"name":"Clean Fixture","legalName":"Clean Fixture LLC",'
                     '"logo":"%s/media/social.png","foundingDate":"2019-05-01",'
                     '"telephone":"+1 555 000 0000","email":"hello@fixture.invalid",'
                     '"sameAs":["https://example.com/clean-fixture"],'
                     '"address":{"@type":"PostalAddress",'
                     '"streetAddress":"4100 Market Street","addressLocality":"Riverbend",'
                     '"addressRegion":"CA","postalCode":"90210","addressCountry":"US"},'
                     '"geo":{"@type":"GeoCoordinates","latitude":"34.05",'
                     '"longitude":"-118.25"},'
                     '"openingHours":"Mo-Fr 09:00-17:00"}'
                     % B),
             extra_body="<p>" + guide_links + "</p>", hero_img=True)
    add_page("/page", "Catalogue of everything the clean fixture publishes",
             ("Browse the catalogue of clean fixture pages and find the section you "
              "need without any broken links."),
             prose(330, seed=1002))
    add_page("/contact", "Contact the clean fixture team by phone or email",
             ("Get in touch with the clean fixture team; the postal address lives on "
              "the about page."),
             prose(320, seed=1003))
    # The postal address lives ONLY here, so a small sample must still find it.
    s.add("/about", route(page_html(
        "About the clean fixture team and its registered office",
        body("<h2>Where to find us</h2><p>" + prose(320, seed=1004) + "</p>"
             "<address>Clean Fixture LLC, 4100 Market Street, Riverbend, CA 90210"
             "</address>"),
        description=("Learn who runs the clean fixture site and where its registered "
                     "office is; the full postal address is published here."),
        canonical=B + "/about",
        og_image=B + "/media/social.png",
        head_extra=head_extra,
    ), headers=html_headers))
    for name, seed in (("privacy", 1005), ("terms", 1006)):
        add_page("/legal/%s" % name,
                 "Clean fixture %s policy in plain language" % name,
                 ("Read the clean fixture %s policy and see exactly what the site does "
                  "with the data it holds." % name),
                 prose(320, seed=seed))
    for i in range(1, 13):
        add_page("/guides/%d" % i, "Clean fixture guide number %d" % i,
                 ("Read clean fixture guide %d, a fully unique page written for the "
                  "regression corpus." % i),
                 prose(320, seed=1100 + i))
    for i in range(1, 4):
        add_page("/news/%d" % i, "Clean fixture news update number %d" % i,
                 ("Read clean fixture news update %d, dated and attributed exactly like "
                  "a real published article." % i),
                 prose(320, seed=1200 + i),
                 jsonld=('{"@context":"https://schema.org","@type":"Article",'
                         '"headline":"Clean fixture news update number %d",'
                         '"image":"%s/media/social.png","datePublished":"2026-02-0%d",'
                         '"dateModified":"2026-02-1%d",'
                         '"author":{"@type":"Person","name":"Fixture Author",'
                         '"url":"%s/about"}}' % (i, B, i, i, B)),
                 extra_body=('<p>Published <time datetime="2026-02-0%d">February %d, '
                             '2026</time>.</p>' % (i, i)))
    for i in range(1, 4):
        add_page("/places/%d" % i, "Clean fixture location profile number %d" % i,
                 ("Discover clean fixture location profile %d, written from scratch "
                  "rather than stamped from a template." % i),
                 prose(330, seed=1300 + i))

    s.add("/robots.txt", route(
        "User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % B, ctype=TEXT_CTYPE))
    entries = [(B + "/", "2026-03-01"), (B + "/page", "2026-03-02"),
               (B + "/contact", "2026-03-03"), (B + "/about", "2026-03-04")]
    entries += [("%s/guides/%d" % (B, i), "2026-04-%02d" % i) for i in range(1, 13)]
    entries += [("%s/news/%d" % (B, i), "2026-05-0%d" % i) for i in range(1, 4)]
    entries += [("%s/places/%d" % (B, i), "2026-06-0%d" % i) for i in range(1, 4)]
    s.add("/sitemap.xml", route(_sitemap(entries), ctype=XML_CTYPE))

    s.add("/llms.txt", route(
        "# Clean Fixture\n\n"
        "> The well formed fixture corpus used as a false positive guard.\n\n"
        "## Pages\n\n"
        "- [Catalogue](%s/page): everything the site publishes\n"
        "- [About](%s/about): who runs the site\n" % (B, B), ctype=TEXT_CTYPE))
    s.add("/.well-known/security.txt", route(
        "Contact: mailto:security@fixture.invalid\n"
        "Expires: 2099-01-01T00:00:00.000Z\n", ctype=TEXT_CTYPE))
    return s


# =====================================================================================
# Small purpose-built sites
# =====================================================================================
def build_minimal_site(*, robots_text=None, robots_status=200, extra=None,
                       homepage_headers=None, robots_ctype=TEXT_CTYPE,
                       robots_headers=None):
    """A one-page site used by the AI-crawler and robots.txt scenarios."""
    s = FixtureServer().start()
    B = s.base
    s.add("/", route(page_html(
        "Minimal fixture homepage for scenario tests",
        ('<nav><a href="/page">Catalogue</a></nav><main><p>' + prose(300, seed=2001)
         + "</p></main>"),
        description=("Explore the minimal fixture homepage used by the AI crawler and "
                     "robots.txt scenario tests."),
        canonical=B + "/",
        og_image=B + "/media/social.png",
    ), headers=homepage_headers))
    s.add("/page", route(page_html(
        "Minimal fixture catalogue page",
        '<main><p>' + prose(300, seed=2002) + "</p></main>",
        description="Browse the minimal fixture catalogue used by the scenario tests.",
        canonical=B + "/page",
        og_image=B + "/media/social.png",
    )))
    s.add("/media/social.png", route(make_png(1200, 630), ctype=PNG_CTYPE))
    s.add("/favicon.ico", route(make_png(32, 32), ctype="image/x-icon"))
    if robots_status != 200:
        s.add("/robots.txt", route("server exploded", status=robots_status,
                                   ctype=TEXT_CTYPE))
    else:
        s.add("/robots.txt", route(
            robots_text if robots_text is not None
            else "User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % B,
            ctype=robots_ctype, headers=robots_headers))
    s.add("/sitemap.xml", route(_sitemap([(B + "/", "2026-03-01"),
                                          (B + "/page", "2026-03-02")]), ctype=XML_CTYPE))
    for path, r in (extra or {}).items():
        s.add(path, r)
    return s


def build_address_site(with_address=True):
    """A site whose only postal address (if any) sits on an /about page that the
    sitemap does not list, so a small sample never crawls it."""
    s = FixtureServer().start()
    B = s.base
    nav = ('<nav><a href="/">Home</a> <a href="/about">About us</a> '
           '<a href="/contact">Contact</a></nav>')
    s.add("/robots.txt", route(
        "User-agent: *\nAllow: /\n\nSitemap: %s/sitemap.xml\n" % B, ctype=TEXT_CTYPE))
    s.add("/sitemap.xml", route(_sitemap([(B + "/", "2026-03-01")]), ctype=XML_CTYPE))
    s.add("/", route(page_html(
        "Homepage of the address visibility fixture site",
        nav + "<main><p>" + prose(320, seed=3001) + "</p></main>",
        description=("Read about the fixture site that publishes its postal address on "
                     "one page only, to test entity transparency."),
        canonical=B + "/",
    )))
    about_body = "<main><p>" + prose(300, seed=3002) + "</p>"
    if with_address:
        about_body += ("<address>Fixture Holdings LLC, 4100 Market Street, Riverbend, "
                       "CA 90210</address>")
    about_body += "</main>"
    s.add("/about", route(page_html(
        "About the address visibility fixture organisation",
        nav + about_body,
        description=("Learn who runs the address visibility fixture site and where it "
                     "is registered."),
        canonical=B + "/about",
    )))
    s.add("/contact", route(page_html(
        "Contact the address visibility fixture organisation",
        nav + "<main><p>" + prose(280, seed=3003) + "</p>"
        '<p><a href="mailto:hello@fixture.invalid">hello@fixture.invalid</a></p></main>',
        description=("Get in touch with the address visibility fixture organisation by "
                     "email."),
        canonical=B + "/contact",
    )))
    return s


def build_catchall_site(body, ctype=TEXT_CTYPE):
    """A server that answers EVERY path with the same 200 response — the Worker/SPA
    catch-all that used to make check_exposed_files cry wolf."""
    s = FixtureServer().start()
    s.add_prefix("/", lambda path: route(body, ctype=ctype))
    return s
