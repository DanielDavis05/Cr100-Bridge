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
    const data = await api('GET', '/api/speakers');
    const rows = data.speakers.map((s) => ({ ...s }));
    let library = data.library_entity || '';
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
      main.append(el(`<p class="lede">These are the rooms the controller lists, sorted by name as it always sorts them. Switch a speaker off to hide it from the controller; nothing changes on the speaker itself.</p>`));
      if (!data.ha_connected) {
        main.append(el(`<div class="popup"><h3>Home Assistant is not connected</h3><p>The list may be out of date. Check the Home Assistant page.</p></div>`));
      }
      main.append(el(`<h2 class="group-title">On the controller</h2>`));
      if (!shown.length) main.append(el(`<p class="empty">No speakers are shown. Switch one on below.</p>`));
      shown.forEach((row) => main.append(line(row)));
      main.append(el(`<h2 class="group-title">Not on the controller</h2>`));
      if (!hidden.length) main.append(el(`<p class="empty">Every speaker Home Assistant knows is on the controller.</p>`));
      hidden.forEach((row) => main.append(line(row)));

      const sonos = shown.filter((r) => r.sonos);
      const panel = el(`<div class="popup"><h3>Music library</h3>
        <p>Favourites, Sonos playlists and your music library are the same on every Sonos speaker. Pick which one the controller reads them from; it matters for speakers that are not Sonos.</p>
        <label class="field"><span>Read the Music menu from</span><select id="library"></select></label></div>`);
      const select = $('#library', panel);
      select.append(new Option('Each speaker itself', ''));
      sonos.forEach((r) => select.append(new Option(r.name, r.entity)));
      select.value = sonos.some((r) => r.entity === library) ? library : '';
      select.addEventListener('change', () => { library = select.value; changed(); });
      main.append(el('<h2 class="group-title">Music menu</h2>'), panel);
      changed();
    };

    const describe = (row) => {
      if (row.missing) return { main: 'Not found in Home Assistant', sub: row.entity };
      if (row.simulated) return { main: 'Simulated room', sub: 'Not connected to a speaker' };
      const state = { playing: 'Playing', paused: 'Paused', idle: 'Idle', off: 'Off', unavailable: 'Unavailable',
        on: 'On', standby: 'Standby', buffering: 'Buffering' }[row.state] || (row.state || 'Unknown');
      return { main: row.playing && row.state === 'playing' ? `${state}: ${row.playing}` : state, sub: row.entity };
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
      const close = () => { overlay.remove(); document.removeEventListener('keydown', onKey, true); trigger.focus(); };
      const onKey = (event) => {
        if (event.key === 'Escape') { event.preventDefault(); close(); }
        if (event.key === 'Tab') {   // keep focus inside the popup
          const items = [...overlay.querySelectorAll('button')];
          const at = items.indexOf(document.activeElement);
          if (event.shiftKey && at <= 0) { event.preventDefault(); items[items.length - 1].focus(); }
          else if (!event.shiftKey && at === items.length - 1) { event.preventDefault(); items[0].focus(); }
        }
      };
      overlay.addEventListener('click', (event) => {
        if (event.target === overlay || event.target.closest('[data-close]')) return close();
        const choice = event.target.closest('[data-icon]');
        if (!choice) return;
        const icon = choice.dataset.icon;
        row.custom_icon = icon && icon !== guessIcon(row.name) ? icon : '';
        row.icon = icon || guessIcon(row.name);
        overlay.remove(); document.removeEventListener('keydown', onKey, true);
        render();
        focusRow(row, 'button.room-icon');
      });
      document.addEventListener('keydown', onKey, true);
      document.body.append(overlay);
      (overlay.querySelector('[aria-pressed="true"]') || overlay.querySelector('.icon-choice')).focus();
    };

    const startRename = (node, row) => {
      const tile = $('.tile', node);
      const input = el(`<input type="text" maxlength="30" aria-label="Room name for ${escapeHtml(row.entity || row.name)}">`);
      input.value = row.name;
      $('.name', tile).replaceWith(input);
      input.focus(); input.select();
      const finish = (keep) => {
        if (keep) {
          const name = input.value.trim();
          row.custom_name = !name || name === row.ha_name ? '' : name;
          row.name = row.custom_name || row.ha_name || row.name;
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
        rooms: shown.map((r) => ({ key: r.key, name: r.custom_name, icon: r.custom_icon })), library_entity: library || null });
      baseline = saved();
      changed();
      toast('Saved', result.message);
    };

    render();
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
    const render = (data) => {
      main.innerHTML = '';
      main.append(el(`<p class="lede">CR100 controllers paired with this bridge. A controller that is asleep in its cradle or idle shows as asleep; pick it up and it reconnects within a few seconds.</p>`));
      if (!data.radio_running) {
        main.append(el(`<div class="popup"><h3>The bridge radio is not running</h3><p>Controllers cannot connect until the bridge is started on the Pi.</p></div>`));
      }
      data.controllers.forEach((c) => main.append(card(c)));
      if (!data.controllers.length) main.append(el(`<p class="empty">No controller is paired yet.</p>`));
      main.append(el(`<h2 class="group-title">Pairing</h2>`), el(`<div class="popup">
        <dl class="facts inset">
          <dt>Household</dt><dd>${escapeHtml(data.household || 'Not paired')}</dd>
          <dt>Radio channel</dt><dd>${escapeHtml(data.channel ?? '?')}</dd>
        </dl>
        <p class="gap-above">${data.pairing?.running ? 'Waiting for a CR100. Put the controller in its add-to-household setup mode within three minutes.' :
          data.pairing?.failed ? 'The pairing attempt failed. Check the Pi’s service logs, then try again.' :
          'The bridge supports one CR100. Start pairing, then put the controller in its add-to-household setup mode.'}</p>
        <button type="button" class="inline-button" id="pair-controller" ${!data.pairing?.available || data.pairing?.running ? 'disabled' : ''}>${data.pairing?.running ? 'Pairing…' : data.controllers.length ? 'Replace pairing' : 'Pair controller'}</button>
      </div>`));
      $('#pair-controller', main).addEventListener('click', async (event) => {
        if (data.controllers.length && !confirm('Replace the existing controller pairing? The bridge will pause while pairing.')) return;
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
            <div class="tools"><button type="button" class="mini" aria-label="Rename ${escapeHtml(c.name)}">${ICONS.pencil}</button></div></div>
        </div>
        <div class="popup">
          <dl class="facts inset">${facts.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('')}</dl>
          ${c.events && c.events.length ? `<h3 class="gap-above">Recent link events</h3><ul class="timeline inset">${c.events.slice().reverse().map((e) =>
            `<li><time>${clock(e.time)}</time><span>${escapeHtml(EVENT_TEXT[e.event] || e.event)}</span></li>`).join('')}</ul>` : ''}
        </div></section>`);
      $('.tools button', node).addEventListener('click', async () => {
        const name = prompt('Name for this controller', c.name);
        if (name == null || !name.trim() || name.trim() === c.name) return;
        try { await api('PUT', `/api/controllers/${c.mac}`, { name: name.trim() }); toast('Renamed', `This controller is now ${name.trim()}.`); refresh(); }
        catch (error) { toast('Not renamed', error.message, true); }
      });
      return node;
    };

    const refresh = async () => render(await api('GET', '/api/controllers'));
    await refresh();
    softkeys([{ label: 'Refresh', run: refresh }, null, null]);
    pollTimer = setInterval(() => { if (!document.hidden && current === 'controllers') refresh().catch(() => {}); }, 10000);
  },
};

// Spotify ----------------------------------------------------------------------------------
views.spotify = {
  title: 'Spotify',
  async open(main) {
    const data = await api('GET', '/api/spotify');
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

    const values = () => ({ enabled: $('#sp-enabled', form).checked, name: $('#sp-name', form).value.trim(),
      client_id: $('#sp-client', form).value.trim() });
    const baseline = JSON.stringify(values());
    const update = () => {
      const isDirty = JSON.stringify(values()) !== baseline;
      setDirty(isDirty);
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
        { label: 'Save changes', disabled: !isDirty, run: async () => {
            const result = await api('PUT', '/api/spotify', values()); setDirty(false); toast('Saved', result.message); show('spotify'); } },
      ]);
    };
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    update();
  },
};

// Home Assistant ---------------------------------------------------------------------------
views['home-assistant'] = {
  title: 'Home Assistant',
  async open(main) {
    const data = await api('GET', '/api/home-assistant');
    main.innerHTML = '';
    main.append(el(`<p class="lede">The bridge reads your speakers, what they are playing and your music from Home Assistant, and sends the controller's commands back through it.</p>`));
    const dot = data.connected ? 'good' : 'bad';
    const status = data.connected
      ? `Connected${data.version ? ` to Home Assistant ${escapeHtml(data.version)}` : ''}. ${data.media_players} media players.`
      : escapeHtml(data.error || 'Not connected.');
    main.append(el(`<div class="popup"><div class="state-line"><span class="dot ${dot}"></span>${status}</div></div>`));
    const form = el(`<div class="popup"><h3>Connection</h3>
      <label class="field"><span>Address</span><input type="url" id="ha-url" value="${escapeHtml(data.url)}" placeholder="http://homeassistant.local:8123" spellcheck="false"></label>
      <label class="field"><span>Long-lived access token</span><input type="password" id="ha-token" autocomplete="off" placeholder="${data.token_saved ? 'Saved. Paste a new token to replace it.' : 'Paste a token'}">
        <div class="hint">Create one in Home Assistant under your profile, Security, Long-lived access tokens. The bridge never shows a saved token.</div></label>
      <p class="error" id="ha-error" role="alert"></p></div>`);
    main.append(form);
    const values = () => ({ url: $('#ha-url', form).value.trim(), token: $('#ha-token', form).value.trim() });
    const baseline = JSON.stringify(values());
    const update = () => {
      const isDirty = JSON.stringify(values()) !== baseline;
      setDirty(isDirty);
      softkeys([
        { label: 'Test connection', run: async () => {
            const result = await api('POST', '/api/home-assistant/test', values());
            toast(result.ok ? 'Connection works' : 'Connection failed', result.message, !result.ok); } },
        null,
        { label: 'Save changes', disabled: !isDirty, run: async () => {
            const result = await api('PUT', '/api/home-assistant', values()); setDirty(false); toast('Saved', result.message);
            setTimeout(() => show('home-assistant'), 2500); } },
      ]);
    };
    form.addEventListener('input', update);
    update();
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
    const controllerText = { connected: 'Controller connected', dozing: 'Controller connected, idle',
      searching: 'Controller reconnecting', away: 'Controller asleep or out of range',
      'radio-off': 'Bridge radio is not running', none: 'No controller paired' }[o.controller];
    const badge = $('#badge-controller');
    badge.innerHTML = bars(lit, o.controller === 'searching' ? '#f3c24a' : '#a6d84a');
    badge.setAttribute('aria-label', controllerText); badge.title = controllerText;
    const ha = $('#badge-ha');
    ha.innerHTML = houseBadge(o.home_assistant);
    const haText = o.home_assistant ? 'Home Assistant connected' : 'Home Assistant not connected';
    ha.setAttribute('aria-label', haText); ha.title = haText;
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
