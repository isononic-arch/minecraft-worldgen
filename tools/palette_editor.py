"""
S64 — Lightweight standalone surface-block palette editor (+ visualizer).

Reads from + writes to `config/thresholds.json` at the `noise_layers_biome`
key.  Embeds the `SurfaceBlockVisualizerWidget` from `tools/terrain_preview.py`
as a live preview pane alongside the editable table.

Run:
  py tools/palette_editor.py
"""

from __future__ import annotations
import json
import sys
from pathlib import Path

# Ensure project root on sys.path for terrain_preview + core imports
_PROJECT_ROOT_STR = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT_STR not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT_STR)

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QComboBox, QPushButton, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QCheckBox, QDoubleSpinBox, QSpinBox, QLineEdit,
    QSlider,
)

# Minimal block-color LUT for preview (covers common Vandir palette blocks)
BLOCK_COLORS: dict[str, tuple[int, int, int]] = {
    "stone":              (128, 128, 128),
    "cobblestone":        ( 96,  96,  96),
    "andesite":           (135, 135, 135),
    "granite":            (150, 110,  90),
    "diorite":            (192, 192, 192),
    "deepslate":          ( 64,  64,  72),
    "cobbled_deepslate":  ( 72,  72,  80),
    "tuff":               (104, 108,  80),
    "calcite":            (232, 228, 220),
    "sandstone":          (216, 208, 160),
    "red_sandstone":      (192, 104,  48),
    "sand":               (232, 216, 168),
    "red_sand":           (208, 128,  64),
    "gravel":             (144, 136, 128),
    "dirt":               (110,  82,  48),
    "coarse_dirt":        ( 94,  70,  40),
    "podzol":             ( 88,  60,  32),
    "rooted_dirt":        (140,  96,  64),
    "grass_block":        ( 90, 143,  60),
    "snow_block":         (240, 244, 248),
    "packed_mud":         (122,  84,  54),
    "mud":                ( 70,  58,  50),
    "muddy_mangrove_roots": ( 92,  74,  58),
    "clay":               (162, 168, 178),
    "basalt":             ( 64,  64,  72),
    "smooth_basalt":      ( 58,  58,  66),
    "blackstone":         ( 42,  38,  46),
    "coal_block":         ( 30,  30,  34),
    "moss_block":         (104, 140,  48),
    "mycelium":           (106,  82,  82),
    "dripstone_block":    (142, 108,  90),
    "mossy_cobblestone":  ( 96, 120,  80),
    "water":              ( 55, 100, 200),
    "lava":               (220, 100,  40),
    "ice":                (180, 210, 240),
    "kelp":               ( 64, 120,  72),
    "seagrass":           ( 80, 150, 130),
}


