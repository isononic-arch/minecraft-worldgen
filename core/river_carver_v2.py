"""
river_carver_v2.py — Vandir Pipeline Step 6a (v2)
===================================================
Carves river channels and lakes using precomputed hydrology masks from
hydrology_precompute.py.  Drop-in replacement for river_carver.py.

Inputs (per tile, via tile_streamer):
    surface_y      : (H, W) int16 — MC Y surface from column generator
    hydro_order    : (H, W) float32 [0,1] — Strahler order / 255
    hydro_width    : (H, W) float32 [0,1] — channel width in blocks / 255
    hydro_depth    : (H, W) float32 [0,1] — max channel depth in blocks / 255
    hydro_lake     : (H, W) float32 [0,1] — lake ID / 65535 (>0 = lake pixel)
    hydro_lkdep    : (H, W) float32 [0,1] — lake depth in blocks / 255
    flow_tile      : (H, W) float32 [0,1] — flow accumulation (for bank width)

Outputs:
    surface_y  : (H, W) int16 — modified with channels/lakes carved
    river_meta : (H, W) uint8 — channel type (0=none, 1=stream, 2=river, 3=lake)
                 Consumed by surface_decorator (Step 7) for bank placement.

Algorithm:
    1. Recover uint8/uint16 values from normalised masks
    2. Identify river centerline pixels (order > 0)
    3. Expand centerlines to target width using distance transform
    4. Apply parabolic depth cross-section
    5. Identify lake pixels, apply precomputed bathymetry
    6. Carve surface_y, build river_meta
    7. Compute bank zones (dilated river/lake edges)

All thresholds from config/thresholds.json under "hydrology_engine".
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import (distance_transform_edt, binary_dilation,
                           gaussian_filter, maximum_filter)

# Channel type constants — must match river_carver.py for API compat
CHAN_NONE   = np.uint8(0)
CHAN_STREAM = np.uint8(1)
CHAN_RIVER  = np.uint8(2)
CHAN_LAKE   = np.uint8(3)
CHAN_WADI   = np.uint8(4)  # dry sand channel — carved but no water fill

MC_Y_MIN   = -64
SEA_LEVEL  = 63
BEDROCK_Y  = MC_Y_MIN


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _denorm_u8(arr: np.ndarray) -> np.ndarray:
    """Convert tile_streamer normalised [0,1] float32 back to uint8 values."""
    return np.round(arr * 255.0).astype(np.uint8)


def _denorm_u16(arr: np.ndarray) -> np.ndarray:
    """Convert tile_streamer normalised [0,1] float32 back to uint16 values."""
    return np.round(arr * 65535.0).astype(np.uint16)


# ═══════════════════════════════════════════════════════════════════════════
# Core carving
# ═══════════════════════════════════════════════════════════════════════════

def _height_norm_to_mc_y(h_norm: np.ndarray, cfg: dict) -> np.ndarray:
    """Convert normalised height [0,1] to MC Y blocks using terrain spline."""
    spline = cfg.get("terrain_spline", {})
    gaea_in = np.array(spline.get("gaea_in", [0, 17050, 45000, 65496]),
                       dtype=np.float64)
    mc_y    = np.array(spline.get("mc_y_out", [-64, 63, 200, 448]),
                       dtype=np.float64)
    gaea_norm = gaea_in / 65535.0
    return np.interp(h_norm.ravel(), gaea_norm, mc_y).reshape(h_norm.shape).astype(np.float32)


def _least_cost_path(cost, start_r, start_c, targets, max_visits=250_000):
    """
    Dijkstra shortest path on 8-connected grid from (start_r, start_c)
    to the nearest True pixel in *targets*.

    Parameters
    ----------
    cost     : (H, W) float32 — per-pixel traversal cost (lower = preferred)
    start_r, start_c : int — start pixel
    targets  : (H, W) bool — destination pixel mask
    max_visits : int — upper bound on explored pixels (safety)

    Returns
    -------
    (path_r, path_c) int arrays including start and end, or None.
    """
    import heapq
    H, W = cost.shape
    INF = float("inf")
    dist = np.full((H, W), INF, dtype=np.float64)
    dist[start_r, start_c] = 0.0
    prev_r = np.full((H, W), -1, dtype=np.int32)
    prev_c = np.full((H, W), -1, dtype=np.int32)
    heap = [(0.0, int(start_r), int(start_c))]
    visited = 0

    while heap and visited < max_visits:
        d, r, c = heapq.heappop(heap)
        if d > dist[r, c]:
            continue
        visited += 1
        if targets[r, c]:
            pr, pc = [], []
            cr, cc = r, c
            while cr >= 0:
                pr.append(cr); pc.append(cc)
                nr2, nc2 = int(prev_r[cr, cc]), int(prev_c[cr, cc])
                cr, cc = nr2, nc2
            return np.array(pr[::-1], dtype=np.intp), np.array(pc[::-1], dtype=np.intp)

        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = r + dr, c + dc
                if 0 <= nr < H and 0 <= nc < W:
                    step = cost[nr, nc] * (1.414 if (abs(dr) + abs(dc) == 2) else 1.0)
                    nd = d + step
                    if nd < dist[nr, nc]:
                        dist[nr, nc] = nd
                        prev_r[nr, nc] = np.int32(r)
                        prev_c[nr, nc] = np.int32(c)
                        heapq.heappush(heap, (nd, int(nr), int(nc)))
    return None


def _smooth_path(path_r, path_c, H, W, subsample=8):
    """
    Smooth a grid-walk path with cubic spline to remove 8-connected
    jaggedness.  Control points come from the actual path (no artificial
    offsets), the spline just rounds off the 45° grid steps.

    Returns (smooth_r, smooth_c) clipped to [0, H-1] × [0, W-1].
    """
    n = len(path_r)
    if n < 8:
        return path_r, path_c

    from scipy.interpolate import UnivariateSpline
    t = np.arange(n, dtype=np.float64)

    # Fewer control points = smoother curves = fewer sharp bends
    step = max(n // max(n // subsample, 3), 4)
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    ctrl_t = np.array(idx, dtype=np.float64)
    ctrl_r = np.array([float(path_r[i]) for i in idx], dtype=np.float64)
    ctrl_c = np.array([float(path_c[i]) for i in idx], dtype=np.float64)

    try:
        # Heavy smoothing: eliminates sharp direction changes.
        # s scales with control point count so long paths stay smooth.
        s = len(idx) * 20.0
        spl_r = UnivariateSpline(ctrl_t, ctrl_r, s=s, k=3)
        spl_c = UnivariateSpline(ctrl_t, ctrl_c, s=s, k=3)
        # Oversample for smooth drawing (3x path length)
        t_fine = np.linspace(0, n - 1, max(n * 3, 30))
        sr = np.clip(np.round(spl_r(t_fine)).astype(np.intp), 0, H - 1)
        sc = np.clip(np.round(spl_c(t_fine)).astype(np.intp), 0, W - 1)
        return sr, sc
    except Exception:
        return path_r, path_c


def _draw_tapered_channel(centerline, order_u8, path_r, path_c,
                          H, W, taper_frac=0.22, taper_max_w=4,
                          channel_order=1, base_width=2):
    """
    Stamp a least-cost path onto *centerline* with proper channel width.
    The raw Dijkstra path is spline-smoothed first.

    Width profile: *base_width* everywhere, widening to *base_width +
    taper_max_w* at both mouths (first/last *taper_frac* of path).
    Perpendicular direction is recomputed per-pixel from local tangent
    so the channel bends correctly.
    """
    if len(path_r) == 0:
        return

    # Smooth the grid-walk into organic curves
    path_r, path_c = _smooth_path(path_r, path_c, H, W)
    n = len(path_r)
    taper_n = max(int(n * taper_frac), 6)

    for i in range(n):
        rr, cc = int(path_r[i]), int(path_c[i])
        if not (0 <= rr < H and 0 <= cc < W):
            continue

        # Local tangent for perpendicular direction (varies along path)
        i0 = max(i - 4, 0)
        i1 = min(i + 4, n - 1)
        dr_loc = float(path_r[i1] - path_r[i0])
        dc_loc = float(path_c[i1] - path_c[i0])
        mag = max(np.hypot(dr_loc, dc_loc), 1.0)
        perp_r = -dc_loc / mag
        perp_c =  dr_loc / mag

        # Width: base everywhere + extra at mouths
        mouth = 0.0
        if i < taper_n:
            mouth = max(mouth, 1.0 - i / taper_n)
        if (n - 1 - i) < taper_n:
            mouth = max(mouth, 1.0 - (n - 1 - i) / taper_n)
        half_w = base_width + int(round(taper_max_w * mouth))

        for dw in range(-half_w, half_w + 1):
            pr = rr + int(round(perp_r * dw))
            pc = cc + int(round(perp_c * dw))
            if 0 <= pr < H and 0 <= pc < W:
                centerline[pr, pc] = True
                order_u8[pr, pc] = max(order_u8[pr, pc], channel_order)


def carve_rivers(
    surface_y:      np.ndarray,   # (H, W) int16
    flow_tile:      np.ndarray,   # (H, W) float32 [0,1]
    river_tile:     np.ndarray,   # (H, W) — unused, API compat
    cfg:            dict,
    hydro_order:    np.ndarray | None = None,  # (H, W) float32 [0,1]
    hydro_width:    np.ndarray | None = None,
    hydro_depth:    np.ndarray | None = None,
    hydro_lake:     np.ndarray | None = None,
    hydro_lkdep:    np.ndarray | None = None,
    hydro_lake_wl:  np.ndarray | None = None,  # (H, W) float32 [0,1] — water level
    hydro_centerline: np.ndarray | None = None,  # (H, W) float32 [0,1] — precomputed NMS centerline
    height_norm:    np.ndarray | None = None,  # (H, W) float32 [0,1] — raw terrain
    masks_dir:      "Path | None" = None,
    tile_x:         int | None = None,
    tile_z:         int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Carve rivers and lakes into surface_y using precomputed hydrology masks.

    If hydro masks are not provided (None), falls back to legacy
    threshold-based carving from river_carver.py.

    Returns (surface_y_carved, river_meta) matching the v1 contract.
    """
    # If no hydro masks AND no precomputed centerline, fall back to legacy carver
    _has_any_hydro = ((hydro_order is not None and hydro_order.max() > 0)
                      or (hydro_centerline is not None and hydro_centerline.max() > 0))
    if not _has_any_hydro:
        from core.river_carver import carve_rivers as _legacy
        sy, rm = _legacy(surface_y, flow_tile, river_tile, cfg)
        return sy, rm, np.zeros(surface_y.shape, dtype=bool)

    H, W = surface_y.shape
    hcfg = cfg.get("hydrology_engine", {})
    geo  = hcfg.get("river_geometry", {})

    # ── 1. Recover integer values from normalised masks ───────────────────
    order_u8  = _denorm_u8(hydro_order)    # Strahler order (0 = no river)
    width_u8  = _denorm_u8(hydro_width)    # target width in blocks
    depth_u8  = _denorm_u8(hydro_depth)    # max depth in blocks
    lake_u16  = _denorm_u16(hydro_lake)    # lake ID (0 = no lake)
    lkdep_u8  = _denorm_u8(hydro_lkdep)   # lake depth in blocks

    # ── 2. Build river meta and carved surface ────────────────────────────
    surface_out = surface_y.astype(np.int32).copy()
    river_meta  = np.zeros((H, W), dtype=np.uint8)

    # ── 3. Lake carving — terrain intersection ─────────────────────────
    # Instead of smoothing a binary mask, let the Gaea terrain define the
    # lake boundary.  Each lake has a water-surface elevation (the spill
    # point).  Pixels where terrain < water_level are underwater.  The
    # shoreline follows the natural terrain contour — smooth by construction.
    lake_mask = np.zeros((H, W), dtype=bool)  # default: no lakes
    lake_raw = lake_u16 > 0
    above_sea = surface_out > SEA_LEVEL

    if lake_raw.any():
        from scipy.ndimage import label

        # ── 3a. Absorb small river fragments near lakes ──────────────────
        river_px_raw  = order_u8 > 0
        absorb_dist   = float(geo.get("lake_absorb_dist", 25.0))
        min_creek_px  = int(geo.get("min_creek_pixels", 80))

        if river_px_raw.any():
            dist_riv_to_lake = distance_transform_edt(~lake_raw).astype(np.float32)
            riv_labeled, n_riv = label(river_px_raw)
            for rid in range(1, n_riv + 1):
                comp = riv_labeled == rid
                comp_size = comp.sum()
                comp_near_lake = (dist_riv_to_lake[comp] <= absorb_dist).any()
                if comp_size < min_creek_px and comp_near_lake:
                    lake_raw = lake_raw | comp  # absorb into lake

        # ── 3b. Terrain-intersection lake fill ───────────────────────────
        # The lake boundary is defined by where Gaea terrain < spill
        # elevation.  No morph/blur — the shoreline follows natural terrain
        # contours at full 50k resolution.  Basin expansion ensures the
        # test region extends past the 8x8 NEAREST staircase of the hydro
        # mask; terrain intersection clips it naturally.
        PAD = 48  # enough for basin expansion dilation
        basin_expand_px = int(geo.get("lake_basin_expand_px", 32))

        _use_terrain = (masks_dir is not None and tile_x is not None
                        and tile_z is not None)
        if _use_terrain:
            import rasterio
            from rasterio.windows import Window as _RWindow
            wl_tif     = masks_dir / "hydro_lake_wl.tif"
            height_tif = masks_dir / "height.tif"
            lake_tif   = masks_dir / "hydro_lake.tif"

            if wl_tif.exists() and height_tif.exists():
                col_off = tile_x * W
                row_off = tile_z * H
                px0 = max(col_off - PAD, 0)
                pz0 = max(row_off - PAD, 0)

                with rasterio.open(wl_tif) as src:
                    px1 = min(col_off + W + PAD, src.width)
                    pz1 = min(row_off + H + PAD, src.height)
                    pad_wl = src.read(1, window=_RWindow(
                        px0, pz0, px1 - px0, pz1 - pz0)).astype(np.float32)

                with rasterio.open(height_tif) as src:
                    pad_h_raw = src.read(1, window=_RWindow(
                        px0, pz0, px1 - px0, pz1 - pz0))
                    pad_h_norm = pad_h_raw.astype(np.float32) / 65535.0

                # Basin = any pixel with a water level assigned
                pad_basin = pad_wl > 0

                # Also read lake ID mask for basin expansion —
                # dilate generously so basin extends past the 8x8
                # NEAREST staircase edge of the hydro mask.
                if basin_expand_px > 0 and lake_tif.exists():
                    with rasterio.open(lake_tif) as src:
                        pad_lake_id = src.read(1, window=_RWindow(
                            px0, pz0, px1 - px0, pz1 - pz0))
                    pad_basin_orig = pad_lake_id > 0
                    pad_basin_exp = binary_dilation(
                        pad_basin_orig, iterations=basin_expand_px)
                    # Propagate water level into expanded zone:
                    # use maximum_filter to spread the wl value outward
                    k = basin_expand_px * 2 + 1
                    pad_wl_exp = maximum_filter(pad_wl, size=k)
                    # Only apply expansion where dilation reached but
                    # original wl was zero
                    new_zone = pad_basin_exp & ~pad_basin
                    pad_wl[new_zone] = pad_wl_exp[new_zone]
                    pad_basin = pad_wl > 0

                # Convert both to MC Y via terrain spline
                pad_water_y = _height_norm_to_mc_y(pad_wl, cfg)
                pad_terrain_y = _height_norm_to_mc_y(pad_h_norm, cfg)

                # Terrain intersection: underwater where terrain < water level
                pad_underwater = pad_basin & (pad_terrain_y < pad_water_y)

                # Terrain-shaped depth: scale the natural terrain relief
                # (water_level - terrain) to reach the target max depth.
                # Preserves the irregular terrain contour shape instead of
                # imposing circular EDT rings.
                nat_depth = np.where(pad_underwater,
                                     pad_water_y - pad_terrain_y, 0.0)
                lake_min_depth = float(geo.get("lake_min_center_depth", 15.0))
                if pad_underwater.any():
                    nat_max = max(float(nat_depth.max()), 0.001)
                    pad_depth_f = (nat_depth / nat_max) * lake_min_depth
                    pad_depth_f[~pad_underwater] = 0.0
                else:
                    pad_depth_f = nat_depth

                # Crop back to tile extent
                crop_r0 = row_off - pz0
                crop_c0 = col_off - px0
                lake_mask = pad_underwater[crop_r0:crop_r0+H, crop_c0:crop_c0+W]
                lake_depths = pad_depth_f[crop_r0:crop_r0+H, crop_c0:crop_c0+W]

                # Also mark basin pixels above water as lake-adjacent
                # (for bank decoration even where terrain > water)
                pad_lake_zone = pad_basin & (pad_terrain_y >= pad_water_y)
                lake_shore_band = pad_lake_zone[crop_r0:crop_r0+H, crop_c0:crop_c0+W]
            else:
                _use_terrain = False

        if not _use_terrain:
            # Fallback: use tile-local hydro_lake_wl if available
            if hydro_lake_wl is not None and hydro_lake_wl.max() > 0 and height_norm is not None:
                wl_local = hydro_lake_wl.copy()
                basin_local = wl_local > 0
                if basin_expand_px > 0:
                    basin_exp = binary_dilation(basin_local, iterations=basin_expand_px)
                    k = basin_expand_px * 2 + 1
                    wl_exp = maximum_filter(wl_local, size=k)
                    new_zone = basin_exp & ~basin_local
                    wl_local[new_zone] = wl_exp[new_zone]
                    basin_local = wl_local > 0
                water_y  = _height_norm_to_mc_y(wl_local, cfg)
                terrain_y = _height_norm_to_mc_y(height_norm, cfg)
                lake_mask = basin_local & (terrain_y < water_y)
                nat_depth = np.where(lake_mask, water_y - terrain_y, 0.0)
                lake_min_depth = float(geo.get("lake_min_center_depth", 15.0))
                if lake_mask.any():
                    nat_max = max(float(nat_depth.max()), 0.001)
                    lake_depths = (nat_depth / nat_max) * lake_min_depth
                    lake_depths[~lake_mask] = 0.0
                else:
                    lake_depths = nat_depth
                lake_shore_band = basin_local & ~lake_mask
            else:
                lake_mask = np.zeros((H, W), dtype=bool)
                lake_depths = np.zeros((H, W), dtype=np.float32)
                lake_shore_band = np.zeros((H, W), dtype=bool)

        # Clamp to not breach bedrock
        max_allowed = (surface_out - (BEDROCK_Y + 3)).astype(np.float32)
        lake_depths = np.minimum(lake_depths, max_allowed)
        lake_depths = np.maximum(lake_depths, 0.0)

        carve_mask = lake_mask & above_sea & (lake_depths > 0.75)

        # Stochastic rounding (dithering) to break up underwater contour rings.
        # Use world-space deterministic hash for cross-tile consistency.
        lake_d_float = lake_depths[carve_mask]
        lake_d_floor = np.floor(lake_d_float)
        frac = lake_d_float - lake_d_floor
        carve_rows, carve_cols = np.where(carve_mask)
        _tx = tile_x if tile_x is not None else 0
        _tz = tile_z if tile_z is not None else 0
        world_x = carve_cols.astype(np.int64) + _tx * W
        world_z = carve_rows.astype(np.int64) + _tz * H
        pixel_hash = ((world_x * 73856093) ^ (world_z * 19349663)) & 0xFFFFFFFF
        pixel_rand = (pixel_hash % 10000).astype(np.float32) / 10000.0
        bump = (pixel_rand < frac).astype(np.int32)
        surface_out[carve_mask] -= (lake_d_floor.astype(np.int32) + bump)

        river_meta[lake_mask & above_sea] = CHAN_LAKE

        # Remove isolated lake blobs that don't touch a tile edge or
        # the main lake body.  Small terrain-intersection artifacts.
        from scipy.ndimage import label as _label_lake
        lake_water = river_meta == CHAN_LAKE
        lk_labeled, lk_n = _label_lake(lake_water)
        if lk_n > 1:
            edge_lk = np.zeros((H, W), dtype=bool)
            edge_lk[0, :] = True; edge_lk[-1, :] = True
            edge_lk[:, 0] = True; edge_lk[:, -1] = True
            min_lake_blob = int(geo.get("min_lake_blob_px", 2000))
            for lid in range(1, lk_n + 1):
                lcomp = lk_labeled == lid
                if not (lcomp & edge_lk).any() and lcomp.sum() < min_lake_blob:
                    river_meta[lcomp] = CHAN_NONE
                    lake_mask[lcomp] = False

    # (Old Section 3f removed — lake-river connection now runs after
    # NMS centerline extraction in Section 4d below.)

    # ── 4. River centerline — precomputed global corridor + 50k NMS ─────
    # The precomputed hydro_centerline.tif (from hydrology_precompute.py)
    # defines WHERE channels exist at 1:8 scale, NMS'd and suppressed
    # globally — zero tile seams by construction.
    #
    # At 50k per-tile, we refine within this corridor using the full-res
    # flow field.  The corridor constrains the channel path; 50k flow
    # provides sub-8px organic detail.
    centerline_raw = order_u8 > 0

    flow_refine_radius = int(geo.get("flow_refine_radius", 8))
    flow_nms_size = int(geo.get("flow_nms_window", 9))
    flow_nms_frac = float(geo.get("flow_nms_frac", 0.85))
    blob_min_large_px = int(geo.get("blob_min_large_px", 500))

    # Recover precomputed centerline (uint8 Strahler order, NEAREST from 1:8)
    _has_precomputed = (hydro_centerline is not None
                        and hydro_centerline.max() > 0)
    if _has_precomputed:
        precomp_cl = _denorm_u8(hydro_centerline)  # back to uint8 order values
    else:
        precomp_cl = None

    wadi_channel = None  # set by precomputed path if wadi pixels exist
    _has_any_river = (centerline_raw.any()
                      or (precomp_cl is not None and precomp_cl.max() > 0))
    if (_has_any_river and flow_tile is not None
            and flow_tile.max() > 0.01):
        if precomp_cl is not None:
            # ── 4a. Spline-rasterized channel boundary ──────────────
            # The meander splines from hydrology_precompute.py define
            # smooth river curves at floating-point precision.  Instead
            # of NEAREST-upscaling a blocky 1:8 binary mask, we evaluate
            # those splines directly at 50k resolution per-tile.
            #
            # This is the river equivalent of lake terrain intersection:
            # lakes use height_50k < spill_elev for smooth boundaries,
            # rivers use splev(tck) → rasterize_circles for smooth curves.
            #
            # precomp_cl still used for: braid fill, wadi classification,
            # blob removal, and order assignment.

            braid_fill_mask = precomp_cl == 255
            wadi_mask = precomp_cl == 128
            thin_corridor = (precomp_cl > 0) & (precomp_cl < 128)
            thin_corridor |= (precomp_cl > 128) & (precomp_cl < 255)

            # Load spline data (saved by meander_rivers in precompute)
            import pickle
            from scipy.interpolate import splev
            _spline_path = (masks_dir / "river_splines.pkl"
                            if masks_dir is not None else None)
            _splines_loaded = []
            _SCALE = 8  # 1:8 → 50k
            if _spline_path is not None and _spline_path.exists():
                with open(_spline_path, "rb") as _sf:
                    _spline_bundle = pickle.load(_sf)
                _SCALE = _spline_bundle.get("scale", 8)
                _splines_loaded = _spline_bundle.get("branches", [])

            # Tile bounding box in 1:8 coordinates
            _tx = tile_x if tile_x is not None else 0
            _tz = tile_z if tile_z is not None else 0
            tile_y0_18 = _tz * H / _SCALE  # convert 50k tile coords to 1:8
            tile_y1_18 = tile_y0_18 + H / _SCALE
            tile_x0_18 = _tx * W / _SCALE
            tile_x1_18 = tile_x0_18 + W / _SCALE
            # Pad by max expected river radius at 1:8 (~8px)
            _PAD_18 = 10.0

            # Rasterize splines at 50k resolution onto this tile
            spline_channel = np.zeros((H, W), dtype=bool)
            spline_order = np.zeros((H, W), dtype=np.uint8)
            _n_rasterized = 0

            for spl in _splines_loaded:
                tck = spl.get("tck")
                if tck is None:
                    continue
                widths_18 = spl.get("widths_18", [])
                spl_order = spl.get("order", 1)

                # Quick bounding box check: evaluate a few points
                u_check = np.linspace(0, 1, 20)
                x_ck, y_ck = splev(u_check, tck)
                if (y_ck.max() < tile_y0_18 - _PAD_18 or
                    y_ck.min() > tile_y1_18 + _PAD_18 or
                    x_ck.max() < tile_x0_18 - _PAD_18 or
                    x_ck.min() > tile_x1_18 + _PAD_18):
                    continue  # spline doesn't intersect this tile

                # Evaluate spline at high density (1 point per 50k pixel)
                # Estimate arc length at 50k scale
                dx = np.diff(x_ck * _SCALE)
                dy = np.diff(y_ck * _SCALE)
                arc_50k = np.sum(np.sqrt(dx**2 + dy**2))
                n_pts = max(int(arc_50k * 1.5), 100)
                u_dense = np.linspace(0, 1, n_pts)
                sx, sy = splev(u_dense, tck)  # 1:8 coords

                # Convert to 50k tile-local coordinates
                px_x = sx * _SCALE - _tx * W   # tile-local X
                px_y = sy * _SCALE - _tz * H   # tile-local Y (row)

                # Interpolate width profile along spline
                if len(widths_18) > 1:
                    w_indices = np.linspace(0, len(widths_18) - 1, n_pts)
                    widths_arr = np.array(widths_18, dtype=np.float32)
                    radii_18 = np.interp(w_indices, np.arange(len(widths_18)),
                                         widths_arr)
                elif len(widths_18) == 1:
                    radii_18 = np.full(n_pts, widths_18[0], dtype=np.float32)
                else:
                    radii_18 = np.full(n_pts, 1.0, dtype=np.float32)
                # Scale radii to 50k
                radii_50k = radii_18 * _SCALE

                # Rasterize circles at each point
                for k in range(n_pts):
                    cx = px_x[k]
                    cy = px_y[k]
                    r = max(radii_50k[k], 1.0)

                    # Skip if circle is entirely outside tile
                    if (cx + r < 0 or cx - r >= W or
                        cy + r < 0 or cy - r >= H):
                        continue

                    ir = int(np.ceil(r))
                    y0 = max(0, int(cy) - ir)
                    y1 = min(H, int(cy) + ir + 1)
                    x0 = max(0, int(cx) - ir)
                    x1 = min(W, int(cx) + ir + 1)
                    if y1 <= y0 or x1 <= x0:
                        continue

                    # Build distance mask for this circle
                    yy = np.arange(y0, y1, dtype=np.float32) - cy
                    xx = np.arange(x0, x1, dtype=np.float32) - cx
                    dyy, dxx = np.meshgrid(yy, xx, indexing='ij')
                    circle = (dyy**2 + dxx**2) <= r**2
                    spline_channel[y0:y1, x0:x1] |= circle
                    spline_order[y0:y1, x0:x1] = np.where(
                        circle & (spline_order[y0:y1, x0:x1] < spl_order),
                        spl_order, spline_order[y0:y1, x0:x1])

                _n_rasterized += 1

            # Braid fill: Gaussian-smooth the NEAREST-upscaled binary mask.
            # Braids are wide solid areas — gaussian blur at sigma=5
            # (half the 8px staircase period) smooths the boundary into
            # organic curves.  Threshold 0.5 = midpoint of transition =
            # same boundary position, just smooth.  Safe because braids
            # are wide enough that blur can't eat them.
            if braid_fill_mask.any():
                braid_smooth = gaussian_filter(
                    braid_fill_mask.astype(np.float32), sigma=5.0)
                braid_water = braid_smooth > 0.5
            else:
                braid_water = braid_fill_mask

            # Wadi: carve terrain but mark as dry
            wadi_channel = (binary_dilation(wadi_mask, iterations=2)
                            if wadi_mask.any() else wadi_mask)

            # Merge: spline channels + braid fill + wadi
            # Use spline channel if splines were loaded, fall back to
            # precomputed corridor otherwise
            if _n_rasterized > 0:
                centerline = spline_channel | braid_water | wadi_channel
                order_u8 = spline_order.copy()
            else:
                # Fallback: use precomputed centerline (blocky but works)
                nms_corridor = binary_dilation(thin_corridor, iterations=2)
                flow_corr = np.where(nms_corridor, flow_tile, 0.0)
                flow_peak = maximum_filter(flow_corr, size=flow_nms_size)
                nms_centerline = (nms_corridor
                                  & (flow_tile >= flow_peak * flow_nms_frac)
                                  & (flow_tile > 0.001))
                centerline = nms_centerline | braid_water | wadi_channel
                order_u8 = precomp_cl.copy()

            # Order: braid fill gets max local order
            order_u8[braid_water & (order_u8 == 255)] = 0
            order_u8[braid_water & (order_u8 == 0)] = np.uint8(
                max(int(order_u8[thin_corridor].max())
                    if thin_corridor.any() else 3, 3))
            order_u8[wadi_channel & (order_u8 == 128)] = np.uint8(1)
            # Propagate order to any spline pixels that didn't get it
            if _n_rasterized > 0:
                needs_order = centerline & (order_u8 == 0)
                if needs_order.any():
                    order_prop_wide = maximum_filter(
                        order_u8.astype(np.float32), size=15)
                    order_u8[needs_order] = np.round(
                        order_prop_wide[needs_order]).astype(np.uint8)
                    order_u8[needs_order & (order_u8 == 0)] = 1

        else:
            # ── 4a-fallback. No precomputed centerline — use order ───
            # Legacy path: NMS within dilated order>0 corridor
            flow_refine_radius = int(geo.get("flow_refine_radius", 8))
            corridor = binary_dilation(centerline_raw,
                                       iterations=flow_refine_radius)
            flow_corr = np.where(corridor, flow_tile, 0.0)
            flow_peak = maximum_filter(flow_corr, size=flow_nms_size)
            centerline = (corridor
                          & (flow_tile >= flow_peak * flow_nms_frac)
                          & (flow_tile > 0.001))

        if centerline.sum() < 50:
            centerline = centerline_raw.copy()

        # ── 4c. Blob removal ─────────────────────────────────────────
        # With precomputed corridor: skip — global NMS already filtered.
        # Without: remove small interior fragments only.
        if not _has_precomputed:
            from scipy.ndimage import label as _label_conn
            cl_labeled, n_comps = _label_conn(centerline)
            if n_comps > 1:
                edge_mask = np.zeros((H, W), dtype=bool)
                edge_mask[0, :] = True; edge_mask[-1, :] = True
                edge_mask[:, 0] = True; edge_mask[:, -1] = True
                for cid in range(1, n_comps + 1):
                    comp = cl_labeled == cid
                    if (comp & edge_mask).any():
                        continue
                    if comp.sum() < blob_min_large_px:
                        centerline[comp] = False

        # ── 4c2. (No morphological smoothing needed) ────────────────
        # Flow-threshold boundaries are already smooth at 50k — the
        # flow gradient defines organic contours natively, like lake
        # terrain intersection.  Just clean up order on non-channel px.
        order_u8[~centerline] = 0
    else:
        centerline = centerline_raw.copy()

    # ── 4d. Lake inflow / outflow channels ──────────────────────────────
    # Connect lakes to the river network using flow-guided least-cost
    # paths.  The flow field from Gaea encodes natural drainage topology
    # — paths follow real valley lines without artificial meander.
    #
    #   Outflow:  spill point → nearest river  (one per lake)
    #   Inflow:   river endpoint → lake shore  (one per approaching river)
    #
    # Cost surface = 1/(flow + concavity + ε).  High flow = low cost,
    # so paths follow natural drainage channels.  Concavity (terrain
    # curvature) guides paths through valleys even where flow is weak.
    # Funnel taper widens channel at lake mouth and river junction.

    lake_outlet_max = int(geo.get("lake_outlet_max_dist", 300))
    lake_inlet_max  = int(geo.get("lake_inlet_max_dist", 300))
    lake_water = river_meta == CHAN_LAKE

    # Snapshot centerline BEFORE connectivity channels — diff = connectivity pixels
    _cl_before_conn = centerline.copy()

    if lake_water.any() and centerline.any():
        from scipy.ndimage import label as _label_outlet

        # ── Build cost surface ───────────────────────────────────────
        # Concavity = local mean height − actual height.  Positive in
        # valleys (concave up), zero on ridges.  Guides paths through
        # valleys even where flow accumulation is negligible.
        if height_norm is not None:
            h_local_mean = gaussian_filter(height_norm, sigma=3.0)
            concavity = np.maximum(h_local_mean - height_norm, 0.0)
        else:
            concavity = np.zeros((H, W), dtype=np.float32)
        path_cost = 1.0 / (flow_tile + concavity * 5.0 + 0.001)
        # Penalise crossing existing lake water (paths should go around,
        # not through the lake interior)
        path_cost[lake_water] *= 50.0

        dist_to_river_cl = distance_transform_edt(
            ~centerline).astype(np.float32)

        # Identify the largest centerline component — outflow should
        # target the main river body, not small edge-touching fragments.
        cl_labeled_out, cl_n_out = _label_outlet(centerline)
        main_river = np.zeros((H, W), dtype=bool)
        if cl_n_out > 0:
            biggest_size, biggest_id = 0, 1
            for cid in range(1, cl_n_out + 1):
                s = (cl_labeled_out == cid).sum()
                if s > biggest_size:
                    biggest_size, biggest_id = s, cid
            main_river = cl_labeled_out == biggest_id
        dist_to_main = distance_transform_edt(~main_river).astype(np.float32)

        lk_out_labeled, lk_out_n = _label_outlet(lake_water)

        tile_interior = np.ones((H, W), dtype=bool)
        tile_interior[0, :] = False; tile_interior[-1, :] = False
        tile_interior[:, 0] = False; tile_interior[:, -1] = False

        for lid in range(1, lk_out_n + 1):
            lcomp = lk_out_labeled == lid

            # ── 4d-i. OUTFLOW — spill point to river ─────────────────
            perim = binary_dilation(lcomp, iterations=1) & ~lcomp
            perim_above = perim & (surface_out > SEA_LEVEL) & tile_interior
            if not perim_above.any():
                continue
            perim_rows, perim_cols = np.where(perim_above)
            if height_norm is not None:
                perim_h = height_norm[perim_rows, perim_cols]
            else:
                perim_h = surface_out[perim_rows, perim_cols].astype(np.float32)

            # Biased spill point: low terrain + close to river
            h_range = max(float(perim_h.max() - perim_h.min()), 1e-6)
            h_norm_score = (perim_h - perim_h.min()) / h_range
            perim_dist = dist_to_river_cl[perim_rows, perim_cols]
            d_range = max(float(perim_dist.max() - perim_dist.min()), 1e-6)
            d_norm_score = (perim_dist - perim_dist.min()) / d_range
            spill_score = h_norm_score * 0.3 + d_norm_score * 0.7
            sp_idx = np.argmin(spill_score)
            sr, sc = int(perim_rows[sp_idx]), int(perim_cols[sp_idx])

            # Track outflow endpoint so inflow channels avoid it
            outflow_end_r, outflow_end_c = -1, -1

            if not main_river[sr, sc] and dist_to_main[sr, sc] <= lake_outlet_max:
                result = _least_cost_path(path_cost, sr, sc, main_river)
                if result is not None:
                    pr, pc = result
                    _draw_tapered_channel(
                        centerline, order_u8, pr, pc, H, W,
                        taper_frac=0.16, taper_max_w=3, channel_order=1)
                    outflow_end_r = int(pr[-1])
                    outflow_end_c = int(pc[-1])

            # ── 4d-ii. INFLOW — river endpoints to lake ──────────────
            # Find centerline components near this lake.  For each, trace
            # a channel from the river to the lake shore.
            #
            # Key constraint: inflow channels must land on a DIFFERENT
            # part of the lake perimeter than the outflow.  An exclusion
            # zone around the outflow spill point + a minimum angular
            # separation prevent the channels from merging.
            dist_to_this_lake = distance_transform_edt(
                ~lcomp).astype(np.float32)
            near_cl = centerline & (dist_to_this_lake <= lake_inlet_max)
            # Exclude pixels already touching the lake or within the
            # outflow channel (which was just drawn onto centerline)
            touching_lake = centerline & (dist_to_this_lake <= 1.5)
            near_cl &= ~touching_lake

            if near_cl.any():
                near_labeled, n_near = _label_outlet(near_cl)

                # Lake centroid for angular separation test
                lk_rows, lk_cols = np.where(lcomp)
                lake_cr = float(lk_rows.mean())
                lake_cc = float(lk_cols.mean())
                # Outflow angle from lake center
                outflow_angle = np.arctan2(sr - lake_cr, sc - lake_cc)

                # Exclusion zone: block inflow landing within 60px of
                # the outflow spill point so they can't merge
                INFLOW_EXCL = 60
                excl_zone = np.zeros((H, W), dtype=bool)
                if outflow_end_r >= 0:
                    # Exclude around both the spill point and the
                    # outflow path endpoint on the river
                    for er, ec in [(sr, sc), (outflow_end_r, outflow_end_c)]:
                        r0 = max(er - INFLOW_EXCL, 0)
                        r1 = min(er + INFLOW_EXCL + 1, H)
                        c0 = max(ec - INFLOW_EXCL, 0)
                        c1 = min(ec + INFLOW_EXCL + 1, W)
                        excl_zone[r0:r1, c0:c1] = True

                # Inflow-eligible lake boundary: perimeter pixels NOT
                # in the exclusion zone and at least 45° angular
                # separation from the outflow
                lake_boundary = perim_above | (perim & ~tile_interior)
                inflow_boundary = lake_boundary & ~excl_zone

                # Angular filter: only allow landing on the far side
                if inflow_boundary.any():
                    ib_rows, ib_cols = np.where(inflow_boundary)
                    ib_angles = np.arctan2(
                        ib_rows.astype(np.float64) - lake_cr,
                        ib_cols.astype(np.float64) - lake_cc)
                    ang_diff = np.abs(ib_angles - outflow_angle)
                    ang_diff = np.minimum(ang_diff, 2 * np.pi - ang_diff)
                    # Keep only perimeter pixels >= 45° away from outflow
                    MIN_ANG_SEP = np.radians(45.0)
                    far_enough = ang_diff >= MIN_ANG_SEP
                    if far_enough.any():
                        inflow_boundary_filtered = np.zeros((H, W), dtype=bool)
                        inflow_boundary_filtered[
                            ib_rows[far_enough], ib_cols[far_enough]] = True
                    else:
                        inflow_boundary_filtered = inflow_boundary
                else:
                    inflow_boundary_filtered = lake_boundary

                for nid in range(1, n_near + 1):
                    ncomp = near_labeled == nid
                    if ncomp.sum() < 100:
                        continue  # too small to be a real river
                    # Pick the pixel in this component closest to the lake
                    nc_rows, nc_cols = np.where(ncomp)
                    nc_dists = dist_to_this_lake[nc_rows, nc_cols]
                    best_i = np.argmin(nc_dists)
                    ir, ic = int(nc_rows[best_i]), int(nc_cols[best_i])

                    # Skip if this river endpoint is inside the outflow
                    # exclusion zone (same river the outflow connects to)
                    if excl_zone[ir, ic]:
                        continue

                    # Trace from river endpoint to filtered lake boundary
                    result = _least_cost_path(
                        path_cost, ir, ic, inflow_boundary_filtered)
                    if result is not None:
                        ipr, ipc = result
                        inlet_order = max(int(order_u8[ir, ic]), 1)
                        _draw_tapered_channel(
                            centerline, order_u8, ipr, ipc, H, W,
                            taper_frac=0.20, taper_max_w=2,
                            channel_order=inlet_order)

    # Connectivity channel mask: everything added by Section 4d
    conn_channel_mask = centerline & ~_cl_before_conn

    # Classify by Strahler order
    is_stream = centerline & (order_u8 <= 2)
    is_river  = centerline & (order_u8 >= 3)

    # ── 5. Channel = NMS centerline directly (no width expansion) ────────
    # The NMS ridgeline IS the channel.  Carve depth by Strahler order.
    river_channel = centerline & (surface_out > SEA_LEVEL) & ~lake_mask

    min_depths = geo.get("min_depths_by_order", {
        "1": 3, "2": 3, "3": 4, "4": 5, "5": 6,
    })
    max_carve = float(geo.get("max_carve_depth", 7))
    river_depth_map = np.zeros((H, W), dtype=np.float32)

    if river_channel.any():
        for order_val in range(1, 6):
            omask = river_channel & (order_u8 == order_val)
            if omask.any():
                d = float(min_depths.get(str(order_val), order_val + 1))
                river_depth_map[omask] = min(d, max_carve)

        # Pixels with order 0 but on centerline (from propagation)
        # get depth 2
        no_order = river_channel & (order_u8 == 0)
        river_depth_map[no_order] = 2.0

        # Connectivity channels need minimum 3 blocks depth for visible
        # water fill (water_y = pre_carve - 1, floor = pre_carve - depth;
        # depth >= 2 needed for at least 1 water block above floor).
        conn_river = conn_channel_mask & river_channel
        if conn_river.any():
            river_depth_map[conn_river] = np.maximum(
                river_depth_map[conn_river], 3.0)

    # ── 6b. (Removed — stochastic jitter replaced by morphological
    #         smoothing in Section 4c2) ───────────────────────────────────

    # ── 6c. Concave depth profile ────────────────────────────────────────
    # Instead of flat carve depth, use distance from channel edge for
    # parabolic cross-section: deepest at center, shallow at banks.
    if river_channel.any():
        chan_dist = distance_transform_edt(river_channel).astype(np.float32)
        chan_max_dist = max(float(chan_dist.max()), 1.0)
        # Normalize to [0, 1] — 0 at edge, 1 at center
        depth_profile = np.clip(chan_dist / min(chan_max_dist, 8.0), 0, 1)
        # Bowled parabola: steep walls, flat deep center.
        # profile^0.5 = aggressive bowl (edges shallow, center deep).
        # 0.25 base keeps at least 25% depth even at edges → visible water.
        bowl = 0.25 + 0.75 * depth_profile[river_channel] ** 0.5

        # Flow-based depth modulation: higher flow = deeper erosion.
        # This mimics how lakes get organic depth from terrain — rivers
        # get it from flow accumulation (more water = more erosion).
        if flow_tile is not None:
            flow_in_chan = flow_tile[river_channel]
            f_max = max(float(flow_in_chan.max()), 0.001)
            flow_frac = flow_in_chan / f_max
            # Higher flow: up to 40% more depth; lower flow: 20% less
            flow_mod = 0.8 + 0.4 * flow_frac
        else:
            flow_mod = 1.0

        river_depth_map[river_channel] *= bowl * flow_mod

    # ── 7. Carve river channels (S70: spline-smoothed water_y) ────────────
    # S70 Item P: smooth target water_y along the river skeleton so adjacent
    # centerline pixels at different terrain heights end up at the SAME
    # water level.  Without this, the water surface tilts visibly across the
    # channel width on hillsides — see "River challenge.png" in worktree root
    # for the bug.  5x5 minimum filter on river-only mask gives "moving min
    # along flow" since the centerline is thin (1-2 px) and 5x5 catches
    # immediate upstream/downstream pixels.  Banks dig DOWN to the smoothed
    # water level; never fill UP where terrain is already lower.
    if river_channel.any():
        from scipy.ndimage import minimum_filter as _mf_water_y
        pre_carve_water_y = surface_out.astype(np.float32) - river_depth_map
        # Replace non-river pixels with +inf so they don't contaminate the min.
        pre_carve_water_y = np.where(river_channel, pre_carve_water_y, np.float32(1e6))
        smoothed_water_y = _mf_water_y(pre_carve_water_y, size=5)

        # carve_depth per river pixel = surface_out - smoothed_water_y
        carve_depth = np.zeros((H, W), dtype=np.int32)
        _diff = surface_out[river_channel].astype(np.float32) - smoothed_water_y[river_channel]
        carve_depth[river_channel] = np.round(_diff).astype(np.int32)
        carve_depth[river_channel & (carve_depth < 1)] = 1

        max_allowed = surface_out - (BEDROCK_Y + 3)
        carve_depth = np.minimum(carve_depth, max_allowed)
        carve_depth = np.maximum(carve_depth, 0)

        carve_px = river_channel & (carve_depth > 0)
        surface_out[carve_px] -= carve_depth[carve_px]

        # Set river_meta for channel pixels
        not_lake = ~lake_mask
        order_prop_k2 = min(flow_refine_radius * 2 + 1, 31)
        order_propagated = maximum_filter(
            order_u8.astype(np.float32), size=order_prop_k2
        ).astype(np.uint8)

        stream_expanded = (order_propagated <= 2) & carve_px & not_lake
        river_expanded  = (order_propagated >= 3) & carve_px & not_lake
        river_meta[stream_expanded] = CHAN_STREAM
        river_meta[river_expanded]  = CHAN_RIVER

        # Wadi channels: carved terrain but no water fill — sand surface
        if wadi_channel is not None and wadi_channel.any():
            wadi_carved = wadi_channel & carve_px & not_lake
            river_meta[wadi_carved] = CHAN_WADI

    # ── 7a. River bank leveling ─────────────────────────────────────────
    # The 1-2 block border of land along rivers should be at the same Y
    # as the water surface, not 1 above.  Lower bank pixels to match the
    # pre-carve surface minus 1 (= water level for terrain-following rivers).
    river_or_stream_7a = (river_meta == CHAN_RIVER) | (river_meta == CHAN_STREAM)
    if river_or_stream_7a.any():
        bank_dist = distance_transform_edt(~river_or_stream_7a).astype(np.float32)
        # 2-pixel-wide border of land touching the river
        bank_border = (~river_or_stream_7a & ~lake_mask
                       & (bank_dist <= 2) & (bank_dist > 0)
                       & above_sea)
        if bank_border.any():
            # Target: water surface = pre_surface - 1 (terrain-following)
            # Lower bank to match that level
            # Use the nearest river pixel's pre-carve surface as reference
            river_surface_ref = maximum_filter(
                np.where(river_or_stream_7a,
                         surface_out + np.round(river_depth_map).astype(np.int32),
                         0).astype(np.float32),
                size=5)
            target_y = river_surface_ref[bank_border] - 1
            current_y = surface_out[bank_border].astype(np.float32)
            # Only lower, never raise
            lower = current_y > target_y
            lowered = bank_border.copy()
            lowered_rows, lowered_cols = np.where(bank_border)
            surface_out[lowered_rows[lower], lowered_cols[lower]] = np.round(
                target_y[lower]).astype(surface_out.dtype)

    # ── 7b. Smooth depth at river-lake junctions ──────────────────────────
    # Where a river channel meets the lake bowl, the independent carving
    # can produce a vertical wall (river at depth 4, adjacent lake at depth
    # 0-1 near its edge).  Blend surface_out in a narrow zone around each
    # junction so the floor ramps smoothly between them.
    river_or_stream = (river_meta == CHAN_RIVER) | (river_meta == CHAN_STREAM)
    lake_water_meta = river_meta == CHAN_LAKE
    if river_or_stream.any() and lake_water_meta.any():
        # Find lake pixels within 20 blocks of a river pixel and vice versa
        JUNCTION_R = 20
        dist_lake_to_river = distance_transform_edt(~river_or_stream).astype(np.float32)
        dist_river_to_lake = distance_transform_edt(~lake_water_meta).astype(np.float32)
        # Junction zone: lake-edge pixels near river OR river pixels near lake
        junction = (
            ((lake_water_meta & (dist_lake_to_river <= JUNCTION_R))
            | (river_or_stream & (dist_river_to_lake <= JUNCTION_R)))
            & above_sea
        )
        if junction.any():
            # Apply a localised Gaussian blur to surface_out in the junction
            # zone to ramp the floor smoothly.  Only modify junction pixels.
            # Weighted blend: stronger near the contact line, tapering out.
            blurred = gaussian_filter(surface_out.astype(np.float32), sigma=8.0)
            # Blend factor: 1.0 at contact line, 0.0 at JUNCTION_R
            contact_dist = np.minimum(dist_lake_to_river, dist_river_to_lake)
            blend_t = np.clip(1.0 - contact_dist / JUNCTION_R, 0, 1)
            blended = (blend_t * blurred + (1.0 - blend_t) * surface_out.astype(np.float32))
            surface_out[junction] = np.round(blended[junction]).astype(surface_out.dtype)

    # ── 8. Bank zones (wider, for surface_decorator) ──────────────────────
    water_mask = (river_meta > 0)
    if water_mask.any():
        bank_cfg = hcfg.get("bank_widths", {})
        stream_bw = bank_cfg.get("stream", 1)
        river_bw  = bank_cfg.get("river", 2)
        lake_bw   = bank_cfg.get("lake", 2)

        stream_water = river_meta == CHAN_STREAM
        if stream_water.any():
            stream_bank = binary_dilation(
                stream_water, iterations=stream_bw
            ) & ~water_mask & (surface_out > SEA_LEVEL)
            river_meta[stream_bank] = CHAN_STREAM

        river_water = river_meta == CHAN_RIVER
        if river_water.any():
            river_bank = binary_dilation(
                river_water, iterations=river_bw
            ) & ~water_mask & (river_meta == 0) & (surface_out > SEA_LEVEL)
            river_meta[river_bank] = CHAN_RIVER

        lake_water = river_meta == CHAN_LAKE
        if lake_water.any():
            lake_bank = binary_dilation(
                lake_water, iterations=lake_bw
            ) & ~water_mask & (river_meta == 0) & (surface_out > SEA_LEVEL)
            river_meta[lake_bank] = CHAN_LAKE

    return surface_out.astype(np.int16), river_meta, conn_channel_mask
