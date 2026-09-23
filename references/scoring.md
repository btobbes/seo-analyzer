# Scoring model & checks

This documents how `scripts/seo_audit.py` turns observations into scores and findings.
Read this when you want to tune the model, add a check, or explain a score.

## The shape of a finding

Every finding is a dict with these fields (see the `f()` helper in `seo_audit.py`):

| field | meaning |
|-------|---------|
| `severity` | CRITICAL / HIGH / MEDIUM / LOW / INFO — drives both sorting and scoring |
| `title` | short human-readable name |
| `category` | one of the eight categories below |
| `impact` | 1–5, how much fixing it helps SEO |
| `effort` | 1–5, how hard it is to fix |
| `observed` | the concrete evidence (what was measured) |
| `fix` | a specific, actionable recommendation |

Impact/effort is shown in the report as `I/E` (e.g. `5/1`). High impact + low effort =
do first. It's advisory; it does not affect the score.

## Categories and weights

The overall score is a weighted average of the eight category scores:

Weights were rebalanced in 2.0.0. AI search went from 0.07 to 0.10: the category gained
real checks (edge access, snippet controls, AI-preference signals) instead of one
robots.txt scan. It stays below crawlability and performance on purpose. The 2026 studies
in `sources-2026.md` put AI referrals at roughly 1% of site traffic and find off-page
signals predict AI citations far better than anything on the page, so an HTML audit must
not let AI findings outrank crawlability or Core Web Vitals. Mobile and social gave up the
difference: a viewport tag and an Open Graph set are table stakes that nearly every site
passes.

| category | weight | what it covers |
|----------|--------|----------------|
| crawlability | 0.20 | link architecture (links to non-canonical / noindexed / redirecting URLs, canonical pointing at a thinner page, sitemap-only templates), sitemap hygiene and lastmod, HEAD/GET parity, robots.txt 5xx and Content-Signal search=no, reachability, status codes, redirect chains, robots/sitemap quality, client-render, soft 404s, noindex (meta + header), canonicals, hreflang, host variants, 404 handling, broken internal links |
| on page | 0.18 | near-duplicate pages and boilerplate-heavy templates, repeated passages, title, meta description, H1, heading hierarchy, word count, duplicates, alt text, CTA, URL hygiene, internal linking, analytics presence |
| performance | 0.15 | the likely LCP image (hotlinked, redirected, heavy, lazy), oversized thumbnails, web-font payload, CSP blocking the page's own scripts, third-party script ahead of CSS, no-store HTML, plus page-observable signals (compression, render-blocking JS, page weight, DOM size, image dimensions/lazy-loading/formats, asset caching, TTFB) plus PageSpeed Insights — lab LCP/INP/CLS/TBT/FCP and real-user CrUX field data (LCP, INP, CLS at the 75th percentile) when available |
| schema | 0.10 | homepage Organization completeness, Article recommended properties, self-serving review markup, retired rich-result types (INFO), JSON-LD presence/validity, schema/content mismatches (e.g. FAQPage without FAQ), entity schema (Organization/WebSite, sameAs), breadcrumbs, LocalBusiness completeness |
| trust | 0.10 | domain registration expiry (RDAP), contact domains that can't receive mail, SPF/DMARC, security.txt, exposed private files, postal address / legal entity, HTTPS enforcement (incl. the plain-http variant), TLS certificate expiry, security headers, mixed content, contact route & trust pages (E-E-A-T) |
| social | 0.08 | share image present, loading and landscape on every audited template, Open Graph / Twitter tags & completeness, share image dimensions & weight, favicon |
| mobile | 0.09 | viewport meta, responsive scaling, zoom; Lighthouse font-size / tap-targets when PSI runs |
| ai search | 0.10 | AI crawler access tested at the network edge by user-agent, robots.txt AI rules (RFC 9309 group merging) and Content-Signal ai-input, snippet controls (nosnippet / max-snippet, which also govern AI Overviews and AI Mode), visible dates on articles, llms.txt link quality, content visible to non-JS crawlers, html lang |

To change weights, edit `CATEGORY_WEIGHTS` in `seo_audit.py` (they don't need to sum to
1; the overall is normalized by the weights actually used).

## Severity → score

`SEVERITY_WEIGHT = {CRITICAL: 40, HIGH: 20, MEDIUM: 10, LOW: 4, INFO: 0}`

`category_score = max(0, 100 - sum(weight of each DISTINCT finding in that category))`

