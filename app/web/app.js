/* 「加油！专注工作记录」前端（原生 JS，无任何依赖、无 CDN）
 *
 * 设计要点：
 *  - 计时状态全部来自后端 /api/timer/status，页面只负责显示；关掉页面时间照样在走。
 *  - 计时显示用「后端返回的 elapsed_sec + 本地经过的毫秒」做插值，避免每秒打后端。
 *  - 图表全部用内联 SVG 自己画。
 */

'use strict';

// ================================================================ 基础

const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const esc = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');

const CAT_COLORS = {
  '科研': '#4da3ff', '会议': '#f0a020', '阅读': '#38c172',
  '运动': '#a06bff', '娱乐': '#ef7fae'
};
const FALLBACK_COLORS = ['#4da3ff', '#f0a020', '#38c172', '#a06bff', '#ef7fae',
  '#22b8cf', '#ff8787', '#94d82d'];
function catColor(name, allNames) {
  if (CAT_COLORS[name]) return CAT_COLORS[name];
  const i = (allNames || []).indexOf(name);
  return FALLBACK_COLORS[(i < 0 ? 0 : i) % FALLBACK_COLORS.length];
}

function fmtHM(min) {
  min = Math.max(0, Math.round(Number(min) || 0));
  return Math.floor(min / 60) + ':' + String(min % 60).padStart(2, '0');
}
function clock(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}
function todayStr(d) {
  const t = d || new Date();
  return t.getFullYear() + '-' + String(t.getMonth() + 1).padStart(2, '0') + '-' +
    String(t.getDate()).padStart(2, '0');
}
function shiftDay(day, n) {
  const d = new Date(day + 'T00:00:00');
  d.setDate(d.getDate() + n);
  return todayStr(d);
}

function toast(msg, kind) {
  const host = $('#toast');
  const el = document.createElement('div');
  el.className = 'toast ' + (kind || 'info');
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, 4200);
  setTimeout(() => el.remove(), 4600);
}

async function api(path, opts) {
  const o = Object.assign({ headers: {} }, opts || {});
  if (o.body !== undefined) {
    o.method = o.method || 'POST';
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(o.body);
  }
  let res, data;
  try {
    res = await fetch(path, o);
  } catch (e) {
    toast('连不上本地服务了（' + e.message + '）。请确认那个黑色窗口还开着，然后刷新页面。', 'bad');
    throw e;
  }
  try {
    data = await res.json();
  } catch (e) {
    toast('服务返回了看不懂的内容，请刷新页面重试。', 'bad');
    throw e;
  }
  if (data && data.ok === false && data.error) toast(data.error, 'bad');
  return data;
}
const post = (p, body) => api(p, { body: body || {} });

// ================================================================ 全局状态

const S = {
  boot: null,
  view: 'home',
  timers: [],           // 后端返回的计时器
  tickBase: 0,          // 上次同步计时器时的本地毫秒
  timersSynced: false,
  manual: {},           // 补录表单状态
  recordForm: {},
  planForm: {},
  statsRange: null,
  report: null,
  tasksCache: [],
  settings: {},
};

// ================================================================ 通用选择器组件

/** 大类选择：返回 <select> 的 HTML */
function catSelect(id, selected, includeArchived) {
  const cats = (S.boot.categories || []).filter(c => includeArchived || !c.archived);
  return '<select id="' + id + '">' +
    '<option value="">— 请选择大类 —</option>' +
    cats.map(c => '<option value="' + c.id + '"' + (String(selected) === String(c.id) ? ' selected' : '') +
      '>' + esc(c.name) + (c.counts_work ? '（计入工作时间）' : '') + '</option>').join('') +
    '</select>';
}

/** 二级任务选择：可按大类过滤；带「＋ 新建一个任务…」选项 */
function taskSelect(id, catId, selected) {
  const tasks = (S.boot.tasks || []).filter(t => !t.archived && (!catId || String(t.category_id) === String(catId)));
  let html = '<select id="' + id + '">' +
    '<option value="">— 请选择二级任务 —</option>' +
    tasks.map(t => '<option value="' + esc(t.name) + '"' + (selected === t.name ? ' selected' : '') +
      '>' + esc(t.name) + '</option>').join('') +
    '<option value="__new__">＋ 新建一个任务…</option>' +
    '</select>';
  // 下拉框 + 「＋新增」按钮（两者都能新建任务，用户 2026-10-12 确认）
  return '<div class="pick-row">' + html +
    '<button type="button" class="btn small nowrap" data-newtask-for="' + id + '">＋新增</button></div>';
}

/**
 * 新增二级任务的统一弹窗（用户 2026-10-12 确认：首页、记录页、设置页三处都要有）。
 * 重名时不建重复任务，直接提示"已经存在"并选中已有那条。
 */
function openNewTaskModal(opts) {
  opts = opts || {};
  const cats = (S.boot.categories || []).filter(c => !c.archived);
  if (!cats.length) { toast('还没有可选的大类。', 'bad'); return; }
  let preCat = opts.categoryId ? String(opts.categoryId) : '';
  if (!preCat || !cats.some(c => String(c.id) === preCat)) preCat = String(cats[0].id);

  const html =
    '<div class="inline">' +
    '<label class="f"><span>属于哪个大类 <span class="req">*</span></span>' +
    '<select id="ntm-cat">' + cats.map(c =>
      '<option value="' + c.id + '"' + (String(c.id) === preCat ? ' selected' : '') + '>' +
      esc(c.name) + '</option>').join('') + '</select></label>' +
    '<label class="f"><span>新二级任务的名字 <span class="req">*</span></span>' +
    '<input type="text" id="ntm-name" placeholder="例如：升力计算" value="' +
    esc(opts.taskName || '') + '"></label>' +
    '</div>' +
    '<div class="hint">任务名会原样保存，不做任何清洗。以后可以在「设置 → 二级任务管理」里改名、合并或归档。</div>' +
    '<div class="hint" id="ntm-msg"></div>';

  const bg = modal('新增二级任务', html, [
    {
      label: '确定新增', cls: 'primary', onClick: async () => {
        // 一律在弹窗内部查找元素，避免和页面上同名的静态元素串味
        const name = $('#ntm-name', bg).value.trim();
        if (!name) { $('#ntm-msg', bg).textContent = '请填写任务名字。'; return false; }
        const cid = Number($('#ntm-cat', bg).value);
        $('#ntm-msg', bg).textContent = '正在保存……';
        const r = await post('/api/task/add', { category_id: cid, name: name });
        if (!r || !r.ok) { $('#ntm-msg', bg).textContent = (r && r.error) || '保存失败。'; return false; }
        // 【顺序很重要】先让调用方把"选中这个新任务"写进表单状态，再 refreshMeta()。
        // refreshMeta() 会按表单状态重渲染录入表单，顺序反了新任务就会丢掉选中。
        if (opts.onDone) opts.onDone(name, cid);
        await refreshMeta();
        toast(r.message || ('已新增任务「' + name + '」'), r.created ? 'ok' : 'info');
        return true;
      }
    },
    { label: '取消' }]);
  // 打开就聚焦、回车即提交
  const inp = $('#ntm-name', bg);
  setTimeout(() => inp.focus(), 30);
  inp.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('.foot .btn.primary', bg).click(); }
  });
  return bg;
}

function tagChips(id, selected, allTags) {
  const sel = selected || [];
  const pool = Array.from(new Set((allTags || []).concat(sel)));
  return '<div id="' + id + '" data-sel="' + esc(sel.join(',')) + '">' +
    pool.map(t => '<span class="tagchip' + (sel.indexOf(t) >= 0 ? ' on' : '') +
      '" data-tag="' + esc(t) + '">' + esc(t) + '</span>').join('') +
    '<span class="tagchip" data-newtag="1">＋ 新建标签</span></div>';
}

function bindTagChips(id) {
  const host = document.getElementById(id);
  if (!host) return;
  host.addEventListener('click', (e) => {
    const chip = e.target.closest('.tagchip');
    if (!chip) return;
    if (chip.dataset.newtag) {
      const name = prompt('新标签的名字（例如：TGV项目、论文写作）：');
      if (!name || !name.trim()) return;
      const nm = name.trim();
      if (!Array.from(host.querySelectorAll('.tagchip[data-tag]')).some(c => c.dataset.tag === nm)) {
        const span = document.createElement('span');
        span.className = 'tagchip';
        span.dataset.tag = nm;
        span.textContent = nm;
        host.insertBefore(span, chip);
      }
      const t = Array.from(host.querySelectorAll('.tagchip[data-tag]')).find(c => c.dataset.tag === nm);
      t.classList.add('on');
      return;
    }
    chip.classList.toggle('on');
  });
}
function getSelectedTags(id) {
  const host = document.getElementById(id);
  if (!host) return [];
  return Array.from(host.querySelectorAll('.tagchip.on[data-tag]')).map(c => c.dataset.tag);
}

/**
 * 绑好「二级任务」下拉框：
 *  1）选中「＋ 新建一个任务…」时，弹出新增任务窗口；
 *  2）每个下拉框旁边的「＋新增」按钮也走同一个窗口；
 *  3）重名由后端判断，返回"已经存在"时直接选中已存在的那条任务，不建重复任务。
 * catSelectId 传大类下拉框的 id，用来知道这个任务应该属于哪个大类。
 */
function bindTaskSelect(id, catSelectId) {
  const sel = document.getElementById(id);
  if (!sel) return;
  const host = sel.parentElement;                       // .pick-row

  const currentCatId = () =>
    catSelectId && document.getElementById(catSelectId) ? document.getElementById(catSelectId).value : '';

  const onCreated = (name) => {
    // 【关键】在重渲染之前先把"选中这个新任务"写进表单状态。
    // 因为 refreshMeta() 会走 fillEverywhere() -> renderStartArea() 重建整个表单，
    // 重建时是按状态值来设 selected 的；如果等重渲染之后再设，设的就是已经被丢弃的旧元素，
    // 新任务就"看起来没加上"（这正是原来那个 bug 的根因）。
    if (id === 'start-task') S.recordForm.startTask = name;
    if (id === 'mf-task') S.manual.task = name;
    if (id === 'pf-task') S.planForm.task = name;
    // 兜底：控件若还在（没被重渲染掉），直接把值设上
    const s = document.getElementById(id);
    if (s) {
      if (!Array.from(s.options).some(o => o.value === name)) {
        const opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        s.insertBefore(opt, s.lastElementChild);
      }
      s.value = name;
    }
  };

  sel.addEventListener('change', () => {
    if (sel.value === '__new__') {
      sel.value = '';                                   // 先退回去，等窗口里填
      openNewTaskModal({ categoryId: currentCatId(), onDone: onCreated });
    }
  });
  if (host) {
    const btn = host.querySelector('[data-newtask-for="' + id + '"]');
    if (btn) btn.addEventListener('click', () =>
      openNewTaskModal({ categoryId: currentCatId(), onDone: onCreated }));
  }
}

// ================================================================ 弹窗

