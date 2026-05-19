#!/usr/bin/env bash
# cloud_bake/collect_outputs.sh
#
# Pulls render output from N Hetzner worker boxes and merges into a single
# output directory on your laptop. Tiles are independent files so "merge"
# is just file union — no conflicts unless boxes overlapped (which they
# don't with z-stripe partitioning).
#
# Usage:
#   ./cloud_bake/collect_outputs.sh OUTPUT_SUBDIR IP1 IP2 IP3 ...
#
# Example:
#   ./cloud_bake/collect_outputs.sh output_s83v17_world \
#     1.2.3.4 5.6.7.8 9.10.11.12 13.14.15.16
#
# Behavior:
#   - Creates ./OUTPUT_SUBDIR/ locally if missing
#   - For each IP, runs `rsync` to pull /root/minecraft-worldgen/OUTPUT_SUBDIR/*.mca
#   - Reports per-box tile count + total at the end
#   - Resumable — re-running just rsyncs missing files (rsync is delta-aware)

set -euo pipefail

if [ "$#" -lt 2 ]; then
    echo "Usage: $0 OUTPUT_SUBDIR IP1 IP2 ..."
    echo "Example: $0 output_s83v17_world 1.2.3.4 5.6.7.8"
    exit 1
fi

OUTPUT_SUBDIR="$1"
shift
IPS=("$@")

LOCAL_DIR="./$OUTPUT_SUBDIR"
mkdir -p "$LOCAL_DIR"

echo "=========================================================="
echo "Collecting $OUTPUT_SUBDIR from ${#IPS[@]} workers"
echo "Local destination: $LOCAL_DIR"
echo "=========================================================="

per_box_counts=()
for i in "${!IPS[@]}"; do
    ip="${IPS[$i]}"
    box_id=$((i + 1))
    echo ""
    echo "── Box $box_id ($ip) ──────────────────────────────"

    # Check tile count on remote
    remote_count=$(ssh -o StrictHostKeyChecking=accept-new "root@$ip" \
        "ls /root/minecraft-worldgen/$OUTPUT_SUBDIR/*.mca 2>/dev/null | wc -l" || echo "0")
    echo "  remote MCAs: $remote_count"

    # rsync (will resume / skip already-copied files)
    rsync -av --progress \
        "root@$ip:/root/minecraft-worldgen/$OUTPUT_SUBDIR/" \
        "$LOCAL_DIR/" \
        --include="*.mca" --include="datapacks/***" --include="*/" --exclude="*"
    per_box_counts+=("$remote_count")
done

echo ""
echo "=========================================================="
echo "COLLECTION COMPLETE"
echo "=========================================================="
total_local=$(ls "$LOCAL_DIR"/*.mca 2>/dev/null | wc -l)
expected=$((97 * 97))
echo "Per-box MCA counts: ${per_box_counts[*]}"
echo "Local MCA total:    $total_local / $expected expected"

if [ "$total_local" -lt "$expected" ]; then
    missing=$((expected - total_local))
    echo ""
    echo "WARNING: $missing tiles missing. Possible causes:"
    echo "  - Workers still running (re-run this script when done)"
    echo "  - A worker crashed (check tmux logs on the boxes)"
    echo "  - Spot interruption (run the same render command on the box to resume)"
else
    echo ""
    echo "ALL TILES COLLECTED. Ready to ship!"
    echo ""
    echo "To copy into your Minecraft world's region/ folder:"
    echo "  cp $LOCAL_DIR/r.*.mca /path/to/YourWorld/region/"
fi
