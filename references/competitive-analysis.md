# Competitive analysis: method

How to find out who is actually taking a site's position, what they do well, and what
the site does to itself. Written after the Freaky Foot Tours vs Flagstaff Underground
analysis of 2026-09-22 (summarised at the end), where the question arrived as "we are
losing position to this rival" and the answer was mostly "the rival sells something you
say no to, and your own domain estate is diluting you".

Two scripts support it: `scripts/find_competitors.py` (who occupies the results for
the client's queries) and `scripts/seo_compare.py` (side by side audits, absent
features, unique findings). The audit itself is `scripts/seo_audit.py`. Where a
source can and cannot be fetched, and what data is worth buying, is in
`competitor-sources.md`.

## 0. Frame the question before fetching anything

"Losing position" can mean five different things, and they have different fixes:
Google organic rank on specific queries, the map pack, a marketplace's category order
(TripAdvisor, Amazon, Yelp), share of bookings or sales, or a feeling. Ask which, since
when, and on which searches. If the owner has Search Console, Business Profile
performance and booking data by channel, ask for exports first; they answer the
question directly and cost nothing. Everything below is what to do while waiting, or
when there is no first-party data.

Write down what the environment cannot observe before starting. On 2026-09-22 that was
Google itself (search, Maps, the local pack), so every position in the analysis was a
proxy from Brave or the WebSearch tool, and the report said so in its first paragraph.

## 1. Build the query set

Twenty queries is enough for a local or niche business. Cover five classes:

- Head terms: the category plus the place ("flagstaff ghost tour", "flagstaff tours").
- Product classes the rivals name: "true crime tour", "family friendly ghost tour",
  "underground tour", "cemetery tour". Take these from the rivals' own navigation.
- Informational queries the business could own: "are there tunnels under flagstaff",
  "things to do in flagstaff at night".
- Venue and landmark queries: "weatherford hotel ghost tour".
- Brand queries for the client and for each rival.

Put them in a file, one per line. The same list feeds the rank tracker later, so the
week-zero baseline and the day-90 comparison use identical queries.

## 2. Identify the competitive set

Run `find_competitors.py` with the query file, the client's domain and every domain the
client owns (satellites, legacy domains). It tallies which domains occupy the results,
weights them by position (1/position summed over appearances), and classifies each as
business, marketplace, social or video, directory or news, or own. Read three tables:

- The business shortlist: the rivals to audit. Take three to five. A domain that
  appears on one query at position 12 is not a competitor.
- The own-domains table: the client's satellites and legacy sites showing up is a
  finding in itself (see lens 5).
- The marketplaces and social/video tables: these are channels and SERP features, not
  rivals. A TripAdvisor category page outranking everyone means the fight is partly on
  TripAdvisor.

