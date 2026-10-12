# -*- coding: utf-8 -*-
"""计时引擎（规格 4.1）与记录录入（规格 4.2）。

关键设计
--------
* **计时状态在后端**（timers 表），不在浏览器里：关掉页面、重启电脑后仍在计时。
* **暂停期间不计入**：单段时长不是用"现在 - 开始"算，而是用 elapsed_sec 累计——
  运行时才把时间加进去，暂停时不动。用户 2026-10-12 确认。
* **并行计时**：同一时刻可以有多条 timers 记录，互不影响。停止时独立落库。
* **跨午夜自动切开**：一次计时跨过 00:00 时，自动把上一段（到当天 23:59:59）落库，
  然后新开一段从 00:00 起算，两段各自归属自己的日期（规格 3.4 / 4.1 / 验收 5）。
* **忘记停止**：单段超过阈值（默认 4:00）时，打开软件由前端弹窗，让用户在
  「保留到实际时长 / 截断到某个时间点 / 删除这段」里三选一（规格 4.1 第 6 条）。
"""

import datetime
import collections

from . import db, util


# ================================================================ 内部工具

def _now():
    return util.now_local()


def _accumulate(t, now):
    """算出这条计时"到目前为止总共计入了多少秒"。

    数据模型（刻意做到没有歧义）：
      seg_started_at = 当前这一段"显示计时"的起点，也就是 elapsed_sec 那一刻的墙上时间。
                       恢复计时时把它设为"现在"，于是显示就从那一刻重新开始数。
      elapsed_sec    = 当前这一段之前的停止段（上一次运行 + 上一次暂停）已计入的秒数。
    所以：
      运行中 -> elapsed_sec + (now - seg_started_at)
      暂停中 -> elapsed_sec（暂停期间不多算，用户 2026-10-12 确认）
    显示锚点 = seg_started_at - elapsed_sec = 当前这一段真实开始走的时刻。
    """
    elapsed = int(t["elapsed_sec"])
    seg_start = util.parse_iso(t["seg_started_at"])
    if t["status"] == "running":
        walked = int((now - seg_start).total_seconds())
        if walked > 0:
            elapsed += walked
        else:
            seg_start = now        # 时钟往回调：只把锚点跟着走，绝不产生负时长
    return elapsed, seg_start, t["status"]


def _timer_row(conn, tid):
    row = conn.execute(
        "SELECT t.*, c.name AS category_name, c.counts_work, c.archived AS cat_archived "
        "FROM timers t JOIN categories c ON c.id=t.category_id WHERE t.id=?", (tid,)).fetchone()
    return dict(row) if row else None


def _persist_accumulate(conn, tid, now):
    """把累计值写回库里（让"已计时间"即使页面关着也在增长）。"""
    t = _timer_row(conn, tid)
    if t is None:
        return None
    if t["status"] == "running":
        elapsed, seg_start, _ = _accumulate(t, now)
        conn.execute("UPDATE timers SET elapsed_sec=?, seg_started_at=? WHERE id=?",
                     (elapsed, util.iso(now), tid))
        t["elapsed_sec"] = elapsed
        t["seg_started_at"] = util.iso(now)
    return t


def _parse_tags(tags):
    if isinstance(tags, (list, tuple)):
        return [str(x).strip() for x in tags if str(x).strip()]
    return [x.strip() for x in str(tags or "").replace("\r", "").split("\n") if x.strip()]


def _register_tags(conn, tags):
    for name in _parse_tags(tags):
        db.ensure_tag(conn, name)


def _undo(conn, action, label, payload):
    """写一条撤销日志（规格 8.5）。只保留最近 N 步由 backup/undo 清理负责。"""
    import json
    conn.execute("INSERT INTO undo_log(action, label, payload) VALUES (?,?,?)",
                 (action, label, json.dumps(payload, ensure_ascii=False, default=str)))


# ================================================================ 跨午夜

