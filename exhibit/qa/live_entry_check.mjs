// URL-entry regression: real local exhibit service, temporary telemetry/DB/profile.
// Chrome and the service use ephemeral ports; no existing exhibit is contacted.
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {createServer} from 'node:http';
import {mkdtemp, readFile, writeFile, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, resolve, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const temp = await mkdtemp(join(tmpdir(), 'r1-live-entry-'));
const snapshot = join(temp, 'snapshot.json'), sleep = ms => new Promise(r => setTimeout(r, ms));
let server, chrome, ws, proxy, serial = 0, replayMode = 'normal', upstream;
const pending = new Map(), errors = [];
async function waitFor(fn, name, timeout = 15000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) { if (await fn()) return; await sleep(75); }
  throw Error('Timeout: ' + name);
}
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++serial, timer = setTimeout(() => { pending.delete(id); reject(Error(method + ' timeout')); }, 15000);
    pending.set(id, {resolve: value => {clearTimeout(timer); resolve(value);}, reject});
    ws.send(JSON.stringify({id, method, params}));
  });
}
async function run(expression) {
  const result = await call('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true});
  assert(!result.exceptionDetails, JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function freshSnapshot() {
  await writeFile(snapshot, JSON.stringify({schema_version: 1, robot_id: 'QA-local-only', run_id: 'QA-entry', task: 'QA URL entry', model_id: 'QA-model', stage: 'QA observing', observed_at: Date.now() / 1000, images: {}}));
}
try {
  await freshSnapshot();
  server = spawn(process.env.PYTHON || 'python3', [join(root, 'server.py'), '--port', '0', '--db', join(temp, 'fleet.sqlite3'), '--telemetry-file', snapshot], {stdio: ['ignore', 'pipe', 'pipe'], env: {...process.env, PYTHONDONTWRITEBYTECODE: '1'}});
  let output = '', stderr = '';
  server.stdout.on('data', x => output += x); server.stderr.on('data', x => stderr += x);
  await waitFor(() => { if (server.exitCode !== null) throw Error(stderr); return /http:\/\/127\.0\.0\.1:\d+/.test(output); }, 'isolated service');
  upstream = output.match(/http:\/\/127\.0\.0\.1:\d+/)[0];
  proxy = createServer(async (req, res) => {
    try {
      if (req.url === '/api/replays' && replayMode !== 'normal') {
        const mode = replayMode;
        await sleep(1200); // Live must render before this unrelated request finishes.
        res.writeHead(mode === 'error' ? 503 : 200, {'Content-Type': 'application/json'});
        return res.end(JSON.stringify(mode === 'error' ? {error: 'QA unavailable'} : {replays: []}));
      }
      const response = await fetch(upstream + req.url);
      res.writeHead(response.status, {'Content-Type': response.headers.get('content-type') || 'application/json'});
      res.end(Buffer.from(await response.arrayBuffer()));
    } catch { res.writeHead(502); res.end(); }
  });
  await new Promise(r => proxy.listen(0, '127.0.0.1', r));
  const url = 'http://127.0.0.1:' + proxy.address().port;
  const args = ['--headless=new', '--no-first-run', '--disable-background-networking', '--remote-debugging-port=0', '--user-data-dir=' + join(temp, 'chrome'), 'about:blank'];
  if (process.getuid?.() === 0) args.unshift('--no-sandbox');
  chrome = spawn(process.env.CHROME || '/usr/bin/google-chrome', args, {stdio: 'ignore'});
  let debugPort;
  await waitFor(async () => {try {debugPort = (await readFile(join(temp, 'chrome', 'DevToolsActivePort'), 'utf8')).split('\n')[0]; return true;} catch {return false;}}, 'Chrome');
  const pages = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
  ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise(r => ws.addEventListener('open', r, {once: true}));
  ws.addEventListener('message', event => {
    const message = JSON.parse(event.data), request = pending.get(message.id);
    if (request) {pending.delete(message.id); message.error ? request.reject(message.error) : request.resolve(message.result);}
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
  });
  await call('Page.enable'); await call('Runtime.enable');
  for (const query of ['', '?source=recording', '?source=unknown']) {
    await call('Page.navigate', {url: url + '/' + query});
    await waitFor(() => run(`!!window.Showcase?.getState().ready`), 'default recording');
    assert.equal(await run(`Showcase.getState().mode`), 'recording');
    assert.equal(await run(`document.getElementById('show-mode').value`), 'recording');
  }
  console.log('PASS absent, recording and unknown source preserve recording default');
  for (const mode of ['normal', 'empty', 'error']) {
    replayMode = mode; await freshSnapshot();
    await call('Page.navigate', {url: url + '/?source=live'});
    await waitFor(() => run(`!!window.Showcase?.getState().liveConnected`), 'URL automatically connects live (' + mode + ')', 5000);
    await sleep(1400);
    assert.equal(await run(`Showcase.getState().mode`), 'live');
    assert.equal(await run(`document.getElementById('show-mode').value`), 'live');
    assert.equal(await run(`document.getElementById('show-task-name').textContent`), 'QA URL entry');
    assert.equal(await run(`document.getElementById('show-recording').disabled`), true);
    assert.equal(await run(`Showcase.getState().ready || Showcase.getState().playing`), false);
    console.log('PASS live URL with ' + mode + ' replay list');
  }
  replayMode = 'normal'; await rm(snapshot);
  await call('Page.navigate', {url: url + '/?source=live'});
  await waitFor(() => run(`window.Showcase?.getState().mode==='live' && document.getElementById('show-source-label').textContent==='等待实时快照'`), 'missing snapshot stays in live');
  assert(await run(`['show-head','show-left','show-right'].every(id=>document.getElementById(id).hidden&&!document.getElementById(id).hasAttribute('src'))`));
  await run(`document.getElementById('show-mode').value='recording';document.getElementById('show-mode').dispatchEvent(new Event('change'))`);
  await waitFor(() => run(`Showcase.getState().ready && Showcase.getState().mode==='recording'`), 'manual return to recording');
  assert.deepEqual(errors, []);
  console.log('PASS missing live source has no replay frames; manual recording switch works; no browser exceptions');
} finally {
  ws?.close(); chrome?.kill('SIGTERM'); server?.kill('SIGINT'); proxy?.closeAllConnections();
  if (proxy) await new Promise(r => proxy.close(r));
  await sleep(300); await rm(temp, {recursive: true, force: true, maxRetries: 5, retryDelay: 200});
}
