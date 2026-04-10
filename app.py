"""
app.py — Vandir World Generation GUI
/gui/app.py

Full 7-panel PyQt6 application. Spawns run_pipeline.py as a managed subprocess
and consumes its stdout JSON IPC stream.

Panels:
  1 — Pipeline Control    (paths, threads, start/stop)
  2 — Live Progress       (97×97 tile grid, % bar, ETA)
  3 — 2D Biome Map        (pannable/zoomable QGraphicsView, live tile updates)
  4 — Biome Override      (imported from panel4_override_painter.py — COMPLETE)
  5 — Parameter Sliders   (Whittaker threshold controls)
  6 — Log Console         (scrolling, filterable)
  7 — Tile Debug          (mask values table + biome stage readout)

Usage:
    python gui/app.py
    python gui/app.py --config config/thresholds.json  (pre-fill paths)

Architecture rules:
  - Pipeline runs as subprocess — never import /core/ from GUI thread
  - IPC reader lives in a QThread — never block the main thread
  - All GUI updates via Qt signals — no direct cross-thread widget access
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    QObject, QRunnable, QSettings, QSize, Qt, QThread, QThreadPool,
    QTimer, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import (
    QColor, QFont, QImage, QPainter, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDockWidget, QFileDialog,
    QFormLayout, QFrame, QGraphicsPixmapItem, QGraphicsScene,
    QGraphicsView, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSpinBox, QSplitter, QStatusBar,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# CONSTANTS
# ---------------------------------------------------------------------------

TILES_PER_AXIS = 97
TOTAL_TILES    = TILES_PER_AXIS * TILES_PER_AXIS   # 9409

TILE_CELL_PX   = 6    # pixels per tile cell in the progress grid

# Dark theme palette
DARK = {
    "bg":       "#1A1A2E",
    "panel":    "#16213E",
    "card":     "#0F3460",
    "accent":   "#E94560",
    "accent2":  "#C73652",
    "text":     "#E0E0F0",
    "muted":    "#888898",
    "border":   "#2A2A4A",
    "ok":       "#4CAF50",
    "warn":     "#FF9800",
    "err":      "#F44336",
}

STYLE = f"""
QMainWindow, QWidget {{ background: {DARK['bg']}; color: {DARK['text']}; }}
QTabWidget::pane {{ border: 1px solid {DARK['border']}; background: {DARK['panel']}; }}
QTabBar::tab {{ background: {DARK['card']}; color: {DARK['muted']}; padding: 6px 18px;
                border-radius: 3px 3px 0 0; margin-right: 2px; font-size: 12px; }}
