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

A second review, on 2026-11-24, of a 20-URL web app that had been live for eleven weeks,
changed the method. The crawl scored 79 and its findings were about meta descriptions and
security headers. The consoles said the GA4 property had received nothing for 60 days
because the tag had been removed from the site on purpose, that Google had indexed one
URL of twenty, and that the site had been shown 22 times in 16 months. The first version
of the script printed empty tables for the first fact and no finding at all for the other
two. The sections "Which stage is the site at" and "Recommended configuration" below, and
most of the checks added in 0.2.0, come from that review.

## Access

Ask the owner for a service account with read access and the JSON key file. Keep the key
outside any repository. Setting it up takes the owner about ten minutes, and each step
fails in its own recognisable way:

1. In Google Cloud, create (or pick) a project, enable the three APIs listed below, create
   a service account, and download a JSON key for it.
2. In GA4: Admin > Property access management > add the service account's `client_email`
   as Viewer.
3. In Search Console: open the property > Settings > Users and permissions > Add user >
   the same `client_email`, Restricted. This is a separate grant from step 2 and from
   anything in Cloud IAM. A key that was only ever added to GA4 authenticates, gets HTTP
   200 from `sites.list`, and sees an empty list. The script reports that as an access
   finding and carries on with the half it can read.
4. Hand over the key as a file path (`--key`) or in an environment variable that holds the
   JSON text or its base64 (`--key-env VAR`; `GOOGLE_SERVICE_ACCOUNT_JSON`,
   `GSC_SERVICE_ACCOUNT_JSON`, `GA_SERVICE_ACCOUNT_JSON`, `GA_READER_KEY_JSON` and
   `GA_READER_KEY_B64` are picked up without the flag). Cloud sessions and CI carry keys as
   environment variables; do not write one to a file inside the repository to satisfy
   `--key`, and never ask for it to be pasted into a chat.
5. In a sandbox with an outbound allowlist, these hosts must be reachable:
   `oauth2.googleapis.com`, `analyticsadmin.googleapis.com`,
   `analyticsdata.googleapis.com`, `www.googleapis.com`, `searchconsole.googleapis.com`,
   and the site. To prove in a browser that a tag fires you also need
   `www.googletagmanager.com` and `www.google-analytics.com`; without them, say that the
   firing check was not possible. A partial check still works when they are blocked and
   you have the site's source: build it, serve the build locally, load it in headless
   Chromium with requests for the production host answered from the local server, and read
   `window.dataLayer` plus the list of attempted requests to Google. That proves the loader
   is requested on the production host and not on localhost, and shows the exact
   `page_location` and parameters each event would carry, without one hit reaching the
   property.

First check, before any analysis: confirm the variable or file parses as a service-account
key without printing it (key names, `client_email`, `private_key` present with a PEM
header), then list what the account can see. Report the `client_email` back to the owner,
because that is the string they have to paste into both consoles.

The account needs:

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

The review never needs more than Viewer and Restricted. If the owner wants the settings
applied for them, that takes Editor on the GA4 property and the `analytics.edit` scope, a
different token from the one this script requests, and it is a separate, explicit request.

Stay read-only unless the owner asks for a change in writing. Every write (retention,
key events, custom dimensions, sitemap submission) is a settings change on a live
business property. Report the exact API call or dashboard path that would make the change
and let the owner do it or approve it.

## Which stage is the site at

Read the two consoles in this order and stop at the first "no". Everything after that
point is real but secondary, and the report should say so.

| # | Question | Where the answer is | If the answer is no |
|---|---|---|---|
| 1 | Is anything being measured? | GA4 Data API events for the window; the 14-month history; the tag check on the homepage; a browser's network panel | Nothing in GA4 can be trusted or tuned. Find out why before proposing a fix: read the site's commit history, privacy policy and tests. A tag removed on purpose is the owner's decision to reverse. |
| 2 | Are the pages indexed? | URL inspection of every sitemap URL | On-page findings describe pages Google has not read. The work is indexing: internal links, a real 404 for unknown URLs, requesting indexing by hand, links from other sites. |
| 3 | Are they shown? | Search analytics totals for the window and for 16 months | Under roughly one impression a day there are no queries to optimise for. The constraint is authority and demand, not titles. |
| 4 | Are they clicked? | Search analytics by query and by page | Now titles and descriptions matter: pages with many impressions and a CTR below the position's norm, queries at positions 5 to 20, two pages competing for one query. |
| 5 | Do the visits convert? | GA4 key events by landing page and by source/medium | Conversion work: landing page by landing page, starting with the ones that carry the traffic. |

A site at stage 1 or 2 with a crawl score of 90 has a hygiene score of 90 and a search
presence of zero. Lead with the stage.

Two things from the same review that are easy to get wrong:

- URL inspection is not stable for pages Google has not crawled. Two runs 25 minutes apart
  moved three URLs from "Discovered - currently not indexed" to "URL is unknown to Google"
  with no change to the site. Count the two states together as "not crawled" and do not
  report movement between them as progress or regression.
- A handful of impressions at position 1 on a new site are people searching the brand
  name. They do not mean the site ranks.

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

## Recommended configuration

What "set up to capture as much as possible" means in practice. The script checks the
items marked (checked); the rest have to be read in the UI or in the site's code.

GA4 property:

