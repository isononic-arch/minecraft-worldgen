#!/usr/bin/env python
"""box_guard.py — Hetzner render-box TTL guard. Guarantees every box dies on
schedule even when the local orchestrator/collection step misfires (S104: one
box's collect check false-negatived and idled until the 3h cap).

Three independent layers (arm all three for real coverage):
  1. LOCAL SWEEPER (this file, `sweep`) — deletes any server older than its TTL.
     DELETION is the only thing that stops Hetzner billing; poweroff still bills.
  2. SCHEDULED TASK (`arm`) — Windows schtasks runs `sweep` every N minutes so
     the guard survives a dead orchestrator, closed terminal, or crashed session.
  3. ON-BOX SELF-DESTRUCT (`selfdestruct`) — nohup poweroff timer on the box
     (stops compute, not billing) and optionally a metadata-service self-DELETE
     (--delete-via-api pushes the token: true billing stop even if this PC is off).

TTL resolution per server (first match wins):
  server label `ttl_min`  >  config `default_ttl_min`  (config: box_guard.json)

Usage:
  py cloud_bake/box_guard.py status
  py cloud_bake/box_guard.py sweep [--dry-run]
  py cloud_bake/box_guard.py set-ttl <name|id|--all> <minutes>
  py cloud_bake/box_guard.py arm [--interval 15]     # install scheduled task
  py cloud_bake/box_guard.py disarm                  # remove scheduled task
  py cloud_bake/box_guard.py armed                   # show task state
  py cloud_bake/box_guard.py selfdestruct <ip> [--minutes 180] [--delete-via-api]

Dispatch-time recipe (put at the top of every render script):
  "$PY" cloud_bake/box_guard.py arm --interval 15
  "$PY" cloud_bake/box_guard.py set-ttl --all <expected_minutes + 30>
"""
from __future__ import annotations
import json, os, sys, subprocess, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "box_guard.json"
LOG = ROOT / "box_guard.log"
API = "https://api.hetzner.cloud/v1"
TOKEN_FILE = Path(r"C:\Users\nicho\.hetzner_token")
TASK_NAME = "VandirBoxGuard"
PYW = Path(sys.executable).with_name("pythonw.exe")

DEFAULTS = {
    # applies to servers WITHOUT a ttl_min label. 300 covers the 3.5-4h mainland
    # render (render_50k_final TTL_MIN=300); island renders should set-ttl tighter.
    "default_ttl_min": 300,
    # sweep only servers whose name matches one of these regexes; ".*" = all
    # (this Hetzner project is single-purpose render boxes). Add patterns to
    # restrict if the project ever hosts anything long-lived.
    "name_patterns": [".*"],
    # never delete servers matching these, regardless of age
    "protected_patterns": [],
    "sweep_interval_min": 15,
}


def cfg() -> dict:
    c = dict(DEFAULTS)
    if CONFIG.exists():
        c.update(json.loads(CONFIG.read_text()))
    return c


