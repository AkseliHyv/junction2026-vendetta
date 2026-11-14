"""Convert an Overpass JSON dump of highways into src/osm-roads.js.

    curl -s -A noise-map -H 'Accept: application/json' https://overpass-api.de/api/interpreter \
      --data-urlencode 'data=[out:json][timeout:60];(
        way["highway"~"^(motorway|motorway_link|trunk|trunk_link|primary|primary_link|secondary|secondary_link|tertiary|tertiary_link|unclassified|residential|living_street|service|pedestrian)$"](60.160,24.780,60.205,24.875);
        way["railway"~"^(tram|light_rail)$"](60.160,24.780,60.205,24.875););out tags geom;' -o roads.json
    python3 tools/osm_to_js.py roads.json src/osm-roads.js

The bbox (south,west,north,east) should be a bit larger than NM.data.area so
streets just outside the area still contribute noise at its edge.
"""
import json, math, sys

src, dst = sys.argv[1], sys.argv[2]
d = json.load(open(src))
LAT0 = 60.184
KX = math.cos(math.radians(LAT0)) * 111320
KY = 110574

def proj(p): return (p['lon'] * KX, p['lat'] * KY)

def dp(pts, tol):
    if len(pts) < 3: return pts
    keep = [False] * len(pts); keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    P = [proj(p) for p in pts]
    while stack:
        a, b = stack.pop()
        ax, ay = P[a]; bx, by = P[b]
        dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy
        best, bi = -1, -1
        for i in range(a + 1, b):
            px, py = P[i]
            t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
            dd = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if dd > best: best, bi = dd, i
        if best > tol:
            keep[bi] = True; stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]

def num(v):
    try: return int(str(v).split(';')[0].split()[0])
    except Exception: return 0

out, npts = [], 0
for e in d['elements']:
    t = e['tags']
    h = t.get('highway') or t.get('railway')
    if t.get('tunnel') in ('yes', 'culvert') or t.get('covered') == 'yes': continue
    if h == 'service' and t.get('service') in ('driveway', 'parking_aisle'): continue
    g = dp(e['geometry'], 2.5)
    npts += len(g)
    flat = []
    for p in g: flat += [round(p['lon'], 5), round(p['lat'], 5)]
    r = {'h': h, 'c': flat}
    if t.get('name'): r['n'] = t['name']
    if num(t.get('maxspeed')): r['s'] = num(t.get('maxspeed'))
    if num(t.get('lanes')): r['l'] = num(t.get('lanes'))
    if t.get('tunnel') == 'building_passage': r['bp'] = 1
    out.append(r)

out.sort(key=lambda r: (r['h'], r.get('n', '')))
ts = d.get('osm3s', {}).get('timestamp_osm_base', '')
with open(dst, 'w') as f:
    f.write('/* Street network around Otaniemi, Espoo. Generated from OpenStreetMap\n')
    f.write(' * (c) OpenStreetMap contributors, ODbL. Snapshot: %s\n' % ts)
    f.write(' * h = highway/railway class, n = name, s = maxspeed, l = lanes,\n')
    f.write(' * bp = building passage, c = [lon, lat, lon, lat, ...]. Tunnels, driveways\n')
    f.write(' * and parking aisles are left out (no surface noise / negligible). */\n')
    f.write('window.NM = window.NM || {};\nNM.osmRoads = [\n')
    for r in out: f.write(json.dumps(r, ensure_ascii=False, separators=(',', ':')) + ',\n')
    f.write('];\n')
print(len(out), 'ways', npts, 'points')
