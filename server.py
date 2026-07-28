"""Charm VSL dashboards — hosted, self-refreshing, with CSV upload.

Serves the VSL funnel + ad funnel dashboards behind HTTP Basic Auth, rebuilds
them (Wistia / GHL / RB2B / Day AI) on startup and every hour, and lets an
authenticated user drop the daily Meta ad-set CSV at /upload to refresh the ad data.
Pure stdlib — no dependencies. All credentials come from environment variables.
"""
import os
import sys
import json
import time
import base64
import threading
import subprocess
import http.server
import socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASH = ROOT / "dashboard"
AD_DAILY = ROOT / "data/meta/ad_daily.json"
POST_CALL = ROOT / "data/manual/post_call.json"
sys.path.insert(0, str(ROOT / "scripts"))
BUILDS = ["scripts/build_dashboard.py", "scripts/build_ad_funnel.py", "scripts/build_unified.py"]
# A verdict only affects the VSL build + the unified page — skip the ad-funnel rebuild.
VERDICT_BUILDS = ["scripts/build_dashboard.py", "scripts/build_unified.py"]

USER = os.environ.get("DASH_USER", "charm")
PASS = os.environ.get("DASH_PASS", "")
INTERVAL = int(os.environ.get("REFRESH_SECONDS", "3600"))
PORT = int(os.environ.get("PORT", "3000"))

state = {"at": None, "ok": False, "ad_updated": None}


def rebuild(builds=BUILDS):
    DASH.mkdir(exist_ok=True)
    for d in ("data/ghl", "data/wistia", "data/meta", "data/dayai", "data/manual"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    # Seed the manual Meta export from env ONLY if no uploaded file exists, so a
    # user upload persists across restarts and isn't clobbered by the (older) env.
    blob = os.environ.get("AD_DAILY_JSON")
    if blob and not AD_DAILY.exists():
        AD_DAILY.write_text(blob)
    # Post-call store (lead PII) — seed from env ONLY if no file yet, so verdicts set
    # via the dashboard dropdowns persist and aren't clobbered by the (older) env.
    pc = os.environ.get("POST_CALL_JSON")
    if pc and not POST_CALL.exists():
        POST_CALL.write_text(pc)
    ok = True
    for b in builds:
        try:
            r = subprocess.run([sys.executable, b], cwd=ROOT, capture_output=True,
                               text=True, timeout=300)
            if r.returncode != 0:
                ok = False
            print(f"[build] {b} rc={r.returncode} {r.stdout.strip()[-300:]} {r.stderr.strip()[-300:]}", flush=True)
        except Exception as e:
            ok = False
            print(f"[build] {b} EXC {e}", flush=True)
    state.update(at=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), ok=ok)
    print(f"[rebuild] ok={ok} at={state['at']}", flush=True)


def ad_summary():
    try:
        d = json.loads(AD_DAILY.read_text())
        rows = d.get("rows", [])
        return {"rows": len(rows), "ad_sets": len({r.get("ad_set_name") for r in rows}),
                "source": d.get("_source", "")}
    except Exception:
        return {"rows": 0, "ad_sets": 0, "source": "none"}


def scheduler():
    while True:
        time.sleep(INTERVAL)
        rebuild()


