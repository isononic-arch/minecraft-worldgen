"""make_island_world.py — assemble a fresh Minecraft world that flies over the
vandir:ocean noise generator, with the rendered islands dropped in.

Bases level.dat + datapacks on an existing datapack world (for a valid 768-height
level.dat), strips its region/, PATCHES the overworld generator to the datapack's
noise-ocean (so blackspace = vandir:ocean), and copies islands/out/*/r.*.mca in.

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="existing world with vandir_height.zip (for level.dat base)")
    ap.add_argument("--dst", required=True, help="new world directory to create")
    ap.add_argument("--names", default="")
    a = ap.parse_args()
    src, dst = Path(a.src), Path(a.dst)
    if dst.exists():
        print(f"refusing to overwrite existing {dst}"); return
    dst.mkdir(parents=True)
    # level.dat + datapacks + icon (NOT region/ -> fresh, never loaded under any old config)
    shutil.copy2(src / "level.dat", dst / "level.dat")
    if (src / "datapacks").exists():
        shutil.copytree(src / "datapacks", dst / "datapacks")
    # Datapack still required for the 768-block EXTENDED HEIGHT (min_y=-64) the flat
    # generator stacks its layers from. The ocean.json noise_settings is now UNUSED
    # (the superflat generator has no noise router) -> the lava risk is moot; no
    # ocean.json check needed.
    (dst / "datapacks").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "assets" / "vandir_height.zip", dst / "datapacks" / "vandir_height.zip")
    for extra in ("icon.png", "session.lock"):
        if (src / extra).exists():
            try: shutil.copy2(src / extra, dst / extra)
            except Exception: pass
    # patch generator -> noise ocean
    lv = nbtlib.load(dst / "level.dat")
    data = lv["Data"] if "Data" in lv else lv
    dims = data["WorldGenSettings"]["dimensions"]
    dims["minecraft:overworld"]["generator"] = FLAT_GEN
    data["LevelName"] = String(dst.name)
    lv.save(dst / "level.dat")
    print(f"patched generator -> minecraft:flat (ocean superflat, seabed Y-60, water Y63; no lava)")
    # install islands — cropped to each island's shelf footprint + merged per-chunk
    # (drops far-ocean chunks so the noise gen fills them as vandir:ocean; no whole-tile
    # overwrite of preexisting ocean, and overlapping island tiles merge not clobber).
    from islands.install_sparse import fast_install              # S95: ~1min vs sparse_install ~60h
    names = [n.strip() for n in a.names.split(",") if n.strip()] or None
    fast_install(dst / "region", names=names)
    print(f"\nworld ready: {dst}\n  islands+shelf installed (raw-copy fast path) + superflat ocean + datapack")
    print("  Open in MC 1.21.x; fly to the island world-coords. NEVER load this world under an old ocean.json.")


if __name__ == "__main__":
    main()
