#!/usr/bin/env python3
"""Per-Pi control panel: service status, camera detection, live low-fps
preview, and the config knobs from thermal_matrix.py's --palette/--fit/
--rotate/etc flags. Changing a setting writes /run/thermal-matrix/live-config.json,
which the running thermal_matrix.py process polls every frame and applies
immediately -- no restart. /etc/default/thermal-matrix is kept in sync too,
so a reboot or manual restart comes back up with the same settings.

Binds to the Tailscale IP only (falls back to localhost if Tailscale isn't
up) -- reachable from your tailnet, not the open LAN or internet.
"""
import base64
import hmac
import http.server
import json
import os
import re
import shlex
import subprocess
import time

SERVICE = "thermal-matrix"
PORT = 8787
USER = os.environ.get("STATUS_USER", "")
PASS = os.environ.get("STATUS_PASS", "")

FLAGS_FILE = "/etc/default/thermal-matrix"
PREVIEW_JPEG = "/run/thermal-matrix/preview.jpg"
PREVIEW_STATS = "/run/thermal-matrix/stats.json"
LIVE_CONFIG_FILE = "/run/thermal-matrix/live-config.json"
PREVIEW_MAX_AGE = 5.0

PRESETS_DIR = "/etc/thermal-matrix/presets"
LAST_PRESET_FILE = os.path.join(PRESETS_DIR, ".last")

# Mirrors thermal_matrix.py's BODYHEAT_* defaults -- kept as plain literals
# here so this lightweight server doesn't have to import numpy/cv2/rgbmatrix.
DEFAULT_COLD_MAX_C = 25.0
DEFAULT_HOT_MIN_C = 25.5
DEFAULT_HOT_MAX_C = 27.0

PALETTES = ["ironbow", "whitehot", "blackhot", "rainbow", "redhot",
            "inferno", "magma", "plasma", "turbo", "jet", "hot", "ocean"]


def run(*cmd, timeout=3):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def service_uptime():
    """Hours since the service last became active, via the monotonic clock --
    avoids parsing systemd's human timestamp, whose format shifts with locale
    and timezone (e.g. a 'CEST' suffix instead of a numeric offset)."""
    raw = run("systemctl", "show", SERVICE, "-p", "ActiveEnterTimestampMonotonic", "--value")
    try:
        active_since_us = int(raw)
        with open("/proc/uptime") as f:
            now_s = float(f.read().split()[0])
    except (ValueError, TypeError, OSError, IndexError):
        return ""
    if active_since_us <= 0:
        return ""
    elapsed_h = (now_s - active_since_us / 1_000_000) / 3600
    return f"{elapsed_h:.1f}h" if elapsed_h >= 0 else ""


def service_info():
    return {
        "host": run("hostname"),
        "active": run("systemctl", "is-active", SERVICE),
        "uptime": service_uptime(),
    }


def camera_detected():
    return bool(re.search(r"\b33\b", run("i2cdetect", "-y", "0")))


def preview_stats():
    try:
        with open(PREVIEW_STATS) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if time.time() - data.get("ts", 0) > PREVIEW_MAX_AGE:
        return None
    return data


def status_payload():
    """Each piece is independent -- one failing (a systemctl hiccup, a
    missing i2c-tools binary) shouldn't blank the whole dashboard."""
    info = {"host": "", "active": "unknown", "uptime": "", "camera": False, "preview": None}
    try:
        info.update(service_info())
    except Exception:
        pass
    try:
        info["camera"] = camera_detected()
    except Exception:
        pass
    try:
        info["preview"] = preview_stats()
    except Exception:
        pass
    return info


# ----------------------------------------------------------------------------
# FLAGS file <-> config dict. Anything not recognised (--stats, --backend,
# ...) round-trips through "extra" untouched.
# ----------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "mode": "auto", "palette": "ironbow", "fit": "letterbox",
    "rotate": 0, "brightness": 50,
    "cold_max": DEFAULT_COLD_MAX_C, "hot_min": DEFAULT_HOT_MIN_C, "hot_max": DEFAULT_HOT_MAX_C,
}


