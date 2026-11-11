#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""🎯 80%胜率规则 · 买卖弹窗守护（读收盘跑批 win80.json + 盘中腾讯实时行情）。

为什么需要它：p13「80%胜率」页面是收盘跑批的静态清单，alertd.py 只盯 trades.json
里的人工持仓，tlbreak_alert.py 弹的是「三线穿阳·破20日高」**预触发信号**（不是本规则的
买点）。本守护补上本规则自己的三类可执行弹窗：

  ① 买  🟢 开盘买入：win80.json 的 pending 里 note='已确认·明日开盘买'
        （确认日 T1 收盘满足 收盘>信号日最高价 且 量比≥1.2）
        → 下一交易日 9:15~9:32 弹「今日 9:30 按开盘价买入」，按开盘价成交。
        注意：确认日当日不买、不提前埋伏；待突破/价已破高·待放量 一律不弹。
  ② 卖  🔴 +2.5% 止盈：confirmed 里 done='持有中'，**买入日次一交易日起**盘中最高
        >= buy×1.025 → 弹卖出。A 股 T+1：买入当日不可卖出（回测撮合已从 bi+1 起判），
        故 buy_date==今日的票一律跳过止盈监控；只看这一帧快照，不等收盘。
  ③ 卖  🟠 20日到期平仓：持有中且 exit_date <= 今日 → 14:45~15:02 弹「收盘前平仓」。
        exit_date 是按交易日推算的预计值，遇节假日自然顺延到下一个有行情的交易日。
        当天已弹过止盈的票不再弹到期（一票一天最多一条卖出窗）。

纪律：无个股硬止损（靠单票≤1/4 分散），因此本守护**不发止损窗**。
去重：date|code|kind 写入 win80_alerts.json，永久归档 win80_alert_history.jsonl；
     重启后读档恢复，不会重复骚扰。心跳写 win80_alert_state.json 供页面看「守护在不在」。
交易日校验：拉 sh000300 实时行情时间戳，必须是今天才认为开盘，避免节假日/周末误弹。

