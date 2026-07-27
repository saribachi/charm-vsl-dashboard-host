#!/usr/bin/env python3
"""Convert a Meta ad-set CSV export into data/meta/ad_daily.json.

Use this until the Meta Ads MCP/API is enabled and the pull can be automated.
Maps Meta's real column names to the ad_daily model and drops empty fields so the
dashboard's presence detection renders NEEDS states correctly.

Usage:
    python3 scripts/import_ad_daily.py "/path/to/export.csv"
    python3 scripts/import_ad_daily.py            # auto-picks newest matching file in ~/Downloads
"""
import csv
import glob
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Meta export column -> ad_daily field
COLS = {
    "Day": "date",
    "Ad set ID": "ad_set_id",
    "Ad set name": "ad_set_name",
    "Reach": "reach",
    "Impressions": "impressions",
    "Amount spent (USD)": "spend",
    "Link clicks": "link_clicks",
    "Leads": "form_fills",
    "Schedule": "meta_bookings",  # the GTM funnel's conversion event (call booked)
}
NUMERIC = {"reach", "impressions", "spend", "link_clicks", "form_fills", "meta_bookings"}


def pick_default():
    hits = glob.glob(os.path.expanduser("~/Downloads/*ad_daily*TableA*.csv"))
    return max(hits, key=os.path.getmtime) if hits else None


def rows_from_csv_text(text, source="upload"):
    """Parse Meta ad-set CSV text -> {"_source", "rows":[...]}. Reused by the CLI
    and the dashboard host's /upload endpoint. Raises ValueError on a bad CSV."""
    import io
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or "Ad set name" not in reader.fieldnames:
        raise ValueError("CSV missing an 'Ad set name' column — is this the ad-set export?")
    rows = []
    for x in reader:
        if not (x.get("Ad set name") or "").strip():
            continue
        row = {}
        for col, field in COLS.items():
            raw = (x.get(col) or "").replace(",", "").strip()
            if raw == "":
                continue  # omit empties so presence detection -> NEEDS
            row[field] = float(raw) if field in NUMERIC else raw
        rows.append(row)
    if not rows:
        raise ValueError("No ad-set rows found in the CSV.")
    return {"_source": f"Meta manual export ({source})", "rows": rows}


def main(argv):
    src = argv[1] if len(argv) > 1 else pick_default()
    if not src or not os.path.exists(src):
        print("No CSV given and none found in ~/Downloads. Pass a path.")
        return 1
    with open(src, newline="", encoding="utf-8-sig") as f:  # utf-8-sig strips Meta's BOM
        out = rows_from_csv_text(f.read(), source=os.path.basename(src))
    rows = out["rows"]
    (ROOT / "data/meta").mkdir(parents=True, exist_ok=True)
    (ROOT / "data/meta/ad_daily.json").write_text(json.dumps(out, indent=1))

    present = sorted({k for r in rows for k in r if k in NUMERIC})
    missing = [k for k in NUMERIC if k not in present]
    print(f"Wrote data/meta/ad_daily.json — {len(rows)} rows, "
          f"{len({r['ad_set_name'] for r in rows})} ad set(s).")
    print(f"  fields present: {present}")
    if missing:
        print(f"  fields MISSING (render as NEEDS): {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
