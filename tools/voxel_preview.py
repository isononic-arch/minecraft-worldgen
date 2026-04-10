"""
voxel_preview.py — Vandir Voxel Preview Tool
============================================
PyQt6 + WebGL interactive terrain inspector.

Features
  • 3D terrain view  — WebGL height mesh, mouse orbit/zoom
  • Cross-section    — vertical slice with cliff-banding reconstruction
  • Live cliff params — band_scale_y / cliff_deg_thr sliders, instant re-render

Launch:
    python tools/voxel_preview.py [--tile-x 56] [--tile-z 46]
    python tools/voxel_preview.py --tile-x 56 --tile-z 46
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QSplitter, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QPushButton, QSpinBox, QSlider, QComboBox, QSizePolicy,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------
PROJECT_ROOT = _PROJECT_ROOT
MASKS_DIR    = PROJECT_ROOT / "masks"
CONFIG_PATH  = PROJECT_ROOT / "config" / "thresholds.json"
TILE_SIZE    = 512
Y_MIN, Y_MAX, SEA_Y = -64, 448, 63
Y_RANGE = Y_MAX - Y_MIN  # 512

# ---------------------------------------------------------------------------
# Block colors  (RGB tuples)
# ---------------------------------------------------------------------------
BLOCK_COLORS: dict[str, tuple] = {
    "bedrock":           (0x3A, 0x3A, 0x3A),
    "stone":             (0x8A, 0x8A, 0x8A),
    "cobblestone":       (0x72, 0x72, 0x72),
    "andesite":          (0x8C, 0x8C, 0x8E),
    "polished_andesite": (0x88, 0x88, 0x90),
    "diorite":           (0xC4, 0xC2, 0xC0),
    "calcite":           (0xDE, 0xDC, 0xD8),
    "granite":           (0xAA, 0x7A, 0x66),
    "tuff":              (0x6A, 0x6B, 0x5E),
    "sandstone":         (0xD9, 0xC9, 0x82),
    "red_sand":          (0xBF, 0x6B, 0x2C),
    "sand":              (0xE3, 0xD6, 0x98),
    "gravel":            (0x99, 0x96, 0x90),
    "dirt":              (0x8B, 0x60, 0x3A),
    "coarse_dirt":       (0x72, 0x4D, 0x2D),
    "rooted_dirt":       (0x78, 0x52, 0x32),
    "podzol":            (0x67, 0x40, 0x1E),
    "mud":               (0x54, 0x44, 0x32),
    "clay":              (0x9E, 0xA3, 0xAA),
    "grass_block":       (0x59, 0x9B, 0x3A),
    "moss_block":        (0x4E, 0x7A, 0x30),
    "mycelium":          (0x7C, 0x68, 0x7C),
    "snow_block":        (0xF0, 0xF4, 0xF8),
    "ice":               (0xA0, 0xC8, 0xF0),
    "packed_ice":        (0x80, 0xB0, 0xE8),
    "water":             (0x2E, 0x6E, 0xB8),
    "oak_log":           (0x68, 0x4E, 0x2A),
    "oak_leaves":        (0x3A, 0x7A, 0x2A),
    "spruce_log":        (0x4E, 0x38, 0x1E),
    "spruce_leaves":     (0x28, 0x60, 0x28),
    "birch_log":         (0xD8, 0xD6, 0xC8),
    "birch_leaves":      (0x52, 0x8A, 0x3A),
    "jungle_log":        (0x4A, 0x3A, 0x18),
    "jungle_leaves":     (0x22, 0x6A, 0x18),
    "mangrove_roots":    (0x3A, 0x2A, 0x18),
    "terracotta":        (0x97, 0x5F, 0x45),
    "red_terracotta":    (0xA0, 0x40, 0x28),
    "air":               (0x18, 0x38, 0x60),
}
_DEFAULT_COLOR = (0x60, 0x20, 0x80)


def _block_rgb(name: str) -> tuple:
    return BLOCK_COLORS.get(name, _DEFAULT_COLOR)


# ---------------------------------------------------------------------------
# Cliff banding  (mirrors chunk_writer.py exactly)
# ---------------------------------------------------------------------------
_BIOME_CLIFF_STONE: dict[str, str] = {
    "ALPINE_MEADOW":           "andesite",
    "ARCTIC_TUNDRA":           "andesite",
    "BOREAL_TAIGA":            "andesite",
    "SNOWY_BOREAL_TAIGA":      "andesite",
    "FROZEN_FLATS":            "andesite",
    "COASTAL_HEATH":           "andesite",
    "SCRUBBY_HEATHLAND":       "andesite",
    "KARST_BARRENS":           "tuff",
    "SAND_DUNE_DESERT":        "sandstone",
    "DESERT_STEPPE_TRANSITION":"sandstone",
    "SEMI_ARID_SHRUBLAND":     "sandstone",
    "DRY_WOODLAND_MAQUIS":     "sandstone",
    "DRY_PINE_BARRENS":        "sandstone",
    "DRY_OAK_SAVANNA":         "sandstone",
    "MIXED_FOREST":            "granite",
    "BIRCH_FOREST":            "granite",
    "CONTINENTAL_STEPPE":      "granite",
    "TEMPERATE_RAINFOREST":    "diorite",
    "TEMPERATE_DECIDUOUS":     "diorite",
    "RAINFOREST_COAST":        "diorite",
    "LUSH_RAINFOREST_COAST":   "diorite",
    "EASTERN_TEMPERATE_COAST": "diorite",
    "RIPARIAN_WOODLAND":       "stone",
    "MANGROVE_COAST":          "stone",
    "FRESHWATER_FEN":          "stone",
    "TIDAL_JUNGLE_FRINGE":     "stone",
}
_DEFAULT_CLIFF_STONE = "stone"

_CLIFF_BANDS: dict[str, list[str]] = {
    "stone":     ["stone",     "gravel",    "tuff",        "cobblestone", "stone"],
    "andesite":  ["andesite",  "cobblestone","stone",       "gravel",      "andesite"],
    "diorite":   ["diorite",   "stone",     "calcite",     "gravel",      "diorite"],
    "granite":   ["granite",   "stone",     "cobblestone", "gravel",      "granite"],
    "tuff":      ["tuff",      "stone",     "gravel",      "tuff",        "cobblestone"],
    "sandstone": ["sandstone", "red_sand",  "gravel",      "sandstone",   "sand"],
}


def _cell_hash_scalar(ri: int, ci: int) -> float:
    """Scalar version of chunk_writer's _cell_hash for cross-section reconstruction."""
    ri2, ci2 = ri & 0xFFFFFFFF, ci & 0xFFFFFFFF
    h = (ri2 * 2654435761 ^ ci2 * 2246822519) & 0xFFFFFFFF
    h ^= (h >> 16) & 0xFFFFFFFF
    h = (h * 0x45D9F3B) & 0xFFFFFFFF
    h ^= (h >> 16) & 0xFFFFFFFF
    return h * 2.3283064e-10  # / 2^32


