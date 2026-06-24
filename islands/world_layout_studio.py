"""
world_layout_studio.py — lightweight Photoshop-style layout tool for placing
real-island DEMs around Vandir on an expandable world canvas.

Vandir (50k x 50k) sits on a wider canvas.  Each island heightmap is a draggable
layer you can move / scale / flip / rotate.  Scale defaults to LORE-ACCURATE
(Vandir = Great Britain by area => 1 block ~ 9.14 lore-metres), auto-derived from
the unrealheightmap filename (latitude + zoom -> real metres/pixel).  Export writes
islands/layout.json — the world offset + transform per island that the
offset-render pipeline (derive_masks_from_height.py) consumes.

Nothing here writes to mainland masks/ or config — it's pure layout authoring.

Run:  py islands/world_layout_studio.py
"""
from __future__ import annotations
import json
import math
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np
from PIL import Image

from PyQt6.QtCore import Qt, QPointF, QRectF, QSize
from PyQt6.QtGui import (QImage, QPixmap, QTransform, QPainter, QPen, QBrush,
                         QColor, QAction, QShortcut, QKeySequence)
from PyQt6.QtWidgets import (QApplication, QMainWindow, QGraphicsView,
                             QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
                             QGraphicsLineItem, QGraphicsSimpleTextItem, QWidget,
                             QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                             QDoubleSpinBox, QSpinBox, QListWidget, QListWidgetItem,
                             QCheckBox, QFileDialog, QGroupBox, QSlider, QMessageBox,
                             QScrollArea)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # so `import derive_masks_from_height` works when run from islands/
MASKS = ROOT / "masks"
CACHE = ROOT / "islands" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)
ERASE_DIR = ROOT / "islands" / "erase_masks"
ERASE_DIR.mkdir(parents=True, exist_ok=True)

WORLD = 50000                 # Vandir size in blocks
LORE_M_PER_BLOCK = 9.14       # Vandir = GB by area: sqrt(209000/2500)
DISPLAY_PX = 1200             # island display downscale (light)
VANDIR_BG_PX = 1500
DECLIP = True                 # dome clipped/saturated summit plateaus in the preview

# terrain spline (for nicer Vandir relief; islands hillshade raw directly)
_GIN = np.array([0,5000,12000,17050,18000,21000,26000,30000,35000,42000,50000,58000,65496], float)
_YOUT = np.array([-64,-45,25,63,67,78,110,145,180,360,490,610,700], float)

DOWNLOADS = Path.home() / "Downloads"
VEX_DEFAULT = 3.0             # vertical exaggeration (1 = true proportions; 3 = 3x taller)
APRON_BLOCKS = 220            # ocean blend halo rendered around each island (irregular footprint)

# filename -> island registry. peak_m = real summit (m) for realistic height;
# declip off for flat coral (no invented peaks); edge_clean = max-fraction-of-largest
# for border-sliver removal (1.0 = drop ALL frame-clipped pieces); place/rot/flip =
# default layout. Kostati + New Vincentia carry the user's exported placement.
REGISTRY = {
    # --- placed (stuck from exported layout.json) ---
    "17_288_-62_743_13_4096_4096_16bit.png":
        dict(name="New Vincentia (St Kitts/Nevis/Statia)", peak_m=1156, declip=True,
             place=(255, 6552), rot=115.0, flipz=True),
    "13_130_-61_239_13_4096_4096_16bit.png":
        dict(name="Kostati (St Vincent/Grenadines)", peak_m=1234, declip=True,
             place=(22232, 47159), rot=80.0, flipz=True),
    # --- Caribbean (tropical -> south) ---
    "11_060_-64_101_13_4096_4096_16bit.png":
        dict(name="Margarita", peak_m=987, declip=True, place=(54000, 10000)),
    "23_887_-74_652_13_4096_4096_16bit.png":
        dict(name="Bahamas (San Salvador/Rum Cay)", peak_m=40, declip=False, place=(40000, 60000)),
    "18_299_-63_070_13_4096_4096_16bit.png":
        dict(name="Anguilla / St Maarten + outliers", peak_m=424, declip=True, place=(-12000, 22000)),
    "10_941_-65_329_13_4096_4096_16bit.png":
        dict(name="La Tortuga", peak_m=40, declip=False, place=(44000, 52000)),
    "11_863_-66_783_13_4096_4096_16bit.png":
        dict(name="Los Roques (atoll)", peak_m=110, declip=False, place=(52000, 56000)),
    "12_445_-61_217_13_4096_4096_16bit.png":
        dict(name="Grenada outliers / Grenadines", peak_m=300, declip=True, place=(30000, 58000)),
    "21_395_-71_065_13_4096_4096_16bit.png":
        dict(name="Grand Turk / Caicos", peak_m=25, declip=False, place=(60000, 50000)),
    "22_358_-75_789_13_4096_4096_16bit.png":
        dict(name="Bahamas (Crooked/Acklins)", peak_m=30, declip=False, place=(48000, 64000)),
    "-1_509_149_782_13_4096_4096_16bit.png":
        dict(name="Admiralty Islands (Papua New Guinea)", peak_m=150, declip=False, place=(54000, 62000)),
    "14_834_-24_703_12_4096_4096_16bit.png":
        dict(name="Fogo (Cabo Verde volcano)", peak_m=2829, declip=True, place=(5000, 54000)),
    # --- Pacific atolls/volcanic ---
    "-20_529_166_479_13_4096_4096_16bit.png":
        dict(name="Ouvea atoll", peak_m=40, declip=False, place=(58000, 30000)),
    "-21_008_167_833_13_4096_4096_16bit.png":
        dict(name="Loyalty islets", peak_m=90, declip=False, place=(62000, 38000)),
    "-17_622_168_398_13_4096_4096_16bit.png":
        dict(name="Efate (Vanuatu)", peak_m=647, declip=True, place=(-12000, 44000)),
    # --- cold (north) ---
    "70_990_-8_476_12_4096_4096_16bit.png":
        dict(name="Jan Mayen (Arctic volcano)", peak_m=2277, declip=True, place=(15000, -16000)),
    "49_722_-54_097_13_4096_4096_16bit.png":
        dict(name="Fogo Island (Newfoundland, cold)", peak_m=150, declip=True, place=(34000, -15000)),
    # --- wet fjord-karst (west) ---
    "-50_393_-75_223_12_4096_4096_16bit.png":
        dict(name="Madre de Dios (fjords/karst)", peak_m=800, declip=True, edge_clean=1.0, place=(-16000, 30000)),
}


