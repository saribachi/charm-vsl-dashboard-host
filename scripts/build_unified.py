#!/usr/bin/env python3
"""Build the unified single-page dashboard: one funnel, ad spend -> cash.

Reads data/_vsl.json and data/_ad.json (dumped by the two builds) and renders
one continuous funnel with the headline KPIs on top. Run after both builds.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _canon(s):
    """Canonical ad-set name for joining Meta (uses '+') to GHL utm_term (URL-decoded)."""
    return re.sub(r"\s+", " ", (s or "").lower().replace("+", " ")).strip()


_IDENTITY = None


def _identity():
    """Resolve ad sets by ID, not by name — Meta ad sets get RENAMED IN PLACE.

    Ad set 120248643976320447 was "GTM LinkedIn + Cold Email - REAL" through
    Jul 31 and "GTM LinkedIn + Cold Email - Videos" from Aug 1. Keying on the
    name split one ad set into two rows: the old name kept all the bookings
    (GHL stamped utm_term at click time) and the new name kept all the August
    spend, so the account's biggest spender read "$2,698, 0 bookings" while the
    old name read a flattering "$42/booking". Both numbers were fiction.

    Returns (display_by_id, id_by_canon_name): the most RECENT name is the one
    shown, and every name the set has ever carried aliases to its ID so old
    utm_terms still join."""
    global _IDENTITY
    if _IDENTITY is None:
        display, alias = {}, {}
        try:
            rows = json.loads((ROOT / "data/meta/ad_daily.json").read_text()).get("rows", [])
        except Exception:
            rows = []
        for r in sorted(rows, key=lambda r: r.get("date") or ""):
            i, n = r.get("ad_set_id"), r.get("ad_set_name")
            if not i or not n:
                continue
            display[i] = n          # ascending by date, so last write = current name
            alias[_canon(n)] = i
        _IDENTITY = (display, alias)
    return _IDENTITY


def _adset_key(name):
    """Stable join key for an ad-set name from either side (Meta row or GHL
    utm_term): its ID when we know it, else the canonical name."""
    _, alias = _identity()
    c = _canon(name)
    return alias.get(c, c)


def _adset_display(name):
    """Current name for whatever name we were handed (old utm_terms included)."""
    display, alias = _identity()
    return display.get(alias.get(_canon(name)), name)


def adset_efficiency(bookings):
    """Per-ad-set: Meta spend/clicks (from ad_daily.json) joined to GHL bookings
    AND their post-call outcomes (held / qualified / no-show), matched on the
    canonicalized ad-set name. Cost-per-QUALIFIED is the real efficiency metric —
    cost-per-booking flatters ad sets whose bookings no-show or aren't a fit."""
    meta = {}
    try:
        # GTM scope: this table is a GTM drill-down, and a CS ad set listed here would
        # always read zero bookings because CS bookings live on the CS calendar.
        for r in _ad_rows("gtm"):
            k = _adset_key(r.get("ad_set_name"))   # by ID: survives renames
            if not k:
                continue
            m = meta.setdefault(k, {"name": _adset_display(r.get("ad_set_name")),
                                    "spend": 0.0, "clicks": 0.0})
            m["spend"] += r.get("spend", 0) or 0
            m["clicks"] += r.get("link_clicks", 0) or 0
    except Exception:
        return []
    # bookings + post-call outcomes per ad set (from real_bookings_detail). GHL
    # stamped utm_term with whatever the set was called on the day of the click,
    # so resolve through the alias table or renamed sets lose their bookings.
    oc = {}
    for b in bookings or []:
        k = _adset_key(b.get("ad_set"))
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
    for key, m in meta.items():
        name = m["name"]
        o = oc.get(key, {"bookings": 0, "held": 0, "qualified": 0, "no_show": 0})
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
    set_cost = {_adset_key(r["ad_set"]): r.get("cost_per_qualified") for r in (eff or [])}
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
        d["set_cost_per_qualified"] = set_cost.get(_adset_key(d["ad_set"])) if d["ad_set"] else None
        d["people"].sort(key=lambda p: p.get("start") or "")
        rows.append(d)
    # Best first: most qualified, then best hit-rate, then most bookings.
    rows.sort(key=lambda r: (-r["qualified"], -(r["qualified_rate"] or 0), -r["bookings"]))
    tot_q = sum(r["qualified"] for r in rows)
    # Split the unresolved bookings into the two things they actually are. `pending`
    # (above) stays as-is because qualified_rate needs every unresolved booking in its
    # denominator — but it is NOT a to-do list: it counts calls that have not happened
    # yet. Only a call that was HELD and has no fit is genuinely awaiting a verdict.
    # Deduped by email, since a reschedule leaves the old row and adds a new one, which
    # otherwise counts one serial rescheduler several times over.
    awaiting, upcoming = set(), set()
    for b in bookings or []:
        if b.get("fit") or b.get("no_show") or b.get("cancelled"):
            continue
        who = (b.get("email") or "").strip().lower() or f"_row{id(b)}"
        (awaiting if b.get("held") else upcoming).add(who)
    upcoming -= awaiting          # a held-but-unjudged call is not also "upcoming"
    return {"rows": rows,
            "total_qualified": tot_q,
            "attributed_qualified": sum(r["qualified"] for r in rows if r["ad"]),
            "unattributed_qualified": sum(r["qualified"] for r in rows if not r["ad"]),
            "pending_verdicts": sum(r["pending"] for r in rows),
            "awaiting_verdicts": len(awaiting),
            "upcoming_calls": len(upcoming),
            "ads_with_qualified": len([r for r in rows if r["qualified"]]),
            "per_ad_cost_available": False}


