#!/usr/bin/env python3
"""
google_console_review.py: a READ-ONLY review of a site's Google Analytics 4 property and
Google Search Console property, authenticated with a Google Cloud service-account JSON key.

Usage:
    python3 scripts/google_console_review.py --site https://www.example.com --key KEY.json
        [--property properties/123] [--sc-site sc-domain:example.com] [--days 28]
        [--urls-from-sitemap | --url URL ...] [--inspect-max N] [--inspect-workers N]
        [--launched YYYY-MM-DD] [--brand TERM ...] [--audit-json seo-report.json]
        [--key-env VAR] [--json out.json] [--markdown out.md]

READ-ONLY, ALWAYS
    The access token is requested with exactly two scopes, analytics.readonly and
    webmasters.readonly, so Google itself refuses any write. On top of that, call() only
    sends GET requests, plus POSTs to the four read-only query endpoints (Data API
    runReport and runRealtimeReport, Search Console searchAnalytics/query, and
    urlInspection/index:inspect); any other POST raises before a byte leaves the machine.
    Nothing is created, changed, submitted or deleted: no sitemap is submitted, no
    indexing is requested, no GA setting is touched. The tool reports; a person acts.

Credentials
    The key comes from --key PATH, --key-env VAR (an environment variable holding the key
    JSON itself, or its base64, which is how cloud sessions and CI usually carry it), the
    GOOGLE_APPLICATION_CREDENTIALS path, or the first of the INLINE_KEY_VARS that is set.
    The service account signs an RS256 JWT (the `cryptography` package, imported
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
    Then the last 14 months: sessions by date, so an empty recent window can be dated
    ("last event 2026-09-25"), plus events, hostnames, source/medium and landing pages
    over that span. The homepage is fetched once without credentials and searched for each
    stream's measurement ID and for a GTM container.
    Search Console: sitemaps; searchAnalytics totals and by page, query, country and device
    for the last --days days and the --days before that (dataState "all"), plus query x page
    pairs and a by-date series for the current window; clicks and impressions by date
    for the last 16 months (everything Search Console keeps); URL Inspection for --url URLs, or
    with --urls-from-sitemap for the sitemap's URLs plus the other host variant of the
    homepage (apex for a www site). When the sitemap holds more URLs than --inspect-max
    (default 50), the inspected set is sampled across URL templates, never "the first N".
    Inspections run --inspect-workers at a time (default 4, each takes 3 to 7 seconds);
    quota is about 2,000 calls a day and 600 a minute per property, and --inspect-max is
    capped at 1,800. Every URL Google records as noindex is then fetched once, without
    credentials and with this tool's own user-agent, to see whether the live page still
    serves a noindex (capped at 25).

What it derives (data["sc"]["rollups"], rendered as tables)
    Search performance by URL template, with the sitemap URLs that earned no impression;
    query and page pairs at positions 4 to 20 ("within reach"); pages near the top with a
    very low click-through rate; queries split across pages; a weekly trend; brand against
    non-brand queries with --brand; coverage state by template, canonical disagreements and
    the age of Google's copies from URL Inspection; and, with --audit-json pointing at a
    seo_audit.py --json report, the crawl's findings ranked by the impressions of the pages
    and templates they sit on.

Output
    Markdown to stdout (findings first, then tables). --markdown also writes it to a file;
    --json writes the raw collected data plus the findings. Progress goes to stderr.
    Exit codes: 0 done (findings or not), 1 authentication or fatal API failure,
    2 usage error, or no GA4 property / Search Console property could be chosen.
"""

import argparse
import base64
import concurrent.futures
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

VERSION = "0.3.0"

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

INSPECT_CAP = 50            # default number of URLs inspected; --inspect-max raises it
INSPECT_HARD_CAP = 1800     # stay under the 2,000-a-day URL Inspection quota per property
INSPECT_WORKERS = 4         # concurrent inspections (the API takes 3 to 7 s per URL)
LIVE_RECHECK_CAP = 25       # noindex verdicts re-fetched live, without credentials
STRIKING_MIN_IMPRESSIONS = 5
INSPECT_DELAY = 0.2          # seconds between URL Inspection calls (tests set it to 0)
NOT_READABLE = "not readable with this scope/API"
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
HISTORY_DAYS = 420            # GA look-back used to date the last event and list what ever arrived
SC_HISTORY_DAYS = 480         # Search Console keeps 16 months
INDEXED_STATES = ("Submitted and indexed", "Indexed, not submitted in sitemap")
UNKNOWN_STATE = "URL is unknown to Google"
# Environment variables that may hold the key itself (JSON text or base64 of it), checked in
# this order after --key, --key-env and GOOGLE_APPLICATION_CREDENTIALS.
INLINE_KEY_VARS = ("GOOGLE_SERVICE_ACCOUNT_JSON", "GSC_SERVICE_ACCOUNT_JSON", "GA_SERVICE_ACCOUNT_JSON",
                   "GA_READER_KEY_JSON", "GA_READER_KEY_B64")
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
    return _accept_key(k, "the key file")


def load_key_env(var):
    """Read a key from an environment variable that holds the JSON text or its base64.
    Only the variable's NAME is ever printed."""
    text = (os.environ.get(var) or "").strip()
    if not text:
        raise SystemExit(_usage_error("environment variable %s is not set or is empty" % var))
    k = None
    try:
        k = json.loads(text)
    except ValueError:
        try:
            k = json.loads(base64.b64decode(text, validate=True).decode("utf-8"))
        except Exception:
            k = None
    if k is None:
        raise SystemExit(_usage_error(
            "environment variable %s is neither service-account JSON nor base64 of it" % var))
    return _accept_key(k, "environment variable " + var)


def _accept_key(k, where):
    global _KEY, _TOKEN
    if not isinstance(k, dict) or k.get("type") != "service_account" \
            or not k.get("client_email") or not k.get("private_key"):
        raise SystemExit(_usage_error(
            "%s is not a service-account key (needs type=service_account, "
            "client_email and private_key)" % where))
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


def collect_ga(prop, days, site_url=None):
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
    if site_url and ga["streams"]:
        ga["tag_check"] = check_tag(site_url, ga["streams"])
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
        da["history"] = collect_ga_history(P)
    else:
        da["available"] = False
        da["status"] = first["status"]
        da["reason"] = first["reason"]
        da["error"] = first["error"]
        da["enable_url"] = first["enable_url"]
    return ga


