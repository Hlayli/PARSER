'use strict';

/* PARSER — клиент. Без сборки и зависимостей. */

const API = '/api';

const STATUS_LABELS = {
  new: 'Новый',
  in_progress: 'В работе',
  called: 'Отзвонено',
  rejected: 'Отказ',
};

const STATUS_ORDER = ['new', 'in_progress', 'called', 'rejected'];
const SOURCE_LABELS = { google: 'Google', yandex: 'Яндекс', '2gis': '2ГИС', demo: 'ДЕМО' };
const SORT_MAP = { newest: 'new', rating: 'rating', name: 'name' };
const CATEGORY_STORAGE_KEY = 'parser.selectedCategories.v1';

const state = {
  adapters: [],
  leads: [],
  total: 0,
  limit: 50,
  filters: { q: '', status: '', sort: 'newest', fav: false, site: '' },
  selectedId: null,
  jobId: null,
  grid: { nx: 0, ny: 0 },
  cells: new Map(),
  stream: { connected: false },
  categories: { groups: [], total: 0, default: [], popular: [] },
  categoriesLoaded: false,
  selectedCategories: new Set(),
  categoryDraft: new Set(),
  categoryFocus: null,
  estimateSeq: 0,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/* ---------- утилиты ---------- */

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function textOr(value, fallback = 'нет данных') {
  const text = String(value ?? '').trim();
  return text ? escapeHtml(text) : `<span class="no-data">${fallback}</span>`;
}

function safeUrl(value) {
  const url = String(value ?? '').trim();
  return /^https?:\/\//i.test(url) ? url : '';
}

function telHref(phone) {
  const digits = String(phone ?? '').replace(/[^\d+]/g, '');
  return digits.length >= 7 ? `tel:${digits}` : '';
}

function plural(count, one, few, many) {
  const n = Math.abs(count) % 100;
  const n1 = n % 10;
  if (n > 10 && n < 20) return many;
  if (n1 > 1 && n1 < 5) return few;
  if (n1 === 1) return one;
  return many;
}

function debounce(fn, delay = 250) {
  let timer = null;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), delay);
  };
}

/* ---------- тосты ---------- */

function toast(message, kind = 'info', timeout = 5000) {
  const root = $('#toasts');
  if (!root) return;
  const node = document.createElement('div');
  node.className = `toast toast--${kind}`;
  node.innerHTML = `<span class="toast-message">${escapeHtml(message)}</span>
    <button class="toast-close" type="button" aria-label="Закрыть">✕</button>`;
  const close = () => node.remove();
  node.querySelector('.toast-close').addEventListener('click', close);
  root.appendChild(node);
  window.setTimeout(close, timeout);
}

/* ---------- запросы ---------- */

async function api(path, options = {}) {
  const response = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      detail = body.detail || body.error || detail;
    } catch (_) {
      /* тело не JSON — оставляем код */
    }
    throw new Error(detail);
  }
  return response.json();
}

/* ---------- источники ---------- */

async function loadHealth() {
  try {
    const data = await api('/health');
    state.adapters = data.adapters || [];
    renderSources();
    $('#demo-badge').hidden = !state.adapters.some((a) => a.name === 'demo' && a.available);
  } catch (error) {
    $('#sources-list').innerHTML =
      `<p class="sources-error">Не удалось получить список источников: ${escapeHtml(error.message)}</p>`;
  }
}

function renderSources() {
  const root = $('#sources-list');
  const real = state.adapters.filter((a) => a.name !== 'demo' && a.available);
  const anyReal = real.length > 0;

  root.innerHTML = state.adapters
    .map((adapter) => {
      const checked = adapter.available && (adapter.name !== 'demo' ? true : !anyReal);
      const disabled = !adapter.available;
      const reason = adapter.reason ? ` title="${escapeHtml(adapter.reason)}"` : '';
      return `<label class="source-row${disabled ? ' is-disabled' : ''}"${reason}>
        <input type="checkbox" value="${escapeHtml(adapter.name)}"
               ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
        <span class="source-label">${escapeHtml(adapter.label || adapter.name)}</span>
        ${disabled ? `<span class="source-reason">${escapeHtml(adapter.reason || 'недоступен')}</span>` : ''}
      </label>`;
    })
    .join('');

  if (!state.adapters.length) {
    root.innerHTML = '<p class="sources-error">Источники не настроены.</p>';
  }
}

function selectedSources() {
  return $$('#sources-list input[type="checkbox"]')
    .filter((input) => input.checked && !input.disabled)
    .map((input) => input.value);
}

/* ---------- категории ---------- */

function categoryById(id) {
  for (const group of state.categories.groups) {
    const found = group.categories.find((item) => item.id === id);
    if (found) return found;
  }
  return null;
}

function readSavedCategories() {
  try {
    const raw = window.localStorage.getItem(CATEGORY_STORAGE_KEY);
    if (raw === null) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.map(String) : null;
  } catch (_) {
    return null;
  }
}

async function loadCategories() {
  if (state.categoriesLoaded || state.categoriesLoading) return;
  state.categoriesLoading = true;
  try {
    const data = await api('/categories');
    state.categories = {
      groups: Array.isArray(data.groups) ? data.groups : [],
      total: Number(data.total || 0),
      default: Array.isArray(data.default) ? data.default : [],
      popular: Array.isArray(data.popular) ? data.popular : [],
    };
    state.categoriesLoaded = true;

    const valid = new Set(
      state.categories.groups.flatMap((group) => group.categories.map((item) => item.id)),
    );
    const saved = readSavedCategories();
    const source = saved === null ? state.categories.default : saved;
    const selected = source.filter((id) => valid.has(id));
    state.selectedCategories = new Set(selected);
    updateCategoryTrigger();
    updateScanEstimate();
  } catch (error) {
    $('#category-summary').textContent = 'Категории не загрузились';
    $('#category-hint').textContent = 'Можно ввести свой запрос в поле ниже.';
    toast(`Не удалось загрузить категории: ${error.message}`, 'error');
  } finally {
    state.categoriesLoading = false;
  }
}

