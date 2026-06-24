"""islands/island_spline_studio.py — per-island DEM→MC-Y vertical spline editor.

The ONE bespoke per-island knob (user: "splines are the only thing we do uniquely
island to island"). Adapts tools/terrain_preview.py:SplineEditorWidget into a
per-island tool that ALSO cross-sections each island north→south and east→west
through its summit, so you can drag the elevation curve and watch the real island
profile respond live — fixing "too steep mountain, slightly too high ocean".

Curve space = (elevation FRACTION 0=sea..1=peak)  →  (MC-Y blocks).
  • frac 0 endpoint is locked at Y63 (the waterline).
  • frac 1 endpoint (the summit) drags in Y → peak height ("too steep/high mountain").
  • drag the low-frac knee DOWN → flatter coastal shelf ("too high ocean").
  • mid points shape the flank (right-click empty space to add, on a point to remove).

Saves islands/spline_overrides.json keyed by island key (e.g. "13_130"). The bake
(islands/render_islands.py:bake_island) applies it via derive(curve=(fracs,mcys)),
overriding the parametric coast_frac/coast_rise/gamma shape for that island only.
Islands with no override fall back to the parametric default — byte-identical.

Launch:
    C:\\Users\\nicho\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe islands/island_spline_studio.py
"""
from __future__ import annotations
import json, sys, math
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from derive_masks_from_height import (load_dem, detect_sea_raw,
                                      clean_edge_fragments, reconstruct_clipped_peaks)
try:
    from islands.render_islands import REGISTRY as _REGISTRY   # authoritative peak_m (matches the bake)
except Exception:
    _REGISTRY = {}

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QComboBox, QPushButton, QLabel, QSlider, QTableWidget, QTableWidgetItem, QCheckBox)
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure

ISL = ROOT / "islands"
LAYOUT = ISL / "layout.json"
OVERRIDES = ISL / "spline_overrides.json"

COAST_FRAC, COAST_RISE, GAMMA = 0.06, 0.015, 2.2   # mirror dem_to_mcy parametric defaults
PREVIEW_LONG = 1024                        # downsample DEM long side for the editor (RAM/speed)
SEA_Y = 63.0


# ── per-island spline store ─────────────────────────────────────────────────
def load_overrides() -> dict:
    if OVERRIDES.exists():
        try:
            return json.loads(OVERRIDES.read_text())
        except Exception:
            return {}
    return {}


def save_overrides(d: dict) -> None:
    OVERRIDES.write_text(json.dumps(d, indent=2))


def island_key(dem_path: str) -> str:
    """Match render_islands._key: the "<lat>_<lon>" filename prefix (= the BANDS key)."""
    return "_".join(Path(dem_path).name.split("_")[:2])


# ── curve math (must match derive_masks_from_height.dem_to_mcy) ──────────────
def parametric_mcy(frac, mcy_peak, coast_frac=COAST_FRAC, coast_rise=COAST_RISE, gamma=GAMMA):
    """The default coastal-gamma shape, sampled to seed a fresh curve."""
    fp = np.clip(frac, 0.0, 1.0)
    cf = max(coast_frac, 1e-3)
    shelf = (fp / cf) * coast_rise
    t = np.clip((fp - cf) / max(1.0 - cf, 1e-3), 0.0, 1.0)
    upper = coast_rise + (1.0 - coast_rise) * (t ** max(gamma, 1e-3))
    shape = np.where(fp <= cf, shelf, upper)
    return SEA_Y + shape * (mcy_peak - SEA_Y)


def seed_curve(mcy_peak: float):
    """Breakpoints that reproduce the parametric default for this peak height.
    Extra knots in the upper flank (where gamma bends hardest) keep the linear-
    interp seed within a few blocks of the true gamma curve, so opening + saving
    an island unchanged is ~a no-op."""
    fr = [0.0, COAST_FRAC, 0.25, 0.50, 0.70, 0.85, 0.95, 1.0]
    my = [float(parametric_mcy(np.array([f]), mcy_peak)[0]) for f in fr]
    my[0] = SEA_Y
    my[-1] = float(mcy_peak)
    return fr, my


