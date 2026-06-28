"""S89 walk3: flow-based erosion to dissect blobby upscaled mountains.

The Gaea height source is bilinear-upscaled to 50k, which rounds ridgelines into
smooth domes ("blobs"). This pass cuts a drainage texture INTO the rock massif so
it reads as a real ridge-and-valley mountain instead of a balloon. Three coupled
terms, applied to surface_y ONLY on rock cells (rock_layers tier >= 1) and never
on carved rivers:

  1. FLOW INCISION  — lower surface_y proportional to flow accumulation (band-
     limited to the tributary range) so channels deepen into V-valleys and the
     ridges between them stand proud. This is the realistic dendritic skeleton
     straight from the DEM's own flow field.
  2. SYNTHETIC GULLY — ridged simplex (1-|n|) sampled at WORLD coords carves a
     fine connected gully network the coarse DEM-derived flow misses. This is the
     actual de-blobber at the detail scale (the flow field is as smooth as the
     blob it came from). World-coord seeded => seam-consistent across tiles.
  3. RIDGE SHARPEN  — raise convex cells (spurs/noses) by their convexity so the
     rock between gullies reads as a crisp arete instead of a round shoulder.

Gating to rock tier>=1 keeps the whole eroded zone inside the painted rock extent
(no grassy gully walls) and leaves lowland/forest terrain untouched. cliff_deg is
recomputed downstream from the eroded surface_y, so ground cover, schematic slope
reject, and the written columns all follow the new shape. Tier PAINT still uses
the baked rock_layers mask (computed from pre-erosion slope) — a v1 limitation;
full tier-follows-drainage needs erosion at mask-build time.

All knobs in cfg["flow_erosion"]; disabled unless enabled=true.
"""
from __future__ import annotations

import numpy as np


