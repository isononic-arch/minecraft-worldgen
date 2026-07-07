#!/usr/bin/env python
"""s107_driver.py — detached overnight orchestrator for the S107 full-world
render -> box-direct Bloomhost deploy -> local zip. Launch with Start-Process
pythonw (S105 lesson: Bash-tool background tasks die at 10 min).

Phases (checkpointed in s107_status/state.json; resume-safe — rerunning the
driver skips completed phases):
  mainland_dispatch -> mainland_runspec -> gate_start -> mainland_monitor
  -> mainland_refire? -> islands_dispatch -> islands_monitor -> islands_refire?
  -> finalize -> stitch_audit (non-fatal) -> pull_zip -> sweep

Failure policy (user-set): ONE refire per phase; hard budget €60 (ccx63 ~€1.35/h
upper bound, 8 boxes); any systemic failure (refire >24 mainland rows, missing
gate, budget breach) -> kill all boxes, state=FAILED, exit. VandirWorld_S106 is
only ever deleted by s107_gate.py AFTER a verified render exists.

Heartbeats: every poll tick appends to s107_status/driver.log — the Opus
takeover watcher treats a >25-min-stale driver.log as "driver dead".
"""
from __future__ import annotations
import json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "s107_status"
STATE = STATUS / "state.json"
PY = r"C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe"
BASH = r"C:\Program Files\Git\bin\bash.exe"
TOKEN = Path(r"C:\Users\nicho\.hetzner_token").read_text().strip()
API = "https://api.hetzner.cloud/v1"
BUDGET_EUR = 55.0          # soft ceiling for authorizing refires (hard cap 60)
RATE_EUR_H = 1.35          # ccx63 upper-bound estimate


