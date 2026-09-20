# The Centurion Awards — first GSC sitemap upload

Brief for Mr. Centurion (Sales / IT / Legal). Live review of https://centurionawards.com on 20 Sep 2026 from the seo-analyzer repo. Analyzer run: 14 pages (named templates plus sitemap fill), HTML/JSON/PDF in `reports/centurion-awards-2026-09-20/`. Overall score 79/100. Crawlability 30/100. Confidence medium (PageSpeed Insights returned 429, so Core Web Vitals were not measured).

This is a technical readiness review. It does not predict rankings.

## Executive summary

The site is not ready for a first Google Search Console sitemap upload.

Googlebot already gets HTTP 200 and the sitemap is valid (480 URLs, `application/xml`, referenced from robots.txt). Google will almost certainly accept the file. That is not the problem. Every public response, including `/sitemap.xml` and `/robots.txt`, still sends `X-Robots-Tag: noindex` (CEN-027). If you submit now, GSC will list the sitemap as processed and then mark the URLs "Excluded by ‘noindex’ tag". That is under-indexing, not a fetch failure. You would spend the first crawl teaching Google these URLs are intentionally withheld, then need a recrawl after Legal clears the flip.

Robots.txt already allows crawling (`Allow: /`, only `/admin`, `/portal`, `/api` disallowed) and points at the sitemap. The operator file also Allows named AI crawlers. That file is not the live access rule. Edge still returns HTTP 403 to GPTBot, ClaudeBot, CCBot, Bytespider, and Amazonbot (CEN-001). PerplexityBot, OAI-SearchBot, and Google-Extended returned 200 in this probe. Do not tell Sales that ChatGPT or Claude can read the site.

Public copy uses info@centurionawards.com. No mr-centurion.com string appeared on the crawled HTML. Host aliases (www, http, and the old host in worker code) 301 to https://centurionawards.com.

## What must change for indexing to work

Three layers have to agree. Right now they do not.

1. HTTP header. Site-wide `X-Robots-Tag: noindex` is set in `_headers` (static assets) and mirrored in the Worker `withSecurityHeaders()` path. CEN-027 is the ticket that removes it. Both copies must flip in the same deploy. Leaving one in place keeps the whole site out of the index.
2. HTML meta robots. Almost every marketing and winner template has no meta robots tag, so the header is the only noindex. `/apply` is the exception: it has `<meta name="robots" content="noindex">` on purpose (checkout funnel). After the header flip, `/apply` stays out of the index. That is the correct leftover. Confirm it in staging before you ship.
3. robots.txt and sitemap. Already in the "allow crawl, here is the map" shape. Do not add `Disallow: /` as a substitute for the header flip. Do not noindex the sitemap response once you want GSC to use it.

Googlebot is not blocked at the edge. The header is the only thing stopping indexation of the URLs you will submit.

## Analyzer evidence (14 pages)

Seeded templates: `/`, `/business`, `/apply`, `/faq`, `/winners`, `/nominate`, dated award `/2025/us/az/flagstaff/tours/freaky-foot-tours`, evergreen `/us/az/flagstaff/bars/beaver-street-brewery`, market hub `/2025/us/az/flagstaff/`, `/blog/`, `/blog/how-centurion-winners-are-verified`, `/about`, `/methodology`. Sitemap fill added `/terms`.

Every one of those 14 returned HTTP 200 and `X-Robots-Tag: noindex`. `/apply` also has meta `noindex`. The dated Freaky Foot page canonicals to the evergreen `/us/az/flagstaff/tours/freaky-foot-tours` (intentional; that is why dated award URLs are omitted from the sitemap). Titles, descriptions, H1s, `lang="en"`, viewport, HTTPS, HSTS, Organization/WebSite/FAQ/LocalBusiness schema, GA4, and `llms.txt` are present on the templates that should have them. `/apply` has no JSON-LD (acceptable for a noindex funnel).

