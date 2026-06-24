"""Rough mockup of a Caribbean-scale archipelago south/SE of Vandir: tiered
island density (signature / mid / cays / banks) along a gentle arc. Procedural
blobs — illustrates DENSITY, not real DEMs."""
import numpy as np
from pathlib import Path
from PIL import Image
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Ellipse

ROOT = Path(__file__).resolve().parent.parent
bg = np.asarray(Image.open(ROOT / "islands" / "cache" / "vandir_bg.png").convert("RGB")) / 255.0
S = bg.shape[0]                     # 1500 px = 50000 blocks
M = 520                             # ocean margin px
W = S + 2 * M
canvas = np.zeros((W, W, 3)); canvas[:] = (0.07, 0.11, 0.18)
canvas[M:M+S, M:M+S] = bg
rng = np.random.default_rng(7)

def I(px, py): return px + M, py + M           # vandir-px -> canvas px (vandir px: 0..S)

def potato(cx, cy, r, n=11, rough=0.4):
    a = np.linspace(0, 2*np.pi, n, endpoint=False)
    rad = r * (1 + rough*(rng.random(n)-0.5)*2)
    return np.column_stack([cx + rad*np.cos(a), cy + rad*np.sin(a)])

fig, ax = plt.subplots(figsize=(14, 14), dpi=100)
ax.imshow(canvas, origin="upper")

# southern arc the chain follows (vandir-px; y>S is south of Vandir, in the ocean margin)
def arc(t):                                    # t in [0,1] -> (x,y) gentle bow south
    x = 0.10*S + t*0.95*S
    y = 1.02*S + 0.45*S*np.sin(t*np.pi)*0.5 + 0.18*S*t
    return x, y

SAND = (0.86, 0.80, 0.55); GREEN = (0.45, 0.62, 0.38); ROCK = (0.55, 0.55, 0.58)

# 4) banks / shoals first (under everything) — turquoise shallows
for _ in range(7):
    t = rng.random(); x, y = arc(t)
    x += rng.normal(0, 0.06*S); y += rng.normal(0, 0.05*S)
    ax.add_patch(Ellipse(I(x, y), rng.uniform(40, 90), rng.uniform(26, 55),
                         angle=rng.uniform(0, 180), fc=(0.30, 0.62, 0.66), ec="none", alpha=0.32, zorder=2))

# 3) cays / islets — ~32 tiny blobs scattered + a couple tight clusters
def cay(x, y, r):
    ax.add_patch(Polygon(np.array([I(*p) for p in potato(x, y, r)]), closed=True,
                         fc=SAND if rng.random() < 0.6 else GREEN, ec="none", alpha=0.95, zorder=4))
for _ in range(26):
    t = rng.random(); x, y = arc(t)
    x += rng.normal(0, 0.10*S); y += rng.normal(0, 0.07*S)
    cay(x, y, rng.uniform(3, 8))
for _ in range(3):                             # tight Grenadines-style clusters
    t = rng.random(); bx, by = arc(t); bx += rng.normal(0, 0.05*S)
    for _ in range(rng.integers(3, 6)):
        cay(bx + rng.normal(0, 16), by + rng.normal(0, 16), rng.uniform(2, 5))

# 2) mid-tier islands — ~7 medium blobs along the arc
for k in range(7):
    t = (k + 0.5) / 7; x, y = arc(t); x += rng.normal(0, 0.04*S); y += rng.normal(0, 0.03*S)
    ax.add_patch(Polygon(np.array([I(*p) for p in potato(x, y, rng.uniform(12, 20), rough=0.45)]),
                         closed=True, fc=GREEN if rng.random() < 0.5 else ROCK, ec="#2a3550", lw=0.5, zorder=5))

# 1) signature islands — labelled anchors at biome-sensible spots
SIG = [("Kostati", 0.62*S, 1.18*S, GREEN), ("Margarita", 1.02*S, 0.80*S, ROCK),
       ("Efate", -0.05*S, 0.92*S, ROCK), ("Fogo", 0.30*S, 1.42*S, ROCK),
       ("Bijagos", 0.80*S, 1.40*S, SAND)]
for name, x, y, col in SIG:
    ax.add_patch(Polygon(np.array([I(*p) for p in potato(x, y, 30, rough=0.5)]), closed=True,
                         fc=col, ec="#dfe8f5", lw=1.0, zorder=6))
    ax.text(*I(x, y-44), name, color="white", fontsize=10, weight="bold", ha="center", zorder=7,
            bbox=dict(boxstyle="round,pad=0.2", fc="#10182add", ec="#5a6b8c"))

ax.text(*I(0.5*S, -0.06*S), "VANDIR", color="#dbe6f2", fontsize=13, style="italic", ha="center")
# legend
from matplotlib.lines import Line2D
leg = [Line2D([0],[0], marker='o', color='w', markerfacecolor=GREEN, markersize=14, label='signature (real DEM, ~5)'),
       Line2D([0],[0], marker='o', color='w', markerfacecolor=ROCK, markersize=10, label='mid-tier (real DEM, ~6-8)'),
       Line2D([0],[0], marker='o', color='w', markerfacecolor=SAND, markersize=6, label='cays/islets (procedural, ~30)'),
       Line2D([0],[0], marker='o', color='w', markerfacecolor=(0.30,0.62,0.66), markersize=12, alpha=0.4, label='banks/shoals (shallow, ~7)')]
ax.legend(handles=leg, loc="upper left", fontsize=10, framealpha=0.85, facecolor="#10182a", labelcolor="#e8eefc", edgecolor="#33415c")
ax.set_title("Vandir — Caribbean-scale archipelago, rough density mock (~50 features, ~10 real downloads)", fontsize=12)
ax.axis("off"); fig.tight_layout()
fig.savefig(ROOT / "islands" / "archipelago_mock.jpg", pil_kwargs={"quality": 88}, facecolor="white")
print("saved islands/archipelago_mock.jpg")
