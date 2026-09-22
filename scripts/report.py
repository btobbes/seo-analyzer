"""
report.py — render an audit dict (from seo_audit.run_audit) to a styled HTML report,
and convert that HTML to PDF using headless Chrome/Chromium.

Kept separate from the analysis so the look can evolve without touching the crawler.
"""

import html as _html
import os
import shutil
import subprocess

SEV_COLOR = {
    "CRITICAL": "#b91c1c", "HIGH": "#c2410c", "MEDIUM": "#b45309",
    "LOW": "#475569", "INFO": "#1d4ed8",
}
SEV_BG = {
    "CRITICAL": "#fee2e2", "HIGH": "#ffedd5", "MEDIUM": "#fef3c7",
    "LOW": "#f1f5f9", "INFO": "#dbeafe",
}
SEV_ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}


def top_priorities(r, limit=8):
    """The highest-leverage fixes across the whole audit: most severe first, then
    lowest effort. Deduplicated by title (site-wide issues repeat per page) and with
    INFO items excluded — this is the 'do these first' list."""
    buckets = [r.get("site_findings", []),
               r.get("social", {}).get("findings", []),
               r.get("footprint", {}).get("findings", []),
               r.get("psi", {}).get("findings", [])]
    buckets += [p.get("findings", []) for p in r.get("pages", [])]
    allf = [fi for b in buckets for fi in b if fi["severity"] != "INFO"]
    allf.sort(key=lambda x: (SEV_ORDER[x["severity"]], x["effort"], -x["impact"]))
    seen, out = set(), []
    for fi in allf:
        if fi["title"] in seen:
            continue
        seen.add(fi["title"])
        out.append(fi)
        if len(out) >= limit:
            break
    return out


def esc(s):
    return _html.escape(str(s if s is not None else ""))


def score_color(n):
    if n >= 90:
        return "#15803d"
    if n >= 70:
        return "#65a30d"
    if n >= 50:
        return "#b45309"
    return "#b91c1c"


def sev_badge(sev):
    return (f'<span class="sev" style="color:{SEV_COLOR[sev]};background:{SEV_BG[sev]}">'
            f'{esc(sev)}</span>')


def findings_table(findings):
    if not findings:
        return '<p class="ok">No issues found. ✓</p>'
    rows = []
    for fi in findings:
        rows.append(
            "<tr>"
            f"<td>{sev_badge(fi['severity'])}</td>"
            f"<td class='finding'>{esc(fi['title'])}</td>"
            f"<td class='cat'>{esc(fi['category'])}</td>"
            f"<td class='ie'>{fi['impact']}/{fi['effort']}</td>"
            f"<td class='obs'>{esc(fi['observed'])}</td>"
            f"<td class='fix'>{esc(fi['fix'])}</td>"
            "</tr>"
        )
    return (
        "<table class='findings'><thead><tr>"
        "<th>Sev</th><th>Finding</th><th>Cat</th><th>I/E</th>"
        "<th>Observed</th><th>Fix</th></tr></thead><tbody>"
        + "".join(rows) + "</tbody></table>"
    )


ZONE_LABEL = {"title": "Title", "h1": "H1", "heading": "H2–H3", "meta": "Meta",
              "url": "URL", "schema": "Schema", "body": "Body"}
PROMINENT_ZONES = {"title", "h1", "heading"}  # rendered with emphasis


def _zone_chips(zones):
    chips = []
    for z in zones:
        strong = z in PROMINENT_ZONES
        bg = "#dbeafe" if strong else "#f1f5f9"
        col = "#1d4ed8" if strong else "#64748b"
        chips.append(f"<span class='zchip' style='background:{bg};color:{col}'>{esc(ZONE_LABEL.get(z, z))}</span>")
    return "".join(chips)


