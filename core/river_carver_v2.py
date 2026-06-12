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


def _carve_lakes_v2(pad_basin, pad_water_y, pad_terrain_y,
                    world_ox, world_oy, geo, pad_dist=None):
    """Organic lake carve (S90). Returns (water_mask, bed_y) over the padded
    window. Flatten + EDT parabolic bowl with a DOMAIN-WARPED, TERRAIN-FOLLOWING
    shoreline so the 1:8 NEAREST staircase becomes an organic outline (Nasworthy,
    not Balmorhea). ALL math is on HEIGHTS / a distance field — never the
    discrete lake-ID mask (NEAREST hard rule preserved). World-coord noise =
    seam-safe across tiles.

    - sdf      = distance-to-shore inside the basin (smooth, but staircased at the
                 raw mask edge).
    - warp     = resample sdf at world-coord low-freq-noise-displaced coords ->
                 the shore iso-contour wiggles organically, breaking the staircase.
    - terrain  = subtract (terrain - water) so ridges poking into the basin become
                 coves and flat-at-spill basins still fill fully (kills dry/striped).
    - bed      = water_y - depth(parabola of the warped/terrain-adjusted distance).
    """
    import numpy as np
    from scipy.ndimage import distance_transform_edt, map_coordinates, gaussian_filter
    hh, ww = pad_basin.shape
    lc = geo.get("lake_carve", {})
    depth_max = float(lc.get("depth_max", 18.0))
    depth_ref = float(lc.get("depth_ref_px", 40.0))
    depth_pow = float(lc.get("depth_pow", 1.6))
    smooth_sig = float(lc.get("shore_smooth_sigma", 6.0))
    warp_amp = float(lc.get("shore_warp_amp_px", 7.0))
    warp_scale = float(lc.get("shore_warp_scale_px", 26.0))
    terr_follow = float(lc.get("terrain_follow", 1.0))
    shore_thresh = float(lc.get("shore_thresh_px", 0.0))
    if not pad_basin.any():
        return np.zeros((hh, ww), bool), pad_terrain_y.copy()

    # FLAT water level (scalar) — pad_water_y is the NEAREST-stepped lake-wl mask
    # (8-block steps, 0 outside basin); using it per-cell re-injects the staircase
    # into sdf AFTER smoothing. The median over the basin is the flat lake level.
    wl_flat = float(np.median(pad_water_y[pad_basin]))

    # SIGNED distance field (+inside, -outside) — continuous across the boundary.
    # Gaussian-smoothing it dissolves the 1:8 NEAREST staircase into a smooth
    # contour (this is a DISTANCE FIELD, not the lake-ID mask, so the NEAREST
    # hard rule is preserved). The warp then adds organic variation on top.
    sdf = (distance_transform_edt(pad_basin).astype(np.float32)
           - distance_transform_edt(~pad_basin).astype(np.float32))
    if smooth_sig > 0.0:
        sdf = gaussian_filter(sdf, smooth_sig)
    _warp_coords = None
    if warp_amp > 0.0:
        try:
            from opensimplex import OpenSimplex
            _sd = int(lc.get("seed", 4111))
            yy, xx = np.mgrid[0:hh, 0:ww].astype(np.float32)
            # 2-octave domain warp: macro octave carves coves/points, fine octave
            # adds organic texture so the 1:8 staircase fully dissolves.
            def _oct(scale, a, s1, s2):
                sx = (world_ox + np.arange(ww, dtype=np.float64)) / max(2.0, scale)
                sz = (world_oy + np.arange(hh, dtype=np.float64)) / max(2.0, scale)
                n1 = OpenSimplex(seed=s1).noise2array(sx, sz).astype(np.float32)
                n2 = OpenSimplex(seed=s2).noise2array(sx, sz).astype(np.float32)
                return a * n1, a * n2
            dy1, dx1 = _oct(warp_scale, warp_amp, _sd, _sd + 777)
            dy2, dx2 = _oct(warp_scale / 3.0, warp_amp * 0.45, _sd + 13, _sd + 790)
            _warp_coords = [yy + dy1 + dy2, xx + dx1 + dx2]
            sdf = map_coordinates(
                sdf, _warp_coords,
                order=1, mode="nearest").astype(np.float32)
        except Exception:
            _warp_coords = None
    if terr_follow > 0.0:
        sdf = sdf - terr_follow * np.clip(pad_terrain_y - wl_flat, 0.0, None)

    water = sdf > shore_thresh
    # DEPTH from a GLOBAL distance-to-shore field (computed by the caller at a
    # large pad so it reaches the centre of lakes wider than a tile). Depth grows
    # ~linearly with distance-to-shore then caps -> deepens gradually all the way
    # to the centre (no flat bottom-out), scales with lake size (bigger lake =
    # deeper), and is SEAM-SAFE (a per-window EDT would truncate at the window
    # edge). pow<1 flattens the deep floor a touch; smoothing dissolves the step.
    if pad_dist is not None:
        depth_per_px = float(lc.get("depth_per_px", 0.15))
        depth_cap = float(lc.get("depth_cap", 70.0))
        depth_curve = float(lc.get("depth_curve_pow", 0.85))
        dsmooth = float(lc.get("depth_smooth_sigma", 5.0))
        _dist = np.asarray(pad_dist, np.float32)
        if dsmooth > 0.0:
            _dist = gaussian_filter(_dist, dsmooth)
        # S93: warp the DEPTH field with the SAME displacement as the
        # shoreline SDF. The depth's distance-to-shore comes from the RAW
        # 1:8 NEAREST mask, so its integer contours (the bed terraces)
        # were long straight axis-aligned ledges while the shoreline
        # above them arced organically (user walk). Sharing the warp
        # makes every depth terrace wobble in sync with the shore.
        if _warp_coords is not None:
            _dist = map_coordinates(
                _dist, _warp_coords,
                order=1, mode="nearest").astype(np.float32)
        _n = np.clip(_dist * depth_per_px / max(1.0, depth_cap), 0.0, 1.0)
        d = depth_cap * (_n ** depth_curve)
    else:
        d = depth_max * np.clip(sdf / max(1.0, depth_ref), 0.0, 1.0) ** depth_pow
    bed = np.where(water, wl_flat - d, pad_terrain_y).astype(np.float32)
    return water, bed


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
    hydro_river_bed:  np.ndarray | None = None,  # (H, W) float32 MC-Y from global 8k precompute (S83 v8)
    hydro_river_water_y: np.ndarray | None = None,  # (H, W) float32 MC-Y from skeleton walk (S83 v9)
    hydro_dist_src: np.ndarray | None = None,  # (H, W) float32 blocks from source tip (S93e taper; -1 = no data)
    hydro_dcl:      np.ndarray | None = None,  # (H, W) float32 blocks to nearest global skeleton pt (S93e v2)
    hydro_hw_cl:    np.ndarray | None = None,  # (H, W) float32 painted half-width at that skeleton pt (S93e v2)
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
    # S88: LEGACY FALLBACK REMOVED.
    #
    # Previously, when no precompute mask had data for a tile AND the painted
    # overlay didn't populate hydro_centerline (e.g. (13,82) RFC where the
    # painted river polygon sits at the tile's edge and doesn't rasterize
    # into the 50k centerline grid), this branch fell through to the
    # flow-threshold-based core/river_carver.py.
    #
    # Side effect: legacy carver hallucinates rivers from `flow > threshold`
    # at locations that DO NOT MATCH the painted/precompute rivers. It sets
    # river_meta on those wrong pixels, but does not return water_y_field,
    # so the v2 wrapper backfills water_y_field = -1 everywhere. Result:
    #   - surface_decorator sees river_meta>0 and paints bank materials
    #     (mud/coarse_dirt/grass_block/clay) at the hallucinated location
    #   - chunk_writer's river_water_mask = (abs_y <= -999) is empty -> no
    #     water placed
    #   - Visible: "ghost river" strip of dirty terrain with NO water, in
    #     the wrong place compared to where the user actually painted.
    #
    # New behaviour: if no precompute data exists for the tile, return
    # empty river_meta + empty water_y_field. No rivers carved at all.
    # Better than a ghost river in the wrong location. The painted overlay
    # is responsible for delivering hydro_centerline/hydro_depth -- if it
    # doesn't, the carver no-ops cleanly.
    _has_any_hydro = ((hydro_order is not None and hydro_order.max() > 0)
                      or (hydro_centerline is not None and hydro_centerline.max() > 0)
                      or (hydro_lake is not None and hydro_lake.max() > 0))
    if not _has_any_hydro:
        return (surface_y.copy(),
                np.zeros(surface_y.shape, dtype=np.uint8),
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
    # S84: above_sea includes painted river cells regardless of elevation.
    # Painted cells whose natural terrain dips at/below sea level (coastal
    # paint, river extending into ocean) must still receive the v17 bed
    # cache carve + water_y assignment. Without this, every `& above_sea`
    # gate downstream (footprint at l.1000, zone at l.1195, orphan at
    # l.1348, river_strict at l.1367, water_mask at l.1469) excludes
    # below-sea painted cells — and the river abruptly disappears at the
    # Y 63 contour. Bank widening at l.1503/1510/1517 uses
    # `surface_out > SEA_LEVEL` directly (not `above_sea`), so banks
    # correctly remain bounded above sea.
    above_sea = surface_out > SEA_LEVEL
    if hydro_centerline is not None:
        _painted_any = np.asarray(hydro_centerline) > 0
        if _painted_any.any():
            above_sea = above_sea | _painted_any

    if lake_raw.any():
        from scipy.ndimage import label
        _skip_stochastic = False  # S90 v2 sets True (smooth bed, no dither)

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

                crop_r0 = row_off - pz0
                crop_c0 = col_off - px0
                _bowl = bool(geo.get("lake_bowl_carve", False))
                import os as _os_lv2
                _v2 = (bool(geo.get("lake_carve", {}).get("enabled", False))
                       and not _os_lv2.environ.get("LAKE_V2_OFF"))
                if _v2:
                    # ── S90 ORGANIC LAKE CARVE ────────────────────────────────
                    # Organic de-staircased shoreline + GLOBAL hydro_lkdep depth
                    # (gradual to centre, no flat bottom-out, seam-safe). only-lower
                    # depth (clip>=0) reuses the subtract-apply path; stochastic
                    # rounding skipped (the bed is already smooth).
                    # GLOBAL distance-to-shore: read the lake mask at a LARGE pad
                    # and EDT it so distance reaches the centre of lakes wider than
                    # a tile (seam-safe gradual depth), then crop to the working pad.
                    from scipy.ndimage import distance_transform_edt as _edt_dep
                    _DPAD = int(geo.get("lake_carve", {}).get("depth_edt_pad_px", 640))
                    _dpx0 = max(col_off - _DPAD, 0); _dpz0 = max(row_off - _DPAD, 0)
                    with rasterio.open(wl_tif) as _src:
                        _dpx1 = min(col_off + W + _DPAD, _src.width)
                        _dpz1 = min(row_off + H + _DPAD, _src.height)
                        _wlb = _src.read(1, window=_RWindow(
                            _dpx0, _dpz0, _dpx1 - _dpx0, _dpz1 - _dpz0))
                    _bg = (_wlb == 0)
                    if _bg.any():
                        _distb = _edt_dep(_wlb > 0).astype(np.float32)
                    else:
                        # window entirely inside a giant lake -> no shore in reach;
                        # treat as deepest (helper caps it).
                        _distb = np.full(_wlb.shape, 1e6, np.float32)
                    _cr = pz0 - _dpz0; _cc = px0 - _dpx0
                    _pad_dist_v2 = _distb[_cr:_cr + (pz1 - pz0), _cc:_cc + (px1 - px0)]
                    _water_pad, _bed_pad = _carve_lakes_v2(
                        pad_basin, pad_water_y, pad_terrain_y, px0, pz0, geo,
                        pad_dist=_pad_dist_v2)
                    lake_mask = _water_pad[crop_r0:crop_r0+H, crop_c0:crop_c0+W]
                    _bedi = _bed_pad[crop_r0:crop_r0+H, crop_c0:crop_c0+W]
                    _terri = pad_terrain_y[crop_r0:crop_r0+H, crop_c0:crop_c0+W]
                    lake_depths = np.clip(_terri - _bedi, 0.0, None).astype(np.float32)
                    lake_shore_band = np.zeros((H, W), dtype=bool)
                    _skip_stochastic = True
                elif _bowl:
                    # ── S89-walk4 BOWL CARVE ──────────────────────────────────
                    # Flood the WHOLE precompute basin and carve its bed to
                    # (water_y - lkdep) using the precomputed parabolic
                    # bathymetry (hydro_lkdep), instead of only flooding cells
                    # whose natural terrain already dips below the spill level.
                    # Fixes flat-at-spill basins that rendered as striped
                    # channels (water only in the river trenches cut through
                    # them, dry Y-walls between). lkdep>0 defines the basin
                    # floor; carve ONLY LOWERS terrain (clip>=0) so existing
                    # deep channels stay deep and the rim is untouched -> a
                    # smooth bowl filled to the flat MIN-spill water level (no
                    # spillover, water never above any rim cell).
                    lkdep_tif = masks_dir / "hydro_lkdep.tif"
                    if lkdep_tif.exists():
                        with rasterio.open(lkdep_tif) as src:
                            pad_lkdep = src.read(1, window=_RWindow(
                                px0, pz0, px1 - px0, pz1 - pz0)).astype(np.float32)
                    else:
                        pad_lkdep = np.zeros_like(pad_water_y)
                    # STRICTLY ADDITIVE union: (a) keep every cell the old
                    # terrain-intersection would flood (terrain < water), so we
                    # never lose existing water; PLUS (b) the precompute basin
                    # floor (lkdep>0) within gouge_cap of the water surface, to
                    # fill the dry Y-walls between channels. carve only LOWERS
                    # toward (water - lkdep); old-wet cells with lkdep=0 carve 0
                    # (already underwater) so they're preserved unchanged.
                    _gouge_cap = float(geo.get("lake_bowl_max_gouge", 6.0))
                    pad_old_wet = pad_basin & (pad_terrain_y < pad_water_y)
                    pad_walls = pad_basin & (pad_lkdep > 0) & (
                        pad_terrain_y <= pad_water_y + _gouge_cap)
                    pad_floor = pad_old_wet | pad_walls
                    # Smooth the 1:8 NEAREST-upscaled bathymetry (8-block
                    # staircase) so the bowl bed is organic, not stepped. Gaussian
                    # within the basin; outside-basin zeros pull edges shallow ->
                    # natural shore. Renormalize by the smoothed basin weight so
                    # the center depth isn't washed out by the surrounding zeros.
                    _sig = float(geo.get("lake_bowl_smooth_sigma", 4.0))
                    if _sig > 0:
                        _w = pad_floor.astype(np.float32)
                        _num = gaussian_filter(pad_lkdep * _w, sigma=_sig)
                        _den = gaussian_filter(_w, sigma=_sig)
                        pad_lkdep_use = np.where(_den > 1e-3, _num / (_den + 1e-6),
                                                 pad_lkdep)
                    else:
                        pad_lkdep_use = pad_lkdep
                    # Deepen: scale the bathymetry then cap. Thin lakes have a
                    # shallow distance-to-shore bowl (e.g. tile 19,76 maxes ~6)
                    # while round lakes reach deeper (33,33 ~11); the scale lifts
                    # the shallow ones and the cap stops round ones from getting
                    # absurd. Edges stay shallow (natural shore) since they scale
                    # from ~1-2.
                    _dscale = float(geo.get("lake_bowl_depth_scale", 1.0))
                    if _dscale != 1.0:
                        pad_lkdep_use = pad_lkdep_use * _dscale
                    _dcap = float(geo.get("lake_bowl_depth_cap", 0.0))
                    if _dcap > 0:
                        pad_lkdep_use = np.minimum(pad_lkdep_use, _dcap)
                    pad_target_bed = pad_water_y - pad_lkdep_use
                    pad_depth_f = np.where(
                        pad_floor,
                        np.clip(pad_terrain_y - pad_target_bed, 0.0, None),
                        0.0).astype(np.float32)
                    lake_mask = pad_floor[crop_r0:crop_r0+H, crop_c0:crop_c0+W]
                    lake_depths = pad_depth_f[crop_r0:crop_r0+H, crop_c0:crop_c0+W]
                    lake_shore_band = np.zeros((H, W), dtype=bool)
                else:
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
        if _skip_stochastic:
            bump = np.zeros_like(lake_d_floor, dtype=np.int32)  # S90: bed already smooth
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

            # S80 v32: spline loading is GATED on hydro_region.png absence.
            # When the user has painted rivers (hydro_region.png exists
            # AND contains id=2 paint), the spline pickle from a prior
            # WP-findPath run would inject those WP rivers on top of
            # the user's paint — defeating the "paint is the sole
            # source" invariant. We skip spline loading entirely in
            # that case. When there's no paint, splines load as before.
            import pickle
            from scipy.interpolate import splev
            _spline_path = (masks_dir / "river_splines.pkl"
                            if masks_dir is not None else None)
            _splines_loaded: list = []
            _SCALE = 8  # 1:8 → 50k

            _paint_active = False
            if masks_dir is not None:
                _hr = masks_dir / "hydro_region.png"
                if _hr.exists():
                    try:
                        from PIL import Image as _PIL
                        _hr_arr = np.asarray(_PIL.open(_hr).convert("L"))
                        _paint_active = bool(np.any(_hr_arr == 2))
                    except Exception:
                        _paint_active = False

            if (_spline_path is not None and _spline_path.exists()
                    and not _paint_active):
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
    # S84: drop above_sea gate — paint always carves. Painted cells whose
    # natural terrain dips below sea level still get the spline+SDF+bed-cache
    # carve, producing a real underwater channel that extends the river into
    # the ocean (real river-delta look) instead of abruptly flattening at the
    # Y 63 contour. Bank widening (sec 8) still gates on above_sea so banks
    # don't extend into ocean.
    river_channel = centerline & ~lake_mask

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
    # S84: drop above_sea gate here too (matches river_channel — paint always
    # carves, even below sea level).
    river_full_mask = river_channel | (conn_channel_mask & ~lake_mask)

    # Pre-allocate water_y_field (default -1 = no water at this pixel).
    water_y_field = np.full((H, W), -1, dtype=np.int16)

    if river_full_mask.any():
        from scipy.ndimage import gaussian_filter as _gf
        from scipy.ndimage import label as _label_grav

        # S81 v8.5 Step B: compute skeleton ONCE at the top of section 7
        # so both the gravity pre-pass (7.1b) AND the EDT propagation (7.5)
        # operate on the same 1-cell-wide medial axis. Pre-S81 only the EDT
        # used skeleton (Step A); the gravity pre-pass + 1D path-smoothing
        # iterated wide-footprint cells which produced the v8.4 "giant
        # chunks of rising water at curves" — at confluences the wide
        # gravity sort mixed cells from multiple flow paths.
        from skimage.morphology import skeletonize as _skel_for_grav
        _skeleton_mask = _skel_for_grav(river_full_mask)
        if not _skeleton_mask.any():
            # Degenerate: small fragments where skeletonize returns empty
            _skeleton_mask = river_full_mask

        # 7.1 — per-centerline width (block half-radius) from ``hydro_width``.
        # Per user directive (S81): paint is the sole source. The legacy
        # Strahler-ORDER_TO_WIDTH lookup table is gone — every painted
        # cell carries an explicit EDT-derived + slope-modified + Hack's-
        # law width set by apply_hydro_region_overlay. Painted cells with
        # no explicit width (rare edge case) fall to a 2.5-block default.
        # WP-style slope augmentation (+ 2·slope²) is applied below.
        width_at_pixel = np.zeros((H, W), dtype=np.float32)
        _w_painted = river_full_mask & (width_u8 > 0) if width_u8 is not None \
            else np.zeros_like(river_full_mask)
        if _w_painted.any():
            width_at_pixel[_w_painted] = width_u8[_w_painted].astype(np.float32)
        _w_default = river_full_mask & ~_w_painted
        if _w_default.any():
            width_at_pixel[_w_default] = 2.5

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
            # S81 v8.6: REVERTED Step B — back to labeling the wide footprint.
            # Step B (label skeleton) caused triangle-shaped water columns at
            # confluences: degree-3 skeleton junctions sit in a single
            # connected component, the 1D path-smoothing argsorts dist_from_ocean
            # which interleaves cells from converging branches, and gaussian
            # smoothing then mixes their independent water_y values. Reverting
            # to wide-mask labeling restores v8.3 behavior at confluences while
            # Step A's skeleton-EDT (below) still keeps cross-sections uniform.
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
        # S81 v8.3 Step A: EDT source = SKELETONIZED centerline (1-cell-wide
        # medial axis), not the wide painted footprint. Pre-S80 WP-findPath
        # rivers were naturally 1-3 cells wide so EDT-from-full-mask gave
        # near-uniform per-Voronoi propagation. Painted rivers (S80+) are
        # 6-12 cells wide → inside-footprint cells got dist=0 and
        # nearest_idx pointing to themselves, so they inherited per-cell
        # nearest_avg / slope / width values → broke cross-section water_y
        # uniformity (~14k uneven cells on (51,53) per memory/diag_water_y).
        # The S73-v7 comment block at 7.7 documented intent ("centerline
        # path only"). Implementation now matches the documented intent.
        from scipy.ndimage import distance_transform_edt as _edt_g
        # S81 v8.5: reuse the skeleton computed at the top of section 7
        # (Step B). _skeleton_mask is already validated (fallback to
        # river_full_mask if degenerate).
        _skel_for_edt = _skeleton_mask
        dist_to_center, ind_center = _edt_g(~_skel_for_edt, return_indices=True)
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

        # 7.6 — terrain-intersection carve from precomputed depth field.
        #
        # S81 v8: TRUE continuous carve.
        #   1. Read depth as FLOAT (sub-block precision via hydro_depth
        #      × 255) — uint8 round() was throwing away the smooth taper.
        #   2. Apply carve EVERYWHERE depth > 0 (no narrow footprint
        #      gate). Cells far from paint have depth ≈ 0 → no effect.
        #      Cells near paint have continuous carve depth → smooth
        #      surface transition.
        #   3. Subtract from cell's OWN original surface_y, preserving
        #      Gaea's ±1-2 block natural terrain variation. Together
        #      with continuous carve, this is the same mechanism as
        #      lakes: surface = terrain_natural - smooth_offset; the
        #      `surface < water_y` contour follows TERRAIN VARIATION,
        #      not a binary mask boundary = no staircase.
        depth_at_cell = (hydro_depth.astype(np.float32) * 255.0
                         if hydro_depth is not None
                         else np.zeros_like(surface_out, dtype=np.float32))
        # S89 walk (#13/#11): SHALLOW HEADWATERS + no chasms. High-altitude
        # headwaters (low Strahler order) were carving deep vertical slots.
        # Scale carve depth down for low order (headwaters shallow, mainstem
        # full) + optional absolute cap. Bounded (only REDUCES depth, never
        # raises) + config-gated (cfg.river_carve); tune from the batch render.
        _rc = (cfg or {}).get("river_carve", {}) if isinstance(cfg, dict) else {}
        if hydro_order is not None and bool(_rc.get("headwater_shallow", True)):
            _ordf = hydro_order.astype(np.float32) * 255.0    # Strahler 1..5 (0 = no river)
            _lo = float(_rc.get("headwater_depth_scale", 0.5))
            _full = float(_rc.get("full_depth_order", 4.0))
            _osc = np.clip(_lo + (1.0 - _lo) * (_ordf - 1.0) / max(1e-3, _full - 1.0), _lo, 1.0)
            depth_at_cell = np.where(_ordf >= 0.5, depth_at_cell * _osc, depth_at_cell)
        _maxc = float(_rc.get("max_carve_blocks", 0.0))
        if _maxc > 0.0:
            depth_at_cell = np.minimum(depth_at_cell, _maxc)
        # ── S93e HEADWATER WIDTH TAPER ──────────────────────────────────
        # User spec: "headwaters should be realistically thin and widen at
        # a realistic pace." Wet width IS the carve footprint (the S80 v15
        # too_high sink floods every footprint cell), and the footprint is
        # the painted polygon — so paint-floor-width headwaters render
        # 12-50 blocks wide from their FIRST block. Gate depth_at_cell on
        # distance-to-painted-centerline vs a target half-width grown with
        # GEODESIC DISTANCE FROM THE CHANNEL'S SOURCE TIP (hydro_dist_src;
        # BFS over the 8k skeleton pixels — robust where the painted flow
        # graph breaks, which is why raw flow accumulation can't drive
        # this). target = hw_min + k*sqrt(d_src), capped at the local
        # painted half-width (never widens) -> exact no-op beyond ~600
        # blocks from a tip (mainstems, estuaries). Outside the target the
        # carve is capped BELOW the 0.05 footprint threshold instead of
        # zeroed: the old wide trough fills back to near-terrain and the
        # channel reads as a thin stream in a soft swale — no dry moat.
        _ht = _rc.get("headwater_taper", {}) if isinstance(_rc, dict) else {}
        if (bool(_ht.get("enabled", True))
                and hydro_dist_src is not None
                and hydro_dcl is not None
                and hydro_hw_cl is not None):
            # S93e v2: PURE per-cell math on globally-sampled fields (the
            # seam law — geometry derived per-tile dries channels at tile
            # borders; the v1 per-tile skeletonize did exactly that at
            # (27,33)|(27,34)). All three inputs come from the overlay's
            # halo'd EDT over the GLOBAL 8k skeleton points, identical on
            # both sides of every seam.
            _hw_min = float(_ht.get("hw_min", 1.5))
            _k_ht = float(_ht.get("k", 0.37))
            _soft_ht = max(float(_ht.get("edge_soft", 1.5)), 0.25)
            _dsrc_n = hydro_dist_src.astype(np.float32)
            _tgt = _hw_min + _k_ht * np.sqrt(np.maximum(_dsrc_n, 0.0))
            _tgt = np.minimum(_tgt, np.maximum(
                hydro_hw_cl.astype(np.float32), _hw_min))
            _tgt = np.where(_dsrc_n >= 0.0, _tgt,
                            np.float32(1e9)).astype(np.float32)
            _g_ht = np.clip((_tgt + _soft_ht - hydro_dcl.astype(np.float32))
                            / _soft_ht, 0.0, 1.0).astype(np.float32)
            # NO-DATA GUARD: cells far from ANY skeleton point belong to
            # painted channels whose flow graph never built — they'd
            # otherwise inherit an unrelated point's values with a huge
            # dcl and be gated DRY ((62,61)'s inter-lake streams died to
            # this). Far-from-skeleton means "no taper", never "no water".
            _nodata_r = float(_ht.get("nodata_dcl", 48.0))
            _g_ht = np.where(hydro_dcl > _nodata_r,
                             np.float32(1.0), _g_ht)
            # TIDAL GUARD: no taper where the pre-carve terrain sits at
            # or below sea level (+1) — tidal fans/estuary mouths are
            # governed by the sea, not by upstream catchment. The
            # above-sea estuary arm still narrows (the desired
            # "accurately narrows upstream" behaviour); without this
            # the whole approved (27,34) fan thinned 43%.
            _tidal_ht = surface_out.astype(np.float32) <= float(SEA_LEVEL + 1)
            if _tidal_ht.any():
                _g_ht = np.where(_tidal_ht, np.float32(1.0), _g_ht)
            _cap_ht = np.float32(0.04) + _g_ht * depth_at_cell
            depth_at_cell = np.where(
                _g_ht >= 1.0, depth_at_cell,
                np.minimum(depth_at_cell, _cap_ht)).astype(np.float32)
            del _dsrc_n, _tgt, _g_ht, _cap_ht, _tidal_ht
        original_sy_f = surface_out.astype(np.float32)
        new_y_f = (original_sy_f - depth_at_cell).astype(np.float32)
        # Footprint covers the BROAD carve buffer (any non-trivial
        # depth) so water_y_field is set across the full area where
        # terrain intersection determines visibility. The actual
        # river_meta tagging uses a stricter threshold below.
        footprint = (depth_at_cell > 0.05) & ~lake_mask & above_sea

        # Only LOWER (never raise) and clamp to bedrock min
        cur_y_f = surface_out.astype(np.float32)
        new_y_f = np.minimum(new_y_f, cur_y_f)
        new_y_f = np.maximum(new_y_f, float(BEDROCK_Y + 3))

        # Apply within footprint
        if footprint.any():
            surface_out[footprint] = np.round(new_y_f[footprint]).astype(surface_out.dtype)

        # S83 v8 + v10: GLOBAL BED + BANK OVERRIDE. hydro_river_bed
        # encodes:
        #   - footprint cells: globally-smoothed bed (σ=4 weighted)
        #   - bank ring cells (1 8k-px around footprint): σ=1 smoothed
        #     LUT(height) — gentle smooth removing bank "raised area"
        #     anomalies
        #   - elsewhere: sentinel -999 (carver skips)
        # The sentinel sits well below BEDROCK_Y+3 (=-61), so the
        # check below cleanly identifies cells where the cache wants
        # to override.
        if hydro_river_bed is not None and footprint.any():
            # S83 v11: GLOBAL bed override at painted footprint. The 8k
            # bed cache is computed in _ensure_caches via:
            #   raw bed → σ=4 weighted gaussian (single pass)
            #   → 5 additional σ=2 weighted gaussian passes
            #     (WorldEdit //brush smooth ×5 equivalent on trough surface)
            # Clean np.round (NOT stochastic) — user feedback: "clean steps
            # are fine"; the real defect was rectangular-prism anomalies
            # protruding from trough walls, fixed by the 5-pass smooth.
            _bed_valid = footprint & (
                hydro_river_bed > float(BEDROCK_Y + 3))
            if _bed_valid.any():
                # S83 v11 invariant: ONLY LOWER terrain (never raise).
                # The 5-pass smooth can pull a deeply-carved bed slightly
                # upward at boundary cells (gaussian averaging). Clamping
                # to current surface_out keeps the trough wall above any
                # water, preventing "river bed sticking up out of land"
                # artifacts at footprint edges.
                _new_bed = np.maximum(hydro_river_bed,
                                       float(BEDROCK_Y + 3))
                _cur_b = surface_out.astype(np.float32)
                # S93c CANYON GUARD: the 8k bed cache stores ABSOLUTE MC-Y
                # baked with a pre-768 height LUT (field max ~446.6 = the
                # old 448-world ceiling). At lowland the baked LUT matches
                # the live spline (bed ~= terrain at river cells, dig ~0,
                # override is a near-no-op vs the live carve) but the error
                # grows with altitude: (79,71) Y~145 dug 16-20 below the
                # banks, (30,12) Y~500 dug a 225-deep canyon. Clamp the
                # override to at most bed_max_extra_depth below the LIVE-
                # carved surface — exact no-op where the cache is sane,
                # canyon-killer at altitude. Proper fix = rebake the bed
                # under the current spline (bed-builder reconciliation
                # session; see memory/S93_handoff.md).
                _bed_extra = float(_rc.get("bed_max_extra_depth", 3.0))
                if _bed_extra > 0.0:
                    _new_bed = np.maximum(_new_bed, _cur_b - _bed_extra)
                _new_bed = np.minimum(_new_bed, _cur_b)
                surface_out[_bed_valid] = (
                    np.round(_new_bed[_bed_valid])
                    .astype(surface_out.dtype))

        # 7.7 — Water level: BANK-RELATIVE (S80 v26).
        #
        # FAIL HISTORY: Previous formulas referenced `nearest_avg` (smoothed
        # centerline elevation). For V-shaped or sloped valleys, banks rise
        # ABOVE centerline → water_y based on centerline sat 2-5+ blocks
        # below visible bank top → multi-block air gap at the bank.
        # User said "river sits 2 blocks of air down. need 1 row of air."
        #
        # Fix: estimate bank elevation as `nearest_avg + slope * width`
        # (elevation gain from centerline to bank, using local gradient
        # over half-width distance). Then water_y = bank_estimate - 1.0.
        # For flat terrain, slope≈0 → water = avg - 1 → 1 below bank
        # (since bank ≈ center on flat). For sloped terrain, slope*width
        # captures the bank rise → water tracks bank → consistent 1 air
        # gap regardless of slope.
        #
        # Capped at nearest_avg + 4.0 to prevent runaway on extreme cliffs
        # (would overflow trench).
        bank_lift = np.minimum(nearest_slope * nearest_width, 4.0).astype(np.float32)
        water_y = nearest_avg + bank_lift - 1.0
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
                # S93: MONOTONE INTEGER QUANTIZATION with hysteresis along
                # the path (replaces the per-pixel round() downstream — see
                # 7.8). round() of the smooth profile put the N|N-1
                # crossing wherever the float wandered past x.5: a ragged,
                # Voronoi-sliver-interleaved 2D zone that MC renders as
                # CHECKERED/raised water patches (user screenshots,
                # estuary at (27,34)). Quantizing HERE makes each integer
                # drop exactly ONE clean line across the channel:
                #  - drop 1 when the smooth profile is >= 0.5 below the
                #    current plateau AND the plateau has run >= _MIN_RUN
                #    cells (hysteresis kills dither on near-flat reaches);
                #  - steep reaches (fallen >= 1.5) re-anchor directly to
                #    round(v) so descent is never artificially backed up;
                #  - monotone non-increasing toward the ocean + the SEA
                #    floor (applied below) → the terminal estuary reach is
                #    a single flat Y63 pool by construction.
                # The S73 objection to explicit plateaus ("ghost weirs" —
                # MC tick cascades at step lines) is obsolete: S86 never
                # fluid-ticks river columns.
                _MIN_RUN = 12
                _lvl = float(np.round(_path_y_smooth[0]))
                _run = 0
                _q_out = np.empty_like(_path_y_smooth)
                for _i in range(len(_path_y_smooth)):
                    _v = float(_path_y_smooth[_i])
                    if _v <= _lvl - 1.5:
                        _lvl = float(np.round(_v))   # steep: re-anchor
                        _run = 0
                    elif _v <= _lvl - 0.5 and _run >= _MIN_RUN:
                        _lvl -= 1.0                  # gentle: single step
                        _run = 0
                    _q_out[_i] = _lvl
                    _run += 1
                water_y[_r_ord, _c_ord] = _q_out
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
            water_y_int = np.round(water_y).astype(np.int16)
            # S83 v9: OVERRIDE water_y with the global skeleton-walk value
            # at painted-river cells where the precompute set it. The
            # global walk produces a monotonic source→sink water profile
            # that is identical at the same physical cell between both
            # tiles → eliminates the 2-block water step at boundaries.
            # Lake cells, BLEND zone, and the per-tile water_y formula
            # for non-painted footprint cells are all untouched.
            if hydro_river_water_y is not None:
                _global_wy_valid = water_zone & (
                    hydro_river_water_y > float(SEA_LEVEL))
                if _global_wy_valid.any():
                    water_y_int[_global_wy_valid] = np.round(
                        hydro_river_water_y[_global_wy_valid]
                    ).astype(np.int16)
            water_y_field[water_zone] = water_y_int[water_zone]
            # S80 v15: ensure surface_y is at LEAST 1 below water_y at every
            # footprint cell.  Without this, the guardrail formula keeps
            # edge cells (factor > 0.1) at original surface, while water_y
            # is set lower — the chunk_writer's
            # `river_water_mask = (abs_y > surface) & (abs_y <= water_y)`
            # comes back EMPTY for those cells, so they show as dry land.
            # Result before fix: visible water = ~2-3-cell centerline, NOT
            # the full footprint width.  Visible streams disconnect from
            # lake water by 1-2 dry cells at the junction.
            # After fix: visible water = full footprint width = spec width.
            too_high = water_zone & (surface_out >= water_y_int)
            if too_high.any():
                surface_out[too_high] = (water_y_int[too_high] - 1).astype(
                    surface_out.dtype
                )

        carve_px = footprint & (cur_y_f > new_y_f)

        # 7.6b — Carve-floor smooth-brush pass (S81 v8.13: FOOTPRINT-ONLY).
        # PRIOR (v8.5-v8.12): smoothing zone was BANK_ZONE=24 blocks wide,
        # including bank cells outside the footprint. The gaussian's
        # kernel reached into the carved (low) river floor and pulled
        # bank cells DOWN, creating an artificial valley dip that the
        # later run_pipeline.py escape-fix + EDT berm couldn't fully
        # restore (berm only reaches BERM_RADIUS=8 with 1/cell falloff,
        # but the smooth-brush valley extended out to 24 cells).
        # Visible result: a 1-cell wall at water sticking up out of a
        # smoothed-down valley — user feedback "trench wall higher than
        # the soft valley around it".
        # NEW: smoothing applies ONLY to footprint cells (inside the
        # trench, where water is). Bank cells stay at natural Y.
        # Footprint cells still get constrained to [original_carved,
        # water_y - 1] so the smoothing only affects the carve-floor
        # taper pattern, not the depth itself. Bank shaping is now
        # entirely the responsibility of run_pipeline's escape-fix
        # (1-cell wall at water_y) + EDT berm (1/cell slope to 8 cells
        # out, then natural terrain).
        from scipy.ndimage import binary_dilation as _bd_shore
        from scipy.ndimage import gaussian_filter as _gf_bank
        BANK_SIGMA = 16.0    # gaussian sigma — WorldEdit-style
        BANK_PASSES = 3      # iterate the blur 3× for cumulative effect
        # S83 v8: SKIP per-tile bank smooth-brush when global hydro_river_bed
        # is provided (it pre-smoothed the bed at 8k globally → consistent
        # across tile boundaries by construction). The per-tile gaussian
        # below was the visible bed-elevation seam source; replacing it
        # with the global precompute eliminates that seam.
        _skip_bank_smooth = (hydro_river_bed is not None)
        if footprint.any() and not _skip_bank_smooth:
            sy_f_bank = surface_out.astype(np.float32)
            in_zone = footprint & above_sea & ~lake_mask
            if in_zone.any():
                w_bank = in_zone.astype(np.float32)
                # === S83 v7: ONLY pad the bank smooth-brush gaussian. ===
                # Everything else in the carver stays at v8.14 baseline
                # (gravity ON, skeleton/EDT unpadded). The σ=16 × 3 passes
                # gaussian was the main cause of 3-block bed elevation
                # step at the tile boundary; padding it eliminates the
                # reflect-mode boundary artifact without touching any
                # algorithmic semantics.
                _BSP = 48
                _PH_B = H + 2 * _BSP
                _PW_B = W + 2 * _BSP
                _bank_padded = False
                if (masks_dir is not None and tile_x is not None
                        and tile_z is not None):
                    try:
                        import rasterio as _rio_b
                        from rasterio.windows import Window as _RWin_b
                        _ht_b = masks_dir / "height.tif"
                        if _ht_b.exists():
                            _col_off_b = tile_x * W
                            _row_off_b = tile_z * H
                            with _rio_b.open(str(_ht_b)) as _src_b:
                                _px0b = max(_col_off_b - _BSP, 0)
                                _pz0b = max(_row_off_b - _BSP, 0)
                                _px1b = min(_col_off_b + W + _BSP, _src_b.width)
                                _pz1b = min(_row_off_b + H + _BSP, _src_b.height)
                                _h_raw_b = _src_b.read(1, window=_RWin_b(
                                    _px0b, _pz0b,
                                    _px1b - _px0b, _pz1b - _pz0b))
                            _LUT_in_b = np.array(
                                [0, 17050, 45000, 65496], dtype=np.float64)
                            _LUT_out_b = np.array(
                                [-64, 63, 200, 448], dtype=np.float64)
                            _mc_y_b = np.interp(
                                _h_raw_b.ravel().astype(np.float64),
                                _LUT_in_b, _LUT_out_b
                            ).reshape(_h_raw_b.shape).astype(np.float32)
                            _pre_carve_pad_b = np.zeros(
                                (_PH_B, _PW_B), dtype=np.float32)
                            _dr0b = _pz0b - (_row_off_b - _BSP)
                            _dc0b = _px0b - (_col_off_b - _BSP)
                            _pre_carve_pad_b[
                                _dr0b:_dr0b + _h_raw_b.shape[0],
                                _dc0b:_dc0b + _h_raw_b.shape[1]
                            ] = _mc_y_b

                            _sy_pad_b = _pre_carve_pad_b.copy()
                            _sy_pad_b[_BSP:_BSP + H, _BSP:_BSP + W] = (
                                surface_out.astype(np.float32))

                            # Footprint approximation in pad: use painted
                            # river binary at padded coords.
                            _hr_path_b = masks_dir / "hydro_region.png"
                            _paint_river_pad_b = np.zeros(
                                (_PH_B, _PW_B), dtype=bool)
                            _paint_lake_pad_b = np.zeros(
                                (_PH_B, _PW_B), dtype=bool)
                            if _hr_path_b.exists():
                                try:
                                    from PIL import Image as _PILImg_b
                                    _hr8k_b = np.asarray(
                                        _PILImg_b.open(_hr_path_b).convert("L"),
                                        dtype=np.uint8)
                                    if _hr8k_b.shape == (8192, 8192):
                                        _S_b = 8192.0 / 50000.0
                                        _ys_b = (np.arange(_PH_B)
                                                 + (_row_off_b - _BSP))
                                        _xs_b = (np.arange(_PW_B)
                                                 + (_col_off_b - _BSP))
                                        _y8b = np.clip(
                                            (_ys_b * _S_b).astype(np.int32),
                                            0, 8191)
                                        _x8b = np.clip(
                                            (_xs_b * _S_b).astype(np.int32),
                                            0, 8191)
                                        _yyb, _xxb = np.meshgrid(
                                            _y8b, _x8b, indexing="ij")
                                        _paint_river_pad_b = (
                                            _hr8k_b[_yyb, _xxb] == 2)
                                        _paint_lake_pad_b = (
                                            (_hr8k_b[_yyb, _xxb] == 1)
                                            & ~_paint_river_pad_b)
                                except Exception:
                                    pass

                            _footprint_pad_b = _paint_river_pad_b.copy()
                            _footprint_pad_b[
                                _BSP:_BSP + H, _BSP:_BSP + W] = footprint
                            _lake_pad_b = _paint_lake_pad_b.copy()
                            _lake_pad_b[_BSP:_BSP + H, _BSP:_BSP + W] = lake_mask
                            _above_sea_pad_b = (
                                _pre_carve_pad_b > float(SEA_LEVEL))
                            _in_zone_pad_b = (
                                _footprint_pad_b
                                & _above_sea_pad_b
                                & ~_lake_pad_b
                            )

                            _w_bank_pad = _in_zone_pad_b.astype(np.float32)
                            _cur_pad_b = _sy_pad_b.copy()
                            for _bp in range(BANK_PASSES):
                                _blur_pad = _gf_bank(_cur_pad_b, sigma=BANK_SIGMA)
                                _cur_pad_b = (
                                    _w_bank_pad * _blur_pad
                                    + (1.0 - _w_bank_pad) * _cur_pad_b
                                )

                            final = _cur_pad_b[
                                _BSP:_BSP + H, _BSP:_BSP + W].copy()
                            cap = (water_y - 1.0).astype(np.float32)
                            final[in_zone] = np.minimum(
                                final[in_zone], cap[in_zone])
                            final[in_zone] = np.maximum(
                                final[in_zone], sy_f_bank[in_zone])
                            changed = in_zone & (
                                np.abs(final - sy_f_bank) > 0.5)
                            if changed.any():
                                surface_out[changed] = np.round(
                                    final[changed]
                                ).astype(surface_out.dtype)
                            _bank_padded = True
                    except Exception as _bank_exc:  # noqa: BLE001
                        print(f"[river_carver_v2] S83 v7 bank padding "
                              f"skipped: {type(_bank_exc).__name__}: "
                              f"{_bank_exc}")
                        _bank_padded = False

                if not _bank_padded:
                    # === Unpadded fallback (v8.14 baseline) ===
                    cur = sy_f_bank.copy()
                    for _bp in range(BANK_PASSES):
                        blurred_bank = _gf_bank(cur, sigma=BANK_SIGMA)
                        cur = (w_bank * blurred_bank
                               + (1.0 - w_bank) * cur)
                    final = cur.copy()
                    cap = (water_y - 1.0).astype(np.float32)
                    final[in_zone] = np.minimum(final[in_zone], cap[in_zone])
                    final[in_zone] = np.maximum(
                        final[in_zone], sy_f_bank[in_zone])
                    changed = in_zone & (np.abs(final - sy_f_bank) > 0.5)
                    if changed.any():
                        surface_out[changed] = np.round(
                            final[changed]
                        ).astype(surface_out.dtype)

        # 7.6c — Steppable shore lip (S81). Final pass: just outside the
        # carve footprint, if the bank still rises more than 1 block
        # above the water surface (after bank-smooth pass above), drop
        # it down to water_y + 1 so the player can hop out of the river
        # without jumping. 1-cell-wide ring at footprint boundary.
        shore_ring = (_bd_shore(footprint, iterations=1)
                      & ~footprint & ~lake_mask & above_sea)
        if shore_ring.any():
            water_y_int_pre = np.round(water_y).astype(np.int16)
            target_y = water_y_int_pre + np.int16(1)
            need_step = shore_ring & (surface_out > target_y)
            if need_step.any():
                surface_out[need_step] = target_y[need_step]

        # Set river_meta for footprint pixels (Strahler-aware)
        not_lake = ~lake_mask
        order_prop_k2 = min(flow_refine_radius * 2 + 1, 31)
        order_propagated = maximum_filter(
            order_u8.astype(np.float32), size=order_prop_k2
        ).astype(np.uint8)
        # river_meta uses STRICTER mask than the broad water_y_field
        # footprint — the broad footprint extends ~22 blocks past the
        # visible river, which would mark unrelated terrain as river
        # bank (suppressing schematics and biome decorations there).
        # Strict: depth ≥ 1 block of carve (the "real" river area).
        river_strict = (depth_at_cell >= 1.0) & ~lake_mask & above_sea
        stream_expanded = river_strict & (order_propagated <= 2) & not_lake
        river_expanded  = river_strict & (order_propagated >= 3) & not_lake
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

        # Pass 2 (edge-spillover guard) REMOVED. It was designed for the
        # legacy tapered carve where edge cells were barely lowered and
        # could let water spill over banks. With the flat-bottom paint
        # carve every footprint cell is uniformly lowered to avg-depth,
        # banks (cells OUTSIDE footprint) keep their original elevation,
        # so water containment is automatic — no spillover possible.
        # When this pass DID fire on flat-bottom carve, it raised carved
        # edges back to water_y level, eating ~half the carve width and
        # producing the "rivers narrower than painted" symptom.

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
    # S80 v20: DISABLED.  This gaussian-blur of surface_out at
    # river-lake junctions was UNDOING the v15 fix.  v15 lowers surface
    # at every water_zone cell to ensure visible water; this pass blurred
    # surface upward toward the lake's higher terrain (basin-shore was
    # at terrain==wl, blur pulls river surface from Y69 up to Y70+),
    # which made `surface >= water_y` again so chunk_writer skipped
    # placing water blocks at the river-lake junction.  Result: visible
    # 18-cell dry gap between river and lake water.
    # Re-enable only if vertical-wall artifacts return at junctions.
    # S80 v20 vs v21: re-enable section 7b but PROTECT v15 surface lowering.
    # Section 7b's gaussian blur was raising surface above water_y at junctions,
    # undoing my v15 carve.  Disabling caused (30,49) lake painting to fail.
    # The fix: run the blur but only update cells where the new surface is
    # STRICTLY LOWER than the existing surface, so v15-fixed cells stay deep.
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
            # S80 v21: only LOWER surface, never raise.  Without this, the
            # gaussian blur was raising surface above water_y at the river
            # side of junctions, undoing the v15 carve fix and leaving
            # river cells dry.  np.minimum keeps each cell at the lower of
            # blurred-or-current → smooth at junctions without breaking
            # v15's water visibility guarantee.
            new_sy = np.round(blended).astype(surface_out.dtype)
            keep = (new_sy < surface_out) & junction
            if keep.any():
                surface_out[keep] = new_sy[keep]

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

    # ── 8b. S89-walk4: LAKE WINS inside its basin ─────────────────────────────
    # Rivers (carved in step 7) override lake cells with CHAN_RIVER at the river's
    # LOWER water level, leaving dry banks inside the lake footprint -> the broken
    # "striped lake with dry walls" look. A real lake SUBMERGES rivers flowing
    # through it. Re-assert the lake over its whole bowl-carved footprint so
    # run_pipeline fills the entire basin to the flat lake spill level; the river
    # channels within become deep lake water, the bowl-carved flats/walls become
    # shallow lake. Only active with bowl-carve (the footprint was carved below
    # the water level, so nothing is left dry).
    if bool(geo.get("lake_bowl_carve", False)) and lake_mask is not None and lake_mask.any():
        river_meta[lake_mask] = CHAN_LAKE

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
