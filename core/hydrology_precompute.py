"""
hydrology_precompute.py — Vandir Hydrology Engine
===================================================
Global river-network extraction + lake detection at 1:8 downscale (6250×6250).
Runs ONCE as a preprocessing step before tile generation.

Inputs  (from masks/):
    height.tif   — uint16 terrain height (LOW value = HIGH terrain)
    flow.tif     — uint16 flow accumulation
    slope.tif    — uint16 slope
    override.tif — uint8  biome zone codes

Outputs (to masks/):
    hydro_order.tif  — uint8  Strahler stream order at centerline (0 = no river)
    hydro_width.tif  — uint8  channel width in blocks per centerline pixel
    hydro_depth.tif  — uint8  max channel depth in blocks per centerline pixel
    hydro_lake.tif   — uint16 lake ID per pixel (0 = no lake)
    hydro_lkdep.tif  — uint8  lake depth in blocks per pixel

All outputs written at 50k×50k via chunked NEAREST upscale from the 6250×6250
working resolution.  Tile pipeline reads them via rasterio Window() — zero changes
to tile_streamer.py needed.

Usage:
    python core/hydrology_precompute.py [--config config/thresholds.json]
                                        [--masks masks]
                                        [--crop X0 Y0 X1 Y1]   # source-pixel crop for testing
                                        [--dry-run]             # skip writing 50k TIFs
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FULL_SIZE = 50_000
SCALE     = 8
DS_SIZE   = FULL_SIZE // SCALE  # 6250

SEA_LEVEL_RAW_16 = 17050           # raw uint16 value at MC Y=63
SEA_NORM         = SEA_LEVEL_RAW_16 / 65535.0   # ≈ 0.2601

# D8 direction encoding:  index → (row_offset, col_offset)
#   0=N  1=NE  2=E  3=SE  4=S  5=SW  6=W  7=NW
D8_DR = np.array([-1, -1,  0,  1,  1,  1,  0, -1], dtype=np.int8)
D8_DC = np.array([ 0,  1,  1,  1,  0, -1, -1, -1], dtype=np.int8)
D8_DIST = np.array([1.0, 1.414, 1.0, 1.414, 1.0, 1.414, 1.0, 1.414],
                    dtype=np.float32)

# Opposite direction index (for upstream lookup)
D8_OPP = np.array([4, 5, 6, 7, 0, 1, 2, 3], dtype=np.int8)


def _log(msg: str) -> None:
    print(f"[hydro] {msg}", file=sys.stderr, flush=True)


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 — Read masks at 1:8
# ═══════════════════════════════════════════════════════════════════════════

def read_downscaled(
    masks_dir: Path,
    names: list[str],
    ds_h: int = DS_SIZE,
    ds_w: int = DS_SIZE,
    crop: tuple[int, int, int, int] | None = None,
) -> dict[str, np.ndarray]:
    """
    Read mask TIFs at downscaled resolution using rasterio's out_shape.
    Returns float32 [0,1] arrays of shape (ds_h, ds_w).
    If *crop* is given as (x0, y0, x1, y1) in SOURCE pixels, only that
    window is read (and ds_h/ds_w are ignored).
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    result: dict[str, np.ndarray] = {}

    for name in names:
        path = masks_dir / f"{name}.tif"
        if not path.exists():
            _log(f"  WARNING: {path} not found — using zeros")
            result[name] = np.zeros((ds_h, ds_w), dtype=np.float32)
            continue

        with rasterio.open(str(path)) as src:
            if crop is not None:
                x0, y0, x1, y1 = crop
                win = Window(x0, y0, x1 - x0, y1 - y0)
                oh = (y1 - y0) // SCALE
                ow = (x1 - x0) // SCALE
            else:
                win = None
                oh, ow = ds_h, ds_w

            raw = src.read(
                1,
                window=win,
                out_shape=(oh, ow),
                resampling=Resampling.nearest
                if name == "override"
                else Resampling.average,
            )

            # Normalise
            if raw.dtype == np.uint16:
                arr = raw.astype(np.float32) / 65535.0
            elif raw.dtype == np.uint8:
                arr = raw.astype(np.float32) / 255.0
            else:
                arr = raw.astype(np.float32)

            result[name] = arr
            _log(f"  {name:12s}  shape={arr.shape}  "
                 f"range=[{arr.min():.4f}, {arr.max():.4f}]")

    return result


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2 — D8 flow direction from height
# ═══════════════════════════════════════════════════════════════════════════

def compute_d8(height: np.ndarray) -> np.ndarray:
    """
    Compute D8 flow direction for every pixel.

    Returns int8 array (H, W) with values 0-7 (direction index) or -1 (pit/flat).
    Each pixel drains to the neighbor with the steepest downhill slope.
    """
    H, W = height.shape
    best_slope = np.full((H, W), -np.inf, dtype=np.float32)
    best_dir   = np.full((H, W), -1, dtype=np.int8)

    for i in range(8):
        dr, dc = int(D8_DR[i]), int(D8_DC[i])
        dist = D8_DIST[i]

        # Build shifted neighbor array
        neighbor = np.full_like(height, np.nan)

        # Source and destination slices
        src_r = slice(max(0, -dr), H - max(0, dr))
        src_c = slice(max(0, -dc), W - max(0, dc))
        dst_r = slice(max(0, dr),  H - max(0, -dr))
        dst_c = slice(max(0, dc),  W - max(0, -dc))
        neighbor[src_r, src_c] = height[dst_r, dst_c]

        drop = height - neighbor
        slope = drop / dist

        better = slope > best_slope
        # Also skip NaN (border pixels with no neighbor in this direction)
        better &= ~np.isnan(slope)
        best_slope[better] = slope[better]
        best_dir[better] = np.int8(i)

    return best_dir


def resolve_flats(d8: np.ndarray, height: np.ndarray) -> np.ndarray:
    """
    For flat/pit pixels (d8 == -1) that are NOT ocean, route to the
    lowest neighbor even if slightly uphill.  This prevents orphan
    dead-ends in the river network.
    """
    H, W = d8.shape
    pits = (d8 == -1) & (height > SEA_NORM)
    pit_rows, pit_cols = np.where(pits)

    for idx in range(len(pit_rows)):
        r, c = int(pit_rows[idx]), int(pit_cols[idx])
        best_h = np.inf
        best_d = -1
        for i in range(8):
            nr = r + int(D8_DR[i])
            nc = c + int(D8_DC[i])
            if 0 <= nr < H and 0 <= nc < W:
                nh = height[nr, nc]
                if nh < best_h:
                    best_h = nh
                    best_d = i
        if best_d >= 0:
            d8[r, c] = np.int8(best_d)

    return d8


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3 — River network extraction + Strahler ordering
# ═══════════════════════════════════════════════════════════════════════════

def extract_river_mask(flow: np.ndarray, height: np.ndarray,
                       min_flow: float) -> np.ndarray:
    """
    Boolean mask of pixels belonging to the river network.
    Criteria: flow >= min_flow AND above sea level.
    """
    return (flow >= min_flow) & (height > SEA_NORM)


def build_upstream_count(d8: np.ndarray, river_mask: np.ndarray) -> np.ndarray:
    """
    For each river pixel, count how many river-neighbor pixels flow INTO it.
    Used for topological sort (Kahn's algorithm).
    """
    H, W = d8.shape
    in_count = np.zeros((H, W), dtype=np.int32)

    # For each river pixel, find where it flows to.  Increment that
    # destination's in_count.
    rr, rc = np.where(river_mask)
    dirs = d8[rr, rc]
    valid = dirs >= 0
    rr, rc, dirs = rr[valid], rc[valid], dirs[valid]

    dst_r = rr + D8_DR[dirs]
    dst_c = rc + D8_DC[dirs]

    # Clip to bounds
    in_bounds = (dst_r >= 0) & (dst_r < H) & (dst_c >= 0) & (dst_c < W)
    dst_r, dst_c = dst_r[in_bounds], dst_c[in_bounds]
    rr2 = rr[in_bounds]
    rc2 = rc[in_bounds]

    # Only count if destination is also a river pixel
    dst_is_river = river_mask[dst_r, dst_c]
    dst_r, dst_c = dst_r[dst_is_river], dst_c[dst_is_river]

    np.add.at(in_count, (dst_r, dst_c), 1)
    return in_count


def strahler_order(
    d8: np.ndarray,
    river_mask: np.ndarray,
    height: np.ndarray,
) -> np.ndarray:
    """
    Assign Strahler stream order to every river pixel.

    Algorithm:
      1. Find sources (river pixels with 0 upstream river neighbors) → order 1
      2. BFS in topological order (upstream → downstream)
      3. At each pixel: collect orders of upstream tributaries
         - max_order = max of upstream orders
         - count_at_max = number of upstream tributaries with max_order
         - if count_at_max >= 2: order = max_order + 1
         - else: order = max_order

    Returns uint8 array (H, W).  0 = not a river, 1-N = Strahler order.
    """
    H, W = d8.shape
    order = np.zeros((H, W), dtype=np.uint8)

    # Build in-count for topological sort
    in_count = build_upstream_count(d8, river_mask)

    # Find sources: river pixels with in_count == 0
    sources_r, sources_c = np.where(river_mask & (in_count == 0))
    _log(f"  Strahler: {len(sources_r)} source pixels, "
         f"{river_mask.sum()} total river pixels")

    # Initialise sources as order 1
    order[sources_r, sources_c] = 1

    # BFS queue
    queue = deque()
    for i in range(len(sources_r)):
        queue.append((int(sources_r[i]), int(sources_c[i])))

    # Working copy of in_count for topological processing
    remaining = in_count.copy()

    processed = 0
    while queue:
        r, c = queue.popleft()
        processed += 1

        # Find downstream pixel
        d = d8[r, c]
        if d < 0:
            continue
        nr = r + int(D8_DR[d])
        nc = c + int(D8_DC[d])
        if not (0 <= nr < H and 0 <= nc < W):
            continue
        if not river_mask[nr, nc]:
            continue

        # Decrement downstream's remaining in-count
        remaining[nr, nc] -= 1

        # If all upstream tributaries of (nr, nc) are processed, compute its order
        if remaining[nr, nc] <= 0:
            # Collect orders of all upstream river neighbors that flow into (nr, nc)
            max_ord = 0
            count_max = 0
            for i in range(8):
                ur = nr + int(D8_DR[i])
                uc = nc + int(D8_DC[i])
                if not (0 <= ur < H and 0 <= uc < W):
                    continue
                if not river_mask[ur, uc]:
                    continue
                # Does (ur, uc) flow into (nr, nc)?
                ud = d8[ur, uc]
                if ud < 0:
                    continue
                udr = ur + int(D8_DR[ud])
                udc = uc + int(D8_DC[ud])
                if udr == nr and udc == nc:
                    o = int(order[ur, uc])
                    if o > max_ord:
                        max_ord = o
                        count_max = 1
                    elif o == max_ord:
                        count_max += 1

            if count_max >= 2:
                order[nr, nc] = min(max_ord + 1, 255)
            else:
                order[nr, nc] = max(max_ord, 1)

            queue.append((nr, nc))

    _log(f"  Strahler: processed {processed} pixels, "
         f"max order = {order.max()}")
    return order


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3b — Global NMS centerline (seam-free by construction)
# ═══════════════════════════════════════════════════════════════════════════

