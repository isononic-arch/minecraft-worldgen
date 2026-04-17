"""
schematic_loader.py — Step 3: Schematic Loader
/core/schematic_loader.py

Loads Sponge .schem files and returns a SchemData namedtuple containing
the block palette and 3D block array. Used by chunk_writer.py to stamp
vegetation schematics into the world volume.

Supported formats:
    - Sponge .schem (NBT, 1.13+ block states)  ← PRIMARY
    - Legacy .schematic (MCEdit, classic IDs)   ← fallback only

SchemData fields:
    width   (int)       — X size
    height  (int)       — Y size  
    length  (int)       — Z size
    blocks  (ndarray)   — (Y, Z, X) object array of block name strings
                          e.g. "minecraft:oak_log[axis=y]"
    palette (dict)      — index → block name string
    anchor  (tuple)     — (ax, ay, az) placement anchor offset, default (0,0,0)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

import numpy as np


@dataclass
class SchemData:
    width:   int
    height:  int
    length:  int
    blocks:  np.ndarray         # (Y, Z, X) object array of block name strings
    palette: dict               # int → str
    anchor:  tuple = (0, 0, 0)  # (ax, ay, az) placement offset


# Classic MCEdit block ID → bare name (partial map, most common)
_CLASSIC_ID_MAP: dict[int, str] = {
    0:   "air",
    1:   "stone",
    2:   "grass_block",
    3:   "dirt",
    4:   "cobblestone",
    5:   "oak_planks",
    6:   "oak_sapling",
    8:   "water",
    9:   "water",
    10:  "lava",
    12:  "sand",
    13:  "gravel",
    14:  "gold_ore",
    15:  "iron_ore",
    16:  "coal_ore",
    17:  "oak_log",
    18:  "oak_leaves",
    20:  "glass",
    24:  "sandstone",
    31:  "short_grass",
    32:  "dead_bush",
    35:  "white_wool",
    37:  "dandelion",
    38:  "poppy",
    43:  "smooth_stone_slab",
    44:  "stone_slab",
    45:  "bricks",
    48:  "mossy_cobblestone",
    53:  "oak_stairs",
    54:  "chest",
    58:  "crafting_table",
    60:  "farmland",
    61:  "furnace",
    64:  "oak_door",
    65:  "ladder",
    67:  "cobblestone_stairs",
    78:  "snow",
    79:  "ice",
    80:  "snow_block",
    81:  "cactus",
    82:  "clay",
    83:  "sugar_cane",
    85:  "oak_fence",
    86:  "carved_pumpkin",
    89:  "glowstone",
    96:  "oak_trapdoor",
    98:  "stone_bricks",
    99:  "brown_mushroom_block",
    100: "red_mushroom_block",
    101: "iron_bars",
    102: "glass_pane",
    106: "vine",
    107: "air",  # fence gate — removed globally (broken blockstates)
    108: "brick_stairs",
    109: "stone_brick_stairs",
    111: "lily_pad",
    126: "oak_slab",
    128: "sandstone_stairs",
    134: "spruce_stairs",
    135: "birch_stairs",
    136: "jungle_stairs",
    154: "hopper",
    161: "acacia_leaves",
    162: "acacia_log",
    163: "acacia_stairs",
    164: "dark_oak_stairs",
    171: "white_carpet",
    175: "tall_grass",
    179: "red_sandstone",
    241: "white_stained_glass",
}


_SPONGE_BLOCK_REMAP: dict[str, str] = {
    "stripped_acacia_log":  "stripped_dark_oak_log",
    "stripped_acacia_wood": "stripped_dark_oak_wood",
    # Fence gates render broken (wrong facing/open state) — remove globally
    "oak_fence_gate":       "air",
    "spruce_fence_gate":    "air",
    "birch_fence_gate":     "air",
    "jungle_fence_gate":    "air",
    "dark_oak_fence_gate":  "air",
    "acacia_fence_gate":    "air",
    "mangrove_fence_gate":  "air",
    "cherry_fence_gate":    "air",
    "bamboo_fence_gate":    "air",
    "crimson_fence_gate":   "air",
    "warped_fence_gate":    "air",
    "pale_oak_fence_gate":  "air",
    # S58: strip baked-in snow accumulation from schematics. Snow on the
    # GROUND (placed by MC's natural snowfall on snowy biomes) is unaffected.
    # 3 sbtaiga spruce variants had hundreds of snow_layer blocks baked into
    # their branches/canopy from the source build; that reads as visually
    # off in lower-altitude/non-snowy areas where those schematics now
    # appear (e.g. zone 35 SNOWY_BOREAL_TAIGA in temperate latitudes).
    "snow":                 "air",
}


def _bare_name(full: str) -> str:
    """Strip minecraft: namespace but PRESERVE blockstate properties.

    'minecraft:oak_fence[north=true]' → 'oak_fence[north=true]'
    'minecraft:stone'                 → 'stone'

    Blockstates are needed for fences (connection), gates (facing/open),
    stairs (facing/half), etc.  The chunk_writer parses them into NBT
    Properties at write time.
    """
    # Split off namespace
    if ":" in full:
        # Handle 'minecraft:oak_fence[state]' → 'oak_fence[state]'
        ns_part = full.split("[")[0]  # 'minecraft:oak_fence'
        if ":" in ns_part:
            bare = ns_part.split(":", 1)[1]  # 'oak_fence'
        else:
            bare = ns_part
        # Re-attach blockstate if present
        if "[" in full:
            state = full[full.index("["):]  # '[north=true]'
            bare = bare + state
    else:
        bare = full

    # Apply remap on the base name only
    base = bare.split("[")[0]
    remapped = _SPONGE_BLOCK_REMAP.get(base)
    if remapped:
        if "[" in bare:
            bare = remapped + bare[bare.index("["):]
        else:
            bare = remapped
    return bare


def load_schem(path: Union[str, Path]) -> SchemData:
    """
    Load a schematic file and return SchemData.

    Raises:
        FileNotFoundError if path doesn't exist.
        ValueError if format is unrecognized.
        ImportError if nbtlib is not installed.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Schematic not found: {path}")

    suffix = path.suffix.lower()

    if suffix == ".schem":
        return _load_sponge_schem(path)
    elif suffix in (".schematic", ".litematic"):
        return _load_classic_schematic(path)
    else:
        # Try sponge first, fall back to classic
        try:
            return _load_sponge_schem(path)
        except Exception:
            return _load_classic_schematic(path)


