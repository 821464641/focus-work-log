# -*- coding: utf-8 -*-
"""手写 .xlsx 导出（规格第 9 节）。零第三方依赖，不联网下载任何组件。

.xlsx 本质是一个 zip，里面放几段 XML。这里只用标准库 zipfile + xml 生成：
  [Content_Types].xml / _rels/.rels / xl/workbook.xml / xl/_rels/workbook.xml.rels
  xl/styles.xml / xl/worksheets/sheetN.xml
字符串全部用 inlineStr 写在单元格里，省掉 sharedStrings，实现最简单、最不容易出错。

4 个 sheet：
  1. 明细表    一行一条记录
  2. 日汇总表  一行一天，按大类分列 + 总计 + 工作时间 + 是否达标
  3. 汇总页    按二级任务聚合 / 按大类聚合 / 按月度统计
  4. 图表页    每日趋势 / 大类占比 / Top 任务（用单元格条形图 + 数据区域表示）
"""

import os
import re
import zipfile
import datetime

from . import db, util


# ================================================================ 基础工具

def _esc(s):
    s = "" if s is None else str(s)
    s = (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
         .replace('"', "&quot;"))
    # XML 1.0 不允许的控制字符，去掉（换行保留为 &#10;）
    s = "".join(ch for ch in s if ch >= " " or ch in "\t\n\r")
    return s.replace("\n", "&#10;")


def _col_letter(idx):
    """1 -> A, 27 -> AA"""
    s = ""
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, value, style=0):
    if value is None or value == "":
        return '<c r="%s" s="%d"/>' % (ref, style) if style else ""
    if isinstance(value, bool):
        value = "是" if value else "否"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, repr(round(float(value), 6)))
    return '<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' \
           % (ref, style, _esc(value))


def _sheet_xml(rows, col_widths=None, freeze_header=True, autofilter=False):
    """rows: [[cell, cell, ...], ...]，cell 可以是值（自动判断）或 ('num',值,样式)。"""
    xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']
    if col_widths:
        xml.append("<cols>")
        for i, w in enumerate(col_widths, start=1):
            xml.append('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i, i, w))
        xml.append("</cols>")
    xml.append("<sheetData>")
    max_col = 0
    for ri, row in enumerate(rows, start=1):
        if not row:
            continue
        xml.append('<row r="%d">' % ri)
        for ci, val in enumerate(row, start=1):
            ref = "%s%d" % (_col_letter(ci), ri)
            style = 0
            if isinstance(val, tuple) and len(val) == 3 and val[0] == "num":
                val, style = val[1], val[2]         # ('num', 值, 样式)
            elif isinstance(val, tuple) and len(val) == 2:
                if val[0] == "s":
                    val, style = val[1], 1          # 表头：蓝底白字
                elif val[0] == "sec":
                    val, style = val[1], 2          # 小标题：加粗
            xml.append(_cell(ref, val, style))
            max_col = max(max_col, ci)
        xml.append("</row>")
    xml.append("</sheetData>")
    if freeze_header:
        xml.append('<sheetViews><sheetView workbookViewId="0">'
                   '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                   '</sheetView></sheetViews>')
    last = "%s%d" % (_col_letter(max(max_col, 1)), max(len(rows), 1))
    xml.append('<dimension ref="A1:%s"/>' % last)
    if autofilter and rows:
        xml.append('<autoFilter ref="A1:%s"/>' % last)
    xml.append("</worksheet>")
    return "".join(xml)


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="3">
  <font><sz val="11"/><name val="微软雅黑"/></font>
  <font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="微软雅黑"/></font>
  <font><b/><sz val="11"/><name val="微软雅黑"/></font>
</fonts>
<fills count="3">
  <fill><patternFill patternType="none"/></fill>
  <fill><patternFill patternType="gray125"/></fill>
  <fill><patternFill patternType="solid"><fgColor rgb="FF3C6E9F"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="3">
  <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
  <xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1">
    <alignment horizontal="center" vertical="center"/></xf>
  <xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
%s
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""


def _workbook_xml(names):
    sheets = "".join(
        '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_esc(n)[:31], i + 1, i + 1)
        for i, n in enumerate(names))
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            "<sheets>%s</sheets></workbook>" % sheets)


def _workbook_rels(n):
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    for i in range(n):
        rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>'
                    % (i + 1, i + 1))
    rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (n + 1))
    rels.append("</Relationships>")
    return "".join(rels)


def write_xlsx(path, sheets):
    """sheets: [(名称, rows, col_widths), ...]"""
    names = [s[0] for s in sheets]
    ct_over = "\n".join(
        '<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1)
        for i in range(len(sheets)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES % ct_over)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _workbook_xml(names))
        z.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets)))
        z.writestr("xl/styles.xml", _STYLES)
        for i, (nm, rows, widths) in enumerate(sheets):
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1),
                       _sheet_xml(rows, widths, freeze_header=True, autofilter=True))
    return path


