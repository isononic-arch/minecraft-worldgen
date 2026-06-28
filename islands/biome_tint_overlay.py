"""islands/biome_tint_overlay.py — post-render MC-biome tint pass for the tropical
(southern) islands. Rewrites ONLY the biome tag (grass/water tint) — block_states,
terrain, trees, lithology are untouched, so the studio biomes' varied vegetation
stays while the GRASS reads uniform jungle:

    land cell  -> minecraft:jungle       (uniform tropical grass over LUSH/decid/mangrove)
    ocean cell -> UNCHANGED (native minecraft:ocean)   # S95 #8: warm_ocean KILLED

land vs ocean is read from each cell's EXISTING biome. Ocean cells are left as-is so
the shelf shallows match the superflat far-ocean (both blue); only LAND is tinted.
Run AFTER render, BEFORE install crop.

    py islands/biome_tint_overlay.py            # all tint-set islands
    py islands/biome_tint_overlay.py <key|name>
"""
import sys, io, zlib, gzip, struct, json
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import nbtlib
from core.chunk_writer import _build_biomes_nbt
from islands.install_islands import read_region, write_region
from islands.render_islands import safe_name, _key

ISL = ROOT / "islands"; OUT = ISL / "out"
EXCLUDE = {"17_288", "49_722", "-50_393", "-17_622"}   # St Kitts, Fogo, Madre, Efate keep own tints
LAND_MC = "minecraft:jungle"
OCEAN_MC = "minecraft:warm_ocean"


def _unpack(longs, count, bpe):
    vpl = 64 // bpe; mask = (1 << bpe) - 1; out = []
    for L in longs:
        u = L & ((1 << 64) - 1)
        for j in range(vpl):
            out.append((u >> (j * bpe)) & mask)
            if len(out) >= count:
                return out
    return out


def _is_ocean(name: str) -> bool:
    return "ocean" in name or "river" in name


def _retint(biomes):
    pal = [str(x) for x in biomes["palette"]]
    if "data" not in biomes:
        cells = [0] * 64
    else:
        cells = _unpack([int(x) for x in biomes["data"]], 64, max(1, (len(pal) - 1).bit_length()))
    # S95 #8: KILL warm_ocean. Ocean cells KEEP their native biome (minecraft:ocean)
    # so the shelf shallows match the superflat far-ocean (both blue) — the old
    # warm_ocean rewrite put stone on beaches and didn't read right across coasts.
    # Only LAND is still tinted to jungle (the wanted tropical grass).
    names = [pal[i] if _is_ocean(pal[i]) else LAND_MC for i in cells]
    return _build_biomes_nbt(np.array(names, dtype=object).reshape(4, 4, 4))


def _decode(raw):
    ln = struct.unpack(">I", raw[:4])[0]; c = raw[4]; cdata = raw[5:4 + ln]
    data = zlib.decompress(cdata) if c == 2 else (gzip.decompress(cdata) if c == 1 else cdata)
    return nbtlib.File.parse(io.BytesIO(data))


def _encode(nbt):
    buf = io.BytesIO(); nbt.write(buf)
    comp = zlib.compress(buf.getvalue(), 6)
    e = struct.pack(">I", len(comp) + 1) + b"\x02" + comp
    return e + b"\x00" * ((4096 - len(e) % 4096) % 4096)


def tint_region(path: Path) -> int:
    ch = read_region(path); out = {}; n = 0
    for k, raw in ch.items():
        nbt = _decode(raw)
        secs = nbt.get("sections")
        if secs:
            for s in secs:
                if "biomes" in s:
                    s["biomes"] = _retint(s["biomes"])
            n += 1
        out[k] = _encode(nbt)
    write_region(path, out)
    return n


def tint_island(name):
    odir = OUT / name
    regions = sorted(odir.glob("r.*.mca"))
    tot = sum(tint_region(r) for r in regions)
    print(f"  {name:38s} tinted {tot} chunks across {len(regions)} regions", flush=True)


def main():
    sel = sys.argv[1] if len(sys.argv) > 1 else None
    lay = json.loads((ISL / "layout.json").read_text())["islands"]
    for e in lay:
        nm = safe_name(e["name"]); key = _key(e["dem_path"])
        if key in EXCLUDE:
            continue
        if sel and sel not in key and sel not in nm:
            continue
        if (OUT / nm).exists():
            tint_island(nm)


if __name__ == "__main__":
    main()
