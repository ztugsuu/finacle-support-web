'use strict';
const $ = id => document.getElementById(id);
const sources = JSON.parse($('sources').textContent);
const namespace = $('project-select').value;
let restartBusy = false;
const localRestarts = new Map();
$('project-select').addEventListener('change', () => {
  if (restartBusy || controlBusy) { $('project-select').value = namespace; return; }
  const target = new URL(location.href);
  target.searchParams.set('namespace', $('project-select').value);
  location.assign(target.href);
});
let source = 'fip', podList = [], content = '', logTimer, logRequest, generation = 0, overviewBusy = false;
let activeScreen = 'overview';
const pages = {
  overview: ['Системийн ерөнхий хяналт', 'Үйлчилгээ болон pod-уудын төлөв.'],
  logs: ['Log хяналт', 'FIP болон FNBRNMPY log файлуудыг унших, хайх, татах.'],
  control: ['Log control', 'Log түвшнийг сонгож, dry-run шалгасны дараа өөрчлөлтөө баталгаажуулна.'],
  deployments: ['Deployments', 'Deployment-ийн төлөв болон restart үйлдэл.']
};
function showPage(focus = false) {
  const aliases = {'#overview': 'overview', '#log-section': 'logs', '#log-control-section': 'control', '#deploy-section': 'deployments'};
  const route = aliases[location.hash] || location.hash.replace(/^#\//, '');
  activeScreen = Object.hasOwn(pages, route) ? route : 'overview';
  document.querySelectorAll('[data-screen]').forEach(el => { el.hidden = el.dataset.screen !== activeScreen; });
  document.querySelectorAll('[data-page]').forEach(link => {
    const active = link.dataset.page === activeScreen;
    link.classList.toggle('active', active);
    if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  });
  $('page-title').textContent = pages[activeScreen][0];
  $('page-description').textContent = pages[activeScreen][1];
  $('page-breadcrumb').textContent = pages[activeScreen][0];
  document.title = `${pages[activeScreen][0]} · Finacle`;
  clearTimeout(logTimer); logRequest?.abort(); generation++;
  if (activeScreen === 'logs') readLogs();
  if (focus) $('page-title').focus({preventScroll: true});
  window.scrollTo({top: 0, behavior: 'instant'});
}
document.querySelectorAll('[data-page], .brand').forEach(link => link.addEventListener('click', event => {
  if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault();
  history.pushState(null, '', link.getAttribute('href'));
  showPage(true);
}));
window.addEventListener('popstate', () => showPage());
window.addEventListener('hashchange', () => showPage());
function notice(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
async function api(url, options = {}) {
  const response = await fetch(url, {...options, headers: {...options.headers, 'X-Finacle-Namespace': namespace}, signal: options.signal || AbortSignal.timeout(45000)});
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.message || `HTTP ${response.status}`);
    error.data = data;
    error.status = response.status;
    throw error;
  }
  return data;
}
function option(value) { return new Option(value, value); }
function cell(row, value) { const td = document.createElement('td'); td.textContent = value; row.append(td); return td; }
function emptyTable(id, columns, message) { const row = document.createElement('tr'); cell(row, message).colSpan = columns; $(id).replaceChildren(row); }
function fillContainers() {
  const previous = $('container').value;
  const pod = podList.find(p => p.name === $('pod').value);
  $('container').replaceChildren(...(pod?.containers || []).map(option));
  if (pod?.containers.includes(previous)) $('container').value = previous;
}
function fillPods() {
  const previous = $('pod').value;
  const available = podList.filter(p => p.source === source).sort((a,b) => Number(b.healthy)-Number(a.healthy) || a.name.localeCompare(b.name));
  $('pod').replaceChildren(...available.map(p => option(p.name)));
  if (available.some(p => p.name === previous)) $('pod').value = previous;
  if (!available.length) $('pod').add(new Option('Тохирох pod олдсонгүй', ''));
  fillContainers();
}
function renderLogs() {
  const query = $('search').value.toLocaleLowerCase();
  const lines = content ? content.split('\n') : [];
  const filtered = query ? lines.filter(line => line.toLocaleLowerCase().includes(query)) : lines;
  const output = $('log-output'), previousScroll = output.scrollTop;
  output.textContent = filtered.join('\n') || (query ? 'Хайлтад тохирох мөр олдсонгүй.' : 'Log файл хоосон байна.');
  $('line-count').textContent = `${filtered.length} мөр`;
  output.scrollTop = $('autoscroll').checked ? output.scrollHeight : previousScroll;
}
function resetLogs() {
  generation++; clearTimeout(logTimer); logRequest?.abort(); content = '';
  $('log-output').textContent = 'Log уншиж байна…'; $('line-count').textContent = '0 мөр';
  $('log-updated').textContent = 'Уншаагүй'; $('truncated').hidden = true;
  notice('log-error', ''); readLogs();
}
async function readLogs() {
  clearTimeout(logTimer); logRequest?.abort();
  if (activeScreen !== 'logs') return;
  const current = ++generation;
  $('log-path').textContent = `${sources[source].path}/${$('file').value}`;
  if (!$('pod').value) {
    $('log-state').textContent = 'Pod байхгүй'; $('log-output').textContent = 'Энэ эх үүсвэрт тохирох pod олдсонгүй. Төлөв шинэчилнэ үү.';
    return;
  }
  logRequest = new AbortController();
  const controller = logRequest;
  const timeout = setTimeout(() => controller.abort(), 45000);
  $('log-state').textContent = 'Уншиж байна…';
  try {
    const query = new URLSearchParams({source, pod: $('pod').value, container: $('container').value, file: $('file').value, lines: $('lines').value});
    const data = await api(`/api/logs?${query}`, {signal: logRequest.signal});
    if (current !== generation) return;
    content = data.content; renderLogs(); notice('log-error', '');
    $('log-updated').textContent = new Date(data.updated_at).toLocaleTimeString('mn-MN');
    $('truncated').hidden = !data.truncated;
    $('log-state').textContent = $('live').checked ? '● Авто шинэчлэлт' : 'Түр зогссон';
  } catch (error) {
    if (current !== generation) return;
    notice('log-error', error.name === 'AbortError' ? 'Хүсэлтийн хугацаа хэтэрлээ.' : error.message);
    $('log-state').textContent = 'Шинэчлэгдээгүй';
    if (!content) $('log-output').textContent = 'Log уншиж чадсангүй. Файл, container болон хандах эрхийг шалгана уу.';
  } finally {
    clearTimeout(timeout);
    if (current === generation && activeScreen === 'logs' && $('live').checked) logTimer = setTimeout(readLogs, 5000);
  }
}
async function refreshOverview() {
  if (overviewBusy) return;
  overviewBusy = true; $('refresh').disabled = true;
  document.querySelectorAll('#deploy-table button').forEach(button => { button.disabled = true; });
  try {
    const data = await api('/api/overview');
    notice('overview-error', '');
    const previous = `${$('pod').value}/${$('container').value}`;
    podList = data.pods; fillPods();
    $('total').textContent = podList.length;
    $('healthy').textContent = podList.filter(p => p.healthy).length;
    $('attention').textContent = podList.filter(p => !p.healthy).length;
    $('overview-updated').textContent = `Сүүлд шинэчилсэн: ${new Date(data.updated_at).toLocaleTimeString('mn-MN')}`;
    $('pod-table').replaceChildren();
    for (const pod of podList) {
      const row = document.createElement('tr'); cell(row, pod.name);
      const badge = document.createElement('span'); badge.className = `badge${pod.healthy ? '' : ' warn'}`; badge.textContent = pod.phase; cell(row, '').append(badge);
      cell(row, `${pod.ready} / ${pod.total}`); cell(row, pod.restarts);
      const link = document.createElement('a'); link.className = 'stdout'; link.href = `/logs/${encodeURIComponent(pod.name)}?namespace=${encodeURIComponent(namespace)}`; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Container log ↗'; cell(row, '').append(link);
      $('pod-table').append(row);
    }
    if (!podList.length) emptyTable('pod-table', 5, 'Namespace дотор pod байхгүй байна.');
    notice('deploy-warning', data.warning);
    $('deploy-table').replaceChildren();
    for (const deployment of data.deployments) {
      const row = document.createElement('tr'); cell(row, deployment.name); cell(row, `${deployment.ready} / ${deployment.desired}`);
      const button = document.createElement('button'); button.className = 'restart'; button.textContent = '↻ Restart';
      button.disabled = restartBusy || !deployment.restart?.allowed || Date.now() - (localRestarts.get(deployment.name) || 0) < 300000;
      const localWait = Math.max(0, Math.ceil((300000 - (Date.now() - (localRestarts.get(deployment.name) || 0))) / 1000));
      button.title = localWait ? `Restart хүсэлтийн дараах хүлээлт: ${localWait} сек.` : deployment.restart?.reason || 'Төлөв баталгаажаагүй.';
      button.addEventListener('click', () => restart(deployment.name, button));
      const actions = cell(row, ''); actions.append(button);
      const reason = document.createElement('p'); reason.textContent = button.title; actions.append(reason);
      $('deploy-table').append(row);
    }
    if (!data.deployments.length) emptyTable('deploy-table', 3, data.warning ? 'Deployment мэдээлэл авч чадсангүй.' : 'Deployment байхгүй байна.');
    if (previous !== `${$('pod').value}/${$('container').value}` || !generation) resetLogs();
  } catch (error) {
    notice('overview-error', `Cluster мэдээлэл шинэчлэгдсэнгүй: ${error.message}`);
    $('overview-updated').textContent = 'Мэдээлэл шинэчлэгдээгүй. Өмнөх төлөв хуучирсан байж болно.';
  } finally { overviewBusy = false; $('refresh').disabled = false; }
}
async function restart(name, button) {
  if (restartBusy || button.disabled) return;
  if (!confirm(`${namespace} namespace-ийн ${name} deployment-ийг restart хийх үү?\nҮйлчилгээ түр тасалдаж болзошгүй.`)) return;
  button.disabled = true; restartBusy = true; $('project-select').disabled = true;
  localRestarts.set(name, Date.now());
  try {
    const result = await api('/restart', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'FinacleSupport'}, body: JSON.stringify({name})});
    notice('action-message', result.message); await refreshOverview();
  } catch (error) {
    if ([400, 403, 404, 409].includes(error.status)) localRestarts.delete(name);
    notice('action-message', error.message);
  }
  finally { restartBusy = false; $('project-select').disabled = controlBusy; }
}
document.querySelectorAll('[data-source]').forEach(button => button.addEventListener('click', () => {
  source = button.dataset.source;
  document.querySelectorAll('[data-source]').forEach(tab => { const active = tab === button; tab.classList.toggle('selected', active); tab.setAttribute('aria-pressed', String(active)); });
  $('file').replaceChildren(...sources[source].files.map(option)); fillPods(); resetLogs();
}));
$('pod').addEventListener('change', () => { fillContainers(); resetLogs(); });
['container','file','lines'].forEach(id => $(id).addEventListener('change', resetLogs));
$('search').addEventListener('input', renderLogs);
$('refresh').addEventListener('click', refreshOverview);
$('read').addEventListener('click', readLogs);
$('live').addEventListener('change', () => { clearTimeout(logTimer); if ($('live').checked) readLogs(); else $('log-state').textContent = 'Түр зогссон'; });
$('download').addEventListener('click', () => {
  const url = URL.createObjectURL(new Blob([content], {type: 'text/plain;charset=utf-8'}));
  const link = document.createElement('a'); link.href = url; link.download = `${source}-${$('file').value}`; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
});
$('file').replaceChildren(...sources[source].files.map(option));
refreshOverview(); setInterval(refreshOverview, 5000);

