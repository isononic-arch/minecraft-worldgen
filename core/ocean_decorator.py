"""
core/ocean_decorator.py  (S62)

Underwater geology pass. Runs AFTER decorate_surface on pixels where
`surface_y < sea_level AND override == 0` (true ocean floor only).  Lakes
have their own palette and are NOT affected.

Three independently-toggleable sub-features, all gated behind the single
feature flag `cfg["ocean"]["enabled"]`:

  1. `palette` — depth-tiered surface/subsurface block selection, with fBm
     noise variation so tiers don't render as hard bands.  Shallow / mid /
     deep depth ranges + weighted block lists.
  2. `floor_noise` — ±N Y simplex noise added to ocean-floor surface_y for
     subtle relief.  Hard-clipped below sea level.
  3. `coastline_shape` — per-pixel gamma reshape of depth.  Alternative to
     touching the global `terrain_spline`.  gamma > 1 → gradual coast (long
     shallow shelf).  gamma < 1 → sharp coast (deep water close to shore).
     gamma = 1.0 → identity (no change).

ISOLATION: the ocean_mask guard (`override == 0 & surface_y < sea_level`)
ensures land pixels are PHYSICALLY unreachable from this module.  Nothing
above sea level, nothing in a named biome, is touched.

Invocation (called at end of surface_decorator.decorate_surface):
  from core.ocean_decorator import decorate_ocean
  decorate_ocean(surface_y, surface_blk, sub_blk, biome_grid, cfg,
                 tile_world_x, tile_world_z)
"""

from __future__ import annotations
import numpy as np
from opensimplex import OpenSimplex
from scipy.ndimage import zoom as _zoom

SEA_LEVEL   = 63   # MC Y at water surface (CLAUDE.md hard rule)
OCEAN_MIN_Y = -60  # clamp floor; prevents runaway noise from punching below bedrock


def _paint_tier(tier_mask, blocks, sub_blocks, surface_blk, sub_blk, pal_noise):
    """S65: paint a depth tier's surface + subsurface blocks where tier_mask
    is True.  Selection via cumulative-weight searchsorted against pal_noise.
    Shared between standard palette and mangrove-variant palette passes."""
    if not blocks or not tier_mask.any():
        return
    weights = np.array([b[1] for b in blocks], dtype=np.float32)
    weights = weights / weights.sum()
    cumu = np.cumsum(weights)
    pick = np.searchsorted(cumu, pal_noise, side='right')
    pick = np.clip(pick, 0, len(blocks) - 1)
    names = [b[0] for b in blocks]
    for i, name in enumerate(names):
        m = tier_mask & (pick == i)
        if m.any():
            surface_blk[m] = name
    # Subsurface
    if sub_blocks:
        sub_names = [b[0] for b in sub_blocks]
        sub_w = np.array([b[1] for b in sub_blocks], dtype=np.float32)
        sub_w = sub_w / sub_w.sum()
        sub_cumu = np.cumsum(sub_w)
        sub_pick = np.searchsorted(sub_cumu, pal_noise, side='right')
        sub_pick = np.clip(sub_pick, 0, len(sub_blocks) - 1)
        for i, name in enumerate(sub_names):
            m = tier_mask & (sub_pick == i)
            if m.any():
                sub_blk[m] = name
    else:
        # Mirror surface to sub by default
        for i, name in enumerate(names):
            m = tier_mask & (pick == i)
            if m.any():
                sub_blk[m] = name


def _coarse_simplex(seed: int, H: int, W: int, tile_wx: int, tile_wz: int,
                    scale: float, step: int = 4) -> np.ndarray:
    """Low-resolution simplex grid at world coordinates, upsampled to (H,W).
    step=4 sampling + bilinear upscale is ~16× faster than per-pixel simplex."""
    sim = OpenSimplex(seed=seed)
    gy = np.arange(0, H, step)
    gx = np.arange(0, W, step)
    coarse = np.empty((len(gy), len(gx)), dtype=np.float32)
    for i, y in enumerate(gy):
        for j, x in enumerate(gx):
            coarse[i, j] = sim.noise2((tile_wx + x) / scale,
                                      (tile_wz + y) / scale)
    full = _zoom(coarse, (H / coarse.shape[0], W / coarse.shape[1]), order=1)
    return full[:H, :W]


