"""Pass 2 surface layers — physical-realism-driven surface block selection.

Each module exports a single Layer-protocol-conforming class.
Layers are ordered by the orchestrator (surface_pipeline.run_pass);
partition layers claim pixels exclusively, overlay layers paint on top.

Phase 2.0 layers (S48):
  - TemperateCliffFace  (partition, priority 10)
  - TemperateTalusApron (partition, priority 20)
  - VerticalFluting     (overlay,  priority 50)

Phase 2.5 layers (S48):
  - GrassTerrace        (partition, priority 30)
  - WeatheredTop        (partition, priority 35)

Note: ForestSurface (partition, priority 40) removed in S50 — it over-claimed
all unclaimed forested-biome pixels including clearings/meadows. The legacy
per-biome surface block logic + gap_mask meadow/clearing system in
decorate_surface() handles forest floors correctly and was already running
before the layer pipeline.

Phase 2.75 layers (S50):
  - RiverBar             (partition, priority 42)
  - DesertPavement       (partition, priority 43)

Note: SnowCapNorth (overlay, priority 55) was a surface layer in S50; converted
to precompute mask (snow_caps_north.tif → eco_gradients gap==7) in S51.

Note: BeachSurface (partition, priority 38) was attempted in S50 but removed —
per-layer EDT from surface_y didn't produce visible beaches on (48,48). Converted
to precompute mask (beach.tif → eco_gradients gap==9) in S51.
"""
from core.layers.pass2_surface.temperate_cliff_face import TemperateCliffFace
from core.layers.pass2_surface.temperate_talus_apron import TemperateTalusApron
from core.layers.pass2_surface.vertical_fluting import VerticalFluting
from core.layers.pass2_surface.grass_terrace import GrassTerrace
from core.layers.pass2_surface.weathered_top import WeatheredTop
from core.layers.pass2_surface.river_bar import RiverBar
from core.layers.pass2_surface.desert_pavement import DesertPavement

__all__ = [
    "TemperateCliffFace",
    "TemperateTalusApron",
    "VerticalFluting",
    "GrassTerrace",
    "WeatheredTop",
    "RiverBar",
    "DesertPavement",
]
