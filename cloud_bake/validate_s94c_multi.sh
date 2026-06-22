#!/usr/bin/env bash
# Run the S94c re-render checklist on the collected multibox tiles.
set -u
R="/d/s94c_multi"; A="$R/all"
PY="C:/Users/nicho/AppData/Local/Python/pythoncore-3.14-64/python.exe"
cd C:/Users/nicho/minecraft-worldgen || exit 2
mkdir -p "$A"; cp -f "$R"/box*/r.*.mca "$A/" 2>/dev/null
N=$(ls "$A"/r.*.mca 2>/dev/null | wc -l); echo "=== collected $N MCAs ==="

echo ""; echo "########## PERCH (water touching air) + FLOATING TREES ##########"
for f in "$A"/r.*.mca; do
  b=$(basename "$f"); tx=$(echo "$b"|cut -d. -f2); tz=$(echo "$b"|cut -d. -f3)
  "$PY" tools/diag_mca_surface_perch.py "$A" "$tx" "$tz" 2>/dev/null | grep -a "==="
  "$PY" tools/diag_floating_trees.py "$A" "$tx" "$tz" 2>/dev/null | grep -a "==="
done

echo ""; echo "########## LAKES (shore + river-lake junction) ##########"
for t in "16 76" "60 72" "20 74" "51 53"; do
  [ -f "$A/r.${t% *}.${t#* }.mca" ] && "$PY" tools/diag_lake_river_level.py "$A" $t 2>/dev/null | grep -aE "===|LAKE-PERCH|JUNCTION"
done

echo ""; echo "########## SEAMS (water-steps>=1 must be ~0 on rivers) ##########"
seam(){ printf "%-22s " "$3"; "$PY" tools/diag_seam_readout.py "$1" "$A" "$2" 2>/dev/null | grep -a SUMMARY; }
seam H "52 53" "water 52,53|52,54"
seam V "52 52" "water 52,52|53,52"
seam V "12 80" "wash 12,80|13,80"
seam H "69 61" "hialt 69,61|69,62"
seam H "69 62" "hialt 69,62|69,63"
seam H "89 57" "hialt 89,57|89,58"
seam H "74 65" "mtn 74,65|74,66 (snow/litho)"
seam H "74 66" "mtn 74,66|74,67"
seam V "73 66" "mtn 73,66|74,66"
seam V "74 66" "mtn 74,66|75,66"
seam H "74 67" "snow-trans 74,67|74,68"
echo ""; echo "(reminder: water-step cols labelled 'land' are terrain micro-steps, not water)"
