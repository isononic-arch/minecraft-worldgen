"""_seam_offline_experiment.py — OFFLINE fast reproduction of the decorate
biome-boundary Y-smoother seam, using SURF_DUMP arrays from TWO adjacent tiles.

Reproduces _smooth_all_biome_boundaries_y (the decorate amplifier) on each tile,
with selectable HALO strategy, and measures the resulting cross-tile seam WITHOUT
re-rendering through the slow pipeline (seconds vs ~10 min/tile).

Inputs per tile from the dump dir: sy_pre (live surface entering decorate),
bg (inner biome_grid), rrp (pre-carve padded, pad 96).

Halo strategies for the iterative blur (_gf_seam equivalent):
  precarve  = current production: splice live inner into PRE-CARVE neighbour halo
  neighbour = FIX: splice live inner into REAL neighbour-tile live-surface halo
  none      = per-tile mode='nearest' (no halo)

Usage:
    py islands/_seam_offline_experiment.py "<dump_dir>" --ax 5 --ay 7 --bx 6 --by 7
"""
import sys, argparse
from pathlib import Path
import numpy as np
from scipy.ndimage import binary_dilation, gaussian_filter, distance_transform_edt

SEA = 63
PAD96 = 96
BUFFER = 24
SIGMA = 8.0
PASSES = 3
ECO_PAD = 48


def _load(dump, kind, tx, ty):
    p = Path(dump) / f"{kind}_{tx}_{ty}.npy"
    if not p.exists():
        raise FileNotFoundError(p)
    return np.load(p, allow_pickle=("bg" in kind))


def _build_seam_halo(inner, neighbour_inner, side, pad):
    """Build a (H+2p, W+2p) halo for `inner` whose ring on `side` carries the
    real neighbour-tile values. side in {'right','left','bottom','top'}.
    Far rings (no real neighbour) edge-replicate (mode='nearest' equivalent)."""
    H, W = inner.shape
    halo = np.empty((H + 2 * pad, W + 2 * pad), np.float32)
    # start with edge-replicate of inner (nearest)
    halo[pad:pad + H, pad:pad + W] = inner
    # replicate edges outward
    halo[:pad, pad:pad + W] = inner[0:1, :]
    halo[pad + H:, pad:pad + W] = inner[-1:, :]
    halo[pad:pad + H, :pad] = inner[:, 0:1]
    halo[pad:pad + H, pad + W:] = inner[:, -1:]
    # corners
    halo[:pad, :pad] = inner[0, 0]; halo[:pad, pad + W:] = inner[0, -1]
    halo[pad + H:, :pad] = inner[-1, 0]; halo[pad + H:, pad + W:] = inner[-1, -1]
    if neighbour_inner is not None:
        if side == "right":   # neighbour is to the RIGHT: fill right ring with its left cols
            halo[pad:pad + H, pad + W:pad + W + pad] = neighbour_inner[:, :pad]
        elif side == "left":
            halo[pad:pad + H, :pad] = neighbour_inner[:, W - pad:W]
        elif side == "bottom":
            halo[pad + H:pad + H + pad, pad:pad + W] = neighbour_inner[:pad, :]
        elif side == "top":
            halo[:pad, pad:pad + W] = neighbour_inner[H - pad:H, :]
    return halo


def _gf_seam(sy_f, sigma, halo, pad, mode="nearest"):
    if halo is None:
        return gaussian_filter(sy_f, sigma=sigma, mode=mode)
    H, W = sy_f.shape
    work = halo.astype(np.float32, copy=True)
    work[pad:pad + H, pad:pad + W] = sy_f
    return gaussian_filter(work, sigma=sigma, mode=mode)[pad:pad + H, pad:pad + W]


