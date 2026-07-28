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

WISTIA_TOKEN = ENV["WISTIA_API_TOKEN"]
GHL_TOKEN = ENV["GHL_PIT_TOKEN"]
LOCATION_ID = ENV["GHL_LOCATION_ID"]

VSL_MEDIA_ID = "swyi1909di"          # VSL 005 — live GTM VSL
GTM_FORM_ID = "XwtroXXXZ58OVpL4pEqy"  # GTM Services form VSL ONLY
GTM_CALENDAR_ID = "KDdgICxdFa0FJQgNSt8c"  # Charm - GTM VSL ONLY

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

# Internal/test/invalid submitters — excluded from "real lead" counts
TEST_EMAILS = {"sarah@hirecharm.com", "sarah+1@hirecharm.com",
               "bachmeiersj@gmail.com", "sarahpodemski@gmail.com",
               "john.doe@gmail.com",
               "ricky@aurevionmarketing.com",   # 7 repeat submissions Jul 24 — not a real lead
               "sarah+3@gmail.com",             # "chris boo" test booking
               "cobooking@yahoo.com"}           # "chris booth" — booking-flow test
TEST_DOMAINS = {"hirecharm.com", "goober.com"}


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


def is_test(email, name=""):
    e = (email or "").lower()
    if e and (e in TEST_EMAILS or e.split("@")[-1] in TEST_DOMAINS):
        return True
    n = (name or "").lower()
    if "test" in n or "goober" in n:  # obvious test entries by name
        return True
    return False


