#!/usr/bin/env python
"""s107_pull_and_zip.py — after finalize: download VandirWorld_S107 from
Bloomhost (boxes are dead by now — this leg is free) to D:\\VandirWorld_S107,
verify counts+bytes, then zip to D:\\VandirWorld_S107.zip.

12 parallel SFTP connections (Bloomhost throttles single conns to ~4 MB/s).
Deflate level 1 — region chunk data is already zlib-compressed; higher levels
buy ~nothing and cost an hour. ZIP entries are rooted at VandirWorld_S107/ so
unzipping into a saves/ dir yields the world folder directly.

Writes s107_status/ALL_DONE.flag on success. Idempotent (re-pulls only bad files).
"""
from __future__ import annotations
import json, posixpath, stat as statmod, sys, threading, time, zipfile
from datetime import datetime
from pathlib import Path
from queue import Queue

import paramiko

ROOT = Path(__file__).resolve().parent.parent
STATUS = ROOT / "s107_status"
CREDS = json.loads(Path(r"C:\Users\nicho\.bloom_creds.json").read_text())
SRC = "VandirWorld_S107"
DEST = Path(r"D:\VandirWorld_S107")
ZIP = Path(r"D:\VandirWorld_S107.zip")
WORKERS = 12


def log(msg):
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    STATUS.mkdir(exist_ok=True)
    with open(STATUS / "pull_zip.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def connect():
    t = paramiko.Transport((CREDS["host"], CREDS["port"]),
                           default_window_size=64 * 1024 * 1024)
    t.connect(username=CREDS["user"], password=CREDS["pw"])
    s = paramiko.SFTPClient.from_transport(t)
    s.get_channel().settimeout(120)
    return t, s


def remote_listing():
    t, s = connect()
    files = []
    def walk(d, rel=""):
        for e in s.listdir_attr(d):
            p = posixpath.join(d, e.filename)
            r = posixpath.join(rel, e.filename) if rel else e.filename
            if statmod.S_ISDIR(e.st_mode):
                walk(p, r)
            else:
                files.append((p, r, e.st_size))
    walk(SRC)
    t.close()
    return files


def pull(files):
    q = Queue()
    for it in files:
        q.put(it)
    lk = threading.Lock()
    done = [0]
    bad = []
    t0 = time.time()
    got_bytes = [0]
    total = len(files)

    def worker():
        t = s = None
        while True:
            try:
                rp, rel, size = q.get_nowait()
            except Exception:
                break
            lp = DEST / rel.replace("/", "\\")
            lp.parent.mkdir(parents=True, exist_ok=True)
            ok = False
            for att in range(3):
                try:
                    if s is None:
                        t, s = connect()
                    if lp.exists() and lp.stat().st_size == size:
                        ok = True
                        break
                    s.get(rp, str(lp))
                    ok = lp.stat().st_size == size
                    if ok:
                        break
                except Exception as e:
                    with lk:
                        log(f"  {rel} attempt{att+1}: {type(e).__name__}: {e}")
                    try:
                        if t:
                            t.close()
                    except Exception:
                        pass
                    t = s = None
                    time.sleep(2 + att * 3)
            with lk:
                done[0] += 1
                if ok:
                    got_bytes[0] += size
                else:
                    bad.append(rel)
                if done[0] % 500 == 0 or done[0] == total:
                    mbps = got_bytes[0] / 1e6 / max(1, time.time() - t0)
                    log(f"PULLED {done[0]}/{total} ({mbps:.0f} MB/s agg, {len(bad)} bad)")
        try:
            if t:
                t.close()
        except Exception:
            pass

    ths = [threading.Thread(target=worker, daemon=True) for _ in range(WORKERS)]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    return bad


def make_zip(files):
    part = ZIP.with_suffix(".zip.part")
    t0 = time.time()
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for i, (_rp, rel, _size) in enumerate(files):
            lp = DEST / rel.replace("/", "\\")
            zf.write(lp, arcname=f"VandirWorld_S107/{rel}")
            if (i + 1) % 1000 == 0:
                log(f"  zipped {i+1}/{len(files)}")
    if ZIP.exists():
        ZIP.unlink()
    part.rename(ZIP)
    log(f"ZIP done: {ZIP} ({ZIP.stat().st_size/1e9:.1f} GB in {(time.time()-t0)/60:.0f}m)")


def main():
    log("=== pull+zip start ===")
    files = remote_listing()
    tot = sum(sz for _, _, sz in files)
    log(f"remote: {len(files)} files, {tot/1e9:.1f} GB")
    if len(files) < 10000:
        log(f"!! only {len(files)} files remote — expected ~10.6k; refusing (world incomplete?)")
        return 1
    for rnd in range(3):
        bad = pull(files)
        if not bad:
            break
        log(f"re-pull round {rnd+1}: {len(bad)} stragglers")
    if bad:
        log(f"CRITICAL: {len(bad)} files failed after 3 rounds: {bad[:8]}")
        return 1
    # local verification: count + bytes
    lcount = sum(1 for _ in DEST.rglob("*") if _.is_file())
    lbytes = sum(p.stat().st_size for p in DEST.rglob("*") if p.is_file())
    log(f"local: {lcount} files, {lbytes/1e9:.1f} GB (remote {len(files)}/{tot/1e9:.1f})")
    if lcount < len(files) or lbytes != tot:
        log("CRITICAL: local vs remote mismatch")
        return 1
    make_zip(files)
    (STATUS / "ALL_DONE.flag").write_text(json.dumps(
        {"when": str(datetime.now()), "files": len(files), "bytes": tot,
         "zip": str(ZIP), "zip_bytes": ZIP.stat().st_size}))
    log("=== ALL DONE ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
