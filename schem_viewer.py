#!/usr/bin/env python3
"""
Vandir Schematic Browser
========================
3D viewer + renamer for .schem / .litematic / .schematic files.
Proper geometry for fences, slabs, stairs, logs, cross-plants, trapdoors etc.
Loads textures from your local Minecraft jar automatically.

Press T to toggle texture mode.

Dependencies:
    py -m pip install PyQt6 PyOpenGL nbtlib Pillow
"""

import sys, os, re, math, random, zipfile, json, tempfile
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter,
    QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLineEdit, QPushButton, QLabel, QFileDialog, QMessageBox,
    QStatusBar, QFrame, QSizePolicy, QToolBar, QComboBox,
    QSlider, QSpinBox, QCheckBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor, QKeySequence, QShortcut

SCHEMATIC_INDEX_PATH = Path(r"C:/Users/nicho/minecraft-worldgen/schematic_index.json")
from PyQt6.QtOpenGLWidgets import QOpenGLWidget
from OpenGL.GL import *
from OpenGL.GLU import *

try:
    import nbtlib
    HAS_NBT = True
except ImportError:
    HAS_NBT = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ─────────────────────────────────────────────────────────────────────────────
# BLOCKSTATE PARSER
# ─────────────────────────────────────────────────────────────────────────────

def parse_blockstate(full_id: str) -> tuple[str, dict]:
    """
    Split 'minecraft:oak_fence[east=true,north=false]'
    into ('minecraft:oak_fence', {'east':'true','north':'false'})
    """
    if "[" not in full_id:
        return full_id, {}
    base, rest = full_id.split("[", 1)
    rest = rest.rstrip("]")
    props = {}
    for part in rest.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            props[k.strip()] = v.strip()
    return base, props


# ─────────────────────────────────────────────────────────────────────────────
# JAR / TEXTURE SYSTEM
# ─────────────────────────────────────────────────────────────────────────────

GRASS_TINT   = (0.47, 0.74, 0.36)
FOLIAGE_TINT = (0.36, 0.62, 0.22)

BLOCK_TEXTURE_MAP = {
    "minecraft:grass_block":        {"top":"grass_block_top","side":"grass_block_side","bottom":"dirt"},
    "minecraft:dirt":               "dirt",
    "minecraft:coarse_dirt":        "coarse_dirt",
    "minecraft:rooted_dirt":        "rooted_dirt",
    "minecraft:podzol":             {"top":"podzol_top","side":"podzol_side","bottom":"dirt"},
    "minecraft:mycelium":           {"top":"mycelium_top","side":"mycelium_side","bottom":"dirt"},
    "minecraft:stone":              "stone",
    "minecraft:cobblestone":        "cobblestone",
    "minecraft:mossy_cobblestone":  "mossy_cobblestone",
    "minecraft:gravel":             "gravel",
    "minecraft:sand":               "sand",
    "minecraft:sandstone":          {"top":"sandstone_top","side":"sandstone","bottom":"sandstone_bottom"},
    "minecraft:red_sandstone":      {"top":"red_sandstone_top","side":"red_sandstone","bottom":"red_sandstone_bottom"},
    "minecraft:smooth_sandstone":   "smooth_sandstone",
    "minecraft:cut_sandstone":      "cut_sandstone",
    "minecraft:mud":                "mud",
    "minecraft:clay":               "clay",
    "minecraft:snow_block":         "snow",
    "minecraft:ice":                "ice",
    "minecraft:packed_ice":         "packed_ice",
    "minecraft:blue_ice":           "blue_ice",
    "minecraft:water":              "water_still",
    "minecraft:tuff":               "tuff",
    "minecraft:calcite":            "calcite",
    "minecraft:diorite":            "diorite",
    "minecraft:polished_diorite":   "polished_diorite",
    "minecraft:andesite":           "andesite",
    "minecraft:polished_andesite":  "polished_andesite",
    "minecraft:granite":            "granite",
    "minecraft:polished_granite":   "polished_granite",
    "minecraft:deepslate":          {"top":"deepslate_top","side":"deepslate","bottom":"deepslate_top"},
    "minecraft:stone_bricks":       "stone_bricks",
    "minecraft:mossy_stone_bricks": "mossy_stone_bricks",
    "minecraft:cracked_stone_bricks":"cracked_stone_bricks",
    "minecraft:cobblestone_wall":   "cobblestone",
    "minecraft:stone_brick_wall":   "stone_bricks",
    # Logs — axis handled by geometry system; texture map gives tex names
    "minecraft:oak_log":            {"top":"oak_log_top","side":"oak_log"},
    "minecraft:spruce_log":         {"top":"spruce_log_top","side":"spruce_log"},
    "minecraft:birch_log":          {"top":"birch_log_top","side":"birch_log"},
    "minecraft:jungle_log":         {"top":"jungle_log_top","side":"jungle_log"},
    "minecraft:dark_oak_log":       {"top":"dark_oak_log_top","side":"dark_oak_log"},
    "minecraft:acacia_log":         {"top":"acacia_log_top","side":"acacia_log"},
    "minecraft:mangrove_log":       {"top":"mangrove_log_top","side":"mangrove_log"},
    "minecraft:cherry_log":         {"top":"cherry_log_top","side":"cherry_log"},
    "minecraft:oak_wood":           "oak_log",
    "minecraft:spruce_wood":        "spruce_log",
    "minecraft:birch_wood":         "birch_log",
    "minecraft:jungle_wood":        "jungle_log",
    "minecraft:dark_oak_wood":      "dark_oak_log",
    "minecraft:acacia_wood":        "acacia_log",
    "minecraft:mangrove_wood":      "mangrove_log",
    "minecraft:stripped_oak_log":   {"top":"stripped_oak_log_top","side":"stripped_oak_log"},
    "minecraft:stripped_spruce_log":{"top":"stripped_spruce_log_top","side":"stripped_spruce_log"},
    "minecraft:stripped_birch_log": {"top":"stripped_birch_log_top","side":"stripped_birch_log"},
    "minecraft:stripped_dark_oak_log":{"top":"stripped_dark_oak_log_top","side":"stripped_dark_oak_log"},
    # Planks
    "minecraft:oak_planks":         "oak_planks",
    "minecraft:spruce_planks":      "spruce_planks",
    "minecraft:birch_planks":       "birch_planks",
    "minecraft:jungle_planks":      "jungle_planks",
    "minecraft:dark_oak_planks":    "dark_oak_planks",
    "minecraft:acacia_planks":      "acacia_planks",
    "minecraft:mangrove_planks":    "mangrove_planks",
    "minecraft:cherry_planks":      "cherry_planks",
    # Leaves (tinted)
    "minecraft:oak_leaves":         ("oak_leaves",       FOLIAGE_TINT),
    "minecraft:spruce_leaves":      ("spruce_leaves",    (0.38,0.60,0.38)),
    "minecraft:birch_leaves":       ("birch_leaves",     FOLIAGE_TINT),
    "minecraft:dark_oak_leaves":    ("dark_oak_leaves",  FOLIAGE_TINT),
    "minecraft:jungle_leaves":      ("jungle_leaves",    FOLIAGE_TINT),
    "minecraft:acacia_leaves":      ("acacia_leaves",    FOLIAGE_TINT),
    "minecraft:mangrove_leaves":    ("mangrove_leaves",  FOLIAGE_TINT),
    "minecraft:cherry_leaves":      "cherry_leaves",
    "minecraft:azalea_leaves":      ("azalea_leaves_flowers", FOLIAGE_TINT),
    "minecraft:flowering_azalea_leaves": ("flowering_azalea_leaves", FOLIAGE_TINT),
    # Fences — use plank texture for rails, log texture for wood fences
    "minecraft:oak_fence":          "oak_planks",
    "minecraft:spruce_fence":       "spruce_planks",
    "minecraft:birch_fence":        "birch_planks",
    "minecraft:jungle_fence":       "jungle_planks",
    "minecraft:dark_oak_fence":     "dark_oak_planks",
    "minecraft:acacia_fence":       "acacia_planks",
    "minecraft:mangrove_fence":     "mangrove_planks",
    "minecraft:cherry_fence":       "cherry_planks",
    "minecraft:nether_brick_fence": "nether_bricks",
    "minecraft:bamboo_fence":       "bamboo_planks",
    # Fence gates
    "minecraft:oak_fence_gate":     "oak_planks",
    "minecraft:spruce_fence_gate":  "spruce_planks",
    "minecraft:birch_fence_gate":   "birch_planks",
    "minecraft:dark_oak_fence_gate":"dark_oak_planks",
    "minecraft:acacia_fence_gate":  "acacia_planks",
    "minecraft:jungle_fence_gate":  "jungle_planks",
    # Stairs
    "minecraft:oak_stairs":         "oak_planks",
    "minecraft:spruce_stairs":      "spruce_planks",
    "minecraft:birch_stairs":       "birch_planks",
    "minecraft:jungle_stairs":      "jungle_planks",
    "minecraft:dark_oak_stairs":    "dark_oak_planks",
    "minecraft:acacia_stairs":      "acacia_planks",
    "minecraft:stone_stairs":       "stone",
    "minecraft:cobblestone_stairs": "cobblestone",
    "minecraft:stone_brick_stairs": "stone_bricks",
    # Slabs
    "minecraft:oak_slab":           "oak_planks",
    "minecraft:spruce_slab":        "spruce_planks",
    "minecraft:birch_slab":         "birch_planks",
    "minecraft:jungle_slab":        "jungle_planks",
    "minecraft:dark_oak_slab":      "dark_oak_planks",
    "minecraft:stone_slab":         "stone",
    "minecraft:cobblestone_slab":   "cobblestone",
    # Trapdoors
    "minecraft:oak_trapdoor":       "oak_trapdoor",
    "minecraft:spruce_trapdoor":    "spruce_trapdoor",
    "minecraft:birch_trapdoor":     "birch_trapdoor",
    "minecraft:dark_oak_trapdoor":  "dark_oak_trapdoor",
    "minecraft:jungle_trapdoor":    "jungle_trapdoor",
    "minecraft:acacia_trapdoor":    "acacia_trapdoor",
    "minecraft:mangrove_trapdoor":  "mangrove_trapdoor",
    "minecraft:cherry_trapdoor":    "cherry_trapdoor",
    # Plants / cross shapes
    "minecraft:grass":              ("grass",            GRASS_TINT),
    "minecraft:tall_grass":         ("tall_grass_bottom",GRASS_TINT),
    "minecraft:fern":               ("fern",             FOLIAGE_TINT),
    "minecraft:large_fern":         ("large_fern_bottom",FOLIAGE_TINT),
    "minecraft:dead_bush":          "dead_bush",
    "minecraft:vine":               ("vine",             FOLIAGE_TINT),
    "minecraft:sugar_cane":         "sugar_cane",
    "minecraft:bamboo":             "bamboo_stalk",
    "minecraft:dandelion":          "dandelion",
    "minecraft:poppy":              "poppy",
    "minecraft:blue_orchid":        "blue_orchid",
    "minecraft:allium":             "allium",
    "minecraft:azure_bluet":        "azure_bluet",
    "minecraft:oxeye_daisy":        "oxeye_daisy",
    "minecraft:cornflower":         "cornflower",
    "minecraft:lily_of_the_valley": "lily_of_the_valley",
    "minecraft:sunflower":          "sunflower_front",
    "minecraft:rose_bush":          "rose_bush_top",
    "minecraft:peony":              "peony_top",
    "minecraft:lily_pad":           ("lily_pad",         GRASS_TINT),
    "minecraft:oak_sapling":        ("oak_sapling",      FOLIAGE_TINT),
    "minecraft:spruce_sapling":     ("spruce_sapling",   FOLIAGE_TINT),
    "minecraft:birch_sapling":      ("birch_sapling",    FOLIAGE_TINT),
    "minecraft:jungle_sapling":     ("jungle_sapling",   FOLIAGE_TINT),
    "minecraft:acacia_sapling":     ("acacia_sapling",   FOLIAGE_TINT),
    "minecraft:dark_oak_sapling":   ("dark_oak_sapling", FOLIAGE_TINT),
    "minecraft:cherry_sapling":     "cherry_sapling",
    "minecraft:mangrove_propagule": ("mangrove_propagule", FOLIAGE_TINT),
    "minecraft:moss_block":         "moss_block",
    "minecraft:moss_carpet":        "moss_block",
    "minecraft:mushroom_stem":      {"top":"mushroom_block_inside","side":"mushroom_stem"},
    "minecraft:brown_mushroom_block":{"top":"brown_mushroom_block","side":"brown_mushroom_block"},
    "minecraft:red_mushroom_block": {"top":"red_mushroom_block","side":"red_mushroom_block"},
    "minecraft:brown_mushroom":     "brown_mushroom",
    "minecraft:red_mushroom":       "red_mushroom",
    # Misc
    "minecraft:ladder":             "ladder",
    "minecraft:torch":              "torch",
    "minecraft:soul_torch":         "soul_torch",
    "minecraft:campfire":           "campfire_fire",
}

