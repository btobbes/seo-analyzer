#!/usr/bin/env python3
"""
seo_compare.py: compare one client audit with one or more competitor audits.

seo_audit.py --json writes one report per site. This script reads the client's report
and each rival's, and writes a side-by-side comparison: Markdown always, plus JSON
(--json) and a self-contained HTML page (--html). The point is to show what each rival
has that the client lacks, and the reverse, from data the audits already collected.

It deliberately does not crown a winner by overall score. The overall score is a
deduction model: every check that runs can cost points, so a ten-page site with no
sitemap has far less to fail than a thousand-page site and can outscore it while being
thinner in every way that matters. The comparison leads with category scores and a
matrix of concrete features that are present or absent, and says plainly when the
overall scores should not be compared at all.

Usage (from the repo root):
    python3 scripts/seo_compare.py CLIENT.json RIVAL.json [RIVAL2.json ...]
                                   [--out DIR] [--json] [--html] [--date YYYY-MM-DD]

Standard library only. load_report and compare_reports build a plain dict;
render_markdown and render_html only format it.
"""

import argparse
import datetime as _dt
import html as _html
import json
import os
import re
import sys

# Same order as seo_audit.CATEGORY_WEIGHTS. Kept local so this script stands alone;
# any category a newer report adds is appended after these.
CATEGORY_ORDER = ["crawlability", "on page", "performance", "ai search", "schema",
                  "trust", "mobile", "social"]
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
PAGE_RATIO_LIMIT = 3.0
TOP_TERMS = 12
COVERAGE_ROWS_SHOWN = 20

CAVEAT = ("Overall scores are NOT comparable across sites when page counts differ by more "
          "than 3x or when one site has no sitemap, because a tiny site scores well by having "
          "nothing to check. Compare category by category and read the absent features "
          "section instead.")

# Free-text health values: these markers mean "absent" or "could not tell".
_ABSENT_MARKERS = ("✗", "none", "not found", "not present")
_UNKNOWN_MARKERS = ("not checked", "not tested", "could not")

# Organization, LocalBusiness and the subtypes a small business is likely to use. Any
# type ending in "Business" or "Organization" also counts, which covers most of the
# schema.org tree; the names listed here are the subtypes that don't follow that rule.
ORG_TYPES = {"Organization", "Corporation", "NGO", "LocalBusiness", "TravelAgency",
             "TouristInformationCenter", "Restaurant", "FoodEstablishment", "Store", "Hotel",
             "ProfessionalService", "LegalService", "FinancialService", "RealEstateAgent",
             "Dentist", "Physician", "ExerciseGym"}

# (key, label shown in the matrix, phrase for the "has / lacks" lists or None when the
# row is informational and never feeds those lists).
FEATURE_ROWS = [
    ("robots_txt", "robots.txt found", "a robots.txt file"),
    ("sitemap", "XML sitemap found", "an XML sitemap"),
    ("sitemap_urls", "Sitemap URL count", None),
    ("jsonld_home", "JSON-LD on homepage", "JSON-LD structured data on the homepage"),
    ("schema_home", "Schema types on homepage", None),
    ("org_home", "Organization or LocalBusiness type on homepage",
     "Organization or LocalBusiness schema on the homepage"),
    ("breadcrumb", "BreadcrumbList on any deep page", "BreadcrumbList schema"),
    ("faq", "FAQPage on any deep page", "FAQPage schema"),
    ("analytics", "Analytics detected", "an analytics tag"),
    ("contact", "Contact route (page, tel:, mailto:)", "a contact route"),
    ("about", "About page linked", "a linked About page"),
    ("privacy", "Privacy page linked", "a linked privacy policy"),
    ("terms", "Terms page linked", "a linked terms page"),
    ("postal", "Postal address published", "a published postal address"),
    ("legal", "Legal entity named", "a named legal entity"),
    ("og_image", "Open Graph image on homepage", "an Open Graph share image"),
    ("ai_edge", "AI crawlers served at the edge", "AI crawler access at the edge (no agent refused)"),
    ("llms", "llms.txt", "an llms.txt file"),
    ("hsts", "HSTS header", "an HSTS header"),
    ("tls_days", "TLS certificate days left", None),
    ("home_words", "Homepage word count", None),
    ("home_title", "Homepage title", "a homepage title"),
    ("home_title_len", "Homepage title length", None),
    ("home_desc_len", "Homepage meta description length", "a homepage meta description"),
    ("home_h1", "Homepage H1", "a homepage H1"),
    ("client_rendered", "Deep pages rendered client-side", None),
    ("social", "Social profile links", "links to social profiles"),
    ("sameas", "sameAs declared in schema", "sameAs links in schema"),
    ("google", "Google Maps or Business Profile links", "Google Maps or Business Profile links"),
    ("kw_primary", "Primary keyword", None),
    ("kw_top5", "Top 5 keywords", None),
]

