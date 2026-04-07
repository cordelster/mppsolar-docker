#!/usr/bin/env python3
"""
mpp-solar Web UI
Flask + HTMX configuration interface.
Manages /config/inverters/*.conf and /config/ssh/tunnels/*.json.
Discovers ser2net hosts via mDNS (zeroconf).
"""

import configparser
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from zeroconf import ServiceBrowser, Zeroconf

app = Flask(__name__, template_folder="templates", static_folder="static")

_CFG_BASE   = Path(os.environ.get("MPP_CONFIG_DIR", "/config"))
INVERTER_DIR = _CFG_BASE / "inverters"
TUNNEL_DIR   = _CFG_BASE / "ssh" / "tunnels"
SSH_KEY_PATH = _CFG_BASE / "ssh" / "id_rsa"

PORT = int(os.environ.get("WEB_UI_PORT", 7669))

logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format="%(asctime)s [web-ui] %(levelname)s %(message)s")
log = logging.getLogger("web-ui")


# ---------------------------------------------------------------------------
# mDNS discovery
# ---------------------------------------------------------------------------

_discovered = {}  # name -> {"host": ..., "port": ...}


class Ser2netListener:
    def add_service(self, zc, type_, name):
        info = zc.get_service_info(type_, name)
        if info:
            host = ".".join(str(b) for b in info.addresses[0]) if info.addresses else None
            _discovered[name] = {"host": host, "port": info.port, "name": name}

    def remove_service(self, zc, type_, name):
        _discovered.pop(name, None)

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)


_zeroconf = Zeroconf()
_browser = ServiceBrowser(_zeroconf, "_ser2net._tcp.local.", Ser2netListener())


# ---------------------------------------------------------------------------
# MQTT status monitor
# ---------------------------------------------------------------------------

# Key fields to surface on the health card (hassd_mqtt field name suffixes)
HEALTH_FIELDS = {
    "battery_voltage":          ("Battery",     "V"),
    "battery_capacity":         ("Battery",     "%"),
    "pv_input_power":           ("PV Power",    "W"),
    "pv1_input_power":          ("PV Power",    "W"),
    "ac_output_active_power":   ("Load Power",  "W"),
    "output_load_percent":      ("Load",        "%"),
    "grid_voltage":             ("Grid",        "V"),
    "inverter_heat_sink_temperature": ("Temp", "°C"),
}


