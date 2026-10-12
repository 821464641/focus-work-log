# -*- coding: utf-8 -*-
"""统计口径实现（规格第 5 节）。

核心口径（不得改动，规格 13.6）
------------------------------
1. 各类别时长 = 该大类当天所有记录时长直接相加，不去重。
2. 实际时长（去重）= 当天所有【计时段】的区间并集长度 + 当天所有【手填记录】时长之和。
   手填记录没有时间区间，无法去重，直接相加。
3. 投入时长之和 = 当天所有记录时长直接相加（参考值）。
4. 首页大字 = 今日「计入工作时间」的大类（默认科研+会议）的实际时长（去重）。
   其中计时段按【这些大类合并后的区间并集】算（用户 2026-10-12 确认），
   不是各类分别去重再相加。
5. 平均每天（记录日）= 周期总时长 ÷ 有记录的天数。
6. 平均每天（自然日）= 周期总时长 ÷ 周期自然日天数；首月分母不早于最早记录日。
7. 记录率 = 有记录天数 ÷ 自然日天数。
"""

import datetime
import collections

from . import db, util


# ================================================================ 单日

def day_detail(conn, day):
    """某一天的完整统计。day 为 'YYYY-MM-DD'。"""
    d = util.parse_day(day)
    rows = [dict(r) for r in conn.execute(
        "SELECT r.*, c.name AS category_name, c.counts_work "
        "FROM records r JOIN categories c ON c.id=r.category_id "
        "WHERE r.day=? AND r.deleted_at IS NULL ORDER BY "
        "  CASE WHEN r.start_ts IS NULL THEN 1 ELSE 0 END, r.start_ts, r.id", (d.isoformat(),))]

    cat_minutes = collections.Counter()
    timer_spans = []            # 所有"带起止时间"的区间（去重用）
    work_spans = []             # 只属于"计入工作时间"大类的区间
    manual_minutes = 0          # 没有区间的记录（直接相加）
    work_manual_minutes = 0
    invested_minutes = 0

    for r in rows:
        mins = int(r["duration_min"])
        invested_minutes += mins
        cat_minutes[r["category_name"]] += mins
        # 判断标准是"有没有起止时间"，不是"来源是计时还是手填"：
        # 补录时如果填了时间段，它同样有起止时间，也应当参与时间轴去重
        # （用户 2026-10-12 确认）。
        if r["start_ts"] and r["end_ts"]:
            s, e = util.parse_iso(r["start_ts"]), util.parse_iso(r["end_ts"])
            timer_spans.append((s, e))
            if r["counts_work"]:
                work_spans.append((s, e))
        else:
            manual_minutes += mins
            if r["counts_work"]:
                work_manual_minutes += mins

    actual = util.union_length_minutes(timer_spans) + manual_minutes
    work_actual = util.union_length_minutes(work_spans) + work_manual_minutes

    return {
        "day": d.isoformat(),
        "weekday": util.weekday_cn(d),
        "records": rows,
        "record_count": len(rows),
        "has_records": len(rows) > 0,
        "category_minutes": dict(cat_minutes),
        "actual_minutes": actual,               # 实际时长（去重）
        "work_minutes": work_actual,            # 实际工作时长（科研+会议，去重）
        "invested_minutes": invested_minutes,   # 投入时长之和（不去重，参考值）
        "timer_minutes": util.union_length_minutes(timer_spans),
        "manual_minutes": manual_minutes,
    }


# ================================================================ 区间