def apply_flow_erosion(
    surface_y:        np.ndarray,
    flow_tile:        np.ndarray | None,
    rock_layers_tile: np.ndarray | None,
    river_meta:       np.ndarray | None,
    cfg:              dict,
    tile_x:           int,
    tile_y:           int,
    pad:              int = 0,
    surface_y_pad:    np.ndarray | None = None,
    flow_pad:         np.ndarray | None = None,
    rock_pad:         np.ndarray | None = None,
) -> np.ndarray:
    """Return an eroded copy of surface_y (same dtype). No-op if disabled / no rock.

    S89-walk4 seam fix: when pad>0 and the *_pad halo arrays are supplied, ALL
    per-tile neighbourhood ops (rock edge-fade EDT, convexity/face/smooth
    gaussians, gradient) run on the padded halo and the displacement is cropped
    back to the inner tile -> no false rock-boundary fade or edge replication at
    tile borders = no relief-texture seam. World-coord noise stays seamless via
    the (tile_x*W - pad) origin. Falls back to per-tile when no halo."""
    fcfg = cfg.get("flow_erosion", {}) if isinstance(cfg, dict) else {}
    if not fcfg.get("enabled", False):
        return surface_y
    if rock_layers_tile is None or flow_tile is None:
        return surface_y

    H, W = surface_y.shape
    _seam = (pad > 0 and surface_y_pad is not None and flow_pad is not None
             and rock_pad is not None
             and surface_y_pad.shape == (H + 2 * pad, W + 2 * pad))
    min_tier = int(fcfg.get("min_rock_tier", 1))
    if _seam:
        sy = surface_y_pad.astype(np.float32)
        hh, ww = sy.shape                       # padded dims
        ox, oy = tile_x * W - pad, tile_y * H - pad   # world origin of halo
        flow_src = flow_pad
        tier = np.round(np.asarray(rock_pad, np.float32) * 255.0).astype(np.int32)
        rm_src = np.zeros((hh, ww), np.uint8)
        if river_meta is not None:
            rm_src[pad:pad + H, pad:pad + W] = (np.asarray(river_meta) > 0).astype(np.uint8)
    else:
        sy = surface_y.astype(np.float32)
        hh, ww = H, W
        ox, oy = tile_x * W, tile_y * H
        flow_src = flow_tile
        tier = np.round(np.asarray(rock_layers_tile, np.float32) * 255.0).astype(np.int32)
        rm_src = (np.asarray(river_meta) > 0).astype(np.uint8) if river_meta is not None else None
    rock = tier >= min_tier
    if not rock.any():
        return surface_y

    from core.column_generator import SEA_LEVEL, MC_Y_MAX

    # ---- 1. Flow incision (band-limited tributary channels) ----
    f = np.clip(np.asarray(flow_src, np.float32), 0.0, 1.0)
    flo = float(fcfg.get("flow_lo", 0.001))
    fhi = float(fcfg.get("flow_hi", 0.02))
    max_incise = float(fcfg.get("max_incise_blocks", 10.0))
    gamma = float(fcfg.get("incise_gamma", 0.6))
    incise = np.clip((f - flo) / max(1e-6, fhi - flo), 0.0, 1.0) ** gamma
    dy = -max_incise * incise

    # ---- 2. Synthetic gully network (ridged simplex at world coords) ----
    gully_amp = float(fcfg.get("gully_amp_blocks", 4.0))
    if gully_amp > 0.0:
        scale = max(2.0, float(fcfg.get("gully_scale_blocks", 24.0)))
        sharp = float(fcfg.get("gully_sharpness", 2.0))
        try:
            from opensimplex import OpenSimplex
            _os = OpenSimplex(seed=int(fcfg.get("seed", 1337)))
            xs = (ox + np.arange(ww, dtype=np.float64)) / scale
            ys = (oy + np.arange(hh, dtype=np.float64)) / scale
            n = _os.noise2array(xs, ys).astype(np.float32)      # (hh, ww) in [-1,1]
            ridged = (1.0 - np.abs(n)) ** sharp                  # [0,1], lines at n~0
            dy = dy - gully_amp * ridged
        except Exception:
            pass

    # ---- 3. Ridge sharpening (raise convex noses) ----
    ridge_amp = float(fcfg.get("ridge_amp_blocks", 3.0))
    if ridge_amp > 0.0:
        from scipy.ndimage import gaussian_filter
        sigma = float(fcfg.get("ridge_sigma", 4.0))
        conv = sy - gaussian_filter(sy, sigma)                   # >0 convex
        ref = max(0.5, float(fcfg.get("ridge_ref_blocks", 3.0)))
        dy = dy + ridge_amp * np.clip(conv / ref, 0.0, 1.0)

    # ---- 4. FACE deepening: deepen+widen gullies ONLY on the big SMOOTH sloped
    # cliff faces (the blobby planar faces), NOT where the terrain already has
    # lots of natural variation. face_weight = smoothness(low local roughness) x
    # slope-in-face-range. Adds extra incision + a WIDER (larger-scale) gully on
    # those faces so they get carved hard, while already-rough terrain is left
    # alone (avoids piling erosion onto natural detail). ---------------------
    face_extra_incise = float(fcfg.get("face_extra_incise_blocks", 0.0))
    face_extra_gully = float(fcfg.get("face_extra_gully_blocks", 0.0))
    if face_extra_incise > 0.0 or face_extra_gully > 0.0:
        from scipy.ndimage import gaussian_filter as _gf_face
        # local slope from surface_y (1 block/px at tile res -> rise/run direct)
        _gy, _gx = np.gradient(sy)
        _slope_deg = np.degrees(np.arctan(np.sqrt(_gy * _gy + _gx * _gx)))
        _fmin = float(fcfg.get("face_min_slope_deg", 28.0))
        _fmax = float(fcfg.get("face_max_slope_deg", 60.0))
        # 0 below fmin, ramp to 1 by ~fmin+8, hold to fmax, off above (vertical)
        _slope_w = np.clip((_slope_deg - _fmin) / 8.0, 0.0, 1.0) \
            * np.clip((_fmax - _slope_deg) / 8.0 + 1.0, 0.0, 1.0)
        # MACRO roughness: deviation from a LARGE-sigma smoothed surface so MC's
        # block-level staircase steps DON'T count as "rough" -- only real gully/
        # ridge variation (the stuff we want to leave alone) suppresses the face.
        # A macro-planar cliff face reads SMOOTH here even though it's staircased.
        _rsig = float(fcfg.get("face_rough_sigma", 16.0))
        _rref = max(0.1, float(fcfg.get("face_rough_ref", 5.0)))
        _rough = np.abs(sy - _gf_face(sy, _rsig))
        _smooth_w = np.clip(1.0 - _rough / _rref, 0.0, 1.0)       # high on flat faces
        _face = (_slope_w * _smooth_w).astype(np.float32)
        # FLOW-INDEPENDENT carve: a big planar face sheds water (~0 flow), so the
        # deepening must NOT be flow-gated (the old `* incise` left open faces
        # untouched). Carve the ridged gully pattern into the face directly:
        # face_extra_incise = uniform face recession, face_extra_gully = gully
        # channel depth. edge-fade + smooth downstream keep it clean.
        try:
            from opensimplex import OpenSimplex as _OSf
            _wsc = max(2.0, float(fcfg.get("face_gully_scale_blocks", 40.0)))
            _osf = _OSf(seed=int(fcfg.get("seed", 1337)) + 991)
            _fxs = (ox + np.arange(ww, dtype=np.float64)) / _wsc
            _fys = (oy + np.arange(hh, dtype=np.float64)) / _wsc
            _fn = _osf.noise2array(_fxs, _fys).astype(np.float32)
            _fridged = (1.0 - np.abs(_fn)) ** float(fcfg.get("gully_sharpness", 2.0))
        except Exception:
            _fridged = np.zeros((hh, ww), np.float32)
        if face_extra_incise > 0.0:
            dy = dy - face_extra_incise * _face                  # uniform recession
        if face_extra_gully > 0.0:
            dy = dy - face_extra_gully * _face * _fridged        # gully channels

    # ---- Anti-artifact: EDGE FADE + SMOOTH the erosion delta ----
    # The ridged gully + face terms are sharp per-cell; applied raw they make
    # jagged 1-block columns and the face on/off boundary makes hard seamlines.
    # (1) taper dy to 0 within edge_fade_blocks of the rock boundary so there's
    #     no step against the surrounding land (the seam fix), then
    # (2) gaussian-smooth dy so the gullies are smooth valleys, not staircases.
    from scipy.ndimage import gaussian_filter as _gf_smooth
    from scipy.ndimage import distance_transform_edt as _edt_fe
    _efb = float(fcfg.get("edge_fade_blocks", 6.0))
    if _efb > 0.0:
        _rfade = np.clip(_edt_fe(rock).astype(np.float32) / _efb, 0.0, 1.0)
        dy = dy * _rfade
    # RIVER FADE (fixes the "floating-bridge" inversion): the incision is
    # strongest on the highest-flow cells = the valley centerline, but the river
    # carver's channel there is PROTECTED (dy=0). So the protected center stays
    # up while its high-flow flanks get cut below it -> the valley floor becomes
    # a raised bridge between fresh walls. Taper dy to 0 within river_fade_blocks
    # of any river/lake cell so the incision blends INTO the valley floor instead
    # of cutting a slot beside it.
    _rvf = float(fcfg.get("river_fade_blocks", 16.0))
    if _rvf > 0.0 and rm_src is not None:
        _rm = np.asarray(rm_src) > 0
        if _rm.any():
            _rvfade = np.clip(_edt_fe(~_rm).astype(np.float32) / _rvf, 0.0, 1.0)
            dy = dy * _rvfade
    _ssig = float(fcfg.get("smooth_sigma", 2.0))
    if _ssig > 0.0:
        # In seam mode the halo gives real neighbour context; 'nearest' only
        # replicates at the OUTER halo edge, which is cropped away below.
        dy = _gf_smooth(dy, _ssig, mode="nearest")

    # ---- Gate + clamp ----
    dy[~rock] = 0.0
    if rm_src is not None:
        dy[np.asarray(rm_src) > 0] = 0.0
    if _seam:
        dy = dy[pad:pad + H, pad:pad + W]   # crop displacement to inner tile
    sy2 = surface_y.astype(np.float32) + dy
    # never push a rock cell below sea (it would read as water-filled gully)
    if fcfg.get("rock_only_sea_clamp", False):
        # ISLAND fix (S97 archipelago flood): the SEA_LEVEL+1 floor must apply
        # ONLY to the ROCK cells that `dy` actually touched. The old whole-array
        # clip raised EVERY non-rock cell to SEA_LEVEL+1 too -- including the flat
        # below-sea OCEAN shelf -- so any tile that merely CONTAINED some rock
        # (every islet tile) had its entire inter-island sea (which sits at
        # ~Y48-62, just under Y63) lifted to Y64 = solid land. Dense archipelagos
        # (Grenadines/Kostati/New Vincentia) flooded wholesale; consolidated
        # islands have little shelf so it went unseen. Clamp rock cells only;
        # non-rock ocean/land keep their carved surface_y. Mainland (flag absent)
        # keeps the exact old whole-array clip -> byte-identical.
        from core.column_generator import MC_Y_MIN as _MC_Y_MIN
        _rock_inner = rock[pad:pad + H, pad:pad + W] if _seam else rock
        sy2 = np.clip(sy2, float(_MC_Y_MIN + 4), float(MC_Y_MAX - 1))
        sy2 = np.where(_rock_inner, np.maximum(sy2, float(SEA_LEVEL + 1)), sy2)
    else:
        sy2 = np.clip(sy2, float(SEA_LEVEL + 1), float(MC_Y_MAX - 1))
    return np.round(sy2).astype(surface_y.dtype)
