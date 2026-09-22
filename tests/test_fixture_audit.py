"""End-to-end regression tests: run_audit against the local fixture sites.

Every run passes ``network_checks=False, use_pagespeed=False`` so nothing leaves the
machine except unavoidable host-variant probes (http://127.0.0.1/, www.127.0.0.1) that
simply fail to connect. Findings are asserted by title, severity and category.

Run with:  python3 -m unittest discover -s tests -v   (from the repo root)
"""

import json
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seo_audit as SA                                            # noqa: E402
import report as RP                                               # noqa: E402
import fixture_site as FX                                         # noqa: E402


# =====================================================================================
# Shared helpers
# =====================================================================================
def index_findings(report):
    """Every finding the report exposes, wherever it lives."""
    out = list(report["site_findings"])
    out += list(report["social"]["findings"])
    out += list(report["footprint"]["findings"])
    out += list(report.get("psi", {}).get("findings", []))
    for page in report["pages"]:
        out.extend(page["findings"])
    # sweep pages are summarised rather than listed page by page
    for row in report.get("sweep_summary", []):
        out.append({"severity": row["severity"], "title": row["title"],
                    "category": row["category"], "observed": row.get("example", ""),
                    "fix": "", "impact": 0, "effort": 0})
    return out


def audit(url, **kw):
    kw.setdefault("max_pages", 4)
    kw.setdefault("sweep", 0)
    kw.setdefault("probe_ai", False)
    return SA.run_audit(url, use_pagespeed=False, network_checks=False, **kw)


#: Every finding the broken fixture is built to trigger. The clean fixture must
#: produce none of them — that false-positive guard is the point of the pairing.
NEW_FINDING_TITLES = [
    # AI crawler access
    "AI training crawlers are refused at the edge",
    "AI search / assistant crawlers are refused at the edge",
    "Site refuses every non-browser user-agent we tried",
    "Site refuses non-browser user-agents",
    # crawl mechanics
    "HEAD requests fail where GET succeeds",
    "Internal links point at non-canonical URLs",
    "Canonical points at a thinner page than the one carrying it",
    "Internal links lead to noindexed pages",
    "A sitemap template gets no internal links from any crawled page",
    "Server ignores Accept-Encoding",
    "HTML exceeds Googlebot's 2 MB fetch limit",
    # sitemap hygiene
    "Sitemap lists noindexed URLs",
    "Sitemap lists non-canonical URLs",
    "Sitemap lists URLs that return errors",
    "Many sitemap URLs carry no <lastmod>",
    "Every sitemap <lastmod> is the same date",
    # robots.txt
    "robots.txt returns a server error",
    "robots.txt is an HTML page",
    "robots.txt exceeds 500 KiB",
    "robots.txt blocks AI search crawlers",
    "robots.txt blocks AI training crawlers",
    "robots.txt AI-preference signal says search=no",
    "robots.txt AI-preference signal opts out of AI answers",
    "robots.txt is partly written by the CDN",
    # front-end performance
    "Content-Security-Policy blocks a script the page loads",
    "Third-party script is requested before the stylesheet",
    "HTML is served Cache-Control: no-store",
    "Hero image is hotlinked cross-origin through redirects",
    "Hero image is very heavy",
    "Priority image is lazy-loaded",
    "Small images are served from large files",
    # social
    "Share image does not load",
    "Share image is small or not landscape",
    "Share image is over 600 KB",
    "<head> ends beyond the first 300 KB of HTML",
    # answer-engine readiness
    "Snippets are disabled for this page",
    "noarchive / nocache limits Bing Chat and Copilot",
    "Article shows no visible publish/update date",
    # trust / hygiene
    "security.txt has expired",
    "Private files are publicly downloadable",
    "No postal address found anywhere on the crawled pages",
    # llms.txt
    "llms.txt lists URLs that don't resolve",
    "llms.txt points at non-canonical URLs",
    "llms.txt deviates from the llmstxt.org format",
    # content duplication
    "Near-duplicate pages within a template",
    "Template pages are mostly boilerplate",
    "The same sentence appears more than once on the page",
]


class FindingAssertions:
    """Mixin: ``self.found`` is a list of finding dicts."""

    def one(self, title_prefix):
        hits = [f for f in self.found if f["title"].startswith(title_prefix)]
        self.assertTrue(hits, "no finding starting with %r; got: %s"
                        % (title_prefix, sorted({f["title"] for f in self.found})))
        return hits[0]

    def assert_finding(self, title_prefix, severity, category):
        fi = self.one(title_prefix)
        self.assertEqual(fi["severity"], severity,
                         "%s: severity %s" % (fi["title"], fi["severity"]))
        self.assertEqual(fi["category"], category,
                         "%s: category %s" % (fi["title"], fi["category"]))
        return fi

    def assert_absent(self, title_prefix):
        hits = [f["title"] for f in self.found if f["title"].startswith(title_prefix)]
        self.assertEqual(hits, [], "unexpected finding(s): %s" % hits)