# Health rows whose raw text is reproduced under the matrix.
HEALTH_RAW_LABELS = ["Analytics", "Contact & trust pages", "Entity transparency",
                     "AI crawler edge access", "llms.txt", "HSTS", "TLS certificate"]

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


# =================================================================================
# Loading and validation
# =================================================================================
def _check_report(r, source="report"):
    """Raise ValueError unless r looks like a seo_audit.py report."""
    if not isinstance(r, dict):
        raise ValueError(f"{source}: expected a JSON object from seo_audit.py --json, "
                         f"got {type(r).__name__}")
    if not isinstance(r.get("domain"), str) or not r["domain"].strip():
        raise ValueError(f"{source}: missing 'domain'; is this a seo_audit.py --json report?")
    return r


def load_report(path):
    """Read and validate one report JSON. Raises ValueError on any malformed input."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as e:
        raise ValueError(f"{path}: cannot read ({e.strerror or e})") from None
    except ValueError as e:              # JSONDecodeError and UnicodeDecodeError
        raise ValueError(f"{path}: not valid JSON ({e})") from None
    return _check_report(data, path)


# =================================================================================
# Small defensive accessors (older reports lack keys; never crash on them)
# =================================================================================
def _as_list(v):
    return v if isinstance(v, list) else []


def _as_dict(v):
    return v if isinstance(v, dict) else {}


def _int(v):
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _bool(v):
    return v if isinstance(v, bool) else None


def _num(v):
    return "n/a" if v is None else f"{v:,}"


def _yn(present, detail=None):
    if present is None:
        return "n/a"
    word = "yes" if present else "no"
    return f"{word} ({detail})" if detail else word


def _cell(value, present=None, raw=None):
    return {"value": value, "present": present, "raw": raw}


def _pages(r):
    return [p for p in _as_list(r.get("pages")) if isinstance(p, dict)]


def _homepage(r):
    pages = _pages(r)
    return next((p for p in pages if p.get("source") == "homepage"), pages[0] if pages else None)


def _types(page):
    return sorted({t for t in _as_list(page.get("schema_types")) if isinstance(t, str)})


def _is_org_type(t):
    return t in ORG_TYPES or t.endswith(("Business", "Organization"))


def _health(r, label):
    """Raw text of the health row with this label, or None."""
    for row in _as_list(r.get("health")):
        if isinstance(row, (list, tuple)) and len(row) >= 2 and row[0] == label:
            return str(row[1])
    return None


def _unknown(text):
    return text is None or any(m in text.lower() for m in _UNKNOWN_MARKERS)


def health_flag(text):
    """True (present), False (absent) or None (unknown) for a free-text health value.

    "GA4 (gtag.js)" is present; "none detected", "not present (...)" or anything with a
    cross mark is absent; "could not inspect" or a missing row is unknown."""
    if _unknown(text):
        return None
    low = text.lower()
    return not any(m in low for m in _ABSENT_MARKERS)


def tick(text, label):
    """Read "label ✓" or "label ✗" out of a health value: True, False or None."""
    if _unknown(text):
        return None
    m = re.search(re.escape(label) + r"\s*([✓✗])", text)
    return None if not m else m.group(1) == "✓"


def _top_terms(r, n=TOP_TERMS):
    terms = _as_list(_as_dict(r.get("keywords")).get("terms"))
    return [str(t["term"]) for t in terms if isinstance(t, dict) and t.get("term")][:n]


def _sitemap_found(r):
    if isinstance(r.get("sitemap_found"), bool):
        return r["sitemap_found"]
    n = _int(r.get("sitemap_url_count"))
    return None if n is None else n > 0


# =================================================================================
# Per-site facts and features
# =================================================================================
def site_facts(r):
    """The header numbers for one site."""
    pc, sc = _int(r.get("page_count")), _int(r.get("sweep_count"))
    return {
        "domain": r["domain"],
        "version": r.get("version") or None,
        "generated": str(r["generated"])[:10] if r.get("generated") else None,
        "page_count": pc,
        "sweep_count": sc,
        "pages_audited": None if pc is None else pc + (sc or 0),
        "sitemap_url_count": _int(r.get("sitemap_url_count")),
        "sitemap_found": _sitemap_found(r),
        "confidence": r.get("confidence") or None,
        "overall": _int(r.get("overall")),
    }


def _crawl_features(r, pages):
    robots, sm = _bool(r.get("robots_found")), _sitemap_found(r)
    cr = sum(1 for p in pages if p.get("client_rendered") is True)
    return {
        "robots_txt": _cell(_yn(robots), robots),
        "sitemap": _cell(_yn(sm), sm),
        "sitemap_urls": _cell(_num(_int(r.get("sitemap_url_count")))),
        "client_rendered": _cell(f"{cr} of {len(pages)}" if pages else "n/a"),
    }


def _schema_features(home, pages):
    if home is None:
        out = {k: _cell("n/a") for k in ("jsonld_home", "schema_home", "org_home")}
    else:
        types = _types(home)
        org = [t for t in types if _is_org_type(t)]
        out = {
            "jsonld_home": _cell(_yn(bool(types)), bool(types)),
            "schema_home": _cell(", ".join(types) or "none"),
            "org_home": _cell(_yn(bool(org), ", ".join(org)), bool(org)),
        }
    for key, schema_type in (("breadcrumb", "BreadcrumbList"), ("faq", "FAQPage")):
        n = sum(1 for p in pages if schema_type in _types(p))
        out[key] = (_cell(_yn(n > 0, f"{n} of {len(pages)} pages"), n > 0) if pages
                    else _cell("n/a"))
    return out


def _health_features(r):
    out = {}
    for key, label in (("analytics", "Analytics"), ("llms", "llms.txt")):
        raw = _health(r, label)
        out[key] = _cell(_yn(health_flag(raw)), health_flag(raw), raw)

    trust = _health(r, "Contact & trust pages")
    contact = None if _unknown(trust) else "no contact" not in trust.split("·")[0].lower()
    out["contact"] = _cell(_yn(contact), contact, trust)
    for key in ("about", "privacy", "terms"):
        flag = tick(trust, key)
        out[key] = _cell(_yn(flag), flag, trust)

    entity = _health(r, "Entity transparency")
    for key, label in (("postal", "postal address"), ("legal", "legal entity named")):
        flag = tick(entity, label)
        out[key] = _cell(_yn(flag), flag, entity)

    # seo_audit omits the HSTS row when the header is not sent, and also when the homepage
    # never answered (error, block or challenge page), so a missing row means "no HSTS"
    # only when the report has health rows and its homepage returned 200. Otherwise the
    # row is unknown, not absent.
    hsts = _health(r, "HSTS")
    home = _homepage(r)
    fetched = home is not None and _int(home.get("status")) == 200
    flag = (health_flag(hsts) if hsts is not None
            else (False if (_as_list(r.get("health")) and fetched) else None))
    out["hsts"] = _cell(_yn(flag), flag, hsts)

    tls = _health(r, "TLS certificate")
    m = re.search(r"(-?\d+)\s+days?\s+left", tls or "")
    out["tls_days"] = _cell(m.group(1) if m else "n/a", None, tls)

    matrix = [m for m in _as_list(r.get("ai_matrix")) if isinstance(m, dict)]
    # seo_audit verdicts: "ok", "refused (N)", "challenged (N)", "pay-per-crawl (402)",
    # or "no response" / "HTTP N" when the auditor's own request failed. Only the
    # middle three are evidence that the site turns the agent away. startswith() keeps
    # the " on some pages" suffix working.
    def _v(m):
        return str(m.get("verdict") or "")
    refused = [str(m.get("agent")) for m in matrix
               if _v(m).startswith(("refused", "challenged", "pay-per-crawl"))]
    unknown = [str(m.get("agent")) for m in matrix
               if not _v(m).startswith(("ok", "refused", "challenged", "pay-per-crawl"))]
    ok_n = len(matrix) - len(refused) - len(unknown)
    if not matrix:
        value, present = "not tested", None
    else:
        value = f"{ok_n} ok, {len(refused)} refused"
        if refused:
            value += " (" + ", ".join(refused) + ")"
        if unknown:
            value += f", {len(unknown)} not answered (" + ", ".join(unknown) + ")"
        present = False if refused else (None if unknown else True)
    out["ai_edge"] = _cell(value, present, _health(r, "AI crawler edge access"))
    return out


def _homepage_features(home):
    keys = ("home_words", "home_title", "home_title_len", "home_desc_len", "home_h1")
    if home is None:
        return {k: _cell("n/a") for k in keys}
    out = {"home_words": _cell(_num(_int(home.get("word_count"))))}
    if "title" in home:
        title = str(home.get("title") or "")
        out["home_title"] = _cell(title or "none", bool(title))
        out["home_title_len"] = _cell(f"{len(title)} chars")
    else:
        out["home_title"], out["home_title_len"] = _cell("n/a"), _cell("n/a")
    if "description" in home:
        desc = str(home.get("description") or "")
        out["home_desc_len"] = _cell(f"{len(desc)} chars" if desc else "none", bool(desc))
    else:
        out["home_desc_len"] = _cell("n/a")
    if "h1" in home:
        h1 = [h for h in _as_list(home.get("h1")) if isinstance(h, str) and h.strip()]
        more = f" (+{len(h1) - 1} more)" if len(h1) > 1 else ""
        out["home_h1"] = _cell(h1[0] + more if h1 else "none", bool(h1))
    else:
        out["home_h1"] = _cell("n/a")
    return out


def _og_image(r):
    soc = r.get("social")
    if not isinstance(soc, dict) or not (soc.get("metrics") or soc.get("findings")):
        return _cell("n/a")
    dims = next((str(m[1]) for m in _as_list(soc.get("metrics"))
                 if isinstance(m, (list, tuple)) and len(m) >= 2 and m[0] == "Image dimensions"),
                None)
    missing = any(isinstance(fi, dict) and fi.get("title") == "No Open Graph image"
                  for fi in _as_list(soc.get("findings")))
    present = bool(dims) or not missing
    detail = dims or ("declared, not measured" if present else None)
    return _cell(_yn(present, detail), present)


def _footprint_features(r):
    fp = r.get("footprint")
    if not isinstance(fp, dict):
        return {k: _cell("n/a") for k in ("social", "sameas", "google")}
    profiles = [s for s in _as_list(fp.get("social")) if isinstance(s, dict) and s.get("url")]
    urls = {str(s["url"]) for s in profiles}
    platforms = sorted({str(s.get("platform") or "other") for s in profiles})
    social = _cell(f"{len(urls)} ({', '.join(platforms)})" if urls else "none", bool(urls))
    sameas = _bool(fp.get("sameas_present"))
    g = fp.get("google")
    if isinstance(g, dict):
        maps = {str(u) for u in _as_list(g.get("maps_links"))}
        gbp = {str(u) for u in _as_list(g.get("gbp_links"))}
        present = bool(maps or gbp)
        google = _cell(_yn(present, f"{len(maps)} Maps, {len(gbp)} Business Profile"
                                    if present else None), present)
    else:
        google = _cell("n/a")
    return {"social": social, "sameas": _cell(_yn(sameas), sameas), "google": google}


def _keyword_features(r):
    kw = _as_dict(r.get("keywords"))
    top5 = _top_terms(r, 5)
    return {"kw_primary": _cell(str(kw.get("primary") or "n/a")),
            "kw_top5": _cell(", ".join(top5) or "n/a")}


def site_features(r):
    """Every matrix cell for one site, keyed by FEATURE_ROWS key."""
    pages, home = _pages(r), _homepage(r)
    out = {}
    out.update(_crawl_features(r, pages))
    out.update(_schema_features(home, pages))
    out.update(_health_features(r))
    out.update(_homepage_features(home))
    out["og_image"] = _og_image(r)
    out.update(_footprint_features(r))
    out.update(_keyword_features(r))
    return out


# =================================================================================
# Findings
# =================================================================================
def _finding_key(category, title):
    """Match on (category, title) with numbers masked, so "Favicon is 191 KB" and
    "Favicon is 40 KB", or "(3 of 40 checked)" and "(7 of 11 checked)", are one finding."""
    return (str(category or ""), _NUM_RE.sub("#", str(title or "")))


def collect_findings(r):
    """Every distinct finding for a site from site_findings, sweep_summary, each deep
    page, the social and footprint blocks, and PageSpeed when it actually ran (its
    rate-limit notice describes the tool, not the site). Keyed by _finding_key."""
    psi = _as_dict(r.get("psi"))
    sources = [("site-wide", _as_list(r.get("site_findings"))),
               ("sweep", _as_list(r.get("sweep_summary"))),
               ("social", _as_list(_as_dict(r.get("social")).get("findings"))),
               ("footprint", _as_list(_as_dict(r.get("footprint")).get("findings"))),
               ("pagespeed", _as_list(psi.get("findings")) if psi.get("status") == "ok" else [])]
    sources += [("page", _as_list(p.get("findings"))) for p in _pages(r)]
    out = {}
    for where, items in sources:
        for fi in items:
            if not isinstance(fi, dict) or not fi.get("title"):
                continue
            key = _finding_key(fi.get("category"), fi["title"])
            sev = str(fi.get("severity") or "INFO").upper()
            e = out.setdefault(key, {"severity": sev, "category": key[0],
                                     "title": str(fi["title"]), "where": {}})
            if SEV_ORDER.get(sev, 5) < SEV_ORDER.get(e["severity"], 5):
                e["severity"] = sev
            n = _int(fi.get("count")) if where == "sweep" else 1
            e["where"][where] = e["where"].get(where, 0) + (n or 1)
    return out


def unique_findings(mine, others):
    """Entries of mine whose key appears in none of others, most severe first."""
    theirs = set()
    for o in others:
        theirs |= set(o)
    items = [v for k, v in mine.items() if k not in theirs]
    return sorted(items, key=lambda e: (SEV_ORDER.get(e["severity"], 5), e["category"], e["title"]))


def _where_text(where):
    parts = []
    for k in ("site-wide", "page", "sweep", "social", "footprint", "pagespeed"):
        n = where.get(k)
        if not n:
            continue
        if k == "page":
            parts.append(f"{n} deep page" + ("s" if n != 1 else ""))
        elif k == "sweep":
            parts.append(f"{n} swept page" + ("s" if n != 1 else ""))
        else:
            parts.append(k)
    return ", ".join(parts)


# =================================================================================
# Comparison
# =================================================================================
def score_caveat(sites):
    """Does the overall-score caveat apply? Compares pages audited (deep + swept, which is
    what the score is built from) between the client and each rival, and sitemaps."""
    client, ratios, reasons = sites[0], [], []
    for s in sites[1:]:
        a, b = client["pages_audited"], s["pages_audited"]
        ratio, over = None, False
        if a is not None and b is not None:
            if min(a, b) > 0:
                ratio = round(max(a, b) / min(a, b), 1)
                over = max(a, b) / min(a, b) > PAGE_RATIO_LIMIT
            else:
                over = max(a, b) > 0
        ratios.append({"rival": s["domain"], "client_pages": a, "rival_pages": b,
                       "ratio": ratio, "over_limit": over})
        if over:
            big, small = (client, s) if a >= b else (s, client)
            reasons.append(f"{small['domain']} had no pages audited" if ratio is None else
                           f"{big['domain']} had {ratio}x as many pages audited as "
                           f"{small['domain']} ({big['pages_audited']:,} vs {small['pages_audited']:,})")
    reasons += [f"{s['domain']} has no sitemap" for s in sites if s["sitemap_found"] is False]
    unknown = list(dict.fromkeys(
        [f"{s['domain']} has no page counts" for s in sites if s["pages_audited"] is None]
        + [f"{s['domain']} has no sitemap status" for s in sites if s["sitemap_found"] is None]))
    versions = sorted({s["version"] for s in sites if s["version"]})
    version_note = (f"The reports come from different seo_audit versions ({', '.join(versions)}); "
                    "some differences may come from changed checks rather than the sites."
                    if len(versions) > 1 else None)
    # applies: True, False, or None when missing data means it cannot be decided.
    applies = True if reasons else (None if unknown else False)
    return {"applies": applies, "reasons": reasons, "unknown": unknown, "ratios": ratios,
            "text": CAVEAT, "version_note": version_note}


def category_table(reports):
    """One row per category: every site's score, client minus best rival, client rank."""
    extra = sorted({c for r in reports for c in _as_dict(r.get("scores"))} - set(CATEGORY_ORDER))
    rows = []
    for cat in CATEGORY_ORDER + extra:
        vals = [_int(_as_dict(r.get("scores")).get(cat)) for r in reports]
        mine, rivals = vals[0], [v for v in vals[1:] if v is not None]
        rows.append({
            "category": cat,
            "scores": vals,
            "gap": mine - max(rivals) if mine is not None and rivals else None,
            "rank": 1 + sum(1 for v in rivals if v > mine) if mine is not None else None,
            "ranked": sum(1 for v in vals if v is not None),
        })
    return rows


