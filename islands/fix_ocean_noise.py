"""fix_ocean_noise.py — rewrite data/vandir/worldgen/noise_settings/ocean.json
inside assets/vandir_height.zip to a CLEAN deep ocean matching the rendered world:
  * solid seabed at ~Y-60 (matches Vandir deep ocean + island apron seabed_base),
    undulating +-5 via vandir:ocean_floor noise (y_scale 0 => no pillars/gaps/cascade)
  * water 63 -> floor, static, solid floor underneath => cannot cascade or hit lava
  * floor SURFACE = gravel/clay/sand mix (scanned palette: gravel 42 / clay 41 / sand 14)
  * aquifers OFF, lava router 0 => no lava anywhere
Python zipfile rewrite (no `zip` CLI). Run: py islands/fix_ocean_noise.py
"""
import json, zipfile, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "assets" / "vandir_height.zip"
ENTRY = "data/vandir/worldgen/noise_settings/ocean.json"

_floor_block = lambda name: {"type": "minecraft:block", "result_state": {"Name": f"minecraft:{name}"}}
def _noise_band(name, lo, hi, block):
    return {"type": "minecraft:condition",
            "if_true": {"type": "minecraft:noise_threshold", "noise": name,
                        "min_threshold": lo, "max_threshold": hi},
            "then_run": _floor_block(block)}

# Seabed density: y_clamped_gradient solid at the bottom (+3.0 at Y-64) -> air
# (-3.0 at Y-56), crossover at Y-60 (the EXACT Vandir deep-ocean prerender floor:
# the column LUT floors deep-ocean cells at Y-60; deepest-10% mean = -60.0). The
# floor noise (constant in Y, y_scale=0) nudges the crossover +-~1.1 blocks
# (amp 0.8 / slope 0.75), so the seabed undulates ~Y-59..-61 (matching the
# prerender's -59..-61 deep range) and the BOTTOM stays deeply solid (>=+2.2 at
# Y-64) -> physically impossible for water to reach bedrock (no "water to the
# bottom"), no sub-seabed air pocket for fluid.
_SEABED_DENSITY = {
    "type": "minecraft:add",
    "argument1": {"type": "minecraft:y_clamped_gradient",
                  "from_y": -64, "to_y": -56, "from_value": 3.0, "to_value": -3.0},
    "argument2": {"type": "minecraft:mul", "argument1": 0.8,
                  "argument2": {"type": "minecraft:noise", "noise": "vandir:ocean_floor",
                                "xz_scale": 0.3, "y_scale": 0.0}}
}
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
    "spawn_target": [],
    "noise": {"min_y": -64, "height": 384, "size_horizontal": 1, "size_vertical": 2},
    "noise_router": {
        "barrier": 0, "fluid_level_floodedness": 0, "fluid_level_spread": 0, "lava": 0,
        "temperature": 0, "vegetation": 0, "continents": 0, "erosion": 0, "depth": 0,
        "ridges": 0, "preliminary_surface_level": 0, "vein_toggle": 0, "vein_ridged": 0, "vein_gap": 0,
        # initial_density_without_jaggedness is REQUIRED by the 1.21 noise_settings
        # schema; omitting it makes MC reject vandir:ocean and silently fall back to
        # the default overworld generator (= aquifer lava + jagged terrain). MUST
        # mirror final_density.
        "initial_density_without_jaggedness": _SEABED_DENSITY,
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
                 _noise_band("minecraft:surface", -2.0, -0.60, "sand"),    # ~14% low tail
                 _noise_band("minecraft:surface", -0.60, 0.20, "clay"),    # ~41% mid band
                 _floor_block("gravel"),                                    # ~42% default
             ]}},
        ]
    }
}


def main():
    assert ZIP.exists(), ZIP
    json.dumps(OCEAN)  # validate serializable
    tmp = ZIP.with_suffix(".zip.tmp")
    with zipfile.ZipFile(ZIP, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        names = zin.namelist()
        for item in names:
            if item == ENTRY:
                continue
            zout.writestr(zin.getinfo(item), zin.read(item))
        zout.writestr(ENTRY, json.dumps(OCEAN, indent=2))
    shutil.move(str(tmp), str(ZIP))
    print(f"rewrote {ENTRY} in {ZIP.name}")
    # verify
    with zipfile.ZipFile(ZIP) as z:
        d = json.loads(z.read(ENTRY))
    g = d["noise_router"]["final_density"]["argument1"]
    print(f"  floor gradient from_y {g['from_y']} to_y {g['to_y']} -> solid below ~Y{(g['from_y']+g['to_y'])//2}")
    print(f"  sea_level {d['sea_level']}  aquifers {d['aquifers_enabled']}  default {d['default_block']['Name']}")
    seq = d["surface_rule"]["sequence"][1]["then_run"]["sequence"]
    blocks = [s["then_run"]["result_state"]["Name"].split(":")[1] if "then_run" in s else s["result_state"]["Name"].split(":")[1] for s in seq]
    print(f"  seabed surface blocks: {blocks}")


if __name__ == "__main__":
    main()
