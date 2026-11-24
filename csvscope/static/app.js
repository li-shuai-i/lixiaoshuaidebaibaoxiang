/* csvscope — app.js
   Vanilla JS, no build step. Talks to the local Python server over JSON. */

'use strict';

const $ = (id) => document.getElementById(id);

const NUMERIC = new Set(['int', 'float']);

const OP_LABELS = {
  contains: 'contains',
  not_contains: 'does not contain',
  eq: '=',
  ne: '≠',
  gt: '>',
  gte: '≥',
  lt: '<',
  lte: '≤',
  empty: 'is empty',
  not_empty: 'is not empty',
};

const OPS_BY_TYPE = {
  string: ['contains', 'not_contains', 'eq', 'ne', 'empty', 'not_empty'],
  int: ['gt', 'gte', 'lt', 'lte', 'eq', 'ne', 'empty', 'not_empty'],
  float: ['gt', 'gte', 'lt', 'lte', 'eq', 'ne', 'empty', 'not_empty'],
  date: ['gt', 'gte', 'lt', 'lte', 'empty', 'not_empty'],
  bool: ['eq', 'ne', 'empty', 'not_empty'],
};

const NO_VALUE_OPS = new Set(['empty', 'not_empty']);

const state = {
  ds: null,
  page: 1,
  pageSize: 50,
  filters: [],
  sort: null,
  selected: 0,
  groupBy: '',
  metric: '',
  agg: 'count',
  total: 0,
  pages: 1,
};

const nf = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });
const cf = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });

/* ── utilities ───────────────────────────────────────────────────────── */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    /* non-JSON error page */
  }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer = null;
function toast(message) {
  const node = $('toast');
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.hidden = true; }, 5200);
}

function busy(on, text) {
  const node = $('busy');
  if (text) $('busyText').textContent = text;
  node.hidden = !on;
}

const fmt = (n) => (typeof n === 'number' && Number.isFinite(n) ? nf.format(n) : String(n ?? ''));
const compact = (n) => (Number.isFinite(n) ? cf.format(n) : '');

/* ── file intake ─────────────────────────────────────────────────────── */

async function readFileText(file) {
  const buffer = await file.arrayBuffer();
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(buffer);
  } catch {
    // Common for CSV exported from Excel on Chinese Windows.
    try {
      return new TextDecoder('gbk', { fatal: true }).decode(buffer);
    } catch {
      return new TextDecoder('utf-8').decode(buffer);
    }
  }
}

async function loadFile(file) {
  if (!file) return;
  if (file.size > 64 * 1024 * 1024) {
    toast('File is larger than the 64 MB limit.');
    return;
  }
  busy(true, 'Reading file…');
  try {
    const text = await readFileText(file);
    await loadText(text, file.name);
  } catch (err) {
    toast(err.message);
  } finally {
    busy(false);
  }
}

async function loadText(text, name) {
  busy(true, 'Profiling columns…');
  try {
    const ds = await api('/api/load', { name, content: text });
    state.ds = ds;
    state.page = 1;
    state.filters = [];
    state.sort = null;
    state.selected = defaultColumn(ds);
    state.groupBy = defaultGroupColumn(ds);
    state.metric = defaultMetric(ds);
    state.agg = 'count';

    $('empty').hidden = true;
    $('workspace').hidden = false;
    $('btnLoad').hidden = false;
    document.title = `${name} — csvscope`;

    renderMeta();
    renderColumns();
    renderProfile();
    buildFilterControls();
    buildGroupControls();
    await refreshAll();
  } catch (err) {
    toast(err.message);
  } finally {
    busy(false);
  }
}

function defaultColumn(ds) {
  const numeric = ds.columns.find((c) => NUMERIC.has(c.type));
  return numeric ? numeric.index : 0;
}

function defaultMetric(ds) {
  const numeric = ds.columns.find((c) => NUMERIC.has(c.type));
  return numeric ? numeric.name : '';
}

