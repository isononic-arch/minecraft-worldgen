"""S94: compute per-tile coverage stats at 1:8 and greedy-select a minimal
validation tile set covering all biomes + rivers/lakes/coast/relief/cliffs.

Usage: py tools/diag_s94_tileset.py
"""
import json
import numpy as np
import rasterio
from rasterio.enums import Resampling

MASKS = "masks"
SCALE_PX = 64          # 512 blocks / 8
N_TILES = 97
SEA_RAW = 17050

ZONES = {
    10: "COASTAL_HEATH", 20: "TEMPERATE_RAINFOREST", 30: "BOREAL_TAIGA",
    35: "SNOWY_BOREAL_TAIGA", 40: "BOREAL_ALPINE", 50: "ARCTIC_TUNDRA",
    55: "FROZEN_FLATS", 60: "TEMPERATE_DECIDUOUS", 70: "RAINFOREST_COAST",
    80: "RIPARIAN_WOODLAND", 90: "DRY_OAK_SAVANNA", 100: "KARST_BARRENS",
    110: "BIRCH_FOREST", 115: "EASTERN_TEMPERATE_COAST", 120: "MIXED_FOREST",
    130: "CONTINENTAL_STEPPE", 140: "DRY_PINE_BARRENS", 150: "SCRUBBY_HEATHLAND",
    160: "LUSH_RAINFOREST_COAST", 170: "SAND_DUNE_DESERT",
    190: "DESERT_STEPPE_TRANSITION", 200: "SEMI_ARID_SHRUBLAND",
    210: "DRY_WOODLAND_MAQUIS", 220: "TIDAL_JUNGLE_FRINGE",
    230: "MANGROVE_COAST", 240: "FRESHWATER_FEN",
}

# Pinned tiles: stale-water sweep + controls + seam pairs + high-risk history
PINNED = {
    (79, 71): "stale-sweep / steep 295-blk descent / rock+river",
    (27, 33): "stale-sweep / river seam pair N",
    (27, 34): "stale-sweep / estuary control + seam pair S",
    (28, 34): "razor-seam pair (biome dither seam, user report spot)",
    (62, 61): "stale-sweep / lake junction + stream depth",
    (19, 76): "stale-sweep / river banks",
    (48, 48): "control: ocean/coast/seams validator default",
    (51, 53): "control: floodplain/lakes/rivers/MIXED_FOREST baseline",
    (30, 12): "extreme headwater Y389-520 / snow-over-rock / S93 money tile",
    (29, 12): "high-relief seam pair w/ (30,12) + temperate_basaltic rock ref",
    (13, 82): "high-risk: S87 missing-river-chunk history (RFC)",
    (50, 48): "high-risk: S87 blank-tile history (MIXED_FOREST)",
}

MIN_BIOME_PX = 800     # ~20% of a 64x64 tile = walkable chunk at 50k


def read8(name, resampling=Resampling.nearest):
    with rasterio.open(f"{MASKS}/{name}") as src:
        return src.read(1, out_shape=(6250, 6250), resampling=resampling)


