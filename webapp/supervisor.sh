#!/bin/bash
# EXPMA 工作台守护：确保 server.py + alertd.py 常驻运行（被 LaunchAgent 与 .app 共用）
# server.py  = 策略工作台 HTTP 服务 (127.0.0.1:8765)
# alertd.py  = 止损/止盈/移动止盈 到价通知（macOS 通知中心，盘中 9:25-15:05 每60秒）
# tlbreak_alert.py = 三线穿阳·破20日高 盘中预触发
# win80_alert.py   = 80%胜率规则 买卖弹窗（开盘买 / +2.5%止盈 / 20日到期）
W="$HOME/WorkBuddy/Claw/webapp"
LOG="$W/launchd.log"
PYTHON="$(command -v python3)"

ensure() { # $1=pgrep特征串 其余=启动命令
  # 注意：server.py 命令行尾部带端口参数(8765)，$ 锚点会导致永远匹配不上、
  # 每15s重复拉起实例(加载40s数据后bind失败退出)。特征串不加 $，用唯一路径片段匹配。
  pgrep -f "$1" >/dev/null 2>&1 || {
    shift
    nohup "$@" >>"$LOG" 2>&1 &
  }
}

while true; do
  ensure "Claw/webapp/server\.py"        "$PYTHON" "$W/server.py" 8765
  ensure "Claw/webapp/alertd\.py"        "$PYTHON" "$W/alertd.py"
  ensure "Claw/webapp/tlbreak_alert\.py" "$PYTHON" "$W/tlbreak_alert.py"
  ensure "Claw/webapp/win80_alert\.py"   "$PYTHON" "$W/win80_alert.py"
  sleep 15
done
