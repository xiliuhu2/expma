#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基础数据体检（Data Quality Check）—— 每日跑批前/后的数据闸门

八项断言：
  1 coverage   名单 vs 日K缓存 覆盖是否完整
  2 freshness  全市场最后 bar 是否落在最新交易日（停牌识别）+ 文件是否今日刷新
  3 structure  重复日期 / 乱序 / OHLC 逻辑 / 零成交 / 非正价格
  4 limit      收益率越界（超出板块涨跌幅板宽 = 脏 bar 或未复权）
  5 xsource    抽样与东方财富前复权收盘交叉校验
  6 regime     独立复算 mom20 与工作台 /api/regime 是否一致
  7 pit        point-in-time 市值快照是否已归档（回测用当日市值、防未来信息）
  8 st         ST/*ST/退市股是否混进日K缓存（策略层已过滤，缓存层再加一道闸）

用法：
  python3 dq_check.py              # 全量（含联网交叉校验 + API 比对）
  python3 dq_check.py --offline    # 跳过联网项（check5）
  python3 dq_check.py --quiet      # 只输出 JSON

退出码：0 = ok/warn；2 = error（跑批应中止，不生成日报）
"""
import argparse, collections, glob, json, os, random, statistics, sys, time
from urllib.request import Request, urlopen
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = _REPO
KL = os.path.join(BASE, 'backtest', 'klines')
IDX = os.path.join(BASE, 'backtest', 'indices')
SNAP = os.path.join(BASE, 'backtest', 'snapshot.json')
MANI = os.path.join(BASE, 'backtest', 'klines_manifest.json')
HIST = os.path.join(BASE, 'backtest', 'history')
API = 'http://127.0.0.1:8765'

CHECKS = []


def add(name, level, detail, **kw):
    CHECKS.append(dict(name=name, level=level, detail=detail, **kw))


def limit_ratio(code, name=''):
    if 'ST' in (name or '').upper():
        return 0.05
    if code.startswith('bj'):
        return 0.30
    if code.startswith('sz30') or code.startswith('sh68'):
        return 0.20
    return 0.10


def load_universe():
    snap = json.load(open(SNAP))
    items = snap if isinstance(snap, list) else snap.get('data', {}).get('items', [])
    meta = {}
    for r in items:
        meta[r['code']] = dict(name=r.get('name', ''), mv=float(r.get('TotalMV') or 0),
                               pe=float(r.get('PE_TTM') or 0))
    def _is_st(name):
        n = (name or '').upper().replace(' ', '').replace('　', '')
        return ('ST' in n) or ('退' in (name or ''))

    uni = [c for c, m in meta.items()
           if m['mv'] > 0 and not _is_st(m['name'])]
    return meta, uni


def index_date():
    p = os.path.join(IDX, 'sh000001.json')
    if not os.path.exists(p):
        return None
    return json.load(open(p))[-1][0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()
    t0 = time.time()

    meta, uni = load_universe()
    files = {os.path.basename(p)[:-5]: p for p in glob.glob(KL + '/*.json')}

    # ---------- 1 coverage ----------
    miss = [c for c in uni if c not in files]
    extra = [c for c in files if c not in set(uni)]
    lvl = 'error' if len(miss) > max(5, 0.005 * len(uni)) else ('warn' if miss else 'ok')
    add('coverage', lvl, '名单 %d 只 / 缓存 %d 只 / 缺失 %d / 名单外 %d'
        % (len(uni), len(files), len(miss), len(extra)),
        missing=miss[:20], extra=extra[:10])

    # ---------- 1.5 st ----------
    st_cached = [c for c in files
                 if ('ST' in (meta.get(c, {}).get('name', '') or '').upper().replace(' ', '').replace('　', '')
                     or '退' in (meta.get(c, {}).get('name', '') or ''))]
    add('st', 'error' if st_cached else 'ok',
        'ST/退市股混入缓存 %d 只（策略层已剔除）' % len(st_cached), st=st_cached[:20])

    # ---------- 2 freshness ----------
    idx_d = index_date()
    last = {}
    mtimes = {}
    for c, p in files.items():
        try:
            k = json.load(open(p))
            last[c] = k[-1][0]
            mtimes[c] = os.path.getmtime(p)
        except Exception:
            last[c] = None
    cnt = collections.Counter(d for d in last.values() if d)
    mode_date = cnt.most_common(1)[0][0] if cnt else None
    stale = sorted(c for c, d in last.items() if d and d != idx_d)
    today0 = time.mktime(time.strptime(time.strftime('%Y-%m-%d'), '%Y-%m-%d'))
    not_today = sum(1 for t in mtimes.values() if t < today0)
    lvl = 'ok'
    if idx_d and mode_date and idx_d != mode_date:
        lvl = 'error'
    elif stale and len(stale) > max(10, 0.01 * len(files)):
        lvl = 'error'
    elif not_today:
        lvl = 'warn'   # 缓存未在今日重刷（增量模式遗留）
    add('freshness', lvl,
        '指数日历最新 %s / 个股众数 %s / 停牌(stale) %d / 非今日刷新 %d'
        % (idx_d, mode_date, len(stale), not_today),
        stale=[(c, meta.get(c, {}).get('name', ''), last[c]) for c in stale[:20]])

    # ---------- 3 structure ----------
    dup = unsorted = ohlc = zerov = negp = short = 0
    ohlc_s, short_s = [], []
    for c, p in files.items():
        try:
            k = json.load(open(p))
        except Exception:
            continue
        ds = [r[0] for r in k]
        if len(set(ds)) != len(ds):
            dup += 1
        if ds != sorted(ds):
            unsorted += 1
        if len(k) < 60:
            short += 1
            short_s.append((c, meta.get(c, {}).get('name', ''), len(k)))
        for r in k:
            try:
                o, cl, h, l, v = float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0)
            except Exception:
                ohlc += 1
                continue
            if not (h >= max(o, cl) - 1e-6 and l <= min(o, cl) + 1e-6 and h >= l):
                ohlc += 1
                if len(ohlc_s) < 10:
                    ohlc_s.append([c, r[0], o, cl, h, l])
            if v == 0:
                zerov += 1
            if min(o, cl, h, l) <= 0:
                negp += 1
    lvl = 'error' if (dup or unsorted or ohlc or negp) else ('warn' if short > max(10, 0.01 * len(files)) else 'ok')
    add('structure', lvl,
        '重复日期 %d / 乱序 %d / OHLC违规 %d / 零成交 %d / 非正价 %d / 样本<60根 %d'
        % (dup, unsorted, ohlc, zerov, negp, short),
        ohlc_samples=ohlc_s, short_samples=short_s[:10])

    # ---------- 4 limit ----------
    ext = []
    for c, p in files.items():
        try:
            k = json.load(open(p))
        except Exception:
            continue
        lim = limit_ratio(c, meta.get(c, {}).get('name', ''))
        ipo = len(k) < 320
        for t in range(1, len(k)):
            try:
                prev, cur = float(k[t - 1][2]), float(k[t][2])
            except Exception:
                continue
            if prev <= 0:
                continue
            r = cur / prev - 1
            if abs(r) > lim + 0.03 and not (ipo and t <= 5):
                ext.append([c, meta.get(c, {}).get('name', ''), k[t][0], round(r * 100, 2)])
    lvl = 'error' if len(ext) > 60 else ('warn' if ext else 'ok')
    add('limit', lvl, '越界收益 %d 条（已排除上市前5日）' % len(ext),
        samples=sorted(ext, key=lambda x: -abs(x[3]))[:10])

    # ---------- 5 xsource ----------
    if args.offline:
        add('xsource', 'skip', '已跳过（--offline）')
    else:
        def em(code):
            mkt = '1' if code.startswith('sh') else ('0' if code.startswith('sz') else '0')
            url = ('https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s.%s'
                   '&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57'
                   '&klt=101&fqt=1&beg=20260801&end=20500101') % (mkt, code[2:])
            d = json.loads(urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}),
                                   timeout=15).read())
            out = {}
            for x in (d['data']['klines'] if d.get('data') else []):
                q = x.split(',')
                out[q[0]] = float(q[2])
            return out

        random.seed(int(time.strftime('%Y%m%d')))
        pool = [c for c in files if last.get(c) == idx_d]
        sample = random.sample(pool, min(12, len(pool)))
        bad, n, err = [], 0, 0
        for c in sample:
            try:
                ref = em(c)
            except Exception:
                err += 1
                continue
            local = {r[0]: float(r[2]) for r in json.load(open(files[c]))}
            for d in sorted(set(local) & set(ref))[-3:]:
                n += 1
                if ref[d] > 0 and abs(local[d] - ref[d]) / ref[d] * 100 > 0.5:
                    bad.append([c, d, local[d], ref[d]])
        if n == 0:
            lvl = 'warn'
        else:
            lvl = 'error' if len(bad) > 2 else ('warn' if bad else 'ok')
        add('xsource', lvl, '抽样 %d 只 / 比对 %d 根 bar / 偏差>0.5%% %d 条 / 接口失败 %d'
            % (len(sample), n, len(bad), err), samples=bad[:8])

    # ---------- 6 regime ----------
    daily = collections.defaultdict(list)
    for c, p in files.items():
        if meta.get(c, {}).get('mv', 0) > 100:
            continue
        try:
            k = json.load(open(p))
        except Exception:
            continue
        lim = limit_ratio(c, meta.get(c, {}).get('name', ''))
        ipo = len(k) < 320
        for t in range(1, len(k)):
            try:
                if float(k[t][5]) <= 0 or float(k[t - 1][2]) <= 0:
                    continue
                if ipo and t <= 5:
                    continue
                r = float(k[t][2]) / float(k[t - 1][2]) - 1
            except Exception:
                continue
            if abs(r) > lim + 0.03:
                continue
            daily[k[t][0]].append(r)
    days = sorted(d for d in daily if len(daily[d]) >= 200)
    mom = None
    if len(days) >= 21:
        m = 1.0
        for d in days[-20:]:
            m *= 1 + statistics.mean(daily[d])
        mom = round((m - 1) * 100, 2)
    api_mom = None
    try:
        reg = json.loads(urlopen(API + '/api/regime', timeout=30).read())
        api_mom = reg.get('mom20')
    except Exception as e:
        reg = {'error': str(e)}
    if mom is None:
        add('regime', 'error', '本地无法复算 mom20（有效交易日不足）')
    elif api_mom is None:
        add('regime', 'warn', '独立复算 mom20=%.2f%%，工作台不可达或未返回' % mom)
    else:
        d = abs(mom - api_mom)
        lvl = 'error' if d > 0.05 else 'ok'
        add('regime', lvl, '独立复算 %.2f%% vs 工作台 %.2f%%（偏差 %.2fpp，成分日均 %d 只）'
            % (mom, api_mom, d, int(statistics.mean([len(daily[x]) for x in days[-20:]]))),
            px_date=reg.get('px_date'), current=reg.get('current'))

    # ---------- 7 pit ----------
    os.makedirs(HIST, exist_ok=True)
    arch = os.path.join(HIST, 'mv_%s.json' % idx_d) if idx_d else None
    if arch and not os.path.exists(arch):
        # 归档当日市值/PE 快照（point-in-time）：回测按「当日市值」划成分，消除未来信息偏差
        json.dump({c: {'name': m['name'], 'mv': m['mv'], 'pe': m['pe']} for c, m in meta.items()},
                  open(arch, 'w'), ensure_ascii=False)
        add('pit', 'ok', '已归档当日市值快照 %s（%d 只）' % (os.path.basename(arch), len(meta)))
    elif arch:
        add('pit', 'ok', '当日市值快照已存在 %s' % os.path.basename(arch))
    else:
        add('pit', 'warn', '缺少指数日历，无法确定快照日期')

    # ---------- 汇总 ----------
    lv = [c['level'] for c in CHECKS]
    status = 'error' if 'error' in lv else ('warn' if 'warn' in lv else 'ok')
    out = dict(status=status, px_date=idx_d, mode_date=mode_date, stale=len(stale),
               universe=len(uni), files=len(files), elapsed=round(time.time() - t0, 1),
               checks=CHECKS)
    if not args.quiet:
        icon = {'ok': '✅', 'warn': '⚠️ ', 'error': '❌', 'skip': '⏭ '}
        for c in CHECKS:
            print('%s %-10s %s' % (icon.get(c['level'], '  '), c['name'], c['detail']))
        print('--- status=%s (%ss) ---' % (status, out['elapsed']))
    print('DQJSON ' + json.dumps(out, ensure_ascii=False))
    return 2 if status == 'error' else 0


if __name__ == '__main__':
    sys.exit(main())