function modal(title, bodyHtml, buttons) {
  const host = $('#modal-host');
  const bg = document.createElement('div');
  bg.className = 'modal-bg';
  bg.innerHTML = '<div class="modal"><h3>' + esc(title) + '</h3><div class="mbody">' +
    bodyHtml + '</div><div class="foot"></div></div>';
  const foot = $('.foot', bg);
  (buttons || [{ label: '关闭' }]).forEach(b => {
    const btn = document.createElement('button');
    btn.className = 'btn ' + (b.cls || '');
    btn.textContent = b.label;
    btn.onclick = async () => {
      if (b.onClick) {
        const keep = await b.onClick(bg);
        if (keep === false) return;
      }
      bg.remove();
    };
    foot.appendChild(btn);
  });
  host.appendChild(bg);
  return bg;
}
function closeModal(bg) { if (bg) bg.remove(); }

// ================================================================ 元数据刷新

async function refreshMeta() {
  const d = await api('/api/bootstrap');
  if (!d || d.ok === false) return;
  S.boot = d;
  S.timers = d.timers || [];
  S.tickBase = Date.now();
  S.timersSynced = false;
  S.settings = d.settings || {};
  S.tasksCache = d.tasks || [];
  applyTheme();
  fillEverywhere();
}

function applyTheme() {
  document.body.classList.toggle('light', S.settings.theme === 'light');
  $('#btn-theme').textContent = S.settings.theme === 'light' ? '浅色' : '深色';
  document.body.classList.toggle('compact', String(S.settings.compact) === '1');
  $('#btn-compact').textContent = String(S.settings.compact) === '1' ? '紧凑✓' : '紧凑';
}

// ================================================================ 首页

function renderHero() {
  const d = S.boot;
  if (!d) return;
  $('#hero-work').textContent = d.work_hm;
  const rec = d.day.record_count;
  $('#hero-meta').textContent = d.today + ' ' + d.weekday + ' · 今天 ' + rec + ' 条记录 · ' +
    '各类别合计（不去重）' + fmtHM(d.day.total_minutes || 0) + ' · ' +
    '投入时长之和 ' + d.invested_hm;
  $('#today-count').textContent = rec + ' 条';
}

function renderNotices() {
  const d = S.boot, host = $('#home-notices');
  if (!d) return;
  const out = [];

  if (d.reports_generated && d.reports_generated.length) {
    out.push('<div class="notice ok">📄 ' + esc(d.reports_message) +
      ' <button class="btn small act" data-goto="summary">去看看</button></div>');
  }
  const br = d.blank_recall || { days: [] };
  if (br.today_blank) {
    out.push('<div class="notice warn">今天还没有记录，现在开始记一笔吧。' +
      ' <button class="btn small act" data-quick="today">去补录今天</button></div>');
  }
  if (br.days && br.days.length) {
    out.push('<div class="notice warn">最近 ' + br.window + ' 天里有 ' + br.days.length +
      ' 天没有记录：' + br.days.map(x => '<a href="#" data-blank="' + x + '">' + x + '</a>').join('、') +
      ' <span class="hint">（点日期直接去补录）</span></div>');
  }
  if (d.goals_set && d.progress && d.progress.today) {
    const lag = d.progress.today.filter(x => !x.ok);
    if (lag.length) {
      out.push('<div class="notice">今天还差：' + lag.map(x =>
        esc(x.category_name) + ' ' + x.remaining_hm + '（已 ' + x.got_hm + ' / 目标 ' + x.goal_hm + '）'
      ).join('；') + '</div>');
    } else {
      out.push('<div class="notice ok">今天的目标已经全部达成，保持住！</div>');
    }
  } else if (!d.goals_set) {
    out.push('<div class="notice">你还没设置目标。' +
      ' <button class="btn small act" data-goto="settings">去设置目标</button>' +
      ' <span class="hint">不设目标也能正常记账，只是没有达成率和连续达标天数。</span></div>');
  }
  if (d.cross_midnight_split && d.cross_midnight_split.length) {
    out.push('<div class="notice ok">检测到跨午夜的计时，已自动切开：' +
      d.cross_midnight_split.map(x => esc(x.day) + ' ' + fmtHM(x.minutes)).join('、') + '</div>');
  }
  host.innerHTML = out.join('');
  host.querySelectorAll('[data-blank]').forEach(a => a.onclick = (e) => {
    e.preventDefault();
    goto('records');
    S.manual.day = a.dataset.blank;
    renderManualForm();
  });
  host.querySelectorAll('[data-goto]').forEach(b => b.onclick = () => goto(b.dataset.goto));
  host.querySelectorAll('[data-quick]').forEach(b => b.onclick = () => {
    goto('records'); S.manual.day = todayStr(); renderManualForm();
  });
}

function renderTimers() {
  const host = $('#timer-area');
  const list = S.timers || [];
  if (!list.length) {
    host.innerHTML = '<div class="hint">现在没有在计时。选好大类、二级任务、标签就能开始。</div>';
    return;
  }
  host.innerHTML = '<div class="timers">' + list.map(t =>
    '<div class="timer ' + t.status + '" data-tid="' + t.id + '">' +
    '<div class="tname">' + esc(t.task_name) + '</div>' +
    '<div class="tcat">' + esc(t.category_name) + (t.tags.length ? ' · ' + t.tags.map(x => '#' + esc(x)).join(' ') : '') +
    (t.cross_midnight ? ' · <span class="badge warn">已跨午夜</span>' : '') + '</div>' +
    '<div class="clock" data-clock="' + t.id + '">' + clock(t.elapsed_sec) + '</div>' +
    '<div class="tbtns">' +
    (t.status === 'running'
      ? '<button class="btn small" data-act="pause">暂停</button>'
      : '<button class="btn small primary" data-act="resume">继续</button>') +
    '<button class="btn small primary" data-act="stop">停止并保存</button>' +
    '<button class="btn small danger" data-act="cancel">放弃</button>' +
    '</div></div>').join('') + '</div>';
  host.querySelectorAll('[data-act]').forEach(b => b.onclick = async () => {
    const tid = Number(b.closest('.timer').dataset.tid);
    const act = b.dataset.act;
    if (act === 'cancel' && !confirm('放弃这段计时？不会生成记录。')) return;
    const map = { pause: '/api/timer/pause', resume: '/api/timer/resume', stop: '/api/timer/stop', cancel: '/api/timer/cancel' };
    const r = await post(map[act], { timer_id: tid });
    if (r && r.ok) {
      if (r.message) toast(r.message, 'ok');
      S.timers = r.timers || [];
      S.tickBase = Date.now();
      if (act === 'stop' || act === 'cancel') await reloadAll();
    }
  });
}

function renderStartArea() {
  const host = $('#start-area');
  const combo = S.boot && S.boot.last_combo;
  const f = S.recordForm = S.recordForm || {};
  if (!f.startCat) f.startCat = combo ? combo.category_id : '';
  if (!f.startTask) f.startTask = combo ? combo.task_name : '';
  if (!f.startTags) f.startTags = combo ? combo.tags.slice() : [];
  // 大类被删掉或归档时，把它从表单状态里清掉，避免下拉框和状态不一致
  if (f.startCat && !(S.boot.categories || []).some(c => String(c.id) === String(f.startCat))) {
    f.startCat = '';
    f.startTask = '';
  }

  host.innerHTML =
    '<div class="inline">' +
    '<label class="f"><span>大类 <span class="req">*</span></span>' + catSelect('start-cat', f.startCat) + '</label>' +
    '<label class="f"><span>二级任务 <span class="req">*</span></span>' + taskSelect('start-task', f.startCat, f.startTask) + '</label>' +
    '</div>' +
    '<label class="f"><span>标签 <span class="req">*</span>（点一下选中，可以多选）</span>' +
    tagChips('start-tags', f.startTags, (S.boot.tags || []).map(t => t.name)) + '</label>' +
    '<div style="display:flex;gap:8px;flex-wrap:wrap">' +
    '<button class="btn primary" id="btn-start">▶ 开始计时</button>' +
    (combo ? '<button class="btn" id="btn-quick">⚡ 快速开始（' + esc(combo.category_name) + ' / ' +
      esc(combo.task_name) + '）</button>' : '') +
    '</div>' +
    '<div class="hint mt8">可以同时开多个计时（比如「跑步」和「听书」并行）；关掉网页也不影响计时，' +
    '下次打开还在走。</div>';

  $('#start-cat').onchange = () => {
    f.startCat = $('#start-cat').value;
    f.startTask = '';
    renderStartArea();
  };
  bindTaskSelect('start-task');
  bindTagChips('start-tags');

  $('#btn-start').onclick = async () => {
    const catId = $('#start-cat').value;
    const taskName = $('#start-task').value;
    const tags = getSelectedTags('start-tags');
    if (!catId) return toast('请先选择大类。', 'bad');
    if (!taskName || taskName === '__new__') return toast('请先选择或新建二级任务。', 'bad');
    if (!tags.length) return toast('标签至少要选一个。', 'bad');
    const r = await post('/api/timer/start', { category_id: Number(catId), task_name: taskName, tags: tags });
    if (r && r.ok) {
      toast('开始计时：' + taskName, 'ok');
      S.timers = r.timers || [];
      S.tickBase = Date.now();
      await refreshMeta();
      renderTimers(); renderStartArea();
    }
  };
  if ($('#btn-quick')) $('#btn-quick').onclick = async () => {
    const c = S.boot.last_combo;
    const r = await post('/api/timer/start', { category_id: c.category_id, task_name: c.task_name, tags: c.tags });
    if (r && r.ok) { toast('快速开始：' + c.task_name, 'ok'); await refreshMeta(); renderTimers(); }
  };
}

function renderTodayGoal() {
  const host = $('#today-goal');
  const d = S.boot;
  if (!d.goals_set) {
    host.innerHTML = '<div class="hint">你还没设置目标。到「设置 → 目标设置」里给大类填每日下限即可。</div>';
    return;
  }
  const items = (d.progress && d.progress.today) || [];
  if (!items.length) { host.innerHTML = '<div class="hint">今天没有需要判定的目标。</div>'; return; }
  const streak = d.streak || {};
  host.innerHTML = items.map(it => {
    const pct = Math.min(100, Math.round(it.got_minutes / Math.max(1, it.got_minutes + it.remaining_minutes) * 100));
    return '<div style="margin-bottom:10px">' +
      '<div style="display:flex;justify-content:space-between">' +
      '<span>' + esc(it.category_name) + ' <span class="hint">' + esc(it.goal_hm) + ' 目标</span></span>' +
      (it.ok ? '<span class="badge ok">已达成 ' + esc(it.got_hm) + '</span>'
        : '<span class="badge warn">还差 ' + esc(it.remaining_hm) + '</span>') + '</div>' +
      '<div class="bar-row"><div class="track"><div class="fill" style="width:' + pct +
      '%;background:' + catColor(it.category_name, []) + '"></div></div>' +
      '<div class="val">' + esc(it.got_hm) + '</div></div></div>';
  }).join('') +
    (streak.has_goal ? '<div class="hint">连续达标：<b class="' + (streak.days > 0 ? 'good' : '') + '">' +
      (streak.days || 0) + '</b> 天' + (streak.from ? '（' + streak.from + ' ~ ' + streak.to + '）' : '') + '</div>' : '');
}

