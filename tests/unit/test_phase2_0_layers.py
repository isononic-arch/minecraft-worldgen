"""
tests/unit/test_phase2_0_layers.py

Phase 2.0 layer unit tests.  Validates:
  1. TemperateCliffFace claims steep temperate pixels; ignores flat/non-temperate.
  2. TemperateTalusApron claims moderate-slope concave pixels; ignores flat/convex.
  3. VerticalFluting overlays only on already-claimed cliff pixels.
  4. Layer composition via run_pass gives correct ownership semantics.
  5. Feature flag wiring: decorate_surface signature accepts new params.

Written S48 (2026-04-12).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.layers.protocol import (  # noqa: E402
    EMPTY_BLOCK,
    SurfaceContext,
)
from core.layers.pass2_surface.temperate_cliff_face import (  # noqa: E402
    TemperateCliffFace,
    CLIFF_DEG_THRESHOLD,
    LAND_BIOMES,
)
from core.layers.pass2_surface.temperate_talus_apron import (  # noqa: E402
    TemperateTalusApron,
    TALUS_DEG_MIN,
    TALUS_DEG_MAX,
)
from core.layers.pass2_surface.vertical_fluting import (  # noqa: E402
    VerticalFluting,
    MIN_CLIFF_DEG,
)
from core.surface_pipeline import run_pass  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LITHO_CFG = {
    "groups": {
        "mossy_temperate": {
            "id": 6,
            "palette": ["mossy_cobblestone", "cobblestone", "andesite",
                        "stone", "mossy_cobblestone", "cobblestone"],
        },
        "granitic": {
            "id": 1,
            "palette": ["granite", "stone", "andesite",
                        "diorite", "granite", "polished_granite"],
        },
    }
}


def _make_ctx(
    shape=(32, 32),
    biome="TEMPERATE_RAINFOREST",
    cliff_degrees=50.0,
    concavity_val=0.6,
    surface_y_val=200,
    litho_id=6,
    prior_ownership=None,
) -> SurfaceContext:
    """Build a synthetic SurfaceContext."""
    H, W = shape
    biome_grid = np.full((H, W), biome, dtype=object)
    lithology_grid = np.full((H, W), litho_id, dtype=np.uint8)
    cliff_deg = np.full((H, W), cliff_degrees, dtype=np.float32)
    surface_y = np.full((H, W), surface_y_val, dtype=np.int16)
    concavity = np.full((H, W), concavity_val, dtype=np.float32)
    noise_b = np.random.default_rng(42).random((H, W)).astype(np.float32)

    eco = {
        "cliff_deg": cliff_deg,
        "surface_y": surface_y,
        "concavity_norm": concavity,
        "noise_b": noise_b,
    }

    if prior_ownership is None:
        prior_ownership = np.zeros((H, W), dtype=np.uint16)

    return SurfaceContext(
        tile_x=59,
        tile_z=53,
        biome_grid=biome_grid,
        lithology_grid=lithology_grid,
        eco_grads=eco,
        column_output={"surface_y": surface_y},
        prior_surface=np.full((H, W), "grass_block", dtype=object),
        prior_ownership=prior_ownership,
        overlay_touched=np.zeros((H, W), dtype=np.uint8),
    )


# ---------------------------------------------------------------------------
# TemperateCliffFace tests
# ---------------------------------------------------------------------------

class TestTemperateCliffFace:

    def test_claims_steep_temperate_pixels(self):
        """All pixels are steep + temperate → all should be claimed."""
        ctx = _make_ctx(cliff_degrees=50.0, biome="TEMPERATE_RAINFOREST")
        layer = TemperateCliffFace(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert result.modified_mask.all(), "All steep temperate pixels should be claimed"
        assert result.kind == "partition"
        # Should use mossy_temperate palette (litho_id=6)
        blocks = set(result.block_output[result.modified_mask].flat)
        assert blocks.issubset({"mossy_cobblestone", "cobblestone", "andesite",
                                "stone", "mossy_cobblestone"}), \
            f"Unexpected blocks: {blocks}"

    def test_ignores_flat_terrain(self):
        """Flat terrain (5°) → no pixels claimed."""
        ctx = _make_ctx(cliff_degrees=5.0)
        layer = TemperateCliffFace(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Flat terrain should have no cliff claims"

    def test_ignores_ocean_biome(self):
        """Ocean/empty biome → no pixels claimed even on steep slope."""
        ctx = _make_ctx(cliff_degrees=50.0, biome="_OCEAN")
        layer = TemperateCliffFace(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Ocean biome should not be claimed"

    def test_ignores_ocean(self):
        """Below sea level → no pixels claimed."""
        ctx = _make_ctx(cliff_degrees=50.0, surface_y_val=50)
        layer = TemperateCliffFace(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Ocean pixels should not be claimed"

    def test_respects_prior_ownership(self):
        """Already-claimed pixels → not overwritten."""
        shape = (32, 32)
        prior = np.ones(shape, dtype=np.uint16)  # all claimed
        ctx = _make_ctx(cliff_degrees=50.0, prior_ownership=prior)
        layer = TemperateCliffFace(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Already-claimed pixels should be skipped"

    def test_granitic_palette(self):
        """Litho group 1 (granitic) → granite/stone/andesite palette."""
        ctx = _make_ctx(cliff_degrees=50.0, litho_id=1)
        layer = TemperateCliffFace(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        blocks = set(result.block_output[result.modified_mask].flat)
        assert blocks.issubset({"granite", "stone", "andesite", "diorite",
                                "polished_granite"}), \
            f"Unexpected blocks for granitic: {blocks}"

    def test_threshold_boundary(self):
        """Exactly at threshold (35°) → should be claimed."""
        ctx = _make_ctx(cliff_degrees=CLIFF_DEG_THRESHOLD)
        layer = TemperateCliffFace(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert result.modified_mask.all(), "At threshold should be claimed"

    def test_just_below_threshold(self):
        """Just below threshold (34.9°) → should NOT be claimed."""
        ctx = _make_ctx(cliff_degrees=CLIFF_DEG_THRESHOLD - 0.1)
        layer = TemperateCliffFace(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Below threshold should not be claimed"


# ---------------------------------------------------------------------------
# TemperateTalusApron tests
# ---------------------------------------------------------------------------

class TestTemperateTalusApron:

    def test_claims_moderate_concave_pixels(self):
        """Moderate slope (20°) + concave → should claim."""
        ctx = _make_ctx(cliff_degrees=20.0, concavity_val=0.6)
        layer = TemperateTalusApron()
        result = layer.apply(ctx)
        assert result.modified_mask.any(), "Moderate concave pixels should be claimed"
        assert result.kind == "partition"

    def test_ignores_flat_terrain(self):
        """Flat terrain (5°) → no talus."""
        ctx = _make_ctx(cliff_degrees=5.0, concavity_val=0.6)
        layer = TemperateTalusApron()
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Flat terrain should have no talus"

    def test_ignores_steep_terrain(self):
        """Steep terrain (36°, above TALUS_DEG_MAX) → handled by cliff_face, not talus."""
        ctx = _make_ctx(cliff_degrees=36.0, concavity_val=0.6)
        layer = TemperateTalusApron()
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Steep terrain should not be talus"

    def test_blocks_are_cobble_or_gravel(self):
        """Talus should only produce cobblestone, gravel, or passthrough."""
        ctx = _make_ctx(cliff_degrees=20.0, concavity_val=0.6)
        layer = TemperateTalusApron()
        result = layer.apply(ctx)
        blocks = set(result.block_output[result.modified_mask].flat)
        assert blocks.issubset({"cobblestone", "gravel", "grass_block"}), \
            f"Unexpected talus blocks: {blocks}"

    def test_convex_excluded(self):
        """Convex terrain (concavity < 0) → no talus."""
        ctx = _make_ctx(cliff_degrees=25.0, concavity_val=-0.5)
        layer = TemperateTalusApron()
        result = layer.apply(ctx)
        # concavity_norm is [0,1] where 0=convex, 1=concave.
        # -0.5 is below threshold of 0.0 → should exclude.
        assert not result.modified_mask.any(), "Convex terrain should have no talus"


# ---------------------------------------------------------------------------
# VerticalFluting tests
# ---------------------------------------------------------------------------

class TestVerticalFluting:

    def test_overlays_only_claimed_cliffs(self):
        """Fluting should only paint on already-claimed cliff pixels."""
        shape = (32, 32)
        # Pre-claim all pixels (simulate cliff_face ran first).
        prior_own = np.ones(shape, dtype=np.uint16)
        ctx = _make_ctx(cliff_degrees=50.0, prior_ownership=prior_own)
        layer = VerticalFluting(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert result.modified_mask.any(), "Should overlay on claimed cliff pixels"
        assert result.kind == "overlay"

    def test_no_overlay_on_unclaimed(self):
        """No claimed pixels → no fluting."""
        ctx = _make_ctx(cliff_degrees=50.0)
        # prior_ownership = 0 (all unclaimed)
        layer = VerticalFluting(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Unclaimed pixels should not get fluting"

    def test_no_overlay_on_flat(self):
        """Flat claimed pixels → no fluting (below MIN_CLIFF_DEG)."""
        shape = (32, 32)
        prior_own = np.ones(shape, dtype=np.uint16)
        ctx = _make_ctx(cliff_degrees=5.0, prior_ownership=prior_own)
        layer = VerticalFluting(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        assert not result.modified_mask.any(), "Flat pixels should not get fluting"

    def test_multiple_variants_present(self):
        """Fluting should produce >1 distinct block (stripe pattern)."""
        shape = (64, 64)
        prior_own = np.ones(shape, dtype=np.uint16)
        ctx = _make_ctx(shape=shape, cliff_degrees=50.0,
                        prior_ownership=prior_own)
        layer = VerticalFluting(lithology_config=_LITHO_CFG)
        result = layer.apply(ctx)
        blocks = set(result.block_output[result.modified_mask].flat)
        assert len(blocks) > 1, f"Expected multiple fluting variants, got {blocks}"


# ---------------------------------------------------------------------------
# Pipeline composition tests
# ---------------------------------------------------------------------------

class TestPipelineComposition:

    def test_cliff_then_talus_then_fluting(self):
        """Full 3-layer pass: cliff claims steep, talus claims moderate, fluting overlays."""
        shape = (64, 64)
        H, W = shape
        biome_grid = np.full(shape, "TEMPERATE_RAINFOREST", dtype=object)
        lithology_grid = np.full(shape, 6, dtype=np.uint8)

        # Left half steep (40°), right half moderate (20°)
        cliff_deg = np.full(shape, 20.0, dtype=np.float32)
        cliff_deg[:, :W//2] = 40.0
        surface_y = np.full(shape, 200, dtype=np.int16)
        concavity = np.full(shape, 0.6, dtype=np.float32)
        noise_b = np.random.default_rng(42).random(shape).astype(np.float32)

        ctx = SurfaceContext(
            tile_x=59, tile_z=53,
            biome_grid=biome_grid,
            lithology_grid=lithology_grid,
            eco_grads={
                "cliff_deg": cliff_deg,
                "surface_y": surface_y,
                "concavity_norm": concavity,
                "noise_b": noise_b,
            },
            column_output={"surface_y": surface_y},
            prior_surface=np.full(shape, "grass_block", dtype=object),
            prior_ownership=np.zeros(shape, dtype=np.uint16),
            overlay_touched=np.zeros(shape, dtype=np.uint8),
        )

        layers = [
            TemperateCliffFace(lithology_config=_LITHO_CFG),
            TemperateTalusApron(),
            VerticalFluting(lithology_config=_LITHO_CFG),
        ]

        result = run_pass(layers, ctx, strict=True)

        # Cliff face should own left half (steep)
        cliff_owned = result.ownership[:, :W//2]
        assert (cliff_owned == 1).all(), "Left half (steep) should be claimed by cliff_face (layer 1)"

        # Talus should own some of right half (moderate + concave)
        talus_owned = result.ownership[:, W//2:] == 2
        assert talus_owned.any(), "Right half should have some talus claims"

        # Fluting overlay should have touched some cliff pixels
        fluted = result.overlay_touched[:, :W//2] > 0
        assert fluted.any(), "Cliff pixels should have fluting overlay"

    def test_flag_off_no_pipeline_changes(self):
        """When flag OFF, pipeline doesn't run and surface is unchanged."""
        # This is a structural test — we verify the param exists.
        import inspect
        from core import surface_decorator as sd
        sig = inspect.signature(sd.decorate_surface)
        assert 'use_new_surface_pipeline' in sig.parameters, \
            "decorate_surface missing use_new_surface_pipeline parameter"
        assert 'lithology_tile' in sig.parameters, \
            "decorate_surface missing lithology_tile parameter"

    def test_flag_default_is_false(self):
        """Default value for use_new_surface_pipeline should be False."""
        import inspect
        from core import surface_decorator as sd
        sig = inspect.signature(sd.decorate_surface)
        param = sig.parameters['use_new_surface_pipeline']
        assert param.default is False, \
            f"Expected default False, got {param.default}"


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestConfig:

    def test_thresholds_has_feature_flag(self):
        """config/thresholds.json should have surface_pipeline.feature_flag_enabled."""
        import json
        cfg_path = PROJECT_ROOT / "config" / "thresholds.json"
        cfg = json.loads(cfg_path.read_text())
        sp = cfg.get("surface_pipeline", {})
        assert "feature_flag_enabled" in sp, \
            "surface_pipeline.feature_flag_enabled missing from config"
        assert isinstance(sp["feature_flag_enabled"], bool), \
            "feature_flag_enabled should be a boolean"
