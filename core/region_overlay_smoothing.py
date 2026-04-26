"""region_overlay_smoothing.py — S70.

Organic-edge smoothing for painted palette-ID arrays (e.g.
`masks/lithology_region.png`). Mirrors the override.tif pipeline
(median + boundary-jitter) but preserves zero pixels as pure
pass-through (no paint invasion / erosion).

Two passes:
  1. Median filter — only swaps boundary pixels to an existing
     neighbour ID. Uses a small kernel so thin painted strips survive.
  2. Boundary jitter — sparsely adopts a random 4-neighbour ID
     along class boundaries. Produces natural-looking scatter at
     transitions.

No phantom IDs introduced. Zero (unpainted) pixels never swap with
non-zero pixels — the mask of "painted or not" is preserved exactly.
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import binary_opening, convolve, median_filter


def _boundary_jitter_nonzero(arr: np.ndarray, passes: int, prob: float,
                             seed: int) -> np.ndarray:
    """Boundary-jitter variant that only swaps between non-zero IDs.

    Zero pixels never change; non-zero pixels never adopt zero.
    Modifies ``arr`` in place and returns it.
    """
    rng = np.random.default_rng(seed)
    h, w = arr.shape
    # Work on the interior (1-px border ignored to keep neighbour lookups simple).
    for _p in range(passes):
        a = arr[1:-1, 1:-1]
        up = arr[:-2, 1:-1]
        dn = arr[2:, 1:-1]
        lt = arr[1:-1, :-2]
        rt = arr[1:-1, 2:]

        # A pixel is a candidate if it's non-zero AND has a different non-zero neighbour.
        nonzero = a != 0
        diff_up = (up != a) & (up != 0) & nonzero
        diff_dn = (dn != a) & (dn != 0) & nonzero
        diff_lt = (lt != a) & (lt != 0) & nonzero
        diff_rt = (rt != a) & (rt != 0) & nonzero
        is_boundary = diff_up | diff_dn | diff_lt | diff_rt

        by, bx = np.where(is_boundary)
        if len(by) == 0:
            continue

        do_swap = rng.random(len(by), dtype=np.float32) < prob
        by = by[do_swap]
        bx = bx[do_swap]
        if len(by) == 0:
            continue

        # Per candidate, pick a random non-zero neighbour direction.
        # Build a per-pixel list of valid directions.
        # Dir 0=up, 1=down, 2=left, 3=right. Try 4 shuffled and pick first non-zero.
        choices = rng.integers(0, 4, size=len(by), dtype=np.uint8)
        # Neighbour lookups (interior coords = by, bx ; full-array coords = by+1, bx+1).
        fby = by + 1
        fbx = bx + 1
        nbr_up = arr[fby - 1, fbx]
        nbr_dn = arr[fby + 1, fbx]
        nbr_lt = arr[fby, fbx - 1]
        nbr_rt = arr[fby, fbx + 1]
        nbrs = np.stack([nbr_up, nbr_dn, nbr_lt, nbr_rt], axis=1)
        chosen = nbrs[np.arange(len(by)), choices]

        # If the chosen direction is zero, keep trying other dirs.
        # Vectorised: for each row find first nonzero column in nbrs.
        nz_mask = nbrs != 0
        # Use the random choice if valid, else first non-zero.
        valid = chosen != 0
        if (~valid).any():
            # For invalid rows, pick the first nonzero neighbour.
            first_nz_col = np.argmax(nz_mask, axis=1)
            has_any = nz_mask.any(axis=1)
            fallback = nbrs[np.arange(len(by)), first_nz_col]
            chosen = np.where(valid, chosen, np.where(has_any, fallback, arr[fby, fbx]))

        arr[fby, fbx] = chosen
    return arr


def smooth_region_paint(arr: np.ndarray, *,
                        median_kernel: int = 7,
                        jitter_passes: int = 4,
                        jitter_prob: float = 0.5,
                        seed: int = 137) -> np.ndarray:
    """Return an organic-edge smoothed copy of a painted ID array.

    Mirrors override.tif pipeline at source resolution:
      1. Median filter (boundary softener, small kernel).
      2. Boundary jitter (organic scatter).

    Zero pixels are preserved verbatim — they represent "unpainted,
    pass-through to derived value" and must not be invaded or erased.

    Parameters
    ----------
    arr : uint8 (H, W)
        Palette-ID array. 0 = unpainted, 1..N = painted IDs.
    median_kernel : int
        Odd kernel size for median filter. 7 is a light touch; 11-15
        more aggressive. Median is applied only inside the non-zero
        region (outside-the-mask pixels stay 0).
    jitter_passes : int
        Number of boundary-jitter passes. Each pass moves boundary
        pixels by at most 1 pixel, so N passes = N-pixel scatter radius.
    jitter_prob : float
        Per-pixel probability of swapping with a random non-zero
        neighbour on each pass.
    seed : int
        RNG seed for reproducibility.

    Returns
    -------
    uint8 (H, W) smoothed array — zero-mask identical to input.
    """
    if arr.dtype != np.uint8:
        arr = arr.astype(np.uint8)
    out = arr.copy()

    # Median — restricted to painted region so zero-mask is preserved.
    nonzero_mask = out != 0
    if nonzero_mask.any() and median_kernel >= 3:
        med = median_filter(out, size=median_kernel)
        # Only update where input was non-zero (keep zero pass-through).
        # Also only adopt median value if it is itself non-zero (else
        # the painted pixel would be erased).
        adopt = nonzero_mask & (med != 0)
        out[adopt] = med[adopt]

    # Boundary jitter — non-zero pixels only; zero boundary preserved.
    if jitter_passes > 0:
        _boundary_jitter_nonzero(out, jitter_passes, jitter_prob, seed)

    return out


def clean_painted_river_mask(painted_mask: np.ndarray, *,
                             opening_radius: int = 2,
                             prune_max_branch_len: int = 8) -> np.ndarray:
    """Return a cleaned 1-pixel skeleton of a user-painted river mask.

    Two-stage cleanup for brush-painted river paths where a fat brush
    can produce clover / asterisk artifacts after skeletonization:

    1. **Morphological opening** — removes isolated small specks and
       thin necks between overlapping brush dabs. Strokes wider than
       ~ ``2*opening_radius + 1`` pixels survive intact.
    2. **Skeletonize + iterative endpoint peel** — runs
       ``prune_max_branch_len`` passes where each pass removes every
       pixel with exactly one skeleton neighbour. Branches whose full
       length is ≤ ``prune_max_branch_len`` vanish entirely. Longer
       branches lose that many pixels from each tip; the main body
       survives.

    Trade-off: set ``prune_max_branch_len`` above the shortest real
    painted segment length, but low enough that long rivers don't lose
    a critical tip. Default 8 at 8192 source ≈ 49 blocks of tip trim.

    Parameters
    ----------
    painted_mask : bool or uint8 (H, W)
        True / non-zero where the user painted this river class.
    opening_radius : int
        Iterations of erosion-then-dilation 3×3 structuring element.
        0 skips opening. Default 2 removes 2-px-radius specks.
    prune_max_branch_len : int
        Endpoint-peel iterations. 0 skips pruning. Default 8.

    Returns
    -------
    bool (H, W) — 1-pixel skeleton, organic and clover-free.
    """
    from skimage.morphology import skeletonize

    mask = np.asarray(painted_mask, dtype=bool).copy()
    if not mask.any():
        return mask

    if opening_radius > 0:
        mask = binary_opening(mask, iterations=opening_radius)
        if not mask.any():
            return mask

    skel = skeletonize(mask)
    if prune_max_branch_len <= 0 or not skel.any():
        return skel

    k = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], dtype=np.uint8)
    s = skel.astype(np.uint8)
    for _ in range(prune_max_branch_len):
        nbrs = convolve(s, k, mode="constant", cval=0)
        endpoints = (s == 1) & (nbrs == 1)
        if not endpoints.any():
            break
        s[endpoints] = 0
    return s.astype(bool)
