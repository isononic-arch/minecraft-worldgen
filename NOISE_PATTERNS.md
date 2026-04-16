# NOISE_PATTERNS.md — Vandir noise/dither reference

*Canonical reference for noise and dither patterns in this codebase. Before writing any code that uses noise/random/probability for block or ground-cover selection, read this.*

Created S57 after repeated classification errors where "gaussian" was assumed to mean per-pixel gaussian but actually produced coherent fBm simplex blobs.

---

## Pattern catalog

### 1. Salt-and-pepper (per-pixel uniform random)
**What it is:** Every pixel independently rolls against a uniform `rng.random` — no spatial coherence. Visible 1-block-scale variation when ratios are balanced.

**How to generate:**
```python
rng = np.random.default_rng(tile_x * 73856093 ^ tile_y * 19349663 ^ MY_SEED)
noise = rng.random((H, W)).astype(np.float32)
# then threshold into bands
```

**Config form** (in `noise_layers_biome`): `"noise": "white"` or `"noise": "per_pixel"` — added S57.

**Critical rule:** no single block/species >~55% share. If >60%, it reads as solid from above. Aim for 10-40% bands, river-bank-style.

**Canonical reference:** `_apply_river_banks()` at [core/surface_decorator.py:1863-1874](core/surface_decorator.py:1863) — 5 blocks at 10-38% each + 1 trace.

**Use for:** river banks, clearing edge fingers, forest floor mix under canopy, any per-pixel block texturing.

---

### 2. Coherent blobs (multi-octave fBm simplex)
**What it is:** OpenSimplex at multiple octaves, produces smooth fractal-edged patches at the configured wavelength. Large scale = large blobs.

**How to generate:** use `_gen_layer_noise(noise_type="simplex", scale=N, seed=S, ...)` at [core/surface_decorator.py:570-607](core/surface_decorator.py:570).

**Config form:** `"noise": "simplex"` OR `"noise": "gaussian"` — **both fall through to the same fBm branch.** The "gaussian" label is historical and misleading. If you see `"gaussian"` in thresholds.json, it is NOT per-pixel gaussian noise.

**Use for:**
- `meadow_clearing_field` (~256 block wavelength, single octave — blob SHAPE of forest clearings)
- Biome base layers where large organic patches make sense (desert, coastal, arid biomes)
- Any "natural patch" where you want 10-100 block coherent regions

**Don't use for:** forest floor under canopy (see S57 — produces giant podzol/dirt blobs instead of fine texture). Dense canopy should use salt-and-pepper.

---

### 3. Gaussian-filtered random (coherent lobes)
**What it is:** Generate per-pixel uniform random, then apply `scipy.ndimage.gaussian_filter(..., sigma=N)`. Sigma controls lobe size:
- `sigma=1` — near per-pixel (still mostly salt-and-pepper, slight smoothing)
- `sigma=3` — 3-10 block coherent lobes
- `sigma=5+` — larger coherent regions, starts competing with fBm at this scale

**How to generate:**
```python
from scipy.ndimage import gaussian_filter
raw = rng.random((H, W)).astype(np.float32)
noise = gaussian_filter(raw, sigma=3)
# then normalize to [0, 1] if needed
```

**Not a config option** — used inline in code.

**Canonical references:**
- Ecotone decision coin: [core/surface_decorator.py:1474-1480](core/surface_decorator.py:1474) (sigma=3, matching beach pattern)
- Beach decision coin: see §19 S55-1 notes — sigma=1 intentionally

**Use for:** boundary decisions where you want coherent "lobes" of one class/biome interleaving with another at ~5-10 block scale. Not for block selection directly.

---

### 4. Gradient probability ramp + decision coin
**What it is:** The "soften a hard line" pattern. Compute a per-pixel probability field that ramps across a transition zone (0 outside → 1 inside). Then each pixel rolls uniform random vs its local probability. Creates salt-and-pepper fingers interleaving at the boundary instead of a hard edge.

**How to generate:**
```python
# Gradient prob: 1.0 inside → 0.0 outside across a seam of width W
prob = np.clip((inner_edge - distance) / W, 0.0, 1.0).astype(np.float32)
# Per-pixel decision
rng = np.random.default_rng(seed)
decision = rng.random((H, W)).astype(np.float32)
is_inside = decision < prob
```

