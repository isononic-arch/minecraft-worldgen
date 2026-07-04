"""render_space_map.py — "from space" satellite-style topdown of the FULL Vandir world. (v2)

Composites the 50k x 50k MAINLAND together with all 15 offset ISLANDS at their
true world positions into a single downscaled RGB canvas with:
  - naturalistic (satellite-look) biome tints, NOT the harsh validator palette
  - NW hillshade multiplied over the LAND (this is what sells "from space")
  - snow / rock / beach blends, rivers + lakes (mainland)
  - ONE FLAT UNIFORM OCEAN BLUE everywhere (v2: no depth shading, no island
    ocean-apron alpha blending — only LAND pixels from an island overwrite the
    canvas, so overlapping island bboxes can never half-cover each other with
    translucent water rectangles)
  - a legend panel (zones present + water/snow/rock/beach/river)
  - a toggleable 512-block tile-grid overlay with (tx,tz) labels for teleporting
  - a CULL/FOOTPRINT side-by-side view (v2): mainland extent as a gray field with
    faint coastline + each island's ACTUAL render footprint from
    islands/region_ownership_s101.json (colored translucent rect-sets, distinct
    outline + name label), the mainland-range collision regions (skip-list)
    hatched red, and the island-vs-island overlap region groups marked.

Outputs to islands/_val/ :
  space_map.png       — clean satellite view + legend
  space_map_grid.png  — same + 512-block tile grid overlay + TP note
  space_map_cull.png  — satellite | footprint panel side-by-side + island key

CLI:
  py islands/render_space_map.py [--stride N] [--out-dir islands/_val]

Reads mainland masks from masks/ via BANDED STRIDED windowed reads (memory-safe on
~7.4GB; a full 50k^2 uint16 array is 4.6GB and won't allocate). Islands read from
their per-island bbox masks in islands/masks_islands/<safe_name>/ at matching stride.

New file only. Does not import or modify run_pipeline / core / config / other islands/*.py.
"""
from __future__ import annotations
import os, sys, re, json, math, argparse, warnings
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window
from PIL import Image, ImageDraw, ImageFont

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
ISL = ROOT / "islands"
MASKS = ROOT / "masks"
MASKS_OUT = ISL / "masks_islands"
LAYOUT = ISL / "layout.json"
OWNERSHIP = ISL / "region_ownership_s101.json"

SEA_RAW = 17050            # raw 16-bit sea level (MC Y63); height > this = land
TILE = 512                 # world blocks per tile / region
WORLD = 50000              # mainland span in blocks
MAINLAND_REGIONS = 97      # regions 0..96 -> 0..49664 blocks

# --- Height -> MC Y spline (from config/thresholds.json terrain_spline / CLAUDE.md) ---
_GAEA_IN = [0, 17050, 45000, 65496]
_MCY_OUT = [-64, 63, 200, 448]

def _raw_to_mcy(raw):
    return np.interp(raw.astype(np.float32), _GAEA_IN, _MCY_OUT)


# ---------------------------------------------------------------------------
# Zone -> biome name (canonical, mirrors core/biome_assignment.OVERRIDE_BIOME_MAP)
# ---------------------------------------------------------------------------
OVERRIDE_BIOME_MAP = {
    0: "", 10: "COASTAL_HEATH", 20: "TEMPERATE_RAINFOREST", 30: "BOREAL_TAIGA",
    35: "SNOWY_BOREAL_TAIGA", 40: "BOREAL_ALPINE", 50: "ARCTIC_TUNDRA",
    55: "FROZEN_FLATS", 60: "TEMPERATE_DECIDUOUS", 70: "RAINFOREST_COAST",
    80: "RIPARIAN_WOODLAND", 90: "DRY_OAK_SAVANNA", 100: "KARST_BARRENS",
    110: "BIRCH_FOREST", 115: "EASTERN_TEMPERATE_COAST", 120: "MIXED_FOREST",
    130: "CONTINENTAL_STEPPE", 140: "DRY_PINE_BARRENS", 150: "SCRUBBY_HEATHLAND",
    160: "LUSH_RAINFOREST_COAST", 170: "SAND_DUNE_DESERT",
    190: "DESERT_STEPPE_TRANSITION", 200: "SEMI_ARID_SHRUBLAND",
    210: "DRY_WOODLAND_MAQUIS", 220: "TIDAL_JUNGLE_FRINGE", 230: "MANGROVE_COAST",
    240: "FRESHWATER_FEN", 254: "_OCEAN",
}

