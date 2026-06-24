"""import_island_raster.py — map a real land-cover raster (islands/island_geo_data/<key>/)
to Vandir override zone codes, GEO-ALIGNED to an island's mask bbox.

Plug-and-play: drop landcover.tif (+ optional classmap.json) under
islands/island_geo_data/<key>/ (or a <key>.zip). The raster is REPROJECTED from
its own CRS into the DEM's web-mercator grid (computed from the DEM filename's
lat/lon/zoom), then run through the same flip/rotate the bake applies -> it lines
up with the island. Returns an override array (bbox-sized, 0 = unmapped -> bake
falls back to altitude bands). 0-byte / no-CRS rasters fall back to a plain resize.

classmap.json: {"10":"TEMPERATE_RAINFOREST", ...} (categorical) or {"#2e7d32":...}
(RGB). null value = leave 0 (ocean/water).
"""
from __future__ import annotations
import sys, json, math, zipfile, tempfile
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.biome_assignment import OVERRIDE_BIOME_MAP

GEO = ROOT / "islands" / "island_geo_data"
ISL = ROOT / "islands"
_NAME_TO_ZONE = {v: k for k, v in OVERRIDE_BIOME_MAP.items()}
_R = 6378137.0


def _safe(n):
    import re
    return re.sub(r"[^a-z0-9]+", "_", n.lower()).strip("_")


def _layout_entry(island_name):
    layout = json.loads((ISL / "layout.json").read_text())
    return next((i for i in layout["islands"] if _safe(i["name"]) == island_name
                 or _safe(i["name"]) in island_name), None)


def _key_fragments(island_name, entry):
    keys = [island_name]
    if entry:
        keys.append(Path(entry["dem_path"]).stem)
        import re
        m = re.match(r"(-?\d+_-?\d+)", Path(entry["dem_path"]).name)
        if m:
            keys.append(m.group(1))
    return keys


def _find_raster(island_name, entry):
    for k in _key_fragments(island_name, entry):
        d = GEO / k
        if d.is_dir():
            for ext in ("landcover.tif", "*.tif", "*.tiff", "*.img", "*.png"):
                hit = sorted(d.glob(ext))
                if hit:
                    cm = d / "classmap.json"
                    return hit[0], (cm if cm.exists() else None)
        z = GEO / f"{k}.zip"
        if z.exists():
            tmp = Path(tempfile.mkdtemp())
            with zipfile.ZipFile(z) as zf:
                zf.extractall(tmp)
            for ext in ("*.tif", "*.tiff", "*.img"):
                hit = sorted(tmp.rglob(ext))
                if hit:
                    return hit[0], next(iter(tmp.rglob("classmap.json")), None)
    return None, None


def _parse_dem_geo(dem_path):
    """DEM filename '<lat>_<frac>_<lon>_<frac>_<zoom>_<w>_<h>_...' -> (lat,lon,zoom,w,h)."""
    p = Path(dem_path).name.split("_")
    lat = float(f"{p[0]}.{p[1]}")
    lon = float(f"{p[2]}.{p[3]}")
    return lat, lon, int(p[4]), int(p[5]), int(p[6])


def _merc(lon, lat):
    return _R * math.radians(lon), _R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _dem_grid(lat, lon, zoom, size):
    """Web-mercator transform for the DEM tile (center lat/lon, zoom, size px)."""
    from rasterio.transform import from_bounds
    cx, cy = _merc(lon, lat)
    res = 40075016.6855785 / (256.0 * 2 ** zoom)        # web-merc m/px at zoom
    half = size / 2.0 * res
    return from_bounds(cx - half, cy - half, cx + half, cy + half, size, size)


def _reproject_to_dem(raster_path, dst_transform, size):
    import rasterio
    from rasterio.warp import reproject, Resampling
    with rasterio.open(str(raster_path)) as src:
        dst = np.zeros((size, size), np.uint8)
        reproject(source=rasterio.band(src, 1), destination=dst,
                  src_transform=src.transform, src_crs=src.crs,
                  dst_transform=dst_transform, dst_crs="EPSG:3857",
                  resampling=Resampling.nearest)
    return dst


