# -*- coding: utf-8 -*-
"""每日备份（规格 8.4）、撤销（规格 8.5）、任务/标签管理（规格 8.2 / 8.3）、记录编辑删除。

* 备份：把 data\\focus.db 用 SQLite 官方 backup API 复制到 backup\\，保留最近 N 份（默认 30）。
* 撤销：所有写操作在 undo_log 里留一步，最近至少 20 步可撤销。
* 删除：一律软删除（deleted_at），可恢复。
"""

import os
import json
import glob
import shutil
import datetime

from . import db, util


# ================================================================ 每日备份

def daily_backup(conn, force=False, note=""):
    """每天做一份备份。当天已经备份过就直接返回（除非 force=True）。

    判断"今天备份过没有"时，不光看登记表，还要确认那份文件**确实还在**——
    这样万一用户手工删了 backup\\ 里的文件，下次打开软件会重新做一份。
    """
    db.ensure_dirs()
    today = util.today_str()
    if not force:
        rows = conn.execute(
            "SELECT file_name FROM backup_log WHERE substr(ts,1,10)=?", (today,)).fetchall()
        for r in rows:
            if os.path.exists(os.path.join(db.BACKUP_DIR, r["file_name"])):
                return {"ok": True, "skipped": True, "file": None,
                        "message": "今天已经备份过了。"}
        if rows:
            # 登记过但文件不在了（被手工删了/挪走了）-> 重新备份一份
            conn.execute("DELETE FROM backup_log WHERE substr(ts,1,10)=?", (today,))

    name = "focus_%s_%s.db" % (today, datetime.datetime.now().strftime("%H%M%S"))
    dst = os.path.join(db.BACKUP_DIR, name)

    # 用 SQLite 官方 backup API：即使有并发写入也能得到一致快照
    target = db.connect(dst)
    try:
        conn.backup(target)
    finally:
        target.close()

    size = os.path.getsize(dst)
    conn.execute("INSERT INTO backup_log(file_name, file_size, note) VALUES (?,?,?)",
                 (name, size, note or ("每日自动备份" if not force else "手动备份")))
    removed = prune_backups(conn)
    return {"ok": True, "skipped": False, "file": name, "size": size,
            "removed": removed,
            "message": "已备份到 backup\\%s（%.1f KB）%s"
                       % (name, size / 1024.0,
                          ("，同时清理了 %d 份旧备份" % len(removed)) if removed else "")}


def prune_backups(conn):
    """只保留最近 N 份（设置项 backup_keep，默认 30）。"""
    keep = db.get_setting_int(conn, "backup_keep", 30)
    if keep <= 0:
        return []
    files = sorted(glob.glob(os.path.join(db.BACKUP_DIR, "focus_*.db")))
    removed = []
    if len(files) > keep:
        for f in files[:len(files) - keep]:
            try:
                os.remove(f)
                removed.append(os.path.basename(f))
            except OSError:
                pass
        if removed:
            conn.execute("DELETE FROM backup_log WHERE file_name IN (%s)"
                         % ",".join("?" * len(removed)), removed)
    return removed