DIRECT_AD = "(direct / pre-UTM)"


# Explicit ad-set → path map (Chris's roles). Ad-set NAMES don't self-describe
# because sets get repurposed — e.g. the videos now live in "GTM LinkedIn + Cold
# Email - REAL", so it's the pool-builder, not cold prospecting. Keyed by _canon.
ADSET_PATH = {
    "gtm linkedin cold email - real": "Video → Retargeting funnel",  # pool builder — holds the videos
    "gtm linkedin cold email - videos": "Video → Retargeting funnel",  # same set, renamed Aug 1
    "retargeting video ads": "Video → Retargeting funnel",           # converter
    "gtm linkedin email statics 1": "Statics — standalone",
}

# Ad sets launched since the last mapping review. Their PATH is Chris's call —
# the names describe the ad body, not the set's role in the funnel — so they are
# surfaced as unmapped rather than guessed at. The keyword fallback below would
# have filed "Your Prospect Might" under Cold prospecting purely on the word
# "prospect" in the ad copy, which is exactly the kind of confident-wrong
# grouping the explicit map exists to prevent.
UNMAPPED_LABEL = "Unmapped — needs a path"


def _path_of(name):
    """Group an ad set into its funnel PATH. Video + Retargeting are one path
    (video builds the pool, retargeting converts it), so keying efficiency by
    ad set shows the pool-builder at zero forever — group by path instead.

    Resolves renames first (the explicit map is keyed by name, and a set that
    gets renamed would otherwise silently fall through to the keyword guess)."""
    for key in (_canon(name), _canon(_adset_display(name))):
        if key in ADSET_PATH:
            return ADSET_PATH[key]
    n = (name or "").lower()
    if "static" in n:
        return "Statics — standalone"
    if "retarget" in n:
        return "Video → Retargeting funnel"
    return UNMAPPED_LABEL if name else "Other"


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


# Must match CS_ADSET_PREFIX in build_dashboard.py. Two funnels now share one Meta
# export, so every GTM number has to exclude CS ad sets or GTM's spend, CPC and cost
# per booking all silently absorb the other funnel's budget.
CS_ADSET_PREFIX = "cs flex"


def _is_cs_row(r):
    return (r.get("ad_set_name") or "").lower().startswith(CS_ADSET_PREFIX)


