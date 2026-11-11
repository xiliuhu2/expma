#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""共享闪屏提醒窗（供 alertd.py / tlbreak_alert.py / win80_alert.py 使用）。

主引擎：原生 AppKit 二进制 `flashnotify`（源码 flashnotify.m，编译命令见该文件头）。
无边框置顶窗，主色/白色交替闪烁，到时自动关闭，点击窗体 → 关窗 + Chrome 打开 URL。
每次弹出/退出原因记录 webapp/tlbreak_flash.log，便于排查「窗不见了」。

2026-11-11 换引擎原因：tkinter 子进程在本机 macOS 上窗口内容不合成（只剩白色
"tk" 空窗），且每个 Python.app 进程都会在 Dock 冒一个火箭图标。AppKit 版由系统
绘制内容、floating 层级置顶、Accessory 策略不占 Dock/菜单栏。二进制缺失时下面的
FLASH_SRC 仅作兜底（已知该环境下内容可能显示不全），不要用回它当主路径。"""
import os, subprocess, sys, time

FLASH_SRC = r'''
import datetime, os, subprocess, sys, tkinter as tk
title, msg, url = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else '')
color = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else '#c02626'   # 主色：止损绿/止盈红等
try:
    DUR = float(sys.argv[5])          # 显示时长（秒），由父进程传入
except Exception:
    DUR = 12.0
try:
    SLOT = int(sys.argv[6])           # 同批多窗纵向错位槽位 0-3
except Exception:
    SLOT = 0
LOGF = os.path.expanduser('~/WorkBuddy/Claw/webapp/tlbreak_flash.log')
def log(why):
    try:
        open(LOGF, 'a').write('%s %s %s\n' % (
            datetime.datetime.now().strftime('%H:%M:%S'), why, title[:40]))
    except Exception:
        pass
r = tk.Tk()
r.attributes('-topmost', True)
try:                                   # 弹窗同时出声，避免盯盘时漏看
    subprocess.Popen(['afplay', '/System/Library/Sounds/Glass.aiff'])
except Exception:
    try:
        subprocess.Popen(['osascript', '-e', 'beep'])
    except Exception:
        pass
r.overrideredirect(True)
r.configure(cursor='hand2')
lbl1 = tk.Label(r, text=title, font=('PingFang SC', 13, 'bold'), wraplength=300)
lbl2 = tk.Label(r, text=msg, font=('PingFang SC', 10), wraplength=300, justify='left')
lbl3 = tk.Label(r, text='👉 点击在 Chrome 查看', font=('PingFang SC', 9))
lbl1.pack(pady=(8, 2), padx=14)
lbl2.pack(padx=14)
lbl3.pack(padx=14, pady=(4, 8))
st = {'hot': True, 't': 0.0, 'why': None}
def paint():
    hot = st['hot']
    bg = color if hot else '#ffffff'
    for x in (r, lbl1, lbl2, lbl3):
        x.configure(bg=bg)
    lbl1.configure(fg='#ffffff' if hot else '#1a1a1a')
    lbl2.configure(fg='#ffe08a' if hot else '#333333')
    lbl3.configure(fg='#ffffff' if hot else '#1d4ed8')
def place():
    r.update_idletasks()
    w = r.winfo_reqwidth()
    h = r.winfo_reqheight()
    r.geometry('+%d+%d' % (r.winfo_screenwidth() - w - 16, 48 + (SLOT % 4) * 132))
    r.lift()
def openurl(e):
    st['why'] = 'click→关闭并打开Chrome' if url else 'click→关闭(无链接)'
    r.destroy()
    if url:
        try:
            subprocess.Popen(['open', '-a', 'Google Chrome', url])
        except Exception:
            subprocess.Popen(['open', url])       # 无 Chrome 走默认浏览器
def tick():
    st['t'] += 0.4
    if st['t'] > DUR:            # 到时自动关，点击可提前关+跳转
        st['why'] = 'timeout→%d秒自动关' % int(DUR)
        r.destroy()
        return
    st['hot'] = not st['hot']
    paint()
    r.after(400, tick)
r.bind('<Button-1>', openurl)
r.after(50, place)
r.after(900, place)          # 显示后重申一次，防 macOS 把无边框窗挪回左上角
log('start')
paint()
tick()
r.mainloop()
log(st['why'] or 'exit-unknown→mainloop结束但非点击/超时')
'''


RED = '#c02626'      # 止盈/买入信号（红=看多动作）
GREEN = '#0d8a45'    # 止损（绿=离场保护）

DEFAULT_DUR = float(os.environ.get('FLASH_SECONDS', '12'))   # 停留秒数（原 5 秒太短，易漏看）
_SLOT = [0]
_LAST = [0.0]


def _next_slot():
    """连续提醒在 12 秒内依次下移，避免多条互相遮挡。"""
    now = time.time()
    _SLOT[0] = 0 if now - _LAST[0] > DEFAULT_DUR else (_SLOT[0] + 1) % 4
    _LAST[0] = now
    return _SLOT[0]


_FLASH_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flashnotify')


def spawn_flash(title, msg, url='', color=RED, dur=None, slot=0):
    """优先用原生 AppKit 闪窗（flashnotify 二进制）；二进制缺失时回退 tkinter 版本。

    2026-11-11：tkinter 在本机只画得出白色空窗（内容不合成），且每个 Python.app
    子进程都会在 Dock 冒一个火箭图标。AppKit 版内容可靠显示 + Accessory 不占 Dock。
    """
    if os.path.exists(_FLASH_BIN):
        try:                                   # 提示音由父进程放，不依赖子进程音频
            subprocess.Popen(['afplay', '/System/Library/Sounds/Glass.aiff'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        return subprocess.Popen([_FLASH_BIN, title, msg, url or '', color,
                                 str(dur or DEFAULT_DUR), str(slot)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    return subprocess.Popen([sys.executable, '-c', FLASH_SRC, title, msg, url or '', color,
                             str(dur or DEFAULT_DUR), str(slot)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)   # 脱离父进程组，防命令结束时被连带杀掉


def flash(title, msg, url='', color=RED, dur=None):
    """弹闪屏窗（带启动即崩重试）。color=闪烁主色，默认红；dur=停留秒数。"""
    slot = _next_slot()
    try:
        p = spawn_flash(title, msg, url, color, dur, slot)
        time.sleep(1.5)
        rc = p.poll()
        # 退出码 0 = 用户点击关闭 / 到时正常退出，不能当崩溃重试（否则会重复弹窗）
        if rc is not None and rc != 0:
            p = spawn_flash(title, msg, url, color, dur, slot)   # 真·启动即崩 → 立即重试一次
    except Exception as e:
        print('flash error:', e)
