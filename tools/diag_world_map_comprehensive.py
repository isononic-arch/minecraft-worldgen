"""
diag_world_map_comprehensive.py

Build a single comprehensive top-down satellite-style map of the Vandir world.
Composites biome base + ocean gradient + hillshade + rock/snow/dune/beach/floodplain
overlays + rivers + lakes, with a tile grid and legend.

Output: memory/world_map_s70.png  (default 6250x6250 RGB PNG)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import binary_dilation
from skimage.morphology import skeletonize
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.region_overlay_smoothing import clean_painted_river_mask

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
WORKTREE = Path(r"C:/Users/nicho/minecraft-worldgen/.claude/worktrees/pensive-mirzakhani-3da700")
MASKS = Path(r"C:/Users/nicho/minecraft-worldgen/masks")
PROJECT_ROOT = Path(r"C:/Users/nicho/minecraft-worldgen")

# Make sure we can import from the worktree (canonical biome data lives there)
sys.path.insert(0, str(WORKTREE))

from core.biome_assignment import OVERRIDE_BIOME_MAP            # zone -> biome
from tools.world_biome_map import BIOME_COLORS                  # biome -> RGB

OUT_PATH = WORKTREE / "memory" / "world_map_s70.png"
OUT_SIZE = 6250  # square; downsamples 50k to 1:8

SEA_LEVEL = 17050  # raw 16-bit

# Lithology group id -> display RGB.  Hardcoded (matches
# config/thresholds.json -> lithology.groups[*].id, S69-current).
LITHOLOGY_COLORS = {
    1: (184, 155, 142),  # granitic — pinkish grey
    2: (62, 58, 56),     # arid_basaltic — dark charcoal
    3: (31, 28, 26),     # temperate_basaltic — near-black
    4: (212, 182, 138),  # limestone — pale buff
    5: (94, 98, 104),    # deepslate_metamorphic — blue-grey
    6: (107, 122, 94),   # mossy_temperate — mossy green-grey
}
LITHOLOGY_NAMES = {
    1: "granitic",
    2: "arid_basaltic",
    3: "temperate_basaltic",
    4: "limestone",
    5: "deepslate_metamorphic",
    6: "mossy_temperate",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hex(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def lerp_color(c0, c1, t):
    """Lerp between two colors with t in [0,1]; t can be array, c is 3-tuple."""
    t = np.asarray(t)
    out = np.empty(t.shape + (3,), dtype=np.float32)
    for i in range(3):
        out[..., i] = c0[i] + (c1[i] - c0[i]) * t
    return out


def blend(base_rgb_f32, mask_bool, target_rgb, alpha):
    """In-place: where mask_bool, blend base toward target_rgb at alpha."""
    if not mask_bool.any():
        return
    tr = np.asarray(target_rgb, dtype=np.float32)
    base_rgb_f32[mask_bool] = base_rgb_f32[mask_bool] * (1 - alpha) + tr * alpha


def read_resampled(path: Path, size: int, resampling: Resampling, dtype=None):
    """Open a raster and read it downsampled to (size, size)."""
    with rasterio.open(str(path)) as src:
        arr = src.read(1, out_shape=(size, size), resampling=resampling)
    if dtype is not None and arr.dtype != dtype:
        arr = arr.astype(dtype)
    return arr


def progress(layer_idx, total, name, t0):
    print(f"Layer {layer_idx}/{total}: {name}... done {time.perf_counter() - t0:.1f}s",
          flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    overall_t0 = time.perf_counter()
    HARD_CAP = 180.0  # seconds

    def cap_check(stage):
        elapsed = time.perf_counter() - overall_t0
        if elapsed > HARD_CAP:
            print(f"WARN: hard cap {HARD_CAP}s exceeded at stage '{stage}' "
                  f"({elapsed:.1f}s) — continuing but flagging", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    size = OUT_SIZE
    skipped = []

    print(f"Target output size: {size}x{size}", flush=True)
    print(f"Masks dir: {MASKS}", flush=True)

    # ------------------------------------------------------------------
    # Sanity checks
    # ------------------------------------------------------------------
    import datetime
    override_path = MASKS / "override.tif"
    lith_path = MASKS / "lithology.tif"
    if override_path.exists():
        ovr_mtime = os.path.getmtime(override_path)
        ovr_when = datetime.datetime.fromtimestamp(ovr_mtime)
        # 2026-04-24 16:52 local
        required = datetime.datetime(2026, 4, 24, 16, 52)
        if ovr_when < required:
            print(f"WARN: override.tif mtime {ovr_when} is older than required "
                  f"{required}. The user expected a fresh override.", flush=True)
        else:
            print(f"  override.tif mtime OK: {ovr_when}", flush=True)
    if lith_path.exists():
        with rasterio.open(str(lith_path)) as src:
            if (src.height, src.width) != (6250, 6250):
                print(f"WARN: lithology.tif shape {(src.height, src.width)} "
                      f"!= (6250, 6250)", flush=True)
            if src.dtypes[0] != "uint8":
                print(f"WARN: lithology.tif dtype {src.dtypes[0]} != uint8",
                      flush=True)
        # Verify the 6 group ids in thresholds.json still map 1..6 to expected names.
        try:
            import json as _json
            with open(PROJECT_ROOT / "config" / "thresholds.json") as _f:
                _cfg = _json.load(_f)
            _groups = _cfg.get("lithology", {}).get("groups", {})
            expected = {
                1: "granitic", 2: "arid_basaltic", 3: "temperate_basaltic",
                4: "limestone", 5: "deepslate_metamorphic", 6: "mossy_temperate",
            }
            actual = {body.get("id"): nm for nm, body in _groups.items()
                      if isinstance(body, dict)}
            for gid, exp_name in expected.items():
                got = actual.get(gid)
                if got != exp_name:
                    print(f"WARN: lithology group id {gid}: expected "
                          f"'{exp_name}', got '{got}'", flush=True)
        except Exception as e:
            print(f"WARN: could not verify lithology group ids: {e}", flush=True)
    else:
        print("WARN: lithology.tif missing — lithology overlay will be skipped",
              flush=True)

    # ------------------------------------------------------------------
    # Read masks (downsampled at read time to stay memory-frugal)
    # ------------------------------------------------------------------
    def safe_read(name, resampling, dtype=None):
        p = MASKS / name
        if not p.exists():
            print(f"  SKIP missing mask: {name}", flush=True)
            skipped.append(name)
            return None
        t = time.perf_counter()
        arr = read_resampled(p, size, resampling, dtype=dtype)
        print(f"  read {name} {arr.shape} {arr.dtype}  {time.perf_counter()-t:.1f}s",
              flush=True)
        return arr

    def safe_read_max(name, size, dtype=None):
        """Read at 2x size with nearest, then 2x2 max-pool to `size`.

        This emulates a max-resampling read for thin discrete masks where
        nearest-resampling at 1:8 might drop single-pixel features.
        """
        p = MASKS / name
        if not p.exists():
            print(f"  SKIP missing mask: {name}", flush=True)
            skipped.append(name)
            return None
        t = time.perf_counter()
        big_size = size * 2
        with rasterio.open(str(p)) as src:
            arr = src.read(1, out_shape=(big_size, big_size),
                           resampling=Resampling.nearest)
        if dtype is not None and arr.dtype != dtype:
            arr = arr.astype(dtype)
        # 2x2 max-pool -> size x size
        arr2 = arr.reshape(size, 2, size, 2).max(axis=(1, 3))
        print(f"  read {name} (max-pool 2x) {arr2.shape} {arr2.dtype}  "
              f"{time.perf_counter()-t:.1f}s", flush=True)
        return arr2

    print("\n[Read] downsampling masks to working size...", flush=True)
    height = safe_read("height.tif", Resampling.bilinear, dtype=np.float32)
    if height is None:
        print("FATAL: height.tif required", flush=True)
        return 2
    # S70: Resampling.mode majority-votes the 8x8 source-block per output px,
    # collapsing 1-block boundary jitter on override.tif into the dominant
    # zone. Eliminates "digital camo" appearance at biome boundaries on the
    # map without changing in-game biome dither (which still uses single-
    # block jitter via override.tif's full-res values).
    override = safe_read("override.tif", Resampling.mode, dtype=np.uint8)
    if override is None:
        print("FATAL: override.tif required", flush=True)
        return 2

    # rasterio.Resampling.max isn't valid for reads/writes (only for warp).
    # For small discrete masks (rock_gap, snow_gap, hydro_centerline) we
    # read at 2x output (12500) with nearest and then 2x2-block max pool to
    # preserve thin features. For lake we use nearest (label preservation).
    rock = safe_read_max("rock_gap.tif", size, dtype=np.uint8)
    snow = safe_read_max("snow_gap.tif", size, dtype=np.uint8)
    floodplain = safe_read("hydro_floodplain.tif", Resampling.bilinear, dtype=np.float32)
    sand_dunes = safe_read("sand_dunes.tif", Resampling.bilinear, dtype=np.float32)
    beach = safe_read("beach.tif", Resampling.bilinear, dtype=np.float32)
    centerline = safe_read_max("hydro_centerline.tif", size, dtype=np.uint8)
    lake = safe_read("hydro_lake.tif", Resampling.nearest, dtype=np.uint16)
    # Lithology is already 6250x6250 uint8 — read directly, no resampling.
    lithology = None
    if lith_path.exists():
        t_lith = time.perf_counter()
        with rasterio.open(str(lith_path)) as _src:
            lithology = _src.read(1)
        if lithology.shape != (size, size):
            print(f"  WARN: lithology shape {lithology.shape} != ({size},{size}); "
                  f"will be resampled with nearest", flush=True)
            with rasterio.open(str(lith_path)) as _src:
                lithology = _src.read(1, out_shape=(size, size),
                                      resampling=Resampling.nearest)
        if lithology.dtype != np.uint8:
            lithology = lithology.astype(np.uint8)
        print(f"  read lithology.tif {lithology.shape} {lithology.dtype}  "
              f"{time.perf_counter()-t_lith:.1f}s", flush=True)
        _u_lith = np.unique(lithology)
        unexpected = [int(v) for v in _u_lith if int(v) not in (0, 1, 2, 3, 4, 5, 6)]
        if unexpected:
            print(f"  WARN: lithology has unexpected ids: {unexpected}",
                  flush=True)

    cap_check("after read")

    H, W = height.shape
    assert (H, W) == (size, size), f"Unexpected size {height.shape}"

    # Working float32 RGB canvas
    out = np.zeros((H, W, 3), dtype=np.float32)

    total_layers = 11

    # ------------------------------------------------------------------
    # Layer 1: Ocean base (where height_raw < SEA_LEVEL)
    # ------------------------------------------------------------------
    t = time.perf_counter()
    ocean_mask = height < SEA_LEVEL
    depth_norm = np.clip((SEA_LEVEL - height) / float(SEA_LEVEL), 0.0, 1.0)
    shallow = np.array(_hex("#5E8FB8"), dtype=np.float32)
    deep    = np.array(_hex("#0A1F3A"), dtype=np.float32)
    # depth_norm=0 -> shallow (just below sea), depth_norm=1 -> deep
    ocean_rgb = lerp_color(shallow, deep, depth_norm)
    out[ocean_mask] = ocean_rgb[ocean_mask]
    progress(1, total_layers, "ocean base", t)

    # ------------------------------------------------------------------
    # Layer 2: Biome base (land — height >= SEA_LEVEL OR override is non-zero
    #          land code).  We treat all land using BIOME_COLORS lookup.
    # ------------------------------------------------------------------
    t = time.perf_counter()
    # Build a 256-entry LUT zone-code -> RGB (default grey fallback).
    default_rgb = np.array(BIOME_COLORS["default"], dtype=np.uint8)
    biome_lut = np.tile(default_rgb, (256, 1)).astype(np.uint8)
    for code, name in OVERRIDE_BIOME_MAP.items():
        if not name:
            continue
        rgb = BIOME_COLORS.get(name, BIOME_COLORS["default"])
        biome_lut[code] = rgb
    biome_rgb = biome_lut[override]  # (H,W,3) uint8

    land_mask = ~ocean_mask
    out[land_mask] = biome_rgb[land_mask].astype(np.float32)
    progress(2, total_layers, "biome base", t)
    cap_check("after biome base")

    # ------------------------------------------------------------------
    # Layer 3: Hillshade (multiply by lerp(0.55, 1.0, shade))
    # Standard hillshade with az=315°, alt=45°.
    # raw is 16-bit; treat as ~0.015m/unit vertical, but per-spec we use raw
    # units directly and instead scale dz/dx by a 1/8 factor to compensate
    # for 8m/px horizontal at 6250 working scale (each output px ≈ 8m).
    # ------------------------------------------------------------------
    t = time.perf_counter()
    z_factor = 1.0 / 8.0
    # np.gradient returns (dz/drow, dz/dcol) -> (dz/dy, dz/dx)
    dzdy, dzdx = np.gradient(height.astype(np.float32))
    dzdx *= z_factor
    dzdy *= z_factor

    slope_rad = np.arctan(np.hypot(dzdx, dzdy))
    # aspect: standard (atan2(dz/dy, -dz/dx) -> compass)
    aspect_rad = np.arctan2(dzdy, -dzdx)
    aspect_rad = np.where(aspect_rad < 0, aspect_rad + 2 * np.pi, aspect_rad)

    az_deg, alt_deg = 315.0, 45.0
    az_rad = np.deg2rad(360.0 - az_deg + 90.0)  # math angle convention
    az_rad = np.deg2rad(az_deg)                 # use compass directly below
    zenith_rad = np.deg2rad(90.0 - alt_deg)

    shade = (np.cos(zenith_rad) * np.cos(slope_rad) +
             np.sin(zenith_rad) * np.sin(slope_rad) *
             np.cos(az_rad - aspect_rad))
    shade = np.clip(shade, 0.0, 1.0)
    factor = 0.55 + (1.0 - 0.55) * shade  # in [0.55, 1.0]
    out *= factor[..., None]
    np.clip(out, 0, 255, out=out)
    progress(3, total_layers, "hillshade", t)
    cap_check("after hillshade")

    # Free intermediates
    del dzdx, dzdy, slope_rad, aspect_rad, shade, factor, biome_rgb, ocean_rgb

    # ------------------------------------------------------------------
    # Layers 4-8: feature blends
    # ------------------------------------------------------------------
    t = time.perf_counter()
    if beach is not None:
        m = beach > 0.5
        blend(out, m & land_mask, _hex("#E8D9A8"), 0.6)
    progress(4, total_layers, "beaches", t)

    t = time.perf_counter()
    if sand_dunes is not None:
        m = sand_dunes > 0.5
        blend(out, m & land_mask, _hex("#D4A865"), 0.7)
    progress(5, total_layers, "sand dunes", t)

    # Floodplain — riparian-meadow yellow-green, distinct from forest.
    t = time.perf_counter()
    floodplain_highlight_pixels = 0
    if floodplain is not None:
        m = floodplain > 0.001
        m = m & land_mask
        floodplain_highlight_pixels = int(m.sum())
        blend(out, m, _hex("#B8C870"), 0.55)
    print(f"  floodplain highlight pixels: {floodplain_highlight_pixels}",
          flush=True)
    progress(6, total_layers, "floodplain", t)

    # Rock exposure — colored by lithology group.
    t = time.perf_counter()
    rock_per_lith = {gid: 0 for gid in LITHOLOGY_COLORS}
    rock_fallback_px = 0
    if rock is not None:
        rock_mask = (rock == 1) & land_mask
        if lithology is not None:
            for gid, color in LITHOLOGY_COLORS.items():
                m_g = rock_mask & (lithology == gid)
                cnt = int(m_g.sum())
                rock_per_lith[gid] = cnt
                if cnt > 0:
                    blend(out, m_g, color, 0.80)
            # Defensive: rock_gap==1 with no lithology id (id == 0)
            m_fb = rock_mask & (lithology == 0)
            rock_fallback_px = int(m_fb.sum())
            if rock_fallback_px > 0:
                blend(out, m_fb, _hex("#6B6B6B"), 0.55)
        else:
            # No lithology mask available — fall back to flat grey.
            rock_fallback_px = int(rock_mask.sum())
            blend(out, rock_mask, _hex("#6B6B6B"), 0.55)
    for gid, cnt in rock_per_lith.items():
        print(f"  rock pixels tinted by lithology[{gid}={LITHOLOGY_NAMES[gid]}]:"
              f" {cnt}", flush=True)
    print(f"  rock pixels fallback (no lithology id): {rock_fallback_px}",
          flush=True)
    progress(7, total_layers, "rock exposure", t)

    # Snow peaks — geological hint THEN white snow.
    t = time.perf_counter()
    if snow is not None:
        snow_mask = (snow == 1) & land_mask
        if lithology is not None and snow_mask.any():
            # First: blend toward lithology color at alpha=0.25 for geology hint.
            for gid, color in LITHOLOGY_COLORS.items():
                m_g = snow_mask & (lithology == gid)
                if m_g.any():
                    blend(out, m_g, color, 0.25)
        # Then: dominant white snow blend.
        blend(out, snow_mask, _hex("#FFFFFF"), 0.80)
    progress(8, total_layers, "snow peaks", t)
    cap_check("after surface overlays")

    # ------------------------------------------------------------------
    # Layer 9a: Painted river additions from masks/hydro_region.png
    # User-painted rivers (8192x8192, value==2 = "river"). Skeletonized
    # then resized to 6250 so the brush dabs collapse to 1-px centerlines
    # and merge cleanly with the precomputed Strahler centerlines.
    # ------------------------------------------------------------------
    t = time.perf_counter()
    hr_path = MASKS / "hydro_region.png"
    painted_river_6250 = np.zeros((H, W), dtype=bool)
    skel_8k_count = 0
    if os.environ.get("MAP_NO_PAINTED_RIVERS") == "1":
        print("[painted rivers] skipped via MAP_NO_PAINTED_RIVERS=1", flush=True)
    elif hr_path.exists():
        hr_8k = np.asarray(Image.open(hr_path).convert("L"), dtype=np.uint8)
        # value == 2 means "river" per hydro_region schema (4 categories:
        # lake, river, river-bank-moist, dry-channel).
        painted_8k = (hr_8k == 2)
        if painted_8k.any():
            # S70: clean_painted_river_mask runs morphological opening
            # + iterative endpoint pruning to eliminate four-leaf-clover
            # artifacts from wide brush strokes. Identical defaults as
            # core/hydro_region_overlay.py so the map and in-world
            # rendering stay in sync.
            skel_8k = clean_painted_river_mask(painted_8k,
                                                 opening_radius=2,
                                                 prune_max_branch_len=8)
            skel_8k_count = int(skel_8k.sum())
            # Dilate to 2px at 8192, then NEAREST to 6250, so thin
            # features survive the downscale.
            thick_8k = binary_dilation(skel_8k, iterations=1)
            painted_river_mask = np.asarray(
                Image.fromarray((thick_8k * 255).astype(np.uint8)).resize(
                    (W, H), Image.NEAREST)
            ) > 127
            # Skeletonize again at 6250 -> clean 1px centerline at output scale.
            painted_river_6250 = skeletonize(painted_river_mask)
    print(f"[painted rivers] {skel_8k_count} 8k skel px, "
          f"{int(painted_river_6250.sum())} at 6250", flush=True)
    progress(9, total_layers, "painted river prep (Layer 9a)", t)

    # ------------------------------------------------------------------
    # Layer 9: Rivers — Strahler order 1..5 with halo + core
    # ------------------------------------------------------------------
    t = time.perf_counter()
    river_halo_pixels = 0
    braid_body_pixels = 0
    wadi_pixels = 0
    # MAP_IN_GAME=1 renders rivers at their true carved-footprint width.
    # No halo, no display-dilation on thin Strahler centerlines.
    in_game_mode = os.environ.get("MAP_IN_GAME") == "1"
    if centerline is not None:
        # Per rebuild_centerline.py encoding:
        #   1..5  = Strahler order on NMS centerline (thin)
        #   128   = wadi / dry channel (distinct — paint as tan)
        #   255   = braid fill / solid water body (paint as fat river)
        halo_color = _hex("#5A8FBE")
        core_color_arr = np.array(_hex("#1F4A78"), dtype=np.float32)

        # --- 1. Widened river body (braid fill, value == 255) --------
        braid_mask = (centerline == 255)
        if braid_mask.any():
            if not in_game_mode:
                # Map mode: 1-px halo for edge readability.
                braid_halo = binary_dilation(braid_mask, iterations=1) & ~braid_mask
                blend(out, braid_halo, halo_color, 0.75)
            out[braid_mask] = core_color_arr
            braid_body_pixels = int(braid_mask.sum())

        # --- 2. Wadi / dry channel (value == 128) -------------------
        wadi_mask = (centerline == 128)
        if wadi_mask.any():
            wadi_color = _hex("#8B6F47")
            if not in_game_mode:
                wadi_halo = binary_dilation(wadi_mask, iterations=1) & ~wadi_mask
                blend(out, wadi_halo, wadi_color, 0.45)
            blend(out, wadi_mask, wadi_color, 0.75)
            wadi_pixels = int(wadi_mask.sum())

        # --- 3. Strahler NMS centerlines (values 1..5) --------------
        all_halo_only = np.zeros_like(centerline, dtype=bool)
        if in_game_mode:
            # Simulate river_carver_v2's distance-transform widening.
            # hydro_width.tif (50k, uint8) carries the carver's target
            # half-width per pixel in blocks. At 6250 scale each map
            # pixel = 8 blocks, so widen river by hydro_width/16 map
            # pixels (radius). Read + max-pool on demand here so the
            # carve-sim is opt-in via MAP_IN_GAME.
            from scipy.ndimage import distance_transform_edt
            river_mask = (centerline >= 1) | (centerline == 255)
            if river_mask.any():
                hw_path = MASKS / "hydro_width.tif"
                with rasterio.open(hw_path) as _hw_src:
                    hw_50k = _hw_src.read(1, out_shape=(H * 2, W * 2),
                                           resampling=Resampling.nearest)
                # 2x2 max-pool to (H, W) so max-width pixel in each
                # 8-block real window wins — matches centerline pool.
                hw_6250 = hw_50k.reshape(H, 2, W, 2).max(axis=(1, 3))
                # Distance-transform from every river pixel; each
                # non-river pixel inherits the width of the nearest
                # river pixel via return_indices.
                dist_map, (iy, ix) = distance_transform_edt(
                    ~river_mask, return_indices=True)
                nearest_hw = hw_6250[iy, ix]  # width in blocks
                # river extent: dist (map px) <= width/2 (blocks) / 8 (blocks per map px)
                in_river = dist_map <= (nearest_hw.astype(np.float32) / 16.0)
                in_river &= ~river_mask  # just the added carve zone
                # Paint the added-carve zone as the river body, keep
                # original braid/NMS pixels already painted above.
                out[in_river] = core_color_arr
                print(f"  carve-sim added pixels: {int(in_river.sum()):,}"
                      f" (max hw={int(hw_6250.max())}, mean@river={hw_6250[river_mask].mean():.1f})",
                      flush=True)
            nms_mask = (centerline >= 1) & (centerline <= 5)
            if nms_mask.any():
                out[nms_mask] = core_color_arr
        else:
            core_widths = {1: 2, 2: 2, 3: 3, 4: 4, 5: 5}
            for order in sorted(core_widths.keys()):
                core_px = core_widths[order]
                halo_px = core_px + 1
                mask_order = (centerline == order)
                if not mask_order.any():
                    continue
                halo = binary_dilation(mask_order, iterations=halo_px - 1) \
                    if halo_px > 1 else mask_order
                core = binary_dilation(mask_order, iterations=core_px - 1) \
                    if core_px > 1 else mask_order
                halo_only = halo & ~core
                blend(out, halo_only, halo_color, 0.75)
                out[core] = core_color_arr
                all_halo_only |= halo_only
        river_halo_pixels = int(all_halo_only.sum())
    print(f"  river halo pixels: {river_halo_pixels}  "
          f"braid body: {braid_body_pixels}  wadi: {wadi_pixels}",
          flush=True)

    # Render painted rivers AFTER precomputed ones — Strahler-3 style
    # (3px core + 1px halo). Where painted overlaps precomputed the
    # painted version wins; visually identical so this is fine.
    painted_halo_pixels = 0
    painted_core_pixels = 0
    if painted_river_6250.any():
        halo_color = _hex("#5A8FBE")
        core_color_arr = np.array(_hex("#1F4A78"), dtype=np.float32)
        # Strahler-3 widths: core_px=3, halo_px=4.
        halo = binary_dilation(painted_river_6250, iterations=3)
        core = binary_dilation(painted_river_6250, iterations=2)
        halo_only = halo & ~core
        blend(out, halo_only, halo_color, 0.75)
        out[core] = core_color_arr
        painted_halo_pixels = int(halo_only.sum())
        painted_core_pixels = int(core.sum())
    print(f"  painted river halo pixels: {painted_halo_pixels}, "
          f"core pixels: {painted_core_pixels}", flush=True)
    progress(9, total_layers, "rivers", t)

    # ------------------------------------------------------------------
    # Layer 10: Lakes
    # ------------------------------------------------------------------
    t = time.perf_counter()
    if lake is not None:
        m = lake > 0
        blend(out, m, _hex("#3877B6"), 0.95)
    progress(10, total_layers, "lakes", t)
    cap_check("after rivers/lakes")

    # ------------------------------------------------------------------
    # Final clip and convert
    # ------------------------------------------------------------------
    np.clip(out, 0, 255, out=out)
    rgb_u8 = out.astype(np.uint8)
    del out

    # Free big arrays we no longer need (override is still useful — no, keep it minimal)
    del height, override
    if rock is not None: del rock
    if snow is not None: del snow
    if floodplain is not None: del floodplain
    if sand_dunes is not None: del sand_dunes
    if beach is not None: del beach
    if centerline is not None: del centerline
    if lake is not None: del lake
    if lithology is not None: del lithology

    img = Image.fromarray(rgb_u8, mode="RGB").convert("RGBA")
    del rgb_u8

    # ------------------------------------------------------------------
    # Layer 11: Tile grid + labels + legend (PIL drawing)
    # ------------------------------------------------------------------
    t = time.perf_counter()
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 97 tiles, 98 grid lines
    grid_positions = np.linspace(0, size, 98)

    line_color = (0, 0, 0, int(255 * 0.15))
    for x in grid_positions:
        xi = int(round(x))
        if 0 <= xi < size:
            draw.line([(xi, 0), (xi, size - 1)], fill=line_color, width=1)
    for y in grid_positions:
        yi = int(round(y))
        if 0 <= yi < size:
            draw.line([(0, yi), (size - 1, yi)], fill=line_color, width=1)

    # Labels every 4 tiles
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    label_color = (40, 40, 40, int(255 * 0.35))
    for tx in range(0, 97, 4):
        for tz in range(0, 97, 4):
            px = int(round(grid_positions[tx])) + 2
            py = int(round(grid_positions[tz])) + 1
            draw.text((px, py), f"{tx},{tz}", fill=label_color, font=font)

    progress(11, total_layers, "grid + labels", t)
    cap_check("after grid")

    # ------------------------------------------------------------------
    # Legend inset
    # ------------------------------------------------------------------
    t = time.perf_counter()
    legend_w, legend_h = 460, 800
    margin = 16
    lx = size - legend_w - margin
    ly = size - legend_h - margin

    bg = (245, 245, 240, int(255 * 0.85))
    draw.rectangle([(lx, ly), (lx + legend_w, ly + legend_h)],
                   fill=bg, outline=(60, 60, 60, 200), width=2)

    # Title
    title = "Vandir World Map — S70"
    draw.text((lx + 14, ly + 10), title, fill=(20, 20, 20, 255), font=font)

    # Two columns
    col1_x = lx + 14
    col2_x = lx + legend_w // 2 + 8
    row_y = ly + 36
    swatch_size = 12
    line_h = 16

    # Column 1: biomes grouped by ecological family
    families = [
        ("Cold / Boreal", [
            "SNOWY_BOREAL_TAIGA", "BOREAL_TAIGA", "BOREAL_ALPINE",
            "ARCTIC_TUNDRA", "FROZEN_FLATS",
        ]),
        ("Temperate forest", [
            "TEMPERATE_DECIDUOUS", "MIXED_FOREST", "BIRCH_FOREST",
            "EASTERN_TEMPERATE_COAST",
        ]),
        ("Rainforest", [
            "TEMPERATE_RAINFOREST", "RAINFOREST_COAST",
            "LUSH_RAINFOREST_COAST", "TIDAL_JUNGLE_FRINGE",
        ]),
        ("Wetland / coast", [
            "MANGROVE_COAST", "FRESHWATER_FEN", "RIPARIAN_WOODLAND",
            "COASTAL_HEATH",
        ]),
        ("Heath / shrub", [
            "SCRUBBY_HEATHLAND", "DRY_WOODLAND_MAQUIS",
        ]),
        ("Savanna / steppe", [
            "DRY_OAK_SAVANNA", "CONTINENTAL_STEPPE", "DRY_PINE_BARRENS",
        ]),
        ("Desert / arid", [
            "SAND_DUNE_DESERT", "DESERT_STEPPE_TRANSITION",
            "SEMI_ARID_SHRUBLAND",
        ]),
        ("Karst", [
            "KARST_BARRENS",
        ]),
    ]

    cy = row_y
    for family_name, biomes in families:
        draw.text((col1_x, cy), family_name, fill=(30, 30, 30, 255), font=font)
        cy += line_h
        for b in biomes:
            rgb = BIOME_COLORS.get(b, BIOME_COLORS["default"])
            draw.rectangle([(col1_x + 4, cy + 1),
                            (col1_x + 4 + swatch_size, cy + 1 + swatch_size)],
                           fill=(rgb[0], rgb[1], rgb[2], 255),
                           outline=(40, 40, 40, 220))
            label = b.replace("_", " ").title()
            draw.text((col1_x + 4 + swatch_size + 6, cy),
                      label, fill=(40, 40, 40, 255), font=font)
            cy += line_h
        cy += 2  # small gap

    # Column 2: physical overlays + lithology
    overlays = [
        ("Ocean shallow",                       _hex("#5E8FB8")),
        ("Ocean deep",                          _hex("#0A1F3A")),
        ("Beach",                               _hex("#E8D9A8")),
        ("Sand dune",                           _hex("#D4A865")),
        ("Floodplain clearing",                 _hex("#B8C870")),
        ("Rock exposure (colored by lithology)", _hex("#6B6B6B")),
        ("Snow peak (tinted by lithology)",     _hex("#FFFFFF")),
        ("River core order 1 (2px)",            _hex("#1F4A78")),
        ("River core order 2 (2px)",            _hex("#1F4A78")),
        ("River core order 3 (3px)",            _hex("#1F4A78")),
        ("River core order 4 (4px)",            _hex("#1F4A78")),
        ("River core order 5 (5px)",            _hex("#1F4A78")),
        ("River halo (Strahler 1-5)",           _hex("#5A8FBE")),
        ("Painted river additions (user)",      _hex("#1F4A78")),
        ("Lake",                                _hex("#3877B6")),
    ]

    cy = row_y
    draw.text((col2_x, cy), "Physical overlays", fill=(30, 30, 30, 255), font=font)
    cy += line_h
    for label, rgb in overlays:
        draw.rectangle([(col2_x + 4, cy + 1),
                        (col2_x + 4 + swatch_size, cy + 1 + swatch_size)],
                       fill=(rgb[0], rgb[1], rgb[2], 255),
                       outline=(40, 40, 40, 220))
        draw.text((col2_x + 4 + swatch_size + 6, cy),
                  label, fill=(40, 40, 40, 255), font=font)
        cy += line_h

    # Lithology group swatches.
    cy += 4
    draw.text((col2_x, cy), "Lithology groups", fill=(30, 30, 30, 255), font=font)
    cy += line_h
    for gid in (1, 2, 3, 4, 5, 6):
        rgb = LITHOLOGY_COLORS[gid]
        label = LITHOLOGY_NAMES[gid].replace("_", " ")
        draw.rectangle([(col2_x + 4, cy + 1),
                        (col2_x + 4 + swatch_size, cy + 1 + swatch_size)],
                       fill=(rgb[0], rgb[1], rgb[2], 255),
                       outline=(40, 40, 40, 220))
        draw.text((col2_x + 4 + swatch_size + 6, cy),
                  label, fill=(40, 40, 40, 255), font=font)
        cy += line_h

    # Composite overlay onto map
    img = Image.alpha_composite(img, overlay)
    img = img.convert("RGB")
    progress(11, total_layers, "legend", t)
    cap_check("after legend")

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------
    t = time.perf_counter()
    img.save(str(OUT_PATH), format="PNG", optimize=False)
    save_dt = time.perf_counter() - t
    file_size_mb = OUT_PATH.stat().st_size / (1024 * 1024)

    total_dt = time.perf_counter() - overall_t0
    print("", flush=True)
    print(f"Saved: {OUT_PATH}", flush=True)
    print(f"  size  : {file_size_mb:.2f} MB", flush=True)
    print(f"  save  : {save_dt:.1f}s", flush=True)
    print(f"  total : {total_dt:.1f}s", flush=True)
    if skipped:
        print(f"  skipped layers (missing mask): {skipped}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