class _LayerPreview(QLabel):
    """Simple 320×320 preview — per-layer fBm noise > (1 - coverage) decides
    which layer 'wins' at each pixel.  Draws the winning block's color.
    Base layer (is_base=True) fills everywhere not claimed by other layers.
    """

    SIZE = 320

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setStyleSheet("background:#333;")
        self._layers: list[dict] = []
        self._rng_seed = 42
        self._render_blank()

    def set_layers(self, layers: list[dict]) -> None:
        self._layers = layers
        self._render()

    def _render_blank(self):
        arr = np.full((self.SIZE, self.SIZE, 3), 48, dtype=np.uint8)
        self._display(arr)

    def _render(self):
        if not self._layers:
            self._render_blank()
            return
        H = W = self.SIZE
        # Simple fBm-ish noise via stacked low-res random + bilinear
        def _fbm(seed: int, scale: int) -> np.ndarray:
            freq = max(2, int(self.SIZE / max(10.0, float(scale))))
            rng = np.random.default_rng(seed)
            low = rng.random((freq, freq), dtype=np.float32)
            # Upsample via scipy.ndimage.zoom
            from scipy.ndimage import zoom
            factor = self.SIZE / low.shape[0]
            up = zoom(low, factor, order=1)[:H, :W]
            # Add a higher-freq octave for texture
            rng2 = np.random.default_rng(seed + 1)
            low2 = rng2.random((freq * 4, freq * 4), dtype=np.float32)
            factor2 = self.SIZE / low2.shape[0]
            up2 = zoom(low2, factor2, order=1)[:H, :W]
            field = 0.7 * up + 0.3 * up2
            return np.clip(field, 0.0, 1.0)

        # Start with base layer's color (is_base wins where nothing else claims)
        base_color = (180, 180, 180)
        base_name = None
        for lyr in self._layers:
            if lyr.get("is_base") and lyr.get("enabled", True):
                base_name = lyr.get("block", "stone")
                base_color = BLOCK_COLORS.get(base_name, (180, 180, 180))
                break

        arr = np.full((H, W, 3), base_color, dtype=np.uint8)

        # Process non-base layers in order — later layers can overwrite earlier ones.
        for i, lyr in enumerate(self._layers):
            if not lyr.get("enabled", True):
                continue
            if lyr.get("is_base"):
                continue
            block = lyr.get("block", "stone")
            coverage = float(lyr.get("coverage", 0.5))
            scale = int(lyr.get("scale", 60))
            seed = int(lyr.get("seed", 42 + i))
            field = _fbm(seed, scale)
            threshold = 1.0 - coverage  # higher coverage → lower threshold → more area
            mask = field > threshold
            color = BLOCK_COLORS.get(block, (200, 80, 80))  # unknown blocks → red
            arr[mask] = color
        self._display(arr)

    def _display(self, arr: np.ndarray):
        h, w, _ = arr.shape
        img = QImage(arr.tobytes(), w, h, 3 * w, QImage.Format.Format_RGB888)
        self.setPixmap(QPixmap.fromImage(img))

REPO = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO / "config" / "thresholds.json"
SECTION_KEY = "noise_layers_biome"

# Columns in the palette layer table
COLS = [
    ("name",     "str"),
    ("enabled",  "bool"),
    ("block",    "str"),
    ("sub",      "str"),
    ("coverage", "float"),
    ("scale",    "int"),
    ("noise",    "str"),
    ("seed",     "int"),
    ("is_base",  "bool"),
]


class PaletteEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Vandir — Surface Palette Editor + Visualizer (S64/S65)")
        self.resize(1500, 700)

        self.config: dict = {}
        self._suppress_viz_update = False
        self._load_config()

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        # Top bar: biome selector + Load/Save
        top = QHBoxLayout()
        top.addWidget(QLabel("Biome:"))
        self.biome_combo = QComboBox()
        self.biome_combo.currentTextChanged.connect(self._on_biome_changed)
        top.addWidget(self.biome_combo)
        top.addStretch(1)

        btn_reload = QPushButton("Reload from disk")
        btn_reload.clicked.connect(self._load_config)
        top.addWidget(btn_reload)

        btn_save = QPushButton("Save to disk")
        btn_save.clicked.connect(self._save_config)
        top.addWidget(btn_save)

        btn_preview = QPushButton("Refresh preview")
        btn_preview.clicked.connect(self._refresh_viz)
        top.addWidget(btn_preview)

        top.addWidget(QLabel("  Input:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Spinbox", "Slider"])
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        top.addWidget(self.mode_combo)

        # Row reorder buttons
        btn_up = QPushButton("▲ Move up")
        btn_up.clicked.connect(lambda: self._move_row(-1))
        top.addWidget(btn_up)
        btn_down = QPushButton("▼ Move down")
        btn_down.clicked.connect(lambda: self._move_row(+1))
        top.addWidget(btn_down)

        self._input_mode = "Spinbox"

        main_layout.addLayout(top)

        # Splitter: editor table (left) | visualizer (right)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        main_layout.addWidget(splitter)

        # Left: editor table
        left_wrap = QWidget()
        left_layout = QVBoxLayout(left_wrap)
        self.table = QTableWidget()
        self.table.setColumnCount(len(COLS))
        self.table.setHorizontalHeaderLabels([c[0] for c in COLS])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        left_layout.addWidget(self.table)
        splitter.addWidget(left_wrap)

        # Right: visualizer (simple per-layer fBm preview)
        right_wrap = QWidget()
        right_layout = QVBoxLayout(right_wrap)
        right_layout.addWidget(QLabel("Preview — per-layer fBm, later layers overwrite earlier"))
        self.viz = _LayerPreview()
        right_layout.addWidget(self.viz)
        right_layout.addWidget(QLabel("(base layer fills first; coverage=threshold area; colors approximate block)"))
        right_layout.addStretch(1)
        splitter.addWidget(right_wrap)
        splitter.setSizes([900, 400])

        # Info bar
        self.info_label = QLabel("Select a biome to begin.")
        self.info_label.setStyleSheet("color: #888;")
        main_layout.addWidget(self.info_label)

        self._populate_biomes()

    # ───────────────────────────────────────────────────────────────
    def _load_config(self) -> None:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        if hasattr(self, "biome_combo"):
            current = self.biome_combo.currentText()
            self._populate_biomes()
            if current:
                idx = self.biome_combo.findText(current)
                if idx >= 0:
                    self.biome_combo.setCurrentIndex(idx)
            self.info_label.setText(f"Reloaded from {CONFIG_PATH}")

    def _populate_biomes(self) -> None:
        self.biome_combo.clear()
        section = self.config.get(SECTION_KEY, {})
        biomes = sorted(section.keys())
        self.biome_combo.addItems(biomes)

    def _on_biome_changed(self, biome: str) -> None:
        if not biome:
            return
        layers = self.config.get(SECTION_KEY, {}).get(biome, [])
        self._suppress_viz_update = True
        self.table.setRowCount(len(layers))
        for r, layer in enumerate(layers):
            for c, (key, kind) in enumerate(COLS):
                value = layer.get(key)
                w = self._make_widget(value, kind)
                # Wire all edits to trigger viz refresh
                self._wire_widget(w, kind)
                self.table.setCellWidget(r, c, w)
        self._suppress_viz_update = False
        self.info_label.setText(f"{biome}: {len(layers)} layers")
        self._refresh_viz()

    def _make_widget(self, value, kind: str):
        if kind == "bool":
            w = QCheckBox()
            w.setChecked(bool(value))
            return w
        if kind == "float":
            if self._input_mode == "Slider":
                w = QSlider(Qt.Orientation.Horizontal)
                w.setRange(0, 10000)  # 0.0 - 10.0 with 4-digit precision
                w.setValue(int(float(value or 0.0) * 1000))
                w.setMinimumWidth(100)
                w.setToolTip(f"{float(value or 0.0):.4f}")
                return w
            w = QDoubleSpinBox()
            w.setDecimals(4)
            w.setRange(0.0, 10000.0)
            w.setValue(float(value) if value is not None else 0.0)
            w.setSingleStep(0.01)
            return w
        if kind == "int":
            if self._input_mode == "Slider":
                w = QSlider(Qt.Orientation.Horizontal)
                w.setRange(0, 500)
                w.setValue(min(500, int(value or 0)))
                w.setMinimumWidth(100)
                w.setToolTip(str(int(value or 0)))
                return w
            w = QSpinBox()
            w.setRange(0, 10_000_000)
            w.setValue(int(value) if value is not None else 0)
            return w
        w = QLineEdit()
        w.setText(str(value) if value is not None else "")
        return w

    def _wire_widget(self, w, kind: str) -> None:
        """Wire any edit signal to refresh the visualizer."""
        if kind == "bool":
            w.toggled.connect(self._refresh_viz)
        elif isinstance(w, QSlider):
            # Update tooltip + preview on slider change
            def _on_slide(v, widget=w, k=kind):
                if k == "float":
                    widget.setToolTip(f"{v / 1000.0:.4f}")
                else:
                    widget.setToolTip(str(v))
                self._refresh_viz()
            w.valueChanged.connect(_on_slide)
        elif kind in ("float", "int"):
            w.valueChanged.connect(lambda _: self._refresh_viz())
        else:
            w.editingFinished.connect(self._refresh_viz)

    def _read_widget(self, w, kind: str):
        if kind == "bool":
            return w.isChecked()
        if kind == "float":
            if isinstance(w, QSlider):
                return w.value() / 1000.0
            return float(w.value())
        if kind == "int":
            if isinstance(w, QSlider):
                return int(w.value())
            return int(w.value())
        return w.text()

    def _on_mode_changed(self, mode: str) -> None:
        self._input_mode = mode
        # Rebuild the table with the new widget type
        biome = self.biome_combo.currentText()
        if biome:
            self._on_biome_changed(biome)

    def _move_row(self, direction: int) -> None:
        """Move the selected row up (-1) or down (+1).  Uses config-level
        reorder so the change persists through save."""
        biome = self.biome_combo.currentText()
        if not biome:
            return
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "No selection",
                "Click a row first, then use the arrow buttons.")
            return
        layers = self.config.get(SECTION_KEY, {}).get(biome, [])
        new_row = row + direction
        if new_row < 0 or new_row >= len(layers):
            return
        # Commit current widget state BEFORE reordering
        rows = self.table.rowCount()
        for r in range(rows):
            for c, (key, kind) in enumerate(COLS):
                w = self.table.cellWidget(r, c)
                layers[r][key] = self._read_widget(w, kind)
        # Swap layers[row] and layers[new_row]
        layers[row], layers[new_row] = layers[new_row], layers[row]
        # Reload table
        self._on_biome_changed(biome)
        # Select the moved row
        self.table.setCurrentCell(new_row, 0)
        self.info_label.setText(f"Moved row {row} → {new_row}")

    def _refresh_viz(self) -> None:
        """Collect current table state as a list of dicts and feed the preview."""
        if self._suppress_viz_update or self.viz is None:
            return
        rows = self.table.rowCount()
        layers = []
        for r in range(rows):
            vals = {}
            for c, (key, kind) in enumerate(COLS):
                w = self.table.cellWidget(r, c)
                vals[key] = self._read_widget(w, kind)
            layers.append(vals)
        try:
            self.viz.set_layers(layers)
        except Exception as exc:
            print(f"[PaletteEditor] preview update failed: {exc}", flush=True)

    def _save_config(self) -> None:
        """Save only our section back to disk.  Preserves any other config
        sections that may have been added to the JSON since we loaded it."""
        biome = self.biome_combo.currentText()
        if not biome:
            return
        # Merge strategy: re-read disk to get latest config, then only replace
        # our SECTION_KEY with our in-memory version.  Protects other sections
        # (e.g. ocean.*, snow_carpet.*) from being overwritten by stale data.
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                disk_config = json.load(f)
        except Exception:
            disk_config = dict(self.config)

        # Apply current widget state to our in-memory copy first
        section = self.config.setdefault(SECTION_KEY, {})
        layers = section.setdefault(biome, [])
        rows = self.table.rowCount()
        if rows != len(layers):
            QMessageBox.warning(self, "Row-count mismatch",
                                 f"UI has {rows} rows but config has "
                                 f"{len(layers)} layers — save aborted.  "
                                 f"Reload from disk and try again.")
            return
        for r in range(rows):
            layer = layers[r]
            for c, (key, kind) in enumerate(COLS):
                w = self.table.cellWidget(r, c)
                layer[key] = self._read_widget(w, kind)

        # Now merge: replace ONLY our section in the disk version
        disk_config[SECTION_KEY] = self.config[SECTION_KEY]

        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(disk_config, f, indent=2)
        # Keep our in-memory copy in sync with disk for subsequent saves
        self.config = disk_config
        self.info_label.setText(f"Saved {biome} ({rows} layers) to {CONFIG_PATH} (merge-safe)")


def main():
    app = QApplication(sys.argv)
    w = PaletteEditor()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
