"""Tests for --include-pages: operator-named pages forced into the deep-page set.

discover_pages is tested with fetch stubbed out, so nothing leaves the machine. One
end-to-end test runs run_audit against a tiny local fixture site.

Run with:  python3 -m unittest discover -s tests -v   (from the repo root)
"""

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seo_audit as SA                                            # noqa: E402
import fixture_site as FX                                         # noqa: E402

BASE = "https://e.com/"
SITEMAP = ["https://e.com/blog/a", "https://e.com/blog/b", "https://e.com/blog/c",
           "https://e.com/shop/x", "https://e.com/shop/y", "https://e.com/about"]
HOME_HTML = ('<html><body><a href="/contact">Contact</a><a href="/team">Team</a>'
             '<a href="https://other.com/x">Elsewhere</a></body></html>')


def stub_fetch(html=HOME_HTML):
    """fetch() returning the homepage HTML, for discover_pages' link fallback."""
    return mock.patch.object(SA, "fetch", return_value=SA.Response(
        BASE, BASE, 200, {"Content-Type": "text/html"}, html.encode("utf-8"), 5.0))


def discover(max_pages, sm_urls=SITEMAP, include=None, **kw):
    with stub_fetch():
        if include is None:
            return SA.discover_pages(BASE, max_pages, sm_urls, **kw)
        return SA.discover_pages(BASE, max_pages, sm_urls, include)


class TestResolveIncludePages(unittest.TestCase):
    def test_relative_paths_resolve_against_the_base(self):
        ok, bad = SA.resolve_include_pages(BASE, ["/apply", "winners", "/a/b?x=1#frag"])
        self.assertEqual(ok, ["https://e.com/apply", "https://e.com/winners",
                              "https://e.com/a/b?x=1"])
        self.assertEqual(bad, [])

    def test_full_urls_on_the_site_are_kept(self):
        ok, _ = SA.resolve_include_pages(BASE, ["https://e.com/apply",
                                                "https://www.e.com/winners"])
        self.assertEqual(ok, ["https://e.com/apply", "https://www.e.com/winners"])

    def test_off_site_and_non_web_entries_are_rejected(self):
        ok, bad = SA.resolve_include_pages(BASE, ["https://other.com/apply", "//cdn.net/x",
                                                  "mailto:hi@e.com", "/ok"])
        self.assertEqual(ok, ["https://e.com/ok"])
        self.assertEqual(bad, ["https://other.com/apply", "//cdn.net/x", "mailto:hi@e.com"])

    def test_homepage_duplicates_and_blanks_are_dropped(self):
        ok, bad = SA.resolve_include_pages(BASE, ["/", BASE, "https://e.com", "/apply",
                                                  "/apply/", "https://e.com/apply", "", "  "])
        self.assertEqual(ok, ["https://e.com/apply"])
        self.assertEqual(bad, [])

    def test_none_is_empty(self):
        self.assertEqual(SA.resolve_include_pages(BASE, None), ([], []))


class TestDiscoverPagesWithIncludes(unittest.TestCase):
    def test_seeds_come_right_after_the_homepage(self):
        got = discover(5, include=["/apply", "https://e.com/winners"])
        self.assertEqual(got[:3], [(BASE, "homepage"), ("https://e.com/apply", "include"),
                                   ("https://e.com/winners", "include")])
        self.assertEqual(len(got), 5)
        self.assertTrue(all(src == "sitemap" for _, src in got[3:]))

    def test_seeds_are_not_repeated_by_the_sitemap_sample(self):
        got = discover(20, include=["/about", "/blog/a/"])
        urls = [u.rstrip("/") for u, _ in got]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(got[1:3], [("https://e.com/about", "include"),
                                    ("https://e.com/blog/a/", "include")])

    def test_homepage_and_duplicate_seeds_appear_once(self):
        got = discover(4, include=["/", BASE, "/apply", "/apply", "https://e.com/apply/"])
        self.assertEqual([u for u, s in got if s == "homepage"], [BASE])
        self.assertEqual([u for u, s in got if s == "include"], ["https://e.com/apply"])
        self.assertEqual(len(got), 4)

    def test_off_site_seeds_are_not_audited(self):
        got = discover(3, include=["https://other.com/apply", "/apply"])
        self.assertNotIn("https://other.com/apply", [u for u, _ in got])
        self.assertEqual(got[1], ("https://e.com/apply", "include"))

    def test_max_pages_grows_to_fit_every_seed(self):
        seeds = ["/s1", "/s2", "/s3", "/s4"]
        got = discover(2, include=seeds)
        self.assertEqual(got, [(BASE, "homepage")] +
                         [("https://e.com" + s, "include") for s in seeds])

    def test_max_pages_counts_only_accepted_seeds(self):
        got = discover(1, include=["/s1", "/s1", "https://other.com/x"])
        self.assertEqual(got, [(BASE, "homepage"), ("https://e.com/s1", "include")])

    def test_seeds_share_max_pages_with_the_sample_when_it_is_larger(self):
        got = discover(4, include=["/apply"])
        self.assertEqual([s for _, s in got], ["homepage", "include", "sitemap", "sitemap"])

    def test_link_fallback_still_fills_after_seeds(self):
        got = discover(5, sm_urls=[], include=["/apply"])
        self.assertEqual(got, [(BASE, "homepage"), ("https://e.com/apply", "include"),
                               ("https://e.com/contact", "link"), ("https://e.com/team", "link")])

    def test_no_seeds_is_unchanged(self):
        for n in (0, 1, 3, 7, 20):
            base = discover(n)
            self.assertEqual(discover(n, include=[]), base)
            self.assertEqual(discover(n, include=["https://other.com/x", " "]), base)
            self.assertFalse(any(s == "include" for _, s in base))
        self.assertEqual(discover(0), [])
        self.assertEqual(discover(3)[0], (BASE, "homepage"))


