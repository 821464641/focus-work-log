# -*- coding: utf-8 -*-
"""周报 / 月报生成（规格 6.2）。

* 周报 = 周一~周日；月报 = 自然月。
* 打开软件时自动检测"上一个已结束的周期"是否已生成，没生成就补生成并存进 reports\\。
* 内容每一项都必须有：周期起止、有记录天数、自然日天数、记录率、总时长（去重）、
  工作时间（科研+会议，去重）、各大类合计、两种平均每天、Top5 二级任务、
  目标达标天数/总天数、连续达标天数、与上一周期对比、一段可直接复制的文字总结。
* 另外支持任意时间段生成（本周/本月/自定义）。
"""

import os
import datetime

from . import db, util, stats, goals


def _prev_week_range(day):
    mon, sun = util.week_range(day)
    return mon - datetime.timedelta(days=7), sun - datetime.timedelta(days=7)


def _prev_month_range(day):
    first, last = util.month_range(day)
    prev_last = first - datetime.timedelta(days=1)
    return util.month_range(prev_last)


def _diff_text(cur, prev, unit="小时", nd=3):
    """与上一周期的对比（差值 + 百分比）。"""
    if prev in (None, 0):
        return "上一周期没有数据，无法比较"
    d = cur - prev
    pct = d / prev * 100.0
    sign = "+" if d >= 0 else ""
    return "%s%.*f%s（%s%.1f%%）" % (sign, nd, d, unit, sign, pct)


def build_report(conn, start_day, end_day, kind="custom", title=None):
    """生成一份报表数据（dict）。kind: week / month / custom"""
    s, e = util.parse_day(start_day), util.parse_day(end_day)
    rng = stats.range_with_goals(conn, s.isoformat(), e.isoformat())
    top = rng["top_tasks"]
    g = rng.get("goal") or {}

    # 上一周期
    prev_start = prev_end = None
    if kind == "week":
        prev_start, prev_end = _prev_week_range(s)
    elif kind == "month":
        prev_start, prev_end = _prev_month_range(s)
    prev = None
    if prev_start:
        prev = stats.range_with_goals(conn, prev_start.isoformat(), prev_end.isoformat())

    rep = {
        "kind": kind,
        "title": title or {
            "week": "%s ~ %s 周报" % (s.isoformat(), e.isoformat()),
            "month": "%s 月报" % s.strftime("%Y-%m"),
            "custom": "%s ~ %s 总结" % (s.isoformat(), e.isoformat()),
        }.get(kind, "总结"),
        "start": s.isoformat(), "end": e.isoformat(),
        "range": rng, "top": top, "goal": g,
        "prev": prev,
        "prev_range": (prev_start.isoformat(), prev_end.isoformat()) if prev_start else None,
    }
    rep["markdown"] = render_markdown(conn, rep)
    return rep


