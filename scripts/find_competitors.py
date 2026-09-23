#!/usr/bin/env python3
"""
find_competitors.py: find out which domains actually occupy the results for the queries a
client wants to rank for, tally and classify them, and write a ranked shortlist of
competitors to audit next with seo_audit.py.

Usage:
    python3 scripts/find_competitors.py --client example.com [--owned a.com b.com]
        (--queries "q1" "q2" ... | --queries-file FILE) [--engine brave]
        [--serp-json FILE ...] [--delay SECONDS] [--top 15] [--shortlist 8]
        [--classify-file JSON] [--out DIR] [--date YYYY-MM-DD]

Writes competitors-<client-domain>-<date>.md and .json to --out (default ".") and prints
both paths and the shortlist to stdout. Progress and errors go to stderr. Nothing is
written when no query was captured, so a failed run never overwrites a good report.

Inputs
    Google is usually not fetchable from an agent environment, so there are two sources.

    1. Brave Search, fetched live (used when no --serp-json is given). One request per
       query to https://search.brave.com/search?q=<query>&source=web with a desktop
       browser User-Agent and --delay seconds between requests (default 4). Brave answers
       HTTP 429 after roughly a dozen rapid queries. The loop never retries: it stops on
       the first 429, on a captcha interstitial, or on the second failure in a row (a
       non-200 status, or a 200 page with no parseable results), and the report says how
       many queries were captured. Raw HTML is kept in memory only.

    2. SERP JSON exported from a paid provider (--serp-json, one or more files, format
       detected by keys). This is how real Google organic and local-pack results come in.
       Nothing is fetched live when --serp-json is given. If --queries or --queries-file is
       also given, it selects which exported queries to use, and requested queries missing
       from the files count as not captured.

       SerpApi (Google engine):
           {"search_parameters": {"q": "...", "engine": "google"},
            "organic_results": [{"position": 1, "link": "...", "title": "..."}, ...],
            "local_results": {"places": [{"position": 1, "title": "...", "rating": 4.9,
                                          "reviews": 812, "website": "..."}]}
                (a plain list of places is accepted too, and the website may sit
                 under "links": {"website": "..."}),
            "ads", "inline_videos", "top_stories", "discussions_and_forums",
            "related_questions": [{"link": "...", "title": "..."}]       (optional)
            "knowledge_graph": {"title": "...", "website": "..."}}        (optional)

       DataForSEO (SERP API):
           {"tasks": [{"data": {"keyword": "...", "se": "google"},
                       "result": [{"keyword": "...", "se_domain": "google.com",
                                   "items": [{"type": "organic", "rank_group": 1,
                                              "rank_absolute": 3, "url": "...",
                                              "domain": "...", "title": "..."},
                                             {"type": "local_pack", "rank_group": 1,
                                              "title": "...", "domain": "...",
                                              "rating": {"value": 4.8,
                                                         "votes_count": 120}},
                                             ...]}]}]}
           The query comes from result.keyword, else task.data.keyword. A bare result
           object ({"keyword": ..., "items": [...]}) is accepted too.

       A file may also hold a JSON list of such objects. A file (or list item) that matches
       neither shape is reported on stderr by name and skipped; the other files still run.

Positions
    Every position is an organic rank: ads, carousels, entity cards and local packs are not
    counted. Brave: the order of the web-result blocks after de-duplicating URLs (Brave's
    own data-pos, which counts every block on the page, is kept as "slot"). SerpApi: the
    provider's organic "position". DataForSEO: "rank_group" of organic items
    ("rank_absolute", which counts every SERP element, is kept as "slot"). Results beyond
    --top per query are dropped, whatever the source.

    Every output names the engine the positions came from. Positions from Brave or any other
    non-Google engine are a proxy for Google, not a measurement of it.

Brave markup relied on (checked against saved result pages, September 2026)
    section#mixed-main holds the main column. Organic results are
    <div class="snippet" data-type="web" data-pos="N">. The first external <a href> in the
    block is the result, and the element with class "title" carries the title in its title
    attribute (its text is the fallback). Other blocks are recorded as SERP features, kept
    apart from organic results: data-type="ad" (landing page in data-landing-page),
    data-type="cluster" (reported as "video" when it holds video-cluster markup), an untyped
    snippet in the main column (Brave's entity/answer card for a business, reported as
    "entity"), #faq (People also ask sources), #discussions (forum threads), #llm-snippet
    (AI answer, usually filled by JavaScript so it rarely has links) and #infobox in
    section#mixed-side (the entity card on the right, reported as "infobox"). #locations is
    Brave's places block and is recorded as the local pack (title, rating, review count;
    Brave does not link a website there, and it is not Google's local pack). If no
    data-type="web" block is found the parser falls back to every external <a href> in
    document order, minus Brave's own hosts, w3.org, Google Maps links and asset URLs, and
    marks the record parse_mode "fallback". Brave's captcha interstitial is detected before
    that fallback runs, because its torproject.org link would otherwise look like a result.
    It is recognized by its page:"/captcha" config, its "flagged as being suspicious"
    sentence or that torproject.org link. The bare word "captcha" is no signal: every normal
    result page carries it in Brave's i18n bundle. A 200 page with none of those markers and
    no parseable result (a query Brave has nothing for) is not a captcha; it counts as a
    failure under the two-in-a-row rule.

Tally and score
    Per domain (lowercased, "www." stripped): the queries it appeared in, best position,
    average position over every appearance, the URLs seen, and a visibility score:

        score = sum over every organic appearance of 1 / position

    Position 1 is worth 1.0, position 2 0.5, position 10 0.1. It is a rough stand-in for
    click share, which falls steeply with position. It rewards ranking high and ranking for
    many queries, and a domain holding two results on one query gets credit for both.
    Local-pack entries and SERP features are reported in their own tables and do not add
    to the score.

Classification (hints, and the report says so)
    own          --client and --owned domains and their subdomains. Excluded from the
                 shortlist and reported in their own table with positions.
    marketplace  OTAs and listing platforms (CLASSIFY_TABLE["marketplace"]).
    social       social networks and video platforms.
    reference    wikis, plus any .edu host.
    news         a few named outlets, plus heuristics on the site name (below): news,
                 times, tribune, herald or gazette as one of its hyphen-separated words or
                 at its end (flag-news, nytimes); a site name ending in "sun";
                 fox/abc/nbc/cbs station names; K/W call letters followed by "tv".
    directory    heuristics on the site name: visit, tourism, downtown or chamber at its
                 start, at its end or as one of its hyphen-separated words (visitarizona,
                 flagstaffchamber), skipped when the name also contains "tour" outside
                 "tourism"; or a .org or .gov host.
    business     everything else. These are the competitor candidates.
    The site name is the registrable label ("goodtimestours" for www.goodtimestours.com,
    "bbc" for bbc.co.uk). The word heuristics above and the brand tokens look only at it:
    subdomain labels and hint words in the middle of a name do not count, so
    booking.rivaltours.com and goodtimestours.com stay "business", and the "tour" rule
    keeps downtownflagstaffghosttours.com there too. Table entries containing a dot are
    domains and match the host or any subdomain. Entries without one are brand tokens and
    match the site name or one of its hyphen-separated words, so "tripadvisor" matches
    tripadvisor.co.uk and "expedia" matches expedia-aarp.com. Blogs, affiliates and
    resellers the table does not know land in "business", and some .org news outlets land
    in "directory". --classify-file takes JSON, either {"domain": "category"} or
    {"category": ["domain", ...]}, and wins over the table and the heuristics. A category
    outside the built-in set gets a table of its own.
"""

