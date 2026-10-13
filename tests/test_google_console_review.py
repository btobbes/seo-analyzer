"""Unit tests for scripts/google_console_review.py. No network: call() and the token are stubbed.

Run with:  python3 -m unittest tests.test_google_console_review -v   (from the repo root)

The canned responses mirror real Admin API, Data API and Search Console answers captured
on 2026-10-12 for a small services site (one GA4 property with a web stream on the apex
host, event retention TWO_MONTHS, only the default 'purchase' key event, Google signals
on with BLENDED identity, the Data API disabled in the key's Cloud project, one sitemap
last downloaded in March, and four of six sitemap URLs unknown to Google).
"""

import contextlib
import copy
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import google_console_review as G                                 # noqa: E402

P = "properties/519189573"
STREAM = P + "/dataStreams/13271860113"
ENABLE_URL = ("https://console.developers.google.com/apis/api/analyticsdata.googleapis.com/"
              "overview?project=267303882129")

SERVICE_DISABLED = {"error": {
    "code": 403,
    "message": ("Google Analytics Data API has not been used in project 267303882129 before or it is "
                "disabled. Enable it by visiting " + ENABLE_URL + " then retry. If you enabled this API "
                "recently, wait a few minutes for the action to propagate to our systems and retry."),
    "status": "PERMISSION_DENIED",
    "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "SERVICE_DISABLED",
                 "domain": "googleapis.com",
                 "metadata": {"service": "analyticsdata.googleapis.com", "consumer": "projects/267303882129",
                              "activationUrl": ENABLE_URL}}]}}
NOT_FOUND = {"raw": "HTTP Error 404: Not Found"}

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://www.btobbes.com/</loc></url>
  <url><loc>https://www.btobbes.com/team</loc></url>
  <url><loc>https://www.btobbes.com/fraud-resources</loc></url>
  <url><loc>https://www.btobbes.com/ai-resources</loc></url>
  <url><loc>https://www.btobbes.com/terms</loc></url>
  <url><loc>https://www.btobbes.com/privacy</loc></url>
