const CFG = window.FKM_CONFIG || {};
const ON_PORT80 = (location.port === '' || location.port === '80');
const IS_OPENWRT = !!CFG.IS_OPENWRT;
const IS_APPLE_SILICON = !!CFG.IS_APPLE_SILICON;
const LS_TAB = 'fkm_active_tab';
const LS_LOG = 'fkm_action_log';
const LS_STATUS = 'fkm_last_status';
const LS_TERMINAL_H = 'fkm_terminal_h';
const LS_TERMINAL_COLLAPSED = 'fkm_terminal_collapsed';
const LS_TERMINAL_OPEN = 'fkm_terminal_open';
let currentSelected = null;
let selectedRunning  = false;
let _busy            = false;
let _pollTimer       = null;
let _busyButtons     = new Set();
let _firstRender     = true;
const LOG_MAX_LINES = 4000;

function trimLogLines(text, maxLines) {
  if (!text) return '';
  const lines = text.split('\n');
  if (lines.length <= maxLines) return text;
  return lines.slice(lines.length - maxLines).join('\n');
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Syntax-highlighted code editors (overlay) ───────────────────────────
const _codeEditors = new Map();

function highlightEnvValue(raw) {
  const lead = raw.match(/^\s*/)[0];
  const body = raw.slice(lead.length);
  if (!body) return escapeHtml(raw);
  const q = body[0];
  if ((q === '"' || q === "'") && body.length > 1) {
    return escapeHtml(lead) + '<span class="hl-string">' + escapeHtml(body) + '</span>';
  }
  const t = body.trim();
  if (/^-?\d+(\.\d+)?$/.test(t)) return escapeHtml(lead) + '<span class="hl-number">' + escapeHtml(body) + '</span>';
  if (/^(true|false)$/i.test(t)) return escapeHtml(lead) + '<span class="hl-bool">' + escapeHtml(body) + '</span>';
  return escapeHtml(lead) + '<span class="hl-value">' + escapeHtml(body) + '</span>';
}

function highlightEnvLine(line) {
  if (/^\s*#/.test(line) || (!line.trim() && line.includes('#'))) {
    return '<span class="hl-comment">' + escapeHtml(line) + '</span>';
  }
  if (!line.trim()) return escapeHtml(line);
  const m = line.match(/^(\s*)((?:export\s+)?)([A-Za-z_][\w.-]*)(\s*=\s*)([\s\S]*)$/);
  if (!m) return escapeHtml(line);
  const exportKw = m[2]
    ? '<span class="hl-kw">export</span> '
    : '';
  return escapeHtml(m[1]) + exportKw +
    '<span class="hl-key">' + escapeHtml(m[3]) + '</span>' +
    '<span class="hl-punct">' + escapeHtml(m[4]) + '</span>' +
    highlightEnvValue(m[5]);
}

function highlightEnv(text) {
  if (!text) return '';
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  const html = lines.map(highlightEnvLine).join('\n');
  return text.endsWith('\n') ? html + '\n' : html;
}

function highlightYamlValue(raw) {
  const lead = raw.match(/^\s*/)[0];
  const body = raw.slice(lead.length);
  if (!body) return escapeHtml(raw);
  if (/^\s*#/.test(body)) return escapeHtml(raw);
  const q = body[0];
  if ((q === '"' || q === "'") && body.length > 1) {
    return escapeHtml(lead) + '<span class="hl-string">' + escapeHtml(body) + '</span>';
  }
  const t = body.trim();
  if (/^-?\d+(\.\d+)?$/.test(t)) return escapeHtml(lead) + '<span class="hl-number">' + escapeHtml(body) + '</span>';
  if (/^(true|false|null|yes|no|on|off)$/i.test(t)) {
    return escapeHtml(lead) + '<span class="hl-bool">' + escapeHtml(body) + '</span>';
  }
  if (/^[>|][-+]?\d*$/.test(t)) return escapeHtml(lead) + '<span class="hl-punct">' + escapeHtml(body) + '</span>';
  return escapeHtml(lead) + '<span class="hl-value">' + escapeHtml(body) + '</span>';
}

function highlightYamlLine(line) {
  if (/^\s*#/.test(line)) return '<span class="hl-comment">' + escapeHtml(line) + '</span>';
  if (!line.trim()) return escapeHtml(line);
  const list = line.match(/^(\s*)(-\s+)(.*)$/);
  if (list) {
    return escapeHtml(list[1]) + '<span class="hl-punct">' + escapeHtml(list[2]) + '</span>' +
      highlightYamlValue(list[3]);
  }
  const kv = line.match(/^(\s*)([^:\s#][^:]*?)(\s*:\s*)(.*)$/);
  if (kv) {
    return escapeHtml(kv[1]) + '<span class="hl-key">' + escapeHtml(kv[2]) + '</span>' +
      '<span class="hl-punct">' + escapeHtml(kv[3]) + '</span>' +
      highlightYamlValue(kv[4]);
  }
  return escapeHtml(line);
}

function highlightYaml(text) {
  if (!text) return '';
  const lines = text.replace(/\r\n/g, '\n').split('\n');
  const html = lines.map(highlightYamlLine).join('\n');
  return text.endsWith('\n') ? html + '\n' : html;
}

function syncCodeEditorScroll(wrap, ta) {
  const pre = wrap.querySelector('.code-highlight');
  if (pre) {
    pre.scrollTop = ta.scrollTop;
    pre.scrollLeft = ta.scrollLeft;
  }
}

function syncCodeEditorHeight(wrap, ta) {
  ta.style.height = '0';
  const h = Math.max(ta.scrollHeight, wrap.clientHeight || 0, 120);
  ta.style.height = h + 'px';
  const pre = wrap.querySelector('.code-highlight');
  if (pre) pre.style.minHeight = h + 'px';
}

function bindCodeEditor(wrap) {
  const ta = wrap.querySelector('.code-input');
  const code = wrap.querySelector('.code-highlight code');
  if (!ta || !code) return;
  const lang = wrap.dataset.lang || 'env';
  const update = () => {
    const text = ta.value.replace(/\r\n/g, '\n');
    code.innerHTML = lang === 'yaml' ? highlightYaml(text) : highlightEnv(text);
    syncCodeEditorHeight(wrap, ta);
    syncCodeEditorScroll(wrap, ta);
  };
  ta.addEventListener('input', update);
  ta.addEventListener('scroll', () => syncCodeEditorScroll(wrap, ta));
  wrap._updateHighlight = update;
  if (ta.id) _codeEditors.set(ta.id, { wrap, update });
  update();
}

function refreshCodeEditor(id) {
  _codeEditors.get(id)?.update();
}

function layoutCodeEditors() {
  _codeEditors.forEach(({ update }) => update());
}

function initCodeEditors() {
  document.querySelectorAll('.code-editor').forEach(bindCodeEditor);
  window.addEventListener('resize', () => {
    if (document.getElementById('app-panels')?.classList.contains('panels--editor')) {
      layoutCodeEditors();
    }
  });
}

function updateEditorPanelsLayout(tab) {
  const panels = document.getElementById('app-panels');
  if (panels) panels.classList.toggle('panels--editor', tab === 'env' || tab === 'compose');
}

const ANSI_FG = {
  30:'#9aa0a6', 31:'#ff6b6b', 32:'#69db69', 33:'#ffd43b', 34:'#74c0fc', 35:'#da77f2', 36:'#3bc9db', 37:'#e0e0e0',
  90:'#6b7280', 91:'#ff8787', 92:'#8ce99a', 93:'#ffe066', 94:'#91a7ff', 95:'#e599f7', 96:'#66d9e8', 97:'#ffffff',
};

function xterm256(n) {
  if (n < 16) {
    return ['#000','#cd3131','#0dbc79','#e5e510','#2472c8','#bc3fbc','#11a8cd','#e5e5e5',
            '#666','#f14c4c','#23d18b','#f5f543','#3b8eea','#d670d6','#29b8db','#e5e5e5'][n] || '#b0c0a0';
  }
  if (n >= 232) {
    const g = 8 + (n - 232) * 10;
    return 'rgb(' + g + ',' + g + ',' + g + ')';
  }
  n -= 16;
  const v = [0, 95, 135, 175, 215, 255];
  return 'rgb(' + v[(n / 36) | 0] + ',' + v[((n % 36) / 6) | 0] + ',' + v[n % 6] + ')';
}

/** Convert ANSI SGR sequences to HTML spans (for docker compose logs). */
function ansiToHtml(text) {
  if (!text) return '';
  if (text.indexOf('\x1b') === -1) return escapeHtml(text);

  const out = [];
  let i = 0;
  let fg = null;
  let bold = false;
  let buf = '';

  function styleAttr() {
    const parts = [];
    if (fg) parts.push('color:' + fg);
    if (bold) parts.push('font-weight:bold');
    return parts.join(';');
  }

  function flush() {
    if (!buf) return;
    const st = styleAttr();
    if (st) out.push('<span style="' + st + '">');
    out.push(escapeHtml(buf));
    if (st) out.push('</span>');
    buf = '';
  }

  function applySgr(params) {
    flush();
    const codes = params === '' ? [0] : params.split(';').map(function(x) { return x === '' ? 0 : parseInt(x, 10); });
    for (let j = 0; j < codes.length; j++) {
      const c = codes[j];
      if (c === 0) { fg = null; bold = false; }
      else if (c === 1) bold = true;
      else if (c === 22) bold = false;
      else if (c === 39) fg = null;
      else if (c >= 30 && c <= 37) fg = ANSI_FG[c];
      else if (c >= 90 && c <= 97) fg = ANSI_FG[c];
      else if (c === 38 && codes[j + 1] === 5 && j + 2 < codes.length) {
        fg = xterm256(codes[j + 2]); j += 2;
      } else if (c === 38 && codes[j + 1] === 2 && j + 4 < codes.length) {
        fg = 'rgb(' + codes[j + 2] + ',' + codes[j + 3] + ',' + codes[j + 4] + ')'; j += 4;
      }
    }
  }

  while (i < text.length) {
    const ch = text.charCodeAt(i);
    if (ch === 27 && text[i + 1] === '[') {
      let j = i + 2;
      let params = '';
      while (j < text.length && /[0-9;]/.test(text[j])) params += text[j++];
      const cmd = text[j++];
      if (cmd === 'm') applySgr(params);
      i = j;
    } else if (ch === 27 && text[i + 1] === ']') {
      flush();
      let j = i + 2;
      while (j < text.length && text.charCodeAt(j) !== 7) {
        if (text.charCodeAt(j) === 27 && text[j + 1] === '\\') { j += 2; break; }
        j++;
      }
      if (j < text.length && text.charCodeAt(j) === 7) j++;
      i = j;
    } else if (text[i] === '\r') {
      if (text[i + 1] === '\n') { flush(); out.push('\n'); i += 2; }
      else i++;
    } else {
      buf += text[i++];
    }
  }
  flush();
  return out.join('');
}

/**
 * Replay raw PTY output (cursor moves, line clears, \\r redraws) then colorize.
 * Replays the full buffer on each poll so chunk boundaries cannot corrupt state.
 */
function renderTerminalAnsi(raw) {
  if (!raw) return '';
  const buf = [];
  let cy = 0;
  let cx = 0;

  function ensureRow(y) {
    while (buf.length <= y) buf.push('');
  }

  function lineAt(y) {
    ensureRow(y);
    return buf[y] || '';
  }

  function setLine(y, s) {
    ensureRow(y);
    buf[y] = s;
  }

  function write(ch) {
    let line = lineAt(cy);
    if (cx >= line.length) {
      if (cx > line.length) line += ' '.repeat(cx - line.length);
      line += ch;
    } else {
      line = line.slice(0, cx) + ch + line.slice(cx + 1);
    }
    setLine(cy, line);
    cx += 1;
  }

  function eraseLine() {
    setLine(cy, '');
  }

  function eraseToEol() {
    setLine(cy, lineAt(cy).slice(0, cx));
  }

  /** Drop stale tail when compose redraws a shorter padded line over an old one. */
  function commitLine() {
    setLine(cy, lineAt(cy).slice(0, cx));
  }

  function parseNums(params) {
    const nums = [];
    for (const part of params.split(';')) {
      const p = part.replace(/^\?/, '');
      if (p !== '' && /^\d+$/.test(p)) nums.push(parseInt(p, 10));
    }
    return nums;
  }

  let i = 0;
  while (i < raw.length) {
    const c = raw[i];
    if (c === '\x1b' && raw[i + 1] === '[') {
      let j = i + 2;
      let params = '';
      while (j < raw.length && /[0-9;?]/.test(raw[j])) params += raw[j++];
      const cmd = raw[j++] || '';
      const nums = parseNums(params);
      if (cmd === 'm') {
        const seq = '\x1b[' + params + 'm';
        for (let k = 0; k < seq.length; k++) write(seq[k]);
      } else if (cmd === 'A' || cmd === 'F') {
        cy = Math.max(0, cy - (nums[0] || 1));
        cx = 0;
      } else if (cmd === 'B' || cmd === 'E') {
        cy += nums[0] || 1;
        cx = 0;
      } else if (cmd === 'G') {
        const col = nums.length ? nums[0] : 1;
        cx = Math.max(0, col - 1);
        eraseToEol();
      } else if (cmd === 'H' || cmd === 'f') {
        cy = Math.max(0, (nums[0] || 1) - 1);
        cx = Math.max(0, (nums[1] || 1) - 1);
      } else if (cmd === 'K') {
        const mode = nums[0] || 0;
        if (mode === 2) eraseLine();
        else if (mode === 1) { setLine(cy, lineAt(cy).slice(cx)); cx = 0; }
        else eraseToEol();
      } else if (cmd === 'J') {
        const mode = nums[0] || 0;
        if (mode === 2) { buf.length = 0; cy = 0; cx = 0; }
        else if (mode === 1) buf.length = cy + 1;
        else buf.length = cy;
      }
      i = j;
    } else if (c === '\x1b' && raw[i + 1] === ']') {
      let j = i + 2;
      while (j < raw.length && raw.charCodeAt(j) !== 7) {
        if (raw.charCodeAt(j) === 27 && raw[j + 1] === '\\') { j += 2; break; }
        j++;
      }
      if (j < raw.length && raw.charCodeAt(j) === 7) j++;
      i = j;
    } else if (c === '\r') {
      if (raw[i + 1] === '\n') {
        commitLine();
        cy += 1;
        cx = 0;
        i += 2;
      } else {
        cx = 0;
        eraseLine();
        i += 1;
      }
    } else if (c === '\n') {
      commitLine();
      cy += 1;
      cx = 0;
      i += 1;
    } else if (c === '\b') {
      cx = Math.max(0, cx - 1);
      i += 1;
    } else if (c === '\x07') {
      i += 1;
    } else if (c === '\t') {
      write(' ');
      i += 1;
    } else if (c >= ' ' || c > '\x7f') {
      write(c);
      i += 1;
    } else {
      i += 1;
    }
  }

  if (cx > 0) commitLine();
  while (buf.length && buf[buf.length - 1] === '') buf.pop();
  return ansiToHtml(buf.join('\n'));
}

/** Render command/progress output with ANSI colors (same as logs modal). */
function formatTerminalHtml(raw) {
  if (!raw) return '';
  const text = trimLogLines(raw, LOG_MAX_LINES);
  if (text.indexOf('\x1b') !== -1 || text.indexOf('\r') !== -1) return renderTerminalAnsi(text);
  return escapeHtml(text);
}

// ── Resizable terminal ─────────────────────────────────────────────────
let _outputCollapsed = false;
let _terminalUnread = false;

function isTerminalVisible() {
  const drawer = document.getElementById('terminal-drawer');
  return !!(drawer?.classList.contains('open') && !drawer.classList.contains('collapsed'));
}

function setTerminalUnread(on) {
  _terminalUnread = !!on;
  document.getElementById('terminal-unread')?.classList.toggle('visible', _terminalUnread);
  document.getElementById('terminal-drawer')?.classList.toggle('has-unread', _terminalUnread);
}

function labeledBtn(cls, attrs, icon, label) {
  const ico = icon ? `<span class="btn-ico" aria-hidden="true">${icon}</span>` : '';
  return `<button type="button" class="btn ${cls} btn-labeled" ${attrs} title="${label}" aria-label="${label}">${ico}<span class="btn-lbl">${label}</span></button>`;
}

function initTerminal() {
  const drawer = document.getElementById('terminal-drawer');
  const handle = document.getElementById('terminal-resize-handle');
  const body = document.getElementById('output-body');
  if (!drawer || !handle) return;

  const savedH = lsGet(LS_TERMINAL_H, 220);
  drawer.style.setProperty('--terminal-h', savedH + 'px');
  _outputCollapsed = !!lsGet(LS_TERMINAL_COLLAPSED, false);
  if (_outputCollapsed) {
    drawer.classList.add('collapsed');
    document.getElementById('terminal-chevron')?.classList.remove('expanded');
  }

  if (lsGet(LS_TERMINAL_OPEN, false)) {
    drawer.classList.add('open');
    const savedLog = lsGet(LS_LOG, '');
    if (savedLog && body) body.innerHTML = formatTerminalHtml(savedLog);
  }

  let startY = 0;
  let startH = 0;

  function onMove(e) {
    const y = e.touches ? e.touches[0].clientY : e.clientY;
    const next = Math.min(window.innerHeight * 0.75, Math.max(100, startH + (startY - y)));
    drawer.style.setProperty('--terminal-h', next + 'px');
    syncProgressOverlayInset();
  }

  function onEnd() {
    handle.classList.remove('active');
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onEnd);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onEnd);
    lsSet(LS_TERMINAL_H, drawer.getBoundingClientRect().height);
    syncProgressOverlayInset();
  }

  function onStart(e) {
    if (drawer.classList.contains('collapsed')) return;
    e.preventDefault();
    handle.classList.add('active');
    startY = e.touches ? e.touches[0].clientY : e.clientY;
    startH = drawer.getBoundingClientRect().height;
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onEnd);
    document.addEventListener('touchmove', onMove, { passive: false });
    document.addEventListener('touchend', onEnd);
  }

  handle.addEventListener('mousedown', onStart);
  handle.addEventListener('touchstart', onStart, { passive: false });

  const ro = typeof ResizeObserver !== 'undefined'
    ? new ResizeObserver(() => syncProgressOverlayInset())
    : null;
  if (ro) ro.observe(drawer);
  window.addEventListener('resize', syncProgressOverlayInset);
}

/** Keep progress backdrop from covering the terminal during actions. */
function syncProgressOverlayInset() {
  if (!document.body.classList.contains('action-running')) return;
  const drawer = document.getElementById('terminal-drawer');
  if (!drawer?.classList.contains('open')) {
    document.body.style.removeProperty('--terminal-visible-h');
    return;
  }
  const h = drawer.classList.contains('collapsed')
    ? (drawer.querySelector('.terminal-bar')?.offsetHeight || 36)
    : drawer.getBoundingClientRect().height;
  document.body.style.setProperty('--terminal-visible-h', Math.ceil(h) + 'px');
}

function openTerminalForAction() {
  const drawer = document.getElementById('terminal-drawer');
  if (!drawer) return;
  drawer.classList.add('open');
  drawer.classList.remove('collapsed');
  _outputCollapsed = false;
  document.getElementById('terminal-chevron')?.classList.add('expanded');
  lsSet(LS_TERMINAL_COLLAPSED, false);
  lsSet(LS_TERMINAL_OPEN, true);
  setTerminalUnread(false);
  syncProgressOverlayInset();
}

function endActionRunning() {
  document.body.classList.remove('action-running');
  document.body.style.removeProperty('--terminal-visible-h');
}

function toggleTerminal() {
  const drawer = document.getElementById('terminal-drawer');
  _outputCollapsed = drawer.classList.toggle('collapsed');
  document.getElementById('terminal-chevron')?.classList.toggle('expanded', !_outputCollapsed);
  lsSet(LS_TERMINAL_COLLAPSED, _outputCollapsed);
  if (!_outputCollapsed) setTerminalUnread(false);
  syncProgressOverlayInset();
}

function showOutput(raw) {
  const drawer = document.getElementById('terminal-drawer');
  const body = document.getElementById('output-body');
  if (!drawer || !body) return;

  const visible = isTerminalVisible();
  const stickToBottom = visible && (body.scrollHeight - body.clientHeight - body.scrollTop <= 48);
  const prevLen = lsGet(LS_LOG, '')?.length || 0;
  const hasNew = raw && raw.length !== prevLen;

  body.innerHTML = formatTerminalHtml(raw);
  drawer.classList.add('open');
  lsSet(LS_TERMINAL_OPEN, true);
  lsSet(LS_LOG, raw);

  if (!visible && hasNew) {
    setTerminalUnread(true);
  } else if (visible) {
    setTerminalUnread(false);
    if (stickToBottom) body.scrollTop = body.scrollHeight;
  }
  syncProgressOverlayInset();
}

function clearOutput() {
  const drawer = document.getElementById('terminal-drawer');
  drawer?.classList.remove('open');
  const body = document.getElementById('output-body');
  if (body) body.innerHTML = '';
  lsSet(LS_LOG, '');
  lsSet(LS_TERMINAL_OPEN, false);
  setTerminalUnread(false);
  _lastProgressLogLen = 0;
}

// ── localStorage helpers ───────────────────────────────────────────────
function lsSet(k,v){ try{ localStorage.setItem(k,JSON.stringify(v)); }catch(e){} }
function lsGet(k,d){ try{ const v=localStorage.getItem(k); return v!==null?JSON.parse(v):d; }catch(e){ return d; } }

function setFormMsg(el, text, type = '') {
  if (!el) return;
  el.textContent = text;
  el.className = 'form-msg' + (type ? ' form-msg-' + type : '');
}

// ── Toast notifications ────────────────────────────────────────────────
function showToast(msg, type='info') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = 'toast toast-' + type;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { if(t.parentNode) t.remove(); }, 3300);
}

// ── Tab switching with persistence ─────────────────────────────────────
const TAB_LABELS = {
  control: 'Control', env: 'Env', compose: 'Compose', wifi: 'WiFi', adapter: 'Adapter',
};

function closeTabsMenu() {
  document.body.classList.remove('tabs-open');
  const btn = document.getElementById('tabbar-toggle');
  if (btn) btn.setAttribute('aria-expanded', 'false');
}

function toggleTabsMenu() {
  const open = document.body.classList.toggle('tabs-open');
  const btn = document.getElementById('tabbar-toggle');
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function initTabMenu() {
  const btn = document.getElementById('tabbar-toggle');
  const backdrop = document.getElementById('tabbar-backdrop');
  btn?.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleTabsMenu();
  });
  backdrop?.addEventListener('click', closeTabsMenu);
  window.addEventListener('resize', () => {
    if (window.matchMedia('(min-width: 768px)').matches) closeTabsMenu();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeTabsMenu();
      closeActionMenus();
    }
  });
}

