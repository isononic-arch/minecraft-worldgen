"""install_sparse.py — copy the rendered island + shelf-buffer tiles into a world's
region/ folder WITHOUT cropping. The render (render_drive buffer_tiles>=1) already
emits only the island land + feathered-shelf-buffer tiles, so every rendered chunk is
wanted — no far-ocean drop. Chunks are merged per-chunk as raw sector blobs:

  * Bigger island wins where two islands contest a chunk.
  * Island chunks OVERLAY onto any pre-existing destination region (so islands slot
    into a fresh noise-ocean world OR the real Vandir prerender — the island's land+
    shelf chunks replace those slots, every other pre-existing chunk is preserved).
  * Beyond the rendered shelf buffer, the world's own generator fills (vandir:ocean
    noise -60 seabed, or the mainland prerender) -> the feathered shelf blends in.

Usage:
  py islands/install_sparse.py --world "<save path>" [--names kostati,grand_turk]
"""
import sys, json, re, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.install_islands import read_region, write_region, safe_name

ISL = ROOT / "islands"; MASKS_OUT = ISL / "masks_islands"; OUT = ISL / "out"


def sparse_install(dst_region: Path, names=None):
    lay = json.loads((ISL / "layout.json").read_text())["islands"]
    recs = []
    for e in lay:
        nm = safe_name(e["name"]); d = MASKS_OUT / nm; o = OUT / nm
        if names and not any(n in nm or n in e["dem_path"] for n in names):
            continue
        if not o.exists() or not list(o.glob("r.*.mca")):
            continue
        man = json.loads((d / "manifest.json").read_text()) if (d / "manifest.json").exists() else {"world_hw": [0, 0]}
        recs.append((nm, o, man))
    # bigger island first so it wins contested chunks
    recs.sort(key=lambda r: -(r[2]["world_hw"][0] * r[2]["world_hw"][1]))

    # 1) gather all island chunks per world-region (island, big-first, wins ties)
    island_reg: dict[str, dict] = {}
    for nm, odir, man in recs:
        kept = 0
        for mca in sorted(odir.glob("r.*.mca")):
            if not re.match(r"r\.(-?\d+)\.(-?\d+)\.mca", mca.name):
                continue
            reg = island_reg.setdefault(mca.name, {})
            for (cx, cz), raw in read_region(mca).items():
                if (cx, cz) not in reg:               # bigger island already placed here
                    reg[(cx, cz)] = raw; kept += 1
        print(f"  {nm:38s} {kept:5d} island+shelf chunks", flush=True)

    # 2) overlay onto any pre-existing destination region, write back
    dst_region.mkdir(parents=True, exist_ok=True)
    n = 0
    for rname, ich in island_reg.items():
        if not ich:
            continue
        dpath = dst_region / rname
        base = read_region(dpath) if dpath.exists() else {}
        base.update(ich)                              # island chunks replace their slots; others kept
        write_region(dpath, base); n += 1
    print(f"\ninstalled {n} region files (no crop) -> {dst_region}")
    print("  island+shelf chunks overlaid; beyond the buffer the world generator fills (noise/prerender)")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--names", default="")
    a = ap.parse_args()
    world = Path(a.world); reg = world / "region"
    dp = world / "datapacks" / "vandir_height.zip"
    if not dp.exists():
        print(f"WARNING: {dp} missing — 48-section island chunks will OOB-crash on load. "
              f"Install vandir_height.zip into {world / 'datapacks'} BEFORE first load.")
    names = [n.strip() for n in a.names.split(",") if n.strip()] or None
    sparse_install(reg, names=names)


if __name__ == "__main__":
    main()