function renderTodayList() {
  const host = $('#today-list');
  const d = S.boot;
  if (!d) return;
  const recs = d.day.records || [];
  if (!recs.length) { host.innerHTML = '<div class="hint">今天还没有记录。</div>'; return; }
  host.innerHTML = '<table><thead><tr><th>大类</th><th>二级任务</th><th>标签</th>' +
    '<th class="num">时长</th><th>来源</th><th>备注</th><th></th></tr></thead><tbody>' +
    recs.map(r => '<tr>' +
      '<td>' + esc(r.category_name) + '</td>' +
      '<td>' + esc(r.task_name) + '</td>' +
      '<td>' + (r.tags ? r.tags.split('\n').map(x => '<span class="tagchip">' + esc(x) + '</span>').join('') : '<span class="hint">—</span>') + '</td>' +
      '<td class="num mono">' + fmtHM(r.duration_min) + '</td>' +
      '<td>' + ({ timer: '计时', manual: '手填', import: '导入' }[r.source] || r.source) + '</td>' +
      '<td>' + esc(r.note || '') + '</td>' +
      '<td class="nowrap"><button class="btn small" data-edit="' + r.id + '">改</button> ' +
      '<button class="btn small danger" data-del="' + r.id + '">删</button></td></tr>').join('') +
    '</tbody></table>';
  host.querySelectorAll('[data-edit]').forEach(b => b.onclick = () => openRecordEdit(Number(b.dataset.edit)));
  host.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
    if (!confirm('删除这条记录？删除后可以在「记录 → 回收站」或「撤销」里找回。')) return;
    const r = await post('/api/record/delete', { id: Number(b.dataset.del) });
    if (r && r.ok) { toast(r.message, 'ok'); await reloadAll(); }
  });
}

// ================================================================ 计时插值（每秒刷新显示）

function tickTimers() {
  const base = S.tickBase || Date.now();
  const passedMs = Date.now() - base;
  $$('[data-clock]').forEach(el => {
    const t = (S.timers || []).find(x => String(x.id) === el.dataset.clock);
    if (!t) return;
    el.textContent = clock(t.elapsed_sec + (t.status === 'running' ? passedMs / 1000 : 0));
  });
  // 每 15 秒跟后端对一次表，让「已计时间」跟后端一致
  if (Date.now() - (S.lastSync || 0) > 15000 && (S.timers || []).length) {
    S.lastSync = Date.now();
    api('/api/timer/status').then(d => {
      if (d && d.ok) {
        S.timers = d.timers || [];
        S.tickBase = Date.now();
        checkOvertime(d.overtime || []);
        if ((S.timers || []).length) renderTimers();
      }
    }).catch(() => { });
  }
  // 跨过 00:00 时自动刷新整页数据（让新的一天正确显示）
  if (S.todayShown && S.todayShown !== todayStr()) location.reload();
}

function checkOvertime(list) {
  if (!list || !list.length) return;
  const key = list.map(x => x.timer_id).join(',');
  if (S.overtimeShown === key) return;
  S.overtimeShown = key;
  const html = list.map(x =>
    '<div class="notice warn" data-ot="' + x.timer_id + '">' +
    '<b>' + esc(x.task_name) + '</b>（' + esc(x.category_name) + '）已经计了 <b>' + x.elapsed_hm +
    '</b>，超过了你设的提醒阈值 ' + x.limit_hm + '。<br>开始时间：' + esc(x.started_at) +
    '<div class="inline mt8">' +
    '<button class="btn small primary" data-otkeep="' + x.timer_id + '">保留到实际时长</button>' +
    '<button class="btn small" data-ottrunc="' + x.timer_id + '">截断到某时刻</button>' +
    '<button class="btn small danger" data-otdel="' + x.timer_id + '">删除这段</button>' +
    '</div><div class="hint">建议截断到你可能真正停下来的时间（比如 ' + esc(x.suggest_end) + '）</div></div>'
  ).join('');

  const bg = modal('有计时忘了停', html +
    '<div class="hint">这只是提醒，不会自动改动你的数据。</div>',
    [{ label: '稍后处理' }]);
  $$('[data-otkeep]', bg).forEach(b => b.onclick = async () => {
    const r = await post('/api/timer/overtime', { timer_id: Number(b.dataset.otkeep), action: 'keep' });
    if (r && r.ok) { toast(r.message, 'ok'); closeModal(bg); S.overtimeShown = null; await reloadAll(); }
  });
  $$('[data-otdel]', bg).forEach(b => b.onclick = async () => {
    if (!confirm('删除这段计时？不会生成记录。')) return;
    const r = await post('/api/timer/overtime', { timer_id: Number(b.dataset.otdel), action: 'delete' });
    if (r && r.ok) { toast(r.message, 'ok'); closeModal(bg); S.overtimeShown = null; await reloadAll(); }
  });
  $$('[data-ottrunc]', bg).forEach(b => b.onclick = () => {
    const tid = Number(b.dataset.ottrunc);
    const row = list.find(x => x.timer_id === tid);
    const val = prompt('截断到哪个时间（格式 2025-08-21 11:00）：', row ? row.suggest_end : '');
    if (!val) return;
    post('/api/timer/overtime', { timer_id: tid, action: 'truncate', end_ts: val }).then(async r => {
      if (r && r.ok) { toast(r.message, 'ok'); closeModal(bg); S.overtimeShown = null; await reloadAll(); }
    });
  });
}

// ================================================================ 记录页

function renderManualForm() {
  const f = S.manual;
  if (!f.day) f.day = todayStr();
  if (!f.tags) f.tags = [];
  // 大类被删或归档时清掉，避免下拉框与表单状态打架
  if (f.cat && !(S.boot.categories || []).some(c => String(c.id) === String(f.cat))) {
    f.cat = '';
    f.task = '';
  }
  const host = $('#manual-form');
  host.innerHTML =
    '<div class="inline">' +
    '<label class="f" style="flex:0 0 160px"><span>日期 <span class="req">*</span></span>' +
    '<input type="date" id="mf-day" value="' + f.day + '" max="' + todayStr() + '"></label>' +
    '<label class="f"><span>大类 <span class="req">*</span></span>' + catSelect('mf-cat', f.cat) + '</label>' +
    '<label class="f"><span>二级任务 <span class="req">*</span></span>' + taskSelect('mf-task', f.cat, f.task) + '</label>' +
    '<label class="f" style="flex:0 0 140px"><span>时长（如 3:30）</span>' +
    '<input type="text" id="mf-dur" placeholder="3:30" value="' + esc(f.dur || '') + '"></label>' +
    '</div>' +
    '<div class="inline">' +
    '<label class="f" style="flex:0 0 150px"><span>起始时间（可空）</span>' +
    '<input type="text" id="mf-start" placeholder="9:30" value="' + esc(f.start || '') + '"></label>' +
    '<label class="f" style="flex:0 0 150px"><span>终止时间（可空）</span>' +
    '<input type="text" id="mf-end" placeholder="11:00" value="' + esc(f.end || '') + '"></label>' +
    '<div style="flex:1 1 260px" class="hint" id="mf-tip">' +
    '时长和时间段<b>至少填一种</b>：只填时长就是一条普通记录；填了起始+终止，时长会自动算出来。' +
    '两个都填时会核对是否一致，对不上不让保存。时间段必须在所选日期当天 00:00~23:59 之内。' +
    '</div>' +
    '</div>' +
    '<label class="f"><span>标签 <span class="req">*</span></span>' +
    tagChips('mf-tags', f.tags, (S.boot.tags || []).map(t => t.name)) + '</label>' +
    '<div class="inline">' +
    '<label class="f"><span>备注（可空）</span><input type="text" id="mf-note" value="' + esc(f.note || '') + '" placeholder="例如：今天卡在网格质量上"></label>' +
    '</div>' +
    '<div class="inline">' +
    '<label class="f" style="flex:0 0 130px"><span>产出物类型</span>' +
    '<select id="mf-ptype">' + (S.boot.product_types || []).map(p =>
      '<option>' + esc(p) + '</option>').join('') + '</select></label>' +
    '<label class="f"><span>产出物名称（可空）</span><input type="text" id="mf-pname" placeholder="例如：升力系数对比图"></label>' +
    '<label class="f"><span>链接（可空）</span><input type="text" id="mf-purl" placeholder="本地路径或网址"></label>' +
    '</div>' +
    '<div style="display:flex;gap:8px"><button class="btn primary" id="mf-save">保存这条补录</button>' +
    '<button class="btn" id="mf-clear">清空</button></div>';

  $('#mf-day').onchange = () => { f.day = $('#mf-day').value; };
  $('#mf-cat').onchange = () => { f.cat = $('#mf-cat').value; f.task = ''; renderManualForm(); };
  $('#mf-task').onchange = () => { f.task = $('#mf-task').value; };
  // 起始 + 终止都填了 -> 自动把时长算出来填进时长框（只做"填"，保存时后端还会再核对一次）
  const syncDurationFromTimes = () => {
    const s = ($('#mf-start').value || '').trim().replace('：', ':');
    const e = ($('#mf-end').value || '').trim().replace('：', ':');
    const tip = $('#mf-tip');
    f.start = s; f.end = e;
    if (!s || !e) {
      if (tip) tip.className = 'hint';
      return;
    }
    const parse = (t) => {
      const m = /^(\d{1,2}):([0-5]?\d)(?::[0-5]?\d)?$/.exec(t);
      if (!m) return null;
      const h = Number(m[1]);
      if (h > 23) return null;
      return h * 60 + Number(m[2]);
    };
    const a = parse(s), b = parse(e);
    if (a === null || b === null) {
      if (tip) { tip.className = 'hint badcake'; tip.textContent = '时间写法看不懂，请用 24 小时制，例如 9:30 或 14:05。'; }
      return;
    }
    if (b <= a) {
      if (tip) { tip.className = 'hint badcake'; tip.textContent = '终止时间必须晚于起始时间。'; }
      return;
    }
    const mins = b - a;
    $('#mf-dur').value = fmtHM(mins);
    f.dur = fmtHM(mins);
    if (tip) {
      tip.className = 'hint good';
      tip.textContent = '时间段 ' + s + '~' + e + ' 共 ' + fmtHM(mins) + '，已自动填进时长框（保存时会再核对一次）。';
    }
  };
  $('#mf-start').oninput = syncDurationFromTimes;
  $('#mf-end').oninput = syncDurationFromTimes;
  bindTaskSelect('mf-task', 'mf-cat');
  bindTagChips('mf-tags');
  $('#mf-clear').onclick = () => { S.manual = { day: f.day }; renderManualForm(); };
  $('#mf-save').onclick = async () => {
    const products = [];
    const pname = $('#mf-pname').value.trim();
    if (pname) products.push({ ptype: $('#mf-ptype').value, name: pname, url: $('#mf-purl').value.trim() });
    const body = {
      day: $('#mf-day').value,
      category_id: $('#mf-cat').value ? Number($('#mf-cat').value) : null,
      task_name: $('#mf-task').value === '__new__' ? '' : $('#mf-task').value,
      tags: getSelectedTags('mf-tags'),
      duration: $('#mf-dur').value,
      start_time: $('#mf-start').value,
      end_time: $('#mf-end').value,
      note: $('#mf-note').value,
      products: products,
    };
    const r = await post('/api/record/add', body);
    if (r && r.ok) {
      toast(r.message, 'ok');
      S.manual = { day: body.day, cat: body.category_id, task: body.task_name, tags: body.tags };
      await reloadAll();
      renderManualForm();
      loadCalendar();
    }
  };
}

