# -*- coding: utf-8 -*-
"""目标与达标（规格 3.6 / 5.4）＋ 计划 vs 实际（规格 3.5 / 6.3）。

口径（不得改动）
----------------
* 目标维度：按大类设「每日下限」，工作日与周末两组；**软件不预设任何数字**。
* 当日目标取值：周一~周五 -> 工作日目标；**周六 -> 工作日目标 × 50%**（比例可改，默认 0.5）；
  周日 -> 周末目标（用户 2026-10-12 确认，周日不折算）。
* 达标日 = 当天所有「已设目标的大类」都达到各自当日目标。
  未设目标的大类不参与；一天都没设目标 -> 不参与判定（既不算达标也不算不达标）。
* 连续达标天数 = 从最近一天往前连续达标的天数，中断一天即清零，无豁免。
  若"今天"还没达标，不把今天算作中断（用户 2026-10-12 确认）。
* 达成率 = 达标天数 ÷ 计入判定的天数。
"""

import datetime
import collections

from . import db, util


# ================================================================ 目标读写

def list_goals(conn):
    """列出所有目标：大类 + 工作日/周末两组分钟数。"""
    rows = conn.execute(
        "SELECT g.*, c.name AS category_name, c.archived FROM goals g "
        "JOIN categories c ON c.id=g.category_id ORDER BY c.sort_order, c.id").fetchall()
    by_cat = collections.defaultdict(dict)
    for r in rows:
        by_cat[r["category_id"]][r["goal_type"]] = r["minutes"]
    out = []
    for cat in db.all_categories(conn):
        g = by_cat.get(cat["id"], {})
        out.append({
            "category_id": cat["id"], "category_name": cat["name"],
            "archived": cat["archived"],
            "workday_min": g.get("workday"), "weekend_min": g.get("weekend"),
            "workday_hm": util.fmt_hm(g["workday"]) if g.get("workday") else "",
            "weekend_hm": util.fmt_hm(g["weekend"]) if g.get("weekend") else "",
            "is_set": bool(g.get("workday") or g.get("weekend")),
        })
    return out


def has_any_goal(conn):
    return conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] > 0


def set_goal(conn, category_id, goal_type, minutes):
    """设置/更新一个目标。minutes 为 None 或 0 表示清除该目标。"""
    if goal_type not in ("workday", "weekend"):
        return {"ok": False, "error": "目标类型只能是「工作日」或「周末」。"}
    cat = db.category_by_id(conn, category_id)
    if cat is None:
        return {"ok": False, "error": "找不到这个大类，请刷新页面后重试。"}
    if minutes in (None, "", 0, "0"):
        conn.execute("DELETE FROM goals WHERE category_id=? AND goal_type=?",
                     (category_id, goal_type))
        return {"ok": True, "message": "已清除「%s」的%s目标。" % (
            cat["name"], "工作日" if goal_type == "workday" else "周末")}
    # 目标时长接受 H:MM 写法（如 3:00），也接受纯分钟数（如 180）
    if isinstance(minutes, int):
        m = minutes
    else:
        m, err = util.parse_duration(minutes)
        if err:
            return {"ok": False, "error": "目标时长要填 H:MM（如 3:00）或分钟数（如 180）。"}
    if m <= 0:
        return {"ok": False, "error": "目标时长必须大于 0。"}
    if m > 24 * 60:
        return {"ok": False, "error": "每天的目标不能超过 24 小时。"}
    conn.execute(
        "INSERT INTO goals(category_id, goal_type, minutes, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(category_id, goal_type) DO UPDATE SET "
        "minutes=excluded.minutes, updated_at=excluded.updated_at",
        (category_id, goal_type, m, util.iso(util.now_local())))
    return {"ok": True, "message": "已设置「%s」的%s目标为 %s。" % (
        cat["name"], "工作日" if goal_type == "workday" else "周末", util.fmt_hm(m))}


