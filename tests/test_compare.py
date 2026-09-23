"""Unit tests for scripts/seo_compare.py: no network, no fixture files.

The reports below are small in-memory dicts shaped like seo_audit.py --json output
(the same keys and the same free-text health rows), cut down to what the comparison
reads. The CLI tests write them to a temporary directory.

Run with:  python3 -m unittest tests.test_compare -v   (from the repo root)
"""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_ROOT, "scripts", "seo_compare.py")
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import seo_compare as SC                                          # noqa: E402


# =====================================================================================
# In-memory reports
# =====================================================================================
def finding(sev, title, cat):
    return {"severity": sev, "title": title, "category": cat, "impact": 3, "effort": 2,
            "observed": "x", "fix": "y"}


def page(url, *, source="crawl", schema=(), words=600, title="A descriptive page title here",
         desc="A page description.", h1=("Welcome",), client_rendered=False, findings=()):
    return {"url": url, "status": 200, "title": title, "description": desc,
            "word_count": words, "h1": list(h1), "canonical": url, "lang": "en",
            "robots": "", "schema_types": list(schema), "internal_links": 20,
            "findings": list(findings), "source": source, "client_rendered": client_rendered}


def scores(**over):
    base = {c: 80 for c in SC.CATEGORY_ORDER}
    base.update({k.replace("_", " "): v for k, v in over.items()})
    return base


def ai_matrix(refused=()):
    agents = ["OAI-SearchBot", "GPTBot", "ClaudeBot"]
    return [{"agent": a, "role": "search", "operator": "x",
             "verdict": "refused (403)" if a in refused else "ok"} for a in agents]


def client_report():
    """A large site with a sitemap and most features present."""
    return {
        "url": "https://client.example/", "domain": "client.example", "version": "2.0.0",
        "generated": "2026-09-22 10:00:00.000000",
        "page_count": 15, "sweep_count": 100, "sitemap_url_count": 400,
        "confidence": "medium", "overall": 78,
        "scores": scores(crawlability=95, performance=55, schema=90),
        "robots_found": True, "sitemap_found": True,
        "health": [
            ["TLS certificate", "valid · 75 days left · Google Trust Services"],
            ["Contact & trust pages", "contact page, tel:, mailto: · about ✓ privacy ✓ terms ✓"],
            ["Entity transparency", "postal address ✓ · legal entity named ✓"],
            ["Analytics", "GA4 (gtag.js)"],
            ["HSTS", "max-age=31536000; includeSubDomains · strong"],
            ["llms.txt", "not present (no action needed; AI crawlers are not observed requesting it)"],
            ["AI crawler edge access", "3/3 agents served 200 on 2 URL(s)"],
        ],
        "coverage": [{"template": "/tours/*", "urls": 300, "audited": 80},
                     {"template": "/", "urls": 1, "audited": 1}],
        "coverage_gaps": [],
        "ai_matrix": ai_matrix(),
        "sweep_summary": [{"severity": "LOW", "title": "Render-blocking scripts in <head>",
                           "category": "performance", "count": 90, "templates": ["/tours/*"],
                           "example": "https://client.example/tours/a"}],
        "psi": {"status": "rate-limited",
                "findings": [finding("INFO", "PageSpeed Insights rate-limited", "performance")]},
        "social": {"metrics": [["Image dimensions", "1200x630"], ["Title length", "40 chars"]],
                   "findings": [finding("LOW", "Favicon is 191 KB", "social")]},
        "keywords": {"available": True, "primary": "ghost",
                     "terms": [{"term": t, "score": 100 - i} for i, t in enumerate(
                         ["ghost", "tours", "haunted", "flagstaff", "history", "tucson"])]},
        "footprint": {"social": [{"platform": "Facebook", "url": "https://facebook.com/client",
                                  "in_sameas": True}],
                      "sameas_present": True,
                      "google": {"maps_links": ["https://www.google.com/maps?cid=1"],
                                 "gbp_links": [], "local": {"name": "Client"}},
                      "findings": []},
        "site_findings": [finding("MEDIUM", "Web font payload is heavy", "performance"),
                          finding("LOW", "Sitemap lists redirecting URLs (3)", "crawlability")],
        "pages": [
            page("https://client.example/", source="homepage", words=1900,
                 title="Client Tours | Ghost and History Walking Tours",
                 schema=("FAQPage", "Organization", "TravelAgency"),
                 findings=[finding("MEDIUM", "Duplicate title across pages", "on page")]),
            page("https://client.example/tours/a", schema=("BreadcrumbList",),
                 findings=[finding("LOW", "Title too long", "on page")]),
        ],
    }


