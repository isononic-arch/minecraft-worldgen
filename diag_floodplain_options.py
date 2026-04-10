"""
diag_floodplain_options.py — Render 3 floodplain width presets side by side.
Patches _FLOOD_BASE in eco_gradients at runtime, renders each, uploads to imgur.
"""
from __future__ import annotations
import json, sys, time, base64, urllib.request
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

MASKS_DIR   = Path(r"C:\Users\nicho\minecraft-worldgen\masks")
CONFIG_PATH = Path(r"C:\Users\nicho\minecraft-worldgen\config\thresholds.json")
OUTPUT_DIR  = Path(r"C:\Users\nicho\minecraft-worldgen\output")
TILE = 512
GRID_X, GRID_Z = 3, 3

GAP_COLOURS = {
    1: np.array([90, 200, 80]),
    2: np.array([220, 160, 50]),
    3: np.array([160, 120, 70]),
    4: np.array([180, 220, 60]),
}
WATER_RGB = np.array([70, 130, 210])
GAP_ALPHA = 0.55

PRESETS = {
    "A_moderate": {1: 25, 2: 50, 3: 80, 4: 120, 5: 160},
    "B_wide":     {1: 35, 2: 70, 3: 110, 4: 170, 5: 230},
    "C_dramatic": {1: 45, 2: 90, 3: 150, 4: 220, 5: 300},
}

def hillshade(h):
    gy, gx = np.gradient(h)
    az, alt = np.radians(315), np.radians(45)
    hyp = np.hypot(gx, gy)
    shade = (np.cos(alt) * np.cos(np.arctan(hyp)) +
             np.sin(alt) * (gx * np.sin(az) + gy * np.cos(az)) / np.maximum(hyp, 1e-6))
    grey = (np.clip((shade + 1) / 2, 0, 1) * 200 + 40).astype(np.uint8)
    return np.stack([grey, grey, grey], axis=-1)

def upload_imgur(path):
    b64 = base64.b64encode(path.read_bytes()).decode()
    boundary = 'b1234'
    body = f'--{boundary}\r\nContent-Disposition: form-data; name="image"\r\n\r\n{b64}\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request('https://api.imgur.com/3/image', data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}',
                 'Authorization': 'Client-ID 546c25a59c58ad7'})
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())['data']['link']