# ── helpers ─────────────────────────────────────────────────────────────────

def parse_uhm(name: str):
    """unrealheightmap filename: {lat}_{latdec}_{lon}_{londec}_{zoom}_{w}_{h}_16bit"""
    m = re.match(r"(-?\d+)_(\d+)_(-?\d+)_(\d+)_(\d+)_(\d+)_(\d+)_", name)
    if not m:
        return None
    lat = float(f"{m.group(1)}.{m.group(2)}")
    lon = float(f"{m.group(3)}.{m.group(4)}")
    return dict(lat=lat, lon=lon, zoom=int(m.group(5)),
                w=int(m.group(6)), h=int(m.group(7)))


def lore_blocks_per_px(lat: float, zoom: float) -> float:
    """Real metres/pixel from web-mercator zoom & latitude, / lore m-per-block."""
    deg_per_px = (360.0 / (2 ** zoom)) / 256.0
    km_per_deg_lng = 111.32 * math.cos(math.radians(lat))
    real_m_per_px = deg_per_px * km_per_deg_lng * 1000.0
    return real_m_per_px / LORE_M_PER_BLOCK


def hillshade(z, az=315, alt=45):
    az, alt = math.radians(az), math.radians(alt)
    gy, gx = np.gradient(z.astype(np.float64))
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    sh = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(sh, 0, 1)


def build_vandir_bg() -> Path:
    out = CACHE / "vandir_bg.png"
    if out.exists():
        return out
    import rasterio
    from rasterio.enums import Resampling
    with rasterio.open(str(MASKS / "height.tif")) as src:
        raw = src.read(1, out_shape=(VANDIR_BG_PX, VANDIR_BG_PX),
                       resampling=Resampling.average).astype(np.float64)
    mcy = np.interp(raw, _GIN, _YOUT)
    hs = (hillshade(mcy) * 255).astype(np.uint8)
    land = mcy > 63
    # tint: ocean dark blue-grey, land hillshade grey-green
    rgb = np.dstack([hs, hs, hs]).astype(np.uint8)
    rgb[~land] = (np.array([28, 42, 66]) + hs[~land, None] * 0.15).astype(np.uint8)
    Image.fromarray(rgb, "RGB").save(out)
    return out


