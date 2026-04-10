#!/usr/bin/env python3
"""
override_aligner2.py — Drag-to-align Override Tool
===================================================
LEFT-drag  : move override layer (fine sensitivity at high zoom)
RIGHT-drag : pan the view (zoom > 1×)
Scroll     : nothing (use zoom dropdown)
Double-click: reset view pan to centre

Zoom dropdown (top-right): 1× / 2× / 4× / 8×
  At 4× you see one quarter of the world at 4× detail.
  Drag sensitivity scales with zoom so 1 screen-pixel always moves
  the same fraction of a biome boundary regardless of zoom level.

Buttons: Flip X / Flip Z / Rotate CW / CCW / Reset offset
Scale slider: 1.01 = override covers 1% more world.
Opacity slider: blend strength.

Save → override.tif  runs the full 50k rebuild with jitter (same pipeline
as upscale_override_vectorized.py).  Status bar shows live params.

Launch:
    C:\\Users\\nicho\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe tools/override_aligner2.py
"""

import sys, os, threading
from pathlib import Path

import numpy as np
from PIL import Image

from PyQt6.QtCore    import Qt, QTimer
from PyQt6.QtGui     import QImage, QPixmap, QCursor
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QVBoxLayout, QHBoxLayout, QSlider, QPushButton,
    QCheckBox, QSizePolicy, QGroupBox, QComboBox,
)

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
MASKS_DIR    = PROJECT_ROOT / "masks"
OVERRIDE_SRC = PROJECT_ROOT / "override_final.png"
VEC_SRC      = PROJECT_ROOT / "override_vectorized.png"
OUTPUT_TIF   = MASKS_DIR / "override.tif"

DISPLAY_PX  = 800
TARGET      = 50_000
SRC_PX      = 8_192
CACHE_SIZE  = 2000      # height downsampled to 2000×2000 in RAM (~8 MB)

# ── Zone data ─────────────────────────────────────────────────────────────────
VALID_ZONES = [
    0, 10, 20, 30, 35, 40, 50, 55, 60, 70, 80, 90, 100,
    110, 115, 120, 130, 140, 150, 160, 170, 190, 200, 210, 220, 230, 240,
]
ZONE_COLOURS = {
      0:(20,30,60),    10:(180,160,100),  20:(30,110,60),   30:(60,100,130),
     35:(100,130,160), 40:(160,200,140),  50:(200,210,220), 55:(230,240,255),
     60:(80,150,60),   70:(40,140,90),    80:(50,120,80),   90:(180,150,60),
    100:(150,140,110),110:(200,220,160), 115:(130,180,150),120:(100,160,80),
    130:(190,180,120),140:(120,100,70),  150:(160,140,90), 160:(20,160,100),
    170:(220,200,100),190:(180,160,80),  200:(160,120,60), 210:(120,140,60),
    220:(0,140,100),  230:(0,120,80),    240:(80,160,200),
}

JITTER_PASSES = 3
JITTER_PROB   = 0.5
JITTER_SEED   = 42

# ── Helpers ───────────────────────────────────────────────────────────────────
def _build_lut() -> np.ndarray:
    zones = np.array(VALID_ZONES, dtype=np.int32)
    lut   = np.zeros(256, dtype=np.uint8)
    for v in range(256):
        lut[v] = zones[np.argmin(np.abs(zones - v))]
    return lut

_LUT = _build_lut()

# Height LUT: Gaea uint16 raw → MC Y int16
def _build_height_lut() -> np.ndarray:
    # Normal polarity — matches step0_output.json and confirmed Session 13.
    gaea_in  = np.array([0,   17050, 45000, 65496], dtype=np.float64)
    mc_y_out = np.array([-64,    63,   200,   448], dtype=np.float64)
    lut = np.clip(np.interp(np.arange(65536), gaea_in, mc_y_out), -64, 448)
    return lut.astype(np.int16)

_HEIGHT_LUT = _build_height_lut()


