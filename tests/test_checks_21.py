"""Tests for the 2.1.0 checks and the false positives fixed in 2.1.0.

Each check here came out of a competitive audit (2026-09-22) where the client's own
site tripped the tool: module scripts counted as render-blocking, AVIF served through
<picture> reported as "no modern formats", a TravelAgency node ignored in favour of a
thinner Organization node, a duplicated Google Fonts link double-counted, the JPEG
fallback measured instead of the AVIF the browser fetches, and a footprint table with
one row per TripAdvisor review. The new checks (review-count consistency, affiliate
share, title concentration, owned domains) are covered with both a firing case and a
silent case. The later cases in each class are regressions from the code review of
2.1.0: commas inside CDN srcset URLs, art-directed <picture> sources, <noscript> font
fallbacks, sitewide affiliate links, brands punctuated as "Brand: Page", nameless
rating nodes, Service and TouristTrip nodes taken for the business, substring matches
in the owned-domains skip list, .co.uk siblings, and directory listings in sameAs.

Run with:  python3 -m unittest tests.test_checks_21 -v   (from the repo root)
No network: the only sockets opened are to the in-process fixture server.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seo_audit as SA                                            # noqa: E402
import report as RP                                               # noqa: E402
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
        cdn_imgs = "".join('<img src="https://images.ctfassets.net/a/b/p%d.jpg?fm=webp&q=80" width="800" '
                           'height="400" alt="Contentful %d">' % (i, i) for i in range(6))
        s.add("/cdn", FX.route(FX.page_html("Page whose images ask the CDN for WebP",
                                            body % cdn_imgs,
                                            description="Six JPEG paths with fm=webp on the query.",
                                            canonical=B + "/cdn")))
        imgix_imgs = "".join('<img src="https://acme.imgix.net/p%d.jpg?auto=format&w=800" width="800" '
                             'height="400" alt="Imgix %d">' % (i, i) for i in range(6))
        s.add("/imgix", FX.route(FX.page_html("Page whose images use imgix auto format",
                                              body % imgix_imgs,
                                              description="Six JPEG paths with auto=format on the query.",
                                              canonical=B + "/imgix")))
        # art-directed hero: a 500 KB desktop source listed first, a 20 KB phone source second
        s.add("/img/desk.webp", FX.route(b"\x00" * (500 * 1024), ctype="image/webp"))
        s.add("/img/phone.webp", FX.route(b"\x00" * (20 * 1024), ctype="image/webp"))
        art = ('<picture><source media="(min-width: 1024px)" type="image/webp" srcset="/img/desk.webp 1600w">'
               '<source type="image/webp" srcset="/img/phone.webp 600w">'
               '<img src="/img/hero.png" fetchpriority="high" width="1200" height="600" '
               'alt="Hero"></picture>')
        s.add("/art", FX.route(FX.page_html("Page with an art-directed hero picture",
                                            body % art,
                                            description="A desktop-only source ahead of the phone source.",
                                            canonical=B + "/art")))

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

    def test_commas_inside_cdn_urls_do_not_split_candidates(self):
        ss = ("https://res.cloudinary.com/demo/image/upload/c_fill,w_400/hero.avif 400w, "
              "https://res.cloudinary.com/demo/image/upload/c_fill,w_1600/hero.avif 1600w")
        hero = {"src": "/hero.jpg", "srcset_val": "", "picture_sources": [{"type": "image/avif", "srcset": ss}]}
        url, note = SA._hero_candidate("https://example.com/", hero)
        self.assertEqual(url, "https://res.cloudinary.com/demo/image/upload/c_fill,w_1600/hero.avif")
        self.assertIn("AVIF", note)
        self.assertTrue(hero["srcset"])
        self.assertEqual(SA._srcset_candidates("/cdn-cgi/image/width=800,format=auto/hero.jpg 800w,/x.jpg 1600w"),
                         [("/cdn-cgi/image/width=800,format=auto/hero.jpg", 800), ("/x.jpg", 1600)])
        self.assertEqual(SA._srcset_candidates("a.jpg 1x, b.jpg 2x"), [("a.jpg", 1000), ("b.jpg", 2000)])
        # one candidate whose URL holds a comma is not a responsive srcset
        one = {"src": "/hero.jpg", "picture_sources": [
            {"type": "image/avif", "srcset": "https://res.cloudinary.com/demo/image/upload/c_fill,w_1600/hero.avif"}]}
        SA._hero_candidate("https://example.com/", one)
        self.assertFalse(one["srcset"])

    def test_data_uri_candidates_are_skipped(self):
        self.assertEqual(SA._srcset_candidates("data:image/png;base64,AAAA 1x, /r.png 2x"), [("/r.png", 2000)])
        hero = {"src": "/hero.jpg", "picture_sources": [{"type": "image/webp", "srcset": "data:image/png;base64,AAAA 1x"}]}
        self.assertEqual(SA._hero_candidate("https://example.com/", hero), ("https://example.com/hero.jpg", ""))

    def test_media_matches_models_a_phone(self):
        self.assertTrue(SA._media_matches(""))
        self.assertTrue(SA._media_matches("(max-width: 600px)"))
        self.assertTrue(SA._media_matches("(orientation: landscape)"))
        self.assertFalse(SA._media_matches("(min-width: 1024px)"))
        self.assertFalse(SA._media_matches("(min-width: 30em)"))
        self.assertFalse(SA._media_matches("(max-width: 320px)"))

    def test_desktop_only_source_is_not_measured_for_the_phone(self):
        page, _ = SA.analyze_page(self.site.url("/art"))
        self.assertEqual(page["parser"].imgs[0]["picture_sources"][0]["media"], "(min-width: 1024px)")
        url, note = SA._hero_candidate(page["url"], dict(page["parser"].imgs[0]))
        self.assertEqual(url, self.site.url("/img/phone.webp"))
        self.assertIn("412px phone", note)
        found = SA.check_hero_and_thumbs(page)
        self.assertNotIn("Hero image is very heavy", titles(found), found)

    def test_cdn_format_parameters_count_as_modern(self):
        for path in ("/cdn", "/imgix"):
            page, findings = SA.analyze_page(self.site.url(path))
            self.assertNotIn("No modern image formats detected", titles(findings), path)

    def test_format_pattern_needs_a_left_boundary(self):
        for u in ("/a.jpg?ref=auto", "/staff_avif.jpg", "/pdf=auto"):
            self.assertIsNone(SA._MODERN_IMG_RE.search(u), u)
        for u in ("/hero.jpg?fm=webp", "/hero.jpg?auto=format", "/u/w_800,f_auto/x.jpg", "/x.jpg?format=webp",
                  "/x.jpg?f=avif", "/a.avif", "/a.webp?x=1"):
            self.assertIsNotNone(SA._MODERN_IMG_RE.search(u), u)


def route_png(body):
    return FX.route(body, ctype="image/png")


# =====================================================================================
# LocalBusiness family
# =====================================================================================
class TestLocalTypes(unittest.TestCase):
    def test_subtypes_without_the_word_business(self):
        for t in ("TravelAgency", "Dentist", "Winery", "TouristAttraction", "HairSalon", "AutoRepair",
                  "ProfessionalService", "LegalService", "OnlineStore", "AmusementPark", "GasStation"):
            self.assertTrue(SA._looks_local_type(t), t)

    def test_non_local_types(self):
        for t in ("Article", "WebPage", "Product", "BreadcrumbList", "", None,
                  "Service", "TaxiService", "TouristTrip", "Park", "SubwayStation",
                  "PerformingArtsTheater", "ElementarySchool"):
            self.assertFalse(SA._looks_local_type(t), t)

    def test_an_offering_node_is_not_the_local_business(self):
        # A rated Service or TouristTrip beside an address-less Organization must not
        # stand in for the business, or "No LocalBusiness schema" would be suppressed.
        rating = {"@type": "AggregateRating", "ratingValue": "4.9", "reviewCount": "312"}
        for typ in ("Service", "TouristTrip"):
            nodes = [{"@type": "Organization", "name": "Acme", "url": "https://e.com/"},
                     {"@type": typ, "name": "Downtown Ghost Walk", "aggregateRating": rating}]
            self.assertIsNone(SA._google_signals([], nodes)["local"], typ)

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

    def test_async_link_with_noscript_fallback_is_not_a_duplicate(self):
        gf = "https://fonts.googleapis.com/css2?family=X&display=swap"
        head = ('<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
                '<link rel="stylesheet" href="%s" media="print" onload="this.media=\'all\'">'
                '<noscript><link rel="stylesheet" href="%s"></noscript>' % (gf, gf))
        findings, _ = self._run(head)
        self.assertNotIn("Google Fonts stylesheet is linked more than once", titles(findings))

    def test_plain_duplicate_still_fires_beside_a_noscript_copy(self):
        link = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=X&display=swap">'
        findings, _ = self._run(link + link + "<noscript>" + link + "</noscript>"
                                + '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>')
        dup = [x for x in findings if x["title"] == "Google Fonts stylesheet is linked more than once"]
        self.assertEqual(len(dup), 1)
        self.assertIn("2 <link rel=stylesheet> tags, 1 distinct", dup[0]["observed"])


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

    def test_nameless_nodes_are_not_pooled_into_one_entity(self):
        def unnamed(typ, count):
            return ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"%s",'
                    '"aggregateRating":{"@type":"AggregateRating","ratingValue":"4.9",'
                    '"reviewCount":"%s"}}</script>' % (typ, count))
        for typ in ("TravelAgency", "Service", "TouristTrip"):
            pages = [make_page(html_doc("<p>a</p>", head=unnamed(typ, 12)), url="https://e.com/a/"),
                     make_page(html_doc("<p>b</p>", head=unnamed(typ, 40)), url="https://e.com/b/")]
            self.assertEqual(SA.check_rating_consistency(pages), [], typ)


# =====================================================================================
# Affiliate share
# =====================================================================================
class TestAffiliateShare(unittest.TestCase):
    def _pages(self, n_aff, n_total, chrome=""):
        # one product per catalog page, as on a real affiliate catalog; `chrome` is a
        # link repeated on every page by the header, footer or navigation
        pages = []
        for i in range(n_total):
            link = ('<a href="https://www.viator.com/tours/city-%d/d%d-P%d?pid=P00048136&mcid=42383">book</a>'
                    % (i, i, i) if i < n_aff else '<a href="https://example.org/partner">partner</a>')
            pages.append(make_page(html_doc("<p>%s</p>%s" % (link, chrome)), url="https://e.com/us/tours/%d" % i))
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

    def test_sitewide_footer_link_does_not_make_every_page_an_affiliate_page(self):
        chrome = '<footer><a href="https://www.amazon.com/dp/B000?tag=acme-20">our book</a></footer>'
        self.assertEqual(SA.check_affiliate_share(self._pages(0, 25, chrome)), [])

    def test_sitewide_link_does_not_hide_a_catalog(self):
        chrome = '<nav><a href="https://www.viator.com/x?pid=P00048136">Book</a></nav>'
        found = SA.check_affiliate_share(self._pages(15, 25, chrome))
        self.assertEqual(found[0]["severity"], "MEDIUM")
        self.assertIn("15 of 25", found[0]["observed"])

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

    def _shaped(self, fmt, n=12, branded=None):
        """n filler titles, the first `branded` of them passed through fmt."""
        branded = n if branded is None else branded
        pages = []
        for i in range(n):
            t = "%s %d" % (self.FILLER[i % len(self.FILLER)], i + 10)
            pages.append({"url": "https://e.com/p%d" % i, "title": fmt(t) if i < branded else t})
        return pages

    def test_separators(self):
        split = SA._TITLE_SPLIT_RE.split
        self.assertEqual(split("Acme Tours: Haunted Downtown"), ["Acme Tours", "Haunted Downtown"])
        self.assertEqual(split("Haunted Downtown by Acme Tours"), ["Haunted Downtown", "Acme Tours"])
        self.assertEqual(split("Haunted Downtown | Acme Tours"), ["Haunted Downtown", "Acme Tours"])
        self.assertEqual(split("Route-66 Walking Tour"), ["Route-66 Walking Tour"])
        self.assertEqual(split("Tours at 10:30"), ["Tours at 10:30"])

    def test_brand_before_a_tight_colon_is_stripped(self):
        self.assertIsNone(SA.title_concentration(self._shaped(lambda t: "Acme Tours: " + t, branded=8)))

    def test_brand_after_by_is_stripped(self):
        self.assertIsNone(SA.title_concentration(self._shaped(lambda t: t + " by Acme Tours", branded=8)))

    def test_identical_titles_do_not_report_the_brand(self):
        self.assertIsNone(SA.title_concentration(self._shaped(lambda t: "Acme Ghost Tours")))

    def test_seeded_brand_with_a_tight_hyphen(self):
        self.assertIsNone(SA.title_concentration(self._shaped(lambda t: t + "-Acme Ghost Tours"),
                                                 brand_names={"Acme Ghost Tours"}))

    def test_seeded_brand_in_a_minority_of_titles_is_stripped(self):
        # 7 of 20 titles end in the brand: under the 40% bar that marks a segment as the
        # brand, yet enough to win; passing the homepage's own name strips it
        pages = self._shaped(lambda t: t + " | Acme Tours", n=20, branded=7)
        self.assertEqual(SA.title_concentration(pages)["phrase"], "acme tours")
        self.assertIsNone(SA.title_concentration(pages, brand_names={"Acme Tours"}))

    def test_seeded_brand_keeps_a_real_concentration(self):
        tc = SA.title_concentration(self._pages(6, 12), brand_names={"Acme Tours"})
        self.assertEqual((tc["phrase"], tc["pages"]), ("ghost tour", 6))


# =====================================================================================
# Owned domains (fixture server: 127.0.0.1 is the site, localhost is "another domain")
# =====================================================================================
class TestOwnedDomains(unittest.TestCase):
    def _site(self, satellite_route, path="/satellite/"):
        s = FX.FixtureServer().start()
        B, X = s.base, s.alt_base
        s.add("/", FX.route(FX.page_html(
            "Homepage that declares a second domain in sameAs",
            '<nav><a href="/about">About</a> <a href="%s%s">Our other site</a></nav>'
            "<main><p>" % (X, path) + FX.prose(300, seed=91) + "</p></main>",
            description="A homepage whose Organization schema declares a sibling domain.",
            canonical=B + "/",
            jsonld=('{"@context":"https://schema.org","@type":"Organization","name":"Acme",'
                    '"sameAs":["%s%s","https://www.facebook.com/acme/"]}' % (X, path)))))
        s.add(path, satellite_route(B, X))
        return s

    @staticmethod
    def _satellite(org_name, B, X, ga="", path="/satellite/", site_name=None):
        # site_name replaces the fixture's default og:site_name ("Fixture Site", which
        # the homepage carries too)
        head = ga + ('<meta property="og:site_name" content="%s">' % site_name if site_name else "")
        return FX.route(FX.page_html("Satellite site for the same business",
                                     "<main><p>" + FX.prose(200, seed=92) + "</p>"
                                     '<p><a href="%s/">Back to the main site</a></p></main>' % B,
                                     description="A microsite that duplicates the business.",
                                     canonical=X + path,
                                     head_extra=head, og=site_name is None,
                                     jsonld='{"@context":"https://schema.org","@type":"LocalBusiness",'
                                            '"name":"%s"}' % org_name))

    @staticmethod
    def _declared_rows(base, hosts):
        """check_owned_domains on a homepage whose sameAs lists `hosts`, with every probe
        answered offline (status 0), so the rows show which hosts became candidates."""
        jsonld = json.dumps({"@context": "https://schema.org", "@type": "LocalBusiness", "name": "Acme",
                             "sameAs": ["https://%s/" % h for h in hosts]})
        page = make_page(html_doc("<p>x</p>", head='<script type="application/ld+json">%s</script>' % jsonld),
                         url=base)
        real = SA.fetch
        SA.fetch = lambda url, *a, **kw: SA.Response(url, url, 0, {}, b"", 1.0, error="offline")
        try:
            return SA.check_owned_domains([page], base)
        finally:
            SA.fetch = real

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

    def test_a_deep_listing_path_is_not_owned(self):
        # A directory page about the business carries LocalBusiness schema under the
        # business's own name; the deep path gives it away.
        path = "/listing/acme/"
        s = self._site(lambda B, X: self._satellite("Acme", B, X, path=path), path=path)
        try:
            page, _ = SA.analyze_page(s.base + "/")
            findings, health, rows = SA.check_owned_domains([page], s.base + "/")
        finally:
            s.stop()
        self.assertEqual(findings, [])
        self.assertTrue(rows[0]["same_entity_name"])
        self.assertTrue(rows[0]["directory_listing"])
        self.assertFalse(rows[0]["owned"])

    def test_a_page_that_names_another_site_is_not_owned(self):
        s = self._site(lambda B, X: self._satellite("Acme", B, X, site_name="Visit Fixture"))
        try:
            page, _ = SA.analyze_page(s.base + "/")
            findings, health, rows = SA.check_owned_domains([page], s.base + "/")
        finally:
            s.stop()
        self.assertEqual(findings, [])
        self.assertIn("0 owned", health[1])
        self.assertTrue(rows[0]["same_entity_name"])
        self.assertFalse(rows[0]["owned"])

    def test_skip_list_matches_whole_labels_only(self):
        owned_like = ["acmeplumbing.com", "breakfastly.com", "coresy.com", "pinstripe.co",
                      "tourbooking.com", "pineapple.com", "box.com"]
        third_party = ["maps.google.com", "s3.amazonaws.com", "booking.com", "fastly.net", "x.com"]
        _, health, rows = self._declared_rows("https://acme.com/", owned_like + third_party)
        self.assertEqual(sorted(r["domain"] for r in rows), sorted(owned_like))
        self.assertIn("7 candidate(s) checked", health[1])

    def test_sibling_under_a_country_code_sld_is_a_candidate(self):
        _, _, rows = self._declared_rows("https://www.acme.co.uk/",
                                         ["acme-heating.co.uk", "shop.acme.co.uk", "acme-heating.com"])
        self.assertEqual(sorted(r["domain"] for r in rows), ["acme-heating.co.uk", "acme-heating.com"])

    def test_site_reg(self):
        self.assertEqual(SA._site_reg("shop.acme.co.uk"), "acme.co.uk")
        self.assertEqual(SA._site_reg("acme.com.au"), "acme.com.au")
        self.assertEqual(SA._site_reg("shop.acme.com"), "acme.com")
        self.assertEqual(SA._site_reg("acme.io"), "acme.io")


# =====================================================================================
# Report rendering: the keyword section
# =====================================================================================
class TestKeywordBlock(unittest.TestCase):
    INSIGHT = "Title concentration: 12 of 12 crawled titles contain \u201cghost tour\u201d."

    def test_insights_render_when_keyword_analysis_is_unavailable(self):
        html = RP.keyword_block({"keywords": {"available": False, "note": "No crawlable on-page text.",
                                              "insights": [self.INSIGHT]}})
        self.assertIn("No crawlable on-page text.", html)
        self.assertIn("Title concentration", html)
        self.assertIn("kwnotes", html)

    def test_insights_render_once_when_available(self):
        html = RP.keyword_block({"keywords": {"available": True, "terms": [], "insights": [self.INSIGHT]}})
        self.assertEqual(html.count("Title concentration"), 1)


if __name__ == "__main__":
    unittest.main()
