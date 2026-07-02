"""world_map_baked.py — composite MAINLAND + all baked ISLANDS at true world
coords, and draw a RED border around each island's *baked* region-tile footprint
(land + the ocean shelf-buffer that actually gets written), so overlaps on Vandir
are visible at a glance.

The baked footprint is recomputed EXACTLY the way the renderer picks tiles
(render_drive._content_tiles, buffer_tiles=1, world offset snapped to 512), so it
matches what gets written to disk regardless of local render state. One 512-block
tile == one MC region file, so overlap is checked exactly at region-tile
granularity.

Outputs islands/out/world_map_baked.png + prints an overlap report.
"""
import sys, json, glob, math, re
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from scipy.ndimage import binary_erosion, binary_dilation
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.biome_assignment import OVERRIDE_BIOME_MAP
from tools.world_biome_map import BIOME_COLORS

TILE = 512
SEA_RAW = 17050
LAND_THR = SEA_RAW + 40            # render_drive._content_tiles land test
TARGET = 2600                      # max canvas dim (px)
OCEAN = (16, 26, 44)
RED = np.array([235, 40, 40], np.uint8)
OVL = np.array([255, 230, 0], np.uint8)   # overlap = bright yellow
MAINLAND_NX = math.ceil(50000 / TILE)     # 98 region tiles per axis


def _snap(v):
    return int(round(v / TILE) * TILE)


def _safe(name):
    import re
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def baked_tiles(mdir: Path, world_w: int, world_h: int, off_x: int, off_z: int,
                buffer_tiles: int = 1, apron_seed_min_px: int = 0):
    """Return the set of WORLD region tiles (rx, rz) this island bakes.

    Exact mirror of render_drive._content_tiles: land = height>SEA_RAW+40 in the
    full-res 512 window, dilated by a buffer ring, then shifted to the snapped
    world offset. `apron_seed_min_px` mirrors the renderer's per-island sliver trim:
    every land tile is kept, but only tiles with >= that many land pixels seed the
    apron dilation (0 = off = legacy)."""
    from rasterio.windows import Window
    ntx = math.ceil(world_w / TILE)
    nty = math.ceil(world_h / TILE)
    land = np.zeros((nty, ntx), bool)
    seed = np.zeros((nty, ntx), bool)
    with rasterio.open(str(mdir / "height.tif")) as src:
        for ty in range(nty):
            for tx in range(ntx):
                w = min(TILE, world_w - tx * TILE)
                h = min(TILE, world_h - ty * TILE)
                if w <= 0 or h <= 0:
                    continue
                a = src.read(1, window=Window(tx * TILE, ty * TILE, w, h))
                if a.size:
                    npx = int((a > LAND_THR).sum())
                    if npx > 0:
                        land[ty, tx] = True
                        if npx >= apron_seed_min_px:
                            seed[ty, tx] = True
    if buffer_tiles > 0:
        # S101b: square (Chebyshev) structure to actually mirror render_drive's
        # dx,dy double loop — the default cross missed buffer-ring CORNER tiles.
        # + fill enclosed holes (atoll lagoons render, same S101b rule as
        # render_drive._content_tiles / render_islands._footprint_tiles_from_height).
        from scipy.ndimage import binary_fill_holes as _bfh
        k = 2 * int(buffer_tiles) + 1
        kept = _bfh(land | binary_dilation(seed, structure=np.ones((k, k), bool)))
    else:
        kept = land
    rx0 = _snap(off_x) // TILE
    ry0 = _snap(off_z) // TILE
    out = set()
    ys, xs = np.where(kept)
    for ty, tx in zip(ys.tolist(), xs.tolist()):
        out.add((rx0 + tx, ry0 + ty))
    return out