def decorate_ocean(
    surface_y:    np.ndarray,  # (H, W) int16 — mutated in place
    surface_blk:  np.ndarray,  # (H, W) object(str) — mutated in place
    sub_blk:      np.ndarray,  # (H, W) object(str) — mutated in place
    biome_grid:   np.ndarray,  # (H, W) object(str) — biome names
    cfg:          dict,
    tile_world_x: int = 0,
    tile_world_z: int = 0,
    ground_cover: np.ndarray | None = None,  # (H, W) object(str) — S63: receives underwater vegetation
) -> dict:
    """
    Mutates surface_y / surface_blk / sub_blk in place for true-ocean pixels.
    Returns a dict of per-tier pixel counts for logging; returns empty dict
    and makes no changes if `cfg['ocean']['enabled']` is False / missing.
    """
    ocfg = cfg.get("ocean", {})
    if not ocfg.get("enabled", False):
        return {}

    # ISOLATION GUARD — ocean mask computed ONCE, reused for every write.
    # S65: dropped biome gate.  User's override.tif paints land biomes on
    # pixels that end up below sea level (since override was painted without
    # reference to the height spline).  Those pixels are PHYSICALLY water
    # columns and should get ocean decoration regardless of biome name.
    # Land pixels (surface_y >= 63) physically unreachable — never touched.
    is_ocean = (surface_y < SEA_LEVEL)
    if not is_ocean.any():
        return {}

    stats: dict[str, int] = {"ocean_px": int(is_ocean.sum())}
    H, W = surface_y.shape

    # ─── 3. Coastline gamma reshape (first, affects subsequent depth lookups) ──
    coast_cfg = ocfg.get("coastline_shape", {})
    gamma = float(coast_cfg.get("gamma", 1.0))
    max_depth = float(coast_cfg.get("max_depth_for_reshape", 80))
    if abs(gamma - 1.0) > 1e-6:
        depth = (SEA_LEVEL - surface_y).astype(np.float32)
        depth = np.clip(depth, 0, max_depth)
        normed = depth / max_depth
        new_depth = (normed ** gamma) * max_depth
        new_sy = (SEA_LEVEL - new_depth).astype(np.int16)
        # Only update ocean pixels.  Use boolean index — no `out=` needed.
        surface_y[is_ocean] = new_sy[is_ocean]
        # Safety clamp: never let ocean surface reach sea level.
        over_cap = is_ocean & (surface_y >= SEA_LEVEL)
        if over_cap.any():
            surface_y[over_cap] = SEA_LEVEL - 1
        stats["reshape_applied"] = 1

    # ─── 2. Floor noise (after reshape so noise sits on the new profile) ──
    noise_cfg = ocfg.get("floor_noise", {})
    amp = int(noise_cfg.get("amplitude_y", 0))
    scale = float(noise_cfg.get("scale_blocks", 30.0))
    seed = int(noise_cfg.get("seed", 747))
    if amp > 0:
        noise = _coarse_simplex(seed, H, W, tile_world_x, tile_world_z, scale, step=4)
        noise_int = np.round(noise * amp).astype(np.int16)
        # Apply only to ocean pixels.
        surface_y[is_ocean] = (surface_y[is_ocean] + noise_int[is_ocean]).astype(np.int16)
        # Re-clamp: never push surface_y above sea_level-1 or below OCEAN_MIN_Y.
        over_cap = is_ocean & (surface_y >= SEA_LEVEL)
        if over_cap.any():
            surface_y[over_cap] = SEA_LEVEL - 1
        under_cap = is_ocean & (surface_y < OCEAN_MIN_Y)
        if under_cap.any():
            surface_y[under_cap] = OCEAN_MIN_Y
        stats["noise_applied"] = 1

    # ─── S65 Mangrove variant mask — underwater pixels whose nearest LAND
    # pixel is MANGROVE_COAST get the brackish palette instead of default ocean.
    mangrove_variant_mask = np.zeros(surface_y.shape, dtype=bool)
    mv_cfg = ocfg.get("biome_variants", {}).get("MANGROVE_COAST", {})
    if mv_cfg.get("enabled", False):
        mangrove_land = (biome_grid == "MANGROVE_COAST") & (surface_y >= SEA_LEVEL)
        if mangrove_land.any():
            from scipy.ndimage import binary_dilation as _bd
            max_reach = int(mv_cfg.get("max_reach_blocks", 40))
            mangrove_variant_mask = (
                _bd(mangrove_land, iterations=max_reach) & is_ocean
            )
            stats["variant_mangrove_px"] = int(mangrove_variant_mask.sum())

    # ─── 1. Depth-tiered palette ──────────────────────────────────────
    pal_cfg = ocfg.get("palette", {})
    if pal_cfg:
        # Recompute depth (may have changed from reshape + noise)
        depth = SEA_LEVEL - surface_y
        # Palette-selection noise — separate seed, finer scale so tiers
        # break into patches not bands.
        pal_noise = _coarse_simplex(seed + 1, H, W, tile_world_x, tile_world_z,
                                    scale=15.0, step=2)
        # Normalize to [0, 1] — clamp to avoid edge issues
        pal_noise = (pal_noise + 1.0) * 0.5
        pal_noise = np.clip(pal_noise, 0.0, 0.9999)

        # Tiers processed shallow → deep so earlier writes are overwritten
        # by more specific tiers if ranges overlap.
        tier_order = [("shallow", pal_cfg.get("shallow", {})),
                      ("mid",     pal_cfg.get("mid", {})),
                      ("deep",    pal_cfg.get("deep", {}))]

        # S65: mangrove palette overrides for shallow/mid (not deep — deep
        # water isn't near mangroves structurally).
        mv_pal = ocfg.get("biome_variants", {}).get("MANGROVE_COAST", {}).get("palette", {})

        for tier_name, tier in tier_order:
            if not tier:
                continue
            dmin, dmax = tier.get("depth_range", [0, 5])
            tier_mask_raw = is_ocean & (depth >= dmin) & (depth < dmax)
            # Split tier into mangrove-variant and standard regions
            mv_tier_mask = tier_mask_raw & mangrove_variant_mask
            std_tier_mask = tier_mask_raw & ~mangrove_variant_mask
            n = int(tier_mask_raw.sum())
            stats[f"tier_{tier_name}_px"] = n
            if n == 0:
                continue

            # Paint mangrove variant first (only if tier has a mangrove palette)
            mv_tier_blocks = mv_pal.get(tier_name, {}).get("blocks", []) if mv_pal else []
            mv_tier_subs = mv_pal.get(tier_name, {}).get("sub_blocks", []) if mv_pal else []
            if mv_tier_blocks and mv_tier_mask.any():
                _paint_tier(mv_tier_mask, mv_tier_blocks, mv_tier_subs,
                            surface_blk, sub_blk, pal_noise)
                stats[f"tier_{tier_name}_mv_px"] = int(mv_tier_mask.sum())
            elif mv_tier_mask.any():
                # No mangrove variant for this tier — apply standard palette
                std_tier_mask = std_tier_mask | mv_tier_mask

            # Standard palette
            blocks = tier.get("blocks", [])
            sub_blocks = tier.get("sub_blocks")  # optional override for subsurface
            if not blocks:
                continue
            # Redefine tier_mask as the non-variant portion for standard paint
            tier_mask = std_tier_mask

            # Build cumulative weights and slot each pixel via searchsorted.
            weights = np.array([b[1] for b in blocks], dtype=np.float32)
            weights = weights / weights.sum()
            cumu = np.cumsum(weights)
            # pick index via searchsorted — O(N log K) for K tiers
            pick = np.searchsorted(cumu, pal_noise, side='right')
            pick = np.clip(pick, 0, len(blocks) - 1)

            names = [b[0] for b in blocks]
            # Surface
            for i, name in enumerate(names):
                mask_i = tier_mask & (pick == i)
                if mask_i.any():
                    surface_blk[mask_i] = name
            # Subsurface
            if sub_blocks:
                sub_names = [b[0] for b in sub_blocks]
                sub_weights = np.array([b[1] for b in sub_blocks], dtype=np.float32)
                sub_weights = sub_weights / sub_weights.sum()
                sub_cumu = np.cumsum(sub_weights)
                sub_pick = np.searchsorted(sub_cumu, pal_noise, side='right')
                sub_pick = np.clip(sub_pick, 0, len(sub_blocks) - 1)
                for i, name in enumerate(sub_names):
                    mask_i = tier_mask & (sub_pick == i)
                    if mask_i.any():
                        sub_blk[mask_i] = name
            else:
                # Default subsurface mirrors surface (safe fallback)
                for i, name in enumerate(names):
                    mask_i = tier_mask & (pick == i)
                    if mask_i.any():
                        sub_blk[mask_i] = name

    # ─── S63: Underwater vegetation (seagrass / kelp / sea_pickle) ─────────
    veg_cfg = ocfg.get("vegetation", {})
    if veg_cfg.get("enabled", False) and ground_cover is not None:
        # Only place vegetation on soft substrate (sand/gravel/dirt/clay).
        # No vegetation on bare stone/tuff/deepslate.
        _SOFT_SUBSTRATES = frozenset({"sand", "gravel", "dirt", "clay",
                                       "coarse_dirt", "rooted_dirt"})
        soft_mask = np.zeros_like(is_ocean, dtype=bool)
        for _blk in _SOFT_SUBSTRATES:
            soft_mask |= (surface_blk == _blk)

        # Depth grid after any reshape/noise above
        depth_v = SEA_LEVEL - surface_y

        # Vegetation-selection noise — fine scale so patches, not solid carpets
        veg_noise_seed = int(noise_cfg.get("seed", 747)) + 2
        veg_noise = _coarse_simplex(veg_noise_seed, H, W,
                                     tile_world_x, tile_world_z,
                                     scale=8.0, step=2)
        # Normalize to [0,1]
        veg_noise = (veg_noise + 1.0) * 0.5
        veg_noise = np.clip(veg_noise, 0.0, 0.9999)

        # S66: replace per-pixel uniform coin with a CLUMPING field — low-freq
        # fBm noise so vegetation forms patches (like real kelp forests / bare
        # seafloor pockets).  Combined with per-pixel uniform jitter to keep
        # edges ragged.  The `clump_noise` controls "is this area vegetated at
        # all"; `coin` then decides per-pixel within vegetated areas.
        clump_noise = _coarse_simplex(veg_noise_seed + 50, H, W,
                                       tile_world_x, tile_world_z,
                                       scale=35.0, step=4)  # low-freq = big clumps
        clump_noise = (clump_noise + 1.0) * 0.5
        clump_noise = np.clip(clump_noise, 0.0, 1.0)
        # S66: kelp-forest shape — strong clumping, bare gaps between.  At
        # clump_noise > 0.6, full probability; below 0.3, near-zero (bare).
        clump_gate = np.clip((clump_noise - 0.30) / 0.30, 0.0, 1.0).astype(np.float32)

        # Per-pixel uniform edge-softener
        rng_veg = np.random.default_rng(veg_noise_seed + 100)
        coin = rng_veg.random(surface_y.shape).astype(np.float32)

        veg_tiers = [
            ("shallow", veg_cfg.get("shallow", {})),
            ("mid",     veg_cfg.get("mid", {})),
            ("deep",    veg_cfg.get("deep", {})),
        ]
        for tier_name, tier in veg_tiers:
            if not tier:
                continue
            dmin, dmax = tier.get("depth_range", [1, 5])
            tier_mask = is_ocean & soft_mask & (depth_v >= dmin) & (depth_v < dmax)
            n = int(tier_mask.sum())
            stats[f"veg_{tier_name}_candidates"] = n
            if n == 0:
                continue
            items = tier.get("items", [])
            if not items:
                continue

            # S66: vegetation density modulated by clump_gate (low = bare,
            # high = full probability).  Real effective p per pixel =
            # total_p * clump_gate — bare spots emerge naturally in low-noise
            # areas.
            weights = np.array([it[1] for it in items], dtype=np.float32)
            total_p = float(weights.sum())
            effective_p = total_p * clump_gate
            place_mask = tier_mask & (coin < effective_p)
            if not place_mask.any():
                continue
            # Per-pixel item selection via cumulative share of total_p
            cumu = np.cumsum(weights) / total_p
            pick = np.searchsorted(cumu, veg_noise, side='right')
            pick = np.clip(pick, 0, len(items) - 1)
            for i, (name, _w) in enumerate(items):
                m_i = place_mask & (pick == i)
                if m_i.any():
                    ground_cover[m_i] = name
                    stats[f"veg_{tier_name}_{name}"] = int(m_i.sum())

    # ─── S65: Ocean features — boulders, coral clusters, glow_lichen accents ──
    feat_cfg = ocfg.get("features", {})
    if feat_cfg.get("enabled", False):
        _place_ocean_features(
            is_ocean, surface_y, surface_blk, sub_blk, biome_grid,
            feat_cfg, tile_world_x, tile_world_z, stats,
        )

    return stats


