"""
preview_renderer.py — Step 11: 2D Preview Renderer
Vandir World Generation Pipeline — /core/preview_renderer.py

Produces a top-down RGBA surface-color preview image from mask tiles.
Works without amulet — reads mask TIFFs directly via rasterio Window().

Two modes:
  1. Full render  — render the entire world (or a region) to a PNG file
  2. Tile render  — render a single tile, return (H, W, 4) RGBA uint8 array
                    (used by Panel 3 for live tile updates during pipeline run)

Render layers (composited in order, all optional):
  - Ocean fill       (deep blue for sub-sea pixels)
  - Surface color    (biome false-color OR block approximate color)
  - Height shading   (hillshade from height tile)
  - River overlay    (blue tint over river pixels)
  - Shore overlay    (pale tint over coastal pixels)

No GUI imports. PIL/Pillow used for PNG output only.
rasterio used for mask reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# BIOME FALSE-COLORS  (matches Panel 4 palette exactly for visual consistency)
# ---------------------------------------------------------------------------

BIOME_COLORS: dict[str, tuple[int, int, int]] = {
    "COASTAL_HEATH":           (0x9A, 0xBC, 0x8A),
    "TEMPERATE_RAINFOREST":    (0x1A, 0x6B, 0x3A),
    "BOREAL_TAIGA":            (0x2A, 0x8A, 0x5A),
    "SNOWY_BOREAL_TAIGA":      (0xA0, 0xC8, 0xB8),
    "ALPINE_MEADOW":           (0x8A, 0xC8, 0x7A),
    "ARCTIC_TUNDRA":           (0xC8, 0xD8, 0xD0),
    "FROZEN_FLATS":            (0xE8, 0xF0, 0xF8),
    "TEMPERATE_DECIDUOUS":     (0x4A, 0x9A, 0x3A),
    "RAINFOREST_COAST":        (0x2A, 0x7A, 0x5A),
    "RIPARIAN_WOODLAND":       (0x3A, 0x6B, 0x5A),
    "DRY_OAK_SAVANNA":         (160, 140,  60),
    "KARST_BARRENS":           (0xC8, 0xC0, 0xA8),
    "BIRCH_FOREST":            (0x7A, 0xBA, 0x5A),
    "EASTERN_TEMPERATE_COAST": (0x7A, 0xB8, 0xC8),
    "MIXED_FOREST":            (0x5A, 0x8A, 0x4A),
    "CONTINENTAL_STEPPE":      (180, 170, 110),
    "DRY_PINE_BARRENS":        (0xB8, 0x7A, 0x3A),
    "SCRUBBY_HEATHLAND":       (0x7A, 0x5C, 0x8A),
    "LUSH_RAINFOREST_COAST":   (0x00, 0x6B, 0x2A),
    "SAND_DUNE_DESERT":        (237, 201,  88),
    "DESERT_STEPPE_TRANSITION":(0xD4, 0x82, 0x4A),
    "SEMI_ARID_SHRUBLAND":     (0xC8, 0xC0, 0x60),
    "DRY_WOODLAND_MAQUIS":     (0xB8, 0x90, 0x50),
    "TIDAL_JUNGLE_FRINGE":     (0x00, 0xA8, 0x6B),
    "MANGROVE_COAST":          (0x00, 0x4D, 0x33),
    "FRESHWATER_FEN":          (0x3A, 0x6B, 0x5A),
    "_OCEAN":                  (0x1A, 0x3A, 0x6B),
    "_DEFAULT":                (0x60, 0x60, 0x60),
}

# Approximate surface block colors for block-color mode
BLOCK_COLORS: dict[str, tuple[int, int, int]] = {
    "grass_block":       (0x59, 0x9B, 0x3A),
    "podzol":            (0x67, 0x40, 0x1E),
    "coarse_dirt":       (0x72, 0x4D, 0x2D),
    "dirt":              (0x8B, 0x60, 0x3A),
    "sand":              (0xE3, 0xD6, 0x98),
    "sandstone":         (0xD9, 0xC9, 0x82),
    "smooth_sandstone":  (0xD5, 0xC6, 0x7C),
    "gravel":            (0x99, 0x96, 0x90),
    "stone":             (0x8A, 0x8A, 0x8A),
    "cobblestone":       (0x7D, 0x7D, 0x7D),
    "andesite":          (0x8C, 0x8C, 0x8E),
    "diorite":           (0xC4, 0xC2, 0xC0),
    "granite":           (0xAA, 0x7A, 0x66),
    "calcite":           (0xE0, 0xDE, 0xD8),
    "tuff":              (0x82, 0x8C, 0x58),   # olive-green — distinct from stone grey
    "deepslate":         (0x4A, 0x4A, 0x52),
    "mud":               (0x54, 0x44, 0x32),
    "clay":              (0x9E, 0xA3, 0xAA),
    "moss_block":        (0x52, 0x72, 0x3A),
    "mossy_cobblestone": (0x60, 0x78, 0x52),
    "snow_block":        (0xF0, 0xF4, 0xF8),
    "ice":               (0xC0, 0xDC, 0xF8),
    "packed_ice":        (0xC8, 0xE4, 0xFF),
    "water":             (0x2E, 0x6E, 0xB8),
    "dripstone_block":   (0x9A, 0x84, 0x6C),
    "bedrock":           (0x3A, 0x3A, 0x3A),
    "air":               (0x1A, 0x3A, 0x6B),   # ocean/void = deep blue
}

OCEAN_COLOR   = np.array([0x1A, 0x3A, 0x6B], dtype=np.uint8)
DEFAULT_COLOR = np.array([0x60, 0x60, 0x60], dtype=np.uint8)

# Y values used for hillshade — sun from NW at 45°
_HILLSHADE_AZIMUTH = 315.0   # degrees
_HILLSHADE_ALTITUDE = 45.0   # degrees


# ---------------------------------------------------------------------------
# CORE: render one tile to RGBA array
# ---------------------------------------------------------------------------

def render_tile(
    biome_grid:   np.ndarray,          # (H, W) str
    surface_y:    np.ndarray,          # (H, W) int16
    height_tile:  np.ndarray,          # (H, W) float32 [0,1]
    flow_tile:    np.ndarray,          # (H, W) float32 [0,1]
    shore_tile:   np.ndarray,          # (H, W) float32 [0,1]  (1 = shore)
    surface_blk:  Optional[np.ndarray] = None,   # (H, W) str — if None, use biome colors
    mode:         str = "biome",       # "biome" | "block" | "height"
    hillshade:    bool = True,
    river_overlay: bool = True,
    shore_overlay: bool = True,
    sea_level_y:  int = 63,
) -> np.ndarray:
    """
    Render one tile to a (H, W, 4) RGBA uint8 array.

    mode:
      "biome"  — biome false-color (matches Panel 4 palette)
      "block"  — approximate surface block colors
      "height" — greyscale heightmap
    """
    H, W = biome_grid.shape
    rgba = np.zeros((H, W, 4), dtype=np.uint8)
    rgba[:, :, 3] = 255  # fully opaque

    ocean_mask = surface_y < sea_level_y  # S55: strict; Y=63 is sea-level LAND, not ocean

    if mode == "height":
        # Greyscale: normalize surface_y to [0,255]
        norm = np.clip((surface_y.astype(np.float32) - (-64)) / (448 - (-64)), 0, 1)
        grey = (norm * 255).astype(np.uint8)
        rgba[:, :, 0] = grey
        rgba[:, :, 1] = grey
        rgba[:, :, 2] = grey

    elif mode == "block" and surface_blk is not None:
        # Per-pixel block color lookup
        for r in range(H):
            for c in range(W):
                blk = surface_blk[r, c]
                col = BLOCK_COLORS.get(str(blk), (0x60, 0x60, 0x60))
                rgba[r, c, 0] = col[0]
                rgba[r, c, 1] = col[1]
                rgba[r, c, 2] = col[2]
        # Ocean override
        rgba[ocean_mask, 0] = OCEAN_COLOR[0]
        rgba[ocean_mask, 1] = OCEAN_COLOR[1]
        rgba[ocean_mask, 2] = OCEAN_COLOR[2]

    else:  # "biome" (default)
        # Vectorised biome color lookup
        rgb = np.empty((H, W, 3), dtype=np.uint8)
        for biome_name in np.unique(biome_grid):
            bstr = str(biome_name)
            col  = BIOME_COLORS.get(bstr, BIOME_COLORS["_DEFAULT"])
            mask = biome_grid == biome_name
            rgb[mask, 0] = col[0]
            rgb[mask, 1] = col[1]
            rgb[mask, 2] = col[2]
        # Ocean override
        rgb[ocean_mask, 0] = OCEAN_COLOR[0]
        rgb[ocean_mask, 1] = OCEAN_COLOR[1]
        rgb[ocean_mask, 2] = OCEAN_COLOR[2]
        rgba[:, :, :3] = rgb

    # --- Hillshade ---
    if hillshade and mode != "height":
        # Normalize surface_y to [0,1] before hillshading.
        # _compute_hillshade expects [0,1] input; raw MC Y in [-64,448]
        # causes gradient magnitudes of 200-4000 which clips the shading.
        sy_norm = np.clip(
            (surface_y.astype(np.float32) - (-64)) / (448 - (-64)), 0.0, 1.0
        )
        hs = _compute_hillshade(sy_norm)
        # Blend: darken shadows, keep highlights
        factor = 0.5 + 0.5 * hs[:, :, None]   # [0.5, 1.0] multiplier
        rgba_f  = rgba[:, :, :3].astype(np.float32) * factor
        rgba[:, :, :3] = np.clip(rgba_f, 0, 255).astype(np.uint8)

    # --- River overlay ---
    if river_overlay and mode != "height":
        river_mask = flow_tile > 0.65
        if np.any(river_mask):
            alpha = 0.55
            river_col = np.array([0x3A, 0x8A, 0xD8], dtype=np.float32)
            orig = rgba[river_mask, :3].astype(np.float32)
            rgba[river_mask, :3] = np.clip(
                orig * (1 - alpha) + river_col * alpha, 0, 255
            ).astype(np.uint8)

    # --- Shore overlay ---
    if shore_overlay and mode != "height":
        shore_mask = shore_tile > 0.5
        if np.any(shore_mask):
            alpha = 0.25
            shore_col = np.array([0xD8, 0xE8, 0xF8], dtype=np.float32)
            orig = rgba[shore_mask, :3].astype(np.float32)
            rgba[shore_mask, :3] = np.clip(
                orig * (1 - alpha) + shore_col * alpha, 0, 255
            ).astype(np.uint8)

    return rgba


def _compute_hillshade(height_f32: np.ndarray) -> np.ndarray:
    """
    Compute a [0,1] hillshade from a float32 height array.
    Uses simple Sobel gradient → dot with sun vector.
    """
    import math
    az_rad  = math.radians(_HILLSHADE_AZIMUTH)
    alt_rad = math.radians(_HILLSHADE_ALTITUDE)

    # Sobel gradients (simple finite difference)
    pad = np.pad(height_f32, 1, mode="edge")
    gx  = (pad[1:-1, 2:] - pad[1:-1, :-2]) * 0.5
    gy  = (pad[2:, 1:-1] - pad[:-2, 1:-1]) * 0.5

    # Exaggerate relief
    gx *= 200.0
    gy *= 200.0

    # Normal vector
    norm_len = np.sqrt(gx**2 + gy**2 + 1.0)
    nx = -gx / norm_len
    ny = -gy / norm_len
    nz = 1.0  / norm_len

    # Sun direction
    sx = math.cos(alt_rad) * math.cos(az_rad)
    sy = math.cos(alt_rad) * math.sin(az_rad)
    sz = math.sin(alt_rad)

    hs = nx * sx + ny * sy + nz * sz
    return np.clip(hs, 0, 1).astype(np.float32)


# ---------------------------------------------------------------------------
# FULL WORLD RENDER  (reads masks tile by tile — no full raster load)
# ---------------------------------------------------------------------------

def render_world(
    masks_dir:    Path,
    config_path:  Path,
    output_png:   Path,
    mode:         str = "biome",
    scale:        int = 1,              # downsample factor (1=full, 4=12500px output etc)
    tile_x0:      int = 0,
    tile_x1:      Optional[int] = None,
    tile_z0:      int = 0,
    tile_z1:      Optional[int] = None,
    progress_cb   = None,               # callable(done, total) or None
) -> None:
    """
    Render the full world (or a region) to a PNG file.
    Reads mask tiles using rasterio Window() — never loads full rasters.

    output_png: path to write the PNG (will be created/overwritten)
    scale:      1 = full resolution (50000×50000 px), 4 = 12500×12500, etc.
    progress_cb: called with (tiles_done, total_tiles) after each tile
    """
    import json
    import importlib

    try:
        import rasterio
        from rasterio.windows import Window
    except ImportError:
        raise RuntimeError("rasterio is required for render_world()")

    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow is required for render_world() PNG output")

    with open(config_path) as f:
        cfg = json.load(f)

    # Lazy imports from core (avoids hard dependency at module import time)
    core_biome   = importlib.import_module("core.biome_assignment")
    core_col     = importlib.import_module("core.column_generator")
    core_noise   = importlib.import_module("core.noise_fields")

    WORLD_SIZE = 50_000
    TILE_SIZE  = 512

    tx0 = tile_x0
    tx1 = tile_x1 if tile_x1 is not None else WORLD_SIZE // TILE_SIZE
    tz0 = tile_z0
    tz1 = tile_z1 if tile_z1 is not None else WORLD_SIZE // TILE_SIZE

    out_w = ((tx1 - tx0) * TILE_SIZE) // scale
    out_h = ((tz1 - tz0) * TILE_SIZE) // scale

    canvas = np.zeros((out_h, out_w, 4), dtype=np.uint8)

    masks_dir = Path(masks_dir)
    noise = core_noise.load_noise_generators(config_path)

    mask_files = {
        "height":   masks_dir / "height.tif",
        "slope":    masks_dir / "slope.tif",
        "flow":     masks_dir / "flow.tif",
        "erosion":  masks_dir / "erosion.tif",
        "override": masks_dir / "override.tif",
        "shore":    masks_dir / "shore.tif",
        "river":    masks_dir / "river.tif",
    }

    total = (tx1 - tx0) * (tz1 - tz0)
    done  = 0

    for tz in range(tz0, tz1):
        for tx in range(tx0, tx1):
            col_off = tx * TILE_SIZE
            row_off = tz * TILE_SIZE
            w = min(TILE_SIZE, WORLD_SIZE - col_off)
            h = min(TILE_SIZE, WORLD_SIZE - row_off)
            if w <= 0 or h <= 0:
                continue

            # Read mask tile windows
            tiles: dict[str, np.ndarray] = {}
            for name, path in mask_files.items():
                if not path.exists():
                    tiles[name] = np.zeros((h, w), dtype=np.float32)
                    continue
                with rasterio.open(str(path)) as src:
                    win = Window(col_off, row_off, w, h)
                    raw = src.read(1, window=win)
                if raw.dtype == np.uint16:
                    tiles[name] = (raw.astype(np.float32) / 65535.0)
                elif raw.dtype == np.uint8:
                    tiles[name] = (raw.astype(np.float32) / 255.0)
                else:
                    tiles[name] = raw.astype(np.float32)

            # Biome assignment
            biome_grid = core_biome.assign_biomes(
                height_tile   = tiles["height"],
                slope_tile    = tiles["slope"],
                flow_tile     = tiles["flow"],
                erosion_tile  = tiles["erosion"],
                override_tile = tiles["override"],
                noise_fields  = noise,
                cfg           = cfg,
            )

            # Surface Y (needed for hillshade + ocean mask)
            surface_y = core_col.generate_columns(
                height_tile  = tiles["height"],
                slope_tile   = tiles["slope"],
                biome_grid   = biome_grid,
                shore_tile   = tiles["shore"],
                noise_fields = noise,
                cfg          = cfg,
                tile_x       = tx,
                tile_y       = tz,
            )

            # Render tile
            tile_rgba = render_tile(
                biome_grid  = biome_grid,
                surface_y   = surface_y,
                height_tile = tiles["height"],
                flow_tile   = tiles["flow"],
                shore_tile  = tiles["shore"],
                mode        = mode,
            )

            # Downsample if needed
            if scale > 1:
                from PIL import Image as _Image
                pil = _Image.fromarray(tile_rgba, "RGBA")
                pil = pil.resize((w // scale, h // scale), _Image.LANCZOS)
                tile_rgba = np.array(pil)

            # Paste into canvas
            out_row = (tz - tz0) * TILE_SIZE // scale
            out_col = (tx - tx0) * TILE_SIZE // scale
            th, tw  = tile_rgba.shape[:2]
            canvas[out_row:out_row+th, out_col:out_col+tw] = tile_rgba

            done += 1
            if progress_cb:
                progress_cb(done, total)

    # Save
    from PIL import Image
    output_png = Path(output_png)
    output_png.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(canvas, "RGBA").save(str(output_png))


# ---------------------------------------------------------------------------
# TILE UPDATE HELPER  (for Panel 3 live updates)
# ---------------------------------------------------------------------------

def render_tile_from_arrays(
    biome_grid:   np.ndarray,
    surface_y:    np.ndarray,
    height_tile:  np.ndarray,
    flow_tile:    np.ndarray,
    shore_tile:   np.ndarray,
    mode:         str = "biome",
    target_px:    int = 256,           # resize output to this square size
) -> np.ndarray:
    """
    Convenience wrapper for Panel 3.
    Returns (target_px, target_px, 4) RGBA uint8 — ready for QImage.
    """
    rgba = render_tile(
        biome_grid  = biome_grid,
        surface_y   = surface_y,
        height_tile = height_tile,
        flow_tile   = flow_tile,
        shore_tile  = shore_tile,
        mode        = mode,
    )

    H, W = rgba.shape[:2]
    if H != target_px or W != target_px:
        try:
            from PIL import Image
            pil  = Image.fromarray(rgba, "RGBA")
            pil  = pil.resize((target_px, target_px), Image.LANCZOS)
            rgba = np.array(pil)
        except ImportError:
            # No Pillow — nearest-neighbour manual resize
            scale_h = H / target_px
            scale_w = W / target_px
            rows    = (np.arange(target_px) * scale_h).astype(int).clip(0, H-1)
            cols    = (np.arange(target_px) * scale_w).astype(int).clip(0, W-1)
            rgba    = rgba[np.ix_(rows, cols)]

    return rgba


# ---------------------------------------------------------------------------
# SMOKE TEST  (stdlib + numpy only)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("preview_renderer.py — smoke test")

    H, W = 64, 64
    rng = np.random.default_rng(42)

    biome_grid  = np.full((H, W), "MIXED_FOREST",  dtype=object)
    biome_grid[:H//4, :]   = "ARCTIC_TUNDRA"
    biome_grid[H//2:, :W//2] = "SAND_DUNE_DESERT"
    biome_grid[H//4:H//2, :] = "_OCEAN"

    surface_y   = np.full((H, W), 80, dtype=np.int16)
    surface_y[H//4:H//2, :] = 50  # sub-sea

    height_tile = rng.random((H, W)).astype(np.float32)
    flow_tile   = np.zeros((H, W), dtype=np.float32)
    flow_tile[10:14, :] = 0.75  # fake river band
    shore_tile  = np.zeros((H, W), dtype=np.float32)
    shore_tile[:, :3]  = 1.0   # shore strip

    surface_blk = np.full((H, W), "grass_block", dtype=object)
    surface_blk[:H//4, :] = "snow_block"
    surface_blk[H//4:H//2, :] = "water"
    surface_blk[H//2:, :W//2] = "sand"

    # Test biome mode
    rgba_biome = render_tile(biome_grid, surface_y, height_tile,
                             flow_tile, shore_tile, mode="biome")
    assert rgba_biome.shape == (H, W, 4), f"biome mode shape wrong: {rgba_biome.shape}"
    assert rgba_biome[:, :, 3].min() == 255, "alpha channel not fully opaque"

    # Check ocean pixels are blue-ish
    ocean_rows = H//4
    blue_val = int(rgba_biome[ocean_rows, W//2, 2])
    assert blue_val > 50, f"ocean not blue enough: B={blue_val}"

    # Test block mode
    rgba_block = render_tile(biome_grid, surface_y, height_tile,
                             flow_tile, shore_tile,
                             surface_blk=surface_blk, mode="block")
    assert rgba_block.shape == (H, W, 4)

    # Test height mode
    rgba_ht = render_tile(biome_grid, surface_y, height_tile,
                          flow_tile, shore_tile, mode="height")
    assert rgba_ht.shape == (H, W, 4)
    # Height mode should be greyscale (R == G == B)
    assert np.all(rgba_ht[:, :, 0] == rgba_ht[:, :, 1]), "height not greyscale"

    # Test render_tile_from_arrays resize
    rgba_small = render_tile_from_arrays(
        biome_grid, surface_y, height_tile, flow_tile, shore_tile,
        mode="biome", target_px=32,
    )
    assert rgba_small.shape == (32, 32, 4), f"resize failed: {rgba_small.shape}"

    # Test river overlay applied
    river_row = 11
    with_river    = render_tile(biome_grid, surface_y, height_tile,
                                flow_tile, shore_tile,
                                mode="biome", river_overlay=True,  hillshade=False)
    without_river = render_tile(biome_grid, surface_y, height_tile,
                                flow_tile, shore_tile,
                                mode="biome", river_overlay=False, hillshade=False)
    # River pixels should be bluer with overlay
    b_with    = int(with_river[river_row, W//2, 2])
    b_without = int(without_river[river_row, W//2, 2])
    assert b_with > b_without, f"river overlay not bluer: {b_with} vs {b_without}"

    print(f"  biome mode shape   : {rgba_biome.shape}")
    print(f"  block mode shape   : {rgba_block.shape}")
    print(f"  height mode shape  : {rgba_ht.shape}")
    print(f"  resize to 32×32    : {rgba_small.shape}")
    print(f"  river overlay      : B={b_with} (with) vs B={b_without} (without) ✓")
    print(f"  BIOME_COLORS count : {len(BIOME_COLORS)} entries")
    print(f"  BLOCK_COLORS count : {len(BLOCK_COLORS)} entries")
    print("PASS")
    sys.exit(0)