def small_rival():
    """A tiny crawl-only site: no robots, no sitemap, no schema, no OG image, no analytics."""
    return {
        "url": "https://www.rival.example/", "domain": "www.rival.example", "version": "2.0.0",
        "generated": "2026-09-22 16:00:00.000000",
        "page_count": 11, "sweep_count": 0, "sitemap_url_count": 0,
        "confidence": "medium", "overall": 81,
        "scores": scores(crawlability=80, performance=83, schema=78),
        "robots_found": False, "sitemap_found": False,
        "health": [
            ["TLS certificate", "valid · 61 days left · Let's Encrypt"],
            ["Contact & trust pages", "contact page, tel:, mailto: · about ✓ privacy ✗ terms ✗"],
            ["Entity transparency", "postal address ✓ · legal entity named ✗"],
            ["Analytics", "none detected"],
            ["llms.txt", "found · 5 links"],
            ["AI crawler edge access", "2/3 agents served 200 on 2 URL(s) · refused: GPTBot"],
        ],
        "coverage": [], "coverage_gaps": [],
        "ai_matrix": ai_matrix(refused=("GPTBot",)),
        "sweep_summary": [],
        "psi": {"status": "rate-limited", "findings": []},
        "social": {"metrics": [["Title length", "36 chars"]],
                   "findings": [finding("MEDIUM", "No Open Graph image", "social"),
                                finding("LOW", "Favicon is 40 KB", "social")]},
        "keywords": {"available": True, "primary": "flagstaff",
                     "terms": [{"term": t} for t in ["flagstaff", "ghost", "underground", "walking tour"]]},
        "footprint": {"social": [{"platform": "Instagram", "url": "https://instagram.com/rival",
                                  "in_sameas": False}],
                      "sameas_present": False,
                      "google": {"maps_links": [], "gbp_links": [], "local": None},
                      "findings": [finding("LOW", "Social profiles not declared in sameAs schema", "schema")]},
        "site_findings": [finding("MEDIUM", "robots.txt not found", "crawlability"),
                          finding("MEDIUM", "XML sitemap not found", "crawlability"),
                          finding("LOW", "Sitemap lists redirecting URLs (7)", "crawlability")],
        "pages": [
            page("https://www.rival.example/", source="homepage", words=223,
                 title="Rival Underground | Walking Tour", desc="", h1=(),
                 findings=[finding("MEDIUM", "Duplicate title across pages", "on page"),
                           finding("LOW", "No structured data (JSON-LD)", "schema")]),
            page("https://www.rival.example/about.html", client_rendered=True),
        ],
    }


def peer_rival():
    """A rival of similar size with a sitemap, so the caveat should not fire."""
    r = copy.deepcopy(client_report())
    r.update({"domain": "peer.example", "url": "https://peer.example/", "page_count": 15,
              "sweep_count": 60, "sitemap_url_count": 90, "overall": 85,
              "scores": scores(crawlability=100, performance=60, schema=70)})
    return r


def row(cmp, key):
    return next(f for f in cmp["features"] if f["key"] == key)


def present(cmp, key, site):
    return row(cmp, key)["cells"][site]["present"]