Host variants: `http://`, `http://www`, and `https://www` 301 to `https://centurionawards.com`. Nonsense URL returns a real HTTP 404. TLS is Google Trust Services WE1, notBefore 15 Aug 2026, notAfter 13 Nov 2026 (about 53 days of runway at review time). Internal-link sample: 30 checked, 0 broken, 1 redirecting.

## Sitemap vs live URLs

`/sitemap.xml` has 480 `<loc>` entries. 27 sampled URLs (all statics plus a random mix of markets and evergreen winners) returned 200. Classification:

- 1 home, 9 other statics (`/winners`, `/methodology`, `/faq`, `/nominate`, `/business`, `/about`, `/terms`, `/privacy`, `/accessibility`)
- 6 blog (index + 5 posts)
- 1 year hub (`/2025/`)
- 28 state hubs, 52 city hubs, 130 category hubs under `/2025/`
- 253 evergreen business overviews under `/us/...`
- 0 dated `/2025/.../<business>` award pages
- `/apply` is absent

That omission of dated award pages is deliberate in `src/sitemap.js`: those pages `rel=canonical` to the evergreen overview, and a sitemap must not list the non-canonical twin. `/apply` is also deliberately absent (funnel, meta noindex). Both are correct for a first submit.

`lastmod` is present on 258 of 480 URLs (blog posts and evergreen winners). Market hubs and statics have none. That will not make Google reject the file.

Slash style is mixed on purpose: static docs are extensionless with no trailing slash; `/blog/` and `/2025/.../` hubs keep the slash; evergreen business URLs have no slash. The sitemap locs match the live 200s we sampled. The problem is the opposite: the non-canonical slash twin also returns 200 (see P1).

## P0 — blockers for the first submit

Do not upload until these are done. None of them are copy changes.

P0-1. Legal clearance, then remove site-wide `X-Robots-Tag: noindex`.
Evidence: header present on `/`, `/sitemap.xml`, `/robots.txt`, `/llms.txt`, and every audited HTML page. Source: `_headers` comment "TEMPORARY until indexing is turned on; CEN-027 removes it" and the Worker mirror.
Fix: Legal signs off. IT deletes the header from `_headers` and `src/worker.js` in one deploy. Recheck `curl -sI https://centurionawards.com/` and `curl -sI https://centurionawards.com/sitemap.xml` until `x-robots-tag` is gone (or is `index, follow` if you set an explicit allow). `/apply` must still show meta `noindex`.

P0-2. Do not submit the sitemap while the header is still noindex.
Evidence: GSC processes a valid sitemap and then applies per-URL robots. All 480 locs inherit the site-wide header.
Fix: Wait for the flip. Submitting now will not usually produce a red "couldn't fetch sitemap" error. It will produce hundreds of "Excluded by noindex" rows and a wasted first crawl.

P0-3. Keep robots.txt in the current allow shape through the flip.
Evidence: live `/robots.txt` has `User-agent: *` / `Allow: /`, disallows only `/admin` `/portal` `/api`, and `Sitemap: https://centurionawards.com/sitemap.xml`. Googlebot UA received HTTP 200 on `/`.
Fix: No change required. Do not "protect" the launch by adding `Disallow: /`. The header is the hold; robots is already the post-flip policy.

P0-4. Register the GSC property on the canonical host only.
Evidence: `https://www.centurionawards.com` and both http variants 301 to `https://centurionawards.com/`.
Fix: URL-prefix property `https://centurionawards.com`. Do not make www the primary property. Submit `https://centurionawards.com/sitemap.xml` after P0-1.

What would actually make Google reject or ignore the file: invalid XML (it is valid), sitemap URL 4xx/5xx (it is 200), sitemap blocked in robots.txt (it is not), locs on another host (they are not), or more than 50,000 URLs / 50 MB (480 URLs, ~54 KB). None of those are true today. The live failure mode is noindex exclusion, not a malformed-sitemap reject.

## P1 — high-impact before or at the flip

Do these in the same release train as the header removal if you can. They will not stop GSC from accepting the file. They will waste crawl budget or split signals once Google is allowed to index.