def main():
    layout = json.loads((ROOT / "islands" / "layout.json").read_text())
    by_name = {_safe(i["name"]): i for i in layout["islands"]}

    islands = []   # dict(name, label, mdir, off, world_hw, tiles)
    for m in sorted(glob.glob(str(ROOT / "islands" / "masks_islands" / "*" / "manifest.json"))):
        d = Path(m).parent
        if not (d / "override.tif").exists() or not (d / "height.tif").exists():
            continue
        man = json.loads(Path(m).read_text())
        nm = d.name
        if nm in ("svg_validation", "svg_seabed"):
            continue
        ox, oz = man["world_offset_px"]
        H, W = man["world_hw"]
        seed_min = int(by_name.get(nm, {}).get("apron_seed_min_px", 0))
        tiles = baked_tiles(d, W, H, int(ox), int(oz), apron_seed_min_px=seed_min)
        # union the actual on-disk rendered region tiles ("what is baked"); these
        # are a touch wider than the buffer=1 recompute (partial 2nd ring).
        odir = ROOT / "islands" / "out" / nm
        n_disk = 0
        for f in glob.glob(str(odir / "r.*.mca")):
            mm = re.search(r"r\.(-?\d+)\.(-?\d+)\.mca$", Path(f).name)
            if mm:
                tiles.add((int(mm.group(1)), int(mm.group(2)))); n_disk += 1
        lbl = man.get("name", nm).split("(")[0].strip()
        islands.append(dict(name=nm, label=lbl, mdir=d, off=(ox, oz),
                            whw=(H, W), tiles=tiles))
        print(f"  {lbl:34s} offset=({ox:>6},{oz:>6})  baked tiles={len(tiles):4d} (on-disk {n_disk})")

    # ---- world bounds (mainland box + every baked tile) ----
    minx = 0; maxx = MAINLAND_NX * TILE
    minz = 0; maxz = MAINLAND_NX * TILE
    for isl in islands:
        for (rx, rz) in isl["tiles"]:
            minx = min(minx, rx * TILE);     maxx = max(maxx, (rx + 1) * TILE)
            minz = min(minz, rz * TILE);     maxz = max(maxz, (rz + 1) * TILE)
    minx -= TILE; minz -= TILE; maxx += TILE; maxz += TILE
    scale = max(maxx - minx, maxz - minz) / TARGET
    cw = int((maxx - minx) / scale) + 1
    ch = int((maxz - minz) / scale) + 1
    print(f"\nworld bounds X[{minx},{maxx}] Z[{minz},{maxz}]  scale 1:{scale:.0f}  canvas {cw}x{ch}")

    def cx(x): return int((x - minx) / scale)
    def cz(z): return int((z - minz) / scale)

    canvas = np.full((ch, cw, 3), OCEAN, np.uint8)
    lut = np.zeros((256, 3), np.uint8)
    for z, n in OVERRIDE_BIOME_MAP.items():
        lut[z] = BIOME_COLORS.get(n, (150, 150, 150))

    # ---- paint mainland biomes + build a 98x98 land-tile grid ----
    ml = ROOT / "masks" / "override.tif"
    ml_land = np.zeros((MAINLAND_NX, MAINLAND_NX), bool)
    if ml.exists():
        dw = max(1, int(50000 / scale)); dh = dw
        ov = rasterio.open(str(ml)).read(1, out_shape=(dh, dw), resampling=Resampling.nearest)
        sub = lut[ov]; mask = ov > 0
        px0, pz0 = cx(0), cz(0)
        reg = canvas[pz0:pz0 + dh, px0:px0 + dw]
        h2, w2 = reg.shape[:2]
        reg[mask[:h2, :w2]] = sub[:h2, :w2][mask[:h2, :w2]]
        # land grid: dedicated tile-res max read
        K = MAINLAND_NX
        g = rasterio.open(str(ml)).read(1, out_shape=(K * 8, K * 8), resampling=Resampling.nearest)
        g = g[:K * 8, :K * 8].reshape(K, 8, K, 8)
        ml_land = (g.max(axis=(1, 3)) > 0)
    # ---- paint island biomes (land only) ----
    for isl in islands:
        H, W = isl["whw"]; ox, oz = isl["off"]
        sx, sz = _snap(ox), _snap(oz)
        dw = max(1, int(W / scale)); dh = max(1, int(H / scale))
        try:
            ov = rasterio.open(str(isl["mdir"] / "override.tif")).read(
                1, out_shape=(dh, dw), resampling=Resampling.nearest)
        except Exception as e:
            print(f"  skip paint {isl['label']}: {e}"); continue
        sub = lut[ov]; mask = (ov > 0) & (ov != 254)   # 254 = island ocean sentinel
        px0, pz0 = cx(sx), cz(sz)
        ez, ex = min(pz0 + dh, ch), min(px0 + dw, cw)
        if ez <= pz0 or ex <= px0:
            continue
        reg = canvas[pz0:ez, px0:ex]
        m2 = mask[:ez - pz0, :ex - px0]
        reg[m2] = sub[:ez - pz0, :ex - px0][m2]

    # ---- detect overlaps (island-vs-island + island-vs-mainland-land) ----
    tile_owners = {}
    for isl in islands:
        for t in isl["tiles"]:
            tile_owners.setdefault(t, []).append(isl["label"])
    ii_overlap = {t: o for t, o in tile_owners.items() if len(o) > 1}
    ml_clobber = {}   # island label -> list of mainland-land tiles it overwrites
    for isl in islands:
        for (rx, rz) in isl["tiles"]:
            if 0 <= rx < MAINLAND_NX and 0 <= rz < MAINLAND_NX and ml_land[rz, rx]:
                ml_clobber.setdefault(isl["label"], []).append((rx, rz))

    # ---- draw red baked-footprint fill + outline per island ----
    overlap_canvas_mask = np.zeros((ch, cw), bool)
    for t in ii_overlap:
        rx, rz = t
        overlap_canvas_mask[cz(rz * TILE):cz((rz + 1) * TILE),
                            cx(rx * TILE):cx((rx + 1) * TILE)] = True

    for isl in islands:
        tm = np.zeros((ch, cw), bool)
        for (rx, rz) in isl["tiles"]:
            tm[cz(rz * TILE):cz((rz + 1) * TILE),
               cx(rx * TILE):cx((rx + 1) * TILE)] = True
        # faint red fill over baked tiles (land + ocean apron)
        fill = tm & ~overlap_canvas_mask
        canvas[fill] = (canvas[fill].astype(np.int16) * 0.72 + RED * 0.28).astype(np.uint8)
        # solid red outline of the union boundary
        outline = tm & ~binary_erosion(tm, iterations=1)
        outline = binary_dilation(outline, iterations=1)
        canvas[outline] = RED

    # overlap tiles painted solid yellow on top
    if overlap_canvas_mask.any():
        canvas[overlap_canvas_mask] = (
            canvas[overlap_canvas_mask].astype(np.int16) * 0.25 + OVL * 0.75).astype(np.uint8)

    img = Image.fromarray(canvas)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 15)
        tfont = ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", 20)
    except Exception:
        font = ImageFont.load_default(); tfont = font

    # mainland box outline
    draw.rectangle([cx(0), cz(0), cx(50000) - 1, cz(50000) - 1],
                   outline=(210, 210, 210), width=2)
    draw.text((cx(0) + 6, cz(0) + 6), "VANDIR MAINLAND (0,0)-(50000,50000)",
              fill=(230, 230, 230), font=font)

    def txt(x, z, s, col=(255, 255, 255)):
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.text((x + dx, z + dy), s, fill=(0, 0, 0), font=font)
        draw.text((x, z), s, fill=col, font=font)

    for isl in islands:
        if not isl["tiles"]:
            continue
        rxs = [t[0] for t in isl["tiles"]]; rzs = [t[1] for t in isl["tiles"]]
        ctx = (min(rxs) + max(rxs) + 1) / 2 * TILE
        ctz = (min(rzs) + max(rzs) + 1) / 2 * TILE
        clob = " [!ML]" if isl["label"] in ml_clobber else ""
        txt(cx(ctx) + 3, cz(ctz) - 8, f"{isl['label']}{clob}",
            col=(255, 235, 120) if clob else (255, 255, 255))

    # title + legend
    draw.text((10, ch - 70), "Vandir — mainland + baked island footprints",
              fill=(255, 255, 255), font=tfont)
    draw.rectangle([12, ch - 40, 30, ch - 28], fill=tuple(int(v) for v in RED))
    draw.text((36, ch - 42), "red = baked tiles (land + ocean apron written to disk)",
              fill=(230, 230, 230), font=font)
    draw.rectangle([12, ch - 20, 30, ch - 8], fill=tuple(int(v) for v in OVL))
    draw.text((36, ch - 22), "yellow = OVERLAP (two islands claim same region tile)",
              fill=(230, 230, 230), font=font)

    out = ROOT / "islands" / "out" / "world_map_baked.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)

    # ---- report ----
    print("\n" + "=" * 64)
    print("OVERLAP REPORT")
    print("=" * 64)
    if ii_overlap:
        print(f"\n!! ISLAND-vs-ISLAND overlap: {len(ii_overlap)} region tile(s)")
        pairs = {}
        for t, owners in ii_overlap.items():
            key = " + ".join(sorted(set(owners)))
            pairs.setdefault(key, []).append(t)
        for key, ts in sorted(pairs.items()):
            print(f"   {key}: {len(ts)} tile(s)  e.g. {sorted(ts)[:4]}")
    else:
        print("\nOK  No island-vs-island region-tile overlap.")
    if ml_clobber:
        print(f"\n!! ISLAND-over-MAINLAND-LAND: {len(ml_clobber)} island(s) write onto mainland land tiles")
        for lbl, ts in sorted(ml_clobber.items()):
            print(f"   {lbl}: {len(ts)} mainland land tile(s)  e.g. {sorted(ts)[:4]}")
    else:
        print("OK  No island writes onto mainland land (ocean placements only).")
    print(f"\nsaved {out}  ({cw}x{ch})")


if __name__ == "__main__":
    main()