class MQTTMonitor:
    """Background thread that subscribes to each inverter's MQTT broker
    and caches the latest values for the health status cards."""

    def __init__(self):
        self._lock = threading.Lock()
        # name -> {"last_seen": datetime|None, "count": int, "values": {field: val},
        #          "connected": bool, "error": str|None}
        self._status: dict = {}
        self._clients: dict = {}   # broker_key -> paho client
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    def _run(self):
        time.sleep(5)  # ensure module is fully loaded before first refresh
        while True:
            try:
                self._refresh()
            except Exception as e:
                log.warning(f"MQTTMonitor refresh error: {e}")
            time.sleep(60)

    def _refresh(self):
        """Re-read all inverter configs and ensure subscriptions are live."""
        wanted: dict = {}   # broker_key -> {client_kwargs, inverters:[name,...]}
        for name in list_inverters():
            cfg = read_inverter_config(name)
            if not cfg.has_section("SETUP"):
                continue
            setup = cfg["SETUP"]
            broker = setup.get("mqtt_broker", "").strip()
            if not broker:
                continue
            port   = int(setup.get("mqtt_port", 1883))
            user   = setup.get("mqtt_user", "").strip() or None
            pw     = setup.get("mqtt_pass", "").strip() or None
            tag    = cfg[name].get("tag", name).strip() if cfg.has_section(name) else name
            key    = f"{broker}:{port}:{user or ''}"
            if key not in wanted:
                wanted[key] = {"broker": broker, "port": port,
                               "user": user, "pw": pw, "inverters": {}}
            wanted[key]["inverters"][name] = tag

        # Start clients for new brokers
        for key, info in wanted.items():
            if key not in self._clients:
                self._start_client(key, info)

        # Stop clients for brokers no longer needed
        for key in list(self._clients):
            if key not in wanted:
                try:
                    self._clients[key].disconnect()
                    self._clients[key].loop_stop()
                except Exception:
                    pass
                del self._clients[key]

    def _start_client(self, key, info):
        inverter_map = info["inverters"]  # name -> tag

        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                client.subscribe("homeassistant/#")
                log.info(f"MQTTMonitor connected to {info['broker']}:{info['port']}")
                for name in inverter_map:
                    with self._lock:
                        s = self._status.setdefault(name, self._blank())
                        s["connected"] = True
                        s["error"] = None
            else:
                for name in inverter_map:
                    with self._lock:
                        self._status.setdefault(name, self._blank())["error"] = f"MQTT rc={rc}"

        def on_disconnect(client, userdata, rc):
            for name in inverter_map:
                with self._lock:
                    self._status.setdefault(name, self._blank())["connected"] = False

        def on_message(client, userdata, msg):
            # Topic: homeassistant/{sensor_type}/mpp_{tag}_{field}/state
            # Both tag and field can contain underscores so we can't split naively.
            # Match by checking if the topic middle component ends with a known field.
            if not msg.topic.endswith("/state"):
                return
            parts = msg.topic.split("/")
            if len(parts) < 3:
                return
            mid = parts[-2]  # e.g. "mpp_solar_battery_voltage"
            if not mid.startswith("mpp_"):
                return

            matched_field = None
            for field in HEALTH_FIELDS:
                if mid.endswith("_" + field):
                    matched_field = field
                    break
            if matched_field is None:
                return

            try:
                val = msg.payload.decode().strip()
            except Exception:
                return

            # Extract the tag portion: strip "mpp_" prefix and "_{field}" suffix
            inner = mid[4:]
            topic_tag = inner[: len(inner) - len(matched_field) - 1].lower()

            for name, cfg_tag in inverter_map.items():
                # hassd_mqtt falls back to command name then "mppsolar" when no tag set
                raw_cfg = read_inverter_config(name)
                candidates = {cfg_tag.replace(" ", "_").lower()}
                if raw_cfg.has_section(name):
                    cmd = raw_cfg[name].get("command", "").split("#")[0].strip()
                    if cmd:
                        candidates.add(cmd.lower())
                candidates.add("mppsolar")

                if topic_tag in candidates:
                    with self._lock:
                        s = self._status.setdefault(name, self._blank())
                        s["last_seen"] = datetime.now(timezone.utc)
                        s["count"] += 1
                        s["values"][matched_field] = val
                    break

        c = mqtt.Client(client_id=f"webui-monitor-{key[:16]}")
        if info["user"]:
            c.username_pw_set(info["user"], info["pw"])
        c.on_connect    = on_connect
        c.on_disconnect = on_disconnect
        c.on_message    = on_message
        try:
            c.connect_async(info["broker"], info["port"], keepalive=60)
            c.loop_start()
            self._clients[key] = c
        except Exception as e:
            log.warning(f"MQTTMonitor could not connect to {info['broker']}: {e}")
            for name in inverter_map:
                with self._lock:
                    self._status.setdefault(name, self._blank())["error"] = str(e)

    @staticmethod
    def _blank():
        return {"last_seen": None, "count": 0, "values": {},
                "connected": False, "error": None}

    def get(self, name: str) -> dict:
        with self._lock:
            return dict(self._status.get(name, self._blank()))

    def invalidate(self):
        """Force a subscription refresh (call after config changes)."""
        t = threading.Thread(target=self._refresh, daemon=True)
        t.start()


_mqtt_monitor = MQTTMonitor()


# ---------------------------------------------------------------------------
# Inverter process status (written by inverter-manager)
# ---------------------------------------------------------------------------

def s6_service_status(name: str) -> dict:
    """Read status written by inverter-manager into /tmp/inverter-status/<name>.json."""
    try:
        data = json.loads(Path(f"/tmp/inverter-status/{name}.json").read_text())
        return {
            "up":       data.get("running", False),
            "pid":      data.get("pid"),
            "uptime_s": data.get("uptime_s"),
        }
    except Exception:
        return {"up": False, "pid": None, "uptime_s": None}


def _hup_inverter_manager():
    """Send SIGHUP to inverter-manager so it rescans configs immediately."""
    try:
        pid = int(Path("/tmp/inverter-manager.pid").read_text().strip())
        os.kill(pid, signal.SIGHUP)
        log.info(f"Sent SIGHUP to inverter-manager (PID {pid})")
    except Exception as e:
        log.warning(f"Could not signal inverter-manager: {e}")


