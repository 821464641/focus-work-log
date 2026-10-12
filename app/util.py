# -*- coding: utf-8 -*-
"""通用小工具：时长格式化 / 解析、日期与周区间计算。

本文件不依赖任何第三方库，只用 Python 标准库。
所有时长在数据库里一律存「整数分钟」，显示一律用 H:MM。
"""

import re
import datetime

# ---------------------------------------------------------------- 常量

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

DEFAULT_CATEGORIES = [
    # (名称, 是否计入工作时间, 排序)
    ("科研", 1, 10),
    ("会议", 1, 20),
    ("阅读", 0, 30),
    ("运动", 0, 40),
    ("娱乐", 0, 50),
]

PRODUCT_TYPES = ["文档", "代码", "实验", "图表", "其他"]

# 原表里「有耗时但没写任务名」的格子统一用这个占位任务名（用户 2026-10-12 确认）
PLACEHOLDER_TASK = "（原表未写任务名）"


# ---------------------------------------------------------------- 时长

def fmt_hm(minutes):
    """整数分钟 -> 'H:MM'（如 317 -> '5:17'）。负数取绝对值前加负号。"""
    if minutes is None:
        return ""
    m = int(minutes)
    sign = "-" if m < 0 else ""
    m = abs(m)
    return "%s%d:%02d" % (sign, m // 60, m % 60)


def fmt_hours(minutes, ndigits=3):
    """整数分钟 -> 小数小时（用于导出 Excel，不用于界面显示）。"""
    if minutes is None:
        return 0.0
    return round(int(minutes) / 60.0, ndigits)


def parse_duration(text):
    """把用户输入解析成整数分钟。

    接受：'1:46'、'1：46'、'1h46m'、'1小时46分'、'106'（分钟）、'1.5h'、'90m'、'3:5'
    返回 (分钟数, 错误信息)；解析成功时错误信息为 None。
    """
    if text is None:
        return None, "时长不能为空"
    s = str(text).strip()
    if s == "":
        return None, "时长不能为空"
    # 全角字符归一
    s = (s.replace("：", ":").replace("．", ".").replace("，", "")
         .replace("小时", "h").replace("分钟", "m").replace("分", "m")
         .replace("时", "h").replace(" ", ""))

    # 1) H:MM / H:MM:SS
    m = re.fullmatch(r"(\d+):([0-5]?\d)(?::([0-5]?\d))?", s)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        sec = int(m.group(3) or 0)
        total = h * 60 + mi + (1 if sec >= 30 else 0)
        if total <= 0:
            return None, "时长必须大于 0"
        return total, None

    # 2) 纯数字 -> 分钟
    if re.fullmatch(r"\d+", s):
        total = int(s)
        if total <= 0:
            return None, "时长必须大于 0"
        return total, None

    # 3) 小数 + 单位（h/m，或 h/m 组合）
    m = re.fullmatch(r"(\d+(?:\.\d+)?)h(?:(\d+(?:\.\d+)?)m?)?", s)
    if m:
        total = float(m.group(1)) * 60 + (float(m.group(2)) if m.group(2) else 0.0)
        total = int(round(total))
        if total <= 0:
            return None, "时长必须大于 0"
        return total, None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)m", s)
    if m:
        total = int(round(float(m.group(1))))
        if total <= 0:
            return None, "时长必须大于 0"
        return total, None

    # 4) 不带单位的小数 -> 按小时理解（保护：'1.5' 更像 1.5 小时）
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        total = int(round(float(s) * 60))
        if total <= 0:
            return None, "时长必须大于 0"
        return total, None

    return None, ("看不懂这个时长：%r。请用 H:MM 写法（如 1:46），"
                  "也接受 106（分钟）、1h46m、1.5h 这类写法。" % text)


# ---------------------------------------------------------------- 日期

def today_str():
    return datetime.date.today().isoformat()


def parse_day(text):
    """字符串 -> date；失败抛 ValueError（中文提示）。"""
    if isinstance(text, datetime.date):
        return text
    s = str(text or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    raise ValueError("看不懂这个日期：%r，请用 2025-08-21 这种写法。" % text)


def weekday_cn(day):
    """date -> '周一'...'周日'"""
    d = parse_day(day)
    return WEEKDAY_CN[d.weekday()]


def week_range(day):
    """给定日期所在的一周（周一~周日），返回 (周一date, 周日date)。"""
    d = parse_day(day)
    mon = d - datetime.timedelta(days=d.weekday())
    return mon, mon + datetime.timedelta(days=6)


def month_range(day):
    """给定日期所在自然月，返回 (1号date, 月末date)。"""
    d = parse_day(day)
    first = d.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return first, nxt - datetime.timedelta(days=1)


def days_between(start, end):
    """闭区间 [start, end] 的所有日期（升序）。"""
    s, e = parse_day(start), parse_day(end)
    out = []
    cur = s
    while cur <= e:
        out.append(cur)
        cur += datetime.timedelta(days=1)
    return out


def iso_week_label(day):
    """'2025-W31' 形式的 ISO 周标签。"""
    d = parse_day(day)
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def now_local():
    """本地当前时间（秒级），返回 datetime。"""
    return datetime.datetime.now().replace(microsecond=0)


def iso(dt):
    """datetime -> 'YYYY-MM-DD HH:MM:SS'"""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.datetime.fromisoformat(dt)
    return dt.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(text):
    """'YYYY-MM-DD HH:MM:SS' 或 'YYYY-MM-DDTHH:MM' -> datetime。"""
    if text in (None, ""):
        return None
    if isinstance(text, datetime.datetime):
        return text.replace(microsecond=0)
    s = str(text).strip().replace("T", " ")
    for pat in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, pat).replace(microsecond=0)
        except ValueError:
            continue
    raise ValueError("看不懂这个时间：%r，请用 2025-08-21 14:30 这种写法。" % text)


def seconds_of_day(text):
    """把 'HH:MM' 或 'HH:MM:SS' 解析成「当天第几秒」；看不懂就抛 ValueError（中文提示）。

    用于补录时填写"起始时间/终止时间"（只填时刻，日期由表单里的日期决定）。
    """
    if text is None:
        return None
    s = str(text).strip().replace("：", ":")
    if s == "":
        return None
    m = re.fullmatch(r"(\d{1,2}):([0-5]?\d)(?::([0-5]?\d))?", s)
    if not m:
        raise ValueError("看不懂这个时间：%r。请用 24 小时制写法，例如 9:30 或 14:05。" % text)
    h, mi = int(m.group(1)), int(m.group(2))
    sec = int(m.group(3) or 0)
    if h > 23:
        raise ValueError("小时数不能超过 23（你填的是 %d）。请用 24 小时制，例如 9:30。" % h)
    return h * 3600 + mi * 60 + sec


def fmt_clock_of_day(seconds):
    """当天第几秒 -> 'HH:MM'（用于界面回填与错误提示）。"""
    s = int(seconds)
    return "%02d:%02d" % (s // 3600, (s % 3600) // 60)


# ---------------------------------------------------------------- 区间并集

def union_length_minutes(intervals):
    """一组 (start_datetime, end_datetime) 的并集总长度（分钟，四舍五入）。

    规格 5.1「实际时长（去重）」就用这个函数：并行计时的重叠部分只算一次。
    """
    if not intervals:
        return 0
    spans = sorted((s, e) for s, e in intervals if e > s)
    if not spans:
        return 0
    total = datetime.timedelta(0)
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e:                      # 有重叠或相接 -> 合并
            if e > cur_e:
                cur_e = e
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return int(round(total.total_seconds() / 60.0))
