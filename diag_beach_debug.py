"""diag_beach_debug.py — S55 v7 fast beach geometry debug.

Runs ONLY the beach placement math from eco_gradients (copied inline) for
a single tile, in isolation.  Skips river carving, surface pipeline, and
chunk_writer.  ~5-10s per run.

Outputs three PNGs so we can see where the dither is breaking down:

    beach_regions.png  4-color map:
        dark blue   = ocean (surface_y < 63)
        dark green  = land, not in beach biome  / outside dither total
        light green = land, in beach biome, beyond dither
        yellow      = core (always sand)
        orange      = dither zone, got sand via coin
        grey        = dither zone, did NOT get sand

    beach_prob.png     grayscale P(sand) field in the dither zone
    beach_coin.png     grayscale Gaussian-blurred coin field

Usage:
    py diag_beach_debug.py --tile-x 6 --tile-z 72
"""
from __future__ import annotations
import argparse
import time
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from scipy.ndimage import distance_transform_edt, gaussian_filter
from PIL import Image

# --- Must match core/eco_gradients.py beach section ---------------------------
_FULL_BEACH = ("COASTAL_HEATH", "EASTERN_TEMPERATE_COAST",
               "RAINFOREST_COAST", "LUSH_RAINFOREST_COAST")
_SHALLOW_BEACH = ("TEMPERATE_RAINFOREST", "BOREAL_TAIGA", "TEMPERATE_DECIDUOUS")

TILE = 512
SEA_LEVEL = 63

# spline from column_generator._build_lut_vectorized
_GAEA_IN = np.array([0, 17050, 45000, 65496], dtype=np.float64)
_MC_Y_OUT = np.array([-64, 63, 200, 448], dtype=np.float64)
_LUT = np.clip(
    np.interp(np.arange(65536, dtype=np.float64), _GAEA_IN, _MC_Y_OUT),
    -64 + 4, 448 - 1,
).astype(np.int16)


def load_tile(tx: int, tz: int, masks_dir: Path):
    """Load surface_y, biome_grid for tile."""
    win = Window(tx * TILE, tz * TILE, TILE, TILE)
    with rasterio.open(masks_dir / "height.tif") as src:
        h_raw = src.read(1, window=win)
    with rasterio.open(masks_dir / "override.tif") as src:
        ov = src.read(1, window=win)

    surface_y = _LUT[h_raw.astype(np.int32)].astype(np.int16)

    # Build biome_grid (object array of biome names)
    from core.biome_assignment import OVERRIDE_BIOME_MAP
    biome_grid = np.full((TILE, TILE), "", dtype=object)
    for code in np.unique(ov):
        name = OVERRIDE_BIOME_MAP.get(int(code), "")
        if name:
            biome_grid[ov == code] = name

    return surface_y, biome_grid