BLOCK_COLORS = {
    "minecraft:grass_block":      (0.35,0.65,0.25), "minecraft:dirt":         (0.55,0.38,0.22),
    "minecraft:coarse_dirt":      (0.45,0.30,0.18), "minecraft:podzol":       (0.40,0.25,0.12),
    "minecraft:stone":            (0.55,0.55,0.55), "minecraft:gravel":       (0.60,0.58,0.56),
    "minecraft:sand":             (0.90,0.85,0.60), "minecraft:sandstone":    (0.88,0.82,0.55),
    "minecraft:mud":              (0.35,0.28,0.20), "minecraft:clay":         (0.62,0.65,0.70),
    "minecraft:snow_block":       (0.95,0.97,1.00), "minecraft:ice":          (0.75,0.88,1.00),
    "minecraft:water":            (0.20,0.45,0.80), "minecraft:packed_ice":   (0.80,0.92,1.00),
    "minecraft:oak_log":          (0.50,0.38,0.18), "minecraft:spruce_log":   (0.35,0.25,0.12),
    "minecraft:birch_log":        (0.88,0.88,0.82), "minecraft:dark_oak_log": (0.28,0.20,0.10),
    "minecraft:jungle_log":       (0.45,0.32,0.15), "minecraft:acacia_log":   (0.48,0.35,0.18),
    "minecraft:mangrove_log":     (0.42,0.22,0.18), "minecraft:cherry_log":   (0.72,0.35,0.40),
    "minecraft:oak_leaves":       (0.25,0.58,0.18), "minecraft:spruce_leaves":(0.38,0.60,0.38),
    "minecraft:birch_leaves":     (0.45,0.68,0.28), "minecraft:dark_oak_leaves":(0.18,0.45,0.12),
    "minecraft:jungle_leaves":    (0.22,0.62,0.20), "minecraft:acacia_leaves":(0.30,0.58,0.18),
    "minecraft:mangrove_leaves":  (0.20,0.52,0.22), "minecraft:cherry_leaves":(0.88,0.55,0.70),
    "minecraft:azalea_leaves":    (0.32,0.58,0.22), "minecraft:oak_planks":   (0.72,0.58,0.32),
    "minecraft:spruce_planks":    (0.48,0.35,0.20), "minecraft:birch_planks": (0.82,0.75,0.52),
    "minecraft:jungle_planks":    (0.62,0.44,0.28), "minecraft:dark_oak_planks":(0.28,0.18,0.08),
    "minecraft:fern":             (0.30,0.62,0.22), "minecraft:grass":        (0.40,0.70,0.25),
    "minecraft:sugar_cane":       (0.45,0.72,0.28), "minecraft:vine":         (0.28,0.55,0.20),
    "minecraft:dandelion":        (0.95,0.90,0.20), "minecraft:poppy":        (0.85,0.18,0.15),
    "minecraft:moss_block":       (0.32,0.52,0.22), "minecraft:tuff":         (0.50,0.52,0.48),
    "minecraft:diorite":          (0.80,0.80,0.80), "minecraft:andesite":     (0.50,0.50,0.50),
    "minecraft:granite":          (0.65,0.42,0.32), "minecraft:cobblestone":  (0.52,0.52,0.52),
    # Fences
    "minecraft:oak_fence":          (0.72,0.58,0.32), "minecraft:spruce_fence":       (0.48,0.35,0.20),
    "minecraft:birch_fence":        (0.82,0.75,0.52), "minecraft:jungle_fence":       (0.62,0.44,0.28),
    "minecraft:dark_oak_fence":     (0.28,0.18,0.08), "minecraft:acacia_fence":       (0.65,0.42,0.22),
    "minecraft:mangrove_fence":     (0.48,0.25,0.20), "minecraft:cherry_fence":       (0.80,0.55,0.62),
    "minecraft:nether_brick_fence": (0.28,0.12,0.12),
    # Fence gates
    "minecraft:oak_fence_gate":     (0.72,0.58,0.32), "minecraft:spruce_fence_gate":  (0.48,0.35,0.20),
    "minecraft:birch_fence_gate":   (0.82,0.75,0.52), "minecraft:dark_oak_fence_gate":(0.28,0.18,0.08),
    "minecraft:acacia_fence_gate":  (0.65,0.42,0.22), "minecraft:jungle_fence_gate":  (0.62,0.44,0.28),
    # Trapdoors
    "minecraft:oak_trapdoor":       (0.72,0.58,0.32), "minecraft:spruce_trapdoor":    (0.48,0.35,0.20),
    "minecraft:birch_trapdoor":     (0.82,0.75,0.52), "minecraft:jungle_trapdoor":    (0.62,0.44,0.28),
    "minecraft:dark_oak_trapdoor":  (0.28,0.18,0.08), "minecraft:acacia_trapdoor":    (0.65,0.42,0.22),
    "minecraft:mangrove_trapdoor":  (0.48,0.25,0.20), "minecraft:cherry_trapdoor":    (0.80,0.55,0.62),
    "minecraft:iron_trapdoor":      (0.72,0.72,0.72),
    # Saplings
    "minecraft:oak_sapling":        (0.30,0.60,0.20), "minecraft:spruce_sapling":     (0.22,0.45,0.22),
    "minecraft:birch_sapling":      (0.50,0.72,0.35), "minecraft:jungle_sapling":     (0.25,0.58,0.20),
    "minecraft:acacia_sapling":     (0.35,0.55,0.20), "minecraft:dark_oak_sapling":   (0.18,0.40,0.15),
    "minecraft:cherry_sapling":     (0.85,0.55,0.65), "minecraft:mangrove_propagule": (0.20,0.50,0.22),
    # Stairs
    "minecraft:oak_stairs":         (0.72,0.58,0.32), "minecraft:spruce_stairs":      (0.48,0.35,0.20),
    "minecraft:birch_stairs":       (0.82,0.75,0.52), "minecraft:jungle_stairs":      (0.62,0.44,0.28),
    "minecraft:dark_oak_stairs":    (0.28,0.18,0.08), "minecraft:acacia_stairs":      (0.65,0.42,0.22),
    "minecraft:stone_stairs":       (0.55,0.55,0.55), "minecraft:cobblestone_stairs": (0.52,0.52,0.52),
    "minecraft:stone_brick_stairs": (0.58,0.58,0.58),
    # Slabs
    "minecraft:stone_slab":         (0.55,0.55,0.55), "minecraft:cobblestone_slab":   (0.52,0.52,0.52),
    # Misc
    "minecraft:glass":              (0.75,0.88,0.95), "minecraft:cactus":             (0.25,0.55,0.20),
    "minecraft:carved_pumpkin":     (0.85,0.52,0.15),
}