def curve_mcy(frac, fracs, mcys):
    return np.interp(np.clip(frac, 0.0, 1.0), fracs, mcys)


# ── per-island DEM holder (lazy, downsampled, declipped to match the bake) ───
class Island:
    def __init__(self, entry: dict):
        self.entry = entry
        self.name = entry["name"]
        self.key = island_key(entry["dem_path"])
        self.dem_path = entry["dem_path"]
        fn = Path(self.dem_path).name
        self.peak_m = float(_REGISTRY.get(fn, {}).get("peak_m", entry.get("peak_m", 500.0)))
        self.bpp = float(entry.get("blocks_per_px", 1.0))   # world blocks per NATIVE DEM px (= derive hstep)
        # TRUE horizontal scale: web-mercator m/px at this lat/zoom ÷ blocks_per_px = real metres per block.
        t = fn.split("_")
        lat = float(f"{t[0]}.{t[1]}"); zoom = int(t[4])
        self.m_per_block = (156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)) / self.bpp
        self.relief_1x = self.peak_m / self.m_per_block      # 1:1 peak relief above sea, in blocks
        self.mcy_peak_default = self.peak_y(1.5)             # project default = 1.5× true proportion
        self._loaded = False

    def peak_y(self, vex: float) -> float:
        """Summit MC-Y at a given vertical exaggeration (1.0 = geographically true 1:1)."""
        return SEA_Y + self.relief_1x * vex

    def load(self):
        if self._loaded:
            return
        dem = load_dem(Path(self.dem_path))
        nat_long = float(max(dem.shape))                    # native DEM long side
        if max(dem.shape) > PREVIEW_LONG:
            from scipy.ndimage import zoom
            dem = zoom(dem.astype(np.float64), PREVIEW_LONG / max(dem.shape), order=1)
        sea = detect_sea_raw(dem)
        dem, _ = clean_edge_fragments(dem, sea, max_frac=0.05)
        declip = bool(self.entry.get("declip", True))
        dem, _ = reconstruct_clipped_peaks(dem, sea, frac=0.12 if declip else 0.0,
                                           noise_frac=0.0 if declip else 0.015)
        self.dem = dem
        self.sea = float(sea)
        self.peak = float(dem.max())
        self.H, self.W = dem.shape
        pr, pc = np.unravel_index(int(np.argmax(dem)), dem.shape)
        self.peak_rc = (int(pr), int(pc))
        # world blocks per PREVIEW px = blocks/native-px × (native long / preview long)
        self.hstep = self.bpp * nat_long / max(self.H, self.W)
        self._loaded = True

    def frac(self, raw):
        return np.clip((raw - self.sea) / max(self.peak - self.sea, 1.0), 0.0, 1.0)


