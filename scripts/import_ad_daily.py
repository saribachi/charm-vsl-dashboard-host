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


def main(argv):
    src = argv[1] if len(argv) > 1 else pick_default()
    if not src or not os.path.exists(src):
        print("No CSV given and none found in ~/Downloads. Pass a path.")
        return 1
    rows = []
    with open(src, newline="", encoding="utf-8-sig") as f:  # utf-8-sig strips Meta's BOM
        for x in csv.DictReader(f):
            if not (x.get("Ad set name") or "").strip():
                continue
            row = {}
            for col, field in COLS.items():
                raw = (x.get(col) or "").replace(",", "").strip()
                if raw == "":
                    continue  # omit empties so presence detection -> NEEDS
                row[field] = float(raw) if field in NUMERIC else raw
            rows.append(row)

    out = {"_source": f"Meta manual export '{os.path.basename(src)}' — until Ads MCP is enabled",
           "rows": rows}
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