def fmt_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def fmt_ago(ts: datetime | None) -> str:
    if ts is None:
        return "no data"
    delta = int((datetime.now(timezone.utc) - ts).total_seconds())
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def list_inverters():
    INVERTER_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in INVERTER_DIR.glob("*.conf"))


def read_inverter_config(name):
    path = INVERTER_DIR / f"{name}.conf"
    cfg = configparser.ConfigParser()
    cfg.read(path)
    return cfg


def write_inverter_config(name, cfg):
    path = INVERTER_DIR / f"{name}.conf"
    with open(path, "w") as f:
        cfg.write(f)


def list_tunnels():
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    tunnels = {}
    for p in sorted(TUNNEL_DIR.glob("*.json")):
        try:
            tunnels[p.stem] = json.loads(p.read_text())
        except Exception:
            pass
    return tunnels


def reload_inverter_service(name):
    """Signal inverter-manager to rescan configs and pick up changes."""
    _hup_inverter_manager()


def remove_inverter_service(name):
    """Signal inverter-manager to rescan — it will stop the removed inverter."""
    _hup_inverter_manager()


def _restart_ssh_tunnel():
    try:
        subprocess.run(["s6-svc", "-r", "/run/service/ssh-tunnel"], check=True)
    except Exception as e:
        log.warning(f"Failed to restart ssh-tunnel service: {e}")


# ---------------------------------------------------------------------------
# Routes — main pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", inverters=list_inverters())


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/settings")
def settings():
    tunnels = list_tunnels()
    key_present = SSH_KEY_PATH.exists()
    return render_template("settings.html", tunnels=tunnels, key_present=key_present)


# ---------------------------------------------------------------------------
# Routes — inverter management (HTMX partials)
# ---------------------------------------------------------------------------

def _inverter_form(name, cfg, discovered):
    return render_template("inverter_form.html", name=name, cfg=cfg, discovered=discovered)


def _save_inverter(name, form):
    """Build and write config from form data, reload service."""
    cfg = configparser.ConfigParser()

    setup = {
        "pause": form.get("pause", "30"),
        "mqtt_broker": form.get("mqtt_broker", ""),
        "mqtt_port": form.get("mqtt_port", "1883"),
        "mqtt_topic": form.get("mqtt_topic", "mpp-solar"),
    }
    if form.get("mqtt_user"):
        setup["mqtt_user"] = form["mqtt_user"]
    if form.get("mqtt_pass"):
        setup["mqtt_pass"] = form["mqtt_pass"]
    if form.get("mqtt_allowed_cmds"):
        setup["mqtt_allowed_cmds"] = form["mqtt_allowed_cmds"]
    cfg["SETUP"] = setup

    # Inverter name = section name (one and the same)
    # UI uses comma-separated commands; config file uses '#' as delimiter
    commands_raw = form.get("command", "QPIGS")
    commands_cfg = "#".join(c.strip() for c in commands_raw.split(",") if c.strip())
    section = {
        "port":     form.get("port", ""),
        "protocol": form.get("protocol", "PI30"),
        "baud":     form.get("baud", "2400"),
        "command":  commands_cfg,
        "outputs":  form.get("outputs", "mqtt"),
    }
    if form.get("tag"):
        section["tag"] = form["tag"]
    if form.get("dev"):
        section["dev"] = form["dev"]
    if form.get("push_url"):
        section["push_url"] = form["push_url"]
    cfg[name] = section

    INVERTER_DIR.mkdir(parents=True, exist_ok=True)
    write_inverter_config(name, cfg)
    reload_inverter_service(name)
    _mqtt_monitor.invalidate()


def _list_response():
    """Return inverter list + OOB panel-close for HTMX."""
    return (
        render_template("inverter_list.html", inverters=list_inverters())
        + '\n<div id="inverter-panel" hx-swap-oob="innerHTML"></div>'
    )


@app.route("/inverters/<name>/status")
def inverter_status(name):
    try:
        svc   = s6_service_status(name)
        mqtt  = _mqtt_monitor.get(name)
        cfg   = read_inverter_config(name) if name in list_inverters() else None
        pause = int(cfg["SETUP"].get("pause", 30)) if cfg and cfg.has_section("SETUP") else 30
        # Pull only the health fields we care about
        values = {k: v for k, v in mqtt["values"].items() if k in HEALTH_FIELDS}
        return render_template(
            "inverter_status.html",
            name=name, svc=svc, mqtt=mqtt, values=values,
            pause=pause, fmt_uptime=fmt_uptime, fmt_ago=fmt_ago,
            HEALTH_FIELDS=HEALTH_FIELDS,
        )
    except Exception as e:
        log.exception("Status render error for %s: %s", name, e)
        return f'<p class="status-waiting" style="color:#dc3545">Error: {e}</p>'