import argparse
import datetime as _dt
import json
import os
import re
import sys
import time
import urllib.parse
from html.parser import HTMLParser

# seo_audit.py sits next to this file. Put this directory on sys.path so the import works
# whether the script is run from scripts/, from the repo root, or imported by the tests.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from seo_audit import BROWSER_UA, fetch  # noqa: E402

VERSION = "1.0.0"
DEFAULT_TOP = 15          # organic results kept per query
DEFAULT_SHORTLIST = 8     # business domains in the shortlist
DEFAULT_DELAY = 4.0       # seconds between live Brave requests

BRAVE_SEARCH_URL = "https://search.brave.com/search?q=%s&source=web"
BRAVE_ENGINE = "Brave Search (search.brave.com, live HTML)"
PROXY_CAVEAT = ("Non-Google engines are proxies: positions from Brave, Bing or any other "
                "non-Google engine show which domains publish for these queries, not where "
                "those domains rank on Google.")

# ----- classification -----------------------------------------------------------------
CATEGORY_LABELS = {
    "business": "business",
    "marketplace": "marketplace/OTA",
    "social": "social/video",
    "reference": "reference",
    "directory": "directory/DMO",
    "news": "news",
    "own": "own",
}
# Entries with a dot are domains; entries without one are brand tokens (module docstring).
# Deliberately small: extend or override per client with --classify-file.
CLASSIFY_TABLE = {
    "marketplace": ("tripadvisor", "viator", "getyourguide", "expedia", "travelocity",
                    "booking", "airbnb", "groupon", "peek", "yelp", "klook", "amazon", "etsy",
                    # tour-activity OTAs that show up on experience queries
                    "pelago", "tiqets", "headout", "civitatis", "musement"),
    "social": ("facebook", "instagram", "tiktok", "youtube", "reddit", "pinterest",
               "twitter", "linkedin", "x.com", "youtu.be"),
    "reference": ("wikipedia", "wikitravel", "wikivoyage"),
    "news": ("azfamily", "kpnx", "azcentral"),
}
CATEGORY_ALIASES = {
    "ota": "marketplace", "marketplace/ota": "marketplace", "aggregator": "marketplace",
    "social/video": "social", "video": "social",
    "dmo": "directory", "directory/dmo": "directory",
    "competitor": "business",
}
DIRECTORY_HINTS = ("visit", "tourism", "downtown", "chamber")
NEWS_HINTS = ("news", "times", "tribune", "herald", "gazette")
_NEWS_SITE_RE = re.compile(r"^(?:fox|abc|nbc|cbs)(?:\d+[a-z]*|news)?$"   # fox10phoenix, abc15
                           r"|^[kw][a-z]{1,3}tv\d*$"                      # wbtv, kxxvtv
                           r"|sun$")                                      # azdailysun
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}

# ----- Brave parsing ------------------------------------------------------------------
_BRAVE_REGIONS = {"mixed-top": "main", "mixed-main": "main", "mixed-side": "side"}
_BRAVE_ID_KINDS = {
    "faq": "faq", "discussions": "discussions", "locations": "local",
    "llm-snippet": "ai_answer", "infobox": "infobox", "infobox-snippet": "infobox",
    # search-elsewhere, related-queries and pagination blocks: consumed so their links
    # never count as results or features
    "search-elsewhere": "_skip", "related-queries": "_skip", "pagination-snippet": "_skip",
}
_ENGINE_HOSTS = ("brave.com", "brave.app", "w3.org", "hackerone.com")
_ASSET_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|svg|ico|css|js|mjs|woff2?|ttf|otf|eot|"
                       r"mp4|webm|json|xml|webmanifest|map)$", re.I)
# Markers of Brave's "your request has been flagged" interstitial. The bare word "captcha"
# is not one: every normal result page has it twice in the i18n bundle.
_CAPTCHA_RE = re.compile(r'page:"/captcha"|flagged as being suspicious'
                         r'|https?://[^"\s]*torproject\.org', re.I)


# =====================================================================================
# Domains
# =====================================================================================
def normalize_domain(value):
    """Lowercase host with any scheme, path, port and leading "www." removed.
    Accepts a bare domain or a full URL; returns "" when there is no host."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if "://" in v:
        try:
            v = urllib.parse.urlsplit(v).hostname or ""
        except ValueError:
            return ""
    else:
        v = v.split("/")[0].split("?")[0].split("#")[0].split("@")[-1].split(":")[0]
    v = v.strip(".")
    if v.startswith("www."):
        v = v[4:]
    return v


def domain_of(url):
    """Normalized domain of a URL ("" if it has none)."""
    try:
        host = urllib.parse.urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
    return normalize_domain(host)


def _host_matches(host, domain):
    return bool(domain) and (host == domain or host.endswith("." + domain))


def _site_name(host):
    """The registrable label: "azdailysun" for azdailysun.com, "bbc" for bbc.co.uk."""
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in _SECOND_LEVEL:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else parts[0]


def _entry_matches(host, entry):
    entry = entry.lower().strip()
    if not entry:
        return False
    if "." in entry:
        return _host_matches(host, normalize_domain(entry))
    # Brand tokens match the site name or one of its hyphen-separated words, never a
    # subdomain label: "booking" is not booking.rivaltours.com.
    name = _site_name(host)
    return entry == name or entry in name.split("-")


def load_classify_file(path):
    """Read a --classify-file. Returns {domain: category}. Accepts {"domain": "category"}
    and {"category": ["domain", ...]}; categories are lowercased and aliased
    (ota -> marketplace, dmo -> directory, competitor -> business, ...)."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("%s: expected a JSON object" % path)
    out = {}
    for key, val in data.items():
        if isinstance(val, str):
            dom, cat = normalize_domain(key), val
            if dom:
                out[dom] = _canon_category(cat)
        elif isinstance(val, list):
            for d in val:
                dom = normalize_domain(str(d))
                if dom:
                    out[dom] = _canon_category(key)
        else:
            raise ValueError("%s: value for %r must be a category string or a list of "
                             "domains" % (path, key))
    return out


def _canon_category(cat):
    c = str(cat).strip().lower()
    return CATEGORY_ALIASES.get(c, c)


