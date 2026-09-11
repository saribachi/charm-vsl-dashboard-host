"""Minimal Day AI connection for the dashboard build.

OAuth refresh -> MCP-over-HTTP (search_objects). Reuses the same credentials the
GHL -> Day AI bridge uses (see ~/Projects/day-ai-sdk for the reference client).
Credentials live in the project .env as DAYAI_CLIENT_ID / DAYAI_CLIENT_SECRET /
DAYAI_REFRESH_TOKEN / DAYAI_BASE_URL.

Usage:
    from dayai import DayAI, available
    if available():
        day = DayAI()
        held = day.held_call("jane@acme.com")   # True if a call was held
"""
import json
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path


def _load_env():
    import os
    env = dict(os.environ)  # container/hosted
    p = Path(__file__).resolve().parent.parent / ".env"
    if p.exists():  # local dev overrides
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def available(env=None):
    env = env or _load_env()
    return all(env.get(k) for k in ("DAYAI_CLIENT_ID", "DAYAI_CLIENT_SECRET", "DAYAI_REFRESH_TOKEN"))


class DayAI:
    def __init__(self, env=None):
        env = env or _load_env()
        self.base = env.get("DAYAI_BASE_URL", "https://day.ai").rstrip("/")
        self.cid = env.get("DAYAI_CLIENT_ID")
        self.csec = env.get("DAYAI_CLIENT_SECRET")
        self.rtok = env.get("DAYAI_REFRESH_TOKEN")
        self._token = None
        self._initialized = False

    def _post(self, path, data, headers, form=False):
        body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def token(self):
        if self._token:
            return self._token
        d = self._post("/api/oauth",
                       {"grant_type": "refresh_token", "client_id": self.cid,
                        "client_secret": self.csec, "refresh_token": self.rtok},
                       {"Content-Type": "application/x-www-form-urlencoded"}, form=True)
        self._token = d["access_token"]
        return self._token

    def _mcp(self, method, params=None):
        h = {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"}
        return self._post("/api/mcp", {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, h)

    def search(self, queries, **options):
        if not self._initialized:
            self._mcp("initialize", {"protocolVersion": "2025-06-18",
                                     "clientInfo": {"name": "charm-vsl-metrics", "version": "1.0"},
                                     "capabilities": {}})
            self._initialized = True
        r = self._mcp("tools/call", {"name": "search_objects", "arguments": {"queries": queries, **options}})
        txt = (r.get("result", {}).get("content") or [{}])[0].get("text", "")
        return json.loads(txt) if txt else {}

    # Day AI contact property "Discovery Attended" — the workspace now writes a rep's
    # show-up verdict here, which is what replaced the manual queue on the dashboard.
    DISCOVERY_ATTENDED = "ec9f0062-5772-448f-aa78-59fbb5274f4a"
    # Its three options, by label. The API returns the LABEL (as a one-item list), not
    # the option id, so these are matched on the label and normalised here — the dashboard
    # never sees Day AI's wording.
    _ATTENDANCE = {"showed up": "showed", "no-show": "no_show", "cancelled": "cancelled"}

    def discovery_attendance(self, emails, chunk=40):
        """{email -> showed|no_show|cancelled} for the contacts that have a verdict.

        An email absent from the result has NO verdict recorded — which is NOT a no-show.
        Callers must keep those two apart: treating "nobody wrote it down" as "they did
        not turn up" is the same mistake that once made show rate read 5 of 5 on a day
        with two real no-shows, only inverted.
        """
        want = [e.lower() for e in {(x or "").lower() for x in emails} if e]
        out = {}
        for i in range(0, len(want), chunk):
            batch = want[i:i + chunk]
            try:
                res = self.search(
                    [{"objectType": "native_contact",
                      "where": {"OR": [{"propertyId": "email", "operator": "eq", "value": e}
                                       for e in batch]}}],
                    propertiesToReturn=["email", self.DISCOVERY_ATTENDED])
            except Exception as exc:
                print(f"  Day AI attendance batch failed ({len(batch)} email(s)): {exc}")
                continue
            for c in res.get("native_contact", {}).get("results", []):
                props = c.get("properties") or {}
                email = (props.get("email") or "").lower()
                raw = props.get("Discovery Attended") or props.get(self.DISCOVERY_ATTENDED)
                if isinstance(raw, list):
                    raw = raw[0] if raw else None
                key = self._ATTENDANCE.get(str(raw).strip().lower()) if raw else None
                if email and key:
                    out[email] = key
        return out

    def opps_in_stage(self, stage_id, since="2026-06-01T00:00:00Z"):
        """Opportunities in one pipeline stage, with deal Amount + contact emails.
        Caller must filter to the funnel's real (external) leads — every deal also
        lists the internal rep (chris@hirecharm.com) as a related contact.

        Emails come from BOTH the `roles` property and the contact relationships.
        A contact relationship's objectId is an email only sometimes — Day AI stores
        newer contacts as a UUID (Macmoor's Ellio is one), which silently matched
        nothing and made a real VSL deal look unattributed. `roles` carries
        personEmail reliably, so it is the primary source.
        """
        res = self.search(
            [{"objectType": "native_opportunity",
              "where": {"propertyId": "stageId", "operator": "contains", "value": stage_id}}],
            includeRelationships=True,
            propertiesToReturn=["title", "89ed34c4-c3cc-45df-b6aa-32c894dc3d51", "roles"],  # Amount
            timeframeStart=since)
        out = []
        for o in res.get("native_opportunity", {}).get("results", []):
            props = o.get("properties", {})
            emails = set()
            try:
                for r in json.loads(props.get("roles") or "[]"):
                    if r.get("personEmail"):
                        emails.add(r["personEmail"].lower())
            except (ValueError, AttributeError, TypeError):
                pass
            for r in (o.get("relationships") or []):
                oid = (r.get("objectId") or "") if isinstance(r, dict) else ""
                if r.get("objectType") == "native_contact" and "@" in oid:
                    emails.add(oid.lower())
            out.append({"title": o.get("title"),
                        "amount": props.get("Amount"),
                        "emails": sorted(emails)})
        return out

    def closed_won(self, stage_id, since="2026-06-01T00:00:00Z"):
        """Back-compat alias — Closed Won is just one stage."""
        return self.opps_in_stage(stage_id, since)

    def held_call(self, email, since="2026-01-01T00:00:00Z"):
        """True if the contact has >= 1 Day AI meeting recording (i.e. a call was held)."""
        res = self.search(
            [{"objectType": "native_meetingrecording",
              "where": {"relationship": "attendee", "targetObjectType": "native_contact",
                        "targetObjectId": email, "operator": "eq"}}],
            timeframeStart=since)
        return len(res.get("native_meetingrecording", {}).get("results", [])) > 0

    def search_all(self, queries, max_pages=12, **options):
        """Every page of a search, not just the first.

        ⚠️ Day AI paginates at the TOP LEVEL of the response — hasMore / nextOffset /
        totalRecords — NOT inside the native_<type> bucket, which only carries `results`
        and `totalCount`. Reading the bucket for pagination gives `undefined`, so a caller
        silently stops after one page and looks like it worked. One page is 40 rows
        against 95 matching records here, and the meetings that fall off the end are the
        oldest — which is exactly where a missed call would hide.
        """
        merged, offset = {}, 0
        for _ in range(max_pages):
            res = self.search(queries, offset=offset, **options)
            for key, bucket in res.items():
                if not isinstance(bucket, dict) or "results" not in bucket:
                    continue
                merged.setdefault(key, {"results": [], "totalCount": bucket.get("totalCount")})
                merged[key]["results"].extend(bucket.get("results") or [])
            if not res.get("hasMore"):
                break
            nxt = res.get("nextOffset")
            if not isinstance(nxt, int) or nxt <= offset:
                break            # no forward progress: stop rather than loop forever
            offset = nxt
        return merged

    def recent_meetings(self, since="2026-06-01T00:00:00Z"):
        """Recent meeting recordings with their title + linked contact emails (attendees).

        Attendee-email linkage lags in Day AI, so held detection also uses the title
        (e.g. 'Todd Dugas & Charm'). Returns [{title, attendees:[email,...]}].
        """
        # `topic` is Day AI's summary of what was discussed, so it only exists once there
        # is a transcript — which makes its PRESENCE the closest thing to an attendance
        # signal this API offers. The meeting object itself is created when the call is
        # booked, so its existence proves nothing: a no-show looks identical to a held
        # call until you look at the topic.
        #
        # Requested by name rather than "*": the full property set over a wide window
        # returns HTTP 502, and a partial response reads as "fewer meetings" rather than
        # as an error. Do NOT add "type" here — Day AI rejects it as a selectable name,
        # and an invalid name is silently omitted from every row, which would make the
        # value read as empty for everybody.
        res = self.search_all([{"objectType": "native_meetingrecording"}],
                              includeRelationships=True, propertiesToReturn=["topic"],
                              timeframeStart=since)
        out = []
        for m in res.get("native_meetingrecording", {}).get("results", []):
            rels = m.get("relationships") or []
            atts = [(r.get("objectId") or "").lower() for r in rels
                    if isinstance(r, dict) and r.get("objectType") == "native_contact"]
            topic = (m.get("properties") or {}).get("topic")
            out.append({"title": (m.get("title") or ""), "attendees": atts,
                        "topic": topic, "transcribed": bool(topic)})
        return out


if __name__ == "__main__":
    # connection self-test
    if not available():
        print("Day AI creds missing from .env")
    else:
        d = DayAI()
        print("token ok:", bool(d.token()))
        print("held_call(sarah@hirecharm.com):", d.held_call("sarah@hirecharm.com"))