**Diminishing returns (2.0.0).** Within a category the distinct findings are sorted by
weight; the two heaviest count in full, the next two at 75%, the rest at 50%. The check
list roughly doubled in 2.0.0, and with straight addition a category hit the floor on a
pile of LOWs and stopped telling a mediocre site from a terrible one. One CRITICAL alone
still drops a category to 60; three MEDIUMs drop it to 72.5 → 73; INFO never counts.

**Findings are deduplicated by `(category, title)` before scoring.** A site-wide issue
(missing security headers, client-rendering, a shared duplicate title) appears on every
crawled page, but counts only once toward the score — otherwise the same problem would be
deducted N times and the score would swing with `--max-pages`. This keeps the score stable
and comparable across runs and crawl depths. The per-page tables still list every instance,
so coverage is unchanged; only the double-counting in the score is removed. (See the dedup
loop in `score_categories()`.)

### Performance special-casing

- The base `performance` score reflects signals observed directly from the HTML and
  headers (compression, render-blocking JS, page weight, DOM size, image handling,
  caching, response time) — those always count because we did observe them.
- If PageSpeed returns a performance score, the category is additionally **capped** at
  that score.
- If PageSpeed is rate-limited, skipped, or unavailable, no cap is applied and an INFO
  finding records that CWV wasn't verified — the site isn't punished for a measurement
  we couldn't take, but the observable findings still stand.
- **Field data (CrUX):** when the site has enough traffic, PageSpeed returns real-user
  metrics at the 75th percentile. INP (which replaced FID in March 2024) and field LCP/CLS
  in the SLOW band add a HIGH finding; AVERAGE adds a MEDIUM. This is the data Google
  actually uses for page experience. Low-traffic sites have no field data; the report says
  so rather than guessing, and lab metrics still display.

## Sampling, the sweep, and coverage

`discover_pages()` no longer takes the first N sitemap entries. `stratified_sample()`
clusters URLs by path shape (`url_template()`: first segment kept, years collapsed to
`{year}`, deeper segments wildcarded, trailing slash preserved), gives every template one
slot before any template gets a second (largest first), and spreads picks evenly inside
each cluster. The homepage is always first.

`--max-pages` pages ("deep") get per-page tables plus the checks that cost extra requests
(hero image, thumbnails, share image), which run once per template. `--sweep` more pages
are analyzed with `analyze_page()` and the no-extra-request checks, then aggregated: one
site-wide finding per issue type that the deep pages didn't already show, with a count,
the templates affected, and an example. They score like any other finding (once per
`(category, title)`), so the score still doesn't swing with crawl size; it just reflects
more of the site. **Prevalence scaling:** an issue found on fewer than 10% of swept pages
(and not on any deep page) scores one severity step lower, because it is a page problem
rather than a template problem; CRITICAL is never softened. The report's **Coverage by URL template** table shows URLs vs audited
per template, and `coverage_gaps` lists templates with ≥5 URLs that were never sampled.

## Confidence level

Shown in the report header (low / medium / high):

- **low** — the site was largely unreachable (no pages returned content).
- **medium** — pages were analyzed but PageSpeed/CWV data was not available.
- **high** — pages analyzed, PageSpeed data obtained, and every sitemap template with ≥5
  URLs was sampled at least once. A coverage gap caps confidence at medium.

## The checks (by category)

This is the current set. Each lives inline in `analyze_page()`, `analyze_social()`,
`pagespeed()`, the site-wide check functions (`check_host_variants`, `check_custom_404`,
`tls_certificate`, `check_trust_pages`, `check_broken_links`, `check_asset_caching`,
`detect_analytics`), or the site-wide section of `run_audit()`.

**crawlability:** unreachable page (CRITICAL); 5xx (CRITICAL) / 4xx (HIGH); redirect (LOW,
suppressed when it's mere scheme/www/slash normalization); redirect chain ≥2 hops (MEDIUM —
`fetch()` follows redirects manually and records the hop chain); client-rendered / content
missing from raw HTML (CRITICAL); soft 404 (HIGH); noindex via meta (HIGH) or X-Robots-Tag
header (HIGH); meta-refresh redirect (HIGH); canonical to another domain / to http:// / to a
different URL (MEDIUM each); hreflang set without self-reference (LOW); robots.txt missing
(MEDIUM); sitemap missing (MEDIUM) / empty (MEDIUM) / not referenced in robots.txt (LOW) /
listing redirecting URLs (LOW); robots blocks all (CRITICAL); duplicate host serving without
redirect — www vs non-www (MEDIUM); host variants unreachable (LOW); missing pages return
200 (HIGH) or redirect to homepage (MEDIUM); broken internal links from a ≤30-link sample
(HIGH); internal links hitting 5xx (MEDIUM) or redirects (LOW); Lighthouse `crawlable-anchors`
/ `canonical` / `hreflang` failures when PSI runs.

