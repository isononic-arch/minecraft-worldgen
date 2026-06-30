"""make_island_world.py — assemble a fresh Minecraft world that flies over the
hard-capped deep-ocean noise generator, with the rendered islands dropped in.

Bases level.dat + datapacks on an existing datapack world (for a valid 768-height
level.dat), strips its region/, PATCHES the overworld generator to a minecraft:noise
generator referencing the built-in `minecraft:overworld` noise_settings (which
vandir_height.zip overrides with a Y-14 seabed cap -> ocean everywhere), and copies
islands/out/*/r.*.mca in. Blackspace fills with capped deep ocean (no lava, no land).

Usage: py islands/make_island_world.py --src "<existing datapack world>" --dst "<new world path>" [--names a,b]
"""
import sys, shutil, argparse, json
from pathlib import Path
import nbtlib
from nbtlib import Compound, String, Int, Byte, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))               # so `from islands.install_sparse import ...` resolves
OUT = ROOT / "islands" / "out"

# S95 #9 (polish2): SUPERFLAT far-ocean filler, COPIED EXACTLY from the trusted
# Vandir50k_verify world (the reference mainland world). The earlier cut added a
# `structure_overrides: []` key that the verify world OMITS — MC 1.21's flat-settings
# codec rejected the empty list, fell back to the DEFAULT noise overworld, and that
# fallback is what produced the aquifer LAVA ("Bahamas = lava ocean") + a wrong sea
# level. The verify settings have NO structure_overrides at all. Layers stack from the
# dimension floor (min_y=-64): bedrock -> deepslate30 -> stone30 -> gravel3 -> sand2
# -> water62, so the seabed sand top is Y1 and the water surface is exactly Y63 (sea
# level). features=0, lakes=0, biome=ocean. NO noise router -> lava impossible.
FLAT_GEN = Compound({
    "type": String("minecraft:flat"),
    "settings": Compound({
        "features": Byte(0),
        "biome": String("minecraft:ocean"),
        "lakes": Byte(0),
        "layers": List[Compound]([
            Compound({"block": String("minecraft:bedrock"),  "height": Int(1)}),
            Compound({"block": String("minecraft:deepslate"), "height": Int(30)}),
            Compound({"block": String("minecraft:stone"),    "height": Int(30)}),
            Compound({"block": String("minecraft:gravel"),   "height": Int(3)}),
            Compound({"block": String("minecraft:sand"),     "height": Int(2)}),
            Compound({"block": String("minecraft:water"),    "height": Int(62)}),
        ]),
        # NO structure_overrides — matches verify; adding it broke the codec.
    }),
})