function updateCategoryTrigger() {
  const count = state.selectedCategories.size;
  const labels = state.categories.groups
    .flatMap((group) => group.categories)
    .filter((item) => state.selectedCategories.has(item.id))
    .map((item) => item.label);

  $('#category-count').textContent = String(count);
  $('#category-count').hidden = count === 0;
  $('#category-trigger').classList.toggle('has-selection', count > 0);

  if (!state.categoriesLoaded) {
    $('#category-summary').textContent = 'Загрузка категорий…';
    return;
  }
  if (!count) {
    $('#category-summary').textContent = 'Выберите категории';
  } else if (labels.length <= 2) {
    $('#category-summary').textContent = labels.join(' + ');
  } else {
    $('#category-summary').textContent = `${labels.slice(0, 2).join(', ')} +${labels.length - 2}`;
  }

  $('#category-hint').textContent = count
    ? `${count} из ${state.categories.total} ниш · нажмите, чтобы изменить`
    : 'Можно выбрать несколько ниш или все сразу.';
}

function renderCategoryGroups(filter = '') {
  const root = $('#category-groups');
  const needle = filter.trim().toLowerCase();
  const groups = state.categories.groups
    .map((group) => {
      const items = group.categories.filter((item) => {
        if (!needle) return true;
        return `${group.title} ${item.label} ${item.query}`.toLowerCase().includes(needle);
      });
      if (!items.length) return '';
      const selected = group.categories.filter((item) => state.categoryDraft.has(item.id)).length;

      const rows = items.map((item) => `
        <label class="category-item">
          <input class="category-check" type="checkbox" data-id="${escapeHtml(item.id)}"
                 data-group="${escapeHtml(group.id)}"
                 ${state.categoryDraft.has(item.id) ? 'checked' : ''}>
          <span class="switch" aria-hidden="true"></span>
          <span class="category-copy">
            <span class="category-name">${escapeHtml(item.label)}</span>
            <span class="category-query">${escapeHtml(item.query)}</span>
          </span>
        </label>`).join('');

      return `
        <section class="category-group" data-group="${escapeHtml(group.id)}">
          <div class="category-group-head">
            <h3>${escapeHtml(group.title)}</h3>
            <span class="category-group-count" data-count-for="${escapeHtml(group.id)}">
              ${selected}/${group.categories.length}
            </span>
            <button class="link-btn category-group-toggle" type="button"
                    data-group="${escapeHtml(group.id)}">
              ${selected === group.categories.length ? 'Снять' : 'Выбрать все'}
            </button>
          </div>
          <div class="category-grid">${rows}</div>
        </section>`;
    })
    .join('');

  root.innerHTML = groups || '<p class="category-empty">Ничего не найдено. Попробуйте другой запрос.</p>';
  root.scrollTop = 0;
  updateCategoryMeta();
}

function syncCategoryChecks() {
  $$('#category-groups input.category-check').forEach((input) => {
    input.checked = state.categoryDraft.has(input.dataset.id);
  });
  updateCategoryMeta();
}

function updateCategoryMeta() {
  const total = state.categories.total || 0;
  const count = state.categoryDraft.size;

  $('#category-selected-count').textContent = `Выбрано: ${count} из ${total}`;

  const all = $('#category-all-toggle');
  all.checked = count > 0 && count === total;
  all.indeterminate = count > 0 && count < total;

  for (const group of state.categories.groups) {
    const selected = group.categories.filter((item) => state.categoryDraft.has(item.id)).length;
    const counter = $(`[data-count-for="${group.id}"]`);
    if (counter) counter.textContent = `${selected}/${group.categories.length}`;
    const button = $(`.category-group-toggle[data-group="${group.id}"]`);
    if (button) button.textContent = selected === group.categories.length ? 'Снять' : 'Выбрать все';
  }

  const note = $('#category-note');
  if (!count) {
    note.textContent = 'Ничего не выбрано. Можно применить и указать свой запрос в поле слева.';
    note.classList.remove('is-warning');
  } else if (count > 24) {
    note.textContent = `${count} ${plural(count, 'ниша', 'ниши', 'ниш')} — это надолго. Лучше разбить на несколько запусков: скан можно остановить, найденное сохранится.`;
    note.classList.add('is-warning');
  } else {
    note.textContent = `${count} ${plural(count, 'ниша', 'ниши', 'ниш')} · сканирование идёт по очереди, по всем квадратам города.`;
    note.classList.remove('is-warning');
  }

  $('#category-apply').textContent = count ? `Применить · ${count}` : 'Применить';
}

async function openCategoryModal() {
  if (!state.categoriesLoaded) await loadCategories();
  if (!state.categoriesLoaded) {
    toast('Каталог категорий не загрузился — введите свой запрос в поле слева.', 'error');
    return;
  }

  state.categoryDraft = new Set(state.selectedCategories);
  state.categoryFocus = document.activeElement;
  $('#category-search').value = '';
  renderCategoryGroups('');
  $('#category-backdrop').hidden = false;
  document.body.classList.add('modal-open');
  $('#category-search').focus();
}

function closeCategoryModal() {
  $('#category-backdrop').hidden = true;
  document.body.classList.remove('modal-open');
  const focus = state.categoryFocus;
  state.categoryFocus = null;
  if (focus && typeof focus.focus === 'function') focus.focus();
}

function applyCategorySelection() {
  state.selectedCategories = new Set(state.categoryDraft);
  try {
    window.localStorage.setItem(
      CATEGORY_STORAGE_KEY,
      JSON.stringify([...state.selectedCategories]),
    );
  } catch (_) {
    /* приватный режим — просто не запоминаем */
  }
  updateCategoryTrigger();
  updateScanEstimate();
  closeCategoryModal();
}

function replaceCategoryDraft(ids) {
  state.categoryDraft = new Set(ids);
  syncCategoryChecks();
}

