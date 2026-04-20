"""Bush schematic routing matrix.

Uses the real `core/schematic_placement.load_index()` loader so the result
reflects actual pipeline routing (including `_INDEX_KEY_MAP` remap, generic
merge, and `NO_BUSH_BIOMES` exclusions).

Emits memory/bush_routing_matrix.md. Flags any biome with < 5 bushes.

Usage:
    py tools/diag_bush_routing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_WORKTREE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_WORKTREE))

from core.biome_assignment import OVERRIDE_BIOME_MAP  # noqa: E402
from core.schematic_placement import (  # noqa: E402
    load_index, NO_BUSH_BIOMES, BASE_DENSITY,
)

INDEX_PATH = Path(r"C:/Users/nicho/minecraft-worldgen/schematic_index.json")
OUT_PATH = _WORKTREE / "memory" / "bush_routing_matrix.md"
MIN_BUSHES = 5


def main() -> int:
    print(f"[bush_routing] loading {INDEX_PATH} via core.schematic_placement.load_index", flush=True)
    idx = load_index(INDEX_PATH)

    known_biomes = [v for v in OVERRIDE_BIOME_MAP.values() if v]
    all_routed = sorted(idx.keys())
    unrouted = sorted(set(known_biomes) - set(idx.keys()) - NO_BUSH_BIOMES)
    extra = sorted(set(idx.keys()) - set(known_biomes))

    rows = []
    for b in sorted(set(known_biomes) | set(idx.keys())):
        entries = idx.get(b, [])
        n_tree = sum(1 for e in entries if e.schem_type == "tree")
        n_bush = sum(1 for e in entries if e.schem_type == "bush")
        total = len(entries)
        density = BASE_DENSITY.get(b, 0.05)
        no_bush = b in NO_BUSH_BIOMES
        example_bushes = ", ".join(
            Path(e.path).stem[:28]
            for e in entries if e.schem_type == "bush"
        )[:160]
        gap = (n_bush < MIN_BUSHES) and not no_bush
        rows.append({
            "biome": b,
            "known": b in known_biomes,
            "trees": n_tree,
            "bushes": n_bush,
            "total": total,
            "density": density,
            "no_bush": no_bush,
            "gap": gap,
            "example_bushes": example_bushes,
        })

    n_gaps = sum(1 for r in rows if r["gap"])
    lines = [
        "# Bush schematic routing matrix",
        "",
        "Generated via `core.schematic_placement.load_index()` (real pipeline loader).",
        f"Known biomes from `OVERRIDE_BIOME_MAP`: **{len(known_biomes)}**.",
        f"Biomes with entries in loaded index: **{len(all_routed)}**.",
        f"Unrouted known biomes (no entries, not in NO_BUSH_BIOMES): **{len(unrouted)}**.",
        f"Extra keys (in index but not in OVERRIDE_BIOME_MAP): **{len(extra)}**.",
        f"NO_BUSH_BIOMES: `{sorted(NO_BUSH_BIOMES)}`.",
        f"Bushes-below-threshold gaps (< {MIN_BUSHES}, excluding NO_BUSH): **{n_gaps}**.",
        "",
        "| Biome | Known? | Trees | Bushes | Density | No-bush? | Gap? | Example bush stems |",
        "|-------|--------|-------|--------|---------|----------|------|-------------------|",
    ]
    for r in rows:
        known = "yes" if r["known"] else "no"
        gap = "GAP" if r["gap"] else ("skip" if r["no_bush"] else "ok")
        no_bush = "yes" if r["no_bush"] else ""
        lines.append(
            f"| {r['biome']} | {known} | {r['trees']} | {r['bushes']} | {r['density']:.3f} | {no_bush} | {gap} | {r['example_bushes']} |"
        )

    if unrouted:
        lines.append("")
        lines.append("## Unrouted known biomes (no entries in loaded index)")
        lines.append("")
        for b in unrouted:
            lines.append(f"- {b}")

    if extra:
        lines.append("")
        lines.append("## Extra keys (in index but NOT in OVERRIDE_BIOME_MAP)")
        lines.append("")
        for k in extra:
            lines.append(f"- {k}")

    OUT_PATH.parent.mkdir(exist_ok=True)
    OUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[bush_routing] report written to {OUT_PATH}")
    print(f"[bush_routing] summary: {len(rows)} biomes, {n_gaps} gaps, {len(unrouted)} unrouted, {len(extra)} extra")
    return 0


if __name__ == "__main__":
    sys.exit(main())