def render_markdown(conn, rep):
    rng = rep["range"]
    g = rep["goal"] or {}
    top = rep["top"]
    L = []
    A = L.append

    A("# %s" % rep["title"])
    A("")
    A("| 项目 | 数值 |")
    A("|---|---|")
    A("| 周期 | %s ~ %s |" % (rng["start"], rng["end"]))
    A("| 有记录天数 | %d 天 |" % rng["record_days"])
    A("| 自然日天数 | %d 天 |" % rng["natural_days"])
    A("| 记录率 | %.2f%%（%d/%d） |" % (rng["record_rate"] * 100, rng["record_days"],
                                        rng["natural_days"]))
    A("| 总时长（去重） | %s（%.3f 小时） |" % (util.fmt_hm(rng["actual_minutes"]), rng["actual_h"]))
    A("| 工作时间（科研+会议，去重） | %s |" % util.fmt_hm(rng["work_minutes"]))
    A("| 投入时长之和（不去重，参考） | %s |" % util.fmt_hm(rng["invested_minutes"]))
    A("| 平均每天（记录日） | %.3f 小时 |" % rng["avg_per_record_day_h"])
    A("| 平均每天（自然日） | %.3f 小时 |" % rng["avg_per_natural_day_h"])
    A("")
    A("## 各大类合计")
    A("")
    A("| 大类 | 合计 | 小时 | 平均/记录日 |")
    A("|---|---|---|---|")
    order = [c["name"] for c in db.all_categories(conn)]
    shown = [c for c in order if rng["category_minutes"].get(c)]
    if not shown:
        A("| （本期没有任何记录） | | | |")
    for c in shown:
        A("| %s | %s | %.3f | %.3f |" % (
            c, util.fmt_hm(rng["category_minutes"].get(c, 0)),
            rng["category_minutes"].get(c, 0) / 60.0,
            rng["category_avg_per_record_day"].get(c, 0.0)))
    A("")
    A("## Top 5 二级任务")
    A("")
    if top:
        A("| 排名 | 二级任务 | 大类 | 时长 | 占比 |")
        A("|---|---|---|---|---|")
        for i, t in enumerate(top, start=1):
            A("| %d | %s | %s | %s（%.3f 小时） | %.1f%% |"
              % (i, t["task_name"], t["category_name"], util.fmt_hm(t["minutes"]),
                 t["hours"], t["share"] * 100))
    else:
        A("（本期没有记录）")
    A("")

    A("## 目标达成")
    A("")
    if g.get("has_goal"):
        A("- 计入判定的天数：%d 天" % g["judged_days"])
        A("- 达标天数：%d 天" % g["achieved_days"])
        A("- 未达标天数：%d 天" % g["fail_days"])
        A("- 达成率：%.1f%%" % (g["rate"] * 100))
        st = goals.streak_days(conn)
        if st.get("days"):
            A("- 连续达标天数：%d 天（%s ~ %s）" % (st["days"], st["from"], st["to"]))
        else:
            A("- 连续达标天数：0 天")
        fails = [d for d in g["days"] if d["has_goal"] and not d["achieved"]]
        if fails:
            A("")
            A("未达标的日子：")
            for d in fails:
                short = "；".join("%s 差 %s（%s/%s）" % (i["category_name"], i["short_hm"],
                                                      i["got_hm"], i["goal_hm"])
                                 for i in d["items"] if not i["ok"])
                A("- %s（%s）：%s" % (d["day"], d["weekday"], short))
    else:
        A("你还没设置目标，这一期的达成率无法计算。可以在「设置 → 目标」里按大类设每日下限。")
    A("")

    A("## 与上一周期对比")
    A("")
    if rep["prev"]:
        p = rep["prev"]
        A("上一周期：%s ~ %s" % rep["prev_range"])
        A("")
        A("| 项目 | 本周期 | 上一周期 | 变化 |")
        A("|---|---|---|---|")
        A("| 总时长（去重） | %s | %s | %s |" % (
            util.fmt_hm(rng["actual_minutes"]), util.fmt_hm(p["actual_minutes"]),
            _diff_text(rng["actual_h"], p["actual_h"])))
        A("| 工作时间（去重） | %s | %s | %s |" % (
            util.fmt_hm(rng["work_minutes"]), util.fmt_hm(p["work_minutes"]),
            _diff_text(rng["work_h"], p["work_h"])))
        A("| 记录天数 | %d | %d | %s |" % (
            rng["record_days"], p["record_days"],
            _diff_text(rng["record_days"], p["record_days"], "天", 0)))
        A("| 平均/记录日 | %.3f | %.3f | %s |" % (
            rng["avg_per_record_day_h"], p["avg_per_record_day_h"],
            _diff_text(rng["avg_per_record_day_h"], p["avg_per_record_day_h"])))
        if g.get("has_goal") and (p.get("goal") or {}).get("has_goal"):
            pg = p["goal"]
            A("| 达标天数 | %d | %d | %s |" % (
                g["achieved_days"], pg["achieved_days"],
                _diff_text(g["achieved_days"], pg["achieved_days"], "天", 0)))
    else:
        A("（没有上一周期可以对比）")
    A("")

    # ---------------------------------------------------------- 可复制文字总结
    A("## 文字总结（可直接复制）")
    A("")
    A("```")
    A(text_summary(conn, rep))
    A("```")
    A("")
    A("---")
    A("由「加油！专注工作记录」于 %s 自动生成。" % util.iso(util.now_local()))
    return "\n".join(L)


