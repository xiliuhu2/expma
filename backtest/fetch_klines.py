#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并发抓取 universe 全部股票日 K（前复权），缓存为 JSON。"""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

OUT = os.path.expanduser('~/WorkBuddy/Claw/backtest/klines')
os.makedirs(OUT, exist_ok=True)

# 名单基石只读正式档，本脚本只消费、绝不写回。旧版会优先读 /tmp/dk_snapshot.json
# 并把它持久化覆盖正式档，测试残留可能静默换掉基石，已移除该后门；
# 新档统一由 backtest/fetch_snapshot.py 收盘校验（含备份/原子替换）后写入。
SNAP = os.path.expanduser('~/WorkBuddy/Claw/backtest/snapshot.json')
snap = json.load(open(SNAP))
items = snap if isinstance(snap, list) else snap.get('data', {}).get('items', [])
def _is_st(name):
    n = (name or '').upper().replace(' ', '').replace('　', '')
    return ('ST' in n) or ('退' in (name or ''))

uni = [r['code'] for r in items
       if float(r.get('TotalMV') or 0) > 0
       and not _is_st(r.get('name', ''))]

refresh = '--refresh' in sys.argv
if refresh:
    todo = uni  # 全量重抓（覆盖旧缓存，纳入最新交易日bar）
else:
    todo = [c for c in uni if not os.path.exists(os.path.join(OUT, c + '.json'))]
print('universe %d, to fetch %d' % (len(uni), len(todo)), flush=True)

ENDPOINTS = [
    'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=%s,day,,,320,qfq',
    'https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?param=%s,day,,,320,qfq',
    'https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param=%s,day,,,320,qfq',
    'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,320,qfq',
]

def fetch(code):
    for attempt in range(len(ENDPOINTS)):
        url = ENDPOINTS[attempt] % code
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://gu.qq.com/'})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=15).read())
            node = d.get('data', {}).get(code, {})
            k = node.get('qfqday') or node.get('day')
            if k and len(k) > 25:
                with open(os.path.join(OUT, code + '.json'), 'w') as fp:
                    json.dump(k, fp)
                return code, len(k)
            return code, 0
        except Exception:
            time.sleep(1 + attempt)
    return code, -1

ok = fail = 0
t0 = time.time()
with ThreadPoolExecutor(max_workers=24) as ex:
    futs = {ex.submit(fetch, c): c for c in todo}
    for i, f in enumerate(as_completed(futs), 1):
        code, n = f.result()
        if n > 0: ok += 1
        else: fail += 1
        if i % 200 == 0:
            print('  %d/%d done (%.0fs)' % (i, len(todo), time.time() - t0), flush=True)

# ---- 新鲜度统计：ok 只代表「HTTP 成功且 bar 数>25」，停牌股会带着旧 bar 返回 ----
# 以全市场最后交易日的众数为基准日，最后 bar 不等于基准日的记为 stale（停牌/退市整理/接口残留）
from collections import Counter
last_dates = Counter()
file_last = {}
for c in uni:
    p = os.path.join(OUT, c + '.json')
    if not os.path.exists(p):
        continue
    try:
        k = json.load(open(p))
        d = k[-1][0]
    except Exception:
        continue
    file_last[c] = d
    last_dates[d] += 1
mode_date = last_dates.most_common(1)[0][0] if last_dates else None
stale = sorted(c for c, d in file_last.items() if d != mode_date)
manifest = {
    'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    'mode_date': mode_date,
    'universe': len(uni),
    'files': len(file_last),
    'ok': ok, 'fail': fail,
    'stale': stale,
    'date_counts': dict(last_dates.most_common(10)),
}
json.dump(manifest, open(os.path.expanduser('~/WorkBuddy/Claw/backtest/klines_manifest.json'), 'w'),
          ensure_ascii=False, indent=1)
if stale:
    print('  stale(最后bar非基准日，视为停牌): %s' % ', '.join(stale[:20]), flush=True)
print('fetch done: ok=%d fail=%d elapsed=%.0fs stale=%d date=%s' %
      (ok, fail, time.time() - t0, len(stale), mode_date), flush=True)