def classify_domain(domain, own=(), overrides=None):
    """Return (category, basis) for a normalized domain.

    Order: own domains, then --classify-file overrides, then CLASSIFY_TABLE, then the news
    and directory heuristics, then "business". basis says which rule fired."""
    host = normalize_domain(domain)
    for o in own:
        if _host_matches(host, normalize_domain(o)):
            return "own", "own domain"
    if overrides:
        for dom in sorted(overrides, key=len, reverse=True):   # most specific first
            if _host_matches(host, dom):
                return overrides[dom], "classify-file"
    for cat in ("marketplace", "social", "reference", "news"):
        for entry in CLASSIFY_TABLE[cat]:
            if _entry_matches(host, entry):
                return cat, "table: " + entry
    if host.endswith(".edu") or ".edu." in host:
        return "reference", "heuristic: .edu"
    # Anchored on the site name so goodtimestours.com is not news and
    # downtownflagstaffghosttours.com is not a DMO (module docstring).
    name = _site_name(host)
    for hint in NEWS_HINTS:
        if hint in name.split("-") or name.endswith(hint):
            return "news", "heuristic: '%s' in site name" % hint
    if _NEWS_SITE_RE.search(name):
        return "news", "heuristic: station or paper name"
    if not re.search(r"tour(?!ism)", name):
        for hint in DIRECTORY_HINTS:
            if name.startswith(hint) or name.endswith(hint) or hint in name.split("-"):
                return "directory", "heuristic: '%s' in site name" % hint
    for tld in (".org", ".gov"):
        if host.endswith(tld) or (tld + ".") in host:
            return "directory", "heuristic: %s host" % tld
    return "business", "default"


# =====================================================================================
# Brave HTML
# =====================================================================================
def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _url_key(url):
    base = urllib.parse.urldefrag(url)[0]
    return base[:-1] if base.endswith("/") else base


def _is_result_url(url):
    """An external http(s) URL that could be a search result: not the engine's own hosts,
    not a Google Maps link, not a static asset."""
    if not str(url).lower().startswith(("http://", "https://")):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if not host or any(_host_matches(host, h) for h in _ENGINE_HOSTS):
        return False
    if re.search(r"(^|\.)google\.[a-z.]+$", host) and parts.path.startswith("/maps"):
        return False
    return not _ASSET_RE.search(parts.path)


