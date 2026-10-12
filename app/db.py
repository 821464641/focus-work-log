# -*- coding: utf-8 -*-
"""数据层：SQLite 连接、建表、种子数据、通用读写辅助。

设计要点
--------
* 单个数据库文件 data/focus.db，零第三方依赖（标准库 sqlite3）。
* 开启 WAL + 外键 + 每次写操作立即提交，断电/强杀进程不损坏数据。
* 所有写操作都记一条 undo_log，供「撤销上一步」使用（规格 8.5）。
* 删除一律是软删除（deleted_at 非空），可以恢复（规格 3.4）。
"""

import os
import sqlite3
import datetime

from . import util

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(APP_DIR)
DATA_DIR = os.path.join(ROOT_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "focus.db")
BACKUP_DIR = os.path.join(ROOT_DIR, "backup")
REPORTS_DIR = os.path.join(ROOT_DIR, "reports")
EXPORT_DIR = os.path.join(ROOT_DIR, "export")
IMPORT_DIR = os.path.join(ROOT_DIR, "import")

_SCHEMA = """
PRAGMA foreign_keys = ON;

-- 大类（规格 3.1）：默认 5 类永不可删；可改名/排序/归档/新增
CREATE TABLE IF NOT EXISTS categories (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL UNIQUE,
  counts_work   INTEGER NOT NULL DEFAULT 0,   -- 1 = 计入"工作时间"
  is_default    INTEGER NOT NULL DEFAULT 0,   -- 1 = 5 个默认大类，禁止删除
  sort_order    INTEGER NOT NULL DEFAULT 100,
  archived      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 二级任务（规格 3.2）：属于某大类，可改名/合并/归档
CREATE TABLE IF NOT EXISTS tasks (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id   INTEGER NOT NULL REFERENCES categories(id),
  name          TEXT    NOT NULL,
  archived      INTEGER NOT NULL DEFAULT 0,
  sort_order    INTEGER NOT NULL DEFAULT 100,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE (category_id, name)
);
CREATE INDEX IF NOT EXISTS idx_tasks_cat ON tasks(category_id);

-- 标签注册表（规格 3.3）：记录里存的是标签名（换行分隔），本表只做候选/改名/合并
CREATE TABLE IF NOT EXISTS tags (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL UNIQUE,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 记录（规格 3.4）
CREATE TABLE IF NOT EXISTS records (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  day           TEXT    NOT NULL,                 -- 归属日 YYYY-MM-DD
  category_id   INTEGER NOT NULL REFERENCES categories(id),
  task_id       INTEGER REFERENCES tasks(id),
  task_name     TEXT    NOT NULL DEFAULT '',      -- 冗余保存，任务改名时同步更新
  tags          TEXT    NOT NULL DEFAULT '',      -- 换行分隔的标签名，可空（仅导入数据豁免）
  duration_min  INTEGER NOT NULL CHECK (duration_min > 0),
  source        TEXT    NOT NULL CHECK (source IN ('timer','manual','import')),
  start_ts      TEXT,                             -- 计时记录才有，'YYYY-MM-DD HH:MM:SS'
  end_ts        TEXT,
  note          TEXT    NOT NULL DEFAULT '',
  cross_midnight INTEGER NOT NULL DEFAULT 0,      -- 1 = 这次计时跨过 00:00（仅作标记）
  import_key    TEXT,                             -- 导入去重指纹（增量跳过已导入行）
  import_row    INTEGER,                          -- 来自原表第几行（可追溯）
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  deleted_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_rec_day  ON records(day);
CREATE INDEX IF NOT EXISTS idx_rec_cat  ON records(category_id);
CREATE INDEX IF NOT EXISTS idx_rec_task ON records(task_id);
CREATE INDEX IF NOT EXISTS idx_rec_src  ON records(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rec_import_key
  ON records(import_key) WHERE import_key IS NOT NULL;

-- 产出物（规格 3.4）：一条记录可以有多条
CREATE TABLE IF NOT EXISTS record_products (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id     INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
  ptype         TEXT    NOT NULL DEFAULT '其他',
  name          TEXT    NOT NULL DEFAULT '',
  url           TEXT    NOT NULL DEFAULT '',
  sort_order    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_prod_rec ON record_products(record_id);

-- 计划条目（规格 3.5）：日期 + 大类 + 二级任务(必填) + 计划时长(可空)
CREATE TABLE IF NOT EXISTS plans (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  day           TEXT    NOT NULL,
  category_id   INTEGER NOT NULL REFERENCES categories(id),
  task_id       INTEGER NOT NULL REFERENCES tasks(id),
  task_name     TEXT    NOT NULL DEFAULT '',
  planned_min   INTEGER,                          -- NULL = 只标记"要做这件事"
  note          TEXT    NOT NULL DEFAULT '',
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  deleted_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_plan_day ON plans(day);
CREATE INDEX IF NOT EXISTS idx_plan_cat ON plans(category_id);

-- 目标（规格 3.6）：按大类设每日下限，工作日/周末两组；软件不预设任何数字
CREATE TABLE IF NOT EXISTS goals (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id   INTEGER NOT NULL REFERENCES categories(id),
  goal_type     TEXT    NOT NULL CHECK (goal_type IN ('workday','weekend')),
  minutes       INTEGER NOT NULL CHECK (minutes > 0),
  updated_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE (category_id, goal_type)
);

-- 设置（键值对）
CREATE TABLE IF NOT EXISTS settings (
  key           TEXT    PRIMARY KEY,
  value         TEXT    NOT NULL
);

-- 撤销日志（规格 8.5）：最近 20 步以上
CREATE TABLE IF NOT EXISTS undo_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  action        TEXT    NOT NULL,                 -- 动作类型
  label         TEXT    NOT NULL DEFAULT '',      -- 给用户看的中文说明
  payload       TEXT    NOT NULL DEFAULT '{}',    -- 撤销所需的 JSON
  undone        INTEGER NOT NULL DEFAULT 0
);

-- 正在计时的任务（规格 4.1）。计时状态放在后端，不在浏览器里：
-- 关掉页面、重启电脑后，下次打开仍然在走。
-- 单段时长不用"现在减开始"算，而是用 elapsed_sec 累计，这样"暂停期间不计入"自然成立。
CREATE TABLE IF NOT EXISTS timers (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  category_id   INTEGER NOT NULL REFERENCES categories(id),
  task_id       INTEGER NOT NULL REFERENCES tasks(id),
  task_name     TEXT    NOT NULL DEFAULT '',
  tags          TEXT    NOT NULL DEFAULT '',
  status        TEXT    NOT NULL DEFAULT 'running' CHECK (status IN ('running','paused')),
  seg_started_at TEXT   NOT NULL,                 -- 当前这一段"显示计时"的锚点
  elapsed_sec   INTEGER NOT NULL DEFAULT 0,       -- 当前这一段之前已计入的秒数（不含暂停）
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  cross_midnight INTEGER NOT NULL DEFAULT 0,      -- 本次计时是否已跨过 00:00
  note          TEXT    NOT NULL DEFAULT ''
);

-- 原表备份登记（规格 10.1）：每次导入都把副本存到 import\，这里留记录
CREATE TABLE IF NOT EXISTS source_archives (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  original_path TEXT    NOT NULL,
  copy_path     TEXT    NOT NULL,
  file_size     INTEGER NOT NULL DEFAULT 0,
  file_sha1     TEXT    NOT NULL DEFAULT '',
  imported_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  note          TEXT    NOT NULL DEFAULT ''
);

-- 原表行留档（可追溯每一次导入读了原表的哪一行）
CREATE TABLE IF NOT EXISTS archive_rows (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  archive_id     INTEGER NOT NULL REFERENCES source_archives(id) ON DELETE CASCADE,
  row_no         INTEGER NOT NULL,
  raw_day_cell   TEXT    NOT NULL DEFAULT '',
  day            TEXT,                            -- 解析出的 YYYY-MM-DD，解析不了则为 NULL
  total_h_col    TEXT    NOT NULL DEFAULT '',     -- N 列原值（不导入，只留档）
  outcome        TEXT    NOT NULL DEFAULT '',     -- date_row / summary_row / blank_row
  records_created INTEGER NOT NULL DEFAULT 0,
  records_skipped INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_arow ON archive_rows(archive_id, row_no);

-- 原表格子留档：导入后原表哪怕丢了，也能在软件里追溯
CREATE TABLE IF NOT EXISTS archive_cells (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  archive_id     INTEGER NOT NULL REFERENCES source_archives(id) ON DELETE CASCADE,
  row_no         INTEGER NOT NULL,
  category_name  TEXT    NOT NULL,
  raw_text       TEXT    NOT NULL DEFAULT '',
  raw_hours      TEXT    NOT NULL DEFAULT '',
  pieces_count   INTEGER NOT NULL DEFAULT 0      -- 拆成了几条
);
CREATE INDEX IF NOT EXISTS idx_acell ON archive_cells(archive_id, row_no);

-- 日报/周报/月报注册表（规格 6.2）：记录已生成的报表文件
CREATE TABLE IF NOT EXISTS reports (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT    NOT NULL,                 -- week / month / custom
  period_key    TEXT    NOT NULL,                 -- 2025-W31 / 2025-09 / custom:...
  start_day     TEXT    NOT NULL,
  end_day       TEXT    NOT NULL,
  file_name     TEXT    NOT NULL DEFAULT '',
  generated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  acknowledged  INTEGER NOT NULL DEFAULT 0,       -- 首页是否已提示过
  UNIQUE (kind, period_key)
);

-- 每日备份登记（规格 8.4）
CREATE TABLE IF NOT EXISTS backup_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts            TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  file_name     TEXT    NOT NULL,
  file_size     INTEGER NOT NULL DEFAULT 0,
  note          TEXT    NOT NULL DEFAULT ''
);

-- 元信息：记录入库时间，用于安全的登录提示等
CREATE TABLE IF NOT EXISTS meta (
  key           TEXT    PRIMARY KEY,
  value         TEXT    NOT NULL
);
"""

