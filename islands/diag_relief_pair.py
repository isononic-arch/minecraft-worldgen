"""diag_relief_pair.py — run two ADJACENT island local tiles through the real
_process_tile (with RELIEF_DUMP_DIR set) and compare the rock-relief delta +
amp_eff at their SHARED edge. Confirms whether the relief amplitude desyncs at
an interior island tile boundary (R3).

Usage:
  RELIEF_DUMP_DIR=islands/_reliefdump \
  py islands/diag_relief_pair.py --name vincentia --ltx 9 --lty 9 --axis x
"""
import sys, os, json, argparse
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"
sys.path.insert(0, str(ISL))
import render_drive


def _safe(n):
    import re
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--ltx", type=int, required=True)
    ap.add_argument("--lty", type=int, required=True)
    ap.add_argument("--axis", choices=["x", "z"], default="x")
    a = ap.parse_args()
    dd = os.environ.get("RELIEF_DUMP_DIR")
    assert dd, "set RELIEF_DUMP_DIR"
    layout = json.loads((ISL / "layout.json").read_text())
    entry = next(i for i in layout["islands"]
                 if a.name in _safe(i["name"]) or a.name in i["dem_path"])
    name = _safe(entry["name"])
    mdir = ISL / "masks_islands" / name
    odir = ISL / "out" / name

    import run_pipeline
    if a.axis == "x":
        pair = [(a.ltx, a.lty), (a.ltx + 1, a.lty)]
    else:
        pair = [(a.ltx, a.lty), (a.ltx, a.lty + 1)]
    for (tx, ty) in pair:
        args = render_drive._tile_args(entry, mdir, odir, tx, ty, fast=True)
        print(f"[run] local tile ({tx},{ty})  woff=({args['world_offset_x']},{args['world_offset_z']})", flush=True)
        run_pipeline._process_tile(args)

    # load the two dumps and compare the shared edge
    A = np.load(f"{dd}/relief_{pair[0][0]}_{pair[0][1]}.npz")
    B = np.load(f"{dd}/relief_{pair[1][0]}_{pair[1][1]}.npz")
    print(f"rough_used A={int(A['rough_used'][0])} B={int(B['rough_used'][0])}", flush=True)
    if a.axis == "x":
        # A is left tile, B is right. shared edge: A[:, -1] abuts B[:, 0]
        eA = slice(None), -1
        eB = slice(None), 0
    else:
        eA = -1, slice(None)
        eB = 0, slice(None)
    for fld in ("delta", "amp_eff", "slope_gain", "smooth_gain", "n", "tier"):
        va = A[fld][eA].astype(np.float64)
        vb = B[fld][eB].astype(np.float64)
        rock = A["rock"][eA] & B["rock"][eB]
        if rock.sum() < 4:
            print(f"  {fld:11s}: <4 shared-rock edge cells", flush=True)
            continue
        d = np.abs(va[rock] - vb[rock])
        print(f"  {fld:11s}: n_rock={int(rock.sum()):4d}  edgeA[mean={va[rock].mean():+.3f}] "
              f"edgeB[mean={vb[rock].mean():+.3f}]  |A-B| mean={d.mean():.3f} p95={np.percentile(d,95):.3f} max={d.max():.3f}",
              flush=True)
    # the seam STEP in surface_y is driven by delta difference; show worst cells
    rock = A["rock"][eA] & B["rock"][eB]
    if rock.sum():
        dd_ = np.abs(A["delta"][eA].astype(int) - B["delta"][eB].astype(int))
        worst = np.argsort(dd_ * rock)[::-1][:8]
        print("  worst delta-step cells (edge row idx, |dA-dB|, nA, nB, ampA, ampB):", flush=True)
        for w in worst:
            if not rock[w]:
                continue
            print(f"    idx={w:3d} step={dd_[w]:2d}  nA={A['n'][eA][w]:+.3f} nB={B['n'][eB][w]:+.3f}"
                  f"  ampA={A['amp_eff'][eA][w]:.3f} ampB={B['amp_eff'][eB][w]:.3f}"
                  f"  sgA={A['smooth_gain'][eA][w]:.3f} sgB={B['smooth_gain'][eB][w]:.3f}", flush=True)


if __name__ == "__main__":
    main()
