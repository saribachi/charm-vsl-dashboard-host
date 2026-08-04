#!/usr/bin/env python3
"""Convert a Meta ad-set CSV export into data/meta/ad_daily.json.

Use this until the Meta Ads MCP/API is enabled and the pull can be automated.
Maps Meta's real column names to the ad_daily model and drops empty fields so the
dashboard's presence detection renders NEEDS states correctly.

Imports MERGE into the existing history by default: a partial export (say Aug 1-4)
adds to what is already on file instead of replacing it, and re-importing an
overlapping range corrects those days rather than duplicating them. Meta's export
range is whatever was selected in Ads Manager, so a replace would silently drop
history — that is exactly how Jul 20-31 was lost on 2026-08-03.

Usage:
    python3 scripts/import_ad_daily.py "/path/to/export.csv"
    python3 scripts/import_ad_daily.py            # auto-picks newest matching file in ~/Downloads
    python3 scripts/import_ad_daily.py FILE --replace   # discard history, use this CSV alone
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


AD_DAILY = "data/meta/ad_daily.json"


def _key(row):
    """Identity of a daily ad-set row. Ad set ID is stable across renames; fall
    back to the name only when Meta's export omits the ID column."""
    return (row.get("date") or "", str(row.get("ad_set_id") or row.get("ad_set_name") or ""))


def merge_rows(existing, incoming):
    """Upsert `incoming` over `existing` on (date, ad set). A day present in the
    new export wins outright (Meta restates recent days as attribution settles);
    days it does not cover are preserved. Returns (rows, stats)."""
    by = {_key(r): r for r in existing}
    added = updated = 0
    for r in incoming:
        k = _key(r)
        if k in by:
            if by[k] != r:
                updated += 1
        else:
            added += 1
        by[k] = r
    rows = sorted(by.values(), key=lambda r: (r.get("date") or "", str(r.get("ad_set_name") or "")))
    return rows, {"added": added, "updated": updated, "kept": len(existing) - updated}


def load_existing(root=ROOT):
    try:
        return json.loads((root / AD_DAILY).read_text()).get("rows", [])
    except Exception:
        return []


def merge_into_file(parsed, root=ROOT, replace=False):
    """Write parsed CSV output to ad_daily.json, merging with history unless
    `replace`. Shared by the CLI and the dashboard host's /upload endpoint."""
    existing = [] if replace else load_existing(root)
    rows, stats = merge_rows(existing, parsed["rows"])
    out = {"_source": parsed.get("_source"), "rows": rows}
    (root / "data/meta").mkdir(parents=True, exist_ok=True)
    (root / AD_DAILY).write_text(json.dumps(out, indent=1))
    dates = sorted({r["date"] for r in rows if r.get("date")})
    stats.update(total_rows=len(rows), days=len(dates),
                 date_from=dates[0] if dates else None, date_to=dates[-1] if dates else None,
                 spend=round(sum(r.get("spend") or 0 for r in rows), 2),
                 ad_sets=len({r.get("ad_set_name") for r in rows}))
    return out, stats


def main(argv):
    args = [a for a in argv[1:] if not a.startswith("--")]
    replace = "--replace" in argv
    src = args[0] if args else pick_default()
    if not src or not os.path.exists(src):
        print("No CSV given and none found in ~/Downloads. Pass a path.")
        return 1
    with open(src, newline="", encoding="utf-8-sig") as f:  # utf-8-sig strips Meta's BOM
        parsed = rows_from_csv_text(f.read(), source=os.path.basename(src))
    out, st = merge_into_file(parsed, replace=replace)
    rows = out["rows"]

    present = sorted({k for r in rows for k in r if k in NUMERIC})
    missing = [k for k in NUMERIC if k not in present]
    verb = "Replaced" if replace else "Merged into"
    print(f"{verb} data/meta/ad_daily.json — {st['total_rows']} rows, {st['ad_sets']} ad set(s), "
          f"{st['days']} day(s) {st['date_from']} -> {st['date_to']}, ${st['spend']:,.2f} total.")
    print(f"  this CSV: +{st['added']} new row(s), {st['updated']} restated, "
          f"{st['kept']} pre-existing row(s) preserved")
    print(f"  fields present: {present}")
    if missing:
        print(f"  fields MISSING (render as NEEDS): {missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