function initCategories() {
  $('#category-trigger').addEventListener('click', openCategoryModal);
  $('#category-close').addEventListener('click', closeCategoryModal);
  $('#category-cancel').addEventListener('click', closeCategoryModal);
  $('#category-apply').addEventListener('click', applyCategorySelection);
  $('#category-backdrop').addEventListener('click', (event) => {
    if (event.target === $('#category-backdrop')) closeCategoryModal();
  });

  $('#category-search').addEventListener('input', debounce((event) => {
    renderCategoryGroups(event.target.value);
  }, 120));

  $('#category-groups').addEventListener('change', (event) => {
    const input = event.target.closest('input.category-check');
    if (!input) return;
    if (input.checked) state.categoryDraft.add(input.dataset.id);
    else state.categoryDraft.delete(input.dataset.id);
    updateCategoryMeta();
  });

  $('#category-groups').addEventListener('click', (event) => {
    const button = event.target.closest('.category-group-toggle');
    if (!button) return;
    const group = state.categories.groups.find((item) => item.id === button.dataset.group);
    if (!group) return;
    const allOn = group.categories.every((item) => state.categoryDraft.has(item.id));
    for (const item of group.categories) {
      if (allOn) state.categoryDraft.delete(item.id);
      else state.categoryDraft.add(item.id);
    }
    syncCategoryChecks();
  });

  $('#category-all-toggle').addEventListener('change', (event) => {
    if (event.target.checked) {
      replaceCategoryDraft(state.categories.groups.flatMap((group) => group.categories.map((item) => item.id)));
    } else {
      replaceCategoryDraft([]);
    }
  });

  $('#category-all').addEventListener('click', () => {
    replaceCategoryDraft(state.categories.groups.flatMap((group) => group.categories.map((item) => item.id)));
  });

  $('#category-popular').addEventListener('click', () => {
    replaceCategoryDraft(state.categories.popular);
  });

  $('#category-none').addEventListener('click', () => replaceCategoryDraft([]));

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !$('#category-backdrop').hidden) {
      event.stopPropagation();
      closeCategoryModal();
    }
  });

  $('#scan-city').addEventListener('input', () => refreshScanEstimate());
  $('#scan-cell').addEventListener('change', () => updateScanEstimate());
  $('#scan-query').addEventListener('input', () => refreshScanEstimate());
}

let estimateTimer = null;
function refreshScanEstimate() {
  window.clearTimeout(estimateTimer);
  estimateTimer = window.setTimeout(updateScanEstimate, 450);
}

function updateScanEstimate() {
  const node = $('#scan-estimate');
  const count = state.selectedCategories.size;
  const custom = $('#scan-query').value.trim();
  const city = $('#scan-city').value.trim();

  if (!count && !custom) {
    node.textContent = 'Выберите ниши или введите свой запрос.';
    node.classList.remove('is-warning');
    return;
  }
  if (!city) {
    node.textContent = 'Укажите город, чтобы увидеть объём сканирования.';
    node.classList.remove('is-warning');
    return;
  }

  const seq = ++state.estimateSeq;
  node.textContent = 'Считаем объём…';
  node.classList.remove('is-warning');

  api('/scan/estimate', {
    method: 'POST',
    body: JSON.stringify({
      categories: [...state.selectedCategories],
      query: custom || null,
      city,
      cell_deg: Number($('#scan-cell').value) || 0.05,
    }),
  })
    .then((data) => {
      if (seq !== state.estimateSeq) return;
      const queries = Number(data.queries || 0);
      const cells = Number(data.cells || 0);
      const requests = Number(data.requests || 0);
      if (!queries) {
        node.textContent = 'Выберите ниши или введите свой запрос.';
        return;
      }
      let text = `${queries} ${plural(queries, 'запрос', 'запроса', 'запросов')} × ${cells} ${plural(cells, 'квадрат', 'квадрата', 'квадратов')} = ${requests} ${plural(requests, 'запрос', 'запроса', 'запросов')} на источник`;
      if (requests > 600) {
        text += ' · это надолго, скан можно остановить в любой момент';
        node.classList.add('is-warning');
      }
      node.textContent = text;
    })
    .catch(() => {
      if (seq === state.estimateSeq) node.textContent = '';
    });
}

/* ---------- сканирование ---------- */

async function startScan(event) {
  event.preventDefault();

  const categories = [...state.selectedCategories];
  const customQuery = $('#scan-query').value.trim();
  const city = $('#scan-city').value.trim();
  const cellDeg = Number($('#scan-cell').value) || 0.05;
  const sources = selectedSources();

  if (!categories.length && !customQuery) {
    toast('Выберите хотя бы одну категорию или введите свой запрос.', 'error');
    openCategoryModal();
    return;
  }
  if (!city) {
    toast('Укажите город: например, «Тюмень».', 'error');
    $('#scan-city').focus();
    return;
  }
  if (!sources.length) {
    toast('Выберите хотя бы один источник.', 'error');
    return;
  }

  const button = $('#scan-start');
  button.disabled = true;
  $('#scan-spinner').hidden = false;
  setJobStatus('запуск');

  try {
    const result = await api('/scan', {
      method: 'POST',
      body: JSON.stringify({
        categories,
        query: customQuery || null,
        city,
        cell_deg: cellDeg,
        sources,
        limit_per_cell: 40,
      }),
    });
    state.jobId = result.job_id;
    await openJob(result.job_id, result.cells);
    const queries = Number(result.queries || 1);
    const cells = Number(result.cells || 0);
    toast(
      `Сканирование запущено: ${queries} ${plural(queries, 'запрос', 'запроса', 'запросов')} × ${cells} ${plural(cells, 'квадрат', 'квадрата', 'квадратов')}.`,
      'success',
    );
  } catch (error) {
    toast(`Не удалось запустить сканирование: ${error.message}`, 'error');
    setJobStatus('ошибка');
  } finally {
    button.disabled = false;
    $('#scan-spinner').hidden = true;
  }
}

async function stopScan() {
  if (!state.jobId) return;
  const button = $('#scan-stop');
  button.disabled = true;
  try {
    await api(`/jobs/${state.jobId}/stop`, { method: 'POST' });
    toast('Сканирование остановлено. Найденные лиды сохранены.', 'success');
  } catch (error) {
    toast(`Не удалось остановить сканирование: ${error.message}`, 'error');
  } finally {
    button.disabled = false;
  }
}

async function openJob(jobId, expectedCells = 0) {
  state.cells.clear();
  try {
    const job = await api(`/jobs/${jobId}`);
    buildGrid(job.cells || []);
    updateProgress(job);
    setJobStatus(job.status);
  } catch (error) {
    toast(`Не удалось получить задание: ${error.message}`, 'error');
    if (expectedCells) buildGrid([]);
  }
}

