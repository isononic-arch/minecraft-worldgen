"""render_seam_test.py — FAST local seam-test harness.

Re-renders a SMALL contiguous block of ADJACENT INTERIOR LAND tiles of ONE island
(spanning an interior tile-to-tile seam, NOT the bbox edge), writes the .mca, and
produces a topdown crop so an interior land seam is visible for A/B before/after a
code fix.

This goes through the EXACT same path the box driver uses
(islands.render_drive._tile_args -> run_pipeline._process_tile with the snapped
world_offset), so any seam it shows is the production seam. It is fully
re-runnable: run it, fix code, run it again with a different --out, diff the PNGs.

Default test target (chosen by analysis 2026-06-27): EFATE (steep volcanic, peak
mcy 275 — the terrain that amplifies the interior relief seam to visible), a 2x2
block of 100%-land interior tiles local (5,7)(6,7)(5,8)(6,8), well away from coast.

Usage (foreground, ~few min):
    py islands/render_seam_test.py
    py islands/render_seam_test.py --name efate --tx0 5 --ty0 7 --nx 2 --ny 2 \
        --workers 1 --out islands/_val/seam_baseline.png
    # after a code fix, A/B:
    py islands/render_seam_test.py --out islands/_val/seam_after.png

Flags:
    --name     island name substring (default efate)              [efate_vanuatu]
    --tx0 --ty0  top-left LOCAL tile coord of the block            [5,7]
    --nx --ny    block size in tiles                               [2,2]
    --workers  process pool size (1-2 to stay under ~7GB RAM)      [1]
    --trees    place schematics (default OFF = fast/low-RAM; the
               terrain seam shows fine without canopy and trees
               cost ~2x RAM/time)
    --clean    delete the block's r.*.mca before rendering (force
               a fresh render; default reuses existing if present
               -- but for an A/B you almost always want --clean)
    --maxpx    topdown longest-edge px                             [1400]
    --out      output PNG  [islands/_val/seam_baseline.png]
    --skip-render  only re-crop the topdown from existing MCAs
"""
from __future__ import annotations
import sys, os, json, time, argparse, struct, zlib, gzip, io
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"

from islands.render_drive import _tile_args, MASKS_OUT, OUT, _safe, _snap, TILE


# --------------------------------------------------------------------------- #
# topdown crop (reuses topdown_fast's fast all-integer chunk scan + palette)
# --------------------------------------------------------------------------- #
from islands.topdown_fast import read_chunk, top_rgb_for_chunk, _apply_hillshade


def _ctypes_peak_ws_mb():
    """Peak working-set of THIS process (MB) via Win32 GetProcessMemoryInfo.
    For workers<=1 the tile runs in-process so this captures the real peak."""
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD),
                        ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        c = PMC(); c.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return c.PeakWorkingSetSize / 1e6
    except Exception:
        pass
    return None


def _peak_rss_mb():
    """Best-effort peak RSS in MB for this process (+ children if psutil present).
    Falls back to a ctypes Win32 peak-working-set of this process (captures the
    in-process tile render when workers<=1)."""
    try:
        import psutil
        me = psutil.Process()
        tot = me.memory_info().rss
        for c in me.children(recursive=True):
            try:
                tot += c.memory_info().rss
            except Exception:
                pass
        return tot / 1e6
    except Exception:
        return _ctypes_peak_ws_mb()


