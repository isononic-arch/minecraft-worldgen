"""
rebuild_centerline.py — Fast rebuild of hydro_centerline.tif

Reads existing masks at 1:8 via rasterio out_shape (no full 50k load),
runs NMS + suppression + braid fill, writes hydro_centerline.tif at 50k.

~5 min total.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
SCALE       = 8
FULL_SIZE   = 50_000
DS_SIZE     = FULL_SIZE // SCALE  # 6250


def read_1_8(name: str, resampling=Resampling.average) -> np.ndarray:
    """Read a 50k TIF downsampled to 6250x6250 via rasterio out_shape."""
    path = MASKS_DIR / f"{name}.tif"
    with rasterio.open(str(path)) as src:
        raw = src.read(1, out_shape=(DS_SIZE, DS_SIZE),
                       resampling=resampling)
    return raw


def main():
    t_total = time.perf_counter()

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    # ── 1. Read masks at 1:8 ─────────────────────────────────────────
    print("Reading masks at 1:8 scale...", flush=True)
    t0 = time.perf_counter()

    order = read_1_8("hydro_order", Resampling.nearest).astype(np.uint8)
    print(f"  hydro_order: {order.shape}, river_px={(order>0).sum()}", flush=True)

    flow_raw = read_1_8("flow", Resampling.average)
    flow = flow_raw.astype(np.float32) / (65535.0 if flow_raw.dtype == np.uint16 else 1.0)
    print(f"  flow: range=[{flow.min():.4f}, {flow.max():.4f}]", flush=True)

    h_raw = read_1_8("height", Resampling.average)
    height = h_raw.astype(np.float32) / (65535.0 if h_raw.dtype == np.uint16 else 1.0)
    print(f"  height: range=[{height.min():.4f}, {height.max():.4f}]", flush=True)

    river_raw = read_1_8("river", Resampling.average)
    river = river_raw.astype(np.float32) / (65535.0 if river_raw.dtype == np.uint16 else 1.0)
    print(f"  river: range=[{river.min():.4f}, {river.max():.4f}]", flush=True)
    print(f"  Read done in {time.perf_counter()-t0:.1f}s", flush=True)

    # ── 2. NMS + suppression + braid fill ────────────────────────────
    print("Running NMS + suppression + braid fill...", flush=True)
    t0 = time.perf_counter()

    from core.hydrology_precompute import nms_centerline
    centerline, braid_fill = nms_centerline(order, flow, height, cfg,
                                            river=river)

    cl_px = centerline.sum()
    bf_px = braid_fill.sum()
    riv_px = (order > 0).sum()
    print(f"  Centerline: {cl_px} px "
          f"({cl_px*100/max(riv_px,1):.0f}% of river), "
          f"braid fill: {bf_px} px  "
          f"in {time.perf_counter()-t0:.1f}s", flush=True)

    # Encode: Strahler order on NMS pixels, 255 on braid fill (solid water)
    # Channels from river.tif that have no Strahler order get order=1
    centerline_order = np.where(centerline, order, np.uint8(0))
    centerline_order[(centerline) & (order == 0)] = np.uint8(1)
    centerline_order[braid_fill] = np.uint8(255)

    # ── 2b. Meander + ocean cutoff + desert wadis ────────────────────
    print("Applying meander + ocean cutoff + desert wadis...", flush=True)
    t0 = time.perf_counter()

    # Read additional masks needed for meander
    override = read_1_8("override", Resampling.nearest).astype(np.uint8)
    lake = read_1_8("hydro_lake", Resampling.nearest)

    from core.hydrology_precompute import meander_rivers
    centerline_order = meander_rivers(
        centerline_order, height, override, order,
        lake=lake, cfg=cfg,
    )
    print(f"  Meander done in {time.perf_counter()-t0:.1f}s", flush=True)

    # Stats
    water_px = ((centerline_order > 0) & (centerline_order != 128)).sum()
    wadi_px = (centerline_order == 128).sum()
    print(f"  Water: {water_px} px, Wadis: {wadi_px} px", flush=True)

    # ── 3. Write at 50k via chunked NEAREST upscale ──────────────────
    print("Writing hydro_centerline.tif at 50k...", flush=True)
    t0 = time.perf_counter()

    from core.hydrology_precompute import write_upscaled
    write_upscaled(centerline_order,
                   MASKS_DIR / "hydro_centerline.tif",
                   "uint8", SCALE, FULL_SIZE,
                   interpolation="nearest")

    print(f"  Write done in {time.perf_counter()-t0:.1f}s", flush=True)

    elapsed = time.perf_counter() - t_total
    print(f"\nTotal: {elapsed:.1f}s", flush=True)


if __name__ == "__main__":
    main()
