"""Render the user's current working layout (layout.json positions) + labels."""
import sys, json
sys.path.insert(0, "islands")
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtCore import QRectF, Qt
import numpy as np
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
import world_layout_studio as W

app = QApplication([])
w = W.Studio()
placed = set(i["dem_path"] for i in json.load(open("islands/layout.json"))["islands"])
w.scene.clearSelection()
sr = w.scene.sceneRect()
WW = 1700; HH = int(WW*sr.height()/sr.width())
img = QImage(WW, HH, QImage.Format.Format_RGB888); img.fill(Qt.GlobalColor.black)
p = QPainter(img); p.setRenderHint(QPainter.RenderHint.Antialiasing)
w.scene.render(p, QRectF(0, 0, WW, HH), sr); p.end()
buf = img.bits(); buf.setsize(HH*WW*3); arr = np.frombuffer(buf, np.uint8).reshape(HH, WW, 3)

def w2px(x, y): return (x-sr.x())/sr.width()*WW, (y-sr.y())/sr.height()*HH

fig, ax = plt.subplots(figsize=(16, 16*HH/WW), dpi=100); ax.imshow(arr)
for l in w.layers:
    fw, fh = l.footprint_w()
    cx, cy = l.world_x+fw/2, l.world_y+fh/2
    px, py = w2px(cx, cy)
    here = l.dem_path in placed
    short = l.name.split("(")[0].strip()[:16]
    ax.annotate(short, (px, py), color="white" if here else "#ffd24a", fontsize=8, weight="bold",
                ha="center", va="center", xytext=(px, py-20), textcoords="data",
                bbox=dict(boxstyle="round,pad=0.15", fc="#10182add" if here else "#3a2a10dd", ec="#5a6b8c"))
ax.set_title("Your working layout  (gold = not yet placed: Jan Mayen / Bahamas-Crooked / Admiralty)", fontsize=12)
ax.axis("off"); fig.tight_layout()
fig.savefig("islands/my_layout.jpg", pil_kwargs={"quality": 88}, facecolor="white")
print("saved islands/my_layout.jpg")
