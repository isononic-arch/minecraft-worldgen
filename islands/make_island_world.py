"""make_island_world.py — assemble a fresh Minecraft world that flies over the
vandir:ocean noise generator, with the rendered islands dropped in.

Bases level.dat + datapacks on an existing datapack world (for a valid 768-height
level.dat), strips its region/, PATCHES the overworld generator to the datapack's
noise-ocean (so blackspace = vandir:ocean), and copies islands/out/*/r.*.mca in.

Usage: py islands/make_island_world.py --src "<existing datapack world>" --dst "<new world path>" [--names a,b]
"""
import sys, shutil, argparse
from pathlib import Path
import nbtlib
from nbtlib import Compound, String

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "islands" / "out"

NOISE_GEN = Compound({
    "type": String("minecraft:noise"),
    "biome_source": Compound({"type": String("minecraft:fixed"), "biome": String("minecraft:ocean")}),
    "settings": String("vandir:ocean"),
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
    # level.dat + datapacks + icon (NOT region/)
    shutil.copy2(src / "level.dat", dst / "level.dat")
    if (src / "datapacks").exists():
        shutil.copytree(src / "datapacks", dst / "datapacks")
    for extra in ("icon.png", "session.lock"):
        if (src / extra).exists():
            try: shutil.copy2(src / extra, dst / extra)
            except Exception: pass
    # patch generator -> noise ocean
    lv = nbtlib.load(dst / "level.dat")
    data = lv["Data"] if "Data" in lv else lv
    dims = data["WorldGenSettings"]["dimensions"]
    dims["minecraft:overworld"]["generator"] = NOISE_GEN
    data["LevelName"] = String(dst.name)
    lv.save(dst / "level.dat")
    print(f"patched generator -> minecraft:noise / vandir:ocean")
    # install islands — cropped to each island's shelf footprint + merged per-chunk
    # (drops far-ocean chunks so the noise gen fills them as vandir:ocean; no whole-tile
    # overwrite of preexisting ocean, and overlapping island tiles merge not clobber).
    from islands.install_islands import crop_merge_install
    names = [n.strip() for n in a.names.split(",") if n.strip()] or None
    crop_merge_install(dst / "region", names=names, dilate_chunks=4)
    print(f"\nworld ready: {dst}\n  islands cropped to shelf + vandir:ocean fill + vandir_height.zip datapack")
    print("  Open in MC 1.21.x; fly to the island world-coords. (Datapack baked at creation -> noise ocean.)")


if __name__ == "__main__":
    main()
