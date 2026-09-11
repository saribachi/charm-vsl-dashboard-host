"""The offer registry — one declaration per offer, one code path for all of them.

WHY THIS EXISTS. Until Sep 2026 the dashboard hardcoded exactly two funnels: GTM lived
inline in `main()` and CS Flex was bolted on as `cs_funnel()`, a near-copy that had
already drifted (it computed play rate on a different denominator and read half as
engaging as GTM for months). Three more iClosed meeting events then appeared. Copying
`cs_funnel()` three more times would guarantee five funnels drifting five ways, so the
per-offer facts move here and the computation becomes one generic function.

WHAT AN OFFER IS. One thing Charm sells, followed ad dollar -> booked call -> cash. It
owns an iClosed event, and MAY own a landing page, a VSL and a set of Meta ad sets. The
three events added in September own none of those yet, which is the point of `wiring`:
a stage with no source configured renders as NOT WIRED, never as zero. Zero is a claim
that nothing happened; not-wired is the truth, which is that nobody is measuring.

QUESTIONS ARE PER OFFER, NOT GLOBAL. Every event asks its own screening questions, and
the old global map was GTM's. CS Flex asks about support volume and ticket types, so it
matched nothing and every CS disqualification silently landed in `dq_other` from the
cutover onward. Each offer now carries its own question map and its own gate rules.
"""

# Canonical qualifier keys are per offer and deliberately NOT shared: "revenue" means
# annual revenue on every offer that asks it, but ACV only exists on GTM and ticket
# volume only on CS. A shared enum would invite reading one offer's gate against
# another's answers.


class Offer:
    def __init__(self, key, name, tag, color, iclosed_event_ids,
                 domain=None, wistia=None, adset_prefixes=(), questions=(),
                 dq_rules=(), gate=False, deep=False, live_from=None, note=""):
        self.key = key
        self.name = name
        self.tag = tag
        self.color = color
        self.iclosed_event_ids = list(iclosed_event_ids)
        self.domain = domain                      # None => no landing page of its own
        self.wistia = wistia or {}                # {} => no VSL of its own
        self.adset_prefixes = [p.lower() for p in adset_prefixes]
        self.questions = list(questions)          # [(identifier substring, canonical key, legacy GHL field id)]
        self.dq_rules = list(dq_rules)            # [(verdict, [(key, op, value), ...])] — FIRST MATCH WINS
        self.gate = gate                          # does iClosed screen and disqualify?
        self.deep = deep                          # gets the Day AI / RB2B / cash panels
        self.live_from = live_from                # start of this offer's live era
        self.note = note

    # ---- wiring ----------------------------------------------------------------
    # Which funnel stages this offer actually has a source for. Read by the builder to
    # decide between a number and a NOT WIRED marker, and rendered on the page so an
    # unwired stage is a visible gap rather than a silent zero.
    @property
    def wiring(self):
        return {
            "ads": bool(self.adset_prefixes) or self.key == DEFAULT_ADSET_OFFER,
            "page": bool(self.domain),
            "video": bool(self.wistia.get("media_id")),
            "fills": True,                        # every offer has an iClosed event
            "booked": True,
            "gate": self.gate,
            "held": self.deep,                    # attendance comes from Day AI transcripts
            "cash": self.deep,                    # Closed Won matching is GTM-only today
        }

    def field_id(self, canonical):
        """The legacy GHL custom-field id this answer is stored under, if it has one.

        The frozen historic era is keyed by GHL field id permanently, so GTM and CS keep
        theirs. Offers created after the cutover have no GHL history and use the
        canonical key directly.
        """
        for _sub, key, fid in self.questions:
            if key == canonical:
                return fid or canonical
        return canonical

    def classify(self, answers):
        """Canonical-keyed answers -> a qualification verdict, or None.

        Returns None when the offer has no gate, or when the lead answered nothing —
        a partial fill that never reached the gate is NOT a disqualification.
        """
        if not self.gate:
            return None
        if not any(answers.get(k) for _s, k, _f in self.questions):
            return None
        for verdict, conds in self.dq_rules:
            if all(_match(answers.get(k), op, val) for k, op, val in conds):
                return verdict
        return "qualified"