</urlset>"""

INDEXED = {"verdict": "PASS", "coverageState": "Submitted and indexed", "robotsTxtState": "ALLOWED",
           "indexingState": "INDEXING_ALLOWED", "lastCrawlTime": "2026-09-24T13:46:02Z",
           "pageFetchState": "SUCCESSFUL", "crawledAs": "MOBILE",
           "referringUrls": ["https://centurionawards.com/about", "https://btobbes.com/sitemap.xml",
                             "https://example.org/third"]}
UNKNOWN = {"verdict": "NEUTRAL", "coverageState": "URL is unknown to Google",
           "robotsTxtState": "ROBOTS_TXT_STATE_UNSPECIFIED", "indexingState": "INDEXING_STATE_UNSPECIFIED",
           "pageFetchState": "PAGE_FETCH_STATE_UNSPECIFIED"}
REDIRECT = {"verdict": "NEUTRAL", "coverageState": "Page with redirect", "robotsTxtState": "ALLOWED",
            "indexingState": "INDEXING_ALLOWED", "lastCrawlTime": "2026-09-20T22:13:29Z",
            "pageFetchState": "SUCCESSFUL", "googleCanonical": "https://www.btobbes.com/",
            "userCanonical": "https://www.btobbes.com/", "crawledAs": "MOBILE"}


def inspection(url):
    if url == "https://btobbes.com/":
        ix = dict(REDIRECT)
    elif url in ("https://www.btobbes.com/", "https://www.btobbes.com/ai-resources"):
        ix = dict(INDEXED, googleCanonical=url, userCanonical=url)
    else:
        ix = dict(UNKNOWN)
    res = {"inspectionResult": {"inspectionResultLink": "https://search.google.com/search-console/inspect?x",
                                "indexStatusResult": ix,
                                "mobileUsabilityResult": {"verdict": "VERDICT_UNSPECIFIED"}}}
    if url == "https://www.btobbes.com/":
        res["inspectionResult"]["richResultsResult"] = {
            "verdict": "PASS", "detectedItems": [{"richResultType": "Breadcrumbs", "items": [{"name": "x"}]},
                                                 {"richResultType": "FAQ", "items": []}]}
    return res


def sa_rows(body):
    dims = body.get("dimensions") or []
    current = body["startDate"] > "2026-09-01"
    if not dims:
        return {"rows": [{"clicks": 0, "impressions": 28 if current else 6, "ctr": 0, "position": 3.0}],
                "responseAggregationType": "byProperty"}
    if dims == ["page"]:
        return {"rows": [{"keys": ["https://www.btobbes.com/"], "clicks": 0, "impressions": 26, "ctr": 0,
                          "position": 3.0},
                         {"keys": ["https://www.btobbes.com/ai-resources"], "clicks": 0, "impressions": 11,
                          "ctr": 0, "position": 3.1}]}
    if dims == ["query"]:
        return {"responseAggregationType": "byProperty"}           # thresholded: no rows at all
    if dims == ["country"]:
        return {"rows": [{"keys": ["usa"], "clicks": 0, "impressions": 20, "ctr": 0, "position": 3.6}]}
    if dims == ["device"]:
        return {"rows": [{"keys": ["DESKTOP"], "clicks": 0, "impressions": 27, "ctr": 0, "position": 3.1}]}
    return {}


BASE = {
    # Admin API
    "v1beta/accountSummaries": {"accountSummaries": [{
        "name": "accountSummaries/379953206", "account": "accounts/379953206", "displayName": "btobbes",
        "propertySummaries": [
            {"property": P, "displayName": "Btobbes", "propertyType": "PROPERTY_TYPE_ORDINARY",
             "parent": "accounts/379953206"},
            {"property": "properties/543665599", "displayName": "Centurion Awards",
             "propertyType": "PROPERTY_TYPE_ORDINARY", "parent": "accounts/379953206"}]}]},
    "v1beta/properties/543665599/dataStreams": {"dataStreams": [{
        "name": "properties/543665599/dataStreams/1", "type": "WEB_DATA_STREAM",
        "displayName": "centurionawards.com",
        "webStreamData": {"measurementId": "G-OTHER", "defaultUri": "https://centurionawards.com"}}]},
    "v1beta/" + P + "/dataStreams": {"dataStreams": [{
        "name": STREAM, "type": "WEB_DATA_STREAM", "displayName": "btobbes.com",
        "webStreamData": {"measurementId": "G-LHZ92FTHKQ", "defaultUri": "https://btobbes.com"}}]},
    "v1beta/" + P + "/dataRetentionSettings": {
        "name": P + "/dataRetentionSettings", "eventDataRetention": "TWO_MONTHS",
        "resetUserDataOnNewActivity": True, "userDataRetention": "FOURTEEN_MONTHS"},
    "v1alpha/" + P + "/googleSignalsSettings": {
        "name": P + "/googleSignalsSettings", "state": "GOOGLE_SIGNALS_ENABLED",
        "consent": "GOOGLE_SIGNALS_CONSENT_CONSENTED"},
    "v1alpha/" + P + "/reportingIdentitySettings": {"reportingIdentity": "BLENDED"},
    "v1alpha/" + STREAM + "/enhancedMeasurementSettings": {
        "streamEnabled": True, "scrollsEnabled": True, "outboundClicksEnabled": True,
        "siteSearchEnabled": False, "searchQueryParameter": "q,s"},
    "v1beta/" + STREAM + "/measurementProtocolSecrets": {"measurementProtocolSecrets": [
        {"name": STREAM + "/measurementProtocolSecrets/9", "displayName": "server",
         "secretValue": "SHOULD-NEVER-APPEAR"}]},
    "v1beta/" + P + "/keyEvents": {"keyEvents": [{
        "name": P + "/keyEvents/13271860110", "eventName": "purchase", "countingMethod": "ONCE_PER_EVENT"}]},
    "v1beta/" + P + "/customDimensions": {},
    "v1beta/" + P + "/customMetrics": {},
    "v1beta/" + P + "/googleAdsLinks": {"googleAdsLinks": [{
        "name": P + "/googleAdsLinks/1", "customerId": "8410928450", "adsPersonalizationEnabled": False}]},
    "v1alpha/" + P + "/bigQueryLinks": {},
    "v1alpha/" + P + "/attributionSettings": {
        "reportingAttributionModel": "PAID_AND_ORGANIC_CHANNELS_DATA_DRIVEN",
        "acquisitionConversionEventLookbackWindow": "ACQUISITION_CONVERSION_EVENT_LOOKBACK_WINDOW_30_DAYS",
        "otherConversionEventLookbackWindow": "OTHER_CONVERSION_EVENT_LOOKBACK_WINDOW_90_DAYS"},
    "v1alpha/" + P + "/reportingDataAnnotations": (404, NOT_FOUND),
    "v1beta/" + P: {"name": P, "displayName": "Btobbes", "timeZone": "America/Los_Angeles",
                    "currencyCode": "USD", "industryCategory": "INTERNET_AND_TELECOM",
                    "serviceLevel": "GOOGLE_ANALYTICS_STANDARD"},
    # Data API
    ":runReport": (403, SERVICE_DISABLED),
    ":runRealtimeReport": (403, SERVICE_DISABLED),
    # Search Console
    "webmasters/v3/sites/sc-domain%3Abtobbes.com/sitemaps": {"sitemap": [{
        "path": "https://www.btobbes.com/sitemap.xml", "lastSubmitted": "2026-03-02T23:52:25.054Z",
        "isPending": False, "isSitemapsIndex": False, "lastDownloaded": "2026-03-12T20:12:24.488Z",
        "warnings": "0", "errors": "0", "contents": [{"type": "web", "submitted": "6", "indexed": "0"}]}]},
    "webmasters/v3/sites": {"siteEntry": [
        {"siteUrl": "https://btobbes.com/", "permissionLevel": "siteFullUser"},
        {"siteUrl": "sc-domain:btobbes.com", "permissionLevel": "siteFullUser"}]},
    "https://www.btobbes.com/sitemap.xml": SITEMAP_XML,
}


class FakeGoogle:
    """Answers call() from canned responses keyed by URL substring. A key must end the URL
    path (query string ignored), so 'v1beta/properties/519189573' answers only the property
    itself and never '.../keyEvents' or the Data API's '...:runReport'."""

    def __init__(self, overrides=None):
        self.routes = copy.deepcopy(BASE)
        self.routes.update(overrides or {})
        self.calls = []

    def __call__(self, url, body=None, auth=True, raw=False, timeout=30):
        method = "POST" if body is not None else "GET"
        G._guard(url, method, auth)                 # the real guard still applies
        self.calls.append((method, url, body, auth))
        if "searchAnalytics/query" in url:
            return 200, sa_rows(body)
        if "urlInspection/index:inspect" in url:
            return 200, inspection(body["inspectionUrl"])
        path = url.split("?")[0]
        keys = [k for k in self.routes if path.endswith(k)]
        if not keys:
            return 404, ("" if raw else NOT_FOUND)
        v = self.routes[max(keys, key=len)]
        status, payload = v if isinstance(v, tuple) else (200, v)
        return status, copy.deepcopy(payload)


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def write_key(dirpath):
    p = os.path.join(dirpath, "key.json")
    with open(p, "w") as fh:
        json.dump({"type": "service_account", "client_email": "reviewer@proj.iam.gserviceaccount.com",
                   "private_key": "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----\n",
                   "project_id": "proj"}, fh)
    return p


