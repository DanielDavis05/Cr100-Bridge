'use strict';
// CR100 Bridge settings page. Plain DOM, no build step: views render into <main>, and each
// view puts its actions on the CR100-style soft-key bar at the bottom.

const $ = (selector, root = document) => root.querySelector(selector);

// --- icons (drawn after the CR100's own glyphs) --------------------------------------------
const ICONS = {
  speaker: `<svg viewBox="0 0 40 40" aria-hidden="true"><defs><linearGradient id="sp" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#bfe7ff"/><stop offset="1" stop-color="#3f9be0"/></linearGradient></defs><path d="M6 15 22 7l12 6-16 9z" fill="#e9f7ff"/><path d="M6 15l12 7v11L6 26z" fill="url(#sp)"/><path d="M18 22l16-9v11l-16 9z" fill="#5fb2ea"/><circle cx="26" cy="22" r="3" fill="#2d79c4" opacity=".7"/></svg>`,
  controller: `<svg viewBox="0 0 40 40" aria-hidden="true"><rect x="11" y="3" width="18" height="34" rx="6" fill="#3a4250" stroke="#cfd6e0" stroke-width="1.5"/><rect x="14" y="7" width="12" height="11" rx="1.5" fill="#8fc0ee"/><circle cx="20" cy="27" r="4.2" fill="none" stroke="#cfd6e0" stroke-width="1.6"/></svg>`,
  note: `<svg viewBox="0 0 40 40" aria-hidden="true"><path d="M16 9l15-4v20" fill="none" stroke="#e9f7ff" stroke-width="3" stroke-linejoin="round"/><path d="M16 9v21" stroke="#e9f7ff" stroke-width="3"/><ellipse cx="11.5" cy="30" rx="5" ry="4" fill="#5fb2ea"/><ellipse cx="26.5" cy="25" rx="5" ry="4" fill="#5fb2ea"/></svg>`,
  house: `<svg viewBox="0 0 40 40" aria-hidden="true"><path d="M5 19 20 6l15 13" fill="none" stroke="#e9f7ff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><path d="M10 17v16h20V17" fill="#5fb2ea"/><rect x="17" y="24" width="6" height="9" fill="#e9f7ff"/></svg>`,
  key: `<svg viewBox="0 0 40 40" aria-hidden="true"><circle cx="13" cy="20" r="7" fill="none" stroke="#e9f7ff" stroke-width="3.2"/><path d="M20 20h15M30 20v6M35 20v5" stroke="#5fb2ea" stroke-width="3.2" stroke-linecap="round"/></svg>`,
  radio: `<svg viewBox="0 0 40 40" aria-hidden="true"><path d="M12 13 29 6" stroke="#e9f7ff" stroke-width="2.6" stroke-linecap="round"/><rect x="5" y="13" width="30" height="21" rx="4" fill="#5fb2ea"/><circle cx="14.5" cy="23.5" r="5.5" fill="none" stroke="#e9f7ff" stroke-width="2.6"/><rect x="23" y="18" width="8" height="3" rx="1.2" fill="#e9f7ff"/><rect x="23" y="24" width="8" height="3" rx="1.2" fill="#e9f7ff"/></svg>`,
  clock: `<svg viewBox="0 0 40 40" aria-hidden="true"><path d="M7 10l5-4.5M33 10l-5-4.5" stroke="#e9f7ff" stroke-width="2.8" stroke-linecap="round"/><circle cx="20" cy="22" r="13" fill="#5fb2ea"/><circle cx="20" cy="22" r="10" fill="none" stroke="#e9f7ff" stroke-width="1.6" opacity=".7"/><path d="M20 15v7l5 3" fill="none" stroke="#e9f7ff" stroke-width="2.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  tick: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  // Small line glyphs for the round mini buttons; they take the text colour.
  pencil: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 13l1-3.5 7-7 2.5 2.5-7 7z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>`,
  trash: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 8.5h5.8l.6-8.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  up: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 13V3.5M4 7.5 8 3.5l4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  down: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v9.5M4 8.5l4 4 4-4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  lock: `<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3.5" y="7" width="9" height="6.5" rx="1.5" fill="currentColor"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>`,
  unlock: `<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3.5" y="7" width="9" height="6.5" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5.5 7V5a2.5 2.5 0 0 1 4.8-1" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>`,
  plus: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3.5v9M3.5 8h9" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  pin: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 14s4.5-4.3 4.5-7.5a4.5 4.5 0 0 0-9 0C3.5 9.7 8 14 8 14z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="8" cy="6.5" r="1.6" fill="currentColor"/></svg>`,
  copy: `<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="7.5" height="7.5" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10.5 3.5V3H3v7.5h.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>`,
  folder: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2 4.5V12a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1V6a1 1 0 0 0-1-1H8L6.5 3.5H3a1 1 0 0 0-1 1z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>`,
};

// The orange level bar from the CR100's volume popup: every quantity on the page uses it.
const levelBar = (lit, total, label) => `<span class="level" role="img" aria-label="${escapeHtml(label)}">${
  Array.from({ length: total }, (_, i) => `<i class="${i < lit ? 'on' : ''}"></i>`).join('')}</span>`;
const ARROW = '<svg class="arrow" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9M8.5 4.5 12 8l-3.5 3.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const CHEVRON = '<svg class="chevron" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
// A round icon button; the label is for screen readers and the tooltip.
const mini = (icon, label, extra = '', attrs = '') =>
  `<button type="button" class="mini ${extra}" title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}" ${attrs}>${ICONS[icon]}</button>`;

// Edit a name in place: the text node becomes a field; Enter or leaving saves, Escape cancels.
// onDone receives the trimmed new value ('' when cleared), or null when cancelled or unchanged.
function editInPlace(target, { value, label, maxlength = 80, className = '' }, onDone) {
  const input = document.createElement('input');
  input.type = 'text';
  input.className = `rename-input ${className}`.trim();
  input.maxLength = maxlength;
  input.value = value;
  input.setAttribute('aria-label', label);
  target.replaceWith(input);
  input.focus(); input.select();
  let done = false;
  const finish = (keep) => {
    if (done) return;
    done = true;
    const next = input.value.trim();
    onDone(keep && next !== value ? next : null);
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); finish(true); }
    if (event.key === 'Escape') { event.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  return input;
}

// Status badges: the CR100's signal-bar glyph for controllers, a house for Home Assistant.
function bars(lit, colour = '#a6d84a') {
  const heights = [5, 8, 11, 14];
  return `<svg viewBox="0 0 30 15" aria-hidden="true">${heights.map((h, i) =>
    `<rect x="${3 + i * 7}" y="${15 - h}" width="5" height="${h}" rx="1" fill="${i < lit ? colour : 'rgba(255,255,255,.25)'}"/>`).join('')}</svg>`;
}
const houseBadge = (on) => `<svg viewBox="0 0 30 15" aria-hidden="true"><path d="M8 7.5 15 2l7 5.5V14H8z" fill="${on ? '#f3c24a' : 'rgba(255,255,255,.3)'}"/><rect x="13.3" y="9" width="3.4" height="5" fill="#545b69"/></svg>`;

// --- small helpers --------------------------------------------------------------------------
const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function el(html) {
  const template = document.createElement('template');
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}

// Save a value as a JSON file. The link must belong to this document (not an el() template,
// whose inert document has no window and ignores clicks), and the URL outlives the click.
function downloadJson(filename, value) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2) + '\n'], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function openModal(overlay, trigger, initialFocus, onClose) {
  const close = ({ restoreFocus = true } = {}) => {
    overlay.remove();
    document.removeEventListener('keydown', onKey, true);
    overlay.removeEventListener('click', onClick);
    onClose?.();
    if (restoreFocus && trigger.isConnected) trigger.focus();
  };
  const onKey = (event) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const controls = [...overlay.querySelectorAll('button, input, select, textarea, a[href], [tabindex]')]
        .filter((item) => !item.disabled && item.tabIndex >= 0 && !item.closest('[hidden]') && item.getClientRects().length);
      if (!controls.length) { event.preventDefault(); return; }
      const at = controls.indexOf(document.activeElement);
      if (event.shiftKey && at <= 0) { event.preventDefault(); controls[controls.length - 1].focus(); }
      else if (!event.shiftKey && (at === -1 || at === controls.length - 1)) { event.preventDefault(); controls[0].focus(); }
    }
  };
  const onClick = (event) => {
    if (event.target === overlay || event.target.closest('[data-close]')) close();
  };
  overlay.addEventListener('click', onClick);
  document.addEventListener('keydown', onKey, true);
  document.body.append(overlay);
  initialFocus?.focus();
  return { close, overlay };
}

function ago(seconds) {
  if (seconds == null) return 'never';
  if (seconds < 10) return 'just now';
  if (seconds < 90) return `${Math.round(seconds)} seconds ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} minutes ago`;
  return `${Math.round(seconds / 3600)} hours ago`;
}

function duration(seconds) {
  if (seconds == null) return '';
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  if (days) return `${days} day${days > 1 ? 's' : ''}, ${hours} hour${hours === 1 ? '' : 's'}`;
  if (hours) return `${hours} hour${hours > 1 ? 's' : ''}, ${minutes} min`;
  return `${minutes} min`;
}

const clock = (epoch) => new Date(epoch * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

class ApiError extends Error {}

async function api(method, path, body) {
  const options = { method, headers: { 'X-Bridge': '1' }, credentials: 'same-origin' };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError('Cannot reach the bridge. Check that the Pi is on and on your network.');
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith('/api/login') && !path.startsWith('/api/session')) {
    showGate(false);
    throw new ApiError('Sign in again to continue.');
  }
  if (!response.ok) throw new ApiError(data.error || `The bridge answered ${response.status}.`);
  return data;
}

function uploadControllerFirmware(mac, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', `/api/controllers/${mac}/firmware`);
    request.setRequestHeader('X-Bridge', '1');
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.setRequestHeader('X-Firmware-Name', encodeURIComponent(file.name));
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 100));
    });
    request.addEventListener('load', () => {
      let data;
      try { data = JSON.parse(request.responseText); } catch { data = {}; }
      if (request.status === 401) {
        showGate(false);
        reject(new ApiError('Sign in again to continue.'));
      } else if (request.status < 200 || request.status >= 300) {
        reject(new ApiError(data.error || `The bridge answered ${request.status}.`));
      } else resolve(data);
    });
    request.addEventListener('error', () => reject(new ApiError('Cannot reach the bridge. Upload the firmware again when it reconnects.')));
    request.addEventListener('timeout', () => reject(new ApiError('The upload timed out. Upload the firmware again.')));
    request.timeout = 120000;
    request.send(file);
  });
}

async function uploadStockBootstrap(mac, file, publicKey, onProgress) {
  let encodedKey;
  if (publicKey) {
    if (publicKey.size > 4096 || publicKey.size === 0) throw new ApiError('The public key must be 1–4096 bytes.');
    const bytes = new Uint8Array(await publicKey.arrayBuffer());
    let binary = '';
    for (const byte of bytes) binary += String.fromCharCode(byte);
    encodedKey = btoa(binary);
  }
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open('POST', `/api/controllers/${mac}/bootstrap`);
    request.setRequestHeader('X-Bridge', '1');
    request.setRequestHeader('Content-Type', 'application/octet-stream');
    request.setRequestHeader('X-Firmware-Name', encodeURIComponent(file.name));
    if (encodedKey) request.setRequestHeader('X-Public-Key', encodedKey);
    request.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 100));
    });
    request.addEventListener('load', () => {
      let data;
      try { data = JSON.parse(request.responseText); } catch { data = {}; }
      if (request.status === 401) {
        showGate(false);
        reject(new ApiError('Sign in again to continue.'));
      } else if (request.status < 200 || request.status >= 300) {
        reject(new ApiError(data.error || `The bridge answered ${request.status}.`));
      } else resolve(data);
    });
    request.addEventListener('error', () => reject(new ApiError('Cannot reach the bridge. Upload the firmware again when it reconnects.')));
    request.addEventListener('timeout', () => reject(new ApiError('The upload timed out. Upload the firmware again.')));
    request.timeout = 120000;
    request.send(file);
  });
}

let toastTimer;
function toast(title, text, bad = false) {
  const box = $('#toast');
  $('#toast-title').textContent = title;
  $('#toast-text').textContent = text || '';
  box.classList.toggle('bad', bad);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, bad ? 7000 : 3500);
}

// --- soft-key bar ---------------------------------------------------------------------------
// Three slots like the CR100: [left, middle, right]; each { label, run, disabled } or null.
let softkeyActions = [null, null, null];
function softkeys(keys) {
  softkeyActions = keys;
  document.querySelectorAll('#softkeys button').forEach((button, slot) => {
    const key = keys[slot];
    button.textContent = key ? key.label : '';
    button.disabled = !key || !!key.disabled;
    button.setAttribute('aria-hidden', key ? 'false' : 'true');
    button.tabIndex = key ? 0 : -1;
  });
}

document.querySelectorAll('#softkeys button').forEach((button, slot) => {
  button.addEventListener('click', async () => {
    const key = softkeyActions[slot];
    if (!key || key.disabled) return;
    button.disabled = true;
    try { await key.run(); } catch (error) { toast('Not done', error.message, true); }
    finally { if (softkeyActions[slot] === key) button.disabled = !!key.disabled; }
  });
});

// --- sign-in gate ---------------------------------------------------------------------------
let gateMode = 'login';
function showGate(setup) {
  gateMode = setup ? 'setup' : 'login';
  $('#app').hidden = true;
  $('#status').hidden = true;
  $('#gate').hidden = false;
  $('#title').textContent = 'Bridge';
  $('#gate-title').textContent = setup ? 'Set a password' : 'Sign in';
  $('#gate-text').textContent = setup
    ? 'This bridge has no password yet. Choose one; anyone on your network who opens this page will need it.'
    : 'Enter the bridge password.';
  $('#gate-confirm-row').hidden = !setup;
  $('#gate-confirm').required = setup;
  $('#gate-password').autocomplete = setup ? 'new-password' : 'current-password';
  $('#gate-error').textContent = '';
  softkeys([null, null, { label: setup ? 'Set password' : 'Sign in', run: submitGate }]);
  $('#gate-password').focus();
}

async function submitGate() {
  const password = $('#gate-password').value;
  if (gateMode === 'setup' && password !== $('#gate-confirm').value) {
    $('#gate-error').textContent = 'The two passwords are different.';
    return;
  }
  try {
    await api('POST', gateMode === 'setup' ? '/api/setup' : '/api/login', { password });
    $('#gate-password').value = $('#gate-confirm').value = '';
    $('#gate').hidden = true;
    start();
  } catch (error) {
    $('#gate-error').textContent = error.message;
  }
}
$('#gate-form').addEventListener('submit', (event) => { event.preventDefault(); submitGate(); });

// --- views ----------------------------------------------------------------------------------
const views = {};
let current = null;
let dirty = false;
let pollTimer = null;

function setDirty(value) { dirty = value; }
window.addEventListener('beforeunload', (event) => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });

