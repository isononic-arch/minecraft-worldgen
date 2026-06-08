"""diag_seam_local.py — reproduce tile-boundary height seams at the surface_y
level WITHOUT chunk_writer (dry_run) so it fits in RAM. Dumps post-decorate
surface_y per tile, then compares shared boundary columns/rows for pairs.

Usage:
  py tools/diag_seam_local.py 73,66 74,66 73,67 74,67   # render these tiles
  py tools/diag_seam_local.py --compare-only             # just compare dumps

Pairs are auto-derived from adjacency among the rendered tiles (vertical = same
z, |dx|=1 -> A.col511 vs B.col0; horizontal = same x, |dz|=1 -> A.row511 vs B.row0).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DUMP = os.path.abspath("diag_seam_out")
os.environ["SURF_DUMP_DIR"] = DUMP
os.makedirs(DUMP, exist_ok=True)

import numpy as np
import run_pipeline as RP

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _args(tx, tz):
    return {
        "tile_x": tx, "tile_y": tz,
        "config_path": os.path.join(ROOT, "config", "thresholds.json"),
        "masks_dir": os.path.join(ROOT, "masks"),
        "schem_index_path": os.path.join(ROOT, "schematic_index.json"),
        "output_dir": os.path.join(ROOT, "output"),
        "tile_size": 512, "dry_run": True,
    }


def run(tx, tz):
    print(f"\n=== running tile ({tx},{tz}) dry_run surface dump ===", flush=True)
    RP._process_tile(_args(tx, tz))


def _load(tx, tz):
    return np.load(f"{DUMP}/sy_post_{tx}_{tz}.npy")


def compare_pair(a, b):
    ax, az = a; bx, bz = b
    A = _load(ax, az); B = _load(bx, bz)
    if az == bz and bx - ax == 1:           # vertical seam
        ae, be, lbl = A[:, 511], B[:, 0], "V  A.col511 | B.col0"
    elif ax == bx and bz - az == 1:         # horizontal seam
        ae, be, lbl = A[511, :], B[0, :], "H  A.row511 | B.row0"
    else:
        print(f"  ({a})-({b}) not adjacent, skip"); return
    d = ae.astype(np.int32) - be.astype(np.int32)
    print(f"\n[{ax},{az} | {bx},{bz}]  {lbl}")
    print(f"  mean={d.mean():+.3f}  median={np.median(d):+.1f}  "
          f"min={d.min():+d}  max={d.max():+d}  abs>2: {(np.abs(d)>2).sum()}/512")


if __name__ == "__main__":
    tiles = []
    for a in sys.argv[1:]:
        if a == "--compare-only":
            continue
        tx, tz = a.split(","); tiles.append((int(tx), int(tz)))
    if "--compare-only" not in sys.argv:
        for t in tiles:
            run(*t)
    # all adjacent pairs among the given tiles
    for i in range(len(tiles)):
        for j in range(len(tiles)):
            if i == j:
                continue
            ax, az = tiles[i]; bx, bz = tiles[j]
            if (az == bz and bx - ax == 1) or (ax == bx and bz - az == 1):
                compare_pair(tiles[i], tiles[j])