def split_crossing_midnight(conn, now=None):
    """把已经跨过午夜的计时段自动切开（规格 3.4、4.1、验收第 5 条）。

    做法：把上一段在 00:00 之前的部分按当天日期落库（结束时间 = 当天 23:59:59），
    然后让这条计时器从 00:00 重新开始累计。
    返回被切出来的记录列表（可能为空）。

    只在"当前这一段（本次运行）的起点"早于今天 00:00 时才切——暂停中的计时器
    不会自己跨午夜（暂停期间本来就不计入时长）。
    """
    now = now or _now()
    today0 = datetime.datetime.combine(now.date(), datetime.time(0, 0, 0))
    made = []
    # 按 seg_started_at 升序处理，并记录已经处理过的 id——
    # 否则同一轮里先把 seg_started_at 挪到今天 00:00 之后，循环再次读到它就会重复处理。
    done = set()
    sql = ("SELECT t.* FROM timers t JOIN categories c ON c.id=t.category_id "
           "WHERE t.status='running' ORDER BY t.seg_started_at, t.id")
    for row in [dict(r) for r in conn.execute(sql)]:
        if row["id"] in done:
            continue
        done.add(row["id"])
        run_start = util.parse_iso(row["seg_started_at"])
        if run_start >= today0:
            continue                          # 这一段还在今天里，不用切
        pre_seconds = int((today0 - run_start).total_seconds())
        if pre_seconds <= 0:
            continue

        boundary = today0 - datetime.timedelta(seconds=1)   # 当天 23:59:59
        pre_date = (today0 - datetime.timedelta(days=1)).date()
        pre_minutes = int(round(pre_seconds / 60.0))
        if pre_minutes > 0:
            rid = db.insert_record(
                conn, pre_date.isoformat(), row["category_id"], row["task_id"],
                row["task_name"], row["tags"], pre_minutes, "timer",
                start_ts=util.iso(run_start), end_ts=util.iso(boundary),
                note=row["note"], cross_midnight=1)
            _undo(conn, "record_create",
                  "跨午夜自动切分：%s %s 记入 %s"
                  % (row["task_name"], util.fmt_hm(pre_minutes), pre_date.isoformat()),
                  {"record_id": rid})
            made.append({"record_id": rid, "day": pre_date.isoformat(),
                         "minutes": pre_minutes, "timer_id": row["id"]})

        # 新的一天从 00:00 重新开始：elapsed_sec 归零、锚点设为今天 00:00。
        # 这样"今日已计"从 0 起算，而昨天那一段已经落库，两段之和 = 实际经过的时间。
        conn.execute(
            "UPDATE timers SET elapsed_sec=0, seg_started_at=?, cross_midnight=1 WHERE id=?",
            (util.iso(today0), row["id"]))
    return made


# ================================================================ 计时操作

def start(conn, category_id, task_id=None, task_name=None, tags=None, note=""):
    """开始计时。规格 4.1：大类 + 二级任务 + 标签必须齐全，否则不给开始。"""
    if not category_id:
        return {"ok": False, "error": "请先选择大类，再开始计时。"}
    cat = db.category_by_id(conn, category_id)
    if cat is None:
        return {"ok": False, "error": "找不到这个大类，请刷新页面后重试。"}
    tag_list = _parse_tags(tags)
    if not tag_list:
        return {"ok": False, "error": "标签至少要选一个（或新建一个）才能开始计时。"}

    # 二级任务：可以用 id，也可以直接用名字（名字不存在就新建，规格 3.3 当场新建的作风）
    name = (task_name or "").strip()
    if task_id:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "找不到这个二级任务，请刷新页面后重试。"}
        if row["category_id"] != cat["id"]:
            return {"ok": False, "error": "二级任务与所选大类不一致，请重新选择。"}
        name = row["name"]
    if not name:
        return {"ok": False, "error": "请先选择或新建一个二级任务，再开始计时。"}
    task_id, _ = db.ensure_task(conn, cat["id"], name)

    now = _now()
    cur = conn.execute(
        "INSERT INTO timers(category_id, task_id, task_name, tags, status, seg_started_at, "
        " elapsed_sec, created_at, note) VALUES (?,?,?,?, 'running', ?, 0, ?, ?)",
        (cat["id"], task_id, name, "\n".join(tag_list), util.iso(now), util.iso(now), note or ""))
    _register_tags(conn, tag_list)
    # 记住"上一次的组合"，供快速开始一键复用（规格 4.1 第 2 条）
    db.set_setting(conn, "last_combo", "%d\t%d\t%s\t%s" % (
        cat["id"], task_id, name, ",".join(tag_list)))
    return {"ok": True, "timer_id": cur.lastrowid, "timers": list_timers(conn)}