def effective_goal(conn, day):
    """某一天每个大类各自的目标分钟数（只含有设目标的大类）。"""
    d = util.parse_day(day)
    wd = d.weekday()            # 0=周一 ... 5=周六 6=周日
    ratio = db.get_setting_float(conn, "saturday_ratio", 0.5)
    rows = conn.execute(
        "SELECT g.category_id, g.goal_type, g.minutes, c.name AS category_name "
        "FROM goals g JOIN categories c ON c.id=g.category_id "
        "WHERE NOT c.archived").fetchall()
    by_cat = collections.defaultdict(dict)
    for r in rows:
        by_cat[r["category_id"]][r["goal_type"]] = (r["minutes"], r["category_name"])

    out = []
    for cid, g in by_cat.items():
        name = list(g.values())[0][1]
        if wd <= 4:                       # 周一~周五
            m = g.get("workday", (None,))[0]
            kind = "工作日"
        elif wd == 5:                     # 周六 = 工作日 × 50%
            base = g.get("workday", (None,))[0]
            m = int(round(base * ratio)) if base else None
            kind = "周六（工作日×%g）" % ratio
        else:                             # 周日 = 周末目标
            m = g.get("weekend", (None,))[0]
            kind = "周末"
        if m:
            out.append({"category_id": cid, "category_name": name,
                        "minutes": m, "hm": util.fmt_hm(m), "goal_type": kind})
    return out


def day_goal_status(conn, day, category_minutes):
    """某一天的达标情况。category_minutes: {大类名: 分钟}"""
    goals = effective_goal(conn, day)
    if not goals:
        return {"has_goal": False, "achieved": None, "items": [],
                "text": "这一天没有设目标，不参与达标判定"}
    items = []
    all_ok = True
    for g in goals:
        got = int(category_minutes.get(g["category_name"], 0))
        ok = got >= g["minutes"]
        all_ok = all_ok and ok
        items.append({
            "category_name": g["category_name"], "goal_type": g["goal_type"],
            "goal_minutes": g["minutes"], "goal_hm": g["hm"],
            "got_minutes": got, "got_hm": util.fmt_hm(got),
            "ok": ok, "short_minutes": max(0, g["minutes"] - got),
            "short_hm": util.fmt_hm(max(0, g["minutes"] - got)),
        })
    return {"has_goal": True, "achieved": all_ok, "items": items}


# ================================================================ 区间达标汇总

def range_goal_summary(conn, start_day, end_day, per_day):
    """区间的达标统计：达标天数 / 计入判定的天数、达成率、连续达标天数。

    per_day 为 stats.range_detail 里每天的明细（含 category_minutes）。
    """
    if not has_any_goal(conn):
        return {"has_goal": False, "text": "你还没设置目标"}

    days = []
    for pd in per_day:
        st = day_goal_status(conn, pd["day"], pd.get("category_minutes") or {})
        days.append({
            "day": pd["day"], "weekday": pd["weekday"],
            "has_goal": st["has_goal"], "achieved": st["achieved"],
            "items": st["items"],
            "work_minutes": pd["work_minutes"],
        })
    judged = [d for d in days if d["has_goal"]]
    achieved = [d for d in judged if d["achieved"]]
    return {
        "has_goal": True,
        "days": days,
        "judged_days": len(judged),
        "achieved_days": len(achieved),
        "fail_days": len(judged) - len(achieved),
        "rate": round(len(achieved) / len(judged), 4) if judged else 0.0,
        "streak": streak_days(conn),
    }


