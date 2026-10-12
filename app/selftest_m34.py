# -*- coding: utf-8 -*-
"""M3 + M4 自测：统计口径（去重实际时长、两种平均每天、记录率、空白日、Top 任务）
＋ 目标（工作日/周末、周六×50%）→ 达成率、达标日、连续达标天数 ＋ 计划 vs 实际。

用法：python -m app.selftest_m34
用一份临时库，跑完删除。
"""

import os
import sys
import datetime

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import db, util, stats, goals
else:
    from . import db, util, stats, goals

FAIL = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("通过" if cond else "不通过", name, ("  <- " + detail) if detail else ""))
    if not cond:
        FAIL.append(name)
    return cond


def line(ch="-", n=100):
    print(ch * n)


def main():
    db.ensure_dirs()
    tmp = os.path.join(db.DATA_DIR, "_selftest_m34.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suf):
            os.remove(tmp + suf)
    conn = db.init_db(db.connect(tmp))
    conn.execute("DELETE FROM records")
    print("临时库：", tmp)

    cats = {c["name"]: c for c in db.all_categories(conn)}
    sci, meet, read, run = (cats["科研"]["id"], cats["会议"]["id"],
                            cats["阅读"]["id"], cats["运动"]["id"])

    D = lambda s: datetime.date(2025, 9, s)          # 2025-09 的某天
    # 2025-09-01 是周一
    print("日期核对：2025-09-01 是 %s，2025-09-06 是 %s，2025-09-07 是 %s"
          % (util.weekday_cn(D(1)), util.weekday_cn(D(6)), util.weekday_cn(D(7))))

    def add(day, cat_id, task, minutes, source="manual", sh=None, eh=None, tags=("T",)):
        tid, _ = db.ensure_task(conn, cat_id, task)
        return db.insert_record(conn, day.isoformat(), cat_id, tid, task, list(tags), minutes,
                                source, start_ts=sh, end_ts=eh)

    # ============================================================ M3 统计口径
    line("=")
    print("① 两种「平均每天」+ 记录率 + 空白日（规格 5.2 / 5.3）")
    # 9/1（周一）两条手填：科研 120、会议 60 ；9/2 无记录 ；9/3 科研 60
    add(D(1), sci, "升力计算", 120)
    add(D(1), meet, "小组会", 60)
    add(D(3), sci, "翼型测量", 60)
    # 为便于核对平均，只看 9/1~9/7 这一周
    wk = stats.range_detail(conn, "2025-09-01", "2025-09-07")
    check("记录日 = 2 天", wk["record_days"] == 2, str(wk["record_days"]))
    check("自然日 = 7 天", wk["natural_days"] == 7, str(wk["natural_days"]))
    check("空白日 = 5 天", wk["blank_days"] == 5, str(wk["blank_days"]))
    check("记录率 = 2/7 = 0.2857", wk["record_rate"] == 0.2857, str(wk["record_rate"]))
    check("总时长 = 240 分钟 = 4.000h", wk["total_h"] == 4.0, str(wk["total_h"]))
    check("平均/记录日 = 4.000/2 = 2.000h", wk["avg_per_record_day_h"] == 2.0,
          str(wk["avg_per_record_day_h"]))
    check("平均/自然日 = 4.000/7 = 0.571h", wk["avg_per_natural_day_h"] == 0.571,
          str(wk["avg_per_natural_day_h"]))
    check("两种平均确实不同（没有被统一成自然日）",
          wk["avg_per_record_day_h"] != wk["avg_per_natural_day_h"])
    blanks = stats.blank_days(conn, "2025-09-01", "2025-09-07")
    check("空白日清单 = 9/2、9/4~9/7", blanks == ["2025-09-02", "2025-09-04", "2025-09-05",
                                                "2025-09-06", "2025-09-07"], str(blanks))
    check("分大类平均（按记录日）= 科研 180÷2 = 1.5h",
          wk["category_avg_per_record_day"].get("科研") == 1.5,
          str(wk["category_avg_per_record_day"].get("科研")))

    line("-")
    print("② 去重实际时长 vs 投入时长之和（规格 5.1）")
    # 并行：9/8 科研 09:00-11:00（120）、会议 10:00-12:00（120），重叠 60 分钟
    add(D(8), sci, "升力计算", 120, "timer", "2025-09-08 09:00:00", "2025-09-08 11:00:00")
    add(D(8), meet, "小组会", 120, "timer", "2025-09-08 10:00:00", "2025-09-08 12:00:00")
    # 手填 9/8 阅读 30 分钟（无区间，直接相加）
    add(D(8), read, "《奇特的一生》", 30)
    dd = stats.day_detail(conn, "2025-09-08")
    check("科研类时长 120、会议类时长 120（各类别不去重）",
          dd["category_minutes"].get("科研") == 120 and dd["category_minutes"].get("会议") == 120,
          str(dd["category_minutes"]))
    check("实际时长（去重）= 并集 180 + 手填 30 = 210 分钟", dd["actual_minutes"] == 210,
          str(dd["actual_minutes"]))
    check("投入时长之和 = 120+120+30 = 270 分钟", dd["invested_minutes"] == 270,
          str(dd["invested_minutes"]))
    check("实际时长 < 投入时长之和（并行去重生效）",
          dd["actual_minutes"] < dd["invested_minutes"])
    check("工作时长（科研+会议合并去重）= 180 + 0 手填 = 180 分钟", dd["work_minutes"] == 180,
          str(dd["work_minutes"]))

    line("-")
    print("③ Top 任务（规格 5.3）")
    top = stats.top_tasks(conn, "2025-09-01", "2025-09-30", 5)
    print("   Top:", [(t["task_name"], t["category_name"], t["hours"], t["share"]) for t in top])
    check("Top1 = 升力计算 240 分钟（120+120）",
          top and top[0]["task_name"] == "升力计算" and top[0]["minutes"] == 240,
          str(top[0]) if top else "空")
    check("Top 附了所属大类", top and top[0]["category_name"] == "科研")
    check("Top1 占比 = 240/510 = 0.4706", abs(top[0]["share"] - 240 / 510.0) < 5e-5,
          str(top[0]["share"]))
    allt = stats.task_totals(conn, "2025-09-01", "2025-09-30")
    check("全部任务占比合计 ≈ 1.0（占比保留 4 位小数，允许 0.001 取整误差）",
          abs(sum(t["share"] for t in allt) - 1.0) < 0.001,
          str(sum(t["share"] for t in allt)))

    line("-")
    print("④ 图表数据源（每日柱状/大类占比/月对比）")
    series = stats.daily_series(conn, "2025-09-01", "2025-09-07")
    check("每日序列长度 = 7 天且含星期", len(series) == 7 and series[0]["weekday"] == "周一",
          str(series[0]))
    check("9/1 那天柱状数据 = 科研120+会议60", series[0]["categories"] == {"科研": 120, "会议": 60},
          str(series[0]["categories"]))
    check("9/2 空白日柱状为空", series[1]["categories"] == {}, str(series[1]["categories"]))

    line("-")
    print("⑤ 月报自然日分母（规格 5.2 下界 + 基线反推的上界）")
    md = stats.month_detail(conn, "2025-09-15")
    check("2025-09 自然日 = 库内 9/1~9/8 共 8 天（末端按最后记录日截断）",
          md["natural_days"] == 8, "%d 天（%s）" % (md["natural_days"], md["natural_days_note"]))
    md2 = stats.month_detail(conn, "2025-08-15")
    check("库里 2025-08 没有记录 -> 自然日 0 天、不报错", md2["natural_days"] == 0,
          "%d 天（%s）" % (md2["natural_days"], md2.get("natural_days_note")))

    # ============================================================ M4 目标与达标
    line("=")
    print("⑥ 目标设置 + 周六折算 50%（规格 3.6 / 5.4，验收第 10 条）")
    r = goals.set_goal(conn, sci, "workday", 180)
    check("设科研工作日目标 3:00 -> 成功", r["ok"], r.get("message", r.get("error", "")))
    r = goals.set_goal(conn, sci, "weekend", 60)
    check("设科研周末目标 1:00 -> 成功", r["ok"], r.get("message", r.get("error", "")))
    r = goals.set_goal(conn, meet, "workday", 60)
    check("设会议工作日目标 1:00 -> 成功", r["ok"], r.get("message", r.get("error", "")))
    r = goals.set_goal(conn, sci, "workday", 0)
    check("把目标设为 0 -> 清除该目标", r["ok"] and "清除" in r.get("message", ""),
          r.get("message", ""))
    goals.set_goal(conn, sci, "workday", 180)          # 恢复

    g_mon = {g["category_name"]: g for g in goals.effective_goal(conn, "2025-09-01")}
    g_sat = {g["category_name"]: g for g in goals.effective_goal(conn, "2025-09-06")}
    g_sun = {g["category_name"]: g for g in goals.effective_goal(conn, "2025-09-07")}
    check("周一（工作日）：科研目标 180 分钟", g_mon["科研"]["minutes"] == 180,
          str(g_mon.get("科研")))
    check("周六：科研目标 = 180 × 50% = 90 分钟", g_sat["科研"]["minutes"] == 90,
          "%s（%s）" % (g_sat["科研"]["minutes"], g_sat["科研"]["goal_type"]))
    check("周日：科研目标 = 周末目标 60 分钟", g_sun["科研"]["minutes"] == 60,
          "%s（%s）" % (g_sun["科研"]["minutes"], g_sun["科研"]["goal_type"]))
    check("会议没有设周末目标 -> 周日不参与判定", "会议" not in g_sun, str(list(g_sun)))

    line("-")
    print("⑦ 达标日判定（当天所有已设目标的大类都要达到）")
    # 9/1：科研 120（目标180，不达标）
    st = goals.day_goal_status(conn, "2025-09-01", {"科研": 120, "会议": 60})
    check("9/1 科研 120 < 180 -> 未达标", st["achieved"] is False,
          str([(i["category_name"], i["got_hm"], i["goal_hm"], i["ok"]) for i in st["items"]]))
    # 给 9/1 补够科研
    add(D(1), sci, "补记", 60)
    st = goals.day_goal_status(conn, "2025-09-01", {"科研": 180, "会议": 60})
    check("9/1 科研刚好 180（等于目标）+ 会议 60 -> 达标", st["achieved"] is True,
          str([(i["category_name"], i["got_hm"], i["goal_hm"], i["ok"]) for i in st["items"]]))
    st = goals.day_goal_status(conn, "2025-09-01", {"科研": 180, "会议": 30})
    check("会议只有 30 < 60 -> 整体未达标（一门不达标就不算）", st["achieved"] is False)

    line("-")
    print("⑧ 连续达标天数（中断一天即清零，无豁免）")
    # 造一个干净的区间：2025-09-10(三)~09-16(二)
    # 目标：科研工作日 180、周末 60；会议工作日 60
    for i in range(5):      # 9/10(三) ~ 9/12(五) 达标三天，9/13(六)、9/14(日) 达标
        pass
    # 清掉旧记录，避免干扰
    conn.execute("DELETE FROM records")
    def mk(day, cmap):
        for cname, mins in cmap.items():
            cid = {"科研": sci, "会议": meet, "阅读": read, "运动": run}[cname]
            add(util.parse_day(day), cid, "任务" + cname, mins)
    # 9/10 达标：科研 180 + 会议 60
    mk("2025-09-10", {"科研": 180, "会议": 60})
    mk("2025-09-11", {"科研": 200, "会议": 90})
    # 9/12 不达标（科研只有 100）
    mk("2025-09-12", {"科研": 100, "会议": 60})
    # 9/13 周六：科研目标 90，会议目标 30（60×50%）-> 达标
    mk("2025-09-13", {"科研": 90, "会议": 30})
    # 9/14 周日：科研周末目标 60（会议没设周末目标 -> 不参与）-> 达标
    mk("2025-09-14", {"科研": 60})
    st = goals.streak_days(conn, today=D(30))
    check("最近一天 9/14 达标、9/13 达标、9/12 不达标 -> 连续 2 天",
          st["days"] == 2, "%d 天（%s ~ %s）" % (st["days"], st["from"], st["to"]))
    # 把 9/12 也补成达标 -> 连续应变 5 天
    add(D(12), sci, "补记12", 80)
    st = goals.streak_days(conn, today=D(30))
    check("把 9/12 补达标后 -> 连续 5 天", st["days"] == 5,
          "%d 天（%s ~ %s）" % (st["days"], st["from"], st["to"]))
    # 9/11 改不达标 -> 只剩 2 天（9/13、9/14 是 9/12 之后的？不，往前数到 9/12、9/12 达标，9/11 不达标 -> 3 天）
    conn.execute("UPDATE records SET duration_min=10 WHERE day='2025-09-11' AND task_name='任务科研'")
    st = goals.streak_days(conn, today=D(30))
    check("9/11 改成不达标 -> 只剩 3 天（9/12、9/13、9/14）", st["days"] == 3,
          "%d 天（%s ~ %s）" % (st["days"], st["from"], st["to"]))
    conn.execute("UPDATE records SET duration_min=200 WHERE day='2025-09-11' AND task_name='任务科研'") 

    line("-")
    print("⑨ 今天还没达标时，不把今天算作中断（用户 2026-10-12 确认）")
    mk("2025-09-15", {"科研": 10})           # 9/15 周一，科研 10 分钟 -> 不达标
    st = goals.streak_days(conn, today=D(15))
    check("今天(9/15)未达标 -> 从昨天起算，连续 5 天", st["days"] == 5,
          "%d 天（%s ~ %s）" % (st["days"], st["from"], st["to"]))
    st = goals.streak_days(conn, today=D(16))
    check("隔天再看（9/15 已成过去）-> 中断清零，从 9/15 起的连续 0 天", st["days"] == 0,
          "%d 天" % st["days"])

    line("-")
    print("⑩ 区间达成率（验收第 10 条）")
    rng = stats.range_with_goals(conn, "2025-09-10", "2025-09-16")
    g = rng["goal"]
    print("   计入判定 %d 天，达标 %d 天，未达标 %d 天，达成率 %.4f"
          % (g["judged_days"], g["achieved_days"], g["fail_days"], g["rate"]))
    check("计入判定 7 天（9/10~9/16 每天都有目标）", g["judged_days"] == 7, str(g["judged_days"]))
    check("达标 5 天（9/10、9/11、9/12、9/13、9/14）", g["achieved_days"] == 5,
          str(g["achieved_days"]))
    check("达成率 = 5/7 = 0.7143", abs(g["rate"] - 0.7143) < 1e-9, str(g["rate"]))

    line("-")
    print("⑪ 一个目标都没设时：隐藏达标模块并提示")
    conn.execute("DELETE FROM goals")
    rng = stats.range_with_goals(conn, "2025-09-10", "2025-09-16")
    check("没有目标 -> has_goal 为假", rng["goal"]["has_goal"] is False, str(rng["goal"]))
    check("提示文案 = 你还没设置目标", rng["goal"].get("text") == "你还没设置目标",
          str(rng["goal"].get("text")))
    check("连续达标天数也返回未设置", goals.streak_days(conn)["has_goal"] is False)

    # ============================================================ 计划 vs 实际
    line("=")
    print("⑫ 计划 vs 实际（规格 6.3，二级任务必填）")
    goals.set_goal(conn, sci, "workday", 180)
    r = goals.add_plan(conn, "2025-09-20", sci, None, "", "3:00")
    check("计划不填二级任务 -> 被拒", not r["ok"], r.get("error", ""))
    r = goals.add_plan(conn, "2025-09-20", sci, None, "升力计算", "3:00")
    check("计划填好 -> 成功", r["ok"], r.get("message", r.get("error", "")))
    r = goals.add_plan(conn, "2025-09-20", sci, None, "翼型测量", None)
    check("计划时长留空 -> 允许（只标记要做）", r["ok"], r.get("message", r.get("error", "")))
    add(D(20), sci, "升力计算", 200)          # 实际做了 3:20，超计划 3:00
    add(D(20), sci, "另外冒出来的活", 30)      # 计划外
    add(util.parse_day("2025-09-21"), sci, "翼型测量", 40)   # 9/21 执行了 9/20 的翼型测量？
    # 注意：翼型测量计划在 9/20，实际记在 9/21 -> 应算"未执行"
    pv = goals.plan_vs_actual(conn, "2025-09-20", "2025-09-21")
    check("计划任务数 = 2", pv["plan_count"] == 2, str(pv["plan_count"]))
    check("实际执行数 = 1（升力计算）", pv["executed_count"] == 1, str(pv["executed_count"]))
    check("未执行清单 = 翼型测量", pv["unexecuted_count"] == 1 and
          pv["undone"][0]["task_name"] == "翼型测量", str([u["task_name"] for u in pv["undone"]]))
    check("计划时长合计 = 180 分钟（3:00）", pv["planned_minutes"] == 180,
          str(pv["planned_minutes"]))
    check("实际时长（已执行计划任务的）= 200 分钟", pv["actual_minutes"] == 200,
          str(pv["actual_minutes"]))
    check("偏差 = 200-180 = +20 分钟", pv["diff_minutes"] == 20, str(pv["diff_minutes"]))
    check("计划外任务被单列出来", any(e["task_name"] == "另外冒出来的活" for e in pv["extra"]),
          str([e["task_name"] for e in pv["extra"]]))
    r = goals.plan_week(conn, "2025-09-22", [
        {"weekday": 1, "category_id": sci, "task_name": "升力计算", "planned": "2:00"},
        {"weekday": 3, "category_id": meet, "task_name": "小组会", "planned": "1:00"},
        {"weekday": 6, "category_id": run, "task_name": "跑步", "planned": "0:40"},
    ])
    check("周计划一次排 3 天 -> 成功 3 条", r["ok"] and r["created"] == 3, str(r))
    wk_plans = goals.list_plans(conn, "2025-09-22", "2025-09-28")
    check("周计划落在周一(9/22)/周三(9/24)/周六(9/27)",
          [p["day"] for p in wk_plans] == ["2025-09-22", "2025-09-24", "2025-09-27"],
          str([p["day"] for p in wk_plans]))

    line("-")
    print("⑬ 今日/本周还差多少（规格 6.1 第 2 条）")
    pr = goals.progress(conn, "2025-09-20")
    print("   今日:", [(i["category_name"], i["got_hm"], i["goal_hm"], i["remaining_hm"])
                     for i in pr["today"]])
    print("   本周:", [(i["category_name"], i["got_hm"], i["goal_hm"], i["remaining_hm"],
                      "可追上" if i["on_track"] else "按当前进度追不上")
                     for i in pr["week"]])
    check("今日进度含科研，已 3:20 超过目标 3:00", 
          any(i["category_name"] == "科研" and i["ok"] for i in pr["today"]),
          str(pr["today"]))
    check("本周进度给出剩余与能否达标", all("on_track" in i for i in pr["week"]))

    conn.close()
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suf):
            os.remove(tmp + suf)

    line("=")
    if FAIL:
        print("M3+M4 自测：%d 项不通过 -> %s" % (len(FAIL), FAIL))
    else:
        print("M3+M4 自测：全部通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