def get_block_color(block_id: str) -> tuple:
    base, _ = parse_blockstate(block_id)
    c = BLOCK_COLORS.get(base)
    if c: return c
    h = hash(base) & 0xFFFFFF
    r,g,b = ((h>>16)&0xFF)/255.0, ((h>>8)&0xFF)/255.0, (h&0xFF)/255.0
    avg = (r+g+b)/3; bl = 0.4
    return (r*bl+avg*(1-bl), g*bl+avg*(1-bl), b*bl+avg*(1-bl))


def find_minecraft_jar() -> Optional[Path]:
    bases = [
        Path(os.environ.get("APPDATA","")) / ".minecraft",
        Path.home() / "AppData" / "Roaming" / ".minecraft",
        Path.home() / "Library" / "Application Support" / "minecraft",
        Path.home() / ".minecraft",
    ]
    for base in bases:
        ver_dir = base / "versions"
        if not ver_dir.exists(): continue
        found = []
        for d in ver_dir.iterdir():
            if not d.is_dir(): continue
            j = d / f"{d.name}.jar"
            if j.exists(): found.append(j)
        if not found: continue
        releases = [j for j in found if not any(x in j.stem for x in ("pre","rc","snapshot","w","a","b"))]
        pool = releases if releases else found
        pool.sort(key=lambda j: j.stem, reverse=True)
        return pool[0]
    return None


class TextureAtlas:
    def __init__(self):
        self.gl_ids:     dict[str, object] = {}
        self.avg_colors: dict[str, tuple]  = {}
        self.loaded = False

    def load_from_jar(self, jar: Path, status_cb=None) -> int:
        if not HAS_PIL: return 0
        prefix = "assets/minecraft/textures/block/"
        count = 0
        try:
            with zipfile.ZipFile(jar,"r") as zf:
                names = [n for n in zf.namelist() if n.startswith(prefix) and n.endswith(".png")]
                for zname in names:
                    tex_name = Path(zname).stem
                    try:
                        import io
                        raw = zf.read(zname)
                        img = Image.open(io.BytesIO(raw)).convert("RGBA")
                        w,h = img.size
                        if h > w: img = img.crop((0,0,w,w))
                        self.avg_colors[tex_name] = self._avg(img)
                        self.gl_ids[tex_name] = img.tobytes()
                        count += 1
                    except Exception:
                        pass
            self.loaded = True
            if status_cb: status_cb(f"Loaded {count} textures from {jar.name}")
        except Exception as e:
            if status_cb: status_cb(f"Texture load failed: {e}")
        return count

    def upload_to_gl(self):
        uploaded = {}
        for name, data in self.gl_ids.items():
            if isinstance(data, bytes):
                try:
                    px = int((len(data)/4)**0.5)
                    if px*px*4 != len(data):
                        uploaded[name] = None; continue
                    tid = glGenTextures(1)
                    glBindTexture(GL_TEXTURE_2D, tid)
                    glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA,px,px,0,GL_RGBA,GL_UNSIGNED_BYTE,data)
                    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_NEAREST)
                    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_NEAREST)
                    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_S,GL_REPEAT)
                    glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_WRAP_T,GL_REPEAT)
                    uploaded[name] = tid
                except Exception:
                    uploaded[name] = None
            elif not isinstance(data, bytes):
                uploaded[name] = data
        self.gl_ids = uploaded

    def _avg(self, img):
        import numpy as np
        arr = np.array(img)
        mask = arr[:,:,3] > 32
        if not mask.any(): return (0.5,0.5,0.5)
        r = arr[:,:,0][mask].mean()/255
        g = arr[:,:,1][mask].mean()/255
        b = arr[:,:,2][mask].mean()/255
        return (float(r),float(g),float(b))

    def get_tex_id(self, name: str):
        v = self.gl_ids.get(name)
        return v if v is not None and not isinstance(v, bytes) else None

    def get_avg_color(self, name: str, tint=None) -> tuple:
        c = self.avg_colors.get(name, (0.5,0.5,0.5))
        if tint: return (c[0]*tint[0], c[1]*tint[1], c[2]*tint[2])
        return c

    def resolve(self, base_id: str):
        """Returns (tex_top, tex_side, tex_bottom, tint)"""
        entry = BLOCK_TEXTURE_MAP.get(base_id)
        if entry is None: return None,None,None,None
        if isinstance(entry, tuple) and len(entry)==2 and isinstance(entry[1],tuple):
            t,tint = entry; return t,t,t,tint
        if isinstance(entry, str): return entry,entry,entry,None
        if isinstance(entry, dict):
            top  = entry.get("top",  entry.get("side",None))
            side = entry.get("side", top)
            bot  = entry.get("bottom", side)
            return top,side,bot,None
        return None,None,None,None


ATLAS = TextureAtlas()


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRY BUILDER
# Each function returns a list of quads:
#   (face_key, normal, [(x,y,z,u,v), ...], brightness)
# face_key: "top"|"side"|"bottom"|"all" — used to pick texture
# ─────────────────────────────────────────────────────────────────────────────

def _quad(face_key, normal, verts_uvs, brightness=1.0):
    return (face_key, normal, verts_uvs, brightness)

def _box(x0,y0,z0, x1,y1,z1, brightness_scale=1.0):
    """Build 6 quads for an axis-aligned box from (x0,y0,z0) to (x1,y1,z1)."""
    quads = []
    # top
    quads.append(_quad("top",(0,1,0),[
        (x0,y1,z1,0,0),(x1,y1,z1,1,0),(x1,y1,z0,1,1),(x0,y1,z0,0,1)], 1.0*brightness_scale))
    # bottom
    quads.append(_quad("bottom",(0,-1,0),[
        (x0,y0,z0,0,0),(x1,y0,z0,1,0),(x1,y0,z1,1,1),(x0,y0,z1,0,1)], 0.5*brightness_scale))
    # front +z
    quads.append(_quad("side",(0,0,1),[
        (x0,y0,z1,0,1),(x1,y0,z1,1,1),(x1,y1,z1,1,0),(x0,y1,z1,0,0)], 0.8*brightness_scale))
    # back -z
    quads.append(_quad("side",(0,0,-1),[
        (x1,y0,z0,0,1),(x0,y0,z0,1,1),(x0,y1,z0,1,0),(x1,y1,z0,0,0)], 0.8*brightness_scale))
    # left -x
    quads.append(_quad("side",(-1,0,0),[
        (x0,y0,z0,0,1),(x0,y0,z1,1,1),(x0,y1,z1,1,0),(x0,y1,z0,0,0)], 0.7*brightness_scale))
    # right +x
    quads.append(_quad("side",(1,0,0),[
        (x1,y0,z1,0,1),(x1,y0,z0,1,1),(x1,y1,z0,1,0),(x1,y1,z1,0,0)], 0.9*brightness_scale))
    return quads

def _cross(tex_u_scale=1.0):
    """Two intersecting quads for grass/flowers/saplings."""
    s = 0.5; o = 0.3  # offset from center
    quads = []
    # Quad 1: NW-SE diagonal
    quads.append(_quad("all",(0,0,0),[
        (-s,0,-o,0,1),(s,0,o,1,1),(s,1,o,1,0),(-s,1,-o,0,0)], 0.9))
    quads.append(_quad("all",(0,0,0),[
        (s,0,o,1,1),(-s,0,-o,0,1),(-s,1,-o,0,0),(s,1,o,1,0)], 0.9))
    # Quad 2: NE-SW diagonal
    quads.append(_quad("all",(0,0,0),[
        (o,0,-s,0,1),(-o,0,s,1,1),(-o,1,s,1,0),(o,1,-s,0,0)], 0.9))
    quads.append(_quad("all",(0,0,0),[
        (-o,0,s,1,1),(o,0,-s,0,1),(o,1,-s,0,0),(-o,1,s,1,0)], 0.9))
    return quads