def _banded_stone(biome: str, world_x: int, world_z: int, mc_y: int,
                  band_scale_y: int) -> str:
    """Return the cliff-band block name for a single (x, y, z) interior stone position."""
    prim = _BIOME_CLIFF_STONE.get(biome, _DEFAULT_CLIFF_STONE)
    variants = _CLIFF_BANDS.get(prim, _CLIFF_BANDS["stone"])
    n_v = len(variants)
    wave_amp  = max(1, band_scale_y // 3)
    wave_cell = 32
    rc = world_z // wave_cell
    cc = world_x // wave_cell
    rf = (world_z % wave_cell) / wave_cell
    cf = (world_x % wave_cell) / wave_cell
    v00 = _cell_hash_scalar(rc,   cc)
    v10 = _cell_hash_scalar(rc+1, cc)
    v01 = _cell_hash_scalar(rc,   cc+1)
    v11 = _cell_hash_scalar(rc+1, cc+1)
    sm  = v00*(1-rf)*(1-cf) + v10*rf*(1-cf) + v01*(1-rf)*cf + v11*rf*cf
    waviness = int((sm * 2 - 1) * wave_amp)
    band_idx = ((mc_y + waviness) // band_scale_y) % n_v
    return variants[band_idx]


# ---------------------------------------------------------------------------
# Pipeline runner (returns surface_y, biome_grid, surface_blk)
# ---------------------------------------------------------------------------

BIOME_TO_MC = {
    "COASTAL_HEATH":            "minecraft:windswept_hills",
    "TEMPERATE_RAINFOREST":     "minecraft:old_growth_spruce_taiga",
    "BOREAL_TAIGA":             "minecraft:taiga",
    "SNOWY_BOREAL_TAIGA":       "minecraft:snowy_taiga",
    "ALPINE_MEADOW":            "minecraft:meadow",
    "ARCTIC_TUNDRA":            "minecraft:frozen_peaks",
    "FROZEN_FLATS":             "minecraft:ice_spikes",
    "TEMPERATE_DECIDUOUS":      "minecraft:forest",
    "RAINFOREST_COAST":         "minecraft:old_growth_birch_forest",
    "RIPARIAN_WOODLAND":        "minecraft:dark_forest",
    "DRY_OAK_SAVANNA":          "minecraft:savanna",
    "KARST_BARRENS":            "minecraft:windswept_gravelly_hills",
    "BIRCH_FOREST":             "minecraft:birch_forest",
    "EASTERN_TEMPERATE_COAST":  "minecraft:beach",
    "MIXED_FOREST":             "minecraft:forest",
    "CONTINENTAL_STEPPE":       "minecraft:plains",
    "DRY_PINE_BARRENS":         "minecraft:wooded_badlands",
    "SCRUBBY_HEATHLAND":        "minecraft:windswept_hills",
    "LUSH_RAINFOREST_COAST":    "minecraft:jungle",
    "SAND_DUNE_DESERT":         "minecraft:desert",
    "DESERT_STEPPE_TRANSITION": "minecraft:savanna_plateau",
    "SEMI_ARID_SHRUBLAND":      "minecraft:savanna",
    "DRY_WOODLAND_MAQUIS":      "minecraft:sparse_jungle",
    "TIDAL_JUNGLE_FRINGE":      "minecraft:sparse_jungle",
    "MANGROVE_COAST":           "minecraft:mangrove_swamp",
    "FRESHWATER_FEN":           "minecraft:swamp",
    "_OCEAN":                   "minecraft:ocean",
}


def run_pipeline(tx: int, tz: int, cfg: dict, progress_cb=None) -> dict:
    """
    Run pipeline steps 4–7 for tile (tx, tz).
    Returns dict with keys: surface_y, biome_grid, surface_blk, masks.
    progress_cb(label: str) is called at each step.
    """
    import importlib
    core_biome   = importlib.import_module("core.biome_assignment")
    core_tiles   = importlib.import_module("core.tile_streamer")
    core_col     = importlib.import_module("core.column_generator")
    core_river   = importlib.import_module("core.river_carver")
    core_dec     = importlib.import_module("core.surface_decorator")
    core_noise   = importlib.import_module("core.noise_fields")

    col_off, row_off = tx * TILE_SIZE, tz * TILE_SIZE

    if progress_cb: progress_cb("Reading masks…")
    masks = core_tiles.read_tile(
        masks_dir=MASKS_DIR, col_off=col_off, row_off=row_off,
        width=TILE_SIZE, height=TILE_SIZE,
    )
    noise = core_noise.load_noise_generators(CONFIG_PATH)

    if progress_cb: progress_cb("Biome assignment…")
    biome_grid = core_biome.assign_biomes(
        height_tile=masks["height"], slope_tile=masks["slope"],
        flow_tile=masks["flow"], erosion_tile=masks["erosion"],
        override_tile=masks["override"], noise_fields=noise, cfg=cfg,
    )

    if progress_cb: progress_cb("Column generation…")
    h_u16  = (masks["height"]  * 65535).astype(np.uint16)
    sl_u16 = (masks["slope"]   * 65535).astype(np.uint16)
    er_u16 = (masks["erosion"] * 65535).astype(np.uint16)
    fl_u16 = (masks["flow"]    * 65535).astype(np.uint16)
    sh_bool = masks["shore"] > 0.5
    dep_u16 = er_u16.copy()

    mc_biomes = np.empty(biome_grid.shape, dtype=object)
    for b in np.unique(biome_grid):
        mc_biomes[biome_grid == b] = BIOME_TO_MC.get(str(b), "minecraft:plains")

    col_results = core_col.process_tile_columns_v2(
        tile_height=h_u16, tile_slope=sl_u16, tile_erosion=er_u16,
        tile_flow=fl_u16, tile_deposits=dep_u16, tile_shore=sh_bool,
        tile_biomes=biome_grid, tile_mc_biomes=mc_biomes,
        tile_origin_x=col_off, tile_origin_y=row_off,
        noise_gens=noise, cfg=cfg,
    )

    if progress_cb: progress_cb("River carving…")
    core_river.carve_tile(tile_columns=col_results, tile_flow=masks["flow"], cfg=cfg)

    surface_y = np.array(
        [[cr.surface_y for cr in row] for row in col_results], dtype=np.int16
    )

    if progress_cb: progress_cb("Surface decoration…")
    flat = [cr for row in col_results for cr in row]
    def _get_blk(cr, dy, default):
        v = cr.blocks.get(cr.surface_y + dy)
        return v if v else default
    surface_blk = np.array(
        [_get_blk(cr, 0, "grass_block") for cr in flat], dtype=object
    ).reshape(TILE_SIZE, TILE_SIZE)

    return {
        "surface_y":   surface_y,
        "biome_grid":  biome_grid,
        "surface_blk": surface_blk,
        "masks":       masks,
        "tile_x":      tx,
        "tile_z":      tz,
        "col_off":     col_off,
        "row_off":     row_off,
    }


# ---------------------------------------------------------------------------
# Cross-section image  (Pillow — runs off-thread)
# ---------------------------------------------------------------------------

def make_xsec_image(state: dict, z_row: int, band_scale_y: int,
                    cliff_deg_thr: float,
                    img_w: int = 1024, img_h: int = 512) -> bytes:
    """
    Return PNG bytes of a vertical slice through the tile at grid row z_row.
    Reconstructs cliff banding using the same hash logic as chunk_writer.py.
    """
    from PIL import Image, ImageDraw

    surface_y  = state["surface_y"]    # (512, 512) int16
    biome_grid = state["biome_grid"]   # (512, 512) str
    surface_blk = state["surface_blk"] # (512, 512) str
    col_off    = state["col_off"]
    row_off    = state["row_off"]

    world_z = row_off + z_row

    # Cliff angle — same formula as build_column_array
    gy, gx = np.gradient(surface_y.astype(np.float32))
    cliff_deg = np.degrees(np.arctan(np.hypot(gx, gy)))  # (512, 512)

    # Scale factors
    x_scale  = img_w / TILE_SIZE          # pixels per block column
    y_scale  = img_h / Y_RANGE            # pixels per MC Y unit

    img = Image.new("RGB", (img_w, img_h), BLOCK_COLORS.get("air", (0x18, 0x38, 0x60)))
    px  = img.load()

    for col_x in range(TILE_SIZE):
        world_x = col_off + col_x
        sy      = int(surface_y[z_row, col_x])
        biome   = str(biome_grid[z_row, col_x])
        sblk    = str(surface_blk[z_row, col_x])
        cdeg    = float(cliff_deg[z_row, col_x])
        is_cliff = (cdeg >= cliff_deg_thr) and (sy > SEA_Y)

        px0 = int(col_x * x_scale)
        px1 = int((col_x + 1) * x_scale)

        for mc_y in range(Y_MIN, Y_MAX):
            # Determine block at this column / y
            if mc_y == Y_MIN:
                blk = "bedrock"
            elif mc_y == sy:
                blk = sblk
            elif mc_y == sy - 1 or mc_y == sy - 2:
                blk = "dirt"  # sub — approximate
            elif mc_y < sy - 2:
                if is_cliff:
                    blk = _banded_stone(biome, world_x, world_z, mc_y, band_scale_y)
                else:
                    blk = "stone"
            elif mc_y > sy and mc_y <= SEA_Y:
                blk = "water"
            else:
                blk = "air"

            rgb  = _block_rgb(blk)
            py0  = img_h - 1 - int((mc_y - Y_MIN + 1) * y_scale)
            py0  = max(0, min(img_h - 1, py0))

            for px_x in range(px0, min(px1, img_w)):
                px[px_x, py0] = rgb

    # Draw sea-level line
    draw = ImageDraw.Draw(img)
    sea_py = img_h - 1 - int((SEA_Y - Y_MIN) * y_scale)
    draw.line([(0, sea_py), (img_w - 1, sea_py)], fill=(0x00, 0xCC, 0xFF), width=1)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Encode terrain data for WebGL
# ---------------------------------------------------------------------------

def encode_terrain(state: dict, mesh_n: int = 256) -> str:
    """Return a JS call string: updateTerrain({...}) with base64-encoded arrays."""
    surface_y  = state["surface_y"]   # (512, 512) int16
    surface_blk = state["surface_blk"] # (512, 512) str

    step = TILE_SIZE // mesh_n
    sy_ds = surface_y[::step, ::step][:mesh_n, :mesh_n]
    sb_ds = surface_blk[::step, ::step][:mesh_n, :mesh_n]

    # Height: normalise to [0, 1]
    h_norm = (sy_ds.astype(np.float32) - Y_MIN) / Y_RANGE
    h_b64  = base64.b64encode(h_norm.astype(np.float32).tobytes()).decode()

    # Color: block RGB
    colors = np.zeros((mesh_n, mesh_n, 3), dtype=np.uint8)
    for r in range(mesh_n):
        for c in range(mesh_n):
            colors[r, c] = _block_rgb(str(sb_ds[r, c]))
    c_b64 = base64.b64encode(colors.tobytes()).decode()

    payload = json.dumps({
        "heights": h_b64,
        "colors":  c_b64,
        "meshN":   mesh_n,
    })
    return f"updateTerrain({payload})"


# ---------------------------------------------------------------------------
# WebGL HTML template
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0a0a10; overflow:hidden; }
canvas { width:100vw; height:100vh; display:block; }
#xsec { display:none; position:absolute; top:0; left:0; width:100%; height:100%; object-fit:contain; background:#0a0a10; }
#hud  { position:absolute; bottom:6px; left:8px; color:#888; font:11px monospace; pointer-events:none; }
</style>
</head>
<body>
<canvas id="c"></canvas>
<img id="xsec">
<div id="hud">Waiting for tile data…</div>
<script>
const canvas = document.getElementById('c');
const xsecImg = document.getElementById('xsec');
const hud = document.getElementById('hud');
const gl = canvas.getContext('webgl2');

// --- Camera state ---
let azimuth = 0.6, elevation = 0.5, dist = 2.8;
let drag = false, lastX = 0, lastY = 0;
canvas.addEventListener('mousedown', e => { drag=true; lastX=e.clientX; lastY=e.clientY; });
canvas.addEventListener('mouseup',   () => drag=false);
canvas.addEventListener('mousemove', e => {
    if (!drag) return;
    azimuth   += (e.clientX - lastX) * 0.01;
    elevation  = Math.max(0.05, Math.min(1.4, elevation - (e.clientY - lastY) * 0.008));
    lastX = e.clientX; lastY = e.clientY;
    render();
});
canvas.addEventListener('wheel', e => {
    dist = Math.max(0.5, Math.min(8.0, dist + e.deltaY * 0.003));
    render();
}, {passive:true});

// --- Resize ---
function resize() {
    canvas.width  = window.innerWidth;
    canvas.height = window.innerHeight;
    if (prog) render();
}
window.addEventListener('resize', resize);

// --- Shaders ---
const vs = `#version 300 es
precision highp float;
uniform sampler2D uH;
uniform sampler2D uC;
uniform mat4 uMVP;
uniform int uN;
out vec3 vColor;
out float vH;

void main(){
    int n = uN;
    int col = gl_VertexID % n;
    int row = gl_VertexID / n;
    vec2 uv = vec2(float(col)/float(n-1), float(row)/float(n-1));
    float h = texture(uH, uv).r;
    vH = h;
    vColor = texture(uC, uv).rgb;
    vec3 p = vec3(uv.x*2.0-1.0, h*2.0-1.0, uv.y*2.0-1.0);
    gl_Position = uMVP * vec4(p, 1.0);
}`;

const fs = `#version 300 es
precision mediump float;
in vec3 vColor;
in float vH;
out vec4 fc;
void main(){
    // Simple top-light hillshade baked into height gradient (approximated per-fragment)
    float shade = 0.7 + 0.3 * vH;
    fc = vec4(vColor * shade, 1.0);
}`;

function compileShader(src, type) {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS))
        console.error(gl.getShaderInfoLog(s));
    return s;
}

