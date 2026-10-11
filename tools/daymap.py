"""Per-day street maps drawn from OpenStreetMap data, rendered as inline SVG at build time.

Base layers (roads, rivers, parks) come from the Overpass API and are cached in data/maps/,
so the page works offline and never loads map tiles. Coordinates are WGS84 throughout;
Amap links get GCJ-02 via wgs_to_gcj().
"""
import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, 'data', 'maps')
OVERPASS = ['https://overpass-api.de/api/interpreter', 'https://overpass.kumi.systems/api/interpreter']
UA = 'shanghai-trip-map/1.0 (personal itinerary, low volume)'
W, H = 360, 320          # SVG viewBox
CLUSTER_KM = 15          # points further apart than this get their own panel


def km(a, b):
    dy = (a[0] - b[0]) * 111.0
    dx = (a[1] - b[1]) * 111.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


# ── WGS84 → GCJ-02 (the offset mainland maps use) ──────────────────────────
def _t_lat(x, y):
    r = -100 + 2 * x + 3 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    r += (20 * math.sin(y * math.pi) + 40 * math.sin(y / 3 * math.pi)) * 2 / 3
    r += (160 * math.sin(y / 12 * math.pi) + 320 * math.sin(y * math.pi / 30)) * 2 / 3
    return r


def _t_lon(x, y):
    r = 300 + x + 2 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20 * math.sin(6 * x * math.pi) + 20 * math.sin(2 * x * math.pi)) * 2 / 3
    r += (20 * math.sin(x * math.pi) + 40 * math.sin(x / 3 * math.pi)) * 2 / 3
    r += (150 * math.sin(x / 12 * math.pi) + 300 * math.sin(x / 30 * math.pi)) * 2 / 3
    return r


def wgs_to_gcj(lat, lon):
    a, ee = 6378245.0, 0.00669342162296594323
    dlat, dlon = _t_lat(lon - 105, lat - 35), _t_lon(lon - 105, lat - 35)
    rad = math.radians(lat)
    magic = 1 - ee * math.sin(rad) ** 2
    sq = math.sqrt(magic)
    dlat = dlat * 180 / ((a * (1 - ee)) / (magic * sq) * math.pi)
    dlon = dlon * 180 / (a / sq * math.cos(rad) * math.pi)
    return lat + dlat, lon + dlon


