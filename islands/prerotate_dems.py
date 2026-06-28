"""prerotate_dems.py — bake the layout rotation INTO the DEM, offline.

WHY (S96 rotated-island fix): render_islands.bake_island DERIVES the four masks
(height/slope/flow/erosion) from the UN-rotated DEM, then flip+rotates EACH derived
mask with scipy nd_rotate(order=1 BILINEAR, reshape=True). Bilinear rotation of the
already-derived SHARP-valued slope/flow/erosion masks injects interpolation
artifacts -> JAGGED terrain; and the out-of-frame corners get cval=0 on the derived
masks (sea_raw only on height) -> the renderer grows gray "coastal land" SLABS in
the bbox corners. Clean rotations (0deg Bahamas, 90deg lossless transpose Grand
Turk) render fine; diagonal angles are broken.

FIX: pre-rotate the DEM (the HEIGHT SOURCE) here, offline, applying the EXACT same
flip+rotate the bake would, with cval = the DEM's OWN detected sea-level raw value
so the out-of-DEM corners become OCEAN (not 0/garbage). Save as a new 16-bit DEM.
Then the bake runs with rot_deg=0 / flipx=false / flipz=false: it DERIVES all four
masks FRESH from the axis-aligned pre-rotated height -> SMOOTH derived masks, the
rotation loop is a no-op, and the sea-level corners derive cleanly to ocean (land =
mcy>63.4 is false there -> override 254). This makes a diagonal-rotated island LOOK
UNROTATED to the pipeline, exactly like Bahamas/Grand-Turk.

Per island this:
  1. loads the original DEM (Downloads/<key>_..._16bit.png),
  2. fliplr/flipud per the CURRENT layout flipx/flipz,
  3. nd_rotate(-rot_deg, reshape=True, order=1, cval=detect_sea_raw(DEM)),
  4. saves islands/prerot_dems/<key>_prerot_16bit.png (16-bit, keeps the <key> token
     so render_islands._key + _resolve_reg + spline_overrides still resolve),
  5. updates islands/layout.json: dem_path -> the new DEM, rot_deg=0, flipx/flipz=false.

ISLAND-ONLY: touches islands/layout.json + writes new island DEM files. No mainland
path, no config/thresholds.json, no core/.

Usage:
    py islands/prerotate_dems.py --list
    py islands/prerotate_dems.py --island grenada           # one (substring match)
    py islands/prerotate_dems.py --all                      # every rot_deg!=0 island
    py islands/prerotate_dems.py --island grenada --dry-run # don't write layout
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ISL = ROOT / "islands"
LAYOUT = ISL / "layout.json"
PREROT_DIR = ISL / "prerot_dems"

from derive_masks_from_height import load_dem, detect_sea_raw


def _safe(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _key_token(dem_name: str) -> str:
    """Leading `{lat}_{lon}` token of a DEM filename, e.g. 12_445."""
    return "_".join(Path(dem_name).name.split("_")[:2])


def prerotate_one(entry, dry_run=False):
    """Bake flip+rot into entry's DEM, save a new DEM, and return the updated entry
    dict (dem_path/rot_deg/flipx/flipz mutated). Returns (entry, info-dict)."""
    src_path = Path(entry["dem_path"])
    flipx = bool(entry.get("flipx", False))
    flipz = bool(entry.get("flipz", False))
    rot = float(entry.get("rot_deg", 0.0))

    dem = load_dem(src_path)                          # float64
    sea_raw = detect_sea_raw(dem)                     # DEM's OWN sea-level raw value

    # EXACT same transform sequence as render_islands.bake_island lines ~501-507,
    # applied to the DEM (the height source) instead of the derived masks.
    a = dem
    if flipx:
        a = np.fliplr(a)
    if flipz:
        a = np.flipud(a)
    rotated = (rot % 360) != 0
    if rotated:
        from scipy.ndimage import rotate as nd_rotate
        a = nd_rotate(a.astype(np.float32), -rot, reshape=True, order=1,
                      cval=float(sea_raw), prefilter=False)
        # clamp to the 16-bit DEM range; round to integer raw (DEMs are integer-valued)
        a = np.clip(np.round(a), 0, 65535).astype(np.uint16)
    else:
        a = np.clip(np.round(a), 0, 65535).astype(np.uint16)

    # corner-ocean sanity: the four corners must read at/below the DEM sea level so
    # the bake's land=mcy>63.4 is false there (raw<=sea -> mcy<=63 -> ocean).
    cs = int(max(8, min(a.shape) // 64))              # small corner sample block
    corners = np.concatenate([
        a[:cs, :cs].ravel(), a[:cs, -cs:].ravel(),
        a[-cs:, :cs].ravel(), a[-cs:, -cs:].ravel()])
    corner_max = int(corners.max())
    corner_med = float(np.median(corners))
    corner_ocean = corner_max <= int(round(sea_raw)) + 1

    PREROT_DIR.mkdir(parents=True, exist_ok=True)
    out_name = f"{_key_token(src_path.name)}_prerot_16bit.png"
    out_path = PREROT_DIR / out_name
    # save 16-bit grayscale PNG (mode 'I;16' so load_dem reads it back as raw uint16)
    Image.fromarray(a, mode="I;16").save(out_path)

    info = dict(src=str(src_path), out=str(out_path),
                src_shape=list(dem.shape), out_shape=list(a.shape),
                sea_raw=float(sea_raw), rot=rot, flipx=flipx, flipz=flipz,
                corner_max=corner_max, corner_med=corner_med,
                corner_ocean=bool(corner_ocean))

    if not dry_run:
        entry["dem_path"] = str(out_path)
        entry["rot_deg"] = 0.0
        entry["flipx"] = False
        entry["flipz"] = False
        # record provenance so re-runs/inspection know this is pre-rotated
        entry["prerot_from"] = info["src"]
        entry["prerot_sea_raw"] = float(sea_raw)
    return entry, info


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--island", help="island name/dem substring (one island)")
    ap.add_argument("--all", action="store_true", help="every rot_deg!=0 island")
    ap.add_argument("--list", action="store_true", help="list rotated islands and exit")
    ap.add_argument("--dry-run", action="store_true", help="compute + save DEM but DON'T write layout.json")
    a = ap.parse_args()

    layout = json.loads(LAYOUT.read_text())
    islands = layout["islands"]

    rotated = [i for i in islands if float(i.get("rot_deg", 0.0)) % 360 != 0]
    if a.list:
        print("Rotated islands (rot_deg != 0):")
        for i in rotated:
            print(f"  {_safe(i['name']):28s} rot={i.get('rot_deg')!s:>5}  "
                  f"flipx={i.get('flipx')} flipz={i.get('flipz')}  "
                  f"dem={Path(i['dem_path']).name}")
        return 0

    if a.island:
        targets = [i for i in islands
                   if a.island.lower() in _safe(i["name"]) or a.island in i["dem_path"]]
        if not targets:
            ap.error(f"no island matches {a.island!r}")
    elif a.all:
        targets = rotated
    else:
        ap.error("pass --island NAME, --all, or --list")

    changed = 0
    for entry in targets:
        nm = _safe(entry["name"])
        rot = float(entry.get("rot_deg", 0.0))
        if rot % 360 == 0 and not (entry.get("flipx") or entry.get("flipz")):
            print(f"[prerot] {nm}: rot=0 + no flip -> nothing to bake, skipping", flush=True)
            continue
        print(f"[prerot] {nm}: baking flip/rot into DEM...", flush=True)
        _, info = prerotate_one(entry, dry_run=a.dry_run)
        print(f"[prerot]   src {info['src_shape']} -> out {info['out_shape']}  "
              f"sea_raw={info['sea_raw']:.0f}", flush=True)
        print(f"[prerot]   corners: max={info['corner_max']} med={info['corner_med']:.0f} "
              f"ocean={info['corner_ocean']}  -> {Path(info['out']).name}", flush=True)
        if not info["corner_ocean"]:
            print(f"[prerot]   WARNING: corners NOT below sea ({info['corner_max']} > "
                  f"{info['sea_raw']:.0f}) -- expect corner land!", flush=True)
        changed += 1

    if not a.dry_run and changed:
        LAYOUT.write_text(json.dumps(layout, indent=2))
        print(f"[prerot] updated layout.json for {changed} island(s) "
              f"(dem_path->prerot, rot_deg=0, flips=false)", flush=True)
    elif a.dry_run:
        print(f"[prerot] DRY-RUN: saved {changed} pre-rotated DEM(s); layout.json NOT modified", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
