"""Tests for the 2.1.0 checks and the false positives fixed in 2.1.0.

Each check here came out of a competitive audit (2026-09-22) where the client's own
site tripped the tool: module scripts counted as render-blocking, AVIF served through
<picture> reported as "no modern formats", a TravelAgency node ignored in favour of a
thinner Organization node, a duplicated Google Fonts link double-counted, the JPEG
fallback measured instead of the AVIF the browser fetches, and a footprint table with
one row per TripAdvisor review. The new checks (review-count consistency, affiliate
share, title concentration, owned domains) are covered with both a firing case and a
silent case.

Run with:  python3 -m unittest tests.test_checks_21 -v   (from the repo root)
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


def _org(name, count, value="4.9", typ="Organization", extra=""):
    return ('<script type="application/ld+json">{"@context":"https://schema.org",'
            '"@type":"%s","name":"%s","telephone":"+1 555 000 0000"%s,'
            '"aggregateRating":{"@type":"AggregateRating","ratingValue":"%s",'
            '"reviewCount":"%s"}}</script>' % (typ, name, extra, value, count))


# =====================================================================================
# Parser: <picture>, <source>, srcset, module scripts
# =====================================================================================
class TestParserPictureAndModule(unittest.TestCase):
    def test_picture_sources_are_attached_to_the_img(self):
        p = SA.PageParser()
        p.feed('<picture><source type="image/avif" srcset="/a.avif 800w, /b.avif 1600w">'
               '<source type="image/webp" srcset="/a.webp">'
               '<img src="/a.jpg" srcset="/a.jpg 800w" alt="x"></picture>'
               '<img src="/plain.jpg" alt="y">')
        self.assertEqual(len(p.sources), 2)
        self.assertEqual(p.imgs[0]["picture_sources"][0]["type"], "image/avif")
        self.assertEqual(p.imgs[0]["srcset_val"], "/a.jpg 800w")
        self.assertEqual(p.imgs[1]["picture_sources"], [])

    def test_module_scripts_are_not_render_blocking(self):
        p = SA.PageParser()
        p.feed('<html><head><script type="module" src="/app.js"></script>'
               '<script src="/legacy.js"></script></head><body></body></html>')
        self.assertEqual(p.render_blocking_scripts, 1)

    def test_deferred_and_async_still_excluded(self):
        p = SA.PageParser()
        p.feed('<html><head><script src="/a.js" defer></script>'
               '<script src="/b.js" async></script></head><body></body></html>')
        self.assertEqual(p.render_blocking_scripts, 0)


# =====================================================================================
# Modern image formats and the hero candidate (served by the fixture server)
# =====================================================================================
class TestModernFormatsAndHero(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s = FX.FixtureServer().start()
        cls.site = s
        B = s.base
        heavy = FX.make_png(1200, 600, pad_bytes=330 * 1024)
        s.add("/img/hero.png", route_png(heavy))
        s.add("/img/hero.avif", FX.route(b"\x00" * (20 * 1024), ctype="image/avif"))
        pic_imgs = "".join(
            '<picture><source type="image/avif" srcset="/img/p%d.avif 800w">'
            '<img src="/img/p%d.jpg" width="800" height="400" alt="Picture %d"></picture>' % (i, i, i)
            for i in range(6))
        plain_imgs = "".join('<img src="/img/p%d.jpg" width="800" height="400" alt="Plain %d">' % (i, i)
                             for i in range(6))
        hero_pic = ('<picture><source type="image/avif" srcset="/img/hero.avif 600w, /img/hero.avif 1200w">'
                    '<img src="/img/hero.png" fetchpriority="high" width="1200" height="600" '
                    'alt="Hero"></picture>')
        body = "<main><p>" + FX.prose(320, seed=41) + "</p>%s</main>"
        s.add("/pic", FX.route(FX.page_html("Page whose images ship AVIF through picture",
                                            body % (hero_pic + pic_imgs),
                                            description="Six JPEG fallbacks behind AVIF picture sources.",
                                            canonical=B + "/pic")))
        s.add("/plain", FX.route(FX.page_html("Page whose images are plain JPEG only",
                                              body % plain_imgs,
                                              description="Six JPEG images with no modern candidate.",
                                              canonical=B + "/plain")))
        s.add("/preload", FX.route(FX.page_html(
            "Page that preloads a WebP hero",
            body % plain_imgs,
            description="Six JPEG images but a WebP preload for the hero.",
            canonical=B + "/preload",
            head_extra='<link rel="preload" as="image" type="image/webp" href="/img/hero.webp">')))

    @classmethod
    def tearDownClass(cls):
        cls.site.stop()

    def test_picture_avif_is_not_flagged_as_legacy_only(self):
        page, findings = SA.analyze_page(self.site.url("/pic"))
        self.assertNotIn("No modern image formats detected", titles(findings))

    def test_webp_preload_counts_as_modern(self):
        page, findings = SA.analyze_page(self.site.url("/preload"))
        self.assertNotIn("No modern image formats detected", titles(findings))

    def test_plain_jpeg_page_is_still_flagged(self):
        page, findings = SA.analyze_page(self.site.url("/plain"))
        self.assertIn("No modern image formats detected", titles(findings))

    def test_hero_measures_the_avif_source_not_the_png_fallback(self):
        page, _ = SA.analyze_page(self.site.url("/pic"))
        found = SA.check_hero_and_thumbs(page)
        self.assertNotIn("Hero image is very heavy", titles(found), found)

    def test_srcset_candidates_and_largest(self):
        cands = SA._srcset_candidates("/a.avif 800w, /b.avif 1600w, /c.avif")
        self.assertEqual(cands, [("/a.avif", 800), ("/b.avif", 1600), ("/c.avif", 0)])
        hero = {"src": "/x.jpg", "picture_sources": [{"type": "image/avif", "srcset": "/a.avif 800w, /b.avif 1600w"}]}
        url, note = SA._hero_candidate("https://e.com/", hero)
        self.assertEqual(url, "https://e.com/b.avif")
        self.assertIn("AVIF", note)
        self.assertTrue(hero["srcset"])

    def test_hero_candidate_falls_back_to_src(self):
        url, note = SA._hero_candidate("https://e.com/", {"src": "/x.jpg", "picture_sources": []})
        self.assertEqual((url, note), ("https://e.com/x.jpg", ""))


def route_png(body):
    return FX.route(body, ctype="image/png")


# =====================================================================================
# LocalBusiness family
# =====================================================================================
class TestLocalTypes(unittest.TestCase):
    def test_subtypes_without_the_word_business(self):
        for t in ("TravelAgency", "Dentist", "Winery", "TouristAttraction", "HairSalon", "AutoRepair"):
            self.assertTrue(SA._looks_local_type(t), t)

    def test_non_local_types(self):
        for t in ("Article", "WebPage", "Product", "BreadcrumbList", "", None):
            self.assertFalse(SA._looks_local_type(t), t)

    def test_richest_local_node_is_picked(self):
        html = html_doc(
            "<p>hello</p>",
            head=('<script type="application/ld+json">{"@type":"Organization","name":"Acme",'
                  '"telephone":"+1 555"}</script>'
                  '<script type="application/ld+json">{"@type":"TravelAgency","name":"Acme",'
                  '"telephone":"+1 555","address":{"@type":"PostalAddress","streetAddress":"1 Main St",'
                  '"addressLocality":"Flagstaff"},"geo":{"@type":"GeoCoordinates","latitude":"35.2",'
                  '"longitude":"-111.6"}}</script>'))
        fp = SA.extract_footprint([make_page(html)])
        local = fp["google"]["local"]
        self.assertIn("1 Main St", local["address"])
        self.assertIn("35.2", local["geo"])


# =====================================================================================
# Google Fonts: duplicate link, no double counting
# =====================================================================================
class TestFontsDedupe(unittest.TestCase):
    CSS = ("/* latin */ @font-face { font-family: 'X'; src: url(https://fonts.gstatic.com/s/x/a.woff2) format('woff2'); }"
           "/* latin */ @font-face { font-family: 'X'; src: url(https://fonts.gstatic.com/s/x/b.woff2) format('woff2'); }"
           "/* latin */ @font-face { font-family: 'X'; src: url(https://fonts.gstatic.com/s/x/c.woff2) format('woff2'); }")

    def _run(self, head):
        page = make_page(html_doc("<p>x</p>", head=head))
        calls = []
        real = SA.fetch

        def fake(url, *a, **kw):
            calls.append(url)
            if "fonts.googleapis.com" in url:
                return SA.Response(url, url, 200, {"Content-Type": "text/css"}, self.CSS.encode(), 5.0)
            return SA.Response(url, url, 200, {"Content-Type": "font/woff2"}, b"\x00" * (60 * 1024), 5.0)
        SA.fetch = fake
        try:
            return SA.check_fonts(page), calls
        finally:
            SA.fetch = real

    def test_duplicate_link_is_reported_and_files_counted_once(self):
        link = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=X&display=swap">'
        findings, calls = self._run(link + link + '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
        self.assertIn("Google Fonts stylesheet is linked more than once", titles(findings))
        heavy = [x for x in findings if x["title"] == "Web font payload is heavy"]
        self.assertEqual(len(heavy), 1)
        self.assertEqual(heavy[0]["severity"], "LOW", heavy[0]["observed"])      # 180 KB, not 360
        self.assertIn("3 Latin woff2", heavy[0]["observed"])
        self.assertEqual(sum(1 for c in calls if c.endswith(".woff2")), 3)

    def test_single_link_is_not_reported_as_duplicate(self):
        link = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=X&display=swap">'
        findings, _ = self._run(link + '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
        self.assertNotIn("Google Fonts stylesheet is linked more than once", titles(findings))


# =====================================================================================
# Footprint: single-post permalinks are not profiles
# =====================================================================================
class TestFootprintNoise(unittest.TestCase):
    def test_review_permalinks_and_posts_are_skipped(self):
        html = html_doc(
            '<a href="https://www.tripadvisor.com/Attraction_Review-g1-d2-Reviews-Acme.html">reviews</a>'
            '<a href="https://www.tripadvisor.com/ShowUserReviews-g1-d2-r3-Acme.html">one review</a>'
            '<a href="https://www.tripadvisor.com/ShowUserReviews-g1-d2-r4-Acme.html">another</a>'
            '<a href="https://www.facebook.com/acme/videos/123/">video</a>'
            '<a href="https://www.facebook.com/acme/">page</a>'
            '<a href="https://www.youtube.com/watch?v=abc">clip</a>'
            '<a href="https://www.instagram.com/acme/">insta</a>')
        fp = SA.extract_footprint([make_page(html)])
        urls = [s["url"] for s in fp["social"]]
        self.assertEqual(len([u for u in urls if "tripadvisor" in u]), 1)
        self.assertTrue(all("ShowUserReviews" not in u and "/videos/" not in u and "watch?v=" not in u
                            for u in urls), urls)
        self.assertIn("https://www.facebook.com/acme/", urls)
        self.assertIn("https://www.instagram.com/acme/", urls)


# =====================================================================================
# Review-count consistency
# =====================================================================================
class TestRatingConsistency(unittest.TestCase):
    def test_disagreeing_counts_on_the_same_entity(self):
        pages = [make_page(html_doc("<p>a</p>", head=_org("Acme Tours", 729)), url="https://e.com/"),
                 make_page(html_doc("<p>b</p>", head=_org("Acme Tours", 268)), url="https://e.com/tour")]
        found = SA.check_rating_consistency(pages)
        self.assertEqual(titles(found), ["Self-declared review counts disagree across pages"])
        self.assertEqual(found[0]["category"], "trust")
        self.assertIn("729", found[0]["observed"])
        self.assertIn("268", found[0]["observed"])

    def test_agreeing_counts_are_silent(self):
        pages = [make_page(html_doc("<p>a</p>", head=_org("Acme Tours", 729)), url="https://e.com/"),
                 make_page(html_doc("<p>b</p>", head=_org("Acme Tours", 729)), url="https://e.com/tour")]
        self.assertEqual(SA.check_rating_consistency(pages), [])

    def test_different_products_may_differ(self):
        pages = [make_page(html_doc("<p>a</p>", head=_org("Ghost Tour", 281, typ="Product")), url="https://e.com/a"),
                 make_page(html_doc("<p>b</p>", head=_org("Mural Tour", 21, typ="Product")), url="https://e.com/b")]
        self.assertEqual(SA.check_rating_consistency(pages), [])


# =====================================================================================
# Affiliate share
# =====================================================================================
class TestAffiliateShare(unittest.TestCase):
    def _pages(self, n_aff, n_total):
        pages = []
        for i in range(n_total):
            link = ('<a href="https://www.viator.com/tours/x/d1-2P3?pid=P00048136&mcid=42383">book</a>'
                    if i < n_aff else '<a href="https://example.org/partner">partner</a>')
            pages.append(make_page(html_doc("<p>%s</p>" % link), url="https://e.com/us/tours/%d" % i))
        return pages

    def test_majority_affiliate_is_medium(self):
        found = SA.check_affiliate_share(self._pages(15, 25))
        self.assertEqual(titles(found), ["Large share of pages carry affiliate links"])
        self.assertEqual(found[0]["severity"], "MEDIUM")
        self.assertIn("Viator pid=", found[0]["observed"])
        self.assertIn("/us/*/*", found[0]["observed"])

    def test_a_third_is_low(self):
        found = SA.check_affiliate_share(self._pages(9, 25))
        self.assertEqual(found[0]["severity"], "LOW")

    def test_few_affiliate_pages_are_silent(self):
        self.assertEqual(SA.check_affiliate_share(self._pages(5, 25)), [])

    def test_small_samples_are_silent(self):
        self.assertEqual(SA.check_affiliate_share(self._pages(10, 12)), [])

    def test_affiliate_kind(self):
        self.assertEqual(SA._affiliate_kind("https://www.amazon.com/dp/B0?tag=acme-20"), "Amazon tag=")
        self.assertEqual(SA._affiliate_kind("https://www.booking.com/hotel/x.html?aid=7991881"), "Booking.com aid=")
        self.assertEqual(SA._affiliate_kind("https://go.skimresources.com/?id=1&url=x"), "go.skimresources.com")
        self.assertEqual(SA._affiliate_kind("https://example.com/page?ref=footer"), "")
        self.assertEqual(SA._affiliate_kind("https://staff.example.com/"), "")


# =====================================================================================
# Title concentration
# =====================================================================================
class TestTitleConcentration(unittest.TestCase):
    FILLER = ["Lantern maker", "Quartz mining", "Cedar carving", "Harbor pilots", "Granite quarry",
              "Willow baskets", "Copper smelting", "Amber trade", "Ridge walking", "Meadow farming",
              "Pebble beaches", "Ember festival"]

    def _pages(self, n_hit, n_total, brand=" | Acme Tours"):
        pages = []
        for i in range(n_total):
            # filler titles share no two-word sequence with each other (only the brand)
            t = ("Flagstaff Ghost Tour number %d%s" % (i, brand) if i < n_hit
                 else "%s %d%s" % (self.FILLER[i % len(self.FILLER)], i + 10, brand))
            pages.append({"url": "https://e.com/p%d" % i, "title": t})
        return pages

    def test_shared_phrase_is_found_and_brand_is_stripped(self):
        tc = SA.title_concentration(self._pages(6, 12))
        self.assertEqual(tc["phrase"], "ghost tour")
        self.assertEqual((tc["pages"], tc["total"]), (6, 12))
        self.assertEqual(len(tc["examples"]), 5)

    def test_brand_segment_alone_never_wins(self):
        self.assertIsNone(SA.title_concentration(self._pages(2, 12)))

    def test_too_few_pages(self):
        self.assertIsNone(SA.title_concentration(self._pages(5, 6)))


# =====================================================================================
# Owned domains (fixture server: 127.0.0.1 is the site, localhost is "another domain")
# =====================================================================================
class TestOwnedDomains(unittest.TestCase):
    def _site(self, satellite_route):
        s = FX.FixtureServer().start()
        B, X = s.base, s.alt_base
        s.add("/", FX.route(FX.page_html(
            "Homepage that declares a second domain in sameAs",
            '<nav><a href="/about">About</a> <a href="%s/satellite/">Our other site</a></nav>'
            "<main><p>" % X + FX.prose(300, seed=91) + "</p></main>",
            description="A homepage whose Organization schema declares a sibling domain.",
            canonical=B + "/",
            jsonld=('{"@context":"https://schema.org","@type":"Organization","name":"Acme",'
                    '"sameAs":["%s/satellite/","https://www.facebook.com/acme/"]}' % X))))
        s.add("/satellite/", satellite_route(B, X))
        return s

    @staticmethod
    def _satellite(org_name, B, X, ga=""):
        return FX.route(FX.page_html("Satellite site for the same business",
                                     "<main><p>" + FX.prose(200, seed=92) + "</p>"
                                     '<p><a href="%s/">Back to the main site</a></p></main>' % B,
                                     description="A microsite that duplicates the business.",
                                     canonical=X + "/satellite/",
                                     head_extra=ga,
                                     jsonld='{"@context":"https://schema.org","@type":"LocalBusiness",'
                                            '"name":"%s"}' % org_name))

    def test_separate_live_site_with_the_same_entity_name_is_reported(self):
        s = self._site(lambda B, X: self._satellite("Acme", B, X))
        try:
            page, _ = SA.analyze_page(s.base + "/")
            findings, health, rows = SA.check_owned_domains([page], s.base + "/")
        finally:
            s.stop()
        self.assertEqual(titles(findings), ["Other domains owned by this business are separate live sites"])
        self.assertEqual(findings[0]["severity"], "LOW")          # one domain, no shared analytics
        self.assertIn("declared in sameAs", findings[0]["observed"])
        self.assertIn("same organization name", findings[0]["observed"])
        self.assertIn("links back", findings[0]["observed"])
        self.assertEqual(health[0], "Owned domains")
        self.assertIn("1 separate live site(s)", health[1])
        self.assertEqual([r["domain"] for r in rows if r["owned"]], ["localhost"])
        self.assertTrue(all("facebook" not in r["domain"] for r in rows))

    def test_a_directory_listing_in_sameas_is_not_owned(self):
        # A DMO listing, a Wikipedia page or a Maps share link in sameAs is a pointer to
        # a page about the business, not a domain the business runs.
        s = self._site(lambda B, X: self._satellite("Discover Fixture", B, X))
        try:
            page, _ = SA.analyze_page(s.base + "/")
            findings, health, rows = SA.check_owned_domains([page], s.base + "/")
        finally:
            s.stop()
        self.assertEqual(findings, [])
        self.assertIn("0 owned", health[1])
        self.assertEqual([r["domain"] for r in rows], ["localhost"])
        self.assertTrue(rows[0]["declared_in_sameas"])
        self.assertFalse(rows[0]["owned"])

    def test_shared_analytics_property_is_ownership_on_its_own(self):
        ga = ('<script async src="https://www.googletagmanager.com/gtag/js?id=G-FIXTURE01"></script>'
              "<script>gtag('config','G-FIXTURE01');</script>")
        s = self._site(lambda B, X: self._satellite("Some Other Name", B, X, ga=ga))
        s.add("/", FX.route(FX.page_html(
            "Homepage that shares an analytics property with another domain",
            '<nav><a href="%s/satellite/">Our other site</a></nav><main><p>' % s.alt_base
            + FX.prose(300, seed=93) + "</p></main>",
            description="A homepage whose GA4 property also loads on a sibling domain.",
            canonical=s.base + "/",
            head_extra=ga)))
        try:
            page, _ = SA.analyze_page(s.base + "/")
            findings, health, rows = SA.check_owned_domains([page], s.base + "/")
        finally:
            s.stop()
        self.assertEqual(titles(findings), ["Other domains owned by this business are separate live sites"])
        self.assertEqual(findings[0]["severity"], "MEDIUM")       # shared analytics
        self.assertIn("shares the analytics property", findings[0]["observed"])
        self.assertTrue(rows[0]["shares_analytics"])

    def test_redirecting_domain_is_not_a_finding(self):
        def sat(B, X):
            return FX.route(b"", status=301, ctype=None, location=B + "/")
        s = self._site(sat)
        # the redirect lands on the homepage itself, whose schema names "Acme", so the
        # domain is owned; it simply is not a separate site
        try:
            page, _ = SA.analyze_page(s.base + "/")
            findings, health, rows = SA.check_owned_domains([page], s.base + "/")
        finally:
            s.stop()
        self.assertEqual(findings, [])
        self.assertIn("redirect or canonicalize here", health[1])

    def test_no_candidates(self):
        page = make_page(html_doc('<a href="/local">x</a>'))
        findings, health, rows = SA.check_owned_domains([page], "https://example.com/")
        self.assertEqual((findings, rows), ([], []))
        self.assertIn("none declared", health[1])


if __name__ == "__main__":
    unittest.main()
