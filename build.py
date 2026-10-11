"""Build the Shanghai trip page from the sheet CSVs in data/.

  python build.py --preview   -> dist/preview.html  (plaintext, local only, never deploy)
  python build.py             -> docs/index.html    (AES-GCM encrypted, safe to publish)
"""
import argparse
import base64
import csv
import datetime as dt
import getpass
import json
import os
import re
import shutil
import sys

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
                              'kind': 'meal' if re.search('早餐|午餐|晚餐', text) else kind, 'notes': [], 'k': key})
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

    days = []
    todos = records('08_行前準備')
    restaurants = [r for r in records('06_餐廳與美食') if r.get('你的評選') in ('想吃', '可選')]
    names = [r['餐廳／美食'] for r in restaurants if r['餐廳／美食'] not in GENERIC_DISHES]
    for row in records('02_每日行程'):
        d, md = short_date(row['日期'])
        bookings = [{'what': t['待辦事項'], 'status': t['狀態'], 'detail': t['重要資料／編號'], 'note': t['備註']}
                    for t in todos if t['分類'] in ('票券', '餐廳訂位') and md in t['待辦事項']]
        tl = timeline(row)
        for item in tl:
            blob = item['text'] + ' '.join(item['notes'])
            item['places'] = [n for n in names if len(n) >= 2 and n in blob][:2]
        days.append({
            'date': d.isoformat(), 'md': md, 'dow': row['星期'], 'theme': row['當日主題'],
            'short': short.get(row['日期'], row['當日主題'].split('、')[0]),
            'stay': lines(row['航班／住宿']), 'timeline': tl, 'bookings': bookings,
            'boxes': [{'title': label, 'items': lines(row.get(col, ''))} for col, label in INFO_BOXES
                      if lines(row.get(col, '')) and lines(row.get(col, '')) != ['無']],
        })

    flights = records('03_航班與交通')
    hotels = records('04_住宿比較')

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

    # Mainland drivers and Amap search expect Simplified Chinese.
    from opencc import OpenCC
    t2s = OpenCC('t2s').convert
    attractions = [a for a in records('05_景點候選') if a.get('你的評選') in ('想去', '可選')]
    terms = {r['餐廳／美食'] for r in restaurants} | {a['景點名稱'].replace('＋', '／').split('／')[0] for a in attractions}
    terms |= {h['飯店名稱'] for h in hotels} | {'上海市' + h['區域'] for h in hotels} | {'師傅，請送我們到', '謝謝！'}
    sc = {t: t2s(t) for t in terms}

    return {
        'sc': sc,
        'built': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M'),
        'days': days,
        'flights': flights,
        'hotels': hotels,
        'disney': {'facts': facts, 'route': route, 'rules': rules,
                   'rides': records('10_迪士尼設施攻略')},
        'attractions': attractions,
        'restaurants': restaurants,
        'todos': todos,
        'budget': records('07_預算與支出'),
    }


def encrypt(plaintext: bytes, password: str) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=PBKDF2_ITERATIONS).derive(password.encode())
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
        pw = getpass.getpass('Password: ')
        if len(pw) < 8:
            sys.exit('Use at least 8 characters.')
        if getpass.getpass('Repeat:   ') != pw:
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


if __name__ == '__main__':
    main()