def keyword_overlap(client, rival):
    a, b = _top_terms(client), _top_terms(rival)
    return {"rival": rival["domain"], "available": bool(a) and bool(b),
            "rival_only": [t for t in b if t not in a],
            "client_only": [t for t in a if t not in b],
            "shared": [t for t in a if t in b]}


def coverage_of(r):
    cov = r.get("coverage")
    rows = [{"template": str(c.get("template")), "urls": _int(c.get("urls")),
             "audited": _int(c.get("audited"))}
            for c in _as_list(cov) if isinstance(c, dict)]
    return {"domain": r["domain"], "templates": rows,
            "crawl_only": (not rows) if isinstance(cov, list) else None,
            "gaps": [str(g) for g in _as_list(r.get("coverage_gaps"))],
            "page_count": _int(r.get("page_count"))}


def advantages(features, n_sites):
    """From the yes/no rows only: what each rival has that the client lacks, and back."""
    out = []
    for i in range(1, n_sites):
        def has(a, b):
            return [row["phrase"] for row in features if row["phrase"]
                    and row["cells"][a]["present"] is True and row["cells"][b]["present"] is False]
        out.append({"rival_index": i, "rival_has": has(i, 0), "client_has": has(0, i)})
    return out


def _summary(cmp):
    sites, cav = cmp["sites"], cmp["caveat"]
    parts = []
    for i, s in enumerate(sites):
        who = s["domain"] + (" (client)" if i == 0 else "")
        score = f"scored {s['overall']}/100" if s["overall"] is not None else "has no overall score"
        pages = f" on {_num(s['pages_audited'])} audited pages" if s["pages_audited"] is not None else ""
        parts.append(who + " " + score + pages)
    text = "; ".join(parts) + "."
    if cav["applies"]:
        text += (" The overall scores are not comparable here, so compare by category and by "
                 "absent features: " + "; ".join(cav["reasons"]) + ".")
    elif cav["applies"] is None:
        text += (" Whether the overall scores are comparable cannot be checked ("
                 + "; ".join(cav["unknown"]) + "), so treat them as not comparable.")
    else:
        text += (" Page counts are within 3x and every site has a sitemap, so the overall "
                 "scores are roughly comparable; the category scores are still the better guide.")
    for adv in cmp["advantages"]:
        i = adv["rival_index"]
        pairs = [(row["scores"][0], row["scores"][i]) for row in cmp["categories"]
                 if row["scores"][0] is not None and row["scores"][i] is not None]
        text += f" Against {sites[i]['domain']}, "
        if pairs:
            text += (f"the client leads in {sum(1 for a, b in pairs if a > b)} and trails in "
                     f"{sum(1 for a, b in pairs if a < b)} of {len(pairs)} categories; ")
        text += (f"the rival has {len(adv['rival_has'])} tracked features the client lacks, "
                 f"and the client has {len(adv['client_has'])} the rival lacks.")
    return text