let prog, uMVP, uN, uH_loc, uC_loc;
let heightTex, colorTex, ibo;
let meshN = 0;

function initGL() {
    prog = gl.createProgram();
    gl.attachShader(prog, compileShader(vs, gl.VERTEX_SHADER));
    gl.attachShader(prog, compileShader(fs, gl.FRAGMENT_SHADER));
    gl.linkProgram(prog);
    uMVP  = gl.getUniformLocation(prog, 'uMVP');
    uN    = gl.getUniformLocation(prog, 'uN');
    uH_loc = gl.getUniformLocation(prog, 'uH');
    uC_loc = gl.getUniformLocation(prog, 'uC');

    heightTex = gl.createTexture();
    colorTex  = gl.createTexture();

    gl.enable(gl.DEPTH_TEST);
}

function b64ToFloat32(b64) {
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i=0; i<bin.length; i++) buf[i]=bin.charCodeAt(i);
    return new Float32Array(buf.buffer);
}
function b64ToUint8(b64) {
    const bin = atob(b64);
    const buf = new Uint8Array(bin.length);
    for (let i=0; i<bin.length; i++) buf[i]=bin.charCodeAt(i);
    return buf;
}

function uploadTexF32(tex, data, n) {
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, n, n, 0, gl.RED, gl.FLOAT, data);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
}
function uploadTexRGB(tex, data, n) {
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB8, n, n, 0, gl.RGB, gl.UNSIGNED_BYTE, data);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
}

