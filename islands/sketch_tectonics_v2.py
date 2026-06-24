"""Canonical plate model for Vandir — cleans the user's hand-drawn lines into a
coherent mosaic: triple junctions (not a starburst), a west subduction system
with correct polarity (trench -> volcanic arc -> mainland), and on-trend island
zones. New Vincentia = temperate volcanic (W, slightly N); Kostati = tropical (SE).
"""
import numpy as np
import rasterio
from rasterio.enums import Resampling
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, FancyArrowPatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N = 1100
M = 440                                  # ocean margin px (~20k blocks)
W = N + 2 * M
_GIN = np.array([0,5000,12000,17050,18000,21000,26000,30000,35000,42000,50000,58000,65496], float)
_YOUT = np.array([-64,-45,25,63,67,78,110,145,180,360,490,610,700], float)

with rasterio.open(str(ROOT/"masks"/"height.tif")) as s:
    raw = s.read(1, out_shape=(N, N), resampling=Resampling.average).astype(float)
mcy = np.interp(raw, _GIN, _YOUT)

def hillshade(z, az=315, alt=45):
    az, alt = np.radians(az), np.radians(alt)
    gy, gx = np.gradient(z); sl = np.arctan(np.hypot(gx, gy)); asp = np.arctan2(-gx, gy)
    return np.clip(np.sin(alt)*np.cos(sl)+np.cos(alt)*np.sin(sl)*np.cos(az-asp), 0, 1)

hs = hillshade(mcy); land = mcy > 63
canvas = np.zeros((W, W, 3), float); canvas[:] = (0.07, 0.11, 0.18)
rgb = np.dstack([hs, hs*1.03, hs*0.85])
rgb[~land] = np.array([0.10, 0.16, 0.26]) + hs[~land, None]*0.10
canvas[M:M+N, M:M+N] = np.clip(rgb, 0, 1)

def I(px, py): return (px + M, py + M)           # vandir-px (may be <0/>N offshore) -> canvas
def chaikin(pts, it=4):
    pts = np.array(pts, float)
    for _ in range(it):
        new = [pts[0]]
        for i in range(len(pts)-1):
            p, q = pts[i], pts[i+1]
            new += [0.75*p+0.25*q, 0.25*p+0.75*q]
        new.append(pts[-1]); pts = np.array(new)
    return pts

fig, ax = plt.subplots(figsize=(13, 13), dpi=110)
ax.imshow(canvas, origin="upper")

def draw(pts, **kw):
    p = chaikin(pts); xy = np.array([I(*q) for q in p])
    ax.plot(xy[:,0], xy[:,1], **kw); return p

RED="#ff5a5a"; TR="#2a48a0"; ARC="#ffd24a"; TROP="#37c0a6"; TF="#9aa6bf"

# ---- triple junctions (replace the central starburst) ----
TJ1 = (388, 372); TJ2 = (470, 545)
# ---- convergent sutures along the ranges (your solid lines, cleaned) ----
draw([(330, 95),(360, 230),TJ1], color=RED, lw=3.2, solid_capstyle="round",
     label="Convergent suture / thrust (ranges)")
draw([TJ1,(300, 300),(245, 470),(215, 660),(250, 770)], color=RED, lw=3.2)      # west front -> SW
draw([TJ1,(560, 430),(690, 520),(880, 500),(1100, 470)], color=RED, lw=3.2)     # east -> off-canvas (Espara)
draw([TJ1, TJ2], color=RED, lw=3.2)                                             # junction link
draw([TJ2,(560, 640),(700, 690),(820, 700)], color=RED, lw=3.2)                 # into east/ SE
draw([TJ2,(360, 660),(300, 760)], color=RED, lw=3.2)                            # SW spur
for tj in (TJ1, TJ2):
    ax.add_patch(plt.Circle(I(*tj), 13, color="white", zorder=6))
    ax.annotate("triple\njunction", I(tj[0]+18, tj[1]-6), color="#e8eefc", fontsize=8)

# ---- WEST subduction system: trench (offshore, convex W) + volcanic arc inboard ----
trench = draw([(-360, 120),(-300, 360),(-310, 600),(-260, 840)], color=TR, lw=4.5,
              label="Oceanic trench (subduction)")
arc = draw([(-150, 200),(-95, 420),(-110, 620),(-70, 800)], color=ARC, lw=2.8, ls=(0,(2,2)),
           label="Volcanic arc  ->  New Vincentia (W, temperate-volcanic)")
# subduction polarity: ocean plate dives EAST (toward +x) under Vandir
for ty in (300, 560, 780):
    a = FancyArrowPatch(I(-300, ty), I(-150, ty), arrowstyle="-|>", mutation_scale=20,
                        color="#9fb4e8", lw=2); ax.add_patch(a)
# New Vincentia marker: NW, slightly north, on the arc
nv = (-120, 290)
ax.add_patch(Ellipse(I(*nv), 150, 230, angle=-25, fill=False, ec=ARC, lw=2.6))
ax.add_patch(plt.Circle(I(nv[0], nv[1]+10), 6, color=ARC))
ax.text(*I(nv[0], nv[1]-150), "NEW VINCENTIA\nvolcanic · cool/temperate\n(W, slightly N)",
        color="#ffe9a8", ha="center", fontsize=10.5, weight="bold")

# ---- SE tropical back-arc / shelf: Kostati ----
draw([(820, 700),(980, 820),(1180, 980),(1360, 1130)], color=TROP, lw=2.6, ls=(0,(4,3)),
     label="Tropical back-arc / shelf  ->  Kostati (SE, Caribbean-type)")
kos = (1200, 1010)
ax.add_patch(Ellipse(I(*kos), 250, 160, angle=22, fill=False, ec=TROP, lw=2.6))
ax.text(*I(kos[0], kos[1]+150), "KOSTATI ARCHIPELAGO\ntropical · Caribbean geology\n(SE)",
        color="#bff0e6", ha="center", fontsize=10.5, weight="bold")

# ---- transform / fracture hint offsetting the arc ----
draw([(-300, 460),(-110, 500)], color=TF, lw=1.6, ls=(0,(1,2)),
     label="Transform / fracture zone")

# ---- plate labels ----
for (px, py, t) in [(150, 120, "WEST OCEANIC PLATE"), (650, 250, "VANDIR PLATE"),
                    (1250, 1300, "warm SE shelf")]:
    ax.text(*I(px, py), t, color="#aebbd0", fontsize=11, style="italic", alpha=0.8)

ax.add_patch(plt.Rectangle(I(0,0), N, N, fill=False, ec="#b6c6da", lw=1.1, alpha=0.55))
ax.text(*I(20, -30), "VANDIR (50k x 50k)", color="#dbe6f2", fontsize=12, style="italic")
ax.set_xlim(0, W); ax.set_ylim(W, 0); ax.axis("off")
ax.legend(loc="lower left", fontsize=9, framealpha=0.85, facecolor="#10182a",
          edgecolor="#33415c", labelcolor="#e8eefc")
ax.set_title("Vandir — canonical plate model (cleaned from your lines)", fontsize=13)
fig.tight_layout()
out = ROOT/"islands"/"tectonic_model.jpg"
fig.savefig(out, pil_kwargs={"quality": 88}, facecolor="white"); print("saved", out)