def compare_reports(client, rivals, date=None):
    """Build the comparison dict. client is one report dict, rivals a list of them.
    Raises ValueError on a malformed report or date."""
    if isinstance(rivals, dict):
        rivals = [rivals]
    if not isinstance(rivals, (list, tuple)) or not rivals:
        raise ValueError("need at least one rival report")
    reports = [_check_report(client, "client report")]
    reports += [_check_report(r, f"rival report {i + 1}") for i, r in enumerate(rivals)]
    if date is None:
        date = _dt.date.today().isoformat()
    try:
        _dt.datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise ValueError(f"date must be YYYY-MM-DD, got {date!r}") from None

    sites = [site_facts(r) for r in reports]
    per_site = [site_features(r) for r in reports]
    features = [{"key": k, "label": label, "phrase": phrase, "cells": [f[k] for f in per_site]}
                for k, label, phrase in FEATURE_ROWS]
    found = [collect_findings(r) for r in reports]
    cmp = {
        "date": date,
        "client": sites[0]["domain"],
        "rivals": [s["domain"] for s in sites[1:]],
        "sites": sites,
        "caveat": score_caveat(sites),
        "categories": category_table(reports),
        "features": features,
        "health_raw": [{"label": lbl, "values": [_health(r, lbl) for r in reports]}
                       for lbl in HEALTH_RAW_LABELS],
        "findings": {
            "client_only": unique_findings(found[0], found[1:]),
            "rival_only": [unique_findings(f, [found[0]]) for f in found[1:]],
        },
        "keywords": [keyword_overlap(reports[0], r) for r in reports[1:]],
        "coverage": [coverage_of(r) for r in reports],
        "advantages": advantages(features, len(reports)),
    }
    cmp["summary"] = _summary(cmp)
    return cmp