# =====================================================================================
# Validation
# =====================================================================================
class TestValidation(unittest.TestCase):
    def test_compare_rejects_non_dict(self):
        with self.assertRaises(ValueError):
            SC.compare_reports(["not", "a", "report"], [small_rival()])
        with self.assertRaises(ValueError):
            SC.compare_reports(client_report(), ["nope"])

    def test_compare_rejects_missing_domain(self):
        bad = client_report()
        del bad["domain"]
        with self.assertRaises(ValueError):
            SC.compare_reports(bad, [small_rival()])

    def test_compare_rejects_no_rivals_and_bad_date(self):
        with self.assertRaises(ValueError):
            SC.compare_reports(client_report(), [])
        with self.assertRaises(ValueError):
            SC.compare_reports(client_report(), [small_rival()], date="22/09/2026")

    def test_load_report_rejects_malformed_files(self):
        with tempfile.TemporaryDirectory() as d:
            cases = {"list.json": "[1, 2]", "nodomain.json": '{"url": "x"}', "broken.json": "{oops"}
            for name, text in cases.items():
                path = os.path.join(d, name)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                with self.assertRaises(ValueError, msg=name):
                    SC.load_report(path)
            with self.assertRaises(ValueError):
                SC.load_report(os.path.join(d, "missing.json"))

    def test_load_report_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "r.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(client_report(), fh)
            self.assertEqual(SC.load_report(path)["domain"], "client.example")

    def test_minimal_old_report_does_not_crash(self):
        cmp = SC.compare_reports({"domain": "old.example"}, [{"domain": "older.example"}],
                                 date="2026-01-02")
        self.assertIsNone(cmp["caveat"]["applies"])          # cannot tell, says so
        self.assertEqual(row(cmp, "robots_txt")["cells"][0]["value"], "n/a")
        self.assertIsNone(present(cmp, "og_image", 0))
        md = SC.render_markdown(cmp)
        self.assertIn("cannot tell", md)
        self.assertIn("<table", SC.render_html(cmp))


# =====================================================================================
# The overall-score caveat
# =====================================================================================
class TestCaveat(unittest.TestCase):
    def test_fires_on_page_ratio_and_missing_sitemap(self):
        cav = SC.compare_reports(client_report(), [small_rival()], date="2026-09-23")["caveat"]
        self.assertIs(cav["applies"], True)
        self.assertEqual(cav["ratios"][0]["ratio"], 10.5)   # 115 audited vs 11
        self.assertTrue(cav["ratios"][0]["over_limit"])
        self.assertTrue(any("10.5x" in r for r in cav["reasons"]))
        self.assertIn("www.rival.example has no sitemap", cav["reasons"])

    def test_does_not_fire_for_a_comparable_peer(self):
        cav = SC.compare_reports(client_report(), [peer_rival()], date="2026-09-23")["caveat"]
        self.assertIs(cav["applies"], False)
        self.assertEqual(cav["ratios"][0]["ratio"], 1.5)    # 115 vs 75
        self.assertEqual(cav["reasons"], [])

    def test_missing_sitemap_alone_fires(self):
        rival = peer_rival()
        rival["sitemap_found"] = False
        cav = SC.compare_reports(client_report(), [rival], date="2026-09-23")["caveat"]
        self.assertIs(cav["applies"], True)
        self.assertEqual(cav["reasons"], ["peer.example has no sitemap"])

    def test_version_mismatch_is_noted(self):
        rival = peer_rival()
        rival["version"] = "1.9.0"
        cav = SC.compare_reports(client_report(), [rival], date="2026-09-23")["caveat"]
        self.assertIn("1.9.0", cav["version_note"])


# =====================================================================================
# Category scores
# =====================================================================================
class TestCategories(unittest.TestCase):
    def test_gap_and_rank(self):
        cmp = SC.compare_reports(client_report(), [small_rival(), peer_rival()], date="2026-09-23")
        cats = {r["category"]: r for r in cmp["categories"]}
        self.assertEqual(list(cats)[:8], SC.CATEGORY_ORDER)
        # client 95, rivals 80 and 100: gap against the best rival, rank 2 of 3
        self.assertEqual(cats["crawlability"]["gap"], -5)
        self.assertEqual((cats["crawlability"]["rank"], cats["crawlability"]["ranked"]), (2, 3))
        # client 90, rivals 78 and 70: leads
        self.assertEqual(cats["schema"]["gap"], 12)
        self.assertEqual(cats["schema"]["rank"], 1)
        md = SC.render_markdown(cmp)
        self.assertIn("Client rank", md)
        self.assertIn("2 of 3", md)


