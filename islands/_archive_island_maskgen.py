"""_archive_island_maskgen.py — ARCHIVED island mask-gen duplicates (S95).

These REIMPLEMENTED the mainland mask generation in the island bake and kept
missing pieces (dark/mid/light rock tiers, talus, ecotone, etc.). Per the user's
directive, the island bake now ROUTES THROUGH THE REAL mainland generator:
`tools/build_lithology.py` + `tools/build_terrain_derived.py` are run (size-
parametric) against the island masks dir with the real `config/thresholds.json`.

These functions are kept here for reference ONLY — they are not imported or called
by `islands/render_islands.py` anymore. They need `_wtif` + `np` from the bake
context to run; treat as a historical snapshot, not live code.

Replacements:
  synth_lithology              -> tools/build_lithology.py build_lithology()
  build_island_terrain_derived -> tools/build_terrain_derived.py main()
  _resize_nn / _resize_lin     -> (only used by build_island_terrain_derived)
"""
import numpy as np


def _resize_nn(a, H, W):
    ri = np.clip(np.arange(H) * a.shape[0] // H, 0, a.shape[0] - 1)
    ci = np.clip(np.arange(W) * a.shape[1] // W, 0, a.shape[1] - 1)
    return a[ri][:, ci]


def _resize_lin(a, H, W):
    from scipy.ndimage import zoom
    z = zoom(a.astype(np.float32), (H / a.shape[0], W / a.shape[1]), order=1, prefilter=False)
    out = np.zeros((H, W), np.float32)
    hh, ww = min(H, z.shape[0]), min(W, z.shape[1])
    out[:hh, :ww] = z[:hh, :ww]
    if hh < H:
        out[hh:, :] = out[hh - 1:hh, :]
    if ww < W:
        out[:, ww:] = out[:, ww - 1:ww]
    return out


def synth_lithology(override, cfg):
    from core.biome_assignment import OVERRIDE_BIOME_MAP
    L = cfg["lithology"]
    z2g = L.get("zone_to_group", {})
    g2id = {g: int(d.get("id", 0)) for g, d in L.get("groups", {}).items()}
    lith = np.zeros(override.shape, np.uint8)
    for zone, biome in OVERRIDE_BIOME_MAP.items():
        gid = g2id.get(z2g.get(biome))
        if gid:
            lith[override == int(zone)] = gid
    return lith


def build_island_terrain_derived(od, mcy, override, flow_u16, cfg, ws=4, lith_full=None, _wtif=None):
    """ARCHIVED — replaced by tools/build_terrain_derived.py run on the island dir."""
    from scipy.ndimage import uniform_filter, gaussian_filter, laplace
    from tools.build_terrain_derived import (slope_deg_from_surface_y, build_aspect,
        build_bedrock_drainage, build_talus_apron, build_cliff_cap,
        build_rock_layers_u, _wind_factor)
    H, W = mcy.shape
    L = cfg["lithology"]; rl_cfg = L["rock_layers"]
    cap_cfg = L.get("cliff_cap", {}); talus_cfg = L.get("talus", {})
    bedrock_cfg = L.get("bedrock_drainage", {})
    aspect_cfg = cfg.get("eco_gradients", {}).get("aspect", {})
    name_to_id = {g: int(d.get("id", 0)) for g, d in L.get("groups", {}).items()}
    sy = mcy[::ws, ::ws].astype(np.float32)
    sy_i = sy.astype(np.int16)
    slope_deg = slope_deg_from_surface_y(sy_i, scale=ws)
    flow_ws = flow_u16[::ws, ::ws].astype(np.float32) / 65535.0
    lith_ws = (lith_full[::ws, ::ws] if lith_full is not None
               else synth_lithology(override[::ws, ::ws], cfg))
    nbr = (uniform_filter(sy, size=3) * 9.0 - sy) / 8.0
    conc = nbr - sy
    concavity_pos = np.clip(conc / max(float(np.abs(conc).max()), 1e-6), 0.0, 1.0).astype(np.float32)
    cap_sigma = max(1.0, float(cap_cfg.get("curv_smooth_blocks", 48.0)) / ws)
    conv = np.clip(-laplace(gaussian_filter(sy, sigma=cap_sigma)), 0.0, None)
    cp = float(np.percentile(conv[conv > 0], 95)) if (conv > 0).any() else 1.0
    convex_norm = np.clip(conv / max(cp, 1e-6), 0.0, 1.0).astype(np.float32)
    w2p = lambda b: max(1, int(round(float(b) / ws)))
    aspect = build_aspect(sy_i, slope_deg, float(aspect_cfg.get("slope_min_deg", 5.0)))
    bedrock = build_bedrock_drainage(flow_ws, slope_deg,
        float(bedrock_cfg.get("flow_threshold", 0.02)), float(bedrock_cfg.get("slope_min_deg", 25.0)),
        w2p(bedrock_cfg.get("dilation_blocks", 1)), w2p(bedrock_cfg.get("fade_blocks", 3)))
    talus = build_talus_apron(sy_i, slope_deg,
        float(talus_cfg.get("cliff_min_deg", 35.0)), float(talus_cfg.get("apron_max_deg", 35.0)),
        w2p(talus_cfg.get("search_blocks", 80)), concavity_pos, float(talus_cfg.get("gully_fan_coeff", 0.0)))
    wf = _wind_factor(sy_i, float(cap_cfg.get("wind_source_deg", 292.0)))
    cap = build_cliff_cap(sy_i, convex_norm, wf, cap_cfg)
    u = build_rock_layers_u(sy_i, slope_deg, lith_ws, name_to_id, rl_cfg)
    _wtif(od / "aspect.tif", _resize_nn(aspect, H, W))
    _wtif(od / "bedrock_drainage.tif", np.clip(_resize_lin(bedrock, H, W), 0, 255).astype(np.uint8))
    _wtif(od / "talus_apron.tif", np.clip(_resize_lin(talus, H, W), 0, 255).astype(np.uint8))
    _wtif(od / "cliff_cap.tif", np.clip(_resize_lin(cap, H, W), 0, 255).astype(np.uint8))
    u_full = _resize_lin(u, H, W)
    rng = np.random.default_rng(0x52CD)
    u_full = u_full + (rng.random(u_full.shape, dtype=np.float32) - 0.5) * 0.5
    rock_layers = np.digitize(u_full, [1.0, 2.0, 3.0]).astype(np.uint8)
    _wtif(od / "rock_layers.tif", rock_layers)