# 默认设置（规格 15 节「默认值」表）
DEFAULT_SETTINGS = {
    "timer_overtime_min": "240",       # 忘记停止阈值 4:00
    "blank_recall_days": "7",          # 空白日召回最近 7 天
    "top_n": "5",                      # Top 任务数
    "backup_keep": "30",               # 备份保留份数
    "undo_steps": "20",                # 撤销步数
    "port": "8090",                    # 优先端口（绝不使用 3080）
    "saturday_ratio": "0.5",           # 周六目标 = 工作日目标 × 50%
    "theme": "dark",                   # 界面主题
    "compact": "0",                    # 紧凑视图
    "first_run_done": "0",
    "last_opened": "",
}


def ensure_dirs():
    for d in (DATA_DIR, BACKUP_DIR, REPORTS_DIR, EXPORT_DIR, IMPORT_DIR):
        os.makedirs(d, exist_ok=True)
    return {
        "data": DATA_DIR, "backup": BACKUP_DIR, "reports": REPORTS_DIR,
        "export": EXPORT_DIR, "import": IMPORT_DIR,
    }


def set_db_path(path):
    """切换当前使用的数据库文件（自测/临时实例用，正常使用不会调用）。"""
    global DB_PATH
    DB_PATH = path
    return DB_PATH


