"""
Convert the LifeSci event spreadsheet into watchlist.json.

Run this once now, and again any time you update the spreadsheet.

    python build_watchlist.py "Prospective_Prediction_Project_-_July_2026_-_v6.xlsx"
"""

import json
import re
import sys

import pandas as pd

SHEET = "LifeSci Clinical Trial Events"


def clean(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s if s and s.lower() != "nan" else None


def clean_phase(v):
    s = clean(v)
    if s is None:
        return None
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else s
    except ValueError:
        return s


def split_tickers(raw):
    """'PRTA / BMY' -> ['PRTA', 'BMY']. Partnered assets can be announced by either party."""
    parts = re.split(r"[/,;]", str(raw))
    out = []
    for p in parts:
        p = p.strip().upper()
        if re.fullmatch(r"[A-Z.\-]{1,6}", p):
            out.append(p)
    return out


def main(path, out_path="watchlist.json"):
    df = pd.read_excel(path, sheet_name=SHEET)

    events = {}
    skipped = []

    for _, row in df.iterrows():
        tickers = split_tickers(row.get("Ticker"))
        if not tickers:
            skipped.append(clean(row.get("Ticker")))
            continue

        event = {
            "company": clean(row.get("Company")),
            "product": clean(row.get("Product")),
            "indication": clean(row.get("Indication")),
            "phase": clean_phase(row.get("Phase")),
            "event": clean(row.get("Event")),
            "event_type": clean(row.get("Event Type")),
            "timing": clean(row.get("Timing")),
        }

        for t in tickers:
            events.setdefault(t, []).append(event)

    with open(out_path, "w") as f:
        json.dump(events, f, indent=2, sort_keys=True)

    print(f"wrote {out_path}")
    print(f"  {len(events)} tickers")
    print(f"  {sum(len(v) for v in events.values())} ticker-event pairs")
    if skipped:
        print(f"  skipped {len(skipped)} unparseable tickers: {set(skipped)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "events.xlsx")
