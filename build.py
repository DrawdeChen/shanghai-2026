"""Build the Shanghai trip page from the sheet CSVs in data/.

  python build.py --preview   -> dist/preview.html  (plaintext, local only, never deploy)
  python build.py             -> docs/index.html    (AES-GCM encrypted, safe to publish)
"""
import argparse
import base64
import unicodedata
import csv
import datetime as dt
import getpass
import json
import os
import re
import shutil
import sys
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, 'data')
SRC = os.path.join(ROOT, 'src')

PBKDF2_ITERATIONS = 600_000
_T = r'\d{1,2}:\d{2}'
# "16:00", "09:00–09:40", "16:00／16:30–18:30", "11:00或11:30", "21:15後", "13:30左右"
TIME_RE = re.compile(rf'^({_T}(?:\s*[／/或]\s*{_T})?)(?:\s*[–\-]\s*({_T}(?:\s*[／/]\s*{_T})?))?\s*(左右|前(?!往)|後)?\s*(.*)$')
# "遊行後–17:20 明日世界…"
EVENT_RE = re.compile(rf'^(\S{{1,4}}[後前])\s*[–\-]\s*({_T})\s*(.*)$')
GENERIC_DISHES = {'蔥油拌麵', '排骨年糕'}
SECTIONS = [('起床／出發', 'start'), ('上午行程', 'am'), ('午餐', 'meal'),
            ('下午行程', 'pm'), ('晚餐', 'meal'), ('夜間行程', 'night')]
# Card to show staff in an emergency. Traditional for us, Simplified (via OpenCC) for them.
PHRASES = [
    ('孩子走失', '我們的孩子走失了，三歲男孩，請幫忙廣播或聯絡工作人員'),
]
# Checked against public sources on 2026-10-11 (see conversation notes).
STORIES = {
    '2026-10-14': '明天要去的上海迪士尼樂園在 2016 年 6 月 16 日開園，是中國內地第一座迪士尼樂園；今年 6 月剛滿十週年。',
    '2026-10-15': '「瘋狂動物城」園區在 2023 年 12 月 20 日開幕，是全球第一個瘋狂動物城主題園區；「熱力追蹤」是中國內地第一個無軌乘騎設施。',
    '2026-10-16': '豫園從 1559 年由潘允端動工，名字取「愉悅老親」，是蓋給父親養老的園林。下午登的東方明珠高 468 公尺，1994 到 2007 年間是中國與亞洲最高的建築。',
    '2026-10-17': '放生橋始建於明代 1571 年，清嘉慶年間重建，全長 72 公尺，是上海現存最大、最長的五孔石拱橋。',
    '2026-10-18': '上海野生動物園 1995 年開園，由上海市政府與國家林業部門合作興建，是中國第一座國家級野生動物園。',
    '2026-10-19': '武康大樓是匈牙利建築師鄔達克 1924 年的作品，原名諾曼底公寓，因地塊只有 30 度夾角，大樓形似一艘船。下午經過的國際飯店也出自他手，1934 年落成、高 83.8 公尺，曾被譽為「遠東第一高樓」。',
}
# Extra places the itinerary sends us to (pier, visitor centre, airport…) that aren't restaurants or
# attractions in the sheet. Kept in the gitignored data/ folder because together they trace our days.
def _extra():
    path = os.path.join(DATA, 'extra_dests.json')
    if not os.path.exists(path):
        return [], set()
    x = json.load(open(path, encoding='utf-8'))
    return [tuple(d) for d in x['dests']], set(x['in_brackets'])


EXTRA_DESTS, EXTRA_IN_BRACKETS = _extra()
# Chagee (霸王茶姬) stores come from the restaurant sheet; their 鄰近景點 tokens are matched against
# each itinerary line. 飯店周邊 is shorthand for every "back to the hotel" line.
TEA_PREFIX = '霸王茶姬'
TEA_ALIASES = {'飯店周邊': ['返回飯店', '回飯店', '人民廣場飯店']}
INFO_BOXES = [('交通安排', '交通'), ('必須預約／確認', '預約與確認'), ('親子／休息', '親子與休息'),
              ('可取消項目', '可取消'), ('延誤處理', '延誤時')]


