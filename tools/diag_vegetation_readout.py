"""Per-biome vegetation + surface-palette readout.

Dumps two tables to memory/vegetation_readout.md:

  A. GROUND_COVER_PALETTES (ground-level vegetation, sugar_cane/ferns/grasses/
     flowers/bushes) — from core/surface_decorator.py. One row per biome,
     columns are species + rolled probability.
  B. noise_layers_biome (surface block palettes — grass/dirt/sand/stone mixes)
     — from config/thresholds.json. One row per biome, columns are layer name
     + block + coverage + noise scale.

Usage:
    py tools/diag_vegetation_readout.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))

from core.surface_decorator import GROUND_COVER_PALETTES  # noqa: E402
from core.biome_assignment import OVERRIDE_BIOME_MAP  # noqa: E402

CFG = _WORKTREE / "config" / "thresholds.json"
OUT = _WORKTREE / "memory" / "vegetation_readout.md"


def main() -> int:
    cfg = json.load(open(CFG, encoding="utf-8"))
    noise_layers = cfg.get("noise_layers_biome", {})

    known = [v for v in OVERRIDE_BIOME_MAP.values() if v]

    lines: list[str] = []
    lines.append("# Per-biome vegetation + surface-palette readout")
    lines.append("")
    lines.append("Two systems drive what you see on the ground:")
    lines.append("- **Ground cover** — grasses/ferns/bushes/flowers placed at `surface_y + 1`.")
    lines.append("  Defined in [`core/surface_decorator.py:GROUND_COVER_PALETTES`](../core/surface_decorator.py).")
    lines.append("  Edit THERE (Python dict, not JSON).")
    lines.append("- **Surface block palette** — the `grass_block`/`dirt`/`sand`/`podzol` mix UNDER the ground cover.")
    lines.append("  Defined in [`config/thresholds.json:noise_layers_biome`](../config/thresholds.json).")
    lines.append("  Editable via `tools/world_studio.py` Tab B, or directly in the JSON.")
    lines.append("")
    lines.append("The two systems are independent — you can change one without affecting the other.")
    lines.append("")

    # ── Section A: ground cover ──────────────────────────────────────────────
    lines.append("## A. Ground cover per biome (GROUND_COVER_PALETTES)")
    lines.append("")
    lines.append("| Biome | Species count | Species (probability = p per-pixel roll; total may exceed 1.0 as independent rolls) |")
    lines.append("|-------|--------------:|--------------|")
    all_species: set = set()
    for biome in sorted(set(known) | set(GROUND_COVER_PALETTES.keys())):
        entries = GROUND_COVER_PALETTES.get(biome, [])
        if not entries:
            lines.append(f"| {biome} | 0 | *(empty — no ground cover)* |")
            continue
        species_str = ", ".join(f"`{s}`={p:.3g}" for s, p in entries)
        lines.append(f"| {biome} | {len(entries)} | {species_str} |")
        for s, _ in entries:
            all_species.add(s)

    lines.append("")
    lines.append(f"### Unique species across all biomes ({len(all_species)})")
    lines.append("")
    lines.append("`" + "`, `".join(sorted(all_species)) + "`")

    # ── Section B: surface block palettes ────────────────────────────────────
    lines.append("")
    lines.append("## B. Surface block palette per biome (noise_layers_biome)")
    lines.append("")
    lines.append("Layer-stack model: layer[0] has highest priority (painted last, overwrites). "
                 "`is_base=true` layer fills the biome; non-base layers paint where their noise "
                 "exceeds `1 - coverage`. `noise` name `simplex_fbm` = fBm simplex blobs "
                 "(and back-compat alias `gaussian`); `white`/`per_pixel` = salt-and-pepper.")
    lines.append("")
    lines.append("| Biome | Layer count | Layers (name — block/sub; cov; noise; scale) |")
    lines.append("|-------|--------------:|--------------|")

    for biome in sorted(set(known) | set(noise_layers.keys())):
        layers = noise_layers.get(biome, [])
        if not layers:
            lines.append(f"| {biome} | 0 | *(empty — falls back to legacy palettes / BIOME_BLOCK_PALETTES)* |")
            continue
        parts = []
        for L in layers:
            name = L.get("name", "?")
            blk  = L.get("block", "?")
            sub  = L.get("sub", "?")
            cov  = L.get("coverage", 0.0)
            nz   = L.get("noise", "simplex_fbm")
            scl  = L.get("scale", 0)
            base = " (base)" if L.get("is_base") else ""
            parts.append(f"`{name}`={blk}/{sub}; cov={cov:.2f}; {nz}; s={scl}{base}")
        lines.append(f"| {biome} | {len(layers)} | " + " • ".join(parts) + " |")

    # Biomes without surface-palette entries (fall through to legacy dict)
    missing_surface = [b for b in known if b not in noise_layers]
    if missing_surface:
        lines.append("")
        lines.append(f"### Biomes falling through to legacy `BIOME_BLOCK_PALETTES` ({len(missing_surface)}) — consider migrating")
        lines.append("")
        for b in missing_surface:
            lines.append(f"- {b}")

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[veg_readout] wrote {OUT}  ({len(lines)} lines)")

    # Also print a quick summary to console
    n_gc = sum(1 for b in known if GROUND_COVER_PALETTES.get(b))
    n_noise = sum(1 for b in known if b in noise_layers)
    print(f"[veg_readout] summary: {len(known)} known biomes")
    print(f"  with ground cover entries:        {n_gc}")
    print(f"  with noise_layers_biome entries:  {n_noise}")
    print(f"  missing surface palette (legacy): {len(missing_surface)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