# =====================================================================================
# The broken site
# =====================================================================================
class TestBrokenSiteAudit(FindingAssertions, unittest.TestCase):
    """One audit of the broken fixture, many assertions against it."""

    MAX_PAGES = 17          # one deep page per non-homepage template

    @classmethod
    def setUpClass(cls):
        cls.site = FX.build_broken_site()
        cls.report = SA.run_audit(cls.site.base + "/", max_pages=cls.MAX_PAGES,
                                  sweep=100, use_pagespeed=False, probe_ai=False,
                                  network_checks=False)
        cls.found = index_findings(cls.report)

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    # ---- the crawl actually happened ----------------------------------------------
    def test_the_whole_sitemap_was_audited(self):
        self.assertEqual(self.report["page_count"], self.MAX_PAGES)
        self.assertGreater(self.report["sweep_count"], 10)
        self.assertEqual(self.report["sitemap_url_count"],
                         self.report["page_count"] + self.report["sweep_count"])

    # ---- head parity ---------------------------------------------------------------
    def test_head_404_where_get_200(self):
        fi = self.assert_finding("HEAD requests fail where GET succeeds",
                                 "LOW", "crawlability")
        self.assertIn("/img/", fi["observed"])
        self.assertIn("HEAD 404", fi["observed"])

    # ---- link architecture ----------------------------------------------------------
    def test_internal_links_point_at_non_canonical_urls(self):
        fi = self.assert_finding("Internal links point at non-canonical URLs",
                                 "HIGH", "crawlability")
        # HIGH because none of the canonical targets (/canon/*) is linked anywhere.
        self.assertIn("None of those canonical URLs is linked", fi["observed"])

    def test_a_query_string_only_variant_is_not_counted_as_non_canonical(self):
        # /page?ref=x canonicalises to /page. Same path, so it must not be counted.
        # The three /dup/* pages are the only genuine non-canonical targets.
        self.assertEqual(self.report["link_summary"]["non_canonical"], 3)
        self.assertEqual(self.report["link_summary"]["sampled"], 10)

    def test_canonical_points_at_a_thinner_page(self):
        fi = self.assert_finding("Canonical points at a thinner page than the one carrying it",
                                 "HIGH", "crawlability")
        self.assertIn("/dup/", fi["observed"])
        self.assertIn("/canon/", fi["observed"])
        self.assertIn("schema only on the non-canonical page: Article", fi["observed"])

    def test_internal_links_lead_to_noindexed_pages(self):
        fi = self.assert_finding("Internal links lead to noindexed pages",
                                 "MEDIUM", "crawlability")
        self.assertTrue(fi["title"].startswith("Internal links lead to noindexed pages (5 of 10"),
                        fi["title"])

    def test_sitemap_only_template_has_no_internal_links(self):
        fi = self.assert_finding("A sitemap template gets no internal links from any crawled page",
                                 "MEDIUM", "crawlability")
        self.assertIn("/orphan/*", fi["observed"])
        self.assertEqual(self.report["link_summary"]["orphan_templates"],
                         [("/orphan/*", 12)])

    # ---- sitemap hygiene -------------------------------------------------------------
    def test_sitemap_lists_noindexed_urls(self):
        self.assert_finding("Sitemap lists noindexed URLs (2 of", "MEDIUM", "crawlability")

    def test_sitemap_lists_non_canonical_urls(self):
        self.assert_finding("Sitemap lists non-canonical URLs (2 of", "MEDIUM", "crawlability")

    def test_sitemap_error_urls_counted_but_not_the_403(self):
        # 2x404 + 2x410 + 2x500 = 6. /sm-403/a is refused, not dead, and must not count.
        fi = self.assert_finding("Sitemap lists URLs that return errors (6 of",
                                 "HIGH", "crawlability")
        self.assertNotIn("/sm-403/", fi["observed"])

    def test_many_sitemap_urls_without_lastmod(self):
        self.assert_finding("Many sitemap URLs carry no <lastmod>", "LOW", "crawlability")

    # ---- non-browser user agents ------------------------------------------------------
    def test_pages_that_stay_blocked_are_reported(self):
        fi = self.assert_finding("Site refuses non-browser user-agents", "INFO", "crawlability")
        self.assertIn("stayed blocked", fi["observed"])

    # ---- front-end performance ----------------------------------------------------------
    def test_csp_blocks_a_script_the_page_loads(self):
        fi = self.assert_finding("Content-Security-Policy blocks a script the page loads",
                                 "MEDIUM", "performance")
        self.assertIn("localhost", fi["observed"])

    def test_third_party_script_before_the_stylesheet(self):
        self.assert_finding("Third-party script is requested before the stylesheet",
                            "LOW", "performance")

    def test_html_served_no_store(self):
        self.assert_finding("HTML is served Cache-Control: no-store", "LOW", "performance")

    def test_forced_content_encoding(self):
        fi = self.assert_finding("Server ignores Accept-Encoding", "MEDIUM", "crawlability")
        self.assertIn("br", fi["observed"])

    def test_html_over_the_two_megabyte_fetch_limit(self):
        self.assert_finding("HTML exceeds Googlebot's 2 MB fetch limit", "HIGH", "crawlability")

    # ---- images -------------------------------------------------------------------------
    def test_cross_origin_hero_through_redirects(self):
        fi = self.assert_finding("Hero image is hotlinked cross-origin through redirects",
                                 "MEDIUM", "performance")
        self.assertIn("localhost", fi["observed"])
        self.assertIn("2 redirect hop(s)", fi["observed"])

    def test_heavy_same_origin_hero(self):
        fi = self.assert_finding("Hero image is very heavy", "MEDIUM", "performance")
        self.assertIn("/hero-assets/big.png", fi["observed"])

    def test_priority_image_is_lazy_loaded(self):
        self.assert_finding("Priority image is lazy-loaded", "MEDIUM", "performance")

    def test_small_images_from_large_files(self):
        fi = self.assert_finding("Small images are served from large files",
                                 "LOW", "performance")
        self.assertIn("of 4 sampled", fi["observed"])

    # ---- social -----------------------------------------------------------------------
    def test_share_image_404_on_an_inner_page(self):
        fi = self.assert_finding("Share image does not load", "MEDIUM", "social")
        self.assertIn("missing-og.png", fi["observed"])

    def test_share_image_small_or_not_landscape(self):
        fi = self.assert_finding("Share image is small or not landscape", "LOW", "social")
        self.assertIn("200×200", fi["observed"])

    def test_share_image_over_600kb(self):
        self.assert_finding("Share image is over 600 KB", "LOW", "social")

    def test_head_ends_past_300kb(self):
        self.assert_finding("<head> ends beyond the first 300 KB of HTML", "LOW", "social")

    # ---- answer-engine readiness ---------------------------------------------------------
    def test_nosnippet(self):
        self.assert_finding("Snippets are disabled for this page", "HIGH", "ai search")

    def test_noarchive(self):
        self.assert_finding("noarchive / nocache limits Bing Chat and Copilot",
                            "LOW", "ai search")

    def test_article_without_a_visible_date(self):
        self.assert_finding("Article shows no visible publish/update date",
                            "LOW", "ai search")

    # ---- trust ----------------------------------------------------------------------------
    def test_expired_security_txt(self):
        fi = self.assert_finding("security.txt has expired", "LOW", "trust")
        self.assertIn("2020-01-01", fi["observed"])

    def test_exposed_git_metadata(self):
        fi = self.assert_finding("Private files are publicly downloadable", "CRITICAL", "trust")
        self.assertIn("/.git/HEAD", fi["observed"])

    def test_no_postal_address_when_a_trust_page_exists_without_one(self):
        self.assert_finding("No postal address found anywhere on the crawled pages",
                            "LOW", "trust")

    # ---- llms.txt --------------------------------------------------------------------------
    def test_llms_txt_dead_link(self):
        fi = self.assert_finding("llms.txt lists URLs that don't resolve", "INFO", "ai search")
        self.assertIn("/llms-dead", fi["observed"])

    def test_llms_txt_non_canonical_link(self):
        fi = self.assert_finding("llms.txt points at non-canonical URLs", "INFO", "ai search")
        self.assertIn("canonical elsewhere", fi["observed"])

    def test_a_valid_llms_txt_shape_is_not_criticised(self):
        self.assert_absent("llms.txt deviates from the llmstxt.org format")

    # ---- duplication -------------------------------------------------------------------------
    def test_near_duplicate_pages(self):
        fi = self.assert_finding("Near-duplicate pages within a template", "MEDIUM", "on page")
        self.assertIn("/twins/*", fi["observed"])

    def test_boilerplate_template(self):
        fi = self.assert_finding("Template pages are mostly boilerplate", "LOW", "on page")
        self.assertIn("/places/*", fi["observed"])

    def test_repeated_sentence(self):
        fi = self.assert_finding("The same sentence appears more than once on the page",
                                 "LOW", "on page")
        self.assertIn("Harbor lantern", fi["observed"])

    def test_duplication_rows(self):
        rows = {d["template"]: d for d in self.report["duplication"]}
        self.assertEqual(rows["/twins/*"]["near_duplicate_pairs"], 1)
        self.assertEqual(rows["/places/*"]["near_duplicate_pairs"], 0)
        self.assertLess(rows["/places/*"]["median_unique_words"], 120)
        self.assertGreaterEqual(rows["/orphan/*"]["median_unique_words"], 120)

    # ---- coverage ------------------------------------------------------------------------------
    def test_every_sitemap_template_was_audited(self):
        cov = self.report["coverage"]
        self.assertGreaterEqual(len(cov), 15)
        unaudited = [c["template"] for c in cov if c["audited"] < 1]
        self.assertEqual(unaudited, [])
        self.assertEqual(self.report["coverage_gaps"], [])
        by_t = {c["template"]: c for c in cov}
        self.assertEqual(by_t["/orphan/*"]["urls"], 12)
        self.assertEqual(by_t["/orphan/*"]["audited"], 12)

    def test_sitemap_was_not_truncated(self):
        self.assertFalse(self.report["sitemap_truncated"])

    # ---- report plumbing ------------------------------------------------------------------------
    def test_report_is_json_serialisable(self):
        self.assertIsInstance(json.dumps(self.report, default=str), str)

    def test_report_renders_to_html(self):
        html = RP.render_html(self.report)
        self.assertIn("Coverage by URL template", html)
        self.assertIn("Sitemap sweep", html)


