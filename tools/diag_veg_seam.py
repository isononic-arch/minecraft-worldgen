"""diag_veg_seam.py — S91 regression #2 assessment: quantify vegetation /
schematic / ground-cover discontinuities across a tile boundary.

Renders two adjacent tiles dry-run (SURF_DUMP_DIR + SURF_DUMP_SCHEM early-return
in run_pipeline._process_tile), then reports four independent seam probes:

  1. FIELD CHECK  — exact replica of place_schematics' density_mult fBm
                    (incl. the per-tile min-max normalization at
                    schematic_placement.py:1068) → measure the step at the
                    shared edge vs interior column-to-column variation.
  2. SBLK PROFILE — per-column surface-block composition; seam-step z-score
                    per block type (z >> 4 = real discontinuity).
  3. GC PROFILE   — ground-cover density + top-type rates, same treatment.
  4. PLACEMENTS   — tree/bush linear density vs distance from seam (8-px bins);
                    border gap/spike; count of footprints crossing the boundary
                    (silently clipped by chunk_writer.stamp_schematic =
                    half-trees in-world).

Usage:
  py tools/diag_veg_seam.py 50,50 51,50                 # render + analyze
  py tools/diag_veg_seam.py 50,50 51,50 --compare-only  # re-analyze dumps
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DUMP = os.path.abspath("diag_veg_seam_out")
os.environ["SURF_DUMP_DIR"] = DUMP
os.environ["SURF_DUMP_SCHEM"] = "1"
os.makedirs(DUMP, exist_ok=True)

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH = os.path.join(ROOT, "config", "thresholds.json")


def _args(tx, tz):
    return {
        "tile_x": tx, "tile_y": tz,
        "config_path": CFG_PATH,
        "masks_dir": os.path.join(ROOT, "masks"),
        "schem_index_path": os.path.join(ROOT, "schematic_index.json"),
        "output_dir": os.path.join(ROOT, "output"),
        "tile_size": 512, "dry_run": True,
    }


def run(tx, tz):
    import run_pipeline as RP
    print(f"\n=== dry-run dump tile ({tx},{tz}) ===", flush=True)
    RP._process_tile(_args(tx, tz))


# ---------------------------------------------------------------------------
# 1. FIELD CHECK — density_mult normalization step
# ---------------------------------------------------------------------------

def field_check(a, b):
    import opensimplex as ox
    from opensimplex import OpenSimplex
    cfg = json.load(open(CFG_PATH))
    seeds = cfg.get("noise_seeds", {})
    cfg_seed = int(seeds.get("decoration_density", 42002))
    # replicate place_schematics: base_seed = getattr(den_gen, '_seed', 42002)
    base_seed = getattr(OpenSimplex(seed=cfg_seed), "_seed", 42002)
    den_cfg = cfg.get("decoration_density_noise",
                      {"scale": 60, "octaves": 3, "floor": 0.15})
    scale = float(den_cfg.get("scale", 60))
    octs = int(den_cfg.get("octaves", 3))
    floor = float(den_cfg.get("floor", 0.15))

    def tile_fields(tx, tz):
        W = H = 512
        xs = (np.arange(W, dtype=np.float64) + tx * W) / scale
        ys = (np.arange(H, dtype=np.float64) + tz * H) / scale
        acc = np.zeros((H, W), dtype=np.float64)
        amp, freq = 1.0, 1.0
        for o in range(octs):
            ox.seed(base_seed + o * 7919)
            acc += ox.noise2array(xs * freq, ys * freq) * amp
            amp *= 0.5
            freq *= 2.0
        # S91 fix mirror: FIXED +/-1.4 normalization (matches
        # schematic_placement.py post-fix; was per-tile min-max)
        norm = np.clip((acc + 1.4) / 2.8, 0.0, 1.0)
        return (acc.astype(np.float32),
                (floor + (1 - floor) * norm).astype(np.float32),
                (acc.min(), acc.max()))

    (ax, az), (bx, bz) = a, b
    rawA, dmA, rngA = tile_fields(ax, az)
    rawB, dmB, rngB = tile_fields(bx, bz)
    if az == bz and bx - ax == 1:    # vertical seam: A col511 | B col0
        eA_raw, eB_raw = rawA[:, 511], rawB[:, 0]
        eA, eB = dmA[:, 511], dmB[:, 0]
        interior = np.abs(np.diff(dmA, axis=1)).mean(), np.abs(np.diff(dmB, axis=1)).mean()
    else:                            # horizontal seam: A row511 | B row0
        eA_raw, eB_raw = rawA[511, :], rawB[0, :]
        eA, eB = dmA[511, :], dmB[0, :]
        interior = np.abs(np.diff(dmA, axis=0)).mean(), np.abs(np.diff(dmB, axis=0)).mean()

    print("\n--- 1. FIELD CHECK: density_mult (per-tile min-max normalization) ---")
    print(f"  raw fBm seam diff (continuity sanity): mean={np.abs(eA_raw-eB_raw).mean():.5f}")
    print(f"  tile A norm range (lo,hi): ({rngA[0]:+.4f}, {rngA[1]:+.4f})")
    print(f"  tile B norm range (lo,hi): ({rngB[0]:+.4f}, {rngB[1]:+.4f})")
    step = np.abs(eA - eB)
    print(f"  density_mult SEAM STEP: mean={step.mean():.4f}  max={step.max():.4f}")
    print(f"  density_mult interior col-to-col diff: A={interior[0]:.4f}  B={interior[1]:.4f}")
    ratio = step.mean() / max(interior[0], 1e-9)
    print(f"  => seam step is {ratio:.1f}x the interior variation"
          f"  ({'SEAM' if ratio > 3 else 'ok'})")


# ---------------------------------------------------------------------------
# 2/3. SBLK + GC composition profiles
# ---------------------------------------------------------------------------

def _col_profiles(A, B, vertical):
    """Stack A|B along the seam-perpendicular axis → (n, 1024) string array."""
    if not vertical:
        A, B = A.T, B.T
    return np.concatenate([A, B], axis=1)


def composition_step(name, A, B, vertical, top_k=10, as_density=None):
    grid = _col_profiles(np.asarray(A, dtype=object), np.asarray(B, dtype=object), vertical)
    grid = np.where(grid == None, "", grid).astype(str)  # noqa: E711
    print(f"\n--- {name}: per-column composition seam step ---")
    if as_density is not None:
        dens = (grid != "") & (grid != as_density)
        f = dens.mean(axis=0)
        d = np.abs(np.diff(f))
        bg = np.delete(d, 511)
        z = (d[511] - bg.mean()) / max(bg.std(), 1e-9)
        print(f"  overall density: seam step={d[511]:.4f}  interior mean={bg.mean():.4f}"
              f"  z={z:+.1f}  {'<<< SEAM' if z > 4 else ''}")
    vals, counts = np.unique(grid, return_counts=True)
    order = np.argsort(-counts)
    flagged = 0
    for vi in order[:top_k]:
        v = vals[vi]
        if v == "":
            continue
        f = (grid == v).mean(axis=0)          # per-column frequency, 1024 cols
        d = np.abs(np.diff(f))                # adjacent col diffs; idx 511 = seam
        bg = np.delete(d, 511)
        sd = max(bg.std(), 1e-9)
        z = (d[511] - bg.mean()) / sd
        mark = "<<< SEAM" if z > 4 and d[511] > 0.01 else ""
        if mark:
            flagged += 1
        print(f"  {v:<22} A-side={f[:512].mean():.3f} B-side={f[512:].mean():.3f}"
              f"  seam-step={d[511]:.4f} (bg {bg.mean():.4f}±{sd:.4f}) z={z:+5.1f} {mark}")
    return flagged


# ---------------------------------------------------------------------------
# 4. PLACEMENTS
# ---------------------------------------------------------------------------

def placement_analysis(a, b, vertical):
    from core.schematic_placement import CANOPY_RADIUS
    (ax, az), (bx, bz) = a, b
    P_A = np.load(f"{DUMP}/plc_{ax}_{az}.npy", allow_pickle=True)
    P_B = np.load(f"{DUMP}/plc_{bx}_{bz}.npy", allow_pickle=True)
    seam = (bx * 512) if vertical else (bz * 512)  # B's low edge in world coords
    axis = 0 if vertical else 1                    # world_x or world_z

    print(f"\n--- 4. PLACEMENTS: linear density vs distance from seam ---")
    print(f"  tile A ({ax},{az}): {len(P_A)} placements   tile B ({bx},{bz}): {len(P_B)}")

    from core.schematic_loader import load_schem as _ld
    _ext_cache: dict = {}

    def _extent(path, size):
        e = _ext_cache.get(path)
        if e is None:
            try:
                s = _ld(path)
                e = int(max(s.blocks.shape[1], s.blocks.shape[2]))
            except Exception:
                e = 2 * {"sm": 2, "md": 3, "lg": 4}.get(size, 3) + 5
            _ext_cache[path] = e
        return e

    for pass_type in ("tree", "bush"):
        dists = []
        clipped = 0
        clip_by_size = {}
        for P in (P_A, P_B):
            for rec in P:
                wx, wz, py, size, stype, species, path = rec
                if stype != pass_type:
                    continue
                d = (wx if vertical else wz) - seam
                dists.append(d)
                # TRUE clip: stamp anchors at the corner with one-sided
                # extent, so ONLY the low-side tile's anchors within extent
                # of the seam actually get cut (d in [-ext, 0)). The high
                # side's anchors at d>=0 extend AWAY from the seam.
                if -_extent(path, size) < d < 0:
                    clipped += 1
                    clip_by_size[size] = clip_by_size.get(size, 0) + 1
        dists = np.asarray(dists)
        n_all = len(dists)
        if n_all == 0:
            print(f"  [{pass_type}] none placed")
            continue
        # 8-px bins over ±64
        bins = np.arange(-64, 65, 8)
        hist, _ = np.histogram(dists, bins=bins)
        labels = [f"{bins[i]:+d}" for i in range(len(hist))]
        # background expectation per bin from ±(64..256) px band
        far = ((np.abs(dists) >= 64) & (np.abs(dists) < 256)).sum() / (2 * 192 / 8)
        print(f"  [{pass_type}] n={n_all}  far-field mean/8px-bin={far:.1f}")
        print(f"    bins(-64..+64): {list(hist)}")
        near = hist[7] + hist[8]   # the two bins touching the seam
        exp = 2 * far
        sig = (near - exp) / max(np.sqrt(exp), 1e-9)
        verdict = "GAP <<< SEAM" if sig < -3 else ("SPIKE <<< SEAM" if sig > 3 else "ok")
        print(f"    seam-adjacent 2 bins: n={near} vs expected {exp:.1f}  ({sig:+.1f} sigma) {verdict}")
        if pass_type == "tree":
            print(f"    footprints CROSSING boundary (clipped by stamp_schematic): "
                  f"{clipped}  by size: {clip_by_size}")


def main():
    tiles = []
    for s in sys.argv[1:]:
        if s == "--compare-only":
            continue
        tx, tz = s.split(",")
        tiles.append((int(tx), int(tz)))
    assert len(tiles) == 2, "need exactly two adjacent tiles"
    a, b = tiles
    vertical = (a[1] == b[1] and b[0] - a[0] == 1)
    horizontal = (a[0] == b[0] and b[1] - a[1] == 1)
    assert vertical or horizontal, "tiles must be adjacent (A left/above B)"

    if "--compare-only" not in sys.argv:
        for t in tiles:
            run(*t)

    field_check(a, b)
    for nm, pre in (("2. SBLK", "sblk"), ("3. GROUND COVER", "gc")):
        A = np.load(f"{DUMP}/{pre}_{a[0]}_{a[1]}.npy", allow_pickle=True)
        B = np.load(f"{DUMP}/{pre}_{b[0]}_{b[1]}.npy", allow_pickle=True)
        composition_step(nm, A, B, vertical,
                         as_density=("" if pre == "gc" else None))
    placement_analysis(a, b, vertical)


if __name__ == "__main__":
    main()
