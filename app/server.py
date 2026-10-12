# -*- coding: utf-8 -*-
"""本地 HTTP 服务（标准库 http.server）+ JSON API。

* 只绑定 127.0.0.1，绝不监听局域网/外网。
* 端口从 8090 开始找，被占用就 +1 重试；**永不使用 3080**（用户明确要求）。
* 前端是原生 HTML/CSS/JS，全部本地自带，不引用任何 CDN。
"""

import os
import io
import re
import json
import time
import socket
import threading
import traceback
import datetime
import mimetypes
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 既支持 `python -m app.server`（包内相对导入），
# 也支持 `python app\server.py`（启动器用的方式），两种都要能用。
if __package__ in (None, ""):
    sys_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    from app import (db, util, timers, stats, goals, maintain,
                     reports as reports_mod, xlsx)
else:
    from . import (db, util, timers, stats, goals, maintain,
                   reports as reports_mod, xlsx)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")

PORTS_TO_TRY = list(range(8090, 8200))       # 8090 起；3080 不在这个范围里，绝不会被占用
FORBIDDEN_PORTS = {3080}
PERIODIC_SPLIT_SECONDS = 20                  # 服务端定期检查跨午夜切分

_conn_lock = threading.RLock()
_local = threading.local()


def get_conn():
    """取当前线程的数据库连接。

    SQLite 连接默认不能跨线程使用（会报 "SQLite objects created in a thread..."），
    所以每个线程各拿一条连接（WAL 模式下多连接读写是安全的）；
    写操作再用 _conn_lock 串行化，避免 "database is locked"。
    """
    c = getattr(_local, "conn", None)
    if c is None:
        c = db.init_db()
        _local.conn = c
    return c