class StubbedNetwork(unittest.TestCase):
    """Base: swap in the fake call(), a fake token, and no inspection delay."""

    def setUp(self):
        self._saved = (G.call, G.get_token, G.INSPECT_DELAY)
        self._env = os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        G.get_token = lambda: "fake-token"
        G.INSPECT_DELAY = 0
        self.tmp = tempfile.TemporaryDirectory()
        self.key = write_key(self.tmp.name)

    def tearDown(self):
        G.call, G.get_token, G.INSPECT_DELAY = self._saved
        if self._env is not None:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self._env
        self.tmp.cleanup()

    def run_main(self, argv, overrides=None):
        G.call = self.fake = FakeGoogle(overrides)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = G.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class TestEndToEnd(StubbedNetwork):

    def test_full_run_selects_property_and_reports(self):
        jpath = os.path.join(self.tmp.name, "out.json")
        code, out, err = self.run_main(["--site", "https://www.btobbes.com", "--key", self.key,
                                        "--urls-from-sitemap", "--launched", "2026-10-10", "--json", jpath])
        self.assertEqual(code, 0, err)
        self.assertIn("GA4: selected " + P, err)
        self.assertIn("sc-domain:btobbes.com, permission siteFullUser", err)
        data = read_json(jpath)
        self.assertEqual(data["ga"]["property_name"], P)
        self.assertEqual(data["sc"]["site"], "sc-domain:btobbes.com")
        titles = {f["title"]: f["severity"] for f in data["findings"]}
        self.assertEqual(titles["GA4 event data retention is TWO_MONTHS"], "HIGH")
        self.assertEqual(titles["No GA4 key event for leads"], "HIGH")
        self.assertEqual(titles["Sitemap not fetched by Google in over 30 days"], "HIGH")
        self.assertEqual(titles["Pages Google has never seen"], "HIGH")
        self.assertEqual(titles["Google's copy predates the launch"], "HIGH")
        self.assertEqual(titles["Google signals on with Blended reporting identity (thresholding)"], "LOW")
        self.assertEqual(titles["GA4 web stream URL differs from the site host"], "LOW")
        self.assertEqual(titles["No GA4 custom dimensions registered"], "INFO")
        self.assertEqual(titles["No queries visible (anonymized by Search Console)"], "INFO")
        self.assertEqual(titles["GA4 Data API is not enabled for the service account's project"], "INFO")
        self.assertNotIn("Non-canonical host variants are collecting impressions", titles)
        # 6 sitemap URLs plus the apex homepage, which is not counted as a sitemap URL
        res = data["sc"]["inspection"]["results"]
        self.assertEqual(len(res), 7)
        self.assertEqual(res[-1]["url"], "https://btobbes.com/")
        self.assertEqual(res[-1]["source"], "variant")
        unknown = next(f for f in data["findings"] if f["title"] == "Pages Google has never seen")
        self.assertIn("4 of 6", unknown["observed"])
        self.assertEqual(len(res[0]["referringUrls"]), 2)
        self.assertEqual(res[0]["richResults"], {"verdict": "PASS", "types": ["Breadcrumbs", "FAQ"]})
        # a 404 on an Admin resource is recorded, not fatal
        ann = data["ga"]["items"]["reportingDataAnnotations"]
        self.assertEqual((ann["ok"], ann["status"], ann["note"]), (False, 404, G.NOT_READABLE))
        # secrets: MP secret values and the token never leave the process
        blob = json.dumps(data) + out + err
        self.assertNotIn("SHOULD-NEVER-APPEAR", blob)
        self.assertNotIn("fake-token", blob)
        self.assertNotIn("PRIVATE KEY", blob)
        self.assertIn("# Google console review: https://www.btobbes.com/", out)
        self.assertIn("| HIGH | GA4 event data retention is TWO_MONTHS", out)

    def test_only_get_and_read_only_posts(self):
        self.run_main(["--site", "https://www.btobbes.com", "--key", self.key, "--urls-from-sitemap"])
        posts = [u for m, u, b, a in self.fake.calls if m == "POST"]
        self.assertTrue(posts)
        for u in posts:
            self.assertRegex(u, r"(:runReport|:runRealtimeReport|/searchAnalytics/query|urlInspection/index:inspect)$")
        self.assertFalse([u for m, u, b, a in self.fake.calls if not a and "googleapis.com" in u])
        self.assertFalse([u for m, u, b, a in self.fake.calls if a and "googleapis.com" not in u])

    def test_service_disabled_is_reported_with_enable_url(self):
        code, out, err = self.run_main(["--site", "https://www.btobbes.com", "--key", self.key])
        self.assertEqual(code, 0, err)
        self.assertIn("SERVICE_DISABLED", out)
        self.assertIn(ENABLE_URL, out)
        # one runReport is enough to learn the API is off; no realtime call either
        reports = [u for m, u, b, a in self.fake.calls if ":run" in u]
        self.assertEqual(len(reports), 1)

    def test_service_disabled_enable_url_from_message_only(self):
        body = copy.deepcopy(SERVICE_DISABLED)
        body["error"]["details"] = []
        e = G.parse_error(403, body)
        self.assertEqual(e["reason"], "SERVICE_DISABLED")
        self.assertEqual(e["enable_url"], ENABLE_URL)

    def test_data_api_available(self):
        rep = {"dimensionHeaders": [{"name": "sessionDefaultChannelGroup"}],
               "metricHeaders": [{"name": "sessions", "type": "TYPE_INTEGER"}],
               "rows": [{"dimensionValues": [{"value": "Direct"}], "metricValues": [{"value": "12"}]}],
               "totals": [{"dimensionValues": [{"value": "RESERVED_TOTAL"}], "metricValues": [{"value": "12"}]}],
               "rowCount": 1, "metadata": {"subjectToThresholding": True}}
        code, out, err = self.run_main(["--site", "https://www.btobbes.com", "--key", self.key],
                                       {":runReport": rep, ":runRealtimeReport": {
                                           "metricHeaders": [{"name": "activeUsers"}],
                                           "rows": [{"metricValues": [{"value": "1"}]}]}})
        self.assertEqual(code, 0, err)
        self.assertIn("| Direct | 12 |", out)
        self.assertIn("subject to thresholding", out)
        self.assertNotIn("Data API is not enabled", out)

    def test_no_matching_property_exits_2_with_candidates(self):
        code, out, err = self.run_main(["--site", "https://www.example.org", "--key", self.key,
                                        "--sc-site", "sc-domain:example.org"])
        self.assertEqual(code, 2)
        self.assertIn(P, err)
        self.assertIn("properties/543665599", err)
        self.assertIn("--property", err)

    def test_explicit_property_skips_autodetect(self):
        code, out, err = self.run_main(["--site", "https://www.btobbes.com", "--key", self.key,
                                        "--property", "519189573"])
        self.assertEqual(code, 0, err)
        self.assertFalse([u for m, u, b, a in self.fake.calls if "accountSummaries" in u])

    def test_admin_403_recorded_not_fatal(self):
        denied = {"error": {"code": 403, "message": "Request had insufficient authentication scopes.",
                            "status": "PERMISSION_DENIED"}}
        jpath = os.path.join(self.tmp.name, "o.json")
        code, out, err = self.run_main(["--site", "https://www.btobbes.com", "--key", self.key,
                                        "--json", jpath],
                                       {"v1alpha/" + P + "/googleSignalsSettings": (403, denied)})
        self.assertEqual(code, 0, err)
        gs = read_json(jpath)["ga"]["items"]["googleSignalsSettings"]
        self.assertEqual((gs["ok"], gs["status"], gs["note"]), (False, 403, G.NOT_READABLE))
        self.assertIn("not readable with this scope/API (HTTP 403)", out)