P1-1. Trailing-slash duplicates return 200.
Evidence: `https://centurionawards.com/2025/us/az/flagstaff` (no slash) is HTTP 200 with canonical to `.../flagstaff/`. `https://centurionawards.com/us/az/flagstaff/bars/beaver-street-brewery/` (extra slash) is HTTP 200 with canonical to the no-slash evergreen URL. Static docs already 301 (`/winners/` → `/winners`, `/apply/` → `/apply`). Market and winner paths do not.
Fix: 301 the non-canonical slash twin to the sitemap loc. Canonical tags are already correct; a 200 on both still lets Google keep two URLs around.

P1-2. Confirm `/apply` stays noindex after the header flip.
Evidence: `/apply` is HTTP 200, in the public nav, has meta `noindex`, is omitted from the sitemap. Source comment: checkout funnel, `/business` is the indexable owner page.
Fix: Staging check after CEN-027. If someone strips all robots directives in a "make us indexable" sweep, `/apply` will become a second owner-intent URL competing with `/business`.

P1-3. Winner-template title and description length.
Evidence: Freaky Foot dated page title 89 characters, description 234. Beaver Street evergreen title 77, description 203. Analyzer flags both as too long for SERP display.
Fix: Keep the city + category + year facts, shorten for ~50–60 / ~150–160 characters. Do not add rank claims. Processing-fee language on `/apply` and `/business` is already in the right register; do not change it to "application fee" or "membership".

P1-4. Homepage image missing alt.
Evidence: analyzer, `/` — 1/1 `<img>` without alt.
Fix: Descriptive alt on that image (the award mark or hero, whichever it is).

P1-5. Heading outline skips H3 site-wide.
Evidence: `/business`, `/faq`, `/winners`, winner pages, blog, about, methodology, terms all report `h2` then `h4` (or `h1` then `h3` on `/apply`). This is a shared layout pattern, not a one-off.
Fix: Renumber the chrome so the page outline is H1 → H2 → H3. Low SEO weight, cheap if the heading mixin is one place.

P1-6. LocalBusiness schema missing opening hours.
Evidence: site-wide finding on crawled LocalBusiness JSON-LD.
Fix: Add `openingHours` / `openingHoursSpecification` when you have them. Do not invent hours.

P1-7. TLS expires 13 Nov 2026.
Evidence: certificate notAfter 13 Nov 2026, ~53 days at review. Analyzer did not flag it (threshold is 30 days).
Fix: Confirm Google Trust Services auto-renew on the zone. Not a sitemap blocker. Do not let it lapse in the first index window.

P1-8. CEN-001 remains open for AI answers, not for Google.
Evidence: HTTP 403 for GPTBot, ClaudeBot, CCBot, Bytespider, Amazonbot. HTTP 200 for PerplexityBot, OAI-SearchBot, Google-Extended, Googlebot, bingbot. Operator robots.txt Allows the blocked agents and warns that a platform prefix and edge rules sit in front of the file. This probe did not see a Cloudflare-managed robots prefix on the GPTBot fetch of `/robots.txt` (that file was 200 and was the repo file). The 403 is the page fetch, not robots.
Fix: IT: turn off Cloudflare AI scraper / Bot Fight blocks for the agents you want quoted. Sales: do not claim ChatGPT or Claude can cite winner pages until a GPTBot/ClaudeBot GET of `/` returns 200. This is not a prerequisite for the GSC upload.

## P2 — nice-to-haves after the first submit

P2-1. `lastmod` on market hubs and statics. Helps recrawl after edits. Will not change whether Google accepts the file.

P2-2. PageSpeed / CrUX. This run was rate-limited (PSI 429). Re-run with `PAGESPEED_API_KEY` after the flip if you want lab LCP/INP/CLS. Do not brief a performance number you have not measured.

P2-3. Market hub images are JPEG/PNG only (Flagstaff hub: 5 raster images, no WebP/AVIF in `src`). Optional weight win.