QTabBar::tab:selected {{ background: {DARK['panel']}; color: {DARK['text']}; }}
QGroupBox {{ border: 1px solid {DARK['border']}; border-radius: 4px; margin-top: 8px;
             padding-top: 10px; font-size: 11px; color: {DARK['muted']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 8px; }}
QLineEdit {{ background: {DARK['card']}; border: 1px solid {DARK['border']};
             border-radius: 4px; padding: 5px 8px; color: {DARK['text']}; font-size: 12px; }}
QLineEdit:focus {{ border-color: {DARK['accent']}; }}
QPushButton {{ background: {DARK['card']}; border: 1px solid {DARK['border']};
               border-radius: 4px; padding: 6px 16px; color: {DARK['text']}; font-size: 12px; }}
QPushButton:hover {{ background: {DARK['accent']}; border-color: {DARK['accent2']};
                     color: #FFF; }}
QPushButton:pressed {{ background: {DARK['accent2']}; }}
QPushButton:disabled {{ color: {DARK['muted']}; background: {DARK['bg']}; }}
QLabel {{ color: {DARK['text']}; }}
QSlider::groove:horizontal {{ height: 4px; background: {DARK['border']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -5px 0;
                               background: {DARK['accent']}; border-radius: 7px; }}
QProgressBar {{ border: 1px solid {DARK['border']}; border-radius: 4px; text-align: center;
                background: {DARK['card']}; color: {DARK['text']}; font-size: 11px; }}
QProgressBar::chunk {{ background: {DARK['accent']}; border-radius: 3px; }}
QPlainTextEdit {{ background: {DARK['card']}; border: 1px solid {DARK['border']};
                  color: {DARK['text']}; font-family: Consolas, monospace; font-size: 11px; }}
QSpinBox {{ background: {DARK['card']}; border: 1px solid {DARK['border']};
            border-radius: 4px; padding: 4px; color: {DARK['text']}; }}
QComboBox {{ background: {DARK['card']}; border: 1px solid {DARK['border']};
             border-radius: 4px; padding: 5px 10px; color: {DARK['text']}; }}
QComboBox QAbstractItemView {{ background: {DARK['card']}; color: {DARK['text']};
                                selection-background-color: {DARK['accent']}; }}
QTableWidget {{ background: {DARK['card']}; color: {DARK['text']}; gridline-color: {DARK['border']};
                border: none; }}
QTableWidget::item:selected {{ background: {DARK['accent']}; }}
QHeaderView::section {{ background: {DARK['panel']}; color: {DARK['muted']};
                         border: 1px solid {DARK['border']}; padding: 4px; font-size: 11px; }}
QStatusBar {{ background: {DARK['panel']}; border-top: 1px solid {DARK['border']};
              color: {DARK['muted']}; font-size: 11px; }}
QScrollBar:vertical {{ background: {DARK['bg']}; width: 8px; }}
QScrollBar::handle:vertical {{ background: {DARK['border']}; border-radius: 4px; min-height: 20px; }}
"""


# ---------------------------------------------------------------------------
# IPC READER THREAD  (reads pipeline stdout in background)
# ---------------------------------------------------------------------------

class IpcReader(QThread):
    """Reads JSON lines from pipeline subprocess stdout, emits signals."""

    tile_start      = pyqtSignal(int, int)
    tile_complete   = pyqtSignal(int, int, list, int)   # tx, tz, biomes, elapsed_ms
    tile_error      = pyqtSignal(int, int, str)
    pipeline_done   = pyqtSignal(int, int, int, float)  # total, completed, errors, elapsed_s
    raw_line        = pyqtSignal(str)

    def __init__(self, proc: subprocess.Popen, parent=None):
        super().__init__(parent)
        self._proc = proc

    def run(self):
        for raw in self._proc.stdout:
            line = raw.strip()
            if not line:
                continue
            self.raw_line.emit(line)
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type", "")
            if t == "tile_start":
                self.tile_start.emit(obj["tile_x"], obj["tile_y"])
            elif t == "tile_complete":
                self.tile_complete.emit(
                    obj["tile_x"], obj["tile_y"],
                    obj.get("biomes", []), obj.get("elapsed_ms", 0),
                )
            elif t == "tile_error":
                self.tile_error.emit(obj["tile_x"], obj["tile_y"], obj.get("error", ""))
            elif t == "pipeline_complete":
                self.pipeline_done.emit(
                    obj.get("total_tiles", 0), obj.get("completed", 0),
                    obj.get("errors", 0), obj.get("elapsed_s", 0.0),
                )


# ---------------------------------------------------------------------------
# PANEL 1 — PIPELINE CONTROL
# ---------------------------------------------------------------------------

class Panel1Control(QWidget):
    run_requested  = pyqtSignal(dict)   # emits config dict when Start clicked
    stop_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_settings()

    def _browse(self, line: QLineEdit, is_dir=False):
        if is_dir:
            p = QFileDialog.getExistingDirectory(self, "Select Directory",
                                                  line.text() or str(Path.home()))
        else:
            p, _ = QFileDialog.getOpenFileName(self, "Select File",
                                                line.text() or str(Path.home()))
        if p:
            line.setText(p)

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # ---- Paths group ----
        grp = QGroupBox("File Paths")
        g = QFormLayout(grp)
        g.setSpacing(6)

        def _row(label, default=""):
            le = QLineEdit(default)
            btn = QPushButton("…")
            btn.setFixedWidth(30)
            row = QHBoxLayout()
            row.addWidget(le)
            row.addWidget(btn)
            w = QWidget()
            w.setLayout(row)
            g.addRow(label, w)
            return le, btn

        self.le_config,  b = _row("Config (thresholds.json):")
        b.clicked.connect(lambda: self._browse(self.le_config))

        self.le_masks,   b = _row("Masks directory:")
        b.clicked.connect(lambda: self._browse(self.le_masks, is_dir=True))

        self.le_schem,   b = _row("Schematic index:")
        b.clicked.connect(lambda: self._browse(self.le_schem))

        self.le_output,  b = _row("Output directory:")
        b.clicked.connect(lambda: self._browse(self.le_output, is_dir=True))

        lay.addWidget(grp)

        # ---- Run options ----
        grp2 = QGroupBox("Run Options")
        g2 = QFormLayout(grp2)
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 32)
        self.spin_threads.setValue(max(1, (os.cpu_count() or 4)))
        g2.addRow("Worker threads:", self.spin_threads)

        self.chk_dryrun = QCheckBox("Dry run (skip chunk writing)")
        g2.addRow("", self.chk_dryrun)

        # Tile range (optional)
        range_row = QHBoxLayout()
        self.spin_tx0 = QSpinBox(); self.spin_tx0.setRange(0, 96); self.spin_tx0.setValue(0)
        self.spin_tx1 = QSpinBox(); self.spin_tx1.setRange(1, 97); self.spin_tx1.setValue(97)
        self.spin_tz0 = QSpinBox(); self.spin_tz0.setRange(0, 96); self.spin_tz0.setValue(0)
        self.spin_tz1 = QSpinBox(); self.spin_tz1.setRange(1, 97); self.spin_tz1.setValue(97)
        for lbl, sp in [("X:", self.spin_tx0), ("→", self.spin_tx1),
                         ("  Z:", self.spin_tz0), ("→", self.spin_tz1)]:
            range_row.addWidget(QLabel(lbl))
            range_row.addWidget(sp)
        g2.addRow("Tile range:", range_row)
        lay.addWidget(grp2)

        # ---- Buttons ----
        btn_row = QHBoxLayout()
        self.btn_start = QPushButton("▶  Start Pipeline")
        self.btn_start.setStyleSheet(
            f"background: {DARK['ok']}; color: #FFF; font-weight: bold; padding: 8px 20px;")
        self.btn_stop  = QPushButton("■  Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setStyleSheet(
            f"background: {DARK['err']}; color: #FFF; padding: 8px 20px;")
        btn_row.addWidget(self.btn_start)
        btn_row.addWidget(self.btn_stop)
        btn_row.addStretch()
        lay.addLayout(btn_row)
        lay.addStretch()

        self.btn_start.clicked.connect(self._on_start)
        self.btn_stop.clicked.connect(self.stop_requested.emit)

    def _on_start(self):
        cfg = {
            "config":      self.le_config.text().strip(),
            "masks":       self.le_masks.text().strip(),
            "schem_index": self.le_schem.text().strip(),
            "output":      self.le_output.text().strip(),
            "threads":     self.spin_threads.value(),
            "dry_run":     self.chk_dryrun.isChecked(),
            "tile_x0":     self.spin_tx0.value(),
            "tile_x1":     self.spin_tx1.value(),
            "tile_z0":     self.spin_tz0.value(),
            "tile_z1":     self.spin_tz1.value(),
        }
        self._save_settings()
        self.run_requested.emit(cfg)

    def set_running(self, running: bool):
        self.btn_start.setEnabled(not running)
        self.btn_stop.setEnabled(running)

    def _save_settings(self):
        s = QSettings("Vandir", "Pipeline")
        s.setValue("config",  self.le_config.text())
        s.setValue("masks",   self.le_masks.text())
        s.setValue("schem",   self.le_schem.text())
        s.setValue("output",  self.le_output.text())
        s.setValue("threads", self.spin_threads.value())

    def _load_settings(self):
        s = QSettings("Vandir", "Pipeline")
        self.le_config.setText(s.value("config", ""))
        self.le_masks.setText(s.value("masks", ""))
        self.le_schem.setText(s.value("schem", ""))
        self.le_output.setText(s.value("output", ""))
        t = s.value("threads", max(1, (os.cpu_count() or 4)))
        self.spin_threads.setValue(int(t))


# ---------------------------------------------------------------------------
# PANEL 2 — LIVE PROGRESS
# ---------------------------------------------------------------------------

# Tile states
_S_IDLE    = 0
_S_RUNNING = 1
_S_DONE    = 2
_S_ERROR   = 3

_TILE_COLORS = {
    _S_IDLE:    QColor(0x1A, 0x1A, 0x3A),
    _S_RUNNING: QColor(0xFF, 0xA5, 0x00),
    _S_DONE:    QColor(0x4C, 0xAF, 0x50),
    _S_ERROR:   QColor(0xF4, 0x43, 0x36),
}


class TileGridWidget(QWidget):
    """97×97 tile status grid — each cell is TILE_CELL_PX pixels."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = [[_S_IDLE] * TILES_PER_AXIS for _ in range(TILES_PER_AXIS)]
        px = TILES_PER_AXIS * TILE_CELL_PX
        self.setFixedSize(px, px)
        self._pixmap = QPixmap(px, px)
        self._pixmap.fill(QColor(0x1A, 0x1A, 0x3A))

    def set_tile(self, tx: int, tz: int, state: int):
        if 0 <= tx < TILES_PER_AXIS and 0 <= tz < TILES_PER_AXIS:
            self._state[tz][tx] = state
            col = _TILE_COLORS[state]
            p = QPainter(self._pixmap)
            p.fillRect(tx * TILE_CELL_PX, tz * TILE_CELL_PX,
                       TILE_CELL_PX - 1, TILE_CELL_PX - 1, col)
            p.end()
            self.update()

    def reset(self):
        self._state = [[_S_IDLE] * TILES_PER_AXIS for _ in range(TILES_PER_AXIS)]
        self._pixmap.fill(QColor(0x1A, 0x1A, 0x3A))
        self.update()

    def paintEvent(self, _):
        QPainter(self).drawPixmap(0, 0, self._pixmap)


class Panel2Progress(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._start_time: Optional[float] = None
        self._total = TOTAL_TILES
        self._done  = 0
        self._errors = 0
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        # Stats bar
        stats = QHBoxLayout()
        self.lbl_done    = QLabel("0 / 9409")
        self.lbl_done.setStyleSheet("font-size: 18px; font-weight: bold;")
        self.lbl_pct     = QLabel("0.0%")
        self.lbl_pct.setStyleSheet(f"font-size: 18px; color: {DARK['accent']};")
        self.lbl_eta     = QLabel("ETA: —")
        self.lbl_eta.setStyleSheet(f"color: {DARK['muted']};")
        self.lbl_errors  = QLabel("Errors: 0")
        self.lbl_errors.setStyleSheet(f"color: {DARK['muted']};")
        for w in [self.lbl_done, self.lbl_pct, self.lbl_eta, self.lbl_errors]:
            stats.addWidget(w)
        stats.addStretch()
        lay.addLayout(stats)

        self.progress = QProgressBar()
        self.progress.setRange(0, self._total)
        self.progress.setValue(0)
        self.progress.setFixedHeight(16)
        lay.addWidget(self.progress)

        # Tile grid in scroll area
        self.grid = TileGridWidget()
        scroll = QScrollArea()
        scroll.setWidget(self.grid)
        scroll.setWidgetResizable(False)
        lay.addWidget(scroll, stretch=1)

        # ETA refresh timer
        self._eta_timer = QTimer(self)
        self._eta_timer.timeout.connect(self._refresh_eta)
        self._eta_timer.start(1000)

    def reset(self, total: int):
        self._total  = total
        self._done   = 0
        self._errors = 0
        self._start_time = None
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.grid.reset()
        self._update_labels()

    def on_tile_start(self, tx: int, tz: int):
        if self._start_time is None:
            self._start_time = time.time()
        self.grid.set_tile(tx, tz, _S_RUNNING)

    def on_tile_complete(self, tx: int, tz: int):
        self._done += 1
        self.grid.set_tile(tx, tz, _S_DONE)
        self.progress.setValue(self._done)
        self._update_labels()

    def on_tile_error(self, tx: int, tz: int):
        self._errors += 1
        self.grid.set_tile(tx, tz, _S_ERROR)
        self._update_labels()

    def _update_labels(self):
        self.lbl_done.setText(f"{self._done} / {self._total}")
        pct = 100 * self._done / max(self._total, 1)
        self.lbl_pct.setText(f"{pct:.1f}%")
        self.lbl_errors.setText(f"Errors: {self._errors}")

    def _refresh_eta(self):
        if self._start_time is None or self._done == 0:
            self.lbl_eta.setText("ETA: —")
            return
        elapsed = time.time() - self._start_time
        rate    = self._done / elapsed
        remain  = max(0, self._total - self._done)
        eta_s   = remain / rate if rate > 0 else 0
        m, s    = divmod(int(eta_s), 60)
        h, m    = divmod(m, 60)
        self.lbl_eta.setText(f"ETA: {h:02d}:{m:02d}:{s:02d}")


# ---------------------------------------------------------------------------
# PANEL 3 — 2D BIOME MAP
# ---------------------------------------------------------------------------

class BiomeMapView(QGraphicsView):
    """Pannable/zoomable view of the world biome map. Receives tile RGBA patches live."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setBackgroundBrush(QColor(DARK["bg"]))

        # Base pixmap: 97×97 pixels (one pixel per tile)
        self._world_px = TILES_PER_AXIS
        self._base = QPixmap(self._world_px, self._world_px)
        self._base.fill(QColor(0x1A, 0x3A, 0x6B))
        self._item = QGraphicsPixmapItem(self._base)
        self._scene.addItem(self._item)
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def update_tile(self, tx: int, tz: int, rgba: "np.ndarray"):
        """Paint a single tile pixel (averaged color) into the world map."""
        if rgba is None or rgba.size == 0:
            return
        # Average color of the tile
        import numpy as np
        mean = rgba[:, :, :3].mean(axis=(0, 1)).astype(np.uint8)
        col  = QColor(int(mean[0]), int(mean[1]), int(mean[2]))
        p = QPainter(self._base)
        p.fillRect(tx, tz, 1, 1, col)
        p.end()
        self._item.setPixmap(self._base)

    def mark_tile_running(self, tx: int, tz: int):
        p = QPainter(self._base)
        p.fillRect(tx, tz, 1, 1, QColor(0xFF, 0xA5, 0x00))
        p.end()
        self._item.setPixmap(self._base)

    def mark_tile_error(self, tx: int, tz: int):
        p = QPainter(self._base)
        p.fillRect(tx, tz, 1, 1, QColor(0xF4, 0x43, 0x36))
        p.end()
        self._item.setPixmap(self._base)

    def reset(self):
        self._base.fill(QColor(0x1A, 0x3A, 0x6B))
        self._item.setPixmap(self._base)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)


class Panel3BiomeMap(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        # Toolbar
        tb = QHBoxLayout()
        tb.setContentsMargins(8, 4, 8, 4)
        self.combo_mode = QComboBox()
        self.combo_mode.addItems(["Biome colors", "Block colors", "Heightmap"])
        tb.addWidget(QLabel("Display:"))
        tb.addWidget(self.combo_mode)
        tb.addStretch()
        btn_fit = QPushButton("Fit to window")
        btn_fit.clicked.connect(self._fit)
        tb.addWidget(btn_fit)
        lay.addLayout(tb)

        self.view = BiomeMapView()
        lay.addWidget(self.view, stretch=1)

    def _fit(self):
        self.view.fitInView(self.view._item, Qt.AspectRatioMode.KeepAspectRatio)

    def on_tile_start(self, tx: int, tz: int):
        self.view.mark_tile_running(tx, tz)

    def on_tile_complete(self, tx: int, tz: int, rgba=None):
        if rgba is not None:
            self.view.update_tile(tx, tz, rgba)
        else:
            # Fallback: paint green
            p = QPainter(self.view._base)
            p.fillRect(tx, tz, 1, 1, QColor(0x4C, 0xAF, 0x50))
            p.end()
            self.view._item.setPixmap(self.view._base)

    def on_tile_error(self, tx: int, tz: int):
        self.view.mark_tile_error(tx, tz)

    def reset(self):
        self.view.reset()


# ---------------------------------------------------------------------------
# PANEL 5 — PARAMETER SLIDERS
# ---------------------------------------------------------------------------

# (threshold key, label, min, max, decimals, thresholds.json path)
_SLIDER_DEFS = [
    # Terrain class
    ("terrain_class.coastal_max_mc_y",  "Coastal max Y",    50,  90,  0, "terrain_class"),
    ("terrain_class.lowland_max_mc_y",  "Lowland max Y",    80, 150,  0, "terrain_class"),
    ("terrain_class.highland_max_mc_y", "Highland max Y",  130, 250,  0, "terrain_class"),
    ("terrain_class.alpine_max_mc_y",   "Alpine max Y",    200, 380,  0, "terrain_class"),
    # River carving
    ("river_carving.stream_threshold",  "Stream thresh",   0.1, 0.9,  2, "river_carving"),
    ("river_carving.river_threshold",   "River thresh",    0.3, 0.9,  2, "river_carving"),
    # Snow cap
    ("snow_cap.snow_line_y",            "Snow line Y",     150, 380,  0, "snow_cap"),
    # Decoration density
    ("decoration_density_noise.floor",  "Decoration floor",0.0, 0.5,  2, "decoration_density_noise"),
]


class Panel5Sliders(QWidget):
    """Whittaker threshold sliders — edits thresholds.json on the fly."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg_path: Optional[Path] = None
        self._cfg: dict = {}
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(6)

        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("Config file:"))
        self.le_cfg = QLineEdit()
        self.le_cfg.setReadOnly(True)
        self.le_cfg.setPlaceholderText("(loaded from Panel 1)")
        hdr.addWidget(self.le_cfg)
        btn_save = QPushButton("Save to JSON")
        btn_save.clicked.connect(self._save)
        hdr.addWidget(btn_save)
        lay.addLayout(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        form  = QFormLayout(inner)
        form.setSpacing(8)

        self._widgets: dict[str, tuple[QSlider, QLabel, float, float, int]] = {}

        for key, label, lo, hi, dec, _ in _SLIDER_DEFS:
            slider = QSlider(Qt.Orientation.Horizontal)
            steps  = int((hi - lo) * (10 ** dec))
            slider.setRange(0, steps)
            lbl_val = QLabel("—")
            lbl_val.setFixedWidth(60)
            lbl_val.setAlignment(Qt.AlignmentFlag.AlignRight)
            row = QHBoxLayout()
            row.addWidget(slider)
            row.addWidget(lbl_val)
            w = QWidget(); w.setLayout(row)
            form.addRow(label + ":", w)
            self._widgets[key] = (slider, lbl_val, lo, hi, dec)

            def _make_cb(k, s, lv, l, h, d):
                def cb(v):
                    val = l + v / (10 ** d) if d else l + v
                    lv.setText(f"{val:.{d}f}")
                return cb
            slider.valueChanged.connect(_make_cb(key, slider, lbl_val, lo, hi, dec))

        scroll.setWidget(inner)
        lay.addWidget(scroll, stretch=1)

    def load_config(self, path: str):
        self._cfg_path = Path(path)
        self.le_cfg.setText(path)
        if not self._cfg_path.exists():
            return
        with open(self._cfg_path) as f:
            self._cfg = json.load(f)
        self._populate()

    def _populate(self):
        for key, (slider, lbl_val, lo, hi, dec, *_) in self._widgets.items():
            parts = key.split(".")
            val   = self._cfg
            for p in parts:
                val = val.get(p, {}) if isinstance(val, dict) else {}
            if isinstance(val, (int, float)):
                steps = int((hi - lo) * (10 ** dec)) if dec else int(hi - lo)
                sv = int((val - lo) * (10 ** dec)) if dec else int(val - lo)
                sv = max(0, min(steps, sv))
                slider.setValue(sv)
                lbl_val.setText(f"{val:.{dec}f}")

    def _save(self):
        if not self._cfg_path:
            return
        for key, (slider, _, lo, hi, dec, *_) in self._widgets.items():
            val = lo + slider.value() / (10 ** dec) if dec else lo + slider.value()
            parts = key.split(".")
            node = self._cfg
            for p in parts[:-1]:
                node = node.setdefault(p, {})
            node[parts[-1]] = round(val, dec) if dec else int(val)
        with open(self._cfg_path, "w") as f:
            json.dump(self._cfg, f, indent=2)


# ---------------------------------------------------------------------------
# PANEL 6 — LOG CONSOLE
# ---------------------------------------------------------------------------

class Panel6Log(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)

        tb = QHBoxLayout()
        self.le_filter = QLineEdit()
        self.le_filter.setPlaceholderText("Filter…")
        self.le_filter.textChanged.connect(self._apply_filter)
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self._clear)
        self.chk_err_only = QCheckBox("Errors only")
        self.chk_err_only.toggled.connect(self._apply_filter)
        tb.addWidget(self.le_filter)
        tb.addWidget(self.chk_err_only)
        tb.addWidget(btn_clear)
        lay.addLayout(tb)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(5000)
        lay.addWidget(self.text)

        self._all_lines: list[str] = []

    def append(self, line: str):
        self._all_lines.append(line)
        if len(self._all_lines) > 5000:
            self._all_lines = self._all_lines[-4000:]
        flt = self.le_filter.text().lower()
        err_only = self.chk_err_only.isChecked()
        if err_only and "error" not in line.lower():
            return
        if flt and flt not in line.lower():
            return
        self.text.appendPlainText(line)
        self.text.verticalScrollBar().setValue(
            self.text.verticalScrollBar().maximum())

    def _apply_filter(self):
        flt      = self.le_filter.text().lower()
        err_only = self.chk_err_only.isChecked()
        self.text.setPlainText("")
        for line in self._all_lines:
            if err_only and "error" not in line.lower():
                continue
            if flt and flt not in line.lower():
                continue
            self.text.appendPlainText(line)

    def _clear(self):
        self._all_lines.clear()
        self.text.setPlainText("")


# ---------------------------------------------------------------------------
# PANEL 7 — TILE DEBUG INSPECTOR
# ---------------------------------------------------------------------------

class Panel7Debug(QWidget):
    """Shows mask values + biome stage breakdown for the last clicked/completed tile."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)

        hdr = QHBoxLayout()
        self.lbl_tile = QLabel("No tile selected")
        self.lbl_tile.setStyleSheet("font-size: 14px; font-weight: bold;")
        hdr.addWidget(self.lbl_tile)
        hdr.addStretch()
        lay.addLayout(hdr)

        # Biome breakdown
        grp = QGroupBox("Biome Distribution")
        glay = QVBoxLayout(grp)
        self.biome_table = QTableWidget(0, 2)
        self.biome_table.setHorizontalHeaderLabels(["Biome", "Coverage"])
        self.biome_table.horizontalHeader().setStretchLastSection(True)
        self.biome_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.biome_table.setFixedHeight(200)
        glay.addWidget(self.biome_table)
        lay.addWidget(grp)

        # Timing
        grp2 = QGroupBox("Performance")
        g2 = QFormLayout(grp2)
        self.lbl_elapsed = QLabel("—")
        self.lbl_rate    = QLabel("—")
        g2.addRow("Elapsed:", self.lbl_elapsed)
        g2.addRow("Rate:", self.lbl_rate)
        lay.addWidget(grp2)

        # Raw IPC data
        grp3 = QGroupBox("Last IPC Message")
        g3 = QVBoxLayout(grp3)
        self.raw_text = QPlainTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setFixedHeight(80)
        g3.addWidget(self.raw_text)
        lay.addWidget(grp3)

        lay.addStretch()

    def show_tile(self, tx: int, tz: int, biomes: list, elapsed_ms: int):
        self.lbl_tile.setText(f"Tile ({tx}, {tz})")
        self.lbl_elapsed.setText(f"{elapsed_ms} ms")
        rate = 1000 / max(elapsed_ms, 1)
        self.lbl_rate.setText(f"{rate:.1f} tiles/s")

        self.biome_table.setRowCount(len(biomes))
        for i, b in enumerate(biomes):
            self.biome_table.setItem(i, 0, QTableWidgetItem(str(b)))
            self.biome_table.setItem(i, 1, QTableWidgetItem("—"))

        self.raw_text.setPlainText(json.dumps({
            "type": "tile_complete", "tile_x": tx, "tile_y": tz,
            "biomes": biomes, "elapsed_ms": elapsed_ms,
        }, indent=2))


# ---------------------------------------------------------------------------
# MAIN WINDOW
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vandir — World Generation Pipeline")
        self.resize(1400, 900)
        self.setStyleSheet(STYLE)

        self._proc: Optional[subprocess.Popen] = None
        self._ipc:  Optional[IpcReader]        = None

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        self.setCentralWidget(tabs)

        self.p1 = Panel1Control()
        self.p2 = Panel2Progress()
        self.p3 = Panel3BiomeMap()
        self.p5 = Panel5Sliders()
        self.p6 = Panel6Log()
        self.p7 = Panel7Debug()

        # Panel 4 — import the standalone painter widget
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "panel4",
                Path(__file__).parent / "panels" / "panel4_override_painter.py",
            )
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self.p4 = mod.MainWindow()
                p4_widget = QWidget()
                p4_lay = QVBoxLayout(p4_widget)
                p4_lay.setContentsMargins(0, 0, 0, 0)
                p4_lay.addWidget(self.p4)
            else:
                raise ImportError
        except Exception:
            p4_widget = QLabel("Panel 4 (Override Painter) — run panel4_override_painter.py standalone")
            p4_widget.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.p4 = None

        tabs.addTab(self.p1, "1 — Control")
        tabs.addTab(self.p2, "2 — Progress")
        tabs.addTab(self.p3, "3 — Biome Map")
        tabs.addTab(p4_widget, "4 — Override Painter")
        tabs.addTab(self.p5, "5 — Parameters")
        tabs.addTab(self.p6, "6 — Log")
        tabs.addTab(self.p7, "7 — Debug")

        self.tabs = tabs

        # Status bar
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Ready")

    def _connect_signals(self):
        self.p1.run_requested.connect(self._start_pipeline)
        self.p1.stop_requested.connect(self._stop_pipeline)

    # ---- Pipeline lifecycle ----

    def _start_pipeline(self, cfg: dict):
        if self._proc and self._proc.poll() is None:
            return  # already running

        # Validate
        for key in ("config", "masks", "output"):
            if not cfg.get(key):
                self._status.showMessage(f"Missing required path: {key}")
                return

        # Load config into Panel 5
        self.p5.load_config(cfg["config"])

        # Reset panels
        total = (cfg["tile_x1"] - cfg["tile_x0"]) * (cfg["tile_z1"] - cfg["tile_z0"])
        self.p2.reset(total)
        self.p3.reset()
        self.p6.append(f"[pipeline] Starting — {total} tiles")

        # Build command
        pipeline_script = Path(__file__).parent.parent / "pipeline" / "run_pipeline.py"
        cmd = [
            sys.executable, str(pipeline_script),
            "--config",      cfg["config"],
            "--masks",       cfg["masks"],
            "--schem-index", cfg.get("schem_index", ""),
            "--output",      cfg["output"],
            "--threads",     str(cfg["threads"]),
            "--tile-x0",     str(cfg["tile_x0"]),
            "--tile-x1",     str(cfg["tile_x1"]),
            "--tile-z0",     str(cfg["tile_z0"]),
            "--tile-z1",     str(cfg["tile_z1"]),
        ]
        if cfg.get("dry_run"):
            cmd.append("--dry-run")

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        self._ipc = IpcReader(self._proc, self)
        self._ipc.tile_start.connect(self._on_tile_start)
        self._ipc.tile_complete.connect(self._on_tile_complete)
        self._ipc.tile_error.connect(self._on_tile_error)
        self._ipc.pipeline_done.connect(self._on_pipeline_done)
        self._ipc.raw_line.connect(self.p6.append)
        self._ipc.start()

        self.p1.set_running(True)
        self._status.showMessage("Pipeline running…")

    def _stop_pipeline(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self.p1.set_running(False)
        self._status.showMessage("Pipeline stopped")
        self.p6.append("[pipeline] Stopped by user")

    # ---- IPC signal handlers ----

    @pyqtSlot(int, int)
    def _on_tile_start(self, tx: int, tz: int):
        self.p2.on_tile_start(tx, tz)
        self.p3.on_tile_start(tx, tz)

    @pyqtSlot(int, int, list, int)
    def _on_tile_complete(self, tx: int, tz: int, biomes: list, elapsed_ms: int):
        self.p2.on_tile_complete(tx, tz)
        self.p3.on_tile_complete(tx, tz)
        self.p7.show_tile(tx, tz, biomes, elapsed_ms)

    @pyqtSlot(int, int, str)
    def _on_tile_error(self, tx: int, tz: int, error: str):
        self.p2.on_tile_error(tx, tz)
        self.p3.on_tile_error(tx, tz)
        self.p6.append(f"[ERROR] tile ({tx},{tz}): {error}")

    @pyqtSlot(int, int, int, float)
    def _on_pipeline_done(self, total: int, completed: int, errors: int, elapsed_s: float):
        self.p1.set_running(False)
        m, s = divmod(int(elapsed_s), 60)
        h, m = divmod(m, 60)
        msg  = (f"Pipeline complete — {completed}/{total} tiles OK, "
                f"{errors} errors, {h:02d}:{m:02d}:{s:02d}")
        self._status.showMessage(msg)
        self.p6.append(f"[pipeline] {msg}")

    def closeEvent(self, event):
        self._stop_pipeline()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Vandir GUI")
    parser.add_argument("--config", default="", help="Pre-fill config path")
    args, _unknown = parser.parse_known_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    if args.config:
        win.p1.le_config.setText(args.config)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