class TestKeyHandling(StubbedNetwork):

    def test_missing_key_exits_nonzero_with_message(self):
        code, out, err = self.run_main(["--site", "https://www.btobbes.com"])
        self.assertNotEqual(code, 0)
        self.assertIn("--key", err)
        self.assertIn("GOOGLE_APPLICATION_CREDENTIALS", err)
        self.assertEqual(self.fake.calls, [])

    def test_key_path_that_does_not_exist(self):
        code, out, err = self.run_main(["--site", "https://www.btobbes.com", "--key",
                                        os.path.join(self.tmp.name, "nope.json")])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)

    def test_key_from_env(self):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.key
        try:
            code, out, err = self.run_main(["--site", "https://www.btobbes.com"])
        finally:
            del os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
        self.assertEqual(code, 0, err)

    def test_not_a_service_account_key(self):
        p = os.path.join(self.tmp.name, "user.json")
        with open(p, "w") as fh:
            json.dump({"type": "authorized_user", "client_id": "x"}, fh)
        code, out, err = self.run_main(["--site", "https://www.btobbes.com", "--key", p])
        self.assertEqual(code, 2)
        self.assertIn("not a service-account key", err)

    def test_version(self):
        code, out, err = self.run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("google-console-review 0.1.0", out + err)


class TestGuards(unittest.TestCase):

    def test_refuses_write_posts(self):
        with self.assertRaises(ValueError):
            G._guard("https://analyticsadmin.googleapis.com/v1beta/properties/1/keyEvents", "POST", True)
        with self.assertRaises(ValueError):
            G._guard("https://www.googleapis.com/webmasters/v3/sites/x/sitemaps/y", "PUT", True)

    def test_refuses_token_to_other_hosts(self):
        with self.assertRaises(ValueError):
            G._guard("https://www.btobbes.com/sitemap.xml", "GET", True)
        G._guard("https://www.btobbes.com/sitemap.xml", "GET", False)

    def test_allows_read_only_queries(self):
        for u in ("https://analyticsdata.googleapis.com/v1beta/properties/1:runReport",
                  "https://analyticsdata.googleapis.com/v1beta/properties/1:runRealtimeReport",
                  "https://www.googleapis.com/webmasters/v3/sites/sc-domain%3Ax.com/searchAnalytics/query",
                  "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"):
            G._guard(u, "POST", True)

    def test_imports_without_cryptography(self):
        # import the module in a child interpreter where 'cryptography' cannot be imported
        code = ("import sys; sys.modules['cryptography'] = None; sys.path.insert(0, %r); "
                "import google_console_review as G; print(G.VERSION)" % os.path.dirname(G.__file__))
        r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), G.VERSION)

    def test_missing_cryptography_gives_clear_error(self):
        saved_key, saved_tok = G._KEY, G._TOKEN
        names = ("cryptography", "cryptography.hazmat", "cryptography.hazmat.primitives",
                 "cryptography.hazmat.primitives.asymmetric")
        saved_mods = {n: sys.modules.get(n) for n in names}
        G._KEY, G._TOKEN = {"client_email": "a@b", "private_key": "x"}, None
        for n in names:
            sys.modules[n] = None                   # makes 'import cryptography...' raise ImportError
        try:
            with self.assertRaises(G.AuthError) as cm:
                G.get_token()
            self.assertIn("pip install cryptography", str(cm.exception))
        finally:
            G._KEY, G._TOKEN = saved_key, saved_tok
            for n, m in saved_mods.items():
                if m is None:
                    sys.modules.pop(n, None)
                else:
                    sys.modules[n] = m