def streak_days(conn, today=None):
    """连续达标天数（规格 5.4）。

    做法：从最近一天往前数，只要那天达标就继续；遇到不达标就停（中断一天即清零，无豁免）。
    若"今天"还没达标，不把今天算作中断——从昨天往前数（用户 2026-10-12 确认）。
    没设目标的那些天不参与判定，跳过继续往前数（它们既不算达标也不算中断）。
    """
    if not has_any_goal(conn):
        return {"has_goal": False, "days": 0, "from": None, "to": None}
    today = today or util.now_local().date()
    latest = db.max_record_day(conn)
    if latest is None:
        return {"has_goal": True, "days": 0, "from": None, "to": None}
    last = util.parse_day(latest)
    if last > today:
        last = today

    # 每一天的分大类分钟数（一次查完，避免逐天查库）
    rows = conn.execute(
        "SELECT day, c.name AS cat, SUM(r.duration_min) AS mins FROM records r "
        "JOIN categories c ON c.id=r.category_id WHERE r.deleted_at IS NULL "
        "GROUP BY day, c.name").fetchall()
    per_day_cat = collections.defaultdict(dict)
    for r in rows:
        per_day_cat[r["day"]][r["cat"]] = r["mins"]

    def ok_on(d):
        """True=达标 False=未达标 None=这一天没设目标（不参与判定）"""
        st = day_goal_status(conn, d.isoformat(), per_day_cat.get(d.isoformat(), {}))
        return st["achieved"] if st["has_goal"] else None

    cur = last
    if cur == today and ok_on(cur) is False:
        cur = cur - datetime.timedelta(days=1)      # 今天还没达标 -> 不当作中断

    count = 0
    streak_end = streak_start = None
    guard = 0
    while guard < 4000:                             # 防御：最多回溯十年
        guard += 1
        r = ok_on(cur)
        if r is None:                               # 这天没设目标：跳过，继续往前
            cur -= datetime.timedelta(days=1)
            if streak_start:                        # 已经数了一段，中间断了个没目标的天 -> 停
                break
            continue
        if not r:
            break
        if streak_end is None:
            streak_end = cur
        streak_start = cur
        count += 1
        cur -= datetime.timedelta(days=1)
    return {"has_goal": True, "days": count,
            "to": streak_end.isoformat() if streak_end else None,
            "from": streak_start.isoformat() if streak_start else None}


# ================================================================ 今天/本周还差多少

def progress(conn, day=None):
    """今日与本周的目标进度（规格 6.1 第 2 条：显示还差多少，按当前进度算能否达标）。"""
    if not has_any_goal(conn):
        return {"has_goal": False, "text": "你还没设置目标"}
    day = day or util.now_local().date().isoformat()
    d = util.parse_day(day)
    mon, sun = util.week_range(d)

    def cat_minutes(start, end):
        rows = conn.execute(
            "SELECT c.name AS cat, SUM(r.duration_min) AS mins FROM records r "
            "JOIN categories c ON c.id=r.category_id WHERE r.deleted_at IS NULL "
            "AND r.day BETWEEN ? AND ? GROUP BY c.name", (start, end)).fetchall()
        return {r["cat"]: r["mins"] for r in rows}

    # -------------------------------------------------- 今日还差多少
    today_cm = cat_minutes(d.isoformat(), d.isoformat())
    today_items = effective_goal(conn, d.isoformat())
    week_items = []
    for i in range(7):
        dd = (mon + datetime.timedelta(days=i)).isoformat()
        for g in effective_goal(conn, dd):
            week_items.append({"day": dd, "category_id": g["category_id"],
                               "category_name": g["category_name"], "minutes": g["minutes"]})
    week_sum = collections.Counter()
    for it in week_items:
        week_sum[(it["category_id"], it["category_name"])] += it["minutes"]

    remaining_today = []
    for g in today_items:
        got = int(today_cm.get(g["category_name"], 0))
        remaining_today.append({
            "category_name": g["category_name"], "goal_hm": g["hm"],
            "got_hm": util.fmt_hm(got), "got_minutes": got,
            "remaining_minutes": max(0, g["minutes"] - got),
            "remaining_hm": util.fmt_hm(max(0, g["minutes"] - got)),
            "ok": got >= g["minutes"],
        })

    remaining_week = []
    for (cid, name), goal in week_sum.items():
        # 本周「到今天为止」该大类的实际完成量：按天加总，避免跨天重复计入
        got = 0
        for it in week_items:
            if it["category_id"] == cid and it["day"] <= d.isoformat():
                got += int(cat_minutes(it["day"], it["day"]).get(name, 0))
        remaining_week.append({
            "category_name": name, "goal_minutes": goal, "goal_hm": util.fmt_hm(goal),
            "got_minutes": got, "got_hm": util.fmt_hm(got),
            "remaining_minutes": max(0, goal - got),
            "remaining_hm": util.fmt_hm(max(0, goal - got)),
            "days_left": max(0, (sun - d).days),
            "ahead": got >= goal,
        })

    week_end = sun.isoformat()
    # 按当前进度能否达标：本周剩余天数里每天还能补多少（用最近 7 天该类别的日均来估）
    recent = conn.execute(
        "SELECT c.name AS cat, SUM(r.duration_min) AS mins FROM records r "
        "JOIN categories c ON c.id=r.category_id WHERE r.deleted_at IS NULL "
        "AND r.day BETWEEN ? AND ? GROUP BY c.name",
        ((d - datetime.timedelta(days=6)).isoformat(), d.isoformat())).fetchall()
    recent_cm = {r["cat"]: r["mins"] for r in recent}
    for item in remaining_week:
        days_left = item["days_left"] + 1        # 含今天
        pace_per_day = recent_cm.get(item["category_name"], 0) / 7.0
        can = item["got_minutes"] + pace_per_day * days_left
        item["on_track"] = can >= item["goal_minutes"]
        item["forecast_hm"] = util.fmt_hm(int(round(can)))

    return {
        "has_goal": True, "day": d.isoformat(), "week_start": mon.isoformat(),
        "week_end": week_end,
        "today": remaining_today, "week": remaining_week,
    }