function buildIBO(n) {
    const idx = new Uint32Array((n-1)*(n-1)*6);
    let k=0;
    for (let r=0;r<n-1;r++) for (let c=0;c<n-1;c++) {
        const a=r*n+c, b=a+1, d=a+n, e=d+1;
        idx[k++]=a; idx[k++]=b; idx[k++]=d;
        idx[k++]=b; idx[k++]=e; idx[k++]=d;
    }
    if (ibo) gl.deleteBuffer(ibo);
    ibo = gl.createBuffer();
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
    gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
}

// Called from Python via runJavaScript
function updateTerrain(data) {
    xsecImg.style.display = 'none';
    canvas.style.display  = 'block';
    if (!prog) initGL();
    meshN = data.meshN;
    const hData = b64ToFloat32(data.heights);
    const cData = b64ToUint8(data.colors);
    uploadTexF32(heightTex, hData, meshN);
    uploadTexRGB(colorTex,  cData, meshN);
    buildIBO(meshN);
    hud.textContent = `Tile loaded (${meshN}×${meshN} mesh) — drag to orbit, scroll to zoom`;
    render();
}

function showXSection(b64png) {
    canvas.style.display  = 'none';
    xsecImg.style.display = 'block';
    xsecImg.src = 'data:image/png;base64,' + b64png;
    hud.textContent = 'Cross-section (drag Z slider to scan)';
}

