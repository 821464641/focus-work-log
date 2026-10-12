# -*- coding: utf-8 -*-
"""「加油！专注工作记录」启动器。

双击 启动.bat 之后，真正干活的是这个文件。它负责：
 1. 判断是不是已经有一个实例在跑（在跑就直接打开浏览器，不重复启动）；
 2. 找一个空闲端口（从 8090 起；**永远跳过 3080**）；
 3. 用 pythonw.exe 把服务放在**独立的后台进程**里起来（关掉任何窗口都不影响使用）；
 4. 等它真的能访问了，再打开默认浏览器；
 5. 出任何问题都把原因写进「启动日志.txt」，并给用户一条能看懂的中文提示。

为什么服务要另起进程：如果服务跑在启动器自己的进程里，用户关掉那个控制台窗口服务就没了。
"""

import os
import sys
import time
import socket
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LOG = os.path.join(HERE, "启动日志.txt")
FORBIDDEN = {3080}
DEFAULT_PORT = 8090
# 自测/临时实例扫描范围：如果用户想用的端口上跑着的是这些端口上的服务，直接启动新实例
SELFTEST_PORTS = (8199,)


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            import datetime
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def port_open(port, timeout=0.35):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def is_our_service(port):
    """端口通还不够，要确认响应的是我们这个服务。"""
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/status" % port, timeout=5) as r:
            txt = r.read().decode("utf-8", "replace")
        return '"ok"' in txt and "records" in txt
    except Exception:                                  # noqa: BLE001
        return False


def find_free_port(start=DEFAULT_PORT, tries=110):
    for p in range(start, start + tries):
        if p in FORBIDDEN:
            continue
        if not port_open(p):
            return p
    raise RuntimeError("从 %d 开始的 %d 个端口都被占用了，请先关掉一些程序再启动。"
                       % (start, tries))


def pick_interpreter():
    """挑一个能真的跑起来的解释器（不弹命令行窗口）。

    优先用当前解释器旁边的 pythonw.exe（同一套安装必然可用），
    因为 WindowsApps 目录下的 pythonw.exe 可能只是个跳转占位符。
    """
    exe = os.path.abspath(sys.executable or "")
    cands = []
    if exe:
        d = os.path.dirname(exe)
        cands.append(os.path.join(d, "pythonw.exe"))
        cands.append(exe)
    return cands


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                  # noqa: BLE001
        pass

    no_browser = os.environ.get("FOCUS_NO_BROWSER") == "1"

    try:
        from app import db
        db.ensure_dirs()
        conn = db.init_db()
        preferred = db.get_setting_int(conn, "port", DEFAULT_PORT)
        db_path = db.DB_PATH
        conn.close()
    except Exception as e:                             # noqa: BLE001
        preferred, db_path = DEFAULT_PORT, "(未知)"
        log("初始化失败（将用默认端口 8090）：%s" % e)

    if preferred in FORBIDDEN or not (1024 <= preferred <= 65000):
        preferred = DEFAULT_PORT

    # ---------- 1) 已经有一个实例在跑？ ----------
    # 候选端口按从小到大排（默认 8090 优先）。这样即使设置里的端口被改过，
    # 也能认出"其实已经有一个跑着的实例"，不会重复起第二个。
    already = None
    for p in sorted({DEFAULT_PORT, preferred}):
        if p in FORBIDDEN:
            continue
        if port_open(p) and is_our_service(p):
            already = p
            break
    if already is not None:
        url = "http://127.0.0.1:%d/" % already
        print("「加油！专注工作记录」已经在运行了：%s" % url)
        print("直接为你打开浏览器，不会重复启动第二个。")
        log("检测到已在运行的实例，端口 %d，直接打开浏览器" % already)
        if not no_browser:
            os.startfile(url)                          # noqa: S606
        return 0
    if port_open(preferred):
        print("注意：%d 端口被别的程序占用，会自动换一个端口。" % preferred)
        log("%d 端口被别的程序占用" % preferred)

    # ---------- 2) 起独立的后台服务进程 ----------
    port = find_free_port(preferred)
    env = dict(os.environ)
    env["FOCUS_SERVER_PORT"] = str(port)
    env["FOCUS_OPEN_BROWSER"] = "0"                    # 浏览器由启动器开，避免开两个标签页
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    creation = 0x00000008 | 0x00000200                 # DETACHED_PROCESS | NEW_PROCESS_GROUP
    server_py = os.path.join(HERE, "app", "server.py")

    proc = None
    errors = []
    for exe in pick_interpreter():
        if not os.path.exists(exe):
            continue
        try:
            logf = open(LOG, "a", encoding="utf-8")
            logf.write("\n[%s] 启动：%s %s（端口 %d）\n"
                       % (time.strftime("%Y-%m-%d %H:%M:%S"), exe, server_py, port))
            logf.flush()
            proc = subprocess.Popen(
                [exe, server_py], cwd=HERE, env=env, creationflags=creation,
                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, close_fds=True)
        except OSError as e:
            errors.append("%s -> %s" % (exe, e))
            log("用 %s 启动失败：%s" % (exe, e))
            continue
        # 等最多 25 秒
        ok = False
        for _ in range(50):
            time.sleep(0.5)
            if port_open(port) and is_our_service(port):
                ok = True
                break
            if proc.poll() is not None:
                break
        if ok:
            break
        if proc.poll() is not None:
            errors.append("%s -> 进程立刻退出了（看启动日志.txt）" % exe)
            log("用 %s 起来的进程立刻退出，返回码 %s" % (exe, proc.returncode))
        else:
            errors.append("%s -> 25 秒内没有就绪" % exe)
            log("用 %s 起来的进程 25 秒内没有就绪" % exe)
            try:
                proc.kill()
            except OSError:
                pass

    url = "http://127.0.0.1:%d/" % port
    print("=" * 66)
    if proc is not None and port_open(port) and is_our_service(port):
        print("  加油！专注工作记录  已启动")
        print("  地址：%s" % url)
        if port != preferred:
            print("  注意：%d 端口被占用了，所以自动改用 %d。" % (preferred, port))
        print("  数据文件：%s" % db_path)
        print()
        print("  服务在后台独立运行，这个窗口可以直接关掉，不影响使用。")
        print("  想停止软件：打开任务管理器结束 pythonw.exe（或重启电脑）。")
        print("=" * 66)
        log("启动成功：%s" % url)
        if not no_browser:
            try:
                os.startfile(url)                      # noqa: S606
                print("  已为你打开默认浏览器；如果没反应，请手动把上面的地址复制到浏览器。")
            except OSError as e:
                print("  自动打开浏览器失败（%s），请手动把上面的地址复制到浏览器。" % e)
        return 0

    print("  启动失败。")
    print("  详细原因已写入：%s" % LOG)
    if errors:
        print("  尝试过的方式：")
        for e in errors:
            print("    - %s" % e)
    print("  常见原因：没有装 Python 3；或者安全软件阻止了程序运行。")
    print("=" * 66)
    log("启动失败：%s" % errors)
    return 1


if __name__ == "__main__":
    sys.exit(main())