function closeActionMenus() {
  document.querySelectorAll('.action-menu.open').forEach((m) => m.classList.remove('open'));
}

function toggleActionMenu(btn) {
  const menu = btn.closest('.action-menu');
  if (!menu) return;
  const wasOpen = menu.classList.contains('open');
  closeActionMenus();
  if (!wasOpen) menu.classList.add('open');
}

document.addEventListener('click', (e) => {
  if (!e.target.closest('.action-menu')) closeActionMenus();
});

function switchTab(name) {
  closeTabsMenu();
  updateEditorPanelsLayout(name);
  if (name === 'wifi' && !IS_OPENWRT) {
    showToast('WiFi management is only available on OpenWrt', 'error');
    return;
  }
  if (name === 'adapter' && !IS_APPLE_SILICON) {
    showToast('Adapter is only available on Apple Silicon Mac', 'error');
    return;
  }
  if (name !== 'adapter') stopAdapterPolling();
  document.querySelectorAll('.tab[data-tab]').forEach(tab => {
    tab.classList.toggle('active', tab.dataset.tab === name);
  });
  document.querySelectorAll('.panel').forEach(panel => {
    panel.classList.toggle('active', panel.id === 'tab-' + name);
  });
  const tabLbl = document.getElementById('tabbar-current');
  if (tabLbl) tabLbl.textContent = TAB_LABELS[name] || name;
  lsSet(LS_TAB, name);
  if (name==='env')     loadEnv();
  if (name==='compose') loadCompose();
  if (name==='wifi')    loadWifi();
  if (name==='adapter') { loadAdapter(); startAdapterPolling(); }
  if (name === 'env' || name === 'compose') {
    requestAnimationFrame(layoutCodeEditors);
  }
}

