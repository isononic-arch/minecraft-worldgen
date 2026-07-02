"""diag_tree_seam.py — reproduce + MEASURE the tree-canopy SEAM (trench) at a tile
boundary, LOCALLY (no cloud). Ground truth: SURF_DUMP_SCHEM two X-adjacent tiles
(default mainland 21,30 | 22,30, both 100% TEMPERATE_RAINFOREST), then rasterize each
tile's TREE canopy CLIPPED to its own bounds (simulating chunk_writer's cross-tile cull)
and measure the canopy-coverage profile across the shared seam. A trench = a coverage
DIP in the columns hugging the seam vs the tile interior.

  py islands/diag_tree_seam.py --dump --tiles 21,30 22,30      # run the pipeline, dump placements
  py islands/diag_tree_seam.py --measure --tiles 21,30 22,30   # metric + PNG (no pipeline)
  py islands/diag_tree_seam.py --both --tiles 21,30 22,30
"""
import os, sys, argparse
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TILE = 512
DUMP = str(ROOT / "islands" / "_seam_dump")


def _dump(tiles):
    os.makedirs(DUMP, exist_ok=True)
    os.environ["SURF_DUMP_DIR"] = DUMP
    os.environ["SURF_DUMP_SCHEM"] = "1"
    import run_pipeline
    for (tx, ty) in tiles:
        args = dict(tile_x=tx, tile_y=ty,
                    config_path=str(ROOT / "config" / "thresholds.json"),
                    masks_dir=str(ROOT / "masks"), output_dir=str(ROOT / "output"),
                    tile_size=TILE, dry_run=False,
                    schem_index_path=str(ROOT / "schematic_index.json"),
                    world_offset_x=0, world_offset_z=0)
        run_pipeline._process_tile(args)
        n = len(np.load(f"{DUMP}/plc_{tx}_{ty}.npy", allow_pickle=True))
        print(f"  dumped tile ({tx},{ty}): {n} placements", flush=True)


def _canopy(tx, ty):
    """Tree-canopy coverage mask for one tile, clipped to [0,TILE) (the cull).

    Models chunk_writer's REAL render rules (core/chunk_writer.py:2721-2730):
      (1) ANCHOR-CULL — a placement whose MIN-corner anchor (wx-px, wz-py) is
          OUTSIDE [0,TILE) is dropped ENTIRELY (line 2723 `continue`). These are
          the neighbour-owned seam trees; THIS tile never stamps them.
      (2) EXTENT-CLIP (STEP A) — for an in-bounds anchor, the canopy that runs
          past the tile edge is clipped away (the disc x0/x1 clamp below).
    Without (1) the harness double-counts the OOB-anchor rim emissions that
    chunk_writer actually culls, inflating the seam coverage."""
    from core.schematic_placement import CANOPY_RADIUS
    plc = np.load(f"{DUMP}/plc_{tx}_{ty}.npy", allow_pickle=True)
    m = np.zeros((TILE, TILE), bool)
    px, py = tx * TILE, ty * TILE
    ntree = 0
    for rec in plc:
        wx, wz, _py, size, stype = rec[0], rec[1], rec[2], rec[3], rec[4]
        if stype != "tree":
            continue
        _band = bool(rec[7]) if len(rec) > 7 else False
        lx = int(wx - px); lz = int(wz - py)
        if _band:
            # S100 seam-band tree: chunk_writer stamps the in-bounds portion
            # even for an OOB anchor (deterministic_seat path) — model that.
            if not (-16 <= lx < TILE + 16 and -16 <= lz < TILE + 16):
                continue
        elif not (0 <= lx < TILE and 0 <= lz < TILE):
            continue  # chunk_writer anchor-cull (non-band placements)
        ntree += 1
        r = int(CANOPY_RADIUS.get(size, 4))
        cx = lx + r            # canopy center ~ min-corner anchor + radius
        cz = lz + r
        for dz in range(-r, r + 1):
            z = cz + dz
            if not (0 <= z < TILE):
                continue
            w = int((r * r - dz * dz) ** 0.5)
            x0 = max(0, cx - w); x1 = min(TILE, cx + w + 1)
            if x1 > x0:
                m[z, x0:x1] = True
    return m, ntree


