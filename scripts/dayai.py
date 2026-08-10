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

    def recent_meetings(self, since="2026-06-01T00:00:00Z"):
        """Recent meeting recordings with their title + linked contact emails (attendees).

        Attendee-email linkage lags in Day AI, so held detection also uses the title
        (e.g. 'Todd Dugas & Charm'). Returns [{title, attendees:[email,...]}].
        """
        res = self.search([{"objectType": "native_meetingrecording"}],
                          includeRelationships=True, timeframeStart=since)
        out = []
        for m in res.get("native_meetingrecording", {}).get("results", []):
            rels = m.get("relationships") or []
            atts = [(r.get("objectId") or "").lower() for r in rels
                    if isinstance(r, dict) and r.get("objectType") == "native_contact"]
            out.append({"title": (m.get("title") or ""), "attendees": atts})
        return out


if __name__ == "__main__":
    # connection self-test
    if not available():
        print("Day AI creds missing from .env")
    else:
        d = DayAI()
        print("token ok:", bool(d.token()))
        print("held_call(sarah@hirecharm.com):", d.held_call("sarah@hirecharm.com"))
