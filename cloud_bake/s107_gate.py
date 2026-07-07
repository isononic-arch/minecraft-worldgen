#!/usr/bin/env python
"""s107_gate.py — home-side gate daemon for the S107 box-direct-to-Bloomhost
render. THE ONLY COMPONENT ALLOWED TO DELETE VandirWorld_S106.

Preconditions enforced before ANY destructive action:
  1. D:/VandirWorld_S107_staging/metadata_saved.flag exists (S106 level.dat,
     datapacks, playerdata etc. are safe locally).
  2. A QUORUM of mainland boxes (default 4, --quorum) each have /root/rendered
     AND >= min_regions .mca on disk AND no crash signature in the log tail.
     Rationale: all boxes run identical code, so N independent successes means
     the pipeline is sound and the rest are just a matter of time / isolated
     flakes (handled by refire). A SYSTEMIC bug crashes the quorum too -> gate
     times out -> S106 is NEVER deleted -> clean abort with the old world intact.
     (There is no local S106 copy; deleting it early on a bad render would strand
     the server. The quorum is the guard against that.)
  3. VandirWorld_S107/.go does not already exist (idempotence: if it does, a
     previous gate run already did the work — exit 0 immediately).

Then: delete VandirWorld_S106 (parallel SFTP, region/ + entities/ + all),
create VandirWorld_S107/region, write VandirWorld_S107/.go. Boxes poll .go and
start pushing (serialized by their own .push_lock mutex).

If NO box verifies within --timeout-min: exit 1 WITHOUT touching the server —
the boxes hit PUSH_GATE_TIMEOUT, the monitor reaps them, and VandirWorld_S106
is still the intact live world. Fail-safe by construction.

Usage:
  py cloud_bake/s107_gate.py cloud_bake/runspec_mainland.json [--timeout-min 200]
Log: s107_status/gate.log   State: s107_status/gate_done.json
"""
from __future__ import annotations
import json, posixpath, re, stat as statmod, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "s107_status"
STAGE = Path(r"D:\VandirWorld_S107_staging")
CREDS = json.loads(Path(r"C:\Users\nicho\.bloom_creds.json").read_text())
SRC_WORLD = "VandirWorld_S106"
DEST_ROOT = "VandirWorld_S107"
CRASH_PATTERNS = [r"Traceback \(most recent call last\)", r"MemoryError",
                  r"No space left", r"\bKilled\b"]