# ── draggable (frac → MC-Y) spline canvas ───────────────────────────────────
class SplineCanvas(FigureCanvasQTAgg):
    def __init__(self, on_change):
        self._fig = Figure(facecolor="#0d1b2a")
        self._fig.subplots_adjust(left=0.13, right=0.97, top=0.90, bottom=0.13)
        self._ax = self._fig.add_subplot(111)
        super().__init__(self._fig)
        self.on_change = on_change
        self._pts: list[list[float]] = []          # [[frac, mcy], ...] sorted by frac
        self._ymax = 720.0
        self._dragging = None
        self.mpl_connect("button_press_event", self._press)
        self.mpl_connect("motion_notify_event", self._motion)
        self.mpl_connect("button_release_event", self._release)

    def set_curve(self, fracs, mcys, ymax):
        self._pts = [[float(f), float(y)] for f, y in zip(fracs, mcys)]
        self._ymax = float(ymax)
        self._redraw()

    def curve(self):
        fr = [p[0] for p in self._pts]
        my = [p[1] for p in self._pts]
        return fr, my

    def _redraw(self):
        ax = self._ax
        ax.cla()
        ax.set_facecolor("#0a1a2e")
        ax.set_xlim(0, 1)
        ax.set_ylim(40, max(self._ymax, 120))
        ax.tick_params(colors="#6688aa", labelsize=7)
        for sp in ax.spines.values():
            sp.set_edgecolor("#1a3a5c")
        ax.set_xlabel("elevation fraction  (0 = sea · 1 = summit)", color="#6688aa", fontsize=8)
        ax.set_ylabel("MC-Y", color="#6688aa", fontsize=8)
        ax.set_title("Vertical spline  (drag orange · L=peak height · low knee=coast · right-click add/remove)",
                     color="#aaaacc", fontsize=7.5, pad=3)
        if len(self._pts) >= 2:
            fr = [p[0] for p in self._pts]
            my = [p[1] for p in self._pts]
            xs = np.linspace(0, 1, 400)
            ax.plot(xs, curve_mcy(xs, fr, my), color="#4488cc", lw=1.5, zorder=3)
        ax.axhline(SEA_Y, color="#00cccc", lw=0.8, ls=":", alpha=0.55, zorder=2)
        for i, (f, y) in enumerate(self._pts):
            locked = (i == 0)                      # frac-0 / sea endpoint Y-locked at 63
            ax.plot(f, y, "o", ms=9, zorder=5, color="#00cccc" if locked else "#ff8844",
                    markeredgecolor="#ffffff", markeredgewidth=0.8)
        self.draw_idle()

    def _hit(self, ev):
        if ev.xdata is None:
            return None
        for i, (f, y) in enumerate(self._pts):
            dx, dy = self._ax.transData.transform((f, y))
            if (np.hypot(dx - ev.x, dy - ev.y)) < 14:
                return i
        return None

    def _press(self, ev):
        if ev.inaxes is not self._ax:
            return
        if ev.button == 1:
            self._dragging = self._hit(ev)
        elif ev.button == 3:
            i = self._hit(ev)
            if i is not None:
                if 0 < i < len(self._pts) - 1:     # keep both endpoints
                    self._pts.pop(i)
                    self._redraw(); self._emit()
            elif ev.xdata is not None:
                f = float(np.clip(ev.xdata, 0.001, 0.999))
                y = float(np.clip(ev.ydata, 40, self._ymax))
                at = next((k for k, p in enumerate(self._pts) if p[0] > f), len(self._pts))
                self._pts.insert(at, [f, y])
                self._redraw(); self._emit()

    def _motion(self, ev):
        if self._dragging is None or ev.inaxes is not self._ax or ev.xdata is None:
            return
        i = self._dragging
        y = float(np.clip(ev.ydata, 40, self._ymax + 40))
        if i == 0:
            self._pts[i] = [0.0, SEA_Y]            # sea endpoint fully locked
        elif i == len(self._pts) - 1:
            self._pts[i] = [1.0, y]                # summit: X locked, Y = peak height
        else:
            lo = self._pts[i - 1][0] + 1e-3
            hi = self._pts[i + 1][0] - 1e-3
            self._pts[i] = [float(np.clip(ev.xdata, lo, hi)), y]
        self._redraw(); self._emit()

    def _release(self, ev):
        self._dragging = None

    def _emit(self):
        if self.on_change:
            self.on_change(*self.curve())