def load(name):
    with open(os.path.join(DATA, name + '.csv'), encoding='utf-8') as f:
        rows = [[c.strip() for c in r] for r in csv.reader(f)]
    return [r for r in rows if any(r)]


def records(name):
    rows = load(name)
    head = rows[0]
    return [{h: (r[i] if i < len(r) else '') for i, h in enumerate(head) if h} for r in rows[1:]]


def lines(text):
    return [l.strip() for l in text.split('\n') if l.strip()]


def minutes(t):
    h, m = t.split(':')
    return int(h) * 60 + int(m)


def timeline(day):
    """Merge the six time-of-day columns into one list ordered by start time."""
    items, key = [], 0
    for col, kind in SECTIONS:
        first = True  # untimed lines attach to the previous item only within the same column
        for line in lines(day.get(col, '')):
            m, ev = TIME_RE.match(line), EVENT_RE.match(line)
            attach = items and not first
            first = False
            if m:
                start, end, mod, text = m.groups()
                key = minutes(start[:5].rstrip('／/或'))
                items.append({'t': start, 'e': end or '', 'mod': mod or '', 'text': text,
                              'kind': 'meal' if re.search('早餐|午餐|晚餐', text) and '取' not in text else kind, 'notes': [], 'k': key})
            elif ev:
                label, end, text = ev.groups()
                key = minutes(end) - 1
                items.append({'t': label, 'e': end, 'mod': '', 'text': text, 'kind': kind, 'notes': [], 'k': key})
            elif attach:
                items[-1]['notes'].append(line)
            else:
                items.append({'t': '', 'e': '', 'mod': '', 'text': line, 'kind': kind, 'notes': [], 'k': key})
    items.sort(key=lambda i: i['k'])  # stable: untimed lines keep their place
    for i in items:
        del i['k']
    return items


def short_date(s):
    d = dt.date.fromisoformat(s.replace('/', '-'))
    return d, f'{d.month}/{d.day}'


