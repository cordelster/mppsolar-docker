#!/usr/bin/env python3
"""
Inverter Manager
Reads /config/inverters/*.conf (or MPP_CONFIG_DIR/inverters/) and maintains
one mpp-solar --daemon process per config file. Supervised by S6.

Send SIGHUP to trigger an immediate config rescan (web UI does this after save).
Per-inverter status is written to /tmp/inverter-status/<name>.json so the
web UI can display running state and uptime without needing s6-svstat.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR   = Path(os.environ.get("MPP_CONFIG_DIR", "/config"))
INVERTER_DIR = CONFIG_DIR / "inverters"
STATUS_DIR   = Path("/tmp/inverter-status")
CHECK_INTERVAL = 10   # seconds between health checks
RESCAN_EVERY   = 6    # rescan configs every CHECK_INTERVAL * RESCAN_EVERY seconds

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s [inverter-manager] %(levelname)s %(message)s",
)
log = logging.getLogger("inverter-manager")

_running = True
_rescan  = True   # trigger scan on first iteration


def _handle_term(signum, _frame):
    global _running
    log.info(f"Signal {signum} — shutting down")
    _running = False


def _handle_hup(_signum, _frame):
    global _rescan
    log.info("SIGHUP — rescanning configs")
    _rescan = True


signal.signal(signal.SIGTERM, _handle_term)
signal.signal(signal.SIGINT,  _handle_term)
signal.signal(signal.SIGHUP,  _handle_hup)


class InverterProcess:
    def __init__(self, name: str, conf: Path):
        self.name     = name
        self.conf     = conf
        self.proc     = None
        self.started  = None
        self.restarts = 0

    @property
    def pidfile(self) -> Path:
        return Path(f"/tmp/mpp-solar-{self.name}.pid")

    def _clean_pidfile(self):
        """Remove stale PID file if the process it references is gone."""
        if self.pidfile.exists():
            try:
                pid = int(self.pidfile.read_text().strip())
                os.kill(pid, 0)   # raises if process doesn't exist
            except (ProcessLookupError, ValueError):
                self.pidfile.unlink(missing_ok=True)

    def start(self):
        self._clean_pidfile()
        env = {**os.environ, "MPP_NO_FORK": "1"}
        log.info(f"[{self.name}] Starting: mpp-solar --daemon -C {self.conf}")
        try:
            self.proc = subprocess.Popen(
                ["mpp-solar", "--daemon", "--pidfile", str(self.pidfile),
                 "-C", str(self.conf)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            self.started = datetime.now(timezone.utc)
            log.info(f"[{self.name}] PID {self.proc.pid}")
        except FileNotFoundError:
            log.error(f"[{self.name}] mpp-solar not found in PATH")
        except Exception as e:
            log.error(f"[{self.name}] Failed to start: {e}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            log.info(f"[{self.name}] Stopping PID {self.proc.pid}")
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.pidfile.unlink(missing_ok=True)

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def check(self):
        """Restart if dead."""
        if not self.is_alive():
            if self.proc is not None:
                self.restarts += 1
                log.warning(f"[{self.name}] Died (exit={self.proc.returncode}), "
                             f"restart #{self.restarts} in 5s...")
                time.sleep(5)
            self.start()

    def write_status(self):
        STATUS_DIR.mkdir(parents=True, exist_ok=True)
        uptime_s = None
        if self.started and self.is_alive():
            uptime_s = int((datetime.now(timezone.utc) - self.started).total_seconds())
        status = {
            "running":  self.is_alive(),
            "pid":      self.proc.pid if self.proc else None,
            "uptime_s": uptime_s,
            "restarts": self.restarts,
        }
        (STATUS_DIR / f"{self.name}.json").write_text(json.dumps(status))

    def clear_status(self):
        (STATUS_DIR / f"{self.name}.json").unlink(missing_ok=True)


def load_configs() -> dict:
    INVERTER_DIR.mkdir(parents=True, exist_ok=True)
    return {p.stem: p for p in sorted(INVERTER_DIR.glob("*.conf"))}


def reconcile(active: dict, desired: dict):
    for name in list(active):
        if name not in desired:
            log.info(f"[{name}] Removed from config — stopping")
            active[name].stop()
            active[name].clear_status()
            del active[name]

    for name, conf in desired.items():
        if name not in active:
            inv = InverterProcess(name, conf)
            inv.start()
            active[name] = inv
        else:
            active[name].conf = conf  # pick up any path change


def main():
    global _rescan
    log.info(f"Inverter Manager starting — watching {INVERTER_DIR}")
    STATUS_DIR.mkdir(parents=True, exist_ok=True)

    # Write PID so web UI can send SIGHUP
    Path("/tmp/inverter-manager.pid").write_text(str(os.getpid()))

    active: dict = {}
    tick = 0

    while _running:
        if _rescan or tick % RESCAN_EVERY == 0:
            _rescan = False
            reconcile(active, load_configs())

        for inv in list(active.values()):
            inv.check()
            inv.write_status()

        tick += 1
        time.sleep(CHECK_INTERVAL)

    log.info("Shutting down all inverters...")
    for inv in active.values():
        inv.stop()
        inv.clear_status()
    Path("/tmp/inverter-manager.pid").unlink(missing_ok=True)
    log.info("Inverter manager stopped.")


if __name__ == "__main__":
    main()