def read_flag_tokens():
    try:
        raw = open(FLAGS_FILE).read()
    except FileNotFoundError:
        return []
    m = re.search(r'FLAGS="(.*)"', raw)
    return shlex.split(m.group(1)) if m else []


def parse_config(tokens):
    cfg = dict(DEFAULT_CONFIG)
    extra = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--bodyheat":
            cfg["mode"] = "bodyheat"
        elif t == "--colorwise":
            cfg["mode"] = "colorwise"
        elif t in ("--palette", "--fit"):
            i += 1
            cfg[t[2:]] = tokens[i]
        elif t == "--rotate":
            i += 1
            cfg["rotate"] = int(tokens[i])
        elif t == "--brightness":
            i += 1
            cfg["brightness"] = int(tokens[i])
        elif t in ("--cold-max", "--hot-min", "--hot-max"):
            i += 1
            cfg[t[2:].replace("-", "_")] = float(tokens[i])
        else:
            extra.append(t)
        i += 1
    cfg["extra"] = extra
    return cfg


def build_flags(cfg):
    parts = []
    if cfg["mode"] == "bodyheat":
        parts.append("--bodyheat")
    elif cfg["mode"] == "colorwise":
        parts.append("--colorwise")
    else:
        parts += ["--palette", cfg["palette"]]
    parts += ["--fit", cfg["fit"], "--rotate", str(cfg["rotate"]),
              "--brightness", str(cfg["brightness"])]
    if cfg["mode"] in ("bodyheat", "colorwise"):
        parts += ["--cold-max", str(cfg["cold_max"]),
                  "--hot-min", str(cfg["hot_min"]),
                  "--hot-max", str(cfg["hot_max"])]
    parts += cfg.get("extra", [])
    if "--stats" not in parts:
        parts.append("--stats")
    return " ".join(parts)


def write_flags(cfg):
    with open(FLAGS_FILE, "w") as f:
        f.write(f'FLAGS="{build_flags(cfg)}"\n')


def restart_service():
    subprocess.run(["systemctl", "restart", SERVICE], timeout=10)


def read_config():
    """Current live state if thermal-matrix is running and has published
    one, otherwise whatever's parked in the flags file (e.g. before first
    start)."""
    try:
        with open(LIVE_CONFIG_FILE) as f:
            data = json.load(f)
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({k: data[k] for k in DEFAULT_CONFIG if k in data})
        cfg["extra"] = parse_config(read_flag_tokens())["extra"]
        return cfg
    except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError):
        return parse_config(read_flag_tokens())


def apply_live(cfg):
    """Push a config live (thermal_matrix.py picks it up within one render
    frame, no restart) and persist it so a future restart/reboot matches."""
    write_flags(cfg)
    os.makedirs(os.path.dirname(LIVE_CONFIG_FILE), exist_ok=True)
    tmp = LIVE_CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: cfg[k] for k in DEFAULT_CONFIG}, f)
    os.replace(tmp, LIVE_CONFIG_FILE)


# ----------------------------------------------------------------------------
# Presets -- named snapshots of the config dict, saved as JSON files so you
# can jump back to a known-good setup instead of re-dialing every field.
# ----------------------------------------------------------------------------

def safe_preset_name(name):
    name = re.sub(r"[^A-Za-z0-9 _-]", "", name or "").strip()
    return re.sub(r"\s+", "_", name)[:64]


def list_presets():
    try:
        return sorted(f[:-5] for f in os.listdir(PRESETS_DIR) if f.endswith(".json"))
    except FileNotFoundError:
        return []


def read_last_preset_name():
    try:
        with open(LAST_PRESET_FILE) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def save_preset(name, cfg):
    safe = safe_preset_name(name)
    if not safe:
        return None
    os.makedirs(PRESETS_DIR, exist_ok=True)
    with open(os.path.join(PRESETS_DIR, safe + ".json"), "w") as f:
        json.dump(cfg, f, indent=2)
    with open(LAST_PRESET_FILE, "w") as f:
        f.write(safe)
    return safe


