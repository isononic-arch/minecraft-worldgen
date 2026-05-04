#!/usr/bin/env python3
"""
tools/override_studio.py — Vandir Override + Lithology Paint Studio (S69)
=========================================================================

Single PyQt6 tool with two tabs:

  1. **Biome Override** — paints detail zones onto override_final.png.
     Brush / flood-fill / eye-dropper / region-replace / river auto-paint.
     Overlays: height, coastal, flow, land-mask, NW-quadrant.
     Save triggers optional upscale via upscale_override_vectorized.py.

  2. **Lithology Region** — paints group IDs (1..6) onto the new
     masks/lithology_region.png override layer.  When present, this file
     overrides the biome-derived zone_to_group mapping in
     tools/build_lithology.py on a per-pixel basis.  Pixels left at 0
     (transparent) fall through to the biome-derived group.

Safety — per S69 user directive ("ensure no mismatched alignment land↔ocean,
triple-check writes for override"):
  - No np.fliplr / flipud anywhere.  Source axes are NEVER flipped.
  - NEAREST resampling ONLY for zone codes / group IDs (never bilinear).
  - Pre-save validation: zone codes in OVERRIDE_BIOME_MAP.keys(),
    lithology IDs in {0..6}, shape matches original, dtype uint8.
  - Round-trip resize check: upscale from edit-res to original res and
    confirm zone-count distribution is preserved (NEAREST is lossless for
    discrete IDs when scale factors are integer ratios; we verify this).
  - Alignment check: ocean-from-heightmap vs. land-from-override overlap
    compared, mismatched pixels reported before save.

Usage:
    py tools/override_studio.py
    py tools/override_studio.py --override override_final.png --height masks/height.tif

Hotkeys:
    B = brush, F = fill, I = eye-dropper, R = region-replace
    Ctrl+Z / Ctrl+Y = undo / redo
    Ctrl+S = save current tab
    Ctrl+Shift+S = save + upscale

Dependencies: PyQt6, numpy, rasterio, pillow, scipy.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image
import rasterio
from rasterio.enums import Resampling

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.biome_assignment import OVERRIDE_BIOME_MAP  # noqa: E402
from tools.world_biome_map import BIOME_COLORS        # noqa: E402

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QLabel, QSlider, QComboBox, QPushButton, QCheckBox,
    QFileDialog, QStatusBar, QSplitter, QGroupBox, QScrollArea,
    QSizePolicy, QMessageBox, QToolBar, QSpinBox, QTabWidget,
    QDialog, QTextEdit, QDialogButtonBox, QProgressBar,
)
from PyQt6.QtCore import (  # noqa: E402
    Qt, QPoint, QPointF, QRect, QRectF, pyqtSignal, QTimer, QProcess,
)
from PyQt6.QtGui import (  # noqa: E402
    QPixmap, QImage, QPainter, QPen, QColor, QBrush,
    QCursor, QIcon, QAction, QKeySequence, QPalette, QShortcut,
)

# =============================================================================
# Constants
# =============================================================================

DISPLAY_SIZE = 2048           # working canvas resolution (edit at this, save at orig)
UNDO_LEVELS  = 20
SEA_LEVEL_RAW = 17050         # Gaea height → MC Y=63 threshold (CLAUDE.md)

# Override + height + flow files live in the MAIN repo, not the worktree.
# Worktrees don't carry the 2.5GB masks/ folder; we always read from the
# canonical main-repo path.
MAIN_REPO_ROOT = Path(r"C:/Users/nicho/minecraft-worldgen")
DEFAULT_OVERRIDE_PATH = MAIN_REPO_ROOT / "override_final.png"
DEFAULT_HEIGHT_PATH   = MAIN_REPO_ROOT / "masks" / "height.tif"
DEFAULT_FLOW_PATH     = MAIN_REPO_ROOT / "masks" / "flow.tif"  # legacy, unused
DEFAULT_HYDRO_CENTERLINE_PATH = MAIN_REPO_ROOT / "masks" / "hydro_centerline.tif"
DEFAULT_HYDRO_ORDER_PATH      = MAIN_REPO_ROOT / "masks" / "hydro_order.tif"
DEFAULT_HYDRO_LAKE_PATH       = MAIN_REPO_ROOT / "masks" / "hydro_lake.tif"
DEFAULT_ROCK_GAP_PATH = MAIN_REPO_ROOT / "masks" / "rock_gap.tif"
DEFAULT_LITHOLOGY_REGION_PATH = MAIN_REPO_ROOT / "masks" / "lithology_region.png"
DEFAULT_HYDRO_REGION_PATH     = MAIN_REPO_ROOT / "masks" / "hydro_region.png"
UPSCALE_SCRIPT = MAIN_REPO_ROOT / "upscale_override_vectorized.py"
LITHOLOGY_CONFIG_PATH = _PROJECT_ROOT / "config" / "thresholds.json"


# =============================================================================
# Canonical color loading (biome) + lithology group loading (config)
# =============================================================================

def _load_biome_zones() -> dict[int, tuple[str, tuple[int, int, int]]]:
    """Return {zone_code: (biome_name, (r,g,b))} from canonical sources.

    Zone code 0 is "no override" — displayed as near-black outline only.
    All other codes must have a BIOME_COLORS entry (enforced on startup).
    """
    zones: dict[int, tuple[str, tuple[int, int, int]]] = {}
    zones[0] = ("No Override", (17, 17, 17))
    for code, name in OVERRIDE_BIOME_MAP.items():
        if code == 0 or not name:
            continue
        color = BIOME_COLORS.get(name)
        if color is None:
            # Protection — every active zone must have a BIOME_COLORS entry.
            raise RuntimeError(
                f"Biome '{name}' (zone {code}) missing from BIOME_COLORS in "
                f"tools/world_biome_map.py — add it before launching studio."
            )
        zones[code] = (name, color)
    return zones


def _load_lithology_groups() -> dict[int, tuple[str, tuple[int, int, int]]]:
    """Return {group_id: (group_name, (r,g,b))} from config/thresholds.json.

    Group 0 means "transparent / fallback to zone_to_group derivation".
    Group colors are fixed per-group (hand-picked for clarity, since the
    config lacks a `color` field today — they can be moved into config
    later if desired).
    """
    # Hand-picked swatches — visually distinct, avoid biome-color collisions.
    _GROUP_COLORS = {
        "granitic":              (180, 150, 140),   # pinkish granite
        "arid_basaltic":         ( 90,  85,  80),   # dark warm stone
        "temperate_basaltic":    ( 50,  50,  60),   # near-black blackstone
        "limestone":             (230, 210, 170),   # sandy tan
        "deepslate_metamorphic": (110, 110, 125),   # cold grey-blue
        "mossy_temperate":       ( 90, 120,  95),   # moss green
    }
    with open(LITHOLOGY_CONFIG_PATH) as f:
        cfg = json.load(f)
    groups_cfg = cfg["lithology"]["groups"]
    out: dict[int, tuple[str, tuple[int, int, int]]] = {
        0: ("(transparent / biome-derived)", (40, 40, 50)),
    }
    for name, data in groups_cfg.items():
        gid = int(data["id"])
        color = _GROUP_COLORS.get(name, (128, 128, 128))
        out[gid] = (name, color)
    return out


# Loaded at startup (raises if BIOME_COLORS incomplete).
BIOME_ZONES: dict[int, tuple[str, tuple[int, int, int]]] = _load_biome_zones()
LITHOLOGY_GROUPS: dict[int, tuple[str, tuple[int, int, int]]] = _load_lithology_groups()

VALID_BIOME_CODES = set(BIOME_ZONES.keys())
VALID_LITH_IDS    = set(LITHOLOGY_GROUPS.keys())


# =============================================================================
# Validation helpers (write safety)
# =============================================================================

def _validate_zone_array(
    arr: np.ndarray,
    expected_shape: tuple[int, int] | None = None,
) -> list[str]:
    """Return list of issues (empty = OK). Run before save."""
    issues: list[str] = []
    if not isinstance(arr, np.ndarray):
        issues.append(f"not a numpy array: {type(arr)}")
        return issues
    if arr.dtype != np.uint8:
        issues.append(f"dtype must be uint8, got {arr.dtype}")
    if arr.ndim != 2:
        issues.append(f"must be 2D (H, W), got shape {arr.shape}")
    if expected_shape is not None and arr.shape != expected_shape:
        issues.append(f"shape mismatch: expected {expected_shape}, got {arr.shape}")
    unique_codes = set(np.unique(arr).tolist())
    invalid = unique_codes - VALID_BIOME_CODES
    if invalid:
        issues.append(f"invalid zone codes present: {sorted(invalid)}")
    return issues


def _validate_lithology_array(
    arr: np.ndarray,
    expected_shape: tuple[int, int] | None = None,
) -> list[str]:
    issues: list[str] = []
    if not isinstance(arr, np.ndarray):
        issues.append(f"not a numpy array: {type(arr)}")
        return issues
    if arr.dtype != np.uint8:
        issues.append(f"dtype must be uint8, got {arr.dtype}")
    if arr.ndim != 2:
        issues.append(f"must be 2D, got shape {arr.shape}")
    if expected_shape is not None and arr.shape != expected_shape:
        issues.append(f"shape mismatch: expected {expected_shape}, got {arr.shape}")
    unique_ids = set(np.unique(arr).tolist())
    invalid = unique_ids - VALID_LITH_IDS
    if invalid:
        issues.append(f"invalid lithology IDs present: {sorted(invalid)} "
                      f"(valid: {sorted(VALID_LITH_IDS)})")
    return issues


def _resize_nearest_check(
    arr: np.ndarray,
    target_size: tuple[int, int],
) -> tuple[np.ndarray, list[str]]:
    """NEAREST resize arr to target_size (W, H) — PIL convention.

    Returns (resized, issues). Issues include any zone-count drift that
    shouldn't happen under NEAREST resampling.
    """
    issues: list[str] = []
    img = Image.fromarray(arr, mode="L")
    resized_img = img.resize(target_size, Image.NEAREST)
    resized = np.asarray(resized_img, dtype=np.uint8)
    # Zone set must match (NEAREST should never introduce new codes).
    src_codes = set(np.unique(arr).tolist())
    dst_codes = set(np.unique(resized).tolist())
    new_codes = dst_codes - src_codes
    if new_codes:
        issues.append(
            f"zone drift under NEAREST resize: {sorted(new_codes)} appeared "
            f"(should be impossible — investigate)"
        )
    return resized, issues


def _check_alignment_report(
    biome_arr: np.ndarray,
    height_arr_norm: np.ndarray,
    land_threshold: float,
) -> dict:
    """Cross-check: biome-land pixels should roughly match height-land pixels.

    Returns report dict with counts + mismatch rates.  A high mismatch rate
    suggests the biome override and height mask are on different axes
    (flipped X, swapped Y, etc.).
    """
    biome_land = biome_arr > 0
    height_land = height_arr_norm > land_threshold
    total = biome_land.size
    both = int((biome_land & height_land).sum())
    biome_only = int((biome_land & ~height_land).sum())
    height_only = int((~biome_land & height_land).sum())
    neither = int((~biome_land & ~height_land).sum())
    return {
        "total_pixels": total,
        "both_land": both,
        "biome_land_height_ocean": biome_only,
        "biome_ocean_height_land": height_only,
        "both_ocean": neither,
        "agreement_pct": (both + neither) / total * 100 if total else 0.0,
        "mismatch_pct": (biome_only + height_only) / total * 100 if total else 0.0,
    }


# =============================================================================
# Base canvas — shared pan/zoom/brush/fill/region-replace machinery
# =============================================================================

class BaseCanvas(QGraphicsView):
    """Shared QGraphicsView for both tabs.

    Tools: brush, fill, eyedropper, region_replace.
    Signals emit canvas-pixel coords (in DISPLAY_SIZE space).
    """
    paint_applied    = pyqtSignal(int, int)       # cx, cy  (brush stroke point)
    fill_applied     = pyqtSignal(int, int)       # cx, cy  (flood-fill seed)
    pick_applied     = pyqtSignal(int, int)       # cx, cy  (eyedropper)
    region_completed = pyqtSignal(QRect)          # canvas-pixel rectangle
    cursor_moved     = pyqtSignal(int, int)       # cx, cy
    # S69: emit on brush-stroke end so tab can reset its _stroke_dirty flag
    # and push a new undo entry on the NEXT press — otherwise only the first
    # stroke in a session was ever pushed.
    stroke_ended     = pyqtSignal()

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
        self._panning  = False
        self._pan_start = QPoint()
        self._tool = "brush"
        self._painting = False
        self._zoom = 1.0
        self._region_start: QPoint | None = None
        self._region_preview_item: QGraphicsPixmapItem | None = None

    def set_tool(self, tool: str):
        self._tool = tool
        cursors = {
            "brush":          Qt.CursorShape.ArrowCursor,
            "fill":           Qt.CursorShape.CrossCursor,
            "eyedropper":     Qt.CursorShape.PointingHandCursor,
            "region_replace": Qt.CursorShape.CrossCursor,
        }
        self.setCursor(cursors.get(tool, Qt.CursorShape.ArrowCursor))

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self._zoom *= factor
        self._zoom = max(0.05, min(32.0, self._zoom))
        self.scale(factor, factor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pt = self._scene_pt(event)
            if pt is None:
                return
            if self._tool == "brush":
                self._painting = True
                self.paint_applied.emit(pt.x(), pt.y())
            elif self._tool == "fill":
                self.fill_applied.emit(pt.x(), pt.y())
            elif self._tool == "eyedropper":
                self.pick_applied.emit(pt.x(), pt.y())
            elif self._tool == "region_replace":
                self._region_start = pt
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning:
            delta = event.position().toPoint() - self._pan_start
            self._pan_start = event.position().toPoint()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            return
        pt = self._scene_pt(event)
        if pt:
            self.cursor_moved.emit(pt.x(), pt.y())
        if self._painting and self._tool == "brush" and pt:
            self.paint_applied.emit(pt.x(), pt.y())
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        if event.button() == Qt.MouseButton.LeftButton:
            was_painting = self._painting
            self._painting = False
            if self._tool == "brush" and was_painting:
                self.stroke_ended.emit()
            if self._tool == "region_replace" and self._region_start is not None:
                pt = self._scene_pt(event)
                if pt is not None and pt != self._region_start:
                    r = QRect(self._region_start, pt).normalized()
                    # Clamp to canvas bounds.
                    r = r.intersected(QRect(0, 0, DISPLAY_SIZE, DISPLAY_SIZE))
                    if r.width() > 0 and r.height() > 0:
                        self.region_completed.emit(r)
                self._region_start = None
        super().mouseReleaseEvent(event)

    def _scene_pt(self, event) -> QPoint | None:
        scene_pos = self.mapToScene(event.position().toPoint())
        x = int(scene_pos.x()); y = int(scene_pos.y())
        if 0 <= x < DISPLAY_SIZE and 0 <= y < DISPLAY_SIZE:
            return QPoint(x, y)
        return None

    def fit_canvas(self, w: int, h: int):
        self.setSceneRect(0, 0, w, h)
        self.fitInView(QRectF(0, 0, w, h), Qt.AspectRatioMode.KeepAspectRatio)


# =============================================================================
# Shared file loading helpers
# =============================================================================

def _load_override_png(path: Path, display_size: int) -> tuple[np.ndarray, tuple[int, int]]:
    """Load override PNG (uint8, grayscale=zone codes).

    Returns (editable_1024_array, original_size_wh).
    NO flipping — raw pixel orientation preserved.
    """
    img = Image.open(path).convert("L")
    orig_size = img.size  # (w, h)
    img_small = img.resize((display_size, display_size), Image.NEAREST)
    arr = np.array(img_small, dtype=np.uint8)
    return arr, orig_size


def _load_height_tif(path: Path, display_size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load height.tif downsampled to display_size.

    Returns (normalized_float32_in_0_1, ocean_mask_bool, h_raw_float32).

    S69: switched to Resampling.nearest to preserve a sharp coastline at
    the SEA_LEVEL_RAW=17050 threshold.  Per CLAUDE.md: HIGH raw = HIGH
    terrain, ocean = raw<17050.  h_raw returned for elevation-band overlay.
    """
    with rasterio.open(path) as src:
        h_raw = src.read(
            1,
            out_shape=(display_size, display_size),
            resampling=Resampling.nearest,
        ).astype(np.float32)
    ocean_mask = h_raw < SEA_LEVEL_RAW
    mn, mx = float(h_raw.min()), float(h_raw.max())
    norm = (h_raw - mn) / max(mx - mn, 1.0)
    return norm, ocean_mask, h_raw


# Elevation bands — raw height thresholds mapped from the canonical spline
# (gaea_in=[0, 17050, 24469, 45000, 65496] → mc_y_out=[-64, 63, 130, 200, 448]).
# Each band is (raw_lo, raw_hi, RGB tuple, label).  Alpha applied by renderer.
ELEVATION_BANDS: list[tuple[int, int, tuple[int, int, int], str]] = [
    (     0,  10000, ( 10,  25,  90), "deep ocean (Y<0)"),
    ( 10000,  17050, ( 50, 100, 180), "shelf (Y ~0..63)"),
    ( 17050,  22000, (230, 210, 120), "lowland (Y 63..~100)"),
    ( 22000,  30000, (120, 180,  60), "midland (Y ~100..~170)"),
    ( 30000,  45000, (180, 110,  50), "upland (Y ~170..~260)"),
    ( 45000,  55000, (200, 180, 180), "alpine (Y ~260..~370)"),
    ( 55000, 70000 , (250, 245, 255), "peak (Y >~370)"),
]
# Alpha when bands render above biome layer — strong enough to tint clearly,
# but below ~160 so biome colors still read.
ELEVATION_BAND_ALPHA = 135