async function show(name) {
  if (name === 'home-assistant') name = 'source';   // the page's earlier name
  if (!views[name]) name = 'speakers';
  if (dirty && current && current !== name && !confirm('Leave without saving your changes?')) {
    history.replaceState(null, '', '#' + current);
    return;
  }
  setDirty(false);
  clearInterval(pollTimer);
  current = name;
  const view = views[name];
  $('#title').textContent = view.title;
  document.title = `${view.title} – CR100 Bridge`;
  document.querySelectorAll('.menu a').forEach((a) => {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  const main = $('#view');
  main.innerHTML = '<p class="lede">Loading…</p>';
  softkeys([null, null, null]);
  try {
    await view.open(main);
  } catch (error) {
    if (!$('#gate').hidden) return;   // signed out: the sign-in screen owns the soft keys
    main.innerHTML = `<div class="popup"><h3>Could not load this page</h3><p>${escapeHtml(error.message)}</p></div>`;
    softkeys([{ label: 'Try again', run: () => show(name) }, null, null]);
  }
}

// Speakers ---------------------------------------------------------------------------------
views.speakers = {
  title: 'Speakers',
  async open(main) {
    let data = await api('GET', '/api/speakers');
    const rows = data.speakers.map((s) => ({ ...s }));
    let library = data.library || '';
    const saved = () => JSON.stringify({ rows: rows.filter((r) => r.shown).map((r) => [r.key, r.custom_name, r.custom_icon]).sort(), library });
    // Rooms without a chosen icon follow their name, as the bridge does (room_icons.guess).
    const guesses = data.icon_guesses.map(([pattern, icon]) => [new RegExp(pattern), icon]);
    const guessIcon = (name) => (guesses.find(([pattern]) => pattern.test(name.toLowerCase())) || [null, data.default_icon])[1];
    const labels = Object.fromEntries(data.icons.map((i) => [i.name, i.label]));
    const iconSrc = (name) => `/static/room-icons/${encodeURIComponent(name)}.png`;
    // The CR100 sorts rooms by name itself (MainUI compareZPObjsByTitle), so the list does too.
    const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' });
    let baseline = saved();

    const render = () => {
      const shown = rows.filter((r) => r.shown).sort(byName), hidden = rows.filter((r) => !r.shown).sort(byName);
      main.innerHTML = '';
      main.append(el(`<p class="lede">${data.source === 'sonos'
        ? 'Sonos speakers on this network appear here by themselves, sorted by name as the controller sorts them. Hidden speakers stay hidden when they return.'
        : 'The rooms the controller lists, sorted by name as it sorts them. Hiding one changes nothing on the speaker.'}</p>`));
      if (data.speaker_health && !data.speaker_health.healthy) {
        main.append(el(`<div class="popup" role="status"><h3>Controller speakers need attention</h3><p>${escapeHtml(data.speaker_health.message)}</p><p>Saved selection: ${escapeHtml(data.speaker_health.selected.join(', ') || 'None')}. Save your changes below to update it.</p></div>`));
      }
      if (!data.source_connected) {
        const problem = data.source === 'sonos' ? 'The Sonos speakers are not answering' : 'Home Assistant is not connected';
        main.append(el(`<div class="popup"><h3>${problem}</h3><p>The list may be out of date. Check the Speaker source page.</p></div>`));
      }
      main.append(el(`<h2 class="group-title">On the controller</h2>`));
      if (!shown.length) main.append(el(`<p class="empty">No speakers are shown. Switch one on below.</p>`));
      shown.forEach((row) => main.append(line(row)));
      main.append(el(`<h2 class="group-title">Not on the controller</h2>`));
      if (!hidden.length) main.append(el(`<p class="empty">Every speaker ${data.source === 'sonos' ? 'in your Sonos system' : 'Home Assistant knows'} is on the controller.</p>`));
      hidden.forEach((row) => main.append(line(row)));

      const sonos = shown.filter((r) => r.sonos);
      const panel = el(`<div class="popup"><label class="field flush"><span>Read Favourites, playlists and the library from</span><select id="library"></select>
        <span class="hint">They are the same on every Sonos speaker${data.source === 'sonos' ? '' : '; pick one so speakers that are not Sonos get them too'}.</span></label></div>`);
      const select = $('#library', panel);
      select.append(new Option('Each speaker itself', ''));
      sonos.forEach((r) => select.append(new Option(r.name, r.key)));
      select.value = sonos.some((r) => r.key === library) ? library : '';
      select.addEventListener('change', () => { library = select.value; changed(); });
      main.append(el('<h2 class="group-title">Music menu</h2>'), panel);
      changed();
    };

    const describe = (row) => {
      if (row.missing) return { main: `Not found in ${data.source === 'sonos' ? 'your Sonos system' : data.source_label}`, sub: row.sub };
      if (row.simulated) return { main: 'Simulated room', sub: 'Not connected to a speaker' };
      const state = { playing: 'Playing', paused: 'Paused', idle: 'Idle', off: 'Off', unavailable: 'Unavailable',
        on: 'On', standby: 'Standby', buffering: 'Buffering' }[row.state] || (row.state || 'Unknown');
      return { main: row.playing && row.state === 'playing' ? `${state}: ${row.playing}` : state, sub: row.sub };
    };

    const line = (row) => {
      const text = describe(row);
      const chips = [];
      if (row.platform && !row.sonos) chips.push(`<span class="chip warn" title="The queue and the Sonos library need a Sonos speaker">Not Sonos: no queue</span>`);
      if (row.state === 'unavailable' || row.missing) chips.push('<span class="chip warn">Offline</span>');
      const node = el(`<div class="line">
        <div class="tile${row.shown ? '' : ' off'}">${row.shown
          ? `<button type="button" class="room-icon" title="Change icon" aria-label="Icon for ${escapeHtml(row.name)}: ${escapeHtml(labels[row.icon] || row.icon)}. Change it"><img src="${iconSrc(row.icon)}" alt=""></button>`
          : `<span class="room-icon"><img src="${iconSrc(row.icon)}" alt=""></span>`}<span class="name">${escapeHtml(row.name)}</span></div>
        <div class="row">
          <div class="what"><div class="main">${escapeHtml(text.main)}${chips.join('')}</div><div class="sub">${escapeHtml(text.sub)}</div></div>
          <div class="tools"></div>
          <label class="check"><input type="checkbox" ${row.shown ? 'checked' : ''}><span class="box">${ICONS.tick}</span><span>Show</span></label>
        </div></div>`);
      const tools = $('.tools', node);
      $('button.room-icon', node)?.addEventListener('click', (event) => pickIcon(row, event.currentTarget));
      if (row.shown) {
        const rename = el(mini('pencil', `Rename ${row.name}`));
        rename.addEventListener('click', () => startRename(node, row));
        tools.append(rename);
      }
      $('input', node).addEventListener('change', (event) => {
        row.shown = event.target.checked;
        render();
        focusRow(row, '.check input');
      });
      node.dataset.key = row.key;
      return node;
    };

    const focusRow = (row, selector) => {
      const node = [...main.querySelectorAll('.line')].find((n) => n.dataset.key === row.key);
      if (node) $(selector, node)?.focus();
    };

    // The CR100's 21 room icons in a grey popup; the current one is a slate tile.
    const pickIcon = (row, trigger) => {
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="picker-title">
        <div class="popup"><h2 id="picker-title">Icon for ${escapeHtml(row.name)}</h2>
          <p>This is the picture next to the room on the controller.</p>
          <div class="icon-grid">${data.icons.map((i) => `<button type="button" class="icon-choice" data-icon="${i.name}"
            aria-pressed="${i.name === row.icon}"><span class="swatch"><img src="${iconSrc(i.name)}" alt=""></span><span>${escapeHtml(i.label)}</span></button>`).join('')}</div>
          <div class="picker-actions">
            ${row.custom_icon ? '<button type="button" class="inline-button quiet" data-icon="">Match the room name</button>' : ''}
            <button type="button" class="inline-button quiet" data-close>Cancel</button></div>
        </div></div>`);
      overlay.addEventListener('click', (event) => {
        const choice = event.target.closest('[data-icon]');
        if (!choice) return;
        const icon = choice.dataset.icon;
        row.custom_icon = icon && icon !== guessIcon(row.name) ? icon : '';
        row.icon = icon || guessIcon(row.name);
        modal.close({ restoreFocus: false });
        render();
        focusRow(row, 'button.room-icon');
      });
      const modal = openModal(overlay, trigger,
        overlay.querySelector('[aria-pressed="true"]') || overlay.querySelector('.icon-choice'));
    };

    const startRename = (node, row) => {
      editInPlace($('.tile .name', node), { value: row.name, label: `Room name for ${row.source_name || row.name}`, maxlength: 30 }, (name) => {
        if (name !== null) {
          row.custom_name = !name || name === row.source_name ? '' : name;
          row.name = row.custom_name || row.source_name || row.name;
          if (!row.custom_icon) row.icon = guessIcon(row.name);
        }
        render();
        focusRow(row, '[aria-label^="Rename"]');
      });
    };

    const changed = () => {
      const isDirty = saved() !== baseline;
      setDirty(isDirty);
      softkeys([
        { label: 'Undo changes', disabled: !isDirty, run: async () => { setDirty(false); await show('speakers'); } },
        { label: 'Refresh', run: async () => { if (!dirty || confirm('Discard your changes and reload the list?')) { setDirty(false); await show('speakers'); } } },
        { label: 'Save changes', disabled: !isDirty, run: save },
      ]);
    };

    const save = async () => {
      const shown = rows.filter((r) => r.shown).sort(byName);
      const result = await api('PUT', '/api/speakers', {
        rooms: shown.map((r) => ({ key: r.key, name: r.custom_name, icon: r.custom_icon })), library: library || null });
      baseline = saved();
      changed();
      toast('Saved', result.message);
    };

    render();
    let refreshing = false;
    pollTimer = setInterval(async () => {
      if (refreshing || document.hidden || current !== 'speakers' || dirty || main.contains(document.activeElement)) return;
      refreshing = true;
      try {
        const fresh = await api('GET', '/api/speakers');
        if (current !== 'speakers' || dirty || main.contains(document.activeElement) || JSON.stringify(fresh) === JSON.stringify(data)) return;
        data = fresh;
        rows.splice(0, rows.length, ...data.speakers.map((speaker) => ({ ...speaker })));
        library = data.library || '';
        baseline = saved();
        render();
      } catch { /* retain the current list while the bridge changes networks */ }
      finally { refreshing = false; }
    }, 10000);
  },
};

// Controllers ------------------------------------------------------------------------------
const EVENT_TEXT = {
  associated: 'Connected to the bridge',
  resumed: 'Reconnected after the bridge restarted',
  'cr100-searching': 'Stopped hearing the bridge; the bridge is calling it back',
  'link-heard': 'Heard from it again',
  'link-silent': 'Quiet for 30 seconds (normal while idle)',
  'radio-lost': 'The bridge radio stopped; restarting it',
  'radio-reopened': 'The bridge radio is back',
  disassoc: 'Disconnected',
  deauth: 'Disconnected',
  'controller-added': 'Paired with the bridge',
  'controller-rekeyed': 'Paired again with a new key',
  'controller-key-confirmed': 'Using its new key',
  'controller-previous-key': 'Still using its old key; pair it again if this persists',
};
// The colour each link event takes on the timeline: the same dots as the status line.
const EVENT_TONE = {
  associated: 'good', resumed: 'good', 'link-heard': 'good', 'radio-reopened': 'good',
  'link-silent': 'idle', 'cr100-searching': 'wait', 'radio-lost': 'alert', disassoc: 'bad', deauth: 'bad',
  'controller-added': 'good', 'controller-rekeyed': 'good',
  'controller-key-confirmed': 'good', 'controller-previous-key': 'wait',
};

const shortDuration = (seconds) => {
  if (seconds == null) return 'Not known yet';
  const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600), minutes = Math.floor(seconds % 3600 / 60);
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  return `${minutes} min`;
};
const shortClock = (epoch) => new Date(epoch * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

views.controllers = {
  title: 'Controllers',
  async open(main) {
    const firmwarePanels = new Map();
    const bootstrapPanels = new Map();
    const openLogs = new Set();
    let renaming = false;
    let importing = false;
    let remoteOpen = false;

    const updateDevicesStatus = (data) => {
      const node = $('.smart-device-status', main);
      if (!node) return;
      const status = data.smart_devices;
      node.hidden = !status;
      if (!status) { node.replaceChildren(); return; }
      const live = status.connected && status.enabled !== false;
      const meta = [`${status.areas || 0} areas`, `${status.devices || 0} devices`];
      if (status.enabled === false) meta.push('Turned off');
      else if (!status.connected) meta.push('Not connected');
      node.innerHTML = `<div class="strip-head"><span class="dot ${live ? 'good' : 'bad'}"></span>
          <span class="strip-title">Home Assistant devices</span><span class="meta">${escapeHtml(meta.join(' · '))}</span></div>
        ${(status.rooms || []).length ? `<ul class="area-map" aria-label="Room to area">${status.rooms.map((room) =>
          `<li><span>${escapeHtml(room.room)}</span>${ARROW}<span class="${room.area ? '' : 'muted'}">${escapeHtml(room.area || 'No area')}</span></li>`).join('')}</ul>` : ''}`;
    };

    const updateBridgeStatus = (data) => {
      const node = $('.bridge-update-status', main);
      if (!node) return;
      const state = data.updates?.bridge || {};
      $('.bridge-update-message', node).textContent = state.message || 'Open System Settings → Online Updates on a controller to check GitHub and update the bridge.';
      const meta = state.status
        ? [state.status, state.phase, Number.isFinite(state.percent) ? `${state.percent}%` : ''].filter(Boolean).join(' · ')
        : 'A staged controller update comes first';
      $('.meta', node).textContent = meta.charAt(0).toUpperCase() + meta.slice(1);
      const error = $('.updates-refresh-error', node);
      error.textContent = data.updates_error || state.last_error || '';
    };

    const render = (data) => {
      main.innerHTML = '';
      main.append(el(`<p class="lede">CR100s paired with this bridge. A controller sleeps while idle; that is normal.</p>`));
      main.append(el('<section class="panel smart-device-status" hidden></section>'));
      updateDevicesStatus(data);
      if (!data.radio_running) {
        main.append(el(`<div class="popup"><h3>The bridge radio is not running</h3><p>Controllers cannot connect until the bridge is started on the Pi.</p></div>`));
      }
      if (data.speaker_health && !data.speaker_health.healthy) {
        main.append(el(`<div class="popup" role="status"><h3>Controller speakers need attention</h3><p>${escapeHtml(data.speaker_health.message)}</p><p>A working radio connection does not mean the controller’s speakers are ready.</p></div>`));
      }
      const pairingRunning = Boolean(data.pairing?.running);
      const last = data.controllers.length === 1;
      data.controllers.forEach((c) => main.append(card(c, { pairingRunning, last })));
      if (!data.controllers.length) main.append(el(`<p class="empty">No controller is paired yet. Pair one below.</p>`));

      main.append(el(`<h2 class="group-title">Bridge updates</h2>`), el(`<section class="panel bridge-update-status" role="status">
        <div class="strip-head"><span class="dot idle"></span><span class="bridge-update-message"></span><span class="meta"></span></div>
        <p class="hint">Updates the bridge software on the Pi. Controller firmware stays unchanged.</p>
        <p class="updates-refresh-error error"></p></section>`));
      updateBridgeStatus(data);

      const pairing = data.pairing || {};
      const pairState = pairing.running ? ['wait', 'Waiting for a CR100. Put it in add-to-household setup mode within 3 minutes.']
        : pairing.failed ? ['alert', 'Pairing failed. Check the Pi’s service logs, then try again.']
        : ['idle', data.channel === 1 || !data.controllers.length
          ? 'Adds a CR100 to this household. Paired controllers keep their settings and stay connected.'
          : `Adds a CR100 to this household. Paired controllers stay connected; while pairing, the bridge also listens on channel 1, so they respond a little more slowly.`];
      const channelChoices = [1, 6, 11].map((value) => `<option value="${value}" ${data.channel === value ? 'selected' : ''}>Channel ${value}${value === 1 ? ' (pairing channel)' : ''}</option>`).join('');
      main.append(el(`<h2 class="group-title">Pairing</h2>`), el(`<section class="popup pairing">
        <dl class="meta-grid"><div><dt>Household</dt><dd>${escapeHtml(data.household || 'Not paired')}</dd></div>
          <div><dt>Radio channel</dt><dd>${escapeHtml(data.channel ?? 'Unknown')}</dd></div></dl>
        ${data.household ? `<div class="pair-row"><p class="hint">Channel 1 is where a CR100 pairs, so pairing on it needs no channel switching. Pick another channel if channel 1 is crowded.</p>
          <div class="radio-actions"><select id="radio-channel" aria-label="Radio channel" ${!pairing.available || pairing.running ? 'disabled' : ''}>${channelChoices}</select>
            <button type="button" class="inline-button quiet" id="radio-channel-save" disabled>Change channel</button></div></div>` : ''}
        <div class="pair-row"><span class="dot ${pairState[0]}"></span><p>${pairState[1]}</p>
          <button type="button" class="inline-button" id="pair-controller" ${!pairing.available || pairing.running ? 'disabled' : ''}>${pairing.running ? 'Pairing…' : data.controllers.length ? 'Add controller' : 'Pair controller'}</button></div>
        <div class="pair-row pair-transfer"><p class="hint">Use the same pairing on another bridge in a different location.</p>
          <div class="radio-actions">
            <button type="button" class="inline-button quiet" id="pairing-export" ${!data.controllers.length || pairing.running ? 'disabled' : ''}>Export pairing</button>
            <span class="file-pick"><label><input type="file" id="pairing-import" accept="application/json,.json" ${pairing.running ? 'disabled' : ''}><span class="inline-button quiet">Import pairing…</span></label></span>
          </div></div>
      </section>`));
      $('#pair-controller', main).addEventListener('click', async (event) => {
        event.currentTarget.disabled = true;
        try {
          await api('POST', '/api/controllers/pair', {});
          toast('Pairing started', 'Put the CR100 in its add-to-household setup mode.');
        } catch (error) {
          toast('Pairing could not start', error.message, true);
        }
        await refresh();
      });
      const channelSelect = $('#radio-channel', main), channelSave = $('#radio-channel-save', main);
      channelSelect?.addEventListener('change', () => { channelSave.disabled = Number(channelSelect.value) === data.channel; });
      channelSave?.addEventListener('click', async () => {
        const channel = Number(channelSelect.value);
        if (!confirm(`Move the bridge to channel ${channel}? The radio restarts, and each controller reconnects on the new channel. A sleeping controller reconnects when you wake it.`)) return;
        channelSave.disabled = true;
        try {
          const result = await api('PUT', '/api/controllers/channel', { channel });
          toast(result.changed ? 'Channel changed' : 'Channel unchanged', result.message);
        } catch (error) {
          toast('Channel not changed', error.message, true);
        }
        await refresh();
      });
      $('#pairing-export', main).addEventListener('click', async () => {
        try {
          downloadJson('cr100-pairing.json', await api('POST', '/api/controllers/pairing/export', {}));
        } catch (error) {
          toast('Export failed', error.message, true);
        }
      });
      $('#pairing-import', main).addEventListener('change', async (event) => {
        const input = event.currentTarget;
        const file = input.files[0];
        input.value = '';
        if (!file) return;
        let parsed;
        try {
          parsed = JSON.parse(await file.text());
        } catch {
          toast('Import failed', 'Choose a valid JSON pairing file.', true);
          return;
        }
        const incoming = parsed && typeof parsed === 'object' && typeof parsed.household === 'string' ? parsed.household : null;
        const message = !data.household
          ? `Import pairing${incoming ? ` for ${incoming}` : ''} onto this bridge?`
          : incoming && incoming === data.household
            ? `Import pairing for ${incoming}? Its controllers are merged into this household.`
            : `Replace this bridge’s pairing (${data.household}) with ${incoming || 'the imported household'}? Your speakers and room settings stay untouched.`;
        if (!confirm(message)) return;
        importing = true;
        try {
          const result = await api('POST', '/api/controllers/pairing/import', { data: parsed });
          toast('Pairing imported', result.message);
          await refresh();
        } catch (error) {
          toast('Import failed', error.message, true);
        } finally {
          importing = false;
        }
      });
    };

    // A strip of the controller's recent link events, oldest on the left and now on the right.
    const linkStrip = (c) => {
      const events = (c.events || []).filter((e) => Number.isFinite(e.time));
      if (!events.length) return null;
      const now = Date.now() / 1000;
      const start = Math.min(...events.map((e) => e.time));
      const span = Math.max(60, now - start);
      const latest = events[events.length - 1];
      const text = (e) => EVENT_TEXT[e.event] || e.event;
      const node = el(`<figure class="link-strip">
        <figcaption><span class="strip-title">Link</span><span class="meta">Latest ${escapeHtml(shortClock(latest.time))}: ${escapeHtml(text(latest))}</span></figcaption>
        <div class="track" role="img" aria-label="${events.length} link events since ${escapeHtml(shortClock(start))}. Latest: ${escapeHtml(text(latest))}"></div>
        <div class="ticks" aria-hidden="true"><span>${escapeHtml(shortClock(start))}</span><span>Now</span></div>
        <details class="event-log"${openLogs.has(c.mac) ? ' open' : ''}><summary>${CHEVRON}All ${events.length} events</summary>
          <ul class="timeline">${events.slice().reverse().map((e) =>
            `<li><span class="dot ${EVENT_TONE[e.event] || 'bad'}"></span><time>${clock(e.time)}</time><span>${escapeHtml(text(e))}</span></li>`).join('')}</ul></details>
      </figure>`);
      const track = el('<span class="rail"></span>');
      $('.track', node).append(track);
      for (const e of events) {
        const mark = el(`<span class="mark ${EVENT_TONE[e.event] || 'bad'}" title="${escapeHtml(`${clock(e.time)} · ${text(e)}`)}"></span>`);
        mark.style.left = `${Math.min(100, Math.max(0, (e.time - start) / span * 100))}%`;
        track.append(mark);
      }
      $('.event-log', node).addEventListener('toggle', (event) => {
        if (event.currentTarget.open) openLogs.add(c.mac); else openLogs.delete(c.mac);
      });
      return node;
    };

    const card = (c, { pairingRunning = false, last = false } = {}) => {
      const dot = { connected: 'good', dozing: 'idle', searching: 'wait' }[c.status] || 'bad';
      const awake = ['connected', 'dozing'].includes(c.status);
      const signal = c.signal != null && awake
        ? levelBar(c.signal, 4, `Signal ${c.signal} of 4`)
        : '<span class="value muted">Asleep</span>';
      const node = el(`<section class="controller">
        <div class="line">
          <div class="tile">${ICONS.controller}<span class="name">${escapeHtml(c.name)}</span></div>
          <div class="row"><div class="what"><div class="main"><span class="dot ${dot}"></span>${escapeHtml(c.status_label)}</div>
            <div class="sub">Heard ${escapeHtml(ago(c.last_heard_s))}</div></div>
            <div class="tools"><button type="button" class="mini rename-controller" title="Rename" aria-label="Rename ${escapeHtml(c.name)}">${ICONS.pencil}</button></div></div>
        </div>
        <div class="popup controller-body">
          <div class="vitals">
            <div class="vital"><span class="label">Signal</span>${signal}</div>
            <div class="vital"><span class="label">Drops, last hour</span><span class="value">${escapeHtml(c.drops_last_hour ?? '–')}</span></div>
            <div class="vital"><span class="label">Running for</span><span class="value">${escapeHtml(c.uptime_s != null ? shortDuration(c.uptime_s) : '–')}</span></div>
          </div>
          <dl class="meta-grid">
            <div><dt>Address</dt><dd>${escapeHtml(c.ip || 'Not known yet')}</dd></div>
            <div><dt>Wi‑Fi address</dt><dd>${escapeHtml(c.mac)}</dd></div>
            <div><dt>Details from</dt><dd>${c.details_at ? escapeHtml(clock(c.details_at)) : 'Once connected'}</dd></div>
          </dl>
          <div class="settings-list">
            <div class="setting"><span class="what"><span class="main">Remote control</span>
              <span class="sub">${c.remote_ready ? 'Controller is responding' : 'Controller is offline or needs RE100 R20'}</span></span>
              <button type="button" class="inline-button quiet remote-controller">Open</button></div>
            <div class="setting"><span class="what"><span class="main">Components</span>
              <span class="sub">${c.visible_components == null ? 'Uses the household selection' : `${c.visible_components.length} chosen for this controller`}</span></span>
              <button type="button" class="inline-button quiet choose-components">Choose</button></div>
            <div class="setting"><span class="what"><span class="main">Forget this controller</span>
              <span class="sub">Removes it and its settings from the bridge. Pair it again to bring it back.</span></span>
              <button type="button" class="inline-button quiet forget-controller" ${pairingRunning ? 'disabled' : ''}>Forget</button></div>
          </div>
        </div></section>`);
      node.dataset.mac = c.mac;
      const strip = linkStrip(c);
      if (strip) $('.vitals', node).after(strip);

      $('.rename-controller', node).addEventListener('click', () => startRename(node, c));
      $('.remote-controller', node).addEventListener('click', async (event) => {
        const trigger = event.currentTarget;
        trigger.disabled = true;
        try { openRemote(await api('GET', `/api/controllers/${c.mac}/remote`), trigger); }
        catch (error) { toast('Remote control unavailable', error.message, true); }
        finally { trigger.disabled = false; }
      });
      $('.choose-components', node).addEventListener('click', async (event) => {
        const trigger = event.currentTarget;
        trigger.disabled = true;
        try { chooseComponents(await api('GET', `/api/controllers/${c.mac}/components`), trigger); }
        catch (error) { toast('Components unavailable', error.message, true); }
        finally { trigger.disabled = false; }
      });
      $('.forget-controller', node).addEventListener('click', async (event) => {
        const effect = last
          ? 'This is the last controller: the bridge goes back to having none paired, as when it was installed.'
          : 'The other controllers are not affected.';
        if (!confirm(`Forget ${c.name}? The bridge stops answering it within a few seconds and removes its settings. ${effect} To use it again, pair it again.`)) return;
        event.currentTarget.disabled = true;
        try {
          const result = await api('POST', `/api/controllers/${c.mac}/forget`, {});
          firmwarePanels.delete(c.mac);
          bootstrapPanels.delete(c.mac);
          toast('Controller forgotten', result.message);
        } catch (error) {
          toast('Not forgotten', error.message, true);
        }
        await refresh();
      });
      // One Firmware row: upload an update, or start the guided custom install on a stock CR100.
      if (!bootstrapPanels.has(c.mac)) bootstrapPanels.set(c.mac, bootstrapPanel(c));
      const bootstrap = bootstrapPanels.get(c.mac);
      if (!firmwarePanels.has(c.mac)) firmwarePanels.set(c.mac, firmwarePanel(c, bootstrap));
      const firmware = firmwarePanels.get(c.mac);
      $('.forget-controller', node).closest('.setting').before(firmware.node);
      bootstrap.update(c.bootstrap, c.bootstrap_error, c.firmware_identity, c.default_cfw);
      firmware.update(c.update, c.firmware, c.firmware_identity);
      return node;
    };

    // Rename in place, as the Speakers page does, instead of a browser prompt.
    const startRename = (node, c) => {
      renaming = true;
      editInPlace($('.tile .name', node), { value: c.name, label: `Name for ${c.name}`, maxlength: 40 }, async (value) => {
        if (value) {
          try { await api('PUT', `/api/controllers/${c.mac}`, { name: value }); toast('Renamed', `This controller is now ${value}.`); }
          catch (error) { toast('Not renamed', error.message, true); }
        }
        renaming = false;
        await refresh();
        main.querySelector(`.controller[data-mac="${c.mac}"] .rename-controller`)?.focus();
      });
    };

    // Choosing a .upd file uploads it and offers it to the controller in one go; the CR100
    // asks for confirmation itself in Online Updates. Unsigned firmware asks here first.
    const FIRMWARE_ACTIVE = ['armed', 'downloading', 'installing', 'started'];
    const firmwarePanel = (controller, bootstrap) => {
      const node = el(`<div class="firmware-entry">
        <div class="setting"><span class="what"><span class="main">Firmware</span><span class="sub firmware-summary"></span></span>
          <span class="firmware-tools">
            <button type="button" class="inline-button quiet firmware-bridge-package" hidden>Use firmware on bridge</button>
            <span class="file-pick"><label><input type="file" class="firmware-file" accept=".upd,application/octet-stream" aria-label="Upload a controller firmware .upd file"><span class="inline-button quiet">Upload .upd</span></label></span>
          </span></div>
        <div class="firmware-state" hidden>
          <div class="firmware-progress" hidden><span class="level" role="progressbar" aria-label="Firmware upload progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">${'<i></i>'.repeat(10)}</span></div>
          <p class="firmware-status" role="status" aria-live="polite"></p>
          <p class="firmware-error error" role="alert"></p>
          <div class="firmware-actions"><button type="button" class="inline-button firmware-offer" hidden>Offer to controller</button>
            <button type="button" class="inline-button quiet firmware-cancel" hidden>Cancel update</button></div>
        </div></div>`);
      $('.firmware-tools', node).append(bootstrap.node);
      const input = $('.firmware-file', node), bridgePackage = $('.firmware-bridge-package', node);
      const offerButton = $('.firmware-offer', node), cancel = $('.firmware-cancel', node);
      const progress = $('.firmware-progress', node), status = $('.firmware-status', node), error = $('.firmware-error', node);
      const setProgress = (percent) => {
        const bar = $('.level', progress);
        bar.setAttribute('aria-valuenow', String(percent));
        bar.querySelectorAll('i').forEach((segment, i) => segment.classList.toggle('on', i < Math.round(percent / 10)));
      };
      let firmware = {}, currentVersion = controller.firmware, currentIdentity = controller.firmware_identity;
      let busy = false, notice = '', failure = '';
      const endpoint = `/api/controllers/${controller.mac}/firmware`;
      const kindOf = (f) => ['official', 'custom', 'unverified'].includes(f.firmware_kind) ? f.firmware_kind :
        f.signature_valid === true ? 'official' : f.cfw_build_id ? 'custom' : f.signature_valid === false ? 'unverified' : null;
      const labelOf = (f) => {
        const kind = kindOf(f);
        if (kind === 'custom') return `${f.cfw_release || 'Custom firmware'} (${f.version})`;
        if (kind === 'official') return `Sonos ${f.version}`;
        if (kind === 'unverified') return `Unsigned firmware ${f.version}`;
        return f.version || 'The firmware';
      };
      const update = function (fresh, current, identity) {
        if (fresh) firmware = fresh;
        if (arguments.length > 1) currentVersion = current || '';
        if (arguments.length > 2) currentIdentity = identity;
        const state = firmware.status || 'none';
        const label = labelOf(firmware);
        const active = FIRMWARE_ACTIVE.includes(state);
        const inProgress = active && state !== 'armed';

        // What the CR100 runs now, then whatever is under way.
        const installed = currentIdentity?.classification === 'custom' ? currentIdentity.release || 'Custom firmware'
          : currentIdentity?.classification === 'stock' ? 'Stock assumed' : '';
        const underWay = { armed: 'Update offered', downloading: 'Downloading update', installing: 'Installing update',
          started: 'Installing update', staged: 'Update uploaded', ready: 'Update uploaded' }[state] || bootstrap.summary();
        $('.firmware-summary', node).textContent =
          [currentVersion || 'Version not known yet', installed, underWay].filter(Boolean).join(' · ');

        const normalized = { '4.3': '24.1-75101', '4.3.2': '25.2-50130' }[currentVersion] || currentVersion;
        const sameVersion = Boolean(firmware.version) && normalized === firmware.version;
        const message = {
          staged: `${label} is uploaded but not offered to the controller yet.`,
          ready: `${label} is uploaded but not offered to the controller yet.`,
          armed: `${label} is ready. On the CR100, open System Settings → Online Updates to install it.${sameVersion
            ? ' The controller already runs this version, so its updater may skip it.' : ''}`,
          downloading: `Installing ${label}. Keep the controller docked and follow its screen.`,
          installing: `Installing ${label}. Keep the controller docked and follow its screen.`,
          started: `Installing ${label}. Keep the controller docked and follow its screen.`,
          completed: `Updated to ${label}.`, complete: `Updated to ${label}.`,
          failed: 'The update did not finish. Upload the firmware again to retry.',
          expired: `The offer for ${label} expired. Upload it again to retry.`,
        }[state] || '';
        if (!busy) status.textContent = notice || message;
        error.textContent = failure || (state === 'failed' ? firmware.last_error || '' : '');

        input.disabled = busy || inProgress;
        bridgePackage.hidden = !controller.bridge_firmware_available;
        bridgePackage.disabled = busy || inProgress;
        offerButton.hidden = !['staged', 'ready'].includes(state);
        offerButton.disabled = busy;
        cancel.hidden = !['staged', 'ready', 'armed'].includes(state);
        cancel.disabled = busy;
        cancel.textContent = state === 'armed' ? 'Cancel update' : 'Discard';
        bootstrap.node.hidden = bootstrap.hidden() || (active && bootstrap.idle());
        $('.firmware-state', node).hidden = !(busy || status.textContent || error.textContent);
      };
      const perform = async (message, operation) => {
        busy = true; notice = ''; failure = ''; status.textContent = message; update();
        try {
          const result = await operation();
          firmware = result.firmware;
          currentVersion = result.current_version || '';
          currentIdentity = result.firmware_identity;
          return true;
        } catch (problem) {
          failure = problem.message;
          return false;
        } finally {
          busy = false; progress.hidden = true; update();
        }
      };
      // Offer what was just uploaded. Unsigned firmware, or custom firmware for a CR100 that
      // still looks stock, asks first; declining discards the upload.
      const offer = async () => {
        const reasons = [];
        if (firmware.signature_valid === false) reasons.push(firmware.signature_warning ||
          'The bridge cannot verify this file as Sonos firmware or a known custom release.');
        if (kindOf(firmware) === 'custom' && currentIdentity?.classification === 'stock') reasons.push(
          'This CR100 looks like stock firmware, which only accepts Sonos-signed updates. For a first custom install, use Install custom firmware instead.');
        if (reasons.length && !confirm(`${reasons.join('\n\n')}\n\nOffer ${labelOf(firmware)} to ${controller.name} anyway?`)) {
          if (await perform('Discarding the upload…', () => api('POST', endpoint + '/cancel', {}))) {
            notice = 'Not offered. The upload was discarded.'; update();
          }
          return;
        }
        await perform('Offering the update to the controller…', () => api('POST', endpoint + '/install',
          { force: false, sha256: firmware.sha256, allow_unverified: firmware.signature_valid === false }));
      };
      input.addEventListener('change', async () => {
        const file = input.files[0];
        input.value = '';
        if (!file || busy) return;
        if (file.size > 16 * 1024 * 1024) { notice = ''; failure = 'Firmware files must be at most 16 MiB.'; update(); return; }
        progress.hidden = false; setProgress(0);
        const uploaded = await perform('Uploading firmware…', () => uploadControllerFirmware(controller.mac, file, (percent) => {
          setProgress(percent);
          status.textContent = percent === 100 ? 'Checking the firmware signature and version…' : `Uploading firmware… ${percent}%`;
        }));
        if (uploaded) await offer();
      });
      bridgePackage.addEventListener('click', async () => {
        if (await perform('Checking the firmware on the bridge…', () => api('POST', endpoint + '/bridge-package', {}))) await offer();
      });
      offerButton.addEventListener('click', () => offer());
      cancel.addEventListener('click', () => perform('Cancelling…', () => api('POST', endpoint + '/cancel', {})));
      return { node, update, isBusy: () => busy };
    };

    const BOOTSTRAP_LABELS = {
      none: 'No custom firmware staged', staged: 'Package uploaded', preparing: 'Preparing the controller',
      await_now_playing: 'Waiting for Now Playing confirmation', triggering: 'Processing album art',
      root_ready: 'Controller access established', preflight: 'Checking the flash destination',
      await_flash_confirmation: 'Ready for flash confirmation', installing: 'Installing custom firmware',
      verifying: 'Checking the installation', completed: 'Installation complete',
      failed: 'Installation stopped', cancelled: 'Installation cancelled', uncertain: 'Installation outcome uncertain',
    };

    const IDLE_BOOTSTRAP = ['none', 'staged', 'failed', 'cancelled', 'completed'];
    const bootstrapPanel = (controller) => {
      const node = el(`<button type="button" class="inline-button install-open" aria-haspopup="dialog">Install custom firmware</button>`);
      const overlay = el(`<div class="picker install-overlay" role="dialog" aria-modal="true" aria-labelledby="install-title" aria-describedby="install-intro">
        <section class="popup install-dialog">
          <header class="install-header"><div><p class="install-eyebrow">CR100 setup</p><h2 id="install-title">Install custom firmware</h2>
            <p id="install-intro">For a stock CR100 on firmware 4.3.2.</p></div>
            <button type="button" class="install-close" aria-label="Close installation guide" data-close>×</button></header>
          <div class="install-meter"><span class="install-step-count"></span><ol aria-label="Installation steps">
            <li>Package</li><li>Controller</li><li>Install</li></ol></div>
          <div class="install-content"><div class="install-copy"><h3 class="install-stage-title" tabindex="-1"></h3>
            <p class="install-instruction"></p>
            <div class="install-default-package" hidden><span>Included with the bridge</span><strong class="install-default-name"></strong><span class="install-default-size"></span></div>
            <div class="install-file-section" hidden><div class="bootstrap-pick file-pick"><label><input type="file" class="bootstrap-package" accept=".upd,application/octet-stream"><span class="inline-button quiet">Choose .upd package</span></label><span class="bootstrap-package-name file-name">No package chosen</span></div>
              <details class="install-key-options"><summary>Use a different public key</summary><div class="bootstrap-pick file-pick"><label><input type="file" class="bootstrap-key" accept=".pub,application/octet-stream"><span class="inline-button quiet">Choose .pub key</span></label><span class="bootstrap-key-name file-name">Bundled development key</span></div></details>
              <div class="bootstrap-progress" hidden><span class="level" role="progressbar" aria-label="Custom firmware upload progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">${'<i></i>'.repeat(10)}</span></div></div>
            <button type="button" class="install-change-package" hidden>Use another .upd package</button>
            <div class="bootstrap-identity" hidden><dl class="meta-grid"><div><dt>Package</dt><dd class="bootstrap-filename"></dd></div><div class="bootstrap-version-row" hidden><dt>Firmware version</dt><dd class="bootstrap-version"></dd></div><div><dt>SHA-256</dt><dd class="bootstrap-sha hash"></dd></div><div class="bootstrap-key-row" hidden><dt>Public key SHA-256</dt><dd class="bootstrap-key-sha hash"></dd></div></dl></div>
            <div class="bootstrap-destination" hidden><p>Flash destination</p><dl class="meta-grid"><div><dt>Controller</dt><dd class="bootstrap-controller"></dd></div><div><dt>Firmware bank</dt><dd class="bootstrap-bank"></dd></div><div><dt>Generation</dt><dd class="bootstrap-generation"></dd></div></dl></div>
            <label class="check install-review" hidden><input type="checkbox" class="bootstrap-reviewed"><span class="box">${ICONS.tick}</span><span>I checked this package and flash destination.</span></label>
            <p class="bootstrap-error error" role="alert"></p></div>
            <figure class="install-figure"><div class="install-device"><div class="install-screen"><img class="install-image" alt=""><span class="install-image-fallback" hidden>Controller screen example unavailable</span></div><div class="install-device-controls" aria-hidden="true"><i></i><i></i><i></i></div></div><figcaption class="install-caption"></figcaption></figure></div>
          <footer class="install-footer"><p class="bootstrap-status" role="status" aria-live="polite"></p>
            <div class="install-actions"><button type="button" class="inline-button quiet bootstrap-cancel" hidden>Cancel installation</button>
              <button type="button" class="inline-button bootstrap-default" hidden>Use included firmware</button>
              <button type="button" class="inline-button bootstrap-upload" hidden>Upload package</button>
              <button type="button" class="inline-button bootstrap-start" hidden>Prepare controller</button>
              <button type="button" class="inline-button bootstrap-screen" hidden>I am on Now Playing</button>
              <button type="button" class="inline-button bootstrap-flash" hidden>Confirm flash</button>
              <button type="button" class="inline-button install-done" hidden>Done</button></div></footer>
        </section></div>`);
      const packageInput = $('.bootstrap-package', overlay), keyInput = $('.bootstrap-key', overlay);
      const stageDefault = $('.bootstrap-default', overlay), upload = $('.bootstrap-upload', overlay);
      const start = $('.bootstrap-start', overlay);
      const screen = $('.bootstrap-screen', overlay), flash = $('.bootstrap-flash', overlay);
      const cancel = $('.bootstrap-cancel', overlay), done = $('.install-done', overlay);
      const progress = $('.bootstrap-progress', overlay), status = $('.bootstrap-status', overlay);
      const error = $('.bootstrap-error', overlay), reviewed = $('.bootstrap-reviewed', overlay);
      const changePackage = $('.install-change-package', overlay), fileSection = $('.install-file-section', overlay);
      const defaultCard = $('.install-default-package', overlay);
      const image = $('.install-image', overlay), imageFallback = $('.install-image-fallback', overlay);
      const endpoint = `/api/controllers/${controller.mac}/bootstrap`;
      let snapshot = { status: 'none' }, identity, defaultCandidate = { available: false };
      let busy = false, notice = '', replacing = false, summary = '', customAlready = false;
      let screenConfirmed = false, flashConfirmed = false, reviewedDestination = '', shownImage = '';
      let modal;

      const stageView = (state) => {
        if (identity?.classification === 'custom' && ['none', 'staged', 'failed', 'cancelled', 'completed'].includes(state))
          return { step: 3, title: 'Custom firmware is installed',
            text: `The bridge detected ${identity.release || 'custom firmware'} on this controller. Upload later releases from the Firmware row, or use Online Updates on the CR100.`,
            art: null, caption: '' };
        if (snapshot.recovery_required) return { step: 2, title: 'Controller recovery needed',
          text: 'The bridge needs the controller to recover before another attempt. Check the status below.',
          art: 'rooms', caption: 'CR100 Rooms screen' };
        switch (state) {
          case 'none': case 'cancelled': return { step: 0, title: 'Choose a firmware package',
            text: defaultCandidate.available ? 'Use the firmware included with the bridge to begin the guided installation.' :
              'Select a custom firmware .upd package for this controller. A different public key is optional.',
            art: 'rooms', caption: 'The installation room will appear after preparation' };
          case 'staged': return { step: 1, title: 'Prepare the controller',
            text: 'Keep the CR100 awake. The bridge will temporarily show only the Install firmware room.',
            art: 'rooms', caption: 'On your CR100 · Rooms' };
          case 'preparing': return { step: 1, title: 'Preparing the controller',
            text: 'Wait for Install firmware to appear in the Rooms list.',
            art: 'rooms', caption: 'On your CR100 · Rooms' };
          case 'await_now_playing': return { step: 1, title: 'Open Now Playing',
            text: 'On the CR100, open Install firmware from Rooms and enter its Now Playing screen. Confirm here only after that screen is open.',
            art: 'now-playing', caption: 'On your CR100 · Now Playing' };
          case 'triggering': case 'root_ready': case 'preflight': return { step: 2, title: 'Checking the controller',
            text: 'The bridge has sent the album art and is checking temporary controller access. The CR100 screen may pause briefly.',
            art: 'now-playing', caption: 'The Now Playing screen may pause during this step' };
          case 'await_flash_confirmation': return { step: 2, title: 'Review and confirm',
            text: 'Check the package and flash destination below. Confirming runs the controller upgrade; the controller may restart.',
            art: null, caption: '' };
          case 'installing': case 'verifying': return { step: 2, title: 'Installing custom firmware',
            text: 'Leave the controller powered on while the bridge checks the result. Its screen may go blank or restart.',
            art: 'now-playing', caption: 'The screen may pause, go blank, or restart' };
          case 'completed': return { step: 0, title: 'Previous install recorded',
            text: 'The bridge assumes stock when it has no fresh custom firmware report. If custom firmware still runs, use Online Updates on the CR100. If you restored original stock 4.3.2, you can install custom firmware here.',
            art: 'rooms', caption: 'The installation room will appear after preparation' };
          case 'failed': return snapshot.sha256 ? { step: 1, title: 'Installation stopped',
            text: 'The temporary room did not become ready. If this controller already runs custom firmware, use Online Updates on the CR100. Retry this setup only on original stock 4.3.2.',
            art: 'rooms', caption: 'On your CR100 · Rooms' } : { step: 0, title: 'Choose a firmware package',
            text: 'The previous attempt stopped. Choose a package to try again.',
            art: 'rooms', caption: 'On your CR100 · Rooms' };
          case 'uncertain': return { step: 2, title: 'Check the controller',
            text: 'The bridge could not confirm the installation result. Check the controller screen and firmware information.',
            art: null, caption: '' };
          default: return { step: 0, title: 'Install custom firmware', text: 'Check the status below.',
            art: 'rooms', caption: 'On your CR100 · Rooms' };
        }
      };
      const setProgress = (percent) => {
        const bar = $('.level', progress);
        bar.setAttribute('aria-valuenow', String(percent));
        bar.querySelectorAll('i').forEach((segment, i) => segment.classList.toggle('on', i < Math.round(percent / 10)));
      };
      const update = (fresh, fetchError, freshIdentity, freshDefault) => {
        if (fresh) {
          if (fresh.status !== snapshot.status) notice = '';
          snapshot = fresh;
        }
        if (freshIdentity) identity = freshIdentity;
        if (freshDefault) defaultCandidate = freshDefault;
        const state = snapshot.status || 'none';
        customAlready = identity?.classification === 'custom' &&
          ['none', 'staged', 'failed', 'cancelled', 'completed'].includes(state);
        node.textContent = IDLE_BOOTSTRAP.includes(state) ? 'Install custom firmware' : 'Continue custom install';
        $('#install-intro', overlay).textContent = customAlready
          ? `${identity.release || 'Custom firmware'} detected on this controller.` : 'For a stock CR100 on firmware 4.3.2.';
        if (state !== 'await_now_playing') screenConfirmed = false;
        if (state !== 'await_flash_confirmation') flashConfirmed = false;
        summary = ['none', 'cancelled', 'completed'].includes(state) ? ''
          : state === 'staged' ? 'Custom install ready to prepare'
          : state === 'failed' ? 'Custom install stopped' : BOOTSTRAP_LABELS[state] || state;
        const selectedFile = packageInput.files[0];
        const canStage = !customAlready && !snapshot.recovery_required && ['none', 'staged', 'failed', 'cancelled', 'completed'].includes(state);
        const needsPackage = ['none', 'cancelled', 'completed'].includes(state) || (state === 'failed' && !snapshot.sha256);
        const showPicker = canStage && (replacing || !!selectedFile || (needsPackage && !defaultCandidate.available));
        fileSection.hidden = !showPicker;
        defaultCard.hidden = !canStage || !defaultCandidate.available || !needsPackage || showPicker;
        $('.install-default-name', overlay).textContent = defaultCandidate.cfw_release
          ? `${defaultCandidate.cfw_release} custom firmware`
          : defaultCandidate.filename || 'Included firmware';
        $('.install-default-size', overlay).textContent = defaultCandidate.bytes ?
          `${(defaultCandidate.bytes / 1024 / 1024).toFixed(1)} MiB` : '';
        changePackage.hidden = !canStage || showPicker;
        $('.bootstrap-package-name', overlay).textContent = selectedFile?.name || 'No package chosen';
        $('.bootstrap-key-name', overlay).textContent = keyInput.files[0]?.name || 'Bundled development key';
        packageInput.disabled = keyInput.disabled = busy || !canStage;
        stageDefault.hidden = defaultCard.hidden;
        stageDefault.textContent = `Use included ${defaultCandidate.cfw_release || 'firmware'}`;
        stageDefault.disabled = busy;
        upload.hidden = !showPicker;
        upload.disabled = busy || !selectedFile;
        start.hidden = customAlready || !['staged', 'failed'].includes(state) || !snapshot.sha256 || showPicker;
        start.disabled = busy || !!snapshot.recovery_required;
        start.textContent = state === 'failed' ? 'Try preparation again' : 'Prepare controller';
        screen.hidden = state !== 'await_now_playing'; screen.disabled = busy || screenConfirmed;
        screen.textContent = screenConfirmed ? 'Now Playing confirmed' : 'I am on Now Playing';
        const hasDestination = Number.isInteger(snapshot.destination_bank) && Number.isInteger(snapshot.destination_generation);
        const destinationId = `${snapshot.sha256}:${snapshot.destination_bank}:${snapshot.destination_generation}`;
        if (state !== 'await_flash_confirmation') { reviewed.checked = false; reviewedDestination = ''; }
        else if (reviewedDestination !== destinationId) { reviewed.checked = false; reviewedDestination = destinationId; }
        $('.install-review', overlay).hidden = state !== 'await_flash_confirmation' || !hasDestination || !snapshot.sha256;
        flash.hidden = state !== 'await_flash_confirmation';
        flash.disabled = busy || flashConfirmed || !hasDestination || !snapshot.sha256 || !reviewed.checked;
        flash.textContent = flashConfirmed ? 'Flash confirmed' : 'Confirm flash';
        cancel.hidden = !snapshot.can_cancel; cancel.disabled = busy;
        done.hidden = !customAlready; done.disabled = busy;
        $('.bootstrap-identity', overlay).hidden = !['staged', 'failed', 'await_flash_confirmation'].includes(state) || !snapshot.sha256 || showPicker;
        $('.bootstrap-filename', overlay).textContent = snapshot.filename || 'Uploaded package';
        $('.bootstrap-version-row', overlay).hidden = !snapshot.version;
        $('.bootstrap-version', overlay).textContent = [snapshot.cfw_release, snapshot.version].filter(Boolean).join(' · ');
        $('.bootstrap-sha', overlay).textContent = snapshot.sha256 || '';
        $('.bootstrap-key-row', overlay).hidden = !snapshot.key_sha256;
        $('.bootstrap-key-sha', overlay).textContent = snapshot.key_sha256 || '';
        $('.bootstrap-destination', overlay).hidden = state !== 'await_flash_confirmation';
        $('.bootstrap-controller', overlay).textContent = `${controller.name} (${controller.mac})${snapshot.controller_ip ? ` · ${snapshot.controller_ip}` : ''}`;
        $('.bootstrap-bank', overlay).textContent = hasDestination ? String(snapshot.destination_bank) : 'Checking…';
        $('.bootstrap-generation', overlay).textContent = hasDestination ? String(snapshot.destination_generation) : 'Checking…';
        const view = showPicker && !['none', 'cancelled'].includes(state) ? {
          step: 0, title: 'Choose a firmware package',
          text: 'Select a different custom firmware .upd package. A different public key is optional.',
          art: 'rooms', caption: 'On your CR100 · Rooms',
        } : stageView(state);
        $('.install-stage-title', overlay).textContent = view.title;
        $('.install-instruction', overlay).textContent = view.text;
        $('.install-caption', overlay).textContent = view.caption;
        $('.install-step-count', overlay).textContent = view.step === 3 ? 'Complete' : `Step ${view.step + 1} of 3`;
        overlay.querySelectorAll('.install-meter li').forEach((item, i) => {
          item.className = i < view.step ? 'done' : i === view.step ? 'now' : '';
          if (i === view.step) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
        });
        $('.install-figure', overlay).hidden = !view.art;
        $('.install-content', overlay).classList.toggle('without-image', !view.art);
        const nextImage = view.art ? `/static/install/${view.art}.png` : '';
        if (nextImage && shownImage !== nextImage) {
          shownImage = nextImage; image.hidden = false; imageFallback.hidden = true;
          image.alt = view.art === 'rooms' ? 'CR100 Rooms screen with Install firmware room' :
            view.art === 'now-playing' ? 'CR100 Now Playing screen for Install firmware' :
              'CR100 screen after custom firmware installation';
          image.src = nextImage;
        }
        $('.install-dialog', overlay).setAttribute('aria-busy', String(busy));
        if (!busy) status.textContent = fetchError || notice || (customAlready ? 'Confirmed from a recent controller heartbeat.' :
          state === 'completed' ? 'Previous installation completed; stock is assumed without a fresh report.' : snapshot.message) ||
          BOOTSTRAP_LABELS[state] || '';
      };
      const perform = async (message, operation) => {
        busy = true; notice = ''; error.textContent = ''; status.textContent = message; update();
        try {
          const result = await operation();
          snapshot = result.bootstrap;
          return true;
        } catch (failure) {
          error.textContent = failure.message;
          if (failure.message === 'Sign in again to continue.') modal?.close({ restoreFocus: false });
          return false;
        } finally {
          busy = false; progress.hidden = true; update();
        }
      };
      image.addEventListener('error', () => { image.hidden = true; imageFallback.hidden = false; });
      node.addEventListener('click', (event) => {
        if (modal?.overlay.isConnected) return;
        update();
        modal = openModal(overlay, event.currentTarget, $('.install-stage-title', overlay), () => { modal = null; });
      });
      done.addEventListener('click', () => modal?.close());
      changePackage.addEventListener('click', () => { replacing = true; error.textContent = ''; update(); packageInput.focus(); });
      packageInput.addEventListener('change', () => { error.textContent = ''; update(); });
      keyInput.addEventListener('change', () => { error.textContent = ''; update(); });
      reviewed.addEventListener('change', () => update());
      stageDefault.addEventListener('click', async () => {
        if (busy || !defaultCandidate.available || identity?.classification === 'custom') return;
        if (await perform('Staging the included package…', () => api('POST', endpoint + '/stage-default', {}))) {
          notice = `${snapshot.cfw_release || 'The firmware'} is ready. Prepare the controller when it is awake.`; update();
        }
      });
      upload.addEventListener('click', async () => {
        const file = packageInput.files[0], publicKey = keyInput.files[0];
        if (!file || busy) return;
        if (!file.name.toLowerCase().endsWith('.upd') || !file.size || file.size > 16 * 1024 * 1024) {
          error.textContent = 'Choose a nonempty .upd file of at most 16 MiB.'; return;
        }
        if (publicKey && (!publicKey.name.toLowerCase().endsWith('.pub') || !publicKey.size || publicKey.size > 4096)) {
          error.textContent = 'Choose a nonempty .pub public key of at most 4096 bytes.'; return;
        }
        progress.hidden = false; setProgress(0);
        const ok = await perform('Uploading custom firmware…', () => uploadStockBootstrap(controller.mac, file, publicKey, (percent) => {
          setProgress(percent); status.textContent = `Uploading custom firmware… ${percent}%`;
        }));
        if (ok) {
          packageInput.value = ''; keyInput.value = ''; replacing = false;
          notice = 'Package uploaded. Prepare the controller when it is awake.'; update();
        }
      });
      start.addEventListener('click', () => {
        if (!busy && ['staged', 'failed'].includes(snapshot.status) && snapshot.sha256)
          perform('Preparing Install firmware room…', () => api('POST', endpoint + '/start', {}));
      });
      screen.addEventListener('click', async () => {
        if (busy || snapshot.status !== 'await_now_playing') return;
        if (await perform('Sending album art to Now Playing…', () => api('POST', endpoint + '/confirm-screen', {}))) {
          if (snapshot.status === 'await_now_playing') {
            screenConfirmed = true; notice = 'Now Playing confirmed. The bridge is sending album art.';
          }
          update();
        }
      });
      flash.addEventListener('click', async () => {
        if (busy || snapshot.status !== 'await_flash_confirmation' || !reviewed.checked) return;
        const sha256 = snapshot.sha256;
        if (!sha256 || !Number.isInteger(snapshot.destination_bank) || !Number.isInteger(snapshot.destination_generation)) return;
        if (await perform('Launching the controller upgrade…', () => api('POST', endpoint + '/confirm-flash', { sha256 }))) {
          if (snapshot.status === 'await_flash_confirmation') {
            flashConfirmed = true; notice = 'Flash confirmed. Waiting for the controller upgrade to start.';
          }
          update();
        }
      });
      cancel.addEventListener('click', () => {
        if (!busy && snapshot.can_cancel) perform('Cancelling installation and restoring rooms…', () => api('POST', endpoint + '/cancel', {}));
      });
      update();
      return { node, update, isBusy: () => busy, isOpen: () => !!modal?.overlay.isConnected,
        summary: () => summary, hidden: () => customAlready,
        idle: () => IDLE_BOOTSTRAP.includes(snapshot.status || 'none') && !snapshot.recovery_required };
    };

    const openRemote = (data, trigger) => {
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="remote-title">
        <div class="popup controller-component-picker">
          <h2 id="remote-title" tabindex="-1">Remote control for ${escapeHtml(data.controller.name)}</h2>
          <p class="hint">${data.ready ? 'Controller is responding.' : 'The controller is offline or needs RE100 firmware with remote control.'}</p>
          <div class="setting firmware-tools">
            <button type="button" class="inline-button" data-action="wake">Wake screen</button>
            <button type="button" class="inline-button" data-action="find">Find controller</button>
          </div>
          <div class="setting"><label class="what">Menu
            <select class="remote-menu">${Object.entries(data.menus).map(([id, name]) =>
              `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`).join('')}</select></label>
            <button type="button" class="inline-button" data-action="menu">Open menu</button></div>
          <div class="setting"><label class="what">Room
            <select class="remote-room">${data.rooms.length ? data.rooms.map(({id, name}) =>
              `<option value="${escapeHtml(id)}">${escapeHtml(name)}</option>`).join('')
              : '<option value="">No rooms available</option>'}</select></label>
            <button type="button" class="inline-button" data-action="now_playing">Open Now Playing</button></div>
          <p class="remote-status" role="status"></p>
          <button type="button" class="inline-button quiet remote-close">Close</button>
        </div></div>`);
      remoteOpen = true;
      const modal = openModal(overlay, trigger, $('#remote-title', overlay), () => { remoteOpen = false; });
      const controls = [...overlay.querySelectorAll('[data-action]')];
      const status = $('.remote-status', overlay);
      const room = $('.remote-room', overlay);
      const disable = (busy) => controls.forEach((button) => {
        button.disabled = busy || (button.dataset.action === 'now_playing' && !room.value);
      });
      for (const button of controls) button.addEventListener('click', async () => {
        const action = button.dataset.action === 'menu' ? $('.remote-menu', overlay).value : button.dataset.action;
        disable(true);
        status.classList.remove('error');
        status.textContent = 'Waiting for the controller…';
        try {
          const result = await api('POST', `/api/controllers/${data.controller.mac}/remote`,
            action === 'now_playing' ? { action, room: room.value } : { action });
          status.textContent = result.message;
        } catch (error) {
          status.classList.add('error');
          status.textContent = error.message;
        } finally {
          disable(false);
        }
      });
      disable(false);
      $('.remote-close', overlay).addEventListener('click', () => modal.close());
    };

    const chooseComponents = (data, trigger) => {
      let inherit = data.visible_components == null;
      const selected = new Set(data.visible_components ?? data.components.filter((item) => item.enabled).map((item) => item.key));
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="components-title">
        <form class="popup controller-component-picker"><h2 id="components-title">Components for ${escapeHtml(data.controller.name)}</h2>
          <p>Choose the speakers and About entries this controller can see. Enable speakers for the household on the Speakers page first.</p>
          <label class="check"><input type="checkbox" class="inherit-components" ${inherit ? 'checked' : ''}><span class="box">${ICONS.tick}</span><span>Use household selection</span></label>
          <div class="controller-component-list"></div>
          <p class="hint">Unavailable saved choices can be removed; otherwise they are kept for when their speaker or network returns. Spotify folders and locks are configured under Spotify → Controller menus.</p>
          <p class="component-save-status" role="status"></p>
          <div class="picker-actions"><button type="button" class="inline-button quiet" data-close>Cancel</button>
            <button type="submit" class="inline-button">Save components</button></div>
        </form></div>`);
      const form = $('form', overlay), list = $('.controller-component-list', overlay);
      const status = $('.component-save-status', overlay);
      const inputs = [];
      for (const item of data.components) {
        const detail = !item.enabled ? 'Not enabled for this household now' : item.kind === 'about'
          ? `About entry${item.parent_names?.length ? ` · Appears only with ${item.parent_names.join(' or ')}` : ''}`
          : item.kind === 'bridge' ? 'About entry for this bridge' : 'Speaker';
        const row = el(`<label class="check controller-component-option"><input type="checkbox"><span class="box">${ICONS.tick}</span>
          <span><strong>${escapeHtml(item.name)}</strong><span class="sub">${escapeHtml(detail)}</span></span></label>`);
        const input = $('input', row);
        input.addEventListener('change', () => {
          if (input.checked) selected.add(item.key); else selected.delete(item.key);
          update();
        });
        inputs.push([input, item]); list.append(row);
      }
      if (!data.components.length) list.append(el('<p class="empty">No components are enabled yet. Choose speakers for the household first.</p>'));
      const update = () => {
        for (const [input, item] of inputs) {
          input.checked = inherit ? item.enabled : selected.has(item.key);
          input.disabled = inherit || (!item.enabled && !selected.has(item.key));
        }
      };
      $('.inherit-components', overlay).addEventListener('change', (event) => { inherit = event.target.checked; update(); });
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const save = $('button[type="submit"]', form);
        save.disabled = true; status.textContent = 'Saving…';
        try {
          const result = await api('PUT', `/api/controllers/${data.controller.mac}/components`,
            { visible_components: inherit ? null : [...selected] });
          modal.close(); toast('Components saved', result.message); await refresh();
        } catch (error) { status.textContent = error.message; save.disabled = false; }
      });
      update();
      const modal = openModal(overlay, trigger, $('.inherit-components', overlay));
    };

    const refresh = async (passive = false) => {
      const data = await api('GET', '/api/controllers');
      try { data.updates = await api('GET', '/api/updates'); }
      catch (error) { data.updates_error = error.message; }
      if (current !== 'controllers') return;
      for (const controller of data.controllers) controller.update = data.updates
        ? data.updates.controllers?.[controller.mac] || { status: 'none' } : undefined;
      await Promise.all(data.controllers.map(async (controller) => {
        try {
          const result = await api('GET', `/api/controllers/${controller.mac}/bootstrap`);
          controller.bootstrap = result.bootstrap;
          controller.default_cfw = result.default_cfw;
          controller.firmware_identity = result.firmware_identity || controller.firmware_identity;
        }
        catch (error) { controller.bootstrap_error = error.message; }
      }));
      if (passive && (renaming || importing || remoteOpen || document.activeElement?.id === 'pairing-import' || document.activeElement?.id === 'radio-channel' || document.activeElement?.closest('.firmware-entry') || [...firmwarePanels.values()].some((panel) => panel.isBusy()) || [...bootstrapPanels.values()].some((panel) => panel.isBusy() || panel.isOpen()))) {
        for (const controller of data.controllers) {
          bootstrapPanels.get(controller.mac)?.update(controller.bootstrap, controller.bootstrap_error, controller.firmware_identity, controller.default_cfw);
          firmwarePanels.get(controller.mac)?.update(controller.update, controller.firmware, controller.firmware_identity);
        }
        updateBridgeStatus(data);
        updateDevicesStatus(data);
        return;
      }
      render(data);
    };
    await refresh();
    softkeys([{ label: 'Refresh', run: refresh }, null, null]);
    pollTimer = setInterval(() => { if (!document.hidden && current === 'controllers') refresh(true).catch(() => {}); }, 3000);
  },
};

// Spotify ----------------------------------------------------------------------------------
// A drawing of the CR100's 320x240 browse screen, as the controller shows a service's root:
// grey status bar with the room, the service title over a rule, bold rows with a pale bar on
// the highlighted one, and the soft keys (blank / Add to Queue for playable items / View Queue).
function cr100Screen() {
  const node = el(`<figure class="cr100-preview">
    <div class="cr100-device">
      <div class="cr100-screen" aria-hidden="true">
        <div class="cr100-status"><span class="cr100-room"></span>${bars(4)}</div>
        <div class="cr100-title"><span class="cr100-play"></span><span class="cr100-service"></span></div>
        <ul class="cr100-list"></ul>
        <div class="cr100-keys"><span></span><span class="cr100-middle"></span><span>View Queue</span></div>
      </div>
      <div class="cr100-wheel" aria-hidden="true"><span></span></div>
    </div>
    <figcaption class="hint">Preview of the controller. Locks show only when an entry is opened.</figcaption>
  </figure>`);
  const list = $('.cr100-list', node);
  const update = ({ room, service, items, selected }) => {
    $('.cr100-room', node).textContent = room || 'Controller';
    $('.cr100-service', node).textContent = service || 'Spotify';
    list.replaceChildren();
    items.forEach((item, index) => {
      const playable = item.kind === 'spotify';
      const track = playable && /(:|\/)track(:|\/)/.test(item.target || '');
      const row = el(`<li class="${index === selected ? 'is-selected' : ''}">${track ? '<span class="cr100-note">♪</span>' : ''}<span></span>${item.locked ? ICONS.lock : ''}</li>`);
      row.children[track ? 1 : 0].textContent = item.label || 'Untitled';
      list.append(row);
    });
    const current = items[selected];
    $('.cr100-middle', node).textContent = current?.kind === 'spotify' ? 'Add to Queue' : '';
    // Scroll the little screen only; scrollIntoView would move the whole page.
    const row = list.querySelector('.is-selected');
    if (row && row.offsetTop < list.scrollTop) list.scrollTop = row.offsetTop;
    else if (row && row.offsetTop + row.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = row.offsetTop + row.offsetHeight - list.clientHeight;
  };
  const describe = (items) => `Preview: ${items.map((item) => `${item.label}${item.locked ? ' (locked)' : ''}`).join(', ')}`;
  return { node, update, describe };
}

async function spotifyMenuEditor(main, onDirty, onConnectionStatus = () => {}, serviceName = () => 'Spotify') {
  const box = el(`<section class="popup spotify-menu-editor">
    <label class="field"><span>Controller</span><select class="sp-controller" aria-label="Controller to edit"></select></label>
    <p class="hint">Each controller has its own menu. A locked entry stays visible and shows an administrator message when opened; the lock covers everything inside it.</p>
    <div class="menu-workbench">
      <div class="menu-column">
        <ol class="sp-menu-list" aria-label="Spotify menu entries"></ol>
        <div class="spotify-menu-actions"><button type="button" class="inline-button sp-menu-save" disabled>Save this menu</button><span class="sp-menu-status" role="status"></span></div>
        <div class="sp-add-controls"><h3>Add a shortcut</h3>
          <div class="inline-field"><label class="field"><span>Playlist</span><select class="sp-playlist" aria-label="Playlist to pin"><option value="">Loading playlists…</option></select></label>
            <button type="button" class="inline-button quiet sp-add-playlist">${ICONS.plus}Add</button></div>
          <div class="inline-field"><label class="field"><span>Or a Spotify link</span><input class="sp-link" type="text" placeholder="https://open.spotify.com/album/…" autocomplete="off" spellcheck="false"></label>
            <button type="button" class="inline-button quiet sp-add-link">Look up</button></div>
          <p class="hint">Playlists, albums, singles and tracks. Singles use Spotify’s album links.</p>
          <p class="error sp-add-error" role="alert"></p>
        </div>
      </div>
    </div>
    <p class="hint">Saved changes block new requests at once. A controller may show lists it already cached until it refreshes.</p>
  </section>`);
  const preview = cr100Screen();
  $('.menu-workbench', box).append(preview.node);
  const folders = el(`<section class="popup spotify-folder-panel">
    <div class="strip-head"><span class="dot sp-folder-dot"></span><span class="sp-folder-status" role="status">Loading folder status…</span></div>
    <p class="hint">Pin a Spotify folder to mirror its playlists and subfolders on this controller. What is inside follows Spotify and refreshes by itself; if Spotify is unavailable, the last complete tree is kept.</p>
    <div class="spotify-menu-actions"><button type="button" class="inline-button sp-connect-folders">Connect Spotify folders</button><button type="button" class="inline-button quiet sp-refresh-folders">Refresh folders</button></div>
    <div class="sp-folder-tree" aria-label="Spotify folder tree"></div>
    <div class="sp-folder-signin" hidden><h3>Finish connecting folders and playback</h3>
      <p class="hint">Use the same Spotify account as above. This connection uses Spotify’s “streaming” permission for folders and for Autoplay; it does not change Spotify’s own Autoplay setting.</p>
      <ol class="howto"><li>Approve access on the Spotify page that opened.</li><li>Spotify then opens an address starting with <strong>127.0.0.1:8898/login</strong> that shows an error. That is expected.</li><li>Copy that whole address and paste it here.</li></ol>
      <div class="inline-field"><label class="field"><span>Address after approving</span><input class="sp-folder-address" type="url" autocomplete="off" placeholder="http://127.0.0.1:8898/login?code=…"></label>
        <button type="button" class="inline-button sp-folder-finish">Finish</button></div>
    </div><p class="error sp-folder-error" role="alert"></p></section>`);
  main.append(el('<h2 class="group-title">Controller menus</h2>'), box, el('<h2 class="group-title">Spotify folders</h2>'), folders);
  const select = $('.sp-controller', box), list = $('.sp-menu-list', box), saveButton = $('.sp-menu-save', box);
  const addControls = $('.sp-add-controls', box), menuStatus = $('.sp-menu-status', box);
  let items = [], baseline = '[]', activeMac = '', generation = 0, library = { folders: [], playlists: [] };
  let folderTreeSignature = null, folderRequest = 0, folderAction = false, selected = 0;
  const editorItems = rows => {
    const search = rows.find(item => item.id === 'search');
    return [{ ...search, label: 'Search' }, ...rows.filter(item => item.id !== 'search')];
  };
  const showPreview = () => {
    selected = Math.min(selected, Math.max(0, items.length - 1));
    preview.update({ room: select.selectedOptions[0]?.textContent, service: serviceName(), items, selected });
    preview.node.setAttribute('aria-label', preview.describe(items));
  };
  const changed = () => {
    const value = JSON.stringify(items) !== baseline;
    saveButton.disabled = !activeMac || !value;
    menuStatus.textContent = value ? 'Unsaved changes' : '';
    showPreview();
    if (box.isConnected) onDirty(value);
  };
  const add = (kind, target, name) => {
    if (!activeMac) return;
    if (items.length >= 64) { $('.sp-add-error', box).textContent = 'Keep this menu to 64 entries.'; return; }
    items.push({ id: `entry_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`,
      kind, target, label: String(name || 'Spotify item').slice(0, 80), locked: false });
    selected = items.length - 1;
    renderItems(); changed();
    menuStatus.textContent = 'Shortcut added. Save this menu to apply it.';
  };
  const kindLabel = (item) => item.id === 'search' ? 'Search · Name and position fixed by the controller'
    : item.kind === 'builtin' ? `Built-in · ${item.target}`
    : item.kind === 'folder' ? `Mirrored folder · ${item.target}` : `Shortcut · ${item.target}`;
  const renderItems = (focus) => {
    list.replaceChildren();
    items.forEach((item, index) => {
      const nativeSearch = item.id === 'search';
      const entry = el(`<li class="menu-entry ${item.locked ? 'is-locked' : ''}">
        <span class="menu-pos" aria-hidden="true">${index + 1}</span>
        <span class="what"><input type="text" class="entry-label" maxlength="80" aria-label="Label for menu entry ${index + 1}" ${nativeSearch ? 'disabled' : ''}>
          <span class="sub"></span></span>
        <span class="entry-tools">
          ${mini(item.locked ? 'lock' : 'unlock', `${item.locked ? 'Unlock' : 'Lock'} ${item.label}`, 'lock-toggle', `aria-pressed="${item.locked}"`)}
          ${mini('up', `Move ${item.label} up`, 'sp-up', index <= 1 || nativeSearch ? 'disabled' : '')}
          ${mini('down', `Move ${item.label} down`, 'sp-down', nativeSearch || index === items.length - 1 ? 'disabled' : '')}
          ${item.kind === 'builtin' ? '<span class="mini-spacer"></span>' : mini('trash', `Remove ${item.label}`, 'sp-remove')}
        </span></li>`);
      const label = $('.entry-label', entry);
      label.value = item.label;
      $('.sub', entry).textContent = kindLabel(item);
      label.addEventListener('input', () => { item.label = label.value; changed(); });
      entry.addEventListener('focusin', () => { if (selected !== index) { selected = index; showPreview(); } });
      $('.lock-toggle', entry).addEventListener('click', () => { item.locked = !item.locked; renderItems(['.lock-toggle', index]); changed(); });
      const move = delta => {
        if (nativeSearch || index + delta <= 0 || index + delta >= items.length) return;
        [items[index], items[index + delta]] = [items[index + delta], items[index]];
        selected = index + delta;
        const target = delta < 0 ? (index + delta <= 1 ? '.sp-down' : '.sp-up') : (index + delta === items.length - 1 ? '.sp-up' : '.sp-down');
        renderItems([target, index + delta]); changed();
      };
      $('.sp-up', entry).addEventListener('click', () => move(-1));
      $('.sp-down', entry).addEventListener('click', () => move(1));
      $('.sp-remove', entry)?.addEventListener('click', () => {
        items.splice(index, 1); selected = Math.max(0, index - 1); renderItems(['.entry-label', selected]); changed();
      });
      list.append(entry);
    });
    if (focus) list.children[focus[1]]?.querySelector(focus[0])?.focus();
  };
  const loadController = async mac => {
    const version = ++generation;
    saveButton.disabled = true;
    const result = await api('GET', '/api/spotify/menu/' + encodeURIComponent(mac));
    if (version !== generation) return;
    activeMac = mac; items = editorItems(result.items); baseline = JSON.stringify(items); selected = 0;
    renderItems(); changed(); addControls.hidden = false;
  };
  select.addEventListener('change', async () => {
    const mac = select.value;
    if (JSON.stringify(items) !== baseline && !confirm('Discard this controller’s unsaved menu changes?')) { select.value = activeMac; return; }
    try { await loadController(mac); } catch (error) { select.value = activeMac; toast('Menu not loaded', error.message, true); }
  });
  const save = async () => {
    if (!activeMac || JSON.stringify(items) === baseline) return null;
    const result = await api('PUT', '/api/spotify/menu/' + encodeURIComponent(activeMac), { items });
    items = editorItems(result.items); baseline = JSON.stringify(items); renderItems(); changed();
    menuStatus.textContent = result.message;
    return result;
  };
  saveButton.addEventListener('click', async () => {
    saveButton.disabled = true;
    try { const result = await save(); if (result) toast('Menu saved', result.message); }
    catch (error) { toast('Menu not saved', error.message, true); }
    finally { saveButton.disabled = !activeMac || JSON.stringify(items) === baseline; }
  });
  $('.sp-add-playlist', box).addEventListener('click', () => {
    const choice = $('.sp-playlist', box).value, item = library.playlists.find(p => p.uri === choice);
    if (item) add('spotify', item.uri, item.name);
  });
  $('.sp-add-link', box).addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true; $('.sp-add-error', box).textContent = '';
    try {
      const result = await api('POST', '/api/spotify/resolve', { uri: $('.sp-link', box).value.trim() });
      add('spotify', result.uri, result.name); $('.sp-link', box).value = '';
    } catch (error) { $('.sp-add-error', box).textContent = error.message; }
    finally { button.disabled = false; }
  });
  $('.sp-link', box).addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); $('.sp-add-link', box).click(); } });
  const renderFolders = () => {
    const status = library.folder_status || {}, tree = $('.sp-folder-tree', folders);
    onConnectionStatus(status);
    const age = status.updated_at ? ago(Math.max(0, Date.now() / 1000 - status.updated_at)) : 'not yet';
    $('.sp-folder-dot', folders).className = `dot sp-folder-dot ${status.connected ? (status.error || status.stale ? 'wait' : 'good') : 'bad'}`;
    $('.sp-folder-status', folders).textContent = status.connected
      ? [`Connected · refreshed ${age}`, status.refreshing ? 'refreshing…' : '', status.stale ? 'showing the saved tree' : '', status.error || ''].filter(Boolean).join(' · ')
      : 'Not connected. Connect folder access to browse your Spotify folders.';
    $('.sp-connect-folders', folders).textContent = status.connected ? 'Reconnect folder access' : 'Connect Spotify folders';
    $('.sp-connect-folders', folders).classList.toggle('quiet', !!status.connected);
    $('.sp-refresh-folders', folders).disabled = !status.connected;
    const signature = JSON.stringify(library.folders || []);
    if (signature === folderTreeSignature) return;
    folderTreeSignature = signature;
    tree.replaceChildren();
    const addButton = (label) => `<button type="button" class="inline-button quiet small" aria-label="${escapeHtml(label)}">${ICONS.plus}Add</button>`;
    const draw = (nodes, destination, depth = 0) => {
      for (const node of nodes || []) {
        if (node.kind !== 'folder' || depth > 32) continue;
        const name = node.name || 'Spotify folder';
        const branch = el(`<details ${depth === 0 ? 'open' : ''}><summary>${CHEVRON}${ICONS.folder}<span class="folder-name"></span>${addButton(`Add folder ${name}`)}</summary><div class="spotify-folder-children"></div></details>`);
        $('.folder-name', branch).textContent = name;
        $('summary button', branch).addEventListener('click', event => { event.preventDefault(); add('folder', node.id, node.name); });
        const children = $('.spotify-folder-children', branch);
        for (const child of node.children || []) {
          if (child.kind === 'folder') draw([child], children, depth + 1);
          else if (child.uri) {
            const label = child.name || child.uri;
            const row = el(`<div class="spotify-folder-leaf"><span></span>${addButton(`Add shortcut ${label}`)}</div>`);
            row.firstElementChild.textContent = label;
            $('button', row).addEventListener('click', () => add('spotify', child.uri, child.name)); children.append(row);
          }
        }
        destination.append(branch);
      }
    };
    draw(library.folders, tree);
    if (!tree.childElementCount) tree.append(el('<p class="hint">No folders in the saved Spotify library yet.</p>'));
  };
  const renderLibrary = () => {
    const options = $('.sp-playlist', box); options.replaceChildren(el('<option value="">Choose a playlist…</option>'));
    for (const item of library.playlists || []) { const option = document.createElement('option'); option.value = item.uri; option.textContent = item.name; options.append(option); }
    $('.sp-add-playlist', box).disabled = !library.playlists?.length;
    if (library.playlist_error) $('.sp-add-error', box).textContent = library.playlist_error;
    renderFolders();
  };
  $('.sp-refresh-folders', folders).addEventListener('click', async event => {
    ++folderRequest; folderAction = true;
    const button = event.currentTarget; button.disabled = true; $('.sp-folder-error', folders).textContent = '';
    try { const result = await api('POST', '/api/spotify/folders/refresh'); Object.assign(library, result); renderFolders(); if (!result.refreshed) $('.sp-folder-error', folders).textContent = result.message; }
    catch (error) { $('.sp-folder-error', folders).textContent = error.message; }
    finally { folderAction = false; button.disabled = !library.folder_status?.connected; }
  });
  $('.sp-connect-folders', folders).addEventListener('click', async () => {
    $('.sp-folder-error', folders).textContent = ''; const tab = window.open('about:blank', '_blank');
    try {
      const result = await api('POST', '/api/spotify/folders/sign-in');
      if (tab) { tab.opener = null; tab.location = result.url; } else location.assign(result.url);
      $('.sp-folder-signin', folders).hidden = false; $('.sp-folder-address', folders).focus();
    } catch (error) { if (tab) tab.close(); $('.sp-folder-error', folders).textContent = error.message; }
  });
  $('.sp-folder-finish', folders).addEventListener('click', async event => {
    ++folderRequest; folderAction = true;
    const button = event.currentTarget; button.disabled = true; $('.sp-folder-error', folders).textContent = '';
    try {
      const result = await api('POST', '/api/spotify/folders/finish', { address: $('.sp-folder-address', folders).value });
      Object.assign(library, result); $('.sp-folder-signin', folders).hidden = true; $('.sp-folder-address', folders).value = ''; renderFolders(); toast('Folder access connected', result.message);
    } catch (error) { $('.sp-folder-error', folders).textContent = error.message; }
    finally { folderAction = false; button.disabled = false; }
  });
  addControls.hidden = true;
  try {
    const data = await api('GET', '/api/controllers');
    for (const controller of data.controllers || []) { const option = document.createElement('option'); option.value = controller.mac; option.textContent = controller.name; select.append(option); }
    if (select.options.length) await loadController(select.value);
    else { select.disabled = true; menuStatus.textContent = 'Pair a controller before editing its menu.'; showPreview(); }
  } catch (error) { menuStatus.textContent = error.message; }
  try { library = await api('GET', '/api/spotify/library'); renderLibrary(); }
  catch (error) { $('.sp-folder-status', folders).textContent = error.message; $('.sp-playlist', box).replaceChildren(el('<option value="">Playlists unavailable</option>')); }
  if (box.isConnected && current === 'spotify') {
    let polling = false;
    pollTimer = setInterval(async () => {
      if (polling || folderAction || document.hidden || current !== 'spotify' || !box.isConnected || !$('#gate').hidden) return;
      const version = folderRequest;
      polling = true;
      try {
        const snapshot = await api('GET', '/api/spotify/folders');
        if (version === folderRequest && box.isConnected && current === 'spotify') {
          Object.assign(library, snapshot); renderFolders();
        }
      } catch { /* the last complete tree stays visible; the next poll retries */ }
      finally { polling = false; }
    }, 30000);
  }
  return { save, connectPlayback: () => $('.sp-connect-folders', folders).click(), refreshPreview: showPreview };
}

views.spotify = {
  title: 'Spotify',
  async open(main) {
    const data = await api('GET', '/api/spotify');
    let menuDirty = false, menuEditor = null, playbackConnected = !!data.autoplay_connected;
    main.innerHTML = '';
    main.append(el(`<p class="lede">Spotify appears in the controller’s Music menu. It needs Spotify Premium and a Client ID from the Spotify developer dashboard.</p>`));

    const account = data.authorized
      ? el(`<div class="line"><div class="tile">${ICONS.note}<span class="name">${escapeHtml(data.account?.name || 'Signed in')}</span></div>
          <div class="row"><div class="what"><div class="main"><span class="dot good"></span>Signed in to Spotify</div><div class="sub">${escapeHtml(data.account?.plan ? `${data.account.plan.charAt(0).toUpperCase()}${data.account.plan.slice(1)} plan` : 'Account details appear after the next sign-in')}</div></div></div></div>`)
      : el(`<div class="line"><div class="tile off">${ICONS.note}<span class="name">Not signed in</span></div>
          <div class="row"><div class="what"><div class="main"><span class="dot bad"></span>Not signed in</div><div class="sub">${data.client_id ? 'Choose Sign in to Spotify below.' : 'Add your Client ID first.'}</div></div></div></div>`);
    main.append(el('<h2 class="group-title">Account</h2>'), account);

    const signIn = el(`<div class="popup" hidden><h3>Finish signing in</h3>
      <ol class="howto">
        <li>Approve access on the Spotify page that just opened.</li>
        <li>Spotify then opens an address starting with <strong>127.0.0.1</strong> that shows an error. That is expected.</li>
        <li>Copy that whole address from the address bar and paste it here.</li>
      </ol>
      <div class="inline-field"><label class="field"><span>Address after approving</span><input type="url" id="sp-address" placeholder="http://127.0.0.1:8888/callback?code=…" autocomplete="off"></label>
        <button type="button" class="inline-button" id="sp-finish">Finish</button></div>
      <p class="error" id="sp-error" role="alert"></p></div>`);
    main.append(signIn);
    $('#sp-finish', signIn).addEventListener('click', async () => {
      try {
        const result = await api('POST', '/api/spotify/finish', { address: $('#sp-address', signIn).value });
        toast('Signed in', result.message);
        show('spotify');
      } catch (error) { $('#sp-error', signIn).textContent = error.message; }
    });

    const form = el(`<div class="popup">
      <div class="settings-list flush">
        <label class="setting check"><span class="what"><span class="main">Show Spotify on the controller</span><span class="sub">As a music service in the Music menu</span></span>
          <input type="checkbox" id="sp-enabled" ${data.enabled ? 'checked' : ''}><span class="box">${ICONS.tick}</span></label>
      </div>
      <label class="field gap-above"><span>Name on the controller</span><input type="text" id="sp-name" maxlength="24" value="${escapeHtml(data.name)}"></label>
      <label class="field"><span>Client ID</span><input type="text" id="sp-client" maxlength="32" spellcheck="false" autocomplete="off" value="${escapeHtml(data.client_id)}">
        <span class="hint">From your app’s settings at developer.spotify.com. Add this redirect URI to the app:</span></label>
      <div class="copy-field"><input type="text" readonly value="${escapeHtml(data.redirect_uri)}" aria-label="Redirect URI">${mini('copy', 'Copy redirect URI', 'copy-uri')}</div>
    </div>`);
    main.append(el('<h2 class="group-title">On the controller</h2>'), form);
    const uri = $('.copy-field input', form);
    uri.addEventListener('focus', () => uri.select());
    $('.copy-uri', form).addEventListener('click', async () => {
      uri.select();
      // The bridge is served over plain HTTP, where the async Clipboard API is unavailable.
      let copied = false;
      try { await navigator.clipboard.writeText(uri.value); copied = true; }
      catch { try { copied = document.execCommand('copy'); } catch { copied = false; } }
      toast(copied ? 'Copied' : 'Select and copy', copied ? 'The redirect URI is on the clipboard.' : 'The address is selected; copy it with your keyboard.', !copied);
    });

    const playback = el(`<section class="popup">
      <div class="settings-list flush">
        <label class="setting check"><span class="what"><span class="main">Autoplay similar music</span>
          <span class="sub">When music started from this bridge ends, keep playing related tracks outside the Sonos queue. Music you queue comes first. Turning it off lets the current track finish; Spotify’s own Autoplay setting is unchanged.</span></span>
          <input type="checkbox" id="sp-autoplay" ${data.autoplay ? 'checked' : ''}><span class="box">${ICONS.tick}</span></label>
      </div>
      <div class="strip-head gap-above"><span class="dot sp-playback-dot"></span><span class="sp-playback-status" role="status"></span>
        <button type="button" class="inline-button quiet sp-connect-playback" disabled>Connect playback</button></div>
    </section>`);
    const renderPlaybackConnection = (status) => {
      if (status) playbackConnected = !!status.connected;
      $('.sp-playback-dot', playback).className = `dot sp-playback-dot ${playbackConnected ? 'good' : 'wait'}`;
      $('.sp-playback-status', playback).textContent = playbackConnected
        ? 'Playback connection saved, shared with Spotify folders'
        : 'Autoplay needs a playback connection with the same account';
      const button = $('.sp-connect-playback', playback);
      button.textContent = playbackConnected ? 'Reconnect' : 'Connect playback';
      button.disabled = !menuEditor;
    };
    $('.sp-connect-playback', playback).addEventListener('click', () => menuEditor?.connectPlayback());
    main.append(el('<h2 class="group-title">Autoplay</h2>'), playback);
    renderPlaybackConnection();

    const values = () => ({ enabled: $('#sp-enabled', form).checked, name: $('#sp-name', form).value.trim(),
      client_id: $('#sp-client', form).value.trim(), autoplay: $('#sp-autoplay', playback).checked });
    const baseline = JSON.stringify(values());
    const update = () => {
      const isDirty = JSON.stringify(values()) !== baseline;
      setDirty(isDirty || menuDirty);
      softkeys([
        data.authorized
          ? { label: 'Sign out', run: async () => {
              if (!confirm('Sign the bridge out of Spotify? Spotify disappears from the controller until you sign in again.')) return;
              const result = await api('POST', '/api/spotify/sign-out'); toast('Signed out', result.message); show('spotify'); } }
          : { label: 'Sign in to Spotify', disabled: !data.client_id || isDirty, run: async () => {
              const tab = window.open('about:blank', '_blank');
              const { url } = await api('POST', '/api/spotify/sign-in');
              if (tab) { tab.opener = null; tab.location = url; } else { location.assign(url); }
              signIn.hidden = false; $('#sp-address', signIn).focus(); } },
        null,
        { label: 'Save changes', disabled: (!isDirty && !menuDirty) || (menuDirty && !menuEditor), run: async () => {
            let result = menuDirty ? await menuEditor.save() : null;
            if (isDirty) result = await api('PUT', '/api/spotify', values());
            setDirty(false); toast('Saved', result?.message); show('spotify'); } },
      ]);
    };
    form.addEventListener('input', () => { update(); menuEditor?.refreshPreview(); });
    form.addEventListener('change', update);
    playback.addEventListener('change', update);
    update();
    menuEditor = await spotifyMenuEditor(main, value => { if (current === 'spotify' && form.isConnected) { menuDirty = value; update(); } },
      renderPlaybackConnection, () => $('#sp-name', form).value.trim() || 'Spotify');
    if (form.isConnected) { renderPlaybackConnection(); update(); }
  },
};

// Speaker source ---------------------------------------------------------------------------
views.source = {
  title: 'Speaker source',
  async open(main) {
    const data = await api('GET', '/api/source');
    const labels = Object.fromEntries(data.sources.map((s) => [s.name, s.label]));
    let chosen = data.source;
    main.innerHTML = '';
    main.append(el(`<p class="lede">The controller's rooms are speakers from one source. Choose where the bridge finds them, what they are playing and your music.</p>`));
    const choices = el(`<div class="popup" role="radiogroup" aria-labelledby="source-title"><h3 id="source-title">Where speakers come from</h3>
      <label class="choice"><input type="radio" name="source" value="ha"><span class="what">
        <span class="main">Home Assistant${data.source === 'ha' ? '<span class="chip">In use</span>' : ''}</span>
        <span class="sub">Any media player Home Assistant knows, Sonos or not. Needs its address and an access token.</span></span></label>
      <label class="choice"><input type="radio" name="source" value="sonos"><span class="what">
        <span class="main">Sonos speakers directly${data.source === 'sonos' ? '<span class="chip">In use</span>' : ''}</span>
        <span class="sub">The bridge talks to the Sonos speakers on its network itself. No Home Assistant needed.</span></span></label>
      </div>`);
    choices.querySelector(`input[value="${chosen}"]`).checked = true;
    const settings = el('<div></div>');
    main.append(choices, settings);

    const ha = data.home_assistant, sonos = data.sonos;
    const haForm = el(`<div class="popup"><h3>Home Assistant</h3>
      <label class="field"><span>Address</span><input type="url" id="ha-url" value="${escapeHtml(ha.url)}" placeholder="http://homeassistant.local:8123" spellcheck="false"></label>
      <label class="field"><span>Long-lived access token</span><input type="password" id="ha-token" autocomplete="off" placeholder="${ha.token_saved ? 'Saved. Paste a new token to replace it.' : 'Paste a token'}">
        <div class="hint">In Home Assistant: your profile › Security › Long-lived access tokens. A saved token is never shown.</div></label></div>`);
    const sonosForm = el(`<div class="popup"><h3>Sonos</h3>
      <label class="field"><span>Speaker addresses</span><input type="text" id="sonos-hosts" value="${escapeHtml(sonos.hosts.join(', '))}" placeholder="Found automatically" spellcheck="false" autocomplete="off">
        <div class="hint">Leave empty to find speakers automatically. For speakers on another part of the network, enter one or more addresses, like 192.168.1.30.</div></label></div>`);
    const haValues = () => ({ url: $('#ha-url', haForm).value.trim(), token: $('#ha-token', haForm).value.trim() });
    const sonosValues = () => ({ hosts: $('#sonos-hosts', sonosForm).value.trim() });
    const baseline = JSON.stringify([haValues(), sonosValues()]);

    const status = (name) => {
      if (name === 'sonos') {
        if (!sonos.connected) return { dot: 'bad', text: escapeHtml(sonos.error || 'The Sonos speakers are not answering.') };
        const count = sonos.speakers.length;
        return { dot: 'good', text: `${count} Sonos speaker${count === 1 ? '' : 's'} found on this network`, rooms: sonos.speakers };
      }
      if (ha.connected) return { dot: 'good', text: `Connected${ha.version ? ` to Home Assistant ${escapeHtml(ha.version)}` : ''} · ${ha.media_players} media players` };
      if (data.source !== 'ha') return { dot: 'bad', text: 'Not in use. The bridge connects once you choose Home Assistant.' };
      return { dot: 'bad', text: escapeHtml(ha.error || 'Not connected.') };
    };

    const render = () => {
      settings.innerHTML = '';
      const state = status(chosen);
      const health = chosen === data.source ? data.speaker_health : null;
      const chips = (names) => `<ul class="area-map">${names.length ? names.map((name) => `<li>${escapeHtml(name)}</li>`).join('') : '<li class="muted">None</li>'}</ul>`;
      settings.append(el(`<div class="popup source-status" role="status"><div class="state-line"><span class="dot ${state.dot}"></span><span>${state.text}</span></div>
        ${state.rooms ? chips(state.rooms) : ''}
        ${health ? `<div class="selected-rooms"><span class="meta">On the controller</span>${chips(health.selected)}
          <p class="hint">${escapeHtml(health.message)} <a href="#speakers">Change on Speakers</a></p></div>` : ''}</div>`));
      settings.append(chosen === 'sonos' ? sonosForm : haForm);
      update();
    };

    const update = () => {
      const isDirty = chosen !== data.source || JSON.stringify([haValues(), sonosValues()]) !== baseline;
      setDirty(isDirty);
      softkeys([
        { label: 'Test connection', run: async () => {
            const result = chosen === 'sonos' ? await api('POST', '/api/sonos/test', sonosValues())
              : await api('POST', '/api/home-assistant/test', haValues());
            toast(result.ok ? 'Connection works' : 'Connection failed', result.message, !result.ok); } },
        null,
        { label: chosen !== data.source ? `Use ${labels[chosen]}` : 'Save changes', disabled: !isDirty, run: save },
      ]);
    };

    const save = async () => {
      const [haNow, sonosNow] = [haValues(), sonosValues()];
      const [haThen, sonosThen] = JSON.parse(baseline);
      if (chosen !== data.source && !confirm(`Take the controller's rooms from ${labels[chosen]}? Your ${labels[data.source]} rooms are kept for when you switch back.`)) return;
      let result = null;
      if (JSON.stringify(haNow) !== JSON.stringify(haThen)) result = await api('PUT', '/api/home-assistant', haNow);
      if (JSON.stringify(sonosNow) !== JSON.stringify(sonosThen)) result = await api('PUT', '/api/sonos', sonosNow);
      if (chosen !== data.source) result = await api('PUT', '/api/source', { source: chosen });
      setDirty(false);
      toast('Saved', result ? result.message : '');
      setTimeout(() => show('source'), 1500);
    };

    choices.addEventListener('change', (event) => { chosen = event.target.value; render(); });
    haForm.addEventListener('input', update);
    sonosForm.addEventListener('input', update);
    render();
  },
};

// Radio ------------------------------------------------------------------------------------
// One settings row: a CR100 check box, or a select on the right.
const settingCheck = (name, label, checked, sub = '') => `<label class="setting check"><span class="what"><span class="main">${label}</span>${
  sub ? `<span class="sub">${sub}</span>` : ''}</span><input type="checkbox" name="${name}" ${checked ? 'checked' : ''}><span class="box">${ICONS.tick}</span></label>`;
const settingSelect = (name, label, options, sub = '') => `<label class="setting"><span class="what"><span class="main">${label}</span>${
  sub ? `<span class="sub">${sub}</span>` : ''}</span><select name="${name}">${options.map(([value, text]) => `<option value="${escapeHtml(value)}">${escapeHtml(text)}</option>`).join('')}</select></label>`;

views.radio = {
  title: 'Radio',
  async open(main) {
    const data = await api('GET', '/api/radio');
    const s = data.settings;
    const place = s.location?.label || s.location?.name || '';
    main.innerHTML = `<p class="lede">TuneIn in the controller’s original Radio menu, with podcasts, favorites and album artwork.</p>
      <form id="radio-settings">
        <h2 class="group-title">Browsing</h2>
        <div class="popup"><div class="settings-list flush">
          ${settingSelect('station_order', 'Station order', [['popular', 'Most popular'], ['name', 'By name'], ['provider', 'Directory order']])}
          ${settingSelect('preferred_language', 'Preferred language', [['', 'All languages']], '<span id="radio-language-status" role="status">Loading languages…</span>')}
          ${settingCheck('language_filter', 'Only show the preferred language', s.language_filter)}
          ${settingCheck('show_podcasts', 'Show Podcasts in Radio', s.show_podcasts)}
          ${settingCheck('show_language', 'Show By Language in Radio', s.show_language)}
        </div></div>
        <h2 class="group-title">Listening</h2>
        <div class="popup"><div class="settings-list flush">
          ${settingSelect('quality', 'Stream quality', [['auto', 'Automatic'], ['best', 'Best available'], ['low', 'Low bandwidth']])}
          ${settingCheck('automatic_reconnect', 'Reconnect automatically', s.automatic_reconnect)}
          ${settingCheck('episode_resume', 'Resume podcast episodes', s.episode_resume)}
          ${settingCheck('cover_lookup', 'Find missing album covers', s.cover_lookup, 'Album art is always on. When a song cannot be identified, the station or show image is shown.')}
          ${settingCheck('history', 'Keep recently played and recent searches', s.history)}
        </div><p class="error" role="alert"></p></div>
        <button type="submit" hidden>Save</button>
      </form>
      <h2 class="group-title">Local Radio</h2>
      <section class="popup">
        <div class="strip-head location-head">${ICONS.pin}<span class="strip-title">${escapeHtml(place || 'Location detected automatically')}</span></div>
        <form id="radio-location" class="inline-field location-form">
          <label class="field"><span>U.S. ZIP code</span><input name="zip" inputmode="numeric" pattern="[0-9]{5}" maxlength="5" required></label>
          <button class="inline-button quiet" type="submit">Set</button>
          <span class="or">or</span>
          <button class="inline-button quiet" type="button" id="radio-city">Browse places</button>
        </form>
      </section>
      <h2 class="group-title">Favorites</h2>
      <section class="popup"><ul class="item-list" id="radio-favorites"></ul></section>
      <h2 class="group-title">Custom stations</h2>
      <section class="popup"><ul class="item-list" id="radio-custom"></ul>
        <button type="button" class="inline-button quiet gap-above" id="radio-add">${ICONS.plus}Add station</button></section>
      <h2 class="group-title">Radio data</h2>
      <section class="popup"><div class="radio-actions">
        <button type="button" class="inline-button quiet" id="radio-export">Export favorites and settings</button>
        <span class="file-pick"><label><input type="file" id="radio-import" accept="application/json,.json"><span class="inline-button quiet">Import…</span></label></span>
        <span class="spacer"></span>
        <button type="button" class="inline-button quiet danger" data-radio-action="history-clear">Clear history</button>
        <button type="button" class="inline-button quiet danger" data-radio-action="reset">Reset preferences</button>
      </div></section>`;
    const form = $('#radio-settings', main);
    const language = form.elements.preferred_language;
    if (s.preferred_language) language.append(new Option('Saved language', s.preferred_language));
    for (const key of ['station_order', 'quality', 'preferred_language']) form.elements[key].value = s[key] || '';
    let baseline = '';
    const snapshot = () => JSON.stringify([...form.elements].filter((f) => f.name).map((f) => f.type === 'checkbox' ? f.checked : f.value));
    api('POST', '/api/radio/language-options', {}).then((result) => {
      if (!language.isConnected) return;
      const selected = language.value;
      language.replaceChildren(new Option('All languages', ''));
      for (const choice of result.languages) language.append(new Option(choice.label, choice.id));
      if (selected && !result.languages.some((choice) => choice.id === selected)) language.append(new Option('Saved language (unavailable now)', selected));
      language.value = selected;
      $('#radio-language-status', main).textContent = 'Turn on the filter below to browse only this language';
      if (!dirty) baseline = snapshot();
    }).catch(() => {
      if (language.isConnected) $('#radio-language-status', main).textContent = 'The language list is unavailable. Your choice is kept; Refresh Radio tries again.';
    });
    const save = async () => {
      const settings = {};
      for (const field of form.elements) if (field.name) settings[field.name] = field.type === 'checkbox' ? field.checked : field.value;
      await api('PUT', '/api/radio', { settings });
      baseline = snapshot(); setDirty(false); keys(); toast('Saved', 'Radio preferences updated.');
    };
    const act = async (command, body) => { await api('POST', '/api/radio/' + command, body); setDirty(false); await show('radio'); };
    const keys = () => softkeys([{ label: 'Refresh Radio', run: () => act('refresh', {}) }, null,
      { label: 'Save changes', disabled: !dirty, run: save }]);
    baseline = snapshot();
    const changed = () => { setDirty(snapshot() !== baseline); keys(); };
    form.addEventListener('input', changed);
    form.addEventListener('change', changed);
    form.addEventListener('submit', (event) => { event.preventDefault(); save().catch((e) => toast('Not saved', e.message, true)); });
    keys();

    $('#radio-location', main).addEventListener('submit', async (event) => {
      event.preventDefault();
      if (dirty && !confirm('Discard your unsaved Radio preferences?')) return;
      try { await api('POST', '/api/radio/location', { zip: event.target.elements.zip.value }); toast('Location updated'); setDirty(false); await show('radio'); }
      catch (e) { toast('Location not changed', e.message, true); }
    });

    // Browse places: a breadcrumb trail and a list where choosing a place opens it.
    $('#radio-city', main).addEventListener('click', (event) => {
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="radio-city-title"><div class="popup">
        <h2 id="radio-city-title">Choose a place</h2>
        <nav class="crumbs" aria-label="Path"></nav>
        <ul class="place-list"></ul>
        <p class="hint" role="status" data-status></p><p class="error" role="alert"></p>
        <div class="picker-actions"><button type="button" class="inline-button quiet" data-close>Cancel</button>
          <button type="button" class="inline-button" data-use disabled>Use this place</button></div></div></div>`);
      const crumbs = $('.crumbs', overlay), places = $('.place-list', overlay), use = $('[data-use]', overlay);
      const modal = openModal(overlay, event.currentTarget, $('[data-close]', overlay));
      const trail = [{ id: 'r0', label: 'World' }];
      let page = null, generation = 0;
      const load = async () => {
        const token = ++generation, here = trail[trail.length - 1];
        use.disabled = true; use.textContent = 'Use this place';
        crumbs.replaceChildren(...trail.map((step, index) => {
          if (index === trail.length - 1) { const last = el('<span aria-current="location"></span>'); last.textContent = step.label; return last; }
          const crumb = el('<button type="button" class="crumb"></button>');
          crumb.textContent = step.label;
          crumb.addEventListener('click', () => { trail.length = index + 1; load(); });
          return crumb;
        }));
        places.replaceChildren();
        $('[data-status]', overlay).textContent = 'Loading places…'; $('.error', overlay).textContent = '';
        try {
          const result = await api('POST', '/api/radio/location-browse', { id: here.id, ancestors: trail.slice(0, -1).map((step) => step.id) });
          if (token !== generation || !overlay.isConnected) return;
          page = result;
          for (const option of page.locations) {
            const row = el(`<li><button type="button" class="place"><span></span>${CHEVRON}</button></li>`);
            $('span', row).textContent = option.label;
            $('button', row).addEventListener('click', () => { trail.push(option); load(); });
            places.append(row);
          }
          use.disabled = !page.selectable;
          if (page.selectable) use.textContent = `Use ${here.label}`;
          $('[data-status]', overlay).textContent = page.selectable ? 'This place has local stations.' : page.locations.length ? 'Choose a place to open it.' : 'No local stations are listed here.';
          (places.querySelector('button') || (page.selectable ? use : $('[data-close]', overlay))).focus();
        } catch (e) {
          if (token === generation && overlay.isConnected) { $('[data-status]', overlay).textContent = ''; $('.error', overlay).textContent = e.message; }
        }
      };
      use.addEventListener('click', async () => {
        if (!page?.selectable) return;
        use.disabled = true;
        try {
          const here = trail[trail.length - 1];
          await api('POST', '/api/radio/location-select', { id: here.id, label: here.label, ancestors: trail.slice(0, -1).map((step) => step.id) });
          modal.close(); setDirty(false); toast('Location updated'); await show('radio');
        } catch (e) { if (overlay.isConnected) { $('.error', overlay).textContent = e.message; use.disabled = false; } }
      });
      load();
    });

    const favorites = $('#radio-favorites', main);
    if (!data.favorites.length) favorites.append(el('<li class="hint">Add stations and shows with Add to Favorites on the controller.</li>'));
    for (const [index, item] of data.favorites.entries()) {
      const row = el(`<li class="item"><span class="what"><span class="main"></span><span class="sub"></span></span>
        <span class="tools">${mini('up', `Move ${item.title} up`, '', index ? 'data-action="up"' : 'data-action="up" disabled')}${mini('pencil', `Rename ${item.title}`, '', 'data-action="rename"')}${mini('trash', `Remove ${item.title}`, '', 'data-action="remove"')}</span></li>`);
      $('.main', row).textContent = item.title;
      $('.sub', row).textContent = item.kind;
      row.addEventListener('click', async (event) => {
        const action = event.target.closest('[data-action]')?.dataset.action;
        if (!action) return;
        try {
          if (action === 'remove' && confirm(`Remove ${item.title} from favorites?`)) await act('favorite-remove', { id: item.id });
          if (action === 'up') await act('favorite-edit', { id: item.id, index: index - 1 });
          if (action === 'rename') {
            editInPlace($('.main', row), { value: item.title, label: `Name for ${item.title}`, maxlength: 250 }, async (title) => {
              if (!title) { await show('radio'); return; }
              try { await act('favorite-edit', { id: item.id, title }); } catch (e) { toast('Not renamed', e.message, true); await show('radio'); }
            });
          }
        } catch (e) { toast('Not changed', e.message, true); }
      });
      favorites.append(row);
    }

    const editStation = (item = null, trigger = $('#radio-add', main)) => {
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="radio-editor-title"><form class="popup">
        <h2 id="radio-editor-title">${item ? 'Edit' : 'Add'} station</h2>
        <label class="field"><span>Name</span><input name="name" required maxlength="250" value="${escapeHtml(item?.title || '')}"></label>
        <div class="inline-field"><label class="field"><span>Stream or playlist URL</span><input name="url" type="url" required value="${escapeHtml(item?.url || '')}"></label>
          <button type="button" class="inline-button quiet" data-test>Test</button></div>
        <p class="hint" data-test-result role="status"></p>
        <label class="field"><span>Station logo URL</span><input name="logo" type="url" value="${escapeHtml(item?.logo || item?.image || '')}"><span class="hint">Optional</span></label>
        <p class="error" role="alert"></p><div class="picker-actions">
        <button type="button" class="inline-button quiet" data-close>Cancel</button><button class="inline-button" type="submit">Save station</button></div></form></div>`);
      const editor = $('form', overlay), modal = openModal(overlay, trigger, editor.elements.name);
      const result = $('[data-test-result]', editor);
      result.hidden = true;
      $('[data-test]', editor).addEventListener('click', async () => {
        $('.error', editor).textContent = ''; result.hidden = false; result.textContent = 'Testing…';
        try { const test = await api('POST', '/api/radio/test-url', { url: editor.elements.url.value }); result.textContent = `Works: ${test.streams.length} stream option${test.streams.length === 1 ? '' : 's'} found. Nothing was played.`; }
        catch (e) { result.hidden = true; $('.error', editor).textContent = e.message; }
      });
      editor.addEventListener('submit', async (event) => {
        event.preventDefault();
        try {
          await api('POST', '/api/radio/custom', { operation: item ? 'edit' : 'add', id: item?.id,
            name: editor.elements.name.value, url: editor.elements.url.value, logo: editor.elements.logo.value });
          modal.close(); setDirty(false); await show('radio');
        } catch (e) { $('.error', editor).textContent = e.message; }
      });
    };
    $('#radio-add', main).addEventListener('click', (event) => editStation(null, event.currentTarget));
    const custom = $('#radio-custom', main);
    if (!data.custom_stations.length) custom.append(el('<li class="hint">Add a stream address to play it from the controller’s Radio menu.</li>'));
    for (const item of data.custom_stations) {
      const row = el(`<li class="item"><span class="what"><span class="main"></span><span class="sub"></span></span>
        <span class="tools">${mini('pencil', `Edit ${item.title}`, 'edit')}${mini('trash', `Delete ${item.title}`, 'delete')}</span></li>`);
      $('.main', row).textContent = item.title;
      $('.sub', row).textContent = item.url;
      $('.edit', row).addEventListener('click', (event) => editStation(item, event.currentTarget));
      $('.delete', row).addEventListener('click', async () => {
        if (!confirm(`Delete ${item.title}?`)) return;
        try { await act('custom', { operation: 'delete', id: item.id }); } catch (e) { toast('Not deleted', e.message, true); }
      });
      custom.append(row);
    }
    main.querySelectorAll('[data-radio-action]').forEach((button) => button.addEventListener('click', async () => {
      if (!confirm(button.dataset.radioAction === 'reset' ? 'Reset Radio preferences? Favorites and custom stations are kept.' : 'Clear Radio listening and search history?')) return;
      try { await act(button.dataset.radioAction, {}); } catch (e) { toast('Not changed', e.message, true); }
    }));
    $('#radio-export', main).addEventListener('click', async () => {
      try {
        downloadJson('cr100-radio.json', await api('POST', '/api/radio/export', {}));
      } catch (e) { toast('Export failed', e.message, true); }
    });
    $('#radio-import', main).addEventListener('change', async (event) => {
      const file = event.target.files[0]; if (!file || !confirm('Merge this file into your Radio favorites and settings?')) { event.target.value = ''; return; }
      try { await act('import', { data: JSON.parse(await file.text()), merge: true }); } catch (e) { toast('Import failed', e.message, true); }
    });
  },
};

// Alarms -----------------------------------------------------------------------------------
// Days in the CR100's order, Monday first; Sonos numbers them from Sunday = 0.
const ALARM_DAYS = [[1, 'M', 'Monday'], [2, 'T', 'Tuesday'], [3, 'W', 'Wednesday'], [4, 'T', 'Thursday'], [5, 'F', 'Friday'], [6, 'S', 'Saturday'], [0, 'S', 'Sunday']];
const RECURRENCE = { DAILY: 'Every day', WEEKDAYS: 'Weekdays', WEEKENDS: 'Weekends', ONCE: 'Once' };
function alarmDays(recurrence) {
  if (recurrence === 'DAILY') return [0, 1, 2, 3, 4, 5, 6];
  if (recurrence === 'WEEKDAYS') return [1, 2, 3, 4, 5];
  if (recurrence === 'WEEKENDS') return [0, 6];
  const custom = /^ON_(\d+)$/.exec(recurrence || '');
  return custom ? [...custom[1]].map(Number) : [];
}
const minutesText = (seconds) => { const m = Math.round(seconds / 60); return m % 60 ? (m > 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`) : `${m / 60} h`; };

views.alarms = {
  title: 'Alarms',
  async open(main) {
    const data = await api('GET', '/api/alarms');
    const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const roomName = (key) => data.rooms.find((room) => room.key === key || room.udn === key)?.name || 'Unavailable room';
    main.innerHTML = `<p class="lede">Household alarms share one schedule across Radio and future music providers.</p>
      <div class="alarm-list" id="alarm-list"></div>
      <p class="hint clock-tip">For Snooze and Stop on a CR100, edit the alarm on that controller and set
        <span class="menu-path"><kbd>Advanced</kbd>${CHEVRON}<kbd>Show Clock on this controller</kbd>${CHEVRON}<kbd>Yes</kbd></span>, then save. Do this on each controller.</p>`;
    const list = $('#alarm-list', main);
    const refresh = () => show('alarms');
    const addKey = () => document.querySelector('#softkeys button[data-slot="2"]');

    const editor = (alarm = null, trigger = addKey()) => {
      const sources = data.sources.filter((source) => source.media_ref);
      const selected = alarm?.media_ref || { provider: 'chime', content_id: 'default', account_ref: null };
      const roomOptions = [...data.rooms];
      const selectedRoom = alarm && roomOptions.find((room) => room.key === alarm.room || room.udn === alarm.room);
      if (alarm && !selectedRoom) roomOptions.push({ key: alarm.room, name: `Unavailable room (${alarm.room})`, shown: true });
      const sourceOptions = [{ title: 'Chime', media_ref: { provider: 'chime', content_id: 'default', account_ref: null } }, ...sources];
      if (!sourceOptions.some((source) => JSON.stringify(source.media_ref) === JSON.stringify(selected))) sourceOptions.push({ title: alarm.title, media_ref: selected });
      const recurrence = alarm?.recurrence || 'DAILY';
      const repeats = Object.entries(RECURRENCE);
      if (!RECURRENCE[recurrence]) repeats.push([recurrence, `Custom: ${ALARM_DAYS.filter(([d]) => alarmDays(recurrence).includes(d)).map(([, , name]) => name.slice(0, 3)).join(' ') || recurrence}`]);
      const minutes = Math.round((alarm?.duration || 3600) / 60);
      const lengths = [15, 30, 45, 60, 90, 120, 180, 240];
      if (!lengths.includes(minutes)) lengths.push(minutes), lengths.sort((a, b) => a - b);
      const zones = typeof Intl.supportedValuesOf === 'function' ? Intl.supportedValuesOf('timeZone') : [];
      const volume = alarm?.volume ?? 20;
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="alarm-editor-title"><form class="popup alarm-editor">
        <h2 id="alarm-editor-title">${alarm ? 'Edit' : 'Add'} alarm</h2>
        <label class="field"><span>Time</span><input name="time" type="time" class="time-input" required value="${escapeHtml((alarm?.time || '07:00').slice(0, 5))}"></label>
        <fieldset class="field segmented"><legend>Repeat</legend><div class="segments">${repeats.map(([value, label]) =>
          `<label><input type="radio" name="recurrence" value="${escapeHtml(value)}" ${value === recurrence ? 'checked' : ''}><span>${escapeHtml(label)}</span></label>`).join('')}</div></fieldset>
        <div class="field-pair">
          <label class="field"><span>Room</span><select name="room" required>${roomOptions.map((r) => `<option value="${escapeHtml(r.key)}">${escapeHtml(r.name)}</option>`).join('')}</select></label>
          <label class="field"><span>Play</span><select name="source">${sourceOptions.map((source, i) => `<option value="${i}">${escapeHtml(source.title)}</option>`).join('')}</select></label>
        </div>
        <label class="field"><span>Volume <output class="volume-value">${volume}</output></span><input name="volume" type="range" min="0" max="100" step="1" value="${volume}" class="level-range"></label>
        <div class="field-pair">
          <label class="field"><span>Plays for</span><select name="duration">${lengths.map((m) => `<option value="${m}" ${m === minutes ? 'selected' : ''}>${minutesText(m * 60)}</option>`).join('')}</select></label>
          <label class="field"><span>Timezone</span><input name="timezone" required list="alarm-zones" autocomplete="off" spellcheck="false" value="${escapeHtml(alarm?.timezone || localZone)}">
            <datalist id="alarm-zones">${zones.map((zone) => `<option value="${escapeHtml(zone)}"></option>`).join('')}</datalist></label>
        </div>
        <div class="settings-list">
          ${settingCheck('enabled', 'Alarm on', alarm?.enabled !== false)}
          ${settingCheck('include_linked', 'Include linked rooms', alarm?.include_linked, 'Rooms grouped with this one when the alarm goes off')}
        </div>
        <p class="error" role="alert"></p><div class="picker-actions"><button type="button" class="inline-button quiet" data-close>Cancel</button><button class="inline-button" type="submit">Save alarm</button></div></form></div>`);
      const form = $('form', overlay), modal = openModal(overlay, trigger, form.elements.time);
      if (alarm) form.elements.room.value = selectedRoom?.key || alarm.room;
      form.elements.source.value = String(sourceOptions.findIndex((source) => JSON.stringify(source.media_ref) === JSON.stringify(selected)));
      const range = form.elements.volume;
      const paint = () => { $('.volume-value', form).textContent = range.value; range.style.setProperty('--fill', `${range.value}%`); };
      range.addEventListener('input', paint); paint();
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        try {
          await api('PUT', '/api/alarms', { ...(alarm || {}), time: form.elements.time.value, timezone: form.elements.timezone.value.trim(),
            recurrence: form.elements.recurrence.value, room: form.elements.room.value, volume: Number(range.value),
            duration: Number(form.elements.duration.value) * 60, enabled: form.elements.enabled.checked,
            include_linked: form.elements.include_linked.checked, media_ref: sourceOptions[Number(form.elements.source.value)].media_ref });
          modal.close(); await refresh();
        } catch (e) { $('.error', form).textContent = e.message; }
      });
    };

    if (!data.alarms.length) list.append(el('<p class="empty">No alarms yet. Add one with Add alarm; save a Radio station to Favorites to wake up to it.</p>'));
    for (const alarm of data.alarms) {
      const days = alarmDays(alarm.recurrence);
      const repeat = RECURRENCE[alarm.recurrence] || 'Custom days';
      const meta = [roomName(alarm.room), `${minutesText(alarm.duration || 3600)}`];
      if (alarm.timezone && alarm.timezone !== localZone) meta.push(alarm.timezone.replace(/_/g, ' '));
      if (alarm.last_run) meta.push(`Last: ${alarm.last_run.status}`);
      const card = el(`<article class="popup alarm ${alarm.enabled ? '' : 'off'}">
        <div class="alarm-clock">
          <span class="alarm-time">${escapeHtml(alarm.time.slice(0, 5))}</span>
          ${alarm.recurrence === 'ONCE' ? '<span class="alarm-once">Once</span>'
            : `<span class="alarm-days" role="img" aria-label="${escapeHtml(repeat)}">${ALARM_DAYS.map(([day, letter]) => `<i class="${days.includes(day) ? 'on' : ''}">${letter}</i>`).join('')}</span>`}
        </div>
        <div class="alarm-what"><div class="main"></div>
          <div class="sub">${escapeHtml(meta.join(' · '))}</div>
          <div class="alarm-volume">${levelBar(Math.round((alarm.volume ?? 0) / 10), 10, `Volume ${alarm.volume}`)}<span class="meta">${escapeHtml(alarm.volume ?? '')}</span></div></div>
        <div class="alarm-tools">
          <label class="check" title="${alarm.enabled ? 'Turn off' : 'Turn on'}"><input type="checkbox" class="alarm-enabled" ${alarm.enabled ? 'checked' : ''} aria-label="Alarm at ${escapeHtml(alarm.time.slice(0, 5))} on"><span class="box">${ICONS.tick}</span></label>
          ${mini('pencil', 'Edit alarm', 'edit')}${mini('trash', 'Delete alarm', 'delete')}
        </div>
        <div class="alarm-now"><span class="meta">If it is sounding now</span>
          <button type="button" class="inline-button quiet small" data-snooze>Snooze</button><button type="button" class="inline-button quiet small" data-stop>Stop</button></div>
      </article>`);
      $('.alarm-what .main', card).textContent = alarm.title;
      $('.edit', card).addEventListener('click', (event) => editor(alarm, event.currentTarget));
      $('.alarm-enabled', card).addEventListener('change', async (event) => {
        const input = event.currentTarget;
        input.disabled = true;
        try { await api('PUT', '/api/alarms', { ...alarm, enabled: input.checked }); await refresh(); }
        catch (e) { input.checked = !input.checked; input.disabled = false; toast('Alarm not changed', e.message, true); }
      });
      for (const [selector, action] of [['[data-snooze]', 'snooze'], ['[data-stop]', 'stop'], ['.delete', 'delete']]) $(selector, card).addEventListener('click', async () => {
        if (action === 'delete' && !confirm(`Delete the ${alarm.time.slice(0, 5)} alarm?`)) return;
        try { await api('POST', '/api/alarms/' + action, { id: alarm.id, room: alarm.room }); await refresh(); }
        catch (e) { toast('Alarm not changed', e.message, true); }
      });
      list.append(card);
    }
    softkeys([{ label: 'Refresh', run: refresh }, null, { label: 'Add alarm', run: () => editor() }]);
  },
};

