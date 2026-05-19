"""
cloud_bake/plan_render.py — partition the 97x97 world into N stripes and
print the exact `python run_pipeline.py ...` command to run on each
Hetzner worker box.

Run LOCALLY (not on the workers):

    python cloud_bake/plan_render.py --boxes 8 --ips 1.2.3.4 5.6.7.8 ...

You can omit --ips; then it just prints the per-box tile-range commands
generic (no SSH wrapper). With --ips, it prints `ssh root@IP "..."`
commands ready to paste.

Outputs:
  - The per-box render command (tmux-detached so it survives disconnect)
  - The expected wall time + cost
  - The collect-output command you'll run when everything's done
"""
from __future__ import annotations
import argparse
import sys

WORLD_TILES = 97
TILE_MIN_PER = 7.0          # average per-tile minute estimate on cloud CPU
WORKERS_PER_BOX_DEFAULT = 48  # CCX63 has 48 vCPU
HOURLY_RATE_CCX63 = 0.57    # USD/hour


def partition_z(boxes: int) -> list[tuple[int, int]]:
    """Split z = [0, WORLD_TILES) into `boxes` contiguous stripes.
    Returns list of (z_start, z_end) per box. End is exclusive (matches
    run_pipeline.py's --tile-z1 semantics).
    The last stripe absorbs any remainder."""
    base = WORLD_TILES // boxes
    extra = WORLD_TILES % boxes
    ranges = []
    cursor = 0
    for i in range(boxes):
        size = base + (1 if i < extra else 0)
        ranges.append((cursor, cursor + size))
        cursor += size
    assert cursor == WORLD_TILES
    return ranges


def render_command(z0: int, z1: int, threads: int,
                   output_subdir: str = "output_s83v17_world") -> str:
    return (
        "python3 run_pipeline.py "
        "--config config/thresholds.json "
        "--masks /root/minecraft-worldgen/masks/ "
        "--schem-index schematic_index.json "
        f"--output /root/minecraft-worldgen/{output_subdir}/ "
        f"--tile-x0 0 --tile-x1 {WORLD_TILES} "
        f"--tile-z0 {z0} --tile-z1 {z1} "
        f"--threads {threads}"
    )


def tmux_wrapper(box_id: int, cmd: str) -> str:
    """Wrap render command in a detached tmux session so SSH disconnect
    doesn't kill it."""
    safe_cmd = cmd.replace('"', '\\"')
    return (
        f"tmux new-session -d -s render-box{box_id} "
        f"'cd /root/minecraft-worldgen && source /root/venv/bin/activate && "
        f"{cmd} > /root/render-box{box_id}.log 2>&1'"
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--boxes", type=int, default=8,
                   help="Number of Hetzner worker boxes (default 8)")
    p.add_argument("--threads", type=int, default=WORKERS_PER_BOX_DEFAULT,
                   help=f"Workers per box (default {WORKERS_PER_BOX_DEFAULT})")
    p.add_argument("--ips", nargs="*", default=None,
                   help="SSH IPs of the N worker boxes (in z-stripe order). "
                        "If given, output is ssh-wrapped ready-to-paste.")
    p.add_argument("--output-subdir", default="output_s83v17_world",
                   help="Output subdir under /root/minecraft-worldgen/")
    args = p.parse_args()

    if args.ips and len(args.ips) != args.boxes:
        print(f"ERROR: {len(args.ips)} IPs given but --boxes={args.boxes}",
              file=sys.stderr)
        return 2

    ranges = partition_z(args.boxes)
    total_tile_minutes = WORLD_TILES * WORLD_TILES * TILE_MIN_PER
    workers_total = args.boxes * args.threads
    wall_minutes = total_tile_minutes / workers_total
    wall_hours = wall_minutes / 60.0
    # Hetzner rounds up to nearest hour; minimum 1 hour billed
    billed_hours = max(1.0, round(wall_hours + 0.5, 0))
    cost_total = billed_hours * HOURLY_RATE_CCX63 * args.boxes

    print("=" * 72)
    print(f"VANDIR CLOUD BAKE PLAN")
    print("=" * 72)
    print(f"Worker boxes:        {args.boxes}x CCX63 (48 vCPU / 192 GB)")
    print(f"Workers per box:     {args.threads}")
    print(f"Total workers:       {workers_total}")
    print(f"Tiles to render:     {WORLD_TILES * WORLD_TILES} (97 x 97)")
    print(f"Expected wall time:  {wall_hours:.1f} hours")
    print(f"Hetzner billing:     ~{billed_hours:.0f} hours x ${HOURLY_RATE_CCX63}/hr x {args.boxes} boxes")
    print(f"Estimated cost:      ~${cost_total:.2f}")
    print(f"Partition strategy:  z-stripes ({WORLD_TILES}/{args.boxes} rows each)")
    print()
    print("PER-BOX COMMANDS")
    print("-" * 72)
    for i, (z0, z1) in enumerate(ranges):
        n_tiles = (z1 - z0) * WORLD_TILES
        print(f"\n# Box {i+1}: z=[{z0}, {z1})  ({n_tiles} tiles)")
        cmd = render_command(z0, z1, args.threads, args.output_subdir)
        wrapped = tmux_wrapper(i + 1, cmd)
        if args.ips:
            ip = args.ips[i]
            print(f"ssh root@{ip} \"{wrapped}\"")
        else:
            print(wrapped)

    print()
    print("=" * 72)
    print("MONITOR ALL BOXES (run from your laptop)")
    print("-" * 72)
    if args.ips:
        for i, ip in enumerate(args.ips):
            print(f"# Box {i+1} ({ip}):")
            print(f"ssh root@{ip} 'tail -n 5 /root/render-box{i+1}.log; "
                  f"ls /root/minecraft-worldgen/{args.output_subdir}/*.mca 2>/dev/null | wc -l'")
    else:
        print("Pass --ips IP1 IP2 ... to get ready-to-paste monitor commands")

    print()
    print("=" * 72)
    print("COLLECT OUTPUT WHEN DONE")
    print("-" * 72)
    if args.ips:
        ips_str = " ".join(args.ips)
        print(f"./cloud_bake/collect_outputs.sh {args.output_subdir} {ips_str}")
    else:
        print("./cloud_bake/collect_outputs.sh <output-subdir> IP1 IP2 ...")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