async function api(path, body=null) {
  const opts = body
    ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}
    : {};
  const r = await fetch(path, opts);
  if (r.status === 401) { window.location.href = '/'; return null; }
  return r.json();
}

async function logout() {
  await api('/api/logout', {});
  window.location.href = '/';
}

function applyStatusData(data) {
  currentSelected = data.selected;
  selectedRunning  = data.instances[data.selected] ? data.instances[data.selected].running : false;

  const hdrSel = document.getElementById('hdr-selected');
  hdrSel.textContent = (data.selected || 'none').toUpperCase();
  hdrSel.className   = 'pill pill-instance';

  const hdrSt = document.getElementById('hdr-status');
  hdrSt.textContent = selectedRunning ? 'Running' : 'Stopped';
  hdrSt.className   = 'pill ' + (selectedRunning ? 'pill-on' : 'pill-off');

  document.getElementById('action-selected').textContent = currentSelected ? `— selected: ${currentSelected}` : '— no instances';

  renderInstances(data.instances, currentSelected);
}

async function refreshAll() {
  try {
    const data = await api('/api/status');
    if (!data) return;
    lsSet(LS_STATUS, data);
    applyStatusData(data);
  } catch(e) {}
}

function renderInstances(instances, selected) {
  const container = document.getElementById('instances-container');
  let html = '';
  let idx = 0;
  const animCls = _firstRender ? ' animate-in' : '';
  for (const [name, info] of Object.entries(instances || {})) {
    const isSel = name === selected;
    let primary = '';
    if (isSel) {
      primary = info.running
        ? labeledBtn('btn-sm', `data-action="stop-${name}" onclick="doAction('stop','${name}',this)"`, '⏹', 'Stop')
        : labeledBtn('btn-sm', `data-action="start-${name}" onclick="doAction('start','${name}',this)"`, '▶', 'Start');
    } else {
      primary = labeledBtn('btn-sm', `data-action="activate-${name}" onclick="startSwitchTo('${name}',this)"`, '⇄', 'Switch');
    }

    let menu = '';
    menu += `<button type="button" class="action-menu-item" onclick="showPullModal('${name}');closeActionMenus()">Pull images</button>`;
    if (isSel) {
      menu += `<button type="button" class="action-menu-item" onclick="openModal();closeActionMenus()">Clear data</button>`;
    }
    menu += `<button type="button" class="action-menu-item" onclick="showBackupModal('${name}');closeActionMenus()">Backup</button>`;
    if (info.running) {
      menu += `<button type="button" class="action-menu-item" onclick="openLogsModal('${name}');closeActionMenus()">View logs</button>`;
    }

    const btns = `
      ${primary}
      ${labeledBtn('btn-sm btn-ghost', `onclick="showDeleteModal('${name}')"`, '🗑', 'Delete')}
      <div class="action-menu">
        ${labeledBtn('btn-sm btn-ghost', 'onclick="toggleActionMenu(this)"', '⋮', 'More')}
        <div class="action-menu-pop">${menu}</div>
      </div>`;

    html += `
<article class="instance-card${isSel ? ' selected' : ''}${isSel && name === 'prod' ? ' prod' : ''}${animCls}" style="${_firstRender ? 'animation-delay:'+Math.min(idx*40,200)+'ms' : ''}">
  <div class="instance-head">
    <span class="inst-name${name === 'prod' ? ' inst-name--prod' : ''}">${name}</span>
    <span class="pill ${info.running ? 'pill-on' : 'pill-off'}">${info.running ? 'Running' : 'Stopped'}${isSel ? ' · active' : ''}</span>
  </div>
  <div class="instance-actions">${btns}</div>
</article>`;
    idx++;
  }
  container.innerHTML = html;
  _firstRender = false;
}