async function loadCalendar(useAll) {
  let start = $('#rec-start').value, end = $('#rec-end').value;
  if (useAll) {
    const b = (S.boot && S.boot.data_bounds) || {};
    start = b.first || todayStr(); end = b.last || todayStr();
    $('#rec-start').value = start; $('#rec-end').value = end;
  }
  if (!start) { start = todayStr(); $('#rec-start').value = start; }
  if (!end) { end = todayStr(); $('#rec-end').value = end; }
  const d = await api('/api/calendar?start=' + start + '&end=' + end);
  if (!d || !d.ok) return;
  const days = d.days || [];
  const withRec = days.filter(x => x.has_records).length;
  const blanks = days.filter(x => !x.has_records);
  $('#rec-summary').innerHTML =
    '<div>区间 <b>' + start + ' ~ ' + end + '</b></div>' +
    '<div>有记录 <b>' + withRec + '</b> 天</div>' +
    '<div>未记录 <b class="warnc">' + blanks.length + '</b> 天</div>' +
    '<div>记录率 <b>' + (days.length ? (withRec / days.length * 100).toFixed(1) : '0') + '%</b></div>';
  $('#rec-days').innerHTML = '<table><thead><tr><th>日期</th><th>星期</th><th>状态</th>' +
    '<th class="num">实际(去重)</th><th class="num">工作(科研+会议)</th><th class="num">条数</th>' +
    '<th>各大类</th><th></th></tr></thead><tbody>' +
    days.map(x => '<tr class="' + (x.has_records ? '' : 'blank') + '">' +
      '<td class="mono">' + x.day + '</td><td>' + x.weekday + '</td>' +
      '<td>' + (x.has_records ? (x.goal && x.goal.has_goal
        ? (x.goal.achieved ? '<span class="badge ok">达标</span>' : '<span class="badge bad">未达标</span>')
        : '<span class="badge">已记录</span>')
        : '<span class="badge warn">未记录</span>') + '</td>' +
      '<td class="num mono">' + fmtHM(x.actual_minutes) + '</td>' +
      '<td class="num mono">' + fmtHM(x.work_minutes) + '</td>' +
      '<td class="num">' + x.record_count + '</td>' +
      '<td>' + Object.entries(x.category_minutes || {}).map(([k, v]) =>
        '<span class="tagchip" style="border-color:' + catColor(k, []) + '">' + esc(k) + ' ' + fmtHM(v) + '</span>').join('') + '</td>' +
      '<td class="nowrap"><button class="btn small" data-day="' + x.day + '">查看/补录</button></td></tr>').join('') +
    '</tbody></table>';
  $$('[data-day]').forEach(b => b.onclick = () => openDay(b.dataset.day));

  const del = await api('/api/deleted');
  if (del && del.ok) {
    $('#rec-deleted').innerHTML = (del.records || []).length
      ? '<table><thead><tr><th>删除时间</th><th>日期</th><th>大类</th><th>任务</th>' +
        '<th class="num">时长</th><th></th></tr></thead><tbody>' +
        del.records.map(r => '<tr><td class="mono">' + esc(r.deleted_at) + '</td>' +
          '<td class="mono">' + r.day + '</td><td>' + esc(r.category_name) + '</td>' +
          '<td>' + esc(r.task_name) + '</td><td class="num mono">' + r.hm + '</td>' +
          '<td><button class="btn small" data-restore="' + r.id + '">恢复</button></td></tr>').join('') +
        '</tbody></table>'
      : '<div class="hint">回收站是空的。</div>';
    $$('[data-restore]').forEach(b => b.onclick = async () => {
      const r = await post('/api/record/restore', { id: Number(b.dataset.restore) });
      if (r && r.ok) { toast(r.message, 'ok'); await reloadAll(); loadCalendar(); }
    });
  }
}

async function openDay(day) {
  const d = await api('/api/day?day=' + day);
  if (!d || !d.ok) return;
  const dd = d.day;
  const body = '<div class="statline" style="margin-bottom:8px">' +
    '<div>日期 <b>' + dd.date_cn + '</b></div>' +
    '<div>实际（去重）<b>' + fmtHM(dd.actual_minutes) + '</b></div>' +
    '<div>工作（科研+会议）<b>' + fmtHM(dd.work_minutes) + '</b></div>' +
    '<div>投入之和 <b>' + fmtHM(dd.invested_minutes) + '</b></div>' +
    '<div>条数 <b>' + dd.record_count + '</b></div></div>' +
    '<div id="md-timeline"></div>' +
    (dd.records.length ? '<div class="table-wrap"><table><thead><tr><th>大类</th><th>任务</th>' +
      '<th>标签</th><th class="num">时长</th><th>起止</th><th>来源</th><th></th></tr></thead><tbody>' +
      dd.records.map(r => '<tr><td>' + esc(r.category_name) + '</td><td>' + esc(r.task_name) + '</td>' +
        '<td>' + (r.tags || '').split('\n').filter(Boolean).map(x => '<span class="tagchip">' + esc(x) + '</span>').join('') + '</td>' +
        '<td class="num mono">' + fmtHM(r.duration_min) + '</td>' +
        '<td class="mono hint">' + (r.start_ts ? esc(r.start_ts.slice(11, 16)) + '~' + esc(r.end_ts.slice(11, 16)) : '—') + '</td>' +
        '<td>' + ({ timer: '计时', manual: '手填', import: '导入' }[r.source] || r.source) + '</td>' +
        '<td class="nowrap"><button class="btn small" data-edit="' + r.id + '">改</button> ' +
        '<button class="btn small danger" data-del="' + r.id + '">删</button></td></tr>').join('') +
      '</tbody></table></div>' : '<div class="hint">这一天还没有记录。</div>');
  const bg = modal(day + ' 的详情', body, [
    { label: '去补录这一天', cls: 'primary', onClick: () => { goto('records'); S.manual.day = day; renderManualForm(); } },
    { label: '关闭' }]);
  $('#md-timeline', bg).innerHTML = timelineSvg(dd.records);
  $$('[data-edit]', bg).forEach(b => b.onclick = () => { closeModal(bg); openRecordEdit(Number(b.dataset.edit)); });
  $$('[data-del]', bg).forEach(b => b.onclick = async () => {
    if (!confirm('删除这条记录？')) return;
    const r = await post('/api/record/delete', { id: Number(b.dataset.del) });
    if (r && r.ok) { toast(r.message, 'ok'); closeModal(bg); await reloadAll(); loadCalendar(); }
  });
}

/** 一天时间轴 / 甘特图（用户确认要做的时段视图） */
function timelineSvg(records) {
  const timed = (records || []).filter(r => r.source === 'timer' && r.start_ts && r.end_ts);
  if (!timed.length) return '<div class="hint">这一天没有带起止时间的计时段，时间轴为空。</div>';
  const W = 900, LAB = 62, H = 30;
  const rows = [];
  timed.forEach(r => {
    const s = new Date(r.start_ts.replace(' ', 'T'));
    const e = new Date(r.end_ts.replace(' ', 'T'));
    rows.push({ s: s, e: e, r: r });
  });
  const minT = new Date(Math.min(...rows.map(x => x.s.getTime())));
  minT.setHours(0, 0, 0, 0);
  const maxT = new Date(minT.getTime() + 24 * 3600 * 1000);
  const x = (d) => LAB + (d.getTime() - minT.getTime()) / (maxT.getTime() - minT.getTime()) * (W - LAB);
  let out = '<div class="timeline"><svg viewBox="0 0 ' + W + ' ' + (rows.length * H + 26) +
    '" width="100%" height="' + (rows.length * H + 26) + '">';
  for (let hh = 0; hh <= 24; hh += 2) {
    const px = LAB + hh / 24 * (W - LAB);
    out += '<line x1="' + px + '" y1="0" x2="' + px + '" y2="' + (rows.length * H) +
      '" stroke="var(--line)" stroke-dasharray="2 3"/>' +
      '<text x="' + px + '" y="' + (rows.length * H + 14) + '" font-size="10" fill="var(--text3)" text-anchor="middle">' +
      String(hh).padStart(2, '0') + ':00</text>';
  }
  rows.forEach((row, i) => {
    const y = i * H + 4;
    const x0 = Math.max(LAB, x(row.s)), x1 = Math.min(W, x(row.e));
    const w = Math.max(3, x1 - x0);
    const color = catColor(row.r.category_name, []);
    out += '<rect x="' + x0 + '" y="' + y + '" width="' + w + '" height="20" rx="4" fill="' +
      color + '" opacity="0.9"/>' +
      '<text x="' + LAB + '" y="' + (y + 14) + '" font-size="10" fill="var(--text2)" text-anchor="end" dx="-4">' +
      esc(row.r.task_name.slice(0, 6)) + '</text>' +
      '<text x="' + (x0 + 4) + '" y="' + (y + 14) + '" font-size="10" fill="#fff">' +
      esc(row.r.category_name + ' ' + fmtHM(row.r.duration_min)) + '</text>';
  });
  out += '</svg></div>';
  return out;
}

function openRecordEdit(id) {
  api('/api/records?start=1970-01-01&end=2999-12-31').then(async d => {
    const r = ((d && d.records) || []).find(x => x.id === id);
    if (!r) return toast('找不到这条记录。', 'bad');
    // 只要有起止时间就能改（计时产生的、以及补录时填了时间段的，都有起止时间；
    // 补录时只填了时长的那种没有起止时间，不能事后加）
    const isTimer = !!(r.start_ts && r.end_ts);
    // 弹窗里的 id 统一带 ed- 前缀，并且查询时一律限定在弹窗内部，避免和页面上的元素撞名
    const html =
      '<div class="inline">' +
      '<label class="f" style="flex:0 0 160px"><span>日期</span><input type="date" id="ed-day" value="' + r.day + '"></label>' +
      '<label class="f"><span>大类</span>' + catSelect('ed-cat', r.category_id, true) + '</label>' +
      '<label class="f"><span>二级任务</span>' + taskSelect('ed-task', r.category_id, r.task_name) + '</label>' +
      '<label class="f" style="flex:0 0 130px"><span>时长</span><input type="text" id="ed-dur" value="' + fmtHM(r.duration_min) + '"></label>' +
      '</div>' +
      '<label class="f"><span>标签</span>' + tagChips('ed-tags', r.tag_list, (S.boot.tags || []).map(t => t.name)) + '</label>' +
      '<label class="f"><span>备注</span><input type="text" id="ed-note" value="' + esc(r.note || '') + '"></label>' +
      (isTimer ? '<div class="inline"><label class="f"><span>开始时间</span>' +
        '<input type="text" id="ed-start" value="' + r.start_ts + '"></label>' +
        '<label class="f"><span>结束时间</span><input type="text" id="ed-end" value="' + r.end_ts + '"></label>' +
        '</div><div class="hint">改完起止时间会自动重算时长。</div>'
        : '<div class="hint">这条记录没有时间段（补录时只填了时长）。要加时间段的话，' +
          '请删掉这条、重新补录并在补录时填写起始时间和终止时间。</div>') +
      (r.source === 'import' ? '<div class="hint">这是从原表导入的记录，本来就没有标签；现在补上也行。</div>' : '');
    const bg = modal('修改记录 #' + id, html, [
      {
        label: '保存', cls: 'primary', onClick: async () => {
          const body = {
            id: id, day: $('#ed-day', bg).value,
            category_id: Number($('#ed-cat', bg).value),
            task_name: $('#ed-task', bg).value,
            tags: getSelectedTags('ed-tags'), note: $('#ed-note', bg).value,
          };
          const dur = $('#ed-dur', bg).value;
          if (dur) body.duration_min = dur;
          let r1 = await post('/api/record/update', body);
          if (!r1 || !r1.ok) return false;
          if (isTimer) {
            const s = $('#ed-start', bg).value, e = $('#ed-end', bg).value;
            if (s !== r.start_ts || e !== r.end_ts) {
              const r2 = await post('/api/record/times', { id: id, start_ts: s, end_ts: e });
              if (!r2 || !r2.ok) return false;
            }
          }
          toast('已保存修改。', 'ok');
          await reloadAll(); loadCalendar();
          return true;
        }
      },
      { label: '取消' }]);
    bindTagChips('ed-tags');
    bindTaskSelect('ed-task', 'ed-cat');
  });
}