def nms_centerline(
    order: np.ndarray,   # (H, W) uint8 Strahler order (0 = no river)
    flow:  np.ndarray,   # (H, W) float32 [0,1] flow accumulation
    height: np.ndarray,  # (H, W) float32 [0,1] terrain height
    cfg:   dict,
    river: np.ndarray | None = None,  # (H, W) float32 [0,1] raw Gaea river mask
) -> np.ndarray:
    """
    Thin the river network using Non-Maximum Suppression on the flow
    field, plus slope-aware density suppression.

    Runs at 1:8 scale (6250×6250) on the FULL map — no tile boundaries,
    no seam issues.  The result constrains where 50k per-tile NMS can
    place channels.

    If `river` is provided (from river.tif), channels that exist in
    the raw Gaea river mask but not in hydro_order are included in the
    corridor and braid zone detection.  This captures thin coastal
    tributaries that the Strahler extraction missed at 1:8.

    Returns (centerline_bool, braid_tag_bool) tuple.
    """
    from scipy.ndimage import (binary_dilation, maximum_filter,
                               gaussian_filter, label)

    geo = cfg.get("hydrology_engine", {}).get("river_geometry", {})

    # Configurable 1:8 params (with sensible defaults)
    nms_win    = int(geo.get("global_nms_window", 3))
    nms_frac   = float(geo.get("global_nms_frac", 0.85))
    corr_iter  = int(geo.get("global_corridor_dilation", 1))
    sup_radius = int(geo.get("global_suppress_radius", 3))
    sup_win    = sup_radius * 2 + 1

    steep_frac = float(geo.get("suppress_steep_frac", 0.60))
    mod_frac   = float(geo.get("suppress_moderate_frac", 0.40))
    flat_thr   = float(geo.get("suppress_flat_slope", 1.0))
    flat_frac  = float(geo.get("suppress_flat_frac", 0.75))
    flat_dens  = float(geo.get("suppress_flat_density", 0.06))
    dens_sigma = float(geo.get("global_density_sigma", 2.0))

    blob_min   = int(geo.get("global_blob_min_px", 20))  # at 1:8 scale

    # Threshold for raw river mask to be considered a channel
    river_thr  = float(geo.get("global_river_tif_thr", 0.15))

    H, W = order.shape
    river_mask = order > 0

    # Union with raw Gaea river.tif — captures channels that the
    # Strahler extraction at 1:8 missed (thin coastal tributaries etc.)
    if river is not None:
        river_extra = (river > river_thr) & ~river_mask
        extra_px = river_extra.sum()
        if extra_px > 0:
            river_mask = river_mask | river_extra
            print(f"  river.tif added {extra_px} extra river px "
                  f"to corridor", flush=True)

    _braid_tag = np.zeros((H, W), dtype=bool)

    if not river_mask.any():
        return np.zeros((H, W), dtype=bool), _braid_tag

    # ── 0. Detect braided zones BEFORE NMS ───────────────────────────
    # Key insight: braided zone detection must use the ORIGINAL river
    # mask, not post-NMS centerline.  NMS kills parallel channels,
    # which destroys the density signal that identifies braiding.
    # In braided zones we skip NMS entirely — the outermost channels
    # become natural riverbanks via morphological closing.
    braid_close_r = int(geo.get("braid_close_radius", 10))
    braid_density_thr = float(geo.get("braid_density_thr", 0.015))
    braid_density_sigma = float(geo.get("braid_density_sigma", 6.0))

    braided_zone = np.zeros((H, W), dtype=bool)
    if braid_close_r > 0:
        river_dens = gaussian_filter(
            river_mask.astype(np.float32), sigma=braid_density_sigma)
        braided_zone = river_dens > braid_density_thr
        print(f"  Braided zone: {braided_zone.sum()} px "
              f"(river density sigma={braid_density_sigma}, "
              f"thr={braid_density_thr})", flush=True)

    # ── 1. NMS on flow within corridor — OUTSIDE braided zones only ──
    corridor = binary_dilation(river_mask, iterations=corr_iter) if corr_iter > 0 else river_mask.copy()
    flow_corr = np.where(corridor, flow, 0.0)
    flow_peak = maximum_filter(flow_corr, size=nms_win)
    centerline = (corridor
                  & (flow >= flow_peak * nms_frac)
                  & (flow > 0.001))

    # ── 2. Slope-aware suppression — OUTSIDE braided zones only ──────
    h_smooth = gaussian_filter(height, sigma=2.0)
    gy, gx = np.gradient(h_smooth)
    slope_deg = np.degrees(np.arctan(np.hypot(gx, gy)))

    flow_on_cl = np.where(centerline, flow, 0.0)
    local_max = maximum_filter(flow_on_cl, size=sup_win)
    has_rival = (local_max > flow * 1.01) & (local_max > 0)

    # Only suppress outside braided zones — braids SHOULD have parallels
    non_braid = ~braided_zone
    suppress = np.zeros((H, W), dtype=bool)
    suppress |= (centerline & non_braid & (slope_deg > 5.0) & has_rival
                 & (flow < local_max * steep_frac))
    suppress |= (centerline & non_braid & (slope_deg >= 2.0) & (slope_deg <= 5.0)
                 & has_rival & (flow < local_max * mod_frac))
    flat = slope_deg < flat_thr
    if flat.any():
        cl_density = gaussian_filter(
            centerline.astype(np.float32), sigma=dens_sigma)
        dense_flat = flat & (cl_density > flat_dens)
        suppress |= (centerline & non_braid & dense_flat & has_rival
                     & (flow < local_max * flat_frac))

    centerline &= ~suppress

    # ── 3. Blob removal — small interior fragments ───────────────────
    labeled, n_comps = label(centerline)
    if n_comps > 1:
        comp_sizes = np.bincount(labeled.ravel(), minlength=n_comps + 1)
        small_ids = np.where(comp_sizes < blob_min)[0]
        small_ids = small_ids[small_ids > 0]
        if len(small_ids) > 0:
            remove_mask = np.isin(labeled, small_ids)
            centerline[remove_mask] = False

    # ── 4. Braided floodplain fill ───────────────────────────────────
    # In braided zones: morphological closing on the ORIGINAL river
    # mask fills gaps between parallel channels.  The outermost braids
    # become natural riverbanks; interior is solid water.
    from scipy.ndimage import binary_erosion
    if braid_close_r > 0 and braided_zone.any():
        # Close the original river mask (not the thinned centerline)
        braid_src = river_mask & braided_zone
        closed = binary_dilation(braid_src, iterations=braid_close_r)
        closed = binary_erosion(closed, iterations=braid_close_r)

        # Keep fill only inside the braided zone
        braid_fill = closed & braided_zone & ~centerline

        # ── 4a. Remove straggler spurs ───────────────────────────
        # Small braid fill blobs not connected to the main body are
        # stray fragments being absorbed.  Keep only components that
        # touch a real centerline pixel (the NMS-thinned channel).
        bf_labeled, bf_n = label(braid_fill)
        if bf_n > 1:
            # A braid fill component is valid if it touches a centerline
            # pixel OR a river_mask pixel (which includes channels that
            # NMS thinned away but are still real river).
            # Small components that don't touch any river pixel are noise.
            river_adj = binary_dilation(river_mask, iterations=1)
            comp_sizes = np.bincount(bf_labeled.ravel(),
                                     minlength=bf_n + 1)
            touches_river = np.bincount(
                bf_labeled[river_adj].ravel(), minlength=bf_n + 1) > 0
            # Keep if: touches river AND large enough, OR very large
            braid_min_large = max(blob_min * 10, 200)  # ~200 px at 1:8
            remove_ids = []
            for cid in range(1, bf_n + 1):
                if comp_sizes[cid] < blob_min:
                    remove_ids.append(cid)
                elif not touches_river[cid] and comp_sizes[cid] < braid_min_large:
                    remove_ids.append(cid)
            if remove_ids:
                braid_fill[np.isin(bf_labeled, remove_ids)] = False
                print(f"  Removed {len(remove_ids)} straggler braid "
                      f"components", flush=True)

        braid_px = braid_fill.sum()
        print(f"  Braid fill: {braid_px} px", flush=True)

        centerline = centerline | braid_fill
        _braid_tag[braid_fill] = True

    # ── 5. Boundary smoothing at 1:8 ─────────────────────────────────
    # The boolean mask has blocky edges from morphological operations.
    # Gaussian smooth + re-threshold at 1:8 BEFORE upscale to 50k.
    # This eliminates the 8x8 staircase that NEAREST upscale produces.
    # Category (thin vs braid) is preserved from the pre-smooth mask.
    smooth_sigma = float(geo.get("global_smooth_sigma", 1.5))
    if smooth_sigma > 0:
        pre_smooth_braid = _braid_tag.copy()
        pre_smooth_cl = centerline.copy()

        combined = gaussian_filter(
            centerline.astype(np.float32), sigma=smooth_sigma)
        # Low threshold to keep extent, just soften edges
        centerline = combined > 0.3

        # Anything new from smoothing that was near braid → braid
        new_px = centerline & ~pre_smooth_cl
        braid_adj = binary_dilation(_braid_tag, iterations=2)
        _braid_tag[new_px & braid_adj] = True
        # Anything new near thin channels → thin
        # (handled by default — not in _braid_tag = thin)

    return centerline, _braid_tag


# ═══════════════════════════════════════════════════════════════════════════
# Phase 3b — Skeleton-to-spline meander + ocean cutoff + desert wadis
# ═══════════════════════════════════════════════════════════════════════════

