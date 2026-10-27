'use strict';
const $ = id => document.getElementById(id);
const ns = 'http://www.w3.org/2000/svg';
let world = null, busy = false, pendingRequests = 0, requestRevision = 0, eventKey = '', decisionKey = '';
let serviceConnected = false;

// Four real simulator participants; identifiers never change in the UI mapping.
const ROBOTS = {
  R1: {name: 'R1 Pro · 01', short: 'Pro 01', role: '源区取放', color: 'var(--s1)'},
  R2: {name: 'R1 Pro · 02', short: 'Pro 02', role: '工位转运', color: 'var(--s1)'},
  R3: {name: 'R1 Lite · 01', short: 'Lite 01', role: '首段运输', color: 'var(--s7)'},
  R4: {name: 'R1 Lite · 02', short: 'Lite 02', role: '次段运输', color: 'var(--s7)'}
};
const skillNames = {acquire_cart: '取车', place_cart: '放车', move_to: '退出交接区', go_to_waiting_point: '前往等待点', observe_station: '观察车位'};
const stageNames = {pickup_full: 'Pro 01 取满车', handoff_full: 'Pro 01 放入满车交接位', clear_full: 'Pro 01 退出交接区', collect_full: 'Pro 02 接取满车', deliver_full: 'Pro 02 送至处理工位', unload: '等待工位确认卸空', pickup_empty: 'Pro 02 取空车', handoff_empty: 'Pro 02 放入空车交接位', clear_empty: 'Pro 02 退出交接区', collect_empty: 'Pro 01 接取空车', return_empty: 'Pro 01 补回源区空车', complete: '满车转运与空车补位已完成'};
const statusNames = {IDLE: '空闲', RUNNING: '执行中', SUCCEEDED: '已完成', FAILED: '失败', UNKNOWN: '待核对', OFFLINE: '离线', READY: '就绪', PENDING: '待派发', WAITING: '等待中', ACTIVE: '运行中', IN_PROGRESS: '运行中', BLOCKED: '等待条件恢复', STANDBY: '待命', CHARGING: '充电'};
const stageIndices = {pickup_full: 0, handoff_full: 1, clear_full: 1, collect_full: 2, deliver_full: 2, unload: 3, pickup_empty: 4, handoff_empty: 4, clear_empty: 4, collect_empty: 5, return_empty: 5, complete: 6};
const SCENARIOS = ['normal', 'blocked_handoff', 'grasp_failure'];
const label = id => ROBOTS[id] ? ROBOTS[id].name : id;
const humanize = text => String(text).replace(/\bR[1-4]\b(?! Pro| Lite)/g, id => label(id));

