"""Re-download every tab of the trip sheet into data/ and print what changed."""
import csv, io, os, sys, urllib.parse, urllib.request
D = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
# The sheet ID stays in the gitignored data/ folder: with link sharing on, the ID alone grants read access.
ID = open(os.path.join(D, 'SHEET_ID'), encoding='utf-8').read().strip()
import re, html as _h
_page = urllib.request.urlopen(f'https://docs.google.com/spreadsheets/d/{ID}/htmlview').read().decode('utf-8')
SHEETS = list(dict.fromkeys(_h.unescape(n) for n in re.findall(r'name: "([^"]+)"', _page)))
print('tabs:', SHEETS)
total = 0
for s in SHEETS:
    url = f'https://docs.google.com/spreadsheets/d/{ID}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(s)}'
    new = urllib.request.urlopen(url).read().decode('utf-8')
    if new.lstrip().startswith('<'):
        print(s, 'FAILED (not CSV, sharing off?)'); continue
    path = os.path.join(D, s + '.csv')
    old = open(path, encoding='utf-8').read() if os.path.exists(path) else ''
    a, b = list(csv.reader(io.StringIO(old))), list(csv.reader(io.StringIO(new)))
    hdr = b[0] if b else []
    diffs = []
    for i in range(max(len(a), len(b))):
        ra = a[i] if i < len(a) else []; rb = b[i] if i < len(b) else []
        for j in range(max(len(ra), len(rb))):
            ca = ra[j] if j < len(ra) else ''; cb = rb[j] if j < len(rb) else ''
            if ca != cb: diffs.append((i, j, ca, cb))
    total += len(diffs)
    if diffs and not old:
        print(f'== {s}: NEW TAB, {len(b)} rows')
    elif diffs:
        print(f'== {s}: rows {len(a)}->{len(b)}, {len(diffs)} cells changed')
        for i, j, ca, cb in diffs:
            row = (b[i][0] if i < len(b) and b[i] else a[i][0] if i < len(a) and a[i] else '')[:24]
            col = hdr[j] if j < len(hdr) else j
            print(f'  [{row} | {col}]\n    OLD: {ca[:400]!r}\n    NEW: {cb[:400]!r}')
    open(path, 'w', encoding='utf-8', newline='').write(new)
print('TOTAL changed cells:', total)