def build_data():
    # Short labels for the overview arches come from the summary table in 01.
    short = {}
    for r in load('01_旅程總覽'):
        if re.match(r'\d{4}/\d{2}/\d{2}', r[0]) and len(r) > 2:
            label = r[2].split('、')[0]
            short[r[0]] = label[2:] if label.startswith('上海') and len(label) > 3 else label

    # Mainland drivers and Amap search expect Simplified Chinese.
    from opencc import OpenCC
    t2s = OpenCC('t2s').convert

    days = []
    todos = records('08_行前準備')
    restaurants = [r for r in records('06_餐廳與美食') if r.get('你的評選') in ('想吃', '可選')]
    attractions = [a for a in records('05_景點候選') if a.get('你的評選') in ('想去', '可選')]
    names = [r['餐廳／美食'] for r in restaurants if r['餐廳／美食'] not in GENERIC_DISHES and not r['餐廳／美食'].startswith(TEA_PREFIX)]

    rest_names = set(names)
    # Destinations we can show a driver or paste into a ride-hailing app: (key found in text, name, address).
    # last field: search the full text (restaurants, bracket-only places) instead of text minus （…）
    dests = [d + (d[0] in EXTRA_IN_BRACKETS,) for d in EXTRA_DESTS]
    for r in restaurants:
        if r['餐廳／美食'] in GENERIC_DISHES:
            continue
        branch = r['區域／分店'].split('｜')[-1]  # "某某店｜某某路1號" -> the address part
        addr = re.sub(r'\s+', '', branch) if re.search(r'\d\s*號', branch) else ''
        dests.append((r['餐廳／美食'], r['餐廳／美食'], ('上海市' + addr) if addr and not addr.startswith('上海') else addr, True))
    for a in attractions:
        for part in re.split('[／＋]', a['景點名稱']):
            part = part.replace('精華段', '').strip()
            dests.append((part, part, '', False))
            for tail in ('塔', '古鎮'):
                if part.endswith(tail) and len(part) > len(tail) + 1:
                    dests.append((part[:-len(tail)], part, '', False))
    dests.sort(key=lambda x: -len(x[0]))

    teas = sorted([r for r in restaurants if r['餐廳／美食'].startswith(TEA_PREFIX)],
                  key=lambda r: r.get('你的評選') != '想吃')

    def find_tea(text):
        hits = []
        for r in teas:
            keys = []
            for tok in re.split('[／/、]', r.get('鄰近景點', '')):
                tok = tok.strip()
                keys += TEA_ALIASES.get(tok, [tok] if len(tok) >= 2 else [])
            if any(k in text for k in keys):
                name = r['餐廳／美食']
                branch = re.sub(r'^.*[（(](.+?)[）)].*$', lambda m: m.group(1), name)
                addr = re.sub(r'\s+', '', r['區域／分店']) if re.search(r'\d\s*號', r['區域／分店']) else ''
                hits.append({'name': name, 'branch': branch, 'addr': addr,
                             's': t2s(name), 'sa': t2s(('上海市' + addr) if addr and not addr.startswith('上海') else addr)})
        return hits[:2]

    def find_dests(text, notes):
        # Places only count when the item itself names them; notes often say "skip X".
        found, seen = [], set()
        for key, name, addr, in_notes in dests:
            # Branch names in brackets ("（外灘外白渡橋店）") mention landmarks we aren't visiting.
            hay = text + notes if in_notes else re.sub(r'（[^）]*）', '', text)
            if key in hay and name not in seen and not any(key in f['name'] for f in found):
                seen.add(name)
                found.append({'name': name, 'addr': addr, 's': t2s(name), 'sa': t2s(addr),
                              'food': name in rest_names})
        return found[:2]

    for row in records('02_每日行程'):
        d, md = short_date(row['日期'])
        bookings = [{'what': t['待辦事項'], 'status': t['狀態'], 'detail': t['重要資料／編號'], 'note': t['備註']}
                    for t in todos if t['分類'] in ('票券', '餐廳訂位') and md in t['待辦事項']]
        tl = timeline(row)
        for item in tl:
            blob = item['text'] + ' '.join(item['notes'])
            item['places'] = [n for n in names if len(n) >= 2 and n in blob][:2]
            item['dests'] = find_dests(item['text'], ' '.join(item['notes']))
            item['tea'] = [] if re.match(r'^(叫車)?前往', item['text']) else find_tea(item['text'])
        # Don't repeat the same hint on consecutive lines (陸家嘴 → 東方明珠).
        for prev, cur in zip(tl, tl[1:]):
            if cur['tea'] and [t['name'] for t in cur['tea']] == [t['name'] for t in prev.get('tea', [])]:
                cur['tea'] = []
        days.append({
            'date': d.isoformat(), 'md': md, 'dow': row['星期'], 'theme': row['當日主題'],
            'short': short.get(row['日期'], row['當日主題'].split('、')[0]),
            'story': STORIES.get(d.isoformat(), ''),
            'stroller': (re.search(r'【推車：(.+?)】', row.get('親子／休息', '')) or [None, ''])[1],
            'stay': lines(row['航班／住宿']), 'timeline': tl, 'bookings': bookings,
            'boxes': [{'title': label, 'items': lines(row.get(col, ''))} for col, label in INFO_BOXES
                      if lines(row.get(col, '')) and lines(row.get(col, '')) != ['無']],
        })

    flights = [f for f in records('03_航班與交通') if '航班' in f.get('類型', '')]
    hotels = records('04_住宿比較')
    add_maps(days, hotels, t2s)

    # Disney day route: key/value header block, timed rows, then rules.
    rows = load('11_迪士尼當日路線')
    facts, route, rules, mode, head = [], [], [], None, None
    for r in rows[1:]:
        if r[0] == '日期':
            facts = [(r[i], r[i + 1]) for i in range(0, len(r) - 1, 2) if r[i]]
        elif r[0] == '使用原則':
            facts.append(('使用原則', r[1]))
        elif r[0] == '時段':
            mode, head = 'route', r
        elif r[0] == '即時調整規則':
            mode = 'rules-title'
        elif r[0] == '情境':
            mode = 'rules'
        elif mode == 'route':
            route.append({h: r[i] for i, h in enumerate(head) if h and i < len(r)})
        elif mode == 'rules':
            rules.append({'case': r[0], 'rule': r[1]})

    terms = {r['餐廳／美食'] for r in restaurants} | {a['景點名稱'].replace('＋', '／').split('／')[0] for a in attractions}
    terms |= {h['飯店名稱'] for h in hotels} | {'上海市' + h['區域'] for h in hotels} | {'師傅，請送我們到', '謝謝！'}
    sc = {t: t2s(t) for t in terms}

    return {
        'sc': sc,
        'fx': fetch_rate(),
        'phrases': [{'label': k, 't': v, 's': t2s(v)} for k, v in PHRASES],
        'built': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M'),
        'days': days,
        'flights': flights,
        'hotels': hotels,
        'disney': {'facts': facts, 'route': route, 'rules': rules,
                   'rides': records('10_迪士尼設施攻略')},
        'attractions': attractions,
        'restaurants': restaurants,
        'todos': todos,
        'delivery': delivery(),
        'budget': records('07_預算與支出'),
    }