// Password ---------------------------------------------------------------------------------
views.password = {
  title: 'Password',
  async open(main) {
    main.innerHTML = '';
    const form = el(`<form class="popup"><h3>Change the bridge password</h3>
      <p>Everyone who opens this page needs it. Other browsers stay signed in until they sign out.</p>
      <label class="field"><span>Current password</span><input type="password" id="pw-current" autocomplete="current-password" required></label>
      <label class="field"><span>New password</span><input type="password" id="pw-new" autocomplete="new-password" minlength="8" required><span class="hint">At least 8 characters</span></label>
      <label class="field"><span>Type it again</span><input type="password" id="pw-confirm" autocomplete="new-password" required></label>
      <p class="error" id="pw-error" role="alert"></p><button type="submit" hidden></button></form>`);
    main.append(form);
    const change = async () => {
      const error = $('#pw-error', form);
      if ($('#pw-new', form).value !== $('#pw-confirm', form).value) { error.textContent = 'The two new passwords are different.'; return; }
      try {
        const result = await api('POST', '/api/password', { current: $('#pw-current', form).value, password: $('#pw-new', form).value });
        form.reset(); error.textContent = ''; toast('Password changed', result.message);
      } catch (e) { error.textContent = e.message; }
    };
    form.addEventListener('submit', (event) => { event.preventDefault(); change(); });
    softkeys([{ label: 'Sign out', run: async () => { await api('POST', '/api/logout'); showGate(false); } }, null,
      { label: 'Change password', run: change }]);

    const tokens = el(`<section class="popup">
      <h3>Controller API tokens</h3>
      <p>Each token can control any paired CR100. <a href="/swagger/" target="_blank" rel="noopener">Open Swagger docs</a></p>
      <form class="inline-field">
        <label class="field"><span>Token name</span><input name="name" required maxlength="64" placeholder="Home Assistant"></label>
        <button type="submit" class="inline-button">Create token</button>
      </form>
      <label class="field new-controller-token" hidden><span>Copy this token now. It is shown once.</span>
        <input type="text" readonly spellcheck="false" autocomplete="off"></label>
      <p class="error token-error" role="alert"></p>
      <div class="settings-list token-list"></div></section>`);
    main.append(tokens);
    const tokenForm = $('form', tokens);
    const tokenList = $('.token-list', tokens);
    const tokenError = $('.token-error', tokens);
    const created = $('.new-controller-token', tokens);
    const secret = $('input', created);
    const refreshTokens = async () => {
      const data = await api('GET', '/api/controller-api-tokens');
      tokenList.replaceChildren();
      if (!data.tokens.length) tokenList.append(el('<p class="hint">No controller API tokens.</p>'));
      for (const token of data.tokens) {
        const row = el(`<div class="setting"><span class="what"><span class="main"></span></span>
          <button type="button" class="inline-button quiet">Revoke</button></div>`);
        $('.main', row).textContent = token.name;
        const revoke = $('button', row);
        revoke.setAttribute('aria-label', `Revoke ${token.name}`);
        revoke.addEventListener('click', async () => {
          if (!confirm(`Revoke the API token “${token.name}”?`)) return;
          tokenError.textContent = '';
          revoke.disabled = true;
          try {
            await api('POST', `/api/controller-api-tokens/${token.id}/revoke`, {});
            if (created.dataset.id === token.id) {
              created.hidden = true;
              secret.value = '';
            }
            await refreshTokens();
          } catch (error) {
            tokenError.textContent = error.message;
            revoke.disabled = false;
          }
        });
        tokenList.append(row);
      }
    };
    tokenForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const create = $('button', tokenForm);
      create.disabled = true;
      tokenError.textContent = '';
      try {
        const { id, token } = await api('POST', '/api/controller-api-tokens', { name: tokenForm.elements.name.value });
        tokenForm.reset();
        created.dataset.id = id;
        created.hidden = false;
        secret.value = token;
        secret.focus();
        secret.select();
        await refreshTokens();
      } catch (error) {
        tokenError.textContent = error.message;
      } finally {
        create.disabled = false;
      }
    });
    await refreshTokens().catch((error) => { tokenError.textContent = error.message; });
  },
};

