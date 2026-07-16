// Feedback pane — standalone plugin UI (left-pane iframe).
// Auth: wiki 0.7.1 handshake — token arrives from the Shell via postMessage;
// no localStorage fallback when embedded (origin-wide storage is untrusted).

const API = '/api/p/plugin-feedback';

// ---- auth (0.7.1 handshake) ----
let TOKEN = null;
const waiters = [];

function embedded() {
  try { return window.self !== window.top; } catch { return true; }
}
window.addEventListener('message', (e) => {
  if (e.data && e.data.type === 'luna-auth' && typeof e.data.token === 'string') {
    TOKEN = e.data.token;
    waiters.splice(0).forEach((resolve) => resolve(TOKEN));
  }
});
function requestAuth() {
  try { window.parent?.postMessage({ type: 'luna-request-auth' }, '*'); } catch {}
}
function getToken(timeoutMs = 3000) {
  if (TOKEN) return Promise.resolve(TOKEN);
  if (!embedded()) {
    try { TOKEN = localStorage.getItem('luna.token'); } catch {}
    if (TOKEN) return Promise.resolve(TOKEN);
  }
  requestAuth();
  return new Promise((resolve) => {
    waiters.push(resolve);
    setTimeout(() => resolve(TOKEN), timeoutMs);
  });
}

async function api(method, path, body) {
  const doFetch = async () => {
    const token = await getToken();
    const init = { method, headers: { Authorization: `Bearer ${token || ''}` } };
    if (body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return fetch(`${API}${path}`, init);
  };
  let res = await doFetch();
  if (res.status === 401) { TOKEN = null; requestAuth(); res = await doFetch(); }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); if (j.detail) msg = j.detail; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

// ---- helpers ----
const el = (id) => document.getElementById(id);
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
const STATUS_LABEL = { open: 'Waiting for the team', answered: 'Team replied', closed: 'Closed' };
const CATEGORY_LABEL = { cost: 'Pricing', bug: 'Something broke', frustration: 'Frustration', feature: 'Idea', praise: 'Praise', other: 'General' };
const WHO_LABEL = { user: 'You', agent: 'Your Luna', admin: 'Luna team' };

function show(view) {
  ['list-view', 'new-view', 'detail-view'].forEach((id) =>
    el(id).classList.toggle('hidden', id !== view));
}
function setError(id, message) {
  const box = el(id);
  if (message) { box.textContent = message; box.classList.remove('hidden'); }
  else box.classList.add('hidden');
}

// ---- list view ----
let currentTicket = null;

async function loadList() {
  setError('error', null);
  let data;
  try {
    data = await api('GET', '/tickets');
  } catch (e) {
    el('headline').textContent = 'Feedback unavailable';
    setError('error', e.message);
    return;
  }
  const tickets = data.tickets || [];
  const open = tickets.filter((t) => t.status === 'open').length;
  const unread = tickets.filter((t) => t.unread).length;
  el('headline').textContent = unread
    ? `${unread} new ${unread === 1 ? 'reply' : 'replies'} from the team`
    : tickets.length
      ? `${open || 'No'} ticket${open === 1 ? '' : 's'} waiting for the team`
      : 'All quiet';
  el('empty').classList.toggle('hidden', tickets.length > 0);
  const list = el('list');
  list.innerHTML = '';
  for (const t of tickets) {
    const btn = document.createElement('button');
    btn.className = 'row';
    btn.innerHTML = `
      <span class="dot ${esc(t.status)}"></span>
      <span class="title">${esc(t.title)}</span>
      ${t.unread ? '<span class="pill unread">new reply</span>'
                 : `<span class="pill ${esc(t.status)}">${esc(STATUS_LABEL[t.status] || t.status)}</span>`}
      <span class="meta">${esc(ago(t.updated_at || t.created_at))}</span>`;
    btn.addEventListener('click', () => openTicket(t.id));
    list.appendChild(btn);
  }
}

// ---- detail view ----
async function openTicket(id) {
  currentTicket = id;
  show('detail-view');
  setError('reply-error', null);
  el('thread').innerHTML = '<p class="support">Loading…</p>';
  let data;
  try {
    data = await api('GET', `/tickets/${id}`);
  } catch (e) {
    el('thread').innerHTML = '';
    setError('reply-error', e.message);
    return;
  }
  const t = data.ticket || {};
  el('detail-eyebrow').textContent = (CATEGORY_LABEL[t.category] || 'TICKET').toUpperCase();
  el('detail-title').textContent = t.title || '';
  el('detail-support').textContent = `Opened ${ago(t.created_at)}${t.origin === 'agent' ? ' · sent by your Luna' : ''}`;
  const pill = el('detail-status');
  pill.textContent = STATUS_LABEL[t.status] || t.status || '';
  pill.className = `pill ${t.status || ''}`;
  const thread = el('thread');
  thread.innerHTML = '';
  for (const m of data.messages || []) {
    const div = document.createElement('div');
    const mine = m.author !== 'admin';
    div.className = `msg ${m.author === 'admin' ? 'admin' : 'mine'}`;
    div.innerHTML = `
      <div class="who">${esc(WHO_LABEL[m.author] || m.author)}</div>
      <div class="body">${esc(m.body)}</div>
      <div class="when">${esc(ago(m.created_at))}</div>`;
    thread.appendChild(div);
  }
}

// ---- new ticket view ----
let category = 'other';

el('category-chips').addEventListener('click', (e) => {
  const chip = e.target.closest('.chip');
  if (!chip) return;
  category = chip.dataset.cat;
  document.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === chip));
});

el('new-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  setError('new-error', null);
  const submit = el('new-submit');
  submit.disabled = true;
  try {
    await api('POST', '/tickets', {
      title: el('new-title').value.trim(),
      body: el('new-body').value.trim(),
      category,
    });
    el('new-title').value = '';
    el('new-body').value = '';
    show('list-view');
    loadList();
  } catch (err) {
    setError('new-error', err.message);
  } finally {
    submit.disabled = false;
  }
});

el('reply-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  setError('reply-error', null);
  const submit = el('reply-submit');
  submit.disabled = true;
  try {
    await api('POST', `/tickets/${currentTicket}/replies`, { body: el('reply-body').value.trim() });
    el('reply-body').value = '';
    openTicket(currentTicket);
  } catch (err) {
    setError('reply-error', err.message);
  } finally {
    submit.disabled = false;
  }
});

el('new-btn').addEventListener('click', () => { setError('new-error', null); show('new-view'); });
document.querySelectorAll('[data-back]').forEach((b) =>
  b.addEventListener('click', () => { show('list-view'); loadList(); }));

// ---- live refresh ----
try {
  const es = new EventSource('/api/events?topics=feedback.*');
  es.addEventListener('feedback.updated', () => {
    if (!el('list-view').classList.contains('hidden')) loadList();
    else if (currentTicket && !el('detail-view').classList.contains('hidden')) openTicket(currentTicket);
  });
} catch {}

// ---- boot ----
try { window.parent?.postMessage({ type: 'luna-ui-ready' }, '*'); } catch {}
getToken().then(loadList);
