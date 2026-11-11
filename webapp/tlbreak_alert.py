#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三线穿阳·破20日高 盘中提醒守护进程（只轮询静态预选池版）。

设计（用户口径：先预选、把离条件远的踢掉，盘中只查预选中的那些）：
  预选池：服务端 /api/tlbreak_pool 用**昨收日K**算好——昨收距突破价
          ≤ POOL_DIST%（默认4%），默认完整取用不截断。这是静态池，
          盘中不依赖实时行情重建，也不扫全市场 2338 只。
  轮询层：每 CYCLE 秒（默认60秒）只拉预选池的实时行情并判定；已提醒 / 当日
          开盘已高于三线最低值（结构性不可能吞没）的票从池里剔除，越查越少。
  刷新层：每 POOL_SECS 秒（默认15分钟）从**本地 API** 重拉一次预选池清单
          （纯本地计算，零外部行情请求），用于适配日K更新/新上市。
  不再有「每15分钟全市场行情扫描」——那是旧版多加的兜底层，费请求且没必要。

触发判定（用户口径：**只认本轮查询那一刻拿到的行情快照**，不做事后复判、
不等「持续站稳」，也不拿之后查到的实时价去否定当时那一帧）：
  1) 阳线进行中：查询时刻 当日最高 > 开盘价
  2) 阳柱实体吞没三线：开盘价 <= min(EMA12, EMA50, MA89)
  3) 突破：查询时刻 当日最高 > trigger = max(三线最高值, 近20日最高收盘)
「现价是否仍站上 trigger」不再作为能否发通知的门槛，只决定弹窗颜色与文案：
  live     现价 > trigger            → 红色闪窗（实时站上，原买入信号口径）
  touched  最高 > trigger 但现价回落 → 琥珀闪窗（曾触及·已回落，文案明确非买入点）
命中即弹 + macOS 通知 + 写 tlbreak_alerts.json + 永久归档 HIST。
CYCLE=60s，首次发现即弹，延迟 ≤60 秒；因为每轮都查，不会几小时后补弹旧信号。

链2（2026-11-10 新增）⑥穿线阳：独立判定 evaluate2()，原破高链 evaluate() 不动。
  口径：阳柱实体包住 EXPMA12/50（MA89 在实体内=三线，优先）；不设量比/破20日高/
  涨幅门槛。EMA 用「若以现价收盘」的递推估计（收盘快照与日报⑥口径严格一致），
  高开动量票也能在盘中判出；以「涨停价下仍无法成形」做结构性剔除。
  kind=🟢穿线阳·双线(盘中) / 🔺三线穿阳·未破20日高(盘中)，与破高链分 key 去重。

「别发太早」的含义：预选池（昨收距突破价 ≤ POOL_DIST%）只用来缩小轮询范围，**不是提前
预警**——没有真越过 trigger 绝不发通知，避免看到「快突破了」就忍不住提前动手。
一票一天一条；曾触及弹过仍继续跟踪，之后真站上 trigger 才升级再弹一次红色。

存量兼容：按旧规则静默留档的今日 touch（无 notified 标记）不补弹，但如果
之后真站上 trigger，仍作为新信号弹红色。

