#!/usr/bin/env python
"""box_push.py — runs ON a render box: push this box's rendered r.*.mca files
directly to the Bloomhost server over parallel SFTP (S106-proven: 16 connections,
30-60 MB/s; single connections are throttled to ~4 MB/s).

Flow (all steps print regularly so render_monitor's stall detector stays fed):
  1. --wait-go: poll for <dest-root>/.go on the SFTP server. The home-side gate
     daemon writes it ONLY after (a) S106 metadata is preserved locally and
     (b) the first box has a verified healthy render and (c) VandirWorld_S106
     has been deleted (disk headroom: S106+S107 cannot coexist). On timeout
     prints PUSH_GATE_TIMEOUT (a monitor crash pattern) and exits 3 — the world
     on the server is still the old one, nothing is lost.
  2. Acquire <dest-root>/.push_lock mutex (atomic SFTP mkdir) so only ONE box
     pushes at a time — 8 boxes x 16 conns would hit shared-host session caps.
     Stale locks (>40 min) are stolen loudly.
  3. Push all matching local regions with 16 worker connections, then a verify
     pass (remote size == local size; re-push mismatches, 3 rounds). Catches the
     S105 zero-byte-file silent-loss failure.
  4. Write /root/push_manifest.json + print PUSH_DONE, release the lock.

Usage (mainland):
  box_push.py --creds /root/bloom_creds.json --src output --min-count 1100 \
              --skip-list cloud_bake/mainland_skip_regions_s101.txt --wait-go
Usage (islands; contested regions are EXCLUDED from push — home merges + uploads
them in finalize from the collect tarballs):
  box_push.py --creds /root/bloom_creds.json --src islands/out --recursive \
              --min-count 800 --exclude r.95.103.mca,r.96.103.mca,... --wait-go
"""
from __future__ import annotations
import argparse, json, re, socket, sys, threading, time
from pathlib import Path
from queue import Queue

import paramiko

RE_MCA = re.compile(r"^r\.(-?\d+)\.(-?\d+)\.mca$")
DEST_ROOT = "VandirWorld_S107"
DEST_REGION = DEST_ROOT + "/region"
WORKERS = 16
LOCK_STALE_S = 40 * 60
VERIFY_ROUNDS = 3


def log(msg):
    print(f"[push +{time.time()-T0:7.0f}s] {msg}", flush=True)


T0 = time.time()


def connect(creds):
    t = paramiko.Transport((creds["host"], creds["port"]),
                           default_window_size=64 * 1024 * 1024)
    t.connect(username=creds["user"], password=creds["pw"])
    s = paramiko.SFTPClient.from_transport(t)
    return t, s


def sftp_exists(s, path):
    try:
        s.stat(path)
        return True
    except IOError:
        return False


def gather_local(src: Path, recursive: bool, skip: set, exclude: set):
    pats = src.glob("*/r.*.mca") if recursive else src.glob("r.*.mca")
    files, dropped = {}, []
    for p in sorted(pats):
        if not RE_MCA.match(p.name):
            continue
        if p.name in skip or p.name in exclude:
            dropped.append(p.name)
            continue
        # recursive (islands): same region name from two islands on one box ->
        # keep the LARGER file (fast_install big-island-wins analogue); the full
        # chunk-merge happens at home from the collect tarball copies.
        if p.name in files and p.stat().st_size <= files[p.name].stat().st_size:
            continue
        files[p.name] = p
    return files, dropped


def wait_go(creds, timeout_s):
    t, s = connect(creds)
    try:
        n = 0
        while time.time() - T0 < timeout_s:
            if sftp_exists(s, DEST_ROOT + "/.go"):
                log("gate OPEN (.go present)")
                return True
            n += 1
            log(f"GATE_WAIT {n} (.go absent)")
            time.sleep(30)
        print("PUSH_GATE_TIMEOUT — gate never opened; aborting without touching server", flush=True)
        return False
    finally:
        t.close()


def acquire_lock(creds, max_wait_s=120 * 60):
    """Mutex via atomic SFTP mkdir of DEST_ROOT/.push_lock. Staleness is judged
    off the LOCK DIR's own mtime (set at mkdir) so a crash between mkdir and the
    owner-file write can't deadlock the whole run (owner-file-based staleness
    would: the stat would IOError forever and nobody could steal)."""
    me = socket.gethostname()
    t, s = connect(creds)
    lockdir = DEST_ROOT + "/.push_lock"
    try:
        n = 0
        while time.time() - T0 < max_wait_s:
            try:
                s.mkdir(lockdir)
                with s.open(lockdir + "/owner", "w") as f:
                    f.write(f"{me} {time.time()}\n")
                log(f"lock ACQUIRED by {me}")
                return True
            except IOError:
                # held — steal if the lock DIR itself is older than the stale TTL
                try:
                    age = time.time() - s.stat(lockdir).st_mtime
                except IOError:
                    age = 0.0
                if age > LOCK_STALE_S:
                    log(f"!! STALE lock (dir age {age:.0f}s) — STEALING")
                    for p in (lockdir + "/owner", lockdir):
                        try:
                            (s.remove if p.endswith("owner") else s.rmdir)(p)
                        except IOError:
                            pass
                    continue
                n += 1
                log(f"LOCK_WAIT {n} (held, dir age {age:.0f}s)")
                time.sleep(20)
        print("PUSH_FATAL — lock never acquired", flush=True)
        return False
    finally:
        t.close()


