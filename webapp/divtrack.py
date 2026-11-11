#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
背离金叉 · 全量持续跟踪（≥30 交易日 / 约1个月，最长60交易日）+ 回测数据集。

每一个「背离金叉」信号（cross_date + code）都是一个独立跟踪单元：
  - 信号定义：EMA12 上穿 MA89 的「交叉事件当日」，且当日满足背离金叉形态
    （MA89 浅降/刚勾 ∈(-6%,+0.6%)、EXPMA12/50 上行、价格未过度透支）；
  - 从交叉日起逐日记录收盘、累计收益、等权小盘基准、超额、EMA50 破位状态；
  - 至少跟踪 30 个交易日才算 matured，最长留 60 个交易日的完整路径；
  - 每日由 daily_run.py 收盘跑批调用 update()，写当日快照（不可变留痕）；
  - 首次运行用本地 320 根日K 回填历史信号与过去 30 个交易日的每日快照（backfill）。

与 tltrack（穿三线）并列，供「背离金叉 lead」策略回测。
用户 2026-11-05 口径：所有满足形态的候选都进池子，不按「距交叉>3日」剔除，
统一由本模块持续观察 ≥30 日（页面 p10 的 fresh3 仅作高亮标记，不参与筛选）。

产物（webapp/divtrack/）：
  signals.json   全量信号主档（特征 + 前向收益 + 近90日路径 + 状态）
  signals.csv    扁平回测表（每行一个信号）
  stats.json     分周期 / 分桶的前向统计
  daily/*.json   每日快照：当天所有仍在跟踪窗口内的信号逐日盯市
  meta.json      首次运行日（区分 backfill / live）

用法：python3 divtrack.py  或  import divtrack; divtrack.update(D)
"""
import csv, datetime, json, os, statistics, sys

WEB = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WEB)
import server as _srv
import tlpool

DIR = os.path.join(WEB, 'divtrack')
DAILY = os.path.join(DIR, 'daily')
MASTER = os.path.join(DIR, 'signals.json')
CSV = os.path.join(DIR, 'signals.csv')
STATS = os.path.join(DIR, 'stats.json')
META = os.path.join(DIR, 'meta.json')

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
    """扫描全市场全历史上所有「背离金叉交叉事件」并计算前向路径。

    信号 = EMA12 上穿 MA89 的交叉事件当日，且 divergence_state 在该日 signal=True。
    仅在交叉事件当日记录一条信号（避免交叉后连续多日 signal=True 产生重复）。
    """
    d2i = {d: i for i, d in enumerate(D.idx_days)}
    out = []
    for code, kk in D.k.items():
        name = D.meta.get(code, {}).get('name', '')
        if _srv.is_st(name):
            continue
        dates, cl, e12a, m89a = kk['dates'], kk['c'], kk['e12'], kk['ma89']
        last = len(cl) - 1
        for i in range(96, last + 1):   # 交叉事件需 t>=95 且 t-1 存在
            # EMA12 上穿 MA89 的交叉事件
            if not (e12a[i] > m89a[i] and e12a[i - 1] <= m89a[i - 1]):
                continue
            d = tlpool.divergence_state(kk, i)
            if not d['signal']:
                continue
            cd = dates[i]
            regime_for_pts = D.regime.get(cd, 'down')
            pts = tlpool.divergence_points(d, regime_for_pts)
            pts_old = tlpool.divergence_points_old(d, regime_for_pts)
            i_idx = d2i.get(cd)
            regime = D.regime.get(cd, 'down')
            fwd, bench, exc = {}, {}, {}
            for h in HOR:
                if i + h <= last:
                    fwd['d%d' % h] = _pct(cl[i + h], cl[i])
                b = _bench_cum(D.idx_rets, i_idx, h)
                if b is not None:
                    bench['b%d' % h] = b
                    if fwd.get('d%d' % h) is not None:
                        exc['e%d' % h] = round(fwd['d%d' % h] - b, 2)
            n = min(MAX_TRACK, last - i)
            path, b50, exit_info = [], 0, None
            for j in range(1, n + 1):
                p = cl[i + j]
                bp = _bench_cum(D.idx_rets, i_idx, j)
                if p < kk['e50'][i + j]:
                    b50 += 1
                else:
                    b50 = 0
                if b50 == 3 and exit_info is None:
                    exit_info = {'date': dates[i + j], 'days': j,
                                 'price': round(p, 2), 'ret': _pct(p, cl[i]),
                                 'reason': '破EMA50三日不收回'}
                path.append({'date': dates[i + j], 'close': round(p, 2),
                             'pnl': _pct(p, cl[i]), 'bench': bp,
                             'excess': round(_pct(p, cl[i]) - bp, 2) if bp is not None else None,
                             'age': j, 'below50': b50})
            pnls = [x['pnl'] for x in path if x['pnl'] is not None]
            track_days = n
            min30 = D.idx_days[min(i_idx + MIN_TRACK, len(D.idx_days) - 1)] if i_idx is not None else None
            rec = {
                'id': cd + '|' + code, 'cross_date': cd, 'code': code, 'name': name,
                'source': 'live' if cd >= live_from else 'backfill',
                'close': round(cl[i], 2), 's89': d['s89'], 's12': d['s12'], 's50': d['s50'],
                'c20': d['c20'], 'cross50': d['cross50'],
                'days_since_cross': d['days_since_cross'],
                'pts': pts, 'pts_old': pts_old,
                'ma89_not_crashing': d['ma89_not_crashing'],
                'ma89_lead_zone': d['ma89_lead_zone'],
                'regime': regime,
                'fwd': fwd, 'bench': bench, 'excess': exc,
                'max_up': max(pnls) if pnls else None,
                'max_down': min(pnls) if pnls else None,
                'exit50': exit_info,
                'track_days': track_days, 'min30_date': min30,
                'status': 'matured' if track_days >= MIN_TRACK else 'tracking',
                'path': path if cd >= D.idx_days[max(0, len(D.idx_days) - 90)] else [],
            }
            out.append(rec)
    out.sort(key=lambda r: (r['cross_date'], r['code']))
    return out


def write_csv(sigs):
    cols = [
        'cross_date', 'code', 'name', 'regime', 'source', 'status', 'track_days',
        'close', 's89', 's12', 's50', 'c20', 'cross50',
        'ma89_not_crashing', 'ma89_lead_zone', 'max_up', 'max_down',
        'exit50_date', 'exit50_days', 'exit50_ret',
    ] + ['fwd%d' % h for h in HOR] + ['bench%d' % h for h in HOR] + ['excess%d' % h for h in HOR]
    with open(CSV, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in sigs:
            row = {k: r.get(k) for k in cols}
            ex = r.get('exit50') or {}
            row['exit50_date'] = ex.get('date')
            row['exit50_days'] = ex.get('days')
            row['exit50_ret'] = ex.get('ret')
            for h in HOR:
                row['fwd%d' % h] = r['fwd'].get('d%d' % h)
                row['bench%d' % h] = r['bench'].get('b%d' % h)
                row['excess%d' % h] = r['excess'].get('e%d' % h)
            for k in ('ma89_not_crashing', 'ma89_lead_zone'):
                row[k] = 1 if row[k] else 0
            w.writerow(row)
    return cols


def _agg(rows, h):
    a = [r['fwd'].get('d%d' % h) for r in rows if r['fwd'].get('d%d' % h) is not None]
    b = [r['bench'].get('b%d' % h) for r in rows if r['bench'].get('b%d' % h) is not None]
    e = [r['excess'].get('e%d' % h) for r in rows if r['excess'].get('e%d' % h) is not None]
    return {'n': len(a), 'avg': _mean(a), 'median': round(statistics.median(a), 2) if a else None,
            'win': _win(a), 'bench_avg': _mean(b), 'excess': _mean(e)}


def make_stats(sigs, as_of, live_from):
    st = {'as_of': as_of, 'generated_at': datetime.datetime.now().isoformat(timespec='seconds'),
          'live_from': live_from, 'min_track_days': MIN_TRACK, 'max_track_days': MAX_TRACK,
          'n_signals': len(sigs),
          'n_matured': sum(1 for r in sigs if r['status'] == 'matured'),
          'n_tracking': sum(1 for r in sigs if r['status'] == 'tracking'),
          'n_live': sum(1 for r in sigs if r['source'] == 'live'),
          'horizons': {}, 'buckets': {},
          'notes': [
              '每一个背离金叉交叉事件独立跟踪 ≥30 个交易日（约1个自然月）才标记 matured，路径最长保留60个交易日。',
              '历史部分由本地320根前复权日K回填（source=backfill），不是当时实盘盯市；live_from 之后为每日跑批实测快照。',
              '信号 = EMA12 上穿 MA89 当日且形态成立（MA89浅降/刚勾、EXPMA12/50上行、未过度透支），每事件仅记一条。',
              '基准为等权小盘指数（市值<=100亿口径，与现有回测一致）；excess=个股前向收益-基准。',
              '卖出统一口径：收盘跌破 EMA50 且连续3个交易日未收回（含跌破当日）才算破位。',
          ]}
    for h in HOR:
        st['horizons']['d%d' % h] = _agg(sigs, h)

    def bucket(name, fn):
        rows = [r for r in sigs if fn(r)]
        st['buckets'][name] = {'n': len(rows), 'd5': _agg(rows, 5),
                               'd10': _agg(rows, 10), 'd20': _agg(rows, 20)}

    bucket('全部', lambda r: True)
    bucket('MA89浅降(-6%~0)', lambda r: -6 < (r['s89'] or 99) < 0)
    bucket('MA89刚勾(0~0.6%)', lambda r: 0 <= (r['s89'] or -99) < 0.6)
    bucket('EMA50已上穿', lambda r: r['cross50'])
    bucket('EMA50未上穿', lambda r: not r['cross50'])
    bucket('位置健康 c20<=15', lambda r: (r['c20'] or 99) <= 15)
    bucket('位置偏高 c20>25', lambda r: (r['c20'] or 0) > 25)
    bucket('regime上行', lambda r: r['regime'] == 'up')
    bucket('regime下行', lambda r: r['regime'] == 'down')

    # ---- 旧权重 vs 新权重：按分数挑前 N 名，前向收益对照（验证权重调整是否改善选股）----
    def top_by_score(key, ns):
        rows = [r for r in sigs if r.get(key) is not None and r['status'] == 'matured']
        rows.sort(key=lambda r: -r[key])
        out = {}
        for n in ns:
            sel = rows[:n]
            out['top%d' % n] = {
                'n': len(sel), 'score_min': sel[-1][key] if sel else None,
                'd5': _agg(sel, 5), 'd10': _agg(sel, 10), 'd20': _agg(sel, 20),
                'n_just_turned': sum(1 for r in sel if 0 <= (r['s89'] or -99) < 0.6),
            }
        return out

    st['score_backtest'] = {
        'as_of': as_of,
        'note': '对全历史信号分别按「旧权重分(pts_old)」与「新权重分(pts)」降序取前 N，'
                '比较其前向收益；新权重把最高分集中到 MA89刚勾(0~0.6%) 区，应使前 N 选股转正。',
        'old': top_by_score('pts_old', [50, 100, 200, 500]),
        'new': top_by_score('pts', [50, 100, 200, 500]),
    }
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
                    'cross_date': r['cross_date'], 'age': p['age'],
                    'close': p['close'], 'pnl': p['pnl'],
                    'bench': p['bench'], 'excess': p['excess'],
                    'below50': p['below50'],
                    'status': r['status'], 'source': r['source'],
                    's89': r['s89'], 'c20': r['c20'],
                })
    written = []
    for d in dates:
        fp = os.path.join(DAILY, d + '.json')
        if os.path.exists(fp):
            written.append(d)
            continue
        marks = sorted(by.get(d, []), key=lambda x: (-(x['pnl'] or 0), x['code']))
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
    snap_dates = [d for d in D.idx_days[-SNAP_DAYS:] if d <= as_of]
    if write:
        with open(MASTER, 'w') as fh:
            json.dump({'as_of': as_of, 'live_from': live_from,
                       'min_track_days': MIN_TRACK, 'max_track_days': MAX_TRACK,
                       'n_signals': len(sigs), 'signals': sigs}, fh, ensure_ascii=False)
        write_csv(sigs)
        st = make_stats(sigs, as_of, live_from)
        with open(STATS, 'w') as fh:
            json.dump(st, fh, ensure_ascii=False)
        written = write_daily(sigs, snap_dates, live_from, as_of)
    else:
        st, written = {}, []
    return {'as_of': as_of, 'live_from': live_from, 'n_signals': len(sigs),
            'n_matured': st.get('n_matured') if st else None,
            'n_tracking': st.get('n_tracking') if st else None,
            'snapshots': len(written) if written else 0, 'stats': st,
            'd5': st.get('horizons', {}).get('d5') if st else None}


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
        print('%-28s n=%-5d avg5=%-8s win5=%-7s exc5=%s' %
              (k, v['n'], v['d5']['avg'], v['d5']['win'], v['d5']['excess']))
