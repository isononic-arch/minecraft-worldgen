"""_dem_gitpath.py <key> — print an island's DEM path (from layout.json) as a
git-bash path (C:\\... -> /c/...), for scp in the cloud render scripts. Resolves
the ACTUAL dem_path (handles Grenada's pre-rotated DEM), not a key-glob guess."""
import json, sys

key = sys.argv[1]
for i in json.load(open('islands/layout.json'))['islands']:
    if key in i['dem_path']:
        p = i['dem_path'].replace('\\', '/')
        if len(p) > 1 and p[1] == ':':          # C:/... -> /c/...
            p = '/' + p[0].lower() + p[2:]
        print(p)
        break
