"""island_painter.py — per-island biome + lithology painter (mirrors the mainland
override_studio workflow, scoped to one island, with real-raster plug-and-play).

Loads a baked island's masks (height for a hillshade backdrop + current band
override for reference), lets you paint:
  * BIOME layer   -> override_painted.tif (zone codes; 0 = use altitude bands)
  * LITHOLOGY layer-> lithology_painted.tif (group ids; 0 = use zone_to_group)
Import a real land-cover raster from island_geo_data/<key>/ to fill the biome
layer from real data. Save writes the *_painted.tif back; the bake honors them
(painted wins, procedural is the fallback) — so zone_to_group / bands only fire
where unpainted.

Run: py islands/island_painter.py            (pick island in the UI)
     py islands/island_painter.py --island new_vincentia
"""
from __future__ import annotations
import sys, json, re
from pathlib import Path
import numpy as np
import rasterio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.biome_assignment import OVERRIDE_BIOME_MAP
from tools.world_biome_map import BIOME_COLORS
from derive_masks_from_height import _GIN, _YOUT

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QComboBox, QSlider, QLabel, QListWidget, QListWidgetItem, QFileDialog, QMessageBox)
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor
from PyQt6.QtCore import Qt, QPoint

MASKS = ROOT / "islands" / "masks_islands"
GEO = ROOT / "islands" / "island_geo_data"
WORK_MAX = 1500   # longest side of the working/paint canvas

LITHO_GROUPS = {"granitic": 1, "arid_basaltic": 2, "temperate_basaltic": 3,
                "limestone": 4, "deepslate_metamorphic": 5, "mossy_temperate": 6}
LITHO_COLORS = {1: (150, 140, 120), 2: (90, 75, 60), 3: (70, 80, 95), 4: (210, 205, 185),
                5: (95, 95, 110), 6: (90, 120, 80)}


def _safe(n): return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


def _hillshade(mcy):
    gz, gx = np.gradient(mcy.astype(np.float32))
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gz) * 0.7)
    aspect = np.arctan2(-gz, gx)
    az, alt = np.deg2rad(315.0), np.deg2rad(45.0)
    hs = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    return np.clip((hs + 1) / 2, 0, 1).astype(np.float32)


