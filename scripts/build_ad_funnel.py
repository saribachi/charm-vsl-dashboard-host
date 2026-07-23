#!/usr/bin/env python3
"""Build the HireCharm ad funnel dashboard (Table A -> Table B model).

Presence-driven: every metric computes only when ALL its inputs are populated;
otherwise it renders an explicit NEEDS: <field> state (never 0 / NaN / blank).
Tier 1 = Meta (live-able now). Tier 2 = CRM/calendar (not wired).

Source of ad_daily rows, in order of preference:
  1. data/meta/ad_daily.json      (real data you drop in)
  2. data/meta/ad_daily.sample.json (labeled SAMPLE, so the UI renders)

Meta's real field names are accepted as aliases (amount_spent, actions:link_click,
lead, ...), because the API does NOT have fields literally named spend/link_clicks/
form_fills — verified against the Ads API field vocabulary.

Run: python3 scripts/build_ad_funnel.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── FILL IN: map each real ad_set_name to one of the four angles ───────────────
angle_map = {
    "<ad_set_name_1>": "burned_before",
    "<ad_set_name_2>": "sell_targeting",
    "<ad_set_name_3>": "broken_infra",
    "<ad_set_name_4>": "referrals_luck",
    # sample names (remove once real names are mapped above):
    "Burned Before - Video A": "burned_before",
    "Sell the Targeting - Static B": "sell_targeting",
    "Broken Infra - Video C": "broken_infra",
    "Referrals & Luck - Static D": "referrals_luck",
}

ANGLE_LABELS = {
    "burned_before": "Burned before",
    "sell_targeting": "Sell the targeting",
    "broken_infra": "Broken infra",
    "referrals_luck": "Referrals & luck",
    "unmapped": "Unmapped ad sets",
}

BENCHMARKS = {
    "target_cac": 3000,
    "target_roas": 5.0,
    "cost_per_qualified_call": 600,
    "cost_per_live_call": 360,
    "cost_per_booked_call": 270,
    "cash_per_qual_booking": 800,   # reference
    "cost_per_qual_booking": 300,   # reference
}

# Table A field -> accepted aliases (model name first, then Meta-native names).
FIELD_ALIASES = {
    "spend":             ["spend", "amount_spent"],
    "impressions":       ["impressions"],
    "reach":             ["reach"],
    "link_clicks":       ["link_clicks", "actions:link_click", "outbound_clicks", "clicks"],
    "form_fills":        ["form_fills", "lead", "onsite_conversion_lead_grouped", "leads"],
    # Tier 2 (CRM/calendar) — no Meta equivalent
    "meetings_booked":   ["meetings_booked"],
    "meetings_showed":   ["meetings_showed"],
    "meetings_qualified":["meetings_qualified"],
    "deals_closed":      ["deals_closed"],
    "cash_collected":    ["cash_collected"],
}
META_FIELDS = ["spend", "impressions", "reach", "link_clicks", "form_fills"]
CRM_FIELDS = ["meetings_booked", "meetings_showed", "meetings_qualified", "deals_closed", "cash_collected"]


def get_field(row, canonical):
    for alias in FIELD_ALIASES[canonical]:
        if alias in row and row[alias] is not None:
            return row[alias]
    return None


def safe_div(n, d):
    """Guarded division. Returns (value, ok). ok=False when denom is 0/None or n is None."""
    if n is None or d is None or d == 0:
        return None, False
    return n / d, True


def load_rows():
    real = ROOT / "data/meta/ad_daily.json"
    if real.exists():
        raw = json.loads(real.read_text())
        return (raw.get("rows", raw) if isinstance(raw, (dict, list)) else []), False
    sample = ROOT / "data/meta/ad_daily.sample.json"
    if sample.exists():
        return json.loads(sample.read_text()).get("rows", []), True
    return [], True


# ── metric specifications ─────────────────────────────────────────────────────
# each: key, label, unit, inputs (list of (numerator_field, denominator_field) recipe),
# formula(callable over an aggregates dict), tier, target key, better direction.
def build_metric_specs():
    def m(key, label, unit, needs, formula, tier, target=None, better=None, note=None):
        return dict(key=key, label=label, unit=unit, needs=needs, formula=formula,
                    tier=tier, target=target, better=better, note=note)

    return [
        # TIER 1 — Meta
        m("cpm", "CPM", "$", ["spend", "impressions"],
          lambda a: safe_div(a["spend"] * 1000, a["impressions"]), 1),
        m("ctr", "CTR", "%", ["link_clicks", "impressions"],
          lambda a: safe_div(a["link_clicks"], a["impressions"]), 1),
        m("cost_per_click", "Cost per click", "$", ["spend", "link_clicks"],
          lambda a: safe_div(a["spend"], a["link_clicks"]), 1),
        m("application_rate", "Application rate", "%", ["form_fills", "link_clicks"],
          lambda a: safe_div(a["form_fills"], a["link_clicks"]), 1),
        m("cost_per_form_fill", "Cost per form fill", "$", ["spend", "form_fills"],
          lambda a: safe_div(a["spend"], a["form_fills"]), 1),
        # TIER 2 — CRM / calendar
        m("cost_per_meeting", "Cost per meeting booked", "$", ["spend", "meetings_booked"],
          lambda a: safe_div(a["spend"], a["meetings_booked"]), 2,
          target="cost_per_booked_call", better="low"),
        m("show_rate", "Show rate", "%", ["meetings_showed", "meetings_booked"],
          lambda a: safe_div(a["meetings_showed"], a["meetings_booked"]), 2),
        m("qualified_rate", "Qualified rate", "%", ["meetings_qualified", "meetings_booked"],
          lambda a: safe_div(a["meetings_qualified"], a["meetings_booked"]), 2),
        m("cost_per_qualified_mtg", "Cost per qualified meeting", "$", ["spend", "meetings_qualified"],
          lambda a: safe_div(a["spend"], a["meetings_qualified"]), 2,
          target="cost_per_qualified_call", better="low"),
        m("cost_per_qual_booking", "Cost per qualified booking", "$", ["spend", "meetings_qualified"],
          lambda a: safe_div(a["spend"], a["meetings_qualified"]), 2,
          target="cost_per_qual_booking", better="low"),
        m("close_rate", "Close rate", "%", ["deals_closed", "meetings_qualified"],
          lambda a: safe_div(a["deals_closed"], a["meetings_qualified"]), 2),
        m("cac", "CAC", "$", ["spend", "deals_closed"],
          lambda a: safe_div(a["spend"], a["deals_closed"]), 2,
          target="target_cac", better="low"),
        m("asp", "ASP", "$", ["cash_collected", "deals_closed"],
          lambda a: safe_div(a["cash_collected"], a["deals_closed"]), 2),
        m("roas", "ROAS", "x", ["cash_collected", "spend"],
          lambda a: safe_div(a["cash_collected"], a["spend"]), 2,
          target="target_roas", better="high"),
        m("cash_per_qual_booking", "Cash per qualified booking", "$", ["cash_collected", "meetings_qualified"],
          lambda a: safe_div(a["cash_collected"], a["meetings_qualified"]), 2,
          target="cash_per_qual_booking", better="high"),
        m("booking_margin_ratio", "Booking margin ratio", "ratio",
          ["cash_collected", "meetings_qualified", "spend"],
          lambda a: _margin_ratio(a), 2, better="high", note="cash_per_qual_booking / cost_per_qual_booking; winner >= 2.0"),
    ]


def _margin_ratio(a):
    cash_pqb, ok1 = safe_div(a["cash_collected"], a["meetings_qualified"])
    cost_pqb, ok2 = safe_div(a["spend"], a["meetings_qualified"])
    if not (ok1 and ok2):
        return None, False
    return safe_div(cash_pqb, cost_pqb)


def compute_metric(spec, agg, present):
    missing = [f for f in spec["needs"] if not present.get(f)]
    if missing:
        crm_missing = [f for f in missing if f in CRM_FIELDS]
        where = "CRM/calendar" if crm_missing else "Meta feed"
        return {"key": spec["key"], "label": spec["label"], "status": "needs",
                "needs": missing, "text": f"NEEDS: {', '.join(missing)} from {where}",
                "unit": spec["unit"], "note": spec["note"]}
    value, ok = spec["formula"](agg)
    if not ok:
        denom = spec["needs"][-1]
        return {"key": spec["key"], "label": spec["label"], "status": "insufficient",
                "text": f"INSUFFICIENT (no {denom})", "unit": spec["unit"], "note": spec["note"]}
    target = BENCHMARKS.get(spec["target"]) if spec["target"] else None
    meets = None
    if target is not None and spec["better"]:
        meets = value <= target if spec["better"] == "low" else value >= target
    return {"key": spec["key"], "label": spec["label"], "status": "ok", "value": value,
            "unit": spec["unit"], "target": target, "better": spec["better"], "meets": meets,
            "note": spec["note"]}


def aggregate(rows):
    agg = {f: 0 for f in FIELD_ALIASES}
    present = {f: False for f in FIELD_ALIASES}
    for r in rows:
        for f in FIELD_ALIASES:
            v = get_field(r, f)
            if v is not None:
                try:
                    agg[f] += float(v)
                    present[f] = True
                except (TypeError, ValueError):
                    pass
    return agg, present


def winner_label(metrics_by_key, agg, present):
    """Kill/scale annotation. Only meaningful once Tier 2 (bookings) flow."""
    if not present.get("meetings_qualified") or not present.get("meetings_booked"):
        return {"label": "INSUFFICIENT DATA", "detail": "ranking by cost_per_form_fill proxy only", "kind": "insufficient"}
    cpqb = metrics_by_key.get("cost_per_qual_booking", {})
    bmr = metrics_by_key.get("booking_margin_ratio", {})
    bookings = agg.get("meetings_booked", 0)
    cpm_booked = metrics_by_key.get("cost_per_meeting", {})
    target_booked = BENCHMARKS["cost_per_booked_call"]
    if bookings >= 3 and cpm_booked.get("status") == "ok" and cpm_booked["value"] > target_booked:
        return {"label": "KILL", "detail": f"cost/booked ${cpm_booked['value']:.0f} > ${target_booked} over {bookings:.0f} bookings", "kind": "kill"}
    if (cpqb.get("status") == "ok" and cpqb["value"] <= 300 and
            bmr.get("status") == "ok" and bmr["value"] >= 2.0):
        return {"label": "WINNER — SCALE", "detail": f"cost/qual ${cpqb['value']:.0f} ≤ 300 · margin {bmr['value']:.1f}x ≥ 2.0", "kind": "winner"}
    return {"label": "HOLD", "detail": "meets neither winner nor kill threshold yet", "kind": "hold"}


def main():
    rows, is_sample = load_rows()
    specs = build_metric_specs()

    # group by angle
    groups = {}
    for r in rows:
        name = r.get("ad_set_name", "")
        angle = r.get("angle") or angle_map.get(name, "unmapped")
        groups.setdefault(angle, []).append(r)

    def build_group(gr):
        agg, present = aggregate(gr)
        metrics = [compute_metric(s, agg, present) for s in specs]
        by_key = {m["key"]: m for m in metrics}
        return {"agg": agg, "present": present, "metrics": metrics,
                "annotation": winner_label(by_key, agg, present)}

    angles = []
    for angle, gr in groups.items():
        g = build_group(gr)
        cpff = next((m for m in g["metrics"] if m["key"] == "cost_per_form_fill"), {})
        angles.append({
            "angle": angle, "label": ANGLE_LABELS.get(angle, angle),
            "ad_sets": sorted({r.get("ad_set_name", "") for r in gr}),
            "spend": g["agg"]["spend"], "impressions": g["agg"]["impressions"],
            "reach": g["agg"]["reach"], "link_clicks": g["agg"]["link_clicks"],
            "form_fills": g["agg"]["form_fills"],
            "cost_per_form_fill": cpff.get("value"),
            "metrics": g["metrics"], "annotation": g["annotation"],
        })
    # sort by cost_per_form_fill ascending (best available proxy); unwired -> last
    angles.sort(key=lambda a: (a["cost_per_form_fill"] is None, a["cost_per_form_fill"] or 0))

    blended = build_group(rows)

    # ---- GHL-reconciled cost per form fill ----
    # Meta's Lead event isn't firing (pixel logs PageView only), so the export's
    # form_fills are empty. GHL holds the real submissions. With a single ad set
    # driving all VSL traffic, blended spend / GHL form fills is a true CPFF.
    ghl_recon = {"available": False}
    summary_path = ROOT / "data/ghl/summary.json"
    if summary_path.exists():
        summ = json.loads(summary_path.read_text())
        spend = blended["agg"].get("spend", 0)
        real_ff = summ.get("real_form_fills", 0)
        val, ok = safe_div(spend, real_ff)
        ghl_recon = {
            "available": True,
            "spend": spend,
            "real_form_fills": real_ff,
            "form_fills_incl_test": summ.get("form_fills_incl_test", 0),
            "generated_at": summ.get("generated_at"),
            "cost_per_form_fill": ({"status": "ok", "value": round(val, 2)} if ok
                                   else {"status": "insufficient", "text": "awaiting real (non-test) form fills"}),
        }

    # data completeness: which fields are missing to unlock Tier 2
    _, present_all = aggregate(rows)
    missing_meta = [f for f in META_FIELDS if not present_all.get(f)]
    missing_crm = [f for f in CRM_FIELDS if not present_all.get(f)]

    data = {
        "generated_at_note": "Rebuild: python3 scripts/build_ad_funnel.py",
        "is_sample": is_sample,
        "benchmarks": BENCHMARKS,
        "field_reality": {
            "spend": "amount_spent", "link_clicks": "actions:link_click (or clicks/outbound_clicks)",
            "form_fills": "lead / onsite_conversion_lead_grouped",
            "note": "Meta API has no fields literally named spend/link_clicks/form_fills; these are the real names. Verified against the Ads API field vocabulary. The loader accepts both.",
        },
        "completeness": {
            "missing_meta": missing_meta,
            "missing_crm": missing_crm,
            "tier2_unlocked": not missing_crm,
        },
        "metric_specs": [{"key": s["key"], "label": s["label"], "tier": s["tier"],
                          "unit": s["unit"], "note": s["note"],
                          "target": BENCHMARKS.get(s["target"]) if s["target"] else None,
                          "better": s["better"]} for s in specs],
        "angles": angles,
        "blended": {"metrics": blended["metrics"], "agg": blended["agg"],
                    "annotation": blended["annotation"]},
        "ghl_reconciled": ghl_recon,
    }

    template = (ROOT / "scripts/ad_funnel_template.html").read_text()
    out = ROOT / "dashboard/ad_funnel_dashboard.html"
    out.write_text(template.replace("/*__DATA__*/null", json.dumps(data)))
    (ROOT / "data/_ad.json").write_text(json.dumps(data))  # for the unified page
    print(f"Wrote {out}")
    print(f"  source: {'SAMPLE' if is_sample else 'data/meta/ad_daily.json'} · "
          f"angles: {len(angles)} · Tier2 unlocked: {not missing_crm}")
    if missing_crm:
        print(f"  Tier 2 blocked — NEEDS from CRM/calendar: {', '.join(missing_crm)}")


if __name__ == "__main__":
    sys.exit(main())
