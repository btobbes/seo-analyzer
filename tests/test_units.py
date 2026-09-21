"""Pure unit tests for scripts/seo_audit.py — no network, no server.

Run with:  python3 -m unittest discover -s tests -v   (from the repo root)

Several tests here were written as regression tests for real defects (malformed @type,
truncated images, text glued across elements, CSP ports); keep them.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import seo_audit as SA                                            # noqa: E402
import fixture_site as FX                                         # noqa: E402


# =====================================================================================
# Helpers
# =====================================================================================
def make_page(html, url="https://example.com/", *, status=200, headers=None,
              final_url=None, body=None):
    """Build the ``page`` dict the per-page checks expect, the way analyze_page does."""
    p = SA.PageParser()
    try:
        p.feed(html)
    except Exception:
        pass
    raw = body if body is not None else html.encode("utf-8")
    resp = SA.Response(url, final_url or url, status, dict(headers or {}), raw, 12.0)
    types, _valid, _invalid = SA.parse_jsonld_types(p.jsonld)
    return {
        "url": url, "final_url": final_url or url, "status": status,
        "parser": p, "response": resp, "schema_types": sorted(types),
        "findings": [], "word_count": p.word_count, "title": p.title,
        "description": p.meta_description, "canonical": p.canonical,
        "robots": p.meta_robots,
    }


def titles(findings):
    return [f["title"] for f in findings]


def by_title(findings, needle):
    return [f for f in findings if needle.lower() in f["title"].lower()]


def html_doc(body, head="", lang="en", title="A perfectly ordinary fixture page title"):
    return ('<!doctype html><html lang="%s"><head><meta charset="utf-8">'
            "<title>%s</title>%s</head><body>%s</body></html>"
            % (lang, title, head, body))


# =====================================================================================
# url_template / cluster_urls
# =====================================================================================
class TestUrlTemplate(unittest.TestCase):
    def test_root(self):
        self.assertEqual(SA.url_template("https://e.com/"), "/")
        self.assertEqual(SA.url_template("https://e.com"), "/")

    def test_single_segment_collapses(self):
        self.assertEqual(SA.url_template("https://e.com/about"), "/*")
        self.assertEqual(SA.url_template("https://e.com/contact"), "/*")

    def test_trailing_slash_is_part_of_the_shape(self):
        self.assertEqual(SA.url_template("https://e.com/about/"), "/*/")
        self.assertEqual(SA.url_template("https://e.com/blog/post"), "/blog/*")
        self.assertEqual(SA.url_template("https://e.com/blog/post/"), "/blog/*/")

    def test_first_segment_is_kept_and_years_are_generalised(self):
        self.assertEqual(SA.url_template("https://e.com/blog/2024/post"), "/blog/*/*")
        self.assertEqual(SA.url_template("https://e.com/2024/us/ca/sf"),
                         "/{year}/*/*/*")
        self.assertEqual(SA.url_template("https://e.com/1999/a"), "/{year}/*")
        # not a year -> kept verbatim
        self.assertEqual(SA.url_template("https://e.com/12345/a"), "/12345/*")

    def test_query_and_fragment_ignored(self):
        self.assertEqual(SA.url_template("https://e.com/blog/post?x=1"), "/blog/*")

    def test_cluster_urls_groups_by_shape(self):
        urls = ["https://e.com/blog/a", "https://e.com/blog/b", "https://e.com/about"]
        clusters = SA.cluster_urls(urls)
        self.assertEqual(sorted(clusters), ["/*", "/blog/*"])
        self.assertEqual(len(clusters["/blog/*"]), 2)


# =====================================================================================
# _spread / stratified_sample
# =====================================================================================
class TestSpread(unittest.TestCase):
    def test_evenly_spaced_not_the_first_k(self):
        self.assertEqual(SA._spread(list(range(10)), 3), [0, 3, 6])

    def test_k_at_or_over_length_returns_everything(self):
        self.assertEqual(SA._spread([1, 2, 3], 3), [1, 2, 3])
        self.assertEqual(SA._spread([1, 2, 3], 9), [1, 2, 3])

    def test_non_positive_k(self):
        self.assertEqual(SA._spread([1, 2, 3], 0), [])
        self.assertEqual(SA._spread([1, 2, 3], -4), [])

    def test_empty_input(self):
        self.assertEqual(SA._spread([], 5), [])

    def test_never_repeats_an_item(self):
        for n in range(1, 20):
            for k in range(1, n + 1):
                got = SA._spread(list(range(n)), k)
                self.assertEqual(len(got), len(set(got)), (n, k))


class TestStratifiedSample(unittest.TestCase):
    BLOG = ["https://e.com/blog/%d" % i for i in range(10)]
    NEWS = ["https://e.com/news/a"]
    FLAT = ["https://e.com/about"]
    ALL = BLOG + NEWS + FLAT

    def test_every_template_before_any_second_slot(self):
        got = SA.stratified_sample(self.ALL, 3)
        self.assertEqual(len(got), 3)
        self.assertEqual({SA.url_template(u) for u in got},
                         {"/blog/*", "/news/*", "/*"})

    def test_is_not_simply_the_first_n(self):
        got = SA.stratified_sample(self.ALL, 3)
        self.assertNotEqual(got, self.ALL[:3])

    def test_second_slots_go_to_the_largest_cluster(self):
        got = SA.stratified_sample(self.ALL, 5)
        self.assertEqual(len(got), 5)
        tmpl = [SA.url_template(u) for u in got]
        self.assertEqual(tmpl.count("/blog/*"), 3)
        self.assertEqual(tmpl.count("/news/*"), 1)
        self.assertEqual(tmpl.count("/*"), 1)

    def test_spreads_inside_a_cluster(self):
        got = SA.stratified_sample(self.ALL, 5)
        blog = [u for u in got if SA.url_template(u) == "/blog/*"]
        self.assertEqual(blog, ["https://e.com/blog/0", "https://e.com/blog/3",
                                "https://e.com/blog/6"])

    def test_respects_exclude_ignoring_trailing_slash(self):
        got = SA.stratified_sample(self.ALL, 12, exclude=["https://e.com/about/"])
        self.assertNotIn("https://e.com/about", got)
        self.assertEqual(len(got), 11)

    def test_n_greater_than_pool_returns_whole_pool(self):
        got = SA.stratified_sample(self.ALL, 100)
        self.assertEqual(sorted(got), sorted(self.ALL))

    def test_n_not_positive(self):
        self.assertEqual(SA.stratified_sample(self.ALL, 0), [])
        self.assertEqual(SA.stratified_sample(self.ALL, -3), [])

    def test_empty_pool(self):
        self.assertEqual(SA.stratified_sample([], 5), [])
        self.assertEqual(SA.stratified_sample(["https://e.com/a"], 5,
                                              exclude=["https://e.com/a"]), [])

    def test_deduplicates_input(self):
        got = SA.stratified_sample(["https://e.com/a", "https://e.com/a"], 5)
        self.assertEqual(got, ["https://e.com/a"])


# =====================================================================================
# parse_robots
# =====================================================================================
class TestParseRobots(unittest.TestCase):
    def test_groups_for_the_same_agent_merge_and_allow_wins(self):
        # The Cloudflare-managed "Disallow: /" block followed by the operator's own
        # "Allow: /" block: RFC 9309 merges them and Allow wins the tie.
        txt = ("# Cloudflare managed block\n"
               "User-agent: GPTBot\n"
               "Disallow: /\n"
               "\n"
               "User-agent: GPTBot\n"
               "Allow: /\n"
               "\n"
               "User-agent: *\n"
               "Disallow: /private\n"
               "Sitemap: https://e.com/sitemap.xml\n")
        info = SA.parse_robots(txt)
        self.assertEqual(info["ai_blocked"], [])
        self.assertFalse(info["blocks_all"])
        self.assertEqual(info["sitemaps"], ["https://e.com/sitemap.xml"])

    def test_allow_wins_inside_one_group(self):
        info = SA.parse_robots("User-agent: *\nDisallow: /\nAllow: /\n")
        self.assertFalse(info["blocks_all"])

    def test_site_wide_disallow(self):
        info = SA.parse_robots("User-agent: *\nDisallow: /\n")
        self.assertTrue(info["blocks_all"])
        # every AI crawler falls back to the * group
        self.assertEqual(set(info["ai_blocked"]), set(SA.AI_CRAWLERS))

    def test_consecutive_user_agent_lines_form_one_group(self):
        info = SA.parse_robots("User-agent: GPTBot\nUser-agent: CCBot\nDisallow: /\n")
        self.assertEqual(set(info["ai_blocked"]), {"GPTBot", "CCBot"})
        self.assertFalse(info["blocks_all"])

    def test_own_group_beats_the_star_fallback(self):
        info = SA.parse_robots("User-agent: *\nDisallow: /\n\n"
                               "User-agent: GPTBot\nAllow: /\n")
        self.assertTrue(info["blocks_all"])
        self.assertNotIn("GPTBot", info["ai_blocked"])
        self.assertIn("CCBot", info["ai_blocked"])

    def test_wildcard_suffix_on_the_root_path_still_counts(self):
        info = SA.parse_robots("User-agent: ClaudeBot\nDisallow: /*\n")
        self.assertIn("ClaudeBot", info["ai_blocked"])

    def test_path_specific_disallow_is_not_a_root_block(self):
        info = SA.parse_robots("User-agent: ClaudeBot\nDisallow: /admin\n")
        self.assertEqual(info["ai_blocked"], [])

    def test_empty_disallow_means_allow_all(self):
        info = SA.parse_robots("User-agent: *\nDisallow:\n")
        self.assertFalse(info["blocks_all"])

    def test_comments_and_blank_lines_ignored(self):
        info = SA.parse_robots("# hello\n\n   \nUser-agent: *  # star\nDisallow: /  # all\n")
        self.assertTrue(info["blocks_all"])

    def test_empty_file(self):
        info = SA.parse_robots("")
        self.assertEqual(info, {"sitemaps": [], "ai_blocked": [], "blocks_all": False})


# =====================================================================================
# robots_platform_signals
# =====================================================================================
class TestRobotsPlatformSignals(unittest.TestCase):
    def test_content_signal_search_no_and_ai_input_no(self):
        txt = ("User-agent: *\nAllow: /\n"
               "Content-Signal: search=no, ai-input=no, ai-train=yes\n")
        findings, row = SA.robots_platform_signals(txt)
        got = {(f["severity"], f["category"], f["title"]) for f in findings}
        self.assertIn(("HIGH", "crawlability",
                       "robots.txt AI-preference signal says search=no"), got)
        self.assertIn(("MEDIUM", "ai search",
                       "robots.txt AI-preference signal opts out of AI answers"), got)
        self.assertIn("search=no", row[1])

    def test_ietf_content_usage_line(self):
        findings, row = SA.robots_platform_signals("Content-Usage: ai-use=n\n")
        self.assertEqual(titles(findings),
                         ["robots.txt AI-preference signal opts out of AI answers"])
        self.assertIn("ai-use=n", row[1])

    def test_content_usage_response_header(self):
        resp = SA.Response("u", "u", 200, {"Content-Usage": "ai-use=n"}, b"", 1.0)
        findings, row = SA.robots_platform_signals("User-agent: *\nAllow: /\n", resp)
        self.assertEqual(titles(findings),
                         ["robots.txt AI-preference signal opts out of AI answers"])

    def test_content_signal_yes_produces_nothing(self):
        findings, row = SA.robots_platform_signals(
            "Content-Signal: search=yes, ai-input=yes\n")
        self.assertEqual(findings, [])
        self.assertIn("search=yes", row[1])

    def test_cloudflare_managed_marker(self):
        findings, row = SA.robots_platform_signals(
            "# BEGIN Cloudflare Managed content\nUser-agent: *\nAllow: /\n"
            "# END Cloudflare Managed content\n")
        self.assertEqual(titles(findings), ["robots.txt is partly written by the CDN"])
        self.assertEqual(findings[0]["severity"], "INFO")
        self.assertTrue(row[1].startswith("CDN-managed block present"))

    def test_a_mere_mention_of_cloudflare_is_not_the_marker(self):
        findings, row = SA.robots_platform_signals(
            "# hosted behind cloudflare\nUser-agent: *\nAllow: /\n")
        self.assertEqual(findings, [])
        self.assertFalse(row[1].startswith("CDN-managed"))

    def test_no_signals_at_all(self):
        findings, row = SA.robots_platform_signals("User-agent: *\nAllow: /\n")
        self.assertEqual(findings, [])
        self.assertEqual(row[1], "no Content-Signal / Content-Usage line")


class TestAiCrawlerRoster(unittest.TestCase):
    def test_every_probed_agent_has_a_declared_role(self):
        for token, role, _operator, _ua in SA.AI_AGENT_PROBES:
            with self.subTest(agent=token):
                self.assertIn(role, ("search", "user", "train"))
                declared = SA.AI_CRAWLER_ROLES.get(token)
                # Applebot is probed as a search crawler but is a classic search bot,
                # so it is not in the AI opt-out roster; everything else must agree.
                if declared is not None:
                    self.assertEqual(declared, role)

    def test_the_robots_roster_is_the_roles_dict(self):
        self.assertEqual(SA.AI_CRAWLERS, list(SA.AI_CRAWLER_ROLES))

    def test_agents_are_probed_with_their_own_token_in_the_user_agent(self):
        for token, _role, _operator, ua in SA.AI_AGENT_PROBES:
            with self.subTest(agent=token):
                self.assertIn(token.lower(), ua.lower())

    def test_retired_tokens_are_gone(self):
        for token in ("cohere-ai", "anthropic-ai", "Claude-Web"):
            self.assertNotIn(token, SA.AI_CRAWLERS)


# =====================================================================================
# _csp_allows / check_csp_blocks
# =====================================================================================
PAGE_URL = "https://example.com/here"


class TestCspAllows(unittest.TestCase):
    def test_host_not_listed_is_not_allowed(self):
        self.assertFalse(SA._csp_allows(["'self'", "https://ok.example"],
                                        "https://bad.example/a.js", PAGE_URL))

    def test_self_allows_the_page_host(self):
        self.assertTrue(SA._csp_allows(["'self'"], "https://example.com/a.js", PAGE_URL))

    def test_self_does_not_allow_another_host(self):
        self.assertFalse(SA._csp_allows(["'self'"], "https://cdn.example.com/a.js",
                                        PAGE_URL))

    def test_wildcard_subdomain(self):
        self.assertTrue(SA._csp_allows(["https://*.example.com"],
                                       "https://cdn.example.com/a.js", PAGE_URL))

    def test_wildcard_subdomain_does_not_match_the_bare_domain(self):
        self.assertFalse(SA._csp_allows(["https://*.example.com"],
                                        "https://example.com/a.js", PAGE_URL))

    def test_wildcard_subdomain_does_not_match_a_lookalike(self):
        self.assertFalse(SA._csp_allows(["https://*.example.com"],
                                        "https://evilexample.com/a.js", PAGE_URL))

    def test_scheme_source(self):
        self.assertTrue(SA._csp_allows(["https:"], "https://anything.example/a.js",
                                       PAGE_URL))
        self.assertFalse(SA._csp_allows(["https:"], "http://anything.example/a.js",
                                        PAGE_URL))

    def test_star(self):
        self.assertTrue(SA._csp_allows(["*"], "https://anything.example/a.js", PAGE_URL))

    def test_exact_host_with_scheme_prefix(self):
        self.assertTrue(SA._csp_allows(["https://cdn.example.net"],
                                       "https://cdn.example.net/a.js", PAGE_URL))

    def test_quoted_keywords_other_than_self_are_skipped(self):
        self.assertFalse(SA._csp_allows(["'unsafe-inline'", "'nonce-abc'"],
                                        "https://cdn.example.net/a.js", PAGE_URL))

    # ---- BUG -----------------------------------------------------------------------
    def test_port_in_a_csp_source_is_honoured(self):
        """ANALYZER BUG: _csp_allows strips the port from a host-source.

        scripts/seo_audit.py line 2279:
            th = re.sub(r"^[a-z]+://", "", t).split("/")[0].split(":")[0]
        `.split(":")[0]` throws away the port, so `script-src https://cdn.example:8443`
        is treated as allowing a script on ANY port of cdn.example. CSP compares the
        full host-source including port, so this silently misses a real block.
        """
        self.assertFalse(SA._csp_allows(["https://cdn.example.net:8443"],
                                        "https://cdn.example.net:9999/a.js", PAGE_URL))


class TestCheckCspBlocks(unittest.TestCase):
    def _page(self, csp, head, url=PAGE_URL):
        return make_page(html_doc("<p>body</p>", head=head), url=url,
                         headers={"Content-Security-Policy": csp} if csp else {})

    def test_blocked_cross_origin_script_is_flagged(self):
        page = self._page("default-src 'self'; script-src 'self'",
                          '<script src="https://cdn.other.example/a.js"></script>')
        out = SA.check_csp_blocks(page)
        self.assertEqual(titles(out),
                         ["Content-Security-Policy blocks a script the page loads"])
        self.assertEqual(out[0]["severity"], "MEDIUM")
        self.assertEqual(out[0]["category"], "performance")
        self.assertIn("cdn.other.example", out[0]["observed"])

    def test_same_origin_script_under_self_is_not_flagged(self):
        page = self._page("script-src 'self'", '<script src="/app.js"></script>')
        self.assertEqual(SA.check_csp_blocks(page), [])

    def test_wildcard_subdomain_source_is_not_flagged(self):
        page = self._page("script-src https://*.other.example",
                          '<script src="https://cdn.other.example/a.js"></script>')
        self.assertEqual(SA.check_csp_blocks(page), [])

    def test_scheme_source_is_not_flagged(self):
        page = self._page("script-src https:",
                          '<script src="https://cdn.other.example/a.js"></script>')
        self.assertEqual(SA.check_csp_blocks(page), [])

    def test_nonced_script_is_not_flagged(self):
        page = self._page("script-src 'self' 'nonce-abc123'",
                          '<script nonce="abc123" src="https://cdn.other.example/a.js">'
                          "</script>")
        self.assertEqual(SA.check_csp_blocks(page), [])

    def test_strict_dynamic_disables_the_check(self):
        page = self._page("script-src 'self' 'strict-dynamic'",
                          '<script src="https://cdn.other.example/a.js"></script>')
        self.assertEqual(SA.check_csp_blocks(page), [])

    def test_falls_back_to_default_src(self):
        page = self._page("default-src 'self'",
                          '<script src="https://cdn.other.example/a.js"></script>')
        self.assertEqual(titles(SA.check_csp_blocks(page)),
                         ["Content-Security-Policy blocks a script the page loads"])

    def test_script_src_elem_wins_over_script_src(self):
        page = self._page("script-src 'self'; script-src-elem https://cdn.other.example",
                          '<script src="https://cdn.other.example/a.js"></script>')
        self.assertEqual(SA.check_csp_blocks(page), [])

    def test_no_csp_header_means_no_finding(self):
        page = self._page(None, '<script src="https://cdn.other.example/a.js"></script>')
        self.assertEqual(SA.check_csp_blocks(page), [])

    def test_json_ld_script_is_not_treated_as_a_blocked_script(self):
        page = self._page("script-src 'self'",
                          '<script type="application/ld+json" '
                          'src="https://cdn.other.example/a.json"></script>')
        self.assertEqual(SA.check_csp_blocks(page), [])


# =====================================================================================
# sitemap_meta_findings
# =====================================================================================
def sm_meta(total=0, with_lastmod=0, lastmods=None, **kw):
    meta = {"urls": [], "total": total, "with_lastmod": with_lastmod,
            "lastmods": list(lastmods or []), "has_images": False,
            "files_read": 1, "files_listed": 0, "truncated": False}
    meta.update(kw)
    return meta


class TestSitemapMetaFindings(unittest.TestCase):
    def test_many_urls_without_lastmod(self):
        out = SA.sitemap_meta_findings(
            sm_meta(total=40, with_lastmod=5, lastmods=["2026-01-0%d" % i for i in range(1, 6)]))
        self.assertEqual(titles(out), ["Many sitemap URLs carry no <lastmod>"])
        self.assertEqual(out[0]["severity"], "LOW")
        self.assertEqual(out[0]["category"], "crawlability")
        self.assertIn("5 of 40", out[0]["observed"])

    def test_enough_lastmod_coverage_is_silent(self):
        out = SA.sitemap_meta_findings(
            sm_meta(total=10, with_lastmod=9,
                    lastmods=["2026-01-%02d" % i for i in range(1, 10)]))
        self.assertEqual(out, [])

    def test_a_small_sitemap_is_not_judged_on_lastmod(self):
        self.assertEqual(SA.sitemap_meta_findings(sm_meta(total=9, with_lastmod=0)), [])

    def test_every_lastmod_identical(self):
        out = SA.sitemap_meta_findings(
            sm_meta(total=30, with_lastmod=30, lastmods=["2026-04-04"] * 30))
        self.assertEqual(titles(out), ["Every sitemap <lastmod> is the same date"])
        self.assertIn("2026-04-04", out[0]["observed"])

    def test_identical_lastmod_needs_twenty_entries(self):
        out = SA.sitemap_meta_findings(
            sm_meta(total=19, with_lastmod=19, lastmods=["2026-04-04"] * 19))
        self.assertNotIn("Every sitemap <lastmod> is the same date", titles(out))

    def test_future_lastmod(self):
        out = SA.sitemap_meta_findings(
            sm_meta(total=2, with_lastmod=2, lastmods=["2099-01-01", "2026-01-01"]))
        self.assertEqual(titles(out), ["Sitemap <lastmod> dates in the future"])
        self.assertIn("2099-01-01", out[0]["observed"])

    def test_empty_sitemap_meta(self):
        self.assertEqual(SA.sitemap_meta_findings(sm_meta()), [])


# =====================================================================================
# _registrable_candidates
# =====================================================================================
class TestRegistrableCandidates(unittest.TestCase):
    def test_www_is_stripped(self):
        self.assertEqual(SA._registrable_candidates("www.example.com"), ["example.com"])

    def test_two_label_host(self):
        self.assertEqual(SA._registrable_candidates("example.com"), ["example.com"])

    def test_subdomains_widen_towards_the_registrable_domain(self):
        self.assertEqual(SA._registrable_candidates("a.b.example.co.uk"),
                         ["a.b.example.co.uk", "b.example.co.uk", "example.co.uk",
                          "co.uk"])

    def test_single_label_host(self):
        self.assertEqual(SA._registrable_candidates("localhost"), ["localhost"])

    def test_leading_dot_and_case(self):
        self.assertEqual(SA._registrable_candidates(".WWW.Example.COM"), ["example.com"])


# =====================================================================================
# check_schema_depth
# =====================================================================================
def ld(obj):
    return '<script type="application/ld+json">%s</script>' % obj


class TestCheckSchemaDepth(unittest.TestCase):
    def test_self_serving_aggregate_rating_on_local_business(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@context":"https://schema.org","@type":"LocalBusiness",'
            '"name":"Acme","aggregateRating":{"@type":"AggregateRating",'
            '"ratingValue":"4.9","reviewCount":"212"}}')))
        out = SA.check_schema_depth(page, is_home=False)
        hit = by_title(out, "Self-serving review markup")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["severity"], "MEDIUM")
        self.assertEqual(hit[0]["category"], "schema")
        self.assertIn("LocalBusiness", hit[0]["observed"])

    def test_aggregate_rating_on_a_product_is_not_self_serving(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@type":"Product","name":"Widget",'
            '"aggregateRating":{"ratingValue":"4.2","reviewCount":"9"}}')))
        self.assertEqual(by_title(SA.check_schema_depth(page, False),
                                  "Self-serving review markup"), [])

    def test_aggregate_rating_pointing_at_another_domain_is_not_flagged(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@type":"Restaurant","name":"Acme","url":"https://elsewhere.test/acme",'
            '"aggregateRating":{"ratingValue":"4.2"}}')))
        self.assertEqual(by_title(SA.check_schema_depth(page, False),
                                  "Self-serving review markup"), [])

    def test_thin_homepage_organization(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@context":"https://schema.org","@type":"Organization","name":"Acme"}')))
        out = SA.check_schema_depth(page, is_home=True)
        hit = by_title(out, "Homepage Organization schema is thin")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["severity"], "LOW")
        self.assertEqual(hit[0]["category"], "schema")
        for want in ("logo", "sameAs", "legalName", "address", "foundingDate"):
            self.assertIn(want, hit[0]["observed"])

    def test_complete_homepage_organization_is_not_flagged(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@type":"Organization","name":"Acme","legalName":"Acme LLC",'
            '"logo":"https://example.com/l.png","sameAs":["https://x.test/acme"],'
            '"foundingDate":"2015-01-01","telephone":"+1 555 000 0000",'
            '"address":{"@type":"PostalAddress","streetAddress":"1 Main Street"}}')))
        self.assertEqual(by_title(SA.check_schema_depth(page, True),
                                  "Homepage Organization schema is thin"), [])

    def test_thin_organization_is_only_judged_on_the_homepage(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@type":"Organization","name":"Acme"}')))
        self.assertEqual(by_title(SA.check_schema_depth(page, False),
                                  "Homepage Organization schema is thin"), [])

    def test_article_missing_image_and_author_url(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@type":"Article","headline":"Hello","datePublished":"2026-01-01",'
            '"dateModified":"2026-01-02","author":{"@type":"Person","name":"Ann"}}')))
        out = SA.check_schema_depth(page, False)
        hit = by_title(out, "Article schema is missing recommended properties")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["severity"], "LOW")
        self.assertEqual(hit[0]["category"], "schema")
        self.assertIn("image", hit[0]["observed"])
        self.assertIn("author.url", hit[0]["observed"])

    def test_complete_article_is_not_flagged(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@type":"Article","headline":"Hello","image":"https://example.com/a.png",'
            '"datePublished":"2026-01-01","dateModified":"2026-01-02",'
            '"author":{"@type":"Person","name":"Ann","url":"https://example.com/ann"}}')))
        self.assertEqual(by_title(SA.check_schema_depth(page, False),
                                  "Article schema is missing"), [])

    def test_faqpage_produces_only_an_info_finding(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@type":"FAQPage","mainEntity":[{"@type":"Question","name":"Why?",'
            '"acceptedAnswer":{"@type":"Answer","text":"Because."}}]}')))
        out = SA.check_schema_depth(page, is_home=False)
        self.assertEqual([f["severity"] for f in out], ["INFO"])
        self.assertEqual(out[0]["title"],
                         "Schema types that no longer earn a Google rich result")
        self.assertIn("FAQPage", out[0]["observed"])

    def test_no_jsonld_means_no_findings(self):
        self.assertEqual(SA.check_schema_depth(make_page(html_doc("<p>x</p>")), True), [])

    def test_top_level_list_of_nodes_is_walked(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '[{"@type":"Organization","name":"Acme"},'
            '{"@type":"Article","headline":"H","datePublished":"2026-01-01",'
            '"dateModified":"2026-01-02","author":"Ann"}]')))
        out = SA.check_schema_depth(page, is_home=True)
        self.assertTrue(by_title(out, "Article schema is missing"))
        self.assertTrue(by_title(out, "Homepage Organization schema is thin"))

    def test_invalid_json_ld_does_not_raise(self):
        page = make_page(html_doc("<p>x</p>", head=ld("{not json at all")))
        self.assertEqual(SA.check_schema_depth(page, True), [])

    def test_type_as_a_list_of_strings(self):
        page = make_page(html_doc("<p>x</p>", head=ld(
            '{"@type":["LocalBusiness","Restaurant"],"name":"Acme",'
            '"aggregateRating":{"ratingValue":"4.0"}}')))
        self.assertTrue(by_title(SA.check_schema_depth(page, False),
                                 "Self-serving review markup"))

    # ---- BUG -----------------------------------------------------------------------
    def test_numeric_type_does_not_crash(self):
        """ANALYZER BUG: _types() raises TypeError on a non-string, non-iterable @type.

        scripts/seo_audit.py lines 2588-2590:
            def _types(node):
                t = node.get("@type")
                return set([t] if isinstance(t, str) else [str(x) for x in (t or [])])

        `"@type": 5` (or `true`) makes the comprehension iterate an int/bool and raise
        `TypeError: 'int' object is not iterable`. _types() is called unguarded from
        check_schema_depth / check_answer_readiness / check_entity_transparency inside
        run_audit, so ONE malformed JSON-LD block aborts the whole audit. Note that
        parse_jsonld_types() and _google_signals.types_of() in the same file both
        handle this. Patch _types the same way:
            t = node.get("@type")
            if isinstance(t, str):
                return {t}
            if isinstance(t, (list, tuple, set)):
                return {str(x) for x in t}
            return set()
        """
        page = make_page(html_doc("<p>x</p>", head=ld('{"@type":5,"name":"Acme"}')))
        self.assertEqual(SA.check_schema_depth(page, True), [])


class TestTypesHelper(unittest.TestCase):
    def test_string_list_and_missing(self):
        self.assertEqual(SA._types({"@type": "Article"}), {"Article"})
        self.assertEqual(SA._types({"@type": ["A", "B"]}), {"A", "B"})
        self.assertEqual(SA._types({}), set())
        self.assertEqual(SA._types({"@type": None}), set())

    def test_boolean_type_does_not_crash(self):
        # Same root cause as TestCheckSchemaDepth.test_numeric_type_does_not_crash.
        self.assertEqual(SA._types({"@type": True}), set())


# =====================================================================================
# check_repeated_passages
# =====================================================================================
class TestCheckRepeatedPassages(unittest.TestCase):
    SENTENCE = ("Harbor lantern meadow ridge copper willow granite cedar hollow amber "
                "quartz thicket ember pebble.")

    def _page(self, text):
        return make_page(html_doc("<p>%s</p>" % text))

    def test_repeated_sentence_is_reported(self):
        text = (FX.prose(90, seed=11) + " " + self.SENTENCE + " "
                + FX.prose(90, seed=12) + " " + self.SENTENCE + " "
                + FX.prose(90, seed=13))
        out = SA.check_repeated_passages(self._page(text))
        self.assertEqual(titles(out),
                         ["The same sentence appears more than once on the page"])
        self.assertEqual(out[0]["severity"], "LOW")
        self.assertEqual(out[0]["category"], "on page")
        self.assertIn("Harbor lantern", out[0]["observed"])

    def test_unique_prose_is_silent(self):
        self.assertEqual(SA.check_repeated_passages(self._page(FX.prose(300, seed=14))), [])

    def test_short_pages_are_skipped(self):
        text = self.SENTENCE + " " + self.SENTENCE
        self.assertEqual(SA.check_repeated_passages(self._page(text)), [])

    def test_short_repeats_are_not_counted_as_sentences(self):
        # under 12 words, so not a "passage"
        text = "Alpha bravo charlie. " * 3 + FX.prose(200, seed=15)
        self.assertEqual(SA.check_repeated_passages(self._page(text)), [])

    # ---- BUG -----------------------------------------------------------------------
    def test_repeated_passage_across_block_elements_is_detected(self):
        """ANALYZER BUG: PageParser.visible_text glues adjacent text nodes together.

        scripts/seo_audit.py:
            line 334  if self._skip_depth == 0 and data.strip():
                           self.text_parts.append(data)
            line 401  return " ".join("".join(self.text_parts).split())

        Whitespace-only text nodes are dropped and the surviving parts are joined with
        the EMPTY string, so `<p>...pebble.</p><p>Harbor...` becomes
        "...pebble.Harbor...". Consequences: word_count under-counts by one word per
        block boundary, `_sentences()` cannot split at a boundary (so the "template
        partial rendered twice" case this check exists for is missed whenever the two
        copies sit in different elements), and `_shingles()` invents cross-boundary
        tokens that skew check_template_duplication.

        Patch: join with a space instead of gluing —
            return " ".join(" ".join(self.text_parts).split())
        """
        html = html_doc("<p>%s</p><p>%s</p><p>%s</p><p>%s</p>"
                        % (FX.prose(90, seed=11), self.SENTENCE,
                           FX.prose(90, seed=12), self.SENTENCE))
        out = SA.check_repeated_passages(make_page(html))
        self.assertEqual(titles(out),
                         ["The same sentence appears more than once on the page"])


class TestVisibleText(unittest.TestCase):
    def test_words_inside_one_node_are_counted(self):
        p = SA.PageParser()
        p.feed("<p>one two three four</p>")
        self.assertEqual(p.word_count, 4)

    def test_script_and_style_text_is_excluded(self):
        p = SA.PageParser()
        p.feed("<p>one two</p><script>var hidden = 1;</script><style>a{b:c}</style>")
        self.assertEqual(p.visible_text, "one two")

    def test_words_across_block_elements_are_counted(self):
        # Same root cause as
        # TestCheckRepeatedPassages.test_repeated_passage_across_block_elements_is_detected
        p = SA.PageParser()
        p.feed("<p>one two.</p>\n<p>three four.</p>")
        self.assertEqual(p.word_count, 4)


# =====================================================================================
# check_template_duplication
# =====================================================================================
class TestCheckTemplateDuplication(unittest.TestCase):
    def _pages(self, texts, prefix="/things/"):
        out = []
        for i, t in enumerate(texts):
            pg = make_page(html_doc("<p>%s</p>" % t),
                           url="https://example.com%s%d" % (prefix, i))
            pg["status"] = 200
            out.append(pg)
        return out

    def test_near_duplicate_pair_is_reported(self):
        base = FX.words(400, seed=21)
        twin = list(base)
        twin[40], twin[300] = "dune", "quarry"
        pages = self._pages([FX.prose_from(base), FX.prose_from(twin),
                             FX.prose(400, seed=22)])
        findings, rows = SA.check_template_duplication(pages)
        self.assertEqual(titles(findings), ["Near-duplicate pages within a template"])
        self.assertEqual(findings[0]["severity"], "MEDIUM")
        self.assertEqual(findings[0]["category"], "on page")
        self.assertEqual(rows[0]["template"], "/things/*")
        self.assertEqual(rows[0]["pages"], 3)
        self.assertEqual(rows[0]["near_duplicate_pairs"], 1)

    def test_mostly_boilerplate_template_is_reported(self):
        boiler = FX.prose(150, seed=31)
        pages = self._pages([boiler + " " + FX.prose(55, seed=32 + i) for i in range(4)])
        findings, rows = SA.check_template_duplication(pages)
        self.assertEqual(titles(findings), ["Template pages are mostly boilerplate"])
        self.assertEqual(findings[0]["severity"], "LOW")
        self.assertEqual(rows[0]["near_duplicate_pairs"], 0)
        self.assertLess(rows[0]["median_unique_words"], 120)

    def test_unique_pages_are_silent(self):
        pages = self._pages([FX.prose(320, seed=40 + i) for i in range(4)])
        findings, rows = SA.check_template_duplication(pages)
        self.assertEqual(findings, [])
        self.assertGreaterEqual(rows[0]["median_unique_words"], 120)

    def test_fewer_than_three_pages_per_template_is_skipped(self):
        pages = self._pages([FX.prose(300, seed=51)] * 2)
        findings, rows = SA.check_template_duplication(pages)
        self.assertEqual(findings, [])
        self.assertEqual(rows, [])

    def test_thin_pages_are_not_compared(self):
        pages = self._pages([FX.prose(40, seed=61)] * 4)
        findings, rows = SA.check_template_duplication(pages)
        self.assertEqual(findings, [])
        self.assertEqual(rows, [])

    def test_non_200_pages_are_excluded(self):
        pages = self._pages([FX.prose(300, seed=71)] * 3)
        for pg in pages:
            pg["status"] = 404
        self.assertEqual(SA.check_template_duplication(pages), ([], []))


# =====================================================================================
# score_categories
# =====================================================================================
def finding(sev, title, cat):
    return SA.f(sev, title, cat, 3, 2, "observed", "fix")


class TestScoreCategories(unittest.TestCase):
    NO_PSI = {"status": "skipped"}

    def test_category_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(SA.CATEGORY_WEIGHTS.values()), 1.0, places=6)

    def test_perfect_site(self):
        scores, overall = SA.score_categories([], self.NO_PSI, {})
        self.assertEqual(set(scores), set(SA.CATEGORIES))
        self.assertTrue(all(v == 100 for v in scores.values()))
        self.assertEqual(overall, 100)

    def test_duplicate_category_title_pairs_are_counted_once(self):
        one = SA.score_categories([finding("HIGH", "Same problem", "on page")],
                                  self.NO_PSI, {})[0]["on page"]
        many = SA.score_categories([finding("HIGH", "Same problem", "on page")] * 7,
                                   self.NO_PSI, {})[0]["on page"]
        self.assertEqual(one, 80)
        self.assertEqual(many, 80)

    def test_the_same_title_in_another_category_still_counts(self):
        scores, _ = SA.score_categories(
            [finding("HIGH", "Same problem", "on page"),
             finding("HIGH", "Same problem", "trust")], self.NO_PSI, {})
        self.assertEqual(scores["on page"], 80)
        self.assertEqual(scores["trust"], 80)

    def test_diminishing_returns_weighting(self):
        # five distinct HIGHs (20 pts each): 20 + 20 + 15 + 15 + 10 = 80
        fs = [finding("HIGH", "Problem %d" % i, "on page") for i in range(5)]
        scores, _ = SA.score_categories(fs, self.NO_PSI, {})
        self.assertEqual(scores["on page"], 20)

    def test_heaviest_findings_take_the_full_weight_first(self):
        # CRITICAL 40 + HIGH 20 + MEDIUM 10*0.75 + LOW 4*0.75 = 70.5 -> 30 (rounded)
        fs = [finding("LOW", "d", "trust"), finding("CRITICAL", "a", "trust"),
              finding("MEDIUM", "c", "trust"), finding("HIGH", "b", "trust")]
        scores, _ = SA.score_categories(fs, self.NO_PSI, {})
        self.assertEqual(scores["trust"], round(100 - (40 + 20 + 7.5 + 3.0)))

    def test_info_findings_cost_nothing(self):
        fs = [finding("INFO", "note %d" % i, "ai search") for i in range(9)]
        self.assertEqual(SA.score_categories(fs, self.NO_PSI, {})[0]["ai search"], 100)

    def test_score_floors_at_zero(self):
        fs = [finding("CRITICAL", "c%d" % i, "schema") for i in range(20)]
        self.assertEqual(SA.score_categories(fs, self.NO_PSI, {})[0]["schema"], 0)

    def test_overall_is_the_weighted_average(self):
        fs = [finding("HIGH", "Problem %d" % i, "on page") for i in range(5)]
        scores, overall = SA.score_categories(fs, self.NO_PSI, {})
        expected = round(sum(scores[c] * SA.CATEGORY_WEIGHTS[c] for c in SA.CATEGORIES))
        self.assertEqual(overall, expected)
        self.assertEqual(overall, 86)

    def test_pagespeed_caps_the_performance_category(self):
        scores, _ = SA.score_categories([], {"status": "ok", "score": 41}, {})
        self.assertEqual(scores["performance"], 41)

    def test_pagespeed_never_raises_the_performance_category(self):
        fs = [finding("CRITICAL", "slow", "performance")]
        scores, _ = SA.score_categories(fs, {"status": "ok", "score": 99}, {})
        self.assertEqual(scores["performance"], 60)


# =====================================================================================
# fetch: non-http schemes
# =====================================================================================
class TestFetchSchemeGuard(unittest.TestCase):
    def test_data_uri_is_refused_without_a_request(self):
        r = SA.fetch("data:text/plain;base64,aGVsbG8=")
        self.assertEqual(r.status, 0)
        self.assertEqual(r.error, "not an http(s) URL")
        self.assertEqual(r.body, b"")
        self.assertEqual(r.chain, [])

    def test_other_non_http_schemes(self):
        for url in ("mailto:a@b.test", "javascript:alert(1)", "file:///etc/passwd",
                    "tel:+15550000000", "ftp://example.test/x", "about:blank", ""):
            with self.subTest(url=url):
                r = SA.fetch(url)
                self.assertEqual(r.status, 0)
                self.assertEqual(r.error, "not an http(s) URL")

    def test_scheme_match_is_case_insensitive(self):
        # HTTP:// is a real http URL; it must not be rejected by the guard.
        r = SA.fetch("HTTP://127.0.0.1:1/")
        self.assertNotEqual(r.error, "not an http(s) URL")


# =====================================================================================
# image_info / _manual_dims
# =====================================================================================
class TestImageInfo(unittest.TestCase):
    def _resp(self, body, ctype="image/png", status=200):
        return SA.Response("u", "u", status, {"Content-Type": ctype}, body, 5.0)

    def test_real_png_dimensions(self):
        info = SA.image_info(self._resp(FX.make_png(120, 60)))
        self.assertEqual((info["width"], info["height"]), (120, 60))
        self.assertEqual(info["mime"], "image/png")

    def test_error_responses_return_none(self):
        self.assertIsNone(SA.image_info(self._resp(b"x", status=404)))
        self.assertIsNone(SA.image_info(self._resp(b"")))
        self.assertIsNone(SA.image_info(None))

    def test_unrecognised_bytes_report_size_only(self):
        info = SA.image_info(self._resp(b"definitely not an image"))
        self.assertIsNone(info["width"])
        self.assertEqual(info["size"], 23)

    # ---- BUG -----------------------------------------------------------------------
    def test_truncated_png_does_not_crash(self):
        """ANALYZER BUG: _manual_dims() raises struct.error on a short image body.

        scripts/seo_audit.py lines 463-466 and 470-477:
            except Exception:
                dims = _manual_dims(resp.body)
        ...
            if b[:8] == b"\\x89PNG\\r\\n\\x1a\\n":
                w, h = struct.unpack(">II", b[16:24])

        _manual_dims is called from image_info's `except` handler, so a struct.error
        raised inside it is NOT caught: a body that carries a PNG signature but fewer
        than 24 bytes (or a GIF signature with fewer than 10) escapes as
        `struct.error: unpack requires a buffer of 8 bytes`. image_info is called
        unguarded from analyze_social (og:image AND favicon) and
        check_page_share_image, so one malformed image aborts the whole audit.

        Patch (line 463): guard the fallback —
            except Exception:
                try:
                    dims = _manual_dims(resp.body)
                except Exception:
                    dims = None
        (or bounds-check each branch of _manual_dims).
        """
        info = SA.image_info(self._resp(b"\x89PNG\r\n\x1a\n" + b"short"))
        self.assertIsNone(info["width"])

    def test_truncated_gif_does_not_crash(self):
        # Same root cause as test_truncated_png_does_not_crash: b[6:10] is too short.
        info = SA.image_info(self._resp(b"GIF89a", ctype="image/gif"))
        self.assertIsNone(info["width"])


# =====================================================================================
# Parser robustness: uppercase markup, odd attribute values, unicode
# =====================================================================================
class TestParserRobustness(unittest.TestCase):
    UPPER = ('<!DOCTYPE HTML><HTML LANG="en"><HEAD><META CHARSET="utf-8">'
             "<TITLE>An uppercase document title for the parser</TITLE>"
             '<LINK REL="CANONICAL" HREF="/canonical-target">'
             '<META NAME="ROBOTS" CONTENT="NOINDEX">'
             '<META NAME="VIEWPORT" CONTENT="width=device-width, initial-scale=1">'
             '<META PROPERTY="OG:IMAGE" CONTENT="social.png">'
             "</HEAD><BODY><H1>Heading</H1>"
             '<IMG SRC="/a.png" LOADING="LAZY" WIDTH="100%" HEIGHT="AUTO" '
             'FETCHPRIORITY="HIGH" ALT="An image"></BODY></HTML>')

    def test_uppercase_tags_and_attributes(self):
        p = SA.PageParser()
        p.feed(self.UPPER)
        self.assertEqual(p.lang, "en")
        self.assertEqual(p.canonical, "/canonical-target")
        self.assertIn("noindex", p.meta_robots)
        self.assertTrue(p.has_viewport_tag)
        self.assertEqual(p.og("og:image"), "social.png")
        self.assertEqual(p.imgs[0]["loading"], "lazy")
        self.assertEqual(p.imgs[0]["fetchpriority"], "high")

    def test_non_numeric_width_does_not_crash_the_image_checks(self):
        # width="100%" / height="AUTO" must not reach int(). The hero fetch is pointed
        # at a closed local port so this test never touches the network.
        page = make_page(self.UPPER, url="http://127.0.0.1:9/")
        page["status"] = 200
        self.assertEqual(SA.check_hero_and_thumbs(page), [])

    def test_canonical_link_without_href(self):
        p = SA.PageParser()
        p.feed('<link rel="canonical">')
        self.assertEqual(p.canonical, "")

    def test_relative_canonical_is_resolved_against_the_final_url(self):
        self.assertEqual(SA.normalize("https://example.com/a/b", "../c"),
                         "https://example.com/c")

    def test_unicode_survives_parsing(self):
        p = SA.PageParser()
        p.feed('<html lang="fr"><head><title>Café 中文 \U0001F600</title>'
               "</head><body><p>élève 中文 \U0001F600 words</p></body></html>")
        self.assertIn("中文", p.title)
        self.assertIn("\U0001F600", p.visible_text)


class TestParseJsonldTypes(unittest.TestCase):
    def test_top_level_list(self):
        types, valid, invalid = SA.parse_jsonld_types(
            ['[{"@type":"Article"},{"@type":["WebPage","Thing"]}]'])
        self.assertEqual(types, {"Article", "WebPage", "Thing"})
        self.assertEqual((valid, invalid), (1, 0))

    def test_bare_string_document(self):
        self.assertEqual(SA.parse_jsonld_types(['"hello"']), (set(), 1, 0))

    def test_invalid_json_is_counted(self):
        self.assertEqual(SA.parse_jsonld_types(["{oops"]), (set(), 0, 1))

    def test_blank_blocks_are_ignored(self):
        self.assertEqual(SA.parse_jsonld_types(["", "   "]), (set(), 0, 0))

    def test_numeric_type_is_tolerated_here(self):
        # parse_jsonld_types is defensive; _types() (see above) is not.
        self.assertEqual(SA.parse_jsonld_types(['{"@type":5}']), (set(), 1, 0))


# =====================================================================================
# same_site / _trivial_redirect
# =====================================================================================
class TestSameSite(unittest.TestCase):
    def test_www_is_ignored(self):
        self.assertTrue(SA.same_site("https://www.e.com/a", "https://e.com/b"))

    def test_different_hosts(self):
        self.assertFalse(SA.same_site("http://localhost:8080/a", "http://127.0.0.1:8080/a"))
        self.assertFalse(SA.same_site("https://cdn.e.com/a", "https://e.com/a"))

    def test_ports_are_ignored(self):
        # Deliberate for SEO purposes (and what the fixtures rely on), but note it is
        # not the same thing as a same-origin comparison.
        self.assertTrue(SA.same_site("http://e.com:1/a", "http://e.com:2/a"))

    def test_trivial_redirect(self):
        self.assertTrue(SA._trivial_redirect("http://e.com/a", "https://e.com/a/"))
        self.assertTrue(SA._trivial_redirect("https://www.e.com/a", "https://e.com/a"))
        self.assertFalse(SA._trivial_redirect("https://e.com/a", "https://e.com/b"))
        self.assertFalse(SA._trivial_redirect("https://e.com/a?x=1", "https://e.com/a"))


if __name__ == "__main__":
    unittest.main()
