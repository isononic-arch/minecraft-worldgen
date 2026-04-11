"""Vegetation block allowlist — Phase 0 scaffolding (S44).

Spec: PHYSICAL_REALISM_REFACTOR.md §6 Pass 4 "No-grow rule (VERY IMPORTANT)"
and §11 Phase 0 / R3-3.

HARD RULE (Nick, repeated):
  NO block that Minecraft can tick into GROWTH is allowed as a final vegetation
  placement. No saplings, no crops, no growable propagules.

Enforcement: every Pass 4 layer must pass its `block_output` through
`validate_no_grow()` before returning, and the chunk writer sentinel test
walks every entry in `schematic_index.json` against `NO_GROW_BLOCKLIST` to
catch schematics that embed saplings.

Violating this is called out in §15 as "capital offense — do not get this
wrong again."

Target MC version: 1.21.10 Java, DataVersion 4556.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np


# ---------------------------------------------------------------------------
# Blocks that MC will tick into growth. NEVER emit as a final placement.
# Source: PHYSICAL_REALISM_REFACTOR.md §6 Pass 4.
# ---------------------------------------------------------------------------
NO_GROW_BLOCKLIST: frozenset[str] = frozenset({
    # Saplings / propagules
    "oak_sapling",
    "birch_sapling",
    "spruce_sapling",
    "jungle_sapling",
    "acacia_sapling",
    "dark_oak_sapling",
    "mangrove_propagule",
    "cherry_sapling",
    "azalea",
    "flowering_azalea",
    "pale_oak_sapling",
    # Crops
    "wheat",
    "carrots",
    "potatoes",
    "beetroots",
    "melon_stem",
    "pumpkin_stem",
    "cocoa",
    "torchflower_crop",
    "pitcher_crop",
    # Growable shrubs / canes
    "sweet_berry_bush",
    "bamboo_sapling",
    "sugar_cane",
    "kelp",
})


# ---------------------------------------------------------------------------
# Allowlist of vegetation blocks that DO NOT tick into growth and are cleared
# for use as final Pass 4 placements. Covers MC 1.21.10 (DataVersion 4556).
# Extending this list requires manual review — add a block here only after
# verifying the in-game tick rules.
# ---------------------------------------------------------------------------
NO_GROW_ALLOWLIST: frozenset[str] = frozenset({
    # Grasses / ferns (static)
    "short_grass",
    "tall_grass",
    "fern",
    "large_fern",
    # 1.21.2+ dead-end plants
    "bush",
    "dead_bush",
    "leaf_litter",
    "pale_moss_carpet",
    "pink_petals",
    "wildflowers",
    "resin_clump",
    # Moss
    "moss_carpet",
    "moss_block",
    "pale_moss_block",
    "pale_hanging_moss",
    # Static flowers
    "dandelion",
    "poppy",
    "azure_bluet",
    "allium",
    "oxeye_daisy",
    "cornflower",
    "lily_of_the_valley",
    "blue_orchid",
    "sunflower",
    "lilac",
    "peony",
    "rose_bush",
    "torchflower",
    "pitcher_plant",
    "closed_eyeblossom",
    "open_eyeblossom",
    # Firefly bush is allowed BY MC but Nick rejects it in forests specifically.
    # Keep it allowlisted here and enforce biome-scope exclusion in the forest
    # layer rather than blacklisting globally.
    "firefly_bush",
})


def validate_no_grow(
    block_output: np.ndarray,
    modified_mask: np.ndarray | None = None,
    *,
    source_layer: str = "<unknown>",
) -> None:
    """Raise ValueError if any emitted block is on the NO_GROW_BLOCKLIST.

    Args:
        block_output: (H, W) str object array from a layer.
        modified_mask: optional bool mask restricting the check to touched pixels.
        source_layer: layer id for the error message.
    """
    if modified_mask is not None:
        to_check = block_output[modified_mask]
    else:
        to_check = block_output.ravel()

    if to_check.size == 0:
        return

    unique = set(to_check.tolist())
    # Drop empty-string sentinel from check.
    unique.discard("")
    bad = unique & NO_GROW_BLOCKLIST
    if bad:
        raise ValueError(
            f"layer {source_layer!r} emitted NO_GROW-blocklisted blocks: "
            f"{sorted(bad)}. See PHYSICAL_REALISM_REFACTOR.md §6 Pass 4."
        )


def assert_palette_safe(palette: Iterable[str], source: str = "<palette>") -> None:
    """Sentinel used by unit tests to block accidental palette additions."""
    bad = set(palette) & NO_GROW_BLOCKLIST
    if bad:
        raise AssertionError(
            f"palette {source!r} contains NO_GROW-blocklisted blocks: "
            f"{sorted(bad)}"
        )