def delivery():
    """12_外送專區: store rows, then a '下單原則' key/value block."""
    rows = load('12_外送專區')
    head, stores, rules, in_rules = rows[0], [], [], False
    for r in rows[1:]:
        if r[0] == '下單原則':
            in_rules = True
        elif in_rules:
            rules.append({'k': r[0], 'v': r[1] if len(r) > 1 else ''})
        else:
            stores.append({h: (r[i] if i < len(r) else '') for i, h in enumerate(head) if h})
    rank = {'想吃': 0, '可選': 1, '': 2}
    stores.sort(key=lambda x: (rank.get(x.get('你的評選', ''), 3), -x.get('優先級', '').count('★')))
    return {'stores': [x for x in stores if x.get('你的評選') != '不吃'], 'rules': rules}


def add_maps(days, hotels, t2s):
    """Attach per-day street maps (see tools/daymap.py); places come from data/geo.json."""
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    import daymap
    geo_path = os.path.join(DATA, 'geo.json')
    geo = json.load(open(geo_path, encoding='utf-8')) if os.path.exists(geo_path) else {}
    for d in days:
        stay = next((h for h in hotels if h['入住日期'] <= d['date'] < h['退房日期']), None)             or next((h for h in hotels if h['退房日期'] == d['date']), None)
        points, legend, unpinned, seen, n = [], [], [], set(), 0

        def add(name, mark, when, label=None):
            g = geo.get(name)
            if not g:
                return False
            seen.add(name)
            points.append({'lat': g['lat'], 'lon': g['lon'], 'mark': mark, 's': t2s(label or name)})
            if mark != 'tea':
                legend.append({'mark': mark, 't': when, 'name': label or name, 'approx': bool(g.get('approx'))})
            return True

        if stay:
            add(stay['飯店名稱'], '宿', '', re.sub(r'（.*）', '', stay['飯店名稱']))
        if '迪士尼' in d['theme']:
            n += 1
            add('上海迪士尼樂園', str(n), '整天')
        # A place's time is when we're there, not the "前往…" line that sends us there.
        arrive = {}
        for it in d['timeline']:
            for p in it.get('dests', []):
                if '前往' not in it['text'] or p['name'] not in arrive:
                    arrive.setdefault(p['name'], None)
                    if '前往' not in it['text'] and arrive[p['name']] is None:
                        arrive[p['name']] = it['t'][:5]
        for it in d['timeline']:
            for p in it.get('dests', []):
                if p['name'] in seen or p['name'] in unpinned:
                    continue
                if '前往' in it['text'] and arrive.get(p['name']):
                    continue  # numbered at the arrival line instead
                if p['name'] in geo:
                    n += 1
                    add(p['name'], str(n), it['t'][:5])
                else:
                    unpinned.append(p['name'])
            for p in it.get('tea', [])[:1]:
                if p['name'] not in seen:
                    add(p['name'], 'tea', it['t'][:5])
        if len(points) < 2:
            continue
        stops = [p for p in points if p['mark'] != 'tea']
        d['map'] = {'panels': daymap.day_panels(points), 'legend': legend, 'unpinned': unpinned,
                    'amap': daymap.amap_markers(stops)}


