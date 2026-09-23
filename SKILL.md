---
name: seo-audit
description: >-
  Audit a website's SEO health and produce a scored report (HTML + PDF) with an
  overall score out of 100, eight category scores (crawlability, on-page,
  performance, AI-search, schema, trust, mobile, social), template-stratified
  coverage, an AI-crawler access matrix tested at the network edge, link-architecture
  and canonical checks, a social-share preview analysis, and per-page findings tables,
  each finding tagged with severity, category, impact/effort, what was observed, and
  a concrete fix. Also use it for pre-launch / pre-indexing readiness reviews. Use this
  whenever the user wants to analyze, audit, score, or improve a site's SEO; check
  search-engine readiness, crawlability, structured data, meta tags, Open Graph /
  social previews, or AI-crawler visibility; or asks for an "SEO report" / "SEO
  audit" / "site health check" for a given URL or domain. Trigger even if they just
  paste a URL and ask "how's the SEO on this?" or "what's wrong with this site for
  search?". Also use it for competitive analysis: "who is outranking us", "compare us
  with <rival>", "why are we losing position to <site>", "what does <rival> do better",
  or "find our top competitors and compare" (competitor discovery, side-by-side audits,
  the seven-lens comparison in references/competitive-analysis.md).
---

# SEO Audit