class TestSelection(unittest.TestCase):

    CANDS = [{"property": P, "displayName": "Btobbes", "uris": ["https://btobbes.com"]},
             {"property": "properties/2", "displayName": "Other", "uris": ["https://centurionawards.com"]},
             {"property": "properties/3", "displayName": "App only", "uris": []}]

    def test_www_insensitive_match(self):
        self.assertEqual(G.pick_property(self.CANDS, "www.btobbes.com"), P)
        self.assertEqual(G.pick_property(self.CANDS, "btobbes.com"), P)

    def test_no_match_and_ambiguous(self):
        self.assertIsNone(G.pick_property(self.CANDS, "www.example.org"))
        two = self.CANDS + [{"property": "properties/4", "displayName": "Dup", "uris": ["https://www.btobbes.com/"]}]
        self.assertIsNone(G.pick_property(two, "www.btobbes.com"))

    def test_sc_prefers_domain_property(self):
        entries = [{"siteUrl": "https://www.btobbes.com/", "permissionLevel": "siteOwner"},
                   {"siteUrl": "sc-domain:btobbes.com", "permissionLevel": "siteFullUser"},
                   {"siteUrl": "sc-domain:other.com", "permissionLevel": "siteOwner"}]
        self.assertEqual(G.pick_sc_site(entries, "https://www.btobbes.com/")["siteUrl"], "sc-domain:btobbes.com")

    def test_sc_falls_back_to_url_prefix(self):
        entries = [{"siteUrl": "https://btobbes.com/", "permissionLevel": "siteFullUser"},
                   {"siteUrl": "https://www.btobbes.com/", "permissionLevel": "siteRestrictedUser"}]
        self.assertEqual(G.pick_sc_site(entries, "https://www.btobbes.com")["siteUrl"], "https://www.btobbes.com/")
        self.assertIsNone(G.pick_sc_site(entries[:1], "https://www.btobbes.com"))