function buildGrid(cells) {
  const grid = $('#cell-grid');
  grid.innerHTML = '';
  state.cells.clear();

  if (!cells.length) {
    $('#grid-empty').hidden = false;
    $('#cells-count').textContent = '0';
    grid.style.gridTemplateColumns = '';
    return;
  }

  let nx = 0;
  let ny = 0;
  for (const [ix, iy] of cells) {
    nx = Math.max(nx, ix + 1);
    ny = Math.max(ny, iy + 1);
  }
  state.grid = { nx, ny };

  grid.style.gridTemplateColumns = `repeat(${nx}, minmax(0, 1fr))`;
  $('#grid-empty').hidden = true;
  $('#cells-count').textContent = String(cells.length);

  const fragment = document.createDocumentFragment();
  for (const [ix, iy, status, found] of cells) {
    const cell = document.createElement('div');
    cell.className = `cell cell--${normalizeCellStatus(status)}`;
    cell.dataset.ix = String(ix);
    cell.dataset.iy = String(iy);
    cell.title = `квадрат ${ix + 1}×${iy + 1}${found ? ` · найдено: ${found}` : ''}`;
    cell.setAttribute('role', 'listitem');
    state.cells.set(`${ix}:${iy}`, { status, found });
    fragment.appendChild(cell);
  }
  grid.appendChild(fragment);
}

function normalizeCellStatus(status) {
  return ['pending', 'running', 'done', 'error'].includes(status) ? status : 'pending';
}

function updateCell(ix, iy, status, found) {
  const key = `${ix}:${iy}`;
  state.cells.set(key, { status, found });
  const node = $(`.cell[data-ix="${ix}"][data-iy="${iy}"]`);
  if (!node) return;
  node.className = `cell cell--${normalizeCellStatus(status)}`;
  if (found) node.title = `квадрат ${ix + 1}×${iy + 1} · найдено: ${found}`;
}

function resetCellGrid() {
  for (const [key, value] of state.cells) {
    state.cells.set(key, { status: 'pending', found: 0 });
  }
  $$('#cell-grid .cell').forEach((node) => {
    node.className = 'cell cell--pending';
    const ix = Number(node.dataset.ix || 0) + 1;
    const iy = Number(node.dataset.iy || 0) + 1;
    node.title = `квадрат ${ix}×${iy}`;
  });
}

function updateProgress(job) {
  const total = Number(job.cells_total || 0);
  const done = Number(job.cells_done || 0);
  const found = Number(job.leads_found || 0);
  const queriesTotal = Math.max(1, Number(job.queries_total || 1));
  const queryIndex = Math.max(0, Math.min(Number(job.query_index || 0), queriesTotal - 1));
  const pass = total ? Math.min(1, done / total) : 0;
  const percent = Math.round(((queryIndex + pass) / queriesTotal) * 100);

  $('#progress-fill').style.width = `${percent}%`;
  const bar = $('#progress-bar');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', String(total));
  bar.setAttribute('aria-valuenow', String(done));
  $('#progress-text').textContent =
    `${done} / ${total} ${plural(total, 'квадрат', 'квадрата', 'квадратов')} · ${found} ${plural(found, 'лид', 'лида', 'лидов')}`;

  const sub = $('#progress-sub');
  if (queriesTotal > 1) {
    sub.hidden = false;
    const current = String(job.current_query || '').trim();
    sub.textContent = `Категория ${queryIndex + 1} из ${queriesTotal}${current ? `: ${current}` : ''}`;
  } else {
    sub.hidden = true;
    sub.textContent = '';
  }
}

function setJobStatus(status) {
  const labels = {
    queued: 'в очереди',
    running: 'идёт',
    paused: 'пауза',
    stopped: 'остановлено',
    done: 'готово',
    error: 'ошибка',
    'запуск': 'запуск',
  };
  const chip = $('#job-status');
  chip.textContent = labels[status] || status || 'ожидание';
  chip.dataset.status = status || '';

  const stop = $('#scan-stop');
  if (stop) {
    const active = ['queued', 'running', 'paused', 'запуск'].includes(status);
    stop.hidden = !(active && state.jobId);
    if (stop.hidden) stop.disabled = false;
  }
}

/* ---------- список лидов ---------- */

function leadMatchesFilters(lead) {
  const f = state.filters;
  if (f.status && lead.status !== f.status) return false;
  if (f.fav && !lead.is_favorite) return false;
  if (f.site === 'no' && lead.has_site) return false;
  if (f.site === 'yes' && !lead.has_site) return false;
  if (f.q) {
    const haystack = [lead.name, lead.address, lead.phone, lead.category]
      .join(' ')
      .toLowerCase();
    if (!haystack.includes(f.q.toLowerCase())) return false;
  }
  return true;
}

async function loadLeads(reset = true) {
  const params = new URLSearchParams();
  if (state.filters.q) params.set('q', state.filters.q);
  if (state.filters.status) params.set('status', state.filters.status);
  if (state.filters.fav) params.set('fav', 'true');
  if (state.filters.site) params.set('site', state.filters.site);
  params.set('sort', SORT_MAP[state.filters.sort] || 'new');
  params.set('limit', String(state.limit));
  params.set('offset', reset ? '0' : String(state.leads.length));

  $('#leads-loading').hidden = false;
  $('#leads-error').hidden = true;
  $('#leads-empty').hidden = true;

  try {
    const data = await api(`/leads?${params.toString()}`);
    state.total = data.total;
    state.leads = reset ? data.items : state.leads.concat(data.items);
    renderLeads();
  } catch (error) {
    $('#leads-error').hidden = false;
    toast(`Не удалось загрузить лиды: ${error.message}`, 'error');
  } finally {
    $('#leads-loading').hidden = true;
  }
}

function renderLeads() {
  const list = $('#lead-list');
  const empty = $('#leads-empty');
  const loadMore = $('#load-more-wrap');

  list.innerHTML = '';
  $('#leads-total').textContent = String(state.total);
  $('#leads-summary').textContent = state.total
    ? `${state.filters.fav ? 'избранных' : 'в базе'}`
    : '';

  if (!state.leads.length) {
    empty.hidden = false;
    loadMore.hidden = true;
    $('#empty-reset').hidden = !hasActiveFilters();
    return;
  }

  empty.hidden = true;
  const fragment = document.createDocumentFragment();
  for (const lead of state.leads) fragment.appendChild(renderLeadCard(lead));
  list.appendChild(fragment);

  loadMore.hidden = state.leads.length >= state.total;
}

