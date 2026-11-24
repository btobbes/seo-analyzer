---
name: seo-audit
description: >-
  Audit a website's SEO health and produce a scored report (HTML + PDF) with an
  overall score out of 100, eight category scores (crawlability, on-page,
  performance, AI-search, schema, trust, mobile, social), template-stratified
  coverage, an AI-crawler access matrix tested at the network edge, link-architecture
  and canonical checks, a social-share preview analysis, and per-page findings tables,
  each finding tagged with severity, category, impact/effort, what was observed, and
  a concrete fix. When Google Search Console or GA4 access exists (a service-account
  key), it also reads the index state of every sitemap URL, search performance by URL
  template, queries within reach of page one, and ranks the crawl's findings by search
  demand; use it whenever the user mentions Search Console, GSC, indexing, impressions,
  clicks or rankings data. Also use it for pre-launch / pre-indexing readiness reviews. Use this
  whenever the user wants to analyze, audit, score, or improve a site's SEO; check
  search-engine readiness, crawlability, structured data, meta tags, Open Graph /
  social previews, or AI-crawler visibility; or asks for an "SEO report" / "SEO
  audit" / "site health check" for a given URL or domain. Trigger even if they just
  paste a URL and ask "how's the SEO on this?" or "what's wrong with this site for
  search?". Also use it for competitive analysis: "who is outranking us", "compare us
  with <rival>", "why are we losing position to <site>", "what does <rival> do better",
  or "find our top competitors and compare" (competitor discovery, side-by-side audits,
  the seven-lens comparison in references/competitive-analysis.md). Also use it to review
  or set up Google Analytics 4 and Google Search Console for a site: checking a
  service-account key, listing what it can see, reading indexing, search performance,
  traffic sources and GA4 settings read-only, and saying what to change so the properties
  capture how visitors arrive and what they do (references/google-console-review.md).
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
Three rules follow from that, and they matter more than anything else in this file:

1. **Read the coverage table before you quote the score.** The crawl samples across URL
   templates so a 250-page directory can't hide behind a dozen static pages, but if a
   template shows 0 audited, the score says nothing about it. Raise `--max-pages` and
   `--sweep` and re-run.
2. **The script cannot see the things that most often sink a launch**: a checkout that
   breaks when a third-party API is down, analytics that record no revenue, a CDN toggle,
   copy that contradicts the access policy, a geo-personalized hub. When the user is about
   to launch, open indexing, or migrate, also work through
   `references/manual-checklist.md` and say which items you did and could not do.
