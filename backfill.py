"""
One-off catch-up scan: checks the last N days of news for the watchlist
instead of just the last cron interval. Useful after a gap in coverage, or
just to sanity-check the live monitor against a wider window.

Reuses monitor.py's own fetch/filter/classify/email functions directly, so
this always stays behavior-consistent with the live pipeline. Does NOT touch
state.json -- this is a read-only scan against Alpaca's news feed and does
not affect the live monitor's dedup state. Re-running it re-scans the whole
window and may re-report the same articles.

Genuine readouts found get emailed to jacob.s@gatchealth.com only (not the
full EMAIL_TO list), regardless of what EMAIL_TO is set to in your shell.

Required environment variables (same as monitor.py, set these in your shell
before running -- nothing is hardcoded here):
    ALPACA_KEY_ID, ALPACA_SECRET_KEY, ANTHROPIC_API_KEY,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM

Usage:
    python backfill.py            # last 14 days
    python backfill.py --days 7   # last 7 days
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

BACKFILL_EMAIL_TO = "jacob.s@gatchealth.com"

# monitor.py reads its config (including EMAIL_TO) at import time, so this
# has to happen before the import -- it scopes every email this script sends
# to just the one address, no matter what EMAIL_TO is set to elsewhere.
os.environ["EMAIL_TO"] = BACKFILL_EMAIL_TO

for required in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_FROM"):
    if not os.environ.get(required):
        sys.exit(f"Missing required environment variable: {required}")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import monitor  # noqa: E402  (must come after the env vars above are set)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    with open(monitor.WATCHLIST_PATH) as f:
        watchlist = json.load(f)
    symbols = sorted(watchlist.keys())

    start = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"scanning {len(symbols)} symbols since {start.isoformat()}")

    articles = monitor.fetch_news(symbols, start.isoformat())
    print(f"  {len(articles)} articles returned")

    candidates = [a for a in articles if monitor.keyword_hit(a)]
    print(f"  {len(candidates)} passed keyword filter")

    found = []
    for article in candidates:
        events = monitor.relevant_events(article, watchlist)
        if not events:
            continue
        verdict = monitor.classify(article, events)
        if verdict.get("is_readout"):
            found.append((article, verdict, events))
            print(f"  READOUT: {article.get('created_at')}  {article.get('headline')}")
            monitor.notify(article, verdict, events)

    print(f"\n{len(found)} readout(s) found and emailed to {BACKFILL_EMAIL_TO}")

    report_path = f"backfill_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_path, "w") as f:
        json.dump([
            {
                "published": a.get("created_at"),
                "headline": a.get("headline"),
                "url": a.get("url"),
                "verdict": v,
            }
            for a, v, _ in found
        ], f, indent=2)
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    main()