// ================================================================ 统计页

async function loadStats(kind, s, e) {
  let url;
  if (kind === 'custom') url = '/api/range?kind=custom&start=' + s + '&end=' + e;
  else url = '/api/range?kind=' + kind + '&day=' + (kind === 'today' ? todayStr() : todayStr());
  const d = await api(url);
  if (!d || !d.ok) return;
  let r = d.range;
  if (kind === 'today') {
    const dd = await api('/api/day?day=' + todayStr());
    r = {
      start: todayStr(), end: todayStr(), record_days: dd.day.has_records ? 1 : 0,
      natural_days: 1, blank_days: dd.day.has_records ? 0 : 1,
      record_rate: dd.day.has_records ? 1 : 0, total_minutes: dd.day.total_minutes,
      total_h: dd.day.total_minutes / 60, actual_minutes: dd.day.actual_minutes,
      actual_h: dd.day.actual_minutes / 60, work_minutes: dd.day.work_minutes,
      work_h: dd.day.work_minutes / 60, invested_minutes: dd.day.invested_minutes,
      avg_per_record_day_h: dd.day.actual_minutes / 60,
      avg_per_natural_day_h: dd.day.actual_minutes / 60,
      category_minutes: dd.day.category_minutes,
      category_h: {}, category_avg_per_record_day: {},
      days: [dd.day], record_count: dd.day.record_count,
      goal: dd.day.goal, top_tasks: [],
      series: [{ day: todayStr(), weekday: dd.day.weekday, categories: dd.day.category_minutes }],
      plan_vs_actual: { has_plan: false },
    };
  }
  S.statsRange = r;
  $('#st-start').value = r.start; $('#st-end').value = r.end;

  const catNames = Object.keys(r.category_minutes || {});
  $('#st-overview').innerHTML = '<div class="statline">' +
    '<div>区间 <b>' + r.start + ' ~ ' + r.end + '</b></div>' +
    '<div>有记录 <b>' + r.record_days + '</b> / ' + r.natural_days + ' 天</div>' +
    '<div>空白日 <b class="warnc">' + r.blank_days + '</b> 天</div>' +
    '<div>记录率 <b>' + (r.record_rate * 100).toFixed(1) + '%</b></div>' +
    '<div>总时长 <b>' + fmtHM(r.total_minutes) + '</b></div>' +
    '<div>实际(去重) <b>' + fmtHM(r.actual_minutes) + '</b></div>' +
    '<div>工作(科研+会议,去重) <b>' + fmtHM(r.work_minutes) + '</b></div>' +
    '<div>投入之和(不去重) <b>' + fmtHM(r.invested_minutes) + '</b></div>' +
    '<div>平均/记录日 <b>' + r.avg_per_record_day_h.toFixed(3) + 'h</b></div>' +
    '<div>平均/自然日 <b>' + r.avg_per_natural_day_h.toFixed(3) + 'h</b></div>' +
    '</div>' +
    '<div class="mt8">' + catNames.map(c => '<span class="tagchip" style="border-color:' +
      catColor(c, []) + '">' + esc(c) + ' ' + fmtHM(r.category_minutes[c]) +
      '（平均 ' + (r.category_avg_per_record_day[c] || 0).toFixed(2) + 'h/记录日）</span>').join('') + '</div>';

  drawDaily($('#st-daily'), r.series || []);
  drawPie($('#st-pie'), r.category_minutes || {});
  drawTop($('#st-top'), r.top_tasks || []);
  drawMonths($('#st-months'));
  renderStatsGoals(r);
  $('#st-days').innerHTML = '<table><thead><tr><th>日期</th><th>星期</th><th>状态</th>' +
    '<th class="num">实际</th><th class="num">工作</th><th class="num">条数</th><th>各大类</th></tr></thead><tbody>' +
    (r.days || []).slice().reverse().map(x => '<tr class="' + (x.has_records ? '' : 'blank') + '">' +
      '<td class="mono">' + x.day + '</td><td>' + x.weekday + '</td>' +
      '<td>' + (x.has_records ? '<span class="badge ok">已记录</span>' : '<span class="badge warn">未记录</span>') + '</td>' +
      '<td class="num mono">' + fmtHM(x.actual_minutes) + '</td>' +
      '<td class="num mono">' + fmtHM(x.work_minutes) + '</td>' +
      '<td class="num">' + x.record_count + '</td>' +
      '<td>' + Object.entries(x.category_minutes || {}).map(([k, v]) =>
        esc(k) + ' ' + fmtHM(v)).join('、') + '</td></tr>').join('') + '</tbody></table>';
}

function renderStatsGoals(r) {
  const g = r.goal || {};
  const host = $('#st-goals');
  if (!g.has_goal) {
    host.innerHTML = '<div class="hint">你还没设置目标，达成率/连续达标天数暂时不显示。' +
      '到「设置 → 目标设置」里填一个大类每日下限就会出现。</div>';
    return;
  }
  host.innerHTML = '<div class="statline">' +
    '<div>计入判定 <b>' + g.judged_days + '</b> 天</div>' +
    '<div>达标 <b class="good">' + g.achieved_days + '</b> 天</div>' +
    '<div>未达标 <b class="badcake">' + g.fail_days + '</b> 天</div>' +
    '<div>达成率 <b>' + (g.rate * 100).toFixed(1) + '%</b></div>' +
    '<div>连续达标 <b>' + ((g.streak && g.streak.days) || 0) + '</b> 天</div>' +
    '</div>' +
    '<div class="table-wrap mt8"><table><thead><tr><th>日期</th><th>星期</th><th>结果</th>' +
    '<th>明细</th></tr></thead><tbody>' +
    (g.days || []).slice().reverse().map(d => '<tr>' +
      '<td class="mono">' + d.day + '</td><td>' + d.weekday + '</td>' +
      '<td>' + (!d.has_goal ? '<span class="badge">未设目标</span>'
        : (d.achieved ? '<span class="badge ok">达标</span>' : '<span class="badge bad">未达标</span>')) + '</td>' +
      '<td>' + (d.items || []).map(i => esc(i.category_name) + ' ' + i.got_hm + '/' + i.goal_hm +
        '(' + i.goal_type + ')' + (i.ok ? '✓' : ' ✗')).join('；') + '</td></tr>').join('') +
    '</tbody></table></div>';
}

// ---------------- SVG 图表 ----------------

function drawDaily(host, series) {
  if (!series || !series.length) { host.innerHTML = '<div class="hint">没有数据。</div>'; return; }
  const cats = Array.from(new Set(series.flatMap(d => Object.keys(d.categories || {}))));
  const maxV = Math.max(1, ...series.map(d => Object.values(d.categories || {}).reduce((a, b) => a + b, 0)));
  const W = Math.max(660, series.length * 26 + 70), H = 250, L = 56, B = 52;
  const bw = Math.max(6, (W - L - 16) / series.length - 6);
  let out = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '">';
  // y 轴刻度
  for (let i = 0; i <= 4; i++) {
    const v = maxV * i / 4, y = H - B - (H - B - 14) * i / 4;
    out += '<line x1="' + L + '" y1="' + y + '" x2="' + (W - 8) + '" y2="' + y +
      '" stroke="var(--line)" stroke-width="0.6"/>' +
      '<text x="' + (L - 6) + '" y="' + (y + 4) + '" font-size="10" fill="var(--text3)" text-anchor="end">' +
      (v / 60).toFixed(1) + 'h</text>';
  }
  series.forEach((d, i) => {
    const x = L + i * ((W - L - 16) / series.length) + 3;
    let acc = 0;
    const total = Object.values(d.categories || {}).reduce((a, b) => a + b, 0);
    cats.forEach(c => {
      const v = (d.categories || {})[c] || 0;
      if (!v) return;
      const h = (H - B - 14) * v / maxV;
      const y = H - B - (H - B - 14) * (acc + v) / maxV;
      out += '<rect x="' + x + '" y="' + y + '" width="' + bw + '" height="' + h +
        '" fill="' + catColor(c, cats) + '" opacity="0.92"><title>' + d.day + ' ' + esc(c) +
        ' ' + fmtHM(v) + '</title></rect>';
      acc += v;
    });
    if (total > 0) {
      out += '<text x="' + (x + bw / 2) + '" y="' + (H - B - (H - B - 14) * total / maxV - 3) +
        '" font-size="9" fill="var(--text2)" text-anchor="middle">' + (total / 60).toFixed(1) + '</text>';
    }
    out += '<text x="' + (x + bw / 2) + '" y="' + (H - B + 12) + '" font-size="9" fill="var(--text3)" ' +
      'text-anchor="end" transform="rotate(-45 ' + (x + bw / 2) + ' ' + (H - B + 12) + ')">' +
      d.day.slice(5) + '</text>';
  });
  out += '</svg><div class="legend">' + cats.map(c =>
    '<span><i style="background:' + catColor(c, cats) + '"></i>' + esc(c) + '</span>').join('') + '</div>';
  host.innerHTML = out;
}

function drawPie(host, cm) {
  const items = Object.entries(cm || {}).filter(([, v]) => v > 0);
  if (!items.length) { host.innerHTML = '<div class="hint">没有数据。</div>'; return; }
  const total = items.reduce((a, [, v]) => a + v, 0);
  const R = 88, r = 52, cx = 110, cy = 110;
  let ang = -Math.PI / 2, out = '<svg viewBox="0 0 220 220" width="220" height="220">';
  const names = items.map(x => x[0]);
  items.forEach(([name, v]) => {
    const a2 = ang + v / total * Math.PI * 2;
    const large = (a2 - ang) > Math.PI ? 1 : 0;
    const x1 = cx + R * Math.cos(ang), y1 = cy + R * Math.sin(ang);
    const x2 = cx + R * Math.cos(a2), y2 = cy + R * Math.sin(a2);
    const x3 = cx + r * Math.cos(a2), y3 = cy + r * Math.sin(a2);
    const x4 = cx + r * Math.cos(ang), y4 = cy + r * Math.sin(ang);
    out += '<path d="M' + x1 + ' ' + y1 + ' A' + R + ' ' + R + ' 0 ' + large + ' 1 ' + x2 + ' ' + y2 +
      ' L' + x3 + ' ' + y3 + ' A' + r + ' ' + r + ' 0 ' + large + ' 0 ' + x4 + ' ' + y4 + ' Z" fill="' +
      catColor(name, names) + '" opacity="0.92"><title>' + esc(name) + ' ' + fmtHM(v) +
      '（' + (v / total * 100).toFixed(1) + '%）</title></path>';
    ang = a2;
  });
  out += '<text x="' + cx + '" y="' + (cy - 2) + '" text-anchor="middle" font-size="15" fill="var(--text)">' +
    fmtHM(total) + '</text>' +
    '<text x="' + cx + '" y="' + (cy + 15) + '" text-anchor="middle" font-size="10" fill="var(--text3)">合计</text>' +
    '</svg><div class="legend">' + items.map(([n, v]) =>
      '<span><i style="background:' + catColor(n, names) + '"></i>' + esc(n) + ' ' +
      (v / total * 100).toFixed(1) + '%</span>').join('') + '</div>';
  host.innerHTML = out;
}

