#!/usr/bin/env python3
"""Build the unified single-page dashboard: one funnel, ad spend -> cash.

Reads data/_vsl.json and data/_ad.json (dumped by the two builds) and renders
one continuous funnel with the headline KPIs on top. Run after both builds.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _canon(s):
    """Canonical ad-set name for joining Meta (uses '+') to GHL utm_term (URL-decoded)."""
    return re.sub(r"\s+", " ", (s or "").lower().replace("+", " ")).strip()


def adset_efficiency(ba):
    """Per-ad-set: Meta spend/clicks (from ad_daily.json) joined to GHL bookings
    (bookings_attribution.by_adset, matched on canonicalized ad-set name)."""
    meta = {}
    try:
        for r in json.loads((ROOT / "data/meta/ad_daily.json").read_text()).get("rows", []):
            k = r.get("ad_set_name")
            if not k:
                continue
            m = meta.setdefault(k, {"spend": 0.0, "clicks": 0.0})
            m["spend"] += r.get("spend", 0) or 0
            m["clicks"] += r.get("link_clicks", 0) or 0
    except Exception:
        return []
    ghl = {}
    for k, v in (ba.get("by_adset") or {}).items():
        if k and k != "(unattributed / direct)":
            ghl[_canon(k)] = ghl.get(_canon(k), 0) + v
    rows = []
    for name, m in meta.items():
        bk = ghl.get(_canon(name), 0)
        rows.append({"ad_set": name, "spend": round(m["spend"], 2), "clicks": int(m["clicks"]),
                     "bookings": bk,
                     "cost_per_booking": round(m["spend"] / bk, 2) if bk else None,
                     "booking_rate": round(100 * bk / m["clicks"], 1) if m["clicks"] else None,
                     "retarget": "retarget" in name.lower()})
    rows.sort(key=lambda r: (r["cost_per_booking"] is None, r["cost_per_booking"] or 0))
    return rows


# Explicit ad-set → path map (Chris's roles). Ad-set NAMES don't self-describe
# because sets get repurposed — e.g. the videos now live in "GTM LinkedIn + Cold
# Email - REAL", so it's the pool-builder, not cold prospecting. Keyed by _canon.
ADSET_PATH = {
    "gtm linkedin cold email - real": "Video → Retargeting funnel",  # pool builder — holds the videos
    "retargeting video ads": "Video → Retargeting funnel",           # converter
    "gtm linkedin email statics 1": "Statics — standalone",
}


def _path_of(name):
    """Group an ad set into its funnel PATH. Video + Retargeting are one path
    (video builds the pool, retargeting converts it), so keying efficiency by
    ad set shows the pool-builder at zero forever — group by path instead.
    Explicit map wins; keyword fallback classifies any unmapped ad set."""
    if _canon(name) in ADSET_PATH:
        return ADSET_PATH[_canon(name)]
    n = (name or "").lower()
    if "static" in n:
        return "Statics — standalone"
    if "retarget" in n or "video" in n:
        return "Video → Retargeting funnel"
    if "cold" in n or "prospect" in n:
        return "Cold prospecting"
    return name or "Other"


def path_efficiency(eff):
    """Roll the per-ad-set efficiency rows up to the funnel path."""
    paths = {}
    for r in eff:
        p = _path_of(r.get("ad_set"))
        d = paths.setdefault(p, {"path": p, "spend": 0.0, "clicks": 0, "bookings": 0, "ad_sets": []})
        d["spend"] += r.get("spend") or 0
        d["clicks"] += r.get("clicks") or 0
        d["bookings"] += r.get("bookings") or 0
        d["ad_sets"].append(r.get("ad_set"))
    out = []
    for d in paths.values():
        d["spend"] = round(d["spend"], 2)
        d["cost_per_booking"] = round(d["spend"] / d["bookings"], 2) if d["bookings"] else None
        out.append(d)
    out.sort(key=lambda r: (r["cost_per_booking"] is None, r["cost_per_booking"] or 0))
    return out


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
    pc = vsl.get("post_call", {})
    rb2b = vsl.get("rb2b", {})

    ctr = div(clicks, impressions)
    cpc = div(spend, clicks)
    cpff = div(spend, real_forms)
    cpbooked = div(spend, real_booked)
    cc = vsl.get("cash_collected")  # VSL-attributed cash from Day AI (0 until a VSL lead closes)

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
        kpi("ROAS", (f"{div(cc, spend):.1f}x" if cc else "—"),
            "live" if cc else "needs",
            "vs 5.0x target" if cc else "no VSL closes yet"),
    ]

    # ── the funnel: click -> cash (bars scaled to link clicks) ──
    def stage(label, value, source, status, prev=None, cost=None, note=None, money_val=False):
        conv = None
        if value is not None and prev not in (None, 0) and not money_val:
            conv = pct(div(value, prev))
        bar = 0.0
        if value is not None and clicks and not money_val:
            bar = max(0.0, min(1.0, value / clicks))
        disp = (money(value) if money_val else f"{value:,.0f}") if isinstance(value, (int, float)) else None
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
        stage("Calls held", held, "Day AI + Chris", "live" if (held or 0) else "needs",
              prev=real_booked, note=" · ".join(
                  ([f"{pc.get('no_show')} no-show"] if pc.get("no_show") else [])
                  + ([f"{pc.get('pending')} awaiting post-call verdict"] if pc.get("pending") else [])) or None),
        stage("Qualified", (vsl.get("meetings_qualified") if pc.get("held") else None),
              "Chris (post-call)", "live" if pc.get("held") else "needs", prev=held,
              note=(f"{pc.get('unqualified',0)} not a fit" if pc.get("unqualified") else None)),
        stage("Deals closed", vsl.get("deals_closed"), "Day AI",
              "live" if vsl.get("deals_closed") is not None else "needs",
              prev=vsl.get("meetings_qualified"),
              note=("no VSL lead closed yet" if vsl.get("deals_closed") == 0 else None)),
        stage("Cash collected", vsl.get("cash_collected"), "Day AI",
              "live" if vsl.get("cash_collected") is not None else "needs", money_val=True),
    ]

    data = {
        "generated_at": vsl.get("generated_at"),
        "qualifier_form_date": vsl.get("qualifier_form_date"),
        "context": {"impressions": impressions, "reach": reach, "spend": money(spend),
                    "cpm": money(div(spend, impressions) * 1000) if impressions else None,
                    "ctr": pct(ctr)},
        "kpis": kpis,
        "funnel": funnel,
        "detail": {
            "video": vsl.get("video"), "avg_watched": vsl.get("avg_percent_watched"),
            "engagement_curve": vsl.get("engagement_curve", []),
            "rb2b": rb2b,
            "coverage": vsl.get("coverage", []), "angles": ad.get("angles", []),
            "ghl_reconciled": ghl, "field_reality": ad.get("field_reality", {}),
            "qualification": vsl.get("qualification", {}),
            "qualifier_impact": vsl.get("qualifier_impact", {}),
            "lead_quality": vsl.get("lead_quality", {}),
            "report": {
                "bookings": vsl.get("real_bookings_detail", []),
                "by_ad": ba.get("by_ad", {}),
                "by_adset": ba.get("by_adset", {}),
                "ad_attributed": ba.get("ad_attributed", 0),
                "retarget_booked": ba.get("retarget_booked", 0),
                "prospect_booked": ba.get("prospect_booked", 0),
                "total_bookings": ba.get("total_real", 0),
                "adset_efficiency": (_eff := adset_efficiency(ba)),
                "path_efficiency": path_efficiency(_eff),
            },
        },
    }

    template = (ROOT / "scripts/unified_template.html").read_text()
    out = ROOT / "dashboard/index.html"
    out.write_text(template.replace("/*__DATA__*/null", json.dumps(data)))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
