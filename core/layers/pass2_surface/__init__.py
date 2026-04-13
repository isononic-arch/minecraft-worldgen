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
  - ForestSurface       (partition, priority 40)
"""
from core.layers.pass2_surface.temperate_cliff_face import TemperateCliffFace
from core.layers.pass2_surface.temperate_talus_apron import TemperateTalusApron
from core.layers.pass2_surface.vertical_fluting import VerticalFluting
from core.layers.pass2_surface.grass_terrace import GrassTerrace
from core.layers.pass2_surface.weathered_top import WeatheredTop
from core.layers.pass2_surface.forest_surface import ForestSurface

__all__ = [
    "TemperateCliffFace",
    "TemperateTalusApron",
    "VerticalFluting",
    "GrassTerrace",
    "WeatheredTop",
    "ForestSurface",
]