function hasActiveFilters() {
  const f = state.filters;
  return Boolean(f.q || f.status || f.fav || f.site);
}

// Перезагрузка списка с задержкой: при потоке новых лидов и сортировке
// не по дате иначе порядок в списке разъезжается.
const scheduleReload = debounce(() => loadLeads(true), 800);

function renderLeadCard(lead) {
  const card = document.createElement('article');
  card.className = `lead-card${lead.id === state.selectedId ? ' is-selected' : ''}`;
  card.dataset.id = lead.id;
  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  card.setAttribute('aria-label', `Открыть карточку: ${lead.name}`);

  const badges = [];
  if (lead.is_demo) badges.push('<span class="badge badge--demo">ДЕМО</span>');
  for (const source of lead.sources || [lead.source]) {
    badges.push(`<span class="badge badge--source">${escapeHtml(SOURCE_LABELS[source] || source)}</span>`);
  }
  badges.push(`<span class="badge badge--${escapeHtml(lead.status)}">${escapeHtml(STATUS_LABELS[lead.status] || lead.status)}</span>`);
  if ((lead.locations_count || 0) > 1) {
    badges.push(`<span class="badge badge--chain">сеть · ${lead.locations_count}</span>`);
  }

  const rating = lead.rating
    ? `<span class="lead-rating"><span class="star">★</span> ${escapeHtml(lead.rating)}</span>`
    : '<span class="lead-rating lead-rating--empty">★ —</span>';

  card.innerHTML = `
    <div class="lead-top">
      <div class="lead-name">${escapeHtml(lead.name)}</div>
      <div class="lead-badges">${badges.join('')}</div>
    </div>
    <div class="lead-sub">
      ${lead.category ? `<div class="lead-category">${escapeHtml(lead.category)}</div>` : ''}
      ${lead.address ? `<div class="lead-address" title="${escapeHtml(lead.address)}">${escapeHtml(lead.address)}</div>` : ''}
    </div>
    <div class="lead-bottom">
      ${rating}
      <span class="lead-phone">${lead.phone ? escapeHtml(lead.phone) : '<span class="no-data">нет телефона</span>'}</span>
      <div class="lead-actions">
        <button class="icon-btn lead-action lead-action--favorite" type="button"
                data-act="favorite" aria-pressed="${lead.is_favorite ? 'true' : 'false'}"
                aria-label="В избранное" title="В избранное">★</button>
        <button class="icon-btn lead-action lead-action--called" type="button"
                data-act="called" aria-pressed="${lead.status === 'called' ? 'true' : 'false'}"
                aria-label="Отзвонено" title="Отзвонено">☎</button>
        <button class="icon-btn lead-action" type="button"
                data-act="in_progress" aria-pressed="${lead.status === 'in_progress' ? 'true' : 'false'}"
                aria-label="В работе" title="В работе">⚙</button>
        <button class="icon-btn lead-action lead-action--reject" type="button"
                data-act="reject" aria-pressed="${lead.status === 'rejected' ? 'true' : 'false'}"
                aria-label="Отказ" title="Отказ">✕</button>
      </div>
    </div>`;

  card.addEventListener('click', (event) => {
    const action = event.target.closest('[data-act]');
    if (action) {
      event.stopPropagation();
      handleLeadAction(lead.id, action.dataset.act, action);
      return;
    }
    selectLead(lead.id);
  });

  card.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      selectLead(lead.id);
    }
  });

  return card;
}