# =================================================================================
# Rendering: one layout as a list of blocks, two small serializers
# =================================================================================
class Code(str):
    """A string rendered as code (URL templates, whose asterisks Markdown would eat)."""


def _site_label(s, client=False):
    return s["domain"] + (" (client)" if client else "")


def _layout_header(cmp, heads):
    sites, cav = cmp["sites"], cmp["caveat"]
    fields = [("Tool version", "version"), ("Generated", "generated"),
              ("Deep pages (page_count)", "page_count"), ("Swept pages (sweep_count)", "sweep_count"),
              ("Pages audited (deep + swept)", "pages_audited"),
              ("Sitemap URLs", "sitemap_url_count"), ("Sitemap found", "sitemap_found"),
              ("Confidence", "confidence"), ("Overall score", "overall")]
    rows = []
    for label, key in fields:
        vals = [s[key] for s in sites]
        if key == "sitemap_found":
            vals = [_yn(v) for v in vals]
        elif key == "overall":
            vals = [f"{v}/100" if v is not None else None for v in vals]
        elif key in ("page_count", "sweep_count", "pages_audited", "sitemap_url_count"):
            vals = [_num(v) for v in vals]
        rows.append([label] + vals)
    blocks = [("h2", "1. Sites and the overall-score caveat"),
              ("table", (["Field"] + heads, rows)),
              ("p", cav["text"])]
    for r in cav["ratios"]:
        ratio = f"{r['ratio']}x" if r["ratio"] is not None else "not computable"
        blocks.append(("p", f"Pages-audited ratio, {cmp['client']} to {r['rival']}: {ratio} "
                            f"({_num(r['client_pages'])} vs {_num(r['rival_pages'])})."))
    if cav["applies"]:
        verdict = "Caveat applies: yes. " + "; ".join(cav["reasons"]) + "."
    elif cav["applies"] is None:
        verdict = ("Caveat applies: cannot tell, so assume it does. "
                   + "; ".join(cav["unknown"]) + ".")
    else:
        verdict = "Caveat applies: no. Pages audited are within 3x and every site has a sitemap."
    blocks.append(("p", verdict))
    if cav["version_note"]:
        blocks.append(("p", cav["version_note"]))
    return blocks


