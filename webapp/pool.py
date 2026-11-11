#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
观察池：信号日收盘入池 → 每日跟踪 → 「破EMA50三日不收回 / 持有3日到期」退出。
退出口径（用户规则·与沪深300池/持仓一致）：收盘跌破EMA50当天算第1日，
连续3个交易日不收回才退出；破EMA12不再作为退出信号。
积累的是真实前向（out-of-sample）收益数据，用于持续验证和改进策略参数。
"""
import json, os

POOL_PATH = os.path.expanduser('~/WorkBuddy/Claw/webapp/pool.json')
MAX_HOLD = 3          # 回测结论：信号有效期1~3天
STRATS = ('pre', 'dk', 'never', 'dc', 'm89', 'cross')
SNAME = {'pre': '爆发前潜伏', 'dk': '登康式启动', 'never': '从未破线', 'dc': '双穿阳线',
         'm89': '89金叉启动', 'cross': '穿线阳'}

def is_st(name):
    """ST/*ST/退市股：不入池、已在池的剔除"""
    n = (name or '').upper().replace(' ', '').replace('　', '')
    return ('ST' in n) or ('退' in (name or ''))

def load():
    if os.path.exists(POOL_PATH):
        return json.load(open(POOL_PATH))
    return {'next_id': 1, 'open': [], 'closed': []}

def save(p):
    json.dump(p, open(POOL_PATH, 'w'), ensure_ascii=False, indent=1)

def update(D, sig_fns):
    """D: server.Data 实例；sig_fns: {strategy: fn(code, kk, t)->bool}"""
    p = load()
    data_date = getattr(D, 'px_date', None) or (D.idx_days[-1] if D.idx_days else None)
    reg = D.regime.get(data_date, 'down')
    added, exited = [], []

    # 1) 更新池内条目：现价、浮动盈亏、退出判定
    for e in list(p['open']):
        code = e['code']
        if code not in D.k or is_st(e.get('name')):
            p['open'].remove(e)   # 退市/被实施ST：直接移出在跟，不计入平仓统计
            continue
        kk = D.k[code]
        t = D.lc(code)
        if kk['dates'][t] != data_date:
            continue   # 停牌/最后 bar 非基准交易日：无当日定价，冻结持仓（不更新盈亏、不判平仓）
        ti = kk['dates'].index(e['entry_date']) if e['entry_date'] in kk['dates'] else t
        e['last_date'] = kk['dates'][t]
        e['last_price'] = kk['c'][t]
        e['pnl'] = round((kk['c'][t] / e['entry_price'] - 1) * 100, 2)
        e['days'] = t - ti
        if kk['dates'][t] == e['entry_date']:
            continue  # 入池当日不判定退出
        # 破EMA50三日不收回（含跌破当天，从入池日起算）
        b50 = 0
        j = t
        while j >= ti and kk['c'][j] < kk['e50'][j]:
            b50 += 1
            j -= 1
        if b50 >= 3:
            e.update(exit_date=kk['dates'][t], exit_price=kk['c'][t],
                     ret=e['pnl'], reason='破EMA50三日不收回')
        elif e['days'] >= MAX_HOLD:
            e.update(exit_date=kk['dates'][t], exit_price=kk['c'][t],
                     ret=e['pnl'], reason='持有%d日到期' % MAX_HOLD)
        else:
            continue
        p['open'].remove(e)
        p['closed'].append(e)
        exited.append(e)

    # 2) 最新收盘bar的新信号入池（同code同策略不重复入池）
    for code, kk in D.k.items():
        t = D.lc(code)
        if t < 65 or kk['dates'][t] != data_date:
            continue
        if is_st(D.meta.get(code, {}).get('name')):
            continue   # ST/*ST/退市：不产生入池信号
        for s in STRATS:
            if not sig_fns[s](code, kk, t):
                continue
            if any(o['code'] == code and o['strategy'] == s for o in p['open']):
                continue
            e = {'id': p['next_id'], 'code': code,
                 'name': D.meta.get(code, {}).get('name', ''),
                 'strategy': s, 'entry_date': data_date,
                 'entry_price': kk['c'][t], 'regime': reg,
                 # 亏损股标记（PE_TTM<=0）：用于单独统计放开亏损股后的前向表现
                 'loss': not ((D.meta.get(code, {}).get('pe') or 0) > 0)}
            p['next_id'] += 1
            p['open'].append(e)
            added.append(e)

    save(p)
    return {'data_date': data_date, 'regime': reg, 'added': added,
            'exited': exited, 'open': p['open'], 'closed': p['closed']}

def stats(p):
    """各策略前向统计（已平仓），并单列亏损股子集"""
    out = {}
    for s in STRATS:
        cl = [e for e in p['closed'] if e['strategy'] == s]
        rets = [e['ret'] for e in cl]
        lo = [e for e in cl if e.get('loss')]
        lr = [e['ret'] for e in lo]
        out[s] = {
            'open': sum(1 for e in p['open'] if e['strategy'] == s),
            'closed': len(cl),
            'win': round(100 * sum(1 for r in rets if r > 0) / len(rets), 1) if rets else None,
            'avg_ret': round(sum(rets) / len(rets), 2) if rets else None,
            'avg_days': round(sum(e['days'] for e in cl) / len(cl), 1) if cl else None,
            'loss': {
                'open': sum(1 for e in p['open'] if e['strategy'] == s and e.get('loss')),
                'closed': len(lo),
                'win': round(100 * sum(1 for r in lr if r > 0) / len(lr), 1) if lr else None,
                'avg_ret': round(sum(lr) / len(lr), 2) if lr else None,
            },
        }
    return out
