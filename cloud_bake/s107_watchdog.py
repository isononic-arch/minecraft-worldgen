#!/usr/bin/env python
"""s107_watchdog.py — zero-token self-healing supervisor for s107_driver.py.
Pure Python: relaunches the driver if its process died while work remains, and
sends ONE PushNotification-style alert file on an unrecoverable failure. Costs
NOTHING (no Claude involvement) — this is the primary autonomy layer.

Loop (every 90s):
  - read s107_status/state.json: if 'completed' or 'failed' -> stop (final alert).
  - read driver.pid: if the PID is dead AND not completed/failed -> relaunch the
    driver detached (it resumes from its phase checkpoint). Bounded: at most
    --max-relaunch (default 5) relaunches, else give up + alert (avoids a crash
    loop billing boxes).
  - hard wall: after --max-hours (default 8) -> stop, sweep boxes defensively,
    alert. Nothing should run longer than the render+zip window.

Alerts are written to s107_status/ALERT.txt (the Claude check-in reads it) and,
if the token file allows, a Hetzner box sweep is triggered on give-up so a dead
driver never leaves boxes billing.
"""
from __future__ import annotations
import json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "s107_status"
STATE = STATUS / "state.json"
PIDF = STATUS / "driver.pid"
PYW = r"C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe"
TOKEN_FILE = Path(r"C:\Users\nicho\.hetzner_token")
API = "https://api.hetzner.cloud/v1"


def log(msg):
    STATUS.mkdir(exist_ok=True)
    with open(STATUS / "watchdog.log", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%m-%d %H:%M:%S}] {msg}\n")


def alert(msg):
    (STATUS / "ALERT.txt").write_text(f"[{datetime.now()}] {msg}\n")
    log(f"ALERT: {msg}")


def pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=20).stdout
        return str(pid) in out
    except Exception:
        return True  # assume alive on probe failure (don't relaunch blindly)


def sweep_boxes(reason):
    if not TOKEN_FILE.exists():
        return
    import urllib.request
    tok = TOKEN_FILE.read_text().strip()
    def req(path, method="GET"):
        r = urllib.request.Request(API + path, method=method,
                                   headers={"Authorization": f"Bearer {tok}"})
        import contextlib
        with contextlib.closing(urllib.request.urlopen(r, timeout=30)) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    try:
        for s in req("/servers?per_page=50").get("servers", []):
            if s["name"].startswith(("vandir-50k-", "v18-")):
                req(f"/servers/{s['id']}", method="DELETE")
                log(f"swept {s['name']} ({reason})")
    except Exception as e:
        log(f"sweep failed: {e}")


def launch_driver():
    subprocess.Popen([PYW, "cloud_bake/s107_driver.py"], cwd=str(ROOT),
                     creationflags=0x00000008)  # DETACHED_PROCESS
    log("relaunched driver (detached) — resumes from checkpoint")


def main():
    max_relaunch = 5
    max_hours = 8
    if "--max-relaunch" in sys.argv:
        max_relaunch = int(sys.argv[sys.argv.index("--max-relaunch") + 1])
    if "--max-hours" in sys.argv:
        max_hours = int(sys.argv[sys.argv.index("--max-hours") + 1])
    t0 = time.time()
    relaunches = 0
    log(f"=== watchdog start (max_relaunch={max_relaunch}, max_hours={max_hours}) ===")
    while True:
        if (time.time() - t0) / 3600 > max_hours:
            alert(f"watchdog wall {max_hours}h reached — sweeping boxes, stopping")
            sweep_boxes("watchdog wall")
            return
        st = json.loads(STATE.read_text()) if STATE.exists() else {}
        if st.get("completed"):
            log("driver COMPLETED — watchdog stopping")
            (STATUS / "WATCHDOG_OK.flag").write_text(str(datetime.now()))
            return
        if st.get("failed"):
            alert(f"driver FAILED: {st['failed']} — boxes swept by driver; "
                  f"world state per finalize/gate logs (S106 intact unless gate opened)")
            return
        pid = int(PIDF.read_text().strip()) if PIDF.exists() else None
        if pid and not pid_alive(pid):
            if relaunches >= max_relaunch:
                alert(f"driver died and relaunch budget ({max_relaunch}) exhausted "
                      f"— sweeping boxes, stopping. Manual takeover needed.")
                sweep_boxes("relaunch budget exhausted")
                return
            relaunches += 1
            log(f"driver PID {pid} dead, work remains — relaunch {relaunches}/{max_relaunch}")
            launch_driver()
            time.sleep(30)  # let it write a new pid
        time.sleep(90)


if __name__ == "__main__":
    main()
