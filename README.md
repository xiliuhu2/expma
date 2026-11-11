# EXPMA · A 股策略工作台

一套**纯本地运行**的 A 股量化研究/盯盘系统：全市场日 K 缓存 + 多策略筛选 + 回测验证 + 收盘跑批日报 + 盘中桌面提醒，前端是一个单页 Web 工作台。

> **免责声明**：本仓库是个人研究记录与数据筛选工具，所有信号、池子、回测结论均**不构成投资建议**。回测受样本区间、当前成份股回溯偏差、参数选择影响；未通过「基准对比 / 无未来函数 / 分年份稳健性」三关检验的结论一律只标为**描述性 / 候选**，不写成操作纪律。据此交易，风险自负。

---

## 快速开始

### 环境

- macOS（桌面提醒弹窗依赖 AppKit）、Python 3.9+
- Web 服务与绝大多数脚本**只用标准库**，无需 `pip install`
- 少数辅助脚本用到 `playwright`（雪球页面抓取，可选）；弹窗需要 Xcode Command Line Tools 的 `clang`

### 路径约定

脚本里把仓库根目录写死为 `~/WorkBuddy/Claw`（`server.py` 的 `ROOT/WEB`、`daily_run.py` 的 `BASE`、`supervisor.sh` 的 `W`）。请把仓库放在这个位置，或自行替换这些常量。

```bash
git clone https://github.com/xiliuhu2/expma.git ~/WorkBuddy/Claw
```

### 启动工作台

```bash
python3 ~/WorkBuddy/Claw/webapp/server.py 8765
# 浏览器打开 http://127.0.0.1:8765
```

`server.py` 每次请求都重新读 `index.html`，改前端**不用重启**；改 `server.py` 的 API 返回字段后必须重启，否则常驻的提醒守护会拿到旧字段而报错。

### 收盘跑批

```bash
python3 ~/WorkBuddy/Claw/webapp/daily_run.py
```

stdout 输出一条 JSON 摘要，并生成：

- `daily/日报_YYYY-MM-DD.md`（regime、情绪、涨停、六策略、精选池、三线池、龙头战法、转势首叉等）
- `daily/三线池与跟踪_YYYY-MM-DD.md`、`daily/龙头战法_YYYY-MM-DD.md`
- 刷新 `backtest/klines/` 全市场日 K、`backtest/indices/` 指数等

环境变量：

| 变量 | 作用 |
|---|---|
| `SKIP_FETCH=1` | 离线模式：跳过所有联网抓取/重启/DQ 闸门，用现有缓存重算信号与报告（盘中复盘或修报告用） |
| `FORCE_FETCH=1` | 强制重抓，绕过「当日一次性」抓取守卫 |

`data_fetch_state.py` 的守卫保证同一天指数/债券/行业/概念/沪深300/名单快照/5000 只日 K 不会被多个定时任务重复拉两遍，失败不记成功，下次跑批自动重试。

### 盘中提醒守护

```bash
bash ~/WorkBuddy/Claw/webapp/supervisor.sh
```

每 15 秒检查并拉起 4 个常驻进程：`server.py`（HTTP）、`alertd.py`（止盈止损到价通知）、`tlbreak_alert.py`（三线穿阳 · 破 20 日高）、`win80_alert.py`（80% 胜率规则弹窗）。

桌面闪窗是原生 AppKit 小窗，二进制缺失时重新编译：

```bash
cd ~/WorkBuddy/Claw/webapp
clang -fobjc-arc -framework AppKit -o flashnotify flashnotify.m
```

`flashwin.py` 提供 `FLASH_ENGINE=appkit|tk` 开关，默认 `appkit`；旧 tkinter 实现在本机 macOS 26 上不合成窗口内容，仅留作兜底，不要设回默认。

---

## 目录结构

| 路径 | 内容 |
|---|---|
| `webapp/index.html` | 单页工作台（14 个页签，K 线图表 / 筛选 / 池子 / 回测 / 提醒） |
| `webapp/server.py` | 纯标准库 HTTP 服务与全部 `/api/*` 接口 |
| `webapp/daily_run.py` | 收盘跑批编排入口 |
| `webapp/tlbreak_alert.py`、`win80_alert.py`、`alertd.py` | 三条盘中提醒链与到价通知 |
| `webapp/flashnotify.m`、`flashwin.py` | 原生桌面弹窗 |
| `webapp/supervisor.sh` | 进程守护 |
| `webapp/pool.py`、`tlpool.py`、`dragon.py`、`firstcross.py` 等 | 各策略池与信号计算 |
| `backtest/fetch_*.py` | 行情/指数/概念/行业/名单抓取 |
| `backtest/*_backtest.py`、`*_mining.py`、`*_verify.py` | 回测、参数挖掘、样本外验证 |
| `backtest/klines/` | 全市场前复权日 K（约 5000 只，JSON） |
| `backtest/indices/` | 上证 / 沪深300 / 创业板 / 科创50 / 北证50 指数日线 |
| `daily/` | 每日日报与专题报告（Markdown） |
| `webapp/*.jsonl`、`alert_archive_bak/` | 盘中提醒的滚动档案与历史归档 |
| `dist/*.zip` | 11-05 的 skill 封装包，**与当前代码不同步**，仅供存档参考 |

