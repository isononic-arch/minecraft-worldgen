"""Save the painted+smoothed mask at 50k for tile (51,53) as an image,
to inspect whether the carve path is a wide smooth area or a thin
staircased line."""
import sys
from pathlib import Path
import numpy as np
from PIL import Image
import warnings
warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import tile_streamer
from core.hydro_region_overlay import apply_hydro_region_overlay

masks_dir = Path("C:/Users/nicho/minecraft-worldgen/masks")
TILE = 512
tx, tz = 51, 53

masks = tile_streamer.read_tile(
    masks_dir=masks_dir, col_off=tx*TILE, row_off=tz*TILE,
    width=TILE, height=TILE,
)
apply_hydro_region_overlay(masks, masks_dir, tx*TILE, tz*TILE, TILE)
cl = masks["hydro_centerline"] > 0
print(f"hydro_centerline > 0: {int(cl.sum())} cells")

# Also save the raw paint area at 50k via a fresh bilinear sample
from core import hydro_region_overlay as h
h._cache_path = None
h._ensure_caches(masks_dir / "hydro_region.png")

# Render the centerline mask as a 512x512 image
img = np.zeros((TILE, TILE, 3), dtype=np.uint8)
img[cl] = [255, 255, 80]  # yellow = centerline cells
img[~cl] = [40, 60, 30]   # dark green = non-centerline
Image.fromarray(img).save("memory/diag_carve_path_51_53.png")
print("Saved memory/diag_carve_path_51_53.png")

# Also render: raw painted cells (bilinear from 8k) WITHOUT smoothing
from scipy.ndimage import map_coordinates as _mc
raw_paint = (np.asarray(Image.open(masks_dir / "hydro_region.png").convert("L"))
             == 2).astype(np.float32)
scale_to_8k = 8192 / 50000
rows_f = (np.arange(TILE, dtype=np.float64) + tz*TILE) * scale_to_8k
cols_f = (np.arange(TILE, dtype=np.float64) + tx*TILE) * scale_to_8k
rg, cg = np.meshgrid(rows_f, cols_f, indexing="ij")
coords = np.stack([rg, cg])
raw_50k = _mc(raw_paint, coords, order=3, mode="constant", cval=0.0) > 0.5
print(f"raw paint bilinear @ 50k: {int(raw_50k.sum())} cells")

img2 = np.zeros((TILE, TILE, 3), dtype=np.uint8)
img2[raw_50k] = [80, 200, 255]   # cyan = raw painted (no smoothing)
img2[cl & ~raw_50k] = [255, 100, 100]  # red = added by smoothing/skeleton
img2[~(raw_50k | cl)] = [40, 60, 30]
Image.fromarray(img2).save("memory/diag_carve_path_51_53_compare.png")
print("Saved memory/diag_carve_path_51_53_compare.png (cyan=raw, red=added)")
