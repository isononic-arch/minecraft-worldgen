#!/usr/bin/env python
"""stitch_audit_remote.py — the S106 stitch audit executed AGAINST BLOOMHOST
over SFTP byte-range reads (no bulk download): region-header inventory heatmap
+ island-edge surface-Y continuity + island-vs-island merge chunk counts.

Usage:
  py cloud_bake/stitch_audit_remote.py --cfg <sftp_cfg.json> [--world VandirWorld_S106]
                                       [--flag-step 3] [--threads 8]
"""
from __future__ import annotations
import argparse, json, sys, time, threading, queue
from pathlib import Path
import numpy as np
import paramiko

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.topdown_fast import read_chunk, _section_idx  # noqa: E402

AIRS = {"air", "cave_air", "void_air"}


def conn(cfg):
    t = paramiko.Transport((cfg["host"], cfg["port"]),
                           default_window_size=67108864, default_max_packet_size=262144)
    t.connect(username=cfg["user"], password=cfg["pw"])
    s = paramiko.SFTPClient.from_transport(t)
    s.get_channel().settimeout(120)
    return t, s


def header_chunk_count(sftp, path):
    try:
        with sftp.open(path, "rb") as f:
            tbl = f.read(4096)
        if len(tbl) < 4096:
            return 0
        return sum(1 for i in range(0, 4096, 4)
                   if ((tbl[i] << 16) | (tbl[i+1] << 8) | tbl[i+2]) and tbl[i+3])
    except IOError:
        return -1


def _surface_cols(ch, axis, edge):
    secs = ch.get("sections") or []
    parsed = []
    for s in secs:
        r = _section_idx(s)
        if r is None:
            continue
        idx, names = r
        shorts = [n.replace("minecraft:", "") for n in names]
        ia = np.array([x in AIRS for x in shorts], bool)
        parsed.append((int(s.get("Y", 0)), idx, ia))
    parsed.sort(key=lambda t: t[0], reverse=True)
    out = np.full(16, -32768, np.int32)
    found = np.zeros(16, bool)
    for secy, idx, ia in parsed:
        if ia.all():
            continue
        for yy in range(15, -1, -1):
            lane = idx[yy][:, edge] if axis == "x" else idx[yy][edge, :]
            newly = (~found) & (~ia[lane])
            if newly.any():
                out[newly] = secy * 16 + yy
                found |= newly
                if found.all():
                    return out
    return out


