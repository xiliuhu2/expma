#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日收盘跑批：更新K线 → 重启策略工作台 → 三策略筛选 + regime → 生成日报
用法：python3 daily_run.py
stdout 输出 JSON 摘要（供自动化/人工读取）
"""
import datetime, json, os, subprocess, sys, time, urllib.request

BASE = os.path.expanduser('~/WorkBuddy/Claw')
PORT = 8765
URL = 'http://127.0.0.1:%d' % PORT
DAILY_DIR = os.path.join(BASE, 'daily')
os.makedirs(DAILY_DIR, exist_ok=True)

# 筛选市值上限：99999 = 全市场口径（snapshot.json 5010只，去除ST/退市/停牌）。
# 注意：四策略回测样本为小盘（<=100亿），大市值命中未经回测验证，仅供观察。
MV_MAX = 99999

out = {'status': 'ok', 'steps': {}}

def fail(step, msg):
    out['status'] = 'error'
    out['steps'][step] = msg
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(1)

# 离线模式：SKIP_FETCH=1 跳过所有联网抓取/重启/DQ闸门，直接用现有缓存与运行中的工作台
# 重算信号、三线池、跟踪与报告（用于盘中复盘或修复报告，不影响收盘自动化）
SKIP_FETCH = os.environ.get('SKIP_FETCH') == '1'
if SKIP_FETCH:
    out['steps']['offline'] = 'SKIP_FETCH=1: 使用现有K线缓存，跳过抓取/重启/DQ'
else:
    # 公共数据「当日一次性」守卫：目前有两个 WorkBuddy 定时任务（15:15 / 17:15）
    # 都会执行本脚本。若不去重，指数/债券/行业/概念/沪深300/名单快照/5009只日K/
    # 盘口异动会在同一天被整批重复联网拉两遍。守卫只记元数据，不改任何主档，
    # 失败不记成功（下次跑批自动重试），FORCE_FETCH=1 可人工强制重抓。
    sys.path.insert(0, BASE + '/webapp')
    from data_fetch_state import once as _once

    def _step(key, argv, timeout, label=None, fatal=False):
        """联网抓取包装 → (did_fetch, rc, line, err)；今日已抓则完全不联网。"""
        lbl = label or key

        def _run():
            r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            line = ((r.stdout.strip().splitlines() or [''])[-1]
                    if r.returncode == 0 else (r.stderr or '')[-200:])
            return r.returncode == 0, {'rc': r.returncode, 'line': line,
                                       'err': (r.stderr or '')[-500:]}
        st, info = _once(key, _run)
        if st == 'skip':
            out['steps'][lbl] = 'fresh(今日已抓，跳过) · ' + str(info)[:120]
            return False, 0, out['steps'][lbl], ''
        if st == 'failed' and not isinstance(info, dict):
            out['steps'][lbl] = 'error: ' + str(info)[:200]
            return True, 1, '', str(info)
        rc, line, err = info['rc'], info['line'], info['err']
        out['steps'][lbl] = line if rc == 0 else ('error: ' + err[-200:])
        if fatal and rc != 0:
            fail(lbl, err)
        return True, rc, line, err

    # 0) 刷新指数日K（全量历史：上证1990年起/沪深300自2005年，腾讯分段源，增量模式只补今年）
    _step('indices', [sys.executable, BASE + '/backtest/fetch_indices.py'], 600)
    # 0.05) 北证50补缺（东财源，本机易限频；快速模式3次重试，失败不影响其它指数）
    _step('bj50_patch', [sys.executable, BASE + '/backtest/fetch_bj50_patch.py', '--quick'], 120)
    _step('bond', [sys.executable, BASE + '/backtest/fetch_bond.py'], 300)
    # 0.5) 更新行业分类（东方财富 f100，涨停复盘主线聚合用；行业归属极少变动）
    _step('industry', [sys.executable, BASE + '/backtest/fetch_industry.py'], 300)
    # 0.6) 概念板块映射（归属变动慢，超5天才刷新；外层 once 再挡同日重复拉）
    _con_path = BASE + '/webapp/concepts.json'
    _stale = True
    try:
        import datetime as _dt
        _age = (_dt.date.today() - _dt.date.fromisoformat(json.load(open(_con_path))['fetched'])).days
        _stale = _age >= 5
    except Exception:
        pass
    if _stale:
        _step('concepts', [sys.executable, BASE + '/backtest/fetch_concepts.py'], 600)
    else:
        out['steps']['concepts'] = 'fresh(≤5天跳过)'
    # 0.7) 沪深300成份股主档（脚本自身超7天才联网刷新，外层 once 双保险）
    _step('hs300', [sys.executable, BASE + '/backtest/fetch_hs300.py'], 120)
    # 0.8) 全市场名单快照基石：收盘后刷新、每天最多一次、校验不过保留旧档。
    #      必须排在 fetch_klines 之前（K线抓取拿它当 universe）。
    _step('snapshot', [sys.executable, BASE + '/backtest/fetch_snapshot.py'], 180)

    # 1) 更新K线缓存（--refresh 全量重抓，纳入最新交易日）；失败必须中止跑批
    _step('klines', [sys.executable, BASE + '/backtest/fetch_klines.py', '--refresh'],
          900, label='fetch', fatal=True)

    # 2) 重启工作台（载入新数据）
    subprocess.run("lsof -ti tcp:%d | xargs kill -9 2>/dev/null; true" % PORT,
                   shell=True, capture_output=True)
    srv = subprocess.Popen([sys.executable, BASE + '/webapp/server.py', str(PORT)],
                           stdout=open(BASE + '/webapp/server.log', 'a'),
                           stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(90):
        try:
            urllib.request.urlopen(URL + '/api/regime', timeout=5)
            break
        except Exception:
            time.sleep(2)
    else:
        fail('server', '工作台启动超时，日志: ' + open(BASE + '/webapp/server.log').read()[-500:])

    # 2.5) 重启到价提醒守护进程（盘中轮询腾讯实时行情，到价弹 macOS 通知）
    subprocess.run("pkill -f 'alertd.py' 2>/dev/null; true", shell=True, capture_output=True)
    try:
        subprocess.Popen([sys.executable, BASE + '/webapp/alertd.py'],
                         stdout=open(BASE + '/webapp/alertd.log', 'a'),
                         stderr=subprocess.STDOUT, start_new_session=True)
        out['steps']['alertd'] = 'restarted'
    except Exception as e:
        out['steps']['alertd'] = str(e)

    # 2.5b) 重启三线穿阳·破20日高 盘中提醒守护（预触发口径，见 tlbreak_alert.py）
    subprocess.run("pkill -f 'tlbreak_alert.py' 2>/dev/null; true", shell=True, capture_output=True)
    try:
        subprocess.Popen([sys.executable, BASE + '/webapp/tlbreak_alert.py'],
                         stdout=open(BASE + '/webapp/alertd.log', 'a'),
                         stderr=subprocess.STDOUT, start_new_session=True)
        out['steps']['tlbreak_alert'] = 'restarted'
    except Exception as e:
        out['steps']['tlbreak_alert'] = str(e)

    # 2.5c) 重启 80%胜率规则 买卖弹窗守护（开盘买 / +2.5%止盈 / 20日到期，见 win80_alert.py）
    subprocess.run("pkill -f 'win80_alert.py' 2>/dev/null; true", shell=True, capture_output=True)
    try:
        subprocess.Popen([sys.executable, BASE + '/webapp/win80_alert.py'],
                         stdout=open(BASE + '/webapp/alertd.log', 'a'),
                         stderr=subprocess.STDOUT, start_new_session=True)
        out['steps']['win80_alert'] = 'restarted'
    except Exception as e:
        out['steps']['win80_alert'] = str(e)

    # 2.6) 基础数据体检（DQ 闸门）：覆盖/新鲜度/结构/涨跌幅越界/跨源抽样/派生指标一致
    #      不通过则中止，绝不把脏数据写进观察池与日报
    rd = subprocess.run([sys.executable, BASE + '/backtest/dq_check.py', '--quiet'],
                        capture_output=True, text=True, timeout=600)
    _dq = None
    for _ln in rd.stdout.splitlines():
        if _ln.startswith('DQJSON '):
            try:
                _dq = json.loads(_ln[7:])
            except Exception:
                _dq = None
    if _dq is None:
        fail('dq', '数据体检无输出: ' + ((rd.stderr or rd.stdout or '')[-400:]))
    out['dq'] = {'status': _dq['status'], 'stale': _dq.get('stale'),
                 'checks': {c['name']: c['level'] for c in _dq['checks']}}
    if _dq['status'] == 'error':
        _bad = ['%s: %s' % (c['name'], c['detail']) for c in _dq['checks'] if c['level'] == 'error']
        fail('dq', '数据体检未通过 → ' + ' | '.join(_bad)[:450])

    # 2.8) 收盘抓当日盘口异动全量（东财免费源只有当日，必须每天落档才能积累可回测样本）
    #      收盘口径：仅在 15:05 之后抓；脚本自身也有守卫，双保险，盘中绝不落半日快照
    _now = datetime.datetime.now()
    if (_now.hour, _now.minute) < (15, 5):
        out['steps']['anomaly_fetch'] = 'skip: 未到收盘 15:05，按收盘口径不抓盘中快照'
    else:
        # 外层 once 守卫：两个定时任务同一天只抓一次盘口异动（免费源当日只有一份）
        _step('anomaly', [sys.executable, BASE + '/backtest/fetch_anomaly.py'],
              180, label='anomaly_fetch')


# 2.9) 异动前瞻扫描：纯本地日K重算，不联网、不取实时行情，故离线复盘也允许重跑。
#      结果始终以「最近一个已收盘交易日」为基准日，盘中重算也不会变成实时数据。
#      页面与日报均标注非实时；盘中不做任何轮询与推送。
try:
    rs = subprocess.run([sys.executable, BASE + '/webapp/anomaly_scan.py'],
                        capture_output=True, text=True, timeout=300)
    out['steps']['anomaly_scan'] = ((rs.stdout.strip().splitlines() or [''])[-1]
                                    if rs.returncode == 0 else (rs.stderr or '')[-200:])
except Exception as e:
    out['steps']['anomaly_scan'] = 'skip: ' + str(e)



# 3) 拉取 regime + 三策略结果
def get(path):
    return json.load(urllib.request.urlopen(URL + path, timeout=180))

try:
    regime = get('/api/regime')
    res = {s: get('/api/screen?strategy=%s&mvmax=%d' % (s, MV_MAX))
           for s in ('pre', 'dk', 'never', 'dc', 'm89', 'cross')}
    indices = get('/api/indices')
    bond = get('/api/bond_stock')
    zt = get('/api/zt?days=20')
    emo = get('/api/emotion?days=30')
    picks = get('/api/picks')
    hs = get('/api/hs300')
    out['bond'] = {k: bond.get(k) for k in ('anchor', 'anchor_sse', 'sse_now', 'bond_now', 'ratio', 'bond_date')} \
        if 'error' not in bond else {'error': bond['error']}
    out['indices'] = [{'name': d['name'], 'close': d['close'], 'chg': d['chg'],
                       'above12': d['above12'], 'date': d['date'],
                       'last_cross_up': d['cross_up'][-1] if d['cross_up'] else None} for d in indices]
except Exception as e:
    fail('api', str(e))

cur = regime['current']
out['date'] = res['pre']['date'] or res['dk']['date'] or res['never']['date']
out['regime'] = {'current': cur, 'mom20': regime['mom20'], 'mkt': regime.get('mkt')}
out['counts'] = {s: res[s]['count'] for s in res}
out['top'] = {s: [{'code': r['code'], 'name': r['name'], 'c20': r['c20'], 'chg': r['chg'],
                   'lines': r.get('lines')}
                  for r in res[s]['rows'][:5]] for s in res}

# 3.5) 观察池更新（信号入池 → 前向跟踪 → 平仓积累真实收益数据）
try:
    sys.path.insert(0, BASE + '/webapp')
    import server as _srv, pool as _pool
    D2 = _srv.Data()
    meta = D2.meta
    sig_fns = {
        'pre': lambda code, kk, t: _srv.sig_pre(kk, t) and meta.get(code, {}).get('mv', MV_MAX + 1) <= MV_MAX and _srv.pe_ok(meta.get(code, {})) and not _srv.is_st(meta.get(code, {}).get('name')),
        'dk': lambda code, kk, t: _srv.sig_dk(kk, t) and meta.get(code, {}).get('mv', MV_MAX + 1) <= MV_MAX and _srv.pe_ok(meta.get(code, {})) and not _srv.is_st(meta.get(code, {}).get('name')),
        'never': lambda code, kk, t: _srv.sig_never(kk, t) and meta.get(code, {}).get('mv', MV_MAX + 1) <= MV_MAX and _srv.pe_ok(meta.get(code, {})) and not _srv.is_st(meta.get(code, {}).get('name')),
        'dc': lambda code, kk, t: _srv.sig_dc(kk, t) and meta.get(code, {}).get('mv', MV_MAX + 1) <= MV_MAX and _srv.pe_ok(meta.get(code, {})) and not _srv.is_st(meta.get(code, {}).get('name')),
        'm89': lambda code, kk, t: _srv.sig_m89(kk, t) and meta.get(code, {}).get('mv', MV_MAX + 1) <= MV_MAX and _srv.pe_ok(meta.get(code, {})) and not _srv.is_st(meta.get(code, {}).get('name')),
        'cross': lambda code, kk, t: _srv.sig_cross(kk, t) and meta.get(code, {}).get('mv', MV_MAX + 1) <= MV_MAX and _srv.pe_ok(meta.get(code, {})) and not _srv.is_st(meta.get(code, {}).get('name')),
    }
    pr = _pool.update(D2, sig_fns)
    pl_stats = _pool.stats(_pool.load())
    # 「今日新入池」按 entry_date 统计（幂等：同一天重复跑批不会把已入池的算漏）
    _today = [e for e in pr['open'] if e.get('entry_date') == pr['data_date']]
    out['pool'] = {
        'added': len(_today),
        'exited': len(pr['exited']),
        'open_by_strategy': {s: pl_stats[s]['open'] for s in _pool.STRATS},
        'added_list': [{'code': e['code'], 'name': e['name'], 'strategy': _pool.SNAME[e['strategy']]} for e in _today],
        'exited_list': [{'code': e['code'], 'name': e['name'], 'ret': e['ret'], 'reason': e['reason']} for e in pr['exited']],
        'stats': pl_stats,
    }
    # D2 延后释放：3.6 三线池/全量跟踪复用同一份已加载的 K线数据（避免重复读5000个文件）
except Exception as e:
    out['pool'] = {'error': str(e)}

# 3.6) 穿三线（用户操作重点）：全量持续跟踪 ≥30交易日 + 当日 Top5「三线池」
#      顺序：先 tltrack（回填/刷新前向统计）→ 再 tlpool（评分依据取最新 evidence）
try:
    import server as _srv2
    if 'D2' not in globals():
        D2 = _srv2.Data()   # 3.5 失败时的兜底，保证三线池/跟踪不被连带跳过
    import tltrack as _trk, tlpool as _tlp
    tr = _trk.update(D2)
    tp = _tlp.build(D2, ctx={'regime': regime, 'indices': indices,
                             'zt': zt, 'emotion': emo})
    out['tltrack'] = {
        'as_of': tr['as_of'], 'live_from': tr['live_from'],
        'n_signals': tr['n_signals'], 'matured': tr['n_matured'],
        'tracking': tr['n_tracking'], 'snapshots': tr['snapshots'],
        'd5': tr.get('d5'),
    }
    out['tlpool'] = {
        'as_of': tp['as_of'], 'n_candidates': tp['n_candidates'],
        'n_confirmed': tp['n_confirmed'], 'n_A': sum(1 for x in tp['pool'] if x['grade'] == 'A'),
        'top5': [{'code': x['code'], 'name': x['name'], 'score': x['score'],
                  'grade': x['grade'], 'pullback': x['entry']['pullback'],
                  'trigger': x['entry']['trigger'], 'hard_stop': x['entry']['hard_stop']}
                 for x in tp['pool']],
    }
    del tr, tp
except Exception as e:
    out['tltrack'] = {'error': str(e)}
    out['tlpool'] = {'error': str(e)}

# 3.6.5) 背离金叉 lead：当前全量候选（不剔除，进池）+ 全历史 ≥30日持续跟踪（回测）
#        与 3.6 复用同一份 D2，避免重复读取 5000 个 K线文件
try:
    if 'D2' not in globals():
        import server as _srv4
        D2 = _srv4.Data()
    import tlpool as _tlp2, divtrack as _dvt
    dc = _tlp2.screen_divcross(D2, D2.px_date, cur, topn=400)
    with open(BASE + '/webapp/divcross.json', 'w') as _fh:
        json.dump(dc, _fh, ensure_ascii=False)
    dv = _dvt.update(D2)
    out['divcross'] = {
        'as_of': dc['as_of'], 'regime': dc['regime'], 'n': dc['n'], 'n_fresh3': dc['n_fresh3'],
    }
    out['divtrack'] = {
        'as_of': dv['as_of'], 'n_signals': dv['n_signals'],
        'matured': dv['n_matured'], 'tracking': dv['n_tracking'],
        'd5': dv.get('d5'),
    }
    del dc, dv
except Exception as e:
    out['divcross'] = {'error': str(e)}
    out['divtrack'] = {'error': str(e)}

# 3.7) 龙头战法：全历史盘面重建 + 次日可成交口径前向验证 + 当日龙头池分级 + 持续跟踪
#     与 3.6 复用同一份 D2，避免重复读取 5000 个 K线文件
try:
    if 'D2' not in globals():
        import server as _srv3
        D2 = _srv3.Data()
    import dragon as _dg
    dr = _dg.update(D2)
    out['dragon'] = {
        'as_of': dr['as_of'], 'n_signals': dr['n_signals'],
        'overall_d5': dr['overall_d5'], 'overall_exc': dr['overall_exc'],
        'n_A': dr['n_A'], 'n_B': dr['n_B'], 'n_D': dr['n_D'],
        'n_candidates': dr['n_candidates'], 'n_pool': dr['n_pool'],
        'leader': dr['leader'], 'phase': dr['phase'],
        'tracking': dr['tracking'], 'matured': dr['matured'],
        'reversed_factors': dr['reversed_factors'],
    }
    del dr
except Exception as e:
    out['dragon'] = {'error': str(e)}
# 3.8) 80%胜率规则当日清单：三线穿阳+A级 → 再放量破信号日高（+2.5%/20日）
#      复用 3.6/3.7 的同一份 D2（避免重读 5000 个K线文件）；结果供日报 ⑪ 段渲染
w80 = None
try:
    if 'D2' not in globals():
        import server as _srv5
        D2 = _srv5.Data()
    sys.path.insert(0, BASE + '/backtest')
    import cross3_80win_today as _w80
    w80 = _w80.collect(D2)
    # 完整清单落盘：工作台 p13「80%胜率」tab 经 /api/win80 读取（与 dragon.json 同模式）
    try:
        with open(BASE + '/webapp/win80.json', 'w') as _f:
            json.dump(dict(date=w80['date'], window=w80['window'], hold=w80['hold'],
                           scan_window=w80['scan_window'], tp=w80['tp'],
                           idx_gate=w80.get('idx_gate'),
                           pending=w80['pending'], confirmed=w80['confirmed'],
                           generated_at=datetime.datetime.now().strftime('%Y-%m-%d %H:%M')),
                      _f, ensure_ascii=False, indent=1)
    except Exception as _e:
        out['win80_json_error'] = str(_e)
    out['win80'] = {
        'date': w80['date'],
        'n_pending': len(w80['pending']),
        'n_confirmed': len(w80['confirmed']),
        'n_tp': sum(1 for x in w80['confirmed'] if x['done'] == '已止盈'),
        'idx_gate': w80.get('idx_gate'),
        'pending': [{'code': x['code'], 'name': x['name'], 'need': x['need'],
                     'note': x['note'], 'idx_ok': x.get('idx_ok'),
                     'idx_why': x.get('idx_why')} for x in w80['pending'][:7]],
        'confirmed': [{'code': x['code'], 'name': x['name'], 'pnl': x['pnl'],
                       'done': x['done']} for x in w80['confirmed']],
    }
except Exception as e:
    out['win80'] = {'error': str(e)}
# 3.9) 沪深300池「高收益候选」：读回测权重，生成 webapp/hs300_profit.json 供 p7 使用
try:
    sys.path.insert(0, BASE + '/backtest')
    import hs300_profit_today as _hsp
    _h = _hsp.collect()
    with open(BASE + '/webapp/hs300_profit.json', 'w') as _f:
        json.dump(_h, _f, ensure_ascii=False, indent=1)
    out['hs300_profit'] = dict(date=_h['date'], pool_n=_h['pool_n'],
                              tradeable=_h['regime']['tradeable'],
                              n_pick=len(_h['intersect']))
except Exception as e:
    out['hs300_profit'] = {'error': str(e)}

# 3.95) 沪深300 · EXPMA12 首次上穿 EXPMA50（转势首叉）：纯本地日K，不联网
#       产出 webapp/firstcross.json（p14 数据源）+ firstcross/signals.csv + 专题报告
try:
    sys.path.insert(0, BASE + '/webapp')
    import firstcross as _fc
    _fcd = _fc.build()
    _fcr = _fc.write_report(_fcd)
    _fcs = _fc.summarize(_fcd['signals'], _fc.QUIET_DEFAULT, 5, _fcd['base_any'])
    out['firstcross'] = {
        'date': _fcd['date'], 'n_signals': _fcd['n_signals'],
        'n_today': _fcd['n_today'], 'n_today_quiet20': _fcd['today_today_gap20'],
        'today': [{'code': x['code'], 'name': x['name'], 'gap': x['gap']}
                  for x in _fcd['signals']
                  if x['date'] == _fcd['date'] and x['gap'] >= _fc.QUIET_DEFAULT],
        'd5_excess': _fcs['excess'], 'd5_fwd': _fcs['fwd'], 'n_quiet20': _fcs['n'],
        'report': _fcr,
    }
    out['firstcross_report'] = _fcr
    del _fcd, _fcs
except Exception as e:
    out['firstcross'] = {'error': str(e)}
try:
    del D2
except NameError:
    pass

# 非交易日检测：数据日期是否为「今天」且已收盘（不再用 today-4 的宽松窗口，避免假期误判为新数据）
_today = datetime.date.today().isoformat()
_now = datetime.datetime.now()
_after_close = _now.hour > 15 or (_now.hour == 15 and _now.minute >= 10)
out['is_new_data'] = bool(out['date'] == _today and _after_close)
out['non_trading_day'] = bool(out['date'] != _today)

# 4) 生成日报 markdown
if out['date']:
    ds = out['date']
    lines = ['# EXPMA 每日收盘报告 · %s' % ds, '',
             '- Regime：**%s**（等权小盘指数 20 日动量 %+.2f%%）' % (
                 '上行 ▲（可开仓）' if cur == 'up' else '下行 ▼（按回测规则应空仓观察）', regime['mom20']),
             '- 指数可操作窗口：' + '；'.join(
                 '%s %s（%+.2f%%）' % (d['name'], 'EMA12上方 ▲可操作' if d['above12'] else 'EMA12下方 ▼回避', d['chg'])
                 for d in out.get('indices', [])),
             '- 爆发前潜伏：%d 只 | 登康式启动：%d 只 | 从未破线：%d 只 | 双穿阳线：%d 只 | 89金叉启动：%d 只 | 穿线阳：%d 只（其中三线 %d 只）' % (
                 res['pre']['count'], res['dk']['count'], res['never']['count'], res['dc']['count'],
                 res['m89']['count'], res['cross']['count'], res['cross'].get('lines3', 0)),
             '- 筛选范围：沪深A股全市场（剔除ST/退市/停牌；**已放开亏损股**，PE≤0 以「亏损」标注，regime 仍按等权小盘口径与回测一致）', '']
    bd = out.get('bond') or {}
    if bd.get('ratio'):
        lines.insert(4, '- 股债长跑：上证 %.0f 点 vs 国债缩放线 %.0f 点（锚点 %s 对齐，股/债比值 %.2f，>1 为股票长期领先）'
                     % (bd['sse_now'], bd['bond_now'], bd['anchor'], bd['ratio']))
    if not out.get('is_new_data'):
        lines.insert(4, '- ⚠️ 数据日期非今日（非交易日或指数未更新），本报告为最近收盘数据，勿当当日信号执行')
    if (out.get('dq') or {}).get('stale'):
        lines.insert(5, '- 数据体检：%s（停牌/最后bar非基准日 %d 只，已从当日信号与定价中剔除）'
                     % (out['dq']['status'], out['dq']['stale']))
    for s, title in (('pre', '① 爆发前潜伏'), ('dk', '② 登康式启动'), ('never', '③ 从未破线（11日）'),
                     ('dc', '④ 双穿阳线+破20日高'), ('m89', '⑤ 89金叉启动（50上穿89·12在50上方·89走平）'),
                     ('cross', '⑥ 穿线阳（阳柱实体包住EXPMA12/50；3线=MA89也在实体内）')):
        extra = ('，其中三线穿阳 %d 只' % res[s].get('lines3', 0)) if s == 'cross' else ''
        lines.append('## %s（%d 只%s）' % (title, res[s]['count'], extra))
        hd = ['代码', '名称'] + (['穿线'] if s == 'cross' else []) + \
             ['收盘', '涨跌%', '量比', '距EMA12%', '20日%', '60日%', '市值(亿)', 'PE']
        lines.append('| ' + ' | '.join(hd) + ' |')
        lines.append('|' + '---|' * len(hd))
        for r2 in res[s]['rows'][:15]:
            _pe = r2.get('pe')
            _pe_s = ('%.0f' % _pe) if (_pe and _pe > 0) else '亏损'
            cells = [r2['code'], r2['name']]
            if s == 'cross':
                cells.append('★3线' if r2.get('lines') == 3 else '2线')
            cells += [r2['close'], '%+.2f' % r2['chg'],
                      r2['vr'] if r2['vr'] is not None else '-',
                      '%+.1f' % r2['dev12'], '%+.1f' % r2['c20'], '%+.1f' % r2['c60'],
                      '%.0f' % r2['mv'] if r2.get('mv') else '-', _pe_s]
            lines.append('| ' + ' | '.join(str(x) for x in cells) + ' |')
        lines.append('')
    lines.append('- ⑥ 穿线阳为形态观察口径：不设量比≥1.5、不要求破20日高，只看当日阳线实体是否同时包住 '
                 'EXPMA12 / EXPMA50（三线再加 MA89），因此④被量比或20日高挡掉的票（如索辰科技）会出现在这里。'
                 '三线优先排序。**离线前向检验（截至 2026-11-04，全A 15个月）显示：三线形态本身不含明显超额，'
                 '叠加「收盘破20日高」后 5 日期望才由负转正，④现行口径仍是最优**——故⑥定位为观察池，'
                 '实盘优先级以④为准；工作台提供「⑥b 三线+破20日高」可单独看强化子集。')
    lines.append('')
    # 涨停复盘：主线行业 + 可操作候选（牛股大多从涨停启动）
    if emo and emo.get('hist'):
        eh = emo['hist'][-1]
        eprev = emo['hist'][-2] if len(emo['hist']) > 1 else None
        ladder = ' | '.join('%s板×%s' % (k, eh['ladder'][k]) for k in sorted(eh['ladder'], key=lambda x: int(x)))
        lines.append('## 🌡️ 情绪周期（%s · 温度 %.1f°）' % (eh['phase'], eh['temp']))
        lines.append('- 今日：涨停 **%d** 家 · 炸板率 **%.1f%%** · 1进2晋级率 %s · 最高 %d板 · 梯队：%s' % (
            eh['zt'], eh['zb_rate'],
            ('%.0f%%' % eh['promo']) if eh.get('promo') is not None else '-',
            eh['hmax'], ladder))
        if eprev:
            lines.append('- 较昨日：涨停 %d→%d · 温度 %.1f°→%.1f°（%s→%s）· 炸板率 %.1f%%→%.1f%%' % (
                eprev['zt'], eh['zt'], eprev['temp'], eh['temp'], eprev['phase'], eh['phase'],
                eprev['zb_rate'], eh['zb_rate']))
        warns = []
        if eh['zb_rate'] >= 40:
            warns.append('炸板率≥40%（封板质量差）')
        if eh.get('promo') is not None and eh['promo'] < 15:
            warns.append('晋级率<15%（接力意愿弱）')
        if eh['temp'] < 20:
            warns.append('情绪冰点（关注修复初期首板方向）')
        if eh['temp'] >= 60:
            warns.append('情绪高潮（禁追高，警惕透支后退潮）')
        lines.append(('- ⚠️ ' + '；'.join(warns)) if warns else '- 情绪中性，按纪律正常执行')
        lines.append('')
    if zt and zt.get('total') is not None:
        lines.append('## 涨停复盘 · 20日涨停池（%d 家）' % zt['total'])
        ms = zt.get('max_streak')
        lines.append('- 今日涨停 **%d** 家（昨日 %d 家）· 市场最高连板：%s（%d板）' % (
            zt['today'], zt['yesterday'], ms['name'] if ms else '-', ms['streak'] if ms else 0))
        lines.append('- 主线行业（近5日涨停家数 Top5）：')
        lines.append('| 行业 | 近5日家数 | 20日家数 | 代表股（连板×次数） |')
        lines.append('|---|---|---|---|')
        for it in zt['industries'][:5]:
            lines.append('| %s | %d | %d | %s |' % (
                it['ind'], it['n5'], it['n20'],
                '、'.join('%s(%s×%d)' % (s['name'], ('%d板' % s['streak']) if s['streak'] > 1 else '首板', s['zt_count'])
                          for s in it['top'][:3])))
        if zt.get('concepts'):
            lines.append('- 主线概念（近5日涨停家数 Top5，题材比行业更贴近短线资金）：')
            lines.append('| 概念 | 近5日家数 | 20日家数 | 代表股 |')
            lines.append('|---|---|---|---|')
            for it in zt['concepts'][:5]:
                lines.append('| %s | %d | %d | %s |' % (
                    it['con'], it['n5'], it['n20'],
                    '、'.join('%s(×%d)' % (s['name'], s['zt_count']) for s in it['top'][:3])))
        # 精选池前向验证
        if picks:
            pf = picks.get('perf')
            if pf:
                lines.append('- 🔥 精选池本周前向验证：池均 **%+.1f%%** vs 等权基线 %+.1f%% → 超额 **%+.1fpp**' % (
                    pf['pool'], pf['bench'], pf['excess']) if pf.get('bench') is not None
                    else '- 🔥 精选池本周前向验证：池均 %+.1f%%（基线数据不足）' % pf['pool'])
            for h in (picks.get('history') or [])[-4:]:
                s = h.get('summary', {})
                lines.append('  - 上期 %s：池均 %+.1f%% vs 基线 %+.1f%%（超额 %+.1fpp，%d 只）' % (
                    h['week'], s.get('pool', 0), s.get('bench', 0), s.get('excess', 0), s.get('n', 0)))
        act = zt.get('actionable', [])
        lines.append('- 可操作候选（20日内涨停≥2次 + 站上EMA50/MA89 + 未过度透支，共 %d 只，列前15）：' % len(act))
        if act:
            lines.append('| 代码 | 名称 | 行业 | 涨停次数 | 连板 | 26年涨停 | 距涨停(日) | 20日% | 市值(亿) |')
            lines.append('|---|---|---|---|---|---|---|---|---|')
            for r2 in act[:15]:
                lines.append('| %s | %s | %s | %d | %d | %d | %d | %+.1f | %.0f |' % (
                    r2['code'], r2['name'], r2['ind'], r2['zt_count'], r2['streak'],
                    r2.get('zt_ytd', 0) or 0, r2['since'], r2['c20'], r2['mv'] or 0))
        else:
            lines.append('  今日无可操作候选（可放宽条件看 p5 全部涨停池）')
        lines.append('')
    # 沪深300 · EXPMA双线向上池（大盘趋势票：入池/剔除/每周Top5）
    if hs and hs.get('pool_n') is not None:
        lines.append('## 沪深300 · EXPMA双线向上池（%d/%d 只在池）' % (hs['pool_n'], hs['total_cons']))
        lines.append('- 口径：收盘 > EMA12 > EMA50 且双线向上（EMA12五日、EMA50十日斜率>0）；剔除=收盘低于 EMA50 连续3日不收回（贴线缓冲带内保留观察），破位参考价 EMA50')
        pev = hs.get('picks_evicted') or []
        if pev:
            lines.append('- 本周额度票剔除：%s' % '、'.join('%s(%s)' % (e['name'], e.get('why', '')) for e in pev))
        ev = hs.get('evicted') or []
        if ev:
            lines.append('- 近期剔除：%s' % '；'.join('%s(%s,%s)' % (e['name'], e['date'], e['why']) for e in ev[:5]))
        lines.append('- 🔥 本周可操作 Top5（评分：趋势30+位置25+动能25+流动性10+相对强度10）：')
        lines.append('| 代码 | 名称 | 行业 | 评分 | 选入价 | 距EMA12% | 止损 | 理由 |')
        lines.append('|---|---|---|---|---|---|---|---|')
        for e in hs['picks']['list']:
            lines.append('| %s | %s | %s | %.1f | %s | %+.1f | %.2f | %s |' % (
                e['code'], e['name'], e.get('ind', ''), e.get('score', 0),
                e.get('pick_price'), e.get('dist12') or 0, e.get('stop'), e.get('reason', '')))
        if hs.get('perf') and hs['perf'].get('bench') is not None:
            lines.append('- 本周前向验证：池均 %+.1f%% vs 基线 %+.1f%% → 超额 %+.1fpp' % (
                hs['perf']['pool'], hs['perf']['bench'], hs['perf']['excess']))
        _hp = globals().get('_h')
        if _hp:
            _s = _hp['summary']
            lines.append('- 🎯 高收益候选（回测优选，详见 p7 页签 / daily/沪深300池_高收益回测_2026-11-07.md）：'
                         '市场 %s（HS300近20日 %+.2f%%）· %s' % (
                             'UP' if _hp['regime']['tradeable'] else 'DOWN',
                             _hp['regime']['idx_c20'],
                             '可按纪律执行' if _hp['regime']['tradeable'] else '按回测应空仓/轻仓观察'))
            lines.append('  - 回测：%d 周频截面 / %d 样本；每周Top5的20日均值 池内等权 %+.2f%% / 现行评分 %+.2f%% / 高弹性 %+.2f%%；'
                         '大赢率 %.1f%% vs %.1f%%' % (
                             _s['panels'], _s['samples'], _s['pool_all'], _s['old_top5_all'],
                             _s['elastic_top5_all'], _s['elastic_bigwin'], _s['old_bigwin']))
            lines.append('  - 交集精选：%s' % '、'.join(
                '%s(%s,稳健第%d/弹性第%d/现行第%d)' % (
                    x['name'], x['code'], x['wf_rank'], x['elastic_rank'], x['old_rank'])
                for x in _hp['intersect'][:6]))
        lines.append('')
    pl = out.get('pool')
    if pl and 'error' not in pl:
        lines.append('## 观察池（前向跟踪）')
        lines.append('- 今日新入池 %d 笔：%s' % (pl['added'],
            '、'.join('%s(%s,%s)' % (e['name'], e['code'], e['strategy']) for e in pl['added_list']) or '无'))
        if pl['exited_list']:
            lines.append('- 今日退出 %d 笔：%s' % (len(pl['exited_list']),
                '、'.join('%s(%s) %+.2f%% [%s]' % (e['name'], e['code'], e['ret'], e['reason']) for e in pl['exited_list'])))
        ob = pl['open_by_strategy']
        lines.append('- 池内在跟：爆发前潜伏 %d / 登康式 %d / 从未破线 %d / 双穿阳线 %d / 89金叉启动 %d / 穿线阳 %d' % (
            ob['pre'], ob['dk'], ob['never'], ob['dc'], ob['m89'], ob.get('cross', 0)))
        st = pl['stats']
        cl_parts = []
        for s, nm in (('pre', '潜伏'), ('dk', '登康式'), ('never', '从未破线'), ('dc', '双穿阳线'),
                      ('m89', '89金叉启动'), ('cross', '穿线阳')):
            if st[s]['closed']:
                cl_parts.append('%s 平仓%d笔 胜率%s%% 均收益%s%%' % (
                    nm, st[s]['closed'], st[s]['win'], st[s]['avg_ret']))
        lines.append('- 前向统计（已平仓）：%s' % ('；'.join(cl_parts) or '暂无平仓记录，数据积累中'))
        lo_parts = []
        for s, nm in (('pre', '潜伏'), ('dk', '登康式'), ('never', '从未破线'), ('dc', '双穿阳线'),
                      ('m89', '89金叉启动'), ('cross', '穿线阳')):
            lo = (st[s].get('loss') or {})
            if lo.get('closed'):
                lo_parts.append('%s 平仓%d笔 胜率%s%% 均%s%%' % (nm, lo['closed'], lo['win'], lo['avg_ret']))
        if lo_parts:
            lines.append('- 亏损股子集（PE≤0，单列验证）：%s' % '；'.join(lo_parts))

    # ⑦ 三线池 + 全量跟踪：报告段落统一由 tl_report.py 生成（独立报告同源码，避免两份口径漂移）
    try:
        import tl_report as _tlr
        _tp = _tlr._j(BASE + '/webapp/tlpool.json')
        _ts = _tlr._j(BASE + '/webapp/tltrack/stats.json')
        _evlib = _tlr._j(BASE + '/webapp/events.json')
        lines.extend(_tlr.sections(_tp, _ts, _evlib, out, inline=True))
    except Exception as _e:
        lines.append('- 三线池/跟踪报告段生成失败：%s' % _e)
    # ⑧ 龙头战法：同源码生成日报段与独立专题报告
    try:
        import dragon_report as _dgr
        lines.extend(_dgr.sections(_dgr._j(BASE + '/webapp/dragon.json'), out, inline=True))
    except Exception as _e:
        lines.append('- 龙头战法报告段生成失败：%s' % _e)
    # ⑨ 背离金叉 lead：全量候选进池 + ≥30日跟踪（回测）
    try:
        import json as _json
        dvt = out.get('divtrack') or {}
        dcr = out.get('divcross') or {}
        if 'error' not in dvt:
            d5 = dvt.get('d5') or {}
            _st = _json.load(open(BASE + '/webapp/divtrack/stats.json')) if os.path.exists(BASE + '/webapp/divtrack/stats.json') else {}
            _bk = (_st.get('buckets') or {}).get('MA89刚勾(0~0.6%)', {})
            _bk5 = _bk.get('d5', {})
            lines.append('## ⑨ 背离金叉 lead（MA89下行+EXPMA上行+EMA12上穿MA89 · 早期位）')
            lines.append('- 当前全量候选 **%d** 只进池（**不按距交叉>3日剔除**，近3日新交叉 %d 只）→ 工作台 p10。' % (
                dvt.get('n', dcr.get('n', 0)), dcr.get('n_fresh3', 0)))
            lines.append('- 全历史跟踪信号 %d 个（成熟 %d / 跟踪中 %d）；5日均值 %s%%、胜率 %s%%、超额 %s%%（等权小盘基准）。' % (
                dvt.get('n_signals', 0), dvt.get('matured', 0), dvt.get('tracking', 0),
                d5.get('avg'), d5.get('win'), d5.get('excess')))
            if _bk5.get('avg') is not None:
                lines.append('- 回测要点：MA89「刚勾(0~0.6%%)」分桶 5日 **%s%%**、胜率 **%s%%**（正向）；仍深跌(<-6%%)判俯冲陷阱已剔除。' % (
                    _bk5.get('avg'), _bk5.get('win')))
    except Exception as _e:
        lines.append('- 背离金叉报告段生成失败：%s' % _e)
    # ⑩ 异动前瞻：量价异动 × EXPMA 分层的样本外条件概率
    try:
        _an = json.load(open(BASE + '/webapp/anomaly.json'))
        _te = (_an.get('verify') or {}).get('test') or {}
        _s, _b, _base = _te.get('S') or {}, _te.get('B') or {}, _te.get('ALL') or {}
        _pk = _an.get('picks') or []
        _cnt = {g: sum(1 for p in _pk if p.get('grade') == g) for g in ('S', 'A', 'B')}
        lines.append('## ⑩ 异动前瞻（放量突破 × MA89 分层 · 样本外验证 · 仅收盘口径）')
        lines.append('- **非实时**：只用最近已收盘交易日的本地日K，盘中不抓行情、不轮询、不推送；'
                     '本段随收盘跑批生成，下一次更新为下一交易日收盘后。')
        lines.append('- 基准日 %s：候选 **%d** 只（S档 %d / A档 %d / B档 %d）→ 工作台 p12。' % (
            _an.get('date'), _an.get('n_picks', 0), _cnt['S'], _cnt['A'], _cnt['B']))
        if _an.get('anomaly'):
            lines.append('- 盘口异动档案：%s 与基准日对齐（%s 条 / %s 只票）。' % (
                _an['anomaly'].get('date'), _an['anomaly'].get('n_rows'),
                _an['anomaly'].get('n_stocks')))
        else:
            lines.append('- 盘口异动档案：%s（不参与档位判定，仅作观察字段）。' %
                         _an.get('anomaly_status', '暂缺'))
        if _s.get('hit10') is not None and _base.get('hit10') is not None:
            lines.append('- **S档共振**样本外 %d 例：10日内最大涨>8%% 概率 **%s%%**（全市场基准 %s%%，%+.1fpp）；'
                         '5日收益均值 %s%%、5日涨>5%% 概率 %s%%。' % (
                             _s.get('n'), _s.get('hit10'), _base.get('hit10'),
                             _s['hit10'] - _base['hit10'], _s.get('d5'), _s.get('hit5')))
        if _b.get('d5') is not None:
            lines.append('- **B档（MA89仍下行）**样本外 %d 例：5日均值 **%s%%**、5日涨>5%% 概率 %s%%（基准 %s%%）—— 历史负期望档，勿追高。' % (
                _b.get('n'), _b.get('d5'), _b.get('hit5'), _base.get('hit5')))
        if _cnt['S']:
            lines.append('- S档候选（前5）：%s' % '、'.join(
                '%s(%s,放量%.1f倍,涨%+.1f%%)' % (p['name'], p['code'], p['volr'], p['ret'])
                for p in [x for x in _pk if x.get('grade') == 'S'][:5]))
        lines.append('- 口径：按交易日历切分 %s 做样本外验证（共 %s 条样本）；本模块给的是'
                     '**条件概率不是收益预测**，5日均值接近 0，放大的是波动与右尾机会。'
                     '盘口异动档案无历史、不参与档位判定，仅作观察。' % (
                         (_an.get('verify') or {}).get('split'),
                         (_an.get('verify') or {}).get('n_rows')))
    except Exception as _e:
        lines.append('- 异动前瞻报告段生成失败：%s' % _e)
    # ⑪ 80%胜率规则：三线穿阳+A级 → 再放量破信号日高（数据来自 3.8 步骤 w80）
    try:
        if w80 is None:
            raise RuntimeError('3.8 未产出数据（见 out.win80.error）')
        _pd, _cf = w80['pending'], w80['confirmed']
        lines.append('## ⑪ 80%%胜率规则 · 再放量破信号日高（基准日 %s）' % w80['date'])
        lines.append('- 规则：三线穿阳（EXPMA12/50+MA89 实体包三线）+ A级（破20日高+MA89十日斜率>0）'
                     '→ 20日内「收盘>信号日最高价 且 量比≥1.2」确认 → **确认日次日开盘买入** → '
                     '**A股 T+1：买入当日不可卖，次日起才可** +2.5% 盘中止盈 / 20日收盘时间退出 / 无个股硬止损（单票≤1/4 靠分散）。')
        lines.append('- 回测（2025-11~2026-08，含0.25%成本，**T+1 口径修正后**）：471笔 **胜率86.8%**（Wilson下界83.5%）· '
                     '均值**+0.01%** · 训练段 86.2%/**-0.13%** · 样本外 94.6%/+1.66% · 9个月逐月胜率最低**71.9%**；'
                     '⚠️ 原口径把「买入当日止盈」算作成交（违反 T+1），修正后训练段期望转负，'
                     '**高胜率≠正期望**，当前 regime 下轻仓执行；T+1 下俱乐部最优为 +4%/20日（待确认是否切换）。')
        lines.append('- 当日：A 级信号（回溯40日 = 20确认 + 20持有）共 %d 只 → **待确认 %d / 已确认持仓 %d**（其中已止盈 %d 只）。' % (
            len(_pd) + len(_cf), len(_pd), len(_cf), sum(1 for x in _cf if x['done'] == '已止盈')))
        lines.append('- **待确认**（收盘>信号日高 且 量比≥1.2 才算确认，次日开盘执行，不追高）：')
        lines.append('| 代码 | 名称 | 信号日 | 现价 | 突破确认价 | 还需涨% | 三线带下沿 | 粘合% | 状态 |')
        lines.append('|---|---|---|---|---|---|---|---|---|')
        for x in _pd:
            lines.append('| %s | %s | %s | %.2f | %.2f | %+.1f%% | %.2f | %.1f%% | %s |' % (
                x['code'], x['name'], x['sig_date'], x['close'], x['hi0'],
                x['need'], x['zlo'], x['glue'], x['note']))
        lines.append('- **已确认持仓跟踪**（T+1：买入次日起 +2.5%止盈 / 20日时间退出）：')
        lines.append('| 代码 | 名称 | 信号日 | 确认日 | 买入价 | +2.5%止盈 | 状态 | 了结/浮盈% | 持有日 | 三线带 |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|')
        for x in _cf:
            lines.append('| %s | %s | %s | %s | %.2f | %.2f | %s | %+.2f%% | %d | %s |' % (
                x['code'], x['name'], x['sig_date'], x['trig_date'], x['buy'],
                x['tp'], x['done'], x['pnl'], x['days'], x['zone']))
        lines.append('- **当日指数门（新变量因子，2026-11-07）**：%s —— %s'
                     % (('✅ 可介入' if (w80.get('idx_gate') or {}).get('ok')
                         else '❌ 不介入（提醒照发，仅观察）'),
                        (w80.get('idx_gate') or {}).get('why', '指数数据缺失')))
        _gate_w80 = w80.get('idx_gate') or {}
        lines.append('- 提醒纪律：确认日次日 9:15-9:32 **弹窗必发**——指数配合弹红色「开盘买入」、'
                     '指数不配合弹琥珀「仅观察」（回测：弱市/缩量/跳空日介入胜率 53~75%）。')
        lines.append('- 口径与回测详见 daily/三线穿阳_80胜率规则_2026-11-06.md 与 '
                     'daily/三线穿阳_介入时机指数条件回测_2026-11-07.md；'
                     '每日清单脚本 backtest/cross3_80win_today.py（CLI 可单独跑）。')
    except Exception as _e:
        lines.append('- 80%胜率规则清单生成失败：%s' % _e)
    # ⑫ 沪深300 · EXPMA12 首次上穿 EXPMA50（转势首叉）：当日清单 + 三关纪律摘要
    try:
        _fc = out.get('firstcross') or {}
        if _fc.get('error'):
            raise RuntimeError(_fc['error'])
        lines.append('## ⑫ 沪深300 · 转势首叉（EXPMA12 首次上穿 EXPMA50）')
        lines.append('- 当日首次转势 **%d 只**（冷静期≥20交易日）：%s' % (
            _fc['n_today_quiet20'],
            '、'.join('%s %s' % (x['name'], x['code']) for x in _fc['today']) or '无'))
        lines.append('- 全历史金叉 %d 次；冷静期≥20 样本 %d 条，5日前向 %+.2f%% / 同日基线超额 %+.2fpp、胜率口径见专题报告。'
                     % (_fc['n_signals'], _fc['n_quiet20'], _fc['d5_fwd'], _fc['d5_excess']))
        lines.append('- 三关之③（分年份稳健性）未过：本地日K年份样本不足且为当前成份股回溯口径，'
                     '**结论仅标描述/候选**，不构成操作建议。详见 daily/转势首叉_%s.md 与工作台「🌱 转势首叉」页签。' % _fc['date'])
    except Exception as _e:
        lines.append('- 转势首叉日报段生成失败：%s' % _e)
    lines.append('')
    lines.append('> 结果为客观数据筛选，不构成投资建议。工作台：http://127.0.0.1:%d' % PORT)
    rep = os.path.join(DAILY_DIR, '日报_%s.md' % ds)
    open(rep, 'w').write('\n'.join(lines))
    out['report'] = rep
    # 独立交付：每日另存一份「三线池与跟踪」专题报告（与日报 ⑦ 段同源码）
    try:
        import tl_report as _tlr2
        _sp = _tlr2.write_standalone()
        if _sp:
            out['tl_report'] = _sp
    except Exception as _e:
        out['tl_report'] = {'error': str(_e)}
    # 独立交付：龙头战法专题报告
    try:
        import dragon_report as _dgr2
        _dpool = _dgr2._j(BASE + '/webapp/dragon.json')
        _dp = _dgr2.write_standalone(_dpool)
        if _dp:
            out['dragon_report'] = _dp
    except Exception as _e:
        out['dragon_report'] = {'error': str(_e)}

print(json.dumps(out, ensure_ascii=False))
