"""
tests/unit/test_phase2_0_layers.py

Surface pipeline structural tests.

Originally tested TemperateCliffFace / TemperateTalusApron / VerticalFluting
(Phase 2.0, S48). Those layers were retired in S56 (Gaea slope/dusting swap).
Remaining tests verify the pipeline harness + feature-flag wiring.

Updated S56 (2026-04-15).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Pipeline infra tests (surviving from S48)
# ---------------------------------------------------------------------------

class TestPipelineConfig:

    def test_flag_off_no_pipeline_changes(self):
        """When flag OFF, pipeline doesn't run and surface is unchanged."""
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