function drawTop(host, top) {
  if (!top || !top.length) { host.innerHTML = '<div class="hint">没有数据。</div>'; return; }
  const max = Math.max(...top.map(t => t.minutes)) || 1;
  host.innerHTML = '<div class="bars">' + top.map(t => '<div class="bar-row">' +
    '<div class="nm" title="' + esc(t.task_name) + '">' + esc(t.task_name) + '</div>' +
    '<div class="track"><div class="fill" style="width:' + (t.minutes / max * 100).toFixed(1) +
    '%;background:' + catColor(t.category_name, []) + '"></div></div>' +
    '<div class="val">' + fmtHM(t.minutes) + ' ' + (t.share * 100).toFixed(0) + '%</div>' +
    '</div>').join('') + '</div>' +
    '<div class="hint mt8">' + top.map(t => esc(t.task_name) + ' 属于「' + esc(t.category_name) + '」').join('；') + '</div>';
}

async function drawMonths() {
  const d = await api('/api/monthly');
  if (!d || !d.ok) return;
  const ms = (d.monthly && d.monthly.months) || [];
  const host = $('#st-months');
  if (!ms.length) { host.innerHTML = '<div class="hint">还没有数据。</div>'; return; }
  const max = Math.max(...ms.map(m => m.total_h)) || 1;
  host.innerHTML = '<div class="bars">' + ms.map(m => '<div class="bar-row">' +
    '<div class="nm">' + m.month + '</div>' +
    '<div class="track"><div class="fill" style="width:' + (m.total_h / max * 100).toFixed(1) +
    '%;background:var(--accent)"></div></div>' +
    '<div class="val">' + m.total_h.toFixed(2) + 'h</div></div>' +
    '<div class="hint" style="margin:-2px 0 6px 168px">记录 ' + m.record_days + ' 天 / 自然日 ' +
    m.natural_days + ' 天 · 平均/记录日 ' + m.avg_per_record_day.toFixed(2) + 'h · 平均/自然日 ' +
    m.avg_per_natural_day.toFixed(2) + 'h</div>').join('') + '</div>';
}

// ================================================================ 计划页

function renderPlanForm() {
  const f = S.planForm;
  if (!f.day) f.day = todayStr();
  $('#plan-form').innerHTML =
    '<div class="inline">' +
    '<label class="f" style="flex:0 0 160px"><span>日期</span><input type="date" id="pf-day" value="' + f.day + '"></label>' +
    '<label class="f"><span>大类</span>' + catSelect('pf-cat', f.cat) + '</label>' +
    '<label class="f"><span>二级任务（必填）</span>' + taskSelect('pf-task', f.cat, f.task) + '</label>' +
    '<label class="f" style="flex:0 0 150px"><span>计划时长（可空）</span><input type="text" id="pf-min" placeholder="3:00" value="' + esc(f.planned || '') + '"></label>' +
    '</div>' +
    '<div style="display:flex;gap:8px"><button class="btn primary" id="pf-save">加这条计划</button></div>' +
    '<div class="hint mt8">计划时长留空 = 只标记「今天要做这件事」。</div>';
  $('#pf-day').onchange = () => { f.day = $('#pf-day').value; };
  $('#pf-cat').onchange = () => { f.cat = $('#pf-cat').value; f.task = ''; renderPlanForm(); };
  bindTaskSelect('pf-task', 'pf-cat');
  $('#pf-save').onclick = async () => {
    const r = await post('/api/plan/add', {
      day: $('#pf-day').value, category_id: Number($('#pf-cat').value || 0),
      task_name: $('#pf-task').value === '__new__' ? '' : $('#pf-task').value,
      planned: $('#pf-min').value,
    });
    if (r && r.ok) { toast(r.message, 'ok'); f.planned = ''; loadPlan(); }
  };
}

function addPlanWeekRow(wd, cat, task, planned) {
  const host = $('#pw-rows');
  const div = document.createElement('div');
  div.className = 'inline plan-week-row';
  div.style.marginBottom = '6px';
  const WK = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
  div.innerHTML =
    '<label class="f" style="flex:0 0 110px"><span>星期</span><select class="pw-wd">' +
    WK.map((n, i) => '<option value="' + (i + 1) + '"' + (wd === i + 1 ? ' selected' : '') + '>' + n + '</option>').join('') +
    '</select></label>' +
    '<label class="f"><span>大类</span><select class="pw-cat">' +
    '<option value="">— 大类 —</option>' + (S.boot.categories || []).filter(c => !c.archived).map(c =>
      '<option value="' + c.id + '"' + (String(cat) === String(c.id) ? ' selected' : '') + '>' + esc(c.name) + '</option>').join('') +
    '</select></label>' +
    '<label class="f"><span>二级任务</span><input class="pw-task" type="text" value="' + esc(task || '') + '" placeholder="例如：升力计算"></label>' +
    '<label class="f" style="flex:0 0 130px"><span>计划时长（可空）</span><input class="pw-min" type="text" value="' + esc(planned || '') + '" placeholder="2:00"></label>' +
    '<div style="flex:0 0 auto"><button class="btn small danger pw-del">删</button></div>';
  $('.pw-del', div).onclick = () => div.remove();
  host.appendChild(div);
}

async function loadPlan() {
  const day = $('#pw-day').value || todayStr();
  const d = await api('/api/plans?start=' + shiftDay(day, -shiftDay(day, 0).length) + '&end=' + day);
  // 用真实区间：所在周 + 当天前后
  const dow = (new Date(day + 'T00:00:00').getDay() + 6) % 7;
  const mon = shiftDay(day, -dow), sun = shiftDay(mon, 6);
  const dd = await api('/api/plans?start=' + mon + '&end=' + sun);
  if (!dd || !dd.ok) return;
  const cmp = dd.compare || {};
  if (!cmp.has_plan) {
    $('#plan-compare').innerHTML = '<div class="hint">这一周还没有排计划。</div>';
  } else {
    $('#plan-compare').innerHTML =
      '<div class="statline"><div>计划 <b>' + cmp.plan_count + '</b> 条</div>' +
      '<div>已执行 <b class="good">' + cmp.executed_count + '</b> 条</div>' +
      '<div>未执行 <b class="badcake">' + cmp.unexecuted_count + '</b> 条</div>' +
      '<div>执行率 <b>' + (cmp.exec_rate * 100).toFixed(0) + '%</b></div>' +
      '<div>计划时长 <b>' + fmtHM(cmp.planned_minutes) + '</b></div>' +
      '<div>实际时长 <b>' + fmtHM(cmp.actual_minutes) + '</b></div>' +
      '<div>偏差 <b class="' + (cmp.diff_minutes >= 0 ? 'good' : 'badcake') + '">' +
      (cmp.diff_minutes >= 0 ? '+' : '-') + fmtHM(Math.abs(cmp.diff_minutes)) + '</b></div></div>' +
      (cmp.undone.length ? '<div class="notice warn mt8">没做的计划：' +
        cmp.undone.map(p => p.day + ' ' + esc(p.task_name)).join('、') + '</div>' : '') +
      (cmp.extra.length ? '<div class="notice mt8">计划外实际做的事：' +
        cmp.extra.map(p => p.day + ' ' + esc(p.task_name) + ' ' + p.hm).join('、') + '</div>' : '');
  }
  $('#plan-list').innerHTML = '<table><thead><tr><th>日期</th><th>星期</th><th>大类</th>' +
    '<th>二级任务</th><th class="num">计划</th><th class="num">实际</th><th>偏差</th><th>执行</th><th></th></tr></thead><tbody>' +
    (dd.plans || []).map(p => '<tr><td class="mono">' + p.day + '</td>' +
      '<td>' + ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][(new Date(p.day + 'T00:00:00').getDay() + 6) % 7] + '</td>' +
      '<td>' + esc(p.category_name) + '</td><td>' + esc(p.task_name) + '</td>' +
      '<td class="num mono">' + (p.planned_hm || '—') + '</td>' +
      '<td class="num mono">' + fmtHM(p.actual_minutes || 0) + '</td>' +
      '<td>' + (p.diff_minutes === null || p.diff_minutes === undefined ? '<span class="hint">—</span>'
        : '<span class="badge ' + (p.diff_minutes >= 0 ? 'ok' : 'bad') + '">' +
        (p.diff_minutes >= 0 ? '+' : '-') + fmtHM(Math.abs(p.diff_minutes)) + '</span>') + '</td>' +
      '<td>' + (p.executed ? '<span class="badge ok">已做</span>' : '<span class="badge bad">没做</span>') + '</td>' +
      '<td><button class="btn small danger" data-pdel="' + p.id + '">删</button></td></tr>').join('') +
    '</tbody></table>';
  $$('[data-pdel]').forEach(b => b.onclick = async () => {
    const r = await post('/api/plan/delete', { id: Number(b.dataset.pdel) });
    if (r && r.ok) { toast(r.message, 'ok'); loadPlan(); }
  });
}

// ================================================================ 总结页

async function generateReport(kind) {
  const body = { kind: kind, save: true };
  if (kind === 'custom') { body.start = $('#rp-start').value; body.end = $('#rp-end').value; }
  else body.day = todayStr();
  const d = await post('/api/report/generate', body);
  if (!d || !d.ok) return;
  S.report = d.report;
  $('#rp-title').textContent = d.report.title + (d.report.saved ? '（' + d.report.saved.message + '）' : '');
  $('#rp-body').textContent = d.report.markdown;
  toast('已生成总结。', 'ok');
  loadReports();
}

async function loadReports() {
  const d = await api('/api/reports');
  if (!d || !d.ok) return;
  $('#rp-list').innerHTML = (d.reports || []).length
    ? '<table><thead><tr><th>类型</th><th>周期</th><th>文件</th><th>生成时间</th><th></th></tr></thead><tbody>' +
      d.reports.map(r => '<tr><td>' + ({ week: '周报', month: '月报', custom: '自定义' }[r.kind] || r.kind) +
        '</td><td class="mono">' + r.start_day + ' ~ ' + r.end_day + '</td>' +
        '<td>' + esc(r.file_name) + (r.exists ? '' : ' <span class="badge bad">文件已丢</span>') + '</td>' +
        '<td class="mono hint">' + esc(r.generated_at) + '</td>' +
        '<td><button class="btn small" data-rp="' + r.id + '">查看</button></td></tr>').join('') +
      '</tbody></table>'
    : '<div class="hint">还没有生成过周报/月报。打开软件时会自动补生成上一个已结束的周和月。</div>';
  $$('[data-rp]').forEach(b => b.onclick = async () => {
    const r = await api('/api/report?id=' + b.dataset.rp);
    if (r && r.ok) {
      $('#rp-body').textContent = r.markdown;
      $('#rp-title').textContent = r.file;
      S.report = { markdown: r.markdown, title: r.file };
    }
  });
}

