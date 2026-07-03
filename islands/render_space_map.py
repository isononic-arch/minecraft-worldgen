"""render_space_map.py — "from space" satellite-style topdown of the FULL Vandir world.

Composites the 50k x 50k MAINLAND together with all 15 offset ISLANDS at their
true world positions into a single downscaled RGB canvas with:
  - naturalistic (satellite-look) biome tints, NOT the harsh validator palette
  - NW hillshade multiplied over the base (this is what sells "from space")
  - snow / rock / beach blends, depth-shaded ocean, rivers + lakes (mainland)
  - a legend panel (zones present + water/snow/rock/beach/river)
  - a toggleable 512-block tile-grid overlay with (tx,tz) labels for teleporting

Outputs to islands/_val/ :
  space_map.png       — clean satellite view + legend
  space_map_grid.png  — same + 512-block tile grid overlay + TP note

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

SEA_RAW = 17050            # raw 16-bit sea level (MC Y63); height > this = land
LAND_MARGIN = 40           # land test hysteresis (matches render_drive)
TILE = 512                 # world blocks per tile
WORLD = 50000              # mainland span in blocks

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
# Chosen to read like a real orbital photo, NOT the validator's saturated palette.
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
    "_OCEAN":                  ( 40,  86, 128),   # island open-ocean sentinel
}
# Fallback for zone 0 / unknown on LAND
DEFAULT_LAND = (96, 120, 78)

# Water / feature colors
OCEAN_SHALLOW = np.array([70, 128, 158], np.float32)   # near-shore sea
OCEAN_DEEP    = np.array([18, 46, 84], np.float32)      # deep ocean
RIVER_COLOR   = np.array([86, 150, 190], np.float32)    # bright river thread
LAKE_COLOR    = np.array([58, 112, 156], np.float32)    # flat lake blue
SNOW_TINT     = np.array([236, 240, 244], np.float32)
ROCK_TINT     = np.array([120, 112, 100], np.float32)   # bare grey-brown rock
BEACH_TINT    = np.array([224, 210, 168], np.float32)   # pale sand rim


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
           centerline=None, lake=None, width=None,
           island_ocean=False):
    """Return (rgb uint8, land_mask bool) for the given decimated mask block.

    island_ocean=True shifts ocean toward the same deep-blue ramp so island
    aprons blend into the mainland sea."""
    H, W = height.shape
    hi = height.astype(np.int32)
    land = hi > SEA_RAW
    mcy = _raw_to_mcy(height)

    rgb = np.zeros((H, W, 3), np.float32)

    # --- OCEAN: depth-shaded blue (deeper below sea = darker) ---
    # depth in MC blocks below Y63; clamp for a smooth shallow->deep ramp.
    depth = np.clip((63.0 - mcy) / 60.0, 0.0, 1.0)   # 0 at coast, 1 at ~ -MC-depth
    ocean_rgb = (OCEAN_SHALLOW[None, None, :] * (1 - depth[..., None]) +
                 OCEAN_DEEP[None, None, :] * depth[..., None])
    rgb[:] = ocean_rgb

    # --- LAND base = naturalistic biome tint ---
    if override is not None:
        ov = override
        for zone, name in OVERRIDE_BIOME_MAP.items():
            if not name:
                continue
            col = SAT_COLORS.get(name)
            if col is None:
                continue
            m = land & (ov == zone)
            if m.any():
                rgb[m] = col
        # land pixels with zone 0 / unmapped -> default green
        unset = land & ~np.isin(ov, [z for z, n in OVERRIDE_BIOME_MAP.items() if n and SAT_COLORS.get(n)])
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

    # --- HILLSHADE multiplied over everything (the "from space" seller) ---
    shade = _hillshade(mcy)
    # keep ocean fairly flat-lit; land gets the full relief
    sh_land = 0.55 + 0.75 * shade    # 0.55..1.30 -> some brightening on lit faces
    sh_ocean = 0.85 + 0.25 * shade
    sh = np.where(land, sh_land, sh_ocean)[..., None]
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
    for name in ("arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"):
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
    isl_rects = []   # (entry, safe, ox, oz, h, w)
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

    # mainland origin on canvas
    def w2c(wx, wz):
        return (wx - min_x) // stride, (wz - min_z) // stride

    canvas = np.zeros((ch, cw, 3), np.uint8)
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
        present_zones.update(int(z) for z in np.unique(m_over) if z in OVERRIDE_BIOME_MAP and OVERRIDE_BIOME_MAP[z])

    m_rgb, m_land = _paint(m_height, m_over, m_snow, m_rock, m_beach,
                           centerline=m_cl, lake=m_lake, width=m_wid)
    cx0, cz0 = w2c(0, 0)
    canvas[cz0:cz0 + m_rgb.shape[0], cx0:cx0 + m_rgb.shape[1]] = m_rgb
    log(f"[mainland] painted at canvas ({cx0},{cz0}) size {m_rgb.shape[1]}x{m_rgb.shape[0]}, land frac {m_land.mean():.3f}")
    del m_height, m_over, m_snow, m_rock, m_beach, m_cl, m_wid, m_lake, m_rgb

    # --- 3. Paint each island into its offset sub-rect (islands overwrite ocean) ---
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
        i_rgb, i_land = _paint(i_h, i_ov, i_sn, i_rk, i_bc,
                               centerline=None, lake=None, width=None,
                               island_ocean=True)
        if i_ov is not None:
            present_zones.update(int(z) for z in np.unique(i_ov) if z in OVERRIDE_BIOME_MAP and OVERRIDE_BIOME_MAP[z])

        # target rect on canvas
        tx0, tz0 = w2c(ox, oz)
        ih, iw = i_rgb.shape[:2]
        tx1, tz1 = tx0 + iw, tz0 + ih
        # clip against canvas bounds
        sx0 = max(0, -tx0); sz0 = max(0, -tz0)
        dx0 = max(0, tx0);  dz0 = max(0, tz0)
        dx1 = min(cw, tx1);  dz1 = min(ch, tz1)
        was_clipped = (tx0 < 0 or tz0 < 0 or tx1 > cw or tz1 > ch)
        if dx1 <= dx0 or dz1 <= dz0:
            log(f"[island] {safe}: FULLY off-canvas, skipped")
            continue
        sub = i_rgb[sz0:sz0 + (dz1 - dz0), sx0:sx0 + (dx1 - dx0)]
        sub_land = i_land[sz0:sz0 + (dz1 - dz0), sx0:sx0 + (dx1 - dx0)]
        # Overwrite ocean: paint island pixels that are LAND, plus its near-shore
        # ocean apron so the coast blends. Island ocean pixels blend 50/50 into the
        # existing mainland-sea canvas so aprons feather rather than hard-edge.
        dst = canvas[dz0:dz1, dx0:dx1]
        dst[sub_land] = sub[sub_land]
        ocean_px = ~sub_land
        dst[ocean_px] = (0.5 * dst[ocean_px].astype(np.float32) +
                         0.5 * sub[ocean_px].astype(np.float32)).astype(np.uint8)
        canvas[dz0:dz1, dx0:dx1] = dst
        landed.append((safe, ox, oz, iw, ih, was_clipped))
        (clipped if was_clipped else landed and None)
        log(f"[island] {safe}: painted at world({ox},{oz}) canvas({tx0},{tz0}) "
            f"size {iw}x{ih} land {i_land.mean():.3f}{' [CLIPPED]' if was_clipped else ''}")
        if was_clipped:
            clipped.append(safe)
        del i_h, i_ov, i_sn, i_rk, i_bc, i_rgb, i_land

    # --- 4. Build the two PNGs ---
    out_dir.mkdir(parents=True, exist_ok=True)
    base_img = Image.fromarray(canvas, "RGB")

    legend_zones = sorted(present_zones)
    clean = _compose_with_legend(base_img, legend_zones, min_x, min_z, max_x, max_z, stride)
    p_clean = out_dir / "space_map.png"
    clean.save(p_clean)

    grid_base = base_img.copy()
    _draw_grid(grid_base, min_x, min_z, max_x, max_z, stride)
    gridded = _compose_with_legend(grid_base, legend_zones, min_x, min_z, max_x, max_z,
                                   stride, grid=True)
    p_grid = out_dir / "space_map_grid.png"
    gridded.save(p_grid)

    # --- 5. Validate outputs are non-trivial ---
    for p, img in [(p_clean, clean), (p_grid, gridded)]:
        arr = np.asarray(img)
        sz = p.stat().st_size
        nonblack = float((arr.sum(axis=2) > 24).mean())
        log(f"[out] {p}  {img.size[0]}x{img.size[1]}  {sz/1024:.0f} KB  nonblack={nonblack:.3f}")
        if sz < 20_000 or nonblack < 0.05:
            log(f"[WARN] {p} looks trivial/blank!")

    log(f"[islands] landed {len(landed)}/{len(isl_rects)}"
        + (f", clipped: {', '.join(clipped)}" if clipped else ", none clipped"))
    return p_clean, p_grid, report


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

    # vertical lines at every tile boundary (world_x multiple of 512)
    tx_start = math.floor(min_x / TILE); tx_end = math.ceil(max_x / TILE)
    for tx in range(tx_start, tx_end + 1):
        wx = tx * TILE
        px = int((wx - min_x) / stride)
        if 0 <= px < W:
            major = (tx % label_every == 0)
            d.line([(px, 0), (px, H)], fill=line_major if major else line, width=1)
    tz_start = math.floor(min_z / TILE); tz_end = math.ceil(max_z / TILE)
    for tz in range(tz_start, tz_end + 1):
        wz = tz * TILE
        py = int((wz - min_z) / stride)
        if 0 <= py < H:
            major = (tz % label_every == 0)
            d.line([(0, py), (W, py)], fill=line_major if major else line, width=1)

    # labels at major intersections
    for tx in range(tx_start, tx_end + 1):
        if tx % label_every != 0:
            continue
        for tz in range(tz_start, tz_end + 1):
            if tz % label_every != 0:
                continue
            wx = tx * TILE; wz = tz * TILE
            px = int((wx - min_x) / stride); py = int((wz - min_z) / stride)
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

    # --- legend strip ---
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
    swatch(OCEAN_SHALLOW, "Ocean (shallow)")
    swatch(OCEAN_DEEP, "Ocean (deep)")
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
    p_clean, p_grid, _ = build(stride, out_dir)
    print("\n=== DONE ===")
    print("clean:", p_clean)
    print("grid :", p_grid)


if __name__ == "__main__":
    main()