class App:
    """把业务函数收在一处，路由表用字符串指向方法名。"""

    # ---------------------------------------------------------- 汇总/首页

    def api_bootstrap(self, q):
        conn = get_conn()
        today = util.today_str()
        split_made = timers.split_crossing_midnight(conn)
        auto = reports_mod.auto_generate(conn)
        day = stats.day_detail(conn, today)
        dd = goals.day_goal_status(conn, today, day["category_minutes"])
        prog = goals.progress(conn, today)
        st = goals.streak_days(conn)
        recall_days = db.get_setting_int(conn, "blank_recall_days", 7)
        start = (util.parse_day(today) - datetime.timedelta(days=recall_days)).isoformat()
        blanks = stats.blank_days(conn, start, today)
        yesterday = (util.parse_day(today) - datetime.timedelta(days=1)).isoformat()
        blanks_past = [d for d in blanks if d != today]
        return {
            "ok": True,
            "today": today,
            "weekday": util.weekday_cn(today),
            "now": util.iso(util.now_local()),
            "day": day,
            "day_hm": util.fmt_hm(day["actual_minutes"]),
            "work_hm": util.fmt_hm(day["work_minutes"]),
            "invested_hm": util.fmt_hm(day["invested_minutes"]),
            "timers": timers.list_timers(conn),
            "overtime": timers.overtime_pending(conn),
            "cross_midnight_split": split_made,
            "categories": db.all_categories(conn),
            "tasks": db.all_tasks(conn),
            "tags": db.all_tags(conn),
            "last_combo": timers.last_combo(conn),
            "goals_set": goals.has_any_goal(conn),
            "day_goal": dd,
            "progress": prog,
            "streak": st,
            "blank_recall": {"start": start, "days": blanks_past,
                             "today_blank": today in blanks, "window": recall_days},
            "reports_generated": auto.get("generated", []),
            "reports_message": auto.get("message", ""),
            "settings": {k: db.get_setting(conn, k) for k in (
                "timer_overtime_min", "blank_recall_days", "top_n", "backup_keep",
                "undo_steps", "saturday_ratio", "theme", "compact", "first_run_done")},
            "product_types": util.PRODUCT_TYPES,
            "encourage": "做好细末是很棒的！",
            "data_bounds": stats.data_bounds(conn),
            "db_path": db.DB_PATH,
        }

    def api_today(self, q):
        conn = get_conn()
        day = q.get("day", [util.today_str()])[0]
        return {"ok": True, "day": stats.day_detail(conn, day)}

    def api_day(self, q):
        conn = get_conn()
        day = q.get("day", [util.today_str()])[0]
        d = stats.day_detail(conn, day)
        d["goal"] = goals.day_goal_status(conn, day, d["category_minutes"])
        d["date_cn"] = "%s %s" % (day, util.weekday_cn(day))
        return {"ok": True, "day": d}

    def api_range(self, q):
        conn = get_conn()
        kind = q.get("kind", ["week"])[0]
        day = q.get("day", [util.today_str()])[0]
        if kind == "week":
            d = stats.week_detail(conn, day, goals.range_goal_summary)
            d["top_tasks"] = stats.top_tasks(conn, d["start"], d["end"],
                                            db.get_setting_int(conn, "top_n", 5))
        elif kind == "month":
            d = stats.month_detail(conn, day, goals.range_goal_summary)
            d["top_tasks"] = stats.top_tasks(conn, d["start"], d["end"],
                                            db.get_setting_int(conn, "top_n", 5))
        else:
            s = q.get("start", [day])[0]
            e = q.get("end", [day])[0]
            d = stats.range_with_goals(conn, s, e)
        d["series"] = stats.daily_series(conn, d["start"], d["end"])
        d["plan_vs_actual"] = goals.plan_vs_actual(conn, d["start"], d["end"])
        return {"ok": True, "range": d}

    def api_calendar(self, q):
        """按日列表：含"未记录"标记（规格 5.3 / 7.2）。"""
        conn = get_conn()
        start = q.get("start", [util.today_str()])[0]
        end = q.get("end", [util.today_str()])[0]
        d = stats.range_detail(conn, start, end)
        out = []
        for pd in reversed(d["days"]):
            st = goals.day_goal_status(conn, pd["day"], pd["category_minutes"])
            out.append(dict(pd, goal=st))
        return {"ok": True, "days": out, "start": d["start"], "end": d["end"],
                "blank_days": [x["day"] for x in out if not x["has_records"]]}

    def api_records(self, q):
        conn = get_conn()
        start = q.get("start", ["1970-01-01"])[0]
        end = q.get("end", ["2999-12-31"])[0]
        include_deleted = q.get("deleted", ["0"])[0] == "1"
        sql = ("SELECT r.*, c.name AS category_name FROM records r "
               "JOIN categories c ON c.id=r.category_id "
               "WHERE r.day BETWEEN ? AND ?")
        if not include_deleted:
            sql += " AND r.deleted_at IS NULL"
        sql += " ORDER BY r.day DESC, r.id DESC LIMIT 3000"
        rows = [dict(r) for r in conn.execute(sql, (start, end))]
        for r in rows:
            r["hm"] = util.fmt_hm(r["duration_min"])
            r["weekday"] = util.weekday_cn(r["day"])
            r["tag_list"] = [x for x in (r["tags"] or "").split("\n") if x]
            r["products"] = [dict(p) for p in conn.execute(
                "SELECT * FROM record_products WHERE record_id=? ORDER BY sort_order, id",
                (r["id"],))]
        return {"ok": True, "records": rows}

    # ---------------------------------------------------------- 计时

    def api_timer_start(self, q, body):
        conn = get_conn()
        with _conn_lock:
            r = timers.start(conn, body.get("category_id"), body.get("task_id"),
                             body.get("task_name"), body.get("tags"), body.get("note", ""))
        return r

    def api_timer_pause(self, q, body):
        conn = get_conn()
        return timers.pause(conn, body.get("timer_id"))

    def api_timer_resume(self, q, body):
        conn = get_conn()
        return timers.resume(conn, body.get("timer_id"))

    def api_timer_stop(self, q, body):
        conn = get_conn()
        r = timers.stop(conn, body.get("timer_id"))
        if r.get("ok"):
            maintain.prune_undo(conn)
        return r

    def api_timer_cancel(self, q, body):
        conn = get_conn()
        return timers.cancel(conn, body.get("timer_id"))

    def api_timer_status(self, q):
        conn = get_conn()
        timers.split_crossing_midnight(conn)
        return {"ok": True, "timers": timers.list_timers(conn),
                "overtime": timers.overtime_pending(conn)}

    def api_timer_overtime_resolve(self, q, body):
        conn = get_conn()
        r = timers.resolve_overtime(conn, body.get("timer_id"), body.get("action"),
                                    body.get("end_ts"))
        if r.get("ok"):
            maintain.prune_undo(conn)
        return r

    # ---------------------------------------------------------- 记录

    def api_record_add(self, q, body):
        conn = get_conn()
        r = timers.add_manual(conn, body.get("day"), body.get("category_id"),
                              body.get("task_id"), body.get("task_name"),
                              body.get("tags"), body.get("duration"),
                              body.get("note", ""), body.get("products"),
                              start_time=body.get("start_time"),
                              end_time=body.get("end_time"))
        if r.get("ok"):
            maintain.prune_undo(conn)
        return r

    def api_record_update(self, q, body):
        conn = get_conn()
        rid = body.pop("id", None)
        if not rid:            return {"ok": False, "error": "缺少要修改的记录 id。"}
        r = maintain.update_record(conn, rid, **body)
        if r.get("ok"):
            maintain.prune_undo(conn)
        return r

    def api_record_times(self, q, body):
        conn = get_conn()
        r = timers.edit_record_times(conn, body.get("id"), body.get("start_ts"),
                                     body.get("end_ts"))
        if r.get("ok"):
            maintain.prune_undo(conn)
        return r

    def api_record_delete(self, q, body):
        conn = get_conn()
        r = maintain.delete_record(conn, body.get("id"))
        if r.get("ok"):
            maintain.prune_undo(conn)
        return r

    def api_record_restore(self, q, body):
        conn = get_conn()
        return maintain.restore_record(conn, body.get("id"))

    def api_record_products(self, q, body):
        """给一条记录追加/替换产出物。"""
        conn = get_conn()
        rid = body.get("id")
        rec = db.record_by_id(conn, rid)
        if rec is None:
            return {"ok": False, "error": "找不到这条记录。"}
        conn.execute("DELETE FROM record_products WHERE record_id=?", (rid,))
        for i, p in enumerate(body.get("products") or []):
            nm = (p.get("name") or "").strip()
            if not nm:
                continue
            pt = p.get("ptype") if p.get("ptype") in util.PRODUCT_TYPES else "其他"
            conn.execute("INSERT INTO record_products(record_id, ptype, name, url, sort_order) "
                         "VALUES (?,?,?,?,?)", (rid, pt, nm, (p.get("url") or "").strip(), i))
        return {"ok": True, "record": db.record_by_id(conn, rid), "message": "产出物已保存。"}

    def api_deleted(self, q):
        conn = get_conn()
        rows = maintain.list_deleted(conn)
        for r in rows:
            r["hm"] = util.fmt_hm(r["duration_min"])
        return {"ok": True, "records": rows}

    # ---------------------------------------------------------- 大类/任务/标签

    def api_category_save(self, q, body):
        conn = get_conn()
        cid = body.get("id")
        if not cid:
            return maintain.add_category(conn, body.get("name"), body.get("counts_work"))
        return maintain.update_category(conn, cid, body.get("name"), body.get("counts_work"),
                                        body.get("archived"), body.get("sort_order"))

    def api_category_delete(self, q, body):
        conn = get_conn()
        return maintain.delete_category(conn, body.get("id"))

    def api_task_rename(self, q, body):
        conn = get_conn()
        return maintain.rename_task(conn, body.get("id"), body.get("name"))

    def api_task_merge(self, q, body):
        conn = get_conn()
        r = maintain.merge_tasks(conn, body.get("from_id"), body.get("to_id"))
        if r.get("ok"):
            maintain.prune_undo(conn)
        return r

    def api_task_archive(self, q, body):
        conn = get_conn()
        return maintain.archive_task(conn, body.get("id"), bool(body.get("archived", True)))

    def api_task_add(self, q, body):
        conn = get_conn()
        cat = db.category_by_id(conn, body.get("category_id"))
        if cat is None:
            return {"ok": False, "error": "找不到这个大类。"}
        name = (body.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "任务名不能为空。"}
        tid, created = db.ensure_task(conn, cat["id"], name)
        return {"ok": True, "task_id": tid,
                "message": ("已新建任务「%s」。" % name) if created else "这个任务已经存在。"}

    def api_tag_rename(self, q, body):
        conn = get_conn()
        return maintain.rename_tag(conn, body.get("old"), body.get("new"))

    def api_tag_merge(self, q, body):
        conn = get_conn()
        r = maintain.merge_tags(conn, body.get("old"), body.get("new"))
        if r.get("ok"):
            maintain.prune_undo(conn)
        return r

    def api_tags(self, q):
        conn = get_conn()
        return {"ok": True, "tags": db.all_tags(conn)}

    # ---------------------------------------------------------- 目标

    def api_goals(self, q):
        conn = get_conn()
        return {"ok": True, "goals": goals.list_goals(conn),
                "has_goal": goals.has_any_goal(conn),
                "saturday_ratio": db.get_setting_float(conn, "saturday_ratio", 0.5),
                "streak": goals.streak_days(conn),
                "progress": goals.progress(conn)}

    def api_goal_set(self, q, body):
        conn = get_conn()
        return goals.set_goal(conn, body.get("category_id"), body.get("goal_type"),
                              body.get("minutes"))

    # ---------------------------------------------------------- 计划

    def api_plans(self, q):
        conn = get_conn()
        start = q.get("start", [util.today_str()])[0]
        end = q.get("end", [util.today_str()])[0]
        return {"ok": True, "plans": goals.list_plans(conn, start, end),
                "compare": goals.plan_vs_actual(conn, start, end)}

    def api_plan_add(self, q, body):
        conn = get_conn()
        return goals.add_plan(conn, body.get("day"), body.get("category_id"),
                              body.get("task_id"), body.get("task_name"),
                              body.get("planned"), body.get("note", ""))

    def api_plan_week(self, q, body):
        conn = get_conn()
        return goals.plan_week(conn, body.get("day"), body.get("items") or [])

    def api_plan_delete(self, q, body):
        conn = get_conn()
        return goals.delete_plan(conn, body.get("id"))

    # ---------------------------------------------------------- 总结

    def api_report_generate(self, q, body):
        conn = get_conn()
        kind = body.get("kind", "custom")
        day = body.get("day") or util.today_str()
        if kind == "week":
            mon, sun = util.week_range(day)
            s, e = mon.isoformat(), sun.isoformat()
        elif kind == "month":
            f, l = util.month_range(day)
            s, e = f.isoformat(), l.isoformat()
        elif kind == "last_week":
            mon, sun = util.week_range(day)
            s = (mon - datetime.timedelta(days=7)).isoformat()
            e = (sun - datetime.timedelta(days=7)).isoformat()
            kind = "custom"
        elif kind == "last_month":
            f, l = util.month_range(day)
            pf, pl = util.month_range(f - datetime.timedelta(days=1))
            s, e = pf.isoformat(), pl.isoformat()
            kind = "custom"
        else:
            s = body.get("start") or day
            e = body.get("end") or s
            kind = "custom"
        rep = reports_mod.build_report(conn, s, e,
                                      kind if kind in ("week", "month") else "custom")
        if body.get("save"):
            saved = reports_mod.save_report(conn, rep, force=bool(body.get("force")))
            # 只回传文件信息，不要把整份报表再塞回去（会造成循环引用，JSON 序列化会失败）
            rep["saved"] = {k: v for k, v in saved.items() if k != "report"}
        return {"ok": True, "report": rep}

    def api_reports(self, q):
        conn = get_conn()
        return {"ok": True, "reports": reports_mod.list_reports(conn)}

    def api_report_read(self, q):
        conn = get_conn()
        rid = q.get("id", [None])[0]
        fname = q.get("file", [None])[0]
        return reports_mod.read_report(conn, int(rid) if rid else None, fname)

    def api_report_auto(self, q):
        conn = get_conn()
        return reports_mod.auto_generate(conn)

    # ---------------------------------------------------------- 设置/导入/导出/备份/撤销

    def api_settings_save(self, q, body):
        conn = get_conn()
        allowed = {"timer_overtime_min", "blank_recall_days", "top_n", "backup_keep",
                   "undo_steps", "saturday_ratio", "theme", "compact"}
        saved = {}
        for k, v in (body or {}).items():
            if k not in allowed:
                continue
            if k == "saturday_ratio":
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    return {"ok": False, "error": "周六折算比例要填数字，比如 0.5。"}
                if not (0 <= f <= 1):
                    return {"ok": False, "error": "周六折算比例要在 0~1 之间。"}
            if k in ("timer_overtime_min", "blank_recall_days", "top_n", "backup_keep",
                     "undo_steps"):
                try:
                    iv = int(float(v))
                except (TypeError, ValueError):
                    return {"ok": False, "error": "「%s」要填整数。" % k}
                if iv <= 0:
                    return {"ok": False, "error": "「%s」必须大于 0。" % k}
                if k == "timer_overtime_min" and iv > 24 * 60:
                    return {"ok": False, "error": "超时阈值不能超过 24 小时。"}
            db.set_setting(conn, k, v)
            saved[k] = v
        return {"ok": True, "saved": saved, "message": "设置已保存。"}

    def api_export(self, q, body):
        conn = get_conn()
        r = xlsx.export_excel(conn, body.get("start"), body.get("end"),
                              body.get("exclude") or [],
                              body.get("note", ""))
        return r

    def api_exports(self, q):
        return {"ok": True, "files": xlsx.list_exports()}

    def api_backup_now(self, q, body):
        conn = get_conn()
        r = maintain.daily_backup(conn, force=True, note=body.get("note", "手动备份"))
        r["list"] = maintain.list_backups(conn)
        return r

    def api_backups(self, q):
        conn = get_conn()
        return {"ok": True, "backups": maintain.list_backups(conn),
                "dir": db.BACKUP_DIR}

    def api_backup_restore(self, q, body):
        path = os.path.join(db.BACKUP_DIR, os.path.basename(body.get("file") or ""))
        return maintain.restore_backup(path, body.get("confirm"))

    def api_undo(self, q, body):
        conn = get_conn()
        if body.get("history"):
            return {"ok": True, "history": maintain.undo_history(conn)}
        return maintain.undo_last(conn)

    def api_undo_history(self, q):
        conn = get_conn()
        return {"ok": True, "history": maintain.undo_history(conn),
                "steps": db.get_setting_int(conn, "undo_steps", 20)}

    def api_open_folder(self, q, body):
        """在文件管理器里打开某个目录（只在用户点按钮时触发）。"""
        which = body.get("which", "data")
        mapping = {"data": db.DATA_DIR, "backup": db.BACKUP_DIR, "reports": db.REPORTS_DIR,
                   "export": db.EXPORT_DIR, "import": db.IMPORT_DIR, "root": db.ROOT_DIR}
        path = mapping.get(which)
        if not path or not os.path.isdir(path):
            return {"ok": False, "error": "找不到目录：%s" % path}
        try:
            os.startfile(path)           # noqa: S606  Windows 专用，打开资源管理器
            return {"ok": True, "message": "已在文件管理器里打开：%s" % path}
        except OSError as e:
            return {"ok": False, "error": "打不开目录：%s。你可以手工进入：%s" % (e, path)}

    def api_monthly(self, q):
        """月度聚合（原「验收基线比对」里用到的那份统计，去掉基线本身）。"""
        conn = get_conn()
        return {"ok": True, "monthly": stats.monthly_stats(conn)}

    def api_status(self, q):
        conn = get_conn()
        return {"ok": True, "db": db.DB_PATH, "records": db.count_records(conn),
                "bounds": stats.data_bounds(conn), "now": util.iso(util.now_local()),
                "python": ".".join(str(x) for x in __import__("sys").version_info[:3])}


