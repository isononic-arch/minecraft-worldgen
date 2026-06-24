"""Quick tectonic-context sketch: read Vandir's mountains, infer plausible plate
boundaries, and highlight where volcanic / shelf islands would form. -> JPG."""
import numpy as np
import rasterio
from rasterio.enums import Resampling
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Ellipse, FancyArrow
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
N = 1100
_GIN = np.array([0,5000,12000,17050,18000,21000,26000,30000,35000,42000,50000,58000,65496], float)
_YOUT = np.array([-64,-45,25,63,67,78,110,145,180,360,490,610,700], float)

with rasterio.open(str(ROOT/"masks"/"height.tif")) as s:
    raw = s.read(1, out_shape=(N, N), resampling=Resampling.average).astype(float)
mcy = np.interp(raw, _GIN, _YOUT)

def hillshade(z, az=315, alt=45):
    az, alt = np.radians(az), np.radians(alt)
    gy, gx = np.gradient(z); sl = np.arctan(np.hypot(gx, gy)); asp = np.arctan2(-gx, gy)
    return np.clip(np.sin(alt)*np.cos(sl)+np.cos(alt)*np.sin(sl)*np.cos(az-asp), 0, 1)

hs = hillshade(mcy)
land = mcy > 63
mount = mcy > 300                     # mountains
ys, xs = np.where(mount)
cx, cy = xs.mean(), ys.mean()         # mountain-mass centroid
# principal axis of the mountain belt (orogen trend)
X = np.vstack([xs - cx, ys - cy]).astype(float)
cov = np.cov(X); evals, evecs = np.linalg.eigh(cov)
axis = evecs[:, np.argmax(evals)]     # primary direction (unit)
ang = np.degrees(np.arctan2(axis[1], axis[0]))
# where does the high mass sit vs the land centroid -> which margin is "active"
lys, lxs = np.where(land); lcx, lcy = lxs.mean(), lys.mean()
print(f"mountains: {mount.sum()} px  centroid=({cx:.0f},{cy:.0f})  orogen trend ~{ang:.0f} deg")
print(f"land centroid=({lcx:.0f},{lcy:.0f})  mountains lean "
      f"{'W' if cx<lcx else 'E'}/{'N' if cy<lcy else 'S'}")

# world->image: Vandir occupies the centre; add ocean margin
M = 490                                # margin px of ocean around Vandir (~22k blocks)
W = N + 2*M
canvas = np.zeros((W, W, 3), float)
canvas[:] = np.array([0.07, 0.11, 0.18])           # deep ocean
# place hillshade tinted (land greenish-grey, sea darker)
rgb = np.dstack([hs, hs*1.03, hs*0.85])
rgb[~land] = np.array([0.10, 0.16, 0.26]) + hs[~land, None]*0.10
canvas[M:M+N, M:M+N] = np.clip(rgb, 0, 1)

fig, ax = plt.subplots(figsize=(13, 13), dpi=110)
ax.imshow(canvas, origin="upper")
def w2i(px, py): return px + M, py + M     # vandir-px -> canvas-px

# --- 1. Orogen axis (continental uplift / collision) through the belt ---
L = 0.46*N
p0 = (cx - axis[0]*L, cy - axis[1]*L); p1 = (cx + axis[0]*L, cy + axis[1]*L)
ax.plot([w2i(*p0)[0], w2i(*p1)[0]], [w2i(*p0)[1], w2i(*p1)[1]],
        color="#ff5a5a", lw=3, ls=(0,(6,4)), label="Orogen / collision uplift (Vandir ranges)")

# normal to the orogen = dip direction of a subduction system
nrm = np.array([-axis[1], axis[0]])
if nrm[0] > 0: nrm = -nrm               # point the volcanic-arc system WEST (New Vincentia)

