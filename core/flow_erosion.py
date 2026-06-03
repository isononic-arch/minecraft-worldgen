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
) -> np.ndarray:
    """Return an eroded copy of surface_y (same dtype). No-op if disabled / no rock."""
    fcfg = cfg.get("flow_erosion", {}) if isinstance(cfg, dict) else {}
    if not fcfg.get("enabled", False):
        return surface_y
    if rock_layers_tile is None or flow_tile is None:
        return surface_y

    H, W = surface_y.shape
    tier = np.round(np.asarray(rock_layers_tile, np.float32) * 255.0).astype(np.int32)
    min_tier = int(fcfg.get("min_rock_tier", 1))
    rock = tier >= min_tier
    if not rock.any():
        return surface_y

    from core.column_generator import SEA_LEVEL, MC_Y_MAX

    sy = surface_y.astype(np.float32)

    # ---- 1. Flow incision (band-limited tributary channels) ----
    f = np.clip(np.asarray(flow_tile, np.float32), 0.0, 1.0)
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
            xs = (tile_x * W + np.arange(W, dtype=np.float64)) / scale
            ys = (tile_y * H + np.arange(H, dtype=np.float64)) / scale
            n = _os.noise2array(xs, ys).astype(np.float32)      # (H, W) in [-1,1]
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
        # local roughness: deviation from a smoothed surface (high = already varied)
        _rsig = float(fcfg.get("face_rough_sigma", 4.0))
        _rref = max(0.1, float(fcfg.get("face_rough_ref", 3.0)))
        _rough = np.abs(sy - _gf_face(sy, _rsig))
        _smooth_w = np.clip(1.0 - _rough / _rref, 0.0, 1.0)       # high on flat faces
        _face = (_slope_w * _smooth_w).astype(np.float32)
        if face_extra_incise > 0.0:
            dy = dy - face_extra_incise * _face * incise
        if face_extra_gully > 0.0:
            try:
                from opensimplex import OpenSimplex as _OSf
                _wsc = max(2.0, float(fcfg.get("face_gully_scale_blocks", 40.0)))
                _osf = _OSf(seed=int(fcfg.get("seed", 1337)) + 991)
                _fxs = (tile_x * W + np.arange(W, dtype=np.float64)) / _wsc
                _fys = (tile_y * H + np.arange(H, dtype=np.float64)) / _wsc
                _fn = _osf.noise2array(_fxs, _fys).astype(np.float32)
                _fridged = (1.0 - np.abs(_fn)) ** float(fcfg.get("gully_sharpness", 2.0))
                dy = dy - face_extra_gully * _face * _fridged
            except Exception:
                pass

    # ---- Anti-artifact: EDGE FADE + SMOOTH the erosion delta ----
    # The ridged gully + face terms are sharp per-cell; applied raw they make
    # jagged 1-block columns and the face on/off boundary makes hard seamlines.
    # (1) taper dy to 0 within edge_fade_blocks of the rock boundary so there's
    #     no step against the surrounding land (the seam fix), then
    # (2) gaussian-smooth dy so the gullies are smooth valleys, not staircases.
    from scipy.ndimage import gaussian_filter as _gf_smooth
    _efb = float(fcfg.get("edge_fade_blocks", 6.0))
    if _efb > 0.0:
        from scipy.ndimage import distance_transform_edt as _edt_fe
        _rfade = np.clip(_edt_fe(rock).astype(np.float32) / _efb, 0.0, 1.0)
        dy = dy * _rfade
    _ssig = float(fcfg.get("smooth_sigma", 2.0))
    if _ssig > 0.0:
        dy = _gf_smooth(dy, _ssig, mode="nearest")

    # ---- Gate + clamp ----
    dy[~rock] = 0.0
    if river_meta is not None:
        dy[np.asarray(river_meta) > 0] = 0.0
    sy2 = sy + dy
    sy2 = np.clip(sy2, float(SEA_LEVEL + 1), float(MC_Y_MAX - 1))
    # never push a rock cell below sea (it would read as water-filled gully)
    return np.round(sy2).astype(surface_y.dtype)
