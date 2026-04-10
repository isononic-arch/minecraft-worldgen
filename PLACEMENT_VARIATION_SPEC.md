# SCHEMATIC PLACEMENT VARIATION SPEC
# Locked session 5 — consumed by Step 8 (schematic_placement.py)
# =====================================================================

## GROUND ANCHOR SYSTEM

Every schematic in schematic_index.json carries:
    anchor_y     — Y coord of trunk base in schematic space
    inset_depth  — mandatory sink depth (from wool/marker blocks below trunk)
    anchor_review — bool, must be False before Step 8 runs

Placement formula:
    place_y = terrain_surface_y - anchor_y

This puts the trunk base flush with the surface. inset_depth is then
applied on top as the BASE inset (always applied):
    base_inset = inset_depth
    place_y -= base_inset


## Y-VARIATION SYSTEM

After applying base_inset, a random EXTRA inset is rolled per placement.
This breaks up the uniform look of trees all sitting at the same depth.

### Extra inset weights by size

| Size | Extra inset 0 | Extra inset 1 | Extra inset 2 |
|------|--------------|--------------|--------------|
| sm   | 80%          | 18%          | 2%           |
| md   | 60%          | 30%          | 10%          |
| lg   | 40%          | 40%          | 20%          |

Rationale:
- sm trees (saplings, krummholz) — typically shallow-rooted, mostly flush
- md trees — moderate root depth variation
- lg trees (kapok, giant fig, banyan) — deep root systems, significant inset common

### Leaf clearance check (MANDATORY before applying extra inset)

Before placing with extra inset, verify leaves won't touch the ground:

    lowest_leaf_y_in_schematic = min Y of any leaf/foliage block
    leaf_clearance = (anchor_y - lowest_leaf_y_in_schematic)  # usually negative
    proposed_inset = base_inset + extra_inset
    leaf_ground_y = terrain_surface_y - anchor_y + lowest_leaf_y_in_schematic - proposed_inset

    if leaf_ground_y <= terrain_surface_y:
        # Leaves would touch/clip ground — fall back to base_inset only
        extra_inset = 0

### Excluded from Y-variation

Trees with method == "no_trunk_lowest_solid" are excluded from extra inset.
These are:
- Fence-trunk trees (birch airy style — canopy only, no log blocks)
- Bush schematics
- Dead trees

All of the above use flush placement only (extra_inset = 0 always).


## LEAF BLOCK SET (for clearance check)

```python
LEAF_BLOCKS = {
    "oak_leaves", "spruce_leaves", "birch_leaves", "jungle_leaves",
    "acacia_leaves", "dark_oak_leaves", "mangrove_leaves",
    "cherry_leaves", "azalea_leaves", "flowering_azalea_leaves",
    "nether_wart_block", "warped_wart_block",
    # Legacy IDs (pre-1.13 classic schematics)
    # ID 18 = leaves → mapped to "legacy_18" in classic parser
    # include "legacy_18" in leaf check
}
LEGACY_LEAF_IDS = {"legacy_18", "legacy_161"}  # oak/spruce/birch/jungle, acacia/dark_oak
```


## IMPLEMENTATION IN STEP 8

```python
import random

# Per-size extra inset weight tables
EXTRA_INSET_WEIGHTS = {
    "sm": [0, 0, 0, 0, 1, 1, 1, 2],        # ~80/18/2
    "md": [0, 0, 0, 1, 1, 1, 2, 2],        # ~60/30/10  (approx via choices list)
    "lg": [0, 0, 1, 1, 1, 2, 2, 2],        # ~40/40/20  (approx via choices list)
}
# Use random.choice(EXTRA_INSET_WEIGHTS[size]) for deterministic seeded placement

def compute_placement_y(
    terrain_y:      int,
    anchor_y:       int,
    inset_depth:    int,
    size:           str,
    method:         str,
    lowest_leaf_y:  int,
    rng:            random.Random,
) -> int:
    """
    Returns the Y coord to pass to the schematic placer.
    terrain_y     — surface Y at placement XZ
    anchor_y      — from schematic_index entry
    inset_depth   — mandatory sink (wool markers)
    size          — "sm" | "md" | "lg"
    method        — anchor detection method (exclude fence-trunk trees)
    lowest_leaf_y — lowest leaf Y in schematic space
    rng           — seeded Random for determinism
    """
    base_y = terrain_y - anchor_y - inset_depth

    # No variation for fence-trunk / bush / dead
    if method == "no_trunk_lowest_solid":
        return base_y

    # Roll extra inset
    extra = rng.choice(EXTRA_INSET_WEIGHTS[size])
    if extra == 0:
        return base_y

    # Leaf clearance check
    leaf_world_y = base_y + lowest_leaf_y - extra
    if leaf_world_y <= terrain_y:
        return base_y  # fall back — leaves would clip ground

    return base_y - extra
```


## NOTES

- Seeded RNG per tile (seed = tile_x * 73856093 ^ tile_y * 19349663 ^ global_seed)
  ensures deterministic placement across re-runs and partial regeneration.
- lowest_leaf_y should be precomputed at validate/scan time and stored in
  schematic_index.json alongside anchor_y (add to scan_schematics.py output).
- Pine trees with 1-block gap between lowest leaves and ground will naturally
  pass the clearance check at extra=1 only if gap > 1, protecting their silhouette.