def main():
    ov = read8("override.tif")
    h = read8("height.tif")
    cl = read8("hydro_centerline.tif")
    lk = read8("hydro_lake.tif")
    try:
        sl = read8("slope.tif")
    except Exception:
        sl = None

    stats = {}
    for tx in range(N_TILES):
        for tz in range(N_TILES):
            x0, x1 = tx * SCALE_PX, (tx + 1) * SCALE_PX
            z0, z1 = tz * SCALE_PX, (tz + 1) * SCALE_PX
            o = ov[z0:z1, x0:x1]
            hh = h[z0:z1, x0:x1]
            biomes = {}
            for code, name in ZONES.items():
                n = int((o == code).sum())
                if n >= MIN_BIOME_PX:
                    biomes[name] = n
            ocean = int((hh < SEA_RAW).sum())
            land = o.size - ocean
            st = {
                "biomes": biomes,
                "river_px": int((cl[z0:z1, x0:x1] > 0).sum()),
                "lake_px": int((lk[z0:z1, x0:x1] > 0).sum()),
                "ocean_px": ocean,
                "coast": 400 < ocean < (o.size - 400),
                "relief_raw": int(hh.max()) - int(hh.min()),
            }
            if sl is not None:
                st["slope_p99"] = float(np.percentile(sl[z0:z1, x0:x1], 99))
            stats[(tx, tz)] = st

    # ---- requirement tracking -------------------------------------------
    need_biomes = set(ZONES.values())
    covered_biomes = set()
    flags = {"river": False, "lake": False, "coast": False,
             "high_relief": False, "cliffs": False}

    # relief threshold: raw range translating to ~150+ MC blocks; use p95 of
    # all-tile relief as the "high relief" bar
    reliefs = sorted(s["relief_raw"] for s in stats.values())
    relief_bar = reliefs[int(len(reliefs) * 0.95)]
    slope_vals = sorted(s.get("slope_p99", 0) for s in stats.values())
    slope_bar = slope_vals[int(len(slope_vals) * 0.97)]

    def absorb(t):
        s = stats[t]
        covered_biomes.update(s["biomes"].keys())
        if s["river_px"] > 50: flags["river"] = True
        if s["lake_px"] > 200: flags["lake"] = True
        if s["coast"]: flags["coast"] = True
        if s["relief_raw"] >= relief_bar: flags["high_relief"] = True
        if s.get("slope_p99", 0) >= slope_bar: flags["cliffs"] = True

    chosen = list(PINNED.keys())
    for t in chosen:
        absorb(t)

    print(f"=== PINNED ({len(chosen)}) cover {len(covered_biomes)}/26 biomes; flags={flags}")
    print(f"    relief_bar(raw)={relief_bar} slope_bar={slope_bar:.4f}")
    for t in chosen:
        s = stats[t]
        print(f"  {t}: biomes={sorted(s['biomes'])} river={s['river_px']} "
              f"lake={s['lake_px']} ocean={s['ocean_px']} relief={s['relief_raw']} "
              f"slope_p99={s.get('slope_p99', 0):.3f}")

    # ---- greedy cover of remaining biomes --------------------------------
    remaining = need_biomes - covered_biomes
    print(f"\n=== remaining biomes ({len(remaining)}): {sorted(remaining)}")
    candidates = list(stats.keys())
    while remaining:
        best, best_gain, best_bonus = None, -1, -1
        for t in candidates:
            if t in chosen:
                continue
            s = stats[t]
            gain = len(remaining & set(s["biomes"]))
            if gain == 0:
                continue
            # tiebreak: prefer tiles that also add features + larger purity
            bonus = (s["river_px"] > 50) + (s["lake_px"] > 200) + s["coast"]
            bonus += sum(s["biomes"][b] for b in remaining & set(s["biomes"])) / 4096.0
            if gain > best_gain or (gain == best_gain and bonus > best_bonus):
                best, best_gain, best_bonus = t, gain, bonus
        if best is None:
            print(f"!!! uncoverable biomes at MIN_BIOME_PX={MIN_BIOME_PX}: {sorted(remaining)}")
            break
        chosen.append(best)
        newly = remaining & set(stats[best]["biomes"])
        remaining -= newly
        s = stats[best]
        print(f"  + {best}: adds {sorted(newly)} (river={s['river_px']} lake={s['lake_px']} "
              f"coast={s['coast']} relief={s['relief_raw']})")
        absorb(best)

    print(f"\n=== FINAL SET: {len(chosen)} tiles, flags={flags}")
    print(" ".join(f"{x},{z}" for x, z in chosen))
    out = {f"{x},{z}": {**stats[(x, z)],
                        "pinned": PINNED.get((x, z), "")} for x, z in chosen}
    with open("diag_s94_tileset.json", "w") as f:
        json.dump(out, f, indent=1)
    print("wrote diag_s94_tileset.json")


if __name__ == "__main__":
    main()