**Optional plateau clamp** (beach pattern): clip probability to `[0.15, 0.85]` within the seam zone only — guarantees visible salt-and-pepper on BOTH sides of the boundary instead of mostly-inside inner / mostly-outside outer sub-bands.

**Canonical references:**
- Clearing edge gradient: [core/surface_decorator.py:decorate_surface](core/surface_decorator.py) — clearing probability via `clearing_field`, decision seed `0xC1EA5F`.
- Floodplain gap==4 EDT softening: same file — distance_transform_edt + per-pixel decision.
- Beach plateau: §19 S55-1 notes — `place_prob = clip(1 - t, 0.15, 0.85)`.

**Use for:** any region-vs-region boundary that otherwise reads as a hard line.

---

### 5. Cumulative threshold bands (per-pixel species selection)
**What it is:** One uniform per-pixel random, threshold into bands to select among N classes. Each band's width = class's coverage share.

**How to generate:**
```python
noise = rng.random((H, W)).astype(np.float32)
# cumulative thresholds for 5 classes at 40/25/15/12/8%
ground_cover[mask & (noise < 0.40)]                        = "A"  # 40%
ground_cover[mask & (noise >= 0.40) & (noise < 0.65)]     = "B"  # 25%
ground_cover[mask & (noise >= 0.65) & (noise < 0.80)]     = "C"  # 15%
ground_cover[mask & (noise >= 0.80) & (noise < 0.92)]     = "D"  # 12%
ground_cover[mask & (noise >= 0.92)]                      = "E"  # 8%
```

**Canonical reference:** clearing ground cover interior/seam interpolation in `_apply_ground_cover` clearing block.

**Use for:** selecting among multiple species per pixel when you want specific ratios. Can be combined with interpolation between two threshold sets (driven by a secondary gradient) for smooth interior→edge transitions.

---

## Classification rules — when to use which

| Goal | Pattern |
|------|---------|
| Multiple blocks visibly interleaved at 1-block scale, balanced ratios | **Salt-and-pepper (§1)** |
| Organic 10-100 block patches (biome variation, large features) | **fBm simplex (§2)** |
| 3-10 block coherent lobes at a boundary decision | **Gaussian-filtered random (§3)** |
| Soften a region/region boundary (no hard line) | **Gradient prob + decision (§4)** |
| Select among N species per pixel with specific ratios | **Cumulative bands (§5)** |

---

## Anti-patterns — don't do

### ❌ Trusting `"gaussian"` in config as per-pixel
`"gaussian"` in `noise_layers_biome` is **fBm simplex**, not per-pixel gaussian. Use `"white"` / `"per_pixel"` for actual per-pixel uniform.

### ❌ Dominant-with-trace as "salt-and-pepper"
94% block A + 1% each of others is per-pixel random but reads as solid block A from any distance. That's NOT salt-and-pepper, it's "dominant with trace". Real salt-and-pepper needs no single block >~55%.

### ❌ Gaussian filter with sigma ≥ 5 when you want per-pixel
Defeats the per-pixel intent. If you want salt-and-pepper, use sigma ≤ 1 or no filter at all.

### ❌ Solid fill when you claimed salt-and-pepper
Writing `surface_blocks[mask] = "grass_block"` inside a masked region gives solid grass — not a mix. If intent is salt-and-pepper, use one of the patterns above.

### ❌ Hard boundary between two regions without a gradient
Region A fills all of mask A, region B fills all of mask B. Edge reads as a line. Use pattern §4.

---

## Before writing noise/random code — checklist

1. **What's the goal?** One of the 5 patterns above, or a new one?
2. **What block/species ratios?** Any class >55%? If yes, that's dominant-fill, not salt-and-pepper.
3. **What noise type in config?** If using `noise_layers_biome`: `"white"` for per-pixel, `"simplex"`/`"gaussian"` for fBm blobs.
4. **Is there a region-to-region boundary?** If yes, use §4 gradient+decision, not hard masks.
5. **Is there a canonical reference in this doc?** Copy the pattern, don't reinvent.

---

## Adding new patterns

If you invent a new noise/dither pattern in this codebase, add it here with:
- What it is
- How to generate (code snippet)
- Canonical code reference (file:line)
- What it's for / when to use

Doc stays current with the code.