def _elevation_bands_overlay(h_raw: np.ndarray) -> np.ndarray:
    """Return (H, W, 4) uint8 RGBA overlay of discrete elevation zones."""
    H, W = h_raw.shape
    out = np.zeros((H, W, 4), dtype=np.uint8)
    a = ELEVATION_BAND_ALPHA
    for lo, hi, (r, g, b), _label in ELEVATION_BANDS:
        m = (h_raw >= lo) & (h_raw < hi)
        if m.any():
            out[m] = [r, g, b, a]
    return out


def _elevation_band_label(raw_value: float) -> str:
    """Return the band label at a given raw height (for hover tooltip)."""
    for lo, hi, _, label in ELEVATION_BANDS:
        if lo <= raw_value < hi:
            return label
    return "?"


def _load_rock_gap_tif(path: Path, display_size: int) -> np.ndarray:
    """Load rock_gap.tif (gap==5 slope-rock mask) downsampled to display_size."""
    with rasterio.open(path) as src:
        rg = src.read(
            1,
            out_shape=(display_size, display_size),
            resampling=Resampling.nearest,
        )
    return (rg > 0).astype(bool)


BRUSH_SHAPES = ["round", "ridge", "blob", "ribbon"]
# Per-shape how far the mask extent needs to reach relative to brush radius.
_SHAPE_EXTENT = {"round": 1.0, "ridge": 3.0, "blob": 1.3, "ribbon": 3.0}


def _make_brush_mask(
    cx: int, cy: int, r: int, shape: str, angle_deg: float,
    H: int, W: int,
) -> tuple[np.ndarray, int, int, int, int]:
    """Build a boolean brush mask at (cx, cy) for the given shape.
    Returns (mask, x0, y0, x1, y1) where mask is shape (y1-y0, x1-x0)."""
    import math
    # r==1 = TRUE single pixel. The disc formula at r=1 covers 5 cells
    # (center + N/S/E/W); special-case it for precision painting.
    if r == 1:
        if 0 <= cx < W and 0 <= cy < H:
            return np.ones((1, 1), dtype=bool), cx, cy, cx + 1, cy + 1
        return np.zeros((0, 0), dtype=bool), 0, 0, 0, 0
    ext = max(1, int(round(r * _SHAPE_EXTENT.get(shape, 1.0))))
    y0 = max(0, cy - ext); y1 = min(H, cy + ext + 1)
    x0 = max(0, cx - ext); x1 = min(W, cx + ext + 1)
    yy, xx = np.ogrid[y0:y1, x0:x1]
    dy = yy - cy; dx = xx - cx
    if shape == "round":
        mask = dx * dx + dy * dy <= r * r
    elif shape == "ridge":
        theta = math.radians(angle_deg)
        rx = dx * math.cos(theta) + dy * math.sin(theta)
        ry = -dx * math.sin(theta) + dy * math.cos(theta)
        # Elongated 3:1 ellipse along rotated axis
        a = r * 3.0; b = r * 1.0
        mask = (rx * rx) / (a * a) + (ry * ry) / (b * b) <= 1.0
    elif shape == "blob":
        # Circle perturbed by a 5-lobe trig — amoeba edges without noise cost
        angle_of_pixel = np.arctan2(dy, dx.astype(np.float32))
        r_eff = r * (1.0 + 0.28 * np.sin(angle_of_pixel * 5.0 + r * 0.31))
        mask = dx * dx + dy * dy <= r_eff * r_eff
    elif shape == "ribbon":
        theta = math.radians(angle_deg)
        rx = dx * math.cos(theta) + dy * math.sin(theta)
        ry = -dx * math.sin(theta) + dy * math.cos(theta)
        # Long thin strip: length = 2*r, thickness = 0.3*r
        mask = (np.abs(rx) <= r) & (np.abs(ry) <= r * 0.3)
    else:
        mask = dx * dx + dy * dy <= r * r
    return mask, x0, y0, x1, y1


def _load_uint_tif(path: Path, display_size: int, dtype=np.uint8) -> np.ndarray:
    """Generic NEAREST load of a uint mask. Used for hydro_centerline/order/lake."""
    with rasterio.open(path) as src:
        a = src.read(
            1,
            out_shape=(display_size, display_size),
            resampling=Resampling.nearest,
        )
    return a.astype(dtype)


def _load_float_tif(path: Path, display_size: int) -> np.ndarray:
    """Load a TIFF as float32 in original units (no normalisation). Bilinear
    downscale. Used for height + hydro_lake_wl so we can compute the
    terrain-intersection lake (height < wl)."""
    with rasterio.open(path) as src:
        a = src.read(
            1,
            out_shape=(display_size, display_size),
            resampling=Resampling.bilinear,
        )
    return a.astype(np.float32)


def _load_binary_mask_tif(path: Path, display_size: int) -> np.ndarray:
    """Load a 1-channel TIFF as uint8, downscaled with a "sum-then-threshold"
    trick that keeps 1-pixel-wide centerlines CONNECTED at display
    resolution.

    rasterio's Resampling.max isn't valid for read() (warp-only), so we
    do this in two passes:
        1. Read with bilinear at 4x display_size — averages source
           pixels but a 1-px line still leaves residual signal at the
           sub-pixel level.
        2. Threshold > 0 → binary mask, then numpy max-pool down to
           display_size. Any residual non-zero cell in a 4x4 source
           block becomes 1 in output. This is mathematically
           equivalent to the unsupported Resampling.max.
    """
    intermediate = display_size * 4
    with rasterio.open(path) as src:
        a = src.read(
            1,
            out_shape=(intermediate, intermediate),
            resampling=Resampling.bilinear,
        )
    binary = (a > 0).astype(np.uint8)
    # numpy max-pool 4x4 → display_size
    binary = binary.reshape(display_size, 4, display_size, 4).max(axis=(1, 3))
    return binary.astype(np.uint8)


# Hydrology-paint categories — values written to masks/hydro_region.png.
# S69 (revised): WATER FEATURES ONLY.  This layer does NOT paint biomes; it
# drives channel carving + water body placement + moisture bank fringes.
# Biome repaint stays in the Biome Override tab.
# 0 = pass-through (no override).  Each entry: (id, name, RGB).
HYDRO_REGIONS: dict[int, tuple[str, tuple[int, int, int]]] = {
    0: ("(none / pass-through)",   ( 40,  40,  50)),
    1: ("lake / oasis",             ( 70, 170, 210)),   # water body
    2: ("river / stream",           ( 40, 130, 230)),   # flowing channel
    3: ("river bank (moist)",       (140, 200, 170)),   # moisture fringe only
    4: ("dry channel (wadi)",       (185, 130,  75)),   # carved but no water
}
VALID_HYDRO_IDS = set(HYDRO_REGIONS.keys())


def _load_flow_tif(path: Path, display_size: int) -> np.ndarray:
    with rasterio.open(path) as src:
        fl = src.read(
            1,
            out_shape=(display_size, display_size),
            resampling=Resampling.average,
        ).astype(np.float32)
    mn, mx = float(fl.min()), float(fl.max())
    return (fl - mn) / max(mx - mn, 1.0)