def connect(db_path=None):
    """建立连接。行工厂用 sqlite3.Row，方便按列名取值。"""
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # 抗崩溃
    conn.execute("PRAGMA synchronous=FULL")      # 写操作立即落盘
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn=None):
    """建表 + 灌默认大类 + 默认设置。可反复执行（幂等）。

    传入 conn 时复用该连接（不关闭它）；不传则自建、自关。
    注意：不要把 return 写在 try/finally 里面——那条 return 会在 finally 关闭连接
    之后才真正取得返回值，于是调用方拿到一个已关闭的连接（Python 3.14 实测如此）。
    """
    own = conn is None
    if own:
        conn = connect()
    try:
        conn.executescript(_SCHEMA)
        # 默认 5 大类，永不可删
        for name, counts, order in util.DEFAULT_CATEGORIES:
            row = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO categories(name, counts_work, is_default, sort_order) "
                    "VALUES (?,?,1,?)", (name, counts, order))
        # 默认设置
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?,?)", (k, v))
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version','1')")
        _migrate(conn)
    except Exception:
        if own:
            conn.close()
        raise
    return conn


# ---------------------------------------------------------------- 轻量升级

def _migrate(conn):
    """给旧库补新列（幂等）。只用 ALTER TABLE ADD COLUMN，绝不动已有数据。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(timers)")}
    if cols and "seg_base_sec" in cols:
        # v1 试验期留下的列，已不再使用（新版模型不需要它），保留不动即可
        pass


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_setting_int(conn, key, default=0):
    try:
        return int(float(get_setting(conn, key, default)))
    except (TypeError, ValueError):
        return default


def get_setting_float(conn, key, default=0.0):
    try:
        return float(get_setting(conn, key, default))
    except (TypeError, ValueError):
        return default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


# ---------------------------------------------------------------- 大类

def all_categories(conn, include_archived=True):
    sql = "SELECT * FROM categories"
    if not include_archived:
        sql += " WHERE archived=0"
    sql += " ORDER BY sort_order, id"
    return [dict(r) for r in conn.execute(sql)]


def category_by_id(conn, cid):
    row = conn.execute("SELECT * FROM categories WHERE id=?", (cid,)).fetchone()
    return dict(row) if row else None


def category_by_name(conn, name):
    row = conn.execute("SELECT * FROM categories WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def ensure_category(conn, name, counts_work=0):
    """按名字取大类，没有就新建（导入时用）。返回 id。"""
    row = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    mx = conn.execute("SELECT COALESCE(MAX(sort_order),0)+10 FROM categories").fetchone()[0]
    cur = conn.execute(
        "INSERT INTO categories(name, counts_work, is_default, sort_order) VALUES (?,?,0,?)",
        (name, 1 if counts_work else 0, mx))
    return cur.lastrowid


# ---------------------------------------------------------------- 二级任务

def ensure_task(conn, category_id, name):
    """按 (大类, 任务名) 取任务，没有就新建。返回 (task_id, created?)。

    规格 3.2：任务名不做自动清洗、不做自动归并。
    """
    row = conn.execute("SELECT id FROM tasks WHERE category_id=? AND name=?",
                       (category_id, name)).fetchone()
    if row:
        return row["id"], False
    mx = conn.execute("SELECT COALESCE(MAX(sort_order),0)+10 FROM tasks WHERE category_id=?",
                      (category_id,)).fetchone()[0]
    cur = conn.execute("INSERT INTO tasks(category_id, name, sort_order) VALUES (?,?,?)",
                       (category_id, name, mx))
    return cur.lastrowid, True


def all_tasks(conn, include_archived=True):
    sql = ("SELECT t.*, c.name AS category_name, c.counts_work "
           "FROM tasks t JOIN categories c ON c.id=t.category_id")
    if not include_archived:
        sql += " WHERE t.archived=0 AND c.archived=0"
    sql += " ORDER BY c.sort_order, t.sort_order, t.id"
    return [dict(r) for r in conn.execute(sql)]


# ---------------------------------------------------------------- 标签

def ensure_tag(conn, name):
    name = (name or "").strip()
    if not name:
        return None
    conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
    row = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()
    return row["id"] if row else None


def all_tags(conn):
    return [dict(r) for r in conn.execute(
        "SELECT t.*, (SELECT COUNT(*) FROM records r WHERE r.deleted_at IS NULL "
        " AND (','||REPLACE(r.tags,char(10),',')||',') LIKE '%,'||t.name||',%') AS use_count "
        "FROM tags t ORDER BY t.name")]


# ---------------------------------------------------------------- 记录辅助

def insert_record(conn, day, category_id, task_id, task_name, tags, duration_min,
                  source, start_ts=None, end_ts=None, note="", cross_midnight=0,
                  import_key=None, import_row=None, products=None):
    """插一条记录，返回 id。tags 传 list 或换行字符串都行。"""
    if isinstance(tags, (list, tuple)):
        tag_text = "\n".join(t.strip() for t in tags if t and t.strip())
    else:
        tag_text = (tags or "").strip()
    now = util.iso(util.now_local())
    cur = conn.execute(
        "INSERT INTO records(day, category_id, task_id, task_name, tags, duration_min, "
        " source, start_ts, end_ts, note, cross_midnight, import_key, import_row, "
        " created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (day, category_id, task_id, task_name, tag_text, int(duration_min), source,
         start_ts, end_ts, note or "", 1 if cross_midnight else 0,
         import_key, import_row, now, now))
    rid = cur.lastrowid
    if products:
        for i, p in enumerate(products):
            conn.execute("INSERT INTO record_products(record_id, ptype, name, url, sort_order) "
                         "VALUES (?,?,?,?,?)",
                         (rid, p.get("ptype", "其他"), p.get("name", ""), p.get("url", ""), i))
    return rid


def record_by_id(conn, rid, include_deleted=False):
    sql = "SELECT r.*, c.name AS category_name, c.counts_work FROM records r " \
          "JOIN categories c ON c.id=r.category_id WHERE r.id=?"
    if not include_deleted:
        sql += " AND r.deleted_at IS NULL"
    row = conn.execute(sql, (rid,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["products"] = [dict(p) for p in conn.execute(
        "SELECT * FROM record_products WHERE record_id=? ORDER BY sort_order, id", (rid,))]
    return d


def count_records(conn):
    return conn.execute("SELECT COUNT(*) FROM records WHERE deleted_at IS NULL").fetchone()[0]


def min_record_day(conn):
    row = conn.execute("SELECT MIN(day) FROM records WHERE deleted_at IS NULL").fetchone()
    return row[0] if row and row[0] else None


def max_record_day(conn):
    row = conn.execute("SELECT MAX(day) FROM records WHERE deleted_at IS NULL").fetchone()
    return row[0] if row and row[0] else None