def _classes_to_zones(cls, classmap):
    if classmap is None:
        return cls.astype(np.uint8)                      # assume already zone codes
    zones = np.zeros(cls.shape, np.uint8)
    for ck, name in classmap.items():
        try:
            ci = int(ck)
        except (ValueError, TypeError):
            continue
        zones[cls == ci] = _NAME_TO_ZONE.get(name, 0) if name else 0
    return zones


def _load_classmap(path):
    return json.loads(Path(path).read_text()) if path else None


def _align_to_bbox(zones, entry, bbox_hw):
    from scipy.ndimage import rotate as nd_rotate, zoom
    a = zones
    if entry:
        if entry.get("flipx"):
            a = np.fliplr(a)
        if entry.get("flipz"):
            a = np.flipud(a)
        rot = float(entry.get("rot_deg", 0.0))
        if rot % 360:
            a = nd_rotate(a, -rot, reshape=True, order=0, cval=0, prefilter=False)
    H, W = bbox_hw
    z = zoom(a, (H / a.shape[0], W / a.shape[1]), order=0)
    out = np.zeros((H, W), np.uint8)
    hh, ww = min(H, z.shape[0]), min(W, z.shape[1])
    out[:hh, :ww] = z[:hh, :ww]
    return out


def _aligned_classes(island_name, bbox_hw):
    """Reproject the raster to the DEM grid, then flip/rotate/fit to the bbox —
    returns the aligned raw CLASS array (uint8) + classmap, or (None, None)."""
    entry = _layout_entry(island_name)
    raster, cmpath = _find_raster(island_name, entry)
    if raster is None:
        return None, None
    classmap = _load_classmap(cmpath)
    classes = None
    if entry:
        try:
            import rasterio
            lat, lon, zoom, w, h = _parse_dem_geo(entry["dem_path"])
            with rasterio.open(str(raster)) as s:
                has_crs = s.crs is not None
            if has_crs:
                classes = _reproject_to_dem(raster, _dem_grid(lat, lon, zoom, w), w)
        except Exception as e:
            print(f"[geo] reproject failed ({e}); plain resize fallback", flush=True)
    if classes is None:
        import rasterio
        classes = rasterio.open(str(raster)).read(1)
    return _align_to_bbox(classes, entry, bbox_hw), classmap


def load_override_from_geo(island_name, bbox_hw):
    """-> (H,W) uint8 override zones from a real raster (geo-aligned), or None."""
    classes, classmap = _aligned_classes(island_name, bbox_hw)
    if classes is None:
        return None
    return _classes_to_zones(classes, classmap)


def load_notree_from_geo(island_name, bbox_hw):
    """-> (H,W) uint8 mask (1 where the raster class is a non-forest '_notree'
    class, e.g. pasture/cane/grass/bare) -> drives hydro_floodplain so the bake
    force-grasses + suppresses trees there. None if no raster / no _notree list."""
    classes, classmap = _aligned_classes(island_name, bbox_hw)
    if classes is None or not classmap:
        return None
    nt = classmap.get("_notree")
    if not nt:
        return None
    return np.isin(classes, [int(c) for c in nt]).astype(np.uint8)


if __name__ == "__main__":
    import argparse, rasterio
    ap = argparse.ArgumentParser(); ap.add_argument("--island", required=True)
    a = ap.parse_args()
    mdir = ROOT / "islands" / "masks_islands"
    cand = next((d.name for d in mdir.iterdir() if a.island in d.name or _safe(a.island) in d.name), None)
    if not cand:
        print("island not baked:", a.island); raise SystemExit(1)
    bbox = rasterio.open(mdir / cand / "height.tif").read(1).shape
    ov = load_override_from_geo(cand, bbox)
    if ov is None:
        print(f"no raster in {GEO} for {cand}")
    else:
        zs = {OVERRIDE_BIOME_MAP.get(int(z), int(z)): int((ov == z).sum()) for z in np.unique(ov) if z}
        print(f"mapped raster -> {sum(v for v in zs.values()):,} land px:")
        for n, c in sorted(zs.items(), key=lambda kv: -kv[1]):
            print(f"  {n:24} {c:>9,}")
