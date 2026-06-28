"""topdown_fast.py — FAST color top-down of an island's WRITTEN .mca region files
(topmost solid block per column), auto-cropped, dark-ocean background.

Same output as topdown_mca.py but uses INTEGER palette indices throughout — no
object/string numpy arrays, no per-y-layer string compares in the inner loop.
Drops per-island time from ~30 min to a few seconds.

Usage: py islands/topdown_fast.py --name fogo [--maxpx 1400] [--out path.png]
"""
import sys, struct, zlib, gzip, io, json, time, argparse
from pathlib import Path
import numpy as np
import nbtlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.preview_renderer import BLOCK_COLORS

ISL = ROOT / "islands"
DEF = (120, 120, 120)
_AIR = ("minecraft:air", "minecraft:cave_air", "minecraft:void_air")
_AIR_SHORT = ("air", "cave_air", "void_air")

# ---------------------------------------------------------------------------
# LOCAL PALETTE OVERRIDE
# core.preview_renderer.BLOCK_COLORS only covers ~30 base surface blocks. The
# island worldgen tops most land columns with LEAF canopy (jungle/oak/birch/...)
# and ground foliage (short_grass/fern/bush/...) — none of which are in that LUT,
# so they all fell through to the gray DEF (120,120,120) and made every forested
# island read as solid gray. This map adds those (and a few other common surface
# blocks) so land renders in legible greens/browns/tans. Entries here WIN over
# BLOCK_COLORS; anything not covered still falls back to BLOCK_COLORS then DEF.
# ---------------------------------------------------------------------------
_LOCAL_PALETTE = {
    # --- tree canopy (leaves) — distinct green per species ---
    "jungle_leaves":     (0x2F, 0x6B, 0x1E),
    "oak_leaves":        (0x40, 0x77, 0x2A),
    "birch_leaves":      (0x6E, 0x8E, 0x3E),
    "spruce_leaves":     (0x33, 0x55, 0x33),
    "dark_oak_leaves":   (0x2A, 0x4F, 0x1F),
    "acacia_leaves":     (0x5E, 0x7C, 0x2E),
    "azalea_leaves":     (0x4A, 0x78, 0x34),
    "flowering_azalea_leaves": (0x6B, 0x86, 0x44),
    "mangrove_leaves":   (0x44, 0x72, 0x30),
    "cherry_leaves":     (0xD8, 0x9C, 0xC0),
    "pale_oak_leaves":   (0x7C, 0x8C, 0x66),
    # --- logs / wood (read as canopy gaps / trunks) ---
    "jungle_wood":       (0x55, 0x44, 0x28),
    "jungle_log":        (0x55, 0x44, 0x28),
    "oak_log":           (0x6B, 0x53, 0x32),
    "oak_wood":          (0x6B, 0x53, 0x32),
    "spruce_log":        (0x46, 0x33, 0x20),
    "birch_log":         (0xC8, 0xC2, 0xA8),
    "dark_oak_log":      (0x3A, 0x2C, 0x1A),
    # --- ground foliage / grass cover — greens, blend toward grass_block ---
    "short_grass":       (0x5C, 0x95, 0x3E),
    "tall_grass":        (0x54, 0x8E, 0x38),
    "fern":              (0x55, 0x88, 0x3C),
    "large_fern":        (0x4E, 0x80, 0x36),
    "bush":              (0x4F, 0x80, 0x36),
    "firefly_bush":      (0x4F, 0x80, 0x36),
    "leaf_litter":       (0x77, 0x66, 0x3A),
    "moss_carpet":       (0x52, 0x72, 0x3A),
    "pale_moss_carpet":  (0x84, 0x90, 0x6C),
    "vine":              (0x3E, 0x6E, 0x2E),
    "melon":             (0x4C, 0x8A, 0x3A),
    "pumpkin":           (0xC0, 0x77, 0x1E),
    "sugar_cane":        (0x7A, 0xB0, 0x55),
    "cactus":            (0x4E, 0x7A, 0x3A),
    "dead_bush":         (0x86, 0x6A, 0x3E),
    # --- dry / arid ground cover — tan-green ---
    "short_dry_grass":   (0xA8, 0xA0, 0x5A),
    "tall_dry_grass":    (0x9E, 0x96, 0x52),
    # --- soil variants ---
    "rooted_dirt":       (0x6E, 0x52, 0x38),
    "packed_mud":        (0x88, 0x66, 0x46),
    "mud_bricks":        (0x86, 0x66, 0x4A),
    "farmland":          (0x5A, 0x3E, 0x26),
    "dirt_path":         (0x8A, 0x70, 0x40),
    "grass_path":        (0x8A, 0x70, 0x40),
    # --- stone-family extras — keep each visually distinct ---
    "smooth_basalt":     (0x47, 0x46, 0x4C),
    "basalt":            (0x4C, 0x4A, 0x50),
    "blackstone":        (0x32, 0x2E, 0x36),
    "tuff":              (0x6E, 0x6C, 0x60),
    "deepslate":         (0x4A, 0x4A, 0x52),
    "cobbled_deepslate": (0x52, 0x52, 0x5A),
    "calcite":           (0xE0, 0xDE, 0xD8),
    "dripstone_block":   (0x9A, 0x84, 0x6C),
    "terracotta":        (0x96, 0x5A, 0x3E),
    # --- litho washes (concrete powder used as surface paint) ---
    "light_gray_concrete_powder": (0x9A, 0x9A, 0x96),
    "gray_concrete_powder":       (0x6A, 0x6A, 0x68),
    "green_concrete_powder":      (0x4E, 0x6E, 0x3A),
    "brown_concrete_powder":      (0x76, 0x56, 0x3A),
    "white_concrete_powder":      (0xD8, 0xD8, 0xD2),
    # --- underwater plants — read as shallow water, not gray ---
    "seagrass":          (0x3C, 0x76, 0xB0),
    "tall_seagrass":     (0x3C, 0x76, 0xB0),
    "kelp":              (0x34, 0x6E, 0x9E),
    "kelp_plant":        (0x34, 0x6E, 0x9E),
    "lily_pad":          (0x3E, 0x72, 0x4E),
    # --- ice / snow extras ---
    "powder_snow":       (0xF2, 0xF6, 0xFA),
    "blue_ice":          (0xB4, 0xD4, 0xF4),
    "frosted_ice":       (0xC8, 0xE4, 0xFF),
}