def collect_ga_history(prop, days=HISTORY_DAYS):
    """What the property has ever received, as far back as `days`: sessions by date (to date
    the first and last day with data) and the events, hostnames, sources and landing pages
    over that span. A short reporting window on a property whose tag was removed is all
    blanks; this is what turns the blanks into 'last event on 2026-09-25'."""
    dr = [{"startDate": "%ddaysAgo" % days, "endDate": "today"}]
    h = {"date_range": dr[0], "reports": {}}
    by_sessions = [{"metric": {"metricName": "sessions"}, "desc": True}]
    reports = {
        "days": {"dateRanges": dr, "dimensions": [{"name": "date"}],
                 "metrics": [{"name": "sessions"}, {"name": "eventCount"}], "limit": 500,
                 "orderBys": [{"dimension": {"dimensionName": "date"}}]},
        "events": {"dateRanges": dr, "dimensions": [{"name": "eventName"}],
                   "metrics": [{"name": "eventCount"}, {"name": "totalUsers"}], "limit": 100,
                   "orderBys": [{"metric": {"metricName": "eventCount"}, "desc": True}]},
        "hosts": {"dateRanges": dr, "dimensions": [{"name": "hostName"}],
                  "metrics": [{"name": "sessions"}], "limit": 15, "orderBys": by_sessions},
        "sources": {"dateRanges": dr, "dimensions": [{"name": "sessionSourceMedium"}],
                    "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}, {"name": "keyEvents"}],
                    "limit": 20, "orderBys": by_sessions},
        # Sessions GA4 could not file under any default channel, by the source/medium that
        # caused it. Almost always a hand-made utm_medium the channel rules do not know.
        "unassigned": {"dateRanges": dr, "dimensions": [{"name": "sessionSourceMedium"}],
                       "metrics": [{"name": "sessions"}], "limit": 10, "orderBys": by_sessions,
                       "dimensionFilter": {"filter": {"fieldName": "sessionDefaultChannelGroup",
                                                      "stringFilter": {"value": "Unassigned"}}}},
        "landing": {"dateRanges": dr, "dimensions": [{"name": "landingPage"}],
                    "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}],
                    "limit": 20, "orderBys": by_sessions},
    }
    for name, body in reports.items():
        h["reports"][name] = data_report(prop, body)
    d = h["reports"]["days"]
    if d["ok"]:
        with_data = [r for r in d["rows"] if len(r) >= 3 and (_int(r[1]) or _int(r[2]))]
        h["days_with_data"] = len(with_data)
        h["first_day"] = _iso_day(with_data[0][0]) if with_data else None
        h["last_day"] = _iso_day(with_data[-1][0]) if with_data else None
        h["sessions"] = sum(_int(r[1]) for r in with_data)
        h["reports"]["days"] = dict(d, rows=[])       # the per-day rows are not worth keeping
    return h


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _iso_day(yyyymmdd):
    return "%s-%s-%s" % (yyyymmdd[:4], yyyymmdd[4:6], yyyymmdd[6:8]) if len(yyyymmdd) == 8 else yyyymmdd


def check_tag(site_url, streams):
    """Fetch the homepage without credentials and look for each web stream's measurement ID
    and for a GTM container. Absence is evidence, not proof: a tag loaded by a first-party
    script, by GTM, or after a consent click is not in the HTML."""
    status, html = call(site_url, auth=False, raw=True)
    out = {"url": site_url, "status": status}
    if status != 200 or not isinstance(html, str):
        return out
    ids = [s.get("measurementId") for s in streams if s.get("measurementId")]
    out["measurement_ids_found"] = [i for i in ids if i in html]
    out["measurement_ids_missing"] = [i for i in ids if i not in html]
    out["other_ga_ids"] = sorted(set(re.findall(r"\bG-[A-Z0-9]{8,12}\b", html)) - set(ids))[:5]
    out["gtm_containers"] = sorted(set(re.findall(r"\bGTM-[A-Z0-9]{5,9}\b", html)))[:5]
    out["gtag_loader"] = "googletagmanager.com/gtag/js" in html
    return out


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


def search_analytics(sc_site, start, end, deep=False):
    url = WEBMASTERS + "sites/" + sc_quote(sc_site) + "/searchAnalytics/query"
    out = {"range": [start, end]}
    for label, dims, limit in (("totals", [], 1), ("page", ["page"], 1000), ("query", ["query"], 1000),
                               ("country", ["country"], 25), ("device", ["device"], 10),
                               ("query_page", ["query", "page"], 1000), ("date", ["date"], 500)):
        if label in ("query_page", "date") and not deep:
            continue
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
        if label == "date":
            rows.sort(key=lambda x: x["keys"])
        elif dims:
            rows.sort(key=lambda x: (-x["impressions"], -x["clicks"]))
        out[label] = {"ok": True, "rows": rows, "metadata": r.get("metadata")}
    return out


def search_history(sc_site, today=None, days=SC_HISTORY_DAYS):
    """Clicks and impressions by date for everything Search Console keeps (16 months), reduced
    to totals, the first and last day with an impression and how many days had one. A 28-day
    window cannot tell a site that lost its traffic from one that never had any."""
    today = today or dt.date.today()
    start, end = (today - dt.timedelta(days=days)).isoformat(), today.isoformat()
    url = WEBMASTERS + "sites/" + sc_quote(sc_site) + "/searchAnalytics/query"
    status, r = call(url, {"startDate": start, "endDate": end, "dimensions": ["date"],
                           "rowLimit": 1000, "dataState": "all"})
    if status != 200:
        e = parse_error(status, r)
        return {"ok": False, "status": status, "error": e["message"][:300], "range": [start, end]}
    rows = [x for x in r.get("rows", []) if x.get("impressions", 0) > 0]
    days_seen = sorted((x.get("keys") or [""])[0] for x in rows)
    return {"ok": True, "range": [start, end],
            "clicks": int(sum(x.get("clicks", 0) for x in rows)),
            "impressions": int(sum(x.get("impressions", 0) for x in rows)),
            "days_with_impressions": len(rows),
            "first_day": days_seen[0] if days_seen else None,
            "last_day": days_seen[-1] if days_seen else None}


def sitemap_urls(paths, site_url, cap=INSPECT_CAP):
    """The sitemap URLs to inspect: all of them when they fit under `cap`, otherwise a
    sample spread across URL templates (the first `cap` entries of a sitemap are usually
    the static pages, which says nothing about the template that holds most of the site).
    Returns (picked, total_found, sources_used, errors)."""
    urls, used, errors = sitemap_all(paths, site_url)
    picked = urls if len(urls) <= cap else stratified_sample(urls, cap)
    return picked, len(urls), used, errors


def sitemap_all(paths, site_url):
    """Collect <loc> URLs from the given sitemaps (following one level of sitemap index).
    Falls back to robots.txt Sitemap: lines, then /sitemap.xml. Fetched without credentials.
    Returns (urls_in_order, sources_used, errors)."""
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
    return urls, used, errors


# URL templates and sampling. Kept in step with seo_audit.py (url_template, stratified_sample)
# so the console tables and the crawl's coverage table use the same template names.
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def url_template(u):
    """A coarse path-shape signature: '/blog/*', '/{year}/*/*/*/', '/about' -> '/*'."""
    path = urllib.parse.urlsplit(u).path or "/"
    segs = [x for x in path.split("/") if x]
    if not segs:
        return "/"
    first = "{year}" if _YEAR_RE.match(segs[0]) else segs[0]
    if len(segs) == 1:
        return "/*" + ("/" if path.endswith("/") else "")
    return "/" + first + "/*" * (len(segs) - 1) + ("/" if path.endswith("/") else "")


def _spread(items, k):
    if k <= 0:
        return []
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def stratified_sample(urls, n):
    """Up to n URLs across template clusters: every template gets a slot before any gets a
    second, and picks are spread evenly inside each. Sitemap order is kept in the result."""
    pool = list(dict.fromkeys(urls))
    if n <= 0 or not pool:
        return []
    clusters = {}
    for u in pool:
        clusters.setdefault(url_template(u), []).append(u)
    ordered = sorted(clusters.items(), key=lambda kv: -len(kv[1]))
    quota = {t: 0 for t, _ in ordered}
    left = min(n, len(pool))
    while left > 0:
        progressed = False
        for t, members in ordered:
            if left and quota[t] < len(members):
                quota[t] += 1
                left -= 1
                progressed = True
        if not progressed:
            break
    chosen = set()
    for t, members in ordered:
        chosen.update(_spread(members, quota[t]))
    return [u for u in pool if u in chosen]


