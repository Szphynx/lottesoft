#!/usr/bin/env python3
"""Status + control page for one Pi: is thermal-matrix running, since when,
what mode, a live preview of the panel, and restart/reboot.

Binds to the Tailscale IP only (falls back to localhost if Tailscale isn't
up) -- reachable from your tailnet, not the open LAN or internet. Runs as
root (same as thermal-matrix.service), so restart/reboot need no sudo.

Endpoints (all require HTTP Basic auth):
    GET  /            human-readable status page
    GET  /status.json status + stats as JSON, for the dashboard
    GET  /preview.png latest snapshot of what's on the LED panel
    POST /restart      restart the thermal-matrix service
    POST /reboot        reboot the Pi

POST endpoints also require an X-Dashboard header, which a plain HTML
form can't set -- the dashboard sends it via fetch(), so a page that
merely reuses a cached browser login can't trigger either action.
"""
import base64
import hmac
import http.server
import json
import os
import subprocess
import time

SERVICE = "thermal-matrix"
PORT = 8787
USER = os.environ.get("STATUS_USER", "")
PASS = os.environ.get("STATUS_PASS", "")
PREVIEW_PATH = "/run/thermal-matrix/preview.png"
DASHBOARD_HEADER = "X-Dashboard"


def run(*cmd, timeout=3):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()


def cpu_temp_c():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except (OSError, ValueError):
        return None


def service_info():
    since = run("systemctl", "show", SERVICE, "-p", "ActiveEnterTimestamp", "--value")
    uptime = ""
    if since and since != "n/a":
        started = time.mktime(time.strptime(since.split(" +")[0], "%a %Y-%m-%d %H:%M:%S"))
        uptime = f"{(time.time() - started) / 3600:.1f}h"
    try:
        flags = open("/etc/default/thermal-matrix").read().strip()
    except FileNotFoundError:
        flags = ""
    return {
        "host": run("hostname"),
        "ip": run("tailscale", "ip", "-4"),
        "active": run("systemctl", "is-active", SERVICE),
        "uptime": uptime,
        "flags": flags,
        "cpu_temp_c": cpu_temp_c(),
        "has_preview": os.path.exists(PREVIEW_PATH),
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            self._unauthorized()
            return
        if self.path == "/status.json":
            self._send(json.dumps(service_info()).encode(), "application/json")
            return
        if self.path == "/preview.png":
            self._send_preview()
            return
        self._send(self._status_page().encode(), "text/html")

    def do_POST(self):
        if not self._authorized() or self.headers.get(DASHBOARD_HEADER) != "1":
            self._unauthorized()
            return
        if self.path == "/restart":
            ok = subprocess.run(["systemctl", "restart", SERVICE], timeout=10).returncode == 0
            self._send(json.dumps({"ok": ok}).encode(), "application/json")
        elif self.path == "/reboot":
            subprocess.Popen(["systemctl", "reboot"])
            self._send(json.dumps({"ok": True}).encode(), "application/json")
        else:
            self.send_response(404)
            self._cors()
            self.end_headers()

    def _status_page(self):
        info = service_info()
        color = "#2ecc71" if info["active"] == "active" else "#e74c3c"
        preview = ('<img src="/preview.png" width="256" height="256" '
                   'style="image-rendering:pixelated;border:1px solid #333">'
                   if info["has_preview"] else "<p>(no preview yet)</p>")
        return f"""<!doctype html><meta charset="utf-8"><title>{info['host']}</title>
<body style="font:16px monospace;background:#111;color:#eee;padding:2rem">
<h1>{info['host']}</h1>
<p>status: <b style="color:{color}">{info['active']}</b></p>
<p>up: {info['uptime'] or '-'}</p>
<p>mode: {info['flags'] or '(not set)'}</p>
<p>cpu: {info['cpu_temp_c'] if info['cpu_temp_c'] is not None else '-'} C</p>
{preview}
</body>"""

    def _send_preview(self):
        try:
            with open(PREVIEW_PATH, "rb") as f:
                body = f.read()
        except OSError:
            self.send_response(404)
            self._cors()
            self.end_headers()
            return
        self._send(body, "image/png")

    def _authorized(self):
        if not USER:
            return True
        expected = "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="thermal-status"')
        self._cors()
        self.end_headers()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", f"Authorization, Content-Type, {DASHBOARD_HEADER}")

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def bind_addr():
    ip = run("tailscale", "ip", "-4")
    return ip if ip else "127.0.0.1"


if __name__ == "__main__":
    http.server.HTTPServer((bind_addr(), PORT), Handler).serve_forever()
