Seeded seo-analyzer run against https://centurionawards.com on 20 Sep 2026.

Command:

```bash
python3 scripts/seo_audit.py https://centurionawards.com \
  --max-pages 14 \
  --include-pages \
    https://centurionawards.com/business,\
https://centurionawards.com/apply,\
https://centurionawards.com/faq,\
https://centurionawards.com/winners,\
https://centurionawards.com/nominate,\
https://centurionawards.com/2025/us/az/flagstaff/tours/freaky-foot-tours,\
https://centurionawards.com/us/az/flagstaff/bars/beaver-street-brewery,\
https://centurionawards.com/2025/us/az/flagstaff/,\
https://centurionawards.com/blog/,\
https://centurionawards.com/blog/how-centurion-winners-are-verified,\
https://centurionawards.com/about,\
https://centurionawards.com/methodology \
  --json --pagespeed --out reports/centurion-awards-2026-09-20
```

Result: 79/100, medium confidence (PageSpeed 429). HTML, JSON, and PDF in this folder. Live copies of robots.txt, sitemap.xml, llms.txt, and response headers were saved alongside.

The prioritized briefing is `../centurion-awards-gsc-readiness.md`.