def _flat_panel(axis, pos, w0, w1, h0, h1):
    """Thin flat panel (trapdoor/door/carpet). axis='x'|'y'|'z', pos=position along axis."""
    T = 0.05  # thickness half
    if axis == 'y':  # horizontal panel at y=pos
        return _box(w0, pos-T, h0, w1, pos+T, h1)
    elif axis == 'z':  # vertical panel facing Z
        return _box(w0, h0, pos-T, w1, h1, pos+T)
    else:  # vertical panel facing X
        return _box(pos-T, h0, w0, pos+T, h1, w1)

FENCE_POST_W = 0.125   # half-width of post = 2/16
FENCE_RAIL_W = 0.075   # half-width of rail = 1.5/16

def build_geometry(base_id: str, props: dict) -> list:
    """
    Return list of quads for a block given its base ID and blockstate properties.
    Coordinates are in block-local space [-0.5, 0.5] on each axis.
    """

    # ── Full cube (default) ──────────────────────────────────────────────────
    def full_cube():
        return _box(-0.5,-0.5,-0.5, 0.5,0.5,0.5)

    # ── Log / pillar (axis-aware) ────────────────────────────────────────────
    if any(x in base_id for x in ("_log","_wood","_pillar","bamboo_block","hay_block")):
        # Geometry is always a full cube, but we need to swap top/side textures for x/z axis
        quads = full_cube()
        axis = props.get("axis","y")
        if axis in ("x","z"):
            # Remap: what was "top" becomes "side", what was "side" becomes "top"
            remapped = []
            for (fk, n, verts, bri) in quads:
                if fk == "top":    remapped.append(("side",n,verts,bri))
                elif fk == "bottom": remapped.append(("side",n,verts,bri))
                else:              remapped.append(("top",n,verts,bri))
            return remapped
        return quads

    # ── Leaves ──────────────────────────────────────────────────────────────
    if "_leaves" in base_id or base_id in ("minecraft:azalea_leaves","minecraft:flowering_azalea_leaves"):
        return full_cube()

    # ── Fence ───────────────────────────────────────────────────────────────
    if "_fence" in base_id and "gate" not in base_id:
        p = FENCE_POST_W
        r = FENCE_RAIL_W
        quads = _box(-p,-0.5,-p, p,0.5,p)          # center post
        # Rails at y=1/4 and y=-1/16 (upper and lower rails)
        for rail_y_lo, rail_y_hi in [(-0.5+6/16, -0.5+9/16), (-0.5+12/16, -0.5+15/16)]:
            if props.get("north","false") == "true":
                quads += _box(-r, rail_y_lo, -0.5, r, rail_y_hi, -p)
            if props.get("south","false") == "true":
                quads += _box(-r, rail_y_lo,  p,   r, rail_y_hi,  0.5)
            if props.get("west","false") == "true":
                quads += _box(-0.5, rail_y_lo, -r, -p, rail_y_hi,  r)
            if props.get("east","false") == "true":
                quads += _box(p,    rail_y_lo, -r,  0.5, rail_y_hi, r)
        # If no connections, show all rails (standalone post preview)
        if not any(props.get(d,"false")=="true" for d in ["north","south","east","west"]):
            for rail_y_lo, rail_y_hi in [(-0.5+6/16, -0.5+9/16), (-0.5+12/16, -0.5+15/16)]:
                quads += _box(-r, rail_y_lo, -0.5, r, rail_y_hi, 0.5)
        return quads

    # ── Fence gate ──────────────────────────────────────────────────────────
    if "fence_gate" in base_id:
        p = 0.0625  # post half-width
        facing = props.get("facing","south")
        open_  = props.get("open","false") == "true"
        quads  = []
        # Two side posts full height
        if facing in ("north","south"):
            quads += _box(-0.5,-0.5,-p, -0.5+2/16, 0.5, p)
            quads += _box( 0.5-2/16,-0.5,-p, 0.5, 0.5, p)
            if not open_:
                quads += _box(-0.5+2/16, 1/16, -p, 0.5-2/16, 5/16, p)
                quads += _box(-0.5+2/16, 7/16, -p, 0.5-2/16, 11/16, p)
        else:
            quads += _box(-p,-0.5,-0.5, p, 0.5, -0.5+2/16)
            quads += _box(-p,-0.5,  0.5-2/16, p, 0.5, 0.5)
            if not open_:
                quads += _box(-p, 1/16, -0.5+2/16, p, 5/16, 0.5-2/16)
                quads += _box(-p, 7/16, -0.5+2/16, p, 11/16, 0.5-2/16)
        return quads

    # ── Slab ────────────────────────────────────────────────────────────────
    if "_slab" in base_id:
        slab_type = props.get("type","bottom")
        if slab_type == "double":
            return full_cube()
        elif slab_type == "top":
            return _box(-0.5, 0.0, -0.5, 0.5, 0.5, 0.5)
        else:  # bottom
            return _box(-0.5, -0.5, -0.5, 0.5, 0.0, 0.5)

    # ── Stairs ──────────────────────────────────────────────────────────────
    if "_stairs" in base_id:
        half    = props.get("half","bottom")
        facing  = props.get("facing","south")
        y0      = 0.0 if half=="top" else -0.5
        y1      = 0.5 if half=="top" else  0.0
        step_y0 = 0.0 if half=="bottom" else -0.5
        step_y1 = 0.5 if half=="bottom" else  0.0
        quads   = _box(-0.5, y0, -0.5, 0.5, y1, 0.5)  # base slab
        # Step on top
        if   facing == "south": quads += _box(-0.5, step_y0,  0.0, 0.5, step_y1, 0.5)
        elif facing == "north": quads += _box(-0.5, step_y0, -0.5, 0.5, step_y1, 0.0)
        elif facing == "east":  quads += _box( 0.0, step_y0, -0.5, 0.5, step_y1, 0.5)
        elif facing == "west":  quads += _box(-0.5, step_y0, -0.5, 0.0, step_y1, 0.5)
        return quads

    # ── Trapdoor ─────────────────────────────────────────────────────────────
    if "trapdoor" in base_id:
        half  = props.get("half","bottom")
        open_ = props.get("open","false") == "true"
        facing= props.get("facing","south")
        T     = 3/16
        if not open_:
            y = -0.5+T if half=="bottom" else 0.5-T
            return _box(-0.5,-0.5,-0.5, 0.5, y, 0.5) if half=="bottom" else _box(-0.5, 0.5-T,-0.5, 0.5,0.5,0.5)
        else:
            if   facing=="south": return _box(-0.5,-0.5, 0.5-T, 0.5,0.5, 0.5)
            elif facing=="north": return _box(-0.5,-0.5,-0.5,   0.5,0.5,-0.5+T)
            elif facing=="east":  return _box( 0.5-T,-0.5,-0.5, 0.5,0.5, 0.5)
            else:                 return _box(-0.5,-0.5,-0.5,   -0.5+T,0.5,0.5)

    # ── Wall ────────────────────────────────────────────────────────────────
    if "_wall" in base_id:
        p = 0.125
        quads = _box(-p,-0.5,-p, p,0.5,p)  # center post
        h = props.get("up","true")
        for d, (x0,z0,x1,z1) in [
            ("north",(-p,-0.5,  p,-p)),
            ("south",(-p, p,    p, 0.5)),
            ("west", (-0.5,-p, -p, p)),
            ("east", (p,  -p,   0.5,p)),
        ]:
            if props.get(d,"none") != "none":
                quads += _box(x0,-0.5,z0,x1,0.5,z1)
        return quads

    # ── Cross plants (grass, flowers, saplings, etc.) ───────────────────────
    CROSS_BLOCKS = {
        "minecraft:grass","minecraft:fern","minecraft:dead_bush",
        "minecraft:dandelion","minecraft:poppy","minecraft:blue_orchid",
        "minecraft:allium","minecraft:azure_bluet","minecraft:oxeye_daisy",
        "minecraft:cornflower","minecraft:lily_of_the_valley","minecraft:sunflower",
        "minecraft:rose_bush","minecraft:peony","minecraft:tall_grass",
        "minecraft:large_fern","minecraft:sugar_cane",
        "minecraft:brown_mushroom","minecraft:red_mushroom",
    }
    if base_id in CROSS_BLOCKS or "_sapling" in base_id or "_mushroom" == base_id[-9:]:
        return _cross()

    # ── Torch ────────────────────────────────────────────────────────────────
    if "torch" in base_id:
        return _box(-0.05,-0.5,-0.05, 0.05,0.2,0.05)

    # ── Carpet / moss carpet ─────────────────────────────────────────────────
    if "_carpet" in base_id or "carpet" == base_id.split(":")[-1]:
        return _box(-0.5,-0.5,-0.5, 0.5,-0.5+1/16,0.5)

    # ── Ladder ───────────────────────────────────────────────────────────────
    if "ladder" in base_id:
        facing = props.get("facing","south")
        T = 0.05
        if   facing=="south": return _flat_panel('z',  0.5-T, -0.5,0.5, -0.5,0.5)
        elif facing=="north": return _flat_panel('z', -0.5+T, -0.5,0.5, -0.5,0.5)
        elif facing=="east":  return _flat_panel('x',  0.5-T, -0.5,0.5, -0.5,0.5)
        else:                 return _flat_panel('x', -0.5+T, -0.5,0.5, -0.5,0.5)

    # ── Vine ────────────────────────────────────────────────────────────────
    if "vine" in base_id:
        quads = []
        T = 0.02
        if props.get("south","false")=="true":  quads += _flat_panel('z',  0.5-T, -0.5,0.5,-0.5,0.5)
        if props.get("north","false")=="true":  quads += _flat_panel('z', -0.5+T, -0.5,0.5,-0.5,0.5)
        if props.get("east","false")=="true":   quads += _flat_panel('x',  0.5-T, -0.5,0.5,-0.5,0.5)
        if props.get("west","false")=="true":   quads += _flat_panel('x', -0.5+T, -0.5,0.5,-0.5,0.5)
        if props.get("up","false")=="true":     quads += _flat_panel('y',  0.5-T, -0.5,0.5,-0.5,0.5)
        if not quads:
            quads = _flat_panel('z', 0.5-T, -0.5,0.5,-0.5,0.5)
        return quads

    # ── Default: full cube ───────────────────────────────────────────────────
    return full_cube()


