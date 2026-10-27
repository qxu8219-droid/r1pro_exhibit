'use strict';
// Read-only evidence view. Recording and live data never share a frame buffer.
window.Showcase = (() => {
  const el = id => document.getElementById(id);
  let active = false, mode = 'recording', file = '', data = null, time = 0;
  let playing = false, generation = 0, lastFrame = 0, endSince = null, lastPaint = 0;
  let status = '正在载入记录', chapters = [], live = null, ready = false, liveReceivedAt = 0, livePending = false;
  const cache = new Map();
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const sec = v => finite(v) ? v.toFixed(1) + ' s' : '—';
  const text = (id, value) => { el(id).textContent = value; };
  const lastAt = (rows, t, getTime) => { let found = null; for (const row of rows) { if (getTime(row) <= t) found = row; } return found; };
  const announce = value => { status = value; window.dispatchEvent(new CustomEvent('showcase-status')); };
  const safeImage = value => typeof value === 'string' && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=\s]+$/.test(value);
  function image(id, value, emptyText) {
    const img = el(id), placeholder = el(id + '-empty');
    if (safeImage(value)) {
      if (img.getAttribute('src') !== value) img.setAttribute('src', value);
      img.hidden = false; placeholder.hidden = true;
    } else {
      img.removeAttribute('src'); img.hidden = true; placeholder.hidden = false;
      placeholder.textContent = emptyText || '此时没有相机记录';
    }
  }
  function clearFrames(message) {
    ['show-head', 'show-left', 'show-right'].forEach(id => image(id, null, message));
    ['show-camera-clock', 'show-left-age', 'show-right-age', 'show-segment', 'show-observation-value', 'show-prediction-value', 'show-feedback-value', 'show-rtt', 'show-gripper'].forEach(id => text(id, '—'));
  }
  function clearEvidence(message) {
    clearFrames(message);
    ['show-observation', 'show-prediction', 'show-feedback'].forEach(id => text(id, message));
    text('show-resolution', '—');
    text('show-model', '—'); text('show-clock', '—'); text('show-frame-note', message);
    el('show-scrub').value = 0;
  }
  function playbackControls() {
    const available = mode === 'recording' && !!data && ready;
    ['show-play', 'show-restart', 'show-speed', 'show-scrub', 'show-detail'].forEach(id => { el(id).disabled = !available; });
    el('show-recording').disabled = mode === 'live';
    text('show-play', playing ? '暂停' : '播放');
  }
  function makeChapters() {
    const c = data?.chunks || [], targets = data?.targets || [], events = data?.events || [];
    chapters = [
      {label: '01 观察输入', t: c.length ? c[0].t_cap : null},
      {label: '02 动作下发', t: targets.length ? targets[0][0] : null},
      {label: '03 闭合条件', t: events.find(e => e.kind === 'grasp_detected')?.t ?? null},
      {label: '04 张开指令', t: events.find(e => e.kind === 'release_detected')?.t ?? null},
    ];
    el('show-chapters').replaceChildren(...chapters.map((chapter, i) => {
      const b = document.createElement('button'); b.dataset.chapter = i;
      const title = document.createElement('span'), mark = document.createElement('small');
      title.textContent = chapter.label; mark.textContent = finite(chapter.t) ? sec(chapter.t) + ' ↗' : '无记录';
      b.append(title, mark); b.disabled = !finite(chapter.t) || mode !== 'recording';
      b.onclick = () => seek(chapter.t, true); return b;
    }));
  }
  function renderRecording() {
    if (!data || !ready || mode !== 'recording') return;
    const chunks = data.chunks, targets = data.targets, meta = data.meta;
    const cap = lastAt(chunks, time, c => c.t_cap);
    const response = lastAt(chunks, time, c => c.t_resp);
    const issued = targets.filter(row => row[0] <= time), target = issued[issued.length - 1];
    const seen = data.events.filter(e => e.t <= time);
    const grip = seen.find(e => e.kind === 'grasp_detected'), release = seen.find(e => e.kind === 'release_detected');
    image('show-head', cap?.img?.head_rgb);
    image('show-left', cap?.img?.left_wrist_rgb);
    image('show-right', cap?.img?.right_wrist_rgb);
    text('show-camera-clock', cap ? '采集 ' + sec(cap.t_cap) : '尚无输入');
    text('show-frame-note', cap ? `第 ${cap.i + 1} 段输入 · 采样帧回放` : '等待首帧');
    text('show-resolution', '原始输入 224 × 224');
    const ages = cap?.ages_ms || {};
    text('show-left-age', finite(ages.left_wrist_rgb) ? `采集延迟 ${ages.left_wrist_rgb} ms` : '原始输入');
    text('show-right-age', finite(ages.right_wrist_rgb) ? `采集延迟 ${ages.right_wrist_rgb} ms` : '原始输入');
    text('show-segment', response ? `第 ${response.i + 1} / ${chunks.length} 段` : '等待模型返回');
    text('show-observation', cap ? `采集于 ${sec(cap.t_cap)} · ${Math.max(0, time - cap.t_cap).toFixed(1)} s 前的记录帧` : '此时尚无采集记录');
    text('show-observation-value', cap ? Object.values(cap.img || {}).filter(safeImage).length + ' 路' : '—');
    text('show-prediction', response ? `请求 ${response.i} 已返回 · ${sec(response.t_resp)}` : '尚无模型返回记录');
    text('show-prediction-value', response ? response.action.length + ' 步' : '—');
    text('show-feedback', target ? `最近记录：第 ${target[1] + 1} 段 / 步 ${target[2] + 1}` : '此时尚无下发记录');
    text('show-feedback-value', issued.length + ' 步');
    text('show-rtt', response && finite(response.rtt) ? Math.round(response.rtt * 1000) + ' ms' : '—');
    text('show-gripper', target && finite(target[4]?.[15]) ? target[4][15].toFixed(1) + '%' : '—');
    let stage = target ? '动作执行中 · 按段预测，逐步下发' : cap ? '相机输入已记录 · 等待动作下发' : '准备观察';
    let moment = '相机输入、动作与反馈来自同一次运行';
    if (grip) { stage = '夹爪闭合条件已触发'; moment = '检测器记录闭合条件；是否持有物体需要核验'; }
    if (release) {
      stage = '张开指令已记录';
      const missing = [];
      if (!chunks.some(c => c.t_cap > release.t)) missing.push('相机画面');
      if (!targets.some(row => row[0] > release.t)) missing.push('执行反馈');
      moment = missing.length ? '缺少张开后的' + missing.join('和') + '；当前画面为此前采样，无法确认放下完成' : '张开指令不代表放下完成，任务结果仍需独立核验';
    }
    if (time >= meta.t_end) { stage = '本次运行记录结束'; if (!release) moment = '已有记录已回放完毕，任务结果仍需独立核验'; }
    text('show-stage', stage); el('show-moment').querySelector('span').textContent = moment;
    text('show-status', playing ? '回放中' : '已暂停');
    text('show-clock', `${time.toFixed(1)} / ${meta.t_end.toFixed(1)} s`);
    el('show-scrub').value = time;
    let current = -1;
    chapters.forEach((c, i) => { if (finite(c.t) && c.t <= time) current = i; });
    [...el('show-chapters').children].forEach((button, i) => { button.classList.toggle('past', i <= current); button.classList.toggle('current', i === current); });
    playbackControls();
  }
  async function loadRecording(nextFile) {
    const rev = ++generation; file = nextFile; data = null; ready = false; playing = false; time = 0; live = null; endSince = null;
    clearEvidence('正在载入这次运行'); makeChapters(); playbackControls();
    text('show-task-name', '正在载入运行记录'); text('show-stage', '等待真实数据');
    text('show-source-label', '真机记录 · 回放'); el('show-source-label').className = 'chip';
    text('show-provenance', '正在载入记录'); announce('正在载入记录');
    try {
      let result = cache.get(nextFile);
      if (!result) {
        const response = await fetch('/api/evidence?file=' + encodeURIComponent(nextFile));
        if (!response.ok) throw new Error('record unavailable');
        result = await response.json();
        if (result.source !== 'recording' || !Array.isArray(result.data?.chunks) || !Array.isArray(result.data?.targets) || !Array.isArray(result.data?.events) || !finite(result.data?.meta?.t_end)) throw new Error('invalid record');
        cache.set(nextFile, result);
      }
      if (rev !== generation || mode !== 'recording') return;
      data = result.data; ready = true;
      time = Math.max(0, data.chunks[0]?.t_cap || 0); playing = active;
      const subtitle = (data.meta.subtitle || '').split('·').map(s => s.trim());
      text('show-task-name', subtitle.length > 1 ? subtitle[1] : (data.meta.task || '机器人操作记录'));
      text('show-model', data.meta.model_id || '未提供模型身份');
      text('show-decision-note', '展示动作模型的实际输出；本次记录无 VLM 决策文本。');
      text('show-result', '是否抓稳、是否入筐，需要独立证据。');
      text('show-result-detail', '这份日志记录了动作与夹爪条件，未提供物体入筐的独立判定。');
      text('show-provenance', `真机记录 · ${(data.meta.started_utc || '').replace('T', ' ').replace(/\.\d+Z$/, 'Z')} · ${data.meta.run_dir}`);
      text('show-boundary', '四机调度是独立模拟场景');
      el('show-scrub').max = data.meta.t_end;
      makeChapters(); renderRecording(); announce('真机记录 · 回放');
    } catch (_) {
      if (rev !== generation) return;
      clearEvidence('记录载入失败'); text('show-task-name', '运行记录不可用'); text('show-stage', '重新选择记录可重试');
      text('show-provenance', '记录载入失败 · 未显示其他来源的数据'); announce('记录载入失败');
    }
  }
  function renderLive(result) {
    if (mode !== 'live') return;
    live = result; liveReceivedAt = performance.now(); const state = result.connected ? result.state : null;
    const labels = {unconfigured: '实时源未接入', missing: '等待实时快照', invalid: '实时数据格式异常', too_large: '实时数据不可用', stale: '实时数据已过期', error: '实时源读取失败', connected: '只读实时观察'};
    const label = labels[result.status] || '实时源未连接';
    clearEvidence(label); text('show-source-label', label);
    el('show-source-label').className = 'chip ' + (state ? 'live' : 'offline');
    text('show-status', state ? '实时观察' : '未连接');
    text('show-task-name', state?.task || '等待真实机器人接入');
    text('show-stage', state?.stage || '接入后显示当前任务与阶段');
    text('show-model', state?.model_id || '—');
    text('show-decision-note', '只读展示机器人回传的信息；本页面不下发动作。');
    text('show-result', '任务结果等待独立核验');
    text('show-result-detail', '相机画面和阶段回传可供核对；连接正常不代表任务完成。');
    text('show-boundary', '实时源与回放独立标识');
    el('show-moment').querySelector('span').textContent = state ? '正在接收同一台机器人、同一次运行的观测' : '尚无有效实时观测，当前没有显示回放画面';
    text('show-provenance', state ? `机器人 ${state.robot_id} · 运行 ${state.run_id}` : '实时观察入口已准备 · 等待本机只读数据源');
    if (state) {
      image('show-head', state.images?.head, '未提供头部画面');
      image('show-left', state.images?.wrist_left, '未提供左腕画面');
      image('show-right', state.images?.wrist_right, '未提供右腕画面');
      const stamp = new Date(state.observed_at * 1000).toLocaleTimeString('zh-CN', {hour12: false});
      text('show-camera-clock', '采集 ' + stamp); text('show-frame-note', `${Number(result.age_s || 0).toFixed(1)} s 前更新`);
      text('show-resolution', '机器人回传画面');
      text('show-observation', '采集于 ' + stamp); text('show-observation-value', Object.values(state.images || {}).filter(safeImage).length + ' 路');
      text('show-prediction', state.request_id != null ? '请求 ' + state.request_id : '未提供动作预测');
      text('show-prediction-value', '—'); text('show-feedback', '未提供下发与反馈序列');
      text('show-rtt', finite(state.rtt_ms) ? Math.round(state.rtt_ms) + ' ms' : '—');
    }
    playbackControls(); announce(label);
  }
  async function pollLive() {
    const rev = generation;
    if (active && mode === 'live' && !livePending) {
      livePending = true;
      const controller = new AbortController(), started = performance.now();
      const timeout = setTimeout(() => controller.abort(), 1600);
      try {
        const response = await fetch('/api/telemetry', {signal: controller.signal});
        if (!response.ok) throw new Error('unavailable');
        const result = await response.json();
        if (result.connected) {
          result.age_s += (performance.now() - started) / 1000;
          if (result.age_s > 5) { result.connected = false; result.status = 'stale'; }
        }
        if (rev === generation && active && mode === 'live') renderLive(result);
      } catch (_) { if (rev === generation && mode === 'live') renderLive({status: 'error', connected: false}); }
      finally { clearTimeout(timeout); livePending = false; }
    }
  }
  function seek(value, paused = true) {
    if (mode !== 'recording' || !data || !finite(value)) return;
    time = Math.min(data.meta.t_end, Math.max(0, value)); playing = active && !paused; endSince = null;
    renderRecording();
  }
  function activate(value) {
    active = value; lastFrame = 0;
    if (!active) playing = false;
    else if (mode === 'recording' && ready) playing = true;
    else if (mode === 'live') pollLive();
    playbackControls(); if (ready && mode === 'recording') renderRecording();
  }
  function animate(now) {
    if (active && mode === 'recording' && ready && playing) {
      const delta = lastFrame ? Math.min((now - lastFrame) / 1000, 1) : 0;
      if (time >= data.meta.t_end) {
        if (endSince == null) endSince = now;
        if (now - endSince > 3000) { time = Math.max(0, data.chunks[0]?.t_cap || 0); endSince = null; }
      } else time = Math.min(data.meta.t_end, time + delta * Number(el('show-speed').value));
      if (now - lastPaint > 100) { renderRecording(); lastPaint = now; }
    }
    lastFrame = now; requestAnimationFrame(animate);
  }
  el('show-play').onclick = () => { playing = !playing; endSince = null; if (data && time >= data.meta.t_end) time = data.chunks[0]?.t_cap || 0; renderRecording(); };
  el('show-restart').onclick = () => seek(data?.chunks[0]?.t_cap || 0, false);
  el('show-scrub').oninput = () => seek(Number(el('show-scrub').value), true);
  el('show-recording').onchange = () => loadRecording(el('show-recording').value);
  el('show-mode').onchange = () => {
    mode = el('show-mode').value; ++generation; playing = false; data = null; ready = false; live = null;
    clearEvidence('正在切换数据来源'); makeChapters(); playbackControls();
    if (mode === 'recording') loadRecording(el('show-recording').value);
    else { renderLive({status: 'unconfigured', connected: false}); pollLive(); }
  };
  el('show-detail').onclick = () => { if (data && mode === 'recording') window.dispatchEvent(new CustomEvent('showcase-detail', {detail: {file, time}})); };
  window.addEventListener('keydown', event => {
    if (!active || mode !== 'recording' || !ready || event.repeat || event.target.isContentEditable || /^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(event.target.tagName)) return;
    if (event.code === 'Space') { event.preventDefault(); el('show-play').click(); }
    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') { event.preventDefault(); seek(time + (event.key === 'ArrowRight' ? 2 : -2)); }
  });
  async function init() {
    try {
      const response = await fetch('/api/replays'); if (!response.ok) throw new Error('unavailable');
      const result = await response.json();
      el('show-recording').replaceChildren(...result.replays.map((name, i) => {
        const option = document.createElement('option'); option.value = name;
        const match = name.match(/T(\d{2})(\d{2})(\d{2})/);
        option.textContent = `运行记录 ${String(i + 1).padStart(2, '0')}${match ? ` · ${match[1]}:${match[2]}:${match[3]} UTC` : ''}`; return option;
      }));
      if (!result.replays.length) throw new Error('empty');
      if (mode === 'recording') await loadRecording(result.replays[0]);
    } catch (_) {
      clearEvidence('未找到可用运行记录'); text('show-stage', '添加真实运行记录后可展示'); announce('运行记录不可用');
    }
  }
  setInterval(() => {
    if (mode === 'live' && live?.connected && live.age_s + (performance.now() - liveReceivedAt) / 1000 > 5) renderLive({...live, connected: false, status: 'stale'});
    pollLive();
  }, 1000);
  requestAnimationFrame(animate); init();
  return {activate, seek, get status() { return status; }, getState: () => ({active, mode, file, ready, time, playing, status, liveConnected: !!live?.connected})};
})();
