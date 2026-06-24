"""map_stkitts_nevis.py — zoomed map of St Kitts + Nevis as RENDERED IN-GAME.
Two aligned panels over the same world crop:
  (1) true in-game surface  — topmost block per column read from the written .mca
  (2) biome + mask classification — override zones + rock/beach/snow/shore (land-masked)
Saves islands/out/stkitts_nevis_ingame.png.
"""
import sys, json, time
from pathlib import Path
import numpy as np
import rasterio
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from scipy.ndimage import label

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.biome_assignment import OVERRIDE_BIOME_MAP
from tools.world_biome_map import BIOME_COLORS
from core.preview_renderer import BLOCK_COLORS
from derive_masks_from_height import _GIN, _YOUT
from islands.topdown_mca import read_chunk, top_block_for_chunk

NAME = "new_vincentia_st_kitts_nevis_statia"
MD = ROOT / "islands" / "masks_islands" / NAME
OD = ROOT / "islands" / "out" / NAME
OUT = ROOT / "islands" / "out" / "stkitts_nevis_ingame.png"
OCEAN = (28, 52, 96)
STEP = 2     # block sampling for the in-game render


def main():
    from rasterio.windows import Window
    man = json.loads((MD / "manifest.json").read_text())
    ox, oz = man["world_offset_px"]
    sx, sz = round(ox / 512) * 512, round(oz / 512) * 512

    # label override ALONE (memory-safe on 1.8GB free), find St Kitts + Nevis bbox, then free.
    ov_full = rasterio.open(str(MD / "override.tif")).read(1)
    land_full = ov_full > 0
    lab, n = label(land_full)
    sizes = np.bincount(lab.ravel()); sizes[0] = 0
    big = np.argsort(sizes)[::-1][:2]            # St Kitts (biggest) + Nevis
    sel = np.isin(lab, big)
    ys, xs = np.where(sel)
    M = 80
    Hf, Wf = ov_full.shape
    ly0, ly1 = max(0, ys.min() - M), min(Hf, ys.max() + M)
    lx0, lx1 = max(0, xs.min() - M), min(Wf, xs.max() + M)
    wx0, wx1 = sx + lx0, sx + lx1
    wz0, wz1 = sz + ly0, sz + ly1
    print(f"crop world X[{wx0},{wx1}) Z[{wz0},{wz1})  ({wx1-wx0}x{wz1-wz0} blocks)", flush=True)
    del ov_full, land_full, lab, sel

    # ---------- panel 2: biome + mask (windowed read of crop only) ----------
    win = Window(lx0, ly0, lx1 - lx0, ly1 - ly0)
    def rdw(n):
        return rasterio.open(str(MD / n)).read(1, window=win)
    ov_c = rdw("override.tif"); rock_c = rdw("rock_gap.tif"); snow_c = rdw("snow_gap.tif")
    beach_c = rdw("beach.tif"); shore_c = rdw("shore.tif"); hgt_c = rdw("height.tif")
    land_c = ov_c > 0
    Hc, Wc = ov_c.shape
    mcy = np.interp(hgt_c.astype(np.float64), _GIN, _YOUT)
    gz, gx = np.gradient(mcy.astype(np.float32))
    slp = np.pi / 2 - np.arctan(np.hypot(gx, gz) * 0.6)
    asp = np.arctan2(-gz, gx)
    az, alt = np.deg2rad(315.0), np.deg2rad(45.0)
    hs = np.clip((np.sin(alt) * np.sin(slp) + np.cos(alt) * np.cos(slp) * np.cos(az - asp) + 1) / 2, 0, 1)

    zone_rgb = {0: OCEAN}; present = {}
    for z in np.unique(ov_c):
        if z == 0:
            continue
        nm = OVERRIDE_BIOME_MAP.get(int(z), f"zone{z}")
        zone_rgb[int(z)] = tuple(BIOME_COLORS.get(nm, (150, 150, 150)))
        present[int(z)] = (nm, int((ov_c == z).sum()))
    bio = np.zeros((Hc, Wc, 3), np.float32)
    for z, c in zone_rgb.items():
        bio[ov_c == z] = c
    l3 = land_c[..., None]
    bio = np.where(l3, np.clip(bio * (0.45 + 0.55 * hs)[..., None], 0, 255), bio).astype(np.uint8)

    FEAT = {"water": OCEAN, "land": (118, 134, 92), "shore": (150, 160, 110),
            "beach": (236, 214, 146), "rock": (140, 136, 130), "snow": (242, 246, 250)}
    feat = np.zeros((Hc, Wc, 3), np.uint8); feat[:] = FEAT["water"]
    feat[land_c] = FEAT["land"]
    feat[land_c & (shore_c > 0)] = FEAT["shore"]
    feat[land_c & (beach_c > 60)] = FEAT["beach"]
    feat[land_c & (rock_c > 0)] = FEAT["rock"]          # LAND-MASKED (map is correct even though bake fix is deferred)
    feat[land_c & (snow_c > 0)] = FEAT["snow"]
    feat = np.clip(feat.astype(np.float32) * np.where(l3, (0.5 + 0.5 * hs)[..., None], 1.0), 0, 255).astype(np.uint8)
    land_n = int(land_c.sum())
    fcnt = {"rock": int((land_c & (rock_c > 0)).sum()), "beach": int((land_c & (beach_c > 60)).sum()),
            "snow": int((land_c & (snow_c > 0)).sum()), "shore": int((land_c & (shore_c > 0)).sum())}

    # ---------- panel 1: true in-game surface from .mca ----------
    iw = (wx1 - wx0 + STEP - 1) // STEP
    ih = (wz1 - wz0 + STEP - 1) // STEP
    game = np.zeros((ih, iw, 3), np.uint8); game[:] = OCEAN
    DEF = (110, 110, 110)
    rx_lo, rx_hi = wx0 // 512, (wx1 - 1) // 512
    rz_lo, rz_hi = wz0 // 512, (wz1 - 1) // 512
    files = []
    for mca in sorted(OD.glob("r.*.mca")):
        rx, rz = map(int, mca.stem.split(".")[1:3])
        if rx_lo <= rx <= rx_hi and rz_lo <= rz <= rz_hi:
            files.append((rx, rz, mca))
    print(f"reading {len(files)} region files...", flush=True)
    t0 = time.time(); nch = 0
    for rx, rz, mca in files:
        with open(mca, "rb") as f:
            for lz in range(32):
                cwz = (rz * 32 + lz) * 16
                if cwz + 16 <= wz0 or cwz >= wz1:
                    continue
                for lx in range(32):
                    cwx = (rx * 32 + lx) * 16
                    if cwx + 16 <= wx0 or cwx >= wx1:
                        continue
                    try:
                        ch = read_chunk(f, lx, lz)
                    except Exception:
                        ch = None
                    if ch is None:
                        continue
                    nch += 1
                    top = top_block_for_chunk(ch)        # (16,16) [z,x]
                    for zz in range(0, 16, STEP):
                        wz = cwz + zz
                        if not (wz0 <= wz < wz1):
                            continue
                        iy = (wz - wz0) // STEP
                        for xx in range(0, 16, STEP):
                            wx = cwx + xx
                            if not (wx0 <= wx < wx1):
                                continue
                            nm = str(top[zz, xx]).replace("minecraft:", "")
                            if nm in ("air", "cave_air", "void_air"):
                                continue
                            game[iy, (wx - wx0) // STEP] = BLOCK_COLORS.get(nm, DEF)
        print(f"  {mca.name}  ({nch} chunks, {time.time()-t0:.0f}s)", flush=True)

    # ---------- labels (re-label within crop) ----------
    lab_c, _ = label(land_c)
    szc = np.bincount(lab_c.ravel()); szc[0] = 0
    bigc = np.argsort(szc)[::-1][:2]
    nm_list = ["St Kitts", "Nevis"]
    labels = []
    for i, comp in enumerate(bigc):
        cy, cx = np.where(lab_c == comp)
        lxm, lym = int(cx.mean()), int(cy.mean())
        wx, wz = sx + lx0 + lxm, sz + ly0 + lym
        pky = int(np.interp(hgt_c[cy, cx].max(), _GIN, _YOUT))
        labels.append((lxm, lym, nm_list[i], wx, wz, pky))

    # ---------- figure ----------
    fig, axes = plt.subplots(1, 2, figsize=(15, 15 * Hc / Wc / 2 * 0.96))
    fig.patch.set_facecolor("#0e1726")
    axes[0].imshow(game, interpolation="nearest", extent=[0, Wc, Hc, 0])
    axes[0].set_title("In-game surface (rendered .mca, top block)", color="white", fontsize=12, pad=8)
    axes[1].imshow(bio, interpolation="nearest")
    axes[1].set_title("Biomes + masks", color="white", fontsize=12, pad=8)
    # mask overlay markers on a 3rd inset? keep feat as small overlay legend via panel-2 outline:
    # draw feature outlines on biome panel using feat colors at reduced alpha
    axes[1].imshow(feat, interpolation="nearest", alpha=0.0)  # feat kept for legend only
    for ax in axes:
        ax.set_xticks([]); ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_color("#3a4a66")
    for lx, ly, nm, wx, wz, pky in labels:
        for ax in axes:
            ax.annotate(f"{nm}\nX{wx} Z{wz}\n~Y{pky}", (lx, ly), color="white", fontsize=8,
                        ha="center", va="center",
                        bbox=dict(boxstyle="round,pad=0.3", fc="#000000bb", ec="white", lw=0.6))
    leg1 = [Patch(fc=np.array(zone_rgb[z]) / 255, ec="#222",
                  label=f"{present[z][0].replace('_',' ').title()} ({present[z][1]/land_n*100:.0f}%)")
            for z in sorted(present, key=lambda z: -present[z][1])]
    leg1 += [Patch(fc=np.array(FEAT[k]) / 255, ec="#222",
                   label=f"{lab2} ({fcnt[k]/land_n*100:.0f}% land)")
             for k, lab2 in (("rock", "rock slope"), ("beach", "beach"), ("snow", "snow"))]
    axes[1].legend(handles=leg1, loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=8,
                   framealpha=0.92, facecolor="#14203a", labelcolor="white", edgecolor="#3a4a66",
                   title="Biomes / masks", title_fontsize=9)
    fig.suptitle(f"St Kitts + Nevis — in-game vs biome/mask  ({land_n/1e6:.1f}M land blocks, "
                 f"world X{wx0}-{wx1} Z{wz0}-{wz1})", color="white", fontsize=13, y=0.99)
    fig.tight_layout(rect=[0, 0, 0.86, 0.97])
    fig.savefig(OUT, dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    print(f"\nsaved {OUT}  ({nch} chunks read)", flush=True)
    print("biomes:", {present[z][0]: f"{present[z][1]/land_n*100:.0f}%" for z in sorted(present, key=lambda z:-present[z][1])})
    print("features %land:", {k: f"{v/land_n*100:.0f}%" for k, v in fcnt.items()})


if __name__ == "__main__":
    main()