def good_data():
    """Collected data in which no rule should fire."""
    return {
        "site_host": "www.btobbes.com", "site_scheme": "https",
        "ga": {"items": {
            "dataRetentionSettings": {"ok": True, "data": {"eventDataRetention": "FOURTEEN_MONTHS"}},
            "keyEvents": {"ok": True, "data": [{"eventName": "purchase"}, {"eventName": "generate_lead"}]},
            "customDimensions": {"ok": True, "data": [{"parameterName": "offer", "scope": "EVENT"}]},
            "googleSignalsSettings": {"ok": True, "data": {"state": "GOOGLE_SIGNALS_ENABLED"}},
            "reportingIdentitySettings": {"ok": True, "data": {"reportingIdentity": "DEVICE_BASED"}}},
            "streams": [{"displayName": "web", "defaultUri": "https://www.btobbes.com"}],
            "data_api": {"available": True}},
        "sc": {"site": "sc-domain:btobbes.com",
               "sitemaps": {"ok": True, "data": [{"path": "https://www.btobbes.com/sitemap.xml",
                                                  "lastSubmitted": "2026-10-10T00:00:00Z",
                                                  "lastDownloaded": "2026-10-11T00:00:00Z"}]},
               "analytics": {"current": {
                   "range": ["2026-09-14", "2026-10-11"],
                   "totals": {"ok": True, "rows": [{"clicks": 1, "impressions": 40}]},
                   "page": {"ok": True, "rows": [{"keys": ["https://www.btobbes.com/"], "impressions": 40}]},
                   "query": {"ok": True, "rows": [{"keys": ["btobbes"], "impressions": 5}]}}},
               "inspection": {"results": [
                   {"url": "https://www.btobbes.com/", "ok": True, "source": "sitemap",
                    "coverageState": "Submitted and indexed", "lastCrawlTime": "2026-10-11T03:00:00Z"},
                   {"url": "https://btobbes.com/", "ok": True, "source": "variant",
                    "coverageState": "URL is unknown to Google"}]}}}


