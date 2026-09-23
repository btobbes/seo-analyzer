# Competitor sources, checked 2026-09-22

Reference for a competitive analysis run alongside the seo-audit skill. The audit script
scores what one site exposes over HTTP. A competitive question ("why does the rival
outrank us?") mostly turns on data the script can't see: marketplace listings, reviews,
the local pack and the client's own consoles. This file records where that data lives,
what an agent could actually fetch, what's worth paying for, and how to read a rival's
audit without fooling yourself.

Prices and reachability go stale. Every price and every reachability note below carries
the date it was checked, 2026-09-22. Re-check anything you quote from here. Where
something was not checked, the file says so; don't fill the gap from memory.

---

## 1. Where competitor data comes from, and what an agent can actually fetch

### Source types

| Source type | Question it answers | Reliability |
|---|---|---|
| Own audit JSON (`scripts/seo_audit.py --json`) | What the client's site exposes to a crawler, scored by category, with each finding and its fix | High for what the crawl observed, silent on templates it did not sample. Read the coverage table before quoting a score |
| The rival's site (the same script, run against the rival) | The same questions for the rival, so the two can be compared category by category | As reliable as the client's audit, but overall scores are not comparable when page counts differ widely or a sitemap is missing (section 3) |
| Marketplace listings (TripAdvisor, Peek, Viator, GetYourGuide, Groupon, Expedia) | Price, rating, ratings count, description, and which other activities the rival lists | First-hand where the page was readable on 2026-09-22 (TripAdvisor, peek.com). Second-hand for Viator, GetYourGuide and Groupon, which returned 403 that day, so label the route every such figure came by. Expedia returned 429 after a few requests |
| Review platforms (TripAdvisor, Yelp, Google reviews) | Review totals, dated reviews for velocity, star breakdown, complaint themes | On 2026-09-22 TripAdvisor review dates and counts were readable directly, Yelp returned 403, and Google review figures were visible only through the Wanderlog proxy. Compare one platform and one time window for both sides |
| DMO and directory listings (flagstaffarizona.org, downtownflagstaff.org, flagstaff365.com) | Whether the destination and local associations list the rival, and how it's described | Readable on 2026-09-22. How these listings are compiled or vetted was not checked |
| Local press (jackcentral.org, azfamily.com) | Whether a rival's "as seen on" and award claims hold up | An outlet's own page is confirmation; the rival's claim about it is not (section 3). Both outlets were readable on 2026-09-22 |
| Social and video (YouTube, TikTok, Reddit, Instagram) | Whether the rival is active on video and social platforms | Weak from an agent. YouTube, TikTok and Reddit returned nothing usable on 2026-09-22. Metricool Starter benchmarks rivals' Instagram (section 2). Direct fetches of Instagram and Facebook pages were not checked |
| Search engines and rank trackers | Who ranks for the head terms in Google organic results, the local pack and Maps, from the client's city | Google and Bing were unobservable on 2026-09-22. Brave and the WebSearch tool are proxies. The paid trackers in section 2 are the route to real positions; their output was not checked in this run |
| The client's own first-party data (Search Console, Business Profile Performance, OTA supplier dashboards) | Whether the client's position fell, on which queries and since when; Maps versus Search reach; OTA competitive-set visibility | The only first-party numbers in the analysis. They need the client's logins and describe the client; only the OTA supplier dashboards show a competitive set. None of them reads a rival's Google profile |

### Reachability, observed 2026-09-22

Observed from a Claude Code session on macOS. WebFetch is the agent's page fetch tool,
curl is the command line with a Chrome user agent, and the in-app browser is the desktop
app's browser pane.

| Target | Result | Consequence or workaround | Checked on |
|---|---|---|---|
| tripadvisor.com business listings (`Attraction_Review`), product listings (`AttractionProductReview`) and category lists (`Attractions-g…-Activities-c42-t226…`) | WebFetch works. Review dates and counts are readable, and "or10" style pagination is reachable | The primary source for review counts and velocity, and the mirror for Viator products | 2026-09-22 |
| peek.com marketplace activity pages | WebFetch works: rating, ratings count, price, description, sibling activities | Use as the Peek source | 2026-09-22 |
| book.peek.com booking pages | JavaScript app; nothing readable | Use the peek.com activity page instead | 2026-09-22 |
| flagstaffarizona.org (a DMO on the Simpleview platform), downtownflagstaff.org, flagstaff365.com, jackcentral.org, azfamily.com | WebFetch works | Use directly | 2026-09-22 |
| viator.com, getyourguide.com, groupon.com, yelp.com | HTTP 403 to WebFetch and to curl | Take figures from TripAdvisor mirrors (Viator products appear on TripAdvisor), search-result snippets or affiliate mirror sites, and label each figure with the route it came by | 2026-09-22 |
| expedia.com | HTTP 429 after a few requests | Not observable beyond a few requests | 2026-09-22 |
| weatherfordhotel.com (a WordPress hotel site) | 403 to WebFetch, 200 to curl with a Chrome UA | A WebFetch 403 is not the last word; retry with curl before calling a site blocked | 2026-09-22 |
| youtube.com watch pages, tiktok.com video pages | Empty shell to WebFetch | Not observable | 2026-09-22 |
| old.reddit.com JSON endpoints; reddit.com | Empty body from the JSON endpoints; 302 or 403 from reddit.com | Not observable | 2026-09-22 |
| web.archive.org | Blocked outright by the fetch tool; no historical snapshots | No history of a rival's old site from this setup | 2026-09-22 |
| google.com (search and Maps), bing.com | Blocked in the in-app browser by site permissions. curl with a text-browser user agent returns a 2 KB stub with no results | Not observable; see the note below this table | 2026-09-22 |
| html.duckduckgo.com | Serves a bot challenge page | Not observable | 2026-09-22 |
| search.brave.com | curl with a Chrome UA returns parseable organic results. Roughly a dozen rapid queries trigger HTTP 429; 4 second delays help. Results include entity cards and video, news and discussion clusters | Useful for seeing SERP features. Label the results as Brave's | 2026-09-22 |
| The WebSearch tool | Works for discovery; results are not Google-ordered | Treat as a proxy | 2026-09-22 |
| Wanderlog pages | Cache Google Places data (rating, review count, listing name, address). The only observable proxy for Google Business Profile figures | Label any such number as a proxy | 2026-09-22 |
| Google Trends explore endpoint | HTTP 429 | Not observable | 2026-09-22 |
| Arizona Office of Tourism research PDFs | HTTP 403 | Not observable | 2026-09-22 |
| operatorresources.viator.com, supplier.viator.com | HTTP 403 | Not observable | 2026-09-22 |
| Metricool (MCP connector) | Exposes only brands connected to the user's own account | Cannot read a rival's Google profile | 2026-09-22 |

Brave's was the only search results page that came back parseable. The command that
worked on 2026-09-22, with a 4 second pause between queries:

```bash
curl -s -A "<Chrome UA>" "https://search.brave.com/search?q=<urlencoded>&source=web"
```

So Google organic results, the local pack and Maps were unobservable on 2026-09-22. That
is the single biggest limitation of an agent-run competitive analysis, and the reason the
rank-tracker purchases in section 2 exist. Any statement about who ranks where on Google,
made without one of those tools or the client's Search Console, is a proxy and has to name
the proxy.

---

## 2. Third-party data worth buying for a competitive analysis (prices checked 2026-09-22)

Pull the free first-party sources before buying anything. Prices are as the cited page
showed them on 2026-09-22.

| Source | What it answers | Price as verified | Source URL | Priority |
|---|---|---|---|---|
| Google Search Console | Whether position fell, on which queries, since when (16 months of history). The Pages report shows URL alternation and index counts | $0 (checked 2026-09-22) | https://support.google.com/webmasters/answer/7576553 | Pull first |
| Google Business Profile Performance | Maps views versus Search views, search terms, calls, website clicks, monthly | $0 (checked 2026-09-22) | https://support.google.com/business/answer/7689763 | Pull first |
| Viator Accelerate insights and GetYourGuide performance dashboards (supplier logins) | Competitive-set visibility on the OTAs | $0 for insights; Accelerate placement costs extra commission, with no list price (checked 2026-09-22) | https://arival.travel/article/viator-accelerate-2-0-what-it-is-and-why-it-matters/ (Arival, Feb 2024) | Pull first |
| Google Ads Transparency Center | Whether rivals run search ads on the head terms | $0 (checked 2026-09-22) | https://adstransparency.google.com/ | Pull first (the tool was not tested in this run) |
| Whitespark Local Rank Tracker | Local pack, Maps and organic rank reported separately, tracked from zip codes or coordinates, with competitors tracked | $17/mo weekly or $25/mo daily on monthly billing ($14 and $20 annual); 14-day trial without a card (checked 2026-09-22) | https://whitespark.ca/local-rank-tracker/ | High |
| Local Falcon Starter | Geo-grid Maps rank, with competitor reports on every plan | $24.99/mo for 7,500 credits, one credit per grid point; 100 free credits on signup (checked 2026-09-22) | https://www.localfalcon.com/pricing | High |
| Apify TripAdvisor Reviews Scraper (`maxcopell/tripadvisor-reviews`) | Monthly review counts, star breakdown and complaint themes per operator | From $0.90 per 1,000 reviews. Free plan $0 with $5 of monthly usage; Starter $19/mo (checked 2026-09-22) | https://apify.com/maxcopell/tripadvisor-reviews | Medium |
| DataForSEO SERP API | Raw Google organic and Maps snapshots by location, kept as an auditable archive | $50 minimum deposit; $0.0006 per standard Google organic SERP, $0.0012 priority, $0.002 live (checked 2026-09-22) | https://dataforseo.com/apis/serp-api | Medium |
| SerpApi | Google local pack, Maps and Maps-reviews snapshots without a deposit | Free 250 searches/mo; Starter $25/mo for 1,000; Developer $75/mo for 5,000 (checked 2026-09-22) | https://serpapi.com/pricing | Alternative to DataForSEO |
| Semrush SEO plan, one month | Keyword volumes for small local query sets (free tools return zero or ranges), a backlink gap with first-seen dates, and a site crawl to confirm a noindex took | $139/mo on monthly billing ($117.33/mo annual); 500 keywords, 5 websites (checked 2026-09-22) | https://www.semrush.com/pricing/ | One month |
| Ahrefs | Alternative to Semrush for the same job | Lite $129/mo (750 keywords, 6 months history); Starter $29/mo with unlisted limits (checked 2026-09-22) | https://ahrefs.com/pricing | Alternative to Semrush |
| BrightLocal Track | Local rank for 100 keywords and four competitors per location, geo-grid for five keywords, citation monitoring | Not rendered on the pricing page ("Price on request"); the page metadata says from $39/mo (checked 2026-09-22) | https://www.brightlocal.com/pricing/ | Optional |
| Metricool Starter | Archives Google Business Profile search terms and Search versus Maps reach beyond Google's window; benchmarks rivals' Instagram | $20/mo annual or $25/mo monthly for 5 brands, 100 competitor profiles (checked 2026-09-22) | https://metricool.com/pricing/ | Optional |

The Viator row's source is Arival's February 2024 article, not a Viator page, and no
GetYourGuide source is listed here. TripAdvisor sells no analytics tool to tour operators
(checked 2026-09-22).

Semrush and Ahrefs cover the same need. Buy one, not both.

DataForSEO needs someone to run the API calls. Its JSON is one of the two formats
`scripts/find_competitors.py` reads; SerpApi's JSON is the other.

### Not worth buying for this purpose (checked 2026-09-22)

| Product | Why not |
|---|---|
| Similarweb | Its free page showed "No Data to Display" for an 11-page rival and only rank and engagement ratios for the client. Third-party pages quote about $125/mo on annual billing; Similarweb publishes no list price (checked 2026-09-22) |
| Placer.ai | Cannot isolate a walking tour that meets inside other businesses |
| Zartico | Sold to destinations; quote only |
| Birdeye and Podium | Reputation tools; quote only |
| Datafiniti | Names no review source |
| Places Scout | Redirects to Yext after its February 2025 acquisition |

### The minimal stack

About $239 in month one at 2026-09-22 prices: Whitespark $25, Local Falcon $25, a $50
DataForSEO deposit and one month of Semrush at $139. About $50 a month after that, at the
same prices. It buys Google organic and local-pack positions for the client and every
shortlisted rival, from the client's city, weekly.

---

## 3. Reading a rival's audit honestly

Overall scores are not comparable across sites when page counts differ by more than about
3x, or when a site has no sitemap. On 2026-09-22 an 11-page static site with no schema, no
robots.txt and no sitemap scored 81 against a 1,062-URL site's 84. The small site scored
well by having nothing to check. Compare category by category instead, and read the
absent-features list from `scripts/seo_compare.py`.

A rival's marketing claims ("award-winning", "as seen on X") are claims until the outlet's
own page confirms them. A TV episode about a venue is not coverage of the operator who
later tours it.

For a young rival, review velocity matters more than review totals. Count dated reviews in
a fixed window on the same platform for both sides. On 2026-09-22 TripAdvisor was the
platform where an agent could read review dates directly (section 1).

A rival in the middle of a migration (a new site, old URLs returning 404 while still
indexed, no sitemap) is weak for now, not for good. Note the date you saw it and re-audit
in 60 days before building a plan on the weakness.

Before blaming the rival, look for the client's own satellite domains, legacy domains and
affiliate catalogs. On 2026-09-22 the client ran eight live tour microsites, a 2021 site
quoting old prices, and 641 affiliate pages (60% of its sitemap). Any one of those can move
rankings on its own.