APP = App()


# ================================================================ 路由表

ROUTES = {
    ("GET", "/api/bootstrap"): "api_bootstrap",
    ("GET", "/api/today"): "api_today",
    ("GET", "/api/day"): "api_day",
    ("GET", "/api/range"): "api_range",
    ("GET", "/api/calendar"): "api_calendar",
    ("GET", "/api/records"): "api_records",
    ("GET", "/api/deleted"): "api_deleted",
    ("GET", "/api/timer/status"): "api_timer_status",
    ("GET", "/api/tags"): "api_tags",
    ("GET", "/api/goals"): "api_goals",
    ("GET", "/api/plans"): "api_plans",
    ("GET", "/api/reports"): "api_reports",
    ("GET", "/api/report"): "api_report_read",
    ("GET", "/api/exports"): "api_exports",
    ("GET", "/api/backups"): "api_backups",
    ("GET", "/api/undo/history"): "api_undo_history",
    ("GET", "/api/monthly"): "api_monthly",
    ("GET", "/api/status"): "api_status",

    ("POST", "/api/timer/start"): "api_timer_start",
    ("POST", "/api/timer/pause"): "api_timer_pause",
    ("POST", "/api/timer/resume"): "api_timer_resume",
    ("POST", "/api/timer/stop"): "api_timer_stop",
    ("POST", "/api/timer/cancel"): "api_timer_cancel",
    ("POST", "/api/timer/overtime"): "api_timer_overtime_resolve",
    ("POST", "/api/record/add"): "api_record_add",
    ("POST", "/api/record/update"): "api_record_update",
    ("POST", "/api/record/times"): "api_record_times",
    ("POST", "/api/record/delete"): "api_record_delete",
    ("POST", "/api/record/restore"): "api_record_restore",
    ("POST", "/api/record/products"): "api_record_products",
    ("POST", "/api/category/save"): "api_category_save",
    ("POST", "/api/category/delete"): "api_category_delete",
    ("POST", "/api/task/rename"): "api_task_rename",
    ("POST", "/api/task/merge"): "api_task_merge",
    ("POST", "/api/task/archive"): "api_task_archive",
    ("POST", "/api/task/add"): "api_task_add",
    ("POST", "/api/tag/rename"): "api_tag_rename",
    ("POST", "/api/tag/merge"): "api_tag_merge",
    ("POST", "/api/goal/set"): "api_goal_set",
    ("POST", "/api/plan/add"): "api_plan_add",
    ("POST", "/api/plan/week"): "api_plan_week",
    ("POST", "/api/plan/delete"): "api_plan_delete",
    ("POST", "/api/report/generate"): "api_report_generate",
    ("POST", "/api/report/auto"): "api_report_auto",
    ("POST", "/api/settings/save"): "api_settings_save",
    ("POST", "/api/export"): "api_export",
    ("POST", "/api/backup"): "api_backup_now",
    ("POST", "/api/backup/restore"): "api_backup_restore",
    ("POST", "/api/undo"): "api_undo",
    ("POST", "/api/open_folder"): "api_open_folder",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "FocusWork/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("FOCUS_VERBOSE"):
            print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    # ---------------------------------------------------------- 工具

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def _serve_static(self, path):
        rel = path.lstrip("/") or "index.html"
        if not rel.startswith("web/"):
            full = os.path.join(WEB_DIR, rel)
        else:
            full = os.path.join(APP_DIR, rel)
        full = os.path.normpath(full)
        if not full.startswith(os.path.normpath(WEB_DIR)):
            return self._send(403, "禁止访问", "text/plain; charset=utf-8")
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.exists(full):
            # 单页应用：未知路径一律回主页
            full = os.path.join(WEB_DIR, "index.html")
            if not os.path.exists(full):
                return self._send(404, "前端文件缺失：app\\web\\index.html",
                                  "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",
                                                  "application/json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            data = f.read()
        self._send(200, data, ctype)

    # ---------------------------------------------------------- 方法

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            q = urllib.parse.parse_qs(parsed.query)
            fn = ROUTES.get(("GET", path))
            if fn is None:
                return self._json({"ok": False, "error": "没有这个接口：%s" % path}, 404)
            return self._dispatch(getattr(APP, fn), q, None)
        return self._serve_static(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        fn = ROUTES.get(("POST", path))
        if fn is None:
            return self._json({"ok": False, "error": "没有这个接口：%s" % path}, 404)
        body = self._read_body()
        return self._dispatch(getattr(APP, fn), q, body)

    def _dispatch(self, func, q, body):
        try:
            if body is None:
                result = func(q)
            else:
                result = func(q, body)
            if isinstance(result, dict) and "ok" not in result:
                result["ok"] = True
            self._json(result if result is not None else {"ok": True})
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass          # 浏览器提前断开（刷新/关页面），不是错误，不用管
        except Exception as e:                     # noqa: BLE001 兜底，绝不把英文堆栈丢给用户
            tb = traceback.format_exc()
            print("!! 接口出错 %s\n%s" % (self.path, tb))
            try:
                self._json({"ok": False,
                            "error": "处理这个请求时出错了：%s。数据没有被改动，"
                                     "可以刷新页面重试；如果一直这样，请把软件目录下的"
                                     "控制台输出发给开发者。" % e}, 500)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass

    def handle_one_request(self):
        """屏蔽浏览器主动断开产生的噪音堆栈。"""
        try:
            BaseHTTPRequestHandler.handle_one_request(self)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True


# ================================================================ 启动

def pick_port(preferred=8090, host="127.0.0.1"):
    """从 preferred 开始找可用端口。3080 永远跳过。"""
    ports = [preferred] + [p for p in PORTS_TO_TRY if p != preferred]
    for p in ports:
        if p in FORBIDDEN_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise RuntimeError("8090~8199 这些端口都被占用了，请先关掉一些程序再启动。")


def _periodic_worker(stop_event):
    """后台线程：定期把跨午夜的计时段切开（规格 4.1 第 5 条），并做当天备份。"""
    conn = get_conn()
    last_backup_day = None
    while not stop_event.wait(PERIODIC_SPLIT_SECONDS):
        try:
            with _conn_lock:
                made = timers.split_crossing_midnight(conn)
                if made:
                    print("跨午夜已自动切分：%s" % made)
                today = util.today_str()
                if last_backup_day != today:
                    r = maintain.daily_backup(conn)
                    if not r.get("skipped"):
                        print(r["message"])
                    last_backup_day = today
        except Exception as e:                     # noqa: BLE001
            print("后台任务出错（不影响使用）：%s" % e)


def serve(port=None, host="127.0.0.1", open_browser=True, quiet=False):
    db.ensure_dirs()
    conn = db.init_db()
    # 启动时就把该做的事做掉
    timers.split_crossing_midnight(conn)
    b = maintain.daily_backup(conn)
    if b.get("file"):
        print(b["message"])

    preferred = port or db.get_setting_int(conn, "port", 8090)
    if preferred in FORBIDDEN_PORTS:
        preferred = 8090
    actual = pick_port(preferred, host)
    httpd = ThreadingHTTPServer((host, actual), Handler)
    httpd.daemon_threads = True

    url = "http://%s:%d/" % (host, actual)
    print("=" * 62)
    print("  加油！专注工作记录  已启动")
    print("  请在浏览器里打开：%s" % url)
    if actual != preferred:
        print("  注意：%d 端口被占用了，所以改用 %d。" % (preferred, actual))
    print("  数据文件：%s" % db.DB_PATH)
    print("  关闭这个窗口即可停止程序（数据已实时保存，不会丢）。")
    print("=" * 62)

    stop_event = threading.Event()
    t = threading.Thread(target=_periodic_worker, args=(stop_event,), daemon=True)
    t.start()

    if open_browser:
        def _open():
            time.sleep(0.6)
            try:
                os.startfile(url)              # noqa: S606
            except OSError:
                pass
        threading.Thread(target=_open, daemon=True).start()

    db.set_setting(conn, "last_opened", util.iso(util.now_local()))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        stop_event.set()
        try:
            maintain.daily_backup(conn, note="退出时备份")
        except Exception:                      # noqa: BLE001
            pass
        httpd.server_close()
    return actual


if __name__ == "__main__":
    import sys
    # 被 启动.bat / launch.pyw 拉起时，端口通过环境变量传进来；没有就用设置里的默认值
    _p = os.environ.get("FOCUS_SERVER_PORT")
    # 后台独立进程模式下不要再自己开一次浏览器（启动器会开），避免开两个标签页
    _open = os.environ.get("FOCUS_OPEN_BROWSER") != "0"
    serve(port=int(_p) if _p else None, open_browser=_open)