_META_ROBOTS_RE = re.compile(
    r"<meta\b[^>]*\bname\s*=\s*[\"']?(?:robots|googlebot)[\"']?[^>]*>", re.I)
_CONTENT_RE = re.compile(r"\bcontent\s*=\s*[\"']([^\"']*)[\"']", re.I)


def live_robots(url, timeout=20):
    """Fetch a URL with no credentials and report what it tells a crawler about indexing
    today: {"status", "x_robots", "meta_robots", "noindex"}. Used to tell a stale noindex
    verdict (Google's record is old, the page is indexable now) from a live one. The request
    is a plain GET with this tool's own user-agent, never a search engine's."""
    _guard(url, "GET", False)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            xr = ", ".join(resp.headers.get_all("X-Robots-Tag") or [])
            body = resp.read(600_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"status": e.code, "x_robots": ", ".join(e.headers.get_all("X-Robots-Tag") or []),
                "meta_robots": "", "noindex": None}
    except (urllib.error.URLError, OSError) as e:
        return {"status": 0, "x_robots": "", "meta_robots": "", "noindex": None,
                "error": str(getattr(e, "reason", e))[:120]}
    metas = []
    for tag in _META_ROBOTS_RE.findall(body):
        m = _CONTENT_RE.search(tag)
        if m:
            metas.append(m.group(1))
    meta = ", ".join(metas)
    return {"status": status, "x_robots": xr, "meta_robots": meta,
            "noindex": bool(re.search(r"\b(noindex|none)\b", (xr + "," + meta).lower()))}


def inspect_url(url, sc_site):
    body = {"inspectionUrl": url, "siteUrl": sc_site, "languageCode": "en-US"}
    status, r = call(INSPECT_URL, body, timeout=60)
    if status == 0:                      # a timeout or dropped connection: one more try
        time.sleep(INSPECT_DELAY * 5)
        status, r = call(INSPECT_URL, body, timeout=60)
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
            "crawledAs": ix.get("crawledAs"), "referringUrls": (ix.get("referringUrls") or [])[:5],
            "sitemap": ix.get("sitemap"),
            "richResults": ({"verdict": rr.get("verdict"), "types": types} if rr else None),
            "link": ir.get("inspectionResultLink")}


def collect_sc(sc_site, permission, site_url, days, inspect_mode, urls, today=None,
               inspect_max=INSPECT_CAP, workers=INSPECT_WORKERS):
    sc = {"site": sc_site, "permission": permission}
    log("Search Console: sitemaps for " + sc_site)
    sc["sitemaps"] = item(*call(WEBMASTERS + "sites/" + sc_quote(sc_site) + "/sitemaps"))
    if sc["sitemaps"]["ok"]:
        sc["sitemaps"]["data"] = sc["sitemaps"]["data"].get("sitemap") or []
    cur, prev = date_windows(days, today)
    log("Search Console: search analytics %s..%s and %s..%s" % (cur + prev))
    sc["analytics"] = {"current": search_analytics(sc_site, *cur, deep=True),
                       "previous": search_analytics(sc_site, *prev)}
    sc["history"] = search_history(sc_site, today)

    # The sitemap is read whenever it can be, inspection or not: the performance tables
    # need the template sizes and the list of sitemap URLs that earned no impression.
    paths = [s.get("path") for s in (sc["sitemaps"].get("data") or []) if s.get("path")] \
        if sc["sitemaps"]["ok"] else []
    all_urls, used, errs = sitemap_all(paths, site_url)
    sc["sitemap_urls"] = all_urls[:50000]

    targets = []
    insp = {"mode": inspect_mode, "cap": inspect_max, "capped": False}
    if inspect_mode == "sitemap":
        total = len(all_urls)
        found = all_urls if total <= inspect_max else stratified_sample(all_urls, inspect_max)
        insp.update({"sitemaps_read": used, "sitemap_errors": errs, "sitemap_url_count": total,
                     "capped": total > inspect_max})
        targets = [(u, "sitemap") for u in found]
        v = variant_homepage(site_url)
        if v not in found:
            targets.append((v, "variant"))
    elif inspect_mode == "url":
        targets = [(u, "url") for u in urls[:inspect_max]]
        insp["capped"] = len(urls) > inspect_max
    insp["results"] = []
    if targets:
        log("Search Console: inspecting %d URL(s), %d at a time (about 4 s each)"
            % (len(targets), max(1, workers)))

    def one(t):
        if INSPECT_DELAY:
            time.sleep(INSPECT_DELAY)
        res = inspect_url(t[0], sc_site)
        res["source"] = t[1]
        return res
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for i, res in enumerate(ex.map(one, targets), 1):      # map keeps the input order
            insp["results"].append(res)
            if i % 25 == 0 and i < len(targets):
                log("  inspected %d of %d" % (i, len(targets)))

    # A noindex verdict is only as fresh as Google's last crawl. Fetch those pages now
    # (no credentials) so the report can say which verdicts the live page no longer earns.
    blocked = [r for r in insp["results"] if r.get("ok") and is_noindex_verdict(r)]
    if blocked:
        log("re-fetching %d page(s) Google records as noindex" % min(len(blocked), LIVE_RECHECK_CAP))
    for r in _spread(blocked, LIVE_RECHECK_CAP):
        r["live"] = live_robots(r["url"])
    sc["inspection"] = insp
    return sc


def is_noindex_verdict(r):
    return "noindex" in (r.get("coverageState") or "").lower() \
        or (r.get("indexingState") or "").startswith("BLOCKED_BY")


# =================================================================================
# Roll-ups (pure: no network). These turn rows into the questions a review asks.
# =================================================================================
def _wpos(rows):
    imps = sum(r.get("impressions", 0) for r in rows)
    return (sum(r.get("position", 0.0) * r.get("impressions", 0) for r in rows) / imps) if imps else 0.0


def performance_by_template(page_rows, sitemap_list):
    """One row per URL template: how many sitemap URLs it holds, how many pages earned an
    impression, clicks, impressions, impression-weighted position. Sorted by impressions."""
    agg = {}
    for u in sitemap_list or []:
        agg.setdefault(url_template(u), {"sitemap_urls": 0, "rows": []})["sitemap_urls"] += 1
    for r in page_rows or []:
        u = (r.get("keys") or [""])[0].split("?")[0].split("#")[0]
        agg.setdefault(url_template(u), {"sitemap_urls": 0, "rows": []})["rows"].append(r)
    out = []
    for t, a in agg.items():
        rows = a["rows"]
        out.append({"template": t, "sitemap_urls": a["sitemap_urls"], "pages_with_impressions": len(rows),
                    "clicks": sum(r.get("clicks", 0) for r in rows),
                    "impressions": sum(r.get("impressions", 0) for r in rows),
                    "position": round(_wpos(rows), 1)})
    out.sort(key=lambda x: (-x["impressions"], -x["sitemap_urls"]))
    return out


def striking_distance(qp_rows, min_impressions=STRIKING_MIN_IMPRESSIONS, lo=3.0, hi=20.0):
    """Query and page pairs ranking just off the top results: the cheapest wins available,
    because the page already ranks and a better title, answer or internal link can move it."""
    out = [{"query": r["keys"][0], "page": r["keys"][1], "clicks": r["clicks"],
            "impressions": r["impressions"], "position": round(r["position"], 1)}
           for r in qp_rows or [] if len(r.get("keys") or []) == 2
           and lo < r.get("position", 0) <= hi and r.get("impressions", 0) >= min_impressions]
    out.sort(key=lambda x: (-x["impressions"], x["position"]))
    return out