def _height_rgba(mc_y: np.ndarray) -> np.ndarray:
    import matplotlib.cm as cm
    y = mc_y.astype(np.float32)
    ocean_norm = np.clip((y - (-64.0)) / (63.0 - (-64.0)), 0.0, 1.0) * 0.22
    land_norm  = 0.22 + np.clip((y - 63.0) / (448.0 - 63.0), 0.0, 1.0) * 0.78
    norm = np.where(y < 63.0, ocean_norm, land_norm)
    return (cm.terrain(norm)[..., :3] * 255).astype(np.uint8)


def _contour_mask(mc_y: np.ndarray, threshold: int) -> np.ndarray:
    """Boolean mask of display pixels that sit on the contour at mc_y == threshold."""
    above = mc_y >= threshold
    mask  = np.zeros(mc_y.shape, dtype=bool)
    h_cross = above[:-1, :] != above[1:, :]
    v_cross = above[:, :-1] != above[:, 1:]
    mask[:-1, :] |= h_cross
    mask[1:,  :] |= h_cross
    mask[:, :-1] |= v_cross
    mask[:, 1:]  |= v_cross
    return mask


def _zones_to_rgb(zones: np.ndarray) -> np.ndarray:
    rgb = np.zeros((*zones.shape, 3), dtype=np.uint8)
    for zone, (r, g, b) in ZONE_COLOURS.items():
        rgb[zones == zone] = [r, g, b]
    return rgb


def _apply_jitter(arr: np.ndarray) -> np.ndarray:
    rng = np.random.default_rng(JITTER_SEED)
    arr = arr.copy()
    for _ in range(JITTER_PASSES):
        n_up    = np.roll(arr,  1, axis=0)
        n_down  = np.roll(arr, -1, axis=0)
        n_left  = np.roll(arr,  1, axis=1)
        n_right = np.roll(arr, -1, axis=1)
        is_bnd  = (n_up!=arr)|(n_down!=arr)|(n_left!=arr)|(n_right!=arr)
        dirs    = np.stack([n_up, n_down, n_left, n_right], axis=-1)
        choice  = rng.integers(0, 4, size=arr.shape)
        h, w    = arr.shape
        chosen  = dirs[np.arange(h)[:,None], np.arange(w)[None,:], choice]
        swap    = is_bnd & (rng.random(arr.shape) < JITTER_PROB)
        arr     = np.where(swap, chosen, arr).astype(np.uint8)
    return arr


