"""iClosed as the live funnel source, shaped like the GHL rows it replaces.

GHL stopped being a live source on 26 Aug 2026 (GTM) and 27 Aug (CS): the landers now
embed iClosed, so no GHL form is submitted and no GHL appointment is created. The GHL
history up to that point is preserved in data/frozen/ghl_history_2026-08-24.json and must
be read from there, never re-fetched — a live GHL pull now returns a funnel that simply
stops growing, which reads as a collapse rather than a cutover.

This module emits rows in the SAME shape build_dashboard.py already consumes, so the
funnel math, the attribution grouping and the qualification mix all keep working:

    submissions -> {createdAt, email, name, others{...field ids...}}
    events      -> {appointmentStatus, contactId, startTime, _email, _utm_*}

WHY THE FIELD-ID SHAPE IS PRESERVED. It would be tidier to emit clean keys, but every
downstream consumer indexes `others` by GHL custom-field id, and the frozen history is in
that shape permanently. Emitting the same shape means one code path over both eras.
"""

import json
import os
import urllib.parse
import urllib.request

import offers as offers_mod

BASE = os.environ.get("ICLOSED_API_BASE_URL", "https://public.api.iclosed.io")

# The GHL custom-field ids the dashboard indexes `others` by. Unchanged from GHL: the
# bridge writes these same fields on the GHL contact, and the frozen history uses them.
UTM_FIELD_IDS = {
    "utm_source": "XbqI6HLGdJKCL18xfqrY",
    "utm_medium": "bCwzhLnzjAG1z0n3BuDg",
    "utm_campaign": "cADGo06z5WiKVDgULcaM",
    "utm_content": "XlRkyGbQihyHZeE2Bxxk",   # ad name — the join key to Meta spend
    "utm_term": "evvs35rWaeYbb8jzQNYe",       # ad set name
}
QUAL_FIELDS = {
    "revenue": "t8kIeNWMhGLyKmelKXYL",
    "acv": "IUjCRF0gg4GKikd3DmlK",
    "capacity": "UX6TIRA7aL6rjW65oKwV",
}

# ⚠️ THE QUESTION MAP IS PER OFFER AND LIVES IN offers.py, NOT HERE.
#
# It used to be one global list, and that list was GTM's. Every offer asks its own
# screening questions, so CS Flex ("who handles your support today", "what do most of
# your tickets involve") matched nothing from the cutover onward and every CS
# disqualification landed in dq_other with its answers dropped. One map per offer means
# a new event's questions are a two-line registry entry rather than a silent blank.
#
# Identifiers are the question text slugified, so they are matched on a SUBSTRING:
# rewording a question in iClosed changes the slug, and an exact match would quietly
# stop mapping anything.


def _get(path, params=None):
    key = os.environ.get("ICLOSED_API_KEY", "")
    if not key:
        raise RuntimeError("ICLOSED_API_KEY is not set")
    url = BASE.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "charm-vsl-metrics/1.0",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


# ---- per-build cache -------------------------------------------------------------
# Five offers each used to re-page the ENTIRE contacts and eventCalls collections, and
# main() pulls GTM a second time for its deep panels — 127 HTTP calls and 142 seconds per
# build. The container's startup rebuild is synchronous, so that was ~2 minutes of Bad
# Gateway on every deploy and every restart.
#
# Nothing iClosed returns changes during a single build, so each collection is fetched
# once and each contact detail once. Cleared by reset_cache() if a caller ever needs a
# genuinely fresh read within one process.
_CACHE = {}


def reset_cache():
    _CACHE.clear()


def _rows(payload, *keys):
    """iClosed nests its array differently per endpoint, and reading the wrong key
    returns zero rows with no error. Try each shape rather than assume one."""
    if isinstance(payload, list):
        return payload
    for k in keys + ("data", "results", "items"):
        v = (payload or {}).get(k)
        if isinstance(v, list):
            return v
    inner = (payload or {}).get("data")
    if isinstance(inner, dict):
        for k in keys:
            if isinstance(inner.get(k), list):
                return inner[k]
    return []


def _paged(path, container, params=None, limit=50, max_pages=40):
    """⚠️ iClosed paging is ZERO-INDEXED. page=1 returns an empty array with HTTP 200
    and a count that still reports rows exist, so starting at 1 finds nothing forever.

    Cached per build — see _CACHE. Every offer walks the same two collections.
    """
    ck = ("paged", path, container, tuple(sorted((params or {}).items())), limit)
    if ck in _CACHE:
        return _CACHE[ck]
    out = []
    for page in range(0, max_pages):
        payload = _get(path, {**(params or {}), "page": str(page), "limit": limit})
        batch = _rows(payload, container)
        out += batch
        total = ((payload or {}).get("data") or {}).get("count")
        if isinstance(total, int) and len(out) >= total:
            break
        if len(batch) < limit:
            break
    _CACHE[ck] = out
    return out


