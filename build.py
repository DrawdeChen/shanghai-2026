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
# Cards to show staff. Traditional for us, Simplified (via OpenCC) for them.
PHRASES = [
    ('人數', '我們三位，兩個大人一個小孩'),
    ('訂位', '我們有訂位，姓陳'),
    ('兒童椅', '請給我們一張兒童椅，謝謝'),
    ('不辣', '請不要放辣'),
    ('小孩吃', '小孩要吃，請少油少鹽'),
    ('打包', '請幫我們打包，謝謝'),
    ('買單', '請幫我們買單'),
    ('洗手間', '請問洗手間在哪裡？'),
    ('等多久', '請問大概要等多久？'),
    ('付款', '可以用支付寶或微信支付嗎？'),
    ('孩子走失', '我們的孩子走失了，三歲男孩，請幫忙聯絡工作人員'),
    ('身體不適', '小孩身體不舒服，請問附近有醫務室或醫院嗎？'),
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
            'story': STORIES.get(d.isoformat(), ''),
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
        'budget': records('07_預算與支出'),
    }


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
