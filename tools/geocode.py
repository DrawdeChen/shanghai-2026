"""Look up coordinates (WGS84) for map places via OpenStreetMap Nominatim; cache in data/geo.json.

Each place is tried with several queries (name, then address) until one hits.
Run: python tools/geocode.py            -> fills missing entries
     python tools/geocode.py --list     -> show what is cached / missing
"""
import json, os, sys, time, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, 'data', 'geo.json')
UA = 'shanghai-trip-map/1.0 (personal itinerary, low volume)'

# key (Traditional, as used in the page) -> queries in Simplified, most specific first.
# Kept in the gitignored data/ folder: it names the hotels we stay at.
PLACES = json.load(open(os.path.join(ROOT, 'data', 'places.json'), encoding='utf-8'))


def nominatim(q):
    url = 'https://nominatim.openstreetmap.org/search?' + urllib.parse.urlencode(
        {'q': q, 'format': 'json', 'limit': 1, 'countrycodes': 'cn', 'accept-language': 'zh',
         'viewbox': '120.8,31.6,122.0,30.6', 'bounded': 1})
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        res = json.load(r)
    time.sleep(1.1)  # Nominatim usage policy: max 1 request per second
    return res[0] if res else None


def main():
    cache = json.load(open(CACHE, encoding='utf-8')) if os.path.exists(CACHE) else {}
    if '--list' not in sys.argv:
        for key, queries in PLACES.items():
            if key in cache:
                continue
            for q in queries:
                hit = nominatim(q)
                if hit:
                    cache[key] = {'lat': float(hit['lat']), 'lon': float(hit['lon']), 'q': q, 'osm': hit.get('display_name', '')[:80]}
                    break
            with open(CACHE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
    for key in PLACES:
        c = cache.get(key)
        print(('OK  ' if c else 'MISS'), key, '' if not c else f"{c['lat']:.5f},{c['lon']:.5f}  <- {c['q']} | {c['osm']}")


if __name__ == '__main__':
    main()
