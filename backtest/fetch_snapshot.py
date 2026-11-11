#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收盘刷新全市场名单快照 backtest/snapshot.json（每天最多联网一次）。

为什么需要这个脚本
------------------
`snapshot.json` 是整个工作台的名单基石：server.Data() 从它取
code/name/TotalMV/PE_TTM，dq_check.py 用它核对覆盖，fetch_klines.py 用它当
universe。此前这份档是 2026-11-03 一次性从选股接口导出到
`/tmp/all_snapshot.json` 再复制过来的（两文件 md5 完全相同），
**没有任何定时任务或脚本会自动维护它**，所以新股 / ST 变更 / 市值变化
不会自动纳入。

口径（严格遵守）
----------------
* 数据源与字段口径同旧档：westock-tool filter「零过滤」表达式，仍返回
  14 列 code/name/TurnoverRate/VolumeRatio/TotalMV/Chg10D/Chg20D/
  Chg60D/PE_TTM/Chg5D/EMA_12/EMA_50/ClosePrice/ChangePCT，不换源、不改字段。
  条件放宽到不因 EMA/价格为 0 剔除新股停牌股，但 universe 仍锁定 sh/sz
  （与 11-03 基线一致，不含北证），避免刷新后基石口径突变。
* 只在 **15:05 收盘后**抓，盘中绝不落半日快照（`--force` 才允许人工补抓）。
* 同一天最多尝试一次（写 snapshot_meta.json 留痕），两个跑批定时任务
  不会重复拉；外层 daily_run.py 另有 fetch_state 守卫双保险。
* 写入前严格校验；不通过则**保留旧档不动**，只记告警，不拿半份数据覆盖基石。
* 覆盖前把旧档备份到 backtest/snapshot_bak/，原子替换，全程可回滚。

用法：
    python3 fetch_snapshot.py            # 收盘跑批调用；盘中/当日已抓会安全跳过
    python3 fetch_snapshot.py --check    # 只校验现有档，零联网、零写入
    python3 fetch_snapshot.py --force    # 人工补抓（绕过收盘与当日一次守卫）