function defaultGroupColumn(ds) {
  const profileOf = {};
  ds.profiles.forEach((p) => { profileOf[p.index] = p; });
  const candidates = ds.columns.filter((c) => {
    const p = profileOf[c.index];
    return c.type === 'string' && p && p.distinct >= 2 && p.distinct <= 25;
  });
  if (!candidates.length) return '';
  // Prefer columns whose name hints at a dimension.
  const hint = /(category|type|status|channel|region|country|group|segment|method|level)/i;
  const preferred = candidates.find((c) => hint.test(c.name));
  return (preferred || candidates[0]).name;
}

/* ── rendering: header, columns, profile ─────────────────────────────── */

function renderMeta() {
  const ds = state.ds;
  if (!ds) return;
  const meta = $('meta');
  meta.replaceChildren();
  meta.append(el('b', null, ds.name));
  meta.append(el('span', null, `· ${fmt(ds.row_count)} rows × ${ds.column_count} columns`));
  $('colCount').textContent = String(ds.column_count);
}

function renderColumns() {
  const list = $('colList');
  list.replaceChildren();
  for (const col of state.ds.columns) {
    const item = el('li', 'col-item');
    if (col.index === state.selected) item.classList.add('active');
    item.append(el('span', 'col-name', col.name));
    item.append(el('span', `badge ${col.type}`, col.type));
    item.title = `${col.name} (${col.type})`;
    item.addEventListener('click', () => {
      state.selected = col.index;
      renderColumns();
      renderProfile();
    });
    list.append(item);
  }
}

function profileFor(index) {
  return state.ds.profiles.find((p) => p.index === index);
}

function renderProfile() {
  const panel = $('profile');
  const profile = profileFor(state.selected);
  panel.replaceChildren();
  if (!profile) return;

  const title = el('div', 'profile-title');
  const name = el('h3', null, profile.name);
  name.title = profile.name;
  title.append(name, el('span', `badge ${profile.type}`, profile.type));
  panel.append(title);

  const missingPct = profile.total ? (profile.missing / profile.total) * 100 : 0;
  const grid = el('dl', 'stat-grid');
  const addStat = (label, value) => {
    const wrap = el('div', 'stat');
    wrap.append(el('dt', null, label), el('dd', null, value));
    grid.append(wrap);
  };
  addStat('Distinct', fmt(profile.distinct));
  addStat('Missing', profile.missing ? `${fmt(profile.missing)} (${missingPct.toFixed(1)}%)` : '0');

  const stats = profile.stats;
  if (stats && NUMERIC.has(profile.type)) {
    addStat('Min', fmt(stats.min));
    addStat('Max', fmt(stats.max));
    addStat('Mean', fmt(Number(stats.mean.toFixed(2))));
    addStat('Median', fmt(Number(stats.median.toFixed(2))));
    addStat('Std dev', fmt(Number(stats.stdev.toFixed(2))));
    addStat('Sum', compact(stats.sum));
  } else if (stats && profile.type === 'date') {
    addStat('Earliest', String(stats.min).slice(0, 10));
    addStat('Latest', String(stats.max).slice(0, 10));
  }
  panel.append(grid);

  if (profile.histogram && profile.histogram.length) {
    panel.append(el('div', 'section-label', 'Distribution'));
    const max = Math.max(...profile.histogram.map((b) => b.count), 1);
    const hist = el('div', 'hist');
    for (const bin of profile.histogram) {
      const bar = el('div', 'hist-bar');
      bar.style.height = `${Math.max(3, (bin.count / max) * 100)}%`;
      bar.title = `${fmt(Number(bin.start.toFixed(2)))} – ${fmt(Number(bin.end.toFixed(2)))}: ${fmt(bin.count)} rows`;
      hist.append(bar);
    }
    panel.append(hist);
    const axis = el('div', 'hist-axis');
    axis.append(
      el('span', null, compact(profile.histogram[0].start)),
      el('span', null, compact(profile.histogram[profile.histogram.length - 1].end)),
    );
    panel.append(axis);
  }

  if (profile.top_values && profile.top_values.length) {
    panel.append(el('div', 'section-label', 'Top values'));
    const list = el('div', 'toplist');
    const topCount = profile.top_values[0].count || 1;
    for (const entry of profile.top_values) {
      const row = el('div', 'top-row');
      const label = el('span', 'top-label');
      label.style.setProperty('--w', `${(entry.count / topCount) * 100}%`);
      label.append(el('span', null, entry.value === '' ? '(empty)' : entry.value));
      label.title = entry.value;
      row.append(label, el('span', 'top-count', fmt(entry.count)));
      list.append(row);
    }
    panel.append(list);
  }
}

