"""
override_aligner.py — Override Map Alignment & Smoothing Tool
=============================================================
Interactive tool to align override_final.png to the height map,
tune smoothing, and save the corrected result to masks/override.tif.

Launch:
    C:\\Users\\nicho\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe tools/override_aligner.py
"""

import sys
import threading
from pathlib import Path

import numpy as np
from PIL import Image

from PyQt6.QtCore import Qt, QTimer, QRect
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QVBoxLayout, QHBoxLayout, QFormLayout, QSlider,
    QPushButton, QCheckBox, QSizePolicy,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MASKS_DIR    = PROJECT_ROOT / "masks"
OVERRIDE_SRC = PROJECT_ROOT / "override_final.png"
VEC_SRC      = PROJECT_ROOT / "override_vectorized.png"
OUTPUT_TIF   = MASKS_DIR / "override.tif"

DISPLAY_PX   = 800    # preview canvas resolution
TARGET_50K   = 50_000
SRC_PX       = 8_192

VALID_ZONES = [
    0, 10, 20, 30, 35, 40, 50, 55, 60, 70, 80, 90, 100,
    110, 115, 120, 130, 140, 150, 160, 170, 190, 200, 210, 220, 230, 240,
]
ZONE_COLOURS = {
    0:(20,30,60),     10:(180,160,100), 20:(30,110,60),   30:(60,100,130),
    35:(100,130,160), 40:(160,200,140), 50:(200,210,220), 55:(230,240,255),
    60:(80,150,60),   70:(40,140,90),   80:(50,120,80),   90:(180,150,60),
    100:(150,140,110),110:(200,220,160),115:(130,180,150),120:(100,160,80),
    130:(190,180,120),140:(120,100,70), 150:(160,140,90), 160:(20,160,100),
    170:(220,200,100),190:(180,160,80), 200:(160,120,60), 210:(120,140,60),
    220:(0,140,100),  230:(0,120,80),   240:(80,160,200),
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_zone_lut() -> np.ndarray:
    zones = np.array(VALID_ZONES, dtype=np.int32)
    lut   = np.zeros(256, dtype=np.uint8)
    for v in range(256):
        lut[v] = zones[np.argmin(np.abs(zones - v))]
    return lut

_ZONE_LUT = _build_zone_lut()


def _height_rgba(raw: np.ndarray) -> np.ndarray:
    """uint16 raw → (H, W, 3) uint8 terrain colormap."""
    import matplotlib.cm as cm
    gaea_in  = np.array([0, 8000, 17050, 65535], dtype=np.float64)
    mc_y_out = np.array([448, 200, 63, -10],      dtype=np.float64)
    lut  = np.clip(np.interp(np.arange(65536), gaea_in, mc_y_out), -64, 448)
    surf = lut[raw].astype(np.float32)
    norm = np.clip((surf + 10) / 458.0, 0.0, 1.0)
    return (cm.terrain(norm)[..., :3] * 255).astype(np.uint8)


def _sample_override(src_arr: np.ndarray,
                     x_off: int, z_off: int, scale_f: float,
                     display_px: int = DISPLAY_PX,
                     target: int = TARGET_50K) -> np.ndarray:
    """
    Remap src_arr (SRC_PX × SRC_PX) into a display_px × display_px array
    using the current offset and scale, then LUT-snap to valid zone codes.
    order=0 (nearest-neighbour) preserves discrete zone values exactly.
    """
    from scipy.ndimage import map_coordinates

    nominal   = target / SRC_PX          # 50k pixels per src pixel
    eff       = scale_f * nominal        # adjusted pixels per src pixel
    d2w       = target / display_px      # 50k pixels per display pixel

    world_z   = np.arange(display_px, dtype=np.float32) * d2w
    world_x   = np.arange(display_px, dtype=np.float32) * d2w
    over_z    = (world_z - z_off) / eff
    over_x    = (world_x - x_off) / eff

    row_grid  = np.broadcast_to(over_z[:, None], (display_px, display_px))
    col_grid  = np.broadcast_to(over_x[None, :], (display_px, display_px))

    sampled = map_coordinates(
        src_arr.astype(np.float32),
        np.stack([row_grid, col_grid]),
        order=0, mode="constant", cval=0,
    )
    return _ZONE_LUT[np.clip(sampled, 0, 255).astype(np.uint8)]


def _zones_to_rgb(zones: np.ndarray) -> np.ndarray:
    rgb = np.zeros((*zones.shape, 3), dtype=np.uint8)
    for zone, (r, g, b) in ZONE_COLOURS.items():
        rgb[zones == zone] = [r, g, b]
    return rgb


# ---------------------------------------------------------------------------
# Zoomable canvas label
# ---------------------------------------------------------------------------
class ZoomCanvas(QLabel):
    """QLabel that supports scroll-wheel zoom and left-drag pan over a numpy RGB image."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_img: np.ndarray | None = None   # (H, W, 3) uint8
        self._zoom  = 1.0
        self._pan   = [0.5, 0.5]   # centre of view as [0,1] fraction
        self._drag_start: tuple | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_image(self, img: np.ndarray) -> None:
        self._full_img = img
        self._display()

    def _display(self) -> None:
        if self._full_img is None:
            return
        h, w = self._full_img.shape[:2]
        if self._zoom <= 1.0:
            arr = self._full_img
        else:
            cw = max(1, int(w / self._zoom))
            ch = max(1, int(h / self._zoom))
            x0 = int(self._pan[0] * (w - cw))
            y0 = int(self._pan[1] * (h - ch))
            arr = self._full_img[y0:y0+ch, x0:x0+cw]
        img_q = QImage(arr.tobytes(), arr.shape[1], arr.shape[0],
                       arr.shape[1] * 3, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(img_q).scaled(
            self.width(), self.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(pix)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._display()

    def wheelEvent(self, event):
        factor = 1.3 if event.angleDelta().y() > 0 else 1 / 1.3
        self._zoom = max(1.0, min(20.0, self._zoom * factor))
        self._display()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        self._zoom = 1.0
        self._pan  = [0.5, 0.5]
        self._display()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = (event.position().x(), event.position().y())

    def mouseMoveEvent(self, event):
        if self._drag_start is None or self._zoom <= 1.0:
            return
        dx = event.position().x() - self._drag_start[0]
        dy = event.position().y() - self._drag_start[1]
        self._pan[0] = max(0.0, min(1.0, self._pan[0] - dx / self.width()))
        self._pan[1] = max(0.0, min(1.0, self._pan[1] - dy / self.height()))
        self._drag_start = (event.position().x(), event.position().y())
        self._display()

    def mouseReleaseEvent(self, event):
        self._drag_start = None


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class OverrideAlignerWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Override Aligner — Vandir")
        self.resize(860, 980)

        self._height_rgb: np.ndarray | None = None   # (800,800,3) uint8
        self._src_arr:    np.ndarray | None = None   # (8192,8192) uint8 zones, X-flipped

        self._render_timer = QTimer()
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render)

        self._build_ui()
        QTimer.singleShot(100, self._load_data)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(6)

        # Canvas
        self._canvas = ZoomCanvas()
        self._canvas.setMinimumSize(DISPLAY_PX, DISPLAY_PX)
        self._canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._canvas.setStyleSheet("background:#0a1020;")
        self._canvas.setText("Loading…")
        vbox.addWidget(self._canvas, stretch=1)

        # Sliders panel
        panel = QWidget()
        panel.setStyleSheet("background:#16213e; color:#cccccc;")
        form = QFormLayout(panel)
        form.setContentsMargins(10, 8, 10, 8)
        form.setSpacing(6)

        def _slider(min_v, max_v, init):
            sl = QSlider(Qt.Orientation.Horizontal)
            sl.setRange(min_v, max_v)
            sl.setValue(init)
            sl.setTracking(True)
            return sl

        def _val_lbl(text):
            l = QLabel(text)
            l.setStyleSheet("color:#aaddff; min-width:80px;")
            l.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return l

        def _row_widget(sl, lbl):
            w = QWidget(); h = QHBoxLayout(w)
            h.setContentsMargins(0,0,0,0); h.setSpacing(4)
            h.addWidget(sl, stretch=1); h.addWidget(lbl)
            return w

        lbl_style = "color:#8899bb; font-size:10px;"

        # X offset
        self._sl_x  = _slider(-5000, 5000, 0)
        self._lbl_x = _val_lbl("0 px")
        self._sl_x.valueChanged.connect(
            lambda v: (self._lbl_x.setText(f"{v:+d} px"), self._schedule()))
        lx = QLabel("X offset  (50k px)"); lx.setStyleSheet(lbl_style)
        form.addRow(lx, _row_widget(self._sl_x, self._lbl_x))

        # Z offset
        self._sl_z  = _slider(-5000, 5000, 0)
        self._lbl_z = _val_lbl("0 px")
        self._sl_z.valueChanged.connect(
            lambda v: (self._lbl_z.setText(f"{v:+d} px"), self._schedule()))
        lz = QLabel("Z offset  (50k px)"); lz.setStyleSheet(lbl_style)
        form.addRow(lz, _row_widget(self._sl_z, self._lbl_z))

        # Scale
        self._sl_sc  = _slider(80, 120, 100)
        self._lbl_sc = _val_lbl("1.00×")
        self._sl_sc.valueChanged.connect(
            lambda v: (self._lbl_sc.setText(f"{v/100:.2f}×"), self._schedule()))
        lsc = QLabel("Scale  (override size)"); lsc.setStyleSheet(lbl_style)
        form.addRow(lsc, _row_widget(self._sl_sc, self._lbl_sc))

        # Sigma
        self._sl_sig  = _slider(0, 40, 0)
        self._lbl_sig = _val_lbl("0 (off)")
        self._sl_sig.valueChanged.connect(
            lambda v: (self._lbl_sig.setText(f"{v} src px" if v else "0 (off)"),
                       self._schedule()))
        lsig = QLabel("Smooth sigma  (src px)"); lsig.setStyleSheet(lbl_style)
        form.addRow(lsig, _row_widget(self._sl_sig, self._lbl_sig))

        # Opacity
        self._sl_op  = _slider(0, 100, 55)
        self._lbl_op = _val_lbl("55%")
        self._sl_op.valueChanged.connect(
            lambda v: (self._lbl_op.setText(f"{v}%"), self._schedule()))
        lop = QLabel("Override opacity"); lop.setStyleSheet(lbl_style)
        form.addRow(lop, _row_widget(self._sl_op, self._lbl_op))

        # Flip toggles
        chk_style = (
            "QCheckBox { font-size:11px; color:#ccd8ee; padding:2px 6px; }"
            "QCheckBox::indicator { width:14px; height:14px; border:1px solid #4466aa;"
            "background:#1a2a50; border-radius:2px; }"
            "QCheckBox::indicator:checked { background:#3a7aee; border-color:#5599ff; }"
        )
        self._flip_x = QCheckBox("Flip X")
        self._flip_z = QCheckBox("Flip Z")
        self._flip_x.setStyleSheet(chk_style)
        self._flip_z.setStyleSheet(chk_style)
        self._flip_x.setChecked(True)   # X-flip on by default (matches current pipeline)
        self._flip_x.stateChanged.connect(lambda _: self._schedule())
        self._flip_z.stateChanged.connect(lambda _: self._schedule())
        flip_row = QWidget(); fr = QHBoxLayout(flip_row)
        fr.setContentsMargins(0, 0, 0, 0); fr.setSpacing(12)
        fr.addWidget(self._flip_x)
        fr.addWidget(self._flip_z)
        fr.addStretch()
        lflip = QLabel("Axis flip"); lflip.setStyleSheet(lbl_style)
        form.addRow(lflip, flip_row)

        vbox.addWidget(panel)

        # Bottom bar
        bar = QWidget(); bl = QHBoxLayout(bar)
        bl.setContentsMargins(0, 2, 0, 2); bl.setSpacing(8)

        btn = QPushButton("Save → override.tif  (full 50k, ~5 min)")
        btn.setStyleSheet(
            "QPushButton{background:#1e3a70;color:#ddeeff;"
            "border:1px solid #2a5090;padding:4px 14px;font-size:11px;}"
            "QPushButton:hover{background:#2a4e90;}")
        btn.clicked.connect(self._on_save)

        self._status = QLabel("Loading…")
        self._status.setStyleSheet("color:#7799aa; font-size:10px;")

        bl.addWidget(btn)
        bl.addStretch()
        bl.addWidget(self._status)
        vbox.addWidget(bar)

    # ------------------------------------------------------------------
    def _schedule(self):
        self._render_timer.start(60)

    # ------------------------------------------------------------------
    def _load_data(self) -> None:
        self._status.setText("Loading height.tif…")
        QApplication.processEvents()
        try:
            import rasterio
            with rasterio.open(str(MASKS_DIR / "height.tif")) as src:
                step = max(1, src.width // DISPLAY_PX)
                raw  = src.read(1)[::step, ::step][:DISPLAY_PX, :DISPLAY_PX]
            self._height_rgb = _height_rgba(raw.astype(np.uint16))
        except Exception as e:
            self._status.setText(f"height err: {e}"); return

        self._status.setText("Loading override_final.png…")
        QApplication.processEvents()
        try:
            img = Image.open(str(OVERRIDE_SRC))
            ch  = img.split()[0] if img.mode in ("RGBA", "RGB") else img.convert("L")
            arr = np.array(ch, dtype=np.uint8)
            # X-flip to match height.tif orientation
            self._src_arr = arr   # store raw; flips applied at render/save time
        except Exception as e:
            self._status.setText(f"override err: {e}"); return

        self._status.setText("Ready — red lines = zone boundaries")
        self._render()

    # ------------------------------------------------------------------
    def _render(self) -> None:
        if self._height_rgb is None or self._src_arr is None:
            return
        try:
            from scipy.ndimage import gaussian_filter

            src = self._src_arr  # already loaded un-flipped from PNG
            if self._flip_x.isChecked():
                src = np.fliplr(src)
            if self._flip_z.isChecked():
                src = np.flipud(src)
            sigma = self._sl_sig.value()
            if sigma > 0:
                blurred = gaussian_filter(src.astype(np.float32), sigma=sigma)
                src = _ZONE_LUT[np.clip(blurred, 0, 255).astype(np.uint8)]

            zones = _sample_override(
                src,
                x_off   = self._sl_x.value(),
                z_off   = self._sl_z.value(),
                scale_f = self._sl_sc.value() / 100.0,
            )

            ov_rgb = _zones_to_rgb(zones)

            # Blend
            op  = self._sl_op.value() / 100.0
            out = (self._height_rgb * (1 - op) + ov_rgb * op).clip(0, 255).astype(np.uint8)

            # Zone boundary overlay (red lines)
            bnd = np.zeros((DISPLAY_PX, DISPLAY_PX), dtype=bool)
            bnd[:-1, :] |= zones[:-1, :] != zones[1:, :]
            bnd[:, :-1] |= zones[:, :-1] != zones[:, 1:]
            out[bnd] = [220, 50, 50]

            self._canvas.set_image(out)

            self._status.setText(
                f"X={self._sl_x.value():+d}  Z={self._sl_z.value():+d}  "
                f"scale={self._sl_sc.value()/100:.2f}  sigma={self._sl_sig.value()}")

        except Exception as e:
            self._status.setText(f"Render error: {e}")
            import traceback; traceback.print_exc()

    # ------------------------------------------------------------------
    def _on_save(self) -> None:
        """Write corrected override.tif at full 50k resolution (background thread)."""
        self._status.setText("Saving in background — do not close…")
        QApplication.processEvents()

        x_off   = self._sl_x.value()
        z_off   = self._sl_z.value()
        scale_f = self._sl_sc.value() / 100.0
        sigma   = self._sl_sig.value()
        do_flip_x = self._flip_x.isChecked()
        do_flip_z = self._flip_z.isChecked()

        def _worker():
            try:
                import rasterio, os
                from rasterio.windows import Window
                from scipy.ndimage import gaussian_filter, map_coordinates

                # Composite vectorized borders over base fill (same as upscale script)
                base_ch = Image.open(str(OVERRIDE_SRC))
                base_arr = np.array(
                    base_ch.split()[0] if base_ch.mode in ("RGBA","RGB")
                    else base_ch.convert("L"), dtype=np.uint8)

                vec_ch   = Image.open(str(VEC_SRC))
                vec_arr  = np.array(
                    vec_ch.split()[0] if vec_ch.mode in ("RGBA","RGB")
                    else vec_ch.convert("L"), dtype=np.uint8)

                src_arr = np.where(vec_arr > 0, vec_arr, base_arr).astype(np.uint8)
                if do_flip_x:
                    src_arr = np.fliplr(src_arr)
                if do_flip_z:
                    src_arr = np.flipud(src_arr)

                if sigma > 0:
                    blurred = gaussian_filter(src_arr.astype(np.float32), sigma=sigma)
                    src_arr = _ZONE_LUT[np.clip(blurred, 0, 255).astype(np.uint8)]

                src_f   = src_arr.astype(np.float32)
                nominal = TARGET_50K / SRC_PX
                eff     = scale_f * nominal
                CHUNK   = 256

                profile = dict(
                    driver="GTiff", height=TARGET_50K, width=TARGET_50K,
                    count=1, dtype=np.uint8, compress="deflate",
                    tiled=True, blockxsize=512, blockysize=512, bigtiff="YES",
                )
                os.makedirs(str(MASKS_DIR), exist_ok=True)

                with rasterio.open(str(OUTPUT_TIF), "w", **profile) as dst:
                    for row_out in range(0, TARGET_50K, CHUNK):
                        ch = min(CHUNK, TARGET_50K - row_out)
                        wz = np.arange(row_out, row_out + ch, dtype=np.float32)
                        wx = np.arange(TARGET_50K,             dtype=np.float32)
                        oz = (wz - z_off) / eff
                        ox = (wx - x_off) / eff
                        rg = np.broadcast_to(oz[:, None], (ch, TARGET_50K))
                        cg = np.broadcast_to(ox[None, :], (ch, TARGET_50K))
                        sampled = map_coordinates(
                            src_f, np.stack([rg, cg]),
                            order=0, mode="constant", cval=0)
                        out = _ZONE_LUT[np.clip(sampled, 0, 255).astype(np.uint8)]
                        dst.write(out[np.newaxis], window=Window(0, row_out, TARGET_50K, ch))
                        pct = (row_out + ch) / TARGET_50K * 100
                        print(f"\r  {pct:.1f}%", end="", flush=True)

                sz = OUTPUT_TIF.stat().st_size / 1024 / 1024
                print(f"\nSaved {OUTPUT_TIF}  ({sz:.1f} MB)")
                self._status.setText(f"Saved OK — {sz:.1f} MB → {OUTPUT_TIF.name}")

            except Exception as e:
                import traceback; traceback.print_exc()
                self._status.setText(f"Save error: {e}")

        threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = OverrideAlignerWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