class Canvas(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setMouseTracking(True)
        self.zoom = 1.0
        self.pan = QPoint(0, 0)
        self._drag = None
        self._paint = False

    # ---- screen <-> image coords ----
    def img_pt(self, sp):
        x = (sp.x() - self.pan.x()) / self.zoom
        y = (sp.y() - self.pan.y()) / self.zoom
        return int(x), int(y)

    def wheelEvent(self, e):
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        before = self.img_pt(e.position().toPoint())
        self.zoom = float(np.clip(self.zoom * f, 0.2, 20))
        # keep cursor anchored
        sp = e.position().toPoint()
        self.pan = QPoint(int(sp.x() - before[0] * self.zoom), int(sp.y() - before[1] * self.zoom))
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton or (
                e.button() == Qt.MouseButton.LeftButton and e.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self._drag = e.position().toPoint()
        elif e.button() == Qt.MouseButton.LeftButton:
            self._paint = True; self._stroke(e.position().toPoint(), erase=False)
        elif e.button() == Qt.MouseButton.RightButton:
            self._paint = True; self._stroke(e.position().toPoint(), erase=True)

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            d = e.position().toPoint() - self._drag
            self.pan += d; self._drag = e.position().toPoint(); self.update()
        elif self._paint:
            self._stroke(e.position().toPoint(), erase=(e.buttons() & Qt.MouseButton.RightButton))

    def mouseReleaseEvent(self, e):
        self._drag = None; self._paint = False

    def _stroke(self, sp, erase):
        ix, iy = self.img_pt(sp)
        self.win.paint_at(ix, iy, erase)

    def paintEvent(self, _):
        img = self.win.compose()
        if img is None:
            return
        qp = QPainter(self)
        qp.fillRect(self.rect(), QColor(14, 23, 38))
        qp.translate(self.pan)
        qp.scale(self.zoom, self.zoom)
        qp.drawImage(0, 0, img)


class PainterWindow(QMainWindow):
    def __init__(self, island=None):
        super().__init__()
        self.setWindowTitle("Island Painter")
        self.resize(1500, 1000)
        self.mode = "biome"
        self.cur_val = 70
        self.brush = 14
        self.name = None
        self.canvas = Canvas(self)
        self._build_ui()
        islands = sorted(d.name for d in MASKS.iterdir() if (d / "height.tif").exists())
        self.sel.addItems(islands)
        if island:
            m = next((i for i in islands if island in i), None)
            if m: self.sel.setCurrentText(m)
        if islands:
            self.load(self.sel.currentText())

    def _build_ui(self):
        cw = QWidget(); lay = QHBoxLayout(cw); self.setCentralWidget(cw)
        side = QVBoxLayout(); side_w = QWidget(); side_w.setLayout(side); side_w.setFixedWidth(250)
        lay.addWidget(side_w); lay.addWidget(self.canvas, 1)

        self.sel = QComboBox(); self.sel.currentTextChanged.connect(lambda t: t and self.load(t))
        side.addWidget(QLabel("Island")); side.addWidget(self.sel)

        self.modebox = QComboBox(); self.modebox.addItems(["Biome", "Lithology"])
        self.modebox.currentTextChanged.connect(self._mode_changed)
        side.addWidget(QLabel("Paint layer")); side.addWidget(self.modebox)

        side.addWidget(QLabel("Palette (click to select)"))
        self.pal = QListWidget(); self.pal.itemClicked.connect(self._pick)
        side.addWidget(self.pal, 1)

        side.addWidget(QLabel("Brush size"))
        self.bs = QSlider(Qt.Orientation.Horizontal); self.bs.setRange(1, 120); self.bs.setValue(self.brush)
        self.bs.valueChanged.connect(lambda v: setattr(self, "brush", v))
        side.addWidget(self.bs)

        for txt, fn in [("Import raster (geo_data)", self.import_raster),
                        ("Clear painted layer", self.clear_layer),
                        ("Save *_painted.tif", self.save)]:
            b = QPushButton(txt); b.clicked.connect(fn); side.addWidget(b)
        self.status = QLabel("—"); self.status.setWordWrap(True); side.addWidget(self.status)
        self._fill_palette()

    def _fill_palette(self):
        self.pal.clear()
        if self.mode == "biome":
            items = [(z, n, BIOME_COLORS.get(n, (150, 150, 150))) for z, n in sorted(OVERRIDE_BIOME_MAP.items()) if z]
        else:
            items = [(gid, g, LITHO_COLORS[gid]) for g, gid in LITHO_GROUPS.items()]
        for val, nm, col in items:
            it = QListWidgetItem(f"{val:>3}  {nm}")
            it.setData(Qt.ItemDataRole.UserRole, val)
            it.setForeground(QColor(*col))
            self.pal.addItem(it)

    def _pick(self, it):
        self.cur_val = int(it.data(Qt.ItemDataRole.UserRole))
        self.status.setText(f"selected {self.mode} value {self.cur_val}")

    def _mode_changed(self, t):
        self.mode = "biome" if t == "Biome" else "litho"
        self.cur_val = 70 if self.mode == "biome" else 6
        self._fill_palette(); self.canvas.update()

    # ---- load island ----
    def load(self, name):
        self.name = name
        d = MASKS / name
        h = rasterio.open(d / "height.tif").read(1)
        self.bbox_hw = h.shape
        mcy = np.interp(h.astype(np.float64), _GIN, _YOUT)
        land = mcy > 63.4
        ys, xs = np.where(land)
        if len(ys) == 0:
            self.status.setText("no land"); return
        m = 40
        self.cy0, self.cx0 = max(0, ys.min() - m), max(0, xs.min() - m)
        cy1, cx1 = min(h.shape[0], ys.max() + m), min(h.shape[1], xs.max() + m)
        crop = (slice(self.cy0, cy1), slice(self.cx0, cx1))
        ch, cw = cy1 - self.cy0, cx1 - self.cx0
        self.scale = max(1, max(ch, cw) // WORK_MAX)
        hs = _hillshade(mcy[crop])[::self.scale, ::self.scale]
        self.hill = (np.stack([hs] * 3, -1) * 255).astype(np.uint8)
        self.land = land[crop][::self.scale, ::self.scale]
        # current band override (reference base) + painted layers (0 = fallback)
        ov = rasterio.open(d / "override.tif").read(1)[crop][::self.scale, ::self.scale]
        self.bands_ov = ov
        self.wh, self.ww = hs.shape
        self.biome = self._load_painted(d / "override_painted.tif", crop)
        self.litho = self._load_painted(d / "lithology_painted.tif", crop)
        self._build_luts()
        self.canvas.zoom = min(self.canvas.width() / max(self.ww, 1), self.canvas.height() / max(self.wh, 1)) or 1.0
        self.canvas.pan = QPoint(0, 0)
        self.status.setText(f"{name}: bbox {self.bbox_hw[1]}x{self.bbox_hw[0]}, work {self.ww}x{self.wh} (1:{self.scale})")
        self.canvas.update()

    def _load_painted(self, path, crop):
        if path.exists():
            return rasterio.open(path).read(1)[crop][::self.scale, ::self.scale].copy()
        return np.zeros((self.wh, self.ww), np.uint8)

    def _build_luts(self):
        self.blut = np.zeros((256, 3), np.uint8)
        for z, n in OVERRIDE_BIOME_MAP.items():
            self.blut[z] = BIOME_COLORS.get(n, (150, 150, 150))
        self.llut = np.zeros((256, 3), np.uint8)
        for gid, c in LITHO_COLORS.items():
            self.llut[gid] = c

    # ---- paint ----
    def paint_at(self, ix, iy, erase):
        if self.name is None or not (0 <= ix < self.ww and 0 <= iy < self.wh):
            return
        r = max(1, int(self.brush / self.scale))
        y0, y1 = max(0, iy - r), min(self.wh, iy + r + 1)
        x0, x1 = max(0, ix - r), min(self.ww, ix + r + 1)
        yy, xx = np.ogrid[y0:y1, x0:x1]
        disk = (yy - iy) ** 2 + (xx - ix) ** 2 <= r * r
        disk &= self.land[y0:y1, x0:x1]            # never paint ocean
        layer = self.biome if self.mode == "biome" else self.litho
        layer[y0:y1, x0:x1][disk] = 0 if erase else self.cur_val
        self.canvas.update()

    def compose(self):
        if self.name is None:
            return None
        eff_b = np.where(self.biome > 0, self.biome, self.bands_ov)
        base = self.blut[eff_b].astype(np.float32)
        out = np.where(self.land[..., None], self.hill * 0.4 + base * 0.6, self.hill * 0.5)
        if self.mode == "litho":
            lc = self.llut[self.litho].astype(np.float32)
            painted = self.litho > 0
            out = np.where(painted[..., None], self.hill * 0.4 + lc * 0.6, out)
        out = np.clip(out, 0, 255).astype(np.uint8)
        h, w = out.shape[:2]
        return QImage(np.ascontiguousarray(out).data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()

    def clear_layer(self):
        if self.name is None: return
        (self.biome if self.mode == "biome" else self.litho)[:] = 0
        self.canvas.update()

    # ---- raster import ----
    def import_raster(self):
        if self.name is None: return
        try:
            from islands.import_island_raster import load_override_from_geo
        except Exception as e:
            QMessageBox.warning(self, "import", f"importer missing: {e}"); return
        key = next((k for k in [self.name] if k), self.name)
        ov_full = load_override_from_geo(self.name, self.bbox_hw)
        if ov_full is None:
            QMessageBox.information(self, "import",
                f"No raster found in island_geo_data/ for {self.name}.\n"
                f"Drop a landcover.tif there (see island_geo_data/README.md).")
            return
        crop = (slice(self.cy0, self.cy0 + self.wh * self.scale),
                slice(self.cx0, self.cx0 + self.ww * self.scale))
        sub = ov_full[crop][::self.scale, ::self.scale]
        sub = sub[:self.wh, :self.ww]
        self.biome[sub > 0] = sub[sub > 0]
        self.status.setText(f"imported raster -> {int((sub>0).sum())} px filled")
        self.canvas.update()

    # ---- save ----
    def save(self):
        if self.name is None: return
        d = MASKS / self.name
        H, W = self.bbox_hw
        for layer, fname in [(self.biome, "override_painted.tif"), (self.litho, "lithology_painted.tif")]:
            if not (layer > 0).any():
                (d / fname).unlink(missing_ok=True); continue
            full = np.zeros((H, W), np.uint8)
            up = np.repeat(np.repeat(layer, self.scale, 0), self.scale, 1)
            ch, cw = up.shape
            full[self.cy0:self.cy0 + ch, self.cx0:self.cx0 + cw] = up[:H - self.cy0, :W - self.cx0]
            prof = dict(driver="GTiff", height=H, width=W, count=1, dtype="uint8", compress="deflate")
            with rasterio.open(d / fname, "w", **prof) as o:
                o.write(full, 1)
        self.status.setText("saved override_painted.tif + lithology_painted.tif — re-bake to apply")


def main():
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--island", default=None)
    a = ap.parse_args()
    app = QApplication(sys.argv)
    w = PainterWindow(a.island); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