/* ── controls ────────────────────────────────────────────────────────── */

function columnByName(name) {
  return state.ds.columns.find((c) => c.name === name);
}

function buildFilterControls() {
  const colSel = $('fColumn');
  colSel.replaceChildren();
  for (const col of state.ds.columns) {
    const opt = el('option', null, col.name);
    opt.value = col.name;
    colSel.append(opt);
  }
  colSel.value = state.ds.columns[0].name;
  syncOps();
}

function syncOps() {
  const col = columnByName($('fColumn').value) || state.ds.columns[0];
  const opSel = $('fOp');
  const previous = opSel.value;
  opSel.replaceChildren();
  for (const op of OPS_BY_TYPE[col.type] || OPS_BY_TYPE.string) {
    const opt = el('option', null, OP_LABELS[op]);
    opt.value = op;
    opSel.append(opt);
  }
  if ([...opSel.options].some((o) => o.value === previous)) opSel.value = previous;
  const needsValue = !NO_VALUE_OPS.has(opSel.value);
  $('fValue').disabled = !needsValue;
  $('fValue').value = needsValue ? $('fValue').value : '';
}

function buildGroupControls() {
  const groupSel = $('gColumn');
  groupSel.replaceChildren();
  const none = el('option', null, '— none —');
  none.value = '';
  groupSel.append(none);
  for (const col of state.ds.columns) {
    if (col.type === 'string' || col.type === 'bool' || col.type === 'date') {
      const opt = el('option', null, col.name);
      opt.value = col.name;
      groupSel.append(opt);
    }
  }
  groupSel.value = state.groupBy;

  const metricSel = $('gMetric');
  metricSel.replaceChildren();
  const noMetric = el('option', null, '— rows —');
  noMetric.value = '';
  metricSel.append(noMetric);
  for (const col of state.ds.columns.filter((c) => NUMERIC.has(c.type))) {
    const opt = el('option', null, col.name);
    opt.value = col.name;
    metricSel.append(opt);
  }
  metricSel.value = state.metric;

  $('gAgg').value = state.agg;
  syncMetricState();
}

function syncMetricState() {
  const isCount = $('gAgg').value === 'count';
  $('gMetric').disabled = isCount;
  if (!isCount && !$('gMetric').value) {
    const first = state.ds.columns.find((c) => NUMERIC.has(c.type));
    if (first) $('gMetric').value = first.name;
  }
}

function labelsForOp(op) {
  return OP_LABELS[op] || op;
}

function renderChips() {
  const wrap = $('chips');
  wrap.replaceChildren();
  if (!state.filters.length) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  state.filters.forEach((filter, index) => {
    const chip = el('span', 'chip');
    chip.append(el('b', null, filter.column));
    chip.append(el('span', null, labelsForOp(filter.op)));
    if (!NO_VALUE_OPS.has(filter.op)) chip.append(el('code', null, String(filter.value)));
    const remove = el('button', null, '×');
    remove.title = 'Remove filter';
    remove.addEventListener('click', () => {
      state.filters.splice(index, 1);
      state.page = 1;
      refreshAll();
    });
    chip.append(remove);
    wrap.append(chip);
  });
  const clear = el('button', 'clear-all', 'clear all');
  clear.addEventListener('click', () => {
    state.filters = [];
    state.page = 1;
    refreshAll();
  });
  wrap.append(clear);
}

/* ── data fetching + table ───────────────────────────────────────────── */

async function refreshAll() {
  renderChips();
  await Promise.all([refreshRows(), refreshChart()]);
}

async function refreshRows() {
  const result = await api('/api/query', {
    id: state.ds.id,
    filters: state.filters,
    sort: state.sort,
    page: state.page,
    page_size: state.pageSize,
  });
  state.total = result.total;
  state.pages = result.pages;
  state.page = result.page;
  renderTable(result.rows);
  renderPager();
}