# ── Overpass fetch with cache ────────────────────────────────────────────────
def fetch(bbox, detailed):
    s, w, n, e = bbox
    key = hashlib.md5(json.dumps([round(v, 4) for v in bbox] + [detailed]).encode()).hexdigest()[:12]
    path = os.path.join(CACHE_DIR, key + '.json')
    if os.path.exists(path):
        return json.load(open(path, encoding='utf-8'))
    minor = '|residential|unclassified|pedestrian|living_street' if detailed else ''
    q = f'''[out:json][timeout:90];
(
  way["highway"~"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link{minor})$"]({s},{w},{n},{e});
  way["waterway"~"^(river|canal)$"]({s},{w},{n},{e});
  way["natural"="water"]({s},{w},{n},{e});
  way["leisure"="park"]({s},{w},{n},{e});
);
out geom;'''
    last = None
    for attempt in range(6):
        for ep in OVERPASS:
            try:
                req = urllib.request.Request(ep, data=urllib.parse.urlencode({'data': q}).encode(), headers={'User-Agent': UA})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.load(r)
                feats = []
                for el in data.get('elements', []):
                    t = el.get('tags', {})
                    geom = [(round(p['lat'], 6), round(p['lon'], 6)) for p in el.get('geometry', [])]
                    if len(geom) < 2:
                        continue
                    hw = t.get('highway', '')
                    if hw:
                        cls = 'r1' if hw.startswith(('motorway', 'trunk')) else 'r2' if hw.startswith(('primary', 'secondary')) else 'r3' if hw == 'tertiary' else 'r4'
                    elif t.get('waterway') == 'river':
                        cls = 'river'
                    elif t.get('waterway'):
                        cls = 'canal'
                    elif t.get('natural') == 'water':
                        cls = 'lake'
                    else:
                        cls = 'park'
                    feats.append({'c': cls, 'g': geom, 'n': t.get('name', '')})
                os.makedirs(CACHE_DIR, exist_ok=True)
                json.dump(feats, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
                return feats
            except Exception as ex:  # busy servers are common; rotate and retry
                last = ex
                time.sleep(10 + 10 * attempt)
    # Overpass is often overloaded. Fall back to features already cached for neighbouring maps.
    feats = []
    for name in os.listdir(CACHE_DIR) if os.path.isdir(CACHE_DIR) else []:
        for f in json.load(open(os.path.join(CACHE_DIR, name), encoding='utf-8')):
            if any(s <= la <= n and w <= lo <= e for la, lo in f['g']):
                feats.append(f)
    if feats:
        print(f'Overpass unavailable ({last}); reused {len(feats)} cached features for {bbox}')
        return feats
    raise RuntimeError(f'Overpass failed for {bbox}: {last}')


# ── Geometry helpers ─────────────────────────────────────────────────────────
def _dp(pts, tol):
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    norm = math.hypot(dx, dy) or 1e-9
    i, dmax = 0, 0
    for k in range(1, len(pts) - 1):
        d = abs(dy * pts[k][0] - dx * pts[k][1] + x2 * y1 - y2 * x1) / norm
        if d > dmax:
            i, dmax = k, d
    if dmax <= tol:
        return [pts[0], pts[-1]]
    return _dp(pts[:i + 1], tol)[:-1] + _dp(pts[i:], tol)


def frame(points):
    """Bounding box around points, padded and stretched to the W:H aspect ratio."""
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    lat0 = (min(lats) + max(lats)) / 2
    kx = math.cos(math.radians(lat0))
    w_km = max((max(lons) - min(lons)) * 111 * kx, 0.9) * 1.35 + 0.5
    h_km = max((max(lats) - min(lats)) * 111, 0.6) * 1.35 + 0.5
    if w_km / h_km < W / H:
        w_km = h_km * W / H
    else:
        h_km = w_km * H / W
    clat, clon = lat0, (min(lons) + max(lons)) / 2
    dlat, dlon = h_km / 111 / 2, w_km / 111 / kx / 2
    return (clat - dlat, clon - dlon, clat + dlat, clon + dlon), w_km


def render(points, feats, bbox, w_km):
    """points: dicts with lat, lon, mark ('1'.. / '宿' / 'tea'), label."""
    s, w, n, e = bbox
    proj = lambda la, lo: ((lo - w) / (e - w) * W, (n - la) / (n - s) * H)
    px_per_km = W / w_km
    out = [f'<svg viewBox="0 0 {W} {H}" class="dmap" role="img" aria-label="當日地圖">',
           f'<rect width="{W}" height="{H}" class="m-bg"/>']
    order = {'park': 0, 'lake': 1, 'river': 2, 'canal': 3, 'r4': 4, 'r3': 5, 'r2': 6, 'r1': 7}
    river_w = max(2.5, 0.35 * px_per_km)  # Huangpu is ~350–400 m wide
    for f in sorted(feats, key=lambda f: order[f['c']]):
        pts = _dp([proj(*p) for p in f['g']], 0.7)
        d = 'M' + 'L'.join(f'{x:.1f} {y:.1f}' for x, y in pts)
        if f['c'] in ('park', 'lake'):
            out.append(f'<path d="{d}Z" class="m-{f["c"]}"/>')
        elif f['c'] == 'river':
            out.append(f'<path d="{d}" class="m-river" stroke-width="{river_w:.1f}"/>')
        else:
            out.append(f'<path d="{d}" class="m-{f["c"]}"/>')
    # river names, once each, at the middle of the longest piece
    named = {}
    for f in feats:
        if f['c'] == 'river' and f['n']:
            if len(f['g']) > len(named.get(f['n'], [])):
                named[f['n']] = f['g']
    for name, g in named.items():
        x, y = proj(*g[len(g) // 2])
        if 20 < x < W - 20 and 15 < y < H - 15:
            out.append(f'<text x="{x:.0f}" y="{y:.0f}" class="m-rname">{name}</text>')
    # markers, fanned out when they would overlap
    placed = []
    for p in points:
        x, y = proj(p['lat'], p['lon'])
        ox, oy, ang = x, y, -math.pi / 2
        while any(math.hypot(ox - a, oy - b) < 22 for a, b in placed):
            ox, oy = x + 25 * math.cos(ang), y + 25 * math.sin(ang)
            ang += math.pi / 3
            if ang > 4 * math.pi:
                break
        placed.append((ox, oy))
        if (ox, oy) != (x, y):
            out.append(f'<path d="M{x:.1f} {y:.1f}L{ox:.1f} {oy:.1f}" class="m-lead"/><circle cx="{x:.1f}" cy="{y:.1f}" r="1.6" class="m-dot"/>')
        if p['mark'] == 'tea':
            out.append(f'<g transform="translate({ox:.1f},{oy:.1f})" class="m-tea"><circle r="7.5"/><path d="M-3.4 -3H3.4L2.6 4H-2.6Z"/></g>')
        else:
            cls = 'm-stay' if p['mark'] == '宿' else 'm-pin'
            out.append(f'<g transform="translate({ox:.1f},{oy:.1f})" class="{cls}"><path d="M0 -11L11 0L0 11L-11 0Z"/><text y="4">{p["mark"]}</text></g>')
    # scale bar + north arrow
    nice = min([0.2, 0.5, 1, 2, 5, 10, 20], key=lambda v: abs(v * px_per_km - 60))
    bar = nice * px_per_km
    lbl = f'{int(nice * 1000)} m' if nice < 1 else f'{nice:g} km'
    out.append(f'<g class="m-scale" transform="translate(12,{H - 12})"><path d="M0 0H{bar:.0f}M0 -3V3M{bar:.0f} -3V3"/><text x="{bar / 2:.0f}" y="-5">{lbl}</text></g>')
    out.append(f'<g class="m-north" transform="translate({W - 16},{H - 22})"><path d="M0 -11L4 0L0 11L-4 0Z"/><text y="-14">N</text></g>')
    out.append('</svg>')
    return ''.join(out)


def amap_markers(points):
    marks = []
    for p in points[:10]:
        la, lo = wgs_to_gcj(p['lat'], p['lon'])
        marks.append(f"{lo:.6f},{la:.6f},{p['s'].replace(',', ' ').replace('|', ' ')}")
    return 'https://uri.amap.com/marker?' + urllib.parse.urlencode({'markers': '|'.join(marks), 'src': 'shanghai-trip', 'callnative': 1}, safe=',|')


def clusters(points):
    groups = []
    for p in points:
        home = next((g for g in groups if any(km((p['lat'], p['lon']), (q['lat'], q['lon'])) < CLUSTER_KM for q in g)), None)
        if home is None:
            groups.append([p])
        else:
            home.append(p)
    return groups


def day_panels(points):
    panels = []
    for g in clusters(points):
        bbox, w_km = frame([(p['lat'], p['lon']) for p in g])
        feats = fetch(bbox, detailed=w_km < 7)
        panels.append({'svg': render(g, feats, bbox, w_km), 'marks': [p['mark'] for p in g]})
    return panels