**on page:** missing/over-long/short title; missing/over-long/short meta description;
meta description lacks CTA verb; missing H1 (HIGH); multiple H1 (LOW); heading levels skip
(LOW); thin content <300 words; long content (≥600 words) without subheadings (LOW);
missing canonical tag (LOW); URL slug not clean — uppercase/underscores/spaces (LOW);
dead-end page with no internal links (LOW); very high internal link count >300 (LOW);
images missing alt; duplicate title across pages; duplicate meta description across pages;
no analytics detected (INFO) / only deprecated Universal Analytics (LOW).

**Client-render suppression:** when a page is detected as client-rendered, the dependent
content checks (missing H1, thin content, alt text, trust-page/link checks) are suppressed —
the content exists after JS runs, we just can't see it. One CRITICAL carries that root cause
instead of a pile of misleading findings that would double-count it.

**schema:** no JSON-LD (LOW); invalid JSON-LD (MEDIUM); FAQPage schema without visible FAQ
content (MEDIUM); homepage missing Organization/WebSite schema (LOW); social profiles not in
`sameAs` (LOW); no BreadcrumbList on any crawled page when ≥3 pages have server-rendered text
(LOW); LocalBusiness schema incomplete — missing phone/address/geo/hours (LOW); local signals
(tel:/Maps links) but no LocalBusiness schema at all (MEDIUM).

**trust:** not HTTPS (CRITICAL); http:// version serves 200 without redirecting to HTTPS
(HIGH); mixed content — http:// subresources on an HTTPS page (MEDIUM); missing security
headers (MEDIUM if CSP missing, else LOW); TLS certificate expired (CRITICAL) / <14 days
(HIGH) / <30 days (MEDIUM); no visible contact route — no contact page, tel: or mailto:
(MEDIUM); trust pages (about/privacy/terms) not found (LOW). Contact/trust checks are
skipped when the site exposes no crawlable links at all.

**social:** OG image aspect ratio far from 1.91:1 (MEDIUM); no OG image (MEDIUM); image over
1 MB (LOW); missing twitter:card (LOW); missing og:title (LOW); OG set incomplete —
og:description/og:url/og:type/og:site_name (LOW); favicon over 100 KB (LOW); no CTA verb in
social title/description (LOW).

**mobile:** missing viewport (HIGH); non-responsive viewport (MEDIUM); viewport disables
zoom (LOW); Lighthouse `font-size` / `tap-targets` failures when PSI runs (MEDIUM each) —
those need a real rendering browser, which PSI provides.

**ai search:** content invisible to AI crawlers when client-rendered (HIGH); missing html
lang (LOW); robots.txt blocks named AI crawlers — one aggregated finding (LOW; the list
covers GPTBot, OAI-SearchBot, ChatGPT-User, ClaudeBot, Claude-Web, anthropic-ai,
PerplexityBot, Perplexity-User, Google-Extended, Applebot-Extended, Amazonbot,
meta-externalagent, Bytespider, CCBot, cohere-ai, DuckAssistBot); no llms.txt (INFO —
emerging convention, no measured citation benefit, so it never moves the score).
Robots evaluation follows RFC 9309: groups for the same agent are merged across the file
and `Allow` wins a specificity tie — so a Cloudflare-managed `Disallow: /` that the
operator re-allows below is correctly read as allowed.

**performance (page-observable, no PSI needed):** HTML served without compression (MEDIUM);
large/heavy HTML document (MEDIUM/LOW); render-blocking scripts in head (MEDIUM/LOW); many
resource requests (MEDIUM/LOW); very large DOM (MEDIUM/LOW); images without width/height —
CLS risk (MEDIUM/LOW); no lazy-loading with ≥8 images (LOW); no modern image formats with
≥5 JPEG/PNG (LOW); slow server response (MEDIUM/LOW); static assets without long-lived
Cache-Control, sampled from the homepage's first CSS/JS/image (LOW).