function copyReport() {
  const text = $('#rp-body').textContent || '';
  if (!text.trim()) return toast('还没有内容可以复制。', 'bad');
  navigator.clipboard.writeText(text).then(
    () => toast('已复制全文到剪贴板。', 'ok'),
    () => {
      const ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
      toast('已复制全文到剪贴板。', 'ok');
    });
}

// ================================================================ 设置页

function renderSettings() {
  const d = S.boot;
  // 顺序列按位置显示 1、2、3……（用户 2026-10-12 要求；数据库里仍存 10/20/30 方便插队，
  // 但界面上不露出这些内部数字）
  $('#set-cats').innerHTML = '<div class="table-wrap"><table><thead><tr><th class="num">顺序</th><th>名称</th>' +
    '<th>计入工作时间</th><th>状态</th><th></th></tr></thead><tbody>' +
    d.categories.map((c, i) => '<tr><td class="num">' + (i + 1) + '</td>' +
      '<td>' + esc(c.name) + (c.is_default ? ' <span class="badge info">默认</span>' : '') + '</td>' +
      '<td><input type="checkbox" data-cwork="' + c.id + '"' + (c.counts_work ? ' checked' : '') + '></td>' +
      '<td>' + (c.archived ? '<span class="badge warn">已归档</span>' : '<span class="badge ok">使用中</span>') + '</td>' +
      '<td class="nowrap"><button class="btn small" data-cname="' + c.id + '">改名</button> ' +
      '<button class="btn small" data-carch="' + c.id + '">' + (c.archived ? '取消归档' : '归档') + '</button> ' +
      (c.is_default ? '' : '<button class="btn small danger" data-cdel="' + c.id + '">删除</button>') +
      '</td></tr>').join('') + '</tbody></table></div>';
  $$('[data-cwork]').forEach(el => el.onchange = async () => {
    const r = await post('/api/category/save', { id: Number(el.dataset.cwork), counts_work: el.checked ? 1 : 0 });
    if (r && r.ok) { toast('已保存。', 'ok'); await refreshMeta(); renderTodayGoal(); }
  });
  $$('[data-cname]').forEach(b => b.onclick = async () => {
    const cid = Number(b.dataset.cname);
    const cur = S.boot.categories.find(x => x.id === cid);
    const name = prompt('新的名称：', cur.name);
    if (!name) return;
    const r = await post('/api/category/save', { id: cid, name: name });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); renderSettings(); }
  });
  $$('[data-carch]').forEach(b => b.onclick = async () => {
    const cid = Number(b.dataset.carch);
    const cur = S.boot.categories.find(x => x.id === cid);
    const r = await post('/api/category/save', { id: cid, archived: cur.archived ? 0 : 1 });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); renderSettings(); }
  });
  $$('[data-cdel]').forEach(b => b.onclick = async () => {
    if (!confirm('删除这个大类？')) return;
    const r = await post('/api/category/delete', { id: Number(b.dataset.cdel) });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); renderSettings(); }
  });
  $('#newcat-add').onclick = async () => {
    const r = await post('/api/category/save', {
      name: $('#newcat-name').value, counts_work: Number($('#newcat-counts').value)
    });
    if (r && r.ok) { toast(r.message, 'ok'); $('#newcat-name').value = ''; await refreshMeta(); renderSettings(); }
  };

  // 目标
  api('/api/goals').then(g => {
    if (!g || !g.ok) return;
    $('#set-goals').innerHTML = '<div class="table-wrap"><table><thead><tr><th>大类</th>' +
      '<th>工作日目标（周一~周五）</th><th>周末目标（周日）</th><th>周六</th></tr></thead><tbody>' +
      g.goals.map(x => '<tr><td>' + esc(x.category_name) + (x.archived ? ' <span class="badge warn">已归档</span>' : '') + '</td>' +
        '<td><input type="text" data-gwd="' + x.category_id + '" value="' + esc(x.workday_hm || '') + '" placeholder="如 3:00"></td>' +
        '<td><input type="text" data-gwe="' + x.category_id + '" value="' + esc(x.weekend_hm || '') + '" placeholder="如 1:00"></td>' +
        '<td class="hint">' + (x.workday_min
          ? '工作日 × ' + g.saturday_ratio + ' = ' + fmtHM(Math.round(x.workday_min * g.saturday_ratio))
          : '—') + '</td></tr>').join('') + '</tbody></table></div>' +
      '<div class="hint mt8">填 H:MM（如 3:00）；留空表示该类不参与达标判定。' +
      (g.has_goal ? '' : ' <b>你还没设置目标</b>，所以达成率模块目前是隐藏的。') +
      '</div>' +
      '<div style="margin-top:8px"><button class="btn primary" id="goals-save">保存目标</button></div>';
    $('#set-ratio').value = g.saturday_ratio;
    $('#goals-save').onclick = async () => {
      let ok = 0, bad = 0;
      for (const x of g.goals) {
        const wd = $('[data-gwd="' + x.category_id + '"]').value.trim();
        const we = $('[data-gwe="' + x.category_id + '"]').value.trim();
        for (const [type, val] of [['workday', wd], ['weekend', we]]) {
          if (val === '') continue;
          const r = await post('/api/goal/set', { category_id: x.category_id, goal_type: type, minutes: val });
          if (r && r.ok) ok++; else bad++;
        }
      }
      toast('目标已保存（成功 ' + ok + ' 项' + (bad ? '，失败 ' + bad + ' 项' : '') + '）。', 'ok');
      await refreshMeta(); renderSettings(); renderTodayGoal();
    };
  });
  $('#set-ratio-save').onclick = async () => {
    const r = await post('/api/settings/save', { saturday_ratio: $('#set-ratio').value });
    if (r && r.ok) { toast('已保存周六折算比例。', 'ok'); await refreshMeta(); renderSettings(); }
  };

  // 任务
  $('#task-pick').innerHTML = '<option value="">— 选择任务 —</option>' +
    d.tasks.filter(t => !t.archived).map(t => '<option value="' + t.id + '">' +
      esc(t.category_name) + ' / ' + esc(t.name) + '</option>').join('');
  $('#task-merge-to').innerHTML = '<option value="">— 合并到 —</option>' +
    d.tasks.filter(t => !t.archived).map(t => '<option value="' + t.id + '">' +
      esc(t.category_name) + ' / ' + esc(t.name) + '</option>').join('');
  // 新增二级任务：一小块表单（大类 + 新任务名 + 新增按钮），用户 2026-10-12 确认
  // 注意：这里的元素 id 故意加了 -set 后缀，避免和"新增任务弹窗"里的 #nt-name / #nt-cat 撞名
  // （id 撞名会让弹窗读到设置页那个看不见的输入框，正是刚才那个 bug 的第二个根因）
  const ntCat = $('#nt-cat-set');
  if (ntCat) {
    const keep = ntCat.value;
    ntCat.innerHTML = d.categories.filter(c => !c.archived).map(c =>
      '<option value="' + c.id + '">' + esc(c.name) + '</option>').join('');
    if (keep && Array.from(ntCat.options).some(o => o.value === keep)) ntCat.value = keep;
    else if ($('#task-pick').value) {
      // 默认跟着上面选中的任务走，符合"给我在这个大类下加一个"的直觉
      const cur = d.tasks.filter(t => !t.archived).find(t => String(t.id) === $('#task-pick').value);
      if (cur && Array.from(ntCat.options).some(o => o.value === String(cur.category_id))) {
        ntCat.value = String(cur.category_id);
      }
    }
  }
  const ntAdd = $('#nt-add-set');
  if (ntAdd) ntAdd.onclick = async () => {
    const inp = $('#nt-name-set');
    const name = inp ? inp.value.trim() : '';
    if (!name) { toast('请先填写二级任务的名字。', 'bad'); return; }
    const r = await post('/api/task/add', {
      category_id: Number($('#nt-cat-set').value), name: name
    });
    if (r && r.ok) {
      toast(r.message, r.created ? 'ok' : 'info');
      await refreshMeta();
      renderSettings();        // 重渲染，新任务立刻出现在上面的下拉框里
      const again = $('#nt-name-set');       // 重渲染后重新聚焦，方便连着加几个
      if (again) { again.value = ''; again.focus(); }
    }
  };
  const ntNameSet = $('#nt-name-set');
  if (ntNameSet) {
    ntNameSet.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); $('#nt-add-set').click(); }
    });
  }
  $('#task-rename').onclick = async () => {
    const r = await post('/api/task/rename', { id: Number($('#task-pick').value), name: $('#task-newname').value });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); renderSettings(); }
  };
  $('#task-merge').onclick = async () => {
    if (!confirm('合并后历史记录会迁到目标任务，原任务被归档。继续？')) return;
    const r = await post('/api/task/merge', {
      from_id: Number($('#task-pick').value), to_id: Number($('#task-merge-to').value)
    });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); renderSettings(); }
  };
  $('#task-archive').onclick = async () => {
    const r = await post('/api/task/archive', { id: Number($('#task-pick').value), archived: true });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); renderSettings(); }
  };

  // 标签
  api('/api/tags').then(t => {
    if (!t || !t.ok) return;
    $('#tag-pick').innerHTML = '<option value="">— 选择标签 —</option>' +
      (t.tags || []).map(x => '<option value="' + esc(x.name) + '">' + esc(x.name) +
        '（' + x.use_count + ' 条）</option>').join('');
    $('#tag-merge-to').innerHTML = '<option value="">— 合并到 —</option>' +
      (t.tags || []).map(x => '<option value="' + esc(x.name) + '">' + esc(x.name) + '</option>').join('');
  });
  $('#tag-rename').onclick = async () => {
    const r = await post('/api/tag/rename', { old: $('#tag-pick').value, new: $('#tag-newname').value });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); renderSettings(); }
  };
  $('#tag-merge').onclick = async () => {
    const r = await post('/api/tag/merge', { old: $('#tag-pick').value, new: $('#tag-merge-to').value });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); renderSettings(); }
  };

  // 其他设置
  const s = d.settings || {};
  $('#set-other').innerHTML =
    '<div class="inline">' +
    '<label class="f"><span>忘记停止的提醒阈值（分钟）</span><input type="number" id="s-ot" value="' + (s.timer_overtime_min || 240) + '"></label>' +
    '<label class="f"><span>空白日召回（最近几天）</span><input type="number" id="s-blank" value="' + (s.blank_recall_days || 7) + '"></label>' +
    '<label class="f"><span>Top 任务个数</span><input type="number" id="s-top" value="' + (s.top_n || 5) + '"></label>' +
    '<label class="f"><span>备份保留份数</span><input type="number" id="s-bk" value="' + (s.backup_keep || 30) + '"></label>' +
    '<label class="f"><span>撤销步数</span><input type="number" id="s-undo" value="' + (s.undo_steps || 20) + '"></label>' +
    '</div>';
  $('#set-save').onclick = async () => {
    const r = await post('/api/settings/save', {
      timer_overtime_min: $('#s-ot').value, blank_recall_days: $('#s-blank').value,
      top_n: $('#s-top').value, backup_keep: $('#s-bk').value, undo_steps: $('#s-undo').value
    });
    if (r && r.ok) { toast(r.message, 'ok'); await refreshMeta(); }
  };

  // 导出
  const b = d.data_bounds || {};
  $('#ex-start').value = b.first || todayStr();
  $('#ex-end').value = b.last || todayStr();
  $('#ex-cats').innerHTML = '<div class="hint">勾选要排除的大类（留空 = 全部导出）：</div>' +
    d.categories.map(c => '<label style="display:inline-block;margin-right:12px">' +
      '<input type="checkbox" data-ex="' + esc(c.name) + '"> ' + esc(c.name) + '</label>').join('');
  $('#ex-run').onclick = async () => {
    const exclude = $$('[data-ex]').filter(x => x.checked).map(x => x.dataset.ex);
    const r = await post('/api/export', {
      start: $('#ex-start').value, end: $('#ex-end').value, exclude: exclude
    });
    if (r && r.ok) { toast(r.message, 'ok'); loadExports(); }
  };
  $('#ex-open').onclick = () => post('/api/open_folder', { which: 'export' }).then(r => r && r.ok && toast(r.message, 'ok'));
  loadExports();

  // 备份
  $('#bk-now').onclick = async () => {
    const r = await post('/api/backup', {});
    if (r && r.ok) { toast(r.message, 'ok'); loadBackups(); }
  };
  $('#bk-open').onclick = () => post('/api/open_folder', { which: 'backup' }).then(r => r && r.ok && toast(r.message, 'ok'));
  loadBackups();

  // 撤销
  $('#undo-do').onclick = async () => {
    const r = await post('/api/undo', {});
    if (r && r.ok) { toast(r.message, 'ok'); await reloadAll(); loadUndo(); }
  };
  $('#undo-refresh').onclick = loadUndo;
  loadUndo();

  // 路径
  $('#set-paths').innerHTML =
    [['数据文件（每天在用）', 'data\\focus.db', 'data'],
    ['每日备份', 'backup\\（保留最近 ' + (s.backup_keep || 30) + ' 份）', 'backup'],
    ['周报/月报', 'reports\\', 'reports'],
    ['导出的 Excel', 'export\\', 'export'],
    ['原表副本', 'import\\（原表只读，这里放的是副本）', 'import']]
      .map(([t, p, w]) => '<div style="margin-bottom:6px">' + esc(t) + '：<b class="mono">' + esc(p) +
        '</b> <button class="btn small" data-open="' + w + '">打开目录</button></div>').join('');
  $$('[data-open]').forEach(b => b.onclick = () => post('/api/open_folder', { which: b.dataset.open })
    .then(r => r && r.ok && toast(r.message, 'ok')));
}