function renderTable(rows) {
  const table = $('table');
  const thead = table.tHead;
  const tbody = table.tBodies[0];
  thead.replaceChildren();
  tbody.replaceChildren();

  const headRow = el('tr');
  for (const col of state.ds.columns) {
    const th = el('th');
    const inner = el('span', 'th-inner');
    inner.append(el('span', null, col.name));
    if (state.sort && state.sort.column === col.name) {
      th.classList.add('sorted');
      inner.append(el('span', 'sort-mark', state.sort.desc ? '▼' : '▲'));
    }
    th.append(inner);
    th.title = `${col.name} (${col.type}) — click to sort`;
    th.addEventListener('click', () => toggleSort(col.name));
    headRow.append(th);
  }
  thead.append(headRow);

  if (!rows.length) {
    const tr = el('tr');
    const td = el('td');
    td.colSpan = state.ds.columns.length;
    const box = el('div', 'empty-rows');
    box.append(el('b', null, 'No rows match these filters'));
    box.append(el('span', null, 'Try loosening a condition or clearing all filters.'));
    td.append(box);
    tr.append(td);
    tbody.append(tr);
    return;
  }

  const frag = document.createDocumentFragment();
  for (const row of rows) {
    const tr = el('tr');
    for (let i = 0; i < state.ds.columns.length; i += 1) {
      const col = state.ds.columns[i];
      const raw = row[i];
      const td = el('td');
      if (NUMERIC.has(col.type)) td.classList.add('num');
      if (raw === undefined || raw === null || String(raw).trim() === '') {
        td.classList.add('null');
        td.textContent = '—';
      } else {
        td.textContent = raw;
        td.title = raw;
      }
      tr.append(td);
    }
    frag.append(tr);
  }
  tbody.append(frag);
}

function toggleSort(column) {
  if (state.sort && state.sort.column === column) {
    state.sort = state.sort.desc ? null : { column, desc: true };
  } else {
    state.sort = { column, desc: false };
  }
  state.page = 1;
  refreshRows();
}

function renderPager() {
  const start = state.total ? (state.page - 1) * state.pageSize + 1 : 0;
  const end = Math.min(state.page * state.pageSize, state.total);
  $('pagerInfo').textContent =
    state.total === state.ds.row_count
      ? `${fmt(start)}–${fmt(end)} of ${fmt(state.total)} rows`
      : `${fmt(start)}–${fmt(end)} of ${fmt(state.total)} matching rows (${fmt(state.ds.row_count)} total)`;
  $('pageLabel').textContent = `${state.page} / ${state.pages}`;
  $('prev').disabled = state.page <= 1;
  $('next').disabled = state.page >= state.pages;
  $('rowHint').textContent = state.filters.length
    ? `${fmt(state.total)} rows after ${state.filters.length} filter${state.filters.length > 1 ? 's' : ''}`
    : '';
}

/* ── chart ───────────────────────────────────────────────────────────── */

async function refreshChart() {
  const card = $('chartCard');
  if (!state.ds || !state.groupBy) {
    card.hidden = true;
    return;
  }
  // Guard: grouping by an exhausted aggregation needs a metric.
  const agg = state.agg;
  const metric = agg === 'count' ? null : $('gMetric').value;
  if (agg !== 'count' && !metric) {
    card.hidden = true;
    return;
  }

  let result;
  try {
    result = await api('/api/aggregate', {
      id: state.ds.id,
      group_by: state.groupBy,
      metric,
      agg,
      limit: 20,
      filters: state.filters,
    });
  } catch (err) {
    toast(err.message);
    card.hidden = true;
    return;
  }

  card.hidden = false;
  const aggLabel = agg === 'count' ? 'row count' : `${agg} of ${metric}`;
  $('chartTitle').textContent = `${aggLabel} by ${state.groupBy}`;
  $('chartHint').textContent = result.group_count > result.series.length
    ? `top ${result.series.length} of ${result.group_count} groups · click to filter`
    : 'click a bar to filter';

  const chart = $('chart');
  chart.replaceChildren();
  const max = Math.max(...result.series.map((s) => s.value), 0) || 1;

  for (const point of result.series) {
    const row = el('div', 'bar-row');
    row.title = `${point.key}: ${fmt(Number(point.value.toFixed(2)))}`;
    const label = el('span', 'bar-label', point.key);
    const track = el('div', 'bar-track');
    const fill = el('div', 'bar-fill');
    fill.style.width = `${Math.max(1.5, (point.value / max) * 100)}%`;
    track.append(fill);
    const value = el('span', 'bar-value',
      state.agg === 'count' ? fmt(point.value) : Number(point.value.toFixed(2)).toLocaleString());
    row.append(label, track, value);
    row.addEventListener('click', () => addFilter(state.groupBy, 'eq', point.key));
    chart.append(row);
  }
}

