#!/usr/bin/env bash
# make_tarball.sh — build the cloud code bundle /tmp/rv_code.tgz DETERMINISTICALLY.
#
# WHY (S96 footgun): the bundle used to be a hand-typed `tar ... --exclude=...`.
# `tar`'s --exclude matches RECURSIVELY across '/', so `--exclude='islands/*.png'`
# silently dropped islands/prerot_dems/*.png -> a box idled an hour on a
# FileNotFoundError. This script uses an INCLUDE-LIST (never an exclude-list) so a
# file is in the bundle iff it is named here -> nothing is ever silently dropped,
# and it ASSERTS the critical bake assets are present before declaring success.
#
# Usage:  bash islands/make_tarball.sh            # -> /tmp/rv_code.tgz
#         TGZ=/tmp/foo.tgz bash islands/make_tarball.sh
set -euo pipefail
ROOT="C:/Users/nicho/minecraft-worldgen"
cd "$ROOT"
TGZ="${TGZ:-/tmp/rv_code.tgz}"

# Explicit include-list: the code + assets the on-box bake+render needs. The
# Hetzner snapshot already carries the full repo at /root/minecraft-worldgen;
# this bundle OVERWRITES the code that changes. DEMs (Downloads/*_16bit.png) are
# scp'd separately by the cloud script, NOT bundled here.
INCLUDE=(
  core
  run_pipeline.py
  derive_masks_from_height.py
  config/thresholds.json
  config/validation_affects.json
  schematic_index.json
  islands/layout.json
  islands/spline_overrides.json
  islands/cache/vandir_seabed_patch.npy
  islands/prerot_dems
)
# all islands/*.py (the render system) — globbed so new helpers are auto-included
for f in islands/*.py; do INCLUDE+=("$f"); done

# Verify every include path exists BEFORE building (fail loud, not on-box).
missing=0
for p in "${INCLUDE[@]}"; do
  [ -e "$p" ] || { echo "  MISSING: $p"; missing=1; }
done
[ "$missing" = 0 ] || { echo "!! make_tarball: missing inputs above; aborting"; exit 1; }

tar czf "$TGZ" "${INCLUDE[@]}"

# Post-build assertions: the assets whose absence cost an hour last time.
# List the bundle ONCE into a var (grep -q would SIGPIPE `tar tzf` under
# pipefail and report a false failure on a large listing).
LISTING="$(tar tzf "$TGZ")"
assert_in(){ printf '%s\n' "$LISTING" | grep -q "$1" || { echo "!! bundle MISSING $1"; exit 1; }; }
assert_in "islands/cache/vandir_seabed_patch.npy"
# prerot DEMs only asserted if any exist locally (rolled-back pre-rotation = none)
if ls islands/prerot_dems/*.png >/dev/null 2>&1; then
  assert_in "islands/prerot_dems/.*_prerot_16bit.png"
fi
assert_in "run_pipeline.py"
assert_in "derive_masks_from_height.py"

echo "built $TGZ ($(du -h "$TGZ" | cut -f1), $(printf '%s\n' "$LISTING" | wc -l) entries)"
