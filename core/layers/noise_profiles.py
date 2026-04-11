"""Shared noise profiles — Phase 0 scaffolding (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 2 "Noise profile: reuse legacy
mixed-forest noise field (scale + type) to drive palette selection" and
§11 Phase 0.

This module is where legacy noise helpers from `core/surface_decorator.py`
get ported so new Pass 2 layers can reuse them WITHOUT importing from
`surface_decorator.py` (which is staying as a shim until the pilot passes
and then deleted).

`legacy_mixed_forest_noise()` is the tile-local fBm noise used by the current
MIXED_FOREST palette selection — Nick confirmed the noise math itself was
good; only the distribution logic was off. Re-exposed here so
`temperate_forest_surface` and `temperate_riparian_fringe` can share the same
grain at biome boundaries (deterministic seam alignment).

Phase 0 status: functional. Byte-identical behavior to the surface_decorator
path is NOT guaranteed yet — that's a Phase 2 concern when the new layer
lands. For now we ship a clean port of the fBm kernel using the same
opensimplex.noise2array pattern + same octave/persistence/lacunarity defaults.
"""
from __future__ import annotations

import numpy as np

# Defaults match core/surface_decorator.py::_noise_tile octave=3 path.
DEFAULT_OCTAVES: int = 3
DEFAULT_PERSISTENCE: float = 0.5
DEFAULT_LACUNARITY: float = 2.0

# Legacy MIXED_FOREST noise scale (block units). Pulled from
# config/thresholds.json → block_mixing.noise_scale = 60 (verified S44
# 2026-04-11). Helper works in unit tests without the full config; Phase 2
# callers should still pass the live config value if they have it in hand.
LEGACY_MIXED_FOREST_SCALE: float = 60.0
LEGACY_MIXED_FOREST_SEED: int = 42002


def legacy_mixed_forest_noise(
    tile_x: int,
    tile_z: int,
    *,
    H: int = 512,
    W: int = 512,
    scale: float = LEGACY_MIXED_FOREST_SCALE,
    seed: int = LEGACY_MIXED_FOREST_SEED,
    octaves: int = DEFAULT_OCTAVES,
    persistence: float = DEFAULT_PERSISTENCE,
    lacunarity: float = DEFAULT_LACUNARITY,
) -> np.ndarray:
    """Port of `_noise_tile()` from surface_decorator for the MIXED_FOREST grain.

    Multi-octave fBm via `opensimplex.noise2array`. World-space coordinates so
    the field is tile-seamless when adjacent tiles compute their own shard.

    Args:
        tile_x, tile_z: tile coordinates; px offset = tile_x * W etc.
        H, W: tile dimensions.
        scale: spatial scale in BLOCK units — larger = coarser.
        seed: base seed (octaves add `octave * 7919` per surface_decorator).
        octaves, persistence, lacunarity: standard fBm knobs.

    Returns:
        (H, W) float32 in [0, 1].
    """
    px_off = tile_x * W
    pz_off = tile_z * H

    try:
        import opensimplex as ox
    except ImportError:
        rng = np.random.default_rng(seed ^ (tile_x * 131072 + tile_z))
        return rng.random((H, W)).astype(np.float32)

    xs_base = (np.arange(W, dtype=np.float64) + px_off) / max(scale, 1.0)
    zs_base = (np.arange(H, dtype=np.float64) + pz_off) / max(scale, 1.0)

    accumulated = np.zeros((H, W), dtype=np.float64)
    amplitude = 1.0
    freq = 1.0
    max_amp = 0.0

    for octave in range(octaves):
        ox.seed(seed + octave * 7919)
        raw = ox.noise2array(xs_base * freq, zs_base * freq)
        accumulated += raw * amplitude
        max_amp += amplitude
        amplitude *= persistence
        freq *= lacunarity

    out = accumulated.astype(np.float32)
    lo, hi = float(out.min()), float(out.max())
    if hi - lo > 1e-9:
        return (out - lo) / (hi - lo)
    return np.full((H, W), 0.5, dtype=np.float32)