class TestIncludedNoindexPagesKeepFindings(unittest.TestCase):
    def test_included_noindex_page_is_not_demoted(self):
        noisy = [{"severity": "HIGH", "title": "Missing H1"}]
        pages = [{"url": BASE, "source": "homepage", "status": 200, "robots": "", "findings": []},
                 {"url": "https://e.com/apply", "source": "include", "status": 200,
                  "robots": "noindex", "findings": list(noisy)},
                 {"url": "https://e.com/login", "source": "link", "status": 200,
                  "robots": "noindex", "findings": list(noisy)}]
        changed = SA.demote_linked_noindex(pages)
        self.assertEqual([p["url"] for p in changed], ["https://e.com/login"])
        self.assertEqual(pages[1]["findings"], noisy)
        self.assertNotIn("utility_noindex", pages[1])


class TestIncludePagesFlag(unittest.TestCase):
    """main() splits commas, accepts the flag repeatedly, and passes [] when it is absent."""

    class Stop(Exception):
        pass

    def parsed(self, *argv):
        seen = {}

        def fake_run_audit(*a, **kw):
            seen.update(kw)
            raise self.Stop

        with mock.patch.object(sys, "argv", ["seo_audit.py", "https://e.com", *argv]), \
                mock.patch.object(SA, "run_audit", fake_run_audit), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(self.Stop):
                SA.main()
        return seen["include_pages"]

    def test_repeatable_and_comma_separated(self):
        self.assertEqual(self.parsed("--include-pages", "/apply, /winners,",
                                     "--include-pages", "https://e.com/hub"),
                         ["/apply", "/winners", "https://e.com/hub"])

    def test_absent_flag_passes_nothing(self):
        self.assertEqual(self.parsed(), [])


class TestRunAuditIncludePages(unittest.TestCase):
    """A noindexed /apply that is neither in the sitemap nor linked is audited in full."""

    @classmethod
    def setUpClass(cls):
        s = cls.site = FX.FixtureServer().start()
        B = s.base

        def page(title, path, robots=None):
            return FX.route(FX.page_html(title, "<h1>%s</h1><p>%s</p>" % (title, FX.prose(300, path)),
                                         description="A fixture page used by the include tests.",
                                         canonical=B + path, robots=robots))

        s.add("/", page("Fixture home page for include tests", "/"))
        for i in range(4):
            s.add("/blog/%d" % i, page("Fixture blog post number %d" % i, "/blog/%d" % i))
        s.add("/apply", page("Apply to the fixture programme", "/apply", robots="noindex"))
        s.add("/robots.txt", FX.route("User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % B,
                                      ctype="text/plain"))
        s.add("/sitemap.xml", FX.route(FX._sitemap(
            [("%s/blog/%d" % (B, i), "2026-01-01") for i in range(4)]),
            ctype="application/xml"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cls.report = SA.run_audit(B + "/", max_pages=1, sweep=0, use_pagespeed=False,
                                      probe_ai=False, network_checks=False,
                                      include_pages=["/apply", "http://elsewhere.test/x"])
        cls.stderr = err.getvalue()

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def test_apply_is_the_second_deep_page(self):
        pages = self.report["pages"]
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1]["url"], self.site.base + "/apply")
        self.assertEqual(pages[1]["source"], "include")
        self.assertNotIn("utility_noindex", pages[1])

    def test_off_site_entry_is_reported_and_skipped(self):
        self.assertIn("elsewhere.test/x", self.stderr)
        self.assertNotIn("elsewhere.test", " ".join(p["url"] for p in self.report["pages"]))


if __name__ == "__main__":
    unittest.main()
