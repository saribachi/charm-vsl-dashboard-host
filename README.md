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