async function handleLeadAction(leadId, action, button) {
  const lead = state.leads.find((item) => item.id === leadId);
  if (!lead) return;

  let patch = {};
  if (action === 'favorite') patch = { is_favorite: !lead.is_favorite };
  if (action === 'new') patch = { status: 'new' };
  if (action === 'called') patch = { status: lead.status === 'called' ? 'new' : 'called' };
  if (action === 'in_progress') patch = { status: lead.status === 'in_progress' ? 'new' : 'in_progress' };
  if (action === 'reject') patch = { status: lead.status === 'rejected' ? 'new' : 'rejected' };

  if (button) button.disabled = true;
  try {
    const data = await api(`/leads/${leadId}`, {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    applyLeadUpdate(data.lead, false);
    if (state.selectedId === leadId) renderDetail(data.lead);
  } catch (error) {
    toast(`Не удалось сохранить: ${error.message}`, 'error');
  } finally {
    if (button) button.disabled = false;
  }
}

function applyLeadUpdate(lead, isNew) {
  const index = state.leads.findIndex((item) => item.id === lead.id);

  if (!leadMatchesFilters(lead)) {
    if (index >= 0) {
      state.leads.splice(index, 1);
      state.total = Math.max(0, state.total - 1);
      renderLeads();
    }
    return;
  }

  if (index >= 0) {
    state.leads[index] = lead;
  } else if (isNew) {
    state.leads.unshift(lead);
    state.total += 1;
    if (state.filters.sort !== 'newest') {
      scheduleReload();
      return;
    }
  } else {
    return;
  }

  const list = $('#lead-list');
  if (!state.leads.length) {
    renderLeads();
    return;
  }
  // точечная замена карточки, чтобы не терять прокрутку
  const existing = list.querySelector(`.lead-card[data-id="${lead.id}"]`);
  const fresh = renderLeadCard(lead);
  if (existing) existing.replaceWith(fresh);
  else list.prepend(fresh);

  $('#leads-total').textContent = String(state.total);
  $('#leads-empty').hidden = true;
  $('#load-more-wrap').hidden = state.leads.length >= state.total;
}

/* ---------- карточка ---------- */

function selectLead(id) {
  state.selectedId = id;
  $$('.lead-card').forEach((card) => {
    card.classList.toggle('is-selected', card.dataset.id === id);
  });
  openDetail(id);
}

function openDetail(id) {
  const pane = $('#detail-pane');
  pane.hidden = false;
  $('#detail-backdrop').hidden = false;
  pane.classList.add('is-open');

  $('#detail-content').innerHTML = `
    <div class="detail-skeleton">
      <div class="skeleton-line skeleton-line--title"></div>
      <div class="skeleton-line"></div>
      <div class="skeleton-line skeleton-line--short"></div>
      <div class="skeleton-line"></div>
      <div class="skeleton-line skeleton-line--short"></div>
    </div>`;

  api(`/leads/${id}`)
    .then((lead) => renderDetail(lead))
    .catch((error) => {
      $('#detail-content').innerHTML = `
        <div class="detail-error">
          <p>Не удалось загрузить карточку: ${escapeHtml(error.message)}</p>
          <button class="btn btn--ghost" type="button" id="detail-retry">Повторить</button>
        </div>`;
      $('#detail-retry')?.addEventListener('click', () => openDetail(id));
    });
}

function renderDetail(lead) {
  const facts = lead.facts || {};
  const photos = (lead.photos || []).filter((url) => safeUrl(url));
  const priceFacts = facts.price || [];
  const emailFacts = facts.email || [];
  const socialFacts = facts.social || [];
  const directorFacts = facts.director || [];
  const innFacts = facts.inn || [];
  const hoursFacts = facts.hours || [];
  const descriptionFacts = facts.description || [];
  const locations = (lead.locations || []).filter(
    (location) => location.address || location.phone,
  );

  // В business_source хранится по записи на каждую точку/скан. В блоке
  // «Источники» показываем каждый источник один раз, а не N одинаковых плашек.
  const sourceLinks = [];
  const seenSources = new Set();
  for (const item of lead.source_links || []) {
    const source = item.source || '';
    if (seenSources.has(source)) continue;
    seenSources.add(source);
    sourceLinks.push(item);
  }
  if (!sourceLinks.length) {
    for (const source of lead.sources || [lead.source]) {
      sourceLinks.push({ source, url: '' });
    }
  }

  const badges = [];
  if (lead.is_demo) badges.push('<span class="badge badge--demo">ДЕМО</span>');
  for (const source of lead.sources || [lead.source]) {
    badges.push(`<span class="badge badge--source">${escapeHtml(SOURCE_LABELS[source] || source)}</span>`);
  }
  badges.push(`<span class="badge badge--${escapeHtml(lead.status)}">${escapeHtml(STATUS_LABELS[lead.status] || lead.status)}</span>`);
  if (locations.length > 1) {
    badges.push(`<span class="badge badge--chain">сеть · ${locations.length}</span>`);
  }

  const website = safeUrl(lead.website);
  const phoneHref = telHref(lead.phone);

  const ratingBlock = lead.rating
    ? `<span class="detail-rating"><span class="star">★</span> ${escapeHtml(lead.rating)}${
        lead.reviews_count ? ` · ${escapeHtml(lead.reviews_count)} ${plural(lead.reviews_count, 'отзыв', 'отзыва', 'отзывов')}` : ''
      }</span>`
    : '<span class="detail-rating muted">рейтинга нет</span>';

  const html = `
    <div class="detail-title-block">
      <div class="detail-badges">${badges.join('')}</div>
      <h2 class="detail-name">${escapeHtml(lead.name)}</h2>
      ${lead.category ? `<div class="detail-category">${escapeHtml(lead.category)}</div>` : ''}
      <div class="detail-summary">
        ${ratingBlock}
        ${website ? `<a class="detail-website" href="${escapeHtml(website)}" target="_blank" rel="noopener noreferrer">${escapeHtml(lead.website)}</a>` : '<span class="no-data">сайта нет</span>'}
      </div>
    </div>

    <section class="detail-section">
      <h3 class="detail-section-title">Статус работы</h3>
      <div class="lead-actions" id="detail-actions">
        ${STATUS_ORDER.map((status) => `
          <button class="icon-btn lead-action ${status === 'called' ? 'lead-action--called' : status === 'in_progress' ? 'lead-action--favorite' : status === 'rejected' ? 'lead-action--reject' : ''}"
                  type="button" data-status="${status}"
                  aria-pressed="${lead.status === status ? 'true' : 'false'}"
                  aria-label="${escapeHtml(STATUS_LABELS[status])}" title="${escapeHtml(STATUS_LABELS[status])}">
            ${status === 'called' ? '☎' : status === 'in_progress' ? '⚙' : status === 'rejected' ? '✕' : '○'}
          </button>`).join('')}
      </div>
    </section>

    <section class="detail-section">
      <h3 class="detail-section-title">Основное</h3>
      <dl class="detail-rows">
        <div class="detail-row"><dt>Телефон</dt><dd>${
          lead.phone && phoneHref
            ? `<a href="${escapeHtml(phoneHref)}">${escapeHtml(lead.phone)}</a>`
            : textOr(lead.phone)
        }</dd></div>
        <div class="detail-row"><dt>Адрес</dt><dd>${textOr(lead.address)}</dd></div>
        <div class="detail-row"><dt>Часы работы</dt><dd>${textOr(lead.hours)}</dd></div>
        <div class="detail-row"><dt>Ценовой уровень</dt><dd>${textOr(lead.price_range)}</dd></div>
        <div class="detail-row"><dt>Добавлено</dt><dd>${formatDate(lead.first_seen)}</dd></div>
        <div class="detail-row"><dt>Обновлено</dt><dd>${formatDate(lead.updated_at)}</dd></div>
      </dl>
    </section>

    ${locations.length > 1 ? `
    <section class="detail-section">
      <h3 class="detail-section-title">Точки сети <span class="count">${locations.length}</span></h3>
      <ul class="location-list">${locations.map((location, index) => `
        <li class="location-item">
          <span class="location-num">${index + 1}</span>
          <div class="location-body">
            <div class="location-address">${escapeHtml(location.address || 'адрес не указан')}</div>
            <div class="location-meta">
              ${location.phone && telHref(location.phone)
                ? `<a href="${escapeHtml(telHref(location.phone))}">${escapeHtml(location.phone)}</a>`
                : '<span class="no-data">телефон не указан</span>'}
              <span class="badge badge--source">${escapeHtml((location.sources || [location.source]).map((source) => SOURCE_LABELS[source] || source).join(' · '))}</span>
            </div>
          </div>
        </li>`).join('')}</ul>
    </section>` : ''}

    ${photos.length ? `
    <section class="detail-section">
      <h3 class="detail-section-title">Фото</h3>
      <div class="photo-grid">${photos.map((url) => `
        <a class="photo-item" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">
          <img src="${escapeHtml(url)}" alt="Фото: ${escapeHtml(lead.name)}" loading="lazy" referrerpolicy="no-referrer">
        </a>`).join('')}</div>
    </section>` : ''}

    ${priceFacts.length ? `
    <section class="detail-section">
      <h3 class="detail-section-title">Цены</h3>
      <div class="fact-list">${priceFacts.map((fact) => `
        <div class="fact-item">
          <span class="fact-value">${escapeHtml(fact.value)}</span>
          ${fact.source_url ? `<a class="fact-source" href="${escapeHtml(fact.source_url)}" target="_blank" rel="noopener noreferrer">источник</a>` : ''}
        </div>`).join('')}</div>
    </section>` : ''}

    <section class="detail-section">
      <h3 class="detail-section-title">Контакты из интернета</h3>
      ${(emailFacts.length || socialFacts.length || directorFacts.length || innFacts.length)
        ? `<dl class="contact-block">
            ${emailFacts.map((fact) => `
              <div class="contact-row"><dt>Email</dt><dd>
                <a href="mailto:${escapeHtml(fact.value)}">${escapeHtml(fact.value)}</a>
                ${fact.source_url ? `<a class="fact-source" href="${escapeHtml(fact.source_url)}" target="_blank" rel="noopener noreferrer">источник</a>` : ''}
              </dd></div>`).join('')}
            ${socialFacts.map((fact) => `
              <div class="contact-row"><dt>Соцсеть</dt><dd>
                <a href="${escapeHtml(safeUrl(fact.value) || '#')}" target="_blank" rel="noopener noreferrer">${escapeHtml(fact.value)}</a>
              </dd></div>`).join('')}
            ${directorFacts.map((fact) => `
              <div class="contact-row"><dt>Руководитель</dt><dd>
                ${escapeHtml(fact.value)}
                ${fact.source_url ? `<a class="fact-source" href="${escapeHtml(fact.source_url)}" target="_blank" rel="noopener noreferrer">источник</a>` : ''}
              </dd></div>`).join('')}
            ${innFacts.map((fact) => `
              <div class="contact-row"><dt>ИНН</dt><dd>${escapeHtml(fact.value)}</dd></div>`).join('')}
          </dl>`
        : '<p class="no-data">Контактов на сайте не найдено. Нажмите «Обогатить данные», чтобы попробовать ещё раз.</p>'}
    </section>

    ${(hoursFacts.length || descriptionFacts.length) ? `
    <section class="detail-section">
      <h3 class="detail-section-title">С сайта</h3>
      <div class="fact-list">
        ${descriptionFacts.map((fact) => `
          <div class="fact-item"><span class="fact-value">${escapeHtml(fact.value)}</span>
          ${fact.source_url ? `<a class="fact-source" href="${escapeHtml(fact.source_url)}" target="_blank" rel="noopener noreferrer">источник</a>` : ''}</div>`).join('')}
        ${hoursFacts.map((fact) => `
          <div class="fact-item"><span class="fact-value">${escapeHtml(fact.value)}</span>
          ${fact.source_url ? `<a class="fact-source" href="${escapeHtml(fact.source_url)}" target="_blank" rel="noopener noreferrer">источник</a>` : ''}</div>`).join('')}
      </div>
    </section>` : ''}

    <section class="detail-section">
      <h3 class="detail-section-title">Источники</h3>
      <div class="source-chips">
        ${sourceLinks.map((item) => item.url
          ? `<a class="source-chip" href="${escapeHtml(safeUrl(item.url) || '#')}" target="_blank" rel="noopener noreferrer">${escapeHtml(SOURCE_LABELS[item.source] || item.source)}</a>`
          : `<span class="source-chip">${escapeHtml(SOURCE_LABELS[item.source] || item.source)}</span>`).join('')}
      </div>
    </section>

    <div class="enrich-block">
      <div class="enrich-actions">
        <button class="btn btn--secondary" id="enrich-btn" type="button">Обогатить данные</button>
      </div>
      <div class="enrich-result" id="enrich-result" hidden></div>
    </div>

    <div class="note-block">
      <h3 class="detail-section-title">Заметка <span class="note-status" id="note-status"></span></h3>
      <textarea id="note-input" rows="3" placeholder="Что обсудили, когда перезвонить…">${escapeHtml(lead.note || '')}</textarea>
      <div class="note-actions">
        <button class="btn btn--ghost" id="note-save" type="button">Сохранить</button>
      </div>
    </div>`;

  const content = $('#detail-content');
  content.innerHTML = html;

  // битые фото скрываем, а не подменяем заглушкой
  $$('.photo-item img', content).forEach((img) => {
    img.addEventListener('error', () => {
      const item = img.closest('.photo-item');
      if (item) item.remove();
      if (!$$('.photo-item', content).length) {
        const grid = $('.photo-grid', content);
        if (grid) grid.replaceWith(Object.assign(document.createElement('p'), {
          className: 'no-data',
          textContent: 'Фото не загрузились.',
        }));
      }
    });
  });

  $$('#detail-actions [data-status]', content).forEach((button) => {
    button.addEventListener('click', () => {
      const status = button.dataset.status;
      handleLeadAction(lead.id, status === 'rejected' ? 'reject' : status);
    });
  });

  const noteInput = $('#note-input', content);
  const noteStatus = $('#note-status', content);
  noteInput.addEventListener('input', () => {
    noteStatus.textContent = 'не сохранено';
    noteStatus.className = 'note-status is-dirty';
  });

  $('#note-save', content).addEventListener('click', async () => {
    try {
      const data = await api(`/leads/${lead.id}`, {
        method: 'PATCH',
        body: JSON.stringify({ note: noteInput.value }),
      });
      noteStatus.textContent = 'сохранено';
      noteStatus.className = 'note-status is-saved';
      applyLeadUpdate(data.lead, false);
    } catch (error) {
      toast(`Не удалось сохранить заметку: ${error.message}`, 'error');
    }
  });

  $('#enrich-btn', content).addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const result = $('#enrich-result', content);
    button.disabled = true;
    button.textContent = 'Обогащаем…';
    result.hidden = false;
    result.className = 'enrich-result';
    result.textContent = 'Обходим сайт и собираем контакты. Это может занять до минуты…';

    try {
      const data = await api(`/leads/${lead.id}/enrich`, { method: 'POST' });
      const added = data.added || {};
      const total = Object.values(added).reduce((sum, value) => sum + Number(value || 0), 0);
      const parts = Object.entries(added)
        .filter(([, value]) => Number(value) > 0)
        .map(([kind, value]) => `${factLabel(kind)}: ${value}`);

      result.className = `enrich-result ${total ? 'is-ok' : ''}`;
      result.textContent = total
        ? `Добавлено — ${parts.join(', ')}.`
        : 'Новых данных не найдено.';
      if (data.errors?.length) {
        result.textContent += ` Ошибки: ${data.errors.join('; ')}`;
      }
      if (data.lead) renderDetail(data.lead);
    } catch (error) {
      result.className = 'enrich-result is-error';
      result.textContent = `Не удалось обогатить: ${error.message}`;
    } finally {
      button.disabled = false;
      button.textContent = 'Обогатить данные';
    }
  });
}

