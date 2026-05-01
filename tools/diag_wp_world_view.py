"""
diag_wp_world_view.py - In-game-style overview of WP findPath rivers.

Reads the pickle written by diag_wp_pathfind.py, applies WP-style widths +
mouth extensions, and renders a side-by-side comparison:
    LEFT  — current river system (hydro_centerline.tif + hydro_width.tif)
    RIGHT — proposed WP findPath system (centerlines + WP-linear widths)

Output: memory/wp_pathfind_world_view.png

Visual style is a topographic in-game-ish view: terrain colormap with
hillshade, lakes cyan, rivers dark blue, sand-dune-desert ochre, ocean blue.
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

import numpy as np

# Mirror constants from sister diag
FULL_SIZE = 50_000
SCALE = 8
DS_SIZE = FULL_SIZE // SCALE  # 6250
SEA_LEVEL_RAW_16 = 17050
SAND_DUNE_DESERT_ZONE = 170
CELLS_PER_TILE = 512 // SCALE  # 64

# WP defaults from river_script1.7.js
WP_START_WIDTH_BLOCKS = 3        # MC blocks at source
WP_END_WIDTH_BLOCKS = 15         # MC blocks at mouth
WP_RIVER_DEPTH = 0.35            # depth = width * riverDepth + 1.5 blocks
WP_MIN_APPARENT_LEN = 200        # min path length used in width formula
WP_MOUTH_EXTENSION_BLOCKS = 60   # endWidth * 4 in WP defaults

REFERENCE_TILES = {
    "(51,53) MIXED_FOREST":     (51, 53),
    "(30,49) DRY_PINE_BARRENS": (30, 49),
    "(37,8) COASTAL_HEATH":     (37, 8),
}


def _log(msg: str) -> None:
    print(f"[wp_world] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------
def _read_ds(path: Path, nearest: bool = False, dtype=None) -> np.ndarray:
    import rasterio
    from rasterio.enums import Resampling
    with rasterio.open(str(path)) as src:
        arr = src.read(
            1,
            out_shape=(DS_SIZE, DS_SIZE),
            resampling=Resampling.nearest if nearest else Resampling.average,
        )
    if dtype is not None and arr.dtype != dtype:
        arr = arr.astype(dtype)
    return arr


def _build_height_lut() -> np.ndarray:
    gaea_in = np.array([0, 17050, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    lut = np.interp(np.arange(65536, dtype=np.float64), gaea_in, mc_y_out)
    return lut.astype(np.float32)


# ---------------------------------------------------------------------------
# WP-style width per centerline cell
# ---------------------------------------------------------------------------
def widths_along_path(
    path: list[tuple[int, int]],
    height_blocks: np.ndarray,
    start_width_blocks: float = WP_START_WIDTH_BLOCKS,
    end_width_blocks: float = WP_END_WIDTH_BLOCKS,
    min_apparent_len: int = WP_MIN_APPARENT_LEN,
) -> np.ndarray:
    """
    Linear width interpolation source -> mouth, per WP river_script1.7.js
    line 506 formula.  Adds slope² bonus near source for guard-rail behaviour.
    Returns widths in MC BLOCKS per centerline cell.

    path[0] = source, path[-1] = mouth.
    """
    n = len(path)
    if n == 0:
        return np.array([], dtype=np.float32)

    # WP iterates from i=path.length-1 (source) down to i=0 (mouth).
    # We have path[0]=source, so i_from_mouth = n-1-i.
    # Apparent length is clamped to min_apparent_len for short rivers
    # (prevents tiny streams getting full endWidth).
    L = max(n, min_apparent_len)
    widths = np.zeros(n, dtype=np.float32)
    for i, (r, c) in enumerate(path):
        i_from_mouth = (n - 1) - i
        adj_i = i_from_mouth - (n - L)
        frac = max(0.0, min(1.0, (L - adj_i) / L))
        w = start_width_blocks + frac * (end_width_blocks - start_width_blocks)
        # Slope bonus: WP uses 2*slope² where slope = max(height_change, 1).
        # We approximate slope by local gradient over 4 cells.
        if 0 < i < n - 4:
            r0, c0 = path[i]
            r1, c1 = path[min(i + 4, n - 1)]
            slope = abs(float(height_blocks[r0, c0]) - float(height_blocks[r1, c1])) / 4.0
            slope = min(slope, 1.0)
            w += 2.0 * slope * slope
        # Source guardrail: WP sets slope=0.5 for first 4 cells, giving 0.5 width bonus
        if i_from_mouth < 4:
            w += 0.5
        widths[i] = w
    return widths


# ---------------------------------------------------------------------------
# Stamp variable-width footprints into a (H, W) bool mask using EDT
# ---------------------------------------------------------------------------
def stamp_footprints(
    paths: list[list[tuple[int, int]]],
    widths_per_path: list[np.ndarray],
    H: int,
    W: int,
    blocks_per_cell: float = SCALE,
) -> np.ndarray:
    """
    Stamps variable-width river footprints into an (H, W) bool mask.

    Each path cell carries a width-in-blocks; a non-river cell becomes part
    of the river if its distance to the nearest river cell (in MAP cells)
    is <= width_at_nearest / 2 / blocks_per_cell.
    """
    from scipy.ndimage import distance_transform_edt

    centerline_mask = np.zeros((H, W), dtype=bool)
    width_at_cell = np.zeros((H, W), dtype=np.float32)
    for path, widths in zip(paths, widths_per_path):
        for (r, c), w in zip(path, widths):
            if 0 <= r < H and 0 <= c < W:
                centerline_mask[r, c] = True
                if w > width_at_cell[r, c]:
                    width_at_cell[r, c] = w

    if not centerline_mask.any():
        return centerline_mask

    # EDT from non-river cells to find nearest river-cell width
    dist_map, (iy, ix) = distance_transform_edt(~centerline_mask, return_indices=True)
    nearest_w = width_at_cell[iy, ix]  # in MC blocks
    # Footprint: dist (map cells) <= width(blocks)/2 / blocks_per_cell
    footprint = dist_map <= (nearest_w / 2.0 / blocks_per_cell)
    return footprint


# ---------------------------------------------------------------------------
# Mouth extension — walk a few cells INTO ocean past each path's terminus
# ---------------------------------------------------------------------------
def add_mouth_extensions(
    paths: list[list[tuple[int, int]]],
    widths_per_path: list[np.ndarray],
    height_raw: np.ndarray,
    lake_mask: np.ndarray,
    extension_blocks: float = WP_MOUTH_EXTENSION_BLOCKS,
    blocks_per_cell: float = SCALE,
) -> tuple[list[list[tuple[int, int]]], list[np.ndarray]]:
    """
    For each path that ends in ocean, append a tapered extension of N cells
    into the water with widening width profile (continues from end-width up
    to end-width * 1.5).  This is WP's `pathFindDown(end, endWidth*4)` step
    simplified — we just walk in the local downhill direction towards open
    ocean.

    Lake-terminating paths are NOT extended (rivers feed lakes, no delta).
    """
    H, W = height_raw.shape
    extension_cells = int(round(extension_blocks / blocks_per_cell))  # ~7-8 at 1:8

    new_paths = []
    new_widths = []
    n_lake_term = 0
    n_ocean_term = 0
    for path, widths in zip(paths, widths_per_path):
        new_paths.append(path)
        new_widths.append(widths)
        if len(path) < 4:
            continue
        # Direction of last 4 path cells
        r_last, c_last = path[-1]
        if not (0 <= r_last < H and 0 <= c_last < W):
            continue
        # Use lake_mask directly — heights at coastlines are averaging-noisy
        if lake_mask[r_last, c_last]:
            n_lake_term += 1
            continue
        n_ocean_term += 1
        r_pre, c_pre = path[-4]
        dr = r_last - r_pre
        dc = c_last - c_pre
        n = max(abs(dr), abs(dc), 1)
        dr_unit = dr / n
        dc_unit = dc / n

        ext_path = []
        ext_w = []
        end_w = float(widths[-1]) if len(widths) else WP_END_WIDTH_BLOCKS
        for k in range(1, extension_cells + 1):
            er = int(round(r_last + dr_unit * k))
            ec = int(round(c_last + dc_unit * k))
            if not (0 <= er < H and 0 <= ec < W):
                break
            if height_raw[er, ec] > SEA_LEVEL_RAW_16:
                break  # extension hit land — don't keep
            # widening profile: end_w → end_w * 1.5 over the extension length
            t = k / extension_cells
            w = end_w * (1.0 + 0.5 * t)
            ext_path.append((er, ec))
            ext_w.append(w)
        if ext_path:
            new_paths.append(ext_path)
            new_widths.append(np.array(ext_w, dtype=np.float32))

    _log(f"  termination split: {n_ocean_term} ocean, {n_lake_term} lake")
    return new_paths, new_widths


# ---------------------------------------------------------------------------
# Existing-system width footprint (for comparison)
# ---------------------------------------------------------------------------
def existing_river_footprint(masks_dir: Path) -> np.ndarray:
    """Re-use core logic from diag_world_map_comprehensive: stamp existing
    hydro_centerline.tif (>=1 OR == 255) at hydro_width.tif/2 radius."""
    from scipy.ndimage import distance_transform_edt

    centerline = _read_ds(masks_dir / "hydro_centerline.tif", nearest=True).astype(np.uint16)
    river_mask = (centerline >= 1) & (centerline != 0)

    if not river_mask.any():
        return np.zeros((DS_SIZE, DS_SIZE), dtype=bool)

    hw_path = masks_dir / "hydro_width.tif"
    if hw_path.exists():
        # hydro_width.tif is 50k uint8.  rasterio.Resampling.max isn't allowed
        # for reads — use 'average' resampling and accept slight smoothing.
        # For diag purposes the visual width is approximate anyway.
        hw_6250 = _read_ds(hw_path).astype(np.float32)
    else:
        hw_6250 = np.full((DS_SIZE, DS_SIZE), 8.0, dtype=np.float32)  # default 8 blocks

    dist_map, (iy, ix) = distance_transform_edt(~river_mask, return_indices=True)
    nearest_w = hw_6250[iy, ix]
    footprint = dist_map <= (nearest_w / 2.0 / SCALE)
    return footprint


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def render(
    out_path: Path,
    height_blocks: np.ndarray,
    height_raw: np.ndarray,
    lake_mask: np.ndarray,
    sand_dune_mask: np.ndarray,
    existing_footprint: np.ndarray,
    proposed_footprint: np.ndarray,
    sources: list[tuple[int, int]] | None = None,
    paths_ok: list[list[tuple[int, int]]] | None = None,
    viz_dilate: int = 1,
) -> None:
    """Render side-by-side in-game-style overview of the two river systems.

    `viz_dilate` thickens BOTH footprints for visibility at the overview
    scale — the underlying carve widths are unaffected, this is render-only.
    """
    import matplotlib.pyplot as plt
    from scipy.ndimage import binary_dilation

    H, W = height_blocks.shape

    # Visibility dilation (render-only)
    if viz_dilate > 0:
        existing_footprint = binary_dilation(existing_footprint, iterations=viz_dilate)
        proposed_footprint = binary_dilation(proposed_footprint, iterations=viz_dilate)

    # 1. Build base RGB: terrain colormap with hillshade
    norm_h = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    bg = plt.get_cmap("terrain")(norm_h)[..., :3].astype(np.float32)

    # Hillshade (NW-light, simple 0.5 ambient + 0.5 directional)
    gy, gx = np.gradient(height_blocks.astype(np.float32))
    # Light from NW: dx<0, dy<0 means face is lit
    light = np.clip(0.5 + 0.5 * (-gx - gy) / 30.0, 0.4, 1.2)
    bg = np.clip(bg * light[..., None], 0, 1)

    # 2. Apply layers (in MC-map order)
    OCEAN_BLUE = np.array([0.13, 0.27, 0.49])
    LAKE_BLUE = np.array([0.34, 0.62, 0.78])
    RIVER_BLUE = np.array([0.20, 0.42, 0.66])
    DUNE_OCHRE = np.array([0.94, 0.83, 0.55])

    ocean = height_raw <= SEA_LEVEL_RAW_16

    def _compose(footprint: np.ndarray) -> np.ndarray:
        out = bg.copy()
        out[sand_dune_mask] = DUNE_OCHRE
        out[ocean] = OCEAN_BLUE
        out[footprint & ~ocean & ~lake_mask] = RIVER_BLUE
        out[lake_mask] = LAKE_BLUE
        return out

    rgb_existing = _compose(existing_footprint)
    rgb_proposed = _compose(proposed_footprint)

    # 3. Layout: 2 rows top-level
    #   row 0: full overview (existing | proposed)
    #   row 1-3: per-tile zooms (existing | proposed) for each reference tile
    n_tiles = len(REFERENCE_TILES)
    fig, axes = plt.subplots(1 + n_tiles, 2, figsize=(14, 6 * (1 + n_tiles)),
                             facecolor="white")

    # Compute termination markers (red = lake-term, orange = ocean-term)
    ocean_mask_chk = height_raw <= SEA_LEVEL_RAW_16
    src_pts = sources or []
    lake_term_pts: list[tuple[int, int]] = []
    ocean_term_pts: list[tuple[int, int]] = []
    if paths_ok:
        for p in paths_ok:
            r, c = p[-1]
            if lake_mask[r, c]:
                lake_term_pts.append((r, c))
            elif ocean_mask_chk[r, c]:
                ocean_term_pts.append((r, c))

    def _draw(ax, img, title, marker_pts=True):
        ax.imshow(img, interpolation="nearest")
        if marker_pts and src_pts:
            ax.scatter([p[1] for p in src_pts], [p[0] for p in src_pts],
                       s=14, c="lime", marker="o", edgecolor="black",
                       linewidths=0.4, zorder=3, label=f"source ({len(src_pts)})")
            if lake_term_pts:
                ax.scatter([p[1] for p in lake_term_pts],
                           [p[0] for p in lake_term_pts],
                           s=22, c="red", marker="X", linewidths=0.4,
                           edgecolor="black", zorder=4,
                           label=f"lake-term ({len(lake_term_pts)})")
            if ocean_term_pts:
                ax.scatter([p[1] for p in ocean_term_pts],
                           [p[0] for p in ocean_term_pts],
                           s=22, c="orange", marker="P", linewidths=0.4,
                           edgecolor="black", zorder=4,
                           label=f"ocean-term ({len(ocean_term_pts)})")
        ax.set_title(title, fontsize=11)
        ax.set_xticks([])
        ax.set_yticks([])

    _draw(axes[0, 0], rgb_existing, "EXISTING river system (hydro_centerline + hydro_width)",
          marker_pts=False)
    _draw(axes[0, 1], rgb_proposed,
          f"PROPOSED WP findPath  ({len(src_pts)} sources, "
          f"{len(lake_term_pts)} hit lake, {len(ocean_term_pts)} hit ocean)")
    if src_pts:
        axes[0, 1].legend(loc="upper right", fontsize=8, framealpha=0.9)

    zoom_half = 3 * CELLS_PER_TILE
    for i, (label, (tx, tz)) in enumerate(REFERENCE_TILES.items()):
        r0 = max(tz * CELLS_PER_TILE - zoom_half, 0)
        r1 = min(tz * CELLS_PER_TILE + zoom_half, H)
        c0 = max(tx * CELLS_PER_TILE - zoom_half, 0)
        c1 = min(tx * CELLS_PER_TILE + zoom_half, W)
        # Subset markers to this zoom region (offset to subimage coords)
        sub_src = [(r - r0, c - c0) for r, c in src_pts
                   if r0 <= r < r1 and c0 <= c < c1]
        sub_lake = [(r - r0, c - c0) for r, c in lake_term_pts
                    if r0 <= r < r1 and c0 <= c < c1]
        sub_ocean = [(r - r0, c - c0) for r, c in ocean_term_pts
                     if r0 <= r < r1 and c0 <= c < c1]

        axes[i + 1, 0].imshow(rgb_existing[r0:r1, c0:c1], interpolation="nearest")
        axes[i + 1, 0].set_title(f"EXISTING — {label}", fontsize=11)
        axes[i + 1, 0].set_xticks([]); axes[i + 1, 0].set_yticks([])

        axes[i + 1, 1].imshow(rgb_proposed[r0:r1, c0:c1], interpolation="nearest")
        if sub_src:
            axes[i + 1, 1].scatter([p[1] for p in sub_src], [p[0] for p in sub_src],
                                   s=40, c="lime", marker="o", edgecolor="black",
                                   linewidths=0.6, zorder=3)
        if sub_lake:
            axes[i + 1, 1].scatter([p[1] for p in sub_lake], [p[0] for p in sub_lake],
                                   s=60, c="red", marker="X", linewidths=0.6,
                                   edgecolor="black", zorder=4)
        if sub_ocean:
            axes[i + 1, 1].scatter([p[1] for p in sub_ocean], [p[0] for p in sub_ocean],
                                   s=60, c="orange", marker="P", linewidths=0.6,
                                   edgecolor="black", zorder=4)
        axes[i + 1, 1].set_title(f"PROPOSED — {label}", fontsize=11)
        axes[i + 1, 1].set_xticks([]); axes[i + 1, 1].set_yticks([])

    fig.suptitle(
        "S80 in-game preview — current vs WP findPath river system",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.98))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=130, bbox_inches="tight")
    plt.close(fig)
    _log(f"  rendered {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--cache", default="memory/wp_pathfind_cache.pkl")
    p.add_argument("--out", default="memory/wp_pathfind_world_view.png")
    p.add_argument("--start-width", type=float, default=8.0,
                   help="River width at source in MC blocks (default 8 = 1 cell at 1:8)")
    p.add_argument("--end-width", type=float, default=40.0,
                   help="River width at mouth in MC blocks (default 40, scaled for 50k world)")
    args = p.parse_args()

    # 1. Load cached pathfinding result
    cache_path = Path(args.cache)
    if not cache_path.is_absolute():
        cache_path = Path(__file__).resolve().parent.parent / cache_path
    if not cache_path.is_file():
        _log(f"ERROR: cache not found at {cache_path}.  Run diag_wp_pathfind.py first.")
        return 2
    with open(cache_path, "rb") as f:
        cache = pickle.load(f)
    paths_ok = cache["paths_ok"]
    H, W = cache["shape"]
    _log(f"  loaded {len(paths_ok)} paths from cache")

    # 2. Load masks
    masks_dir = Path(args.masks)
    _log("  loading masks...")
    t0 = time.time()
    height_raw = _read_ds(masks_dir / "height.tif")
    lake_id = _read_ds(masks_dir / "hydro_lake.tif", nearest=True).astype(np.uint16)
    override = _read_ds(masks_dir / "override.tif", nearest=True).astype(np.uint8)
    _log(f"  load: {time.time() - t0:.1f}s")

    lut = _build_height_lut()
    height_blocks = lut[height_raw.astype(np.int32)]
    lake_mask = lake_id > 0
    sand_dune_mask = override == SAND_DUNE_DESERT_ZONE

    # Sanity: where did the cached paths terminate?
    ocean_mask_chk = height_raw <= SEA_LEVEL_RAW_16
    n_ocean = sum(1 for p in paths_ok if ocean_mask_chk[p[-1][0], p[-1][1]] and not lake_mask[p[-1][0], p[-1][1]])
    n_lake = sum(1 for p in paths_ok if lake_mask[p[-1][0], p[-1][1]])
    n_other = len(paths_ok) - n_ocean - n_lake
    _log(f"  termination breakdown: {n_ocean} ocean, {n_lake} lake, {n_other} other")
    if n_other > 0:
        # Inspect a few "other" terminations
        for p in paths_ok[:5]:
            r, c = p[-1]
            if not (ocean_mask_chk[r, c] or lake_mask[r, c]):
                _log(f"    other-term path[-1]=({r},{c}) height_raw={height_raw[r,c]} "
                     f"lake_id={lake_id[r,c]}")

    # 3. Compute WP widths per cell (scaled to our 50k world by default)
    _log(f"  computing WP widths per path (start={args.start_width} blocks, "
         f"end={args.end_width} blocks)...")
    widths_per_path = [
        widths_along_path(path, height_blocks,
                          start_width_blocks=args.start_width,
                          end_width_blocks=args.end_width)
        for path in paths_ok
    ]

    # 4. Add mouth extensions for paths ending in ocean
    _log("  adding mouth extensions...")
    paths_ext, widths_ext = add_mouth_extensions(
        paths_ok, widths_per_path, height_raw, lake_mask,
    )
    _log(f"  {len(paths_ext) - len(paths_ok)} mouth-extension fragments added")

    # 5. Stamp footprints (proposed WP system)
    _log("  stamping proposed footprints (WP)...")
    t0 = time.time()
    proposed_footprint = stamp_footprints(paths_ext, widths_ext, H, W,
                                          blocks_per_cell=SCALE)
    _log(f"  proposed footprint: {proposed_footprint.sum():,} cells in {time.time()-t0:.1f}s")

    # 6. Existing system footprint for comparison
    _log("  stamping existing footprint (current)...")
    t0 = time.time()
    existing_footprint = existing_river_footprint(masks_dir)
    _log(f"  existing footprint: {existing_footprint.sum():,} cells in {time.time()-t0:.1f}s")

    # 7. Render
    _log("  rendering...")
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = Path(__file__).resolve().parent.parent / out_path
    render(out_path, height_blocks, height_raw, lake_mask, sand_dune_mask,
           existing_footprint, proposed_footprint,
           sources=cache.get("sources"),
           paths_ok=paths_ok,
           viz_dilate=2)

    _log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
