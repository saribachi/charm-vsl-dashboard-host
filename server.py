"""Charm VSL dashboards — hosted, self-refreshing.

Serves the VSL funnel + ad funnel dashboards behind HTTP Basic Auth, and rebuilds
them (pulling fresh Wistia / GHL / RB2B / Day AI data) on startup and every hour.
Pure stdlib — no dependencies. All credentials come from environment variables.
"""
import os
import sys
import time
import base64
import threading
import subprocess
import http.server
import socketserver
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DASH = ROOT / "dashboard"
BUILDS = ["scripts/build_dashboard.py", "scripts/build_ad_funnel.py", "scripts/build_unified.py"]

USER = os.environ.get("DASH_USER", "charm")
PASS = os.environ.get("DASH_PASS", "")
INTERVAL = int(os.environ.get("REFRESH_SECONDS", "3600"))
PORT = int(os.environ.get("PORT", "3000"))

state = {"at": None, "ok": False}


def rebuild():
    DASH.mkdir(exist_ok=True)
    for d in ("data/ghl", "data/wistia", "data/meta", "data/dayai"):
        (ROOT / d).mkdir(parents=True, exist_ok=True)
    # Manual Meta export injected via env (kept out of the repo). Update this env
    # var whenever a fresh CSV is imported, until the Ads MCP pull is automated.
    blob = os.environ.get("AD_DAILY_JSON")
    if blob:
        (ROOT / "data/meta/ad_daily.json").write_text(blob)
    ok = True
    for b in BUILDS:
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


def scheduler():
    while True:
        time.sleep(INTERVAL)
        rebuild()


INDEX = """<!doctype html><html><head><meta charset="utf-8"><title>Charm VSL Dashboards</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
<style>body{{font-family:Poppins,system-ui,sans-serif;background:#fcfcfb;color:#221833;padding:56px 24px;max-width:620px;margin:auto}}
h1{{font-size:24px}} h1 span{{color:#8733ed}}
a{{display:block;padding:18px 22px;margin:14px 0;border:1px solid #ece8f4;border-radius:14px;text-decoration:none;color:#8733ed;font-weight:600;font-size:18px;background:#fff}}
a:hover{{background:#f3ecfe}} .m{{color:#8d86a0;font-size:13px;margin-top:28px}}</style></head>
<body><h1>Charm <span>VSL</span> Dashboards</h1>
<a href="/vsl">VSL Funnel &mdash; GTM &rarr;</a>
<a href="/ads">Ad Funnel &rarr;</a>
<div class="m">Auto-refreshes every {mins} min. Last build: {at} ({status}).</div></body></html>"""


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

    def _send(self, body, ct="text/html"):
        self.send_response(200)
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
        if path in ("/", "/index.html"):
            # the unified single-page dashboard is the default view
            if (DASH / "index.html").exists():
                return self._send((DASH / "index.html").read_bytes())
            html = INDEX.format(mins=INTERVAL // 60, at=state["at"] or "pending",
                                status="ok" if state["ok"] else "building")
            return self._send(html.encode())
        fname = {"/vsl": "vsl_dashboard.html", "/ads": "ad_funnel_dashboard.html"}.get(path)
        if fname and (DASH / fname).exists():
            return self._send((DASH / fname).read_bytes())
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    rebuild()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"serving on :{PORT} (refresh every {INTERVAL}s)", flush=True)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer(("", PORT), Handler).serve_forever()