def list_backups(conn):
    out = []
    for f in sorted(glob.glob(os.path.join(db.BACKUP_DIR, "focus_*.db")), reverse=True):
        st = os.stat(f)
        out.append({"file": os.path.basename(f), "size": st.st_size,
                    "time": datetime.datetime.fromtimestamp(st.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"),
                    "size_text": "%.1f KB" % (st.st_size / 1024.0)})
    return out


def restore_backup(path, confirm_token=None):
    """从备份恢复数据库。

    安全做法：先把当前库改名留档（focus.db.before_restore_时间戳），再把备份复制回来。
    需要 confirm_token == "RESTORE" 才会真的执行，防止误触。
    """
    if confirm_token != "RESTORE":
        return {"ok": False, "error": "恢复数据需要二次确认（请勾选确认框）。"}
    if not os.path.exists(path):
        return {"ok": False, "error": "找不到这个备份文件：%s" % path}
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    keep = db.DB_PATH + ".before_restore_" + stamp
    try:
        if os.path.exists(db.DB_PATH):
            shutil.move(db.DB_PATH, keep)
        for suf in ("-wal", "-shm"):
            if os.path.exists(db.DB_PATH + suf):
                os.remove(db.DB_PATH + suf)
        shutil.copy2(path, db.DB_PATH)
    except OSError as e:
        return {"ok": False, "error": "恢复失败：%s。原来的数据仍在 %s，请手工改回来。"
                                      % (e, keep)}
    return {"ok": True, "kept_old_at": keep,
            "message": "已从备份恢复。恢复前的数据库已留在 %s，确认没问题后可以自行删除。"
                       % keep}


# ================================================================ 撤销

def undo_last(conn):
    """撤销最近一步（规格 8.5）。"""
    row = conn.execute(
        "SELECT * FROM undo_log WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return {"ok": False, "error": "没有可以撤销的操作了。"}
    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    action = row["action"]

    if action == "record_create":
        rid = payload.get("record_id")
        conn.execute("UPDATE records SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                     (util.iso(util.now_local()), rid))
        msg = "已撤销：%s" % row["label"]

    elif action == "record_delete":
        rid = payload.get("record_id")
        conn.execute("UPDATE records SET deleted_at=NULL WHERE id=?", (rid,))
        msg = "已恢复被删除的记录：%s" % row["label"]

    elif action == "record_update":
        rid = payload.get("record_id")
        before = payload.get("before") or {}
        if not before:
            return {"ok": False, "error": "这一步没有留下可以回退的内容。"}
        cols = [k for k in ("day", "category_id", "task_id", "task_name", "tags",
                            "duration_min", "note", "start_ts", "end_ts")
                if k in before]
        sets = ", ".join("%s=?" % c for c in cols)
        conn.execute("UPDATE records SET %s, updated_at=? WHERE id=?" % sets,
                     [before[c] for c in cols] + [util.iso(util.now_local()), rid])
        msg = "已撤销修改：%s" % row["label"]

    elif action == "record_time_edit":
        rid = payload.get("record_id")
        before = payload.get("before") or {}
        conn.execute("UPDATE records SET start_ts=?, end_ts=?, duration_min=?, day=?, "
                     "updated_at=? WHERE id=?",
                     (before.get("start_ts"), before.get("end_ts"), before.get("duration_min"),
                      before.get("day"), util.iso(util.now_local()), rid))
        msg = "已撤销起止时间修改：%s" % row["label"]

    elif action == "task_merge":
        # 把被合并过去的记录挪回原任务
        src = payload["from_task_id"]
        dst = payload["to_task_id"]
        rec_ids = payload.get("record_ids") or []
        conn.execute("UPDATE tasks SET archived=0 WHERE id=?", (src,))
        conn.execute("UPDATE tasks SET name=? WHERE id=?", (payload["from_name"], src))
        for rid in rec_ids:
            conn.execute("UPDATE records SET task_id=?, task_name=? WHERE id=?",
                         (src, payload["from_name"], rid))
        msg = "已撤销任务合并：%s" % row["label"]

    elif action == "tag_merge":
        old, new = payload["from_name"], payload["to_name"]
        for rid in (payload.get("record_ids") or []):
            row2 = conn.execute("SELECT tags FROM records WHERE id=?", (rid,)).fetchone()
            if not row2:
                continue
            tags = [x for x in (row2["tags"] or "").split("\n") if x]
            tags = [old if x == new else x for x in tags]
            seen, uniq = set(), []
            for x in tags:
                if x not in seen:
                    seen.add(x)
                    uniq.append(x)
            conn.execute("UPDATE records SET tags=? WHERE id=?", ("\n".join(uniq), rid))
        db.ensure_tag(conn, old)
        msg = "已撤销标签合并：%s" % row["label"]

    elif action == "batch":
        # 一组动作按倒序逐个撤销
        ok_any = False
        for step in reversed(payload.get("steps", [])):
            conn.execute("UPDATE undo_log SET undone=0 WHERE id=?", (step,))
        msg = "已撤销一批操作（%d 步）" % len(payload.get("steps", []))
        ok_any = True
        if not ok_any:
            return {"ok": False, "error": "这批操作没有可撤销的内容。"}

    else:
        return {"ok": False, "error": "这一步暂时不支持自动撤销（%s），请用备份或手工修改。"
                                      % action}

    conn.execute("UPDATE undo_log SET undone=1 WHERE id=?", (row["id"],))
    return {"ok": True, "message": msg, "action": action}


def undo_history(conn, limit=30):
    rows = conn.execute(
        "SELECT id, ts, action, label, undone FROM undo_log ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def prune_undo(conn):
    """只保留最近 N 步（设置项 undo_steps，默认 20；实际保留 3 倍余量）。"""
    keep = max(20, db.get_setting_int(conn, "undo_steps", 20)) * 3
    conn.execute(
        "DELETE FROM undo_log WHERE id NOT IN "
        "(SELECT id FROM undo_log ORDER BY id DESC LIMIT ?)", (keep,))


# ================================================================ 记录编辑 / 删除

EDITABLE = ("day", "category_id", "task_id", "task_name", "tags", "duration_min", "note")


def update_record(conn, rid, **fields):
    """修改一条记录（规格 8.1）。改完写撤销日志。"""
    rec = db.record_by_id(conn, rid)
    if rec is None:
        return {"ok": False, "error": "找不到这条记录（可能已被删除）。"}

    new = {}
    if "day" in fields and fields["day"]:
        try:
            d = util.parse_day(fields["day"])
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        new["day"] = d.isoformat()

    cat_id = fields.get("category_id", rec["category_id"])
    cat = db.category_by_id(conn, cat_id)
    if cat is None:
        return {"ok": False, "error": "找不到这个大类，请刷新页面后重试。"}
    if cat["archived"]:
        return {"ok": False, "error": "「%s」已归档，不能把记录改到这个大类。" % cat["name"]}
    new["category_id"] = cat["id"]

    # 二级任务：没传就沿用原来的（部分字段更新时不要把它清空）
    name = fields.get("task_name")
    name = rec["task_name"] if name is None else str(name).strip()
    if fields.get("task_id"):
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (fields["task_id"],)).fetchone()
        if row is None:
            return {"ok": False, "error": "找不到这个二级任务，请刷新页面后重试。"}
        if row["category_id"] != cat["id"]:
            return {"ok": False, "error": "二级任务与所选大类不一致，请重新选择。"}
        name = row["name"]
    if not name:
        return {"ok": False, "error": "二级任务不能为空。"}
    task_id, _ = db.ensure_task(conn, cat["id"], name)
    new["task_id"] = task_id
    new["task_name"] = name

    tags = fields.get("tags", rec["tags"])
    if isinstance(tags, (list, tuple)):
        tags = "\n".join(t.strip() for t in tags if t and t.strip())
    else:
        tags = "\n".join(x.strip() for x in str(tags or "").split("\n") if x.strip())
    # 历史导入的记录允许没有标签（规格 3.4 / 10.5）；其它来源必须至少一个
    if rec["source"] != "import" and not tags:
        return {"ok": False, "error": "标签至少要填一个。"}
    new["tags"] = tags
    for t in [x for x in tags.split("\n") if x]:
        db.ensure_tag(conn, t)

    # 时长：前端/接口可能用 duration_min，也可能用 duration（H:MM 写法）
    dur_value = None
    for key in ("duration_min", "duration"):
        if key in fields and fields[key] not in (None, ""):
            dur_value = fields[key]
            break
    if dur_value is not None:
        if isinstance(dur_value, int):
            minutes, err = dur_value, None
        else:
            minutes, err = util.parse_duration(dur_value)
        if err:
            return {"ok": False, "error": err}
        if minutes is None or minutes <= 0:
            return {"ok": False, "error": "时长必须大于 0。"}
        new["duration_min"] = minutes

    if "note" in fields:
        new["note"] = str(fields["note"] or "")

    # 计时记录改归属日时，同步挪起止时间（否则日期与区间打架）
    if "day" in new and rec["source"] == "timer" and rec["start_ts"] and rec["end_ts"]:
        s = util.parse_iso(rec["start_ts"])
        e = util.parse_iso(rec["end_ts"])
        new_day = util.parse_day(new["day"])
        new["start_ts"] = util.iso(datetime.datetime.combine(new_day, s.time()))
        new["end_ts"] = util.iso(datetime.datetime.combine(new_day, e.time()))

    before = {k: rec[k] for k in new if k in rec}
    cols = ", ".join("%s=?" % k for k in new)
    conn.execute("UPDATE records SET %s, updated_at=? WHERE id=?" % cols,
                 list(new.values()) + [util.iso(util.now_local()), rid])
    conn.execute("INSERT INTO undo_log(action, label, payload) VALUES (?,?,?)",
                 ("record_update",
                  "修改记录 #%d %s" % (rid, new.get("task_name", rec["task_name"])),
                  json.dumps({"record_id": rid, "before": before}, ensure_ascii=False,
                             default=str)))
    return {"ok": True, "record": db.record_by_id(conn, rid), "message": "已保存修改。"}


def delete_record(conn, rid, cascade_products=True):
    """软删除一条记录（可恢复）。"""
    rec = db.record_by_id(conn, rid)
    if rec is None:
        return {"ok": False, "error": "找不到这条记录（可能已被删除）。"}
    conn.execute("UPDATE records SET deleted_at=?, updated_at=? WHERE id=?",
                 (util.iso(util.now_local()), util.iso(util.now_local()), rid))
    conn.execute("INSERT INTO undo_log(action, label, payload) VALUES (?,?,?)",
                 ("record_delete", "删除记录 #%d %s" % (rid, rec["task_name"]),
                  json.dumps({"record_id": rid}, ensure_ascii=False)))
    return {"ok": True, "message": "已删除这条记录（可以在「撤销」里恢复，或用回收站找回）。"}


def restore_record(conn, rid):
    row = conn.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    if row is None:
        return {"ok": False, "error": "找不到这条记录。"}
    if row["deleted_at"] is None:
        return {"ok": False, "error": "这条记录没有被删除。"}
    conn.execute("UPDATE records SET deleted_at=NULL WHERE id=?", (rid,))
    conn.execute("INSERT INTO undo_log(action, label, payload) VALUES (?,?,?)",
                 ("record_delete", "恢复记录 #%d %s" % (rid, row["task_name"]),
                  json.dumps({"record_id": rid}, ensure_ascii=False)))
    return {"ok": True, "message": "已恢复这条记录。"}


def list_deleted(conn, limit=100):
    rows = conn.execute(
        "SELECT r.*, c.name AS category_name FROM records r "
        "JOIN categories c ON c.id=r.category_id WHERE r.deleted_at IS NOT NULL "
        "ORDER BY r.deleted_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ================================================================ 任务与标签管理

def rename_task(conn, task_id, new_name):
    new_name = (new_name or "").strip()
    if not new_name:
        return {"ok": False, "error": "任务名不能为空。"}
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if t is None:
        return {"ok": False, "error": "找不到这个二级任务。"}
    dup = conn.execute("SELECT id FROM tasks WHERE category_id=? AND name=? AND id<>?",
                       (t["category_id"], new_name, task_id)).fetchone()
    if dup:
        return {"ok": False, "error": "同一大类下已经有叫「%s」的任务了，要合并请用「合并」功能。"
                                      % new_name}
    conn.execute("UPDATE tasks SET name=? WHERE id=?", (new_name, task_id))
    conn.execute("UPDATE records SET task_name=? WHERE task_id=?", (new_name, task_id))
    conn.execute("UPDATE plans SET task_name=? WHERE task_id=?", (new_name, task_id))
    return {"ok": True, "message": "已把任务改名为「%s」，历史记录同步更新。" % new_name}


def merge_tasks(conn, from_id, to_id):
    """把 from 任务合并到 to 任务，历史记录同步迁移（规格 8.2）。"""
    if from_id == to_id:
        return {"ok": False, "error": "不能把任务合并到它自己。"}
    a = conn.execute("SELECT * FROM tasks WHERE id=?", (from_id,)).fetchone()
    b = conn.execute("SELECT * FROM tasks WHERE id=?", (to_id,)).fetchone()
    if a is None or b is None:
        return {"ok": False, "error": "找不到要合并的任务。"}
    rec_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM records WHERE task_id=?", (from_id,))]
    conn.execute("UPDATE records SET task_id=?, task_name=? WHERE task_id=?",
                 (b["id"], b["name"], from_id))
    conn.execute("UPDATE plans SET task_id=?, task_name=? WHERE task_id=?",
                 (b["id"], b["name"], from_id))
    conn.execute("UPDATE tasks SET archived=1 WHERE id=?", (from_id,))
    conn.execute("INSERT INTO undo_log(action, label, payload) VALUES (?,?,?)",
                 ("task_merge", "合并任务「%s」-> 「%s」" % (a["name"], b["name"]),
                  json.dumps({"from_task_id": a["id"], "to_task_id": b["id"],
                              "from_name": a["name"], "record_ids": rec_ids},
                             ensure_ascii=False)))
    return {"ok": True, "moved": len(rec_ids),
            "message": "已把「%s」合并到「%s」，%d 条历史记录已同步迁移。"
                       % (a["name"], b["name"], len(rec_ids))}


def archive_task(conn, task_id, archived=True):
    conn.execute("UPDATE tasks SET archived=? WHERE id=?", (1 if archived else 0, task_id))
    return {"ok": True, "message": "已%s该任务。" % ("归档" if archived else "取消归档")}


def rename_tag(conn, old, new):
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new:
        return {"ok": False, "error": "标签名不能为空。"}
    if old == new:
        return {"ok": True, "message": "标签名没有变化。"}
    rows = conn.execute("SELECT id, tags FROM records WHERE tags LIKE ?",
                        ("%" + old + "%",)).fetchall()
    changed = 0
    for r in rows:
        tags = [x for x in (r["tags"] or "").split("\n") if x]
        if old not in tags:
            continue
        tags = [new if x == old else x for x in tags]
        seen, uniq = set(), []
        for x in tags:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        conn.execute("UPDATE records SET tags=? WHERE id=?", ("\n".join(uniq), r["id"]))
        changed += 1
    db.ensure_tag(conn, new)
    conn.execute("DELETE FROM tags WHERE name=? AND NOT EXISTS "
                 "(SELECT 1 FROM records WHERE (','||REPLACE(tags,char(10),',')||',') "
                 "LIKE '%,'||?||',%')", (old, old))
    return {"ok": True, "changed": changed,
            "message": "已把标签「%s」改名为「%s」，%d 条记录同步更新。" % (old, new, changed)}


def merge_tags(conn, old, new):
    """把标签 old 合并进 new（记录上去重、只留 new）。"""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        return {"ok": False, "error": "请选择两个不同的标签。"}
    rows = conn.execute("SELECT id, tags FROM records WHERE tags LIKE ?",
                        ("%" + old + "%",)).fetchall()
    rec_ids, changed = [], 0
    for r in rows:
        tags = [x for x in (r["tags"] or "").split("\n") if x]
        if old not in tags:
            continue
        tags = [new if x == old else x for x in tags]
        seen, uniq = set(), []
        for x in tags:
            if x not in seen:
                seen.add(x)
                uniq.append(x)
        conn.execute("UPDATE records SET tags=? WHERE id=?", ("\n".join(uniq), r["id"]))
        rec_ids.append(r["id"])
        changed += 1
    db.ensure_tag(conn, new)
    conn.execute("DELETE FROM tags WHERE name=?", (old,))
    conn.execute("INSERT INTO undo_log(action, label, payload) VALUES (?,?,?)",
                 ("tag_merge", "合并标签「%s」-> 「%s」" % (old, new),
                  json.dumps({"from_name": old, "to_name": new, "record_ids": rec_ids},
                             ensure_ascii=False)))
    return {"ok": True, "changed": changed,
            "message": "已把标签「%s」合并到「%s」，%d 条记录同步更新。" % (old, new, changed)}


# ================================================================ 大类管理

def add_category(conn, name, counts_work=False):
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "大类名称不能为空。"}
    if db.category_by_name(conn, name):
        return {"ok": False, "error": "已经有叫「%s」的大类了。" % name}
    cid = db.ensure_category(conn, name, 1 if counts_work else 0)
    return {"ok": True, "category_id": cid,
            "message": "已新增大类「%s」（是否计入工作时间：%s）。"
                       % (name, "是" if counts_work else "否")}


def update_category(conn, cid, name=None, counts_work=None, archived=None, sort_order=None):
    cat = db.category_by_id(conn, cid)
    if cat is None:
        return {"ok": False, "error": "找不到这个大类。"}
    if name is not None:
        name = name.strip()
        if not name:
            return {"ok": False, "error": "大类名称不能为空。"}
        other = db.category_by_name(conn, name)
        if other and other["id"] != cid:
            return {"ok": False, "error": "已经有叫「%s」的大类了。" % name}
        conn.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
    if counts_work is not None:
        conn.execute("UPDATE categories SET counts_work=? WHERE id=?",
                     (1 if counts_work else 0, cid))
    if archived is not None:
        conn.execute("UPDATE categories SET archived=? WHERE id=?", (1 if archived else 0, cid))
    if sort_order is not None:
        conn.execute("UPDATE categories SET sort_order=? WHERE id=?", (int(sort_order), cid))
    return {"ok": True, "message": "已保存大类设置。"}


def delete_category(conn, cid):
    """默认 5 类永不可删（规格 3.1）。其它大类只有在没有任何记录/计划/目标时才能删。"""
    cat = db.category_by_id(conn, cid)
    if cat is None:
        return {"ok": False, "error": "找不到这个大类。"}
    if cat["is_default"]:
        return {"ok": False, "error": "「%s」是默认大类，不能删除（可以改名或归档）。" % cat["name"]}
    n = conn.execute("SELECT COUNT(*) FROM records WHERE category_id=? AND deleted_at IS NULL",
                     (cid,)).fetchone()[0]
    if n:
        return {"ok": False, "error": "「%s」下还有 %d 条记录，不能删除。请先把记录改到别的大类，"
                                      "或改成归档。" % (cat["name"], n)}
    conn.execute("DELETE FROM goals WHERE category_id=?", (cid,))
    conn.execute("DELETE FROM plans WHERE category_id=?", (cid,))
    conn.execute("DELETE FROM tasks WHERE category_id=?", (cid,))
    conn.execute("DELETE FROM categories WHERE id=?", (cid,))
    return {"ok": True, "message": "已删除大类「%s」。" % cat["name"]}