def render_preset(preset_name, flood_base, cx, cz, cfg):
    import core.eco_gradients as eco_mod
    from core.tile_streamer import read_tile
    from core.river_carver_v2 import carve_rivers, _height_norm_to_mc_y
    from core.eco_gradients import compute_eco_gradients
    from core.biome_assignment import assign_biomes

    tx0, tz0 = cx - GRID_X // 2, cz - GRID_Z // 2
    rh, rw = GRID_Z * TILE, GRID_X * TILE
    composite = np.zeros((rh, rw, 3), dtype=np.uint8)
    gap_counts = {1:0, 2:0, 3:0, 4:0}
    total_land = 0

    for gi in range(GRID_X):
        for gj in range(GRID_Z):
            tx, tz = tx0 + gi, tz0 + gj
            masks = read_tile(MASKS_DIR, tx*TILE, tz*TILE, TILE, TILE)
            surface_y = _height_norm_to_mc_y(masks["height"], cfg).astype(np.int16)
            cr = carve_rivers(surface_y=surface_y, flow_tile=masks["flow"],
                river_tile=masks["river"], cfg=cfg,
                hydro_order=masks.get("hydro_order"), hydro_width=masks.get("hydro_width"),
                hydro_depth=masks.get("hydro_depth"), hydro_lake=masks.get("hydro_lake"),
                hydro_lkdep=masks.get("hydro_lkdep"), hydro_lake_wl=masks.get("hydro_lake_wl"),
                hydro_centerline=masks.get("hydro_centerline"), height_norm=masks["height"],
                masks_dir=MASKS_DIR, tile_x=tx, tile_z=tz)
            sy_carved, rm = cr[0], cr[1]
            land = sy_carved >= 63
            bg = assign_biomes(masks["height"],
                masks.get("slope", np.zeros((TILE,TILE),dtype=np.float32)),
                masks["flow"], masks["erosion"], masks.get("override"),
                None, cfg, tile_x=tx, tile_y=tz)
            _gy, _gx = np.gradient(sy_carved.astype(np.float32))
            cliff_deg = np.degrees(np.arctan(np.hypot(_gx, _gy))).astype(np.float32)
            eco = compute_eco_gradients(
                surface_y=sy_carved, flow_f=masks["flow"], erosion_f=masks["erosion"],
                cliff_deg=cliff_deg,
                hydro_order=masks.get("hydro_order", np.zeros((TILE,TILE),dtype=np.float32)),
                hydro_width=masks.get("hydro_width", np.zeros((TILE,TILE),dtype=np.float32)),
                hydro_lake=masks.get("hydro_lake", np.zeros((TILE,TILE),dtype=np.float32)),
                land_mask=land, cfg=cfg, river_meta=rm,
                tile_x=tx, tile_z=tz, biome_grid=bg)

            tile_rgb = hillshade(masks["height"])
            gap = eco.gap_mask
            for gv, col in GAP_COLOURS.items():
                px = gap == gv
                if px.any():
                    tile_rgb[px] = (tile_rgb[px] * (1 - GAP_ALPHA) + col * GAP_ALPHA).astype(np.uint8)
                    gap_counts[gv] += px.sum()
            tile_rgb[rm > 0] = WATER_RGB
            total_land += land.sum()
            composite[gj*TILE:(gj+1)*TILE, gi*TILE:(gi+1)*TILE] = tile_rgb

    img = Image.fromarray(composite, "RGB")
    draw = ImageDraw.Draw(img)
    for i in range(GRID_X + 1):
        x = i * TILE
        if x < rw: draw.line([(x,0),(x,rh-1)], fill=(80,80,80), width=1)
    for j in range(GRID_Z + 1):
        y = j * TILE
        if y < rh: draw.line([(0,y),(rw-1,y)], fill=(80,80,80), width=1)
    # Title
    draw.text((10, 10), f"{preset_name}  S1={flood_base[1]} S2={flood_base[2]} S3={flood_base[3]} S4={flood_base[4]} S5={flood_base[5]}", fill=(255,255,255))
    fp_pct = gap_counts[4] * 100 / max(total_land, 1)
    draw.text((10, 26), f"Floodplain: {gap_counts[4]} px ({fp_pct:.1f}%)", fill=(180,220,60))

    out = OUTPUT_DIR / f"floodplain_{preset_name}.png"
    img.save(str(out))
    return out, gap_counts, total_land

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--cx", type=int, default=54)
    parser.add_argument("--cz", type=int, default=53)
    args = parser.parse_args()

    with open(CONFIG_PATH) as f:
        cfg = json.load(f)

    import core.eco_gradients as eco_mod
    OUTPUT_DIR.mkdir(exist_ok=True)
    urls = []

    for name, bases in PRESETS.items():
        t0 = time.perf_counter()
        print(f"\n=== {name}: {bases} ===")

        # Monkey-patch _FLOOD_BASE at the source level
        # The function reads it as a local var, so we inject via module globals
        original_compute = eco_mod.compute_eco_gradients
        def make_patched(fb):
            def patched(*a, **kw):
                # Temporarily replace the constant in the function's code
                import types
                old_code = original_compute.__code__
                # Can't easily patch locals, so we use a different approach:
                # Set a module-level override that the function checks
                eco_mod._FLOOD_BASE_OVERRIDE = fb
                result = original_compute(*a, **kw)
                eco_mod._FLOOD_BASE_OVERRIDE = None
                return result
            return patched

        # Simpler approach: just edit the source constant directly
        # Find and replace _FLOOD_BASE in the function's globals
        # Actually, _FLOOD_BASE is a local variable, so let's use a module-level override
        eco_mod._FLOOD_BASE_OVERRIDE = bases
        out_path, gc, tl = render_preset(name, bases, args.cx, args.cz, cfg)
        eco_mod._FLOOD_BASE_OVERRIDE = None

        elapsed = time.perf_counter() - t0
        fp_pct = gc[4] * 100 / max(tl, 1)
        print(f"  Floodplain: {gc[4]} px ({fp_pct:.1f}%), {elapsed:.0f}s")
        print(f"  Saved: {out_path}")

        try:
            url = upload_imgur(out_path)
            urls.append((name, url))
            print(f"  Imgur: {url}")
        except Exception as e:
            print(f"  Upload failed: {e}")
            urls.append((name, "FAILED"))

    print("\n=== ALL URLS ===")
    for name, url in urls:
        print(f"  {name}: {url}")

if __name__ == "__main__":
    main()
