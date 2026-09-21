# What the script cannot see: the manual pre-index checklist

The analyzer reads public HTTP responses. A site can score well and still be unready to
launch, because the failures that cost the most tend to live behind a login, a payment, a
dashboard, or a judgment call. When the user is about to open a site to indexing, launch,
or migrate, run the script first and then work through this list. Say plainly which items
you did and which you could not do.

Each item names the failure it exists to catch. They come from real audits.

## Before you trust any finding

1. **Audit what is deployed, not what is on disk.** Compare the live files with the repo
   head (`git fetch`, then diff a few served files against the working tree). A stale
   checkout makes every source-level explanation wrong, and deploying from one can undo
   fixes that are already live.
2. **Check the tool's own version.** `python3 scripts/seo_audit.py --version`. An installed
   copy of a skill can lag its repository by months with nothing to show it.
3. **Re-run when the site is changing under you.** If other people or agents are shipping
   fixes during the audit, a finding can be true at 2pm and false at 3pm. Re-verify the
   headline findings immediately before you report them.
4. **Verify every "broken" claim with a GET.** Some servers answer HEAD with 404 or 405 on
   routes that serve GET correctly. A HEAD-based checker then reports hundreds of broken
   images that load fine. Anything reported broken gets one real GET before it goes in a report.
5. **When two checks disagree, resolve it before reporting either.** Conflicting results are
   usually one real bug seen from two sides.
6. **Read the coverage table.** A score describes the templates that were sampled. If the
   biggest template shows 0 audited, the score says nothing about most of the site.

## Crawler and AI access

7. **Confirm edge blocks in the CDN dashboard.** The script tests by user-agent from an
   ordinary IP. It cannot see IP-verified allow rules, and it cannot change the setting.
   Many CDNs now block AI crawlers by default on new zones; the owner has to flip it.
8. **Hunt for copy that contradicts the access policy.** Sites that once blocked indexing or
   AI crawlers often say so in public copy, robots.txt comments, FAQs, or sales pages. After
   the policy changes, those sentences become false. After it changes back, they become
   self-sabotage. Grep for "index", "crawler", "noindex", "blocked".
9. **Ask the question an assistant will be asked.** Read the site's own About, Methodology,
   FAQ, Terms and pricing pages and answer "Is this company legitimate?" using only that
   text. Note what helps (named people, a legal entity, an address, refund and removal
   policies, disclosed conflicts, dated pages) and what hurts (hidden criteria with no worked
   example, no denominator behind a "selective" claim, zero third-party corroboration,
   circular evidence, unsourced claims in schema).
10. **Look for geo- or visitor-personalized hubs.** A hub page that shows each visitor a
    different subset shows a crawler one subset forever. The script sees only its own
    vantage point; compare against a request from another region or read the code.

## Rendering, validation and search consoles

11. Run Google's Rich Results Test and URL Inspection (live test) on one page per template.
    The script parses JSON-LD; it does not know what Google will accept.
12. Confirm Search Console and Bing Webmaster verification, submit the sitemap to both, and
    set up IndexNow if the site changes often. DNS and file verification are invisible to a crawl.
13. Get real Core Web Vitals. Run `--pagespeed` with a `PAGESPEED_API_KEY`, or Lighthouse
    locally on one page per template in mobile mode. Static checks find the causes; only a
    browser measures LCP, INP and CLS. New sites have no field data, so make sure a
    real-user beacon is actually able to run (no CSP block) before traffic arrives.

## The money path

14. **Run a test transaction end to end** on an isolated environment: qualify, pay, receive,
    cancel, refund. Check the database rows and the emails, not just the success page.
15. **Test dependency failure, not only success.** If a step depends on a third-party API
    (an archive, a registry, a places lookup), make it fail and watch what the customer sees.
    A flaky free API that gates checkout turns every outage into lost sales.
16. Confirm payment webhooks deliver 2xx in the provider dashboard. A browser redirect that
    works can hide a webhook that has been failing for weeks.
17. **Check analytics can see revenue.** The purchase event needs value, currency and a
    transaction id, and purchases made inside logged-in areas need server-side events.
18. **Check nothing secret rides in a URL that analytics can read.** Resume links, magic
    links and offer tokens in query strings are sent to analytics as the page location.
19. Read the promises in the checkout and account copy against what the code does:
    "instantly", "every January", "refunded in full". Every unenforced promise is a finding.

## Trust and hygiene

20. Domain auto-renew and a valid card on file (the script reports the expiry date only).
21. Typo and hyphen variants of the domain: registered defensively or at least known.
22. Transactional email: sending domain matches the site, SPF/DKIM/DMARC aligned, DMARC has
    a reporting address, and the contact addresses in templates are the current ones.
23. Private files and internal docs excluded from the deploy (the script probes a few
    well-known paths; a repo-root-is-the-site setup needs a full allowlist review).
24. A consent mechanism if the site will get EU/UK traffic and runs analytics.
25. Uptime and error alerting wired to a channel someone reads, and a database backup taken
    before traffic arrives.

## How to report

Lead with what blocks launch. Separate what you verified yourself from what a tool or a
delegate reported. State what you could not check and why. A high score next to an
unchecked money path is not a green light, so never present it as one.