# S98 (ocean overhaul, FINAL): the hard-capped DEEP-OCEAN noise generator. References
# the BUILT-IN `minecraft:overworld` noise_settings, which vandir_height.zip OVERRIDES
# (built by islands/build_ocean_pack.py) so its terrain "top slide" is moved to Y-14 ->
# nothing solid generates above ~Y-14, water fills to sea 63, gentle bathymetry below,
# NO mountains. aquifers OFF in the patched settings => clean water column, no lava.
# A VANILLA settings reference is the key fix: a CUSTOM (vandir:ocean) reference made MC
# 1.21.10 silently reset to superflat; minecraft:overworld is never rejected. The fixed
# deep_ocean biome carries seagrass + kelp natively, so the generator decorates the
# seabed (superflat features=0 cannot). The seabed (~Y-14/-16) meets the rendered island
# apron (~-16) -> no tile-step -> keep the apron (no open-ocean cull).
NOISE_GEN = Compound({
    "type": String("minecraft:noise"),
    "settings": String("minecraft:overworld"),
    "biome_source": Compound({
        "type": String("minecraft:fixed"),
        "biome": String("minecraft:deep_ocean"),
    }),
})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="existing world with vandir_height.zip (for level.dat base)")
    ap.add_argument("--dst", required=True, help="new world directory to create")
    ap.add_argument("--names", default="")
    ap.add_argument("--gen", choices=["noise", "flat"], default="noise",
                    help="ocean filler: 'noise'=capped minecraft:overworld (seabed ~Y-14/-16, "
                         "gravel/clay/sand, seagrass+kelp via deep_ocean biome, matches the rendered apron — DEFAULT); "
                         "'flat'=superflat (bulletproof, flat Y2 sand, no veg — safe fallback only)")
    ap.add_argument("--drop-ocean", action="store_true",
                    help="cull all-ocean island regions to the generator (the S98 bandaid). Default OFF: "
                         "keep the rendered shelf apron — it now matches the Y-60 noise seabed (no tile-step).")
    a = ap.parse_args()
    src, dst = Path(a.src), Path(a.dst)
    if dst.exists():
        print(f"refusing to overwrite existing {dst}"); return
    dst.mkdir(parents=True)
    # level.dat + datapacks + icon (NOT region/ -> fresh, never loaded under any old config)
    shutil.copy2(src / "level.dat", dst / "level.dat")
    if (src / "datapacks").exists():
        shutil.copytree(src / "datapacks", dst / "datapacks")
    # Datapack required for the 768-block EXTENDED HEIGHT (min_y=-64). For --gen noise it
    # ALSO supplies the OVERRIDE of minecraft:overworld noise_settings with the Y-14 seabed
    # cap; without it the generator would produce normal vanilla LAND (mountains), not ocean.
    # Assert the cap is present BEFORE creating the world rather than finding land in-game.
    (dst / "datapacks").mkdir(exist_ok=True)
    _dp = ROOT / "assets" / "vandir_height.zip"
    if a.gen == "noise":
        import zipfile as _zf
        _NS = "data/minecraft/worldgen/noise_settings/overworld.json"
        with _zf.ZipFile(_dp) as _z:
            _txt = _z.read(_NS).decode("utf-8", "ignore") if _NS in _z.namelist() else ""
            # capped pack has the vanilla top-slide (to_y 256) RETARGETED away; a stock/missing
            # settings still carries "to_y": 256 -> would generate normal vanilla land, not ocean.
            ok = bool(_txt) and '"to_y": 256' not in _txt
            if not ok:
                print("!! ABORT: --gen noise but the datapack's minecraft:overworld noise_settings is "
                      "missing or NOT capped (still has the vanilla to_y=256 top-slide) -> MC would "
                      "generate normal vanilla land, not ocean. Rebuild: `py islands/build_ocean_pack.py`.")
                shutil.rmtree(dst, ignore_errors=True)
                return
    shutil.copy2(_dp, dst / "datapacks" / "vandir_height.zip")
    for extra in ("icon.png", "session.lock"):
        if (src / extra).exists():
            try: shutil.copy2(src / extra, dst / extra)
            except Exception: pass
    # patch generator -> noise ocean
    lv = nbtlib.load(dst / "level.dat")
    data = lv["Data"] if "Data" in lv else lv
    dims = data["WorldGenSettings"]["dimensions"]
    gen = NOISE_GEN if a.gen == "noise" else FLAT_GEN
    dims["minecraft:overworld"]["generator"] = gen
    data["LevelName"] = String(dst.name)
    lv.save(dst / "level.dat")
    if a.gen == "noise":
        print("patched generator -> minecraft:noise (minecraft:overworld capped at Y-14; seabed "
              "~Y-14/-16 gravel/clay/sand, water Y63, seagrass+kelp via deep_ocean biome; aquifers off -> no lava)")
    else:
        print("patched generator -> minecraft:flat (ocean superflat, flat Y2 sand seabed, no veg; safe fallback)")
    # install islands — cropped to each island's shelf footprint + merged per-chunk
    # (drops far-ocean chunks so the noise gen fills them as vandir:ocean; no whole-tile
    # overwrite of preexisting ocean, and overlapping island tiles merge not clobber).
    from islands.install_sparse import fast_install              # S95: ~1min vs sparse_install ~60h
    names = [n.strip() for n in a.names.split(",") if n.strip()] or None
    fast_install(dst / "region", names=names)
    # S98 (default OFF): the open-ocean cull was a bandaid for the superflat (Y2) seabed
    # not matching the rendered apron (Y-60). With --gen noise the generator seabed IS at
    # Y-60, so the apron meets it seamlessly and we KEEP the rendered ocean (palette + veg).
    # Only cull if explicitly asked (e.g. with --gen flat, where the step is unavoidable).
    if a.drop_ocean:
        from islands._drop_ocean_regions import drop_ocean_regions
        drop_ocean_regions(dst / "region", apply=True)
        print(f"\nworld ready: {dst}\n  islands+shelf installed (raw-copy) + open-ocean tiles culled to the generator + datapack")
    else:
        print(f"\nworld ready: {dst}\n  ALL rendered island+shelf+apron tiles installed (no cull) + datapack")
    print("  Open in MC 1.21.x; fly to the island world-coords. FIRST CHECK: fly over open ocean — "
          "confirm WATER + seagrass/kelp on the seabed (~Y-14/-16). Visible LAND/mountains => the datapack "
          "lost the Y-14 cap (rebuild via build_ocean_pack.py).")


if __name__ == "__main__":
    main()
