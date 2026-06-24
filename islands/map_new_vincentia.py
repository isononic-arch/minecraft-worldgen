"""map_new_vincentia.py — zoomed, labeled zone map of New Vincentia from the baked
masks (override biomes + rock/beach/snow/shore feature masks). Two panels:
  (1) biome zones, hillshade-relief tinted, island labels
  (2) surface features (water / land / beach / rock slope / snow / shore)
Saves islands/out/new_vincentia_zonemap.png.
"""
import sys, json
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
from derive_masks_from_height import _GIN, _YOUT

MD = ROOT / "islands" / "masks_islands" / "new_vincentia_st_kitts_nevis_statia"
OUT = ROOT / "islands" / "out" / "new_vincentia_zonemap.png"

OCEAN = (28, 52, 96)


def rd(name):
    return rasterio.open(str(MD / name)).read(1)


def main():
    ov = rd("override.tif")
    rock = rd("rock_gap.tif")
    snow = rd("snow_gap.tif")
    beach = rd("beach.tif")
    shore = rd("shore.tif")
    hgt = rd("height.tif")
    man = json.loads((MD / "manifest.json").read_text())
    ox, oz = man["world_offset_px"]
    sx, sz = round(ox / 512) * 512, round(oz / 512) * 512   # render snaps to 512

    land = ov > 0
    # crop to content
    ys, xs = np.where(land)
    M = 70
    y0, y1 = max(0, ys.min() - M), min(ov.shape[0], ys.max() + M)
    x0, x1 = max(0, xs.min() - M), min(ov.shape[1], xs.max() + M)
    sl = (slice(y0, y1), slice(x0, x1))
    ov, rock, snow, beach, shore, hgt, land = (a[sl] for a in (ov, rock, snow, beach, shore, hgt, land))
    H, W = ov.shape
    mcy = np.interp(hgt.astype(np.float64), _GIN, _YOUT)

    # hillshade from terrain
    gz, gx = np.gradient(mcy.astype(np.float32))
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gz) * 0.6)
    aspect = np.arctan2(-gz, gx)
    az, alt = np.deg2rad(315.0), np.deg2rad(45.0)
    hs = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    hs = np.clip((hs + 1) / 2, 0, 1)

    # ---- panel 1: biomes, relief-tinted ----
    zone_rgb = {0: OCEAN}
    present = {}
    for z in np.unique(ov):
        if z == 0:
            continue
        nm = OVERRIDE_BIOME_MAP.get(int(z), f"zone{z}")
        zone_rgb[int(z)] = tuple(BIOME_COLORS.get(nm, (150, 150, 150)))
        present[int(z)] = (nm, int((ov == z).sum()))
    img1 = np.zeros((H, W, 3), np.float32)
    for z, c in zone_rgb.items():
        img1[ov == z] = c
    # relief tint on land only
    tint = (0.45 + 0.55 * hs)[..., None]
    landmask3 = land[..., None]
    img1 = np.where(landmask3, np.clip(img1 * tint, 0, 255), img1)
    img1 = img1.astype(np.uint8)

    # ---- panel 2: surface features ----
    FEAT = {"water": OCEAN, "land": (122, 138, 96), "shore": (150, 160, 110),
            "beach": (236, 214, 146), "rock": (138, 134, 128), "snow": (242, 246, 250)}
    img2 = np.zeros((H, W, 3), np.uint8)
    img2[:] = FEAT["water"]
    img2[land] = FEAT["land"]
    img2[land & (shore > 0)] = FEAT["shore"]
    img2[land & (beach > 60)] = FEAT["beach"]      # within ~4 blocks of coast
    img2[land & (rock > 0)] = FEAT["rock"]          # steep slope -> rock gap (land only)
    img2[land & (snow > 0)] = FEAT["snow"]
    # relief tint features too (keep water flat)
    t2 = np.where(landmask3, (0.5 + 0.5 * hs)[..., None], 1.0)
    img2 = np.clip(img2.astype(np.float32) * t2, 0, 255).astype(np.uint8)

    feat_counts = {
        "rock slope (gap 5)": int((land & (rock > 0)).sum()),
        "beach (gap 9)": int((land & (beach > 60)).sum()),
        "snow (gap 7)": int((land & (snow > 0)).sum()),
        "shore band": int((land & (shore > 0)).sum()),
    }
    land_n = int(land.sum())

    # ---- island labels (centroids of 3 biggest landmasses) ----
    lab, n = label(land)
    sizes = np.bincount(lab.ravel()); sizes[0] = 0
    big = np.argsort(sizes)[::-1][:3]
    names = ["St Kitts", "Nevis", "Statia"]
    labels = []
    order = []
    for comp in big:
        if sizes[comp] == 0:
            continue
        cy, cx = np.where(lab == comp)
        order.append((cx.mean(), cy.mean(), int(hgt[cy, cx].max())))
    # name by size already in big order: biggest=St Kitts, etc. but assign by Z (N->S): Statia top
    order_sorted = sorted(range(len(order)), key=lambda i: order[i][1])  # by cy ascending (north->south)
    nm_by_pos = {}
    # north-most -> Statia, then St Kitts(biggest), Nevis south. Just label biggest=St Kitts, southern=Nevis, north=Statia
    # simplest: biggest is St Kitts
    big_idx = int(np.argmax([sizes[c] for c in big]))
    for i, comp in enumerate(big):
        cy, cx = np.where(lab == comp)
        wx, wz = sx + x0 + int(cx.mean()), sz + y0 + int(cy.mean())
        nm = names[i] if i < len(names) else f"isle{i}"
        pky = int(np.interp(hgt[cy, cx].max(), _GIN, _YOUT))
        labels.append((cx.mean(), cy.mean(), nm, wx, wz, pky))

    # ---- figure ----
    asp = H / W
    fw = 7.2
    fig, axes = plt.subplots(1, 2, figsize=(fw * 2 + 0.5, fw * asp * 0.95))
    fig.patch.set_facecolor("#0e1726")

    for ax, img, title in ((axes[0], img1, "Biomes (override zones, relief-tinted)"),
                           (axes[1], img2, "Surface features / zones")):
        ax.imshow(img, interpolation="nearest")
        ax.set_title(title, color="white", fontsize=12, pad=8)
        ax.set_xticks([]); ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_color("#3a4a66")

    # labels on biome panel
    for lx, ly, nm, wx, wz, pky in labels:
        axes[0].annotate(f"{nm}\nX {wx} Z {wz}\npeak ~Y{pky}", (lx, ly),
                         color="white", fontsize=8, ha="center", va="center",
                         bbox=dict(boxstyle="round,pad=0.3", fc="#000000aa", ec="white", lw=0.6))

    # biome legend
    leg1 = [Patch(fc=np.array(zone_rgb[z]) / 255, ec="#222",
                  label=f"{present[z][0].replace('_',' ').title()} ({present[z][1]/land_n*100:.0f}%)")
            for z in sorted(present, key=lambda z: -present[z][1])]
    axes[0].legend(handles=leg1, loc="upper left", bbox_to_anchor=(1.0, 1.0),
                   fontsize=8, framealpha=0.9, facecolor="#14203a", labelcolor="white",
                   edgecolor="#3a4a66", title="Biomes", title_fontsize=9)

    # feature legend
    fl = [("Rock slope", FEAT["rock"], feat_counts["rock slope (gap 5)"]),
          ("Beach", FEAT["beach"], feat_counts["beach (gap 9)"]),
          ("Snow cap", FEAT["snow"], feat_counts["snow (gap 7)"]),
          ("Shore band", FEAT["shore"], feat_counts["shore band"]),
          ("Land (vegetated)", FEAT["land"], None),
          ("Ocean (noise-gen fill)", FEAT["water"], None)]
    leg2 = []
    for nm, c, cnt in fl:
        lab_txt = nm if cnt is None else f"{nm} ({cnt/land_n*100:.0f}% of land)"
        leg2.append(Patch(fc=np.array(c) / 255, ec="#222", label=lab_txt))
    axes[1].legend(handles=leg2, loc="upper left", fontsize=8, framealpha=0.9,
                   facecolor="#14203a", labelcolor="white", edgecolor="#3a4a66")

    fig.suptitle(f"New Vincentia — St Kitts / Nevis / Statia   ({land_n/1e6:.1f}M land blocks, "
                 f"world X{sx}-{sx+W} Z{sz}-{sz+H})", color="white", fontsize=13, y=0.99)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(OUT, dpi=140, facecolor=fig.get_facecolor(), bbox_inches="tight")
    print(f"saved {OUT}  ({W}x{H} crop, {n} landmasses)", flush=True)
    print("biomes:", {present[z][0]: f"{present[z][1]/land_n*100:.0f}%" for z in sorted(present, key=lambda z:-present[z][1])})
    print("features:", feat_counts, "land_px", land_n)


if __name__ == "__main__":
    main()
