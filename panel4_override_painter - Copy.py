#!/usr/bin/env python3
"""
panel4_override_painter.py — Vandir Override Zone Painter (Panel 4)
====================================================================
Standalone PyQt6 tool for painting detail zones onto override_base.png.

Features:
  - Load override_base.png as editable layer
  - Load Erosion2_Out.tif (downsampled) as background reference
  - Load coastal_reference.png as toggleable overlay
  - Paint with hard round brush, configurable size
  - Flood-fill tool (fixes downsampling-induced shoreline gaps)
  - False-color zone preview with per-value distinct colours
  - Height-derived land mask overlay (shows true shoreline)
  - Undo/redo (20 levels)
  - Export → override_final.png (8-bit grayscale)
  - Pannable / zoomable QGraphicsView canvas

Usage:
  py panel4_override_painter.py

Dependencies:
  py -m pip install PyQt6 numpy rasterio pillow scipy
"""

import sys
import json
import copy
from pathlib import Path
from collections import deque

import numpy as np
from PIL import Image

import rasterio
from rasterio.enums import Resampling

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QLabel, QSlider, QComboBox, QPushButton, QCheckBox,
    QFileDialog, QStatusBar, QSplitter, QGroupBox, QScrollArea,
    QSizePolicy, QMessageBox, QToolBar, QSpinBox
)
from PyQt6.QtCore import (
    Qt, QPoint, QPointF, QRectF, pyqtSignal, QTimer
)
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QPen, QColor, QBrush,
    QCursor, QIcon, QAction, QKeySequence
)

# ---------------------------------------------------------------------------
# Override value definitions (from Project Bible)
# ---------------------------------------------------------------------------

OVERRIDE_ZONES = {
    0:   ("No Override",               "#111111"),
    10:  ("Coastal Heath",             "#8B7355"),
    20:  ("Temperate Rainforest",      "#1A5C2A"),
    30:  ("Boreal Taiga",              "#2D6B4A"),
    35:  ("Snowy Boreal Taiga",        "#A8D8C0"),  # NEW
    40:  ("Alpine Meadow",             "#7EC850"),
    50:  ("Arctic Tundra",             "#C8E8F0"),
    55:  ("Frozen Flats",              "#E8F4FF"),  # NEW
    60:  ("Temperate Deciduous",       "#6AAF3A"),  # brighter lime — distinct from 120
    70:  ("Rainforest Coast",          "#0D7A3E"),
    80:  ("Riparian Woodland",         "#1A3F6F"),  # dark blue-green — distinct from forest greens
    90:  ("Dry Oak Savanna",           "#C4A35A"),
    100: ("Karst Barrens",             "#9E9E8A"),
    110: ("Birch Forest",              "#A8C87A"),
    115: ("Eastern Temperate Coast",   "#7AB8C8"),  # NEW
    120: ("Mixed Forest",              "#5A8A4A"),
    130: ("Continental Steppe",        "#D4B86A"),
    140: ("Dry Pine Barrens",          "#B87A3A"),
    150: ("Scrubby Heathland",         "#7A5C8A"),  # purple-grey — distinct from Coastal Heath
    160: ("Lush Rainforest Coast",     "#006B2A"),
    170: ("Sand Dune Desert",          "#E8C87A"),
    # 180 removed — merged into 190
    190: ("Desert Steppe Transition",  "#D4824A"),  # orange — distinct from 90/200
    200: ("Semi-Arid Shrubland",       "#C8C060"),  # yellow-green — distinct from 90/190
    210: ("Dry Woodland Maquis",       "#B89050"),
    220: ("Tidal Jungle Fringe",       "#00A86B"),  # brighter green — distinct from 70
    230: ("Mangrove Coast",            "#004D33"),
    240: ("Freshwater Fen",            "#3A6B5A"),
}

DISPLAY_SIZE = 1024   # working canvas resolution
UNDO_LEVELS  = 20


# ---------------------------------------------------------------------------
# Canvas widget
# ---------------------------------------------------------------------------