口径说明：盘中通知是「预触发」——今日收盘的 EMA 要收盘后才知道（用最近收盘值近似）；
收盘确认以三线池/日报口径为准（三线池只认「收盘破20日高」）。
用法：python3 tlbreak_alert.py [--dry]   dry=只打印不弹通知（测试用）
     supervisor.sh 常驻；daily_run.py 每日收盘后随 alertd 一起重启。"""
import datetime, json, os, subprocess, sys, time, urllib.request

try:
    sys.stdout.reconfigure(line_buffering=True)   # nohup 下日志实时可见
except Exception:
    pass

from flashwin import flash, RED

TOUCH_COLOR = '#b45309'   # 琥珀：曾触及但已回落，弱于红色买入信号，避免诱导追高

WEB = os.path.expanduser('~/WorkBuddy/Claw/webapp')
RECS = os.path.join(WEB, 'tlbreak_alerts.json')          # 滚动窗口（去重判重用，只留最近300条）
HIST = os.path.join(WEB, 'tlbreak_alert_history.jsonl')  # 永久档案（append-only，供回测，永不截断）
SRV = 'http://127.0.0.1:8765'
DRY = '--dry' in sys.argv
CYCLE = 60          # 预选池轮询周期（秒）
POOL_SECS = 900     # 从本地 API 刷新预选池清单的周期（秒），不产生外部行情请求
POOL_TOP = 0        # 0=完整预选池；设 N 则只取距突破价最近的 N 只
POOL_DIST = 4.0     # 预选判据：昨收距突破价≤4%（用户 2026-11-10 定稿：宁可少查，也不要
                    #  1720 只的量；4% 实测 735 只/轮）。曾试 6%(1034)/10%(1720) 后回退到 4%。
                    # 已知取舍：2026-11-09 收盘破20高的5只中4只距离8.0~13.8%（含唯一A级
                    #  华纳药厂8.1%），4%池更覆盖不到；这类跳空大阳线需换判据而非放宽距离。
BATCH = 200         # 每批行情代码数（实测 300 也稳，200 保守）；4%池735只→4个请求/轮

_kind = '🔺三线穿阳·破20日高(盘中)'
# ⑥穿线阳 第二条链（用户 2026-11-10）：阳柱实体包住 EXPMA12/50（o<min线 且 现价>max线），
# MA89 也在实体内=三线（优先级高）。不设量比/破20日高/涨幅门槛；预选=全市场线位，
# 盘中用「今开 < lo」做结构性过滤（开盘盘中不变，第一轮全市场扫描后自动收敛）。
_kind3 = '🔺三线穿阳·未破20日高(盘中)'
_kind2 = '🟢穿线阳·双线(盘中)'


# 运行态：W=预选池条目 {完整代码: 条目}；BY_BARE={裸代码: 条目}（行情字典用裸代码键）；
# pool=待轮询代码集合；fired=今日已提醒；drop=当日结构性不可能触发（开盘已高于三线最低值）
STATE = {'W': {}, 'BY_BARE': {}, 'pool': set(), 'fired': set(), 'drop': set(),
         'pool_ts': 0.0, 'day': ''}
# 穿线阳第二条链：lines=全市场线位 {完整代码: 条目}；pool=待轮询集合（首轮全市场，
# 之后开盘>=lo 的结构性剔除、已提醒的剔除，越查越少）；drop=当日开盘已不低于双线最低值
STATE2 = {'lines': {}, 'BY_BARE': {}, 'pool': set(), 'fired': set(), 'drop': set(),
          'pool_ts': 0.0}


def quotes(codes):
    """腾讯实时行情：返回 {bare_code: {name, price, open, prev_close, high}}"""
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
                out[val[2]] = dict(name=val[1],
                                   price=float(val[3]) if val[3] else 0,
                                   prev_close=float(val[4]) if val[4] else 0,
                                   open=float(val[5]) if val[5] else 0,
                                   high=float(val[33]) if val[33] else 0)
            except ValueError:
                pass
    return out


def notify(title, msg, url='', color=RED):
    if DRY:
        print('[DRY] %s | %s' % (title, msg))
        return
    # 1) 闪屏窗（置顶闪烁 5 秒，点击 → Chrome 打开该票 K 线）
    flash(title, msg, url, color)
    # 2) macOS 系统通知（横幅 + Glass 提示音，进通知中心可回看）
    msg = msg.replace('"', "'")
    subprocess.run(['osascript', '-e',
                    'display notification "%s" with title "%s" sound name "Glass"' % (msg, title)],
                   capture_output=True)


def upsert_history(records):
    """把本轮新增/升级的提醒同步到永久档案（jsonl 每行一条，按 key 覆盖）。

    RECS 只保留最近 300 条用于「每股每日只提醒一次」去重，历史会被挤掉；
    回测需要全量，故单独维护 HIST。按 key 覆盖而非只追加，是为了支持
    「曾触及(已回落) → 后来真正站上」的升级，保证一票一天一条。
    """
    try:
        rows, index = [], {}
        if os.path.exists(HIST):
            with open(HIST, encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    # 老档可能缺来源/触发方式标记：读档时自愈，保证永久档案字段恒齐
                    r.setdefault('source', 'live')
                    r.setdefault('mode', 'backfill' if r.get('source') == 'backfill' else 'live')
                    k = r.get('key')
                    if k in index:
                        rows[index[k]] = r
                    else:
                        index[k] = len(rows)
                        rows.append(r)
        for r in records:
            k = r.get('key')
            if k in index:
                rows[index[k]] = r
            else:
                index[k] = len(rows)
                rows.append(r)
        tmp = HIST + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(tmp, HIST)
    except Exception as e:
        print('history upsert error:', e)


def fetch_pool():
    """从本地 API 拉静态预选池（纯日K计算，不拉任何实时行情）。"""
    req = urllib.request.Request(
        SRV + '/api/tlbreak_pool?top=%d&dist=%g' % (POOL_TOP, POOL_DIST),
        headers={'User-Agent': 'Mozilla/5.0'})
    data = json.load(urllib.request.urlopen(req, timeout=30))
    pool = data.get('pool') or []
    STATE['W'] = {w['code']: w for w in pool}
    STATE['BY_BARE'] = {w['code'][2:]: w for w in pool}
    STATE['pool'] = set(STATE['W'])
    STATE['pool_ts'] = time.time()
    print('%s 预选池刷新：基准日 %s · 昨收距突破价≤%g%% 的票 %d 只 → 本轮轮询 %d 只（只查这些，不扫全市场）'
          % (datetime.datetime.now().strftime('%H:%M:%S'), data.get('date'),
             data.get('dist_pct', POOL_DIST), data.get('n_all', 0), len(pool)))
    return pool


def poll(codes):
    """批量拉行情（每批80只，带 sh/sz 前缀）。只传预选池代码。"""
    qs = {}
    for i in range(0, len(codes), BATCH):
        part = codes[i:i + BATCH]
        for attempt in (1, 2):
            try:
                qs.update(quotes(part))
                break
            except Exception as e:
                print('quotes batch error (try %d): %s' % (attempt, e))
                time.sleep(0.5)   # 单批失败立即重试一次，避免整批票本轮被跳过
        time.sleep(0.3)          # 批间稍作间隔，避免触发行情源限频
    return qs


def evaluate(qs):
    """对预选池本轮拿到的行情快照逐票判定，返回本轮**真正弹窗**的记录列表。

    【用户口径·不可变】两条核心判据 + 一条判定时点（2026-11-10 用户定稿保留）：
      ① 开盘吞没三线：o <= min(EMA12,EMA50,MA89)
      ② 当日最高越突破价：hi > o 且 hi > trigger = max(三线最高, 近20日最高收盘)
      ③ 只看「查询那一刻」的快照，穿了就算：用当日 high 判定（含冲高回落），
         不做事后复判、不等持续站稳、不等收盘确认；收盘确认属三线池/日报口径。
    这是纪律，不得为提高命中数而放宽或删除；覆盖不足只能改「预选层」
    （哪些票进入轮询），evaluate() 判定逻辑保持原样。

    口径：命中的判据只看这一帧快照，不再要求「现价必须持续站上」。
      live     = px > o 且 px > trigger            → 红色弹窗（实时站上）
      touched  = hi > o 且 hi > trigger（含冲高回落）→ 琥珀弹窗（曾触及·已回落）
    共用同一个 key：曾触及弹过后仍继续跟踪，之后真站上才升级再弹一次红色；
    live 弹过则从轮询池剔除。保证「一票一天最多两条、不重复骚扰」。
    """
    today = datetime.date.today().isoformat()
    try:
        recs = json.load(open(RECS))       # 滚动窗口 = 当日**真实弹过窗**的提醒
    except Exception:
        recs = []
    by_key = {}
    for i, a in enumerate(recs):
        if a.get('key'):
            by_key[a['key']] = i
    # 今日已在永久档案留痕的 key；其中旧规则下「静默归档、没弹过窗」的 touch
    # 单独记为 legacy_silent：不补发旧信号（避免几小时后突然弹），但真站上仍可弹。
    archived, legacy_silent = set(), set()
    if os.path.exists(HIST):
        try:
            with open(HIST, encoding='utf-8') as f:
                for line in f:
                    try:
                        a = json.loads(line)
                        if a.get('date') == today:
                            k = a.get('key')
                            archived.add(k)
                            if a.get('mode') == 'touch' and not a.get('notified'):
                                legacy_silent.add(k)
                    except Exception:
                        pass
        except Exception:
            pass
    fired, changed, n_touch = [], [], 0
    for bare in sorted(qs):
        w = STATE['BY_BARE'].get(bare)
        if not w:
            continue
        q = qs[bare]
        o, px, hi = q['open'], q['price'], q['high']
        if o > 0 and o > w['lo'] + 1e-9:
            STATE['drop'].add(w['code'])   # 开盘已高于最低线：当天不可能吞没三线，后续不用再查
            continue
        if o <= 0 or px <= 0 or hi <= 0:
            continue
        # 只看本轮查询这一帧快照：当日最高越过触发价即命中，防 60 秒间隙的瞬时穿越漏掉
        if not (hi > o and hi > w['trigger']):
            continue
        live = px > o and px > w['trigger']   # 仅区分颜色/文案，不再是「能不能发通知」的门槛
        key = '%s|%s|%s' % (today, w['code'], _kind)
        prev_i = by_key.get(key)
        if prev_i is not None:
            prev = recs[prev_i]
            if not (prev.get('mode') == 'touch' and live):
                if prev.get('mode') != 'touch':
                    STATE['fired'].add(w['code'])       # live 已弹过 → 彻底剔除
                continue                                # touch 已弹过且仍回落 → 静默跟踪
            # touch → live 升级：允许再弹一次红色
        elif key in legacy_silent and not live:
            continue                                    # 旧规则静默档：不补发旧信号
        grade = 'MA89上行·A级特征' if w['s89'] > 0 else 'MA89仍下行·B级特征'
        rec = dict(key=key, date=today,
                   time=datetime.datetime.now().strftime('%H:%M:%S'),
                   code=w['code'], name=(q['name'] or w['name']),
                   source='live',     # 盘中守护产生；仅日K回放的回填样本是 backfill
                   mode='live', notified=True,   # notified=True：这条是真弹过窗的
                   price=px, high=hi, trigger=w['trigger'], s89=w['s89'])
        if live:
            msg = ('查询时刻现价 %.2f 站上 %.2f · 开盘 %.2f 已吞没三线 · %s · 20日高 %.2f —— '
                   '盘中为预触发，收盘站稳才确认；勿追高开，等回踩三线带低吸。'
                   % (px, w['trigger'], o, grade, w['hi20']))
            rec.update(kind=_kind, mode='live', msg=msg)
            notify('%s · %s' % (_kind, rec['name']), msg,
                   SRV + '/?jump=%s/p1' % w['code'], color=RED)  # 点击闪窗 → Chrome 该票 K 线
            STATE['fired'].add(w['code'])               # 实时站上：后续不再跟踪
        else:
            msg = ('查询时刻最高 %.2f 已越过触发价 %.2f，现价回落至 %.2f —— '
                   '曾触及·已回落，不是买入点，别追 · 开盘 %.2f 已吞没三线 · %s'
                   % (hi, w['trigger'], px, o, grade))
            rec.update(kind='🔺三线穿阳·曾触及(已回落·非实时)', mode='touch', msg=msg)
            notify('🔺三线穿阳·曾触及(已回落) · %s' % rec['name'], msg,
                   SRV + '/?jump=%s/p1' % w['code'], color=TOUCH_COLOR)
            n_touch += 1
            # touch 不加入 STATE['fired']：继续跟踪，等真站上再升级弹红色
        if prev_i is not None:
            recs[prev_i] = rec                          # 升级覆盖，仍是一票一天一条
        else:
            recs.append(rec)
            by_key[key] = len(recs) - 1
        fired.append(rec)
        changed.append(rec)
        archived.add(key)
    if fired:
        json.dump(recs[-300:], open(RECS, 'w'), ensure_ascii=False, indent=1)
    if changed:
        # 按 key upsert：touch 首次落档；之后真站上则覆盖升级为 live
        upsert_history(changed)
        if n_touch:
            print('%s 查询快照命中「曾触及·已回落」%d 只（已弹琥珀闪窗，非买入点）'
                  % (datetime.datetime.now().strftime('%H:%M:%S'), n_touch))
    return fired


def fetch_pool2():
    """从本地 API 拉穿线阳全市场线位（昨收基准 EMA12/50/MA89 + c89，纯本地计算）。"""
    req = urllib.request.Request(SRV + '/api/crossline_pool',
                                 headers={'User-Agent': 'Mozilla/5.0'})
    data = json.load(urllib.request.urlopen(req, timeout=60))
    pool = data.get('pool') or []
    STATE2['lines'] = {w['code']: w for w in pool}
    STATE2['BY_BARE'] = {w['code'][2:]: w for w in pool}
    # drop/fired 跨刷新保留（开盘盘中不变、当日已提醒不重复）
    STATE2['pool'] = set(STATE2['lines']) - STATE2['drop'] - STATE2['fired']
    STATE2['pool_ts'] = time.time()
    print('%s 穿线阳线位刷新：基准日 %s · 全市场 %d 只（首轮全查，此后只查「涨停价下仍可能成形」的存量）'
          % (datetime.datetime.now().strftime('%H:%M:%S'), data.get('date'), len(pool)))
    return pool


def _zt_ratio(code):
    """涨停幅度近似（判可达性用，留 0.2% 余量防浮点边界）：创业/科创 20%、北交 30%、其余 10%。"""
    if code.startswith('sz30') or code.startswith('sh68'):
        return 0.198
    if code.startswith('bj'):
        return 0.298
    return 0.098


def _engulf(o, px, w):
    """穿线阳形态判定（动态口径：若以 px 收盘的 EMA 估计，与日报 cross_lines 收盘时严格相等）。

    e12e/e50e/m89e = 昨收值 + k*(px-昨收值)；k12=2/13、k50=2/51、MA89 逐日增量=(px-c89)/89。
    双线 = o < min(e12e,e50e) 且 px > max(e12e,e50e)（阳柱实体包住双线）
    三线 = o < min(e12e,e50e,m89e) 且 px > max(e12e,e50e,m89e)（MA89 也在实体内，优先）
    返回 3/2/0。"""
    k12, k50 = 2 / 13.0, 2 / 51.0
    e12e = w['e12'] + k12 * (px - w['e12'])
    e50e = w['e50'] + k50 * (px - w['e50'])
    m89e = w['m89'] + (px - w['c89']) / 89.0
    lo3, hi3 = min(e12e, e50e, m89e), max(e12e, e50e, m89e)
    if o < lo3 and px > hi3:
        return 3
    lo2, hi2 = min(e12e, e50e), max(e12e, e50e)
    if o < lo2 and px > hi2:
        return 2
    return 0


def evaluate2(qs):
    """⑥穿线阳 快照判定（独立于 evaluate()，原三线破高判定逻辑不动）。

    口径（用户 2026-11-10）：当日阳柱实体包住 EXPMA12/50，MA89 在实体内=三线优先；
    不设量比/破20日高/涨幅门槛。只看「查询那一刻」快照：用现价做 EMA 收盘估计，
    形态此刻成形即弹（预触发），不等收盘、不做事后复判；收盘快照与日报⑥口径一致。
    结构性剔除：以涨停价代入仍无法成形 → 今日不可能命中，从轮询池剔除（越查越少）。
    一票一天一条（分链去重，与破高链互不挤占）。
    """
    today = datetime.date.today().isoformat()
    try:
        recs = json.load(open(RECS))
    except Exception:
        recs = []
    by_key = {}
    for i, a in enumerate(recs):
        if a.get('key'):
            by_key[a['key']] = i
    fired = []
    for bare in sorted(qs):
        w = STATE2['BY_BARE'].get(bare)
        if not w:
            continue
        q = qs[bare]
        o, px = q['open'], q['price']
        if o <= 0 or px <= 0:
            continue
        n_lines = _engulf(o, px, w)
        if not n_lines:
            zt = round(q.get('prev_close', 0) * (1 + _zt_ratio(w['code'])), 2)
            if zt <= 0 or _engulf(o, zt, w) == 0:
                STATE2['drop'].add(w['code'])   # 涨停价下也无法成形：今日结构性不可能
            continue
        kind = _kind3 if n_lines == 3 else _kind2
        key = '%s|%s|%s' % (today, w['code'], kind)
        if key in by_key:
            continue                        # 本链今日已弹过
        grade = 'MA89上行·A级特征' if w['s89'] > 0 else 'MA89仍下行·B级特征'
        try:
            chg = (px / q['prev_close'] - 1) * 100 if q.get('prev_close') else 0
        except Exception:
            chg = 0
        if n_lines == 3:
            rel89 = 'MA89=%.2f 在实体内' % w['m89']
        else:
            rel89 = ('MA89=%.2f 在实体上方' % w['m89'] if w['m89'] >= max(w['e12'], w['e50'])
                     else 'MA89=%.2f 在实体下方' % w['m89'])
        msg = ('查询时刻现价 %.2f（%+.1f%%）阳柱实体包住 %d 线 · 开盘 %.2f · %s · %s —— '
               '盘中预触发（按现价收盘的EMA估计，收盘快照与日报⑥口径一致）；'
               '形态观察，勿追已大涨票。'
               % (px, chg, n_lines, o, rel89, grade))
        rec = dict(key=key, date=today,
                   time=datetime.datetime.now().strftime('%H:%M:%S'),
                   code=w['code'], name=(q['name'] or w['name']),
                   source='live', mode='live', notified=True,
                   price=px, e12=w['e12'], e50=w['e50'], m89=w['m89'], s89=w['s89'],
                   n_lines=n_lines, kind=kind, msg=msg)
        notify('%s · %s' % (kind, rec['name']), msg,
               SRV + '/?jump=%s/p1' % w['code'], color=RED)
        recs.append(rec)
        by_key[key] = len(recs) - 1
        STATE2['fired'].add(w['code'])
        STATE2['pool'].discard(w['code'])
        fired.append(rec)
    if fired:
        json.dump(recs[-300:], open(RECS, 'w'), ensure_ascii=False, indent=1)
        upsert_history(fired)
        print('%s 穿线阳快照命中 %d 只（%s）' % (
            datetime.datetime.now().strftime('%H:%M:%S'), len(fired),
            ', '.join('%s[%d线]' % (r['name'], r['n_lines']) for r in fired[:8])
            + ('…' if len(fired) > 8 else '')))
    return fired


def check():
    """一轮检查：只轮询两条链预选池中「未提醒 + 当日仍可能触发」的票。"""
    today = datetime.date.today().isoformat()
    if STATE['day'] != today:                # 跨日自动重置（无需等 daily_run 重启）
        STATE['day'] = today
        STATE['fired'].clear()
        STATE['drop'].clear()
        STATE['pool_ts'] = 0.0
        STATE2['fired'].clear()
        STATE2['drop'].clear()
        STATE2['pool_ts'] = 0.0
    if not STATE['W'] or time.time() - STATE['pool_ts'] >= POOL_SECS:
        fetch_pool()
    if not STATE2['lines'] or time.time() - STATE2['pool_ts'] >= POOL_SECS:
        fetch_pool2()
    codes = sorted(STATE['pool'] - STATE['fired'] - STATE['drop'])
    codes2 = sorted(STATE2['pool'] - STATE2['fired'] - STATE2['drop'])
    codes_all = sorted(set(codes) | set(codes2))
    if not codes_all:
        return []
    qs = poll(codes_all)                     # 两链合并一次行情请求（去重后分链判定）
    fired = evaluate(qs)                     # 链1：三线穿阳·破20日高（原逻辑不动）
    fired += evaluate2(qs)                   # 链2：⑥穿线阳（双线/三线，不限涨幅）
    print('%s 轮询 破高池 %d + 穿线阳池 %d 只 → 命中 %d（链1 已提醒 %d/剔除 %d · 链2 已提醒 %d/剔除 %d）'
          % (datetime.datetime.now().strftime('%H:%M:%S'), len(codes), len(codes2), len(fired),
             len(STATE['fired']), len(STATE['drop']),
             len(STATE2['fired']), len(STATE2['drop'])))
    return fired


def main():
    print('tlbreak_alert started: 链1=三线穿阳·破20日高（静态预选池 昨收距突破价≤%g%%，%s）；'
          '链2=⑥穿线阳（全市场线位，盘中「现价收盘EMA估计」下阳柱实体包住双线即弹，'
          '三线优先，不限涨幅/量比/破20日高）'
          '每%ds 一轮、每批%d只；池子每%ds 从本地API刷新（不扫外部行情）'
          '(9:26-11:30, 13:00-15:02 工作日)%s'
          % (POOL_DIST, '完整池' if not POOL_TOP else 'TOP%d' % POOL_TOP,
             CYCLE, BATCH, POOL_SECS, ' [DRY]' if DRY else ''))
    while True:
        t0 = time.time()
        now = datetime.datetime.now()
        if now.weekday() < 5:
            mins = now.hour * 60 + now.minute
            if (9 * 60 + 26 <= mins <= 11 * 60 + 30) or (13 * 60 <= mins <= 15 * 60 + 2):
                try:
                    fired = check()
                    if fired:
                        print(datetime.datetime.now().strftime('%H:%M:%S'), 'fired:', ', '.join(
                            '%s(%s)' % (r['name'], r['code']) for r in fired))
                except Exception as e:
                    print('check error:', e)
        # 按实际耗时补偿，避免「一轮 2.5s + sleep 60s」逐轮漂移成 62.5s/轮
        time.sleep(max(1.0, CYCLE - (time.time() - t0)))


if __name__ == '__main__':
    main()