**performance (PSI):** field LCP/INP/CLS poor at 75th pct (HIGH) / needs improvement
(MEDIUM); poor PageSpeed score <50 (HIGH); <90 (MEDIUM); top 3 Lighthouse opportunities
≥300 ms (INFO — the perf score already caps the category, these add the "what to do");
PSI rate-limited/unavailable (INFO). PSI is queried with all four Lighthouse categories,
so the report also shows Lighthouse SEO / Accessibility / Best-practices scores.

## Checks added in 2.0.0

These sit alongside the lists above. Function names are in `scripts/seo_audit.py`.

**crawlability** — `check_link_architecture`: broken internal links (HIGH), links to 5xx
(MEDIUM), links at redirects ≥5 (LOW), links using slash/scheme/www variants that 301 ≥3
(LOW), *internal links point at non-canonical URLs* (MEDIUM; HIGH when no canonical target
is linked from any crawled page), *canonical points at a thinner page than the one
carrying it* (HIGH; ≥150 fewer words and <75% of the source, schema types lost are
listed), *internal links lead to noindexed pages* (LOW; MEDIUM at ≥40% of the sample),
*a sitemap template gets no internal links* (MEDIUM; only when the crawl saw a meaningful
share of the sitemap). Sitemap: lists error URLs (HIGH at ≥10%, else MEDIUM; 404/410/5xx
only), noindexed URLs (MEDIUM), non-canonical URLs (MEDIUM), redirecting URLs (LOW),
lastmod on <80% of URLs / all identical / in the future (LOW each). robots.txt answers 5xx
(CRITICAL), is an HTML page (HIGH), exceeds 500 KiB (MEDIUM). AI-preference `search=no`
(HIGH). HTML over Googlebot's 2 MB uncompressed fetch limit (HIGH). Server answers br/zstd
when only gzip was offered (MEDIUM). HEAD fails where GET succeeds (LOW: it breaks link
checkers and monitors, not crawlers; no social platform documents using HEAD).
Site refuses non-browser user-agents, audited via browser-UA fallback (INFO).

**ai search** — `probe_ai_agents` requests the homepage and one inner page as 17
documented agents (bot rules can vary page by page): search/user agents refused at the
edge (HIGH); only training agents refused (MEDIUM; the fix text notes when robots.txt
names them, since then the file and the edge disagree); every non-browser UA refused
(MEDIUM, generic bot protection, test inconclusive). Every finding says it is a
differential response by User-Agent from an ordinary IP, not proof the real bot is
blocked. robots.txt: search/user tokens disallowed (MEDIUM), only training/opt-out tokens
(LOW). AI-preference signals: Content-Signal `ai-input=no` or IETF Content-Usage
`ai-use=n`, in robots.txt or as a response header (MEDIUM). `noarchive`/`nocache` (LOW:
dead at Google, but Bing maps them to Chat/Copilot behavior). `check_answer_readiness`: snippets disabled (HIGH), max-snippet
under 50 (LOW), ≥5 `data-nosnippet` elements (LOW), article with no visible date (LOW).
`check_llms_txt`: everything INFO. A missing llms.txt is never a finding.

**performance** — `check_hero_and_thumbs`: priority image lazy-loaded (MEDIUM), hero
hotlinked cross-origin through redirects (MEDIUM), hero on a third-party origin (LOW),
hero >300 KB (MEDIUM) or >150 KB without srcset (LOW), small images served from large
files (MEDIUM with ≥12 such images, else LOW). `check_fonts`: Google Fonts payload
>150 KB (LOW) / >250 KB (MEDIUM), no preconnect (LOW), no `display=` (LOW).
`check_csp_blocks` (MEDIUM), `check_head_order` (LOW), `check_html_caching` no-store (LOW).

**schema** — `check_schema_depth`: Article missing image/dates/author/author.url (LOW),
self-serving review markup on the site's own business entity (MEDIUM), thin homepage
Organization, two or more of logo/sameAs/legalName/address/foundingDate/contact missing
(LOW), retired rich-result types (INFO).

**trust** — domain registration ≤30 days (HIGH) / ≤60 days (MEDIUM), contact address on a
domain that cannot receive mail, meaning Null MX or no MX/A/AAAA at all (MEDIUM; no MX
alone is not a failure, RFC 5321 falls back to A), SPF or DMARC missing on the site's own
mail domain (LOW), DMARC without `rua` (LOW), DKIM never reported (selectors can't be
enumerated),
security.txt expired or without Expires (LOW), private files downloadable (CRITICAL;
four gates: 200, non-HTML type, format signature, differs from a random-path control), no
postal address on the crawled or about/contact/legal pages (LOW).

