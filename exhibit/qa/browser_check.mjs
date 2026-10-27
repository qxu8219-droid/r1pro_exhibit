// Development-only QA. No Node dependency is needed by the exhibit itself.
// node --experimental-websocket exhibit/qa/browser_check.mjs
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
const here = dirname(fileURLToPath(import.meta.url)), root = resolve(here, '..');
const temp = await mkdtemp(join(tmpdir(), 'r1-exhibit-qa-'));
const chromePath = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = ms => new Promise(r => setTimeout(r, ms));
let server, chrome, ws, serial = 0;
const pending = new Map(), errors = [], checks = [];
async function waitFor(fn, label, ms = 10000) {
  const until = Date.now() + ms;
  while (Date.now() < until) { if (await fn()) return; await sleep(100); }
  throw new Error('Timeout: ' + label);
}
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++serial;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(method + ' timed out')); }, 15000);
    pending.set(id, {resolve: value => {clearTimeout(timer); resolve(value);}, reject});
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function run(expression) {
  const r = await call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
async function screenshot(name) {
  const r = await call('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false});
  await writeFile(join(here, name + '_1920x1080.png'), Buffer.from(r.data, 'base64'));
}
function pass(name, detail) { checks.push({name, detail}); console.log('PASS ' + name); }
try {
  server = spawn(process.env.PYTHON || 'python3', [join(root, 'server.py'), '--port', '0', '--db', join(temp, 'qa.sqlite3')], {stdio: ['ignore', 'pipe', 'pipe']});
  let output = ''; server.stdout.on('data', b => output += b.toString());
  await waitFor(() => /http:\/\/127\.0\.0\.1:\d+/.test(output), 'server');
  const url = output.match(/http:\/\/127\.0\.0\.1:\d+/)[0];
  chrome = spawn(chromePath, ['--headless=new', '--window-size=1920,1080', '--no-first-run', '--no-default-browser-check', '--disable-background-networking', '--remote-debugging-port=0', '--user-data-dir=' + join(temp, 'profile'), 'about:blank'], {stdio: 'ignore'});
  let debugPort;
  await waitFor(async () => { try { debugPort = (await readFile(join(temp, 'profile', 'DevToolsActivePort'), 'utf8')).split('\n')[0]; return true; } catch { return false; } }, 'Chrome');
  const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r, {once: true}));
  ws.addEventListener('message', event => {
    const m = JSON.parse(event.data);
    if (m.id) { const p = pending.get(m.id); pending.delete(m.id); if (p) m.error ? p.reject(m.error) : p.resolve(m.result); }
    if (m.method === 'Runtime.exceptionThrown') errors.push(m.params.exceptionDetails);
  });
  await call('Page.enable'); await call('Runtime.enable');
  await call('Emulation.setDeviceMetricsOverride', {width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false});
  await call('Page.navigate', {url:url+'/#fleet'});
  await waitFor(() => run('typeof world!=="undefined" && !!world && world.sim_time >= 6'), 'fleet execution');
  await screenshot('fleet_live');
  const bounds = await run(`['floor','side','controls'].map(id => {const r=$(id).getBoundingClientRect();return {id,x:r.x,y:r.y,right:r.right,bottom:r.bottom};})`);
  assert(bounds.every(r => r.x >= 0 && r.y >= 0 && r.right <= 1920 && r.bottom <= 1080));
  pass('1920x1080 fleet fits', bounds);
  const roster = await run(`({cards:document.querySelectorAll('.robot-card').length,map:document.querySelectorAll('#robot-layer > g').length,engine:world.robots.map(r=>r.id),names:[...document.querySelectorAll('.robot-head')].map(el=>el.textContent),phases:document.querySelectorAll('#mission-track .step').length,overflow:$('robots').getBoundingClientRect().bottom>$('floor').getBoundingClientRect().bottom})`);
  assert.equal(roster.cards,4);assert.equal(roster.map,4);assert.deepEqual(roster.engine,['R1','R2','R3','R4']);assert.equal(roster.phases,8);assert(!roster.overflow);
  assert(roster.names[0].includes('R1 Pro · 01'));assert(roster.names[1].includes('R1 Pro · 02'));assert(roster.names[2].includes('R1 Lite · 01'));assert(roster.names[3].includes('R1 Lite · 02'));
  pass('four actual robot states with eight relay phases',roster);
  await run(`$('autodemo').checked=false; mutate('/api/control',{action:'pause'})`);
  await waitFor(() => run('!busy && !autoBusy'), 'pause');
  // Drive the real API, not render fixtures, through both Lite legs and return.
  await run(`(async()=>{for(let i=0;i<100;i++){const r=await mutate('/api/control',{action:'step'});if(!r)throw Error('step rejected');if(world.mission.stage==='handoff_end_full' && world.robots.find(r=>r.id==='R4').progress>=.5)return;}throw Error('Lite 02 never transported the cart');})()`);
  await screenshot('fleet_live');
  const cycle = await run(`(async()=>{for(let i=0;i<150 && world.mission.status!=='SUCCEEDED';i++)await mutate('/api/control',{action:'step'});return {status:world.mission.status,cart:world.carts[0],steps:world.tasks.filter(t=>t.status==='SUCCEEDED').map(t=>({robot:t.robot_id,skill:t.skill})),reserved:world.stations.some(s=>s.reserved_by),cards:[...document.querySelectorAll('.robot-card')].map(el=>el.textContent)};})()`);
  assert.equal(cycle.status,'SUCCEEDED');assert.equal(cycle.cart.load,'empty');assert.equal(cycle.cart.station_id,'SOURCE');assert(!cycle.cart.held_by);assert(!cycle.reserved);
  assert.equal(cycle.steps.length,22);
  for(const rid of ['R1','R2','R3','R4'])for(const skill of ['acquire_cart','place_cart'])assert.equal(cycle.steps.filter(t=>t.robot===rid&&t.skill===skill).length,2);
  assert(cycle.cards.every(t=>/本轮完成 [4-6] 项/.test(t)));
  pass('real four-robot API completes full and empty relay with all cards updated',cycle);
  await run(`rearmAuto();$('autodemo').checked=true;doneSince=Date.now()-9000;void autoDemo();`);
  await waitFor(()=>run(`!autoBusy && !busy && world.scenario==='blocked_handoff' && world.runtime.running`),'four-robot automatic next scenario');
  assert.deepEqual(await run(`world.robots.map(r=>r.id)`),['R1','R2','R3','R4']);
  pass('automatic scenario reset retains all four active participants');
  await run(`$('autodemo').checked=false;mutate('/api/control',{action:'pause'});`);
  await waitFor(()=>run('!busy && !autoBusy'),'pause after four-robot reset');
  // Real browser render fixtures, isolated from simulator poll responses.
  await run(`window.originalFetch=window.fetch; window.qaState=structuredClone(world); window.qaPosts=[];
    window.fetch=async (path,opts)=>{if(path==='/api/state')return new Response(JSON.stringify(qaState));if(opts){qaPosts.push(JSON.parse(opts.body));throw new Error('QA deliberate request failure');}return originalFetch(path,opts);};
    qaState.epoch='qa-motion';qaState.runtime.running=true;qaState.runtime.last_error=null;qaState.events=[];
    qaState.tasks=[{id:'T-qa',robot_id:'R1',source_station_id:'SOURCE',station_id:'HANDOFF_FULL',status:'RUNNING',progress:0,skill:'place_cart'}];
    Object.assign(qaState.robots[0],{active_task_id:'T-qa',held_cart_id:'C17',station_id:'SOURCE',online:true,location_known:true,status:'RUNNING'});
    Object.assign(qaState.carts[0],{held_by:'R1',station_id:null});
    qaState.stations.forEach(s=>{s.cart_id=null;s.blocked=false;s.reserved_by=s.id==='HANDOFF_FULL'?'T-qa':null;});
    render(structuredClone(qaState));qaState.tasks[0].progress=1;render(structuredClone(qaState));`);
  const start = await run(`robotViews.get('R1').x`); await sleep(180);
  const midway = await run(`({x:robotViews.get('R1').x,to:robotViews.get('R1').toX,carts:document.querySelectorAll('[data-cart="C17"]').length,held:!!document.querySelector('[data-robot="R1"] [data-cart="C17"]'),reservation:document.querySelector('[data-station="HANDOFF_FULL"]').textContent})`);
  assert(midway.x > start && midway.x < midway.to); assert.equal(midway.carts, 1); assert(midway.held); assert(midway.reservation.includes('Pro 01 预约'));
  pass('interpolated motion + attached cart + reservation name', midway);
  await run(`qaState.events.push({id:1001,type:'TASK_DISPATCHED',time:4,message:'QA new event'});render(structuredClone(qaState));`);
  assert(await run(`!!document.querySelector('#evlist .event-new')`)); pass('new event highlight');
  await run(`qaState.epoch='qa-pickup';qaState.robots[0].held_cart_id=null;qaState.carts[0].held_by=null;qaState.carts[0].station_id='HANDOFF_FULL';qaState.tasks[0].progress=0;render(structuredClone(qaState));
    qaState.tasks[0].progress=1;qaState.robots[0].station_id='HANDOFF_FULL';qaState.robots[0].held_cart_id='C17';qaState.carts[0].held_by='R1';qaState.carts[0].station_id=null;render(structuredClone(qaState));`);
  assert.equal(await run(`document.querySelectorAll('[data-cart="C17"]').length`),1);
  pass('pickup receipt never hides the cart between layers');
  // Failed start is attempted once even across repeated poll renders.
  await run(`qaState.runtime.running=false;qaState.tasks=[];qaState.robots.forEach(r=>Object.assign(r,{active_task_id:null,status:'IDLE',online:true,location_known:true}));qaState.mission.status='READY';render(structuredClone(qaState));rearmAuto();$('autodemo').checked=true;autoDemo();`);
  await waitFor(() => run('!autoBusy && !busy'), 'failed start');
  await run(`(async()=>{for(let i=0;i<8;i++){render(structuredClone(qaState));await autoDemo();}})()`);
  assert.equal(await run(`qaPosts.filter(p=>p.action==='start').length`), 1); pass('failed start is not retried by polls');
  await run(`rearmAuto();qaPosts=[];qaState.robots[0].online=false;qaState.robots[0].status='UNKNOWN';qaState.robots[0].location_known=false;qaState.mission.status='BLOCKED';render(structuredClone(qaState));`);
  await run(`(async()=>{for(let i=0;i<8;i++)await autoDemo();})()`);
  assert.equal(await run(`qaPosts.filter(p=>p.action==='start').length`), 0); pass('offline/UNKNOWN never starts');
  // Failed automatic reset must not dispatch start, and must not loop reset.
  await run(`rearmAuto();qaPosts=[];qaState.robots.forEach(r=>Object.assign(r,{online:true,status:'IDLE',location_known:true}));qaState.mission.status='SUCCEEDED';render(structuredClone(qaState));doneSince=Date.now()-9000;autoDemo();`);
  await waitFor(() => run('!autoBusy && !busy'), 'failed reset');
  await run(`(async()=>{for(let i=0;i<8;i++)await autoDemo();})()`);
  assert.deepEqual(await run(`qaPosts.map(p=>p.action)`), ['reset']); pass('reset failure stops carousel sequence');
  await run(`rearmAuto();qaPosts=[];qaState.mission.status='READY';qaState.runtime.last_error='QA model failed';render(structuredClone(qaState));`);
  await run(`(async()=>{for(let i=0;i<8;i++)await autoDemo();})()`);
  assert.equal(await run(`qaPosts.length`),0); pass('planner error blocks automatic restart');
  await run(`window.fetch=async(path,opts)=>{if(opts){qaPosts.push(JSON.parse(opts.body));return new Promise(resolve=>{window.releaseReset=()=>{qaState.epoch='qa-reset';qaState.mission.status='READY';resolve(new Response(JSON.stringify({result:{ok:true},state:qaState})));};});}return new Response(JSON.stringify(qaState));};
    rearmAuto();qaPosts=[];qaState.runtime.last_error=null;qaState.mission.status='SUCCEEDED';render(structuredClone(qaState));doneSince=Date.now()-9000;void autoDemo();`);
  await waitFor(()=>run(`typeof releaseReset==='function'`),'pending reset');
  await run(`$('autodemo').checked=false;$('autodemo').onchange();releaseReset();`);
  await waitFor(()=>run('!autoBusy && !busy'),'cancel reset sequence');
  assert.deepEqual(await run(`qaPosts.map(p=>p.action)`),['reset']);pass('unchecking auto during reset prevents start');
  await run(`$('autodemo').checked=false;window.fetch=originalFetch;showNotice('');show('replay');`);
  await waitFor(() => run('replayReady'), 'replay ready');
  await sleep(400);
  // Keys sent into the focused iframe use the actual browser event path.
  await call('Input.dispatchKeyEvent', {type:'keyDown',key:'1',code:'Digit1',windowsVirtualKeyCode:49});
  await call('Input.dispatchKeyEvent', {type:'keyUp',key:'1',code:'Digit1',windowsVirtualKeyCode:49});
  await waitFor(() => run(`screen==='fleet'`), 'iframe key1');
  await call('Input.dispatchKeyEvent', {type:'keyDown',key:'2',code:'Digit2',windowsVirtualKeyCode:50});
  await call('Input.dispatchKeyEvent', {type:'keyUp',key:'2',code:'Digit2',windowsVirtualKeyCode:50});
  await waitFor(() => run(`screen==='replay'`), 'shell key2');
  await sleep(200); // Let the asynchronously posted restart reach the iframe.
  pass('iframe key1 → shell key2 with browser keyboard events');
  await run(`$('replay-frame').contentWindow.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',code:'ArrowRight'}));`);
  await sleep(100); const before = await run(`$('replay-frame').contentDocument.getElementById('clock').textContent`);
  await run(`show('fleet');show('replay');`); await sleep(120);
  const after = await run(`$('replay-frame').contentDocument.getElementById('clock').textContent`);
  assert(parseFloat(before) >= 2); assert(parseFloat(after) < 1); pass('replay resets on re-entry', {before, after});
  await run(`$('rotate').checked=true;$('rotate-sec').value='45';lastSwitch=Date.now()-46000;`);
  await waitFor(() => run(`screen==='showcase'`), 'carousel to showcase');
  await run(`lastSwitch=Date.now()-46000;`);
  await waitFor(() => run(`screen==='fleet'`), 'carousel to fleet');
  await run(`lastSwitch=Date.now()-46000;`);
  await waitFor(() => run(`screen==='replay'`), 'carousel to replay');
  assert(parseFloat(await run(`$('replay-frame').contentDocument.getElementById('clock').textContent`)) < 1.5);
  pass('automatic carousel restarts replay');
  await run(`$('rotate').checked=false;const w=$('replay-frame').contentWindow;for(let i=0;i<9;i++)w.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',code:'ArrowRight'}));w.dispatchEvent(new KeyboardEvent('keydown',{key:' ',code:'Space'}));`);
  await sleep(200); await screenshot('replay_live');
  const replayLayout = await run(`(()=>{const d=$('replay-frame').contentDocument,w=$('replay-frame').contentWindow;return {width:w.innerWidth,height:w.innerHeight,overflow:d.documentElement.scrollWidth>w.innerWidth,footer:d.querySelector('footer').getBoundingClientRect().bottom,metric:d.getElementById('tracking').textContent};})()`);
  assert(!replayLayout.overflow); assert(replayLayout.footer <= replayLayout.height); pass('embedded replay fits', replayLayout);
  // Independently calculate the displayed metric from the recorded targets.
  // Exercise both real runs, including the second run's delayed requests.
  const recordings = (await (await fetch(url+'/api/replays')).json()).replays;
  for (const [recordIndex, file] of recordings.entries()) {
    const data = (await (await fetch(url+'/api/evidence?file='+encodeURIComponent(file))).json()).data;
    await run(`$('replay-list').value=${JSON.stringify(file)};loadReplay();`);
    await waitFor(() => run('replayReady'), 'recording for metric check');
    const at = recordIndex === 0 ? 40 : 70;
    await run(`$('replay-frame').contentWindow.postMessage({type:'replay-control',action:'seek',time:${at},paused:true},location.origin)`);
    await waitFor(() => run(`parseFloat($('replay-frame').contentDocument.getElementById('clock').textContent)===${at}`), 'paused metric sample');
    const current = data.chunks.filter(c=>c.t_resp<=at).at(-1);
    const rows = data.targets.filter(row=>row[1]===current.i && row[0]<=at);
    const differences = rows.flatMap(row=>Array.from({length:14},(_,d)=>Math.abs(row[3][d]-row[4][d])));
    const expected = (differences.reduce((a,b)=>a+b,0)/differences.length).toFixed(3);
    const actual = await run(`(()=>{const d=$('replay-frame').contentDocument;return {value:d.getElementById('tracking-value').textContent,steps:+d.getElementById('tracking').dataset.steps,prediction:d.getElementById('prediction-label').textContent,note:d.getElementById('tracking').title};})()`);
    assert.equal(actual.value,expected);assert.equal(actual.steps,rows.length);
    assert(actual.prediction.includes('32 步'));assert(actual.note.includes('未作时序对齐'));
    pass('recording '+(recordIndex+1)+' arm MAE matches recorded targets and 32-step label', {at,expected,...actual});
    const sorted = data.chunks.map(c=>c.rtt).sort((a,b)=>a-b), middle=Math.floor(sorted.length/2);
    const limit=(sorted.length%2 ? sorted[middle] : (sorted[middle-1]+sorted[middle])/2)*1.5;
    const outliers=[];
    for(const [i,c] of data.chunks.entries()) {
      const center=(c.t_cap+(data.chunks[i+1]?.t_cap??data.meta.t_end))/2;
      const tip=await run(`(()=>{const d=$('replay-frame').contentDocument,w=$('replay-frame').contentWindow,r=d.getElementById('tc').getBoundingClientRect();d.getElementById('timeline').dispatchEvent(new w.MouseEvent('mousemove',{clientX:r.left+10+${center/data.meta.t_end}*(r.width-20),clientY:r.top+35}));return d.getElementById('tip').textContent;})()`);
      assert(tip.includes('第 '+(c.i+1)+' 段'));
      assert(tip.includes(c.rtt>limit?'RTT 异常':'RTT 正常'));
      assert(tip.includes(limit.toFixed(3)+' s'));
      if(c.rtt>limit)outliers.push(c.i+1);
    }
    await run(`$('replay-frame').contentDocument.getElementById('timeline').dispatchEvent(new Event('mouseleave'))`);
    await screenshot('replay_recording_'+(recordIndex+1));
    pass('recording '+(recordIndex+1)+' every timeline RTT classification matches median times 1.5',{segments:data.chunks.length,limit,outliers});
  }
  await call('Page.navigate', {url:url+'/#replay'});
  await waitFor(() => run(`typeof replayReady!=='undefined' && replayReady && screen==='replay'`), 'direct replay hash'); pass('direct #replay loads iframe');
  assert.deepEqual(errors, []); pass('no browser JavaScript exceptions');
  await writeFile(join(here, 'browser_report.json'), JSON.stringify({checked_at:new Date().toISOString(),platform:process.platform,checks},null,2));
} finally {
  ws?.close(); chrome?.kill('SIGTERM'); server?.kill('SIGINT');
  await sleep(500); await rm(temp,{recursive:true,force:true,maxRetries:5,retryDelay:200});
}
