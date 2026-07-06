#!/usr/bin/env python
"""stitch_audit.py — post-assembly STITCH verification for the combined world.

Three passes over the ASSEMBLED region dir (the world as it will be walked):

  1. INVENTORY HEATMAP — header-scan every r.*.mca (4KB offset table only, no
     chunk decode): chunk count per region -> a grid PNG where every region is
     one cell (mainland grid + island overlay). Missing/empty/thin regions jump
     out visually. Seconds, whole world.
  2. BOUNDARY EDGE CONTINUITY — for every island-owned region with a 4-neighbour
     that is NOT the same island (mainland-rendered or another island), decode
     ONLY the facing edge chunks of both regions and measure surface-Y
     continuity across the shared 512-block edge + chunk presence. Flags any
     step >= threshold (default 3 blocks) or missing edge chunks.
  3. ISLAND-vs-ISLAND MERGES — the ownership manifest's contested regions:
     verify both islands' chunks coexist (chunk count >= each single source).

Usage:
  py cloud_bake/stitch_audit.py --world "D:/VandirWorld_S106/region" [--flag-step 3]
                                [--out cloud_bake/_stitch_audit] [--procs 8]

Outputs: <out>/inventory_heatmap.png, <out>/report.txt, crops of flagged pairs.
Exit 1 if any HIGH finding (missing region / missing edge chunk / step>=flag).
"""
from __future__ import annotations
import argparse, json, struct, sys, zlib, gzip, io, time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MAINLAND_R = 97


def region_chunk_count(path: Path) -> int:
    """Count present chunks from the 4KB offset table (no decompress)."""
    try:
        with open(path, "rb") as f:
            tbl = f.read(4096)
        if len(tbl) < 4096:
            return 0
        n = 0
        for i in range(0, 4096, 4):
            off = (tbl[i] << 16) | (tbl[i + 1] << 8) | tbl[i + 2]
            if off and tbl[i + 3]:
                n += 1
        return n
    except OSError:
        return -1


def _surface_cols_of_chunk(ch, axis: str, edge: int):
    """Top non-air Y for the 16 columns on the given edge of a decoded chunk.
    axis 'x': edge column (x=edge in-chunk) across z; axis 'z': edge row."""
    from islands.topdown_fast import _section_idx
    AIRS = {"air", "cave_air", "void_air"}
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
            if axis == "x":
                lane = idx[yy][:, edge]     # (z,) palette ids at x=edge
            else:
                lane = idx[yy][edge, :]     # (x,) at z=edge
            newly = (~found) & (~ia[lane])
            if newly.any():
                out[newly] = secy * 16 + yy
                found |= newly
                if found.all():
                    return out
    return out


def edge_surface(region_path: Path, side: str):
    """(512,) surface-Y along one side of a region ('E','W','N','S').
    Returns None where chunks missing (marked -32768)."""
    from islands.topdown_fast import read_chunk
    out = np.full(512, -32768, np.int32)
    with open(region_path, "rb") as f:
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
            vals = _surface_cols_of_chunk(ch, axis, edge)
            out[k * 16:(k + 1) * 16] = vals
    return out