def range_detail(conn, start_day, end_day, goal_engine=None):
    """任意区间的统计（日/周/月/自定义都用它）。

    返回的每个数字都严格按规格 5.x 口径，不做任何"顺手改口径"。
    """
    s, e = util.parse_day(start_day), util.parse_day(end_day)
    if e < s:
        s, e = e, s

    # 全部记录（含是否计入工作时间）
    rows = [dict(r) for r in conn.execute(
        "SELECT r.*, c.name AS category_name, c.counts_work "
        "FROM records r JOIN categories c ON c.id=r.category_id "
        "WHERE r.deleted_at IS NULL AND r.day BETWEEN ? AND ? "
        "ORDER BY r.day, r.id", (s.isoformat(), e.isoformat()))]

    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)

    cat_minutes = collections.Counter()
    tot_invested = tot_actual = tot_work = 0
    per_day = []
    for d in util.days_between(s, e):
        key = d.isoformat()
        recs = by_day.get(key, [])
        spans, wspans = [], []
        manual = wmanual = invested = 0
        cm = collections.Counter()
        for r in recs:
            mins = int(r["duration_min"])
            invested += mins
            cat_minutes[r["category_name"]] += mins
            cm[r["category_name"]] += mins
            # 同上：只要有起止时间就参与时间轴去重（补录填了时间段也一样）
            if r["start_ts"] and r["end_ts"]:
                sp = (util.parse_iso(r["start_ts"]), util.parse_iso(r["end_ts"]))
                spans.append(sp)
                if r["counts_work"]:
                    wspans.append(sp)
            else:
                manual += mins
                if r["counts_work"]:
                    wmanual += mins
        actual = util.union_length_minutes(spans) + manual
        work = util.union_length_minutes(wspans) + wmanual
        tot_invested += invested
        tot_actual += actual
        tot_work += work
        per_day.append({
            "day": key, "weekday": util.weekday_cn(d),
            "record_count": len(recs),
            "has_records": len(recs) > 0,
            "actual_minutes": actual,
            "work_minutes": work,
            "invested_minutes": invested,
            "category_minutes": dict(cm),
        })

    record_days = len([x for x in per_day if x["has_records"]])
    natural_days = len(per_day)
    total_min = sum(cat_minutes.values())

    # 分大类平均每天（沿用原表口径：按有记录天数平均）
    cat_avg_per_record_day = {
        c: round(v / 60.0 / record_days, 3) if record_days else 0.0
        for c, v in cat_minutes.items()}

    out = {
        "start": s.isoformat(), "end": e.isoformat(),
        "record_days": record_days,
        "natural_days": natural_days,
        "blank_days": natural_days - record_days,
        "record_rate": round(record_days / natural_days, 4) if natural_days else 0.0,
        "total_minutes": total_min,
        "total_h": round(total_min / 60.0, 3),
        "actual_minutes": tot_actual,          # 区间内逐日"实际时长（去重）"之和
        "actual_h": round(tot_actual / 60.0, 3),
        "work_minutes": tot_work,              # 逐日"工作时间（去重）"之和
        "work_h": round(tot_work / 60.0, 3),
        "invested_minutes": tot_invested,      # 投入时长之和（不去重）
        "avg_per_record_day_h": round(total_min / 60.0 / record_days, 3) if record_days else 0.0,
        "avg_per_natural_day_h": round(total_min / 60.0 / natural_days, 3) if natural_days else 0.0,
        "category_minutes": dict(cat_minutes),
        "category_h": {c: round(v / 60.0, 3) for c, v in cat_minutes.items()},
        "category_avg_per_record_day": cat_avg_per_record_day,
        "days": per_day,
        "record_count": len(rows),
    }

    # 目标/达标（M4 接入；没设置目标时不参与）
    if goal_engine is not None:
        out["goal"] = goal_engine(conn, s, e, per_day)
    return out


def range_with_goals(conn, start_day, end_day):
    """区间统计 + 达标汇总（页面/报表默认走这个）。"""
    from . import goals as goals_mod
    d = range_detail(conn, start_day, end_day, goals_mod.range_goal_summary)
    d["top_tasks"] = top_tasks(conn, start_day, end_day,
                              db.get_setting_int(conn, "top_n", 5))
    return d


def week_detail(conn, day, goal_engine=None):
    """某天所在的一周（周一~周日）。"""
    mon, sun = util.week_range(day)
    d = range_detail(conn, mon, sun, goal_engine)
    d["week_label"] = util.iso_week_label(mon)
    d["monday"], d["sunday"] = mon.isoformat(), sun.isoformat()
    return d