def render_block(entry, mdir, odir, tx0, ty0, nx, ny, workers, trees, clean):
    """Render the [tx0..tx0+nx) x [ty0..ty0+ny) LOCAL-tile block via the SAME
    path the box driver uses. Returns (n_ok, n_fail, elapsed_s, peak_rss_mb)."""
    import run_pipeline
    # fast (no-schem) mode in render_drive relies on ISL/_no_schem.json being ABSENT
    nosch = ISL / "_no_schem.json"
    if not trees and nosch.exists():
        nosch.unlink()

    tiles = [(tx0 + dx, ty0 + dy) for dy in range(ny) for dx in range(nx)]
    odir.mkdir(parents=True, exist_ok=True)

    sox, soz = _snap(entry["world_offset_px"][0]), _snap(entry["world_offset_px"][1])
    print(f"[seam-test] {_safe(entry['name'])}: block local {tiles}", flush=True)
    print(f"[seam-test]   snapped world_offset=({sox},{soz})  trees={trees}  workers={workers}", flush=True)
    region_files = []
    for (tx, ty) in tiles:
        wx0 = sox + tx * TILE; wz0 = soz + ty * TILE
        rx = wx0 // TILE; rz = wz0 // TILE
        region_files.append(odir / f"r.{rx}.{rz}.mca")
        print(f"[seam-test]   local({tx},{ty}) -> world_blocks=({wx0},{wz0}) -> r.{rx}.{rz}.mca", flush=True)

    if clean:
        for rf in region_files:
            if rf.exists():
                rf.unlink()
                print(f"[seam-test]   removed stale {rf.name}", flush=True)

    args = [_tile_args(entry, mdir, odir, tx, ty, fast=not trees) for (tx, ty) in tiles]

    t0 = time.time()
    n_ok = n_fail = 0
    peak = 0.0
    if workers <= 1:
        for a in args:
            try:
                r = run_pipeline._process_tile(a)
                n_ok += 1
                print(f"[seam-test]   tile ({a['tile_x']},{a['tile_y']}) ok "
                      f"{r.get('elapsed_ms')}ms biomes={len(r.get('biomes', []))}", flush=True)
            except Exception as e:
                n_fail += 1
                print(f"[seam-test]   tile ({a['tile_x']},{a['tile_y']}) FAILED: "
                      f"{type(e).__name__}: {e}", flush=True)
            rss = _peak_rss_mb()
            if rss:
                peak = max(peak, rss)
    else:
        from concurrent.futures import ProcessPoolExecutor, as_completed
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(run_pipeline._process_tile, a): (a["tile_x"], a["tile_y"]) for a in args}
            # sample peak RSS while futures run
            pending = set(futs)
            while pending:
                done_now = {f for f in pending if f.done()}
                for f in done_now:
                    try:
                        f.result(); n_ok += 1
                        print(f"[seam-test]   tile {futs[f]} ok", flush=True)
                    except Exception as e:
                        n_fail += 1
                        print(f"[seam-test]   tile {futs[f]} FAILED: {type(e).__name__}: {e}", flush=True)
                pending -= done_now
                rss = _peak_rss_mb()
                if rss:
                    peak = max(peak, rss)
                time.sleep(0.5)
    elapsed = time.time() - t0
    return n_ok, n_fail, elapsed, peak, region_files


