// Development QA only: Node 20 --experimental-websocket, no installed packages.
// Uses an isolated local simulator DB and Chrome profile; never contacts a robot.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

const here = dirname(fileURLToPath(import.meta.url)), root = resolve(here, '..');
const temp = await mkdtemp(join(tmpdir(), 'r1-showcase-qa-'));
const telemetryPath = join(temp, 'snapshot.json');
const chromePath = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let server, chrome, ws, serial = 0;
const pending = new Map(), errors = [], checks = [];

async function waitFor(fn, label, timeout = 15000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) { if (await fn()) return; await sleep(100); }
  throw new Error('Timeout: ' + label);
}
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++serial;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(method + ' timed out')); }, 15000);
    pending.set(id, {
      resolve: value => { clearTimeout(timer); resolve(value); },
      reject: value => { clearTimeout(timer); reject(value); }
    });
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function run(expression) {
  const result = await call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function screenshot(name) {
  const result = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  await writeFile(join(here, name + '.png'), Buffer.from(result.data, 'base64'));
}
function pass(name, detail) { checks.push({name, detail}); console.log('PASS ' + name); }
const change = (id, value, event = 'change') => run(`(()=>{const el=document.getElementById(${JSON.stringify(id)});el.value=${JSON.stringify(value)};el.dispatchEvent(new Event(${JSON.stringify(event)},{bubbles:true}));})()`);
const click = id => run(`document.getElementById(${JSON.stringify(id)}).click()`);
const clock = () => run(`parseFloat(document.getElementById('show-clock').textContent)`);
async function key(key, code, keyCode) {
  for (const type of ['keyDown', 'keyUp']) await call('Input.dispatchKeyEvent', {type, key, code, windowsVirtualKeyCode: keyCode});
}
async function setViewport(width, height) {
  await call('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
  await sleep(150);
}
async function disconnectedLive() {
  const state = await run(`({state:Showcase.getState(),images:['show-head','show-left','show-right'].map(id=>{const el=document.getElementById(id);return {id,hidden:el.hidden,src:el.getAttribute('src')};}),metrics:['show-observation-value','show-prediction-value','show-feedback-value','show-rtt','show-gripper'].map(id=>document.getElementById(id).textContent),resolution:document.getElementById('show-resolution').textContent,label:document.getElementById('show-source-label').textContent,task:document.getElementById('show-task-name').textContent,controls:['show-play','show-scrub','show-detail'].map(id=>document.getElementById(id).disabled)})`);
  assert(state.images.every(image=>image.hidden&&image.src===null));
  assert(state.metrics.every(value=>value==='—'));
  assert(state.controls.every(Boolean));
  assert.equal(state.resolution,'—');
  assert.equal(state.state.mode,'live');
  assert.equal(state.state.liveConnected,false);
  assert.equal(state.state.playing,false);
  return state;
}

try {
  server = spawn(process.env.PYTHON || 'python3', [join(root, 'server.py'), '--port', '0', '--db', join(temp, 'qa.sqlite3'), '--telemetry-file', telemetryPath], {stdio: ['ignore', 'pipe', 'pipe']});
  let output = '', serverErrors = '';
  server.stdout.on('data', chunk => output += chunk.toString());
  server.stderr.on('data', chunk => serverErrors += chunk.toString());
  await waitFor(() => { if (server.exitCode !== null) throw new Error(serverErrors); return /http:\/\/127\.0\.0\.1:\d+/.test(output); }, 'local service');
  const url = output.match(/http:\/\/127\.0\.0\.1:\d+/)[0];
  const files = (await (await fetch(url + '/api/replays')).json()).replays;
  assert(files.length >= 2, 'This regression fixture expects two real recordings');
  const records = await Promise.all(files.slice(0, 2).map(async file => (await (await fetch(url + '/api/evidence?file=' + encodeURIComponent(file))).json()).data));
  const missingTelemetry = await (await fetch(url + '/api/telemetry')).json();
  assert.equal(missingTelemetry.connected, false);
  assert.equal(missingTelemetry.status, 'missing');
  chrome = spawn(chromePath, ['--headless=new', '--window-size=1920,1080', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--remote-debugging-port=0', '--user-data-dir=' + join(temp, 'profile'), 'about:blank'], {stdio:'ignore'});
  let debugPort;
  await waitFor(async () => { try { debugPort = (await readFile(join(temp, 'profile', 'DevToolsActivePort'), 'utf8')).split('\n')[0]; return true; } catch { return false; } }, 'headless Chrome');
  const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  ws = new WebSocket(pages.find(page => page.type === 'page').webSocketDebuggerUrl);
  await new Promise(resolve => ws.addEventListener('open', resolve, {once:true}));
  ws.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) {
      const request = pending.get(message.id); pending.delete(message.id);
      if (request) message.error ? request.reject(message.error) : request.resolve(message.result);
    }
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
  });
  await call('Page.enable'); await call('Runtime.enable');
  await setViewport(1920, 1080);
  await call('Page.navigate', {url});
  await waitFor(() => run(`typeof screen!=='undefined' && screen==='showcase' && document.getElementById('show-recording').options.length>=2`), 'default exhibition screen and recordings');
  await waitFor(() => run(`Showcase.getState().ready && !document.getElementById('show-head').hidden`), 'first recording frames');
  await click('show-play');
  assert.equal(await run('Showcase.getState().playing'), false);
  pass('default screen loads a real recording and can pause', await run('Showcase.getState()'));

  const bounds = await run(`['.show-heading','.show-vision','.show-task','.show-chain','.show-verification','.show-bottom'].map(selector=>{const r=document.querySelector(selector).getBoundingClientRect();return {selector,x:r.x,y:r.y,right:r.right,bottom:r.bottom};})`);
  assert(bounds.every(rect=>rect.x>=0 && rect.y>=0 && rect.right<=1921 && rect.bottom<=1081), JSON.stringify(bounds));
  await screenshot('showcase_1920x1080');
  pass('all exhibition panels fit at 1920x1080', bounds);

  // Request the second real run but hold its response; no old frames may survive.
  await run(`window.qaOriginalFetch=window.fetch;window.qaReleaseRecording=null;
    window.fetch=async(...args)=>{const response=await qaOriginalFetch(...args);if(String(args[0])==='/api/evidence?file='+encodeURIComponent(${JSON.stringify(files[1])})){return new Promise(resolve=>{window.qaReleaseRecording=()=>resolve(response);});}return response;};`);
  await change('show-recording', files[1]);
  await waitFor(()=>run(`typeof qaReleaseRecording==='function'`),'delayed second recording');
  assert(await run(`['show-head','show-left','show-right'].every(id=>{const el=document.getElementById(id);return el.hidden&&!el.hasAttribute('src');})`));
  assert.equal(await run(`document.getElementById('show-feedback-value').textContent`), '—');
  pass('recording switch clears previous frames and counts while loading');
  await change('show-recording', files[0]);
  await run(`qaReleaseRecording();window.fetch=qaOriginalFetch;`);
  await sleep(300);
  assert.equal(await run('Showcase.getState().file'), files[0]);
  assert(await run(`document.getElementById('show-provenance').textContent.includes(${JSON.stringify(records[0].meta.run_dir)})`));
  pass('late response from another recording cannot replace the selected run');

  for (let recordIndex=0; recordIndex<2; recordIndex++) {
    const record=records[recordIndex], file=files[recordIndex];
    await change('show-recording',file);
    await waitFor(()=>run(`Showcase.getState().ready && Showcase.getState().file===${JSON.stringify(file)}`),'record '+recordIndex);
    await change('show-scrub', Math.max(0,record.chunks[0].t_cap)+0.2,'input');
    const currentTime=await clock();
    const cap=record.chunks.filter(chunk=>chunk.t_cap<=currentTime).at(-1);
    assert(cap,'captured frame exists at selected time');
    for (const [id,key] of [['show-head','head_rgb'],['show-left','left_wrist_rgb'],['show-right','right_wrist_rgb']]) {
      const matches=await run(`document.getElementById(${JSON.stringify(id)}).getAttribute('src')===${JSON.stringify(cap.img[key])}`);
      assert(matches,`${id} must come from selected real recording ${recordIndex}`);
    }
    pass('three cameras match selected recording '+(recordIndex+1),{file,time:currentTime});
  }

  await change('show-recording',files[0]);
  await waitFor(()=>run(`Showcase.getState().ready && Showcase.getState().file===${JSON.stringify(files[0])}`),'first run restored');
  const record=records[0], grasp=record.events.find(event=>event.kind==='grasp_detected'), release=record.events.find(event=>event.kind==='release_detected');
  assert(grasp && release,'regression record contains the demonstrated two key events');
  // Jump ahead and then back: UI must recompute evidence at the selected time.
  await run(`document.querySelector('#show-chapters [data-chapter="3"]').click()`);
  assert.match(await run(`document.getElementById('show-stage').textContent`),/张开指令/);
  assert(Math.abs(await clock()-release.t)<0.11);
  assert.match(await run(`document.querySelector('#show-moment span').textContent`),/缺少张开后的相机画面和执行反馈/);
  await screenshot('showcase_release_1920x1080');
  pass('release chapter explains missing post-command images and feedback');

  await change('show-scrub',Math.max(0,grasp.t-0.3),'input');
  const before=await run(`({time:Showcase.getState().time,stage:document.getElementById('show-stage').textContent,count:document.getElementById('show-feedback-value').textContent,prediction:document.getElementById('show-prediction-value').textContent})`);
  assert(!/闭合条件已触发|张开指令已记录|结束/.test(before.stage));
  assert.equal(parseInt(before.count,10),record.targets.filter(row=>row[0]<=before.time).length);
  const response=record.chunks.filter(chunk=>chunk.t_resp<=before.time).at(-1);
  assert.equal(before.prediction,response?response.action.length+' 步':'—');
  await run(`document.querySelector('#show-chapters [data-chapter="2"]').click()`);
  assert.match(await run(`document.getElementById('show-stage').textContent`),/闭合条件已触发/);
  await change('show-scrub',0,'input');
  assert(!/闭合条件已触发|张开指令已记录/.test(await run(`document.getElementById('show-stage').textContent`)));
  assert.equal(parseInt(await run(`document.getElementById('show-feedback-value').textContent`),10),record.targets.filter(row=>row[0]<=0).length);
  pass('chapters and backward seek never present future execution evidence',before);

  await change('show-scrub',grasp.t+0.5,'input');
  const deepTime=await clock();
  await click('show-detail');
  await waitFor(()=>run(`screen==='replay' && replayReady`),'same-time model detail');
  await sleep(250);
  const detail=await run(`({source:document.getElementById('replay-frame').getAttribute('src'),clock:document.getElementById('replay-frame').contentDocument.getElementById('clock').textContent,play:document.getElementById('replay-frame').contentDocument.getElementById('play').textContent})`);
  assert(detail.source.includes(encodeURIComponent(files[0])) || detail.source.includes(files[0]));
  assert(Math.abs(parseFloat(detail.clock)-deepTime)<0.2,JSON.stringify({deepTime,detail}));
  assert.equal(detail.play,'播放');
  pass('detail view opens the same recording, timestamp and paused state',{deepTime,...detail});
  await run(`document.getElementById('replay-frame').contentWindow.postMessage({type:'replay-control',action:'seek',time:${release.t},paused:true},location.origin)`);
  await waitFor(()=>run(`document.getElementById('replay-frame').contentDocument.getElementById('grip-note').textContent.includes('缺少张开后的画面和反馈')`),'release limitation in detail');
  await screenshot('replay_release_1920x1080');
  await run(`document.getElementById('replay-frame').contentWindow.postMessage({type:'replay-control',action:'seek',time:${deepTime},paused:true},location.origin)`);
  await waitFor(()=>run(`!document.getElementById('replay-frame').contentDocument.getElementById('grip-note').textContent.includes('缺少张开后')`),'clear release note on backward seek');
  pass('detail release notice appears and clears on backward seek');

  await run(`document.getElementById('replay-frame').contentWindow.focus()`);
  await key('3','Digit3',51);
  await waitFor(()=>run(`screen==='showcase'`),'iframe key3 returns to exhibition');
  pass('iframe key3 returns to exhibition through real keyboard event');
  // A queued shortcut from a now-hidden iframe must not undo a newer choice.
  await run(`show('fleet');window.dispatchEvent(new MessageEvent('message',{origin:location.origin,source:document.getElementById('replay-frame').contentWindow,data:{switchScreen:'3'}}));`);
  assert.equal(await run('screen'),'fleet','hidden iframe must not switch the active screen');
  pass('late shortcut from hidden iframe cannot switch back');
  for (let i=0;i<12;i++) {
    await key('3','Digit3',51); await key('1','Digit1',49);
    assert.equal(await run('screen'),'fleet');
    assert.deepEqual(await run(`[...document.querySelectorAll('.screen')].filter(s=>getComputedStyle(s).display!=='none').map(s=>s.id)`),['fleet-screen']);
  }
  await sleep(900);
  assert.equal(await run('screen'),'fleet');
  pass('twelve real keyboard 3-to-1 switches keep one stable visible screen');
  await run(`show('replay');show('showcase');show('fleet')`);
  await sleep(120);
  assert.equal(await run('screen'),'fleet');
  assert.notEqual(await run(`document.activeElement.id`),'replay-frame');
  pass('rapid 2-to-3-to-1 cancels deferred iframe focus');
  assert.equal(await run(`document.getElementById('rotate-sec').disabled`),true);
  await run(`lastSwitch=Date.now()-200000;document.getElementById('rotate').click()`);
  await sleep(1100);
  assert.equal(await run('screen'),'fleet');
  assert.equal(await run(`document.getElementById('rotate-sec').disabled`),false);
  await run(`lastSwitch=Date.now()-46000;document.getElementById('rotate-sec').value='45';document.getElementById('rotate-sec').dispatchEvent(new Event('change'))`);
  await sleep(1100);
  assert.equal(await run('screen'),'fleet');
  await run(`lastSwitch=Date.now()-46000`);
  await waitFor(()=>run(`screen==='replay'`),'automatic rotation after full interval');
  await click('rotate');
  assert.equal(await run(`document.getElementById('rotate-sec').disabled`),true);
  await run(`show('showcase')`);
  pass('rotation waits a full interval after enabling or changing interval');


  await change('show-scrub',5,'input');
  await click('show-play');
  await sleep(200);
  await run(`show('fleet')`);
  const pausedTime=await run('Showcase.getState().time');
  await sleep(600);
  assert.equal(await run('Showcase.getState().time'),pausedTime);
  assert.equal(await run('Showcase.getState().playing'),false);
  pass('leaving exhibition pauses playback',{pausedTime});
  await run(`show('showcase')`);
  await waitFor(()=>run(`!document.getElementById('show-head').hidden`),'recording visible before live switch');
  await change('show-mode','live');
  await waitFor(()=>run(`document.getElementById('show-source-label').textContent==='等待实时快照'`),'missing live snapshot');
  const disconnected=await disconnectedLive();
  await screenshot('showcase_live_unconnected_1920x1080');
  pass('missing live snapshot contains no recording frames or counts',disconnected);

  // Synthetic local-only telemetry. These QA identities and copied image payloads
  // live exclusively in the temporary directory and are deleted in finally.
  const fixtureImages=record.chunks.find(chunk=>chunk.img?.head_rgb&&chunk.img?.left_wrist_rgb&&chunk.img?.right_wrist_rgb).img;
  const fixture={schema_version:1,robot_id:'QA-only',run_id:'QA-only-synthetic-run',task:'QA-only local telemetry fixture',model_id:'QA-only-fixture-model',stage:'QA-only synthetic observation',observed_at:Date.now()/1000,images:{head:fixtureImages.head_rgb,wrist_left:fixtureImages.left_wrist_rgb,wrist_right:fixtureImages.right_wrist_rgb},request_id:42,rtt_ms:12};
  await writeFile(telemetryPath,JSON.stringify(fixture));
  await waitFor(()=>run(`Showcase.getState().liveConnected`),'fresh synthetic local telemetry');
  const connected=await run(`({state:Showcase.getState(),provenance:document.getElementById('show-provenance').textContent,label:document.getElementById('show-source-label').textContent,task:document.getElementById('show-task-name').textContent,observation:document.getElementById('show-observation-value').textContent,rtt:document.getElementById('show-rtt').textContent})`);
  assert.equal(connected.state.mode,'live');
  assert.equal(connected.label,'只读实时观察');
  assert.equal(connected.task,fixture.task);
  assert(connected.provenance.includes(fixture.robot_id)&&connected.provenance.includes(fixture.run_id));
  assert.equal(connected.observation,'3 路');
  assert.equal(connected.rtt,'12 ms');
  for (const [id,key] of [['show-head','head'],['show-left','wrist_left'],['show-right','wrist_right']]) {
    assert(await run(`!document.getElementById(${JSON.stringify(id)}).hidden && document.getElementById(${JSON.stringify(id)}).getAttribute('src')===${JSON.stringify(fixture.images[key])}`));
  }
  pass('fresh local QA-only telemetry shows matching identity, three cameras and RTT',connected);

  // A stalled request must respect the UI's AbortController timeout and never
  // allow overlapping polls or leave the previously visible camera behind.
  await run(`window.qaHung={calls:0,active:0,maxActive:0,aborted:0,started:0};
    window.fetch=(path,options)=>{if(String(path)!=='/api/telemetry')return qaOriginalFetch(path,options);
      qaHung.calls++;qaHung.active++;qaHung.maxActive=Math.max(qaHung.maxActive,qaHung.active);qaHung.started=performance.now();
      return new Promise((resolve,reject)=>{const abort=()=>{qaHung.active--;qaHung.aborted++;reject(new DOMException('QA-only stalled telemetry','AbortError'));};
        if(options.signal.aborted)abort();else options.signal.addEventListener('abort',abort,{once:true});});};`);
  await waitFor(()=>run('qaHung.calls>0'),'stalled telemetry request');
  await waitFor(()=>run(`!Showcase.getState().liveConnected && document.getElementById('show-source-label').textContent==='实时源读取失败'`),'abort timeout clears live frame',4000);
  const abortedRequest=await run(`({...qaHung,elapsed:performance.now()-qaHung.started})`);
  assert.equal(abortedRequest.maxActive,1);
  assert.equal(abortedRequest.aborted,1);
  assert(abortedRequest.elapsed>=1500 && abortedRequest.elapsed<2400,JSON.stringify(abortedRequest));
  const afterAbort=await disconnectedLive();
  await run('window.fetch=qaOriginalFetch');
  fixture.observed_at=Date.now()/1000;
  await writeFile(telemetryPath,JSON.stringify(fixture));
  await waitFor(()=>run(`Showcase.getState().liveConnected && !document.getElementById('show-head').hidden`),'fresh telemetry after abort');
  pass('hung telemetry reaches the 1600 ms abort deadline, clears evidence and recovers',{request:abortedRequest,cleared:afterAbort.state,recovered:await run('Showcase.getState()')});

  // Deliberately ignore abort in this local fetch fixture to exercise the
  // independent displayed-frame age guard even if a request never settles.
  await run(`window.qaDelayed={calls:0,active:0,maxActive:0};window.qaReleaseTelemetry=null;
    window.fetch=async(path,options)=>{if(String(path)!=='/api/telemetry')return qaOriginalFetch(path,options);
      qaDelayed.calls++;qaDelayed.active++;qaDelayed.maxActive=Math.max(qaDelayed.maxActive,qaDelayed.active);
      const response=await qaOriginalFetch(path);return new Promise(resolve=>{window.qaReleaseTelemetry=()=>{qaDelayed.active--;resolve(response);};});};`);
  await waitFor(()=>run(`typeof qaReleaseTelemetry==='function'`),'nonsettling telemetry fixture');
  await waitFor(()=>run(`!Showcase.getState().liveConnected && document.getElementById('show-source-label').textContent==='实时数据已过期'`),'independent frame-age guard',9000);
  const delayedRequest=await run('({...qaDelayed})');
  const clearedAge=Date.now()/1000-fixture.observed_at;
  assert.equal(delayedRequest.calls,1);
  assert.equal(delayedRequest.maxActive,1);
  assert(clearedAge>5 && clearedAge<6.8,JSON.stringify({clearedAge,delayedRequest}));
  const afterDelay=await disconnectedLive();
  await run('window.fetch=qaOriginalFetch;qaReleaseTelemetry()');
  fixture.observed_at=Date.now()/1000;
  await writeFile(telemetryPath,JSON.stringify(fixture));
  await waitFor(()=>run(`Showcase.getState().liveConnected && !document.getElementById('show-head').hidden`),'fresh telemetry after delayed response');
  pass('independent age guard clears old live images during a nonsettling single-flight request and recovers',{request:delayedRequest,clearedAge,cleared:afterDelay.state,recovered:await run('Showcase.getState()')});

  fixture.observed_at=Date.now()/1000-6;
  await writeFile(telemetryPath,JSON.stringify(fixture));
  await waitFor(()=>run(`document.getElementById('show-source-label').textContent==='实时数据已过期'`),'stale synthetic local telemetry');
  pass('six-second stale telemetry clears images and counts without reverting to recording',await disconnectedLive());
  // Restore a visible fresh frame before corruption so the clearing is exercised
  // independently of the previous stale-data transition.
  fixture.observed_at=Date.now()/1000;
  await writeFile(telemetryPath,JSON.stringify(fixture));
  await waitFor(()=>run(`Showcase.getState().liveConnected && !document.getElementById('show-head').hidden`),'fresh telemetry before corruption');
  await writeFile(telemetryPath,'{"schema_version": 1, "QA-only": broken JSON');
  await waitFor(()=>run(`document.getElementById('show-source-label').textContent==='实时数据格式异常'`),'corrupt synthetic local telemetry');
  pass('corrupt local JSON clears a previously visible live frame and all counts',await disconnectedLive());

  await change('show-mode','recording');
  await waitFor(()=>run('Showcase.getState().ready'),'restore recording after live');
  await change('show-scrub',grasp.t+1,'input');
  assert.equal(await run('Showcase.getState().mode'),'recording');
  assert.equal(await run('Showcase.getState().playing'),false);
  assert.equal(await run(`document.getElementById('show-prediction-value').textContent`),'32 步');
  assert(parseInt(await run(`document.getElementById('show-feedback-value').textContent`),10)>0);
  assert(await run(`!document.getElementById('show-head').hidden`));
  await screenshot('showcase_1920x1080');
  await screenshot('showcase_action_1920x1080');
  pass('recording resumes normally after missing, fresh, stale and corrupt live data',await run('Showcase.getState()'));
  for (const [width,height] of [[1100,800],[960,800]]) {
    await setViewport(width,height);
    await run(`window.scrollTo(0,0)`);
    const size=await run(`({viewport:innerWidth,scroll:document.documentElement.scrollWidth,body:document.body.scrollWidth,sections:[...document.querySelectorAll('#showcase-screen .card,.show-heading,.topbar')].map(el=>{const r=el.getBoundingClientRect();return {name:el.className,x:r.x,right:r.right};})})`);
    assert(size.scroll<=width+1 && size.body<=width+1,JSON.stringify(size));
    assert(size.sections.every(rect=>rect.x>=0&&rect.right<=width+1),JSON.stringify(size));
    await screenshot(`showcase_${width}x${height}`);
    await run(`window.scrollTo(0,document.documentElement.scrollHeight)`);
    await sleep(100);
    const bottom=await run(`({scrollY,bottom:document.querySelector('.show-bottom').getBoundingClientRect().bottom,height:innerHeight})`);
    assert(bottom.scrollY>0 && bottom.bottom<=height+1,JSON.stringify(bottom));
    pass(`narrow ${width}x${height} has no horizontal overflow and bottom controls are reachable`,bottom);
  }
  assert.deepEqual(errors,[]);
  pass('no browser JavaScript exceptions');
  await writeFile(join(here, 'showcase_report.json'), JSON.stringify({checked_at:new Date().toISOString(),platform:process.platform,checks},null,2));
} finally {
  ws?.close(); chrome?.kill('SIGTERM'); server?.kill('SIGINT');
  await sleep(500); await rm(temp,{recursive:true,force:true,maxRetries:5,retryDelay:200});
}
