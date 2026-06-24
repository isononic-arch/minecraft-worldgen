"""install_islands.py — crop each island to its SHELF FOOTPRINT, then merge-install
the rendered .mca into a Minecraft world's region/ folder.

Old behavior blind-copied full 512x512 region tiles, so an island's ocean overwrote
whole tiles of the preexisting ocean and overlapping island tiles clobbered each
other. Now we keep only the chunks within (land dilated by the seabed apron + feather)
and DROP the far-ocean chunks; in the noise-ocean world MC regenerates them as
vandir:ocean, so each island reads as a cropped contour hugging its subaquatic shelf
and overlapping tiles MERGE per-chunk (bigger island wins) instead of overwriting.
Chunks are relocated as raw sector blobs (no NBT decode) -> fast + lossless.

The world MUST have vandir_height.zip in datapacks/ (48-section height) and be created
with the noise-ocean generator so dropped/blackspace chunks fill with vandir:ocean.

Usage:
  py islands/install_islands.py --world "<path-to-save>"            # all islands
  py islands/install_islands.py --world ... --names kostati,grenada
  py islands/install_islands.py --world ... --dilate 4             # shelf feather (chunks)
"""
import sys, json, re, argparse
from pathlib import Path
import numpy as np
import rasterio
from scipy.ndimage import binary_dilation

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from islands.render_islands import safe_name

ISL = ROOT / "islands"; MASKS_OUT = ISL / "masks_islands"; OUT = ISL / "out"
SEA = 17050; SECTOR = 4096


def read_region(p: Path) -> dict:
    """{(cx,cz)->raw sector bytes} for present chunks (cx,cz in 0..31)."""
    data = p.read_bytes()
    if len(data) < 8192:
        return {}
    loc = data[:4096]; ch = {}
    for i in range(1024):
        off = int.from_bytes(loc[i * 4:i * 4 + 3], "big"); n = loc[i * 4 + 3]
        if off and n:
            ch[(i % 32, i // 32)] = data[off * SECTOR:(off + n) * SECTOR]
    return ch


def write_region(p: Path, ch: dict) -> None:
    loc = bytearray(4096); tim = bytearray(4096); body = bytearray(); nxt = 2
    for (cx, cz), raw in ch.items():
        n = len(raw) // SECTOR
        idx = cz * 32 + cx
        loc[idx * 4:idx * 4 + 3] = nxt.to_bytes(3, "big"); loc[idx * 4 + 3] = n & 0xFF
        body += raw; nxt += n
    p.write_bytes(bytes(loc) + bytes(tim) + bytes(body))


def footprint_chunks(mdir: Path, dilate_chunks: int) -> np.ndarray:
    """Local chunk-grid (H/16 x W/16), True where land or within dilate_chunks of land."""
    with rasterio.open(mdir / "height.tif") as s:
        land = s.read(1) > SEA
    H, W = land.shape; gh, gw = H // 16, W // 16
    lc = land[:gh * 16, :gw * 16].reshape(gh, 16, gw, 16).any(axis=(1, 3))
    return binary_dilation(lc, iterations=int(dilate_chunks))


def crop_merge_install(dst_region: Path, names=None, dilate_chunks=4):
    lay = json.loads((ISL / "layout.json").read_text())["islands"]
    recs = []
    for e in lay:
        nm = safe_name(e["name"]); d = MASKS_OUT / nm; o = OUT / nm
        if names and not any(n in nm or n in e["dem_path"] for n in names):
            continue
        if not (d / "height.tif").exists() or not o.exists():
            continue
        man = json.loads((d / "manifest.json").read_text())
        recs.append((nm, d, o, man))
    recs.sort(key=lambda r: -(r[3]["world_hw"][0] * r[3]["world_hw"][1]))   # big islands win chunk conflicts

    merged: dict[str, dict] = {}
    for nm, d, odir, man in recs:
        ox = round(man["world_offset_px"][0] / 512) * 512
        oz = round(man["world_offset_px"][1] / 512) * 512
        ocx, ocz = ox // 16, oz // 16
        fp = footprint_chunks(d, dilate_chunks); gch, gcw = fp.shape
        kept = dropped = 0
        for mca in sorted(odir.glob("r.*.mca")):
            m = re.match(r"r\.(-?\d+)\.(-?\d+)\.mca", mca.name)
            if not m:
                continue
            RX, RZ = int(m.group(1)), int(m.group(2))
            reg = merged.setdefault(mca.name, {})
            for (cx, cz), raw in read_region(mca).items():
                lcx, lcz = RX * 32 + cx - ocx, RZ * 32 + cz - ocz     # world chunk -> local chunk
                if 0 <= lcz < gch and 0 <= lcx < gcw and fp[lcz, lcx]:
                    if (cx, cz) not in reg:                            # bigger island already won
                        reg[(cx, cz)] = raw; kept += 1
                else:
                    dropped += 1
        print(f"  {nm:38s} kept {kept:5d} shelf chunks, dropped {dropped:5d} far-ocean", flush=True)

    dst_region.mkdir(parents=True, exist_ok=True)
    n = 0
    for rname, ch in merged.items():
        if ch:
            write_region(dst_region / rname, ch); n += 1
    print(f"\ninstalled {n} cropped+merged region files -> {dst_region}")
    print("  dropped chunks regenerate as vandir:ocean -> islands hug their shelf, no tile overwrite")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--names", default="")
    ap.add_argument("--dilate", type=int, default=4, help="shelf feather in chunks (~16 blocks each)")
    a = ap.parse_args()
    world = Path(a.world); reg = world / "region"
    dp = world / "datapacks" / "vandir_height.zip"
    if not dp.exists():
        print(f"WARNING: {dp} missing — 48-section island chunks will OOB-crash on load. "
              f"Copy vandir_height.zip into {world / 'datapacks'} BEFORE first load.")
    names = [n.strip() for n in a.names.split(",") if n.strip()] or None
    crop_merge_install(reg, names=names, dilate_chunks=a.dilate)


if __name__ == "__main__":
    main()