def _color_for(short):
    """Resolve a block short-name to an RGB: local override → BLOCK_COLORS → DEF."""
    c = _LOCAL_PALETTE.get(short)
    if c is not None:
        return c
    return BLOCK_COLORS.get(short, DEF)


def _safe(n):
    import re
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


def read_chunk(f, lx, lz):
    """Parse one chunk from an open region file. Reused verbatim from topdown_mca."""
    idx = (lz % 32) * 32 + (lx % 32)
    f.seek(idx * 4); loc = f.read(4)
    off = (loc[0] << 16) | (loc[1] << 8) | loc[2]; secs = loc[3]
    if off == 0 or secs == 0:
        return None
    f.seek(off * 4096); length = struct.unpack(">I", f.read(4))[0]
    ct = f.read(1)[0]; data = f.read(length - 1)
    raw = gzip.decompress(data) if ct == 1 else zlib.decompress(data) if ct == 2 else data
    return nbtlib.File.parse(io.BytesIO(raw))


def _section_idx(sec):
    """Return (idx, names) where idx is an int16 (16,16,16) (y,z,x) array of palette
    ids, and names is the section's palette name list. Returns None if no block data.
    Single-palette sections get a constant-filled idx (all zeros). NEVER builds a
    name/object array — that's the slow path we are replacing."""
    bs = sec.get("block_states")
    if bs is None:
        return None
    pal = bs.get("palette")
    if pal is None:
        return None
    names = [str(p.get("Name", "minecraft:air")) for p in pal]
    if len(names) == 1:
        return np.zeros((16, 16, 16), dtype=np.int16), names
    data = bs.get("data")
    if data is None:
        return np.zeros((16, 16, 16), dtype=np.int16), names
    bits = max(4, (len(names) - 1).bit_length())
    per = 64 // bits
    mask = np.uint64((1 << bits) - 1)
    longs = np.asarray(data, dtype=np.uint64)            # 1.18+ padded long array
    shifts = (np.arange(per, dtype=np.uint64) * np.uint64(bits))
    idx = ((longs[:, None] >> shifts[None, :]) & mask).reshape(-1)[:4096]
    idx = np.clip(idx.astype(np.int64), 0, len(names) - 1).astype(np.int16)
    return idx.reshape(16, 16, 16), names   # (y,z,x)


def top_rgb_for_chunk(chunk):
    """Return (16,16,3) uint8 top-block RGB indexed [z,x], (16,16) bool 'has', and
    (16,16) int16 top-block world-Y (for hillshade; -32768 where nothing found).
    All-integer/boolean scan: highest Y section to lowest, yy=15..0 within a section."""
    secs = chunk.get("sections") or chunk.get("Sections") or []
    parsed = []   # (Y, idx, pal_rgb, is_air)
    for s in secs:
        r = _section_idx(s)
        if r is None:
            continue
        idx, names = r
        n = len(names)
        pal_rgb = np.empty((n, 3), np.uint8)
        is_air = np.zeros(n, bool)
        for i, nm in enumerate(names):
            short = nm.replace("minecraft:", "")
            pal_rgb[i] = _color_for(short)
            if nm in _AIR or short in _AIR_SHORT:
                is_air[i] = True
        parsed.append((int(s.get("Y", 0)), idx, pal_rgb, is_air))

    top_rgb = np.zeros((16, 16, 3), np.uint8)
    found = np.zeros((16, 16), bool)
    top_y = np.full((16, 16), -32768, np.int16)
    if not parsed:
        return top_rgb, found, top_y
    parsed.sort(key=lambda t: t[0], reverse=True)   # high Y first
    for secy, idx, pal_rgb, is_air in parsed:
        # quick skip: section entirely air
        if is_air.all():
            continue
        for yy in range(15, -1, -1):
            layer = idx[yy]                          # (z,x) int16
            newly = (~found) & (~is_air[layer])      # bool (z,x)
            if newly.any():
                top_rgb[newly] = pal_rgb[layer][newly]
                top_y[newly] = secy * 16 + yy
                found |= newly
                if found.all():
                    return top_rgb, found, top_y
    return top_rgb, found, top_y