function setBtnLoading(btn, loading) {
  if (!btn) return;
  if (loading) {
    btn.disabled = true;
    btn._origHTML = btn.innerHTML;
    btn.innerHTML = '<span class="spinner"></span>' + btn.innerText.replace(/[^\w\s]/g,'').trim();
    _busyButtons.add(btn.dataset.action);
  } else {
    btn.disabled = false;
    if (btn._origHTML) btn.innerHTML = btn._origHTML;
    _busyButtons.delete(btn.dataset.action);
  }
}

function startProgressPolling() {
  _busy = true;
  _lastProgressLogLen = 0;
  lsSet(LS_LOG, '');
  const outBody = document.getElementById('output-body');
  if (outBody) outBody.innerHTML = '';
  if (_pollTimer) clearInterval(_pollTimer);
  document.body.classList.add('action-running');
  openTerminalForAction();
  document.getElementById('progress-modal-overlay').classList.add('show');
  document.getElementById('progress-stages').innerHTML = '';
  document.getElementById('progress-fill').style.width = '0%';
  _pollTimer = setInterval(pollProgress, 600);
}

async function doAction(action, target=null, triggerBtn=null) {
  if (_busy) return;
  setBtnLoading(triggerBtn, true);
  if (action === 'start' && ON_PORT80) {
    document.getElementById('redirect-banner').style.display = 'block';
    setTimeout(() => {
      window.location.href = 'http://' + location.hostname + ':8181/';
    }, 2200);
  }
  const body = target ? {action, instance: target} : {action};
  const data = await api('/api/action', body);
  setBtnLoading(triggerBtn, false);
  if (!data || !data.ok) {
    showToast(data?.error || 'Action failed', 'error');
    return;
  }
  startProgressPolling();
}