let controlConfigs = [], controlPlan = null, controlBusy = false;
let currentRead = 0;
function clearControlPlan() {
  controlPlan = null;
  $('control-preview-panel').hidden = true;
}
function controlSelection() {
  clearControlPlan();
  const config = controlConfigs.find(c => c.name === $('control-config').value);
  const modes = config?.group === 'FNHTTP' ? ['DEBUG', 'TRACE', 'NORMAL'] : ['DEBUG', 'NORMAL'];
  $('control-mode').replaceChildren(...modes.map(option));
  $('control-mode').required = true;
  $('control-mode').disabled = true;
  $('control-user').value = '';
  $('control-user').disabled = true;
  $('control-preview').disabled = true;
  loadCurrentValues(config);
}
function updateControlUser(config) {
  $('control-user').disabled = config?.group !== 'FNHTTP' || $('control-mode').value === 'NORMAL';
  $('control-user-help').textContent = config?.group !== 'FNHTTP' ? 'FIP / Finbranch-д USER ашиглахгүй' :
    $('control-mode').value === 'NORMAL' ? 'NORMAL үед DEBUG_LOG_USERS = NONE' : 'ALL эсвэл нэг USERNAME';
}
function currentLogMode(group, values) {
  if (group !== 'FNHTTP') {
    if (values.FI_LOGLEVEL === 'DEBUG' && values.LOG_LEVEL === 'DEBUG') return 'DEBUG';
    if (values.FI_LOGLEVEL === 'NONE' && values.LOG_LEVEL === 'WARN') return 'NORMAL';
    return '';
  }
  if (values.DEBUG_LOG_LEVEL === 'DEBUG' && values.FINACLE_LOG4FIN_ENABLED === 'N' && values.GLOBAL_TRACE === 'OFF' && values.TBA_DEBUG_LEVEL === '0') return 'DEBUG';
  if (values.DEBUG_LOG_LEVEL === 'TRACE' && values.FINACLE_LOG4FIN_ENABLED === 'Y' && values.GLOBAL_TRACE === 'ON') return 'TRACE';
  if (values.DEBUG_LOG_LEVEL === 'NONE' && values.FINACLE_LOG4FIN_ENABLED === 'N' && values.GLOBAL_TRACE === 'OFF' && values.DEBUG_LOG_USERS === 'NONE') return 'NORMAL';
  return '';
}
async function loadCurrentValues(config, fillForm = true) {
  const read = ++currentRead;
  $('control-preview').disabled = true;
  if (fillForm) {
    $('control-mode').disabled = true;
    $('control-user').disabled = true;
  }
  $('control-current').replaceChildren();
  notice('control-current-status', config ? 'Одоогийн утгуудыг уншиж байна…' : 'ConfigMap сонгоно уу.');
  if (!config) return;
  try {
    const data = await api(`/api/log-control/current?configmap=${encodeURIComponent(config.name)}`);
    if (read !== currentRead) return;
    if (fillForm) {
      const mode = currentLogMode(config.group, data.values);
      $('control-mode').querySelector('option[value=""]')?.remove();
      if (!mode) {
        const custom = new Option('Одоогийн: CUSTOM / холимог — сонгоно уу', '');
        custom.disabled = true;
        $('control-mode').prepend(custom);
      }
      $('control-mode').value = mode;
      $('control-user').value = config.group === 'FNHTTP' ? (data.values.DEBUG_LOG_USERS ?? 'ALL') : '';
      $('control-mode').disabled = false;
      updateControlUser(config);
    }
    const keys = config.group === 'FNHTTP' ? ['FINACLE_LOG4FIN_ENABLED', 'DEBUG_LOG_USERS', 'DEBUG_LOG_LEVEL', 'GLOBAL_TRACE'] : ['FI_LOGLEVEL', 'LOG_LEVEL'];
    if (config.group === 'FNHTTP' && $('control-mode').value === 'DEBUG') keys.push('TBA_DEBUG_LEVEL');
    for (const key of keys) { const row = document.createElement('tr'); cell(row, key); cell(row, data.values[key] ?? '(байхгүй)'); $('control-current').append(row); }
    notice('control-current-status', `${namespace} · ${config.name} · ${new Date().toLocaleTimeString('mn-MN')}`);
    $('control-preview').disabled = !$('control-mode').value;
  } catch (error) { if (read === currentRead) notice('control-current-status', error.message); }
}
function controlPayload() {
  return {configmap: $('control-config').value, mode: $('control-mode').value, username: $('control-user').value.trim()};
}
function controlLock(busy) {
  controlBusy = busy;
  $('control-fields').disabled = busy;
  ['control-refresh', 'control-apply', 'control-cancel', 'control-reset-all'].forEach(id => { $(id).disabled = busy; });
  $('project-select').disabled = busy || restartBusy;
}
async function loadControlConfigs() {
  if (controlBusy) return;
  controlLock(true); clearControlPlan(); notice('control-message', 'ConfigMap уншиж байна…');
  try {
    const previous = $('control-config').value;
    const data = await api('/api/log-control');
    controlConfigs = data.configmaps;
    $('control-config').replaceChildren(...controlConfigs.map(c => new Option(`${c.group} · ${c.name}`, c.name)));
    if (controlConfigs.some(c => c.name === previous)) $('control-config').value = previous;
    notice('control-message', controlConfigs.length ? '' : 'Тохирох ConfigMap олдсонгүй.');
  } catch (error) {
    controlConfigs = []; $('control-config').replaceChildren(new Option('Уншиж чадсангүй', ''));
    notice('control-message', error.message);
  } finally { controlLock(false); controlSelection(); }
}
async function controlPost(action, body) {
  // Apply may restart several deployments; do not retry mutations automatically.
  return api(`/api/log-control/${action}`, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Requested-With': 'FinacleSupport'},
    signal: AbortSignal.timeout(1800000), body: JSON.stringify(body)});
}
$('control-refresh').addEventListener('click', loadControlConfigs);
$('control-config').addEventListener('change', controlSelection);
$('control-mode').addEventListener('change', () => {
  clearControlPlan();
  const config = controlConfigs.find(c => c.name === $('control-config').value);
  if ($('control-user').disabled && config?.group === 'FNHTTP' && $('control-mode').value !== 'NORMAL' && $('control-user').value === 'NONE') $('control-user').value = 'ALL';
  updateControlUser(config);
  $('control-preview').disabled = true;
  loadCurrentValues(config, false);
});
$('control-user').addEventListener('input', clearControlPlan);
$('control-cancel').addEventListener('click', clearControlPlan);
async function previewControl(resetAll = false) {
  if (controlBusy) return;
  clearControlPlan(); controlLock(true); notice('control-message', 'ConfigMap болон Deployment өөрчлөлтүүдийг dry-run шалгаж байна…');
  try {
    const payload = resetAll ? {} : controlPayload();
    const plan = await controlPost(resetAll ? 'reset-preview' : 'preview', payload);
    controlPlan = {payload, plan, resetAll};
    $('control-plan-title').textContent = `${namespace} · ${resetAll ? 'Бүх logging ConfigMap' : plan.configmap} → ${plan.mode}`;
    $('control-deployments').textContent = `Restart: ${plan.deployments.map(d => d.name).join(', ')}`;
    $('control-changes').replaceChildren();
    for (const config of plan.configurations || [plan]) {
      for (const change of config.changes) {
        const row = document.createElement('tr'); cell(row, config.configmap); cell(row, change.key); cell(row, change.before ?? '(байхгүй)'); cell(row, change.after);
        $('control-changes').append(row);
      }
    }
    $('control-preview-panel').hidden = false; notice('control-message', 'Өөрчлөлт хийгдээгүй. Доорх утгуудыг шалгаад баталгаажуулна уу.');
  } catch (error) { notice('control-message', error.message); }
  finally { controlLock(false); }
}
$('control-form').addEventListener('submit', event => { event.preventDefault(); previewControl(); });
$('control-reset-all').addEventListener('click', () => previewControl(true));
$('control-apply').addEventListener('click', async () => {
  if (controlBusy || !controlPlan) return;
  const {payload, plan, resetAll} = controlPlan;
  const configNames = (plan.configurations || [plan]).map(c => c.configmap).join(', ');
  if (!confirm(`${plan.namespace}\n${configNames} → ${plan.mode}\nRestart: ${plan.deployments.map(d => d.name).join(', ')}\n\nConfigMap-ийг өөрчилж restart хийх үү?`)) return;
  controlLock(true); notice('control-message', 'Өөрчлөлт хийж байна. Хүсэлтийг дахин бүү илгээгээрэй…');
  try {
    const result = await controlPost(resetAll ? 'reset-apply' : 'apply', {...payload, fingerprint: plan.fingerprint, confirmed: true});
    for (const name of result.restarted || []) localRestarts.set(name, Date.now());
    notice('control-message', result.message); refreshOverview(); startRolloutWatch(result.tracking);
  } catch (error) {
    notice('control-message', `${error.message} Шинэ хүсэлт илгээхээс өмнө ConfigMap болон Deployment төлөвийг дахин шалгана уу.`);
    if (error.data?.tracking) startRolloutWatch(error.data.tracking);
  } finally { clearControlPlan(); controlLock(false); loadCurrentValues(controlConfigs.find(c => c.name === $('control-config').value)); }
});
controlSelection();

