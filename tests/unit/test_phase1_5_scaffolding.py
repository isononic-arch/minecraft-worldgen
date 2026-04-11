"""
tests/unit/test_phase1_5_scaffolding.py

Phase 1.5 "Lithology wiring scaffolding" pins. Validates that:
  1. build_column_array() flag-OFF is byte-identical to the pre-S46 code path
     (no kwargs, or use_new_geology=False, or use_new_geology=True but
     lithology_tile is None).
  2. build_column_array() with a lithology_tile provided but flag OFF is still
     byte-identical to the no-kwargs call.
  3. build_column_array() with use_new_geology=True and a lithology_tile
     provided raises NotImplementedError with a message referencing
     'Phase 1.5' or '§11' and 'Phase 2' or 'S47' (deferral note).
  4. Param threading: process_tile_columns_v2 accepts the 4 new kwargs and
     generate_columns accepts them as pass-through. Neither is required to
     actually use them in Phase 1.5 — this just pins the signature.
  5. No-caller-enables-flag sentinel: grep production code for
     `use_new_geology=True` — should be zero matches in `core/`, `tools/`,
     `run_pipeline.py`. Only this test file + docs are allowed.

Written S46 (2026-04-11). Phase 2 will remove test 3 and replace it with the
real geology assertion.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import chunk_writer as cw  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _tiny_tile(h: int = 16, w: int = 16) -> dict:
    """Build the smallest valid input set for build_column_array()."""
    rng = np.random.default_rng(seed=1)
    surface_y = np.full((h, w), 70, dtype=np.int16)
    surface_y[::4, ::4] = 120  # a few cliffs
    surface_y[1::4, 1::4] = 55  # a few ocean cells
    surface_blk = np.full((h, w), "grass_block", dtype=object)
    sub_blk = np.full((h, w), "dirt", dtype=object)
    ground_cover = np.full((h, w), "", dtype=object)
    biome_grid = np.full((h, w), "TEMPERATE_DECIDUOUS", dtype=object)
    return dict(
        surface_y=surface_y,
        surface_blk=surface_blk,
        sub_blk=sub_blk,
        ground_cover=ground_cover,
        biome_grid=biome_grid,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_flag_off_no_kwargs_identity():
    """Baseline call with no new kwargs — must work and produce a volume."""
    kw = _tiny_tile()
    vol, pal = cw.build_column_array(**kw)
    assert vol.ndim == 3
    # At least bedrock row should be non-zero
    assert (vol[0] != 0).all()
    assert pal is not None


def test_flag_off_with_new_kwargs_identity():
    """
    Calling with the new kwargs but use_new_geology=False must yield a volume
    byte-identical to the no-kwargs call. Pins "flag OFF = pre-S46 code path".
    """
    kw = _tiny_tile()
    baseline_vol, _ = cw.build_column_array(**kw)

    H, W = kw["surface_y"].shape
    lith = np.zeros((H, W), dtype=np.uint8)
    sed = np.full((H, W), 3, dtype=np.uint8)
    soil = np.full((H, W), 2, dtype=np.uint8)

    # Flag OFF: should be byte-identical even with tiles provided
    guarded_vol, _ = cw.build_column_array(
        **kw,
        lithology_tile=lith,
        sediment_thickness_tile=sed,
        soil_horizon_depth_tile=soil,
        use_new_geology=False,
    )
    assert np.array_equal(baseline_vol, guarded_vol), \
        "flag OFF must not touch the volume"

    # Flag ON but lithology_tile=None: also falls through to legacy path
    guarded2_vol, _ = cw.build_column_array(
        **kw,
        lithology_tile=None,
        sediment_thickness_tile=sed,
        soil_horizon_depth_tile=soil,
        use_new_geology=True,
    )
    assert np.array_equal(baseline_vol, guarded2_vol), \
        "flag ON with lithology_tile=None must fall through"


def test_flag_on_raises_not_implemented():
    """
    Phase 1.5 scaffolding: the flag-ON path is deliberately stubbed. This test
    pins the deferral contract. Phase 2 will delete this test and replace it
    with the real geology assertion.
    """
    kw = _tiny_tile()
    H, W = kw["surface_y"].shape
    lith = np.zeros((H, W), dtype=np.uint8)

    with pytest.raises(NotImplementedError) as exc:
        cw.build_column_array(
            **kw,
            lithology_tile=lith,
            use_new_geology=True,
        )
    msg = str(exc.value).lower()
    # Message must reference the phase plan + deferral so a future reader
    # lands on the right doc.
    assert ("phase 1.5" in msg or "§11" in msg or "section 11" in msg), \
        f"NotImplementedError must cite §11 Phase 1.5: {exc.value!r}"
    assert ("phase 2" in msg or "s47" in msg), \
        f"NotImplementedError must cite Phase 2 / S47 deferral: {exc.value!r}"


def test_param_threading_through_column_generator():
    """
    process_tile_columns_v2 and generate_columns must accept the 4 new
    kwargs as pass-through (no logic required in Phase 1.5).
    """
    import inspect
    from core import column_generator as cg

    for fn_name in ("process_tile_columns_v2", "generate_columns"):
        fn = getattr(cg, fn_name)
        sig = inspect.signature(fn)
        params = set(sig.parameters.keys())
        for kw in (
            "lithology_tile",
            "sediment_thickness_tile",
            "soil_horizon_depth_tile",
            "use_new_geology",
        ):
            assert kw in params, \
                f"{fn_name} missing pass-through kwarg {kw!r}"


def test_no_caller_enables_flag_in_production():
    """
    Sentinel: no production code path may pass use_new_geology=True as a real
    keyword argument at a call site. AST-based so docstring/comment mentions of
    the flag name do not false-positive. Phase 2 will relax this.
    """
    import ast

    repo = PROJECT_ROOT
    banned_hits: list[tuple[str, int]] = []

    for path in repo.rglob("*.py"):
        rel = path.relative_to(repo).as_posix()
        if rel.startswith("tests/"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "use_new_geology":
                    continue
                val = kw.value
                if isinstance(val, ast.Constant) and val.value is True:
                    banned_hits.append((rel, node.lineno))

    assert not banned_hits, (
        f"Production code must not pass use_new_geology=True in Phase 1.5; "
        f"found at: {banned_hits}"
    )