function factLabel(kind) {
  return {
    email: 'email',
    phone: 'телефонов',
    price: 'цен',
    photo: 'фото',
    social: 'соцсетей',
    director: 'руководителей',
    description: 'описаний',
    inn: 'ИНН',
    hours: 'часов работы',
    address: 'адресов',
  }[kind] || kind;
}

function formatDate(value) {
  if (!value) return '<span class="no-data">нет данных</span>';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return escapeHtml(date.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }));
}

/* ---------- SSE ---------- */

function setStream(connected) {
  state.stream.connected = connected;
  const dot = $('#stream-dot');
  dot.classList.toggle('is-online', connected);
  dot.classList.toggle('is-offline', !connected);
  $('#stream-text').textContent = connected ? 'Поток событий: онлайн' : 'Поток событий: переподключение…';
}

function connectEvents() {
  const source = new EventSource('/api/events');

  source.onopen = () => setStream(true);

  source.onmessage = (event) => {
    setStream(true);
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    handleEvent(payload);
  };

  source.onerror = () => {
    setStream(false);
    source.close();
    window.setTimeout(connectEvents, 3000);
  };
}

function handleEvent(event) {
  if (event.type === 'hello') return;

  if (event.type === 'cell') {
    if (event.ix >= 0) updateCell(event.ix, event.iy, event.status, event.found);
    return;
  }

  if (event.type === 'lead' && event.lead?.id) {
    if (event.job_id && state.jobId && event.job_id !== state.jobId) return;
    applyLeadUpdate(event.lead, Boolean(event.is_new));
    if (state.selectedId === event.lead.id && !$('#detail-content .detail-skeleton')) {
      // не затираем несохранённую заметку, пока пользователь её печатает
      const noteDirty = $('#note-status')?.classList.contains('is-dirty');
      if (!noteDirty) {
        api(`/leads/${event.lead.id}`).then(renderDetail).catch(() => {});
      }
    }
    return;
  }

  if (event.type === 'job') {
    if (state.jobId && event.job_id && event.job_id !== state.jobId) return;
    if (event.reset_cells) resetCellGrid();
    if (event.status) setJobStatus(event.status);
    if (event.cells_total !== undefined) updateProgress(event);
    if (['done', 'stopped', 'error'].includes(event.status)) {
      loadLeads(true);
      api('/stats').then(() => {}).catch(() => {});
    }
    return;
  }

  if (event.type === 'error') {
    toast(`${event.source || 'источник'}: ${event.message || 'ошибка'}`, 'error', 8000);
  }
}