def log(msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def token() -> str:
    return TOKEN_FILE.read_text().strip()


def api(path: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        API + path, method=method,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        return {"error": {"code": e.code, "message": e.read().decode(errors="replace")}}


def servers() -> list[dict]:
    out, page = [], 1
    while True:
        d = api(f"/servers?page={page}&per_page=50")
        if "error" in d:
            log(f"!! API error listing servers: {d['error']}")
            return out
        out += d.get("servers", [])
        if not d.get("meta", {}).get("pagination", {}).get("next_page"):
            return out
        page += 1


def age_min(s: dict) -> float:
    created = datetime.fromisoformat(s["created"])
    return (datetime.now(timezone.utc) - created).total_seconds() / 60.0


def ttl_min(s: dict, c: dict) -> float:
    lbl = s.get("labels", {}).get("ttl_min")
    if lbl is not None:
        try:
            return float(lbl)
        except ValueError:
            pass
    return float(c["default_ttl_min"])


def _matches(name: str, patterns: list[str]) -> bool:
    import re
    return any(re.fullmatch(p, name) for p in patterns)


def cmd_status() -> int:
    c = cfg()
    ss = servers()
    if not ss:
        print("0 servers running.")
        return 0
    print(f"{'id':>10} {'name':20} {'status':10} {'age_min':>8} {'ttl_min':>8} {'left':>8}  eligible")
    for s in ss:
        a, t = age_min(s), ttl_min(s, c)
        el = _matches(s["name"], c["name_patterns"]) and not _matches(s["name"], c["protected_patterns"])
        print(f"{s['id']:>10} {s['name']:20} {s['status']:10} {a:8.0f} {t:8.0f} {t - a:8.0f}  {'yes' if el else 'NO (pattern)'}")
    return 0


def cmd_sweep(dry: bool) -> int:
    c = cfg()
    ss = servers()
    if not ss:
        return 0  # quiet no-op — this runs every 15 min from the scheduled task
    killed = 0
    for s in ss:
        a, t = age_min(s), ttl_min(s, c)
        if not _matches(s["name"], c["name_patterns"]) or _matches(s["name"], c["protected_patterns"]):
            log(f"sweep: {s['name']} (id={s['id']}) age={a:.0f}m SKIPPED (pattern)")
            continue
        if a <= t:
            log(f"sweep: {s['name']} (id={s['id']}) age={a:.0f}m ttl={t:.0f}m — ok")
            continue
        if dry:
            log(f"sweep DRY-RUN: would DELETE {s['name']} (id={s['id']}) age={a:.0f}m > ttl={t:.0f}m")
            continue
        r = api(f"/servers/{s['id']}", method="DELETE")
        if "error" in r:
            log(f"sweep: DELETE {s['name']} (id={s['id']}) FAILED: {r['error']}")
        else:
            killed += 1
            log(f"sweep: DELETED {s['name']} (id={s['id']}) age={a:.0f}m > ttl={t:.0f}m")
    return 0 if killed == 0 else 0


def cmd_set_ttl(target: str, minutes: str) -> int:
    c = cfg()
    ss = servers()
    picked = ss if target == "--all" else [
        s for s in ss if str(s["id"]) == target or s["name"] == target]
    if not picked:
        print(f"no server matches '{target}'")
        return 1
    for s in picked:
        labels = dict(s.get("labels", {}))
        labels["ttl_min"] = str(int(minutes))
        r = api(f"/servers/{s['id']}", method="PUT", body={"labels": labels})
        if "error" in r:
            log(f"set-ttl {s['name']}: FAILED {r['error']}")
        else:
            log(f"set-ttl {s['name']} (id={s['id']}) ttl_min={minutes} (age now {age_min(s):.0f}m)")
    return 0


def cmd_arm(interval: int) -> int:
    exe = PYW if PYW.exists() else Path(sys.executable)
    tr = f'"{exe}" "{Path(__file__).resolve()}" sweep'
    r = subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/TR", tr,
         "/SC", "MINUTE", "/MO", str(interval)],
        capture_output=True, text=True)
    if r.returncode != 0:
        log(f"arm FAILED: {r.stderr.strip() or r.stdout.strip()}")
        return 1
    log(f"armed: scheduled task '{TASK_NAME}' sweeps every {interval} min ({tr})")
    return 0


def cmd_disarm() -> int:
    r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
                       capture_output=True, text=True)
    if r.returncode != 0:
        log(f"disarm: {r.stderr.strip() or r.stdout.strip()}")
        return 1
    log(f"disarmed: removed scheduled task '{TASK_NAME}'")
    return 0


def cmd_armed() -> int:
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"NOT armed (no scheduled task '{TASK_NAME}')")
        return 1
    keep = ("TaskName", "Status", "Next Run Time", "Last Run Time", "Last Result", "Task To Run")
    for line in r.stdout.splitlines():
        if any(line.strip().startswith(k) for k in keep):
            print(line.strip())
    return 0


SSHO = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15"]


def cmd_selfdestruct(ip: str, minutes: int, delete_via_api: bool) -> int:
    if delete_via_api:
        # metadata service gives the box its own id; token pushed to /root (0600).
        # True billing stop even if this PC is off. Boxes are ephemeral + private.
        script = (
            f"echo '{token()}' > /root/.hz_tok; chmod 600 /root/.hz_tok; "
            f"nohup sh -c 'sleep {minutes * 60}; "
            f"ID=$(curl -s http://169.254.169.254/hetzner/v1/metadata/instance-id); "
            f"curl -s -X DELETE -H \"Authorization: Bearer $(cat /root/.hz_tok)\" "
            f"{API}/servers/$ID; poweroff -f' >/dev/null 2>&1 &")
        mode = "self-DELETE (billing stops)"
    else:
        script = f"nohup sh -c 'sleep {minutes * 60}; poweroff -f' >/dev/null 2>&1 &"
        mode = "poweroff only (compute stops; billing continues until swept)"
    r = subprocess.run(["ssh", *SSHO, f"root@{ip}", script], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"selfdestruct {ip} FAILED: {r.stderr.strip()}")
        return 1
    log(f"selfdestruct armed on {ip}: +{minutes}m, {mode}")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == "status":
        return cmd_status()
    if cmd == "sweep":
        return cmd_sweep(dry="--dry-run" in rest)
    if cmd == "set-ttl":
        if len(rest) != 2:
            print("usage: set-ttl <name|id|--all> <minutes>")
            return 2
        return cmd_set_ttl(rest[0], rest[1])
    if cmd == "arm":
        iv = int(rest[rest.index("--interval") + 1]) if "--interval" in rest else cfg()["sweep_interval_min"]
        return cmd_arm(iv)
    if cmd == "disarm":
        return cmd_disarm()
    if cmd == "armed":
        return cmd_armed()
    if cmd == "selfdestruct":
        if not rest:
            print("usage: selfdestruct <ip> [--minutes 180] [--delete-via-api]")
            return 2
        mins = int(rest[rest.index("--minutes") + 1]) if "--minutes" in rest else 180
        return cmd_selfdestruct(rest[0], mins, "--delete-via-api" in rest)
    print(f"unknown command: {cmd}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
