"""build_ocean_pack.py — build the Vandir ocean datapack.

Turns a plain **Default** world into an all-ocean seabed with a HARD CEILING at
~Y CAP_TOP (no mountains can ever poke above it), the 768 world-height, and ocean
biomes (kelp/seagrass/gravel seabed). One self-contained pack, no load-order games.

TECHNIQUE (proven by the "Ocean Only" reference pack): vanilla terrain height is
gated by a "top slide" y_clamped_gradient (1@240 -> 0@256) that fades density to AIR
near build height. We move that slide DOWN to (1@CAP_BOTTOM -> 0@CAP_TOP), so nothing
solid generates above CAP_TOP. Everything above fills with sea water to sea_level 63.
Below the slide you still get gentle vanilla bathymetry. This lives INLINE in the
noise_settings noise_router (final_density + preliminary_surface_level), so we ship a
patched copy of vanilla 1.21.10's noise_settings/overworld.json. A custom noise_settings
REFERENCED from level.dat resets MC to superflat; OVERRIDING the built-in vanilla file
is never rejected.

CAP_TOP = the highest the seabed can reach. Default -14 to meet the islands' ~-16 apron.
Lower it for a deeper seabed. aquifers_enabled=false guarantees a clean water column
(no lava pockets, no patchy aquifers) for the ocean filler.

Composition (single pack, pack_format 88 = 1.21.10):
  worldgen/noise_settings/overworld.json  <- vanilla 1.21.10, top-slides -> CAP, aquifers off
  dimension/overworld.json                <- noise generator + fixed deep_ocean biome source
                                             (type=minecraft:overworld -> uses the 768 dim_type below)
  dimension_type/overworld.json + _caves  <- 768 height (from vandir_height.zip)
  worldgen/biome/*.json                    <- no-snow land-biome overrides (from vandir_height.zip,
                                             inert in open ocean, needed once islands are placed)

Usage:
  py islands/build_ocean_pack.py [--cap-top -14] [--cap-bottom -30] [--biome minecraft:deep_ocean]
     [--out "C:/Users/nicho/Downloads/vandir_ocean_TEST.zip"]
"""
import json, argparse, zipfile, io
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JAR = Path(r"C:/Users/nicho/AppData/Roaming/ModrinthApp/meta/versions/1.21.10/1.21.10.jar")
# Stable source for the 768 dimension_type + no-snow biome overrides. This is a snapshot
# of the pre-ocean vandir_height.zip; it is NOT the deployed pack (avoid circular read).
HEIGHT_PACK = ROOT / "assets" / "vandir_height_base.zip"
PACK_FORMAT = 88  # MC 1.21.10

VANILLA_TOP_SLIDE = (240, 256)  # (from_y, to_y) of the slides we retarget


def retarget_top_slides(node, cap_bottom, cap_top, counter):
    """Recursively move every (1@240 -> 0@256) y_clamped_gradient to (1@cap_bottom -> 0@cap_top)."""
    if isinstance(node, dict):
        if (node.get("type") == "minecraft:y_clamped_gradient"
                and node.get("from_y") == VANILLA_TOP_SLIDE[0]
                and node.get("to_y") == VANILLA_TOP_SLIDE[1]):
            node["from_y"] = cap_bottom
            node["to_y"] = cap_top
            counter[0] += 1
        for v in node.values():
            retarget_top_slides(v, cap_bottom, cap_top, counter)
    elif isinstance(node, list):
        for v in node:
            retarget_top_slides(v, cap_bottom, cap_top, counter)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap-top", type=int, default=-14,
                    help="highest Y the seabed can reach (hard ceiling)")
    ap.add_argument("--cap-bottom", type=int, default=-30,
                    help="Y where the top-slide begins (full terrain below this)")
    ap.add_argument("--biome", default="minecraft:deep_ocean",
                    help="fixed ocean biome for the filler")
    ap.add_argument("--aquifers", action="store_true",
                    help="enable aquifers (default OFF for clean water column / no lava)")
    ap.add_argument("--sea-level", type=int, default=64,
                    help="generator sea_level. MC fills water up to sea_level-1, so 64 -> topmost "
                         "water Y63, matching the rendered pipeline (chunk_writer water_mask abs_y<=SEA_Y=63). "
                         "Vanilla 63 would top at Y62 (one block low vs the rendered ocean).")
    ap.add_argument("--out", default=str(ROOT / "assets" / "vandir_height.zip"),
                    help="deployed pack (default: the canonical auto-installed assets/vandir_height.zip)")
    a = ap.parse_args()

    # 1) vanilla 1.21.10 noise_settings, patched
    with zipfile.ZipFile(JAR) as jar:
        noise = json.loads(jar.read("data/minecraft/worldgen/noise_settings/overworld.json"))
    cnt = [0]
    retarget_top_slides(noise["noise_router"], a.cap_bottom, a.cap_top, cnt)
    if cnt[0] < 2:
        raise SystemExit(f"ERROR: expected >=2 top-slide gradients, retargeted {cnt[0]} "
                         "(vanilla structure changed?) — aborting.")
    noise["aquifers_enabled"] = bool(a.aquifers)
    noise["sea_level"] = int(a.sea_level)

    # 2) ocean dimension (noise generator, fixed ocean biomes) -> replaces the flat one
    dim = {
        "type": "minecraft:overworld",
        "generator": {
            "type": "minecraft:noise",
            "settings": "minecraft:overworld",
            "biome_source": {"type": "minecraft:fixed", "biome": a.biome},
        },
    }

    # 3) pull 768 dimension_type + no-snow biome overrides from the height pack
    carry = {}
    with zipfile.ZipFile(HEIGHT_PACK) as hp:
        for n in hp.namelist():
            if (n.startswith("data/minecraft/dimension_type/")
                    or n.startswith("data/minecraft/worldgen/biome/")):
                carry[n] = hp.read(n)

    mcmeta = {
        "pack": {
            "pack_format": PACK_FORMAT, "min_format": PACK_FORMAT, "max_format": PACK_FORMAT,
            "description": f"Vandir ocean: seabed cap Y{a.cap_top} (slide {a.cap_bottom}->{a.cap_top}), "
                           f"768 height, aquifers={'on' if a.aquifers else 'off'}",
        }
    }

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("pack.mcmeta", json.dumps(mcmeta, indent=2))
        z.writestr("data/minecraft/worldgen/noise_settings/overworld.json", json.dumps(noise))
        z.writestr("data/minecraft/dimension/overworld.json", json.dumps(dim, indent=2))
        for n, b in carry.items():
            z.writestr(n, b)

    print(f"wrote {out}")
    print(f"  seabed cap: Y{a.cap_top}  (slide {a.cap_bottom} -> {a.cap_top})  "
          f"top-slides retargeted: {cnt[0]}")
    print(f"  aquifers_enabled: {bool(a.aquifers)}   sea_level: {a.sea_level} (top water Y{a.sea_level-1})   biome: {a.biome}")
    print(f"  carried {len(carry)} files from height pack (768 dim_type + no-snow biomes)")


if __name__ == "__main__":
    main()