# ── Alignment + view state ────────────────────────────────────────────────────
class AlignState:
    def __init__(self):
        # Override transform
        self.flip_x   = True
        self.flip_z   = False
        self.rotation = 0          # 0 / 90 / 180 / 270
        self.scale    = 1.00
        self.x_off    = 0.0       # source-pixel offset
        self.z_off    = 0.0
        self.opacity  = 0.55
        # View
        self.view_zoom = 1          # 1, 2, 4, 8
        self.view_cx   = 25000.0   # world-coord centre of zoomed view
        self.view_cz   = 25000.0

    def effective_src(self) -> int:
        return max(1, int(round(SRC_PX / self.scale)))

    def transform(self, arr: np.ndarray) -> np.ndarray:
        if self.flip_x:   arr = np.fliplr(arr)
        if self.flip_z:   arr = np.flipud(arr)
        if self.rotation: arr = np.rot90(arr, self.rotation // 90)
        return arr

    def src_per_display(self) -> float:
        """Source pixels per display pixel at current zoom."""
        return self.effective_src() / (DISPLAY_PX * self.view_zoom)

    def world_per_display(self) -> float:
        """World pixels per display pixel at current zoom."""
        return TARGET / (DISPLAY_PX * self.view_zoom)


# ── Drag canvas ───────────────────────────────────────────────────────────────
class DragCanvas(QLabel):
    def __init__(self, state: AlignState, on_update, parent=None):
        super().__init__(parent)
        self._state     = state
        self._on_update = on_update
        self._ldrag     = None     # left-drag start pos (override move)
        self._rdrag     = None     # right-drag start pos (view pan)
        self._full: np.ndarray | None = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        self.setMouseTracking(True)

    def set_image(self, img: np.ndarray):
        self._full = img
        self._blit()

    def _blit(self):
        if self._full is None:
            return
        arr = self._full
        qi  = QImage(arr.tobytes(), arr.shape[1], arr.shape[0],
                     arr.shape[1]*3, QImage.Format.Format_RGB888)
        pix = QPixmap.fromImage(qi).scaled(
            self.width(), self.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(pix)

    def resizeEvent(self, e):
        super().resizeEvent(e); self._blit()

    def mouseDoubleClickEvent(self, e):
        # Reset view centre to world centre
        self._state.view_cx = 25000.0
        self._state.view_cz = 25000.0
        self._on_update()

    def mousePressEvent(self, e):
        pos = (e.position().x(), e.position().y())
        if e.button() == Qt.MouseButton.LeftButton:
            self._ldrag = pos
        elif e.button() == Qt.MouseButton.RightButton:
            self._rdrag = pos

    def mouseMoveEvent(self, e):
        pos = (e.position().x(), e.position().y())

        if self._ldrag is not None:
            dx = pos[0] - self._ldrag[0]
            dy = pos[1] - self._ldrag[1]
            self._ldrag = pos
            spd = self._state.src_per_display()
            self._state.x_off -= dx * spd
            self._state.z_off -= dy * spd
            self._on_update()

        if self._rdrag is not None:
            dx = pos[0] - self._rdrag[0]
            dy = pos[1] - self._rdrag[1]
            self._rdrag = pos
            wpd = self._state.world_per_display()
            self._state.view_cx = max(0.0, min(TARGET, self._state.view_cx - dx * wpd))
            self._state.view_cz = max(0.0, min(TARGET, self._state.view_cz - dy * wpd))
            self._on_update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:  self._ldrag = None
        if e.button() == Qt.MouseButton.RightButton: self._rdrag = None


# ── Main window ───────────────────────────────────────────────────────────────
class OverrideAligner2(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Override Aligner 2 — L-drag: move override | R-drag: pan view")
        self.resize(940, 1060)

        self._state          = AlignState()
        self._height_cache: np.ndarray | None = None   # (CACHE_SIZE,CACHE_SIZE) uint16
        self._composite:    np.ndarray | None = None   # (8192,8192) uint8 zone values

        self._render_timer = QTimer()
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render)

        self._build_ui()
        QTimer.singleShot(100, self._load_data)

    # ── UI ────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        vbox = QVBoxLayout(root)
        vbox.setContentsMargins(8,8,8,8)
        vbox.setSpacing(6)

        self._canvas = DragCanvas(self._state, self._schedule)
        self._canvas.setMinimumSize(DISPLAY_PX, DISPLAY_PX)
        self._canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._canvas.setStyleSheet("background:#0a1020;")
        self._canvas.setText("Loading…")
        vbox.addWidget(self._canvas, stretch=1)

        ctrl = QWidget()
        ctrl.setStyleSheet("background:#16213e; color:#ccc;")
        hrow = QHBoxLayout(ctrl)
        hrow.setContentsMargins(8,6,8,6)
        hrow.setSpacing(10)

        grp_style = ("QGroupBox{color:#8899bb;font-size:10px;border:1px solid #2a3a60;"
                     "margin-top:6px;padding-top:4px;}"
                     "QGroupBox::title{subcontrol-origin:margin;left:6px;}")
        chk_style = ("QCheckBox{font-size:11px;color:#ccd8ee;padding:2px 6px;}"
                     "QCheckBox::indicator{width:13px;height:13px;border:1px solid #4466aa;"
                     "background:#1a2a50;border-radius:2px;}"
                     "QCheckBox::indicator:checked{background:#3a7aee;border-color:#5599ff;}")
        btn_style = ("QPushButton{background:#1e3a70;color:#dde;border:1px solid #2a5090;"
                     "padding:3px 10px;font-size:11px;}"
                     "QPushButton:hover{background:#2a4e90;}")

        def _btn(label, cb):
            b = QPushButton(label); b.setStyleSheet(btn_style); b.clicked.connect(cb)
            return b

        # ── Axes group ──
        g_axes = QGroupBox("Axes"); g_axes.setStyleSheet(grp_style)
        gl_a = QHBoxLayout(g_axes); gl_a.setSpacing(4)
        self._chk_fx = QCheckBox("Flip X"); self._chk_fx.setStyleSheet(chk_style)
        self._chk_fz = QCheckBox("Flip Z"); self._chk_fz.setStyleSheet(chk_style)
        self._chk_fx.setChecked(self._state.flip_x)
        self._chk_fz.setChecked(self._state.flip_z)
        self._chk_fx.stateChanged.connect(lambda v: self._toggle('flip_x', bool(v)))
        self._chk_fz.stateChanged.connect(lambda v: self._toggle('flip_z', bool(v)))
        gl_a.addWidget(self._chk_fx)
        gl_a.addWidget(self._chk_fz)
        gl_a.addWidget(_btn("↺ CCW", self._rot_ccw))
        gl_a.addWidget(_btn("↻ CW",  self._rot_cw))
        hrow.addWidget(g_axes)

        # ── Offset group ──
        g_off = QGroupBox("Offset"); g_off.setStyleSheet(grp_style)
        gl_o = QHBoxLayout(g_off)
        gl_o.addWidget(_btn("Reset offset", self._reset_offset))
        hrow.addWidget(g_off)

        # ── Scale group ──
        g_sc = QGroupBox("Scale"); g_sc.setStyleSheet(grp_style)
        gl_s = QHBoxLayout(g_sc)
        self._sl_scale = QSlider(Qt.Orientation.Horizontal)
        self._sl_scale.setRange(80, 120); self._sl_scale.setValue(100)
        self._lbl_scale = QLabel("1.00×")
        self._lbl_scale.setStyleSheet("color:#aaddff;min-width:42px;")
        self._sl_scale.valueChanged.connect(self._on_scale)
        gl_s.addWidget(self._sl_scale); gl_s.addWidget(self._lbl_scale)
        hrow.addWidget(g_sc)

        # ── Opacity group ──
        g_op = QGroupBox("Opacity"); g_op.setStyleSheet(grp_style)
        gl_op = QHBoxLayout(g_op)
        self._sl_op = QSlider(Qt.Orientation.Horizontal)
        self._sl_op.setRange(0, 100); self._sl_op.setValue(55)
        self._lbl_op = QLabel("55%")
        self._lbl_op.setStyleSheet("color:#aaddff;min-width:32px;")
        self._sl_op.valueChanged.connect(
            lambda v: (self._lbl_op.setText(f"{v}%"),
                       setattr(self._state, 'opacity', v/100),
                       self._schedule()))
        gl_op.addWidget(self._sl_op); gl_op.addWidget(self._lbl_op)
        hrow.addWidget(g_op)

        # ── Zoom group ──
        g_zm = QGroupBox("View Zoom"); g_zm.setStyleSheet(grp_style)
        gl_z = QHBoxLayout(g_zm)
        self._zoom_cb = QComboBox()
        self._zoom_cb.setStyleSheet(
            "QComboBox{background:#1a2a50;color:#ccd8ee;border:1px solid #4466aa;"
            "padding:2px 6px;font-size:11px;}"
            "QComboBox QAbstractItemView{background:#1a2a50;color:#ccd8ee;}")
        for label in ("1×", "2×", "4×", "8×"):
            self._zoom_cb.addItem(label)
        self._zoom_cb.currentIndexChanged.connect(self._on_zoom)
        gl_z.addWidget(self._zoom_cb)
        hrow.addWidget(g_zm)

        vbox.addWidget(ctrl)

        # ── Contour row ──
        ctr = QWidget()
        ctr.setStyleSheet("background:#16213e; color:#ccc;")
        crow = QHBoxLayout(ctr)
        crow.setContentsMargins(8,4,8,4); crow.setSpacing(10)

        g_ctr = QGroupBox("Height Contours"); g_ctr.setStyleSheet(grp_style)
        gl_ctr = QHBoxLayout(g_ctr); gl_ctr.setSpacing(8)

        self._chk_sea = QCheckBox("Sea level  Y=63")
        self._chk_sea.setStyleSheet(chk_style)
        self._chk_sea.setChecked(True)
        self._chk_sea.stateChanged.connect(lambda _: self._schedule())
        gl_ctr.addWidget(self._chk_sea)

        self._chk_custom_ctr = QCheckBox("Custom Y:")
        self._chk_custom_ctr.setStyleSheet(chk_style)
        self._chk_custom_ctr.stateChanged.connect(lambda _: self._schedule())
        gl_ctr.addWidget(self._chk_custom_ctr)

        self._sl_ctr = QSlider(Qt.Orientation.Horizontal)
        self._sl_ctr.setRange(-64, 448)
        self._sl_ctr.setValue(100)
        self._sl_ctr.setMinimumWidth(200)
        self._lbl_ctr = QLabel("Y=100")
        self._lbl_ctr.setStyleSheet("color:#ffee88;min-width:48px;")
        self._sl_ctr.valueChanged.connect(
            lambda v: (self._lbl_ctr.setText(f"Y={v}"), self._schedule()))
        gl_ctr.addWidget(self._sl_ctr)
        gl_ctr.addWidget(self._lbl_ctr)
        gl_ctr.addStretch()

        crow.addWidget(g_ctr)
        vbox.addWidget(ctr)

        # ── Bottom bar ──
        bar = QWidget()
        bl  = QHBoxLayout(bar)
        bl.setContentsMargins(0,2,0,2); bl.setSpacing(8)
        save_btn = QPushButton("Save → override.tif  (full 50k + jitter, ~5 min)")
        save_btn.setStyleSheet(
            "QPushButton{background:#1e3a70;color:#ddeeff;border:1px solid #2a5090;"
            "padding:4px 14px;font-size:11px;}"
            "QPushButton:hover{background:#2a4e90;}")
        save_btn.clicked.connect(self._on_save)
        self._status = QLabel("Loading…")
        self._status.setStyleSheet("color:#7799aa;font-size:10px;")
        bl.addWidget(save_btn); bl.addStretch(); bl.addWidget(self._status)
        vbox.addWidget(bar)

    # ── Callbacks ─────────────────────────────────────────────────────────────
    def _schedule(self): self._render_timer.start(50)

    def _toggle(self, attr, val):
        setattr(self._state, attr, val); self._schedule()

    def _rot_cw(self):
        self._state.rotation = (self._state.rotation + 90) % 360; self._schedule()

    def _rot_ccw(self):
        self._state.rotation = (self._state.rotation - 90) % 360; self._schedule()

    def _reset_offset(self):
        self._state.x_off = 0.0; self._state.z_off = 0.0; self._schedule()

    def _on_scale(self, v):
        self._state.scale = v / 100.0
        self._lbl_scale.setText(f"{v/100:.2f}×")
        self._schedule()

    def _on_zoom(self, idx):
        self._state.view_zoom = [1, 2, 4, 8][idx]
        self._schedule()

    # ── Data load ─────────────────────────────────────────────────────────────
    def _load_data(self):
        self._status.setText("Loading height.tif…")
        QApplication.processEvents()
        try:
            import rasterio
            with rasterio.open(str(MASKS_DIR / "height.tif")) as src:
                step = max(1, src.width // CACHE_SIZE)
                raw  = src.read(1)[::step, ::step][:CACHE_SIZE, :CACHE_SIZE]
            self._height_cache = raw.astype(np.uint16)
            print(f"Height cache: {self._height_cache.shape}  step={step}")
        except Exception as e:
            self._status.setText(f"height err: {e}"); return

        self._status.setText("Loading override composite…")
        QApplication.processEvents()
        try:
            base = Image.open(str(OVERRIDE_SRC))
            ba   = np.array(base.split()[0] if base.mode in ("RGBA","RGB")
                            else base.convert("L"), dtype=np.uint8)
            vec  = Image.open(str(VEC_SRC))
            va   = np.array(vec.split()[0] if vec.mode in ("RGBA","RGB")
                            else vec.convert("L"), dtype=np.uint8)
            self._composite = np.where(va > 0, va, ba).astype(np.uint8)
            print(f"Composite: {self._composite.shape}")
        except Exception as e:
            self._status.setText(f"override err: {e}"); return

        self._status.setText(
            "Ready — L-drag: move override | R-drag: pan view | "
            "zoom dropdown top-right | dbl-click: reset view")
        self._render()

    # ── Render ────────────────────────────────────────────────────────────────
    def _render(self):
        if self._height_cache is None or self._composite is None:
            return
        try:
            from scipy.ndimage import map_coordinates, zoom as nd_zoom

            s   = self._state
            Z   = s.view_zoom
            eff = s.effective_src()

            # ── World region for this view ──────────────────────────────────
            half_world = TARGET / (2.0 * Z)
            wx0 = max(0.0, s.view_cx - half_world)
            wz0 = max(0.0, s.view_cz - half_world)
            # clamp so we don't go past world edge
            wx0 = min(wx0, TARGET - 2 * half_world)
            wz0 = min(wz0, TARGET - 2 * half_world)

            # ── Height: crop + resize from CACHE_SIZE×CACHE_SIZE ───────────
            c_scale = CACHE_SIZE / TARGET          # cache pixels per world pixel
            cx0 = int(wx0 * c_scale);  cx1 = int((wx0 + 2*half_world) * c_scale) + 1
            cz0 = int(wz0 * c_scale);  cz1 = int((wz0 + 2*half_world) * c_scale) + 1
            cx0, cx1 = max(0,cx0), min(CACHE_SIZE, cx1)
            cz0, cz1 = max(0,cz0), min(CACHE_SIZE, cz1)

            raw_crop = self._height_cache[cz0:cz1, cx0:cx1].astype(np.float32)
            zh = DISPLAY_PX / raw_crop.shape[0]
            zw = DISPLAY_PX / raw_crop.shape[1]
            raw_disp = nd_zoom(raw_crop, (zh, zw), order=1)
            raw_disp = np.clip(raw_disp, 0, 65535).astype(np.uint16)
            height_rgb = _height_rgba(_HEIGHT_LUT[raw_disp])

            # ── Override: map_coordinates into transformed+cropped source ──
            src = s.transform(self._composite)
            src = src[:eff, :eff]

            # Source coord of world pixel at (wx0, wz0):
            #   src_x = wx  * (eff / TARGET) + x_off
            src_scale = eff / TARGET
            col0 = wx0 * src_scale + s.x_off
            row0 = wz0 * src_scale + s.z_off
            spd  = s.src_per_display()   # source px per display px at this zoom

            cols = col0 + np.arange(DISPLAY_PX, dtype=np.float32) * spd
            rows = row0 + np.arange(DISPLAY_PX, dtype=np.float32) * spd
            rg   = np.broadcast_to(rows[:,None], (DISPLAY_PX, DISPLAY_PX))
            cg   = np.broadcast_to(cols[None,:], (DISPLAY_PX, DISPLAY_PX))

            sampled = map_coordinates(src.astype(np.float32), np.stack([rg, cg]),
                                      order=0, mode="constant", cval=0)
            zones   = _LUT[np.clip(sampled, 0, 255).astype(np.uint8)]
            ov_rgb  = _zones_to_rgb(zones)

            op  = s.opacity
            out = (height_rgb*(1-op) + ov_rgb*op).clip(0,255).astype(np.uint8)

            # Red zone boundary lines
            bnd = np.zeros((DISPLAY_PX, DISPLAY_PX), dtype=bool)
            bnd[:-1,:] |= zones[:-1,:] != zones[1:,:]
            bnd[:,:-1] |= zones[:,:-1] != zones[:,1:]
            out[bnd] = [220, 50, 50]

            # Height contour overlays (drawn from raw_disp so they're always sharp)
            mc_y_disp = _HEIGHT_LUT[raw_disp]
            if self._chk_sea.isChecked():
                out[_contour_mask(mc_y_disp, 63)]  = [0, 230, 255]   # cyan = sea level
            if self._chk_custom_ctr.isChecked():
                out[_contour_mask(mc_y_disp, self._sl_ctr.value())] = [255, 230, 0]  # yellow

            self._canvas.set_image(out)
            self._status.setText(
                f"zoom={Z}×  view=({s.view_cx:.0f},{s.view_cz:.0f})  "
                f"flip_x={s.flip_x} flip_z={s.flip_z} rot={s.rotation}°  "
                f"scale={s.scale:.3f}  x_off={s.x_off:.1f} z_off={s.z_off:.1f} src-px")

        except Exception as e:
            self._status.setText(f"Render error: {e}")
            import traceback; traceback.print_exc()

    # ── Save ──────────────────────────────────────────────────────────────────
    def _on_save(self):
        self._status.setText("Saving — do not close…")
        QApplication.processEvents()

        s        = self._state
        flip_x   = s.flip_x
        flip_z   = s.flip_z
        rotation = s.rotation
        scale    = s.scale
        x_off    = s.x_off
        z_off    = s.z_off

        def _worker():
            try:
                import rasterio
                from rasterio.windows import Window
                from scipy.ndimage import map_coordinates

                base = Image.open(str(OVERRIDE_SRC))
                ba   = np.array(base.split()[0] if base.mode in ("RGBA","RGB")
                                else base.convert("L"), dtype=np.uint8)
                vec  = Image.open(str(VEC_SRC))
                va   = np.array(vec.split()[0] if vec.mode in ("RGBA","RGB")
                                else vec.convert("L"), dtype=np.uint8)
                composite = np.where(va > 0, va, ba).astype(np.uint8)

                if flip_x:   composite = np.fliplr(composite)
                if flip_z:   composite = np.flipud(composite)
                if rotation: composite = np.rot90(composite, rotation // 90)

                eff_px    = max(1, int(round(SRC_PX / scale)))
                composite = composite[:eff_px, :eff_px]
                print(f"Scale {scale:.3f}: using {eff_px}×{eff_px} src px")

                print("Applying jitter…")
                composite = _apply_jitter(composite)
                src_f     = composite.astype(np.float32)
                src_h, src_w = composite.shape
                sppop     = src_w / TARGET   # source px per output px

                profile = dict(
                    driver="GTiff", height=TARGET, width=TARGET,
                    count=1, dtype=np.uint8, compress="deflate",
                    tiled=True, blockxsize=512, blockysize=512, bigtiff="YES",
                )
                os.makedirs(str(MASKS_DIR), exist_ok=True)

                CHUNK = 256
                with rasterio.open(str(OUTPUT_TIF), "w", **profile) as dst:
                    row_out = 0
                    while row_out < TARGET:
                        ch   = min(CHUNK, TARGET - row_out)
                        rows = np.arange(row_out, row_out+ch, dtype=np.float32) * sppop + z_off
                        cols = np.arange(TARGET,             dtype=np.float32) * sppop + x_off
                        rg   = np.broadcast_to(rows[:,None], (ch, TARGET))
                        cg   = np.broadcast_to(cols[None,:], (ch, TARGET))
                        sampled = map_coordinates(src_f, np.stack([rg, cg]),
                                                  order=1, mode="constant", cval=0)
                        out = _LUT[np.clip(sampled, 0, 255).astype(np.uint8)]
                        dst.write(out[np.newaxis], window=Window(0, row_out, TARGET, ch))
                        row_out += ch
                        print(f"\r  {row_out/TARGET*100:.1f}%", end="", flush=True)

                sz = OUTPUT_TIF.stat().st_size / 1024/1024
                print(f"\nSaved {OUTPUT_TIF}  ({sz:.1f} MB)")
                self._status.setText(
                    f"Saved OK — {sz:.1f} MB  |  "
                    f"flip_x={flip_x} flip_z={flip_z} rot={rotation}° "
                    f"scale={scale:.3f} x={x_off:.1f} z={z_off:.1f}")
            except Exception as e:
                import traceback; traceback.print_exc()
                self._status.setText(f"Save error: {e}")

        threading.Thread(target=_worker, daemon=True).start()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = OverrideAligner2()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
