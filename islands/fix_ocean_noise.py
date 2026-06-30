"""fix_ocean_noise.py — ★ ABANDONED (S98 ocean overhaul) ★

DO NOT RUN. This authored the CUSTOM `vandir:ocean` noise_settings + `vandir:ocean_floor`
noise inside vandir_height.zip — the dead-end approach (a custom noise_settings referenced
from level.dat silently resets MC 1.21.10 to superflat). The ocean generator is now built
by `islands/build_ocean_pack.py`, which OVERRIDES the built-in `minecraft:overworld`
noise_settings with a hard seabed cap (top-slide gradient moved to Y-14). Running this
script would re-add the abandoned `data/vandir/...` files and CORRUPT the new pack.

Kept for reference only (the seabed palette/aquifer notes below). Superseded by
build_ocean_pack.py. See memory/S98_ocean_gen_handoff.md.
"""
import sys
print("fix_ocean_noise.py is ABANDONED — use `py islands/build_ocean_pack.py` instead. "
      "(It would corrupt the new vandir_height.zip by re-adding the dead vandir:ocean files.)")
sys.exit(2)

import json, zipfile, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "assets" / "vandir_height.zip"
ENTRY = "data/vandir/worldgen/noise_settings/ocean.json"
# The noise_settings density function references the custom noise vandir:ocean_floor;
# it MUST be a bound worldgen/noise registry entry or MC fails registry load with
# "Unbound values in registry [minecraft:worldgen/noise]: [vandir:ocean_floor]" (the
# datapack then errors -> "load in safe mode", which disables the 768-height extension
# -> island chunks OOB-crash). Ship it ALONGSIDE the settings so the pack is self-
# consistent. Value = the original (recovered from vandir_height.zip.bak_noise_lava).
NOISE_ENTRY = "data/vandir/worldgen/noise/ocean_floor.json"
OCEAN_FLOOR = {"firstOctave": -6, "amplitudes": [1.0, 1.0]}

_floor_block = lambda name: {"type": "minecraft:block", "result_state": {"Name": f"minecraft:{name}"}}
def _noise_band(name, lo, hi, block):
    return {"type": "minecraft:condition",
            "if_true": {"type": "minecraft:noise_threshold", "noise": name,
                        "min_threshold": lo, "max_threshold": hi},
            "then_run": _floor_block(block)}

# Seabed density (S98 — user: raise the ocean floor to MEET the island apron ~Y-16
# instead of -60, add SOME bathymetry + gravel/clay/stone variation):
#   gradient crossover (density=0) at ~Y-18 (from_y=-30 +3 -> to_y=-6 -3, slope 0.25/blk);
#   a BROAD low-freq noise term (same vandir:ocean_floor param at coarse xz_scale=0.06,
#   amp 1.4 -> +-~5.6 blk) undulates the crossover across the map = bathymetry ~Y-12..-26
#   meeting the apron; a FINE noise (xz_scale 0.35, amp 0.7 -> +-~2.8 blk) adds texture.
#   BOTTOM stays solid: at Y-64 the gradient is clamped to +3.0, so density >= +0.9 even
#   at the noise trough -> water can NEVER reach bedrock; aquifers off + lava router 0 =>
#   no lava. y_scale=0 keeps both noises constant-in-Y (no pillars/cascade).
_SEABED_DENSITY = {
    "type": "minecraft:add",
    "argument1": {
        "type": "minecraft:add",
        "argument1": {"type": "minecraft:y_clamped_gradient",
                      "from_y": -30, "to_y": -6, "from_value": 3.0, "to_value": -3.0},
        "argument2": {"type": "minecraft:mul", "argument1": 1.4,
                      "argument2": {"type": "minecraft:noise", "noise": "vandir:ocean_floor",
                                    "xz_scale": 0.4, "y_scale": 0.0}},   # broad swells ~320-blk wavelength
    },
    "argument2": {"type": "minecraft:mul", "argument1": 0.7,
                  "argument2": {"type": "minecraft:noise", "noise": "vandir:ocean_floor",
                                "xz_scale": 1.0, "y_scale": 0.0}},        # finer ~128-blk detail
}
# NOTE the gradient slope (0.25/block) AMPLIFIES the noise 4x: seabed Y = -18 + 4*(1.4*broad +
# 0.7*fine) -> swing +-8.4 blocks over [-26,-10] (within the [-30,-6] gradient span, no saturation).
# So amplitude was always fine; the OLD xz_scale=0.06 gave a ~2100-block wavelength = looked dead flat.
OCEAN = {
    "sea_level": 63,
    "disable_mob_generation": False,
    # aquifers OFF + lava router 0 + default_fluid water => NO lava anywhere, ever.
    # (The "lava under the water" was MC's default aquifer lava floor, which only
    # appears when aquifers are enabled OR when the custom settings fail to load
    # and MC falls back to minecraft:overworld. Both are foreclosed here.)
    "aquifers_enabled": False,
    "ore_veins_enabled": False,
    "legacy_random_source": False,
    "default_block": {"Name": "minecraft:stone"},
    "default_fluid": {"Name": "minecraft:water", "Properties": {"level": "0"}},
    # S98: match vanilla's spawn_target exactly (empty [] may be why the spawn search /
    # gen validation choked). Copied from 1.21.10 minecraft:overworld noise_settings.
    "spawn_target": [
        {"continentalness": [-0.11, 1.0], "depth": 0.0, "erosion": [-1.0, 1.0],
         "humidity": [-1.0, 1.0], "offset": 0.0, "temperature": [-1.0, 1.0], "weirdness": [-1.0, -0.16]},
        {"continentalness": [-0.11, 1.0], "depth": 0.0, "erosion": [-1.0, 1.0],
         "humidity": [-1.0, 1.0], "offset": 0.0, "temperature": [-1.0, 1.0], "weirdness": [0.16, 1.0]},
    ],
    "noise": {"min_y": -64, "height": 768, "size_horizontal": 1, "size_vertical": 2},  # matches the custom 768-height dimension
    "noise_router": {
        "barrier": 0, "fluid_level_floodedness": 0, "fluid_level_spread": 0, "lava": 0,
        "temperature": 0, "vegetation": 0, "continents": 0, "erosion": 0, "depth": 0,
        "ridges": 0, "preliminary_surface_level": 0, "vein_toggle": 0, "vein_ridged": 0, "vein_gap": 0,
        # S98 ROOT-CAUSE FIX: vanilla 1.21.10 noise_router does NOT contain
        # `initial_density_without_jaggedness` (it was a 1.18-1.20 field, removed since).
        # Including it made MC's strict noise_settings codec REJECT vandir:ocean -> the
        # generator silently fell back and level.dat reset to superflat. Removed to match
        # vanilla exactly. (The old comment claiming it was "required" predated 1.21.)
        "final_density": _SEABED_DENSITY,
    },
    "surface_rule": {
        "type": "minecraft:sequence",
        "sequence": [
            # bedrock floor
            {"type": "minecraft:condition",
             "if_true": {"type": "minecraft:vertical_gradient",
                         "random_name": "minecraft:bedrock_floor",
                         "true_at_and_below": {"above_bottom": 0},
                         "false_at_and_above": {"above_bottom": 1}},
             "then_run": _floor_block("bedrock")},
            # seabed SURFACE: gravel-dominant with clay + sand patches (scanned ocean palette)
            {"type": "minecraft:condition",
             "if_true": {"type": "minecraft:stone_depth", "offset": 0, "surface_type": "floor",
                         "add_surface_depth": False, "secondary_depth_range": 0},
             "then_run": {"type": "minecraft:sequence", "sequence": [
                 _noise_band("minecraft:surface", -2.0, -0.70, "sand"),    # sandy shallows
                 _noise_band("minecraft:surface", -0.70, -0.05, "gravel"), # gravel
                 _noise_band("minecraft:surface", -0.05, 0.55, "clay"),    # clay
                 _floor_block("stone"),                                     # S98: rocky outcrops (default)
             ]}},
        ]
    }
}


