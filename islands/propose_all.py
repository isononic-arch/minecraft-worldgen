"""Geological placement mockup for ALL islands around Vandir, by climate zone +
tectonics. Kostati + New Vincentia stay where the user stuck them."""
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
w.margin = 33000; w._rebuild_scene_rect()

STUCK = ("17_288", "13_130")        # New Vincentia, Kostati — keep their positions
# filename-substring -> (centre x, y world blocks, short label, climate tag)
PLAN = {
    # cold NORTH
    "70_993":  (8000, -19000,  "Jan Mayen", "cold volcanic"),
    "49_722":  (32000, -19000, "Fogo (NF)", "cold low"),
    # wet WEST (windward)
    "-50_393": (-20000, 26000, "Madre de Dios", "wet fjord/karst"),
    "-17_622": (-15000, 45000, "Efate", "wet volcanic"),
    # dry SW desert / leeward E (rainshadow)
    "14_834":  (1000, 61000,   "Fogo (CV)", "dry volcano"),
    "11_060":  (59000, 18000,  "Margarita", "dry continental"),
    "10_941":  (61000, 31000,  "La Tortuga", "arid flat"),
    "21_395":  (63000, 43000,  "Grand Turk", "dry coral"),
    # warm SOUTH — tropical / coral archipelago
    "23_887":  (15000, 66000,  "Bahamas", "coral"),
    "11_015":  (27000, 71000,  "Bijagos", "mangrove arch."),
    "12_445":  (34000, 57000,  "Grenadines", "near Kostati"),
    "18_299":  (43000, 64000,  "Anguilla/StM", "Lesser Antilles"),
    "11_863":  (51000, 68000,  "Los Roques", "atoll"),
    "-20_529": (55000, 58000,  "Ouvea", "atoll"),
    "-21_008": (61000, 62000,  "Loyalty", "islets"),
}

labels = []
for lyr in w.layers:
    fw, fh = lyr.footprint_w()
    if any(k in lyr.dem_path for k in STUCK):
        cx, cy = lyr.world_x + fw/2, lyr.world_y + fh/2
        nm = "New Vincentia" if "17_288" in lyr.dem_path else "Kostati"
        labels.append((cx, cy, nm, "stuck", True))
        continue
    key = next((k for k in PLAN if k in lyr.dem_path), None)
    if not key:
        continue
    cx, cy, nm, tag = PLAN[key]
    lyr.world_x = cx - fw/2; lyr.world_y = cy - fh/2
    w._refresh_item(lyr, keep_center=(cx, cy))
    labels.append((cx, cy, nm, tag, False))

w.scene.clearSelection()
sr = w.scene.sceneRect()
WW = 1700; HH = int(WW * sr.height() / sr.width())
img = QImage(WW, HH, QImage.Format.Format_RGB888); img.fill(Qt.GlobalColor.black)
p = QPainter(img); p.setRenderHint(QPainter.RenderHint.Antialiasing)
w.scene.render(p, QRectF(0, 0, WW, HH), sr); p.end()
buf = img.bits(); buf.setsize(HH * WW * 3)
arr = np.frombuffer(buf, np.uint8).reshape(HH, WW, 3)

def w2px(x, y): return (x - sr.x())/sr.width()*WW, (y - sr.y())/sr.height()*HH

fig, ax = plt.subplots(figsize=(16, 16*HH/WW), dpi=100); ax.imshow(arr)
# climate-zone captions
ax.text(*w2px(25000, -26000), "cold NORTH (snow / tundra)", color="#bcd0e8", fontsize=11, ha="center", alpha=0.75)
ax.text(*w2px(35000, 80000), "warm SOUTH — tropical / coral archipelago", color="#37c0a6", fontsize=12, ha="center", alpha=0.85)
ax.text(*w2px(-27000, 38000), "wet WEST\n(windward)", color="#7fc7a0", fontsize=10, ha="center", alpha=0.75, rotation=90)
ax.text(*w2px(72000, 30000), "dry EAST\n(leeward /\nrainshadow)", color="#d8b48a", fontsize=10, ha="center", alpha=0.75)
ax.text(*w2px(5000, 70000), "dry SW\n(desert)", color="#d8b48a", fontsize=9, ha="center", alpha=0.7)
# W volcanic arc hint
ax.plot(*zip(w2px(-7000, 2000), w2px(-3000, 50000)), color="#ffd24a", lw=1.4, ls=(0,(5,4)), alpha=0.5)
for cx, cy, nm, tag, stuck in labels:
    px, py = w2px(cx, cy)
    fc = "#3a2a10dd" if stuck else "#10182add"; ec = "#e0b000" if stuck else "#5a6b8c"
    ax.annotate(nm, (px, py), color="white", fontsize=8.5, weight="bold", ha="center", va="center",
                xytext=(px, py-22), textcoords="data", zorder=8,
                bbox=dict(boxstyle="round,pad=0.18", fc=fc, ec=ec))
    ax.text(px, py+16, tag, color="#9fb0c8", fontsize=6.5, ha="center", style="italic", zorder=8)
ax.set_title("Vandir — geological placement of all islands (cold N · wet W · dry SW/E · warm S)  — gold = stuck", fontsize=12)
ax.axis("off"); fig.tight_layout()
fig.savefig("islands/placement_all.jpg", pil_kwargs={"quality": 88}, facecolor="white")
print("saved islands/placement_all.jpg")