# Naturalistic, satellite-style earthy tints per biome (RGB 0..255).
SAT_COLORS = {
    "COASTAL_HEATH":           (122, 132,  92),   # muted olive-heath
    "TEMPERATE_RAINFOREST":    ( 42,  92,  52),   # deep temperate green
    "BOREAL_TAIGA":            ( 46,  78,  64),   # dark blue-green conifer
    "SNOWY_BOREAL_TAIGA":      (110, 128, 120),   # frost-grey green
    "BOREAL_ALPINE":           ( 78, 100,  88),   # cool alpine green-grey
    "ARCTIC_TUNDRA":           (176, 178, 168),   # pale grey-white
    "FROZEN_FLATS":            (214, 218, 222),   # near-white ice
    "TEMPERATE_DECIDUOUS":     ( 72, 112,  54),   # medium leafy green
    "RAINFOREST_COAST":        ( 38, 108,  50),   # vivid tropical green
    "RIPARIAN_WOODLAND":       ( 82, 118,  70),   # riverine green
    "DRY_OAK_SAVANNA":         (150, 148,  90),   # khaki-olive savanna
    "KARST_BARRENS":           (168, 172, 150),   # pale grey-green limestone
    "BIRCH_FOREST":            ( 96, 132,  72),   # light birch green
    "EASTERN_TEMPERATE_COAST": (108, 138,  84),   # coastal grass-green
    "MIXED_FOREST":            ( 62, 104,  60),   # mixed green
    "CONTINENTAL_STEPPE":      (156, 150,  96),   # dry khaki grassland
    "DRY_PINE_BARRENS":        ( 96, 116,  72),   # dusty pine
    "SCRUBBY_HEATHLAND":       (120, 112, 104),   # muted olive-purple scrub
    "LUSH_RAINFOREST_COAST":   ( 34, 116,  56),   # lush vivid green
    "SAND_DUNE_DESERT":        (214, 196, 148),   # pale sand
    "DESERT_STEPPE_TRANSITION":(188, 172, 118),   # tan-to-steppe
    "SEMI_ARID_SHRUBLAND":     (170, 156, 110),   # dusty tan-olive
    "DRY_WOODLAND_MAQUIS":     (132, 128,  86),   # (extinct) dry woodland
    "TIDAL_JUNGLE_FRINGE":     ( 56, 118,  70),   # mangrove-jungle green
    "MANGROVE_COAST":          ( 66, 104,  72),   # muddy green
    "FRESHWATER_FEN":          ( 92, 120,  88),   # wet meadow green
    "_OCEAN":                  None,               # island ocean sentinel -> flat sea
}
DEFAULT_LAND = (96, 120, 78)     # fallback for zone 0 / unknown on LAND

# v2: ONE flat uniform ocean blue everywhere (mainland sea + island aprons).
OCEAN_BLUE  = np.array([36, 74, 116], np.float32)
RIVER_COLOR = np.array([86, 150, 190], np.float32)    # bright river thread
LAKE_COLOR  = np.array([58, 112, 156], np.float32)    # flat lake blue
SNOW_TINT   = np.array([236, 240, 244], np.float32)
ROCK_TINT   = np.array([120, 112, 100], np.float32)   # bare grey-brown rock
BEACH_TINT  = np.array([224, 210, 168], np.float32)   # pale sand rim

# --- Cull/footprint panel colors ---
CULL_BG        = (16, 18, 24)
MAINLAND_FIELD = (52, 55, 62)     # mainland extent gray field
MAINLAND_LAND  = (84, 88, 96)     # faint coastline (land) over the field
COLLISION_FILL = (255, 40, 40)    # mainland-range collision hatch (red)
IVI_COLOR      = (255, 0, 255)    # island-vs-island overlap marker (magenta)

# 15 visually-distinct island colors for the footprint panel
ISLAND_PALETTE = [
    (255, 120,  60), ( 80, 200, 255), (140, 255, 100), (255, 220,  70),
    (200, 120, 255), (255, 100, 160), (100, 255, 200), (255, 170, 120),
    (120, 150, 255), (190, 255,  60), (255,  90,  90), ( 60, 230, 160),
    (240, 160, 220), (160, 200, 120), (110, 220, 235),
]