def pause(conn, tid, now=None):
    now = now or _now()
    t = _timer_row(conn, tid)
    if t is None:
        return {"ok": False, "error": "这条计时已经不存在了（可能已在别处停止），请刷新页面。"}
    if t["status"] == "paused":
        return {"ok": True, "timers": list_timers(conn, now)}
    elapsed, seg_start, _ = _accumulate(t, now)
    conn.execute("UPDATE timers SET status='paused', elapsed_sec=?, seg_started_at=? WHERE id=?",
                 (elapsed, util.iso(seg_start), tid))
    return {"ok": True, "timers": list_timers(conn, now)}


def resume(conn, tid, now=None):
    now = now or _now()
    t = _timer_row(conn, tid)
    if t is None:
        return {"ok": False, "error": "这条计时已经不存在了，请刷新页面。"}
    if t["status"] == "running":
        return {"ok": True, "timers": list_timers(conn, now)}
    # elapsed_sec 保留（上次运行+上次暂停的累计），seg_started_at 设为现在 -> 显示重新开始数
    conn.execute("UPDATE timers SET status='running', seg_started_at=? WHERE id=?",
                 (util.iso(now), tid))
    return {"ok": True, "timers": list_timers(conn, now)}


def _effective_start(t, elapsed):
    """这条计时"实际开始计入时长的那个时刻" = seg_started_at - elapsed_sec。

    运行中时它就等于 seg_started_at（当前这一段的起点）；
    暂停中时它是往回推出来的起点。
    """
    return util.parse_iso(t["seg_started_at"]) - datetime.timedelta(seconds=int(t["elapsed_sec"]))


def _finalize(conn, t, now, note_extra=""):
    """把一条计时停止并落库（规格 4.1 第 3 条：不弹窗追问，直接保存）。"""
    elapsed, seg_start, status = _accumulate(t, now)
    if status == "running":
        end_ts = now
    else:
        end_ts = util.parse_iso(t["seg_started_at"])   # 暂停时刻就是终点
    start_all = _effective_start(t, elapsed)
    minutes = int(round(elapsed / 60.0))
    rid = None
    if minutes > 0:
        rid = db.insert_record(
            conn, start_all.date().isoformat(), t["category_id"], t["task_id"],
            t["task_name"], t["tags"], minutes, "timer",
            start_ts=util.iso(start_all), end_ts=util.iso(end_ts),
            note=(t["note"] or "") + note_extra)
        _undo(conn, "record_create",
              "计时落库：%s %s" % (t["task_name"], util.fmt_hm(minutes)),
              {"record_id": rid})
    conn.execute("DELETE FROM timers WHERE id=?", (t["id"],))
    return {"record_id": rid, "minutes": minutes,
            "start_ts": util.iso(start_all), "end_ts": util.iso(end_ts)}