class OverrideCanvas(QGraphicsView):
    """
    Pannable, zoomable QGraphicsView.
    Handles mouse paint and flood-fill interactions.
    Reports paint events upward via signal.
    """
    paint_applied  = pyqtSignal(int, int, int)   # canvas_x, canvas_y, value
    fill_applied   = pyqtSignal(int, int, int)   # canvas_x, canvas_y, value
    cursor_moved   = pyqtSignal(int, int)        # canvas_x, canvas_y

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = QGraphicsScene()
        self.setScene(self.scene)

        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setBackgroundBrush(QBrush(QColor("#0a0a12")))

        self._panning    = False
        self._pan_start  = QPoint()
        self._tool       = "brush"   # "brush" | "fill"
        self._painting   = False
        self._last_pt    = None
        self._zoom       = 1.0

    def set_tool(self, tool: str):
        self._tool = tool
        if tool == "fill":
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    # --- zoom ---

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom *= factor
        self._zoom = max(0.05, min(32.0, self._zoom))
        self.scale(factor, factor)

    # --- pan (middle mouse or space+drag) ---

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._painting = True
            pt = self._scene_pt(event)
            if pt and self._tool == "brush":
                self.paint_applied.emit(pt.x(), pt.y(), -1)
                self._last_pt = pt
            elif pt and self._tool == "fill":
                self.fill_applied.emit(pt.x(), pt.y(), -1)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position().toPoint() - self._pan_start
            self._pan_start = event.position().toPoint()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            return

        pt = self._scene_pt(event)
        if pt:
            self.cursor_moved.emit(pt.x(), pt.y())
        if self._painting and self._tool == "brush" and pt:
            self.paint_applied.emit(pt.x(), pt.y(), -1)
            self._last_pt = pt
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        if event.button() == Qt.MouseButton.LeftButton:
            self._painting = False
            self._last_pt = None
        super().mouseReleaseEvent(event)

    def _scene_pt(self, event) -> QPoint | None:
        scene_pos = self.mapToScene(event.position().toPoint())
        x = int(scene_pos.x())
        y = int(scene_pos.y())
        if 0 <= x < DISPLAY_SIZE and 0 <= y < DISPLAY_SIZE:
            return QPoint(x, y)
        return None

    def fit_canvas(self, w: int, h: int):
        self.setSceneRect(0, 0, w, h)
        self.fitInView(QRectF(0, 0, w, h), Qt.AspectRatioMode.KeepAspectRatio)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class Panel4Window(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vandir — Panel 4: Override Zone Painter")
        self.setMinimumSize(1300, 800)
        self.resize(1600, 950)

        # Data state
        self.override_data: np.ndarray | None = None   # 8-bit, display res
        self.height_data:   np.ndarray | None = None   # float32, display res, normalized
        self.coastal_data:  np.ndarray | None = None   # uint8 255/0
        self.flow_data:     np.ndarray | None = None   # float32, normalized

        self.undo_stack: deque = deque(maxlen=UNDO_LEVELS)
        self.redo_stack: deque = deque(maxlen=UNDO_LEVELS)

        self._current_value   = 20    # active zone value
        self._brush_size      = 8     # radius in canvas pixels
        self._show_falsecolor = True
        self._show_coastal    = False
        self._show_height     = True
        self._show_landmask   = False
        self._show_flow       = False
        self._flow_threshold  = 90
        self._height_solo     = False
        self._opacity_height  = 0.45
        self._opacity_coastal = 0.55
        self._tool            = "brush"
        self._land_threshold  = 26    # 0–100 slider; 26 ≈ 17050/65535 normalized
        self._fill_tolerance  = 10    # flood fill ± tolerance
        self._paint_over_enabled = False
        self._paint_over_value   = 0   # only paint over this zone value

        # Layer pixmaps in scene
        self._bg_item:       QGraphicsPixmapItem | None = None
        self._ov_item:       QGraphicsPixmapItem | None = None
        self._coastal_item:  QGraphicsPixmapItem | None = None
        self._flow_item:     QGraphicsPixmapItem | None = None

        self._stroke_dirty = False   # flag: update display after stroke batch

        self._build_ui()
        self._build_toolbar()
        self._connect_signals()

        # Ask for files on launch
        QTimer.singleShot(100, self._load_files_dialog)

    # -----------------------------------------------------------------------
    # UI layout
    # -----------------------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(4, 4, 4, 4)
        root_layout.setSpacing(4)

        # Left: canvas
        self.canvas = OverrideCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # Right: controls panel (fixed width)
        controls = QWidget()
        controls.setFixedWidth(280)
        controls.setStyleSheet("background: #13131f; color: #d0d0e0;")
        ctrl_layout = QVBoxLayout(controls)
        ctrl_layout.setContentsMargins(8, 8, 8, 8)
        ctrl_layout.setSpacing(6)

        # -- Tool group --
        tool_group = QGroupBox("Tool")
        tool_group.setStyleSheet(self._group_style())
        tg_lay = QHBoxLayout(tool_group)
        self.btn_brush = QPushButton("🖌 Brush")
        self.btn_brush.setCheckable(True)
        self.btn_brush.setChecked(True)
        self.btn_fill  = QPushButton("🪣 Fill")
        self.btn_fill.setCheckable(True)
        self.btn_brush.setStyleSheet(self._tool_btn_style())
        self.btn_fill.setStyleSheet(self._tool_btn_style())
        tg_lay.addWidget(self.btn_brush)
        tg_lay.addWidget(self.btn_fill)
        ctrl_layout.addWidget(tool_group)

        # -- Paint-over filter --
        po_group = QGroupBox("Paint Over Filter")
        po_group.setStyleSheet(self._group_style())
        po_lay = QVBoxLayout(po_group)

        self.chk_paint_over = QCheckBox("Only paint over:")
        self.chk_paint_over.setChecked(False)
        self.chk_paint_over.setStyleSheet("color: #d0d0e0; font-size: 11px;")
        self.paint_over_combo = QComboBox()
        self.paint_over_combo.setStyleSheet("""
            QComboBox { background: #1e1e30; color: #d0d0e0; border: 1px solid #3a3a5a;
                        padding: 3px; font-size: 11px; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #1e1e30; color: #d0d0e0; }
        """)
        self.paint_over_combo.setEnabled(False)
        for val, (name, _) in OVERRIDE_ZONES.items():
            self.paint_over_combo.addItem(f"{val:3d}  {name}", userData=val)

        po_hint = QLabel("Brush/fill won't touch\nany other zone value.")
        po_hint.setStyleSheet("color: #606080; font-size: 10px;")

        po_lay.addWidget(self.chk_paint_over)
        po_lay.addWidget(self.paint_over_combo)
        po_lay.addWidget(po_hint)
        ctrl_layout.addWidget(po_group)

        # -- Brush size --
        size_group = QGroupBox("Brush Size")
        size_group.setStyleSheet(self._group_style())
        sg_lay = QHBoxLayout(size_group)
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(1, 100)
        self.brush_slider.setValue(self._brush_size)
        self.brush_label = QLabel(f"{self._brush_size}px")
        self.brush_label.setFixedWidth(38)
        sg_lay.addWidget(self.brush_slider)
        sg_lay.addWidget(self.brush_label)
        ctrl_layout.addWidget(size_group)

        # -- Zone selector --
        zone_group = QGroupBox("Zone Value")
        zone_group.setStyleSheet(self._group_style())
        zg_lay = QVBoxLayout(zone_group)
        self.zone_combo = QComboBox()
        self.zone_combo.setStyleSheet("""
            QComboBox { background: #1e1e30; color: #d0d0e0; border: 1px solid #3a3a5a;
                        padding: 4px; font-size: 12px; }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView { background: #1e1e30; color: #d0d0e0; }
        """)
        for val, (name, _) in OVERRIDE_ZONES.items():
            self.zone_combo.addItem(f"{val:3d}  {name}", userData=val)
        # Default to value 20
        self.zone_combo.setCurrentIndex(
            list(OVERRIDE_ZONES.keys()).index(self._current_value)
        )
        self.zone_swatch = QLabel()
        self.zone_swatch.setFixedHeight(18)
        self.zone_swatch.setStyleSheet(f"background: {OVERRIDE_ZONES[self._current_value][1]}; border-radius: 3px;")
        zg_lay.addWidget(self.zone_combo)
        zg_lay.addWidget(self.zone_swatch)
        ctrl_layout.addWidget(zone_group)

        # -- Layers / overlays --
        layer_group = QGroupBox("Layers")
        layer_group.setStyleSheet(self._group_style())
        lg_lay = QVBoxLayout(layer_group)

        self.chk_falsecolor = QCheckBox("False-color zones")
        self.chk_falsecolor.setChecked(True)
        self.chk_height = QCheckBox("Height reference")
        self.chk_height.setChecked(True)
        self.chk_coastal = QCheckBox("Coastal reference overlay")
        self.chk_coastal.setChecked(False)
        self.chk_landmask = QCheckBox("Land mask (true shoreline)")
        self.chk_landmask.setChecked(False)
        self.chk_flow = QCheckBox("Flow / river overlay")
        self.chk_flow.setChecked(False)
        self.chk_height_solo = QCheckBox("Height solo (hide zones)")
        self.chk_height_solo.setChecked(False)

        for chk in (self.chk_falsecolor, self.chk_height, self.chk_coastal,
                    self.chk_landmask, self.chk_flow, self.chk_height_solo):
            chk.setStyleSheet("color: #d0d0e0; font-size: 11px;")
            lg_lay.addWidget(chk)

        # Flow threshold
        fl_row = QHBoxLayout()
        fl_row.addWidget(QLabel("River threshold:"))
        self.flow_threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.flow_threshold_slider.setRange(1, 99)
        self.flow_threshold_slider.setValue(self._flow_threshold)
        self.flow_threshold_label = QLabel(f"{self._flow_threshold}%")
        self.flow_threshold_label.setFixedWidth(32)
        fl_row.addWidget(self.flow_threshold_slider)
        fl_row.addWidget(self.flow_threshold_label)
        lg_lay.addLayout(fl_row)

        # Height opacity
        ht_row = QHBoxLayout()
        ht_row.addWidget(QLabel("Height opacity:"))
        self.height_opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.height_opacity_slider.setRange(0, 100)
        self.height_opacity_slider.setValue(int(self._opacity_height * 100))
        ht_row.addWidget(self.height_opacity_slider)
        lg_lay.addLayout(ht_row)

        # Land mask threshold
        lm_row = QHBoxLayout()
        lm_row.addWidget(QLabel("Land thresh:"))
        self.land_threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.land_threshold_slider.setRange(1, 99)
        self.land_threshold_slider.setValue(self._land_threshold)
        self.land_threshold_label = QLabel(f"{self._land_threshold}%")
        self.land_threshold_label.setFixedWidth(32)
        lm_row.addWidget(self.land_threshold_slider)
        lm_row.addWidget(self.land_threshold_label)
        lg_lay.addLayout(lm_row)

        # Fill tolerance
        ft_row = QHBoxLayout()
        ft_row.addWidget(QLabel("Fill tolerance:"))
        self.fill_tolerance_slider = QSlider(Qt.Orientation.Horizontal)
        self.fill_tolerance_slider.setRange(0, 50)
        self.fill_tolerance_slider.setValue(self._fill_tolerance)
        self.fill_tolerance_label = QLabel(f"±{self._fill_tolerance}")
        self.fill_tolerance_label.setFixedWidth(32)
        ft_row.addWidget(self.fill_tolerance_slider)
        ft_row.addWidget(self.fill_tolerance_label)
        lg_lay.addLayout(ft_row)

        ctrl_layout.addWidget(layer_group)

        # -- Legend --
        legend_group = QGroupBox("Legend")
        legend_group.setStyleSheet(self._group_style())
        legend_scroll = QScrollArea()
        legend_scroll.setStyleSheet("background: #0e0e1a; border: none;")
        legend_scroll.setWidgetResizable(True)
        legend_inner = QWidget()
        legend_layout = QVBoxLayout(legend_inner)
        legend_layout.setSpacing(2)
        legend_layout.setContentsMargins(4, 4, 4, 4)

        for val, (name, color) in OVERRIDE_ZONES.items():
            if val == 0:
                continue
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(0, 0, 0, 0)
            row_lay.setSpacing(4)
            swatch = QLabel()
            swatch.setFixedSize(14, 14)
            swatch.setStyleSheet(f"background: {color}; border-radius: 2px;")
            label = QLabel(f"{val}  {name}")
            label.setStyleSheet("color: #b0b0c8; font-size: 10px;")
            row_lay.addWidget(swatch)
            row_lay.addWidget(label)
            row_lay.addStretch()
            legend_layout.addWidget(row)

        legend_scroll.setWidget(legend_inner)
        legend_scroll.setFixedHeight(200)
        lg2_lay = QVBoxLayout(legend_group)
        lg2_lay.addWidget(legend_scroll)
        ctrl_layout.addWidget(legend_group)

        ctrl_layout.addStretch()

        # -- Export / undo buttons --
        btn_row = QHBoxLayout()
        self.btn_undo = QPushButton("↩ Undo")
        self.btn_redo = QPushButton("↪ Redo")
        btn_row.addWidget(self.btn_undo)
        btn_row.addWidget(self.btn_redo)
        ctrl_layout.addLayout(btn_row)

        self.btn_export = QPushButton("💾  Export override_final.png")
        self.btn_export.setStyleSheet("""
            QPushButton { background: #1a6b3a; color: white; font-size: 12px;
                          font-weight: bold; padding: 8px; border-radius: 4px; }
            QPushButton:hover { background: #2a9b5a; }
            QPushButton:disabled { background: #333; color: #666; }
        """)
        self.btn_export.setEnabled(False)
        ctrl_layout.addWidget(self.btn_export)

        # Status bar
        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status.setStyleSheet("background: #0e0e1a; color: #8080a0; font-size: 11px;")
        self._coord_label = QLabel("x:—  y:—")
        self._value_label = QLabel("zone: —")
        self.status.addPermanentWidget(self._coord_label)
        self.status.addPermanentWidget(self._value_label)

        root_layout.addWidget(self.canvas, stretch=1)
        root_layout.addWidget(controls)

        self.setStyleSheet("QMainWindow { background: #0a0a12; }")

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setStyleSheet("QToolBar { background: #13131f; border: none; spacing: 4px; }")
        self.addToolBar(tb)

        act_load  = QAction("📂 Load Files", self)
        act_load.triggered.connect(self._load_files_dialog)
        tb.addAction(act_load)

        tb.addSeparator()

        act_undo = QAction("↩ Undo", self)
        act_undo.setShortcut(QKeySequence.StandardKey.Undo)
        act_undo.triggered.connect(self._undo)
        tb.addAction(act_undo)

        act_redo = QAction("↪ Redo", self)
        act_redo.setShortcut(QKeySequence.StandardKey.Redo)
        act_redo.triggered.connect(self._redo)
        tb.addAction(act_redo)

    def _connect_signals(self):
        self.canvas.paint_applied.connect(self._on_paint)
        self.canvas.fill_applied.connect(self._on_fill)
        self.canvas.cursor_moved.connect(self._on_cursor_moved)

        self.brush_slider.valueChanged.connect(self._on_brush_size)
        self.zone_combo.currentIndexChanged.connect(self._on_zone_changed)

        self.btn_brush.clicked.connect(lambda: self._set_tool("brush"))
        self.btn_fill.clicked.connect(lambda: self._set_tool("fill"))

        self.chk_paint_over.toggled.connect(self._on_paint_over_toggled)
        self.paint_over_combo.currentIndexChanged.connect(self._on_paint_over_changed)

        self.chk_falsecolor.toggled.connect(lambda v: self._set_flag("_show_falsecolor", v))
        self.chk_height.toggled.connect(lambda v: self._set_flag("_show_height", v))
        self.chk_coastal.toggled.connect(lambda v: self._set_flag("_show_coastal", v))
        self.chk_landmask.toggled.connect(lambda v: self._set_flag("_show_landmask", v))
        self.chk_flow.toggled.connect(lambda v: self._set_flag("_show_flow", v))
        self.chk_height_solo.toggled.connect(lambda v: self._set_flag("_height_solo", v))
        self.flow_threshold_slider.valueChanged.connect(self._on_flow_threshold)
        self.height_opacity_slider.valueChanged.connect(
            lambda v: self._set_opacity_height(v / 100)
        )
        self.land_threshold_slider.valueChanged.connect(self._on_land_threshold)
        self.fill_tolerance_slider.valueChanged.connect(self._on_fill_tolerance)

        self.btn_undo.clicked.connect(self._undo)
        self.btn_redo.clicked.connect(self._redo)
        self.btn_export.clicked.connect(self._export)

    # -----------------------------------------------------------------------
    # File loading
    # -----------------------------------------------------------------------

    def _load_files_dialog(self):
        default_dir = r"C:\Users\nicho\minecraft-worldgen"

        # Override base (required)
        ov_path, _ = QFileDialog.getOpenFileName(
            self, "Load override_base.png", default_dir,
            "PNG Images (*.png);;All files (*)"
        )
        if not ov_path:
            self.status.showMessage("No override file loaded.")
            return

        # Height TIFF (required)
        ht_path, _ = QFileDialog.getOpenFileName(
            self, "Load height TIFF (Erosion2_Out.tif)", r"C:\Gaea Stuff",
            "TIFF files (*.tif *.tiff);;All files (*)"
        )

        # Coastal reference (optional)
        co_path, _ = QFileDialog.getOpenFileName(
            self, "Load coastal_reference.png (optional — cancel to skip)",
            default_dir, "PNG Images (*.png);;All files (*)"
        )

        # Flow mask (optional)
        fl_path, _ = QFileDialog.getOpenFileName(
            self, "Load flow TIFF — Erosion2_Flow.tif (optional — cancel to skip)",
            r"C:\Gaea Stuff", "TIFF files (*.tif *.tiff);;All files (*)"
        )

        self.status.showMessage("Loading files…")
        QApplication.processEvents()
        self._load_data(
            ov_path,
            ht_path if ht_path else None,
            co_path if co_path else None,
            fl_path if fl_path else None,
        )

    def _load_data(self, ov_path: str, ht_path: str | None, co_path: str | None, fl_path: str | None = None):
        try:
            # Override data
            img = Image.open(ov_path).convert("L")
            # Resize to DISPLAY_SIZE for working canvas (nearest — preserve zone IDs)
            img_small = img.resize((DISPLAY_SIZE, DISPLAY_SIZE), Image.NEAREST)
            self.override_data = np.array(img_small, dtype=np.uint8)
            self._orig_size = img.size  # (w, h)

            # Height data
            if ht_path:
                with rasterio.open(ht_path) as src:
                    h = src.read(
                        1,
                        out_shape=(DISPLAY_SIZE, DISPLAY_SIZE),
                        resampling=Resampling.average
                    ).astype(np.float32)
                mn, mx = h.min(), h.max()
                self.height_data = (h - mn) / max(mx - mn, 1)
            else:
                self.height_data = None

            # Coastal reference
            if co_path:
                co = Image.open(co_path).convert("L")
                co_small = co.resize((DISPLAY_SIZE, DISPLAY_SIZE), Image.NEAREST)
                self.coastal_data = np.array(co_small, dtype=np.uint8)
            else:
                self.coastal_data = None

            # Flow mask
            if fl_path:
                with rasterio.open(fl_path) as src:
                    fl = src.read(
                        1,
                        out_shape=(DISPLAY_SIZE, DISPLAY_SIZE),
                        resampling=Resampling.average
                    ).astype(np.float32)
                mn, mx = fl.min(), fl.max()
                self.flow_data = (fl - mn) / max(mx - mn, 1)
            else:
                self.flow_data = None

        except Exception as e:
            QMessageBox.critical(self, "Load Error", str(e))
            return

        self.undo_stack.clear()
        self.redo_stack.clear()
        self._update_undo_buttons()
        self.btn_export.setEnabled(True)

        self._init_scene()
        self._refresh_display()
        self.status.showMessage(
            f"Loaded: {Path(ov_path).name}  |  canvas: {DISPLAY_SIZE}×{DISPLAY_SIZE}  "
            f"|  original: {self._orig_size[0]}×{self._orig_size[1]}"
        )

    # -----------------------------------------------------------------------
    # Scene / display
    # -----------------------------------------------------------------------

    def _init_scene(self):
        self.canvas.scene.clear()

        # Background layer (height or solid black)
        w = h = DISPLAY_SIZE
        self._bg_item = QGraphicsPixmapItem()
        self._bg_item.setZValue(0)
        self.canvas.scene.addItem(self._bg_item)

        # Override layer
        self._ov_item = QGraphicsPixmapItem()
        self._ov_item.setZValue(1)
        self.canvas.scene.addItem(self._ov_item)

        # Coastal overlay
        self._coastal_item = QGraphicsPixmapItem()
        self._coastal_item.setZValue(2)
        self.canvas.scene.addItem(self._coastal_item)

        self._flow_item = QGraphicsPixmapItem()
        self._flow_item.setZValue(3)
        self.canvas.scene.addItem(self._flow_item)

        self.canvas.fit_canvas(w, h)

    def _refresh_display(self):
        if self.override_data is None:
            return

        h = w = DISPLAY_SIZE

        # --- Background: height map ---
        if self.height_data is not None and self._show_height:
            if self._height_solo:
                # Full brightness, no dimming
                ht_norm = (self.height_data * 255).astype(np.uint8)
            else:
                ht_norm = (self.height_data * 255).astype(np.uint8)
                ht_norm = (ht_norm * self._opacity_height).astype(np.uint8)
            bg_rgb = np.stack([ht_norm, ht_norm, ht_norm], axis=-1)
        else:
            bg_rgb = np.zeros((h, w, 3), dtype=np.uint8)

        # Land mask overlay — threshold adjustable via slider
        if self._show_landmask and self.height_data is not None:
            thresh = self._land_threshold / 100.0
            land_mask = self.height_data > thresh  # inverted: high value = ocean
            bg_rgb[land_mask, 1] = np.minimum(
                bg_rgb[land_mask, 1].astype(np.int32) + 40, 255
            ).astype(np.uint8)

        self._bg_item.setPixmap(self._ndarray_to_pixmap(bg_rgb))

        # --- Override layer: false-color or grayscale ---
        # Hidden in height solo mode so terrain is fully visible
        ov = self.override_data
        if self._height_solo:
            blank = np.zeros((h, w, 4), dtype=np.uint8)
            self._ov_item.setPixmap(self._ndarray_to_pixmap_rgba(blank))
        else:
            if self._show_falsecolor:
                ov_rgb = self._override_to_falsecolor(ov)
                alpha = np.where(ov > 0, 200, 0).astype(np.uint8)
            else:
                gray = ov.copy()
                ov_rgb = np.stack([gray, gray, gray], axis=-1)
                alpha = np.where(ov > 0, 180, 0).astype(np.uint8)
            ov_rgba = np.dstack([ov_rgb, alpha])
            self._ov_item.setPixmap(self._ndarray_to_pixmap_rgba(ov_rgba))

        # --- Coastal overlay ---
        if self._show_coastal and self.coastal_data is not None:
            co = self.coastal_data
            # Render as semi-transparent cyan
            co_rgb = np.zeros((h, w, 4), dtype=np.uint8)
            mask = co > 128
            co_rgb[mask] = [0, 220, 220, int(self._opacity_coastal * 255)]
            self._coastal_item.setPixmap(self._ndarray_to_pixmap_rgba(co_rgb))
            self._coastal_item.setVisible(True)
        else:
            self._coastal_item.setVisible(False)

        # --- Flow / river overlay ---
        if self._show_flow and self.flow_data is not None:
            thresh = self._flow_threshold / 100.0
            fl_rgb = np.zeros((h, w, 4), dtype=np.uint8)
            # Only show flow on land pixels
            if self.height_data is not None:
                land = self.height_data > (self._land_threshold / 100.0)
            else:
                land = np.ones((h, w), dtype=bool)
            river_mask = (self.flow_data > thresh) & land
            mid_mask = (self.flow_data > thresh * 0.6) & ~river_mask & land
            fl_rgb[river_mask] = [40, 160, 255, 200]
            fl_rgb[mid_mask] = [20, 80, 180, 80]
            self._flow_item.setPixmap(self._ndarray_to_pixmap_rgba(fl_rgb))
            self._flow_item.setVisible(True)
        else:
            if self._flow_item:
                self._flow_item.setVisible(False)

    def _override_to_falsecolor(self, ov: np.ndarray) -> np.ndarray:
        out = np.zeros((*ov.shape, 3), dtype=np.uint8)
        for val, (_, hex_color) in OVERRIDE_ZONES.items():
            if val == 0:
                continue
            mask = ov == val
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            out[mask] = [r, g, b]
        return out

    @staticmethod
    def _ndarray_to_pixmap(arr: np.ndarray) -> QPixmap:
        h, w = arr.shape[:2]
        c_arr = np.ascontiguousarray(arr)
        img = QImage(c_arr.data, w, h, w * 3, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(img)

    @staticmethod
    def _ndarray_to_pixmap_rgba(arr: np.ndarray) -> QPixmap:
        h, w = arr.shape[:2]
        c_arr = np.ascontiguousarray(arr)
        img = QImage(c_arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
        return QPixmap.fromImage(img)

    # -----------------------------------------------------------------------
    # Paint operations
    # -----------------------------------------------------------------------

    def _on_paint(self, cx: int, cy: int, _):
        if self.override_data is None:
            return
        # Save undo snapshot on first touch of a new stroke
        if not self._stroke_dirty:
            self._push_undo()
            self._stroke_dirty = True

        r = self._brush_size
        y0 = max(0, cy - r)
        y1 = min(DISPLAY_SIZE, cy + r + 1)
        x0 = max(0, cx - r)
        x1 = min(DISPLAY_SIZE, cx + r + 1)

        yy, xx = np.ogrid[y0:y1, x0:x1]
        circle_mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r

        # Constrain to land mask — brush never touches ocean pixels
        if self.height_data is not None:
            thresh = self._land_threshold / 100.0
            region_height = self.height_data[y0:y1, x0:x1]
            land_region = region_height > thresh
            circle_mask = circle_mask & land_region

        # Paint-over filter — only overwrite the specified zone value
        if self._paint_over_enabled:
            existing = self.override_data[y0:y1, x0:x1]
            circle_mask = circle_mask & (existing == self._paint_over_value)

        self.override_data[y0:y1, x0:x1][circle_mask] = self._current_value
        self._refresh_display()

    def mousePressEvent(self, event):
        # Reset stroke dirty flag on new click
        self._stroke_dirty = False
        super().mousePressEvent(event)

    def _on_fill(self, cx: int, cy: int, _):
        if self.override_data is None:
            return
        self._push_undo()
        target_val = int(self.override_data[cy, cx])
        fill_val   = self._current_value
        if target_val == fill_val:
            return
        # Paint-over filter — only fill if clicked pixel matches the allowed value
        if self._paint_over_enabled and target_val != self._paint_over_value:
            self.status.showMessage(
                f"Paint-over filter active: can only fill over val {self._paint_over_value}"
            )
            return
        self._flood_fill(cx, cy, target_val, fill_val)
        self._refresh_display()

    def _flood_fill(self, x: int, y: int, target: int, fill: int):
        """
        Scanline flood fill with tolerance, constrained to land mask.
        Land mask prevents fill bleeding across ocean gaps between continents.
        """
        data = self.override_data
        h, w = data.shape
        tol = self._fill_tolerance

        # Build land mask from height data if available
        # Ocean pixels are hard stops — fill never crosses them
        if self.height_data is not None:
            thresh = self._land_threshold / 100.0
            land_mask = self.height_data > thresh  # inverted heightmap: high value = ocean, low = land
        else:
            land_mask = np.ones((h, w), dtype=bool)  # no height data → no constraint

        def passable(cx: int, cy: int) -> bool:
            if not land_mask[cy, cx]:
                return False  # ocean pixel — hard stop
            return abs(int(data[cy, cx]) - target) <= tol

        if not passable(x, y):
            return

        stack = [(x, y)]
        visited = np.zeros((h, w), dtype=bool)

        while stack:
            cx, cy = stack.pop()
            if cx < 0 or cx >= w or cy < 0 or cy >= h:
                continue
            if visited[cy, cx] or not passable(cx, cy):
                continue
            # Scanline left
            lx = cx
            while lx >= 0 and passable(lx, cy) and not visited[cy, lx]:
                lx -= 1
            lx += 1
            # Scanline right
            rx = cx
            while rx < w and passable(rx, cy) and not visited[cy, rx]:
                rx += 1
            rx -= 1
            # Fill the run
            data[cy, lx:rx+1] = fill
            visited[cy, lx:rx+1] = True
            # Queue rows above and below
            for nx in range(lx, rx + 1):
                if cy - 1 >= 0 and not visited[cy-1, nx] and passable(nx, cy - 1):
                    stack.append((nx, cy - 1))
                if cy + 1 < h and not visited[cy+1, nx] and passable(nx, cy + 1):
                    stack.append((nx, cy + 1))

    def _on_cursor_moved(self, cx: int, cy: int):
        self._coord_label.setText(f"x:{cx}  y:{cy}")
        if self.override_data is not None:
            val = int(self.override_data[cy, cx])
            name = OVERRIDE_ZONES.get(val, ("?",))[0]
            self._value_label.setText(f"zone: {val} ({name})")

    # -----------------------------------------------------------------------
    # Undo / redo
    # -----------------------------------------------------------------------

    def _push_undo(self):
        if self.override_data is None:
            return
        self.undo_stack.append(self.override_data.copy())
        self.redo_stack.clear()
        self._update_undo_buttons()

    def _undo(self):
        if not self.undo_stack or self.override_data is None:
            return
        self.redo_stack.append(self.override_data.copy())
        self.override_data = self.undo_stack.pop()
        self._refresh_display()
        self._update_undo_buttons()
        self.status.showMessage(f"Undo ({len(self.undo_stack)} left)")

    def _redo(self):
        if not self.redo_stack or self.override_data is None:
            return
        self.undo_stack.append(self.override_data.copy())
        self.override_data = self.redo_stack.pop()
        self._refresh_display()
        self._update_undo_buttons()
        self.status.showMessage(f"Redo")

    def _update_undo_buttons(self):
        self.btn_undo.setEnabled(bool(self.undo_stack))
        self.btn_redo.setEnabled(bool(self.redo_stack))

    # -----------------------------------------------------------------------
    # Controls
    # -----------------------------------------------------------------------

    def _set_tool(self, tool: str):
        self._tool = tool
        self.canvas.set_tool(tool)
        self.btn_brush.setChecked(tool == "brush")
        self.btn_fill.setChecked(tool == "fill")

    def _on_brush_size(self, val: int):
        self._brush_size = val
        self.brush_label.setText(f"{val}px")

    def _on_zone_changed(self, idx: int):
        val = self.zone_combo.itemData(idx)
        self._current_value = val
        color = OVERRIDE_ZONES[val][1]
        self.zone_swatch.setStyleSheet(
            f"background: {color}; border-radius: 3px;"
        )

    def _set_flag(self, attr: str, val: bool):
        setattr(self, attr, val)
        self._refresh_display()

    def _set_opacity_height(self, val: float):
        self._opacity_height = val
        self._refresh_display()

    def _on_paint_over_toggled(self, enabled: bool):
        self._paint_over_enabled = enabled
        self.paint_over_combo.setEnabled(enabled)

    def _on_paint_over_changed(self, idx: int):
        self._paint_over_value = self.paint_over_combo.itemData(idx)

    def _on_flow_threshold(self, val: int):
        self._flow_threshold = val
        self.flow_threshold_label.setText(f"{val}%")
        self._refresh_display()

    def _on_land_threshold(self, val: int):
        self._land_threshold = val
        self.land_threshold_label.setText(f"{val}%")
        self._refresh_display()

    def _on_fill_tolerance(self, val: int):
        self._fill_tolerance = val
        self.fill_tolerance_label.setText(f"±{val}")

    # -----------------------------------------------------------------------
    # Export
    # -----------------------------------------------------------------------

    def _export(self):
        if self.override_data is None:
            return

        save_path, _ = QFileDialog.getSaveFileName(
            self, "Save override_final.png", r"C:\Users\nicho\minecraft-worldgen\override_final.png",
            "PNG Images (*.png)"
        )
        if not save_path:
            return

        # Upscale back to original resolution using nearest (preserve IDs)
        out_img = Image.fromarray(self.override_data, mode="L")
        if hasattr(self, "_orig_size"):
            out_img = out_img.resize(self._orig_size, Image.NEAREST)

        out_img.save(save_path)
        self.status.showMessage(
            f"Saved: {Path(save_path).name}  ({out_img.size[0]}×{out_img.size[1]})"
        )
        QMessageBox.information(
            self, "Export Complete",
            f"Override mask saved to:\n{save_path}\n\n"
            f"Size: {out_img.size[0]}×{out_img.size[1]} px (original resolution)\n\n"
            f"Next step: feed into convert_masks.py as override source."
        )

    # -----------------------------------------------------------------------
    # Style helpers
    # -----------------------------------------------------------------------

    @staticmethod
    def _group_style():
        return """
            QGroupBox {
                color: #8080b0; font-size: 11px; font-weight: bold;
                border: 1px solid #2a2a40; border-radius: 4px;
                margin-top: 8px; padding-top: 6px;
            }
            QGroupBox::title { subcontrol-origin: margin; left: 8px; top: 0px; }
        """

    @staticmethod
    def _tool_btn_style():
        return """
            QPushButton {
                background: #1e1e30; color: #b0b0d0; border: 1px solid #3a3a5a;
                padding: 5px; border-radius: 3px; font-size: 12px;
            }
            QPushButton:checked { background: #2d4a7a; color: white; border-color: #4a7abf; }
            QPushButton:hover   { background: #252540; }
        """


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Dark palette
    from PyQt6.QtGui import QPalette
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0e0e1a"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#d0d0e0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#13131f"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#1a1a2e"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#d0d0e0"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1e1e30"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#d0d0e0"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2d4a7a"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    app.setPalette(palette)

    win = Panel4Window()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