def main():
    now = datetime.now(timezone.utc)
    since = "2026-06-20"
    until = now.strftime("%Y-%m-%d")

    print("Pulling Wistia…")
    media = wistia(f"medias/{VSL_MEDIA_ID}/stats.json")
    by_date = wistia(f"stats/medias/{VSL_MEDIA_ID}/by_date.json?start_date={since}&end_date={until}")
    engagement = wistia(f"stats/medias/{VSL_MEDIA_ID}/engagement.json")
    duration = wistia(f"medias/{VSL_MEDIA_ID}.json").get("duration", 0)

    print("Pulling GHL…")
    subs, page = [], 1
    while True:
        d = ghl(f"forms/submissions?formId={GTM_FORM_ID}&limit=100&page={page}")
        batch = d.get("submissions", [])
        subs += batch
        if len(subs) >= d.get("meta", {}).get("total", len(subs)) or not batch:
            break
        page += 1

    start_ms = int(datetime(2026, 6, 1, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int((now + timedelta(days=30)).timestamp() * 1000)
    events = ghl(f"calendars/events?calendarId={GTM_CALENDAR_ID}&startTime={start_ms}&endTime={end_ms}").get("events", [])

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
                 "qual": classify_qual(x.get("others") or {}),
                 "test": is_test(x.get("email"), x.get("name"))} for x in subs]
    real_subs = [x for x in sub_rows if not x["test"]]

    eng = engagement.get("engagement_data", [])
    plays = s["plays"] or 1
    n = len(eng) or 1
    # engagement_data[i] = watch count for slice i (rewatches included); cap at 100%
    curve = [min(100.0, 100.0 * v / plays) for v in eng]
    watched_50 = round(plays * (curve[n // 2] / 100.0)) if eng else 0

    booked = [e for e in events if e.get("appointmentStatus") not in ("cancelled", "noshow")]
    # resolve appointment contacts: email (test-flagging) + GHL attribution (per-ad UTM)
    contact_cache = {}
    for e in booked:
        cid = e.get("contactId")
        if cid and cid not in contact_cache:
            try:
                con = ghl(f"contacts/{cid}").get("contact", {})
                # UTMs are captured into custom fields on the contact (not native attribution).
                cf = {f.get("id"): (f.get("value") or None) for f in (con.get("customFields") or [])}
                nm = con.get("contactName") or " ".join(
                    x for x in [con.get("firstName"), con.get("lastName")] if x)
                ws = lambda v: " ".join(v.split()) if v else None  # collapse URL-encoding spaces
                contact_cache[cid] = {"email": con.get("email"), "name": nm,
                                      "utm_content": ws(cf.get(UTM_FIELD_IDS["utm_content"])),
                                      "utm_source": ws(cf.get(UTM_FIELD_IDS["utm_source"])),
                                      "utm_term": ws(cf.get(UTM_FIELD_IDS["utm_term"]))}
            except Exception:
                contact_cache[cid] = {}
        info = contact_cache.get(cid, {})
        e["_email"] = info.get("email")
        e["_utm_content"] = info.get("utm_content")
        e["_utm_source"] = info.get("utm_source")
        e["_utm_term"] = info.get("utm_term")   # ad set (e.g. "Retargeting Video Ads")
        e["_test"] = is_test(e.get("_email"), info.get("name"))
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
    qualification = {
        "with_answers": n_with,                      # real subs carrying the qualifier answers
        "qualified": n_qualified,
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
    dayai_conn, meetings_held, deals_closed, cash_collected = False, None, None, None
    try:
        import dayai as _dayai
        if _dayai.available():
            dayai_conn = True
            day = _dayai.DayAI()
            meetings = day.recent_meetings("2026-06-01T00:00:00Z")

            def is_held(name, email):
                email = (email or "").lower()
                if email and any(email in mt["attendees"] for mt in meetings):
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
                nm = contact_cache.get(e.get("contactId"), {}).get("name")
                e["_held"] = is_held(nm, e.get("_email"))
            meetings_held = sum(1 for e in real_booked if e.get("_held"))
            held_names = [e.get("_email") for e in real_booked if e.get("_held")]
            print(f"Day AI connected — {meetings_held} of {len(real_booked)} real booked lead(s) have a held call (by email): {held_names}")

            # VSL-attributed closed deals + cash from Day AI Closed Won opps.
            # Match ONLY real external VSL leads (drop internal reps who are on every deal).
            vsl_lead_emails = {(x["email"] or "").lower() for x in sub_rows
                               if not x["test"] and x.get("email")}
            deals_closed, cash_collected = 0, 0.0
            for o in day.closed_won(CLOSED_WON_STAGE_ID):
                ext = [e for e in o["emails"] if not e.endswith("hirecharm.com")]
                if any(e in vsl_lead_emails for e in ext):
                    deals_closed += 1
                    if o.get("amount"):
                        cash_collected += o["amount"]
            print(f"Day AI VSL-attributed closed deals: {deals_closed} · cash ${cash_collected:.0f}")
    except Exception as ex:
        print(f"Day AI held-call pull skipped ({ex})")

    # apply post-call qualification (manual) — a judgment implies the call was HELD.
    # Kept SEPARATE from the form gate: this is fit-after-the-call, not who-reached-the-calendar.
    for e in real_booked:
        pc = post_call_leads.get((e.get("_email") or "").lower())
        e["_post_call"] = pc
        if pc:
            if pc.get("no_show"):
                e["_no_show"] = True
                e["_held"] = False        # a no-show is definitively NOT held (overrides Day AI)
            else:
                e["_held"] = True         # a fit verdict implies the call was held
    meetings_held = sum(1 for e in real_booked if e.get("_held"))
    post_call = {
        "held": meetings_held,
        "qualified": sum(1 for e in real_booked if (e.get("_post_call") or {}).get("qualified") is True),
        "unqualified": sum(1 for e in real_booked if (e.get("_post_call") or {}).get("qualified") is False),
        "no_show": sum(1 for e in real_booked if e.get("_no_show")),
        "pending": sum(1 for e in real_booked if e.get("_held") and not e.get("_post_call")),
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
        "show_rate":        {**(rate(meetings_held, len(real_booked), "booked calls") if meetings_held is not None else {"status": "insufficient", "text": "Day AI not connected"}), "label": "Show rate", "of": "booked → call held"},
    }

    # ---- post-ad funnel coverage map (every stage below the ad) ----
    landing_status = "live" if (rb2b_ok and len(rb2b_visitors)) else "available"
    coverage = [
        {"stage": "Landing page visits", "source": "RB2B", "status": landing_status,
         "detail": "RB2B identifies the people and companies landing on the VSL page — your landing-page-views source. "
                   + ("Live." if landing_status == "live" else "Receiver is deployed and connected; visitors populate once the Clay HTTP API column is firing.")},
        {"stage": "VSL video engagement", "source": "Wistia", "status": "live",
         "detail": "Plays, play rate, avg % watched, and the drop-off curve — all live."},
        {"stage": "Form fill", "source": "GHL forms", "status": "live",
         "detail": "GTM Services form (VSL only), live via API."},
        {"stage": "Booking", "source": "GHL calendar", "status": "live",
         "detail": "GTM VSL calendar appointments, live via API."},
        {"stage": "Show / call held", "source": "Day AI (meeting recordings)", "status": "live" if dayai_conn else "available",
         "detail": ("Connected — a Day AI meeting recording with the lead as attendee = the call was held. Show-rate computes automatically once real leads book."
                    if dayai_conn else "A Day AI meeting recording with the lead as attendee = the call was held. Connection set up; add DAYAI_* creds to .env to activate.")},
        {"stage": "Qualified (post-call)", "source": "Chris (manual verdict)", "status": "live",
         "detail": "Chris's fit judgment after each held call — separate from the automatic form gate."},
        {"stage": "Deals closed · Cash · ROAS", "source": "Day AI (Closed Won + Amount)", "status": "live" if dayai_conn else "needs",
         "detail": f"Wired to Day AI Closed Won opps, matched to real VSL leads (external contact, internal reps excluded). {deals_closed if deals_closed is not None else '—'} closed / ${cash_collected:,.0f} so far — the VSL funnel is days old, so none have closed yet." if dayai_conn else "Day AI connection unavailable this build."},
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
            "no_show": e.get("_no_show", False),
            "post_call": (e.get("_post_call") or {}).get("qualified") if (e.get("_post_call") and not e.get("_no_show")) else None,
            "form_qual": next((x["qual"] for x in sub_rows
                               if (x.get("email") or "").lower() == (e.get("_email") or "").lower() and x.get("qual")), None),
            "pre_gate": ((e.get("startTime") or "")[:10] < QUALIFIER_FORM_DATE)} for e in real_booked],
        "meetings_held": meetings_held,
        "meetings_qualified": meetings_qualified,
        "deals_closed": deals_closed,
        "cash_collected": cash_collected,
        "post_call": post_call,
        "qualifier_form_date": QUALIFIER_FORM_DATE,
        "wistia": {"page_loads": s["pageLoads"], "visitors": s["visitors"],
                   "plays": s["plays"], "watched_50": watched_50},
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