function node(tag, cls, text) { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = text; return el; }
function svg(tag, attrs, text) { const el = document.createElementNS(ns, tag); Object.entries(attrs || {}).forEach(([k, v]) => el.setAttribute(k, String(v))); if (text !== undefined) el.textContent = text; return el; }
function timeLabel(v) { const s = Number(v) || 0; return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(Math.floor(s % 60)).padStart(2, '0')}`; }
function station(id) { return world?.stations.find(s => s.id === id); }
function stationName(id) { const s = station(id); return s ? s.name.replace(/\bR[1-4]\b/g, rid => ROBOTS[rid]?.short || rid) : (id || '—'); }
function position(id) { const s = station(id); return s ? {x: 60 + s.x * 7.8, y: 70 + s.y * 4.8} : {x: 450, y: 310}; }
let noticeTimer = null;
function showNotice(text) { const n = $('notice'); n.textContent = text || ''; n.classList.toggle('on', !!text); clearTimeout(noticeTimer); if (text) noticeTimer = setTimeout(() => n.classList.remove('on'), 6000); }

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await r.json(); if (!r.ok) throw new Error(data.error || `请求失败 (${r.status})`); return data;
}
async function mutate(path, body) {
  const preempt = path === '/api/control' && ['pause', 'reset'].includes(body.action);
  if (busy && !preempt) return null;
  const rev = ++requestRevision; pendingRequests++; busy = true; syncControls();
  try {
    const res = await api(path, body);
    if (rev !== requestRevision) return null;
    if (res.state) render(res.state);
    if (res.result?.ok === false) { showNotice(`操作未执行：${res.result.code || '请检查现场状态'}`); return null; }
    showNotice(res.state?.runtime?.last_error || ''); return res;
  }
  catch (e) { if (rev === requestRevision) showNotice(e.message); return null; }
  finally { pendingRequests--; busy = pendingRequests > 0; syncControls(); }
}
function syncControls() {
  if (!world) return; const r = world.runtime, done = world.mission.status === 'SUCCEEDED';
  const blocked = recoveryReason();
  $('start').disabled = busy || r.running || done || !!blocked; $('pause').disabled = !busy && !r.running && !r.decision_pending;
  $('step').disabled = busy || r.running || r.decision_pending || done || !!blocked; $('planner-mode').disabled = busy || r.decision_pending;
  $('start').title = blocked;
  $('planner-mode').value = r.planner_mode; $('start').textContent = r.running ? '运行中' : '开始运行';
}

function renderMap() {
  const st = $('stations'); st.replaceChildren();
  for (const s of world.stations) {
    const p = position(s.id), waiting = s.id.includes('WAIT');
    const state = s.blocked ? 'blocked' : s.reserved_by ? 'reserved' : s.cart_id ? 'occupied' : '';
    const owner = world.tasks.find(t => t.id === s.reserved_by)?.robot_id;
    const g = svg('g', {transform: `translate(${p.x} ${p.y})`, 'data-station': s.id});
    g.append(svg('rect', {x: waiting ? -40 : -53, y: -24, width: waiting ? 80 : 106, height: 48, rx: 7, class: `station-plate ${state}`}));
    g.append(svg('text', {x: 0, y: -5, class: 'station-title'}, waiting ? stationName(s.id).replace(' 等待点', '') : s.name));
    g.append(svg('text', {x: 0, y: 13, class: `station-detail ${state}`}, s.blocked ? '× 外部占用' : s.reserved_by ? `${owner ? ROBOTS[owner]?.short || label(owner) : '任务'} 预约` : s.cart_id ? `${s.cart_id} 已就位` : s.detail || (waiting ? '等待点' : '空位')));
    if (s.reserved_by && s.cart_id) g.append(svg('title', {}, `${s.cart_id} 已就位 · ${owner ? label(owner) : '任务'} 已预约`));
    st.append(g);
  }
  const now = performance.now(), reset = mapEpoch !== world.epoch;
  if (reset) { robotViews.clear(); $('robot-layer').replaceChildren(); mapEpoch = world.epoch; }
  world.robots.forEach((r, i) => {
    let p = position(r.station_id); const task = world.tasks.find(t => t.id === r.active_task_id);
    if (task && ['RUNNING', 'UNKNOWN'].includes(task.status) && task.source_station_id) {
      const a = position(task.source_station_id), b = position(task.station_id), k = Math.min(1, Math.max(0, Number(task.progress) || 0));
      p = {x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k};
    }
    p = {x: p.x + (i === 0 ? -20 : 20), y: p.y - 50};
    let v = robotViews.get(r.id);
    if (!v) {
      v = {g: svg('g', {'data-robot': r.id}), x: p.x, y: p.y, held: r.held_cart_id};
      robotViews.set(r.id, v); $('robot-layer').append(v.g);
    }
    // Interpolate only between observed snapshots, never predict an unconfirmed move.
    if (!r.online || r.status === 'UNKNOWN') p = {x: v.x, y: v.y};
    Object.assign(v, {fromX: v.x, fromY: v.y, toX: p.x, toY: p.y, started: now,
      duration: reset || !r.online || !world.runtime.running || reduceMotion.matches ? 0 : 760,
      nextHeld: r.held_cart_id, robot: r, index: i});
    if (!v.duration || Math.hypot(v.x - p.x, v.y - p.y) < 1) v.held = v.nextHeld;
    drawRobot(v);
  });
  drawParkedCarts();
}
const robotViews = new Map(), reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
let mapEpoch = null;
function cartGraphic(c) {
  const g = svg('g', {'data-cart': c.id});
  g.append(svg('rect', {x: -19, y: -8, width: 38, height: 17, rx: 3, class: `cart-body ${c.load === 'full' ? 'full' : ''}`}));
  for (const x of [-12, 12]) g.append(svg('circle', {cx: x, cy: 12, r: 3, class: 'cart-wheel'}));
  g.append(svg('text', {x: 0, y: 29, class: 'cart-label'}, `${c.id} · ${c.load === 'full' ? '满车' : c.load === 'empty' ? '空车' : '待确认'}`));
  return g;
}
function drawRobot(v) {
  const r = v.robot, color = r.online ? ROBOTS[r.id]?.color : 'var(--muted)';
  v.g.replaceChildren(svg('circle', {r: 25, fill: color, stroke: 'var(--bg)', 'stroke-width': 4}), svg('text', {x: 0, y: 5, class: 'robot-label'}, ROBOTS[r.id]?.short || r.id));
  const cart = world.carts.find(c => c.id === v.held);
  if (cart) {
    const load = cartGraphic(cart); load.setAttribute('transform', 'translate(0 -50)');
    v.g.append(svg('path', {d: 'M0 -24V-38', class: 'cart-link'}), load);
  }
  if (!r.online) v.g.append(svg('text', {x: 0, y: 42, class: 'robot-load'}, r.status === 'UNKNOWN' ? '待核对 · 位置保留' : '离线'));
  v.g.setAttribute('transform', `translate(${v.x} ${v.y})`);
}
function drawParkedCarts() {
  const root = $('cart-layer'); root.replaceChildren();
  for (const c of world.carts) {
    if ([...robotViews.values()].some(v => v.held === c.id)) continue;
    // A pickup receipt can arrive before its visual approach finishes. Keep the
    // cart at that confirmed pickup station until the robot graphic reaches it.
    const owner = world.robots.find(r => r.id === c.held_by);
    const p = position(owner ? owner.station_id : c.station_id), g = cartGraphic(c);
    g.setAttribute('transform', `translate(${p.x} ${p.y + 40})`); root.append(g);
  }
}
function animateMap(now) {
  for (const v of robotViews.values()) {
    const k = v.duration ? Math.min(1, (now - v.started) / v.duration) : 1;
    v.x = v.fromX + (v.toX - v.fromX) * k; v.y = v.fromY + (v.toY - v.fromY) * k;
    v.g.setAttribute('transform', `translate(${v.x.toFixed(2)} ${v.y.toFixed(2)})`);
    if (k === 1 && v.held !== v.nextHeld) { v.held = v.nextHeld; drawRobot(v); drawParkedCarts(); }
  }
  requestAnimationFrame(animateMap);
}
requestAnimationFrame(animateMap);
function renderRobots() {
  const root = $('robots'); root.replaceChildren();
  world.robots.forEach(r => {
    const meta = ROBOTS[r.id] || {name: r.id, role: '', color: 'var(--muted)'};
    const task = world.tasks.find(t => t.id === r.active_task_id);
    const card = node('div', 'robot-card'); card.style.setProperty('--c', meta.color); card.dataset.robot = r.id;
    const head = node('div', 'robot-head'); head.append(node('i', 'dot'), node('span', '', meta.name), node('span', 'state', r.online ? (statusNames[r.status] || r.status) : '离线'));
    card.append(head, node('div', 'robot-task', task ? `${skillNames[task.skill] || task.skill} → ${stationName(task.station_id)}` : `${meta.role} · ${stationName(r.station_id)}`));
    const completed = world.tasks.filter(t => t.robot_id === r.id && t.status === 'SUCCEEDED').length;
    const foot = node('div', 'robot-foot'); foot.append(node('span', '', r.held_cart_id ? `持车 ${r.held_cart_id}` : '未持车'), node('span', '', `本轮完成 ${completed} 项${task ? ' · 当前任务执行中' : ' · 等待派发'}`));
    const prog = node('div', 'progress'); const fill = node('i'); fill.style.width = `${Math.round((Number(task?.progress) || 0) * 100)}%`; prog.append(fill);
    card.append(foot, prog); root.append(card);
  });
}
function renderMission() {
  const m = world.mission, status = $('mission-status');
  status.textContent = m.status === 'BLOCKED' ? '等待条件恢复' : world.runtime.running ? '正在执行' : (statusNames[m.status] || m.status);
  status.className = 'chip ' + (m.status === 'SUCCEEDED' ? 'ok' : world.robots.some(r => r.status === 'UNKNOWN') ? 'warn' : world.runtime.running ? 'run' : '');
  $('mission-id').textContent = m.id; $('mission-stage').textContent = (m.stage_label || stageNames[m.stage] || m.stage || '等待任务派发') + (m.status === 'BLOCKED' ? ` · ${humanize(m.wait_reason)}` : '');
  const four = world.fleet_mode === 'four';
  const phases = four ? [['01 源区取车', 'Pro 01'], ['02 交接 A', 'Pro 01 → Lite 01'], ['03 交接 B', 'Lite 01 → Lite 02'], ['04 交接 C', 'Lite 02 → Pro 02'], ['05 工位送达', 'Pro 02'], ['06 确认卸空', '独立工位事件'], ['07 反向接力', 'Pro 02 → 两台 Lite'], ['08 源区补位', 'Pro 01 · 空车返回']] : [['01 取出满车', 'Pro 01 · 源区'], ['02 满车交接', '释放与退出确认'], ['03 送至工位', 'Pro 02 · 处理工位'], ['04 等待卸空', '独立工位事件'], ['05 空车交接', 'Pro 02 → Pro 01'], ['06 源区补位', '空车返回验证']];
  const cur = m.status === 'SUCCEEDED' ? phases.length : (m.phase_index ?? stageIndices[m.stage] ?? 0);
  $('mission-track').classList.toggle('four', four);
  $('mission-track').replaceChildren(...phases.map(([t, d], i) => { const el = node('div', `step ${i < cur ? 'done' : i === cur ? 'active' : ''}`, t); el.append(node('small', '', d)); return el; }));
  const ok = world.tasks.filter(t => t.status === 'SUCCEEDED').length, failed = world.tasks.filter(t => t.status === 'FAILED').length, unknown = world.tasks.filter(t => t.status === 'UNKNOWN').length;
  $('metrics').textContent = `已完成 ${ok} 项技能${failed ? ` · 失败 ${failed} 次` : ''}${unknown ? ` · 待核对 ${unknown} 项` : ''}`;
}
function renderDecision() {
  const rt = world.runtime, model = rt.model || {};
  $('planner-label').textContent = rt.planner_mode === 'model' ? '真实模型' : '规则';
  $('model-info').textContent = model.configured ? `已配置：${model.model || '模型'}` : '未配置模型，规则调度';
  const ds = world.decisions || [], d = ds[ds.length - 1];
  $('decision-state').textContent = rt.decision_pending ? '正在生成决策…' : d ? `基于世界状态 v${d.world_version ?? '—'}` : '等待首次决策';
  if (!d) { decisionKey = ''; $('decision-action').textContent = '准备就绪'; $('decision-reason').textContent = '开始运行后，调度器从可执行候选里选一个任务，派发前重新核验世界状态版本和资源预约。'; $('candidate-list').replaceChildren(); $('decision-meta').textContent = ''; return; }
  const key = JSON.stringify(d); if (key === decisionKey) return; decisionKey = key;
  const sel = (d.candidates || []).find(c => c.id === d.candidate_id);
  $('decision-action').textContent = humanize(sel?.description || (d.status === 'error' ? '决策未通过' : '等待现场变化'));
  $('decision-reason').textContent = humanize(d.reason || d.error || '未提供原因');
  $('candidate-list').replaceChildren(...(d.candidates || []).map(c => node('div', `cand ${c.id === d.candidate_id ? 'sel' : ''}`, `${c.id === d.candidate_id ? '已选 · ' : '候选 · '}${humanize(c.description)}`)));
  $('decision-meta').textContent = `${d.mode === 'model' ? '模型推理' : '规则选择'} · ${Math.round(d.latency_ms || 0)} ms · ${d.dispatch?.code || d.status}`;
}
function renderEvents() {
  const ev = world.events || [], key = `${world.epoch}:${ev.length}:${ev[ev.length - 1]?.id}`;
  if (key === eventKey) return;
  const sameEpoch = eventKey.startsWith(`${world.epoch}:`), oldRows = new Map([...$('evlist').children].map(row => [row.dataset.id, row]));
  eventKey = key;
  $('event-count').textContent = `最近 ${ev.length} 条`;
  $('evlist').replaceChildren(...ev.slice(-18).reverse().map(e => {
    const old = sameEpoch && oldRows.get(String(e.id)); if (old) return old;
    const cls = /FAIL|UNKNOWN|REJECT|OFFLINE/.test(e.type) ? 'bad' : /SUCCESS|VERIF|COMPLETE/.test(e.type) ? 'ok' : /RESET|RECOVER/.test(e.type) ? 'warn' : '';
    const row = node('div', `${cls}${sameEpoch ? ' event-new' : ''}`); row.dataset.id = e.id;
    row.addEventListener('animationend', () => row.classList.remove('event-new'), {once: true});
    row.append(node('span', 't', timeLabel(e.time)), node('i', 'mark'), node('span', '', humanize(e.message)), node('span', 'ty', e.type)); return row;
  }));
}
function render(data) {
  world = data; serviceConnected = true; renderHeader();
  $('sim-time').textContent = timeLabel(world.sim_time); $('world-version').textContent = `v${world.version}`;
  syncControls(); renderMap(); renderRobots(); renderMission(); renderDecision(); renderEvents();
  if (world.runtime.last_error) showNotice(humanize(world.runtime.last_error));
  autoDemo();
}

// Each epoch gets one automatic start attempt. Explicit recovery/reset can re-arm it.
let doneSince = null, autoBusy = false, autoAttemptEpoch = null, autoHold = '';
function recoveryReason() {
  if (!world) return '';
  if (world.tasks.some(t => t.status === 'UNKNOWN') || world.robots.some(r => r.status === 'UNKNOWN' || r.location_known === false)) return '待核对：请点“恢复并核对”';
  if (world.robots.some(r => !r.online || r.status === 'OFFLINE')) return '机器人离线：请点“恢复并核对”';
  return '';
}
function autoStatus(text) { $('auto-status').textContent = text; }
async function autoDemo() {
  if (!world || autoBusy || busy) return;
  if (!$('autodemo').checked) { doneSince = null; autoStatus('手动控制'); return; }
  const r = world.runtime, m = world.mission;
  const recovery = recoveryReason();
  if (recovery) {
    doneSince = null; autoStatus(recovery);
    // Stop the simulator clock once, while keeping its UNKNOWN reservations intact.
    if (r.running && autoAttemptEpoch !== `pause:${world.epoch}`) {
      autoAttemptEpoch = `pause:${world.epoch}`; autoBusy = true;
      try { await mutate('/api/control', {action: 'pause'}); } finally { autoBusy = false; }
    }
    return;
  }
  if (autoHold || r.last_error) { doneSince = null; autoStatus(autoHold || '调度异常：请检查后手动开始或重置'); return; }
  if (m.status === 'SUCCEEDED') {
    doneSince = doneSince || Date.now();
    autoStatus(`本轮完成 · ${Math.max(0, Math.ceil((8000 - Date.now() + doneSince) / 1000))} s 后换场景`);
    if (Date.now() - doneSince > 8000) {
      autoBusy = true; doneSince = null;
      const next = SCENARIOS[(SCENARIOS.indexOf(world.scenario) + 1) % SCENARIOS.length]; $('scenario').value = next;
      try {
        const reset = await mutate('/api/control', {action: 'reset', scenario: next});
        if (!reset) { autoHold = '重置未成功：请手动重置'; return; }
        if (!$('autodemo').checked || reset.state?.epoch !== world.epoch) return;
        autoAttemptEpoch = world.epoch;
        if (!await mutate('/api/control', {action: 'start'})) autoHold = '启动未成功：请手动开始或重置';
      } finally { autoBusy = false; }
    }
  } else if (!r.running && !r.decision_pending && m.status !== 'BLOCKED') {
    doneSince = null; autoBusy = true;
    try {
      if (autoAttemptEpoch === world.epoch) { autoStatus('自动启动已尝试 · 请手动开始或重置'); return; }
      autoAttemptEpoch = world.epoch; autoStatus('正在启动');
      if (!await mutate('/api/control', {action: 'start'})) autoHold = '启动未成功：请手动开始或重置';
    } finally { autoBusy = false; }
  } else { doneSince = null; autoStatus(m.status === 'BLOCKED' ? '等待条件恢复' : '自动演示中'); }
}

function rearmAuto() { autoHold = ''; autoAttemptEpoch = null; doneSince = null; }
$('start').onclick = async () => { rearmAuto(); autoAttemptEpoch = world?.epoch; await mutate('/api/control', {action: 'start'}); };
$('pause').onclick = () => { $('autodemo').checked = false; mutate('/api/control', {action: 'pause'}); };
$('step').onclick = () => { $('autodemo').checked = false; mutate('/api/control', {action: 'step'}); };
$('reset').onclick = async () => { if (await mutate('/api/control', {action: 'reset', scenario: $('scenario').value})) { rearmAuto(); autoDemo(); } };
$('planner-mode').onchange = async () => { if (await mutate('/api/mode', {mode: $('planner-mode').value})) { rearmAuto(); autoDemo(); } };
$('autodemo').onchange = () => { rearmAuto(); autoDemo(); };
document.querySelectorAll('[data-fault]').forEach(b => b.onclick = async () => {
  const res = await mutate('/api/inject', {kind: b.dataset.fault});
  if (res && ['robot_online', 'clear_handoff'].includes(b.dataset.fault)) rearmAuto();
  autoDemo();
});

async function poll() {
  const rev = requestRevision;
  try { const state = await api('/api/state'); if (rev === requestRevision && !busy) render(state); }
  catch (e) { serviceConnected = false; renderHeader(); showNotice(`无法读取本地模拟服务：${e.message}`); }
  finally { setTimeout(poll, 800); }
}

// ---------- 画面切换 + 回放 ----------
let screen = 'showcase', replayReady = false, lastSwitch = Date.now(), replaySeek = null, replayFocusTimer = null;
function renderHeader() {
  $('screen-subtitle').textContent = screen === 'showcase' ? 'R1 Pro · 现场观察、动作执行与结果证据' : screen === 'fleet' ? '2 × R1 Pro + 2 × R1 Lite · 四台机队 · 本地模拟' : 'R1 Pro · 模型动作预测与执行反馈 · 真机日志离线回放';
  $('conn').className = screen === 'fleet' && !serviceConnected ? 'off' : '';
  if (screen === 'showcase') {
    const s = window.Showcase.getState();
    $('conn').className = s.mode === 'live' && !s.liveConnected ? 'off' : '';
    $('conn').lastElementChild.textContent = s.status;
  } else $('conn').lastElementChild.textContent = screen === 'replay' ? (replayReady ? '离线回放' : '正在载入回放') : serviceConnected ? '模拟服务已连接' : '模拟服务未连接';
}
function replayCommand(action) { if (replayReady) $('replay-frame').contentWindow?.postMessage({type: 'replay-control', action}, location.origin); }
function show(name) {
  if (!['showcase', 'fleet', 'replay'].includes(name)) return;
  const entering = screen !== name;
  clearTimeout(replayFocusTimer);
  screen = name; document.querySelectorAll('.tab').forEach(t => t.classList.toggle('on', t.dataset.screen === name));
  document.querySelectorAll('.screen').forEach(s => {
    const selected = s.id === name + '-screen';
    s.hidden = !selected; s.inert = !selected; s.classList.toggle('on', selected);
  });
  window.Showcase.activate(name === 'showcase');
  if (name === 'replay') {
    const f = $('replay-frame'); if (f.src === 'about:blank' || !f.src) loadReplay(); else if (entering) replayCommand('restart');
    replayFocusTimer = setTimeout(() => { if (screen === 'replay') f.contentWindow?.focus(); }, 50);
  } else { replayCommand('pause'); if (entering) document.querySelector(`[data-screen="${name}"]`).focus({preventScroll: true}); }
  renderHeader();
  lastSwitch = Date.now();
}
function loadReplay() { const v = $('replay-list').value; if (v) { replayReady = false; $('replay-frame').src = '/replays/' + encodeURIComponent(v); renderHeader(); } }
document.querySelectorAll('.tab').forEach(t => t.onclick = () => show(t.dataset.screen));
$('replay-list').onchange = () => { replaySeek = null; loadReplay(); };
const screenKey = {'1': 'fleet', '2': 'replay', '3': 'showcase'};
window.addEventListener('keydown', e => { if (e.repeat || e.ctrlKey || e.metaKey || e.altKey || e.target.isContentEditable || /^(SELECT|INPUT|TEXTAREA)$/.test(e.target.tagName)) return; if (screenKey[e.key]) { e.preventDefault(); show(screenKey[e.key]); } });
window.addEventListener('message', e => {
  if (e.origin !== location.origin || e.source !== $('replay-frame').contentWindow || !e.data) return;
  if (e.data.type === 'replay-ready') {
    replayReady = true;
    if (screen === 'replay' && replaySeek != null) { $('replay-frame').contentWindow.postMessage({type:'replay-control',action:'seek',time:replaySeek,paused:true},location.origin); replaySeek=null; }
    else { replaySeek=null; replayCommand(screen === 'replay' ? 'restart' : 'pause'); }
    renderHeader();
  }
  if (screen === 'replay' && screenKey[e.data.switchScreen]) show(screenKey[e.data.switchScreen]);
});
window.addEventListener('showcase-status', renderHeader);
window.addEventListener('showcase-detail', e => {
  if (!e.detail || !Number.isFinite(e.detail.time) || ![...$('replay-list').options].some(o => o.value === e.detail.file)) return;
  $('replay-list').value=e.detail.file;
  // Load the selected record before switching so readiness applies to that file.
  replaySeek=e.detail.time; loadReplay(); show('replay');
});
function updateRotation() {
  $('rotate-sec').disabled = !$('rotate').checked;
  lastSwitch = Date.now();
}
$('rotate').onchange = updateRotation;
$('rotate-sec').onchange = updateRotation;
updateRotation();
setInterval(() => { if ($('rotate').checked && Date.now() - lastSwitch > Number($('rotate-sec').value) * 1000) { const screens=['showcase','fleet','replay']; show(screens[(screens.indexOf(screen)+1)%screens.length]); } }, 1000);
(async () => {
  try { const r = await api('/api/replays'); $('replay-list').replaceChildren(...r.replays.map(f => { const o = node('option', '', f.replace(/^replay_|\.html$/g, '')); o.value = f; return o; })); if (!r.replays.length) $('replay-pick').textContent = '没有回放文件：把 replay_panel 生成的 HTML 放进 static/replays/'; else if (screen === 'replay') loadReplay(); }
  catch (e) { $('replay-pick').textContent = '回放列表读取失败'; }
})();
show(location.hash === '#replay' ? 'replay' : location.hash === '#fleet' ? 'fleet' : 'showcase');
poll();