# ================================================================ 计划（规格 3.5 / 6.3）

def add_plan(conn, day, category_id, task_id=None, task_name=None, planned=None, note=""):
    """加一条计划。二级任务必填（用户 2026-10-12 确认 16-4）；计划时长可空。"""
    if not day:
        return {"ok": False, "error": "请选择计划的日期。"}
    try:
        d = util.parse_day(day)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    cat = db.category_by_id(conn, category_id) if category_id else None
    if cat is None:
        return {"ok": False, "error": "请选择大类。"}
    if cat["archived"]:
        return {"ok": False, "error": "「%s」已归档，不能再排计划。" % cat["name"]}
    name = (task_name or "").strip()
    if task_id:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "找不到这个二级任务，请刷新页面后重试。"}
        if row["category_id"] != cat["id"]:
            return {"ok": False, "error": "二级任务与所选大类不一致，请重新选择。"}
        name = row["name"]
    if not name:
        return {"ok": False, "error": "计划也要填二级任务（与记录一致）。"}

    planned_min = None
    if planned not in (None, "", 0, "0"):
        if isinstance(planned, int):
            planned_min = planned
        else:
            planned_min, err = util.parse_duration(planned)
            if err:
                return {"ok": False, "error": err}
    task_id, _ = db.ensure_task(conn, cat["id"], name)
    cur = conn.execute(
        "INSERT INTO plans(day, category_id, task_id, task_name, planned_min, note) "
        "VALUES (?,?,?,?,?,?)",
        (d.isoformat(), cat["id"], task_id, name, planned_min, note or ""))
    return {"ok": True, "plan_id": cur.lastrowid,
            "message": "已排计划：%s %s%s" % (d.isoformat(), name,
                                          (" 计划 " + util.fmt_hm(planned_min)) if planned_min else "")}


def delete_plan(conn, plan_id):
    conn.execute("UPDATE plans SET deleted_at=? WHERE id=?",
                 (util.iso(util.now_local()), plan_id))
    return {"ok": True, "message": "已删除这条计划。"}


def list_plans(conn, start_day, end_day):
    rows = conn.execute(
        "SELECT p.*, c.name AS category_name FROM plans p "
        "JOIN categories c ON c.id=p.category_id "
        "WHERE p.deleted_at IS NULL AND p.day BETWEEN ? AND ? "
        "ORDER BY p.day, c.sort_order, p.id", (start_day, end_day)).fetchall()
    return [{"id": r["id"], "day": r["day"], "category_id": r["category_id"],
             "category_name": r["category_name"], "task_id": r["task_id"],
             "task_name": r["task_name"], "planned_min": r["planned_min"],
             "planned_hm": util.fmt_hm(r["planned_min"]) if r["planned_min"] else "",
             "note": r["note"]} for r in rows]


