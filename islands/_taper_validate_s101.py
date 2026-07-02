"""_taper_validate_s101.py — validate the S101 bake-time EDGE DEPTH TAPER.

For a baked island (islands/masks_islands/<name>/height.tif):
  1. recompute the render-footprint tile set (render_drive._content_tiles rule:
     land tiles + 1 buffer ring seeded by tiles >= apron_seed_min_px) and its
     boundary ring, using the SAME helpers the bake uses (imported from
     render_islands — no duplicated rule);
  2. report max blocks-above-target + px-count >3 above target on the boundary
     ring for the CURRENT height.tif, and, with --before <pre-taper height.tif>,
     the same numbers for the pre-taper bake (after must be ~0);
  3. with --before, verify NO land cell changed, NO cell got shallower, and every
     changed cell lies inside the taper band;
  4. write a side-by-side before/after PNG of the depth field around the worst
     boundary-edge cell.

Usage:
  py islands/_taper_validate_s101.py --name grand_turk
      [--before C:/path/height_before.tif] [--png out.png] [--band 320] [--target -17]

Read-only on the masks dir (PNG goes to islands/_taper_val_s101/ by default).
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
import numpy as np

ISL = Path(__file__).resolve().parent
sys.path.insert(0, str(ISL))
from render_islands import (_footprint_tiles_from_height, _edge_distance_field,
                            _expand_tiles_to_px, raw_to_mcy, safe_name,
                            TAPER_BAND_PX, TAPER_TARGET_MCY, SEA_RAW)

MASKS_OUT = ISL / "masks_islands"
PNG_DIR = ISL / "_taper_val_s101"


def _read_tif(path):
    import rasterio
    with rasterio.open(str(path)) as src:
        return src.read(1)


def _ring_stats(mcy, land, ring, target):
    """(max blocks above target, px >3 above, px on ring) over OCEAN ring cells."""
    roc = ring & ~land
    if not roc.any():
        return 0.0, 0, 0
    above = mcy[roc] - target
    return float(above.max()), int((above > 3.0).sum()), int(roc.sum())


def _analyze(height, apron_seed_min_px, band, target, label):
    H, W = height.shape
    mcy = raw_to_mcy(height)
    land = mcy > 63.4
    sel_t = _footprint_tiles_from_height(height, apron_seed_min_px)
    d = _edge_distance_field(sel_t, H, W, band)
    rend = _expand_tiles_to_px(sel_t, H, W)
    ring = rend & (d < 1.5)
    mx, n3, nring = _ring_stats(mcy, land, ring, target)
    n_land_ring = int((ring & land).sum())
    in_band = rend & (d <= band)
    n_land_band = int((in_band & land).sum())
    # land proximity to the boundary (verifies the ">200px from land" assumption)
    land_d_min = float(d[land].min()) if land.any() else float("inf")
    print(f"[{label}] footprint: {int(sel_t.sum())} tiles | boundary ring: {nring}px ocean, "
          f"{n_land_ring}px land | band(<= {band:.0f}px): {n_land_band}px land inside | "
          f"min dist(land -> boundary) = {land_d_min:.0f}px")
    print(f"[{label}] ring ocean vs target Y{target:.0f}:  max above = {mx:.2f} blocks,  "
          f"px >3 above = {n3}")
    return dict(mcy=mcy, land=land, sel_t=sel_t, d=d, ring=ring,
                max_above=mx, n_above3=n3)


def _worst_cell(mcy, land, ring, target):
    roc = ring & ~land
    if not roc.any():
        return None
    above = np.where(roc, mcy - target, -np.inf)
    return np.unravel_index(int(np.argmax(above)), mcy.shape)


def _png(before, after, target, cell, out_path, name, band):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    r, c = cell
    H, W = after["mcy"].shape
    hh, hw = 384, 640                      # crop half-height/width
    r0, r1 = max(r - hh, 0), min(r + hh, H)
    c0, c1 = max(c - hw, 0), min(c + hw, W)
    panels = [("BEFORE", before), ("AFTER", after)] if before is not None else [("CURRENT", after)]
    fig, axes = plt.subplots(1, len(panels), figsize=(7 * len(panels), 6), squeeze=False)
    vmin, vmax = target - 10.0, 70.0
    for ax, (ttl, data) in zip(axes[0], panels):
        crop = data["mcy"][r0:r1, c0:c1]
        im = ax.imshow(crop, cmap="viridis", vmin=vmin, vmax=vmax,
                       extent=[c0, c1, r1, r0], interpolation="nearest")
        ax.contour(np.arange(c0, c1), np.arange(r0, r1), data["ring"][r0:r1, c0:c1],
                   levels=[0.5], colors="red", linewidths=0.8)
        ax.contour(np.arange(c0, c1), np.arange(r0, r1), crop, levels=[target],
                   colors="white", linewidths=0.5, linestyles="dotted")
        ax.set_title(f"{ttl}  mcy depth field (red = render boundary)")
        fig.colorbar(im, ax=ax, shrink=0.8, label="MC-Y")
    fig.suptitle(f"{name} — edge depth taper (band {band:.0f}px, target Y{target:.0f}) "
                 f"@ worst edge px ({r},{c})")
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=110)
    plt.close(fig)
    print(f"[png] wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="island safe-name substring")
    ap.add_argument("--before", help="pre-taper height.tif snapshot for A/B checks")
    ap.add_argument("--png", help="output PNG (default islands/_taper_val_s101/<name>.png)")
    ap.add_argument("--band", type=float, default=None)
    ap.add_argument("--target", type=float, default=None)
    a = ap.parse_args()

    dirs = [p for p in MASKS_OUT.iterdir() if p.is_dir() and a.name in p.name]
    if len(dirs) != 1:
        sys.exit(f"--name {a.name!r} matches {len(dirs)} dirs: {[p.name for p in dirs]}")
    mdir = dirs[0]
    man = json.loads((mdir / "manifest.json").read_text())
    tp = man.get("edge_depth_taper") or {}
    band = a.band if a.band is not None else float(tp.get("band_px", TAPER_BAND_PX))
    target = a.target if a.target is not None else float(tp.get("target_mcy", TAPER_TARGET_MCY))

    # apron_seed_min_px from layout.json (the drive reads it from the layout entry)
    seed_min = int(tp.get("apron_seed_min_px", -1))
    if seed_min < 0:
        layout = json.loads((ISL / "layout.json").read_text())
        ent = next((i for i in layout["islands"] if safe_name(i["name"]) == mdir.name), None)
        seed_min = int(ent.get("apron_seed_min_px", 0)) if ent else 0

    print(f"== {mdir.name}  band={band:.0f}px target=Y{target:.0f} apron_seed_min_px={seed_min}")
    after = _analyze(_read_tif(mdir / "height.tif"), seed_min, band, target, "after")

    before = None
    if a.before:
        h_b = _read_tif(a.before)
        h_a = _read_tif(mdir / "height.tif")
        before = _analyze(h_b, seed_min, band, target, "before")
        same_fp = bool(np.array_equal(before["sel_t"], after["sel_t"]))
        chg = h_a != h_b
        n_chg = int(chg.sum())
        land_changed = int((chg & before["land"]).sum())
        raised = int((h_a > h_b).sum())
        outside_band = int((chg & ~(after["d"] <= band)).sum())
        print(f"[diff] footprint tile set identical: {same_fp}")
        print(f"[diff] changed px: {n_chg} | land changed: {land_changed} (must be 0) | "
              f"raised (shallower): {raised} (must be 0) | changed outside band: {outside_band} (must be 0)")
        ok = same_fp and land_changed == 0 and raised == 0 and outside_band == 0
        print(f"[diff] {'PASS' if ok else 'FAIL'}")
        del h_a, h_b, chg

    # note: rock_gap on tapered ocean would re-trip the S97 flow_erosion rock clamp
    rg_path = mdir / "rock_gap.tif"
    if rg_path.exists():
        rg = _read_tif(rg_path) > 0
        n_rock_band = int((rg & ~after["land"] & (after["d"] <= band)).sum())
        print(f"[note] rock_gap px on ocean inside taper band: {n_rock_band}")
        del rg

    src = before if before is not None else after
    cell = _worst_cell(src["mcy"], src["land"], src["ring"], target)
    if cell is not None:
        out = Path(a.png) if a.png else PNG_DIR / f"{mdir.name}_edge.png"
        _png(before, after, target, cell, out, mdir.name, band)

    verdict = "PASS" if after["max_above"] <= 0.5 else "FAIL"
    print(f"[verdict] boundary-ring ocean max above target AFTER = "
          f"{after['max_above']:.2f} blocks -> {verdict}")


if __name__ == "__main__":
    main()
