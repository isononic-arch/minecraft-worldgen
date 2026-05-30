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
    "rock_gap",
    "snow_gap",
    # S89 physics snow A/B (built by tools/build_snow_physics.py)
    "snow_gap_physics",
    "sand_dunes",
    "beach",
    # S88 terrain-derived masks (built by tools/build_terrain_derived.py)
    "aspect",
    "cliff_cap",
    "talus_apron",
    "bedrock_drainage",
    # S89 rock_layers tier mask (0..3; built by build_terrain_derived.py --only rock_layers)
    "rock_layers",
    # S88 walk #10/11: vein_field + varnish_field masks
    # (built by tools/build_vein_and_cap_masks.py)
    "vein_field",
    "varnish_field",
    # S88 walk #11: joint_pattern (basaltic columnar joints) +
    # insolation_index (aspect+slope sun exposure) +
    # concavity_field (baked laplacian depression mask)
    "joint_pattern",
    "insolation_index",
    "concavity_field",
]


def read_tile(
    masks_dir: Union[str, Path],
    col_off: int,
    row_off: int,
    width:   int = 512,
    height:  int = 512,
    pad_px:  int = 0,
    mask_subset: Union[list, tuple, set, None] = None,
    gap_config: Union[dict, None] = None,
) -> dict[str, np.ndarray]:
    """
    Read one tile window from all mask TIFFs.

    Args:
        masks_dir:     Directory containing mask TIFFs.
        col_off:       X pixel offset (= tile_x * TILE_SIZE).
        row_off:       Y pixel offset (= tile_z * TILE_SIZE).
        width:         Inner tile width in pixels (default 512).
        height:        Inner tile height in pixels (default 512).
        pad_px:        Halo width in pixels to read around the tile (default 0).
                       When > 0, the returned arrays are
                       (height + 2*pad_px, width + 2*pad_px).
                       Out-of-world pixels (negative coords or past the raster
                       edge) are zero-filled. Phase 3b cross-tile ecotone (S58).
        mask_subset:   Optional iterable of mask names to read. When provided,
                       only these masks are read (others are omitted from the
                       result). When None, all MASK_NAMES are read. Useful
                       together with pad_px=48 to cheaply fetch only the 5
                       assign_biomes inputs at padded shape.

    Returns:
        Dict mapping mask name → float32 ndarray in [0, 1].
        When pad_px == 0: shape (height, width).
        When pad_px > 0:  shape (height + 2*pad_px, width + 2*pad_px).
        Missing masks return zero arrays of the correct shape.
    """
    try:
        import rasterio
        from rasterio.windows import Window
    except ImportError:
        raise ImportError("rasterio required: pip install rasterio")

    masks_dir = Path(masks_dir)
    result: dict[str, np.ndarray] = {}

    # Effective window: inner tile expanded by pad_px on all sides.
    eff_col_off = col_off - pad_px
    eff_row_off = row_off - pad_px
    eff_w = width  + 2 * pad_px
    eff_h = height + 2 * pad_px

    names = list(mask_subset) if mask_subset is not None else MASK_NAMES

    # S60 query-time gap sampler: for any mask name present in `gap_config`,
    # compute the mask live by sampling its 8k Gaea source via Catmull-Rom
    # instead of reading the 50k TIF. gap_config is produced by
    # core.gaea_gap_sampler.build_gap_config from thresholds.json.
    sampler_kwargs = gap_config or {}

    for name in names:
        if name in sampler_kwargs:
            try:
                from core.gaea_gap_sampler import sample_gap_at_tile
                result[name] = sample_gap_at_tile(
                    col_off=col_off, row_off=row_off,
                    width=width, height=height, pad_px=pad_px,
                    **sampler_kwargs[name],
                )
                continue
            except Exception:
                # Fall through to TIF read on any sampler failure
                pass

        path = masks_dir / f"{name}.tif"
        if not path.exists():
            result[name] = np.zeros((eff_h, eff_w), dtype=np.float32)
            continue

        try:
            with rasterio.open(str(path)) as src:
                # override.tif is built by upscale_override_vectorized.py with
                # FLIP_Z=True and fliplr applied — it is already in the same
                # coordinate system as height.tif. Read it straight.

                # Compute source-space intersection with effective window.
                # The effective window spans [eff_col_off, eff_col_off+eff_w)
                # in source coords. Clip to source bounds; anything outside
                # becomes zero-fill in the output array.
                src_col_start = max(0, eff_col_off)
                src_row_start = max(0, eff_row_off)
                src_col_end   = min(src.width,  eff_col_off + eff_w)
                src_row_end   = min(src.height, eff_row_off + eff_h)

                w_read = src_col_end - src_col_start
                h_read = src_row_end - src_row_start

                if w_read <= 0 or h_read <= 0:
                    # Window entirely outside raster — all zeros.
                    result[name] = np.zeros((eff_h, eff_w), dtype=np.float32)
                    continue

                # Destination offset inside the output array.
                dst_col_start = src_col_start - eff_col_off
                dst_row_start = src_row_start - eff_row_off

                win = Window(src_col_start, src_row_start, w_read, h_read)
                raw = src.read(1, window=win)

                # Normalize based on dtype
                if raw.dtype == np.uint16:
                    tile_read = raw.astype(np.float32) / 65535.0
                elif raw.dtype == np.uint8:
                    tile_read = raw.astype(np.float32) / 255.0
                elif raw.dtype in (np.float32, np.float64):
                    tile_read = raw.astype(np.float32)
                    # Clamp to [0,1] if needed
                    lo, hi = float(tile_read.min()), float(tile_read.max())
                    if hi > 1.0 or lo < 0.0:
                        rng_ = hi - lo
                        tile_read = (tile_read - lo) / rng_ if rng_ > 0 else tile_read * 0
                else:
                    tile_read = raw.astype(np.float32) / float(
                        np.iinfo(raw.dtype).max
                        if np.issubdtype(raw.dtype, np.integer)
                        else 1.0
                    )

                # Place the read data into the correctly-sized output array,
                # zero-filling any out-of-world strip.
                out = np.zeros((eff_h, eff_w), dtype=np.float32)
                out[dst_row_start:dst_row_start + h_read,
                    dst_col_start:dst_col_start + w_read] = tile_read
                result[name] = out

        except Exception:
            # Non-fatal: return zeros and continue
            result[name] = np.zeros((eff_h, eff_w), dtype=np.float32)

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


def read_discrete_tile(
    path:    Union[str, Path],
    col_off: int,
    row_off: int,
    width:   int = 512,
    height:  int = 512,
) -> np.ndarray | None:
    """
    Read a discrete (uint8/uint16) mask tile WITHOUT float normalisation.

    Returns the raw integer array (zero-padded at world edges), or None if the
    file does not exist.  Used for masks like ``lithology.tif`` where pixel
    values are group IDs (1-6), not continuous gradients.
    """
    path = Path(path)
    if not path.exists():
        return None

    try:
        import rasterio
        from rasterio.windows import Window

        with rasterio.open(str(path)) as src:
            w = min(width,  src.width  - col_off)
            h = min(height, src.height - row_off)
            if w <= 0 or h <= 0:
                return np.zeros((height, width), dtype=np.uint8)
            win = Window(col_off, row_off, w, h)
            raw = src.read(1, window=win)

            if h < height or w < width:
                padded = np.zeros((height, width), dtype=raw.dtype)
                padded[:h, :w] = raw
                raw = padded

            return raw
    except Exception:
        return None


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
