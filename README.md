# Charm VSL Dashboard Host

Serves the VSL funnel + ad funnel dashboards behind HTTP Basic Auth and rebuilds
them hourly from live Wistia / GHL / RB2B / Day AI data. Pure Python stdlib.

Routes: `/` index · `/vsl` · `/ads` · `/health`

Env vars (set in Coolify): `WISTIA_API_TOKEN`, `GHL_PIT_TOKEN`, `GHL_LOCATION_ID`,
`RB2B_ENDPOINT`, `RB2B_SECRET`, `DAYAI_BASE_URL`, `DAYAI_CLIENT_ID`,
`DAYAI_CLIENT_SECRET`, `DAYAI_REFRESH_TOKEN`, `DASH_USER`, `DASH_PASS`,
`REFRESH_SECONDS` (default 3600).

Build scripts + templates are copied from the `charm-vsl-metrics` project (source
of truth). No secrets or lead data are committed — data is regenerated at runtime.

⚠️ **THAT COPY IS THE WHOLE TRAP.** Fixing a build script in `charm-vsl-metrics` changes
NOTHING here. The live page ran a week behind that way, still fetching GoHighLevel after the
26 Aug iClosed cutover, because the fixes never reached this repo. After any build change:
copy `scripts/{build_dashboard.py,iclosed_source.py,dayai.py,template.html}` across, commit
and push HERE, then redeploy.

⚠️ **`/app/data` is mounted over at runtime.** A file committed under `data/` does NOT reach
the container — verified live: `scripts/iclosed_source.py` and `data/frozen/historic_era.json`
were added in the SAME commit and only the first arrived. That is also why `data/ghl` and
friends survive redeploys. Anything static the build must read goes in `scripts/`. The frozen
historic era is shipped as `scripts/historic_era.json` for exactly this reason.

**Sources are iClosed + Wistia + Day AI.** GHL is frozen history only (the historic tab) and
is no longer fetched. `ICLOSED_API_KEY` is required — without it the hourly rebuild throws
and serves no page at all, which is worse than serving a stale one.
