#!/usr/bin/env python3
"""
SSH Tunnel Manager
Reads tunnel configs from /config/ssh/tunnels/*.json and maintains one
dbclient (dropbear) tunnel process per config file. Supervised by S6;
restarts individual tunnels if they die.

Tunnel config format (/config/ssh/tunnels/<name>.json):
{
    "host": "192.168.1.100",
    "port": 22,
    "user": "pi",
    "key":  "/config/ssh/id_rsa",
    "forwards": [
        {"local_port": 2200, "remote_host": "localhost", "remote_port": 2400}
    ],
    "enabled": true
}
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

TUNNEL_DIR = Path("/config/ssh/tunnels")
CHECK_INTERVAL = 30  # seconds between config rescans

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [tunnel-manager] %(levelname)s %(message)s",
)
log = logging.getLogger("tunnel-manager")

running = True


def handle_signal(signum, _frame):
    global running
    log.info(f"Received signal {signum}, shutting down...")
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def load_configs():
    configs = {}
    if not TUNNEL_DIR.exists():
        return configs
    for path in sorted(TUNNEL_DIR.glob("*.json")):
        try:
            with open(path) as f:
                cfg = json.load(f)
            if not cfg.get("enabled", True):
                continue
            name = path.stem
            configs[name] = cfg
        except Exception as e:
            log.warning(f"Skipping {path.name}: {e}")
    return configs


def build_dbclient_cmd(cfg):
    key = cfg.get("key", "/config/ssh/id_rsa")
    host = cfg["host"]
    port = cfg.get("port", 22)
    user = cfg.get("user", "root")

    cmd = [
        "dbclient",
        "-i", key,
        "-p", str(port),
        "-N",                   # no remote command, tunnel only
        "-y",                   # auto-accept host key (first connect)
    ]

    for fwd in cfg.get("forwards", []):
        local_port = fwd["local_port"]
        remote_host = fwd.get("remote_host", "localhost")
        remote_port = fwd["remote_port"]
        cmd += ["-L", f"{local_port}:{remote_host}:{remote_port}"]

    cmd.append(f"{user}@{host}")
    return cmd


class TunnelProcess:
    def __init__(self, name, cfg):
        self.name = name
        self.cfg = cfg
        self.proc = None

    def start(self):
        cmd = build_dbclient_cmd(self.cfg)
        log.info(f"[{self.name}] Starting tunnel: {' '.join(cmd)}")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            log.info(f"[{self.name}] Tunnel PID {self.proc.pid}")
        except FileNotFoundError:
            log.error(f"[{self.name}] dbclient not found — is dropbear-client installed?")
        except Exception as e:
            log.error(f"[{self.name}] Failed to start: {e}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            log.info(f"[{self.name}] Stopping tunnel PID {self.proc.pid}")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def is_alive(self):
        return self.proc is not None and self.proc.poll() is None


def reconcile(active: dict, desired: dict):
    """Start missing tunnels, stop removed ones, restart dead ones."""
    # Stop tunnels no longer in config
    for name in list(active.keys()):
        if name not in desired:
            log.info(f"[{name}] Config removed, stopping.")
            active[name].stop()
            del active[name]

    # Start new or restart dead tunnels
    for name, cfg in desired.items():
        if name not in active:
            t = TunnelProcess(name, cfg)
            t.start()
            active[name] = t
        elif not active[name].is_alive():
            log.info(f"[{name}] Tunnel died, restarting...")
            active[name].cfg = cfg
            active[name].start()


def main():
    log.info(f"SSH Tunnel Manager starting. Watching {TUNNEL_DIR}")
    active = {}

    while running:
        desired = load_configs()
        reconcile(active, desired)
        time.sleep(CHECK_INTERVAL)

    log.info("Shutting down all tunnels...")
    for t in active.values():
        t.stop()
    log.info("Tunnel manager stopped.")


if __name__ == "__main__":
    main()
