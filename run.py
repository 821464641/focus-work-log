# -*- coding: utf-8 -*-
"""启动入口：检查环境 -> 起本地服务 -> 自动打开浏览器。

用 pythonw.exe 运行本文件就不会出现命令行窗口（见 启动.bat）。
"""

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LOG = os.path.join(HERE, "启动日志.txt")


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            import datetime
            f.write("[%s] %s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def main():
    # 保证中文输出不炸（有些环境默认 GBK）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                                  # noqa: BLE001
        pass
    try:
        from app import server
        server.serve()
    except Exception as e:                             # noqa: BLE001
        tb = traceback.format_exc()
        log("启动失败：%s\n%s" % (e, tb))
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "「加油！专注工作记录」启动失败了。\n\n"
                "原因：%s\n\n"
                "详细日志已经写到：\n%s\n\n"
                "最常见的两个原因：\n"
                "1) 电脑上没装 Python 3（到 python.org 下载安装，安装时勾选 Add Python to PATH）；\n"
                "2) 8090~8199 端口都被占用（关掉一些程序再试）。\n\n"
                "你也可以手工在这个目录里运行：python run.py" % (e, LOG),
                "启动失败", 0x10)
        except Exception:                              # noqa: BLE001
            pass
        print("启动失败：%s\n%s" % (e, tb))
        input("按回车键关闭……")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