// Switch / Activate
async function startSwitchTo(name, triggerBtn=null) {
  if (_busy) return;
  setBtnLoading(triggerBtn, true);
  if (ON_PORT80) {
    document.getElementById('redirect-banner').style.display = 'block';
    setTimeout(() => {
      window.location.href = 'http://' + location.hostname + ':8181/';
    }, 2800);
  }
  await api('/api/switch_to', {target: name});
  setBtnLoading(triggerBtn, false);
  startProgressPolling();
}

function renderProgress(data) {
  const container = document.getElementById('progress-stages');
  container.innerHTML = '';
  let doneCount = 0;
  const icons = {pending:'○', running:'◉', done:'✓', error:'✕'};
  data.stages.forEach(s => {
    if (s.status==='done') doneCount++;
    container.innerHTML += '<div class="stage-row">' +
      '<div class="stage-icon '+s.status+'">'+icons[s.status]+'</div>' +
      '<span class="stage-label '+s.status+'">'+s.label+'</span></div>';
  });
  const pct = data.stages.length ? Math.round(doneCount/data.stages.length*100) : 0;
  document.getElementById('progress-fill').style.width = pct+'%';
}

let _lastProgressLogLen = 0;

async function pollProgress() {
  let data;
  try { data = await api('/api/progress'); } catch(e) { return; }
  if (!data) return;
  renderProgress(data);
  // Live-update output panel while action is running (skip if log unchanged)
  if (data.log && data.log.length !== _lastProgressLogLen) {
    _lastProgressLogLen = data.log.length;
    showOutput(data.log);
  }
  if (data.done) {
    _lastProgressLogLen = 0;
    clearInterval(_pollTimer);
    _pollTimer = null;
    _busy = false;
    setTimeout(() => {
      document.getElementById('progress-modal-overlay').classList.remove('show');
      endActionRunning();
    }, 1200);
    showToast(data.ok ? 'Action completed successfully' : 'Action failed', data.ok ? 'success' : 'error');
    await refreshAll();
  }
}