def keyword_block(r):
    kw = r.get("keywords") or {}
    if not kw.get("available"):
        note = esc(kw.get("note", "Keyword analysis unavailable — no crawlable on-page text."))
        return ("<h2>Keyword focus</h2>"
                f"<p class='status' style='margin-top:0'>{note}</p>")
    rows = []
    for t in kw["terms"]:
        kind = "phrase" if t["n"] >= 2 else "word"
        bar = (f"<span class='barwrap'><span class='bar' style='width:{t['prominence']}%'></span></span>"
               f"<span class='barnum'>{t['prominence']}</span>")
        rows.append(
            "<tr>"
            f"<td class='finding'>{esc(t['term'])}</td>"
            f"<td class='cat'>{kind}</td>"
            f"<td>{_zone_chips(t['zones'])}</td>"
            f"<td class='ie'>{t['body_count'] or '—'}</td>"
            f"<td class='prom'>{bar}</td>"
            "</tr>"
        )
    table = ("<table class='findings kw'><thead><tr>"
             "<th>Keyword</th><th>Type</th><th>Appears in (SEO-weighted)</th>"
             "<th>Body uses</th><th>Prominence</th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table>")
    insights = ""
    if kw.get("insights"):
        insights = "<ul class='kwnotes'>" + "".join(f"<li>{esc(i)}</li>" for i in kw["insights"]) + "</ul>"
    method = ("<p class='status' style='margin:0 0 8px'>What the site actually emphasizes, ranked by "
              "<b>where</b> each term appears — not raw frequency. Placement is weighted Title ×5, H1 ×4, "
              "H2–H3 / meta ×3, URL / schema ×2, body ×1 (body capped so repetition can't outrank deliberate "
              "placement). The “Appears in” chips show which signals give each keyword its weight; blue chips "
              "(Title / H1 / headings) are the ones search engines and AI engines trust most.</p>")
    return f"<h2>Keyword focus</h2>{method}{table}{insights}"


def footprint_block(r):
    fp = r.get("footprint") or {}
    social = fp.get("social", [])
    google = fp.get("google", {}) or {}
    parts = ["<h2>Social &amp; local footprint</h2>"]

    # --- social profiles ---
    if social:
        rows = "".join(
            "<tr>"
            f"<td class='finding'>{esc(s['platform'])}</td>"
            f"<td class='obs'><a href='{esc(s['url'])}'>{esc(s['url'])}</a></td>"
            f"<td class='cat'>{'✓ yes' if s['in_sameas'] else '— no'}</td>"
            "</tr>"
            for s in social
        )
        parts.append(
            "<p class='status' style='margin:0 0 4px'>Social profiles linked from the crawled pages. "
            "<b>In sameAs schema</b> shows whether the profile is declared in Organization/LocalBusiness "
            "structured data — the signal that ties a profile to your entity in Google's knowledge graph.</p>"
            "<table class='findings'><thead><tr><th>Platform</th><th>Profile</th>"
            "<th>In sameAs schema</th></tr></thead><tbody>" + rows + "</tbody></table>")
    elif fp.get("no_links"):
        parts.append("<p class='status' style='margin:0 0 6px'>Could not evaluate — the crawled pages "
                     "expose no links in their raw HTML (client-rendered site), so social profiles "
                     "can't be detected. Fixing server-side rendering will also make these visible.</p>")
    else:
        parts.append("<p class='status' style='margin:0 0 6px'>No social profiles were linked from the "
                     "crawled pages. Add links to your active profiles (and list them in Organization "
                     "<code>sameAs</code> schema) so search and AI engines associate them with the business.</p>")

    # --- Google Business Profile / local ---
    local = google.get("local")
    gbp_links = google.get("gbp_links", [])
    maps_links = google.get("maps_links", [])
    parts.append("<h3 style='font-size:13px;margin:16px 0 4px'>Google Business Profile &amp; local signals</h3>")
    link_bits = []
    for u in gbp_links:
        link_bits.append(f"<a href='{esc(u)}'>{esc(u)}</a> <span class='cat'>(Business Profile link)</span>")
    for u in maps_links:
        link_bits.append(f"<a href='{esc(u)}'>{esc(u)}</a> <span class='cat'>(Google Maps link)</span>")
    if link_bits:
        parts.append("<p class='status' style='margin:0 0 4px'>" + "<br>".join(link_bits) + "</p>")
    else:
        parts.append("<p class='status' style='margin:0 0 4px'>No Google Maps or Business Profile link "
                     "found on the site. If this is a local business, link your Google Business Profile.</p>")
    if local:
        order = [("name", "Business name"), ("rating", "Rating (self-declared)"),
                 ("telephone", "Phone"), ("address", "Address"), ("hours", "Hours"),
                 ("geo", "Geo (lat, long)"), ("price_range", "Price range")]
        mrows = "".join(f"<tr><td>{esc(lbl)}</td><td>{esc(local[k])}</td></tr>"
                        for k, lbl in order if local.get(k))
        if mrows:
            parts.append("<p class='status' style='margin:8px 0 4px'>Local-business details the site "
                         "publishes in its own structured data (this mirrors, but is not pulled live from, "
                         "the Google Business Profile):</p>"
                         "<table class='kv'><thead><tr><th>Field</th><th>Value</th></tr></thead><tbody>"
                         + mrows + "</tbody></table>")
    parts.append("<p class='status' style='margin:6px 0 0;font-size:11.5px'>Note: live Google Business "
                 "Profile metrics (current rating, review count, hours, category) require the Google Places "
                 "API and aren't queried here. Ratings shown above are self-declared in the site's schema and "
                 "should be verified against the actual Business Profile.</p>")

    if fp.get("findings"):
        parts.append(findings_table(fp["findings"]))
    return "".join(parts)