def log(msg):
    line = f"[{datetime.now():%m-%d %H:%M:%S}] {msg}"
    STATUS.mkdir(exist_ok=True)
    with open(STATUS / "driver.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"phases": {}, "spend_est_eur": 0.0, "started": str(datetime.now())}


def save_state(st):
    STATE.write_text(json.dumps(st, indent=1))


def api_req(path, method="GET"):
    import urllib.request
    req = urllib.request.Request(API + path, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    import contextlib
    with contextlib.closing(urllib.request.urlopen(req, timeout=30)) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def sweep_boxes(reason):
    try:
        d = api_req("/servers?per_page=50")
        n = 0
        for s in d.get("servers", []):
            if s["name"].startswith(("vandir-50k-", "v18-")):
                log(f"SWEEP delete {s['name']} (id={s['id']}) — {reason}")
                api_req(f"/servers/{s['id']}", method="DELETE")
                n += 1
        log(f"sweep done: {n} boxes deleted ({reason})")
    except Exception as e:
        log(f"!! sweep failed: {e} — box_guard TTL sweeper is the net")


def run_phase(name, cmd, timeout_s, cwd=None, log_name=None, env_extra=None):
    """Popen + heartbeat poll loop; stdout/stderr -> s107_status/<log_name>."""
    log(f"PHASE {name}: start — {' '.join(cmd[:4])}... (timeout {timeout_s/60:.0f}m)")
    lf = open(STATUS / (log_name or f"{name}.log"), "a", encoding="utf-8", errors="replace")
    lf.write(f"\n===== {name} @ {datetime.now()} =====\n")
    lf.flush()
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    p = subprocess.Popen(cmd, cwd=str(cwd or ROOT), stdout=lf, stderr=subprocess.STDOUT,
                         env=env)
    t0 = time.time()
    while True:
        rc = p.poll()
        if rc is not None:
            lf.close()
            log(f"PHASE {name}: exit rc={rc} after {(time.time()-t0)/60:.0f}m")
            return rc
        if time.time() - t0 > timeout_s:
            p.kill()
            lf.close()
            log(f"PHASE {name}: TIMEOUT after {timeout_s/60:.0f}m — killed")
            return -999
        log(f"  heartbeat: {name} running ({(time.time()-t0)/60:.0f}m)")
        time.sleep(60)


def fail(st, reason):
    log(f"### FAILED: {reason}")
    st["failed"] = reason
    st["failed_at"] = str(datetime.now())
    save_state(st)
    sweep_boxes("driver FAILED")
    sys.exit(1)


def add_spend(st, boxes, minutes):
    st["spend_est_eur"] += boxes * (minutes / 60.0) * RATE_EUR_H
    log(f"spend estimate now €{st['spend_est_eur']:.1f}")
    save_state(st)


def refire_list(collect_dir):
    ms = Path(collect_dir) / "monitor_state.json"
    if not ms.exists():
        return None
    return json.loads(ms.read_text()).get("refire", [])


def build_mainland_refire_runspec(rows):
    lb = Path(r"D:\render_s107\live_boxes.txt").read_text().split()
    bid, bip = lb[0], lb[1]
    skip = {tuple(map(int, l.split())) for l in
            (ROOT / "cloud_bake" / "mainland_skip_regions_s101.txt").read_text().splitlines() if l.strip()}
    exp = [f"r.{x}.{z}.mca" for z in rows for x in range(97) if (x, z) not in skip]
    spec = {
        "run_name": "vandir-50k", "kind": "mainland",
        "ttl_min": 240, "wall_cap_min": 260,
        "stall_secs": 1800, "poll_secs": 60, "collect_retries": 3, "unreach_grace": 5,
        "collect_dir": "D:/render_s107/_collect_mainland_r",
        "boxes": [{
            "name": "vandir-50k-9", "id": int(bid), "ip": bip,
            "flag": "/root/done", "log": "/root/job.log",
            "collect": "/root/venv/bin/python /root/minecraft-worldgen/cloud_bake/box_collect_s107.py",
            "remote_tar": "/tmp/out.tgz", "verify": "push_manifest",
            "work_units": [f"row_{z}" for z in rows],
            "expected_regions": exp, "min_regions": int(0.98 * len(exp)),
        }],
    }
    p = ROOT / "cloud_bake" / "runspec_mainland_refire.json"
    p.write_text(json.dumps(spec, indent=1))
    return p


ISLAND_KEY_TO_BOX = {  # v18 allocation: key -> 1-based box index
    "-17_622": 1, "11_060": 2, "-50_393": 3, "-1_509": 4, "21_395": 4,
    "13_130": 5, "11_863": 5, "17_288": 6, "49_722": 6,
    "23_887": 7, "18_299": 7, "12_445": 7, "-20_529": 8, "10_941": 8, "-21_008": 8,
}


def main():
    st = load_state()
    ph = st["phases"]

    def done(name):
        return ph.get(name, {}).get("status") in ("done", "skipped")

    def mark(name, status):
        ph[name] = {"status": status, "at": str(datetime.now())}
        save_state(st)

    log(f"=== driver start (resume={STATE.exists()}) ===")

    # -- 1. mainland dispatch ------------------------------------------------
    if not done("mainland_dispatch"):
        rc = run_phase("mainland_dispatch",
                       [BASH, "cloud_bake/render_50k_s107.sh"], 5400)
        if rc != 0:
            fail(st, f"mainland dispatch rc={rc}")
        mark("mainland_dispatch", "done")

    # -- 2. runspec ------------------------------------------------------------
    if not done("mainland_runspec"):
        rc = run_phase("mainland_runspec",
                       [PY, "cloud_bake/make_mainland_runspec.py", "--nboxes", "8",
                        "--s107", "--ttl", "190"], 300)
        spec = json.loads((ROOT / "cloud_bake" / "runspec_mainland.json").read_text())
        if rc != 0 or len(spec["boxes"]) != 8:
            fail(st, f"runspec build rc={rc} boxes={len(spec.get('boxes', []))}")
        mark("mainland_runspec", "done")

    # -- 3. gate (async) -------------------------------------------------------
    if not (STATUS / "gate_done.json").exists():
        gl = open(STATUS / "gate_stdout.log", "a")
        subprocess.Popen([PY, "cloud_bake/s107_gate.py",
                          "cloud_bake/runspec_mainland.json", "--timeout-min", "200"],
                         cwd=str(ROOT), stdout=gl, stderr=subprocess.STDOUT)
        log("gate daemon launched (async)")
    mark("gate_start", "done")

    # -- 4. mainland monitor ----------------------------------------------------
    if not done("mainland_monitor"):
        t0 = time.time()
        rc = run_phase("mainland_monitor",
                       [PY, "cloud_bake/render_monitor.py", "cloud_bake/runspec_mainland.json"],
                       290 * 60, log_name="mainland_monitor.log")
        add_spend(st, 8, (time.time() - t0) / 60 + 20)   # +20m dispatch tail
        if rc != 0:
            fail(st, f"mainland monitor rc={rc}")
        mark("mainland_monitor", "done")

    # -- 5. mainland refire (<=1) ----------------------------------------------
    if not done("mainland_refire"):
        rf = refire_list("D:/render_s107/_collect_mainland") or []
        rows = sorted({int(u.split("_")[1]) for u in rf if u.startswith("row_")})
        if not rows:
            mark("mainland_refire", "skipped")
        else:
            if len(rows) > 24:
                fail(st, f"mainland refire covers {len(rows)} rows — SYSTEMIC, not retrying")
            if not (STATUS / "gate_done.json").exists():
                fail(st, "mainland refire needed but gate never opened — S106 intact, stopping")
            if st["spend_est_eur"] > BUDGET_EUR - 5:
                fail(st, f"budget: €{st['spend_est_eur']:.0f} — refire would breach cap")
            log(f"REFIRE mainland rows: {rows}")
            rc = run_phase("mainland_refire_dispatch",
                           [BASH, "cloud_bake/render_50k_s107.sh"], 3600,
                           env_extra={"ROWS_OVERRIDE": " ".join(map(str, rows)),
                                      "BOX_INDEX": "9", "TTL_MIN": "240"})
            if rc != 0:
                fail(st, f"refire dispatch rc={rc}")
            spec = build_mainland_refire_runspec(rows)
            t0 = time.time()
            rc = run_phase("mainland_refire_monitor",
                           [PY, "cloud_bake/render_monitor.py", str(spec)], 280 * 60)
            add_spend(st, 1, (time.time() - t0) / 60 + 15)
            rf2 = refire_list("D:/render_s107/_collect_mainland_r") or []
            if rc != 0 or rf2:
                fail(st, f"mainland refire failed (rc={rc}, still-missing={rf2})")
            mark("mainland_refire", "done")

    # -- 6. islands dispatch -----------------------------------------------------
    if not done("islands_dispatch"):
        if not (STATUS / "gate_done.json").exists():
            fail(st, "islands dispatch blocked: gate never opened")
        rc = run_phase("islands_dispatch", [BASH, "islands/_cloud_render_v18.sh"], 5400)
        if rc != 0:
            fail(st, f"islands dispatch rc={rc}")
        mark("islands_dispatch", "done")

    # -- 7. islands monitor -------------------------------------------------------
    if not done("islands_monitor"):
        t0 = time.time()
        rc = run_phase("islands_monitor",
                       [PY, "cloud_bake/render_monitor.py", "cloud_bake/runspec_v18.json"],
                       190 * 60, log_name="islands_monitor.log")
        add_spend(st, 8, (time.time() - t0) / 60 + 15)
        if rc != 0:
            fail(st, f"islands monitor rc={rc}")
        mark("islands_monitor", "done")

    # -- 8. islands refire (<=1) ----------------------------------------------------
    if not done("islands_refire"):
        rf = refire_list(str(ROOT / "islands" / "_collect_v18")) or []
        if not rf:
            mark("islands_refire", "skipped")
        else:
            idxs = sorted({ISLAND_KEY_TO_BOX.get(k) for k in rf if ISLAND_KEY_TO_BOX.get(k)})
            if st["spend_est_eur"] > BUDGET_EUR - 5:
                fail(st, f"budget: €{st['spend_est_eur']:.0f} — islands refire would breach cap")
            log(f"REFIRE islands keys={rf} -> boxes {idxs}")
            rc = run_phase("islands_refire_dispatch", [BASH, "islands/_cloud_render_v18.sh"],
                           3600, env_extra={"SUBSET": ",".join(map(str, idxs))})
            if rc != 0:
                fail(st, f"islands refire dispatch rc={rc}")
            # the v18 script rewrote runspec_v18.json for the subset — move it,
            # point its collect at a fresh dir so monitor state starts clean
            spec_p = ROOT / "cloud_bake" / "runspec_v18.json"
            spec = json.loads(spec_p.read_text())
            spec["collect_dir"] = "islands/_collect_v18r"
            rp = ROOT / "cloud_bake" / "runspec_v18_refire.json"
            rp.write_text(json.dumps(spec, indent=1))
            t0 = time.time()
            rc = run_phase("islands_refire_monitor",
                           [PY, "cloud_bake/render_monitor.py", str(rp)], 190 * 60)
            add_spend(st, len(idxs), (time.time() - t0) / 60 + 15)
            rf2 = refire_list(str(ROOT / "islands" / "_collect_v18r")) or []
            if rc != 0 or rf2:
                fail(st, f"islands refire failed (rc={rc}, still-missing={rf2})")
            mark("islands_refire", "done")

    # -- 9. finalize ------------------------------------------------------------------
    if not done("finalize"):
        rc = run_phase("finalize", [PY, "cloud_bake/s107_finalize.py"], 3600)
        if rc != 0:
            fail(st, f"finalize rc={rc} — see s107_status/finalize.log")
        mark("finalize", "done")

    # -- 10. stitch audit (non-fatal) ----------------------------------------------------
    if not done("stitch_audit"):
        rc = run_phase("stitch_audit",
                       [PY, "cloud_bake/stitch_audit_remote.py",
                        "--cfg", r"C:\Users\nicho\.bloom_creds.json",
                        "--world", "VandirWorld_S107"], 7200)
        mark("stitch_audit", "done" if rc == 0 else "nonfatal_fail")
        if rc != 0:
            log("!! stitch audit failed — NON-FATAL, review in the morning")

    # -- 11. pull + zip -------------------------------------------------------------------
    if not done("pull_zip"):
        rc = run_phase("pull_zip", [PY, "cloud_bake/s107_pull_and_zip.py"], 6 * 3600)
        if rc != 0:
            fail(st, f"pull_zip rc={rc}")
        mark("pull_zip", "done")

    # -- 12. final sweep ---------------------------------------------------------------------
    sweep_boxes("final safety sweep")
    st["completed"] = str(datetime.now())
    save_state(st)
    log("=== DRIVER COMPLETE — world on Bloomhost as VandirWorld_S107, "
        "zip at D:\\VandirWorld_S107.zip ===")


if __name__ == "__main__":
    main()
