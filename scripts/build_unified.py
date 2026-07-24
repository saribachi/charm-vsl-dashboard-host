#!/usr/bin/env python3
"""Build the unified single-page dashboard: one funnel, ad spend -> cash.

Reads data/_vsl.json and data/_ad.json (dumped by the two builds) and renders
one continuous funnel with the headline KPIs on top. Run after both builds.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def div(n, d):
    if not d or n is None:
        return None
    return n / d


def money(v):
    if v is None:
        return None
    return f"${v:,.0f}" if v >= 100 else f"${v:,.2f}".rstrip("0").rstrip(".")


def pct(v):
    return None if v is None else f"{v*100:.1f}%"


def main():
    vsl = json.loads((ROOT / "data/_vsl.json").read_text())
    ad = json.loads((ROOT / "data/_ad.json").read_text())

    agg = ad["blended"]["agg"]
    ghl = ad.get("ghl_reconciled") or {}
    W = vsl["wistia"]

    spend = agg.get("spend") or 0
    impressions = int(agg.get("impressions") or 0)
    reach = int(agg.get("reach") or 0)
    clicks = int(agg.get("link_clicks") or 0)
    visitors = int(W.get("visitors") or 0)
    plays = int(W.get("plays") or 0)
    watched = int(W.get("watched_50") or 0)
    real_forms = int(vsl.get("real_form_fills") or 0)
    real_booked = int(vsl.get("real_bookings") or 0)
    held = vsl.get("meetings_held")
    dayai_on = held is not None
    rb2b = vsl.get("rb2b", {})

    ctr = div(clicks, impressions)
    cpc = div(spend, clicks)
    cpff = div(spend, real_forms)
    cpbooked = div(spend, real_booked)

    # ── headline KPIs ──
    def kpi(label, value, status, sub):
        return {"label": label, "value": value, "status": status, "sub": sub}

    kpis = [
        kpi("Ad spend", money(spend), "live", "GTM ad set · Jul 20-23"),
        kpi("Cost / click", money(cpc), "live" if cpc else "needs", f"{pct(ctr) or '—'} CTR"),
        kpi("Cost / form fill", money(cpff) if cpff else "—", "live" if cpff else "needs",
            "GHL actual" if cpff else "awaiting real leads"),
        kpi("Cost / booked call", money(cpbooked) if cpbooked else "—", "live" if cpbooked else "needs",
            "target $270" if not cpbooked else "vs $270 target"),
        kpi("ROAS", "—", "needs", "needs CRM cash"),
    ]

    # ── the funnel: click -> cash (bars scaled to link clicks) ──
    def stage(label, value, source, status, prev=None, cost=None, note=None):
        conv = None
        if value is not None and prev not in (None, 0):
            conv = pct(div(value, prev))
        bar = 0.0
        if value is not None and clicks:
            bar = max(0.0, min(1.0, value / clicks))
        disp = f"{value:,.0f}" if isinstance(value, (int, float)) else None
        return {"label": label, "value": disp, "raw": value, "bar": bar, "conv": conv,
                "source": source, "status": status, "cost": cost, "note": note}

    landing_note = (f"{rb2b.get('count', 0)} identified by RB2B" if rb2b.get("connected")
                    else "RB2B feed pending")
    ba = vsl.get("bookings_attribution", {})
    if ba.get("total_real"):
        att = ba.get("ad_attributed", 0)
        booked_note = f"{att} tagged to an ad (utm_content)" if att else "UTMs now live — new bookings will tag to an ad"
    else:
        booked_note = None
    funnel = [
        stage("Link clicks", clicks, "Meta", "live", cost=f"{money(cpc)}/click" if cpc else None,
              note=f"{pct(ctr) or '—'} of {impressions:,} impressions"),
        stage("Landing page visitors", visitors, "Wistia", "live", prev=clicks, note=landing_note),
        stage("Video plays", plays, "Wistia", "live", prev=visitors, note="pressed play"),
        stage("Watched ≥50%", watched, "Wistia", "live", prev=plays),
        stage("Form fills", real_forms, "GHL", "live", prev=watched,
              cost=(f"{money(cpff)}/lead" if cpff else None), note="real leads, tests excluded"),
        stage("Booked calls", real_booked, "GHL", "live", prev=real_forms,
              cost=(f"{money(cpbooked)}/call" if cpbooked else None), note=booked_note),
        stage("Calls held", held, "Day AI", "live" if dayai_on else "needs", prev=real_booked),
        stage("Qualified", None, "CRM", "needs"),
        stage("Deals closed", None, "CRM", "needs"),
        stage("Cash collected", None, "CRM", "needs"),
    ]

    data = {
        "generated_at": vsl.get("generated_at"),
        "context": {"impressions": impressions, "reach": reach, "spend": money(spend),
                    "cpm": money(div(spend, impressions) * 1000) if impressions else None,
                    "ctr": pct(ctr)},
        "kpis": kpis,
        "funnel": funnel,
        "detail": {
            "video": vsl.get("video"), "avg_watched": vsl.get("avg_percent_watched"),
            "engagement_curve": vsl.get("engagement_curve", []),
            "followup": vsl.get("followup", {}), "rb2b": rb2b,
            "coverage": vsl.get("coverage", []), "angles": ad.get("angles", []),
            "ghl_reconciled": ghl, "field_reality": ad.get("field_reality", {}),
        },
    }

    template = (ROOT / "scripts/unified_template.html").read_text()
    out = ROOT / "dashboard/index.html"
    out.write_text(template.replace("/*__DATA__*/null", json.dumps(data)))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