def main():
    assert ZIP.exists(), ZIP
    json.dumps(OCEAN)  # validate serializable
    tmp = ZIP.with_suffix(".zip.tmp")
    PACK_MCMETA = "pack.mcmeta"
    with zipfile.ZipFile(ZIP, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        names = zin.namelist()
        for item in names:
            if item in (ENTRY, NOISE_ENTRY, PACK_MCMETA):
                continue
            zout.writestr(zin.getinfo(item), zin.read(item))
        zout.writestr(ENTRY, json.dumps(OCEAN, indent=2))
        zout.writestr(NOISE_ENTRY, json.dumps(OCEAN_FLOOR, indent=2))
        # S98 CRITICAL FIX (the flat-sand-ocean root cause): the old pack.mcmeta used the
        # 1.20.5-era `supported_formats` WITHOUT the min_format/max_format fields 1.21.9+
        # requires -> MC 1.21.10 "Couldn't load pack metadata" -> the datapack's worldgen
        # (vandir:ocean) never registered -> MC rejected the noise generator + silently RESET
        # level.dat to superflat (the flat-sand ocean). The EXACT working schema (copied from
        # a 1.21.10 mod datapack on this machine, e.g. collective-1.21.10): pack_format 88 +
        # integer min_format/max_format = 88. 1.21.10 datapack format is 88, NOT 81.
        zout.writestr(PACK_MCMETA, json.dumps({"pack": {
            "pack_format": 88, "min_format": 88, "max_format": 88,
            "description": "Vandir height-768 + vandir:ocean noise filler"}}, indent=2))
    shutil.move(str(tmp), str(ZIP))
    print(f"rewrote {ENTRY} + {NOISE_ENTRY} in {ZIP.name}")
    # verify
    with zipfile.ZipFile(ZIP) as z:
        assert NOISE_ENTRY in z.namelist(), f"{NOISE_ENTRY} missing -> would be unbound -> safe-mode error"
        d = json.loads(z.read(ENTRY))
    g = d["noise_router"]["final_density"]["argument1"]["argument1"]  # nested add -> gradient
    print(f"  floor gradient from_y {g['from_y']} to_y {g['to_y']} -> seabed crossover ~Y{(g['from_y']+g['to_y'])//2}")
    print(f"  sea_level {d['sea_level']}  aquifers {d['aquifers_enabled']}  default {d['default_block']['Name']}")
    seq = d["surface_rule"]["sequence"][1]["then_run"]["sequence"]
    blocks = [s["then_run"]["result_state"]["Name"].split(":")[1] if "then_run" in s else s["result_state"]["Name"].split(":")[1] for s in seq]
    print(f"  seabed surface blocks: {blocks}")


if __name__ == "__main__":
    main()
