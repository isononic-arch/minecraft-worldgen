"""Tree-density hint field — Phase 0 scaffolding (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 2 "Tree-density coupling" and
§11 Phase 0, plus risk R3-1.

Problem: Pass 2 (surface block selection) needs to know how dense the forest
will be in Pass 4 (vegetation placement), because forest-floor surface mix
differs from open-pasture surface mix. But Pass 4 runs AFTER Pass 2 in pipeline
order. Solution: precompute the suitability function ONCE at tile start, and
have both passes consume the same field — Pass 2 reads it as a scalar hint,
Pass 4 uses the same function to sample placement probabilities. By
construction, the two stay consistent.

Output: (H, W) float32 array in [0, 1].
  - 0.0 → no canopy expected (open pasture / cliff / water / disturbance)
  - 0.3 → transitional band
  - 0.6+ → closed canopy expected

Phase 0 status: STUB. Returns a plausible field derived from biome + slope +
moisture + disturbance so Pass 2 layers can unit-test against it. Phase 3
replaces the body with the real suitability kernel used by
`core/layers/pass4_vegetation/temperate_tree_canopy.py`.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np

# Biome zone codes for forested temperate — temporary hand list until we pull
# the canonical set from core/biome_assignment.OVERRIDE_BIOME_MAP during
# Phase 2 integration. Keeping as a string list because the biome_grid in
# SurfaceContext is expected to be a string object array.
TEMPERATE_FORESTED_BIOMES: tuple[str, ...] = (
    "MIXED_FOREST",
    "TEMPERATE_RAINFOREST",
    "TEMPERATE_CONIFEROUS_FOREST",
)


def compute_tree_density_hint(
    biome_grid: np.ndarray,
    *,
    slope: np.ndarray,
    moisture_idx: np.ndarray,
    disturbance: np.ndarray | None = None,
    forested_biomes: Iterable[str] = TEMPERATE_FORESTED_BIOMES,
) -> np.ndarray:
    """Compute the precomputed tree-density hint field.

    Args:
        biome_grid: (H, W) str object — biome names per pixel.
        slope: (H, W) float32 in [0, 1]. Steep slopes inhibit canopy.
        moisture_idx: (H, W) float32 in [0, 1]. Wetter → denser canopy.
        disturbance: optional (H, W) float32 in [0, 1]. Windthrow / floodplain
            disturbance. Inhibits canopy.
        forested_biomes: iterable of biome names considered "could host forest".

    Returns:
        (H, W) float32 hint in [0, 1].

    This is the Phase 0 stub — the weights here are plausibility placeholders,
    NOT tuned values. Phase 2 will replace the body with the real suitability
    kernel. The signature is stable.
    """
    H, W = biome_grid.shape
    out = np.zeros((H, W), dtype=np.float32)

    # Base mask: only pixels in a forested biome can have canopy.
    forest_set = set(forested_biomes)
    in_forest = np.zeros((H, W), dtype=bool)
    # str object arrays don't vectorize equality well; iterate the small set.
    for b in forest_set:
        in_forest |= (biome_grid == b)

    if not in_forest.any():
        return out

    slope_f = np.clip(slope.astype(np.float32), 0.0, 1.0)
    moist_f = np.clip(moisture_idx.astype(np.float32), 0.0, 1.0)

    # Placeholder kernel: moisture pulls density up, slope pulls it down.
    density = 0.35 + 0.55 * moist_f - 0.45 * slope_f
    density = np.clip(density, 0.0, 1.0)

    if disturbance is not None:
        dist_f = np.clip(disturbance.astype(np.float32), 0.0, 1.0)
        density *= (1.0 - 0.8 * dist_f)

    out[in_forest] = density[in_forest]
    return out.astype(np.float32)
