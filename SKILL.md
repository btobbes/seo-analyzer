---
name: seo-audit
description: >-
  Audit a website's SEO health and produce a scored report (HTML + PDF) with an
  overall score out of 100, eight category scores (crawlability, Core Web Vitals,
  on-page, schema, trust, social, mobile, AI-search), a social-share preview
  analysis, and per-page findings tables — each finding tagged with severity,
  category, impact/effort, what was observed, and a concrete fix. Use this
  whenever the user wants to analyze, audit, score, or improve a site's SEO; check
  search-engine readiness, crawlability, structured data, meta tags, Open Graph /
  social previews, or AI-crawler visibility; or asks for an "SEO report" / "SEO
  audit" / "site health check" for a given URL or domain. Trigger even if they just
  paste a URL and ask "how's the SEO on this?" or "what's wrong with this site for
  search?".
---

# SEO Audit

Crawl a website, score it, and write a report a non-expert can act on. The report
mirrors the format of a professional SEO audit: one headline score, eight category
scores, a **Top priorities** action list (highest-leverage fixes first), a **Site
health checks** table (host-variant redirects, 404 handling, TLS certificate, contact
& trust pages, sampled internal links, analytics stack, AI-crawler access), a **Keyword
focus** section (which terms the site emphasizes in the places that matter to SEO), a
social-share preview block, a **Social & local footprint** section (linked social
profiles + Google Business Profile / local signals), and per-page tables of findings —
every finding paired with a concrete fix.
Checks are aligned with 2026 search priorities: content quality / E-E-A-T signals,
crawlability, page experience (Core Web Vitals incl. **INP** and real-user field data),
and visibility to AI answer engines (server-rendered content, schema, AI-crawler access).

## Workflow

1. **Get the target URL.** A bare domain is fine (`example.com` → `https://example.com`).
   If the user named pages they care about or a page count, note it for `--max-pages`.

2. **Run the analyzer.** It does everything — fetch, crawl, parse, score, render HTML
   and PDF — in one command. From the skill directory:

   ```bash
   python3 scripts/seo_audit.py <URL> --max-pages 5 --out <output-dir>
   ```

   Useful flags:
   - `--max-pages N` — how many pages to crawl (default 5; homepage is always first).
   - `--include-pages URL,URL` — force-include specific URLs or paths (repeatable).
     Use this when the operator named templates that sitemap order would skip
     (e.g. `/apply`, a winner page, a market hub). `--max-pages` is raised
     automatically so the seed set plus the homepage always fits.
   - `--out DIR` — where to write the report (default: current dir). Default to the
     user's working directory or `~/Downloads` unless they say otherwise.
   - `--pagespeed` — also query Google PageSpeed Insights for Core Web Vitals. Pulls both
     lab metrics (LCP, INP, CLS, TBT, FCP) and, when the site has enough traffic, Google's
     real-user **CrUX field data** at the 75th percentile (LCP, INP, CLS) — the data Google
     actually uses for page experience. Best-effort and frequently rate-limited without a
     key; the report degrades gracefully and marks CWV unavailable. Set a `PAGESPEED_API_KEY`
     env var to make it reliable (free key at developers.google.com/speed).
   - `--no-pdf` — skip PDF rendering (HTML only).
   - `--json` — also write the raw report data as JSON (handy for diffing re-runs).

   The script prints the output paths and the overall score to stderr. It writes
   `seo-report-<domain>-<date>.html` and `.pdf`.

3. **Report back.** Tell the user the overall score and the headline issues, link the
   generated HTML and PDF by path, and summarize the highest-leverage fixes — the
   findings with high impact and low effort (look at the I/E column; e.g. 5/1 = do
   first). Lead with CRITICAL and HIGH severity items. Don't just dump the table;
   interpret it: what's the single most important thing to fix and why.

## How it works (so you can explain or adjust it)

- **Site-wide probes** go beyond the crawled pages: all four host variants (http/https ×
  www/non-www) are fetched to verify they 301 to one canonical origin; a nonsense URL is
  probed to verify real 404 handling (soft-404 detection); the TLS certificate's expiry
  runway is inspected; up to 30 internal links beyond the crawled set are sampled for
  404s/5xx/redirects; the first same-site CSS/JS/image is checked for long-lived caching;
  and the analytics stack (GA4/GTM/Plausible/…) is detected. `fetch()` follows redirects
  manually, so redirect **chains** (≥2 hops) are flagged with the full hop list.