def compute_beach_debug(surface_y: np.ndarray, biome_grid: np.ndarray,
                       tile_x: int, tile_z: int) -> dict:
    """Run the beach placement math (mirror of core/eco_gradients.py)."""
    H, W = surface_y.shape
    land_mask = surface_y >= SEA_LEVEL
    # No river_meta (skip river carving) — water_mask = 0
    water_mask = np.zeros((H, W), dtype=bool)

    # --- Ocean seed ---
    _ocean = (surface_y < SEA_LEVEL) & ~water_mask
    _dist_from_ocean = distance_transform_edt(~_ocean).astype(np.float32)

    # --- Biome classes ---
    _full_bch = np.zeros((H, W), dtype=bool)
    _shallow_bch = np.zeros((H, W), dtype=bool)
    for b in _FULL_BEACH:
        _full_bch |= (biome_grid == b)
    for b in _SHALLOW_BEACH:
        _shallow_bch |= (biome_grid == b)
    _any_beach_biome = _full_bch | _shallow_bch

    # --- Width fields ---
    _base_width = np.where(
        _full_bch, np.float32(4.0),
        np.where(_shallow_bch, np.float32(2.0), np.float32(0.0)),
    ).astype(np.float32)
    _amp = np.where(
        _full_bch, np.float32(2.0),
        np.where(_shallow_bch, np.float32(1.0), np.float32(0.0)),
    ).astype(np.float32)

    _bch_rng = np.random.default_rng(99001 + tile_x * 97 + tile_z)
    _wn_raw = gaussian_filter(
        _bch_rng.standard_normal((H, W)).astype(np.float32), sigma=12)
    _wn_lo, _wn_hi = float(_wn_raw.min()), float(_wn_raw.max())
    if _wn_hi > _wn_lo:
        _width_noise = (2.0 * (_wn_raw - _wn_lo) / (_wn_hi - _wn_lo) - 1.0).astype(np.float32)
    else:
        _width_noise = np.zeros_like(_wn_raw)

    _core_width = np.maximum(_base_width + _amp * _width_noise, 0.0).astype(np.float32)
    _core_width[~_any_beach_biome] = 0.0
    _dither_width = _core_width * np.float32(3.0)
    _total_width = _core_width + _dither_width

    # --- Eligibility (biome + land + gap==0 + dist>0) ---
    _bch_eligible = (
        land_mask & ~water_mask
        & _any_beach_biome
        & (_dist_from_ocean > 0)
    )

    # --- Core ---
    _bch_core = _bch_eligible & (_dist_from_ocean <= _core_width)

    # --- Dither zone ---
    _in_dither = (
        _bch_eligible
        & (_dist_from_ocean > _core_width)
        & (_dist_from_ocean <= _total_width)
    )
    _t = np.clip(
        (_dist_from_ocean - _core_width) / np.maximum(_dither_width, 0.5),
        0.0, 1.0,
    )
    _place_prob = np.clip(1.0 - _t, 0.15, 0.85).astype(np.float32)

    _dr_raw = gaussian_filter(
        _bch_rng.random((H, W)).astype(np.float32), sigma=1)
    _dr_lo, _dr_hi = float(_dr_raw.min()), float(_dr_raw.max())
    if _dr_hi > _dr_lo:
        _dith_coin = ((_dr_raw - _dr_lo) / (_dr_hi - _dr_lo)).astype(np.float32)
    else:
        _dith_coin = _dr_raw

    _bch_dithered = _in_dither & (_dith_coin < _place_prob)

    return {
        "ocean": _ocean,
        "land": land_mask,
        "any_beach_biome": _any_beach_biome,
        "dist_from_ocean": _dist_from_ocean,
        "core_width": _core_width,
        "dither_width": _dither_width,
        "total_width": _total_width,
        "bch_core": _bch_core,
        "in_dither": _in_dither,
        "bch_dithered": _bch_dithered,
        "place_prob": _place_prob,
        "dith_coin": _dith_coin,
    }


def render_regions(dbg: dict, out: Path):
    """4-color region map."""
    H, W = dbg["ocean"].shape
    img = np.zeros((H, W, 3), dtype=np.uint8)
    # Default: white (non-land non-beach non-ocean — shouldn't happen but fallback)
    img[:] = (255, 255, 255)
    # Ocean — dark blue
    img[dbg["ocean"]] = (20, 40, 100)
    # Land (default) — dark green
    img[dbg["land"] & ~dbg["any_beach_biome"]] = (40, 80, 40)
    # Land + beach biome but outside any beach zone — light green
    beach_zone = dbg["bch_core"] | dbg["in_dither"]
    img[dbg["land"] & dbg["any_beach_biome"] & ~beach_zone] = (120, 180, 80)
    # Dither zone, did NOT get sand — grey
    img[dbg["in_dither"] & ~dbg["bch_dithered"]] = (160, 160, 160)
    # Dither zone, GOT sand via coin — orange
    img[dbg["bch_dithered"]] = (230, 150, 50)
    # Core, always sand — yellow
    img[dbg["bch_core"]] = (240, 220, 80)
    Image.fromarray(img).save(out)