def _match(actual, op, expected):
    a = (actual or "").strip()
    if op == "eq":
        return a == expected
    if op == "in":
        return a in expected
    if op == "contains":
        return expected.lower() in a.lower()
    raise ValueError(f"unknown operator {op!r}")


# ---------------------------------------------------------------------------------
# The registry. Order is display order.
# ---------------------------------------------------------------------------------

OFFERS = [
    Offer(
        key="gtm", name="Lead generation", tag="GTM", color="#8247f5",
        iclosed_event_ids=[47452],
        domain="gtm.hirecharm.com",
        # ⚠️ The VSL was replaced 26 Aug 2026 with a version 8x shorter. Watch metrics are
        # ratios against duration, so nothing spanning that date is comparable. The prior
        # id stays recorded so the frozen historic era can be read deliberately.
        wistia={"media_id": "18w5xszdv9", "prior_media_id": "swyi1909di",
                "switched_on": "2026-08-26", "stats_since": "2026-08-26"},
        # GTM is the DEFAULT ad-set owner (see DEFAULT_ADSET_OFFER) — these prefixes are
        # the sets known to be GTM's, not the full set it receives.
        # "Master GTM Ads" arrived in the 11 Sep export as a rename. It would have fallen
        # to the catch-all and still counted, but naming it makes the assignment explicit
        # instead of accidental. Note CS's "CS Flex Master" is NOT caught by "master gtm":
        # startswith is anchored, and GTM is checked first.
        adset_prefixes=["gtm ", "master gtm", "retargeting", '"what if we', '"your prospect'],
        questions=[
            ("annual-revenue",         "revenue",  "t8kIeNWMhGLyKmelKXYL"),
            ("average-contract-value", "acv",      "IUjCRF0gg4GKikd3DmlK"),
            ("could-you-service-them", "capacity", "UX6TIRA7aL6rjW65oKwV"),
            ("company-website",        "website",  "website"),
        ],
        # Unchanged from the original classify_qual(). First match wins, so the order
        # of these three is part of the spec, not an implementation detail.
        dq_rules=[
            ("dq_capacity",    [("capacity", "eq", "No, we're at capacity")]),
            ("dq_acv_low",     [("acv", "eq", "Under $5K")]),
            ("dq_revenue_acv", [("revenue", "eq", "Under $1M"), ("acv", "eq", "$5K to $14K")]),
        ],
        gate=True, deep=True, live_from="2026-08-26",
        note="The original funnel. The only offer wired end to end, ad spend through to cash.",
    ),
    Offer(
        key="cs", name="Customer success", tag="CS FLEX", color="#0099ff",
        iclosed_event_ids=[47740],
        domain="cs.hirecharm.com",
        wistia={"media_id": "51low6lcfn", "prior_media_id": "lk17fifkvg",
                "switched_on": "2026-08-27", "stats_since": "2026-08-01"},
        adset_prefixes=["cs flex"],
        # These are CS's REAL iClosed identifiers. The previous build mapped GTM's
        # questions here, so none of them ever matched and the answer mix was empty.
        questions=[
            ("who-handles-your-support-today",     "who_handles_support", "GOS9ePxFcZVLcxICP2Xc"),
            ("support-volume-look-like",           "volume_driver",       "I1Uzm4LbH3SmM21M07Yq"),
            ("what-do-most-of-your-tickets-involve", "ticket_types",      "i1S56AS9bJVItMrsJ6IP"),
        ],
        # No gate yet: the answers are reported as a mix so the shape of demand is
        # visible, and rules get written once there is enough data to know a bad fit.
        dq_rules=[], gate=False, deep=False, live_from="2026-08-27",
        note="Second funnel, launched 18 Aug 2026. No qualification gate defined yet — "
             "judge it on cost per booking, not cost per qualified.",
    ),
    Offer(
        key="xyz", name="XYZ calls", tag="XYZ", color="#17e885",
        iclosed_event_ids=[48804],
        # No lander, no VSL, no ad sets of its own. Its first booking arrived carrying
        # gtm.hirecharm.com as the source page and a GTM VSL tag, i.e. GTM traffic routed
        # into a different meeting type. Give it a domain/video/prefix here the moment it
        # gets its own, and the top of its funnel lights up with no other change.
        wistia={}, adset_prefixes=[],
        questions=[
            ("what-is-your-companys-annual-revenue",                "revenue",       None),
            ("what-is-your-monthly-budget-for-growth-initiatives",  "budget",        None),
            ("when-are-you-looking-to-get-started",                 "timeline",      None),
            ("are-you-the-decision-maker-for-this",                 "decision_maker", None),
        ],
        # It screens, but no disqualification rules have been agreed. Leaving dq_rules
        # empty means iClosed's own verdict stands and the answer mix is reported;
        # inventing thresholds here would manufacture a gate nobody signed off.
        dq_rules=[], gate=False, deep=False, live_from="2026-09-10",
        note="60-minute call. Screens on revenue, budget, timeline and decision-maker, "
             "but no gate rules agreed — answers are reported as a mix.",
    ),
    Offer(
        key="consulting", name="Consulting with Chris", tag="CONSULT", color="#020202",
        iclosed_event_ids=[48805],
        wistia={}, adset_prefixes=[],
        # bypassQuestion is true on this event: iClosed asks nothing and screens nobody,
        # so there is no fill stage distinct from the booking and no qualified stage.
        questions=[], dq_rules=[], gate=False, deep=False, live_from="2026-09-10",
        note="No screening questions (bypassQuestion). Booking IS the fill — the funnel "
             "starts at booked and there can be no qualification rate.",
    ),
    Offer(
        key="booth", name="Chris Booth 1-1 mentorship", tag="BOOTH 1-1", color="#ff4f00",
        iclosed_event_ids=[48803],
        wistia={}, adset_prefixes=[],
        questions=[], dq_rules=[], gate=False, deep=False, live_from="2026-09-10",
        note="Chris Booth Advisory, a SEPARATE brand from Charm — its spend and its "
             "conversion rates do not belong in any Charm total.",
    ),
]

