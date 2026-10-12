# -*- coding: utf-8 -*-
"""M2 自测：计时（并行/跨午夜/关页面保留/超时确认/事后改起止）+ 补录 + 必填校验。

用法：python -m app.selftest_m2
为了不污染真实数据，本脚本用一份临时数据库 data\\_selftest_m2.db，
结束后把里面用到的口径数字打印出来，并删除临时库。
"""

import os
import sys
import datetime

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import db, util, timers, stats
else:
    from . import db, util, timers, stats

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
    tmp_path = os.path.join(db.DATA_DIR, "_selftest_m2.db")
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(tmp_path + suf):
            os.remove(tmp_path + suf)
    conn = db.init_db(db.connect(tmp_path))
    # 清掉上次自测残留的记录，保证每次跑出来的绝对数字都一样
    conn.execute("DELETE FROM record_products")
    conn.execute("DELETE FROM records")
    conn.execute("DELETE FROM timers")
    conn.execute("DELETE FROM plans")
    print("临时库：", tmp_path)

    cats = {c["name"]: c for c in db.all_categories(conn)}
    sci, meet = cats["科研"]["id"], cats["会议"]["id"]
    run_cat = db.ensure_category(conn, "运动", 0)

    # ============================================================ 验收 2：必填校验
    line("=")
    print("① 未选全「大类+二级任务+标签」时不能开始计时（验收第 2 条）")
    r = timers.start(conn, None, None, "升力计算", ["TGV"])
    check("不选大类 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.start(conn, sci, None, "", ["TGV"])
    check("不填二级任务 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.start(conn, sci, None, "升力计算", [])
    check("不填标签 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.start(conn, sci, None, "升力计算", ["TGV"])
    check("三类都齐 -> 开计时成功", r["ok"], r.get("error", ""))
    t1 = r["timer_id"] if r["ok"] else None
    check("计时状态确实写进了数据库", conn.execute(
        "SELECT COUNT(*) FROM timers").fetchone()[0] == 1)
    timers.cancel(conn, t1)

    # ============================================================ 验收 3：关页面后仍在计时
    line("=")
    print("② 关掉页面/重开进程后，计时状态仍在走（验收第 3 条）")
    r = timers.start(conn, sci, None, "升力计算", ["TGV"])
    tmain = r["timer_id"]
    # 模拟"关掉浏览器"：直接断开连接，再重新打开
    conn.close()
    conn = db.connect(tmp_path)
    tl = timers.list_timers(conn)
    check("重开连接后仍能看到正在计时", len(tl) == 1, "条数=%d" % len(tl))
    # 模拟"10 分钟前就开始计时了"：把起点改早，再读
    past = util.now_local() - datetime.timedelta(minutes=10)
    conn.execute("UPDATE timers SET seg_started_at=?, created_at=? WHERE id=?",
                 (util.iso(past), util.iso(past), tmain))
    tl = timers.list_timers(conn)
    check("已计时间按真实时间推进 = 600 秒左右（显示 %s）" % tl[0]["elapsed_clock"],
          595 <= tl[0]["elapsed_sec"] <= 605, "实际 %d 秒" % tl[0]["elapsed_sec"])

    # ============================================================ 验收 3：停止即落库
    r = timers.stop(conn, tmain)
    check("停止后立刻落库（无追问）", r["ok"] and r["saved"]["record_id"] is not None,
          r.get("message", ""))
    rec = db.record_by_id(conn, r["saved"]["record_id"])
    check("记录时长 = 10 分钟", rec["duration_min"] == 10, str(rec["duration_min"]))
    check("来源标记为 timer", rec["source"] == "timer")
    check("带起止时间", bool(rec["start_ts"] and rec["end_ts"]),
          "%s ~ %s" % (rec["start_ts"], rec["end_ts"]))
    check("计时器已从 timers 表移除", conn.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == 0)

    # ============================================================ 验收 4：并行 + 去重口径
    line("=")
    print("③ 并行两个计时器：各类别不去重、实际时长按区间并集去重（验收第 4 条）")
    today = util.now_local().date().isoformat()
    # 用固定时间轴（22:00 之后），这样不论几点跑自测，结论都一样。
    # 【教训】最初这里用了 10:00–11:30，而上一节那条 10 分钟的记录是"现在往前 10 分钟"，
    # 于是自测在 11:00 之前跑和之后跑，并集结果会不一样 —— 那是自测脚本的缺陷，不是软件的。
    base = datetime.datetime.combine(util.now_local().date(), datetime.time(22, 0, 0))
    a0, a1 = base, base + datetime.timedelta(hours=1)          # 科研 22:00-23:00
    b0, b1 = base + datetime.timedelta(minutes=30), base + datetime.timedelta(minutes=90)
    root_task, _ = db.ensure_task(conn, sci, "升力计算")
    run_task, _ = db.ensure_task(conn, run_cat, "跑步")
    db.insert_record(conn, today, sci, root_task, "升力计算", ["TGV"], 60, "timer",
                     start_ts=util.iso(a0), end_ts=util.iso(a1))
    db.insert_record(conn, today, run_cat, run_task, "跑步", ["健身"], 60, "timer",
                     start_ts=util.iso(b0), end_ts=util.iso(b1))
    dd = stats.day_detail(conn, today)
    check("科研类时长 = 60 分钟（不去重）", dd["category_minutes"].get("科研", 0) - 10 == 60,
          "本次新增 %d 分钟" % (dd["category_minutes"].get("科研", 0) - 10))
    check("运动类时长 = 60 分钟（不去重）", dd["category_minutes"].get("运动") == 60,
          str(dd["category_minutes"].get("运动")))
    check("投入时长之和 = 原有 10 分钟 + 新增 120 分钟", dd["invested_minutes"] == 130,
          str(dd["invested_minutes"]))
    # 上一节那条 10 分钟的记录落在"今天早些时候"，与这里的 22:00 之后不重叠，
    # 所以并集 = 10 + 90 = 100 分钟；两条并行段重叠的 30 分钟只算一次。
    check("实际时长（去重）= 10 + 90 = 100 分钟（并行重叠的 30 分钟只算一次）",
          dd["actual_minutes"] == 100, str(dd["actual_minutes"]))
    check("工作时长（科研+会议，去重）= 10 + 60 = 70 分钟（运动不计入）",
          dd["work_minutes"] == 70, str(dd["work_minutes"]))

    # ============================================================ 验收 5：跨午夜自动切两条
    line("=")
    print("④ 23:30–01:00 的计时段自动切成两条、分别归属两天（验收第 5 条）")
    # 用一条真实的计时器 + 把它的开始时间改到"昨天 23:30"，现在时刻当作"今天 01:00"
    # 先清掉前面几节残留的计时器，免得干扰本节的断言
    for x in timers.list_timers(conn):
        timers.cancel(conn, x["id"])

    # 注意：这一段用"假时间"把计时器挪到昨天 23:30，所有后续调用都要用同一个假时间，
    # 否则系统时间"往回调"会让累计值失真。
    r = timers.start(conn, meet, None, "小组会", ["组会"])
    tid = r["timer_id"]
    fake_now = datetime.datetime.combine(util.now_local().date(), datetime.time(1, 0, 0))
    prev = fake_now - datetime.timedelta(days=1)
    started = datetime.datetime.combine(prev.date(), datetime.time(23, 30, 0))
    conn.execute("UPDATE timers SET seg_started_at=?, created_at=? WHERE id=?",
                 (util.iso(started), util.iso(started), tid))
    made = timers.split_crossing_midnight(conn, now=fake_now)
    check("切出 1 条历史记录", len(made) == 1, str(made))
    check("切出的时长 = 30 分钟（23:30:00 -> 23:59:59）",
          made and made[0]["minutes"] == 30, str(made[0]["minutes"]) if made else "无")
    check("切出的记录归属「昨天」", made and made[0]["day"] == prev.date().isoformat(),
          made[0]["day"] if made else "无")
    tl = timers.list_timers(conn, now=fake_now)
    check("计时器继续在走，剩余 = 60 分钟（%s）且归属今天"
          % (tl[0]["elapsed_clock"] if tl else "无"),
          len(tl) == 1 and tl[0]["elapsed_minutes"] == 60,
          str(tl[0]["elapsed_minutes"]) if tl else "无")
    # 停止，第二条应归今天
    r = timers.stop(conn, tid, now=fake_now)
    rec2 = db.record_by_id(conn, r["saved"]["record_id"])
    check("停止后第二条归属「今天」", rec2["day"] == util.now_local().date().isoformat(),
          rec2["day"])
    check("两条都在，日期不同、内容相同",
          conn.execute("SELECT COUNT(*) FROM records WHERE task_name='小组会' AND source='timer'"
                       ).fetchone()[0] == 2)

    # ============================================================ 验收 6：补录校验
    line("=")
    print("⑤ 事后补录：无标签被拒、带标签成功（验收第 6 条）")
    r = timers.add_manual(conn, today, sci, None, "升力计算", [], "3:30")
    check("无标签 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.add_manual(conn, today, sci, None, "", ["TGV"], "3:30")
    check("无二级任务 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.add_manual(conn, today, None, None, "升力计算", ["TGV"], "3:30")
    check("无大类 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.add_manual(conn, today, sci, None, "升力计算", ["TGV"], "0:00")
    check("时长 0 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.add_manual(conn, today, sci, None, "升力计算", ["TGV"], "abc")
    check("时长乱填 -> 被拒且是中文提示", not r["ok"] and "时长" in r.get("error", ""),
          r.get("error", ""))
    r = timers.add_manual(conn, "2030-01-01", sci, None, "升力计算", ["TGV"], "1:00")
    check("未来日期 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.add_manual(conn, today, sci, None, "升力计算", ["TGV项目"], "3:30",
                          note="今天卡在网格质量上",
                          products=[{"ptype": "代码", "name": "wake_survey.py", "url": ""}])
    check("带标签 -> 成功", r["ok"], r.get("error", ""))
    check("时长 3:30 = 210 分钟", r["ok"] and r["record"]["duration_min"] == 210,
          str(r["record"]["duration_min"]) if r["ok"] else "")
    check("产出物已保存", r["ok"] and len(r["record"]["products"]) == 1)
    check("标签是新建后立刻可用", db.all_tags(conn) and
          any(t["name"] == "TGV项目" for t in db.all_tags(conn)))

    # 时长写法兼容性
    line("-")
    print("时长输入兼容性（规格 4.4）")
    for text, expect in [("1:46", 106), ("1：46", 106), ("1h46m", 106), ("106", 106),
                         ("1小时46分", 106), ("1.5h", 90), ("90m", 90), ("3:5", 185)]:
        m, e = util.parse_duration(text)
        check("%-10s -> %d 分钟" % (text, expect), m == expect, "实际 %s / %s" % (m, e))

    # ============================================================ 验收 7：超时确认
    line("=")
    print("⑥ 计时超过 4:00 未停止 -> 下次打开弹窗确认/截断（验收第 7 条）")
    r = timers.start(conn, sci, None, "读Mario博士论文", ["文献"])
    tid = r["timer_id"]
    long_ago = util.now_local() - datetime.timedelta(hours=5, minutes=20)
    conn.execute("UPDATE timers SET seg_started_at=?, created_at=? WHERE id=?",
                 (util.iso(long_ago), util.iso(long_ago), tid))
    pend = timers.overtime_pending(conn)
    check("检出 1 条超时计时", len(pend) == 1, str(pend))
    check("阈值提示为 4:00", pend and pend[0]["limit_hm"] == "4:00")
    check("已计时间约 5:20", pend and pend[0]["elapsed_hm"] == "5:20",
          pend[0]["elapsed_hm"] if pend else "")
    r = timers.resolve_overtime(conn, tid, "truncate", util.iso(long_ago + datetime.timedelta(hours=2)))
    check("截断到开始后 2 小时 -> 保存 2:00", r["ok"] and "2:00" in r.get("message", ""),
          r.get("message", r.get("error", "")))
    check("截断后计时器已停止", conn.execute("SELECT COUNT(*) FROM timers").fetchone()[0] == 0)
    # keep 与 delete 两条分支
    r = timers.start(conn, sci, None, "读Mario博士论文", ["文献"])
    tid = r["timer_id"]
    conn.execute("UPDATE timers SET seg_started_at=?, created_at=? WHERE id=?",
                 (util.iso(long_ago), util.iso(long_ago), tid))
    r = timers.resolve_overtime(conn, tid, "keep")
    check("选「保留到实际时长」-> 保存 5:20", r["ok"] and r["saved"]["minutes"] == 320,
          str(r["saved"]["minutes"]) if r["ok"] else r.get("error", ""))
    r = timers.start(conn, sci, None, "读Mario博士论文", ["文献"])
    tid = r["timer_id"]
    conn.execute("UPDATE timers SET seg_started_at=?, created_at=? WHERE id=?",
                 (util.iso(long_ago), util.iso(long_ago), tid))
    n_before = db.count_records(conn)
    r = timers.resolve_overtime(conn, tid, "delete")
    check("选「删除这段」-> 不生成记录", r["ok"] and db.count_records(conn) == n_before)

    # ============================================================ 事后改起止
    line("=")
    print("⑦ 计时段事后编辑起止时间，自动重算时长（规格 4.1 第 7 条）")
    r = timers.start(conn, sci, None, "翼型测量", ["风洞"])
    tid = r["timer_id"]
    conn.execute("UPDATE timers SET seg_started_at=?, created_at=? WHERE id=?",
                 (util.iso(util.now_local() - datetime.timedelta(minutes=45)),
                  util.iso(util.now_local() - datetime.timedelta(minutes=45)), tid))
    r = timers.stop(conn, tid)
    rid = r["saved"]["record_id"]
    check("停止落库 45 分钟", r["saved"]["minutes"] == 45, str(r["saved"]["minutes"]))
    s = datetime.datetime.combine(util.now_local().date(), datetime.time(9, 0, 0))
    e = datetime.datetime.combine(util.now_local().date(), datetime.time(10, 30, 0))
    r = timers.edit_record_times(conn, rid, util.iso(s), util.iso(e))
    check("改成 09:00~10:30 -> 时长重算 90 分钟",
          r["ok"] and r["record"]["duration_min"] == 90,
          str(r["record"]["duration_min"]) if r["ok"] else r.get("error", ""))
    r = timers.edit_record_times(conn, rid, util.iso(e), util.iso(s))
    check("起止颠倒 -> 被拒", not r["ok"], r.get("error", ""))
    r = timers.edit_record_times(conn, rid, util.iso(s), util.iso(s))
    check("0 分钟 -> 被拒", not r["ok"], r.get("error", ""))
    # 只填了时长、没有时间段的补录记录，不能事后补时间段
    # （2026-10-12 起：补录时可以当场填起始/终止时间，那种记录算"带区间"，也能改；
    #  但没有时间段的记录事后加时间段会改变统计口径，仍然不允许）
    m = timers.add_manual(conn, today, sci, None, "升力计算", ["TGV"], "1:00")
    r = timers.edit_record_times(conn, m["record_id"], util.iso(s), util.iso(e))
    check("没有时间段的补录记录改起止 -> 被拒并说明原因",
          not r["ok"] and "没有时间段" in r.get("error", ""), r.get("error", ""))
    # 补录时填了时间段的记录，可以像计时记录一样改起止时间
    m2 = timers.add_manual(conn, today, sci, None, "升力计算", ["TGV"], "",
                           start_time="00:10", end_time="00:40")
    if m2.get("ok"):
        r = timers.edit_record_times(conn, m2["record_id"], "%s 00:10:00" % today,
                                     "%s 01:10:00" % today)
        check("带时间段的补录记录改起止 -> 允许且时长重算为 60 分钟",
              r["ok"] and r["record"]["duration_min"] == 60,
              (r.get("error") or "") if not r["ok"] else str(r["record"]["duration_min"]))

    # ============================================================ 暂停不计入
    line("=")
    print("⑧ 暂停期间不计入时长（用户 2026-10-12 确认）")
    r = timers.start(conn, sci, None, "升力计算", ["TGV"])
    tid = r["timer_id"]
    past = util.now_local() - datetime.timedelta(minutes=30)
    conn.execute("UPDATE timers SET seg_started_at=?, created_at=? WHERE id=?",
                 (util.iso(past), util.iso(past), tid))
    timers.pause(conn, tid)
    tl = timers.list_timers(conn)
    check("暂停时累计 = 30 分钟（%s）" % tl[0]["elapsed_clock"], tl[0]["elapsed_minutes"] == 30,
          str(tl[0]["elapsed_minutes"]))
    # 暂停 20 分钟后再恢复
    conn.execute("UPDATE timers SET seg_started_at=? WHERE id=?",
                 (util.iso(util.now_local() - datetime.timedelta(minutes=20)), tid))
    timers.resume(conn, tid)
    tl = timers.list_timers(conn)
    check("恢复后仍然只累计 30 分钟（暂停的 20 分钟没算进去）",
          tl[0]["elapsed_minutes"] == 30, str(tl[0]["elapsed_minutes"]))
    conn.execute("UPDATE timers SET seg_started_at=? WHERE id=?",
                 (util.iso(util.now_local() - datetime.timedelta(minutes=10)), tid))
    r = timers.stop(conn, tid)
    check("停止时 = 30 + 10 = 40 分钟", r["saved"]["minutes"] == 40, str(r["saved"]["minutes"]))

    # ============================================================ 汇总
    line("=")
    print("本日汇总（自测库里造出来的数据）：")
    dd = stats.day_detail(conn, today)
    print("   记录 %d 条，实际时长 %s，工作时长 %s，投入时长之和 %s"
          % (dd["record_count"], util.fmt_hm(dd["actual_minutes"]),
             util.fmt_hm(dd["work_minutes"]), util.fmt_hm(dd["invested_minutes"])))
    tl = timers.list_timers(conn)
    print("   剩余正在计时：%d 条" % len(tl))

    conn.close()
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(tmp_path + suf):
            os.remove(tmp_path + suf)

    line("=")
    if FAIL:
        print("M2 自测：%d 项不通过 -> %s" % (len(FAIL), FAIL))
    else:
        print("M2 自测：全部通过")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