async function maybeRestoreProgress() {
  const data = await api('/api/progress');
  if (!data || data.done) return;
  _busy = true;
  document.body.classList.add('action-running');
  openTerminalForAction();
  document.getElementById('progress-modal-overlay').classList.add('show');
  renderProgress(data);
  if (data.log) showOutput(data.log);
  _pollTimer = setInterval(pollProgress, 600);
}

// Env
async function loadEnv() {
  const data = await api('/api/env');
  if (!data) return;
  document.getElementById('env-template').value = data.template;
  document.getElementById('env-content').value  = data.content;
  document.getElementById('env-instance-label').textContent = data.selected || 'none';
  setFormMsg(document.getElementById('env-save-msg'), '');
  refreshCodeEditor('env-template');
  refreshCodeEditor('env-content');
}

async function saveEnv() {
  const content = document.getElementById('env-content').value;
  const data    = await api('/api/env/save', {content});
  if (!data) return;
  const msg = document.getElementById('env-save-msg');
  setFormMsg(msg, data.ok ? 'Saved' : ('✗ ' + data.error), data.ok ? 'ok' : 'err');
  if (data.ok && data.restarting) {
    showToast('Saved — restarting compose to apply changes…', 'info');
    startProgressPolling();
  } else {
    showToast(data.ok ? 'Environment saved' : 'Failed to save .env', data.ok ? 'success' : 'error');
  }
}

// Compose
async function loadCompose() {
  const data = await api('/api/compose');
  if (!data) return;
  document.getElementById('compose-content').value = data.content || '';
  document.getElementById('compose-instance-label').textContent = data.selected || 'none';
  setFormMsg(document.getElementById('compose-save-msg'), '');
  refreshCodeEditor('compose-content');
}

function showComposeSaveModal() {
  document.getElementById('compose-save-modal-overlay').classList.add('show');
}
function closeComposeSaveModal() {
  document.getElementById('compose-save-modal-overlay').classList.remove('show');
}

async function confirmComposeSave() {
  closeComposeSaveModal();
  const content = document.getElementById('compose-content').value;
  const data = await api('/api/compose/save', {content});
  if (!data) return;
  const msg = document.getElementById('compose-save-msg');
  setFormMsg(msg, data.ok ? 'Saved' : ('✗ ' + data.error), data.ok ? 'ok' : 'err');
  if (data.ok && data.restarting) {
    showToast('Saved — restarting compose to apply changes…', 'info');
    startProgressPolling();
  } else {
    showToast(data.ok ? 'Compose file saved' : 'Failed to save', data.ok ? 'success' : 'error');
  }
}

// WiFi
function togglePasswordVisibility(inputId, btn) {
  const input = document.getElementById(inputId);
  if (!input || !btn) return;
  const show = input.type === 'password';
  input.type = show ? 'text' : 'password';
  btn.classList.toggle('password-toggle--visible', show);
  btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
  btn.setAttribute('aria-pressed', show ? 'true' : 'false');
}

function resetPasswordVisibility(inputId) {
  const input = document.getElementById(inputId);
  const btn = input?.closest('.password-field')?.querySelector('.password-toggle');
  if (!input || !btn) return;
  input.type = 'password';
  btn.classList.remove('password-toggle--visible');
  btn.setAttribute('aria-label', 'Show password');
  btn.setAttribute('aria-pressed', 'false');
}

async function loadWifi() {
  const msg = document.getElementById('wifi-msg');
  setFormMsg(msg, 'Loading…');
  const data = await api('/api/wifi/current');
  if (!data) return;
  document.getElementById('hs-ssid').value  = data.hs_ssid  || '';
  document.getElementById('hs-psk').value   = data.hs_psk   || '';
  document.getElementById('sta-ssid').value = data.sta_ssid || '';
  document.getElementById('sta-psk').value  = data.sta_psk  || '';
  ['hs-psk', 'sta-psk'].forEach(resetPasswordVisibility);
  document.getElementById('ip-lan').textContent     = data.lan_ip      || '—';
  document.getElementById('ip-wan').textContent     = data.wan_ip      || '—';
  document.getElementById('ip-wan-wifi').textContent = data.wan_wifi_ip || '—';
  if (data.ok) setFormMsg(msg, '');
  else setFormMsg(msg, 'uci unavailable', 'warn');
}

