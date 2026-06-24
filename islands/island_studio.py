"""island_studio.py — the MAINLAND override_studio (fast QGraphicsView canvas,
brush/fill/eyedropper/region-replace, undo/redo, separate Biome + Lithology tabs)
adapted to island data. Subclasses the real tab classes; only load/save change:

  * loads an island's override.tif / lithology.tif (cropped to the land bbox,
    downscaled to 1024 -> fast + the island fills the canvas)
  * saves to override_painted.tif / lithology_painted.tif (NEAREST-upscaled back
    into the full bbox) -> the bake honors them (painted wins; raster/bands fall back)

Run: py islands/island_studio.py  [--island new_vincentia]
"""
from __future__ import annotations
import sys, re
from pathlib import Path
import numpy as np
import rasterio
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import tools.override_studio as st
from tools.override_studio import (BiomePainterTab, LithologyPainterTab,
                                    DISPLAY_SIZE, SEA_LEVEL_RAW)

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QComboBox, QLabel, QTabWidget, QStatusBar, QMessageBox)
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtCore import Qt

MASKS = ROOT / "islands" / "masks_islands"
DS = DISPLAY_SIZE


def _square_crop(land, H, W, pad=40):
    ys, xs = np.where(land)
    if len(ys) == 0:
        return 0, 0, min(H, W)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    side = min(max(y1 - y0, x1 - x0) + 2 * pad, min(H, W))
    cy, cx = (y0 + y1) // 2, (x0 + x1) // 2
    sy0 = int(np.clip(cy - side // 2, 0, H - side))
    sx0 = int(np.clip(cx - side // 2, 0, W - side))
    return sy0, sx0, int(side)


def _nn(a, h, w):
    return np.array(Image.fromarray(a).resize((w, h), Image.NEAREST))


def _bil(a, h, w):
    return np.array(Image.fromarray(a.astype(np.float32), mode="F").resize((w, h), Image.BILINEAR))


class _IslandMixin:
    """Shared crop/back-drop plumbing for the island tabs."""
    def _setup_island(self, name):
        d = MASKS / name
        ov = rasterio.open(d / "override.tif").read(1)
        self._isl_H, self._isl_W = ov.shape
        land = ov > 0
        sy0, sx0, side = _square_crop(land, self._isl_H, self._isl_W)
        self._isl_crop = (sy0, sx0, side)
        self._isl_name = name
        self._isl_dir = d
        h = rasterio.open(d / "height.tif").read(1).astype(np.float32)
        crop_h = h[sy0:sy0 + side, sx0:sx0 + side]
        hr = _bil(crop_h, DS, DS)
        self.height_raw = hr
        self.ocean_mask = hr < SEA_LEVEL_RAW
        mn, mx = float(hr.min()), float(hr.max())
        self.height_norm = (hr - mn) / max(mx - mn, 1.0)
        self.orig_size = (side, side)        # save target = the square crop (W,H)
        return d, sy0, sx0, side, ov

    def _expand_to_bbox(self, grid_1024):
        """1024 edit grid -> NEAREST-upscaled into the full bbox (0 outside crop)."""
        sy0, sx0, side = self._isl_crop
        H, W = self._isl_H, self._isl_W
        up = _nn(grid_1024, side, side)
        full = np.zeros((H, W), np.uint8)
        ey, ex = min(sy0 + side, H), min(sx0 + side, W)
        full[sy0:ey, sx0:ex] = up[:ey - sy0, :ex - sx0]
        return full

    def _write_painted(self, full, path):
        prof = dict(driver="GTiff", height=full.shape[0], width=full.shape[1],
                    count=1, dtype="uint8", compress="deflate")
        with rasterio.open(path, "w", **prof) as o:
            o.write(full, 1)


class IslandBiomeTab(BiomePainterTab, _IslandMixin):
    def load_island(self, name):
        d, sy0, sx0, side, ov = self._setup_island(name)
        # continue from saved paint if present, else the (raster/band) override
        src = d / "override_painted.tif"
        base = rasterio.open(src).read(1) if src.exists() else ov
        self.override = _nn(base[sy0:sy0 + side, sx0:sx0 + side], DS, DS).astype(np.uint8)
        self.override_path = d / "override_painted.tif"
        self.hydro_centerline = self.hydro_order = self.hydro_lake = None
        self.undo.clear(); self.redo.clear(); self._update_undo_buttons()
        self.btn_save.setEnabled(True)
        if hasattr(self, "btn_save_upscale"):
            self.btn_save_upscale.setEnabled(True)
        self.canvas.fit_canvas(DS, DS)
        self._refresh()

    def save(self, upscale=False):
        if self.override is None:
            return
        full = self._expand_to_bbox(self.override)
        self._write_painted(full, self._isl_dir / "override_painted.tif")
        QMessageBox.information(self, "Saved",
            f"override_painted.tif written ({int((full>0).sum())} px).\nRe-bake: py islands/render_islands.py --bake {self._isl_name}")


class IslandLithoTab(LithologyPainterTab, _IslandMixin):
    def load_island(self, name, biome_backdrop):
        d, sy0, sx0, side, ov = self._setup_island(name)
        src = d / "lithology_painted.tif"
        if src.exists():
            base = rasterio.open(src).read(1)
        elif (d / "lithology.tif").exists():
            # lithology.tif is 1:8 -> upscale to bbox first
            l8 = rasterio.open(d / "lithology.tif").read(1)
            base = _nn(l8, self._isl_H, self._isl_W)
        else:
            base = np.zeros((self._isl_H, self._isl_W), np.uint8)
        self.lith = _nn(base[sy0:sy0 + side, sx0:sx0 + side], DS, DS).astype(np.uint8)
        self.lith_path = d / "lithology_painted.tif"
        self.biome_backdrop = biome_backdrop      # 1024 zone codes for context
        self.undo.clear(); self.redo.clear(); self._update_undo_buttons()
        if hasattr(self, "btn_save"):
            self.btn_save.setEnabled(True)
        self.canvas.fit_canvas(DS, DS)
        self._refresh()

    def save(self):
        if self.lith is None:
            return
        full = self._expand_to_bbox(self.lith)
        self._write_painted(full, self._isl_dir / "lithology_painted.tif")
        QMessageBox.information(self, "Saved",
            f"lithology_painted.tif written ({int((full>0).sum())} px).\nRe-bake to apply.")


class IslandStudioWindow(st.OverrideStudioWindow):
    def __init__(self, island=None):
        QMainWindow.__init__(self)
        self.setWindowTitle("Island Studio — biome + lithology")
        self.setMinimumSize(1300, 800); self.resize(1620, 1000)
        self.tabs = QTabWidget()
        self.tab_biome = IslandBiomeTab()
        self.tab_lith = IslandLithoTab()
        self.tabs.addTab(self.tab_biome, "Biome")
        self.tabs.addTab(self.tab_lith, "Lithology")
        self.tab_hydro = None
        top = QWidget(); tl = QVBoxLayout(top); tl.setContentsMargins(0, 0, 0, 0)
        bar = QWidget(); bl = QHBoxLayout(bar); bl.setContentsMargins(8, 4, 8, 4)
        bl.addWidget(QLabel("Island:"))
        self.sel = QComboBox(); self.sel.setMinimumWidth(320)
        bl.addWidget(self.sel); bl.addStretch(1)
        tl.addWidget(bar); tl.addWidget(self.tabs, 1)
        self.setCentralWidget(top)
        self.status = QStatusBar(); self.setStatusBar(self.status)
        self._build_toolbar()
        islands = sorted(d.name for d in MASKS.iterdir() if (d / "height.tif").exists())
        self.sel.addItems(islands)
        self.sel.currentTextChanged.connect(lambda t: t and self.load_island(t))
        if island:
            m = next((i for i in islands if island in i), None)
            if m:
                self.sel.setCurrentText(m)
        if islands:
            self.load_island(self.sel.currentText())

    def load_island(self, name):
        self.status.showMessage(f"loading {name}...")
        QApplication.processEvents()
        self.tab_biome.load_island(name)
        self.tab_lith.load_island(name, self.tab_biome.override)
        self.status.showMessage(f"{name} — paint, Ctrl+Z undo, Ctrl+S save, then re-bake")

    def _load_dialog(self):
        pass                                  # island chosen via the dropdown


def main():
    import argparse
    ap = argparse.ArgumentParser(); ap.add_argument("--island", default=None)
    a = ap.parse_args()
    app = QApplication(sys.argv); app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#0e0e1a"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#d0d0e0"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#13131f"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#d0d0e0"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#1e1e30"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#d0d0e0"))
    app.setPalette(pal)
    win = IslandStudioWindow(a.island); win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