class _BraveParser(HTMLParser):
    """Collects Brave result blocks (see the module docstring) plus every absolute link in
    document order for the fallback. Nesting is tracked per tag name, so only the tags that
    open a block, a capture or a list item need to be balanced."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []        # closed blocks in document order
        self.links = []         # (url, anchor text) for every absolute href
        self._region = None     # "main" / "side" while inside section#mixed-*
        self._region_depth = 0
        self._block = None
        self._block_depth = 0
        self._anchor = None
        self._captures = []     # [tag, depth, target dict, key, text parts]
        self._item = None       # current #locations list item
        self._item_depth = 0

    # -- helpers --
    def _capture(self, tag, target, key):
        self._captures.append([tag, 1, target, key, []])

    def _open_block(self, tag, a, cls, eid):
        dtype = a.get("data-type", "").strip().lower()
        if dtype and tag == "div":
            kind = dtype
        elif eid in _BRAVE_ID_KINDS:
            kind = _BRAVE_ID_KINDS[eid]
        elif tag == "div" and "snippet" in cls and not eid and self._region == "main":
            kind = "entity"
        else:
            return
        slot = a.get("data-pos", "").strip()
        self._block = {"kind": kind, "_tag": tag, "slot": int(slot) if slot.isdigit() else None,
                       "title": "", "links": [], "items": [], "video": False,
                       "landing": a.get("data-landing-page", "").strip(),
                       "headline": _clean(a.get("data-headline-text", ""))}
        self._block_depth = 1

    def _inside_block(self, tag, a, cls):
        b = self._block
        if any(c.startswith("video-cluster") for c in cls):
            b["video"] = True
        if (b["kind"] == "web" and not b["title"]
                and ("title" in cls or "search-snippet-title" in cls)):
            if _clean(a.get("title", "")):
                b["title"] = _clean(a["title"])
            else:
                self._capture(tag, b, "title")
        if b["kind"] == "local":
            if tag == "li" and self._item is None:
                self._item = {"title": "", "rating": "", "reviews": ""}
                self._item_depth = 1
            elif self._item is not None and tag == "div":
                if "title" in cls and not self._item["title"]:
                    self._capture(tag, self._item, "title")
                elif "rating" in cls and not self._item["rating"]:
                    self._capture(tag, self._item, "rating")
                elif "review-count" in cls:
                    self._capture(tag, self._item, "reviews")

    def _finish_anchor(self):
        anc, self._anchor = self._anchor, None
        text = _clean("".join(anc["text"]))
        self.links.append((anc["url"], text))
        if anc["block"] is not None:
            anc["block"]["links"].append((anc["url"], text))

    def _close_block(self):
        b, self._block, self._block_depth = self._block, None, 0
        if self._item is not None:
            b["items"].append(self._item)
            self._item = None
        for cap in self._captures:
            cap[2][cap[3]] = _clean("".join(cap[4]))
        self._captures = []
        if b["kind"] != "_skip":
            self.blocks.append(b)

    def finish(self):
        """Flush anything a truncated page left open."""
        if self._anchor is not None:
            self._finish_anchor()
        if self._block is not None:
            self._close_block()

    # -- HTMLParser hooks --
    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "").split()
        eid = a.get("id", "").strip()
        had_block = self._block is not None
        if self._region and tag == "section":
            self._region_depth += 1
        if had_block and tag == self._block["_tag"]:
            self._block_depth += 1
        if self._item is not None and tag == "li":
            self._item_depth += 1
        for cap in self._captures:
            if cap[0] == tag:
                cap[1] += 1
        if tag == "section" and self._region is None and eid in _BRAVE_REGIONS:
            self._region, self._region_depth = _BRAVE_REGIONS[eid], 1
        if had_block:
            self._inside_block(tag, a, cls)
        else:
            self._open_block(tag, a, cls, eid)
        if tag == "a":
            if self._anchor is not None:
                self._finish_anchor()
            href = a.get("href", "").strip()
            if href.lower().startswith(("http://", "https://")):
                self._anchor = {"url": href, "text": [], "block": self._block}

    def handle_endtag(self, tag):
        if tag == "a" and self._anchor is not None:
            self._finish_anchor()
        for cap in list(self._captures):
            if cap[0] == tag:
                cap[1] -= 1
                if cap[1] == 0:
                    cap[2][cap[3]] = _clean("".join(cap[4]))
                    self._captures.remove(cap)
        if self._item is not None and tag == "li":
            self._item_depth -= 1
            if self._item_depth == 0:
                if self._block is not None:
                    self._block["items"].append(self._item)
                self._item = None
        if self._block is not None and tag == self._block["_tag"]:
            self._block_depth -= 1
            if self._block_depth == 0:
                self._close_block()
        if self._region and tag == "section":
            self._region_depth -= 1
            if self._region_depth == 0:
                self._region = None

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor["text"].append(data)
        for cap in self._captures:
            cap[4].append(data)


def looks_like_captcha(html):
    """True for Brave's "your request has been flagged" interstitial.

    Matches its page:"/captcha" config, its "flagged as being suspicious" sentence or its
    torproject.org link. A normal page with no organic blocks is not a captcha: it falls
    through to the "no results parsed" failure in fetch_brave_serps."""
    return bool(_CAPTCHA_RE.search(html or ""))


def parse_brave_page(html, top=DEFAULT_TOP):
    """Parse one Brave result page.

    Returns {"organic": [...], "local_pack": [...], "features": [...], "parse_mode": str}.
    organic items are {"position", "url", "domain", "title", "slot"}; local_pack items are
    {"position", "title", "rating", "reviews", "domain"}; features are
    {"kind", "url", "domain", "title"} for ads, entity/infobox cards, video carousels,
    discussions and People-also-ask sources. parse_mode is "blocks" when data-type="web"
    blocks were found and "fallback" when every external link was used instead."""
    p = _BraveParser()
    try:
        p.feed(html or "")
        p.close()
    except Exception:   # pathological markup: keep whatever was parsed before the error
        pass
    p.finish()

    organic, seen = [], set()
    for b in p.blocks:
        if b["kind"] != "web":
            continue
        link = next(((u, t) for u, t in b["links"] if _is_result_url(u)), None)
        if not link or _url_key(link[0]) in seen:
            continue
        seen.add(_url_key(link[0]))
        organic.append({"position": len(organic) + 1, "url": link[0],
                        "domain": domain_of(link[0]), "title": b["title"] or link[1],
                        "slot": b["slot"]})
        if len(organic) >= top:
            break
    mode = "blocks"
    if not organic:
        mode = "fallback"
        for url, text in p.links:
            if not _is_result_url(url) or _url_key(url) in seen:
                continue
            seen.add(_url_key(url))
            organic.append({"position": len(organic) + 1, "url": url,
                            "domain": domain_of(url), "title": text, "slot": None})
            if len(organic) >= top:
                break

    features = []
    for b in p.blocks:
        kind = b["kind"]
        if kind in ("web", "local"):
            continue
        if kind == "cluster" and b["video"]:
            kind = "video"
        if kind == "ad":
            if _is_result_url(b["landing"]):
                features.append({"kind": "ad", "url": b["landing"],
                                 "domain": domain_of(b["landing"]), "title": b["headline"]})
            continue
        fseen = set()
        for url, text in b["links"]:
            if not _is_result_url(url) or _url_key(url) in fseen:
                continue
            fseen.add(_url_key(url))
            features.append({"kind": kind, "url": url, "domain": domain_of(url),
                             "title": text[:160]})

    local = []
    for b in p.blocks:
        if b["kind"] != "local":
            continue
        for it in b["items"]:
            if not it["title"]:
                continue
            m = re.search(r"\d+(?:\.\d+)?", it["rating"])
            digits = re.sub(r"[^\d]", "", it["reviews"])
            local.append({"position": len(local) + 1, "title": it["title"],
                          "rating": float(m.group(0)) if m else None,
                          "reviews": int(digits) if digits else None, "domain": ""})
    return {"organic": organic, "local_pack": local, "features": features, "parse_mode": mode}


def parse_brave_html(html, top=DEFAULT_TOP):
    """Ordered organic results of a Brave page: [{"position", "url", "domain", "title",
    "slot"}, ...], de-duplicated by URL and capped at ``top``."""
    return parse_brave_page(html, top)["organic"]


def brave_search_url(query):
    return BRAVE_SEARCH_URL % urllib.parse.quote_plus(query)


def fetch_brave_serps(queries, delay=DEFAULT_DELAY, top=DEFAULT_TOP, fetcher=None,
                      sleep=None, log=None):
    """Fetch and parse Brave results for each query, politely and without retries.

    Waits ``delay`` seconds between requests. Stops on HTTP 429, on a captcha page, or on
    the second failure in a row (non-200, or a 200 page with no results). ``fetcher``
    defaults to seo_audit.fetch, looked up at call time so tests can replace it.

    Returns (records, status). status = {"requested", "captured", "stopped" (None or a
    sentence), "notes": [{"query", "status", "note"}], "html": {query: raw html}}; the raw
    HTML stays in memory and is never written."""
    fetcher = fetcher or fetch
    sleep = sleep or time.sleep
    log = log or sys.stderr
    records, notes, html_cache = [], [], {}
    stopped, bad_in_a_row = None, 0
    total = len(queries)
    for i, q in enumerate(queries):
        if i:
            sleep(delay)
        url = brave_search_url(q)
        resp = fetcher(url, ua=BROWSER_UA,
                       extra_headers={"Accept-Language": "en-US,en;q=0.9"})
        status = getattr(resp, "status", 0) or 0
        if status == 429:
            notes.append({"query": q, "status": 429, "note": "rate limited"})
            stopped = ("Brave answered HTTP 429 (rate limited) on query %d of %d; stopped "
                       "without retrying. %d captured." % (i + 1, total, len(records)))
            print("[%d/%d] %s: HTTP 429, stopping" % (i + 1, total, q), file=log)
            break
        if status == 200:
            html = resp.text
            html_cache[q] = html
            if looks_like_captcha(html):
                notes.append({"query": q, "status": 200, "note": "captcha page"})
                stopped = ("Brave served a captcha page on query %d of %d; stopped without "
                           "retrying. %d captured." % (i + 1, total, len(records)))
                print("[%d/%d] %s: captcha page, stopping" % (i + 1, total, q), file=log)
                break
            page = parse_brave_page(html, top)
            if page["organic"]:
                bad_in_a_row = 0
                rec = {"query": q, "engine": BRAVE_ENGINE, "engine_key": "brave",
                       "source": url}
                rec.update(page)
                records.append(rec)
                print("[%d/%d] %s: %d results (%s)" % (i + 1, total, q, len(page["organic"]),
                                                       page["parse_mode"]), file=log)
                continue
            note = "no results parsed"
        else:
            note = getattr(resp, "error", None) or "HTTP %s" % status
        bad_in_a_row += 1
        notes.append({"query": q, "status": status, "note": note})
        print("[%d/%d] %s: %s" % (i + 1, total, q, note), file=log)
        if bad_in_a_row >= 2:
            stopped = ("Two failed responses in a row (last: %s) on query %d of %d; stopped "
                       "without retrying. %d captured." % (note, i + 1, total, len(records)))
            break
    return records, {"requested": total, "captured": len(records), "stopped": stopped,
                     "notes": notes, "html": html_cache}


# =====================================================================================
# SERP JSON exports
# =====================================================================================
_SERPAPI_ENGINES = {"google": "Google", "google_maps": "Google Maps", "google_local": "Google",
                    "bing": "Bing", "duckduckgo": "DuckDuckGo", "yahoo": "Yahoo"}
_SERPAPI_FEATURES = (("ads", "ad"), ("inline_videos", "video"), ("top_stories", "news"),
                     ("discussions_and_forums", "discussions"), ("related_questions", "faq"))
_DFS_FEATURE_KINDS = {"paid": "ad", "video": "video", "top_stories": "news",
                      "people_also_ask": "faq", "discussions_and_forums": "discussions",
                      "knowledge_graph": "knowledge_graph", "images": "images"}


def _num(value, cast):
    try:
        return cast(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def detect_serp_format(data):
    """"serpapi", "dataforseo" or None, decided by keys (see the module docstring)."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("tasks"), list):
        return "dataforseo"
    if isinstance(data.get("items"), list) and ("keyword" in data or "se_domain" in data):
        return "dataforseo"
    if any(k in data for k in ("organic_results", "local_results", "search_parameters")):
        return "serpapi"
    return None