def _ndarray_to_pixmap_rgb(arr: np.ndarray) -> QPixmap:
    h, w = arr.shape[:2]
    c_arr = np.ascontiguousarray(arr)
    img = QImage(c_arr.data, w, h, w * 3, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(img.copy())   # copy detaches from c_arr


def _ndarray_to_pixmap_rgba(arr: np.ndarray) -> QPixmap:
    h, w = arr.shape[:2]
    c_arr = np.ascontiguousarray(arr)
    img = QImage(c_arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(img.copy())


def _zone_to_rgb(arr: np.ndarray) -> np.ndarray:
    """Convert (H, W) uint8 zone codes → (H, W, 3) uint8 RGB."""
    out = np.zeros((*arr.shape, 3), dtype=np.uint8)
    for code, (_, (r, g, b)) in BIOME_ZONES.items():
        if code == 0:
            continue
        m = arr == code
        if m.any():
            out[m] = [r, g, b]
    return out


def _lith_to_rgb(arr: np.ndarray) -> np.ndarray:
    out = np.zeros((*arr.shape, 3), dtype=np.uint8)
    for gid, (_, (r, g, b)) in LITHOLOGY_GROUPS.items():
        if gid == 0:
            continue
        m = arr == gid
        if m.any():
            out[m] = [r, g, b]
    return out


# =============================================================================
# Biome Override Painter tab
# =============================================================================

class BiomePainterTab(QWidget):
    """Biome override painter.  Edits override buffer at DISPLAY_SIZE, saves
    back to override_final.png at original resolution with NEAREST upscale."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.override: np.ndarray | None = None     # (DISPLAY_SIZE, DISPLAY_SIZE) uint8
        self.height_norm: np.ndarray | None = None  # (DISPLAY_SIZE, DISPLAY_SIZE) float32
        self.ocean_mask: np.ndarray | None = None   # bool — true ocean @ Y63 threshold
        self.height_raw: np.ndarray | None = None   # raw 16-bit for elevation bands
        # S69: hydro pipeline masks — refined rivers + lakes post-hydrology
        self.hydro_centerline: np.ndarray | None = None  # uint8 non-zero = river
        self.hydro_order: np.ndarray | None = None       # uint8 Strahler order 0-5
        self.hydro_lake: np.ndarray | None = None        # uint16 lake ID
        self.orig_size: tuple[int, int] = (DISPLAY_SIZE, DISPLAY_SIZE)
        self.override_path: Path | None = None
        self.undo: deque = deque(maxlen=UNDO_LEVELS)
        self.redo: deque = deque(maxlen=UNDO_LEVELS)
        self._stroke_dirty = False
        self._current_value = 20
        self._brush_size = 8
        self._paint_over_enabled = False
        self._paint_over_value = 0
        self._show_falsecolor = True
        self._show_height = True
        self._show_hydro = False       # S69: renamed from _show_flow; now hydro pipeline
        self._show_nw_quadrant = False
        self._show_landmask = False
        self._show_ocean = True        # S69: true ocean @ Y63 (fixed threshold)
        self._show_override_ocean_warn = False   # S69: hi-lite painted land below sea
        self._show_elevation_bands = False        # S69: discrete elevation zones
        # S69: brush/fill/region clamp — only paint where h_raw is in the
        # selected elevation band.  Lets "repaint everything at alpine
        # elevation as X" in a single stroke.
        self._clamp_elev_enabled = False
        self._clamp_elev_band = 5     # default: alpine band index
        self._land_threshold = 26       # 0–100 slider → threshold = value/100
        self._flow_threshold = 90      # legacy — no longer used (hydro mask supersedes)
        self._fill_tolerance = 10
        self._opacity_height = 0.45
        self._region_src = 0
        self._region_dst = 20
        self._build_ui()
        self._connect_signals()

    # -- UI -----------------------------------------------------------------

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        self.canvas = BaseCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
        # S69 z-order (per user): biome false-color is the BASE, all overlay
        # masks render ABOVE it so their signals are visible on painted
        # biomes too (not just on unpainted pixels).
        self._bg_item       = QGraphicsPixmapItem(); self._bg_item.setZValue(0)
        self._ov_item       = QGraphicsPixmapItem(); self._ov_item.setZValue(1)
        self._ocean_item    = QGraphicsPixmapItem(); self._ocean_item.setZValue(2)
        self._elev_item     = QGraphicsPixmapItem(); self._elev_item.setZValue(3)
        self._warn_item     = QGraphicsPixmapItem(); self._warn_item.setZValue(4)
        self._flow_item     = QGraphicsPixmapItem(); self._flow_item.setZValue(5)
        self._nw_item       = QGraphicsPixmapItem(); self._nw_item.setZValue(6)
        for it in (self._bg_item, self._ov_item, self._ocean_item,
                   self._elev_item, self._warn_item, self._flow_item,
                   self._nw_item):
            self.canvas.scene.addItem(it)
        controls = self._build_controls()
        # S69: wrap controls in a scroll area so panels don't collapse on
        # small monitors.  Fixed width preserved; scroll bar only appears when
        # the vertical content exceeds viewport height.
        controls_scroll = QScrollArea()
        controls_scroll.setWidget(controls)
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFixedWidth(310)
        controls_scroll.setStyleSheet(
            "QScrollArea {background:#13131f;border:none;}"
            "QScrollBar:vertical {background:#0a0a12;width:10px;}"
            "QScrollBar::handle:vertical {background:#3a3a5a;border-radius:4px;}"
        )
        root.addWidget(self.canvas, stretch=1)
        root.addWidget(controls_scroll)

    def _build_controls(self) -> QWidget:
        ctrl = QWidget()
        ctrl.setFixedWidth(290)
        ctrl.setStyleSheet("background:#13131f;color:#d0d0e0;")
        lay = QVBoxLayout(ctrl); lay.setContentsMargins(8, 8, 8, 8); lay.setSpacing(6)

        # Tool group
        tg = QGroupBox("Tool"); tg.setStyleSheet(self._group_style())
        tgl = QHBoxLayout(tg)
        self.btn_brush = QPushButton("🖌 Brush"); self.btn_brush.setCheckable(True); self.btn_brush.setChecked(True)
        self.btn_fill = QPushButton("🪣 Fill"); self.btn_fill.setCheckable(True)
        self.btn_pick = QPushButton("💧 Pick"); self.btn_pick.setCheckable(True)
        self.btn_region = QPushButton("🔲 Region"); self.btn_region.setCheckable(True)
        for b in (self.btn_brush, self.btn_fill, self.btn_pick, self.btn_region):
            b.setStyleSheet(self._tool_btn_style()); tgl.addWidget(b)
        lay.addWidget(tg)

        # Zone selector
        zg = QGroupBox("Active Zone"); zg.setStyleSheet(self._group_style())
        zgl = QVBoxLayout(zg)
        self.zone_combo = QComboBox(); self.zone_combo.setStyleSheet(self._combo_style())
        for code, (name, _) in BIOME_ZONES.items():
            self.zone_combo.addItem(f"{code:3d}  {name}", userData=code)
        # Default to first non-zero code.
        try:
            self.zone_combo.setCurrentIndex(list(BIOME_ZONES.keys()).index(self._current_value))
        except ValueError:
            pass
        self.zone_swatch = QLabel(); self.zone_swatch.setFixedHeight(18)
        self._update_zone_swatch()
        zgl.addWidget(self.zone_combo); zgl.addWidget(self.zone_swatch)
        lay.addWidget(zg)

        # Brush size
        sg = QGroupBox("Brush Size"); sg.setStyleSheet(self._group_style())
        sgl = QHBoxLayout(sg)
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(1, 100); self.brush_slider.setValue(self._brush_size)
        self.brush_label = QLabel(f"{self._brush_size}px"); self.brush_label.setFixedWidth(38)
        sgl.addWidget(self.brush_slider); sgl.addWidget(self.brush_label)
        lay.addWidget(sg)

        # Paint-over filter
        pg = QGroupBox("Paint-Over Filter"); pg.setStyleSheet(self._group_style())
        pgl = QVBoxLayout(pg)
        self.chk_paint_over = QCheckBox("Only overwrite:")
        self.chk_paint_over.setStyleSheet("color:#d0d0e0;font-size:11px;")
        self.paint_over_combo = QComboBox(); self.paint_over_combo.setStyleSheet(self._combo_style())
        self.paint_over_combo.setEnabled(False)
        for code, (name, _) in BIOME_ZONES.items():
            self.paint_over_combo.addItem(f"{code:3d}  {name}", userData=code)
        pgl.addWidget(self.chk_paint_over); pgl.addWidget(self.paint_over_combo)
        lay.addWidget(pg)

        # Elevation-band clamp: restrict brush/fill/region to pixels in the
        # chosen elevation band.
        clg = QGroupBox("Clamp to elevation band"); clg.setStyleSheet(self._group_style())
        clgl = QVBoxLayout(clg)
        self.chk_clamp_elev = QCheckBox("Only paint where h_raw in:")
        self.chk_clamp_elev.setStyleSheet("color:#d0d0e0;font-size:11px;")
        self.chk_clamp_elev.setToolTip(
            "Restricts brush, fill, AND region-replace to pixels whose raw\n"
            "height falls in the chosen elevation band.  Essential for:\n"
            "  • 'repaint alpine-only pixels as BOREAL_ALPINE'\n"
            "  • 'move SBT off lowland pixels'\n"
            "Needs height.tif loaded."
        )
        self.clamp_elev_combo = QComboBox()
        self.clamp_elev_combo.setStyleSheet(self._combo_style())
        self.clamp_elev_combo.setEnabled(False)
        for i, (lo, hi, _color, label) in enumerate(ELEVATION_BANDS):
            self.clamp_elev_combo.addItem(f"{label}", userData=i)
        self.clamp_elev_combo.setCurrentIndex(self._clamp_elev_band)
        clgl.addWidget(self.chk_clamp_elev); clgl.addWidget(self.clamp_elev_combo)
        lay.addWidget(clg)

        # Region replace config
        rg = QGroupBox("Region Replace (drag rect)"); rg.setStyleSheet(self._group_style())
        rgl = QVBoxLayout(rg)
        row = QHBoxLayout(); row.addWidget(QLabel("From:"))
        self.region_src_combo = QComboBox(); self.region_src_combo.setStyleSheet(self._combo_style())
        for code, (name, _) in BIOME_ZONES.items():
            self.region_src_combo.addItem(f"{code:3d}  {name}", userData=code)
        row.addWidget(self.region_src_combo); rgl.addLayout(row)
        row2 = QHBoxLayout(); row2.addWidget(QLabel("To:  "))
        self.region_dst_combo = QComboBox(); self.region_dst_combo.setStyleSheet(self._combo_style())
        for code, (name, _) in BIOME_ZONES.items():
            self.region_dst_combo.addItem(f"{code:3d}  {name}", userData=code)
        row2.addWidget(self.region_dst_combo); rgl.addLayout(row2)
        hint = QLabel("Select Region tool, drag a rectangle to\nreplace From → To within that box.")
        hint.setStyleSheet("color:#606080;font-size:10px;")
        rgl.addWidget(hint)
        lay.addWidget(rg)

        # Overlays
        og = QGroupBox("Overlays"); og.setStyleSheet(self._group_style())
        ogl = QVBoxLayout(og)
        self.chk_falsecolor = QCheckBox("Biome false-colour"); self.chk_falsecolor.setChecked(True)
        self.chk_height = QCheckBox("Height reference");       self.chk_height.setChecked(True)
        self.chk_ocean = QCheckBox("Ocean @ Y63 (fixed)");     self.chk_ocean.setChecked(True)
        self.chk_ocean.setToolTip("Renders the true ocean at fixed sea level "
                                   "(raw < 17050 / MC Y=63) — independent of "
                                   "the adjustable land-threshold slider.")
        self.chk_elev = QCheckBox("Elevation bands");          self.chk_elev.setChecked(False)
        self.chk_elev.setToolTip("Colour-bands the world by raw height zones:\n"
                                  "deep ocean / shelf / lowland / midland /\n"
                                  "upland / alpine / peak.  Useful for spotting\n"
                                  "high-altitude regions and placing biomes\n"
                                  "along elevation gradients.")
        self.chk_warn = QCheckBox("⚠ Override-on-ocean");      self.chk_warn.setChecked(False)
        self.chk_warn.setToolTip("Highlights pixels where you've painted a "
                                  "biome override (zone > 0) on a pixel below "
                                  "MC sea level Y63. In-game those show as "
                                  "underwater biome tags — usually a paint "
                                  "mistake unless you want submerged terrain.")
        self.chk_flow = QCheckBox("Rivers + Lakes (hydro)");  self.chk_flow.setChecked(False)
        self.chk_flow.setToolTip(
            "Renders the refined hydrology masks from the river/lake pipeline:\n"
            "  - hydro_centerline.tif as blue river lines (thicker = higher stream order)\n"
            "  - hydro_lake.tif as cyan lake fill\n"
            "Centerlines are suppressed on SAND_DUNE_DESERT pixels UNLESS the\n"
            "pixel is inside a lake (oases). Replaces the old raw flow.tif overlay."
        )
        self.chk_landmask = QCheckBox("Land mask (slider)");   self.chk_landmask.setChecked(False)
        self.chk_nw = QCheckBox("NW quadrant outline");        self.chk_nw.setChecked(False)
        for c in (self.chk_falsecolor, self.chk_height, self.chk_ocean,
                  self.chk_elev, self.chk_warn, self.chk_flow,
                  self.chk_landmask, self.chk_nw):
            c.setStyleSheet("color:#d0d0e0;font-size:11px;"); ogl.addWidget(c)

        # Land threshold
        lt_row = QHBoxLayout(); lt_row.addWidget(QLabel("Land thresh:"))
        self.land_slider = QSlider(Qt.Orientation.Horizontal)
        self.land_slider.setRange(1, 99); self.land_slider.setValue(self._land_threshold)
        self.land_label = QLabel(f"{self._land_threshold}%"); self.land_label.setFixedWidth(32)
        lt_row.addWidget(self.land_slider); lt_row.addWidget(self.land_label)
        ogl.addLayout(lt_row)

        # Flow threshold
        ft_row = QHBoxLayout(); ft_row.addWidget(QLabel("Flow thresh:"))
        self.flow_slider = QSlider(Qt.Orientation.Horizontal)
        self.flow_slider.setRange(1, 99); self.flow_slider.setValue(self._flow_threshold)
        self.flow_label = QLabel(f"{self._flow_threshold}%"); self.flow_label.setFixedWidth(32)
        ft_row.addWidget(self.flow_slider); ft_row.addWidget(self.flow_label)
        ogl.addLayout(ft_row)

        # Fill tolerance
        ftol_row = QHBoxLayout(); ftol_row.addWidget(QLabel("Fill tol:   "))
        self.tol_slider = QSlider(Qt.Orientation.Horizontal)
        self.tol_slider.setRange(0, 50); self.tol_slider.setValue(self._fill_tolerance)
        self.tol_label = QLabel(f"±{self._fill_tolerance}"); self.tol_label.setFixedWidth(32)
        ftol_row.addWidget(self.tol_slider); ftol_row.addWidget(self.tol_label)
        ogl.addLayout(ftol_row)

        lay.addWidget(og)

        # Legend (scrollable)
        lg = QGroupBox("Legend"); lg.setStyleSheet(self._group_style())
        legend_scroll = QScrollArea(); legend_scroll.setStyleSheet("background:#0e0e1a;border:none;")
        legend_scroll.setWidgetResizable(True); legend_inner = QWidget()
        legend_layout = QVBoxLayout(legend_inner); legend_layout.setSpacing(2)
        legend_layout.setContentsMargins(4, 4, 4, 4)
        for code, (name, (r, g, b)) in BIOME_ZONES.items():
            if code == 0:
                continue
            row = QWidget(); rlay = QHBoxLayout(row)
            rlay.setContentsMargins(0, 0, 0, 0); rlay.setSpacing(4)
            sw = QLabel(); sw.setFixedSize(14, 14)
            sw.setStyleSheet(f"background:rgb({r},{g},{b});border-radius:2px;")
            lbl = QLabel(f"{code}  {name}")
            lbl.setStyleSheet("color:#b0b0c8;font-size:10px;")
            rlay.addWidget(sw); rlay.addWidget(lbl); rlay.addStretch()
            legend_layout.addWidget(row)
        legend_scroll.setWidget(legend_inner); legend_scroll.setFixedHeight(140)
        lg_lay = QVBoxLayout(lg); lg_lay.addWidget(legend_scroll)
        lay.addWidget(lg)

        lay.addStretch()

        # Buttons
        bt_row = QHBoxLayout()
        self.btn_undo = QPushButton("↩ Undo"); self.btn_redo = QPushButton("↪ Redo")
        bt_row.addWidget(self.btn_undo); bt_row.addWidget(self.btn_redo)
        lay.addLayout(bt_row)
        self.btn_preflight = QPushButton("✓ Preflight Check")
        self.btn_preflight.setStyleSheet("""
            QPushButton {background:#4a4a6e;color:white;padding:6px;border-radius:4px;}
            QPushButton:hover {background:#5a5a8e;}
        """)
        lay.addWidget(self.btn_preflight)
        self.btn_save = QPushButton("💾 Save override_final.png")
        self.btn_save.setStyleSheet("""
            QPushButton {background:#1a6b3a;color:white;font-weight:bold;padding:8px;border-radius:4px;}
            QPushButton:hover {background:#2a9b5a;}
            QPushButton:disabled {background:#333;color:#666;}
        """)
        self.btn_save.setEnabled(False); lay.addWidget(self.btn_save)
        self.btn_save_upscale = QPushButton("💾⬆ Save + Upscale to override.tif")
        self.btn_save_upscale.setStyleSheet("""
            QPushButton {background:#2a4a9f;color:white;font-weight:bold;padding:8px;border-radius:4px;}
            QPushButton:hover {background:#3a6ab0;}
            QPushButton:disabled {background:#333;color:#666;}
        """)
        self.btn_save_upscale.setEnabled(False); lay.addWidget(self.btn_save_upscale)

        return ctrl

    def _update_zone_swatch(self):
        _, (r, g, b) = BIOME_ZONES[self._current_value]
        self.zone_swatch.setStyleSheet(f"background:rgb({r},{g},{b});border-radius:3px;")

    # -- signal wiring ------------------------------------------------------

    def _connect_signals(self):
        self.canvas.paint_applied.connect(self._on_paint)
        self.canvas.fill_applied.connect(self._on_fill)
        self.canvas.pick_applied.connect(self._on_pick)
        self.canvas.region_completed.connect(self._on_region)
        self.canvas.cursor_moved.connect(self._on_cursor)
        self.canvas.stroke_ended.connect(self._on_stroke_ended)
        self.btn_brush.clicked.connect(lambda: self._set_tool("brush"))
        self.btn_fill.clicked.connect(lambda: self._set_tool("fill"))
        self.btn_pick.clicked.connect(lambda: self._set_tool("eyedropper"))
        self.btn_region.clicked.connect(lambda: self._set_tool("region_replace"))
        self.brush_slider.valueChanged.connect(self._on_brush_size)
        self.zone_combo.currentIndexChanged.connect(self._on_zone_changed)
        self.chk_paint_over.toggled.connect(self._on_paint_over_toggled)
        self.paint_over_combo.currentIndexChanged.connect(self._on_paint_over_changed)
        self.chk_clamp_elev.toggled.connect(self._on_clamp_elev_toggled)
        self.clamp_elev_combo.currentIndexChanged.connect(
            lambda i: setattr(self, "_clamp_elev_band", self.clamp_elev_combo.itemData(i)))
        self.region_src_combo.currentIndexChanged.connect(
            lambda i: setattr(self, "_region_src", self.region_src_combo.itemData(i)))
        self.region_dst_combo.currentIndexChanged.connect(
            lambda i: setattr(self, "_region_dst", self.region_dst_combo.itemData(i)))
        self.chk_falsecolor.toggled.connect(lambda v: self._set_flag("_show_falsecolor", v))
        self.chk_height.toggled.connect(lambda v: self._set_flag("_show_height", v))
        self.chk_ocean.toggled.connect(lambda v: self._set_flag("_show_ocean", v))
        self.chk_elev.toggled.connect(lambda v: self._set_flag("_show_elevation_bands", v))
        self.chk_warn.toggled.connect(lambda v: self._set_flag("_show_override_ocean_warn", v))
        self.chk_flow.toggled.connect(lambda v: self._set_flag("_show_hydro", v))
        self.chk_landmask.toggled.connect(lambda v: self._set_flag("_show_landmask", v))
        self.chk_nw.toggled.connect(lambda v: self._set_flag("_show_nw_quadrant", v))
        self.land_slider.valueChanged.connect(self._on_land_threshold)
        self.flow_slider.valueChanged.connect(self._on_flow_threshold)
        self.tol_slider.valueChanged.connect(self._on_tol_threshold)
        self.btn_undo.clicked.connect(self._do_undo)
        self.btn_redo.clicked.connect(self._do_redo)
        self.btn_preflight.clicked.connect(self.run_preflight)
        self.btn_save.clicked.connect(lambda: self.save(upscale=False))
        self.btn_save_upscale.clicked.connect(lambda: self.save(upscale=True))

    # -- data load ----------------------------------------------------------

    def load_data(self, override_path: Path, height_path: Path | None,
                  flow_path: Path | None = None,
                  hydro_centerline_path: Path | None = None,
                  hydro_order_path: Path | None = None,
                  hydro_lake_path: Path | None = None):
        """Load all data layers. Fails loud on mismatch."""
        try:
            ov_arr, orig = _load_override_png(override_path, DISPLAY_SIZE)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Override load failed: {e}")
            return False
        issues = _validate_zone_array(ov_arr, expected_shape=(DISPLAY_SIZE, DISPLAY_SIZE))
        if issues:
            # Strip any INVALID codes down to 0 rather than refusing — loudly.
            ov_arr = self._strip_invalid_codes(ov_arr)
            QMessageBox.warning(
                self, "Override sanitized",
                f"Source file had issues:\n\n" + "\n".join(issues) +
                f"\n\nInvalid codes were zeroed. Review before saving."
            )
        self.override = ov_arr
        self.orig_size = orig
        self.override_path = override_path

        if height_path and height_path.exists():
            try:
                self.height_norm, self.ocean_mask, self.height_raw = _load_height_tif(
                    height_path, DISPLAY_SIZE)
            except Exception as e:
                self.height_norm = None
                self.ocean_mask = None
                self.height_raw = None
                print(f"[biome tab] height load failed: {e}")
        else:
            self.height_norm = None
            self.ocean_mask = None
            self.height_raw = None

        # S69: hydro pipeline masks — use these in place of legacy flow.tif.
        hcp = hydro_centerline_path or DEFAULT_HYDRO_CENTERLINE_PATH
        hop = hydro_order_path or DEFAULT_HYDRO_ORDER_PATH
        hlp = hydro_lake_path or DEFAULT_HYDRO_LAKE_PATH
        try:
            # Use max-resampling so 1px centerlines stay connected after
            # the 50k→DISPLAY_SIZE downsample (NEAREST gave dotted lines).
            self.hydro_centerline = _load_binary_mask_tif(hcp, DISPLAY_SIZE) if hcp.exists() else None
        except Exception as e:
            self.hydro_centerline = None; print(f"[biome tab] hydro_centerline load failed: {e}")
        try:
            self.hydro_order = _load_uint_tif(hop, DISPLAY_SIZE, np.uint8) if hop.exists() else None
        except Exception as e:
            self.hydro_order = None; print(f"[biome tab] hydro_order load failed: {e}")
        try:
            self.hydro_lake = _load_uint_tif(hlp, DISPLAY_SIZE, np.uint16) if hlp.exists() else None
        except Exception as e:
            self.hydro_lake = None; print(f"[biome tab] hydro_lake load failed: {e}")

        # S80 v28: also load lake_wl + height (already loaded as height_raw)
        # + river.tif (legacy original-precompute river mask) so the
        # Hydrology tab can compute terrain-intersection lakes and offer
        # a toggle between original (river.tif) and WP-script-1.7
        # (hydro_centerline.tif) river overlays.
        wlp = MAIN_REPO_ROOT / "masks" / "hydro_lake_wl.tif"
        rvp = MAIN_REPO_ROOT / "masks" / "river.tif"
        try:
            self.hydro_lake_wl = _load_float_tif(wlp, DISPLAY_SIZE) if wlp.exists() else None
        except Exception as e:
            self.hydro_lake_wl = None; print(f"[biome tab] hydro_lake_wl load failed: {e}")
        try:
            self.river_legacy = _load_binary_mask_tif(rvp, DISPLAY_SIZE) if rvp.exists() else None
        except Exception as e:
            self.river_legacy = None; print(f"[biome tab] river.tif load failed: {e}")

        # S80 v30: also load flow.tif (raw Gaea flow accumulation) for the
        # tracing overlay in the Hydrology tab.
        flp = MAIN_REPO_ROOT / "masks" / "flow.tif"
        try:
            self.flow_tif = _load_float_tif(flp, DISPLAY_SIZE) if flp.exists() else None
        except Exception as e:
            self.flow_tif = None; print(f"[biome tab] flow.tif load failed: {e}")

        self.undo.clear(); self.redo.clear()
        self._update_undo_buttons()
        self.btn_save.setEnabled(True)
        self.btn_save_upscale.setEnabled(True)
        self.canvas.fit_canvas(DISPLAY_SIZE, DISPLAY_SIZE)
        self._refresh()
        return True

    def _strip_invalid_codes(self, arr: np.ndarray) -> np.ndarray:
        """Zero-out codes not in VALID_BIOME_CODES (defensive)."""
        mask = np.zeros(arr.shape, dtype=bool)
        for code in VALID_BIOME_CODES:
            mask |= (arr == code)
        out = arr.copy()
        out[~mask] = 0
        return out

    # -- display ------------------------------------------------------------

    def _refresh(self):
        if self.override is None:
            return
        h = w = DISPLAY_SIZE
        # Background: height
        if self.height_norm is not None and self._show_height:
            ht = (self.height_norm * 255 * self._opacity_height).astype(np.uint8)
            bg = np.stack([ht, ht, ht], axis=-1)
        else:
            bg = np.zeros((h, w, 3), dtype=np.uint8)
        if self._show_landmask and self.height_norm is not None:
            land = self.height_norm > (self._land_threshold / 100.0)
            bg[land, 1] = np.minimum(bg[land, 1].astype(np.int32) + 30, 255).astype(np.uint8)
        self._bg_item.setPixmap(_ndarray_to_pixmap_rgb(bg))

        # Elevation bands overlay (discrete zones from raw height)
        if self._show_elevation_bands and self.height_raw is not None:
            elev_rgba = _elevation_bands_overlay(self.height_raw)
            self._elev_item.setPixmap(_ndarray_to_pixmap_rgba(elev_rgba))
            self._elev_item.setVisible(True)
        else:
            self._elev_item.setVisible(False)

        # Ocean @ Y63 overlay — true ocean from fixed SEA_LEVEL_RAW threshold
        if self._show_ocean and self.ocean_mask is not None:
            ocean_rgba = np.zeros((h, w, 4), dtype=np.uint8)
            ocean_rgba[self.ocean_mask] = [30, 80, 160, 180]   # canonical ocean blue
            self._ocean_item.setPixmap(_ndarray_to_pixmap_rgba(ocean_rgba))
            self._ocean_item.setVisible(True)
        else:
            self._ocean_item.setVisible(False)

        # Override-on-ocean warning: painted biome on a pixel below sea level.
        # These pixels render with the biome tag UNDERWATER in-game.
        if self._show_override_ocean_warn and self.ocean_mask is not None:
            warn = (self.override > 0) & self.ocean_mask
            warn_rgba = np.zeros((h, w, 4), dtype=np.uint8)
            warn_rgba[warn] = [255, 90, 80, 220]     # hot-red pop
            self._warn_item.setPixmap(_ndarray_to_pixmap_rgba(warn_rgba))
            self._warn_item.setVisible(True)
            # Count + report once
            count = int(warn.sum())
            mw = self.window()
            if mw and hasattr(mw, "status"):
                mw.status.showMessage(
                    f"⚠ {count:,} painted-land pixels below MC Y63 "
                    f"(would render underwater in-game)")
        else:
            self._warn_item.setVisible(False)

        # Override layer
        if self._show_falsecolor:
            ov_rgb = _zone_to_rgb(self.override)
            alpha = np.where(self.override > 0, 200, 0).astype(np.uint8)
        else:
            g = self.override.copy()
            ov_rgb = np.stack([g, g, g], axis=-1)
            alpha = np.where(self.override > 0, 180, 0).astype(np.uint8)
        ov_rgba = np.dstack([ov_rgb, alpha])
        self._ov_item.setPixmap(_ndarray_to_pixmap_rgba(ov_rgba))

        # Rivers + lakes overlay (hydro pipeline).  SAND_DUNE_DESERT pixels
        # suppress centerlines unless they're inside a lake (dune-lake oases).
        if self._show_hydro and (self.hydro_centerline is not None or self.hydro_lake is not None):
            fl_rgb = np.zeros((h, w, 4), dtype=np.uint8)
            # 1. Lake fill first (underlying)
            if self.hydro_lake is not None:
                lakes = self.hydro_lake > 0
                fl_rgb[lakes] = [70, 160, 210, 200]      # cyan lake
            # 2. Rivers styled by order (higher order = saturated larger blue)
            if self.hydro_centerline is not None:
                river_mask = self.hydro_centerline > 0
                # Desert channel suppression: SAND_DUNE_DESERT painted pixels
                # hide the centerline unless they're in a hydro_lake cell.
                DESERT_CODE = 170  # SAND_DUNE_DESERT zone code
                if self.override is not None:
                    desert_painted = (self.override == DESERT_CODE)
                    has_lake = (self.hydro_lake > 0) if self.hydro_lake is not None else np.zeros_like(desert_painted)
                    river_mask = river_mask & ~(desert_painted & ~has_lake)
                if self.hydro_order is not None:
                    order = self.hydro_order.astype(np.int32)
                    # Color ramp: order 1 = muted blue, 5 = bright saturated blue
                    for o in range(1, 6):
                        m = river_mask & (order == o)
                        if m.any():
                            # Lightness decreases with order (darker = bigger)
                            b = min(255, 100 + 35 * o)
                            g = min(255, 110 + 20 * o)
                            fl_rgb[m] = [30, g, b, 230]
                else:
                    fl_rgb[river_mask] = [30, 130, 230, 230]
            self._flow_item.setPixmap(_ndarray_to_pixmap_rgba(fl_rgb))
            self._flow_item.setVisible(True)
        else:
            self._flow_item.setVisible(False)

        # NW-quadrant overlay
        if self._show_nw_quadrant:
            nw_rgba = np.zeros((h, w, 4), dtype=np.uint8)
            # 2px outline of top-left 50%x50% rectangle
            half = h // 2
            # top edge
            nw_rgba[:2, :half] = [255, 80, 80, 220]
            # left edge
            nw_rgba[:half, :2] = [255, 80, 80, 220]
            # bottom edge
            nw_rgba[half-2:half, :half] = [255, 80, 80, 220]
            # right edge
            nw_rgba[:half, half-2:half] = [255, 80, 80, 220]
            self._nw_item.setPixmap(_ndarray_to_pixmap_rgba(nw_rgba))
            self._nw_item.setVisible(True)
        else:
            self._nw_item.setVisible(False)

    # -- tool handlers ------------------------------------------------------

    def _set_tool(self, tool: str):
        self.canvas.set_tool(tool)
        self.btn_brush.setChecked(tool == "brush")
        self.btn_fill.setChecked(tool == "fill")
        self.btn_pick.setChecked(tool == "eyedropper")
        self.btn_region.setChecked(tool == "region_replace")

    def _on_paint(self, cx: int, cy: int):
        if self.override is None:
            return
        if not self._stroke_dirty:
            self._push_undo(); self._stroke_dirty = True
        r = self._brush_size
        # Special case: r==1 = TRUE single pixel.
        if r == 1:
            if not (0 <= cx < DISPLAY_SIZE and 0 <= cy < DISPLAY_SIZE):
                return
            allow = True
            if self.height_norm is not None:
                allow = allow and bool(
                    self.height_norm[cy, cx] > (self._land_threshold / 100.0)
                )
            if self._paint_over_enabled:
                allow = allow and (int(self.override[cy, cx]) == self._paint_over_value)
            clamp = self._elev_clamp_mask(cy, cy + 1, cx, cx + 1)
            if clamp is not None:
                allow = allow and bool(clamp[0, 0])
            if allow:
                self.override[cy, cx] = self._current_value
            self._refresh()
            return
        y0 = max(0, cy - r); y1 = min(DISPLAY_SIZE, cy + r + 1)
        x0 = max(0, cx - r); x1 = min(DISPLAY_SIZE, cx + r + 1)
        yy, xx = np.ogrid[y0:y1, x0:x1]
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r
        if self.height_norm is not None:
            mask = mask & (self.height_norm[y0:y1, x0:x1] > (self._land_threshold / 100.0))
        if self._paint_over_enabled:
            mask = mask & (self.override[y0:y1, x0:x1] == self._paint_over_value)
        clamp = self._elev_clamp_mask(y0, y1, x0, x1)
        if clamp is not None:
            mask = mask & clamp
        self.override[y0:y1, x0:x1][mask] = self._current_value
        self._refresh()

    def _on_fill(self, cx: int, cy: int):
        if self.override is None:
            return
        self._push_undo(); self._stroke_dirty = False
        target = int(self.override[cy, cx])
        fill = self._current_value
        if target == fill:
            return
        if self._paint_over_enabled and target != self._paint_over_value:
            return
        self._flood_fill(cx, cy, target, fill)
        self._refresh()

    def _flood_fill(self, x, y, target, fill):
        data = self.override
        h, w = data.shape
        tol = self._fill_tolerance
        if self.height_norm is not None:
            land = self.height_norm > (self._land_threshold / 100.0)
        else:
            land = np.ones((h, w), dtype=bool)
        # S69: elevation-band clamp (full-array, brush/fill/region share it).
        if self._clamp_elev_enabled and self.height_raw is not None:
            lo, hi, _, _ = ELEVATION_BANDS[self._clamp_elev_band]
            elev_allow = (self.height_raw >= lo) & (self.height_raw < hi)
        else:
            elev_allow = None

        def passable(cx, cy):
            if not land[cy, cx]:
                return False
            if elev_allow is not None and not elev_allow[cy, cx]:
                return False
            return abs(int(data[cy, cx]) - target) <= tol

        if not passable(x, y):
            return
        stack = [(x, y)]
        visited = np.zeros((h, w), dtype=bool)
        while stack:
            cx, cy = stack.pop()
            if cx < 0 or cx >= w or cy < 0 or cy >= h: continue
            if visited[cy, cx] or not passable(cx, cy): continue
            lx = cx
            while lx >= 0 and passable(lx, cy) and not visited[cy, lx]: lx -= 1
            lx += 1
            rx = cx
            while rx < w and passable(rx, cy) and not visited[cy, rx]: rx += 1
            rx -= 1
            data[cy, lx:rx+1] = fill
            visited[cy, lx:rx+1] = True
            for nx in range(lx, rx + 1):
                if cy - 1 >= 0 and not visited[cy-1, nx] and passable(nx, cy - 1):
                    stack.append((nx, cy - 1))
                if cy + 1 < h and not visited[cy+1, nx] and passable(nx, cy + 1):
                    stack.append((nx, cy + 1))

    def _on_pick(self, cx: int, cy: int):
        if self.override is None:
            return
        val = int(self.override[cy, cx])
        if val in BIOME_ZONES:
            self._current_value = val
            # Update combo + swatch
            idx = list(BIOME_ZONES.keys()).index(val)
            self.zone_combo.setCurrentIndex(idx)

    def _on_region(self, rect: QRect):
        if self.override is None or self._region_src == self._region_dst:
            return
        self._push_undo(); self._stroke_dirty = False
        x0, y0 = rect.left(), rect.top()
        x1, y1 = rect.right() + 1, rect.bottom() + 1
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(DISPLAY_SIZE, x1), min(DISPLAY_SIZE, y1)
        region = self.override[y0:y1, x0:x1]
        mask = region == self._region_src
        clamp = self._elev_clamp_mask(y0, y1, x0, x1)
        if clamp is not None:
            mask = mask & clamp
        changed = int(mask.sum())
        region[mask] = self._region_dst
        self.override[y0:y1, x0:x1] = region
        self._refresh()
        clamp_info = ""
        if clamp is not None:
            _, _, _, lbl = ELEVATION_BANDS[self._clamp_elev_band]
            clamp_info = f" [clamped to {lbl}]"
        QMessageBox.information(
            self, "Region Replace",
            f"Replaced {changed:,} pixels of {self._region_src} → {self._region_dst} "
            f"in region ({x0},{y0})–({x1},{y1}){clamp_info}.",
        )

    def _on_stroke_ended(self):
        """S69: reset stroke_dirty so the NEXT brush press pushes a new undo
        entry. Before this, only the first stroke after load was saved."""
        self._stroke_dirty = False

    def _on_cursor(self, cx: int, cy: int):
        if self.override is None:
            return
        val = int(self.override[cy, cx])
        name = BIOME_ZONES.get(val, ("?", None))[0]
        # World coords: canvas pixel → original coord
        ow, oh = self.orig_size
        wx = int(cx * ow / DISPLAY_SIZE)
        wy = int(cy * oh / DISPLAY_SIZE)
        # S69: append raw height + elevation band so user can diagnose
        # biome-vs-elevation mismatches directly from the status bar.
        elev_info = ""
        if self.height_raw is not None:
            raw = int(self.height_raw[cy, cx])
            band = _elevation_band_label(raw)
            elev_info = f"  h_raw={raw:5d}  band={band}"
        mw = self.window()
        if mw and hasattr(mw, "status"):
            mw.status.showMessage(
                f"cursor: canvas ({cx},{cy})  orig ({wx},{wy})  "
                f"zone={val} ({name}){elev_info}"
            )

    # -- slider handlers ----------------------------------------------------

    def _on_brush_size(self, v):
        self._brush_size = v; self.brush_label.setText(f"{v}px")

    def _on_zone_changed(self, idx):
        self._current_value = self.zone_combo.itemData(idx)
        self._update_zone_swatch()

    def _set_flag(self, attr, v):
        setattr(self, attr, v); self._refresh()

    def _on_paint_over_toggled(self, enabled):
        self._paint_over_enabled = enabled
        self.paint_over_combo.setEnabled(enabled)

    def _on_clamp_elev_toggled(self, enabled):
        self._clamp_elev_enabled = enabled
        self.clamp_elev_combo.setEnabled(enabled)

    def _elev_clamp_mask(self, y0: int, y1: int, x0: int, x1: int) -> np.ndarray | None:
        """Helper — returns (y1-y0, x1-x0) bool mask True where h_raw is in
        the currently-selected elevation band, or None if clamp is off or
        height.tif isn't loaded."""
        if not self._clamp_elev_enabled or self.height_raw is None:
            return None
        lo, hi, _, _ = ELEVATION_BANDS[self._clamp_elev_band]
        return (self.height_raw[y0:y1, x0:x1] >= lo) & \
               (self.height_raw[y0:y1, x0:x1] < hi)

    def _on_paint_over_changed(self, idx):
        self._paint_over_value = self.paint_over_combo.itemData(idx)

    def _on_land_threshold(self, v):
        self._land_threshold = v; self.land_label.setText(f"{v}%"); self._refresh()

    def _on_flow_threshold(self, v):
        self._flow_threshold = v; self.flow_label.setText(f"{v}%"); self._refresh()

    def _on_tol_threshold(self, v):
        self._fill_tolerance = v; self.tol_label.setText(f"±{v}")

    # -- undo/redo ----------------------------------------------------------

    def _push_undo(self):
        if self.override is not None:
            self.undo.append(self.override.copy()); self.redo.clear()
            self._update_undo_buttons()

    def _do_undo(self):
        if not self.undo or self.override is None: return
        self.redo.append(self.override.copy())
        self.override = self.undo.pop()
        self._refresh(); self._update_undo_buttons()

    def _do_redo(self):
        if not self.redo or self.override is None: return
        self.undo.append(self.override.copy())
        self.override = self.redo.pop()
        self._refresh(); self._update_undo_buttons()

    def _update_undo_buttons(self):
        self.btn_undo.setEnabled(bool(self.undo))
        self.btn_redo.setEnabled(bool(self.redo))

    # -- preflight + save ---------------------------------------------------

    def run_preflight(self):
        """Run all sanity checks WITHOUT saving. Shows a dialog with the
        report so user can spot alignment issues before committing to disk."""
        if self.override is None:
            return
        report_lines: list[str] = []
        report_lines.append(f"Edit buffer shape: {self.override.shape}  dtype={self.override.dtype}")
        report_lines.append(f"Original (save target) size: {self.orig_size} (W, H)")
        report_lines.append("")
        # Zone validation
        issues = _validate_zone_array(self.override, (DISPLAY_SIZE, DISPLAY_SIZE))
        if issues:
            report_lines.append("⚠ Zone validation issues:")
            for i in issues:
                report_lines.append(f"  - {i}")
        else:
            report_lines.append("✓ All zone codes valid.")
        # Zone distribution
        vals, counts = np.unique(self.override, return_counts=True)
        total = int(self.override.size)
        report_lines.append("")
        report_lines.append("Zone distribution (edit buffer):")
        for v, c in zip(vals, counts):
            name = BIOME_ZONES.get(int(v), ("?", None))[0]
            pct = c / total * 100
            report_lines.append(f"  {int(v):3d} ({name}): {int(c):10,} px ({pct:5.2f}%)")
        # Round-trip
        report_lines.append("")
        resized, rt_issues = _resize_nearest_check(self.override, self.orig_size)
        if rt_issues:
            report_lines.append("⚠ Round-trip NEAREST resize issues:")
            for i in rt_issues:
                report_lines.append(f"  - {i}")
        else:
            report_lines.append(f"✓ Round-trip NEAREST resize to {self.orig_size} clean "
                                f"(no zone drift).")
        # Alignment
        if self.height_norm is not None:
            rep = _check_alignment_report(
                self.override, self.height_norm, self._land_threshold / 100.0)
            report_lines.append("")
            report_lines.append(f"Biome-vs-Height alignment @ land_thresh={self._land_threshold}%:")
            report_lines.append(f"  both land:     {rep['both_land']:10,}")
            report_lines.append(f"  both ocean:    {rep['both_ocean']:10,}")
            report_lines.append(f"  biome-land / height-ocean: {rep['biome_land_height_ocean']:10,}")
            report_lines.append(f"  biome-ocean / height-land: {rep['biome_ocean_height_land']:10,}")
            report_lines.append(f"  agreement: {rep['agreement_pct']:.2f}%  "
                                f"mismatch: {rep['mismatch_pct']:.2f}%")
            if rep["mismatch_pct"] > 15:
                report_lines.append("")
                report_lines.append("⚠ HIGH mismatch (>15%) — possible axis flip or "
                                    "scale issue. Verify land_thresh first; if still "
                                    "high, the override and height.tif may be on "
                                    "different coordinate systems.")
        else:
            report_lines.append("")
            report_lines.append("(height.tif not loaded — skipping alignment check)")

        _show_report(self.window(), "Biome Override Preflight", "\n".join(report_lines))

    def save(self, upscale: bool = False):
        if self.override is None:
            return
        # ---- validation ----
        issues = _validate_zone_array(self.override, (DISPLAY_SIZE, DISPLAY_SIZE))
        if issues:
            reply = QMessageBox.warning(
                self, "Validation failed",
                "Edit buffer has issues:\n\n" + "\n".join(issues) +
                "\n\nAbort save?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                return

        # ---- resize to orig (NEAREST) + re-validate ----
        resized, rt_issues = _resize_nearest_check(self.override, self.orig_size)
        if rt_issues:
            reply = QMessageBox.critical(
                self, "Round-trip check failed",
                "Resize introduced zone drift (should be impossible):\n\n" +
                "\n".join(rt_issues) + "\n\nAbort save?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                return

        # ---- save target ----
        save_path = self.override_path or DEFAULT_OVERRIDE_PATH
        save_str, _ = QFileDialog.getSaveFileName(
            self, "Save override PNG", str(save_path), "PNG Images (*.png)")
        if not save_str:
            return
        save_p = Path(save_str)
        # Backup existing file if present.
        if save_p.exists():
            bak = save_p.with_suffix(save_p.suffix + ".bak")
            try:
                shutil.copy2(save_p, bak)
            except Exception:
                pass
        try:
            Image.fromarray(resized, mode="L").save(save_p)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return

        msg = (f"Saved: {save_p.name}\n\nSize: {self.orig_size[0]}×{self.orig_size[1]}\n"
               f"Backup: {save_p.name}.bak (if file existed)")
        if upscale:
            self._run_upscale(save_p)
        else:
            QMessageBox.information(self, "Saved", msg)

    def _run_upscale(self, override_png_path: Path):
        """Spawn upscale_override_vectorized.py via QProcess. Shows progress
        dialog with stdout."""
        if not UPSCALE_SCRIPT.exists():
            QMessageBox.warning(
                self, "Upscale script missing",
                f"Can't find {UPSCALE_SCRIPT}. Saved PNG only."
            )
            return
        dlg = _ProcessProgressDialog(self.window(), "Upscaling override → 50k TIF",
                                      f"Running {UPSCALE_SCRIPT.name}\nThis takes a few minutes...")
        dlg.run(sys.executable, [str(UPSCALE_SCRIPT)])

    # -- style helpers ------------------------------------------------------

    @staticmethod
    def _group_style():
        return """
            QGroupBox {color:#8080b0;font-size:11px;font-weight:bold;
                       border:1px solid #2a2a40;border-radius:4px;
                       margin-top:8px;padding-top:6px;}
            QGroupBox::title {subcontrol-origin:margin;left:8px;top:0px;}
        """

    @staticmethod
    def _tool_btn_style():
        return """
            QPushButton {background:#1e1e30;color:#b0b0d0;border:1px solid #3a3a5a;
                         padding:5px;border-radius:3px;font-size:11px;}
            QPushButton:checked {background:#2d4a7a;color:white;border-color:#4a7abf;}
            QPushButton:hover {background:#252540;}
        """

    @staticmethod
    def _combo_style():
        return """
            QComboBox {background:#1e1e30;color:#d0d0e0;border:1px solid #3a3a5a;
                       padding:4px;font-size:11px;}
            QComboBox::drop-down {border:none;}
            QComboBox QAbstractItemView {background:#1e1e30;color:#d0d0e0;}
        """


# =============================================================================
# Lithology Painter tab
# =============================================================================

class LithologyPainterTab(QWidget):
    """Paints group IDs (1..6) onto the new masks/lithology_region.png override
    layer.  0 = transparent (fallback to zone_to_group derivation).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.lith: np.ndarray | None = None          # (DISPLAY_SIZE, DISPLAY_SIZE) uint8, paint buffer
        self.biome_backdrop: np.ndarray | None = None  # zone codes for display
        self.rock_gap_mask: np.ndarray | None = None   # bool — exposed-rock slopes (gap==5)
        self.ocean_mask: np.ndarray | None = None      # bool — true ocean @ Y63
        self.height_raw: np.ndarray | None = None      # raw 16-bit for elevation clamp
        self.orig_size: tuple[int, int] = (DISPLAY_SIZE, DISPLAY_SIZE)
        self.lith_path: Path = DEFAULT_LITHOLOGY_REGION_PATH
        self.undo: deque = deque(maxlen=UNDO_LEVELS)
        self.redo: deque = deque(maxlen=UNDO_LEVELS)
        self._stroke_dirty = False
        self._current_gid = 1
        self._brush_size = 8
        self._show_biome_backdrop = True
        self._show_rock_gap = True                 # S69: exposed-rock slope overlay
        self._backdrop_opacity = 0.40
        # S69 brush + clamp modifiers
        self._brush_shape = "round"                # round / ridge / blob / ribbon
        self._brush_angle = 0                      # 0-180° for ridge + ribbon
        self._brush_scatter = 100                  # probability brush: 1-100%
        self._ocean_clamp = "off"                  # off / land / ocean
        self._clamp_elev_enabled = False
        self._clamp_elev_band = 5                  # default alpine band index
        self._show_ocean = True                    # true-Y63 ocean overlay in lith tab
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4); root.setSpacing(4)
        self.canvas = BaseCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._bg_item     = QGraphicsPixmapItem(); self._bg_item.setZValue(0)
        self._rock_item   = QGraphicsPixmapItem(); self._rock_item.setZValue(1)
        self._lith_item   = QGraphicsPixmapItem(); self._lith_item.setZValue(2)
        self._ocean_item  = QGraphicsPixmapItem(); self._ocean_item.setZValue(3)
        for it in (self._bg_item, self._rock_item, self._lith_item,
                   self._ocean_item):
            self.canvas.scene.addItem(it)
        controls = self._build_controls()
        controls_scroll = QScrollArea()
        controls_scroll.setWidget(controls)
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFixedWidth(310)
        controls_scroll.setStyleSheet(
            "QScrollArea {background:#13131f;border:none;}"
            "QScrollBar:vertical {background:#0a0a12;width:10px;}"
            "QScrollBar::handle:vertical {background:#3a3a5a;border-radius:4px;}"
        )
        root.addWidget(self.canvas, stretch=1)
        root.addWidget(controls_scroll)

    def _build_controls(self) -> QWidget:
        ctrl = QWidget(); ctrl.setFixedWidth(290)
        ctrl.setStyleSheet("background:#13131f;color:#d0d0e0;")
        lay = QVBoxLayout(ctrl); lay.setContentsMargins(8, 8, 8, 8); lay.setSpacing(6)

        # Tools
        tg = QGroupBox("Tool"); tg.setStyleSheet(BiomePainterTab._group_style())
        tgl = QHBoxLayout(tg)
        self.btn_brush = QPushButton("🖌 Brush"); self.btn_brush.setCheckable(True); self.btn_brush.setChecked(True)
        self.btn_fill  = QPushButton("🪣 Fill"); self.btn_fill.setCheckable(True)
        self.btn_pick  = QPushButton("💧 Pick"); self.btn_pick.setCheckable(True)
        for b in (self.btn_brush, self.btn_fill, self.btn_pick):
            b.setStyleSheet(BiomePainterTab._tool_btn_style()); tgl.addWidget(b)
        lay.addWidget(tg)

        # Group selector
        gg = QGroupBox("Active Group"); gg.setStyleSheet(BiomePainterTab._group_style())
        ggl = QVBoxLayout(gg)
        self.gid_combo = QComboBox(); self.gid_combo.setStyleSheet(BiomePainterTab._combo_style())
        for gid, (name, _) in LITHOLOGY_GROUPS.items():
            self.gid_combo.addItem(f"{gid}  {name}", userData=gid)
        try:
            self.gid_combo.setCurrentIndex(list(LITHOLOGY_GROUPS.keys()).index(self._current_gid))
        except ValueError:
            pass
        self.gid_swatch = QLabel(); self.gid_swatch.setFixedHeight(18)
        self._update_gid_swatch()
        ggl.addWidget(self.gid_combo); ggl.addWidget(self.gid_swatch)
        lay.addWidget(gg)

        # Brush size
        sg = QGroupBox("Brush Size"); sg.setStyleSheet(BiomePainterTab._group_style())
        sgl = QHBoxLayout(sg)
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(1, 100); self.brush_slider.setValue(self._brush_size)
        self.brush_label = QLabel(f"{self._brush_size}px"); self.brush_label.setFixedWidth(38)
        sgl.addWidget(self.brush_slider); sgl.addWidget(self.brush_label)
        lay.addWidget(sg)

        # Brush shape + angle
        bsg = QGroupBox("Brush Shape"); bsg.setStyleSheet(BiomePainterTab._group_style())
        bsgl = QVBoxLayout(bsg)
        self.shape_combo = QComboBox(); self.shape_combo.setStyleSheet(BiomePainterTab._combo_style())
        for s in BRUSH_SHAPES:
            self.shape_combo.addItem(s, userData=s)
        self.shape_combo.setCurrentText(self._brush_shape)
        bsgl.addWidget(self.shape_combo)
        ang_row = QHBoxLayout(); ang_row.addWidget(QLabel("Angle:"))
        self.angle_slider = QSlider(Qt.Orientation.Horizontal)
        self.angle_slider.setRange(0, 180); self.angle_slider.setValue(self._brush_angle)
        self.angle_label = QLabel(f"{self._brush_angle}°"); self.angle_label.setFixedWidth(38)
        ang_row.addWidget(self.angle_slider); ang_row.addWidget(self.angle_label)
        bsgl.addLayout(ang_row)
        hint = QLabel(
            "round = disk · ridge = elongated ellipse\n"
            "blob = amoeba (organic edges) · ribbon = thin line"
        )
        hint.setStyleSheet("color:#606080;font-size:10px;")
        bsgl.addWidget(hint)
        # Scatter / probability — per-pixel density inside the shape.
        scat_row = QHBoxLayout(); scat_row.addWidget(QLabel("Density:"))
        self.scatter_slider = QSlider(Qt.Orientation.Horizontal)
        self.scatter_slider.setRange(1, 100); self.scatter_slider.setValue(self._brush_scatter)
        self.scatter_label = QLabel(f"{self._brush_scatter}%"); self.scatter_label.setFixedWidth(38)
        scat_row.addWidget(self.scatter_slider); scat_row.addWidget(self.scatter_label)
        bsgl.addLayout(scat_row)
        scat_hint = QLabel("100% = solid fill.  <100% = probability per pixel —\n"
                            "great for transitional bands between groups.")
        scat_hint.setStyleSheet("color:#606080;font-size:10px;")
        bsgl.addWidget(scat_hint)
        lay.addWidget(bsg)

        # Ocean clamp
        ocg = QGroupBox("Clamp — Ocean / Land"); ocg.setStyleSheet(BiomePainterTab._group_style())
        ocgl = QVBoxLayout(ocg)
        self.ocean_clamp_combo = QComboBox()
        self.ocean_clamp_combo.setStyleSheet(BiomePainterTab._combo_style())
        self.ocean_clamp_combo.addItem("off — paint anywhere", userData="off")
        self.ocean_clamp_combo.addItem("land only (raw ≥ 17050)", userData="land")
        self.ocean_clamp_combo.addItem("ocean only (raw < 17050)", userData="ocean")
        ocgl.addWidget(self.ocean_clamp_combo)
        ocg_hint = QLabel("Uses the fixed SEA_LEVEL_RAW (17050) threshold\n"
                          "from height.tif, independent of any land slider.")
        ocg_hint.setStyleSheet("color:#606080;font-size:10px;")
        ocgl.addWidget(ocg_hint)
        lay.addWidget(ocg)

        # Elevation-band clamp (same pattern as biome tab)
        ebg = QGroupBox("Clamp — Elevation Band"); ebg.setStyleSheet(BiomePainterTab._group_style())
        ebgl = QVBoxLayout(ebg)
        self.chk_clamp_elev = QCheckBox("Only paint where h_raw in:")
        self.chk_clamp_elev.setStyleSheet("color:#d0d0e0;font-size:11px;")
        self.clamp_elev_combo = QComboBox()
        self.clamp_elev_combo.setStyleSheet(BiomePainterTab._combo_style())
        self.clamp_elev_combo.setEnabled(False)
        for i, (lo, hi, _color, label) in enumerate(ELEVATION_BANDS):
            self.clamp_elev_combo.addItem(f"{label}", userData=i)
        self.clamp_elev_combo.setCurrentIndex(self._clamp_elev_band)
        ebgl.addWidget(self.chk_clamp_elev); ebgl.addWidget(self.clamp_elev_combo)
        eb_hint = QLabel("Stacks with Ocean/Land clamp and with the brush\n"
                         "shape — all filters AND together.")
        eb_hint.setStyleSheet("color:#606080;font-size:10px;")
        ebgl.addWidget(eb_hint)
        lay.addWidget(ebg)

        # Biome backdrop
        bg = QGroupBox("Biome Backdrop"); bg.setStyleSheet(BiomePainterTab._group_style())
        bgl = QVBoxLayout(bg)
        self.chk_backdrop = QCheckBox("Show biome zones behind lithology")
        self.chk_backdrop.setChecked(True); self.chk_backdrop.setStyleSheet("color:#d0d0e0;font-size:11px;")
        bgl.addWidget(self.chk_backdrop)
        self.chk_rock = QCheckBox("Show rock-exposed slopes (gap==5)")
        self.chk_rock.setChecked(True); self.chk_rock.setStyleSheet("color:#d0d0e0;font-size:11px;")
        self.chk_rock.setToolTip(
            "Overlays the Gaea slope-derived exposed-rock mask from\n"
            "masks/rock_gap.tif (gap==5).  These are the high-slope areas\n"
            "where the pipeline exposes basement stone — i.e. the pixels\n"
            "where your lithology painting is most visible in-game."
        )
        bgl.addWidget(self.chk_rock)
        self.chk_ocean_show = QCheckBox("Show ocean @ Y63 (fixed)")
        self.chk_ocean_show.setChecked(True); self.chk_ocean_show.setStyleSheet("color:#d0d0e0;font-size:11px;")
        self.chk_ocean_show.setToolTip(
            "Overlays the true in-game ocean from the raw 17050 sea-level\n"
            "threshold.  Independent of the adjustable land slider."
        )
        bgl.addWidget(self.chk_ocean_show)
        op_row = QHBoxLayout(); op_row.addWidget(QLabel("Opacity:"))
        self.op_slider = QSlider(Qt.Orientation.Horizontal)
        self.op_slider.setRange(0, 100); self.op_slider.setValue(int(self._backdrop_opacity * 100))
        op_row.addWidget(self.op_slider); bgl.addLayout(op_row)
        lay.addWidget(bg)

        # Legend
        lg = QGroupBox("Legend"); lg.setStyleSheet(BiomePainterTab._group_style())
        lg_lay = QVBoxLayout(lg)
        for gid, (name, (r, g, b)) in LITHOLOGY_GROUPS.items():
            row = QWidget(); rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0); rl.setSpacing(4)
            sw = QLabel(); sw.setFixedSize(14, 14)
            sw.setStyleSheet(f"background:rgb({r},{g},{b});border-radius:2px;")
            lbl = QLabel(f"{gid}  {name}"); lbl.setStyleSheet("color:#b0b0c8;font-size:10px;")
            rl.addWidget(sw); rl.addWidget(lbl); rl.addStretch()
            lg_lay.addWidget(row)
        lay.addWidget(lg)

        lay.addStretch()

        bt_row = QHBoxLayout()
        self.btn_undo = QPushButton("↩ Undo"); self.btn_redo = QPushButton("↪ Redo")
        bt_row.addWidget(self.btn_undo); bt_row.addWidget(self.btn_redo)
        lay.addLayout(bt_row)

        self.btn_prefill = QPushButton("⟲ Prefill from zone_to_group")
        self.btn_prefill.setStyleSheet("""
            QPushButton {background:#4a4a6e;color:white;padding:6px;border-radius:4px;}
            QPushButton:hover {background:#5a5a8e;}
        """)
        self.btn_prefill.setToolTip(
            "Fill the lithology buffer with the current zone_to_group derivation.\n"
            "Use this to start from 'parity with current pipeline', then paint only\n"
            "the regions you want to differ."
        )
        lay.addWidget(self.btn_prefill)

        self.btn_preflight = QPushButton("✓ Preflight Check")
        self.btn_preflight.setStyleSheet("""
            QPushButton {background:#4a4a6e;color:white;padding:6px;border-radius:4px;}
            QPushButton:hover {background:#5a5a8e;}
        """)
        lay.addWidget(self.btn_preflight)

        self.btn_save = QPushButton("💾 Save masks/lithology_region.png")
        self.btn_save.setStyleSheet("""
            QPushButton {background:#1a6b3a;color:white;font-weight:bold;padding:8px;border-radius:4px;}
            QPushButton:hover {background:#2a9b5a;}
            QPushButton:disabled {background:#333;color:#666;}
        """)
        self.btn_save.setEnabled(False); lay.addWidget(self.btn_save)

        return ctrl

    def _update_gid_swatch(self):
        _, (r, g, b) = LITHOLOGY_GROUPS[self._current_gid]
        self.gid_swatch.setStyleSheet(f"background:rgb({r},{g},{b});border-radius:3px;")

    def _connect_signals(self):
        self.canvas.paint_applied.connect(self._on_paint)
        self.canvas.fill_applied.connect(self._on_fill)
        self.canvas.pick_applied.connect(self._on_pick)
        self.canvas.cursor_moved.connect(self._on_cursor)
        self.canvas.stroke_ended.connect(self._on_stroke_ended)
        self.btn_brush.clicked.connect(lambda: self._set_tool("brush"))
        self.btn_fill.clicked.connect(lambda: self._set_tool("fill"))
        self.btn_pick.clicked.connect(lambda: self._set_tool("eyedropper"))
        self.brush_slider.valueChanged.connect(
            lambda v: (setattr(self, "_brush_size", v),
                       self.brush_label.setText(f"{v}px")))
        self.shape_combo.currentIndexChanged.connect(
            lambda i: setattr(self, "_brush_shape", self.shape_combo.itemData(i)))
        self.angle_slider.valueChanged.connect(
            lambda v: (setattr(self, "_brush_angle", v),
                       self.angle_label.setText(f"{v}°")))
        self.ocean_clamp_combo.currentIndexChanged.connect(
            lambda i: setattr(self, "_ocean_clamp", self.ocean_clamp_combo.itemData(i)))
        self.scatter_slider.valueChanged.connect(
            lambda v: (setattr(self, "_brush_scatter", v),
                       self.scatter_label.setText(f"{v}%")))
        self.chk_clamp_elev.toggled.connect(self._on_clamp_elev_toggled)
        self.clamp_elev_combo.currentIndexChanged.connect(
            lambda i: setattr(self, "_clamp_elev_band", self.clamp_elev_combo.itemData(i)))
        self.chk_ocean_show.toggled.connect(
            lambda v: (setattr(self, "_show_ocean", v), self._refresh()))
        self.gid_combo.currentIndexChanged.connect(self._on_gid_changed)
        self.chk_backdrop.toggled.connect(lambda v: (setattr(self, "_show_biome_backdrop", v), self._refresh()))
        self.chk_rock.toggled.connect(lambda v: (setattr(self, "_show_rock_gap", v), self._refresh()))
        self.op_slider.valueChanged.connect(lambda v: (setattr(self, "_backdrop_opacity", v / 100.0), self._refresh()))
        self.btn_undo.clicked.connect(self._do_undo)
        self.btn_redo.clicked.connect(self._do_redo)
        self.btn_prefill.clicked.connect(self._prefill_from_zone_to_group)
        self.btn_preflight.clicked.connect(self.run_preflight)
        self.btn_save.clicked.connect(self.save)

    def load_data(self, biome_backdrop: np.ndarray, orig_size: tuple[int, int],
                  rock_gap_path: Path | None = None,
                  ocean_mask: np.ndarray | None = None,
                  height_raw: np.ndarray | None = None):
        """Load biome backdrop (for reference) + optional rock-gap overlay +
        ocean_mask + height_raw (both from biome-tab height.tif).  Lithology
        buffer loaded separately below from lith_path."""
        self.biome_backdrop = biome_backdrop.copy()
        self.orig_size = orig_size
        self.ocean_mask = ocean_mask.copy() if ocean_mask is not None else None
        self.height_raw = height_raw.copy() if height_raw is not None else None
        if rock_gap_path is None:
            rock_gap_path = DEFAULT_ROCK_GAP_PATH
        if rock_gap_path and rock_gap_path.exists():
            try:
                self.rock_gap_mask = _load_rock_gap_tif(rock_gap_path, DISPLAY_SIZE)
            except Exception as e:
                print(f"[lith tab] rock_gap load failed: {e}")
                self.rock_gap_mask = None
        else:
            self.rock_gap_mask = None
        if self.lith_path.exists():
            try:
                img = Image.open(self.lith_path).convert("L")
                img_small = img.resize((DISPLAY_SIZE, DISPLAY_SIZE), Image.NEAREST)
                self.lith = np.array(img_small, dtype=np.uint8)
                self.orig_size = img.size
                issues = _validate_lithology_array(self.lith, (DISPLAY_SIZE, DISPLAY_SIZE))
                if issues:
                    QMessageBox.warning(
                        self, "Lithology sanitized",
                        "Issues:\n\n" + "\n".join(issues) +
                        "\n\nInvalid IDs zeroed."
                    )
                    self.lith = self._strip_invalid(self.lith)
            except Exception as e:
                print(f"[lith] load failed: {e}, starting blank")
                self.lith = np.zeros((DISPLAY_SIZE, DISPLAY_SIZE), dtype=np.uint8)
        else:
            # Start blank; user can Prefill from zone_to_group to seed.
            self.lith = np.zeros((DISPLAY_SIZE, DISPLAY_SIZE), dtype=np.uint8)
        self.undo.clear(); self.redo.clear()
        self._update_undo_buttons()
        self.btn_save.setEnabled(True)
        self.canvas.fit_canvas(DISPLAY_SIZE, DISPLAY_SIZE)
        self._refresh()

    def _strip_invalid(self, arr: np.ndarray) -> np.ndarray:
        mask = np.zeros(arr.shape, dtype=bool)
        for gid in VALID_LITH_IDS:
            mask |= (arr == gid)
        out = arr.copy(); out[~mask] = 0
        return out

    def _refresh(self):
        if self.lith is None:
            return
        h = w = DISPLAY_SIZE
        # Background: biome zones (dimmed)
        if self._show_biome_backdrop and self.biome_backdrop is not None:
            biome_rgb = _zone_to_rgb(self.biome_backdrop)
            op = self._backdrop_opacity
            biome_rgb = (biome_rgb.astype(np.float32) * op).astype(np.uint8)
            self._bg_item.setPixmap(_ndarray_to_pixmap_rgb(biome_rgb))
        else:
            blank = np.zeros((h, w, 3), dtype=np.uint8)
            self._bg_item.setPixmap(_ndarray_to_pixmap_rgb(blank))

        # Rock-exposed slopes overlay (from rock_gap.tif, gap==5)
        if self._show_rock_gap and self.rock_gap_mask is not None:
            rock_rgba = np.zeros((h, w, 4), dtype=np.uint8)
            rock_rgba[self.rock_gap_mask] = [255, 170, 40, 150]  # warm orange tint
            self._rock_item.setPixmap(_ndarray_to_pixmap_rgba(rock_rgba))
            self._rock_item.setVisible(True)
        else:
            self._rock_item.setVisible(False)

        # Lithology layer
        lith_rgb = _lith_to_rgb(self.lith)
        alpha = np.where(self.lith > 0, 220, 0).astype(np.uint8)
        lith_rgba = np.dstack([lith_rgb, alpha])
        self._lith_item.setPixmap(_ndarray_to_pixmap_rgba(lith_rgba))

        # True-ocean @ Y63 overlay
        if self._show_ocean and self.ocean_mask is not None:
            ocean_rgba = np.zeros((h, w, 4), dtype=np.uint8)
            ocean_rgba[self.ocean_mask] = [30, 80, 160, 170]
            self._ocean_item.setPixmap(_ndarray_to_pixmap_rgba(ocean_rgba))
            self._ocean_item.setVisible(True)
        else:
            self._ocean_item.setVisible(False)

    def _set_tool(self, tool):
        self.canvas.set_tool(tool)
        self.btn_brush.setChecked(tool == "brush")
        self.btn_fill.setChecked(tool == "fill")
        self.btn_pick.setChecked(tool == "eyedropper")

    def _on_paint(self, cx: int, cy: int):
        if self.lith is None: return
        if not self._stroke_dirty:
            self._push_undo(); self._stroke_dirty = True
        r = self._brush_size
        mask, x0, y0, x1, y1 = _make_brush_mask(
            cx, cy, r, self._brush_shape, self._brush_angle,
            DISPLAY_SIZE, DISPLAY_SIZE,
        )
        # Ocean/land clamp
        if self._ocean_clamp != "off" and self.ocean_mask is not None:
            ocean_slab = self.ocean_mask[y0:y1, x0:x1]
            if self._ocean_clamp == "land":
                mask = mask & ~ocean_slab
            elif self._ocean_clamp == "ocean":
                mask = mask & ocean_slab
        # Elevation-band clamp
        if self._clamp_elev_enabled and self.height_raw is not None:
            lo, hi, _, _ = ELEVATION_BANDS[self._clamp_elev_band]
            hr = self.height_raw[y0:y1, x0:x1]
            mask = mask & (hr >= lo) & (hr < hi)
        # Scatter / probability — per-pixel dither inside the shape
        if self._brush_scatter < 100 and mask.any():
            rng = np.random.default_rng()
            coin = rng.random(mask.shape, dtype=np.float32)
            mask = mask & (coin < (self._brush_scatter / 100.0))
        self.lith[y0:y1, x0:x1][mask] = self._current_gid
        self._refresh()

    def _on_clamp_elev_toggled(self, enabled):
        self._clamp_elev_enabled = enabled
        self.clamp_elev_combo.setEnabled(enabled)

    def _on_fill(self, cx: int, cy: int):
        if self.lith is None: return
        self._push_undo(); self._stroke_dirty = False
        target = int(self.lith[cy, cx])
        fill = self._current_gid
        if target == fill:
            return
        # Scanline flood fill (no land-mask constraint — lithology doesn't care)
        data = self.lith
        h, w = data.shape
        visited = np.zeros((h, w), dtype=bool)
        stack = [(cx, cy)]
        while stack:
            x, y = stack.pop()
            if x < 0 or x >= w or y < 0 or y >= h: continue
            if visited[y, x] or data[y, x] != target: continue
            lx = x
            while lx >= 0 and data[y, lx] == target and not visited[y, lx]: lx -= 1
            lx += 1
            rx = x
            while rx < w and data[y, rx] == target and not visited[y, rx]: rx += 1
            rx -= 1
            data[y, lx:rx+1] = fill
            visited[y, lx:rx+1] = True
            for nx in range(lx, rx + 1):
                if y - 1 >= 0 and not visited[y-1, nx] and data[y-1, nx] == target:
                    stack.append((nx, y - 1))
                if y + 1 < h and not visited[y+1, nx] and data[y+1, nx] == target:
                    stack.append((nx, y + 1))
        self._refresh()

    def _on_pick(self, cx: int, cy: int):
        if self.lith is None: return
        val = int(self.lith[cy, cx])
        if val in LITHOLOGY_GROUPS:
            self._current_gid = val
            idx = list(LITHOLOGY_GROUPS.keys()).index(val)
            self.gid_combo.setCurrentIndex(idx)

    def _on_stroke_ended(self):
        self._stroke_dirty = False

    def _on_cursor(self, cx, cy):
        if self.lith is None: return
        val = int(self.lith[cy, cx])
        name = LITHOLOGY_GROUPS.get(val, ("?", None))[0]
        biome_name = "-"
        if self.biome_backdrop is not None:
            bcode = int(self.biome_backdrop[cy, cx])
            biome_name = BIOME_ZONES.get(bcode, ("?", None))[0]
        ow, oh = self.orig_size
        wx = int(cx * ow / DISPLAY_SIZE)
        wy = int(cy * oh / DISPLAY_SIZE)
        mw = self.window()
        if mw and hasattr(mw, "status"):
            mw.status.showMessage(
                f"cursor: canvas ({cx},{cy})  orig ({wx},{wy})  "
                f"lith={val}({name})  biome={biome_name}"
            )

    def _on_gid_changed(self, idx):
        self._current_gid = self.gid_combo.itemData(idx)
        self._update_gid_swatch()

    def _prefill_from_zone_to_group(self):
        if self.biome_backdrop is None:
            QMessageBox.warning(self, "Prefill", "Biome backdrop not loaded.")
            return
        with open(LITHOLOGY_CONFIG_PATH) as f:
            cfg = json.load(f)
        groups = cfg["lithology"]["groups"]
        zone_to_group = cfg["lithology"]["zone_to_group"]
        # Build zone_code → group_id LUT
        code_to_gid = np.zeros(256, dtype=np.uint8)
        for code, name in OVERRIDE_BIOME_MAP.items():
            if name in zone_to_group:
                g_name = zone_to_group[name]
                if g_name in groups:
                    code_to_gid[code] = int(groups[g_name]["id"])
        self._push_undo()
        self.lith = code_to_gid[self.biome_backdrop]
        self._refresh()
        QMessageBox.information(
            self, "Prefill done",
            f"Lithology buffer prefilled from zone_to_group derivation.\n"
            f"Non-zero pixels: {int((self.lith > 0).sum()):,}"
        )

    def _push_undo(self):
        if self.lith is not None:
            self.undo.append(self.lith.copy()); self.redo.clear()
            self._update_undo_buttons()

    def _do_undo(self):
        if not self.undo or self.lith is None: return
        self.redo.append(self.lith.copy()); self.lith = self.undo.pop()
        self._refresh(); self._update_undo_buttons()

    def _do_redo(self):
        if not self.redo or self.lith is None: return
        self.undo.append(self.lith.copy()); self.lith = self.redo.pop()
        self._refresh(); self._update_undo_buttons()

    def _update_undo_buttons(self):
        self.btn_undo.setEnabled(bool(self.undo))
        self.btn_redo.setEnabled(bool(self.redo))

    def run_preflight(self):
        if self.lith is None: return
        lines: list[str] = []
        lines.append(f"Edit buffer shape: {self.lith.shape}  dtype={self.lith.dtype}")
        lines.append(f"Save target size: {self.orig_size}")
        lines.append("")
        issues = _validate_lithology_array(self.lith, (DISPLAY_SIZE, DISPLAY_SIZE))
        if issues:
            lines.append("⚠ Lithology validation issues:")
            for i in issues: lines.append(f"  - {i}")
        else:
            lines.append("✓ All lithology IDs valid.")
        vals, counts = np.unique(self.lith, return_counts=True)
        total = int(self.lith.size)
        lines.append("")
        lines.append("Group distribution:")
        for v, c in zip(vals, counts):
            name = LITHOLOGY_GROUPS.get(int(v), ("?", None))[0]
            lines.append(f"  {int(v)} ({name}): {int(c):,} px ({c/total*100:.2f}%)")
        lines.append("")
        resized, rt = _resize_nearest_check(self.lith, self.orig_size)
        if rt:
            lines.append("⚠ Round-trip resize drift:")
            for i in rt: lines.append(f"  - {i}")
        else:
            lines.append(f"✓ Round-trip NEAREST resize to {self.orig_size} clean.")
        _show_report(self.window(), "Lithology Preflight", "\n".join(lines))

    def save(self):
        if self.lith is None: return
        issues = _validate_lithology_array(self.lith, (DISPLAY_SIZE, DISPLAY_SIZE))
        if issues:
            reply = QMessageBox.warning(
                self, "Validation",
                "Issues:\n\n" + "\n".join(issues) + "\n\nAbort save?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                return
        resized, rt = _resize_nearest_check(self.lith, self.orig_size)
        if rt:
            reply = QMessageBox.critical(
                self, "Round-trip", "\n".join(rt) + "\n\nAbort save?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes:
                return
        save_path = str(self.lith_path)
        save_str, _ = QFileDialog.getSaveFileName(
            self, "Save lithology_region.png", save_path, "PNG Images (*.png)")
        if not save_str: return
        save_p = Path(save_str)
        if save_p.exists():
            try: shutil.copy2(save_p, save_p.with_suffix(save_p.suffix + ".bak"))
            except Exception: pass
        try:
            Image.fromarray(resized, mode="L").save(save_p)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self.lith_path = save_p
        nonzero = int((resized > 0).sum())
        QMessageBox.information(
            self, "Saved",
            f"{save_p}\n\nSize: {self.orig_size}\n"
            f"Non-zero (painted) pixels: {nonzero:,}\n\n"
            f"Next: run `py tools/build_lithology.py` to rebuild lithology.tif."
        )


# =============================================================================
# Hydrology Paint tab — oases, dune lakes, custom riparian, fertile valleys
# =============================================================================

class HydrologyPainterTab(QWidget):
    """Paints WATER FEATURES onto masks/hydro_region.png.  This layer
    influences the hydrology pipeline ONLY — it never repaints biomes.

    Categories (see HYDRO_REGIONS):
      0 = pass-through
      1 = lake / oasis  (force a water body)
      2 = river / stream  (force a flowing channel)
      3 = river bank  (moisture fringe beside rivers, no biome change)
      4 = dry channel / wadi  (carved channel, no water)

    Pipeline wiring is future work — the tab currently just produces the
    editable layer.  When consumed, the pipeline will:
      - Force lake placement at id=1
      - Force river carving along id=2 paths
      - Boost local moisture at id=3 (no biome swap)
      - Carve dry channels at id=4 without water fill
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hyd: np.ndarray | None = None
        self.biome_backdrop: np.ndarray | None = None
        # Raw inputs needed to recompute the backdrop on toggle changes
        self.hydro_centerline_wp: np.ndarray | None = None  # from WP findPath / "river script 1.7"
        self.river_legacy: np.ndarray | None = None         # from river.tif (original precompute)
        self.hydro_lake: np.ndarray | None = None           # raw basin extent (uint16 lake ID)
        self.hydro_lake_wl: np.ndarray | None = None        # float water level
        self.height_raw: np.ndarray | None = None           # uint16 terrain height
        self.flow_tif: np.ndarray | None = None             # float Gaea flow accumulation
        self.orig_size: tuple[int, int] = (DISPLAY_SIZE, DISPLAY_SIZE)
        self.hyd_path: Path = DEFAULT_HYDRO_REGION_PATH
        self.undo: deque = deque(maxlen=UNDO_LEVELS)
        self.redo: deque = deque(maxlen=UNDO_LEVELS)
        self._stroke_dirty = False
        self._last_paint_pt: tuple[int, int] | None = None
        # Cached pre-baked pixmaps (set by _rebuild_backdrop) so paint
        # events don't rebuild the static backdrops every frame.
        self._cached_bg_pixmap = None
        self._cached_biome_pixmap = None
        # Hardcoded: paint always emits id=2 (river). All other categories
        # (lake/bank/wadi) were legacy and removed from the UI per
        # S80 v29 — Hydrology painter is RIVER-ONLY now.
        self._current_id = 2
        self._brush_size = 8
        self._show_biome_backdrop = True
        # Independent overlay toggles, each rendered in its own colour
        self._show_ocean = True       # height <= raw sea-level → deep blue
        self._show_precompute = False
        self._show_wp_script = True
        self._show_real_lakes = True
        self._show_flow = False        # Gaea flow.tif accumulation, log-scaled
        self._backdrop_opacity = 0.35
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        root = QHBoxLayout(self); root.setContentsMargins(4, 4, 4, 4); root.setSpacing(4)
        self.canvas = BaseCanvas()
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._bg_item     = QGraphicsPixmapItem(); self._bg_item.setZValue(0)
        self._hydbg_item  = QGraphicsPixmapItem(); self._hydbg_item.setZValue(1)
        self._paint_item  = QGraphicsPixmapItem(); self._paint_item.setZValue(2)
        for it in (self._bg_item, self._hydbg_item, self._paint_item):
            self.canvas.scene.addItem(it)
        controls = self._build_controls()
        scroll = QScrollArea(); scroll.setWidget(controls); scroll.setWidgetResizable(True)
        scroll.setFixedWidth(310)
        scroll.setStyleSheet(
            "QScrollArea {background:#13131f;border:none;}"
            "QScrollBar:vertical {background:#0a0a12;width:10px;}"
            "QScrollBar::handle:vertical {background:#3a3a5a;border-radius:4px;}"
        )
        root.addWidget(self.canvas, stretch=1)
        root.addWidget(scroll)

    def _build_controls(self) -> QWidget:
        ctrl = QWidget(); ctrl.setFixedWidth(290)
        ctrl.setStyleSheet("background:#13131f;color:#d0d0e0;")
        lay = QVBoxLayout(ctrl); lay.setContentsMargins(8, 8, 8, 8); lay.setSpacing(6)

        tg = QGroupBox("Tool"); tg.setStyleSheet(BiomePainterTab._group_style())
        tgl = QHBoxLayout(tg)
        self.btn_brush  = QPushButton("🖌 Brush");  self.btn_brush.setCheckable(True); self.btn_brush.setChecked(True)
        self.btn_eraser = QPushButton("🧽 Eraser"); self.btn_eraser.setCheckable(True)
        self.btn_fill   = QPushButton("🪣 Fill");   self.btn_fill.setCheckable(True)
        for b in (self.btn_brush, self.btn_eraser, self.btn_fill):
            b.setStyleSheet(BiomePainterTab._tool_btn_style()); tgl.addWidget(b)
        # Pick (eyedropper) removed in v29 — paint mode is fixed id=2,
        # nothing meaningful to pick.
        lay.addWidget(tg)

        # Single paint mode — RIVER ONLY (id=2). No category combo;
        # other ids (lake/bank/wadi) were legacy noise.
        info_lbl = QLabel("🖌  Painting: <b style='color:#FFB400;'>RIVER</b>")
        info_lbl.setStyleSheet("color:#d0d0e0;font-size:12px;padding:6px;"
                                "background:#0a0a12;border-radius:4px;")
        lay.addWidget(info_lbl)

        # Brush size
        sg = QGroupBox("Brush Size"); sg.setStyleSheet(BiomePainterTab._group_style())
        sgl = QHBoxLayout(sg)
        self.brush_slider = QSlider(Qt.Orientation.Horizontal)
        self.brush_slider.setRange(1, 100); self.brush_slider.setValue(self._brush_size)
        self.brush_label = QLabel(f"{self._brush_size}px"); self.brush_label.setFixedWidth(38)
        sgl.addWidget(self.brush_slider); sgl.addWidget(self.brush_label)
        lay.addWidget(sg)

        # Backdrop — three independent overlay layers, each its own colour
        bg_ = QGroupBox("Overlays"); bg_.setStyleSheet(BiomePainterTab._group_style())
        bgl = QVBoxLayout(bg_)
        self.chk_biome_bg = QCheckBox("Biome zones (dimmed)")
        self.chk_biome_bg.setChecked(True)
        self.chk_biome_bg.setStyleSheet("color:#d0d0e0;font-size:11px;")
        bgl.addWidget(self.chk_biome_bg)

        # Independent toggles, each in their own distinct color.
        self.chk_ocean = QCheckBox("True ocean (height ≤ Y 63)")
        self.chk_ocean.setChecked(True)
        self.chk_ocean.setStyleSheet(
            "color:#3070C0;font-size:11px;font-weight:bold;")
        bgl.addWidget(self.chk_ocean)

        self.chk_precompute = QCheckBox("Precompute rivers (river.tif)")
        self.chk_precompute.setChecked(False)
        self.chk_precompute.setStyleSheet(
            "color:#FF8060;font-size:11px;font-weight:bold;")
        bgl.addWidget(self.chk_precompute)

        self.chk_wp_script = QCheckBox("WP script 1.7 (hydro_centerline.tif)")
        self.chk_wp_script.setChecked(True)
        self.chk_wp_script.setStyleSheet(
            "color:#60A0FF;font-size:11px;font-weight:bold;")
        bgl.addWidget(self.chk_wp_script)

        self.chk_real_lakes = QCheckBox("Real lakes (terrain-intersection)")
        self.chk_real_lakes.setChecked(True)
        self.chk_real_lakes.setStyleSheet(
            "color:#40D8E0;font-size:11px;font-weight:bold;")
        bgl.addWidget(self.chk_real_lakes)

        self.chk_flow = QCheckBox("Gaea flow accumulation (flow.tif)")
        self.chk_flow.setChecked(False)
        self.chk_flow.setStyleSheet(
            "color:#80E060;font-size:11px;font-weight:bold;")
        bgl.addWidget(self.chk_flow)

        op_row = QHBoxLayout(); op_row.addWidget(QLabel("Backdrop opacity:"))
        self.op_slider = QSlider(Qt.Orientation.Horizontal)
        self.op_slider.setRange(0, 100); self.op_slider.setValue(int(self._backdrop_opacity * 100))
        op_row.addWidget(self.op_slider); bgl.addLayout(op_row)
        lay.addWidget(bg_)

        lay.addStretch()

        bt_row = QHBoxLayout()
        self.btn_undo = QPushButton("↩ Undo"); self.btn_redo = QPushButton("↪ Redo")
        bt_row.addWidget(self.btn_undo); bt_row.addWidget(self.btn_redo)
        lay.addLayout(bt_row)

        self.btn_clear = QPushButton("🗑 Clear all painting")
        self.btn_clear.setStyleSheet("""
            QPushButton {background:#7a2a2a;color:white;padding:6px;border-radius:4px;}
            QPushButton:hover {background:#9a3a3a;}
        """)
        lay.addWidget(self.btn_clear)

        self.btn_preflight = QPushButton("✓ Preflight Check")
        self.btn_preflight.setStyleSheet("""
            QPushButton {background:#4a4a6e;color:white;padding:6px;border-radius:4px;}
            QPushButton:hover {background:#5a5a8e;}
        """)
        lay.addWidget(self.btn_preflight)

        self.btn_save = QPushButton("💾 Save masks/hydro_region.png")
        self.btn_save.setStyleSheet("""
            QPushButton {background:#1a6b3a;color:white;font-weight:bold;padding:8px;border-radius:4px;}
            QPushButton:hover {background:#2a9b5a;}
            QPushButton:disabled {background:#333;color:#666;}
        """)
        self.btn_save.setEnabled(False); lay.addWidget(self.btn_save)

        return ctrl

    # _update_cat_swatch / _on_cat_changed removed — paint mode is fixed
    # to id=2 (river); no category selector in v29.

    def _connect_signals(self):
        self.canvas.paint_applied.connect(self._on_paint)
        self.canvas.fill_applied.connect(self._on_fill)
        self.canvas.pick_applied.connect(self._on_pick)
        self.canvas.cursor_moved.connect(self._on_cursor)
        self.canvas.stroke_ended.connect(self._on_stroke_ended)
        self.btn_brush.clicked.connect(lambda: self._set_tool("brush"))
        self.btn_eraser.clicked.connect(lambda: self._set_tool("eraser"))
        self.btn_fill.clicked.connect(lambda: self._set_tool("fill"))
        self.brush_slider.valueChanged.connect(
            lambda v: (setattr(self, "_brush_size", v), self.brush_label.setText(f"{v}px")))
        self.chk_biome_bg.toggled.connect(
            lambda v: (setattr(self, "_show_biome_backdrop", v), self._refresh()))
        self.chk_ocean.toggled.connect(
            lambda v: (setattr(self, "_show_ocean", v),
                       self._rebuild_backdrop(), self._refresh()))
        self.chk_precompute.toggled.connect(
            lambda v: (setattr(self, "_show_precompute", v),
                       self._rebuild_backdrop(), self._refresh()))
        self.chk_wp_script.toggled.connect(
            lambda v: (setattr(self, "_show_wp_script", v),
                       self._rebuild_backdrop(), self._refresh()))
        self.chk_real_lakes.toggled.connect(
            lambda v: (setattr(self, "_show_real_lakes", v),
                       self._rebuild_backdrop(), self._refresh()))
        self.chk_flow.toggled.connect(
            lambda v: (setattr(self, "_show_flow", v),
                       self._rebuild_backdrop(), self._refresh()))
        self.op_slider.valueChanged.connect(
            lambda v: (setattr(self, "_backdrop_opacity", v / 100.0), self._refresh()))
        self.btn_undo.clicked.connect(self._do_undo)
        self.btn_redo.clicked.connect(self._do_redo)
        self.btn_clear.clicked.connect(self._do_clear_all)
        self.btn_preflight.clicked.connect(self.run_preflight)
        self.btn_save.clicked.connect(self.save)

        # Hotkeys: B = brush, E = eraser. WindowShortcut so they fire
        # whenever the studio window is focused, including when canvas
        # has focus.
        self._sc_brush = QShortcut(QKeySequence("B"), self)
        self._sc_brush.activated.connect(lambda: self._set_tool("brush"))
        self._sc_eraser = QShortcut(QKeySequence("E"), self)
        self._sc_eraser.activated.connect(lambda: self._set_tool("eraser"))

    def load_data(self, biome_backdrop: np.ndarray, orig_size: tuple[int, int],
                  hydro_centerline: np.ndarray | None = None,
                  hydro_lake: np.ndarray | None = None,
                  hydro_lake_wl: np.ndarray | None = None,
                  height_raw: np.ndarray | None = None,
                  river_legacy: np.ndarray | None = None,
                  flow_tif: np.ndarray | None = None):
        self.biome_backdrop = biome_backdrop.copy()
        self.orig_size = orig_size
        # Stash raw inputs so we can rebuild the backdrop on toggle changes
        self.hydro_centerline_wp = hydro_centerline
        self.river_legacy = river_legacy
        self.hydro_lake = hydro_lake
        self.hydro_lake_wl = hydro_lake_wl
        self.height_raw = height_raw
        self.flow_tif = flow_tif
        self._rebuild_backdrop()
        # Load or create paint buffer
        if self.hyd_path.exists():
            try:
                img = Image.open(self.hyd_path).convert("L")
                img_small = img.resize((DISPLAY_SIZE, DISPLAY_SIZE), Image.NEAREST)
                self.hyd = np.array(img_small, dtype=np.uint8)
                self.orig_size = img.size
                # Strip invalid ids
                mask = np.zeros(self.hyd.shape, dtype=bool)
                for v in VALID_HYDRO_IDS:
                    mask |= (self.hyd == v)
                self.hyd = np.where(mask, self.hyd, 0).astype(np.uint8)
            except Exception as e:
                print(f"[hydro tab] load failed: {e}")
                self.hyd = np.zeros((DISPLAY_SIZE, DISPLAY_SIZE), dtype=np.uint8)
        else:
            self.hyd = np.zeros((DISPLAY_SIZE, DISPLAY_SIZE), dtype=np.uint8)
        self.undo.clear(); self.redo.clear()
        self._update_undo_buttons()
        self.btn_save.setEnabled(True)
        self.canvas.fit_canvas(DISPLAY_SIZE, DISPLAY_SIZE)
        self._refresh()

    def _rebuild_backdrop(self):
        """Build the overlays backdrop from the independent toggles.
        Each layer renders in its own distinct colour. Caches the result
        as a QPixmap so paint events don't rebuild."""
        h = DISPLAY_SIZE
        backdrop = np.zeros((h, h, 4), dtype=np.uint8)

        # ── 0. True ocean (base layer) — DEEP BLUE.
        # height <= raw 17050 (= MC Y 63) is "below sea level".
        # Painted first so other overlays render on top of it. Brush
        # has no land/ocean clamp in this tab, so rivers paint straight
        # across into ocean (no seam at the coast).
        if self._show_ocean and self.height_raw is not None:
            ocean = self.height_raw <= 17050
            backdrop[ocean] = [ 18,  56, 128, 220]

        # ── 0.5. Gaea flow accumulation (flow.tif) — GREEN, log-tiered.
        # Continuous-gradient field: log-scale and threshold into three
        # tiers so main trunks pop and tributaries fade naturally.
        if self._show_flow and self.flow_tif is not None:
            f = self.flow_tif
            fmax = float(f.max())
            if fmax > 0:
                with np.errstate(invalid="ignore"):
                    intensity = np.log1p(f) / np.log1p(fmax)
                t1 = intensity >= 0.45  # tributaries
                t2 = intensity >= 0.65  # rivers
                t3 = intensity >= 0.85  # main trunks
                backdrop[t1] = [128, 224,  96, 130]
                backdrop[t2] = [128, 224,  96, 190]
                backdrop[t3] = [160, 255, 120, 240]

        # ── 1. Precompute rivers (river.tif) — ORANGE-RED ──
        if self._show_precompute and self.river_legacy is not None:
            mask = self.river_legacy > 0
            backdrop[mask] = [255, 128,  64, 235]

        # ── 2. WP script 1.7 (hydro_centerline.tif) — VIVID BLUE ──
        if self._show_wp_script and self.hydro_centerline_wp is not None:
            mask = self.hydro_centerline_wp > 0
            backdrop[mask] = [ 80, 160, 255, 235]

        # ── 3. Real lakes (terrain-intersection) — CYAN ──
        if (self._show_real_lakes
                and self.hydro_lake is not None
                and self.hydro_lake_wl is not None
                and self.height_raw is not None):
            basin = self.hydro_lake > 0
            wl = self.hydro_lake_wl.astype(np.float32)
            ht = self.height_raw.astype(np.float32)
            if wl.max() <= 1.5:
                wl = wl * 65535.0
            underwater = basin & (ht < wl)
            backdrop[underwater] = [ 64, 216, 224, 215]

        self.hydro_backdrop = backdrop
        # Pre-bake QPixmap so paint events don't rebuild (perf).
        self._cached_bg_pixmap = _ndarray_to_pixmap_rgba(backdrop)
        # Pre-bake the dimmed biome backdrop too — also static between
        # paint events.
        if self.biome_backdrop is not None:
            biome_rgb = _zone_to_rgb(self.biome_backdrop)
            op = self._backdrop_opacity
            biome_rgb = (biome_rgb.astype(np.float32) * op).astype(np.uint8)
            self._cached_biome_pixmap = _ndarray_to_pixmap_rgb(biome_rgb)
        else:
            self._cached_biome_pixmap = None

    def _refresh(self):
        """Full refresh: rebuilds biome + overlays + paint pixmaps.
        Use _refresh_paint_only() during active painting for speed."""
        if self.hyd is None: return
        h = w = DISPLAY_SIZE
        # Biome backdrop (cached pixmap from _rebuild_backdrop)
        if self._show_biome_backdrop and self._cached_biome_pixmap is not None:
            self._bg_item.setPixmap(self._cached_biome_pixmap)
        else:
            blank = np.zeros((h, w, 3), dtype=np.uint8)
            self._bg_item.setPixmap(_ndarray_to_pixmap_rgb(blank))
        # Overlays backdrop (cached)
        if self._cached_bg_pixmap is not None:
            self._hydbg_item.setPixmap(self._cached_bg_pixmap)
            self._hydbg_item.setVisible(True)
        else:
            self._hydbg_item.setVisible(False)
        # Paint layer
        self._refresh_paint_only()

    def _refresh_paint_only(self):
        """Fast-path: rebuild ONLY the paint layer pixmap. Used during
        active brush dragging so we don't redo the biome/overlays
        backdrops (which haven't changed)."""
        if self.hyd is None: return
        h = w = DISPLAY_SIZE
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        m = self.hyd == 2
        if m.any():
            rgba[m] = [255, 220,  40, 250]
        self._paint_item.setPixmap(_ndarray_to_pixmap_rgba(rgba))

    def _set_tool(self, tool):
        # "eraser" is a virtual tool: canvas still uses brush/fill, but
        # paint id is 0 (pass-through) instead of 2 (river).
        if tool == "eraser":
            self.canvas.set_tool("brush")
            self._current_id = 0
        elif tool == "brush":
            self.canvas.set_tool("brush")
            self._current_id = 2
        elif tool == "fill":
            # Fill keeps the LAST chosen paint id — if eraser was active
            # before, fill erases; if brush, fill fills with river.
            self.canvas.set_tool("fill")
        else:
            self.canvas.set_tool(tool)
        self.btn_brush.setChecked(tool == "brush")
        self.btn_eraser.setChecked(tool == "eraser")
        self.btn_fill.setChecked(tool == "fill")

    def _stamp_disc(self, cx: int, cy: int, r: int):
        """Stamp a disc of radius r at (cx, cy). r==1 = single pixel."""
        if r == 1:
            if 0 <= cx < DISPLAY_SIZE and 0 <= cy < DISPLAY_SIZE:
                self.hyd[cy, cx] = self._current_id
            return
        y0 = max(0, cy - r); y1 = min(DISPLAY_SIZE, cy + r + 1)
        x0 = max(0, cx - r); x1 = min(DISPLAY_SIZE, cx + r + 1)
        if y1 <= y0 or x1 <= x0:
            return
        yy, xx = np.ogrid[y0:y1, x0:x1]
        mask = (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r
        self.hyd[y0:y1, x0:x1][mask] = self._current_id

    def _on_paint(self, cx: int, cy: int):
        if self.hyd is None: return
        if not self._stroke_dirty:
            self._push_undo(); self._stroke_dirty = True
        r = self._brush_size

        # Line interpolation: if there's a previous paint point, stamp
        # discs at every cell along the Bresenham line so a fast drag
        # produces a continuous stroke rather than discrete dots.
        last = getattr(self, "_last_paint_pt", None)
        if last is not None:
            x0, y0 = last
            dx = abs(cx - x0); dy = abs(cy - y0)
            steps = max(dx, dy)
            if steps > 0:
                # Sample stamps at most every (r) pixels for big brushes
                # — for r==1 we sample every pixel for crispness.
                stride = 1 if r <= 2 else max(1, r // 2)
                for s in range(stride, steps, stride):
                    px = int(x0 + (cx - x0) * s / steps)
                    py = int(y0 + (cy - y0) * s / steps)
                    self._stamp_disc(px, py, r)
        # Always stamp the current point as the final disc
        self._stamp_disc(cx, cy, r)
        self._last_paint_pt = (cx, cy)
        # Fast paint-only refresh — backdrops are cached pixmaps.
        self._refresh_paint_only()

    def _on_fill(self, cx: int, cy: int):
        """Channel-aware bucket fill. Click on a visible WP script (blue)
        or precompute (orange) river cell — the entire CONNECTED CHANNEL
        of that overlay gets painted with the current paint id (yellow
        when brush, 0 when eraser).

        If the click is NOT on either visible river overlay, the fill
        is a no-op (status message). This avoids the old behaviour of
        flood-filling the entire empty world."""
        if self.hyd is None: return
        if not (0 <= cx < DISPLAY_SIZE and 0 <= cy < DISPLAY_SIZE):
            return

        # Identify which backdrop layer the click landed on.
        # Priority: WP script > Precompute (top-rendered first under
        # _rebuild_backdrop). If both are visible AND a cell has both,
        # the WP-script channel wins.
        target_mask = None
        src_name = None
        if (self._show_wp_script and self.hydro_centerline_wp is not None
                and self.hydro_centerline_wp[cy, cx] > 0):
            target_mask = self.hydro_centerline_wp > 0
            src_name = "WP script 1.7"
        elif (self._show_precompute and self.river_legacy is not None
                and self.river_legacy[cy, cx] > 0):
            target_mask = self.river_legacy > 0
            src_name = "precompute"

        if target_mask is None:
            mw = self.window()
            if mw and hasattr(mw, "status"):
                mw.status.showMessage(
                    "Bucket fill: click directly on a visible "
                    "WP-script (blue) or precompute (orange) river cell"
                )
            return

        # Find the connected component under the cursor and paint it.
        from scipy.ndimage import label as _label
        lbl, _n = _label(target_mask)
        cid = int(lbl[cy, cx])
        if cid == 0:
            return
        component = lbl == cid
        cell_count = int(component.sum())

        self._push_undo(); self._stroke_dirty = False
        self.hyd[component] = self._current_id

        mw = self.window()
        if mw and hasattr(mw, "status"):
            verb = "erased" if self._current_id == 0 else "painted"
            mw.status.showMessage(
                f"Bucket fill: {verb} {cell_count:,} cells of {src_name} channel"
            )
        self._refresh()

    def _on_pick(self, cx, cy):
        # Eyedropper is a no-op in v29 — paint mode is fixed to id=2
        # (river). Kept method to satisfy canvas signal connection.
        return

    def _on_stroke_ended(self):
        self._stroke_dirty = False
        self._last_paint_pt = None
        # Run a full refresh on stroke end (in case backdrops drifted
        # — cheap because backdrops are cached, paint layer rebuilt).
        self._refresh()

    def _on_cursor(self, cx, cy):
        if self.hyd is None: return
        val = int(self.hyd[cy, cx])
        name = HYDRO_REGIONS.get(val, ("?", None))[0]
        biome_name = "-"
        if self.biome_backdrop is not None:
            bcode = int(self.biome_backdrop[cy, cx])
            biome_name = BIOME_ZONES.get(bcode, ("?", None))[0]
        ow, oh = self.orig_size
        wx = int(cx * ow / DISPLAY_SIZE); wy = int(cy * oh / DISPLAY_SIZE)
        mw = self.window()
        if mw and hasattr(mw, "status"):
            mw.status.showMessage(
                f"cursor: canvas ({cx},{cy})  orig ({wx},{wy})  "
                f"hydro={val}({name})  biome={biome_name}"
            )

    def _push_undo(self):
        if self.hyd is not None:
            self.undo.append(self.hyd.copy()); self.redo.clear(); self._update_undo_buttons()

    def _do_undo(self):
        if not self.undo or self.hyd is None: return
        self.redo.append(self.hyd.copy()); self.hyd = self.undo.pop()
        self._refresh(); self._update_undo_buttons()

    def _do_redo(self):
        if not self.redo or self.hyd is None: return
        self.undo.append(self.hyd.copy()); self.hyd = self.redo.pop()
        self._refresh(); self._update_undo_buttons()

    def _update_undo_buttons(self):
        self.btn_undo.setEnabled(bool(self.undo))
        self.btn_redo.setEnabled(bool(self.redo))

    def _do_clear_all(self):
        """Wipe all painting (in-buffer + the on-disk hydro_region.png)."""
        if self.hyd is None:
            return
        from PyQt6.QtWidgets import QMessageBox
        ans = QMessageBox.warning(
            self, "Clear all painting?",
            "This wipes ALL painted hydrology and overwrites "
            f"{self.hyd_path.name} with an empty mask. Cannot be undone.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._push_undo()
        self.hyd[:] = 0
        self._stroke_dirty = False
        # Wipe on-disk file too so a future Load gives a clean start.
        try:
            from PIL import Image as _PIL
            ow, oh = self.orig_size
            zero = np.zeros((oh, ow), dtype=np.uint8)
            _PIL.fromarray(zero, mode="L").save(self.hyd_path)
            mw = self.window()
            if mw and hasattr(mw, "status"):
                mw.status.showMessage(
                    f"Cleared {self.hyd_path.name} ({ow}x{oh}) — paint buffer reset"
                )
        except Exception as e:
            print(f"[hydro tab] clear failed to wipe disk: {e}")
        self._refresh()

    def run_preflight(self):
        if self.hyd is None: return
        lines = []
        lines.append(f"Edit buffer shape: {self.hyd.shape}  dtype={self.hyd.dtype}")
        lines.append(f"Save target size: {self.orig_size}")
        lines.append("")
        unique = set(np.unique(self.hyd).tolist())
        invalid = unique - VALID_HYDRO_IDS
        if invalid:
            lines.append(f"⚠ Invalid IDs: {sorted(invalid)}")
        else:
            lines.append("✓ All IDs valid.")
        vals, counts = np.unique(self.hyd, return_counts=True)
        total = int(self.hyd.size)
        lines.append("")
        lines.append("Category distribution:")
        for v, c in zip(vals, counts):
            nm = HYDRO_REGIONS.get(int(v), ("?", None))[0]
            lines.append(f"  {int(v)} ({nm}): {int(c):,} px ({c/total*100:.2f}%)")
        _, rt = _resize_nearest_check(self.hyd, self.orig_size)
        lines.append("")
        if rt: lines.append("⚠ " + "\n".join(rt))
        else: lines.append(f"✓ Round-trip NEAREST resize to {self.orig_size} clean.")
        _show_report(self.window(), "Hydrology Paint Preflight", "\n".join(lines))

    def save(self):
        if self.hyd is None: return
        unique = set(np.unique(self.hyd).tolist())
        if unique - VALID_HYDRO_IDS:
            reply = QMessageBox.warning(
                self, "Validation",
                f"Invalid IDs in buffer: {sorted(unique - VALID_HYDRO_IDS)}\nAbort?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes: return
        resized, rt = _resize_nearest_check(self.hyd, self.orig_size)
        if rt:
            reply = QMessageBox.critical(
                self, "Round-trip", "\n".join(rt) + "\n\nAbort?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes: return
        save_str, _ = QFileDialog.getSaveFileName(
            self, "Save hydro_region.png", str(self.hyd_path), "PNG Images (*.png)")
        if not save_str: return
        save_p = Path(save_str)
        if save_p.exists():
            try: shutil.copy2(save_p, save_p.with_suffix(save_p.suffix + ".bak"))
            except Exception: pass
        try:
            Image.fromarray(resized, mode="L").save(save_p)
        except Exception as e:
            QMessageBox.critical(self, "Save failed", str(e)); return
        self.hyd_path = save_p
        nonzero = int((resized > 0).sum())
        QMessageBox.information(
            self, "Saved",
            f"{save_p}\n\nSize: {self.orig_size}\nNon-zero: {nonzero:,}\n\n"
            f"(Pipeline wiring for this overlay is future work.)"
        )


# =============================================================================
# Helper dialogs
# =============================================================================

def _show_report(parent, title: str, text: str):
    dlg = QDialog(parent)
    dlg.setWindowTitle(title); dlg.resize(700, 500)
    lay = QVBoxLayout(dlg)
    te = QTextEdit(); te.setPlainText(text); te.setReadOnly(True)
    te.setStyleSheet("background:#0a0a12;color:#d0d0e0;font-family:Consolas,monospace;font-size:11px;")
    lay.addWidget(te)
    bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    bb.rejected.connect(dlg.reject); bb.accepted.connect(dlg.accept)
    lay.addWidget(bb)
    dlg.exec()


class _ProcessProgressDialog(QDialog):
    """Simple modal that runs a subprocess and streams stdout into a TextEdit."""
    def __init__(self, parent, title: str, intro: str):
        super().__init__(parent)
        self.setWindowTitle(title); self.resize(700, 450)
        lay = QVBoxLayout(self)
        self._lbl = QLabel(intro)
        self._lbl.setStyleSheet("color:#d0d0e0;")
        lay.addWidget(self._lbl)
        self._bar = QProgressBar(); self._bar.setRange(0, 0)  # indeterminate
        lay.addWidget(self._bar)
        self._te = QTextEdit(); self._te.setReadOnly(True)
        self._te.setStyleSheet("background:#0a0a12;color:#8fc98f;font-family:Consolas,monospace;font-size:11px;")
        lay.addWidget(self._te)
        self._bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self._bb.rejected.connect(self.reject); self._bb.accepted.connect(self.accept)
        self._bb.button(QDialogButtonBox.StandardButton.Close).setEnabled(False)
        lay.addWidget(self._bb)
        self._proc: QProcess | None = None

    def run(self, program: str, args: list[str]):
        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.finished.connect(self._on_finished)
        self._proc.start(program, args)
        self.exec()

    def _on_stdout(self):
        if self._proc is None: return
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._te.append(data.rstrip())

    def _on_finished(self, code, status):
        self._bar.setRange(0, 1); self._bar.setValue(1)
        self._te.append(f"\n---\nExit code: {code}")
        self._bb.button(QDialogButtonBox.StandardButton.Close).setEnabled(True)


# =============================================================================
# Main window
# =============================================================================

class OverrideStudioWindow(QMainWindow):
    def __init__(self, override_path: Path | None, height_path: Path | None,
                 flow_path: Path | None):
        super().__init__()
        self.setWindowTitle("Vandir — Override + Lithology Studio")
        self.setMinimumSize(1300, 800); self.resize(1600, 980)

        self.tabs = QTabWidget()
        self.tab_biome = BiomePainterTab()
        self.tab_lith  = LithologyPainterTab()
        self.tab_hydro = HydrologyPainterTab()
        self.tabs.addTab(self.tab_biome, "Biome Override")
        self.tabs.addTab(self.tab_lith, "Lithology Region")
        self.tabs.addTab(self.tab_hydro, "Hydrology Paint")
        self.setCentralWidget(self.tabs)

        self.status = QStatusBar(); self.setStatusBar(self.status)
        self.status.setStyleSheet("background:#0e0e1a;color:#8080a0;font-size:11px;")

        self._build_toolbar()
        self.setStyleSheet("QMainWindow {background:#0a0a12;}")

        # Ask for files if not passed.
        QTimer.singleShot(100, lambda: self._load_initial(override_path, height_path, flow_path))

    def _build_toolbar(self):
        tb = QToolBar("Main"); tb.setStyleSheet("QToolBar {background:#13131f;border:none;spacing:4px;}")
        self.addToolBar(tb)
        act_load = QAction("📂 Load", self); act_load.triggered.connect(self._load_dialog)
        tb.addAction(act_load)
        tb.addSeparator()
        act_b = QAction("B Brush", self); act_b.setShortcut("B")
        act_b.triggered.connect(lambda: self._active_tab_tool("brush"))
        tb.addAction(act_b)
        act_f = QAction("F Fill", self); act_f.setShortcut("F")
        act_f.triggered.connect(lambda: self._active_tab_tool("fill"))
        tb.addAction(act_f)
        act_i = QAction("I Pick", self); act_i.setShortcut("I")
        act_i.triggered.connect(lambda: self._active_tab_tool("eyedropper"))
        tb.addAction(act_i)
        act_r = QAction("R Region", self); act_r.setShortcut("R")
        act_r.triggered.connect(lambda: self._active_tab_tool("region_replace"))
        tb.addAction(act_r)
        tb.addSeparator()
        # Undo/redo — QKeySequence.StandardKey.Undo = Ctrl+Z on Win/Linux, Cmd+Z on Mac.
        # Redo maps to Ctrl+Y on Win, Ctrl+Shift+Z also wired for cross-platform.
        act_u = QAction("↩ Undo", self)
        act_u.setShortcuts([QKeySequence.StandardKey.Undo])
        act_u.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_u.triggered.connect(self._active_undo); tb.addAction(act_u)
        self.addAction(act_u)
        act_y = QAction("↪ Redo", self)
        act_y.setShortcuts([QKeySequence.StandardKey.Redo, QKeySequence("Ctrl+Shift+Z")])
        act_y.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_y.triggered.connect(self._active_redo); tb.addAction(act_y)
        self.addAction(act_y)
        tb.addSeparator()
        act_s = QAction("💾 Save", self); act_s.setShortcut("Ctrl+S")
        act_s.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_s.triggered.connect(self._active_save); tb.addAction(act_s)
        act_p = QAction("✓ Preflight", self)
        act_p.triggered.connect(self._active_preflight); tb.addAction(act_p)
        # Brush size hotkeys — [ and ] on the active tab's brush slider.
        act_bd = QAction("[ smaller", self); act_bd.setShortcut("[")
        act_bd.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_bd.triggered.connect(lambda: self._active_brush_step(-1))
        self.addAction(act_bd)
        act_bu = QAction("] larger", self); act_bu.setShortcut("]")
        act_bu.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_bu.triggered.connect(lambda: self._active_brush_step(+1))
        self.addAction(act_bu)
        # Eraser — set active tab's selected value to 0 (pass-through / No Override)
        act_e = QAction("E eraser", self); act_e.setShortcut("E")
        act_e.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
        act_e.triggered.connect(self._active_eraser)
        self.addAction(act_e)

    def _active_tab_tool(self, tool: str):
        w = self.tabs.currentWidget()
        if hasattr(w, "_set_tool"):
            w._set_tool(tool)

    def _active_undo(self):
        w = self.tabs.currentWidget()
        if hasattr(w, "_do_undo"): w._do_undo()

    def _active_redo(self):
        w = self.tabs.currentWidget()
        if hasattr(w, "_do_redo"): w._do_redo()

    def _active_save(self):
        w = self.tabs.currentWidget()
        if hasattr(w, "save"): w.save()

    def _active_preflight(self):
        w = self.tabs.currentWidget()
        if hasattr(w, "run_preflight"): w.run_preflight()

    def _active_brush_step(self, delta: int):
        w = self.tabs.currentWidget()
        if hasattr(w, "brush_slider"):
            s = w.brush_slider
            new = max(s.minimum(), min(s.maximum(), s.value() + delta))
            s.setValue(new)

    def _active_eraser(self):
        """Set the active tab's selected paint value to 0 (pass-through)."""
        w = self.tabs.currentWidget()
        # Biome tab uses zone_combo + _current_value
        if hasattr(w, "zone_combo"):
            idx = w.zone_combo.findData(0)
            if idx >= 0:
                w.zone_combo.setCurrentIndex(idx)
                return
        # Lithology tab uses gid_combo + _current_gid
        if hasattr(w, "gid_combo"):
            idx = w.gid_combo.findData(0)
            if idx >= 0:
                w.gid_combo.setCurrentIndex(idx)
                return
        # Hydrology tab in v29 has no category combo (river-only paint).
        # Eraser shortcut is a no-op there — the user can use Undo or
        # paint a 0 explicitly, but we don't auto-switch ids.
        if isinstance(w, HydrologyPainterTab):
            return

    def _load_initial(self, ov: Path | None, ht: Path | None, fl: Path | None):
        # Auto-load defaults.  Silent fallthrough to dialog only when the
        # override itself is missing.  Height/flow are optional; missing
        # height disables the alignment check but not the painter.
        if ov is None and DEFAULT_OVERRIDE_PATH.exists():
            ov = DEFAULT_OVERRIDE_PATH
        if ht is None and DEFAULT_HEIGHT_PATH.exists():
            ht = DEFAULT_HEIGHT_PATH
        if fl is None and DEFAULT_FLOW_PATH.exists():
            fl = DEFAULT_FLOW_PATH
        if ov is None:
            self.status.showMessage(
                f"override_final.png not found at {DEFAULT_OVERRIDE_PATH} — "
                f"use Load button to pick manually")
            self._load_dialog()
            return
        self.status.showMessage(
            f"Auto-loading: {ov.name}"
            + (f" + height" if ht else "")
            + (f" + flow" if fl else ""))
        self._do_load(ov, ht, fl)

    def _load_dialog(self):
        default = str(_PROJECT_ROOT)
        ov, _ = QFileDialog.getOpenFileName(
            self, "Load override PNG", default, "PNG (*.png);;All files (*)")
        if not ov: return
        ht, _ = QFileDialog.getOpenFileName(
            self, "Load height.tif (optional, cancel to skip)", default,
            "TIFF (*.tif *.tiff);;All files (*)")
        fl, _ = QFileDialog.getOpenFileName(
            self, "Load flow.tif (optional, cancel to skip)", default,
            "TIFF (*.tif *.tiff);;All files (*)")
        self._do_load(Path(ov), Path(ht) if ht else None, Path(fl) if fl else None)

    def _do_load(self, ov: Path, ht: Path | None, fl: Path | None):
        self.status.showMessage(f"Loading {ov.name}...")
        QApplication.processEvents()
        ok = self.tab_biome.load_data(ov, ht, fl)
        if ok and self.tab_biome.override is not None:
            # Share biome + hydro backdrops + ocean_mask with the other tabs.
            self.tab_lith.load_data(
                self.tab_biome.override, self.tab_biome.orig_size,
                ocean_mask=self.tab_biome.ocean_mask,
                height_raw=self.tab_biome.height_raw,
            )
            self.tab_hydro.load_data(
                self.tab_biome.override, self.tab_biome.orig_size,
                self.tab_biome.hydro_centerline, self.tab_biome.hydro_lake,
                hydro_lake_wl=getattr(self.tab_biome, "hydro_lake_wl", None),
                height_raw=self.tab_biome.height_raw,
                river_legacy=getattr(self.tab_biome, "river_legacy", None),
                flow_tif=getattr(self.tab_biome, "flow_tif", None),
            )
        self.status.showMessage(f"Loaded: {ov.name}")


# =============================================================================
# Entry point
# =============================================================================

def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--override", type=Path, default=None)
    p.add_argument("--height", type=Path, default=None)
    p.add_argument("--flow", type=Path, default=None)
    return p.parse_args()


def main():
    args = _parse_args()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor("#0e0e1a"))
    pal.setColor(QPalette.ColorRole.WindowText, QColor("#d0d0e0"))
    pal.setColor(QPalette.ColorRole.Base, QColor("#13131f"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#1a1a2e"))
    pal.setColor(QPalette.ColorRole.Text, QColor("#d0d0e0"))
    pal.setColor(QPalette.ColorRole.Button, QColor("#1e1e30"))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor("#d0d0e0"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#2d4a7a"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    app.setPalette(pal)
    win = OverrideStudioWindow(args.override, args.height, args.flow)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