class TestRules(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 12, 12, tzinfo=dt.timezone.utc)
    LAUNCH = dt.date(2026, 10, 10)

    def titles(self, data, launched=LAUNCH):
        return {f["title"]: f["severity"] for f in G.evaluate(data, launched, self.NOW)}

    def test_all_silent_on_a_healthy_setup(self):
        self.assertEqual(self.titles(good_data()), {})

    def test_retention(self):
        d = good_data()
        d["ga"]["items"]["dataRetentionSettings"]["data"]["eventDataRetention"] = "TWO_MONTHS"
        self.assertEqual(self.titles(d), {"GA4 event data retention is TWO_MONTHS": "HIGH"})
        d["ga"]["items"]["dataRetentionSettings"] = {"ok": False, "status": 403}
        self.assertEqual(self.titles(d), {})

    def test_key_events(self):
        d = good_data()
        d["ga"]["items"]["keyEvents"]["data"] = []
        self.assertEqual(self.titles(d), {"No GA4 key event for leads": "HIGH"})
        d["ga"]["items"]["keyEvents"]["data"] = [{"eventName": "purchase"}]
        self.assertEqual(self.titles(d), {"No GA4 key event for leads": "HIGH"})

    def test_custom_dimensions(self):
        d = good_data()
        d["ga"]["items"]["customDimensions"]["data"] = []
        self.assertEqual(self.titles(d), {"No GA4 custom dimensions registered": "INFO"})

    def test_signals_blended(self):
        d = good_data()
        d["ga"]["items"]["reportingIdentitySettings"]["data"]["reportingIdentity"] = "BLENDED"
        self.assertEqual(self.titles(d), {"Google signals on with Blended reporting identity (thresholding)": "LOW"})
        d["ga"]["items"]["googleSignalsSettings"]["data"]["state"] = "GOOGLE_SIGNALS_DISABLED"
        self.assertEqual(self.titles(d), {})

    def test_stream_host(self):
        d = good_data()
        d["ga"]["streams"][0]["defaultUri"] = "https://btobbes.com"
        self.assertEqual(self.titles(d), {"GA4 web stream URL differs from the site host": "LOW"})

    def test_stale_sitemap(self):
        d = good_data()
        d["sc"]["sitemaps"]["data"][0]["lastDownloaded"] = "2026-03-12T20:12:24.488Z"
        self.assertEqual(self.titles(d), {"Sitemap not fetched by Google in over 30 days": "HIGH"})
        d["sc"]["sitemaps"]["data"][0]["lastDownloaded"] = "2026-09-20T00:00:00Z"   # 22 days: fine
        self.assertEqual(self.titles(d), {})

    def test_no_sitemap(self):
        d = good_data()
        d["sc"]["sitemaps"]["data"] = []
        self.assertEqual(self.titles(d), {"No sitemap submitted to Search Console": "HIGH"})

    def test_unknown_and_prelaunch(self):
        d = good_data()
        d["sc"]["inspection"]["results"][0]["coverageState"] = "URL is unknown to Google"
        self.assertEqual(self.titles(d), {"Pages Google has never seen": "HIGH"})
        d = good_data()
        d["sc"]["inspection"]["results"][0]["lastCrawlTime"] = "2026-09-24T13:46:02Z"
        self.assertEqual(self.titles(d), {"Google's copy predates the launch": "HIGH"})
        self.assertEqual(self.titles(d, launched=None), {})        # no --launched, no rule

    def test_variant_impressions(self):
        d = good_data()
        d["sc"]["analytics"]["current"]["page"]["rows"].append(
            {"keys": ["https://btobbes.com/"], "impressions": 3})
        self.assertEqual(self.titles(d), {"Non-canonical host variants are collecting impressions": "MEDIUM"})
        d = good_data()
        d["sc"]["analytics"]["current"]["page"]["rows"].append(
            {"keys": ["http://www.btobbes.com/team"], "impressions": 1})
        self.assertIn("Non-canonical host variants are collecting impressions", self.titles(d))

    def test_data_api_missing(self):
        d = good_data()
        d["ga"]["data_api"] = {"available": False, "status": 403, "reason": "SERVICE_DISABLED",
                               "enable_url": ENABLE_URL}
        f = G.evaluate(d, None, self.NOW)
        self.assertEqual([(x["severity"], x["title"]) for x in f],
                         [("INFO", "GA4 Data API is not enabled for the service account's project")])
        self.assertIn(ENABLE_URL, f[0]["fix"])

    def test_thresholded_queries(self):
        d = good_data()
        d["sc"]["analytics"]["current"]["query"]["rows"] = []
        self.assertEqual(self.titles(d), {"No queries visible (anonymized by Search Console)": "INFO"})
        d["sc"]["analytics"]["current"]["totals"]["rows"] = [{"clicks": 0, "impressions": 0}]
        self.assertEqual(self.titles(d), {})

    def test_findings_shape_and_order(self):
        d = good_data()
        d["ga"]["items"]["customDimensions"]["data"] = []
        d["ga"]["items"]["keyEvents"]["data"] = []
        f = G.evaluate(d, None, self.NOW)
        self.assertEqual([x["severity"] for x in f], ["HIGH", "INFO"])
        for x in f:
            self.assertTrue({"severity", "title", "observed", "fix"} <= set(x))


