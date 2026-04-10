"""
tile_streamer.py — Step 4: Tile Streaming Engine
/core/tile_streamer.py

Reads a 512×512 window from each mask TIFF using rasterio Window() reads.
Never loads full rasters. Returns normalized float32 arrays per mask.

Mask files expected in masks_dir:
    height.tif    — terrain height (uint16, low value = HIGH terrain — polarity confirmed)
    slope.tif     — slope steepness (uint16 or float32)
    flow.tif      — flow accumulation / moisture proxy (uint16)
    erosion.tif   — erosion intensity (uint16)
    override.tif  — biome override zones (uint8, 8-bit grayscale)
    shore.tif     — shore proximity (uint8, 255=shore — polarity: high > 17050 = land)
    river.tif     — river channels (uint8 or uint16)

All uint16 inputs normalized to [0,1] by dividing by 65535.
All uint8 inputs normalized to [0,1] by dividing by 255.
Missing mask files return zero arrays (non-fatal).
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import numpy as np


# Expected mask filenames
MASK_NAMES = [
    "height", "slope", "flow", "erosion",
    "override", "shore", "river",
    "hydro_order", "hydro_width", "hydro_depth",
    "hydro_lake", "hydro_lkdep", "hydro_lake_wl",
    "hydro_centerline",
    "hydro_floodplain",
    "wind_windthrow",
    "rock_exposure",
    "rock_exposure_tight",
    "snow_caps",
    "sand_dunes",
]


def read_tile(
    masks_dir: Union[str, Path],
    col_off: int,
    row_off: int,
    width:   int = 512,
    height:  int = 512,
) -> dict[str, np.ndarray]:
    """
    Read one tile window from all mask TIFFs.

    Args:
        masks_dir:  Directory containing mask TIFFs.
        col_off:    X pixel offset (= tile_x * TILE_SIZE).
        row_off:    Y pixel offset (= tile_z * TILE_SIZE).
        width:      Tile width in pixels (default 512).
        height:     Tile height in pixels (default 512).

    Returns:
        Dict mapping mask name → (height, width) float32 ndarray in [0, 1].
        Missing masks return zero arrays of the correct shape.
    """
    try:
        import rasterio
        from rasterio.windows import Window
    except ImportError:
        raise ImportError("rasterio required: pip install rasterio")

    masks_dir = Path(masks_dir)
    result: dict[str, np.ndarray] = {}

    for name in MASK_NAMES:
        path = masks_dir / f"{name}.tif"
        if not path.exists():
            result[name] = np.zeros((height, width), dtype=np.float32)
            continue

        try:
            with rasterio.open(str(path)) as src:
                # override.tif is built by upscale_override_vectorized.py with
                # FLIP_Z=True and fliplr applied — it is already in the same
                # coordinate system as height.tif. Read it straight.
                # Clip window to raster bounds
                w = min(width,  src.width  - col_off)
                h = min(height, src.height - row_off)
                if w <= 0 or h <= 0:
                    result[name] = np.zeros((height, width), dtype=np.float32)
                    continue
                win = Window(col_off, row_off, w, h)
                raw = src.read(1, window=win)

                # Normalize based on dtype
                if raw.dtype == np.uint16:
                    tile = raw.astype(np.float32) / 65535.0
                elif raw.dtype == np.uint8:
                    tile = raw.astype(np.float32) / 255.0
                elif raw.dtype in (np.float32, np.float64):
                    tile = raw.astype(np.float32)
                    # Clamp to [0,1] if needed
                    lo, hi = float(tile.min()), float(tile.max())
                    if hi > 1.0 or lo < 0.0:
                        rng = hi - lo
                        tile = (tile - lo) / rng if rng > 0 else tile * 0
                else:
                    tile = raw.astype(np.float32) / float(np.iinfo(raw.dtype).max
                                                          if np.issubdtype(raw.dtype, np.integer)
                                                          else 1.0)

                # Pad to full tile size if at world edge
                if h < height or w < width:
                    padded = np.zeros((height, width), dtype=np.float32)
                    padded[:h, :w] = tile
                    tile = padded

                result[name] = tile

        except Exception as e:
            # Non-fatal: return zeros and continue
            result[name] = np.zeros((height, width), dtype=np.float32)

    return result


def read_single_mask(
    path:    Union[str, Path],
    col_off: int,
    row_off: int,
    width:   int = 512,
    height:  int = 512,
) -> np.ndarray:
    """
    Read a single mask TIFF window. Returns float32 [0,1] array.
    Convenience wrapper for when you only need one mask.
    """
    name = Path(path).stem
    masks_dir = Path(path).parent
    result = read_tile(masks_dir, col_off, row_off, width, height)
    # read_tile uses filenames, so we need to read directly
    try:
        import rasterio
        from rasterio.windows import Window
        with rasterio.open(str(path)) as src:
            w = min(width,  src.width  - col_off)
            h = min(height, src.height - row_off)
            if w <= 0 or h <= 0:
                return np.zeros((height, width), dtype=np.float32)
            win = Window(col_off, row_off, w, h)
            raw = src.read(1, window=win)
            if raw.dtype == np.uint16:
                tile = raw.astype(np.float32) / 65535.0
            elif raw.dtype == np.uint8:
                tile = raw.astype(np.float32) / 255.0
            else:
                tile = raw.astype(np.float32)
            if h < height or w < width:
                padded = np.zeros((height, width), dtype=np.float32)
                padded[:h, :w] = tile
                tile = padded
            return tile
    except Exception:
        return np.zeros((height, width), dtype=np.float32)


# ---------------------------------------------------------------------------
# SMOKE TEST
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, tempfile, os
    from pathlib import Path

    print("tile_streamer.py — smoke test")

    try:
        import rasterio
        from rasterio.transform import from_bounds
    except ImportError:
        print("  SKIP: rasterio not installed")
        sys.exit(0)

    # Create synthetic 1024×1024 TIFFs in a temp dir
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        rng = np.random.default_rng(42)

        # Write uint16 height
        data_u16 = (rng.random((1024, 1024)) * 65535).astype(np.uint16)
        with rasterio.open(
            str(tmpdir / "height.tif"), "w",
            driver="GTiff", height=1024, width=1024,
            count=1, dtype=np.uint16,
        ) as dst:
            dst.write(data_u16, 1)

        # Write uint8 override
        data_u8 = (rng.random((1024, 1024)) * 255).astype(np.uint8)
        with rasterio.open(
            str(tmpdir / "override.tif"), "w",
            driver="GTiff", height=1024, width=1024,
            count=1, dtype=np.uint8,
        ) as dst:
            dst.write(data_u8, 1)

        # Read a 512×512 tile from offset (0, 0)
        masks = read_tile(tmpdir, col_off=0, row_off=0, width=512, height=512)

        assert "height" in masks
        assert masks["height"].shape == (512, 512), f"Wrong shape: {masks['height'].shape}"
        assert masks["height"].dtype == np.float32
        assert 0.0 <= masks["height"].min() <= masks["height"].max() <= 1.0

        assert "override" in masks
        assert masks["override"].shape == (512, 512)

        # Missing mask should return zeros
        assert "slope" in masks
        assert masks["slope"].max() == 0.0, "Missing mask should be zeros"

        # Read tile at world edge (partial window)
        masks_edge = read_tile(tmpdir, col_off=768, row_off=768, width=512, height=512)
        assert masks_edge["height"].shape == (512, 512), "Edge tile wrong shape"

        # Verify normalization
        raw_val = float(data_u16[0, 0]) / 65535.0
        read_val = float(masks["height"][0, 0])
        assert abs(raw_val - read_val) < 1e-4, f"Normalization error: {raw_val} vs {read_val}"

    print(f"  Masks read:       {list(masks.keys())}")
    print(f"  height shape:     {masks['height'].shape}")
    print(f"  height range:     [{masks['height'].min():.3f}, {masks['height'].max():.3f}]")
    print(f"  missing→zeros:    slope.max()={masks['slope'].max()}")
    print(f"  edge tile shape:  {masks_edge['height'].shape}")
    print(f"  normalization:    OK")
    print("PASS")
    sys.exit(0)
