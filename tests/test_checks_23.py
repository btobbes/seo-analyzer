"""Tests for the false positives fixed in 2.3.0.

Each case came out of the 2026-11-24 audit of a 20-URL prerendered single-page app, run
from a cloud sandbox whose outbound HTTPS goes through an intercepting proxy. The report
carried three findings that were about the tool or its surroundings, not the site: a
"TLS certificate expires within 30 days" that was the proxy's own certificate, a missing
Content-Security-Policy on a site that ships its policy in a <meta> tag, and four HIGH or
MEDIUM findings per sign-in page (noindex, no H1, thin, duplicate title) for pages the
owner had deliberately kept out of search. The soft-404 finding now also says what the
200 body claims. Every change has a firing case and a silent case.

Run with:  python3 -m unittest tests.test_checks_23 -v   (from the repo root). No network.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import seo_audit as SA                                            # noqa: E402

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


class FakeResp:
    def __init__(self, headers=None, status=200, text="", final_url="https://example.com/x"):
        self.headers, self.status, self.text, self.final_url = headers or {}, status, text, final_url


class TestMetaCsp(unittest.TestCase):
    def test_header_missing_and_no_meta_is_reported(self):
        self.assertIn("Content-Security-Policy", SA.missing_security_headers(FakeResp()))

    def test_meta_policy_counts_as_present(self):
        metas = [{"content": "default-src 'self'", "http-equiv": "Content-Security-Policy"}]
        missing = SA.missing_security_headers(FakeResp(), metas)
        self.assertNotIn("Content-Security-Policy", missing)
        self.assertIn("X-Frame-Options", missing)        # a meta policy cannot set frame-ancestors

    def test_empty_meta_policy_does_not_count(self):
        metas = [{"content": " ", "http-equiv": "content-security-policy"}]
        self.assertIn("Content-Security-Policy", SA.missing_security_headers(FakeResp(), metas))

    def test_parser_keeps_http_equiv_metas(self):
        p = SA.PageParser()
        p.feed('<meta content="default-src \'self\'" http-equiv="Content-Security-Policy">')
        self.assertNotIn("Content-Security-Policy", SA.missing_security_headers(FakeResp(), p.metas))


class TestLinkedNoindex(unittest.TestCase):
    def pages(self):
        noisy = [SA.f("HIGH", "Page set to noindex", "crawlability", 4, 1, "x", "y"),
                 SA.f("HIGH", "Missing H1 tag", "on page", 5, 1, "x", "y")]
        return [
            {"url": "https://e.com/", "source": "seed", "status": 200, "robots": "noindex", "findings": list(noisy)},
            {"url": "https://e.com/auth", "source": "link", "status": 200, "robots": "noindex, follow",
             "findings": list(noisy)},
            {"url": "https://e.com/old", "source": "sitemap", "status": 200, "robots": "noindex",
             "findings": list(noisy)},
            {"url": "https://e.com/about", "source": "link", "status": 200, "robots": "index,follow",
             "findings": list(noisy)},
            {"url": "https://e.com/gone", "source": "link", "status": 404, "robots": "noindex",
             "findings": list(noisy)},
        ]

    def test_only_linked_200_noindex_pages_are_demoted(self):
        pages = self.pages()
        changed = SA.demote_linked_noindex(pages)
        self.assertEqual([p["url"] for p in changed], ["https://e.com/auth"])
        auth = pages[1]
        self.assertTrue(auth["utility_noindex"])
        self.assertEqual([(x["severity"], x["title"]) for x in auth["findings"]],
                         [("INFO", "Noindexed page reached by a link (not in the sitemap)")])

    def test_homepage_sitemap_and_error_pages_keep_their_findings(self):
        pages = self.pages()
        SA.demote_linked_noindex(pages)
        for i in (0, 2, 3, 4):       # a noindexed homepage or sitemap URL is a real defect
            self.assertEqual(len(pages[i]["findings"]), 2, pages[i]["url"])
            self.assertNotIn("utility_noindex", pages[i])


class TestNofollowedNoindexLinks(unittest.TestCase):
    """A sign-up button with rel=nofollow that leads to a noindexed auth page is the site doing
    the right thing. Only followed links to noindexed pages are a finding."""

    def run_arch(self, rel):
        parser = SA.PageParser()
        parser.feed("".join('<a href="/auth?next=%d"%s>Start</a>' % (i, rel) for i in range(6)))
        pages = [{"url": "https://e.com/", "final_url": "https://e.com/", "parser": parser}]
        resp = SA.Response("u", "https://e.com/auth", 200, {"content-type": "text/html"},
                           b'<html><head><meta name="robots" content="noindex, follow"></head></html>', 5)
        with mock.patch.object(SA, "fetch", return_value=resp):
            findings, _, summary = SA.check_link_architecture(pages, ["https://e.com/"], [])
        return [x["title"] for x in findings], summary

    def test_followed_links_to_noindex_fire(self):
        titles, summary = self.run_arch("")
        self.assertEqual(titles, ["Internal links lead to noindexed pages (6 of 6 sampled)"])

    def test_nofollowed_links_to_noindex_are_silent(self):
        titles, summary = self.run_arch(' rel="nofollow noopener"')
        self.assertEqual(titles, [])
        self.assertEqual(summary.get("noindexed"), 0)

    def test_parser_reads_rel_tokens(self):
        p = SA.PageParser()
        p.feed('<a href="/a" rel="NoFollow">a</a><a href="/b" rel="noopener">b</a><a href="/c" rel="ugc">c</a>')
        self.assertEqual(p.nofollow_links, {"/a", "/c"})


class TestTlsThroughProxy(unittest.TestCase):
    def clean_env(self):
        return {k: v for k, v in os.environ.items() if k not in PROXY_VARS}

    def test_detects_a_proxy(self):
        with mock.patch.dict(os.environ, self.clean_env(), clear=True):
            self.assertFalse(SA.tls_seen_through_proxy())
            os.environ["https_proxy"] = "http://127.0.0.1:3128"
            self.assertTrue(SA.tls_seen_through_proxy())

    def cert_in_days(self, days):
        import datetime as dt
        exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days, hours=1)
        return {"notAfter": exp.strftime("%b %d %H:%M:%S %Y GMT"),
                "issuer": ((("organizationName", "Sandbox Proxy CA"),),)}

    def run_check(self, days):
        ssock = mock.MagicMock()
        ssock.__enter__.return_value.getpeercert.return_value = self.cert_in_days(days)
        ctx = mock.MagicMock()
        ctx.wrap_socket.return_value = ssock
        with mock.patch.object(SA.socket, "create_connection"), \
                mock.patch.object(SA.ssl, "create_default_context", return_value=ctx):
            return SA.tls_certificate("example.com")

    def test_short_lived_proxy_certificate_is_not_a_finding(self):
        with mock.patch.dict(os.environ, dict(self.clean_env(), HTTPS_PROXY="http://proxy:3128"), clear=True):
            findings, health = self.run_check(20)
        self.assertEqual(findings, [])
        self.assertIn("not measured", health[1])
        self.assertIn("Sandbox Proxy CA", health[1])

    def test_same_certificate_without_a_proxy_is_reported(self):
        with mock.patch.dict(os.environ, self.clean_env(), clear=True):
            findings, health = self.run_check(20)
        self.assertEqual([x["title"] for x in findings], ["TLS certificate expires within 30 days"])


class TestSoft404Says(unittest.TestCase):
    def probe(self, body, status=200):
        resp = FakeResp(status=status, text=body, final_url="https://example.com/seo-audit-404-probe-x9q3z7/")
        with mock.patch.object(SA, "fetch", return_value=resp):
            return SA.check_custom_404("https://example.com/")

    def test_reports_the_canonical_the_shell_carries(self):
        findings, health = self.probe('<head><link href="https://example.com/" rel="canonical"></head>')
        self.assertEqual(findings[0]["title"], "Missing pages return HTTP 200 (soft 404)")
        self.assertIn("the body's canonical is https://example.com/", findings[0]["observed"])

    def test_reports_a_noindex_shell(self):
        findings, _ = self.probe('<meta name="robots" content="noindex, follow"><link rel="canonical" href="/x">')
        self.assertIn("the body carries noindex", findings[0]["observed"])

    def test_reports_a_bare_shell(self):
        findings, _ = self.probe("<html><body>app</body></html>")
        self.assertIn("no canonical and no noindex", findings[0]["observed"])

    def test_a_real_404_is_silent(self):
        findings, health = self.probe("not found", status=404)
        self.assertEqual(findings, [])
        self.assertEqual(health, ("404 handling", "correct (HTTP 404)"))


if __name__ == "__main__":
    unittest.main()
