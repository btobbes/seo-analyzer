"""Unit tests for scripts/find_competitors.py. No network: live fetching is stubbed.

Run with:  python3 -m unittest tests.test_find_competitors -v   (from the repo root)

The Brave sample below is synthetic but mirrors the structure of real search.brave.com
result pages (September 2026): section#mixed-main, div.snippet[data-type=web][data-pos]
with the title in a .title element's title attribute, an ad block with
data-landing-page, an untyped entity snippet, a video cluster, #faq, #discussions,
#locations, and the #infobox card in section#mixed-side.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import find_competitors as FC                                     # noqa: E402
import seo_audit as SA                                            # noqa: E402


BRAVE_SAMPLE = """<!doctype html><html lang="en"><head><title>flagstaff ghost tour - Brave Search</title>
<link rel="stylesheet" href="https://cdn.search.brave.com/serp/v3/_app/immutable/assets/app.css">
</head><body><header><a href="https://search.brave.com/">Brave Search</a></header>
<main class="main-column"><section id="mixed-top"></section>
<section id="mixed-main" class="svelte-e12qt1"><!--[-->
 <div class="snippet svelte-jmfu5f" data-type="ad" id="search-ad"
      data-landing-page="https://www.viator.com/tours/Flagstaff/ad-landing?m=1&amp;x=2"
      data-headline-text="Haunted Flagstaff Tour">
   <a href="/a/redirect?click_url=https%3A%2F%2Fwww.viator.com%2F">Sponsored</a></div>
 <div class="snippet svelte-jmfu5f"><section class="mb-xl"><a href="https://www.example-client.com/"
      target="_self"><h1 class="desktop-heading-h3">Example Client: Ghost Tours</h1></a></section>
      <section class="reviews"><ul><li>Great tour</li></ul></section></div>
 <div class="snippet svelte-jmfu5f" data-pos="2" data-type="web" data-keynav="true">
   <div class="result-body"><div class="result-content">
   <a href="https://www.Example-Client.com/" target="_self" class="l1">
     <div class="site-name-wrapper"><img src="https://imgs.search.brave.com/fav.png" alt=""/>
       <div class="site-name-content"><div class="t-secondary">Example Client</div>
       <cite class="snippet-url">example-client.com</cite></div></div>
     <div class="title search-snippet-title line-clamp-1" title="Example Client | Ghost Tours">Example Client | Ghost Tours</div></a>
   <div class="snippet deep-results"><a href="https://www.example-client.com/about">About us</a></div>
   </div></div></div>
 <div class="snippet svelte-jmfu5f" id="faq"><header>People also ask</header>
   <a href="https://ghosttours-rival.com/faq/">ghosttours-rival.com</a></div>
 <div class="snippet svelte-jmfu5f" data-pos="4" data-type="cluster"><div class="video-cluster-grid odd">
   <a href="https://www.youtube.com/watch?v=abc">Ghost walk video</a>
   <a href="https://www.tiktok.com/@someone/video/1">Clip</a>
   <a href="/videos?q=flagstaff+ghost+tour&amp;source=vcluster">View all</a></div></div>
 <div class="snippet svelte-jmfu5f" data-pos="5" data-type="web"><a href="https://www.tripadvisor.com/Attractions-g1-Activities.html">
   <div class="title search-snippet-title" title="THE 10 BEST Flagstaff Ghost Tours">x</div></a></div>
 <div class="snippet svelte-jmfu5f" id="discussions"><a href="https://www.reddit.com/r/Flagstaff/comments/1/">Paranormal thread</a></div>
 <div class="snippet svelte-jmfu5f" id="locations"><ul>
   <li><button type="button"><div class="item-wrapper"><div class="title desktop-large-semibold">Rival Ghost Walks</div>
     <div class="rating flex-hcenter"><div>4.9</div><div class="rating-ta"><span></span></div><div>Tripadvisor</div>
     <div class="review-count"><div class="spacing"><svg xmlns="http://www.w3.org/2000/svg"></svg></div> 1,216</div></div>
     <address>1 Main St</address></div></button></li>
   <li><button type="button"><div class="title">Other Tours &amp; Co</div><address>2 Main St</address></button></li>
 </ul></div>
 <div class="snippet svelte-jmfu5f" data-pos="7" data-type="web"><a href="https://ghosttours-rival.com/flagstaff/">
   <div class="title search-snippet-title">Rival Ghost Walks &amp; Tours</div></a></div>
 <div class="snippet svelte-jmfu5f" data-pos="8" data-type="web"><a href="https://www.tripadvisor.com/Attractions-g1-Activities.html#reviews">
   <div class="title" title="Duplicate URL">dup</div></a></div>
 <div class="snippet svelte-jmfu5f" data-pos="9" data-type="web"><a href="https://visitflagstaff.org/tours/">
   <div class="title" title="Tours | Visit Flagstaff">Tours</div></a></div>
 <div class="snippet svelte-jmfu5f" id="search-elsewhere"><a href="https://www.google.com/search?q=x">Google</a></div>
</section></main>
<aside><section id="mixed-side"><div class="snippet" id="infobox-snippet">
  <a href="#map-attribution">map</a><a href="https://www.example-client.com/">Website</a>
  <a href="https://www.google.com/maps/search/?api=1&amp;query=Example">Directions</a>
  <a href="https://www.tripadvisor.com/Attraction_Review-g1-d2.html">4.9 (363) Tripadvisor</a></div></section></aside>
<footer><a href="https://brave.com/privacy/">Privacy</a><a href="https://status.brave.app/">Status</a></footer>
</body></html>"""

FALLBACK_SAMPLE = """<html><body><a href="https://search.brave.com/settings">Settings</a>
<a href="https://alpha-tours.com/">Alpha Tours</a><img src="https://imgs.search.brave.com/x.png">
<a href="https://www.w3.org/2000/svg">w3</a><a href="https://cdn.example.net/app.js">script</a>
<a href="https://beta.example.org/page">Beta page</a><a href="https://alpha-tours.com/">Alpha again</a>
<a href="/relative">relative</a><a href="https://gamma.com/x">Gamma</a></body></html>"""

CAPTCHA_SAMPLE = """<html><head><title>Brave Search</title></head><body><p>Your request has
been flagged as being suspicious and Brave Search decided to schedule a captcha for you.</p>
<a href="https://tb-manual.torproject.org/security-settings/#safest">safest</a></body></html>"""

SERPAPI_SAMPLE = {
    "search_metadata": {"status": "Success"},
    "search_parameters": {"engine": "google", "q": "flagstaff ghost tour"},
    "organic_results": [
        {"position": 2, "link": "https://www.tripadvisor.com/Attractions-g1.html", "title": "TA list"},
        {"position": 1, "link": "https://rival.com/", "title": "Rival Ghost Walks"},
        {"position": 3, "link": "https://www.example-client.com/", "title": "Example Client"},
        {"position": 4, "link": "https://satellite-client.com/", "title": "Satellite"},
    ],
    "local_results": {"places": [
        {"position": 1, "title": "Rival Ghost Walks", "rating": 4.9, "reviews": 812,
         "links": {"website": "https://www.rival.com/"}},
        {"position": 2, "title": "Example Client", "rating": 5.0, "reviews": 1700,
         "website": "https://example-client.com/"},
    ]},
    "inline_videos": [{"link": "https://www.youtube.com/watch?v=1", "title": "A video"}],
}

DATAFORSEO_SAMPLE = {
    "version": "0.1.20260901", "status_code": 20000, "tasks": [
        {"status_code": 20000, "data": {"keyword": "flagstaff walking tour", "se": "google"},
         "result": [{"keyword": "flagstaff walking tour", "se_domain": "google.com", "items": [
             {"type": "paid", "rank_group": 1, "rank_absolute": 1,
              "url": "https://www.viator.com/ad", "domain": "www.viator.com", "title": "Ad"},
             {"type": "local_pack", "rank_group": 1, "rank_absolute": 2,
              "title": "Rival Ghost Walks", "domain": "rival.com", "url": "https://rival.com/",
              "rating": {"rating_type": "Max5", "value": 4.8, "votes_count": 120}},
             {"type": "organic", "rank_group": 2, "rank_absolute": 5,
              "url": "https://www.example-client.com/", "domain": "www.example-client.com",
              "title": "Client"},
             {"type": "organic", "rank_group": 1, "rank_absolute": 3,
              "url": "https://rival.com/walk", "domain": "rival.com", "title": "Walk"},
         ]}]},
        {"status_code": 20000, "data": {"keyword": "haunted flagstaff", "se": "google"},
         "result": [{"items": [
             {"type": "organic", "rank_absolute": 1,
              "url": "https://en.wikipedia.org/wiki/Flagstaff", "title": "Wiki"}]}]},
    ]}


def rec(query, domains, engine="Brave Search (test)", engine_key="brave"):
    """A record whose organic results are the given domains in order."""
    return {"query": query, "engine": engine, "engine_key": engine_key, "source": "test",
            "parse_mode": "blocks", "local_pack": [], "features": [],
            "organic": [{"position": i, "url": "https://%s/p%d" % (d, i), "domain": d,
                         "title": d, "slot": None} for i, d in enumerate(domains, 1)]}


def html_response(body, status=200):
    return SA.Response("u", "u", status, {"Content-Type": "text/html; charset=utf-8"},
                       body.encode("utf-8") if isinstance(body, str) else body, 5.0)


# =====================================================================================
# Domains
# =====================================================================================
class TestNormalizeDomain(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(FC.normalize_domain("WWW.Example.COM"), "example.com")
        self.assertEqual(FC.normalize_domain("https://www.example.com:443/a?b=1"), "example.com")
        self.assertEqual(FC.normalize_domain("example.com/path"), "example.com")
        self.assertEqual(FC.normalize_domain("shop.example.com."), "shop.example.com")
        self.assertEqual(FC.normalize_domain(""), "")
        self.assertEqual(FC.domain_of("https://www.Example-Client.com/"), "example-client.com")
        self.assertEqual(FC.domain_of("not a url"), "")


# =====================================================================================
# Brave parsing
# =====================================================================================
class TestParseBrave(unittest.TestCase):
    def test_organic_order_positions_domains_titles(self):
        got = FC.parse_brave_html(BRAVE_SAMPLE)
        self.assertEqual([(r["position"], r["domain"]) for r in got],
                         [(1, "example-client.com"), (2, "tripadvisor.com"),
                          (3, "ghosttours-rival.com"), (4, "visitflagstaff.org")])
        self.assertEqual(got[0]["url"], "https://www.Example-Client.com/")
        self.assertEqual(got[0]["title"], "Example Client | Ghost Tours")      # title attr
        self.assertEqual(got[1]["title"], "THE 10 BEST Flagstaff Ghost Tours")
        self.assertEqual(got[2]["title"], "Rival Ghost Walks & Tours")         # text fallback
        self.assertEqual([r["slot"] for r in got], [2, 5, 7, 9])               # Brave data-pos

    def test_duplicate_url_and_sitelinks_are_not_extra_results(self):
        urls = [r["url"] for r in FC.parse_brave_html(BRAVE_SAMPLE)]
        self.assertEqual(sum("tripadvisor" in u for u in urls), 1)   # #reviews duplicate dropped
        self.assertNotIn("https://www.example-client.com/about", urls)

    def test_cap_at_top(self):
        got = FC.parse_brave_html(BRAVE_SAMPLE, top=2)
        self.assertEqual([r["position"] for r in got], [1, 2])

    def test_features_are_separate_from_organic(self):
        page = FC.parse_brave_page(BRAVE_SAMPLE)
        self.assertEqual(page["parse_mode"], "blocks")
        kinds = {(f["kind"], f["domain"]) for f in page["features"]}
        self.assertIn(("ad", "viator.com"), kinds)
        self.assertIn(("entity", "example-client.com"), kinds)
        self.assertIn(("video", "youtube.com"), kinds)
        self.assertIn(("video", "tiktok.com"), kinds)
        self.assertIn(("discussions", "reddit.com"), kinds)
        self.assertIn(("faq", "ghosttours-rival.com"), kinds)
        self.assertIn(("infobox", "example-client.com"), kinds)
        self.assertIn(("infobox", "tripadvisor.com"), kinds)
        doms = {f["domain"] for f in page["features"]}
        self.assertNotIn("google.com", doms)        # maps link and search-elsewhere skipped
        self.assertNotIn("brave.com", doms)
        ad = [f for f in page["features"] if f["kind"] == "ad"][0]
        self.assertEqual(ad["url"], "https://www.viator.com/tours/Flagstaff/ad-landing?m=1&x=2")
        self.assertEqual(ad["title"], "Haunted Flagstaff Tour")
        organic_domains = {r["domain"] for r in page["organic"]}
        self.assertNotIn("reddit.com", organic_domains)
        self.assertNotIn("youtube.com", organic_domains)

    def test_locations_block_is_the_local_pack(self):
        local = FC.parse_brave_page(BRAVE_SAMPLE)["local_pack"]
        self.assertEqual([(e["position"], e["title"]) for e in local],
                         [(1, "Rival Ghost Walks"), (2, "Other Tours & Co")])
        self.assertEqual(local[0]["rating"], 4.9)
        self.assertEqual(local[0]["reviews"], 1216)
        self.assertIsNone(local[1]["rating"])

    def test_fallback_uses_external_links_in_order(self):
        page = FC.parse_brave_page(FALLBACK_SAMPLE)
        self.assertEqual(page["parse_mode"], "fallback")
        self.assertEqual([r["domain"] for r in page["organic"]],
                         ["alpha-tours.com", "beta.example.org", "gamma.com"])
        self.assertEqual(page["organic"][0]["title"], "Alpha Tours")
        self.assertEqual(len(FC.parse_brave_html(FALLBACK_SAMPLE, top=1)), 1)

    def test_captcha_detection(self):
        self.assertTrue(FC.looks_like_captcha(CAPTCHA_SAMPLE))
        self.assertFalse(FC.looks_like_captcha(BRAVE_SAMPLE))

    def test_garbage_does_not_raise(self):
        self.assertEqual(FC.parse_brave_html("<div data-type='web'><a href='https://x.com/"), [])
        self.assertEqual(FC.parse_brave_html(""), [])

    def test_search_url(self):
        self.assertEqual(FC.brave_search_url("flagstaff ghost tour"),
                         "https://search.brave.com/search?q=flagstaff+ghost+tour&source=web")


# =====================================================================================
# Live loop (stubbed)
# =====================================================================================
class TestBraveLoop(unittest.TestCase):
    def run_loop(self, responses, n_queries=4):
        calls, sleeps = [], []

        def stub(url, **kw):
            calls.append((url, kw))
            return responses[min(len(calls) - 1, len(responses) - 1)]

        orig = FC.fetch
        FC.fetch = stub          # the loop must look fetch up at call time
        try:
            records, status = FC.fetch_brave_serps(["q%d" % i for i in range(n_queries)],
                                                   delay=7, sleep=sleeps.append,
                                                   log=io.StringIO())
        finally:
            FC.fetch = orig
        return records, status, calls, sleeps

    def test_429_stops_early_without_retry(self):
        records, status, calls, sleeps = self.run_loop(
            [html_response(BRAVE_SAMPLE), html_response(b"", status=429)])
        self.assertEqual(len(calls), 2)                  # no third request, no retry
        self.assertEqual(len(records), 1)
        self.assertEqual(status["captured"], 1)
        self.assertEqual(status["requested"], 4)
        self.assertIn("429", status["stopped"])
        self.assertIn("1 captured", status["stopped"])
        self.assertEqual(sleeps, [7])                    # polite delay between requests only
        self.assertEqual(calls[0][1]["ua"], FC.BROWSER_UA)
        self.assertEqual(records[0]["engine_key"], "brave")
        self.assertEqual(records[0]["organic"][0]["domain"], "example-client.com")

    def test_two_failures_in_a_row_stop(self):
        records, status, calls, _ = self.run_loop(
            [html_response(b"", status=500), html_response(b"", status=503)])
        self.assertEqual(len(calls), 2)
        self.assertEqual(records, [])
        self.assertIn("Two failed responses", status["stopped"])

    def test_single_failure_does_not_stop(self):
        records, status, calls, _ = self.run_loop(
            [html_response(b"", status=500), html_response(BRAVE_SAMPLE),
             html_response(b"", status=502), html_response(BRAVE_SAMPLE)])
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(records), 2)
        self.assertIsNone(status["stopped"])

    def test_captcha_stops(self):
        records, status, calls, _ = self.run_loop([html_response(CAPTCHA_SAMPLE)])
        self.assertEqual(len(calls), 1)
        self.assertEqual(records, [])
        self.assertIn("captcha", status["stopped"])


# =====================================================================================
# SERP JSON adapters
# =====================================================================================
class TestSerpApi(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(FC.detect_serp_format(SERPAPI_SAMPLE), "serpapi")
        self.assertEqual(FC.detect_serp_format(DATAFORSEO_SAMPLE), "dataforseo")
        self.assertEqual(FC.detect_serp_format({"keyword": "k", "items": []}), "dataforseo")
        self.assertIsNone(FC.detect_serp_format({"hello": 1}))
        self.assertIsNone(FC.detect_serp_format([1, 2]))

    def test_organic_and_local_pack(self):
        r = FC.parse_serpapi(SERPAPI_SAMPLE, source="f.json")
        self.assertEqual(r["query"], "flagstaff ghost tour")
        self.assertEqual(r["engine_key"], "google")
        self.assertIn("Google", r["engine"])
        self.assertEqual([(o["position"], o["domain"]) for o in r["organic"]],
                         [(1, "rival.com"), (2, "tripadvisor.com"),
                          (3, "example-client.com"), (4, "satellite-client.com")])
        self.assertEqual([(e["title"], e["rating"], e["reviews"], e["domain"])
                          for e in r["local_pack"]],
                         [("Rival Ghost Walks", 4.9, 812, "rival.com"),
                          ("Example Client", 5.0, 1700, "example-client.com")])
        self.assertEqual([(f["kind"], f["domain"]) for f in r["features"]],
                         [("video", "youtube.com")])

    def test_local_results_as_list_and_top_cap(self):
        data = dict(SERPAPI_SAMPLE, local_results=[{"title": "Only Place", "rating": "4.5"}])
        r = FC.parse_serpapi(data, top=2)
        self.assertEqual(len(r["organic"]), 2)
        self.assertEqual(r["local_pack"][0]["title"], "Only Place")
        self.assertEqual(r["local_pack"][0]["rating"], 4.5)
        self.assertEqual(r["local_pack"][0]["position"], 1)


class TestDataForSeo(unittest.TestCase):
    def test_tasks_items_and_local_pack(self):
        recs = FC.parse_dataforseo(DATAFORSEO_SAMPLE, source="d.json")
        self.assertEqual([r["query"] for r in recs],
                         ["flagstaff walking tour", "haunted flagstaff"])  # 2nd from data.keyword
        first = recs[0]
        self.assertEqual(first["engine_key"], "google")
        self.assertEqual([(o["position"], o["slot"], o["domain"]) for o in first["organic"]],
                         [(1, 3, "rival.com"), (2, 5, "example-client.com")])
        self.assertEqual(first["local_pack"], [{"position": 1, "title": "Rival Ghost Walks",
                                                "rating": 4.8, "reviews": 120,
                                                "domain": "rival.com"}])
        self.assertIn(("ad", "viator.com"), {(f["kind"], f["domain"]) for f in first["features"]})
        second = recs[1]
        self.assertEqual([(o["position"], o["domain"]) for o in second["organic"]],
                         [(1, "en.wikipedia.org")])

    def test_bare_result_object(self):
        res = DATAFORSEO_SAMPLE["tasks"][0]["result"][0]
        recs = FC.parse_dataforseo(res)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["query"], "flagstaff walking tour")

    def test_task_without_result_is_reported(self):
        errors = []
        data = {"tasks": [{"status_message": "No Search Results.", "data": {"keyword": "zz"},
                           "result": None}]}
        self.assertEqual(FC.parse_dataforseo(data, source="x.json", errors=errors), [])
        self.assertEqual(len(errors), 1)
        self.assertIn("x.json", errors[0])


class TestLoadSerpFile(unittest.TestCase):
    def test_bad_file_is_named_and_others_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "good.json")
            bad = os.path.join(tmp, "bad.json")
            broken = os.path.join(tmp, "broken.json")
            listed = os.path.join(tmp, "list.json")
            with open(good, "w") as fh:
                json.dump(SERPAPI_SAMPLE, fh)
            with open(bad, "w") as fh:
                json.dump({"hello": "world"}, fh)
            with open(broken, "w") as fh:
                fh.write("{not json")
            with open(listed, "w") as fh:
                json.dump([SERPAPI_SAMPLE, {"nope": 1}], fh)
            recs, errs = FC.load_serp_file(good)
            self.assertEqual((len(recs), errs), (1, []))
            recs, errs = FC.load_serp_file(bad)
            self.assertEqual(recs, [])
            self.assertIn("bad.json", errs[0])
            self.assertIn("not a recognized SERP export", errs[0])
            recs, errs = FC.load_serp_file(broken)
            self.assertEqual(recs, [])
            self.assertIn("broken.json", errs[0])
            recs, errs = FC.load_serp_file(listed)
            self.assertEqual(len(recs), 1)
            self.assertIn("item 2", errs[0])


# =====================================================================================
# Classification
# =====================================================================================
class TestClassify(unittest.TestCase):
    OWN = ("example-client.com", "satellite-client.com")

    def cat(self, d, overrides=None):
        return FC.classify_domain(d, self.OWN, overrides)[0]

    def test_builtin_table_and_heuristics(self):
        self.assertEqual(self.cat("tripadvisor.com"), "marketplace")
        self.assertEqual(self.cat("tripadvisor.co.uk"), "marketplace")
        self.assertEqual(self.cat("expedia-aarp.com"), "marketplace")
        self.assertEqual(self.cat("m.facebook.com"), "social")
        self.assertEqual(self.cat("x.com"), "social")
        self.assertEqual(self.cat("box.com"), "business")          # "x.com" is a domain entry
        self.assertEqual(self.cat("en.wikipedia.org"), "reference")
        self.assertEqual(self.cat("flagstaffarizona.org"), "directory")
        self.assertEqual(self.cat("visitarizona.com"), "directory")
        self.assertEqual(self.cat("azfamily.com"), "news")
        self.assertEqual(self.cat("fox10phoenix.com"), "news")
        self.assertEqual(self.cat("azdailysun.com"), "news")
        self.assertEqual(self.cat("ghostcitytours.com"), "business")

    def test_own_domains_and_subdomains(self):
        self.assertEqual(self.cat("example-client.com"), "own")
        self.assertEqual(self.cat("shop.example-client.com"), "own")
        self.assertEqual(self.cat("satellite-client.com"), "own")
        self.assertEqual(self.cat("notexample-client.com"), "business")

    def test_classify_file_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "classes.json")
            with open(path, "w") as fh:
                json.dump({"ghostcitytours.com": "OTA", "blog": ["www.travel-blog.com"],
                           "flagstaffarizona.org": "competitor"}, fh)
            ov = FC.load_classify_file(path)
        self.assertEqual(self.cat("ghostcitytours.com", ov), "marketplace")   # alias
        self.assertEqual(self.cat("travel-blog.com", ov), "blog")             # custom
        self.assertEqual(self.cat("flagstaffarizona.org", ov), "business")
        self.assertEqual(FC.classify_domain("ghostcitytours.com", (), ov)[1], "classify-file")


# =====================================================================================
# Tally, score and report
# =====================================================================================
class TestTally(unittest.TestCase):
    def test_visibility_score_arithmetic(self):
        self.assertAlmostEqual(FC.visibility_score([1]), 1.0)
        self.assertAlmostEqual(FC.visibility_score([1, 4]), 1.25)
        self.assertAlmostEqual(FC.visibility_score([2, 5, 10]), 0.8)
        self.assertEqual(FC.visibility_score([]), 0)

    def test_tally(self):
        recs = [rec("q1", ["a.com", "rival.com", "b.com", "c.com", "rival.com"]),
                rec("q2", ["x.com", "y.com", "z.com", "w.com", "rival.com"])]
        s = FC.tally_domains(recs)["rival.com"]
        self.assertEqual(s["queries"], ["q1", "q2"])
        self.assertEqual(s["n_queries"], 2)
        self.assertEqual(s["appearances"], 3)
        self.assertEqual(s["best_pos"], 2)
        self.assertEqual(s["best_url"], "https://rival.com/p2")
        self.assertEqual(s["positions"], {"q1": [2, 5], "q2": [5]})
        self.assertAlmostEqual(s["avg_pos"], 4.0)
        self.assertAlmostEqual(s["score"], 0.9)        # 1/2 + 1/5 + 1/5

    def test_report_shortlist_own_and_categories(self):
        recs = [rec("q1", ["example-client.com", "rival.com", "tripadvisor.com",
                           "visitflagstaff.org", "unknown-tours.com"]),
                rec("q2", ["tripadvisor.com", "satellite-client.com", "rival.com"])]
        r = FC.build_report(recs, "www.example-client.com",
                            owned=["satellite-client.com", "never-seen.com"],
                            requested=["q1", "q2", "q3"], shortlist_size=8, date="2026-01-02")
        short = [s["domain"] for s in r["shortlist"]]
        self.assertEqual(short, ["rival.com", "unknown-tours.com"])   # 0.5+0.333 > 0.2
        self.assertNotIn("example-client.com", short)
        self.assertNotIn("satellite-client.com", short)
        own = {s["domain"]: s for s in r["own"]}
        self.assertEqual(own["example-client.com"]["role"], "client")
        self.assertEqual(own["example-client.com"]["best_pos"], 1)
        self.assertEqual(own["satellite-client.com"]["role"], "owned")
        self.assertEqual(own["satellite-client.com"]["positions"], {"q2": [2]})
        self.assertEqual(own["never-seen.com"]["n_queries"], 0)
        self.assertEqual(r["by_category"]["marketplace"], ["tripadvisor.com"])
        self.assertEqual(r["by_category"]["directory"], ["visitflagstaff.org"])
        self.assertEqual((r["queries_requested"], r["queries_captured"]), (3, 2))
        self.assertEqual(r["not_captured"], ["q3"])
        self.assertFalse(r["all_google"])

    def test_render_markdown(self):
        recs = [rec("flagstaff ghost tour", ["example-client.com", "rival.com",
                                             "tripadvisor.com", "youtube.com"]),
                rec("flagstaff walking tour", ["rival.com", "flagstaffarizona.org"])]
        r = FC.build_report(recs, "example-client.com", owned=["satellite-client.com"],
                            date="2026-01-02")
        md = FC.render_markdown(r)
        head = md.split("## Competitor shortlist")[0]
        self.assertIn(FC.PROXY_CAVEAT, head)
        self.assertIn("Engine: Brave Search (test)", head)
        self.assertIn("Date: 2026-01-02", head)
        self.assertIn("Queries: 2 requested, 2 captured", head)
        short = md.split("## Competitor shortlist")[1].split("## Marketplaces")[0]
        self.assertIn("| 1 | rival.com | 2/2 | 1 | 1.5 | 1.500 |", short)
        self.assertIn("python3 scripts/seo_audit.py https://rival.com --json", short)
        self.assertIn("rival.com (S1)", md)
        self.assertIn("example-client.com (client)", md)
        self.assertIn("## Own domains", md)
        self.assertIn("satellite-client.com | owned | 0/2", md)
        self.assertNotIn("\u2014", md)                       # no em dashes

    def test_render_google_only_wording(self):
        recs = [rec("q", ["rival.com"], engine="Google (SerpApi export)", engine_key="google")]
        r = FC.build_report(recs, "example-client.com", date="2026-01-02")
        md = FC.render_markdown(r)
        self.assertTrue(r["all_google"])
        self.assertIn(FC.PROXY_CAVEAT, md)
        self.assertIn("came from Google through a provider export", md)


# =====================================================================================
# CLI (no network)
# =====================================================================================
class TestCli(unittest.TestCase):
    def test_smoke_with_serp_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = os.path.join(tmp, "serp.json")
            bad = os.path.join(tmp, "bad.json")
            dfs = os.path.join(tmp, "dfs.json")
            with open(good, "w") as fh:
                json.dump(SERPAPI_SAMPLE, fh)
            with open(bad, "w") as fh:
                json.dump({"hello": "world"}, fh)
            with open(dfs, "w") as fh:
                json.dump(DATAFORSEO_SAMPLE, fh)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                FC.main(["--client", "https://www.example-client.com/",
                         "--owned", "satellite-client.com",
                         "--serp-json", good, bad, dfs,
                         "--date", "2026-01-02", "--out", tmp])
            md_path = os.path.join(tmp, "competitors-example-client.com-2026-01-02.md")
            json_path = os.path.join(tmp, "competitors-example-client.com-2026-01-02.json")
            self.assertTrue(os.path.exists(md_path))
            self.assertTrue(os.path.exists(json_path))
            self.assertIn(md_path, out.getvalue())
            self.assertIn(json_path, out.getvalue())
            self.assertIn("rival.com", out.getvalue())
            self.assertIn("bad.json", err.getvalue())
            with open(json_path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual(data["date"], "2026-01-02")
            self.assertEqual(data["queries_captured"], 3)
            self.assertTrue(data["all_google"])
            self.assertEqual(data["shortlist"][0]["domain"], "rival.com")
            self.assertEqual([s["domain"] for s in data["own"]],
                             ["example-client.com", "satellite-client.com"])
            self.assertEqual(len(data["errors"]), 1)
            with open(md_path, encoding="utf-8") as fh:
                md = fh.read()
            self.assertIn(FC.PROXY_CAVEAT, md)
            self.assertIn("## Local pack", md)
            self.assertIn("Rival Ghost Walks", md)

    def test_queries_select_from_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            dfs = os.path.join(tmp, "dfs.json")
            with open(dfs, "w") as fh:
                json.dump(DATAFORSEO_SAMPLE, fh)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                FC.main(["--client", "example-client.com", "--serp-json", dfs,
                         "--queries", "Flagstaff  Walking Tour", "not exported",
                         "--date", "2026-01-02", "--out", tmp])
            with open(os.path.join(tmp, "competitors-example-client.com-2026-01-02.json"),
                      encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertEqual((data["queries_requested"], data["queries_captured"]), (2, 1))
            self.assertEqual(data["not_captured"], ["not exported"])

    def test_live_path_uses_stubbed_fetch_and_writes_nothing_when_blocked(self):
        orig = FC.fetch
        with tempfile.TemporaryDirectory() as tmp:
            try:
                FC.fetch = lambda url, **kw: html_response(BRAVE_SAMPLE)
                with contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    FC.main(["--client", "example-client.com", "--queries", "q one",
                             "--delay", "0", "--date", "2026-01-02", "--out", tmp])
                with open(os.path.join(tmp, "competitors-example-client.com-2026-01-02.md"),
                          encoding="utf-8") as fh:
                    self.assertIn("Engine: " + FC.BRAVE_ENGINE, fh.read())

                FC.fetch = lambda url, **kw: html_response(b"", status=429)
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    with self.assertRaises(SystemExit):
                        FC.main(["--client", "blocked.com", "--queries", "q", "--delay", "0",
                                 "--date", "2026-01-02", "--out", tmp])
                self.assertIn("429", err.getvalue())
                self.assertFalse(os.path.exists(
                    os.path.join(tmp, "competitors-blocked.com-2026-01-02.md")))
            finally:
                FC.fetch = orig

    def test_needs_queries_or_json(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                FC.main(["--client", "example.com"])


if __name__ == "__main__":
    unittest.main()
