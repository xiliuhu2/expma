#!/usr/bin/env python3
"""xqsim.py — 雪球模拟盘自动交易（基于 Playwright 持久化登录）

用法:
  python3 xqsim.py login    # 弹出浏览器，人工登录雪球一次，会话保存到本地
  python3 xqsim.py probe    # 登录态下探测组合页面结构，输出 HTML 供分析
  python3 xqsim.py daemon   # (待实现) 轮询 tlpool.json A级信号 -> 自动调仓
"""
import json
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(BASE, "xq_profile")
STATE = os.path.join(BASE, "xq_state.json")
LOG = os.path.join(BASE, "xqsim.log")
OUT = os.path.join(BASE, "xqsim_probe")


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def launch(headed=True):
    """启动持久化登录上下文"""
    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(
        PROFILE,
        headless=not headed,
        viewport={"width": 1440, "height": 900},
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        locale="zh-CN",
    )
    return p, ctx


def goto_retry(page, url, tries=3, **kw):
    last = None
    for i in range(tries):
        try:
            return page.goto(url, **kw)
        except Exception as e:
            last = e
            log(f"goto {url} 第{i+1}次失败: {e}")
            time.sleep(3)
    raise last


def verify_login(ctx):
    """实测登录：开新页访问 /mine，看是否还有「登录/注册」入口"""
    p2 = ctx.new_page()
    try:
        p2.goto("https://xueqiu.com/mine", wait_until="domcontentloaded", timeout=20000)
        time.sleep(2)
        html = p2.content()
        logged = ("登录/注册" not in html) and ("/signin" not in p2.url)
        return logged
    except Exception as e:
        log(f"verify 页面异常: {e}")
        return False
    finally:
        try:
            p2.close()
        except Exception:
            pass


def cmd_login():
    p, ctx = launch(headed=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    goto_retry(page, "https://xueqiu.com", wait_until="domcontentloaded")
    page.bring_to_front()
    try:
        page.evaluate("window.moveTo(0,0); window.resizeTo(1440,900);")
    except Exception:
        pass
    log("浏览器已打开雪球首页并置顶，请在窗口内完成登录（扫码/账号均可），最多等待 15 分钟...")
    # 若 profile 里已有登录态（例如上次窗口里登录过），直接确认并进入探测
    if verify_login(ctx):
        ok = True
        log("检测到已有登录态，无需重复扫码")
    base = {c["name"]: c["value"] for c in ctx.cookies("https://xueqiu.com")}
    if not ok:
        for _ in range(300):
            try:
                if page.is_closed():
                    page = ctx.new_page()
                    goto_retry(page, "https://xueqiu.com", wait_until="domcontentloaded")
                    page.bring_to_front()
            except Exception:
                pass
            cur = {c["name"]: c["value"] for c in ctx.cookies("https://xueqiu.com")}
            changed = any(cur.get(k) != base.get(k)
                          for k in ("xq_a_token", "xqat", "xq_id_token", "xq_u_id", "xq_token_login"))
            if changed and verify_login(ctx):
                ok = True
                break
            time.sleep(3)
    if ok:
        ctx.storage_state(path=STATE)
        log(f"登录成功，会话已保存：{STATE}")
        log("开始探测组合相关页面...")
        dump(page, "https://xueqiu.com/cubes/list", "cubes_list")
        dump(page, "https://xueqiu.com/cubes/create", "cubes_create")
        dump(page, "https://xueqiu.com/mine", "mine")
    else:
        # 诊断：超时时截图 + 导出 cookie 名单
        os.makedirs(OUT, exist_ok=True)
        try:
            page.screenshot(path=os.path.join(OUT, "login_timeout.png"))
            log(f"超时截图已保存: {OUT}/login_timeout.png")
        except Exception as e:
            log(f"截图失败: {e}")
        names = sorted({c["name"] for c in ctx.cookies("https://xueqiu.com")})
        log(f"当前 cookie 名单: {names}")
        log("等待登录超时，未保存会话")
    ctx.close()
    p.stop()
    return 0 if ok else 1


def dump(page, url, name):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3)
        html = page.content()
        os.makedirs(OUT, exist_ok=True)
        path = os.path.join(OUT, name + ".html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        shot = os.path.join(OUT, name + ".png")
        page.screenshot(path=shot, full_page=False)
        log(f"已保存 {path} ({len(html)} bytes) + 截图 {shot}")
    except Exception as e:
        log(f"dump {url} 失败: {e}")


def cmd_probe():
    p, ctx = launch(headed=True)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    if not verify_login(ctx):
        log("尚未登录，请先运行: python3 xqsim.py login")
        ctx.close()
        p.stop()
        return 1
    log("登录态有效，开始探测组合相关页面...")
    dump(page, "https://xueqiu.com/cubes/list", "cubes_list")
    dump(page, "https://xueqiu.com/cubes/create", "cubes_create")
    dump(page, "https://xueqiu.com/mine", "mine")
    ctx.close()
    p.stop()
    return 0


def cmd_daemon():
    log("daemon 待实现（探测完成后编写）")
    return 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    sys.exit({"login": cmd_login, "probe": cmd_probe, "daemon": cmd_daemon}[cmd]())