def text_summary(conn, rep):
    """像用户手写"7月总结"那样成文的一段话。"""
    rng = rep["range"]
    g = rep["goal"] or {}
    top = rep["top"]
    mon = util.parse_day(rng["start"])
    period = ("%d 月 %d 日到 %d 月 %d 日" % (mon.month, mon.day,
                                          util.parse_day(rng["end"]).month,
                                          util.parse_day(rng["end"]).day)
              if rep["kind"] == "custom" else
              ("%s 这一周" % rng["start"] if rep["kind"] == "week" else
               "%d 月" % mon.month))
    if rng["record_days"] == 0:
        return "%s这段时间没有记录。等有记录了再来看看。" % period

    order = [c["name"] for c in db.all_categories(conn)]
    parts = []
    for c in order:
        m = rng["category_minutes"].get(c, 0)
        if m:
            parts.append("%s %s" % (c, util.fmt_hm(m)))
    cat_text = "、".join(parts)

    lines = []
    lines.append("%s：一共记录了 %d 天（自然日 %d 天，记录率 %.0f%%），"
                 "实际投入 %s，其中工作时间（科研+会议）%s。"
                 % (period, rng["record_days"], rng["natural_days"],
                    rng["record_rate"] * 100, util.fmt_hm(rng["actual_minutes"]),
                    util.fmt_hm(rng["work_minutes"])))
    lines.append("分类看：%s。" % cat_text)
    lines.append("平均每天（按记录日）%.2f 小时，平均每天（按自然日）%.2f 小时。"
                 % (rng["avg_per_record_day_h"], rng["avg_per_natural_day_h"]))
    if top:
        lines.append("花时间最多的是「%s」（%s，占 %.0f%%），其次是%s。"
                     % (top[0]["task_name"], util.fmt_hm(top[0]["minutes"]),
                        top[0]["share"] * 100,
                        "、".join("「%s」%s" % (t["task_name"], util.fmt_hm(t["minutes"]))
                                 for t in top[1:4]) or "没有别的了"))
    if g.get("has_goal"):
        st = goals.streak_days(conn)
        lines.append("目标方面：%d 天里有 %d 天达标，达成率 %.0f%%；目前连续达标 %d 天。"
                     % (g["judged_days"], g["achieved_days"], g["rate"] * 100,
                        st.get("days", 0)))
        if g["fail_days"] == 0:
            lines.append("每一天都达到了自己设的目标，做得很好。")
        else:
            lines.append("有 %d 天没达到目标，把没做到的那几天补一补也不迟。"
                         % g["fail_days"])
    else:
        lines.append("还没有设置目标，可以先在设置里给科研和会议各定一个每日下限。")
    if rep["prev"]:
        p = rep["prev"]
        d = rng["actual_h"] - p["actual_h"]
        if abs(d) < 0.05:
            lines.append("和上一周期比基本持平。")
        elif d > 0:
            lines.append("比上一周期多了 %.2f 小时，是在往上走的。" % abs(d))
        else:
            lines.append("比上一周期少了 %.2f 小时，下个周期往回追一追。" % abs(d))
    lines.append("做好细末是很棒的！")
    return "\n".join(lines)


# ================================================================ 周期键与文件

def _period_key(kind, start, end):
    if kind == "week":
        return util.iso_week_label(util.parse_day(start))
    if kind == "month":
        return util.parse_day(start).strftime("%Y-%m")
    return "custom:%s_%s" % (start, end)


