"""
diag_v12_cache_only.py — Standalone test of _ensure_caches.

Runs the entire 8k cache build WITHOUT touching the per-tile render
pipeline. Catches exceptions, prints memory at each stage, dumps
geomorph statistics. Cheap — ~5-10 minutes vs 30-60 for a full render.

If this passes, the per-tile carve is unlikely to introduce new
failure modes (it just bilinear-samples the cache).
"""
from __future__ import annotations
import os
import sys
import time
import traceback
from pathlib import Path

# Force single-threaded numpy so we can attribute memory to ONE worker
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import hydro_region_overlay as hro


def mem_mb():
    # ctypes fallback for Windows when psutil isn't installed.
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ('cb', wintypes.DWORD),
                ('PageFaultCount', wintypes.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t),
            ]
        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        return -1.0


def main() -> int:
    masks_dir = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
    hr_path = masks_dir / "hydro_region.png"
    if not hr_path.exists():
        print(f"FATAL: hydro_region.png missing at {hr_path}")
        return 2

    print(f"=== v12 cache-only smoke test ===")
    print(f"hr_path:    {hr_path}")
    print(f"start RSS:  {mem_mb():.0f} MB")

    t0 = time.time()
    try:
        hro._ensure_caches(hr_path)
    except Exception as e:
        print(f"!! _ensure_caches raised: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 1
    t_total = time.time() - t0
    print(f"end RSS:    {mem_mb():.0f} MB")
    print(f"elapsed:    {t_total:.1f}s")

    # Inspect what the cache has
    bed = hro._river_bed_8k_cache
    if bed is None:
        print("!! _river_bed_8k_cache is None - global bed precompute failed")
        return 1

    paint_mask = None
    try:
        from PIL import Image as _PILImage
        hr_arr = np.asarray(_PILImage.open(hr_path).convert("L"), dtype=np.uint8)
        paint_mask = (hr_arr == 2)
    except Exception as _e:
        print(f"paint reload failed: {_e}")
        return 1

    # The geomorph applies INSIDE the (widened) footprint. Reconstruct
    # the analogous mask via the carve-depth threshold so we report on
    # the right set of cells.
    sdf = hro._paint_smooth_8k_cache
    if sdf is None:
        print("!! _paint_smooth_8k_cache is None")
        return 1
    blocks_per_px = hro._WORLD_PX / hro._REGION_PX
    sdf_blocks = sdf * blocks_per_px
    t_norm = np.clip(
        (sdf_blocks - (hro._CARVE_INWARD_BIAS - hro._CARVE_SOFTNESS))
        / hro._CARVE_SOFTNESS, 0.0, 1.0)
    carve_depth = hro._CARVE_MAX_DEPTH * t_norm * t_norm * (3.0 - 2.0 * t_norm)
    footprint = carve_depth > 0.1

    print()
    print(f"=== bed cache stats (in {int(footprint.sum())} footprint cells) ===")
    fp_vals = bed[footprint]
    print(f"  count:    {len(fp_vals)}")
    print(f"  min:      {float(fp_vals.min()):.2f}")
    print(f"  max:      {float(fp_vals.max()):.2f}")
    print(f"  mean:     {float(fp_vals.mean()):.2f}")
    print(f"  median:   {float(np.median(fp_vals)):.2f}")
    # If the per-cell geomorph variation is working, std dev should
    # exceed the smoothing baseline by something close to the bias
    # amplitudes (thalweg ±2.5 + bedform ±0.8 + riffle ±1.5 → up to
    # ~5 blocks). If std dev is tiny (<1), geomorph isn't firing.
    print(f"  std:      {float(fp_vals.std()):.2f}  "
          f"(expect a few blocks of variation if geomorph worked)")

    # Cross-section: pick a row through the middle of the painted area
    # and dump bed values
    pys, pxs = np.where(footprint)
    if len(pys) == 0:
        print("!! footprint is empty")
        return 1
    mid_y = int(np.median(pys))
    row = bed[mid_y, :]
    in_row = footprint[mid_y, :]
    if in_row.any():
        cols = np.where(in_row)[0]
        first, last = cols[0], cols[-1]
        sample_cols = np.linspace(first, last, 30).astype(int)
        print(f"\n=== row y={mid_y} cross-section sample ===")
        print(f"  x:    " + " ".join(f"{c:>5}" for c in sample_cols))
        print(f"  bed:  " + " ".join(
            f"{bed[mid_y, c]:>5.1f}" for c in sample_cols))

    print("\nPASS" if t_total < 1200 else "PASS (slow)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