// --- Math helpers ---
function mat4mul(a, b) {
    const out = new Float32Array(16);
    for (let i=0;i<4;i++) for (let j=0;j<4;j++) {
        let s=0; for (let k=0;k<4;k++) s+=a[i*4+k]*b[k*4+j];
        out[i*4+j]=s;
    }
    return out;
}
function perspective(fov, aspect, near, far) {
    const f=1/Math.tan(fov/2), nf=1/(near-far);
    return new Float32Array([f/aspect,0,0,0, 0,f,0,0, 0,0,(far+near)*nf,-1, 0,0,2*far*near*nf,0]);
}
function lookAt(eye, center, up) {
    let fx=center[0]-eye[0], fy=center[1]-eye[1], fz=center[2]-eye[2];
    let fl=Math.sqrt(fx*fx+fy*fy+fz*fz); fx/=fl; fy/=fl; fz/=fl;
    let rx=fy*up[2]-fz*up[1], ry=fz*up[0]-fx*up[2], rz=fx*up[1]-fy*up[0];
    let rl=Math.sqrt(rx*rx+ry*ry+rz*rz); rx/=rl; ry/=rl; rz/=rl;
    let ux=ry*fz-rz*fy, uy=rz*fx-rx*fz, uz=rx*fy-ry*fx;
    return new Float32Array([rx,ux,-fx,0, ry,uy,-fy,0, rz,uz,-fz,0,
        -(rx*eye[0]+ry*eye[1]+rz*eye[2]),-(ux*eye[0]+uy*eye[1]+uz*eye[2]),fx*eye[0]+fy*eye[1]+fz*eye[2],1]);
}