def _organic_from(rows, top, pos_key, slot_key=None):
    """Sort provider rows by their own position, drop duplicates, cap at top."""
    rows = [r for r in rows if isinstance(r, dict) and _is_result_url(r.get("_url", ""))]
    rows.sort(key=lambda r: (_num(r.get(slot_key or pos_key), int) or 10 ** 6))
    out, seen = [], set()
    for r in rows:
        if _url_key(r["_url"]) in seen:
            continue
        seen.add(_url_key(r["_url"]))
        pos = _num(r.get(pos_key), int) or (len(out) + 1)
        out.append({"position": pos, "url": r["_url"], "domain": domain_of(r["_url"]),
                    "title": _clean(r.get("title", "")),
                    "slot": _num(r.get(slot_key), int) if slot_key else None})
        if len(out) >= top:
            break
    return out


def parse_serpapi(data, top=DEFAULT_TOP, source=""):
    """One SerpApi result JSON -> record {"query", "engine", "engine_key", "source",
    "parse_mode", "organic", "local_pack", "features"}."""
    params = data.get("search_parameters") or {}
    query = _clean(params.get("q") or (data.get("search_information") or {})
                   .get("query_displayed") or "")
    raw_engine = str(params.get("engine") or "google").lower()
    engine_key = "google" if raw_engine.startswith("google") else raw_engine
    engine = "%s (SerpApi export)" % _SERPAPI_ENGINES.get(raw_engine, raw_engine)

    rows = []
    for r in data.get("organic_results") or []:
        if isinstance(r, dict):
            rows.append(dict(r, _url=r.get("link") or ""))
    organic = _organic_from(rows, top, "position")

    lr = data.get("local_results")
    places = lr.get("places") if isinstance(lr, dict) else lr if isinstance(lr, list) else []
    local = []
    for p in places or []:
        if not isinstance(p, dict) or not p.get("title"):
            continue
        site = p.get("website") or (p.get("links") or {}).get("website") or ""
        local.append({"position": _num(p.get("position"), int) or len(local) + 1,
                      "title": _clean(p["title"]), "rating": _num(p.get("rating"), float),
                      "reviews": _num(p.get("reviews"), int), "domain": domain_of(site)})

    features = []
    for key, kind in _SERPAPI_FEATURES:
        items = data.get(key)
        for it in items if isinstance(items, list) else []:
            link = isinstance(it, dict) and (it.get("link") or it.get("url"))
            if link and _is_result_url(link):
                features.append({"kind": kind, "url": link, "domain": domain_of(link),
                                 "title": _clean(it.get("title", ""))[:160]})
    kg = data.get("knowledge_graph")
    if isinstance(kg, dict) and _is_result_url(kg.get("website") or ""):
        features.append({"kind": "knowledge_graph", "url": kg["website"],
                         "domain": domain_of(kg["website"]), "title": _clean(kg.get("title", ""))})
    return {"query": query, "engine": engine, "engine_key": engine_key, "source": source,
            "parse_mode": "serpapi", "organic": organic, "local_pack": local,
            "features": features}


def _dfs_record(res, tdata, top, source):
    query = _clean(res.get("keyword") or tdata.get("keyword") or "")
    se = str(tdata.get("se") or res.get("se_domain") or "google").lower()
    engine_key = "google" if "google" in se else se.split(".")[0]
    engine = "%s (DataForSEO export)" % ("Google" if engine_key == "google"
                                         else engine_key.capitalize())
    items = [it for it in res.get("items") or [] if isinstance(it, dict)]
    rows = [dict(it, _url=it.get("url") or "") for it in items if it.get("type") == "organic"]
    organic = _organic_from(rows, top, "rank_group", "rank_absolute")

    local, features = [], []
    for it in sorted(items, key=lambda x: _num(x.get("rank_absolute"), int) or 10 ** 6):
        typ = it.get("type")
        if typ == "organic":
            continue
        if typ in ("local_pack", "maps_search"):
            if not it.get("title"):
                continue
            rating = it.get("rating") if isinstance(it.get("rating"), dict) else {}
            local.append({"position": _num(it.get("rank_group"), int) or len(local) + 1,
                          "title": _clean(it["title"]),
                          "rating": _num(rating.get("value"), float),
                          "reviews": _num(rating.get("votes_count"), int),
                          "domain": normalize_domain(it.get("domain") or "")
                          or domain_of(it.get("url") or "")})
            continue
        kind = _DFS_FEATURE_KINDS.get(typ, str(typ))
        nested = it.get("items") if isinstance(it.get("items"), list) else []
        for sub in [it] + [n for n in nested if isinstance(n, dict)]:
            link = sub.get("url") or ""
            if _is_result_url(link):
                features.append({"kind": kind, "url": link, "domain": domain_of(link),
                                 "title": _clean(sub.get("title", ""))[:160]})
    return {"query": query, "engine": engine, "engine_key": engine_key, "source": source,
            "parse_mode": "dataforseo", "organic": organic, "local_pack": local,
            "features": features}


def parse_dataforseo(data, top=DEFAULT_TOP, source="", errors=None):
    """A DataForSEO SERP response (or one bare result object) -> list of records, one per
    task result. Tasks without results are reported through ``errors`` when given."""
    if "tasks" not in data:
        return [_dfs_record(data, {}, top, source)]
    records = []
    for n, task in enumerate(data.get("tasks") or [], 1):
        if not isinstance(task, dict):
            continue
        tdata = task.get("data") or {}
        results = task.get("result") or []
        if not results and errors is not None:
            errors.append("%s: task %d (%s) has no result: %s" % (
                source, n, tdata.get("keyword", "?"),
                task.get("status_message") or "empty"))
        for res in results:
            if isinstance(res, dict):
                records.append(_dfs_record(res, tdata, top, source))
    return records


