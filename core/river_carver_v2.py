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
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Carve rivers and lakes into surface_y using precomputed hydrology masks.

    If hydro masks are not provided (None), falls back to legacy
    threshold-based carving from river_carver.py.

    Returns (surface_y_carved, river_meta, conn_channel_mask, water_y_field).
    Legacy fallback returns empty conn_channel_mask + water_y_field=-1
    everywhere (no per-pixel water surface; chunk_writer falls back to lake
    handling + standard river_meta for water emission).
    """
    # If no hydro masks AND no precomputed centerline, fall back to legacy carver
    _has_any_hydro = ((hydro_order is not None and hydro_order.max() > 0)
                      or (hydro_centerline is not None and hydro_centerline.max() > 0))
    if not _has_any_hydro:
        from core.river_carver import carve_rivers as _legacy
        sy, rm = _legacy(surface_y, flow_tile, river_tile, cfg)
        return (sy, rm,
                np.zeros(surface_y.shape, dtype=bool),
                np.full(surface_y.shape, -1, dtype=np.int16))

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
        # S80: DISABLED.  This pass was tuned for the old D8/Strahler
        # network where small NMS fragments near lakes were noise.  WP
        # findPath in S80 produces guaranteed-connected source-to-sink
        # paths; per-tile fragments of those paths can legitimately be
        # short (a few cells crossing the tile near a lake junction).
        # Absorbing them removes visible streams the user expects to see
        # ("streams not streaming" — user observation, S80 v4 walk).
        # Kept the code commented for reference / re-enable.
        #
        # river_px_raw  = order_u8 > 0
        # absorb_dist   = float(geo.get("lake_absorb_dist", 25.0))
        # min_creek_px  = int(geo.get("min_creek_pixels", 80))
        # if river_px_raw.any():
        #     dist_riv_to_lake = distance_transform_edt(~lake_raw).astype(np.float32)
        #     riv_labeled, n_riv = label(river_px_raw)
        #     for rid in range(1, n_riv + 1):
        #         comp = riv_labeled == rid
        #         comp_size = comp.sum()
        #         comp_near_lake = (dist_riv_to_lake[comp] <= absorb_dist).any()
        #         if comp_size < min_creek_px and comp_near_lake:
        #             lake_raw = lake_raw | comp  # absorb into lake

        # ── 3b. Terrain-intersection lake fill ───────────────────────────
        # The lake boundary is defined by where Gaea terrain < spill
        # elevation.  No morph/blur — the shoreline follows natural terrain
        # contours at full 50k resolution.  Basin expansion ensures the
        # test region extends past the 8x8 NEAREST staircase of the hydro
        # mask; terrain intersection clips it naturally.
        PAD = 48  # enough for basin expansion dilation
        # S80 v9: basin_expand_px was 32, propagating lake water up to
        # 32 blocks beyond the precompute basin via maximum_filter +
        # terrain-intersection.  This added ~45k mystery water cells
        # per tile and was the dominant cause of "giant streams" (some
        # lake-water bleeding far from any lake into stream
        # neighbourhoods).  Cut to 2 — just enough to bridge the 8x8
        # NEAREST staircase edge from the 1:8 mask, no further.
        basin_expand_px = int(geo.get("lake_basin_expand_px", 2))

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

            # S80 v11: spline loading DISABLED.  river_splines.pkl was
            # written by the OLD meander_rivers (Strahler/NMS pipeline)
            # and contained 660 spline branches with widths up to 4 at
            # 1:8 scale (= 32-block radii at 50k = 64-block-wide
            # trenches).  The carver rasterized these legacy splines
            # over MY WP centerlines, completely overriding wp_river_network
            # output.  WP findPath now produces the centerline + width
            # masks directly; no splines needed.
            from scipy.interpolate import splev
            _splines_loaded: list = []
            _SCALE = 8  # 1:8 → 50k

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
                # S80 v12: use precomputed centerline DIRECTLY (no flow-NMS filter).
                # The NMS-flow filter (flow >= 0.85 * peak & flow > 0.001) was
                # dropping ~55% of WP centerline cells in dry biomes (where
                # Gaea flow accumulation is ~0).  Result: 11,648 path cells →
                # carved water at only ~5,200 → 35 fragmented water blobs
                # instead of 3 connected systems.  WP findPath already
                # curates the paths through findPath's cost surface; no
                # need for flow-based re-thinning.
                centerline = thin_corridor | braid_water | wadi_channel
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

    # S80: connectivity layer DELETED.  WP findPath in hydrology_precompute
    # produces guaranteed-connected paths (mountain → lake / ocean,
    # spillpoint → next sink), so the post-hoc connectivity Dijkstra here
    # is redundant.  Conn_channel_mask remains in the API for downstream
    # post-process passes (run_pipeline.py wall-to-wall + lake-override),
    # but is empty by construction now.
    # Connectivity channel mask: everything added by Section 4d
    conn_channel_mask = centerline & ~_cl_before_conn

    # Classify by Strahler order
    is_stream = centerline & (order_u8 <= 2)
    is_river  = centerline & (order_u8 >= 3)

    # ── 5. Channel = NMS centerline directly (no width expansion) ────────
    # The NMS ridgeline IS the channel.  Section 7 (WP guardrails) computes
    # depth from per-pixel width × slope × elevation; sections 5/6 (legacy
    # `river_depth_map` + parabolic profile + flow modulation) were removed
    # in S72 — they were dead code, computed but never read.
    river_channel = centerline & (surface_out > SEA_LEVEL) & ~lake_mask

    # ── 7. Carve river channels (S71-3: WP-style guardrails) ──────────────
    # Replaces S70 5x5 minimum_filter + S71 valley smoothing β.  Adapted from
    # sijmen_v_b's WorldPainter river script.  For each centerline pixel:
    #   1. Width (block-radius) from Strahler order × hydro_width.
    #   2. avg_terrain = Gaussian-smoothed surface_y (sigma proportional to
    #      width) — captures the "mean elevation under a circle of radius
    #      width" without per-pixel circle iteration.
    #   3. slope = magnitude of smoothed surface_y gradient.
    #   4. Per-pixel in `width` radius:
    #        factor = clamp(dist_to_centerline / width, 0, 0.99)
    #        depth  = width * RIVER_DEPTH_FRAC + 1.5
    #        guard  = 4.2 * clamp(slope, DYKE_MIN, GUARDRAIL_MAX)
    #        new_y  = (1-f) * (avg - depth + f * depth * guard)
    #               + f * surface_y
    #   5. water_y = avg - 0.5 - slope_correction (lowers on wide+steep)
    # Effect: river cuts a soft valley with raised berms on slopes;
    # cross-section is naturally concave; water never sits above land.
    # S71-3 final-river: combine all centerlines (river_channel + conn_channel_mask)
    # into one footprint set so connectivity channels also get water_y_field.
    # This addresses "river deltas — land connects but water doesn't".
    river_full_mask = river_channel | (conn_channel_mask & (surface_out > SEA_LEVEL) & ~lake_mask)

    # Pre-allocate water_y_field (default -1 = no water at this pixel).
    water_y_field = np.full((H, W), -1, dtype=np.int16)

    if river_full_mask.any():
        from scipy.ndimage import gaussian_filter as _gf
        from scipy.ndimage import label as _label_grav

        # 7.1 — per-centerline width (block half-radius), from Strahler order.
        # WP-style: slope-aware width: width = base + 2 * slope^2.
        _ORDER_TO_WIDTH = {1: 2.5, 2: 3.0, 3: 4.0, 4: 5.5, 5: 7.0}
        width_at_pixel = np.zeros((H, W), dtype=np.float32)
        for _o in range(1, 6):
            _m = river_full_mask & (order_u8 == _o)
            if _m.any():
                width_at_pixel[_m] = _ORDER_TO_WIDTH.get(_o, 4.0)
        # Pixels with order 0 but on centerline (incl. conn_channel): small default
        _no_o = river_full_mask & (order_u8 == 0)
        if _no_o.any():
            width_at_pixel[_no_o] = 2.5

        # 7.1b — GRAVITY pre-pass (S72 fix).  Walk each centerline component
        # from source to ocean (descending dist-from-ocean) tracking running
        # min surface_y; drop any cell exceeding it (= upstream bump that
        # would block flow).  MUST run BEFORE avg_terrain & water_y are
        # computed so they reflect the gravity-corrected centerline.
        # JS reference: river_script1.7.js:455-481 (per-path topological walk).
        # S71 bug: argsort by surface_y (height order) instead of topological
        # order flattened the ENTIRE centerline to its global min.
        _ocean_seed = surface_out <= SEA_LEVEL
        if not _ocean_seed.any():
            # No ocean in tile — seed at lowest centerline cell so dist-EDT
            # still gives a usable topological proxy.
            _r0, _c0 = np.where(river_full_mask)
            if len(_r0) > 0:
                _min_idx = int(np.argmin(surface_out[_r0, _c0]))
                _ocean_seed = np.zeros((H, W), dtype=bool)
                _ocean_seed[_r0[_min_idx], _c0[_min_idx]] = True
        _gravity_ran = False
        if _ocean_seed.any():
            _dist_from_ocean = distance_transform_edt(~_ocean_seed).astype(np.float32)
            _grav_labeled, _grav_n = _label_grav(river_full_mask)
            _gravity_ran = True
            for _grav_iter in range(5):
                _stable = True
                for _cid in range(1, _grav_n + 1):
                    _comp = _grav_labeled == _cid
                    _r, _c = np.where(_comp)
                    if len(_r) < 2:
                        continue
                    _d = _dist_from_ocean[_r, _c]
                    _ord = np.argsort(-_d)  # source first (descending dist)
                    _min_y = np.iinfo(np.int32).max
                    for _i in _ord:
                        _rr, _cc = int(_r[_i]), int(_c[_i])
                        _y = int(surface_out[_rr, _cc])
                        if _y > _min_y:
                            surface_out[_rr, _cc] = _min_y
                            _stable = False
                        else:
                            _min_y = _y
                if _stable:
                    break

        # 7.2 — avg_terrain: Gaussian-smoothed surface_y (sigma=4 captures
        # ~7-9 block circle, matches typical river width).  Computed AFTER
        # gravity pre-pass so it reflects the monotonic-descent centerline.
        avg_terrain = _gf(surface_out.astype(np.float32), sigma=4.0)

        # 7.3 — slope magnitude (smoothed gradient)
        _sy_smooth = _gf(surface_out.astype(np.float32), sigma=2.0)
        _gy, _gx = np.gradient(_sy_smooth)
        slope_mag = np.sqrt(_gx * _gx + _gy * _gy).astype(np.float32)
        del _sy_smooth, _gy, _gx

        # WP slope-aware width: width += 2 * slope^2 (steep needs guardrail headroom)
        slope_at_centerline = np.zeros((H, W), dtype=np.float32)
        slope_at_centerline[river_full_mask] = slope_mag[river_full_mask]
        width_at_pixel = width_at_pixel + 2.0 * np.square(slope_at_centerline)

        # 7.4 — meander: simplex-noise displacement field, world-coord seamless.
        # Per WP: noise(x/1000, y/1000) but we use a scale better-tuned for our
        # 1-pixel-per-block resolution.  Strength scales with slope so flat
        # rivers wiggle less, steep rivers (which would naturally erode banks)
        # wiggle more.  Apply to all centerlines INCLUDING connectivity channels.
        try:
            import opensimplex as _ox_meander
            _tx = tile_x if tile_x is not None else 0
            _tz = tile_z if tile_z is not None else 0
            _meander_seed = (_tx * 73856093 ^ _tz * 19349663 ^ 0xC1EA5F) & 0x7FFFFFFF
            _ox_m = _ox_meander.OpenSimplex(seed=_meander_seed)
            _world_xs = ((np.arange(W) + _tx * W) / 40.0).astype(np.float64)
            _world_zs = ((np.arange(H) + _tz * H) / 40.0).astype(np.float64)
            meander_noise = _ox_m.noise2array(_world_xs, _world_zs).astype(np.float32)
            # meander_noise in [-1, 1]; scale by amplitude × slope_factor
            MEANDER_AMP = 4.0  # max ±4-block perpendicular displacement
        except ImportError:
            meander_noise = np.zeros((H, W), dtype=np.float32)
            MEANDER_AMP = 0.0

        # 7.5 — distance to nearest centerline + propagate per-pixel width
        from scipy.ndimage import distance_transform_edt as _edt_g
        dist_to_center, ind_center = _edt_g(~river_full_mask, return_indices=True)
        dist_to_center = dist_to_center.astype(np.float32)
        # Preserve indices for plateau-water-y propagation in Section 7.7b.
        # Cost: ~2 MB (H*W*int32*2) — fine for 512x512 tiles.
        nearest_idx_r = ind_center[0]
        nearest_idx_c = ind_center[1]
        nearest_width = width_at_pixel[nearest_idx_r, nearest_idx_c]
        nearest_avg   = avg_terrain[nearest_idx_r, nearest_idx_c]
        nearest_slope = slope_mag[nearest_idx_r, nearest_idx_c]
        del ind_center

        # Apply meander displacement to dist_to_center: shifts the deepest cut
        # ±MEANDER_AMP perpendicularly (in noise-space, not flow-space — close
        # enough for organic-looking wobble at 50k scale).
        if MEANDER_AMP > 0:
            meander_shift = meander_noise * MEANDER_AMP * np.minimum(nearest_slope * 4, 1.0)
            dist_to_center = np.abs(dist_to_center - meander_shift)

        # 7.6 — guardrails formula (WP-style)
        # S72 — Bug 4 + Bug 6 trench depth tuning:
        #   * RIVER_DEPTH_FRAC 0.35 → 0.25 and base bias 1.5 → 1.0 (shallower).
        #   * slope_atten = clamp(1 - 0.5*slope, 0.4, 1.0) — shallower on steep
        #     terrain (geomorphically correct: young rivers cut shallow).
        #   * elev_atten = clamp((avg - SEA_LEVEL) / 30, 0.3, 1.0) — shallower
        #     near ocean for delta-fan look (matches JS depthMultiplier).
        RIVER_DEPTH_FRAC = 0.25
        DEPTH_BASE = 1.0
        DYKE_MIN = 0.32
        GUARDRAIL_MAX_SLOPE = 1.2
        factor = np.clip(dist_to_center / np.maximum(nearest_width, 1.0), 0.0, 0.99)
        slope_atten = np.clip(1.0 - 0.5 * nearest_slope, 0.4, 1.0).astype(np.float32)
        elev_above_sea = np.maximum(nearest_avg - float(SEA_LEVEL), 0.0).astype(np.float32)
        elev_atten = np.clip(elev_above_sea / 30.0, 0.3, 1.0).astype(np.float32)
        depth_blocks = (nearest_width * RIVER_DEPTH_FRAC + DEPTH_BASE) * slope_atten * elev_atten
        # S73-v9: trench drop = 1 (user-requested middle ground between v7's
        # +2 and v8's +0).  River bed sits 1 block deeper than S72 base,
        # water_y drops 1 to match (see 7.7 below).
        depth_blocks += 1.0
        guard = 4.2 * np.maximum(DYKE_MIN, np.minimum(GUARDRAIL_MAX_SLOPE, nearest_slope))
        new_y_f = (1.0 - factor) * (nearest_avg - depth_blocks + factor * depth_blocks * guard) \
                + factor * surface_out.astype(np.float32)

        # Footprint: pixels within `width` of any centerline (factor < 1)
        footprint = (dist_to_center <= nearest_width) & ~lake_mask & above_sea

        # Only LOWER (never raise) and clamp to bedrock min
        cur_y_f = surface_out.astype(np.float32)
        new_y_f = np.minimum(new_y_f, cur_y_f)
        new_y_f = np.maximum(new_y_f, float(BEDROCK_Y + 3))

        # Apply within footprint
        if footprint.any():
            surface_out[footprint] = np.round(new_y_f[footprint]).astype(surface_out.dtype)

        # 7.7 — Water level: avg - 0.5 - slope_correction (lowers on wide+steep)
        SLOPE_CORR_START_W = 7.0
        SLOPE_CORR_FALLOFF = 4.0
        width_excess = np.maximum(nearest_width - SLOPE_CORR_START_W, 0.0)
        slope_correction = (np.clip(width_excess / SLOPE_CORR_FALLOFF, 0.0, 1.0)
                            * np.maximum(np.minimum(nearest_slope * 4.0, 1.0), 0.25))
        water_y = nearest_avg - 0.5 - 1.3 * slope_correction
        # S73-v9: trench drop = 1 (matches depth_blocks += 1 above).
        # Subtract BEFORE the SEA_LEVEL clamp so coast segments still meet
        # the ocean cleanly (clamp catches anything below 63).
        water_y = water_y - 1.0
        # S73-v7: 1D smoothing along each centerline component's flow path.
        # 2D gaussian (v6) blurred across cross-sections at curves and
        # produced TILTED water surfaces (one side of channel higher than
        # the other).  The fix: smooth water_y values ALONG the centerline
        # path only (1D), then re-propagate via EDT.  Result: every Voronoi
        # cell takes its nearest centerline pixel's smoothed water_y, and
        # neighbors-along-path are heavily smoothed → adjacent Voronoi
        # cells have nearly-identical water_y → cross-sections (which
        # span 2-3 path pixels at curves) all share the same water_y → MC
        # sees ONE uniform water surface across each cross-section, with
        # Y drops only along flow direction.
        if _gravity_ran:
            from scipy.ndimage import gaussian_filter1d as _gf1
            for _cid in range(1, _grav_n + 1):
                _comp = _grav_labeled == _cid
                _r_arr, _c_arr = np.where(_comp)
                if len(_r_arr) < 2:
                    continue
                _d_arr = _dist_from_ocean[_r_arr, _c_arr]
                _ord = np.argsort(-_d_arr)  # source first
                _r_ord = _r_arr[_ord]
                _c_ord = _c_arr[_ord]
                _path_y = water_y[_r_ord, _c_ord].astype(np.float32)
                # sigma=8: ~16-cell support along path → adjacent path
                # pixels round to same int (longer implicit plateaus, no
                # cross-section tilt).
                _path_y_smooth = _gf1(_path_y, sigma=8.0, mode='reflect')
                water_y[_r_ord, _c_ord] = _path_y_smooth
            # Propagate smoothed centerline water_y to ALL footprint cells
            # via existing EDT: every pixel takes its nearest centerline's
            # water_y → uniform per Voronoi cell → uniform per cross-section.
            water_y = water_y[nearest_idx_r, nearest_idx_c]
        # S72 — Bug 1 fix: clamp to SEA_LEVEL (matches JS `Math.max(..., minWaterDepth)`),
        # NOT `surface_out + 1`.  The old clamp coupled water_y to the
        # gravity-flattened centerline, dragging the entire water surface flat
        # along long stretches.  The Pass 3 narrow-section fix (below) handles
        # "surface poking through water" by lowering surface, not raising water.
        water_y = np.maximum(water_y, float(SEA_LEVEL))

        # 7.7b — Plateau quantization REMOVED (S73-v5).  Tried in S73 v1-v4
        # but explicit plateau-stepping concentrates MC's water cascade
        # artifacts at plateau boundaries (visible as "ghost weirs" — 7-cell
        # bands of flowing water at the higher plateau's Y above the lower
        # plateau's source).  Reverting to per-pixel water_y matches the JS
        # WorldPainter approach: nearest_avg propagation via EDT already
        # gives every cross-section a uniform water_y, and MC's int-rounding
        # creates IMPLICIT mini-plateaus (~2-5 cells) distributed organically
        # along the river — each cascade tier is just 1 row long, visually
        # subtle.  Bank-lift (size=15) + interior hole fill below still
        # contain water laterally.


        # 7.8 — populate water_y_field for chunk_writer.
        # S73-v7: fill ENTIRE trench (was: factor < edge_threshold gate).
        # Water_y_field is set for every footprint cell; whether water
        # shows depends on surface_y < water_y (carve dipped surface
        # below water level) vs surface_y >= water_y (terrain bank pokes
        # through).  Banks contained naturally by surface vs water_y;
        # bank-lift in 7c handles cells where surface needs to rise to
        # contain laterally.  Eliminates the edge_threshold "dry strip"
        # that left visible cross-section non-uniformity at curves.
        water_zone = footprint & ~lake_mask
        if water_zone.any():
            water_y_field[water_zone] = np.round(water_y[water_zone]).astype(np.int16)

        carve_px = footprint & (cur_y_f > new_y_f)

        # Set river_meta for footprint pixels (Strahler-aware)
        not_lake = ~lake_mask
        order_prop_k2 = min(flow_refine_radius * 2 + 1, 31)
        order_propagated = maximum_filter(
            order_u8.astype(np.float32), size=order_prop_k2
        ).astype(np.uint8)
        stream_expanded = footprint & (order_propagated <= 2) & not_lake
        river_expanded  = footprint & (order_propagated >= 3) & not_lake
        river_meta[stream_expanded] = CHAN_STREAM
        river_meta[river_expanded]  = CHAN_RIVER

        # Wadi channels: carved terrain but no water fill — sand surface
        if wadi_channel is not None and wadi_channel.any():
            from scipy.ndimage import binary_dilation as _bd_wadi
            wadi_footprint = _bd_wadi(wadi_channel, iterations=3) & footprint
            wadi_carved = wadi_footprint & not_lake
            river_meta[wadi_carved] = CHAN_WADI

        # ── 7a. Two-pass containment ─────────────────────────────────────
        # (Gravity Pass 1 was moved to Section 7.1b above — it must run
        #  BEFORE avg_terrain/water_y are computed so they see the
        #  monotonic-descent centerline.  S71 had it here AFTER, which left
        #  water_y based on pre-gravity bumpy terrain.)

        # Pass 2: edge-spillover guard.  Only at edge pixels (factor > 0.55).
        # If any 4-neighbor's water level >= my surface, raise me to that
        # neighbor's water level.  Sparse 8-neighbor max for stability.
        edge_band = footprint & (factor > 0.55) & ~river_channel
        if edge_band.any():
            # For each edge pixel, look at 4-neighbors' water_y
            from scipy.ndimage import maximum_filter as _mf3
            water_y_3 = np.where(footprint, water_y, np.float32(-1e6))
            nb_max_water = _mf3(water_y_3, size=3)
            spill_mask = edge_band & (nb_max_water > surface_out + 0.5)
            if spill_mask.any():
                surface_out[spill_mask] = np.round(nb_max_water[spill_mask]).astype(surface_out.dtype)

        # Pass 3: narrow-section fix.  Centerline pixels where surface_y >=
        # water_y - 1 → drop them to water_y - 1.  Catches per-pixel spikes
        # that survive the carve.
        if river_channel.any():
            narrow_fix = river_channel & (surface_out.astype(np.float32) >= water_y - 1)
            if narrow_fix.any():
                surface_out[narrow_fix] = np.round(water_y[narrow_fix] - 1).astype(surface_out.dtype)

        # ── 7b. Fixify spike relaxation (cheap final pass on river footprint) ──
        # Smooth 1-pixel local maxima/minima within the river footprint.
        # Average a local extremum with its 4-neighbours (clamped so it
        # doesn't go below the lowest neighbour or above the highest).
        if footprint.any():
            from scipy.ndimage import minimum_filter as _mf_minf
            from scipy.ndimage import maximum_filter as _mf_maxf
            sy_f = surface_out.astype(np.float32)
            # 3x3 cross neighborhood (size=3 = 8-connected; use cross via two
            # passes for true 4-neighbor — close enough with 3x3 box here).
            nb_min = _mf_minf(sy_f, size=3)
            nb_max = _mf_maxf(sy_f, size=3)
            is_local_max = footprint & (sy_f > nb_max - 0.5) & (sy_f - nb_min > 1.5)
            is_local_min = footprint & (sy_f < nb_min + 0.5) & (nb_max - sy_f > 1.5)
            # Average of 4-neighbors (3x3 mean minus self/9 ≈ 3x3 mean)
            # uniform_filter works fine here
            from scipy.ndimage import uniform_filter
            nb_avg = uniform_filter(sy_f, size=3)
            if is_local_max.any():
                surface_out[is_local_max] = np.round(
                    np.maximum(nb_avg[is_local_max], nb_min[is_local_max] + 0.5)
                ).astype(surface_out.dtype)
            if is_local_min.any():
                surface_out[is_local_min] = np.round(
                    np.minimum(nb_avg[is_local_min], nb_max[is_local_min] - 0.5)
                ).astype(surface_out.dtype)

        # ── 7c. DISABLED in S80 v8 ──────────────────────────────────────
        # The interior-hole-fill + bank-lift was producing ~67k extra
        # water cells per tile (footprint computation gives 14k
        # cells but MCA had 82k water cells).  binary_fill_holes
        # closes any U-shaped meander into a solid water mass; the
        # max-filter then propagates water_y outward.  For narrow
        # WP streams this turns isolated meanders into giant water
        # blobs.  Disabled entirely — narrow streams contain water
        # vertically (1-2 block deep) without needing bank-lift.
        # Re-enable only if water-escape regressions appear on
        # steep terrain.

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

    # ── 9. Delta connectivity (WP `pathFindDown` equivalent) ───────────────
    # S80 v9: DELTA CONNECTIVITY pass DISABLED.  This pre-S80 hack walked
    # downhill 80 steps from any above-sea river component tail and
    # stamped a tapered-width 1→4 footprint with carve + water_y_field.
    # It was the dominant source of "mystery water cells" (45k+ extra
    # water cells per tile not in any centerline footprint or lake).
    # WP findPath already produces source-to-sink connected paths via
    # _wp_find_path + _add_mouth_extensions, so this hack is redundant.
    # If a river component still doesn't reach a sink, the right fix is
    # to improve findPath's success rate, not bandaid with a downhill
    # walk that ignores the precompute spec.
    pass

    return surface_out.astype(np.int16), river_meta, conn_channel_mask, water_y_field
