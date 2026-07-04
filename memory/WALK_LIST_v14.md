# VandirIslandsV14 WALK LIST — S104 island fixes (2026-07-04)

World: `VandirIslandsV14` (Modrinth test profile, NVMe). ALL 15 islands re-rendered
with: beaches redone (slope-driven, steep=rock-to-water), flat-island spline variation,
lightened shallow kelp, and 4 tree-placement fixes (no floating leaves over water, clearings
grassed + tree-suppressed, no clearing-seam/half-cut trees, no trees on beaches).

**FIRST CHECK:** fly open ocean → water to horizon, gravel/kelp seabed ~Y-14/-16.

TPs are LAND-CENTROIDS (the old bbox-center TPs landed in ocean — that's why NV was "unfindable").

| Island | TP | What changed / to check |
|---|---|---|
| **New Vincentia** ⭐ | `/tp @s 4920 200 11376` | FINDABLE now (was bad TP). Steep volcanic beaches → thin sand/rock-to-water |
| **Anguilla** ⭐ | `/tp @s 32840 200 57696` | SPLINE VARIATION: was pancake, now rolling limestone (true hills preserved) |
| **Bahamas** ⭐ | `/tp @s 44104 200 58352` | SPLINE: rolling dune/cay undulation (was flat plate); +1 tile from lift |
| **Grand Turk** ⭐ | `/tp @s 44976 200 49296` | SPLINE: rolling relief; dive Caicos Bank edge (taper ramp, not wall) |
| **La Tortuga** | `/tp @s 53024 200 65248` | SPLINE: rolling arid carbonate (was Y64-69 squash) |
| **Los Roques** | `/tp @s 35024 200 50608` | SPLINE: rolling atoll rims + cays |
| **Ouvea** | `/tp @s 53768 200 56512` | SPLINE: rolling raised atoll; lagoon renders from DEM |
| **Admiralty** | `/tp @s 57672 200 53240` | SPLINE: gentle relief; equatorial LUSH |
| **Loyalty** | `/tp @s 50952 200 63080` | UNCHANGED (you approved as-is) |
| **Efate** ⭐ | `/tp @s -1152 200 40584` | Warm-temperate reband; clearings grassed; 4 NW-edge tiles are ocean gaps (new beach shrank footprint — cosmetic, offer to patch) |
| **Fogo** | `/tp @s 23600 200 -4704` | Boreal barrens clearings (grassed now, not forested) |
| **Madre de Dios** | `/tp @s 12480 200 -5320` | Fjord karst; floodplain benches; taper rims |
| **Kostati** | `/tp @s 21896 200 55328` | Tropical archipelago; lightened kelp in the turquoise water |
| **Margarita** | `/tp @s 47120 200 51912` | Dry continental semi-arid |
| **Grenada** | `/tp @s 29576 200 52885` | Windward-ridge windthrow |

**WALK-FOCUS on the fixes you reported:**
- Gallery forest at rivers (any forested island w/ a river): trees should thicken toward water but NO floating leaves/branches over the channel now.
- Clearings (Efate/Fogo/Kostati openings): should be GRASS surface, not forest-floor with canopy.
- Tile seams in dense canopy: no trees marching into clearings, no half-cut trees.
- Beaches: sand only on gentle shores, no trees standing in the sand.
- Shallow (turquoise) water: kelp halved — should read lighter.

KNOWN: 4 Efate NW-edge ocean gaps (footprint shrank with the new beach). Cosmetic; a
~5-min local patch-render can fill them if you want. Semi-arid seam = MAINLAND biome,
deferred to mainland-render prep (doesn't affect this island walk).
