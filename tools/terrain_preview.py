"""
terrain_preview.py — Vandir Terrain Preview Tool
=================================================
Standalone PyQt6 diagnostic tool for the Vandir pipeline.

Launch:
    C:\\Users\\nicho\\AppData\\Local\\Python\\bin\\python.exe tools/terrain_preview.py --tile-x 32 --tile-z 2

Build status:
    Step 1 — Window scaffold (three-panel layout)    [DONE]
    Step 2 — Map view single tile (surface_height)   [DONE]
    Step 3 — Map view 3x3 tile grid                  [DONE]
    Step 4 — Cross-section static profile row 256    [DONE]
    Step 5 — Cross-section click-to-update from map  [DONE]
    Step 6 — Control panel Tab C (ocean/depth)        [DONE]
    Step 7 — Control panel Tab A (spline editor)      [DONE]
    Step 8 — Control panel Tab B (surface blocks)     [DONE]
    Step 9 — Render modes (height/biome/slope/blocks) [DONE]
    Step 10 — Mouse-wheel zoom + left-drag pan        [DONE]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, Tuple

# Ensure project root is on sys.path so core modules are importable
_PROJECT_ROOT_STR = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT_STR not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT_STR)

import numpy as np

from PyQt6.QtCore import Qt, QTimer, QPoint, QRect
from PyQt6.QtGui import QImage, QPixmap, QPainter, QColor, QFont, QPen
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QSplitter, QLabel, QSizePolicy, QVBoxLayout, QHBoxLayout,
    QTabWidget, QSlider, QFormLayout, QGroupBox,
    QPushButton, QTableWidget, QTableWidgetItem, QComboBox, QSpinBox,
)

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT  = Path(__file__).resolve().parent.parent
MASKS_DIR     = PROJECT_ROOT / "masks"
CONFIG_PATH   = PROJECT_ROOT / "config" / "thresholds.json"

OVERRIDE_TIF = MASKS_DIR / "override.tif"

TILE_SIZE    = 512
DISPLAY_SIZE = 256   # each tile downsampled to 256×256 for display
GRID_N       = 3     # 3×3 tile grid
CANVAS_SIZE  = DISPLAY_SIZE * GRID_N   # 768×768


# ---------------------------------------------------------------------------
# Height LUT  (Gaea 16-bit → MC Y)
# ---------------------------------------------------------------------------
_DEFAULT_GAEA_IN  = [0, 17050, 45000, 65496]
_DEFAULT_MC_Y_OUT = [-64,   63,   200,   448]


def _build_lut() -> np.ndarray:
    # NORMAL polarity: LOW raw = deep ocean, HIGH raw = high terrain.
    # Reads terrain_spline from config/thresholds.json at module load so the
    # cross-section panel reflects the current spline (matches the pipeline's
    # column_generator behavior post-S84). Falls back to legacy hardcoded
    # values if config can't be read.
    gaea_in_list, mc_y_out_list = _DEFAULT_GAEA_IN, _DEFAULT_MC_Y_OUT
    try:
        import json as _json
        from pathlib import Path as _Path
        cfg_path = _Path(__file__).resolve().parent.parent / "config" / "thresholds.json"
        if cfg_path.exists():
            with open(cfg_path) as _f:
                _cfg = _json.load(_f)
            _sp = _cfg.get("terrain_spline", {})
            gaea_in_list  = _sp.get("gaea_in",  _DEFAULT_GAEA_IN)
            mc_y_out_list = _sp.get("mc_y_out", _DEFAULT_MC_Y_OUT)
    except Exception:
        pass
    gaea_in  = np.array(gaea_in_list,  dtype=np.float64)
    mc_y_out = np.array(mc_y_out_list, dtype=np.float64)
    lut = np.interp(np.arange(65536, dtype=np.float64), gaea_in, mc_y_out)
    return np.clip(lut, -64, 703).astype(np.int16)

_LUT = _build_lut()


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------
import json as _json

def _load_thresholds() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return _json.load(f)

def _save_thresholds(data: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        _json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Colormap helpers
# ---------------------------------------------------------------------------
def _surface_height_rgba(surface_y: np.ndarray) -> np.ndarray:
    """Convert (H, W) int16 MC Y array → (H, W, 4) uint8 RGBA using terrain colormap.

    Piecewise normalisation anchored at sea level (Y=63 → norm=0.22) so that
    ocean/underwater terrain maps to the blue range [0.0, 0.22] and land maps
    to the sand/green/brown range [0.22, 1.0].  Previously the linear mapping
    placed sea level at norm≈0.16, making low-lying land look like deep ocean.
    """
    import matplotlib.cm as cm
    y = surface_y.astype(np.float32)
    # Below sea level: Y in [-64, 63] → norm in [0.0, 0.22]
    ocean_norm = np.clip((y - (-64.0)) / (63.0 - (-64.0)), 0.0, 1.0) * 0.22
    # Above sea level: Y in [63, 703] → norm in [0.22, 1.0]
    land_norm  = 0.22 + np.clip((y - 63.0) / (703.0 - 63.0), 0.0, 1.0) * 0.78
    norm = np.where(y < 63.0, ocean_norm, land_norm)
    rgba = (cm.terrain(norm) * 255).astype(np.uint8)
    return rgba  # (H, W, 4)


# ---------------------------------------------------------------------------
# Tile reader + per-tile RGBA renderer
# ---------------------------------------------------------------------------
def _read_height_tile_raw(tile_x: int, tile_z: int) -> np.ndarray:
    """Read height mask window. Returns (512, 512) uint16."""
    import rasterio
    from rasterio.windows import Window

    path    = MASKS_DIR / "height.tif"
    col_off = tile_x * TILE_SIZE
    row_off = tile_z * TILE_SIZE
    with rasterio.open(str(path)) as src:
        win  = Window(col_off, row_off, TILE_SIZE, TILE_SIZE)
        data = src.read(1, window=win)
    return data.astype(np.uint16)


def _read_zone_tile_raw(tile_x: int, tile_z: int) -> np.ndarray:
    """Read override zone window. Returns (512, 512) uint8."""
    import rasterio
    from rasterio.windows import Window
    with rasterio.open(str(OVERRIDE_TIF)) as src:
        win  = Window(tile_x * TILE_SIZE, tile_z * TILE_SIZE, TILE_SIZE, TILE_SIZE)
        data = src.read(1, window=win)
    return data.astype(np.uint8)


def _placeholder(display_px: int) -> np.ndarray:
    p = np.full((display_px, display_px, 4), 30, dtype=np.uint8)
    p[..., 3] = 255
    return p


def _render_surface_height(tile_x: int, tile_z: int,
                            display_px: int = DISPLAY_SIZE) -> np.ndarray:
    try:
        raw       = _read_height_tile_raw(tile_x, tile_z)
        step      = TILE_SIZE // display_px
        return _surface_height_rgba(_LUT[raw][::step, ::step])
    except Exception:
        return _placeholder(display_px)


def _render_biome(tile_x: int, tile_z: int,
                  display_px: int = DISPLAY_SIZE) -> np.ndarray:
    try:
        import rasterio
        from rasterio.windows import Window
        from PIL import Image as _PILImg
        from scipy.ndimage import median_filter as _mf
        with rasterio.open(str(OVERRIDE_TIF)) as src:
            win  = Window(tile_x * TILE_SIZE, tile_z * TILE_SIZE, TILE_SIZE, TILE_SIZE)
            data = src.read(1, window=win).astype(np.uint8)
        # Majority-vote display filter: median with odd window size always returns
        # a value present in the input, so all outputs are valid zone codes.
        # Removes jitter-scattered boundary pixels for a clean display view
        # without touching the underlying override.tif data.
        data = _mf(data, size=11)
        rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)
        rgba[..., 3] = 255
        for zone, (r, g, b) in _ZONE_COLOURS.items():
            rgba[data == zone] = [r, g, b, 255]
        unlisted = ~np.isin(data, list(_ZONE_COLOURS.keys()))
        rgba[unlisted] = [70, 70, 70, 255]
        pil = _PILImg.fromarray(rgba, mode="RGBA").resize(
            (display_px, display_px), _PILImg.BOX)
        return np.array(pil, dtype=np.uint8)
    except Exception:
        return _placeholder(display_px)


def _render_slope(tile_x: int, tile_z: int,
                  display_px: int = DISPLAY_SIZE) -> np.ndarray:
    try:
        import matplotlib.cm as cm
        raw      = _read_height_tile_raw(tile_x, tile_z)
        surf     = _LUT[raw].astype(np.float32)
        gy       = surf[2:, 1:-1] - surf[:-2, 1:-1]
        gx       = surf[1:-1, 2:] - surf[1:-1, :-2]
        slope    = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.float32)
        slope[1:-1, 1:-1] = np.sqrt(gx ** 2 + gy ** 2)
        step     = TILE_SIZE // display_px
        ds       = slope[::step, ::step]
        norm     = np.clip(ds / max(ds.max(), 1e-6), 0.0, 1.0)
        return (cm.hot(norm) * 255).astype(np.uint8)
    except Exception:
        return _placeholder(display_px)


def _render_surface_block(tile_x: int, tile_z: int,
                           display_px: int = DISPLAY_SIZE) -> np.ndarray:
    """Show base-block colour per pixel, derived from override zone."""
    try:
        import rasterio
        from rasterio.windows import Window
        from PIL import Image as _PILImg
        with rasterio.open(str(OVERRIDE_TIF)) as src:
            win   = Window(tile_x * TILE_SIZE, tile_z * TILE_SIZE, TILE_SIZE, TILE_SIZE)
            zones = src.read(1, window=win).astype(np.uint8)
        try:
            from core.surface_decorator import BIOME_BLOCK_PALETTES as _BPL
        except Exception:
            _BPL = {}
        from scipy.ndimage import median_filter as _mf
        zones = _mf(zones, size=11)
        # Render at full tile resolution then BOX-downsample (same as biome render)
        rgba = np.zeros((TILE_SIZE, TILE_SIZE, 4), dtype=np.uint8)
        rgba[..., 3] = 255
        for zone_val, zone_name in _ZONE_NAMES.items():
            mask = zones == zone_val
            if not mask.any():
                continue
            if zone_val == 0:
                rgba[mask] = [20, 30, 60, 255]
                continue
            palette    = _BPL.get(zone_name, [])
            base_block = next((s for s, _, c in palette if c == "base"), None)
            hex_c      = _BLOCK_COLOURS.get(base_block or "", "#505050")
            rgba[mask] = [int(hex_c[1:3],16), int(hex_c[3:5],16), int(hex_c[5:7],16), 255]
        pil = _PILImg.fromarray(rgba, mode="RGBA").resize(
            (display_px, display_px), _PILImg.BOX)
        return np.array(pil, dtype=np.uint8)
    except Exception:
        return _placeholder(display_px)


_RENDER_FUNCS = {
    "surface_height": _render_surface_height,
    "biome":          _render_biome,
    "slope":          _render_slope,
    "surface_block":  _render_surface_block,
}


def _render_tile_rgba(tile_x: int, tile_z: int,
                      display_px: int = DISPLAY_SIZE,
                      mode: str = "surface_height") -> np.ndarray:
    return _RENDER_FUNCS.get(mode, _render_surface_height)(tile_x, tile_z, display_px)


# ---------------------------------------------------------------------------
# 3×3 grid composer
# ---------------------------------------------------------------------------
def _build_3x3_pixmap(
    centre_x: int,
    centre_z: int,
    tile_cache: Dict[Tuple[int, int, str], np.ndarray],
    mode: str = "surface_height",
) -> QPixmap:
    """
    Compose a 3×3 grid of tiles centred on (centre_x, centre_z).
    Draws grey grid lines and tile coordinate labels.
    Returns a QPixmap of size (CANVAS_SIZE, CANVAS_SIZE).
    """
    canvas = np.zeros((CANVAS_SIZE, CANVAS_SIZE, 4), dtype=np.uint8)

    for dz in range(GRID_N):
        for dx in range(GRID_N):
            tx = centre_x + dx - 1
            tz = centre_z + dz - 1
            key = (tx, tz, mode)
            if key not in tile_cache:
                tile_cache[key] = _render_tile_rgba(tx, tz, mode=mode)
            rgba = tile_cache[key]
            row0 = dz * DISPLAY_SIZE
            col0 = dx * DISPLAY_SIZE
            canvas[row0:row0 + DISPLAY_SIZE, col0:col0 + DISPLAY_SIZE] = rgba

    # Convert to QPixmap
    h, w, _ = canvas.shape
    img = QImage(canvas.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888)
    pix = QPixmap.fromImage(img)

    # Draw grid lines and labels with QPainter
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

    grid_pen = QPen(QColor(80, 80, 80, 200))
    grid_pen.setWidth(1)
    painter.setPen(grid_pen)

    for i in range(1, GRID_N):
        x = i * DISPLAY_SIZE
        painter.drawLine(x, 0, x, CANVAS_SIZE)
        painter.drawLine(0, x, CANVAS_SIZE, x)

    # Centre tile highlight
    centre_pen = QPen(QColor(220, 220, 80, 180))
    centre_pen.setWidth(2)
    painter.setPen(centre_pen)
    painter.drawRect(DISPLAY_SIZE, DISPLAY_SIZE, DISPLAY_SIZE - 1, DISPLAY_SIZE - 1)

    # Tile coordinate labels
    font = QFont("Consolas", 9)
    font.setBold(True)
    painter.setFont(font)

    for dz in range(GRID_N):
        for dx in range(GRID_N):
            tx = centre_x + dx - 1
            tz = centre_z + dz - 1
            col0 = dx * DISPLAY_SIZE
            row0 = dz * DISPLAY_SIZE
            label = f"({tx},{tz})"

            # Shadow
            painter.setPen(QPen(QColor(0, 0, 0, 180)))
            painter.drawText(col0 + 5, row0 + 17, label)
            # Text
            painter.setPen(QPen(QColor(230, 230, 230, 220)))
            painter.drawText(col0 + 4, row0 + 16, label)

    painter.end()
    return pix


# ---------------------------------------------------------------------------
# Panel 1 — Map View (3×3 grid)
# ---------------------------------------------------------------------------
class MapPanel(QLabel):
    """Left panel: renders a 3×3 tile grid centred on (tile_x, tile_z).
    Supports four render modes, mouse-wheel zoom, and left-drag pan."""

    def __init__(self, tile_x: int, tile_z: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.tile_x   = tile_x
        self.tile_z   = tile_z
        self._mode    = "surface_height"
        self._zoom    = 1.0          # 1.0 = full 3×3 grid; >1 zoomed in
        self._pan     = [0.5, 0.5]  # centre of view as fraction [0,1]
        self._panning     = False
        self._pan_start: tuple | None = None
        self._pan_moved   = False
        self._cache: Dict[Tuple[int, int, str], np.ndarray] = {}
        self._zone_cache: Dict[Tuple[int, int], np.ndarray] = {}
        self._height_cache: Dict[Tuple[int, int], np.ndarray] = {}
        self._full_pix: QPixmap | None = None

        # Callback: set by main window — (tile_x, tile_z, row) -> None
        self.on_row_selected = None
        # Callback: fired when user navigates to a new centre tile — (tile_x, tile_z) -> None
        self.on_center_changed = None
        # Callback: hover info — (zone_name, mc_y, tile_x, tile_z, px, pz) -> None
        self.on_hover = None

        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(256, 256)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background-color: #1a1a2e; color: #aaaaaa; font-size: 13px;")
        self.setText(f"Map — tile ({tile_x}, {tile_z})\nRendering…")
        self.setCursor(Qt.CursorShape.CrossCursor)

    # ------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.load()

    def load(self) -> None:
        """Render the 3×3 grid and display it."""
        self.setText(f"Map [{self._mode}] — rendering…")
        QApplication.processEvents()
        try:
            self._full_pix = _build_3x3_pixmap(
                self.tile_x, self.tile_z, self._cache, mode=self._mode)
            self._display()
        except Exception as exc:
            self.setText(f"Render error:\n{exc}")

    def set_center(self, tile_x: int, tile_z: int) -> None:
        """Re-centre the 3×3 grid on a new tile and reload."""
        self.tile_x = tile_x
        self.tile_z = tile_z
        self.load()

    # ------------------------------------------------------------------
    def _display(self) -> None:
        if self._full_pix is None:
            return
        if self._zoom <= 1.0:
            pix = self._full_pix.scaled(
                self.width(), self.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            fw = fh = CANVAS_SIZE
            cw = max(1, int(fw / self._zoom))
            ch = max(1, int(fh / self._zoom))
            x0 = int(self._pan[0] * (fw - cw))
            y0 = int(self._pan[1] * (fh - ch))
            cropped = self._full_pix.copy(QRect(x0, y0, cw, ch))
            pix = cropped.scaled(
                self.width(), self.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        self.setPixmap(pix)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._display()

    # ------------------------------------------------------------------
    def wheelEvent(self, event) -> None:  # type: ignore[override]
        factor = 1.25 if event.angleDelta().y() > 0 else 1 / 1.25
        self._zoom = max(1.0, min(10.0, self._zoom * factor))
        self._display()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        """Double-click resets zoom and pan."""
        self._zoom = 1.0
        self._pan  = [0.5, 0.5]
        self._display()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._panning   = (self._zoom > 1.0)
            self._pan_start = (event.position().x(), event.position().y())
            self._pan_moved = False
        if self._zoom <= 1.0:
            self._select_row(event)   # immediate at zoom=1 (original behaviour)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        # Pan handling
        if self._panning and self._pan_start is not None:
            dx = event.position().x() - self._pan_start[0]
            dy = event.position().y() - self._pan_start[1]
            if abs(dx) > 3 or abs(dy) > 3:
                self._pan_moved = True
            self._pan[0] = max(0.0, min(1.0, self._pan[0] - dx / self.width()))
            self._pan[1] = max(0.0, min(1.0, self._pan[1] - dy / self.height()))
            self._pan_start = (event.position().x(), event.position().y())
            self._display()
        # Hover readout
        self._fire_hover(event.position().x(), event.position().y())

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton and self._panning:
            self._panning = False
            if not self._pan_moved:
                self._select_row(event)   # short tap = row select
            self._pan_start = None

    # ------------------------------------------------------------------
    def _disp_size(self) -> tuple[int, int, int, int]:
        """Return (disp_w, disp_h, offset_x, offset_y) of displayed pixmap."""
        if self.width() >= self.height():
            dw = dh = self.height()
        else:
            dw = dh = self.width()
        return dw, dh, (self.width() - dw) // 2, (self.height() - dh) // 2

    def _select_row(self, event) -> None:
        """Compute tile + row from click position and fire callback."""
        if self._full_pix is None or self.on_row_selected is None:
            return
        dw, dh, ox, oy = self._disp_size()
        px = event.position().x() - ox
        py = event.position().y() - oy
        if not (0 <= px < dw and 0 <= py < dh):
            return

        # Canvas coordinates accounting for zoom/pan
        if self._zoom > 1.0:
            cw = max(1, int(CANVAS_SIZE / self._zoom))
            ch = max(1, int(CANVAS_SIZE / self._zoom))
            x0 = int(self._pan[0] * (CANVAS_SIZE - cw))
            y0 = int(self._pan[1] * (CANVAS_SIZE - ch))
            cx = int(px * cw / dw) + x0
            cy = int(py * ch / dh) + y0
        else:
            cx = int(px * CANVAS_SIZE / dw)
            cy = int(py * CANVAS_SIZE / dh)

        cx = max(0, min(CANVAS_SIZE - 1, cx))
        cy = max(0, min(CANVAS_SIZE - 1, cy))

        dx  = cx // DISPLAY_SIZE
        dz  = cy // DISPLAY_SIZE
        tx  = self.tile_x + dx - 1
        tz  = self.tile_z + dz - 1
        row = min((cy % DISPLAY_SIZE) * (TILE_SIZE // DISPLAY_SIZE), TILE_SIZE - 1)
        self.on_row_selected(tx, tz, row)
        if (tx != self.tile_x or tz != self.tile_z) and self.on_center_changed:
            self.on_center_changed(tx, tz)

    def _cursor_to_tile_px(self, scr_x: float, scr_y: float
                           ) -> tuple[int, int, int, int] | None:
        """Map screen position → (tile_x, tile_z, px_in_tile, pz_in_tile) or None."""
        dw, dh, ox, oy = self._disp_size()
        px = scr_x - ox
        py = scr_y - oy
        if not (0 <= px < dw and 0 <= py < dh):
            return None
        if self._zoom > 1.0:
            cw = max(1, int(CANVAS_SIZE / self._zoom))
            ch = max(1, int(CANVAS_SIZE / self._zoom))
            x0 = int(self._pan[0] * (CANVAS_SIZE - cw))
            y0 = int(self._pan[1] * (CANVAS_SIZE - ch))
            cx = int(px * cw / dw) + x0
            cy = int(py * ch / dh) + y0
        else:
            cx = int(px * CANVAS_SIZE / dw)
            cy = int(py * CANVAS_SIZE / dh)
        cx = max(0, min(CANVAS_SIZE - 1, cx))
        cy = max(0, min(CANVAS_SIZE - 1, cy))
        scale = TILE_SIZE / DISPLAY_SIZE
        tx = self.tile_x + cx // DISPLAY_SIZE - 1
        tz = self.tile_z + cy // DISPLAY_SIZE - 1
        tile_px = int((cx % DISPLAY_SIZE) * scale)
        tile_pz = int((cy % DISPLAY_SIZE) * scale)
        return tx, tz, min(tile_px, TILE_SIZE - 1), min(tile_pz, TILE_SIZE - 1)

    def _fire_hover(self, scr_x: float, scr_y: float) -> None:
        if self.on_hover is None or self._full_pix is None:
            return
        result = self._cursor_to_tile_px(scr_x, scr_y)
        if result is None:
            return
        tx, tz, tile_px, tile_pz = result
        # Zone lookup
        zkey = (tx, tz)
        if zkey not in self._zone_cache:
            try:
                self._zone_cache[zkey] = _read_zone_tile_raw(tx, tz)
            except Exception:
                self._zone_cache[zkey] = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
        # Height lookup
        hkey = (tx, tz)
        if hkey not in self._height_cache:
            try:
                self._height_cache[hkey] = _read_height_tile_raw(tx, tz)
            except Exception:
                self._height_cache[hkey] = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint16)
        zone_val = int(self._zone_cache[zkey][tile_pz, tile_px])
        raw_h    = int(self._height_cache[hkey][tile_pz, tile_px])
        mc_y     = int(_LUT[raw_h])
        zone_name = _ZONE_NAMES.get(zone_val, f"zone{zone_val}")
        self.on_hover(zone_name, mc_y, tx, tz, tile_px, tile_pz)


# ---------------------------------------------------------------------------
# Panel 2 — Cross-Section
# ---------------------------------------------------------------------------
class CrossSectionPanel(QWidget):
    """Top-right panel: matplotlib cross-section for a single tile row."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(200, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._fig = Figure(facecolor="#0f3460")
        self._fig.subplots_adjust(left=0.10, right=0.97, top=0.88, bottom=0.14)
        self._ax  = self._fig.add_subplot(111)
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._canvas.setParent(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._canvas)

        self._style_axes()
        self._ax.text(
            256, 192, "Loading…",
            ha="center", va="center", color="#6688aa", fontsize=11,
        )
        self._canvas.draw()

    # ------------------------------------------------------------------
    def _style_axes(self) -> None:
        ax = self._ax
        ax.set_facecolor("#0a2040")
        ax.set_xlim(0, 511)
        ax.set_ylim(-64, 703)
        ax.tick_params(colors="#6688aa", labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor("#1a5276")
        ax.set_xlabel("West → East (px)", color="#6688aa", fontsize=9)
        ax.set_ylabel("MC Y", color="#6688aa", fontsize=9)

    # ------------------------------------------------------------------
    def profile_row(self, tile_x: int, tile_z: int, row: int,
                    min_ocean_depth: int = 0) -> None:
        """Render cross-section for one row of a tile."""
        ax = self._ax
        ax.cla()
        self._style_axes()

        try:
            raw       = _read_height_tile_raw(tile_x, tile_z)
            surface_y = _LUT[raw][row, :].astype(np.float32)   # (512,)
            xs        = np.arange(512)

            # Terrain silhouette
            ax.fill_between(xs, -64, surface_y, color="#3a6b30", alpha=0.85)
            ax.plot(xs, surface_y, color="#7ec870", linewidth=0.8)

            # Ocean depth overlay — blue fill from raw surface down to carved floor
            if min_ocean_depth > 0:
                ocean_mask  = surface_y < 63
                carved_floor = np.where(ocean_mask, surface_y - min_ocean_depth, surface_y)
                ax.fill_between(
                    xs, carved_floor, surface_y,
                    where=ocean_mask,
                    color="#1a6fa8", alpha=0.75,
                    label=f"ocean depth (–{min_ocean_depth})",
                )

            # Sea level
            ax.axhline(63, color="#00cccc", linewidth=1.0, linestyle="--", alpha=0.8)
            ax.text(508, 66, "Y=63", color="#00cccc", fontsize=7, ha="right")

            ax.set_title(
                f"Tile ({tile_x},{tile_z})  row {row}",
                color="#aaaacc", fontsize=9, pad=4,
            )
        except Exception as exc:
            ax.text(
                256, 192, f"Error: {exc}",
                ha="center", va="center", color="#cc4444", fontsize=9,
            )

        self._canvas.draw()


# ---------------------------------------------------------------------------
# Panel 3 — Control Panel (Tab C live; Tabs A/B placeholder)
# ---------------------------------------------------------------------------
def _make_slider(min_val: int, max_val: int, value: int) -> QSlider:
    s = QSlider(Qt.Orientation.Horizontal)
    s.setRange(min_val, max_val)
    s.setValue(value)
    s.setTracking(True)
    return s


# ---------------------------------------------------------------------------
# Spline Editor widget (used in Tab A)
# ---------------------------------------------------------------------------
class SplineEditorWidget(QWidget):
    """Draggable PCHIP spline editor embedded in Tab A."""

    _SEA_RAW = 17050
    _SEA_Y   = 63

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.on_applied = None   # callback(gaea_in, mc_y_out) -> None
        self._dragging: int | None = None
        self._pts: list[tuple[int, int]] = []
        self._load_breakpoints()
        self._build_ui()
        self._redraw()

    # ------------------------------------------------------------------
    _DEFAULT_PTS = [(0, -64), (17050, 63), (45000, 200), (65496, 448)]

    def _load_breakpoints(self) -> None:
        try:
            cfg = _load_thresholds()
            sp  = cfg.get("terrain_spline", {})
            gs  = sp.get("gaea_in",  [p[0] for p in self._DEFAULT_PTS])
            ys  = sp.get("mc_y_out", [p[1] for p in self._DEFAULT_PTS])
            self._pts = list(zip(map(int, gs), map(int, ys)))
        except Exception:
            self._pts = list(self._DEFAULT_PTS)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._fig = Figure(facecolor="#0d1b2a")
        self._fig.subplots_adjust(left=0.13, right=0.97, top=0.91, bottom=0.13)
        self._ax  = self._fig.add_subplot(111)
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._canvas.mpl_connect("button_press_event",   self._on_press)
        self._canvas.mpl_connect("motion_notify_event",  self._on_motion)
        self._canvas.mpl_connect("button_release_event", self._on_release)

        # Breakpoint table
        self._table = QTableWidget(len(self._pts), 2)
        self._table.setHorizontalHeaderLabels(["Gaea raw", "MC Y"])
        self._table.setMaximumHeight(90)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setStyleSheet(
            "background:#0d1b2a; color:#cccccc; font-size:10px; "
            "QHeaderView::section { background:#0a1525; color:#8899bb; }"
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        # Apply / Reset buttons
        btn_apply = QPushButton("Apply")
        btn_reset = QPushButton("Reset")
        for btn in (btn_apply, btn_reset):
            btn.setStyleSheet(
                "QPushButton { background:#1e3a70; color:#ddeeff; "
                "border:1px solid #2a5090; padding:3px 10px; }"
                "QPushButton:hover { background:#2a4e90; }"
            )
        btn_apply.clicked.connect(self._on_apply)
        btn_reset.clicked.connect(self._on_reset)

        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 2, 0, 2)
        bl.addWidget(btn_apply)
        bl.addWidget(btn_reset)
        bl.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.addWidget(self._canvas, stretch=3)
        layout.addWidget(self._table,  stretch=0)
        layout.addWidget(btn_row,      stretch=0)

    # ------------------------------------------------------------------
    def _is_fixed(self, idx: int) -> bool:
        g, y = self._pts[idx]
        return g == self._SEA_RAW and y == self._SEA_Y

    # ------------------------------------------------------------------
    def _redraw(self) -> None:
        from scipy.interpolate import PchipInterpolator
        ax = self._ax
        ax.cla()
        ax.set_facecolor("#0a1a2e")
        ax.set_xlim(0, 65535)
        ax.set_ylim(-64, 703)
        ax.tick_params(colors="#6688aa", labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor("#1a3a5c")
        ax.set_xlabel("Gaea raw (16-bit)", color="#6688aa", fontsize=8)
        ax.set_ylabel("MC Y",              color="#6688aa", fontsize=8)
        ax.set_title("Height Spline  (drag orange · right-click to add/remove)",
                     color="#aaaacc", fontsize=8, pad=3)

        # PCHIP curve
        if len(self._pts) >= 2:
            gs = [p[0] for p in self._pts]
            ys = [p[1] for p in self._pts]
            xs_dense = np.linspace(0, 65535, 600)
            ys_dense = np.clip(PchipInterpolator(gs, ys)(xs_dense), -64, 703)
            ax.plot(xs_dense, ys_dense, color="#4488cc", linewidth=1.5, zorder=3)

        # Sea level reference lines
        ax.axhline(self._SEA_Y,  color="#00cccc", linewidth=0.8,
                   linestyle=":", alpha=0.55, zorder=2)
        ax.axvline(self._SEA_RAW, color="#00cccc", linewidth=0.8,
                   linestyle=":", alpha=0.55, zorder=2)

        # Breakpoints
        for i, (g, y) in enumerate(self._pts):
            color = "#00cccc" if self._is_fixed(i) else "#ff8844"
            ax.plot(g, y, "o", color=color, markersize=9, zorder=5,
                    markeredgecolor="#ffffff", markeredgewidth=0.8)

        # Crosshair artists — recreated here so they survive ax.cla()
        self._hover_vline = ax.axvline(x=0, color="#ffdd88", linewidth=0.7,
                                       linestyle="--", alpha=0.0, zorder=6)
        self._hover_hline = ax.axhline(y=0, color="#ffdd88", linewidth=0.7,
                                       linestyle="--", alpha=0.0, zorder=6)
        self._hover_label = ax.text(
            0.02, 0.97, "", transform=ax.transAxes,
            color="#ffdd88", fontsize=8, va="top", ha="left", zorder=7,
            bbox=dict(facecolor="#0a1a2e", alpha=0.75, edgecolor="none", pad=2),
        )

        self._canvas.draw()
        self._update_table()

    # ------------------------------------------------------------------
    def _update_table(self) -> None:
        self._table.setRowCount(len(self._pts))
        for i, (g, y) in enumerate(self._pts):
            self._table.setItem(i, 0, QTableWidgetItem(str(g)))
            self._table.setItem(i, 1, QTableWidgetItem(str(y)))

    # ------------------------------------------------------------------
    def _hit_test(self, event) -> int | None:
        if event.xdata is None:
            return None
        PICK_PX = 14
        for i, (g, y) in enumerate(self._pts):
            disp  = self._ax.transData.transform((g, y))
            mouse = np.array([event.x, event.y])
            if float(np.linalg.norm(disp - mouse)) < PICK_PX:
                return i
        return None

    # ------------------------------------------------------------------
    def _is_endpoint(self, idx: int) -> bool:
        return idx == 0 or idx == len(self._pts) - 1

    def _on_press(self, event) -> None:
        if event.inaxes is None:
            return

        if event.button == 1:
            # Left-click: start drag
            idx = self._hit_test(event)
            if idx is not None and not self._is_fixed(idx):
                self._dragging = idx

        elif event.button == 3:
            # Right-click: add or remove breakpoint
            idx = self._hit_test(event)
            if idx is not None:
                # Remove if movable and not an endpoint
                if not self._is_fixed(idx) and not self._is_endpoint(idx):
                    self._pts.pop(idx)
                    self._redraw()
            else:
                # Add new point at cursor
                if event.xdata is not None and event.ydata is not None:
                    new_g = int(np.clip(event.xdata, 1, 65534))
                    new_y = int(np.clip(event.ydata, -64, 703))
                    # Insert maintaining sorted order by gaea raw
                    insert_at = len(self._pts)
                    for i, (g, _) in enumerate(self._pts):
                        if g > new_g:
                            insert_at = i
                            break
                    self._pts.insert(insert_at, (new_g, new_y))
                    self._redraw()

    def _on_motion(self, event) -> None:
        # --- Hover inspector (when not dragging) ---
        if self._dragging is None:
            if event.inaxes is self._ax and event.xdata is not None:
                from scipy.interpolate import PchipInterpolator
                gs = [p[0] for p in self._pts]
                ys = [p[1] for p in self._pts]
                mc_y = float(np.clip(PchipInterpolator(gs, ys)(event.xdata), -64, 703))
                self._hover_vline.set_xdata([event.xdata])
                self._hover_hline.set_ydata([mc_y])
                self._hover_vline.set_alpha(0.45)
                self._hover_hline.set_alpha(0.45)
                self._hover_label.set_text(
                    f"raw {int(event.xdata):,}  →  Y {mc_y:.0f}")
            else:
                self._hover_vline.set_alpha(0.0)
                self._hover_hline.set_alpha(0.0)
                self._hover_label.set_text("")
            self._canvas.draw_idle()
            return

        idx = self._dragging
        gs  = [p[0] for p in self._pts]

        # Monotonicity clamp on X
        x_min = (gs[idx - 1] + 1) if idx > 0         else 0
        x_max = (gs[idx + 1] - 1) if idx < len(self._pts) - 1 else 65535

        new_g = int(np.clip(event.xdata, x_min, x_max))
        new_y = int(np.clip(event.ydata, -64, 703))

        # Lock X for first and last points
        if idx == 0:
            new_g = 0
        elif idx == len(self._pts) - 1:
            new_g = 65535

        self._pts[idx] = (new_g, new_y)
        self._redraw()

    def _on_release(self, event) -> None:
        self._dragging = None

    # ------------------------------------------------------------------
    def _on_apply(self) -> None:
        try:
            cfg = _load_thresholds()
            cfg["terrain_spline"]["gaea_in"]  = [p[0] for p in self._pts]
            cfg["terrain_spline"]["mc_y_out"] = [p[1] for p in self._pts]
            _save_thresholds(cfg)
        except Exception as exc:
            print(f"[SplineEditor] save failed: {exc}", flush=True)
        if self.on_applied:
            self.on_applied([p[0] for p in self._pts],
                            [p[1] for p in self._pts])

    def _on_reset(self) -> None:
        self._load_breakpoints()
        self._redraw()


# ---------------------------------------------------------------------------
# Surface Block Editor widget (used in Tab B)
# ---------------------------------------------------------------------------
_CONDITION_OPTIONS = [
    "base", "noise", "noise2", "noise3", "noise4",
    "erosion", "erosion2", "moisture", "moisture2", "altitude",
]

# Pipeline block-assignment priority (lowest → highest; later overwrites)
_APPLY_PRIORITY = [
    "base", "noise4", "noise3", "noise2", "noise",
    "moisture2", "moisture", "erosion2", "erosion", "altitude",
]

# Approximate Minecraft block colours for the preview
# Zone value → biome name (from override.tif)
_ZONE_NAMES: dict[int, str] = {
    0: "none", 10: "COASTAL_HEATH", 20: "TEMPERATE_RAINFOREST",
    30: "BOREAL_TAIGA", 35: "SNOWY_BOREAL_TAIGA", 40: "ALPINE_MEADOW",
    50: "ARCTIC_TUNDRA", 55: "FROZEN_FLATS", 60: "TEMPERATE_DECIDUOUS",
    70: "RAINFOREST_COAST", 80: "RIPARIAN_WOODLAND", 90: "DRY_OAK_SAVANNA",
    100: "KARST_BARRENS", 110: "BIRCH_FOREST", 115: "EASTERN_TEMPERATE_COAST",
    120: "MIXED_FOREST", 130: "CONTINENTAL_STEPPE", 140: "DRY_PINE_BARRENS",
    150: "SCRUBBY_HEATHLAND", 160: "LUSH_RAINFOREST_COAST", 170: "SAND_DUNE_DESERT",
    190: "DESERT_STEPPE_TRANSITION", 200: "SEMI_ARID_SHRUBLAND",
    210: "DRY_WOODLAND_MAQUIS", 220: "TIDAL_JUNGLE_FRINGE",
    230: "MANGROVE_COAST", 240: "FRESHWATER_FEN",
}

# False-colour palette for biome map render mode
_ZONE_COLOURS: dict[int, tuple[int,int,int]] = {
    0:   (20,  30,  60),   10:  (180,160,100),  20:  (30, 110, 60),
    30:  (60, 100,130),    35:  (100,130,160),   40:  (160,200,140),
    50:  (200,210,220),    55:  (230,240,255),   60:  (80, 150, 60),
    70:  (40, 140, 90),    80:  (50, 120, 80),   90:  (180,150, 60),
    100: (150,140,110),   110:  (200,220,160),  115:  (130,180,150),
    120: (100,160, 80),   130:  (190,180,120),  140:  (120,100, 70),
    150: (160,140, 90),   160:  (20, 160,100),  170:  (220,200,100),
    190: (180,160, 80),   200:  (160,120, 60),  210:  (120,140, 60),
    220: (0,  140,100),   230:  (0,  120, 80),  240:  (80, 160,200),
}

_BLOCK_COLOURS: dict[str, str] = {
    "grass_block":        "#5a8f3c",
    "dirt":               "#7a5230",
    "coarse_dirt":        "#6b4a2a",
    "podzol":             "#5c4020",
    "stone":              "#6e7070",
    "cobblestone":        "#7a7060",
    "mossy_cobblestone":  "#5a7050",
    "andesite":           "#5c6868",   # darker teal-grey — distinct from stone
    "diorite":            "#c0b8a8",   # warm off-white — distinct from calcite
    "granite":            "#a06040",
    "tuff":               "#828c58",
    "gravel":             "#9a8878",
    "sand":               "#d4c870",
    "sandstone":          "#c8b048",
    "mud":                "#5a4838",
    "clay":               "#8898a8",
    "moss_block":         "#4a7040",
    "snow_block":         "#eef4f8",
    "ice":                "#90c0d8",
    "calcite":            "#e8e4dc",   # near-white cream — distinct from diorite
    "basalt":             "#383838",
    "deepslate":          "#424450",
    "rooted_dirt":        "#7a5a35",
    "mycelium":           "#7a6888",
    "packed_ice":         "#78a8c0",
}


# ---------------------------------------------------------------------------
# Surface Block Visualiser
# ---------------------------------------------------------------------------
class SurfaceBlockVisualizerWidget(QWidget):
    """
    192×192 px real-time preview of how a biome's block palette distributes
    under synthetic noise, erosion and moisture fields.

    Noise type dropdown + scale (fuzziness) slider change the field character.
    """
    _PREVIEW_PX = 192
    _NOISE_TYPES = ["OpenSimplex", "Voronoi", "Gaussian", "Fractal", "Mixed"]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._palette: list = []
        self._noise_type    = "OpenSimplex"
        self._scale         = 60
        self._jitter_amp    = 0.8   # Mixed mode only: ±jitter on macro field
        self._thresholds: dict = {}
        self._tile_x        = 32
        self._tile_z        = 2
        self._real_fields:  dict | None = None   # cached real mask fields
        self._real_tile_key: tuple | None = None # (tile_x, tile_z)
        self._load_thresholds_local()
        self._build_ui()

    # Balanced visual thresholds for preview — NOT the mask-calibrated values.
    # Real thresholds (e.g. erosion=0.004) are calibrated for actual GeoTIFF
    # data ranges; using them against synthetic [0,1] noise gives 89% coverage
    # and buries everything. These mirror the test-mode defaults in
    # surface_decorator.py (erosion=0.60, moisture=0.65, noise=0.68).
    _BASE_THRESHOLDS = {
        "noise":    0.62, "noise2":   0.64, "noise3":   0.67, "noise4":   0.71,
        "erosion":  0.58, "erosion2": 0.73,
        "moisture": 0.62, "moisture2":0.76,
        "altitude": 0.70,
    }

    def _load_thresholds_local(self) -> None:
        try:
            cfg = _load_thresholds()
            self._scale = int(cfg.get("block_mixing", {}).get("noise_scale", 60))
        except Exception:
            self._scale = 60
        self._thresholds = dict(self._BASE_THRESHOLDS)

    # ------------------------------------------------------------------
    def set_tile(self, tile_x: int, tile_z: int) -> None:
        """Update the tile coordinates; invalidate real-field cache."""
        if (tile_x, tile_z) != (self._tile_x, self._tile_z):
            self._tile_x        = tile_x
            self._tile_z        = tile_z
            self._real_fields   = None
            self._real_tile_key = None
            if hasattr(self, "_tile_label"):
                self._tile_label.setText(f"Tile ({tile_x}, {tile_z})")
            self._update_biome_coverage()
            if hasattr(self, "_source_combo") and \
                    self._source_combo.currentText() == "Real tile":
                self._render_fast()

    # ------------------------------------------------------------------
    def _read_real_fields(self, tile_x: int, tile_z: int) -> dict[str, np.ndarray]:
        """
        Read erosion, flow, height, slope tiles → normalize to [0,1] at
        PREVIEW_PX resolution. Returns field dict keyed by condition group.
        """
        import rasterio
        from rasterio.windows import Window
        from scipy.ndimage import zoom

        sz  = TILE_SIZE   # 512 — native tile size
        psz = self._PREVIEW_PX   # 192 — display size

        def read_mask(name: str) -> np.ndarray:
            path = MASKS_DIR / f"{name}.tif"
            with rasterio.open(str(path)) as src:
                win = Window(tile_x * sz, tile_z * sz, sz, sz)
                raw = src.read(1, window=win).astype(np.float32)
            # Downscale to preview size
            factor = psz / sz
            down   = zoom(raw, factor, order=1)
            lo, hi = down.min(), down.max()
            return (down - lo) / (hi - lo + 1e-9)

        erosion_f  = read_mask("erosion")
        flow_f     = read_mask("flow")
        height_f   = read_mask("height")
        # height mask polarity: LOW raw = HIGH terrain, so invert for altitude
        altitude_f = 1.0 - height_f

        return {
            "erosion":   erosion_f,
            "erosion2":  erosion_f,
            "moisture":  flow_f,
            "moisture2": flow_f,
            "altitude":  altitude_f,
        }

    def _get_real_fields(self) -> dict[str, np.ndarray] | None:
        """Return cached real fields, loading if needed. Returns None on error."""
        key = (self._tile_x, self._tile_z)
        if self._real_fields is None or self._real_tile_key != key:
            try:
                self._real_fields   = self._read_real_fields(*key)
                self._real_tile_key = key
            except Exception as exc:
                print(f"[Viz] real field load failed: {exc}", flush=True)
                return None
        return self._real_fields

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setStyleSheet("background:#16213e; color:#cccccc;")
        self._cond_sliders: dict[str, QSlider] = {}
        self._cond_labels:  dict[str, QLabel]  = {}
        self._noise_cache:  dict | None = None
        self._cache_key:    tuple | None = None

        self._fig = Figure(facecolor="#0d1b2a")
        self._fig.subplots_adjust(0, 0, 1, 1)
        self._ax  = self._fig.add_subplot(111)
        self._ax.axis("off")
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._canvas.setFixedSize(self._PREVIEW_PX, self._PREVIEW_PX)

        # ── Mode + Scale + Apply row ──────────────────────────────────────
        from PyQt6.QtWidgets import QScrollArea
        noise_lbl = QLabel("Mode:")
        noise_lbl.setStyleSheet("font-size:10px; color:#8899bb;")
        self._noise_combo = QComboBox()
        self._noise_combo.addItems(self._NOISE_TYPES)
        self._noise_combo.setStyleSheet("background:#1a2a50; color:#ddeeff; font-size:10px;")

        scale_lbl       = QLabel("Scale:")
        scale_lbl.setStyleSheet("font-size:10px; color:#8899bb;")
        self._scale_val = QLabel(f"{self._scale}")
        self._scale_val.setStyleSheet("font-size:10px; color:#aaddff; min-width:26px;")
        self._scale_slider = _make_slider(10, 300, self._scale)
        self._scale_slider.valueChanged.connect(
            lambda v: self._scale_val.setText(str(v)))

        btn_apply = QPushButton("Apply")
        btn_apply.setFixedWidth(46)
        btn_apply.setStyleSheet(
            "QPushButton { background:#1e3a70; color:#ddeeff; border:1px solid #2a5090;"
            "padding:2px 4px; font-size:10px; }"
            "QPushButton:hover { background:#2a4e90; }")
        btn_apply.clicked.connect(self._on_apply_noise)

        top_row = QWidget()
        tr = QHBoxLayout(top_row)
        tr.setContentsMargins(0, 0, 0, 0); tr.setSpacing(4)
        tr.addWidget(noise_lbl); tr.addWidget(self._noise_combo, stretch=1)
        tr.addWidget(scale_lbl); tr.addWidget(self._scale_slider, stretch=1)
        tr.addWidget(self._scale_val); tr.addWidget(btn_apply)

        # ── Jitter row (Mixed mode only) ──────────────────────────────────
        jitter_lbl = QLabel("Jitter:")
        jitter_lbl.setStyleSheet("font-size:10px; color:#8899bb;")
        jit_init = int(self._jitter_amp * 100)
        self._jitter_val = QLabel(f"{self._jitter_amp:.2f}")
        self._jitter_val.setStyleSheet("font-size:10px; color:#aaddff; min-width:30px;")
        self._jitter_slider = _make_slider(5, 200, jit_init)
        self._jitter_slider.valueChanged.connect(
            lambda v: self._jitter_val.setText(f"{v/100:.2f}"))
        self._jitter_slider.sliderReleased.connect(self._on_jitter_released)
        self._jitter_row = QWidget()
        jr = QHBoxLayout(self._jitter_row)
        jr.setContentsMargins(0, 0, 0, 0); jr.setSpacing(4)
        jr.addWidget(jitter_lbl)
        jr.addWidget(self._jitter_slider, stretch=1)
        jr.addWidget(self._jitter_val)
        self._jitter_row.setVisible(False)  # shown only in Mixed mode
        self._noise_combo.currentTextChanged.connect(
            lambda t: self._jitter_row.setVisible(t == "Mixed"))

        # ── Source toggle ─────────────────────────────────────────────────
        src_lbl = QLabel("Source:")
        src_lbl.setStyleSheet("font-size:10px; color:#8899bb;")
        self._source_combo = QComboBox()
        self._source_combo.addItems(["Synthetic", "Real tile"])
        self._source_combo.setStyleSheet(
            "background:#1a2a50; color:#ddeeff; font-size:10px;")
        self._source_combo.setToolTip(
            "Synthetic: random noise fields\n"
            "Real tile: reads actual erosion/flow/height masks for the current tile")
        self._source_combo.currentTextChanged.connect(
            lambda _: self._render_fast())
        src_row = QWidget(); srcr = QHBoxLayout(src_row)
        srcr.setContentsMargins(0, 0, 0, 0)
        srcr.addWidget(src_lbl); srcr.addWidget(self._source_combo, stretch=1)

        # ── Category filter ───────────────────────────────────────────────
        filter_lbl = QLabel("Show:")
        filter_lbl.setStyleSheet("font-size:10px; color:#8899bb;")
        self._filter_combo = QComboBox()
        self._filter_combo.addItems([
            "All", "Noise only", "Erosion only", "Moisture only", "Altitude only",
        ])
        self._filter_combo.setStyleSheet("background:#1a2a50; color:#ddeeff; font-size:10px;")
        self._filter_combo.currentTextChanged.connect(lambda _: self._render_fast())
        filter_row = QWidget(); fr = QHBoxLayout(filter_row)
        fr.setContentsMargins(0, 0, 0, 0)
        fr.addWidget(filter_lbl); fr.addWidget(self._filter_combo, stretch=1)

        # ── Per-condition sliders (in a scroll area) ──────────────────────
        self._sliders_widget = QWidget()
        self._sliders_widget.setStyleSheet("background:#111a2e;")
        self._sliders_layout = QVBoxLayout(self._sliders_widget)
        self._sliders_layout.setContentsMargins(4, 2, 4, 2)
        self._sliders_layout.setSpacing(2)

        from PyQt6.QtWidgets import QScrollArea
        scroll = QScrollArea()
        scroll.setWidget(self._sliders_widget)
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(110)
        scroll.setStyleSheet(
            "QScrollArea { background:#111a2e; border:1px solid #1a3060; }"
            "QScrollBar:vertical { width:8px; background:#0d1525; }"
            "QScrollBar::handle:vertical { background:#2a4070; }")

        # ── Legend ────────────────────────────────────────────────────────
        self._legend = QLabel()
        self._legend.setStyleSheet("font-size:9px; color:#aaaaaa;")
        self._legend.setWordWrap(True)

        # ── Tile label ────────────────────────────────────────────────────
        self._tile_label = QLabel(f"Tile ({self._tile_x}, {self._tile_z})")
        self._tile_label.setStyleSheet(
            "font-size:9px; color:#6688aa; background:#0d1525; "
            "padding:1px 4px; border-radius:2px;")
        self._tile_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ── Biome coverage label ───────────────────────────────────────────
        self._biome_cov_label = QLabel("Biome: —")
        self._biome_cov_label.setStyleSheet(
            "font-size:8px; color:#7799aa; background:#0a1020; "
            "padding:2px 4px; border-radius:2px;")
        self._biome_cov_label.setWordWrap(True)
        self._biome_cov_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # ── Smooth toggle ─────────────────────────────────────────────────
        from PyQt6.QtWidgets import QCheckBox
        self._smooth_check = QCheckBox("Smooth")
        self._smooth_check.setStyleSheet(
            "QCheckBox { font-size:10px; color:#ccd8ee; }"
            "QCheckBox::indicator { width:13px; height:13px; "
            "border:1px solid #4466aa; background:#1a2a50; border-radius:2px; }"
            "QCheckBox::indicator:checked { background:#3a7aee; border-color:#5599ff; }"
        )
        self._smooth_check.stateChanged.connect(lambda _: self._render_fast())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self._tile_label)
        layout.addWidget(self._biome_cov_label)
        layout.addWidget(self._canvas, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Populate biome coverage after UI is shown
        QTimer.singleShot(200, self._update_biome_coverage)

        top_row_w = QWidget(); trw = QHBoxLayout(top_row_w)
        trw.setContentsMargins(0,0,0,0); trw.setSpacing(4)
        trw.addWidget(top_row, stretch=1)
        trw.addWidget(self._smooth_check)
        layout.addWidget(top_row_w)
        layout.addWidget(self._jitter_row)

        layout.addWidget(src_row)
        layout.addWidget(filter_row)
        layout.addWidget(scroll)
        layout.addWidget(self._legend)

    # ------------------------------------------------------------------
    def _update_biome_coverage(self) -> None:
        """Read override.tif for the current tile and update biome coverage label."""
        if not hasattr(self, "_biome_cov_label"):
            return
        try:
            import rasterio
            from rasterio.windows import Window
            path = OVERRIDE_TIF
            with rasterio.open(str(path)) as src:
                win  = Window(self._tile_x * TILE_SIZE, self._tile_z * TILE_SIZE,
                              TILE_SIZE, TILE_SIZE)
                data = src.read(1, window=win).ravel()
            unique, counts = np.unique(data, return_counts=True)
            total = data.size
            # Sort by count descending
            pairs = sorted(zip(counts, unique), reverse=True)
            parts = []
            none_pct = 0
            for cnt, val in pairs[:5]:
                pct = int(cnt * 100 / total)
                if pct == 0:
                    continue
                name = _ZONE_NAMES.get(int(val), f"zone{val}")
                if name == "none":
                    none_pct = pct
                    continue
                parts.append(f"{name}: {pct}%")
            if not parts:
                txt = f"unassigned ({none_pct}% zone 0 — pipeline auto-assigns)"
            else:
                if none_pct:
                    parts.append(f"unassigned: {none_pct}%")
                txt = "  ·  ".join(parts)
            self._biome_cov_label.setText(f"Biome: {txt}")
        except Exception as exc:
            self._biome_cov_label.setText(f"Biome: (err: {exc})")

    # ------------------------------------------------------------------
    def apply_sparse_overrides(self, biome: str) -> None:
        """
        Load sparse_overrides for a biome from thresholds.json and apply
        to active thresholds + sliders. Resets to BASE_THRESHOLDS if no
        override entry exists for the biome.
        """
        try:
            cfg       = _load_thresholds()
            overrides = cfg.get("sparse_overrides", {}).get(biome, {})
            if overrides:
                for cond, thr in overrides.items():
                    self._thresholds[cond] = float(thr)
            else:
                # Reset to defaults for biomes with no specific override
                self._thresholds = dict(self._BASE_THRESHOLDS)
                overrides = {}  # nothing extra to apply
            # Sync sliders for every condition we touched
            for cond, thr in self._thresholds.items():
                if cond in self._cond_sliders:
                    cov = int(round((1.0 - float(thr)) * 100))
                    self._cond_sliders[cond].blockSignals(True)
                    self._cond_sliders[cond].setValue(max(5, min(95, cov)))
                    self._cond_sliders[cond].blockSignals(False)
                    if cond in self._cond_labels:
                        self._cond_labels[cond].setText(f"{cov}%")
        except Exception as exc:
            print(f"[Viz] sparse_overrides load failed: {exc}", flush=True)

    # ------------------------------------------------------------------
    def _build_condition_sliders(self, conditions: list[str]) -> None:
        """Rebuild per-condition sliders for the given unique condition list."""
        # Clear old
        for i in reversed(range(self._sliders_layout.count())):
            w = self._sliders_layout.itemAt(i).widget()
            if w:
                w.setParent(None)
        self._cond_sliders.clear()
        self._cond_labels.clear()
        self._base_cov_label: QLabel | None = None

        _lbl_style = "font-size:9px; color:#8899bb; min-width:64px;"
        _val_style = "font-size:9px; color:#aaddff; min-width:28px;"

        for cond in conditions:
            row = QWidget(); rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(3)

            if cond == "base":
                # Find the base block name from current palette
                base_block = next(
                    (s for s, _, c in self._palette if c == "base"), "?")
                hex_c = _BLOCK_COLOURS.get(base_block, "#888888")
                swatch = QLabel("■")
                swatch.setStyleSheet(f"font-size:12px; color:{hex_c};")
                name_lbl = QLabel(f"base ({base_block}):")
                name_lbl.setStyleSheet("font-size:9px; color:#aaaacc; min-width:64px;")
                self._base_cov_label = QLabel("–%")
                self._base_cov_label.setStyleSheet(
                    "font-size:9px; color:#aaddff; min-width:28px;")
                spacer = QLabel("(fallback — no threshold)")
                spacer.setStyleSheet("font-size:8px; color:#556677;")
                rl.addWidget(swatch)
                rl.addWidget(name_lbl)
                rl.addWidget(spacer, stretch=1)
                rl.addWidget(self._base_cov_label)
                self._sliders_layout.addWidget(row)
                continue

            thr  = self._thresholds.get(cond, 0.65)
            cov  = int(round((1.0 - thr) * 100))

            name_lbl = QLabel(cond + ":")
            name_lbl.setStyleSheet(_lbl_style)
            val_lbl  = QLabel(f"{cov}%")
            val_lbl.setStyleSheet(_val_style)

            sl = _make_slider(5, 95, cov)
            sl.valueChanged.connect(
                lambda v, lbl=val_lbl: lbl.setText(f"{v}%"))
            sl.sliderReleased.connect(
                lambda c=cond, s=sl: self._on_cond_slider_released(c, s))

            rl.addWidget(name_lbl)
            rl.addWidget(sl, stretch=1)
            rl.addWidget(val_lbl)
            self._sliders_layout.addWidget(row)
            self._cond_sliders[cond] = sl
            self._cond_labels[cond]  = val_lbl

    # ------------------------------------------------------------------
    def update_palette(self, palette: list) -> None:
        self._palette = palette
        unique_conds  = list(dict.fromkeys(tag for _, _, tag in palette))
        self._build_condition_sliders(unique_conds)
        self._render_fast()

    # ------------------------------------------------------------------
    def _on_apply_noise(self) -> None:
        """Force noise regen (expensive) then re-render."""
        self._cache_key   = None   # invalidate cache
        self._noise_cache = None
        self._noise_type  = self._noise_combo.currentText()
        self._scale       = self._scale_slider.value()
        self._jitter_amp  = self._jitter_slider.value() / 100.0
        self._render_fast()

    def _on_jitter_released(self) -> None:
        """Jitter slider released — regenerate Mixed field."""
        self._cache_key   = None
        self._noise_cache = None
        self._jitter_amp  = self._jitter_slider.value() / 100.0
        self._render_fast()

    def _on_cond_slider_released(self, cond: str, slider: QSlider) -> None:
        cov = slider.value() / 100.0
        self._thresholds[cond] = float(np.clip(1.0 - cov, 0.01, 0.99))
        self._render_fast()

    # ------------------------------------------------------------------
    @staticmethod
    def _gen_field(sz: int, scale: float, noise_type: str, seed: int,
                   jitter: float = 0.8) -> np.ndarray:
        """Returns (sz, sz) float32 in [0, 1]."""
        if noise_type in ("OpenSimplex", "Fractal"):
            import opensimplex as ox
            ox.seed(seed)
            xs = np.linspace(0.0, sz / scale, sz, dtype=np.double)
            ys = np.linspace(0.0, sz / scale, sz, dtype=np.double)
            f  = ox.noise2array(xs, ys).astype(np.float32)
            if noise_type == "Fractal":
                ox.seed(seed + 1000)
                xs2 = np.linspace(0.0, sz / (scale * 0.4), sz, dtype=np.double)
                ys2 = np.linspace(0.0, sz / (scale * 0.4), sz, dtype=np.double)
                f  += 0.45 * ox.noise2array(xs2, ys2).astype(np.float32)
                ox.seed(seed + 2000)
                xs3 = np.linspace(0.0, sz / (scale * 0.15), sz, dtype=np.double)
                ys3 = np.linspace(0.0, sz / (scale * 0.15), sz, dtype=np.double)
                f  += 0.2 * ox.noise2array(xs3, ys3).astype(np.float32)
                f  /= 1.65
        elif noise_type == "Voronoi":
            # Worley F2−F1: organic cell-boundary pattern (not distance rings)
            from scipy.spatial import cKDTree
            rng   = np.random.RandomState(seed)
            n_pts = max(10, int((sz / max(scale * 0.5, 1)) ** 2 * 3))
            n_pts = min(n_pts, 800)
            pts   = rng.rand(n_pts, 2) * sz
            gy, gx = np.mgrid[0:sz, 0:sz]
            grid   = np.stack([gx.ravel(), gy.ravel()], axis=1).astype(np.float64)
            dists, _ = cKDTree(pts).query(grid, k=2)
            # F2−F1 gives values near 0 at cell centres, high at boundaries
            f = (dists[:, 1] - dists[:, 0]).reshape(sz, sz).astype(np.float32)
        elif noise_type == "Gaussian":
            from scipy.ndimage import gaussian_filter
            rng   = np.random.RandomState(seed)
            raw   = rng.random((sz, sz)).astype(np.float32)
            sigma = max(1.0, scale / 40.0)   # gentler sigma; was /15 → went flat
            blurred = gaussian_filter(raw, sigma=sigma).astype(np.float32)
            # Blend with a fine-grained layer so high sigma never goes uniform
            fine  = gaussian_filter(raw, sigma=max(0.5, sigma * 0.1)).astype(np.float32)
            f = blurred * 0.80 + fine * 0.20
        elif noise_type == "Mixed":
            def _norm01(a: np.ndarray) -> np.ndarray:
                lo, hi = a.min(), a.max()
                return (a - lo) / (hi - lo + 1e-9)
            # Macro: large-scale Simplex defines blob shapes
            import opensimplex as ox
            ox.seed(seed)
            xs = np.linspace(0.0, sz / scale, sz, dtype=np.double)
            ys = np.linspace(0.0, sz / scale, sz, dtype=np.double)
            macro = _norm01(ox.noise2array(xs, ys).astype(np.float32))
            # Fine: tight Gaussian grain — sigma stays small regardless of scale
            # so it always reads as pixel-level texture, not another blob layer
            from scipy.ndimage import gaussian_filter
            rng2  = np.random.RandomState(seed + 77)
            raw   = rng2.random((sz, sz)).astype(np.float32)
            sigma = max(0.8, scale / 60.0)
            fine  = _norm01(gaussian_filter(raw, sigma=sigma).astype(np.float32))
            # Threshold jitter: add CENTERED grain to the macro field.
            # Pixels far from any threshold boundary are unaffected — blob cores
            # stay solid. Pixels near a boundary get randomly pushed above/below
            # it, creating ragged noisy edges while preserving large blob shapes.
            f = macro + (fine - 0.5) * jitter
        else:
            f = np.zeros((sz, sz), dtype=np.float32)

        lo, hi = f.min(), f.max()
        return (f - lo) / (hi - lo + 1e-9)

    # ------------------------------------------------------------------
    # Condition groupings for category filter
    _COND_GROUPS: dict[str, set] = {
        "Noise only":    {"noise", "noise2", "noise3", "noise4", "base"},
        "Erosion only":  {"erosion", "erosion2", "base"},
        "Moisture only": {"moisture", "moisture2", "base"},
        "Altitude only": {"altitude", "base"},
    }

    def _render_fast(self) -> None:
        """Re-render using cached noise fields (fast — only recomputes block assignment)."""
        if not self._palette:
            return
        sz = self._PREVIEW_PX
        sc = float(self._scale)
        nt = self._noise_type

        use_real = (hasattr(self, "_source_combo") and
                    self._source_combo.currentText() == "Real tile")

        # Cache key includes jitter for Mixed mode so slider changes regenerate
        jitter = self._jitter_amp
        key = (nt, sc, jitter if nt == "Mixed" else 0.0)
        if self._noise_cache is None or self._cache_key != key:
            self._noise_cache = {
                "noise":    self._gen_field(sz, sc, nt, seed=42,       jitter=jitter),
                "noise2":   self._gen_field(sz, sc, nt, seed=42 + 313, jitter=jitter),
                "noise3":   self._gen_field(sz, sc, nt, seed=42 + 999, jitter=jitter),
                "noise4":   self._gen_field(sz, sc, nt, seed=42 + 777, jitter=jitter),
            }
            if not use_real:
                self._noise_cache.update({
                    "erosion":  self._gen_field(sz, sc * 0.6, nt, seed=43,       jitter=jitter),
                    "erosion2": self._gen_field(sz, sc * 0.6, nt, seed=43 + 200, jitter=jitter),
                    "moisture": self._gen_field(sz, sc * 1.4, nt, seed=44,       jitter=jitter),
                    "moisture2":self._gen_field(sz, sc * 1.4, nt, seed=44 + 400, jitter=jitter),
                    "altitude": self._gen_field(sz, sc * 2.0, nt, seed=45,       jitter=jitter),
                })
            self._cache_key = key

        noise_fields = dict(self._noise_cache)

        if use_real:
            real = self._get_real_fields()
            if real is not None:
                noise_fields.update(real)
                # Recalibrate thresholds for real fields using pipeline raw values
                try:
                    cfg = _load_thresholds()
                    bm  = cfg.get("block_mixing", {})
                    # Load raw mask data to compute percentile thresholds
                    import rasterio
                    from rasterio.windows import Window

                    def _field_threshold(mask_name: str, raw_thr: float,
                                         invert: bool = False) -> float:
                        """
                        Percentile-based threshold: compute what fraction of
                        real pixels exceed raw_thr, then return normalized
                        threshold that produces the same coverage on [0,1] field.
                        """
                        path = MASKS_DIR / f"{mask_name}.tif"
                        try:
                            with rasterio.open(str(path)) as src:
                                win = Window(
                                    self._tile_x * TILE_SIZE,
                                    self._tile_z * TILE_SIZE,
                                    TILE_SIZE, TILE_SIZE)
                                raw = src.read(1, window=win).astype(np.float32)
                            if invert:
                                frac_above = float((raw <= raw_thr).mean())
                            else:
                                frac_above = float((raw >= raw_thr).mean())
                            # norm threshold = fraction of [0,1] field NOT covered
                            return float(np.clip(1.0 - frac_above, 0.01, 0.99))
                        except Exception:
                            return 0.60

                    noise_fields_real_thr = {
                        "erosion":   _field_threshold(
                            "erosion", bm.get("erosion_threshold",  0.004)),
                        "erosion2":  _field_threshold(
                            "erosion", bm.get("erosion2_threshold", 0.010)),
                        "moisture":  _field_threshold(
                            "flow",    bm.get("moisture_threshold",  0.005)),
                        "moisture2": _field_threshold(
                            "flow",    bm.get("moisture2_threshold", 0.020)),
                        # height mask: LOW raw = HIGH terrain, so invert
                        "altitude":  _field_threshold(
                            "height",  bm.get("altitude_threshold",  0.72),
                            invert=True),
                    }
                    # Merge real thresholds into active thresholds for erosion/moisture/altitude
                    for cond, thr in noise_fields_real_thr.items():
                        self._thresholds[cond] = thr
                        # Update sliders to reflect real coverage
                        if cond in self._cond_sliders:
                            cov = int(round((1.0 - thr) * 100))
                            self._cond_sliders[cond].blockSignals(True)
                            self._cond_sliders[cond].setValue(
                                max(5, min(95, cov)))
                            self._cond_sliders[cond].blockSignals(False)
                            if cond in self._cond_labels:
                                self._cond_labels[cond].setText(f"{cov}%")
                except Exception as exc:
                    print(f"[Viz] threshold recalibration failed: {exc}",
                          flush=True)
            else:
                # Fall back to synthetic if real load failed
                for cond in ("erosion","erosion2","moisture","moisture2","altitude"):
                    if cond not in noise_fields:
                        seed_map = {"erosion":43,"erosion2":243,
                                    "moisture":44,"moisture2":444,"altitude":45}
                        sc_map   = {"erosion":0.6,"erosion2":0.6,
                                    "moisture":1.4,"moisture2":1.4,"altitude":2.0}
                        noise_fields[cond] = self._gen_field(
                            sz, sc * sc_map[cond], nt, seed=seed_map[cond])

        # Category filter — restrict which conditions are active
        filter_mode = self._filter_combo.currentText()
        active_conds = self._COND_GROUPS.get(filter_mode, None)  # None = All

        # {condition -> surface_block} last entry per tag wins
        cond_block: dict[str, str] = {}
        for surf, _sub, tag in self._palette:
            cond_block[tag] = surf

        # Apply PRIORITY order (later = higher priority, overwrites)
        block_img = np.full((sz, sz), "", dtype=object)
        for cond in _APPLY_PRIORITY:
            if cond not in cond_block:
                continue
            if active_conds is not None and cond not in active_conds:
                continue   # filtered out
            block_name = cond_block[cond]
            if cond == "base":
                block_img[:] = block_name
            else:
                thr   = self._thresholds.get(cond, 0.5)
                field = noise_fields.get(cond, noise_fields["noise"])
                block_img[field >= thr] = block_name

        # Colour map + legend
        unique_surf = list(dict.fromkeys(e[0] for e in self._palette))
        # Find which block is base
        base_block  = next((s for s, _, c in self._palette if c == "base"), None)
        rgb = np.zeros((sz, sz, 3), dtype=np.float32)
        legend_parts = []
        for block in unique_surf:
            hex_c = _BLOCK_COLOURS.get(block, "#888888")
            r = int(hex_c[1:3], 16) / 255.0
            g = int(hex_c[3:5], 16) / 255.0
            b = int(hex_c[5:7], 16) / 255.0
            mask = block_img == block
            rgb[mask] = [r, g, b]
            pct = int(mask.sum() * 100 / (sz * sz))
            tag = " (base)" if block == base_block else ""
            if pct > 0:
                legend_parts.append(
                    f'<span style="color:{hex_c};">&#9632;</span>'
                    f' <span style="color:#cccccc;">{block[:14]}{tag}&nbsp;&nbsp;{pct}%</span>'
                )
            # Update base coverage label in sliders panel
            if block == base_block and hasattr(self, "_base_cov_label") \
                    and self._base_cov_label is not None:
                self._base_cov_label.setText(f"{pct}%")

        interp = ("bilinear"
                  if hasattr(self, "_smooth_check") and self._smooth_check.isChecked()
                  else "nearest")
        self._ax.cla()
        self._ax.axis("off")
        self._ax.imshow(rgb, origin="upper", aspect="equal", interpolation=interp)
        self._canvas.draw()
        self._legend.setTextFormat(Qt.TextFormat.RichText)
        self._legend.setText("<br>".join(legend_parts))


class SurfaceBlockEditorWidget(QWidget):
    """Editable per-biome surface block palette table for Tab B."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._palettes: dict = {}   # biome -> list of [surf, sub, condition]
        self._dirty: dict   = {}    # biome -> modified list (not yet saved)
        self._load_palettes()
        self._build_ui()

    # ------------------------------------------------------------------
    def _load_palettes(self) -> None:
        try:
            from core.surface_decorator import BIOME_BLOCK_PALETTES
            # Deep-copy as mutable lists
            self._palettes = {
                k: [list(entry) for entry in v]
                for k, v in BIOME_BLOCK_PALETTES.items()
            }
        except Exception as exc:
            print(f"[SurfaceBlockEditor] could not load palettes: {exc}", flush=True)
            self._palettes = {}

        # Apply any saved overrides from thresholds.json
        try:
            cfg = _load_thresholds()
            for biome, entries in cfg.get("surface_palettes", {}).items():
                if biome in self._palettes:
                    self._palettes[biome] = [list(e) for e in entries]
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setStyleSheet("background-color: #16213e; color: #cccccc;")

        # Biome selector (full width across top)
        self._biome_combo = QComboBox()
        self._biome_combo.addItems(sorted(self._palettes.keys()))
        self._biome_combo.setStyleSheet(
            "background:#1a2a50; color:#ddeeff; font-size:11px; padding:2px;"
        )
        self._biome_combo.currentTextChanged.connect(self._on_biome_changed)

        # ---- Left side: editable palette table ----
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Surface block", "Sub-surface", "Condition"])
        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setSectionResizeMode(0, hdr.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, hdr.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, hdr.ResizeMode.Stretch)
        hdr.setMinimumSectionSize(60)
        self._table.setStyleSheet(
            "QTableWidget { background:#0d1b2a; color:#cccccc; font-size:11px; "
            "gridline-color:#1a3a5c; }"
            "QHeaderView::section { background:#0a1525; color:#8899bb; font-size:10px; }"
        )
        self._table.itemChanged.connect(self._on_item_changed)

        btn_add   = QPushButton("+ Row")
        btn_del   = QPushButton("– Row")
        btn_save  = QPushButton("Save")
        btn_reset = QPushButton("Reset")
        for btn in (btn_add, btn_del, btn_save, btn_reset):
            btn.setStyleSheet(
                "QPushButton { background:#1e3a70; color:#ddeeff; "
                "border:1px solid #2a5090; padding:2px 6px; font-size:10px; }"
                "QPushButton:hover { background:#2a4e90; }"
            )
        btn_add.clicked.connect(self._add_row)
        btn_del.clicked.connect(self._del_row)
        btn_save.clicked.connect(self._on_save)
        btn_reset.clicked.connect(self._on_reset)

        btn_bar = QWidget()
        bl = QHBoxLayout(btn_bar)
        bl.setContentsMargins(0, 2, 0, 2)
        bl.addWidget(btn_add); bl.addWidget(btn_del)
        bl.addStretch()
        bl.addWidget(btn_save); bl.addWidget(btn_reset)

        left = QWidget()
        ll   = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self._table, stretch=1)
        ll.addWidget(btn_bar)

        # ---- Right side: visualiser ----
        self._viz = SurfaceBlockVisualizerWidget()

        # ---- Horizontal split ----
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.setChildrenCollapsible(False)
        h_split.addWidget(left)
        h_split.addWidget(self._viz)
        h_split.setSizes([220, 240])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._biome_combo)
        layout.addWidget(h_split, stretch=1)

        # Populate first biome + seed visualiser
        if self._palettes:
            first = self._biome_combo.currentText()
            self._populate_table(first)
            self._viz.update_palette(self._palettes.get(first, []))

    # ------------------------------------------------------------------
    def _populate_table(self, biome: str) -> None:
        from PyQt6.QtWidgets import QComboBox as _QCB
        entries = self._dirty.get(biome, self._palettes.get(biome, []))
        self._table.blockSignals(True)
        self._table.setRowCount(len(entries))
        for row, (surf, sub, cond) in enumerate(entries):
            self._table.setItem(row, 0, QTableWidgetItem(surf))
            self._table.setItem(row, 1, QTableWidgetItem(sub))
            # Condition: non-editable QTableWidgetItem (could be combo later)
            cond_item = QTableWidgetItem(cond)
            self._table.setItem(row, 2, cond_item)
        self._table.blockSignals(False)

    # ------------------------------------------------------------------
    def _current_entries(self) -> list:
        entries = []
        for row in range(self._table.rowCount()):
            s = (self._table.item(row, 0) or QTableWidgetItem("stone")).text().strip()
            b = (self._table.item(row, 1) or QTableWidgetItem("stone")).text().strip()
            c = (self._table.item(row, 2) or QTableWidgetItem("base")).text().strip()
            entries.append([s, b, c])
        return entries

    # ------------------------------------------------------------------
    def _on_biome_changed(self, biome: str) -> None:
        self._populate_table(biome)
        self._viz.update_palette(self._palettes.get(biome, []))
        self._viz.apply_sparse_overrides(biome)

    def _on_item_changed(self) -> None:
        biome = self._biome_combo.currentText()
        entries = self._current_entries()
        self._dirty[biome] = entries
        self._viz.update_palette(entries)

    def _add_row(self) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem("stone"))
        self._table.setItem(row, 1, QTableWidgetItem("stone"))
        self._table.setItem(row, 2, QTableWidgetItem("noise"))

    def _del_row(self) -> None:
        row = self._table.currentRow()
        if row >= 0:
            self._table.removeRow(row)
            biome = self._biome_combo.currentText()
            self._dirty[biome] = self._current_entries()

    def _on_save(self) -> None:
        # Flush all dirty biomes to thresholds.json
        try:
            cfg = _load_thresholds()
            overrides = cfg.get("surface_palettes", {})
            overrides.update(self._dirty)
            cfg["surface_palettes"] = overrides
            _save_thresholds(cfg)
            self._dirty.clear()
            print("[SurfaceBlockEditor] saved.", flush=True)
        except Exception as exc:
            print(f"[SurfaceBlockEditor] save failed: {exc}", flush=True)

    def _on_reset(self) -> None:
        biome = self._biome_combo.currentText()
        self._dirty.pop(biome, None)
        self._populate_table(biome)


# ---------------------------------------------------------------------------
# Control Panel
# ---------------------------------------------------------------------------
class ControlPanel(QWidget):
    """Bottom-right panel: tabbed controls. Tab C (Ocean/Depth) is live."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(200, 150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background-color: #16213e; color: #cccccc; font-size: 12px;")

        # Callback: (min_ocean_depth, transition_px) -> None
        self.on_ocean_changed = None

        # Load initial values from thresholds.json
        try:
            cfg = _load_thresholds()
            _od = cfg.get("ocean_depth", {})
            init_depth  = int(_od.get("min_depth",       15))
            init_trans  = int(_od.get("transition_px",   30))
        except Exception:
            init_depth, init_trans = 15, 30

        tabs = QTabWidget(self)
        tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #1a3060; background: #16213e; }"
            "QTabBar::tab { background: #1a2a50; color: #8899bb; padding: 4px 10px; }"
            "QTabBar::tab:selected { background: #1e3a70; color: #ddeeff; }"
        )

        # --- Tab A: Spline Editor ---
        self._spline_editor = SplineEditorWidget()
        self._spline_editor.on_applied = self._on_spline_applied
        tabs.addTab(self._spline_editor, "Spline")

        # --- Tab B: Surface Block Editor ---
        self._surface_editor = SurfaceBlockEditorWidget()
        tabs.addTab(self._surface_editor, "Surface Blocks")

        # --- Tab C: Ocean/Depth ---
        tab_c = QWidget()
        tab_c.setStyleSheet("background-color: #16213e;")
        form  = QFormLayout(tab_c)
        form.setContentsMargins(12, 10, 12, 10)
        form.setSpacing(10)

        self._depth_lbl  = QLabel(f"{init_depth} blocks")
        self._trans_lbl  = QLabel(f"{init_trans} px")
        self._depth_lbl.setStyleSheet("color: #aaddff; min-width: 60px;")
        self._trans_lbl.setStyleSheet("color: #aaddff; min-width: 60px;")

        self._depth_slider = _make_slider(0, 30,  init_depth)
        self._trans_slider = _make_slider(0, 200, init_trans)

        self._depth_slider.valueChanged.connect(
            lambda v: self._depth_lbl.setText(f"{v} blocks"))
        self._trans_slider.valueChanged.connect(
            lambda v: self._trans_lbl.setText(f"{v} px"))

        self._depth_slider.sliderReleased.connect(self._on_released)
        self._trans_slider.sliderReleased.connect(self._on_released)

        row_d = QWidget(); rl = QHBoxLayout(row_d); rl.setContentsMargins(0,0,0,0)
        rl.addWidget(self._depth_slider); rl.addWidget(self._depth_lbl)
        row_t = QWidget(); rl2 = QHBoxLayout(row_t); rl2.setContentsMargins(0,0,0,0)
        rl2.addWidget(self._trans_slider); rl2.addWidget(self._trans_lbl)

        form.addRow("min ocean depth", row_d)
        form.addRow("transition px",   row_t)
        tabs.addTab(tab_c, "Ocean/Depth")
        tabs.setCurrentIndex(2)  # show Tab C by default

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(tabs)

    def _on_released(self) -> None:
        if self.on_ocean_changed:
            self.on_ocean_changed(
                self._depth_slider.value(),
                self._trans_slider.value(),
            )

    def _on_spline_applied(self, gaea_in: list, mc_y_out: list) -> None:
        """Rebuild global LUT and notify main window to refresh cross-section."""
        global _LUT
        _LUT = np.clip(
            np.interp(
                np.arange(65536, dtype=np.float64),
                np.array(gaea_in,  dtype=np.float64),
                np.array(mc_y_out, dtype=np.float64),
            ),
            -64, 703,
        ).astype(np.int16)
        if self.on_ocean_changed:
            # Reuse the ocean callback to trigger a cross-section refresh
            self.on_ocean_changed(self._depth_slider.value(),
                                  self._trans_slider.value())

    @property
    def min_ocean_depth(self) -> int:
        return self._depth_slider.value()

    @property
    def transition_px(self) -> int:
        return self._trans_slider.value()


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------
class TerrainPreviewWindow(QMainWindow):
    def __init__(self, tile_x: int, tile_z: int):
        super().__init__()
        self.tile_x = tile_x
        self.tile_z = tile_z

        self.setWindowTitle(f"Vandir Terrain Preview  —  Tile ({tile_x}, {tile_z})")
        self.resize(1200, 720)
        self._build_layout()

    def _build_layout(self) -> None:
        h_split = QSplitter(Qt.Orientation.Horizontal)
        h_split.setChildrenCollapsible(False)

        # ── Map panel + mode toolbar ──────────────────────────────────────
        self.map_panel = MapPanel(self.tile_x, self.tile_z)
        self.map_panel.on_row_selected  = self._on_map_click
        self.map_panel.on_center_changed = self._on_navigate

        # Mode selector toolbar sits above the map
        mode_bar = QWidget()
        mode_bar.setStyleSheet("background:#0d1525;")
        mb_layout = QHBoxLayout(mode_bar)
        mb_layout.setContentsMargins(4, 2, 4, 2)
        mb_layout.setSpacing(6)

        mode_lbl = QLabel("View:")
        mode_lbl.setStyleSheet("color:#8899bb; font-size:10px;")
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["surface_height", "biome", "slope", "surface_block"])
        self._mode_combo.setStyleSheet(
            "background:#1a2a50; color:#ddeeff; font-size:10px; padding:1px 4px;")
        self._mode_combo.setToolTip(
            "surface_height — terrain colormap\n"
            "biome           — override zone false-colour\n"
            "slope           — Sobel gradient (hot)\n"
            "surface_block   — base block colour per zone")
        self._mode_combo.currentTextChanged.connect(self._on_map_mode_changed)

        zoom_hint = QLabel("scroll=zoom  drag=pan  dbl-click=reset")
        zoom_hint.setStyleSheet("color:#445566; font-size:9px;")

        # Tile navigation spinboxes
        nav_lbl = QLabel("Tile:")
        nav_lbl.setStyleSheet("color:#8899bb; font-size:10px;")
        self._nav_x = QSpinBox()
        self._nav_x.setRange(0, 97)
        self._nav_x.setValue(self.tile_x)
        self._nav_x.setPrefix("X:")
        self._nav_x.setFixedWidth(72)
        self._nav_x.setStyleSheet("background:#1a2a50; color:#ddeeff; font-size:10px;")
        self._nav_z = QSpinBox()
        self._nav_z.setRange(0, 97)
        self._nav_z.setValue(self.tile_z)
        self._nav_z.setPrefix("Z:")
        self._nav_z.setFixedWidth(72)
        self._nav_z.setStyleSheet("background:#1a2a50; color:#ddeeff; font-size:10px;")
        nav_go = QPushButton("Go")
        nav_go.setFixedWidth(36)
        nav_go.setStyleSheet(
            "QPushButton { background:#1e3a70; color:#ddeeff; font-size:10px; "
            "border:1px solid #2a5090; padding:1px 4px; }"
            "QPushButton:hover { background:#2a4e90; }")
        nav_go.clicked.connect(
            lambda: self._on_navigate(self._nav_x.value(), self._nav_z.value()))

        mb_layout.addWidget(mode_lbl)
        mb_layout.addWidget(self._mode_combo)
        mb_layout.addSpacing(12)
        mb_layout.addWidget(nav_lbl)
        mb_layout.addWidget(self._nav_x)
        mb_layout.addWidget(self._nav_z)
        mb_layout.addWidget(nav_go)
        mb_layout.addStretch()
        mb_layout.addWidget(zoom_hint)

        # Hover status bar
        self._hover_lbl = QLabel("hover over map for biome info")
        self._hover_lbl.setStyleSheet(
            "background:#0a1020; color:#6699bb; font-size:10px; "
            "font-family:Consolas; padding:2px 6px;")
        self._hover_lbl.setFixedHeight(20)
        self.map_panel.on_hover = self._on_map_hover

        map_container = QWidget()
        mc_layout = QVBoxLayout(map_container)
        mc_layout.setContentsMargins(0, 0, 0, 0)
        mc_layout.setSpacing(0)
        mc_layout.addWidget(mode_bar)
        mc_layout.addWidget(self.map_panel, stretch=1)
        mc_layout.addWidget(self._hover_lbl)

        h_split.addWidget(map_container)

        v_split = QSplitter(Qt.Orientation.Vertical)
        v_split.setChildrenCollapsible(False)

        self.cross_section = CrossSectionPanel()
        self.controls      = ControlPanel()
        self.controls.on_ocean_changed = self._on_ocean_changed
        v_split.addWidget(self.cross_section)
        v_split.addWidget(self.controls)
        v_split.setSizes([280, 440])

        h_split.addWidget(v_split)
        h_split.setSizes([700, 500])

        self.setCentralWidget(h_split)

        # Remember last profiled position for re-render on param changes
        self._last_profile = (self.tile_x, self.tile_z, 256)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        QTimer.singleShot(50, self._load_all)

    def _load_all(self) -> None:
        self.map_panel.load()
        self._refresh_cross_section()

    def _refresh_cross_section(self) -> None:
        tx, tz, row = self._last_profile
        self.cross_section.profile_row(
            tx, tz, row,
            min_ocean_depth=self.controls.min_ocean_depth,
        )

    def _on_map_click(self, tile_x: int, tile_z: int, row: int) -> None:
        self._last_profile = (tile_x, tile_z, row)
        self._refresh_cross_section()
        # Keep surface block visualiser in sync with the clicked tile
        try:
            self.controls._surface_editor._viz.set_tile(tile_x, tile_z)
        except Exception:
            pass

    def _on_map_mode_changed(self, mode: str) -> None:
        self.map_panel.set_mode(mode)

    def _on_ocean_changed(self, min_depth: int, transition_px: int) -> None:
        self._refresh_cross_section()

    def _on_map_hover(self, zone_name: str, mc_y: int,
                      tile_x: int, tile_z: int, px: int, pz: int) -> None:
        self._hover_lbl.setText(
            f"  tile ({tile_x},{tile_z})  px ({px},{pz})  |  "
            f"biome: {zone_name}  |  Y: {mc_y}")

    def _on_navigate(self, tile_x: int, tile_z: int) -> None:
        """Jump map to a new centre tile and sync spinboxes + title."""
        self.tile_x = tile_x
        self.tile_z = tile_z
        self._nav_x.setValue(tile_x)
        self._nav_z.setValue(tile_z)
        self.setWindowTitle(f"Vandir Terrain Preview  —  Tile ({tile_x}, {tile_z})")
        self.map_panel.set_center(tile_x, tile_z)
        self._last_profile = (tile_x, tile_z, 256)
        self._refresh_cross_section()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Vandir Terrain Preview Tool")
    parser.add_argument("--tile-x", type=int, default=32, help="Tile X index")
    parser.add_argument("--tile-z", type=int, default=2,  help="Tile Z index")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = TerrainPreviewWindow(args.tile_x, args.tile_z)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
