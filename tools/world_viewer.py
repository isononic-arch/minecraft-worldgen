"""tools/world_viewer.py — Phase 0.5 (S44) MVP.

Spec: PHYSICAL_REALISM_REFACTOR.md §11 Phase 0.5 + §17 "World viewer".

A PyQt6 desktop GUI for visualizing generator output across the whole
50k×50k world at multiple zoom levels WITHOUT touching .mca files. The
existing `tools/world_studio.py` grew into a 6000-line kitchen sink;
this viewer narrows to a single job:

    Fast top-down review of precomputed masks + generator output tiles,
    overlaid on a hillshaded terrain base, with a cliff cross-section
    inset and the S43 surface palette editor migrated over.

Written blind (no PyQt6 sandbox). Architecture notes below are the
contract — if the runtime signature of anything in PyQt6.QtWidgets has
shifted, patch the affected class; the data-flow layer
(`TileCache`, `MaskOverlay`, `HillshadeBase`) has no Qt deps and is
importable from headless scripts for testing.

USAGE:
    py tools/world_viewer.py
    py tools/world_viewer.py --masks masks/ --config config/thresholds.json

LAYOUT:
    ┌────────────────────────┬──────────────────┐
    │                        │  Layer panel     │
    │  Main canvas           │  ──────────────  │
    │  (tile pyramid)        │  [x] hillshade   │
    │                        │  [x] biome LUT   │
    │  (pan: drag)           │  [ ] lithology   │
    │  (zoom: wheel)         │  [ ] wave_fetch  │
    │                        │  [ ] suitability │
    │                        │  [ ] ownership   │
    │                        │  [ ] contours    │
    │                        │                  │
    │                        │  Cliff inset     │
    │                        │  ┌────────────┐  │
    │                        │  │ cross-sect │  │
    │                        │  └────────────┘  │
    │                        │                  │
    │                        │  Palette editor  │
    │                        │  (from studio)   │
    └────────────────────────┴──────────────────┘

DATA FLOW:
    On open: scan masks/ for every .tif.
    TileCache: LRU dict keyed by (layer, zoom, tile_x, tile_y).
       Zoom 0 = full 6250×6250 precompute (1 px = 8 blocks).
       Zoom 1 = 1562×1562 half-res via block-average.
       Zoom 2 = 781×781 quarter.
       Zoom 3 = 390×390 eighth (overview).
    MainCanvas: paints the currently selected layers from topmost to
    bottom at the active zoom with alpha compositing. Pan/zoom via
    QGraphicsView's native transform so we don't reinvent the wheel.

NOT IN MVP:
    - Live regeneration (still done via validate_3x3.py + rebuild_*.py)
    - .mca rendering (that's schematic work; validator report does it)
    - Multi-monitor / tabbed views
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PRECOMPUTE_SIZE = 6250   # 1:8 of 50000


# ---------------------------------------------------------------------------
# Headless data-flow layer — no Qt deps, importable in tests.
# ---------------------------------------------------------------------------


@dataclass
class LayerSpec:
    """Description of a mask layer the viewer can render."""
    name: str
    path: Path
    kind: str                 # "gradient" | "discrete" | "dense"
    colormap: str = "viridis"
    alpha: float = 0.8
    enabled: bool = False
    z_order: int = 0


def _downsample_mean(arr: np.ndarray, factor: int) -> np.ndarray:
    if factor == 1:
        return arr
    H, W = arr.shape
    nH, nW = H // factor, W // factor
    clipped = arr[: nH * factor, : nW * factor]
    if arr.dtype.kind in ("u", "i"):
        tmp = clipped.astype(np.float32)
    else:
        tmp = clipped
    reshaped = tmp.reshape(nH, factor, nW, factor)
    out = reshaped.mean(axis=(1, 3))
    return out.astype(arr.dtype) if arr.dtype.kind == "u" else out


class TileCache:
    """LRU-ish cache of (layer_name, zoom) → ndarray.

    We cache the full downsampled raster per zoom, not per viewport tile,
    because even at z=0 (6250×6250) a full-res uint8 grayscale is only
    ~40MB — cheap for a 16GB dev box.
    """

    def __init__(self, max_layers: int = 32):
        self._cache: dict[tuple[str, int], np.ndarray] = {}
        self._max = max_layers

    def get(self, layer: LayerSpec, zoom: int) -> np.ndarray:
        key = (layer.name, zoom)
        if key in self._cache:
            return self._cache[key]
        arr = self._load_full(layer.path)
        factor = 1 << zoom  # 1, 2, 4, 8
        out = _downsample_mean(arr, factor)
        if len(self._cache) >= self._max:
            # evict oldest (FIFO approximation)
            oldest = next(iter(self._cache))
            self._cache.pop(oldest)
        self._cache[key] = out
        return out

    @staticmethod
    def _load_full(path: Path) -> np.ndarray:
        try:
            import rasterio
        except ImportError as e:
            raise RuntimeError(f"rasterio required to load {path}") from e
        with rasterio.open(str(path)) as src:
            return src.read(1)


def discover_layers(masks_dir: Path) -> list[LayerSpec]:
    """Scan masks/ and build a default LayerSpec list."""
    # Hand-curated kinds per the existing validate_masks.py registry.
    KIND_HINTS = {
        "height": "dense",
        "slope": "dense",
        "erosion": "dense",
        "override": "discrete",
        "shore": "discrete",
        "lithology": "discrete",
    }
    specs: list[LayerSpec] = []
    for p in sorted(masks_dir.glob("*.tif")):
        kind = KIND_HINTS.get(p.stem, "gradient")
        specs.append(LayerSpec(name=p.stem, path=p, kind=kind))
    return specs


# ---------------------------------------------------------------------------
# Hillshade — reused by the viewer base layer.
# ---------------------------------------------------------------------------


def compute_hillshade(
    height: np.ndarray,
    *,
    azimuth_deg: float = 315.0,
    altitude_deg: float = 45.0,
    vertical_exaggeration: float = 1.0,
) -> np.ndarray:
    """Classic Horn hillshade. Returns (H, W) float32 in [0, 1].

    Default azimuth 315° (NW light) matches USGS convention. For Vandir we
    may eventually want to show the tradewind direction (270° from west)
    as an alternate preset — exposed as a kwarg for now.
    """
    h = height.astype(np.float32)
    dzdy, dzdx = np.gradient(h)
    dzdx *= vertical_exaggeration
    dzdy *= vertical_exaggeration
    slope = np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2))
    aspect = np.arctan2(-dzdx, dzdy)  # note: matches MATLAB/USGS convention
    az_rad = np.deg2rad(360.0 - azimuth_deg + 90.0)
    alt_rad = np.deg2rad(altitude_deg)
    shade = (
        np.sin(alt_rad) * np.cos(slope)
        + np.cos(alt_rad) * np.sin(slope) * np.cos(az_rad - aspect)
    )
    return np.clip(shade, 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------------------
# Cliff cross-section extractor (used by CliffInset widget).
# ---------------------------------------------------------------------------


def extract_cliff_section(
    height: np.ndarray,
    *,
    center_xy: tuple[int, int],
    length_px: int = 64,
    azimuth_deg: float = 90.0,
) -> np.ndarray:
    """Return a (length_px,) height profile along a straight transect.

    `azimuth_deg` = compass direction of the transect (90° = east).
    Coordinates are in whatever resolution `height` is in.
    """
    cx, cy = center_xy
    rad = np.deg2rad(azimuth_deg)
    dx = np.cos(rad)
    dy = -np.sin(rad)  # image y grows downward
    ts = np.linspace(-length_px / 2, length_px / 2, length_px)
    xs = (cx + ts * dx).astype(np.int32)
    ys = (cy + ts * dy).astype(np.int32)
    H, W = height.shape
    xs = np.clip(xs, 0, W - 1)
    ys = np.clip(ys, 0, H - 1)
    return height[ys, xs]


# ---------------------------------------------------------------------------
# Colormaps — minimal inline, no matplotlib dep.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _colormap_lut(name: str) -> np.ndarray:
    """Return (256, 3) uint8 LUT for a named colormap."""
    t = np.linspace(0, 1, 256)
    if name == "viridis":
        r = np.clip(-0.35 + 1.95 * t - 1.05 * t ** 2, 0, 1)
        g = np.clip(0.01 + 1.35 * t - 0.4 * t ** 2, 0, 1)
        b = np.clip(0.35 + 0.9 * t - 1.2 * t ** 2, 0, 1)
    elif name == "magma":
        r = np.clip(0.0 + 1.8 * t - 0.6 * t ** 2, 0, 1)
        g = np.clip(-0.1 + 0.3 * t + 0.5 * t ** 2, 0, 1)
        b = np.clip(0.1 + 1.4 * t - 1.3 * t ** 2, 0, 1)
    elif name == "terrain":
        r = np.clip(0.2 + 0.6 * t + 0.2 * t ** 2, 0, 1)
        g = np.clip(0.4 + 0.5 * t - 0.3 * t ** 2, 0, 1)
        b = np.clip(0.3 - 0.2 * t + 0.7 * t ** 2, 0, 1)
    else:  # grayscale fallback
        r = g = b = t
    lut = np.stack([r, g, b], axis=1)
    return (lut * 255).astype(np.uint8)


def apply_colormap(arr: np.ndarray, name: str) -> np.ndarray:
    """Map [0,1] float32 → (H, W, 3) uint8 RGB."""
    a = np.asarray(arr, dtype=np.float32)
    if a.dtype.kind != "f":
        a = a.astype(np.float32)
    lo, hi = float(a.min()), float(a.max())
    if hi - lo > 1e-9:
        a = (a - lo) / (hi - lo)
    idx = np.clip((a * 255).astype(np.int32), 0, 255)
    return _colormap_lut(name)[idx]


# ---------------------------------------------------------------------------
# Qt GUI — only runs if PyQt6 importable. Headless users can still use the
# helpers above (TileCache, compute_hillshade, extract_cliff_section, etc.).
# ---------------------------------------------------------------------------


def _run_gui(masks_dir: Path, config_path: Path) -> int:
    try:
        from PyQt6.QtCore import Qt, QRectF
        from PyQt6.QtGui import QImage, QPixmap, QPainter
        from PyQt6.QtWidgets import (
            QApplication, QMainWindow, QWidget, QSplitter, QVBoxLayout,
            QHBoxLayout, QCheckBox, QLabel, QComboBox, QPushButton,
            QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QScrollArea,
        )
    except ImportError as e:
        print(f"[world_viewer] PyQt6 not available: {e}", file=sys.stderr)
        print("[world_viewer] install with: pip install PyQt6", file=sys.stderr)
        return 2

    # Try to import the S43 palette editor — optional, degrade gracefully.
    try:
        from tools.world_studio import PaletteEditorWidget  # noqa: F401
        palette_available = True
    except Exception as e:
        print(f"[world_viewer] palette editor unavailable ({e}); skipping.")
        palette_available = False

    cache = TileCache()
    layers = discover_layers(masks_dir)

    # ---- Canvas ---------------------------------------------------------
    class WorldCanvas(QGraphicsView):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._scene = QGraphicsScene(self)
            self.setScene(self._scene)
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            self._zoom = 0
            self._pixmap_item: Optional[QGraphicsPixmapItem] = None
            self._enabled_layers: list[LayerSpec] = []
            self._base_height: Optional[np.ndarray] = None

        def set_base_height(self, h: np.ndarray) -> None:
            self._base_height = h
            self.render_world()

        def set_layers(self, enabled: list[LayerSpec]) -> None:
            self._enabled_layers = enabled
            self.render_world()

        def render_world(self) -> None:
            if self._base_height is None:
                return
            zoom = self._zoom
            # 1. Base: hillshade × terrain colormap.
            h_ds = _downsample_mean(self._base_height, 1 << zoom)
            hs = compute_hillshade(h_ds)
            terrain = apply_colormap(h_ds.astype(np.float32), "terrain")
            base = (terrain.astype(np.float32) * hs[..., None]).astype(np.uint8)

            # 2. Overlay each enabled layer by alpha-blending.
            composite = base.copy()
            for L in self._enabled_layers:
                arr = cache.get(L, zoom)
                if arr.shape != base.shape[:2]:
                    # Resize-align via simple slicing — layers come from 1:8 so
                    # this should be a no-op in practice.
                    continue
                if L.kind == "discrete":
                    # Random tab-colors per unique id.
                    rgb = _discrete_colors(arr)
                else:
                    rgb = apply_colormap(arr.astype(np.float32), L.colormap)
                alpha = L.alpha
                composite = (
                    composite.astype(np.float32) * (1 - alpha)
                    + rgb.astype(np.float32) * alpha
                ).astype(np.uint8)

            # 3. Paint into the scene.
            H, W, _ = composite.shape
            qimg = QImage(composite.tobytes(), W, H, W * 3,
                          QImage.Format.Format_RGB888)
            pix = QPixmap.fromImage(qimg)
            if self._pixmap_item is None:
                self._pixmap_item = self._scene.addPixmap(pix)
                self._scene.setSceneRect(QRectF(0, 0, W, H))
            else:
                self._pixmap_item.setPixmap(pix)

        def wheelEvent(self, ev):
            delta = ev.angleDelta().y()
            if delta > 0 and self._zoom > 0:
                self._zoom -= 1
            elif delta < 0 and self._zoom < 3:
                self._zoom += 1
            self.render_world()

    def _discrete_colors(arr: np.ndarray) -> np.ndarray:
        ids = np.unique(arr)
        palette = {}
        for i, v in enumerate(ids):
            r = (37 * (int(v) + 1)) & 0xFF
            g = (91 * (int(v) + 1)) & 0xFF
            b = (157 * (int(v) + 1)) & 0xFF
            palette[int(v)] = (r, g, b)
        rgb = np.zeros((*arr.shape, 3), dtype=np.uint8)
        for v, color in palette.items():
            rgb[arr == v] = color
        return rgb

    # ---- Layer panel ----------------------------------------------------
    class LayerPanel(QWidget):
        def __init__(self, canvas: WorldCanvas, parent=None):
            super().__init__(parent)
            self._canvas = canvas
            self._checkboxes: dict[str, QCheckBox] = {}
            root = QVBoxLayout(self)
            root.addWidget(QLabel("<b>Layers</b>"))
            for L in layers:
                cb = QCheckBox(L.name)
                cb.setChecked(L.enabled)
                cb.stateChanged.connect(
                    lambda _s, lyr=L, c=cb: self._on_toggle(lyr, c))
                root.addWidget(cb)
                self._checkboxes[L.name] = cb
            root.addStretch(1)

        def _on_toggle(self, layer: LayerSpec, cb: QCheckBox) -> None:
            layer.enabled = cb.isChecked()
            self._canvas.set_layers([L for L in layers if L.enabled])

    # ---- Cliff inset ----------------------------------------------------
    class CliffInset(QLabel):
        def __init__(self, height: np.ndarray, parent=None):
            super().__init__(parent)
            self._height = height
            self.setFixedSize(256, 128)
            self.setStyleSheet("background: #111; color: #ccc;")
            self._draw_default()

        def _draw_default(self):
            section = extract_cliff_section(
                self._height,
                center_xy=(self._height.shape[1] // 2,
                           self._height.shape[0] // 2),
                length_px=64,
            )
            img = np.full((128, 256, 3), 30, dtype=np.uint8)
            if section.max() > section.min():
                norm = (section - section.min()) / (section.max() - section.min() + 1e-9)
            else:
                norm = np.zeros_like(section, dtype=np.float32)
            xs = np.linspace(0, 255, len(section)).astype(np.int32)
            ys = (127 - norm * 120).astype(np.int32)
            for i in range(1, len(xs)):
                img[ys[i]:128, xs[i]] = (180, 140, 80)
            qimg = QImage(img.tobytes(), 256, 128, 256 * 3,
                          QImage.Format.Format_RGB888)
            self.setPixmap(QPixmap.fromImage(qimg))

    # ---- Main window ----------------------------------------------------
    class ViewerWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("Vandir World Viewer (Phase 0.5 MVP)")
            self.resize(1400, 900)

            canvas = WorldCanvas()
            # Bootstrap with height.tif.
            try:
                import rasterio
                with rasterio.open(str(masks_dir / "height.tif")) as src:
                    h_full = src.read(1)
                # Downsample to precompute scale for snappier first paint.
                h_precompute = _downsample_mean(h_full, 8)
                canvas.set_base_height(h_precompute)
                cliff = CliffInset(h_precompute)
            except Exception as e:
                print(f"[world_viewer] base height load failed: {e}")
                cliff = QLabel(f"cliff inset unavailable: {e}")

            panel = LayerPanel(canvas)

            right = QWidget()
            right_layout = QVBoxLayout(right)
            right_layout.addWidget(panel)
            right_layout.addWidget(QLabel("<b>Cliff inset</b>"))
            right_layout.addWidget(cliff)

            if palette_available:
                from tools.world_studio import PaletteEditorWidget
                right_layout.addWidget(QLabel("<b>Palette editor</b>"))
                right_layout.addWidget(PaletteEditorWidget())

            right_scroll = QScrollArea()
            right_scroll.setWidget(right)
            right_scroll.setWidgetResizable(True)
            right_scroll.setFixedWidth(360)

            splitter = QSplitter(Qt.Orientation.Horizontal)
            splitter.addWidget(canvas)
            splitter.addWidget(right_scroll)
            splitter.setStretchFactor(0, 1)
            splitter.setStretchFactor(1, 0)
            self.setCentralWidget(splitter)

    app = QApplication(sys.argv)
    win = ViewerWindow()
    win.show()
    return app.exec()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--masks", type=Path, default=REPO_ROOT / "masks")
    ap.add_argument("--config", type=Path,
                    default=REPO_ROOT / "config" / "thresholds.json")
    args = ap.parse_args()
    return _run_gui(args.masks, args.config)


if __name__ == "__main__":
    raise SystemExit(main())