# =====================================================================================
# Absent-features matrix
# =====================================================================================
class TestFeatures(unittest.TestCase):
    def setUp(self):
        self.cmp = SC.compare_reports(client_report(), [small_rival()], date="2026-09-23")

    def test_robots_sitemap_schema(self):
        for key in ("robots_txt", "sitemap", "jsonld_home", "org_home", "breadcrumb", "faq"):
            self.assertIs(present(self.cmp, key, 0), True, key)
            self.assertIs(present(self.cmp, key, 1), False, key)
        self.assertIn("TravelAgency", row(self.cmp, "org_home")["cells"][0]["value"])
        self.assertEqual(row(self.cmp, "schema_home")["cells"][1]["value"], "none")
        self.assertEqual(row(self.cmp, "breadcrumb")["cells"][0]["value"], "yes (1 of 2 pages)")

    def test_analytics_from_health_text(self):
        self.assertIs(present(self.cmp, "analytics", 0), True)
        self.assertIs(present(self.cmp, "analytics", 1), False)
        self.assertEqual(row(self.cmp, "analytics")["cells"][1]["raw"], "none detected")

    def test_trust_pages_and_entity_from_ticks(self):
        for key in ("about", "privacy", "terms", "contact", "postal", "legal"):
            self.assertIs(present(self.cmp, key, 0), True, key)
        self.assertIs(present(self.cmp, "about", 1), True)
        self.assertIs(present(self.cmp, "privacy", 1), False)
        self.assertIs(present(self.cmp, "terms", 1), False)
        self.assertIs(present(self.cmp, "postal", 1), True)
        self.assertIs(present(self.cmp, "legal", 1), False)

    def test_og_image(self):
        self.assertIs(present(self.cmp, "og_image", 0), True)
        self.assertEqual(row(self.cmp, "og_image")["cells"][0]["value"], "yes (1200x630)")
        self.assertIs(present(self.cmp, "og_image", 1), False)

    def test_hsts_llms_tls_ai(self):
        self.assertIs(present(self.cmp, "hsts", 0), True)
        self.assertIs(present(self.cmp, "hsts", 1), False)      # row omitted = no header
        self.assertIs(present(self.cmp, "llms", 0), False)      # "not present (...)"
        self.assertIs(present(self.cmp, "llms", 1), True)
        self.assertEqual(row(self.cmp, "tls_days")["cells"][0]["value"], "75")
        self.assertIs(present(self.cmp, "ai_edge", 0), True)
        self.assertIs(present(self.cmp, "ai_edge", 1), False)
        self.assertEqual(row(self.cmp, "ai_edge")["cells"][1]["value"], "2 ok, 1 refused (GPTBot)")

    def test_homepage_and_footprint_rows(self):
        def cells(key):
            return row(self.cmp, key)["cells"]
        self.assertEqual(cells("home_words")[0]["value"], "1,900")
        self.assertEqual(cells("home_title_len")[1]["value"], "32 chars")
        self.assertIs(cells("home_desc_len")[1]["present"], False)
        self.assertIs(cells("home_h1")[1]["present"], False)
        self.assertEqual(cells("client_rendered")[1]["value"], "1 of 2")
        self.assertEqual(cells("social")[1]["value"], "1 (Instagram)")
        self.assertIs(cells("sameas")[1]["present"], False)
        self.assertIs(cells("google")[0]["present"], True)
        self.assertEqual(cells("kw_primary")[1]["value"], "flagstaff")

    def test_health_flag_parser(self):
        self.assertIs(SC.health_flag("GA4 (gtag.js)"), True)
        self.assertIs(SC.health_flag("none detected"), False)
        self.assertIs(SC.health_flag("not present (no action needed)"), False)
        self.assertIs(SC.health_flag("robots.txt not found"), False)
        self.assertIs(SC.health_flag("postal address ✗"), False)
        self.assertIsNone(SC.health_flag("could not inspect (TimeoutError)"))
        self.assertIsNone(SC.health_flag(None))
        self.assertIs(SC.tick("about ✓ privacy ✗ terms ✗", "about"), True)
        self.assertIs(SC.tick("about ✓ privacy ✗ terms ✗", "terms"), False)
        self.assertIsNone(SC.tick("could not evaluate: no crawlable links", "about"))


