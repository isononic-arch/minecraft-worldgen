"""
noise_fields.py — Core noise field initializer
/core/noise_fields.py

Initializes all 6 deterministic OpenSimplex2 noise generators from thresholds.json.
Must be called once at pipeline startup and passed into tile workers.

Seeds (LOCKED):
    biome_patch:        42001
    decoration_density: 42002
    slope_mix:          42003
    snow_line:          42004
    dune_a:             42005
    dune_b:             42006
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union


def load_noise_generators(config_path: Union[str, Path]) -> dict:
    """
    Load thresholds.json and return a dict of 6 OpenSimplex noise generators.

    Returns:
        {
            "biome_patch":        OpenSimplex instance,
            "decoration_density": OpenSimplex instance,
            "slope_mix":          OpenSimplex instance,
            "snow_line":          OpenSimplex instance,
            "dune_a":             OpenSimplex instance,
            "dune_b":             OpenSimplex instance,
        }
    """
    with open(config_path) as f:
        cfg = json.load(f)

    seeds = cfg.get("noise_seeds", {
        "biome_patch":        42001,
        "decoration_density": 42002,
        "slope_mix":          42003,
        "snow_line":          42004,
        "dune_a":             42005,
        "dune_b":             42006,
    })

    try:
        from opensimplex import OpenSimplex
    except ImportError:
        raise ImportError(
            "opensimplex package required: pip install opensimplex"
        )

    return {
        "biome_patch":        OpenSimplex(seed=seeds["biome_patch"]),
        "decoration_density": OpenSimplex(seed=seeds["decoration_density"]),
        "slope_mix":          OpenSimplex(seed=seeds["slope_mix"]),
        "snow_line":          OpenSimplex(seed=seeds["snow_line"]),
        "dune_a":             OpenSimplex(seed=seeds["dune_a"]),
        "dune_b":             OpenSimplex(seed=seeds["dune_b"]),
    }


def fbm_noise(gen, x: float, y: float, octaves: int = 4,
              persistence: float = 0.5, lacunarity: float = 2.0) -> float:
    """
    Fractional Brownian Motion noise using an OpenSimplex generator.
    Returns a value in [0, 1].
    """
    value = 0.0
    amplitude = 1.0
    frequency = 1.0
    max_val = 0.0

    for _ in range(octaves):
        value    += gen.noise2(x * frequency, y * frequency) * amplitude
        max_val  += amplitude
        amplitude *= persistence
        frequency *= lacunarity

    # Normalize from [-max_val, max_val] to [0, 1]
    return (value / max_val + 1.0) / 2.0


# ---------------------------------------------------------------------------
# SMOKE TEST
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys, json, tempfile, os

    print("noise_fields.py — smoke test")

    # Write a minimal thresholds.json
    cfg = {
        "noise_seeds": {
            "biome_patch":        42001,
            "decoration_density": 42002,
            "slope_mix":          42003,
            "snow_line":          42004,
            "dune_a":             42005,
            "dune_b":             42006,
        }
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                     delete=False) as f:
        json.dump(cfg, f)
        tmp = f.name

    try:
        noise = load_noise_generators(tmp)
    except ImportError as e:
        print(f"  SKIP: {e}")
        sys.exit(0)
    finally:
        os.unlink(tmp)

    assert set(noise.keys()) == {
        "biome_patch","decoration_density","slope_mix",
        "snow_line","dune_a","dune_b"
    }, "Wrong keys"

    # Test fbm_noise returns [0,1]
    for name, gen in noise.items():
        v = fbm_noise(gen, 12.3, 45.6, octaves=4)
        assert 0.0 <= v <= 1.0, f"{name}: fbm out of range: {v}"

    # Test determinism
    from opensimplex import OpenSimplex
    g1 = OpenSimplex(seed=42001)
    g2 = OpenSimplex(seed=42001)
    assert fbm_noise(g1, 1.0, 2.0) == fbm_noise(g2, 1.0, 2.0), "Not deterministic"

    print(f"  6 generators loaded: {list(noise.keys())}")
    print(f"  fbm_noise sample:    {fbm_noise(noise['biome_patch'], 10.0, 20.0):.4f}")
    print(f"  determinism:         OK")
    print("PASS")
    sys.exit(0)