class TestHelpers(unittest.TestCase):

    def test_date_windows(self):
        cur, prev = G.date_windows(28, dt.date(2026, 10, 12))
        self.assertEqual(cur, ("2026-09-14", "2026-10-11"))
        self.assertEqual(prev, ("2026-08-17", "2026-09-13"))

    def test_variant_homepage(self):
        self.assertEqual(G.variant_homepage("https://www.btobbes.com/"), "https://btobbes.com/")
        self.assertEqual(G.variant_homepage("https://btobbes.com/"), "https://www.btobbes.com/")

    def test_sitemap_index_and_cap(self):
        idx = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
               '<sitemap><loc>https://x.com/a.xml</loc></sitemap></sitemapindex>')
        urls = "".join("<url><loc>https://x.com/p%d</loc></url>" % i for i in range(60))
        child = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">%s</urlset>' % urls
        saved = G.call
        G.call = lambda u, body=None, auth=True, raw=False, timeout=30: \
            (200, idx) if u.endswith("index.xml") else (200, child)
        try:
            found, total, used, errs = G.sitemap_urls(["https://x.com/index.xml"], "https://x.com/")
        finally:
            G.call = saved
        self.assertEqual((len(found), total, errs), (50, 60, []))
        self.assertEqual(used, ["https://x.com/index.xml", "https://x.com/a.xml"])


if __name__ == "__main__":
    unittest.main()