function render() {
    if (!prog || !meshN) return;
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.clearColor(0.04, 0.04, 0.08, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

    // Camera orbit
    const cx = dist * Math.cos(elevation) * Math.sin(azimuth);
    const cy = dist * Math.sin(elevation);
    const cz = dist * Math.cos(elevation) * Math.cos(azimuth);
    const proj = perspective(0.85, canvas.width/canvas.height, 0.01, 20.0);
    const view = lookAt([cx, cy, cz], [0,0,0], [0,1,0]);
    const mvp  = mat4mul(proj, view);

    gl.useProgram(prog);
    gl.uniformMatrix4fv(uMVP, false, mvp);
    gl.uniform1i(uN, meshN);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, heightTex);
    gl.uniform1i(uH_loc, 0);

    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, colorTex);
    gl.uniform1i(uC_loc, 1);

    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ibo);
    gl.drawElements(gl.TRIANGLES, (meshN-1)*(meshN-1)*6, gl.UNSIGNED_INT, 0);
}

resize();
</script>
</body></html>
"""

# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class GenerateWorker(QThread):
    progress  = pyqtSignal(str)
    finished  = pyqtSignal(dict)
    error     = pyqtSignal(str)

    def __init__(self, tx: int, tz: int, cfg: dict):
        super().__init__()
        self.tx, self.tz, self.cfg = tx, tz, cfg

    def run(self):
        try:
            result = run_pipeline(self.tx, self.tz, self.cfg,
                                  progress_cb=lambda s: self.progress.emit(s))
            self.finished.emit(result)
        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


class XSectionWorker(QThread):
    finished = pyqtSignal(bytes)
    error    = pyqtSignal(str)

    def __init__(self, state: dict, z_row: int, band_scale_y: int, cliff_deg_thr: float):
        super().__init__()
        self.state, self.z_row = state, z_row
        self.band_scale_y, self.cliff_deg_thr = band_scale_y, cliff_deg_thr

    def run(self):
        try:
            png = make_xsec_image(self.state, self.z_row,
                                  self.band_scale_y, self.cliff_deg_thr)
            self.finished.emit(png)
        except Exception as e:
            import traceback
            self.error.emit(f"{e}\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class VoxelPreview(QMainWindow):
    def __init__(self, init_tx: int = 56, init_tz: int = 46):
        super().__init__()
        self.setWindowTitle("Vandir Voxel Preview")
        self.resize(1400, 800)

        self._state: Optional[dict] = None
        self._worker: Optional[GenerateWorker] = None
        self._xsec_worker: Optional[XSectionWorker] = None

        # Load config
        with open(CONFIG_PATH) as f:
            self.cfg = json.load(f)

        # ---- layout ----
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.setCentralWidget(splitter)

        # WebGL view
        self.webview = QWebEngineView()
        self.webview.setHtml(_HTML)
        self.webview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        splitter.addWidget(self.webview)

        # Controls panel
        panel = QWidget()
        panel.setFixedWidth(280)
        panel.setStyleSheet("background:#1e1e2e; color:#cdd6f4;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        splitter.addWidget(panel)
        splitter.setSizes([1120, 280])

        # -- Tile selector --
        tile_group = QGroupBox("Tile")
        tile_group.setStyleSheet("QGroupBox { color:#89b4fa; font-weight:bold; }")
        tform = QFormLayout(tile_group)
        self.spin_tx = QSpinBox(); self.spin_tx.setRange(0, 96); self.spin_tx.setValue(init_tx)
        self.spin_tz = QSpinBox(); self.spin_tz.setRange(0, 96); self.spin_tz.setValue(init_tz)
        tform.addRow("Tile X:", self.spin_tx)
        tform.addRow("Tile Z:", self.spin_tz)
        layout.addWidget(tile_group)

        # -- View mode --
        view_group = QGroupBox("View Mode")
        view_group.setStyleSheet("QGroupBox { color:#89b4fa; font-weight:bold; }")
        vform = QFormLayout(view_group)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["3D Terrain", "Cross-Section"])
        self.mode_combo.currentIndexChanged.connect(self._on_mode_change)
        vform.addRow("Mode:", self.mode_combo)
        layout.addWidget(view_group)

        # -- Cross-section Z slider --
        self.xsec_group = QGroupBox("Cross-Section Z Row")
        self.xsec_group.setStyleSheet("QGroupBox { color:#89b4fa; font-weight:bold; }")
        self.xsec_group.setEnabled(False)
        xform = QVBoxLayout(self.xsec_group)
        self.z_slider = QSlider(Qt.Orientation.Horizontal)
        self.z_slider.setRange(0, TILE_SIZE - 1)
        self.z_slider.setValue(256)
        self.z_label  = QLabel("Z = 256")
        self.z_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.z_slider.valueChanged.connect(self._on_z_changed)
        xform.addWidget(self.z_label)
        xform.addWidget(self.z_slider)
        layout.addWidget(self.xsec_group)

        # -- Cliff banding params --
        cliff_group = QGroupBox("Cliff Banding")
        cliff_group.setStyleSheet("QGroupBox { color:#89b4fa; font-weight:bold; }")
        cform = QFormLayout(cliff_group)

        self.band_slider = QSlider(Qt.Orientation.Horizontal)
        self.band_slider.setRange(4, 48)
        cb = self.cfg.get("cliff_banding", {})
        self.band_slider.setValue(int(cb.get("band_scale_y", 12)))
        self.band_label  = QLabel(f"{self.band_slider.value()} blocks/band")
        self.band_slider.valueChanged.connect(
            lambda v: self.band_label.setText(f"{v} blocks/band"))
        cform.addRow("Band scale Y:", self.band_slider)
        cform.addRow("", self.band_label)

        self.deg_slider = QSlider(Qt.Orientation.Horizontal)
        self.deg_slider.setRange(10, 80)
        self.deg_slider.setValue(int(float(cb.get("cliff_deg_thr", 45.0))))
        self.deg_label  = QLabel(f"{self.deg_slider.value()}°")
        self.deg_slider.valueChanged.connect(
            lambda v: self.deg_label.setText(f"{v}°"))
        cform.addRow("Cliff angle thr:", self.deg_slider)
        cform.addRow("", self.deg_label)

        layout.addWidget(cliff_group)

        # -- Buttons --
        btn_style = ("QPushButton { background:#313244; color:#cdd6f4; border:1px solid #45475a;"
                     " border-radius:4px; padding:6px; }"
                     "QPushButton:hover { background:#45475a; }"
                     "QPushButton:disabled { color:#585b70; }")

        self.gen_btn = QPushButton("Generate Tile")
        self.gen_btn.setStyleSheet(btn_style)
        self.gen_btn.clicked.connect(self._generate)
        layout.addWidget(self.gen_btn)

        self.xsec_btn = QPushButton("Render Cross-Section")
        self.xsec_btn.setStyleSheet(btn_style)
        self.xsec_btn.setEnabled(False)
        self.xsec_btn.clicked.connect(self._render_xsec)
        layout.addWidget(self.xsec_btn)

        # -- Status --
        self.status = QLabel("Ready")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#a6adc8; font-size:11px;")
        layout.addWidget(self.status)

        layout.addStretch()

    # ------------------------------------------------------------------
    def _set_busy(self, busy: bool):
        self.gen_btn.setEnabled(not busy)
        self.spin_tx.setEnabled(not busy)
        self.spin_tz.setEnabled(not busy)

    def _generate(self):
        tx, tz = self.spin_tx.value(), self.spin_tz.value()
        self.status.setText(f"Generating tile ({tx}, {tz})…")
        self._set_busy(True)
        self._worker = GenerateWorker(tx, tz, self.cfg)
        self._worker.progress.connect(lambda s: self.status.setText(s))
        self._worker.finished.connect(self._on_generate_done)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_generate_done(self, state: dict):
        self._state = state
        self._set_busy(False)
        self.xsec_btn.setEnabled(True)
        self.xsec_group.setEnabled(True)
        tx, tz = state["tile_x"], state["tile_z"]
        self.status.setText(f"Tile ({tx}, {tz}) ready")
        # Send 3D terrain to WebGL
        js = encode_terrain(state)
        self.webview.page().runJavaScript(js)
        if self.mode_combo.currentIndex() == 1:
            self._render_xsec()

    def _on_error(self, msg: str):
        self._set_busy(False)
        self.status.setText(f"ERROR: {msg[:200]}")
        print("[voxel_preview] ERROR:", msg)

    def _on_mode_change(self, idx: int):
        if idx == 0:
            # Back to 3D
            if self._state:
                js = encode_terrain(self._state)
                self.webview.page().runJavaScript(js)
            self.xsec_group.setEnabled(self._state is not None)
        else:
            # Cross-section
            self.xsec_group.setEnabled(True)
            if self._state:
                self._render_xsec()

    def _on_z_changed(self, value: int):
        self.z_label.setText(f"Z = {value}")
        if self._state and self.mode_combo.currentIndex() == 1:
            self._render_xsec()

    def _render_xsec(self):
        if not self._state:
            return
        z_row         = self.z_slider.value()
        band_scale_y  = self.band_slider.value()
        cliff_deg_thr = float(self.deg_slider.value())
        self.status.setText(f"Rendering cross-section at Z={z_row}…")
        if self._xsec_worker and self._xsec_worker.isRunning():
            self._xsec_worker.terminate()
            self._xsec_worker.wait()
        self._xsec_worker = XSectionWorker(self._state, z_row, band_scale_y, cliff_deg_thr)
        self._xsec_worker.finished.connect(self._on_xsec_done)
        self._xsec_worker.error.connect(self._on_error)
        self._xsec_worker.start()

    def _on_xsec_done(self, png_bytes: bytes):
        b64 = base64.b64encode(png_bytes).decode()
        self.webview.page().runJavaScript(f"showXSection('{b64}')")
        self.status.setText("Cross-section ready")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Vandir Voxel Preview")
    parser.add_argument("--tile-x", type=int, default=56)
    parser.add_argument("--tile-z", type=int, default=46)
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = VoxelPreview(init_tx=args.tile_x, init_tz=args.tile_z)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