def _apply_hillshade(img, elev):
    """Multiply land pixels by a subtle NW-sun hillshade derived from the top-block
    Y array. Ocean/void (elev==NaN) is left untouched. Cheap Sobel-on-elevation;
    relief is gentle (0.78..1.12) so colors stay legible, not blown out."""
    land = np.isfinite(elev)
    if not land.any():
        return img
    e = np.where(land, elev, np.nan).astype(np.float32)
    # fill NaN with local mean so gradients at the coast don't explode
    filled = e.copy()
    m = np.nanmean(e)
    filled[~land] = m
    pad = np.pad(filled, 1, mode="edge")
    gx = (pad[1:-1, 2:] - pad[1:-1, :-2]) * 0.5
    gy = (pad[2:, 1:-1] - pad[:-2, 1:-1]) * 0.5
    # NW sun: shade = dot(normal, sun); exaggerate modestly
    k = 2.2
    gx *= k; gy *= k
    nlen = np.sqrt(gx * gx + gy * gy + 1.0)
    nx = -gx / nlen; ny = -gy / nlen; nz = 1.0 / nlen
    # sun from NW, ~45° altitude
    sx, sy, sz = -0.5, -0.5, 0.707
    hs = np.clip(nx * sx + ny * sy + nz * sz, 0.0, 1.0)
    factor = (0.78 + 0.34 * hs).astype(np.float32)   # [0.78, 1.12]
    factor[~land] = 1.0
    out = img.astype(np.float32) * factor[:, :, None]
    return np.clip(out, 0, 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--maxpx", type=int, default=1400)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    t0 = time.time()

    layout = json.loads((ISL / "layout.json").read_text())
    entry = next(i for i in layout["islands"]
                 if a.name in _safe(i["name"]) or a.name in i["dem_path"])
    name = _safe(entry["name"])
    odir = ISL / "out" / name
    man = json.loads((ISL / "masks_islands" / name / "manifest.json").read_text())
    ox, oz = man["world_offset_px"]; wh, ww = man["world_hw"]
    x0, z0 = ox, oz; x1, z1 = ox + ww, oz + wh
    step = max(1, max(ww, wh) // a.maxpx)

    H = (z1 - z0) // step + 1
    W = (x1 - x0) // step + 1
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = (18, 28, 46)
    elev = np.full((H, W), np.nan, np.float32)   # top-block Y per output px (for hillshade)

    nfiles = 0
    for mca in sorted(odir.glob("r.*.mca")):
        rx, rz = map(int, mca.stem.split(".")[1:3])
        with open(mca, "rb") as f:
            for lz in range(32):
                for lx in range(32):
                    try:
                        ch = read_chunk(f, lx, lz)
                    except Exception:
                        ch = None
                    if ch is None:
                        continue
                    top_rgb, has, top_y = top_rgb_for_chunk(ch)
                    cwx = (rx * 32 + lx) * 16; cwz = (rz * 32 + lz) * 16
                    for zz in range(0, 16, step):
                        for xx in range(0, 16, step):
                            if not has[zz, xx]:
                                continue
                            wx = cwx + xx; wz = cwz + zz
                            if not (x0 <= wx < x1 and z0 <= wz < z1):
                                continue
                            oy = (wz - z0) // step; ox2 = (wx - x0) // step
                            img[oy, ox2] = top_rgb[zz, xx]
                            elev[oy, ox2] = top_y[zz, xx]
        nfiles += 1

    # --- subtle hillshade (cheap, NW sun) — only on land (elev present) ---
    img = _apply_hillshade(img, elev)

    from PIL import Image
    # auto-crop to rendered content (trim empty ocean border)
    bg = np.array([18, 28, 46], np.uint8)
    content = np.any(img != bg, axis=2)
    if content.any():
        ys, xs = np.where(content)
        m = 12
        y0c, y1c = max(0, ys.min() - m), min(img.shape[0], ys.max() + m)
        x0c, x1c = max(0, xs.min() - m), min(img.shape[1], xs.max() + m)
        img = img[y0c:y1c, x0c:x1c]

    out = Path(a.out) if a.out else (ISL / "_val" / f"td_{name}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out)
    secs = time.time() - t0
    print(f"saved {out} ({nfiles} regions, {img.shape[1]}x{img.shape[0]}, {secs:.1f}s)", flush=True)


if __name__ == "__main__":
    main()
