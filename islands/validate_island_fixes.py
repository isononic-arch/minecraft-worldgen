"""
validate_island_fixes.py — S104 validation harness for the two island-bake realism
fixes (beach redo + flat-island spline variation). ISLAND-ONLY, read-only except for
PNGs written to islands/_val/. Does NOT bake or render — it recomputes the beach mask
in-process from a baked island's height/slope and measures relief through the spline.

Usage:
  py islands/validate_island_fixes.py --beach              # before/after beach PNGs
  py islands/validate_island_fixes.py --relief             # spline relief table (all 15)
  py islands/validate_island_fixes.py --hillshade <name>   # hillshade PNG from baked height
  py islands/validate_island_fixes.py --all                # beach previews + relief table

The beach preview recomputes BOTH the OLD algorithm (core+dithered apron) and the
NEW slope-driven strip from the SAME baked height/slope, side by side, so the visual
diff is apples-to-apples (no re-bake needed).
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"
MASKS_OUT = ISL / "masks_islands"
VAL = ISL / "_val"
VAL.mkdir(exist_ok=True)

from islands.render_islands import (safe_name, raw_to_mcy, synth_shore_beach_wide,
                                    _near_ocean_disk, BANDS, _key, VEX, REGISTRY,
                                    _resolve_reg)
from derive_masks_from_height import SEA_LEVEL_RAW16 as SEA_RAW


def _read_tif(path):
    import rasterio
    with rasterio.open(str(path)) as d:
        return d.read(1)


def _safe_from_key(key):
    """map a BANDS key -> the baked safe_name dir via layout.json."""
    layout = json.loads((ISL / "layout.json").read_text())
    for i in layout["islands"]:
        if _key(i["dem_path"]) == key:
            return safe_name(i["name"]), i
    return None, None


# ── OLD beach algorithm (verbatim S95-T2 recipe) for the before/after diff ──────
def _old_beach(land, mcy, slope_u16, ox, oz,
               beach_core_width=9.0, beach_dither_mult=4.0, gentle_slope_deg=14.0):
    from scipy.ndimage import distance_transform_edt, gaussian_filter
    H, W = land.shape
    beach_land = mcy > 63.4
    ocean = ~beach_land
    if not ocean.any() or not beach_land.any():
        return np.zeros((H, W), np.uint8)
    dist = distance_transform_edt(~ocean).astype(np.float32)
    deg = slope_u16.astype(np.float32) / 65535.0 * 45.0
    gentle = deg < float(gentle_slope_deg)
    wx = (np.int64(ox) + np.arange(W, dtype=np.int64))[None, :].astype(np.uint64)
    wz = (np.int64(oz) + np.arange(H, dtype=np.int64))[:, None].astype(np.uint64)

    def _h(salt):
        s64 = np.uint64((salt * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF)
        h = (wx * np.uint64(0x9E3779B97F4A7C15) + wz * np.uint64(0xBF58476D1CE4E5B9) + s64)
        h = (h ^ (h >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
        h = (h ^ (h >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
        h = h ^ (h >> np.uint64(31))
        return (h.astype(np.float64) / np.float64(np.iinfo(np.uint64).max)).astype(np.float32)

    S = 12.0
    wn = gaussian_filter(_h(0xBEAC4), sigma=S)
    std = np.float32((1.0 / np.sqrt(12.0)) / (2.0 * np.sqrt(np.pi) * S))
    wnn = np.clip((wn - 0.5) / (2.5 * std), -1, 1).astype(np.float32)
    amp = np.float32(beach_core_width * 0.4)
    cw = np.maximum(np.float32(beach_core_width) + amp * wnn, 0.0)
    dw = cw * np.float32(beach_dither_mult)
    tw = cw + dw
    core = beach_land & (dist >= 1.0) & (dist <= cw)
    ind = beach_land & gentle & (dist > cw) & (dist <= tw)
    t = np.clip((dist - cw) / np.maximum(dw, 0.5), 0, 1)
    pp = np.clip(1.0 - t, 0.15, 0.85)
    dc = gaussian_filter(_h(0xD17BEA), sigma=1.0)
    stdc = np.float32((1.0 / np.sqrt(12.0)) / (2.0 * np.sqrt(np.pi) * 1.0))
    coin = np.clip(0.5 + (dc - 0.5) / (2.5 * stdc) * 0.5, 0, 1)
    dith = ind & (coin < pp)
    return ((core | dith).astype(np.uint8)) * 255


BEACH_PREVIEW = ["bahamas", "new_vincentia", "efate"]   # flat / steep / mixed


def beach_previews():
    from PIL import Image
    layout = json.loads((ISL / "layout.json").read_text())
    for want in BEACH_PREVIEW:
        # find baked dir
        match = None
        for i in layout["islands"]:
            sn = safe_name(i["name"])
            if want in sn:
                match = (sn, i); break
        if not match:
            print(f"[beach] {want}: no layout match"); continue
        sn, entry = match
        od = MASKS_OUT / sn
        if not (od / "height.tif").exists():
            print(f"[beach] {sn}: not baked, skip"); continue
        H16 = _read_tif(od / "height.tif")
        S16 = _read_tif(od / "slope.tif")
        ox = round(entry["world_offset_px"][0] / 512) * 512
        oz = round(entry["world_offset_px"][1] / 512) * 512
        # crop to the land bbox + margin so both beach algos run on a small window
        # (memory-lean; the full 7.8k^2 EDT OOMs a loaded box). Adjust ox/oz by the
        # crop so the world-coord hash stays seam-consistent within the window.
        _ld_full = H16 > 17090
        if _ld_full.any():
            rs, cs = np.where(_ld_full)
            MARGIN = 80
            r0, r1 = max(0, rs.min() - MARGIN), min(H16.shape[0], rs.max() + MARGIN)
            c0, c1 = max(0, cs.min() - MARGIN), min(H16.shape[1], cs.max() + MARGIN)
            H16 = H16[r0:r1, c0:c1]; S16 = S16[r0:r1, c0:c1]
            ox += c0; oz += r0
        del _ld_full
        mcy = raw_to_mcy(H16)
        land = mcy > 63.4
        old = _old_beach(land, mcy, S16, ox, oz)
        _, new = synth_shore_beach_wide(land, mcy, S16, world_offset_px=(ox, oz),
                                        beach_max_width=9.0, flat_slope_deg=6.0,
                                        steep_slope_deg=20.0, width_jitter=0.20)
        # downsample to keep the PNG manageable (nearest, ~1200px wide)
        step = max(1, land.shape[1] // 1400)
        ld = land[::step, ::step]; od_b = old[::step, ::step]; nw_b = new[::step, ::step]
        h, w = ld.shape
        panel = np.zeros((h, w * 2 + 8, 3), np.uint8)

        def _render(msk_beach):
            img = np.zeros((h, w, 3), np.uint8)
            img[~ld] = (40, 70, 120)            # ocean blue
            img[ld] = (70, 120, 60)             # land green
            img[msk_beach > 0] = (235, 220, 150)  # sand
            return img
        panel[:, :w] = _render(od_b)
        panel[:, w + 8:] = _render(nw_b)
        p = VAL / f"beach_redo_{want}.png"
        Image.fromarray(panel).save(p)
        # stats
        def _pct(m): return 100.0 * int((m > 0).sum()) / max(int(land.sum()), 1)
        print(f"[beach] {sn}: OLD sand {_pct(old):.2f}% of land | NEW sand {_pct(new):.2f}% "
              f"| LEFT=old RIGHT=new -> {p}")


def _spline_for(key):
    ov = json.loads((ISL / "spline_overrides.json").read_text()).get(key)
    if ov and ov.get("fracs") and ov.get("mcys"):
        return ov["fracs"], ov["mcys"]
    return None


def relief_table():
    """Measure the rendered-relief (max-min MC-Y over LAND) each island's spline
    produces, by mapping its baked height.tif raw values through the spline (or the
    baked height directly since the spline is already baked in). Reports max-min of
    the CURRENT baked height over land cells (raw>17090)."""
    layout = json.loads((ISL / "layout.json").read_text())
    rows = []
    for i in layout["islands"]:
        sn = safe_name(i["name"]); key = _key(i["dem_path"])
        od = MASKS_OUT / sn
        if not (od / "height.tif").exists():
            continue
        H16 = _read_tif(od / "height.tif")
        land = H16 > 17090
        if not land.any():
            rows.append((sn, key, 0.0)); continue
        mcy = raw_to_mcy(H16[land])
        rows.append((sn, key, float(mcy.max() - mcy.min())))
    rows.sort(key=lambda r: r[2])
    print(f"{'island':40} {'key':10} relief(blocks)")
    for sn, key, r in rows:
        print(f"{sn:40} {key:10} {r:7.1f}")


def hillshade(name):
    from PIL import Image
    layout = json.loads((ISL / "layout.json").read_text())
    match = None
    for i in layout["islands"]:
        if name in safe_name(i["name"]):
            match = (safe_name(i["name"]), i); break
    if not match:
        print(f"[hillshade] no match for {name}"); return
    sn, entry = match
    od = MASKS_OUT / sn
    if not (od / "height.tif").exists():
        print(f"[hillshade] {sn} not baked"); return
    H16 = _read_tif(od / "height.tif")
    mcy = raw_to_mcy(H16).astype(np.float32)
    land = H16 > 17090
    step = max(1, mcy.shape[1] // 1400)
    m = mcy[::step, ::step]; ld = land[::step, ::step]
    gz, gx = np.gradient(m)
    slope = np.arctan(np.hypot(gx, gz))
    aspect = np.arctan2(-gx, gz)
    az, alt = np.deg2rad(315.0), np.deg2rad(45.0)
    hs = (np.sin(alt) * np.cos(slope) +
          np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
    hs = np.clip(hs, 0, 1)
    img = np.zeros((*m.shape, 3), np.uint8)
    img[~ld] = (40, 70, 120)
    sh = (hs * 255).astype(np.uint8)
    img[ld] = np.stack([sh, sh, sh], -1)[ld]
    # tint land by elevation for readability
    if ld.any():
        elev = np.clip((m - 63.0) / max(float(m[ld].max()) - 63.0, 1.0), 0, 1)
        tint = (np.stack([80 + 120 * elev, 130 + 60 * (1 - elev), 70 + 30 * elev], -1)).astype(np.uint8)
        blended = (0.55 * img.astype(np.float32) + 0.45 * tint.astype(np.float32)).astype(np.uint8)
        img[ld] = blended[ld]
    p = VAL / f"spline_{name}.png"
    Image.fromarray(img).save(p)
    reln = mcy[land]
    print(f"[hillshade] {sn}: relief {float(reln.max()-reln.min()):.1f} blocks -> {p}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--beach", action="store_true")
    ap.add_argument("--relief", action="store_true")
    ap.add_argument("--hillshade")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.all or a.beach:
        beach_previews()
    if a.all or a.relief:
        relief_table()
    if a.hillshade:
        hillshade(a.hillshade)


if __name__ == "__main__":
    main()