def month_detail(conn, day, goal_engine=None):
    """某天所在的自然月。

    自然日分母的两端修正（规格 5.2 + 10.6 基线反推）：
      下界 = max(该月 1 号, 库中最早记录日)   —— 规格 5.2 明写（首月 2025-07 按 9 天算）
      上界 = min(该月末日, 库中最晚记录日)   —— 由基线反推（2025-10 按 15 天而非 31 天算）
    """
    first, last = util.month_range(day)
    earliest = db.min_record_day(conn)
    latest = db.max_record_day(conn)
    eff_start, eff_end = first, last
    if earliest:
        eff_start = max(first, util.parse_day(earliest))
    if latest:
        eff_end = min(last, util.parse_day(latest))
    if eff_end < eff_start:
        # 这个月库里完全没有记录 —— 自然日按 0 天算，不做任何"至少 1 天"的假设
        return {
            "start": eff_start.isoformat(), "end": eff_start.isoformat(),
            "record_days": 0, "natural_days": 0, "blank_days": 0, "record_rate": 0.0,
            "total_minutes": 0, "total_h": 0.0, "actual_minutes": 0, "actual_h": 0.0,
            "work_minutes": 0, "work_h": 0.0, "invested_minutes": 0,
            "avg_per_record_day_h": 0.0, "avg_per_natural_day_h": 0.0,
            "category_minutes": {}, "category_h": {},
            "category_avg_per_record_day": {}, "days": [], "record_count": 0,
            "month": first.strftime("%Y-%m"), "month_first": first.isoformat(),
            "month_last": last.isoformat(), "start_effective": eff_start.isoformat(),
            "end_effective": eff_end.isoformat(), "data_first_day": earliest,
            "data_last_day": latest, "empty_month": True,
            "natural_days_note": "这个月没有任何记录",
        }

    d = range_detail(conn, eff_start, eff_end, goal_engine)
    d["month"] = first.strftime("%Y-%m")
    d["month_first"] = first.isoformat()
    d["month_last"] = last.isoformat()
    d["start_effective"] = eff_start.isoformat()
    d["end_effective"] = eff_end.isoformat()
    d["data_first_day"] = earliest
    d["data_last_day"] = latest
    d["natural_days_note"] = ("自然日 %d 天 = %s ~ %s（末端按库里最后一条记录日截断）"
                             % (d["natural_days"], eff_start.isoformat(), eff_end.isoformat()))
    return d


# ================================================================ Top 任务

def top_tasks(conn, start_day, end_day, limit=5):
    """Top N 二级任务：按任务聚合时长排序，附所属大类与占比（规格 5.3）。"""
    rows = conn.execute(
        "SELECT r.task_name, c.name AS category_name, SUM(r.duration_min) AS mins "
        "FROM records r JOIN categories c ON c.id=r.category_id "
        "WHERE r.deleted_at IS NULL AND r.day BETWEEN ? AND ? "
        "GROUP BY r.task_name, c.name ORDER BY mins DESC, r.task_name", 
        (start_day, end_day)).fetchall()
    total = sum(r["mins"] for r in rows) or 0
    out = []
    for r in rows[:limit]:
        out.append({
            "task_name": r["task_name"], "category_name": r["category_name"],
            "minutes": r["mins"], "hours": round(r["mins"] / 60.0, 3),
            "share": round(r["mins"] / total, 4) if total else 0.0,
        })
    return out


def task_totals(conn, start_day, end_day):
    """区间内全部任务聚合（导出用）。"""
    rows = conn.execute(
        "SELECT r.task_name, c.name AS category_name, SUM(r.duration_min) AS mins, "
        "       COUNT(*) AS cnt FROM records r JOIN categories c ON c.id=r.category_id "
        "WHERE r.deleted_at IS NULL AND r.day BETWEEN ? AND ? "
        "GROUP BY r.task_name, c.name ORDER BY mins DESC", (start_day, end_day)).fetchall()
    total = sum(r["mins"] for r in rows) or 0
    return [{"task_name": r["task_name"], "category_name": r["category_name"],
             "minutes": r["mins"], "hours": round(r["mins"] / 60.0, 3),
             "count": r["cnt"], "share": round(r["mins"] / total, 4) if total else 0.0}
            for r in rows]


# ================================================================ 数据范围

def data_bounds(conn):
    """库里数据的时间范围（用于"打开时的空白日召回"等）。"""
    return {"first": db.min_record_day(conn), "last": db.max_record_day(conn),
            "count": db.count_records(conn)}


def blank_days(conn, start_day, end_day):
    """区间内没有任何记录的日子（规格 5.3：标记为"未记录"）。"""
    s, e = util.parse_day(start_day), util.parse_day(end_day)
    have = {r[0] for r in conn.execute(
        "SELECT DISTINCT day FROM records WHERE deleted_at IS NULL AND day BETWEEN ? AND ?",
        (s.isoformat(), e.isoformat()))}
    return [d.isoformat() for d in util.days_between(s, e) if d.isoformat() not in have]