def health_block(r):
    rows = r.get("health") or []
    if not rows:
        return ""
    trs = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in rows)
    return ("<h2>Site health checks</h2>"
            "<p class='status' style='margin:0 0 4px'>One-shot infrastructure checks: host-variant "
            "redirects, 404 handling, TLS certificate and domain registration, contact/trust pages, where "
            "internal links really point, robots.txt platform signals, email authentication, and the "
            "measurement stack.</p>"
            "<table class='kv'><thead><tr><th>Check</th><th>Result</th></tr></thead><tbody>"
            + trs + "</tbody></table>")


def coverage_block(r):
    cov = r.get("coverage") or []
    if not cov:
        return ""
    trs = "".join(
        f"<tr><td><code>{esc(c['template'])}</code></td><td>{c['urls']}</td>"
        f"<td style='color:{'#b91c1c' if not c['audited'] and c['urls'] >= 5 else 'inherit'}'>{c['audited']}</td></tr>"
        for c in cov[:12])
    gaps = r.get("coverage_gaps") or []
    note = (f"<p class='status' style='color:#b91c1c'>Not sampled at all: {esc(', '.join(gaps))} — raise "
            "--max-pages / --sweep before trusting the score for those sections.</p>") if gaps else ""
    dup = {d["template"]: d for d in (r.get("duplication") or [])}
    extra = ""
    if dup:
        rows = "".join(f"<tr><td><code>{esc(t)}</code></td><td>{d['pages']}</td>"
                       f"<td>{d['median_unique_words']}</td><td>{d['near_duplicate_pairs']}</td></tr>"
                       for t, d in dup.items())
        extra = ("<table class='kv'><thead><tr><th>Template</th><th>Pages compared</th>"
                 "<th>Median unique words / page</th><th>Near-duplicate pairs</th></tr></thead>"
                 f"<tbody>{rows}</tbody></table>")
    return ("<h2>Coverage by URL template</h2>"
            f"<p class='status' style='margin:0 0 4px'>The sitemap lists {r.get('sitemap_url_count', 0)} URLs. "
            f"{r.get('page_count', 0)} pages got a full per-page table and {r.get('sweep_count', 0)} more were "
            "analyzed and reported in aggregate, sampled across URL templates so a large section can't hide "
            "behind the first few sitemap entries. A score only describes the templates it looked at.</p>"
            "<table class='kv'><thead><tr><th>Template (path shape)</th><th>URLs in sitemap</th>"
            f"<th>Audited</th></tr></thead><tbody>{trs}</tbody></table>{note}{extra}")


