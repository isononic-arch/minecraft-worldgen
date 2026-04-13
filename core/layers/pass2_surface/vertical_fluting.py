"""vertical_fluting — Overlay layer: cliff-tangent striping on rock faces.

Phase 2.0 (S48). Spec: PHYSICAL_REALISM_REFACTOR.md §8 + §11 Phase 2.0.

Paints alternating rock variants along the cliff tangent direction, giving
vertical cliff faces a fluted/columnar appearance. Only overlays pixels
already claimed by temperate_cliff_face (or any cliff partition layer).

Algorithm (from §8):
  1. Compute surface_y gradient via np.gradient on 3×3 mean-filtered surface_y.
  2. Cliff tangent = perpendicular to gradient: tangent = (-gz, gx).
  3. Phase = dot(pixel_position, tangent_direction).
  4. variant_idx = (phase // stripe_width) % N_VARIANTS.
  5. ±1 block jitter from noise.
  6. Block = lithology group palette[variant_idx].
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import uniform_filter

from core.layers.protocol import (
    EMPTY_BLOCK,
    LayerKind,
    LayerResult,
    SurfaceContext,
    make_result,
)

# Stripe width in blocks (phase units).
STRIPE_WIDTH = 4

# Jitter amplitude (±blocks of phase offset from noise).
JITTER_AMP = 1.0

# Minimum cliff_deg to apply fluting (only on true cliff faces).
MIN_CLIFF_DEG = 35.0

# Default palette (used when no lithology).
_FALLBACK_PALETTE = ["stone", "andesite", "cobblestone", "diorite", "tuff", "stone"]


class VerticalFluting:
    """Overlay layer: vertical striping on cliff faces."""

    id = "vertical_fluting"
    pass_num = 2
    priority = 50  # runs after partition layers
    kind: LayerKind = "overlay"

    def __init__(self, lithology_config: dict | None = None):
        self._group_palettes: dict[int, list[str]] = {}
        if lithology_config:
            groups = lithology_config.get("groups", {})
            for _name, gdef in groups.items():
                gid = gdef.get("id", 0)
                pal = gdef.get("palette", _FALLBACK_PALETTE)
                self._group_palettes[gid] = pal if pal else _FALLBACK_PALETTE

    def apply(self, ctx: SurfaceContext) -> LayerResult:
        shape = ctx.biome_grid.shape
        block_out = np.full(shape, EMPTY_BLOCK, dtype=object)

        # --- Scope: only overlay pixels already claimed by a cliff partition ---
        # We check ownership > 0 (some partition claimed it) AND cliff_deg >= threshold.
        cliff_deg = ctx.eco_grads.get("cliff_deg")
        if cliff_deg is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        claimed_cliff = (ctx.prior_ownership > 0) & (cliff_deg >= MIN_CLIFF_DEG)
        if not claimed_cliff.any():
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # --- Compute cliff tangent direction ---
        surface_y = ctx.eco_grads.get("surface_y")
        if surface_y is None:
            return make_result(np.zeros(shape, dtype=bool), block_out,
                               self.kind, layer_id=0)

        # 3×3 mean filter to smooth before gradient (reduces single-block noise).
        sy_smooth = uniform_filter(surface_y.astype(np.float32), size=3)

        # np.gradient returns (dY/drow, dY/dcol) = (gz, gx) in array coords.
        gz, gx = np.gradient(sy_smooth)

        # Cliff tangent = perpendicular to gradient = (-gz, gx).
        # We don't normalize — phase just needs consistent direction, magnitude
        # doesn't matter for integer binning.
        tang_row = -gz
        tang_col = gx

        # --- Phase computation ---
        # Create coordinate grids.
        rows = np.arange(shape[0], dtype=np.float32)[:, np.newaxis]
        cols = np.arange(shape[1], dtype=np.float32)[np.newaxis, :]
        row_grid = np.broadcast_to(rows, shape)
        col_grid = np.broadcast_to(cols, shape)

        # Phase = dot product of (row, col) with tangent direction.
        phase = row_grid * tang_row + col_grid * tang_col

        # Add noise jitter (±JITTER_AMP blocks).
        noise = ctx.eco_grads.get("noise_b")
        if noise is None:
            noise = np.random.default_rng(45).random(shape).astype(np.float32)
        phase += (noise - 0.5) * 2.0 * JITTER_AMP

        # --- Variant index ---
        litho_grid = ctx.lithology_grid
        if litho_grid is not None:
            group_ids = np.unique(litho_grid[claimed_cliff])
        else:
            group_ids = [0]

        for gid in group_ids:
            gid_int = int(gid)
            palette = self._group_palettes.get(gid_int, _FALLBACK_PALETTE)
            n_variants = len(palette)
            if n_variants == 0:
                continue

            if litho_grid is not None:
                group_scope = claimed_cliff & (litho_grid == gid_int)
            else:
                group_scope = claimed_cliff

            if not group_scope.any():
                continue

            # Integer bin: variant_idx = floor(phase / stripe_width) % n_variants
            variant_idx = (np.floor(phase / STRIPE_WIDTH).astype(int)) % n_variants
            # Ensure positive modulo.
            variant_idx = variant_idx % n_variants

            # Vectorized: assign block for each variant index.
            for vi in range(n_variants):
                mask = group_scope & (variant_idx == vi)
                if mask.any():
                    block_out[mask] = palette[vi]

        modified = claimed_cliff & (block_out != EMPTY_BLOCK)
        return make_result(modified, block_out, self.kind, layer_id=0,
                           debug_meta={"fluted_px": int(modified.sum())})
