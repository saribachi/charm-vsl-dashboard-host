#!/usr/bin/env python3
"""Build the Charm VSL funnel dashboard.

Pulls live data from Wistia + GHL, reads the Day AI snapshot, writes
dashboard/vsl_dashboard.html. Run: python3 scripts/build_dashboard.py
"""
import json
import os
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
        e["_test"] = is_test(e.get("_email"), info.get("name"))
    real_booked = [e for e in booked if not e["_test"]]

    # person-level attribution: bookings grouped by ad (utm_content = ad name).
    # UTMs were configured Jul 24 2026 — bookings before that read "Direct traffic".
    bookings_by_ad = {}
    for e in real_booked:
        ad = e.get("_utm_content") or "(unattributed / direct)"
        bookings_by_ad[ad] = bookings_by_ad.get(ad, 0) + 1
    ad_attributed = sum(v for k, v in bookings_by_ad.items() if k != "(unattributed / direct)")
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

    # ---- follow-up / speed-to-lead (GHL conversations, live) ----
    def parse_ts(iso):
        if not iso:
            return None
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    print("Pulling GHL conversations for follow-up…")
    followup, msg_cache = [], {}
    for sub in subs:
        cid = sub.get("contactId")
        form_ts = parse_ts(sub.get("createdAt"))
        if cid and cid not in msg_cache:
            msgs = []
            try:
                for c in ghl(f"conversations/search?contactId={cid}").get("conversations", []):
                    mm = ghl(f"conversations/{c['id']}/messages").get("messages", {})
                    arr = mm.get("messages", mm) if isinstance(mm, dict) else mm
                    if isinstance(arr, list):
                        msgs += arr
            except Exception:
                pass
            msg_cache[cid] = msgs
        msgs = msg_cache.get(cid, [])
        # outbound comms (exclude TYPE_ACTIVITY_* records) that happened at/after the form fill
        outs = []
        for m in msgs:
            t = (m.get("messageType") or m.get("type") or "")
            mt = parse_ts(m.get("dateAdded"))
            if m.get("direction") == "outbound" and not t.startswith("TYPE_ACTIVITY") and mt and (form_ts is None or mt >= form_ts):
                outs.append(m)
        outs.sort(key=lambda m: m.get("dateAdded", ""))
        first = outs[0] if outs else None
        stl = round((parse_ts(first["dateAdded"]) - form_ts) / 60.0, 1) if (first and form_ts) else None
        followup.append({"name": sub.get("name"), "email": sub.get("email"),
                         "test": is_test(sub.get("email"), sub.get("name")), "form_at": sub.get("createdAt"),
                         "first_touch_at": first.get("dateAdded") if first else None,
                         "speed_min": stl, "touches": len(outs), "contacted": bool(first)})

    real_fu = [f for f in followup if not f["test"]]
    contacted = [f for f in real_fu if f["contacted"]]
    speeds = [f["speed_min"] for f in contacted if f["speed_min"] is not None]
    fu_summary = {
        "real_leads": len(real_fu),
        "contacted": len(contacted),
        "never_contacted": len(real_fu) - len(contacted),
        "median_speed_min": round(sorted(speeds)[len(speeds) // 2], 1) if speeds else None,
        "within_5min": sum(1 for s in speeds if s <= 5),
        "within_1hr": sum(1 for s in speeds if s <= 60),
    }

    # ---- held-call status from Day AI (show-rate), guarded to real booked leads ----
    # A call is "held" only when the booked lead's EMAIL is an attendee on a Day AI
    # meeting recording. Email-only (deterministic) — no name/title matching, which
    # produced false positives (e.g. "mark" matching "Go-to-Market").
    dayai_conn, meetings_held = False, None
    try:
        import dayai as _dayai
        if _dayai.available():
            dayai_conn = True
            day = _dayai.DayAI()
            meetings = day.recent_meetings("2026-06-01T00:00:00Z")

            def is_held(email):
                email = (email or "").lower()
                return bool(email) and any(email in mt["attendees"] for mt in meetings)

            for e in real_booked:
                e["_held"] = is_held(e.get("_email"))
            meetings_held = sum(1 for e in real_booked if e.get("_held"))
            held_names = [e.get("_email") for e in real_booked if e.get("_held")]
            print(f"Day AI connected — {meetings_held} of {len(real_booked)} real booked lead(s) have a held call (by email): {held_names}")
    except Exception as ex:
        print(f"Day AI held-call pull skipped ({ex})")

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
        {"stage": "Follow-up / speed-to-lead", "source": "GHL conversations", "status": "live",
         "detail": "Time-to-first-touch and outbound touch count per lead, computed live from GHL conversations."},
        {"stage": "Show / call held", "source": "Day AI (meeting recordings)", "status": "live" if dayai_conn else "available",
         "detail": ("Connected — a Day AI meeting recording with the lead as attendee = the call was held. Show-rate computes automatically once real leads book."
                    if dayai_conn else "A Day AI meeting recording with the lead as attendee = the call was held. Connection set up; add DAYAI_* creds to .env to activate.")},
        {"stage": "Qualified · Close · Cash", "source": "Day AI / CRM", "status": "needs",
         "detail": "The CRM join is not wired (this is the ad funnel's Tier 2). Blocks qualified-rate, close-rate, CAC, ROAS."},
    ]

    data = {
        "generated_at": now.strftime("%b %d, %Y %H:%M UTC"),
        "dayai_pulled_at": dayai["pulled_at"],
        "video": {"name": media["name"], "id": VSL_MEDIA_ID, "duration": duration},
        "rates": rates,
        "coverage": coverage,
        "followup": {"summary": fu_summary, "leads": followup},
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
        "submissions": sub_rows,
        "real_meetings": len(real_booked),
        "appointments": [{"start": e.get("startTime"), "title": e.get("title"),
                          "status": e.get("appointmentStatus"),
                          "email": e.get("_email"), "test": e.get("_test", False)} for e in events],
        "dayai_contacts": dayai["contacts"],
        "rb2b": {
            "connected": rb2b_ok,
            "count": len(rb2b_visitors),
            "no_form": len(rb2b_no_form),
            "visitors": rb2b_visitors,
        },
        "real_form_fills": len(real_subs),
        "real_bookings": len(real_booked),
        "bookings_attribution": {"total_real": len(real_booked), "ad_attributed": ad_attributed,
                                 "by_ad": bookings_by_ad},
        "real_bookings_detail": [{
            "name": contact_cache.get(e.get("contactId"), {}).get("name") or e.get("title"),
            "email": e.get("_email"), "start": e.get("startTime"),
            "ad": e.get("_utm_content"), "utm_source": e.get("_utm_source"),
            "held": e.get("_held", False)} for e in real_booked],
        "meetings_held": meetings_held,
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