def _smooth(sy, bg_padded, eco_pad, halo, pad):
    """Reproduce _smooth_all_biome_boundaries_y default (iterative-blur) path."""
    H, W = sy.shape
    bg = bg_padded
    boundary = np.zeros(bg.shape, dtype=bool)
    boundary[:-1, :] |= (bg[:-1, :] != bg[1:, :]); boundary[1:, :] |= (bg[:-1, :] != bg[1:, :])
    boundary[:, :-1] |= (bg[:, :-1] != bg[:, 1:]); boundary[:, 1:] |= (bg[:, :-1] != bg[:, 1:])
    ring = binary_dilation(boundary, iterations=BUFFER)
    dist = distance_transform_edt(~boundary).astype(np.float32)
    weight = np.clip(1.0 - dist / max(float(BUFFER), 1.0), 0.0, 1.0)
    weight[~ring] = 0.0
    weight = weight[eco_pad:eco_pad + H, eco_pad:eco_pad + W]
    sy_f = sy.astype(np.float32)
    for _ in range(PASSES):
        blurred = _gf_seam(sy_f, SIGMA, halo, pad)
        sy_f = weight * blurred + (1.0 - weight) * sy_f
    return np.round(sy_f).astype(sy.dtype)


def _pad_bg(inner_a, inner_b, side, eco_pad):
    """Build (H+2*eco_pad, W+2*eco_pad) biome grid for inner_a with real neighbour
    on `side`, edge-replicate elsewhere."""
    H, W = inner_a.shape
    bgp = np.empty((H + 2 * eco_pad, W + 2 * eco_pad), dtype=object)
    bgp[eco_pad:eco_pad + H, eco_pad:eco_pad + W] = inner_a
    bgp[:eco_pad, eco_pad:eco_pad + W] = inner_a[0:1, :]
    bgp[eco_pad + H:, eco_pad:eco_pad + W] = inner_a[-1:, :]
    bgp[eco_pad:eco_pad + H, :eco_pad] = inner_a[:, 0:1]
    bgp[eco_pad:eco_pad + H, eco_pad + W:] = inner_a[:, -1:]
    bgp[:eco_pad, :eco_pad] = inner_a[0, 0]; bgp[:eco_pad, eco_pad + W:] = inner_a[0, -1]
    bgp[eco_pad + H:, :eco_pad] = inner_a[-1, 0]; bgp[eco_pad + H:, eco_pad + W:] = inner_a[-1, -1]
    if inner_b is not None:
        if side == "right":
            bgp[eco_pad:eco_pad + H, eco_pad + W:] = inner_b[:, :eco_pad]
        elif side == "left":
            bgp[eco_pad:eco_pad + H, :eco_pad] = inner_b[:, W - eco_pad:]
        elif side == "bottom":
            bgp[eco_pad + H:, eco_pad:eco_pad + W] = inner_b[:eco_pad, :]
        elif side == "top":
            bgp[:eco_pad, eco_pad:eco_pad + W] = inner_b[H - eco_pad:, :]
    return bgp


