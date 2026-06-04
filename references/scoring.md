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

| category | weight | what it covers |
|----------|--------|----------------|
| crawlability | 0.20 | reachability, status codes, redirects, robots/sitemap, client-render, soft 404, noindex |
| on page | 0.18 | title, meta description, H1, headings, word count, duplicates, alt text, CTA |
| cwv | 0.15 | Core Web Vitals via PageSpeed Insights — lab LCP/INP/CLS/TBT/FCP plus real-user CrUX field data (LCP, INP, CLS at the 75th percentile) when available |
| schema | 0.10 | JSON-LD presence/validity, schema/content mismatches (e.g. FAQPage without FAQ) |
| trust | 0.10 | HTTPS, security headers (CSP, HSTS, X-Frame-Options, etc.) |
| social | 0.10 | Open Graph / Twitter tags, share image dimensions & weight, favicon |
| mobile | 0.10 | viewport meta, responsive scaling, zoom |
| ai search | 0.07 | content visible to non-JS AI crawlers, html lang, AI-crawler robots rules |

To change weights, edit `CATEGORY_WEIGHTS` in `seo_audit.py` (they don't need to sum to
1; the overall is normalized by the weights actually used).

## Severity → score

`SEVERITY_WEIGHT = {CRITICAL: 40, HIGH: 20, MEDIUM: 10, LOW: 4, INFO: 0}`

`category_score = max(0, 100 - sum(weight of each DISTINCT finding in that category))`

So one CRITICAL alone drops a category to 60; three MEDIUMs drop it to 70. INFO findings
are informational and never reduce the score.

**Findings are deduplicated by `(category, title)` before scoring.** A site-wide issue
(missing security headers, client-rendering, a shared duplicate title) appears on every
crawled page, but counts only once toward the score — otherwise the same problem would be
deducted N times and the score would swing with `--max-pages`. This keeps the score stable
and comparable across runs and crawl depths. The per-page tables still list every instance,
so coverage is unchanged; only the double-counting in the score is removed. (See the dedup
loop in `score_categories()`.)

### Core Web Vitals special-casing

- If PageSpeed returns a performance score, the `cwv` category is capped at that score.
- If PageSpeed is rate-limited, skipped, or unavailable, `cwv` is set to 100 (neutral) —
  we don't penalize a site for a measurement we couldn't take. An INFO finding records
  that it was unavailable so the reader knows it wasn't actually verified.
- **Field data (CrUX):** when the site has enough traffic, PageSpeed returns real-user
  metrics at the 75th percentile. INP (which replaced FID in March 2024) and field LCP/CLS
  in the SLOW band add a HIGH finding; AVERAGE adds a MEDIUM. This is the data Google
  actually uses for page experience. Low-traffic sites have no field data; the report says
  so rather than guessing, and lab metrics still display.

## Confidence level

Shown in the report header (low / medium / high):

- **low** — the site was largely unreachable (no pages returned content).
- **medium** — pages were analyzed but PageSpeed/CWV data was not available.
- **high** — pages analyzed and PageSpeed data obtained.

## The checks (by category)

This is the current set. Each lives inline in `analyze_page()`, `analyze_social()`,
`pagespeed()`, or the site-wide section of `run_audit()`.

**crawlability:** unreachable page (CRITICAL); 5xx (CRITICAL) / 4xx (HIGH); redirect
(LOW); client-rendered / content missing from raw HTML (CRITICAL); soft 404 (HIGH);
noindex (HIGH); robots.txt missing (MEDIUM); sitemap missing (MEDIUM); robots blocks all
(CRITICAL).

**on page:** missing/over-long/short title; missing/over-long/short meta description;
meta description lacks CTA verb; missing H1 (HIGH); multiple H1; thin content <300 words;
long content (≥600 words) without subheadings (LOW); missing canonical tag (LOW);
images missing alt; duplicate title across pages; duplicate meta description across pages.

**schema:** no JSON-LD (LOW); invalid JSON-LD (MEDIUM); FAQPage schema without visible FAQ
content (MEDIUM); homepage missing Organization/WebSite schema (LOW).

**trust:** not HTTPS (CRITICAL); mixed content — http:// subresources on an HTTPS page
(MEDIUM); missing security headers (MEDIUM if CSP missing, else LOW).

**social:** OG image aspect ratio far from 1.91:1 (MEDIUM); no OG image (MEDIUM); image over
1 MB (LOW); missing twitter:card (LOW); missing og:title (LOW); favicon over 100 KB (LOW);
no CTA verb in social title/description (LOW).

**mobile:** missing viewport (HIGH); non-responsive viewport (MEDIUM); viewport disables
zoom (LOW).

**ai search:** content invisible to AI crawlers when client-rendered (HIGH); missing html
lang (LOW); robots.txt blocks a named AI crawler like GPTBot/ClaudeBot (LOW); no llms.txt
(INFO — emerging convention, no measured citation benefit, so it never moves the score).

**cwv:** field LCP/INP/CLS poor at 75th pct (HIGH) / needs improvement (MEDIUM); poor
PageSpeed score <50 (HIGH); <90 (MEDIUM); PSI rate-limited/unavailable (INFO).

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
4. Re-run against a known site and confirm the finding appears and the score moves as
   expected.
