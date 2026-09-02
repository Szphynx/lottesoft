#!/usr/bin/env python3
"""Tiny status page for one Pi: is thermal-matrix running, since when, what mode.

Binds to the Tailscale IP only (falls back to localhost if Tailscale isn't
up) -- reachable from your tailnet, not the open LAN or internet.
"""
import http.server
import json
import subprocess
import time

SERVICE = "thermal-matrix"
PORT = 8787


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()


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
        "active": run("systemctl", "is-active", SERVICE),
        "uptime": uptime,
        "flags": flags,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        info = service_info()
        if self.path == "/status.json":
            self._send(json.dumps(info).encode(), "application/json")
            return
        color = "#2ecc71" if info["active"] == "active" else "#e74c3c"
        html = f"""<!doctype html><meta charset="utf-8"><title>{info['host']}</title>
<body style="font:16px monospace;background:#111;color:#eee;padding:2rem">
<h1>{info['host']}</h1>
<p>status: <b style="color:{color}">{info['active']}</b></p>
<p>up: {info['uptime'] or '-'}</p>
<p>mode: {info['flags'] or '(not set)'}</p>
</body>"""
        self._send(html.encode(), "text/html")

    def _send(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def bind_addr():
    ip = run("tailscale", "ip", "-4")
    return ip if ip else "127.0.0.1"


if __name__ == "__main__":
    http.server.HTTPServer((bind_addr(), PORT), Handler).serve_forever()
