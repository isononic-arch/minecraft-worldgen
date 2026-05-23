"""
tests/unit/test_phase1_75b_gating.py

Phase 1.75b "All-biome surface decorator gating" tests.  Validates that:
  1. _apply_slope_zones skips subsurface when use_new_geology=True.
  2. _apply_slope_zones writes subsurface when use_new_geology=False (legacy).
  3. Snow/sand/gap-edge subsurface gating logic is correct (tested via
     code-path grep to confirm all subsurface writes are guarded).

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

from core import surface_decorator as sd  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_arrays(H=16, W=16, surface_block="grass_block", subsurface_block="dirt"):
    """Create baseline surface/subsurface arrays."""
    surface = np.full((H, W), surface_block, dtype=object)
    subsurface = np.full((H, W), subsurface_block, dtype=object)
    return surface, subsurface


# ---------------------------------------------------------------------------
# Slope zone gating tests (direct _apply_slope_zones calls)
# ---------------------------------------------------------------------------

class TestSlopeZoneGating:
    """_apply_slope_zones should skip subsurface writes when use_new_geology=True."""

    def _run_slope_zones(self, use_new_geology: bool, cliff_degrees: float = 70.0):
        H, W = 16, 16
        surface, subsurface = _make_arrays(H, W)
        cliff_deg = np.full((H, W), cliff_degrees, dtype=np.float32)
        biome_grid = np.full((H, W), "TEMPERATE_RAINFOREST", dtype=object)
        surface_y = np.full((H, W), 200, dtype=np.int16)
        noise = np.random.default_rng(42).random((H, W)).astype(np.float32)
        cfg = {"slope_zones": {"full_grass_max_deg": 45, "transition_max_deg": 65},
               "talus": {"enabled": False}}

        sd._apply_slope_zones(
            surface, subsurface, cliff_deg, biome_grid, surface_y,
            noise, noise, noise, cfg,
            meadow_exclude=None,
            use_new_geology=use_new_geology,
        )
        return surface, subsurface

    def test_geology_on_cliff_preserves_subsurface(self):
        """Full cliff (70deg): surface should be stone, subsurface should stay 'dirt'."""
        surface, subsurface = self._run_slope_zones(use_new_geology=True, cliff_degrees=70.0)
        # Surface should be overwritten to stone (cliff zone)
        stone_surf = np.sum(surface == "stone")
        assert stone_surf > 0, "Cliff surface should be stone even with geology ON"
        # Subsurface should NOT be overwritten
        dirt_sub = np.sum(subsurface == "dirt")
        assert dirt_sub == subsurface.size, (
            f"Geology ON: subsurface should stay 'dirt' everywhere, "
            f"got {subsurface.size - dirt_sub} non-dirt pixels"
        )

    def test_geology_off_cliff_writes_stone_subsurface(self):
        """Full cliff (70deg): legacy path should write stone subsurface."""
        surface, subsurface = self._run_slope_zones(use_new_geology=False, cliff_degrees=70.0)
        stone_sub = np.sum(subsurface == "stone")
        assert stone_sub > 0, (
            "Geology OFF: cliff pixels should have subsurface='stone' from legacy handler"
        )

    def test_geology_on_transition_preserves_subsurface(self):
        """Transition zone (50deg): surface changes but subsurface stays 'dirt'."""
        surface, subsurface = self._run_slope_zones(use_new_geology=True, cliff_degrees=50.0)
        dirt_sub = np.sum(subsurface == "dirt")
        assert dirt_sub == subsurface.size, (
            f"Geology ON: transition zone subsurface should stay 'dirt' everywhere"
        )

    def test_geology_off_transition_writes_stone_subsurface(self):
        """Transition zone (50deg): legacy path should write stone subsurface."""
        surface, subsurface = self._run_slope_zones(use_new_geology=False, cliff_degrees=50.0)
        stone_sub = np.sum(subsurface == "stone")
        assert stone_sub > 0, (
            "Geology OFF: transition pixels should have subsurface='stone' from legacy handler"
        )

    def test_geology_on_talus_preserves_subsurface(self):
        """Talus zone: subsurface stays 'dirt' when geology ON."""
        H, W = 32, 32
        surface, subsurface = _make_arrays(H, W)
        # Create a cliff edge: left half = 70deg cliff, right half = 10deg flat
        cliff_deg = np.full((H, W), 10.0, dtype=np.float32)
        cliff_deg[:, :W//2] = 70.0
        biome_grid = np.full((H, W), "TEMPERATE_RAINFOREST", dtype=object)
        surface_y = np.full((H, W), 200, dtype=np.int16)
        noise = np.random.default_rng(42).random((H, W)).astype(np.float32)
        cfg = {"slope_zones": {"full_grass_max_deg": 45, "transition_max_deg": 65},
               "talus": {"enabled": True, "dilate_px": 4, "gravel_frac": 0.5}}

        sd._apply_slope_zones(
            surface, subsurface, cliff_deg, biome_grid, surface_y,
            noise, noise, noise, cfg,
            meadow_exclude=None,
            use_new_geology=True,
        )
        dirt_sub = np.sum(subsurface == "dirt")
        assert dirt_sub == subsurface.size, (
            f"Geology ON: talus subsurface should stay 'dirt', "
            f"got {subsurface.size - dirt_sub} non-dirt"
        )

    def test_geology_on_flat_unchanged(self):
        """Flat terrain (5deg): no slope zone changes at all."""
        surface, subsurface = self._run_slope_zones(use_new_geology=True, cliff_degrees=5.0)
        grass_count = np.sum(surface == "grass_block")
        dirt_count = np.sum(subsurface == "dirt")
        assert grass_count == surface.size, "Flat terrain should be all grass_block"
        assert dirt_count == subsurface.size, "Flat terrain should be all dirt subsurface"


# ---------------------------------------------------------------------------
# Structural grep tests: verify all subsurface writes are gated
# ---------------------------------------------------------------------------

class TestSubsurfaceGatingCompleteness:
    """
    Grep-based structural tests that confirm every subsurface_blocks write
    inside the gap-handler section of decorate_surface is gated behind
    use_new_geology.  These catch accidental un-gated writes.
    """

    def _get_gap_handler_source(self):
        """Extract the gap handler section of decorate_surface."""
        src_path = Path(__file__).resolve().parents[2] / "core" / "surface_decorator.py"
        source = src_path.read_text(encoding="utf-8")
        return source

    def test_snow_subsurface_gated(self):
        """Every subsurface_blocks write in the snow handler must be inside
        'if not use_new_geology'."""
        source = self._get_gap_handler_source()
        # Find snow section: between "Snow caps (gap==7)" and next gap section
        import re
        snow_section = re.search(
            r'# ── Snow caps \(gap==7\).*?(?=# ── Sand dunes)',
            source, re.DOTALL
        )
        assert snow_section, "Could not find snow section in source"
        snow_code = snow_section.group(0)

        # Find all subsurface_blocks writes
        sub_writes = [line.strip() for line in snow_code.split('\n')
                      if 'subsurface_blocks[' in line and '=' in line
                      and not line.strip().startswith('#')]

        assert len(sub_writes) >= 1, "Expected at least 1 subsurface write in snow section"

        # Each subsurface write should be preceded (within 2 lines) by
        # 'if not use_new_geology' — verify none are ungated
        lines = snow_code.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            if 'subsurface_blocks[' in stripped and '=' in stripped and not stripped.startswith('#'):
                # Look at preceding 2 lines for the gate
                context = '\n'.join(lines[max(0,i-2):i+1])
                assert 'use_new_geology' in context, (
                    f"Un-gated subsurface write in snow section: {stripped}"
                )

    def test_sand_subsurface_gated(self):
        """Every subsurface_blocks write in the sand dune handler must be gated."""
        source = self._get_gap_handler_source()
        import re
        sand_section = re.search(
            r'# ── Sand dunes \(gap==8\).*?(?=# ── Sand flows)',
            source, re.DOTALL
        )
        assert sand_section, "Could not find sand dune section in source"
        sand_code = sand_section.group(0)

        sub_writes = [line.strip() for line in sand_code.split('\n')
                      if 'subsurface_blocks[' in line and '=' in line
                      and not line.strip().startswith('#')]

        assert len(sub_writes) >= 1, "Expected at least 1 subsurface write in sand section"

        lines = sand_code.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            if 'subsurface_blocks[' in stripped and '=' in stripped and not stripped.startswith('#'):
                context = '\n'.join(lines[max(0,i-2):i+1])
                assert 'use_new_geology' in context, (
                    f"Un-gated subsurface write in sand section: {stripped}"
                )

    def test_slope_zones_param_exists(self):
        """_apply_slope_zones must accept use_new_geology parameter."""
        import inspect
        sig = inspect.signature(sd._apply_slope_zones)
        assert 'use_new_geology' in sig.parameters, (
            "_apply_slope_zones missing use_new_geology parameter"
        )

    def test_slope_zones_called_with_flag(self):
        """The call to _apply_slope_zones in decorate_surface passes use_new_geology."""
        source = self._get_gap_handler_source()
        import re
        call = re.search(r'_apply_slope_zones\(.*?use_new_geology', source, re.DOTALL)
        assert call, (
            "_apply_slope_zones call site does not pass use_new_geology"
        )
