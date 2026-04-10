"""
river_diagnostic.py — Render actual river carving result as top-down view.
"""
import sys, json
import numpy as np
from pathlib import Path
from PIL import Image
from scipy.ndimage import binary_dilation

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.tile_streamer import read_tile
from core import river_carver_v2 as rc2
from core import column_generator as col_gen

MASKS_DIR = ROOT / "masks"
CFG_PATH  = ROOT / "config" / "thresholds.json"

TILES = [(52, 53), (51, 53)]


def render_tile(tx, tz, cfg):
    """Render the actual pipeline carve result."""
    masks = read_tile(MASKS_DIR, tx * 512, tz * 512, 512, 512)
    height = masks["height"]
    flow   = masks["flow"]
    H, W = 512, 512

    # Build surface_y
    height_u16 = np.round(height * 65535.0).astype(np.uint16)
    surface_y = col_gen.generate_columns(
        height_tile=height_u16, slope_tile=masks["slope"],
        biome_grid=np.full((H, W), "MIXED_FOREST", dtype=object),
        shore_tile=masks["shore"], noise_fields={}, cfg=cfg,
        tile_x=tx, tile_y=tz,
    )

    # Carve rivers with flow-guided NMS
    surface_carved, river_meta = rc2.carve_rivers(
        surface_y=surface_y, flow_tile=flow,
        river_tile=masks["river"], cfg=cfg,
        hydro_order=masks.get("hydro_order"),
        hydro_width=masks.get("hydro_width"),
        hydro_depth=masks.get("hydro_depth"),
        hydro_lake=masks.get("hydro_lake"),
        hydro_lkdep=masks.get("hydro_lkdep"),
        hydro_lake_wl=masks.get("hydro_lake_wl"),
        height_norm=height,
        masks_dir=MASKS_DIR, tile_x=tx, tile_z=tz,
    )

    # Hillshade
    gy, gx = np.gradient(height)
    shade = np.clip(0.5 + gx * 5 - gy * 3, 0, 1)
    img = np.stack([shade, shade, shade], axis=-1)
    img = (img * 180 + 40).clip(0, 255).astype(np.uint8)

    # Water overlay
    stream = river_meta == rc2.CHAN_STREAM
    river  = river_meta == rc2.CHAN_RIVER
    lake   = river_meta == rc2.CHAN_LAKE

    img[stream] = [80, 140, 210]
    img[river]  = [40, 90, 220]
    img[lake]   = [30, 60, 180]

    # Raw NEAREST outline for comparison (thin red edge)
    order_u8 = np.round(masks["hydro_order"] * 255.0).astype(np.uint8)
    raw_cl = order_u8 > 0
    raw_edge = binary_dilation(raw_cl, iterations=1) & ~raw_cl
    # Only show raw edge where it DIFFERS from carved water
    water_mask = river_meta > 0
    raw_edge_visible = raw_edge & ~water_mask
    img[raw_edge_visible] = [255, 80, 80]

    return img, river_meta


def main():
    with open(CFG_PATH) as f:
        cfg = json.load(f)

    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)

    for tx, tz in TILES:
        print(f"Rendering tile ({tx},{tz})...")
        img, meta = render_tile(tx, tz, cfg)
        path = out_dir / f"river_result_{tx}_{tz}.png"
        Image.fromarray(img).save(path)
        print(f"  Saved {path}")
        for name, val in [("stream", 1), ("river", 2), ("lake", 3)]:
            print(f"  {name}: {(meta == val).sum()} px")


if __name__ == "__main__":
    main()