- Event data retention at 14 months (checked).
- Key events that are the site's actual funnel, each one something the site sends: the
  account or lead (`sign_up`, `generate_lead`), the first use of the product, the start of
  checkout (`begin_checkout`) and the sale (`purchase`). A key event that no event has
  matched in 14 months is reported (checked); `close_convert_lead` and `qualify_lead` are
  left behind by GA's lead-generation setup flow.
- A custom dimension for every event parameter anyone will want to split by (checked for
  none at all). Not retroactive, 50 event-scoped per property.
- Enhanced measurement on, with one exception: on a single-page app that sends its own
  `page_view`, turn off "Page changes based on browser history events" (the
  `pageChangesEnabled` field in `enhancedMeasurementSettings`), or every in-app navigation
  is counted twice. Pick one mechanism, never both.
- Internal traffic: either an IP rule in the stream's tag settings or a `traffic_type`
  parameter the site sets for staff and test accounts, and then the Internal Traffic data
  filter switched from Testing to Active. It ships in Testing, where it excludes nothing.
- Unwanted referrals: the payment processor's checkout host and any external sign-in host.
  Without it, the return from checkout starts a new session credited to the processor and
  the purchase is attributed to it.
- Cross-domain measurement if the business runs more than one domain.
- The Search Console link (Admin > Product links). Not readable through the API.
- Data redaction for email addresses and for query parameters that carry user text.
- Google signals off, or reporting identity Device-based, on a low-traffic property
  (checked): otherwise thresholding hides rows.
- A Measurement Protocol secret if anything happens server-side that the browser never
  sees, such as subscription renewals.
- An annotation at every launch and at both ends of any gap in collection.

The tag on the site (read the code; the crawl only sees whether an ID is in the HTML):

- Loads on the production host only (checked from the hostnames GA4 has recorded).
- Never sends an email address, a name or free text the visitor typed, in an event
  parameter or in the page URL. Look at what the site puts in query strings and hashes:
  sign-in links carry tokens in the hash, and "return to" parameters carry whole URLs.
- Sends `user_id` as an opaque account ID if cross-device counts matter.
- Is described accurately in the privacy policy. Check both directions: a policy that says
  there is no third-party analytics while a tag fires, and a tag removed while the policy
  still describes it.

Campaign links (checked when GA4 has data): every link the business controls (emails,
social posts, scheduled reels, partner listings) needs `utm_source`, `utm_medium` and
`utm_campaign`, and `utm_medium` has to be one GA4's channel rules know. A scheduling
tool's default of `utm_medium=reel` put 19% of one site's sessions in Unassigned. Use
`social`, `email`, `cpc`, `affiliate`, `referral`, `display` or `video`, and put the detail
in `utm_content`. Untagged email links arrive as Direct.

Search Console property:

- A domain property (`sc-domain:`), verified by DNS, so every host and scheme is covered
  and verification survives a redesign. Keep any URL-prefix property too. The account is
  shown whichever kinds it was added to; one URL-prefix property is a finding worth a line.
- The sitemap submitted at the canonical host, with `lastDownloaded` recent (checked).
- The GA4 property associated (Settings > Associations).
- Not in the API, so ask the owner to look: Manual actions, Security issues, Removals, the
  Crawl stats report (host status, response codes, crawl requests per day), and the Page
  indexing report's totals. After a domain move, the Change of Address tool on the old
  property.
- "Request indexing" in URL Inspection for the handful of pages that matter most. It is
  manual and limited to roughly ten a day.
- Bing Webmaster Tools can import the verified site from Search Console in one step, and
  IndexNow covers Bing, Yandex and others; it does nothing for Google.

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
   URL inspection's `referringUrls` add a fourth host question: a referrer on another
   domain (an old brand name, a platform subdomain, a sister site whose sitemap lists these
   URLs) is reported, and that host deserves one request to see whether it redirects here.
3. **Measurement.** The crawl's analytics detection and CSP check (does the tag load, is a
   beacon blocked), Lighthouse's network hosts (does it actually fire), GA4's key events
   and custom dimensions (is it kept and split), the Data API's event list (did it
   arrive). A site can pass every crawl check and still record nothing useful.

4. **Where visitors land.** GA4's landing pages against the sitemap. A sign-in URL with a
   long `next=` parameter as the second-largest landing page means emailed links are
   dropping logged-out readers on a login form: a conversion finding the crawl cannot
   make. Landing pages with sessions that are noindexed or missing from the sitemap, and
   sitemap pages with no sessions at all, are both worth a line.
5. **Who the visitors are.** Channel and device together. A property that is 75% Direct on
   desktop with an engagement rate under 10% on that slice is mostly the owner, uptime
   checks and bots; say so before anyone reads the session count as an audience.

## Reporting

Lead with the stage (see "Which stage is the site at"), then the console facts that
override the crawl score: "Google has not fetched the
sitemap since March and holds the pre-launch version of the two pages it knows" says more
than any on-page finding. Then the GA4 settings that lose data with every passing day
(retention, key events, custom dimensions), each with the UI path and the API call. Then the
rest. Put every unverified item in its own list with the exact error that stopped you
(`403 SERVICE_DISABLED` with the enable URL, `404` on a resource the API lacks, a scope the
key did not have). Never present an unchanged setting as checked because the API did not
return it.