def render_prob(dbg: dict, out: Path):
    """Grayscale place_prob field — only inside dither zone."""
    p = dbg["place_prob"].copy()
    # Show prob only where dither zone is active; elsewhere black
    mask = dbg["in_dither"]
    g = np.zeros_like(p)
    g[mask] = p[mask]
    # Colour ocean dark blue for context
    img = (g * 255).astype(np.uint8)
    rgb = np.stack([img, img, img], axis=-1)
    rgb[dbg["ocean"]] = (20, 40, 100)
    rgb[dbg["bch_core"]] = (240, 220, 80)  # core highlighted yellow
    Image.fromarray(rgb).save(out)


def render_coin(dbg: dict, out: Path):
    """Grayscale Gaussian-blurred coin field across entire tile."""
    c = dbg["dith_coin"]
    img = (np.clip(c, 0.0, 1.0) * 255).astype(np.uint8)
    rgb = np.stack([img, img, img], axis=-1)
    rgb[dbg["ocean"]] = (20, 40, 100)
    Image.fromarray(rgb).save(out)


def print_stats(dbg: dict, tx: int, tz: int) -> None:
    """Summary numbers."""
    print(f"Tile ({tx},{tz}):")
    print(f"  ocean                    : {int(dbg['ocean'].sum()):>6d}")
    print(f"  land                     : {int(dbg['land'].sum()):>6d}")
    print(f"  beach-eligible biome     : {int(dbg['any_beach_biome'].sum()):>6d}")
    print(f"  core (always sand)       : {int(dbg['bch_core'].sum()):>6d}")
    print(f"  in_dither (zone total)   : {int(dbg['in_dither'].sum()):>6d}")
    print(f"  dithered (got sand)      : {int(dbg['bch_dithered'].sum()):>6d}")
    print(f"  dither non-sand          : {int((dbg['in_dither'] & ~dbg['bch_dithered']).sum()):>6d}")
    inb = dbg["any_beach_biome"]
    if inb.any():
        cw = dbg["core_width"][inb]
        dw = dbg["dither_width"][inb]
        tw = dbg["total_width"][inb]
        print(f"  core_width (biome pixels): min={cw.min():.2f} max={cw.max():.2f} mean={cw.mean():.2f}")
        print(f"  dither_width             : min={dw.min():.2f} max={dw.max():.2f} mean={dw.mean():.2f}")
        print(f"  total_width              : min={tw.min():.2f} max={tw.max():.2f} mean={tw.mean():.2f}")
    if dbg["in_dither"].any():
        p = dbg["place_prob"][dbg["in_dither"]]
        c = dbg["dith_coin"][dbg["in_dither"]]
        print(f"  place_prob (in dither)   : min={p.min():.3f} max={p.max():.3f} mean={p.mean():.3f}")
        print(f"  dith_coin  (in dither)   : min={c.min():.3f} max={c.max():.3f} mean={c.mean():.3f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tile-x", type=int, default=6)
    ap.add_argument("--tile-z", type=int, default=72)
    ap.add_argument("--masks", type=Path, default=Path("masks"))
    ap.add_argument("--out", type=Path, default=Path("diag_output/beach_debug"))
    args = ap.parse_args()

    tx, tz = args.tile_x, args.tile_z
    args.out.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    surface_y, biome_grid = load_tile(tx, tz, args.masks)
    t1 = time.time()
    dbg = compute_beach_debug(surface_y, biome_grid, tx, tz)
    t2 = time.time()
    render_regions(dbg, args.out / f"beach_regions_{tx}_{tz}.png")
    render_prob(dbg, args.out / f"beach_prob_{tx}_{tz}.png")
    render_coin(dbg, args.out / f"beach_coin_{tx}_{tz}.png")
    t3 = time.time()

    print(f"Load  : {t1-t0:.2f}s")
    print(f"Beach : {t2-t1:.2f}s")
    print(f"Render: {t3-t2:.2f}s")
    print(f"TOTAL : {t3-t0:.2f}s")
    print()
    print_stats(dbg, tx, tz)
    print()
    print(f"Output: {args.out}")


if __name__ == "__main__":
    main()