Crawl a website, score it, and write a report a non-expert can act on: one headline
score, eight category scores, a **Top priorities** list, a **Coverage by URL template**
table (what was and wasn't looked at), **Site health checks**, an **AI crawler access**
matrix tested at the network edge, an informational **Answer-engine readiness** table,
Core Web Vitals when PageSpeed runs, **Keyword focus**, a social-share preview, a
**Social & local footprint** section, a **Sitemap sweep** of issues by frequency, and
per-page findings tables. Every finding is paired with a concrete fix.

The score is a description of what the script could observe, not a verdict on the site.
Two rules follow from that, and they matter more than anything else in this file:

1. **Read the coverage table before you quote the score.** The crawl samples across URL
   templates so a 250-page directory can't hide behind a dozen static pages, but if a
   template shows 0 audited, the score says nothing about it. Raise `--max-pages` and
   `--sweep` and re-run.
2. **The script cannot see the things that most often sink a launch**: a checkout that
   breaks when a third-party API is down, analytics that record no revenue, a CDN toggle,
   copy that contradicts the access policy, a geo-personalized hub. When the user is about
   to launch, open indexing, or migrate, also work through
   `references/manual-checklist.md` and say which items you did and could not do.

## Workflow

1. **Get the target URL.** A bare domain is fine (`example.com` → `https://example.com`).

2. **Check you are running the current version.** `python3 scripts/seo_audit.py --version`.
   If this skill was installed by copying files, compare with the repository
   (`gh api repos/btobbes/seo-analyzer/commits/main --jq .sha`); installed copies go stale
   silently. The version is printed in the report footer.

3. **Run the analyzer.** From the skill directory:

   ```bash
   python3 scripts/seo_audit.py <URL> --out <output-dir> --json
   ```

   Flags:
   - `--max-pages N` — pages that get a full per-page table (default 15). Chosen by
     **stratified sampling across URL templates**, evenly spread inside each template,
     homepage always first.
   - `--sweep N` — additional sitemap URLs analyzed the same way but reported in aggregate
     (default 100; `0` disables). This is what finds a problem that lives on the biggest
     template. For a pre-launch audit of a site under ~1,000 URLs, sweep most of it.
   - `--pagespeed` — query PageSpeed Insights for lab metrics and CrUX field data. Often
     rate-limited without a key; set `PAGESPEED_API_KEY`. Without it Core Web Vitals are
     **not measured**; the static performance checks find causes, not numbers.
   - `--no-ai-probe` — skip requesting the homepage with AI-crawler user-agents.
   - `--offline-dns` — skip third-party lookups (RDAP domain expiry, DNS-over-HTTPS).
   - `--no-pdf`, `--json`, `--out DIR`.

   It writes `seo-report-<domain>-<date>.html`, `.pdf`, and optionally `.json`.

4. **Verify before you report.** Re-check the top findings by hand with one request each.
   Anything reported broken gets a real GET (some servers 404 a HEAD on routes that serve
   GET fine). If two findings conflict, resolve it first. If the site is being changed
   while you audit, re-run immediately before reporting; findings go stale in hours.

5. **Report back.** Lead with what blocks launch, not with the score. Give the overall
   score with its coverage ("81/100 from 115 of 480 URLs, all 8 templates sampled"), then
   the CRITICAL and HIGH items, then the highest-leverage fixes (high impact, low effort).
   Separate what you verified from what the tool reported, and state what was not checked
   (Core Web Vitals without PageSpeed, anything behind a login, the money path).
   Interpret; don't dump the table.

6. **Competitive analysis (when the question is "who is beating us and why").** The full
   method is `references/competitive-analysis.md`; what can be fetched and what is worth
   buying is `references/competitor-sources.md`. The short form:

   1. Ask what "losing position" means (organic rank, map pack, a marketplace's category
      order, bookings) and request the owner's Search Console, Business Profile and
      booking exports; they answer the question directly. Say up front what the
      environment cannot observe (usually Google itself).
   2. Write about 20 queries in five classes (head terms, the rivals' product classes,
      informational, venue/landmark, brands) into a file.
   3. Find the competitive set:

      ```bash
      python3 scripts/find_competitors.py --client example.com --owned other-example.com \
        --queries-file queries.txt --out <output-dir>
      ```

      It tallies the domains occupying the results (Brave live, or `--serp-json` exports
      from SerpApi or DataForSEO when someone has bought real Google data), classifies
      them (business, marketplace, social/video, directory/news, own) and shortlists the
      rivals to audit. Add the marketplace category leaders and anyone the owner named.
      Three to five rivals; classify each as local-direct, national franchise or aggregator.
   4. Audit every rival with the same flags as the client, then compare:

      ```bash
      python3 scripts/seo_compare.py client.json rival1.json rival2.json --out <output-dir> --json --html
      ```

      Ignore the overall scores when page counts differ by more than about 3x or a site
      has no sitemap; the comparison says when that applies. Read the absent-features
      matrix and the findings unique to each side.
   5. Run the seven lenses (on-page and offer, distribution and reviews, links and
      partners, search visibility by query class, the client's own architecture,
      conversion path, data sources), verify every strategy-changing claim with a fresh
      request, and write the report in the seven-part order the reference gives: verdict,
      what the rival does well and the counter-move that does it better, self-inflicted
      problems, conversion fixes, where the client already wins, the other rivals, and
      data plus a dated 90-day plan.

## How it works (so you can explain or adjust it)

- **Template-stratified sampling.** Sitemap URLs are clustered by path shape
  (`/blog/*`, `/{year}/*/*/*/`, …). Every template gets a slot before any template gets a
  second one, and picks are spread evenly through each cluster. The old behavior (first N
  sitemap entries) audited the same corner of a site on every run.
- **AI crawler access is tested, not inferred.** robots.txt states intent; CDNs and
  firewalls decide. The homepage is requested with each crawler's documented user-agent
  (17 training, answer-engine index, and live user-fetch agents from OpenAI, Anthropic,
  Perplexity, Apple, Meta, Amazon, Common Crawl, DuckDuckGo, Mistral, ByteDance), on the
  homepage and one inner page, and compared with a browser. Refused search/user agents are
  HIGH; refused training agents are MEDIUM, with a note when robots.txt names them (the
  file and the edge disagree). The probe runs from an ordinary IP, so a firewall that verifies crawler IPs
  can refuse the test yet admit the real bot: every finding says so, and a site that
  refuses *every* non-browser UA gets a softer, separate finding. robots.txt is also
  scanned for a CDN-managed block (the file crawlers get is then not the origin's),
  Cloudflare `Content-Signal` lines and IETF `Content-Usage` lines (`search`, `ai-input` /
  `ai-use`), an HTML body, and the 500 KiB parse limit.
- **Link architecture.** Up to 40 internal link targets are sampled across templates and
  classified: broken, 5xx, redirecting, URL-variant 301s, canonicalized elsewhere,
  noindexed. From that: *internal links point at non-canonical URLs* (HIGH when none of
  the canonical targets is linked from any crawled page); *canonical points at a thinner
  page than the one carrying it* (both pages fetched in full, words and schema types
  compared); *links lead to noindexed pages*; and *a sitemap template gets no internal
  links*. A `?query` variant canonicalizing to its clean URL is not counted, and a page's
  links to itself are not inlinks.
- **Sitemap hygiene** on every fetched sitemap URL: errors (404/410/5xx only), redirects,
  noindex, non-canonical entries, `<lastmod>` coverage and plausibility, image extension.
  Sitemap indexes are read through one capped pass (20 files spread across the index,
  50,000 URLs) so large publishers don't hang the run.
- **Bot-protection fallback.** If the honest auditor UA gets 401/403/429, the page is
  retried once as a browser and the report says so, rather than calling the page dead.
- **Static performance beyond the document.** The likely LCP image is fetched: cross-origin
  hotlinks, redirect chains, bytes, `loading=lazy` on a priority image. Small displayed
  images are sampled for oversized files. Google Fonts payload is measured (Latin woff2
  files), plus preconnect and `display=swap`. A CSP that blocks a script the page itself
  ships (typically a CDN-injected RUM beacon), third-party scripts ahead of the stylesheet,
  and `Cache-Control: no-store` HTML are flagged. Image checks run once per template.
- **HEAD/GET parity.** The homepage, share image, sitemap, and one image per top-level
  directory are requested with HEAD; a 4xx/5xx where GET gives 200 is flagged LOW. It
  breaks link checkers and monitors (and fooled a previous audit into reporting 220 broken
  images), not search crawlers. The analyzer never trusts HEAD for status or bytes.
- **Trust and launch hygiene.** Domain expiry via RDAP, mail-capable contact domains and
  SPF/DMARC via DNS-over-HTTPS, `security.txt` (RFC 9116 fields, expiry), exposed
  `.git/HEAD`/`.env`/`.DS_Store`, HSTS quality, and entity transparency (a postal address
  and legal entity name, looked for on the about/contact/legal pages specifically).
- **Structured data depth.** Homepage Organization completeness, Article recommended
  properties (`image`, dates, `author.url`), self-serving `aggregateRating` on the site's
  own LocalBusiness/Organization, and an INFO note for types whose Google rich result was
  retired or restricted.
- **Answer-engine readiness.** Snippet restrictions (`nosnippet`, `max-snippet:0`) are
  scored because Google documents that they also govern AI Overviews and AI Mode.
  Everything else (question headings, lists, tables, visible dates) is shown in an
  informational table and not scored: end-to-end studies find most on-page "GEO" tactics
  ineffective or negative, and off-page signals dominate. A missing `llms.txt` is never a
  finding (server logs show AI crawlers don't request it; Google says it doesn't use it);
  an existing one is validated and its links checked, INFO only. Schema is scored for
  rich-result correctness and entity clarity, never as "AI visibility".
- **Duplicate and boilerplate content.** Within each template, 6-word shingles give the
  median unique words per page and any near-duplicate pairs (≥80% shared). A sentence
  repeated on one page (a partial rendered twice) is flagged.
- **Share images on every template**, fetched with GET: missing, broken, or small/square.
- **Consistency and estate checks (2.1.0).** Self-declared review counts that disagree
  across pages (LOW, trust); a large share of analysed pages carrying affiliate tracking,
  which is what Google's thin-affiliation and scaled-content policies describe (LOW at
  30%, MEDIUM at 50%, on page); other domains the same business runs, found through a
  shared analytics property or a `sameAs` entry whose target names the same organization
  in its own schema, fetched once each, reported when they are separate live sites rather
  than redirects (LOW/MEDIUM, crawlability; skipped with `--offline-dns`; listed in the
  JSON as `owned_domains`); and title concentration,
  how many crawled titles share one two-word phrase after the brand segment is stripped,
  shown in the keyword section and never scored. Each came out of a competitive audit
  where the client's losses were partly self-inflicted.
- **False positives fixed in 2.1.0.** `<script type="module">` is deferred by
  specification and no longer counts as render-blocking; AVIF/WebP delivered through
  `<picture><source>`, `srcset` or an image preload no longer triggers "no modern
  formats"; the hero check measures the `<picture>` source a browser fetches instead of
  the JPEG fallback and says so; a Google Fonts stylesheet linked twice is reported once
  as a duplicate link and its files are counted once; LocalBusiness subtypes without the
  word "Business" in their name (TravelAgency, Dentist, Winery...) are recognised; and
  single-post permalinks (one TripAdvisor review, one Facebook video) no longer fill the
  footprint table.
- **Site-wide probes** (unchanged): all four host variants, real-404 handling, TLS expiry,
  asset caching, analytics stack, redirect chains with the full hop list.
- **Client-render suppression**, **RFC 9309 robots evaluation**, **PageSpeed integration**,
  **Keyword focus**, and **Social & local footprint** work as before; see
  `references/scoring.md`.
- **Standard library only.** Pillow is used for image dimensions if present. PDF rendering
  uses headless Chrome/Chromium/Edge, falling back to WeasyPrint; if neither exists the
  HTML report is still written.

### Scoring

Each category starts at 100 and loses points per distinct finding by severity (CRITICAL
−40, HIGH −20, MEDIUM −10, LOW −4, INFO 0), with diminishing returns inside a category
(two heaviest in full, next two at 75%, the rest at 50%) and a floor of 0. The overall
score is a weighted average: crawlability 0.20, on page 0.18, performance 0.15, AI search
0.10, schema 0.10, trust 0.10, mobile 0.09, social 0.08. AI search is deliberately
weighted below crawlability and performance (see `references/sources-2026.md`). Findings from swept pages count
once per issue type, like everything else. Details and the full check list are in
`references/scoring.md`; sources for the 2026 checks are in `references/sources-2026.md`.

## Tests

`python3 -m unittest discover -s tests -v` runs unit tests and an integration suite
against a local fixture site that reproduces each defect (and a clean twin that guards
against false positives), plus `tests/test_checks_21.py` (the 2.1.0 checks and fixed
false positives, each with a firing and a silent case), `tests/test_compare.py` and
`tests/test_find_competitors.py` (the comparison and discovery scripts on synthetic
inputs). Run it after changing any check. No network access is needed.

## Notes

- This audits what is observable from the public site. It does not log in, submit forms,
  execute JavaScript, or read Search Console. Be explicit about that scope.
- The network load is modest but real: about 200 to 300 requests for a default run,
  eight at a time. Lower `--sweep` for fragile sites.
- Re-running after fixes and diffing the `--json` outputs is a good way to show progress.
- If the site blocks the crawler entirely, say so plainly rather than presenting an empty
  report as if the site were healthy.
- Never compare overall scores across sites. An 11-page site with no robots.txt, no
  sitemap and no schema scored 81 against a 1,062-URL site's 84 (2026-09-22): the small
  site scored well by having nothing to check. Compare category by category, page counts
  beside them, and read what each site lacks (`scripts/seo_compare.py` does this).
- A rival's marketing claim ("award-winning, as seen on...") is a claim until the
  outlet's own page confirms it. Search for it; report what was and was not found.