3. **When Google Analytics or Search Console access exists, their data outranks the
   score.** A site Google has indexed one page of, or that has been shown 22 times in 16
   months, has a search problem no crawl finding explains, whatever the score says. Run
   the console review (step 5) in the same pass as the crawl, decide which stage the site
   is at (measured, indexed, shown, clicked, converting), and lead the report with that.

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
     When PSI answers 429 (the keyless quota is shared across every keyless user and is
     often exhausted), run Lighthouse locally instead:
     `npx -y lighthouse@12 <URL> --only-categories=performance,accessibility,seo,best-practices --form-factor=mobile --screenEmulation.mobile --throttling-method=simulate --output=json --output-path=lh.json --chrome-flags="--headless=new" --quiet`.
     In a sandbox with a preinstalled browser, add `--chrome-path=<path to chromium>` and
     `--no-sandbox` to the chrome flags. From `lh.json` read the category scores; the LCP, CLS and TBT display values; the
     `largest-contentful-paint-element` phases (a large Render Delay on a text LCP usually
     means a web-font swap); `errors-in-console` (a CSP block shows here); and the hosts in
     `network-requests`, which prove which analytics tags actually fire (the static audit
     can't). These are lab numbers under simulated slow 4G, not field data.
     Run one template per run and re-run any page that returns `NO_FCP` or an empty
     category list (headless Chrome sometimes fails to paint; it is not a site fault). A
     multi-second TTFB phase inside the LCP breakdown while `server-response-time` reads
     under 300 ms is the simulator's model of the slow link, not the server. A
     `robots-txt` audit that fails with no items listed is a fetch flake; check the file
     with a GET before reporting it.
   - `--no-ai-probe` — skip requesting the homepage with AI-crawler user-agents.
   - `--offline-dns` — skip third-party lookups (RDAP domain expiry, DNS-over-HTTPS) and
     the owned-domains probe, which fetches up to 12 other sites once each.
   - `--no-pdf`, `--json`, `--out DIR`.

   It writes `seo-report-<domain>-<date>.html`, `.pdf`, and optionally `.json`.

4. **Verify before you report.** Re-check the top findings by hand with one request each.
   Anything reported broken gets a real GET (some servers 404 a HEAD on routes that serve
   GET fine). If two findings conflict, resolve it first. If the site is being changed
   while you audit, re-run immediately before reporting; findings go stale in hours.

5. **Review Google Analytics and Search Console whenever access exists, and ask for it
   when it does not.** The crawl cannot see whether Google has indexed the pages, whether
   anyone is shown them, or whether GA4 receives, keeps and counts what the site sends.
   With a read-only service-account key:

   ```bash
   python3 scripts/google_console_review.py --site <URL> --key <key.json> \
     --urls-from-sitemap --inspect-max 400 --days 90 --launched YYYY-MM-DD \
     --brand <brand words> --audit-json <output-dir>/seo-report-<domain>-<date>.json \
     --json <output-dir>/console.json --markdown <output-dir>/console.md
   ```

   Search Console's record can be months older than the page: a site can score 84 on
   the crawl while a quarter of its pages still carry a noindex verdict from a crawl
   made before a header was removed (133 of 473 sitemap URLs on one site, 85 days after
   the fix, 2026-11-23). Nothing in the crawl can see that.

   The key can also come from an environment variable holding the JSON or its base64
   (`--key-env VAR`, or one of the default names the script looks for), which is how a
   cloud session or CI job carries it. Look for one before asking:
   `env | grep -iE 'GOOGLE|GSC|GA_' | sed 's/=.*/=<set>/'`, and check the project's notes
   for a key path. Never print or `cat` the key, never write it into the repository. With
   no access at all, ask once for a read-only key or for the exports listed under "No API
   access" in the reference, and carry on with the crawl while you wait.

   Run it after the crawl so `--audit-json` can join the two, and in the background: URL
   Inspection takes 3 to 10 seconds a URL, so 400 URLs is 15 to 25 minutes. Choose the flags
   on purpose:
   - `--inspect-max`: the whole sitemap when it is under about 1,500 URLs (the quota is
     about 2,000 a day per property and re-runs spend it again). Above that, the sample is
     spread across URL templates and the table carries a scaled site-wide estimate.
   - `--days`: 28 for an established site; 90 when 28 days holds under about 1,000
     impressions, because nothing is readable below that.
   - `--launched`: the date of the last change Google must have seen (a relaunch, a
     noindex removed, a migration).
   - `--brand`: the brand's words, with the common misspelling, to split brand from
     non-brand demand.

   The order of work:

   1. **Check access first.** Confirm the key parses (field names and `client_email`
      only) and see what the account can read. GA4 and Search Console are granted
      separately, each inside its own console; an IAM role does nothing. A 200 with an
      empty property list means the owner has not added the `client_email` yet. The script
      reports that as a finding and reviews whichever half it can. Tell the owner the
      exact address to add and where (`references/google-console-review.md`, "Access").
   2. **Run the review** with `--urls-from-sitemap`. It inspects the sitemap's URLs (all
      of them up to `--inspect-max`, a sample across URL templates above that),
      pulls search performance for the window, the window before and all 16 months, reads
      the GA4 settings, the events for the window, and 14 months of sessions by date,
      source, landing page, event and hostname, and fetches the homepage to see whether
      the stream's measurement ID is in it.
   3. **Place the site on the ladder** (measured, indexed, shown, clicked, converting; the
      table is in the reference) and stop at the first "no". That is the headline. The
      findings that decide it: *GA4 has received no events*, *Sitemap URLs Google has not
      indexed*, *Pages Google has never seen*, *The site is close to invisible in Google
      Search*.
   4. **Before proposing to fix missing measurement, find out why it is missing.** Read
      the site's commit history, privacy policy, and tests for the tag. On 2026-11-24 the
      tag had been removed on purpose, with a test that failed if it came back and a
      privacy policy that promised its absence. Putting it back was the owner's call, and
      it meant rewriting the policy in the same change. Ask; do not patch.
   5. **Verify the tag in a browser** when the hosts are reachable: load the page in
      headless Chromium, list requests to `googletagmanager.com` and
      `google-analytics.com/g/collect`, and read `window.dataLayer`. The crawl and the
      homepage check only see an ID in HTML. If the sandbox blocks those hosts, say the
      firing check was not done.
   6. **Use the data in the SEO findings.** Queries and pages with impressions decide
      which titles are worth rewriting; with none, say that copy changes cannot be
      measured yet. GA4's landing pages show where visits really arrive (a sign-in URL
      near the top is a finding). Source/medium shows which campaign links are tagged in a
      way GA4 can file. The joins are listed in the reference.
   7. **Report the configuration changes as a list the owner can act on**, each with its
      Admin path, in two groups: settings that lose data every day they stay wrong
      (retention, key events, custom dimensions, the history-change page view on a
      single-page app, the internal-traffic filter left in Testing) and the rest
      (unwanted referrals, Search Console link, redaction, annotations). The full
      checklist for both consoles is "Recommended configuration" in the reference. Say
      which items the API could not show.

   The script never writes: it requests read-only scopes and refuses any other POST.
   Applying settings needs Editor access and a separate, explicit request from the owner.
   A relaunch two days old can score 90 on the crawl while Search Console still holds the
   old site and GA4 is on two-month retention with no key event; an eleven-week-old site
   can score 92 with one page indexed and no measurement at all. These facts outrank the
   score.

   Read the output in this order, and let it reorder the crawl's findings:
   1. **Index state by template** (URL inspection table). Any noindex verdict the tool
      re-fetched and found indexable today is the top finding of the whole review. It does
      not clear by itself: Google recrawls a page it believes is noindex rarely, and a
      fresh sitemap `<lastmod>` alone did not trigger it in the case above. The fix is
      the owner's "Request indexing" on the hubs, about a dozen a day, so give them the
      list in priority order (hubs that link to the most pages first).
   2. **Search performance by URL template.** Which template earns the impressions and at
      what position decides where on-page work pays. A template ranking around 10 is worth
      title and content work; one ranking around 40 is not going to move on a title.
   3. **Queries within reach** (positions 4 to 20). Check each page's title, H1 and first
      paragraph against the query before proposing a change; many already match and need
      links or time instead.
   4. **Crawl findings ranked by search demand.** Fix from the top. This is the table that
      turns the crawl's severity order into a work order.
   5. The GA4 settings (retention, key events, custom dimensions) and the rest.

   Do not: quote a position that sits on a handful of impressions; read a zero-impression
   template as "no demand" before checking whether it is indexed; treat the raw totals of a
   sampled inspection as site-wide rates (use the scaled estimate row); or change a
   Google setting. The tool never writes; resubmitting a sitemap or requesting indexing is
   the owner's click unless they ask you in writing. What each field means, the traps, and
   what the APIs refuse to show are in `references/google-console-review.md`.