class TestBrokenSiteTinyBudget(unittest.TestCase):
    """A 3-page budget must still spread across templates, not take the first 3 URLs."""

    @classmethod
    def setUpClass(cls):
        cls.site = FX.build_broken_site()
        cls.report = audit(cls.site.base + "/", max_pages=3, sweep=0)

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def test_three_deep_pages(self):
        self.assertEqual(len(self.report["pages"]), 3)

    def test_deep_pages_span_at_least_three_templates(self):
        templates = {SA.url_template(p["url"]) for p in self.report["pages"]}
        self.assertGreaterEqual(len(templates), 3, templates)

    def test_it_did_not_simply_take_the_first_sitemap_entries(self):
        # The sitemap opens with the homepage then /orphan/1../orphan/12, so a naive
        # "first N" crawl would only ever see "/" and "/orphan/*".
        templates = {SA.url_template(p["url"]) for p in self.report["pages"]}
        self.assertTrue(templates - {"/", "/orphan/*"}, templates)


# =====================================================================================
# The clean site — the false-positive guard
# =====================================================================================
class TestCleanSiteAudit(FindingAssertions, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = FX.build_clean_site()
        cls.report = SA.run_audit(cls.site.base + "/", max_pages=15, sweep=100,
                                  use_pagespeed=False, probe_ai=True,
                                  network_checks=False)
        cls.found = index_findings(cls.report)

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def test_none_of_the_new_findings_fire(self):
        for title in NEW_FINDING_TITLES:
            with self.subTest(finding=title):
                self.assert_absent(title)

    def test_a_missing_llms_txt_is_never_a_finding(self):
        # (the clean site does publish one; this guards the removed "No llms.txt file")
        self.assert_absent("No llms.txt file")

    def test_it_scored_well(self):
        # Only the unavoidable local-fixture findings remain (plain http, unreachable
        # host variants, no analytics, no BreadcrumbList).
        self.assertGreaterEqual(self.report["overall"], 85)

    def test_ai_matrix_has_one_row_per_probed_agent(self):
        matrix = self.report["ai_matrix"]
        self.assertEqual(len(matrix), len(SA.AI_AGENT_PROBES))
        self.assertEqual([m["agent"] for m in matrix],
                         [spec[0] for spec in SA.AI_AGENT_PROBES])
        self.assertTrue(all(m["verdict"] == "ok" for m in matrix),
                        [m for m in matrix if m["verdict"] != "ok"])

    def test_every_sitemap_template_was_audited(self):
        self.assertTrue(self.report["coverage"])
        self.assertEqual([c["template"] for c in self.report["coverage"]
                          if c["audited"] < 1], [])

    def test_link_architecture_found_nothing_wrong(self):
        summary = self.report["link_summary"]
        self.assertGreaterEqual(summary["sampled"], 1)
        self.assertEqual(summary["broken"], 0)
        self.assertEqual(summary["non_canonical"], 0)
        self.assertEqual(summary["noindexed"], 0)
        self.assertEqual(summary["orphan_templates"], [])

    def test_no_template_looks_duplicated(self):
        for row in self.report["duplication"]:
            self.assertEqual(row["near_duplicate_pairs"], 0, row)
            self.assertGreaterEqual(row["median_unique_words"], 120, row)

    def test_report_is_json_serialisable(self):
        self.assertIsInstance(json.dumps(self.report, default=str), str)

    def test_report_renders_to_html(self):
        html = RP.render_html(self.report)
        self.assertIn("Coverage by URL template", html)
        self.assertIn("AI crawler access", html)


# =====================================================================================
# AI crawler access at the edge
# =====================================================================================
FORBIDDEN = ("<!doctype html><html lang=\"en\"><head><title>Forbidden</title></head>"
             "<body><h1>Access denied</h1></body></html>")


def refuse_on_inner_pages_only(ua, path):
    """GPTBot is served the homepage but refused everywhere else."""
    if "gptbot" in (ua or "").lower() and path != "/":
        return FX.route(FORBIDDEN, status=403)
    return None


def challenge_gptbot(ua, path):
    """A Cloudflare-style managed challenge, announced with cf-mitigated."""
    if "gptbot" in (ua or "").lower():
        return FX.route(FORBIDDEN, status=403, headers={"cf-mitigated": "challenge"})
    return None


def charge_ccbot(ua, path):
    """Pay-per-crawl: 402 rather than a refusal."""
    if "ccbot" in (ua or "").lower():
        return FX.route("payment required", status=402, ctype=FX.TEXT_CTYPE)
    return None


class TestAiEdgeAccess(FindingAssertions, unittest.TestCase):
    """One minimal site, a different CDN bot rule per test."""

    @classmethod
    def setUpClass(cls):
        cls.site = FX.build_minimal_site()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def tearDown(self):
        self.site.ua_policy = None

    def _run(self, policy, robots_text=None):
        self.site.ua_policy = policy
        if robots_text is not None:
            self.site.add("/robots.txt", FX.route(robots_text, ctype=FX.TEXT_CTYPE))
        report = audit(self.site.base + "/", max_pages=2, sweep=0, probe_ai=True)
        self.found = index_findings(report)
        return report

    def test_training_crawlers_refused_by_user_agent(self):
        report = self._run(FX.AI_TRAIN_REFUSED)
        fi = self.assert_finding("AI training crawlers are refused at the edge",
                                 "MEDIUM", "ai search")
        self.assertIn("GPTBot (train)", fi["observed"])
        self.assertIn("ClaudeBot (train)", fi["observed"])
        refused = {m["agent"] for m in report["ai_matrix"] if m["verdict"] != "ok"}
        self.assertEqual(refused, {"GPTBot", "ClaudeBot"})
        self.assert_absent("AI search / assistant crawlers are refused at the edge")

    def test_a_browser_user_agent_still_gets_200(self):
        report = self._run(FX.AI_TRAIN_REFUSED)
        self.assertEqual(report["pages"][0]["status"], 200)
        ok = [m["agent"] for m in report["ai_matrix"] if m["verdict"] == "ok"]
        self.assertGreater(len(ok), 10)

    def test_search_crawler_refused_is_high(self):
        self._run(FX.AI_SEARCH_REFUSED)
        fi = self.assert_finding("AI search / assistant crawlers are refused at the edge",
                                 "HIGH", "ai search")
        self.assertIn("OAI-SearchBot (search)", fi["observed"])

    def test_robots_naming_the_refused_bot_is_reported_in_the_fix(self):
        # The severity no longer escalates; the disagreement is called out in the fix.
        self._run(FX.AI_GPTBOT_REFUSED,
                  robots_text="User-agent: GPTBot\nAllow: /\n\nUser-agent: *\nAllow: /\n")
        fi = self.assert_finding("AI training crawlers are refused at the edge",
                                 "MEDIUM", "ai search")
        self.assertIn("robots.txt names GPTBot", fi["fix"])
        self.assertIn("the file and the edge disagree", fi["fix"])

    def test_every_non_browser_user_agent_refused(self):
        report = self._run(FX.AI_ALL_NON_BROWSER_REFUSED)
        self.assert_finding("Site refuses every non-browser user-agent we tried",
                            "MEDIUM", "ai search")
        # the auditor UA was refused too, so analyze_page retried as a browser
        self.assertTrue(any(p.get("ua_fallback") for p in report["pages"]))
        fi = self.assert_finding("Site refuses non-browser user-agents",
                                 "INFO", "crawlability")
        self.assertIn("browser user-agent", fi["observed"])
        self.assert_absent("AI training crawlers are refused at the edge")
        self.assert_absent("AI search / assistant crawlers are refused at the edge")

    def test_refusal_on_an_inner_page_only(self):
        report = self._run(refuse_on_inner_pages_only)
        row = next(m for m in report["ai_matrix"] if m["agent"] == "GPTBot")
        self.assertEqual(row["verdict"], "refused (403) on some pages")
        self.assert_finding("AI training crawlers are refused at the edge",
                            "MEDIUM", "ai search")

    def test_a_managed_challenge_is_reported_as_challenged(self):
        report = self._run(challenge_gptbot)
        row = next(m for m in report["ai_matrix"] if m["agent"] == "GPTBot")
        self.assertEqual(row["verdict"], "challenged (403)")
        self.assert_finding("AI training crawlers are refused at the edge",
                            "MEDIUM", "ai search")

    def test_pay_per_crawl_is_reported(self):
        report = self._run(charge_ccbot)
        row = next(m for m in report["ai_matrix"] if m["agent"] == "CCBot")
        self.assertEqual(row["verdict"], "pay-per-crawl (402)")
        fi = self.assert_finding("AI training crawlers are refused at the edge",
                                 "MEDIUM", "ai search")
        self.assertIn("pay-per-crawl (402)", fi["observed"])

    def test_matrix_row_shape(self):
        report = self._run(None)
        self.assertEqual(len(report["ai_matrix"]), len(SA.AI_AGENT_PROBES))
        for row in report["ai_matrix"]:
            self.assertEqual(set(row), {"agent", "role", "operator", "verdict"})
            self.assertIn(row["role"], ("search", "user", "train"))
        self.assertTrue(all(m["verdict"] == "ok" for m in report["ai_matrix"]))

    def test_nothing_fires_when_every_agent_is_served(self):
        self._run(None)
        for title in ("AI training crawlers are refused at the edge",
                      "AI search / assistant crawlers are refused at the edge",
                      "Site refuses every non-browser user-agent we tried",
                      "Site refuses non-browser user-agents"):
            with self.subTest(finding=title):
                self.assert_absent(title)


# =====================================================================================
# robots.txt scenarios
# =====================================================================================
class TestRobotsScenarios(FindingAssertions, unittest.TestCase):
    def _audit(self, **kw):
        self.site = FX.build_minimal_site(**kw)
        self.addCleanup(self.site.stop)
        report = audit(self.site.base + "/", max_pages=1, sweep=0)
        self.found = index_findings(report)
        return report

    def test_server_error_is_critical(self):
        report = self._audit(robots_status=500)
        fi = self.assert_finding("robots.txt returns a server error", "CRITICAL", "crawlability")
        self.assertIn("500", fi["observed"])
        self.assertFalse(report["robots_found"])

    def test_html_robots_txt(self):
        self._audit(robots_text="<!doctype html><html><body>Not found</body></html>",
                    robots_ctype="text/html; charset=utf-8")
        self.assert_finding("robots.txt is an HTML page", "HIGH", "crawlability")

    def test_oversized_robots_txt(self):
        self._audit(robots_text="# padding comment line\n" * 26000)
        fi = self.assert_finding("robots.txt exceeds 500 KiB", "MEDIUM", "crawlability")
        self.assertIn("KiB", fi["observed"])

    def test_blocking_a_search_crawler(self):
        self._audit(robots_text="User-agent: OAI-SearchBot\nDisallow: /\n")
        fi = self.assert_finding("robots.txt blocks AI search crawlers", "MEDIUM", "ai search")
        self.assertIn("OAI-SearchBot", fi["observed"])

    def test_blocking_only_training_crawlers(self):
        self._audit(robots_text="User-agent: GPTBot\nDisallow: /\n"
                                "User-agent: CCBot\nDisallow: /\n")
        fi = self.assert_finding("robots.txt blocks AI training crawlers", "LOW", "ai search")
        self.assertIn("GPTBot", fi["observed"])
        self.assert_absent("robots.txt blocks AI search crawlers")

    def test_allowing_everything_produces_no_ai_robots_finding(self):
        self._audit()
        self.assert_absent("robots.txt blocks AI")

    def test_content_signal_opt_outs(self):
        self._audit(robots_text="User-agent: *\nAllow: /\n"
                                "Content-Signal: search=no, ai-input=no\n")
        self.assert_finding("robots.txt AI-preference signal says search=no",
                            "HIGH", "crawlability")
        self.assert_finding("robots.txt AI-preference signal opts out of AI answers",
                            "MEDIUM", "ai search")

    def test_content_usage_response_header_on_robots_txt(self):
        self._audit(robots_headers={"Content-Usage": "ai-use=n"})
        self.assert_finding("robots.txt AI-preference signal opts out of AI answers",
                            "MEDIUM", "ai search")

    def test_cloudflare_managed_block_is_info(self):
        self._audit(robots_text="# BEGIN Cloudflare Managed content\n"
                                "User-agent: *\nAllow: /\n"
                                "# END Cloudflare Managed content\n")
        self.assert_finding("robots.txt is partly written by the CDN", "INFO", "crawlability")

    def test_a_missing_llms_txt_is_never_reported(self):
        report = self._audit()
        self.assert_absent("No llms.txt")
        self.assertIn(("llms.txt",
                       "not present (no action needed; AI crawlers are not observed "
                       "requesting it)"), report["health"])


# =====================================================================================
# check_exposed_files: the catch-all control
# =====================================================================================
class TestExposedFiles(unittest.TestCase):
    def test_real_git_metadata_is_flagged(self):
        site = FX.build_broken_site()
        self.addCleanup(site.stop)
        out = SA.check_exposed_files(site.base + "/")
        self.assertEqual([f["title"] for f in out],
                         ["Private files are publicly downloadable"])
        self.assertEqual(out[0]["severity"], "CRITICAL")
        self.assertEqual(out[0]["category"], "trust")

    def test_a_catch_all_route_is_not_flagged(self):
        # Every path answers 200 with the same body — including one that happens to
        # look exactly like a .git/HEAD file.
        site = FX.build_catchall_site("ref: refs/heads/main\n")
        self.addCleanup(site.stop)
        self.assertEqual(SA.check_exposed_files(site.base + "/"), [])

    def test_a_catch_all_html_shell_is_not_flagged(self):
        site = FX.build_catchall_site(
            "<!doctype html><html><body><h1>App</h1></body></html>",
            ctype="text/html; charset=utf-8")
        self.addCleanup(site.stop)
        self.assertEqual(SA.check_exposed_files(site.base + "/"), [])

    def test_nothing_exposed_on_the_clean_site(self):
        site = FX.build_clean_site()
        self.addCleanup(site.stop)
        self.assertEqual(SA.check_exposed_files(site.base + "/"), [])


# =====================================================================================
# read_sitemaps against the fixture
# =====================================================================================
class TestReadSitemaps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = FX.build_broken_site()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def read(self, path):
        return SA.read_sitemaps([self.site.base + path])

    def test_the_main_sitemap(self):
        meta = self.read("/sitemap.xml")
        self.assertEqual(meta["total"], len(meta["urls"]))
        self.assertEqual(meta["with_lastmod"], 5)
        self.assertFalse(meta["truncated"])
        self.assertEqual(meta["files_read"], 1)

    def test_every_lastmod_identical(self):
        meta = self.read("/sitemap-same-lastmod.xml")
        self.assertEqual(meta["with_lastmod"], 25)
        self.assertEqual(set(meta["lastmods"]), {"2026-04-04"})
        titles = [f["title"] for f in SA.sitemap_meta_findings(meta)]
        self.assertIn("Every sitemap <lastmod> is the same date", titles)

    def test_empty_sitemap(self):
        meta = self.read("/sitemap-empty.xml")
        self.assertEqual(meta["urls"], [])
        self.assertEqual(meta["total"], 0)
        self.assertEqual(SA.sitemap_meta_findings(meta), [])

    def test_future_lastmod(self):
        meta = self.read("/sitemap-future.xml")
        titles = [f["title"] for f in SA.sitemap_meta_findings(meta)]
        self.assertIn("Sitemap <lastmod> dates in the future", titles)

    def test_index_larger_than_the_file_budget_sets_truncated(self):
        meta = self.read("/sitemap-big-index.xml")
        self.assertTrue(meta["truncated"])
        self.assertEqual(meta["files_listed"], 30)
        self.assertGreater(meta["files_listed"], SA.MAX_SITEMAP_FILES)
        self.assertLessEqual(meta["files_read"], SA.MAX_SITEMAP_FILES)

    def test_a_missing_sitemap_reads_as_empty(self):
        meta = self.read("/no-such-sitemap.xml")
        self.assertEqual(meta["urls"], [])
        self.assertEqual(meta["files_read"], 0)


# =====================================================================================
# Entity transparency: an address on a page the sample never crawls
# =====================================================================================
class TestPostalAddressDiscovery(FindingAssertions, unittest.TestCase):
    TITLE = "No postal address found anywhere on the crawled pages"

    def _audit(self, with_address):
        site = FX.build_address_site(with_address=with_address)
        self.addCleanup(site.stop)
        report = audit(site.base + "/", max_pages=1, sweep=0)
        self.found = index_findings(report)
        return report

    def test_address_only_on_an_uncrawled_about_page_is_still_found(self):
        report = self._audit(with_address=True)
        self.assertEqual([p["url"] for p in report["pages"]],
                         [report["url"]])          # only the homepage was crawled
        self.assert_absent(self.TITLE)
        self.assertIn(("Entity transparency", "postal address ✓ · legal entity named ✓"),
                      report["health"])

    def test_no_address_anywhere_is_reported(self):
        self._audit(with_address=False)
        self.assert_finding(self.TITLE, "LOW", "trust")


# =====================================================================================
# Sites that must not crash the auditor
# =====================================================================================
class TestAwkwardSites(unittest.TestCase):
    def _site(self):
        s = FX.FixtureServer().start()
        self.addCleanup(s.stop)
        return s

    def _finish(self, report):
        self.assertIsInstance(json.dumps(report, default=str), str)
        self.assertIsInstance(RP.render_html(report), str)
        return {f["title"] for f in index_findings(report)}

    def test_no_sitemap_unicode_uppercase_markup_and_broken_jsonld(self):
        s = self._site()
        B = s.base
        s.add("/robots.txt", FX.route("User-agent: *\nAllow: /\n", ctype=FX.TEXT_CTYPE))
        s.add("/social.png", FX.route(FX.make_png(1200, 630), ctype=FX.PNG_CTYPE))
        s.add("/", FX.route(
            '<!DOCTYPE HTML><HTML LANG="fr"><HEAD><META CHARSET="utf-8">'
            "<TITLE>Bienvenue sur le site de démonstration 中文 \U0001F600</TITLE>"
            '<META NAME="DESCRIPTION" CONTENT="Découvrez le site de démonstration '
            'avec des caractères accentués et un emoji pour tester lencodage.">'
            '<LINK REL="CANONICAL">'                       # canonical with no href
            '<META PROPERTY="OG:IMAGE" CONTENT="social.png">'   # relative og:image
            '<META NAME="VIEWPORT" CONTENT="width=device-width, initial-scale=1">'
            '<SCRIPT TYPE="APPLICATION/LD+JSON">[{"@type":"Article","headline":"x"},'
            '{"@type":["WebPage","Thing"]},"a bare string"]</SCRIPT>'
            '<SCRIPT TYPE="application/ld+json">{not json}</SCRIPT>'
            "</HEAD><BODY><H1>Démonstration 中文</H1>"
            '<IMG SRC="/social.png" WIDTH="100%" HEIGHT="AUTO" ALT="Une image">'
            "<P>" + FX.prose(320, seed=1) + " Caractères: éàùç "
            "中文 \U0001F600.</P>"
            '<A HREF="/page">Autre page</A></BODY></HTML>'))
        s.add("/page", FX.route(FX.page_html(
            "Deuxieme page du site de demonstration francais",
            "<p>" + FX.prose(300, seed=2) + "</p>",
            description="Explorez la deuxieme page du site de demonstration pour verifier lencodage.",
            canonical=B + "/page", og_image=B + "/social.png")))
        got = self._finish(audit(B + "/"))
        self.assertIn("XML sitemap not found", got)
        self.assertIn("Invalid JSON-LD", got)
        self.assertIn("Missing canonical tag", got)      # rel=canonical with no href
        self.assertNotIn("No Open Graph image", got)     # the relative og:image resolved

    def test_empty_sitemap(self):
        s = self._site()
        B = s.base
        s.add("/robots.txt", FX.route("User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % B,
                                      ctype=FX.TEXT_CTYPE))
        s.add("/sitemap.xml", FX.route(
            '<?xml version="1.0"?><urlset '
            'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>',
            ctype=FX.XML_CTYPE))
        s.add("/", FX.route(FX.page_html(
            "Site whose XML sitemap file contains no URLs",
            "<p>" + FX.prose(320, seed=3) + '</p><a href="/page">Next page</a>',
            description="Read about the site whose XML sitemap file holds no URL entries at all.",
            canonical=B + "/")))
        s.add("/page", FX.route(FX.page_html(
            "Second page of the empty sitemap fixture site",
            "<p>" + FX.prose(300, seed=4) + "</p>",
            description="Explore the second page of the fixture site with the empty sitemap.",
            canonical=B + "/page")))
        report = audit(B + "/")
        got = self._finish(report)
        self.assertIn("Sitemap contains no URLs", got)
        self.assertEqual(report["coverage"], [])
        self.assertFalse(report["sitemap_truncated"])
        # the crawl fell back to following homepage links
        self.assertEqual(len(report["pages"]), 2)

    def test_homepage_that_redirects(self):
        s = self._site()
        B = s.base
        s.add("/robots.txt", FX.route("User-agent: *\nAllow: /\n", ctype=FX.TEXT_CTYPE))
        s.add("/", FX.route(b"", status=301, ctype=None, location="/home"))
        s.add("/home", FX.route(FX.page_html(
            "Landing page reached after the homepage redirect",
            "<p>" + FX.prose(320, seed=5) + "</p>",
            description="Read the landing page the root URL redirects to with a single hop.",
            canonical=B + "/home")))
        report = audit(B + "/")
        got = self._finish(report)
        self.assertIn("Page redirects", got)
        self.assertEqual(report["pages"][0]["final_url"], B + "/home")

    # ---- BUGS ----------------------------------------------------------------------
    def test_a_numeric_jsonld_type_does_not_abort_the_audit(self):
        """ANALYZER BUG: one malformed JSON-LD block aborts the entire audit.

        scripts/seo_audit.py lines 2588-2590:
            def _types(node):
                t = node.get("@type")
                return set([t] if isinstance(t, str) else [str(x) for x in (t or [])])

        `"@type": 5` (or `true`) makes the comprehension iterate a non-iterable and
        raise TypeError. _types() is called unguarded from check_schema_depth
        (line 2600), check_answer_readiness and check_entity_transparency, all inside
        run_audit, so run_audit propagates the TypeError and the user gets no report
        at all. parse_jsonld_types() and _google_signals.types_of() in the same file
        already handle this shape. Patch:
            def _types(node):
                t = node.get("@type")
                if isinstance(t, str):
                    return {t}
                if isinstance(t, (list, tuple, set)):
                    return {str(x) for x in t}
                return set()
        """
        s = self._site()
        B = s.base
        s.add("/robots.txt", FX.route("User-agent: *\nAllow: /\n", ctype=FX.TEXT_CTYPE))
        s.add("/", FX.route(FX.page_html(
            "Homepage carrying one malformed structured data block",
            "<p>" + FX.prose(320, seed=6) + "</p>",
            description="Read the homepage whose JSON-LD carries a numeric at-type by mistake.",
            canonical=B + "/",
            jsonld='{"@context":"https://schema.org","@type":5,"name":"Acme"}')))
        report = audit(B + "/", max_pages=1)
        self.assertEqual(report["page_count"], 1)

    def test_a_truncated_share_image_does_not_abort_the_audit(self):
        """ANALYZER BUG: a malformed og:image aborts the entire audit.

        scripts/seo_audit.py lines 463-466 and 470-477:
            except Exception:
                dims = _manual_dims(resp.body)
        ...
            if b[:8] == b"\x89PNG\r\n\x1a\n":
                w, h = struct.unpack(">II", b[16:24])      # line 474
            if b[:6] in (b"GIF87a", b"GIF89a"):
                w, h = struct.unpack("<HH", b[6:10])       # line 477

        _manual_dims runs inside image_info's `except` handler, so the struct.error it
        raises on a short body is not caught: a file with a PNG signature and fewer than
        24 bytes (or a GIF signature and fewer than 10) escapes as
        `struct.error: unpack requires a buffer of 8 bytes`. image_info is called
        unguarded by analyze_social (for BOTH og:image and the favicon) and by
        check_page_share_image, so one truncated image file kills the whole run.
        Patch (seo_audit.py line 464):
            except Exception:
                try:
                    dims = _manual_dims(resp.body)
                except Exception:
                    dims = None
        """
        s = self._site()
        B = s.base
        s.add("/robots.txt", FX.route("User-agent: *\nAllow: /\n", ctype=FX.TEXT_CTYPE))
        s.add("/broken.png", FX.route(b"\x89PNG\r\n\x1a\nshort", ctype=FX.PNG_CTYPE))
        s.add("/", FX.route(FX.page_html(
            "Homepage whose share image is a truncated png file",
            "<p>" + FX.prose(320, seed=7) + "</p>",
            description="Read the homepage whose Open Graph image is a truncated PNG file.",
            canonical=B + "/", og_image=B + "/broken.png")))
        report = audit(B + "/", max_pages=1)
        self.assertEqual(report["page_count"], 1)

    def test_a_site_where_every_page_404s(self):
        s = self._site()
        s.add("/robots.txt", FX.route("User-agent: *\nAllow: /\n", ctype=FX.TEXT_CTYPE))
        report = audit(s.base + "/", max_pages=2)
        got = self._finish(report)
        self.assertIn("Client error 404", got)
        self.assertEqual(report["pages"][0]["status"], 404)
        # a dead front door must lead the report and sink confidence, not hide in a page table
        self.assertIn("The homepage does not load", got)
        top = [f for f in report["site_findings"] if f["title"] == "The homepage does not load"][0]
        self.assertEqual(top["severity"], "CRITICAL")
        self.assertEqual(report["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
