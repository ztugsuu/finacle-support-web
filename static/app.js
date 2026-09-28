'use strict';
const $ = id => document.getElementById(id);
const sources = JSON.parse($('sources').textContent);
let source = 'fip', podList = [], content = '', logTimer, logRequest, generation = 0, overviewBusy = false;
function notice(id, message) { $(id).textContent = message || ''; $(id).hidden = !message; }
async function api(url, options = {}) {
  const response = await fetch(url, {...options, signal: options.signal || AbortSignal.timeout(45000)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || `HTTP ${response.status}`);
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
    if (current === generation && $('live').checked) logTimer = setTimeout(readLogs, 5000);
  }
}
async function refreshOverview() {
  if (overviewBusy) return;
  overviewBusy = true; $('refresh').disabled = true;
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
      const link = document.createElement('a'); link.className = 'stdout'; link.href = `/logs/${encodeURIComponent(pod.name)}`; link.target = '_blank'; link.rel = 'noopener'; link.textContent = 'Container log ↗'; cell(row, '').append(link);
      $('pod-table').append(row);
    }
    if (!podList.length) emptyTable('pod-table', 5, 'Namespace дотор pod байхгүй байна.');
    notice('deploy-warning', data.warning);
    $('deploy-table').replaceChildren();
    for (const deployment of data.deployments) {
      const row = document.createElement('tr'); cell(row, deployment.name); cell(row, `${deployment.ready} / ${deployment.desired}`);
      const button = document.createElement('button'); button.className = 'restart'; button.textContent = '↻ Restart'; button.addEventListener('click', () => restart(deployment.name, button)); cell(row, '').append(button); $('deploy-table').append(row);
    }
    if (!data.deployments.length) emptyTable('deploy-table', 3, data.warning ? 'Deployment мэдээлэл авч чадсангүй.' : 'Deployment байхгүй байна.');
    if (previous !== `${$('pod').value}/${$('container').value}` || !generation) resetLogs();
  } catch (error) {
    notice('overview-error', `Cluster мэдээлэл шинэчлэгдсэнгүй: ${error.message}`);
    $('overview-updated').textContent = 'Мэдээлэл шинэчлэгдээгүй. Өмнөх төлөв хуучирсан байж болно.';
  } finally { overviewBusy = false; $('refresh').disabled = false; }
}
async function restart(name, button) {
  if (!confirm(`pre-uat-finacle namespace-ийн ${name} deployment-ийг restart хийх үү?\nҮйлчилгээ түр тасалдаж болзошгүй.`)) return;
  button.disabled = true;
  try {
    const result = await api('/restart', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'FinacleSupport'}, body: JSON.stringify({name})});
    notice('action-message', result.message); await refreshOverview();
  } catch (error) { notice('action-message', error.message); }
  finally { button.disabled = false; }
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
refreshOverview(); setInterval(refreshOverview, 30000);

