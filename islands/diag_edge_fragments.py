"""islands/diag_edge_fragments.py — find land that TOUCHES the frame edge.

Land that runs into the DEM frame border gets cut off when the island region is
offset-rendered into the world → a harsh vertical seam where land meets the
noise-ocean. These are usually erase-job leftovers (stragglers). The bake's
`clean_edge_fragments` only removes border-touching land < `edge_clean`(5%) of the
main island, so bigger leftovers survive.

This replicates the bake's pre-derive state (erase mask + clean_edge_fragments)
then, per island, labels land components and flags the ones with pixels ON the
frame border. Renders a contact sheet (main island green, border stragglers RED)
+ a report so we can confirm which to kill.

    py islands/diag_edge_fragments.py            # all islands
Output: islands/out/edge_fragments.png + a printed table.
"""
from __future__ import annotations
import json, sys
from pathlib import Path
import numpy as np
from scipy.ndimage import label

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from derive_masks_from_height import _GIN, _YOUT
from islands.render_islands import MASKS_OUT, safe_name

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ISL = ROOT / "islands"
ANALYZE_LONG = 3000      # decimated read of the baked mask (border rows stay native under nearest)
LAND_MCY = 63.4          # bake's land threshold


def _border_labels(lbl: np.ndarray, b: int = 2) -> set[int]:
    # b-px band (not just the 1-px edge) so the decimated read's outermost-rows miss
    # doesn't hide a straggler sitting in the last few native rows.
    s = (set(lbl[:b].ravel()) | set(lbl[-b:].ravel())
         | set(lbl[:, :b].ravel()) | set(lbl[:, -b:].ravel()))
    s.discard(0)
    return s


def analyze(entry: dict):
    """Analyze the FINAL baked height.tif (post flip/rotate/ocean-snap) — the exact
    surface that gets offset-rendered. Land touching its border = world seam."""
    key = _key(Path(entry["dem_path"]).name)
    hp = MASKS_OUT / safe_name(entry["name"]) / "height.tif"
    if not hp.exists():
        return dict(key=key, name=entry["name"], missing=True)
    import rasterio
    from rasterio.enums import Resampling
    with rasterio.open(hp) as src:
        H, W = src.height, src.width
        f = max(1, max(H, W) // ANALYZE_LONG)
        oh, ow = H // f, W // f
        raw = src.read(1, out_shape=(oh, ow), resampling=Resampling.nearest).astype(np.float64)
    mcy = np.interp(raw, _GIN, _YOUT)
    land = mcy > LAND_MCY
    lbl, n = label(land)
    if n == 0:
        z = np.zeros_like(land)
        return dict(key=key, name=entry["name"], land=land, main=z, strag=z,
                    land_px=0, strag_px=0, n_strag=0, main_on_border=False, missing=False)
    sizes = np.bincount(lbl.ravel()); sizes[0] = 0
    main = int(sizes.argmax())
    border = _border_labels(lbl)
    strag_labels = sorted(border - {main})
    strag = np.isin(lbl, strag_labels) if strag_labels else np.zeros_like(land)
    return dict(key=key, name=entry["name"], land=land, main=(lbl == main), strag=strag,
                land_px=int(land.sum()), strag_px=int(strag.sum()),
                n_strag=len(strag_labels), main_on_border=(main in border), missing=False)


def _key(fn: str) -> str:
    t = Path(fn).name.split("_")
    return f"{t[0]}_{t[1]}"


def _panel_rgb(r, ds=4):
    land, main, strag = r["land"][::ds, ::ds], r["main"][::ds, ::ds], r["strag"][::ds, ::ds]
    img = np.full((*land.shape, 3), (16, 28, 48), np.uint8)     # ocean
    img[land] = (110, 110, 120)                                 # generic land
    img[main] = (70, 150, 70)                                   # main island
    img[strag] = (220, 55, 55)                                  # border stragglers
    return img


def main():
    lay = json.loads((ISL / "layout.json").read_text())["islands"]
    results = []
    for e in lay:
        r = analyze(e)
        if r.get("missing"):
            print(f"  {r['key']:9s} {r['name'][:30]:30s} (no baked height.tif — skipped)", flush=True)
            continue
        results.append(r)
        flag = " MAIN-ON-BORDER!" if r["main_on_border"] else ""
        print(f"  {r['key']:9s} {r['name'][:30]:30s} stragglers={r['n_strag']:2d} "
              f"px={r['strag_px']:7d} ({r['strag_px']/max(r['land_px'],1)*100:4.1f}% of land){flag}",
              flush=True)

    # contact sheet
    ncol = 5; nrow = (len(results) + ncol - 1) // ncol
    fig, axes = plt.subplots(nrow, ncol, figsize=(ncol * 3.0, nrow * 3.0), facecolor="#0d1b2a")
    for ax in np.ravel(axes):
        ax.axis("off")
    for r, ax in zip(results, np.ravel(axes)):
        ax.imshow(_panel_rgb(r))
        pct = r["strag_px"] / max(r["land_px"], 1) * 100
        col = "#ff6666" if r["strag_px"] > 0 else "#88aa88"
        title = f"{r['key']}  {r['n_strag']} strag · {pct:.1f}%"
        if r["main_on_border"]:
            title += "  ⚠MAIN"
        ax.set_title(title, color=col, fontsize=8)
        for sp in ax.spines.values():
            sp.set_visible(True); sp.set_edgecolor("#ff3333" if r["main_on_border"] else "#335577")
        ax.axis("on"); ax.set_xticks([]); ax.set_yticks([])
    fig.suptitle("Edge fragments — RED = land touching the frame border (stragglers) · green = main island",
                 color="#cccccc", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    out = ISL / "out" / "edge_fragments.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=110, facecolor="#0d1b2a")
    print(f"\nwrote {out}")

    # summary
    bad = [r for r in results if r["strag_px"] > 0]
    main_b = [r for r in results if r["main_on_border"]]
    print(f"\n{len(bad)}/{len(results)} islands have border stragglers.")
    if main_b:
        print("MAIN island touches the border (needs re-framing, NOT auto-kill): "
              + ", ".join(r["key"] for r in main_b))


if __name__ == "__main__":
    main()
