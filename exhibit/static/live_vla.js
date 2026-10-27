'use strict';
// Independent, read-only VLA evidence. Never writes camera nodes or replay state.
window.LiveVLA = (() => {
  const el = id => document.getElementById(id);
  const put = (id, value) => { el(id).textContent = value; };
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const vector = v => Array.isArray(v) && v.length === 23 && v.every(finite);
  const actions = v => vector(v) ? [v] : Array.isArray(v) && v.length && v.every(vector) ? v : null;
  const json = v => JSON.stringify(v, null, 2);
  const ms = v => finite(v) ? Math.round(v) + ' ms' : '—';
  const stamp = v => finite(v) ? new Date(v * 1000).toLocaleTimeString('zh-CN', {hour12: false}) : '未知';
  const safeImage = value => typeof value === 'string' && /^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=\s]+$/.test(value);
  const ids = ['show-task-name', 'show-stage', 'show-model', 'show-decision-note', 'show-observation', 'show-observation-value', 'show-prediction', 'show-prediction-value', 'show-feedback', 'show-feedback-value', 'show-rtt', 'show-gripper', 'show-result', 'show-result-detail', 'show-status', 'show-provenance', 'show-boundary'];
  const selectors = ['.show-chain>.show-caption', '.show-metrics>div:last-child>span'];
  let enabled = false, revision = 0, timer = null, controller = null, saved = [], cameraRobot = null;
  let envelope = null, received = 0, lastIdentity = null;
  function resetDetails(message) {
    ['input', 'output', 'cot', 'timing', 'target', 'feedback', 'verification', 'source'].forEach(name => put('live-vla-' + name, message));
    el('live-vla-images').replaceChildren();
  }
  function requestImages(request) {
    const parent = el('live-vla-images'); parent.replaceChildren();
    const names = {head_rgb: '请求头部输入', left_wrist_rgb: '请求左腕输入', right_wrist_rgb: '请求右腕输入'};
    for (const [key, label] of Object.entries(names)) {
      const value = request?.images?.[key];
      if (!safeImage(value)) continue;
      const figure = document.createElement('figure'), img = document.createElement('img'), caption = document.createElement('figcaption');
      img.src = value; img.alt = label; caption.textContent = label; figure.append(img, caption); parent.append(figure);
    }
    if (!parent.children.length) parent.textContent = request ? '本次请求图像未就绪或未提供；现场相机独立刷新。' : '尚无请求图像';
  }
  function render() {
    if (!enabled || !envelope) return;
    const s = envelope.state?.robot_id ? envelope.state : null;
    const mismatch = s?.robot_id && cameraRobot && s.robot_id !== cameraRobot;
    const freshness = finite(envelope.freshness_s) && envelope.freshness_s > 0 ? envelope.freshness_s : 15;
    const stale = envelope.status === 'stale' || (finite(envelope.age_s) && envelope.age_s + (performance.now() - received) / 1000 > freshness);
    const connected = !!envelope.connected && !stale;
    const badge = mismatch ? 'VLA 与相机机器人身份不匹配' : stale ? 'VLA 证据已过期' : connected ? (s?.legacy ? 'VLA 只读 · 旧日志' : 'VLA 证据已连接') : envelope.status === 'unconfigured' ? 'VLA 事件源未接入' : 'VLA 连接断开 · 等待事件';
    put('live-vla-status', badge); el('live-vla-status').className = 'chip ' + (connected && !mismatch ? 'live' : 'warn');
    // Without VLA evidence, preserve the existing snapshot-only task presentation.
    if (!s && !lastIdentity) {
      put('show-stage', badge + ' · 当前执行阶段待事件确认');
      put('show-decision-note', '现场快照独立接收；尚无 VLA 请求与动作事件。');
      resetDetails('等待 VLA 事件'); return;
    }
    if (!s || mismatch) {
      ['show-observation-value', 'show-prediction-value', 'show-feedback-value', 'show-rtt', 'show-gripper'].forEach(id => put(id, '—'));
      put('show-task-name', mismatch ? '机器人身份待核对' : '等待 VLA 事件');
      put('show-stage', badge); put('show-model', '—');
      ['show-observation', 'show-prediction', 'show-feedback'].forEach(id => put(id, '等待同一台机器人的有效 VLA 事件'));
      put('show-result', '任务结果等待独立核验'); put('show-result-detail', '尚无匹配的运行证据。');
      put('show-decision-note', '请求、动作和结果只展示匹配机器人身份的事件。');
      put('show-provenance', badge); put('show-status', '证据不可用');
      resetDetails(badge); return;
    }
    const identity = [s.robot_id, s.session_id, s.run_id].join('\n');
    if (identity !== lastIdentity) { resetDetails('本次运行尚无记录'); lastIdentity = identity; }
    const r = typeof s.request?.id === 'string' && s.request.id.trim() ? s.request : null;
    const x = s.execution || {}, verification = s.verification || {};
    const failed = r && /failed|cancelled/.test(r.status || '');
    const waiting = r && /^(observed|started|pending|requesting|request_started)$/.test(r.status || '');
    const returned = r && !failed && !waiting && (r.status === 'responded' || actions(r.action23));
    const output = returned ? actions(r.action23) : null;
    const requestState = !r ? '尚无请求记录' : failed ? (r.status === 'cancelled' ? '请求已取消' : '请求失败') : returned ? '模型已返回' : r.status === 'observed' ? '输入已记录 · 等待模型结果' : '请求中 · 等待模型返回';
    const requestLabel = r ? `请求 ${r.id ?? '未知'} · 尝试 ${Number.isInteger(r.attempt) ? r.attempt : '未知'}` : '尚无请求记录';
    const phases = {waiting: '等待运行事件', run: '任务已开始', home: '回位过程', precheck: '准备检查', precheck_failed: '预检查未通过', precheck_finished: '预检查结果已记录', stop: '停止过程', observation: '输入已记录 · 等待模型结果', request_started: '请求中 · 等待模型返回', raw_response: '模型已返回 · 等待动作下发', target: '动作已下发 · 等待反馈核对', run_finished: '运行已结束 · 结果待独立核验', home_finished: '回位已结束 · 任务结果待核验', request_failed: '请求失败 · 等待重试', request_cancelled: '请求已取消', finished: '运行已结束 · 结果待独立核验', executing: '动作已下发 · 等待反馈核对', response: '模型已返回 · 等待动作下发', requesting: '请求中 · 等待模型返回', observing: '观察输入'};
    put('show-task-name', s.task || '未提供当前任务'); put('show-model', s.model_id || '—');
    put('show-status', stale ? '证据已过期' : connected ? 'VLA 实时证据' : 'VLA 已断开');
    put('show-stage', stale ? '证据已过期 · 保留最后记录' : !connected ? 'VLA 已断开 · 保留最后记录' : phases[s.phase] || s.phase || requestState);
    put('show-decision-note', returned && r.cot_text ? '模型返回了动作与 CoT；详见下方实时证据。' : '只读展示模型实际返回；缺少 CoT 时不生成解释。');
    put('show-observation', `${requestLabel}${r ? ' · 采集 ' + stamp(r.capture_at) : ''}`);
    put('show-observation-value', vector(r?.state23) ? '23 维' : '—');
    put('show-prediction', `${requestState}${r ? ' · ' + requestLabel : ''}`);
    put('show-prediction-value', output ? output.length + ' 步' : '—'); put('show-rtt', returned ? ms(r.rtt_ms) : '—');
    const sampled = x.feedback_kind === 'recovery_feedback';
    const prepublish = /^(prepublish|pre_publish)$/.test(x.feedback_kind || '');
    const feedbackLabel = sampled ? '独立采样反馈' : prepublish ? '本步指令发送前反馈' : '反馈来源未说明';
    const issued = Number.isInteger(x.issued_steps) && x.issued_steps >= 0;
    put('show-feedback-value', issued ? x.issued_steps + ' 步' : '—');
    put('show-feedback', `${vector(x.target23) ? `已下发 · 段 ${x.chunk ?? '未知'} / 步 ${x.step ?? '未知'}` : '尚无下发目标'} · ${vector(x.feedback23) ? feedbackLabel + ' ' + stamp(x.feedback_at) : '尚无反馈采样'}`);
    put('show-gripper', vector(x.feedback23) ? x.feedback23[15].toFixed(1) + '%' : '—');
    document.querySelector(selectors[0]).textContent = '下发目标、发送前反馈与独立采样分别记录；动作与夹爪条件不代表任务成功。';
    document.querySelector(selectors[1]).textContent = sampled ? '独立采样右爪开度' : prepublish ? '发送前右爪开度' : '右爪反馈开度';
    put('show-result', '任务结果等待独立核验');
    const lifecycle = [verification.run_finished ? '运行结束已记录' : '尚无运行结束记录', verification.stopped ? '停止已记录' : null, verification.home_success === true ? '回位成功已记录' : verification.home_success === false ? '回位未成功' : null].filter(Boolean);
    put('show-result-detail', lifecycle.join('；') + '；入筐与抓稳结果仍需独立证据。');
    put('show-provenance', `VLA · 机器人 ${s.robot_id || '未知'} · 会话 ${s.session_id || '未知'} · 运行 ${s.run_id || '未知'} · 操作 ${s.operation_id || '未知'}`);
    put('show-boundary', '现场相机独立刷新 · 请求图像见实时证据');
    put('live-vla-input', `${requestLabel}\n状态：${requestState}\n采集：${stamp(r?.capture_at)} · 开始：${stamp(r?.started_at)}\n${vector(r?.state23) ? json(r.state23) : '未提供 state23'}`);
    put('live-vla-output', output ? `${requestLabel}\n返回：${stamp(r.response_at)} · chunk ${r.chunk ?? '未知'}\n${json(r.action23)}` : `${requestState}；尚无本次请求的有效 action23`);
    put('live-vla-cot', returned && typeof r.cot_text === 'string' && r.cot_text ? r.cot_text : '本次请求未提供 CoT');
    put('live-vla-timing', returned ? `请求往返：${ms(r.rtt_ms)}\n模型推理耗时：${r.inference_timing_ms == null ? '未提供' : json(r.inference_timing_ms)}\n服务端排队与计算：${ms(r.server_queue_and_compute_ms)}` : '本次请求尚无耗时数据');
    put('live-vla-target', vector(x.target23) ? `累计下发：${issued ? x.issued_steps : '未知'} 步 · chunk ${x.chunk ?? '未知'} / step ${x.step ?? '未知'}\n${json(x.target23)}` : '本次运行尚无下发目标记录');
    put('live-vla-feedback', vector(x.feedback23) ? `${feedbackLabel} · 采样 ${stamp(x.feedback_at)}\n${json(x.feedback23)}` : '本次运行尚无反馈记录');
    put('live-vla-verification', `${lifecycle.join('；')}\n任务结果：待独立核验${verification.reason ? '\n原因：' + verification.reason : ''}\n${Array.isArray(verification.evidence) && verification.evidence.length ? json(verification.evidence) : '本次运行尚无结果证据'}`);
    put('live-vla-source', `阶段：${s.phase || '未知'}${s.reason ? '\n原因：' + s.reason : ''}\n${s.legacy ? '旧日志：缺少生命周期时不推测' : '事件通道'}\n最后事件：${stamp(s.last_event_at)} · 年龄 ${finite(envelope.age_s) ? envelope.age_s.toFixed(1) + ' s' : '未知'}\n${json(s.stats || {})}`);
    requestImages(r);
  }
  async function poll(token) {
    if (!enabled || token !== revision) return;
    const requestController = new AbortController(); controller = requestController;
    const timeout = setTimeout(() => requestController.abort(), 1600);
    try {
      const response = await fetch('/api/vla/state', {signal: requestController.signal});
      if (!response.ok) throw new Error('VLA unavailable');
      const result = await response.json();
      if (!result || typeof result !== 'object') throw new Error('invalid VLA state');
      if (!enabled || token !== revision) return;
      envelope = result; received = performance.now(); render();
    } catch (_) {
      if (!enabled || token !== revision) return;
      // Keep evidence for this identity when the independent event channel fails.
      envelope = {...(envelope || {}), connected: false, status: 'error'}; render();
    } finally {
      clearTimeout(timeout);
      if (enabled && token === revision) {controller = null; timer = setTimeout(() => poll(token), 1000);}
    }
  }
  function enter() {
    if (enabled) return;
    enabled = true; ++revision; envelope = null; cameraRobot = null; lastIdentity = null;
    saved = [...ids.map(id => el(id)), ...selectors.map(s => document.querySelector(s))].map(node => [node, node.textContent]);
    el('live-vla-details').hidden = false; el('live-vla-status').hidden = false;
    document.querySelector('.show-evidence').setAttribute('data-live-vla', '');
    put('live-vla-status', 'VLA 等待事件'); resetDetails('等待 VLA 事件'); poll(revision);
  }
  function leave() {
    if (!enabled) return;
    enabled = false; ++revision; clearTimeout(timer); controller?.abort(); controller = null;
    saved.forEach(([node, value]) => {node.textContent = value;}); saved = [];
    el('live-vla-details').hidden = true; el('live-vla-details').open = false; el('live-vla-status').hidden = true;
    document.querySelector('.show-evidence').removeAttribute('data-live-vla');
    envelope = null; lastIdentity = null; cameraRobot = null; resetDetails('等待 VLA 事件');
  }
  function camera(result) {
    if (!enabled) return;
    cameraRobot = result.state?.robot_id || cameraRobot;
    render();
  }
  return {enter, leave, camera};
})();