def load_preset(name):
    safe = safe_preset_name(name)
    if not safe:
        raise FileNotFoundError(name)
    with open(os.path.join(PRESETS_DIR, safe + ".json")) as f:
        return json.load(f)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if not self._authorized():
            return self._unauthorized()
        if self.path.startswith("/preview.jpg"):
            return self._serve_preview()
        if self.path == "/api/status":
            return self._json(status_payload())
        if self.path == "/api/config":
            return self._json(read_config())
        if self.path == "/api/presets":
            return self._json({"names": list_presets(), "last": read_last_preset_name()})
        self._send(DASHBOARD_HTML.encode(), "text/html")

    def do_POST(self):
        if not self._authorized():
            return self._unauthorized()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b"{}"

        if self.path == "/api/config":
            try:
                incoming = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            cfg = read_config()
            for key in DEFAULT_CONFIG:
                if key in incoming:
                    cfg[key] = incoming[key]
            apply_live(cfg)
            return self._json({"ok": True, "config": cfg})

        if self.path == "/api/restart":
            restart_service()
            return self._json({"ok": True})

        if self.path == "/api/presets/save":
            try:
                incoming = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return
            cfg = {k: incoming[k] for k in DEFAULT_CONFIG if k in incoming}
            cfg["extra"] = read_config()["extra"]
            safe = save_preset(incoming.get("name", ""), cfg)
            if not safe:
                self.send_response(400)
                self.end_headers()
                return
            return self._json({"ok": True, "name": safe})

        if self.path == "/api/presets/load":
            try:
                incoming = json.loads(body)
                cfg = load_preset(incoming.get("name", ""))
            except (json.JSONDecodeError, FileNotFoundError):
                self.send_response(404)
                self.end_headers()
                return
            full_cfg = dict(DEFAULT_CONFIG)
            full_cfg.update({k: cfg[k] for k in DEFAULT_CONFIG if k in cfg})
            full_cfg["extra"] = cfg.get("extra", [])
            apply_live(full_cfg)
            return self._json({"ok": True, "config": full_cfg})

        self.send_response(404)
        self.end_headers()

    def _authorized(self):
        if not USER:
            return True
        expected = "Basic " + base64.b64encode(f"{USER}:{PASS}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), expected)

    def _unauthorized(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="thermal-status"')
        self.end_headers()

    def _serve_preview(self):
        try:
            with open(PREVIEW_JPEG, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()
            return
        self._send(body, "image/jpeg", cache=False)

    def _json(self, payload):
        self._send(json.dumps(payload).encode(), "application/json", cache=False)

    def _send(self, body, content_type, cache=True):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def bind_addr():
    ip = run("tailscale", "ip", "-4")
    return ip if ip else "127.0.0.1"


DASHBOARD_HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>thermal-matrix control</title>
<style>
  * { box-sizing: border-box; }
  body {
    font: 15px/1.5 "SF Mono", "Consolas", monospace;
    background: #0e1210; color: #d8e0dc;
    margin: 0; padding: 1.5rem;
  }
  h1 {
    font-size: 1.1rem; margin: 0 0 1.25rem; color: #eef4f0;
    display: flex; align-items: center; gap: 0.55rem;
  }
  .dot {
    width: 11px; height: 11px; border-radius: 50%; flex: none;
    background: #4a5750; transition: background 0.2s, box-shadow 0.2s;
  }
  .dot.ok { background: #4fd18f; box-shadow: 0 0 8px 1px #4fd18f80; }
  .dot.bad { background: #e2574c; box-shadow: 0 0 8px 1px #e2574c80; }
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 1rem; max-width: 960px;
  }
  .card {
    background: #161c19; border: 1px solid #2a3430; border-radius: 8px;
    padding: 1rem 1.15rem;
  }
  .card h2 {
    font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em;
    color: #7f9088; margin: 0 0 0.75rem;
  }
  .row { display: flex; justify-content: space-between; margin-bottom: 0.4rem; }
  .row:last-child { margin-bottom: 0; }
  .pill {
    display: inline-block; padding: 0.05em 0.6em; border-radius: 999px;
    font-size: 0.82rem; font-weight: 600;
  }
  .pill.ok { background: #163a26; color: #4fd18f; }
  .pill.bad { background: #3a1616; color: #e2574c; }
  #preview {
    width: 100%; aspect-ratio: 1; image-rendering: pixelated;
    background: #000; border-radius: 6px; display: block;
  }
  form, .stack { display: flex; flex-direction: column; gap: 0.7rem; }
  label { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.82rem; color: #9fb0a8; }
  select, input {
    background: #0e1210; border: 1px solid #2a3430; color: #d8e0dc;
    border-radius: 5px; padding: 0.4rem 0.5rem; font: inherit;
  }
  .thresholds { display: none; gap: 0.7rem; flex-direction: row; }
  .thresholds.show { display: flex; }
  .thresholds label { flex: 1; }
  button {
    background: #1e6b46; color: #eef4f0; border: none; border-radius: 5px;
    padding: 0.55rem 0.9rem; font: inherit; font-weight: 600; cursor: pointer;
  }
  button.secondary { background: #2a3430; }
  button:active { transform: translateY(1px); }
  .msg { font-size: 0.8rem; color: #7f9088; min-height: 1.2em; }
  .preset-row { display: flex; gap: 0.6rem; flex-wrap: wrap; align-items: end; margin-bottom: 0.6rem; }
</style>
<h1><span id="svc-dot" class="dot"></span><span id="host">thermal-matrix</span></h1>
<div class="grid">

  <div class="card">
    <h2>Status</h2>
    <div class="row"><span>service</span><span id="svc" class="pill">-</span></div>
    <div class="row"><span>camera</span><span id="cam" class="pill">-</span></div>
    <div class="row"><span>uptime</span><span id="uptime">-</span></div>
    <div class="row"><span>scene (min/mean/max)</span><span id="scene">-</span></div>
    <div class="row"><span>mapped range</span><span id="mapped">-</span></div>
    <button class="secondary" id="restart" style="margin-top:0.75rem">Restart service</button>
    <div class="msg" id="restart-msg"></div>
  </div>

  <div class="card">
    <h2>Live preview (~2 fps)</h2>
    <img id="preview" src="/preview.jpg">
  </div>

  <div class="card" style="grid-column: 1 / -1">
    <h2>Image <span style="text-transform:none; letter-spacing:normal; font-weight:400">-- applies instantly, no restart</span></h2>
    <div id="cfg" class="stack">
      <label>Mode
        <select name="mode" id="mode">
          <option value="auto">Auto-range (scene-relative)</option>
          <option value="bodyheat">Body heat (fixed thresholds)</option>
          <option value="colorwise">Colorwise (fixed, dark cold)</option>
        </select>
      </label>
      <label id="palette-row">Palette / colorscheme
        <select name="palette" id="palette"></select>
      </label>
      <div class="thresholds" id="thresholds">
        <label>Cold max (C)<input type="number" step="0.1" name="cold_max" id="cold_max"></label>
        <label>Hot min (C)<input type="number" step="0.1" name="hot_min" id="hot_min"></label>
        <label>Hot max (C)<input type="number" step="0.1" name="hot_max" id="hot_max"></label>
      </div>
      <label>Fit
        <select name="fit" id="fit">
          <option value="letterbox">No fill - full field of view (letterbox)</option>
          <option value="fill">Fill panel - centre-crop</option>
        </select>
      </label>
      <label>Rotate
        <select name="rotate" id="rotate">
          <option value="0">0</option><option value="90">90</option>
          <option value="180">180</option><option value="270">270</option>
        </select>
      </label>
      <label>Brightness <span id="brightness-val"></span>
        <input type="range" min="10" max="100" name="brightness" id="brightness">
      </label>
      <div class="msg" id="cfg-msg"></div>
    </div>
  </div>

  <div class="card" style="grid-column: 1 / -1">
    <h2>Presets</h2>
    <div class="preset-row">
      <label style="flex:1; min-width:160px">Name
        <input type="text" id="preset-name" placeholder="e.g. night-mode">
      </label>
      <button type="button" id="preset-save">Save current settings</button>
    </div>
    <div class="preset-row">
      <label style="flex:1; min-width:160px">Load preset
        <select id="preset-select"></select>
      </label>
      <button type="button" class="secondary" id="preset-load">Load &amp; apply</button>
      <button type="button" class="secondary" id="preset-load-last">Load last saved</button>
    </div>
    <div class="msg" id="preset-msg"></div>
  </div>

</div>
<script>
const PALETTES = __PALETTES_JSON__;
const paletteSel = document.getElementById("palette");
PALETTES.forEach(p => {
  const o = document.createElement("option");
  o.value = p; o.textContent = p;
  paletteSel.appendChild(o);
});

function setPill(el, ok, onText, offText) {
  el.textContent = ok ? onText : offText;
  el.className = "pill " + (ok ? "ok" : "bad");
}

function markUnreachable() {
  setPill(document.getElementById("svc"), false, "online", "unreachable");
  document.getElementById("svc-dot").className = "dot bad";
  setPill(document.getElementById("cam"), false, "connected", "unreachable");
  document.getElementById("uptime").textContent = "-";
  document.getElementById("scene").textContent = "-";
  document.getElementById("mapped").textContent = "-";
}

async function pollStatus() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3000);
  try {
    const r = await fetch("/api/status", {cache: "no-store", signal: controller.signal});
    if (!r.ok) throw new Error("bad response");
    const s = await r.json();
    document.getElementById("host").textContent = "thermal-matrix - " + s.host;
    const online = s.active === "active";
    setPill(document.getElementById("svc"), online, "online", "offline");
    document.getElementById("svc-dot").className = "dot " + (online ? "ok" : "bad");
    setPill(document.getElementById("cam"), s.camera, "connected", "not detected");
    document.getElementById("uptime").textContent = s.uptime || "-";
    if (s.preview) {
      document.getElementById("scene").textContent =
        `${s.preview.scene_min.toFixed(1)} / ${s.preview.scene_mean.toFixed(1)} / ${s.preview.scene_max.toFixed(1)} C`;
      document.getElementById("mapped").textContent =
        `${s.preview.mapped_lo.toFixed(1)} - ${s.preview.mapped_hi.toFixed(1)} C`;
    } else {
      document.getElementById("scene").textContent = "-";
      document.getElementById("mapped").textContent = "-";
    }
  } catch (e) {
    // dashboard's own server (thermal-status) is down/unreachable, or timed out
    markUnreachable();
  } finally {
    clearTimeout(timer);
  }
}

function refreshPreview() {
  document.getElementById("preview").src = "/preview.jpg?t=" + Date.now();
}

function toggleModeFields() {
  const mode = document.getElementById("mode").value;
  document.getElementById("palette-row").style.display = mode === "auto" ? "" : "none";
  document.getElementById("thresholds").classList.toggle("show", mode !== "auto");
}

async function loadConfig() {
  const r = await fetch("/api/config", {cache: "no-store"});
  const c = await r.json();
  document.getElementById("mode").value = c.mode;
  document.getElementById("palette").value = c.palette;
  document.getElementById("fit").value = c.fit;
  document.getElementById("rotate").value = c.rotate;
  document.getElementById("brightness").value = c.brightness;
  document.getElementById("brightness-val").textContent = c.brightness;
  document.getElementById("cold_max").value = c.cold_max;
  document.getElementById("hot_min").value = c.hot_min;
  document.getElementById("hot_max").value = c.hot_max;
  toggleModeFields();
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function applyLive() {
  const msg = document.getElementById("cfg-msg");
  msg.textContent = "applying...";
  try {
    const r = await fetch("/api/config", {method: "POST", body: JSON.stringify(currentFormConfig())});
    msg.textContent = r.ok ? "live" : "failed to apply";
  } catch (e) {
    msg.textContent = "failed to apply";
  }
}
const applyLiveDebounced = debounce(applyLive, 150);

document.getElementById("mode").addEventListener("change", () => { toggleModeFields(); applyLive(); });
["palette", "fit", "rotate"].forEach(id => {
  document.getElementById(id).addEventListener("change", applyLive);
});
["cold_max", "hot_min", "hot_max"].forEach(id => {
  document.getElementById(id).addEventListener("change", applyLive);
});
document.getElementById("brightness").addEventListener("input", e => {
  document.getElementById("brightness-val").textContent = e.target.value;
  applyLiveDebounced();
});

document.getElementById("restart").addEventListener("click", async () => {
  const msg = document.getElementById("restart-msg");
  msg.textContent = "restarting...";
  await fetch("/api/restart", {method: "POST"});
  setTimeout(() => { msg.textContent = "restarted"; pollStatus(); }, 1500);
});

function currentFormConfig() {
  return {
    mode: document.getElementById("mode").value,
    palette: document.getElementById("palette").value,
    fit: document.getElementById("fit").value,
    rotate: parseInt(document.getElementById("rotate").value),
    brightness: parseInt(document.getElementById("brightness").value),
    cold_max: parseFloat(document.getElementById("cold_max").value),
    hot_min: parseFloat(document.getElementById("hot_min").value),
    hot_max: parseFloat(document.getElementById("hot_max").value),
  };
}

function applyConfigToForm(c) {
  document.getElementById("mode").value = c.mode;
  document.getElementById("palette").value = c.palette;
  document.getElementById("fit").value = c.fit;
  document.getElementById("rotate").value = c.rotate;
  document.getElementById("brightness").value = c.brightness;
  document.getElementById("brightness-val").textContent = c.brightness;
  document.getElementById("cold_max").value = c.cold_max;
  document.getElementById("hot_min").value = c.hot_min;
  document.getElementById("hot_max").value = c.hot_max;
  toggleModeFields();
}

async function refreshPresetList(selectName) {
  const r = await fetch("/api/presets", {cache: "no-store"});
  const data = await r.json();
  const sel = document.getElementById("preset-select");
  sel.innerHTML = "";
  data.names.forEach(n => {
    const o = document.createElement("option");
    o.value = n; o.textContent = n;
    sel.appendChild(o);
  });
  if (selectName) sel.value = selectName;
  return data;
}

async function loadPresetByName(name) {
  const msg = document.getElementById("preset-msg");
  msg.textContent = `loading "${name}"...`;
  const r = await fetch("/api/presets/load", {method: "POST", body: JSON.stringify({name})});
  if (!r.ok) { msg.textContent = `couldn't load "${name}"`; return; }
  const result = await r.json();
  applyConfigToForm(result.config);
  msg.textContent = `loaded "${name}"`;
  pollStatus();
}

document.getElementById("preset-save").addEventListener("click", async () => {
  const name = document.getElementById("preset-name").value.trim();
  const msg = document.getElementById("preset-msg");
  if (!name) { msg.textContent = "give it a name first"; return; }
  const body = { name, ...currentFormConfig() };
  const r = await fetch("/api/presets/save", {method: "POST", body: JSON.stringify(body)});
  if (!r.ok) { msg.textContent = "couldn't save that name"; return; }
  const result = await r.json();
  msg.textContent = `saved as "${result.name}"`;
  refreshPresetList(result.name);
});

document.getElementById("preset-load").addEventListener("click", () => {
  const name = document.getElementById("preset-select").value;
  if (name) loadPresetByName(name);
});

document.getElementById("preset-load-last").addEventListener("click", async () => {
  const data = await refreshPresetList();
  if (!data.last) { document.getElementById("preset-msg").textContent = "nothing saved yet"; return; }
  loadPresetByName(data.last);
});

loadConfig();
pollStatus();
refreshPresetList();
setInterval(pollStatus, 1000);
setInterval(refreshPreview, 500);
</script>
""".replace("__PALETTES_JSON__", json.dumps(PALETTES))


if __name__ == "__main__":
    http.server.HTTPServer((bind_addr(), PORT), Handler).serve_forever()