# ── N-S + E-W cross-section canvas ──────────────────────────────────────────
class CrossSectionCanvas(FigureCanvasQTAgg):
    def __init__(self):
        self._fig = Figure(facecolor="#0d1b2a")
        self._fig.subplots_adjust(left=0.10, right=0.97, top=0.93, bottom=0.09, hspace=0.35)
        self._ns = self._fig.add_subplot(211)
        self._ew = self._fig.add_subplot(212)
        super().__init__(self._fig)
        self._isl = None
        self.col = 0
        self.row = 0
        self.true_aspect = False        # False = auto (vert-exaggerated); True = 1:1 honest
        self._keep_view: dict = {}      # ax -> (xlim, ylim) the user zoomed/panned to (survives redraws)
        self._pan = None
        self._last = None               # last (fracs, mcys) for internal re-renders
        self.mpl_connect("scroll_event", self._scroll)
        self.mpl_connect("button_press_event", self._press)
        self.mpl_connect("motion_notify_event", self._drag)
        self.mpl_connect("button_release_event", self._release)

    def set_island(self, isl: Island):
        self._isl = isl
        self.row, self.col = isl.peak_rc
        self._keep_view.clear()         # fresh island -> reset any zoom/pan

    # --- zoom (scroll, cursor-centred) / pan (left-drag) / reset (double-click) ---
    def _scroll(self, ev):
        ax = ev.inaxes
        if ax is None or ev.xdata is None:
            return
        s = 0.8 if ev.button == "up" else 1.25
        x0, x1 = ax.get_xlim(); y0, y1 = ax.get_ylim()
        ax.set_xlim(ev.xdata - (ev.xdata - x0) * s, ev.xdata + (x1 - ev.xdata) * s)
        ax.set_ylim(ev.ydata - (ev.ydata - y0) * s, ev.ydata + (y1 - ev.ydata) * s)
        self._keep_view[ax] = (ax.get_xlim(), ax.get_ylim())
        self.draw_idle()

    def _press(self, ev):
        if ev.inaxes is None:
            return
        if ev.dblclick:                 # reset this subplot's view
            self._keep_view.pop(ev.inaxes, None)
            self.refresh()
        elif ev.button == 1:            # start pan (anchor in pixels for stability)
            self._pan = (ev.inaxes, ev.x, ev.y, ev.inaxes.get_xlim(), ev.inaxes.get_ylim())

    def _drag(self, ev):
        if self._pan is None or ev.x is None:
            return
        ax, px, py, xl, yl = self._pan
        if ev.inaxes is not ax:
            return
        inv = ax.transData.inverted()
        d0 = inv.transform((px, py)); d1 = inv.transform((ev.x, ev.y))
        dx, dy = d0[0] - d1[0], d0[1] - d1[1]
        ax.set_xlim(xl[0] + dx, xl[1] + dx); ax.set_ylim(yl[0] + dy, yl[1] + dy)
        self._keep_view[ax] = (ax.get_xlim(), ax.get_ylim())
        self.draw_idle()

    def _release(self, ev):
        self._pan = None

    def refresh(self):
        if self._last is not None:
            self.redraw(*self._last)

    def _profile(self, ax, dist_blocks, mcy, ocean, title, peak_x):
        ax.cla()
        ax.set_facecolor("#0a1a2e")
        ax.tick_params(colors="#6688aa", labelsize=7)
        for sp in ax.spines.values():
            sp.set_edgecolor("#1a3a5c")
        floor = -8
        # solid ground FOLLOWS the terrain line (no `where` → no false vertical wall at
        # the coast; the land wedge tapers naturally down to the waterline).
        ax.fill_between(dist_blocks, floor, mcy, color="#4a6a3a", zorder=1)
        # sea fills to the waterline over submerged cells. NOTE: the editor clips
        # bathymetry to a flat Y63 — in-game the seabed keeps sloping down to ~-60.
        ax.fill_between(dist_blocks, floor, SEA_Y, where=ocean, color="#1b3a55", zorder=2)
        ax.plot(dist_blocks, mcy, color="#9fd17a", lw=0.9, zorder=3)
        ax.axhline(SEA_Y, color="#00cccc", lw=0.8, ls=":", alpha=0.6, zorder=4)
        ax.axvline(peak_x, color="#ffaa55", lw=0.7, ls="--", alpha=0.5, zorder=4)
        ax.set_ylabel("MC-Y", color="#6688aa", fontsize=8)
        ax.set_aspect("auto")                # box ALWAYS fills the panel (never a sliver)
        pk = float(np.max(mcy))
        # TRUE flank steepness (scale-independent): rise/run in blocks -> degrees
        land = ~ocean
        if int(land.sum()) > 3:
            g = np.gradient(mcy, dist_blocks)
            sl = np.degrees(np.arctan(np.abs(g)))
            s95, smax = float(np.percentile(sl[land], 95)), float(np.max(sl[land]))
        else:
            s95 = smax = 0.0
        if self.true_aspect:
            # Honest 1:1 by EQUAL blocks-per-pixel in the full-size box: pick limits
            # whose span ratio == the panel's pixel ratio. Scroll/pan keep that ratio,
            # so the terrain MAGNIFIES (box never shrinks) instead of staying a sliver.
            bb = ax.get_window_extent()
            w_px, h_px = max(float(bb.width), 1.0), max(float(bb.height), 1.0)
            length = float(dist_blocks[-1] - dist_blocks[0])
            yr = max(float(pk * 1.12 - floor), 1.0)
            bpp = max(length / w_px, yr / h_px)        # blocks/px to fit the whole island
            cx = float(dist_blocks[0] + dist_blocks[-1]) / 2.0
            cy = (floor + max(pk * 1.12, 120)) / 2.0
            ax.set_xlim(cx - w_px / 2 * bpp, cx + w_px / 2 * bpp)
            ax.set_ylim(cy - h_px / 2 * bpp, cy + h_px / 2 * bpp)
            note = "1:1 TRUE · scroll=zoom drag=pan"
        else:
            ax.set_xlim(dist_blocks[0], dist_blocks[-1])
            ax.set_ylim(floor, max(pk * 1.12, 120))
            note = "auto · VERT-EXAG"
        ax.set_title(f"{title}   peak Y≈{pk:.0f}   flank≈{s95:.0f}° (max {smax:.0f}°)   "
                     f"{dist_blocks[-1]:.0f}blk   [{note}]",
                     color="#aaaacc", fontsize=7.5, pad=2)

    def redraw(self, fracs, mcys):
        isl = self._isl
        if isl is None:
            return
        self._last = (fracs, mcys)
        ns_raw = isl.dem[:, self.col]
        ew_raw = isl.dem[self.row, :]
        ns_ocean = ns_raw <= isl.sea
        ew_ocean = ew_raw <= isl.sea
        ns_mcy = curve_mcy(isl.frac(ns_raw), fracs, mcys)
        ew_mcy = curve_mcy(isl.frac(ew_raw), fracs, mcys)
        ns_d = np.arange(isl.H) * isl.hstep
        ew_d = np.arange(isl.W) * isl.hstep
        self._profile(self._ns, ns_d, ns_mcy, ns_ocean, f"N-S  (column {self.col})", isl.peak_rc[0] * isl.hstep)
        self._profile(self._ew, ew_d, ew_mcy, ew_ocean, f"E-W  (row {self.row})", isl.peak_rc[1] * isl.hstep)
        # restore any zoom/pan the user set, so live spline edits don't snap the view back
        for ax, (xl, yl) in self._keep_view.items():
            ax.set_xlim(xl); ax.set_ylim(yl)
        self.draw_idle()


