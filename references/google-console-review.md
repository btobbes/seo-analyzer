# Reviewing Google Analytics 4 and Search Console alongside the crawl

The crawl audits what the public site serves. Half of what decides whether a launch shows
up in search and gets measured lives in two Google consoles the crawl cannot see: Search
Console (has Google fetched the sitemap, which URLs does it know, what does it rank for) and
GA4 (is the property set up so the events the site sends are kept, counted and split the
way the owner expects). This reference is the method for reviewing both, read-only, with a
service account. `scripts/google_console_review.py` automates the collection and the
mechanical checks; this file is what to look at, what each answer means, and what the APIs
refuse to tell you.

It came out of the 2026-10-12 review of a six-page consultancy site two days after its
relaunch. The crawl scored 90. Search Console showed Google had not fetched the sitemap in
seven months and had never heard of four of the six URLs; GA4 still had two-month
retention and no key event for the booking the whole site exists to produce. Neither
fact was visible from the crawl, and both mattered more than anything the crawl found.

## Access

Ask the owner for a service account with read access and the JSON key file. Keep the key
outside any repository. The account needs:

- GA4: Viewer (or Analyst) on the property, at account or property level. Scope
  `https://www.googleapis.com/auth/analytics.readonly`.
- Search Console: Full or Restricted user on the property. Scope
  `https://www.googleapis.com/auth/webmasters.readonly`.
- The Cloud project that owns the service account must have the **Google Analytics Admin
  API**, the **Google Analytics Data API** and the **Google Search Console API** enabled.
  A 403 with reason `SERVICE_DISABLED` names the project and gives the enable URL; the
  owner has to click it. The Data API being off is common: the Admin API answers, so the
  configuration review works, but no traffic can be read.

Authentication without Google's client libraries: sign a JWT (RS256, `iss` = the service
account email, `scope` = the space-separated scopes, `aud` =
`https://oauth2.googleapis.com/token`, one-hour expiry) with the key's private key using
the `cryptography` package, exchange it at the token endpoint with
`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, and send the access token as a
Bearer header. The script does this. Never print the key or the token; never log request
bodies that contain them.

Stay read-only unless the owner asks for a change in writing. Every write (retention,
key events, custom dimensions, sitemap submission) is a settings change on a live
business property. Report the exact API call or dashboard path that would make the change
and let the owner do it or approve it.

## Search Console: what to read and what it means

Property. `GET webmasters/v3/sites` lists the properties the account can see with its
permission level. A domain property (`sc-domain:example.com`) covers every host and
scheme; URL-prefix properties (`https://www.example.com/`) cover one. If the account sees
only one kind, say so: a 403 on the other does not tell you whether it exists.

Sitemaps. `GET sites/{site}/sitemaps`. The fields that matter are `lastSubmitted`,
`lastDownloaded`, `isPending`, `errors`, `warnings` and `contents[].submitted`. The
`indexed` count in this API is always 0 and means nothing. A `lastDownloaded` older than a
few weeks on a site that just changed means Google is not refreshing its view; after a
relaunch, resubmitting the same path from the Sitemaps page is the fastest trigger. Check
the live sitemap's `<lastmod>` values against the real change history while you are here:
hand-maintained dates that predate the launch tell Google nothing changed.

Search analytics. `POST sites/{site}/searchAnalytics/query` with `dataState: all` so the
last two days are included (they are marked incomplete). Pull totals, then by `page`,
`query`, `country` and `device`, for the current window and the one before it. Read:

- Zero rows by query while impressions exist means every query fell under the
  anonymization threshold. On a small site that is normal; it is not a data problem.
- Impressions on the apex or http variants of URLs mean Google holds them as separate
  pages. Pair this with the crawl's host-variant check: if the variants answer 200
  instead of redirecting, the redirect is the fix, and the canonical tag is only holding
  the line.
- Pages sum to more than the property total. The two are aggregated differently; do not
  report the discrepancy as an error.
- Position on a handful of impressions is noise. Say how many impressions sit behind a
  position before quoting it.

URL inspection. `POST searchconsole/v1/urlInspection/index:inspect` with `inspectionUrl`
and `siteUrl`, one call per URL, roughly 2,000 a day per property and 600 a minute. Inspect
every sitemap URL on a small site, one per template on a large one, plus the apex variant
of the homepage. Fields:

- `verdict` PASS with `coverageState` "Submitted and indexed" is the only good answer.
  "URL is unknown to Google" means never crawled: on a relaunch that is normal for new
  URLs and is the queue you ask the owner to push through "Request indexing" (no public
  API does this; the Indexing API is only for job postings and livestream videos).
- `lastCrawlTime` before the launch date means Google holds the old version. New titles,
  descriptions and structured data are not in the index yet, whatever the crawl found.
- `googleCanonical` different from `userCanonical` is a real problem; the same is fine.
- `crawledAs` is MOBILE on nearly every site now. The mobile usability verdict is
  `VERDICT_UNSPECIFIED` everywhere since Google retired that report; do not call it a gap.
- `richResultsResult` appears only when Google has seen eligible markup on its last crawl.
  Absent after a relaunch just means the new markup has not been fetched.
