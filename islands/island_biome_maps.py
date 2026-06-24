"""islands/island_biome_maps.py — quick per-island BIOME preview maps with a legend.

Reuses the REAL override pipeline (render_islands.paint_override + the raster overlay
+ Irish bog) so the preview matches what the bake will produce, applying the current
BANDS, the Madre de Dios east-cut, and the mainland-land overwrite removal. Built from
the already-baked height/slope/flow (no re-derive) -> fast. Biome zonation is fraction-
based so it's correct regardless of the (changed) spline scale.

    py islands/island_biome_maps.py            # all islands -> islands/out/biome_<name>.jpg
    py islands/island_biome_maps.py <key>      # one island
"""
import sys, json
from pathlib import Path
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from derive_masks_from_height import _GIN, _YOUT, SEA_LEVEL_RAW16 as SEA
from islands.render_islands import (BANDS, DEFAULT_BAND, _key, safe_name, paint_override,
                                    MASKS_OUT, REGISTRY)
from core.biome_assignment import OVERRIDE_BIOME_MAP
from tools.world_biome_map import BIOME_COLORS

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

ISL = ROOT / "islands"
MAPLONG = 1500

_LUT = np.zeros((256, 3), np.uint8)
for _z, _n in OVERRIDE_BIOME_MAP.items():
    _LUT[_z] = BIOME_COLORS.get(_n, (150, 150, 150))
_LUT[0] = (18, 28, 46)        # ocean — set LAST (zone 0 "none" has no BIOME_COLORS entry)


def _read(path, mh, mw, resamp=Resampling.nearest):
    with rasterio.open(path) as s:
        return s.read(1, out_shape=(mh, mw), resampling=resamp)


def _mainland_overlap(land, ox, oz, mh, mw, W, H):
    """Boolean map (map-res) of island cells sitting over mainland land."""
    mp = ROOT / "masks" / "height.tif"
    out = np.zeros((mh, mw), bool)
    if not mp.exists():
        return out
    with rasterio.open(mp) as s:
        MW, MH = s.width, s.height
        wx0, wz0 = max(ox, 0), max(oz, 0)
        wx1, wz1 = min(ox + W, MW), min(oz + H, MH)
        if wx1 <= wx0 or wz1 <= wz0:
            return out
        # map-res sub-rect of the island that overlaps mainland
        ca, cb = int((wx0 - ox) / W * mw), int((wx1 - ox) / W * mw)
        ra, rb = int((wz0 - oz) / H * mh), int((wz1 - oz) / H * mh)
        cb, rb = max(cb, ca + 1), max(rb, ra + 1)
        msub = s.read(1, window=Window(wx0, wz0, wx1 - wx0, wz1 - wz0),
                      out_shape=(rb - ra, cb - ca), resampling=Resampling.nearest)
    out[ra:rb, ca:cb] = msub > SEA
    return out


def build_override(rec):
    d = rec["dir"]; bands = BANDS.get(rec["key"], DEFAULT_BAND)
    W, H = rec["W"], rec["H"]
    scale = max(W, H) / MAPLONG
    mw, mh = max(1, int(W / scale)), max(1, int(H / scale))
    mcy = np.interp(_read(d / "height.tif", mh, mw), _GIN, _YOUT)
    land = mcy > 63.4
    # Madre de Dios east-cut
    cut = bands.get("frag_cut_east")
    if cut is not None:
        land[:, int(cut * mw):] = False
    # remove island land over mainland land
    ox = round(rec["off"][0] / 512) * 512; oz = round(rec["off"][1] / 512) * 512
    land &= ~_mainland_overlap(land, ox, oz, mh, mw, W, H)
    if int(land.sum()) == 0:
        return np.zeros((mh, mw), np.uint8)
    ov = paint_override(mcy, land, bands, bands["wind"])
    # real land-cover raster (St Kitts) overlay
    try:
        from islands.import_island_raster import load_override_from_geo
        rov = load_override_from_geo(rec["name"], ov.shape)
        if rov is not None:
            rov[~land] = 0
            ov = np.where(rov > 0, rov, ov)
    except Exception:
        pass
    # painted override touch-ups
    pp = d / "override_painted.tif"
    if pp.exists():
        pov = _read(pp, mh, mw)
        ov = np.where(pov > 0, pov, ov)
    # Irish bog (terrain-derived FRESHWATER_FEN)
    reg = REGISTRY.get(Path(rec["dem"]).name, {})
    bf = float(reg.get("bog_frac", 0.15))
    if bf > 0 and (d / "slope.tif").exists() and (d / "flow.tif").exists():
        deg = _read(d / "slope.tif", mh, mw).astype(np.float32) / 65535 * 45
        flow = _read(d / "flow.tif", mh, mw).astype(np.float32) / 65535
        flat = land & (deg < 7.0); ntar = int(bf * int(land.sum()))
        if int(flat.sum()) > ntar > 0:
            thr = np.partition(flow[flat], -ntar)[-ntar]
            ov[flat & (flow >= thr)] = 240
        else:
            ov[flat] = 240
    ov[~land] = 0
    return ov


def make_map(rec):
    ov = build_override(rec)
    rgb = _LUT[ov]
    H, W = ov.shape
    present = [z for z in sorted(set(np.unique(ov).tolist())) if z != 0]
    bands = BANDS.get(rec["key"], DEFAULT_BAND)
    desc = f"litho={bands.get('litho','temperate_basaltic')}  wind={bands.get('wind')}"
    fig, ax = plt.subplots(figsize=(W / 130 + 3.4, max(H / 130, 3)), dpi=130, facecolor="#0d1b2a")
    ax.imshow(rgb); ax.axis("off")
    ax.set_title(f"{rec['name']}   [{rec['key']}]\n{desc}", color="#dddddd", fontsize=9)
    handles = [Patch(facecolor=np.array(_LUT[z]) / 255, edgecolor="#444",
                     label=OVERRIDE_BIOME_MAP.get(z, str(z))) for z in present]
    if handles:
        ax.legend(handles=handles, loc="center left", bbox_to_anchor=(1.005, 0.5),
                  fontsize=8, frameon=False, labelcolor="#cccccc")
    out = ISL / "out" / f"biome_{rec['key']}_{rec['name']}.jpg"
    fig.savefig(out, bbox_inches="tight", facecolor="#0d1b2a", pil_kwargs={"quality": 84})
    plt.close(fig)
    return out


def records(sel=None):
    lay = json.loads((ISL / "layout.json").read_text())["islands"]
    recs = []
    for e in lay:
        d = MASKS_OUT / safe_name(e["name"])
        if not (d / "height.tif").exists() or not (d / "manifest.json").exists():
            continue
        k = _key(e["dem_path"])
        if sel and sel not in k and sel not in safe_name(e["name"]):
            continue
        hw = json.loads((d / "manifest.json").read_text())["world_hw"]
        recs.append(dict(name=safe_name(e["name"]), key=k, dir=d, dem=e["dem_path"],
                         off=e["world_offset_px"], H=hw[0], W=hw[1]))
    return recs


def main():
    sel = sys.argv[1] if len(sys.argv) > 1 else None
    for rec in records(sel):
        out = make_map(rec)
        print(f"  wrote {out}", flush=True)


if __name__ == "__main__":
    main()
