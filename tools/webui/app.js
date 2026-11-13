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
  tick: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  pencil: `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 13l1-3.5 7-7 2.5 2.5-7 7z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>`,
};

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

function openModal(overlay, trigger, initialFocus) {
  const close = ({ restoreFocus = true } = {}) => {
    overlay.remove();
    document.removeEventListener('keydown', onKey, true);
    if (restoreFocus && trigger.isConnected) trigger.focus();
  };
  const onKey = (event) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    if (event.key === 'Tab') {
      const controls = [...overlay.querySelectorAll('button, input, select, textarea, a[href], [tabindex]')]
        .filter((item) => !item.disabled && item.tabIndex >= 0);
      if (!controls.length) { event.preventDefault(); return; }
      const at = controls.indexOf(document.activeElement);
      if (event.shiftKey && at <= 0) { event.preventDefault(); controls[controls.length - 1].focus(); }
      else if (!event.shiftKey && (at === -1 || at === controls.length - 1)) { event.preventDefault(); controls[0].focus(); }
    }
  };
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay || event.target.closest('[data-close]')) close();
  });
  document.addEventListener('keydown', onKey, true);
  document.body.append(overlay);
  initialFocus?.focus();
  return { close };
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
        ? 'Available Sonos speakers on this network appear automatically. When you switch networks, the controller follows the speakers found there. Hidden speakers stay hidden when they return; names and icons are remembered.'
        : 'These are the rooms the controller lists, sorted by name as it always sorts them. Switch a speaker off to hide it from the controller; nothing changes on the speaker itself.'}</p>`));
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
      const panel = el(`<div class="popup"><h3>Music library</h3>
        <p>Favourites, Sonos playlists and your music library are the same on every Sonos speaker. Pick which one the controller reads them from${data.source === 'sonos' ? '.' : '; it matters for speakers that are not Sonos.'}</p>
        <label class="field"><span>Read the Music menu from</span><select id="library"></select></label></div>`);
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
        const rename = el(`<button type="button" class="mini" title="Rename" aria-label="Rename ${escapeHtml(row.name)}">${ICONS.pencil}</button>`);
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
      const tile = $('.tile', node);
      const input = el(`<input type="text" maxlength="30" aria-label="Room name for ${escapeHtml(row.source_name || row.name)}">`);
      input.value = row.name;
      $('.name', tile).replaceWith(input);
      input.focus(); input.select();
      const finish = (keep) => {
        if (keep) {
          const name = input.value.trim();
          row.custom_name = !name || name === row.source_name ? '' : name;
          row.name = row.custom_name || row.source_name || row.name;
          if (!row.custom_icon) row.icon = guessIcon(row.name);
        }
        render();
        focusRow(row, '[aria-label^="Rename"]');
      };
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); finish(true); }
        if (event.key === 'Escape') finish(false);
      });
      input.addEventListener('blur', () => finish(true), { once: true });
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
};

views.controllers = {
  title: 'Controllers',
  async open(main) {
    const firmwarePanels = new Map();
    const updateBridgeStatus = (data) => {
      const node = $('.bridge-update-status', main);
      if (!node) return;
      const state = data.updates?.bridge || {};
      $('.bridge-update-message', node).textContent = state.message || 'Bridge updates currently run a demonstration through Online Updates on the controller. Automatic bridge installation is not available yet.';
      $('.hint', node).textContent = state.status
        ? `Status: ${state.status}${state.phase ? ` · ${state.phase}` : ''}${Number.isFinite(state.percent) ? ` · ${state.percent}%` : ''}`
        : 'A staged controller install takes priority over the bridge update demonstration.';
      const error = $('.updates-refresh-error', node);
      error.textContent = data.updates_error || state.last_error || '';
      error.hidden = !error.textContent;
    };
    const render = (data) => {
      main.innerHTML = '';
      main.append(el(`<p class="lede">CR100 controllers paired with this bridge. A controller can sleep while idle. Resume reconnect attempts before using a controller whose attempts are paused.</p>`));
      if (!data.radio_running) {
        main.append(el(`<div class="popup"><h3>The bridge radio is not running</h3><p>Controllers cannot connect until the bridge is started on the Pi.</p></div>`));
      }
      if (data.speaker_health && !data.speaker_health.healthy) {
        main.append(el(`<div class="popup" role="status"><h3>Controller speakers need attention</h3><p>${escapeHtml(data.speaker_health.message)}</p><p>A working radio connection does not mean the controller’s speakers are ready.</p></div>`));
      }
      data.controllers.forEach((c) => main.append(card(c)));
      if (!data.controllers.length) main.append(el(`<p class="empty">No controller is paired yet.</p>`));
      main.append(el(`<h2 class="group-title">Bridge updates</h2>`), el(`<div class="popup bridge-update-status" role="status">
        <p class="bridge-update-message"></p><p class="hint"></p><p class="updates-refresh-error error" hidden></p>
      </div>`));
      updateBridgeStatus(data);
      main.append(el(`<h2 class="group-title">Pairing</h2>`), el(`<div class="popup">
        <dl class="facts inset">
          <dt>Household</dt><dd>${escapeHtml(data.household || 'Not paired')}</dd>
          <dt>Radio channel</dt><dd>${escapeHtml(data.channel ?? '?')}</dd>
        </dl>
        <p class="gap-above">${data.pairing?.running ? 'Waiting for a CR100. Put the controller in its add-to-household setup mode within three minutes.' :
          data.pairing?.failed ? 'The pairing attempt failed. Check the Pi’s service logs, then try again.' :
          'Add a CR100 to this household. Existing controllers and their settings are kept. The bridge pauses during pairing and reconnects afterward.'}</p>
        <button type="button" class="inline-button" id="pair-controller" ${!data.pairing?.available || data.pairing?.running ? 'disabled' : ''}>${data.pairing?.running ? 'Pairing…' : data.controllers.length ? 'Add controller' : 'Pair controller'}</button>
      </div>`));
      $('#pair-controller', main).addEventListener('click', async (event) => {
        event.currentTarget.disabled = true;
        try {
          await api('POST', '/api/controllers/pair', {});
          toast('Pairing started', 'Put the CR100 in its add-to-household setup mode.');
          await refresh();
        } catch (error) {
          toast('Pairing could not start', error.message, true);
          await refresh();
        }
      });
    };

    const card = (c) => {
      const dot = { connected: 'good', dozing: 'idle', searching: 'wait' }[c.status] || 'bad';
      const signal = c.signal != null && ['connected', 'dozing'].includes(c.status)
        ? `<span class="level" role="img" aria-label="Signal ${c.signal} of 4">${[0, 1, 2, 3].map((i) => `<i class="${i < c.signal ? 'on' : ''}"></i>`).join('')}</span>`
        : 'Not available while it is asleep';
      const facts = [
        ['Last heard', ago(c.last_heard_s)],
        ['Signal', signal],
        ['Link drops in the last hour', c.drops_last_hour ?? 'Not known yet'],
        ['Address', escapeHtml(c.ip || 'Not known yet')],
        ['Firmware', escapeHtml(c.firmware || 'Not known yet')],
        ['Running for', c.uptime_s != null ? duration(c.uptime_s) : 'Not known yet'],
        ['Wi-Fi address', escapeHtml(c.mac)],
      ];
      const node = el(`<section class="controller">
        <div class="line">
          <div class="tile">${ICONS.controller}<span class="name">${escapeHtml(c.name)}</span></div>
          <div class="row"><div class="what"><div class="main"><span class="dot ${dot}"></span>${escapeHtml(c.status_label)}</div>
            <div class="sub">${c.details_at ? `Details from ${clock(c.details_at)}` : 'Details appear once it is connected'}</div></div>
            <div class="tools"><button type="button" class="mini rename-controller" aria-label="Rename ${escapeHtml(c.name)}">${ICONS.pencil}</button></div></div>
        </div>
        <div class="popup">
          <dl class="facts inset">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
          <div class="controller-component-actions"><button type="button" class="inline-button quiet choose-components">Choose components</button>
            <span>${c.visible_components == null ? 'Uses household selection' : `${c.visible_components.length} component${c.visible_components.length === 1 ? '' : 's'} selected`}</span></div>
          <div class="controller-component-actions"><button type="button" class="inline-button quiet controller-reconnect">${c.auto_reconnect === false ? 'Resume reconnect attempts' : 'Pause reconnect attempts'}</button>
            <span>${c.auto_reconnect === false ? 'Reconnect attempts paused' : 'Reconnect attempts enabled'}</span></div>
          <p class="hint">Reconnect attempts can stay enabled while this controller is switched off. Pause only for troubleshooting; pairing and chosen components are kept.</p>
          ${c.events && c.events.length ? `<h3 class="gap-above">Recent link events</h3><ul class="timeline inset">${c.events.slice().reverse().map((e) =>
            `<li><time>${clock(e.time)}</time><span>${escapeHtml(EVENT_TEXT[e.event] || e.event)}</span></li>`).join('')}</ul>` : ''}
        </div></section>`);
      $('.rename-controller', node).addEventListener('click', async () => {
        const name = prompt('Name for this controller', c.name);
        if (name == null || !name.trim() || name.trim() === c.name) return;
        try { await api('PUT', `/api/controllers/${c.mac}`, { name: name.trim() }); toast('Renamed', `This controller is now ${name.trim()}.`); refresh(); }
        catch (error) { toast('Not renamed', error.message, true); }
      });
      $('.choose-components', node).addEventListener('click', async (event) => {
        const trigger = event.currentTarget;
        trigger.disabled = true;
        try { chooseComponents(await api('GET', `/api/controllers/${c.mac}/components`), trigger); }
        catch (error) { toast('Components unavailable', error.message, true); }
        finally { trigger.disabled = false; }
      });
      $('.controller-reconnect', node).addEventListener('click', async (event) => {
        const trigger = event.currentTarget;
        trigger.disabled = true;
        try {
          const result = await api('PUT', `/api/controllers/${c.mac}/reconnect`, { enabled: c.auto_reconnect === false });
          toast(result.auto_reconnect ? 'Reconnect attempts resumed' : 'Reconnect attempts paused', result.message);
          await refresh();
        } catch (error) { toast('Reconnect setting not saved', error.message, true); }
        finally { trigger.disabled = false; }
      });
      if (!firmwarePanels.has(c.mac)) firmwarePanels.set(c.mac, firmwarePanel(c));
      const firmware = firmwarePanels.get(c.mac);
      $('.popup', node).append(firmware.node);
      firmware.update(c.update, c.firmware);
      return node;
    };

    const firmwarePanel = (controller) => {
      const node = el(`<form class="controller-firmware"><h3>Controller firmware</h3>
        <dl class="facts inset firmware-facts"></dl>
        <label class="field"><span>Firmware file (.upd)</span><input type="file" class="firmware-file" accept=".upd,application/octet-stream"></label>
        <p class="hint">Upload a CR100 firmware file, then choose Install. The target version is read from the firmware package.</p>
        <p class="hint firmware-version-warning" role="status" hidden></p>
        <div class="firmware-signature" hidden><p class="hint firmware-signature-warning" role="alert"></p>
          <label class="check"><input type="checkbox" class="firmware-continue"><span class="box">${ICONS.tick}</span><span>Continue anyway — I trust this firmware</span></label></div>
        <label class="check"><input type="checkbox" class="firmware-force"><span class="box">${ICONS.tick}</span><span>Force update prompt</span></label>
        <p class="hint firmware-guidance">Install from Online Updates on the controller.</p>
        <div class="firmware-actions"><button type="submit" class="inline-button quiet firmware-upload" disabled>Upload</button>
          <button type="button" class="inline-button firmware-install" disabled>Install</button>
          <button type="button" class="inline-button quiet firmware-cancel" disabled>Cancel staged update</button></div>
        <progress class="firmware-progress" max="100" value="0" hidden aria-label="Firmware upload progress"></progress>
        <p class="firmware-status" role="status" aria-live="polite"></p>
        <p class="firmware-error error" role="alert"></p>
      </form>`);
      const input = $('.firmware-file', node), force = $('.firmware-force', node);
      const proceed = $('.firmware-continue', node);
      let reviewedSha256;
      const upload = $('.firmware-upload', node), install = $('.firmware-install', node), cancel = $('.firmware-cancel', node);
      const progress = $('.firmware-progress', node), status = $('.firmware-status', node), error = $('.firmware-error', node);
      let firmware = {}, currentVersion = controller.firmware, busy = false, notice = '';
      const endpoint = `/api/controllers/${controller.mac}/firmware`;
      const update = function (fresh, current) {
        if (fresh) firmware = fresh;
        if (arguments.length > 1) currentVersion = current || '';
        if (reviewedSha256 !== firmware.sha256) { proceed.checked = false; reviewedSha256 = firmware.sha256; }
        const unverified = firmware.signature_valid === false;
        $('.firmware-signature', node).hidden = !unverified;
        $('.firmware-signature-warning', node).textContent = firmware.signature_warning || '';
        proceed.disabled = busy || Boolean(firmware.armed);
        const state = firmware.status || 'none';
        const labels = { none: 'No firmware staged', staged: 'Uploaded and ready', ready: 'Uploaded and ready',
          armed: 'Ready to install from the controller', downloading: 'Downloading to the controller',
          installing: 'Controller installation started', started: 'Controller installation started',
          completed: 'Controller reported the target version', complete: 'Controller reported the target version',
          failed: 'Update failed', expired: 'Staged update expired', cancelled: 'Staged update cancelled' };
        const inProgress = ['downloading', 'installing', 'started'].includes(state);
        const terminal = ['none', 'completed', 'complete', 'cancelled', 'expired', 'failed'].includes(state);
        const reportedVersion = currentVersion || '';
        const normalizedVersion = { '4.3': '24.1-75101', '4.3.2': '25.2-50130' }[reportedVersion] || reportedVersion;
        const sameVersion = Boolean(firmware.version) && normalizedVersion === firmware.version;
        const versionWarning = $('.firmware-version-warning', node);
        versionWarning.hidden = !sameVersion;
        versionWarning.textContent = sameVersion
          ? 'This controller already runs the selected version. The stock update flow may skip reinstalling it. You can still choose Install.' : '';
        const forceAllowed = reportedVersion === '4.3' || /^24\.1-\d+$/.test(reportedVersion);
        const internalVersion = /^(\d+)\.(\d+)-\d+$/.exec(reportedVersion);
        const retirementUI = reportedVersion === '4.3.2' || (internalVersion &&
          (Number(internalVersion[1]) > 25 || (Number(internalVersion[1]) === 25 && Number(internalVersion[2]) >= 2)));
        if (!forceAllowed) force.checked = false;
        $('.firmware-facts', node).innerHTML = `<dt>Current version</dt><dd>${escapeHtml(reportedVersion || 'Not known yet')}</dd>
          <dt>Target version</dt><dd>${escapeHtml(firmware.version || 'None')}</dd>
          <dt>Update status</dt><dd>${escapeHtml(labels[state] || state)}</dd>`;
        if (!busy) {
          const forceActive = firmware.force_active ?? firmware.force_prompt;
          status.textContent = firmware.armed ? (forceActive && forceAllowed
            ? 'Ready. The controller will show an update prompt when it reconnects.'
            : firmware.force_requested
              ? 'The update prompt is no longer active. Open Online Updates on the controller to retry.'
              : 'Ready. Open System Settings → Online Updates on the controller to install.')
            : firmware.last_error || notice;
        }
        input.disabled = busy || inProgress;
        force.disabled = busy || inProgress || firmware.armed || !forceAllowed;
        upload.disabled = busy || inProgress || !input.files.length;
        install.disabled = busy || inProgress || terminal || Boolean(firmware.armed) || !firmware.version || (unverified && !proceed.checked);
        cancel.disabled = busy || inProgress || terminal;
        $('.firmware-guidance', node).textContent = retirementUI
          ? 'Firmware 4.3.2 must use System Settings → Online Updates; its compatibility notice hides the updater.'
          : !forceAllowed
            ? 'Force prompts require known firmware 4.3. Install from Online Updates on the controller.'
            : force.checked
              ? 'Ask this controller to update when it reconnects. The controller’s own firmware checks still apply.'
              : 'Install from Online Updates on the controller.';
      };
      const perform = async (message, operation) => {
        busy = true; notice = ''; error.textContent = ''; status.textContent = message; update();
        try {
          const result = await operation();
          firmware = result.firmware;
          currentVersion = result.current_version || '';
          return true;
        } catch (failure) {
          error.textContent = failure.message;
          return false;
        } finally {
          busy = false; progress.hidden = true; update();
        }
      };
      input.addEventListener('change', () => { error.textContent = ''; update(); });
      force.addEventListener('change', () => update());
      proceed.addEventListener('change', () => update());
      node.addEventListener('submit', async (event) => {
        event.preventDefault();
        const file = input.files[0];
        if (!file || busy) return;
        if (file.size > 16 * 1024 * 1024) { error.textContent = 'Firmware files must be at most 16 MiB.'; return; }
        progress.hidden = false; progress.value = 0;
        const ok = await perform('Uploading and verifying firmware…', () => uploadControllerFirmware(controller.mac, file, (percent) => {
          progress.value = percent;
          status.textContent = percent === 100 ? 'Verifying the firmware signature and version…' : `Uploading firmware… ${percent}%`;
        }));
        if (ok) { input.value = ''; force.checked = false; notice = `Firmware ${firmware.version} is ready. Choose Install to stage it for this controller.`; update(); }
      });
      install.addEventListener('click', () => perform('Preparing the controller update…', () => api('POST', endpoint + '/install', { force: force.checked, sha256: firmware.sha256, allow_unverified: proceed.checked })));
      cancel.addEventListener('click', async () => {
        if (await perform('Cancelling the staged update…', () => api('POST', endpoint + '/cancel', {}))) { force.checked = false; update(); }
      });
      return { node, update, isBusy: () => busy };
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
      if (passive && (document.activeElement?.closest('.controller-firmware') || [...firmwarePanels.values()].some((panel) => panel.isBusy()))) {
        for (const controller of data.controllers) firmwarePanels.get(controller.mac)?.update(controller.update, controller.firmware);
        updateBridgeStatus(data);
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
async function spotifyMenuEditor(main, onDirty, onConnectionStatus = () => {}) {
  const box = el(`<section class="popup spotify-menu-editor"><h3>Menu and feature locks</h3>
    <p>Each controller has its own Spotify menu. Locked entries stay visible and show an administrator message when opened. A lock includes everything inside that entry; the same music can still be reached through another unlocked entry.</p>
    <p class="hint">Search stays first. Its name and position are fixed by the controller; its lock can be changed here.</p>
    <label class="field"><span>Controller</span><select class="sp-controller" aria-label="Controller to edit"></select></label>
    <div class="sp-menu-list" aria-label="Spotify menu entries"></div>
    <p class="hint">Changes block new requests after saving. Lists already cached on a controller may remain until its next refresh.</p>
    <div class="spotify-menu-actions"><button type="button" class="inline-button sp-menu-save" disabled>Save this menu</button><span class="sp-menu-status" role="status"></span></div>
    <div class="sp-add-controls"><h3>Add a shortcut</h3>
      <label class="field"><span>Spotify playlist</span><select class="sp-playlist" aria-label="Playlist to pin"><option value="">Loading playlists…</option></select></label>
      <button type="button" class="inline-button quiet sp-add-playlist">Add playlist</button>
      <label class="field gap-above"><span>Or paste a Spotify link or URI</span><input class="sp-link" type="text" placeholder="https://open.spotify.com/album/…" autocomplete="off" spellcheck="false"><span class="hint">Playlists, albums, singles, and tracks can be pinned. Singles use Spotify’s album links.</span></label>
      <button type="button" class="inline-button quiet sp-add-link">Look up and add</button>
      <p class="error sp-add-error" role="alert"></p>
    </div></section>`);
  const folders = el(`<section class="popup spotify-folder-panel"><h3>Spotify folders</h3>
    <p>Pin a real Spotify folder to mirror its playlists and nested folders on this controller. Names and ordering inside the folder follow Spotify; edits here only change your CR100 menu.</p>
    <p class="sp-folder-status" role="status">Loading folder status…</p>
    <div class="spotify-menu-actions"><button type="button" class="inline-button sp-connect-folders">Connect Spotify folders</button><button type="button" class="inline-button quiet sp-refresh-folders">Refresh folders</button></div>
    <p class="hint">Folders refresh automatically. If Spotify is unavailable, the last complete folder tree is kept.</p>
    <div class="sp-folder-tree" aria-label="Spotify folder tree"></div>
    <div class="sp-folder-signin gap-above" hidden><h3>Connect Spotify folders and playback</h3>
      <p>Sign in to the same Spotify account used above. This separate connection uses Spotify’s “streaming” permission for folders and for bridge-controlled playback when Autoplay is enabled. It does not change Spotify’s account-wide Autoplay setting.</p>
      <ol class="steps"><li>Approve access on the Spotify page.</li><li>Spotify opens a local address beginning with <strong>127.0.0.1:8898/login</strong>. An error page is expected.</li><li>Copy the whole address and paste it below.</li></ol>
      <label class="field"><span>Address after approving</span><input class="sp-folder-address" type="url" autocomplete="off" placeholder="http://127.0.0.1:8898/login?code=…"></label>
      <button type="button" class="inline-button sp-folder-finish">Finish folder sign-in</button>
    </div><p class="error sp-folder-error" role="alert"></p></section>`);
  main.append(el('<h2 class="group-title">Controller menus</h2>'), box, folders);
  const select = $('.sp-controller', box), list = $('.sp-menu-list', box), saveButton = $('.sp-menu-save', box);
  const addControls = $('.sp-add-controls', box), menuStatus = $('.sp-menu-status', box);
  let items = [], baseline = '[]', activeMac = '', generation = 0, library = { folders: [], playlists: [] };
  let folderTreeSignature = null, folderRequest = 0, folderAction = false;
  const editorItems = rows => {
    const search = rows.find(item => item.id === 'search');
    return [{ ...search, label: 'Search' }, ...rows.filter(item => item.id !== 'search')];
  };
  const changed = () => {
    const value = JSON.stringify(items) !== baseline;
    saveButton.disabled = !activeMac || !value;
    menuStatus.textContent = value ? 'Unsaved changes' : '';
    if (box.isConnected) onDirty(value);
  };
  const add = (kind, target, name) => {
    if (!activeMac) return;
    if (items.length >= 64) { $('.sp-add-error', box).textContent = 'Keep this menu to 64 entries.'; return; }
    items.push({ id: `entry_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`,
      kind, target, label: String(name || 'Spotify item').slice(0, 80), locked: false });
    renderItems(); changed();
    menuStatus.textContent = 'Shortcut added. Save this menu to apply it.';
  };
  const renderItems = () => {
    list.replaceChildren();
    items.forEach((item, index) => {
      const nativeSearch = item.id === 'search';
      const type = item.kind === 'builtin' ? 'Built-in section' : item.kind === 'folder' ? 'Mirrored Spotify folder' : 'Spotify shortcut';
      const entry = el(`<div class="spotify-menu-entry ${item.locked ? 'is-locked' : ''}">
        <span class="spotify-menu-position" aria-hidden="true">${index + 1}</span>
        <label class="field spotify-menu-label"><span>${escapeHtml(type)}</span><input type="text" maxlength="80" value="${escapeHtml(item.label)}" aria-label="Label for menu entry ${index + 1}" ${nativeSearch ? 'disabled' : ''}></label>
        <label class="check spotify-menu-lock"><input type="checkbox" ${item.locked ? 'checked' : ''}><span class="box">${ICONS.tick}</span><span>Locked</span></label>
        <div class="spotify-menu-move"><button type="button" class="inline-button quiet sp-up" aria-label="Move entry ${index + 1} up" ${index <= 1 ? 'disabled' : ''}>↑</button><button type="button" class="inline-button quiet sp-down" aria-label="Move entry ${index + 1} down" ${nativeSearch || index === items.length - 1 ? 'disabled' : ''}>↓</button>${item.kind === 'builtin' ? '' : '<button type="button" class="inline-button quiet sp-remove">Remove</button>'}</div>
        <div class="spotify-menu-target">${escapeHtml(nativeSearch ? 'Native Search: name and position fixed by the controller' : item.kind === 'builtin' ? item.target : item.kind === 'folder' ? 'Folder: ' + item.target : item.target)}</div></div>`);
      $('input[type=text]', entry).addEventListener('input', event => { item.label = event.target.value; changed(); });
      $('input[type=checkbox]', entry).addEventListener('change', event => {
        item.locked = event.target.checked; entry.classList.toggle('is-locked', item.locked); changed();
      });
      const move = delta => {
        if (nativeSearch || index + delta <= 0) return;
        [items[index], items[index + delta]] = [items[index + delta], items[index]]; renderItems(); changed();
      };
      $('.sp-up', entry).addEventListener('click', () => move(-1));
      $('.sp-down', entry).addEventListener('click', () => move(1));
      $('.sp-remove', entry)?.addEventListener('click', () => { items.splice(index, 1); renderItems(); changed(); });
      list.append(entry);
    });
  };
  const loadController = async mac => {
    const version = ++generation;
    saveButton.disabled = true;
    const result = await api('GET', '/api/spotify/menu/' + encodeURIComponent(mac));
    if (version !== generation) return;
    activeMac = mac; items = editorItems(result.items); baseline = JSON.stringify(items);
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
  const renderFolders = () => {
    const status = library.folder_status || {}, tree = $('.sp-folder-tree', folders);
    onConnectionStatus(status);
    const age = status.updated_at ? ago(Math.max(0, Date.now() / 1000 - status.updated_at)) : 'not yet';
    $('.sp-folder-status', folders).textContent = status.connected
      ? `Connected · last complete refresh: ${age}${status.refreshing ? ' · refreshing…' : ''}${status.stale ? ' · showing the saved tree' : ''}${status.error ? ' · ' + status.error : ''}`
      : 'Connect folder access to browse your Spotify folders.';
    $('.sp-connect-folders', folders).textContent = status.connected ? 'Reconnect folder access' : 'Connect Spotify folders';
    $('.sp-refresh-folders', folders).disabled = !status.connected;
    const signature = JSON.stringify(library.folders || []);
    if (signature === folderTreeSignature) return;
    folderTreeSignature = signature;
    tree.replaceChildren();
    const draw = (nodes, destination, depth = 0) => {
      for (const node of nodes || []) {
        if (node.kind !== 'folder' || depth > 32) continue;
        const branch = el(`<details ${depth === 0 ? 'open' : ''}><summary><span>${escapeHtml(node.name || 'Spotify folder')}</span><button type="button" class="inline-button quiet">Add folder</button></summary><div class="spotify-folder-children"></div></details>`);
        $('summary button', branch).addEventListener('click', event => { event.preventDefault(); add('folder', node.id, node.name); });
        const children = $('.spotify-folder-children', branch);
        for (const child of node.children || []) {
          if (child.kind === 'folder') draw([child], children, depth + 1);
          else if (child.uri) {
            const row = el(`<div class="spotify-folder-leaf"><span>${escapeHtml(child.name || child.uri)}</span><button type="button" class="inline-button quiet">Add shortcut</button></div>`);
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
    else { select.disabled = true; menuStatus.textContent = 'Pair a controller before editing its menu.'; }
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
  return { save, connectPlayback: () => $('.sp-connect-folders', folders).click() };
}

views.spotify = {
  title: 'Spotify',
  async open(main) {
    const data = await api('GET', '/api/spotify');
    let menuDirty = false, menuEditor = null, playbackConnected = !!data.autoplay_connected;
    main.innerHTML = '';
    main.append(el(`<p class="lede">Spotify appears in the controller's Music menu as a music service. The bridge searches and browses Spotify for you; it needs a Spotify Premium account and a Client ID from the Spotify developer dashboard.</p>`));

    const account = data.authorized
      ? el(`<div class="line"><div class="tile">${ICONS.note}<span class="name">${escapeHtml(data.account?.name || 'Signed in')}</span></div>
          <div class="row"><div class="what"><div class="main">Signed in to Spotify</div><div class="sub">${escapeHtml(data.account?.plan ? `Plan: ${data.account.plan}` : 'Account details appear after the next sign-in')}</div></div></div></div>`)
      : el(`<div class="line"><div class="tile off">${ICONS.note}<span class="name">Not signed in</span></div>
          <div class="row"><div class="what"><div class="main">Spotify needs you to sign in</div><div class="sub">${data.client_id ? 'Use Sign in to Spotify below.' : 'Add your Client ID first.'}</div></div></div></div>`);
    main.append(el('<h2 class="group-title">Account</h2>'), account);

    const signIn = el(`<div class="popup" hidden><h3>Sign in to Spotify</h3>
      <ol class="steps">
        <li>Approve access on the Spotify page that just opened.</li>
        <li>Spotify then sends your browser to an address starting with <strong>127.0.0.1</strong>, which shows an error. That is expected.</li>
        <li>Copy that whole address from the address bar and paste it here.</li>
      </ol>
      <label class="field"><span>Address after approving</span><input type="url" id="sp-address" placeholder="http://127.0.0.1:8888/callback?code=…" autocomplete="off"></label>
      <p class="error" id="sp-error" role="alert"></p>
      <button type="button" class="inline-button" id="sp-finish">Finish sign-in</button></div>`);
    main.append(signIn);
    $('#sp-finish', signIn).addEventListener('click', async () => {
      try {
        const result = await api('POST', '/api/spotify/finish', { address: $('#sp-address', signIn).value });
        toast('Signed in', result.message);
        show('spotify');
      } catch (error) { $('#sp-error', signIn).textContent = error.message; }
    });

    const form = el(`<div class="popup"><h3>Music service</h3>
      <label class="check gap-below"><input type="checkbox" id="sp-enabled" ${data.enabled ? 'checked' : ''}><span class="box">${ICONS.tick}</span><span>Show Spotify on the controller</span></label>
      <label class="field"><span>Name on the controller</span><input type="text" id="sp-name" maxlength="24" value="${escapeHtml(data.name)}"></label>
      <label class="field"><span>Client ID</span><input type="text" id="sp-client" maxlength="32" spellcheck="false" autocomplete="off" value="${escapeHtml(data.client_id)}">
        <div class="hint">From developer.spotify.com, under your app's settings. Add ${escapeHtml(data.redirect_uri)} to the app's redirect URIs.</div></label>
    </div>`);
    main.append(el('<h2 class="group-title">On the controller</h2>'), form);

    const playback = el(`<section class="popup"><h3>Autoplay</h3>
      <label class="check gap-below"><input type="checkbox" id="sp-autoplay" ${data.autoplay ? 'checked' : ''}><span class="box">${ICONS.tick}</span><span>Autoplay similar music</span></label>
      <p class="hint">For music started from this bridge, continue with related Spotify tracks after your selection finishes. Recommendations stay outside the Sonos queue. Music you explicitly queue takes priority; after queued music from another source, playback stops.</p>
      <p class="hint">Turning this off lets the current bridge-controlled track finish. Spotify’s global Autoplay setting is unchanged.</p>
      <p class="sp-playback-status" role="status"></p>
      <button type="button" class="inline-button quiet sp-connect-playback" disabled>Connect Spotify playback</button>
    </section>`);
    const renderPlaybackConnection = (status) => {
      if (status) playbackConnected = !!status.connected;
      $('.sp-playback-status', playback).textContent = playbackConnected
        ? 'Spotify playback sign-in is saved. It is shared with Spotify folders.'
        : 'Autoplay needs an additional Spotify connection. Connect playback below using the same account.';
      const button = $('.sp-connect-playback', playback);
      button.textContent = playbackConnected ? 'Reconnect Spotify playback' : 'Connect Spotify playback';
      button.disabled = !menuEditor;
    };
    $('.sp-connect-playback', playback).addEventListener('click', () => menuEditor?.connectPlayback());
    main.append(el('<h2 class="group-title">Playback</h2>'), playback);
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
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    playback.addEventListener('change', update);
    update();
    menuEditor = await spotifyMenuEditor(main, value => { if (current === 'spotify' && form.isConnected) { menuDirty = value; update(); } }, renderPlaybackConnection);
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
        <div class="hint">Create one in Home Assistant under your profile, Security, Long-lived access tokens. The bridge never shows a saved token.</div></label></div>`);
    const sonosForm = el(`<div class="popup"><h3>Sonos</h3>
      <label class="field"><span>Speaker addresses</span><input type="text" id="sonos-hosts" value="${escapeHtml(sonos.hosts.join(', '))}" placeholder="Found automatically" spellcheck="false" autocomplete="off">
        <div class="hint">Leave this empty to find the speakers automatically. If they are on another part of your network, enter the address of one or more of them, like 192.168.1.30.</div></label></div>`);
    const haValues = () => ({ url: $('#ha-url', haForm).value.trim(), token: $('#ha-token', haForm).value.trim() });
    const sonosValues = () => ({ hosts: $('#sonos-hosts', sonosForm).value.trim() });
    const baseline = JSON.stringify([haValues(), sonosValues()]);

    const status = (name) => {
      if (name === 'sonos') {
        if (!sonos.connected) return { dot: 'bad', text: escapeHtml(sonos.error || 'The Sonos speakers are not answering.') };
        const count = sonos.speakers.length;
        return { dot: 'good', text: `Found on this network: ${escapeHtml(sonos.speakers.join(', '))} (${count} speaker${count === 1 ? '' : 's'}).` };
      }
      if (ha.connected) return { dot: 'good', text: `Connected${ha.version ? ` to Home Assistant ${escapeHtml(ha.version)}` : ''}. ${ha.media_players} media players.` };
      if (data.source !== 'ha') return { dot: 'bad', text: 'Not in use. The bridge connects once you choose Home Assistant.' };
      return { dot: 'bad', text: escapeHtml(ha.error || 'Not connected.') };
    };

    const render = () => {
      settings.innerHTML = '';
      const state = status(chosen);
      settings.append(el(`<div class="popup"><div class="state-line"><span class="dot ${state.dot}"></span><span>${state.text}</span></div></div>`));
      if (chosen === data.source && data.speaker_health) {
        settings.append(el(`<div class="popup" role="status"><h3>Selected for the controller</h3><p>${escapeHtml(data.speaker_health.selected.join(', ') || 'None')}.</p><p>${escapeHtml(data.speaker_health.message)}</p><p>Change the selection on the Speakers page.</p></div>`));
      }
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
views.radio = {
  title: 'Radio',
  async open(main) {
    const data = await api('GET', '/api/radio');
    const s = data.settings;
    main.innerHTML = `<p class="lede">Browse TuneIn from the controller's original Radio menu. Podcasts, favorites and album artwork are included.</p>
      <form class="popup" id="radio-settings"><h3>Listening preferences</h3>
        <label class="field"><span>Station order</span><select name="station_order"><option value="popular">Most popular</option><option value="name">By name</option><option value="provider">Directory order</option></select></label>
        <label class="field"><span>Stream quality</span><select name="quality"><option value="auto">Automatic</option><option value="best">Best available</option><option value="low">Low bandwidth</option></select></label>
        <label class="field"><span>Preferred language</span><select name="preferred_language"><option value="">All languages</option></select><small id="radio-language-status" role="status">Loading languages…</small></label>
        ${[['language_filter', 'Filter to preferred language'], ['automatic_reconnect', 'Reconnect automatically'],
          ['history', 'Keep recently played and recent searches'], ['episode_resume', 'Resume podcast episodes'],
          ['show_podcasts', 'Show Podcasts in Radio'], ['show_language', 'Show By Language in Radio'],
          ['cover_lookup', 'Find missing album covers']].map(([key, label]) =>
          `<label class="radio-check"><input type="checkbox" name="${key}" ${s[key] ? 'checked' : ''}> ${label}</label>`).join('')}
        <p>Album artwork is always enabled. When a song cannot be identified, the station or show image is displayed.</p>
        <p class="error" role="alert"></p><button type="submit" hidden>Save</button></form>
      <section class="popup"><h3>Local Radio</h3><p>${escapeHtml(s.location?.label || s.location?.name || 'Location detected automatically')}</p>
        <form id="radio-location"><label class="field"><span>U.S. ZIP code</span><input name="zip" inputmode="numeric" pattern="[0-9]{5}" maxlength="5" required></label>
        <button class="inline-button" type="submit">Set location</button></form>
        <button class="inline-button" type="button" id="radio-city">Pick a city</button></section>
      <section class="popup"><h3>Favorites</h3><div id="radio-favorites"></div></section>
      <section class="popup"><h3>Custom stations</h3><div id="radio-custom"></div><button type="button" class="inline-button" id="radio-add">Add station</button></section>
      <section class="popup"><h3>Radio data</h3><div class="radio-actions">
        <button class="inline-button" data-radio-action="history-clear">Clear history</button>
        <button class="inline-button" id="radio-export">Export favorites and settings</button>
        <label class="inline-button">Import data<input type="file" id="radio-import" accept="application/json,.json" hidden></label>
        <button class="inline-button" data-radio-action="reset">Reset preferences</button>
        <a href="#alarms">Manage global alarms</a></div></section>`;
    const form = $('#radio-settings', main);
    const language = form.elements.preferred_language;
    if (s.preferred_language) language.append(new Option('Saved language', s.preferred_language));
    for (const key of ['station_order', 'quality', 'preferred_language']) form.elements[key].value = s[key] || '';
    api('POST', '/api/radio/language-options', {}).then((result) => {
      if (!language.isConnected) return;
      const selected = language.value;
      language.replaceChildren(new Option('All languages', ''));
      for (const choice of result.languages) language.append(new Option(choice.label, choice.id));
      if (selected && !result.languages.some((choice) => choice.id === selected)) language.append(new Option('Saved language (currently unavailable)', selected));
      language.value = selected;
      $('#radio-language-status', main).textContent = 'Choose a language, then enable the language filter to narrow browsing.';
    }).catch(() => {
      if (language.isConnected) $('#radio-language-status', main).textContent = 'The language list is unavailable. Your saved selection is kept; Refresh Radio retries.';
    });
    const save = async () => {
      const settings = {};
      for (const field of form.elements) if (field.name) settings[field.name] = field.type === 'checkbox' ? field.checked : field.value;
      await api('PUT', '/api/radio', { settings });
      setDirty(false); toast('Saved', 'Radio preferences updated.');
    };
    form.addEventListener('input', () => setDirty(true));
    form.addEventListener('submit', (event) => { event.preventDefault(); save().catch((e) => toast('Not saved', e.message, true)); });
    $('#radio-location', main).addEventListener('submit', async (event) => {
      event.preventDefault();
      try { await api('POST', '/api/radio/location', { zip: event.target.elements.zip.value }); toast('Location updated'); setDirty(false); await show('radio'); }
      catch (e) { toast('Location not changed', e.message, true); }
    });
    const act = async (command, body) => { await api('POST', '/api/radio/' + command, body); setDirty(false); await show('radio'); };
    $('#radio-city', main).addEventListener('click', (event) => {
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="radio-city-title"><div class="popup">
        <h2 id="radio-city-title">Pick a city</h2><p data-path></p>
        <label class="field"><span>Places</span><select data-places size="9" aria-label="Places"></select></label>
        <p role="status" data-status></p><p class="error" role="alert"></p>
        <div class="radio-actions"><button class="inline-button" data-back>Back</button><button class="inline-button" data-browse>Browse</button>
        <button class="inline-button" data-use>Use this location</button><button class="inline-button" data-close>Cancel</button></div></div></div>`);
      const places = $('[data-places]', overlay), back = $('[data-back]', overlay), browse = $('[data-browse]', overlay), use = $('[data-use]', overlay);
      const modal = openModal(overlay, event.target, places);
      const trail = [{ id: 'r0', label: 'World' }];
      let page = null, generation = 0;
      const load = async () => {
        const token = ++generation, current = trail[trail.length - 1];
        back.disabled = trail.length < 2; browse.disabled = true; use.disabled = true; places.disabled = true;
        $('[data-path]', overlay).textContent = trail.map((place) => place.label).join(' › ');
        $('[data-status]', overlay).textContent = 'Loading places…'; $('.error', overlay).textContent = '';
        try {
          const result = await api('POST', '/api/radio/location-browse', { id: current.id, ancestors: trail.slice(0, -1).map((place) => place.id) });
          if (token !== generation || !overlay.isConnected) return;
          page = result; places.replaceChildren();
          for (const place of page.locations) places.append(new Option(place.label, place.id));
          if (page.locations.length) places.selectedIndex = 0;
          places.disabled = !page.locations.length; browse.disabled = !page.locations.length; use.disabled = !page.selectable;
          $('[data-status]', overlay).textContent = page.selectable ? 'This location has internet radio stations.' : page.locations.length ? 'Choose a place to continue.' : 'No local stations are listed here.';
        } catch (e) {
          if (token === generation && overlay.isConnected) { $('[data-status]', overlay).textContent = ''; $('.error', overlay).textContent = e.message; }
        }
      };
      const descend = () => {
        const selected = page?.locations.find((place) => place.id === places.value);
        if (selected) { trail.push(selected); load(); }
      };
      browse.addEventListener('click', descend); places.addEventListener('dblclick', descend);
      back.addEventListener('click', () => { if (trail.length > 1) { trail.pop(); load(); } });
      use.addEventListener('click', async () => {
        if (!page?.selectable) return;
        use.disabled = true;
        try {
          const current = trail[trail.length - 1];
          await api('POST', '/api/radio/location-select', { id: current.id, label: current.label, ancestors: trail.slice(0, -1).map((place) => place.id) });
          modal.close(); setDirty(false); toast('Location updated'); await show('radio');
        } catch (e) { if (overlay.isConnected) { $('.error', overlay).textContent = e.message; use.disabled = false; } }
      });
      load();
    });
    const favorites = $('#radio-favorites', main);
    if (!data.favorites.length) favorites.innerHTML = '<p>Add stations and shows with Add to Favorites on the controller.</p>';
    for (const [index, item] of data.favorites.entries()) {
      const row = el(`<div class="radio-managed-row"><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.kind)}</small></div>
        <div class="radio-actions"><button class="inline-button" data-action="up" ${index ? '' : 'disabled'} aria-label="Move ${escapeHtml(item.title)} up">↑</button>
        <button class="inline-button" data-action="rename">Rename</button><button class="inline-button" data-action="remove">Remove</button></div></div>`);
      row.addEventListener('click', async (event) => {
        const action = event.target.closest('[data-action]')?.dataset.action;
        if (!action) return;
        try {
          if (action === 'remove') await act('favorite-remove', { id: item.id });
          if (action === 'up') await act('favorite-edit', { id: item.id, index: index - 1 });
          if (action === 'rename') {
            const title = prompt('Favorite name', item.title);
            if (title?.trim()) await act('favorite-edit', { id: item.id, title: title.trim() });
          }
        } catch (e) { toast('Not changed', e.message, true); }
      });
      favorites.append(row);
    }
    const editStation = (item = null, trigger = $('#radio-add', main)) => {
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="radio-editor-title"><form class="popup">
        <h2 id="radio-editor-title">${item ? 'Edit' : 'Add'} station</h2>
        <label class="field"><span>Name</span><input name="name" required maxlength="250" value="${escapeHtml(item?.title || '')}"></label>
        <label class="field"><span>Stream or playlist URL</span><input name="url" type="url" required value="${escapeHtml(item?.url || '')}"></label>
        <label class="field"><span>Station logo URL</span><input name="logo" type="url" value="${escapeHtml(item?.logo || item?.image || '')}"></label>
        <p class="error" role="alert"></p><div class="radio-actions"><button type="button" class="inline-button" data-test>Test URL</button>
        <button type="button" class="inline-button" data-close>Cancel</button><button class="inline-button" type="submit">Save station</button></div></form></div>`);
      const editor = $('form', overlay), modal = openModal(overlay, trigger, editor.elements.name);
      $('[data-test]', editor).addEventListener('click', async () => {
        try { const test = await api('POST', '/api/radio/test-url', { url: editor.elements.url.value }); $('.error', editor).textContent = `${test.streams.length} stream option(s) found. Playback was not started.`; }
        catch (e) { $('.error', editor).textContent = e.message; }
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
    $('#radio-add', main).addEventListener('click', () => editStation());
    for (const item of data.custom_stations) {
      const row = el(`<div class="radio-managed-row"><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.url)}</small></div>
        <div class="radio-actions"><button class="inline-button" data-edit>Edit</button><button class="inline-button" data-delete>Delete</button></div></div>`);
      $('[data-edit]', row).addEventListener('click', (event) => editStation(item, event.target));
      $('[data-delete]', row).addEventListener('click', async () => {
        if (!confirm(`Delete ${item.title}?`)) return;
        try { await act('custom', { operation: 'delete', id: item.id }); } catch (e) { toast('Not deleted', e.message, true); }
      });
      $('#radio-custom', main).append(row);
    }
    main.querySelectorAll('[data-radio-action]').forEach((button) => button.addEventListener('click', async () => {
      if (!confirm(button.dataset.radioAction === 'reset' ? 'Reset Radio preferences? Favorites and custom stations will be kept.' : 'Clear Radio listening and search history?')) return;
      try { await act(button.dataset.radioAction, {}); } catch (e) { toast('Not changed', e.message, true); }
    }));
    $('#radio-export', main).addEventListener('click', async () => {
      try {
        const exported = await api('POST', '/api/radio/export', {});
        const url = URL.createObjectURL(new Blob([JSON.stringify(exported, null, 2)], { type: 'application/json' }));
        const link = el('<a download="cr100-radio.json">Download</a>'); link.href = url; link.click(); URL.revokeObjectURL(url);
      } catch (e) { toast('Export failed', e.message, true); }
    });
    $('#radio-import', main).addEventListener('change', async (event) => {
      const file = event.target.files[0]; if (!file || !confirm('Merge this file into your Radio favorites and settings?')) return;
      try { await act('import', { data: JSON.parse(await file.text()), merge: true }); } catch (e) { toast('Import failed', e.message, true); }
    });
    softkeys([{ label: 'Refresh Radio', run: () => act('refresh', {}) }, { label: 'Alarms', run: () => { location.hash = 'alarms'; } },
      { label: 'Save changes', run: save }]);
  },
};

views.alarms = {
  title: 'Alarms',
  async open(main) {
    const data = await api('GET', '/api/alarms');
    main.innerHTML = '<p class="lede">Household alarms share one schedule across Radio and future music providers.</p><p class="hint">For Snooze and Stop Alarm on a CR100, edit the alarm on that controller, choose <strong>Advanced → Show Clock on this controller → Yes</strong>, then save. Set this separately on each controller.</p><section class="popup"><div id="alarm-list"></div><button class="inline-button" id="alarm-add">Add alarm</button></section>';
    const list = $('#alarm-list', main);
    const refresh = () => show('alarms');
    const editor = (alarm = null, trigger = $('#alarm-add', main)) => {
      const sources = data.sources.filter((source) => source.media_ref);
      const selected = alarm?.media_ref || { provider: 'chime', content_id: 'default', account_ref: null };
      const roomOptions = [...data.rooms];
      const selectedRoom = alarm && roomOptions.find((room) => room.key === alarm.room || room.udn === alarm.room);
      if (alarm && !selectedRoom) roomOptions.push({ key: alarm.room, name: `Unavailable room (${alarm.room})`, shown: true });
      const sourceOptions = [{ title: 'Chime', media_ref: { provider: 'chime', content_id: 'default', account_ref: null } }, ...sources];
      if (!sourceOptions.some((source) => JSON.stringify(source.media_ref) === JSON.stringify(selected))) sourceOptions.push({ title: alarm.title, media_ref: selected });
      const overlay = el(`<div class="picker" role="dialog" aria-modal="true" aria-labelledby="alarm-editor-title"><form class="popup">
        <h2 id="alarm-editor-title">${alarm ? 'Edit' : 'Add'} alarm</h2>
        <label class="field"><span>Time</span><input name="time" type="time" required value="${escapeHtml((alarm?.time || '07:00').slice(0, 5))}"></label>
        <label class="field"><span>Timezone</span><input name="timezone" required value="${escapeHtml(alarm?.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone)}"></label>
        <label class="field"><span>Repeat</span><select name="recurrence"><option value="DAILY">Every day</option><option value="WEEKDAYS">Weekdays</option><option value="WEEKENDS">Weekends</option><option value="ONCE">Once</option></select></label>
        <label class="field"><span>Room</span><select name="room" required>${roomOptions.map((r) => `<option value="${escapeHtml(r.key)}">${escapeHtml(r.name)}</option>`).join('')}</select></label>
        <label class="field"><span>Source</span><select name="source">${sourceOptions.map((source, i) => `<option value="${i}">${escapeHtml(source.title)}</option>`).join('')}</select></label>
        <label class="field"><span>Volume</span><input name="volume" type="number" min="0" max="100" value="${alarm?.volume ?? 20}" required></label>
        <label class="field"><span>Duration in minutes</span><input name="duration" type="number" min="1" max="1440" value="${Math.round((alarm?.duration || 3600) / 60)}" required></label>
        <label class="radio-check"><input name="enabled" type="checkbox" ${alarm?.enabled === false ? '' : 'checked'}> Enabled</label>
        <label class="radio-check"><input name="include_linked" type="checkbox" ${alarm?.include_linked ? 'checked' : ''}> Include linked rooms at alarm time</label>
        <p class="error" role="alert"></p><div class="radio-actions"><button type="button" class="inline-button" data-close>Cancel</button><button class="inline-button" type="submit">Save alarm</button></div></form></div>`);
      const form = $('form', overlay), modal = openModal(overlay, trigger, form.elements.time);
      form.elements.recurrence.value = alarm?.recurrence || 'DAILY';
      if (alarm) form.elements.room.value = selectedRoom?.key || alarm.room;
      form.elements.source.value = String(sourceOptions.findIndex((source) => JSON.stringify(source.media_ref) === JSON.stringify(selected)));
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        try {
          await api('PUT', '/api/alarms', { ...(alarm || {}), time: form.elements.time.value, timezone: form.elements.timezone.value,
            recurrence: form.elements.recurrence.value, room: form.elements.room.value, volume: Number(form.elements.volume.value),
            duration: Number(form.elements.duration.value) * 60, enabled: form.elements.enabled.checked,
            include_linked: form.elements.include_linked.checked, media_ref: sourceOptions[Number(form.elements.source.value)].media_ref });
          modal.close(); await refresh();
        } catch (e) { $('.error', form).textContent = e.message; }
      });
    };
    if (!data.alarms.length) list.innerHTML = '<p>No alarms yet. Save a Radio station to Favorites to choose it here.</p>';
    for (const alarm of data.alarms) {
      const row = el(`<div class="radio-managed-row"><div><strong>${escapeHtml(alarm.time.slice(0, 5))} · ${escapeHtml(alarm.title)}</strong>
        <small>${escapeHtml(alarm.recurrence)} · ${escapeHtml(alarm.timezone)} · ${alarm.enabled ? 'Enabled' : 'Off'}${alarm.last_run ? ' · ' + escapeHtml(alarm.last_run.status) : ''}</small></div>
        <div class="radio-actions"><button class="inline-button" data-edit>Edit</button><button class="inline-button" data-snooze>Snooze</button><button class="inline-button" data-stop>Stop</button><button class="inline-button" data-delete>Delete</button></div></div>`);
      $('[data-edit]', row).addEventListener('click', (event) => editor(alarm, event.target));
      for (const action of ['snooze', 'stop', 'delete']) $('[data-' + action + ']', row).addEventListener('click', async () => {
        if (action === 'delete' && !confirm('Delete this alarm?')) return;
        try { await api('POST', '/api/alarms/' + action, { id: alarm.id, room: alarm.room }); await refresh(); }
        catch (e) { toast('Alarm not changed', e.message, true); }
      });
      list.append(row);
    }
    $('#alarm-add', main).addEventListener('click', () => editor());
    softkeys([{ label: 'Refresh', run: refresh }, { label: 'Radio', run: () => { location.hash = 'radio'; } }, { label: 'Add alarm', run: () => editor() }]);
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
      <label class="field"><span>New password</span><input type="password" id="pw-new" autocomplete="new-password" minlength="8" required></label>
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