UPLOAD_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Update ad data — Charm VSL</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
body{{font-family:Poppins,system-ui,sans-serif;background:#faf9fc;color:#1c1330;max-width:640px;margin:auto;padding:44px 22px}}
h1{{font-size:22px}} h1 span{{color:#8733ed}} .sub{{color:#5c5470;font-size:13px;margin:4px 0 22px}}
#drop{{border:2px dashed #d9c9f7;border-radius:16px;background:#fff;padding:44px 22px;text-align:center;cursor:pointer;transition:.15s}}
#drop.hot{{background:#f3ecfe;border-color:#8733ed}}
#drop .big{{font-size:16px;font-weight:600;color:#8733ed}} #drop .small{{color:#8d86a0;font-size:13px;margin-top:6px}}
#out{{margin-top:18px;font-size:14px}} .ok{{color:#0f7a4f}} .err{{color:#c0362c;font-family:ui-monospace,Menlo,monospace;font-size:13px}}
.cur{{background:#fff;border:1px solid #ece8f4;border-radius:12px;padding:14px 16px;font-size:13px;color:#5c5470;margin-bottom:20px}}
.cur b{{color:#1c1330}} a.back{{display:inline-block;margin-top:20px;color:#8733ed;text-decoration:none;font-weight:600}}
.steps{{color:#8d86a0;font-size:12px;margin-top:22px;line-height:1.7}}
button{{margin-top:14px;background:#8733ed;color:#fff;border:0;border-radius:10px;padding:11px 20px;font-weight:600;font-size:14px;cursor:pointer;font-family:inherit}}
button:disabled{{opacity:.5;cursor:default}}
</style></head><body>
<h1>Update <span>ad data</span></h1>
<div class="sub">Drop today's Meta ad-set CSV export. It refreshes the dashboard's ad numbers immediately &mdash; no more doing it by hand.</div>
<div class="cur">Currently loaded: <b>{ad_sets} ad set(s)</b>, {rows} day-rows &nbsp;&middot;&nbsp; last rebuild {at}</div>
<div id="drop"><div class="big">Drop CSV here</div><div class="small">or click to choose &middot; the file from Ads Manager &rarr; Export (per ad set)</div>
<input id="file" type="file" accept=".csv,text/csv" style="display:none"></div>
<div id="out"></div>
<div class="steps"><b>How to export:</b> Ads Manager &rarr; your GTM campaign &rarr; breakdown by <b>Ad set</b> &rarr; Reports/Export &rarr; CSV. Columns needed: Ad set name, Day, Reach, Impressions, Amount spent, Link clicks (and Schedule, once added).</div>
<a class="back" href="/">&larr; Back to dashboard</a>
<script>
const drop=document.getElementById('drop'),file=document.getElementById('file'),out=document.getElementById('out');
drop.onclick=()=>file.click();
['dragover','dragenter'].forEach(e=>drop.addEventListener(e,ev=>{{ev.preventDefault();drop.classList.add('hot')}}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{{ev.preventDefault();drop.classList.remove('hot')}}));
drop.addEventListener('drop',ev=>{{if(ev.dataTransfer.files[0])send(ev.dataTransfer.files[0])}});
file.onchange=()=>{{if(file.files[0])send(file.files[0])}};
function send(f){{
  if(!f.name.toLowerCase().endsWith('.csv')){{out.innerHTML='<span class="err">Please drop a .csv file.</span>';return;}}
  out.innerHTML='Reading <b>'+f.name+'</b>&hellip; then rebuilding the dashboard (~20s), hold on&hellip;';
  const r=new FileReader();
  r.onload=async()=>{{
    try{{
      const res=await fetch('/upload',{{method:'POST',headers:{{'Content-Type':'text/csv'}},body:r.result}});
      const j=await res.json();
      if(j.ok) out.innerHTML='<span class="ok">&#10003; Updated &mdash; '+j.ad_sets+' ad set(s), '+j.rows+' day-rows. Dashboard refreshed.</span>';
      else out.innerHTML='<span class="err">'+ (j.error||'upload failed') +'</span>';
    }}catch(e){{out.innerHTML='<span class="err">'+e+'</span>';}}
  }};
  r.readAsText(f);
}}
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _authed(self):
        if not PASS:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                u, p = base64.b64decode(h[6:]).decode().split(":", 1)
                if u == USER and p == PASS:
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Charm VSL"')
        self.end_headers()
        return False

    def _send(self, body, ct="text/html", code=200):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._send(b"ok", "text/plain")
        if not self._authed():
            return
        if path == "/upload":
            s = ad_summary()
            return self._send(UPLOAD_PAGE.format(ad_sets=s["ad_sets"], rows=s["rows"],
                                                 at=state["at"] or "pending"))
        if path in ("/", "/index.html") and (DASH / "index.html").exists():
            return self._send((DASH / "index.html").read_bytes())
        fname = {"/vsl": "vsl_dashboard.html", "/ads": "ad_funnel_dashboard.html"}.get(path)
        if fname and (DASH / fname).exists():
            return self._send((DASH / fname).read_bytes())
        self.send_response(404)
        self.end_headers()

    def _verdict(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
            email = (body.get("email") or "").strip().lower()
            outcome = body.get("outcome") or "auto"
            fit = body.get("fit") or None
            if not email:
                raise ValueError("missing email")
            if outcome not in ("auto", "held", "no_show", "cancelled"):
                raise ValueError("bad outcome")
            if fit not in (None, "qualified", "unqualified"):
                raise ValueError("bad fit")
            data = json.loads(POST_CALL.read_text()) if POST_CALL.exists() else {"leads": {}}
            leads = data.setdefault("leads", {})
            for k in [k for k in leads if k.lower() == email]:  # drop case-variant dupes
                leads.pop(k)
            if outcome == "auto" and not fit:
                pass  # cleared → no entry (falls back to Day AI auto-detection / "not yet")
            else:
                leads[email] = {"outcome": outcome, "fit": fit,
                                "date": time.strftime("%Y-%m-%d", time.gmtime())}
            POST_CALL.parent.mkdir(parents=True, exist_ok=True)
            POST_CALL.write_text(json.dumps(data, indent=1))
            rebuild(VERDICT_BUILDS)  # synchronous — reflect before responding
            resp = {"ok": True}
        except Exception as e:
            resp = {"ok": False, "error": str(e)}
        self._send(json.dumps(resp), "application/json", 200 if resp.get("ok") else 400)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path not in ("/upload", "/verdict"):
            self.send_response(404)
            self.end_headers()
            return
        if not self._authed():
            return
        if path == "/verdict":
            return self._verdict()
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8", "replace")
            import import_ad_daily
            out = import_ad_daily.rows_from_csv_text(
                body, source="uploaded " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
            AD_DAILY.parent.mkdir(parents=True, exist_ok=True)
            AD_DAILY.write_text(json.dumps(out))
            state["ad_updated"] = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
            rebuild()  # synchronous — reflect the new data before responding
            resp = {"ok": True, "rows": len(out["rows"]),
                    "ad_sets": len({r["ad_set_name"] for r in out["rows"]})}
        except Exception as e:
            resp = {"ok": False, "error": str(e)}
        self._send(json.dumps(resp), "application/json", 200 if resp.get("ok") else 400)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    rebuild()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"serving on :{PORT} (refresh every {INTERVAL}s)", flush=True)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer(("", PORT), Handler).serve_forever()