def _layout_categories(cmp, heads):
    many = len(cmp["sites"]) > 2
    head = ["Category"] + heads + ["Gap (client minus best rival)" if many else "Gap (client minus rival)"]
    if many:
        head.append("Client rank")
    rows = []
    for row in cmp["categories"]:
        gap = row["gap"]
        cells = [row["category"]] + [_num(v) for v in row["scores"]]
        cells.append("n/a" if gap is None else f"{gap:+d}")
        if many:
            cells.append("n/a" if row["rank"] is None else f"{row['rank']} of {row['ranked']}")
        rows.append(cells)
    return [("h2", "2. Category scores"), ("table", (head, rows))]


def _layout_features(cmp, heads):
    rows = [[f["label"]] + [c["value"] for c in f["cells"]] for f in cmp["features"]]
    raw = [[h["label"]] + [v if v is not None else "n/a" for v in h["values"]]
           for h in cmp["health_raw"]]
    return [("h2", "3. Absent features"),
            ("p", "Yes/no rows feed the lists in section 7; n/a means the report did not "
                  "contain the data. Deep pages are the ones given a full per-page audit."),
            ("table", (["Feature"] + heads, rows)),
            ("h3", "Health rows as reported"),
            ("table", (["Health row"] + heads, raw))]


def _findings_table(items):
    if not items:
        return ("p", "None.")
    return ("table", (["Severity", "Category", "Finding", "Seen on"],
                      [[e["severity"], e["category"], e["title"], _where_text(e["where"])]
                       for e in items]))