# ================================================================ 导出内容

HEAD = lambda *titles: [("s", t) for t in titles]


def export_excel(conn, start_day=None, end_day=None, exclude_categories=None, note=""):
    """一键导出。exclude_categories 里的类名会被排除（规格第 9 节）。"""
    from . import stats, goals

    exclude = set(exclude_categories or [])
    cats = [c for c in db.all_categories(conn, include_archived=True)
            if c["name"] not in exclude]
    cat_names = [c["name"] for c in cats]
    cat_ids = {c["id"] for c in cats}

    if not start_day:
        start_day = db.min_record_day(conn) or util.today_str()
    if not end_day:
        end_day = db.max_record_day(conn) or util.today_str()

    where_cat = ""
    params = [start_day, end_day]
    if exclude:
        where_cat = " AND c.name NOT IN (%s)" % ",".join("?" * len(exclude))
        params += list(exclude)

    recs = conn.execute(
        "SELECT r.*, c.name AS category_name FROM records r "
        "JOIN categories c ON c.id=r.category_id "
        "WHERE r.deleted_at IS NULL AND r.day BETWEEN ? AND ?" + where_cat +
        " ORDER BY r.day, c.sort_order, r.id", params).fetchall()

    # ---------------- sheet 1 明细表 ----------------
    s1 = [HEAD("日期", "星期", "大类", "二级任务", "标签", "时长(H:MM)", "时长(小时小数)",
               "备注", "产出物", "来源", "开始时间", "结束时间")]
    src_cn = {"timer": "计时", "manual": "手填", "import": "导入"}
    for r in recs:
        prods = conn.execute(
            "SELECT * FROM record_products WHERE record_id=? ORDER BY sort_order, id",
            (r["id"],)).fetchall()
        ptext = "；".join("%s：%s%s" % (p["ptype"], p["name"],
                                      ("（%s）" % p["url"]) if p["url"] else "") for p in prods)
        s1.append([util.parse_day(r["day"]).isoformat(), util.weekday_cn(r["day"]),
                   r["category_name"], r["task_name"],
                   (r["tags"] or "").replace("\n", "、"),
                   util.fmt_hm(r["duration_min"]), util.fmt_hours(r["duration_min"]),
                   r["note"] or "", ptext, src_cn.get(r["source"], r["source"]),
                   (r["start_ts"] or "").replace(" ", "\n") if r["start_ts"] else "",
                   (r["end_ts"] or "").replace(" ", "\n") if r["end_ts"] else ""])

    # ---------------- sheet 2 日汇总表 ----------------
    day_rows = conn.execute(
        "SELECT r.day, c.name AS cat, SUM(r.duration_min) AS mins FROM records r "
        "JOIN categories c ON c.id=r.category_id WHERE r.deleted_at IS NULL "
        "AND r.day BETWEEN ? AND ?" + where_cat +
        " GROUP BY r.day, c.name ORDER BY r.day", params).fetchall()
    per_day = {}
    for r in day_rows:
        per_day.setdefault(r["day"], {})[r["cat"]] = r["mins"]

    # 达标判定用"全部已设目标大类"，所以这里按未排除的口径算
    all_cat_min = conn.execute(
        "SELECT r.day, c.name AS cat, SUM(r.duration_min) AS mins FROM records r "
        "JOIN categories c ON c.id=r.category_id WHERE r.deleted_at IS NULL "
        "AND r.day BETWEEN ? AND ? GROUP BY r.day, c.name",
        (start_day, end_day)).fetchall()
    all_per_day = {}
    for r in all_cat_min:
        all_per_day.setdefault(r["day"], {})[r["cat"]] = r["mins"]

    s2 = [HEAD("日期", "星期") + cat_names + ["总计", "工作时间(科研+会议)", "是否达标", "记录条数"]]
    for d in util.days_between(start_day, end_day):
        key = d.isoformat()
        cm = per_day.get(key, {})
        if not cm:
            continue                      # 空白日：不写进导出（明细里也没有内容）
        row = [key, util.weekday_cn(d)] + [
            util.fmt_hm(cm.get(n, 0)) if cm.get(n) else "" for n in cat_names]
        total = sum(cm.values())
        work = 0
        for c in cats:
            if c["counts_work"]:
                work += cm.get(c["name"], 0)
        st = goals.day_goal_status(conn, key, all_per_day.get(key, {}))
        if not st["has_goal"]:
            flag = "—"                       # 这一天没设目标，不参与判定
        else:
            flag = "达标" if st["achieved"] else "未达标"
        cnt = conn.execute("SELECT COUNT(*) FROM records WHERE deleted_at IS NULL AND day=?"
                           + (" AND category_id IN (%s)" % ",".join("?" * len(cat_ids))
                              if cat_ids else ""),
                           [key] + list(cat_ids)).fetchone()[0]
        row += [util.fmt_hm(total), util.fmt_hm(work), flag, cnt]
        s2.append(row)

    # ---------------- sheet 3 汇总页 ----------------
    s3 = [("sec", "一、按二级任务聚合")]
    s3.append(HEAD("排名", "二级任务", "大类", "总时长(H:MM)", "总时长(小时)", "记录条数", "占比"))
    tasks = stats.task_totals(conn, start_day, end_day)
    tasks = [t for t in tasks if t["category_name"] in cat_names]
    for i, t in enumerate(tasks, start=1):
        s3.append([i, t["task_name"], t["category_name"], util.fmt_hm(t["minutes"]),
                   t["hours"], t["count"], "%.2f%%" % (t["share"] * 100)])
    s3.append([])
    s3.append(("sec", "二、按大类聚合"))
    s3.append(HEAD("大类", "总时长(H:MM)", "总时长(小时)", "记录条数", "占区间总时长",
                   "平均/记录日(h)", "是否计入工作时间"))
    total_min = sum(t["minutes"] for t in tasks) or 0
    rec_days = len(per_day) or 0
    for c in cats:
        m = sum(v.get(c["name"], 0) for v in per_day.values())
        cnt = conn.execute("SELECT COUNT(*) FROM records r JOIN categories c ON c.id=r.category_id "
                           "WHERE r.deleted_at IS NULL AND r.day BETWEEN ? AND ? "
                           "AND c.name=?", (start_day, end_day, c["name"])).fetchone()[0]
        s3.append([c["name"], util.fmt_hm(m), util.fmt_hours(m), cnt,
                   ("%.2f%%" % (m / total_min * 100)) if total_min else "0%",
                   round(m / 60.0 / rec_days, 3) if rec_days else 0.0,
                   "是" if c["counts_work"] else "否"])
    s3.append([])
    s3.append(("sec", "三、按月度统计"))
    s3.append(HEAD("月份", "记录天数", "自然日天数", "记录率", "总计(h)", "平均/记录日(h)",
                   "平均/自然日(h)", "达标天数", "计入判定天数", "达成率"))
    from . import stats as _stats          # 月度聚合在 stats 里，导出不再依赖导入模块
    ms = _stats.monthly_stats(conn)
    for m in ms["months"]:
        # 该月每天的达标情况：用真实的月度区间（1 号 ~ 月末）逐天算
        import calendar
        y, mo = int(m["month"][:4]), int(m["month"][5:7])
        last_d = calendar.monthrange(y, mo)[1]
        m_start = m["month"] + "-01"
        m_end = "%s-%02d" % (m["month"], last_d)
        day_list = stats.range_detail(conn, m_start, m_end)["days"]
        g = goals.range_goal_summary(conn, m_start, m_end, day_list)
        s3.append([m["month"], m["record_days"], m["natural_days"],
                   ("%.2f%%" % (m["record_days"] / m["natural_days"] * 100))
                   if m["natural_days"] else "0%",
                   m["total_h"], m["avg_per_record_day"], m["avg_per_natural_day"],
                   g.get("achieved_days", 0) if g.get("has_goal") else "—",
                   g.get("judged_days", 0) if g.get("has_goal") else "—",
                   ("%.2f%%" % (g["rate"] * 100)) if g.get("has_goal") else "未设目标"])
    s3.append([])
    rng = stats.range_with_goals(conn, start_day, end_day)
    s3.append(("sec", "四、区间总览（%s ~ %s）" % (start_day, end_day)))
    s3.append(HEAD("项目", "数值"))
    for k, v in [("记录天数", rng["record_days"]), ("自然日天数", rng["natural_days"]),
                 ("空白日天数", rng["blank_days"]),
                 ("记录率", "%.2f%%" % (rng["record_rate"] * 100)),
                 ("总计(H:MM)", util.fmt_hm(rng["total_minutes"])),
                 ("总计(小时)", rng["total_h"]),
                 ("平均/记录日(小时)", rng["avg_per_record_day_h"]),
                 ("平均/自然日(小时)", rng["avg_per_natural_day_h"]),
                 ("实际时长(去重,H:MM)", util.fmt_hm(rng["actual_minutes"])),
                 ("投入时长之和(H:MM)", util.fmt_hm(rng["invested_minutes"])),
                 ("工作时间(科研+会议,去重)", util.fmt_hm(rng["work_minutes"])),
                 ("导出时排除的大类", "、".join(sorted(exclude)) if exclude else "无")]:
        s3.append([k, v])

    # ---------------- sheet 4 图表页 ----------------
    # 不依赖外部组件：用"数据 + 单元格内条形图（重复字符）"来表示，Excel/WPS 打开即可看
    from . import stats as st2
    series = st2.daily_series(conn, start_day, end_day)
    s4 = [("sec", "每日趋势（每日总时长与大类分布）")]
    used_cats = sorted({c for d in series for c in d["categories"]})
    s4.append(HEAD("日期", "星期") + used_cats + ["当日合计(H:MM)", "条形图"])
    maxm = max([sum(d["categories"].values()) for d in series] or [1]) or 1
    for d in series:
        tot = sum(d["categories"].values())
        if tot == 0:
            s4.append([d["day"], d["weekday"]] + [""] * len(used_cats) + ["", ""])
            continue
        bar = "█" * max(1, int(round(tot / maxm * 40)))
        s4.append([d["day"], d["weekday"]] +
                  [util.fmt_hm(d["categories"].get(c, 0)) if d["categories"].get(c) else ""
                   for c in used_cats] + [util.fmt_hm(tot), bar])
    s4.append([])
    s4.append(("sec", "大类占比"))
    s4.append(HEAD("大类", "总时长(H:MM)", "占比", "条形图"))
    cm_all = rng["category_minutes"]
    ctot = sum(cm_all.values()) or 1
    for c in cats:
        m = cm_all.get(c["name"], 0)
        if not m:
            continue
        bar = "█" * max(1, int(round(m / ctot * 40)))
        s4.append([c["name"], util.fmt_hm(m), "%.2f%%" % (m / ctot * 100), bar])
    s4.append([])
    s4.append(("sec", "Top 任务"))
    s4.append(HEAD("排名", "二级任务", "大类", "总时长(H:MM)", "条形图"))
    tmax = max([t["minutes"] for t in tasks] or [1]) or 1
    for i, t in enumerate(tasks[:20], start=1):
        bar = "█" * max(1, int(round(t["minutes"] / tmax * 40)))
        s4.append([i, t["task_name"], t["category_name"], util.fmt_hm(t["minutes"]), bar])
    s4.append([])
    s4.append(("sec", "月度对比（近几个月）"))
    s4.append(HEAD("月份", "总计(h)", "平均/记录日(h)", "平均/自然日(h)", "条形图"))
    mmax = max([m["total_h"] for m in ms["months"]] or [1]) or 1
    for m in ms["months"]:
        bar = "█" * max(1, int(round(m["total_h"] / mmax * 40)))
        s4.append([m["month"], m["total_h"], m["avg_per_record_day"], m["avg_per_natural_day"],
                   bar])
    s4.append([])
    s4.append(["说明：本页的条形图用字符画在单元格里，不依赖任何外部组件，"
               "Excel/WPS 打开即可查看。"])

    fname = "专注工作记录_%s_%s%s.xlsx" % (
        start_day.replace("-", ""), end_day.replace("-", ""),
        ("_排除" + "".join(sorted(exclude))) if exclude else "")
    path = os.path.join(db.EXPORT_DIR, fname)
    write_xlsx(path, [
        ("明细表", s1, [12, 6, 9, 34, 18, 11, 13, 26, 30, 7, 18, 18]),
        ("日汇总表", s2, [12, 6] + [10] * len(cat_names) + [10, 18, 9, 9]),
        ("汇总页", s3, [10, 34, 12, 13, 13, 14, 12, 12, 13, 12]),
        ("图表页", s4, [12, 8] + [9] * len(used_cats) + [14, 44]),
    ])
    return {"ok": True, "path": path, "file": fname,
            "size": os.path.getsize(path),
            "records": len(recs) - 1,
            "excluded": sorted(exclude),
            "range": {"start": start_day, "end": end_day},
            "message": "已导出：export\\%s（%.1f KB，%d 条记录%s）"
                       % (fname, os.path.getsize(path) / 1024.0, len(recs) - 1,
                          ("，已排除：" + "、".join(sorted(exclude))) if exclude else "")}


def list_exports():
    out = []
    if not os.path.isdir(db.EXPORT_DIR):
        return out
    for f in sorted(os.listdir(db.EXPORT_DIR), reverse=True):
        if not f.lower().endswith(".xlsx"):
            continue
        p = os.path.join(db.EXPORT_DIR, f)
        out.append({"file": f, "size": os.path.getsize(p),
                    "size_text": "%.1f KB" % (os.path.getsize(p) / 1024.0),
                    "time": datetime.datetime.fromtimestamp(
                        os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")})
    return out
