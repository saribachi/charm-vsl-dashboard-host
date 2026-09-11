#!/usr/bin/env python3
"""Build the Charm VSL funnel dashboard.

Pulls live data from Wistia + GHL, reads the Day AI snapshot, writes
dashboard/vsl_dashboard.html. Run: python3 scripts/build_dashboard.py
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import iclosed_source
import offers

ROOT = Path(__file__).resolve().parent.parent
# creds come from the environment (hosted/container) with .env overriding locally
ENV = dict(os.environ)
_env_file = ROOT / ".env"
if _env_file.exists():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            ENV[k.strip()] = v.strip()
# Imported modules (iclosed_source) read os.environ directly, so a .env-only value was
# invisible to them and every local build failed on ICLOSED_API_KEY while the container
# — where these are real env vars — worked fine. Push them back so both paths agree.
os.environ.update(ENV)

WISTIA_TOKEN = ENV["WISTIA_API_TOKEN"]
GHL_TOKEN = ENV["GHL_PIT_TOKEN"]
LOCATION_ID = ENV["GHL_LOCATION_ID"]

# ⚠️ THE VSL WAS REPLACED 26/27 Aug 2026 AND THE NEW ONES ARE 8x SHORTER.
#
#   GTM: swyi1909di "VSL 005"        14m38s  ->  18w5xszdv9 "small CHARM VSL"  1m51s
#   CS:  lk17fifkvg "CS VSL"         13m58s  ->  51low6lcfn "small CS VSL"     1m33s
#
# Engagement is NOT comparable across that boundary. "Watched 50%" of fourteen minutes and
# "watched 50%" of one minute fifty-one are different behaviours, and completion rate will
# jump for reasons that have nothing to do with the creative being better. Any trend line
# spanning the switch is measuring the edit, not performance. The prior ids are kept so
# history can be read deliberately, never blended.
VSL_MEDIA_ID = "18w5xszdv9"                 # small CHARM VSL — live GTM VSL (1m51s)
VSL_MEDIA_ID_PRIOR = "swyi1909di"           # VSL 005 (14m38s), live until 26 Aug 2026
VSL_SWITCHED_ON = "2026-08-26"
# The GHL form and calendar the GTM funnel ran on until 26 Aug 2026. Kept for the record
# and still referenced by the frozen historic snapshot; NOT fetched on a live build.
GTM_FORM_ID = "XwtroXXXZ58OVpL4pEqy"  # GTM Services form VSL ONLY
GTM_CALENDAR_ID = "KDdgICxdFa0FJQgNSt8c"  # Charm - GTM VSL ONLY

# iClosed event ids — the live source. Scoped per lane so GTM never counts a CS booking.
# Which iClosed meeting event belongs to which offer now lives in offers.py, so adding
# an offer is a registry entry rather than a new constant plus a new code path. GTM is
# still pulled by name here because main() computes its deep panels (Day AI attendance,
# RB2B, cash) that no other offer has a source for yet.
GTM = offers.BY_KEY["gtm"]

# GHL custom fields that capture the ad UTMs on the contact (written from the booking
# URL params — NOT in GHL's native attributionSource, which reads "Direct traffic").
UTM_FIELD_IDS = {
    "utm_source": "XbqI6HLGdJKCL18xfqrY",
    "utm_medium": "bCwzhLnzjAG1z0n3BuDg",
    "utm_campaign": "cADGo06z5WiKVDgULcaM",
    "utm_content": "XlRkyGbQihyHZeE2Bxxk",   # = ad name  (join key)
    "utm_term": "evvs35rWaeYbb8jzQNYe",       # = ad set name
}

# Qualification gate on the GTM form — three qualifier answers in the submission "others".
QUAL_FIELDS = {"revenue": "t8kIeNWMhGLyKmelKXYL",       # Annual Revenue
               "acv": "IUjCRF0gg4GKikd3DmlK",           # Average contract value
               "capacity": "UX6TIRA7aL6rjW65oKwV"}      # could you service 20 mtgs?


def classify_qual(others):
    """Deterministic per the GTM qualification spec; first match wins. Returns None
    for submissions that predate the qualifier form (no qualifier answers)."""
    rev = others.get(QUAL_FIELDS["revenue"])
    acv = others.get(QUAL_FIELDS["acv"])
    cap = others.get(QUAL_FIELDS["capacity"])
    if not (rev or acv or cap):
        return None
    if cap == "No, we're at capacity":
        return "dq_capacity"
    if acv == "Under $5K":
        return "dq_acv_low"
    if rev == "Under $1M" and acv == "$5K to $14K":
        return "dq_revenue_acv"
    return "qualified"


# When the qualifier questions were added — submissions/bookings before this predate
# the gate, so form-fill and booking rates before/after aren't directly comparable.
QUALIFIER_FORM_DATE = "2026-07-27"

# Day AI "Closed Won" stage — deals_closed/cash for VSL-attributed opportunities.
CLOSED_WON_STAGE_ID = "bef2d697-5f90-4b8e-a421-b6ee3e359aed"

# Day AI "Committed" stage — verbal yes, contract out for signature, NOT yet paid.
# Tracked separately from Closed Won on purpose: a committed deal is a real win to
# reference, but its Amount is contracted value, not cash. Cash and ROAS stay wired
# to Closed Won only, so nothing here can inflate collected revenue.
COMMITTED_STAGE_ID = "559edc45-0431-483b-abcd-f9d960469c63"

# Real contract terms per deal, keyed by the VSL lead's email.
# Day AI's Amount field is NOT reliable for committed deals: Macmoor's reads $129,000,
# which is the annualized CEILING ((4,500 base + 6,250 max scaling) x 12), not what was
# agreed. Chris confirmed the actual terms. Drop an entry once the CRM Amount is
# corrected — the fallback uses Amount whenever a deal has no override.
#
# Lives in the COMMITTED_TERMS_JSON env (like POST_CALL_JSON / AD_DAILY_JSON), NEVER in
# source: this is client email + commercial terms, and the deploy repo is PUBLIC.
# Shape: {"lead@example.com": {"monthly": 4500, "setup": 1000}}
try:
    COMMITTED_TERMS = json.loads(ENV.get("COMMITTED_TERMS_JSON") or "{}")
    CLOSED_TERMS = json.loads(ENV.get("CLOSED_TERMS_JSON") or "{}")
    VSL_LEAD_EXTRA = json.loads(ENV.get("VSL_LEAD_EXTRA_JSON") or "[]")
except ValueError:
    print("COMMITTED_TERMS_JSON is not valid JSON — falling back to CRM Amount")
    COMMITTED_TERMS = {}
    CLOSED_TERMS = {}
    VSL_LEAD_EXTRA = []

# Internal/test/invalid submitters — excluded from "real lead" counts.
#
# The individual addresses live in the TEST_EMAILS_JSON env, NEVER in source: they are
# personal addresses (and one real person who spam-submitted) and this deploy repo is
# PUBLIC. Shape: ["a@example.com", "b@example.com"].
# Domain-level exclusions stay in source — company domains, not personal data.
try:
    TEST_EMAILS = {e.strip().lower()
                   for e in json.loads(ENV.get("TEST_EMAILS_JSON") or "[]") if e.strip()}
except ValueError:
    print("TEST_EMAILS_JSON is not valid JSON — no per-address test exclusions applied")
    TEST_EMAILS = set()
if not TEST_EMAILS:
    # Loud on purpose: with no exclusions, test submissions count as REAL leads and
    # silently inflate form fills, bookings, and every cost-per metric derived from them.
    print("WARNING: TEST_EMAILS_JSON is empty — test submitters will count as real leads")
TEST_DOMAINS = {"hirecharm.com", "goober.com"}

# Extra addresses belonging to a lead already in the funnel. Lives in env, never in source:
# these are real people. Shape: {"second@example.com": "known-lead@example.com"}
try:
    VSL_LEAD_ALIASES = json.loads(ENV.get("VSL_LEAD_ALIASES_JSON") or "{}")
except ValueError:
    print("VSL_LEAD_ALIASES_JSON is not valid JSON — no alias attribution applied")
    VSL_LEAD_ALIASES = {}


def http_get(url, headers):
    req = urllib.request.Request(url, headers={"User-Agent": "charm-vsl-metrics/1.0",
                                               "Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def wistia(path):
    return http_get(f"https://api.wistia.com/v1/{path}",
                    {"Authorization": f"Bearer {WISTIA_TOKEN}"})


def ghl(path):
    sep = "&" if "?" in path else "?"
    return http_get(f"https://services.leadconnectorhq.com/{path}{sep}locationId={LOCATION_ID}",
                    {"Authorization": f"Bearer {GHL_TOKEN}", "Version": "2021-07-28"})


def _plus_stripped(e):
    """name+anything@gmail.com is the same inbox as name@gmail.com, so a tester who
    varies the suffix to get a fresh CRM contact is still the same person. Without
    this every new +suffix reads as a brand-new real lead and quietly inflates the
    funnel — which is exactly what +cs1 and +cs2 did to the CS numbers."""
    local, sep, domain = (e or "").partition("@")
    return (local.split("+", 1)[0] + sep + domain) if sep else e


# test@, test1@, test6@ … — the shape testers reach for when they need a fresh address.
# Anchored and digits-only after "test" on purpose: it must not catch a real person at
# testa@ or tester@ (Testa is a surname).
_TEST_LOCAL = re.compile(r"^test\d*@")


def is_test(email, name=""):
    e = (email or "").lower().strip()
    if e and (e in TEST_EMAILS or _plus_stripped(e) in TEST_EMAILS
              or e.split("@")[-1] in TEST_DOMAINS
              or _TEST_LOCAL.match(e)):
        return True
    # Whole word only. A substring match excludes real people — "Marco Testa" contains
    # "test", and a surname is a silent, permanent exclusion with nothing to show it.
    return bool(re.search(r"\b(test|goober)\b", (name or "").lower()))


# ------------------------------------------------------------- offer funnels --
# One generic funnel, driven by the registry in offers.py. This replaces cs_funnel(),
# which was a near-copy of the GTM computation and had already drifted from it (it
# divided play rate by page loads while GTM divided by visitors, so CS read 4.4% against
# GTM's 7.3% when like-for-like they were 8.2% and 7.3%). With five offers, five copies
# would drift five ways.

ANOMALY_MIN_LOADS = 60      # ignore quiet days, where any ratio is noise
ANOMALY_RATIO = 8           # loads per ad click before a day looks non-ad


def ad_daily_rows():
    """Every row of the shared Meta export. One file, many offers — always split it."""
    try:
        return json.loads((ROOT / "data/meta/ad_daily.json").read_text()).get("rows", [])
    except Exception:
        return []


def adset_assignment():
    """Which offer each ad set in the export belongs to, and how it was decided.

    Reported on the page rather than kept internal. GTM is the catch-all owner, so an ad
    set named outside every known prefix still counts (nothing silently drops out of
    spend) but shows as `default` — which is the only way a mis-assigned set is ever
    noticed. Ad set names get repurposed on this account: GTM's "- REAL" became
    "- Videos" mid-flight and split one set into two.
    """
    seen = {}
    for r in ad_daily_rows():
        name = r.get("ad_set_name")
        if name and name not in seen:
            key, how = offers.assign_adset(name)
            seen[name] = {"ad_set": name, "offer": key, "matched_by": how}
    return sorted(seen.values(), key=lambda x: (x["offer"], x["ad_set"]))


def offer_ad_rows(offer):
    return [r for r in ad_daily_rows()
            if offers.assign_adset(r.get("ad_set_name"))[0] == offer.key]


def ad_daily_for(offer):
    """Per-day spend/clicks/impressions for ONE offer, dated so the page can window it."""
    by = {}
    for r in offer_ad_rows(offer):
        d = r.get("date")
        if not d:
            continue
        acc = by.setdefault(d, {"date": d, "spend": 0.0, "clicks": 0, "impressions": 0, "reach": 0})
        acc["spend"] += float(r.get("spend") or 0)
        acc["clicks"] += int(r.get("link_clicks") or 0)
        acc["impressions"] += int(r.get("impressions") or 0)
        acc["reach"] += int(r.get("reach") or 0)
    for v in by.values():
        v["spend"] = round(v["spend"], 2)
    return [by[d] for d in sorted(by)]


def offer_ad_spend(offer):
    rows = offer_ad_rows(offer)
    return {
        "spend": round(sum(r.get("spend") or 0 for r in rows), 2),
        "clicks": int(sum(r.get("link_clicks") or 0 for r in rows)),
        "impressions": int(sum(r.get("impressions") or 0 for r in rows)),
        "ad_sets": sorted({r.get("ad_set_name") for r in rows if r.get("ad_set_name")}),
        "wired": bool(rows),
    }


def ad_clicks_by_day(offer):
    """Ad link clicks per calendar day for ONE offer. Used to sanity-check page loads:
    a day with far more page loads than ad clicks is not ad traffic."""
    return {r["date"]: r["clicks"] for r in ad_daily_for(offer)}


def ad_window(offer):
    """First/last date the export actually covers for ONE offer. Derived, never
    hardcoded — Wistia is windowed to this so the two sources share a time base."""
    days = sorted(ad_clicks_by_day(offer))
    return (days[0], days[-1]) if days else (None, None)


def engagement_block(media_stats, by_date, offer):
    """Page + video engagement for ONE offer, in a shape IDENTICAL across offers.

    Every field carries its own basis, because they are not the same:
      - loads / plays are WINDOWED to the ad export's date range (from by_date)
      - unique visitors is LIFETIME — Wistia's API exposes no per-day unique count,
        so it cannot honestly be windowed. Labelled rather than silently mixed.
    """
    stats = media_stats or {}
    start, end = ad_window(offer)
    rows = [r for r in (by_date or [])
            if not start or start <= (r.get("date") or "")[:10] <= end]

    loads_w = int(sum(r.get("load_count") or 0 for r in rows))
    plays_w = int(sum(r.get("play_count") or 0 for r in rows))
    visitors_life = int(stats.get("visitors") or 0)
    plays_life = int(stats.get("plays") or 0)
    loads_life = int(stats.get("pageLoads") or 0)

    # Play rate on ONE basis for every offer: plays per unique visitor, lifetime.
    # Wistia's own percentOfVisitorsClickingPlay rounds to whole percents, so it is
    # recomputed here to keep a decimal place.
    play_rate = round(100 * plays_life / visitors_life, 1) if visitors_life else None

    clicks_by_day = ad_clicks_by_day(offer)
    anomalies = []
    for r in rows:
        d = (r.get("date") or "")[:10]
        loads = int(r.get("load_count") or 0)
        clicks = clicks_by_day.get(d, 0)
        if loads >= ANOMALY_MIN_LOADS and loads > max(1, clicks) * ANOMALY_RATIO:
            anomalies.append({"date": d, "loads": loads, "clicks": clicks,
                              "plays": int(r.get("play_count") or 0)})
    anomalies.sort(key=lambda a: -a["loads"])

    return {
        "window": {"start": start, "end": end},
        "loads_window": loads_w,
        "plays_window": plays_w,
        "loads_lifetime": loads_life,
        "visitors_lifetime": visitors_life,
        "plays_lifetime": plays_life,
        "play_rate": play_rate,                       # plays / unique visitors, lifetime
        "play_rate_basis": "plays per unique visitor (lifetime)",
        "avg_percent_watched": stats.get("averagePercentWatched"),
        "anomalies": anomalies,
        "anomaly_loads": sum(a["loads"] for a in anomalies),
        # Per-day rows so the timeframe control can window loads and plays. Unique
        # visitors deliberately has no daily equivalent — see the note above.
        "daily": [{"date": (r.get("date") or "")[:10],
                   "loads": int(r.get("load_count") or 0),
                   "plays": int(r.get("play_count") or 0)} for r in (by_date or [])],
    }


def offer_video(offer, now):
    """Wistia stats for an offer that owns a video. ({}, []) when it owns none."""
    mid = (offer.wistia or {}).get("media_id")
    if not mid:
        return {}, [], None
    # Stats live on medias/{id}/stats.json. The plain medias/{id}.json returns the media
    # record with stats:null, which silently reads as a video nobody watched.
    try:
        stats = (wistia(f"medias/{mid}/stats.json") or {}).get("stats", {}) or {}
    except Exception as e:
        print(f"  {offer.key}: Wistia stats failed: {e}")
        stats = {}
    since = offer.wistia.get("stats_since") or offer.live_from
    try:
        by_date = wistia(f"stats/medias/{mid}/by_date.json"
                         f"?start_date={since}&end_date={now.strftime('%Y-%m-%d')}") or []
    except Exception as e:
        print(f"  {offer.key}: Wistia by_date failed: {e}")
        by_date = []
    try:
        name = (wistia(f"medias/{mid}.json") or {}).get("name")
    except Exception:
        name = None
    return stats, by_date, name


def offer_funnel(offer, now):
    """One offer, end to end — the same computation for all five.

    Returns dated ROW LISTS alongside the totals. The totals are the all-time figures;
    the rows are what the timeframe control re-aggregates in the browser. Anything that
    cannot honestly be windowed (lifetime unique visitors) is returned as a total only
    and flagged, never faked into a daily series.
    """
    stats, by_date, video_name = offer_video(offer, now)

    try:
        subs = iclosed_source.submissions(offer)
    except Exception as e:
        print(f"  {offer.key}: iClosed contacts failed: {e}")
        subs = []
    real_subs = [x for x in subs if not is_test(x.get("email"), x.get("name"))]

    try:
        events = iclosed_source.appointments(offer)
    except Exception as e:
        print(f"  {offer.key}: iClosed calls failed: {e}")
        events = []
    real_events = [e for e in events if not is_test(e.get("_email"), e.get("_name"))]
    booked = [e for e in real_events if e.get("appointmentStatus") not in ("cancelled", "noshow")]

    # --- dated rows: what the timeframe filter re-aggregates ---
    fill_rows = [{
        "date": (x.get("createdAt") or "")[:10],
        "ts": x.get("createdAt"),
        "email": x.get("email"),
        "name": x.get("name"),
        "qual": x.get("_qual"),
        "status": x.get("_status"),
        "gate_leak": x.get("_gate_leak", False),
        "answers": x.get("answers") or {},
    } for x in real_subs]

    booking_rows = [{
        # Two dates, two questions. `booked` is when the call was made; `call` is when it
        # runs. A call booked on Friday for the following Tuesday belongs to different
        # weeks depending on which one you ask about, so both travel to the page.
        "booked": (e.get("_booked_at") or "")[:10],
        "call": (e.get("_call_at") or "")[:10],
        "email": e.get("_email"),
        "name": e.get("_name"),
        "ad": e.get("_utm_content"),
        "ad_set": e.get("_utm_term"),
        "utm_source": e.get("_utm_source"),
        "status": e.get("appointmentStatus"),
        "rescheduled": e.get("_rescheduled"),
        "cancel_reason": e.get("_cancel_reason"),
        "outcome": e.get("_call_outcome"),
        "no_sale_reason": e.get("_no_sale_reason"),
        "objection": e.get("_objection"),
    } for e in real_events]

    # --- attribution ---
    by_ad, by_adset = {}, {}
    for e in booked:
        if e.get("_utm_content"):
            by_ad[e["_utm_content"]] = by_ad.get(e["_utm_content"], 0) + 1
        if e.get("_utm_term"):
            by_adset[e["_utm_term"]] = by_adset.get(e["_utm_term"], 0) + 1

    # --- answer mix, in the offer's OWN canonical keys ---
    mix = {k: {} for _s, k, _f in offer.questions}
    for x in real_subs:
        for key, val in (x.get("answers") or {}).items():
            if key in mix and val:
                mix[key][str(val)] = mix[key].get(str(val), 0) + 1

    # --- qualification, only where a gate exists ---
    qualification = None
    if offer.gate:
        graded = [x for x in fill_rows if x.get("qual")]
        qmix = {}
        for x in graded:
            qmix[x["qual"]] = qmix.get(x["qual"], 0) + 1
        n_with, n_q = len(graded), qmix.get("qualified", 0)
        booked_emails = {(e.get("_email") or "").lower() for e in booked} - {""}
        q_emails = {(x["email"] or "").lower() for x in graded if x["qual"] == "qualified"} - {""}
        leaked = [x for x in fill_rows if x.get("gate_leak")]
        qualification = {
            "with_answers": n_with, "qualified": n_q, "mix": qmix,
            # Leads the gate should have stopped who hold a booking anyway. Not folded
            # into any rate — it is a defect count, not a conversion.
            "gate_leak": len(leaked),
            "gate_leak_detail": [{"email": x["email"], "qual": x["qual"],
                                  "status": x["status"]} for x in leaked],
            "qualification_rate": round(100 * n_q / n_with, 1) if n_with else None,
            "booked_qualified": len(q_emails & booked_emails),
            "calendar_completion": round(100 * len(q_emails & booked_emails) / n_q, 1) if n_q else None,
        }

    # --- iClosed's own post-call outcomes (partial; see iclosed_source._call_outcome) ---
    oc_mix, ns_mix = {}, {}
    for e in real_events:
        if e.get("_call_outcome"):
            oc_mix[e["_call_outcome"]] = oc_mix.get(e["_call_outcome"], 0) + 1
        if e.get("_no_sale_reason"):
            ns_mix[e["_no_sale_reason"]] = ns_mix.get(e["_no_sale_reason"], 0) + 1

    ad = offer_ad_spend(offer)
    eng = engagement_block(stats, by_date, offer) if stats or by_date else None
    fills, books = len(real_subs), len(booked)
    div = lambda a, b: round(a / b, 2) if b else None

    return {
        "key": offer.key, "name": offer.name, "tag": offer.tag, "color": offer.color,
        "domain": offer.domain, "note": offer.note, "live_from": offer.live_from,
        "wiring": offer.wiring,
        "event_ids": offer.iclosed_event_ids,
        "video": ({"id": offer.wistia.get("media_id"), "name": video_name,
                   "duration": stats.get("duration")} if offer.wistia.get("media_id") else None),
        "ad": ad,
        "ad_daily": ad_daily_for(offer),
        "engagement": eng,
        "form_fills": fills,
        "form_fills_all": len(subs),          # incl. tests, so the gap is visible
        "booked": books,
        "booked_all": len(events),
        "cancelled": sum(1 for e in real_events if e.get("appointmentStatus") == "cancelled"),
        "noshow": sum(1 for e in real_events if e.get("appointmentStatus") == "noshow"),
        "by_ad": by_ad,
        "by_adset": by_adset,
        "qualifier_mix": mix,
        "qualification": qualification,
        "outcomes": {"mix": oc_mix, "no_sale_reasons": ns_mix,
                     "with_outcome": sum(1 for e in real_events if e.get("_call_outcome")),
                     "total_calls": len(real_events)},
        "rows": {"fills": fill_rows, "bookings": booking_rows,
                 "ad_daily": ad_daily_for(offer),
                 "video_daily": (eng or {}).get("daily", [])},
        "cost_per_fill": div(ad["spend"], fills),
        "cost_per_booking": div(ad["spend"], books),
        "fill_rate": round(100 * fills / ad["clicks"], 1) if ad["clicks"] else None,
        "booking_rate": round(100 * books / fills, 1) if fills else None,
    }


def main():
    now = datetime.now(timezone.utc)
    # The live era begins at the video switch. Anything earlier belongs to the frozen
    # historic tab: a different video of a different length on a different data source.
    since = VSL_SWITCHED_ON
    until = now.strftime("%Y-%m-%d")

    print("Pulling Wistia…")
    media = wistia(f"medias/{VSL_MEDIA_ID}/stats.json")
    by_date = wistia(f"stats/medias/{VSL_MEDIA_ID}/by_date.json?start_date={since}&end_date={until}")
    engagement = wistia(f"stats/medias/{VSL_MEDIA_ID}/engagement.json")
    duration = wistia(f"medias/{VSL_MEDIA_ID}.json").get("duration", 0)

    # Live funnel comes from iClosed. GHL stopped receiving form fills and bookings at the
    # cutover: its forms and calendars still answer, they just never grow again, so a live
    # pull returns a funnel that flatlines and reads as a collapse. Everything before the
    # cutover is frozen in data/frozen/historic_era.json and shown on its own tab.
    #
    # iclosed_source emits GHL-SHAPED rows on purpose, so every computation below this
    # point is untouched by the migration.
    print("Pulling iClosed…")
    subs = iclosed_source.submissions(GTM)
    events = iclosed_source.appointments(GTM)
    print(f"  {len(subs)} contact(s) · {len(events)} call(s)")

    snap_path = ROOT / "data/dayai/contacts_snapshot.json"
    dayai = json.loads(snap_path.read_text()) if snap_path.exists() else {
        "pulled_at": now.strftime("%Y-%m-%d"), "contacts": []}

    # Persist raw pulls
    (ROOT / "data/wistia/vsl_stats.json").write_text(json.dumps(
        {"media": media, "by_date": by_date, "engagement": engagement, "duration": duration}, indent=1))
    (ROOT / "data/ghl/gtm_form_submissions.json").write_text(json.dumps(subs, indent=1))
    (ROOT / "data/ghl/gtm_vsl_appointments.json").write_text(json.dumps(events, indent=1))

    # ---- compute funnel ----
    s = media["stats"]
    sub_rows = [{"date": (x.get("createdAt") or "")[:10],
                 "ts": x.get("createdAt"),
                 "email": x.get("email"),
                 "name": x.get("name"),
                 "website": (x.get("others") or {}).get("website"),
                 # iClosed decides qualification itself, so its verdict wins where it
                 # exists. classify_qual stays the fallback for the frozen GHL era,
                 # whose rows predate iClosed and carry no status.
                 "qual": x.get("_qual") or classify_qual(x.get("others") or {}),
                 "gate_leak": x.get("_gate_leak", False),
                 "test": is_test(x.get("email"), x.get("name"))} for x in subs]
    real_subs = [x for x in sub_rows if not x["test"]]

    eng = engagement.get("engagement_data", [])
    plays = s["plays"] or 1
    n = len(eng) or 1
    # engagement_data[i] = watch count for slice i (rewatches included); cap at 100%
    curve = [min(100.0, 100.0 * v / plays) for v in eng]
    watched_50 = round(plays * (curve[n // 2] / 100.0)) if eng else 0

    booked = [e for e in events if e.get("appointmentStatus") not in ("cancelled", "noshow")]
    # iClosed carries the invitee email and the UTMs on the call itself, so the per-contact
    # GHL lookup this used to need is gone. contact_cache is kept (empty) because a later
    # block still reads names out of it for contacts that predate the cutover.
    contact_cache = {}
    for e in events:
        e["_test"] = is_test(e.get("_email"), e.get("_name"))
    real_booked = [e for e in booked if not e["_test"]]

    # person-level attribution: bookings grouped by ad (utm_content = ad name).
    # UTMs were configured Jul 24 2026 — bookings before that read "Direct traffic".
    DIRECT = "(unattributed / direct)"
    bookings_by_ad, bookings_by_adset = {}, {}
    for e in real_booked:
        ad = e.get("_utm_content") or DIRECT
        bookings_by_ad[ad] = bookings_by_ad.get(ad, 0) + 1
        adset = e.get("_utm_term") or DIRECT
        bookings_by_adset[adset] = bookings_by_adset.get(adset, 0) + 1
    ad_attributed = sum(v for k, v in bookings_by_ad.items() if k != DIRECT)
    is_retarget = lambda s: "retarget" in (s or "").lower()
    retarget_booked = sum(v for k, v in bookings_by_adset.items() if is_retarget(k))
    prospect_booked = ad_attributed - retarget_booked

    # ---- qualification gate (GTM form) — submissions carrying qualifier answers ----
    qual_rows = [x for x in sub_rows if not x["test"] and x.get("qual")]
    qual_mix = {}
    for x in qual_rows:
        qual_mix[x["qual"]] = qual_mix.get(x["qual"], 0) + 1
    n_with = len(qual_rows)
    n_qualified = qual_mix.get("qualified", 0)
    booked_emails = {(e.get("_email") or "").lower() for e in real_booked} - {""}
    qualified_emails = {(x["email"] or "").lower() for x in qual_rows if x["qual"] == "qualified"} - {""}
    booked_qualified = len(qualified_emails & booked_emails)
    leaked_rows = [x for x in sub_rows if not x["test"] and x.get("gate_leak")]
    qualification = {
        "with_answers": n_with,                      # real subs carrying the qualifier answers
        "qualified": n_qualified,
        # Leads whose answers fail the gate but who booked regardless. Reported as a
        # count, never folded into qualification rate: it is a defect, not a conversion.
        "gate_leak": len(leaked_rows),
        "gate_leak_detail": [{"email": x["email"], "qual": x["qual"]} for x in leaked_rows],
        "mix": qual_mix,                             # counts by dq_capacity/dq_acv_low/dq_revenue_acv/qualified
        "qualification_rate": round(100 * n_qualified / n_with, 1) if n_with else None,
        "booked_qualified": booked_qualified,
        "calendar_completion": round(100 * booked_qualified / n_qualified, 1) if n_qualified else None,
    }
    # ---- qualifier-impact: before vs after the gate went live (Jul 27) ----
    def cohort(rows):
        n = len(rows)
        bk = sum(1 for x in rows if (x.get("email") or "").lower() in booked_emails)
        return {"subs": n, "booked": bk,
                "booking_rate": round(100 * bk / n, 1) if n else None}
    before = [x for x in real_subs if x["date"] and x["date"] < QUALIFIER_FORM_DATE]
    after = [x for x in real_subs if x["date"] and x["date"] >= QUALIFIER_FORM_DATE]
    qualifier_impact = {"gate_date": QUALIFIER_FORM_DATE,
                        "before": cohort(before), "after": cohort(after)}

    # ---- lead quality: free-mail vs company domain, website, email↔site match ----
    FREE_MAIL = {"gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
                 "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
                 "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
                 "pm.me", "gmx.com", "gmx.net", "mail.com", "zoho.com", "yandex.com", "hey.com",
                 "comcast.net", "verizon.net", "sbcglobal.net", "att.net", "cox.net", "fastmail.com"}

    def email_dom(s):
        s = (s or "").lower().strip()
        return s.rsplit("@", 1)[-1] if "@" in s else ""

    def site_dom(s):
        s = (s or "").lower().strip()
        s = re.sub(r"^https?://", "", s)
        s = re.sub(r"^www\.", "", s).split("/")[0].split("?")[0]
        return s.strip()

    def quality_grp(rows):
        n = len(rows)
        bk = sum(1 for x in rows if (x.get("email") or "").lower() in booked_emails)
        return {"n": n, "booked": bk, "booking_rate": round(100 * bk / n, 1) if n else None}

    comp_rows = [x for x in real_subs if email_dom(x.get("email")) and email_dom(x.get("email")) not in FREE_MAIL]
    free_rows = [x for x in real_subs if email_dom(x.get("email")) in FREE_MAIL]
    n_subs = len(real_subs)
    n_site = sum(1 for x in real_subs if site_dom(x.get("website")))
    free_with_site = sum(1 for x in free_rows if site_dom(x.get("website")))
    lead_quality = {
        "total": n_subs,
        "company": len(comp_rows), "free": len(free_rows),
        "company_pct": round(100 * len(comp_rows) / n_subs, 1) if n_subs else None,
        "free_pct": round(100 * len(free_rows) / n_subs, 1) if n_subs else None,
        "website_pct": round(100 * n_site / n_subs, 1) if n_subs else None,
        "free_with_site": free_with_site,   # free-mail leads that still have a real business site
        "free_with_site_pct": round(100 * free_with_site / len(free_rows), 1) if free_rows else None,
        "by_type": {"company": quality_grp(comp_rows), "free": quality_grp(free_rows)},
    }

    dayai_gtm = [c for c in dayai["contacts"] if c["form"] == "GTM Services"]

    # ground-truth GHL counts for the ad funnel to reconcile against (Meta's Lead
    # event isn't firing, so GHL form fills are the real conversion source)
    (ROOT / "data/ghl/summary.json").write_text(json.dumps({
        "generated_at": now.strftime("%Y-%m-%d %H:%M UTC"),
        "real_form_fills": len(real_subs),
        "form_fills_incl_test": len(sub_rows),
        "real_bookings": len(real_booked),
    }, indent=1))

    # ---- RB2B identified visitors (standalone receiver, optional) ----
    rb2b_visitors = []
    rb2b_ok = False
    endpoint = ENV.get("RB2B_ENDPOINT")
    if endpoint:
        try:
            hdr = {"x-rb2b-secret": ENV.get("RB2B_SECRET", "")}
            rb2b_visitors = http_get(endpoint.rstrip("/") + "/rb2b/visitors", hdr).get("visitors", [])
            rb2b_ok = True
        except Exception as ex:
            print(f"RB2B endpoint unreachable ({ex}); showing empty.")
    # did each identified visitor also submit the form? (match on email or name)
    sub_emails = {x["email"].lower() for x in sub_rows if x.get("email")}
    sub_names = {(x["name"] or "").strip().lower() for x in sub_rows if x.get("name")}
    for v in rb2b_visitors:
        em = (v.get("email") or "").lower()
        nm = (v.get("name") or "").strip().lower()
        v["submitted"] = bool((em and em in sub_emails) or (nm and nm in sub_names))
    rb2b_no_form = [v for v in rb2b_visitors if not v["submitted"]]

    sub_by_day = {}
    for x in sub_rows:
        sub_by_day[x["date"]] = sub_by_day.get(x["date"], 0) + 1

    daily = [{"date": r["date"], "loads": r["load_count"], "plays": r["play_count"],
              "subs": sub_by_day.get(r["date"], 0)} for r in by_date]
    # include submission days outside wistia range
    wistia_days = {r["date"] for r in by_date}
    for d_, c in sorted(sub_by_day.items()):
        if d_ and d_ not in wistia_days:
            daily.append({"date": d_, "loads": 0, "plays": 0, "subs": c})
    daily.sort(key=lambda r: r["date"])

    # ---- post-call qualification (manual, from Chris) — SEPARATE from the form gate ----
    pc_path = ROOT / "data/manual/post_call.json"
    post_call_leads = {}
    if pc_path.exists():
        post_call_leads = {k.lower(): v for k, v in
                           json.loads(pc_path.read_text()).get("leads", {}).items()}

    # ---- held-call status from Day AI (show-rate), guarded to real booked leads ----
    # A call is "held" only when the booked lead's EMAIL is an attendee on a Day AI
    # meeting recording. Email-only (deterministic) — no name/title matching, which
    # produced false positives (e.g. "mark" matching "Go-to-Market").
    hist_path_for_leads = ROOT / "data/frozen/historic_era.json"
    if not hist_path_for_leads.exists():
        hist_path_for_leads = ROOT / "scripts/historic_era.json"
    dayai_conn, meetings_held, deals_closed, cash_collected = False, None, None, None
    closed_detail = []
    attended = no_transcript = None
    showed = no_show = cancelled = awaiting = past_calls = 0
    deals_committed, committed_value, committed_detail = None, None, []
    committed_first_invoice = None
    try:
        import dayai as _dayai
        if _dayai.available():
            dayai_conn = True
            day = _dayai.DayAI()
            meetings = day.recent_meetings("2026-06-01T00:00:00Z")

            # iClosed names its calendar events "<First> with Charm @ <D Mon YYYY> - <HH:MM>",
            # and Day AI exposes the prospect only as an object UUID — their email never
            # appears in `attendees`. So for every booking made since the cutover, the email
            # match below cannot fire and the name match cannot either: the title carries a
            # FIRST name only, while the name rule deliberately requires two tokens so
            # "mark" does not match "Go-to-Market". Result was 0 of 14 held, which reads as
            # a total no-show week rather than a broken matcher.
            #
            # Matched on first name PLUS the meeting date, which the title also carries.
            # Not on the time: the title renders it in the invitee's timezone, not UTC.
            def iclosed_title_match(name, start_iso, mt):
                title = (mt.get("title") or "")
                # "Canceled: Updated - Sarah with Charm @ ..." — a cancelled call is not a
                # held call, and the prefix is the only thing distinguishing it.
                if re.match(r"^\s*(canceled|cancelled)\b", title, re.I):
                    return False
                first = next((t for t in re.findall(r"[A-Za-z]+", name or "") if len(t) > 1), "")
                if not first:
                    return False
                try:
                    d = datetime.fromisoformat((start_iso or "").replace("Z", "+00:00"))
                except ValueError:
                    return False
                # "28 Aug 2026" — no leading zero, matching how iClosed renders it.
                stamp = f"{d.day} {d.strftime('%b %Y')}"
                return bool(re.search(rf"\b{re.escape(first)}\b\s+with\s+Charm\s*@\s*{re.escape(stamp)}", title, re.I))

            # Returns the MATCHED meeting rather than a bool, so the caller can read its
            # transcript state. Attendance and existence are different questions and the
            # old bool could only answer the second one.
            def matched_meeting(name, email, start_iso=None):
                email = (email or "").lower()
                for mt in meetings:
                    if email and email in mt["attendees"]:
                        return mt
                if start_iso:
                    for mt in meetings:
                        if iclosed_title_match(name, start_iso, mt):
                            return mt
                return None

            def is_held(name, email, start_iso=None):
                if matched_meeting(name, email, start_iso):
                    return True
                # exact FULL name (>=2 word-boundary tokens) so "mark" != "market"
                toks = [t for t in re.findall(r"[a-z]+", (name or "").lower()) if len(t) > 2]
                if len(toks) >= 2:
                    pats = [re.compile(r"\b" + re.escape(t) + r"\b") for t in toks]
                    for mt in meetings:
                        title = mt["title"].lower()
                        if all(p.search(title) for p in pats):
                            return True
                return False

            for e in real_booked:
                # contact_cache is empty since the iClosed cutover — the per-contact GHL
                # lookup that filled it is gone — so the name has to come off the row.
                nm = e.get("_name") or contact_cache.get(e.get("contactId"), {}).get("name")
                mt = matched_meeting(nm, e.get("_email"), e.get("startTime"))
                held = bool(mt)
                # A call scheduled in the future can't have been held yet. Day AI
                # creates a meeting-recording object for a booked-but-upcoming call,
                # which the name match would otherwise count as held (e.g. Ellio).
                try:
                    e["_past"] = datetime.fromisoformat(e.get("startTime") or "") <= now
                    if held and not e["_past"]:
                        held = False
                except ValueError:
                    e["_past"] = False
                e["_held"] = held

                # THREE states, not two. A booked meeting object exists whether or not
                # anybody turned up, so "a meeting exists" was counting no-shows as held
                # and reported 5 of 5 on a day with real no-shows. Day AI writes `topic`
                # from the transcript, so its presence is the attendance signal.
                #
                # "no transcript" is deliberately NOT reported as a confirmed no-show:
                # it also happens when the notetaker fails to join or the call runs
                # somewhere Day AI is not. Those are different problems with different
                # fixes, so the number says "unconfirmed" and lets a human look.
                if not held:
                    e["_transcript"] = "upcoming" if not e.get("_past") else "no_meeting"
                elif mt.get("transcribed"):
                    e["_transcript"] = "attended"
                else:
                    e["_transcript"] = "no_transcript"

            # ---- attendance: the Day AI contact property is now the source of truth ----
            #
            # `Discovery Attended` is written per contact by the workspace. It replaced
            # the manual queue on the dashboard, where Chris had to confirm every call by
            # hand, and it replaces transcript-sniffing as the headline signal: a missing
            # transcript is ambiguous (no-show, or the notetaker failed to join), while
            # this property is somebody's actual verdict.
            #
            # ⚠️ ABSENCE IS NOT A NO-SHOW. Only the contacts with a recorded verdict count
            # toward the rate; everyone else is "awaiting a verdict" and is reported as
            # coverage, never folded into the denominator. Reading a blank as a no-show is
            # the same error as the old "a meeting object exists, so it was held" — which
            # printed 5 of 5 on a day with two real no-shows — just pointing the other way.
            try:
                verdicts = day.discovery_attendance([e.get("_email") for e in real_booked])
            except Exception as exc:
                verdicts = {}
                print(f"  Day AI attendance property pull failed: {exc}")

            for e in real_booked:
                v = verdicts.get((e.get("_email") or "").lower())
                e["_attendance"] = v or ("upcoming" if not e.get("_past") else "awaiting")
                # `_held` still drives the post-call fit control and the funnel row. A
                # recorded "showed" is authoritative; with no verdict yet it falls back to
                # the transcript signal so the funnel does not collapse to 2 overnight
                # while the property backfills. The two sources are reported separately
                # below so the fallback is never mistaken for a verdict.
                e["_held"] = True if v == "showed" else (False if v in ("no_show", "cancelled")
                                                         else e.get("_held", False))

            showed = sum(1 for e in real_booked if e.get("_attendance") == "showed")
            no_show = sum(1 for e in real_booked if e.get("_attendance") == "no_show")
            cancelled = sum(1 for e in real_booked if e.get("_attendance") == "cancelled")
            past_calls = sum(1 for e in real_booked if e.get("_past"))
            awaiting = sum(1 for e in real_booked if e.get("_attendance") == "awaiting")
            meetings_held = sum(1 for e in real_booked if e.get("_held"))
            attended = sum(1 for e in real_booked if e.get("_transcript") == "attended")
            no_transcript = sum(1 for e in real_booked if e.get("_transcript") == "no_transcript")
            print(f"Attendance (Day AI property) — {showed} showed, {no_show} no-show, "
                  f"{cancelled} cancelled · {awaiting} of {past_calls} past call(s) awaiting a verdict")
            print(f"  transcript signal, secondary: {attended} with a transcript, {no_transcript} without")

            # VSL-attributed closed deals + cash from Day AI Closed Won opps.
            # Match ONLY real external VSL leads (drop internal reps who are on every deal).
            # EVERY lead the funnel has ever produced, not just this era's.
            #
            # This was built from sub_rows alone, which after the cutover holds only
            # post-cutover contacts. A lead who came through the GHL era and closed later
            # therefore matched nothing: the live tab could not see them, and the historic
            # tab is FROZEN, so it can never see a deal that closed after the freeze.
            # Macmoor's $55k committed deal fell straight down that gap and appeared
            # nowhere. Revenue arrives long after the lead does, so the lead list has to
            # span both eras even though the traffic numbers do not.
            vsl_lead_emails = {(x["email"] or "").lower() for x in sub_rows
                               if not x["test"] and x.get("email")}
            try:
                _hist = json.loads(hist_path_for_leads.read_text()) if hist_path_for_leads.exists() else {}
                for x in (_hist.get("submissions") or []):
                    if not x.get("test") and x.get("email"):
                        vsl_lead_emails.add(x["email"].lower())
            except Exception as e:
                print(f"WARNING: could not read frozen leads for attribution: {e}")

            # Leads known to be from this funnel but with no submission on record. VSL
            # submission history starts 28 Jul, when the qualifier form went live, so
            # anyone who came through before that cannot be matched — Macmoor's Ellio is
            # one, and his deal was invisible in every tab as a result. An explicit list,
            # because it asserts revenue is ad-attributed on a human's say-so rather than
            # on data, and that claim should be visible in config rather than inferred.
            for e in (VSL_LEAD_EXTRA or []):
                vsl_lead_emails.add(str(e).lower())

            # Second emails for a lead we already know. One person can close more than one
            # deal under different addresses — Nykelle produced both AltGrowth (Dash) and
            # Path. Wellness — and matching on email alone credits only the first.
            # Deliberately an explicit map rather than fuzzy name matching: this decides
            # which revenue is ad-attributed, and a wrong guess overstates ROAS.
            # Shape: {"second@example.com": "known-vsl-lead@example.com"}
            for alias, known in (VSL_LEAD_ALIASES or {}).items():
                if known.lower() in vsl_lead_emails:
                    vsl_lead_emails.add(alias.lower())
            def vsl_contact(o):
                """The external VSL lead on a deal, or None. Internal reps are on
                every deal, so they must never be what makes a deal match."""
                for e in o["emails"]:
                    if not e.endswith("hirecharm.com") and e in vsl_lead_emails:
                        return e
                return None

            # Day AI's Amount on a closed deal is NOT the contract. It carries an
            # annualised ceiling: AltGrowth (Dash) reads $76,900 against a real value of
            # $7,000 — $1,500/mo on a four-month commit plus $1,000 onboarding. Overstated
            # by $51,900 on one deal, which is the difference between a 4.9x ROAS and a
            # fictional 8.8x. Same failure the COMMITTED_TERMS override already exists for.
            #
            # CLOSED_TERMS is keyed by the deal's VSL lead email and wins over Amount.
            # Shape: {"lead@example.com": {"monthly": 4500, "months": 4, "onboarding": 0}}
            # Drop an entry once Day AI's Amount is corrected at source.
            deals_closed, cash_collected, closed_detail = 0, 0.0, []
            for o in day.opps_in_stage(CLOSED_WON_STAGE_ID):
                lead = vsl_contact(o)
                if not lead:
                    continue
                deals_closed += 1
                terms = CLOSED_TERMS.get(o.get("title")) or CLOSED_TERMS.get(lead)
                if terms:
                    value = terms["monthly"] * terms.get("months", 1) + terms.get("onboarding", 0)
                    source = "confirmed terms"
                else:
                    value = o.get("amount") or 0
                    source = "Day AI Amount (unverified)"
                cash_collected += value
                closed_detail.append({"title": o.get("title"), "email": lead,
                                      "value": value, "source": source,
                                      "monthly": (terms or {}).get("monthly"),
                                      "months": (terms or {}).get("months")})
            print(f"Day AI VSL-attributed closed deals: {deals_closed} · cash ${cash_collected:.0f}")

            # Committed = verbal yes + contract out, payment NOT collected.
            # Counted and shown, but deliberately kept out of cash/ROAS.
            deals_committed, committed_value, committed_first_invoice = 0, 0.0, 0.0
            for o in day.opps_in_stage(COMMITTED_STAGE_ID):
                lead = vsl_contact(o)
                if lead:
                    deals_committed += 1
                    terms = COMMITTED_TERMS.get(lead)
                    if terms:
                        monthly, setup = terms["monthly"], terms.get("setup", 0)
                        # `months` because these are fixed-term commitments, not annual
                        # subscriptions. Macmoor is 4,500/mo on a FOUR month commit —
                        # 18,000, not the 54,000 a 12-month assumption produces. Defaults
                        # to 12 so any existing entry keeps its previous meaning.
                        months = terms.get("months", 12)
                        year_one, first_invoice = monthly * months + setup, monthly + setup
                    else:
                        # No confirmed terms — fall back to the CRM Amount as year-one
                        # value, and leave first-invoice unknown rather than guessing.
                        monthly = setup = first_invoice = None
                        year_one = o.get("amount") or 0
                    committed_value += year_one
                    committed_first_invoice += first_invoice or 0
                    bk = next((e for e in real_booked
                               if (e.get("_email") or "").lower() == lead), None)
                    committed_detail.append({
                        "title": o.get("title"),
                        "email": lead,
                        "name": (contact_cache.get(bk.get("contactId"), {}).get("name")
                                 if bk else None),
                        "monthly": monthly,
                        "setup": setup,
                        "first_invoice": first_invoice,
                        "year_one": year_one,
                        "crm_amount": o.get("amount"),
                        "ad": bk.get("_utm_content") if bk else None,
                        "ad_set": bk.get("_utm_term") if bk else None,
                    })
            print(f"Day AI VSL-attributed COMMITTED deals: {deals_committed} · "
                  f"year-one ${committed_value or 0:,.0f} · first invoice "
                  f"${committed_first_invoice or 0:,.0f} (not cash until paid)")
    except Exception as ex:
        print(f"Day AI held-call pull skipped ({ex})")

    # apply post-call qualification (manual) — a judgment implies the call was HELD.
    # Kept SEPARATE from the form gate: this is fit-after-the-call, not who-reached-the-calendar.
    def _normalize(pc):
        """Accept the two-axis shape {outcome, fit} and the legacy shapes
        ({qualified:bool} / {no_show:true}) so old env values still parse."""
        if not pc:
            return None
        if "outcome" in pc or "fit" in pc:
            return {"outcome": pc.get("outcome") or "auto", "fit": pc.get("fit")}
        if pc.get("no_show"):
            return {"outcome": "no_show", "fit": None}
        if "qualified" in pc:
            return {"outcome": "held", "fit": "qualified" if pc["qualified"] else "unqualified"}
        return {"outcome": "auto", "fit": None}

    for e in real_booked:
        pc = _normalize(post_call_leads.get((e.get("_email") or "").lower()))
        e["_pc"] = pc
        if pc:
            oc, fit = pc["outcome"], pc["fit"]
            if oc == "no_show":
                e["_no_show"] = True
                e["_held"] = False        # overrides Day AI transcript detection
            elif oc == "cancelled":
                e["_cancelled"] = True
                e["_held"] = False
            elif oc == "rescheduled":
                e["_rescheduled"] = True
                e["_held"] = False        # moved to a new time — didn't happen (yet)
            elif oc == "held":
                e["_held"] = True
            # oc == "auto" → keep Day AI's _held as-is
            # a fit verdict implies the call happened (unless explicitly not-held)
            if fit in ("qualified", "unqualified") and oc not in ("no_show", "cancelled", "rescheduled"):
                e["_held"] = True
            e["_fit"] = fit
    meetings_held = sum(1 for e in real_booked if e.get("_held"))
    post_call = {
        "held": meetings_held,
        "qualified": sum(1 for e in real_booked if e.get("_fit") == "qualified" and e.get("_held")),
        "unqualified": sum(1 for e in real_booked if e.get("_fit") == "unqualified" and e.get("_held")),
        "no_show": sum(1 for e in real_booked if e.get("_no_show")),
        "cancelled": sum(1 for e in real_booked if e.get("_cancelled")),
        "rescheduled": sum(1 for e in real_booked if e.get("_rescheduled")),
        "pending": sum(1 for e in real_booked if e.get("_held") and not e.get("_fit")),
    }
    meetings_qualified = post_call["qualified"]

    # ---- post-ad funnel: inter-stage conversion rates (guarded) ----
    def rate(n, d, need):
        if d in (0, None):
            return {"status": "insufficient", "text": f"awaiting real {need}"}
        return {"status": "ok", "value": round(100 * n / d, 1)}

    rates = {
        "play_rate":        {**rate(s["plays"], s["visitors"], "visitors"), "label": "Play rate", "of": "visitor → play"},
        "watch_through":    {**rate(watched_50, s["plays"], "plays"), "label": "Watch-through ≥50%", "of": "play → watched half"},
        "application_rate": {**rate(len(real_subs), s["visitors"], "visitors"), "label": "Application rate", "of": "visitor → form fill"},
        "booking_rate":     {**rate(len(real_booked), len(real_subs), "form fills"), "label": "Booking rate", "of": "form fill → booked call"},
        # Denominator is calls WITH A VERDICT, not all booked calls. Dividing by every
        # booking would count "nobody has written it down yet" as a no-show.
        "show_rate": {**(rate(showed, showed + no_show, "calls with a verdict")
                         if (showed + no_show) else
                         {"status": "insufficient",
                          "text": (f"0 of {past_calls} past call(s) have a Day AI verdict"
                                   if dayai_conn else "Day AI not connected")}),
                      "label": "Show-up rate", "of": "showed ÷ (showed + no-show)"},
    }

    # ---- post-ad funnel coverage map (every stage below the ad) ----
    landing_status = "live" if (rb2b_ok and len(rb2b_visitors)) else "available"
    coverage = [
        {"stage": "Landing page visits", "source": "RB2B", "status": landing_status,
         "detail": "RB2B identifies the people and companies landing on the VSL page — your landing-page-views source. "
                   + ("Live." if landing_status == "live" else "Receiver is deployed and connected; visitors populate once the Clay HTTP API column is firing.")},
        {"stage": "VSL video engagement", "source": "Wistia", "status": "live",
         "detail": "Plays, play rate, avg % watched, and the drop-off curve — all live."},
        # These two moved to iClosed at the cutover and the labels did not follow, so the
        # page kept naming GHL as a live source days after it stopped receiving anything.
        # A stale source label is worse than a missing one: it tells a reader the number
        # came from somewhere it did not, and there is nothing on the page to contradict it.
        {"stage": "Form fill", "source": "iClosed contacts", "status": "live",
         "detail": "Charm GTM event, live via the iClosed API. GHL stopped receiving form "
                   "fills at the 26 Aug cutover — pre-cutover fills are on the historic tab."},
        {"stage": "Booking", "source": "iClosed eventCalls", "status": "live",
         "detail": "Charm GTM bookings, live via the iClosed API, with UTMs read off the "
                   "call itself. The GHL calendar is no longer written to."},
        {"stage": "Show / call held", "source": "Day AI (meeting recordings)", "status": "live" if dayai_conn else "available",
         "detail": ("Connected. Matched on iClosed's calendar title (\"<First> with Charm @ <date>\") because Day AI exposes the prospect only as an object id, so their email never appears in the attendee list. Cancelled calls are excluded. This confirms the meeting happened and was not cancelled — not that the prospect turned up."
                    if dayai_conn else "A Day AI meeting recording with the lead as attendee = the call was held. Connection set up; add DAYAI_* creds to .env to activate.")},
        {"stage": "Qualified (post-call)", "source": "Chris (manual verdict)", "status": "live",
         "detail": "Chris's fit judgment after each held call — separate from the automatic form gate."},
        {"stage": "Committed (verbal yes)", "source": "Day AI (Committed stage)", "status": "live" if dayai_conn else "needs",
         "detail": (f"{deals_committed if deals_committed is not None else '—'} VSL-attributed deal(s) committed · "
                    f"${committed_value or 0:,.0f} year-one contracted, ${committed_first_invoice or 0:,.0f} first invoice. "
                    f"Committed = the prospect said yes and the contract is out for signature. Deliberately NOT counted as "
                    f"cash or in ROAS — those stay wired to Closed Won, which requires a signed MSA and first payment. "
                    f"Values come from confirmed contract terms (COMMITTED_TERMS), not Day AI's Amount field, which holds "
                    f"the un-corrected ceiling.") if dayai_conn else "Day AI connection unavailable this build."},
        {"stage": "Deals closed · Cash · ROAS", "source": "Day AI (Closed Won + Amount)", "status": "live" if dayai_conn else "needs",
         "detail": (f"Wired to Day AI Closed Won opps, matched to real VSL leads (external contact, internal "
                    f"reps excluded). {deals_closed if deals_closed is not None else '—'} closed / "
                    f"${cash_collected:,.0f} collected — a deal only lands here once the MSA is signed AND "
                    f"first payment clears."
                    if (dayai_conn and cash_collected is not None) else
                    "Day AI answered but the Closed Won pull failed this build (usually a 502) — deals and cash "
                    "are unavailable, not zero." if dayai_conn else
                    "Day AI connection unavailable this build.")},
    ]

    data = {
        "generated_at": now.strftime("%b %d, %Y %H:%M UTC"),
        "dayai_pulled_at": dayai["pulled_at"],
        "video": {"name": media["name"], "id": VSL_MEDIA_ID, "duration": duration},
        "rates": rates,
        "coverage": coverage,
        "funnel": [
            {"stage": "Ad clicks", "value": None, "note": "Meta Ads pending — Ads MCP not yet enabled on the ad account"},
            {"stage": "VSL page loads", "value": s["pageLoads"], "note": "Wistia embed loads (incl. reloads)"},
            {"stage": "Unique visitors", "value": s["visitors"], "note": "Wistia unique viewers of the page"},
            {"stage": "Video plays", "value": s["plays"], "note": f"{s['percentOfVisitorsClickingPlay']}% of visitors pressed play"},
            {"stage": "Watched ≥50%", "value": watched_50, "note": "From the engagement curve"},
            {"stage": "Form submissions", "value": len(sub_rows), "note": f"{len(real_subs)} real · {len(sub_rows) - len(real_subs)} internal tests"},
            {"stage": "Meetings booked", "value": len(booked), "note": f"{len(real_booked)} real · {len(booked) - len(real_booked)} internal tests"},
            {"stage": "Day AI contacts", "value": len(dayai_gtm), "note": "GTM form contacts synced via bridge"},
        ],
        "avg_percent_watched": s["averagePercentWatched"],
        "engagement_curve": [round(v, 1) for v in curve],
        "daily": daily,
        "submissions": real_subs,  # real submissions only (tests hidden)
        "real_meetings": len(real_booked),
        "appointments": [{"start": e.get("startTime"), "title": e.get("title"),
                          "status": e.get("appointmentStatus"), "email": e.get("_email"),
                          "held": e.get("_held", False), "test": False}
                         for e in real_booked],  # real bookings only
        "dayai_contacts": dayai["contacts"],
        "rb2b": {
            "connected": rb2b_ok,
            "count": len(rb2b_visitors),
            "no_form": len(rb2b_no_form),
            "visitors": rb2b_visitors,
        },
        "real_form_fills": len(real_subs),
        "real_bookings": len(real_booked),
        "qualification": qualification,
        "qualifier_impact": qualifier_impact,
        "lead_quality": lead_quality,
        "bookings_attribution": {"total_real": len(real_booked), "ad_attributed": ad_attributed,
                                 "by_ad": bookings_by_ad, "by_adset": bookings_by_adset,
                                 "retarget_booked": retarget_booked, "prospect_booked": prospect_booked},
        "real_bookings_detail": [{
            "name": contact_cache.get(e.get("contactId"), {}).get("name") or e.get("title"),
            "email": e.get("_email"), "start": e.get("startTime"),
            "ad": e.get("_utm_content"), "ad_set": e.get("_utm_term"),
            "retarget": is_retarget(e.get("_utm_term")),
            "utm_source": e.get("_utm_source"),
            "held": e.get("_held", False),
            # showed | no_show | cancelled | awaiting | upcoming — drives the grouped
            # dropdown under the show-up metric.
            "attendance": e.get("_attendance"),
            "transcript": e.get("_transcript"),
            "past": e.get("_past", False),
            "no_show": e.get("_no_show", False),
            "cancelled": e.get("_cancelled", False),
            "rescheduled": e.get("_rescheduled", False),
            "outcome": (e.get("_pc") or {}).get("outcome", "auto"),   # manual setting → dropdown state
            "fit": e.get("_fit"),                                     # qualified|unqualified|None → dropdown state
            "post_call": (True if e.get("_fit") == "qualified" else False if e.get("_fit") == "unqualified" else None) if e.get("_held") else None,
            "form_qual": next((x["qual"] for x in sub_rows
                               if (x.get("email") or "").lower() == (e.get("_email") or "").lower() and x.get("qual")), None),
            "pre_gate": ((e.get("startTime") or "")[:10] < QUALIFIER_FORM_DATE)} for e in real_booked],
        "meetings_held": meetings_held,
        "closed_detail": closed_detail,
        # ---- show-up rate, from the Day AI `Discovery Attended` contact property ----
        #
        # ONE clean headline with an honest denominator: showed / (showed + no-show).
        # Cancelled is excluded from BOTH halves — a call the prospect called off in
        # advance is not a no-show, and burying it in the denominator would understate
        # the rate for something that is a different problem with a different fix.
        #
        # `coverage` is the number that keeps this honest. Only calls with a recorded
        # verdict count, so a rate computed on 2 of 20 past calls is a rate on 2 calls,
        # and the page says so rather than implying it describes the funnel.
        "attendance": {
            "showed": showed, "no_show": no_show, "cancelled": cancelled,
            "awaiting": awaiting, "past_calls": past_calls,
            "with_verdict": showed + no_show + cancelled,
            "show_rate": (round(100 * showed / (showed + no_show), 1)
                          if (showed + no_show) else None),
            "coverage_pct": (round(100 * (showed + no_show + cancelled) / past_calls, 1)
                             if past_calls else None),
            "source": "Day AI · Discovery Attended",
            # The transcript signal is kept as a SECONDARY read for calls with no verdict
            # yet, clearly separate so a fallback is never mistaken for somebody's verdict.
            "transcript": {"attended": attended, "no_transcript": no_transcript},
        },
        "meetings_qualified": meetings_qualified,
        "deals_closed": deals_closed,
        "cash_collected": cash_collected,
        "deals_committed": deals_committed,
        "committed_value": committed_value,                  # year-one contracted
        "committed_first_invoice": committed_first_invoice,  # what should land first
        "committed_detail": committed_detail,
        "post_call": post_call,
        "qualifier_form_date": QUALIFIER_FORM_DATE,
        "wistia": {"page_loads": s["pageLoads"], "visitors": s["visitors"],
                   "plays": s["plays"], "watched_50": watched_50},
        # Same builder as CS, so the two funnels are comparable by construction.
        "engagement": engagement_block(s, by_date, GTM),
    }

    # ---- every offer, through one code path ----
    # Each is computed independently and wrapped on its own, because a failure in a new
    # offer must never take the dashboard down with it: GTM pays the bills. GTM's deep
    # blocks (attendance, cash, RB2B) stay on the top-level payload above; what lands
    # here is the spine every offer shares, plus the dated rows the timeframe control
    # re-aggregates in the browser.
    data["offers"] = {}
    for off in offers.OFFERS:
        try:
            data["offers"][off.key] = offer_funnel(off, now)
            f = data["offers"][off.key]
            print(f"  {off.key:11s} {f['form_fills']:3d} fill(s) · {f['booked']:3d} booking(s) · "
                  f"${f['ad']['spend']:,.2f} spend"
                  + ("" if f["wiring"]["ads"] else "  [no ad sets wired]"))
        except Exception as e:
            data["offers"][off.key] = {"key": off.key, "name": off.name, "tag": off.tag,
                                       "color": off.color, "error": str(e)}
            print(f"  {off.key} FAILED (other offers unaffected): {e}")

    # Back-compat alias. build_unified.py and the historic snapshot still read data["cs"].
    data["cs"] = data["offers"].get("cs", {})

    # Which ad set feeds which offer, and whether that was decided by an explicit prefix
    # or by GTM's catch-all. On the page so a mis-assigned set is visible rather than
    # buried inside a spend total.
    data["adset_assignment"] = adset_assignment()

    # An event created in iClosed but not declared in offers.py is invisible to this
    # dashboard — its bookings are filtered out by id and nothing says they exist. Check
    # every build and put it on the page.
    try:
        data["unregistered_events"] = offers.unregistered(iclosed_source.events())
        if data["unregistered_events"]:
            names = ", ".join(f"{e['name']} ({e['id']})" for e in data["unregistered_events"])
            print(f"WARNING: iClosed events with no offer declared: {names}")
    except Exception as e:
        data["unregistered_events"] = []
        print(f"  could not list iClosed events: {e}")

    # The closed GHL + long-VSL era, read from disk and never recomputed. It rides along
    # in the same payload so the historic tab is a tab, not a second page to keep in sync.
    # Candidate locations, in order. data/frozen is the natural home and works locally,
    # but on the deploy host the frozen era did NOT arrive under data/ while a new file
    # added to scripts/ in the SAME commit did — so something is mounted over data/ at
    # runtime, which is also why data/ghl and friends survive redeploys. Shipping a copy
    # beside the scripts sidesteps that entirely; it is a static, committed artefact, so
    # having it in two places costs nothing and the tab stops depending on mount layout.
    hist_path = next(
        (p for p in (ROOT / "data/frozen/historic_era.json",
                     ROOT / "scripts/historic_era.json") if p.exists()),
        ROOT / "data/frozen/historic_era.json")
    if hist_path.exists():
        data["historic"] = json.loads(hist_path.read_text())
        era = data["historic"].get("_era", {})
        print(f"Historic era attached (frozen {era.get('frozen_at')})")
    else:
        data["historic"] = None
        print(f"WARNING: no frozen historic era at {hist_path} — run scripts/freeze_historic.py")

    data["era"] = {
        "name": "iClosed + short VSL",
        "source": "iClosed",
        "offers": {o.key: {"media_id": (o.wistia or {}).get("media_id"),
                           "started": o.live_from} for o in offers.OFFERS},
        # Kept for the historic tab, which was frozen when only these two existed.
        "gtm_media_id": GTM.wistia.get("media_id"),
        "cs_media_id": offers.BY_KEY["cs"].wistia.get("media_id"),
        "gtm_started": GTM.live_from,
        "cs_started": offers.BY_KEY["cs"].live_from,
    }

    html = build_html(data)
    out = ROOT / "dashboard/vsl_dashboard.html"
    out.write_text(html)
    (ROOT / "data/_vsl.json").write_text(json.dumps(data))  # for the unified page
    print(f"Wrote {out}")


def build_html(data):
    payload = json.dumps(data)
    template = (ROOT / "scripts/template.html").read_text()
    return template.replace("/*__DATA__*/null", payload)


if __name__ == "__main__":
    sys.exit(main())