---

## 工作台页签

| # | 页签 | # | 页签 |
|---|---|---|---|
| p1 | K 线 & 信号 | p8 | ⭐ 三线池 |
| p2 | 策略筛选 | p9 | 🐉 龙头战法 |
| p3 | Regime & 回测结论 / 指数监控 | p10 | 🔀 背离金叉 |
| p4 | 观察池 · 前向跟踪 | p11 | 📟 提醒记录 |
| p5 | 涨停复盘 | p12 | ⚡ 异动前瞻 |
| p6 | 交易计划 | p13 | 🎯 80% 胜率 |
| p7 | 沪深300 池 | p14 | 🌱 转势首叉 |

图表交互：滚轮以光标为锚缩放、拖拽平移、Shift 框选放大、缩略导航条、十字光标 + OHLC 数据条、双击锁定中心点后 ↑/↓ 连续缩放、点击日期联动大图弹窗；弹窗尺寸/区间/模式均记忆在 localStorage。

主要接口：`/api/screen`、`/api/pool`、`/api/tlpool`、`/api/dragon`、`/api/firstcross`、`/api/regime`、`/api/emotion`、`/api/zt`、`/api/indices`、`/api/kline`、`/api/idx_bottom_study`、`/api/idx_trend_hold`、`/api/alert_records` 等。注意 `/api/screen` 默认 `mvmax=100`（小盘回测口径），收盘跑批显式传 `mvmax=99999` 覆盖全市场。

---

## 策略口径要点

- **三线穿阳 · 破 20 日高**（盘中链 1，当前唯一启用的穿线提醒）：开盘吞没三线 `open <= min(EMA12, EMA50, MA89)`；当日最高突破 `high > open 且 high > max(三线最高, 近 20 日最高收盘)`；**只看查询那一刻的快照**，含冲高回落，不等收盘确认。收盘确认归三线池/日报口径。
- 预选池距离固定 4%；**严格优先于命中数**，0 提醒日属正常，不为凑数放宽判据。
- 「⑥穿线阳」盘中链 2 默认停用：`tlbreak_alert.py` 中 `CROSSLINE_ENABLED=0`（设环境变量 `1` 才启用），代码与线位 API 全部保留。历史提醒记录已归档在 `webapp/alert_archive_bak/`，数据未删除。
- **指数是环境闸门，不是择时标的**：`cross3_idx_timing.py` 只用触发日及之前的指数状态做过滤。
- 研究结论（详见 `backtest/idx_trend_hold_result.json` 与 `daily/`）：EXPMA12 偏离度与未来收益在 1~10 日呈均值回归、20 日后转动量；所谓「均线回归」大部分靠 EMA12 自身下移而非价格涨回；模式 B 的价值在状态过滤，不在金叉事件择时。

所有结论都要过**三关**：① 与「同期任意日买入、同持有期」的基线比较，不看裸收益；② 信号当日收盘确认、次日开盘成交，标签不得用未来数据；③ 分年份稳健性（正收益年数、最强两年贡献、前后段样本外）。网格搜索的最优参数尤其要查样本外与参数敏感性。

---

## 数据与隐私

入库的是**策略产物**：`daily/` 日报、`backtest/klines/` 日 K、提醒档案 `*.jsonl`。

以下本地私有数据由 `.gitignore` 排除，**从未进入任何提交**，公开仓库也请不要提交：

| 类型 | 例子 |
|---|---|
| 登录会话 | `webapp/xq_state.json`（雪球 cookies） |
| 真实持仓/交易 | `webapp/trades.json`、`win80.json`、`pool.json` 等运行时状态 |
| 本机痕迹 | `backtest/fetch_state.json`（含绝对路径）、`*.log`、`.workbuddy/` |
| 个人运营笔记 | `dist/发布信息卡.md` |

行情数据来自公开接口，仅作研究用途，不保证准确性与实时性；请自行核验后再使用。

---

## 许可

未指定开源许可证，代码仅供阅读与个人研究参考。
