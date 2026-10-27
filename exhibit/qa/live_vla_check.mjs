// Isolated real Chrome regression: local HTTP VLA fixture and temporary exhibit DB.
// No robot access and no QA report files are changed.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temp = await mkdtemp(join(tmpdir(), 'r1-live-vla-'));
const sleep = ms => new Promise(r => setTimeout(r, ms));
const pixel = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=';
const v = n => Array.from({length: 23}, (_, i) => n + i / 100);
let projection = {configured: true, connected: true, status: 'connected', age_s: 0, state: {
  robot_id: 'QA-local-only', session_id: 'qa-session', run_id: 'qa-run-1', operation_id: 'qa-operation',
  task: 'VLA 当前任务', model_id: 'qa-policy', phase: 'requesting', reason: null, legacy: false,
  request: {id: 'qa-request-1', status: 'started', attempt: 1, chunk: null, capture_at: 1700000000,
    started_at: 1700000001, response_at: null, state23: v(1), action23: null, rtt_ms: null,
    cot_text: null, inference_timing_ms: null, server_queue_and_compute_ms: null, images: null},
  execution: {issued_steps: 0, chunk: null, step: null, target23: null, feedback23: null, feedback_at: null, feedback_kind: null},
  verification: {status: 'pending', run_finished: false, stopped: false, home_success: null, reason: null, evidence: []},
  stats: {events: 2, requests: 1, responses: 0, failed_requests: 0, dropped: 0}, last_event_at: 1700000001
}};
let server, chrome, ws, proxy, serial = 0, apiMode = 'normal', cameraStage = 'camera phase', vlaRequests = 0, telemetryRequests = 0, upstream;
const pending = new Map(), errors = [];
async function waitFor(fn, name, timeout = 12000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) { if (await fn()) return; await sleep(75); }
  throw Error('Timeout: ' + name);
}
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++serial, timer = setTimeout(() => {pending.delete(id); reject(Error(method + ' timeout'));}, 15000);
    pending.set(id, {resolve: value => {clearTimeout(timer); resolve(value);}, reject});
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function run(expression) {
  const result = await call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
const read = id => run(`document.getElementById(${JSON.stringify(id)})?.textContent`);
const changeMode = mode => run(`document.getElementById('show-mode').value=${JSON.stringify(mode)};document.getElementById('show-mode').dispatchEvent(new Event('change'))`);
const cameraState = () => run(`['show-head','show-left','show-right'].map(id=>{const e=document.getElementById(id);return {src:e.getAttribute('src'),hidden:e.hidden,box:[e.parentElement.offsetLeft,e.parentElement.offsetTop,e.parentElement.offsetWidth,e.parentElement.offsetHeight]}})`);
const shared = ['show-task-name','show-stage','show-model','show-decision-note','show-observation','show-observation-value','show-prediction','show-prediction-value','show-feedback','show-feedback-value','show-rtt','show-gripper','show-result','show-result-detail','show-status','show-provenance','show-boundary'];
const recordingLabels = () => run(`({values:${JSON.stringify(shared)}.map(id=>document.getElementById(id).textContent),caption:document.querySelector('.show-chain>.show-caption').textContent,metric:document.querySelector('.show-metrics>div:last-child>span').textContent})`);
async function updated(name) { const before = vlaRequests; await waitFor(() => vlaRequests > before, name); await sleep(180); }
try {
  await writeFile(join(temp, 'snapshot.json'), JSON.stringify({schema_version: 1, robot_id: 'QA-local-only', run_id: 'qa-camera-run', task: 'camera task', model_id: 'camera-model', stage: 'camera phase', observed_at: Date.now() / 1000, images: {head: pixel, wrist_left: pixel, wrist_right: pixel}}));
  server = spawn(process.env.PYTHON || 'python3', [join(root, 'server.py'), '--port', '0', '--db', join(temp, 'fleet.sqlite3'), '--telemetry-file', join(temp, 'snapshot.json')], {stdio: ['ignore','pipe','pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  let output = '', stderr = ''; server.stdout.on('data', x => output += x); server.stderr.on('data', x => stderr += x);
  await waitFor(() => {if (server.exitCode !== null) throw Error(stderr); return /http:\/\/127\.0\.0\.1:\d+/.test(output);}, 'isolated server');
  upstream = output.match(/http:\/\/127\.0\.0\.1:\d+/)[0];
  proxy = createServer(async (req, res) => {
    try {
      if (req.url === '/api/vla/state') {
        vlaRequests++;
        if (apiMode === 'error') {res.writeHead(503); return res.end('{}');}
        if (apiMode === 'slow') await sleep(2300);
        res.writeHead(200, {'Content-Type': 'application/json'}); return res.end(JSON.stringify(projection));
      }
      // Camera source remains independent and fresh throughout VLA failures.
      if (req.url === '/api/telemetry') {
        telemetryRequests++;
        const response = await fetch(upstream + req.url), payload = await response.json();
        payload.connected = true; payload.status = 'connected'; payload.age_s = 0; payload.state.stage = cameraStage;
        res.writeHead(200, {'Content-Type':'application/json'}); return res.end(JSON.stringify(payload));
      }
      // Serve the new asset before the backend's whitelist lands.
      if (req.url === '/live_vla.js') {
        res.writeHead(200, {'Content-Type':'application/javascript'}); return res.end(await readFile(join(root, 'static/live_vla.js')));
      }
      const options = {method: req.method};
      if (req.method !== 'GET' && req.method !== 'HEAD') {
        const chunks = []; for await (const chunk of req) chunks.push(chunk);
        options.body = Buffer.concat(chunks);
        options.headers = {'Content-Type': req.headers['content-type'] || 'application/json'};
      }
      const response = await fetch(upstream + req.url, options);
      res.writeHead(response.status, {'Content-Type':response.headers.get('content-type') || 'application/json'}); res.end(Buffer.from(await response.arrayBuffer()));
    } catch {res.writeHead(502); res.end();}
  });
  await new Promise(r => proxy.listen(0, '127.0.0.1', r)); const url = 'http://127.0.0.1:' + proxy.address().port;
  const args = ['--headless=new','--no-first-run','--disable-background-networking','--remote-debugging-port=0','--user-data-dir=' + join(temp,'chrome'),'about:blank'];
  if (process.getuid?.() === 0) args.unshift('--no-sandbox');
  chrome = spawn(process.env.CHROME || '/usr/bin/google-chrome', args, {stdio:'ignore'});
  let debugPort;
  await waitFor(async () => {try {debugPort = (await readFile(join(temp,'chrome','DevToolsActivePort'),'utf8')).split('\n')[0]; return true;} catch {return false;}}, 'Chrome');
  const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open',r,{once:true}));
  ws.addEventListener('message', event => {const message=JSON.parse(event.data), request=pending.get(message.id); if(request){pending.delete(message.id);message.error?request.reject(message.error):request.resolve(message.result);} if(message.method==='Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);});
  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride', {width:1920,height:1080,deviceScaleFactor:1,mobile:false});
  await call('Page.navigate', {url}); await waitFor(() => run(`!!window.Showcase?.getState().ready`), 'recording baseline');
  await run(`Showcase.seek(0,true)`); const baseline = await recordingLabels();
  await changeMode('live'); await waitFor(() => run(`!!Showcase.getState().liveConnected`), 'live cameras');
  await waitFor(async () => (await read('show-task-name')) === 'VLA 当前任务', 'VLA task replaces snapshot placeholder');
  assert.match(await read('show-observation'), /qa-request-1/); assert.match(await read('show-prediction'), /等待|请求中/);
  assert.equal(await read('show-rtt'), '—');
  assert.match(await read('live-vla-input'), /1\.01/); assert.match(await read('live-vla-output'), /等待|尚无/);
  const cameras = await cameraState(); assert(cameras.every(c=>c.src===pixel&&!c.hidden));
  console.log('PASS current request/input and unknown metrics; three camera sources/layout retained');
  Object.assign(projection.state.request, {status:'responded',response_at:1700000002,action23:[v(2),v(3)],rtt_ms:123,cot_text:'<img src=x onerror=alert(1)> 抓取依据',inference_timing_ms:{total:81},server_queue_and_compute_ms:95,images:{head_rgb:pixel}}); projection.state.phase='response';
  await updated('model response');
  assert.equal(await read('show-prediction-value'),'2 步'); assert.equal(await read('show-rtt'),'123 ms');
  assert.match(await read('live-vla-output'), /2\.01/); assert.match(await read('live-vla-cot'), /<img src=x/);
  assert.equal(await run(`document.getElementById('live-vla-cot').children.length`),0);
  assert.match(await read('live-vla-timing'),/81/); assert.deepEqual(await cameraState(),cameras);
  assert.equal(await run(`document.getElementById('show-detail').disabled`),true);
  assert.equal(await run(`document.querySelectorAll('#live-vla-images img').length`),1);
  assert(await run(`(()=>{const chain=document.querySelector('.show-chain').getBoundingClientRect(),metrics=document.querySelector('.show-metrics').getBoundingClientRect(),caption=document.querySelector('.show-chain>.show-caption').getBoundingClientRect(),result=document.querySelector('.show-verification').getBoundingClientRect();return metrics.bottom<=chain.bottom&&caption.bottom<=chain.bottom&&chain.bottom<=result.top})()`),'live chain metrics and caption fit their card');
  assert(await run(`document.querySelector('.show-verification').getBoundingClientRect().bottom<=document.querySelector('.show-evidence').getBoundingClientRect().bottom+1`),'four existing regions fit before opening live details');
  await run(`document.getElementById('live-vla-details').open=true`); assert.deepEqual(await cameraState(),cameras);
  assert(await run(`(()=>{const e=document.querySelector('.show-evidence');return e.scrollHeight>e.clientHeight&&e.scrollWidth<=e.clientWidth+1})()`));
  if(process.env.LIVE_VLA_SCREENSHOT){const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});await writeFile(process.env.LIVE_VLA_SCREENSHOT,Buffer.from(shot.data,'base64'));}
  await run(`document.getElementById('live-vla-details').open=false`);
  console.log('PASS real model response/CoT/timing rendered safely inside live details');
  projection.state.request.action23=null; await updated('response without action');
  assert.match(await read('show-prediction'),/模型已返回/); assert.equal(await read('show-prediction-value'),'—'); assert.equal(await read('show-rtt'),'123 ms'); assert.match(await read('live-vla-cot'),/抓取依据/);
  projection.state.request.action23=[v(2),v(3)];
  Object.assign(projection.state.execution,{issued_steps:1,chunk:0,step:0,target23:v(4),feedback23:v(5),feedback_kind:'pre_publish',feedback_at:1700000002}); projection.state.phase='executing';
  await updated('issued target');
  assert.equal(await read('show-feedback-value'),'1 步'); assert.match(await read('live-vla-target'),/4\.01/); assert.match(await read('live-vla-feedback'),/发送前/);
  projection.state.execution.feedback_kind='recovery_feedback'; projection.state.execution.feedback23=v(6); projection.state.execution.feedback_at=1700000003;
  await updated('sampled feedback'); assert.match(await read('live-vla-feedback'),/独立采样/); assert.match(await read('live-vla-feedback'),/6\.01/);
  projection.age_s=8; await updated('VLA within freshness limit'); assert.doesNotMatch(await read('live-vla-status'),/过期/);
  projection.freshness_s=30; projection.age_s=20; await updated('custom VLA freshness limit'); assert.doesNotMatch(await read('live-vla-status'),/过期/);
  projection.freshness_s=15; projection.connected=false; projection.status='stale'; projection.age_s=18;
  await updated('stale recorded action'); assert.match(await read('live-vla-status'),/过期/); assert.match(await read('live-vla-output'),/2\.01/); assert.doesNotMatch(await read('show-stage'),/执行中/); assert.deepEqual(await cameraState(),cameras);
  projection.connected=true; projection.status='connected'; projection.age_s=0;
  Object.assign(projection.state.verification,{run_finished:true,home_success:true,evidence:[{kind:'release_detected',observed_at:1700000003,detail:'张开检测'}]}); projection.state.phase='finished';
  await updated('finished pending verification'); assert.match(await read('show-result'),/待.*核验|等待独立核验/); assert.match(await read('show-result-detail'),/结束/); assert.match(await read('live-vla-verification'),/回位/);
  console.log('PASS issued target, pre-publish/sample feedback and finished/home evidence stay distinct');
  Object.assign(projection.state.request,{id:'qa-request-2',attempt:2,status:'pending',action23:null,cot_text:null,inference_timing_ms:null,rtt_ms:null,images:null});
  await updated('retry'); assert.equal(await run(`document.querySelectorAll('#live-vla-images img').length`),0); assert.equal(await read('show-prediction-value'),'—'); assert.doesNotMatch(await read('live-vla-output'),/2\.01/); assert.doesNotMatch(await read('live-vla-cot'),/抓取依据/);
  // Failure must suppress even an inconsistent response payload from an adapter.
  Object.assign(projection.state.request,{status:'failed',action23:[v(8)],cot_text:'OLD RESPONSE'}); projection.state.phase='request_failed';
  await updated('failed retry'); assert.match(await read('show-prediction'),/失败/); assert.equal(await read('show-prediction-value'),'—'); assert.doesNotMatch(await read('live-vla-output'),/8\.01/); assert.doesNotMatch(await read('live-vla-cot'),/OLD RESPONSE/);
  console.log('PASS retry and failed request cannot show earlier actions or CoT');
  projection.state.run_id='qa-run-2'; projection.state.request={id:null,status:null,attempt:null,chunk:null,capture_at:null,started_at:null,response_at:null,state23:null,action23:null,rtt_ms:null,cot_text:null,inference_timing_ms:null,server_queue_and_compute_ms:null,images:null}; projection.state.execution={}; projection.state.verification={status:'pending',evidence:[]}; projection.state.phase='observing';
  await updated('new run'); assert.match(await read('show-prediction'),/尚无请求记录/); assert.doesNotMatch(await read('show-prediction'),/请求中/); assert.match(await read('show-provenance'),/qa-run-2/); assert.equal(await read('show-feedback-value'),'—'); assert.doesNotMatch(await read('live-vla-target'),/4\.01/); assert.doesNotMatch(await read('live-vla-verification'),/张开检测/);
  projection.connected=false; projection.status='stale'; projection.age_s=18;
  await updated('stale'); assert.match(await read('live-vla-status'),/过期/); assert.doesNotMatch(await read('show-stage'),/执行中/); assert.deepEqual(await cameraState(),cameras);
  projection.connected=true; projection.status='connected'; projection.age_s=0; projection.state.robot_id='different-robot'; projection.state.task='WRONG ROBOT TASK';
  await updated('identity mismatch'); assert.doesNotMatch(await read('show-task-name'),/WRONG ROBOT/); assert.match(await read('live-vla-status'),/身份不匹配/); assert.deepEqual(await cameraState(),cameras);
  console.log('PASS run reset, stale evidence and mismatched robot identity do not contaminate cameras');
  projection.state.robot_id='QA-local-only'; await updated('identity restored'); apiMode='error'; await updated('VLA unavailable'); assert.match(await read('live-vla-status'),/断开|失败|不可用/); assert.deepEqual(await cameraState(),cameras);
  apiMode='slow'; await updated('slow VLA request');
  await changeMode('recording'); await waitFor(() => run(`Showcase.getState().ready`),'recording restored'); await run(`Showcase.seek(0,true)`);
  assert.deepEqual(await recordingLabels(),baseline);
  await sleep(2400); assert.equal(await run(`Showcase.getState().mode`),'recording'); assert.deepEqual(await recordingLabels(),baseline);
  assert.equal(await run(`document.getElementById('live-vla-details').hidden`),true);
  assert.equal(await run(`document.getElementById('show-detail').disabled`),false);
  assert.deepEqual(errors,[]); assert(telemetryRequests>=3);
  console.log('PASS live→recording restores all shared evidence/labels; late VLA response ignored; no browser errors');
  apiMode='normal'; cameraStage='机器人执行中'; projection={configured:false,connected:false,status:'unconfigured',age_s:null,state:{robot_id:null,session_id:null,run_id:null,request:null,execution:{},verification:{status:'pending',evidence:[]}}};
  await call('Page.navigate',{url:url+'/?source=live'}); await waitFor(()=>run(`!!window.Showcase?.getState().liveConnected`),'camera without VLA'); await updated('unconfigured VLA');
  assert.equal(await read('show-task-name'),'camera task'); assert.doesNotMatch(await read('show-stage'),/执行中/);
  assert.match(await read('live-vla-status'),/未接入/); console.log('PASS camera-only source cannot imply VLA is executing');
} finally {
  ws?.close(); chrome?.kill('SIGTERM'); server?.kill('SIGINT'); proxy?.closeAllConnections(); if(proxy) await new Promise(r=>proxy.close(r));
  await sleep(300); await rm(temp,{recursive:true,force:true,maxRetries:5,retryDelay:200});
}