/* ── filters ─────────────────────────────────────────────────────────── */

function addFilter(column, op, value) {
  const exists = state.filters.some(
    (f) => f.column === column && f.op === op && String(f.value) === String(value),
  );
  if (exists) {
    toast('That filter is already applied.');
    return;
  }
  state.filters.push({ column, op, value });
  state.page = 1;
  refreshAll();
}

/* ── wiring ──────────────────────────────────────────────────────────── */

function wire() {
  $('fileInput').addEventListener('change', (event) => {
    const file = event.target.files && event.target.files[0];
    loadFile(file);
    event.target.value = '';
  });

  $('btnLoad').addEventListener('click', () => $('fileInput').click());

  $('btnSample').addEventListener('click', async () => {
    busy(true, 'Loading sample…');
    try {
      const sample = await api('/api/example');
      await loadText(sample.content, sample.name);
    } catch (err) {
      toast(err.message);
    } finally {
      busy(false);
    }
  });

  // drag & drop, with a counter so child elements do not flicker
  let dragDepth = 0;
  window.addEventListener('dragenter', (event) => {
    if (!event.dataTransfer || ![...event.dataTransfer.types].includes('Files')) return;
    dragDepth += 1;
    $('dropOverlay').hidden = false;
  });
  window.addEventListener('dragover', (event) => {
    if (event.dataTransfer && [...event.dataTransfer.types].includes('Files')) {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'copy';
    }
  });
  window.addEventListener('dragleave', () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) $('dropOverlay').hidden = true;
  });
  window.addEventListener('drop', (event) => {
    if (!event.dataTransfer) return;
    event.preventDefault();
    dragDepth = 0;
    $('dropOverlay').hidden = true;
    const file = event.dataTransfer.files && event.dataTransfer.files[0];
    loadFile(file);
  });

  // filter builder
  $('fColumn').addEventListener('change', syncOps);
  $('fOp').addEventListener('change', syncOps);
  $('fAdd').addEventListener('click', () => {
    const column = $('fColumn').value;
    const op = $('fOp').value;
    const value = $('fValue').value;
    if (!column || !op) return;
    if (!NO_VALUE_OPS.has(op) && value.trim() === '') {
      toast('Enter a value first.');
      return;
    }
    addFilter(column, op, NO_VALUE_OPS.has(op) ? '' : value.trim());
    $('fValue').value = '';
    $('fValue').focus();
  });
  $('fValue').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') $('fAdd').click();
  });

  // group by
  $('gColumn').addEventListener('change', () => {
    state.groupBy = $('gColumn').value;
    refreshChart();
  });
  $('gMetric').addEventListener('change', () => {
    state.metric = $('gMetric').value;
    refreshChart();
  });
  $('gAgg').addEventListener('change', () => {
    state.agg = $('gAgg').value;
    syncMetricState();
    refreshChart();
  });

  // pager
  $('prev').addEventListener('click', () => {
    if (state.page > 1) {
      state.page -= 1;
      refreshRows();
    }
  });
  $('next').addEventListener('click', () => {
    if (state.page < state.pages) {
      state.page += 1;
      refreshRows();
    }
  });
  $('pageSize').addEventListener('change', () => {
    state.pageSize = Number($('pageSize').value);
    state.page = 1;
    refreshRows();
  });

  // shortcuts
  window.addEventListener('keydown', (event) => {
    if (event.key === '/' && document.activeElement.tagName !== 'INPUT'
        && document.activeElement.tagName !== 'SELECT' && state.ds) {
      event.preventDefault();
      $('fValue').focus();
    }
  });
}

wire();

// http://127.0.0.1:8787/?sample loads the bundled dataset straight away —
// handy for demos and for checking an install.
if (new URLSearchParams(window.location.search).has('sample')) {
  $('btnSample').click();
}