- **E-E-A-T / trust pages**: the crawl looks for a contact route (contact page, tel:,
  mailto:) and about/privacy/terms links — the things quality raters check. Skipped
  honestly when a client-rendered site exposes no crawlable links at all.
- **Client-render suppression**: when a page's raw HTML has no content but ships scripts,
  one CRITICAL finding carries that root cause and the dependent checks (H1, word count,
  alt text, trust pages) are suppressed instead of misreporting the rendered page.
- **Robots evaluation is RFC 9309-correct**: groups for the same user-agent merge across
  the file and Allow wins specificity ties — so a Cloudflare-managed "Disallow: /" that
  the operator re-allows below is read as allowed. The AI-crawler list covers 16 agents
  (GPTBot, OAI-SearchBot, ClaudeBot, Claude-Web, PerplexityBot, Google-Extended,
  Applebot-Extended, Amazonbot, meta-externalagent, Bytespider, CCBot, …).
  **HTTP access is probed separately.** A robots Allow is not treated as proof a
  crawler can read the site. The auditor fetches the homepage as a sample of
  named AI UAs (GPTBot, ClaudeBot, PerplexityBot, OAI-SearchBot, CCBot,
  Bytespider, Amazonbot, Google-Extended). A 403/4xx/5xx from the edge is a
  HIGH "AI crawlers blocked at the HTTP edge" finding. Never report AI
  visibility from robots.txt alone.
- **PageSpeed runs all four Lighthouse categories** when `--pagespeed` is set: the report
  shows Lighthouse SEO/Accessibility/Best-practices scores, mobile-only checks we can't do
  without a browser (font size, tap targets, crawlable anchors) become findings, and the
  top three load-time opportunities are listed as INFO.
- **Keyword focus** ranks the terms (1–3 word phrases) the site emphasizes by *weighted
  prominence*, not raw frequency: a term scores higher for appearing in the title (×5),
  H1 (×4), H2–H3/meta (×3), URL/schema (×2) than in body copy (×1, capped so repetition
  can't dominate placement). The section shows which zones give each keyword its weight,
  plus insights like "this body theme never reaches a title/H1." It's informational and
  does not affect the score. Needs crawlable text — on a client-rendered page with no
  server HTML it reports that it couldn't measure, rather than guessing. See
  `extract_keywords()` in `scripts/seo_audit.py`.
- **Social & local footprint** (`extract_footprint()`) collects social-profile links from
  the crawled pages and from schema `sameAs`, matches them to known platforms (filtering out
  share/login buttons), and flags whether each is declared in `sameAs` (the knowledge-graph
  signal). For local presence it detects Google Maps / Business Profile (`g.page`,
  `maps.app.goo.gl`) links and extracts self-declared `LocalBusiness`/`Organization`/`Place`
  data (name, phone, address, geo, hours, `aggregateRating`). It is honest about scope: live
  Google Business Profile metrics need the Google Places API and are **not** scraped; the
  ratings shown are self-declared in the site's own schema and labeled as such.
- **Crawling** uses only the Python standard library, so it runs with no install step.
  It reads `robots.txt`, follows the declared sitemap (or `/sitemap.xml`), and falls
  back to following homepage links when the sitemap is thin.
- **Image analysis** (social/OG image and favicon dimensions) uses Pillow if present,
  with a manual PNG/JPEG/GIF header reader as fallback.
- **PDF rendering** uses headless Chrome/Chromium/Edge (`--print-to-pdf`), falling back
  to WeasyPrint if installed. If neither is available the HTML report is still written
  and the script says so — that's not a failure, just report the HTML path.

### Scoring

Each category starts at 100 and loses points per finding by severity (CRITICAL −40,
HIGH −20, MEDIUM −10, LOW −4, INFO 0), floored at 0. The overall score is a weighted
average across categories. Core Web Vitals stays neutral (100) when PageSpeed can't be
measured, so the site isn't punished for something we couldn't observe. The exact
checks, weights, and impact/effort values live in `scripts/seo_audit.py` and are
documented in `references/scoring.md` — read that file if the user wants to understand
or tune the model, add checks, or change category weights.

## Notes

- This audits what's observable from the public site (HTML, headers, robots, sitemap,
  social tags, structured data, and optionally PageSpeed). It does not log into the
  site, submit forms, or use Search Console data — be honest about that scope if asked.
- Re-running after fixes and diffing the `--json` outputs is a good way to show progress.
- If a site blocks the crawler entirely (everything returns status 0 / unreachable),
  say so plainly and suggest checking that the URL is correct and publicly reachable,
  rather than presenting an empty report as if the site were healthy.