P2-4. Meta descriptions without a CTA verb on `/business`, `/apply`, winner templates, `/about`. The analyzer wants verbs like "Explore" or "Learn". Optional. Do not turn them into rank promises.

P2-5. HSTS is `max-age=31536000` without `includeSubDomains` or `preload`. Fine for a first submit. Add those only if you intend to lock every subdomain to HTTPS.

P2-6. `/contact` is HTTP 404. Trust pages exist (`/about`, `/privacy`, `/terms`) and the public inbox is `mailto:info@centurionawards.com`. A dedicated contact URL is optional.

P2-7. `/llms.txt` and `/llms-full.txt` already exist. Emerging convention. They do not move the score and they do not help while GPTBot/ClaudeBot are 403.

P2-8. Default OG image on most statics (`/assets/og-default.png`). Winner pages that have a real cover (Freaky Foot, Beaver Street) already override it. Fine for launch.

## Ready-for-first-sitemap-upload checklist

Indexation flip (Legal + IT)

- [ ] Legal clears public indexing (CEN-027).
- [ ] `X-Robots-Tag: noindex` removed from `_headers` and Worker security headers in one deploy.
- [ ] `curl -sI https://centurionawards.com/` has no noindex.
- [ ] `curl -sI https://centurionawards.com/sitemap.xml` has no noindex.
- [ ] `curl -sI https://centurionawards.com/winners` has no noindex.
- [ ] `curl -sL https://centurionawards.com/apply | grep -i robots` still shows meta `noindex`.
- [ ] robots.txt still Allows `/` and still lists `Sitemap: https://centurionawards.com/sitemap.xml`.
- [ ] `/admin`, `/portal`, `/api` still Disallow.

Sitemap hygiene (IT)

- [ ] `/sitemap.xml` is 200, `application/xml`, well-formed `<urlset>`.
- [ ] Locs are `https://centurionawards.com/...` only (no www, no http).
- [ ] `/apply` still absent.
- [ ] Dated `/2025/.../<slug>` award pages still absent (canonicals stay on evergreen).
- [ ] Spot-check 20 locs still 200 after the flip.
- [ ] Non-canonical slash twins 301 to the sitemap loc (P1-1), or you accept the risk.

GSC (whoever owns Search Console)

- [ ] Property is URL-prefix `https://centurionawards.com` (not www, not a Domain property pointed at the old host).
- [ ] Ownership verified.
- [ ] Submit `https://centurionawards.com/sitemap.xml` only after the header flip is live.
- [ ] Expect "Success" on the sitemap and a delay before "Indexed". Do not treat "Discovered / not indexed" in week one as a content failure.
- [ ] Optional: URL Inspection on `/`, `/winners`, one market hub, one evergreen winner. Do not bulk-request the whole directory.

Sales / public claims

- [ ] Public inbox stays info@centurionawards.com. No mr-centurion.com on the site, in the sitemap, or in GSC notes that might leak.
- [ ] Fee language stays "processing fee" for publishing an award already earned. Evaluation is free.
- [ ] No ranking or "we'll get you on Google" promises in launch copy.
- [ ] Do not claim AI assistants can read or cite the site while GPTBot/ClaudeBot still 403 (CEN-001).

## Replay

```bash
python3 scripts/seo_audit.py https://centurionawards.com \
  --max-pages 14 \
  --include-pages \
    /business,/apply,/faq,/winners,/nominate,\
    /2025/us/az/flagstaff/tours/freaky-foot-tours,\
    /us/az/flagstaff/bars/beaver-street-brewery,\
    /2025/us/az/flagstaff/,/blog/,\
    /blog/how-centurion-winners-are-verified,/about,/methodology \
  --json --out reports/centurion-awards-2026-09-20
```

`--pagespeed` needs a `PAGESPEED_API_KEY` or it will 429 again. After CEN-027 ships, the HIGH "Noindexed via X-Robots-Tag" and "Sitemap served with X-Robots-Tag: noindex" findings should disappear. `/apply` should still show meta noindex. The AI-edge HIGH finding stays until CEN-001 is actually open, regardless of robots.txt.
