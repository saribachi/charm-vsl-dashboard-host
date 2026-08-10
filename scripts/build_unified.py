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


def adset_efficiency(bookings):
    """Per-ad-set: Meta spend/clicks (from ad_daily.json) joined to GHL bookings
    AND their post-call outcomes (held / qualified / no-show), matched on the
    canonicalized ad-set name. Cost-per-QUALIFIED is the real efficiency metric —
    cost-per-booking flatters ad sets whose bookings no-show or aren't a fit."""
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
    # bookings + post-call outcomes per canonicalized ad set (from real_bookings_detail)
    oc = {}
    for b in bookings or []:
        k = _canon(b.get("ad_set"))
        if not k:                       # direct / pre-UTM — no ad set to credit
            continue
        d = oc.setdefault(k, {"bookings": 0, "held": 0, "qualified": 0, "no_show": 0})
        d["bookings"] += 1
        if b.get("held"):
            d["held"] += 1
        if b.get("fit") == "qualified":
            d["qualified"] += 1
        if b.get("no_show"):
            d["no_show"] += 1
    rows = []
    for name, m in meta.items():
        o = oc.get(_canon(name), {"bookings": 0, "held": 0, "qualified": 0, "no_show": 0})
        bk, q, ns = o["bookings"], o["qualified"], o["no_show"]
        rows.append({"ad_set": name, "spend": round(m["spend"], 2), "clicks": int(m["clicks"]),
                     "bookings": bk, "held": o["held"], "qualified": q, "no_show": ns,
                     "cost_per_booking": round(m["spend"] / bk, 2) if bk else None,
                     "cost_per_qualified": round(m["spend"] / q, 2) if q else None,
                     "no_show_rate": round(100 * ns / bk, 1) if bk else None,
                     "booking_rate": round(100 * bk / m["clicks"], 1) if m["clicks"] else None,
                     "retarget": "retarget" in name.lower()})
    rows.sort(key=lambda r: (r["cost_per_booking"] is None, r["cost_per_booking"] or 0))
    return rows


def qualified_by_ad(bookings, eff=None):
    """Per AD (creative = utm_content): who booked, who turned out to be a fit,
    and the names behind the numbers. The ad-SET view can't answer "which ads
    bring the good ones" because one set runs several creatives — Statics 1 alone
    holds nine.

    Deliberately NO cost-per-qualified column here: the Meta export is broken down
    by ad SET, so per-creative spend does not exist yet (needs an "Ad name"
    breakdown in the export). Ad-set cost/qualified is carried alongside as the
    closest available proxy, labelled as the SET's number, not the ad's."""
    set_cost = {_canon(r["ad_set"]): r.get("cost_per_qualified") for r in (eff or [])}
    ads = {}
    for b in bookings or []:
        ad = b.get("ad")
        key = ad or DIRECT_AD
        d = ads.setdefault(key, {"ad": ad, "ad_set": b.get("ad_set"), "retarget": b.get("retarget"),
                                 "bookings": 0, "held": 0, "qualified": 0, "no_show": 0,
                                 "pending": 0, "people": []})
        d["bookings"] += 1
        if b.get("held"):
            d["held"] += 1
        if b.get("no_show"):
            d["no_show"] += 1
        if b.get("fit") == "qualified":
            d["qualified"] += 1
            d["people"].append({"name": b.get("name") or b.get("email"), "email": b.get("email"),
                                "start": b.get("start")})
        elif not b.get("fit") and not b.get("no_show") and not b.get("cancelled"):
            d["pending"] += 1     # no verdict yet — keeps the rate honest
    rows = []
    for key, d in ads.items():
        bk, q = d["bookings"], d["qualified"]
        d["name"] = key
        d["path"] = _path_of(d["ad_set"]) if d["ad_set"] else None
        d["qualified_rate"] = round(100 * q / bk, 1) if bk else None
        d["set_cost_per_qualified"] = set_cost.get(_canon(d["ad_set"])) if d["ad_set"] else None
        d["people"].sort(key=lambda p: p.get("start") or "")
        rows.append(d)
    # Best first: most qualified, then best hit-rate, then most bookings.
    rows.sort(key=lambda r: (-r["qualified"], -(r["qualified_rate"] or 0), -r["bookings"]))
    tot_q = sum(r["qualified"] for r in rows)
    return {"rows": rows,
            "total_qualified": tot_q,
            "attributed_qualified": sum(r["qualified"] for r in rows if r["ad"]),
            "unattributed_qualified": sum(r["qualified"] for r in rows if not r["ad"]),
            "pending_verdicts": sum(r["pending"] for r in rows),
            "ads_with_qualified": len([r for r in rows if r["qualified"]]),
            "per_ad_cost_available": False}