def ai_block(r):
    m = r.get("ai_matrix") or []
    if not m:
        return ""
    role = {"search": "answer-engine index", "user": "live fetch for a user", "train": "model training"}
    trs = "".join(
        f"<tr><td>{esc(a['agent'])}</td><td>{esc(a['operator'])}</td><td>{esc(role.get(a['role'], a['role']))}</td>"
        f"<td style='color:{'#15803d' if a['verdict'] == 'ok' else '#b91c1c'};font-weight:600'>"
        f"{'served 200' if a['verdict'] == 'ok' else esc(a['verdict'])}</td></tr>" for a in m)
    out = ("<h2>AI crawler access (tested at the edge)</h2>"
           "<p class='status' style='margin:0 0 4px'>robots.txt states intent; CDNs and firewalls decide. "
           "The homepage was requested with each crawler's documented user-agent and compared with a "
           "browser. Tested from a non-crawler IP: a firewall that verifies crawler IPs can refuse this "
           "test yet admit the real bot, so confirm any block in the CDN/WAF dashboard.</p>"
           "<table class='kv'><thead><tr><th>Agent</th><th>Operator</th><th>Purpose</th><th>Result</th></tr>"
           f"</thead><tbody>{trs}</tbody></table>")
    rd = r.get("readiness") or []
    if rd:
        rows = "".join(
            f"<tr><td>{esc(x['url'].split('//', 1)[-1].split('/', 1)[-1] or '/')}</td><td>{x['words']}</td>"
            f"<td>{x['subheadings']}</td><td>{x['question_headings']}</td><td>{x['lists']}</td>"
            f"<td>{x['tables']}</td><td>{esc(x['dated'])}</td><td>{esc(x['schema'])}</td></tr>" for x in rd)
        out += ("<h2>Answer-engine readiness (informational)</h2>"
                "<p class='status' style='margin:0 0 4px'>What an answer engine can extract, date and attribute "
                "on each audited page. These are correlational signals, not ranking factors, so they are shown "
                "rather than scored: self-contained sections under descriptive or question headings, lists and "
                "tables for facts, a visible date, and schema naming the entity. Keep this in proportion: the "
                "largest 2026 studies find off-page signals (brand search demand, referring domains, mentions) "
                "predict AI citations far better than anything on the page, and AI referrals are about 1% of "
                "site traffic. Crawler access and server-rendered text are the parts an audit can verify.</p>"
                "<table class='kv'><thead><tr><th>Page</th><th>Words</th><th>H2/H3</th><th>Question headings</th>"
                "<th>Lists</th><th>Tables</th><th>Date</th><th>Schema</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>")
    return out


def sweep_block(r):
    sw = r.get("sweep_summary") or []
    if not sw:
        return ""
    trs = "".join(
        f"<tr><td>{sev_badge(x['severity'])}</td><td>{esc(x['title'])}</td><td>{x['count']}</td>"
        f"<td><code>{esc(', '.join(x['templates'][:3]))}</code></td><td>{esc(x['example'])}</td></tr>" for x in sw[:25])
    return ("<h2>Sitemap sweep — issues by frequency</h2>"
            f"<p class='status' style='margin:0 0 4px'>Across the {r.get('sweep_count', 0)} additional pages: "
            "how many carry each issue and in which templates. A template-level count is the thing to fix.</p>"
            "<table class='kv'><thead><tr><th></th><th>Issue</th><th>Pages</th><th>Templates</th>"
            f"<th>Example</th></tr></thead><tbody>{trs}</tbody></table>")