- `referringUrls` tells you how Google found the page. A sitemap URL or a third-party
  site are both fine; an old host variant is a clue the migration is incomplete.

Compare the inspection date with what the crawl saw live. "Page with redirect" recorded
weeks ago on an apex that answers 200 today is a conflict to resolve (usually the redirect
was lost in a hosting move), not two findings.

## GA4: what to read and what it means

Use the Admin API (v1beta, with v1alpha for the few resources only it exposes) to read the
configuration, and the Data API only to confirm events arrive. The reviewable settings,
and what a bad value costs:

| Setting | Endpoint | Watch for |
|---|---|---|
| Event data retention | `dataRetentionSettings` | `TWO_MONTHS` is the default and silently destroys exploration data; `FOURTEEN_MONTHS` is the free maximum. Launch-day data expires two months after launch unless changed. Standard reports are unaffected, explorations and funnel comparisons are. |
| Key events | `keyEvents` | The event the business runs on (a booking, a lead, a purchase) must be a key event or it never becomes a conversion, cannot be imported into Google Ads and is not in the conversion reports. A `purchase` key event nothing sends is a leftover. |
| Custom dimensions and metrics | `customDimensions`, `customMetrics` | Event parameters the site sends (which consultant, which offer, which path) are invisible in reports until registered. They do not apply retroactively: every day without them is a day that cannot be split later. Compare with the site's own tracking documentation. |
| Web stream | `dataStreams` | `defaultUri` should be the canonical host. `enhancedMeasurementSettings` shows which automatic events are on; a custom event that reuses a built-in name (`video_start`) only double-counts if both fire. |
| Google signals and reporting identity | `googleSignalsSettings`; identity is not readable, check the UI | Signals on plus blended identity means thresholding: rows withheld in small reports. On a low-traffic site that hides most of the data. Device-based identity, or signals off, stops it. |
| Google Ads, BigQuery, other links | `googleAdsLinks`, `bigQueryLinks`, `firebaseLinks`, `displayVideo360AdvertiserLinks`, `searchAds360Links` | A Google Ads link without a key event imports nothing. A BigQuery link is the only full export. |
| Attribution | `attributionSettings` | Data-driven with 30/90-day lookbacks is the default and fine. |
| Annotations | v1alpha `reportingDataAnnotations` | A launch with no annotation is a future question nobody can answer from the chart. |
| Measurement Protocol secrets | `measurementProtocolSecrets` (names only) | Needed for server-side events (a booking webhook); report whether one exists. |
| Property basics | `properties/{id}` | Time zone, currency, industry category. The category only affects benchmarks. |

Things the Admin API does not expose, so say "check in the UI" rather than "fine":
data filters (internal and developer traffic, and whether they are Testing or Active),
the Search Console link, unwanted referrals, cross-domain measurement, reporting identity,
session timeout and the engaged-session timer. Change history
(`searchChangeHistoryEvents`) and access bindings need scopes beyond read-only
(`analytics.edit`, `analytics.manage.users.readonly`), so the review usually cannot say
who changed what.

Data API. `runReport` for sessions and users by `sessionDefaultChannelGroup`, `eventCount`
by `eventName`, and `screenPageViews` by `pagePath`; `runRealtimeReport` for
`activeUsers`. The event list is the proof that the site's custom events exist: a
documented event missing from `eventName` after a few days of traffic is a tracking bug
or a consent gate, and the review has to say which it could not tell. When the Data API is
disabled, the whole "are events arriving" question is unverified; the GA4 Realtime report
in the UI is the owner's fallback, and the review should ask for a screenshot of it.

## Putting the two together with the crawl

Three joins tend to matter:

1. **Hosts.** The crawl's host-variant check (apex, http, trailing slash), Search
   Console's impressions by page (which hosts carry them), the inspection of the apex, and
   the GA4 stream's `defaultUri` plus the tag's host gate in the site's own analytics
   script. When the apex answers 200 and the tag accepts it, GA4 splits traffic by
   `hostName` as well.
2. **Freshness.** Sitemap `<lastmod>` from the crawl, `lastDownloaded` from Search Console,
   `lastCrawlTime` per URL, and the site's deploy history. If all of them predate the
   launch, nothing Google shows reflects the new site yet, and the on-page findings from
   the crawl are describing a version Google has not seen.
3. **Measurement.** The crawl's analytics detection and CSP check (does the tag load, is a
   beacon blocked), Lighthouse's network hosts (does it actually fire), GA4's key events
   and custom dimensions (is it kept and split), the Data API's event list (did it
   arrive). A site can pass every crawl check and still record nothing useful.

## Reporting

Lead with the console facts that override the crawl score: "Google has not fetched the
sitemap since March and holds the pre-launch version of the two pages it knows" says more
than any on-page finding. Then the GA4 settings that lose data with every passing day
(retention, key events, custom dimensions), each with the UI path and the API call. Then the
rest. Put every unverified item in its own list with the exact error that stopped you
(`403 SERVICE_DISABLED` with the enable URL, `404` on a resource the API lacks, a scope the
key did not have). Never present an unchanged setting as checked because the API did not
return it.