def _smooth_full_halo(syA, syB, bgA, bgB, side, pad):
    """DEFINITIVE seam-clean test: run the ENTIRE smoother (weight+blur+blend) on a
    padded surface+biome grid built from REAL neighbour data, then crop to inner.
    Both tiles, processed this way, are byte-identical in the overlap -> the seam
    pixel is computed identically -> zero seam BY CONSTRUCTION. Returns (outA_edge,
    outB_edge)."""
    H, W = syA.shape

    def _pad_arr(a, b, side, pad, fill_obj=False):
        if fill_obj:
            out = np.empty((H + 2 * pad, W + 2 * pad), dtype=object)
        else:
            out = np.empty((H + 2 * pad, W + 2 * pad), np.float32)
        out[pad:pad + H, pad:pad + W] = a
        out[:pad, pad:pad + W] = a[0:1, :]; out[pad + H:, pad:pad + W] = a[-1:, :]
        out[pad:pad + H, :pad] = a[:, 0:1]; out[pad:pad + H, pad + W:] = a[:, -1:]
        out[:pad, :pad] = a[0, 0]; out[:pad, pad + W:] = a[0, -1]
        out[pad + H:, :pad] = a[-1, 0]; out[pad + H:, pad + W:] = a[-1, -1]
        if b is not None:
            if side == "right":   out[pad:pad + H, pad + W:] = b[:, :pad]
            elif side == "left":  out[pad:pad + H, :pad] = b[:, W - pad:]
            elif side == "bottom":out[pad + H:, pad:pad + W] = b[:pad, :]
            elif side == "top":   out[:pad, pad:pad + W] = b[H - pad:, :]
        return out

    def _run(a, b, bga, bgb, side):
        syp = _pad_arr(a.astype(np.float32), b.astype(np.float32), side, pad)
        bgp = _pad_arr(bga, bgb, side, pad, fill_obj=True)
        boundary = np.zeros(bgp.shape, dtype=bool)
        boundary[:-1, :] |= (bgp[:-1, :] != bgp[1:, :]); boundary[1:, :] |= (bgp[:-1, :] != bgp[1:, :])
        boundary[:, :-1] |= (bgp[:, :-1] != bgp[:, 1:]); boundary[:, 1:] |= (bgp[:, :-1] != bgp[:, 1:])
        ring = binary_dilation(boundary, iterations=BUFFER)
        dist = distance_transform_edt(~boundary).astype(np.float32)
        weight = np.clip(1.0 - dist / max(float(BUFFER), 1.0), 0.0, 1.0); weight[~ring] = 0.0
        sy_f = syp.copy()
        for _ in range(PASSES):
            blurred = gaussian_filter(sy_f, SIGMA, mode="nearest")
            sy_f = weight * blurred + (1.0 - weight) * sy_f
        return np.round(sy_f).astype(np.int16)[pad:pad + H, pad:pad + W]

    outA = _run(syA, syB, bgA, bgB, side)
    sideB = {"right": "left", "left": "right", "bottom": "top", "top": "bottom"}[side]
    outB = _run(syB, syA, bgB, bgA, sideB)
    return outA, outB


def _seam_dY(eaA, eaB):
    m = (eaA > SEA) & (eaB > SEA)
    if not m.any():
        return None
    d = np.abs(eaA[m].astype(float) - eaB[m].astype(float))
    return d