用法：python3 win80_alert.py [--dry] [--once] [--force] [--selftest]
  --dry      只打印不弹窗/不写档   --once 跑一轮退出
  --force    跳过交易日+时段校验（用真实行情验证逻辑，周末/盘后调试用）
  --selftest 合成数据跑三类分支断言，不联网
  supervisor.sh 常驻；daily_run.py 每日收盘后随其它守护一起重启。"""
import datetime, json, os, re, subprocess, sys, time, urllib.request
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

from flashwin import flash, RED

WEB = os.path.join(_REPO, 'webapp')
DATA = os.path.join(WEB, 'win80.json')                    # 收盘跑批清单
RECS = os.path.join(WEB, 'win80_alerts.json')              # 滚动窗口（去重）
HIST = os.path.join(WEB, 'win80_alert_history.jsonl')       # 永久档案 append-only
STATEF = os.path.join(WEB, 'win80_alert_state.json')       # 心跳 / 页面可见状态
SRV = 'http://127.0.0.1:8765'
DRY = '--dry' in sys.argv
ONCE = '--once' in sys.argv
FORCE = '--force' in sys.argv
SELFTEST = '--selftest' in sys.argv
CYCLE = 60

BUY_COLOR = '#c02626'      # 买入信号：红
TP_COLOR = '#c02626'       # 止盈卖出：红
EXIT_COLOR = '#b45309'     # 到期平仓：琥珀
KIND = {
    'buy': '🟢 80%胜率·开盘买入',
    'buy_watch': '🟡 80%胜率·确认但指数不配合（仅观察）',
    'tp': '🔴 80%胜率·+2.5%止盈卖出',
    'exit': '🟠 80%胜率·20日到期平仓',
}
BUY_WATCH_COLOR = '#b45309'   # 指数不配合：琥珀（提醒照发，但明确不介入）


def log(s):
    print('%s %s' % (datetime.datetime.now().strftime('%H:%M:%S'), s))


def add_trade_days(s, n):
    """周一至周五顺延推算交易日（不含节假日；节假日靠行情时间戳兜底）。"""
    x = datetime.date.fromisoformat(s)
    while n > 0:
        x += datetime.timedelta(days=1)
        if x.weekday() < 5:
            n -= 1
    return x.isoformat()


def quotes(codes):
    """腾讯实时行情：{裸代码: {name, price, prev_close, open, high, ts}}"""
    out = {}
    if not codes:
        return out
    url = 'https://qt.gtimg.cn/q=' + ','.join(codes)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    text = urllib.request.urlopen(req, timeout=10).read().decode('gbk', 'ignore')
    for line in text.strip().split(';'):
        line = line.strip()
        if not line or '=' not in line:
            continue
        val = line.split('=', 1)[1].strip('"').split('~')
        if len(val) > 34:
            try:
                m = re.search(r'\d{14}', line)
                out[val[2]] = dict(
                    name=val[1],
                    price=float(val[3]) if val[3] else 0,
                    prev_close=float(val[4]) if val[4] else 0,
                    open=float(val[5]) if val[5] else 0,
                    high=float(val[33]) if val[33] else 0,
                    ts=(m.group(0) if m else (val[30] if len(val) > 30 else '')))
            except ValueError:
                pass
    return out


def market_today():
    """沪深300 行情时间戳是今天 → 真交易日；周末/节假日返回 False（不弹）。"""
    if FORCE:
        return True, 'force'
    try:
        q = quotes(['sh000300'])
        if not q:
            return False, '行情源无响应'
        ts = next(iter(q.values())).get('ts', '')
        ymd = datetime.date.today().strftime('%Y%m%d')
        return (ts[:8] == ymd), (ts or 'no-ts')
    except Exception as e:
        return False, str(e)[:60]


def notify(title, msg, code, color):
    if DRY:
        print('[DRY] %s | %s' % (title, msg))
        return
    url = SRV + '/?jump=%s/p13' % code
    flash(title, msg, url, color)                     # 置顶闪烁 5 秒，点击 → p13 该票
    msg = msg.replace('"', "'")
    subprocess.run(['osascript', '-e',
                    'display notification "%s" with title "%s" sound name "Glass"'
                    % (msg, title)], capture_output=True)


def load_tasks():
    """把 win80.json 变成守护要盯的任务（买：分可介入/仅观察两类，都提醒）。"""
    d = json.load(open(DATA, encoding='utf-8'))
    base = d['date']
    holds = [x for x in d.get('confirmed', []) if x.get('done') == '持有中']
    buys = [x for x in d.get('pending', []) if x.get('note') == '已确认·明日开盘买']
    return dict(base=base, generated_at=d.get('generated_at', ''),
                buy_date=add_trade_days(base, 1), buys=buys, holds=holds,
                idx_gate=d.get('idx_gate') or {},
                tp=d.get('tp', 2.5), hold=d.get('hold', 20),
                n_pending=len(d.get('pending', [])), n_confirmed=len(d.get('confirmed', [])))


def load_fired(today):
    """重启后从档案恢复当日已弹记录，避免重复弹窗。"""
    keys, codes = set(), set()
    try:
        for r in json.load(open(RECS, encoding='utf-8')):
            if r.get('date') == today:
                keys.add(r.get('key'))
                codes.add(r.get('code'))
    except Exception:
        pass
    return keys, codes


def save(records):
    """滚动去重档 + 永久归档 + 心跳由调用方写。"""
    try:
        old = json.load(open(RECS, encoding='utf-8'))
    except Exception:
        old = []
    by_key = {r.get('key'): i for i, r in enumerate(old)}
    for r in records:
        i = by_key.get(r['key'])
        if i is None:
            by_key[r['key']] = len(old)
            old.append(r)
        else:
            old[i] = r
    with open(RECS, 'w', encoding='utf-8') as f:
        json.dump(old[-300:], f, ensure_ascii=False, indent=1)
    if not DRY:
        with open(HIST, 'a', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')


def evaluate(tasks, qs, today, mins, fired_keys, fired_codes, market_ok=True):
    """纯判定：返回本轮新增提醒记录（含已调用 notify）。"""
    out = []
    base = tasks['base']
    gap = (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(base)).days
    buy_win = 9 * 60 + 15 <= mins <= 9 * 60 + 32
    trade_win = (9 * 60 + 26 <= mins <= 11 * 60 + 30) or (13 * 60 <= mins <= 15 * 60 + 2)
    exit_win = 14 * 60 + 45 <= mins <= 15 * 60 + 2

    def emit(kind, x, msg, extra=None, color=None):
        key = '%s|%s|%s' % (today, x['code'], KIND[kind])
        if key in fired_keys:
            return
        rec = dict(key=key, date=today,
                   time=datetime.datetime.now().strftime('%H:%M:%S'),
                   code=x['code'], name=x['name'], kind=KIND[kind],
                   msg=msg, base=base)
        if extra:
            rec.update(extra)
        notify('%s · %s' % (KIND[kind], x['name']), msg, x['code'],
                color or {'buy': BUY_COLOR, 'buy_watch': BUY_WATCH_COLOR,
                          'tp': TP_COLOR, 'exit': EXIT_COLOR}[kind])
        fired_keys.add(key)
        # A 股 T+1：买入当日不可卖出。买入/仅观察窗弹过的票当天不再发任何卖出窗。
        fired_codes.add(x['code'])
        out.append(rec)

    # ① 买：确认日次一交易日 9:15~9:32。**用户口径：提醒当日必须发出来**——
    #        触发日指数过滤（idx_gate：上证>MA20·量比≥0.9·mom20 0~6%·|缺口|<1%）
    #        只决定「可介入」还是「仅观察」，两类都弹窗，颜色与文案不同。
    if market_ok and buy_win and today >= tasks['buy_date'] and 0 <= gap <= 7:
        for x in tasks['buys']:
            if x.get('idx_ok'):                      # 指数配合：可介入
                msg = ('确认日 %s 收盘成立（收盘 %.2f 站上信号日高 %.2f 且量比≥1.2）。'
                       '指数配合（%s）。今日 9:30 按开盘价买入，不追高、不提前埋伏；'
                       'T+1：今日不可卖出，次一交易日起盘中触及 开盘价×1.025 才止盈，'
                       '20 个交易日未触达则收盘平仓；无个股硬止损，单票≤1/4 分散。'
                       % (x.get('trig_date') or base, x.get('close', 0), x.get('hi0', 0),
                          x.get('idx_why') or '触发日指数状态达标'))
                emit('buy', x, msg,
                     dict(hi0=x.get('hi0'), close=x.get('close'), idx_ok=True,
                          trig_date=x.get('trig_date'), buy_hint='9:30 开盘价'),
                     BUY_COLOR)
            else:                                    # 指数不配合：照常提醒，但仅观察
                msg = ('确认日 %s 收盘成立（收盘 %.2f 站上信号日高 %.2f 且量比≥1.2），'
                       '但触发日指数不配合：%s。'
                       '按纪律本轮不介入（回测：弱市/缩量/跳空日介入胜率 53~75%%）；'
                       '票已入观察池，指数转好后的下一次确认再介入。不买也要知道它确认了。'
                       % (x.get('trig_date') or base, x.get('close', 0), x.get('hi0', 0),
                          x.get('idx_why') or '触发日指数过滤未达标'))
                emit('buy_watch', x, msg,
                     dict(hi0=x.get('hi0'), close=x.get('close'), idx_ok=False,
                          idx_why=x.get('idx_why'),
                          trig_date=x.get('trig_date'), buy_hint='仅观察·不介入'),
                     BUY_WATCH_COLOR)

    # ② 卖：+2.5% 盘中止盈。A股 T+1——买入当日（buy_date==today）不可卖出，
    #        回测撮合同样从 bi+1 起判，故这里必须排除今日刚买入的票。
    if market_ok and trade_win:
        for x in tasks['holds']:
            if x.get('buy_date') == today:   # T+1：当日买入不可卖
                continue
            q = qs.get(x['code'][2:])
            if not q:
                continue
            hi = max(q.get('high') or 0, q.get('price') or 0)
            tp = x.get('tp') or 0
            if hi <= 0 or tp <= 0 or hi < tp - 1e-9:
                continue
            if x['code'] in fired_codes:      # 今天已弹过别的窗，不重复
                continue
            msg = ('买入 %.2f → 止盈价 %.2f，盘中最高 %.2f 已触达（现价 %.2f）—— '
                   '按规则卖出，扣 0.25%% 成本约 +2.25%%；已持有 %s 个交易日。'
                   % (x.get('buy', 0), tp, hi, q.get('price') or 0, x.get('days', '-')))
            emit('tp', x, msg, dict(buy=x.get('buy'), tp=tp, high=round(hi, 2),
                                   price=round(q.get('price') or 0, 2)), TP_COLOR)

    # ③ 卖：20 日时间退出，收盘前平仓（当天已止盈的不重复）
    if market_ok and exit_win:
        for x in tasks['holds']:
            ex = x.get('exit_date')
            if not ex or ex > today or x['code'] in fired_codes:
                continue
            q = qs.get(x['code'][2:]) or {}
            px = q.get('price') or 0
            msg = ('第 %s 个交易日时间退出（预计到期日 %s），今日收盘前按收盘价平仓；'
                   '现价 %.2f、浮盈 %+.2f%%。本规则无个股硬止损，靠单票≤1/4 分散控尾部。'
                   % (tasks['hold'], ex, px, x.get('pnl', 0)))
            emit('exit', x, msg, dict(exit_date=ex, price=round(px, 2),
                                     pnl=x.get('pnl')), EXIT_COLOR)
    return out


def check():
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now()
    mins = now.hour * 60 + now.minute
    try:
        tasks = load_tasks()
    except Exception as e:
        log('win80.json 读取失败：%s' % e)
        return []
    ok, why = market_today()
    fired_keys, fired_codes = load_fired(today)

    # 心跳：页面据此显示「守护是否在跑 / 当前盯几只」
    state = dict(date=today, last_check=now.strftime('%Y-%m-%d %H:%M:%S'),
                 market_open=ok, market_ts=why, base=tasks['base'],
                 generated_at=tasks['generated_at'],
                 buy_date=tasks['buy_date'], n_buy=len(tasks['buys']),
                 n_buy_ok=sum(1 for x in tasks['buys'] if x.get('idx_ok')),
                 idx_gate=tasks.get('idx_gate'),
                 n_hold=len(tasks['holds']), n_pending=tasks['n_pending'],
                 n_confirmed=tasks['n_confirmed'],
                 next_buy=[dict(name=x['name'], idx_ok=x.get('idx_ok'))
                           for x in tasks['buys'][:8]],
                 next_exit=[dict(name=x['name'], exit_date=x.get('exit_date'))
                            for x in tasks['holds'] if x.get('exit_date')][:8],
                 dry=DRY)
    try:
        with open(STATEF, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception as e:
        log('心跳写入失败：%s' % e)

    if not ok:
        log('非交易日/行情未更新（%s）→ 静默；盯 买%d / 持有%d' %
            (why, len(tasks['buys']), len(tasks['holds'])))
        return []

    codes = sorted({x['code'] for x in tasks['holds']})
    qs = {}
    if codes:
        try:
            qs = quotes(codes)
        except Exception as e:
            log('行情拉取失败：%s' % e)
    recs = evaluate(tasks, qs, today, mins, fired_keys, fired_codes, True)
    if recs:
        save(recs)
        log('弹窗 %d 条：%s' % (len(recs), ', '.join(
            '%s(%s)' % (r['name'], r['kind'][:6]) for r in recs)))
    else:
        log('轮询 %d 只持仓 → 无买卖信号（盯 明日买 %d / 到期 %d）' %
            (len(qs), len(tasks['buys']),
             sum(1 for x in tasks['holds'] if x.get('exit_date'))) )
    return recs


def selftest():
    """合成数据验证三类弹窗分支 + 去重 + 互斥，不联网。"""
    global DRY
    DRY = True
    tasks = dict(base='2026-11-06', generated_at='selftest', buy_date='2026-11-09',
                 tp=2.5, hold=20, n_pending=2, n_confirmed=3,
                 buys=[dict(code='sh600001', name='买点票', hi0=10.0, close=10.5,
                            trig_date='2026-11-06', idx_ok=True,
                            idx_why='指数配合'),
                       # 指数不配合：仍必须在买入窗口弹「仅观察」窗（用户口径：提醒当日要发出来）
                       dict(code='sh600004', name='观察票', hi0=8.0, close=8.2,
                            trig_date='2026-11-06', idx_ok=False,
                            idx_why='上证<MA20；量比0.86<0.9')],
                 holds=[dict(code='sz000002', name='止盈票', buy=20.0, tp=20.5,
                             buy_date='2026-11-06', days=3, pnl=1.2, exit_date='2026-12-04'),
                        dict(code='sz000003', name='到期票', buy=30.0, tp=30.75,
                             buy_date='2026-09-01', days=19, pnl=-4.0, exit_date='2026-11-09'),
                        # 今日刚买入：T+1 不可卖，即使 high 已越过止盈价也不得弹窗
                        dict(code='sh600001', name='今日买票', buy=10.0, tp=10.25,
                             buy_date='2026-11-09', days=1, pnl=2.3, exit_date='2026-12-07')])
    qs = {'000002': dict(name='止盈票', price=20.6, high=20.8, open=20.0),
          '000003': dict(name='到期票', price=29.0, high=29.2, open=30.0),
          '600001': dict(name='今日买票', price=10.4, high=10.5, open=10.0)}
    today = '2026-11-09'
    # 买入窗口：可介入（红）+ 仅观察（琥珀）两类都要弹
    r1 = evaluate(tasks, qs, today, 9 * 60 + 20, set(), set())
    k1 = sorted(x['kind'] for x in r1)
    assert k1 == sorted([KIND['buy'], KIND['buy_watch']]), \
        '买入窗口应同时弹 可介入+仅观察 两类：%s' % k1
    # 盘中止盈窗口（high 20.8 >= 20.5）
    r2 = evaluate(tasks, qs, today, 10 * 60, set(), set())
    assert len(r2) == 1 and r2[0]['kind'] == KIND['tp'], '止盈弹窗未触发'
    # 到期窗口：到期票弹、止盈票也弹？止盈票 exit_date=12-04 未到 → 只 1 条
    r3 = evaluate(tasks, qs, today, 14 * 60 + 50, set(), set())
    kinds = sorted(x['kind'] for x in r3)
    assert kinds == sorted([KIND['tp'], KIND['exit']]), '到期/止盈分支异常：%s' % kinds
    # 去重：已弹过的 key 不再弹
    keys = {x['key'] for x in r2 + r3}
    r4 = evaluate(tasks, qs, today, 14 * 60 + 52, keys, {'sz000002', 'sz000003'})
    assert not r4, '去重失效'
    # 同票同日互斥：止盈已弹 → 到期不再弹
    r5 = evaluate(tasks, qs, today, 14 * 60 + 50, set(), {'sz000003'})
    assert not any(x['code'] == 'sz000003' for x in r5), '互斥失效'
    # T+1：买入当日 high 已越过止盈价也不弹；买入弹过的票当天不再发任何卖出窗。
    # 但其它票（非今日买入）的止盈窗必须照常工作。
    fk, fc = set(), set()
    ra = evaluate(tasks, qs, today, 9 * 60 + 20, fk, fc)
    rb = evaluate(tasks, qs, today, 10 * 60, fk, fc)
    assert sorted(x['kind'] for x in ra) == sorted([KIND['buy'], KIND['buy_watch']]), \
        '买入窗两类异常'
    assert any(x['code'] == 'sz000002' and x['kind'] == KIND['tp'] for x in rb), \
        '正常次日止盈被误挡'
    assert not any(x['code'] == 'sh600001' for x in rb), 'T+1 失效：买入当日弹了止盈'
    assert not any(x['code'] == 'sh600001' for x in r2), 'T+1 失效：buy_date 未排除'
    log('SELFTEST OK：买(可介入/仅观察两类) + 止盈/到期 + 去重 + 互斥 + T+1 禁卖 全部通过')
    return True


def main():
    if SELFTEST:
        selftest()
        return
    log('win80_alert started：买(9:15-9:32 开盘) / 卖(T+1 次日起 +2.5%%盘中止盈 + 20日到期) '
        '· 每%ds 轮询 · 基准 %s%s%s' %
        (CYCLE, (load_tasks()['base'] if os.path.exists(DATA) else 'win80.json缺失'),
         ' [DRY]' if DRY else '', ' [FORCE]' if FORCE else ''))
    while True:
        t0 = time.time()
        now = datetime.datetime.now()
        mins = now.hour * 60 + now.minute
        active = FORCE or (now.weekday() < 5 and (
            (9 * 60 + 15 <= mins <= 11 * 60 + 30) or
            (13 * 60 <= mins <= 15 * 60 + 2)))
        if active:
            try:
                check()
            except Exception as e:
                log('check error: %s' % e)
        else:
            # 非交易时段也写心跳，页面能看到「守护活着、当前是休市」
            try:
                tasks = load_tasks()
                with open(STATEF, 'w', encoding='utf-8') as f:
                    json.dump(dict(date=now.date().isoformat(),
                                   last_check=now.strftime('%Y-%m-%d %H:%M:%S'),
                                   market_open=False, market_ts='休市/非交易时段',
                                   base=tasks['base'], generated_at=tasks['generated_at'],
                                   buy_date=tasks['buy_date'], n_buy=len(tasks['buys']),
                                   n_buy_ok=sum(1 for x in tasks['buys'] if x.get('idx_ok')),
                                   idx_gate=tasks.get('idx_gate'),
                                   n_hold=len(tasks['holds']),
                                   n_pending=tasks['n_pending'],
                                   n_confirmed=tasks['n_confirmed'],
                                   next_buy=[dict(name=x['name'], idx_ok=x.get('idx_ok'))
                                             for x in tasks['buys'][:8]],
                                   next_exit=[dict(name=x['name'], exit_date=x.get('exit_date'))
                                              for x in tasks['holds'] if x.get('exit_date')][:8],
                                   dry=DRY), f, ensure_ascii=False, indent=1)
            except Exception:
                pass
        if ONCE:
            break
        time.sleep(max(1.0, CYCLE - (time.time() - t0)))


if __name__ == '__main__':
    main()
