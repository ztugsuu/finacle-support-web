// Dependency-free browser checks. All API requests are served by this mock server.
// Run on Windows: node tests/browser_smoke.cjs (Chrome must be installed).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const {spawn, spawnSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const python = path.join(root, '.venv-local', 'Scripts', 'python.exe');
const rendered = spawnSync(python, ['-B', '-c', "import sys; sys.path.insert(0, 'tests'); from fixtures import authenticated_client; from app import app; print(authenticated_client(app).get('/').data.decode('utf-8'))"],
  {cwd: root, encoding: 'utf8', env: {...process.env, PYTHONIOENCODING: 'utf-8'}, windowsHide: true});
assert.equal(rendered.status, 0, rendered.stderr);
const renderedUat = spawnSync(python, ['-B', '-c', "import sys; sys.path.insert(0, 'tests'); from fixtures import authenticated_client; from app import app; print(authenticated_client(app).get('/?namespace=uat-finacle').data.decode('utf-8'))"],
  {cwd: root, encoding: 'utf8', env: {...process.env, PYTHONIOENCODING: 'utf-8'}, windowsHide: true});
assert.equal(renderedUat.status, 0, renderedUat.stderr);
let failPreview = false, applied = 0, rolloutReads = 0;
let restartAllowed = true, resets = 0, restartRequests = 0;
const tracking = {configmap: 'fnhttp-ac-env', restarted_at: 'mock-restart', names: ['fnhttpac']};
let zipMode = 'error', failZipBatch = 0;
const zipBodies = [];
const requests = [];
const scopedRequests = [];
const server = http.createServer((req, res) => {
  requests.push(req.url);
  if (req.url.startsWith('/api/') || req.url === '/restart') scopedRequests.push({url: req.url, namespace: req.headers['x-finacle-namespace']});
  if (req.url === '/' || req.url.startsWith('/?')) { res.setHeader('Content-Type', 'text/html; charset=utf-8'); return res.end(req.url.includes('namespace=uat-finacle') ? renderedUat.stdout : rendered.stdout); }
  if (['/static/app.js', '/static/app.css'].includes(req.url)) {
    res.setHeader('Content-Type', req.url.endsWith('.js') ? 'text/javascript' : 'text/css');
    return res.end(fs.readFileSync(path.join(root, req.url.slice(1))));
  }
  res.setHeader('Content-Type', 'application/json');
  let data;
  if (req.url.startsWith('/api/logs/download?')) {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      zipBodies.push(JSON.parse(body));
      if (zipMode === 'html') {
        res.statusCode = 504;
        res.setHeader('Content-Type', 'text/html');
        return res.end('<html><body>Gateway timeout</body></html>');
      }
      if (zipMode === 'error' || zipBodies.length === failZipBatch) {
        res.statusCode = 500;
        setTimeout(() => res.end(JSON.stringify({message: 'ZIP mock read failure'})), 1600);
      } else {
        res.setHeader('Content-Type', 'application/zip');
        setTimeout(() => res.end(Buffer.from('504b0506000000000000000000000000000000000000', 'hex')), 40);
      }
    });
    return;
  }
  if (req.url.startsWith('/api/log-folders?')) return res.end(JSON.stringify({folders:['FIVUSR','KHANDARMAA.B','REPORTS','UPLOAD']}));
  if (req.url.startsWith('/api/log-files?')) return res.end(JSON.stringify({files:['a.trc'],file_details:[{name:'a.trc',time:new Date().toISOString(),time_kind:'modified',size_bytes:3}]}));
  if (req.url.startsWith('/api/logs?')) return res.end(JSON.stringify({content:'abc',truncated:false,updated_at:new Date().toISOString()}));
  if (req.url === '/api/overview') data = {pods: [], deployments: [{name: 'fnhttpac', desired: 1, ready: 1,
    restart: {allowed: restartAllowed, reason: restartAllowed ? 'Stable' : 'Cooldown 120 сек', cooldown_seconds: restartAllowed ? 0 : 120}}], updated_at: new Date().toISOString()};
  else if (req.url === '/api/log-control') data = {configmaps: [{name: tracking.configmap, group: 'FNHTTP'}, {name: 'fnhttp-la-env', group: 'FNHTTP'}, {name: 'fip-env', group: 'FIP'}, {name: 'fnhttp-py-env', group: 'FNHTTP'}]};
  else if (req.url === '/restart') { restartRequests++; data = {message: 'Restart accepted (mock)'}; }
  else if (req.url.startsWith('/api/log-control/current?')) {
    const name = new URL(req.url, 'http://localhost').searchParams.get('configmap');
    const values = {
      'fnhttp-ac-env': {DEBUG_LOG_LEVEL: 'TRACE', DEBUG_LOG_USERS: 'alice', FINACLE_LOG4FIN_ENABLED: 'Y', GLOBAL_TRACE: 'ON'},
      'fnhttp-la-env': {DEBUG_LOG_LEVEL: 'NONE', DEBUG_LOG_USERS: 'NONE', FINACLE_LOG4FIN_ENABLED: 'N', GLOBAL_TRACE: 'OFF'},
      'fip-env': {FI_LOGLEVEL: 'NONE', LOG_LEVEL: 'WARN'},
      'fnhttp-py-env': {DEBUG_LOG_LEVEL: 'DEBUG', DEBUG_LOG_USERS: 'custom_user', FINACLE_LOG4FIN_ENABLED: 'Y', GLOBAL_TRACE: 'NONE'}
    };
    data = {name, values: values[name]};
  }
  else if (req.url === '/api/log-control/reset-preview') data = {namespace: req.headers['x-finacle-namespace'], mode: 'NORMAL', reset_all: true,
    configurations: [{configmap: 'fip-env', changes: [{key: 'LOG_LEVEL', before: 'DEBUG', after: 'WARN'}]}],
    deployments: [{name: 'fnhttpac'}], fingerprint: 'mock-reset'};
  else if (req.url === '/api/log-control/reset-apply') { resets++; data = {message: 'Defaults restored (mock)', tracking}; }
  else if (req.url === '/api/log-control/preview') {
    if (failPreview) { res.statusCode = 502; data = {message: 'Dry-run амжилтгүй (mock)'}; }
    else data = {namespace: 'pre-uat-finacle', configmap: tracking.configmap, mode: 'DEBUG', dry_run: true,
      fingerprint: 'mock', deployments: [{name: 'fnhttpac'}], changes: [{key: 'DEBUG_LOG_LEVEL', before: 'NONE', after: 'DEBUG'}]};
  } else if (req.url === '/api/log-control/apply') { applied++; data = {message: 'Restart requested (mock)', tracking}; }
  else if (req.url.startsWith('/api/log-control/rollout?')) {
    const complete = ++rolloutReads > 1;
    data = {complete, deployments: [{name: 'fnhttpac', desired: 1, updated: 1, ready: complete ? 1 : 0,
      state: complete ? 'ready' : 'progressing', message: complete ? 'Ready / Available' : 'Starting'}]};
  } else { res.statusCode = 404; data = {message: 'Unexpected mock request'}; }
  res.end(JSON.stringify(data));
});
let browser, socket;
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'finacle-browser-'));
const pending = new Map(), errors = [];
let sequence = 0;
async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  browser = spawn(process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=0',
      `--user-data-dir=${scratch}`, 'about:blank'], {windowsHide: true, stdio: ['ignore', 'ignore', 'pipe']});
  const wsAddress = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Chrome startup timeout')), 20000);
    browser.once('error', reject);
    browser.stderr.on('data', chunk => { const match = String(chunk).match(/DevTools listening on (ws:\/\/\S+)/); if (match) { clearTimeout(timer); resolve(match[1]); } });
  });
  const port = new URL(wsAddress).port;
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, {method: 'PUT'})).json();
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(resolve => socket.addEventListener('open', resolve, {once: true}));
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) { const callback = pending.get(message.id); pending.delete(message.id); callback?.(message); }
    if (message.method === 'Runtime.exceptionThrown') errors.push(message.params.exceptionDetails);
    if (message.method === 'Page.javascriptDialogOpening') call('Page.handleJavaScriptDialog', {accept: true});
  });
  await call('Runtime.enable'); await call('Page.enable');
  await call('Page.navigate', {url: `http://127.0.0.1:${server.address().port}/`});
  await waitFor("document.querySelector('#total')?.textContent === '0'");
  const visible = () => evaluate("[...document.querySelectorAll('[data-screen]')].filter(e => !e.hidden).map(e => e.dataset.screen)");
  assert.deepEqual(await visible(), ['overview', 'overview']);
  for (const page of ['logs', 'control', 'deployments', 'overview']) {
    await evaluate(`document.querySelector('[data-page="${page}"]').click()`);
    assert.ok((await visible()).every(p => p === page));
    assert.equal(await evaluate('window.scrollY'), 0);
    assert.equal(await evaluate("document.querySelector('[aria-current=page]').dataset.page"), page);
  }
  await evaluate("document.querySelector('[data-page=logs]').click(); renderFileChoices([{name:'test.stdout'}]); document.querySelector('#file-checkboxes input').click(); document.querySelector('#download-selected').click()");
  assert.equal(await evaluate("document.querySelector('#download-selected').disabled"), true);
  assert.equal(await evaluate("document.querySelector('#download-selected').getAttribute('aria-busy')"), 'true');
  assert.equal(await evaluate("document.querySelector('#bulk-message').hidden"), false);
  assert.ok(await evaluate("document.querySelector('#bulk-message').textContent.includes('ZIP бэлтгэж байна')"));
  await waitFor("document.querySelector('#bulk-message').textContent.includes('ZIP mock read failure')");
  assert.equal(await evaluate("document.querySelector('#bulk-message').classList.contains('error')"), true);
  assert.equal(await evaluate("document.querySelector('#download-selected').disabled"), false);
  zipMode = 'success'; zipBodies.length = 0;
  await evaluate("window.zipNames = []; const originalClick = HTMLAnchorElement.prototype.click; HTMLAnchorElement.prototype.click = function() { if (this.download) window.zipNames.push(this.download); else originalClick.call(this); }; renderFileChoices(Array.from({length:951}, (_,i) => ({name:'log'+i}))); document.querySelector('#files-all').click(); document.querySelector('#download-selected').click()");
  await waitFor("document.querySelector('#bulk-message').classList.contains('success')");
  assert.deepEqual(zipBodies.map(body => body.files.length), [100,100,100,100,100,100,100,100,100,51]);
  assert.equal(new Set(zipBodies.flatMap(body => body.files)).size, 951);
  assert.equal(await evaluate('window.zipNames.length'), 10);
  assert.ok(await evaluate("window.zipNames[0].includes('part-001-of-010') && window.zipNames[9].includes('part-010-of-010')"));
  assert.equal(await evaluate("document.querySelectorAll('#batch-downloads a').length"), 10);
  zipBodies.length = 0; failZipBatch = 2;
  await evaluate("document.querySelector('#download-selected').click()");
  await waitFor("document.querySelector('#bulk-message').classList.contains('error')");
  assert.equal(zipBodies.length, 2);
  assert.equal(await evaluate("document.querySelectorAll('#batch-downloads a').length"), 1);
  assert.ok(await evaluate("document.querySelector('#bulk-message').textContent.includes('2/10')"));
  zipMode = 'html'; failZipBatch = 0;
  await evaluate("document.querySelector('#download-selected').click()");
  await waitFor("document.querySelector('#bulk-message').textContent.includes('HTTP 504')");
  assert.ok(await evaluate("document.querySelector('#bulk-message').textContent.includes('Route/proxy')"));
  assert.equal(await evaluate("document.querySelector('#bulk-message').textContent.includes('Unexpected token')"), false);

  assert.equal(await evaluate("getComputedStyle(document.querySelector('#file-checkboxes label')).display"), 'flex');
  assert.ok(await evaluate("document.querySelector('#file-checkboxes').scrollHeight > document.querySelector('#file-checkboxes').clientHeight"));
  assert.ok(await evaluate("document.querySelector('#file-checkboxes').getBoundingClientRect().height <= 282"));
  assert.ok(await evaluate("document.querySelectorAll('#file-checkboxes label')[1].getBoundingClientRect().top > document.querySelectorAll('#file-checkboxes label')[0].getBoundingClientRect().top"));
  await call('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:true});
  await evaluate("renderFileChoices([{name:'long_filename_'.repeat(30)+'.trc'}])");
  assert.equal(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), true);
  await call('Emulation.clearDeviceMetricsOverride');

  await evaluate("podList = [{name:'fnhttpac-pod',source:'fnhttp',fnhttp_service:'ac',healthy:true,containers:['fnhttpac']}]; document.querySelector('[data-source=fnhttp]').click()");
  await waitFor("document.querySelector('#fnhttp-folder').options.length === 4 && document.querySelector('#file').value === 'a.trc'");
  await evaluate("document.querySelector('#file-checkboxes input').click(); document.querySelector('#fnhttp-folder').value='REPORTS'; document.querySelector('#fnhttp-folder').dispatchEvent(new Event('change'))");
  await waitFor("document.querySelector('#log-path').textContent.includes('/REPORTS/a.trc') && document.querySelector('#log-state').textContent.includes('Авто шинэчлэлт')");
  assert.equal(await evaluate('selectedFiles.size'), 0);
  assert.ok(requests.some(url => url.startsWith('/api/log-files?') && url.includes('folder=REPORTS')));
  await evaluate("history.back()");
  await waitFor("document.querySelector('[aria-current=page]').dataset.page === 'overview'");
  await evaluate('history.back()'); await waitFor("document.querySelector('[aria-current=page]').dataset.page === 'deployments'");
  await evaluate('history.forward()'); await waitFor("document.querySelector('[aria-current=page]').dataset.page === 'overview'");
  await evaluate("document.querySelector('[data-page=deployments]').click()");
  assert.equal(await evaluate("document.querySelector('#deploy-table button').disabled"), false);
  restartAllowed = false;
  await waitFor("document.querySelector('#deploy-table').textContent.includes('Cooldown')");
  assert.equal(await evaluate("document.querySelector('#deploy-table button').disabled"), true);
  await evaluate("document.querySelector('#deploy-table button').click()");
  assert.equal(restartRequests, 0);
  restartAllowed = true;
  await evaluate("document.querySelector('#refresh').click()");
  await waitFor("!document.querySelector('#deploy-table button').disabled");
  await evaluate("document.querySelector('#deploy-table button').click()");
  await waitFor("document.querySelector('#action-message').textContent.includes('Restart accepted')");
  assert.equal(restartRequests, 1);
  await waitFor("document.querySelector('#deploy-table button').disabled");
  await evaluate("document.querySelector('[data-page=control]').click(); document.querySelector('#control-refresh').click()");
  await waitFor("!document.querySelector('#control-preview').disabled");
  assert.equal(await evaluate("document.querySelector('#control-current').textContent.includes('TRACE')"), true);
  assert.equal(await evaluate("document.querySelector('#control-mode').value"), 'TRACE');
  assert.equal(await evaluate("document.querySelector('#control-user').value"), 'alice');
  await evaluate("document.querySelector('#control-user').value = 'bob'; document.querySelector('#control-user').dispatchEvent(new Event('input')); document.querySelector('#control-mode').value = 'DEBUG'; document.querySelector('#control-mode').dispatchEvent(new Event('change'))");
  await waitFor("!document.querySelector('#control-preview').disabled");
  assert.equal(await evaluate("document.querySelector('#control-mode').value"), 'DEBUG');
  assert.equal(await evaluate("document.querySelector('#control-user').value"), 'bob');
  for (const [name, mode, user] of [['fnhttp-la-env', 'NORMAL', 'NONE'], ['fip-env', 'NORMAL', ''], ['fnhttp-py-env', '', 'custom_user']]) {
    await evaluate(`document.querySelector('#control-config').value = '${name}'; document.querySelector('#control-config').dispatchEvent(new Event('change'))`);
    await waitFor(`document.querySelector('#control-current-status').textContent.includes('${name}')`);
    assert.equal(await evaluate("document.querySelector('#control-mode').value"), mode);
    assert.equal(await evaluate("document.querySelector('#control-user').value"), user);
    assert.equal(await evaluate("document.querySelector('#control-preview').disabled"), !mode);
  }
  await evaluate("document.querySelector('#control-config').value = 'fnhttp-ac-env'; document.querySelector('#control-config').dispatchEvent(new Event('change'))");
  await waitFor("!document.querySelector('#control-preview').disabled");
  await evaluate("document.querySelector('#control-preview').click()");
  await waitFor("!document.querySelector('#control-preview-panel').hidden && !document.querySelector('#control-apply').disabled");
  assert.equal(applied, 0);
  await evaluate("document.querySelector('#control-apply').click()");
  await waitFor("document.querySelector('#rollout-table').textContent.includes('Starting')");
  await evaluate("document.querySelector('#rollout-refresh').click()");
  await waitFor("document.querySelector('#rollout-summary').textContent.includes('Rollout дууссан')");
  assert.equal(applied, 1);
  failPreview = false;
  await evaluate("document.querySelector('#control-reset-all').click()");
  await waitFor("document.querySelector('#control-changes').textContent.includes('WARN') && !document.querySelector('#control-apply').disabled");
  assert.equal(resets, 0);
  await evaluate("document.querySelector('#control-apply').click()");
  await waitFor("document.querySelector('#control-message').textContent.includes('Defaults restored')");
  assert.equal(resets, 1);
  assert.ok(scopedRequests.every(r => r.namespace === 'pre-uat-finacle'));
  await evaluate("document.querySelector('#project-select').value = 'uat-finacle'; document.querySelector('#project-select').dispatchEvent(new Event('change'))");
  await waitFor("typeof namespace !== 'undefined' && namespace === 'uat-finacle' && document.querySelector('#total')?.textContent === '0'");
  const beforeSwitch = scopedRequests.length;
  await evaluate("document.querySelector('#control-refresh').click()");
  await waitFor("document.querySelector('#control-current').textContent.includes('TRACE')");
  assert.ok(scopedRequests.slice(beforeSwitch).every(r => r.namespace === 'uat-finacle'));
  assert.equal(await evaluate("document.querySelector('#control-preview-panel').hidden"), true);
  failPreview = true;
  await evaluate("document.querySelector('#control-preview').click()");
  await waitFor("document.querySelector('#control-message').textContent.includes('Dry-run амжилтгүй')");
  assert.equal(await evaluate("document.querySelector('#control-preview-panel').hidden"), true);
  assert.equal(applied, 1);
  await call('Emulation.setDeviceMetricsOverride', {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  assert.equal(await evaluate("getComputedStyle(document.querySelector('[data-page=control]')).display"), 'block');
  assert.equal(await evaluate('document.documentElement.scrollWidth <= window.innerWidth'), true);
  assert.deepEqual(errors, []);
  assert.ok(requests.some(url => url.startsWith('/api/logs?') && url.includes('folder=REPORTS')));
  console.log('PASS: screens, history, mobile, 5-second restart gate, current values, confirmed reset-all, namespace switching, ZIP loading/error feedback, 100-file batching and partial failure and rollout (mock APIs only).');
}
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++sequence;
    const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    pending.set(id, message => { clearTimeout(timeout); message.error ? reject(new Error(JSON.stringify(message.error))) : resolve(message.result); });
    socket.send(JSON.stringify({id, method, params}));
  });
}
async function evaluate(expression) {
  const result = await call('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function waitFor(expression) {
  for (let i = 0; i < 100; i++) { if (await evaluate(expression)) return; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw new Error(`Browser condition timed out: ${expression}`);
}
main().catch(error => { console.error(error); process.exitCode = 1; }).finally(async () => {
  socket?.close();
  if (browser && browser.exitCode === null) { browser.kill(); await new Promise(resolve => { browser.once('exit', resolve); setTimeout(resolve, 2000); }); }
  server.closeAllConnections(); server.close();
  if (path.dirname(scratch) === path.resolve(os.tmpdir()) && path.basename(scratch).startsWith('finacle-browser-')) {
    try { fs.rmSync(scratch, {recursive: true, force: true, maxRetries: 5, retryDelay: 200}); } catch { /* Chrome may still hold the temporary profile. */ }
  }
});