# ── main window ─────────────────────────────────────────────────────────────
class SplineStudio(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Island Spline Studio")
        self.resize(1320, 760)
        self.setStyleSheet("background:#0d1b2a; color:#ccd;")
        layout = json.loads(LAYOUT.read_text())["islands"]
        self.islands = [Island(e) for e in layout]
        self.overrides = load_overrides()
        self.cur: Island | None = None

        # top bar
        self.pick = QComboBox()
        for i in self.islands:
            tag = "  ●" if i.key in self.overrides else ""
            self.pick.addItem(f"{i.name}  [{i.key}]{tag}")
        self.pick.currentIndexChanged.connect(self._select)
        self.status = QLabel("")
        self.status.setStyleSheet("color:#88aacc; font-size:11px;")
        btn_save = QPushButton("Save override")
        btn_reset = QPushButton("Reset to default")
        btn_clear = QPushButton("Delete override")
        for b in (btn_save, btn_reset, btn_clear):
            b.setStyleSheet("QPushButton{background:#1e3a70;color:#ddeeff;border:1px solid #2a5090;"
                            "padding:4px 12px;} QPushButton:hover{background:#2a4e90;}")
        btn_save.clicked.connect(self._save)
        btn_reset.clicked.connect(self._reset)
        btn_clear.clicked.connect(self._clear)
        # vertical-scale dropdown (1x true / 1.5x / 2x / custom) — rescales the curve
        self.vscale = QComboBox()
        for mult in (1.0, 1.5, 2.0, None):          # None = "custom" (hand-tuned)
            self.vscale.addItem("", mult)
        self.vscale.setStyleSheet("color:#ffcc88;")
        self.vscale.currentIndexChanged.connect(self._set_vscale)
        top = QHBoxLayout()
        top.addWidget(QLabel("Island:"))
        top.addWidget(self.pick, 2)
        top.addWidget(QLabel("  Vert scale:"))
        top.addWidget(self.vscale, 1)
        top.addWidget(btn_save)
        top.addWidget(btn_reset)
        top.addWidget(btn_clear)
        top.addStretch()

        # canvases
        self.spline = SplineCanvas(self._on_curve)
        self.xsec = CrossSectionCanvas()
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["frac", "MC-Y"])
        self.table.setMaximumHeight(150)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)

        # cross-section line sliders
        self.s_col = QSlider(Qt.Orientation.Horizontal)
        self.s_row = QSlider(Qt.Orientation.Horizontal)
        self.s_col.valueChanged.connect(self._move_col)
        self.s_row.valueChanged.connect(self._move_row)
        self.chk_true = QCheckBox("True 1:1 proportions  (honest in-game steepness — flank° in titles is always true)")
        self.chk_true.setStyleSheet("color:#ffcc88; font-size:11px;")
        self.chk_true.toggled.connect(self._toggle_aspect)
        hint = QLabel("cross-sections:  scroll = zoom (at cursor) · left-drag = pan · double-click = reset view")
        hint.setStyleSheet("color:#6688aa; font-size:10px;")
        sl = QVBoxLayout()
        sl.addWidget(self.chk_true)
        sl.addWidget(hint)
        sl.addWidget(QLabel("N–S section line (column / west↔east position):"))
        sl.addWidget(self.s_col)
        sl.addWidget(QLabel("E–W section line (row / north↔south position):"))
        sl.addWidget(self.s_row)
        sl_w = QWidget(); sl_w.setLayout(sl)

        left = QWidget(); lv = QVBoxLayout(left)
        lv.addWidget(self.spline, 3)
        lv.addWidget(self.table, 1)
        right = QWidget(); rv = QVBoxLayout(right)
        rv.addWidget(self.xsec, 4)
        rv.addWidget(sl_w, 0)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left); split.addWidget(right)
        split.setSizes([520, 800])

        root = QWidget(); rl = QVBoxLayout(root)
        rl.addLayout(top)
        rl.addWidget(self.status)
        rl.addWidget(split, 1)
        self.setCentralWidget(root)
        if self.islands:
            self._select(0)

    # ----- island selection / curve seeding -----
    def _select(self, idx):
        self.cur = self.islands[idx]
        self.status.setText(f"loading {self.cur.name} …")
        QApplication.processEvents()
        self.cur.load()
        ov = self.overrides.get(self.cur.key)
        if ov and ov.get("fracs") and ov.get("mcys"):
            fr, my = list(map(float, ov["fracs"])), list(map(float, ov["mcys"]))
        else:
            fr, my = seed_curve(self.cur.mcy_peak_default)
        ymax = max(my[-1], self.cur.mcy_peak_default) + 60
        self.xsec.set_island(self.cur)
        self.s_col.blockSignals(True); self.s_row.blockSignals(True)
        self.s_col.setRange(0, self.cur.W - 1); self.s_col.setValue(self.cur.peak_rc[1])
        self.s_row.setRange(0, self.cur.H - 1); self.s_row.setValue(self.cur.peak_rc[0])
        self.s_col.blockSignals(False); self.s_row.blockSignals(False)
        self.xsec.col = self.cur.peak_rc[1]; self.xsec.row = self.cur.peak_rc[0]
        self.spline.set_curve(fr, my, ymax)
        self._sync_vscale_combo(my[-1])
        self._on_curve(fr, my)
        tag = "OVERRIDE loaded" if ov else "1.5× default (no override yet)"
        self.status.setText(f"{self.cur.name}  [{self.cur.key}]   sea_raw={self.cur.sea:.0f}  "
                            f"1block={self.cur.m_per_block:.1f}m  peak Y={my[-1]:.0f}  —  {tag}")

    def _on_curve(self, fr, my):
        self.xsec.redraw(fr, my)
        self.table.setRowCount(len(fr))
        for i, (f, y) in enumerate(zip(fr, my)):
            self.table.setItem(i, 0, QTableWidgetItem(f"{f:.3f}"))
            self.table.setItem(i, 1, QTableWidgetItem(f"{y:.1f}"))

    def _toggle_aspect(self, on):
        self.xsec.true_aspect = bool(on)
        self.xsec._keep_view.clear()        # reframe when switching auto<->1:1
        self.xsec.redraw(*self.spline.curve())

    def _move_col(self, v):
        self.xsec.col = int(v); self.xsec.redraw(*self.spline.curve())

    def _move_row(self, v):
        self.xsec.row = int(v); self.xsec.redraw(*self.spline.curve())

    # ----- vertical-scale dropdown -----
    def _sync_vscale_combo(self, peak):
        """Label the dropdown with THIS island's Y values + select the CLOSEST scale
        (closest, not last-within-tolerance — so very flat islands, where 1×/1.5×/2×
        sit within a couple blocks, don't mislabel as 2×)."""
        self.vscale.blockSignals(True)
        custom_i = self.vscale.count() - 1
        best_i, best_d = custom_i, 1e9
        for i in range(self.vscale.count()):
            m = self.vscale.itemData(i)
            if m is None:
                self.vscale.setItemText(i, "custom")
            elif self.cur:
                self.vscale.setItemText(i, f"{m:g}× (Y{self.cur.peak_y(m):.0f})")
                d = abs(peak - self.cur.peak_y(m))
                if d < best_d:
                    best_d, best_i = d, i
        self.vscale.setCurrentIndex(best_i if best_d < 2.0 else custom_i)
        self.vscale.blockSignals(False)

    def _set_vscale(self, idx):
        """Rescale the current curve to the chosen vertical exaggeration (preserves shape)."""
        if not self.cur:
            return
        mult = self.vscale.itemData(idx)
        if mult is None:                                  # "custom" — leave the curve alone
            return
        fr, my = self.spline.curve()
        if my[-1] - SEA_Y < 1:                            # flat island, nothing to scale
            return
        target = self.cur.peak_y(mult)
        s = (target - SEA_Y) / (my[-1] - SEA_Y)
        my = [SEA_Y + (y - SEA_Y) * s for y in my]        # scale every knot's height-above-sea
        self.spline.set_curve(fr, my, target + 60)
        self._on_curve(fr, my)
        self.status.setText(f"{self.cur.key}: vertical scale {mult:g}× → peak Y={target:.0f}   "
                            f"(flank° in the cross-section is the real steepness — Save to persist)")

    # ----- save / reset / clear -----
    def _save(self):
        if not self.cur:
            return
        fr, my = self.spline.curve()
        self.overrides[self.cur.key] = {
            "name": self.cur.name, "mcy_peak": round(my[-1], 1),
            "fracs": [round(f, 4) for f in fr], "mcys": [round(y, 1) for y in my]}
        save_overrides(self.overrides)
        # mark the dropdown entry
        self.pick.setItemText(self.pick.currentIndex(),
                              f"{self.cur.name}  [{self.cur.key}]  ●")
        self.status.setText(f"SAVED {self.cur.key} → spline_overrides.json   "
                            f"(re-bake:  py islands/render_islands.py --bake {self.cur.key})")

    def _reset(self):
        if not self.cur:
            return
        fr, my = seed_curve(self.cur.mcy_peak_default)     # clean parametric shape at 1.5× true
        self.spline.set_curve(fr, my, self.cur.mcy_peak_default + 60)
        self._sync_vscale_combo(my[-1])
        self._on_curve(fr, my)
        self.status.setText(f"{self.cur.key} reset to 1.5× clean shape (not saved — Save to persist, "
                            f"or Delete override to remove the file entry)")

    def _clear(self):
        if not self.cur:
            return
        if self.overrides.pop(self.cur.key, None) is not None:
            save_overrides(self.overrides)
            self.pick.setItemText(self.pick.currentIndex(), f"{self.cur.name}  [{self.cur.key}]")
            self.status.setText(f"deleted override for {self.cur.key} — island reverts to parametric default")
        self._reset()


def main():
    app = QApplication(sys.argv)
    w = SplineStudio()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