def release_lock(creds):
    try:
        t, s = connect(creds)
        try:
            s.remove(DEST_ROOT + "/.push_lock/owner")
        except IOError:
            pass
        try:
            s.rmdir(DEST_ROOT + "/.push_lock")
        except IOError:
            pass
        t.close()
        log("lock RELEASED")
    except Exception as e:
        log(f"!! lock release failed: {e} (stale-steal will recover)")


def push_all(creds, files: dict):
    """Push name->Path with WORKERS connections; returns (pushed, failed)."""
    q = Queue()
    for name, p in files.items():
        q.put((name, p))
    pushed, failed = {}, {}
    lk = threading.Lock()
    done_n = [0]
    total = len(files)
    t_start = time.time()
    bytes_done = [0]

    def worker(wid):
        t = s = None
        while True:
            try:
                name, p = q.get_nowait()
            except Exception:
                break
            ok = False
            for attempt in range(3):
                try:
                    if s is None:
                        t, s = connect(creds)
                    s.put(str(p), f"{DEST_REGION}/{name}")
                    ok = True
                    break
                except Exception as e:
                    with lk:
                        log(f"  w{wid} {name} attempt{attempt+1} failed: {type(e).__name__}: {e}")
                    try:
                        if t:
                            t.close()
                    except Exception:
                        pass
                    t = s = None
                    time.sleep(2 + attempt * 3)
            sz = p.stat().st_size
            with lk:
                if ok:
                    pushed[name] = sz
                    bytes_done[0] += sz
                else:
                    failed[name] = sz
                done_n[0] += 1
                if done_n[0] % 50 == 0 or done_n[0] == total:
                    mbps = bytes_done[0] / 1e6 / max(1, time.time() - t_start)
                    log(f"PUSHED {done_n[0]}/{total} ({mbps:.0f} MB/s agg, {len(failed)} failed)")
        try:
            if t:
                t.close()
        except Exception:
            pass

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(WORKERS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return pushed, failed


def verify_remote(creds, files: dict):
    """One listdir of the region dir; return names missing or size-mismatched."""
    t, s = connect(creds)
    try:
        remote = {e.filename: e.st_size for e in s.listdir_attr(DEST_REGION)}
    finally:
        t.close()
    bad = []
    for name, p in files.items():
        if remote.get(name) != p.stat().st_size:
            bad.append(name)
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", default="/root/bloom_creds.json")
    ap.add_argument("--src", required=True)
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--min-count", type=int, required=True)
    ap.add_argument("--skip-list", default="")
    ap.add_argument("--exclude", default="", help="comma-sep region names NOT to push")
    ap.add_argument("--wait-go", action="store_true")
    ap.add_argument("--gate-timeout-min", type=int, default=150)
    ap.add_argument("--manifest", default="/root/push_manifest.json")
    a = ap.parse_args()

    creds = json.loads(Path(a.creds).read_text())
    skip = set()
    if a.skip_list:
        for l in Path(a.skip_list).read_text().splitlines():
            if l.strip():
                x, z = l.split()
                skip.add(f"r.{x}.{z}.mca")
    exclude = {e for e in a.exclude.split(",") if e}

    files, dropped = gather_local(Path(a.src), a.recursive, skip, exclude)
    tot = sum(p.stat().st_size for p in files.values())
    log(f"local regions: {len(files)} ({tot/1e9:.2f} GB), dropped(skip/exclude): {len(dropped)}")
    if len(files) < a.min_count:
        print(f"PUSH_FATAL — only {len(files)} local regions (< {a.min_count}); refusing to push", flush=True)
        return 2

    if a.wait_go and not wait_go(creds, a.gate_timeout_min * 60):
        return 3

    if not acquire_lock(creds):
        return 2
    rc = 1
    try:
        # ensure dest dirs (gate creates them; be defensive)
        t, s = connect(creds)
        for d in (DEST_ROOT, DEST_REGION):
            if not sftp_exists(s, d):
                s.mkdir(d)
        t.close()

        pushed, failed = push_all(creds, files)
        for rnd in range(VERIFY_ROUNDS):
            bad = verify_remote(creds, files)
            if not bad and not failed:
                break
            redo = {n: files[n] for n in set(bad) | set(failed)}
            log(f"verify round {rnd+1}: {len(redo)} bad/missing — re-pushing")
            p2, failed = push_all(creds, redo)
            pushed.update(p2)
        bad = verify_remote(creds, files)
        manifest = {
            "pushed": sorted(pushed.keys()),
            "failed": sorted(set(bad) | set(failed)),
            "excluded": sorted(dropped),
            "total_bytes": tot,
            "elapsed_s": round(time.time() - T0),
            "remote_verified": not bad and not failed,
        }
        Path(a.manifest).write_text(json.dumps(manifest, indent=1))
        if manifest["remote_verified"]:
            log(f"PUSH_DONE {len(pushed)} regions, {tot/1e9:.2f} GB in {manifest['elapsed_s']}s")
            rc = 0
        else:
            print(f"PUSH_FATAL — {len(manifest['failed'])} regions failed verify: "
                  f"{manifest['failed'][:6]}", flush=True)
            rc = 2
    finally:
        release_lock(creds)
    return rc


if __name__ == "__main__":
    sys.exit(main())