def edge_surface_remote(sftp, path, side):
    out = np.full(512, -32768, np.int32)
    try:
        f = sftp.open(path, "rb")
        f.prefetch()
    except IOError:
        return None
    with f:
        for k in range(32):
            if side in ("E", "W"):
                lx, lz = (31, k) if side == "E" else (0, k)
                axis, edge = "x", 15 if side == "E" else 0
            else:
                lx, lz = (k, 31) if side == "S" else (k, 0)
                axis, edge = "z", 15 if side == "S" else 0
            try:
                ch = read_chunk(f, lx, lz)
            except Exception:
                ch = None
            if ch is None:
                continue
            out[k*16:(k+1)*16] = _surface_cols(ch, axis, edge)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--world", default="VandirWorld_S106")
    ap.add_argument("--flag-step", type=int, default=3)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--out", default=str(ROOT / "cloud_bake" / "_stitch_audit_remote"))
    a = ap.parse_args()
    cfg = json.load(open(a.cfg))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rep = open(out / "report.txt", "w", encoding="utf-8")
    def log(m):
        print(m, flush=True); rep.write(m + "\n"); rep.flush()

    own = json.loads((ROOT / "islands" / "region_ownership_s101.json").read_text())
    island_regs = {(rx, rz): isl for isl, regs in own["islands"].items() for rx, rz in regs}

    t, s0 = conn(cfg)
    names = {x.filename: x.st_size for x in s0.listdir_attr(f"{a.world}/region")}
    log(f"[inventory] {len(names)} regions on Bloomhost, "
        f"{sum(names.values())/1e9:.1f} GB, {sum(1 for v in names.values() if v==0)} zero-byte")
    s0.close(); t.close()

    # threaded header scan
    counts = {}
    q = queue.Queue()
    for fn in names:
        q.put(fn)
    lock = threading.Lock()
    def hdr_worker():
        tt, ss = conn(cfg)
        try:
            while True:
                try: fn = q.get_nowait()
                except queue.Empty: return
                c = header_chunk_count(ss, f"{a.world}/region/{fn}")
                with lock:
                    counts[fn] = c
                q.task_done()
        finally:
            ss.close(); tt.close()
    t0 = time.time()
    ths = [threading.Thread(target=hdr_worker, daemon=True) for _ in range(a.threads)]
    for th in ths: th.start()
    for th in ths: th.join()
    log(f"[inventory] header-scanned in {time.time()-t0:.0f}s")
    empty = [f for f, v in counts.items() if v <= 0]
    log(f"[inventory] empty/unreadable: {len(empty)} {empty[:6]}")

    # heatmap
    keys = {}
    for fn in counts:
        _, sx, sz, _ = fn.split(".")
        keys[(int(sx), int(sz))] = counts[fn]
    xs = [k[0] for k in keys]; zs = [k[1] for k in keys]
    x0, x1, z0, z1 = min(xs), max(xs), min(zs), max(zs)
    grid = np.zeros((z1-z0+1, x1-x0+1, 3), np.uint8)
    for (rx, rz), v in keys.items():
        c = (40,44,52) if v <= 0 else ((200,60,60) if v < 200 else
             ((70,160,90) if (rx,rz) in island_regs else (90,110,160)))
        grid[rz-z0, rx-x0] = c
    from PIL import Image
    Image.fromarray(np.repeat(np.repeat(grid, 8, 0), 8, 1)).save(out / "inventory_heatmap.png")
    log(f"[inventory] heatmap saved")
    missing_isl = [k for k in island_regs if f"r.{k[0]}.{k[1]}.mca" not in names]
    log(f"[inventory] island-owned regions missing: {len(missing_isl)} {missing_isl[:6]}")

    # boundary pairs (threaded)
    pairs = []
    for (rx, rz), isl in island_regs.items():
        for nx, nz in ((rx+1,rz),(rx-1,rz),(rx,rz+1),(rx,rz-1)):
            if island_regs.get((nx,nz)) == isl:
                continue
            if f"r.{nx}.{nz}.mca" not in names:
                continue
            pairs.append(((rx,rz),(nx,nz),isl))
    log(f"[boundary] {len(pairs)} island-edge pairs")
    results = []
    pq = queue.Queue()
    for p in pairs:
        pq.put(p)
    def pair_worker():
        tt, ss = conn(cfg)
        try:
            while True:
                try: (rx,rz),(nx,nz),isl = pq.get_nowait()
                except queue.Empty: return
                if nx > rx: sa, sb = "E","W"
                elif nx < rx: sa, sb = "W","E"
                elif nz > rz: sa, sb = "S","N"
                else: sa, sb = "N","S"
                ea = edge_surface_remote(ss, f"{a.world}/region/r.{rx}.{rz}.mca", sa)
                eb = edge_surface_remote(ss, f"{a.world}/region/r.{nx}.{nz}.mca", sb)
                if ea is None or eb is None:
                    with lock: results.append((isl,rx,rz,nx,nz,-1,"UNREADABLE"))
                    pq.task_done(); continue
                both = (ea > -32768) & (eb > -32768)
                if not both.any():
                    with lock: results.append((isl,rx,rz,nx,nz,0,"no shared data"))
                    pq.task_done(); continue
                d = np.abs(ea[both] - eb[both])
                with lock:
                    results.append((isl,rx,rz,nx,nz,int(d.max()),f"ge3={int((d>=3).sum())}"))
                pq.task_done()
        finally:
            ss.close(); tt.close()
    t0 = time.time()
    ths = [threading.Thread(target=pair_worker, daemon=True) for _ in range(a.threads)]
    for th in ths: th.start()
    for th in ths: th.join()
    log(f"[boundary] decoded in {(time.time()-t0)/60:.1f} min")
    flagged = [r for r in results if r[5] >= a.flag_step or r[5] < 0]
    log(f"[boundary] flagged (step >= {a.flag_step} or unreadable): {len(flagged)}")
    for r in sorted(flagged, key=lambda r: -r[5])[:20]:
        log(f"   FLAG {r[0]:32s} r.{r[1]}.{r[2]}|r.{r[3]}.{r[4]} max_step={r[5]} {r[6]}")
    worst = sorted(results, key=lambda r: -r[5])[:8]
    log("[boundary] worst 8 (context):")
    for r in worst:
        log(f"   {r[0]:32s} r.{r[1]}.{r[2]}|r.{r[3]}.{r[4]} max_step={r[5]} {r[6]}")

    for pairname, regs in own.get("island_vs_island", {}).items():
        for rx, rz in regs:
            v = counts.get(f"r.{rx}.{rz}.mca", -1)
            log(f"[merge] {pairname} r.{rx}.{rz}: {v} chunks "
                f"{'OK' if v >= 900 else 'CHECK'}")

    n_high = len(flagged) + len(missing_isl) + len(empty)
    log(f"=== REMOTE STITCH AUDIT {'PASS' if n_high == 0 else f'{n_high} HIGH FINDINGS'} ===")
    rep.close()
    return 1 if n_high else 0


if __name__ == "__main__":
    sys.exit(main())
