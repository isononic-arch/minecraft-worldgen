"""
diag_wp_pathfind.py - WP-style findPath visualizer (S80, NO carving, NO commits)

Loads height.tif + hydro_lake.tif + override.tif at 1:8 (6250x6250),
identifies stream-head sources from existing hydro_order.tif (Strahler==1
and in_count==0), runs a Python port of WP `findPath` from each, and
renders a multi-panel PNG showing the proposed paths over the heightmap.

USAGE:
    py tools/diag_wp_pathfind.py [--out memory/wp_pathfind_diag.png]
                                 [--max-sources N]
                                 [--meander R]

Validates:
  - Path connectivity (every source reaches ocean or lake)
  - SAND_DUNE_DESERT exclusion
  - Meander appearance
  - Spillpoint-to-ocean traversal (for any lake whose spill direction is detectable)

This is a READ-ONLY tool.  No mask writes, no pipeline modification.
"""
from __future__ import annotations

import argparse
import heapq
import sys
import time
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Constants (mirror core/hydrology_precompute.py)
# ---------------------------------------------------------------------------
FULL_SIZE = 50_000
SCALE = 8
DS_SIZE = FULL_SIZE // SCALE  # 6250
SEA_LEVEL_RAW_16 = 17050
SEA_LEVEL_MC_Y = 63
SAND_DUNE_DESERT_ZONE = 170

# Tile->cell mapping at 1:8: each MC tile = 512 blocks = 64 cells
CELLS_PER_TILE = 512 // SCALE  # 64

# Reference tiles (per CLAUDE.md)
REFERENCE_TILES = {
    "(51,53) MIXED_FOREST":     (51, 53),
    "(30,49) DRY_PINE_BARRENS": (30, 49),
    "(37,8) COASTAL_HEATH":     (37, 8),
}

# WP findPath neighborhood: rim of 5x5 without corners (12 cells)
RIM_5X5_OFFSETS = np.array([
    (2, 1), (2, 0), (2, -1),
    (-2, 1), (-2, 0), (-2, -1),
    (1, 2), (0, 2), (-1, 2),
    (1, -2), (0, -2), (-1, -2),
], dtype=np.int32)