# ─────────────────────────────────────────────────────────────────────────────
# GL DRAW HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def gl_draw_quads(quads, t_top, t_side, t_bot, tint, fallback_color, use_tex):
    """Render a list of geometry quads."""
    tex_map = {"top": t_top, "bottom": t_bot, "side": t_side, "all": t_side}

    for (face_key, normal, verts_uvs, brightness) in quads:
        tex_name = tex_map.get(face_key, t_side)
        tex_id   = ATLAS.get_tex_id(tex_name) if (use_tex and tex_name) else None

        if tex_id is not None:
            glEnable(GL_TEXTURE_2D)
            glBindTexture(GL_TEXTURE_2D, tex_id)
            if tint:
                glColor3f(tint[0]*brightness, tint[1]*brightness, tint[2]*brightness)
            else:
                glColor3f(brightness, brightness, brightness)
            glBegin(GL_QUADS)
            for (x,y,z,u,v) in verts_uvs:
                glTexCoord2f(u,v); glVertex3f(x,y,z)
            glEnd()
        else:
            glDisable(GL_TEXTURE_2D)
            if use_tex and tex_name and tex_name in ATLAS.avg_colors:
                c = ATLAS.get_avg_color(tex_name, tint)
            else:
                c = fallback_color
            glColor3f(c[0]*brightness, c[1]*brightness, c[2]*brightness)
            glBegin(GL_QUADS)
            for (x,y,z,u,v) in verts_uvs:
                glVertex3f(x,y,z)
            glEnd()

    glDisable(GL_TEXTURE_2D)


def gl_wireframe_box():
    s = 0.5
    edges = [
        [(-s,-s,-s),(s,-s,-s)],[( s,-s,-s),(s,-s,s)],
        [( s,-s, s),(-s,-s,s)],[(-s,-s, s),(-s,-s,-s)],
        [(-s, s,-s),(s, s,-s)],[( s, s,-s),(s, s,s)],
        [( s, s, s),(-s, s,s)],[(-s, s, s),(-s, s,-s)],
        [(-s,-s,-s),(-s,s,-s)],[( s,-s,-s),(s, s,-s)],
        [( s,-s, s),(s, s, s)],[(-s,-s, s),(-s,s, s)],
    ]
    glBegin(GL_LINES)
    for (a,b) in edges:
        glVertex3f(*a); glVertex3f(*b)
    glEnd()


# ─────────────────────────────────────────────────────────────────────────────
# NBT PARSERS  (now preserve full blockstate string)
# ─────────────────────────────────────────────────────────────────────────────

def _decode_varints(data: bytes) -> list:
    result, i = [], 0
    while i < len(data):
        value, shift = 0, 0
        while True:
            byte = data[i]; i += 1
            value |= (byte & 0x7F) << shift
            if not (byte & 0x80): break
            shift += 7
        result.append(value)
    return result

def _dummy_blocks(path: Path, err: str = "") -> dict:
    rng = random.Random(hash(path.stem))
    color = (rng.uniform(0.3,0.9), rng.uniform(0.3,0.9), rng.uniform(0.3,0.9))
    blocks = [(x,y,z,color,"minecraft:stone",{}) for x in range(4) for y in range(4) for z in range(4)]
    return {"width":4,"height":4,"length":4,"blocks":blocks,"error":err or "nbtlib not installed"}

def parse_schem(path: Path) -> dict:
    if not HAS_NBT: return _dummy_blocks(path)
    try:
        nbt = nbtlib.load(str(path))
        root = nbt.get("Schematic", nbt)
        w,h,l = int(root["Width"]),int(root["Height"]),int(root["Length"])
        # Preserve full blockstate key
        palette = {int(v): str(k) for k,v in root["Palette"].items()}
        indices = _decode_varints(bytes(root["BlockData"]))
        blocks = []
        for i,idx in enumerate(indices):
            y,z,x = i//(w*l),(i%(w*l))//w,i%w
            if y>=h or z>=l or x>=w: continue
            full_id = palette.get(idx,"minecraft:stone")
            base, props = parse_blockstate(full_id)
            if "air" in base: continue
            blocks.append((x,y,z,get_block_color(base),base,props))
        return {"width":w,"height":h,"length":l,"blocks":blocks}
    except Exception as e:
        return _dummy_blocks(path,str(e))

def parse_litematic(path: Path) -> dict:
    if not HAS_NBT: return _dummy_blocks(path)
    try:
        nbt = nbtlib.load(str(path))
        regions = nbt.get("Regions",{})
        if not regions: return _dummy_blocks(path,"no regions")
        region = regions[next(iter(regions))]
        size = region.get("Size",{})
        w,h,l = abs(int(size.get("x",1))),abs(int(size.get("y",1))),abs(int(size.get("z",1)))
        pal_list = region.get("BlockStatePalette",[])
        # Store full blockstate per palette entry
        def entry_to_id(e):
            name = str(e.get("Name","minecraft:air"))
            props_nbt = e.get("Properties",{})
            if props_nbt:
                pstr = ",".join(f"{k}={v}" for k,v in props_nbt.items())
                return f"{name}[{pstr}]"
            return name
        palette = [entry_to_id(e) for e in pal_list]
        block_states = region.get("BlockStates",[])
        n_bits = max(4,math.ceil(math.log2(max(len(palette),2))))
        total = w*h*l
        indices = []
        for lv in block_states:
            lv = int(lv)
            if lv < 0: lv += (1<<64)
            off = 0
            while off+n_bits <= 64:
                indices.append((lv>>off)&((1<<n_bits)-1))
                off += n_bits
            if len(indices) >= total: break
        blocks = []
        for i,idx in enumerate(indices[:total]):
            if idx>=len(palette): continue
            y,z,x = i//(w*l),(i%(w*l))//w,i%w
            full_id = palette[idx]
            base,props = parse_blockstate(full_id)
            if "air" in base: continue
            blocks.append((x,y,z,get_block_color(base),base,props))
        return {"width":w,"height":h,"length":l,"blocks":blocks}
    except Exception as e:
        return _dummy_blocks(path,str(e))