async function applyWifi() {
  if (_busy) return;
  const msg = document.getElementById('wifi-msg');
  setFormMsg(msg, '');
  const body = {
    hs_ssid: document.getElementById('hs-ssid').value,
    hs_psk:  document.getElementById('hs-psk').value,
    sta_ssid:document.getElementById('sta-ssid').value,
    sta_psk: document.getElementById('sta-psk').value,
  };
  const data = await api('/api/wifi', body);
  if (!data || !data.ok) {
    showToast(data?.error || 'WiFi apply failed', 'error');
    setFormMsg(msg, '✗ ' + (data?.error || 'Error'), 'err');
    return;
  }
  startProgressPolling();
}

// Modals
async function showAccountModal() {
  const data = await api('/api/account');
  if (!data) return;
  document.getElementById('account-username').value = data.username || 'root';
  document.getElementById('account-current').value = '';
  document.getElementById('account-new').value = '';
  document.getElementById('account-confirm').value = '';
  setFormMsg(document.getElementById('account-msg'), '');
  const desc = document.getElementById('account-modal-desc');
  if (desc) {
    desc.textContent = data.uses_system_password
      ? 'Changes the OpenWrt root password used for FKM, LuCI, and SSH.'
      : 'Change the manager login password.';
  }
  document.getElementById('account-modal-overlay').classList.add('show');
  document.getElementById('account-current').focus();
}

function closeAccountModal() {
  document.getElementById('account-modal-overlay').classList.remove('show');
}

async function confirmAccountPassword() {
  const msg = document.getElementById('account-msg');
  setFormMsg(msg, '');
  const body = {
    current_password: document.getElementById('account-current').value,
    new_password: document.getElementById('account-new').value,
    confirm_password: document.getElementById('account-confirm').value,
  };
  const data = await api('/api/password', body);
  if (!data) return;
  if (data.ok) {
    closeAccountModal();
    showToast(IS_OPENWRT ? 'OpenWrt password updated' : 'Password updated', 'success');
    return;
  }
  setFormMsg(msg, data.error || 'Password change failed', 'err');
}

function openModal() {
  document.getElementById('modal-confirm-input').value = '';
  document.getElementById('modal-ok-btn').disabled = true;
  document.getElementById('modal-overlay').classList.add('show');
  setTimeout(() => document.getElementById('modal-confirm-input').focus(), 50);
}
function closeModal() { document.getElementById('modal-overlay').classList.remove('show'); }
function checkModalInput() {
  document.getElementById('modal-ok-btn').disabled =
    document.getElementById('modal-confirm-input').value !== 'DELETE';
}
async function confirmDownVolumes() { closeModal(); await doAction('down_volumes'); }

let _pullTarget = null;
function showPullModal(name) {
  _pullTarget = name;
  document.getElementById('pull-modal-overlay').classList.add('show');
}
function closePullModal() { document.getElementById('pull-modal-overlay').classList.remove('show'); }
async function confirmPull(withUp) {
  closePullModal();
  await doAction(withUp ? 'pull_up' : 'pull', _pullTarget);
}

async function showCreateModal() {
  document.getElementById('create-name').value = '';
  document.getElementById('create-ok-btn').disabled = true;
  const sel = document.getElementById('create-template');
  sel.innerHTML = '<option disabled selected>Loading…</option>';
  try {
    const r = await fetch('/api/templates');
    if (r.status === 401) { window.location.href = '/'; return; }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    sel.innerHTML = '';
    if (data?.templates?.length) {
      data.templates.forEach(t => {
        const opt = document.createElement('option');
        opt.value = t; opt.textContent = t;
        sel.appendChild(opt);
      });
    } else {
      sel.innerHTML = '<option disabled selected>No templates found</option>';
    }
  } catch (e) {
    console.error('templates load failed', e);
    sel.innerHTML = '<option disabled selected>Failed to load templates</option>';
  }
  document.getElementById('create-modal-overlay').classList.add('show');
}
function closeCreateModal() { document.getElementById('create-modal-overlay').classList.remove('show'); }
function checkCreateReady() {
  document.getElementById('create-ok-btn').disabled = !document.getElementById('create-name').value.trim();
}
function confirmCreate() {
  const name = document.getElementById('create-name').value.trim();
  const templ = document.getElementById('create-template').value;
  closeCreateModal();
  createNewInstance(name, templ);
}
async function createNewInstance(name, template) {
  const data = await api('/api/instance/create', {name, template});
  if (!data) return;
  if (data.ok) {
    showToast(`Instance "${name}" created`, 'success');
    await refreshAll();
  } else {
    showToast('Create failed: ' + (data.error || 'Unknown error'), 'error');
  }
}

function showDeleteModal(name) {
  document.getElementById('delete-instance-name').textContent = name;
  document.getElementById('delete-confirm-input').value = '';
  document.getElementById('delete-ok-btn').disabled = true;
  document.getElementById('delete-modal-overlay').classList.add('show');
}
function closeDeleteModal() { document.getElementById('delete-modal-overlay').classList.remove('show'); }
function checkDeleteInput() {
  document.getElementById('delete-ok-btn').disabled =
    document.getElementById('delete-confirm-input').value !== 'DELETE';
}
function confirmDeleteInstance() {
  const name = document.getElementById('delete-instance-name').textContent;
  closeDeleteModal();
  deleteInstance(name);
}
async function deleteInstance(name) {
  if (_busy) return;
  const data = await api('/api/instance/delete', {name});
  if (!data || !data.ok) {
    showToast('Delete failed: ' + (data?.error || 'Unknown error'), 'error');
    return;
  }
  startProgressPolling();
}

let _backupTarget = null;
function showBackupModal(name) {
  _backupTarget = name;
  document.getElementById('backup-instance-name').textContent = name;
  document.getElementById('backup-modal-overlay').classList.add('show');
}
function closeBackupModal() { document.getElementById('backup-modal-overlay').classList.remove('show'); }
async function confirmBackup() {
  closeBackupModal();
  if (_busy || !_backupTarget) return;
  const name = _backupTarget;
  const data = await api('/api/instance/backup', {name});
  if (!data || !data.ok) {
    showToast('Backup failed: ' + (data?.error || 'Unknown error'), 'error');
    return;
  }
  startProgressPolling();
  // Poll for completion and trigger download
  const dlPoll = setInterval(async () => {
    let pg;
    try { pg = await api('/api/progress'); } catch(e) { return; }
    if (!pg || !pg.done) return;
    clearInterval(dlPoll);
    if (pg.ok) {
      // Trigger download via hidden link
      const a = document.createElement('a');
      a.href = '/api/instance/backup/download?name=' + encodeURIComponent(name);
      a.download = name + '.tar.gz';
      document.body.appendChild(a);
      a.click();
      a.remove();
      showToast('Backup download started', 'success');
    }
  }, 800);
}