# =====================================================================================
# Findings, keywords, derived lists
# =====================================================================================
class TestFindings(unittest.TestCase):
    def setUp(self):
        self.cmp = SC.compare_reports(client_report(), [small_rival()], date="2026-09-23")
        self.client_only = {e["title"]: e for e in self.cmp["findings"]["client_only"]}
        self.rival_only = {e["title"]: e for e in self.cmp["findings"]["rival_only"][0]}

    def test_unique_both_directions_with_severity(self):
        self.assertEqual(self.client_only["Web font payload is heavy"]["severity"], "MEDIUM")
        self.assertIn("Title too long", self.client_only)                     # page finding
        self.assertIn("Render-blocking scripts in <head>", self.client_only)  # sweep summary
        self.assertEqual(self.rival_only["robots.txt not found"]["severity"], "MEDIUM")
        self.assertIn("No Open Graph image", self.rival_only)                 # social block
        self.assertIn("Social profiles not declared in sameAs schema", self.rival_only)

    def test_shared_findings_are_excluded(self):
        for title in ("Duplicate title across pages",):
            self.assertNotIn(title, self.client_only)
            self.assertNotIn(title, self.rival_only)

    def test_numbers_in_titles_do_not_split_a_finding(self):
        for title in ("Sitemap lists redirecting URLs (3)", "Favicon is 191 KB"):
            self.assertNotIn(title, self.client_only)
        for title in ("Sitemap lists redirecting URLs (7)", "Favicon is 40 KB"):
            self.assertNotIn(title, self.rival_only)

    def test_pagespeed_tool_notices_are_ignored(self):
        self.assertNotIn("PageSpeed Insights rate-limited", self.client_only)

    def test_sorted_most_severe_first(self):
        sev = [SC.SEV_ORDER[e["severity"]] for e in self.cmp["findings"]["rival_only"][0]]
        self.assertEqual(sev, sorted(sev))


class TestKeywordsAndAdvantages(unittest.TestCase):
    def setUp(self):
        self.cmp = SC.compare_reports(client_report(), [small_rival()], date="2026-09-23")

    def test_keyword_overlap(self):
        kw = self.cmp["keywords"][0]
        self.assertEqual(kw["rival_only"], ["underground", "walking tour"])
        self.assertEqual(kw["client_only"], ["tours", "haunted", "history", "tucson"])
        self.assertEqual(kw["shared"], ["ghost", "flagstaff"])

    def test_derived_lists(self):
        adv = self.cmp["advantages"][0]
        self.assertEqual(adv["rival_has"], ["an llms.txt file"])
        for phrase in ("a robots.txt file", "an XML sitemap", "an analytics tag",
                       "a linked privacy policy", "a linked terms page",
                       "an Open Graph share image", "an HSTS header", "a named legal entity"):
            self.assertIn(phrase, adv["client_has"])
        # both have an About page and social links: in neither list
        for phrase in ("a linked About page", "links to social profiles"):
            self.assertNotIn(phrase, adv["client_has"] + adv["rival_has"])

    def test_lists_come_only_from_yes_no_rows(self):
        allowed = {f["phrase"] for f in self.cmp["features"]
                   if f["phrase"] and f["cells"][0]["present"] is not None
                   and f["cells"][1]["present"] is not None}
        adv = self.cmp["advantages"][0]
        self.assertTrue(set(adv["rival_has"] + adv["client_has"]) <= allowed)

    def test_n_a_rows_never_feed_the_lists(self):
        rival = small_rival()
        del rival["health"]            # every health-derived row becomes n/a
        adv = SC.compare_reports(client_report(), [rival], date="2026-09-23")["advantages"][0]
        self.assertNotIn("an llms.txt file", adv["rival_has"])
        self.assertNotIn("an analytics tag", adv["client_has"])
        self.assertIn("a robots.txt file", adv["client_has"])