def _layout_findings(cmp):
    blocks = [("h2", "4. Findings on one side only"),
              ("p", "Matched by category and title, with numbers in titles ignored. Sources: "
                    "site-wide findings, the sweep summary, every deep page, the social and "
                    "footprint blocks, and PageSpeed when it ran."),
              ("h3", f"On {cmp['client']} (client) and on no rival"),
              _findings_table(cmp["findings"]["client_only"])]
    for rival, items in zip(cmp["rivals"], cmp["findings"]["rival_only"]):
        blocks += [("h3", f"On {rival} and not on the client"), _findings_table(items)]
    return blocks


def _layout_keywords(cmp):
    blocks = [("h2", f"5. Keyword overlap (top {TOP_TERMS} terms)")]
    for kw in cmp["keywords"]:
        blocks.append(("h3", f"{cmp['client']} vs {kw['rival']}"))
        if not kw["available"]:
            blocks.append(("p", "Keyword data is missing from at least one report."))
            continue
        blocks.append(("ul", [
            f"In {kw['rival']}'s top {TOP_TERMS}, not the client's: " + (", ".join(kw["rival_only"]) or "none"),
            f"In the client's top {TOP_TERMS}, not {kw['rival']}'s: " + (", ".join(kw["client_only"]) or "none"),
            "Shared: " + (", ".join(kw["shared"]) or "none")]))
    return blocks


def _layout_coverage(cmp):
    blocks = [("h2", "6. Coverage")]
    for cov in cmp["coverage"]:
        blocks.append(("h3", cov["domain"]))
        if cov["crawl_only"] is None:
            blocks.append(("p", "No coverage data in this report."))
            continue
        if cov["crawl_only"]:
            blocks.append(("p", "Crawl-discovered pages only: the coverage list is empty because "
                                f"no usable sitemap was found ({_num(cov['page_count'])} deep pages "
                                "reached by following links)."))
            continue
        shown = cov["templates"][:COVERAGE_ROWS_SHOWN]
        blocks.append(("table", (["Template", "Sitemap URLs", "Audited"],
                                 [[Code(t["template"]), _num(t["urls"]), _num(t["audited"])]
                                  for t in shown])))
        rest = cov["templates"][COVERAGE_ROWS_SHOWN:]
        if rest:
            blocks.append(("p", f"Plus {len(rest)} more templates holding "
                                f"{sum(t['urls'] or 0 for t in rest):,} URLs (all listed in the JSON)."))
        if cov["gaps"]:
            blocks.append(("p", "Templates with 5 or more URLs that were never sampled: "
                                + ", ".join(cov["gaps"]) + "."))
    return blocks


def _layout_advantages(cmp):
    blocks = [("h2", "7. What each side has that the other lacks"),
              ("p", "Derived only from the yes/no rows in section 3; a row that is n/a on "
                    "either side is left out.")]
    for adv in cmp["advantages"]:
        rival = cmp["sites"][adv["rival_index"]]["domain"]
        for title, items in ((f"What {rival} has that the client lacks", adv["rival_has"]),
                             (f"What the client has that {rival} lacks", adv["client_has"])):
            blocks.append(("h3", title))
            blocks.append(("ul", items) if items else ("p", "Nothing among the tracked features."))
    return blocks


def _layout(cmp):
    heads = [_site_label(s, i == 0) for i, s in enumerate(cmp["sites"])]
    blocks = [("h1", f"SEO comparison: {cmp['client']} vs {', '.join(cmp['rivals'])}"),
              ("p", f"Generated {cmp['date']} by seo_compare.py from seo_audit.py JSON reports."),
              ("p", cmp["summary"])]
    blocks += _layout_header(cmp, heads)
    blocks += _layout_categories(cmp, heads)
    blocks += _layout_features(cmp, heads)
    blocks += _layout_findings(cmp)
    blocks += _layout_keywords(cmp)
    blocks += _layout_coverage(cmp)
    blocks += _layout_advantages(cmp)
    return blocks


