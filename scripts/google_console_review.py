#!/usr/bin/env python3
"""
google_console_review.py: a READ-ONLY review of a site's Google Analytics 4 property and
Google Search Console property, authenticated with a Google Cloud service-account JSON key.

Usage:
    python3 scripts/google_console_review.py --site https://www.example.com --key KEY.json
        [--property properties/123] [--sc-site sc-domain:example.com] [--days 28]
        [--urls-from-sitemap | --url URL ...] [--launched YYYY-MM-DD]
        [--json out.json] [--markdown out.md]

READ-ONLY, ALWAYS
    The access token is requested with exactly two scopes, analytics.readonly and
    webmasters.readonly, so Google itself refuses any write. On top of that, call() only
    sends GET requests, plus POSTs to the four read-only query endpoints (Data API
    runReport and runRealtimeReport, Search Console searchAnalytics/query, and
    urlInspection/index:inspect); any other POST raises before a byte leaves the machine.
    Nothing is created, changed, submitted or deleted: no sitemap is submitted, no
    indexing is requested, no GA setting is touched. The tool reports; a person acts.

Credentials
    The key path comes only from --key or the GOOGLE_APPLICATION_CREDENTIALS environment
    variable. The service account signs an RS256 JWT (the `cryptography` package, imported
    lazily so this module imports without it) and trades it at
    https://oauth2.googleapis.com/token for a one-hour access token. Neither the private
    key nor the access token is ever printed, logged or written to the JSON output, and
    the token is only ever sent to *.googleapis.com hosts. The sitemap itself is fetched
    without credentials. The service account must be added as a user on the GA4 property
    (Viewer is enough) and on the Search Console property (Restricted is enough for
    everything except a few fields).

What it collects
    GA4, Admin API (v1beta; v1alpha where v1beta has no such resource): property,
    dataRetentionSettings, googleSignalsSettings, reportingIdentitySettings, dataStreams
    (with enhancedMeasurementSettings and measurementProtocolSecrets, names only),
    keyEvents, customDimensions, customMetrics, googleAdsLinks, bigQueryLinks,
    attributionSettings, reportingDataAnnotations. A 403 or 404 on any of them is recorded
    as "not readable with this scope/API" with the status code; the run goes on.
    GA4, Data API v1beta: sessions and totalUsers by sessionDefaultChannelGroup, eventCount
    by eventName, screenPageViews by pagePath (top 20), all for the last --days days, plus
    a realtime activeUsers. A 403 SERVICE_DISABLED is reported with Google's enable URL.
    Search Console: sitemaps; searchAnalytics totals and by page, query (top 25), country
    and device for the last --days days and the --days before that (dataState "all");
    URL Inspection for --url URLs, or with --urls-from-sitemap for every sitemap URL
    (capped at 50 per run) plus the other host variant of the homepage (apex for a www
    site). Inspection quota is about 2,000 calls a day and 600 a minute per property; the
    tool sleeps 0.2 s between calls and never comes near either.

Output
    Markdown to stdout (findings first, then tables). --markdown also writes it to a file;
    --json writes the raw collected data plus the findings. Progress goes to stderr.
    Exit codes: 0 done (findings or not), 1 authentication or fatal API failure,
    2 usage error, or no GA4 property / Search Console property could be chosen.
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "0.1.0"

SCOPES = ("https://www.googleapis.com/auth/analytics.readonly "
          "https://www.googleapis.com/auth/webmasters.readonly")
TOKEN_URL = "https://oauth2.googleapis.com/token"
ADMIN_B = "https://analyticsadmin.googleapis.com/v1beta/"
ADMIN_A = "https://analyticsadmin.googleapis.com/v1alpha/"
DATA_B = "https://analyticsdata.googleapis.com/v1beta/"
WEBMASTERS = "https://www.googleapis.com/webmasters/v3/"
INSPECT_URL = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
UA = "google-console-review/" + VERSION

# The only POST endpoints call() will hit. All are read-only queries.
READ_ONLY_POSTS = (
    re.compile(r"^https://analyticsdata\.googleapis\.com/v1beta/properties/\d+:run(Realtime)?Report$"),
    re.compile(r"^https://www\.googleapis\.com/webmasters/v3/sites/[^/]+/searchAnalytics/query$"),
    re.compile(r"^https://searchconsole\.googleapis\.com/v1/urlInspection/index:inspect$"),
)

INSPECT_CAP = 50
INSPECT_DELAY = 0.2          # seconds between URL Inspection calls (tests set it to 0)
NOT_READABLE = "not readable with this scope/API"
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
GOOD_RETENTION = {"FOURTEEN_MONTHS", "TWENTY_SIX_MONTHS", "THIRTY_EIGHT_MONTHS", "FIFTY_MONTHS"}

_KEY = None                  # parsed service-account key (dict); never printed
_TOKEN = None                # (access_token, expiry_epoch); never printed


class AuthError(Exception):
    pass


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# =================================================================================
# Auth and HTTP
# =================================================================================
def load_key(path):
    """Read and validate a service-account key file. Raises SystemExit(2) with a clear
    message on any problem; never echoes the file's contents."""
    global _KEY, _TOKEN
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        raise SystemExit(_usage_error("service-account key file not found: " + path))
    try:
        with open(path, encoding="utf-8") as fh:
            k = json.load(fh)
    except (OSError, ValueError) as e:
        raise SystemExit(_usage_error("cannot read the key file as JSON (%s)" % type(e).__name__))
    if not isinstance(k, dict) or k.get("type") != "service_account" \
            or not k.get("client_email") or not k.get("private_key"):
        raise SystemExit(_usage_error(
            "the key file is not a service-account key (needs type=service_account, "
            "client_email and private_key)"))
    _KEY, _TOKEN = k, None
    return k["client_email"]


