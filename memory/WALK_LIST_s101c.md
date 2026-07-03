# S101c COMPREHENSIVE WALK LIST — assess before the full mainland render + splice

Two worlds to walk. **First check in EITHER:** fly open ocean → water to the horizon,
gravel/kelp seabed ~Y-14/-16. Visible land/mountains in open ocean = datapack lost the cap.

═══════════════════════════════════════════════════════════════════════════
## WORLD 1 — ISLANDS (VandirIslandsV13, Modrinth test profile)
All 15 islands, V10 render (tree-seam fix + taper + hole-fill + new offsets +
DEM eco masks: windthrow/floodplain/clearings). TP to island CENTERS:

| Island | TP | What's new / to check |
|---|---|---|
| **Efate** ⭐ | `/tp @s -1039 200 39921` | Warm-temperate reband (rainforest→mixed→deciduous, NO tropics); floodplains 8.6% |
| Efate verified clearing ⭐ | `/tp @s -768 200 40704` | The block-measured clearing: grass sea + tall-grass dither at forest edge |
| **Fogo** ⭐ | `/tp @s 24272 200 -5424` | New offset; boreal barrens — strongest clearings (7.2%) |
| **Madre de Dios** ⭐ | `/tp @s 12115 200 -3245` | z=-8888 offset; fjord karst; taper on bank rims; floodplain benches 6.9% |
| **Ouvea** ⭐ | `/tp @s 52719 200 55791` | Atoll LAGOON now renders from DEM (was a generator hole) |
| Grand Turk | `/tp @s 43923 200 50067` | Dive the Caicos Bank edge: old 75-blk wall → smooth ramp to ~Y-17 |
| New Vincentia | `/tp @s 5433 200 12089` | St Kitts/Nevis/Statia; floodplain 13.3% (judge if overdone) |
| Kostati | `/tp @s 23263 200 54495` | Tropical archipelago; clearings 4.3% |
| Grenada | `/tp @s 32405 200 52885` | Windward-ridge windthrow (3%, the most of any island) |
| Margarita | `/tp @s 45162 200 51306` | Dry continental; semi-arid brush |
| Anguilla | `/tp @s 33174 200 56726` | Limestone; tidal jungle |
| La Tortuga | `/tp @s 52844 200 65132` | Flat arid coral (0 floodplain by relief gate — correct) |
| Los Roques | `/tp @s 33058 200 51490` | Coral atoll |
| Admiralty | `/tp @s 59805 200 52637` | Equatorial rainforest, LUSH |
| Loyalty | `/tp @s 50190 200 64014` | Raised coral limestone |

═══════════════════════════════════════════════════════════════════════════
## WORLD 2 — MAINLAND REALISM TEST TILES (install to Vandirtest10)
8 tiles = 4 fix-headlining 2×1 pairs. Each pair chosen to showcase one new
mainland realism fix. Walk each pair, compare to your mental "before".

### FIX #1 — Terrain-derived clearings (was pure-noise blobs on cliffs)
Dense forest w/ benches + hollows; clearings should sit on FLAT/WET ground, none on ridges.
- (75,70)  `/tp @s 38656 200 36096`
- (76,70)  `/tp @s 39168 200 36096`

### FIX #2 — Riparian canopy densification (rivers = denser forest lanes, +90% at channel)
Full-forest river tiles; the gallery forest should visibly THICKEN toward the water.
- (68,65)  `/tp @s 35072 200 33536`
- (68,66)  `/tp @s 35072 200 34048`

### FIX #3 — Aspect asymmetry (sun-baked south slopes thinner than shaded north)
High-relief ridge; a ridge's two flanks should read DIFFERENTLY (S sparser, N denser).
- (89,51)  `/tp @s 45824 200 26368`
- (89,52)  `/tp @s 45824 200 26880`

### FIX #4 — Aspect-dependent treeline (sawtooth edge, higher on sun slopes)
High-elevation forest crossing treeline; the forest edge should ZIGZAG with aspect, not a flat line.
- (57,48)  `/tp @s 29440 200 24832`
- (58,48)  `/tp @s 29952 200 24832`

Note: all 4 fixes are GLOBAL mainland code — every tile shows all of them; the
pairs just headline one each. Knobs (if any reads over/under): eco_placement.
riparian_density_boost=0.9, aspect_density_amp=0.28, treelines.aspect_amplitude=45,
+ clearing synth knobs in tools/build_mainland_clearing_mask.py.