let rolloutTracking = null, rolloutTimer, rolloutStarted = 0, rolloutRequest = 0;
function startRolloutWatch(tracking) {
  if (!tracking || !tracking.names?.length) return;
  for (const name of tracking.names) localRestarts.set(name, Date.now());
  clearTimeout(rolloutTimer);
  rolloutTracking = tracking; rolloutStarted = Date.now();
  $('control-rollout').hidden = false;
  notice('rollout-summary', 'Restart хүсэлт илгээгдсэн. Шинэ replica-уудын бэлэн байдлыг шалгаж байна…');
  emptyTable('rollout-table', 4, 'Шалгаж байна…');
  checkRollout();
}
async function checkRollout() {
  if (!rolloutTracking) return;
  clearTimeout(rolloutTimer);
  const requestId = ++rolloutRequest;
  $('rollout-refresh').disabled = true;
  try {
    const query = new URLSearchParams({...rolloutTracking, names: rolloutTracking.names.join(',')});
    const result = await api(`/api/log-control/rollout?${query}`);
    if (requestId !== rolloutRequest) return;
    $('rollout-table').replaceChildren();
    for (const d of result.deployments) {
      const row = document.createElement('tr'); cell(row, d.name); cell(row, d.updated ?? '—'); cell(row, `${d.ready ?? '—'} / ${d.desired ?? '—'}`);
      const badge = document.createElement('span'); badge.className = `badge${d.state === 'ready' ? '' : ' warn'}`; badge.textContent = d.message;
      cell(row, '').append(badge); $('rollout-table').append(row);
    }
    const failed = result.deployments.some(d => ['failed', 'blocked', 'changed'].includes(d.state));
    if (result.complete) notice('rollout-summary', 'Бүх шинэ replica Ready / Available боллоо. Rollout дууссан.');
    else if (failed) notice('rollout-summary', 'Rollout бүрэн дуусаагүй. Доорх төлөвийг шалгана уу. ConfigMap-ийг автоматаар буцаагаагүй.');
    else if (Date.now() - rolloutStarted >= 600000) notice('rollout-summary', '10 минутын хяналтын хугацаа дууслаа. Бүх pod бэлэн болоогүй. Дахин шалгах боломжтой.');
    else {
      notice('rollout-summary', 'Pod-ууд асаж байна… 5 секунд тутам бэлэн байдлыг шалгана.');
      rolloutTimer = setTimeout(checkRollout, 5000);
    }
  } catch (error) {
    if (requestId === rolloutRequest) notice('rollout-summary', `Төлөв баталгаажаагүй: ${error.message}. Дахин шалгана уу.`);
  } finally { if (requestId === rolloutRequest) $('rollout-refresh').disabled = false; }
}
$('rollout-refresh').addEventListener('click', () => { rolloutStarted = Date.now(); checkRollout(); });
showPage();