def topdown_block(odir, region_files, sox, soz, tx0, ty0, nx, ny, maxpx, out):
    """Crop a topdown of EXACTLY the rendered block's world window so the interior
    seam line(s) are centred and visible. Draws a thin marker at each interior
    tile-seam world-X / world-Z so you know where to look."""
    # world-block window covered by the rendered block
    x0 = sox + tx0 * TILE
    z0 = soz + ty0 * TILE
    x1 = x0 + nx * TILE
    z1 = z0 + ny * TILE
    step = max(1, max(x1 - x0, z1 - z0) // maxpx)
    H = (z1 - z0) // step + 1
    W = (x1 - x0) // step + 1
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (18, 28, 46)
    elev = np.full((H, W), np.nan, np.float32)

    nfiles = 0
    for mca in region_files:
        if not mca.exists():
            continue
        rx, rz = map(int, mca.stem.split(".")[1:3])
        with open(mca, "rb") as f:
            for lz in range(32):
                for lx in range(32):
                    try:
                        ch = read_chunk(f, lx, lz)
                    except Exception:
                        ch = None
                    if ch is None:
                        continue
                    top_rgb, has, top_y = top_rgb_for_chunk(ch)
                    cwx = (rx * 32 + lx) * 16; cwz = (rz * 32 + lz) * 16
                    for zz in range(0, 16, step):
                        for xx in range(0, 16, step):
                            if not has[zz, xx]:
                                continue
                            wx = cwx + xx; wz = cwz + zz
                            if not (x0 <= wx < x1 and z0 <= wz < z1):
                                continue
                            oy = (wz - z0) // step; ox2 = (wx - x0) // step
                            img[oy, ox2] = top_rgb[zz, xx]
                            elev[oy, ox2] = top_y[zz, xx]
        nfiles += 1

    img = _apply_hillshade(img, elev)

    # mark interior seams (magenta dotted) — every 512-block tile boundary inside the block
    seam_xs = [((sox + tx * TILE) - x0) // step for tx in range(tx0 + 1, tx0 + nx)]
    seam_zs = [((soz + ty * TILE) - z0) // step for ty in range(ty0 + 1, ty0 + ny)]
    for sx in seam_xs:
        if 0 <= sx < W:
            img[::4, sx] = (255, 0, 255)
    for sz in seam_zs:
        if 0 <= sz < H:
            img[sz, ::4] = (255, 0, 255)

    from PIL import Image
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out)
    return nfiles, img.shape, seam_xs, seam_zs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="efate")
    ap.add_argument("--tx0", type=int, default=5)
    ap.add_argument("--ty0", type=int, default=7)
    ap.add_argument("--nx", type=int, default=2)
    ap.add_argument("--ny", type=int, default=2)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--trees", action="store_true")
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--maxpx", type=int, default=1400)
    ap.add_argument("--out", default=str(ISL / "_val" / "seam_baseline.png"))
    ap.add_argument("--skip-render", action="store_true")
    a = ap.parse_args()

    layout = json.loads((ISL / "layout.json").read_text())
    entry = next(i for i in layout["islands"]
                 if a.name in _safe(i["name"]) or a.name in i["dem_path"])
    name = _safe(entry["name"])
    mdir = MASKS_OUT / name
    odir = OUT / name
    sox, soz = _snap(entry["world_offset_px"][0]), _snap(entry["world_offset_px"][1])

    t_all = time.time()
    if a.skip_render:
        region_files = []
        tiles = [(a.tx0 + dx, a.ty0 + dy) for dy in range(a.ny) for dx in range(a.nx)]
        for (tx, ty) in tiles:
            rx = (sox + tx * TILE) // TILE; rz = (soz + ty * TILE) // TILE
            region_files.append(odir / f"r.{rx}.{rz}.mca")
        n_ok = n_fail = 0; r_elapsed = 0.0; peak = _peak_rss_mb() or 0.0
    else:
        n_ok, n_fail, r_elapsed, peak, region_files = render_block(
            entry, mdir, odir, a.tx0, a.ty0, a.nx, a.ny, a.workers, a.trees, a.clean or not a.skip_render)

    nfiles, shape, seam_xs, seam_zs = topdown_block(
        odir, region_files, sox, soz, a.tx0, a.ty0, a.nx, a.ny, a.maxpx, a.out)

    print("", flush=True)
    print("=" * 64, flush=True)
    print(f"SEAM-TEST DONE  island={name}", flush=True)
    print(f"  block local tiles: tx {a.tx0}..{a.tx0+a.nx-1}  ty {a.ty0}..{a.ty0+a.ny-1}", flush=True)
    print(f"  world block window: X[{sox+a.tx0*TILE}..{sox+(a.tx0+a.nx)*TILE}) "
          f"Z[{soz+a.ty0*TILE}..{soz+(a.ty0+a.ny)*TILE})", flush=True)
    print(f"  interior vertical seam at world-X = "
          f"{[sox+(a.tx0+i+1)*TILE for i in range(a.nx-1)]}", flush=True)
    print(f"  interior horizontal seam at world-Z = "
          f"{[soz+(a.ty0+i+1)*TILE for i in range(a.ny-1)]}", flush=True)
    print(f"  tiles rendered ok={n_ok} fail={n_fail}  render_time={r_elapsed/60:.2f}m", flush=True)
    print(f"  topdown regions read={nfiles}  png={shape[1]}x{shape[0]}  -> {a.out}", flush=True)
    pk = f"{peak:.0f} MB" if peak else "n/a (psutil missing)"
    print(f"  peak RSS (this proc + children)= {pk}", flush=True)
    print(f"  TOTAL wall {(time.time()-t_all)/60:.2f}m", flush=True)
    print("=" * 64, flush=True)


if __name__ == "__main__":
    main()