SSHO = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15"]


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    STATUS.mkdir(exist_ok=True)
    with open(STATUS / "gate.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def connect():
    t = paramiko.Transport((CREDS["host"], CREDS["port"]),
                           default_window_size=64 * 1024 * 1024)
    t.connect(username=CREDS["user"], password=CREDS["pw"])
    return t, paramiko.SFTPClient.from_transport(t)


def sftp_exists(s, path):
    try:
        s.stat(path)
        return True
    except IOError:
        return False


def box_verified(box) -> bool:
    """rendered flag + enough regions on disk + no crash in log tail."""
    cmd = ("test -f /root/rendered && echo __REND__; "
           "ls /root/minecraft-worldgen/output/r.*.mca 2>/dev/null | wc -l; "
           "tail -c 3000 /root/job.log 2>/dev/null")
    try:
        r = subprocess.run(["ssh", *SSHO, f"root@{box['ip']}", cmd],
                           capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        return False
    out = r.stdout
    if r.returncode != 0 or "__REND__" not in out:
        return False
    m = re.search(r"__REND__\s*(\d+)", out)
    n = int(m.group(1)) if m else 0
    need = int(box.get("min_regions", 1))
    if n < need:
        log(f"  {box['name']}: rendered flag but only {n}/{need} regions")
        return False
    for pat in CRASH_PATTERNS:
        if re.search(pat, out):
            log(f"  {box['name']}: rendered but CRASH signature in log tail — not trusting")
            return False
    log(f"  {box['name']}: VERIFIED ({n} regions >= {need}, log clean)")
    return True


def collect_remote_files(s, rdir):
    """Recursive (path, is_dir) listing, files first then dirs bottom-up."""
    files, dirs = [], []
    def walk(d):
        for e in s.listdir_attr(d):
            p = posixpath.join(d, e.filename)
            if statmod.S_ISDIR(e.st_mode):
                walk(p)
                dirs.append(p)
            else:
                files.append(p)
    walk(rdir)
    return files, dirs


def delete_s106():
    t0 = time.time()
    t, s = connect()
    log(f"listing {SRC_WORLD} for deletion ...")
    files, dirs = collect_remote_files(s, SRC_WORLD)
    log(f"deleting {len(files)} files + {len(dirs)} dirs with 8 workers")
    t.close()

    def worker(chunk):
        tw, sw = connect()
        n = 0
        for p in chunk:
            try:
                sw.remove(p)
                n += 1
            except IOError as e:
                log(f"  !! remove {p}: {e}")
        tw.close()
        return n

    chunks = [files[i::8] for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as ex:
        done = sum(ex.map(worker, chunks))
    t, s = connect()
    for d in sorted(dirs, key=len, reverse=True):
        try:
            s.rmdir(d)
        except IOError as e:
            log(f"  !! rmdir {d}: {e}")
    try:
        s.rmdir(SRC_WORLD)
    except IOError as e:
        log(f"  !! rmdir {SRC_WORLD}: {e}")
    gone = not sftp_exists(s, SRC_WORLD)
    t.close()
    log(f"S106 delete: {done}/{len(files)} files removed in {time.time()-t0:.0f}s; "
        f"folder gone: {gone}")
    return gone


def main():
    spec_path = Path(sys.argv[1])
    timeout_min = 200
    quorum = 4
    if "--timeout-min" in sys.argv:
        timeout_min = int(sys.argv[sys.argv.index("--timeout-min") + 1])
    if "--quorum" in sys.argv:
        quorum = int(sys.argv[sys.argv.index("--quorum") + 1])
    spec = json.loads(spec_path.read_text())
    boxes = spec["boxes"]
    quorum = min(quorum, len(boxes))
    log(f"=== gate start: {len(boxes)} boxes, quorum {quorum}, timeout {timeout_min}m ===")

    # precondition 1: metadata preserved
    flag = STAGE / "metadata_saved.flag"
    if not flag.exists():
        log("CRITICAL: metadata_saved.flag missing — REFUSING to ever delete S106. Exit.")
        return 1
    log(f"metadata flag OK: {flag.read_text().strip()}")

    # precondition 3: idempotence
    t, s = connect()
    if sftp_exists(s, DEST_ROOT + "/.go"):
        log(".go already present — gate already ran. Exit 0.")
        t.close()
        return 0
    t.close()

    t0 = time.time()
    while (time.time() - t0) / 60 < timeout_min:
        verified = [b["name"] for b in boxes if box_verified(b)]
        if len(verified) >= quorum:
            log(f"GATE TRIGGER: {len(verified)}/{len(boxes)} verified "
                f"(>= quorum {quorum}): {verified} — deleting S106, opening gate")
            if not delete_s106():
                log("CRITICAL: S106 delete incomplete — opening gate anyway "
                    "(leftover files reduce headroom; finalize audits disk)")
            t, s = connect()
            if not sftp_exists(s, DEST_ROOT):
                s.mkdir(DEST_ROOT)
            if not sftp_exists(s, DEST_ROOT + "/region"):
                s.mkdir(DEST_ROOT + "/region")
            with s.open(DEST_ROOT + "/.go", "w") as f:
                f.write(f"opened {datetime.now()} after {len(verified)}/{len(boxes)} verified\n")
            t.close()
            (STATUS / "gate_done.json").write_text(json.dumps(
                {"opened_by": verified, "when": str(datetime.now())}))
            log("=== GATE OPEN (.go written) — boxes may push ===")
            return 0
        log(f"verified {len(verified)}/{len(boxes)} (need {quorum}); "
            f"{(time.time()-t0)/60:.0f}m elapsed; sleeping 60s")
        time.sleep(60)
    log("CRITICAL: gate TIMEOUT — no box verified. S106 UNTOUCHED. Boxes will "
        "PUSH_GATE_TIMEOUT and be reaped by the monitor.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