def low_ctr_pages(page_rows, max_position=5.0, min_impressions=20, max_ctr=0.02):
    """Pages shown near the top that almost nobody clicks: a title or description problem,
    or a query the page does not really answer."""
    out = [{"page": r["keys"][0], "clicks": r["clicks"], "impressions": r["impressions"],
            "ctr": r.get("ctr", 0.0), "position": round(r["position"], 1)}
           for r in page_rows or [] if r.get("keys") and 0 < r.get("position", 0) <= max_position
           and r.get("impressions", 0) >= min_impressions and r.get("ctr", 0.0) < max_ctr]
    out.sort(key=lambda x: -x["impressions"])
    return out


def split_queries(qp_rows, min_share=0.2, min_impressions=10):
    """Queries whose impressions are split between two or more pages, each holding a real
    share. Usually two pages competing for one intent; sometimes a hub and its child."""
    by_q = {}
    for r in qp_rows or []:
        if len(r.get("keys") or []) == 2:
            by_q.setdefault(r["keys"][0], []).append(r)
    out = []
    for q, rows in by_q.items():
        total = sum(r["impressions"] for r in rows)
        big = [r for r in rows if total and r["impressions"] / total >= min_share]
        if total >= min_impressions and len(big) >= 2:
            big.sort(key=lambda r: -r["impressions"])
            out.append({"query": q, "impressions": total,
                        "pages": [{"page": r["keys"][1], "impressions": r["impressions"],
                                   "position": round(r["position"], 1)} for r in big]})
    out.sort(key=lambda x: -x["impressions"])
    return out


def brand_split(query_rows, brand_terms):
    """Clicks and impressions for queries that contain a brand term against the rest.
    Only visible queries count; anonymized ones are in the totals and nowhere else."""
    terms = [t.lower() for t in brand_terms or [] if t.strip()]
    out = {"brand": {"queries": 0, "clicks": 0, "impressions": 0},
           "non_brand": {"queries": 0, "clicks": 0, "impressions": 0}}
    for r in query_rows or []:
        q = ((r.get("keys") or [""])[0]).lower()
        k = "brand" if any(t in q for t in terms) else "non_brand"
        out[k]["queries"] += 1
        out[k]["clicks"] += r.get("clicks", 0)
        out[k]["impressions"] += r.get("impressions", 0)
    return out


def weekly_trend(date_rows):
    """Clicks and impressions per ISO week (Monday start) from by-date rows."""
    wk = {}
    for r in date_rows or []:
        try:
            d = dt.date.fromisoformat((r.get("keys") or [""])[0])
        except ValueError:
            continue
        k = (d - dt.timedelta(days=d.weekday())).isoformat()
        a = wk.setdefault(k, {"week": k, "clicks": 0, "impressions": 0, "days": 0})
        a["clicks"] += r.get("clicks", 0)
        a["impressions"] += r.get("impressions", 0)
        a["days"] += 1
    return [wk[k] for k in sorted(wk)]