# =====================================================================================
# Rendering
# =====================================================================================
class TestRender(unittest.TestCase):
    def setUp(self):
        self.cmp = SC.compare_reports(client_report(), [small_rival()], date="2026-09-23")

    def test_markdown_has_caveat_domains_and_sections_in_order(self):
        md = SC.render_markdown(self.cmp)
        self.assertIn(SC.CAVEAT, md)
        self.assertIn("client.example", md)
        self.assertIn("www.rival.example", md)
        self.assertIn("Caveat applies: yes", md)
        self.assertIn("Crawl-discovered pages only", md)
        heads = [f"## {n}." for n in range(1, 8)]
        positions = [md.index(h) for h in heads]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("`/tours/*`", md)             # templates as code
        self.assertIn("\\<head>", md)               # angle bracket escaped for Markdown
        self.assertNotIn("—", md)              # no em dashes in generated prose

    def test_markdown_for_a_peer_says_the_caveat_does_not_apply(self):
        md = SC.render_markdown(SC.compare_reports(client_report(), [peer_rival()], date="2026-09-23"))
        self.assertIn("Caveat applies: no", md)
        self.assertIn(SC.CAVEAT, md)                # the explanation is always printed

    def test_html_is_self_contained(self):
        page_html = SC.render_html(self.cmp)
        self.assertIn("<table", page_html)
        self.assertIn("<style>", page_html)
        self.assertIn("&lt;head&gt;", page_html)
        for external in ("<script", "<link", " src=", "@import"):
            self.assertNotIn(external, page_html)

    def test_json_serialisable(self):
        back = json.loads(json.dumps(self.cmp))
        self.assertEqual(back["client"], "client.example")


# =====================================================================================
# CLI
# =====================================================================================
class TestCli(unittest.TestCase):
    def _write(self, d, name, obj):
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def test_writes_all_three_files(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._write(d, "client.json", client_report())
            r = self._write(d, "rival.json", small_rival())
            out = os.path.join(d, "out")
            buf = io.StringIO()
            with redirect_stdout(buf):
                SC.main([c, r, "--out", out, "--json", "--html", "--date", "2026-01-02"])
            stem = os.path.join(out, "seo-compare-client.example-2026-01-02")
            for ext in (".md", ".json", ".html"):
                self.assertTrue(os.path.isfile(stem + ext), ext)
                self.assertIn(stem + ext, buf.getvalue())
            self.assertIn("scored 78/100", buf.getvalue())
            with open(stem + ".json", encoding="utf-8") as fh:
                self.assertIs(json.load(fh)["caveat"]["applies"], True)

    def test_markdown_only_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._write(d, "client.json", client_report())
            r = self._write(d, "rival.json", peer_rival())
            with redirect_stdout(io.StringIO()):
                SC.main([c, r, "--out", d, "--date", "2026-01-02"])
            made = sorted(f for f in os.listdir(d) if f.startswith("seo-compare-"))
            self.assertEqual(made, ["seo-compare-client.example-2026-01-02.md"])

    def test_malformed_input_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._write(d, "client.json", client_report())
            bad = self._write(d, "bad.json", {"url": "https://no-domain.example/"})
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                SC.main([c, bad, "--out", d])
            self.assertNotIn(ctx.exception.code, (0, None))
            self.assertIn("domain", str(ctx.exception.code))

    def test_script_runs_as_a_program(self):
        with tempfile.TemporaryDirectory() as d:
            c = self._write(d, "client.json", client_report())
            r = self._write(d, "rival.json", small_rival())
            ok = subprocess.run([sys.executable, _SCRIPT, c, r, "--out", d, "--date", "2026-01-02"],
                                capture_output=True, text=True, timeout=60)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertIn("seo-compare-client.example-2026-01-02.md", ok.stdout)
            bad = self._write(d, "list.json", [1, 2, 3])
            fail = subprocess.run([sys.executable, _SCRIPT, c, bad, "--out", d],
                                  capture_output=True, text=True, timeout=60)
            self.assertNotEqual(fail.returncode, 0)
            self.assertIn("seo_compare: error", fail.stderr)


if __name__ == "__main__":
    unittest.main()