Add rivals the proxies miss: the top operators in the marketplace category listing
(TripAdvisor's "Ghost & Vampire Tours in Flagstaff" list, for a tour company), the DMO
directory, and whoever the owner named. Then classify each rival as local and direct
(same town, same product), national franchise (one brand in fifty cities, one template
page per city), or aggregator. The counter-moves differ: a franchise page is thin and
beatable on depth; a local rival with a unique venue is beatable only on product.

When a paid SERP export exists (SerpApi or DataForSEO JSON), feed it to the script with
`--serp-json` instead of relying on Brave. That is the only way to see the local pack.

## 3. Audit like with like

Run `seo_audit.py` on the client and on each rival with the same flags (`--max-pages 40
--sweep 1100 --json` was the 2026-09-22 setting). Then run `seo_compare.py` on the JSONs.

Read the comparison in this order and ignore the overall scores. An 11-page static site
with no robots.txt, no sitemap and no structured data scored 81 against a 1,062-URL
site's 84: the small site scored well by having nothing to check. Page counts that
differ by more than about 3x, or a site with no sitemap, make the headline number
meaningless. The comparison script prints that caveat when it applies. What matters is
the absent-features matrix (what one site has and the other lacks), the findings unique
to each side, and each site's coverage.

## 4. The seven lenses

Each lens collects different evidence, and each has a trap. In the 2026-09-22 run each
lens was one agent; run them in parallel, then verify (section 5).

**1. On-page and offer.** For each rival product, find the client's closest page and
compare title, H1, price shown on the page, schedule shown, meeting point, inclusions
(what the guest gets that the other does not), age policy, duration, call-to-action
wording, trust signals, FAQ, word count. Also compare above-the-fold clarity: one hook
and six cards can beat 3,000 words and 43 headings. The trap is judging pages instead
of products: the rival won on product-intent queries because it sold inside access,
public per-seat departures and a bookable family tour, with pages a third the length.

**2. Distribution and reviews.** For both sides, every marketplace and directory: present
or not, rating, review count, price, badges, URL. Then review velocity, not totals: count
dated reviews in a fixed window (the last 90 days) on the same platform for both. A
young rival with 55 reviews and four a week is a different threat from one with 55 and
none since spring. Compare the client's direct price with its marketplace prices; a
cheaper direct price nobody mentions is a conversion fix waiting to happen. The trap is
platforms that refuse automated requests (Viator, GetYourGuide, Groupon, Yelp): label
figures from mirrors and snippets as such.

**3. Links, press and partners.** Who links to or names each side: local press, the
DMO, event calendars, venue partners, student papers, affiliate scrapers. Which venues
the rival has locked up (a hotel whose own page says "all bookings through X" is a
followed link plus a ranking on the venue query). Verify every marketing claim ("as seen
on Ghost Adventures") against the outlet's own page; a TV episode about a venue is not
coverage of the operator who later tours it. The trap is calling a discovered sample a
backlink index; say it is a sample.

**4. Search visibility by query class.** For the query set, the ordered domains per
query from every observable engine, with each engine named and the proxy caveat
repeated. Look for which classes each side wins and why: exact-match brand names win
their own terms, unique products win product classes, freshness and OTA amplification
win the rest. Note SERP features (video, news, discussion clusters, FAQ boxes) the
client could occupy. Check whether a rival is mid-migration (new site, old URLs 404 but
still indexed, no sitemap): that is a dated window, not a permanent state.

**5. The client's own architecture.** Before blaming the rival, look for self-inflicted
dilution: off-mission or affiliate pages as a share of the sitemap (the audit's
affiliate-share check), satellite and legacy domains still live (the owned-domains
check), several owned URLs chasing one query (title concentration in the keyword
section), contradictory prices, ages, counts and entity names across pages
(review-count consistency), placeholder pages, and stale third-party listings. On
2026-09-22 this lens explained as much as the rival did.

**6. Conversion path.** Where the header call-to-action goes on every template, whether
price and schedule are on the page, how many clicks to a date picker, whether products
that already win their query can be booked (a cemetery page that ranked first and said
"private groups only, inquire" was losing bookings to a $30 public walk), and whether
the client's own third-party listings advertise the gap the rival sells ("we will not
be entering any buildings"). If the checkout is a client-rendered widget and the browser
is unavailable, say it was not tested and ask the owner to complete one mobile booking.

**7. Data sources.** What the owner already has (Search Console, Business Profile
performance, marketplace supplier dashboards, booking data) and what to buy to see what
the environment could not (a local rank tracker, a geo-grid tool, a raw SERP archive,
one month of a keyword and backlink index, a review scraper). Verify every price on the
vendor's page the same day and date it. The stack that fits a small operator is about
$240 in month one and $50 a month after; the list is in `competitor-sources.md`.

## 5. Verify adversarially

Every claim that would change the strategy gets one skeptic with fresh requests, told to
refute it. In the 2026-09-22 run, 18 claims went through this: 8 came back confirmed, 10
confirmed with corrections (a wrong URL, a count off by one, a claim that rested on a
snippet), none refuted. The corrections mattered: "TripWorks widget URLs return 404"
became "render client-side and were not observed", and "the rival's site was rebuilt
this week" gained its evidence (a Last-Modified header and four dead Wix URLs).

Then run a completeness critic over the draft: what angle nobody checked, which claim
rests on one unverified source, which recommendation does not follow from the evidence,
which number disagrees between lenses. The critic caught a misattributed book, the two
national operators nobody had analysed, a product name that borrowed the rival's trade
name, and a timeline that put October launches in November.

Rules that fall out of this: label every proxy as a proxy; date every number; never
carry a rival's marketing claim into the report as fact; never compare overall audit
scores across sites; separate what was verified from what the tool reported.

## 6. Synthesize

The report has seven parts, in this order, and each item traces to evidence:

1. The verdict in three paragraphs: what the rival is and is not beating the client on,
   what it does that the client refuses to do, and what the client does to itself.
2. What the rival does well, and the counter-move that does it better, not equal. Each
   row: the practice, the evidence, the move, impact, effort. "Better" means the client's
   existing assets (an author, a book, a venue relationship, a bigger review base) applied
   to the rival's idea.
3. Self-inflicted problems that could explain the loss on their own, ranked by likely
   effect and cost.
4. Conversion fixes on the booking path.
5. Where the client already wins and should not spend.
6. The other rivals the question did not mention, with the instruction that the rank
   tracker must include them.
7. Data to buy, a 90-day plan with dates that respect the seasonal peak, weekly and
   monthly numbers, decision rules for day 30, 60 and 90, and the questions only the
   owner can answer.

Timing belongs in the verdict when it changes the order of work: a rival mid-migration,
a seasonal peak six weeks away, an anniversary event.

## 7. Report honesty

- Say what was not observed, first, in the method section and again beside any number
  that depends on it.
- Keep the tool's known false positives out of the rival's list of faults; several
  audit findings (module scripts, AVIF through `<picture>`, a LocalBusiness subtype)
  were tool defects until 2.1.0 fixed them, and new ones will appear.
- Quote the rival's copy exactly when it is the evidence ("we will not be entering any
  historic haunts"); paraphrase everything else.
- Give the owner the files: both audits, the comparison, the competitor table and the
  brief, under `reports/` with the date in the name, so the day-90 comparison has a
  baseline.

## Checklist

1. Define "losing position"; request Search Console, Business Profile and booking exports.
2. Write the 20-query set in five classes.
3. `find_competitors.py`; add marketplace and DMO rivals; classify; pick three to five.
4. Audit client and rivals with identical flags; `seo_compare.py`; ignore overall scores.
5. Seven lenses in parallel; every number dated and sourced; proxies labelled.
6. One refuter per strategy-changing claim; a completeness critic on the draft.
7. Report in the seven-part order; timing in the verdict; files in `reports/`.
8. Set the rank tracker up with the same query set and every shortlisted rival, and
   schedule the day-90 re-audit of the client and the rivals.

## Worked example, 2026-09-22

Freaky Foot Tours (freakyfoottours.com, 1,062 sitemap URLs, audit 84/100) against
Flagstaff Underground (flagstaffunderground.com, 11 pages, audit 81/100, no robots.txt,
no sitemap, no JSON-LD, no analytics, rebuilt from Wix the same day with no redirects).

What the rival did well: sold basement and hotel access the client's own FAQ and
listings said "no" to; ran public per-seat afternoon and early-evening departures the
client kept private; priced its family tour in the title; held a followed link from a
hotel's own site naming it the only booking channel; showed six cards and one verb above
the fold. What the client did to itself: 641 of 1,062 sitemap URLs were an affiliate
catalog for other cities; eight tour microsites and a 2021 legacy site were live and
self-canonical; six owned URLs on three domains chased the head query and the money page's
title lacked it; prices, ages, counts and even the legal entity name disagreed across
pages. Where the client already won: 363 TripAdvisor reviews to 55 and 43 new to 9 since
July, six DMO listings to one, the press record, the informational tunnels page every
journalist cited.

The recommended order: consolidate the domain and fix the booking path in the first
week, ship the product gaps before the October peak, then buy a $240 measurement stack
and re-audit both sites after 60 days, when the rival's migration will have settled.
