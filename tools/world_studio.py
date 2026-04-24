"""
world_studio.py — Vandir World Studio  v2.0
============================================
Single-pane-of-glass integrated tool per ARCHITECTURE_VISION.md Part II.

  Tool A — World Map        : zoomable 97×97 tile grid, LOD thumbnails,
                              layer toggles (Height / Biome), click to select
  Tool B — Live Config Panel: thresholds.json sliders with live save
  Tool C — Tile Inspector   : 4 render modes (Height/Biome/Slope/Surface),
                              fast preview before pipeline, hover detail
  Tool D — Biome Studio     : height×flow scatterplot, draggable thresholds
  Tool E — 3D Voxel Preview : WebGL2 VBO/VAO mesh, perspective + isometric,
                              Y-slice cross-section, cliff params
  Tool F — Structure Placer : placeholder
  Tool G — Annotations      : placeholder

Layout:
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Toolbar: [VANDIR WORLD STUDIO] [Height|Biome toggles] [tile coords] │
  ├───────────────────────┬─────────────────────────────────────────────┤
  │                       │ Tabs: [Inspect][3D][Config][Biome][Notes]   │
  │  WORLD MAP            │                                             │
  │  QGraphicsView        │  Tab content (right panel)                  │
  │  97×97 LOD grid       │                                             │
  │                       │                                             │
  ├───────────────────────┴─────────────────────────────────────────────┤
  │ Status: tile (tx,tz) | world coords | MCA file | hover info         │
  └─────────────────────────────────────────────────────────────────────┘

Launch:
    python tools/world_studio.py
    python tools/world_studio.py --tile-x 56 --tile-z 46
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
    from matplotlib.figure import Figure
    _MATPLOTLIB_OK = True
except ImportError:
    _MATPLOTLIB_OK = False

# ---------------------------------------------------------------------------
# Project root on sys.path
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QRectF, QPointF, QSize, QPoint, QRect,
)
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QPen, QColor, QBrush, QWheelEvent,
    QMouseEvent, QFont, QAction, QKeyEvent,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QTabWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox, QToolBar,
    QLabel, QPushButton, QSpinBox, QDoubleSpinBox, QSlider, QComboBox, QCheckBox,
    QSizePolicy, QStatusBar, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QGraphicsRectItem, QScrollArea, QMenu,
    QFrame, QStackedWidget, QDialog, QDialogButtonBox,
    QTextEdit, QListWidget, QListWidgetItem,
    QTableWidget, QTableWidgetItem,
)

# ---------------------------------------------------------------------------
# Constants — paths and grid geometry
# ---------------------------------------------------------------------------
PROJECT_ROOT = _PROJECT_ROOT
MASKS_DIR    = PROJECT_ROOT / "masks"
CONFIG_PATH  = PROJECT_ROOT / "config" / "thresholds.json"

GRID_N   = 97       # 97×97 tile grid
TILE_PX  = 512      # blocks per tile (512×512)
Y_MIN    = -64
Y_MAX    = 448
SEA_Y    = 63
Y_RANGE  = Y_MAX - Y_MIN   # 512

THUMB_LOD1_SZ  = 64     # px per tile thumbnail at LOD1 (zoomed in a little)
THUMB_LOD2_SZ  = 256    # px per tile thumbnail at LOD2 (zoomed in further)
LOD1_THRESHOLD = 9999.0 # LOD disabled — high-res overview is sharp enough
LOD2_THRESHOLD = 9999.0 # LOD disabled
THUMB_CACHE_MAX = 400   # LRU tile thumbnail cache size
OVERVIEW_RES   = 2048   # overview resolution — sharp single-image render

PYTHON_EXE       = r"C:\Users\nicho\AppData\Local\Python\pythoncore-3.14-64\python.exe"
HILLSHADE_CACHE  = PROJECT_ROOT / "output" / "hillshade_cache"
RENDER_MANIFEST  = PROJECT_ROOT / "output" / "render_manifest.json"

# ---------------------------------------------------------------------------
# Design language — color palette
# ---------------------------------------------------------------------------
C_BG     = "#0d0d14"    # deepest background
C_SURF   = "#16161f"    # surface / widget background
C_PANEL  = "#1d1d2a"    # panel / groupbox background
C_BORDER = "#2a2a3d"    # subtle borders
C_ACCENT = "#6d8ee8"    # slate blue — primary accent
C_SEL    = "#f5c842"    # amber — tile selection highlight
C_TEXT   = "#dde4f0"    # primary text
C_MUTED  = "#7a82a0"    # secondary / muted text
C_OK     = "#4ade80"    # green
C_WARN   = "#f59e0b"    # yellow/amber
C_ERR    = "#f87171"    # red

# ---------------------------------------------------------------------------
# QSS stylesheet — applied to the entire application
# ---------------------------------------------------------------------------
APP_QSS = f"""
QMainWindow, QDialog {{
    background: {C_BG};
}}
QWidget {{
    background: {C_SURF};
    color: {C_TEXT};
    font-family: 'Segoe UI', 'Arial', sans-serif;
    font-size: 12px;
}}
QSplitter::handle {{
    background: {C_BORDER};
    width: 2px; height: 2px;
}}
QTabWidget::pane {{
    border: 1px solid {C_BORDER};
    background: {C_PANEL};
    border-radius: 3px;
}}
QTabWidget::tab-bar {{
    alignment: left;
}}
QTabBar::tab {{
    background: {C_SURF};
    color: {C_MUTED};
    padding: 6px 16px;
    border: none;
    border-bottom: 2px solid transparent;
    font-size: 12px;
    min-width: 70px;
}}
QTabBar::tab:selected {{
    color: {C_TEXT};
    border-bottom: 2px solid {C_ACCENT};
    background: {C_PANEL};
}}
QTabBar::tab:hover {{
    color: {C_TEXT};
    background: {C_PANEL};
}}
QGroupBox {{
    color: {C_MUTED};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    margin-top: 18px;
    padding: 8px 6px 6px 6px;
    font-size: 10px;
    font-variant: small-caps;
    letter-spacing: 1px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    color: {C_MUTED};
}}
QLabel {{
    background: transparent;
    color: {C_TEXT};
}}
QPushButton {{
    background: {C_PANEL};
    color: {C_TEXT};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 5px 14px;
    font-size: 12px;
}}
QPushButton:hover {{
    background: {C_BORDER};
    border-color: {C_ACCENT};
    color: {C_TEXT};
}}
QPushButton:pressed {{
    background: {C_ACCENT};
    color: {C_BG};
}}
QPushButton:disabled {{
    color: {C_MUTED};
    border-color: {C_BORDER};
    background: {C_SURF};
}}
QPushButton#accent {{
    background: {C_ACCENT};
    color: #ffffff;
    border: none;
    font-weight: bold;
}}
QPushButton#accent:hover {{
    background: #8aaaf0;
    color: #ffffff;
}}
QPushButton#accent:pressed {{
    background: #5577cc;
    color: #ffffff;
}}
QPushButton#accent:disabled {{
    background: #3a4a6e;
    color: #8899cc;
}}
QSlider::groove:horizontal {{
    background: {C_BORDER};
    height: 4px;
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {C_ACCENT};
    width: 14px;
    height: 14px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::sub-page:horizontal {{
    background: {C_ACCENT};
    border-radius: 2px;
}}
QComboBox {{
    background: {C_PANEL};
    color: {C_TEXT};
    border: 1px solid {C_BORDER};
    border-radius: 4px;
    padding: 4px 8px;
}}
QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
QComboBox QAbstractItemView {{
    background: {C_PANEL};
    color: {C_TEXT};
    selection-background-color: {C_ACCENT};
    selection-color: {C_BG};
    border: 1px solid {C_BORDER};
}}
QScrollBar:vertical {{
    background: {C_SURF};
    width: 8px;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {C_BORDER};
    border-radius: 4px;
    min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QStatusBar {{
    background: {C_BG};
    color: {C_MUTED};
    font-size: 11px;
    font-family: 'Consolas', 'Courier New', monospace;
    border-top: 1px solid {C_BORDER};
}}
QToolBar {{
    background: {C_BG};
    border-bottom: 1px solid {C_BORDER};
    spacing: 8px;
    padding: 4px 8px;
}}
"""

# ---------------------------------------------------------------------------
# Provenance inspector — mirrors core/biome_assignment.py constants exactly
# so we can show the full math stack in the GUI without importing core/.
# ---------------------------------------------------------------------------
_SEA_NORM   = 17050 / 65535.0          # ≈ 0.260  (sea level normalised)
_LAND_RANGE = 1.0 - _SEA_NORM          # ≈ 0.740

def _terrain_class_at(h: float) -> str:
    """Return terrain class string for a normalised height value [0,1]."""
    if h < _SEA_NORM:
        return "ocean"
    s = _SEA_NORM
    r = _LAND_RANGE
    if h < s + 0.35 * r: return "coastal"
    if h < s + 0.55 * r: return "lowland"
    if h < s + 0.72 * r: return "highland"
    if h < s + 0.88 * r: return "alpine"
    return "ice_cap"

# Override zone code → biome name  (matches OVERRIDE_BIOME_MAP in biome_assignment.py)
_OVERRIDE_NAMES: dict[int, str] = {
    10: "COASTAL_HEATH",        20: "TEMPERATE_RAINFOREST",
    30: "BOREAL_TAIGA",         35: "SNOWY_BOREAL_TAIGA",
    50: "ARCTIC_TUNDRA",  # 40 retired S56
    55: "FROZEN_FLATS",         60: "TEMPERATE_DECIDUOUS",
    70: "RAINFOREST_COAST",     80: "RIPARIAN_WOODLAND",
    90: "DRY_OAK_SAVANNA",     100: "KARST_BARRENS",
   110: "BIRCH_FOREST",         115: "EASTERN_TEMPERATE_COAST",
   120: "MIXED_FOREST",         130: "CONTINENTAL_STEPPE",
   140: "DRY_PINE_BARRENS",    150: "SCRUBBY_HEATHLAND",
   160: "LUSH_RAINFOREST_COAST", 170: "SAND_DUNE_DESERT",
   190: "DESERT_STEPPE_TRANSITION", 200: "SEMI_ARID_SHRUBLAND",
   210: "DRY_WOODLAND_MAQUIS", 220: "TIDAL_JUNGLE_FRINGE",
   230: "MANGROVE_COAST",       240: "FRESHWATER_FEN",
}

def _provenance_at(h_norm: float, override_code: int, biome: str) -> dict:
    """
    Return the full math stack for one pixel as a display dict.
    Pure function — no GUI, no I/O, fully unit-testable.

    Returns:
        raw_h:    int   — 16-bit raw value (0-65535)
        norm:     str   — formatted normalised height
        terrain:  str   — terrain class name
        override: str   — "120 MIXED_FOREST" or "" if no override
        biome:    str   — final biome name
        is_ocean: bool  — True for _OCEAN pixels
    """
    raw_h   = int(round(h_norm * 65535))
    terrain = _terrain_class_at(h_norm)
    ov_str  = f"{override_code} {_OVERRIDE_NAMES[override_code]}" \
              if override_code in _OVERRIDE_NAMES else ""
    is_ocean = biome == "_OCEAN"
    return {
        "raw_h":    raw_h,
        "norm":     f"{h_norm:.4f}",
        "terrain":  terrain,
        "override": ov_str,
        "biome":    biome,
        "is_ocean": is_ocean,
    }


# ---------------------------------------------------------------------------
# Biome colors — top-down inspector palette
# ---------------------------------------------------------------------------
BIOME_COLORS: dict[str, tuple] = {
    # S69: byte-identical to tools/world_biome_map.py (canonical).  See that
    # file for family groupings.  Any change here MUST be mirrored there.
    "_OCEAN":                    ( 30,  80, 160),
    "SNOWY_BOREAL_TAIGA":        (210, 230, 240),
    "BOREAL_ALPINE":             (100, 145, 140),
    "ARCTIC_TUNDRA":             (185, 215, 230),
    "FROZEN_FLATS":              (248, 252, 255),
    "BOREAL_TAIGA":              ( 55, 115,  85),
    "TEMPERATE_DECIDUOUS":       (125, 200,  65),
    "BIRCH_FOREST":              (210, 230, 150),
    "MIXED_FOREST":              ( 90, 155,  70),
    "EASTERN_TEMPERATE_COAST":   ( 70, 145, 170),
    "TEMPERATE_RAINFOREST":      ( 20,  75,  45),
    "RAINFOREST_COAST":          ( 40, 175, 115),
    "LUSH_RAINFOREST_COAST":     ( 15, 125,  75),
    "TIDAL_JUNGLE_FRINGE":       ( 80, 200, 135),
    "MANGROVE_COAST":            ( 40,  95,  70),
    "FRESHWATER_FEN":            (130, 180, 145),
    "RIPARIAN_WOODLAND":         (135, 200, 205),
    "COASTAL_HEATH":             (190, 180,  95),
    "SCRUBBY_HEATHLAND":         (170,  95, 160),
    "DRY_WOODLAND_MAQUIS":       (150, 110,  55),
    "DRY_OAK_SAVANNA":           (215, 160,  60),
    "CONTINENTAL_STEPPE":        (230, 195,  90),
    "DRY_PINE_BARRENS":          (130, 150,  75),
    "KARST_BARRENS":             (195, 185, 180),
    "SAND_DUNE_DESERT":          (245, 215, 130),
    "DESERT_STEPPE_TRANSITION":  (220, 120,  65),
    "SEMI_ARID_SHRUBLAND":       (200, 170,  90),
}

# ---------------------------------------------------------------------------
# Block → approximate RGB  (surface_decorator.py palette preview)
# Values chosen to be visually representative, not exact Minecraft texture averages.
# ---------------------------------------------------------------------------
_BLOCK_COLORS: dict[str, tuple] = {
    "grass_block":        (106, 148,  52),
    "podzol":             (105,  84,  52),
    "mud":                ( 60,  52,  45),
    "coarse_dirt":        ( 90,  67,  48),
    "dirt":               (112,  80,  56),
    "rooted_dirt":        (110,  78,  54),
    "gravel":             (130, 122, 118),
    "stone":              (125, 125, 125),
    "andesite":           ( 96,  96,  96),
    "diorite":            (176, 172, 168),
    "granite":            (154, 110,  97),
    "cobblestone":        (112, 108, 104),
    "mossy_cobblestone":  ( 98, 108,  88),
    "moss_block":         ( 88, 100,  62),
    "snow_block":         (248, 252, 255),
    "ice":                (180, 218, 240),
    "packed_ice":         (155, 196, 228),
    "sand":               (220, 210, 155),
    "sandstone":          (208, 194, 138),
    "red_sand":           (192,  96,  48),
    "red_sandstone":      (164,  78,  38),
    "terracotta":         (152,  94,  68),
    "orange_terracotta":  (178,  98,  52),
    "tuff":               ( 98,  96,  88),
    "calcite":            (218, 218, 208),
    "clay":               (148, 148, 162),
    "mycelium":           (114,  98, 112),
    "obsidian":           ( 15,  10,  25),
    "dripstone_block":    (134, 107,  92),
    "dirt_path":          (148, 124,  82),
    "smooth_sandstone":   (216, 202, 148),
    "short_grass":        ( 90, 140,  50),
    "short_dry_grass":    (168, 156,  92),
    "tall_dry_grass":     (158, 146,  82),
    "bush":               ( 72, 112,  48),
    "leaf_litter":        (120,  92,  56),
    "moss_carpet":        ( 82,  96,  58),
    "pale_moss_carpet":   (168, 172, 158),
    "firefly_bush":       (108, 148,  72),
    "resin_clump":        (180, 130,  50),
    "azalea":             ( 88, 120,  62),
    "flowering_azalea":   (148, 108, 128),
    "sweet_berry_bush":   ( 82, 108,  58),
    "hanging_roots":      (120,  96,  72),
    "cactus":             ( 68, 128,  52),
    "cornflower":         ( 68, 100, 178),
    "allium":             (168,  88, 178),
    "azure_bluet":        (188, 208, 218),
    "oxeye_daisy":        (218, 218, 198),
    "lily_of_the_valley": (228, 238, 218),
    "blue_orchid":        ( 48, 148, 188),
    "pink_tulip":         (218, 148, 178),
    "white_tulip":        (218, 228, 218),
    "red_tulip":          (198,  58,  48),
    "orange_tulip":       (218, 138,  48),
    "sunflower":          (228, 198,  48),
    "peony":              (198, 148, 168),
    "rose_bush":          (168,  48,  48),
    "lilac":              (178, 148, 188),
    "torchflower":        (218, 148,  48),
    "pitcher_plant":      ( 88, 128, 108),
    "default":            (128, 128, 128),   # fallback
}

def _swatch_image(biome: str, w: int = 160, h: int = 80,
                  palettes: Optional[dict] = None) -> QImage:
    """
    Render a noise-textured palette swatch for one biome.
    Top half = surface blocks, bottom half = subsurface blocks.
    Each palette entry gets an equal-width vertical strip.
    Returns a QImage (w × h, RGB32).
    """
    rng = np.random.RandomState(hash(biome) & 0xFFFFFFFF)

    if palettes is None:
        # Lazy import — core not needed at module load time
        try:
            import sys as _sys
            if str(_PROJECT_ROOT) not in _sys.path:
                _sys.path.insert(0, str(_PROJECT_ROOT))
            from core.surface_decorator import BIOME_BLOCK_PALETTES
            palettes = BIOME_BLOCK_PALETTES
        except Exception:
            palettes = {}

    entries = palettes.get(biome, [("stone", "stone", "base")])
    n = len(entries)
    strip_w = max(1, w // n)

    img = np.zeros((h, w, 3), dtype=np.uint8)
    for i, (surf_blk, sub_blk, _cond) in enumerate(entries):
        x0 = i * strip_w
        x1 = min(x0 + strip_w, w)
        # Surface (top half)
        sc = np.array(_BLOCK_COLORS.get(surf_blk, _BLOCK_COLORS["default"]))
        noise_s = rng.randint(-10, 11, (h // 2, x1 - x0, 3))
        img[:h//2, x0:x1] = np.clip(sc + noise_s, 0, 255)
        # Subsurface (bottom half)
        bc = np.array(_BLOCK_COLORS.get(sub_blk, _BLOCK_COLORS["default"]))
        noise_b = rng.randint(-10, 11, (h - h // 2, x1 - x0, 3))
        img[h//2:, x0:x1] = np.clip(bc + noise_b, 0, 255)

    # Thin divider line between surface and subsurface
    div_y = h // 2
    img[div_y, :] = np.clip(img[div_y, :].astype(int) - 30, 0, 255)

    # Build QImage from numpy array (RGB888)
    h_, w_ = img.shape[:2]
    return QImage(img.tobytes(), w_, h_, w_ * 3, QImage.Format.Format_RGB888).copy()


class PaletteEditorWidget(QWidget):
    """Layer-stack surface block editor with live preview.

    Each layer is a noise-driven block placement rule:
      - Noise type (simplex, gaussian, voronoi, mix)
      - Block + sub-surface block
      - Coverage (0-100%) and scale (frequency)
      - Eye toggle (on/off) and drag-to-reorder

    Supports Global mode (applies to all biomes) and Per-Biome overrides.
    Hover a layer card to solo-preview its contribution.
    """

    # Noise type registry. "simplex_fbm" is canonical for the multi-octave
    # simplex branch; "gaussian" remains listed so back-compat configs still
    # load but the preview renders it as actual gaussian-filtered noise
    # (pre-existing legacy behaviour — distinct from the pipeline alias).
    NOISE_TYPES = ["simplex_fbm", "simplex", "gaussian", "voronoi", "mix"]

    _PREVIEW_PX = 380    # display size
    _RENDER_PX  = 128    # internal render size (upscaled bilinear for display)

    # Legacy compat — kept so old code paths don't break
    _APPLY_PRIORITY = [
        "base", "noise4", "noise3", "noise2", "noise",
        "moisture2", "moisture", "erosion2", "erosion", "altitude",
    ]
    _ALL_CONDITIONS = [
        "base", "noise", "noise2", "noise3", "noise4",
        "erosion", "erosion2", "moisture", "moisture2", "altitude",
    ]
    _BASE_THRESHOLDS = {
        "noise": 0.62, "noise2": 0.64, "noise3": 0.67, "noise4": 0.71,
        "erosion": 0.58, "erosion2": 0.73,
        "moisture": 0.62, "moisture2": 0.76, "altitude": 0.70,
    }
    _NOISE_SEEDS = {
        "noise": 42, "noise2": 355, "noise3": 1041, "noise4": 819,
        "erosion": 43, "erosion2": 243,
        "moisture": 44, "moisture2": 444, "altitude": 45,
    }
    _SCALE_MULTS = {
        "erosion": 0.6, "erosion2": 0.6,
        "moisture": 1.4, "moisture2": 1.4, "altitude": 2.0,
    }

    # ── Default layer stack for new biomes ──────────────────────────────
    _DEFAULT_LAYERS = [
        {"name": "Base Surface", "noise": "simplex_fbm", "enabled": True,
         "block": "grass_block", "sub": "dirt", "coverage": 1.0, "scale": 80, "is_base": True},
        {"name": "Stone Scatter", "noise": "simplex_fbm", "enabled": True,
         "block": "stone", "sub": "stone", "coverage": 0.12, "scale": 40, "is_base": False},
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._palettes: Optional[dict] = None
        self._active_biomes: list[str] = []
        self._all_mode = False
        self._current_biome = ""
        self._global_mode = False       # True = editing global layers
        self._layers: list[dict] = []   # current layer stack
        self._hover_solo: int = -1      # layer index being hovered (-1 = composite)
        self._noise_cache: dict = {}    # (noise_type, scale, seed) → field
        self._block_names = sorted(k for k in _BLOCK_COLORS if k != "default")
        # Debounce timer for scale slider — prevents re-rendering every tick
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(80)
        self._render_timer.timeout.connect(self._do_deferred_render)
        self._build_ui()
        # Eagerly load all biomes so the widget is ready before any tile sim
        self._ensure_palettes()
        if self._palettes:
            first = sorted(self._palettes.keys())[0]
            self._all_mode = True  # show all biomes initially
            self._all_btn.setChecked(True)
            self._biome_combo.addItems(sorted(self._palettes.keys()))
            self._biome_combo.setCurrentText(first)

    # ── Palette loading (legacy compat) ───────────────────────────────────
    def _ensure_palettes(self):
        if self._palettes is not None:
            return
        try:
            import sys as _sys
            if str(_PROJECT_ROOT) not in _sys.path:
                _sys.path.insert(0, str(_PROJECT_ROOT))
            from core.surface_decorator import BIOME_BLOCK_PALETTES
            self._palettes = {k: [list(e) for e in v]
                              for k, v in BIOME_BLOCK_PALETTES.items()}
            try:
                with open(CONFIG_PATH) as f:
                    cfg = json.load(f)
                for biome, entries in cfg.get("surface_palettes", {}).items():
                    if biome in self._palettes:
                        self._palettes[biome] = [list(e) for e in entries]
            except Exception:
                pass
        except Exception:
            self._palettes = {}

    # ── Noise field generation ────────────────────────────────────────────
    def _gen_field(self, noise_type: str, scale: float, seed: int) -> np.ndarray:
        """Generate a [0,1] noise field of size _RENDER_PX × _RENDER_PX."""
        key = (noise_type, scale, seed)
        if key in self._noise_cache:
            return self._noise_cache[key]

        sz = self._RENDER_PX
        rng = np.random.default_rng(seed)

        if noise_type in ("simplex", "simplex_fbm"):
            try:
                import opensimplex as ox
                ox.seed(seed)
                xs = np.linspace(0.0, sz / max(scale, 1), sz, dtype=np.double)
                ys = np.linspace(0.0, sz / max(scale, 1), sz, dtype=np.double)
                f = ox.noise2array(xs, ys).astype(np.float32)
                lo, hi = f.min(), f.max()
                field = (f - lo) / (hi - lo + 1e-9)
            except ImportError:
                from scipy.ndimage import gaussian_filter
                raw = rng.random((sz, sz), dtype=np.float32)
                field = gaussian_filter(raw, sigma=max(1, scale / 15))

        elif noise_type == "gaussian":
            from scipy.ndimage import gaussian_filter
            raw = rng.random((sz, sz), dtype=np.float32)
            sigma = max(0.5, scale / 20)
            field = gaussian_filter(raw, sigma=sigma)

        elif noise_type == "voronoi":
            # Cellular / mosaic pattern via KDTree (fast)
            from scipy.spatial import cKDTree
            n_seeds = max(4, int(sz * sz / (scale * scale * 4)))
            n_seeds = min(n_seeds, 500)
            pts = np.column_stack([rng.random(n_seeds) * sz,
                                   rng.random(n_seeds) * sz])
            tree = cKDTree(pts)
            yy, xx = np.mgrid[0:sz, 0:sz]
            coords = np.column_stack([yy.ravel(), xx.ravel()])
            min_dist, _ = tree.query(coords)
            min_dist = min_dist.reshape(sz, sz).astype(np.float32)
            lo, hi = min_dist.min(), min_dist.max()
            field = (min_dist - lo) / (hi - lo + 1e-9)

        elif noise_type == "mix":
            # 60% simplex + 40% voronoi
            f1 = self._gen_field("simplex", scale, seed)
            f2 = self._gen_field("voronoi", scale, seed + 10000)
            field = 0.6 * f1 + 0.4 * f2
        else:
            field = rng.random((sz, sz)).astype(np.float32)

        # Normalise to [0, 1]
        lo, hi = field.min(), field.max()
        if hi - lo > 1e-9:
            field = (field - lo) / (hi - lo)
        self._noise_cache[key] = field
        return field

    # ── Layer data I/O ────────────────────────────────────────────────────
    def _load_layers(self):
        """Load layer stack from thresholds.json for current mode/biome."""
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        if self._global_mode:
            layers = cfg.get("noise_layers_global", None)
        else:
            biome = self._current_biome
            layers = cfg.get("noise_layers_biome", {}).get(biome, None)

        if layers is not None:
            self._layers = [dict(l) for l in layers]
        else:
            # Convert from legacy palette format
            self._layers = self._layers_from_legacy()

    def _layers_from_legacy(self) -> list[dict]:
        """Convert old-style palette + sparse_overrides to layer stack."""
        self._ensure_palettes()
        biome = self._current_biome
        if not biome:
            return [dict(l) for l in self._DEFAULT_LAYERS]
        palette = self._palettes.get(biome, [["grass_block", "dirt", "base"]])

        # Load sparse_overrides for thresholds
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            overrides = cfg.get("sparse_overrides", {}).get(biome, {})
        except Exception:
            overrides = {}

        layers = []
        for surf, sub, cond in palette:
            thr = overrides.get(cond, self._BASE_THRESHOLDS.get(cond, 0.65))
            coverage = round(1.0 - thr, 2) if cond != "base" else 1.0
            seed_val = self._NOISE_SEEDS.get(cond, 42)
            layers.append({
                "name": f"{surf}" if cond == "base" else f"{surf} ({cond})",
                "noise": "simplex_fbm",
                "enabled": True,
                "block": surf,
                "sub": sub,
                "coverage": coverage,
                "scale": int(60 * self._SCALE_MULTS.get(cond, 1.0)),
                "seed": seed_val,
                "is_base": cond == "base",
            })
        return layers

    def _save_layers(self):
        """Save current layer stack to thresholds.json."""
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        layer_data = []
        for l in self._layers:
            layer_data.append({
                "name": l["name"], "noise": l["noise"], "enabled": l["enabled"],
                "block": l["block"], "sub": l.get("sub", "stone"),
                "coverage": round(l["coverage"], 3),
                "scale": l["scale"], "seed": l.get("seed", 42),
                "is_base": l.get("is_base", False),
            })

        if self._global_mode:
            cfg["noise_layers_global"] = layer_data
        else:
            biome_layers = cfg.setdefault("noise_layers_biome", {})
            biome_layers[self._current_biome] = layer_data

        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(6)

        # ── LEFT: layer stack panel ──────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(4)

        # Header: mode toggle + biome selector
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        self._mode_radio_biome = QPushButton("Per-Biome")
        self._mode_radio_biome.setCheckable(True)
        self._mode_radio_biome.setChecked(True)
        self._mode_radio_biome.setFixedWidth(80)
        self._mode_radio_biome.clicked.connect(lambda: self._set_mode(False))
        self._mode_radio_global = QPushButton("Global")
        self._mode_radio_global.setCheckable(True)
        self._mode_radio_global.setFixedWidth(60)
        self._mode_radio_global.clicked.connect(lambda: self._set_mode(True))
        hdr.addWidget(self._mode_radio_biome)
        hdr.addWidget(self._mode_radio_global)
        hdr.addSpacing(8)

        self._biome_combo = QComboBox()
        self._biome_combo.setMinimumWidth(120)
        self._biome_combo.currentTextChanged.connect(self._on_biome_changed)
        hdr.addWidget(QLabel("Biome:"))
        hdr.addWidget(self._biome_combo, stretch=1)
        self._all_btn = QPushButton("All")
        self._all_btn.setCheckable(True)
        self._all_btn.setFixedWidth(40)
        self._all_btn.setToolTip("Show all biomes in dropdown")
        self._all_btn.clicked.connect(self._toggle_all)
        hdr.addWidget(self._all_btn)
        left.addLayout(hdr)

        # Layer list (scrollable)
        self._layer_scroll = QScrollArea()
        self._layer_scroll.setWidgetResizable(True)
        self._layer_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._layer_scroll.setStyleSheet(f"background:{C_BG};")
        self._layer_container = QWidget()
        self._layer_layout = QVBoxLayout(self._layer_container)
        self._layer_layout.setContentsMargins(0, 0, 0, 0)
        self._layer_layout.setSpacing(2)
        self._layer_layout.addStretch()
        self._layer_scroll.setWidget(self._layer_container)
        left.addWidget(self._layer_scroll, stretch=1)

        # Add layer button
        add_btn = QPushButton("+ Add Layer")
        add_btn.setFixedHeight(28)
        add_btn.clicked.connect(self._add_layer)
        left.addWidget(add_btn)

        # Bottom: coverage + apply/reset
        bot = QHBoxLayout()
        self._coverage_lbl = QLabel("")
        self._coverage_lbl.setStyleSheet(f"font-size:9px; color:{C_MUTED};")
        self._coverage_lbl.setWordWrap(True)
        bot.addWidget(self._coverage_lbl, stretch=1)
        apply_btn = QPushButton("Apply")
        apply_btn.setObjectName("accent")
        apply_btn.clicked.connect(self._on_apply)
        bot.addWidget(apply_btn)
        reset_btn = QPushButton("Reset")
        reset_btn.clicked.connect(self._on_reset)
        bot.addWidget(reset_btn)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"font-size:10px; color:{C_MUTED};")
        bot.addWidget(self._status_lbl)
        left.addLayout(bot)

        root.addLayout(left, stretch=1)

        # ── RIGHT: preview ───────────────────────────────────────────────
        right = QVBoxLayout()
        right.setSpacing(4)
        self._preview_lbl = QLabel()
        self._preview_lbl.setFixedSize(self._PREVIEW_PX, self._PREVIEW_PX)
        self._preview_lbl.setStyleSheet("background:#0a1a2e; border:1px solid #2a3a5e;")
        self._preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(self._preview_lbl)
        hint = QLabel("Check 'Solo' on a layer to isolate it")
        hint.setStyleSheet(f"font-size:9px; color:{C_MUTED}; font-style:italic;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(hint)
        right.addStretch()
        root.addLayout(right)

    # ── Layer card builder ────────────────────────────────────────────────
    def _rebuild_layer_cards(self):
        """Rebuild all layer card widgets from self._layers."""
        # Clear existing
        old = self._layer_container
        new_c = QWidget()
        new_l = QVBoxLayout(new_c)
        new_l.setContentsMargins(0, 0, 0, 0)
        new_l.setSpacing(2)

        # Layers render top-to-bottom (top = highest priority)
        for i, layer in enumerate(self._layers):
            card = self._make_layer_card(i, layer)
            new_l.addWidget(card)

        new_l.addStretch()
        self._layer_scroll.setWidget(new_c)
        self._layer_container = new_c
        self._layer_layout = new_l

    def _make_layer_card(self, idx: int, layer: dict) -> QWidget:
        """Build a single layer card widget."""
        is_base = layer.get("is_base", False)
        card = QFrame()
        card.setFrameShape(QFrame.Shape.StyledPanel)
        bg = "#162236" if layer["enabled"] else "#0e1520"
        card.setStyleSheet(
            f"QFrame {{ background:{bg}; border:1px solid {C_BORDER}; "
            f"border-radius:4px; }}")
        card.setFixedHeight(78 if not is_base else 48)

        vl = QVBoxLayout(card)
        vl.setContentsMargins(6, 3, 6, 3)
        vl.setSpacing(2)

        # Row 1: enable checkbox, block, sub, noise, solo, move, delete
        r1 = QHBoxLayout()
        r1.setSpacing(4)

        # Enable checkbox
        chk = QCheckBox()
        chk.setChecked(layer["enabled"])
        chk.setToolTip("Enable/disable this layer")
        chk.toggled.connect(lambda checked, i=idx: self._toggle_layer_to(i, checked))
        r1.addWidget(chk)

        if is_base:
            name_lbl = QLabel("BASE")
            name_lbl.setStyleSheet(f"color:{C_ACCENT}; font-weight:bold; font-size:11px;")
            r1.addWidget(name_lbl)
        else:
            up_btn = QPushButton("+")
            up_btn.setFixedSize(22, 22)
            up_btn.setToolTip("Move layer up (higher priority)")
            up_btn.setStyleSheet(f"font-size:12px; font-weight:bold;")
            def _do_up(_checked=False, _i=idx):
                self._move_layer(_i, -1)
            up_btn.clicked.connect(_do_up)
            dn_btn = QPushButton("-")
            dn_btn.setFixedSize(22, 22)
            dn_btn.setToolTip("Move layer down (lower priority)")
            dn_btn.setStyleSheet(f"font-size:12px; font-weight:bold;")
            def _do_dn(_checked=False, _i=idx):
                self._move_layer(_i, 1)
            dn_btn.clicked.connect(_do_dn)
            r1.addWidget(up_btn)
            r1.addWidget(dn_btn)

        # Block dropdown
        blk = QComboBox()
        blk.addItems(self._block_names)
        blk_idx = blk.findText(layer["block"])
        if blk_idx >= 0:
            blk.setCurrentIndex(blk_idx)
        blk.setFixedWidth(130)
        blk.setStyleSheet("font-size:10px;")
        blk.currentTextChanged.connect(
            lambda t, i=idx: self._update_layer(i, "block", t))
        r1.addWidget(blk)

        # Noise type dropdown (not for base)
        if not is_base:
            nt = QComboBox()
            nt.addItems(self.NOISE_TYPES)
            nt_idx = nt.findText(layer["noise"])
            if nt_idx >= 0:
                nt.setCurrentIndex(nt_idx)
            nt.setFixedWidth(80)
            nt.setStyleSheet("font-size:10px;")
            nt.currentTextChanged.connect(
                lambda t, i=idx: self._update_layer(i, "noise", t))
            r1.addWidget(nt)

        r1.addStretch()

        # Solo checkbox (not for base)
        if not is_base:
            solo = QCheckBox("Solo")
            solo.setStyleSheet(f"font-size:9px; color:{C_MUTED};")
            solo.setChecked(self._hover_solo == idx)
            solo.toggled.connect(lambda checked, i=idx: self._set_solo(i if checked else -1))
            r1.addWidget(solo)

        # Delete button (not for base)
        if not is_base:
            del_btn = QPushButton("Del")
            del_btn.setFixedSize(32, 20)
            del_btn.setStyleSheet(f"color:#ff6666; font-size:9px;")
            del_btn.clicked.connect(lambda _, i=idx: self._delete_layer(i))
            r1.addWidget(del_btn)

        vl.addLayout(r1)

        # Row 2: coverage + scale sliders (not for base)
        if not is_base:
            r2 = QHBoxLayout()
            r2.setSpacing(4)

            cov_lbl = QLabel("Cov:")
            cov_lbl.setFixedWidth(28)
            cov_lbl.setStyleSheet(f"font-size:9px; color:{C_MUTED};")
            r2.addWidget(cov_lbl)
            cov_sl = QSlider(Qt.Orientation.Horizontal)
            cov_sl.setRange(1, 95)
            cov_sl.setValue(int(layer["coverage"] * 100))
            cov_val = QLabel(f"{int(layer['coverage']*100)}%")
            cov_val.setFixedWidth(30)
            cov_val.setStyleSheet(f"font-size:9px; color:{C_MUTED};")
            def _on_cov_changed(v, _i=idx, _lbl=cov_val):
                _lbl.setText(f"{v}%")
                self._update_layer(_i, "coverage", v / 100.0)
            cov_sl.valueChanged.connect(_on_cov_changed)
            r2.addWidget(cov_sl, stretch=1)
            r2.addWidget(cov_val)

            r2.addSpacing(8)
            sc_lbl = QLabel("Scale:")
            sc_lbl.setFixedWidth(34)
            sc_lbl.setStyleSheet(f"font-size:9px; color:{C_MUTED};")
            r2.addWidget(sc_lbl)
            sc_sl = QSlider(Qt.Orientation.Horizontal)
            sc_sl.setRange(5, 200)
            sc_sl.setValue(layer.get("scale", 60))
            sc_val = QLabel(str(layer.get("scale", 60)))
            sc_val.setFixedWidth(26)
            sc_val.setStyleSheet(f"font-size:9px; color:{C_MUTED};")
            def _on_scale_changed(v, _i=idx, _lbl=sc_val):
                _lbl.setText(str(v))
                self._update_layer(_i, "scale", v)
            sc_sl.valueChanged.connect(_on_scale_changed)
            r2.addWidget(sc_sl, stretch=1)
            r2.addWidget(sc_val)

            vl.addLayout(r2)

        return card

    # ── Layer operations ──────────────────────────────────────────────────
    def _toggle_layer(self, idx: int):
        self._layers[idx]["enabled"] = not self._layers[idx]["enabled"]
        self._rebuild_layer_cards()
        self._render_preview()

    def _toggle_layer_to(self, idx: int, enabled: bool):
        self._layers[idx]["enabled"] = enabled
        self._rebuild_layer_cards()
        self._render_preview()

    def _set_solo(self, idx: int):
        self._hover_solo = idx
        self._rebuild_layer_cards()
        self._render_preview()

    def _move_layer(self, idx: int, direction: int):
        new_idx = idx + direction
        if 0 <= new_idx < len(self._layers):
            self._layers[idx], self._layers[new_idx] = \
                self._layers[new_idx], self._layers[idx]
            self._rebuild_layer_cards()
            self._render_preview()

    def _delete_layer(self, idx: int):
        if len(self._layers) > 1:
            self._layers.pop(idx)
            self._rebuild_layer_cards()
            self._render_preview()

    def _add_layer(self):
        seed = np.random.default_rng().integers(1, 99999)
        self._layers.insert(0, {
            "name": "New Layer", "noise": "simplex_fbm", "enabled": True,
            "block": "gravel", "sub": "stone", "coverage": 0.10,
            "scale": 50, "seed": int(seed), "is_base": False,
        })
        self._rebuild_layer_cards()
        self._render_preview()

    def _update_layer(self, idx: int, key: str, value):
        if 0 <= idx < len(self._layers):
            old_layer = self._layers[idx]
            old_layer[key] = value
            if key in ("noise", "scale", "seed"):
                # Only invalidate THIS layer's cached field
                old_key = (old_layer["noise"], old_layer["scale"],
                           old_layer.get("seed", 42 + idx))
                self._noise_cache.pop(old_key, None)
                self._render_timer.start()
            else:
                self._render_preview()

    def _do_deferred_render(self):
        self._render_preview()

    # Solo preview is now controlled by checkbox, not hover

    # ── Biome / mode selection ────────────────────────────────────────────
    def _set_mode(self, global_mode: bool):
        self._global_mode = global_mode
        self._mode_radio_global.setChecked(global_mode)
        self._mode_radio_biome.setChecked(not global_mode)
        self._biome_combo.setEnabled(not global_mode)
        self._load_layers()
        self._rebuild_layer_cards()
        self._render_preview()

    def _toggle_all(self, checked: bool):
        self._all_mode = checked
        self._refresh_biome_combo()

    def update_biomes(self, biome_names: list[str]):
        """Called when a tile sim completes."""
        self._active_biomes = [b for b in biome_names if not b.startswith("_")]
        self._refresh_biome_combo()
        if self._active_biomes and not self._current_biome:
            self._biome_combo.setCurrentText(sorted(self._active_biomes)[0])

    def _refresh_biome_combo(self):
        self._ensure_palettes()
        self._biome_combo.blockSignals(True)
        prev = self._biome_combo.currentText()
        self._biome_combo.clear()
        if self._all_mode and self._palettes:
            items = sorted(self._palettes.keys())
        elif self._active_biomes:
            items = sorted(self._active_biomes)
        else:
            items = sorted(self._palettes.keys()) if self._palettes else []
        self._biome_combo.addItems(items)
        if prev in items:
            self._biome_combo.setCurrentText(prev)
        self._biome_combo.blockSignals(False)
        if self._biome_combo.currentText() != self._current_biome:
            self._on_biome_changed(self._biome_combo.currentText())

    def _on_biome_changed(self, name: str):
        if not name or self._global_mode:
            return
        self._current_biome = name
        self._load_layers()
        self._rebuild_layer_cards()
        self._render_preview()

    # ── Preview renderer ──────────────────────────────────────────────────
    def _render_preview(self, solo_idx: int = -1):
        """Render composite (or solo) preview from layer stack.

        Renders at _RENDER_PX (128) internally, upscales to _PREVIEW_PX (380)
        for display.  128×128 = 16K pixels vs 380×380 = 144K — 9× faster.
        """
        if not self._layers:
            return
        sz = self._RENDER_PX
        dsz = self._PREVIEW_PX
        solo = self._hover_solo if solo_idx < 0 else solo_idx

        # Work with integer block indices for speed (no object arrays)
        # Build block → index mapping from active layers
        block_set = set()
        for layer in self._layers:
            block_set.add(layer["block"])
        block_list = sorted(block_set)
        blk_to_idx = {b: i for i, b in enumerate(block_list)}

        # Initialize surface grid with base
        base_blk = "stone"
        for layer in reversed(self._layers):
            if layer.get("is_base") and layer["enabled"]:
                base_blk = layer["block"]
                break
        grid = np.full((sz, sz), blk_to_idx.get(base_blk, 0), dtype=np.uint8)

        # Apply layers bottom-to-top
        for i in range(len(self._layers) - 1, -1, -1):
            layer = self._layers[i]
            if not layer["enabled"] or layer.get("is_base"):
                continue
            if solo >= 0 and i != solo:
                continue

            field = self._gen_field(
                layer["noise"],
                layer.get("scale", 60),
                layer.get("seed", 42 + i),
            )
            threshold = 1.0 - layer["coverage"]
            mask = field >= threshold
            grid[mask] = blk_to_idx.get(layer["block"], 0)

        # Colorize at render resolution
        # Build RGB LUT from block indices
        lut = np.zeros((len(block_list), 3), dtype=np.uint8)
        for i, blk in enumerate(block_list):
            lut[i] = _BLOCK_COLORS.get(blk, (128, 128, 128))

        img = lut[grid]  # (sz, sz, 3) via fancy indexing

        # Add subtle noise texture
        rng = np.random.default_rng(hash(self._current_biome or "global") & 0xFFFFFFFF)
        noise_tex = rng.integers(-6, 7, (sz, sz, 3), dtype=np.int16)
        img = np.clip(img.astype(np.int16) + noise_tex, 0, 255).astype(np.uint8)

        # Coverage stats
        total = sz * sz
        parts = []
        for bi in range(len(block_list)):
            cnt = int((grid == bi).sum())
            pct = cnt / total * 100
            if pct >= 0.5:
                parts.append(f"{block_list[bi]} {pct:.0f}%")
        self._coverage_lbl.setText("  ".join(parts))

        # Upscale to display size and show
        # Use PIL for fast nearest-neighbor upscale
        from PIL import Image as _PILImage
        pil_img = _PILImage.fromarray(img, "RGB").resize(
            (dsz, dsz), _PILImage.Resampling.BILINEAR)
        arr = np.array(pil_img)
        qimg = QImage(arr.tobytes(), dsz, dsz, dsz * 3, QImage.Format.Format_RGB888)
        self._preview_lbl.setPixmap(QPixmap.fromImage(qimg.copy()))

    # ── Apply / Reset ─────────────────────────────────────────────────────
    def _on_apply(self):
        self._save_layers()
        self._status_lbl.setText("Saved.")
        self._status_lbl.setStyleSheet(f"font-size:10px; color:{C_OK};")
        # Clear status after 2s so button doesn't look stuck
        QTimer.singleShot(2000, lambda: self._status_lbl.setText(""))

    def _on_reset(self):
        self._noise_cache.clear()
        self._load_layers()
        self._rebuild_layer_cards()
        self._render_preview()
        self._status_lbl.setText("Reset.")
        self._status_lbl.setStyleSheet(f"font-size:10px; color:{C_MUTED};")


# ---------------------------------------------------------------------------
# Height → MC Y lookup table  (raw uint16 → MC Y coordinate)
# ---------------------------------------------------------------------------
def _build_lut(gaea_in=None, mc_y_out=None) -> np.ndarray:
    """Interpolates 16-bit raw height → Minecraft Y using spline from config."""
    if gaea_in is None or mc_y_out is None:
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            ts = cfg.get("terrain_spline", {})
            gaea_in  = ts.get("gaea_in",  [0, 17050, 45000, 65496])
            mc_y_out = ts.get("mc_y_out", [-64, 63, 200, 448])
        except Exception:
            gaea_in  = [0, 17050, 45000, 65496]
            mc_y_out = [-64, 63, 200, 448]
    lut = np.interp(np.arange(65536, dtype=np.float64),
                    np.array(gaea_in, dtype=np.float64),
                    np.array(mc_y_out, dtype=np.float64))
    return np.clip(lut, -64, 448).astype(np.int16)

_LUT = _build_lut()

def _rebuild_lut(gaea_in=None, mc_y_out=None):
    """Rebuild the global _LUT from new spline values. Call after spline Apply."""
    global _LUT
    _LUT = _build_lut(gaea_in, mc_y_out)

# ---------------------------------------------------------------------------
# Terrain colormap  (height-based, piecewise stops anchored to sea level)
# ---------------------------------------------------------------------------
_CMAP_STOPS = [
    (0.00, (0x08, 0x18, 0x50)),   # deep ocean floor
    (0.10, (0x16, 0x46, 0x8C)),
    (0.18, (0x22, 0x78, 0xB0)),
    (0.22, (0x3C, 0xA0, 0x60)),   # sea level Y=63 → norm 0.22
    (0.30, (0x5A, 0xB8, 0x48)),
    (0.42, (0x7C, 0x96, 0x50)),
    (0.58, (0x9E, 0x8A, 0x58)),
    (0.72, (0xB8, 0xAC, 0x84)),
    (0.85, (0xD4, 0xCC, 0xB4)),
    (1.00, (0xF8, 0xF8, 0xFF)),   # alpine peaks
]

def _h_to_rgba(h_data: np.ndarray) -> np.ndarray:
    """
    Convert raw height data to terrain-colored RGBA.

    h_data: (H, W) uint16 [0, 65535]  OR  float32 [0, 1]
    Returns: (H, W, 4) uint8 RGBA
    """
    h_f32 = h_data.astype(np.float32)
    # Handle both raw uint16 and pre-normalized float
    if h_f32.max() > 1.0:
        h_f32 = h_f32 / 65535.0
    u16   = (h_f32 * 65535).clip(0, 65535).astype(np.uint16)
    mc_y  = _LUT[u16].astype(np.float32)
    # Piecewise normalization: sea level (Y=63) anchors at 0.22
    sea_norm = 0.22
    norm = np.where(
        mc_y <= SEA_Y,
        (mc_y - Y_MIN) / (SEA_Y - Y_MIN) * sea_norm,
        sea_norm + (mc_y - SEA_Y) / (Y_MAX - SEA_Y) * (1.0 - sea_norm),
    ).astype(np.float32)
    norm = np.clip(norm, 0.0, 1.0)
    ns = np.array([s[0] for s in _CMAP_STOPS])
    rs = np.array([s[1][0] for s in _CMAP_STOPS])
    gs = np.array([s[1][1] for s in _CMAP_STOPS])
    bs = np.array([s[1][2] for s in _CMAP_STOPS])
    flat = norm.ravel()
    r = np.interp(flat, ns, rs).reshape(norm.shape).astype(np.uint8)
    g = np.interp(flat, ns, gs).reshape(norm.shape).astype(np.uint8)
    b = np.interp(flat, ns, bs).reshape(norm.shape).astype(np.uint8)
    a = np.full_like(r, 255)
    return np.stack([r, g, b, a], axis=-1)


def _rgba_to_qpixmap(rgba: np.ndarray) -> QPixmap:
    """Convert (H, W, 4) uint8 RGBA numpy array to QPixmap."""
    h, w = rgba.shape[:2]
    img = QImage(rgba.tobytes(), w, h, w * 4, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(img)


# ---------------------------------------------------------------------------
# Cliff banding helpers  (mirrors chunk_writer.py exactly)
# ---------------------------------------------------------------------------
_BIOME_CLIFF_STONE: dict[str, str] = {
    "ARCTIC_TUNDRA": "andesite",
    "BOREAL_TAIGA": "andesite",         "SNOWY_BOREAL_TAIGA": "andesite",
    "FROZEN_FLATS": "andesite",         "COASTAL_HEATH": "andesite",
    "SCRUBBY_HEATHLAND": "andesite",    "KARST_BARRENS": "tuff",
    "SAND_DUNE_DESERT": "sandstone",    "DESERT_STEPPE_TRANSITION": "sandstone",
    "SEMI_ARID_SHRUBLAND": "sandstone", "DRY_WOODLAND_MAQUIS": "sandstone",
    "DRY_PINE_BARRENS": "sandstone",    "DRY_OAK_SAVANNA": "sandstone",
    "MIXED_FOREST": "granite",          "BIRCH_FOREST": "granite",
    "CONTINENTAL_STEPPE": "granite",    "TEMPERATE_RAINFOREST": "diorite",
    "TEMPERATE_DECIDUOUS": "diorite",   "RAINFOREST_COAST": "diorite",
    "LUSH_RAINFOREST_COAST": "diorite", "EASTERN_TEMPERATE_COAST": "diorite",
    "RIPARIAN_WOODLAND": "stone",       "MANGROVE_COAST": "stone",
    "FRESHWATER_FEN": "stone",          "TIDAL_JUNGLE_FRINGE": "stone",
}
_CLIFF_BANDS: dict[str, list[str]] = {
    "stone":     ["stone",     "gravel",     "tuff",        "cobblestone", "stone"],
    "andesite":  ["andesite",  "cobblestone","stone",        "gravel",      "andesite"],
    "diorite":   ["diorite",   "stone",      "calcite",      "gravel",      "diorite"],
    "granite":   ["granite",   "stone",      "cobblestone",  "gravel",      "granite"],
    "tuff":      ["tuff",      "stone",      "gravel",       "tuff",        "cobblestone"],
    "sandstone": ["sandstone", "red_sand",   "gravel",       "sandstone",   "sand"],
}
BLOCK_COLORS: dict[str, tuple] = {
    "bedrock": (58,58,58),       "stone": (138,138,138),      "cobblestone": (114,114,114),
    "andesite": (140,140,142),   "polished_andesite": (136,136,144),
    "diorite": (196,194,192),    "calcite": (222,220,216),    "granite": (170,122,102),
    "tuff": (106,107,94),        "sandstone": (217,201,130),  "red_sand": (191,107,44),
    "sand": (227,214,152),       "gravel": (153,150,144),     "dirt": (139,96,58),
    "coarse_dirt": (114,77,45),  "podzol": (103,64,30),       "mud": (84,68,50),
    "clay": (158,163,170),       "grass_block": (89,155,58),  "moss_block": (78,122,48),
    "mycelium": (124,104,124),   "snow_block": (240,244,248), "ice": (160,200,240),
    "packed_ice": (128,176,232), "water": (46,110,184),       "air": (24,56,96),
    "oak_log": (104,78,42),      "oak_leaves": (58,122,42),
    "spruce_log": (78,56,30),    "spruce_leaves": (40,96,40),
    "terracotta": (151,95,69),   "red_terracotta": (160,64,40),
}

def _cell_hash_s(ri: int, ci: int) -> float:
    """Deterministic pseudo-random float [0,1] from integer grid cell coords."""
    h = (ri * 2654435761 ^ ci * 2246822519) & 0xFFFFFFFF
    h ^= (h >> 16) & 0xFFFFFFFF
    h  = (h * 0x45D9F3B) & 0xFFFFFFFF
    h ^= (h >> 16) & 0xFFFFFFFF
    return h * 2.3283064e-10

def _banded_stone(biome: str, wx: int, wz: int, mc_y: int, bsy: int) -> str:
    """Return banded stone block for cliff face. Mirrors chunk_writer.py exactly."""
    prim     = _BIOME_CLIFF_STONE.get(biome, "stone")
    variants = _CLIFF_BANDS.get(prim, _CLIFF_BANDS["stone"])
    n_v = len(variants)
    wa  = max(1, bsy // 3)
    wc  = 32
    rc, cc = wz // wc, wx // wc
    rf, cf = (wz % wc) / wc, (wx % wc) / wc
    # Bilinear smooth hash — NOTE: fixed bug (was rc,rc for second arg)
    sm = (  _cell_hash_s(rc,   cc)   * (1-rf) * (1-cf)
          + _cell_hash_s(rc+1, cc)   *    rf   * (1-cf)
          + _cell_hash_s(rc,   cc+1) * (1-rf)  *    cf
          + _cell_hash_s(rc+1, cc+1) *    rf   *    cf)
    return variants[((mc_y + int((sm * 2 - 1) * wa)) // bsy) % n_v]


# ---------------------------------------------------------------------------
# Pipeline runner — runs steps 1-7 in a worker thread
# ---------------------------------------------------------------------------
BIOME_TO_MC: dict[str, str] = {
    "COASTAL_HEATH":             "minecraft:windswept_hills",
    "TEMPERATE_RAINFOREST":      "minecraft:old_growth_spruce_taiga",
    "BOREAL_TAIGA":              "minecraft:taiga",
    "SNOWY_BOREAL_TAIGA":        "minecraft:snowy_taiga",
    "ARCTIC_TUNDRA":             "minecraft:frozen_peaks",
    "FROZEN_FLATS":              "minecraft:ice_spikes",
    "TEMPERATE_DECIDUOUS":       "minecraft:forest",
    "RAINFOREST_COAST":          "minecraft:old_growth_birch_forest",
    "RIPARIAN_WOODLAND":         "minecraft:dark_forest",
    "DRY_OAK_SAVANNA":           "minecraft:savanna",
    "KARST_BARRENS":             "minecraft:windswept_gravelly_hills",
    "BIRCH_FOREST":              "minecraft:birch_forest",
    "EASTERN_TEMPERATE_COAST":   "minecraft:beach",
    "MIXED_FOREST":              "minecraft:forest",
    "CONTINENTAL_STEPPE":        "minecraft:plains",
    "DRY_PINE_BARRENS":          "minecraft:wooded_badlands",
    "SCRUBBY_HEATHLAND":         "minecraft:windswept_hills",
    "LUSH_RAINFOREST_COAST":     "minecraft:jungle",
    "SAND_DUNE_DESERT":          "minecraft:desert",
    "DESERT_STEPPE_TRANSITION":  "minecraft:savanna_plateau",
    "SEMI_ARID_SHRUBLAND":       "minecraft:savanna",
    "DRY_WOODLAND_MAQUIS":       "minecraft:sparse_jungle",
    "TIDAL_JUNGLE_FRINGE":       "minecraft:sparse_jungle",
    "MANGROVE_COAST":            "minecraft:mangrove_swamp",
    "FRESHWATER_FEN":            "minecraft:swamp",
    "_OCEAN":                    "minecraft:ocean",
}

def run_pipeline(tx: int, tz: int, cfg: dict, progress_cb=None) -> dict:
    """
    Execute pipeline steps 1-7 for tile (tx, tz).
    Returns dict with surface_y, biome_grid, surface_blk, masks.
    """
    import importlib
    core_biome = importlib.import_module("core.biome_assignment")
    core_tiles = importlib.import_module("core.tile_streamer")
    core_col   = importlib.import_module("core.column_generator")
    core_river = importlib.import_module("core.river_carver")
    core_noise = importlib.import_module("core.noise_fields")

    col_off, row_off = tx * TILE_PX, tz * TILE_PX

    if progress_cb: progress_cb("Reading masks…")
    masks = core_tiles.read_tile(
        masks_dir=MASKS_DIR, col_off=col_off, row_off=row_off,
        width=TILE_PX, height=TILE_PX)
    noise = core_noise.load_noise_generators(CONFIG_PATH)

    if progress_cb: progress_cb("Biome assignment…")
    biome_grid = core_biome.assign_biomes(
        height_tile=masks["height"],   slope_tile=masks["slope"],
        flow_tile=masks["flow"],       erosion_tile=masks["erosion"],
        override_tile=masks["override"], noise_fields=noise, cfg=cfg)

    if progress_cb: progress_cb("Column generation…")
    h_u16  = (masks["height"]  * 65535).astype(np.uint16)
    sl_u16 = (masks["slope"]   * 65535).astype(np.uint16)
    er_u16 = (masks["erosion"] * 65535).astype(np.uint16)
    fl_u16 = (masks["flow"]    * 65535).astype(np.uint16)

    mc_biomes = np.empty(biome_grid.shape, dtype=object)
    for b in np.unique(biome_grid):
        mc_biomes[biome_grid == b] = BIOME_TO_MC.get(str(b), "minecraft:plains")

    col_results = core_col.process_tile_columns_v2(
        tile_height=h_u16, tile_slope=sl_u16,
        tile_erosion=er_u16, tile_flow=fl_u16,
        tile_deposits=er_u16.copy(), tile_shore=masks["shore"] > 0.5,
        tile_biomes=biome_grid, tile_mc_biomes=mc_biomes,
        tile_origin_x=col_off, tile_origin_y=row_off,
        noise_gens=noise, cfg=cfg)

    if progress_cb: progress_cb("River carving…")
    core_river.carve_tile(tile_columns=col_results, tile_flow=masks["flow"], cfg=cfg)

    surface_y = np.array(
        [[cr.surface_y for cr in row] for row in col_results], dtype=np.int16)
    flat = [cr for row in col_results for cr in row]
    surface_blk = np.array(
        [(cr.blocks.get(cr.surface_y) or "grass_block") for cr in flat],
        dtype=object).reshape(TILE_PX, TILE_PX)

    return dict(
        surface_y=surface_y, biome_grid=biome_grid, surface_blk=surface_blk,
        masks=masks, tile_x=tx, tile_z=tz,
        col_off=col_off, row_off=row_off)



def make_xsec_from_hnorm(h_norm: np.ndarray, z_row: int) -> np.ndarray:
    """
    Height-only cross-section from h_norm float32 [0,1]. No pipeline needed.
    Returns (Y_RANGE, TILE_PX, 4) uint8 RGBA.
    Uses stone fill + grass/sand surface + water — no biome banding.
    """
    h_u16  = (h_norm * 65535).clip(0, 65535).astype(np.uint16)
    surf   = _LUT[h_u16[z_row]].astype(np.int32)   # (W,) MC Y values

    W, H   = TILE_PX, Y_RANGE
    PY_SEA = (H - 1 + Y_MIN) - SEA_Y               # = 384
    py_surf = (H - 1 + Y_MIN) - surf                # (W,)

    def c(name, default=(96, 32, 128)):
        return np.array(BLOCK_COLORS.get(name, default), dtype=np.uint8)

    img = np.full((H, W, 3), c("air", (24, 56, 96)), dtype=np.uint8)
    img[H - 1, :] = c("bedrock")

    stone_rgb = c("stone")
    for x in range(W):
        py_top = py_surf[x] + 3
        py_bot = H - 2
        if 0 <= py_top <= py_bot:
            img[py_top:py_bot + 1, x] = stone_rgb

    dirt_rgb = c("dirt")
    for d in (1, 2):
        py_d = py_surf + d
        mask = (py_d >= 0) & (py_d < H)
        img[py_d[mask], np.where(mask)[0]] = dirt_rgb

    for x in range(W):
        py = py_surf[x]
        if 0 <= py < H:
            blk = "grass_block" if surf[x] >= SEA_Y else "sand"
            img[py, x] = BLOCK_COLORS.get(blk, c("grass_block"))

    water_rgb = c("water")
    for x in range(W):
        if surf[x] < SEA_Y:
            py_w_bot = py_surf[x] - 1
            if 0 <= PY_SEA <= py_w_bot < H:
                img[PY_SEA:py_w_bot + 1, x] = water_rgb

    if 0 <= PY_SEA < H:
        img[PY_SEA, :] = (0, 204, 255)

    return np.concatenate([img, np.full((H, W, 1), 255, np.uint8)], axis=2)


def make_xsec_rgba(state: dict, z_row: int, band_scale_y: int,
                   cliff_deg_thr: float) -> np.ndarray:
    """
    Render a cross-section for the given Z row.
    Returns (H, W, 4) uint8 RGBA — no PIL, no external deps.
    H = Y_RANGE = 512 (1 px per MC block); W = TILE_PX = 512.
    Pixel py = (Y_RANGE-1) - (mc_y - Y_MIN)  →  py=0 is Y_MAX-1, py=511 is Y_MIN.
    """
    sy      = state["surface_y"]
    bg      = state["biome_grid"]
    sb      = state["surface_blk"]
    col_off = state["col_off"]
    row_off = state["row_off"]

    W = TILE_PX
    H = Y_RANGE   # 512; y_scale = 1.0 exactly

    gy, gx   = np.gradient(sy.astype(np.float32))
    cliff_deg = np.degrees(np.arctan(np.hypot(gx, gy)))

    surf  = sy[z_row].astype(np.int32)          # (W,)
    biome = bg[z_row]
    sblk  = sb[z_row]
    cliff = (cliff_deg[z_row] >= cliff_deg_thr) & (surf > SEA_Y)

    # py(mc_y) = (H-1+Y_MIN) - mc_y  =  447 - mc_y
    # → py_surf[x] = 447 - surf[x]
    py_surf = (H - 1 + Y_MIN) - surf            # (W,) — may be negative (above image top)
    PY_SEA  = (H - 1 + Y_MIN) - SEA_Y          # = 384  (constant)

    def c(name, default=(96,32,128)):
        return np.array(BLOCK_COLORS.get(name, default), dtype=np.uint8)

    # Start: fill everything with air
    img = np.full((H, W, 3), c("air", (24,56,96)), dtype=np.uint8)

    # ── Bedrock (bottom row) ──────────────────────────────────────────────────
    img[H-1, :] = c("bedrock")

    # ── Stone fill (vectorised for non-cliff columns) ─────────────────────────
    stone_rgb = c("stone")
    for x in range(W):
        s      = surf[x]
        py_top = py_surf[x] + 3          # mc_y = s-3  (exclusive: dirt above)
        py_bot = H - 2                   # mc_y = Y_MIN+1
        if py_top > py_bot or py_top >= H:
            continue
        py_top = max(0, py_top)
        if cliff[x]:
            wx = col_off + x
            wz = row_off + z_row
            for py in range(py_top, py_bot + 1):
                mc_y = (H - 1 + Y_MIN) - py      # inverse: mc_y = 447 - py
                blk  = _banded_stone(str(biome[x]), wx, wz, mc_y, band_scale_y)
                img[py, x] = BLOCK_COLORS.get(blk, stone_rgb)
        else:
            img[py_top:py_bot+1, x] = stone_rgb

    # ── Dirt (2 px below surface) ─────────────────────────────────────────────
    dirt_rgb = c("dirt")
    for d in (1, 2):
        py_d = py_surf + d
        mask = (py_d >= 0) & (py_d < H)
        img[py_d[mask], np.where(mask)[0]] = dirt_rgb

    # ── Surface blocks ────────────────────────────────────────────────────────
    for x in range(W):
        py = py_surf[x]
        if 0 <= py < H:
            img[py, x] = BLOCK_COLORS.get(str(sblk[x]), (96,32,128))

    # ── Water ─────────────────────────────────────────────────────────────────
    water_rgb = c("water")
    for x in range(W):
        s = surf[x]
        if s < SEA_Y:
            py_w_top = PY_SEA
            py_w_bot = py_surf[x] - 1           # mc_y = s+1
            if 0 <= py_w_top <= py_w_bot < H:
                img[py_w_top:py_w_bot+1, x] = water_rgb

    # ── Sea-level line ────────────────────────────────────────────────────────
    if 0 <= PY_SEA < H:
        img[PY_SEA, :] = (0, 204, 255)

    alpha = np.full((H, W, 1), 255, dtype=np.uint8)
    return np.concatenate([img, alpha], axis=2)


# ---------------------------------------------------------------------------
# Inspector image rendering helpers  (Tool C)
# ---------------------------------------------------------------------------
def _fast_height_preview(tx: int, tz: int, size: int = 256) -> Optional[np.ndarray]:
    """
    Load a tile height window directly from rasterio, downsampled to `size` px.
    Fast path — no pipeline needed. Returns (size, size, 4) RGBA or None.
    """
    try:
        import rasterio
        from rasterio.windows import Window
        from rasterio.enums import Resampling
        with rasterio.open(str(MASKS_DIR / "height.tif")) as src:
            data = src.read(
                1,
                window=Window(tx * TILE_PX, tz * TILE_PX, TILE_PX, TILE_PX),
                out_shape=(size, size),
                resampling=Resampling.average,
            )
        return _h_to_rgba(data)
    except Exception as e:
        print(f"[fast_height_preview] {e}")
        return None


def _render_inspect_preview(state: dict, mode: str, size: int = 256) -> np.ndarray:
    """
    Render a top-down 2D preview of the tile in the given mode.
    mode: 'height' | 'biome' | 'slope' | 'surface'
    Returns (size, size, 4) RGBA uint8.  Vectorized — no Python pixel loops.
    """
    step = max(1, TILE_PX // size)

    if mode == "height":
        h = state["masks"]["height"]
        return _h_to_rgba(h[::step, ::step][:size, :size])

    out = np.zeros((size, size, 4), dtype=np.uint8)
    out[:, :, 3] = 255

    if mode == "biome":
        bg_ds = state["biome_grid"][::step, ::step][:size, :size]
        out[:, :, :3] = (128, 64, 192)
        for biome_name, rgb in BIOME_COLORS.items():
            mask = bg_ds == biome_name
            if mask.any():
                out[:, :, 0][mask] = rgb[0]
                out[:, :, 1][mask] = rgb[1]
                out[:, :, 2][mask] = rgb[2]
        return out

    if mode == "slope":
        sl_ds = state["masks"]["slope"][::step, ::step][:size, :size]
        v = (sl_ds * 255).clip(0, 255).astype(np.uint8)
        out[:, :, 0] = v
        out[:, :, 1] = (255 - v)
        out[:, :, 2] = 80
        return out

    if mode == "surface":
        sb_ds = state["surface_blk"][::step, ::step][:size, :size]
        out[:, :, :3] = (96, 32, 128)
        for blk_name, rgb in BLOCK_COLORS.items():
            mask = sb_ds == blk_name
            if mask.any():
                out[:, :, 0][mask] = rgb[0]
                out[:, :, 1][mask] = rgb[1]
                out[:, :, 2][mask] = rgb[2]
        return out

    return out


# ---------------------------------------------------------------------------
# Isometric renderer  (numpy, vectorized — no side panels, top-face + hillshade)
# ---------------------------------------------------------------------------

def render_isometric(
    h_f32: np.ndarray,
    surface_blk: Optional[np.ndarray] = None,
    v_scale: float = 0.12,
    h_scale: float = 1.5,
    sun_az: float = 315.0,
    sun_alt: float = 45.0,
) -> np.ndarray:
    """
    2:1 isometric projection. Returns (out_h, out_w, 4) RGBA uint8.
    h_f32: (H, W) float32 [0, 1].  Downsamples 2× internally for speed.
    Painter's algorithm (back-to-front argsort) — fully vectorized.
    """
    DS = 2
    h = h_f32[::DS, ::DS]
    Hd, Wd = h.shape

    # ── Base colors ──────────────────────────────────────────────────────────
    if surface_blk is not None:
        sb = surface_blk[::DS, ::DS]
        base = np.full((Hd, Wd, 3), (96, 32, 128), dtype=np.float32)
        for blk_name, rgb in BLOCK_COLORS.items():
            mask = sb == blk_name
            if mask.any():
                base[mask] = rgb
    else:
        base = _h_to_rgba((h * 65535).astype(np.uint16))[:, :, :3].astype(np.float32)

    # ── Hillshade the top face ────────────────────────────────────────────────
    idy, idx = np.gradient(h * Y_RANGE * h_scale)
    mag  = np.sqrt(idx**2 + idy**2 + 1.0)
    nx   = -idx / mag;  ny = -idy / mag;  nz = 1.0 / mag
    az_r = np.radians(sun_az);  alt_r = np.radians(sun_alt)
    lx   = np.cos(alt_r) * np.sin(az_r)   # east
    ly   = np.cos(alt_r) * np.cos(az_r)   # north
    lz   = np.sin(alt_r)
    illum = np.clip(nx*lx + ny*ly + nz*lz, 0.08, 1.0).astype(np.float32)
    top_rgb = (base * illum[:, :, None]).clip(0, 255).astype(np.uint8)

    # ── Isometric projection ──────────────────────────────────────────────────
    # 2:1 standard: iso_x = (col - row)*2 + offset, iso_y = (col + row) - h_px
    h_px = np.round(h * Y_RANGE * h_scale * v_scale).astype(np.int32)

    rows, cols = np.mgrid[0:Hd, 0:Wd]
    # iso_x: 0 at (row=Hd-1, col=0), increases right
    iso_x  = (cols - rows + (Hd - 1)) * 2       # range [0 .. 2*(Wd+Hd-2)]
    iso_yg = cols + rows                          # ground row (no height)
    iso_yt = iso_yg - h_px                        # top face row (raised)

    out_w  = 2 * (Wd + Hd - 1) + 2
    # shift iso_yt so minimum ≥ 0
    y_min_raw = int(iso_yt.min())
    y_off  = max(0, -y_min_raw) + 2
    iso_yt = iso_yt + y_off
    out_h  = int(iso_yt.max()) + 4

    bg_r = int(C_BG[1:3], 16)
    bg_g = int(C_BG[3:5], 16)
    bg_b = int(C_BG[5:7], 16)
    canvas = np.empty((out_h, out_w, 4), dtype=np.uint8)
    canvas[:, :, 0] = bg_r;  canvas[:, :, 1] = bg_g
    canvas[:, :, 2] = bg_b;  canvas[:, :, 3] = 255

    # Back-to-front: ascending (row + col)
    order = np.argsort((rows + cols).ravel(), kind='stable')
    ix0   = iso_x.ravel()[order]
    iy    = iso_yt.ravel()[order]
    cr    = top_rgb[:, :, 0].ravel()[order]
    cg    = top_rgb[:, :, 1].ravel()[order]
    cb    = top_rgb[:, :, 2].ravel()[order]

    # Paint 2 px wide per block (fills gaps in 2:1 iso grid)
    for dx in range(2):
        ix    = ix0 + dx
        valid = (ix >= 0) & (ix < out_w) & (iy >= 0) & (iy < out_h)
        canvas[iy[valid], ix[valid], 0] = cr[valid]
        canvas[iy[valid], ix[valid], 1] = cg[valid]
        canvas[iy[valid], ix[valid], 2] = cb[valid]

    return canvas


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

import hashlib

# ---------------------------------------------------------------------------
# Render manifest — tracks per-tile status: none / sim / full / stale
# ---------------------------------------------------------------------------

class RenderManifest:
    """
    Persistent per-tile render status written to output/render_manifest.json.
    Statuses: 'none' | 'sim' | 'full' | 'stale'

    Granular stale detection:
      - _global_hash covers biome-assignment keys (terrain_class, hydrology,
        moisture, sea_level_16bit, biome_patch_noise, terrain_spline).
        A change here marks ALL tiles stale.
      - _sparse_hashes covers per-biome sparse_overrides entries.
        A change here marks only tiles whose stored biome_names intersect.
      - _surface_hash covers everything else (surface appearance).
        A change here marks all tiles stale (conservative).
    """
    _STATUS_COLORS = {
        "sim":   (100, 160, 255, 55),   # blue tint
        "full":  (80,  220, 100, 65),   # green tint
        "stale": (245, 160,  50, 55),   # amber tint
    }
    # Keys that affect biome assignment globally — change → all tiles stale
    _GLOBAL_KEYS = frozenset({
        "terrain_class", "hydrology", "moisture",
        "sea_level_16bit", "biome_patch_noise", "terrain_spline",
    })

    def __init__(self):
        self._data: dict = {}
        self._load()

    def _load(self):
        if RENDER_MANIFEST.exists():
            try:
                with open(RENDER_MANIFEST) as f:
                    self._data = json.load(f)
            except Exception:
                self._data = {}

    def _save(self):
        try:
            RENDER_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
            with open(RENDER_MANIFEST, "w") as f:
                json.dump(self._data, f)
        except Exception as e:
            print(f"[RenderManifest] save failed: {e}")

    @staticmethod
    def _hash(obj) -> str:
        return hashlib.md5(
            json.dumps(obj, sort_keys=True).encode()).hexdigest()[:8]

    @classmethod
    def _global_hash(cls, cfg: dict) -> str:
        return cls._hash({k: cfg[k] for k in cls._GLOBAL_KEYS if k in cfg})

    @classmethod
    def _surface_hash(cls, cfg: dict) -> str:
        return cls._hash({k: v for k, v in cfg.items()
                          if k not in cls._GLOBAL_KEYS and k != "sparse_overrides"})

    @classmethod
    def _sparse_hashes(cls, cfg: dict) -> dict:
        """Per-biome hash from sparse_overrides section."""
        return {b: cls._hash(v)
                for b, v in cfg.get("sparse_overrides", {}).items()}

    def get_status(self, tx: int, tz: int) -> str:
        return self._data.get(f"{tx},{tz}", {}).get("status", "none")

    def set_full(self, tx: int, tz: int, cfg: dict, biome_names: list = None):
        entry = {
            "status":       "full",
            "global_hash":  self._global_hash(cfg),
            "surface_hash": self._surface_hash(cfg),
            "sparse_hashes":self._sparse_hashes(cfg),
        }
        if biome_names is not None:
            entry["biome_names"] = list(biome_names)
        self._data[f"{tx},{tz}"] = entry
        self._save()

    def set_sim(self, tx: int, tz: int, cfg: dict, biome_names: list = None):
        entry = self._data.get(f"{tx},{tz}", {})
        if entry.get("status") == "full":
            return  # don't downgrade full → sim
        new_entry = {
            "status":       "sim",
            "global_hash":  self._global_hash(cfg),
            "surface_hash": self._surface_hash(cfg),
            "sparse_hashes":self._sparse_hashes(cfg),
        }
        if biome_names is not None:
            new_entry["biome_names"] = list(biome_names)
        self._data[f"{tx},{tz}"] = new_entry
        self._save()

    def mark_config_change(self, cfg: dict):
        """Granular stale marking — only mark tiles actually affected by the change."""
        new_global  = self._global_hash(cfg)
        new_surface = self._surface_hash(cfg)
        new_sparse  = self._sparse_hashes(cfg)
        changed = False
        for key, val in self._data.items():
            if val.get("status") not in ("full", "sim"):
                continue
            # Global biome-assignment keys changed → always stale
            if val.get("global_hash") != new_global:
                val["status"] = "stale"; changed = True; continue
            # Surface appearance changed → stale
            if val.get("surface_hash") != new_surface:
                val["status"] = "stale"; changed = True; continue
            # Per-biome sparse_overrides changed → stale only if biome present
            stored_biomes = set(val.get("biome_names", []))
            for biome, new_h in new_sparse.items():
                old_h = val.get("sparse_hashes", {}).get(biome)
                if old_h != new_h and biome in stored_biomes:
                    val["status"] = "stale"; changed = True; break
        if changed:
            self._save()

    def status_color(self, tx: int, tz: int) -> Optional[tuple]:
        """Return (r,g,b,a) overlay color or None for untouched tiles."""
        return self._STATUS_COLORS.get(self.get_status(tx, tz))


# ---------------------------------------------------------------------------
# Annotation store  (Tool G)
# ---------------------------------------------------------------------------

class AnnotationStore:
    """
    Persistent per-tile notes stored in output/annotations.json.
    Severity: 'info' | 'warn' | 'error'
    """
    _PATH = PROJECT_ROOT / "output" / "annotations.json"
    SEVERITIES  = ["info", "warn", "error"]
    SEV_COLORS  = {
        "info":  (100, 160, 255),
        "warn":  (245, 160,  50),
        "error": (248, 113, 113),
    }
    SEV_LABELS  = {"info": "ℹ", "warn": "⚠", "error": "✖"}

    def __init__(self):
        self._data: list[dict] = []
        self._load()

    def _load(self):
        if self._PATH.exists():
            try:
                with open(self._PATH) as f:
                    self._data = json.load(f)
            except Exception:
                self._data = []

    def _save(self):
        try:
            self._PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self._PATH, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            print(f"[AnnotationStore] {e}")

    def add(self, tx: int, tz: int, text: str, severity: str = "info"):
        import time
        self._data.append({"tx": tx, "tz": tz, "text": text,
                           "severity": severity, "resolved": False,
                           "ts": int(time.time())})
        self._save()

    def all(self) -> list[dict]:
        return list(self._data)

    def toggle_resolve(self, idx: int):
        if 0 <= idx < len(self._data):
            self._data[idx]["resolved"] = not self._data[idx]["resolved"]
            self._save()

    def delete(self, idx: int):
        if 0 <= idx < len(self._data):
            del self._data[idx]
            self._save()


# ---------------------------------------------------------------------------
# River extraction worker  (Phase C — procedural)
# ---------------------------------------------------------------------------

HYDRO_RES = 2048   # overlay resolution — high enough to show individual rivers

class HydroOverlayLoader(QThread):
    """
    Loads precomputed hydrology masks at high resolution (HYDRO_RES × HYDRO_RES)
    and builds a crisp RGBA overlay image with rivers as thin lines (color-coded
    by Strahler order, width-scaled) and lakes as filled shapes.

    Also loads a GRID_N × GRID_N version for tile-level hover info.
    """
    done     = pyqtSignal(object)   # dict {image, order_tile, ...} | None
    progress = pyqtSignal(str)

    def run(self):
        try:
            import rasterio, warnings
            from rasterio.enums import Resampling
            from scipy.ndimage import maximum_filter, binary_dilation

            self.progress.emit("Loading hydrology masks at high resolution…")

            def _read(name, size, resamp=Resampling.nearest):
                p = MASKS_DIR / f"{name}.tif"
                if not p.exists():
                    return None
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    with rasterio.open(str(p)) as ds:
                        return ds.read(1, out_shape=(size, size),
                                       resampling=resamp)

            # High-res for rendering
            order_hr = _read("hydro_order", HYDRO_RES)
            width_hr = _read("hydro_width", HYDRO_RES)
            lake_hr  = _read("hydro_lake",  HYDRO_RES)
            lkdep_hr = _read("hydro_lkdep", HYDRO_RES)

            if order_hr is None:
                self.progress.emit("No hydrology masks found — run hydrology_precompute.py first")
                self.done.emit(None)
                return

            # Tile-res for hover info
            order_tile = _read("hydro_order", GRID_N)
            width_tile = _read("hydro_width", GRID_N)
            lake_tile  = _read("hydro_lake",  GRID_N)
            lkdep_tile = _read("hydro_lkdep", GRID_N)

            order = order_hr.astype(np.uint8)
            width = width_hr.astype(np.uint8) if width_hr is not None else np.zeros_like(order)
            lake  = lake_hr.astype(np.uint16) if lake_hr is not None else np.zeros((HYDRO_RES, HYDRO_RES), dtype=np.uint16)
            lkdep = lkdep_hr.astype(np.uint8) if lkdep_hr is not None else np.zeros_like(order)

            river_px = int((order > 0).sum())
            lake_px  = int((lake > 0).sum())
            max_order = int(order.max())
            n_lakes  = int(lake.max())

            self.progress.emit(
                f"Building hydro overlay ({HYDRO_RES}×{HYDRO_RES}): "
                f"{river_px} river px, {lake_px} lake px…")

            # ── Build RGBA overlay at HYDRO_RES ──────────────────────────────
            rgba = np.zeros((HYDRO_RES, HYDRO_RES, 4), dtype=np.uint8)

            # Scale for how many overlay pixels per tile
            px_per_tile = HYDRO_RES / GRID_N  # ≈ 21.1

            # Strahler order → base color (R, G, B)
            ORDER_RGB = {
                1: (80, 150, 230),     # steel blue
                2: (60, 170, 245),     # medium blue
                3: (40, 190, 255),     # bright blue
                4: (25, 210, 255),     # cyan-blue
                5: (15, 225, 255),     # near-cyan
            }

            # Width expansion: at HYDRO_RES, 1 source pixel ≈ 0.04 tiles.
            # Width in blocks / 512 blocks_per_tile * px_per_tile gives
            # the pixel radius at overlay scale.  We dilate the centerline
            # to approximate this, capped to keep performance sane.
            centerline = order > 0
            if centerline.any():
                # For each order level, dilate by proportional width
                for o in range(max_order, 0, -1):
                    o_mask = order == o
                    if not o_mask.any():
                        continue

                    # Median width for this order
                    o_widths = width[o_mask]
                    median_w = int(np.median(o_widths)) if len(o_widths) > 0 else 2

                    # Convert block width to overlay pixel radius
                    # 1 block = 1/512 tiles = px_per_tile/512 overlay pixels
                    radius = max(1, int(median_w * px_per_tile / 512 * 1.5))
                    radius = min(radius, 4)  # cap dilation

                    if radius > 1:
                        expanded = binary_dilation(o_mask, iterations=radius - 1)
                    else:
                        expanded = o_mask

                    r, g, b = ORDER_RGB.get(o, (15, 225, 255))
                    # Higher order = more opaque
                    alpha = min(140 + o * 25, 240)
                    rgba[expanded, 0] = r
                    rgba[expanded, 1] = g
                    rgba[expanded, 2] = b
                    rgba[expanded, 3] = alpha

            # Paint lakes (deep blue fill)
            lake_mask = lake > 0
            if lake_mask.any():
                lake_alpha = np.clip(140 + lkdep.astype(np.int32) * 5, 140, 220)
                rgba[lake_mask, 0] = 20
                rgba[lake_mask, 1] = 80
                rgba[lake_mask, 2] = 170
                rgba[lake_mask, 3] = lake_alpha[lake_mask].astype(np.uint8)

            # Convert to QImage
            from PyQt6.QtGui import QImage
            h, w = rgba.shape[:2]
            img = QImage(rgba.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()

            self.progress.emit(
                f"Hydrology overlay ready: {river_px} river px (max order {max_order}), "
                f"{n_lakes} lakes")

            self.done.emit({
                "image": img,
                "order_tile": order_tile.astype(np.uint8) if order_tile is not None else None,
                "width_tile": width_tile.astype(np.uint8) if width_tile is not None else None,
                "lake_tile":  lake_tile.astype(np.uint16) if lake_tile is not None else None,
                "lkdep_tile": lkdep_tile.astype(np.uint8) if lkdep_tile is not None else None,
                "river_px": river_px,
                "lake_px":  lake_px,
                "max_order": max_order,
                "n_lakes": n_lakes,
            })

        except Exception:
            self.done.emit(None)
            import traceback; traceback.print_exc()


class HydroOverlayStore:
    """
    Holds the loaded hydrology overlay data for the world map view.
    Replaces RiverSketchStore — no user-drawn rivers, everything comes
    from the precomputed hydro masks.
    """
    def __init__(self):
        self.order: np.ndarray | None = None   # (GRID_N, GRID_N) uint8  — tile-level
        self.width: np.ndarray | None = None   # (GRID_N, GRID_N) uint8
        self.lake:  np.ndarray | None = None   # (GRID_N, GRID_N) uint16
        self.lkdep: np.ndarray | None = None   # (GRID_N, GRID_N) uint8
        self.image: object     | None = None   # QImage RGBA overlay (HYDRO_RES)
        self.loaded: bool = False
        self.stats: dict  = {}

    def set_data(self, data: dict) -> None:
        """Called when HydroOverlayLoader finishes."""
        self.order = data.get("order_tile")
        self.width = data.get("width_tile")
        self.lake  = data.get("lake_tile")
        self.lkdep = data.get("lkdep_tile")
        self.image = data["image"]
        self.loaded = True
        self.stats = {
            "river_px":  data.get("river_px", 0),
            "lake_px":   data.get("lake_px", 0),
            "max_order": data.get("max_order", 0),
            "n_lakes":   data.get("n_lakes", 0),
        }

    def info_at(self, tx: int, tz: int) -> str:
        """Return hydrology info string for a tile coordinate (for hover tooltip)."""
        if not self.loaded or self.order is None:
            return ""
        if not (0 <= tx < GRID_N and 0 <= tz < GRID_N):
            return ""
        o = int(self.order[tz, tx])
        w = int(self.width[tz, tx]) if self.width is not None else 0
        lk = int(self.lake[tz, tx]) if self.lake is not None else 0
        ld = int(self.lkdep[tz, tx]) if self.lkdep is not None else 0
        parts = []
        if o > 0:
            parts.append(f"river order {o}, width {w}blk")
        if lk > 0:
            parts.append(f"lake #{lk}, depth {ld}blk")
        return " | ".join(parts)


def _start_thread(thread: QThread) -> QThread:
    """Start a QThread.  Caller must store it as an instance variable to
    prevent Python GC from collecting it while the C++ thread is running."""
    thread.start()
    return thread


class OverviewLoader(QThread):
    """Load a high-res height overview with hillshade for satellite-like clarity."""
    done = pyqtSignal(object)   # emits QPixmap or None

    def run(self):
        try:
            import rasterio
            from rasterio.enums import Resampling

            with rasterio.open(str(MASKS_DIR / "height.tif")) as src:
                data = src.read(1, out_shape=(OVERVIEW_RES, OVERVIEW_RES),
                                resampling=Resampling.average)

            rgba = _h_to_rgba(data)

            # Add hillshade for satellite-like depth
            h_f32 = data.astype(np.float32)
            if h_f32.max() > 1.0:
                h_f32 /= 65535.0
            dy, dx = np.gradient(h_f32)
            # Directional light from NW (azimuth=315°, altitude=45°)
            shade = (-dx + dy) / (np.sqrt(dx*dx + dy*dy + 0.04) * 1.414)
            shade = (shade * 0.5 + 0.5)  # [0, 1]
            shade = np.clip(shade, 0.15, 0.95)

            # Blend: darken terrain by hillshade
            for c in range(3):
                rgba[:, :, c] = np.clip(
                    rgba[:, :, c].astype(np.float32) * shade * 1.1,
                    0, 255
                ).astype(np.uint8)

            self.done.emit(_rgba_to_qpixmap(rgba))
        except Exception as e:
            print(f"[OverviewLoader] {e}")
            self.done.emit(None)


class BiomeOverviewLoader(QThread):
    """Load output/world_biome_map.png, crop to map area, build 97×97 biome-name grid."""
    done = pyqtSignal(object, object)   # QPixmap or None, list[list[str]] or None

    def run(self):
        try:
            from PIL import Image
            path = PROJECT_ROOT / "output" / "world_biome_map.png"
            if not path.exists():
                self.done.emit(None, None)
                return
            img = Image.open(path).convert("RGB")
            w, h = img.size
            cell = w // GRID_N          # pixels per tile column
            map_img = img.crop((0, 0, w, cell * GRID_N))

            # Reverse-colour → biome name lookup
            reverse: dict[tuple, str] = {v: k for k, v in BIOME_COLORS.items()}
            arr = np.array(map_img)
            half = cell // 2
            grid: list[list[str]] = []
            for tz in range(GRID_N):
                row: list[str] = []
                for tx in range(GRID_N):
                    rgb = tuple(arr[tz * cell + half, tx * cell + half, :3].tolist())
                    name = reverse.get(rgb)
                    if name is None:
                        best, best_d = "_OCEAN", float("inf")
                        for bn, bc in BIOME_COLORS.items():
                            d = (rgb[0]-bc[0])**2 + (rgb[1]-bc[1])**2 + (rgb[2]-bc[2])**2
                            if d < best_d:
                                best_d, best = d, bn
                        name = best
                    row.append(name)
                grid.append(row)

            from PyQt6.QtGui import QImage as _QImage
            qimg = _QImage(map_img.tobytes(), map_img.width, map_img.height,
                           map_img.width * 3, _QImage.Format.Format_RGB888)
            self.done.emit(QPixmap.fromImage(qimg), grid)
        except Exception as e:
            print(f"[BiomeOverviewLoader] {e}")
            self.done.emit(None, None)


class TileThumbLoader(QThread):
    """Load per-tile thumbnails from rasterio windows for LOD display."""
    thumb_ready = pyqtSignal(int, int, object)  # tx, tz, QPixmap

    def __init__(self, tiles: list[tuple[int, int]], sz: int = THUMB_LOD1_SZ):
        super().__init__()
        self.tiles = tiles
        self.sz    = sz

    def run(self):
        try:
            import rasterio
            from rasterio.windows import Window
            from rasterio.enums import Resampling
            HILLSHADE_CACHE.mkdir(parents=True, exist_ok=True)
            with rasterio.open(str(MASKS_DIR / "height.tif")) as src:
                for tx, tz in self.tiles:
                    cache_path = HILLSHADE_CACHE / f"{tx}_{tz}_{self.sz}.png"
                    if cache_path.exists():
                        pm = QPixmap(str(cache_path))
                        if not pm.isNull():
                            self.thumb_ready.emit(tx, tz, pm)
                            continue
                    try:
                        data = src.read(
                            1,
                            window=Window(tx * TILE_PX, tz * TILE_PX, TILE_PX, TILE_PX),
                            out_shape=(self.sz, self.sz),
                            resampling=Resampling.average,
                        )
                        rgba = _h_to_rgba(data)
                        pm = _rgba_to_qpixmap(rgba)
                        pm.save(str(cache_path))
                        self.thumb_ready.emit(tx, tz, pm)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[TileThumbLoader] {e}")


class FastPreviewLoader(QThread):
    """Load a single tile height preview quickly (no pipeline)."""
    done = pyqtSignal(object)  # QPixmap or None

    def __init__(self, tx: int, tz: int, size: int = 256):
        super().__init__()
        self.tx   = tx
        self.tz   = tz
        self.size = size

    def run(self):
        rgba = _fast_height_preview(self.tx, self.tz, self.size)
        if rgba is not None:
            self.done.emit(_rgba_to_qpixmap(rgba))
        else:
            self.done.emit(None)



class GenerateWorker(QThread):
    """Run the full pipeline in a background thread."""
    progress = pyqtSignal(str)
    finished = pyqtSignal(dict)
    error    = pyqtSignal(str)

    def __init__(self, tx: int, tz: int, cfg: dict):
        super().__init__()
        self.tx  = tx
        self.tz  = tz
        self.cfg = cfg

    def run(self):
        try:
            result = run_pipeline(
                self.tx, self.tz, self.cfg,
                progress_cb=lambda s: self.progress.emit(s))
            self.finished.emit(result)
        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


class SimPreviewWorker(QThread):
    """
    Phase B simulation layer: runs the ACTUAL pipeline surface decoration
    (eco_gradients + noise_layers_biome + decorate_surface) so World Studio
    shows exactly what the pipeline produces.

    Emits dict with h_norm, biome_rgb, surface_blk_rgb (block-color view),
    biome_grid, surface_blk grid, override_8bit, flow_norm.
    """
    done     = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, tx: int, tz: int, cfg: dict):
        super().__init__()
        self.tx  = tx
        self.tz  = tz
        self.cfg = cfg

    def run(self):
        try:
            import rasterio, warnings
            from rasterio.windows import Window

            col_off = self.tx * TILE_PX
            row_off = self.tz * TILE_PX
            win = Window(col_off, row_off, TILE_PX, TILE_PX)

            self.progress.emit("Sim: reading masks…")
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")

                def _read_mask(name, dtype_max=65535.0):
                    p = MASKS_DIR / f"{name}.tif"
                    if not p.exists():
                        return np.zeros((TILE_PX, TILE_PX), dtype=np.float32)
                    with rasterio.open(str(p)) as ds:
                        raw = ds.read(1, window=win).astype(np.float32)
                    return raw / dtype_max if raw.max() > 1.0 else raw

                h_norm       = _read_mask("height")
                slope_f      = _read_mask("slope")
                flow_f       = _read_mask("flow")
                erosion_f    = _read_mask("erosion")
                override_f   = _read_mask("override", 255.0)
                hydro_order  = _read_mask("hydro_order", 255.0)
                hydro_width  = _read_mask("hydro_width", 255.0)
                hydro_lake   = _read_mask("hydro_lake")

                # Read raw height for surface_y computation
                with rasterio.open(str(MASKS_DIR / "height.tif")) as ds:
                    h_raw_u16 = ds.read(1, window=win)
                    if h_raw_u16.dtype != np.uint16:
                        h_raw_u16 = (h_norm * 65535).astype(np.uint16)

            override_8bit = (override_f * 255).round().astype(np.uint8)
            H, W = h_norm.shape
            flow_norm = flow_f

            # ── Biome assignment (from override) ──
            valid_zones = sorted(_OVERRIDE_NAMES.keys())
            lut_name = ["_OCEAN"] * 256
            lut_rgb  = np.zeros((256, 3), dtype=np.uint8)
            lut_rgb[0] = BIOME_COLORS.get("_OCEAN", (30, 80, 160))
            for v in range(1, 256):
                nearest = min(valid_zones, key=lambda z: abs(z - v))
                biome = _OVERRIDE_NAMES[nearest]
                lut_name[v] = biome
                lut_rgb[v] = BIOME_COLORS.get(biome, (128, 128, 128))

            biome_grid = np.empty((H, W), dtype=object)
            for v in np.unique(override_8bit):
                biome_grid[override_8bit == v] = lut_name[int(v)]
            biome_rgb = lut_rgb[override_8bit].astype(np.float32)

            self.progress.emit("Sim: running surface decorator pipeline…")

            # ── Surface Y from height LUT ──
            from core.column_generator import _LUT, SEA_LEVEL
            surface_y = _LUT[h_raw_u16].astype(np.int16)

            # ── Eco gradients ──
            from core.eco_gradients import compute_eco_gradients
            _gy, _gx = np.gradient(surface_y.astype(np.float32))
            cliff_deg = np.degrees(np.arctan(np.hypot(_gx, _gy))).astype(np.float32)
            land_mask = surface_y >= SEA_LEVEL

            eco_grads = compute_eco_gradients(
                surface_y, flow_f, erosion_f, cliff_deg,
                hydro_order, hydro_width, hydro_lake,
                land_mask, self.cfg,
            )

            # ── Surface decoration (the actual pipeline path) ──
            from core.surface_decorator import decorate_surface
            from core.noise_fields import load_noise_generators

            noise = load_noise_generators(CONFIG_PATH)
            river_meta = np.zeros((H, W), dtype=np.uint8)  # simplified — no carving in preview

            surface_blk, sub_blk, ground_cover = decorate_surface(
                surface_y, biome_grid,
                erosion_f, flow_f, h_norm,
                river_meta, flow_f,
                noise, self.cfg,
                self.tx, self.tz,
                eco_grads=eco_grads,
                cliff_deg=cliff_deg,
            )

            # ── Colorize surface blocks + ground cover overlay ──
            self.progress.emit("Sim: colorizing surface + vegetation…")
            surface_blk_rgb = np.full((H, W, 3), 128, dtype=np.uint8)
            for blk_name in np.unique(surface_blk):
                blk_str = str(blk_name)
                color = _BLOCK_COLORS.get(blk_str, _BLOCK_COLORS["default"])
                surface_blk_rgb[surface_blk == blk_name] = color

            # Overlay ground cover where present — blend 60% surface + 40% vegetation
            has_cover = ground_cover != ""
            if has_cover.any():
                cover_rgb = np.zeros((H, W, 3), dtype=np.uint8)
                for blk_name in np.unique(ground_cover):
                    if not blk_name or str(blk_name) == "":
                        continue
                    blk_str = str(blk_name)
                    color = _BLOCK_COLORS.get(blk_str, _BLOCK_COLORS["default"])
                    cover_rgb[ground_cover == blk_name] = color
                # Blend: surface base with vegetation tint
                blended = (surface_blk_rgb[has_cover].astype(np.float32) * 0.55
                         + cover_rgb[has_cover].astype(np.float32) * 0.45)
                surface_blk_rgb[has_cover] = np.clip(blended, 0, 255).astype(np.uint8)

            self.done.emit({
                "h_norm":           h_norm,
                "biome_rgb":        biome_rgb,
                "biome_grid":       biome_grid,
                "override_8bit":    override_8bit,
                "flow_norm":        flow_norm,
                "surface_blk_rgb":  surface_blk_rgb.astype(np.float32),
                "surface_blk":      surface_blk,
            })
        except Exception as e:
            import traceback
            print(f"[SimPreviewWorker] {e}\n{traceback.format_exc()}")
            self.done.emit(None)


class MaskHistogramLoader(QThread):
    """
    Sample each mask TIF at 512×512 resolution for histogram display.
    Runs once at startup; result is passed to ConfigPanel.
    """
    done = pyqtSignal(dict)   # key → np.ndarray float32 [0,1] flattened

    def run(self):
        try:
            import rasterio
            from rasterio.enums import Resampling
            SZ = 512
            result = {}
            for key, fname in [("slope",   "slope.tif"),
                                ("height",  "height.tif"),
                                ("erosion", "erosion.tif"),
                                ("flow",    "flow.tif")]:
                path = MASKS_DIR / fname
                if not path.exists():
                    continue
                with rasterio.open(str(path)) as src:
                    data = src.read(1, out_shape=(SZ, SZ),
                                    resampling=Resampling.average).astype(np.float32)
                if data.max() > 1.0:
                    data = data / 65535.0
                result[key] = data.ravel()
            self.done.emit(result)
        except Exception as e:
            print(f"[MaskHistogramLoader] {e}")
            self.done.emit({})


class XSectionWorker(QThread):
    """Render a cross-section in a background thread — no PIL, numpy only."""
    finished = pyqtSignal(object)   # QPixmap
    error    = pyqtSignal(str)

    def __init__(self, state: dict, z_row: int, bsy: int, thr: float):
        super().__init__()
        self.state = state
        self.z_row = z_row
        self.bsy   = bsy
        self.thr   = thr

    def run(self):
        try:
            rgba = make_xsec_rgba(self.state, self.z_row, self.bsy, self.thr)
            self.finished.emit(_rgba_to_qpixmap(rgba))
        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Tool A — World Map  (zoomable 97×97 QGraphicsView)
# ---------------------------------------------------------------------------

class WorldMapView(QGraphicsView):
    """
    Zoomable, pannable 97×97 tile grid showing the full 50k world.
    Left-click tile to select; right-click for context menu.
    LOD thumbnails loaded on demand at two zoom levels.
    """
    tile_selected        = pyqtSignal(int, int)   # (tx, tz)
    tile_hovered         = pyqtSignal(int, int)
    annotation_requested = pyqtSignal(int, int)   # right-click → add note

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(0, 0, GRID_N, GRID_N)
        self.setScene(self._scene)
        self.setBackgroundBrush(QBrush(QColor(C_BG)))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)

        # Overview — one 97×97 pixmap stretched to fill the scene
        self._bg_item: Optional[QGraphicsPixmapItem] = None
        self._height_pixmap: Optional[QPixmap] = None
        self._biome_pixmap:  Optional[QPixmap] = None

        # Per-tile thumbnails — dict (tx, tz) → QGraphicsPixmapItem
        self._thumb_items:  dict[tuple, QGraphicsPixmapItem] = {}
        self._thumb_cache:  dict[tuple, QPixmap]             = {}
        self._thumb_loading: set[tuple]                      = set()
        self._thumb_loader: Optional[TileThumbLoader]        = None

        # Selection rect drawn with amber C_SEL pen
        self._sel_rect: Optional[QGraphicsRectItem]   = None
        self._selected: Optional[tuple[int, int]]     = None

        # Layer mode — "height" or "biome"
        self._layer = "height"

        # LOD debounce timer
        self._lod_timer = QTimer(self)
        self._lod_timer.setSingleShot(True)
        self._lod_timer.timeout.connect(self._load_visible_thumbs)

        self._grid_alpha  = 60
        self._manifest:     Optional[RenderManifest]   = None
        self._annotations:  Optional[AnnotationStore]  = None
        self._hydro:        Optional[HydroOverlayStore] = None
        self._hydro_visible = True
        self._hydro_pixmap: Optional[QGraphicsPixmapItem] = None

        self.setMinimumSize(380, 380)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_overview(self, pixmap: QPixmap):
        """Install the height overview and fit view."""
        self._height_pixmap = pixmap
        self._show_overview(pixmap)
        self.fitInView(QRectF(0, 0, GRID_N, GRID_N), Qt.AspectRatioMode.KeepAspectRatio)

    def set_biome_overview(self, pixmap: QPixmap):
        """Store biome overview (shown on swap_layer('biome'))."""
        self._biome_pixmap = pixmap

    def swap_layer(self, layer: str):
        """Switch background between height and biome overview without resetting zoom."""
        pm = self._biome_pixmap if (layer == "biome" and self._biome_pixmap) else self._height_pixmap
        if pm:
            self._show_overview(pm)

    def _show_overview(self, pixmap: QPixmap):
        if self._bg_item:
            self._scene.removeItem(self._bg_item)
            self._bg_item = None
        if pixmap:
            self._bg_item = QGraphicsPixmapItem(pixmap)
            self._bg_item.setZValue(0)
            self._bg_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
            self._bg_item.setScale(GRID_N / pixmap.width())
            self._scene.addItem(self._bg_item)

    def select_tile_external(self, tx: int, tz: int):
        """Programmatically select a tile (e.g. from CLI args)."""
        self._select_tile(tx, tz)

    def set_grid_alpha(self, v: int):
        """Set grid line opacity 0–100."""
        self._grid_alpha = v
        self.scene().update()

    def set_manifest(self, manifest: "RenderManifest"):
        self._manifest = manifest
        self.scene().update()

    def set_annotations(self, store: "AnnotationStore"):
        self._annotations = store
        self.scene().update()

    def set_hydro(self, store: "HydroOverlayStore"):
        """Install hydrology overlay store."""
        self._hydro = store

    def set_hydro_visible(self, visible: bool):
        """Toggle hydrology overlay visibility."""
        self._hydro_visible = visible
        self._update_hydro_overlay()

    def _update_hydro_overlay(self):
        """Rebuild or hide the hydro overlay pixmap item."""
        # Remove old
        if self._hydro_pixmap:
            self._scene.removeItem(self._hydro_pixmap)
            self._hydro_pixmap = None

        if (self._hydro_visible and self._hydro
                and self._hydro.loaded and self._hydro.image):
            from PyQt6.QtGui import QPixmap
            pm = QPixmap.fromImage(self._hydro.image)
            self._hydro_pixmap = QGraphicsPixmapItem(pm)
            self._hydro_pixmap.setZValue(10)  # above LOD tiles (z=5), below grid/selection
            # Scale: image is HYDRO_RES×HYDRO_RES, scene is GRID_N×GRID_N
            self._hydro_pixmap.setScale(float(GRID_N) / pm.width())
            self._hydro_pixmap.setTransformationMode(
                Qt.TransformationMode.SmoothTransformation)
            self._scene.addItem(self._hydro_pixmap)
        self.scene().update()

    # ------------------------------------------------------------------
    # Mouse events
    # ------------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.18 if event.angleDelta().y() > 0 else 1.0 / 1.18
        self.scale(factor, factor)
        t = self.transform().m11()
        tile_px = t * (self.viewport().width() / GRID_N)
        if tile_px < 1.5:
            self.fitInView(QRectF(0, 0, GRID_N, GRID_N),
                           Qt.AspectRatioMode.KeepAspectRatio)
        elif tile_px > 600:
            self.scale(1.0 / factor, 1.0 / factor)
        self._lod_timer.start(150)

    def mousePressEvent(self, event: QMouseEvent):
        sp = self.mapToScene(event.pos())
        tx, tz = int(sp.x()), int(sp.y())
        in_bounds = 0 <= tx < GRID_N and 0 <= tz < GRID_N
        if event.button() == Qt.MouseButton.LeftButton and in_bounds:
            self._select_tile(tx, tz)
        elif event.button() == Qt.MouseButton.RightButton and in_bounds:
            self._show_context_menu(tx, tz, event.globalPosition().toPoint())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        sp = self.mapToScene(event.pos())
        tx, tz = int(sp.x()), int(sp.y())
        if 0 <= tx < GRID_N and 0 <= tz < GRID_N:
            self.tile_hovered.emit(tx, tz)
        super().mouseMoveEvent(event)

    # ------------------------------------------------------------------
    # Internal tile logic
    # ------------------------------------------------------------------

    def _select_tile(self, tx: int, tz: int):
        self._selected = (tx, tz)
        if self._sel_rect is None:
            pen = QPen(QColor(C_SEL))
            pen.setWidthF(0.10)
            self._sel_rect = QGraphicsRectItem()
            self._sel_rect.setPen(pen)
            self._sel_rect.setBrush(QBrush(QColor(0, 0, 0, 0)))
            self._sel_rect.setZValue(20)
            self._scene.addItem(self._sel_rect)
        self._sel_rect.setRect(QRectF(tx, tz, 1, 1))
        self.tile_selected.emit(tx, tz)

    def _show_context_menu(self, tx: int, tz: int, global_pos):
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu{{background:{C_PANEL};color:{C_TEXT};border:1px solid {C_BORDER};}}"
            f"QMenu::item:selected{{background:{C_ACCENT};color:{C_BG};}}")
        a_inspect  = menu.addAction(f"Inspect tile ({tx}, {tz})")
        a_render3d = menu.addAction("Render 3D")
        menu.addSeparator()
        a_note = menu.addAction(f"Add note at ({tx}, {tz})…")
        action = menu.exec(global_pos)
        if action == a_inspect:
            self._select_tile(tx, tz)
        elif action == a_render3d:
            self._select_tile(tx, tz)
        elif action == a_note:
            self.annotation_requested.emit(tx, tz)
            # Signal handled by parent to switch to 3D tab and trigger render

    # ------------------------------------------------------------------
    # LOD thumbnail management
    # ------------------------------------------------------------------

    def _current_tile_px(self) -> float:
        """Return approximate screen pixels per tile at current zoom."""
        vw = self.viewport().width()
        scene_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        sw = scene_rect.width()
        return (vw / sw) if sw > 0 else 0.0

    def _visible_tile_range(self) -> tuple[int, int, int, int]:
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        tx0 = max(0,         int(vr.x()))
        tz0 = max(0,         int(vr.y()))
        tx1 = min(GRID_N-1,  int(vr.right())  + 1)
        tz1 = min(GRID_N-1,  int(vr.bottom()) + 1)
        return tx0, tz0, tx1, tz1

    def _load_visible_thumbs(self):
        tile_px = self._current_tile_px()
        if tile_px < LOD1_THRESHOLD:
            # Too zoomed out — evict individual thumbs to save memory
            for item in self._thumb_items.values():
                self._scene.removeItem(item)
            self._thumb_items.clear()
            return

        tx0, tz0, tx1, tz1 = self._visible_tile_range()
        sz = THUMB_LOD2_SZ if tile_px >= LOD2_THRESHOLD else THUMB_LOD1_SZ

        # Show already-cached thumbs immediately
        for (tx, tz), pm in list(self._thumb_cache.items()):
            if tx0 <= tx <= tx1 and tz0 <= tz <= tz1:
                self._show_thumb(tx, tz, pm)

        # Queue missing tiles for background load
        to_load = [
            (tx, tz)
            for tz in range(tz0, tz1 + 1)
            for tx in range(tx0, tx1 + 1)
            if (tx, tz) not in self._thumb_cache
            and (tx, tz) not in self._thumb_loading
        ]
        if not to_load:
            return
        self._thumb_loading.update(to_load)
        try:
            if self._thumb_loader and self._thumb_loader.isRunning():
                self._thumb_loader.wait()
        except RuntimeError:
            pass
        self._thumb_loader = None
        self._thumb_loader = TileThumbLoader(to_load, sz)
        self._thumb_loader.thumb_ready.connect(self._on_thumb_ready)
        _start_thread(self._thumb_loader)

    def _on_thumb_ready(self, tx: int, tz: int, pixmap: QPixmap):
        self._thumb_loading.discard((tx, tz))
        self._thumb_cache[(tx, tz)] = pixmap
        # LRU eviction
        if len(self._thumb_cache) > THUMB_CACHE_MAX:
            oldest = next(iter(self._thumb_cache))
            del self._thumb_cache[oldest]
            if oldest in self._thumb_items:
                self._scene.removeItem(self._thumb_items.pop(oldest))
        self._show_thumb(tx, tz, pixmap)

    def _show_thumb(self, tx: int, tz: int, pixmap: QPixmap):
        if (tx, tz) in self._thumb_items:
            return
        item = QGraphicsPixmapItem(pixmap)
        item.setPos(QPointF(tx, tz))
        item.setScale(1.0 / pixmap.width())
        item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        item.setZValue(5)
        self._scene.addItem(item)
        self._thumb_items[(tx, tz)] = item

    # ------------------------------------------------------------------
    # Grid overlay (drawn in foreground — no scene items, very fast)
    # ------------------------------------------------------------------

    def drawForeground(self, painter: QPainter, rect):
        tile_px = self._current_tile_px()
        if tile_px < 3:
            return

        # ── Render-status overlays ────────────────────────────────────────────
        if self._manifest and tile_px >= 4:
            tx0, tz0, tx1, tz1 = self._visible_tile_range()
            for tz in range(tz0, tz1 + 1):
                for tx in range(tx0, tx1 + 1):
                    col = self._manifest.status_color(tx, tz)
                    if col:
                        painter.fillRect(
                            QRectF(tx + 0.05, tz + 0.05, 0.90, 0.90),
                            QColor(*col))

        # ── Hydrology overlay is rendered as a QGraphicsPixmapItem (z=5) ──
        # No foreground painting needed — the overlay is a scene item.

        # ── Annotations ───────────────────────────────────────────────────────
        if self._annotations and tile_px >= 6:
            for ann in self._annotations.all():
                if ann.get("resolved"):
                    continue
                tx2, tz2 = ann["tx"], ann["tz"]
                if not (rect.x() - 2 <= tx2 <= rect.right() + 2 and
                        rect.y() - 2 <= tz2 <= rect.bottom() + 2):
                    continue
                rgb = AnnotationStore.SEV_COLORS.get(ann.get("severity","info"),
                                                     (100,160,255))
                cx, cy = tx2 + 0.5, tz2 + 0.5
                r = 0.35
                painter.setBrush(QBrush(QColor(*rgb, 200)))
                painter.setPen(QPen(QColor(0,0,0,100), 0.04))
                painter.drawEllipse(QPointF(cx, cy), r, r)

        # ── Grid lines ────────────────────────────────────────────────────────
        if self._grid_alpha == 0:
            return
        base_alpha = 30 if tile_px < 15 else 60
        alpha = max(0, min(255, int(base_alpha * self._grid_alpha / 100)))
        if alpha == 0:
            return
        pen = QPen(QColor(255, 255, 255, alpha))
        pen.setCosmetic(True)
        pen.setWidth(1)
        painter.setPen(pen)
        x0 = max(0,      int(rect.x()))
        x1 = min(GRID_N, int(rect.right()) + 1)
        y0 = max(0,      int(rect.y()))
        y1 = min(GRID_N, int(rect.bottom()) + 1)
        for x in range(x0, x1 + 1):
            painter.drawLine(QPointF(x, y0), QPointF(x, y1))
        for y in range(y0, y1 + 1):
            painter.drawLine(QPointF(x0, y), QPointF(x1, y))


# ---------------------------------------------------------------------------
# Tool E — Hillshade renderer (replaces WebGL — pure numpy + QPainter)
# ---------------------------------------------------------------------------
def render_hillshade(
    h_f32: np.ndarray,
    surface_blk: Optional[np.ndarray] = None,
    base_rgb: Optional[np.ndarray] = None,   # (H,W,3) float32 — overrides surface_blk/height
    sun_az: float = 315.0,
    sun_alt: float = 45.0,
    h_scale: float = 1.5,
) -> np.ndarray:
    """
    Numpy hillshade render. Returns (H, W, 3) uint8 RGB.
    h_f32: (H, W) float32 [0, 1] — normalised height.
    surface_blk: (H, W) object dtype of block name strings, or None for height colormap.
    h_scale: vertical exaggeration (1.0 = physically accurate MC blocks/pixel).
    """
    # Scale to MC Y range so gradient is in blocks/pixel — gives realistic slope angles
    dy, dx = np.gradient(h_f32 * Y_RANGE * h_scale)
    mag = np.sqrt(dx * dx + dy * dy + 1.0)
    nx = -dx / mag;  ny = -dy / mag;  nz = 1.0 / mag
    az  = np.radians(sun_az);   alt = np.radians(sun_alt)
    lx  = np.cos(alt) * np.sin(az)   # east component (geographic az: clockwise from N)
    ly  = np.cos(alt) * np.cos(az)   # north component
    lz  = np.sin(alt)
    illum = np.clip(nx * lx + ny * ly + nz * lz, 0.08, 1.0).astype(np.float32)

    H, W = h_f32.shape
    if base_rgb is not None:
        base = base_rgb.astype(np.float32)
    elif surface_blk is not None:
        base = np.full((H, W, 3), (96, 32, 128), dtype=np.float32)
        for blk_name, rgb in BLOCK_COLORS.items():
            mask = surface_blk == blk_name
            if mask.any():
                base[mask] = rgb
    else:
        base = _h_to_rgba((h_f32 * 65535).astype(np.uint16))[:, :, :3].astype(np.float32)

    return (base * illum[:, :, None]).clip(0, 255).astype(np.uint8)


class FastIsoLoader(QThread):
    """Load tile height from height.tif; emits raw h_norm array for any render mode."""
    done = pyqtSignal(object)   # np.ndarray float32 [0,1] or None

    def __init__(self, tx: int, tz: int):
        super().__init__()
        self.tx = tx
        self.tz = tz

    def run(self):
        try:
            import rasterio, rasterio.windows
            col_off = self.tx * TILE_PX
            row_off = self.tz * TILE_PX
            with rasterio.open(MASKS_DIR / "height.tif") as ds:
                win = rasterio.windows.Window(col_off, row_off, TILE_PX, TILE_PX)
                raw = ds.read(1, window=win).astype(np.float32)
            # Auto-detect: uint16 source → float32 [0,65535]; float source → [0,1]
            h_norm = raw / 65535.0 if raw.max() > 1.0 else raw.copy()
            self.done.emit(h_norm)
        except Exception as e:
            print(f"[FastIsoLoader] {e}")
            self.done.emit(None)


class RegionIsoLoader(QThread):
    """
    Load a multi-tile or full-world region downsampled to RENDER_SZ px.
    Always emits h_norm float32 [0,1] at RENDER_SZ×RENDER_SZ regardless of
    actual region extent — rasterio averages down, so iso render time is constant.
    """
    RENDER_SZ = 512
    done = pyqtSignal(object)  # np.ndarray float32 [0,1] or None

    def __init__(self, tx: int, tz: int, region_tiles: int):
        """
        region_tiles: N for an N×N tile area centred on (tx, tz).
                      Pass 0 for full-world view (reads entire 50k TIF).
        """
        super().__init__()
        self.tx           = tx
        self.tz           = tz
        self.region_tiles = region_tiles

    def run(self):
        try:
            import rasterio
            from rasterio.windows import Window
            from rasterio.enums import Resampling

            sz = self.RENDER_SZ
            with rasterio.open(str(MASKS_DIR / "height.tif")) as src:
                if self.region_tiles == 0:
                    # Full world — read entire TIF at sz×sz
                    data = src.read(1, out_shape=(sz, sz),
                                    resampling=Resampling.average)
                else:
                    n    = self.region_tiles
                    tx0  = max(0, min(GRID_N - n, self.tx - n // 2))
                    tz0  = max(0, min(GRID_N - n, self.tz - n // 2))
                    data = src.read(
                        1,
                        window=Window(tx0 * TILE_PX, tz0 * TILE_PX,
                                      n * TILE_PX, n * TILE_PX),
                        out_shape=(sz, sz),
                        resampling=Resampling.average,
                    )

            raw    = data.astype(np.float32)
            h_norm = raw / 65535.0 if raw.max() > 1.0 else raw
            self.done.emit(h_norm)
        except Exception as e:
            print(f"[RegionIsoLoader] {e}")
            self.done.emit(None)


class TileCanvas(QWidget):
    """
    Terrain image display: pan (left-drag), zoom (scroll wheel), R to reset.
    Drawn entirely in paintEvent — no QLabel/setPixmap so no layout side effects.

    xsec_mode: when True, draws a yellow Y-value crosshair on mouse hover.
    The cross-section PNG is always H=512 px representing Y_MIN..Y_MAX (1:1).
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self._base:      Optional[QPixmap] = None
        self._drag:      Optional[QPoint]  = None
        self._offset     = QPoint(0, 0)
        self._zoom       = 1.0
        self._xsec_mode  = False
        self._hover_iy:  Optional[int]     = None   # image-space pixel Y
        self._hover_ix:  Optional[int]     = None   # image-space pixel X
        self._biome_grid: Optional[object] = None   # np.ndarray of biome strings (H,W)
        self._h_norm_grid:   Optional[object] = None  # np.ndarray float32 [0,1] (H,W)
        self._override_grid: Optional[object] = None  # np.ndarray uint8 zone codes (H,W)

    def set_xsec_mode(self, enabled: bool):
        self._xsec_mode = enabled
        self._hover_iy  = None
        self._hover_ix  = None
        self.update()

    def set_biome_grid(self, grid):
        """Store the biome string grid (H,W ndarray). None to clear."""
        self._biome_grid = grid
        if grid is None:
            self._h_norm_grid   = None
            self._override_grid = None
        self.update()

    def set_provenance_data(self, h_norm, override_8bit):
        """Store height-norm and override grids for the provenance hover panel."""
        self._h_norm_grid   = h_norm
        self._override_grid = override_8bit
        self.update()

    def set_pixmap_data(self, pixmap: QPixmap):
        self._base = pixmap
        self._offset = QPoint(0, 0)
        self._fit_zoom()
        self.update()

    def _fit_zoom(self):
        if (self._base and not self._base.isNull()
                and self.width() > 10 and self.height() > 10):
            self._zoom = min(self.width()  / self._base.width(),
                             self.height() / self._base.height()) * 0.97

    def resizeEvent(self, e):
        self._fit_zoom()
        super().resizeEvent(e)

    def showEvent(self, e):
        self._fit_zoom()
        super().showEvent(e)

    def _img_y(self, widget_y: int) -> Optional[int]:
        """Convert widget Y pixel to image Y pixel, or None if out of bounds."""
        if not self._base or self._base.isNull():
            return None
        sh = int(self._base.height() * self._zoom)
        oy = (self.height() - sh) // 2 + self._offset.y()
        iy = (widget_y - oy) / self._zoom
        if 0 <= iy < self._base.height():
            return int(iy)
        return None

    def _img_xy(self, wx: int, wy: int):
        """Convert widget (x, y) to image (ix, iy), or (None, None) if out of bounds."""
        if not self._base or self._base.isNull():
            return None, None
        iw, ih = self._base.width(), self._base.height()
        sw = int(iw * self._zoom)
        sh = int(ih * self._zoom)
        ox = (self.width()  - sw) // 2 + self._offset.x()
        oy = (self.height() - sh) // 2 + self._offset.y()
        ix = (wx - ox) / self._zoom
        iy = (wy - oy) / self._zoom
        if 0 <= ix < iw and 0 <= iy < ih:
            return int(ix), int(iy)
        return None, None

    def paintEvent(self, _e):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(C_BG))
        if self._base is None:
            p.setPen(QColor(C_MUTED))
            f = p.font(); f.setPointSize(11); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "Select a tile — hillshade loads automatically")
            p.end()
            return
        W, H = self.width(), self.height()
        sc = self._base.scaled(
            int(self._base.width()  * self._zoom),
            int(self._base.height() * self._zoom),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        x = (W - sc.width())  // 2 + self._offset.x()
        y = (H - sc.height()) // 2 + self._offset.y()
        p.drawPixmap(x, y, sc)

        # ── X-section Y hover overlay ─────────────────────────────────────
        if self._xsec_mode and self._hover_iy is not None:
            sh  = int(self._base.height() * self._zoom)
            oy  = (H - sh) // 2 + self._offset.y()
            wy  = int(self._hover_iy * self._zoom) + oy
            # MC Y: image top (iy=0) = Y_MAX, bottom (iy=H-1) = Y_MIN
            img_h = self._base.height()
            mc_y  = Y_MIN + int((img_h - 1 - self._hover_iy) * Y_RANGE / max(1, img_h))
            # horizontal line
            pen = QPen(QColor(255, 230, 50, 210))
            pen.setWidth(1)
            p.setPen(pen)
            p.drawLine(0, wy, W, wy)
            # label
            label = f"  Y = {mc_y}"
            f = p.font(); f.setPointSize(9); f.setBold(True); p.setFont(f)
            lbl_y = max(wy - 4, 12)
            # shadow
            p.setPen(QColor(0, 0, 0, 160))
            p.drawText(7, lbl_y + 1, label)
            # text
            p.setPen(QColor(255, 230, 50))
            p.drawText(6, lbl_y, label)

        # ── Biome / provenance hover overlay ─────────────────────────────
        if (not self._xsec_mode
                and self._biome_grid is not None
                and self._hover_ix is not None
                and self._hover_iy is not None):
            H, W2 = self._biome_grid.shape
            gx = min(self._hover_ix, W2 - 1)
            gy = min(self._hover_iy, H  - 1)
            biome_name = str(self._biome_grid[gy, gx])
            if biome_name and biome_name != "nan":
                # ── Gather provenance data if available ────────────────
                prov = None
                if self._h_norm_grid is not None and self._override_grid is not None:
                    h_val  = float(self._h_norm_grid[gy, gx])
                    ov_val = int(self._override_grid[gy, gx])
                    prov   = _provenance_at(h_val, ov_val, biome_name)

                # ── Build text lines ───────────────────────────────────
                biome_display = biome_name.replace("_", " ").title()
                lines_detail: list[str] = []
                if prov:
                    lines_detail.append(
                        f"raw {prov['raw_h']:,}  ·  norm {prov['norm']}  ·  {prov['terrain']}")
                    if prov["override"]:
                        lines_detail.append(f"override: {prov['override']}")

                # ── Measure pill ───────────────────────────────────────
                f_bold = p.font(); f_bold.setPointSize(9); f_bold.setBold(True)
                f_small = p.font(); f_small.setPointSize(8); f_small.setBold(False)
                p.setFont(f_bold)
                fm_bold  = p.fontMetrics()
                p.setFont(f_small)
                fm_small = p.fontMetrics()

                pad_x, pad_y, line_gap = 8, 5, 2
                title_h = fm_bold.height()
                detail_h = fm_small.height()
                pill_h = pad_y + title_h
                for _ in lines_detail:
                    pill_h += line_gap + detail_h
                pill_h += pad_y

                title_w  = fm_bold.horizontalAdvance(biome_display)
                detail_w = max((fm_small.horizontalAdvance(ln) for ln in lines_detail),
                               default=0)
                pill_w   = max(title_w, detail_w) + pad_x * 2

                # ── Position: above cursor, clamped inside widget ──────
                cursor_sx = (self._hover_ix * self._zoom
                             + (self.width()  - int(self._base.width()  * self._zoom)) // 2
                             + self._offset.x())
                cursor_sy = (self._hover_iy * self._zoom
                             + (self.height() - int(self._base.height() * self._zoom)) // 2
                             + self._offset.y())
                px = int(min(cursor_sx + 12, self.width()  - pill_w - 4))
                py = int(min(cursor_sy - pill_h - 4, self.height() - pill_h - 4))
                px = max(px, 4)
                py = max(py, 4)

                # ── Draw background pill ───────────────────────────────
                p.setBrush(QBrush(QColor(14, 16, 28, 220)))
                p.setPen(QPen(QColor(C_BORDER), 1))
                p.drawRoundedRect(px, py, pill_w, pill_h, 5, 5)

                # ── Draw title (biome name) ────────────────────────────
                p.setFont(f_bold)
                p.setPen(QColor(255, 240, 140))
                ty = py + pad_y
                p.drawText(px + pad_x, ty, pill_w - pad_x * 2, title_h,
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           biome_display)

                # ── Draw detail lines ──────────────────────────────────
                p.setFont(f_small)
                p.setPen(QColor(160, 180, 210))
                cy = ty + title_h + line_gap
                for ln in lines_detail:
                    p.drawText(px + pad_x, cy, pill_w - pad_x * 2, detail_h,
                               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                               ln)
                    cy += detail_h + line_gap

        p.end()

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag = e.pos()

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._drag is not None:
            self._offset += e.pos() - self._drag
            self._drag = e.pos()
            self.update()
        if self._xsec_mode:
            new_iy = self._img_y(e.pos().y())
            if new_iy != self._hover_iy:
                self._hover_iy = new_iy
                self.update()
        elif self._biome_grid is not None:
            new_ix, new_iy = self._img_xy(e.pos().x(), e.pos().y())
            if (new_ix, new_iy) != (self._hover_ix, self._hover_iy):
                self._hover_ix, self._hover_iy = new_ix, new_iy
                self.update()

    def leaveEvent(self, e):
        changed = False
        if self._hover_iy is not None:
            self._hover_iy = None
            changed = True
        if self._hover_ix is not None:
            self._hover_ix = None
            changed = True
        if changed:
            self.update()
        super().leaveEvent(e)

    def mouseReleaseEvent(self, e: QMouseEvent):
        self._drag = None

    def wheelEvent(self, e: QWheelEvent):
        f = 1.15 if e.angleDelta().y() > 0 else 1.0 / 1.15
        self._zoom = max(0.15, min(10.0, self._zoom * f))
        self.update()

    def keyPressEvent(self, e: QKeyEvent):
        step = 30
        k = e.key()
        if   k == Qt.Key.Key_R:     self._zoom = 1.0; self._offset = QPoint(0, 0); self.update()
        elif k == Qt.Key.Key_Left:  self._offset += QPoint(-step, 0); self.update()
        elif k == Qt.Key.Key_Right: self._offset += QPoint(step,  0); self.update()
        elif k == Qt.Key.Key_Up:    self._offset += QPoint(0, -step); self.update()
        elif k == Qt.Key.Key_Down:  self._offset += QPoint(0,  step); self.update()
        else: super().keyPressEvent(e)


# ---------------------------------------------------------------------------
# Cluster support — load 3×3 neighbor tiles and display as a context grid
# ---------------------------------------------------------------------------

class ClusterLoader(QThread):
    """Load hillshade thumbnails for a 3×3 tile cluster around (tx, tz)."""
    tile_ready = pyqtSignal(int, int, QPixmap)   # dx, dz, pixmap

    def __init__(self, tx: int, tz: int, sz: int = 128, radius: int = 1):
        super().__init__()
        self.tx     = tx
        self.tz     = tz
        self.sz     = sz
        self.radius = radius

    def run(self):
        try:
            import rasterio
            from rasterio.windows import Window
            from rasterio.enums import Resampling
            HILLSHADE_CACHE.mkdir(parents=True, exist_ok=True)
            r = getattr(self, 'radius', 1)
            with rasterio.open(str(MASKS_DIR / "height.tif")) as src:
                for dz in range(-r, r + 1):
                    for dx in range(-r, r + 1):
                        if self.isInterruptionRequested():
                            return
                        tx2, tz2 = self.tx + dx, self.tz + dz
                        if not (0 <= tx2 < GRID_N and 0 <= tz2 < GRID_N):
                            self.tile_ready.emit(dx, dz, QPixmap())
                            continue
                        cache_path = HILLSHADE_CACHE / f"{tx2}_{tz2}_{self.sz}.png"
                        if cache_path.exists():
                            pm = QPixmap(str(cache_path))
                            if not pm.isNull():
                                self.tile_ready.emit(dx, dz, pm)
                                continue
                        try:
                            raw = src.read(
                                1,
                                window=Window(tx2*TILE_PX, tz2*TILE_PX, TILE_PX, TILE_PX),
                                out_shape=(self.sz, self.sz),
                                resampling=Resampling.average,
                            ).astype(np.float32)
                            h_norm = raw / 65535.0 if raw.max() > 1.0 else raw
                            rgb  = render_hillshade(h_norm)
                            rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
                            pm   = _rgba_to_qpixmap(rgba)
                            pm.save(str(cache_path))
                            self.tile_ready.emit(dx, dz, pm)
                        except Exception as inner_e:
                            print(f"[ClusterLoader] ({dx},{dz}): {inner_e}")
                            self.tile_ready.emit(dx, dz, QPixmap())
        except Exception as e:
            print(f"[ClusterLoader] {e}")


class BiomeClusterLoader(QThread):
    """
    Loads biome-coloured thumbnails for an N×N tile cluster.
    Reads override.tif directly and maps zone codes → biome colours.
    Override is the sole source of truth — no procedural assignment.
    """
    tile_ready = pyqtSignal(int, int, QPixmap)   # dx, dz, pixmap

    def __init__(self, tx: int, tz: int, radius: int = 1, sz: int = 128):
        super().__init__()
        self.tx     = tx
        self.tz     = tz
        self.radius = radius   # 1=3×3, 2=5×5, 3=9×9
        self.sz     = sz

    def run(self):
        try:
            import rasterio, warnings
            from rasterio.windows import Window
            from rasterio.enums import Resampling

            # Build 256-entry RGB LUT: zone code → (R, G, B)
            valid_zones = sorted(_OVERRIDE_NAMES.keys())
            lut = np.zeros((256, 3), dtype=np.uint8)
            # Zone 0 → ocean colour
            lut[0] = BIOME_COLORS.get("_OCEAN", (30, 80, 160))
            for v in range(1, 256):
                nearest = min(valid_zones, key=lambda z: abs(z - v))
                biome = _OVERRIDE_NAMES[nearest]
                lut[v] = BIOME_COLORS.get(biome, (128, 128, 128))

            override_path = str(MASKS_DIR / "override.tif")
            height_path   = str(MASKS_DIR / "height.tif")
            r = self.radius
            sz = self.sz

            for dz in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if self.isInterruptionRequested():
                        return
                    tx2, tz2 = self.tx + dx, self.tz + dz
                    if not (0 <= tx2 < GRID_N and 0 <= tz2 < GRID_N):
                        self.tile_ready.emit(dx, dz, QPixmap())
                        continue
                    try:
                        col, row = tx2 * TILE_PX, tz2 * TILE_PX
                        win = Window(col, row, TILE_PX, TILE_PX)

                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            # Read override at thumbnail size — nearest for discrete codes
                            with rasterio.open(override_path) as ds:
                                ov = ds.read(1, window=win, out_shape=(sz, sz),
                                             resampling=Resampling.nearest)
                            # Read height for hillshade blend
                            with rasterio.open(height_path) as ds:
                                h_raw = ds.read(1, window=win, out_shape=(sz, sz),
                                                resampling=Resampling.average).astype(np.float32)

                        # Map zone codes → RGB via LUT
                        biome_rgb = lut[ov].astype(np.float32)

                        # Blend 65% biome + 35% hillshade for terrain context
                        h_norm = h_raw / 65535.0 if h_raw.max() > 1.0 else h_raw
                        hs_rgb = render_hillshade(h_norm).astype(np.float32)
                        blended = (biome_rgb * 0.65 + hs_rgb * 0.35).clip(0, 255).astype(np.uint8)
                        rgba = np.dstack([blended, np.full((sz, sz), 255, np.uint8)])
                        self.tile_ready.emit(dx, dz, _rgba_to_qpixmap(rgba))

                    except Exception as e2:
                        print(f"[BiomeClusterLoader] ({dx},{dz}): {e2}")
                        self.tile_ready.emit(dx, dz, QPixmap())
        except Exception as e:
            print(f"[BiomeClusterLoader] {e}")


class ClusterView(QWidget):
    """
    3×3 grid of hillshade tiles centred on the selected tile.
    Selected tile has amber highlight.  Click a neighbour to select it.
    Fixed size: CELL_SZ×3 per side.
    """
    tile_selected = pyqtSignal(int, int)
    CELL_SZ = 130

    def __init__(self, parent=None):
        super().__init__(parent)
        self._center:        Optional[tuple[int, int]] = None
        self._pixmaps:       dict[tuple, QPixmap]      = {}   # (dx, dz) → QPixmap
        self._biome_pixmaps: dict[tuple, QPixmap]      = {}   # (dx, dz) → QPixmap
        self._loader:        Optional[ClusterLoader]         = None
        self._biome_loader:  Optional[BiomeClusterLoader]    = None
        self._biome_mode     = False
        self._radius         = 1   # 1=3×3, 2=5×5, 3=9×9
        self._hover_cell: Optional[tuple[int, int]] = None   # (dx, dz) hovered
        sz = self.CELL_SZ * 3
        self.setFixedWidth(sz)
        self.setMinimumHeight(sz)
        self.setMouseTracking(True)

    def set_biome_mode(self, enabled: bool):
        self._biome_mode = enabled
        if enabled and self._center:
            self._load_biome_cluster()
        self.update()

    def set_radius(self, radius: int):
        """Set cluster radius: 1=3×3, 2=5×5, 3=9×9."""
        if radius == self._radius:
            return
        self._radius = radius
        n = radius * 2 + 1
        cell_sz = max(44, min(self.CELL_SZ, 390 // n))
        total = n * cell_sz
        self.setFixedWidth(total)
        self.setMinimumHeight(total)
        self._pixmaps.clear()
        self._biome_pixmaps.clear()
        if self._center:
            self.load_cluster(*self._center)
        self.update()

    def _cell_sz(self) -> int:
        """Current cell size based on radius."""
        n = self._radius * 2 + 1
        return max(44, min(self.CELL_SZ, 390 // n))

    def load_cluster(self, tx: int, tz: int):
        self._center = (tx, tz)
        self._pixmaps.clear()
        self.update()
        try:
            if self._loader and self._loader.isRunning():
                self._loader.requestInterruption()
                self._loader.wait(400)
        except RuntimeError:
            pass
        sz = self._cell_sz()
        self._loader = ClusterLoader(tx, tz, sz, self._radius)
        self._loader.tile_ready.connect(self._on_tile_ready)
        _start_thread(self._loader)
        if self._biome_mode:
            self._load_biome_cluster()

    def _on_tile_ready(self, dx: int, dz: int, pixmap: QPixmap):
        self._pixmaps[(dx, dz)] = pixmap
        self.update()

    def _load_biome_cluster(self):
        try:
            if self._biome_loader and self._biome_loader.isRunning():
                self._biome_loader.requestInterruption()
                self._biome_loader.wait(400)
        except RuntimeError:
            pass
        sz = self._cell_sz()
        self._biome_loader = BiomeClusterLoader(
            self._center[0], self._center[1], self._radius, sz)
        self._biome_loader.tile_ready.connect(self._on_biome_tile_ready)
        _start_thread(self._biome_loader)

    def _on_biome_tile_ready(self, dx: int, dz: int, pixmap: QPixmap):
        self._biome_pixmaps[(dx, dz)] = pixmap
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(C_BG))
        sz  = self._cell_sz()
        r   = self._radius

        if self._center is None:
            p.setPen(QColor(C_MUTED))
            f = p.font(); f.setPointSize(10); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Select a tile")
            p.end()
            return

        for dz in range(-r, r + 1):
            for dx in range(-r, r + 1):
                cx = (dx + r) * sz
                cy = (dz + r) * sz
                tx2 = self._center[0] + dx
                tz2 = self._center[1] + dz
                in_bounds = 0 <= tx2 < GRID_N and 0 <= tz2 < GRID_N

                # Pick pixmap: biome mode prefers biome_pixmaps, falls back to hillshade
                if self._biome_mode:
                    pm = self._biome_pixmaps.get((dx, dz)) or self._pixmaps.get((dx, dz))
                else:
                    pm = self._pixmaps.get((dx, dz))

                # Background / tile image
                if pm and not pm.isNull():
                    p.drawPixmap(QRect(cx, cy, sz, sz), pm)
                else:
                    p.fillRect(QRect(cx, cy, sz, sz), QColor(C_PANEL))
                    if not in_bounds:
                        p.setPen(QColor(C_BORDER))
                        p.drawText(QRect(cx, cy, sz, sz),
                                   Qt.AlignmentFlag.AlignCenter, "—")
                    else:
                        label = "biome…" if self._biome_mode else "…"
                        p.setPen(QColor(C_MUTED))
                        p.drawText(QRect(cx, cy, sz, sz),
                                   Qt.AlignmentFlag.AlignCenter, label)

                # Tile coord label
                if in_bounds and sz >= 60:
                    p.setPen(QColor(200, 200, 220, 200))
                    f = p.font(); f.setPointSize(7 if sz < 80 else 8); p.setFont(f)
                    p.drawText(cx + 2, cy + 10, f"{tx2},{tz2}")

                # Border — amber for selected, hover highlight, subtle for others
                if dx == 0 and dz == 0:
                    pen = QPen(QColor(C_SEL))
                    pen.setWidth(2)
                    p.setPen(pen)
                    p.drawRect(cx + 1, cy + 1, sz - 2, sz - 2)
                elif self._hover_cell == (dx, dz):
                    pen = QPen(QColor(180, 180, 255, 160))
                    pen.setWidth(1)
                    p.setPen(pen)
                    p.drawRect(cx, cy, sz - 1, sz - 1)
                else:
                    p.setPen(QColor(C_BORDER))
                    p.drawRect(cx, cy, sz - 1, sz - 1)

        p.end()

    def mouseMoveEvent(self, e: QMouseEvent):
        if self._center:
            sz = self._cell_sz()
            r  = self._radius
            dx = e.pos().x() // sz - r
            dz = e.pos().y() // sz - r
            cell = (dx, dz) if (-r <= dx <= r and -r <= dz <= r) else None
            if cell != self._hover_cell:
                self._hover_cell = cell
                self.update()

    def mousePressEvent(self, e: QMouseEvent):
        if e.button() == Qt.MouseButton.LeftButton and self._center:
            sz = self._cell_sz()
            r  = self._radius
            dx = e.pos().x() // sz - r
            dz = e.pos().y() // sz - r
            if -r <= dx <= r and -r <= dz <= r:
                tx2 = self._center[0] + dx
                tz2 = self._center[1] + dz
                if 0 <= tx2 < GRID_N and 0 <= tz2 < GRID_N:
                    self.tile_selected.emit(tx2, tz2)


# ---------------------------------------------------------------------------
# PreviewPanel — unified single-tile + cluster preview (replaces Inspect + Voxel)
# ---------------------------------------------------------------------------

class PreviewPanel(QWidget):
    """
    Left: 3×3 cluster context (ClusterView).
    Right: single-tile detail — Top-Down or Isometric, with optional X-section.
    Both panels always visible simultaneously.
    Generate Colors runs the pipeline and enriches both views.
    """
    pipeline_done  = pyqtSignal(dict)
    tile_selected  = pyqtSignal(int, int)   # emitted when cluster click picks a tile
    sim_complete   = pyqtSignal(int, int, list)  # (tx, tz, biome_names) on successful sim

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tx      = -1
        self._tz      = -1
        self._state:   Optional[dict]          = None
        self._h_norm:  Optional[np.ndarray]    = None   # cached fast-load height
        self._worker:         Optional[GenerateWorker]   = None
        self._xworker:        Optional[XSectionWorker]   = None
        self._loader:         Optional[FastIsoLoader]    = None
        self._region_loader:  Optional[RegionIsoLoader]  = None
        self._sim_worker:     Optional[SimPreviewWorker] = None
        self._sim_biome_rgb:  Optional[np.ndarray]       = None   # (H,W,3) float32
        self._region_tiles    = 1       # 1, 3, 5, 9, or 0 (world)
        self._view_mode = "topdown"   # "topdown" | "iso"
        self._td_mode   = "height"    # "height" | "biome" | "slope" | "surface"
        self._sun_az    = 315.0
        self._h_scale   = 1.5

        # In-session h_norm cache: tile (tx,tz) → h_norm ndarray
        # Avoids re-reading height.tif on every tile revisit (TIF window read ~50-200ms).
        from collections import OrderedDict
        self._h_norm_cache: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
        self._H_NORM_CACHE_MAX = 20   # ~20 × 1MB = ~20MB

        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                self.cfg = json.load(f)
        else:
            self.cfg = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Toolbar ──────────────────────────────────────────────────────────
        tb_widget = QWidget()
        tb_widget.setStyleSheet(
            f"background:{C_PANEL}; border-bottom:1px solid {C_BORDER};")
        tb_widget.setMaximumHeight(38)
        tb = QHBoxLayout(tb_widget)
        tb.setContentsMargins(8, 3, 8, 3)
        tb.setSpacing(5)

        # View mode buttons
        self._td_btn = QPushButton("Top-Down")
        self._td_btn.setCheckable(True); self._td_btn.setChecked(True)
        self._td_btn.setFixedWidth(80)
        self._td_btn.clicked.connect(lambda: self._set_view("topdown"))
        tb.addWidget(self._td_btn)

        self._iso_btn = QPushButton("Isometric")
        self._iso_btn.setCheckable(True)
        self._iso_btn.setFixedWidth(80)
        self._iso_btn.clicked.connect(lambda: self._set_view("iso"))
        tb.addWidget(self._iso_btn)

        # Region selector — visible only in Iso mode
        self._region_combo = QComboBox()
        self._region_combo.addItems(["1×1", "3×3", "5×5", "9×9", "World"])
        self._region_combo.setFixedWidth(68)
        self._region_combo.setToolTip("Isometric region size (centred on selected tile)")
        self._region_combo.hide()
        self._region_combo.currentIndexChanged.connect(self._on_region_change)
        tb.addWidget(self._region_combo)

        self._xsec_btn = QPushButton("X-Section")
        self._xsec_btn.setCheckable(True)
        self._xsec_btn.setEnabled(False)
        self._xsec_btn.setFixedWidth(80)
        self._xsec_btn.toggled.connect(self._on_xsec_toggle)
        tb.addWidget(self._xsec_btn)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(f"color:{C_BORDER};"); sep.setFixedHeight(18)
        tb.addWidget(sep)

        # Generate MCA — runs full pipeline + writes .mca output
        self._gen_btn = QPushButton("Generate MCA")
        self._gen_btn.setObjectName("accent")
        self._gen_btn.setEnabled(False)
        self._gen_btn.setFixedWidth(118)
        self._gen_btn.clicked.connect(self._do_render)
        tb.addWidget(self._gen_btn)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet(f"color:{C_BORDER};"); sep2.setFixedHeight(18)
        tb.addWidget(sep2)

        # Mode selector (top-down only)
        self._mode_combo = QComboBox()
        self._mode_combo.addItems(["Height", "Biome", "Slope", "Surface Blocks"])
        self._mode_combo.setEnabled(False)
        self._mode_combo.setFixedWidth(118)
        self._mode_combo.currentIndexChanged.connect(self._on_td_mode_change)
        tb.addWidget(self._mode_combo)

        tb.addStretch()

        # Sun + exag sliders — pushed right by stretch
        tb.addWidget(QLabel("☀"))
        self._sun_sl = QSlider(Qt.Orientation.Horizontal)
        self._sun_sl.setRange(0, 360); self._sun_sl.setValue(315)
        self._sun_sl.setFixedWidth(72)
        self._sun_sl.valueChanged.connect(self._on_sun_change)
        tb.addWidget(self._sun_sl)

        tb.addWidget(QLabel("Exag"))
        self._exag_sl = QSlider(Qt.Orientation.Horizontal)
        self._exag_sl.setRange(1, 30); self._exag_sl.setValue(15)
        self._exag_sl.setFixedWidth(60)
        self._exag_sl.valueChanged.connect(self._on_exag_change)
        tb.addWidget(self._exag_sl)

        # Z-section (hidden unless X-Section active)
        self._z_lbl = QLabel("Z=256")
        self._z_lbl.hide()
        tb.addWidget(self._z_lbl)
        self._z_sl = QSlider(Qt.Orientation.Horizontal)
        self._z_sl.setRange(0, TILE_PX - 1); self._z_sl.setValue(TILE_PX // 2)
        self._z_sl.setFixedWidth(80); self._z_sl.setEnabled(False)
        self._z_sl.hide()
        self._z_sl.valueChanged.connect(self._on_z_change)
        tb.addWidget(self._z_sl)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.VLine)
        sep3.setStyleSheet(f"color:{C_BORDER};"); sep3.setFixedHeight(18)
        tb.addWidget(sep3)

        self._tile_lbl = QLabel("—")
        self._tile_lbl.setStyleSheet(
            f"color:{C_ACCENT}; font-family:'Consolas','Courier New',monospace; font-size:11px;")
        tb.addWidget(self._tile_lbl)

        root.addWidget(tb_widget)

        # ── Main area: cluster (left) | single-tile canvas (right) ───────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)

        # Cluster controls header
        cluster_hdr = QWidget()
        cluster_hdr_lay = QHBoxLayout(cluster_hdr)
        cluster_hdr_lay.setContentsMargins(4, 2, 4, 2)
        cluster_hdr_lay.setSpacing(4)

        _sz_lbl = QLabel("Cluster:")
        _sz_lbl.setStyleSheet(f"color:{C_MUTED}; font-size:10px;")
        cluster_hdr_lay.addWidget(_sz_lbl)

        self._cluster_sz_btns: list[QPushButton] = []
        for _r, _lbl in [(1, "3×3"), (2, "5×5"), (3, "9×9")]:
            _b = QPushButton(_lbl)
            _b.setCheckable(True)
            _b.setFixedWidth(40)
            _b.setFixedHeight(20)
            _b.setStyleSheet("font-size:10px; padding:0;")
            _b.clicked.connect(lambda _, rv=_r: self._on_cluster_radius(rv))
            cluster_hdr_lay.addWidget(_b)
            self._cluster_sz_btns.append(_b)
        self._cluster_sz_btns[0].setChecked(True)

        cluster_hdr_lay.addSpacing(8)
        self._biome_cluster_btn = QPushButton("Biome")
        self._biome_cluster_btn.setCheckable(True)
        self._biome_cluster_btn.setFixedWidth(50)
        self._biome_cluster_btn.setFixedHeight(20)
        self._biome_cluster_btn.setStyleSheet("font-size:10px; padding:0;")
        self._biome_cluster_btn.toggled.connect(self._on_biome_cluster_toggle)
        cluster_hdr_lay.addWidget(self._biome_cluster_btn)
        cluster_hdr_lay.addStretch()

        self._cluster = ClusterView()
        self._cluster.tile_selected.connect(self._on_cluster_select)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)
        left_lay.addWidget(cluster_hdr)
        left_lay.addWidget(self._cluster)
        splitter.addWidget(left)

        self._canvas = TileCanvas()
        splitter.addWidget(self._canvas)

        cluster_w = ClusterView.CELL_SZ * 3 + 6
        splitter.setSizes([cluster_w + 6, max(400, cluster_w)])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        root.addWidget(splitter, stretch=1)

        # ── Status line ───────────────────────────────────────────────────────
        self._status = QLabel("Select a tile in the world map")
        self._status.setStyleSheet(
            f"color:{C_MUTED}; font-size:11px; padding:2px 8px;"
            f"background:{C_PANEL}; border-top:1px solid {C_BORDER};"
            f"font-family:'Consolas','Courier New',monospace;")
        self._status.setMaximumHeight(22)
        root.addWidget(self._status)

        self.setMinimumHeight(400)

        # Debounce timer for x-section
        self._xsec_timer = QTimer(self)
        self._xsec_timer.setSingleShot(True)
        self._xsec_timer.timeout.connect(self._do_xsec)

    # ── Public API ────────────────────────────────────────────────────────────

    def select_tile(self, tx: int, tz: int):
        self._tx, self._tz = tx, tz
        self._state       = None
        self._h_norm      = None
        self._sim_biome_rgb = None
        self._canvas.set_biome_grid(None)
        self._gen_btn.setEnabled(True)
        # Preserve cross-section mode — don't reset the user's view choice.
        # xsec will be re-enabled once h_norm loads; then we restore the toggle.
        self._restore_xsec = self._xsec_btn.isChecked()
        self._xsec_btn.blockSignals(True)
        self._xsec_btn.setChecked(False)
        self._xsec_btn.setEnabled(False)
        self._xsec_btn.blockSignals(False)
        self._mode_combo.setEnabled(False)
        wx0, wz0 = tx * TILE_PX, tz * TILE_PX
        self._tile_lbl.setText(f"({tx}, {tz})  X{wx0}–{wx0+511}  Z{wz0}–{wz0+511}")
        self._status.setText(f"Tile ({tx}, {tz}) — loading…")
        self._cluster.load_cluster(tx, tz)
        if self._view_mode == "iso" and self._region_tiles != 1:
            self._start_region_load()
        else:
            cached = self._h_norm_cache.get((tx, tz))
            if cached is not None:
                # Move to most-recently-used end
                self._h_norm_cache.move_to_end((tx, tz))
                self._h_norm = cached
                self._xsec_btn.setEnabled(True)
                if getattr(self, '_restore_xsec', False):
                    self._xsec_btn.setChecked(True)  # restores cross-section
                    self._restore_xsec = False
                else:
                    self._render_from_h_norm()
            else:
                self._start_fast_load()
        self._start_sim_preview()

    def set_state(self, state: dict):
        self._state = state
        self._tx    = state["tile_x"]
        self._tz    = state["tile_z"]
        self._mode_combo.setEnabled(True)
        self._xsec_btn.setEnabled(True)
        self._gen_btn.setEnabled(True)
        if not self._xsec_btn.isChecked():
            self._refresh_canvas()

    # ── Fast load (no pipeline) ───────────────────────────────────────────────

    def _start_fast_load(self):
        try:
            if self._loader and self._loader.isRunning():
                self._loader.requestInterruption()
                self._loader.wait(300)
        except RuntimeError:
            pass
        self._loader = FastIsoLoader(self._tx, self._tz)
        self._loader.done.connect(self._on_fast_load_done)
        _start_thread(self._loader)

    def _on_fast_load_done(self, h_norm):
        if h_norm is None:
            self._status.setText(f"Tile ({self._tx}, {self._tz}) — could not load height.tif")
            return
        self._h_norm = h_norm
        # Populate in-session LRU cache
        key = (self._tx, self._tz)
        self._h_norm_cache[key] = h_norm
        self._h_norm_cache.move_to_end(key)
        if len(self._h_norm_cache) > self._H_NORM_CACHE_MAX:
            self._h_norm_cache.popitem(last=False)   # evict LRU
        self._xsec_btn.setEnabled(True)
        if getattr(self, '_restore_xsec', False):
            self._xsec_btn.setChecked(True)  # restores cross-section
            self._restore_xsec = False
        else:
            self._render_from_h_norm()

    # ── Region iso load ───────────────────────────────────────────────────────

    _REGION_SIZES = [1, 3, 5, 9, 0]   # 0 = world
    _REGION_LABELS = {1: "1×1 tile", 3: "3×3 tiles", 5: "5×5 tiles",
                      9: "9×9 tiles", 0: "full world"}

    def _on_region_change(self, idx: int):
        self._region_tiles = self._REGION_SIZES[idx]
        if self._view_mode == "iso" and self._tx >= 0:
            if self._region_tiles == 1:
                # Single tile — use cached h_norm or fast-reload
                if self._h_norm is not None:
                    self._render_from_h_norm()
                else:
                    self._start_fast_load()
            else:
                self._start_region_load()

    def _start_region_load(self):
        if self._tx < 0:
            return
        label = self._REGION_LABELS.get(self._region_tiles, "region")
        self._status.setText(
            f"Tile ({self._tx}, {self._tz}) — loading {label}…")
        try:
            if self._region_loader and self._region_loader.isRunning():
                self._region_loader.requestInterruption()
                self._region_loader.wait(400)
        except RuntimeError:
            pass
        self._region_loader = RegionIsoLoader(self._tx, self._tz, self._region_tiles)
        self._region_loader.done.connect(self._on_region_load_done)
        _start_thread(self._region_loader)

    def _on_region_load_done(self, h_norm):
        if h_norm is None:
            self._status.setText("Region load failed — check height.tif")
            return
        self._h_norm = h_norm   # cache so sun/exag sliders work
        self._render_from_h_norm()

    # ── Simulation preview (biome assignment only) ────────────────────────────

    @staticmethod
    def _sim_config_hash(cfg: dict) -> str:
        """MD5 of the biome-assignment-relevant config keys. Cache-busts on change."""
        import hashlib
        _BIOME_KEYS = ("terrain_class", "hydrology", "moisture",
                       "sea_level_16bit", "biome_patch_noise", "terrain_spline",
                       "sparse_overrides", "block_mixing", "noise_layers_biome",
                       "eco_gradients", "eco_vegetation")
        subset = {k: cfg[k] for k in _BIOME_KEYS if k in cfg}
        return hashlib.md5(
            json.dumps(subset, sort_keys=True).encode()).hexdigest()[:8]

    def _sim_cache_path(self, tx: int, tz: int) -> Path:
        return HILLSHADE_CACHE / f"sim_{tx}_{tz}_{self._sim_config_hash(self.cfg)}.pkl"

    def _try_load_sim_cache(self, tx: int, tz: int) -> bool:
        """Try loading a cached sim result. Returns True if cache hit and result applied."""
        import pickle
        path = self._sim_cache_path(tx, tz)
        if not path.exists():
            return False
        try:
            with open(path, "rb") as f:
                result = pickle.load(f)
            # Verify it matches current tile (safety check)
            if result.get("tx") != tx or result.get("tz") != tz:
                return False
            self._status.setText(
                f"Tile ({tx}, {tz}) — biome preview  (cached)  "
                f"| click Generate MCA for block export")
            self._on_sim_done(result)
            return True
        except Exception as e:
            print(f"[SimCache] load failed for ({tx},{tz}): {e}")
            return False

    def _start_sim_preview(self):
        if self._tx < 0:
            return
        # Check disk cache first — skip 3-5s sim if config hasn't changed
        HILLSHADE_CACHE.mkdir(parents=True, exist_ok=True)
        if self._try_load_sim_cache(self._tx, self._tz):
            return
        try:
            if self._sim_worker and self._sim_worker.isRunning():
                self._sim_worker.requestInterruption()
                self._sim_worker.wait(300)
        except RuntimeError:
            pass
        self._sim_worker = SimPreviewWorker(self._tx, self._tz, self.cfg)
        self._sim_worker.progress.connect(
            lambda s: self._status.setText(
                f"Tile ({self._tx}, {self._tz}) — {s}"))
        self._sim_worker.done.connect(self._on_sim_done)
        _start_thread(self._sim_worker)

    def _on_sim_done(self, result):
        # Guard against stale results from a previous tile's worker completing late.
        # select_tile() tries requestInterruption()+wait(300ms) but numpy/scipy
        # operations don't honour interruption — the old worker may still finish.
        if (self._sim_worker is None
                or self._sim_worker.tx != self._tx
                or self._sim_worker.tz != self._tz):
            return   # discard — result is for a tile we've since navigated away from
        if result is None:
            # Sim failed — stay on hillshade but clear the loading message
            self._status.setText(
                f"Tile ({self._tx}, {self._tz}) — hillshade preview  "
                f"| biome sim failed (check console)  "
                f"| click Generate MCA for block export")
            return
        h_norm       = result["h_norm"]
        biome_rgb    = result["biome_rgb"]
        biome_grid   = result["biome_grid"]
        override_8bit= result.get("override_8bit")
        # Use surface block colors from pipeline if available, else biome colors
        surface_blk_rgb = result.get("surface_blk_rgb")
        self._sim_biome_rgb = surface_blk_rgb if surface_blk_rgb is not None else biome_rgb
        self._canvas.set_biome_grid(biome_grid)
        self._canvas.set_provenance_data(h_norm, override_8bit)
        self.last_sim_result = result  # stored for Biome Studio access
        self.sim_complete.emit(
            self._tx, self._tz,
            [b for b in np.unique(biome_grid).tolist() if not b.startswith("_")])
        # Write sim result to disk cache (skip if it came from cache already)
        if not result.get("_from_cache"):
            try:
                import pickle
                cache_path = self._sim_cache_path(self._tx, self._tz)
                HILLSHADE_CACHE.mkdir(parents=True, exist_ok=True)
                payload = dict(result)
                payload["tx"] = self._tx
                payload["tz"] = self._tz
                payload["_from_cache"] = True
                with open(cache_path, "wb") as f:
                    pickle.dump(payload, f, protocol=4)
            except Exception as e:
                print(f"[SimCache] write failed: {e}")
        # Only update the cached h_norm if we don't already have a better one
        if self._h_norm is None:
            self._h_norm = h_norm
            self._xsec_btn.setEnabled(True)
        # Re-render with biome colours (only if no pipeline state is shown)
        if self._state is None and not self._xsec_btn.isChecked():
            self._render_from_h_norm()

    # ── Fast render from h_norm ───────────────────────────────────────────────

    def _render_from_h_norm(self):
        """Render the canvas from cached h_norm (fast path, no pipeline)."""
        if self._h_norm is None:
            return
        base = self._sim_biome_rgb   # None until sim completes
        if self._view_mode == "iso":
            rgba = render_isometric(
                self._h_norm, sun_az=self._sun_az, h_scale=self._h_scale)
        else:
            rgb  = render_hillshade(
                self._h_norm, base_rgb=base,
                sun_az=self._sun_az, h_scale=self._h_scale)
            rgba = np.dstack([rgb, np.full(rgb.shape[:2], 255, np.uint8)])
        self._canvas.set_pixmap_data(_rgba_to_qpixmap(rgba))

        if self._view_mode == "iso":
            region_lbl = self._REGION_LABELS.get(self._region_tiles, "")
            mode_str   = f"isometric  {region_lbl}"
        elif base is not None:
            mode_str   = "surface block preview (pipeline)"
        else:
            mode_str   = "hillshade  (surface blocks loading…)"
        suffix = "| click Generate MCA for block export"
        self._status.setText(
            f"Tile ({self._tx}, {self._tz}) — {mode_str}  {suffix}")

    # ── Canvas refresh (post-pipeline) ───────────────────────────────────────

    def _refresh_canvas(self):
        if not self._state:
            self._render_from_h_norm()
            return
        sy   = self._state["surface_y"]
        h_n  = (sy.astype(np.float32) - Y_MIN) / Y_RANGE
        sblk = self._state.get("surface_blk")

        if self._view_mode == "iso":
            rgba = render_isometric(
                h_n, sblk, sun_az=self._sun_az, h_scale=self._h_scale)
            self._canvas.set_pixmap_data(_rgba_to_qpixmap(rgba))
            self._status.setText(
                f"Tile ({self._state['tile_x']}, {self._state['tile_z']}) — isometric  "
                f"Y {sy.min()}–{sy.max()}")
        else:
            modes = ["height", "biome", "slope", "surface"]
            mode  = modes[self._mode_combo.currentIndex()]
            rgba  = _render_inspect_preview(self._state, mode, TILE_PX)
            self._canvas.set_pixmap_data(_rgba_to_qpixmap(rgba))
            self._status.setText(
                f"Tile ({self._state['tile_x']}, {self._state['tile_z']}) — {mode}  "
                f"Y {sy.min()}–{sy.max()}")

    # ── View mode ─────────────────────────────────────────────────────────────

    def _set_view(self, mode: str):
        self._view_mode = mode
        self._td_btn.setChecked(mode == "topdown")
        self._iso_btn.setChecked(mode == "iso")
        self._region_combo.setVisible(mode == "iso")
        self._mode_combo.setEnabled(mode == "topdown" and self._state is not None)
        if not self._xsec_btn.isChecked():
            if mode == "iso" and self._region_tiles != 1 and self._tx >= 0:
                self._start_region_load()
            else:
                self._refresh_canvas()

    def _on_td_mode_change(self, _idx: int):
        if self._state and self._view_mode == "topdown":
            self._refresh_canvas()

    def _on_sun_change(self, v: int):
        self._sun_az = float(v)
        if not self._xsec_btn.isChecked():
            self._refresh_canvas()

    def _on_exag_change(self, v: int):
        self._h_scale = v / 10.0
        if not self._xsec_btn.isChecked():
            self._refresh_canvas()

    # ── Pipeline ─────────────────────────────────────────────────────────────

    def _do_render(self):
        if self._tx < 0:
            return
        self._gen_btn.setEnabled(False)
        self._status.setText(f"Running pipeline for ({self._tx}, {self._tz})…")
        self._worker = GenerateWorker(self._tx, self._tz, self.cfg)
        self._worker.progress.connect(lambda s: self._status.setText(s))
        self._worker.finished.connect(self._on_render_done)
        self._worker.error.connect(
            lambda e: (self._status.setText(f"Error: {e[:120]}"),
                       self._gen_btn.setEnabled(True)))
        _start_thread(self._worker)

    def _on_render_done(self, state: dict):
        self._gen_btn.setEnabled(True)
        self.set_state(state)
        self.pipeline_done.emit(state)

    # ── Cross-section ─────────────────────────────────────────────────────────

    def _on_xsec_toggle(self, checked: bool):
        self._z_sl.setVisible(checked)
        self._z_lbl.setVisible(checked)
        self._z_sl.setEnabled(checked)
        self._canvas.set_xsec_mode(checked)
        if not checked:
            self._refresh_canvas()
        else:
            self._do_xsec()

    def _on_z_change(self, v: int):
        self._z_lbl.setText(f"Z={v}")
        if self._xsec_btn.isChecked() and (self._state or self._h_norm is not None):
            self._xsec_timer.start(300)

    def _do_xsec(self):
        if self._state:
            # Full biome+banding cross-section in background thread
            try:
                if self._xworker and self._xworker.isRunning():
                    self._xworker.terminate()
                    self._xworker.wait()
            except RuntimeError:
                pass
            geo = self.cfg.get("geo_surface", {})
            self._xworker = XSectionWorker(
                self._state, self._z_sl.value(),
                geo.get("band_scale_y", 16), 45.0)
            self._xworker.finished.connect(self._on_xsec_done)
            self._xworker.error.connect(self._on_xsec_error)
            _start_thread(self._xworker)
        elif self._h_norm is not None:
            # Height-only cross-section — fast enough to run inline
            self._status.setText(
                f"Tile ({self._tx}, {self._tz}) — height cross-section  "
                f"| run Generate Colors for biome detail")
            rgba = make_xsec_from_hnorm(self._h_norm, self._z_sl.value())
            self._canvas.set_pixmap_data(_rgba_to_qpixmap(rgba))

    def _on_xsec_done(self, pixmap: QPixmap):
        if pixmap and not pixmap.isNull():
            self._canvas.set_pixmap_data(pixmap)

    def _on_xsec_error(self, err: str):
        print(f"[XSection] {err}")
        self._status.setText(f"X-Section error — see console for details")

    # ── Cluster interaction ───────────────────────────────────────────────────

    def _on_cluster_select(self, tx: int, tz: int):
        self.tile_selected.emit(tx, tz)

    def _on_cluster_radius(self, radius: int):
        for i, b in enumerate(self._cluster_sz_btns):
            b.setChecked([1, 2, 3][i] == radius)
        self._cluster.set_radius(radius)

    def _on_biome_cluster_toggle(self, checked: bool):
        self._cluster.set_biome_mode(checked)


# ---------------------------------------------------------------------------
# Tool B — Live Config Panel
# ---------------------------------------------------------------------------

class HistogramWidget(QWidget):
    """
    Thin bar-chart widget (28px tall) showing a mask value distribution.
    A vertical amber line tracks the current threshold position.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._hist:      Optional[np.ndarray] = None   # (n_bins,) float [0,1] normalised
        self._threshold: float = 0.5
        self.setFixedHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, samples: np.ndarray, n_bins: int = 60):
        counts, _ = np.histogram(samples, bins=n_bins, range=(0.0, 1.0))
        mx = counts.max()
        self._hist = counts / mx if mx > 0 else counts.astype(float)
        self.update()

    def set_threshold(self, v_norm: float):
        self._threshold = float(np.clip(v_norm, 0.0, 1.0))
        self.update()

    def paintEvent(self, _e):
        if self._hist is None:
            return
        p = QPainter(self)
        W, H = self.width(), self.height()
        n    = len(self._hist)
        bw   = W / n
        bar_color  = QColor(C_BORDER)
        # Bars
        for i, h in enumerate(self._hist):
            x  = int(i * bw)
            bh = max(1, int(h * (H - 3)))
            p.fillRect(x, H - bh, max(1, int(bw) + 1), bh, bar_color)
        # Threshold line
        tx = int(self._threshold * W)
        p.setPen(QPen(QColor(C_SEL), 2))
        p.drawLine(tx, 0, tx, H)
        p.end()


# ---------------------------------------------------------------------------
# Spline Editor — height spline (Gaea raw → MC Y) with PCHIP interpolation
# ---------------------------------------------------------------------------

class SplineEditorWidget(QWidget):
    """
    Draggable PCHIP spline editor.  Edits terrain_spline in thresholds.json.
    Left-drag to move breakpoints; right-click to add/remove; Apply to save.
    Sea-level pin (17050, 63) is locked — cannot be moved or deleted.
    """

    spline_applied  = pyqtSignal(list, list)  # gaea_in, mc_y_out (on Apply)
    spline_previewed = pyqtSignal(list, list) # gaea_in, mc_y_out (live drag)

    _SEA_Y   = 63
    _DEFAULT_PTS = [(0, -64), (17050, 63), (45000, 200), (65496, 448)]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._dragging: int | None = None
        self._pts: list[tuple[int, int]] = []
        self._load_breakpoints()
        self._build_ui()
        self._redraw()

    # ------------------------------------------------------------------
    def _load_breakpoints(self) -> None:
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            sp  = cfg.get("terrain_spline", {})
            gs  = sp.get("gaea_in",  [p[0] for p in self._DEFAULT_PTS])
            ys  = sp.get("mc_y_out", [p[1] for p in self._DEFAULT_PTS])
            self._pts = list(zip(map(int, gs), map(int, ys)))
        except Exception:
            self._pts = list(self._DEFAULT_PTS)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        if not _MATPLOTLIB_OK:
            layout.addWidget(QLabel(
                "matplotlib not found — install it to use the spline editor."))
            return

        self._fig = Figure(facecolor=C_BG)
        self._fig.subplots_adjust(left=0.13, right=0.97, top=0.91, bottom=0.13)
        self._ax  = self._fig.add_subplot(111)
        self._canvas = FigureCanvasQTAgg(self._fig)
        self._canvas.mpl_connect("button_press_event",   self._on_press)
        self._canvas.mpl_connect("motion_notify_event",  self._on_motion)
        self._canvas.mpl_connect("button_release_event", self._on_release)
        layout.addWidget(self._canvas, stretch=3)

        # Breakpoint table
        self._table = QTableWidget(len(self._pts), 2)
        self._table.setHorizontalHeaderLabels(["Gaea raw", "MC Y"])
        self._table.setMaximumHeight(80)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setStyleSheet(
            f"background:{C_PANEL}; color:{C_TEXT}; font-size:10px; "
            f"QHeaderView::section {{ background:{C_BG}; color:{C_MUTED}; }}")
        layout.addWidget(self._table, stretch=0)

        # Apply / Reset buttons
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 2, 0, 2)
        for label, slot in (("Apply", self._on_apply), ("Reset", self._on_reset)):
            btn = QPushButton(label)
            btn.setObjectName("accent")
            btn.clicked.connect(slot)
            bl.addWidget(btn)
        bl.addStretch()
        layout.addWidget(btn_row, stretch=0)

    # ------------------------------------------------------------------
    def _is_sea_level(self, idx: int) -> bool:
        """Sea-level point: Y is locked at 63, X (raw threshold) is draggable."""
        _, y = self._pts[idx]
        return y == self._SEA_Y

    def _is_endpoint(self, idx: int) -> bool:
        return idx == 0 or idx == len(self._pts) - 1

    # ------------------------------------------------------------------
    def _redraw(self) -> None:
        if not _MATPLOTLIB_OK:
            return
        from scipy.interpolate import PchipInterpolator
        ax = self._ax
        ax.cla()
        ax.set_facecolor("#0a1a2e")
        ax.set_xlim(0, 65535)
        ax.set_ylim(-64, 448)
        ax.tick_params(colors=C_MUTED, labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor(C_BORDER)
        ax.set_xlabel("Gaea raw (16-bit)", color=C_MUTED, fontsize=8)
        ax.set_ylabel("MC Y",              color=C_MUTED, fontsize=8)
        ax.set_title(
            "Height Spline  (drag orange · right-click add/remove)",
            color=C_TEXT, fontsize=8, pad=3)

        if len(self._pts) >= 2:
            gs = [p[0] for p in self._pts]
            ys = [p[1] for p in self._pts]
            xs = np.linspace(0, 65535, 600)
            ys_c = np.clip(PchipInterpolator(gs, ys)(xs), -64, 448)
            ax.plot(xs, ys_c, color=C_ACCENT, linewidth=1.5, zorder=3)

        ax.axhline(self._SEA_Y,  color="#00cccc", linewidth=0.8,
                   linestyle=":", alpha=0.55, zorder=2)
        # Find current sea-level raw X dynamically
        sea_raw = next((g for g, y in self._pts if y == self._SEA_Y), 17050)
        ax.axvline(sea_raw, color="#00cccc", linewidth=0.8,
                   linestyle=":", alpha=0.55, zorder=2)

        for i, (g, y) in enumerate(self._pts):
            color = "#00cccc" if self._is_sea_level(i) else "#ff8844"
            ax.plot(g, y, "o", color=color, markersize=9, zorder=5,
                    markeredgecolor="#ffffff", markeredgewidth=0.8)

        self._hover_vline = ax.axvline(x=0, color="#ffdd88", linewidth=0.7,
                                       linestyle="--", alpha=0.0, zorder=6)
        self._hover_hline = ax.axhline(y=0, color="#ffdd88", linewidth=0.7,
                                       linestyle="--", alpha=0.0, zorder=6)
        self._hover_label = ax.text(
            0.02, 0.97, "", transform=ax.transAxes,
            color="#ffdd88", fontsize=8, va="top", ha="left", zorder=7,
            bbox=dict(facecolor="#0a1a2e", alpha=0.75, edgecolor="none", pad=2))

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
        if event.xdata is None or not _MATPLOTLIB_OK:
            return None
        PICK_PX = 14
        for i, (g, y) in enumerate(self._pts):
            disp  = self._ax.transData.transform((g, y))
            mouse = np.array([event.x, event.y])
            if float(np.linalg.norm(disp - mouse)) < PICK_PX:
                return i
        return None

    # ------------------------------------------------------------------
    def _on_press(self, event) -> None:
        if event.inaxes is None:
            return
        if event.button == 1:
            idx = self._hit_test(event)
            if idx is not None:
                self._dragging = idx
        elif event.button == 3:
            idx = self._hit_test(event)
            if idx is not None:
                if not self._is_sea_level(idx) and not self._is_endpoint(idx):
                    self._pts.pop(idx)
                    self._redraw()
            elif event.xdata is not None and event.ydata is not None:
                new_g = int(np.clip(event.xdata, 1, 65534))
                new_y = int(np.clip(event.ydata, -64, 448))
                insert_at = len(self._pts)
                for i, (g, _) in enumerate(self._pts):
                    if g > new_g:
                        insert_at = i
                        break
                self._pts.insert(insert_at, (new_g, new_y))
                self._redraw()

    def _on_motion(self, event) -> None:
        if self._dragging is None:
            if event.inaxes is self._ax and event.xdata is not None:
                from scipy.interpolate import PchipInterpolator
                gs = [p[0] for p in self._pts]
                ys = [p[1] for p in self._pts]
                mc_y = float(np.clip(PchipInterpolator(gs, ys)(event.xdata), -64, 448))
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
        x_min = (gs[idx - 1] + 1) if idx > 0                  else 0
        x_max = (gs[idx + 1] - 1) if idx < len(self._pts) - 1 else 65535
        new_g = int(np.clip(event.xdata, x_min, x_max))
        new_y = int(np.clip(event.ydata, -64, 448))
        # Endpoints: lock X at 0 / 65535
        if idx == 0:
            new_g = 0
        elif idx == len(self._pts) - 1:
            new_g = 65535
        # Sea-level point: lock Y at 63, allow X drag
        if self._is_sea_level(idx):
            new_y = self._SEA_Y
        self._pts[idx] = (new_g, new_y)
        self._redraw()
        # Live preview — rebuild LUT + cross-section without saving to disk
        self.spline_previewed.emit(
            [p[0] for p in self._pts],
            [p[1] for p in self._pts])

    def _on_release(self, event) -> None:
        self._dragging = None

    # ------------------------------------------------------------------
    def _on_apply(self) -> None:
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            if "terrain_spline" not in cfg:
                cfg["terrain_spline"] = {}
            cfg["terrain_spline"]["gaea_in"]  = [p[0] for p in self._pts]
            cfg["terrain_spline"]["mc_y_out"] = [p[1] for p in self._pts]
            # Update sea_level_16bit if the sea-level point was moved on X axis
            sea_raw = next((g for g, y in self._pts if y == self._SEA_Y), None)
            if sea_raw is not None:
                cfg["sea_level_16bit"] = sea_raw
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
        except Exception as exc:
            print(f"[SplineEditor] save failed: {exc}", flush=True)
            return
        self.spline_applied.emit(
            [p[0] for p in self._pts],
            [p[1] for p in self._pts])

    def _on_reset(self) -> None:
        self._load_breakpoints()
        self._redraw()


class ConfigPanel(QWidget):
    """
    Live editor for config/thresholds.json.
    Sliders for key thresholds; Save button writes back to disk.
    """
    config_saved = pyqtSignal(dict)   # emitted after successful save

    # Maps config key → mask key used for histogram data
    _HIST_MASK: dict[str, str] = {
        "steep":              "slope",
        "very_steep":         "slope",
        "alpine_exposure_y":  "height",
        "frost_ridge_y":      "height",
        "frost_ridge_deg":    "slope",
        "band_scale_y":       None,
        "cliff_deg_thr":      "slope",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cfg: dict = {}
        self._sliders:    dict[str, QSlider]        = {}
        self._value_labels: dict[str, QLabel]       = {}
        self._histograms: dict[str, HistogramWidget] = {}
        self._hist_data:  dict[str, np.ndarray]     = {}   # mask key → samples

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QLabel("Live Configuration")
        header.setStyleSheet(
            f"color:{C_ACCENT}; font-weight:bold; font-size:14px;")
        layout.addWidget(header)

        note = QLabel(
            f"Editing  {CONFIG_PATH.name}  — click Save to persist changes.")
        note.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        note.setWordWrap(True)
        layout.addWidget(note)

        self._load_config()

        # Slope thresholds
        slope_group = QGroupBox("Slope Thresholds")
        sf = QFormLayout(slope_group)
        sf.setContentsMargins(8, 8, 8, 8)
        self._add_float_slider(sf, "steep",       0.01, 1.00, 0.001,
                               self._cfg.get("steep", 0.65))
        self._add_float_slider(sf, "very_steep",  0.01, 1.00, 0.001,
                               self._cfg.get("very_steep", 0.35))
        layout.addWidget(slope_group)

        # Geo surface thresholds
        geo_group = QGroupBox("Geo Surface Thresholds")
        gf = QFormLayout(geo_group)
        gf.setContentsMargins(8, 8, 8, 8)
        geo = self._cfg.get("geo_surface", {})
        self._add_int_slider(gf, "alpine_exposure_y",  100, 448,
                             geo.get("alpine_exposure_y", 340))
        self._add_int_slider(gf, "frost_ridge_y",       100, 448,
                             geo.get("frost_ridge_y", 300))
        self._add_int_slider(gf, "frost_ridge_deg",      10,  80,
                             geo.get("frost_ridge_deg", 35))
        layout.addWidget(geo_group)

        # Cliff banding
        cliff_group = QGroupBox("Cliff Banding")
        clf = QFormLayout(cliff_group)
        clf.setContentsMargins(8, 8, 8, 8)
        cb = self._cfg.get("cliff_banding", {})
        self._add_int_slider(clf, "band_scale_y",  4, 48,
                             cb.get("band_scale_y", 12))
        self._add_int_slider(clf, "cliff_deg_thr", 10, 80,
                             int(float(cb.get("cliff_deg_thr", 45))))
        layout.addWidget(cliff_group)

        # Save button
        self._save_btn = QPushButton("Save Config")
        self._save_btn.setObjectName("accent")
        self._save_btn.clicked.connect(self._save_config)
        layout.addWidget(self._save_btn)

        # Clear sim cache (invalidates all cached biome previews)
        self._clear_cache_btn = QPushButton("Clear Sim Cache")
        self._clear_cache_btn.setToolTip(
            "Delete all cached biome sim results in output/hillshade_cache/.\n"
            "Next tile select will re-run the full biome sim (3-5s).")
        self._clear_cache_btn.clicked.connect(self._clear_sim_cache)
        layout.addWidget(self._clear_cache_btn)

        self._save_status = QLabel("")
        self._save_status.setStyleSheet(f"font-size:11px; color:{C_OK};")
        layout.addWidget(self._save_status)

        layout.addStretch()

    def set_histogram_data(self, data: dict):
        """Receive mask sample arrays from MaskHistogramLoader."""
        self._hist_data = data
        for key, hw in self._histograms.items():
            mask_key = self._HIST_MASK.get(key)
            if mask_key and mask_key in data:
                hw.set_data(data[mask_key])
                hw.setVisible(True)

    def _load_config(self):
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                self._cfg = json.load(f)
        else:
            self._cfg = {}

    def _add_float_slider(self, form, key: str,
                          lo: float, hi: float, step: float, val: float):
        """Add a float-valued slider with histogram backing (scaled as int × 1000)."""
        scale   = 1000
        i_lo    = int(lo * scale)
        i_hi    = int(hi * scale)
        i_val   = int(val * scale)
        slider  = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(i_lo, i_hi)
        slider.setValue(i_val)
        val_lbl = QLabel(f"{val:.3f}")
        val_lbl.setStyleSheet(
            f"color:{C_TEXT}; font-family:'Consolas','Courier New',monospace;")
        val_lbl.setMinimumWidth(50)

        hw = HistogramWidget()
        hw.set_threshold((val - lo) / (hi - lo) if hi > lo else 0.5)
        hw.setVisible(False)   # hidden until data arrives
        self._histograms[key] = hw

        def _on_change(v, lbl=val_lbl, s=scale, h=hw, _lo=lo, _hi=hi):
            lbl.setText(f"{v/s:.3f}")
            norm = (v/s - _lo) / (_hi - _lo) if _hi > _lo else 0.5
            h.set_threshold(norm)
        slider.valueChanged.connect(_on_change)

        col = QWidget()
        col_layout = QVBoxLayout(col)
        col_layout.setContentsMargins(0, 0, 0, 0)
        col_layout.setSpacing(1)
        col_layout.addWidget(hw)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(slider)
        row_layout.addWidget(val_lbl)
        col_layout.addWidget(row)

        lbl = QLabel(key)
        lbl.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        form.addRow(lbl, col)
        self._sliders[key]      = slider
        self._value_labels[key] = val_lbl

    def _add_int_slider(self, form, key: str, lo: int, hi: int, val: int):
        """Add an integer-valued slider with histogram backing."""
        slider  = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(int(round(val)))
        val_lbl = QLabel(str(int(round(val))))
        val_lbl.setStyleSheet(
            f"color:{C_TEXT}; font-family:'Consolas','Courier New',monospace;")
        val_lbl.setMinimumWidth(50)

        hw = HistogramWidget()
        hw.set_threshold((val - lo) / (hi - lo) if hi > lo else 0.5)
        hw.setVisible(False)
        self._histograms[key] = hw

        def _on_change(v, lbl=val_lbl, h=hw, _lo=lo, _hi=hi):
            lbl.setText(str(v))
            h.set_threshold((v - _lo) / (_hi - _lo) if _hi > _lo else 0.5)
        slider.valueChanged.connect(_on_change)

        col = QWidget()
        col_layout = QVBoxLayout(col)
        col_layout.setContentsMargins(0, 0, 0, 0)
        col_layout.setSpacing(1)
        col_layout.addWidget(hw)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(slider)
        row_layout.addWidget(val_lbl)
        col_layout.addWidget(row)

        lbl = QLabel(key)
        lbl.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        form.addRow(lbl, col)
        self._sliders[key]      = slider
        self._value_labels[key] = val_lbl

    def _save_config(self):
        """Read slider values back into cfg and write to disk."""
        self._load_config()  # start from fresh disk copy

        # Slope
        if "steep" in self._sliders:
            self._cfg["steep"] = self._sliders["steep"].value() / 1000.0
        if "very_steep" in self._sliders:
            self._cfg["very_steep"] = self._sliders["very_steep"].value() / 1000.0

        # Geo surface
        if "geo_surface" not in self._cfg:
            self._cfg["geo_surface"] = {}
        for k in ("alpine_exposure_y", "frost_ridge_y", "frost_ridge_deg"):
            if k in self._sliders:
                self._cfg["geo_surface"][k] = self._sliders[k].value()

        # Cliff banding
        if "cliff_banding" not in self._cfg:
            self._cfg["cliff_banding"] = {}
        for k in ("band_scale_y", "cliff_deg_thr"):
            if k in self._sliders:
                self._cfg["cliff_banding"][k] = self._sliders[k].value()

        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(self._cfg, f, indent=2)
            self.config_saved.emit(self._cfg)
            self._save_status.setText("Saved successfully.")
            self._save_status.setStyleSheet(f"font-size:11px; color:{C_OK};")
        except Exception as e:
            self._save_status.setText(f"Save failed: {e}")
            self._save_status.setStyleSheet(f"font-size:11px; color:{C_ERR};")

    def _clear_sim_cache(self):
        """Delete all sim_*.pkl cache files from output/hillshade_cache/."""
        import glob as _glob
        deleted = 0
        for p in HILLSHADE_CACHE.glob("sim_*.pkl"):
            try:
                p.unlink()
                deleted += 1
            except Exception:
                pass
        self._save_status.setText(f"Sim cache cleared ({deleted} files removed).")
        self._save_status.setStyleSheet(f"font-size:11px; color:{C_OK};")


# ---------------------------------------------------------------------------
# Tool D — Biome Studio (scatterplot + draggable thresholds)
# ---------------------------------------------------------------------------

class BiomeStudioWidget(QWidget):
    """Height × Flow scatterplot with draggable biome threshold lines.

    Shows downsampled tile data colored by procedural biome assignment.
    Drag threshold lines to adjust terrain_class and moisture boundaries
    in real time; Apply persists changes to thresholds.json.
    """
    thresholds_changed = pyqtSignal(dict)

    _SEA_NORM    = 17050 / 65535.0   # ~0.260
    _LAND_RANGE  = 1.0 - _SEA_NORM  # ~0.740
    _DS          = 8                 # downsample factor: 512/8 = 64 → 4096 pts

    # Biome matrix: (terrain_class, flow_band) → biome name
    # flow_band: 0 = dry (<flow_mid), 1 = mid, 2 = wet (>=flow_high)
    _BIOME_MATRIX = {
        ("ocean",   0): "_OCEAN",     ("ocean",   1): "_OCEAN",     ("ocean",   2): "_OCEAN",
        ("coastal", 0): "SAND_DUNE_DESERT",  ("coastal", 1): "COASTAL_HEATH",  ("coastal", 2): "RAINFOREST_COAST",
        ("lowland", 0): "DRY_OAK_SAVANNA",   ("lowland", 1): "MIXED_FOREST",   ("lowland", 2): "TEMPERATE_DECIDUOUS",
        ("highland",0): "CONTINENTAL_STEPPE", ("highland",1): "BOREAL_TAIGA",   ("highland",2): "TEMPERATE_RAINFOREST",
        ("alpine",  0): "SNOWY_BOREAL_TAIGA",("alpine",  1): "SNOWY_BOREAL_TAIGA",  ("alpine",  2): "SNOWY_BOREAL_TAIGA",
        ("ice_cap", 0): "ARCTIC_TUNDRA",     ("ice_cap", 1): "ARCTIC_TUNDRA",  ("ice_cap", 2): "ARCTIC_TUNDRA",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._h_data: Optional[np.ndarray] = None       # (N,) height
        self._f_data: Optional[np.ndarray] = None       # (N,) flow
        self._ov_mask: Optional[np.ndarray] = None      # (N,) bool: True = override px
        self._cfg: dict = {}
        self._local_thr: dict = {}  # working copy of thresholds during drag
        self._dragging: Optional[dict] = None
        self._scatter = None
        self._ov_scatter = None
        self._lines: list[dict] = []  # {"artist", "key", "orient", "value", "label"}
        self._build_ui()
        self._load_config()

    # ── UI ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        if not _MATPLOTLIB_OK:
            layout.addWidget(QLabel("matplotlib not available"))
            return

        self._fig = Figure(facecolor=C_BG, dpi=80)
        self._fig.subplots_adjust(left=0.10, right=0.96, top=0.92, bottom=0.14)
        self._ax = self._fig.add_subplot(111)
        self._mpl_canvas = FigureCanvasQTAgg(self._fig)
        self._mpl_canvas.mpl_connect("button_press_event",  self._on_press)
        self._mpl_canvas.mpl_connect("motion_notify_event", self._on_motion)
        self._mpl_canvas.mpl_connect("button_release_event", self._on_release)
        layout.addWidget(self._mpl_canvas, stretch=1)

        # Button row
        btn_row = QHBoxLayout()
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setObjectName("accent")
        self._apply_btn.setToolTip("Save current thresholds to thresholds.json")
        self._apply_btn.clicked.connect(self._on_apply)
        btn_row.addWidget(self._apply_btn)
        self._reset_btn = QPushButton("Reset")
        self._reset_btn.setToolTip("Revert to saved thresholds.json values")
        self._reset_btn.clicked.connect(self._on_reset)
        btn_row.addWidget(self._reset_btn)
        self._status_lbl = QLabel("")
        self._status_lbl.setStyleSheet(f"font-size:10px; color:{C_MUTED};")
        btn_row.addWidget(self._status_lbl, stretch=1)
        layout.addLayout(btn_row)

    # ── Config ────────────────────────────────────────────────────────────
    def _load_config(self):
        try:
            with open(CONFIG_PATH) as f:
                self._cfg = json.load(f)
        except Exception:
            self._cfg = {}
        self._sync_local_thr()

    def _sync_local_thr(self):
        """Populate working thresholds from config."""
        tc = self._cfg.get("terrain_class", {})
        mo = self._cfg.get("moisture", {})
        s = self._SEA_NORM
        lr = self._LAND_RANGE
        self._local_thr = {
            "coastal_max":  s + tc.get("coastal_max_norm",  0.35) * lr,
            "lowland_max":  s + tc.get("lowland_max_norm",  0.55) * lr,
            "highland_max": s + tc.get("highland_max_norm", 0.72) * lr,
            "alpine_max":   s + tc.get("alpine_max_norm",   0.88) * lr,
            "flow_high":    mo.get("flow_high", 0.55),
            "flow_mid":     mo.get("flow_mid",  0.30),
        }

    def on_config_changed(self, cfg: dict):
        """Called externally when ConfigPanel saves."""
        self._cfg = cfg
        self._sync_local_thr()
        self._recompute_and_redraw()

    # ── Data update ───────────────────────────────────────────────────────
    def update_tile_data(self, h_norm: np.ndarray,
                         flow_norm: Optional[np.ndarray],
                         override_8bit: np.ndarray):
        """Receive new tile data from SimPreviewWorker; downsample and draw."""
        ds = self._DS
        h_ds = h_norm[::ds, ::ds]
        ov_ds = override_8bit[::ds, ::ds]
        if flow_norm is not None:
            f_ds = flow_norm[::ds, ::ds]
        else:
            f_ds = np.zeros_like(h_ds)
        self._h_data = h_ds.ravel()
        self._f_data = f_ds.ravel()
        self._ov_mask = (ov_ds.ravel() != 0)
        self._recompute_and_redraw()

    # ── Biome recomputation ──────────────────────────────────────────────
    def _assign_biomes_local(self) -> np.ndarray:
        """Assign biomes to downsampled points using current working thresholds."""
        h = self._h_data
        f = self._f_data
        thr = self._local_thr
        n = len(h)
        biomes = np.full(n, "MIXED_FOREST", dtype=object)

        # Terrain class
        tc = np.full(n, "ocean", dtype=object)
        land = h >= self._SEA_NORM
        tc[land & (h < thr["coastal_max"])]  = "coastal"
        tc[land & (h >= thr["coastal_max"])  & (h < thr["lowland_max"])]  = "lowland"
        tc[land & (h >= thr["lowland_max"])  & (h < thr["highland_max"])] = "highland"
        tc[land & (h >= thr["highland_max"]) & (h < thr["alpine_max"])]   = "alpine"
        tc[land & (h >= thr["alpine_max"])]  = "ice_cap"

        # Flow band: 0=dry, 1=mid, 2=wet
        fb = np.zeros(n, dtype=np.int8)
        fb[f >= thr["flow_mid"]]  = 1
        fb[f >= thr["flow_high"]] = 2

        # Matrix lookup
        for (tc_val, fb_val), biome in self._BIOME_MATRIX.items():
            mask = (tc == tc_val) & (fb == fb_val)
            biomes[mask] = biome

        return biomes

    # ── Drawing ──────────────────────────────────────────────────────────
    def _recompute_and_redraw(self):
        if self._h_data is None or not _MATPLOTLIB_OK:
            return
        biomes = self._assign_biomes_local()
        # Override pixels get "_OVERRIDE" label
        biomes[self._ov_mask] = "_OVERRIDE"

        # Map to colors
        colors = np.zeros((len(biomes), 4), dtype=float)
        for i, b in enumerate(biomes):
            if b == "_OVERRIDE":
                colors[i] = (0.5, 0.5, 0.5, 0.25)
            else:
                rgb = BIOME_COLORS.get(b, (128, 128, 128))
                colors[i] = (rgb[0]/255, rgb[1]/255, rgb[2]/255, 0.7)

        ax = self._ax
        ax.clear()

        # Scatter: override pixels behind, procedural on top
        ov = self._ov_mask
        if ov.any():
            ax.scatter(self._h_data[ov], self._f_data[ov],
                       c=colors[ov], s=3, marker='.', linewidths=0)
        proc = ~ov
        if proc.any():
            self._scatter = ax.scatter(
                self._h_data[proc], self._f_data[proc],
                c=colors[proc], s=6, marker='.', linewidths=0)

        # Threshold lines
        self._lines.clear()
        thr = self._local_thr

        # Sea level (fixed reference, not draggable)
        ax.axvline(self._SEA_NORM, color='cyan', ls='--', lw=0.8, alpha=0.5)
        ax.text(self._SEA_NORM, 1.02, 'sea', fontsize=7, color='cyan',
                ha='center', transform=ax.get_xaxis_transform())

        # Vertical height thresholds
        v_lines = [
            ("coastal_max",  "#e8e850", "coastal"),
            ("lowland_max",  "#e8a030", "lowland"),
            ("highland_max", "#e06030", "highland"),
            ("alpine_max",   "#e0e0e0", "alpine"),
        ]
        for key, color, label in v_lines:
            val = thr[key]
            ln = ax.axvline(val, color=color, lw=1.5, alpha=0.85)
            txt = ax.text(val, 1.02, label, fontsize=7, color=color,
                          ha='center', transform=ax.get_xaxis_transform())
            self._lines.append({"artist": ln, "key": key, "orient": "v",
                                "value": val, "label": txt, "color": color})

        # Horizontal flow thresholds
        h_lines = [
            ("flow_mid",  "#80b040", "flow_mid"),
            ("flow_high", "#40c080", "flow_high"),
        ]
        for key, color, label in h_lines:
            val = thr[key]
            ln = ax.axhline(val, color=color, lw=1.5, alpha=0.85)
            txt = ax.text(1.02, val, label, fontsize=7, color=color,
                          va='center', transform=ax.get_yaxis_transform())
            self._lines.append({"artist": ln, "key": key, "orient": "h",
                                "value": val, "label": txt, "color": color})

        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_xlabel("Height (normalized)", fontsize=9, color=C_MUTED)
        ax.set_ylabel("Flow (moisture proxy)", fontsize=9, color=C_MUTED)
        ax.set_title("Biome Studio", fontsize=10, color=C_ACCENT)
        ax.set_facecolor('#0a1a2e')
        ax.tick_params(colors=C_MUTED, labelsize=7)
        for spine in ax.spines.values():
            spine.set_color(C_BORDER)
        self._mpl_canvas.draw_idle()

    # ── Drag interaction ─────────────────────────────────────────────────
    def _on_press(self, event):
        if event.button != 1 or event.inaxes != self._ax:
            return
        # Hit test: find nearest threshold line within 8 display-pixels
        best, best_dist = None, 999
        for li in self._lines:
            if li["orient"] == "v":
                # Convert data x to display x
                disp = self._ax.transData.transform((li["value"], 0))[0]
                mouse = self._ax.transData.transform((event.xdata, 0))[0]
                d = abs(disp - mouse)
            else:
                disp = self._ax.transData.transform((0, li["value"]))[1]
                mouse = self._ax.transData.transform((0, event.ydata))[1]
                d = abs(disp - mouse)
            if d < 8 and d < best_dist:
                best, best_dist = li, d
        if best:
            self._dragging = best
            self._mpl_canvas.setCursor(
                Qt.CursorShape.SizeHorCursor if best["orient"] == "v"
                else Qt.CursorShape.SizeVerCursor)

    def _on_motion(self, event):
        if self._dragging is None or event.inaxes != self._ax:
            return
        li = self._dragging
        key = li["key"]
        thr = self._local_thr

        if li["orient"] == "v":
            val = max(self._SEA_NORM + 0.01, min(0.99, event.xdata))
            # Enforce ordering
            if key == "coastal_max":
                val = min(val, thr["lowland_max"] - 0.005)
            elif key == "lowland_max":
                val = max(val, thr["coastal_max"] + 0.005)
                val = min(val, thr["highland_max"] - 0.005)
            elif key == "highland_max":
                val = max(val, thr["lowland_max"] + 0.005)
                val = min(val, thr["alpine_max"] - 0.005)
            elif key == "alpine_max":
                val = max(val, thr["highland_max"] + 0.005)
            thr[key] = val
        else:
            val = max(0.01, min(0.99, event.ydata))
            if key == "flow_mid":
                val = min(val, thr["flow_high"] - 0.005)
            elif key == "flow_high":
                val = max(val, thr["flow_mid"] + 0.005)
            thr[key] = val

        self._recompute_and_redraw()

    def _on_release(self, event):
        if self._dragging:
            self._dragging = None
            self._mpl_canvas.setCursor(Qt.CursorShape.ArrowCursor)

    # ── Apply / Reset ────────────────────────────────────────────────────
    def _on_apply(self):
        """Persist current working thresholds to thresholds.json."""
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

        lr = self._LAND_RANGE
        s = self._SEA_NORM
        thr = self._local_thr

        # Convert absolute height thresholds back to normalized fractions
        tc = cfg.setdefault("terrain_class", {})
        tc["coastal_max_norm"]  = round((thr["coastal_max"]  - s) / lr, 4)
        tc["lowland_max_norm"]  = round((thr["lowland_max"]  - s) / lr, 4)
        tc["highland_max_norm"] = round((thr["highland_max"] - s) / lr, 4)
        tc["alpine_max_norm"]   = round((thr["alpine_max"]   - s) / lr, 4)

        mo = cfg.setdefault("moisture", {})
        mo["flow_high"] = round(thr["flow_high"], 4)
        mo["flow_mid"]  = round(thr["flow_mid"], 4)

        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)

        self._cfg = cfg
        self._status_lbl.setText("Thresholds saved.")
        self._status_lbl.setStyleSheet(f"font-size:10px; color:{C_OK};")
        self.thresholds_changed.emit(cfg)

    def _on_reset(self):
        self._load_config()
        self._recompute_and_redraw()
        self._status_lbl.setText("Reset to saved values.")
        self._status_lbl.setStyleSheet(f"font-size:10px; color:{C_MUTED};")


# ---------------------------------------------------------------------------
# World Alignment Panel
# ---------------------------------------------------------------------------

class AlignmentPanel(QWidget):
    """Edit override↔terrain alignment params and rebuild override.tif."""
    rebuild_requested = pyqtSignal()

    _SCRIPT = str(PROJECT_ROOT / "upscale_override_vectorized.py")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self._load_params()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        title = QLabel("Override ↔ Terrain Alignment")
        title.setStyleSheet(f"color:{C_ACCENT}; font-weight:bold; font-size:13px;")
        layout.addWidget(title)

        form = QFormLayout()
        form.setSpacing(4)

        self._flip_z = QCheckBox("Flip Z (vertical)")
        form.addRow("Flip Z:", self._flip_z)

        self._scale = QDoubleSpinBox()
        self._scale.setRange(0.90, 1.10)
        self._scale.setSingleStep(0.005)
        self._scale.setDecimals(3)
        form.addRow("Scale:", self._scale)

        self._smooth_s = QSpinBox()
        self._smooth_s.setRange(10, 1000)
        self._smooth_s.setSingleStep(50)
        form.addRow("Contour smooth S:", self._smooth_s)

        self._pre_jitter = QSpinBox()
        self._pre_jitter.setRange(0, 10)
        form.addRow("Pre-jitter passes:", self._pre_jitter)

        self._post_jitter = QSpinBox()
        self._post_jitter.setRange(0, 60)
        form.addRow("Post-jitter passes:", self._post_jitter)

        layout.addLayout(form)

        hint = QLabel(
            "These params control upscale_override_vectorized.py.\n"
            "Rebuild writes masks/override.tif (~90s).")
        hint.setStyleSheet(f"font-size:10px; color:{C_MUTED};")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        self._save_btn = QPushButton("Save Params")
        self._save_btn.clicked.connect(self._save_params)
        btn_row.addWidget(self._save_btn)
        self._rebuild_btn = QPushButton("Rebuild override.tif")
        self._rebuild_btn.setObjectName("accent")
        self._rebuild_btn.clicked.connect(self._rebuild)
        btn_row.addWidget(self._rebuild_btn)
        layout.addLayout(btn_row)

        self._status = QLabel("")
        self._status.setStyleSheet(f"font-size:10px; color:{C_MUTED};")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        layout.addStretch()

    def _load_params(self):
        """Read current values from the upscale script constants."""
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("upscale_mod", self._SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            # Only read constants, don't execute main
            with open(self._SCRIPT) as f:
                src = f.read()
            import re
            def _extract(name, default, cast=str):
                m = re.search(rf'^{name}\s*=\s*(.+?)(?:\s*#|$)', src, re.MULTILINE)
                if m:
                    try: return cast(m.group(1).strip())
                    except Exception: pass
                return default
            self._flip_z.setChecked(_extract("FLIP_Z", "False").strip() not in ("False", "0"))
            self._scale.setValue(float(_extract("ALIGN_SCALE", "1.01")))
            self._smooth_s.setValue(int(_extract("CONTOUR_SMOOTH_S", "200")))
            self._pre_jitter.setValue(int(_extract("JITTER_PASSES", "3")))
            self._post_jitter.setValue(int(_extract("POST_JITTER_PASSES", "25")))
        except Exception as e:
            self._status.setText(f"Load failed: {e}")

    def _save_params(self):
        """Write params back to the upscale script as constants."""
        try:
            with open(self._SCRIPT) as f:
                src = f.read()
            import re
            replacements = {
                "FLIP_Z":             str(self._flip_z.isChecked()),
                "ALIGN_SCALE":        f"{self._scale.value():.3f}",
                "CONTOUR_SMOOTH_S":   str(self._smooth_s.value()),
                "JITTER_PASSES":      str(self._pre_jitter.value()),
                "POST_JITTER_PASSES": str(self._post_jitter.value()),
            }
            for name, new_val in replacements.items():
                # Match: NAME = value  # optional comment
                src = re.sub(
                    rf'^({name}\s*=\s*)(.+?)(\s*#.*)?$',
                    lambda m, nv=new_val: f"{m.group(1)}{nv}{m.group(3) or ''}",
                    src, count=1, flags=re.MULTILINE)
            with open(self._SCRIPT, "w") as f:
                f.write(src)
            self._status.setText("Params saved to upscale script.")
            self._status.setStyleSheet(f"font-size:10px; color:{C_OK};")
        except Exception as e:
            self._status.setText(f"Save failed: {e}")
            self._status.setStyleSheet(f"font-size:10px; color:#e06060;")

    def _rebuild(self):
        """Run the upscale script in a background thread."""
        self._rebuild_btn.setEnabled(False)
        self._status.setText("Rebuilding override.tif...")
        self._status.setStyleSheet(f"font-size:10px; color:{C_ACCENT};")
        self._worker = _AlignRebuildWorker(self._SCRIPT)
        self._worker.done.connect(self._on_rebuild_done)
        self._worker.start()

    def _on_rebuild_done(self, ok: bool, msg: str):
        self._rebuild_btn.setEnabled(True)
        if ok:
            self._status.setText(f"Rebuild complete. {msg}")
            self._status.setStyleSheet(f"font-size:10px; color:{C_OK};")
            self.rebuild_requested.emit()
        else:
            self._status.setText(f"Rebuild failed: {msg}")
            self._status.setStyleSheet(f"font-size:10px; color:#e06060;")


class _AlignRebuildWorker(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, script_path: str):
        super().__init__()
        self._script = script_path

    def run(self):
        import subprocess, sys
        try:
            result = subprocess.run(
                [sys.executable, self._script],
                capture_output=True, text=True, timeout=1800)
            if result.returncode == 0:
                self.done.emit(True, "")
            else:
                self.done.emit(False, result.stderr[-300:] if result.stderr else "unknown error")
        except Exception as e:
            self.done.emit(False, str(e))


# ---------------------------------------------------------------------------
# Detachable Tab Widget
# ---------------------------------------------------------------------------

class DetachableTabWidget(QTabWidget):
    """QTabWidget with double-click-to-detach: opens the tab in a floating window.

    Double-click a tab header to pop it out. Close the floating window to
    re-dock it back into the tab bar at its original position.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._detached: dict[str, QWidget] = {}     # title → floating window
        self._original_widgets: dict[str, QWidget] = {}  # title → original widget
        self._original_indices: dict[str, int] = {}  # title → original tab index
        self.tabBar().installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj == self.tabBar() and event.type() == event.Type.MouseButtonDblClick:
            idx = self.tabBar().tabAt(event.pos())
            if idx >= 0:
                self._detach_tab(idx)
                return True
        return super().eventFilter(obj, event)

    def _detach_tab(self, idx: int):
        title = self.tabText(idx)
        widget = self.widget(idx)
        if title in self._detached:
            return  # already detached
        self._original_widgets[title] = widget
        self._original_indices[title] = idx

        # Create floating window
        win = QMainWindow()
        win.setWindowTitle(f"Vandir — {title}")
        win.resize(600, 500)
        win.setStyleSheet(self.styleSheet())

        # Remove widget from tab, reparent to floating window
        self.removeTab(idx)
        win.setCentralWidget(widget)
        widget.show()

        # Insert placeholder so tab indices don't shift
        placeholder = QLabel(f"{title} (detached — close window to re-dock)")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        self.insertTab(idx, placeholder, f"{title} *")

        win.closeEvent = lambda e, t=title: self._redock(t, e)
        win.show()
        self._detached[title] = win

    def _redock(self, title: str, event):
        if title not in self._detached:
            event.accept()
            return
        win = self._detached.pop(title)
        widget = self._original_widgets.pop(title)
        orig_idx = self._original_indices.pop(title, -1)

        # Find and remove the placeholder tab
        for i in range(self.count()):
            if self.tabText(i) == f"{title} *":
                self.removeTab(i)
                break

        # Re-insert at original position
        idx = min(orig_idx, self.count())
        self.insertTab(idx, widget, title)
        self.setCurrentIndex(idx)
        event.accept()


# ---------------------------------------------------------------------------
# Placeholder panel factory  (Tools F)
# ---------------------------------------------------------------------------

def _make_placeholder(title: str, subtitle: str, note: str) -> QWidget:
    """Create a styled placeholder widget for unimplemented tools."""
    w = QWidget()
    layout = QVBoxLayout(w)
    layout.setContentsMargins(20, 40, 20, 20)
    layout.setSpacing(12)

    t_lbl = QLabel(title)
    t_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    t_lbl.setStyleSheet(
        f"color:{C_ACCENT}; font-size:18px; font-weight:bold; "
        f"font-variant:small-caps; letter-spacing:2px;")
    layout.addWidget(t_lbl)

    s_lbl = QLabel(subtitle)
    s_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    s_lbl.setWordWrap(True)
    s_lbl.setStyleSheet(f"color:{C_TEXT}; font-size:12px; max-width:400px;")
    layout.addWidget(s_lbl)

    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.HLine)
    sep.setStyleSheet(f"color:{C_BORDER};")
    layout.addWidget(sep)

    n_lbl = QLabel(note)
    n_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    n_lbl.setStyleSheet(
        f"color:{C_MUTED}; font-size:11px; font-variant:small-caps; "
        f"letter-spacing:1px;")
    layout.addWidget(n_lbl)

    layout.addStretch()
    return w


# ---------------------------------------------------------------------------
# Annotation dialog + panel  (Tool G)
# ---------------------------------------------------------------------------

class AnnotationDialog(QDialog):
    """Modal dialog to add a new annotation at a tile."""
    def __init__(self, tx: int, tz: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Add note — tile ({tx}, {tz})")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel(f"Tile ({tx}, {tz})"))

        self._text = QTextEdit()
        self._text.setPlaceholderText("Enter note…")
        self._text.setMaximumHeight(80)
        layout.addWidget(self._text)

        sev_row = QHBoxLayout()
        sev_row.addWidget(QLabel("Severity:"))
        self._sev_btns: dict[str, QPushButton] = {}
        for sev, color in AnnotationStore.SEV_COLORS.items():
            btn = QPushButton(sev.capitalize())
            btn.setCheckable(True)
            btn.setFixedWidth(70)
            btn.setStyleSheet(
                f"QPushButton:checked{{background:rgb{color};color:#000;}}")
            self._sev_btns[sev] = btn
            sev_row.addWidget(btn)
        self._sev_btns["info"].setChecked(True)
        # Mutual exclusion
        for sev, btn in self._sev_btns.items():
            btn.clicked.connect(lambda _, s=sev: self._set_sev(s))
        sev_row.addStretch()
        layout.addLayout(sev_row)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _set_sev(self, active: str):
        for sev, btn in self._sev_btns.items():
            btn.setChecked(sev == active)

    def severity(self) -> str:
        for sev, btn in self._sev_btns.items():
            if btn.isChecked():
                return sev
        return "info"

    def text(self) -> str:
        return self._text.toPlainText().strip()


class AnnotationPanel(QWidget):
    """Tool G — annotation list with severity filter and resolve/delete actions."""
    navigate_to = pyqtSignal(int, int)   # tile to navigate to

    SEV_QSS = {
        "info":  f"color: rgb(100,160,255);",
        "warn":  f"color: rgb(245,160,50);",
        "error": f"color: rgb(248,113,113);",
    }

    def __init__(self, store: "AnnotationStore", parent=None):
        super().__init__(parent)
        self._store  = store
        self._filter = "all"   # "all" | "info" | "warn" | "error" | "open"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Filter bar
        fbar = QHBoxLayout()
        for label, key in [("All","all"),("Info","info"),
                            ("Warn","warn"),("Error","error"),("Open","open")]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == "all")
            btn.setFixedWidth(52)
            btn.clicked.connect(lambda _, k=key: self._set_filter(k))
            fbar.addWidget(btn)
            setattr(self, f"_fbtn_{key}", btn)
        fbar.addStretch()
        layout.addLayout(fbar)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(
            f"QListWidget{{background:{C_PANEL};border:1px solid {C_BORDER};}}"
            f"QListWidget::item{{padding:4px;}}"
            f"QListWidget::item:selected{{background:{C_ACCENT};color:{C_BG};}}")
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list, stretch=1)

        # Action buttons
        abar = QHBoxLayout()
        resolve_btn = QPushButton("Toggle Resolve")
        resolve_btn.clicked.connect(self._on_resolve)
        delete_btn  = QPushButton("Delete")
        delete_btn.clicked.connect(self._on_delete)
        abar.addWidget(resolve_btn)
        abar.addWidget(delete_btn)
        abar.addStretch()
        layout.addLayout(abar)

        self.refresh()

    def refresh(self):
        self._list.clear()
        self._indices = []
        for i, ann in enumerate(self._store.all()):
            sev  = ann.get("severity", "info")
            res  = ann.get("resolved", False)
            if self._filter == "open" and res:
                continue
            if self._filter in ("info","warn","error") and sev != self._filter:
                continue
            marker = "✓" if res else AnnotationStore.SEV_LABELS.get(sev, "•")
            text   = ann.get("text","")[:60]
            item   = QListWidgetItem(
                f"{marker}  ({ann['tx']},{ann['tz']})  {text}")
            item.setForeground(QColor(
                *(AnnotationStore.SEV_COLORS.get(sev,(200,200,200)))))
            if res:
                item.setForeground(QColor(C_MUTED))
            self._list.addItem(item)
            self._indices.append(i)

    def _set_filter(self, key: str):
        self._filter = key
        for k in ("all","info","warn","error","open"):
            getattr(self, f"_fbtn_{k}").setChecked(k == key)
        self.refresh()

    def _selected_store_idx(self) -> Optional[int]:
        row = self._list.currentRow()
        if row < 0 or row >= len(self._indices):
            return None
        return self._indices[row]

    def _on_double_click(self, item):
        idx = self._selected_store_idx()
        if idx is not None:
            ann = self._store.all()[idx]
            self.navigate_to.emit(ann["tx"], ann["tz"])

    def _on_resolve(self):
        idx = self._selected_store_idx()
        if idx is not None:
            self._store.toggle_resolve(idx)
            self.refresh()

    def _on_delete(self):
        idx = self._selected_store_idx()
        if idx is not None:
            self._store.delete(idx)
            self.refresh()


# ---------------------------------------------------------------------------
# World Studio — main window
# ---------------------------------------------------------------------------

class WorldStudio(QMainWindow):
    """
    Vandir World Studio — single-pane-of-glass integrated tool.
    Implements Tool A (World Map) as the left panel and tabs
    for Tool C (Inspect), E (3D), B (Config), D (Biome), F (Structures),
    G (Notes) on the right.
    """

    def __init__(self, init_tx: int = 48, init_tz: int = 48):
        super().__init__()
        self.setWindowTitle("Vandir World Studio")
        self.resize(1680, 960)
        self._init_tx = init_tx
        self._init_tz = init_tz
        self._manifest    = RenderManifest()
        self._annotations = AnnotationStore()
        self._hydro_store = HydroOverlayStore()
        self._current_layer   = "height"
        self._biome_tile_grid: list | None = None   # 97×97 list[list[str]]

        self._build_toolbar()
        self._build_central_widget()
        self._build_status_bar()
        self._wire_signals()
        self._map_view.set_manifest(self._manifest)
        self._map_view.set_annotations(self._annotations)
        self._map_view.set_hydro(self._hydro_store)
        self._start_overview_load()
        self._start_histogram_load()

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_toolbar(self):
        tb = QToolBar("Main Toolbar", self)
        tb.setMovable(False)
        tb.setIconSize(QSize(16, 16))

        title = QLabel("  VANDIR  WORLD  STUDIO  ")
        title.setStyleSheet(
            f"color:{C_ACCENT}; font-size:14px; font-weight:bold; "
            f"font-variant:small-caps; letter-spacing:3px; "
            f"font-family:'Segoe UI','Arial',sans-serif;")
        tb.addWidget(title)

        sep1 = QFrame(); sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setStyleSheet(f"color:{C_BORDER};")
        sep1.setFixedHeight(20)
        tb.addWidget(sep1)

        # Layer toggles
        layer_lbl = QLabel("  Layer: ")
        layer_lbl.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        tb.addWidget(layer_lbl)

        self._height_btn = QPushButton("Height")
        self._height_btn.setCheckable(True)
        self._height_btn.setChecked(True)
        self._height_btn.setFixedWidth(70)
        self._height_btn.clicked.connect(lambda: self._set_layer("height"))
        tb.addWidget(self._height_btn)

        self._biome_btn = QPushButton("Biome")
        self._biome_btn.setCheckable(True)
        self._biome_btn.setChecked(False)
        self._biome_btn.setFixedWidth(70)
        self._biome_btn.clicked.connect(lambda: self._set_layer("biome"))
        tb.addWidget(self._biome_btn)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet(f"color:{C_BORDER};")
        sep2.setFixedHeight(20)
        tb.addWidget(sep2)

        self._refresh_btn = QPushButton("Refresh All")
        self._refresh_btn.setFixedWidth(90)
        self._refresh_btn.setToolTip(
            "Flush all caches and reload from disk.\n"
            "Use after regenerating override.tif or world_biome_map.png.")
        self._refresh_btn.clicked.connect(self._refresh_all)
        tb.addWidget(self._refresh_btn)

        # Spacer
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)

        # Selected tile display
        self._toolbar_tile_lbl = QLabel("Tile: —")
        self._toolbar_tile_lbl.setStyleSheet(
            f"color:{C_TEXT}; font-size:12px; "
            f"font-family:'Consolas','Courier New',monospace;")
        tb.addWidget(self._toolbar_tile_lbl)

        tb.addWidget(QLabel("  "))
        self.addToolBar(tb)

    def _build_central_widget(self):
        # Horizontal: world map panel (left) | right column (right)
        h_split = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(h_split)

        # ── Left — World Map panel (Tool A) with header bar ───────────────────
        map_container = QWidget()
        map_layout = QVBoxLayout(map_container)
        map_layout.setContentsMargins(0, 0, 0, 0)
        map_layout.setSpacing(0)

        # Header bar: [Grid][Iso]  ───  Grid: [opacity slider]
        map_header = QWidget()
        map_header.setFixedHeight(34)
        map_header.setStyleSheet(
            f"background:{C_PANEL}; border-bottom:1px solid {C_BORDER};")
        mh = QHBoxLayout(map_header)
        mh.setContentsMargins(6, 3, 6, 3)
        mh.setSpacing(5)

        self._map_grid_btn = QPushButton("Grid")
        self._map_grid_btn.setCheckable(True)
        self._map_grid_btn.setChecked(True)
        self._map_grid_btn.setFixedWidth(52)
        self._map_grid_btn.clicked.connect(lambda: self._set_map_mode("grid"))
        mh.addWidget(self._map_grid_btn)

        self._map_iso_btn = QPushButton("World Iso")
        self._map_iso_btn.setCheckable(True)
        self._map_iso_btn.setFixedWidth(80)
        self._map_iso_btn.clicked.connect(lambda: self._set_map_mode("iso"))
        mh.addWidget(self._map_iso_btn)

        sep_m = QFrame(); sep_m.setFrameShape(QFrame.Shape.VLine)
        sep_m.setStyleSheet(f"color:{C_BORDER};"); sep_m.setFixedHeight(18)
        mh.addWidget(sep_m)

        self._hydro_btn = QPushButton("Hydro")
        self._hydro_btn.setCheckable(True)
        self._hydro_btn.setChecked(True)
        self._hydro_btn.setFixedWidth(60)
        self._hydro_btn.setToolTip("Toggle hydrology overlay (rivers + lakes from precomputed masks)")
        self._hydro_btn.toggled.connect(self._on_hydro_toggled)
        mh.addWidget(self._hydro_btn)

        self._hydro_reload_btn = QPushButton("Reload Hydro")
        self._hydro_reload_btn.setFixedWidth(100)
        self._hydro_reload_btn.setToolTip("Reload hydrology masks from disk (after re-running precompute)")
        self._hydro_reload_btn.clicked.connect(self._on_hydro_reload)
        mh.addWidget(self._hydro_reload_btn)

        mh.addStretch()

        grid_lbl = QLabel("Grid:")
        grid_lbl.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
        mh.addWidget(grid_lbl)
        self._grid_alpha_sl = QSlider(Qt.Orientation.Horizontal)
        self._grid_alpha_sl.setRange(0, 100)
        self._grid_alpha_sl.setValue(60)
        self._grid_alpha_sl.setFixedWidth(80)
        self._grid_alpha_sl.setToolTip("Grid line opacity")
        self._grid_alpha_sl.valueChanged.connect(self._on_grid_alpha_change)
        mh.addWidget(self._grid_alpha_sl)

        map_layout.addWidget(map_header)

        # Stacked widget: 0=map grid, 1=world iso canvas
        self._map_stack = QStackedWidget()
        self._map_view = WorldMapView()
        self._map_stack.addWidget(self._map_view)
        self._world_iso_canvas = TileCanvas()
        self._world_iso_canvas.setMinimumSize(380, 380)
        self._map_stack.addWidget(self._world_iso_canvas)
        self._world_iso_loader: Optional[RegionIsoLoader] = None

        map_layout.addWidget(self._map_stack, stretch=1)
        h_split.addWidget(map_container)

        # Right — vertical: preview panel (top) | config tabs (bottom)
        v_split = QSplitter(Qt.Orientation.Vertical)

        self._preview_panel = PreviewPanel()
        v_split.addWidget(self._preview_panel)

        # Bottom config/tools tabs (double-click tab header to detach into window)
        self._tabs = DetachableTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setMaximumHeight(400)

        self._config_panel = ConfigPanel()
        _cfg_scroll = QScrollArea()
        _cfg_scroll.setWidgetResizable(True)
        _cfg_scroll.setFrameShape(QFrame.Shape.NoFrame)
        _cfg_scroll.setWidget(self._config_panel)
        _cfg_scroll.setStyleSheet(f"background:{C_BG};")
        self._tabs.addTab(_cfg_scroll, "Config")

        self._spline_editor = SplineEditorWidget()
        self._spline_editor.spline_applied.connect(self._on_spline_applied)
        self._spline_editor.spline_previewed.connect(self._on_spline_previewed)
        self._tabs.addTab(self._spline_editor, "Spline")

        self._palette_widget = PaletteEditorWidget()
        _pal_scroll = QScrollArea()
        _pal_scroll.setWidgetResizable(True)
        _pal_scroll.setFrameShape(QFrame.Shape.NoFrame)
        _pal_scroll.setWidget(self._palette_widget)
        _pal_scroll.setStyleSheet(f"background:{C_BG};")
        self._tabs.addTab(_pal_scroll, "Palette")

        self._biome_studio = BiomeStudioWidget()
        self._tabs.addTab(self._biome_studio, "Biome Studio")
        self._alignment_panel = AlignmentPanel()
        self._alignment_panel.rebuild_requested.connect(self._refresh_all)
        self._tabs.addTab(self._alignment_panel, "Alignment")
        self._tabs.addTab(
            _make_placeholder("Structure Placer",
                              "Visual map overlay for schematic placement.",
                              "Phase 3"),
            "Structures")
        self._annotation_panel = AnnotationPanel(self._annotations)
        self._annotation_panel.navigate_to.connect(self._on_annotation_navigate)
        self._tabs.addTab(self._annotation_panel, "Notes")

        v_split.addWidget(self._tabs)
        v_split.setSizes([680, 220])
        v_split.setStretchFactor(0, 1)
        v_split.setStretchFactor(1, 0)

        h_split.addWidget(v_split)
        h_split.setSizes([480, 1200])

    def _build_status_bar(self):
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Loading world overview…")

    def _wire_signals(self):
        self._map_view.tile_selected.connect(self._on_tile_selected)
        self._map_view.tile_hovered.connect(self._on_tile_hovered)
        self._preview_panel.pipeline_done.connect(self._on_pipeline_done)
        self._preview_panel.sim_complete.connect(self._on_sim_complete)
        self._preview_panel.tile_selected.connect(self._on_cluster_tile_selected)
        self._config_panel.config_saved.connect(self._on_config_saved)
        self._config_panel.config_saved.connect(self._biome_studio.on_config_changed)
        self._biome_studio.thresholds_changed.connect(self._on_config_saved)
        self._map_view.annotation_requested.connect(self._on_annotation_requested)
        # Start loading hydrology overlay
        self._start_hydro_load()

    def _on_annotation_requested(self, tx: int, tz: int):
        dlg = AnnotationDialog(tx, tz, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.text():
            self._annotations.add(tx, tz, dlg.text(), dlg.severity())
            self._annotation_panel.refresh()
            self._map_view.scene().update()
            # Switch to Notes tab so user sees it was saved
            self._tabs.setCurrentWidget(self._annotation_panel)

    def _on_annotation_navigate(self, tx: int, tz: int):
        self._map_view.select_tile_external(tx, tz)

    def _on_hydro_toggled(self, checked: bool):
        self._map_view.set_hydro_visible(checked)

    def _start_hydro_load(self):
        """Load hydrology overlay from precomputed masks."""
        self._hydro_worker = HydroOverlayLoader()
        self._hydro_worker.progress.connect(
            lambda msg: self._status_bar.showMessage(msg))
        self._hydro_worker.done.connect(self._on_hydro_loaded)
        self._hydro_worker.start()

    def _on_hydro_loaded(self, data):
        if data is None:
            self._status_bar.showMessage(
                "No hydrology overlay — run: python core/hydrology_precompute.py")
            return
        self._hydro_store.set_data(data)
        self._map_view._update_hydro_overlay()
        s = self._hydro_store.stats
        self._status_bar.showMessage(
            f"Hydrology: {s['river_px']} river px (max order {s['max_order']}), "
            f"{s['n_lakes']} lakes ({s['lake_px']} px)")

    def _on_hydro_reload(self):
        """Reload hydro masks from disk (e.g. after re-running precompute)."""
        self._hydro_store = HydroOverlayStore()
        self._map_view.set_hydro(self._hydro_store)
        self._start_hydro_load()

    def _on_config_saved(self, cfg: dict):
        self._manifest.mark_config_change(cfg)
        self._map_view.scene().update()

    def _on_spline_previewed(self, gaea_in: list, mc_y_out: list):
        """Live preview during drag — rebuild LUT + cross-section without saving."""
        _rebuild_lut(gaea_in, mc_y_out)
        if hasattr(self._preview_panel, '_h_norm') and self._preview_panel._h_norm is not None:
            self._preview_panel._render_from_h_norm()

    def _on_spline_applied(self, gaea_in: list, mc_y_out: list):
        """
        Spline editor applied — save already done in SplineEditorWidget._on_apply.
        Rebuilds the global height LUT so cross-sections and height previews
        immediately reflect the new spline without restarting.
        """
        _rebuild_lut(gaea_in, mc_y_out)

        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
            self._manifest.mark_config_change(cfg)
        except Exception:
            cfg = {}
        self._map_view.scene().update()
        try:
            self._preview_panel.cfg = cfg
        except Exception:
            pass

        if hasattr(self._preview_panel, '_h_norm') and self._preview_panel._h_norm is not None:
            self._preview_panel._render_from_h_norm()

        self._status_bar.showMessage(
            "Spline applied — height LUT rebuilt, previews updated.")

    def _start_histogram_load(self):
        self._hist_loader = MaskHistogramLoader()
        self._hist_loader.done.connect(self._config_panel.set_histogram_data)
        _start_thread(self._hist_loader)

    def _start_overview_load(self):
        self._overview_loader = OverviewLoader()
        self._overview_loader.done.connect(self._on_overview_ready)
        _start_thread(self._overview_loader)
        self._biome_overview_loader = BiomeOverviewLoader()
        self._biome_overview_loader.done.connect(self._on_biome_overview_ready)
        _start_thread(self._biome_overview_loader)

    # ------------------------------------------------------------------
    # Map panel mode (Grid ↔ World Iso) and grid opacity
    # ------------------------------------------------------------------

    def _set_map_mode(self, mode: str):
        self._map_grid_btn.setChecked(mode == "grid")
        self._map_iso_btn.setChecked(mode == "iso")
        if mode == "iso":
            self._map_stack.setCurrentIndex(1)
            self._load_world_iso()
        else:
            self._map_stack.setCurrentIndex(0)

    def _load_world_iso(self):
        self._world_iso_canvas.set_pixmap_data(None)
        self._status_bar.showMessage("Rendering world isometric…")
        try:
            if self._world_iso_loader and self._world_iso_loader.isRunning():
                self._world_iso_loader.wait(400)
        except RuntimeError:
            pass
        self._world_iso_loader = RegionIsoLoader(48, 48, 0)  # tx/tz ignored for world
        self._world_iso_loader.done.connect(self._on_world_iso_done)
        _start_thread(self._world_iso_loader)

    def _on_world_iso_done(self, h_norm):
        if h_norm is None:
            self._status_bar.showMessage("World iso render failed")
            return
        rgba = render_isometric(h_norm, v_scale=0.08)
        self._world_iso_canvas.set_pixmap_data(_rgba_to_qpixmap(rgba))
        self._status_bar.showMessage("World isometric rendered  |  click Grid to return to map")

    def _on_grid_alpha_change(self, v: int):
        self._map_view.set_grid_alpha(v)

    # ------------------------------------------------------------------
    # Toolbar layer toggle
    # ------------------------------------------------------------------

    def _set_layer(self, layer: str):
        self._height_btn.setChecked(layer == "height")
        self._biome_btn.setChecked(layer == "biome")
        self._current_layer = layer
        self._map_view.swap_layer(layer)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_overview_ready(self, pixmap):
        if pixmap:
            self._map_view._height_pixmap = pixmap
            # Only show if current layer is height (don't override biome view)
            if self._current_layer == "height":
                self._map_view._show_overview(pixmap)
                self._map_view.fitInView(
                    QRectF(0, 0, GRID_N, GRID_N),
                    Qt.AspectRatioMode.KeepAspectRatio)
            self._status_bar.showMessage(
                f"World map loaded  —  {GRID_N}×{GRID_N} tiles  "
                f"|  click any tile to inspect")
        else:
            self._status_bar.showMessage(
                f"Could not load height.tif — check MASKS_DIR ({MASKS_DIR})")

        tx, tz = self._init_tx, self._init_tz
        rect = QRectF(tx - 4, tz - 4, 9, 9)
        self._map_view.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        # select_tile_external emits tile_selected → _on_tile_selected → preview_panel.select_tile
        self._map_view.select_tile_external(tx, tz)

    def _on_biome_overview_ready(self, pixmap, grid):
        if pixmap:
            self._map_view.set_biome_overview(pixmap)
            self._biome_tile_grid = grid
            if self._current_layer == "biome":
                self._map_view.swap_layer("biome")

    def _refresh_all(self):
        """Flush every cache and reload from disk.

        Use after regenerating override.tif / world_biome_map.png externally.
        """
        # 1. Clear sim disk cache (pkl files)
        deleted = 0
        for p in HILLSHADE_CACHE.glob("sim_*.pkl"):
            try:
                p.unlink()
                deleted += 1
            except Exception:
                pass

        # 2. Clear in-memory h_norm cache in preview panel
        if hasattr(self._preview_panel, '_h_norm_cache'):
            self._preview_panel._h_norm_cache.clear()

        # 3. Clear thumbnail cache in map view
        if hasattr(self._map_view, '_thumb_cache'):
            self._map_view._thumb_cache.clear()
            # Also remove pixmap items from scene
            for item in list(self._map_view._thumb_items.values()):
                self._map_view._scene.removeItem(item)
            self._map_view._thumb_items.clear()
            self._map_view._thumb_loading.clear()

        # 4. Clear render manifest (all tiles back to 'none')
        self._manifest._data.clear()
        self._manifest._save()

        # 5. Reload biome overview PNG + height overview
        self._start_overview_load()

        # 6. Force map view repaint (picks up cleared overlays + reloads thumbs)
        self._map_view.scene().update()
        self._map_view.viewport().update()

        # 7. Re-run sim on current tile if one is selected
        if hasattr(self._preview_panel, '_tx') and self._preview_panel._tx >= 0:
            self._preview_panel.select_tile(
                self._preview_panel._tx, self._preview_panel._tz)

        self._status_bar.showMessage(
            f"Refreshed — {deleted} sim cache files cleared, "
            f"all in-memory caches flushed, overview reloaded.")

    def _on_tile_selected(self, tx: int, tz: int):
        wx0, wz0 = tx * TILE_PX, tz * TILE_PX
        reg_x, reg_z = tx // 2, tz // 2
        self._toolbar_tile_lbl.setText(f"Tile: ({tx}, {tz})")
        self._status_bar.showMessage(
            f"Tile ({tx}, {tz})  |  "
            f"X {wx0}–{wx0+511}, Z {wz0}–{wz0+511}  |  "
            f"r.{reg_x}.{reg_z}.mca")
        self._preview_panel.select_tile(tx, tz)

    def _on_cluster_tile_selected(self, tx: int, tz: int):
        """Cluster click picked a different tile — sync the world map."""
        self._map_view.select_tile_external(tx, tz)
        # tile_selected signal from map_view fires _on_tile_selected → preview_panel.select_tile

    def _on_tile_hovered(self, tx: int, tz: int):
        wx0, wz0 = tx * TILE_PX, tz * TILE_PX
        base = f"({tx}, {tz})  |  X {wx0}–{wx0+511}, Z {wz0}–{wz0+511}"
        extra = ""
        if self._current_layer == "biome" and self._biome_tile_grid:
            extra = f"  {self._biome_tile_grid[tz][tx]}"
        hydro_info = self._hydro_store.info_at(tx, tz)
        if hydro_info:
            extra += f"  | {hydro_info}"
        self._status_bar.showMessage(base + extra)

    def _on_sim_complete(self, tx: int, tz: int, biome_names: list):
        self._manifest.set_sim(tx, tz, self._preview_panel.cfg, biome_names)
        self._map_view.scene().update()
        self._palette_widget.update_biomes(biome_names)
        # Forward tile data to Biome Studio scatterplot
        result = getattr(self._preview_panel, 'last_sim_result', None)
        if result and result.get("h_norm") is not None:
            self._biome_studio.update_tile_data(
                result["h_norm"],
                result.get("flow_norm"),
                result.get("override_8bit", np.zeros_like(result["h_norm"], dtype=np.uint8)))

    def _on_pipeline_done(self, state: dict):
        tx, tz = state["tile_x"], state["tile_z"]
        biome_names = [b for b in state.get("biomes", []) if not b.startswith("_")]
        self._manifest.set_full(tx, tz, self._preview_panel.cfg, biome_names)
        self._map_view.scene().update()
        self._status_bar.showMessage(
            f"Tile ({tx}, {tz}) generated  "
            f"|  Y {state['surface_y'].min()} – {state['surface_y'].max()}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Vandir World Studio — integrated terrain editor")
    parser.add_argument("--tile-x", type=int, default=48,
                        help="Initial tile X to select (default: 48)")
    parser.add_argument("--tile-z", type=int, default=48,
                        help="Initial tile Z to select (default: 48)")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_QSS)

    win = WorldStudio(init_tx=args.tile_x, init_tz=args.tile_z)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