def daily_series(conn, start_day, end_day):
    """每日柱状图数据：按大类堆叠的分钟数。"""
    rows = conn.execute(
        "SELECT r.day, c.name AS category_name, SUM(r.duration_min) AS mins "
        "FROM records r JOIN categories c ON c.id=r.category_id "
        "WHERE r.deleted_at IS NULL AND r.day BETWEEN ? AND ? "
        "GROUP BY r.day, c.name ORDER BY r.day", (start_day, end_day)).fetchall()
    by_day = collections.defaultdict(dict)
    for r in rows:
        by_day[r["day"]][r["category_name"]] = r["mins"]
    s, e = util.parse_day(start_day), util.parse_day(end_day)
    return [{"day": d.isoformat(), "weekday": util.weekday_cn(d),
             "categories": by_day.get(d.isoformat(), {})} for d in util.days_between(s, e)]


# ================================================================ 月度统计

def monthly_stats(conn, start_day=None, end_day=None, only_import=False):
    """按月统计（规格 5.2 / 10.6 口径）。

    * 各大类时长 = 该月该类所有记录分钟之和 / 60（每条已按整数分钟存储）
    * 总计 = 各大类之和
    * 平均/记录日 = 总计 ÷ 该月有记录的天数
    * 平均/自然日 = 总计 ÷ 该月自然日天数；
      分母两端都修正：不早于"数据中最早记录日"、不晚于"数据中最晚记录日"
      （下界来自规格 5.2，上界由 10.6 基线反推：2025-10 按 1–15 日共 15 天算）

    only_import=True 时只统计 source='import' 的记录（本地版做验收基线比对时用）。
    """
    where_src = " AND source='import'" if only_import else ""
    first_row = conn.execute("SELECT MIN(day) FROM records WHERE deleted_at IS NULL"
                             + where_src).fetchone()
    last_row = conn.execute("SELECT MAX(day) FROM records WHERE deleted_at IS NULL"
                            + where_src).fetchone()
    first, last = first_row[0], last_row[0]
    if first is None:
        return {"months": [], "range": {"first": None, "last": None},
                "total": {"record_days": 0, "natural_days": 0, "blank_days": 0,
                          "total_h": 0.0, "record_count": 0}}

    rows = conn.execute(
        "SELECT substr(r.day,1,7) AS mo, c.name AS cat, SUM(r.duration_min) AS mins "
        "FROM records r JOIN categories c ON c.id=r.category_id "
        "WHERE r.deleted_at IS NULL" + where_src + " GROUP BY mo, cat").fetchall()
    dayrows = conn.execute(
        "SELECT substr(day,1,7) AS mo, COUNT(DISTINCT day) AS days FROM records "
        "WHERE deleted_at IS NULL" + where_src + " GROUP BY mo").fetchall()

    by_month = collections.defaultdict(collections.Counter)
    for r in rows:
        by_month[r["mo"]][r["cat"]] = r["mins"]
    days_by_month = {r["mo"]: r["days"] for r in dayrows}

    cats_all = ("科研", "会议", "阅读", "运动", "娱乐")
    months = []
    for mo in sorted(by_month.keys()):
        if start_day and mo < start_day[:7]:
            continue
        if end_day and mo > end_day[:7]:
            continue
        y, m = int(mo[:4]), int(mo[5:7])
        first_of_month = datetime.date(y, m, 1)
        last_of_month = util.month_range(first_of_month)[1]
        d0 = max(first_of_month, util.parse_day(first))
        d1 = min(last_of_month, util.parse_day(last))
        if end_day:
            d1 = min(d1, util.parse_day(end_day))
        natural_days = max(0, (d1 - d0).days + 1)
        cats = {c: round(by_month[mo].get(c, 0) / 60.0, 3) for c in cats_all}
        total_h = round(sum(by_month[mo].values()) / 60.0, 3)
        rec_days = days_by_month.get(mo, 0)
        months.append({
            "month": mo, "record_days": rec_days, "natural_days": natural_days,
            "total_h": total_h,
            "avg_per_record_day": round(total_h / rec_days, 3) if rec_days else 0.0,
            "avg_per_natural_day": round(total_h / natural_days, 3) if natural_days else 0.0,
            "categories": cats,
            "category_minutes": {c: by_month[mo].get(c, 0) for c in cats_all},
        })

    span_days = util.days_between(first, last)
    total_minutes = sum(sum(by_month[mo].values()) for mo in by_month)
    total_record_days = sum(days_by_month.values())
    n_records = conn.execute(
        "SELECT COUNT(*) FROM records WHERE deleted_at IS NULL" + where_src).fetchone()[0]
    return {
        "months": months,
        "range": {"first": first, "last": last},
        "total": {
            "record_days": total_record_days,
            "natural_days": len(span_days),
            "blank_days": len(span_days) - total_record_days,
            "total_h": round(total_minutes / 60.0, 3),
            "record_count": n_records,
        },
    }
