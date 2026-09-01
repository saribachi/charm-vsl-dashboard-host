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

# iClosed invitee-question identifiers -> the GHL field id the dashboard expects.
# Identifiers are the question text slugified, so they are matched on a SUBSTRING:
# rewording a question in iClosed changes the slug, and an exact match would silently
# stop mapping anything.
ANSWER_TO_FIELD = [
    ("annual-revenue", QUAL_FIELDS["revenue"]),
    ("average-contract-value", QUAL_FIELDS["acv"]),
    ("could-you-service-them", QUAL_FIELDS["capacity"]),
    ("company-website", "website"),
]


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
    and a count that still reports rows exist, so starting at 1 finds nothing forever."""
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


def qual_from_status(status, answers):
    """Map an iClosed status to the dashboard's qualification vocabulary.

    iClosed decides qualification itself, so its status is authoritative — the DQ REASON
    is re-derived, because iClosed records THAT a lead was disqualified and never WHY.
    Order is first-match-wins, identical to classify_qual(), so rates stay comparable
    across the cutover.

    ⚠️ QUALIFIED IS NOT TERMINAL: a contact who books moves QUALIFIED ->
    STRATEGY_CALL_BOOKED. Anything at or past the gate counts as qualified.
    """
    s = (status or "").strip().upper().replace(" ", "_").replace("-", "_")
    if not s:
        return None
    if "DISQUALIFIED" in s:
        get = lambda needle: next((v for k, v in answers.items() if needle in k.lower()), "")
        if get("could-you-service-them") == "No, we're at capacity":
            return "dq_capacity"
        acv = get("average-contract-value")
        if acv == "Under $5K":
            return "dq_acv_low"
        if get("annual-revenue") == "Under $1M" and acv == "$5K to $14K":
            return "dq_revenue_acv"
        # Disqualified by a rule this mapping does not know. Named rather than dropped,
        # so a new iClosed rule shows up as an unknown bucket instead of vanishing.
        return "dq_other"
    if s == "QUALIFIED" or s.endswith("_CALL_BOOKED"):
        return "qualified"
    return None  # POTENTIAL: partial fill, never reached the gate


def submissions(event_ids=None):
    """iClosed contacts, shaped like GHL form submissions."""
    wanted = {str(e) for e in (event_ids or [])}
    out = []
    for row in _paged("/v1/contacts", "contacts"):
        if wanted:
            ev = {str(e.get("id")) for e in (row.get("ContactEvents") or [])}
            if not (ev & wanted):
                continue
        # Answers only exist on the DETAIL endpoint — the list omits
        # CustomFieldAssociation entirely, which silently yields zero answers.
        try:
            detail = (_get("/v1/contacts/detail", {"contactId": row["id"]}) or {})
            detail = detail.get("data") or detail
        except Exception:
            detail = row
        answers = invitee_answers(detail)

        others = {}
        for needle, field_id in ANSWER_TO_FIELD:
            val = next((v for k, v in answers.items() if needle in k.lower()), None)
            if val:
                others[field_id] = val

        name = " ".join(x for x in [detail.get("firstName"), detail.get("lastName")] if x)
        out.append({
            "id": str(detail.get("id")),
            "createdAt": detail.get("createdAt"),
            "email": detail.get("email"),
            "name": name or None,
            "others": others,
            "_status": detail.get("status"),
            "_qual": qual_from_status(detail.get("status"), answers),
            "_source": "iclosed",
        })
    return out


def appointments(event_ids=None):
    """iClosed eventCalls, shaped like GHL calendar events.

    UTMs come off the CALL, not the contact: /v1/contacts and /v1/contacts/detail return
    no utm at all, while eventCalls carries them as [{utmKey, utmValue}].
    """
    wanted = {str(e) for e in (event_ids or [])}
    out = []
    for c in _paged("/v1/eventCalls", "eventCalls"):
        if wanted and str(c.get("eventId")) not in wanted:
            continue
        utm = {str(p.get("utmKey")): p.get("utmValue")
               for p in (c.get("utm") or []) if isinstance(p, dict) and p.get("utmKey")}

        # Attendance, in iClosed's own order of authority. There is NO status field on a
        # call: a cancellation still reads eventType PAST, so cancelledBy must win.
        outcomes = " ".join(str((t or {}).get("outcome") or "") for t in (c.get("task") or [])).upper()
        if c.get("cancelledBy") or c.get("cancelReason"):
            status = "cancelled"
        elif "NO_SHOW" in outcomes:
            status = "noshow"
        else:
            status = "confirmed"

        out.append({
            "id": str(c.get("id")),
            "appointmentStatus": status,
            "contactId": str(c.get("contactId") or ""),
            "startTime": c.get("dateTimeUTC"),
            "createdAt": c.get("createdAt"),
            "_email": c.get("inviteeEmail"),
            "_name": (c.get("inviteeName") or "").strip() or None,
            # Values arrive HALF-DECODED: iClosed resolves %2B to '+' but never converts
            # '+' back to a space, so an ad name with spaces or plus signs comes back
            # mangled and will not join to Meta spend. Fix the ad NAME, not this.
            "_utm_content": utm.get("utm_content"),
            "_utm_source": utm.get("utm_source"),
            "_utm_term": utm.get("utm_term"),
            "_fbclid": utm.get("fbclid"),
            "_source": "iclosed",
        })
    return out


def frozen_ghl(path):
    """GHL history from before the cutover. Read, never re-fetched: the forms and
    calendars still exist in GHL and still answer, they simply stop growing, so a live
    pull silently understates nothing and overstates continuity."""
    with open(path) as f:
        return json.load(f)
