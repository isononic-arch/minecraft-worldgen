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
from nbtlib import Compound, String

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))               # so `from islands.install_sparse import ...` resolves
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
    # level.dat + datapacks + icon (NOT region/ -> fresh, never loaded under any old config)
    shutil.copy2(src / "level.dat", dst / "level.dat")
    if (src / "datapacks").exists():
        shutil.copytree(src / "datapacks", dst / "datapacks")
    # AUTHORITATIVE datapack: overwrite vandir_height.zip with the canonical assets/ copy
    # (the fixed ocean.json -> aquifers off, no lava, -60 noise seabed). This is the
    # reliability fix: the world is created fresh WITH the corrected ocean.json present,
    # so no chunk is ever generated/frozen under a broken config (CLAUDE.md antipattern #7).
    (dst / "datapacks").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "assets" / "vandir_height.zip", dst / "datapacks" / "vandir_height.zip")
    import zipfile as _zip
    _oc = json.loads(_zip.ZipFile(dst / "datapacks" / "vandir_height.zip").read(
        "data/vandir/worldgen/noise_settings/ocean.json"))
    _nr = _oc.get("noise_router", {})
    print(f"  datapack ocean check: aquifers={_oc.get('aquifers_enabled')} lava={_nr.get('lava')} "
          f"idwj={'initial_density_without_jaggedness' in _nr}  (all must be False/0/True for no-lava)")
    if _oc.get("aquifers_enabled") or _nr.get("lava") or "initial_density_without_jaggedness" not in _nr:
        print("  *** WARNING: ocean.json would produce lava — run islands/fix_ocean_noise.py first ***")
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
    from islands.install_sparse import sparse_install
    names = [n.strip() for n in a.names.split(",") if n.strip()] or None
    sparse_install(dst / "region", names=names)
    print(f"\nworld ready: {dst}\n  islands+shelf installed (no crop) + vandir:ocean noise fill + fixed datapack")
    print("  Open in MC 1.21.x; fly to the island world-coords. NEVER load this world under an old ocean.json.")


if __name__ == "__main__":
    main()