def _safe(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _snap(v):
    return int(round(v / TILE) * TILE)


# ---------------------------------------------------------------------------
# Banded strided read — memory-safe decimation of a large single-band raster.
# ---------------------------------------------------------------------------
def _read_strided(path: Path, stride: int, dtype=None, band_rows_mult=400):
    """Read `path` decimated by `stride` using windowed row-bands (bounded RAM).

    Returns (arr, out_h, out_w) at ceil(H/stride) x ceil(W/stride). Uses simple
    [::stride] point-decimation on each band — robust; no phantom-value averaging
    (safe for zone codes AND continuous height). Verified nonzero land fraction on
    height (~30%)."""
    with rasterio.open(str(path)) as s:
        W, H = s.width, s.height
        out_w = (W + stride - 1) // stride
        out_h = (H + stride - 1) // stride
        out = np.zeros((out_h, out_w), dtype or s.dtypes[0])
        band = max(stride, stride * band_rows_mult)
        for y0 in range(0, H, band):
            h = min(band, H - y0)
            a = s.read(1, window=Window(0, y0, W, h))
            dec = a[::stride, ::stride]
            oy = y0 // stride
            out[oy:oy + dec.shape[0], :dec.shape[1]] = dec
        return out, out_h, out_w


def _read_strided_opt(path: Path, stride: int, dtype=None):
    """Like _read_strided but returns None if file missing (optional masks)."""
    if not path.exists():
        return None
    arr, _, _ = _read_strided(path, stride, dtype)
    return arr


# ---------------------------------------------------------------------------
# Hillshade (NW light) from a downscaled height field.
# ---------------------------------------------------------------------------
def _hillshade(mcy, azimuth_deg=315.0, altitude_deg=45.0, z_exag=2.2):
    az = math.radians(360.0 - azimuth_deg + 90.0)
    alt = math.radians(altitude_deg)
    dy, dx = np.gradient(mcy.astype(np.float32) * z_exag)
    slope = np.pi / 2.0 - np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    shade = (np.sin(alt) * np.sin(slope) +
             np.cos(alt) * np.cos(slope) * np.cos(az - aspect))
    return np.clip(shade, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Paint a block of masks -> RGB (works for mainland OR an island sub-rect).
# ---------------------------------------------------------------------------
def _paint(height, override, snow, rock, beach,
           centerline=None, lake=None, width=None):
    """Return (rgb uint8, land_mask bool) for the given decimated mask block.

    v2: OCEAN = one flat uniform blue (OCEAN_BLUE). No depth shading. Hillshade
    is applied to LAND only, so every sea pixel in the whole composite is the
    exact same color."""
    H, W = height.shape
    hi = height.astype(np.int32)
    land = hi > SEA_RAW
    mcy = _raw_to_mcy(height)

    rgb = np.empty((H, W, 3), np.float32)
    rgb[:] = OCEAN_BLUE          # flat sea everywhere; land overwrites below

    # --- LAND base = naturalistic biome tint ---
    if override is not None:
        ov = override
        painted = np.zeros((H, W), bool)
        for zone, name in OVERRIDE_BIOME_MAP.items():
            if not name:
                continue
            col = SAT_COLORS.get(name)
            if col is None:
                continue    # _OCEAN sentinel stays flat sea
            m = land & (ov == zone)
            if m.any():
                rgb[m] = col
                painted |= m
        unset = land & ~painted
        rgb[unset] = DEFAULT_LAND
    else:
        rgb[land] = DEFAULT_LAND

    # --- BEACH: pale sand rim (land only) ---
    if beach is not None:
        bm = land & (beach > 0)
        if bm.any():
            rgb[bm] = 0.45 * rgb[bm] + 0.55 * BEACH_TINT

    # --- ROCK: blend toward bare grey-brown, stronger at altitude ---
    if rock is not None:
        rm = land & (rock > 0)
        if rm.any():
            alt_f = np.clip((mcy - 120.0) / 200.0, 0.15, 0.75)[rm]
            rgb[rm] = (1 - alt_f)[:, None] * rgb[rm] + alt_f[:, None] * ROCK_TINT

    # --- SNOW: tint toward white, stronger at altitude ---
    if snow is not None:
        sm = land & (snow > 0)
        if sm.any():
            alt_f = np.clip((mcy - 200.0) / 240.0, 0.35, 0.92)[sm]
            rgb[sm] = (1 - alt_f)[:, None] * rgb[sm] + alt_f[:, None] * SNOW_TINT

    # --- HILLSHADE multiplied over LAND ONLY (ocean stays perfectly flat) ---
    shade = _hillshade(mcy)
    sh_land = 0.55 + 0.75 * shade    # 0.55..1.30 -> some brightening on lit faces
    sh = np.where(land, sh_land, 1.0)[..., None]
    rgb = np.clip(rgb * sh, 0, 255)

    # --- RIVERS + LAKES painted AFTER shade so they stay crisp blue (mainland) ---
    if lake is not None:
        lm = lake > 0
        if lm.any():
            rgb[lm] = LAKE_COLOR
    if centerline is not None:
        cm = centerline > 0
        if width is not None:
            cm = cm | (width > 0)
        if cm.any():
            rgb[cm] = RIVER_COLOR

    return rgb.astype(np.uint8), land


# ---------------------------------------------------------------------------
# Font helper
# ---------------------------------------------------------------------------
def _font(size):
    for name in ("arial.ttf", "arialbd.ttf", "DejaVuSans.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Main compose
# ---------------------------------------------------------------------------
def build(stride: int, out_dir: Path):
    report = []
    def log(m):
        print(m, flush=True); report.append(m)

    layout = json.loads(LAYOUT.read_text())
    islands = layout["islands"]

    # --- 1. Union bbox over mainland + islands (in WORLD blocks) ---
    min_x = 0; min_z = 0; max_x = WORLD; max_z = WORLD
    isl_rects = []   # (entry, safe, mdir, ox, oz, h, w)
    for e in islands:
        safe = _safe(e["name"])
        mdir = MASKS_OUT / safe
        man_p = mdir / "manifest.json"
        if not man_p.exists():
            log(f"[skip] {safe}: no manifest.json"); continue
        man = json.loads(man_p.read_text())
        h, w = man["world_hw"]
        ox = _snap(e["world_offset_px"][0]); oz = _snap(e["world_offset_px"][1])
        isl_rects.append((e, safe, mdir, ox, oz, h, w))
        min_x = min(min_x, ox);       min_z = min(min_z, oz)
        max_x = max(max_x, ox + w);   max_z = max(max_z, oz + h)

    span_x = max_x - min_x; span_z = max_z - min_z
    cw = (span_x + stride - 1) // stride
    ch = (span_z + stride - 1) // stride
    log(f"[bbox] world x[{min_x},{max_x}] z[{min_z},{max_z}]  span {span_x}x{span_z} blocks")
    log(f"[canvas] {cw} x {ch} px  (stride={stride}, ~{stride} blocks/px)")

    def w2c(wx, wz):
        return (wx - min_x) // stride, (wz - min_z) // stride

    # v2: canvas starts as ONE flat uniform ocean blue.
    canvas = np.empty((ch, cw, 3), np.uint8)
    canvas[:] = OCEAN_BLUE.astype(np.uint8)
    present_zones = set()

    # --- 2. Paint mainland ---
    log("[mainland] reading masks (banded strided)...")
    m_height, mh, mw = _read_strided(MASKS / "height.tif", stride, np.uint16)
    m_over = _read_strided_opt(MASKS / "override.tif", stride, np.uint8)
    m_snow = _read_strided_opt(MASKS / "snow_gap.tif", stride, np.uint8)
    m_rock = _read_strided_opt(MASKS / "rock_gap.tif", stride, np.uint8)
    m_beach = _read_strided_opt(MASKS / "beach.tif", stride, np.uint8)
    m_cl = _read_strided_opt(MASKS / "hydro_centerline.tif", stride, np.uint8)
    m_wid = _read_strided_opt(MASKS / "hydro_width.tif", stride, np.uint8)
    m_lake = _read_strided_opt(MASKS / "hydro_lake.tif", stride, np.uint16)
    for nm, arr in [("override", m_over), ("snow_gap", m_snow), ("rock_gap", m_rock),
                    ("beach", m_beach), ("hydro_centerline", m_cl),
                    ("hydro_width", m_wid), ("hydro_lake", m_lake)]:
        if arr is None:
            log(f"[mainland] MISSING mask skipped: {nm}")
    if m_over is not None:
        present_zones.update(int(z) for z in np.unique(m_over)
                             if z in OVERRIDE_BIOME_MAP and OVERRIDE_BIOME_MAP[z]
                             and SAT_COLORS.get(OVERRIDE_BIOME_MAP[z]))

    m_rgb, m_land = _paint(m_height, m_over, m_snow, m_rock, m_beach,
                           centerline=m_cl, lake=m_lake, width=m_wid)
    cx0, cz0 = w2c(0, 0)
    canvas[cz0:cz0 + m_rgb.shape[0], cx0:cx0 + m_rgb.shape[1]] = m_rgb
    log(f"[mainland] painted at canvas ({cx0},{cz0}) size {m_rgb.shape[1]}x{m_rgb.shape[0]}, land frac {m_land.mean():.3f}")
    mainland_land = m_land          # kept for the cull panel coastline
    del m_height, m_over, m_snow, m_rock, m_beach, m_cl, m_wid, m_lake, m_rgb

    # --- 3. Paint each island: ONLY ITS LAND PIXELS overwrite the canvas (v2). ---
    # No ocean blend at all — island aprons/ocean stay the canvas' flat blue, so
    # overlapping island bboxes can never dim each other with translucent water.
    # Land always wins; later islands' land may legitimately overwrite earlier
    # islands' land in the known apron-overlap regions.
    landed = []; clipped = []
    for (e, safe, mdir, ox, oz, h, w) in isl_rects:
        hpath = mdir / "height.tif"
        if not hpath.exists():
            log(f"[skip] {safe}: no height.tif"); continue
        i_h = _read_strided_opt(hpath, stride, np.uint16)
        i_ov = _read_strided_opt(mdir / "override.tif", stride, np.uint8)
        i_sn = _read_strided_opt(mdir / "snow_gap.tif", stride, np.uint8)
        i_rk = _read_strided_opt(mdir / "rock_gap.tif", stride, np.uint8)
        i_bc = _read_strided_opt(mdir / "beach.tif", stride, np.uint8)
        # islands: no hydro_* -> no rivers/lakes
        i_rgb, i_land = _paint(i_h, i_ov, i_sn, i_rk, i_bc)
        if i_ov is not None:
            present_zones.update(int(z) for z in np.unique(i_ov[i_land])
                                 if z in OVERRIDE_BIOME_MAP and OVERRIDE_BIOME_MAP[z]
                                 and SAT_COLORS.get(OVERRIDE_BIOME_MAP[z]))

        tx0, tz0 = w2c(ox, oz)
        ih, iw = i_rgb.shape[:2]
        tx1, tz1 = tx0 + iw, tz0 + ih
        sx0 = max(0, -tx0); sz0 = max(0, -tz0)
        dx0 = max(0, tx0);  dz0 = max(0, tz0)
        dx1 = min(cw, tx1);  dz1 = min(ch, tz1)
        was_clipped = (tx0 < 0 or tz0 < 0 or tx1 > cw or tz1 > ch)
        if dx1 <= dx0 or dz1 <= dz0:
            log(f"[island] {safe}: FULLY off-canvas, skipped")
            continue
        sub = i_rgb[sz0:sz0 + (dz1 - dz0), sx0:sx0 + (dx1 - dx0)]
        sub_land = i_land[sz0:sz0 + (dz1 - dz0), sx0:sx0 + (dx1 - dx0)]
        dst = canvas[dz0:dz1, dx0:dx1]
        dst[sub_land] = sub[sub_land]        # LAND ALWAYS WINS; ocean untouched
        landed.append((safe, ox, oz, iw, ih, was_clipped))
        log(f"[island] {safe}: painted at world({ox},{oz}) canvas({tx0},{tz0}) "
            f"size {iw}x{ih} land {i_land.mean():.3f}{' [CLIPPED]' if was_clipped else ''}")
        if was_clipped:
            clipped.append(safe)
        del i_h, i_ov, i_sn, i_rk, i_bc, i_rgb, i_land

    # --- 3b. VERIFY flat ocean / no translucent-water overlap: sample a known
    # island-vs-island bbox overlap area — every non-land pixel there must be the
    # EXACT flat OCEAN_BLUE (mainland ocean is painted the same constant).
    ob = OCEAN_BLUE.astype(np.uint8)
    ovx0, ovz0 = w2c(95 * TILE, 103 * TILE)      # Ouvea x Margarita overlap regions
    ovx1, ovz1 = w2c(97 * TILE, 104 * TILE)
    patch = canvas[max(0, ovz0):ovz1, max(0, ovx0):ovx1].reshape(-1, 3)
    if patch.size:
        is_flat = (patch == ob).all(axis=1)
        log(f"[verify] ouvea/margarita bbox-overlap patch: {is_flat.mean()*100:.1f}% "
            f"pixels are EXACT flat ocean blue (rest = island land) — no alpha-blend water")

    # --- 4. Build the three PNGs ---
    out_dir.mkdir(parents=True, exist_ok=True)
    base_img = Image.fromarray(canvas, "RGB")

    legend_zones = sorted(present_zones)
    clean = _compose_with_legend(base_img, legend_zones, min_x, min_z, max_x, max_z, stride)
    p_clean = out_dir / "space_map.png"
    clean.save(p_clean)
    del clean

    grid_base = base_img.copy()
    _draw_grid(grid_base, min_x, min_z, max_x, max_z, stride)
    gridded = _compose_with_legend(grid_base, legend_zones, min_x, min_z, max_x, max_z,
                                   stride, grid=True)
    p_grid = out_dir / "space_map_grid.png"
    gridded.save(p_grid)
    del grid_base, gridded

    # --- 4b. CULL / FOOTPRINT panel (side-by-side with the satellite view) ---
    p_cull = None
    if OWNERSHIP.exists():
        own = json.loads(OWNERSHIP.read_text())
        cull_img, isl_colors, cull_stats = _build_cull_panel(
            (cw, ch), mainland_land, (cx0, cz0), own, min_x, min_z, stride, log)
        cull = _compose_cull(base_img, cull_img, isl_colors, own, cull_stats,
                             min_x, min_z, max_x, max_z, stride)
        p_cull = out_dir / "space_map_cull.png"
        cull.save(p_cull)
        del cull_img, cull
    else:
        log(f"[cull] MISSING {OWNERSHIP} — footprint panel skipped")

    # --- 5. Validate outputs are non-trivial ---
    for p in [p_clean, p_grid] + ([p_cull] if p_cull else []):
        arr = np.asarray(Image.open(p))
        sz = p.stat().st_size
        nonblack = float((arr[..., :3].sum(axis=2) > 24).mean())
        log(f"[out] {p}  {arr.shape[1]}x{arr.shape[0]}  {sz/1024:.0f} KB  nonblack={nonblack:.3f}")
        if sz < 20_000 or nonblack < 0.05:
            log(f"[WARN] {p} looks trivial/blank!")

    log(f"[islands] landed {len(landed)}/{len(isl_rects)}"
        + (f", clipped: {', '.join(clipped)}" if clipped else ", none clipped"))
    return p_clean, p_grid, p_cull, report


# ---------------------------------------------------------------------------
# Cull / footprint panel — region ownership visualization
# ---------------------------------------------------------------------------
def _build_cull_panel(canvas_wh, mainland_land, mainland_c0, own,
                      min_x, min_z, stride, log):
    """Build the footprint panel image (same px geometry as the satellite canvas).

    - mainland extent 0..49664 = gray field, coastline (land) faintly lighter
    - each island's owned regions (region_ownership_s101.json) = translucent
      colored rect-set + boundary outline + name label
    - mainland-range collision regions (skip-list) = red diagonal hatch
    - island-vs-island overlap region groups = magenta X-marked outline
    Returns (PIL RGB image, {safe: color}, stats dict)."""
    cw, ch = canvas_wh
    mx0, mz0 = mainland_c0

    # region (rx,rz) -> canvas px rect
    def rrect(rx, rz):
        x0 = round((rx * TILE - min_x) / stride); z0 = round((rz * TILE - min_z) / stride)
        x1 = round(((rx + 1) * TILE - min_x) / stride); z1 = round(((rz + 1) * TILE - min_z) / stride)
        return x0, z0, x1, z1

    # --- mainland gray field + faint coastline from the land mask ---
    arr = np.empty((ch, cw, 3), np.uint8)
    arr[:] = CULL_BG
    fx1 = round((MAINLAND_REGIONS * TILE - min_x) / stride)
    fz1 = round((MAINLAND_REGIONS * TILE - min_z) / stride)
    arr[mz0:fz1, mx0:fx1] = MAINLAND_FIELD
    lh, lw = mainland_land.shape
    sub = arr[mz0:mz0 + lh, mx0:mx0 + lw]
    sub[mainland_land] = MAINLAND_LAND
    arr[mz0:mz0 + lh, mx0:mx0 + lw] = sub
    img = Image.fromarray(arr, "RGB")
    d = ImageDraw.Draw(img, "RGBA")

    # --- per-island owned regions ---
    isl_colors = {}
    label_pts = []
    for i, (safe, regions) in enumerate(own["islands"].items()):
        col = ISLAND_PALETTE[i % len(ISLAND_PALETTE)]
        isl_colors[safe] = col
        rset = {(rx, rz) for rx, rz in regions}
        for (rx, rz) in rset:                      # translucent fill
            x0, z0, x1, z1 = rrect(rx, rz)
            d.rectangle([x0, z0, x1 - 1, z1 - 1], fill=col + (58,))
        for (rx, rz) in rset:                      # outline only on set boundary
            x0, z0, x1, z1 = rrect(rx, rz)
            if (rx, rz - 1) not in rset: d.line([(x0, z0), (x1, z0)], fill=col + (230,), width=3)
            if (rx, rz + 1) not in rset: d.line([(x0, z1), (x1, z1)], fill=col + (230,), width=3)
            if (rx - 1, rz) not in rset: d.line([(x0, z0), (x0, z1)], fill=col + (230,), width=3)
            if (rx + 1, rz) not in rset: d.line([(x1, z0), (x1, z1)], fill=col + (230,), width=3)
        cxs = sum(r[0] for r in rset) / len(rset); czs = sum(r[1] for r in rset) / len(rset)
        px = round(((cxs + 0.5) * TILE - min_x) / stride)
        pz = round(((czs + 0.5) * TILE - min_z) / stride)
        label_pts.append((px, pz, safe, col))

    # --- mainland-range collision regions (skip-list): red diagonal hatch ---
    collisions = own.get("mainland_collisions", [])
    hatch_step = max(6, round(TILE / stride / 6))
    n_on_land = 0
    for (rx, rz) in collisions:
        x0, z0, x1, z1 = rrect(rx, rz)
        d.rectangle([x0, z0, x1 - 1, z1 - 1], outline=COLLISION_FILL + (200,), width=2)
        for off in range(-(z1 - z0), x1 - x0, hatch_step):
            d.line([(x0 + off, z0), (x0 + off + (z1 - z0), z1)],
                   fill=COLLISION_FILL + (120,), width=1)
        # check: does this collision region contain MAINLAND LAND?
        lx0 = max(0, x0 - mx0); lz0 = max(0, z0 - mz0)
        lx1 = min(lw, x1 - mx0); lz1 = min(lh, z1 - mz0)
        if lx1 > lx0 and lz1 > lz0 and mainland_land[lz0:lz1, lx0:lx1].any():
            n_on_land += 1
    log(f"[cull] {len(collisions)} mainland-range collision regions hatched; "
        f"{n_on_land} of them contain MAINLAND LAND pixels"
        + (" <-- CONFLICT!" if n_on_land else " (all pure ocean — no land conflict)"))

    # --- island-vs-island overlap groups: magenta X-marked outline ---
    ivi = own.get("island_vs_island", {})
    f_ivi = _font(max(20, round(TILE / stride * 0.45)))
    for gi, (pair, regions) in enumerate(ivi.items(), 1):
        for (rx, rz) in regions:
            x0, z0, x1, z1 = rrect(rx, rz)
            d.rectangle([x0 - 2, z0 - 2, x1 + 1, z1 + 1], outline=IVI_COLOR + (255,), width=4)
            d.line([(x0, z0), (x1, z1)], fill=IVI_COLOR + (200,), width=3)
            d.line([(x0, z1), (x1, z0)], fill=IVI_COLOR + (200,), width=3)
        rx0, rz0 = regions[0]
        x0, z0, _, _ = rrect(rx0, rz0)
        d.text((x0 + 4, z0 - round(TILE / stride) - 6), f"OVL {gi}",
               fill=IVI_COLOR + (255,), font=f_ivi, stroke_width=2, stroke_fill=(0, 0, 0, 255))
    log(f"[cull] {len(ivi)} island-vs-island overlap groups marked: "
        + "; ".join(f"OVL {i+1}={k} ({len(v)} rgn)" for i, (k, v) in enumerate(ivi.items())))

    # --- island name labels (last, so they stay on top) ---
    f_lbl = _font(max(26, round(TILE / stride * 0.85)))
    for (px, pz, safe, col) in label_pts:
        short = " ".join(safe.split("_")[:2]).title()
        d.text((px, pz), short, fill=col + (255,), font=f_lbl, anchor="mm",
               stroke_width=3, stroke_fill=(0, 0, 0, 255))

    stats = {"n_collisions": len(collisions), "n_collisions_on_land": n_on_land,
             "n_ivi_groups": len(ivi)}
    return img, isl_colors, stats


def _compose_cull(sat_img, cull_img, isl_colors, own, stats,
                  min_x, min_z, max_x, max_z, stride):
    """Side-by-side: satellite | footprint panel, + island color key strip."""
    mw, mh = sat_img.size
    gap = 12; strip_w = 430; top = 76
    out = Image.new("RGB", (mw * 2 + gap + strip_w, mh + top), (12, 14, 18))
    out.paste(sat_img, (0, top))
    out.paste(cull_img, (mw + gap, top))

    d = ImageDraw.Draw(out)
    tf = _font(34); cf = _font(18); lf = _font(17); sf = _font(15)
    d.text((16, 12), "Vandir — From Space  |  Render Footprints (region ownership S101)",
           fill=(235, 238, 242), font=tf)
    d.text((16, 50), f"Left: satellite composite. Right: cull/footprint view — "
           f"world x[{min_x},{max_x}] z[{min_z},{max_z}], {stride} blocks/px, "
           f"regions = 512 blocks", fill=(150, 158, 168), font=cf)

    lx = mw * 2 + gap + 16; ly = top + 14
    d.text((lx, ly), "ISLAND FOOTPRINTS", fill=(235, 238, 242), font=_font(22)); ly += 34
    sw = 22
    def swatch(rgb, label):
        nonlocal ly
        d.rectangle([lx, ly, lx + sw, ly + sw], fill=tuple(rgb), outline=(60, 64, 70))
        d.text((lx + sw + 8, ly + 3), label, fill=(210, 214, 220), font=lf)
        ly += sw + 6
    for safe, col in isl_colors.items():
        n = len(own["islands"][safe])
        swatch(col, f"{' '.join(safe.split('_')[:3]).title()} ({n} rgn)")
    ly += 10
    d.text((lx, ly), "Markers:", fill=(150, 158, 168), font=sf); ly += 22
    swatch(MAINLAND_FIELD, "Mainland extent (0..49664)")
    swatch(MAINLAND_LAND, "Mainland land (coastline)")
    swatch(COLLISION_FILL, f"Mainland-range collision / skip-list ({stats['n_collisions']} rgn, hatched)")
    swatch(IVI_COLOR, f"Island-vs-island overlap ({stats['n_ivi_groups']} groups, X-marked)")
    ly += 8
    ok = stats["n_collisions_on_land"] == 0
    msg = ("All collision regions are pure ocean —" if ok else
           f"WARNING: {stats['n_collisions_on_land']} collision regions touch mainland LAND")
    d.text((lx, ly), msg, fill=(120, 220, 150) if ok else (255, 90, 90), font=lf); ly += 20
    if ok:
        d.text((lx, ly), "no island footprint conflicts onto mainland land.",
               fill=(120, 220, 150), font=lf)
    return out


# ---------------------------------------------------------------------------
# Tile-grid overlay (drawn onto the map area, in PLACE)
# ---------------------------------------------------------------------------
def _draw_grid(img: Image.Image, min_x, min_z, max_x, max_z, stride,
               label_every=8):
    d = ImageDraw.Draw(img, "RGBA")
    W, H = img.size
    f = _font(max(9, int(28 / max(1, stride / 11))))
    line = (255, 255, 255, 60)
    line_major = (255, 255, 60, 130)

    tx_start = math.floor(min_x / TILE); tx_end = math.ceil(max_x / TILE)
    for tx in range(tx_start, tx_end + 1):
        px = int((tx * TILE - min_x) / stride)
        if 0 <= px < W:
            major = (tx % label_every == 0)
            d.line([(px, 0), (px, H)], fill=line_major if major else line, width=1)
    tz_start = math.floor(min_z / TILE); tz_end = math.ceil(max_z / TILE)
    for tz in range(tz_start, tz_end + 1):
        py = int((tz * TILE - min_z) / stride)
        if 0 <= py < H:
            major = (tz % label_every == 0)
            d.line([(0, py), (W, py)], fill=line_major if major else line, width=1)

    for tx in range(tx_start, tx_end + 1):
        if tx % label_every != 0:
            continue
        for tz in range(tz_start, tz_end + 1):
            if tz % label_every != 0:
                continue
            px = int((tx * TILE - min_x) / stride); py = int((tz * TILE - min_z) / stride)
            if 0 <= px < W - 30 and 0 <= py < H - 12:
                d.text((px + 2, py + 1), f"{tx},{tz}", fill=(255, 255, 120, 220), font=f)


# ---------------------------------------------------------------------------
# Compose final image: map + title/caption + right-side legend strip.
# ---------------------------------------------------------------------------
def _compose_with_legend(map_img: Image.Image, zones, min_x, min_z, max_x, max_z,
                         stride, grid=False):
    mw, mh = map_img.size
    strip_w = 360
    top = 70
    out = Image.new("RGB", (mw + strip_w, mh + top), (12, 14, 18))
    out.paste(map_img, (0, top))

    d = ImageDraw.Draw(out)
    tf = _font(34); cf = _font(18); lf = _font(17); sf = _font(15)

    title = "Vandir — From Space" + ("  (tile grid)" if grid else "")
    d.text((16, 14), title, fill=(235, 238, 242), font=tf)
    cap = (f"Full world: mainland 50k x 50k + 15 islands  |  world x[{min_x},{max_x}] "
           f"z[{min_z},{max_z}]  |  {stride} blocks/px")
    d.text((16, 50), cap, fill=(150, 158, 168), font=cf)

    lx = mw + 16; ly = top + 14
    d.text((lx, ly), "LEGEND", fill=(235, 238, 242), font=_font(22)); ly += 34
    sw = 22; gap = 6

    def swatch(rgb, label):
        nonlocal ly
        d.rectangle([lx, ly, lx + sw, ly + sw], fill=tuple(int(c) for c in rgb),
                    outline=(60, 64, 70))
        d.text((lx + sw + 8, ly + 3), label, fill=(210, 214, 220), font=lf)
        ly += sw + gap

    d.text((lx, ly), "Biomes present:", fill=(150, 158, 168), font=sf); ly += 22
    for z in zones:
        name = OVERRIDE_BIOME_MAP.get(z, "")
        col = SAT_COLORS.get(name)
        if col:
            swatch(col, f"{name.replace('_',' ').title()} ({z})")
    ly += 8
    d.text((lx, ly), "Terrain & water:", fill=(150, 158, 168), font=sf); ly += 22
    swatch(OCEAN_BLUE, "Ocean (uniform)")
    swatch(RIVER_COLOR, "River")
    swatch(LAKE_COLOR, "Lake")
    swatch(SNOW_TINT, "Snow")
    swatch(ROCK_TINT, "Bare rock")
    swatch(BEACH_TINT, "Beach / sand rim")

    if grid:
        ly += 10
        d.text((lx, ly), "Tile grid:", fill=(150, 158, 168), font=sf); ly += 22
        d.text((lx, ly), "512-block tiles. Labels = (tx,tz),", fill=(210, 214, 220), font=sf); ly += 20
        d.text((lx, ly), "every 8th line. Yellow = major.", fill=(210, 214, 220), font=sf); ly += 26
        d.text((lx, ly), "TP to tile center:", fill=(235, 238, 242), font=lf); ly += 22
        d.text((lx, ly), "/tp @s (tx*512+256)", fill=(120, 220, 150), font=sf); ly += 18
        d.text((lx, ly), "        200 (tz*512+256)", fill=(120, 220, 150), font=sf); ly += 24
        d.text((lx, ly), "Mainland tx,tz = 0..96.", fill=(180, 186, 192), font=sf); ly += 18
        d.text((lx, ly), "Islands sit at tx=world_x//512.", fill=(180, 186, 192), font=sf)

    return out


def main():
    ap = argparse.ArgumentParser(description="From-space satellite topdown of full Vandir world")
    ap.add_argument("--stride", type=int, default=0,
                    help="world blocks per pixel (0 = auto: mainland ~4500px)")
    ap.add_argument("--out-dir", default=str(ISL / "_val"))
    a = ap.parse_args()
    stride = a.stride if a.stride > 0 else max(1, round(WORLD / 4500))
    out_dir = Path(a.out_dir)
    print(f"[cfg] stride={stride} (mainland ~{WORLD//stride}px), out_dir={out_dir}", flush=True)
    p_clean, p_grid, p_cull, _ = build(stride, out_dir)
    print("\n=== DONE ===")
    print("clean:", p_clean)
    print("grid :", p_grid)
    print("cull :", p_cull)


if __name__ == "__main__":
    main()
