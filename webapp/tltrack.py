#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
穿三线 · 全量持续跟踪（≥30 交易日 / 约1个月，最长60交易日）+ 回测数据集。

每一个「三线穿阳」信号（date + code）都是一个独立跟踪单元：
  - 从 signal_date 次日起逐日记录收盘、累计收益、等权小盘基准、超额、EMA50 破位状态；
  - 至少跟踪 30 个交易日才算 matured，最长留 60 个交易日的完整路径；
  - 每日由 daily_run.py 收盘跑批调用 update()，写当日快照（不可变留痕）；
  - 首次运行会用本地 320 根日K 回填历史信号与过去 30 个交易日的每日快照（标注 backfill），
    之后每个交易日新增的快照标注 live。

产物（webapp/tltrack/）：
  signals.json   全量信号主档（特征 + 前向收益 + 近90日路径 + 状态）
  signals.csv    扁平回测表（每行一个信号，可直接 Excel/Python 回测）
  stats.json     分周期 / 分桶 / 分日的前向统计
  daily/*.json   每日快照：当天所有仍在跟踪窗口内的信号逐日盯市
  meta.json      首次运行日（区分 backfill / live）

用法：python3 tltrack.py  或  import tltrack; tltrack.update(D)
"""
import csv, datetime, glob, json, os, statistics, sys

WEB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WEB)
import server as _srv
import tlpool

DIR = os.path.join(WEB, 'tltrack')
DAILY = os.path.join(DIR, 'daily')
MASTER = os.path.join(DIR, 'signals.json')
CSV = os.path.join(DIR, 'signals.csv')
STATS = os.path.join(DIR, 'stats.json')
META = os.path.join(DIR, 'meta.json')
POOL_PATH = os.path.join(WEB, 'tlpool.json')

MIN_TRACK = 30      # 至少跟踪 30 个交易日（≈1个自然月+）
MAX_TRACK = 60      # 最长保留 60 个交易日路径（≈3个月）
SNAP_DAYS = 30      # 每次运行生成/覆盖最近 30 个交易日的每日快照
HOR = (1, 3, 5, 10, 20, 30)


def _j(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def _pct(a, b):
    return round((a / b - 1) * 100, 2) if b else None


def _mean(a):
    return round(statistics.mean(a), 2) if a else None


def _win(a):
    return round(sum(1 for x in a if x > 0) / len(a) * 100, 1) if a else None


def _bench_cum(idx_rets, i, h):
    """基准（等权小盘指数）自 i 日起 h 个交易日累计收益%。"""
    if i is None or i + h >= len(idx_rets):
        return None
    b = 1.0
    for r in idx_rets[i + 1:i + h + 1]:
        b *= 1 + r
    return round((b - 1) * 100, 2)


def scan(D, live_from):
    """扫描全市场全历史上所有三线穿阳信号（t>=95，ST/退市剔除），并计算前向路径。"""
    d2i = {d: i for i, d in enumerate(D.idx_days)}
    out = []
    for code, kk in D.k.items():
        name = D.meta.get(code, {}).get('name', '')
        if _srv.is_st(name):
            continue
        dates, cl = kk['dates'], kk['c']
        last = len(cl) - 1
        for t in range(95, last + 1):   # 含最后一根bar：当日新信号也要入档（track_days=0，次日起盯市）
            if _srv.cross_lines(kk, t) != 3:
                continue
            d = dates[t]
            f = tlpool.signal_features(D, code, t)
            i = d2i.get(d)
            regime = D.regime.get(d, 'down')
            tech, tb = tlpool.tech_score(f, regime)
            evs, ev_hits = tlpool.event_match(f, d)
            fwd, bench, exc = {}, {}, {}
            for h in HOR:
                if t + h <= last:
                    fwd['d%d' % h] = _pct(cl[t + h], cl[t])
                b = _bench_cum(D.idx_rets, i, h)
                if b is not None:
                    bench['b%d' % h] = b
                    if fwd.get('d%d' % h) is not None:
                        exc['e%d' % h] = round(fwd['d%d' % h] - b, 2)
            n = min(MAX_TRACK, last - t)
            path, b50, exit_info = [], 0, None
            for j in range(1, n + 1):
                p = cl[t + j]
                bp = _bench_cum(D.idx_rets, i, j)
                if p < kk['e50'][t + j]:
                    b50 += 1
                else:
                    b50 = 0
                if b50 == 3 and exit_info is None:
                    exit_info = {'date': dates[t + j], 'days': j,
                                 'price': round(p, 2), 'ret': _pct(p, cl[t]),
                                 'reason': '破EMA50三日不收回'}
                path.append({'date': dates[t + j], 'close': round(p, 2),
                             'pnl': _pct(p, cl[t]), 'bench': bp,
                             'excess': round(_pct(p, cl[t]) - bp, 2) if bp is not None else None,
                             'age': j, 'below50': b50})
            pnls = [x['pnl'] for x in path if x['pnl'] is not None]
            track_days = n
            min30 = D.idx_days[min(i + MIN_TRACK, len(D.idx_days) - 1)] if i is not None else None
            rec = {
                'id': d + '|' + code, 'date': d, 'code': code, 'name': name,
                'ind': f['ind'], 'source': 'live' if d >= live_from else 'backfill',
                'close': f['close'], 'chg': f['chg'], 'vr': f['vr'],
                'body': f['body'], 'upsh': f['upsh'], 'close_pos': f['close_pos'],
                'brk20': f['brk20'], 'dist_hi60': f['dist_hi60'], 'plat20': f['plat20'],
                'stack': f['stack'], 'ma89_slope': f['ma89_slope'],
                'dev12': f['dev12'], 'c5': f['c5'], 'c20': f['c20'], 'c60': f['c60'],
                'mv': f['mv'], 'pe': f['pe'], 'loss': f['loss'],
                'limit_up': f['limit_up'], 'one_word': f['one_word'],
                'zone_lo': f['zone_lo'], 'zone_hi': f['zone_hi'],
                'tech': tech, 'event_lib': round(evs, 1),
                'event_hits': [h['tag'] for h in ev_hits],
                'concepts': f['concepts'], 'regime': regime,
                'fwd': fwd, 'bench': bench, 'excess': exc,
                'max_up': max(pnls) if pnls else None,
                'max_down': min(pnls) if pnls else None,
                'exit50': exit_info,
                'track_days': track_days, 'min30_date': min30,
                'status': 'matured' if track_days >= MIN_TRACK else 'tracking',
                'path': path if d >= D.idx_days[max(0, len(D.idx_days) - 90)] else [],
            }
            out.append(rec)
    out.sort(key=lambda r: (r['date'], r['code']))
    return out


def _pool_picks():
    p = _j(POOL_PATH, {})
    s = set()
    for h in (p.get('history') or []):
        for e in (h.get('pool') or []):
            s.add((h.get('date'), e.get('code')))
    for e in (p.get('pool') or []):
        s.add((p.get('as_of'), e.get('code')))
    return s


def write_csv(sigs, picks):
    cols = [
        'date', 'code', 'name', 'ind', 'regime', 'source', 'status', 'track_days',
        'close', 'chg', 'vr', 'body', 'upsh', 'close_pos', 'brk20', 'dist_hi60',
        'plat20', 'stack', 'ma89_slope', 'dev12', 'c5', 'c20', 'c60', 'mv', 'pe',
        'loss', 'limit_up', 'one_word', 'zone_lo', 'zone_hi', 'tech', 'event_lib',
        'event_hits', 'in_tlpool', 'max_up', 'max_down',
        'exit50_date', 'exit50_days', 'exit50_ret',
    ] + ['fwd%d' % h for h in HOR] + ['bench%d' % h for h in HOR] + ['excess%d' % h for h in HOR]
    with open(CSV, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in sigs:
            row = {k: r.get(k) for k in cols}
            row['event_hits'] = '/'.join(r.get('event_hits') or [])
            row['in_tlpool'] = 1 if (r['date'], r['code']) in picks else 0
            ex = r.get('exit50') or {}
            row['exit50_date'] = ex.get('date')
            row['exit50_days'] = ex.get('days')
            row['exit50_ret'] = ex.get('ret')
            for h in HOR:
                row['fwd%d' % h] = r['fwd'].get('d%d' % h)
                row['bench%d' % h] = r['bench'].get('b%d' % h)
                row['excess%d' % h] = r['excess'].get('e%d' % h)
            for k in ('brk20', 'stack', 'loss', 'limit_up', 'one_word'):
                row[k] = 1 if row[k] else 0
            w.writerow(row)
    return cols


def _agg(rows, h):
    a = [r['fwd'].get('d%d' % h) for r in rows if r['fwd'].get('d%d' % h) is not None]
    b = [r['bench'].get('b%d' % h) for r in rows if r['bench'].get('b%d' % h) is not None]
    e = [r['excess'].get('e%d' % h) for r in rows if r['excess'].get('e%d' % h) is not None]
    return {'n': len(a), 'avg': _mean(a), 'median': round(statistics.median(a), 2) if a else None,
            'win': _win(a), 'bench_avg': _mean(b), 'excess': _mean(e)}


def make_stats(sigs, as_of, live_from, picks, recent_dates):
    st = {'as_of': as_of, 'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
          'live_from': live_from, 'min_track_days': MIN_TRACK, 'max_track_days': MAX_TRACK,
          'n_signals': len(sigs),
          'n_matured': sum(1 for r in sigs if r['status'] == 'matured'),
          'n_tracking': sum(1 for r in sigs if r['status'] == 'tracking'),
          'n_live': sum(1 for r in sigs if r['source'] == 'live'),
          'horizons': {}, 'buckets': {}, 'recent_days': [],
          'notes': [
              '每一个三线穿阳信号独立跟踪 ≥30 个交易日（约1个自然月）才标记 matured，路径最长保留60个交易日。',
              '历史部分由本地320根前复权日K回填（source=backfill），不是当时实盘盯市；live_from 之后为每日跑批实测快照。',
              '基准为等权小盘指数（市值<=100亿口径，与现有回测一致）；excess=个股前向收益-基准。',
              '实盘卖出统一口径（2026-11-05）：A级「信号日/次日买入 → 持 2~3 日了结（胜率 55~56%），最多 5 日」；'
              'EMA50 破位天数仅作跟踪参考（破EMA50三日长持回测最差，不推荐）。',
          ]}
    for h in HOR:
        st['horizons']['d%d' % h] = _agg(sigs, h)

    def bucket(name, fn):
        rows = [r for r in sigs if fn(r)]
        st['buckets'][name] = {'n': len(rows), 'd5': _agg(rows, 5),
                               'd10': _agg(rows, 10), 'd20': _agg(rows, 20)}

    bucket('破20日高', lambda r: r['brk20'])
    bucket('未破20日高', lambda r: not r['brk20'])
    bucket('温和放量 vr1.0~2.2', lambda r: (r['vr'] or 0) and 1.0 <= r['vr'] <= 2.2)
    bucket('巨量 vr>3.0', lambda r: (r['vr'] or 0) > 3.0)
    bucket('缩量 vr<1.0', lambda r: 0 < (r['vr'] or 0) < 1.0)
    bucket('三线多头排列 12>50>89', lambda r: r['stack'])
    bucket('MA89上行', lambda r: (r['ma89_slope'] or -99) > 0)
    bucket('涨停信号', lambda r: r['limit_up'])
    bucket('亏损股 PE<=0', lambda r: r['loss'])
    bucket('盈利股 PE>0', lambda r: not r['loss'])
    bucket('事件库命中', lambda r: r['event_lib'] > 0)
    bucket('regime上行', lambda r: r['regime'] == 'up')
    bucket('regime下行', lambda r: r['regime'] == 'down')
    bucket('三线池Top5', lambda r: (r['date'], r['code']) in picks)
    ts = sorted(r['tech'] for r in sigs)
    cut = ts[int(len(ts) * 0.8)] if ts else 999
    bucket('技术分Top20%% (>=%.1f)' % cut, lambda r: r['tech'] >= cut)

    # 组合条件（历史前向期望，给三线池评分权重与分级提供依据）
    def combo(name, fn):
        rows = [r for r in sigs if fn(r)]
        st.setdefault('combos', {})[name] = {
            'n': len(rows), 'd5': _agg(rows, 5), 'd10': _agg(rows, 10),
            'd20': _agg(rows, 20)}
    up89 = lambda r: (r['ma89_slope'] or -99) > 0
    combo('破20日高+MA89上行', lambda r: r['brk20'] and up89(r))
    combo('破20日高+MA89仍下行', lambda r: r['brk20'] and not up89(r))
    combo('regime上行+破20高+MA89上行',
          lambda r: r['regime'] == 'up' and r['brk20'] and up89(r))
    combo('regime下行+破20日高', lambda r: r['regime'] == 'down' and r['brk20'])
    combo('regime下行+破20高+MA89上行',
          lambda r: r['regime'] == 'down' and r['brk20'] and up89(r))
    combo('regime下行+破20高+MA89下行',
          lambda r: r['regime'] == 'down' and r['brk20'] and not up89(r))

    bydate = {}
    recent_set = set(recent_dates)
    for r in sigs:
        if r['date'] in recent_set:
            bydate.setdefault(r['date'], []).append(r)
    for d in sorted(bydate)[-20:]:
        a = _agg(bydate[d], 5)
        st['recent_days'].append({'date': d, 'n': len(bydate[d]),
                                 'avg_d5': a['avg'], 'win_d5': a['win'],
                                 'excess_d5': a['excess']})
    return st


def write_daily(sigs, dates, live_from, as_of):
    """每日快照：当天所有仍处于跟踪窗口（age<=60）的信号逐日盯市，留痕不可变。"""
    os.makedirs(DAILY, exist_ok=True)
    by = {}
    for r in sigs:
        for p in (r.get('path') or []):
            if p['date'] in dates:
                by.setdefault(p['date'], []).append({
                    'id': r['id'], 'code': r['code'], 'name': r['name'],
                    'signal_date': r['date'], 'age': p['age'],
                    'close': p['close'], 'pnl': p['pnl'],
                    'bench': p['bench'], 'excess': p['excess'],
                    'below50': p['below50'],
                    'status': r['status'], 'source': r['source'],
                    'brk20': r['brk20'], 'tech': r['tech'],
                })
    written = []
    for d in dates:
        fp = os.path.join(DAILY, d + '.json')
        # 快照留痕：已存在的日期不再覆写（首次落盘即为当时的点估计，避免后续复权/
        # 数据修订把历史盯市记录改掉）。新交易日会产生新文件，天然形成连续跟踪链。
        if os.path.exists(fp):
            written.append(d)
            continue
        marks = sorted(by.get(d, []), key=lambda x: (-x['tech'], x['code']))
        payload = {'date': d, 'as_of': as_of,
                   'source': 'live' if d >= live_from else 'backfill(历史K线重建)',
                   'n': len(marks),
                   'matured_n': sum(1 for x in marks if x['status'] == 'matured'),
                   'avg_pnl': _mean([x['pnl'] for x in marks if x['pnl'] is not None]),
                   'win': _win([x['pnl'] for x in marks if x['pnl'] is not None]),
                   'marks': marks}
        with open(fp, 'w') as fh:
            json.dump(payload, fh, ensure_ascii=False)
        written.append(d)
    return written


def update(D, write=True):
    os.makedirs(DIR, exist_ok=True)
    os.makedirs(DAILY, exist_ok=True)
    meta = _j(META, {})
    today = datetime.date.today().isoformat()
    if not meta.get('live_from'):
        meta = {'live_from': today, 'created_at': datetime.datetime.now().isoformat(timespec='seconds'),
                'min_track_days': MIN_TRACK, 'max_track_days': MAX_TRACK}
        if write:
            with open(META, 'w') as fh:
                json.dump(meta, fh, ensure_ascii=False)
    live_from = meta['live_from']
    as_of = D.px_date
    sigs = scan(D, live_from)
    picks = _pool_picks()
    snap_dates = [d for d in D.idx_days[-SNAP_DAYS:] if d <= as_of]
    if write:
        with open(MASTER, 'w') as fh:
            json.dump({'as_of': as_of, 'live_from': live_from,
                       'min_track_days': MIN_TRACK, 'max_track_days': MAX_TRACK,
                       'n_signals': len(sigs), 'signals': sigs}, fh, ensure_ascii=False)
        write_csv(sigs, picks)
        st = make_stats(sigs, as_of, live_from, picks, snap_dates)
        with open(STATS, 'w') as fh:
            json.dump(st, fh, ensure_ascii=False)
        written = write_daily(sigs, snap_dates, live_from, as_of)
    else:
        st, written = {}, []
    return {'as_of': as_of, 'live_from': live_from, 'n_signals': len(sigs),
            'n_matured': st.get('n_matured'), 'n_tracking': st.get('n_tracking'),
            'snapshots': len(written), 'stats': st,
            'd5': st.get('horizons', {}).get('d5')}


if __name__ == '__main__':
    D = _srv.Data()
    r = update(D)
    print(json.dumps({k: v for k, v in r.items() if k != 'stats'}, ensure_ascii=False))
    h = r['stats']['horizons']
    for k in h:
        x = h[k]
        print('%s n=%d avg=%s%% win=%s%% bench=%s%% excess=%s%%' %
              (k, x['n'], x['avg'], x['win'], x['bench_avg'], x['excess']))
    print('--- buckets (d5) ---')
    for k, v in r['stats']['buckets'].items():
        print('%-32s n=%-5d avg5=%-8s win5=%-7s exc5=%s' %
              (k, v['n'], v['d5']['avg'], v['d5']['win'], v['d5']['excess']))
