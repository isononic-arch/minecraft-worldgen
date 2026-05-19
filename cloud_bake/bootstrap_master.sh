#!/usr/bin/env bash
# cloud_bake/bootstrap_master.sh
#
# One-shot setup for a Hetzner Cloud staging box (Ubuntu 24.04).
# Installs Python + deps, clones the repo, prepares for masks upload.
#
# Run as root on a fresh CCX53 (or any CCX) right after first SSH login:
#   curl -fsSL https://raw.githubusercontent.com/isononic-arch/minecraft-worldgen/master/cloud_bake/bootstrap_master.sh | bash
# or
#   wget -qO- https://raw.githubusercontent.com/isononic-arch/minecraft-worldgen/master/cloud_bake/bootstrap_master.sh | bash
#
# After this finishes:
#   1. Upload masks/ from your laptop (see SETUP.md)
#   2. Take a Hetzner Snapshot of this server (Hetzner Console UI → server → Snapshots → Create Snapshot)
#   3. Destroy this staging box (snapshot persists)
#   4. Spin N CCX63 workers FROM that snapshot (Console → Add Server → Image: your snapshot)
#   5. Use plan_render.py locally to generate per-worker commands.

set -euo pipefail

REPO_URL="https://github.com/isononic-arch/minecraft-worldgen.git"
REPO_DIR="/root/minecraft-worldgen"
VENV_DIR="/root/venv"

echo "=========================================================="
echo "Vandir cloud-bake master bootstrap"
echo "=========================================================="

# ── 1. System packages ────────────────────────────────────────
echo "[1/5] apt update + system deps"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y \
    python3 python3-pip python3-venv python3-dev \
    build-essential \
    git tmux rsync htop \
    gdal-bin libgdal-dev libgeos-dev libproj-dev libspatialindex-dev

# ── 2. Repo clone ─────────────────────────────────────────────
echo "[2/5] cloning repo"
if [ ! -d "$REPO_DIR" ]; then
    git clone "$REPO_URL" "$REPO_DIR"
else
    cd "$REPO_DIR" && git pull
fi
cd "$REPO_DIR"
HEAD_COMMIT=$(git rev-parse --short HEAD)
echo "    repo at commit $HEAD_COMMIT"

# ── 3. Python venv + deps ────────────────────────────────────
echo "[3/5] python venv + pip install"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"
pip install --upgrade pip wheel setuptools

# pinned, tested deps — match local development environment
pip install \
    "numpy>=1.26" \
    "scipy>=1.11" \
    "Pillow>=10" \
    "scikit-image>=0.22" \
    "matplotlib>=3.8" \
    "rasterio>=1.3" \
    "nbtlib>=2.0" \
    "opensimplex>=0.4"

# ── 4. Output + masks dirs (empty for now) ───────────────────
echo "[4/5] preparing directories"
mkdir -p "$REPO_DIR/output_s83v17_world"
mkdir -p "$REPO_DIR/masks"

# Auto-source the venv in future shells so the user can just type `python ...`
if ! grep -q "source $VENV_DIR/bin/activate" /root/.bashrc; then
    echo "source $VENV_DIR/bin/activate" >> /root/.bashrc
fi

# ── 5. Self-test ──────────────────────────────────────────────
echo "[5/5] self-test"
python3 -c "
import sys
print(f'  python = {sys.version.split()[0]}')
import numpy, scipy, rasterio, nbtlib, PIL, skimage, opensimplex, matplotlib
print(f'  numpy = {numpy.__version__}')
print(f'  scipy = {scipy.__version__}')
print(f'  rasterio = {rasterio.__version__}')
print(f'  nbtlib = {nbtlib.__version__}')
"

cat <<EOF

==========================================================
DONE. Master bootstrap complete.

NEXT STEPS (from your laptop):

  1. Upload masks/ directory (about 10 GB):
       scp -r /path/to/local/masks/  root@<this-ip>:$REPO_DIR/

  2. (Optional) Upload the spline cache to skip a 10-15 min build:
       scp /path/to/local/masks/_spline_cache.pkl  root@<this-ip>:$REPO_DIR/masks/

  3. Smoke-test ONE tile to validate everything works:
       cd $REPO_DIR && source $VENV_DIR/bin/activate
       python3 run_pipeline.py \\
           --config config/thresholds.json \\
           --masks $REPO_DIR/masks/ \\
           --schem-index schematic_index.json \\
           --output $REPO_DIR/output_smoke/ \\
           --tile-x0 48 --tile-x1 49 --tile-z0 48 --tile-z1 49 \\
           --threads 1

  4. If the smoke tile completed: take a Hetzner Snapshot (UI).
  5. Destroy this server.
  6. Spin N (8 recommended) CCX63 workers from your snapshot.
  7. Use cloud_bake/plan_render.py LOCALLY to generate per-worker commands.

==========================================================
EOF