def plan_week(conn, day, items):
    """一次性给某一周（周一~周日）排计划。

    items: [{"weekday": 1-7, "category_id":..., "task_id"/"task_name":..., "planned": ...}]
    """
    mon, sun = util.week_range(day)
    created, errors = 0, []
    for it in items:
        wd = int(it.get("weekday") or 1)
        target = mon + datetime.timedelta(days=wd - 1)
        r = add_plan(conn, target.isoformat(), it.get("category_id"),
                     it.get("task_id"), it.get("task_name"),
                     it.get("planned"), it.get("note", ""))
        if r["ok"]:
            created += 1
        else:
            errors.append(r["error"])
    return {"ok": not errors, "created": created, "errors": errors,
            "week_start": mon.isoformat(), "week_end": sun.isoformat()}


def plan_vs_actual(conn, start_day, end_day):
    """计划 vs 实际（规格 6.3）：计划任务数/实际执行数、计划时长 vs 实际时长、偏差、未执行清单。"""
    plans = list_plans(conn, start_day, end_day)
    if not plans:
        return {"has_plan": False, "text": "这个区间没有排计划"}
    recs = conn.execute(
        "SELECT r.day, r.task_id, r.task_name, r.duration_min, c.name AS category_name "
        "FROM records r JOIN categories c ON c.id=r.category_id "
        "WHERE r.deleted_at IS NULL AND r.day BETWEEN ? AND ?",
        (start_day, end_day)).fetchall()
    actual_by_day_task = collections.Counter()
    actual_min_by_day_task = collections.Counter()
    for r in recs:
        actual_by_day_task[(r["day"], r["task_name"])] += 1
        actual_min_by_day_task[(r["day"], r["task_name"])] += int(r["duration_min"])

    done, undone = [], []
    planned_min_total = 0
    actual_min_total = 0
    for p in plans:
        key = (p["day"], p["task_name"])
        cnt = actual_by_day_task.get(key, 0)
        am = actual_min_by_day_task.get(key, 0)
        if p["planned_min"]:
            planned_min_total += int(p["planned_min"])
        actual_min_total += am
        row = dict(p)
        row["actual_count"] = cnt
        row["actual_minutes"] = am
        row["actual_hm"] = util.fmt_hm(am)
        row["executed"] = cnt > 0
        if p["planned_min"]:
            row["diff_minutes"] = am - int(p["planned_min"])
            row["diff_hm"] = util.fmt_hm(abs(row["diff_minutes"]))
            row["diff_sign"] = "+" if row["diff_minutes"] > 0 else ("-" if row["diff_minutes"] < 0 else "")
        else:
            row["diff_minutes"] = None
            row["diff_hm"] = ""
            row["diff_sign"] = ""
        (done if cnt > 0 else undone).append(row)

    # 计划里没排、但实际做了的任务（供参考，不算偏差）
    planned_keys = {(p["day"], p["task_name"]) for p in plans}
    extra = []
    for r in recs:
        if (r["day"], r["task_name"]) not in planned_keys:
            extra.append({"day": r["day"], "task_name": r["task_name"],
                          "category_name": r["category_name"],
                          "minutes": int(r["duration_min"]),
                          "hm": util.fmt_hm(int(r["duration_min"]))})

    return {
        "has_plan": True,
        "plan_count": len(plans),
        "executed_count": len(done),
        "unexecuted_count": len(undone),
        "exec_rate": round(len(done) / len(plans), 4) if plans else 0.0,
        "planned_minutes": planned_min_total,
        "planned_h": round(planned_min_total / 60.0, 3),
        "actual_minutes": actual_min_total,
        "actual_h": round(actual_min_total / 60.0, 3),
        "diff_minutes": actual_min_total - planned_min_total,
        "diff_h": round((actual_min_total - planned_min_total) / 60.0, 3),
        "plans": [dict(p, executed=(p["day"], p["task_name"]) in actual_by_day_task)
                  for p in plans],
        "done": done, "undone": undone, "extra": extra,
    }