def inspection_rollup(results, today=None):
    """Coverage state by template, canonical disagreements, and how old Google's copies are."""
    today = today or dt.datetime.now(dt.timezone.utc)
    ok = [r for r in results or [] if r.get("ok") and r.get("source") != "variant"]
    by_state, by_tpl, ages, mismatch = {}, {}, [], []
    for r in ok:
        state = r.get("coverageState") or "unspecified"
        by_state[state] = by_state.get(state, 0) + 1
        t = by_tpl.setdefault(url_template(r["url"]), {})
        t[state] = t.get(state, 0) + 1
        lc = parse_time(r.get("lastCrawlTime"))
        if lc:
            ages.append((today - lc).days)
        g, u = r.get("googleCanonical"), r.get("userCanonical")
        if g and u and g.rstrip("/") != u.rstrip("/"):
            mismatch.append({"url": r["url"], "googleCanonical": g, "userCanonical": u})
    ages.sort()
    return {"inspected": len(ok), "by_state": by_state, "by_template": by_tpl,
            "crawled": len(ages), "never_crawled": len(ok) - len(ages),
            "median_crawl_age_days": ages[len(ages) // 2] if ages else None,
            "crawl_older_than_60d": len([a for a in ages if a > 60]),
            "canonical_mismatch": mismatch}


def rank_crawl_findings(audit, page_rows, sitemap_list=None):
    """Join a seo_audit.py --json report with Search Console page rows: every crawl finding
    gets the impressions of the pages (deep-crawled) or templates (swept) it sits on, so the
    fix list can be ordered by where search demand already is. A swept finding is reported
    per template, not per URL, so its impressions are the templates' impressions scaled by
    the share of those templates' pages that carry it (19 slow pages out of 460 do not own
    the whole template's demand). Returns rows sorted by impressions, then severity. Pure."""
    tpl_size = {}
    for u in sitemap_list or []:
        tpl_size[url_template(u)] = tpl_size.get(url_template(u), 0) + 1
    by_url, by_tpl = {}, {}
    for r in page_rows or []:
        u = (r.get("keys") or [""])[0].split("?")[0].split("#")[0].rstrip("/")
        by_url[u] = by_url.get(u, 0) + r.get("impressions", 0)
        t = url_template((r.get("keys") or [""])[0].split("?")[0])
        by_tpl[t] = by_tpl.get(t, 0) + r.get("impressions", 0)
    rows = {}
    for p in audit.get("pages") or []:
        imp = by_url.get((p.get("url") or "").rstrip("/"), 0)
        for f in p.get("findings") or []:
            if f.get("severity") == "INFO":
                continue
            k = (f.get("severity"), f.get("title"))
            a = rows.setdefault(k, {"severity": k[0], "title": k[1], "pages": 0, "impressions": 0,
                                    "templates": set(), "example": p.get("url")})
            a["pages"] += 1
            a["impressions"] += imp
            a["templates"].add(url_template(p.get("url") or "/"))
    for sw in audit.get("sweep_summary") or []:
        if sw.get("severity") == "INFO":
            continue
        k = (sw.get("severity"), sw.get("title"))
        a = rows.setdefault(k, {"severity": k[0], "title": k[1], "pages": 0, "impressions": 0,
                                "templates": set(), "example": sw.get("example")})
        a["pages"] += sw.get("count", 0)
        a["templates"] |= set(sw.get("templates") or [])
        # template impressions stand in for the swept pages; deep pages already counted by URL
        size = sum(tpl_size.get(t, 0) for t in a["templates"])
        share = min(1.0, a["pages"] / size) if size else 1.0
        a["impressions"] = max(a["impressions"],
                               int(round(share * sum(by_tpl.get(t, 0) for t in a["templates"]))))
    out = [dict(a, templates=sorted(a["templates"])) for a in rows.values()]
    out.sort(key=lambda x: (-x["impressions"], SEVERITY_ORDER.get(x["severity"], 9), -x["pages"]))
    return out


def build_rollups(data, brand_terms=None, audit=None, today=None):
    """Everything derived from the collected Search Console data, in one place, stored under
    data["sc"]["rollups"] so the JSON carries it and the Markdown renders it."""
    sc = data.get("sc") or {}
    cur = (sc.get("analytics") or {}).get("current") or {}

    def rows(label):
        blk = cur.get(label) or {}
        return blk.get("rows") or [] if blk.get("ok") else []
    sm = sc.get("sitemap_urls") or []
    seen = {(r.get("keys") or [""])[0].split("?")[0].split("#")[0].rstrip("/") for r in rows("page")}
    silent = {}
    for u in sm:
        if u.rstrip("/") not in seen:
            silent[url_template(u)] = silent.get(url_template(u), 0) + 1
    out = {"by_template": performance_by_template(rows("page"), sm),
           "striking_distance": striking_distance(rows("query_page")),
           "low_ctr": low_ctr_pages(rows("page")),
           "split_queries": split_queries(rows("query_page")),
           "weekly": weekly_trend(rows("date")),
           "sitemap_urls_without_impressions": {"total": sum(silent.values()), "of": len(sm),
                                                "by_template": silent},
           "inspection": inspection_rollup((sc.get("inspection") or {}).get("results"), today)}
    if brand_terms:
        out["brand"] = dict(brand_split(rows("query"), brand_terms), terms=list(brand_terms))
    if audit:
        out["crawl_findings_by_demand"] = rank_crawl_findings(audit, rows("page"), sm)
    # A capped inspection samples every template about equally, so its raw totals overweight
    # the small templates. Scale each template's shares by its size for a site-wide estimate.
    insp = sc.get("inspection") or {}
    ir = out["inspection"]
    if insp.get("capped") and insp.get("mode") == "sitemap" and ir["inspected"]:
        sizes, est = {}, {}
        for u in sm:
            sizes[url_template(u)] = sizes.get(url_template(u), 0) + 1
        for t, states in ir["by_template"].items():
            n = sum(states.values())
            for st, c in states.items():
                est[st] = est.get(st, 0.0) + sizes.get(t, 0) * c / n
        ir["estimated_site_wide"] = {st: int(round(v)) for st, v in est.items()}
    return out


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

    acc = data.get("access") or {}
    email = data.get("client_email") or "the service account"
    if acc.get("ga") == "none":
        out.append(finding(
            "HIGH", "The service account cannot see any GA4 property", "access",
            "accountSummaries returned no accounts for %s" % email,
            "In GA4, Admin > Property access management > add %s as Viewer. Access is granted per "
            "property (or account) inside Analytics; an IAM role in the Cloud project does nothing. "
            "GA4 was not reviewed." % email))
    if acc.get("sc") == "none":
        seen = acc.get("sc_visible") or []
        out.append(finding(
            "HIGH", "The service account has no Search Console property for this site", "access",
            "sites.list returned %s" % (", ".join(seen) if seen else "an empty list (HTTP 200)"),
            "In Search Console open the property, Settings > Users and permissions > Add user, and "
            "enter %s (Restricted is enough to read). A 200 with an empty list means the key and the "
            "API are fine and only this grant is missing. Add it to the domain property if there is "
            "one. Search Console was not reviewed." % email))

    da = ga.get("data_api") or {}
    hist = da.get("history") or {}
    hrep = hist.get("reports") or {}
    tag = ga.get("tag_check") or {}
    ev = (da.get("reports") or {}).get("events") or {}
    if da.get("available") and ev.get("ok") and not ev.get("rows"):
        window = (da.get("date_range") or {}).get("startDate", "the window")
        if hist.get("last_day"):
            last = dt.date.fromisoformat(hist["last_day"])
            seen = "last day with data %s (%d days ago), first %s, %d sessions over %d day(s) with data" % (
                hist["last_day"], (today.date() - last).days, hist.get("first_day"),
                hist.get("sessions", 0), hist.get("days_with_data", 0))
        elif "days_with_data" in hist:
            seen = "and nothing in the last %d days either: the property has never received an event" % HISTORY_DAYS
        else:
            seen = "history could not be read"
        missing = tag.get("measurement_ids_missing") or []
        if tag.get("status") == 200 and missing and not tag.get("measurement_ids_found"):
            where = "; %s is not in the homepage HTML%s" % (
                ", ".join(missing),
                " (GTM container %s is, so the tag may load from there)" % ", ".join(tag["gtm_containers"])
                if tag.get("gtm_containers") else
                " (another GA4 ID is: %s)" % ", ".join(tag["other_ga_ids"]) if tag.get("other_ga_ids") else
                " and no gtag loader or GTM container is either")
        elif tag.get("measurement_ids_found"):
            where = "; %s is in the homepage HTML, so look at a consent gate, a CSP block or a host " \
                    "filter" % ", ".join(tag["measurement_ids_found"])
        else:
            where = ""
        out.append(finding(
            "HIGH", "GA4 has received no events in the reporting window", "analytics",
            "0 events since %s; %s%s" % (window, seen, where),
            "The property is configured but nothing is arriving, so every other GA4 setting is moot "
            "until this is fixed. Confirm in a real browser (network requests to "
            "google-analytics.com/g/collect) whether the tag fires, then check the site's history for "
            "why it stopped: a tag removed on purpose (privacy policy, consent) is a decision for the "
            "owner, not a bug to patch. Annotate the gap in GA4 once data flows again."))

    kev = it.get("keyEvents") or {}
    hev = hrep.get("events") or {}
    if kev.get("ok") and hev.get("ok") and hev.get("rows"):
        sent = {r[0] for r in hev["rows"] if r}
        dead = [k.get("eventName") for k in kev["data"] if k.get("eventName") not in sent]
        if dead:
            out.append(finding(
                "MEDIUM", "Key events that nothing sends", "analytics",
                "marked as key events but absent from %d days of events: %s (events seen: %s)"
                % (HISTORY_DAYS, ", ".join(dead), ", ".join(sorted(sent))[:200]),
                "A key event no event matches counts nothing, so conversion and acquisition reports "
                "stay at zero. Either send the event from the site (the action the business runs on: "
                "sign_up, purchase, generate_lead) or unmark it in Admin > Data display > Key events and "
                "mark the events that do arrive. close_convert_lead and qualify_lead are created by "
                "GA's lead-generation setup and are leftovers on a site that never sends them."))

    un = hrep.get("unassigned") or {}
    if un.get("ok") and un.get("rows") and hist.get("sessions"):
        rows_u = [(r[0], _int(r[1])) for r in un["rows"] if len(r) >= 2 and _int(r[1])]
        lost = sum(n for _, n in rows_u)
        if lost >= 10 and lost * 20 >= hist["sessions"]:           # at least 10 sessions and 5%
            out.append(finding(
                "LOW", "Campaign traffic GA4 files under Unassigned", "analytics",
                "%d of %d sessions (%d%%): %s" % (lost, hist["sessions"], round(100 * lost / hist["sessions"]),
                                                  ", ".join("%s (%d)" % r for r in rows_u[:6])),
                "GA4 assigns a channel from utm_medium, and only knows a fixed vocabulary: social, "
                "email, cpc, organic, referral, affiliate, display, video and a few "
                "more. A medium such as 'reel', 'post' or 'bio' matches no rule, so the visit lands in "
                "Unassigned and drops out of every channel report. Use utm_medium=social (or email, "
                "cpc...) and move the detail to utm_content or utm_campaign. It cannot be fixed "
                "retroactively."))

    hosts = hrep.get("hosts") or {}
    if hosts.get("ok") and site_host:
        stray = [(r[0], _int(r[1])) for r in hosts["rows"]
                 if len(r) >= 2 and r[0] and strip_www(r[0].lower()) != strip_www(site_host) and _int(r[1])]
        if stray:
            out.append(finding(
                "LOW", "GA4 is collecting from hosts other than the site", "analytics",
                ", ".join("%s (%d sessions)" % h for h in stray[:8]),
                "Localhost, preview and staging hits pollute a small property. Load the tag only when "
                "location.hostname is the production host, or exclude the others with a data filter "
                "(Admin > Data collection and modification > Data filters)."))

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

    # --- what Google's index holds, by state, against what the page serves today ---
    noidx = [r for r in checked if is_noindex_verdict(r)]
    stale = [r for r in noidx if (r.get("live") or {}).get("noindex") is False
             and (r.get("live") or {}).get("status") == 200]
    live_no = [r for r in noidx if (r.get("live") or {}).get("noindex") is True]
    if stale:
        oldest = min((r.get("lastCrawlTime") or "9999")[:10] for r in stale)
        rechecked = len([r for r in noidx if r.get("live")])
        out.append(finding(
            "HIGH", "Google still records a noindex these pages no longer serve", "search-console",
            "%d of %d re-fetched noindex verdicts are stale (%d noindex verdicts among %d inspected "
            "URLs; oldest crawl %s): %s"
            % (len(stale), rechecked, len(noidx), len(checked), oldest,
               ", ".join("%s (crawled %s)" % (r["url"], (r.get("lastCrawlTime") or "?")[:10])
                         for r in stale[:12]) + (" ..." if len(stale) > 12 else "")),
            "A verdict lasts until Google recrawls the URL, and a page it believes is noindex is "
            "recrawled rarely, so this does not clear on its own for months. Use URL Inspection > "
            "Request indexing on the hubs first (the pages that link to the most others), a dozen a "
            "day; move the sitemap <lastmod> of every affected URL to a date after the noindex was "
            "removed and resubmit the sitemap; link the affected pages from pages Google does crawl."))
    if live_no:
        out.append(finding(
            "MEDIUM", "Sitemap lists pages that serve noindex", "search-console",
            ", ".join("%s (%s)" % (r["url"], (r["live"].get("x_robots") or r["live"].get("meta_robots")))
                      for r in live_no[:12]),
            "A sitemap should list only indexable URLs. Remove these from the sitemap, or remove "
            "the noindex if the page is meant to rank."))
    mism = [r for r in checked if r.get("googleCanonical") and r.get("userCanonical")
            and r["googleCanonical"].rstrip("/") != r["userCanonical"].rstrip("/")]
    if mism:
        out.append(finding(
            "MEDIUM", "Google chose a different canonical than the page declares", "search-console",
            "%d of %d inspected: %s" % (len(mism), len(checked), "; ".join(
                "%s declares %s, Google chose %s" % (r["url"], r["userCanonical"], r["googleCanonical"])
                for r in mism[:8])),
            "Google follows links and content over the tag. Point internal links, the sitemap and "
            "redirects at the URL you declare, and make sure the declared page is the fuller one."))
    dated = [parse_time(r.get("lastCrawlTime")) for r in checked if parse_time(r.get("lastCrawlTime"))]
    old = [d for d in dated if (today - d).days > 60]
    if len(dated) >= 10 and len(old) * 2 > len(dated):
        out.append(finding(
            "MEDIUM", "Most of Google's copies are over 60 days old", "search-console",
            "%d of %d crawled URLs were last fetched more than 60 days ago" % (len(old), len(dated)),
            "Google is spending very few fetches here. Changes shipped since then are not in the "
            "index. Crawl demand follows links and traffic: earn links to the hubs, keep the sitemap "
            "<lastmod> honest, and request indexing for the pages that matter most."))

    # the general finding; verdicts the live re-fetch already explained above are left out
    explained = set(r["url"] for r in stale + live_no)
    held, held_tp = {}, {}
    for r in checked:
        state = r.get("coverageState") or "no coverage state"
        if state not in INDEXED_STATES and state != UNKNOWN_STATE and r["url"] not in explained:
            held.setdefault(state, []).append(r["url"])
            held_tp[url_template(r["url"])] = held_tp.get(url_template(r["url"]), 0) + 1
    if held:
        indexed = len([r for r in checked if r.get("coverageState") in INDEXED_STATES])
        n_held = sum(len(v) for v in held.values())
        hints = []
        if any(k.startswith("Discovered") for k in held):
            hints.append("'Discovered - currently not indexed' means Google has the URL and has not "
                         "spent a crawl on it. That is a demand problem, not a blocked page: the site "
                         "has too little authority or too little evidence of value for Google to "
                         "bother. Request indexing by hand for the few pages that matter most (about "
                         "ten a day), earn links from other sites to those pages, and make sure "
                         "unknown URLs return a real 404, since a host that answers 200 to anything "
                         "teaches Google that its URLs are not worth sampling")
        if any(k.startswith("Crawled") for k in held):
            hints.append("'Crawled - currently not indexed' means Google fetched the page and "
                         "declined it: look for thin, templated or duplicated content before anything else")
        if any("redirect" in k.lower() or "canonical" in k.lower() or "noindex" in k.lower()
               or "404" in k or "robots" in k.lower() for k in held):
            hints.append("redirecting, canonicalized, noindexed, blocked or missing URLs do not belong "
                         "in a sitemap: fix the page or remove the entry")
        out.append(finding(
            "HIGH" if indexed * 2 < len(checked) else "MEDIUM",
            "Sitemap URLs Google has not indexed", "search-console",
            "%d of %d inspected URLs are indexed; %d held back: %s. By template: %s" % (
                indexed, len(checked), n_held,
                "; ".join("%s (%d): %s" % (k, len(v), ", ".join(v[:6]) + (" ..." if len(v) > 6 else ""))
                          for k, v in sorted(held.items(), key=lambda kv: -len(kv[1]))),
                ", ".join("%s %d" % kv for kv in sorted(held_tp.items(), key=lambda kv: -kv[1]))),
            ". ".join(h[0].upper() + h[1:] for h in hints) + "." if hints else
            "Open each in Search Console URL Inspection for the reason."))

    own = strip_www(site_host)
    ext = {}
    for r in checked:
        for u in r.get("referringUrls") or []:
            h = strip_www(host_of(u))
            if h and h != own:
                ext.setdefault(h, set()).add(r["url"])
    if ext:
        out.append(finding(
            "INFO", "Google found pages through other hosts", "search-console",
            "; ".join("%s referred %d URL(s)" % (h, len(v)) for h, v in sorted(ext.items())),
            "Check that each is intended. An old domain, a staging or platform host, or another site "
            "whose sitemap lists these URLs should 301 here (or stop listing them); a genuine "
            "third-party link is good news and worth knowing about."))

    an = sc.get("analytics") or {}
    cur0 = an.get("current") or {}
    qp = (cur0.get("query_page") or {}).get("rows") or [] if (cur0.get("query_page") or {}).get("ok") else []
    near = striking_distance(qp)
    if near:
        out.append(finding(
            "LOW", "Queries ranking just off the top results", "search-console",
            "%d query and page pairs at positions 4 to 20 with at least %d impressions; largest: %s"
            % (len(near), STRIKING_MIN_IMPRESSIONS, "; ".join(
                "'%s' on %s (pos %.1f, %d imp)" % (x["query"], urllib.parse.urlsplit(x["page"]).path,
                                                   x["position"], x["impressions"]) for x in near[:6])),
            "These pages already rank, so they are the cheapest gains: put the query's words in the "
            "title and H1 where honest, answer it in the first screen, and link to the page from "
            "related pages with descriptive anchor text. Re-read after 28 days."))
    pg_rows = (cur0.get("page") or {}).get("rows") or [] if (cur0.get("page") or {}).get("ok") else []
    weak = low_ctr_pages(pg_rows)
    if weak:
        out.append(finding(
            "LOW", "Pages shown near the top that few people click", "search-console",
            "; ".join("%s (pos %.1f, %d imp, %.1f%% CTR)" % (urllib.parse.urlsplit(x["page"]).path,
                      x["position"], x["impressions"], 100 * x["ctr"]) for x in weak[:6]),
            "Rewrite the title and meta description for the queries the page is shown for (the "
            "query table, filtered to that page). A top position with no clicks can also mean a "
            "SERP feature answers the query above the result; check the live results page."))

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

    oldest = min([t for t in (parse_time(x.get("lastSubmitted")) for x in (sm.get("data") or [])
                              if isinstance(x, dict)) if t] or [None], key=lambda t: t or today) \
        if sm.get("ok") else None
    days_n = max(1, int(data.get("days") or 28))
    if tot.get("ok") and oldest and (today - oldest).days >= 60:
        imps = sum(r.get("impressions", 0) for r in tot["rows"])
        if imps < days_n:                      # under one impression a day
            sh = sc.get("history") or {}
            longer = ("; %d impressions and %d clicks in the %d days Search Console keeps, on %d day(s)"
                      % (sh.get("impressions", 0), sh.get("clicks", 0), SC_HISTORY_DAYS,
                         sh.get("days_with_impressions", 0))) if sh.get("ok") else ""
            out.append(finding(
                "HIGH", "The site is close to invisible in Google Search", "search-console",
                "%d impressions in the last %d days, sitemap first submitted %d days ago%s"
                % (imps, days_n, (today - oldest).days, longer),
                "At this level on-page tuning is not the constraint: titles and descriptions cannot "
                "raise the click-through of pages that are never shown. Work in this order: get the "
                "pages indexed (see the inspection findings), then earn links and mentions so Google "
                "has a reason to rank them, then revisit copy once queries appear in this report. "
                "Report the crawl score as hygiene, not as an explanation of traffic."))

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
            h = da.get("history") or {}
            if h:
                w("### Data API history (last %d days)\n" % HISTORY_DAYS)
                w("First day with data: %s. Last day with data: %s. %s sessions on %s day(s).\n" % (
                    h.get("first_day") or "none", h.get("last_day") or "none",
                    h.get("sessions", 0), h.get("days_with_data", 0)))
                for name in ("sources", "unassigned", "landing", "events", "hosts"):
                    rep_ = (h.get("reports") or {}).get(name) or {}
                    if rep_.get("ok") and rep_.get("rows"):
                        w("**%s**\n" % name)
                        w(md_table(rep_["headers"], rep_["rows"]))
            tc_ = ga.get("tag_check") or {}
            if tc_:
                w("### Tag on the homepage\n")
                if tc_.get("status") != 200:
                    w("Homepage not readable (HTTP %s).\n" % tc_.get("status"))
                else:
                    w("Stream IDs found in the HTML: %s. Missing: %s. GTM containers: %s. gtag loader: %s. "
                      "A tag loaded by a first-party script or after consent is not visible here.\n" % (
                          ", ".join(tc_.get("measurement_ids_found") or []) or "none",
                          ", ".join(tc_.get("measurement_ids_missing") or []) or "none",
                          ", ".join(tc_.get("gtm_containers") or []) or "none",
                          "yes" if tc_.get("gtag_loader") else "no"))
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

    sh = sc.get("history") or {}
    if sh.get("ok"):
        w("### Search history (%s..%s, everything Search Console keeps)\n" % tuple(sh["range"]))
        w("%d impressions and %d clicks on %d day(s) with an impression; first %s, last %s.\n" % (
            sh["impressions"], sh["clicks"], sh["days_with_impressions"],
            sh.get("first_day") or "none", sh.get("last_day") or "none"))
    ru = sc.get("rollups") or {}
    if ru.get("by_template"):
        w("### Search performance by URL template\n")
        w(md_table(["Template", "Sitemap URLs", "Pages with impressions", "Clicks", "Impressions", "Position"],
                   [[x["template"], x["sitemap_urls"], x["pages_with_impressions"], x["clicks"],
                     x["impressions"], "%.1f" % x["position"]] for x in ru["by_template"]]))
        si = ru.get("sitemap_urls_without_impressions") or {}
        if si.get("of"):
            w("%d of %d sitemap URLs earned no impression in the period%s.\n" % (
                si["total"], si["of"], (": " + ", ".join("%s %d" % kv for kv in sorted(
                    si["by_template"].items(), key=lambda kv: -kv[1]))) if si["by_template"] else ""))
    if ru.get("brand"):
        b = ru["brand"]
        w("### Brand and non-brand queries (terms: %s)\n" % ", ".join(b.get("terms") or []))
        w(md_table(["Segment", "Visible queries", "Clicks", "Impressions"],
                   [[k, b[k]["queries"], b[k]["clicks"], b[k]["impressions"]] for k in ("brand", "non_brand")]))
    if ru.get("striking_distance"):
        w("### Queries within reach (positions 4 to 20)\n")
        w(md_table(["Query", "Page", "Clicks", "Impressions", "Position"],
                   [[x["query"], x["page"], x["clicks"], x["impressions"], "%.1f" % x["position"]]
                    for x in ru["striking_distance"][:25]]))
    if ru.get("split_queries"):
        w("### Queries split across pages\n")
        w(md_table(["Query", "Impressions", "Pages"],
                   [[x["query"], x["impressions"], "; ".join("%s (%d imp, pos %.1f)" % (
                       p["page"], p["impressions"], p["position"]) for p in x["pages"])]
                    for x in ru["split_queries"][:15]]))
    if ru.get("weekly"):
        w("### Weekly trend\n")
        w(md_table(["Week of", "Days", "Clicks", "Impressions"],
                   [[x["week"], x["days"], x["clicks"], x["impressions"]] for x in ru["weekly"]]))
    if ru.get("crawl_findings_by_demand"):
        w("### Crawl findings ranked by search demand\n")
        w("Each crawl finding with the impressions of the pages or templates it sits on. Fix from "
          "the top: a LOW on the template that earns the impressions outranks a MEDIUM on pages "
          "nobody is shown.\n")
        w(md_table(["Impressions", "Severity", "Finding", "Pages", "Templates"],
                   [[x["impressions"], x["severity"], x["title"], x["pages"], ", ".join(x["templates"])]
                    for x in ru["crawl_findings_by_demand"][:30]]))

    insp = sc.get("inspection") or {}
    w("### URL inspection\n")
    ir = ru.get("inspection") or {}
    if ir.get("inspected"):
        states = sorted(ir["by_state"], key=lambda k: -ir["by_state"][k])
        w("%d URLs inspected; %d never crawled; median age of Google's copy %s days; %d older than "
          "60 days.\n" % (ir["inspected"], ir["never_crawled"], ir["median_crawl_age_days"],
                          ir["crawl_older_than_60d"]))
        w(md_table(["Template"] + states + ["Inspected"],
                   [[t] + [c.get(st, 0) for st in states] + [sum(c.values())]
                    for t, c in sorted(ir["by_template"].items(), key=lambda kv: -sum(kv[1].values()))]
                   + [["ALL"] + [ir["by_state"][st] for st in states] + [ir["inspected"]]]
                   + ([["Estimate for all %d sitemap URLs (sample scaled by template size)"
                        % len(sc.get("sitemap_urls") or [])]
                       + [ir["estimated_site_wide"].get(st, 0) for st in states] + [""]]
                      if ir.get("estimated_site_wide") else [])))
    if insp.get("mode") is None:
        w("Skipped (pass --urls-from-sitemap or --url).\n")
    else:
        if insp.get("mode") == "sitemap":
            w("Sitemaps read: %s. %d URL(s) found%s.%s\n" % (
                ", ".join(insp.get("sitemaps_read") or []) or "none", insp.get("sitemap_url_count", 0),
                "; %d inspected, sampled across URL templates (raise --inspect-max)" % insp.get("cap", INSPECT_CAP)
                if insp.get("capped") else "",
                (" Errors: " + "; ".join(insp["sitemap_errors"])) if insp.get("sitemap_errors") else ""))
        elif insp.get("capped"):
            w("Capped at %d URLs.\n" % insp.get("cap", INSPECT_CAP))
        rows = []
        for r in insp.get("results", []):
            if not r.get("ok"):
                rows.append([r["url"], "HTTP %s" % r.get("status"), r.get("error"), "", "", "", "", "", "", ""])
                continue
            rich = r.get("richResults")
            rows.append([r["url"] + (" (variant)" if r.get("source") == "variant" else ""), r.get("verdict"),
                         r.get("coverageState") + (
                             "" if "live" not in r else " (live: indexable)" if r["live"].get("noindex") is False
                             else " (live: noindex)" if r["live"].get("noindex") else " (live: HTTP %s)"
                             % r["live"].get("status")) if r.get("coverageState") else None,
                         r.get("indexingState"), (r.get("lastCrawlTime") or "")[:16],
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
    ap.add_argument("--key", help="service-account JSON key file (else $GOOGLE_APPLICATION_CREDENTIALS)")
    ap.add_argument("--key-env", metavar="VAR",
                    help="environment variable holding the key JSON or its base64 (default: the first "
                         "set of %s)" % ", ".join(INLINE_KEY_VARS))
    ap.add_argument("--property", help="GA4 property, e.g. properties/123 (default: auto-detect)")
    ap.add_argument("--sc-site", help="Search Console property, e.g. sc-domain:example.com (default: auto)")
    ap.add_argument("--days", type=int, default=28, help="reporting window in days (default 28)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--urls-from-sitemap", action="store_true",
                   help="inspect sitemap URLs (up to --inspect-max, sampled across URL templates when "
                        "there are more) plus the homepage host variant")
    g.add_argument("--url", nargs="+", default=[], help="inspect these URLs")
    ap.add_argument("--inspect-max", type=int, default=INSPECT_CAP,
                    help="URLs to inspect (default %d, max %d; the API allows about 2,000 a day)"
                         % (INSPECT_CAP, INSPECT_HARD_CAP))
    ap.add_argument("--inspect-workers", type=int, default=INSPECT_WORKERS,
                    help="concurrent inspections (default %d)" % INSPECT_WORKERS)
    ap.add_argument("--brand", nargs="+", default=[],
                    help="brand terms (lowercase substrings) to split brand from non-brand queries")
    ap.add_argument("--audit-json",
                    help="a seo_audit.py --json report; its findings are ranked by the search "
                         "impressions of the pages and templates they sit on")
    ap.add_argument("--launched", help="YYYY-MM-DD; flag URLs last crawled before this date")
    ap.add_argument("--json", help="write raw data plus findings to this file")
    ap.add_argument("--markdown", help="also write the Markdown report to this file")
    ap.add_argument("--version", action="version", version="google-console-review " + VERSION)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not 1 <= args.days <= 480:
        return _usage_error("--days must be between 1 and 480")
    if not 1 <= args.inspect_max <= INSPECT_HARD_CAP:
        return _usage_error("--inspect-max must be between 1 and %d" % INSPECT_HARD_CAP)
    if not 1 <= args.inspect_workers <= 8:
        return _usage_error("--inspect-workers must be between 1 and 8")
    audit = None
    if args.audit_json:
        try:
            with open(args.audit_json, encoding="utf-8") as fh:
                audit = json.load(fh)
        except (OSError, ValueError) as e:
            return _usage_error("cannot read --audit-json (%s)" % type(e).__name__)
    launched = None
    if args.launched:
        try:
            launched = dt.date.fromisoformat(args.launched)
        except ValueError:
            return _usage_error("--launched must be YYYY-MM-DD")
    key_path = args.key or (None if args.key_env else os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    key_var = args.key_env or (None if key_path else
                               next((v for v in INLINE_KEY_VARS if os.environ.get(v, "").strip()), None))
    if not key_path and not key_var:
        return _usage_error("no service-account key: pass --key PATH or --key-env VAR, or set "
                            "GOOGLE_APPLICATION_CREDENTIALS (a path) or one of %s (the key JSON or its "
                            "base64)" % ", ".join(INLINE_KEY_VARS))
    try:
        client_email = load_key(key_path) if key_path else load_key_env(key_var)
    except SystemExit as e:
        return e.code
    if key_var:
        log("key read from environment variable " + key_var)

    site_url = normalize_site(args.site)
    site_host = host_of(site_url)
    data = {"tool": "google-console-review", "version": VERSION,
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "site": site_url, "site_host": site_host, "site_scheme": urllib.parse.urlsplit(site_url).scheme,
            "client_email": client_email, "days": args.days, "launched": args.launched, "access": {}}
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
            if not prop and not cands:
                # A key nobody has added to a GA4 property sees no accounts at all. That is an
                # access finding to report, not a reason to throw away the Search Console half.
                ga_error = "the service account sees no GA4 account"
                data["access"]["ga"] = "none"
                log("GA4: %s; continuing with Search Console only" % ga_error)
            elif not prop:
                log("error: no single GA4 property has a web stream for %s. Candidates:" % strip_www(site_host))
                for c in cands:
                    log("  %s  %s  %s" % (c["property"], c["displayName"], ", ".join(c["uris"]) or "(no web stream)"))
                log("pass --property properties/N")
                return 2
            else:
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
            log("Search Console: no property covers %s. Visible properties:%s" % (
                site_url, "" if entries else " none"))
            for e in entries:
                log("  %s  (%s)" % (e.get("siteUrl"), e.get("permissionLevel")))
            if not prop:
                log("error: nothing to review; add the service account to the GA4 property and the "
                    "Search Console property, or pass --property / --sc-site")
                return 2
            sc_error = "no Search Console property covers %s for this service account" % site_url
            data["access"].update(sc="none", sc_visible=[e.get("siteUrl") for e in entries])
            log("continuing with GA4 only")
        else:
            sc_site = chosen["siteUrl"]
    if sc_site:
        permission = next((e.get("permissionLevel") for e in entries if e.get("siteUrl") == sc_site),
                          "not in this account's site list")
        log("Search Console: %s, permission %s" % (sc_site, permission))
        sc_error = None

    data["ga"] = collect_ga(prop, args.days, site_url) if prop else {"error": ga_error or "no property"}
    if sc_site:
        mode = "sitemap" if args.urls_from_sitemap else ("url" if args.url else None)
        data["sc"] = collect_sc(sc_site, permission, site_url, args.days, mode, args.url,
                                inspect_max=args.inspect_max, workers=args.inspect_workers)
        data["sc"]["rollups"] = build_rollups(data, args.brand, audit)
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
