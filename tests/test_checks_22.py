"""Tests for the false positives fixed in 2.2.0.

Each case came out of a hand-verified audit of a six-page site (2026-10-12) where the
tool's findings were wrong or noisy: decorative alt="" images reported as missing alt,
"No analytics detected" on a site that loads GA4, the Meta Pixel and the LinkedIn
Insight Tag from a first-party script, privacy-policy and product pages listed as social
profiles, a BreadcrumbList finding on a site with no hierarchy, a "lacks a CTA verb"
finding on five of six pages (privacy policy and terms included), and a CSP block of the
Cloudflare Web Analytics beacon reported without saying what the script was. Every
change has a firing case and a silent case.

Run with:  python3 -m unittest tests.test_checks_22 -v   (from the repo root)
No network: the only sockets opened are to the in-process fixture server.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seo_audit as SA                                            # noqa: E402
import fixture_site as FX                                         # noqa: E402
from test_units import make_page, html_doc, titles                # noqa: E402

DECORATIVE = 'All images marked decorative (alt="")'
MISSING_ALT = "Images missing alt text"


# =====================================================================================
# Alt text: alt="" is decorative, not missing
# =====================================================================================
class TestAltParser(unittest.TestCase):
    def test_empty_and_absent_alt_are_counted_apart(self):
        p = SA.PageParser()
        p.feed('<img src="/a.png" alt=""><img src="/b.png" alt><img src="/c.png">'
               '<img src="/d.png" alt="A chart of sales">')
        self.assertEqual(p.img_total, 4)
        self.assertEqual(p.img_missing_alt, 1)
        self.assertEqual(p.img_empty_alt, 2)

    def test_whitespace_alt_counts_as_empty(self):
        p = SA.PageParser()
        p.feed('<img src="/a.png" alt="  ">')
        self.assertEqual((p.img_missing_alt, p.img_empty_alt), (0, 1))


class TestAltFindings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = FX.FixtureServer().start()
        cls.site = s
        B = s.base

        def add(path, imgs):
            body = "<main><p>" + FX.prose(320, seed=7) + "</p>%s</main>" % imgs
            s.add(path, FX.route(FX.page_html("Fixture page about decorative images " + path,
                                              body, description="A page used to test alt text "
                                              "handling on images of several kinds.",
                                              canonical=B + path)))

        img = '<img src="/i%d.png" width="10" height="10" %s>'
        add("/decorative", "".join(img % (i, 'alt=""') for i in range(4)))
        add("/mixed", "".join(img % (i, 'alt=""') for i in range(3)) + img % (9, 'alt="Team photo"'))
        add("/two", "".join(img % (i, 'alt=""') for i in range(2)))
        add("/missing", img % (1, "") + img % (2, "") + img % (3, 'alt=""'))

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def test_all_decorative_is_info_not_missing(self):
        _page, findings = SA.analyze_page(self.site.url("/decorative"))
        self.assertNotIn(MISSING_ALT, titles(findings))
        fi = [x for x in findings if x["title"] == DECORATIVE]
        self.assertEqual(len(fi), 1)
        self.assertEqual(fi[0]["severity"], "INFO")
        self.assertEqual(fi[0]["observed"], '4/4 images carry alt=""')

    def test_one_real_alt_silences_the_note(self):
        _page, findings = SA.analyze_page(self.site.url("/mixed"))
        self.assertNotIn(DECORATIVE, titles(findings))
        self.assertNotIn(MISSING_ALT, titles(findings))

    def test_fewer_than_three_images_is_silent(self):
        _page, findings = SA.analyze_page(self.site.url("/two"))
        self.assertNotIn(DECORATIVE, titles(findings))

    def test_absent_alt_still_fires(self):
        _page, findings = SA.analyze_page(self.site.url("/missing"))
        fi = [x for x in findings if x["title"] == MISSING_ALT]
        self.assertEqual(len(fi), 1)
        self.assertEqual(fi[0]["observed"], "2/3 images without alt")
        self.assertEqual(fi[0]["severity"], "MEDIUM")
        self.assertNotIn(DECORATIVE, titles(findings))


# =====================================================================================
# Analytics behind a first-party loader
# =====================================================================================
class TestAnalyticsDetection(unittest.TestCase):
    def _detect(self, head="", body="<p>body</p>"):
        return SA.detect_analytics(make_page(html_doc(body, head=head)))

    def test_first_party_loader_with_data_attributes(self):
        findings, row = self._detect(
            '<script src="/assets/js/analytics.js" data-ga4="G-AB12CD34EF" '
            'data-meta="1638543396172308" data-linkedin="8709602" defer></script>')
        self.assertEqual(findings, [])
        self.assertEqual(row[0], "Analytics")
        for name in ("GA4 measurement ID", "Meta Pixel", "LinkedIn Insight Tag"):
            self.assertIn(name, row[1])

    def test_vendor_hosts_and_calls(self):
        _f, row = self._detect(
            '<script>fbq("init","1")</script>'
            '<script src="https://snap.licdn.com/li.lms-analytics/insight.min.js"></script>'
            '<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
            'data-cf-beacon="{}"></script>'
            '<script defer src="https://cloud.umami.is/script.js" '
            'data-website-id="94db1cb1-74f4-4a40-ad6c-962362670409"></script>')
        for name in ("Meta Pixel", "LinkedIn Insight Tag", "Cloudflare Web Analytics", "Umami"):
            self.assertIn(name, row[1])

    def test_gtag_and_its_id_are_reported_once(self):
        _f, row = self._detect(
            '<script async src="https://www.googletagmanager.com/gtag/js?id=G-AB12CD34EF"></script>'
            "<script>gtag('config','G-AB12CD34EF')</script>")
        self.assertEqual(row[1], "GA4 (gtag.js)")

    def test_lookalikes_do_not_count(self):
        findings, row = self._detect(
            '<link rel="stylesheet" href="/assets/G-AB12CD34EF.css">'
            '<script src="/assets/chunk.G-AB12CD34EF.js"></script>'
            '<script src="/assets/G-AB12CD34EF.js"></script>',
            '<a href="https://linkedin.com/in/jane" data-linkedin="https://linkedin.com/in/jane">'
            "Jane</a><p>Our broth is rich in umami. Model G-12 ships today.</p>")
        self.assertEqual(row[1], "none detected")
        self.assertEqual(titles(findings), ["No analytics detected"])
        self.assertIn("Meta/LinkedIn", findings[0]["observed"])
        self.assertIn("Cloudflare", findings[0]["observed"])
        self.assertIn("first-party script", findings[0]["fix"])
        self.assertIn("network panel", findings[0]["fix"])

    def test_leftover_ua_beside_cloudflare_is_not_ua_only(self):
        findings, _row = self._detect(
            "<script>ga('create','UA-12345-1')</script>"
            '<script src="https://static.cloudflareinsights.com/beacon.min.js"></script>')
        self.assertNotIn("Only deprecated Universal Analytics found", titles(findings))

    def test_ua_alone_still_fires(self):
        findings, _row = self._detect("<script>ga('create','UA-12345-1')</script>")
        self.assertEqual(titles(findings), ["Only deprecated Universal Analytics found"])


# =====================================================================================
# Footprint: a platform's own pages are not the business's profile
# =====================================================================================
class TestFootprintNonProfiles(unittest.TestCase):
    def _urls(self, hrefs):
        html = html_doc("".join('<a href="%s">x</a>' % h for h in hrefs))
        return [s["url"] for s in SA.extract_footprint([make_page(html)])["social"]]

    def test_legal_help_and_product_pages_are_skipped(self):
        urls = self._urls([
            "https://www.facebook.com/privacy/policy/",
            "https://www.linkedin.com/legal/privacy-policy",
            "https://github.com/features/copilot/ai-code-editor",
            "https://help.instagram.com/581066165581870",
            "https://x.com/en/privacy",
            "https://www.youtube.com/t/terms",
            "https://www.facebook.com/help/1234",
            "https://github.com/acme/tool/issues/4",
            "https://github.com/topics/seo",
            "https://x.com/hashtag/seo",
        ])
        self.assertEqual(urls, [])

    def test_real_profiles_are_kept(self):
        urls = self._urls([
            "https://www.facebook.com/acme/",
            "https://www.linkedin.com/company/acme",
            "https://github.com/acme",
            "https://github.com/acme/tool",
            "https://x.com/helpscout",
            "https://www.instagram.com/pressreader/",
        ])
        self.assertEqual(sorted(urls), sorted([
            "https://www.facebook.com/acme/", "https://www.linkedin.com/company/acme",
            "https://github.com/acme", "https://github.com/acme/tool",
            "https://x.com/helpscout", "https://www.instagram.com/pressreader/"]))

    def test_single_post_exclusions_from_2_1_still_hold(self):
        urls = self._urls(["https://www.facebook.com/acme/videos/123/",
                           "https://x.com/acme/status/1", "https://www.facebook.com/acme/"])
        self.assertEqual(urls, ["https://www.facebook.com/acme/"])


# =====================================================================================
# BreadcrumbList needs a hierarchy
# =====================================================================================
def _text_page(url, title="A perfectly ordinary fixture page title", desc=None, schema=""):
    head = ('<meta name="description" content="%s">' % desc if desc else "") + schema
    return make_page(html_doc("<p>%s</p>" % FX.prose(60, seed=3), head=head, title=title), url=url)


class TestBreadcrumbs(unittest.TestCase):
    TITLE = "No BreadcrumbList schema on any crawled page"

    def test_flat_site_is_silent(self):
        pages = [_text_page("https://e.com" + p) for p in ("/", "/team", "/privacy", "/pricing/")]
        self.assertEqual(SA.check_breadcrumbs(pages, pages), [])

    def test_nested_url_fires(self):
        pages = [_text_page("https://e.com" + p) for p in ("/", "/team", "/privacy")]
        swept = [_text_page("https://e.com/blog/first-post")]
        self.assertEqual(titles(SA.check_breadcrumbs(pages, pages + swept)), [self.TITLE])

    def test_breadcrumb_schema_present_is_silent(self):
        crumbs = ('<script type="application/ld+json">{"@context":"https://schema.org",'
                  '"@type":"BreadcrumbList","itemListElement":[]}</script>')
        pages = [_text_page("https://e.com" + p) for p in ("/", "/a/b", "/c")]
        pages[1] = _text_page("https://e.com/a/b", schema=crumbs)
        self.assertEqual(SA.check_breadcrumbs(pages, pages), [])


# =====================================================================================
# Meta-description call to action: one site-level finding
# =====================================================================================
class TestDescriptionCtas(unittest.TestCase):
    TITLE = "Most meta descriptions lack a call to action"
    PLAIN = "A boutique advisory firm working with founders on pricing and hiring decisions."
    CTA = "Book a free thirty-minute call with a partner to talk through your pricing."

    def test_most_marketing_pages_without_a_verb_fires_once(self):
        pages = [_text_page("https://e.com/", desc=self.PLAIN),
                 _text_page("https://e.com/team", desc=self.PLAIN),
                 _text_page("https://e.com/pricing", desc=self.PLAIN),
                 _text_page("https://e.com/contact", desc=self.CTA),
                 _text_page("https://e.com/privacy", desc=self.PLAIN),
                 _text_page("https://e.com/conditions", title="Terms of service", desc=self.PLAIN)]
        out = SA.check_description_ctas(pages)
        self.assertEqual(titles(out), [self.TITLE])
        self.assertEqual(out[0]["severity"], "LOW")
        self.assertEqual(out[0]["category"], "on page")
        self.assertTrue(out[0]["observed"].startswith("3 of 4 descriptions have no action verb"))
        self.assertIn("https://e.com/, https://e.com/team)", out[0]["observed"])
        self.assertNotIn("privacy", out[0]["observed"])

    def test_half_or_fewer_is_silent(self):
        pages = [_text_page("https://e.com/", desc=self.PLAIN),
                 _text_page("https://e.com/team", desc=self.PLAIN),
                 _text_page("https://e.com/pricing", desc=self.CTA),
                 _text_page("https://e.com/contact", desc=self.CTA)]
        self.assertEqual(SA.check_description_ctas(pages), [])

    def test_legal_pages_do_not_make_up_the_count(self):
        pages = [_text_page("https://e.com/", desc=self.PLAIN),
                 _text_page("https://e.com/team", desc=self.PLAIN),
                 _text_page("https://e.com/privacy", desc=self.PLAIN),
                 _text_page("https://e.com/cookies", desc=self.PLAIN),
                 _text_page("https://e.com/accessibility", desc=self.PLAIN)]
        self.assertEqual(SA.check_description_ctas(pages), [])

    def test_no_per_page_finding_any_more(self):
        site = FX.FixtureServer().start()
        try:
            site.add("/", FX.route(FX.page_html(
                "A fixture page whose description has no verb",
                "<main><p>" + FX.prose(320, seed=11) + "</p></main>",
                description=self.PLAIN, canonical=site.base + "/")))
            _page, findings = SA.analyze_page(site.url("/"))
        finally:
            site.stop()
        self.assertNotIn("Meta description lacks a CTA verb", titles(findings))
        self.assertNotIn(self.TITLE, titles(findings))


# =====================================================================================
# CSP: the Cloudflare Web Analytics beacon is named
# =====================================================================================
class TestCspCloudflareBeacon(unittest.TestCase):
    BEACON = ('<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
              'data-cf-beacon=\'{"token":"x"}\'></script>')

    def _page(self, csp, head):
        return make_page(html_doc("<p>body</p>", head=head), url="https://e.com/",
                         headers={"Content-Security-Policy": csp})

    def test_blocked_beacon_is_named_with_the_exact_fix(self):
        out = SA.check_csp_blocks(self._page("script-src 'self'", self.BEACON))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "MEDIUM")
        self.assertIn("Cloudflare Web Analytics beacon", out[0]["observed"])
        self.assertIn("https://cloudflareinsights.com to connect-src", out[0]["fix"])
        self.assertIn("Analytics & Logs > Web Analytics", out[0]["fix"])

    def test_other_hosts_are_listed_beside_the_beacon(self):
        out = SA.check_csp_blocks(self._page(
            "script-src 'self'", self.BEACON + '<script src="https://cdn.other.example/a.js"></script>'))
        self.assertIn("also blocked: cdn.other.example", out[0]["observed"])

    def test_other_blocked_host_keeps_the_generic_text(self):
        out = SA.check_csp_blocks(self._page(
            "script-src 'self'", '<script src="https://cdn.other.example/a.js"></script>'))
        self.assertEqual(out[0]["observed"], "script-src does not allow: cdn.other.example")

    def test_allowed_beacon_is_silent(self):
        out = SA.check_csp_blocks(self._page(
            "script-src 'self' https://static.cloudflareinsights.com", self.BEACON))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