"""
import datetime
import glob
import json
import os
import re
import shutil
import subprocess
import sys
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE = _REPO
SNAP = os.path.join(BASE, 'backtest', 'snapshot.json')
META = os.path.join(BASE, 'backtest', 'snapshot_meta.json')
BAK_DIR = os.path.join(BASE, 'backtest', 'snapshot_bak')

# 「零过滤」表达式：条件全部取极宽范围，目的是把全市场股票连同一组显示字段
# 一次性带出来；真正的 ST / 停牌 / 无市值过滤由消费方（fetch_klines 等）做。
EXPR = ('intersect([TotalMV > 0, TurnoverRate >= 0, VolumeRatio >= -1000000, '
        'Chg5D > -1000000, Chg10D > -1000000, Chg20D > -1000000, '
        'Chg60D > -1000000, PE_TTM > -1000000, EMA_12 > -1000000, '
        'EMA_50 > -1000000, ClosePrice > -1000000, ChangePCT > -1000000])')
LIMIT = 6000
MIN_N = 4500                 # 沪深A股约 5000 只，低于此视为接口截断/异常
MAX_CHURN_RATIO = 0.20       # 名单日变动超过 20% 视为接口异常，拒绝覆盖基石
REQUIRED = ('code', 'name', 'TotalMV', 'PE_TTM')   # 基石实际消费的 4 列
CORE_FIELDS = ('code', 'name', 'TotalMV', 'PE_TTM', 'ClosePrice',
               'EMA_12', 'EMA_50', 'Chg20D', 'Chg60D')
CODE_RE = re.compile(r'^(sh|sz|bj)\d{6}$')
SCOPE = ('sh', 'sz')        # 与 11-03 基线一致：沪深A股，不含北证
CLOSE = (15, 5)             # 15:05 之后才允许落收盘名单
SOURCE = 'westock-tool filter (零过滤全市场)'


def hidden_config_dirs():
    """用户主目录下的隐藏配置目录，逐项枚举而非用通配符（glob 默认不进隐藏目录）。"""
    home = os.path.expanduser('~')
    return [os.path.join(home, d) for d in sorted(os.listdir(home))
            if d.startswith('.') and os.path.isdir(os.path.join(home, d))]


def find_cli():
    # 优先用环境变量显式指定；否则在用户配置目录里自动发现 CLI
    explicit = os.environ.get('WESTOCK_JS')
    if explicit and os.path.isfile(explicit):
        return explicit
    for base in hidden_config_dirs():
        for sub in ('plugins', 'skills'):
            root = os.path.join(base, sub)
            if not os.path.isdir(root):
                continue
            hits = sorted(glob.glob(os.path.join(
                root, '**', 'westock-tool', 'scripts', 'index.js'), recursive=True))
            if hits:
                return hits[-1]
    raise FileNotFoundError('未找到 westock-tool CLI（scripts/index.js）')


def find_node():
    n = shutil.which('node')
    if n:
        return n
    for base in hidden_config_dirs():
        hits = sorted(glob.glob(os.path.join(
            base, 'binaries', 'node', 'versions', '*', 'bin', 'node')))
        if hits:
            return hits[-1]
    raise FileNotFoundError('未找到 node 可执行文件')


def after_close(now):
    return (now.hour, now.minute) >= CLOSE


def is_st(name):
    n = (name or '').upper().replace(' ', '')
    return ('ST' in n) or ('退' in (name or ''))


def validate(rows, old=None):
    """校验新快照；返回 (ok, msg, info)。绝不因为字段小瑕疵就放行半份数据。"""
    info = {}
    if not isinstance(rows, list) or not rows:
        return False, '接口未返回非空列表', info
    n = len(rows)
    info['n'] = n
    if n < MIN_N:
        return False, '仅 %d 只 < 下限 %d，疑似接口分页/截断' % (n, MIN_N), info
    if n >= LIMIT:
        return False, '返回 %d 只已达 limit=%d，疑似被截断，拒绝写入' % (n, LIMIT), info

    codes = set()
    bad_code = bad_field = bad_mv = 0

    def filled(v):
        return v is not None and str(v).strip() != ''

    for r in rows:
        if not isinstance(r, dict):
            bad_field += 1
            continue
        c = r.get('code')
        if not isinstance(c, str) or not CODE_RE.match(c):
            bad_code += 1
        elif c in codes:
            bad_code += 1
        codes.add(c)
        if any(k not in r or not filled(r[k]) for k in REQUIRED):
            bad_field += 1
        try:
            float(r.get('TotalMV') or 0)
        except (TypeError, ValueError):
            bad_mv += 1
    if bad_code or bad_field or bad_mv:
        return False, ('结构不合格：非法/重复代码 %d、缺字段 %d、市值不可解析 %d'
                       % (bad_code, bad_field, bad_mv)), info

    info['n_st'] = sum(1 for r in rows if is_st(r.get('name')))
    # 附带行情列（EMA/ClosePrice 等）只作观察：基石真正消费的是 REQUIRED 4 列，
    # EMA 由 server.Data() 用日K在内存复算，不读快照里的这一列。
    info['n_field_complete'] = sum(
        1 for r in rows if all(k in r and filled(r[k]) for k in CORE_FIELDS))

    if old:
        old_codes = {r['code'] for r in old if isinstance(r, dict) and r.get('code')}
        added, removed = sorted(codes - old_codes), sorted(old_codes - codes)
        info.update(prev_n=len(old_codes), added_n=len(added), removed_n=len(removed),
                    added_sample=added[:20], removed_sample=removed[:20])
        churn = len(added) + len(removed)
        # 基线是 3 天前的一次性档，累积新股/ST/退市变动会比单日大；
        # 但接口只返回半份数据的情况会被 MIN_N 与「剔除数占比」双重挡住。
        limit_churn = max(600, int(len(old_codes) * MAX_CHURN_RATIO))
        if churn > limit_churn or len(removed) > len(old_codes) * MAX_CHURN_RATIO:
            return False, ('名单变动 %d 只（+%d/-%d）超过阈值 %d，疑似接口异常，拒绝覆盖基石'
                           % (churn, len(added), len(removed), limit_churn)), info
    return True, 'ok', info


def _write(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
    os.replace(tmp, path)


def check_only():
    try:
        rows = json.load(open(SNAP, encoding='utf-8'))
    except Exception as e:
        print('现有名单快照读取失败：%s' % e)
        return 1
    ok, msg, info = validate(rows)
    print('名单快照校验：%s · %d 只 · ST/退 %d · 字段齐全 %d/%d · %s' % (
        SNAP, len(rows), info.get('n_st', 0), info.get('n_field_complete', 0),
        len(rows), '通过' if ok else '未通过：' + msg))
    print('mtime=%s' % datetime.datetime.fromtimestamp(
        os.path.getmtime(SNAP)).strftime('%Y-%m-%d %H:%M:%S'))
    try:
        m = json.load(open(META, encoding='utf-8'))
        print('meta：date=%s fetched_at=%s ok=%s' % (
            m.get('date'), m.get('fetched_at'), m.get('ok')))
    except Exception:
        print('meta：尚无（历史一次性档）')
    return 0 if ok else 1


def main():
    argv = sys.argv[1:]
    if '--check' in argv:
        return check_only()
    force = '--force' in argv
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now()

    if not after_close(now) and not force:
        print('未到收盘 15:05，按收盘口径不抓名单快照（--force 可人工补抓）')
        return 0
    try:
        meta = json.load(open(META, encoding='utf-8'))
    except Exception:
        meta = {}
    if not force and meta.get('date') == today and meta.get('attempted'):
        print('名单快照今日已尝试刷新（%s，ok=%s），跳过以避免重复拉' % (
            meta.get('fetched_at'), meta.get('ok')))
        return 0

    try:
        cli, node = find_cli(), find_node()
    except FileNotFoundError as e:
        print('名单快照刷新失败（保留旧档）：%s' % e)
        return 1
    cmd = [node, cli, 'filter', EXPR, '--limit', str(LIMIT), '--raw']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except Exception as e:
        print('名单快照刷新失败（保留旧档）：CLI 调用异常 %s' % e)
        return 1
    if r.returncode != 0 or not r.stdout.strip():
        print('名单快照刷新失败（保留旧档）：CLI rc=%d %s' % (
            r.returncode, (r.stderr or '')[-180:]))
        return 1
    try:
        d = json.loads(r.stdout)
        rows = d if isinstance(d, list) else d.get('data', {}).get('items', [])
        n_raw = len(rows)
        # 锁定既有 universe 口径：沪深A股 sh/sz，不含北证（避免刷新后
        # fetch_klines 去抓腾讯接口不支持的 bj 代码，也避免基石口径突变）。
        rows = [x for x in rows if isinstance(x, dict)
                and str(x.get('code', '')).startswith(SCOPE)]
    except Exception as e:
        print('名单快照刷新失败（保留旧档）：返回非 JSON %s' % e)
        return 1
    if not rows:
        print('名单快照刷新失败（保留旧档）：按 sh/sz 口径过滤后为空（n_raw=%d）' % n_raw)
        return 1

    try:
        old = json.load(open(SNAP, encoding='utf-8'))
    except Exception:
        old = None
    ok, msg, info = validate(rows, old)
    info['n_raw'] = n_raw
    info['scope'] = '/'.join(SCOPE) + '（与 11-03 基线一致，不含北证）'
    stamp = now.strftime('%Y-%m-%d %H:%M:%S')
    _closed = after_close(now)
    meta_out = {
        'date': today, 'fetched_at': stamp, 'attempted': True, 'ok': ok,
        'mode': 'close' if _closed else 'intraday_force',
        'complete': ok and _closed,      # 盘中 --force 补抓不算完整收盘档
        'source': SOURCE, 'expr': EXPR, 'limit': LIMIT,
        'n': len(rows), 'checks': msg, **info,
    }

    if not ok:
        # 拿到数据但校验不过：当天已尝试过，不再联网重试；旧档原样保留。
        meta_out['reject'] = msg
        _write(META, meta_out)
        print('名单快照校验未通过，保留旧档不覆盖：%s（今日不再重试）' % msg)
        return 0

    os.makedirs(BAK_DIR, exist_ok=True)
    if os.path.exists(SNAP):
        stem = 'snapshot_%s_%s' % (today, now.strftime('%H%M%S'))
        bak = os.path.join(BAK_DIR, stem + '.json')
        seq = 2                       # 同一秒多次 --force 也不覆盖上一份备份
        while os.path.exists(bak):
            bak = os.path.join(BAK_DIR, '%s_%d.json' % (stem, seq))
            seq += 1
        shutil.copy2(SNAP, bak)
        meta_out['backup'] = os.path.relpath(bak, BASE)
    else:
        meta_out['backup'] = None
    _write(SNAP, rows)
    _write(META, meta_out)
    print('名单快照 %s：%d 只（旧 %s 只，新增 %s / 剔除 %s，ST/退 %d）→ %s' % (
        today, len(rows), info.get('prev_n', '-'), info.get('added_n', 0),
        info.get('removed_n', 0), info.get('n_st', 0), SNAP))
    return 0


if __name__ == '__main__':
    sys.exit(main())