def load_serp_file(path, top=DEFAULT_TOP):
    """Read one --serp-json file. Returns (records, errors); never raises."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        return [], ["%s: could not read JSON (%s); skipped" % (path, e)]
    items = data if isinstance(data, list) else [data]
    records, errors = [], []
    for n, item in enumerate(items, 1):
        where = path if len(items) == 1 else "%s (item %d)" % (path, n)
        fmt = detect_serp_format(item)
        if fmt == "serpapi":
            records.append(parse_serpapi(item, top, path))
        elif fmt == "dataforseo":
            records.extend(parse_dataforseo(item, top, path, errors))
        else:
            errors.append("%s: not a recognized SERP export (expected SerpApi "
                          "organic_results/search_parameters or DataForSEO "
                          "tasks[].result[].items[]); skipped" % where)
    return records, errors


# =====================================================================================
# Tally and report
# =====================================================================================
def _qkey(query):
    return _clean(query).casefold()


def visibility_score(positions):
    """Sum of 1/position over every appearance (positions start at 1)."""
    return sum(1.0 / p for p in positions if p and p > 0)


def tally_domains(records):
    """{domain: stats} over every organic result of every record. stats has queries (in
    first-seen order), n_queries, appearances, best_pos, best_url, avg_pos, score, urls,
    and positions ({query: [positions]})."""
    stats = {}
    for rec in records:
        q = rec["query"]
        for r in rec.get("organic") or []:
            d = r.get("domain")
            pos = r.get("position")
            if not d or not pos:
                continue
            s = stats.setdefault(d, {"domain": d, "queries": [], "positions": {}, "urls": [],
                                     "best_pos": None, "best_url": "", "_all": []})
            if q not in s["positions"]:
                s["queries"].append(q)
                s["positions"][q] = []
            s["positions"][q].append(pos)
            s["_all"].append(pos)
            if r["url"] not in s["urls"]:
                s["urls"].append(r["url"])
            if s["best_pos"] is None or pos < s["best_pos"]:
                s["best_pos"], s["best_url"] = pos, r["url"]
    for s in stats.values():
        allp = s.pop("_all")
        s["n_queries"] = len(s["queries"])
        s["appearances"] = len(allp)
        s["avg_pos"] = round(sum(allp) / len(allp), 1)
        s["score"] = round(visibility_score(allp), 3)
    return stats


def _rank_key(s):
    return (-s["score"], -s["n_queries"], s["best_pos"] or 10 ** 6, s["domain"])


def tally_local(records, own=()):
    """Local-pack entries across queries, keyed by business name."""
    agg = {}
    for rec in records:
        for e in rec.get("local_pack") or []:
            key = e["title"].casefold()
            a = agg.setdefault(key, {"title": e["title"], "domain": "", "queries": [],
                                     "best_pos": None, "rating": None, "reviews": None,
                                     "engine": rec["engine"]})
            if rec["query"] not in a["queries"]:
                a["queries"].append(rec["query"])
            if a["best_pos"] is None or e["position"] < a["best_pos"]:
                a["best_pos"] = e["position"]
            a["domain"] = a["domain"] or e.get("domain") or ""
            if e.get("rating") is not None:
                a["rating"] = e["rating"]
            if e.get("reviews") is not None:
                a["reviews"] = max(a["reviews"] or 0, e["reviews"])
    out = list(agg.values())
    for a in out:
        a["n_queries"] = len(a["queries"])
        a["own"] = any(_host_matches(a["domain"], o) for o in own) if a["domain"] else False
    out.sort(key=lambda a: (-a["n_queries"], a["best_pos"] or 10 ** 6, a["title"].casefold()))
    return out


def tally_features(records):
    """{domain: {"kinds": {kind: count}, "queries": [...]}} for SERP features."""
    agg = {}
    for rec in records:
        for f in rec.get("features") or []:
            d = f.get("domain")
            if not d:
                continue
            a = agg.setdefault(d, {"domain": d, "kinds": {}, "queries": []})
            a["kinds"][f["kind"]] = a["kinds"].get(f["kind"], 0) + 1
            if rec["query"] not in a["queries"]:
                a["queries"].append(rec["query"])
    return agg


def build_report(records, client, owned=(), requested=None, overrides=None,
                 shortlist_size=DEFAULT_SHORTLIST, date=None, top=DEFAULT_TOP,
                 stopped=None, errors=None):
    """Assemble everything render_markdown and the JSON output need.

    ``requested`` is the list of queries asked for (defaults to the records' queries). A
    query counts as captured when its record has organic or local-pack results."""
    client = normalize_domain(client)
    owned = [d for d in (normalize_domain(x) for x in owned or ()) if d and d != client]
    own = [client] + owned
    captured = [r for r in records if r.get("organic") or r.get("local_pack")]
    if requested is None:
        requested = [r["query"] for r in records]
    cap_keys = {_qkey(r["query"]) for r in captured}
    not_captured = [q for q in requested if _qkey(q) not in cap_keys]

    rows = sorted(tally_domains(captured).values(), key=_rank_key)
    by_cat = {}
    for s in rows:
        s["category"], s["basis"] = classify_domain(s["domain"], own, overrides)
        if s["category"] == "own":
            s["role"] = "client" if _host_matches(s["domain"], client) else "owned"
        by_cat.setdefault(s["category"], []).append(s["domain"])
    shortlist = [s for s in rows if s["category"] == "business"][:shortlist_size]

    own_rows = [s for s in rows if s["category"] == "own"]
    for od in own:
        if not any(_host_matches(s["domain"], od) for s in own_rows):
            own_rows.append({"domain": od, "role": "client" if od == client else "owned",
                             "queries": [], "n_queries": 0, "appearances": 0,
                             "best_pos": None, "avg_pos": None, "score": 0.0,
                             "positions": {}, "urls": [], "best_url": ""})

    engines = []
    for r in captured:
        if r["engine"] not in engines:
            engines.append(r["engine"])
    feats = tally_features(captured)
    for d, a in feats.items():
        a["category"] = classify_domain(d, own, overrides)[0]
    return {
        "tool": "find_competitors.py", "version": VERSION,
        "date": date or _dt.date.today().isoformat(),
        "generated": _dt.datetime.now().isoformat(timespec="seconds"),
        "client": client, "owned": owned,
        "engines": engines,
        "all_google": bool(captured) and all(r.get("engine_key") == "google" for r in captured),
        "caveat": PROXY_CAVEAT,
        "queries_requested": len(requested), "queries_captured": len(captured),
        "requested_queries": list(requested), "not_captured": not_captured,
        "stopped": stopped, "errors": list(errors or []),
        "top": top, "shortlist_size": shortlist_size,
        "score_formula": "sum over every organic appearance of 1/position",
        "shortlist": shortlist,
        "by_category": by_cat,
        "own": own_rows,
        "domains": rows,
        "local_pack": tally_local(captured, own),
        "features": sorted(feats.values(), key=lambda a: (-sum(a["kinds"].values()),
                                                          a["domain"])),
        "queries": captured,
    }


# =====================================================================================
# Markdown
# =====================================================================================
def _cell(value):
    return str("-" if value is None or value == "" else value).replace("|", "\\|") \
        .replace("\n", " ")


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(_cell(c) for c in r) + " |")
    return out


def _stat_row(s, total):
    return [s["domain"], "%d/%d" % (s["n_queries"], total), s["best_pos"],
            "%.1f" % s["avg_pos"] if s["avg_pos"] is not None else None,
            "%.3f" % s["score"], s["best_url"]]


def _engine_sentence(report):
    engines = ", ".join(report["engines"]) or "no engine"
    if report["all_google"]:
        return ("Positions below came from Google through a provider export (%s), for the "
                "location and moment the provider captured." % engines)
    if any("google" in e.lower() for e in report["engines"]):
        return ("Positions below mix engines (%s), and the tallies combine them, so read the "
                "combined numbers as a proxy." % engines)
    return "Positions below came from %s, so read them as a proxy for Google." % engines


def render_markdown(report):
    """The human report. Opens with engine, date, queries requested vs captured and the
    proxy caveat, then the shortlist and the supporting tables."""
    total = report["queries_captured"]
    client = report["client"]
    own_all = [client] + report["owned"]
    short_rank = {s["domain"]: i for i, s in enumerate(report["shortlist"], 1)}
    L = ["# Competitor discovery: %s" % client, ""]
    L.append("Engine: %s. Date: %s. Queries: %d requested, %d captured." % (
        "; ".join(report["engines"]) or "none", report["date"],
        report["queries_requested"], total))
    L += ["", PROXY_CAVEAT + " " + _engine_sentence(report), ""]
    if report["stopped"]:
        L += ["Capture stopped early. " + report["stopped"], ""]
    if report["not_captured"]:
        L += ["Not captured: " + "; ".join(report["not_captured"]) + ".", ""]
    if report["errors"]:
        L += ["Input problems:", ""] + ["- " + e for e in report["errors"]] + [""]
    L += ["Own domains, excluded from the shortlist: %s." % ", ".join(
        "%s (%s)" % (d, "client" if d == client else "owned") for d in own_all), ""]

    L += ["## Competitor shortlist", ""]
    L.append("The top %d business domains by visibility score, the sum of 1/position over "
             "every organic result a domain holds (position 1 = 1.0, position 4 = 0.25, "
             "position 10 = 0.1). It rewards ranking high and ranking for many queries. "
             "Business only means the domain matched no marketplace, social, reference, "
             "directory or news rule, so blogs and affiliate sites can land here; check "
             "each one before auditing it." % report["shortlist_size"])
    L.append("")
    if report["shortlist"]:
        L += _table(["#", "Domain", "Queries", "Best pos", "Avg pos", "Score", "Example URL"],
                    [[i] + _stat_row(s, total) for i, s in enumerate(report["shortlist"], 1)])
        L += ["", "Audit next:", "", "```"]
        for s in report["shortlist"]:
            parts = urllib.parse.urlsplit(s["best_url"])
            L.append("python3 scripts/seo_audit.py %s://%s --json" % (parts.scheme or "https",
                                                                      parts.netloc or s["domain"]))
        L += ["```"]
    else:
        L.append("No business domain appeared in the captured results.")
    L.append("")

    rows = report["domains"]

    def section(title, intro, cats, with_basis=False):
        picked = [s for s in rows if s["category"] in cats]
        L.extend(["## " + title, "", intro, ""])
        if not picked:
            L.extend(["None in the captured results.", ""])
            return
        headers = ["Domain", "Queries", "Best pos", "Avg pos", "Score", "Example URL"]
        if with_basis:
            headers = ["Domain", "Class", "Basis"] + headers[1:]
        body = []
        for s in picked:
            r = _stat_row(s, total)
            if with_basis:
                r = [r[0], CATEGORY_LABELS.get(s["category"], s["category"]), s["basis"]] + r[1:]
            body.append(r)
        L.extend(_table(headers, body) + [""])

    section("Marketplaces and OTAs",
            "Channels to be listed on and ranked within, not competitors to audit.",
            ("marketplace",))
    section("Social and video",
            "Results the client could occupy with its own profiles, threads and videos.",
            ("social",))
    section("Directories, DMOs, news and reference",
            "Classified by heuristics (every .org host lands under directories, for "
            "example), so treat the class as a hint.",
            ("directory", "news", "reference"), with_basis=True)
    custom = sorted({s["category"] for s in rows} - set(CATEGORY_LABELS))
    if custom:
        section("Other categories (from --classify-file)", "Categories supplied by the user.",
                tuple(custom), with_basis=True)

    L += ["## Own domains", ""]
    L.append("The client and its other domains. A satellite domain ranking for the client's "
             "queries splits the client's own visibility.")
    L.append("")
    own_body = []
    for s in report["own"]:
        pos = "; ".join("%s: %s" % (q, ", ".join(str(p) for p in ps))
                        for q, ps in s["positions"].items())
        own_body.append([s["domain"], s["role"], "%d/%d" % (s["n_queries"], total),
                         s["best_pos"], "%.1f" % s["avg_pos"] if s["avg_pos"] else None,
                         "%.3f" % s["score"], pos or "not seen"])
    L += _table(["Domain", "Role", "Queries", "Best pos", "Avg pos", "Score",
                 "Positions by query"], own_body) + [""]

    # position matrix: client, other own hosts that appeared, then the shortlist
    cols, seen_cols = [], set()
    for s in report["own"]:
        if (s["role"] == "client" or s["n_queries"]) and s["domain"] not in seen_cols:
            cols.append(s)
            seen_cols.add(s["domain"])
    cols += report["shortlist"]
    L += ["## Positions by query", ""]
    L.append("Organic positions (up to %d per query) of the own domains and the shortlist; "
             "\"-\" means not in the captured results." % report["top"])
    L.append("")
    matrix = []
    for rec in report["queries"]:
        row = [rec["query"]]
        for s in cols:
            ps = s["positions"].get(rec["query"]) or []
            row.append(", ".join(str(p) for p in ps) or "-")
        matrix.append(row)
    L += _table(["Query"] + [s["domain"] for s in cols], matrix) + [""]

    L += ["## Top 10 by query", ""]
    L.append("Tags: (client), (own), and (S1) to (S%d) for shortlist rank."
             % max(1, len(report["shortlist"])))
    L.append("")

    def tag(d):
        if _host_matches(d, client):
            return d + " (client)"
        if any(_host_matches(d, o) for o in report["owned"]):
            return d + " (own)"
        if d in short_rank:
            return "%s (S%d)" % (d, short_rank[d])
        return d

    top_rows = []
    for rec in report["queries"]:
        doms = [tag(r["domain"]) for r in rec["organic"][:10]]
        top_rows.append([rec["query"]] + doms + [""] * (10 - len(doms)))
    L += _table(["Query"] + [str(i) for i in range(1, 11)], top_rows) + [""]

    if report["local_pack"]:
        L += ["## Local pack", ""]
        intro = "Map/places results, kept apart from organic positions and not scored."
        if any(r.get("engine_key") == "brave" for r in report["queries"]):
            intro += (" Brave's places block is not Google's local pack; only a Google "
                      "export shows the real one.")
        L.append(intro)
        L.append("")
        L += _table(["Business", "Website", "Queries", "Best pos", "Rating", "Reviews",
                     "Engine"],
                    [[a["title"] + (" (own)" if a["own"] else ""), a["domain"],
                      "%d/%d" % (a["n_queries"], total), a["best_pos"], a["rating"],
                      a["reviews"], a["engine"]] for a in report["local_pack"]]) + [""]

    if report["features"]:
        L += ["## SERP features", ""]
        L.append("Blocks outside the organic list: ads, entity and knowledge cards, video "
                 "carousels, discussions, People also ask sources. Not scored.")
        L.append("")
        L += _table(["Domain", "Class", "Features", "Queries"],
                    [[a["domain"], CATEGORY_LABELS.get(a["category"], a["category"]),
                      ", ".join("%s x%d" % (k, n) for k, n in sorted(a["kinds"].items())),
                      "%d/%d" % (len(a["queries"]), total)] for a in report["features"]]) + [""]

    L += ["## Method", ""]
    fallback = [r["query"] for r in report["queries"] if r.get("parse_mode") == "fallback"]
    L.append("Positions are organic ranks with ads, carousels and cards not counted, capped "
             "at %d per query. Domains are lowercased with www. stripped. Classification "
             "uses a small built-in table plus name heuristics; the classes are hints, and a "
             "--classify-file JSON corrects them for the next run." % report["top"])
    if any(r.get("engine_key") == "brave" for r in report["queries"]):
        L.append("")
        L.append("Brave localizes results to the requester's IP address, so a run from "
                 "outside the client's market can surface out-of-market businesses, most "
                 "visibly in the places block.")
    if fallback:
        L.append("")
        L.append("The Brave parser fell back to raw links for: %s. Check those rows by "
                 "hand." % "; ".join(fallback))
    L.append("")
    return "\n".join(L)


# =====================================================================================
# CLI
# =====================================================================================
def _read_queries(args):
    raw = list(args.queries or [])
    if args.queries_file:
        with open(args.queries_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    raw.append(line)
    out, seen = [], set()
    for q in raw:
        q = _clean(q)
        if q and _qkey(q) not in seen:
            seen.add(_qkey(q))
            out.append(q)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Discover which domains occupy the results for a client's queries and "
                    "write a ranked competitor shortlist (Markdown + JSON).")
    ap.add_argument("--client", required=True, help="Client domain, e.g. example.com")
    ap.add_argument("--owned", nargs="*", default=[],
                    help="Other domains the client owns (excluded from the shortlist)")
    qg = ap.add_mutually_exclusive_group()
    qg.add_argument("--queries", nargs="+", help="Queries the client wants to rank for")
    qg.add_argument("--queries-file", help="File with one query per line (# comments ok)")
    ap.add_argument("--engine", choices=["brave"], default="brave",
                    help="Engine fetched live when no --serp-json is given (default brave)")
    ap.add_argument("--serp-json", nargs="+", metavar="FILE",
                    help="SerpApi or DataForSEO JSON exports; nothing is fetched live")
    ap.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                    help="Seconds between live requests (default %g)" % DEFAULT_DELAY)
    ap.add_argument("--top", type=int, default=DEFAULT_TOP,
                    help="Organic results kept per query (default %d)" % DEFAULT_TOP)
    ap.add_argument("--shortlist", type=int, default=DEFAULT_SHORTLIST,
                    help="Business domains in the shortlist (default %d)" % DEFAULT_SHORTLIST)
    ap.add_argument("--classify-file", help="JSON overriding or extending the classes")
    ap.add_argument("--out", default=".", help="Output directory (default .)")
    ap.add_argument("--date", help="Report date YYYY-MM-DD (default today)")
    ap.add_argument("--version", action="version", version="find-competitors " + VERSION)
    args = ap.parse_args(argv)

    client = normalize_domain(args.client)
    if not client:
        ap.error("--client needs a domain")
    if args.top < 1 or args.shortlist < 1 or args.delay < 0:
        ap.error("--top and --shortlist must be at least 1 and --delay at least 0")
    date = args.date or _dt.date.today().isoformat()
    try:
        _dt.date.fromisoformat(date)
    except ValueError:
        ap.error("--date must be YYYY-MM-DD")
    try:
        queries = _read_queries(args)
    except OSError as e:
        ap.error("cannot read --queries-file: %s" % e)
    if not queries and not args.serp_json:
        ap.error("give --queries, --queries-file or --serp-json")
    overrides = None
    if args.classify_file:
        try:
            overrides = load_classify_file(args.classify_file)
        except (OSError, ValueError) as e:
            ap.error("cannot use --classify-file: %s" % e)

    errors, stopped = [], None
    if args.serp_json:
        records, seen = [], set()
        for path in args.serp_json:
            recs, errs = load_serp_file(path, args.top)
            for e in errs:
                print("error: " + e, file=sys.stderr)
            errors.extend(errs)
            for r in recs:
                if not r["query"]:
                    r["query"] = os.path.splitext(os.path.basename(path))[0]
                if _qkey(r["query"]) in seen:
                    print("note: %s: query %r already loaded from another file; kept the "
                          "first" % (path, r["query"]), file=sys.stderr)
                    continue
                seen.add(_qkey(r["query"]))
                records.append(r)
        if queries:
            wanted = {_qkey(q) for q in queries}
            dropped = [r["query"] for r in records if _qkey(r["query"]) not in wanted]
            if dropped:
                print("note: ignoring exported queries not in --queries: " +
                      "; ".join(dropped), file=sys.stderr)
            records = [r for r in records if _qkey(r["query"]) in wanted]
            requested = queries
        else:
            requested = [r["query"] for r in records]
    else:
        print("find-competitors %s: %d queries on Brave, %gs apart" % (
            VERSION, len(queries), args.delay), file=sys.stderr)
        records, status = fetch_brave_serps(queries, args.delay, args.top)
        stopped = status["stopped"]
        requested = queries
        if stopped:
            print(stopped, file=sys.stderr)

    report = build_report(records, client, args.owned, requested=requested,
                          overrides=overrides, shortlist_size=args.shortlist, date=date,
                          top=args.top, stopped=stopped, errors=errors)
    if not report["queries_captured"]:
        print("error: no query was captured (%d requested); nothing written."
              % report["queries_requested"], file=sys.stderr)
        raise SystemExit(1)

    stem = os.path.join(args.out, "competitors-%s-%s" % (client, date))
    md_path, json_path = stem + ".md", stem + ".json"
    try:
        os.makedirs(args.out, exist_ok=True)
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(report))
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False, default=str)
    except OSError as e:
        print("error: cannot write to %s (%s)" % (args.out, e.strerror or e), file=sys.stderr)
        raise SystemExit(1)

    total = report["queries_captured"]
    print("Wrote %s" % md_path)
    print("Wrote %s" % json_path)
    print("Engine: %s. Queries: %d requested, %d captured." % (
        "; ".join(report["engines"]), report["queries_requested"], total))
    print(PROXY_CAVEAT)
    if stopped:
        print("Stopped early: " + stopped)
    print("\nShortlist (business domains by visibility score):")
    if not report["shortlist"]:
        print("  none")
    width = max([len(s["domain"]) for s in report["shortlist"]] + [6])
    for i, s in enumerate(report["shortlist"], 1):
        print("  %2d. %-*s  %d/%d queries  best %-3s avg %-5.1f score %.3f" % (
            i, width, s["domain"], s["n_queries"], total, s["best_pos"], s["avg_pos"],
            s["score"]))
    return 0


if __name__ == "__main__":
    main()