DIRECT_AD = "(direct / pre-UTM)"


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
        d = paths.setdefault(p, {"path": p, "spend": 0.0, "clicks": 0, "bookings": 0,
                                 "held": 0, "qualified": 0, "no_show": 0, "ad_sets": []})
        d["spend"] += r.get("spend") or 0
        d["clicks"] += r.get("clicks") or 0
        d["bookings"] += r.get("bookings") or 0
        d["held"] += r.get("held") or 0
        d["qualified"] += r.get("qualified") or 0
        d["no_show"] += r.get("no_show") or 0
        d["ad_sets"].append(r.get("ad_set"))
    out = []
    for d in paths.values():
        d["spend"] = round(d["spend"], 2)
        d["cost_per_booking"] = round(d["spend"] / d["bookings"], 2) if d["bookings"] else None
        d["cost_per_qualified"] = round(d["spend"] / d["qualified"], 2) if d["qualified"] else None
        d["no_show_rate"] = round(100 * d["no_show"] / d["bookings"], 1) if d["bookings"] else None
        out.append(d)
    out.sort(key=lambda r: (r["cost_per_booking"] is None, r["cost_per_booking"] or 0))
    return out


def _ad_rows():
    try:
        return json.loads((ROOT / "data/meta/ad_daily.json").read_text()).get("rows", [])
    except Exception:
        return []


def daily_spend():
    """Per-day total ad spend (summed across ad sets) — the number to watch as
    Meta's Lead event unthrottles delivery against the daily budget."""
    rows = _ad_rows()
    by = {}
    for r in rows:
        d = r.get("date")
        if d:
            by[d] = by.get(d, 0.0) + (r.get("spend") or 0)
    return [{"date": d, "spend": round(by[d], 2)} for d in sorted(by)]


def spend_window():
    """Human date range actually covered by ad_daily.json, e.g. "Jul 20 - Aug 4".
    Derived, never hardcoded: the KPI sub-label read "Jul 20-23" for two weeks
    after the data had moved on, which is how a truncated upload went unnoticed."""
    ds = [r["date"] for r in daily_spend()]
    if not ds:
        return None, 0
    def fmt(s):
        y, m, d = (int(x) for x in s.split("-"))
        return f"{MONTHS[m-1]} {d}"
    lo, hi = fmt(ds[0]), fmt(ds[-1])
    return (lo if lo == hi else f"{lo} - {hi}"), len(ds)


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


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
    n_committed = vsl.get("deals_committed") or 0  # verbal yes, not yet cash — never feeds ROAS

    # ── headline KPIs ──
    def kpi(label, value, status, sub):
        return {"label": label, "value": value, "status": status, "sub": sub}

    window, window_days = spend_window()
    n_sets = len({r.get("ad_set_name") for r in _ad_rows() if r.get("ad_set_name")})

    kpis = [
        kpi("Ad spend", money(spend), "live",
            f"{n_sets} ad set{'s' if n_sets != 1 else ''} · {window}" if window
            else "awaiting ad data"),
        kpi("Cost / click", money(cpc), "live" if cpc else "needs", f"{pct(ctr) or '—'} CTR"),
        kpi("Cost / form fill", money(cpff) if cpff else "—", "live" if cpff else "needs",
            f"GHL actual · {window}" if cpff else "awaiting real leads"),
        kpi("Cost / booked call", money(cpbooked) if cpbooked else "—", "live" if cpbooked else "needs",
            "target $270" if not cpbooked else f"vs $270 target · {window}"),
        kpi("ROAS", (f"{div(cc, spend):.1f}x" if cc else "—"),
            "live" if cc else "needs",
            "vs 5.0x target" if cc
            else (f"{n_committed} committed, awaiting first payment" if n_committed
                  else "no VSL closes yet")),
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

    # Committed = verbal yes, contract out, payment not collected. Named on the funnel
    # because the first one is a milestone worth seeing, but never folded into cash.
    cd = vsl.get("committed_detail") or []
    if cd:
        def _terms(c):
            name = c.get("title") or c.get("email") or "—"
            if c.get("monthly"):
                bits = f"{money(c['monthly'])}/mo"
                if c.get("setup"):
                    bits += f" + {money(c['setup'])} setup"
                return f"{name} — {bits}"
            return f"{name} — {money(c.get('year_one') or 0)} year one"
        who = " · ".join(_terms(c) for c in cd[:3])
        first = vsl.get("committed_first_invoice") or 0
        year_one = vsl.get("committed_value") or 0
        tail = (f"{money(first)} first invoice · {money(year_one)} year one" if first
                else f"{money(year_one)} year one")
        committed_note = f"{who} · {tail} — awaiting signature + first payment"
    else:
        committed_note = None

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
        stage("Committed (verbal yes)", vsl.get("deals_committed"), "Day AI",
              "live" if vsl.get("deals_committed") else "needs",
              prev=vsl.get("meetings_qualified"),
              note=(committed_note or "no VSL lead committed yet")),
        stage("Deals closed", vsl.get("deals_closed"), "Day AI",
              "live" if vsl.get("deals_closed") is not None else "needs",
              prev=(vsl.get("deals_committed") or vsl.get("meetings_qualified")),
              note=("signed + paid; none yet" if vsl.get("deals_closed") == 0 else None)),
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
            "daily_spend": daily_spend(),
            "daily_budget": 200,
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
                "adset_efficiency": (_eff := adset_efficiency(vsl.get("real_bookings_detail", []))),
                "path_efficiency": path_efficiency(_eff),
                "qualified_by_ad": qualified_by_ad(vsl.get("real_bookings_detail", []), _eff),
            },
        },
    }

    template = (ROOT / "scripts/unified_template.html").read_text()
    out = ROOT / "dashboard/index.html"
    out.write_text(template.replace("/*__DATA__*/null", json.dumps(data)))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
