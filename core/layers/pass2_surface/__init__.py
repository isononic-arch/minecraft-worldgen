"""Pass 2 surface layers — physical-realism-driven surface block selection.

Each module exports a single Layer-protocol-conforming class.
Layers are ordered by the orchestrator (surface_pipeline.run_pass);
partition layers claim pixels exclusively, overlay layers paint on top.

Phase 2.0 layers — TemperateCliffFace, TemperateTalusApron, VerticalFluting
and SnowCapNorth RETIRED in S56 (Gaea slope/dusting mask swap).

Surviving layers:
  - GrassTerrace        (partition, priority 30)
  - WeatheredTop        (partition, priority 35)
  - RiverBar            (partition, priority 42)
  - DesertPavement      (partition, priority 43)
"""
from core.layers.pass2_surface.grass_terrace import GrassTerrace
from core.layers.pass2_surface.weathered_top import WeatheredTop
from core.layers.pass2_surface.river_bar import RiverBar
from core.layers.pass2_surface.desert_pavement import DesertPavement

__all__ = [
    "GrassTerrace",
    "WeatheredTop",
    "RiverBar",
    "DesertPavement",
]