def _load_sponge_schem(path: Path) -> SchemData:
    """Load Sponge Schematic Format v2/v3 (.schem)."""
    try:
        import nbtlib
    except ImportError:
        raise ImportError("nbtlib required: pip install nbtlib")

    nbt = nbtlib.load(str(path))

    # Handle both root-wrapped and unwrapped
    root = nbt.get("Schematic", nbt)

    W = int(root["Width"])
    H = int(root["Height"])
    L = int(root["Length"])

    # Palette
    palette_nbt = root.get("Palette", root.get("BlockEntities", {}))
    # Sponge v2: Palette is a compound of name→index
    if "Palette" in root:
        palette_nbt = root["Palette"]
        palette = {int(v): str(k) for k, v in palette_nbt.items()}
    else:
        palette = {0: "minecraft:air"}

    # Block data — variable-length encoded integers
    block_data = bytes(root["BlockData"])
    indices = _decode_varint_array(block_data, W * H * L)

    # Build (Y, Z, X) array
    blocks = np.empty((H, L, W), dtype=object)
    for i, idx in enumerate(indices):
        y = i // (L * W)
        z = (i // W) % L
        x = i % W
        name = palette.get(idx, "minecraft:air")
        blocks[y, z, x] = _bare_name(name)

    # Anchor offset
    offset = root.get("Offset", None)
    if offset is not None:
        ax, ay, az = int(offset[0]), int(offset[1]), int(offset[2])
    else:
        ax, ay, az = 0, 0, 0

    return SchemData(
        width=W, height=H, length=L,
        blocks=blocks, palette=palette,
        anchor=(ax, ay, az),
    )


def _decode_varint_array(data: bytes, count: int) -> list[int]:
    """Decode a Minecraft-style VarInt-encoded byte array."""
    result = []
    i = 0
    while len(result) < count and i < len(data):
        value = 0
        shift = 0
        while True:
            if i >= len(data):
                break
            b = data[i]
            i += 1
            value |= (b & 0x7F) << shift
            shift += 7
            if not (b & 0x80):
                break
        result.append(value)
    # Pad with 0 (air) if short
    while len(result) < count:
        result.append(0)
    return result


def _load_classic_schematic(path: Path) -> SchemData:
    """Load legacy MCEdit .schematic format (fallback)."""
    try:
        import nbtlib
    except ImportError:
        raise ImportError("nbtlib required: pip install nbtlib")

    nbt = nbtlib.load(str(path))

    W = int(nbt["Width"])
    H = int(nbt["Height"])
    L = int(nbt["Length"])

    block_ids   = bytes(nbt["Blocks"])
    block_data  = bytes(nbt.get("Data", b"\x00" * len(block_ids)))

    blocks = np.empty((H, L, W), dtype=object)
    palette: dict[int, str] = {}

    for i in range(len(block_ids)):
        y = i // (L * W)
        z = (i // W) % L
        x = i % W
        bid = block_ids[i]
        dat = block_data[i] if i < len(block_data) else 0

        # Handle data-variant blocks (wood type encoded in data bits 0-1)
        if bid == 17:  # log — data&3: 0=oak, 1=spruce, 2=birch, 3=jungle
            name = {0: "oak_log", 1: "spruce_log", 2: "birch_log",
                    3: "jungle_log"}.get(dat & 3, "oak_log")
        elif bid == 18:  # leaves — data&3: 0=oak, 1=spruce, 2=birch, 3=jungle
            name = {0: "oak_leaves", 1: "spruce_leaves", 2: "birch_leaves",
                    3: "jungle_leaves"}.get(dat & 3, "oak_leaves")
        elif bid == 161:  # leaves2 — always dark_oak (matches log2 override)
            name = "dark_oak_leaves"
        elif bid == 162:  # log2 — always dark_oak (acacia too orange for temperate forests)
            name = "dark_oak_log"
        elif bid == 6:  # sapling → map to matching leaves (decorative tips)
            name = {0: "oak_leaves", 1: "spruce_leaves", 2: "birch_leaves",
                    3: "jungle_leaves", 4: "acacia_leaves",
                    5: "dark_oak_leaves"}.get(dat & 7, "oak_leaves")
        elif bid in (95, 159, 35):  # stained glass, stained clay, wool → markers, skip
            name = "air"
        elif bid in (144, 143, 127):  # mob head, button, cocoa → decorative, skip
            name = "air"
        elif bid == 188: name = "spruce_fence"
        elif bid == 189: name = "birch_fence"
        elif bid == 190: name = "jungle_fence"
        elif bid == 191: name = "dark_oak_fence"
        elif bid == 103: name = "melon"
        elif bid == 186: name = "air"  # fence gate removed
        elif bid == 134: name = "spruce_stairs"
        elif bid == 40:  name = "red_mushroom"
        elif bid == 86:  name = "air"  # carved pumpkin — decorative, remove
        elif bid == 126:  # wood slab
            name = {0: "oak_slab", 1: "spruce_slab", 2: "birch_slab",
                    3: "jungle_slab"}.get(dat & 7, "oak_slab")
        else:
            name = _CLASSIC_ID_MAP.get(bid, "air")  # unknown → air (not stone)
        # Apply global remap (snow→air, fence_gate→air, etc.) so classic
        # .schematic files get the same treatment as modern .schem files.
        name = _SPONGE_BLOCK_REMAP.get(name, name)
        blocks[y, z, x] = name
        palette[bid] = name

    return SchemData(
        width=W, height=H, length=L,
        blocks=blocks, palette=palette,
        anchor=(0, 0, 0),
    )


# ---------------------------------------------------------------------------
# SMOKE TEST
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, tempfile, os

    print("schematic_loader.py — smoke test")

    try:
        import nbtlib
    except ImportError:
        print("  SKIP: nbtlib not installed")
        sys.exit(0)

    # Build a minimal Sponge v2 .schem in memory and write it
    import nbtlib, io, gzip

    W, H, L = 3, 4, 3
    palette = {
        "minecraft:air":        nbtlib.Int(0),
        "minecraft:oak_log":    nbtlib.Int(1),
        "minecraft:oak_leaves": nbtlib.Int(2),
        "minecraft:grass_block":nbtlib.Int(3),
    }

    # Simple tree shape: grass base, log column, leaves top
    indices = []
    for y in range(H):
        for z in range(L):
            for x in range(W):
                if y == 0:
                    indices.append(3)  # grass_block
                elif y in (1, 2) and x == 1 and z == 1:
                    indices.append(1)  # oak_log
                elif y == 3:
                    indices.append(2)  # oak_leaves
                else:
                    indices.append(0)  # air

    # Encode as VarInt
    def encode_varint(values):
        out = []
        for v in values:
            while True:
                b = v & 0x7F
                v >>= 7
                if v:
                    out.append(b | 0x80)
                else:
                    out.append(b)
                    break
        return bytes(out)

    block_data = encode_varint(indices)

    nbt_data = nbtlib.File({
        "Schematic": nbtlib.Compound({
            "Width":     nbtlib.Short(W),
            "Height":    nbtlib.Short(H),
            "Length":    nbtlib.Short(L),
            "Palette":   nbtlib.Compound(palette),
            "BlockData": nbtlib.ByteArray(list(block_data)),
        })
    })

    with tempfile.NamedTemporaryFile(suffix=".schem", delete=False) as f:
        tmp = f.name
    nbt_data.save(tmp)

    try:
        sd = load_schem(tmp)
        assert sd.width  == W, f"Width wrong: {sd.width}"
        assert sd.height == H, f"Height wrong: {sd.height}"
        assert sd.length == L, f"Length wrong: {sd.length}"
        assert sd.blocks.shape == (H, L, W), f"Shape wrong: {sd.blocks.shape}"
        assert sd.blocks[0, 0, 0] == "grass_block", f"Base wrong: {sd.blocks[0,0,0]}"
        assert sd.blocks[1, 1, 1] == "oak_log",     f"Log wrong: {sd.blocks[1,1,1]}"
        assert sd.blocks[3, 0, 0] == "oak_leaves",  f"Leaves wrong: {sd.blocks[3,0,0]}"
        print(f"  shape:      {sd.blocks.shape}")
        print(f"  base block: {sd.blocks[0,0,0]}")
        print(f"  log block:  {sd.blocks[1,1,1]}")
        print(f"  top block:  {sd.blocks[3,0,0]}")
        print(f"  palette:    {len(sd.palette)} entries")
        print("PASS")
    finally:
        os.unlink(tmp)

    sys.exit(0)