def invitee_answers(record):
    """Flatten iClosed's answers to {identifier: answer}. Two shapes exist: contacts use
    CustomFieldAssociation, eventCalls use secondaryAnswers."""
    assoc = record.get("CustomFieldAssociation") or record.get("secondaryAnswers") or []
    out = {}
    for entry in assoc if isinstance(assoc, list) else []:
        raw = ((entry.get("customField") or {}).get("identifier")
               or entry.get("customFieldIdentifier") or entry.get("identifier"))
        if not raw:
            continue
        ident = str(raw).split(".")[-1].replace("}}", "").strip()
        answers = entry.get("CustomFieldAnswer") or entry.get("answer") or []
        vals = [a.get("answer") for a in (answers if isinstance(answers, list) else [answers])
                if isinstance(a, dict) and a.get("answer")]
        if vals:
            out[ident] = ", ".join(str(v).strip() for v in vals)
    return out


def qual_from_status(status, answers, offer):
    """The gate verdict for ONE lead. ANSWERS decide it, not iClosed's status.

    ⚠️ THESE ARE TWO DIFFERENT AXES AND CONFLATING THEM MOVES THE DENOMINATOR.
    The gate asks "should we take this call"; iClosed's status says how far the lead got.
    Reading the verdict off the status looked right because QUALIFIED and
    STRATEGY_CALL_BOOKED sit past the gate — but POTENTIAL does NOT mean "never reached
    the gate", it means "did not book". Seven GTM leads currently sit at POTENTIAL with
    every qualifier question answered. Treating them as ungraded drops them out of
    `with_answers` and lifts the qualification rate from 57.7% to 82.5% on nothing but a
    smaller denominator — precisely the kind of invisible-denominator move that put 129%
    and 660% on this dashboard for weeks.

    So: the offer's own DQ rules grade anyone who answered. iClosed's status is kept
    alongside as progression, and is consulted only where it knows something the answers
    cannot show — a DISQUALIFIED verdict reached by a rule this registry does not model,
    which is named dq_other rather than dropped.
    """
    if not offer.gate:
        return None
    verdict = offer.classify(answers)
    if verdict:
        return verdict
    s = (status or "").strip().upper().replace(" ", "_").replace("-", "_")
    if "DISQUALIFIED" in s:
        # iClosed disqualified a lead who answered nothing this offer maps. Named so a
        # new iClosed rule shows up as an unknown bucket instead of vanishing.
        return "dq_other"
    return None


def gate_leak(status, answers, offer):
    """True when a lead the gate should have stopped booked a call anyway.

    Worth its own function because it is the only place the two axes are SUPPOSED to
    disagree, and the disagreement is the finding: seven GTM leads whose answers fail the
    ACV or revenue rule currently hold STRATEGY_CALL_BOOKED. Either iClosed's configured
    rules differ from the spec modelled here, or the gate does not actually block
    booking. Either way it is a number Chris should see, not one to smooth away.
    """
    if not offer.gate:
        return False
    verdict = offer.classify(answers)
    if not (verdict or "").startswith("dq_"):
        return False
    s = (status or "").strip().upper().replace(" ", "_").replace("-", "_")
    return s == "QUALIFIED" or s.endswith("_CALL_BOOKED")


def canonical_answers(offer, raw):
    """iClosed's {identifier: answer} -> {canonical key: answer} for ONE offer."""
    out = {}
    for sub, key, _fid in offer.questions:
        val = next((v for k, v in raw.items() if sub in k.lower()), None)
        if val:
            out[key] = val
    return out


def events():
    """Every meeting event configured in iClosed, registered here or not.

    Pulled on each build so an event created in iClosed cannot stay invisible: without
    this, a new event's bookings are filtered out by id and nothing says they exist.
    """
    if ("events",) not in _CACHE:
        _CACHE[("events",)] = _rows(_get("/v1/events", {"page": "0", "limit": "100"}), "events")
    return _CACHE[("events",)]


def submissions(offer):
    """iClosed contacts for ONE offer, shaped like GHL form submissions."""
    wanted = {int(e) for e in offer.iclosed_event_ids}
    out = []
    for row in _paged("/v1/contacts", "contacts"):
        ev = {int(e.get("id")) for e in (row.get("ContactEvents") or []) if e.get("id")}
        if not (ev & wanted):
            continue
        # Answers only exist on the DETAIL endpoint — the list omits
        # CustomFieldAssociation entirely, which silently yields zero answers.
        dk = ("detail", str(row["id"]))
        if dk in _CACHE:
            detail = _CACHE[dk]
        else:
            try:
                detail = (_get("/v1/contacts/detail", {"contactId": row["id"]}) or {})
                detail = detail.get("data") or detail
            except Exception:
                detail = row
            _CACHE[dk] = detail
        raw = invitee_answers(detail)
        answers = canonical_answers(offer, raw)

        # `others` stays keyed by GHL custom-field id for GTM and CS, because the frozen
        # historic era is in that shape permanently and both eras share one code path.
        # Offers created after the cutover have no GHL history, so their registry entry
        # carries no field id and the canonical key is used directly.
        others = {offer.field_id(k): v for k, v in answers.items()}

        name = " ".join(x for x in [detail.get("firstName"), detail.get("lastName")] if x)
        out.append({
            "id": str(detail.get("id")),
            "createdAt": detail.get("createdAt"),
            "email": detail.get("email"),
            "name": name or None,
            "others": others,
            "answers": answers,               # canonical keys, for the per-offer answer mix
            "_status": detail.get("status"),
            "_qual": qual_from_status(detail.get("status"), answers, offer),
            "_gate_leak": gate_leak(detail.get("status"), answers, offer),
            "_offer": offer.key,
            "_source": "iclosed",
        })
    return out


