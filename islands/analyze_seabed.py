"""analyze_seabed.py — read GENERATED region chunks of a world and report the
seabed Y distribution + how much terrain pokes above sea level. Used to tune the
ocean-generator depth DROP (override of minecraft:overworld/depth).

Reads GENERATED blocks (never masks). Reports, over a sample of columns:
  - seabed Y = topmost solid (non-air, non-fluid) block per column
  - surface block name at the seabed
  - how many columns poke above sea level (Y > SEA) = islets/land
  - water column presence

Usage:
  py islands/analyze_seabed.py --region "D:/modrinth_vandir/saves/<world>/region" [--sea 63] [--max-regions 4] [--step 4]
"""
import sys, struct, zlib, gzip, io, argparse
from pathlib import Path
from collections import Counter
import numpy as np
import nbtlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FLUIDS = {"minecraft:water", "minecraft:lava", "minecraft:flowing_water", "minecraft:flowing_lava"}
AIR = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}


def read_chunk(f, lx, lz):
    idx = (lz % 32) * 32 + (lx % 32)
    f.seek(idx * 4); loc = f.read(4)
    off = (loc[0] << 16) | (loc[1] << 8) | loc[2]; secs = loc[3]
    if off == 0 or secs == 0:
        return None
    f.seek(off * 4096); length = struct.unpack(">I", f.read(4))[0]
    ct = f.read(1)[0]; data = f.read(length - 1)
    raw = gzip.decompress(data) if ct == 1 else zlib.decompress(data) if ct == 2 else data
    return nbtlib.File.parse(io.BytesIO(raw))


def section_blocks(sec):
    bs = sec.get("block_states")
    if bs is None:
        return None
    pal = bs.get("palette")
    if pal is None:
        return None
    names = [str(p.get("Name", "minecraft:air")) for p in pal]
    if len(names) == 1:
        return np.full((16, 16, 16), names[0], dtype=object)
    data = bs.get("data")
    if data is None:
        return np.full((16, 16, 16), names[0], dtype=object)
    bits = max(4, (len(names) - 1).bit_length())
    per = 64 // bits
    mask = np.uint64((1 << bits) - 1)
    longs = np.asarray(data, dtype=np.uint64)
    shifts = (np.arange(per, dtype=np.uint64) * np.uint64(bits))
    idx = ((longs[:, None] >> shifts[None, :]) & mask).reshape(-1)[:4096].astype(np.int64)
    arr = np.array(names, dtype=object)[np.clip(idx, 0, len(names) - 1)]
    return arr.reshape(16, 16, 16)  # (y,z,x)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--sea", type=int, default=63)
    ap.add_argument("--max-regions", type=int, default=4)
    ap.add_argument("--step", type=int, default=4, help="sample every Nth column")
    a = ap.parse_args()

    rdir = Path(a.region)
    mcas = sorted(rdir.glob("r.*.mca"))[: a.max_regions]
    if not mcas:
        print(f"NO region files in {rdir}"); return

    seabed_ys = []          # topmost solid Y per sampled column
    surf_blocks = Counter()  # block name at seabed top
    water_cols = 0
    land_cols = 0           # solid top above sea
    total_cols = 0

    for mca in mcas:
        with open(mca, "rb") as f:
            for lz in range(32):
                for lx in range(32):
                    try:
                        ch = read_chunk(f, lx, lz)
                    except Exception:
                        ch = None
                    if ch is None:
                        continue
                    secs = ch.get("sections") or ch.get("Sections") or []
                    by_y = {}
                    for s in secs:
                        b = section_blocks(s)
                        if b is not None:
                            by_y[int(s.get("Y", 0))] = b
                    if not by_y:
                        continue
                    ys_sorted = sorted(by_y.keys(), reverse=True)
                    for zz in range(0, 16, a.step):
                        for xx in range(0, 16, a.step):
                            total_cols += 1
                            top_solid_y = None
                            top_solid_nm = None
                            has_water = False
                            for secy in ys_sorted:
                                blk = by_y[secy]
                                for yy in range(15, -1, -1):
                                    nm = str(blk[yy, zz, xx])
                                    if nm in FLUIDS:
                                        if "water" in nm:
                                            has_water = True
                                        continue
                                    if nm in AIR:
                                        continue
                                    top_solid_y = secy * 16 + yy
                                    top_solid_nm = nm
                                    break
                                if top_solid_y is not None:
                                    break
                            if top_solid_y is None:
                                continue
                            seabed_ys.append(top_solid_y)
                            surf_blocks[top_solid_nm.replace("minecraft:", "")] += 1
                            if has_water:
                                water_cols += 1
                            if top_solid_y > a.sea:
                                land_cols += 1

    if not seabed_ys:
        print("NO solid columns found (all air?)"); return
    arr = np.array(seabed_ys)
    print(f"World: {rdir.parent.name}   regions read: {len(mcas)}   sampled columns: {total_cols}")
    print(f"SEA level: {a.sea}")
    print(f"Seabed/top-solid Y:  min={arr.min()}  p5={np.percentile(arr,5):.0f}  "
          f"median={np.median(arr):.0f}  mean={arr.mean():.1f}  p95={np.percentile(arr,95):.0f}  max={arr.max()}")
    print(f"Columns with water above: {water_cols}/{len(arr)} ({100*water_cols/len(arr):.1f}%)")
    print(f"Columns poking ABOVE sea (Y>{a.sea}) = land/islets: {land_cols}/{len(arr)} ({100*land_cols/len(arr):.1f}%)")
    submerged = arr[arr <= a.sea]
    if len(submerged):
        print(f"Submerged seabed Y: median={np.median(submerged):.0f}  "
              f"range [{submerged.min()}..{submerged.max()}]  "
              f"depth below sea: median={a.sea-np.median(submerged):.0f} blocks")
    print("Top seabed-surface blocks:")
    for nm, c in surf_blocks.most_common(12):
        print(f"   {nm:24s} {c:6d} ({100*c/len(arr):.1f}%)")
    # crude superflat detector
    if arr.min() == arr.max():
        print("\n!! ALL columns same Y -> SUPERFLAT fallback (generator was reset).")
    elif land_cols == len(arr):
        print("\n!! No water columns -> generator did NOT drown the land (DROP too small or override ignored).")


if __name__ == "__main__":
    main()