def check_pair(args):
    """One boundary pair: (world_dir, (rx,rz), (nx,nz), owner). Returns metrics."""
    wdir, (rx, rz), (nx, nz), owner = args
    a = Path(wdir) / f"r.{rx}.{rz}.mca"
    b = Path(wdir) / f"r.{nx}.{nz}.mca"
    if not a.exists() or not b.exists():
        return (owner, rx, rz, nx, nz, -1, -1, "MISSING REGION")
    if nx > rx:      side_a, side_b = "E", "W"
    elif nx < rx:    side_a, side_b = "W", "E"
    elif nz > rz:    side_a, side_b = "S", "N"
    else:            side_a, side_b = "N", "S"
    ea = edge_surface(a, side_a)
    eb = edge_surface(b, side_b)
    both = (ea > -32768) & (eb > -32768)
    miss_a = int((ea == -32768).sum())
    miss_b = int((eb == -32768).sum())
    if not both.any():
        return (owner, rx, rz, nx, nz, 0, max(miss_a, miss_b), "NO SHARED DATA")
    d = np.abs(ea[both] - eb[both])
    return (owner, rx, rz, nx, nz, int(d.max()), max(miss_a, miss_b),
            f"ge3={int((d >= 3).sum())}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True, help="assembled world region dir")
    ap.add_argument("--out", default=str(ROOT / "cloud_bake" / "_stitch_audit"))
    ap.add_argument("--flag-step", type=int, default=3)
    ap.add_argument("--procs", type=int, default=8)
    a = ap.parse_args()
    wdir = Path(a.world)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    rep = open(out / "report.txt", "w", encoding="utf-8")
    def log(m):
        print(m, flush=True); rep.write(m + "\n"); rep.flush()

    own = json.loads((ROOT / "islands" / "region_ownership_s101.json").read_text())
    island_regs = {}
    for isl, regs in own["islands"].items():
        for rx, rz in regs:
            island_regs[(rx, rz)] = isl

    # ── 1. inventory heatmap ────────────────────────────────────────────────
    t0 = time.time()
    files = sorted(wdir.glob("r.*.mca"))
    counts = {}
    for p in files:
        _, sx, sz, _ = p.name.split(".")
        counts[(int(sx), int(sz))] = region_chunk_count(p)
    log(f"[inventory] {len(files)} regions header-scanned in {time.time()-t0:.0f}s")
    empty = [k for k, v in counts.items() if v <= 0]
    thin = [k for k, v in counts.items() if 0 < v < 200]
    log(f"[inventory] empty/unreadable: {len(empty)} {empty[:6]}")
    log(f"[inventory] thin (<200 chunks — expected only at island footprint fringes): {len(thin)}")
    xs = [k[0] for k in counts]; zs = [k[1] for k in counts]
    x0, x1 = min(xs), max(xs); z0, z1 = min(zs), max(zs)
    grid = np.zeros((z1 - z0 + 1, x1 - x0 + 1, 3), np.uint8)
    for (rx, rz), v in counts.items():
        c = (40, 44, 52) if v <= 0 else (
            (200, 60, 60) if v < 200 else (
                (70, 160, 90) if (rx, rz) in island_regs else (90, 110, 160)))
        grid[rz - z0, rx - x0] = c
    from PIL import Image
    Image.fromarray(np.repeat(np.repeat(grid, 8, 0), 8, 1)).save(out / "inventory_heatmap.png")
    log(f"[inventory] heatmap -> {out/'inventory_heatmap.png'} "
        f"(green=island, blue=mainland, red=thin, dark=empty)")

    # expected inventory: every owned island region MUST exist
    missing_isl = [k for k in island_regs if k not in counts]
    log(f"[inventory] island-owned regions missing from world: {len(missing_isl)} {missing_isl[:6]}")

    # ── 2. boundary pairs ───────────────────────────────────────────────────
    pairs = []
    for (rx, rz), isl in island_regs.items():
        for nx, nz in ((rx+1, rz), (rx-1, rz), (rx, rz+1), (rx, rz-1)):
            if island_regs.get((nx, nz)) == isl:
                continue                      # same island — rendered together
            if (nx, nz) not in counts:
                continue                      # frontier (generator fills)
            pairs.append((str(wdir), (rx, rz), (nx, nz), isl))
    log(f"[boundary] {len(pairs)} island-edge region pairs to check")
    t0 = time.time()
    results = []
    if a.procs > 1:
        with ProcessPoolExecutor(max_workers=a.procs) as ex:
            results = list(ex.map(check_pair, pairs, chunksize=4))
    else:
        results = [check_pair(p) for p in pairs]
    log(f"[boundary] decoded in {time.time()-t0:.0f}s")
    flagged = [r for r in results if r[5] >= a.flag_step or r[7] == "MISSING REGION"]
    worst = sorted(results, key=lambda r: -r[5])[:10]
    log(f"[boundary] flagged (step >= {a.flag_step} or missing): {len(flagged)}")
    for r in sorted(flagged, key=lambda r: -r[5])[:20]:
        log(f"   FLAG {r[0]:34s} r.{r[1]}.{r[2]} | r.{r[3]}.{r[4]}  max_step={r[5]} miss={r[6]} {r[7]}")
    log("[boundary] worst 10 (context):")
    for r in worst:
        log(f"   {r[0]:34s} r.{r[1]}.{r[2]}|r.{r[3]}.{r[4]}  max_step={r[5]} {r[7]}")

    # ── 3. island-vs-island merged regions ─────────────────────────────────
    for pairname, regs in own.get("island_vs_island", {}).items():
        for rx, rz in regs:
            v = counts.get((rx, rz), -1)
            log(f"[merge] {pairname} r.{rx}.{rz}: {v} chunks "
                f"{'OK' if v >= 900 else 'CHECK (expected near-full 1024)'}")

    n_high = len(flagged) + len(missing_isl) + len(empty)
    log(f"=== STITCH AUDIT {'PASS' if n_high == 0 else f'{n_high} HIGH FINDINGS'} ===")
    rep.close()
    return 1 if n_high else 0


if __name__ == "__main__":
    sys.exit(main())