def render_html(r):
    gen = r["generated"].strftime("%-m/%-d/%Y, %-I:%M:%S %p") if hasattr(r["generated"], "strftime") else str(r["generated"])
    overall = r["overall"]

    # category score table
    cat_rows = []
    for cat, score in r["scores"].items():
        cat_rows.append(
            f"<tr><td>{esc(cat)}</td>"
            f"<td class='catscore' style='color:{score_color(score)}'>{score}</td></tr>"
        )
    cat_table = "<table class='scores'><thead><tr><th>Category</th><th>Score</th></tr></thead><tbody>" \
                + "".join(cat_rows) + "</tbody></table>"

    # status line
    psi = r["psi"]
    psi_txt = {"ok": f"PSI: {psi.get('score','')}/100", "rate-limited": "PSI: unavailable (rate-limited)",
               "unavailable": "PSI: unavailable", "skipped": "PSI: not run"}.get(psi.get("status"), "PSI: —")
    status = (f"{psi_txt}  ·  robots.txt: {'found' if r['robots_found'] else 'missing'}"
              f"  ·  sitemap: {'found' if r['sitemap_found'] else 'missing'}")

    # top priorities — the "do these first" list, highest leverage across the whole audit
    tp = top_priorities(r)
    tp_block = ""
    if tp:
        lis = "".join(
            f"<li>{sev_badge(fi['severity'])} <b>{esc(fi['title'])}</b>"
            f"<span class='ie'> · impact {fi['impact']}/5, effort {fi['effort']}/5</span>"
            f"<div class='fix'>{esc(fi['fix'])}</div></li>"
            for fi in tp
        )
        tp_block = ("<h2>Top priorities</h2>"
                    "<p class='status' style='margin:0 0 6px'>Highest-leverage fixes first — ranked by "
                    "severity, then least effort. Clear these before anything below.</p>"
                    f"<ol class='top'>{lis}</ol>")

    # PSI metrics block
    psi_block = ""
    if psi.get("status") == "ok" and psi.get("metrics"):
        mrows = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in psi["metrics"].items())
        psi_block = ("<h2>Core Web Vitals (PageSpeed, mobile)</h2>"
                     "<table class='kv'><thead><tr><th>Metric</th><th>Value</th></tr></thead>"
                     f"<tbody>{mrows}</tbody></table>")

    # social preview
    social = r["social"]
    soc_block = ""
    if social["metrics"] or social["findings"]:
        mrows = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in social["metrics"])
        soc_block = "<h2>Social share preview</h2>"
        if mrows:
            soc_block += ("<table class='kv'><thead><tr><th>Metric</th><th>Value</th></tr></thead>"
                          f"<tbody>{mrows}</tbody></table>")
        if social["findings"]:
            soc_block += findings_table(social["findings"])

    # keyword focus
    kw_block = keyword_block(r)

    # social & local footprint
    fp_block = footprint_block(r)

    # site-wide findings
    site_block = ""
    if r["site_findings"]:
        site_block = "<h2>Site-wide</h2>" + findings_table(r["site_findings"])

    # per-page
    page_blocks = []
    for p in r["pages"]:
        n = len(p["findings"])
        label = p["url"]
        if p["url"].rstrip("/") == r["url"].rstrip("/"):
            label += "  (Homepage)"
        bits = [f"HTTP {p.get('status', '?')}", f"{p.get('word_count', 0)} words"]
        if p.get("title"):
            bits.append(f"title {len(p['title'])} chars")
        if p.get("internal_links") is not None:
            bits.append(f"{p['internal_links']} internal links")
        if p.get("source"):
            bits.append(f"found via {p['source']}")
        page_blocks.append(
            f"<div class='page'><h2>{esc(label)} — {n} finding{'s' if n != 1 else ''}</h2>"
            f"<div class='purl'>{esc(p['url'])} · {esc(' · '.join(bits))}</div>"
            + findings_table(p["findings"]) + "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>SEO Audit — {esc(r['domain'])}</title>
<style>
  :root {{ --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; }}
  * {{ box-sizing:border-box; }}
  body {{ font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         color:var(--ink); margin:0; padding:40px; max-width:1000px; }}
  h1 {{ font-size:26px; margin:0 0 4px; }}
  h2 {{ font-size:16px; margin:28px 0 8px; padding-bottom:4px; border-bottom:2px solid var(--line); }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
  a {{ color:#2563eb; text-decoration:none; word-break:break-all; }}
  .hero {{ display:flex; gap:28px; align-items:center; }}
  .scorebadge {{ font-size:48px; font-weight:700; line-height:1;
                 padding:18px 22px; border-radius:14px; color:#fff; white-space:nowrap; }}
  .scores, .kv, .findings {{ border-collapse:collapse; width:100%; margin:6px 0 4px; }}
  .scores td, .scores th, .kv td, .kv th {{ text-align:left; padding:5px 10px; border-bottom:1px solid var(--line); }}
  .scores {{ max-width:280px; }}
  .scores th, .kv th, .findings th {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em;
                                       color:var(--muted); background:#f8fafc; }}
  .catscore {{ font-weight:700; text-align:right; }}
  .status {{ color:var(--muted); font-size:12.5px; margin:8px 0 0; }}
  .findings td, .findings th {{ vertical-align:top; padding:6px 8px; border-bottom:1px solid var(--line);
                                font-size:12.5px; }}
  .findings th {{ text-align:left; }}
  .sev {{ display:inline-block; font-size:10px; font-weight:700; padding:2px 6px; border-radius:5px;
          letter-spacing:.03em; }}
  .finding {{ font-weight:600; width:18%; }}
  .cat {{ color:var(--muted); width:8%; }}
  .ie {{ font-variant-numeric:tabular-nums; color:var(--muted); width:5%; }}
  .obs {{ color:#334155; width:26%; }}
  .fix {{ color:#334155; width:32%; }}
  .kw td {{ vertical-align:middle; }}
  .zchip {{ display:inline-block; font-size:9.5px; font-weight:700; padding:1px 5px; border-radius:4px;
            margin:1px 3px 1px 0; letter-spacing:.02em; }}
  .barwrap {{ display:inline-block; width:90px; height:7px; background:#eef2f7; border-radius:4px;
              vertical-align:middle; overflow:hidden; }}
  .bar {{ display:block; height:100%; background:#2563eb; border-radius:4px; }}
  .barnum {{ font-variant-numeric:tabular-nums; color:var(--muted); font-size:11px; margin-left:6px; }}
  .prom {{ white-space:nowrap; width:18%; }}
  .kwnotes {{ margin:8px 0 0; padding-left:20px; color:#334155; font-size:12.5px; }}
  .kwnotes li {{ margin:3px 0; }}
  .top {{ margin:6px 0 4px; padding-left:22px; }}
  .top li {{ margin:0 0 10px; }}
  .top .fix {{ color:#334155; font-size:12.5px; margin-top:2px; width:auto; }}
  .top .ie {{ color:var(--muted); font-size:11.5px; font-weight:400; }}
  .purl {{ color:var(--muted); font-size:12px; margin:-4px 0 6px; }}
  .ok {{ color:#15803d; font-size:13px; }}
  code {{ font:12px ui-monospace,Menlo,monospace; background:#f1f5f9; padding:1px 4px; border-radius:4px; }}
  .page {{ break-inside:avoid; }}
  .legend {{ color:var(--muted); font-size:11px; margin-top:36px; padding-top:10px;
             border-top:1px solid var(--line); }}
  @page {{ size:A4; margin:14mm; }}
  @media print {{ body {{ padding:0; max-width:none; }} h2 {{ break-after:avoid; }} }}
</style></head><body>
  <h1>SEO Audit Report</h1>
  <div class="sub"><a href="{esc(r['url'])}">{esc(r['url'])}</a></div>
  <div class="hero">
    <div class="scorebadge" style="background:{score_color(overall)}">{overall}<span style="font-size:18px">/100</span></div>
    <div>{cat_table}</div>
  </div>
  <div class="sub" style="margin-top:14px">Generated {esc(gen)} · {r['page_count']} page{'s' if r['page_count']!=1 else ''} in depth + {r.get('sweep_count', 0)} swept of {r.get('sitemap_url_count', 0)} sitemap URLs · {esc(r['confidence'])} confidence · seo-audit {esc(r.get('version', ''))}</div>
  <div class="status">{esc(status)}</div>
  {tp_block}
  {coverage_block(r)}
  {health_block(r)}
  {ai_block(r)}
  {psi_block}
  {kw_block}
  {soc_block}
  {fp_block}
  {site_block}
  {sweep_block(r)}
  {''.join(page_blocks)}
  <div class="legend">
    <b>Cat</b> = category · <b>I/E</b> = impact / effort, each 1–5 (high impact, low effort = do first).
    Category score = 100 minus severity deductions (CRITICAL −40, HIGH −20, MEDIUM −10, LOW −4),
    counting each distinct issue type once so the score doesn't swing with the number of pages crawled,
    with diminishing returns inside a category (two heaviest findings in full, next two at 75%, the rest at 50%).
    Overall = weighted average across categories. The Performance category combines signals
    observed directly from the HTML &amp; headers (compression, render-blocking scripts, page
    weight, DOM size, image dimensions, response time) with Google's real-user Core Web Vitals
    field data (CrUX, incl. INP at the 75th percentile) when PageSpeed is run. Site health
    checks (host-variant redirects, 404 handling, TLS certificate, trust pages, sampled
    internal links) feed the crawlability and trust scores.
    Aligned with 2026 priorities: content quality &amp; E-E-A-T, crawlability, page experience,
    and visibility to AI answer engines. Generated by the seo-audit skill.
  </div>
</body></html>"""


def _find_chrome():
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def html_to_pdf(html_path, pdf_path):
    """Render HTML to PDF. Tries headless Chrome first, then WeasyPrint. Returns bool."""
    chrome = _find_chrome()
    if chrome:
        url = "file://" + os.path.abspath(html_path)
        for flags in (
            ["--headless=new", "--no-pdf-header-footer"],
            ["--headless", "--print-to-pdf-no-header"],
        ):
            try:
                subprocess.run(
                    [chrome, *flags, "--disable-gpu", "--no-sandbox",
                     f"--print-to-pdf={os.path.abspath(pdf_path)}", url],
                    check=True, capture_output=True, timeout=120,
                )
                if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                    return True
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                continue
    try:
        from weasyprint import HTML
        HTML(filename=html_path).write_pdf(pdf_path)
        return os.path.exists(pdf_path)
    except Exception:
        return False