def _place_ocean_features(
    is_ocean, surface_y, surface_blk, sub_blk, biome_grid,
    feat_cfg, tile_world_x, tile_world_z, stats,
) -> None:
    """S65: sparse visual accents on the ocean floor.  Overwrites surface_blk
    only.  Does NOT modify ground_cover or surface_y.  All fully isolated
    to is_ocean pixels."""
    H, W = surface_y.shape
    depth = SEA_LEVEL - surface_y
    rng_base = int(feat_cfg.get("seed", 8675))

    # ─── Boulders: per-pixel 0.0008 chance of cobblestone/mossy_cobblestone/stone
    # cluster (radius 2-4).  Shallow + mid tiers only, not in deep ocean.
    boulder_cfg = feat_cfg.get("boulders", {})
    if boulder_cfg.get("enabled", True):
        b_prob = float(boulder_cfg.get("probability", 0.0008))
        b_tiers = (depth >= 1) & (depth < 20)
        b_mask = is_ocean & b_tiers
        rng = np.random.default_rng(rng_base ^ 0xB0B0)
        b_coin = rng.random(surface_y.shape)
        centers = b_mask & (b_coin < b_prob)
        if centers.any():
            # Expand centers into 3-block-radius blobs
            from scipy.ndimage import binary_dilation
            blob = binary_dilation(centers, iterations=3) & is_ocean
            # Mix cobblestone/mossy_cobblestone/stone/tuff
            rock_noise = _coarse_simplex(rng_base ^ 0xBA11, H, W,
                                          tile_world_x, tile_world_z,
                                          scale=6.0, step=2)
            rock_noise = (rock_noise + 1.0) * 0.5
            rock_pick = np.floor(rock_noise * 4).astype(int) % 4
            names = ["cobblestone", "mossy_cobblestone", "stone", "tuff"]
            for i, name in enumerate(names):
                m = blob & (rock_pick == i)
                if m.any():
                    surface_blk[m] = name
            stats["feat_boulder_px"] = int(blob.sum())

    # ─── Coral clusters: shallow tropical only (near MANGROVE/TIDAL_JUNGLE/LUSH_RF)
    coral_cfg = feat_cfg.get("coral", {})
    if coral_cfg.get("enabled", True):
        TROPICAL_ADJACENT = ("MANGROVE_COAST", "TIDAL_JUNGLE_FRINGE",
                              "LUSH_RAINFOREST_COAST", "RAINFOREST_COAST")
        # Distance-based tropical proximity
        tropical_mask = np.zeros(biome_grid.shape, dtype=bool)
        for b in TROPICAL_ADJACENT:
            tropical_mask |= (biome_grid == b)
        if tropical_mask.any():
            from scipy.ndimage import binary_dilation
            near_tropical = binary_dilation(tropical_mask, iterations=24)
            c_prob = float(coral_cfg.get("probability", 0.0008))
            c_tiers = (depth >= 1) & (depth < 7)
            c_mask = is_ocean & c_tiers & near_tropical
            rng = np.random.default_rng(rng_base ^ 0xC0FA)
            c_coin = rng.random(surface_y.shape)
            centers = c_mask & (c_coin < c_prob)
            if centers.any():
                blob = binary_dilation(centers, iterations=2) & c_mask
                sp_noise = _coarse_simplex(rng_base ^ 0xC0FB, H, W,
                                            tile_world_x, tile_world_z,
                                            scale=4.0, step=2)
                sp_noise = (sp_noise + 1.0) * 0.5
                sp_pick = np.floor(sp_noise * 5).astype(int) % 5
                coral_names = [
                    "brain_coral_block", "tube_coral_block",
                    "fire_coral_block", "horn_coral_block", "bubble_coral_block",
                ]
                for i, name in enumerate(coral_names):
                    m = blob & (sp_pick == i)
                    if m.any():
                        surface_blk[m] = name
                stats["feat_coral_px"] = int(blob.sum())

    # ─── Clay lenses: cluster existing clay palette into 5-block patches
    # in mid tier (depth 5-15).  Reinforces sediment-pocket look.
    clay_cfg = feat_cfg.get("clay_lenses", {})
    if clay_cfg.get("enabled", True):
        cl_tiers = (depth >= 3) & (depth < 16)
        cl_mask = is_ocean & cl_tiers
        if cl_mask.any():
            cl_noise = _coarse_simplex(rng_base ^ 0xC1A7, H, W,
                                        tile_world_x, tile_world_z,
                                        scale=10.0, step=2)
            cl_noise = (cl_noise + 1.0) * 0.5
            # Patches where noise > 0.75 (about 25% of mid tier)
            clay_patch = cl_mask & (cl_noise > 0.72)
            if clay_patch.any():
                surface_blk[clay_patch] = "clay"
                stats["feat_clay_lens_px"] = int(clay_patch.sum())