def save_report(conn, rep, force=False):
    """把报表写进 reports\\ 并登记。同名文件默认不覆盖（除非 force）。"""
    db.ensure_dirs()
    kind = rep["kind"]
    key = _period_key(kind, rep["start"], rep["end"])
    if kind == "week":
        fname = "%s-周报.md" % key
    elif kind == "month":
        fname = "%s-月报.md" % key
    else:
        fname = "%s至%s-总结.md" % (rep["start"], rep["end"])
    path = os.path.join(db.REPORTS_DIR, fname)

    old = conn.execute("SELECT * FROM reports WHERE kind=? AND period_key=?",
                       (kind, key)).fetchone()
    if old and not force and os.path.exists(path):
        return {"ok": True, "skipped": True, "file": fname, "path": path,
                "message": "这份报表已经生成过了。", "report": rep}

    with open(path, "w", encoding="utf-8") as f:
        f.write(rep["markdown"])
    conn.execute(
        "INSERT INTO reports(kind, period_key, start_day, end_day, file_name, generated_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(kind, period_key) DO UPDATE SET "
        "file_name=excluded.file_name, generated_at=excluded.generated_at, "
        "acknowledged=0",
        (kind, key, rep["start"], rep["end"], fname, util.iso(util.now_local())))
    return {"ok": True, "skipped": False, "file": fname, "path": path,
            "message": "已生成：reports\\%s" % fname, "report": rep}


def auto_generate(conn, today=None):
    """打开软件时自动补生成"上一个已结束的周"和"上一个已结束的月"（规格 6.2）。

    只补这两个，更早的周期由用户在「总结」页按需生成（用户 2026-10-12 确认）。
    """
    today = util.parse_day(today) if today else util.now_local().date()
    first = db.min_record_day(conn)
    out = []
    if first is None:
        return {"ok": True, "generated": [], "message": ""}

    # 上一个已结束的周
    ps, pe = _prev_week_range(today)
    if util.parse_day(first) <= pe:
        key = util.iso_week_label(ps)
        exists = conn.execute("SELECT 1 FROM reports WHERE kind='week' AND period_key=?",
                              (key,)).fetchone()
        if not exists:
            rep = build_report(conn, ps.isoformat(), pe.isoformat(), "week")
            r = save_report(conn, rep)
            if not r.get("skipped"):
                out.append({"kind": "week", "period": key, "file": r["file"],
                            "start": ps.isoformat(), "end": pe.isoformat(),
                            "title": rep["title"]})

    # 上一个已结束的月
    ms, me = _prev_month_range(today)
    if util.parse_day(first) <= me:
        key = ms.strftime("%Y-%m")
        exists = conn.execute("SELECT 1 FROM reports WHERE kind='month' AND period_key=?",
                              (key,)).fetchone()
        if not exists:
            rep = build_report(conn, ms.isoformat(), me.isoformat(), "month")
            r = save_report(conn, rep)
            if not r.get("skipped"):
                out.append({"kind": "month", "period": key, "file": r["file"],
                            "start": ms.isoformat(), "end": me.isoformat(),
                            "title": rep["title"]})
    return {"ok": True, "generated": out,
            "message": ("已自动生成：" + "、".join(x["title"] for x in out)) if out else ""}


def list_reports(conn, limit=100):
    rows = conn.execute(
        "SELECT * FROM reports ORDER BY start_day DESC, id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        p = os.path.join(db.REPORTS_DIR, r["file_name"])
        out.append(dict(r, exists=os.path.exists(p),
                        size=os.path.getsize(p) if os.path.exists(p) else 0))
    return out


def read_report(conn, report_id=None, file_name=None):
    if report_id:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        file_name = row["file_name"] if row else None
    if not file_name:
        return {"ok": False, "error": "找不到这份报表。"}
    p = os.path.join(db.REPORTS_DIR, os.path.basename(file_name))
    if not os.path.exists(p):
        return {"ok": False, "error": "报表文件不见了：reports\\%s" % file_name}
    with open(p, "r", encoding="utf-8") as f:
        return {"ok": True, "file": file_name, "markdown": f.read()}