def meander_rivers(
    centerline_order: np.ndarray,  # (H, W) uint8 — 1-5 Strahler, 255 braid, 0 nothing
    height: np.ndarray,            # (H, W) float32 [0,1]
    override: np.ndarray,          # (H, W) uint8 zone codes
    order: np.ndarray,             # (H, W) uint8 Strahler order
    lake: np.ndarray | None = None,  # (H, W) lake IDs (>0 = lake)
    cfg: dict | None = None,
) -> np.ndarray:
    """
    Post-process the centerline mask with:
    1. Ocean cutoff — zero out river pixels below sea level
    2. Skeleton-to-spline meander — replace geometric channels with organic curves
    3. Desert wadi encoding — mark non-preserved desert rivers as value 128

    Works at 1:8 scale (6250x6250).

    Returns updated centerline_order array with:
      0 = no river
      1-5 = Strahler NMS channel (water)
      128 = dry wadi (sand channel, no water)
      255 = braid fill (solid water)
    """
    from scipy.ndimage import (binary_dilation, maximum_filter,
                               gaussian_filter, distance_transform_edt, label)
    from scipy.interpolate import splprep, splev
    from skimage.morphology import skeletonize

    H, W = centerline_order.shape
    sea_norm = SEA_NORM
    is_sea = height < sea_norm
    desert = (override == 170)  # SAND_DUNE_DESERT

    # ── 1. Ocean cutoff ──────────────────────────────────────────────
    ocean_cut = (centerline_order > 0) & is_sea
    ocean_px = ocean_cut.sum()
    centerline_order[ocean_cut] = 0
    _log(f"Ocean cutoff: removed {ocean_px} river px below sea level")

    # ── 2. Build river mask for meander ──────────────────────────────
    river_mask = centerline_order > 0

    # Exclude lake pixels from meander (lakes keep their shape)
    lake_mask = np.zeros_like(river_mask)
    if lake is not None:
        lake_mask = lake > 0

    river_no_lake = river_mask & ~lake_mask
    if not river_no_lake.any():
        _log("No river pixels to meander")
        return centerline_order

    # ── 3. Desert component analysis ─────────────────────────────────
    river_labels, n_rivers = label(river_no_lake)

    # Identify components to preserve as water in desert
    preserve_labels = set()

    # Lake feeders
    if lake is not None and lake_mask.any():
        lake_dilated = binary_dilation(lake_mask & desert, iterations=3)
        feeder_labels = set(np.unique(river_labels[lake_dilated & (river_labels > 0)]))
        preserve_labels |= feeder_labels

    # High-order rivers in desert (Strahler >= 4)
    high_order_desert = desert & (order >= 4) & (river_labels > 0)
    high_labels = set(np.unique(river_labels[high_order_desert]))
    preserve_labels |= high_labels

    # User-specified preserves by tile location
    for lbl in range(1, n_rivers + 1):
        comp = river_labels == lbl
        total = comp.sum()
        if total < 3:
            continue
        in_desert = (comp & desert).sum()
        if in_desert == 0:
            continue
        max_ord = order[comp].max()
        ys, xs = np.where(comp)
        tile_x = int(xs.mean() * SCALE // 512)
        tile_z = int(ys.mean() * SCALE // 512)

        # #495 equiv: order 4, tile ~13,79
        if max_ord >= 4 and 11 <= tile_x <= 15 and 77 <= tile_z <= 82 and total > 500:
            preserve_labels.add(lbl)
        # #104 equiv: order 4, tile ~24,69
        elif max_ord >= 4 and 22 <= tile_x <= 26 and 67 <= tile_z <= 72 and total > 500:
            preserve_labels.add(lbl)
        # #1 equiv: order 3, tile ~19,65
        elif max_ord >= 3 and 17 <= tile_x <= 21 and 63 <= tile_z <= 67 and total > 1000:
            preserve_labels.add(lbl)

    # Build preserve mask and wadi mask
    preserved_mask = np.zeros_like(desert)
    for lbl in preserve_labels:
        preserved_mask |= (river_labels == lbl)

    wadi_source = desert & river_no_lake & ~preserved_mask
    _log(f"Desert: {len(preserve_labels)} preserved components, "
         f"{wadi_source.sum()} wadi px")

    # ── 4. Skeletonize ───────────────────────────────────────────────
    _log("Skeletonizing river mask...")
    t0 = time.time()
    skel = skeletonize(river_no_lake)
    _log(f"  Skeleton: {skel.sum()} px ({time.time()-t0:.1f}s)")

    # ── 5. Trace branches ────────────────────────────────────────────
    _log("Tracing skeleton branches...")
    branches = _trace_skeleton_branches(skel, min_len=3)
    _log(f"  {len(branches)} branches")

    # ── 6. Add meander to each branch ────────────────────────────────
    _log("Adding meander to branches...")
    meandered = []
    spline_data = []  # collect (tck, width_profile, order) per branch for 50k rasterization
    for i, branch in enumerate(branches):
        max_ord = 1
        for pt in branch[::5]:
            y, x = int(pt[0]), int(pt[1])
            if 0 <= y < H and 0 <= x < W:
                max_ord = max(max_ord, order[y, x])

        # 1:8 scale amplitudes
        amp = {1: 1, 2: 2, 3: 3, 4: 4, 5: 6}.get(max_ord, 2)
        wl = {1: 6, 2: 9, 3: 12, 4: 18, 5: 22}.get(max_ord, 10)

        m, tck = _add_meander(branch, amplitude=amp, wavelength=wl,
                              seed=i * 7, return_tck=True)
        meandered.append(m)
        spline_data.append({
            "tck": tck,       # spline params (or None for short branches)
            "order": int(max_ord),
            "branch_idx": i,
        })

    # ── 7. Rebuild channel mask from splines ─────────────────────────
    _log("Rebuilding channel mask from splines...")
    t0 = time.time()
    original_dist = distance_transform_edt(river_no_lake).astype(np.float32)

    rng = np.random.RandomState(123)
    width_noise = gaussian_filter(rng.randn(H, W).astype(np.float32), sigma=4)
    wn_min, wn_max = width_noise.min(), width_noise.max()
    if wn_max > wn_min:
        width_noise = (width_noise - wn_min) / (wn_max - wn_min)
    width_noise = 0.7 + width_noise * 0.6  # [0.7, 1.3]

    new_river = np.zeros((H, W), dtype=bool)
    new_braid = np.zeros((H, W), dtype=bool)  # track which pixels were braid

    for bi, (orig_branch, mean_branch) in enumerate(zip(branches, meandered)):
        n_orig = len(orig_branch)
        n_mean = len(mean_branch)
        # Collect width samples for this branch (at 1:8 scale)
        branch_widths = []

        for j in range(n_mean):
            orig_idx = min(int(j * n_orig / n_mean), n_orig - 1)
            oy = max(0, min(int(orig_branch[orig_idx, 0]), H - 1))
            ox = max(0, min(int(orig_branch[orig_idx, 1]), W - 1))

            orig_radius = max(original_dist[oy, ox], 1.0)

            my = max(0, min(int(round(mean_branch[j, 0])), H - 1))
            mx = max(0, min(int(round(mean_branch[j, 1])), W - 1))
            local_r = int(orig_radius * width_noise[my, mx])
            local_r = max(1, local_r)
            branch_widths.append(float(local_r))

            y0 = max(0, my - local_r)
            y1 = min(H, my + local_r + 1)
            x0 = max(0, mx - local_r)
            x1 = min(W, mx + local_r + 1)
            yy, xx = np.ogrid[y0 - my:y1 - my, x0 - mx:x1 - mx]
            circle = (yy**2 + xx**2) <= local_r**2
            new_river[y0:y1, x0:x1] |= circle

            # Track braid origin
            was_braid = centerline_order[oy, ox] == 255
            if was_braid:
                new_braid[y0:y1, x0:x1] |= circle

        # Store width profile for 50k re-rasterization
        if bi < len(spline_data):
            spline_data[bi]["widths_18"] = branch_widths

    # Remove ocean + lake overlap
    new_river &= ~is_sea
    new_river &= ~lake_mask

    _log(f"  Rebuild done ({time.time()-t0:.1f}s), "
         f"new={new_river.sum()} px (was {river_no_lake.sum()})")

    # ── 7b. River mouth widening ─────────────────────────────────────
    # Widen channels in the last few pixels before coastline.
    # At 1:8 scale, ~3px = ~24 blocks. Gradual width increase near coast.
    coast_dist = distance_transform_edt(~is_sea).astype(np.float32)
    mouth_band = (coast_dist > 0) & (coast_dist <= 4) & new_river  # within 4px of coast
    if mouth_band.any():
        # Dilate mouth pixels proportional to proximity to coast
        # Closest to coast = most dilation
        mouth_close = new_river & (coast_dist > 0) & (coast_dist <= 2)
        mouth_mid = new_river & (coast_dist > 2) & (coast_dist <= 4)
        widened = binary_dilation(mouth_close, iterations=2)
        widened |= binary_dilation(mouth_mid, iterations=1)
        widened &= ~is_sea & ~lake_mask
        mouth_new = widened & ~new_river
        new_river |= mouth_new
        new_braid[mouth_new] = True  # mouth pixels are solid water like braid
        _log(f"  River mouth widening: +{mouth_new.sum()} px")

    # ── 7c. River-lake gap bridging ──────────────────────────────────
    # Where meandered rivers end up a few pixels from lake terrain
    # intersection, bridge the gap so they connect visually.
    if lake_mask.any() and new_river.any():
        lake_edge = binary_dilation(lake_mask, iterations=3) & ~lake_mask
        river_near_lake = binary_dilation(new_river, iterations=3) & ~new_river
        bridge = lake_edge & river_near_lake & ~is_sea
        if bridge.any():
            new_river |= bridge
            new_braid[bridge] = True
            _log(f"  River-lake bridging: +{bridge.sum()} px")

    # ── 8. Encode result ─────────────────────────────────────────────
    # Build new centerline_order from the meandered mask
    result = np.zeros((H, W), dtype=np.uint8)

    # For each new river pixel, find the nearest original order
    # Use the order map directly — most pixels will be close to their origin
    result[new_river] = np.where(
        new_braid[new_river], np.uint8(255),
        np.where(order[new_river] > 0, order[new_river], np.uint8(1))
    )

    # Desert wadis: river pixels in desert that are NOT preserved → 128
    wadi_px = new_river & desert & ~preserved_mask
    result[wadi_px] = np.uint8(128)
    _log(f"  Wadi pixels: {wadi_px.sum()}")

    # Preserve lake pixels unchanged from original
    if lake_mask.any():
        lake_river = lake_mask & (centerline_order > 0)
        result[lake_river] = centerline_order[lake_river]

    # ── 9. Save spline data for 50k re-rasterization ────────────────
    # The per-tile carver can evaluate these splines at full 50k resolution
    # to produce smooth river boundaries without upscale staircasing.
    import pickle
    spline_path = Path(__file__).parent.parent / "masks" / "river_splines.pkl"
    valid_splines = [s for s in spline_data if s.get("tck") is not None]
    with open(spline_path, "wb") as f:
        pickle.dump({
            "scale": 8,  # spline coords are in 1:8 space, multiply by 8 for 50k
            "branches": valid_splines,
        }, f, protocol=4)
    _log(f"  Saved {len(valid_splines)} splines to {spline_path}")

    return result


# ── Helper functions for meander ─────────────────────────────────────

def _trace_skeleton_branches(skel, min_len=3):
    """Trace a skeleton image into ordered point sequences (branches)."""
    H, W = skel.shape
    pts = set(zip(*np.where(skel)))
    if not pts:
        return []

    def neighbors(y, x):
        out = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                ny, nx = y + dy, x + dx
                if 0 <= ny < H and 0 <= nx < W and skel[ny, nx]:
                    out.append((ny, nx))
        return out

    neighbor_count = np.zeros_like(skel, dtype=np.uint8)
    for y, x in pts:
        neighbor_count[y, x] = len(neighbors(y, x))

    endpoints = {(y, x) for y, x in pts if neighbor_count[y, x] == 1}
    junctions = {(y, x) for y, x in pts if neighbor_count[y, x] >= 3}

    visited = np.zeros_like(skel, dtype=bool)
    branches = []

    def trace_from(sy, sx):
        path = [(sy, sx)]
        visited[sy, sx] = True
        cy, cx = sy, sx
        while True:
            nbrs = [(ny, nx) for ny, nx in neighbors(cy, cx)
                    if not visited[ny, nx]]
            if not nbrs:
                break
            if len(path) >= 2:
                dy = cy - path[-2][0]
                dx = cx - path[-2][1]
                nbrs.sort(key=lambda p: -((p[0]-cy)*dy + (p[1]-cx)*dx))
            ny, nx = nbrs[0]
            path.append((ny, nx))
            visited[ny, nx] = True
            cy, cx = ny, nx
            if (cy, cx) in junctions and len(path) > 1:
                break
        return np.array(path)

    starts = sorted(endpoints) + sorted(junctions)
    for sy, sx in starts:
        if visited[sy, sx]:
            continue
        branch = trace_from(sy, sx)
        if len(branch) >= min_len:
            branches.append(branch)

    for y, x in pts:
        if not visited[y, x]:
            branch = trace_from(y, x)
            if len(branch) >= min_len:
                branches.append(branch)

    return branches


def _add_meander(points, amplitude=3.0, wavelength=10, seed=0,
                  return_tck=False):
    """Displace branch control points perpendicular to flow for meander.

    If return_tck=True, returns (points, tck) where tck is the spline
    tuple from splprep — needed for 50k re-rasterization.
    """
    from scipy.interpolate import splprep, splev

    if len(points) < 4:
        return (points, None) if return_tck else points

    total_len = 0
    seg_lens = [0.0]
    for i in range(1, len(points)):
        d = np.sqrt((points[i, 0] - points[i-1, 0])**2 +
                     (points[i, 1] - points[i-1, 1])**2)
        total_len += d
        seg_lens.append(total_len)
    seg_lens = np.array(seg_lens)

    if total_len < wavelength:
        return (points, None) if return_tck else points

    n_ctrl = max(4, int(total_len / wavelength) + 1)
    ctrl_dists = np.linspace(0, total_len, n_ctrl)

    ctrl_pts = []
    for d in ctrl_dists:
        idx = np.searchsorted(seg_lens, d, side='right') - 1
        idx = max(0, min(idx, len(points) - 2))
        seg_len = max(seg_lens[idx+1] - seg_lens[idx], 1e-6)
        frac = np.clip((d - seg_lens[idx]) / seg_len, 0, 1)
        y = points[idx, 0] * (1 - frac) + points[idx+1, 0] * frac
        x = points[idx, 1] * (1 - frac) + points[idx+1, 1] * frac
        ctrl_pts.append([y, x])
    ctrl_pts = np.array(ctrl_pts)

    rng = np.random.RandomState(seed)
    for i in range(1, len(ctrl_pts) - 1):
        ty = ctrl_pts[i+1, 0] - ctrl_pts[i-1, 0]
        tx = ctrl_pts[i+1, 1] - ctrl_pts[i-1, 1]
        tlen = np.sqrt(ty**2 + tx**2)
        if tlen < 1e-6:
            continue
        perp_y = -tx / tlen
        perp_x = ty / tlen

        phase = rng.uniform(-np.pi, np.pi)
        t_frac = i / len(ctrl_pts)
        offset = amplitude * np.sin(2 * np.pi * t_frac * 2.5 + phase)
        offset += rng.uniform(-amplitude * 0.3, amplitude * 0.3)

        ctrl_pts[i, 0] += perp_y * offset
        ctrl_pts[i, 1] += perp_x * offset

    try:
        tck, u = splprep([ctrl_pts[:, 1], ctrl_pts[:, 0]], s=0, k=3)
        n_eval = max(int(total_len * 2), len(points))
        u_new = np.linspace(0, 1, n_eval)
        x_new, y_new = splev(u_new, tck)
        result = np.column_stack([y_new, x_new])
        return (result, tck) if return_tck else result
    except Exception:
        return (points, None) if return_tck else points


# ═══════════════════════════════════════════════════════════════════════════
# Phase 4 — Leopold hydraulic geometry (width + depth)
# ═══════════════════════════════════════════════════════════════════════════

def leopold_geometry(
    order: np.ndarray,
    flow: np.ndarray,
    slope: np.ndarray,
    override: np.ndarray,
    cfg: dict,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Compute channel width (blocks) and max depth (blocks) at every river pixel
    using Leopold & Maddock power-law scaling.

    Width  = w_min + (w_max - w_min) * Q^exp_w * slope_factor
    Depth  = d_min + (d_max - d_min) * Q^exp_d * slope_factor
    Q      = flow * rainfall_proxy(biome)
    slope_factor = clamp(1.0 - slope * slope_atten, 0.3, 1.5)

    Returns (width_u8, depth_u8) both shape (H, W), dtype uint8.
    """
    hcfg = cfg.get("hydrology_engine", {})
    geo  = hcfg.get("river_geometry", {})

    w_min  = geo.get("width_min", 1)
    w_max  = geo.get("width_max", 24)
    d_min  = geo.get("depth_min", 2)
    d_max  = geo.get("depth_max", 16)
    exp_w  = geo.get("discharge_exponent_w", 0.5)
    exp_d  = geo.get("discharge_exponent_d", 0.4)
    s_att  = geo.get("slope_attenuation", 2.0)

    rainfall = hcfg.get("rainfall_proxy", {})
    default_rain = rainfall.get("_default", 0.8)

    # Map override zone codes → rainfall multiplier
    # Override is normalised [0,1] from uint8.  Zone codes are 0,10,20...240.
    # Recover uint8: override * 255 → round → int
    try:
        from core.biome_assignment import OVERRIDE_BIOME_MAP
    except ImportError:
        from biome_assignment import OVERRIDE_BIOME_MAP
    zone_to_rain = {}
    for code, biome_name in OVERRIDE_BIOME_MAP.items():
        zone_to_rain[code] = rainfall.get(biome_name, default_rain)

    # Build rainfall raster
    override_u8 = np.round(override * 255.0).astype(np.uint8)
    rain_map = np.full_like(flow, default_rain)
    for code, r in zone_to_rain.items():
        rain_map[override_u8 == code] = r

    # Discharge proxy Q = flow * rainfall, clipped to [0, 1]
    Q = np.clip(flow * rain_map, 0.0, 1.0)

    # Slope factor: steep → narrower/shallower, flat → wider/deeper
    slope_factor = np.clip(1.0 - slope * s_att, 0.3, 1.5)

    # Only compute where river exists
    river_px = order > 0

    width_f = np.zeros_like(flow)
    depth_f = np.zeros_like(flow)

    width_f[river_px] = (
        w_min + (w_max - w_min)
        * np.power(Q[river_px], exp_w)
        * slope_factor[river_px]
    )
    depth_f[river_px] = (
        d_min + (d_max - d_min)
        * np.power(Q[river_px], exp_d)
        * slope_factor[river_px]
    )

    width_u8 = np.clip(width_f, 0, 255).astype(np.uint8)
    depth_u8 = np.clip(depth_f, 0, 255).astype(np.uint8)

    _log(f"  Leopold: width range [{width_u8[river_px].min()}-"
         f"{width_u8[river_px].max()}] blocks, "
         f"depth range [{depth_u8[river_px].min()}-"
         f"{depth_u8[river_px].max()}] blocks")

    return width_u8, depth_u8


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5 — Lake detection (curvature + flow convergence)
# ═══════════════════════════════════════════════════════════════════════════

def detect_lakes(
    height: np.ndarray,
    flow: np.ndarray,
    slope: np.ndarray,
    d8: np.ndarray,
    cfg: dict,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Detect natural lake basins using two complementary strategies:

    Strategy A — D8 pit flooding:
      Pixels where D8 == -1 (no downhill neighbor) are natural depressions.
      Flood-fill from each pit outward at equal elevation to find the basin
      extent, bounded by the spill point.

    Strategy B — Flow convergence flats:
      Relative-slope percentile approach.  Pixels in the bottom N% of slope
      for their local neighborhood AND with above-median flow form valley-
      bottom flats suitable for ribbon lakes / oxbow lakes.

    Both strategies produce candidate masks which are merged, labelled,
    area-filtered, then assigned spill-point bathymetry.

    Returns:
        lake_id   — uint16 (H, W), 0 = no lake, 1-N = lake ID
        lake_depth — uint8 (H, W), depth in blocks at each lake pixel
    """
    from scipy.ndimage import (label, binary_dilation, uniform_filter,
                                minimum_filter, maximum_filter)

    hcfg = cfg.get("hydrology_engine", {})
    lcfg = hcfg.get("lake_detection", {})

    min_area   = lcfg.get("min_area_src_px", 80)
    max_depth  = lcfg.get("max_depth_blocks", 20)
    slope_pctl = lcfg.get("slope_percentile", 8)       # bottom N% of local slope
    flow_pctl  = lcfg.get("flow_percentile", 70)       # top N% of flow
    local_win  = lcfg.get("local_window", 31)           # window for local stats
    pit_expand = lcfg.get("pit_expand_px", 5)           # dilation radius for pit seeds

    _log(f"  Lake detection: min_area={min_area}px, slope_pctl={slope_pctl}%, "
         f"flow_pctl={flow_pctl}%, local_win={local_win}")

    land = height > SEA_NORM

    # ── Strategy A: D8 pit seeds ──────────────────────────────────────────
    # Pits that survived resolve_flats are genuine depressions (ocean pits
    # were already resolved).  For pits that WERE resolved, we lost them —
    # but we can find pixels that are local height minima instead.
    local_min = minimum_filter(height, size=local_win)
    is_local_min = (height <= local_min + 0.001) & land  # within 0.1% of local min
    # Expand pit seeds to capture the basin floor
    if pit_expand > 0:
        from scipy.ndimage import binary_dilation as bd
        struct = np.ones((pit_expand * 2 + 1, pit_expand * 2 + 1), dtype=bool)
        pit_basins = bd(is_local_min, structure=struct) & land
    else:
        pit_basins = is_local_min

    _log(f"  Strategy A (local minima): {is_local_min.sum()} seed px, "
         f"{pit_basins.sum()} after expansion")

    # ── Strategy B: Flow convergence flats ────────────────────────────────
    # Use local slope percentile: flat-for-their-neighborhood pixels
    # with high-for-their-neighborhood flow
    if land.sum() > 0:
        land_slope = slope.copy()
        land_slope[~land] = np.nan

        # Local slope threshold (Nth percentile within window)
        # Approximate with: pixel slope < local_mean_slope * factor
        local_mean_slope = uniform_filter(
            np.where(land, slope, 0).astype(np.float64), size=local_win
        ).astype(np.float32)
        local_mean_slope = np.maximum(local_mean_slope, 0.01)

        # Relative flatness: slope < slope_pctl/100 * local mean
        rel_flat = (slope < local_mean_slope * (slope_pctl / 100.0)) & land

        # Flow above local percentile
        flow_thr = np.percentile(flow[land], flow_pctl)
        high_flow = (flow >= flow_thr) & land

        convergence_flats = rel_flat & high_flow
        _log(f"  Strategy B (convergence flats): {convergence_flats.sum()} px "
             f"(flow_thr={flow_thr:.4f})")
    else:
        convergence_flats = np.zeros_like(land)

    # ── Merge candidates ──────────────────────────────────────────────────
    candidates = (pit_basins | convergence_flats) & land

    # Connected components — filter by area
    labels, n_raw = label(candidates)
    _log(f"  Lake candidates (merged, raw): {n_raw}")

    if n_raw > 0:
        comp_sizes = np.bincount(labels.ravel())
        # Vectorized removal: build a LUT that maps small components to 0
        keep_lut = np.arange(len(comp_sizes), dtype=labels.dtype)
        for i in range(1, min(len(comp_sizes), n_raw + 1)):
            if comp_sizes[i] < min_area:
                keep_lut[i] = 0
        labels = keep_lut[labels]

    # Re-label to compact IDs via LUT
    unique_ids = np.unique(labels)
    unique_ids = unique_ids[unique_ids > 0]
    relabel_lut = np.zeros(int(labels.max()) + 1, dtype=np.uint16)
    for new_id, old_id in enumerate(unique_ids, start=1):
        relabel_lut[old_id] = new_id
    lake_id = relabel_lut[labels]

    n_lakes = len(unique_ids)
    _log(f"  Lakes after area filter: {n_lakes}")

    # ── Spill-point bathymetry (vectorized) ─────────────────────────────
    from scipy.ndimage import distance_transform_edt

    lake_depth = np.zeros(height.shape, dtype=np.float32)
    lake_wl    = np.zeros(height.shape, dtype=np.float32)  # water level (normalised height)
    TOTAL_HEIGHT_BLOCKS = 512  # Y range: -64 to 448

    all_lake_mask = lake_id > 0
    if all_lake_mask.any():
        # Per-lake bounding-box crop — distance transform per crop only
        # (avoids 6250×6250 global EDT which OOMs on tight-memory machines)
        for lid in range(1, n_lakes + 1):
            rows, cols = np.where(lake_id == lid)
            if len(rows) == 0:
                continue

            r0 = max(int(rows.min()) - 2, 0)
            r1 = min(int(rows.max()) + 3, height.shape[0])
            c0 = max(int(cols.min()) - 2, 0)
            c1 = min(int(cols.max()) + 3, height.shape[1])

            lid_crop = lake_id[r0:r1, c0:c1] == lid
            h_crop = height[r0:r1, c0:c1]

            # Distance-to-shore within this crop only (small, fast)
            dist_crop = distance_transform_edt(lid_crop).astype(np.float32)

            perim_crop = binary_dilation(lid_crop) & ~lid_crop
            if not perim_crop.any():
                continue

            spill_elev = h_crop[perim_crop].min()

            # Store water level for terrain-intersection carving
            lake_wl[r0:r1, c0:c1][lid_crop] = spill_elev

            terrain_d = np.clip((spill_elev - h_crop) * TOTAL_HEIGHT_BLOCKS,
                                0.0, max_depth)

            local_max = max(float(dist_crop[lid_crop].max()), 1.0)
            dist_norm = dist_crop / local_max
            synthetic_d = (dist_norm ** 1.3) * max_depth * 0.6

            combined = np.maximum(terrain_d, synthetic_d)
            combined[~lid_crop] = 0.0
            combined[lid_crop & (combined < 2)] = 2

            lake_depth[r0:r1, c0:c1][lid_crop] = combined[lid_crop]

    lake_depth_u8 = np.clip(lake_depth, 0, 255).astype(np.uint8)

    if n_lakes > 0:
        lake_px = lake_id > 0
        _log(f"  Lake depth range: [{lake_depth_u8[lake_px].min()}-"
             f"{lake_depth_u8[lake_px].max()}] blocks")
        _log(f"  Total lake area: {lake_px.sum()} px "
             f"({lake_px.sum()*100/lake_id.size:.2f}%)")
        _log(f"  Lake water levels: {lake_wl[lake_px].min():.4f}-"
             f"{lake_wl[lake_px].max():.4f} (normalised)")
    else:
        _log(f"  No lakes detected")

    return lake_id, lake_depth_u8, lake_wl


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5b — Lake spillpoint detection (S80 — feeds WP-style outflow rivers)
# ═══════════════════════════════════════════════════════════════════════════

def compute_lake_spillpoints(
    lake_id: np.ndarray,    # uint16 (H, W) — labelled lakes from detect_lakes
    height:  np.ndarray,    # float32 (H, W) — terrain (lower value = lower terrain)
) -> np.ndarray:
    """
    For each labelled lake, find the SPILLPOINT — the lowest non-lake cell
    that's adjacent to the lake.  This is where water would naturally spill
    out if the lake overflowed, and serves as a source for the lake's
    outflow river.

    Returns:
        spill_id: uint16 (H, W) — lake_id at the spillpoint cell of each
                  lake, 0 elsewhere.  At most one spillpoint per lake.

    Algorithm:
      1. Dilate the lake_id label image by 1 cell (3×3 max-filter), giving
         each non-lake cell adjacent to a lake the lake's id.
      2. Mask to "just-outside-lake" cells: dilated_id > 0 & lake_id == 0.
      3. Per lake, argmin(height) over its just-outside cells.

    O(N + n_lakes) — single dilation pass + one np.where + per-lake argmin
    via vectorized scatter.  ~1s for 6250×6250 with ~30 lakes.
    """
    from scipy.ndimage import grey_dilation

    H, W = lake_id.shape
    if lake_id.max() == 0:
        return np.zeros((H, W), dtype=np.uint16)

    # 1. Dilate label image: each non-lake cell adjacent to a lake takes
    #    the highest lake_id of its 8-neighbours.  (Grey dilation picks
    #    max value in the footprint, so non-lake cells become labelled.)
    dilated = grey_dilation(lake_id, footprint=np.ones((3, 3), dtype=bool))

    # 2. just-outside-lake mask: cells the dilation wrapped around but
    #    that aren't lake cells themselves
    just_outside = (dilated > 0) & (lake_id == 0)

    # 3. For each lake, find lowest just-outside cell (argmin height)
    #    Vectorise: bucket the just-outside cells by their assigned
    #    lake_id, then per-bucket argmin.
    spill_id = np.zeros((H, W), dtype=np.uint16)
    n_lakes = int(lake_id.max())
    out_r, out_c = np.where(just_outside)
    if len(out_r) == 0:
        return spill_id
    out_h = height[out_r, out_c]
    out_lid = dilated[out_r, out_c]

    # For each lake id present, find argmin(height) among its candidates
    for lid in range(1, n_lakes + 1):
        mask = out_lid == lid
        if not mask.any():
            continue
        local = np.argmin(out_h[mask])
        global_idx = np.where(mask)[0][local]
        spill_id[out_r[global_idx], out_c[global_idx]] = np.uint16(lid)

    n_spill = int((spill_id > 0).sum())
    _log(f"  spillpoints: {n_spill} found across {n_lakes} lakes")
    return spill_id


# ═══════════════════════════════════════════════════════════════════════════
# Phase 5c — WP-style findPath river network (S80, replaces NMS+Strahler)
# ═══════════════════════════════════════════════════════════════════════════
#
# Port of river_script1.7.js (sijmen_v_b's WorldPainter river generator).
# Per source, runs a weighted Dijkstra to the nearest sink (ocean OR lake
# OR an already-drawn river). Cost = path_dist + 150·height_blocks +
# meander_noise·random.  Neighborhood is rim-of-5×5-without-corners (12
# cells) which lets paths "jump" past tiny ridges and produces more
# natural-looking routes than 8-connected.
#
# Sources:
#   • Stream heads (river_mask & in_count==0) — top-flow per spatial bucket
#   • Lake spillpoints (one per labelled lake, lowest just-outside-lake
#     cell adjacent to the lake)
#
# Widths follow the WP linear formula: width = startWidth + (length-i)/length
# × (endWidth - startWidth) + slope-bonus.  Defaults are scaled to our 50k
# world (start=8 blocks, end=40 blocks) — WP defaults of 3-15 are tuned
# for 1-2k WP worlds and look like trickles at our scale.

# WP findPath neighborhood: rim of 5×5 minus the corners
_RIM_5X5_OFFSETS = np.array([
    (2, 1), (2, 0), (2, -1),
    (-2, 1), (-2, 0), (-2, -1),
    (1, 2), (0, 2), (-1, 2),
    (1, -2), (0, -2), (-1, -2),
], dtype=np.int32)

#
# IMPORTANT: WP widths are RADII (the carver and the original WP script
# both treat "width" as half-width / distance from centerline).  WP
# defaults are 3 and 15 — these are radii.  When wp_river_network was
# first written I treated these as diameters and doubled them, which
# inflated all carved rivers ~2× through the carver's
# `footprint = dist <= nearest_width` test, producing the "blobby
# circles" the user flagged.  S80 v3 fixes this — values below are
# RADII in MC blocks; total carved widths = 2 × these.
WP_START_WIDTH_BLOCKS = 2.0    # river HALF-width at source (radius, MC blocks)
WP_END_WIDTH_BLOCKS   = 5.0    # river HALF-width at mouth (radius, MC blocks)
                                # S80 v7: dropped 12→5 to fix "giant streams"
                                # complaint.  Trench widths now 4→10 blocks.
WP_RIVER_DEPTH        = 0.35   # depth = width × this + 1.5
WP_MIN_APPARENT_LEN   = 25     # min path length used in width formula (1:8 cells)
WP_MOUTH_EXTENSION_CELLS = 8   # ~60 MC blocks at 1:8 scale
WP_LAKE_EXTENSION_CELLS = 4    # cells to push lake-terminating paths INTO the
                                # lake interior so the carve crosses the lake
                                # boundary cleanly (no river→lake connection gap).
WP_MOUTH_WIDEN_FACTOR = 1.2    # mouth flare = end_radius × this
WP_HEIGHT_PENALTY     = 150.0  # cost of detouring vs going up 1 MC block
WP_RANDOMNESS         = 1000.0 # meander noise amplitude
WP_MAX_VISITS         = 250_000


def _build_meander_field(H: int, W: int, seed: int = 0xC0DEC0D) -> np.ndarray:
    """
    Deterministic smooth-ish noise field, [0, 1].  Used in place of WP's
    simplex2 for production.  Two-octave bilinear-upsampled value-noise.
    """
    rng = np.random.default_rng(seed)
    cH, cW = H // 16 + 2, W // 16 + 2
    coarse = rng.random((cH, cW), dtype=np.float32)
    yi = np.linspace(0, cH - 1, H).astype(np.float32)
    xi = np.linspace(0, cW - 1, W).astype(np.float32)
    y0 = np.floor(yi).astype(np.int32)
    x0 = np.floor(xi).astype(np.int32)
    y1 = np.minimum(y0 + 1, cH - 1)
    x1 = np.minimum(x0 + 1, cW - 1)
    fy = (yi - y0)[:, None]
    fx = (xi - x0)[None, :]
    a = coarse[y0[:, None], x0[None, :]]
    b = coarse[y0[:, None], x1[None, :]]
    c = coarse[y1[:, None], x0[None, :]]
    d = coarse[y1[:, None], x1[None, :]]
    return a * (1 - fy) * (1 - fx) + b * (1 - fy) * fx + c * fy * (1 - fx) + d * fy * fx


def _wp_find_path(
    sr: int, sc: int,
    height_blocks: np.ndarray,
    sink_mask: np.ndarray,
    avoid_mask: np.ndarray,
    meander: np.ndarray,
    randomness: float = WP_RANDOMNESS,
    height_penalty: float = WP_HEIGHT_PENALTY,
    max_visits: int = WP_MAX_VISITS,
    forbid_lake_id: int = 0,
    lake_id_arr: np.ndarray | None = None,
) -> list[tuple[int, int]] | None:
    """
    Single-source WP findPath.  Returns path source→sink (1-cell connected,
    intermediate cells re-parented through low-h candidates), or None.
    """
    import heapq

    H, W = height_blocks.shape
    if not (0 <= sr < H and 0 <= sc < W):
        return None

    # When pathing FROM a lake spillpoint, exclude the source lake cells
    # so the path doesn't trivially re-enter the lake it just spilled from.
    use_sink_mask = sink_mask
    use_avoid_mask = avoid_mask
    if forbid_lake_id and lake_id_arr is not None:
        forbid_cells = lake_id_arr == forbid_lake_id
        if forbid_cells.any():
            use_sink_mask = sink_mask & ~forbid_cells
            use_avoid_mask = avoid_mask | forbid_cells

    if use_sink_mask[sr, sc] or use_avoid_mask[sr, sc]:
        return None

    counter = 0
    open_set: list[tuple[float, int, int, int, float]] = []
    heapq.heappush(open_set, (4.0, counter, sr, sc, 4.0))
    counter += 1

    came_from = -np.ones((H, W, 2), dtype=np.int32)
    came_from[sr, sc] = (-1, -1)
    visited = np.zeros((H, W), dtype=bool)
    visited[sr, sc] = True

    visits = 0
    while open_set and visits < max_visits:
        _, _, r, c, dist = heapq.heappop(open_set)
        visits += 1

        if use_sink_mask[r, c]:
            path: list[tuple[int, int]] = []
            cr, cc = r, c
            while cr >= 0 and cc >= 0:
                path.append((int(cr), int(cc)))
                pr, pc = came_from[cr, cc]
                cr, cc = int(pr), int(pc)
            path.reverse()
            return path

        parent_meander = float(meander[r, c]) * randomness

        for dr, dc in _RIM_5X5_OFFSETS:
            nr, nc = r + int(dr), c + int(dc)
            if not (0 <= nr < H and 0 <= nc < W):
                continue
            if use_avoid_mask[nr, nc] or visited[nr, nc]:
                continue
            cell_dist = 1.0 if abs(dr) + abs(dc) <= 1 else float(np.hypot(dr, dc))
            new_dist = dist + cell_dist
            h_blocks = float(height_blocks[nr, nc])
            priority = new_dist + height_penalty * h_blocks + parent_meander

            visited[nr, nc] = True
            came_from[nr, nc] = (r, c)
            heapq.heappush(open_set, (priority, counter, nr, nc, new_dist))
            counter += 1

            # Intermediate fill for 5×5-rim moves
            if abs(dr) == 2 or abs(dc) == 2:
                if abs(dr) == 2:
                    cand = [(r + dr // 2, c), (r + dr // 2, c + dc)]
                else:
                    cand = [(r, c + dc // 2), (r + dr, c + dc // 2)]
                best = None
                best_h = np.inf
                for ir, ic in cand:
                    if not (0 <= ir < H and 0 <= ic < W):
                        continue
                    if visited[ir, ic]:
                        continue
                    h = float(height_blocks[ir, ic])
                    if h < best_h:
                        best_h = h
                        best = (ir, ic)
                if best is not None:
                    visited[best] = True
                    came_from[best] = (r, c)
                    came_from[nr, nc] = best

    return None


def _densify_path_4conn(
    path: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    """
    Ensure every adjacent pair of cells in the path is 4-connected.
    WP findPath uses rim-of-5x5 moves which include diagonal jumps
    (delta=(2,1) etc.); the in-loop intermediate fill bridges 1 cell
    but leaves the path 8-connected at best.  At 50k after NEAREST
    upsample, diagonally-adjacent 1:8 cells produce 8x8 blocks that
    touch only at a corner — EDT treats them as separate regions and
    the trench renders as disjoint blobs.
    """
    if len(path) < 2:
        return path
    out = [path[0]]
    for i in range(1, len(path)):
        r0, c0 = out[-1]
        r1, c1 = path[i]
        dr = r1 - r0
        dc = c1 - c0
        # Walk in 4-connected steps from (r0,c0) to (r1,c1)
        while (r0, c0) != (r1, c1):
            step_r = 1 if dr > 0 else (-1 if dr < 0 else 0)
            step_c = 1 if dc > 0 else (-1 if dc < 0 else 0)
            # Prefer the larger-magnitude axis first to keep the path
            # near a straight line.
            if abs(dr) >= abs(dc):
                r0 += step_r
                dr -= step_r
            else:
                c0 += step_c
                dc -= step_c
            out.append((r0, c0))
    return out


def _widths_along_path(
    path: list[tuple[int, int]],
    height_blocks: np.ndarray,
    start_width: float = WP_START_WIDTH_BLOCKS,
    end_width:   float = WP_END_WIDTH_BLOCKS,
    min_apparent_len: int = WP_MIN_APPARENT_LEN,
) -> np.ndarray:
    """WP-linear width per centerline cell, in MC blocks. Source → mouth."""
    n = len(path)
    if n == 0:
        return np.array([], dtype=np.float32)
    L = max(n, min_apparent_len)
    widths = np.zeros(n, dtype=np.float32)
    for i in range(n):
        i_from_mouth = (n - 1) - i
        adj_i = i_from_mouth - (n - L)
        frac = max(0.0, min(1.0, (L - adj_i) / L))
        w = start_width + frac * (end_width - start_width)
        if 0 < i < n - 4:
            r0, c0 = path[i]
            r1, c1 = path[min(i + 4, n - 1)]
            slope = abs(float(height_blocks[r0, c0]) - float(height_blocks[r1, c1])) / 4.0
            slope = min(slope, 1.0)
            w += 2.0 * slope * slope
        if i_from_mouth < 4:
            w += 0.5  # source guardrail
        widths[i] = w
    return widths


def _add_mouth_extensions(
    paths: list[list[tuple[int, int]]],
    widths_per_path: list[np.ndarray],
    lake_mask: np.ndarray,
    H: int, W: int,
    extension_cells: int = WP_MOUTH_EXTENSION_CELLS,
    lake_extension_cells: int = WP_LAKE_EXTENSION_CELLS,
    mouth_widen: float = WP_MOUTH_WIDEN_FACTOR,
) -> tuple[list[list[tuple[int, int]]], list[np.ndarray]]:
    """
    Append a tapered extension at every path terminus:
      • OCEAN-terminating: extend `extension_cells` cells with widening up
        to end_w × mouth_widen (delta).
      • LAKE-terminating: extend `lake_extension_cells` cells INTO the lake
        interior (constant width), so the carved trench crosses the lake
        boundary cleanly and the river-to-lake water transition has no gap.
    """
    new_paths, new_widths = [], []
    for path, widths in zip(paths, widths_per_path):
        new_paths.append(path)
        new_widths.append(widths)
        if len(path) < 4:
            continue
        r_last, c_last = path[-1]
        if not (0 <= r_last < H and 0 <= c_last < W):
            continue
        # Direction from last 4 cells (averaged trajectory)
        r_pre, c_pre = path[-4]
        dr = r_last - r_pre
        dc = c_last - c_pre
        n = max(abs(dr), abs(dc), 1)
        dr_unit = dr / n
        dc_unit = dc / n
        end_w = float(widths[-1]) if len(widths) else WP_END_WIDTH_BLOCKS

        ext_path = []
        ext_w = []

        if lake_mask[r_last, c_last]:
            # LAKE terminus — push into the lake interior with constant width
            for k in range(1, lake_extension_cells + 1):
                er = int(round(r_last + dr_unit * k))
                ec = int(round(c_last + dc_unit * k))
                if not (0 <= er < H and 0 <= ec < W):
                    break
                # Stop if we've left the lake (don't extend into far shore)
                if not lake_mask[er, ec]:
                    break
                ext_path.append((er, ec))
                ext_w.append(end_w)
        else:
            # OCEAN terminus — taper-widen into the water
            for k in range(1, extension_cells + 1):
                er = int(round(r_last + dr_unit * k))
                ec = int(round(c_last + dc_unit * k))
                if not (0 <= er < H and 0 <= ec < W):
                    break
                t = k / extension_cells
                w = end_w * (1.0 + (mouth_widen - 1.0) * t)
                ext_path.append((er, ec))
                ext_w.append(w)

        if ext_path:
            new_paths.append(ext_path)
            new_widths.append(np.array(ext_w, dtype=np.float32))
    return new_paths, new_widths


def _pick_sources_grid(
    river_mask: np.ndarray,
    in_count: np.ndarray,
    avoid_mask: np.ndarray,
    flow: np.ndarray,
    grid_cells: int = 24,
    max_sources: int = 1000,
) -> list[tuple[int, int]]:
    """
    Stream heads = (river_mask & in_count==0 & ~avoid).  Subsample by
    spatial grid: keep highest-flow head per grid cell for uniform coverage.
    """
    cand_mask = river_mask & (in_count == 0) & ~avoid_mask
    head_r, head_c = np.where(cand_mask)
    if len(head_r) == 0:
        return []
    H, W = river_mask.shape
    bucket_h = max(1, H // grid_cells)
    bucket_w = max(1, W // grid_cells)
    head_flow = flow[head_r, head_c]
    bucket = (head_r // bucket_h) * grid_cells + (head_c // bucket_w)
    order = np.argsort(bucket, kind="stable")
    bucket_s = bucket[order]
    flow_s = head_flow[order]
    starts = np.concatenate(([0], np.where(np.diff(bucket_s) != 0)[0] + 1))
    ends = np.concatenate((starts[1:], [len(bucket_s)]))
    keep_idx = []
    for s, e in zip(starts, ends):
        local = order[s:e]
        local_flow = flow_s[s:e]
        keep_idx.append(local[np.argmax(local_flow)])
    keep_idx = np.array(keep_idx, dtype=np.int64)
    if len(keep_idx) > max_sources:
        flow_keep = flow[head_r[keep_idx], head_c[keep_idx]]
        top = np.argsort(flow_keep)[::-1][:max_sources]
        keep_idx = keep_idx[top]
    return [(int(head_r[i]), int(head_c[i])) for i in keep_idx]


def wp_river_network(
    height: np.ndarray,           # (H, W) float32 [0,1] — terrain
    lake_id: np.ndarray,          # (H, W) uint16 — lake basin labels
    lake_spill: np.ndarray,       # (H, W) uint16 — spillpoint cells per lake
    override: np.ndarray,         # (H, W) uint8 — biome zone codes
    flow: np.ndarray,             # (H, W) float32 [0,1]
    d8: np.ndarray,               # (H, W) int8
    river_mask: np.ndarray,       # (H, W) bool
    in_count: np.ndarray,         # (H, W) int32
    cfg: dict,
    grid_cells: int = 24,
    max_stream_sources: int = 1000,
    lake_wl: np.ndarray | None = None,  # (H, W) float32 [0,1] — per-cell water level
    masks_dir: Path | None = None,      # for writing river_splines.pkl
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Run WP findPath from all sources (stream heads + lake spillpoints) and
    rasterise the result into (centerline_order, width, depth) arrays.

    Returns:
        centerline_order: (H, W) uint8 — 0 outside rivers, 1+ inside.
                          1 = stream-head paths, 2+ = spillpoint outflows
                          (encoding lake_id+1 capped at 254).
        width:            (H, W) uint8 — river width in MC blocks
        depth:            (H, W) uint8 — river depth in MC blocks
    """
    from scipy.ndimage import distance_transform_edt

    H, W = height.shape

    # Convert normalized height -> MC Y blocks for cost-units consistency
    # (height ∈ [0,1] times 65535 → raw uint16, then via the gaea-MC LUT
    # gives MC Y).  Use linear approximation matching column_generator's
    # spline at first order (same breakpoints).
    raw = (height.astype(np.float64) * 65535.0).clip(0, 65535)
    gaea_in = np.array([0, 17050, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    height_blocks = np.interp(raw.ravel(), gaea_in, mc_y_out).astype(np.float32).reshape(H, W)

    # Masks
    SAND_DUNE_DESERT_ZONE = 170
    ocean_mask = height <= SEA_NORM
    lake_mask = lake_id > 0
    # override may be raw uint8 (zone codes) or normalized float32 (zone/255).
    # read_downscaled normalises uint8 to [0, 1], so the precompute path
    # passes a float here.  Detect and normalise the comparison value.
    if override.dtype.kind == "f":
        override_u8 = np.round(override * 255.0).astype(np.uint8)
    else:
        override_u8 = override.astype(np.uint8)
    sand_dune_mask = override_u8 == SAND_DUNE_DESERT_ZONE
    # S80 v13: sink_mask uses TERRAIN-INTERSECTION lake (height < wl), NOT
    # full basin extent.  The carver paints lake water only where
    # `terrain < spill_elevation` (CLAUDE.md hard rule).  Cells in the
    # basin where terrain rises ABOVE WL are dry land in-game.  If
    # findPath terminates paths at basin-edge cells that are dry, streams
    # visibly stop short of the actual lake water surface.  Using
    # terrain-intersection sink ensures paths reach where water actually
    # is.  Falls back to full basin if lake_wl wasn't provided.
    if lake_wl is not None:
        underwater_lake = lake_mask & (height < lake_wl)
    else:
        underwater_lake = lake_mask
    sink_mask = ocean_mask | underwater_lake
    # No avoid-blocked cells by default.
    #   - Ocean is a SINK (target), not a wall — putting it in avoid
    #     prevents Dijkstra from ever reaching it.
    #   - SAND_DUNE_DESERT used to block traversal but that produces
    #     unrealistic "rivers tracing around the dune sea" artefacts.
    #     Real rivers cross deserts.  Sources are still excluded from
    #     dunes via river_mask (flow≈0 in arid zones) — paths can
    #     still pass through.
    avoid_mask = np.zeros_like(sand_dune_mask, dtype=bool)
    _log(f"  WP: ocean {ocean_mask.sum():,} cells | lakes {lake_mask.sum():,} | "
         f"dunes {sand_dune_mask.sum():,}")

    # Sources — exclude dunes from source selection only (paths can still
    # cross them at carve time per the avoid_mask above).
    source_avoid = sand_dune_mask
    sources = _pick_sources_grid(river_mask, in_count, source_avoid, flow,
                                 grid_cells=grid_cells,
                                 max_sources=max_stream_sources)
    _log(f"  WP: {len(sources)} stream-head sources")

    # Spillpoint sources + their forbid_lake_id mapping
    spill_source_lid: dict[tuple[int, int], int] = {}
    spill_r, spill_c = np.where(lake_spill > 0)
    for r, c in zip(spill_r, spill_c):
        spill_source_lid[(int(r), int(c))] = int(lake_spill[r, c])
    _log(f"  WP: {len(spill_source_lid)} spillpoint sources")
    sources.extend(spill_source_lid.keys())

    # Meander field
    meander = _build_meander_field(H, W, seed=0xC0DEC0D)

    # S80 v14: TRIBUTARY MERGING.
    # Process sources in priority order (highest source elevation first =
    # longest mainstem rivers first).  After each path is computed, add its
    # cells to a running sink set.  Subsequent paths terminate at:
    #   ocean | underwater_lake | EXISTING DRAWN PATHS
    # This is what makes tributaries merge into mainstem rivers visibly,
    # instead of every source producing an independent disconnected channel.
    sources_sorted = sorted(
        sources,
        key=lambda rc: float(height_blocks[rc[0], rc[1]]),
        reverse=True,  # highest elevation first
    )
    paths_ok: list[list[tuple[int, int]]] = []
    paths_failed: list[tuple[int, int]] = []
    spillpoint_lids: list[int] = []
    existing_path_mask = np.zeros((H, W), dtype=bool)
    n_lake = 0
    n_ocean = 0
    n_tributary = 0
    for i, (sr, sc) in enumerate(sources_sorted):
        if i % 50 == 0 and i > 0:
            _log(f"    WP findPath: {i}/{len(sources_sorted)} sources processed, "
                 f"{len(paths_ok)} ok ({n_ocean} ocean / {n_lake} lake / "
                 f"{n_tributary} tributary)")
        forbid_lid = spill_source_lid.get((sr, sc), 0)
        # Sink set for this path = static sinks ∪ existing drawn paths
        per_path_sink = sink_mask | existing_path_mask
        path = _wp_find_path(
            sr, sc, height_blocks, per_path_sink, avoid_mask, meander,
            forbid_lake_id=forbid_lid,
            lake_id_arr=lake_id if forbid_lid else None,
        )
        if path is None:
            paths_failed.append((sr, sc))
            continue
        paths_ok.append(path)
        spillpoint_lids.append(forbid_lid)
        # Classify terminus
        end_r, end_c = path[-1]
        if existing_path_mask[end_r, end_c]:
            n_tributary += 1
        elif lake_mask[end_r, end_c]:
            n_lake += 1
        elif ocean_mask[end_r, end_c]:
            n_ocean += 1
        # Add this path's cells to the existing sink set so subsequent
        # paths can merge into it as tributaries.
        for r, c in path:
            if 0 <= r < H and 0 <= c < W:
                existing_path_mask[r, c] = True

    _log(f"  WP findPath: {len(paths_ok)}/{len(sources_sorted)} reached water "
         f"({n_ocean} ocean, {n_lake} lake, {n_tributary} tributary)")

    # Densify paths to 4-connectivity so the NEAREST upsample to 50k
    # produces a continuous centerline (without this, diagonal rim-5x5
    # moves leave gaps that the carver's EDT renders as disjoint blobs).
    paths_ok = [_densify_path_4conn(p) for p in paths_ok]
    n_cells_after = sum(len(p) for p in paths_ok)
    _log(f"  WP densify: {n_cells_after:,} total path cells (4-connected)")

    # Compute WP-linear widths per path
    widths_per_path = [_widths_along_path(p, height_blocks) for p in paths_ok]

    # Add mouth extensions for ocean-terminating paths
    paths_ext, widths_ext = _add_mouth_extensions(
        paths_ok, widths_per_path, lake_mask, H, W,
    )
    n_ext = len(paths_ext) - len(paths_ok)
    _log(f"  WP mouth extensions: {n_ext} added")

    # ─────────────────────────────────────────────────────────────────────
    # S80 v13 — fit B-splines through each WP path, save to
    # masks/river_splines.pkl.  The carver re-rasterizes these splines at
    # 50k resolution producing smooth curves (not 8-block stairsteps from
    # NEAREST upsample).  Per-spline `widths_18` holds the WP-linear
    # widths, so the carver gets correct geometry.
    # ─────────────────────────────────────────────────────────────────────
    spline_branches: list[dict] = []
    try:
        from scipy.interpolate import splprep
        for path, widths in zip(paths_ext, widths_ext):
            if len(path) < 4:
                continue  # too short for B-spline (need k+1 = 4 points min)
            # Coords in 1:8 space.  Spline parametrised over [0, 1].
            ys = np.asarray([p[0] for p in path], dtype=np.float64)
            xs = np.asarray([p[1] for p in path], dtype=np.float64)
            # Drop consecutive duplicates (splprep doesn't accept them)
            keep = np.ones(len(xs), dtype=bool)
            keep[1:] = (np.diff(xs) != 0) | (np.diff(ys) != 0)
            xs, ys, ws = xs[keep], ys[keep], widths[keep]
            if len(xs) < 4:
                continue
            try:
                tck, _u = splprep([xs, ys], s=len(xs) * 0.5, k=3)
            except Exception:
                continue
            # widths_18 is in 1:8-SCALE radii (carver does radii_50k = radii_18 * SCALE).
            # ws here is in MC blocks (50k radii).  Divide by SCALE=8 so the
            # carver's multiplication restores the correct 50k radii.
            ws_18 = ws.astype(np.float32) / 8.0
            spline_branches.append({
                "tck": tck,
                "order": 1,            # WP paths emit as Strahler-1
                "branch_idx": len(spline_branches),
                "widths_18": list(ws_18),
            })
    except ImportError:
        _log("  WARNING: scipy.interpolate.splprep unavailable; skipping splines")

    # Write splines to masks/river_splines.pkl for the carver.
    if masks_dir is not None:
        import pickle
        spline_path = masks_dir / "river_splines.pkl"
        with open(spline_path, "wb") as f:
            pickle.dump({"scale": 8, "branches": spline_branches}, f, protocol=4)
        _log(f"  WP splines: {len(spline_branches)} branches saved to {spline_path}")
    else:
        _log(f"  WP splines: {len(spline_branches)} branches computed (no masks_dir, not saved)")

    # Stamp into width array (per-cell max if multiple paths cross)
    centerline_mask = np.zeros((H, W), dtype=bool)
    width_at_cell = np.zeros((H, W), dtype=np.float32)
    order_at_cell = np.zeros((H, W), dtype=np.uint8)
    for i, (path, widths) in enumerate(zip(paths_ext, widths_ext)):
        # Order encoding: 1 for any WP path (kept simple — confluence
        # widening would change this, deferred to follow-up).
        order_val = np.uint8(1)
        for (r, c), w in zip(path, widths):
            if 0 <= r < H and 0 <= c < W:
                centerline_mask[r, c] = True
                if w > width_at_cell[r, c]:
                    width_at_cell[r, c] = w
                if order_val > order_at_cell[r, c]:
                    order_at_cell[r, c] = order_val

    # Stamp footprint via EDT-based variable-width fill so the carver's
    # NMS-like behaviour gets a centerline + width pair to consume.
    if centerline_mask.any():
        dist_map, (iy, ix) = distance_transform_edt(~centerline_mask, return_indices=True)
        nearest_w = width_at_cell[iy, ix]
        # Width in BLOCKS, divide by SCALE to get cells, /2 for radius
        footprint = dist_map <= (nearest_w / 2.0 / SCALE)
        # Carver expects: centerline marks the river's traced line, width
        # carries the per-pixel target width.  Output width array spans
        # the entire footprint so the carver's per-tile rasterisation
        # picks up the right width at every river pixel.
        nearest_w_full = np.where(footprint, nearest_w, 0).astype(np.float32)
    else:
        nearest_w_full = np.zeros((H, W), dtype=np.float32)
        footprint = np.zeros((H, W), dtype=bool)

    # Build output arrays.
    # S80 v2: ONLY mark thin centerline cells in the order field.  The
    # earlier version stamped 255 ("braid fill") at footprint cells, which
    # caused the carver to treat the entire footprint as centerline, then
    # re-EDT from those cells, producing "puffy circle" artifacts in wide
    # river sections.  The width array (already per-cell across the
    # footprint via EDT propagation) carries enough information for the
    # carver to compute the right trench geometry from the thin centerline.
    centerline_order = np.where(centerline_mask, order_at_cell, np.uint8(0))

    # uint8 width clamped to [0, 255] blocks
    width_out = np.clip(nearest_w_full, 0, 255).astype(np.uint8)
    # Depth via WP formula: depth = width * 0.35 + 1.5
    depth_f = nearest_w_full * WP_RIVER_DEPTH + 1.5
    depth_f = np.where(footprint, depth_f, 0)
    depth_out = np.clip(depth_f, 0, 255).astype(np.uint8)

    n_river_px = int(footprint.sum())
    _log(f"  WP final: {n_river_px:,} river-footprint cells "
         f"({n_river_px*100/(H*W):.2f}% of map)")
    return centerline_order, width_out, depth_out


# ═══════════════════════════════════════════════════════════════════════════
# Phase 6 — Connectivity enforcement
# ═══════════════════════════════════════════════════════════════════════════

def enforce_connectivity(
    order: np.ndarray,
    d8: np.ndarray,
    height: np.ndarray,
    lake_id: np.ndarray,
) -> np.ndarray:
    """
    Ensure every river terminus either reaches the sea (height <= SEA_NORM)
    or connects to a lake.  Orphan dead-ends are extended downstream via D8
    until they connect.

    Uses iterative wavefront expansion: each iteration finds ALL current
    dead-end pixels and extends them one step downstream simultaneously.
    Repeats until no more dead-ends remain (or max iterations reached).

    Hybrid connectivity (Option C): extends rivers that are either
      - Strahler order >= 2 (significant channels), OR
      - any order within 50 pixels of coastline (~400 blocks at 1:8 scale)
    This ensures small coastal streams reach the sea without creating
    hundreds of parallel carved channels inland.

    Mutates *order* in-place and returns it.
    """
    from scipy.ndimage import distance_transform_edt

    H, W = d8.shape
    river_px = order > 0
    ocean = height <= SEA_NORM
    has_lake = lake_id > 0

    # Hybrid connectivity (Option C):
    # - Extend order >= 2 everywhere (significant channels)
    # - Extend ANY order within COASTAL_DIST of the ocean
    #   (small coastal streams must reach the sea for visual quality)
    # Low-order streams far inland are still skipped to avoid
    # hundreds of parallel carved channels.
    MIN_EXTEND_ORDER = 2
    COASTAL_DIST = 50  # pixels at 1:8 scale = 400 blocks
    dist_to_ocean = distance_transform_edt(~ocean)
    high_order_px = (order >= MIN_EXTEND_ORDER) | (
        (order > 0) & (dist_to_ocean < COASTAL_DIST)
    )

    # Find initial dead-end frontier: eligible river pixels (order >= 2
    # or coastal) whose downstream is not river, not lake, not ocean
    rr, rc = np.where(high_order_px)
    dirs = d8[rr, rc]
    valid = dirs >= 0
    rr, rc, dirs = rr[valid], rc[valid], dirs[valid]

    dst_r = rr + D8_DR[dirs]
    dst_c = rc + D8_DC[dirs]
    in_bounds = (dst_r >= 0) & (dst_r < H) & (dst_c >= 0) & (dst_c < W)
    dst_r, dst_c = dst_r[in_bounds], dst_c[in_bounds]

    is_dead = (
        ~river_px[dst_r, dst_c]
        & ~has_lake[dst_r, dst_c]
        & ~ocean[dst_r, dst_c]
    )

    # Frontier = the downstream pixels of dead-ends (these will be extended)
    frontier_r, frontier_c = dst_r[is_dead], dst_c[is_dead]

    extended_total = 0
    max_iterations = 300

    for iteration in range(max_iterations):
        if len(frontier_r) == 0:
            break

        # Deduplicate
        coords = frontier_r.astype(np.int64) * W + frontier_c.astype(np.int64)
        _, unique_idx = np.unique(coords, return_index=True)
        frontier_r, frontier_c = frontier_r[unique_idx], frontier_c[unique_idx]

        # Only extend pixels not already connected
        still_dead = (
            ~river_px[frontier_r, frontier_c]
            & ~ocean[frontier_r, frontier_c]
            & ~has_lake[frontier_r, frontier_c]
        )
        frontier_r, frontier_c = frontier_r[still_dead], frontier_c[still_dead]

        if len(frontier_r) == 0:
            break

        # Mark as river
        order[frontier_r, frontier_c] = 1
        river_px[frontier_r, frontier_c] = True
        extended_total += len(frontier_r)

        # Advance frontier: follow D8 downstream one more step.
        # For pixels with no valid D8 direction (d8 == -1), fall back to
        # steepest-descent toward lowest neighbour — this bridges flat
        # coastal areas where D8 routing failed.
        dirs = d8[frontier_r, frontier_c]
        valid_d8 = dirs >= 0

        # Handle valid D8 directions
        next_r = np.empty_like(frontier_r)
        next_c = np.empty_like(frontier_c)
        keep = np.zeros(len(frontier_r), dtype=bool)

        if valid_d8.any():
            vr = frontier_r[valid_d8]
            vc = frontier_c[valid_d8]
            vd = dirs[valid_d8]
            next_r[valid_d8] = vr + D8_DR[vd]
            next_c[valid_d8] = vc + D8_DC[vd]
            keep[valid_d8] = True

        # Fallback for invalid D8: find lowest 8-neighbour
        invalid_d8 = ~valid_d8
        if invalid_d8.any():
            ir = frontier_r[invalid_d8]
            ic = frontier_c[invalid_d8]
            # For each pixel, check all 8 neighbours and pick lowest height
            best_r = ir.copy()
            best_c = ic.copy()
            best_h = height[ir, ic].copy()
            for di in range(8):
                nr = ir + D8_DR[di]
                nc = ic + D8_DC[di]
                ib = (nr >= 0) & (nr < H) & (nc >= 0) & (nc < W)
                nh = np.full_like(best_h, 999.0)
                nh[ib] = height[nr[ib], nc[ib]]
                lower = nh < best_h
                best_r[lower] = nr[lower]
                best_c[lower] = nc[lower]
                best_h[lower] = nh[lower]
            # Only keep if we actually found a lower neighbour
            moved = (best_r != ir) | (best_c != ic)
            idx = np.where(invalid_d8)[0]
            next_r[idx[moved]] = best_r[moved]
            next_c[idx[moved]] = best_c[moved]
            keep[idx[moved]] = True

        frontier_r = next_r[keep]
        frontier_c = next_c[keep]

        in_bounds = (
            (frontier_r >= 0) & (frontier_r < H)
            & (frontier_c >= 0) & (frontier_c < W)
        )
        frontier_r, frontier_c = frontier_r[in_bounds], frontier_c[in_bounds]

    _log(f"  Connectivity: extended {extended_total} pixels "
         f"in {iteration + 1} iterations")
    return order


# ═══════════════════════════════════════════════════════════════════════════
# Phase 7 — Write output masks at 50k via chunked upscale
# ═══════════════════════════════════════════════════════════════════════════

def write_upscaled(
    data: np.ndarray,
    path: Path,
    dtype: str = "uint8",
    scale: int = SCALE,
    full_size: int = FULL_SIZE,
    chunk_rows: int = 50,
    interpolation: str = "nearest",
) -> None:
    """
    Write a downscaled array to a 50k×50k GeoTIFF via chunked upscale.

    interpolation: "nearest" for discrete values (order, lake IDs),
                   "bilinear" for continuous values (width, depth).

    Each chunk of *chunk_rows* source rows is expanded by *scale* in both axes,
    then written via rasterio windowed write.  Peak memory ≈ chunk_rows × full_size
    × dtype_size.
    """
    import rasterio
    from rasterio.windows import Window
    from rasterio.transform import from_bounds

    ds_h, ds_w = data.shape
    out_h = ds_h * scale
    out_w = ds_w * scale

    # Safety: clip to full_size
    out_h = min(out_h, full_size)
    out_w = min(out_w, full_size)

    np_dtype = np.dtype(dtype)

    profile = {
        "driver": "GTiff",
        "width": out_w,
        "height": out_h,
        "count": 1,
        "dtype": np_dtype,
        "compress": "lzw",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
    }

    use_bilinear = interpolation == "bilinear"

    with rasterio.open(str(path), "w", **profile) as dst:
        for src_row in range(0, ds_h, chunk_rows):
            src_end = min(src_row + chunk_rows, ds_h)

            if use_bilinear:
                # Grab 1 extra row on each side for bilinear overlap
                pad_top = 1 if src_row > 0 else 0
                pad_bot = 1 if src_end < ds_h else 0
                chunk_ext = data[src_row - pad_top : src_end + pad_bot].astype(np.float32)

                from scipy.ndimage import zoom
                up_ext = zoom(chunk_ext, (scale, scale), order=1)  # bilinear

                # Trim the padding rows from the upscaled result
                trim_top = pad_top * scale
                trim_bot = up_ext.shape[0] - pad_bot * scale if pad_bot else up_ext.shape[0]
                up = up_ext[trim_top:trim_bot]
            else:
                chunk = data[src_row:src_end]
                up = np.repeat(np.repeat(chunk, scale, axis=0), scale, axis=1)

            # Clip width if needed
            up = up[:, :out_w]

            dst_row = src_row * scale
            win = Window(0, dst_row, up.shape[1], up.shape[0])
            dst.write(up.astype(np_dtype), 1, window=win)

    _log(f"  Wrote {path.name}  ({out_h}×{out_w}, {dtype}, {interpolation})")


# ═══════════════════════════════════════════════════════════════════════════
# Phase 8 — Lake outlet connectivity to river network
# ═══════════════════════════════════════════════════════════════════════════

def connect_lake_outlets(
    order: np.ndarray,
    width: np.ndarray,
    depth: np.ndarray,
    lake_id: np.ndarray,
    d8: np.ndarray,
    height: np.ndarray,
) -> None:
    """
    For each lake, ensure the spill point connects to the river network.
    If the spill-point pixel isn't already a river pixel, trace downstream
    from it until hitting the existing network or ocean.

    Mutates order/width/depth in-place.
    """
    from scipy.ndimage import binary_dilation

    H, W = d8.shape
    n_lakes = int(lake_id.max())
    connected = 0

    for lid in range(1, n_lakes + 1):
        # Use bounding-box crop to avoid full-array boolean ops (OOM)
        rows, cols = np.where(lake_id == lid)
        if len(rows) == 0:
            continue

        r0 = max(int(rows.min()) - 2, 0)
        r1 = min(int(rows.max()) + 3, H)
        c0 = max(int(cols.min()) - 2, 0)
        c1 = min(int(cols.max()) + 3, W)

        lid_crop = lake_id[r0:r1, c0:c1] == lid
        perim_crop = binary_dilation(lid_crop) & ~lid_crop
        perim_local = np.where(perim_crop)
        if len(perim_local[0]) == 0:
            continue

        # Convert back to global coords
        perim_r = perim_local[0] + r0
        perim_c = perim_local[1] + c0

        perim_h = height[perim_r, perim_c]
        sp_idx = np.argmin(perim_h)
        sr, sc = int(perim_r[sp_idx]), int(perim_c[sp_idx])

        # If already a river pixel, connected
        if order[sr, sc] > 0:
            continue

        # Trace downstream from spill point
        cr, cc = sr, sc
        steps = 0
        while steps < 300:
            if order[cr, cc] > 0:
                break
            if height[cr, cc] <= SEA_NORM:
                break

            order[cr, cc] = 1
            width[cr, cc] = max(width[cr, cc], 3)  # lake outlet = at least 3 wide
            depth[cr, cc] = max(depth[cr, cc], 4)
            connected += 1

            d = int(d8[cr, cc])
            if d < 0:
                break
            nr = cr + int(D8_DR[d])
            nc = cc + int(D8_DC[d])
            if not (0 <= nr < H and 0 <= nc < W):
                break
            cr, cc = nr, nc
            steps += 1

    _log(f"  Lake outlets: connected {connected} pixels from "
         f"{n_lakes} lakes")


# ═══════════════════════════════════════════════════════════════════════════
# Orchestrator
# ═══════════════════════════════════════════════════════════════════════════

def run(
    masks_dir: Path,
    cfg: dict,
    crop: tuple[int, int, int, int] | None = None,
    dry_run: bool = False,
) -> dict[str, np.ndarray]:
    """
    Run the full hydrology precompute pipeline.

    Returns dict of downscaled arrays for inspection/testing.
    """
    t_total = time.perf_counter()

    hcfg = cfg.get("hydrology_engine", {})
    min_flow = hcfg.get("min_stream_flow", 0.15)

    # ── 1. Read masks ─────────────────────────────────────────────────────
    _log("Phase 1: Reading masks at 1:8...")
    masks = read_downscaled(
        masks_dir,
        ["height", "flow", "slope", "override"],
        crop=crop,
    )
    height   = masks["height"]
    flow     = masks["flow"]
    slope    = masks["slope"]
    override = masks["override"]
    ds_h, ds_w = height.shape
    _log(f"  Working resolution: {ds_h}×{ds_w}")

    # ── 2. D8 flow direction ──────────────────────────────────────────────
    _log("Phase 2: Computing D8 flow directions...")
    t0 = time.perf_counter()
    d8 = compute_d8(height)
    d8 = resolve_flats(d8, height)
    _log(f"  D8 done in {time.perf_counter()-t0:.1f}s  "
         f"pits remaining: {(d8 == -1).sum()}")

    # ── 3. River network + Strahler ───────────────────────────────────────
    _log("Phase 3: Extracting river network...")
    t0 = time.perf_counter()
    river_mask = extract_river_mask(flow, height, min_flow)
    _log(f"  River pixels (flow >= {min_flow}): "
         f"{river_mask.sum()} ({river_mask.sum()*100/river_mask.size:.2f}%)")

    order = strahler_order(d8, river_mask, height)
    _log(f"  Strahler done in {time.perf_counter()-t0:.1f}s")

    # ── 5. Lake detection (moved up — needed for WP findPath sinks) ──────
    _log("Phase 5: Detecting lakes...")
    gc.collect()  # free memory before lake detection (EDT is memory-hungry)
    t0 = time.perf_counter()
    lake_id, lake_depth, lake_wl = detect_lakes(height, flow, slope, d8, cfg)
    _log(f"  Lakes done in {time.perf_counter()-t0:.1f}s")

    # ── 5b. Lake spillpoints (S80) ────────────────────────────────────────
    _log("Phase 5b: Computing lake spillpoints...")
    t0 = time.perf_counter()
    lake_spill = compute_lake_spillpoints(lake_id, height)
    _log(f"  Spillpoints done in {time.perf_counter()-t0:.1f}s")

    # ── 5c. Build upstream count for WP source picking ────────────────────
    in_count = build_upstream_count(d8, river_mask)

    # ── 5d. WP-style findPath river network (S80) ─────────────────────────
    # Replaces Phase 3b NMS centerline + Phase 4 Leopold geometry +
    # Phase 6 connectivity enforcement.  WP findPath produces guaranteed-
    # connected paths from each source to nearest sink (ocean / lake /
    # later: existing river), with WP-linear widths and WP mouth
    # extensions for ocean-terminating paths.
    _log("Phase 5d: WP findPath river network...")
    t0 = time.perf_counter()
    centerline_order, width, depth = wp_river_network(
        height, lake_id, lake_spill, override,
        flow, d8, river_mask, in_count, cfg,
        lake_wl=lake_wl, masks_dir=masks_dir,
    )
    _log(f"  WP network done in {time.perf_counter()-t0:.1f}s")
    # `order` (Strahler) has been replaced by WP-tagged centerline_order.
    # Carver only checks `order_u8 > 0`, so equivalent for trenching.
    order = centerline_order

    # ── 7. Write output masks ─────────────────────────────────────────────
    if not dry_run:
        _log("Phase 7: Writing 50k output masks...")
        t0 = time.perf_counter()

        scale = SCALE
        full = FULL_SIZE
        if crop is not None:
            # Crop mode — write at downscaled size directly (no upscale)
            _log("  (crop mode — writing at working resolution, no upscale)")
            for name, arr, dt in [
                ("hydro_order", order, "uint8"),
                ("hydro_width", width, "uint8"),
                ("hydro_depth", depth, "uint8"),
                ("hydro_lake",  lake_id, "uint16"),
                ("hydro_lkdep", lake_depth, "uint8"),
                ("hydro_lake_wl", lake_wl, "float32"),
                ("hydro_lake_spill", lake_spill, "uint16"),
                ("hydro_centerline", order, "uint8"),
            ]:
                import rasterio
                out_path = masks_dir / f"{name}.tif"
                with rasterio.open(
                    str(out_path), "w", driver="GTiff",
                    width=ds_w, height=ds_h, count=1,
                    dtype=dt, compress="lzw",
                ) as dst:
                    dst.write(arr.astype(dt), 1)
                _log(f"  Wrote {name}.tif  ({ds_h}×{ds_w}, {dt})")
        else:
            write_upscaled(order,      masks_dir / "hydro_order.tif", "uint8",  scale, full,
                           interpolation="nearest")   # discrete Strahler order
            gc.collect()
            write_upscaled(width,      masks_dir / "hydro_width.tif", "uint8",  scale, full,
                           interpolation="bilinear")   # continuous channel width
            gc.collect()
            write_upscaled(depth,      masks_dir / "hydro_depth.tif", "uint8",  scale, full,
                           interpolation="bilinear")   # continuous channel depth
            gc.collect()
            write_upscaled(lake_id,    masks_dir / "hydro_lake.tif",  "uint16", scale, full,
                           interpolation="nearest")    # discrete lake ID
            gc.collect()
            write_upscaled(lake_depth, masks_dir / "hydro_lkdep.tif", "uint8",  scale, full,
                           interpolation="bilinear")   # continuous lake depth
            gc.collect()
            write_upscaled(lake_wl,    masks_dir / "hydro_lake_wl.tif", "float32", scale, full,
                           interpolation="nearest")    # per-lake water level (normalised height)
            gc.collect()
            write_upscaled(lake_spill, masks_dir / "hydro_lake_spill.tif", "uint16", scale, full,
                           interpolation="nearest")    # discrete spillpoint cell per lake
            gc.collect()
            write_upscaled(centerline_order, masks_dir / "hydro_centerline.tif", "uint8", scale, full,
                           interpolation="nearest")    # NMS-thinned centerline with Strahler order

        _log(f"  Write phase done in {time.perf_counter()-t0:.1f}s")
    else:
        _log("Phase 7: SKIPPED (dry-run)")

    elapsed = time.perf_counter() - t_total
    _log(f"=== Hydrology precompute complete in {elapsed:.1f}s ===")

    # Summary stats
    river_px = (order > 0).sum()
    lake_px  = (lake_id > 0).sum()
    n_lakes  = int(lake_id.max())
    _log(f"  River pixels: {river_px} ({river_px*100/order.size:.2f}%)")
    _log(f"  Lake pixels:  {lake_px} ({lake_px*100/order.size:.2f}%)")
    _log(f"  Lakes:        {n_lakes}")
    _log(f"  Max Strahler: {order.max()}")
    _log(f"  Width range:  {width[order>0].min() if river_px else 0}-"
         f"{width[order>0].max() if river_px else 0} blocks")
    _log(f"  Depth range:  {depth[order>0].min() if river_px else 0}-"
         f"{depth[order>0].max() if river_px else 0} blocks")

    return {
        "order": order,
        "width": width,
        "depth": depth,
        "lake_id": lake_id,
        "lake_depth": lake_depth,
        "lake_wl": lake_wl,
        "d8": d8,
        "height": height,
        "flow": flow,
    }


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Vandir Hydrology Precompute — global river/lake extraction"
    )
    parser.add_argument("--config", default="config/thresholds.json",
                        help="Path to thresholds.json")
    parser.add_argument("--masks", default="masks",
                        help="Masks directory")
    parser.add_argument("--crop", type=int, nargs=4, metavar=("X0", "Y0", "X1", "Y1"),
                        help="Source-pixel crop window for testing "
                             "(e.g. --crop 20000 20000 28000 28000)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run computation but skip writing 50k TIFs")

    args = parser.parse_args()

    cfg_path = Path(args.config)
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = json.load(f)
    else:
        _log(f"WARNING: {cfg_path} not found — using defaults")
        cfg = {}

    masks_dir = Path(args.masks)
    if not masks_dir.exists():
        _log(f"ERROR: masks directory {masks_dir} not found")
        sys.exit(1)

    crop = tuple(args.crop) if args.crop else None

    run(masks_dir=masks_dir, cfg=cfg, crop=crop, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