6. **Report back.** Lead with what blocks launch, not with the score. When Search Console
   was read, the index state comes first ("262 of 473 sitemap URLs indexed, 133 held as
   noindex from a crawl on 30 August"), then search demand by template, then the crawl.
   Give the overall score with its coverage ("81/100 from 115 of 480 URLs, all 8 templates
   sampled"), then the CRITICAL and HIGH items, then the highest-leverage fixes (high
   impact, low effort) in the order the demand table gives. Separate what you verified
   from what the tool reported, and state what was not checked (Core Web Vitals without
   PageSpeed, anything behind a login, the money path, Search Console when no access was
   given). Interpret; don't dump the table.

   **Before applying a fix in the site's own repository**, read the project's rules and
   run its tests: a finding's suggested fix is a hypothesis about someone else's product.
   On the site above, `rel="nofollow"` on links to a noindex utility page was the textbook
   fix and the repository had a guard test, backed by an owner ruling, that forbids the
   token anywhere in the source. Leave such a fix out and say why. Split the work list
   into what you changed, what needs the owner's click in a console, and what needs a
   product decision.

7. **Competitive analysis (when the question is "who is beating us and why").** The full
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
- **False positives fixed in 2.2.0.** An image with `alt=""` is decorative by the HTML
  spec and WCAG and no longer counts as missing alt (only an absent attribute does); a
  page whose images are all `alt=""` gets one unscored INFO note. Analytics loaded from a
  first-party script is recognised by its IDs (a bare GA4 `G-` measurement ID, numeric
  `data-meta` / `data-linkedin` attributes, `fbq(`), and the LinkedIn Insight Tag,
  Cloudflare Web Analytics and Umami are detected; when nothing is found the finding says
  what was searched and that a first-party loader is invisible to it. The footprint table
  skips a platform's own pages (privacy policy, legal, help, product and feature pages)
  and on GitHub accepts only `owner` or `owner/repo`. "No BreadcrumbList schema" needs at
  least one crawled URL two path segments deep, since a flat site has no hierarchy to
  show. A meta description without an action verb is now one site-level LOW, raised only
  when more than half of at least three non-legal pages lack one, instead of a LOW on
  nearly every page. A CSP that blocks the Cloudflare Web Analytics beacon is named as
  such, with the exact directives to add or the dashboard switch to turn it off.
- **False positives fixed in 2.3.0** (a prerendered single-page app audited from a cloud
  sandbox, 2026-11-24; the same site went from 79 to 92 with nothing changed on it). The
  TLS expiry check is skipped, and says so, when the run goes through an HTTPS proxy,
  because the certificate it would read is the proxy's (the report had flagged a 29-day
  certificate issued by the sandbox). A Content-Security-Policy delivered in a
  `<meta http-equiv>` tag counts as present; X-Frame-Options is still judged, since a meta
  policy cannot carry `frame-ancestors`. A page that answers 200 with noindex and was
  reached by a link, not listed in the sitemap, is a utility page (sign-in, account): it
  gets one INFO note in place of noindex, missing-H1, thin-content and duplicate-title
  findings. Links carrying `rel=nofollow` to noindexed pages no longer count toward
  "Internal links lead to noindexed pages". The soft-404 finding now says what the 200
  body claims (noindex, a canonical and where it points, or neither), which is what
  decides how much it matters on a single-page-app host.