def _log(msg: str) -> None:
    print(f"[diag_wp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------
def _read_ds(path: Path, name: str, nearest: bool = False) -> np.ndarray:
    import rasterio
    from rasterio.enums import Resampling
    with rasterio.open(str(path)) as src:
        return src.read(
            1,
            out_shape=(DS_SIZE, DS_SIZE),
            resampling=Resampling.nearest if nearest else Resampling.average,
        )


def _build_height_lut() -> np.ndarray:
    """Raw uint16 -> MC Y (matches column_generator._build_lut_vectorized)."""
    gaea_in = np.array([0, 17050, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64, 63, 200, 448], dtype=np.float64)
    lut = np.interp(np.arange(65536, dtype=np.float64), gaea_in, mc_y_out)
    return lut.astype(np.float32)


# ---------------------------------------------------------------------------
# Cheap deterministic meander noise (no opensimplex dependency)
# Hash-based fbm: smooth at scale ~30 cells, [0,1].
# ---------------------------------------------------------------------------
def _build_meander_field(H: int, W: int, seed: int = 0xC0DEC0D) -> np.ndarray:
    """
    Deterministic smooth-ish noise field, [0, 1].  Used in place of WP's
    simplex2 for the diagnostic.  Matches WP intent: bias path costs by a
    spatially-coherent 'preferred-route' field.
    """
    # Two octaves of value-noise via integer hashing + bilinear upsample.
    rng = np.random.default_rng(seed)
    # Coarse grid at 1/16 of full resolution
    cH, cW = H // 16 + 2, W // 16 + 2
    coarse = rng.random((cH, cW), dtype=np.float32)

    # Bilinear upsample to (H, W)
    yi = np.linspace(0, cH - 1, H).astype(np.float32)
    xi = np.linspace(0, cW - 1, W).astype(np.float32)
    y0 = np.floor(yi).astype(np.int32)
    x0 = np.floor(xi).astype(np.int32)
    y1 = np.minimum(y0 + 1, cH - 1)
    x1 = np.minimum(x0 + 1, cW - 1)
    fy = (yi - y0)[:, None]
    fx = (xi - x0)[None, :]
    a = coarse[y0[:, None], x0[None, :]]
    b = coarse[y0[:, None], x1[None, :]]
    c = coarse[y1[:, None], x0[None, :]]
    d = coarse[y1[:, None], x1[None, :]]
    return a * (1 - fy) * (1 - fx) + b * (1 - fy) * fx + c * fy * (1 - fx) + d * fy * fx


# ---------------------------------------------------------------------------
# WP findPath port (Python, heapq-based)
# ---------------------------------------------------------------------------
def find_path(
    sr: int, sc: int,
    height_blocks: np.ndarray,       # (H, W) float32 MC Y
    sink_mask: np.ndarray,           # (H, W) bool — True = ocean OR lake
    avoid_mask: np.ndarray,          # (H, W) bool — True = blocked (e.g. sand_dune)
    meander: np.ndarray,             # (H, W) float32 [0, 1]
    randomness: float = 1000.0,      # WP default
    height_penalty: float = 150.0,   # WP default ("blocks of detour per +1 height")
    max_visits: int = 250_000,
    forbid_lake_id: int = 0,         # if >0, cells with lake_id==this are not sinks
    lake_id_arr: np.ndarray | None = None,
) -> list[tuple[int, int]] | None:
    """
    Port of WP river_script1.7.js `findPath`.
    Returns the path from source -> first sink as a list of (r, c) cells,
    or None if no path was found.
    """
    H, W = height_blocks.shape
    if not (0 <= sr < H and 0 <= sc < W):
        return None

    # When pathing FROM a lake spillpoint, the source lake's cells are
    # EXCLUDED from the sink set (so the path doesn't trivially re-enter
    # the lake it just spilled out of).  We achieve this by overlaying
    # an excluded mask for the duration of this call.
    use_sink_mask = sink_mask
    use_avoid_mask = avoid_mask
    if forbid_lake_id and lake_id_arr is not None:
        forbid_cells = lake_id_arr == forbid_lake_id
        if forbid_cells.any():
            use_sink_mask = sink_mask & ~forbid_cells
            use_avoid_mask = avoid_mask | forbid_cells

    if use_sink_mask[sr, sc]:
        return None  # source already in water; skip
    if use_avoid_mask[sr, sc]:
        return None

    # Open set: (priority, counter, r, c, path_dist)
    # 'counter' for heapq tie-break determinism.
    counter = 0
    open_set: list[tuple[float, int, int, int, float]] = []
    heapq.heappush(open_set, (4.0, counter, sr, sc, 4.0))
    counter += 1

    # came_from[r, c] = (parent_r, parent_c) or (-1, -1) for source
    came_from = -np.ones((H, W, 2), dtype=np.int32)
    came_from[sr, sc] = (-1, -1)
    visited = np.zeros((H, W), dtype=bool)
    visited[sr, sc] = True

    visits = 0
    while open_set and visits < max_visits:
        _, _, r, c, dist = heapq.heappop(open_set)
        visits += 1

        # Goal test
        if use_sink_mask[r, c]:
            # Reconstruct path source->goal
            path: list[tuple[int, int]] = []
            cr, cc = r, c
            while cr >= 0 and cc >= 0:
                path.append((int(cr), int(cc)))
                pr, pc = came_from[cr, cc]
                cr, cc = int(pr), int(pc)
            path.reverse()
            return path

        # Per-parent meander value (matches WP: same value for all 12 children).
        # The bias selects WHICH parent's neighbours get expanded first; over
        # many parents this produces wandering paths through the noise field.
        parent_meander = float(meander[r, c]) * randomness

        for dr, dc in RIM_5X5_OFFSETS:
            nr, nc = r + int(dr), c + int(dc)
            if not (0 <= nr < H and 0 <= nc < W):
                continue
            if use_avoid_mask[nr, nc]:
                continue
            if visited[nr, nc]:
                continue

            cell_dist = 1.0 if abs(dr) + abs(dc) <= 1 else float(np.hypot(dr, dc))
            new_dist = dist + cell_dist
            h_blocks = float(height_blocks[nr, nc])
            priority = new_dist + height_penalty * h_blocks + parent_meander

            visited[nr, nc] = True
            came_from[nr, nc] = (r, c)
            heapq.heappush(open_set, (priority, counter, nr, nc, new_dist))
            counter += 1

            # Intermediate fill for 5x5-rim moves (back-fill 1-step intermediate)
            if abs(dr) == 2 or abs(dc) == 2:
                # WP: pick the lower of (dr/2, 0) and (dr/2, dc) [or (0, dc/2) and
                # (dr, dc/2)] as the intermediate; we pick the lower of the
                # two candidates by height and stamp came_from so the path is
                # 1-cell connected after reconstruction.
                if abs(dr) == 2:
                    cand = [(r + dr // 2, c), (r + dr // 2, c + dc)]
                else:
                    cand = [(r, c + dc // 2), (r + dr, c + dc // 2)]
                best = None
                best_h = np.inf
                for ir, ic in cand:
                    if not (0 <= ir < H and 0 <= ic < W):
                        continue
                    if visited[ir, ic]:
                        continue
                    h = float(height_blocks[ir, ic])
                    if h < best_h:
                        best_h = h
                        best = (ir, ic)
                if best is not None:
                    visited[best] = True
                    came_from[best] = (r, c)
                    # Re-parent the long-jump cell through the intermediate
                    came_from[nr, nc] = best

    return None


# ---------------------------------------------------------------------------
# Source picking
# ---------------------------------------------------------------------------
def pick_sources(
    river_mask: np.ndarray,     # (H, W) bool — Strahler river extent
    in_count: np.ndarray,       # (H, W) int32 — upstream river-neighbour count
    avoid_mask: np.ndarray,     # (H, W) bool — exclude (sand dune, ocean, lake)
    flow: np.ndarray,           # (H, W) float32 — for picking high-flow sources
    max_sources: int = 200,
    grid_cells: int = 16,       # spatial grid for uniform subsample
) -> list[tuple[int, int]]:
    """
    Stream heads = river_mask & (in_count == 0) & ~avoid.

    For uniform spatial coverage we lay down a grid_cells x grid_cells grid
    over the world and keep the highest-flow head per grid cell.  This gives
    roughly max_sources sources spread across the whole map (not clustered
    in dense-flow regions like a random subsample would).
    """
    cand_mask = river_mask & (in_count == 0) & ~avoid_mask
    head_r, head_c = np.where(cand_mask)
    n_total = len(head_r)
    _log(f"  found {n_total} stream-head candidates")
    if n_total == 0:
        return []

    H, W = river_mask.shape
    # Bucket each candidate into a grid cell, keep the highest-flow per bucket
    bucket_h = max(1, H // grid_cells)
    bucket_w = max(1, W // grid_cells)
    head_flow = flow[head_r, head_c]
    bucket = (head_r // bucket_h) * grid_cells + (head_c // bucket_w)
    # For each unique bucket, take argmax of flow
    order = np.argsort(bucket, kind="stable")
    bucket_s = bucket[order]
    flow_s = head_flow[order]
    starts = np.concatenate(([0], np.where(np.diff(bucket_s) != 0)[0] + 1))
    ends = np.concatenate((starts[1:], [len(bucket_s)]))
    keep_idx = []
    for s, e in zip(starts, ends):
        local = order[s:e]
        local_flow = flow_s[s:e]
        keep_idx.append(local[np.argmax(local_flow)])
    keep_idx = np.array(keep_idx, dtype=np.int64)
    _log(f"  grid-subsampled to {len(keep_idx)} buckets ({grid_cells}x{grid_cells} grid)")

    # If still too many, take the top max_sources by flow
    if len(keep_idx) > max_sources:
        flow_keep = flow[head_r[keep_idx], head_c[keep_idx]]
        top = np.argsort(flow_keep)[::-1][:max_sources]
        keep_idx = keep_idx[top]

    return [(int(head_r[i]), int(head_c[i])) for i in keep_idx]


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------
def render_diag(
    height_blocks: np.ndarray,
    height_raw: np.ndarray,
    lake: np.ndarray,
    override: np.ndarray,
    sink_mask: np.ndarray,
    avoid_mask: np.ndarray,
    sources: list[tuple[int, int]],
    paths_ok: list[list[tuple[int, int]]],
    paths_failed: list[tuple[int, int]],
    existing_centerline: np.ndarray | None,
    out_path: Path,
    path_dilate: int = 2,
) -> None:
    import matplotlib.pyplot as plt
    import matplotlib.colors as mcolors
    from scipy.ndimage import binary_dilation

    H, W = height_blocks.shape

    # Background: shaded heightmap (terrain colourmap)
    norm_h = np.clip((height_blocks + 64) / (448 + 64), 0, 1)
    bg_rgb = plt.get_cmap("terrain")(norm_h)[..., :3]

    # Paint ocean blue
    ocean = height_raw <= SEA_LEVEL_RAW_16
    bg_rgb[ocean] = (0.18, 0.32, 0.55)
    # Paint lakes light cyan
    bg_rgb[lake > 0] = (0.50, 0.78, 0.92)
    # Paint sand_dune_desert distinctive ochre
    bg_rgb[override == SAND_DUNE_DESERT_ZONE] = (0.94, 0.83, 0.55)

    # Build path raster + dilate for visibility
    new_path_img = np.zeros((H, W), dtype=bool)
    for path in paths_ok:
        for r, c in path:
            if 0 <= r < H and 0 <= c < W:
                new_path_img[r, c] = True
    if path_dilate > 0:
        new_path_img = binary_dilation(new_path_img, iterations=path_dilate)

    # Existing centerlines also dilated for fair visual comparison
    if existing_centerline is not None:
        old_mask = existing_centerline > 0
        if path_dilate > 0:
            old_mask = binary_dilation(old_mask, iterations=path_dilate)
    else:
        old_mask = np.zeros((H, W), dtype=bool)

    # Compose overlay
    overlay = bg_rgb.copy()
    path_mask = new_path_img
    overlay[path_mask] = np.array([1.0, 0.10, 0.10])  # bright red
    only_old = old_mask & ~path_mask
    overlay[only_old] = np.array([0.90, 0.10, 0.85])  # magenta
    both = old_mask & path_mask
    overlay[both] = np.array([1.0, 1.0, 0.20])       # yellow

    # Source markers: small green crosses (drawn after raster, in scatter)
    src_r = [s[0] for s in sources]
    src_c = [s[1] for s in sources]

    # Failed sources: small white X
    fail_r = [s[0] for s in paths_failed]
    fail_c = [s[1] for s in paths_failed]

    # Figure layout: 2 rows x 4 cols.
    # Row 0: full overview + 3 zooms (existing centerlines reference)
    # Row 1: full overview (NEW paths) + 3 zooms with NEW paths
    # We render WP paths in the bottom row, comparison row uses bg-only top + existing centerline in magenta.
    fig, axes = plt.subplots(2, 4, figsize=(22, 12), facecolor="white")

    def _draw(ax, sub_bg, title, src_pts=None, fail_pts=None):
        ax.imshow(sub_bg, interpolation="nearest")
        if src_pts:
            ax.scatter([p[1] for p in src_pts], [p[0] for p in src_pts],
                       s=18, c="lime", marker="o", edgecolor="black",
                       linewidths=0.5, zorder=3)
        if fail_pts:
            ax.scatter([p[1] for p in fail_pts], [p[0] for p in fail_pts],
                       s=22, c="white", marker="x", linewidths=1.5, zorder=4)
        ax.set_title(title, fontsize=10)
        ax.set_xticks([])
        ax.set_yticks([])

    # Row 0: existing centerlines as reference (magenta on bg)
    ref_overlay = bg_rgb.copy()
    ref_overlay[old_mask] = np.array([0.90, 0.10, 0.85])
    _draw(axes[0, 0], ref_overlay, "EXISTING Strahler centerlines (magenta) — full 6250x6250")

    # Row 1: WP findPath result on full
    _draw(axes[1, 0],
          overlay,
          f"WP findPath (red=new, magenta=old, yellow=overlap) — full 6250x6250",
          src_pts=sources, fail_pts=paths_failed)

    # Per-region zooms
    zoom_half = 3 * CELLS_PER_TILE  # 192 cells = 3-tile radius
    for col, (label, (tx, tz)) in enumerate(REFERENCE_TILES.items(), start=1):
        # Tile (tx, tz) in MC tile coords: row=tz, col=tx
        r0 = max(tz * CELLS_PER_TILE - zoom_half, 0)
        r1 = min(tz * CELLS_PER_TILE + zoom_half, H)
        c0 = max(tx * CELLS_PER_TILE - zoom_half, 0)
        c1 = min(tx * CELLS_PER_TILE + zoom_half, W)
        sub_ref = ref_overlay[r0:r1, c0:c1]
        sub_new = overlay[r0:r1, c0:c1]
        sub_src = [(r - r0, c - c0) for r, c in sources if r0 <= r < r1 and c0 <= c < c1]
        sub_fail = [(r - r0, c - c0) for r, c in paths_failed if r0 <= r < r1 and c0 <= c < c1]

        _draw(axes[0, col], sub_ref, f"EXISTING — {label}")
        _draw(axes[1, col], sub_new, f"WP findPath — {label}",
              src_pts=sub_src, fail_pts=sub_fail)

    fig.suptitle(
        f"S80 WP findPath diagnostic — {len(paths_ok)}/{len(sources)} sources reached water  "
        f"(green=source, white-X=no-path, red=new path, magenta=existing-only, yellow=both)",
        fontsize=12,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=140, bbox_inches="tight")
    plt.close(fig)
    _log(f"  rendered {out_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--masks", default=r"C:\Users\nicho\minecraft-worldgen\masks")
    p.add_argument("--out", default="memory/wp_pathfind_diag.png")
    p.add_argument("--max-sources", type=int, default=120)
    p.add_argument("--meander", type=float, default=1000.0,
                   help="WP randomness param (default 1000)")
    p.add_argument("--height-penalty", type=float, default=150.0,
                   help="WP height-penalty (default 150 = 'blocks of detour per +1 MC Y')")
    p.add_argument("--max-visits", type=int, default=300_000,
                   help="Per-source visit cap")
    p.add_argument("--grid-cells", type=int, default=16,
                   help="NxN spatial grid for uniform source subsample")
    p.add_argument("--path-dilate", type=int, default=2,
                   help="Dilate paths by N cells in render for visibility")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--cache", default="memory/wp_pathfind_cache.pkl",
                   help="Pickle path for findPath results (re-used by render tools)")
    p.add_argument("--no-spillpoints", action="store_true",
                   help="Skip lake spillpoint sources (test stream-heads only)")
    args = p.parse_args()

    masks_dir = Path(args.masks)
    if not masks_dir.is_dir():
        _log(f"ERROR: masks dir not found: {masks_dir}")
        return 2

    # 1. Load masks
    _log("Loading masks at 1:8...")
    t0 = time.time()
    height_raw = _read_ds(masks_dir / "height.tif", "height")
    lake_id = _read_ds(masks_dir / "hydro_lake.tif", "lake", nearest=True).astype(np.uint16)
    override = _read_ds(masks_dir / "override.tif", "override", nearest=True).astype(np.uint8)
    flow = _read_ds(masks_dir / "flow.tif", "flow")  # for source picking
    # Use Strahler order (>=1) as "existing centerlines" reference.
    # hydro_centerline.tif also contains 128 (wadi) + 255 (braid fill) per
    # CLAUDE.md S70 — those would render as chunky blobs that aren't really
    # comparable to per-cell findPath output.
    centerline_existing = _read_ds(masks_dir / "hydro_order.tif",
                                    "hydro_order", nearest=True)
    centerline_existing = (centerline_existing > 0).astype(np.uint8)
    _log(f"  load: {time.time() - t0:.1f}s")

    # Convert raw -> MC Y blocks
    lut = _build_height_lut()
    height_blocks = lut[height_raw.astype(np.int32)]

    # 2. Build masks
    H, W = height_raw.shape
    ocean_mask = height_raw <= SEA_LEVEL_RAW_16
    lake_mask = lake_id > 0
    sink_mask = ocean_mask | lake_mask
    sand_dune_mask = override == SAND_DUNE_DESERT_ZONE
    # avoid_mask ONLY blocks sand-dune-desert.  Ocean and lakes are sinks
    # (target cells where the path STOPS) — they must NOT also be in avoid,
    # because avoid is checked BEFORE adding to the heap.  If ocean were
    # in avoid, ocean cells would never be enqueued, so the path could
    # never reach them, and Dijkstra would route to whichever lake-sink
    # was reachable instead.  This was the bug producing 0 ocean / N lake
    # terminations on the first pass.
    avoid_mask = sand_dune_mask
    _log(f"  ocean: {ocean_mask.sum():,} cells | lakes: {lake_mask.sum():,} cells | "
         f"sand-dune: {sand_dune_mask.sum():,} cells")

    # 3. Identify stream heads from existing Strahler ordering.
    #    river_mask = flow > threshold AND above sea (matches precompute defaults).
    #    Use a low threshold to surface plausible heads even where Gaea flow is sparse.
    flow_norm = flow.astype(np.float32) / 65535.0 if flow.dtype != np.float32 else flow
    river_mask = (flow_norm >= 0.0008) & (~ocean_mask) & (~sand_dune_mask)
    _log(f"  river_mask: {river_mask.sum():,} cells")

    # Build D8 from height (lowest-of-8 neighbours)
    _log("  building D8...")
    t0 = time.time()
    pad = np.pad(height_raw.astype(np.int32), 1, mode="edge")
    # 8 neighbours (N, NE, E, SE, S, SW, W, NW)
    offsets = [(-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1)]
    stack = np.stack([pad[1 + dr:1 + dr + H, 1 + dc:1 + dc + W] for dr, dc in offsets], axis=0)
    cur = height_raw.astype(np.int32)
    diff = cur[None, :, :] - stack  # positive = neighbour is lower (downhill)
    d8 = diff.argmax(axis=0).astype(np.int8)
    # Mark flat/uphill cells as -1 (no downstream)
    no_down = diff.max(axis=0) <= 0
    d8[no_down] = -1
    _log(f"  D8: {time.time() - t0:.1f}s")

    # Upstream count via the same logic as core/hydrology_precompute.build_upstream_count
    in_count = np.zeros((H, W), dtype=np.int32)
    rr, rc = np.where(river_mask)
    dirs = d8[rr, rc]
    valid = dirs >= 0
    rr, rc, dirs = rr[valid], rc[valid], dirs[valid]
    drs = np.array([-1, -1, 0, 1, 1, 1, 0, -1], dtype=np.int32)[dirs]
    dcs = np.array([0, 1, 1, 1, 0, -1, -1, -1], dtype=np.int32)[dirs]
    dst_r = rr + drs
    dst_c = rc + dcs
    in_b = (dst_r >= 0) & (dst_r < H) & (dst_c >= 0) & (dst_c < W)
    dst_r, dst_c = dst_r[in_b], dst_c[in_b]
    dst_is_river = river_mask[dst_r, dst_c]
    np.add.at(in_count, (dst_r[dst_is_river], dst_c[dst_is_river]), 1)

    sources = pick_sources(river_mask, in_count, avoid_mask, flow_norm,
                           max_sources=args.max_sources,
                           grid_cells=args.grid_cells)
    _log(f"  sources picked: {len(sources)}")

    # Add lake spillpoints as additional sources — these produce the
    # outflow rivers that connect lakes to the ocean (or to the next lake).
    spill_source_lid: dict[tuple[int, int], int] = {}
    if not args.no_spillpoints:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from core.hydrology_precompute import compute_lake_spillpoints

        height_norm = height_raw.astype(np.float32) / 65535.0
        spill_id = compute_lake_spillpoints(lake_id, height_norm)
        spill_r, spill_c = np.where(spill_id > 0)
        for r, c in zip(spill_r, spill_c):
            r, c = int(r), int(c)
            spill_source_lid[(r, c)] = int(spill_id[r, c])
        _log(f"  spillpoint sources: {len(spill_source_lid)}")
        sources.extend(spill_source_lid.keys())

    if not sources:
        _log("ERROR: no sources to path from")
        return 2

    # 4. Build meander field
    _log("  building meander field...")
    meander = _build_meander_field(H, W, seed=0xC0DEC0D + args.seed)

    # 5. Run findPath from each source
    _log(f"  running findPath x{len(sources)}...")
    t0 = time.time()
    paths_ok: list[list[tuple[int, int]]] = []
    paths_failed: list[tuple[int, int]] = []
    visit_caps_hit = 0
    avg_len_acc = 0
    for i, (sr, sc) in enumerate(sources):
        if i % 10 == 0 and i > 0:
            _log(f"    {i}/{len(sources)} sources processed, {len(paths_ok)} ok")
        forbid_lid = spill_source_lid.get((sr, sc), 0)
        path = find_path(
            sr, sc, height_blocks, sink_mask, avoid_mask, meander,
            randomness=args.meander, height_penalty=args.height_penalty,
            max_visits=args.max_visits,
            forbid_lake_id=forbid_lid,
            lake_id_arr=lake_id if forbid_lid else None,
        )
        if path is None:
            paths_failed.append((sr, sc))
        else:
            paths_ok.append(path)
            avg_len_acc += len(path)

    elapsed = time.time() - t0
    avg_len = avg_len_acc / max(1, len(paths_ok))
    _log(f"  findPath: {elapsed:.1f}s, {len(paths_ok)}/{len(sources)} reached water, "
         f"avg path len = {avg_len:.0f} cells")

    # Cache pathfinding results for downstream render tools
    import pickle
    cache_path = Path(args.cache)
    if not cache_path.is_absolute():
        cache_path = Path(__file__).resolve().parent.parent / cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump({
            "sources": sources,
            "paths_ok": paths_ok,
            "paths_failed": paths_failed,
            "shape": (H, W),
            "params": {
                "max_sources": args.max_sources,
                "grid_cells": args.grid_cells,
                "meander": args.meander,
                "height_penalty": args.height_penalty,
                "max_visits": args.max_visits,
            },
        }, f)
    _log(f"  cached to {cache_path}")

    # 6. Render
    _log("Rendering...")
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = Path(__file__).resolve().parent.parent / out_path
    render_diag(
        height_blocks=height_blocks,
        height_raw=height_raw,
        lake=lake_id,
        override=override,
        sink_mask=sink_mask,
        avoid_mask=avoid_mask,
        sources=sources,
        paths_ok=paths_ok,
        paths_failed=paths_failed,
        existing_centerline=centerline_existing,
        out_path=out_path,
        path_dilate=args.path_dilate,
    )

    _log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