async function loadExports() {
  const d = await api('/api/exports');
  if (!d || !d.ok) return;
  $('#ex-list').innerHTML = (d.files || []).length
    ? '<table><thead><tr><th>文件</th><th class="num">大小</th><th>时间</th></tr></thead><tbody>' +
      d.files.map(f => '<tr><td>' + esc(f.file) + '</td><td class="num">' + f.size_text +
        '</td><td class="mono hint">' + f.time + '</td></tr>').join('') + '</tbody></table>'
    : '<div class="hint">还没有导出过。</div>';
}

async function loadBackups() {
  const d = await api('/api/backups');
  if (!d || !d.ok) return;
  $('#bk-list').innerHTML = (d.backups || []).length
    ? '<table><thead><tr><th>备份文件</th><th class="num">大小</th><th>时间</th><th></th></tr></thead><tbody>' +
      d.backups.map(f => '<tr><td>' + esc(f.file) + '</td><td class="num">' + f.size_text +
        '</td><td class="mono hint">' + f.time + '</td>' +
        '<td><button class="btn small danger" data-restore-bk="' + esc(f.file) + '">恢复</button></td></tr>').join('') +
      '</tbody></table>'
    : '<div class="hint">还没有备份。</div>';
  $$('[data-restore-bk]').forEach(b => b.onclick = () => {
    const file = b.dataset.restoreBk;
    const html = '<div class="notice bad">恢复会用这份备份覆盖当前数据。' +
      '当前数据库会先被改名留档（focus.db.before_restore_时间戳），但请确认你确实要这么做。</div>' +
      '<div>要恢复的文件：<b class="mono">' + esc(file) + '</b></div>' +
      '<label style="display:block;margin-top:10px"><input type="checkbox" id="bk-confirm"> 我确认要恢复</label>' +
      '<div class="hint">恢复后请刷新页面。</div>';
    const bg = modal('恢复备份', html, [{
      label: '确认恢复', cls: 'danger', onClick: async () => {
        if (!$('#bk-confirm', bg).checked) { toast('请先勾选确认。', 'bad'); return false; }
        const r = await post('/api/backup/restore', { file: file, confirm: 'RESTORE' });
        if (r && r.ok) {
          toast(r.message, 'ok');
          $('#bk-list').insertAdjacentHTML('beforebegin',
            '<div class="notice ok">' + esc(r.message) + '</div>');
          await reloadAll(); loadBackups();
          return true;
        }
        return false;
      }
    }, { label: '取消' }]);
  });
}

async function loadUndo() {
  const d = await api('/api/undo/history');
  if (!d || !d.ok) return;
  $('#undo-list').innerHTML = (d.history || []).length
    ? '<table><thead><tr><th>时间</th><th>操作</th><th>状态</th></tr></thead><tbody>' +
      d.history.map(h => '<tr><td class="mono hint">' + esc(h.ts) + '</td><td>' + esc(h.label) +
        '</td><td>' + (h.undone ? '<span class="badge">已撤销</span>' : '<span class="badge info">可撤销</span>') +
        '</td></tr>').join('') + '</tbody></table>'
    : '<div class="hint">还没有可撤销的操作。</div>';
}

async function loadAllSettings() {
  renderSettings();
}

// ================================================================ 视图切换与总刷新

const VIEWS = ['home', 'records', 'stats', 'plan', 'summary', 'settings'];

function goto(view, skipHash) {
  if (VIEWS.indexOf(view) < 0) view = 'home';
  S.view = view;
  $$('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  // 让每个页签都有一个可以直接打开的地址（例如 http://127.0.0.1:8090/#stats）
  if (!skipHash && location.hash !== '#' + view) {
    try { history.replaceState(null, '', '#' + view); } catch (e) { location.hash = view; }
  }
  if (view === 'records') { loadCalendar(); renderManualForm(); }
  if (view === 'stats') loadStats('week');
  if (view === 'plan') { renderPlanForm(); if (!$('#pw-rows').children.length) addPlanWeekRow(1); loadPlan(); }
  if (view === 'summary') loadReports();
  if (view === 'settings') loadAllSettings();
}

function fillEverywhere() {
  renderHero();
  renderNotices();
  renderTimers();
  renderStartArea();
  renderTodayGoal();
  renderTodayList();
  const b = (S.boot && S.boot.data_bounds) || {};
  if (b.first) { $('#rec-start').value = $('#rec-start').value || b.first; }
  if (b.last) { $('#rec-end').value = $('#rec-end').value || b.last; }
  if (!$('#rp-start').value) { $('#rp-start').value = b.first || todayStr(); }
  if (!$('#rp-end').value) { $('#rp-end').value = b.last || todayStr(); }
  if (!$('#pw-day').value) { $('#pw-day').value = todayStr(); }
  S.todayShown = S.boot && S.boot.today;
  checkOvertime(S.boot.overtime || []);
}

async function reloadAll() {
  await refreshMeta();
  if (S.view === 'records') loadCalendar();
  if (S.view === 'stats') loadStats('week');
  if (S.view === 'plan') loadPlan();
}

// ================================================================ 启动

function init() {
  $('#tabs').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-view]');
    if (b) goto(b.dataset.view);
  });
  $('#btn-theme').onclick = async () => {
    const next = S.settings.theme === 'light' ? 'dark' : 'light';
    S.settings.theme = next;
    applyTheme();
    await post('/api/settings/save', { theme: next });
  };
  $('#btn-compact').onclick = async () => {
    const next = String(S.settings.compact) === '1' ? '0' : '1';
    S.settings.compact = next;
    applyTheme();
    await post('/api/settings/save', { compact: next });
  };
  $('#rec-load').onclick = () => loadCalendar();
  $('#rec-week').onclick = () => {
    const t = todayStr(); const dow = (new Date(t + 'T00:00:00').getDay() + 6) % 7;
    $('#rec-start').value = shiftDay(t, -dow); $('#rec-end').value = shiftDay(t, 6 - dow);
    loadCalendar();
  };
  $('#rec-month').onclick = () => {
    const t = todayStr();
    $('#rec-start').value = t.slice(0, 7) + '-01';
    $('#rec-end').value = t;
    loadCalendar();
  };
  $('#rec-all').onclick = () => loadCalendar(true);

  $$('[data-stat]').forEach(b => b.onclick = () => {
    const k = b.dataset.stat;
    if (k === 'custom') loadStats('custom', $('#st-start').value, $('#st-end').value);
    else loadStats(k);
  });
  $$('[data-rep]').forEach(b => b.onclick = () => generateReport(b.dataset.rep));
  $('#rp-copy').onclick = copyReport;
  $('#rp-save').onclick = () => {
    if (!S.report) return toast('先在上面生成一份总结。', 'bad');
    post('/api/report/generate', {
      kind: S.report.kind || 'custom', start: S.report.start, end: S.report.end,
      day: todayStr(), save: true, force: true
    }).then(r => { if (r && r.ok) { toast(r.report.saved.message, 'ok'); loadReports(); } });
  };
  $('#rp-open').onclick = () => post('/api/open_folder', { which: 'reports' })
    .then(r => r && r.ok && toast(r.message, 'ok'));
  $('#pw-add-row').onclick = () => addPlanWeekRow(1);
  $('#pw-save').onclick = async () => {
    const day = $('#pw-day').value || todayStr();
    const items = $$('.plan-week-row').map(row => ({
      weekday: Number($('.pw-wd', row).value),
      category_id: Number($('.pw-cat', row).value || 0),
      task_name: $('.pw-task', row).value,
      planned: $('.pw-min', row).value,
    })).filter(x => x.category_id && x.task_name);
    if (!items.length) return toast('至少填一行（大类 + 二级任务）。', 'bad');
    const r = await post('/api/plan/week', { day: day, items: items });
    if (r && r.ok) { toast('已排 ' + r.created + ' 条周计划。', 'ok'); loadPlan(); }
    else if (r && r.errors && r.errors.length) toast(r.errors[0], 'bad');
  };

  // 首次进入：优先用地址里的 #页签，没有就进首页
  const startView = (location.hash || '').replace('#', '');
  refreshMeta().then(() => {
    goto(VIEWS.indexOf(startView) >= 0 ? startView : 'home');
    loadCalendar(true);
  });
  window.addEventListener('hashchange', () => {
    const v = (location.hash || '').replace('#', '');
    if (VIEWS.indexOf(v) >= 0 && v !== S.view) goto(v, true);
  });
  setInterval(tickTimers, 1000);
  // 每 5 分钟静默刷新一次首页数据（比如跨天、或后台自动切分之后）
  setInterval(() => { if (S.view === 'home') reloadAll().catch(() => { }); }, 300000);
  window.addEventListener('focus', () => { api('/api/timer/status').then(d => {
    if (d && d.ok) { S.timers = d.timers || []; S.tickBase = Date.now(); renderTimers(); checkOvertime(d.overtime || []); }
  }).catch(() => { }); });
}

document.addEventListener('DOMContentLoaded', init);