def parse_schematic_classic(path: Path) -> dict:
    if not HAS_NBT: return _dummy_blocks(path)
    try:
        nbt = nbtlib.load(str(path))
        root = nbt.get("Schematic", nbt)
        w,h,l = int(root["Width"]),int(root["Height"]),int(root["Length"])

        # 1.13+ WorldEdit: Sponge-v2 layout inside .schematic
        if "Palette" in root and "BlockData" in root:
            palette = {int(v): str(k) for k,v in root["Palette"].items()}
            indices = _decode_varints(bytes(root["BlockData"]))
            blocks = []
            for i, idx in enumerate(indices):
                y,z,x = i//(w*l),(i%(w*l))//w, i%w
                if y>=h or z>=l or x>=w: continue
                full_id = palette.get(idx, "minecraft:stone")
                base, props = parse_blockstate(full_id)
                if "air" in base: continue
                blocks.append((x,y,z,get_block_color(base),base,props))
            return {"width":w,"height":h,"length":l,"blocks":blocks}

        # Legacy MCEdit: numeric block IDs
        raw_blocks = bytes(root["Blocks"])
        raw_data   = bytes(root.get("Data", b'\x00'*len(raw_blocks)))
        LOG_MAP  = {0:"minecraft:oak_log",   1:"minecraft:spruce_log",
                    2:"minecraft:birch_log",  3:"minecraft:jungle_log"}
        LEAF_MAP = {0:"minecraft:oak_leaves", 1:"minecraft:spruce_leaves",
                    2:"minecraft:birch_leaves",3:"minecraft:jungle_leaves"}
        TALLGRASS_MAP = {0:"minecraft:dead_bush", 1:"minecraft:grass", 2:"minecraft:fern"}
        ID_MAP = {
            1:"minecraft:stone",       2:"minecraft:grass_block",  3:"minecraft:dirt",
            4:"minecraft:cobblestone", 5:"minecraft:oak_planks",   6:"minecraft:oak_sapling",
            8:"minecraft:water",       9:"minecraft:water",        12:"minecraft:sand",
            13:"minecraft:gravel",     17:"minecraft:oak_log",     18:"minecraft:oak_leaves",
            20:"minecraft:glass",      31:"minecraft:grass",       32:"minecraft:dead_bush",
            37:"minecraft:dandelion",  38:"minecraft:poppy",       39:"minecraft:brown_mushroom",
            40:"minecraft:red_mushroom",
            53:"minecraft:oak_stairs",    67:"minecraft:cobblestone_stairs",
            64:"minecraft:oak_door",      71:"minecraft:iron_door",
            78:"minecraft:snow_block",    79:"minecraft:ice",       80:"minecraft:snow_block",
            81:"minecraft:cactus",        82:"minecraft:clay",      83:"minecraft:sugar_cane",
            85:"minecraft:oak_fence",     86:"minecraft:carved_pumpkin",
            96:"minecraft:oak_trapdoor",  106:"minecraft:vine",     107:"minecraft:oak_fence_gate",
            108:"minecraft:brick_stairs", 109:"minecraft:stone_brick_stairs",
            126:"minecraft:oak_slab",
            134:"minecraft:spruce_stairs",  135:"minecraft:birch_stairs",
            136:"minecraft:jungle_stairs",  163:"minecraft:acacia_stairs",
            164:"minecraft:dark_oak_stairs",
            188:"minecraft:spruce_fence",   189:"minecraft:birch_fence",
            190:"minecraft:jungle_fence",   191:"minecraft:dark_oak_fence",
            192:"minecraft:acacia_fence",
            193:"minecraft:spruce_fence_gate", 194:"minecraft:birch_fence_gate",
            195:"minecraft:jungle_fence_gate", 196:"minecraft:dark_oak_fence_gate",
            197:"minecraft:acacia_fence_gate",
        }
        LOG_AXIS = {0:"y", 4:"x", 8:"z"}
        blocks = []
        for i, bid in enumerate(raw_blocks):
            bid = bid & 0xFF
            if bid == 0: continue
            y,z,x = i//(w*l),(i%(w*l))//w, i%w
            dv = raw_data[i]&0x0F if i<len(raw_data) else 0
            props = {}
            if bid == 17:
                block_str = LOG_MAP.get(dv&3, "minecraft:oak_log")
                props = {"axis": LOG_AXIS.get(dv&12, "y")}
            elif bid == 18:
                block_str = LEAF_MAP.get(dv&3, "minecraft:oak_leaves")
            elif bid == 31:
                block_str = TALLGRASS_MAP.get(dv, "minecraft:grass")
            else:
                block_str = ID_MAP.get(bid, "minecraft:stone")
            base, extra = parse_blockstate(block_str)
            props.update(extra)
            blocks.append((x,y,z,get_block_color(base),base,props))
        return {"width":w,"height":h,"length":l,"blocks":blocks}
    except Exception as e:
        return _dummy_blocks(path,str(e))


# ─────────────────────────────────────────────────────────────────────────────
# 3D VIEWER
# ─────────────────────────────────────────────────────────────────────────────

