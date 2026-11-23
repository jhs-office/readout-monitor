"""
Convert the LifeSci event spreadsheet into watchlist.json, and pull the
pipeline-page hyperlinks on the Company column into pipelines.json.

Run this once now, and again any time you update the spreadsheet.

    python build_watchlist.py "Prospective_Prediction_Project_-_July_2026_-_v6.xlsx"
"""

import json
import re
import sys

import pandas as pd

SHEET = "MASTER TRIAL READOUTS"


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
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return []  # blank cell -- str(nan) would otherwise become the literal "NAN"
    parts = re.split(r"[/,;]", str(raw))
    out = []
    for p in parts:
        p = p.strip().upper()
        if re.fullmatch(r"[A-Z.\-]{1,6}", p):
            out.append(p)
    return out


def extract_pipeline_links(path):
    """Ticker -> pipeline-page URL, from the hyperlink on each row's Company cell.

    pandas can't see cell hyperlinks, so this reads the sheet with openpyxl.
    On a partnered row ('PRTA / BMY') the link is the first-listed company's
    page, so it's only attached to that first ticker, and only as a fallback
    when the ticker has no link from a single-ticker row of its own.
    """
    import openpyxl

    ws = openpyxl.load_workbook(path)[SHEET]
    col = {c.value: c.column for c in ws[1]}
    own, fallback = {}, {}
    for r in range(2, ws.max_row + 1):
        tickers = split_tickers(ws.cell(r, col["Ticker"]).value)
        link = ws.cell(r, col["Company"]).hyperlink
        url = link.target if link and link.target else None
        if not tickers or not url or not url.startswith(("http://", "https://")):
            continue
        if len(tickers) == 1:
            own.setdefault(tickers[0], []).append(url)
        else:
            fallback.setdefault(tickers[0], url)

    out = {}
    for t, urls in own.items():
        out[t] = max(set(urls), key=urls.count)  # most common if rows disagree
        if len(set(urls)) > 1:
            print(f"  note: {t} has {len(set(urls))} different pipeline links; using {out[t]}")
    for t, url in fallback.items():
        out.setdefault(t, url)
    return out


def main(path, out_path="watchlist.json", pipelines_path="pipelines.json"):
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

    pipelines = extract_pipeline_links(path)
    with open(pipelines_path, "w") as f:
        json.dump(pipelines, f, indent=2, sort_keys=True)
    no_link = sorted(set(events) - set(pipelines))
    print(f"wrote {pipelines_path}: {len(pipelines)} pipeline links")
    if no_link:
        print(f"  tickers with no pipeline link: {no_link}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "events.xlsx")
