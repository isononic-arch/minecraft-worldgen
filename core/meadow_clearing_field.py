"""Shared meadow/clearing noise field — Phase 0 scaffolding (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 3 (`temperate_clearing`,
`clearing_edge_dither`) and §11 Phase 0.

Single low-frequency organic-blob noise field (~200–400 block wavelength,
one octave of opensimplex at 1:8 precompute res, bilinear upscale).
Precomputed ONCE at pipeline start and shared between:

  - Pass 3 `temperate_clearing` layer — decides where forest/floodplain
    canopy gives way to grass clearings.
  - Pass 3 `clearing_edge_dither` overlay — feathers the seam.
  - Pass 4 tree-scatter density — same field weights Poisson-disk placement
    so tree absence and grass clearings line up on the EXACT same seam.

The whole point is that both consumers read the SAME field with the same
seed so the clearing geometry is deterministic and seamless. No per-layer
noise re-rolling.

Scope: temperate forest biomes + floodplain corridors (gap==4).

Phase 0 status: functional (not a stub). Uses the same opensimplex import
pattern as `core/surface_decorator.py:_noise_tile` for consistency.
"""
from __future__ import annotations

import numpy as np

# ~200–400 block wavelength at 1:1 (block-space) == ~25–50 px at 1:8.
# We target ~32 px wavelength at 1:8 precompute res, which is ~256 blocks.
DEFAULT_PRECOMPUTE_SCALE: float = 8.0   # 1:8
DEFAULT_WAVELENGTH_1_8_PX: float = 32.0
DEFAULT_SEED: int = 0xBEEF_CAFE

# Threshold below which the field counts as "clearing interior".
# Held in one place so all consumers line up. Phase 3 tunes in-game.
CLEARING_INTERIOR_THRESHOLD: float = 0.38

# Band (in field units) on either side of the threshold that counts as the
# seam zone for the edge dither layer. ~4 blocks at 1:8 is ~0.5 px, which
# at ~32 px wavelength is field delta ~0.03. Tune in Phase 3.
CLEARING_EDGE_BAND: float = 0.06


def compute_meadow_clearing_field(
    tile_x: int,
    tile_z: int,
    *,
    H: int = 512,
    W: int = 512,
    wavelength_blocks: float = 256.0,
    seed: int = DEFAULT_SEED,
) -> np.ndarray:
    """Compute the (H, W) meadow-clearing field for a single tile.

    World-space coordinates so the field is tile-seamless. Single octave of
    opensimplex at `1 / wavelength_blocks` frequency. Normalized to [0, 1].

    Args:
        tile_x, tile_z: tile coordinates in the 97×97 grid (1 tile = 512 blocks).
        H, W: tile dimensions (default 512×512).
        wavelength_blocks: spatial wavelength in BLOCK units. ~200–400 per spec.
        seed: deterministic seed.

    Returns:
        (H, W) float32 in [0, 1].
    """
    px_off = tile_x * W
    pz_off = tile_z * H

    xs = (np.arange(W, dtype=np.float64) + px_off) / wavelength_blocks
    zs = (np.arange(H, dtype=np.float64) + pz_off) / wavelength_blocks

    try:
        import opensimplex as ox
    except ImportError:
        # Deterministic fallback for the stub — rng seeded by (seed, tile).
        rng = np.random.default_rng(seed ^ (tile_x * 131072 + tile_z))
        return rng.random((H, W)).astype(np.float32)

    ox.seed(seed)
    raw = ox.noise2array(xs, zs)  # shape (H, W), range ~[-1, 1]
    out = ((raw + 1.0) * 0.5).astype(np.float32)
    return out


def clearing_interior_mask(
    field: np.ndarray,
    threshold: float = CLEARING_INTERIOR_THRESHOLD,
) -> np.ndarray:
    """Where the clearing is unambiguously grass (interior)."""
    return field < threshold


def clearing_seam_mask(
    field: np.ndarray,
    threshold: float = CLEARING_INTERIOR_THRESHOLD,
    band: float = CLEARING_EDGE_BAND,
) -> np.ndarray:
    """The dither band on either side of the clearing/forest seam."""
    return np.abs(field - threshold) < band