class SchematicViewer(QOpenGLWidget):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.blocks        = []   # (x,y,z,color,base_id,props)
        self.center        = (0,0,0)
        self.yaw           = 35.0
        self.pitch         = -25.0
        self.zoom          = 20.0
        self._last_mouse   = None
        self._mouse_btn    = None
        self._tex_uploaded = False
        self.texture_mode  = True
        self.anchor_y      = 0      # S60: ground plane Y within schematic space
        self.show_ground   = True   # toggle the translucent ground quad
        self.setMinimumSize(400,400)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def load_blocks(self, data: dict):
        self.blocks = data.get("blocks",[])
        w,h,l = data.get("width",1),data.get("height",1),data.get("length",1)
        self.center = (w/2,h/2,l/2)
        self.zoom = max(w,h,l)*1.8
        self._tex_uploaded = False
        self.update()

    def set_anchor_y(self, y: int):
        self.anchor_y = int(y)
        self.update()

    def initializeGL(self):
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE); glCullFace(GL_BACK)
        glClearColor(0.08,0.09,0.11,1.0)
        glTexEnvf(GL_TEXTURE_ENV,GL_TEXTURE_ENV_MODE,GL_MODULATE)
        glDisable(GL_LIGHTING)

    def resizeGL(self, w, h):
        glViewport(0,0,w,max(h,1))
        glMatrixMode(GL_PROJECTION); glLoadIdentity()
        gluPerspective(45.0, w/max(h,1), 0.1, 2000.0)
        glMatrixMode(GL_MODELVIEW)

    def paintGL(self):
        if not self._tex_uploaded:
            if ATLAS.loaded: ATLAS.upload_to_gl()
            self._tex_uploaded = True

        glClear(GL_COLOR_BUFFER_BIT|GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()

        ry,rp = math.radians(self.yaw), math.radians(self.pitch)
        ex = self.zoom*math.cos(rp)*math.sin(ry)
        ey = self.zoom*math.sin(rp)
        ez = self.zoom*math.cos(rp)*math.cos(ry)
        cx,cy,cz = self.center
        gluLookAt(cx+ex,cy+ey,cz+ez, cx,cy,cz, 0,1,0)

        self._draw_grid(cx,cz)
        if self.show_ground:
            self._draw_ground_plane(cx, cz)

        use_tex = self.texture_mode and ATLAS.loaded
        glDisable(GL_TEXTURE_2D)

        for (bx,by,bz,color,base_id,props) in self.blocks:
            glPushMatrix()
            glTranslatef(bx+0.5, by+0.5, bz+0.5)

            t_top,t_side,t_bot,tint = ATLAS.resolve(base_id)
            quads = build_geometry(base_id, props)
            gl_draw_quads(quads, t_top, t_side, t_bot, tint, color, use_tex)

            # Wireframe outline (unit box always, even for non-full-cube shapes)
            glDisable(GL_TEXTURE_2D)
            glColor3f(0,0,0); glLineWidth(0.3)
            gl_wireframe_box()

            glPopMatrix()

    def _draw_grid(self, cx, cz):
        glDisable(GL_TEXTURE_2D)
        glColor4f(0.25,0.28,0.32,0.6); glLineWidth(0.8)
        size = max(20,int(self.zoom*0.8))
        ox,oz = int(cx)-size//2, int(cz)-size//2
        glBegin(GL_LINES)
        for i in range(size+1):
            glVertex3f(ox+i,0,oz); glVertex3f(ox+i,0,oz+size)
            glVertex3f(ox,0,oz+i); glVertex3f(ox+size,0,oz+i)
        glEnd()

    def _draw_ground_plane(self, cx, cz):
        """Translucent green quad at y=anchor_y representing the "ground" level
        where the schematic will sit in-world. Drag anchor_y slider until the
        trunk base / block base of interest meets this plane."""
        glDisable(GL_TEXTURE_2D)
        glEnable(GL_BLEND); glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        size = max(30, int(self.zoom * 1.2))
        ox, oz = int(cx) - size // 2, int(cz) - size // 2
        y = self.anchor_y
        glColor4f(0.35, 0.58, 0.32, 0.35)
        glBegin(GL_QUADS)
        glVertex3f(ox,       y, oz)
        glVertex3f(ox + size, y, oz)
        glVertex3f(ox + size, y, oz + size)
        glVertex3f(ox,       y, oz + size)
        glEnd()
        glColor3f(0.45, 0.75, 0.40); glLineWidth(1.4)
        glBegin(GL_LINE_LOOP)
        glVertex3f(ox,       y, oz)
        glVertex3f(ox + size, y, oz)
        glVertex3f(ox + size, y, oz + size)
        glVertex3f(ox,       y, oz + size)
        glEnd()
        glDisable(GL_BLEND)

    def keyPressEvent(self,e):
        if e.key()==Qt.Key.Key_T:
            self.texture_mode=not self.texture_mode; self.update()

    def mousePressEvent(self,e):
        self._last_mouse=e.position(); self._mouse_btn=e.button()
    def mouseReleaseEvent(self,e):
        self._last_mouse=None; self._mouse_btn=None
    def mouseMoveEvent(self,e):
        if self._last_mouse is None: return
        dx=e.position().x()-self._last_mouse.x()
        dy=e.position().y()-self._last_mouse.y()
        self._last_mouse=e.position()
        if self._mouse_btn==Qt.MouseButton.LeftButton:
            self.yaw+=dx*0.5
            self.pitch=max(-89,min(89,self.pitch+dy*0.5))
        self.update()
    def wheelEvent(self,e):
        self.zoom=max(2,self.zoom-e.angleDelta().y()*0.04); self.update()


# ─────────────────────────────────────────────────────────────────────────────
# FILE LIST ITEM
# ─────────────────────────────────────────────────────────────────────────────

class SchematicItem:
    def __init__(self, path: Path):
        self.path=path; self.stem=path.stem
        self.suffix=path.suffix.lower(); self.display=path.stem
        self._data=None
    def load(self):
        if self._data is None:
            if self.suffix==".schem":           self._data=parse_schem(self.path)
            elif self.suffix==".schematic":     self._data=parse_schematic_classic(self.path)
            elif self.suffix in(".litematic",".nbt"): self._data=parse_litematic(self.path)
            else: self._data=_dummy_blocks(self.path,"unsupported")
        return self._data
    def rename(self, new_stem):
        new_path=self.path.parent/(new_stem+self.path.suffix)
        if new_path.exists() and new_path!=self.path: return False
        try:
            self.path.rename(new_path)
            self.path=new_path; self.stem=self.display=new_stem; self._data=None; return True
        except OSError: return False


# ─────────────────────────────────────────────────────────────────────────────
# STYLE
# ─────────────────────────────────────────────────────────────────────────────

DARK={"bg":"#0E1014","panel":"#161A20","card":"#1E2430","border":"#2A3040",
      "accent":"#3D8B5E","accent2":"#5BAF80","text":"#D8E0EC","muted":"#6A7890","danger":"#C05050"}

STYLE=f"""
QMainWindow,QWidget{{background:{DARK['bg']};color:{DARK['text']};font-family:'Consolas','Courier New',monospace;}}
QSplitter::handle{{background:{DARK['border']};width:2px;height:2px;}}
QListWidget{{background:{DARK['panel']};border:1px solid {DARK['border']};border-radius:4px;outline:none;font-size:12px;padding:2px;}}
QListWidget::item{{padding:6px 10px;border-radius:3px;color:{DARK['text']};}}
QListWidget::item:selected{{background:{DARK['accent']};color:#FFF;}}
QListWidget::item:hover:!selected{{background:{DARK['card']};}}
QLineEdit{{background:{DARK['card']};border:1px solid {DARK['border']};border-radius:4px;padding:6px 10px;color:{DARK['text']};font-size:13px;}}
QLineEdit:focus{{border-color:{DARK['accent']};}}
QPushButton{{background:{DARK['card']};border:1px solid {DARK['border']};border-radius:4px;padding:7px 16px;color:{DARK['text']};font-size:12px;}}
QPushButton:hover{{background:{DARK['accent']};border-color:{DARK['accent2']};color:#FFF;}}
QPushButton:pressed{{background:{DARK['accent2']};}}
QLabel{{color:{DARK['text']};}}
QLabel#muted{{color:{DARK['muted']};font-size:11px;}}
QStatusBar{{background:{DARK['panel']};border-top:1px solid {DARK['border']};color:{DARK['muted']};font-size:11px;}}
QToolBar{{background:{DARK['panel']};border-bottom:1px solid {DARK['border']};spacing:4px;padding:4px;}}
QComboBox{{background:{DARK['card']};border:1px solid {DARK['border']};border-radius:4px;padding:5px 10px;color:{DARK['text']};min-width:140px;}}
QComboBox QAbstractItemView{{background:{DARK['card']};border:1px solid {DARK['border']};color:{DARK['text']};selection-background-color:{DARK['accent']};}}
QFrame#divider{{background:{DARK['border']};max-height:1px;min-height:1px;}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, start_dir=""):
        super().__init__()
        self.setWindowTitle("Vandir — Schematic Browser")
        self.resize(1280,800); self.setStyleSheet(STYLE)
        self.items=[]; self.current=None
        self._build_ui(); self._build_shortcuts(); self._load_textures()
        if start_dir and Path(start_dir).exists():
            self._load_directory(start_dir)

    def _load_textures(self):
        jar=find_minecraft_jar()
        if jar:
            self._status.showMessage(f"Loading textures from {jar.name}…")
            QApplication.processEvents()
            n=ATLAS.load_from_jar(jar, status_cb=self._status.showMessage)
            self._tex_btn.setText(f"🎨 Textures ON  [T]  ({n} loaded)")
        else:
            self._status.showMessage("Minecraft jar not found — flat colors only.")
            self._tex_btn.setText("🎨 Textures OFF  [T]  (jar not found)")

    def _build_ui(self):
        tb=QToolBar(); tb.setMovable(False); self.addToolBar(tb)
        btn=QPushButton("📁  Open Directory"); btn.clicked.connect(self._browse_directory); tb.addWidget(btn)
        tb.addSeparator()
        self._search=QLineEdit(); self._search.setPlaceholderText("Filter schematics…")
        self._search.setFixedWidth(240); self._search.textChanged.connect(self._apply_filter); tb.addWidget(self._search)
        tb.addSeparator()
        self._filter_type=QComboBox()
        self._filter_type.addItems(["All types",".schem only",".litematic only",".schematic only"])
        self._filter_type.currentIndexChanged.connect(self._apply_filter); tb.addWidget(self._filter_type)
        tb.addSeparator()
        self._tex_btn=QPushButton("🎨 Textures  [T]")
        self._tex_btn.clicked.connect(self._toggle_texture); tb.addWidget(self._tex_btn)
        self._lbl_count=QLabel("  0 files"); self._lbl_count.setObjectName("muted"); tb.addWidget(self._lbl_count)

        splitter=QSplitter(Qt.Orientation.Horizontal); self.setCentralWidget(splitter)
        left=QWidget(); llay=QVBoxLayout(left); llay.setContentsMargins(8,8,4,8); llay.setSpacing(6)
        lbl=QLabel("SCHEMATICS"); lbl.setObjectName("muted"); lbl.setFont(QFont("Consolas",9)); llay.addWidget(lbl)
        self._list=QListWidget(); self._list.currentItemChanged.connect(self._on_select); llay.addWidget(self._list)
        splitter.addWidget(left)

        right=QWidget(); rlay=QVBoxLayout(right); rlay.setContentsMargins(4,8,8,8); rlay.setSpacing(0)
        self._viewer=SchematicViewer(); rlay.addWidget(self._viewer,1)
        div=QFrame(); div.setObjectName("divider"); rlay.addWidget(div)
        detail=QWidget(); detail.setStyleSheet(f"background:{DARK['panel']};")
        dlay=QVBoxLayout(detail); dlay.setContentsMargins(16,12,16,12); dlay.setSpacing(8)
        self._lbl_file=QLabel("No file selected"); self._lbl_file.setObjectName("muted")
        self._lbl_file.setFont(QFont("Consolas",10)); dlay.addWidget(self._lbl_file)
        row=QHBoxLayout(); row.setSpacing(6)
        lbl_r=QLabel("Rename:"); lbl_r.setFixedWidth(60); row.addWidget(lbl_r)
        self._rename_edit=QLineEdit(); self._rename_edit.setPlaceholderText("Enter new name…")
        self._rename_edit.returnPressed.connect(self._do_rename); row.addWidget(self._rename_edit)
        btn_r=QPushButton("Apply  ↵"); btn_r.setFixedWidth(90); btn_r.clicked.connect(self._do_rename); row.addWidget(btn_r)
        dlay.addLayout(row)
        self._lbl_info=QLabel(""); self._lbl_info.setObjectName("muted"); dlay.addWidget(self._lbl_info)

        # S60: anchor_y editor row — slider + spinbox + save & approve
        anchor_row = QHBoxLayout(); anchor_row.setSpacing(6)
        lbl_a = QLabel("Ground Y:"); lbl_a.setFixedWidth(60); anchor_row.addWidget(lbl_a)
        self._anchor_slider = QSlider(Qt.Orientation.Horizontal)
        self._anchor_slider.setRange(-20, 40)
        self._anchor_slider.setValue(0)
        self._anchor_slider.valueChanged.connect(self._on_anchor_changed)
        anchor_row.addWidget(self._anchor_slider, 1)
        self._anchor_spin = QSpinBox()
        self._anchor_spin.setRange(-20, 40); self._anchor_spin.setValue(0)
        self._anchor_spin.setFixedWidth(64)
        self._anchor_spin.valueChanged.connect(self._on_anchor_spin)
        anchor_row.addWidget(self._anchor_spin)
        self._ground_chk = QCheckBox("Show ground"); self._ground_chk.setChecked(True)
        self._ground_chk.stateChanged.connect(self._on_ground_toggle)
        anchor_row.addWidget(self._ground_chk)
        self._btn_save = QPushButton("Save && Approve")
        self._btn_save.setFixedWidth(140)
        self._btn_save.clicked.connect(self._save_anchor)
        anchor_row.addWidget(self._btn_save)
        dlay.addLayout(anchor_row)
        self._lbl_anchor_info = QLabel("(no schematic selected)")
        self._lbl_anchor_info.setObjectName("muted")
        dlay.addWidget(self._lbl_anchor_info)

        rlay.addWidget(detail); splitter.addWidget(right); splitter.setSizes([320,960])
        self._status=QStatusBar(); self.setStatusBar(self._status)
        self._status.showMessage("Open a directory to begin.")
        self._schem_index = self._load_schem_index()

    def _build_shortcuts(self):
        QShortcut(QKeySequence("Return"), self, self._do_rename)
        QShortcut(QKeySequence("Ctrl+O"), self, self._browse_directory)
        QShortcut(QKeySequence("T"),      self, self._toggle_texture)

    def _toggle_texture(self):
        self._viewer.texture_mode=not self._viewer.texture_mode
        state="ON" if self._viewer.texture_mode else "OFF"
        n=len([v for v in ATLAS.gl_ids.values() if v is not None and not isinstance(v,bytes)])
        self._tex_btn.setText(f"🎨 Textures {state}  [T]  ({n} loaded)")
        self._viewer.update()

    def _browse_directory(self):
        d=QFileDialog.getExistingDirectory(self,"Open Schematic Directory")
        if d: self._load_directory(d)

    def _load_directory(self, path):
        p=Path(path); exts={".schem",".litematic",".nbt",".schematic"}
        files=[f for f in p.rglob("*") if f.suffix.lower() in exts]
        if not files: files=[f for f in p.glob("*") if f.suffix.lower() in exts]
        # S60: filter to only schematics referenced in schematic_index.json, so
        # users can't accidentally open huge un-indexed files ("all trees package"
        # crash). Fallback: if no index or no matches, show everything.
        allowed = self._indexed_filenames()
        if allowed:
            indexed = [f for f in files if f.name.lower() in allowed]
            skipped = len(files) - len(indexed)
            if indexed:
                files = indexed
                self._status.showMessage(
                    f"Showing {len(files)} indexed / {len(files)+skipped} on-disk  (skipped {skipped} un-indexed)"
                )
        files=sorted(files, key=lambda f: f.name.lower())
        self.items=[SchematicItem(f) for f in files]
        if not files:
            all_f=list(p.rglob("*")); sample=[f.name for f in all_f[:6]]
            self._status.showMessage(f"No schematic files found. Found {len(all_f)} total: {sample}")
        else:
            if not allowed:
                self._status.showMessage(f"Loaded {len(self.items)} files (schematic_index.json not loaded; showing all)")
        self.setWindowTitle(f"Vandir — {p.name}  ({len(self.items)} files)")
        self._apply_filter()

    def _indexed_filenames(self) -> set:
        """Return the set of lowercased filenames referenced in schematic_index.json.
        Used to filter `_load_directory` to the indexed subset only."""
        if not getattr(self, "_schem_index", None):
            return set()
        names = set()
        for biome, entries in self._schem_index.items():
            if not isinstance(entries, list):
                continue
            for e in entries:
                p = e.get("path", "")
                if p:
                    names.add(Path(p).name.lower())
        return names

    def _apply_filter(self):
        text=self._search.text().lower(); tf=self._filter_type.currentIndex()
        self._list.clear(); shown=0
        for item in self.items:
            if tf==1 and item.suffix!=".schem": continue
            if tf==2 and item.suffix!=".litematic": continue
            if tf==3 and item.suffix!=".schematic": continue
            if text and text not in item.display.lower(): continue
            li=QListWidgetItem(f"{item.display}  {item.suffix}")
            li.setData(Qt.ItemDataRole.UserRole,item)
            if   item.suffix==".schem":     li.setForeground(QColor(DARK["accent2"]))
            elif item.suffix==".litematic": li.setForeground(QColor("#7AB8E8"))
            elif item.suffix==".schematic": li.setForeground(QColor("#E8B87A"))
            self._list.addItem(li); shown+=1
        self._lbl_count.setText(f"  {shown} / {len(self.items)} files")

    def _on_select(self, current, previous):
        if current is None: return
        item=current.data(Qt.ItemDataRole.UserRole); self.current=item
        self._lbl_file.setText(f"{item.path.parent.name} / {item.path.name}")
        self._rename_edit.setText(item.stem); self._rename_edit.selectAll()
        self._status.showMessage(f"Loading {item.path.name}…"); QApplication.processEvents()
        data=item.load(); self._viewer.load_blocks(data)
        n=len(data.get("blocks",[])); w,h,l=data.get("width",0),data.get("height",0),data.get("length",0)
        err=data.get("error","")
        info=f"{w}W × {h}H × {l}L  |  {n:,} visible blocks"
        if err: info+=f"  ⚠ {err}"
        self._lbl_info.setText(info); self._status.showMessage(f"Loaded {item.path.name}  —  {info}")
        # S60: load anchor_y from schematic_index for this file
        matches = self._find_index_entries(item.path)
        if matches:
            # Use the first entry's anchor_y (all matches for the same path
            # should have identical anchor_y). Block signals to avoid feedback.
            a_y = int(matches[0][1].get("anchor_y", 0))
            reviewed = any(m[1].get("anchor_review", False) for m in matches)
            self._anchor_slider.blockSignals(True); self._anchor_spin.blockSignals(True)
            self._anchor_slider.setValue(a_y); self._anchor_spin.setValue(a_y)
            self._anchor_slider.blockSignals(False); self._anchor_spin.blockSignals(False)
            self._viewer.set_anchor_y(a_y)
            biomes = sorted({b for b, _ in matches})
            review_str = "  ⚠ anchor_review=true" if reviewed else ""
            self._lbl_anchor_info.setText(
                f"{len(matches)} index entry/entries  biomes: {', '.join(biomes)}{review_str}"
            )
        else:
            self._lbl_anchor_info.setText("(no schematic_index entry for this file)")
            self._anchor_slider.blockSignals(True); self._anchor_spin.blockSignals(True)
            self._anchor_slider.setValue(0); self._anchor_spin.setValue(0)
            self._anchor_slider.blockSignals(False); self._anchor_spin.blockSignals(False)
            self._viewer.set_anchor_y(0)

    def _do_rename(self):
        if self.current is None: return
        new_stem=self._rename_edit.text().strip()
        if not new_stem or new_stem==self.current.stem: self._next_item(); return
        if not re.match(r'^[\w\-. ]+$',new_stem):
            QMessageBox.warning(self,"Invalid name","Use letters, numbers, underscores, hyphens."); return
        old=self.current.stem
        if self.current.rename(new_stem):
            li=self._list.currentItem()
            if li: li.setText(f"{new_stem}  {self.current.suffix}"); li.setData(Qt.ItemDataRole.UserRole,self.current)
            self._lbl_file.setText(f"{self.current.path.parent.name} / {self.current.path.name}")
            self._status.showMessage(f"✓ Renamed: {old} → {new_stem}"); self._next_item()
        else:
            QMessageBox.warning(self,"Rename failed","Could not rename — target may already exist.")

    def _next_item(self):
        row=self._list.currentRow()
        if row<self._list.count()-1: self._list.setCurrentRow(row+1)
        self._rename_edit.selectAll(); self._rename_edit.setFocus()

    # ── S60: anchor_y editor (ground plane + save & approve) ──────────────

    def _load_schem_index(self):
        """Load schematic_index.json into memory. Called once at startup."""
        try:
            with open(SCHEMATIC_INDEX_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self._status.showMessage(f"schematic_index.json load failed: {e}")
            return {}

    def _find_index_entries(self, path: Path) -> list:
        """Return list of (biome_key, entry_dict) matching `path` by absolute
        filesystem path (case-insensitive) or by filename stem fallback."""
        if not self._schem_index:
            return []
        target_abs = str(path.resolve()).lower()
        target_name = path.name.lower()
        matches = []
        for biome, entries in self._schem_index.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                p = entry.get("path", "")
                if not p:
                    continue
                if p.lower() == target_abs or Path(p).name.lower() == target_name:
                    matches.append((biome, entry))
        return matches

    def _on_anchor_changed(self, val: int):
        self._anchor_spin.blockSignals(True)
        self._anchor_spin.setValue(val)
        self._anchor_spin.blockSignals(False)
        self._viewer.set_anchor_y(val)

    def _on_anchor_spin(self, val: int):
        self._anchor_slider.blockSignals(True)
        self._anchor_slider.setValue(val)
        self._anchor_slider.blockSignals(False)
        self._viewer.set_anchor_y(val)

    def _on_ground_toggle(self, state: int):
        self._viewer.show_ground = (state == Qt.CheckState.Checked.value)
        self._viewer.update()

    def _save_anchor(self):
        """Write current slider value as anchor_y into all matching index
        entries, flip anchor_review=false, then atomically save the JSON."""
        if self.current is None:
            QMessageBox.warning(self, "No schematic", "Select a schematic first.")
            return
        if not self._schem_index:
            QMessageBox.warning(self, "No index", "schematic_index.json not loaded.")
            return
        matches = self._find_index_entries(self.current.path)
        if not matches:
            QMessageBox.warning(
                self, "No index entry",
                f"No schematic_index.json entry matches {self.current.path.name}.\n"
                "Cannot save anchor_y.",
            )
            return
        new_y = int(self._anchor_spin.value())
        for biome, entry in matches:
            entry["anchor_y"] = new_y
            entry["anchor_review"] = False
        try:
            self._atomic_save_index()
        except Exception as e:
            QMessageBox.critical(self, "Save failed", f"Could not write schematic_index.json:\n{e}")
            return
        self._status.showMessage(
            f"Saved anchor_y={new_y} to {len(matches)} entry/entries for {self.current.path.name}"
        )
        biomes = sorted({b for b, _ in matches})
        self._lbl_anchor_info.setText(
            f"Saved: anchor_y={new_y} across biomes {', '.join(biomes)} (anchor_review=false)"
        )

    def _atomic_save_index(self):
        """Write schematic_index.json via temp+rename to avoid partial writes."""
        target = SCHEMATIC_INDEX_PATH
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="schem_index_", suffix=".json", dir=str(target.parent),
        )
        os.close(tmp_fd)
        tmp = Path(tmp_path)
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._schem_index, f, indent=2, ensure_ascii=False)
            os.replace(tmp, target)
        except Exception:
            try: tmp.unlink()
            except Exception: pass
            raise


def main():
    app=QApplication(sys.argv); app.setApplicationName("Vandir Schematic Browser")
    start_dir=sys.argv[1] if len(sys.argv)>1 else ""
    win=MainWindow(start_dir); win.show(); sys.exit(app.exec())

if __name__=="__main__":
    main()