def _report(d, label):
    if d is None:
        print(f"    {label}: EMPTY"); return
    print(f"    {label}: n={d.size} mean|dY|={d.mean():.3f} "
          f"p95={np.percentile(d,95):.1f} max={d.max():.0f} "
          f"ge3={int((d>=3).sum())} ge5={int((d>=5).sum())}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--ax", type=int, default=5); ap.add_argument("--ay", type=int, default=7)
    ap.add_argument("--bx", type=int, default=6); ap.add_argument("--by", type=int, default=7)
    a = ap.parse_args()
    vertical = (a.ay == a.by)
    sideA = "right" if vertical else "bottom"
    sideB = "left" if vertical else "top"

    syA = _load(a.dump, "sy_pre", a.ax, a.ay).astype(np.int16)
    syB = _load(a.dump, "sy_pre", a.bx, a.by).astype(np.int16)
    bgA = _load(a.dump, "bg", a.ax, a.ay)
    bgB = _load(a.dump, "bg", a.bx, a.by)
    rrpA = _load(a.dump, "rrp", a.ax, a.ay).astype(np.float32)
    rrpB = _load(a.dump, "rrp", a.bx, a.by).astype(np.float32)
    H, W = syA.shape

    # raw seam BEFORE smoothing (live entering decorate)
    if vertical:
        rawA, rawB = syA[:, -1], syB[:, 0]
    else:
        rawA, rawB = syA[-1, :], syB[0, :]
    print("RAW seam (sy_pre, no decorate smoothing):")
    _report(_seam_dY(rawA, rawB), "raw seam")

    bgpA = _pad_bg(bgA, bgB, sideA, ECO_PAD)
    bgpB = _pad_bg(bgB, bgA, sideB, ECO_PAD)

    # ---- build the PRODUCTION post-erosion halo for each tile -------------
    # Mirror run_pipeline: erode the pre-carve padded field (rrp, pad96) with the
    # tile's flow + rock_layers at the SEAM_PAD window + correct world_origin,
    # then re-stamp the LIVE inner (= sy). RING = eroded pre-carve, INNER = live.
    eroded_haloA = eroded_haloB = None
    try:
        import sys as _sys, json as _json, re as _re
        from pathlib import Path as _P
        ROOT = _P(__file__).resolve().parent.parent
        _sys.path.insert(0, str(ROOT))
        import core.flow_erosion as _fe
        from core.tile_streamer import read_tile as _rt
        layout = _json.loads((ROOT / "islands" / "layout.json").read_text())
        entry = next(i for i in layout["islands"]
                     if "efate" in _re.sub(r"[^a-z0-9]+", "_", i["name"].lower()))
        sox = int(round(entry["world_offset_px"][0] / 512) * 512)
        soz = int(round(entry["world_offset_px"][1] / 512) * 512)
        cfg = _json.loads((ROOT / "islands" / "masks_islands"
                           / _re.sub(r"[^a-z0-9]+", "_", entry["name"].lower()).strip("_")
                           / "thresholds_island.json").read_text())
        mdir = str(ROOT / "islands" / "masks_islands"
                   / _re.sub(r"[^a-z0-9]+", "_", entry["name"].lower()).strip("_"))

        def _erode_halo(rrp, sy_inner, tx, ty):
            dim = H + 2 * PAD96
            fl = _rt(mdir, tx * 512, ty * 512, 512, 512, pad_px=PAD96,
                     mask_subset=["flow", "rock_layers"])
            flow = fl["flow"]; rock = fl["rock_layers"]
            wo = (sox + tx * 512 - PAD96, soz + ty * 512 - PAD96)
            er = _fe.apply_flow_erosion(rrp.astype(np.int16), flow, rock, None,
                                        cfg, tx, ty, pad=0, world_origin=wo)
            er = er.astype(np.float32)
            er[PAD96:PAD96 + H, PAD96:PAD96 + W] = sy_inner  # re-stamp live inner
            return er
        eroded_haloA = _erode_halo(rrpA, syA.astype(np.float32), a.ax, a.ay)
        eroded_haloB = _erode_halo(rrpB, syB.astype(np.float32), a.bx, a.by)
    except Exception as e:
        print(f"  (postcarve_eroded halo build FAILED: {type(e).__name__}: {e})")

    strats = ["precarve", "neighbour", "none"]
    if eroded_haloA is not None:
        strats.insert(1, "postcarve_eroded")
    for strat in strats:
        if strat == "precarve":
            haloA = rrpA.copy(); haloB = rrpB.copy(); pad = PAD96
        elif strat == "postcarve_eroded":
            haloA = eroded_haloA.copy(); haloB = eroded_haloB.copy(); pad = PAD96
        elif strat == "neighbour":
            haloA = _build_seam_halo(syA.astype(np.float32), syB.astype(np.float32), sideA, PAD96)
            haloB = _build_seam_halo(syB.astype(np.float32), syA.astype(np.float32), sideB, PAD96)
            pad = PAD96
        else:
            haloA = haloB = None; pad = 0
        outA = _smooth(syA.copy(), bgpA, ECO_PAD, haloA, pad)
        outB = _smooth(syB.copy(), bgpB, ECO_PAD, haloB, pad)
        if vertical:
            eA, eB = outA[:, -1], outB[:, 0]
        else:
            eA, eB = outA[-1, :], outB[0, :]
        print(f"STRATEGY={strat}: post-biome-boundary-smooth seam")
        _report(_seam_dY(eA, eB), "seam edge")

    # DEFINITIVE: full-halo processing (entire smoother on padded grid, crop)
    fhA, fhB = _smooth_full_halo(syA, syB, bgA, bgB, sideA, PAD96)
    if vertical:
        eA, eB = fhA[:, -1], fhB[:, 0]
    else:
        eA, eB = fhA[-1, :], fhB[0, :]
    print("STRATEGY=full_halo (entire smoother on padded grid): post-smooth seam")
    _report(_seam_dY(eA, eB), "seam edge")


if __name__ == "__main__":
    main()