def fetch_rate():
    """CNY→TWD rate for price hints; the page works without it."""
    try:
        with urllib.request.urlopen('https://open.er-api.com/v6/latest/CNY', timeout=10) as r:
            data = json.load(r)
        when = dt.datetime.fromtimestamp(data['time_last_update_unix'], dt.timezone(dt.timedelta(hours=8)))
        return {'twd': round(data['rates']['TWD'], 3), 'date': f'{when.month}/{when.day}'}
    except Exception as e:  # offline build: skip the hints
        print(f'exchange rate unavailable ({e}); building without NT$ hints')
        return None


def normalize(password: str) -> str:
    # Full-width letters from a Chinese IME (ａｂｃ１２３) and stray spaces are the usual reason a
    # password typed in the terminal "doesn't match" the phone. The page applies the same rule.
    return unicodedata.normalize('NFKC', password).strip()


def encrypt(plaintext: bytes, password: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    # The salt stays fixed across builds so phones that chose "remember" keep working
    # after an update with the same password. The IV must be fresh every time.
    salt_file = os.path.join(DATA, 'SALT')
    if os.path.exists(salt_file):
        salt = base64.b64decode(open(salt_file).read())
    else:
        salt = os.urandom(16)
        with open(salt_file, 'w') as f:
            f.write(base64.b64encode(salt).decode())
    iv = os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS).derive(normalize(password).encode())
    ct = AESGCM(key).encrypt(iv, plaintext, None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {'salt': b64(salt), 'iv': b64(iv), 'ct': b64(ct), 'iter': PBKDF2_ITERATIONS}


def render(payload_js: str) -> str:
    with open(os.path.join(SRC, 'template.html'), encoding='utf-8') as f:
        html = f.read()
    with open(os.path.join(SRC, 'poiret-one.woff2'), 'rb') as f:
        font = base64.b64encode(f.read()).decode()
    return html.replace('__FONT_B64__', font).replace('/*__PAYLOAD__*/', payload_js)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--preview', action='store_true', help='write an unencrypted local preview')
    args = ap.parse_args()
    data = json.dumps(build_data(), ensure_ascii=False)

    if args.preview:
        out = os.path.join(ROOT, 'dist', 'preview.html')
        payload = f'window.TRIP_PLAIN = {data};'
    else:
        pw = normalize(getpass.getpass('Password: '))
        if not pw.isascii() or not pw.isprintable():
            sys.exit('密碼含有英數字以外的字元（可能是注音輸入法）。請切到英文輸入法再試一次。')
        if len(pw) < 8:
            sys.exit('Use at least 8 characters.')
        if normalize(getpass.getpass('Repeat:   ')) != pw:
            sys.exit('Passwords do not match.')
        out = os.path.join(ROOT, 'docs', 'index.html')
        payload = f'window.TRIP_SEALED = {json.dumps(encrypt(data.encode(), pw))};'

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, 'w', encoding='utf-8') as f:
        f.write(render(payload))
    static = os.path.join(SRC, 'static')
    for name in os.listdir(static):
        shutil.copy(os.path.join(static, name), os.path.dirname(out))
    print(f'wrote {os.path.relpath(out, ROOT)} ({os.path.getsize(out) // 1024} KB)')
    if not args.preview:
        print(f'密碼長度：{len(pw)} 個字元（請確認和你想設定的一樣）')


if __name__ == '__main__':
    main()
