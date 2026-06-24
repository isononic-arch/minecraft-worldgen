"""Render a PROPOSED island placement around Vandir as an annotated JPG."""
import sys
sys.path.insert(0, "islands")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtCore import QRectF, Qt
import numpy as np
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
import world_layout_studio as W

app = QApplication([])
w = W.Studio()
w.margin = 30000; w._rebuild_scene_rect()

# proposed CENTRES (world blocks). Biome map: N=cold (snow peaks), S=warm ->
# tropical/coral go SOUTH; only temperate New Vincentia stays NW.
PLAN = {
    "17_288": ((-3000, 13000),  "temperate volcanic - W arc, NW (cool)"),
    "-17_622": ((-2000, 45000), "volcanic - W arc, SW"),
    "11_060": ((54000, 42000),  "continental shelf island, warm SE coast"),
    "13_130": ((57000, 59000),  "tropical - warm south"),
    "23_887": ((28000, 65000),  "coral platform - southern shallows"),
    "-20_529": ((43000, 62000), "coral atoll - southern archipelago"),
    "-21_008": ((49000, 66000), "low islets - paired w/ atoll"),
}
SHORT = {"17_288": "New Vincentia", "-17_622": "Efate", "11_060": "Margarita",
         "13_130": "Kostati", "23_887": "Bahamas", "-20_529": "Ouvea", "-21_008": "Loyalty"}

placed = []
for lyr in w.layers:
    key = next((k for k in PLAN if k in lyr.dem_path), None)
    if not key:
        continue
    (cx, cy), why = PLAN[key]
    fw, fh = lyr.footprint_w()
    lyr.world_x = cx - fw / 2; lyr.world_y = cy - fh / 2
    w._refresh_item(lyr, keep_center=(cx, cy))
    placed.append((SHORT[key], cx, cy, why))

w.scene.clearSelection()        # drop dashed selection boxes for a clean render
# render scene to numpy
sr = w.scene.sceneRect()
WW = 1600; HH = int(WW * sr.height() / sr.width())
img = QImage(WW, HH, QImage.Format.Format_RGB888); img.fill(Qt.GlobalColor.black)
p = QPainter(img); p.setRenderHint(QPainter.RenderHint.Antialiasing)
w.scene.render(p, QRectF(0, 0, WW, HH), sr); p.end()
buf = img.bits(); buf.setsize(HH * WW * 3)
arr = np.frombuffer(buf, np.uint8).reshape(HH, WW, 3)

def w2px(wx, wy):
    return (wx - sr.x()) / sr.width() * WW, (wy - sr.y()) / sr.height() * HH

fig, ax = plt.subplots(figsize=(15, 15 * HH / WW), dpi=100)
ax.imshow(arr)
# faint tectonic hints: W arc + SE warm zone
ax.plot(*zip(w2px(-6000, 2000), w2px(-2000, 48000)), color="#ffd24a", lw=1.6, ls=(0, (5, 4)), alpha=0.55)
ax.text(*w2px(-9500, 28000), "W volcanic arc", color="#ffd24a", fontsize=9, rotation=90, alpha=0.7, va="center")
ax.text(*w2px(38000, 73000), "warm SOUTH — tropical / coral archipelago", color="#37c0a6", fontsize=10, alpha=0.8, ha="center")
ax.text(*w2px(25000, -6000), "cold NORTH (snow peaks)", color="#bcd0e8", fontsize=9, alpha=0.6, ha="center")
for name, cx, cy, why in placed:
    px, py = w2px(cx, cy)
    ax.annotate(name, (px, py), color="white", fontsize=11, weight="bold", ha="center",
                va="center", xytext=(px, py - 26), textcoords="data",
                bbox=dict(boxstyle="round,pad=0.25", fc="#10182add", ec="#5a6b8c"))
    ax.text(px, py + 20, why, color="#aebbd0", fontsize=7.5, ha="center", style="italic")
ax.set_title("Vandir — proposed island placement (a starting point, drag to taste)",
             color="#1a1a1a", fontsize=13)
ax.axis("off"); fig.tight_layout()
fig.savefig("islands/proposed_placement.jpg", pil_kwargs={"quality": 88}, facecolor="white")
print("saved islands/proposed_placement.jpg")