def _measure(tiles):
    (ax, ay), (bx, by) = tiles
    assert bx == ax + 1 and by == ay, "tiles must be X-adjacent (txA,ty)+(txA+1,ty)"
    A, na = _canopy(ax, ay)
    B, nb = _canopy(bx, by)
    colA = A.mean(axis=0)   # coverage per column (over z) in A; A's far/right edge is the seam
    colB = B.mean(axis=0)   # B's low/left edge is the seam
    E = 16
    interior = float(np.concatenate([colA[TILE // 4:TILE // 2], colB[TILE // 2:3 * TILE // 4]]).mean())
    a_seam = float(colA[TILE - E:].mean())     # A's last E cols (touching the seam)
    b_seam = float(colB[:E].mean())            # B's first E cols (touching the seam)
    a_edge1 = float(colA[TILE - 4:].mean())    # the worst 4 cols at the very seam
    b_edge1 = float(colB[:4].mean())
    print(f"\n=== TREE-SEAM at world X={bx*TILE} ({ax},{ay})|{(bx,by)}  trees A={na} B={nb} ===")
    print(f"  interior canopy coverage : {interior*100:5.1f}%")
    print(f"  A far-edge band ({E}col)  : {a_seam*100:5.1f}%   (very-edge 4col: {a_edge1*100:4.1f}%)")
    print(f"  B low-edge band ({E}col)  : {b_seam*100:5.1f}%   (very-edge 4col: {b_edge1*100:4.1f}%)")
    print(f"  TRENCH DEPTH (interior - min seam band): {(interior - min(a_seam, b_seam))*100:5.1f} pts")
    print(f"  EDGE ASYMMETRY |A-B| at seam: {abs(a_seam-b_seam)*100:5.1f} pts")
    # per-column profile across the seam (A's last 20 + B's first 20)
    prof = np.concatenate([colA[-20:], colB[:20]])
    print("  coverage profile across seam (A[-20:] | B[:20], % ):")
    print("   " + " ".join(f"{int(v*100):2d}" for v in prof[:20]) + " |")
    print("   | " + " ".join(f"{int(v*100):2d}" for v in prof[20:]))
    # side-by-side PNG (A | seam | B), green=canopy
    try:
        from PIL import Image
        img = np.zeros((TILE, 2 * TILE + 3, 3), np.uint8)
        img[:, :TILE][A] = (60, 200, 60); img[:, :TILE][~A] = (30, 30, 30)
        img[:, TILE + 3:][B] = (60, 200, 60); img[:, TILE + 3:][~B] = (30, 30, 30)
        img[:, TILE:TILE + 3] = (220, 60, 60)   # the seam line
        Path(ROOT / "islands" / "_val").mkdir(parents=True, exist_ok=True)
        out = str(ROOT / "islands" / "_val" / f"treeseam_{ax}_{ay}.png")
        Image.fromarray(img).save(out)
        print(f"  PNG: {out}  (left=A right=B, red=seam, green=canopy)")
    except Exception as e:
        print("  PNG skipped:", e)
    return dict(interior=interior, a_seam=a_seam, b_seam=b_seam,
                trench=interior - min(a_seam, b_seam), asym=abs(a_seam - b_seam))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--both", action="store_true")
    ap.add_argument("--tiles", nargs=2, default=["21,30", "22,30"])
    a = ap.parse_args()
    tiles = [tuple(int(x) for x in t.split(",")) for t in a.tiles]
    if a.dump or a.both:
        _dump(tiles)
    if a.measure or a.both:
        _measure(tiles)


if __name__ == "__main__":
    main()