# --- 2. West subduction system: trench (offshore) + volcanic arc (inboard) ---
def parallel_curve(offset, bow=0.0, t=np.linspace(-0.5, 0.5, 60)):
    # line parallel to orogen, shifted along nrm by `offset`, with a slight bow
    base = np.array([cx, cy])[None,:] + np.outer(t, axis*N*0.9)
    shift = nrm*offset + nrm*bow*np.cos(t*np.pi)[:,None]*N
    return base + shift
trench = parallel_curve(0.86*N)       # deep trench, well offshore W
arc    = parallel_curve(0.60*N)       # volcanic arc in the open ocean (separate island)
ax.plot(*np.array([w2i(*p) for p in trench]).T, color="#23408f", lw=4,
        label="Subduction trench (deep)")
ax.plot(*np.array([w2i(*p) for p in arc]).T, color="#ffd24a", lw=2.5, ls=(0,(2,2)),
        label="Volcanic island arc  ->  New Vincentia (W, volcanic)")
# arc island highlight
amid = arc[len(arc)//2]
ax.add_patch(Ellipse(w2i(*amid), 150, 230, angle=ang, fill=False, ec="#ffd24a", lw=2.5))
ax.text(*w2i(amid[0]-40, amid[1]-160), "New Vincentia\n(volcanic arc)",
        color="#ffe9a8", ha="center", fontsize=11, weight="bold")
# subduction arrow (plate diving under)
av = nrm*70
ax.add_patch(FancyArrow(*w2i(trench[len(trench)//2][0], trench[len(trench)//2][1]),
             -av[0], -av[1], width=6, color="#9fb4e8", length_includes_head=True))

# --- 3. SE tropical arc / shelf (Kostati) ---
# a gentler boundary to the SE corner where warm shelf/arc islands sit
se = np.array([cx + 0.55*N, cy + 0.62*N])
t = np.linspace(0, 1, 40)
kos = np.array([cx + (0.25+0.6*t)*N, cy + (0.30+0.7*t)*N]).T
kbow = kos + np.outer(np.sin(t*np.pi), np.array([0.10*N, -0.06*N]))
ax.plot(*np.array([w2i(*p) for p in kbow]).T, color="#37c0a6", lw=2.5, ls=(0,(4,3)),
        label="Tropical back-arc / shelf  ->  Kostati (SE)")
ax.add_patch(Ellipse(w2i(*se), 230, 150, angle=25, fill=False, ec="#37c0a6", lw=2.5))
ax.text(*w2i(se[0], se[1]+140), "Kostati archipelago\n(tropical, SE)",
        color="#bff0e6", ha="center", fontsize=11, weight="bold")

# --- 4. a transform offset (oceanic fracture) for flavour ---
tf0 = trench[8]; tf1 = trench[8] + axis*0 + nrm*0   # short hint
ax.plot([w2i(*trench[14])[0], w2i(*(trench[14]+nrm*0.5*N))[0]],
        [w2i(*trench[14])[1], w2i(*(trench[14]+nrm*0.5*N))[1]],
        color="#7f8aa3", lw=1.6, ls=(0,(1,2)), label="Transform / fracture zone")

ax.add_patch(plt.Rectangle(w2i(0,0), N, N, fill=False, ec="#b6c6da", lw=1.2, alpha=0.6))
ax.text(*w2i(20, -28), "VANDIR (50k x 50k)", color="#dbe6f2", fontsize=12, style="italic")
ax.set_xlim(0, W); ax.set_ylim(W, 0); ax.axis("off")
ax.legend(loc="lower left", fontsize=9, framealpha=0.82, facecolor="#10182a",
          edgecolor="#33415c", labelcolor="#e8eefc")
ax.set_title("Vandir — plausible tectonic boundaries & island-forming zones (sketch)",
             color="#1a1a1a", fontsize=13)
fig.tight_layout()
out = ROOT/"islands"/"tectonic_sketch.jpg"
fig.savefig(out, pil_kwargs={"quality": 88}, facecolor="white"); print("saved", out)