def _md_text(v):
    """Markdown-safe text. Code values go in backticks as they are; anything else has its
    emphasis, code-span and link markers (* _ ` [ ]), backslashes and < escaped, so a
    title like "*Adult Only*" is quoted, not turned into italics."""
    s = "n/a" if v is None or v == "" else str(v)
    s = s.replace("\n", " ")
    if isinstance(v, Code):
        return f"`{s}`"
    s = re.sub(r"([*_`\\\[\]])", r"\\\1", s)
    return s.replace("<", "\\<")


def _md_cell(v):
    return _md_text(v).replace("|", "\\|")


def render_markdown(cmp):
    out = []
    for kind, data in _layout(cmp):
        if kind in ("h1", "h2", "h3"):
            out.append("#" * int(kind[1]) + " " + _md_text(data))
        elif kind == "p":
            out.append(_md_text(data))
        elif kind == "ul":
            out.append("\n".join("- " + _md_text(item) for item in data))
        elif kind == "table":
            head, rows = data
            lines = ["| " + " | ".join(_md_cell(h) for h in head) + " |",
                     "|" + "|".join(" --- " for _ in head) + "|"]
            lines += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
            out.append("\n".join(lines))
    return "\n\n".join(out) + "\n"


_CSS = """
body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;color:#1f2933;
background:#fff;max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:1.6em;margin:0 0 .5em}h2{margin-top:2em;border-bottom:1px solid #d9dee3;padding-bottom:4px}
h3{margin-top:1.4em}.tw{overflow-x:auto}
table{border-collapse:collapse;margin:8px 0 16px;font-size:14px}
th,td{border:1px solid #d9dee3;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f3f5f7}td.y{background:#e7f6ec}td.n{background:#fdecec}
code{font:13px ui-monospace,Menlo,Consolas,monospace}
"""


def _html_cell(tag, v):
    s = "n/a" if v is None or v == "" else str(v)
    body = f"<code>{_html.escape(s)}</code>" if isinstance(v, Code) else _html.escape(s)
    m = re.match(r"(yes|no)\b", s) if tag == "td" else None
    cls = f' class="{m.group(1)[0]}"' if m else ""
    return f"<{tag}{cls}>{body}</{tag}>"


def render_html(cmp):
    parts = []
    for kind, data in _layout(cmp):
        if kind in ("h1", "h2", "h3", "p"):
            parts.append(f"<{kind}>{_html.escape(str(data))}</{kind}>")
        elif kind == "ul":
            parts.append("<ul>" + "".join(f"<li>{_html.escape(str(i))}</li>" for i in data) + "</ul>")
        elif kind == "table":
            head, rows = data
            thead = "<tr>" + "".join(_html_cell("th", h) for h in head) + "</tr>"
            tbody = "".join("<tr>" + "".join(_html_cell("td", c) for c in row) + "</tr>" for row in rows)
            parts.append(f'<div class="tw"><table><thead>{thead}</thead><tbody>{tbody}</tbody></table></div>')
    title = _html.escape(f"SEO comparison: {cmp['client']}")
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{title}</title><style>{_CSS}</style></head><body>\n"
            + "\n".join(parts) + "\n</body></html>\n")


# =================================================================================
# CLI
# =================================================================================
def _safe_name(s):
    return re.sub(r"[^A-Za-z0-9.-]+", "_", s)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Compare a client's seo_audit.py --json report with one or more competitors'.")
    ap.add_argument("client", help="Client report JSON (from seo_audit.py --json)")
    ap.add_argument("rivals", nargs="+", help="One or more competitor report JSONs")
    ap.add_argument("--out", default=".", help="Output directory (default: current directory)")
    ap.add_argument("--json", action="store_true", help="Also write the comparison as JSON")
    ap.add_argument("--html", action="store_true", help="Also write a self-contained HTML page")
    ap.add_argument("--date", help="Date for the file name and header, YYYY-MM-DD (default: today)")
    args = ap.parse_args(argv)

    try:
        client = load_report(args.client)
        rivals = [load_report(p) for p in args.rivals]
        cmp = compare_reports(client, rivals, date=args.date)
    except ValueError as e:
        sys.exit(f"seo_compare: error: {e}")

    stem = os.path.join(args.out, f"seo-compare-{_safe_name(cmp['client'])}-{cmp['date']}")
    outputs = [(stem + ".md", render_markdown(cmp))]
    if args.json:
        outputs.append((stem + ".json", json.dumps(cmp, indent=2, ensure_ascii=False) + "\n"))
    if args.html:
        outputs.append((stem + ".html", render_html(cmp)))
    try:
        os.makedirs(args.out, exist_ok=True)
        for path, text in outputs:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"Wrote {path}")
    except OSError as e:
        sys.exit(f"seo_compare: error: cannot write to {args.out} ({e.strerror or e})")
    print()
    print(cmp["summary"])


if __name__ == "__main__":
    main()