/* ---------- фильтры ---------- */

function initFilters() {
  const reload = debounce(() => loadLeads(true), 250);

  $('#filter-q').addEventListener('input', (event) => {
    state.filters.q = event.target.value.trim();
    $('#filter-clear').hidden = !state.filters.q;
    reload();
  });

  $('#filter-clear').addEventListener('click', () => {
    $('#filter-q').value = '';
    state.filters.q = '';
    $('#filter-clear').hidden = true;
    loadLeads(true);
  });

  $('#filter-status').addEventListener('change', (event) => {
    state.filters.status = event.target.value;
    loadLeads(true);
  });

  $('#filter-sort').addEventListener('change', (event) => {
    state.filters.sort = event.target.value;
    loadLeads(true);
  });

  $('#filter-site').addEventListener('change', (event) => {
    state.filters.site = event.target.value;
    loadLeads(true);
  });

  $('#filter-fav').addEventListener('change', (event) => {
    state.filters.fav = event.target.checked;
    loadLeads(true);
  });

  $('#empty-reset').addEventListener('click', resetFilters);
  $('#load-more-btn').addEventListener('click', () => loadLeads(false));
  $('#leads-error-retry').addEventListener('click', () => loadLeads(true));
}

function resetFilters() {
  state.filters = { q: '', status: '', sort: 'newest', fav: false, site: '' };
  $('#filter-q').value = '';
  $('#filter-status').value = '';
  $('#filter-sort').value = 'newest';
  $('#filter-fav').checked = false;
  $('#filter-site').value = '';
  $('#filter-clear').hidden = true;
  loadLeads(true);
}

/* ---------- каркас ---------- */

function initDetail() {
  const close = () => {
    $('#detail-pane').hidden = true;
    $('#detail-pane').classList.remove('is-open');
    $('#detail-backdrop').hidden = true;
    state.selectedId = null;
    $$('.lead-card').forEach((card) => card.classList.remove('is-selected'));
  };
  $('#detail-close').addEventListener('click', close);
  $('#detail-backdrop').addEventListener('click', close);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      if (!$('#category-backdrop').hidden) return;
      close();
    }
  });
}

function init() {
  initFilters();
  initDetail();
  initCategories();
  loadCategories();
  loadHealth();
  loadLeads(true);
  connectEvents();

  $('#scan-form').addEventListener('submit', startScan);
  $('#scan-stop').addEventListener('click', stopScan);
}

document.addEventListener('DOMContentLoaded', init);