def _ad_rows(scope="gtm"):
    """Ad rows for one funnel. Defaults to GTM because every existing caller is a GTM
    view — a caller that wants everything has to ask for it explicitly."""
    try:
        rows = json.loads((ROOT / "data/meta/ad_daily.json").read_text()).get("rows", [])
    except Exception:
        return []
    if scope == "all":
        return rows
    if scope == "cs":
        return [r for r in rows if _is_cs_row(r)]
    return [r for r in rows if not _is_cs_row(r)]


def gtm_agg():
    """GTM-only spend/impressions/reach/clicks. The blended agg in _ad.json covers
    every ad set in the export, which now includes CS."""
    out = {"spend": 0.0, "impressions": 0, "reach": 0, "link_clicks": 0}
    for r in _ad_rows("gtm"):
        out["spend"] += r.get("spend") or 0
        out["impressions"] += int(r.get("impressions") or 0)
        out["reach"] += int(r.get("reach") or 0)
        out["link_clicks"] += int(r.get("link_clicks") or 0)
    out["spend"] = round(out["spend"], 2)
    return out


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

    # GTM-only: _ad.json's blended agg covers every ad set in the export, CS included.
    agg = gtm_agg()
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
    # Count by ID, not name — a renamed ad set is one ad set, not two.
    n_sets = len({r.get("ad_set_id") or _canon(r.get("ad_set_name"))
                  for r in _ad_rows("gtm") if r.get("ad_set_name")})

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

    # ── the funnel: impressions -> cash ──
    #
    # ONE POPULATION, ONE TIME BASE. Every row below counts ad-driven activity inside
    # the ad export's date window, so dividing a row by the row above it is always a
    # real conversion rate.
    #
    # What used to be here instead: Wistia's landing-page and video rows sat on this
    # spine. They count ALL page traffic (paid + direct + organic + repeat) over the
    # video's LIFETIME, so "visitors / link clicks" printed 129% — more people landing
    # than the ads ever sent. Below them, "form fills / watched >=50%" printed 660%,
    # because watching half of a 14-minute video was never a gate on filling the form
    # (average watch is 11%). Both rows now live in the engagement panel, off the spine,
    # with no conversion arrow into it. See `engagement` in the payload.
    #
    # `conv_of` names the denominator on every row. A percentage whose denominator you
    # cannot see is how 129% survived on this page for weeks.
    #
    # Bars scale WITHIN a group, not across the whole funnel: linear against impressions
    # would make every post-click row a 0.1% sliver and the whole funnel unreadable,
    # while a non-linear scale would misrepresent the drop. Two honest linear scales.
    def stage(label, value, source, status, prev=None, prev_label=None, group="conversion",
              cost=None, note=None, money_val=False, basis=None):
        conv = None
        if value is not None and prev not in (None, 0) and not money_val:
            conv = pct(div(value, prev))
        base = impressions if group == "traffic" else clicks
        bar = 0.0
        if value is not None and base and not money_val:
            bar = max(0.0, min(1.0, value / base))
        disp = (money(value) if money_val else f"{value:,.0f}") if isinstance(value, (int, float)) else None
        return {"label": label, "value": disp, "raw": value, "bar": bar, "conv": conv,
                "conv_of": prev_label, "group": group, "basis": basis,
                "source": source, "status": status, "cost": cost, "note": note}

    rb2b_note = (f"{rb2b.get('count', 0)} identified by RB2B" if rb2b.get("connected")
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

    # ── data freshness ────────────────────────────────────────────────────────
    # The spine draws on two clocks: Meta arrives by manual CSV upload and stops
    # wherever that export stopped, while GHL and Day AI are pulled live on every
    # rebuild. Cost-per-anything divides one by the other, so a lagging export makes
    # every cost metric read LOW — stale spend over live leads.
    #
    # Worse, Meta RESTATES recent days. The export loaded on Aug 20 2026 revised Aug 19
    # from ~7 GTM link clicks to 502; the post-rebuild click→form-fill rate read 6.4%
    # (healthy) on the partial data and 1.6% (junk) once the real numbers landed. A
    # partial export does not merely lag, it actively misleads — so the page has to say
    # how old the ad data is rather than leaving it to be inferred.
    ad_days = daily_spend()
    ad_end = ad_days[-1]["date"] if ad_days else None
    gen_day = (vsl.get("generated_at") or "")[:12]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lag = None
    if ad_end:
        try:
            lag = (datetime.strptime(today, "%Y-%m-%d")
                   - datetime.strptime(ad_end, "%Y-%m-%d")).days
        except Exception:
            lag = None
    freshness = {
        "ad_end": ad_end,
        "ad_window": window,
        "today": today,
        "lag_days": lag,
        # 0 = today's spend is in; 1 = yesterday's, which is normal because Meta's
        # same-day numbers are always incomplete. 2+ means the export is behind.
        "stale": (lag is not None and lag >= 2),
        "partial_today": (lag == 0),
        "generated_at": vsl.get("generated_at"),
    }

    ad_basis = f"paid · {window}" if window else "paid"
    live_basis = f"live at {freshness['generated_at']}" if freshness.get("generated_at") else "live"
    funnel = [
        stage("Impressions", impressions, "Meta", "live", group="traffic", basis=ad_basis,
              note=f"{reach:,} people reached" if reach else None),
        stage("Link clicks", clicks, "Meta", "live", group="traffic", basis=ad_basis,
              prev=impressions, prev_label="impressions (CTR)",
              cost=f"{money(cpc)}/click" if cpc else None),
        stage("Form fills", real_forms, "GHL", "live", prev=clicks, prev_label="link clicks",
              basis=f"{live_basis} · tests excluded",
              cost=(f"{money(cpff)}/lead" if cpff else None),
              note="the page and video sit between these two rows — see Page & video engagement"),
        stage("Booked calls", real_booked, "GHL", "live", prev=real_forms, prev_label="form fills",
              basis=live_basis, cost=(f"{money(cpbooked)}/call" if cpbooked else None), note=booked_note),
        stage("Calls held", held, "Day AI + Chris", "live" if (held or 0) else "needs",
              basis=live_basis,
              prev=real_booked, prev_label="booked calls", note=" · ".join(
                  ([f"{pc.get('no_show')} no-show"] if pc.get("no_show") else [])
                  + ([f"{pc.get('pending')} awaiting post-call verdict"] if pc.get("pending") else [])) or None),
        stage("Qualified", (vsl.get("meetings_qualified") if pc.get("held") else None),
              "Chris (post-call)", "live" if pc.get("held") else "needs",
              basis="live", prev=held, prev_label="calls held",
              note=(f"{pc.get('unqualified',0)} not a fit" if pc.get("unqualified") else None)),
        stage("Committed (verbal yes)", vsl.get("deals_committed"), "Day AI",
              "live" if vsl.get("deals_committed") else "needs",
              prev=vsl.get("meetings_qualified"), prev_label="qualified",
              note=(committed_note or "no VSL lead committed yet")),
        stage("Deals closed", vsl.get("deals_closed"), "Day AI",
              "live" if vsl.get("deals_closed") is not None else "needs",
              prev=(vsl.get("deals_committed") or vsl.get("meetings_qualified")),
              prev_label=("committed" if vsl.get("deals_committed") else "qualified"),
              note=("signed + paid; none yet" if vsl.get("deals_closed") == 0 else None)),
        stage("Cash collected", vsl.get("cash_collected"), "Day AI",
              "live" if vsl.get("cash_collected") is not None else "needs", money_val=True),
    ]

    data = {
        "generated_at": vsl.get("generated_at"),
        "qualifier_form_date": vsl.get("qualifier_form_date"),
        # Page + video engagement, deliberately OUTSIDE the funnel: it counts all page
        # traffic, not just ad traffic, so it has no honest conversion arrow into the
        # spine. Same builder as CS's — see engagement_block() in build_dashboard.py.
        "engagement": vsl.get("engagement") or {},
        "freshness": freshness,
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
            # The CS funnel rides alongside GTM rather than being merged into it: same
            # dataset and bridge, different offer, page, video, form and calendar.
            # Merging the two would make both unreadable.
            "cs": vsl.get("cs", {}),
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
