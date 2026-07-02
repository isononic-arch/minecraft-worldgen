"""fast_simplex.py — S101: vectorized, BIT-IDENTICAL OpenSimplex 2D array noise.

WHY: cProfile of a dense land tile showed opensimplex's pure-Python per-point
loop (`_noise2a` -> `_noise2` -> `_extrapolate2`) = 234s of a 382s tile (61%).
numba (opensimplex's own accelerator) does not support Python 3.14, so this is
a numpy port of `opensimplex/internals.py:_noise2` evaluated over the whole
grid at once.

BIT-IDENTITY ARGUMENT (why exact equality is achievable, not approximate):
every output point's arithmetic is INDEPENDENT — vectorizing across points
reorders nothing within a point. Each expression below reproduces the scalar
source's operation order per lane; branch selection uses np.where on the same
predicates the scalar `if`s use; conditional accumulations use
`np.where(mask, value + c, value)` (NOT `value += mask*c`) so skipped lanes
perform no float add at all, exactly like the scalar skip. Verified bitwise by
`tools/diag_fast_simplex_equiv.py` across seeds/scales/offsets (run it after
ANY edit here or any opensimplex version bump).

KILL SWITCH: set env VANDIR_FAST_NOISE=0 to skip the patch (falls back to the
pure-Python reference).
"""
from __future__ import annotations

import os

import numpy as np
import opensimplex
from opensimplex.constants import (GRADIENTS2, NORM_CONSTANT2,
                                   SQUISH_CONSTANT2, STRETCH_CONSTANT2)

_GRAD2 = GRADIENTS2.astype(np.float64)   # int64 {±5, ±2}: exact in float64
_SQ2 = 2 * SQUISH_CONSTANT2              # same value every scalar evaluation computes inline


def _extrapolate2_vec(perm, xsb, ysb, dx, dy):
    # index = perm[(perm[xsb & 0xFF] + ysb) & 0xFF] & 0x0E  — int64 two's-
    # complement & matches Python's semantics for negative lattice coords.
    index = perm[(perm[xsb & 0xFF] + ysb) & 0xFF] & 0x0E
    return _GRAD2[index] * dx + _GRAD2[index + 1] * dy


def _noise2a_vec(x, y, perm):
    """Vectorized twin of opensimplex.internals._noise2a: x (W,), y (H,) -> (H, W)."""
    X = np.asarray(x, dtype=np.float64)[None, :]
    Y = np.asarray(y, dtype=np.float64)[:, None]

    stretch_offset = (X + Y) * STRETCH_CONSTANT2
    xs = X + stretch_offset
    ys = Y + stretch_offset

    xsb_f = np.floor(xs)
    ysb_f = np.floor(ys)
    xsb = xsb_f.astype(np.int64)
    ysb = ysb_f.astype(np.int64)

    squish_offset = (xsb_f + ysb_f) * SQUISH_CONSTANT2
    xb = xsb_f + squish_offset
    yb = ysb_f + squish_offset

    xins = xs - xsb_f
    yins = ys - ysb_f
    in_sum = xins + yins

    dx0 = X - xb
    dy0 = Y - yb

    value = np.zeros(np.broadcast(dx0, dy0).shape, dtype=np.float64)
    dx0, dy0 = np.broadcast_arrays(dx0, dy0)
    xsb, ysb = np.broadcast_arrays(xsb, ysb)
    dx0 = dx0.copy(); dy0 = dy0.copy()          # mutated in the (1,1) branch
    xsb = xsb.copy(); ysb = ysb.copy()

    # Contribution (1,0)
    dx1 = dx0 - 1 - SQUISH_CONSTANT2
    dy1 = dy0 - 0 - SQUISH_CONSTANT2
    attn1 = 2 - dx1 * dx1 - dy1 * dy1
    m1 = attn1 > 0
    a1 = attn1 * attn1
    value = np.where(m1, value + a1 * a1 * _extrapolate2_vec(perm, xsb + 1, ysb + 0, dx1, dy1), value)

    # Contribution (0,1)
    dx2 = dx0 - 0 - SQUISH_CONSTANT2
    dy2 = dy0 - 1 - SQUISH_CONSTANT2
    attn2 = 2 - dx2 * dx2 - dy2 * dy2
    m2 = attn2 > 0
    a2 = attn2 * attn2
    value = np.where(m2, value + a2 * a2 * _extrapolate2_vec(perm, xsb + 0, ysb + 1, dx2, dy2), value)

    # Region / extra-vertex selection (branch predicates mirror the scalar ifs;
    # ext candidates are computed from the PRE-SHIFT dx0/dy0, as in the source).
    mB = ~(in_sum <= 1)                              # else-branch: triangle at (1,1)
    zins = np.where(mB, 2 - in_sum, 1 - in_sum)
    outer = np.where(mB, (zins < xins) | (zins < yins), (zins > xins) | (zins > yins))
    condX = xins > yins

    xsv_ext = np.where(
        mB,
        np.where(outer, np.where(condX, xsb + 2, xsb + 0), xsb),
        np.where(outer, np.where(condX, xsb + 1, xsb - 1), xsb + 1))
    ysv_ext = np.where(
        mB,
        np.where(outer, np.where(condX, ysb + 0, ysb + 2), ysb),
        np.where(outer, np.where(condX, ysb - 1, ysb + 1), ysb + 1))
    dx_ext = np.where(
        mB,
        np.where(outer, np.where(condX, dx0 - 2 - _SQ2, dx0 + 0 - _SQ2), dx0),
        np.where(outer, np.where(condX, dx0 - 1, dx0 + 1), dx0 - 1 - _SQ2))
    dy_ext = np.where(
        mB,
        np.where(outer, np.where(condX, dy0 + 0 - _SQ2, dy0 - 2 - _SQ2), dy0),
        np.where(outer, np.where(condX, dy0 + 1, dy0 - 1), dy0 - 1 - _SQ2))

    # (1,1)-branch origin shift, applied after ext candidates (as in the source)
    xsb = np.where(mB, xsb + 1, xsb)
    ysb = np.where(mB, ysb + 1, ysb)
    dx0 = np.where(mB, dx0 - 1 - _SQ2, dx0)
    dy0 = np.where(mB, dy0 - 1 - _SQ2, dy0)

    # Contribution (0,0) or (1,1)
    attn0 = 2 - dx0 * dx0 - dy0 * dy0
    m0 = attn0 > 0
    a0 = attn0 * attn0
    value = np.where(m0, value + a0 * a0 * _extrapolate2_vec(perm, xsb, ysb, dx0, dy0), value)

    # Extra vertex
    attn_ext = 2 - dx_ext * dx_ext - dy_ext * dy_ext
    mE = attn_ext > 0
    aE = attn_ext * attn_ext
    value = np.where(mE, value + aE * aE * _extrapolate2_vec(perm, xsv_ext, ysv_ext, dx_ext, dy_ext), value)

    return value / NORM_CONSTANT2


_installed = False
_reference_noise2array = opensimplex.OpenSimplex.noise2array   # pre-patch, for A/B


def install():
    """Monkeypatch OpenSimplex.noise2array with the vectorized twin. Idempotent.
    Reaches every existing and future instance (they all dispatch through the
    class). Disable with VANDIR_FAST_NOISE=0."""
    global _installed
    if _installed or os.environ.get("VANDIR_FAST_NOISE", "1") == "0":
        return
    def noise2array(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        return _noise2a_vec(x, y, self._perm)
    opensimplex.OpenSimplex.noise2array = noise2array
    _installed = True