def _usage_error(msg):
    log("error: " + msg)
    return 2


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def get_token():
    """Sign a JWT with the service-account key and exchange it for an access token.
    The token is cached for its lifetime and never printed."""
    global _TOKEN
    now = int(time.time())
    if _TOKEN and _TOKEN[1] - 60 > now:
        return _TOKEN[0]
    if _KEY is None:
        raise AuthError("no service-account key loaded")
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        raise AuthError("the 'cryptography' package is needed to sign the service-account "
                        "JWT: python3 -m pip install cryptography")
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": _KEY["client_email"], "scope": SCOPES, "aud": TOKEN_URL,
                              "iat": now, "exp": now + 3600}).encode())
    try:
        pk = serialization.load_pem_private_key(_KEY["private_key"].encode(), None)
    except Exception:
        raise AuthError("the private_key in the key file could not be loaded")
    sig = _b64(pk.sign(header + b"." + claims, padding.PKCS1v15(), hashes.SHA256()))
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": (header + b"." + claims + b"." + sig).decode()}).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            r = json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
            why = "%s: %s" % (body.get("error"), body.get("error_description", ""))
        except Exception:
            why = "HTTP %d" % e.code
        raise AuthError("token exchange refused (" + why.strip() + ")")
    except (urllib.error.URLError, OSError) as e:
        raise AuthError("token endpoint unreachable (%s)" % getattr(e, "reason", e))
    if "access_token" not in r:
        raise AuthError("token endpoint answered without an access_token")
    _TOKEN = (r["access_token"], now + int(r.get("expires_in", 3600)))
    return _TOKEN[0]


def _guard(url, method, auth):
    host = urllib.parse.urlsplit(url).hostname or ""
    if auth and not (host == "googleapis.com" or host.endswith(".googleapis.com")):
        raise ValueError("refusing to send credentials to a non-Google host: " + host)
    if method == "POST" and not any(p.match(url.split("?")[0]) for p in READ_ONLY_POSTS):
        raise ValueError("refusing a POST to an endpoint that is not a read-only query: " + url)
    if method not in ("GET", "POST"):
        raise ValueError("refusing method " + method)