def stop(conn, tid, now=None):
    now = now or _now()
    t = _timer_row(conn, tid)
    if t is None:
        return {"ok": False, "error": "这条计时已经不存在了，请刷新页面。"}
    info = _finalize(conn, t, now)
    return {"ok": True, "saved": info, "timers": list_timers(conn, now),
            "message": ("已保存：%s %s" % (t["task_name"], util.fmt_hm(info["minutes"]))
                        if info["minutes"] > 0 else
                        "这段计时不足 1 分钟，没有生成记录。")}


def cancel(conn, tid):
    """放弃这段计时（不落库）。用于"忘记停止"里选「删除这段」。"""
    t = _timer_row(conn, tid)
    if t is None:
        return {"ok": False, "error": "这条计时已经不存在了，请刷新页面。"}
    conn.execute("DELETE FROM timers WHERE id=?", (tid,))
    return {"ok": True, "timers": list_timers(conn), "message": "已放弃这段计时，未生成记录。"}


def list_timers(conn, now=None):
    """页面顶部"正在计时"条用的数据：任务名、已计时间、状态、并行多条。

    注意：正在计时显示到秒（H:MM:SS）——这是**临时显示**，让用户看得见时间在走；
    落库时一律四舍五入成整数分钟，界面上的历史时长仍统一是 H:MM（规格 3.4）。
    """
    now = now or _now()
    out = []
    sql = ("SELECT t.*, c.name AS category_name, c.counts_work FROM timers t "
           "JOIN categories c ON c.id=t.category_id ORDER BY t.id")
    for r in conn.execute(sql):
        t = dict(r)
        elapsed, seg_start, status = _accumulate(t, now)
        h, rem = divmod(int(elapsed), 3600)
        out.append({
            "id": t["id"], "category_id": t["category_id"], "task_id": t["task_id"],
            "task_name": t["task_name"], "category_name": t["category_name"],
            "tags": _parse_tags(t["tags"]), "status": status,
            "elapsed_sec": elapsed,
            "elapsed_clock": "%d:%02d:%02d" % (h, rem // 60, rem % 60),
            "elapsed_minutes": int(round(elapsed / 60.0)),
            "elapsed_hm": util.fmt_hm(int(round(elapsed / 60.0))),
            "started_at": t["created_at"], "seg_started_at": t["seg_started_at"],
            "cross_midnight": t["cross_midnight"], "note": t["note"],
        })
    return out


# ================================================================ 忘记停止

def overtime_pending(conn, now=None):
    """列出超过阈值还在跑的计时段（规格 4.1 第 6 条、验收第 7 条）。

    阈值默认 4:00，可在设置里改（timer_overtime_min）。
    """
    now = now or _now()
    limit_min = db.get_setting_int(conn, "timer_overtime_min", 240)
    out = []
    sql = ("SELECT t.*, c.name AS category_name FROM timers t "
           "JOIN categories c ON c.id=t.category_id ORDER BY t.id")
    for r in conn.execute(sql):
        t = dict(r)
        elapsed, seg_start, status = _accumulate(t, now)
        if status != "running":
            continue
        minutes = int(round(elapsed / 60.0))
        if minutes > limit_min:
            run_start = util.parse_iso(t["seg_started_at"])
            out.append({
                "timer_id": t["id"], "task_name": t["task_name"],
                "category_name": t["category_name"],
                "elapsed_minutes": minutes, "elapsed_hm": util.fmt_hm(minutes),
                "limit_hm": util.fmt_hm(limit_min),
                "started_at": t["created_at"], "seg_started_at": t["seg_started_at"],
                "suggest_end": util.iso(run_start + datetime.timedelta(minutes=limit_min)),
            })
    return out


def resolve_overtime(conn, tid, action, end_ts=None):
    """处理超时计时：keep=保留到实际时长，truncate=截断到某个时间点，delete=删除这段。"""
    now = _now()
    t = _timer_row(conn, tid)
    if t is None:
        return {"ok": False, "error": "这条计时已经不存在了，请刷新页面。"}
    if action == "delete":
        return cancel(conn, tid)
    if action == "keep":
        info = _finalize(conn, t, now)
        return {"ok": True, "saved": info, "timers": list_timers(conn),
                "message": "已按实际时长保留：%s %s" % (t["task_name"], util.fmt_hm(info["minutes"]))}
    if action == "truncate":
        try:
            cut = util.parse_iso(end_ts)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if cut is None:
            return {"ok": False, "error": "请填写截断到的时间点，例如 2025-08-21 11:00。"}
        start_all = _effective_start(t, _accumulate(t, now)[0])
        if cut <= start_all:
            return {"ok": False, "error": "截断时间必须晚于这段计时的开始时间（%s）。"
                                          % util.iso(start_all)}
        if cut > now:
            return {"ok": False, "error": "截断时间不能晚于现在。"}
        minutes = int(round((cut - start_all).total_seconds() / 60.0))
        rid = None
        if minutes > 0:
            rid = db.insert_record(
                conn, start_all.date().isoformat(), t["category_id"], t["task_id"],
                t["task_name"], t["tags"], minutes, "timer",
                start_ts=util.iso(start_all), end_ts=util.iso(cut), note=t["note"] or "")
            _undo(conn, "record_create", "超时截断落库：%s %s" % (t["task_name"], util.fmt_hm(minutes)),
                  {"record_id": rid})
        conn.execute("DELETE FROM timers WHERE id=?", (tid,))
        return {"ok": True, "timers": list_timers(conn),
                "message": "已截断到 %s，保存 %s。" % (util.iso(cut), util.fmt_hm(minutes))}
    return {"ok": False, "error": "看不懂这个处理方式：%r" % (action,)}


# ================================================================ 事后编辑起止时间

def edit_record_times(conn, rid, start_ts, end_ts):
    """修改记录的时间段，自动重算时长（规格 4.1 第 7 条）。

    计时产生的记录一定有起止时间；补录时填了时间段的记录也有，同样可以改。
    没有时间段的记录（只填了时长的补录）不能事后补时间段 —— 那会改变它的统计口径，
    要加时间段请删掉重录。
    """
    rec = db.record_by_id(conn, rid)
    if rec is None:
        return {"ok": False, "error": "找不到这条记录（可能已被删除）。"}
    if not (rec["start_ts"] and rec["end_ts"]):
        return {"ok": False, "error": "这条记录没有时间段（补录时只填了时长），不能事后加时间段；"
                                      "需要时间段的话，请删掉这条、重新补录并在补录时填写"
                                      "起始时间和终止时间。"}
    try:
        s, e = util.parse_iso(start_ts), util.parse_iso(end_ts)
    except ValueError as err:
        return {"ok": False, "error": str(err)}
    if s is None or e is None:
        return {"ok": False, "error": "请把开始时间和结束时间都填上。"}
    if e <= s:
        return {"ok": False, "error": "结束时间必须晚于开始时间（当前相差 %d 分钟）。"
                                      % int((e - s).total_seconds() // 60)}
    minutes = int(round((e - s).total_seconds() / 60.0))
    if minutes <= 0:
        return {"ok": False, "error": "改完之后时长不足 1 分钟，请调整时间。"}
    old = {"start_ts": rec["start_ts"], "end_ts": rec["end_ts"],
           "duration_min": rec["duration_min"], "day": rec["day"]}
    conn.execute("UPDATE records SET start_ts=?, end_ts=?, duration_min=?, day=?, "
                 "updated_at=? WHERE id=?",
                 (util.iso(s), util.iso(e), minutes, s.date().isoformat(),
                  util.iso(util.now_local()), rid))
    _undo(conn, "record_time_edit",
          "改计时起止：%s -> %s（%s）" % (rec["task_name"], util.iso(s) + " ~" + util.iso(e),
                                       util.fmt_hm(minutes)),
          {"record_id": rid, "before": old})
    return {"ok": True, "record": db.record_by_id(conn, rid),
            "message": "已改为 %s，时长重算为 %s。" % (util.iso(s) + " ~ " + util.iso(e),
                                                    util.fmt_hm(minutes))}


# ================================================================ 事后补录

def add_manual(conn, day, category_id, task_id=None, task_name=None, tags=None,
               duration=None, note="", products=None, start_time=None, end_time=None):
    """事后补录一条记录（规格 4.2）。

    必填：日期 + 大类 + 二级任务 + 标签；并且**时长与时间段至少要填一种**：
      * 只填时长          -> 记成无区间的记录，直接相加（原来就有的用法）
      * 只填时间段        -> 时长由"终止 - 起始"算出来
      * 两者都填          -> 会核对是否一致；一致就按时间段存，不一致直接报错、不保存

    时间段规则（用户 2026-10-12 确认）：
      * 起始、终止必须成对填写；
      * 都必须在所选日期的 00:00~23:59 之内（不能跨天）；
      * 填了时间段的记录带起了止时间，会和其它带时间的记录一起参与"时间轴去重"。
    """
    if not day:
        return {"ok": False, "error": "请选择日期。"}
    try:
        d = util.parse_day(day)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if d > util.now_local().date():
        return {"ok": False, "error": "不能补录未来的日期（%s）。" % d.isoformat()}
    if not category_id:
        return {"ok": False, "error": "请选择大类。"}
    cat = db.category_by_id(conn, category_id)
    if cat is None:
        return {"ok": False, "error": "找不到这个大类，请刷新页面后重试。"}
    if cat["archived"]:
        return {"ok": False, "error": "「%s」已归档，不能再用于新记录。" % cat["name"]}

    name = (task_name or "").strip()
    if task_id:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return {"ok": False, "error": "找不到这个二级任务，请刷新页面后重试。"}
        if row["category_id"] != cat["id"]:
            return {"ok": False, "error": "二级任务与所选大类不一致，请重新选择。"}
        name = row["name"]
    if not name:
        return {"ok": False, "error": "请选择或填写二级任务。"}

    tag_list = _parse_tags(tags)
    if not tag_list:
        return {"ok": False, "error": "标签至少要填一个（历史导入的数据才会豁免）。"}

    # ---------------- 时间段（起始 / 终止） ----------------
    has_start = start_time not in (None, "")
    has_end = end_time not in (None, "")
    if has_start != has_end:
        return {"ok": False, "error": "起始时间和终止时间要成对填：只填一个的话，"
                                     "另一个也要填上（或者两个都留空、只填时长）。"}
    interval_min = None
    start_ts = end_ts = None
    if has_start and has_end:
        try:
            s_sec = util.seconds_of_day(start_time)
            e_sec = util.seconds_of_day(end_time)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if e_sec <= s_sec:
            return {"ok": False, "error": "终止时间必须晚于起始时间"
                                         "（当前 %s → %s，相差 %d 分钟）。"
                                          % (util.fmt_clock_of_day(s_sec),
                                             util.fmt_clock_of_day(e_sec),
                                             (e_sec - s_sec) // 60)}
        interval_min = int(round((e_sec - s_sec) / 60.0))
        if interval_min <= 0:
            return {"ok": False, "error": "这个时间段的长度不足 1 分钟，请调整时间。"}
        # 时间必须落在所选日期的当天（0:00 ~ 23:59:59）
        start_dt = datetime.datetime.combine(d, datetime.time(0, 0, 0)) + \
            datetime.timedelta(seconds=s_sec)
        end_dt = datetime.datetime.combine(d, datetime.time(0, 0, 0)) + \
            datetime.timedelta(seconds=e_sec)
        if end_dt.date() != d:
            return {"ok": False, "error": "时间段必须在所选日期（%s）当天 00:00~23:59 之内，"
                                         "不能跨天。" % d.isoformat()}
        if end_dt > util.now_local():
            return {"ok": False, "error": "终止时间不能晚于现在（%s）。"
                                          % util.iso(util.now_local())[11:16]}
        start_ts, end_ts = util.iso(start_dt), util.iso(end_dt)

    # ---------------- 时长 ----------------
    minutes = None
    raw_dur = "" if duration in (None, "") else str(duration).strip()
    if raw_dur:
        if isinstance(duration, int):
            minutes, err = duration, None
        else:
            minutes, err = util.parse_duration(duration)
        if err:
            return {"ok": False, "error": err}
        if minutes is None or minutes <= 0:
            return {"ok": False, "error": "时长必须大于 0。"}

    if interval_min is None and minutes is None:
        return {"ok": False, "error": "时长和时间段至少要填一种：要么填时长，"
                                     "要么把起始时间和终止时间都填上。"}

    # 两者都有 -> 必须一致，不一致直接不保存（用户 2026-10-12 确认）
    if interval_min is not None and minutes is not None and minutes != interval_min:
        return {"ok": False, "error": "时长与时间段对不上：时间段 %s~%s 是 %s，"
                                     "但时长填的是 %s。请改成一致再保存。"
                                      % (util.fmt_clock_of_day(s_sec),
                                         util.fmt_clock_of_day(e_sec),
                                         util.fmt_hm(interval_min), util.fmt_hm(minutes))}

    stored_min = interval_min if interval_min is not None else minutes
    note_suffix = ("（按时间段 %s~%s 记录）"
                   % (util.fmt_clock_of_day(s_sec), util.fmt_clock_of_day(e_sec))
                   if interval_min is not None and not raw_dur else "")

    task_id, _ = db.ensure_task(conn, cat["id"], name)
    _register_tags(conn, tag_list)
    clean_products = []
    for p in (products or []):
        nm = (p.get("name") or "").strip()
        if not nm:
            continue
        pt = p.get("ptype") or "其他"
        if pt not in util.PRODUCT_TYPES:
            pt = "其他"
        clean_products.append({"ptype": pt, "name": nm, "url": (p.get("url") or "").strip()})
    rid = db.insert_record(conn, d.isoformat(), cat["id"], task_id, name, tag_list,
                           stored_min, "manual",
                           start_ts=start_ts, end_ts=end_ts,
                           note=(note or "") + note_suffix, products=clean_products)
    db.set_setting(conn, "last_combo", "%d\t%d\t%s\t%s" % (
        cat["id"], task_id, name, ",".join(tag_list)))
    _undo(conn, "record_create",
          "补录：%s %s %s" % (d.isoformat(), name, util.fmt_hm(stored_min)), {"record_id": rid})
    extra = ("（时间段 %s~%s）" % (util.fmt_clock_of_day(s_sec), util.fmt_clock_of_day(e_sec))
             if interval_min is not None else "")
    return {"ok": True, "record_id": rid, "record": db.record_by_id(conn, rid),
            "message": "已补录：%s %s %s%s" % (d.isoformat(), name,
                                             util.fmt_hm(stored_min), extra)}


# ================================================================ 快速开始

def last_combo(conn):
    """上一次用过的大类/任务/标签组合（规格 4.1 第 2 条「快速开始」）。

    只有"这个任务现在确实存在、大类也没归档"时才返回，避免快速开始指向已被删除/合并的任务。
    """
    raw = db.get_setting(conn, "last_combo", "")
    if not raw:
        return None
    parts = raw.split("\t")
    if len(parts) < 4:
        return None
    try:
        cid, tid = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    cat = db.category_by_id(conn, cid)
    if cat is None or cat["archived"]:
        return None
    task = conn.execute("SELECT * FROM tasks WHERE id=? AND archived=0", (tid,)).fetchone()
    if task is None or task["category_id"] != cid:
        return None
    return {"category_id": cid, "category_name": cat["name"], "task_id": tid,
            "task_name": task["name"], "tags": [x for x in parts[3].split(",") if x]}