def load_island_image(path: Path, declip=True, blocks_per_px=2.0, edge_clean=0.05):
    """Return (QImage rgba hillshade, sea_raw, full_w, full_h).  Alpha encodes the
    RENDER FOOTPRINT: land = opaque, a coastal APRON_BLOCKS halo = semi-transparent
    (the irregular blend zone we re-render), everything beyond = fully transparent
    (left as Vandir ocean).  declip per-island so flat coral isn't domed into a peak."""
    from scipy.ndimage import binary_dilation
    im = Image.open(str(path))
    if im.mode not in ("I", "I;16", "L", "F"):
        im = im.convert("I")
    full = np.asarray(im)
    fh, fw = full.shape
    # downscale FIRST, then clean/de-clip on the small preview array (fast; the
    # full-res cleanup is done at derive time for the real render)
    step = max(1, max(fh, fw) // DISPLAY_PX)
    z = full[::step, ::step].astype(np.float64)
    lo = np.round(z[z <= np.percentile(z, 60)]).astype(np.int64)
    vals, counts = np.unique(lo, return_counts=True)
    sea_raw = float(vals[counts.argmax()])
    try:
        from derive_masks_from_height import clean_edge_fragments, reconstruct_clipped_peaks
        z, _ = clean_edge_fragments(z, sea_raw, max_frac=edge_clean)   # drop frame-edge slivers
        if declip:
            z, _ = reconstruct_clipped_peaks(z, sea_raw)                    # dome volcanic
        else:
            z, _ = reconstruct_clipped_peaks(z, sea_raw, frac=0.0, noise_frac=0.015)  # roughen flat caps
    except Exception:
        pass
    hs = (hillshade(z) * 255).astype(np.uint8)
    land = z > sea_raw + (z.max() - sea_raw) * 0.02
    # apron halo: dilate land by APRON_BLOCKS, in display px (world_per_disp = bpp*step)
    apron_px = max(1, int(round(APRON_BLOCKS / max(blocks_per_px * step, 1e-6))))
    footprint = binary_dilation(land, iterations=apron_px)
    rgb = np.dstack([hs, np.clip(hs * 1.02, 0, 255), hs * 0.9]).astype(np.uint8)
    alpha = np.where(land, 255, np.where(footprint, 95, 0)).astype(np.uint8)
    mp = ERASE_DIR / (path.stem + ".png")           # restore saved erasing
    if mp.exists():
        try:
            em = np.asarray(Image.open(mp).convert("L"))
            if em.shape == alpha.shape:
                alpha = np.where(em > 0, 0, alpha).astype(np.uint8)
        except Exception:
            pass
    return _rgba(rgb, alpha), sea_raw, fw, fh


def _qimg_alpha(img):
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    h, w = img.height(), img.width()
    ptr = img.constBits(); ptr.setsize(h * w * 4)
    return np.frombuffer(ptr, np.uint8).reshape(h, w, 4)[:, :, 3].copy()


def _rgba(rgb, alpha):
    h, w = alpha.shape
    rgba = np.ascontiguousarray(np.dstack([rgb, alpha]).astype(np.uint8))
    return QImage(rgba.data, w, h, 4 * w, QImage.Format.Format_RGBA8888).copy()


# ── layer model ─────────────────────────────────────────────────────────────

@dataclass
class Layer:
    name: str
    dem_path: str
    world_x: float            # top-left, world blocks
    world_y: float
    blocks_per_px: float      # lore scale on the FULL dem
    full_w: int
    full_h: int
    sea_raw: float
    peak_m: float = 500.0     # real-world summit elevation (m) for realistic height
    declip: bool = True       # dome clipped summit (off for flat coral)
    flipx: bool = False
    flipz: bool = False
    rot_deg: float = 0.0      # free rotation, degrees
    opacity: float = 1.0
    base_disp_w: int = 0      # px width of the (unrotated) display image
    base_img: QImage = field(default=None, repr=False, compare=False)
    item: QGraphicsPixmapItem = field(default=None, repr=False, compare=False)
    base2disp: object = field(default=None, repr=False, compare=False)  # base-img px -> display px (for eraser)
    erased: bool = False      # whether the user has erased any land
    orig_img: QImage = field(default=None, repr=False, compare=False)   # pre-erase copy (for reset)
    undo_stack: list = field(default_factory=list, repr=False, compare=False)  # per-stroke snapshots

    def ds_factor(self):      # full-dem px per display px (scale is rotation-invariant)
        return self.full_w / max(self.base_disp_w, 1)

    def world_scale(self):    # world blocks per display px
        return self.blocks_per_px * self.ds_factor()

    def mcy_peak(self, vex):  # realistic summit MC-Y = sea + real_peak/9.14 * exaggeration
        return 63.0 + self.peak_m / LORE_M_PER_BLOCK * vex

    def footprint_w(self):    # world-block bbox of the (possibly rotated) island
        if self.item is not None and not self.item.pixmap().isNull():
            s = self.item.scale(); pm = self.item.pixmap()
            return pm.width() * s, pm.height() * s
        return self.blocks_per_px * self.full_w, self.blocks_per_px * self.full_h


class IslandItem(QGraphicsPixmapItem):
    def __init__(self, layer: Layer, on_moved):
        super().__init__()
        self.layer = layer
        self.on_moved = on_moved
        self.setFlag(QGraphicsPixmapItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsPixmapItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsPixmapItem.GraphicsItemFlag.ItemSendsScenePositionChanges, True)
        self.setZValue(10)

    def itemChange(self, change, value):
        if change == QGraphicsPixmapItem.GraphicsItemChange.ItemPositionHasChanged:
            self.layer.world_x = self.pos().x()
            self.layer.world_y = self.pos().y()
            if self.on_moved:
                self.on_moved(self.layer)
        return super().itemChange(change, value)


# ── view ────────────────────────────────────────────────────────────────────

class CanvasView(QGraphicsView):
    def __init__(self, scene, status_cb, measure_cb, erase_cb):
        super().__init__(scene)
        self.status_cb = status_cb
        self.measure_cb = measure_cb
        self.erase_cb = erase_cb
        self.measuring = False
        self.erasing = False
        self._mpts = []
        self.setRenderHints(QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMouseTracking(True)

    def wheelEvent(self, e):
        f = 1.2 if e.angleDelta().y() > 0 else 1 / 1.2
        self.scale(f, f)

    def mouseMoveEvent(self, e):
        p = self.mapToScene(e.position().toPoint())
        kmx = (p.x() - WORLD / 2) * LORE_M_PER_BLOCK / 1000
        kmy = (p.y() - WORLD / 2) * LORE_M_PER_BLOCK / 1000
        self.status_cb(f"world ({p.x():.0f}, {p.y():.0f}) blocks   "
                       f"lore from Vandir centre: ({kmx:+.1f}, {kmy:+.1f}) km")
        if self.erasing and (e.buttons() & Qt.MouseButton.LeftButton):
            self.erase_cb(p, False)
            return
        super().mouseMoveEvent(e)

    def mousePressEvent(self, e):
        if self.erasing and e.button() == Qt.MouseButton.LeftButton:
            self.erase_cb(self.mapToScene(e.position().toPoint()), True)   # start of stroke
            return
        if self.measuring and e.button() == Qt.MouseButton.LeftButton:
            self._mpts.append(self.mapToScene(e.position().toPoint()))
            if len(self._mpts) == 2:
                self.measure_cb(self._mpts[0], self._mpts[1])
                self._mpts = []
            return
        super().mousePressEvent(e)


# ── main window ─────────────────────────────────────────────────────────────

class Studio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vandir Island Layout Studio")
        self.resize(1500, 950)
        self.layers: list[Layer] = []
        self.margin = 30000      # canvas margin around Vandir (blocks)
        self.vex = VEX_DEFAULT   # vertical exaggeration (shared across islands)

        self.scene = QGraphicsScene()
        self._build_scene()
        self.view = CanvasView(self.scene, self._status, self._do_measure, self._erase_at)
        self.erase_radius = 600   # eraser brush radius, world blocks

        self._build_ui()
        QShortcut(QKeySequence("Ctrl+Z"), self).activated.connect(self._undo_erase)  # undo erase stroke
        self.scene.selectionChanged.connect(self._on_scene_selection)  # click island -> select layer
        self._auto_load_presets()
        self._load_layout()      # restore working positions from islands/layout.json if present
        self._fit()

    # scene ---------------------------------------------------------------
    def _build_scene(self):
        self.scene.clear()
        m = self.margin
        self.scene.setSceneRect(-m, -m, WORLD + 2 * m, WORLD + 2 * m)
        # ocean backdrop
        bg = QGraphicsRectItem(-m, -m, WORLD + 2 * m, WORLD + 2 * m)
        bg.setBrush(QBrush(QColor(18, 28, 46)))
        bg.setPen(QPen(Qt.PenStyle.NoPen)); bg.setZValue(-100)
        self.scene.addItem(bg)
        # Vandir background
        bgp = QPixmap(str(build_vandir_bg()))
        self.vandir_item = QGraphicsPixmapItem(bgp)
        self.vandir_item.setScale(WORLD / bgp.width())
        self.vandir_item.setZValue(0)
        self.scene.addItem(self.vandir_item)
        # Vandir border + label
        border = QGraphicsRectItem(0, 0, WORLD, WORLD)
        border.setPen(QPen(QColor(180, 200, 220), 120)); border.setZValue(1)
        self.scene.addItem(border)
        lab = QGraphicsSimpleTextItem("VANDIR  (50k x 50k)")
        f = lab.font(); f.setPixelSize(2600); f.setBold(True); lab.setFont(f)
        lab.setBrush(QBrush(QColor(210, 225, 240, 200)))
        lab.setPos(600, 600); lab.setZValue(1)
        self.scene.addItem(lab)
        self.measure_artifacts = []

    def _rebuild_scene_rect(self):
        m = self.margin
        self.scene.setSceneRect(-m, -m, WORLD + 2 * m, WORLD + 2 * m)
        # resize ocean backdrop (first item with zvalue -100)
        for it in self.scene.items():
            if isinstance(it, QGraphicsRectItem) and it.zValue() == -100:
                it.setRect(-m, -m, WORLD + 2 * m, WORLD + 2 * m)

    # ui ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget(); h = QHBoxLayout(central)
        h.addWidget(self.view, 4)
        panel = QWidget(); v = QVBoxLayout(panel)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(panel)
        scroll.setMinimumWidth(340); h.addWidget(scroll, 1)

        # canvas controls
        gb_c = QGroupBox("Canvas"); cv = QVBoxLayout(gb_c)
        row = QHBoxLayout(); row.addWidget(QLabel("margin (blocks):"))
        self.sp_margin = QSpinBox(); self.sp_margin.setRange(5000, 200000)
        self.sp_margin.setSingleStep(5000); self.sp_margin.setValue(self.margin)
        self.sp_margin.valueChanged.connect(self._on_margin)
        row.addWidget(self.sp_margin); cv.addLayout(row)
        rowv = QHBoxLayout(); rowv.addWidget(QLabel("vertical exag ×:"))
        self.sp_vex = QDoubleSpinBox(); self.sp_vex.setRange(1.0, 8.0); self.sp_vex.setSingleStep(0.5)
        self.sp_vex.setDecimals(1); self.sp_vex.setValue(self.vex)
        self.sp_vex.valueChanged.connect(self._on_vex); rowv.addWidget(self.sp_vex); cv.addLayout(rowv)
        bfit = QPushButton("Fit view"); bfit.clicked.connect(self._fit); cv.addWidget(bfit)
        v.addWidget(gb_c)

        # layers
        gb_l = QGroupBox("Layers  (click an island on the map to select it)"); lv = QVBoxLayout(gb_l)
        self.lst = QListWidget(); self.lst.currentRowChanged.connect(self._select)
        self.lst.setMinimumHeight(280)
        lv.addWidget(self.lst)
        rowlb = QHBoxLayout()
        ba = QPushButton("Add DEM…"); ba.clicked.connect(self._add_dialog); rowlb.addWidget(ba)
        bd = QPushButton("Remove"); bd.clicked.connect(self._remove); rowlb.addWidget(bd)
        lv.addLayout(rowlb)
        v.addWidget(gb_l)

        # transform
        gb_t = QGroupBox("Selected island"); tv = QVBoxLayout(gb_t)
        self.sp_x = self._spin(tv, "world X (blocks)", -200000, 250000, 500, self._apply_numeric)
        self.sp_y = self._spin(tv, "world Y (blocks)", -200000, 250000, 500, self._apply_numeric)
        self.sp_scale = self._spin(tv, "scale (blocks/px)", 0.05, 50.0, 0.05, self._apply_numeric, dec=3)
        blore = QPushButton("Set lore-accurate scale"); blore.clicked.connect(self._set_lore); tv.addWidget(blore)
        rowf = QHBoxLayout()
        self.cb_fx = QCheckBox("Flip X"); self.cb_fx.toggled.connect(self._apply_numeric)
        self.cb_fz = QCheckBox("Flip Z"); self.cb_fz.toggled.connect(self._apply_numeric)
        brot = QPushButton("Rotate 90°"); brot.clicked.connect(self._rotate90)
        rowf.addWidget(self.cb_fx); rowf.addWidget(self.cb_fz); rowf.addWidget(brot); tv.addLayout(rowf)
        # free rotation: slider 0-359 + numeric spin (centre-preserving)
        rowr = QHBoxLayout(); rowr.addWidget(QLabel("rotate°"))
        self.sl_rot = QSlider(Qt.Orientation.Horizontal); self.sl_rot.setRange(0, 359)
        self.sl_rot.valueChanged.connect(self._on_rot_slider)
        self.sp_rot = QDoubleSpinBox(); self.sp_rot.setRange(0, 359.9); self.sp_rot.setDecimals(1)
        self.sp_rot.setSingleStep(1.0); self.sp_rot.setFixedWidth(70)
        self.sp_rot.valueChanged.connect(self._on_rot_spin)
        rowr.addWidget(self.sl_rot); rowr.addWidget(self.sp_rot); tv.addLayout(rowr)
        rowo = QHBoxLayout(); rowo.addWidget(QLabel("opacity"))
        self.sl_op = QSlider(Qt.Orientation.Horizontal); self.sl_op.setRange(20, 100); self.sl_op.setValue(100)
        self.sl_op.valueChanged.connect(self._apply_numeric); rowo.addWidget(self.sl_op); tv.addLayout(rowo)
        self.lbl_info = QLabel("—"); self.lbl_info.setWordWrap(True); tv.addWidget(self.lbl_info)
        v.addWidget(gb_t)

        # tools
        gb_x = QGroupBox("Tools"); xv = QVBoxLayout(gb_x)
        self.b_measure = QPushButton("Measure distance (2 clicks)"); self.b_measure.setCheckable(True)
        self.b_measure.toggled.connect(self._toggle_measure); xv.addWidget(self.b_measure)
        self.lbl_measure = QLabel("—"); self.lbl_measure.setWordWrap(True); xv.addWidget(self.lbl_measure)
        # eraser
        self.b_erase = QPushButton("Eraser (drag on selected island)"); self.b_erase.setCheckable(True)
        self.b_erase.toggled.connect(self._toggle_erase); xv.addWidget(self.b_erase)
        rowe = QHBoxLayout(); rowe.addWidget(QLabel("brush (blocks):"))
        self.sp_erase = QSpinBox(); self.sp_erase.setRange(50, 5000); self.sp_erase.setSingleStep(100)
        self.sp_erase.setValue(self.erase_radius)
        self.sp_erase.valueChanged.connect(lambda v: setattr(self, "erase_radius", v))
        rowe.addWidget(self.sp_erase); xv.addLayout(rowe)
        rowu = QHBoxLayout()
        bundo = QPushButton("Undo stroke (Ctrl+Z)"); bundo.clicked.connect(self._undo_erase); rowu.addWidget(bundo)
        brst = QPushButton("Reset all"); brst.clicked.connect(self._reset_erase); rowu.addWidget(brst)
        xv.addLayout(rowu)
        bexp = QPushButton("Export layout.json"); bexp.clicked.connect(self._export); xv.addWidget(bexp)
        v.addWidget(gb_x)
        v.addStretch(1)

        self.setCentralWidget(central)
        self.status = self.statusBar()

    def _spin(self, layout, label, lo, hi, step, cb, dec=0):
        row = QHBoxLayout(); row.addWidget(QLabel(label))
        sp = QDoubleSpinBox(); sp.setRange(lo, hi); sp.setSingleStep(step); sp.setDecimals(dec)
        sp.valueChanged.connect(cb); row.addWidget(sp); layout.addLayout(row)
        return sp

    # layer ops -----------------------------------------------------------
    def _add_layer(self, path: Path, wx=None, wy=None, name=None):
        reg = REGISTRY.get(path.name, {})
        meta = parse_uhm(path.name)
        bpp = lore_blocks_per_px(meta["lat"], meta["zoom"]) if meta else 2.0
        declip = reg.get("declip", True); peak_m = reg.get("peak_m", 500.0)
        img, sea_raw, fw, fh = load_island_image(path, declip=declip, blocks_per_px=bpp,
                                                 edge_clean=reg.get("edge_clean", 0.05))
        if wx is None:
            wx, wy = reg.get("place", (WORLD + 5000, 5000))
        lyr = Layer(name=name or reg.get("name", path.stem), dem_path=str(path),
                    world_x=wx, world_y=wy, blocks_per_px=bpp, full_w=fw, full_h=fh,
                    sea_raw=sea_raw, peak_m=peak_m, declip=declip, base_img=img,
                    base_disp_w=img.width(), rot_deg=reg.get("rot", 0.0),
                    flipx=reg.get("flipx", False), flipz=reg.get("flipz", False))
        item = IslandItem(lyr, self._on_item_moved)
        lyr.item = item
        self.scene.addItem(item)
        self.layers.append(lyr)          # MUST precede _refresh_item: setPos fires the
        self._refresh_item(lyr)          # move callback, which looks lyr up in self.layers
        self.lst.addItem(QListWidgetItem(lyr.name))
        self.lst.setCurrentRow(len(self.layers) - 1)

    def _refresh_item(self, lyr: Layer, keep_center=None):
        """Rebuild the (flipped/rotated) pixmap. Scale is rotation-INVARIANT
        (world blocks per display px), so free rotation just bakes into the image
        and the expanded transparent bbox stays correctly sized. If keep_center is
        given (world cx,cy), reposition so the island stays centred (no drift)."""
        img = lyr.base_img
        W0, H0 = img.width(), img.height()
        mir = QTransform()                       # base-img px -> mirrored px
        if lyr.flipx:
            mir = QTransform(-1, 0, 0, 1, W0 - 1, 0) * mir
        if lyr.flipz:
            mir = QTransform(1, 0, 0, -1, 0, H0 - 1) * mir
        if lyr.flipx or lyr.flipz:
            img = img.mirrored(lyr.flipx, lyr.flipz)
        if lyr.rot_deg % 360:
            rt = QTransform().rotate(lyr.rot_deg)
            img = img.transformed(rt, Qt.TransformationMode.SmoothTransformation)
            lyr.base2disp = QImage.trueMatrix(rt, W0, H0) * mir   # base px -> display px
        else:
            lyr.base2disp = mir
        pm = QPixmap.fromImage(img)
        lyr.item.setPixmap(pm)
        s = lyr.world_scale()
        lyr.item.setScale(s)
        lyr.item.setOpacity(lyr.opacity)
        if keep_center is not None:
            cx, cy = keep_center
            lyr.world_x = cx - pm.width() * s / 2
            lyr.world_y = cy - pm.height() * s / 2
        lyr.item.setPos(lyr.world_x, lyr.world_y)

    def _on_item_moved(self, lyr):
        # called from the item's itemChange (a Qt C++ callback) — must never raise
        if lyr in self.layers and self.lst.currentRow() == self.layers.index(lyr):
            self._sync_spins(lyr)

    def _selected(self):
        i = self.lst.currentRow()
        return self.layers[i] if 0 <= i < len(self.layers) else None

    def _select(self, _):
        lyr = self._selected()
        if lyr:
            self._sync_spins(lyr); lyr.item.setSelected(True)

    def _on_scene_selection(self):
        # clicking an island on the canvas -> switch the layer panel to it
        items = self.scene.selectedItems()
        it = next((i for i in items if isinstance(i, IslandItem)), None)
        if it is None or it.layer not in self.layers:
            return
        idx = self.layers.index(it.layer)
        if self.lst.currentRow() != idx:
            self.lst.blockSignals(True); self.lst.setCurrentRow(idx); self.lst.blockSignals(False)
            self._sync_spins(it.layer)

    def _sync_spins(self, lyr):
        ws = (self.sp_x, self.sp_y, self.sp_scale, self.cb_fx, self.cb_fz,
              self.sl_op, self.sl_rot, self.sp_rot)
        for w in ws:
            w.blockSignals(True)
        self.sp_x.setValue(lyr.world_x); self.sp_y.setValue(lyr.world_y)
        self.sp_scale.setValue(lyr.blocks_per_px)
        self.cb_fx.setChecked(lyr.flipx); self.cb_fz.setChecked(lyr.flipz)
        self.sl_op.setValue(int(lyr.opacity * 100))
        self.sl_rot.setValue(int(lyr.rot_deg) % 360); self.sp_rot.setValue(lyr.rot_deg)
        for w in ws:
            w.blockSignals(False)
        fw_b, fh_b = lyr.footprint_w()
        peak_y = lyr.mcy_peak(self.vex)
        self.lbl_info.setText(
            f"{lyr.name}\nbbox ~{fw_b:.0f} × {fh_b:.0f} blocks "
            f"({fw_b/512:.1f} × {fh_b/512:.1f} tiles)\n"
            f"lore size ~{fw_b*LORE_M_PER_BLOCK/1000:.1f} × {fh_b*LORE_M_PER_BLOCK/1000:.1f} km\n"
            f"real peak {lyr.peak_m:.0f} m → MC-Y {peak_y:.0f} ({peak_y-63:.0f} above sea, {self.vex:.1f}× exag)\n"
            f"declip={'on' if lyr.declip else 'OFF (flat)'}  rot={lyr.rot_deg:.1f}°  sea_raw={lyr.sea_raw:.0f}")

    def _apply_numeric(self, *_):
        lyr = self._selected()
        if not lyr:
            return
        lyr.world_x = self.sp_x.value(); lyr.world_y = self.sp_y.value()
        lyr.blocks_per_px = self.sp_scale.value()
        lyr.flipx = self.cb_fx.isChecked(); lyr.flipz = self.cb_fz.isChecked()
        lyr.opacity = self.sl_op.value() / 100.0
        self._refresh_item(lyr); self._sync_spins(lyr)

    def _set_lore(self):
        lyr = self._selected()
        if not lyr:
            return
        meta = parse_uhm(Path(lyr.dem_path).name)
        if meta:
            lyr.blocks_per_px = lore_blocks_per_px(meta["lat"], meta["zoom"])
            self._refresh_item(lyr); self._sync_spins(lyr)
        else:
            QMessageBox.information(self, "Lore scale",
                                    "Filename isn't an unrealheightmap export — set scale manually.")

    def _set_rot(self, deg):
        """Set free rotation, preserving the island's centre (no drift)."""
        lyr = self._selected()
        if not lyr:
            return
        pm = lyr.item.pixmap(); s = lyr.item.scale()
        cx = lyr.world_x + pm.width() * s / 2
        cy = lyr.world_y + pm.height() * s / 2
        lyr.rot_deg = float(deg) % 360
        self._refresh_item(lyr, keep_center=(cx, cy))
        self._sync_spins(lyr)

    def _on_rot_slider(self, v):
        self._set_rot(float(v))

    def _on_rot_spin(self, v):
        self._set_rot(float(v))

    def _rotate90(self):
        lyr = self._selected()
        if lyr:
            self._set_rot((lyr.rot_deg + 90) % 360)

    def _add_dialog(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Add island DEM", str(DOWNLOADS),
                                            "Heightmaps (*.png *.tif *.tiff)")
        if fn:
            self._add_layer(Path(fn))

    def _remove(self):
        lyr = self._selected()
        if not lyr:
            return
        self.scene.removeItem(lyr.item)
        i = self.layers.index(lyr); self.layers.pop(i); self.lst.takeItem(i)

    def _auto_load_presets(self):
        for fn in REGISTRY:
            p = DOWNLOADS / fn
            if p.exists():
                self._add_layer(p)

    def _load_layout(self):
        """Restore saved positions/rot/flip from islands/layout.json (the user's
        working version). Islands not in the file keep their registry defaults."""
        f = ROOT / "islands" / "layout.json"
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text())
        except Exception:
            return
        by_path = {i.get("dem_path"): i for i in data.get("islands", [])}
        self.vex = data.get("canvas", {}).get("vex", self.vex)
        self.sp_vex.blockSignals(True); self.sp_vex.setValue(self.vex); self.sp_vex.blockSignals(False)
        n = 0
        for lyr in self.layers:
            e = by_path.get(lyr.dem_path)
            if not e:
                continue
            lyr.world_x, lyr.world_y = e["world_offset_px"]
            lyr.rot_deg = float(e.get("rot_deg", 0.0))
            lyr.flipx = bool(e.get("flipx", False)); lyr.flipz = bool(e.get("flipz", False))
            if e.get("blocks_per_px"):
                lyr.blocks_per_px = float(e["blocks_per_px"])
            self._refresh_item(lyr); n += 1
        self._status(f"restored {n} island positions from layout.json")

    # tools ---------------------------------------------------------------
    def _on_margin(self, v):
        self.margin = v; self._rebuild_scene_rect()

    def _on_vex(self, v):
        self.vex = v
        lyr = self._selected()
        if lyr:
            self._sync_spins(lyr)

    def _toggle_measure(self, on):
        self.view.measuring = on
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag if on
                              else QGraphicsView.DragMode.ScrollHandDrag)

    def _toggle_erase(self, on):
        if on and self.b_measure.isChecked():
            self.b_measure.setChecked(False)
        self.view.erasing = on
        self.view.setDragMode(QGraphicsView.DragMode.NoDrag if on
                              else QGraphicsView.DragMode.ScrollHandDrag)
        for l in self.layers:                  # lock movement so dragging erases, not moves
            l.item.setFlag(QGraphicsPixmapItem.GraphicsItemFlag.ItemIsMovable, not on)
        if not on:                             # finished erasing -> persist to disk
            for l in self.layers:
                self._save_erase(l)

    def _save_erase(self, lyr):
        mp = ERASE_DIR / (Path(lyr.dem_path).stem + ".png")
        if not lyr.erased or lyr.orig_img is None:
            if mp.exists():
                mp.unlink()                    # fully undone/reset -> clear saved erasing
            return
        cur, orig = _qimg_alpha(lyr.base_img), _qimg_alpha(lyr.orig_img)
        erased = (((orig > 0) & (cur == 0)).astype(np.uint8) * 255)
        Image.fromarray(erased, "L").save(mp)

    def _erase_at(self, scene_pt, start=False):
        lyr = self._selected()
        if not lyr or lyr.base2disp is None:
            return
        inv, ok = lyr.base2disp.inverted()
        if not ok:
            return
        bp = inv.map(lyr.item.mapFromScene(scene_pt))   # scene -> display px -> base px
        W0, H0 = lyr.base_img.width(), lyr.base_img.height()
        if not (-2 <= bp.x() <= W0 + 2 and -2 <= bp.y() <= H0 + 2):
            return
        if not lyr.erased:
            lyr.orig_img = QImage(lyr.base_img); lyr.erased = True
        if start:                                         # snapshot pre-stroke for Ctrl+Z
            lyr.undo_stack.append(QImage(lyr.base_img))
            del lyr.undo_stack[:-20]                       # cap history
        r = max(2.0, self.erase_radius / lyr.world_scale())   # world blocks -> base px
        qp = QPainter(lyr.base_img)
        qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        qp.setPen(Qt.PenStyle.NoPen); qp.setBrush(QColor(0, 0, 0, 0))
        qp.drawEllipse(bp, r, r); qp.end()
        pm = lyr.item.pixmap(); s = lyr.item.scale()
        self._refresh_item(lyr, keep_center=(lyr.world_x + pm.width()*s/2, lyr.world_y + pm.height()*s/2))

    def _undo_erase(self):
        """Ctrl+Z: undo the last erase stroke on the selected island."""
        lyr = self._selected()
        if not lyr or not lyr.undo_stack:
            return
        lyr.base_img = lyr.undo_stack.pop()
        lyr.erased = bool(lyr.undo_stack)
        pm = lyr.item.pixmap(); s = lyr.item.scale()
        self._refresh_item(lyr, keep_center=(lyr.world_x + pm.width()*s/2, lyr.world_y + pm.height()*s/2))
        self._save_erase(lyr)

    def _reset_erase(self):
        lyr = self._selected()
        if lyr and lyr.erased and lyr.orig_img is not None:
            lyr.base_img = QImage(lyr.orig_img); lyr.erased = False; lyr.undo_stack.clear()
            pm = lyr.item.pixmap(); s = lyr.item.scale()
            self._refresh_item(lyr, keep_center=(lyr.world_x + pm.width()*s/2, lyr.world_y + pm.height()*s/2))
            self._save_erase(lyr)

    def _do_measure(self, a: QPointF, b: QPointF):
        for it in self.measure_artifacts:
            self.scene.removeItem(it)
        self.measure_artifacts = []
        ln = QGraphicsLineItem(a.x(), a.y(), b.x(), b.y())
        ln.setPen(QPen(QColor(255, 210, 80), 140)); ln.setZValue(50)
        self.scene.addItem(ln); self.measure_artifacts.append(ln)
        d = math.hypot(b.x() - a.x(), b.y() - a.y())
        self.lbl_measure.setText(f"{d:.0f} blocks   =   {d*LORE_M_PER_BLOCK/1000:.1f} lore-km")

    def _status(self, msg):
        self.status.showMessage(msg)

    def _fit(self):
        self.view.fitInView(self.scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def _export(self):
        for l in self.layers:                  # persist any erasing alongside the layout
            self._save_erase(l)
        data = dict(
            canvas=dict(world=WORLD, margin=self.margin, lore_m_per_block=LORE_M_PER_BLOCK,
                        vex=self.vex, apron_blocks=APRON_BLOCKS),
            islands=[])
        for l in self.layers:
            fw_b, fh_b = l.footprint_w()
            data["islands"].append(dict(
                name=l.name, dem_path=l.dem_path,
                world_offset_px=[round(l.world_x), round(l.world_y)],
                blocks_per_px=round(l.blocks_per_px, 4),
                full_w=l.full_w, full_h=l.full_h, sea_raw=l.sea_raw,
                peak_m=l.peak_m, mcy_peak=round(l.mcy_peak(self.vex), 1), declip=l.declip,
                flipx=l.flipx, flipz=l.flipz, rot_deg=round(l.rot_deg, 1),
                footprint_blocks=[round(fw_b), round(fh_b)]))
        out = ROOT / "islands" / "layout.json"
        out.write_text(json.dumps(data, indent=2))
        QMessageBox.information(self, "Exported", f"Wrote {out}\n\n{len(self.layers)} island(s).")


def main():
    app = QApplication(sys.argv)
    w = Studio(); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