def call(url, body=None, auth=True, raw=False, timeout=30):
    """The single network entry point. Returns (status, payload): payload is parsed JSON
    (or {"raw": text} if the body is not JSON), or the text itself with raw=True. A network
    failure returns status 0 and {"error": {"message": ...}}. Retries twice on 429/5xx."""
    method = "POST" if body is not None else "GET"
    _guard(url, method, auth)
    headers = {"User-Agent": UA, "Accept": "application/json" if not raw else "*/*"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    status, text = 0, ""
    for attempt in range(3):
        if auth:
            headers["Authorization"] = "Bearer " + get_token()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status, text = resp.status, resp.read(10_000_000).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read(1_000_000).decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as e:
            return 0, ("" if raw else {"error": {"message": str(getattr(e, "reason", e))}})
        if status in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(2 * (attempt + 1))
            continue
        break
    if raw:
        return status, text
    try:
        return status, json.loads(text) if text.strip() else {}
    except ValueError:
        return status, {"raw": text[:500]}


def parse_error(status, body):
    """Summarize a Google API error body: status, reason, message, enable URL."""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        err = {"message": (body.get("raw") if isinstance(body, dict) else str(body)) or ""}
    out = {"status": status, "message": (err.get("message") or "").strip(),
           "api_status": err.get("status"), "reason": None, "enable_url": None}
    for d in err.get("details") or []:
        if isinstance(d, dict) and d.get("reason"):
            out["reason"] = d["reason"]
            md = d.get("metadata") or {}
            if md.get("activationUrl"):
                out["enable_url"] = md["activationUrl"]
    if not out["enable_url"]:
        m = re.search(r"https://console\.(?:developers|cloud)\.google\.com/apis/api/[^\s\"']+",
                      out["message"])
        if m:
            out["enable_url"] = m.group(0).rstrip(".,;)")
    if not out["reason"] and "has not been used in project" in out["message"]:
        out["reason"] = "SERVICE_DISABLED"
    return out


# =================================================================================
# Small helpers
# =================================================================================
def host_of(url):
    if "://" not in url:
        url = "https://" + url
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def strip_www(host):
    return host[4:] if host.startswith("www.") else host


def normalize_site(url):
    if "://" not in url:
        url = "https://" + url
    p = urllib.parse.urlsplit(url)
    return "%s://%s%s" % (p.scheme.lower(), (p.hostname or "").lower(), p.path or "/")


def variant_homepage(site_url):
    """The other host variant of the homepage: apex for a www site, www for an apex site."""
    p = urllib.parse.urlsplit(site_url)
    h = (p.hostname or "").lower()
    other = h[4:] if h.startswith("www.") else "www." + h
    return "%s://%s/" % (p.scheme, other)


def parse_time(s):
    if not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def item(status, body, data=None):
    """Record one Admin API resource: ok/status/data, or why it was not readable."""
    if status == 200:
        return {"ok": True, "status": 200, "data": body if data is None else data}
    e = parse_error(status, body)
    note = NOT_READABLE if status in (403, 404) else "error"
    return {"ok": False, "status": status, "note": note, "error": e["message"][:300],
            "reason": e["reason"], "enable_url": e["enable_url"]}


def list_all(url, field, page_size=200, max_pages=10):
    """GET a paginated Google list endpoint; returns (status, body, items)."""
    items, token, status, body = [], None, 0, {}
    for _ in range(max_pages):
        sep = "&" if "?" in url else "?"
        u = url + sep + "pageSize=%d" % page_size + ("&pageToken=" + urllib.parse.quote(token) if token else "")
        status, body = call(u)
        if status != 200:
            return status, body, None
        items.extend(body.get(field) or [])
        token = body.get("nextPageToken")
        if not token:
            break
    return 200, body, items


def admin_list(url, field):
    status, body, items = list_all(url, field)
    return item(status, body, items)


# =================================================================================
# GA4
# =================================================================================
def ga_candidates():
    """List every GA4 property the service account can see, with its web stream URIs.
    Returns (candidates, error_item_or_None)."""
    status, body, summaries = list_all(ADMIN_B + "accountSummaries", "accountSummaries")
    if status != 200:
        return [], item(status, body)
    out = []
    for acct in summaries:
        for ps in acct.get("propertySummaries") or []:
            st, b, streams = list_all(ADMIN_B + ps["property"] + "/dataStreams", "dataStreams")
            uris = [(s.get("webStreamData") or {}).get("defaultUri") for s in (streams or [])]
            out.append({"property": ps["property"], "displayName": ps.get("displayName"),
                        "account": acct.get("account"), "accountName": acct.get("displayName"),
                        "uris": [u for u in uris if u]})
    return out, None


def pick_property(candidates, site_host):
    """The one candidate whose web stream defaultUri host matches the site (www-insensitive),
    or None when there are zero or several matches."""
    want = strip_www(site_host)
    matches = [c for c in candidates if any(strip_www(host_of(u)) == want for u in c["uris"])]
    return matches[0]["property"] if len(matches) == 1 else None


def data_report(prop, body, realtime=False):
    url = DATA_B + prop + (":runRealtimeReport" if realtime else ":runReport")
    status, r = call(url, body)
    if status != 200:
        e = parse_error(status, r)
        return {"ok": False, "status": status, "error": e["message"][:400], "reason": e["reason"],
                "enable_url": e["enable_url"]}
    heads = [h["name"] for h in r.get("dimensionHeaders", [])] + \
            [h["name"] for h in r.get("metricHeaders", [])]

    def flat(row):
        return [v.get("value", "") for v in row.get("dimensionValues", [])] + \
               [v.get("value", "") for v in row.get("metricValues", [])]
    return {"ok": True, "status": 200, "headers": heads,
            "rows": [flat(x) for x in r.get("rows", [])],
            "totals": [flat(x) for x in r.get("totals", [])],
            "rowCount": r.get("rowCount", 0), "metadata": r.get("metadata", {})}


def collect_ga(prop, days):
    P = prop
    ga = {"property_name": P, "items": {}, "streams": [], "data_api": {}}
    it = ga["items"]
    log("GA4: reading Admin API settings for " + P)
    it["property"] = item(*call(ADMIN_B + P))
    it["dataRetentionSettings"] = item(*call(ADMIN_B + P + "/dataRetentionSettings"))
    it["googleSignalsSettings"] = item(*call(ADMIN_A + P + "/googleSignalsSettings"))
    it["reportingIdentitySettings"] = item(*call(ADMIN_A + P + "/reportingIdentitySettings"))
    it["dataStreams"] = admin_list(ADMIN_B + P + "/dataStreams", "dataStreams")
    for s in (it["dataStreams"].get("data") or []) if it["dataStreams"]["ok"] else []:
        st = {"name": s.get("name"), "type": s.get("type"), "displayName": s.get("displayName"),
              "measurementId": (s.get("webStreamData") or {}).get("measurementId"),
              "defaultUri": (s.get("webStreamData") or {}).get("defaultUri")}
        if s.get("type") == "WEB_DATA_STREAM":
            st["enhancedMeasurementSettings"] = item(*call(ADMIN_A + s["name"] + "/enhancedMeasurementSettings"))
        sec = admin_list(ADMIN_B + s["name"] + "/measurementProtocolSecrets", "measurementProtocolSecrets")
        if sec["ok"]:   # names only: the secret values never leave this function
            sec["data"] = [{"name": x.get("name"), "displayName": x.get("displayName")} for x in sec["data"]]
        st["measurementProtocolSecrets"] = sec
        ga["streams"].append(st)
    it["keyEvents"] = admin_list(ADMIN_B + P + "/keyEvents", "keyEvents")
    it["customDimensions"] = admin_list(ADMIN_B + P + "/customDimensions", "customDimensions")
    it["customMetrics"] = admin_list(ADMIN_B + P + "/customMetrics", "customMetrics")
    it["googleAdsLinks"] = admin_list(ADMIN_B + P + "/googleAdsLinks", "googleAdsLinks")
    it["bigQueryLinks"] = admin_list(ADMIN_A + P + "/bigQueryLinks", "bigQueryLinks")
    it["attributionSettings"] = item(*call(ADMIN_A + P + "/attributionSettings"))
    it["reportingDataAnnotations"] = admin_list(ADMIN_A + P + "/reportingDataAnnotations",
                                                "reportingDataAnnotations")

    log("GA4: running Data API reports")
    dr = [{"startDate": "%ddaysAgo" % days, "endDate": "yesterday"}]
    da = ga["data_api"]
    da["date_range"] = dr[0]
    reports = {
        "channels": {"dateRanges": dr, "dimensions": [{"name": "sessionDefaultChannelGroup"}],
                     "metrics": [{"name": "sessions"}, {"name": "totalUsers"}],
                     "metricAggregations": ["TOTAL"],
                     "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}]},
        "events": {"dateRanges": dr, "dimensions": [{"name": "eventName"}],
                   "metrics": [{"name": "eventCount"}], "limit": 50,
                   "orderBys": [{"metric": {"metricName": "eventCount"}, "desc": True}]},
        "pages": {"dateRanges": dr, "dimensions": [{"name": "pagePath"}],
                  "metrics": [{"name": "screenPageViews"}], "limit": 20,
                  "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}]},
    }
    da["reports"] = {}
    for name, body in reports.items():
        rep = data_report(P, body)
        da["reports"][name] = rep
        if not rep["ok"] and (rep["reason"] == "SERVICE_DISABLED" or rep["status"] == 403):
            break   # the same answer would come back for every report
    first = next(iter(da["reports"].values()))
    if first["ok"]:
        da["available"] = True
        da["reports"]["realtime"] = data_report(P, {"metrics": [{"name": "activeUsers"}]}, realtime=True)
    else:
        da["available"] = False
        da["status"] = first["status"]
        da["reason"] = first["reason"]
        da["error"] = first["error"]
        da["enable_url"] = first["enable_url"]
    return ga


# =================================================================================
# Search Console
# =================================================================================
def pick_sc_site(entries, site_url):
    """Prefer sc-domain:<registrable domain> (the shortest sc-domain covering the host),
    then the longest URL-prefix property the site URL starts with. Returns an entry or None."""
    host = host_of(site_url)
    norm = normalize_site(site_url)
    doms = []
    for e in entries:
        su = e.get("siteUrl", "")
        if su.startswith("sc-domain:"):
            d = su[len("sc-domain:"):].lower()
            if host == d or host.endswith("." + d):
                doms.append((len(d), e))
    if doms:
        return min(doms, key=lambda x: x[0])[1]
    prefixes = [(len(e["siteUrl"]), e) for e in entries
                if not e.get("siteUrl", "").startswith("sc-domain:")
                and norm.lower().startswith(e["siteUrl"].lower())]
    return max(prefixes, key=lambda x: x[0])[1] if prefixes else None


def sc_quote(site):
    return urllib.parse.quote(site, safe="")


def date_windows(days, today=None):
    today = today or dt.date.today()
    end = today - dt.timedelta(days=1)
    start = end - dt.timedelta(days=days - 1)
    p_end = start - dt.timedelta(days=1)
    p_start = p_end - dt.timedelta(days=days - 1)
    return (start.isoformat(), end.isoformat()), (p_start.isoformat(), p_end.isoformat())


def search_analytics(sc_site, start, end):
    url = WEBMASTERS + "sites/" + sc_quote(sc_site) + "/searchAnalytics/query"
    out = {"range": [start, end]}
    for label, dims, limit in (("totals", [], 1), ("page", ["page"], 250), ("query", ["query"], 25),
                               ("country", ["country"], 25), ("device", ["device"], 10)):
        body = {"startDate": start, "endDate": end, "dimensions": dims, "rowLimit": limit,
                "dataState": "all"}
        status, r = call(url, body)
        if status != 200:
            e = parse_error(status, r)
            out[label] = {"ok": False, "status": status, "error": e["message"][:300],
                          "reason": e["reason"], "enable_url": e["enable_url"]}
            if status in (401, 403):
                break
            continue
        rows = [{"keys": x.get("keys", []), "clicks": x.get("clicks", 0),
                 "impressions": x.get("impressions", 0), "ctr": x.get("ctr", 0.0),
                 "position": x.get("position", 0.0)} for x in r.get("rows", [])]
        if dims:
            rows.sort(key=lambda x: (-x["impressions"], -x["clicks"]))
        out[label] = {"ok": True, "rows": rows, "metadata": r.get("metadata")}
    return out


def sitemap_urls(paths, site_url, cap=INSPECT_CAP):
    """Collect <loc> URLs from the given sitemaps (following one level of sitemap index).
    Falls back to robots.txt Sitemap: lines, then /sitemap.xml. Fetched without credentials.
    Returns (urls_in_order, total_found, sources_used, errors)."""
    if not paths:
        root = "%s://%s/" % (urllib.parse.urlsplit(site_url).scheme, host_of(site_url))
        st, txt = call(root + "robots.txt", auth=False, raw=True)
        if st == 200:
            paths = [l.split(":", 1)[1].strip() for l in txt.splitlines()
                     if l.lower().startswith("sitemap:")]
        paths = paths or [root + "sitemap.xml"]
    urls, seen, used, errors = [], set(), [], []
    queue, fetched = list(paths), 0
    while queue and fetched < 25:
        sm = queue.pop(0)
        fetched += 1
        st, txt = call(sm, auth=False, raw=True)
        if st != 200:
            errors.append("%s: HTTP %s" % (sm, st))
            continue
        try:
            root_el = ET.fromstring(txt.encode("utf-8"))
        except ET.ParseError as e:
            errors.append("%s: not XML (%s)" % (sm, e))
            continue
        used.append(sm)
        tag = root_el.tag.rsplit("}", 1)[-1]
        locs = [el.text.strip() for el in root_el.iter() if el.tag.rsplit("}", 1)[-1] == "loc" and el.text]
        if tag == "sitemapindex":
            queue.extend(locs)
            continue
        for u in locs:
            if u not in seen:
                seen.add(u)
                urls.append(u)
    return urls[:cap], len(urls), used, errors


def inspect_url(url, sc_site):
    status, r = call(INSPECT_URL, {"inspectionUrl": url, "siteUrl": sc_site, "languageCode": "en-US"})
    if status != 200:
        e = parse_error(status, r)
        return {"url": url, "ok": False, "status": status, "error": e["message"][:300]}
    ir = r.get("inspectionResult") or {}
    ix = ir.get("indexStatusResult") or {}
    rr = ir.get("richResultsResult") or {}
    types = [d.get("richResultType") for d in rr.get("detectedItems") or [] if d.get("richResultType")]
    return {"url": url, "ok": True, "status": 200,
            "verdict": ix.get("verdict"), "coverageState": ix.get("coverageState"),
            "indexingState": ix.get("indexingState"), "lastCrawlTime": ix.get("lastCrawlTime"),
            "pageFetchState": ix.get("pageFetchState"), "googleCanonical": ix.get("googleCanonical"),
            "userCanonical": ix.get("userCanonical"), "robotsTxtState": ix.get("robotsTxtState"),
            "crawledAs": ix.get("crawledAs"), "referringUrls": (ix.get("referringUrls") or [])[:2],
            "sitemap": ix.get("sitemap"),
            "richResults": ({"verdict": rr.get("verdict"), "types": types} if rr else None),
            "link": ir.get("inspectionResultLink")}


def collect_sc(sc_site, permission, site_url, days, inspect_mode, urls, today=None):
    sc = {"site": sc_site, "permission": permission}
    log("Search Console: sitemaps for " + sc_site)
    sc["sitemaps"] = item(*call(WEBMASTERS + "sites/" + sc_quote(sc_site) + "/sitemaps"))
    if sc["sitemaps"]["ok"]:
        sc["sitemaps"]["data"] = sc["sitemaps"]["data"].get("sitemap") or []
    cur, prev = date_windows(days, today)
    log("Search Console: search analytics %s..%s and %s..%s" % (cur + prev))
    sc["analytics"] = {"current": search_analytics(sc_site, *cur),
                       "previous": search_analytics(sc_site, *prev)}

    targets = []
    insp = {"mode": inspect_mode, "cap": INSPECT_CAP, "capped": False}
    if inspect_mode == "sitemap":
        paths = [s.get("path") for s in (sc["sitemaps"].get("data") or []) if s.get("path")] \
            if sc["sitemaps"]["ok"] else []
        found, total, used, errs = sitemap_urls(paths, site_url)
        insp.update({"sitemaps_read": used, "sitemap_errors": errs, "sitemap_url_count": total,
                     "capped": total > INSPECT_CAP})
        targets = [(u, "sitemap") for u in found]
        v = variant_homepage(site_url)
        if v not in found:
            targets.append((v, "variant"))
    elif inspect_mode == "url":
        targets = [(u, "url") for u in urls[:INSPECT_CAP]]
        insp["capped"] = len(urls) > INSPECT_CAP
    insp["results"] = []
    if targets:
        log("Search Console: inspecting %d URL(s)" % len(targets))
    for i, (u, source) in enumerate(targets):
        if i:
            time.sleep(INSPECT_DELAY)
        res = inspect_url(u, sc_site)
        res["source"] = source
        insp["results"].append(res)
    sc["inspection"] = insp
    return sc


# =================================================================================
# Evaluation
# =================================================================================
def finding(severity, title, area, observed, fix):
    return {"severity": severity, "title": title, "category": area, "observed": observed, "fix": fix}


def evaluate(data, launched=None, today=None):
    """Turn collected data into findings. Pure: no network."""
    out = []
    today = today or dt.datetime.now(dt.timezone.utc)
    site_host = data.get("site_host", "")
    ga = data.get("ga") or {}
    it = ga.get("items") or {}

    ret = it.get("dataRetentionSettings") or {}
    if ret.get("ok"):
        v = ret["data"].get("eventDataRetention")
        if v not in GOOD_RETENTION:
            out.append(finding(
                "HIGH", "GA4 event data retention is %s" % (v or "unset"), "analytics",
                "eventDataRetention=%s (userDataRetention=%s)" % (v, ret["data"].get("userDataRetention")),
                "Admin > Data collection and modification > Data retention: set Event data retention "
                "to 14 months. It is not retroactive: explorations can't reach events older than the "
                "current window, so every week at the shorter setting is lost for good."))

    ke = it.get("keyEvents") or {}
    if ke.get("ok"):
        names = [k.get("eventName") for k in ke["data"]]
        if not [n for n in names if n != "purchase"]:
            out.append(finding(
                "HIGH", "No GA4 key event for leads", "analytics",
                "key events: %s" % (", ".join(names) or "none"),
                "Mark the events that mean a lead (booking click, form submit, generate_lead) as key "
                "events in Admin > Data display > Key events. 'purchase' is the default GA creates for "
                "every property and a services site never sends it, so conversion reports stay empty."))

    cd = it.get("customDimensions") or {}
    if cd.get("ok") and not cd["data"]:
        out.append(finding(
            "INFO", "No GA4 custom dimensions registered", "analytics", "customDimensions: none",
            "Event parameters the site sends (e.g. an offer or source parameter) can't be used in "
            "reports or explorations until each is registered in Admin > Data display > Custom "
            "definitions. Register the ones the site's tracking plan relies on; nothing to do if none."))

    gs = it.get("googleSignalsSettings") or {}
    ri = it.get("reportingIdentitySettings") or {}
    if gs.get("ok") and ri.get("ok") and gs["data"].get("state") == "GOOGLE_SIGNALS_ENABLED" \
            and ri["data"].get("reportingIdentity") == "BLENDED":
        out.append(finding(
            "LOW", "Google signals on with Blended reporting identity (thresholding)", "analytics",
            "googleSignals=%s, reportingIdentity=BLENDED" % gs["data"].get("state"),
            "On a low-traffic property Blended identity with Google signals withholds rows "
            "(data thresholds), so small reports come back partial or empty. Switch Admin > Data "
            "display > Reporting identity to Device-based (reversible, applies to past data), or "
            "turn Google signals off if remarketing audiences aren't used."))

    for st in ga.get("streams") or []:
        uri = st.get("defaultUri")
        if uri and site_host and host_of(uri) != site_host:
            out.append(finding(
                "LOW", "GA4 web stream URL differs from the site host", "analytics",
                "%s (%s) defaultUri=%s, site host=%s" % (st.get("displayName"), st.get("measurementId"),
                                                        uri, site_host),
                "Edit the stream URL in Admin > Data streams to https://%s. The tag collects from any "
                "host, so no data is lost; this keeps the stream pointed at the canonical host." % site_host))

    da = ga.get("data_api") or {}
    if da and da.get("available") is False:
        if da.get("reason") == "SERVICE_DISABLED":
            out.append(finding(
                "INFO", "GA4 Data API is not enabled for the service account's project", "analytics",
                "runReport answered 403 SERVICE_DISABLED",
                "Enable the Google Analytics Data API in the key's Cloud project%s, wait a few minutes, "
                "and re-run; the traffic tables are skipped until then."
                % (": " + da["enable_url"] if da.get("enable_url") else "")))
        else:
            out.append(finding(
                "INFO", "GA4 Data API reports could not be read", "analytics",
                "runReport answered %s: %s" % (da.get("status"), (da.get("error") or "")[:160]),
                "Check that the service account is a Viewer on the property and that the Data API is "
                "enabled in its Cloud project."))

    sc = data.get("sc") or {}
    sm = sc.get("sitemaps") or {}
    if sm.get("ok"):
        if not sm["data"]:
            out.append(finding(
                "HIGH", "No sitemap submitted to Search Console", "search-console",
                "sitemaps list is empty for %s" % sc.get("site"),
                "Submit the sitemap in Search Console > Sitemaps (the URL from robots.txt)."))
        for s in sm["data"]:
            last = parse_time(s.get("lastDownloaded"))
            ref = last or parse_time(s.get("lastSubmitted"))
            if ref is None or (today - ref).days > 30:
                age = "never" if last is None else "%d days ago (%s)" % ((today - last).days, s["lastDownloaded"])
                out.append(finding(
                    "HIGH", "Sitemap not fetched by Google in over 30 days", "search-console",
                    "%s last downloaded %s; submitted %s" % (s.get("path"), age, s.get("lastSubmitted")),
                    "Resubmit it in Search Console > Sitemaps, ideally at the canonical host's URL "
                    "(the one robots.txt lists), and check that it answers 200 with XML."))

    insp = (sc.get("inspection") or {}).get("results") or []
    checked = [r for r in insp if r.get("ok") and r.get("source") != "variant"]
    unknown = [r["url"] for r in checked if r.get("coverageState") == "URL is unknown to Google"]
    if unknown:
        out.append(finding(
            "HIGH", "Pages Google has never seen", "search-console",
            "%d of %d inspected URLs are 'URL is unknown to Google': %s"
            % (len(unknown), len(checked), ", ".join(unknown)),
            "Request indexing for each in Search Console URL Inspection, link them from the homepage "
            "or navigation, and make sure the sitemap that lists them has been downloaded since."))
    if launched:
        cutoff = dt.datetime.combine(launched, dt.time(0), tzinfo=dt.timezone.utc)
        stale = [(r["url"], r["lastCrawlTime"]) for r in checked
                 if parse_time(r.get("lastCrawlTime")) and parse_time(r["lastCrawlTime"]) < cutoff]
        if stale:
            out.append(finding(
                "HIGH", "Google's copy predates the launch", "search-console",
                "last crawled before %s: %s" % (launched.isoformat(),
                                                ", ".join("%s (%s)" % (u, t[:10]) for u, t in stale)),
                "Request indexing for these URLs so Google picks up the launched content, titles and "
                "canonicals instead of the pre-launch version."))

    an = sc.get("analytics") or {}
    site_scheme = data.get("site_scheme", "https")
    variants = {}
    for period in ("current", "previous"):
        pg = (an.get(period) or {}).get("page") or {}
        for r in pg.get("rows") or [] if pg.get("ok") else []:
            u = (r.get("keys") or [""])[0]
            p = urllib.parse.urlsplit(u)
            if r.get("impressions", 0) > 0 and ((p.hostname or "").lower() != site_host
                                                or p.scheme != site_scheme):
                variants.setdefault(u, {})[period] = r["impressions"]
    if variants:
        out.append(finding(
            "MEDIUM", "Non-canonical host variants are collecting impressions", "search-console",
            "; ".join("%s: %s" % (u, ", ".join("%s %d imp" % (k, v) for k, v in sorted(d.items())))
                      for u, d in sorted(variants.items())),
            "301 every apex and http URL to https://%s at the edge, keep canonical tags on %s, and "
            "submit the sitemap at that host; impressions consolidate as Google recrawls." % (site_host, site_host)))

    cur = an.get("current") or {}
    tot = cur.get("totals") or {}
    q = cur.get("query") or {}
    if tot.get("ok") and q.get("ok"):
        imps = sum(r["impressions"] for r in tot["rows"])
        if imps > 0 and not q["rows"]:
            out.append(finding(
                "INFO", "No queries visible (anonymized by Search Console)", "search-console",
                "%d impressions in %s..%s but zero query rows" % (imps, *cur.get("range", ["?", "?"])),
                "Search Console hides rare queries for privacy; totals still count them. Not a fault: "
                "read the empty query table as 'too little data', not 'no demand'."))

    out.sort(key=lambda x: SEVERITY_ORDER[x["severity"]])
    return out


# =================================================================================
# Markdown
# =================================================================================
def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        v = "%.2f" % v
    return str(v).replace("|", "\\|").replace("\n", " ")


def md_table(headers, rows):
    if not rows:
        return "_none_\n"
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(_cell(c) for c in r) + " |" for r in rows]
    return "\n".join(lines) + "\n"


def _item_summary(name, it):
    if not it.get("ok"):
        return "%s (HTTP %s)" % (it.get("note"), it.get("status"))
    d = it["data"]
    if name == "property":
        return "%s; timeZone %s; currency %s; industry %s; %s" % (
            d.get("displayName"), d.get("timeZone"), d.get("currencyCode"),
            d.get("industryCategory"), d.get("serviceLevel"))
    if name == "dataRetentionSettings":
        return "events %s; users %s; resetUserDataOnNewActivity %s" % (
            d.get("eventDataRetention"), d.get("userDataRetention"), d.get("resetUserDataOnNewActivity"))
    if name == "googleSignalsSettings":
        return "%s; consent %s" % (d.get("state"), d.get("consent"))
    if name == "reportingIdentitySettings":
        return str(d.get("reportingIdentity"))
    if name == "attributionSettings":
        return "%s; acquisition lookback %s; other lookback %s" % (
            d.get("reportingAttributionModel"),
            (d.get("acquisitionConversionEventLookbackWindow") or "").rsplit("_WINDOW_", 1)[-1],
            (d.get("otherConversionEventLookbackWindow") or "").rsplit("_WINDOW_", 1)[-1])
    if name == "keyEvents":
        return ", ".join("%s (%s)" % (k.get("eventName"), k.get("countingMethod")) for k in d) or "none"
    if name == "customDimensions":
        return ", ".join("%s [%s]" % (k.get("parameterName"), k.get("scope")) for k in d) or "none"
    if name == "customMetrics":
        return ", ".join("%s [%s]" % (k.get("parameterName"), k.get("measurementUnit")) for k in d) or "none"
    if name == "googleAdsLinks":
        return ", ".join("Ads %s (personalization %s)" % (k.get("customerId"), k.get("adsPersonalizationEnabled"))
                         for k in d) or "none"
    if name == "bigQueryLinks":
        return ", ".join("%s (daily %s, streaming %s)" % (k.get("project"), k.get("dailyExportEnabled"),
                                                          k.get("streamingExportEnabled")) for k in d) or "none"
    if name == "reportingDataAnnotations":
        return ", ".join("%s %s" % ((k.get("annotationDate") or k.get("annotationDateRange") or ""), k.get("title"))
                         for k in d) or "none"
    if name == "dataStreams":
        return "%d stream(s)" % len(d)
    return json.dumps(d)[:120]


def render_markdown(data, findings):
    L = []
    w = L.append
    w("# Google console review: %s\n" % data["site"])
    w("Generated %s by google-console-review %s as `%s`. Read-only: scopes analytics.readonly "
      "and webmasters.readonly.\n" % (data["generated"], VERSION, data.get("client_email")))
    w("## Findings\n")
    w(md_table(["Severity", "Title", "Observed", "Fix"],
               [[f["severity"], f["title"], f["observed"], f["fix"]] for f in findings]))

    ga = data.get("ga") or {}
    w("## GA4 property %s\n" % (ga.get("property_name") or ""))
    if ga.get("error"):
        w("GA4 not reviewed: %s\n" % ga["error"])
    else:
        w(md_table(["Setting", "Value"], [[k, _item_summary(k, v)] for k, v in ga.get("items", {}).items()]))
        rows = []
        for s in ga.get("streams", []):
            em = s.get("enhancedMeasurementSettings") or {}
            if em.get("ok"):
                on = [k[:-7] for k, v in em["data"].items() if k.endswith("Enabled") and v is True]
                emtxt = ", ".join(on) or "all off"
            else:
                emtxt = "%s (HTTP %s)" % (em.get("note", "n/a"), em.get("status", ""))
            mp = s.get("measurementProtocolSecrets") or {}
            mptxt = (", ".join(x.get("displayName") or x.get("name") for x in mp["data"]) or "none") \
                if mp.get("ok") else "%s (HTTP %s)" % (mp.get("note"), mp.get("status"))
            rows.append([s.get("displayName"), s.get("measurementId"), s.get("defaultUri"), emtxt, mptxt])
        w("### Data streams\n")
        w(md_table(["Stream", "Measurement ID", "defaultUri", "Enhanced measurement on", "MP secrets"], rows))
        da = ga.get("data_api") or {}
        w("### Data API (%s to %s)\n" % ((da.get("date_range") or {}).get("startDate"),
                                         (da.get("date_range") or {}).get("endDate")))
        if da.get("available"):
            for name, rep in da.get("reports", {}).items():
                w("**%s**%s\n" % (name, " (subject to thresholding)" if rep.get("metadata", {}).get(
                    "subjectToThresholding") else ""))
                if rep.get("ok"):
                    rows = list(rep["rows"])
                    if rep.get("totals") and rep["rows"]:
                        rows.append(["TOTAL"] + rep["totals"][0][1:])
                    w(md_table(rep["headers"], rows))
                else:
                    w("HTTP %s: %s\n" % (rep.get("status"), rep.get("error")))
        else:
            w("Not available: HTTP %s %s. %s\n" % (da.get("status"), da.get("reason") or "",
                                                   ("Enable: " + da["enable_url"]) if da.get("enable_url")
                                                   else (da.get("error") or "")))

    sc = data.get("sc") or {}
    w("## Search Console %s\n" % (sc.get("site") or ""))
    if sc.get("error"):
        w("Search Console not reviewed: %s\n" % sc["error"])
        return "\n".join(L)
    w("Permission level: %s\n" % sc.get("permission"))
    sm = sc.get("sitemaps") or {}
    w("### Sitemaps\n")
    if sm.get("ok"):
        w(md_table(["Path", "Last submitted", "Last downloaded", "Pending", "Errors", "Warnings", "Submitted"],
                   [[s.get("path"), s.get("lastSubmitted"), s.get("lastDownloaded"), s.get("isPending"),
                     s.get("errors"), s.get("warnings"),
                     ", ".join("%s %s" % (c.get("type"), c.get("submitted")) for c in s.get("contents") or [])]
                    for s in sm["data"]]))
    else:
        w("%s (HTTP %s)\n" % (sm.get("note"), sm.get("status")))

    an = sc.get("analytics") or {}
    cur, prev = an.get("current") or {}, an.get("previous") or {}
    w("### Search performance (dataState all)\n")

    def tot(p):
        t = p.get("totals") or {}
        if not t.get("ok"):
            return None
        return t["rows"][0] if t["rows"] else {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    tc, tp = tot(cur), tot(prev)
    if tc is None:
        w("Not readable: HTTP %s %s\n" % ((cur.get("totals") or {}).get("status"),
                                         (cur.get("totals") or {}).get("error")))
    else:
        rows = []
        for m in ("clicks", "impressions", "ctr", "position"):
            a, b = tc.get(m, 0), (tp or {}).get(m, 0)
            fmt = (lambda x: "%.1f%%" % (100 * x)) if m == "ctr" else \
                  (lambda x: "%.1f" % x) if m == "position" else (lambda x: "%d" % x)
            rows.append([m, fmt(a), fmt(b), ("%+.1f" % (a - b) if m == "position" else
                                              "%+.1f pt" % (100 * (a - b)) if m == "ctr" else "%+d" % (a - b))])
        w(md_table(["Metric", "%s..%s" % tuple(cur.get("range", ["", ""])),
                    "%s..%s" % tuple(prev.get("range", ["", ""])), "Change"], rows))
        for label, n in (("page", 25), ("query", 25), ("country", 25), ("device", 10)):
            blk = cur.get(label) or {}
            w("**Top %s, current period**\n" % label)
            if blk.get("ok"):
                w(md_table([label, "Clicks", "Impressions", "CTR", "Position"],
                           [[r["keys"][0] if r["keys"] else "", r["clicks"], r["impressions"],
                             "%.1f%%" % (100 * r["ctr"]), "%.1f" % r["position"]] for r in blk["rows"][:n]]))
            else:
                w("HTTP %s %s\n" % (blk.get("status"), blk.get("error")))

    insp = sc.get("inspection") or {}
    w("### URL inspection\n")
    if insp.get("mode") is None:
        w("Skipped (pass --urls-from-sitemap or --url).\n")
    else:
        if insp.get("mode") == "sitemap":
            w("Sitemaps read: %s. %d URL(s) found%s.%s\n" % (
                ", ".join(insp.get("sitemaps_read") or []) or "none", insp.get("sitemap_url_count", 0),
                "; capped at %d" % INSPECT_CAP if insp.get("capped") else "",
                (" Errors: " + "; ".join(insp["sitemap_errors"])) if insp.get("sitemap_errors") else ""))
        elif insp.get("capped"):
            w("Capped at %d URLs.\n" % INSPECT_CAP)
        rows = []
        for r in insp.get("results", []):
            if not r.get("ok"):
                rows.append([r["url"], "HTTP %s" % r.get("status"), r.get("error"), "", "", "", "", "", "", ""])
                continue
            rich = r.get("richResults")
            rows.append([r["url"] + (" (variant)" if r.get("source") == "variant" else ""), r.get("verdict"),
                         r.get("coverageState"), r.get("indexingState"), (r.get("lastCrawlTime") or "")[:16],
                         r.get("pageFetchState"),
                         r.get("googleCanonical") if r.get("googleCanonical") == r.get("userCanonical")
                         else "G: %s / U: %s" % (r.get("googleCanonical"), r.get("userCanonical")),
                         ", ".join(x for x in (r.get("robotsTxtState"), r.get("crawledAs")) if x),
                         ", ".join(r.get("referringUrls") or []),
                         ("%s %s" % (rich.get("verdict"), ",".join(rich.get("types") or []))) if rich else ""])
        w(md_table(["URL", "Verdict", "Coverage", "Indexing", "Last crawl", "Fetch", "Canonical",
                    "Robots, crawled as", "Referring", "Rich results"], rows))
    return "\n".join(L)


# =================================================================================
# CLI
# =================================================================================
def build_parser():
    ap = argparse.ArgumentParser(
        prog="google_console_review.py",
        description="Read-only review of a site's GA4 and Search Console properties with a "
                    "service-account key. Nothing is ever written to Google.")
    ap.add_argument("--site", required=True, help="the site, e.g. https://www.example.com")
    ap.add_argument("--key", help="service-account JSON key (else $GOOGLE_APPLICATION_CREDENTIALS)")
    ap.add_argument("--property", help="GA4 property, e.g. properties/123 (default: auto-detect)")
    ap.add_argument("--sc-site", help="Search Console property, e.g. sc-domain:example.com (default: auto)")
    ap.add_argument("--days", type=int, default=28, help="reporting window in days (default 28)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--urls-from-sitemap", action="store_true",
                   help="inspect every sitemap URL (cap %d) plus the homepage host variant" % INSPECT_CAP)
    g.add_argument("--url", nargs="+", default=[], help="inspect these URLs")
    ap.add_argument("--launched", help="YYYY-MM-DD; flag URLs last crawled before this date")
    ap.add_argument("--json", help="write raw data plus findings to this file")
    ap.add_argument("--markdown", help="also write the Markdown report to this file")
    ap.add_argument("--version", action="version", version="google-console-review " + VERSION)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not 1 <= args.days <= 480:
        return _usage_error("--days must be between 1 and 480")
    launched = None
    if args.launched:
        try:
            launched = dt.date.fromisoformat(args.launched)
        except ValueError:
            return _usage_error("--launched must be YYYY-MM-DD")
    key_path = args.key or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not key_path:
        return _usage_error("no service-account key: pass --key PATH or set GOOGLE_APPLICATION_CREDENTIALS")
    try:
        client_email = load_key(key_path)
    except SystemExit as e:
        return e.code

    site_url = normalize_site(args.site)
    site_host = host_of(site_url)
    data = {"tool": "google-console-review", "version": VERSION,
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "site": site_url, "site_host": site_host, "site_scheme": urllib.parse.urlsplit(site_url).scheme,
            "client_email": client_email, "days": args.days, "launched": args.launched}
    log("authenticating as %s (read-only scopes)" % client_email)
    try:
        get_token()
    except AuthError as e:
        log("error: " + str(e))
        return 1

    # GA4 property
    prop = args.property
    ga_error = None
    if prop and not re.fullmatch(r"properties/\d+", prop):
        prop = "properties/" + prop if prop.isdigit() else prop
    if not prop:
        cands, err = ga_candidates()
        data["ga_candidates"] = cands
        if err:
            ga_error = "account summaries %s (HTTP %s): %s" % (err["note"], err["status"], err.get("error"))
            log("GA4: " + ga_error)
        else:
            prop = pick_property(cands, site_host)
            if not prop:
                log("error: no single GA4 property has a web stream for %s. Candidates:" % strip_www(site_host))
                for c in cands:
                    log("  %s  %s  %s" % (c["property"], c["displayName"], ", ".join(c["uris"]) or "(no web stream)"))
                log("pass --property properties/N")
                return 2
            log("GA4: selected %s" % prop)

    # Search Console property
    st, body = call(WEBMASTERS + "sites")
    entries = body.get("siteEntry") or [] if st == 200 else []
    sc_error = None
    sc_site, permission = args.sc_site, None
    if st != 200:
        e = parse_error(st, body)
        sc_error = "sites list HTTP %s: %s" % (st, e["message"][:200])
        log("Search Console: " + sc_error)
    if not sc_site and not sc_error:
        chosen = pick_sc_site(entries, site_url)
        if not chosen:
            log("error: no Search Console property covers %s. Visible properties:" % site_url)
            for e in entries:
                log("  %s  (%s)" % (e.get("siteUrl"), e.get("permissionLevel")))
            log("pass --sc-site")
            return 2
        sc_site = chosen["siteUrl"]
    if sc_site:
        permission = next((e.get("permissionLevel") for e in entries if e.get("siteUrl") == sc_site),
                          "not in this account's site list")
        log("Search Console: %s, permission %s" % (sc_site, permission))
        sc_error = None

    data["ga"] = collect_ga(prop, args.days) if prop else {"error": ga_error or "no property"}
    if sc_site:
        mode = "sitemap" if args.urls_from_sitemap else ("url" if args.url else None)
        data["sc"] = collect_sc(sc_site, permission, site_url, args.days, mode, args.url)
    else:
        data["sc"] = {"error": sc_error}

    findings = evaluate(data, launched)
    md = render_markdown(data, findings)
    print(md)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
        log("wrote " + args.markdown)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(dict(data, findings=findings), fh, indent=1)
        log("wrote " + args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