// Logs modal — batch DOM updates to avoid freezing on bursty SSE
let _logsEventSource = null;
let _logsPending = '';
let _logsDisplayed = '';
let _logsFlushScheduled = false;

function scheduleLogsFlush() {
  if (_logsFlushScheduled) return;
  _logsFlushScheduled = true;
  requestAnimationFrame(flushLogsToDom);
}

function flushLogsToDom() {
  _logsFlushScheduled = false;
  if (!_logsPending) return;
  const body = document.getElementById('logs-body');
  const stickToBottom = body.scrollHeight - body.clientHeight - body.scrollTop <= 48;
  _logsDisplayed += _logsPending;
  _logsPending = '';
  _logsDisplayed = trimLogLines(_logsDisplayed, LOG_MAX_LINES);
  body.innerHTML = ansiToHtml(_logsDisplayed);
  if (stickToBottom) body.scrollTop = body.scrollHeight;
}

function openLogsModal(name) {
  document.getElementById('logs-instance-name').textContent = name;
  const body = document.getElementById('logs-body');
  body.innerHTML = '';
  _logsPending = '';
  _logsDisplayed = '';
  document.getElementById('logs-modal-overlay').classList.add('show');
  if (_logsEventSource) { _logsEventSource.close(); _logsEventSource = null; }
  _logsEventSource = new EventSource('/api/logs?name=' + encodeURIComponent(name));
  _logsEventSource.onmessage = function(e) {
    _logsPending += e.data + '\n';
    scheduleLogsFlush();
  };
  _logsEventSource.onerror = function() {
    flushLogsToDom();
    _logsEventSource.close();
    _logsEventSource = null;
  };
}
function closeLogsModal() {
  document.getElementById('logs-modal-overlay').classList.remove('show');
  flushLogsToDom();
  _logsPending = '';
  _logsDisplayed = '';
  if (_logsEventSource) { _logsEventSource.close(); _logsEventSource = null; }
}

// ── Adapter tab JS ─────────────────────────────────────────────────────
let _adapterPollTimer = null;

async function loadAdapter() {
  const data = await api('/api/adapter');
  if (data) applyAdapterState(data);
}

function applyAdapterState(data) {
  const badge   = document.getElementById('adapter-status-badge');
  const btn     = document.getElementById('adapter-toggle-btn');
  const dlRow   = document.getElementById('adapter-download-row');
  const errRow  = document.getElementById('adapter-error-row');
  const logWrap = document.getElementById('adapter-log-wrap');
  const logEl   = document.getElementById('adapter-log');
  const pidEl   = document.getElementById('adapter-pid');

  if (data.installing) {
    badge.textContent = 'Installing';
    badge.className   = 'pill';
  } else if (data.running) {
    badge.textContent = 'Running';
    badge.className   = 'pill pill-on';
  } else if (data.enabled) {
    badge.textContent = 'Starting';
    badge.className   = 'pill';
  } else {
    badge.textContent = 'Disabled';
    badge.className   = 'pill pill-off';
  }

  btn.textContent = data.enabled ? 'Disable' : 'Enable';
  btn.className   = 'btn btn-sm';

  dlRow.style.display  = data.downloading ? 'block' : 'none';
  errRow.style.display = data.error       ? 'block' : 'none';
  if (data.error) errRow.textContent = '✗ ' + data.error;

  pidEl.textContent = data.pid ? `PID: ${data.pid}` : '';

  if (data.log) {
    logWrap.style.display = 'block';
    logEl.textContent = data.log;
    logEl.scrollTop   = logEl.scrollHeight;
  } else if (!data.enabled) {
    logWrap.style.display = 'none';
  }
}

async function toggleAdapter() {
  const btn = document.getElementById('adapter-toggle-btn');
  btn.disabled = true;
  const cur = await api('/api/adapter');
  if (!cur) { btn.disabled = false; return; }
  const result = await api('/api/adapter/set', {enabled: !cur.enabled});
  btn.disabled = false;
  if (!result) return;
  applyAdapterState(result);
  if (result.enabled) startAdapterPolling();
  else stopAdapterPolling();
}

function startAdapterPolling() {
  if (_adapterPollTimer) return;
  _adapterPollTimer = setInterval(async () => {
    const data = await api('/api/adapter');
    if (data) applyAdapterState(data);
  }, 2000);
}

function stopAdapterPolling() {
  if (_adapterPollTimer) { clearInterval(_adapterPollTimer); _adapterPollTimer = null; }
}

// ── Boot: restore saved state then fetch live data ─────────────────────
(async () => {
  initTerminal();
  initTabMenu();
  initCodeEditors();

  if (!IS_OPENWRT) {
    document.getElementById('tab-wifi-btn').classList.add('tab-disabled');
  }
  // Disable Adapter tab when not on Apple Silicon
  if (!IS_APPLE_SILICON) {
    document.getElementById('tab-adapter-btn').classList.add('tab-disabled');
  }

  // Restore saved tab (skip wifi on non-OpenWrt, adapter on non-Apple-Silicon, default to control)
  const savedTab = lsGet(LS_TAB, 'control');
  const canRestore = savedTab !== 'control'
    && (IS_OPENWRT        || savedTab !== 'wifi')
    && (IS_APPLE_SILICON  || savedTab !== 'adapter');
  if (canRestore) switchTab(savedTab);
  else updateEditorPanelsLayout('control');

  // Restore cached status instantly (prevents blank screen on reload)
  const cached = lsGet(LS_STATUS, null);
  if (cached && cached.instances) {
    applyStatusData(cached);
  }

  // Terminal open state + log restored in initTerminal()

  await maybeRestoreProgress();
  await refreshAll();
  setInterval(refreshAll, 10000);
})();