**social** — per-template share image: missing (LOW), does not load (MEDIUM), under 600px
wide or far from landscape (LOW). Always fetched with GET. Homepage: og:image over 600 KB
(WhatsApp's documented ceiling), `</head>` beyond the first 300 KB (LOW each).

**on page** — near-duplicate pages within a template, ≥80% shared 6-word shingles
(MEDIUM); template median under 120 unique words (LOW); a ≥12-word sentence repeated on
one page (LOW).

## Checks added in 2.1.0

Each came out of the competitive audit of 2026-09-22 (see `competitive-analysis.md`),
where the client's lost position was partly its own doing. Function names are in
`scripts/seo_audit.py`.

**trust** — `check_rating_consistency`: the site's own LocalBusiness/Organization-family
nodes declare different `reviewCount`/`ratingCount` values for the same entity name on
different pages (LOW). Product nodes are ignored, since different products may differ.

**on page** — `check_affiliate_share`: of the analysed pages (status 200, at least 20 of
them), the share whose outbound links carry affiliate tracking: Amazon `tag=`, Viator
`pid=P…`, Booking.com `aid=`, GetYourGuide `partner_id=`, Expedia `affcid=`, generic click
ids (`irclickid`, `clickid`, `affiliate_id`, `aff_id`, `affid`, `afftrack`), or a known
affiliate network host. LOW at 30%, MEDIUM at 50%; the templates and the kinds of
tracking are named. `ref=` and `utm_` alone never count.

**crawlability** — `check_owned_domains` (network; skipped with `--offline-dns`): the
homepage's `sameAs` URLs and its external links, minus social platforms, marketplaces,
CDNs and other well-known hosts, up to 12 candidates, fetched once each with a browser
UA. A domain is "owned" when it shares an analytics property id (`G-`, `GTM-`, `UA-`,
`AW-`) with the homepage, or when it is declared in `sameAs` *and* its own schema names
the same organization (a `sameAs` pointing at a DMO listing, a Wikipedia page or a Maps
share link is a pointer to a page about the business, not a domain it runs; the first
real-site run caught exactly that). Owned domains that return 200 and canonicalize to
themselves are *separate live sites* (LOW; MEDIUM with two or more, or when one shares
the analytics property). Owned domains that redirect or canonicalize to the audited site
are counted in the health row and never a finding. The JSON carries every probed
candidate under `owned_domains` (status, final host, canonical host, title, declared,
shares analytics, same entity name, links back).

**keyword focus (not scored)** — `title_concentration`: after stripping the brand segment
that 40% or more of titles share, the two-word phrase found in the most crawled titles,
reported when at least 5 titles and 15% of them carry it. Six owned URLs on three domains
chasing "flagstaff ghost tour" is the case it was written for.

## Fixed in 2.1.0

False positives hand-verified on one site and reproduced in `tests/test_checks_21.py`:

- **Render-blocking scripts** ignored `type="module"`; module scripts are deferred by
  specification, so a Vite/Astro bundle in `<head>` fired on every page of a modern build.
- **No modern image formats** only looked at `<img src>`; AVIF/WebP through
  `<picture><source type=image/avif>`, `srcset` or `<link rel=preload as=image>` was
  invisible.
- **Hero image weight** measured the `<img src>` JPEG fallback (716 KB) when the browser
  fetched the `<picture>` AVIF (282 KB). It now measures the largest candidate of the first
  AVIF/WebP source (or the preload's `imagesrcset`) and says which file it measured.
- **Web font payload** counted the same woff2 files twice when the Google Fonts
  stylesheet was linked twice (308 KB reported, 154 KB real). Files are counted once, and
  the duplicate `<link>` is its own LOW finding ("Google Fonts stylesheet is linked more
  than once").
- **LocalBusiness schema incomplete** picked a thinner `Organization` node over a
  `TravelAgency` node because subtypes were matched by substring against a short list.
  `_looks_local_type()` knows the schema.org LocalBusiness family and its suffixes.
- **Social & local footprint** listed every TripAdvisor `ShowUserReviews-*` permalink and
  every Facebook video as a profile row. Single-post permalinks are now noise.

### Deliberately not scored

Question headings, lists, tables, llms.txt presence, markdown content negotiation,
webmaster verification tags, HSTS sub-options, and retired schema types are shown but
never move the score. The evidence that they change rankings or AI citations is
correlational or absent, and a score should only punish what we can defend.

## Site health checks (report table)

`run_audit()` assembles a `health` list rendered as the **Site health checks** table:
host-variant behavior (http/https × www/non-www), 404 handling, TLS certificate expiry
and issuer, domain registration expiry, contact & trust pages, entity transparency, the
internal-link classification, detected analytics, HSTS, markdown negotiation, robots.txt
platform signals, llms.txt, AI-crawler robots rules and edge access, security.txt, email
authentication, and webmaster verification tags. The scored findings above are derived from these same
probes; the table gives the reader the raw observations.

## Social & local footprint (mostly non-scoring)

`extract_footprint()` produces the report's **Social & local footprint** section. It
collects outbound links across the crawled pages plus schema `sameAs` URLs, matches them
to known social platforms (skipping share/login URLs and bare homepages), and marks
whether each profile is declared in `sameAs`. It also detects Google Maps / Business
Profile links and extracts self-declared `LocalBusiness`/`Organization`/`Place` schema
(name, phone, address, geo, hours, `aggregateRating`).

It emits exactly one scored finding: **social profiles linked but none declared in sameAs
schema** (LOW, schema) — `sameAs` is what connects profiles to the entity in the knowledge
graph. Everything else in the section is displayed for context and does not affect the
score. Live Google Business Profile metrics are never scraped — that needs the Google
Places API; displayed ratings are self-declared in the site's schema and labeled as such.

## Keyword focus (non-scoring)

`extract_keywords()` produces the report's **Keyword focus** section. It is purely
informational — it never changes any category or the overall score.

It ranks 1–3 word phrases by **weighted prominence across zones**, because where a term
appears matters far more to SEO than how often:

| zone | weight | per-zone occurrence cap |
|------|--------|--------------------------|
| title | 5 | 2 |
| h1 | 4 | 2 |
| h2/h3 heading | 3 | 3 |
| meta description | 3 | 2 |
| url slug | 2 | 2 |
| schema text (name/description/etc.) | 2 | 2 |
| body | 1 | 6 |

Phrases are stopword-filtered (multi-word phrases never contain a stopword, so they read
as real keyphrases), pure numbers are dropped, and a shorter phrase is suppressed when a
higher-ranked longer phrase already contains it. The body cap keeps sheer repetition from
outranking deliberate placement in the title/H1. Each term reports its zones (shown as
chips in the report), its raw body count, and a prominence score normalized to the top
term = 100. `_keyword_insights()` derives a few observations (primary-term alignment,
body themes missing from any title/H1, strongest multi-word phrases). When no page has
crawlable text (fully client-rendered), the section says so instead of inventing keywords.

## Adding a check

1. Inside the relevant analysis function, append `f(severity, title, category, impact,
   effort, observed, fix)` to the `findings` list when the condition holds.
2. Use an existing category string so it rolls into a category score.
3. Keep `observed` factual (the measurement) and `fix` imperative and specific.
4. Add a defect to `tests/fixture_site.py`'s broken site and an assertion in
   `tests/test_fixture_audit.py`, and make sure the clean site stays silent. A check
   without a false-positive guard is a liability.
5. Run it against two or three unrelated real sites. Most false positives only show up on
   sites built differently from the one that motivated the check (bot-protected
   publishers, 50,000-URL sitemaps, sites with no sitemap).
6. Bump `VERSION` in `seo_audit.py`.

## Comparing sites

Category scores and the overall score are built from findings deduplicated by
`(category, title)`, with diminishing returns, so they describe *what was observed*.
They do not reward what a site has: a site with no structured data has no schema
findings about wrong structured data, and a site with no sitemap has no sitemap hygiene
findings. Two consequences for competitive work:

- Do not rank sites by overall score. Compare category by category, with the audited
  page counts beside them, and treat absent features (no robots.txt, no sitemap, no
  JSON-LD, no analytics, no trust pages, no share image) as the gap they are.
  `scripts/seo_compare.py` builds that view from two or more `--json` reports and prints a
  caveat when page counts differ by more than 3x or a site has no sitemap.
- Audit rivals with the same `--max-pages` and `--sweep` as the client, and re-audit
  after any migration settles; a rival caught mid-rebuild (old URLs 404, no sitemap yet)
  looks weaker than it will be in 60 days.