@app.route("/inverters/new", methods=["GET"])
def inverter_new():
    return _inverter_form("", None, list(_discovered.values()))


@app.route("/inverters/new", methods=["POST"])
def inverter_create():
    name = request.form.get("name", "").strip()
    if not name:
        abort(400)
    _save_inverter(name, request.form)
    return _list_response()


@app.route("/inverters/<name>", methods=["GET"])
def inverter_edit(name):
    if name not in list_inverters():
        abort(404)
    return _inverter_form(name, read_inverter_config(name), list(_discovered.values()))


@app.route("/inverters/<name>", methods=["POST"])
def inverter_save(name):
    _save_inverter(name, request.form)
    return _list_response()


@app.route("/inverters/<name>/delete", methods=["POST"])
def inverter_delete(name):
    conf = INVERTER_DIR / f"{name}.conf"
    if conf.exists():
        conf.unlink()
    remove_inverter_service(name)
    _mqtt_monitor.invalidate()
    return _list_response()


# ---------------------------------------------------------------------------
# Routes — ser2net discovery (HTMX partial)
# ---------------------------------------------------------------------------

@app.route("/discover")
def discover():
    return jsonify(list(_discovered.values()))


# ---------------------------------------------------------------------------
# Routes — SSH tunnel management
# ---------------------------------------------------------------------------

@app.route("/tunnels/new", methods=["GET"])
def tunnel_new():
    return render_template("tunnel_form.html", name="", tunnel=None)


@app.route("/tunnels/<name>", methods=["GET"])
def tunnel_edit(name):
    path = TUNNEL_DIR / f"{name}.json"
    tunnel = json.loads(path.read_text()) if path.exists() else None
    return render_template("tunnel_form.html", name=name, tunnel=tunnel)


@app.route("/tunnels/<name>", methods=["POST"])
def tunnel_save(name):
    form = request.form
    forwards = []
    for lp, rh, rp in zip(
        form.getlist("local_port"),
        form.getlist("remote_host"),
        form.getlist("remote_port"),
    ):
        if lp and rp:
            forwards.append({"local_port": int(lp), "remote_host": rh or "localhost", "remote_port": int(rp)})

    tunnel = {
        "host": form["host"],
        "port": int(form.get("port", 22)),
        "user": form.get("user", "root"),
        "key": form.get("key", str(SSH_KEY_PATH)),
        "forwards": forwards,
        "enabled": "enabled" in form,
    }
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    (TUNNEL_DIR / f"{name}.json").write_text(json.dumps(tunnel, indent=2))
    _restart_ssh_tunnel()
    return render_template("tunnel_list.html", tunnels=list_tunnels())


@app.route("/tunnels/<name>/delete", methods=["POST"])
def tunnel_delete(name):
    path = TUNNEL_DIR / f"{name}.json"
    if path.exists():
        path.unlink()
    _restart_ssh_tunnel()
    return render_template("tunnel_list.html", tunnels=list_tunnels())


@app.route("/tunnels/<name>/toggle", methods=["POST"])
def tunnel_toggle(name):
    path = TUNNEL_DIR / f"{name}.json"
    if path.exists():
        tunnel = json.loads(path.read_text())
        tunnel["enabled"] = not tunnel.get("enabled", True)
        path.write_text(json.dumps(tunnel, indent=2))
        _restart_ssh_tunnel()
    return render_template("tunnel_list.html", tunnels=list_tunnels())


# ---------------------------------------------------------------------------
# Routes — SSH key upload
# ---------------------------------------------------------------------------

@app.route("/settings/ssh-key", methods=["POST"])
def upload_ssh_key():
    key_data = request.form.get("key_data", "").strip()
    if key_data:
        SSH_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        SSH_KEY_PATH.write_text(key_data + "\n")
        SSH_KEY_PATH.chmod(0o600)
    return redirect(url_for("settings"))


if __name__ == "__main__":
    log.info(f"Starting web UI on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