// --- status badges --------------------------------------------------------------------------
async function refreshStatus() {
  try {
    const o = await api('GET', '/api/overview');
    const lit = { connected: 4, dozing: 3, searching: 1 }[o.controller] ?? 0;
    const controllerText = { connected: 'Controller radio connected', dozing: 'Controller radio connected, idle',
      searching: 'Controller reconnecting', away: 'Controller asleep or out of range',
      'radio-off': 'Bridge radio is not running', none: 'No controller paired' }[o.controller];
    const badge = $('#badge-controller');
    badge.innerHTML = bars(lit, o.controller === 'searching' ? '#f3c24a' : '#a6d84a');
    badge.setAttribute('aria-label', controllerText); badge.title = controllerText;
    const source = $('#badge-source');
    source.innerHTML = houseBadge(o.speaker_health ? o.speaker_health.healthy : o.source_connected);
    const sourceText = o.speaker_health?.message || (o.source === 'Sonos' ? (o.source_connected ? 'Sonos speakers answering' : 'Sonos speakers not answering')
      : (o.source_connected ? 'Home Assistant connected' : 'Home Assistant not connected'));
    source.setAttribute('aria-label', sourceText); source.title = sourceText;
    $('#status').hidden = false;
  } catch { /* the next refresh tries again */ }
}

// --- start ----------------------------------------------------------------------------------
document.querySelectorAll('.menu .icon').forEach((span) => { span.innerHTML = ICONS[span.dataset.icon]; });
window.addEventListener('hashchange', () => show(location.hash.slice(1)));

let statusTimer = null;
function start() {
  $('#app').hidden = false;
  show(location.hash.slice(1) || 'speakers');
  refreshStatus();
  clearInterval(statusTimer);
  statusTimer = setInterval(() => { if (!document.hidden) refreshStatus(); }, 15000);
}

(async () => {
  try {
    const session = await api('GET', '/api/session');
    if (session.authenticated) start(); else showGate(!session.password_set);
  } catch (error) {
    showGate(false);
    $('#gate-error').textContent = error.message;
  }
})();