# iClosed writes a rep's post-call verdict onto the call as a secondary answer AND as a
# task. Both are read: the task carries the structured outcome, the secondary answers
# carry the same thing where a rep filled the form instead. This is the first automated
# feed for what has been manual in data/manual/post_call.json — it does NOT replace those
# verdicts, because only 10 of 35 calls carry one and every one so far is a cancellation
# rather than a real sales outcome.
_OUTCOME_FIELDS = {"call_outcome": "_call_outcome", "no_sale_reason": "_no_sale_reason"}


def _call_outcome(call):
    out = {"_call_outcome": None, "_no_sale_reason": None,
           "_objection": None, "_rep_notes": None}
    for t in (call.get("task") or []):
        if t.get("outcome"):
            out["_call_outcome"] = t["outcome"]
        if t.get("noSaleReason"):
            out["_no_sale_reason"] = t["noSaleReason"]
        if t.get("objection"):
            out["_objection"] = t["objection"]
        if t.get("notes"):
            out["_rep_notes"] = t["notes"]
    for entry in (call.get("secondaryAnswers") or []):
        if entry.get("isSecondaryQuestion"):
            continue                       # that is a screening answer, not an outcome
        key = _OUTCOME_FIELDS.get(entry.get("customFieldIdentifier"))
        if not key or out.get(key):
            continue
        vals = [a.get("answer") for a in (entry.get("answer") or []) if a.get("answer")]
        if vals:
            out[key] = vals[0]
    return out


def appointments(offer):
    """iClosed eventCalls for ONE offer, shaped like GHL calendar events.

    UTMs come off the CALL, not the contact: /v1/contacts and /v1/contacts/detail return
    no utm at all, while eventCalls carries them as [{utmKey, utmValue}].
    """
    wanted = {int(e) for e in offer.iclosed_event_ids}
    out = []
    for c in _paged("/v1/eventCalls", "eventCalls"):
        if int(c.get("eventId") or 0) not in wanted:
            continue
        utm = {str(p.get("utmKey")): p.get("utmValue")
               for p in (c.get("utm") or []) if isinstance(p, dict) and p.get("utmKey")}

        # Attendance, in iClosed's own order of authority. There is NO status field on a
        # call: a cancellation still reads eventType PAST, so cancelledBy must win.
        oc = _call_outcome(c)
        outcomes = " ".join(str((t or {}).get("outcome") or "") for t in (c.get("task") or [])).upper()
        if c.get("cancelledBy") or c.get("cancelReason"):
            status = "cancelled"
        elif "NO_SHOW" in outcomes:
            status = "noshow"
        else:
            status = "confirmed"

        row = {
            "id": str(c.get("id")),
            "appointmentStatus": status,
            "contactId": str(c.get("contactId") or ""),
            "startTime": c.get("dateTimeUTC"),
            # createdAt is when the call was BOOKED; dateTimeUTC is when it RUNS. The
            # timeframe filter needs both: "booked last week" and "ran last week" are
            # different questions and a call routinely straddles the two windows.
            "createdAt": c.get("createdAt"),
            "_booked_at": c.get("createdAt"),
            "_call_at": c.get("dateTimeUTC"),
            "_event_id": c.get("eventId"),
            "_offer": offer.key,
            "_email": c.get("inviteeEmail"),
            "_name": (c.get("inviteeName") or "").strip() or None,
            "_rescheduled": bool(c.get("rescheduledBy") or c.get("rescheduleReason")),
            "_cancel_reason": c.get("cancelReason"),
            # Values arrive HALF-DECODED: iClosed resolves %2B to '+' but never converts
            # '+' back to a space, so an ad name with spaces or plus signs comes back
            # mangled and will not join to Meta spend. Fix the ad NAME, not this.
            "_utm_content": utm.get("utm_content"),
            "_utm_source": utm.get("utm_source"),
            "_utm_term": utm.get("utm_term"),
            "_fbclid": utm.get("fbclid"),
            "_source": "iclosed",
        }
        row.update(oc)
        out.append(row)
    return out


def frozen_ghl(path):
    """GHL history from before the cutover. Read, never re-fetched: the forms and
    calendars still exist in GHL and still answer, they simply stop growing, so a live
    pull silently understates nothing and overstates continuity."""
    with open(path) as f:
        return json.load(f)
