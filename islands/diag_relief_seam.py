"""diag_relief_seam.py — measure surface_y (top solid Y) discontinuity across
shared tile (region) edges of a rendered island, to classify R3 rock_relief
seams as INTERIOR (land-land tile boundary) vs BORDER (island bbox edge).

Fully vectorized top-solid-Y per chunk. For each region boundary line
(x or z = multiple of 512) we compute |Y[edge]-Y[edge-1]| over land columns on
BOTH sides, and compare to a CONTROL line at +256 (mid-tile natural slope). A
relief AMPLITUDE desync -> systematic step ON the 512 line, small at control.

Usage: py islands/diag_relief_seam.py --name vincentia [--limit N]
"""
import sys, struct, zlib, gzip, io, json, argparse
from pathlib import Path
import numpy as np
import nbtlib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"


def _safe(n):
    import re
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


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


def section_solid(sec):
    """(16,16,16) bool 'solid' (not air/cave_air/void_air/water) indexed (y,z,x)."""
    bs = sec.get("block_states")
    if bs is None:
        return None
    pal = bs.get("palette")
    if pal is None:
        return None
    names = [str(p.get("Name", "minecraft:air")) for p in pal]
    nonsolid = np.array([n in ("minecraft:air", "minecraft:cave_air",
                               "minecraft:void_air", "minecraft:water")
                         for n in names], bool)
    if len(names) == 1:
        return np.full((16, 16, 16), not nonsolid[0])
    data = bs.get("data")
    if data is None:
        return np.full((16, 16, 16), not nonsolid[0])
    bits = max(4, (len(names) - 1).bit_length())
    per = 64 // bits
    mask = np.uint64((1 << bits) - 1)
    longs = np.asarray(data, dtype=np.uint64)
    shifts = (np.arange(per, dtype=np.uint64) * np.uint64(bits))
    idx = ((longs[:, None] >> shifts[None, :]) & mask).reshape(-1)[:4096].astype(np.int64)
    idx = np.clip(idx, 0, len(names) - 1)
    solid = ~nonsolid[idx]
    return solid.reshape(16, 16, 16)


def chunk_topY(chunk):
    """(16,16) top SOLID Y indexed [z,x] (-999 if none). Vectorized."""
    secs = chunk.get("sections") or chunk.get("Sections") or []
    items = []
    for s in secs:
        sy = int(s.get("Y", 0)); sb = section_solid(s)
        if sb is not None:
            items.append((sy, sb))
    topY = np.full((16, 16), -999, dtype=np.int32)
    found = np.zeros((16, 16), bool)
    for sy, sb in sorted(items, key=lambda t: t[0], reverse=True):
        # any solid in this section, highest yy first
        for yy in range(15, -1, -1):
            layer = sb[yy]                       # (z,x) bool
            fill = (~found) & layer
            if fill.any():
                topY[fill] = sy * 16 + yy
                found |= fill
        if found.all():
            break
    return topY


def build_map(odir, limit=None):
    files = sorted(odir.glob("r.*.mca"))
    if limit:
        files = files[:limit]
    rxs = []; rzs = []
    for m in files:
        rx, rz = map(int, m.stem.split(".")[1:3]); rxs.append(rx); rzs.append(rz)
    rx0, rx1 = min(rxs), max(rxs); rz0, rz1 = min(rzs), max(rzs)
    bx = rx0 * 512; bz = rz0 * 512
    W = (rx1 - rx0 + 1) * 512; H = (rz1 - rz0 + 1) * 512
    Y = np.full((H, W), -999, dtype=np.int32)
    for i, m in enumerate(files):
        rx, rz = map(int, m.stem.split(".")[1:3])
        with open(m, "rb") as f:
            for lz in range(32):
                for lx in range(32):
                    try:
                        ch = read_chunk(f, lx, lz)
                    except Exception:
                        ch = None
                    if ch is None:
                        continue
                    tY = chunk_topY(ch)
                    cwx = (rx * 32 + lx) * 16 - bx
                    cwz = (rz * 32 + lz) * 16 - bz
                    if 0 <= cwx < W and 0 <= cwz < H:
                        Y[cwz:cwz+16, cwx:cwx+16] = tY
        print(f"  ..{i+1}/{len(files)} {m.name}", flush=True)
    return Y, bx, bz


def seam_steps(Y, axis, line_local):
    if axis == 1:
        if line_local <= 0 or line_local >= Y.shape[1]:
            return None
        a = Y[:, line_local-1]; c = Y[:, line_local]
    else:
        if line_local <= 0 or line_local >= Y.shape[0]:
            return None
        a = Y[line_local-1, :]; c = Y[line_local, :]
    valid = (a > 63) & (c > 63)
    if valid.sum() < 8:
        return None
    step = np.abs(c[valid].astype(np.int32) - a[valid].astype(np.int32))
    sgn = (c[valid].astype(np.int32) - a[valid].astype(np.int32))
    return dict(n=int(valid.sum()), mean=float(step.mean()),
                p95=float(np.percentile(step, 95)), mx=int(step.max()),
                bias=float(sgn.mean()), ge3=int((step >= 3).sum()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    layout = json.loads((ISL / "layout.json").read_text())
    entry = next(i for i in layout["islands"] if a.name in _safe(i["name"]) or a.name in i["dem_path"])
    name = _safe(entry["name"])
    odir = ISL / "out" / name
    man = json.loads((ISL / "masks_islands" / name / "manifest.json").read_text())
    ox, oz = man["world_offset_px"]
    sox = round(ox / 512) * 512; soz = round(oz / 512) * 512
    print(f"island={name} offset=({ox},{oz}) snapped=({sox},{soz})", flush=True)
    print("building global top-Y map...", flush=True)
    Y, bx, bz = build_map(odir, limit=a.limit)
    print(f"map {Y.shape}  worldX[{bx},{bx+Y.shape[1]})  worldZ[{bz},{bz+Y.shape[0]})", flush=True)
    print(f"land cols: {int((Y>63).sum())}", flush=True)

    print("\n== X (vertical) seams: SEAM(512-line) vs CTRL(+256) ==", flush=True)
    for xw in range((bx//512+1)*512, bx+Y.shape[1], 512):
        s = seam_steps(Y, 1, xw-bx)
        c = seam_steps(Y, 1, xw-bx+256)
        if s is None:
            continue
        ltx = (xw - sox)//512
        cm = (f"CTRL mean={c['mean']:.2f} p95={c['p95']:.0f} ge3={c['ge3']}" if c else "CTRL=NA")
        print(f" x={xw} lt={ltx:2d}  SEAM n={s['n']:4d} mean={s['mean']:.2f} "
              f"p95={s['p95']:.0f} mx={s['mx']:3d} bias={s['bias']:+.2f} ge3={s['ge3']:4d}   {cm}",
              flush=True)

    print("\n== Z (horizontal) seams ==", flush=True)
    for zw in range((bz//512+1)*512, bz+Y.shape[0], 512):
        s = seam_steps(Y, 0, zw-bz)
        c = seam_steps(Y, 0, zw-bz+256)
        if s is None:
            continue
        lty = (zw - soz)//512
        cm = (f"CTRL mean={c['mean']:.2f} p95={c['p95']:.0f} ge3={c['ge3']}" if c else "CTRL=NA")
        print(f" z={zw} lt={lty:2d}  SEAM n={s['n']:4d} mean={s['mean']:.2f} "
              f"p95={s['p95']:.0f} mx={s['mx']:3d} bias={s['bias']:+.2f} ge3={s['ge3']:4d}   {cm}",
              flush=True)


if __name__ == "__main__":
    main()
