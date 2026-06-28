"""seam_metric.py — quantify the INTERIOR tile-to-tile seam in a rendered island
block by reading top-block-Y directly from the 4 (or NxN) MCAs and comparing the
shared tile-edge column/row against a mid-tile control. This is the objective
A/B number for the seam fix: after a code change, re-render the block with
render_seam_test.py then re-run this — the seam-edge mean|dY| and ge3 count should
fall toward the mid-tile control.

Default target = the efate interior 2x2 block from render_seam_test.py.

Usage:
    py islands/seam_metric.py
    py islands/seam_metric.py --name efate --tx0 5 --ty0 7 --nx 2 --ny 2
"""
import sys, json, argparse
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.topdown_fast import read_chunk, top_rgb_for_chunk
from islands.render_drive import MASKS_OUT, OUT, _safe, _snap, TILE


def _seam(a, b, nm):
    m = (a > 0) & (b > 0)
    if m.sum() == 0:
        print(f"  {nm}: no shared land"); return None
    d = np.abs(a[m].astype(float) - b[m].astype(float))
    print(f"  {nm}: n={int(m.sum())} mean|dY|={d.mean():.3f} "
          f"p95={np.percentile(d,95):.1f} max={d.max():.0f} "
          f"ge3={int((d>=3).sum())} ge5={int((d>=5).sum())}")
    return d.mean()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="efate")
    ap.add_argument("--tx0", type=int, default=5)
    ap.add_argument("--ty0", type=int, default=7)
    ap.add_argument("--nx", type=int, default=2)
    ap.add_argument("--ny", type=int, default=2)
    a = ap.parse_args()

    layout = json.loads((ROOT / "islands" / "layout.json").read_text())
    entry = next(i for i in layout["islands"]
                 if a.name in _safe(i["name"]) or a.name in i["dem_path"])
    name = _safe(entry["name"])
    odir = OUT / name
    sox, soz = _snap(entry["world_offset_px"][0]), _snap(entry["world_offset_px"][1])

    x0 = sox + a.tx0 * TILE; z0 = soz + a.ty0 * TILE
    W = a.nx * TILE; H = a.ny * TILE
    topY = np.full((H, W), -32768, np.int16)
    for dy in range(a.ny):
        for dx in range(a.nx):
            rx = (sox + (a.tx0 + dx) * TILE) // TILE
            rz = (soz + (a.ty0 + dy) * TILE) // TILE
            mca = odir / f"r.{rx}.{rz}.mca"
            if not mca.exists():
                print(f"  MISSING {mca.name}"); continue
            with open(mca, "rb") as f:
                for lz in range(32):
                    for lx in range(32):
                        try:
                            ch = read_chunk(f, lx, lz)
                        except Exception:
                            ch = None
                        if ch is None:
                            continue
                        _, has, ty = top_rgb_for_chunk(ch)
                        cwx = (rx * 32 + lx) * 16; cwz = (rz * 32 + lz) * 16
                        for zz in range(16):
                            for xx in range(16):
                                if not has[zz, xx]:
                                    continue
                                oy = cwz + zz - z0; ox = cwx + xx - x0
                                if 0 <= oy < H and 0 <= ox < W:
                                    topY[oy, ox] = ty[zz, xx]

    land = topY > 0
    print(f"block {name} {a.nx}x{a.ny} @ local ({a.tx0},{a.ty0}); "
          f"land {100*land.sum()/topY.size:.1f}%")
    for i in range(1, a.nx):
        sc = i * TILE
        print(f"VERTICAL interior seam at local col {sc} (world-X {x0+sc}):")
        _seam(topY[:, sc-1], topY[:, sc], "seam edge")
        cc = max(1, sc // 2)
        _seam(topY[:, cc-1], topY[:, cc], "mid-tile control")
    for j in range(1, a.ny):
        sr = j * TILE
        print(f"HORIZONTAL interior seam at local row {sr} (world-Z {z0+sr}):")
        _seam(topY[sr-1, :], topY[sr, :], "seam edge")
        rc = max(1, sr // 2)
        _seam(topY[rc-1, :], topY[rc, :], "mid-tile control")


if __name__ == "__main__":
    main()