- **Console reviewer 0.2.0.** Reads the key from an environment variable; reviews one
  console when the account was only added to the other, with an access finding for the
  missing one; dates the last GA4 event when the window is empty and checks the homepage
  for the measurement ID; reports key events nothing sends, hostnames other than the
  site's, campaign traffic filed under Unassigned, sitemap URLs held back from the index
  by coverage state, other hosts Google found the pages through, and near-zero search
  visibility on a site whose sitemap is more than 60 days old.
- **Console reviewer 0.3.0 (skill 2.4.0).** Search Console data now sets the review's
  priorities. `--inspect-max` inspects up to 1,800 URLs, sampled across URL templates when
  the sitemap is larger, several at a time. Every noindex verdict is re-fetched live, so a
  stale verdict (Google's record is old, the page is indexable now) is told apart from a
  sitemap entry that really serves noindex; verdicts explained that way are left out of
  the general held-back finding, which now breaks down by template. New findings: a
  canonical Google chose against the declared one, and copies mostly over 60 days old.
  New tables: performance by URL template, queries at positions 4 to 20, queries split
  across pages, brand against non-brand (`--brand`), a weekly trend, and the crawl's
  findings ranked by search demand (`--audit-json`). No crawl check changed in 2.4.0.
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
against false positives), plus `tests/test_checks_21.py` and `tests/test_checks_22.py`
(the 2.1.0 and 2.2.0 checks and fixed false positives, each with a firing and a silent
case), `tests/test_checks_23.py` (the 2.3.0 fixes), `tests/test_google_console_review.py` (the console reviewer against canned Google
responses, no key needed: collection, the findings rules, template sampling and the
Search Console roll-ups), `tests/test_compare.py` and `tests/test_find_competitors.py`
(the comparison and discovery scripts on synthetic inputs). Run it after changing any check. No network access is needed.

## Notes

- The crawl audits what is observable from the public site. It does not log in, submit
  forms or execute JavaScript. `scripts/google_console_review.py` covers Search Console
  and GA4 read-only when a key is available; without one, say plainly that indexing,
  search performance and measurement were not reviewed, and ask for access.
- A sandbox changes what the tool can see. Behind an outbound allowlist, DNS-over-HTTPS,
  RDAP, PageSpeed and any host not on the list fail, and the report says "could not
  query"; list those as unchecked, not as passed. An old domain or a platform subdomain
  that Search Console names as a referrer is worth asking the owner to allow, or to check
  by hand, because whether it redirects is a real finding.
- The crawl score moves when the tool changes. When comparing with an earlier run, compare
  the findings, and re-run the old commit of the site with the new version if the number
  matters.
- Search Console's verdicts are as old as Google's last crawl of each URL. Always compare
  a verdict with what the page serves now before calling it a live fault, and always date
  it.
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
