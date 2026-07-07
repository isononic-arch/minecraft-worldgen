# Bloomhost storage audit — completed 2026-07-06 (post-S106 deploy)

## Current major consumers (~110 GB used of 135)
| GB | Item | Note |
|---|---|---|
| 76.3 | VandirWorld_S106 | THE world — keep |
| 15.9 | bluemap/ | map tiles incl. STALE renders of deleted S105 + old worlds — biggest reclaim |
| 5.7 | Vandir_2024 | old world |
| 1.8 | Vandir | old world (pre-2024) |
| 1.8 | DNV1123 | old DNV build |
| 1.7 | Zachdir | member world |
| 1.3 | OOC_Sandbox | sandbox |
| 1.2 | DONOTVISITPLEASE | old world |
| 0.6 | DoNotVisitBroville | old world |
| 0.4 | DoNotVisitBroville-2022 tar.gz | root-level backup blob |
| 0.3 | cache/ | server cache |
| 0.05 | server.jar.old | superseded jar |

## Recommendations (nothing deleted — owner's call)
1. bluemap purge (~10-14 GB back): delete bluemap web tiles for deleted/old worlds;
   let it re-render S106 only. Config-safe.
2. tar.gz + server.jar.old (~0.45 GB): pure junk, zero risk.
3. Old worlds (~13 GB total): archive to local disk via SFTP before deleting if wanted.
Headroom now ~25 GB — no urgency; bluemap will grow as it renders S106.