BY_KEY = {o.key: o for o in OFFERS}

# Ad sets that match no offer's prefixes land here. GTM has always been the catch-all —
# the previous code defined CS by prefix and gave GTM everything else — and that stays,
# because the alternative silently DROPS a newly named GTM ad set out of the spend
# figures. What changes is that the catch-all is now visible: every set assigned this way
# is reported as `matched_by: "default"` so drift shows up on the page instead of hiding
# inside a total.
DEFAULT_ADSET_OFFER = "gtm"


def assign_adset(name):
    """Ad set name -> (offer key, how it matched). Never returns None: an unmatched set
    goes to the default owner and is labelled, so nothing vanishes from spend."""
    n = (name or "").strip().lower()
    for o in OFFERS:
        for p in o.adset_prefixes:
            if n.startswith(p):
                return o.key, "prefix"
    return DEFAULT_ADSET_OFFER, "default"


def event_owner(event_id):
    """iClosed event id -> the offer that owns it, or None if unregistered."""
    for o in OFFERS:
        if int(event_id) in o.iclosed_event_ids:
            return o
    return None


def unregistered(events):
    """iClosed events with no offer declared here.

    Called on every build against the live /v1/events list. A meeting event created in
    iClosed is otherwise invisible to this dashboard — its bookings are simply filtered
    out by event id and nothing anywhere says so. Surfaced as a warning instead.
    """
    out = []
    for e in events or []:
        if e.get("deletedAt"):
            continue
        if not event_owner(e.get("id")):
            out.append({"id": e.get("id"), "name": e.get("name"),
                        "status": e.get("status"), "link": e.get("linkPrefix")})
    return out
