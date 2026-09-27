/* Mystic Dashboard — App Logic */
(function(){
'use strict';

// ── State ──
let currentView = 'chat';
let currentThread = null;
let threads = [];
let cloudPath = '';
let models = {};
let tunnelRunning = false;
let modalCallback = null;
let pendingAttachments = [];
let providers = [];
let colorPickerTarget = null;
let terminalHistory = [];
let terminalHistoryIdx = -1;

// Preset colors for provider/model color coding
const PRESET_COLORS = [
  '#7c3aed', '#3b82f6', '#06b6d4', '#10b981', '#84cc16', '#eab308',
  '#f59e0b', '#ef4444', '#ec4899', '#6366f1', '#14b8a6', '#f97316',
];

// ── Helpers ──
const $ = id => document.getElementById(id);
const api = async (url, opts = {}) => {
  const headers = { ...opts.headers };
  if (!(opts.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const r = await fetch(url, { ...opts, headers });
  if (r.status === 401) { showLogin(); throw new Error('Unauthorized'); }
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    const txt = await r.text();
    throw new Error('Server error (' + r.status + '): ' + txt.slice(0, 120));
  }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || 'HTTP ' + r.status);
  return data;
};
function formatBytes(b) {
  if (!b) return '0 B';
  const u = ['B','KB','MB','GB'];
  const i = Math.floor(Math.log(b) / Math.log(1024));
  return (b / Math.pow(1024, i)).toFixed(i ? 1 : 0) + ' ' + u[i];
}
function timeAgo(iso) {
  if (!iso) return '';
  const s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return Math.floor(s/60) + 'm';
  if (s < 86400) return Math.floor(s/3600) + 'h';
  return Math.floor(s/86400) + 'd';
}
function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}
function renderMd(text) {
  if (!text) return '';
  let h = escHtml(text);
  // Code blocks
  h = h.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) =>
    '<pre><code>' + code.trim() + '</code></pre>');
  // Inline code
  h = h.replace(/`([^`]+)`/g, '<code>$1</code>');
  // Bold
  h = h.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Italic
  h = h.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // Links
  h = h.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
  // Line breaks
  h = h.replace(/\n/g, '<br>');
  return h;
}

// ── Auth ──
async function checkAuth() {
  try {
    const r = await api('/api/auth/check');
    if (r.authenticated) { showApp(); return; }
  } catch(e) {}
  // Check URL key param
  const p = new URLSearchParams(location.search);
  if (p.get('key')) {
    try {
      const r = await fetch('/api/auth/login', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({key: p.get('key')})
      });
      const d = await r.json();
      if (d.authenticated) {
        history.replaceState(null, '', '/');
        showApp(); return;
      }
    } catch(e) {}
  }
  showLogin();
}

function showLogin() {
  $('loginScreen').classList.remove('hidden');
  $('app').style.display = 'none';
}

function showApp() {
  $('loginScreen').classList.add('hidden');
  $('app').style.display = 'flex';
  initApp();
}

window.doLogin = async function() {
  const key = $('loginKey').value.trim();
  if (!key) return;
  $('loginBtn').textContent = 'Signing in...';
  $('loginError').textContent = '';
  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({key})
    });
    const d = await r.json();
    if (d.authenticated) { showApp(); }
    else { $('loginError').textContent = 'Invalid API key'; }
  } catch(e) {
    $('loginError').textContent = 'Connection error';
  }
  $('loginBtn').textContent = 'Sign In';
};

// ── Init ──
let inited = false;
function initApp() {
  if (inited) return;
  inited = true;
  loadThreads();
  loadModels();
  loadSkills();
  loadStatus();
  loadTunnelStatus();
  loadProviders();
  loadMCPServers();
  initColorPicker();
  // Refresh status every 30s
  setInterval(loadStatus, 30000);
  // Setup drag-drop
  setupDragDrop();
  // Input listener
  $('chatInput').addEventListener('input', () => {
    $('sendBtn').disabled = !($('chatInput').value.trim() || pendingAttachments.length);
    $('charCount').textContent = $('chatInput').value.length ? $('chatInput').value.length + ' chars' : '';
  });
}

// ── View Switching ──
window.switchView = function(view) {
  currentView = view;
  $('navChat').classList.toggle('active', view === 'chat');
  $('navCloud').classList.toggle('active', view === 'cloud');
  $('navTerminal').classList.toggle('active', view === 'terminal');

  // Hide all views first
  $('welcomeScreen').classList.add('hidden');
  $('chatMessages').classList.add('hidden');
  $('inputArea').style.display = 'none';
  $('cloudView').classList.remove('active');
  $('terminalView').classList.remove('active');

  if (view === 'chat') {
    $('inputArea').style.display = '';
    if (currentThread) {
      $('chatMessages').classList.remove('hidden');
    } else {
      $('welcomeScreen').classList.remove('hidden');
    }
  } else if (view === 'cloud') {
    $('cloudView').classList.add('active');
    loadFiles();
  } else if (view === 'terminal') {
    $('terminalView').classList.add('active');
    $('terminalInput').focus();
  }

  // Close sidebar on mobile
  if (window.innerWidth <= 768) $('sidebar').classList.add('collapsed');
};

// ── Sidebar ──
window.toggleSidebar = function() {
  $('sidebar').classList.toggle('collapsed');
};

window.toggleSettings = function() {
  const p = $('settingsPanel');
  const isCollapsed = p.classList.contains('collapsed');
  p.classList.toggle('collapsed');
  $('toggleSettings').classList.toggle('active', isCollapsed);
  // Show/hide backdrop on tablet
  const backdrop = $('settingsBackdrop');
  if (backdrop) {
    backdrop.classList.toggle('active', isCollapsed);
  }
};

// ── Threads ──
async function loadThreads() {
  try {
    threads = await api('/api/threads');
    renderThreads();
  } catch(e) { console.error('loadThreads', e); }
}

function renderThreads() {
  const el = $('threadList');
  if (!threads.length) {
    el.innerHTML = '<div style="padding:12px;font-size:12px;color:var(--text-ter);text-align:center">No threads yet</div>';
    return;
  }
  el.innerHTML = threads.map(t => `
    <div class="thread-item ${currentThread && currentThread.id === t.id ? 'active' : ''}"
         onclick="openThread('${t.id}')">
      <span class="title">${escHtml(t.title)}</span>
      <span class="time">${timeAgo(t.updated_at)}</span>
      <button class="thread-delete" onclick="event.stopPropagation();deleteThread('${t.id}')" title="Delete">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
      </button>
    </div>
  `).join('');
}

window.openThread = async function(id) {
  try {
    const t = await api('/api/threads/' + id);
    if (!t || t.error) { console.error('Thread load failed:', t); return; }
    currentThread = t;
    $('headerTitle').textContent = t.title || 'Thread';
    switchView('chat');
    $('chatMessages').classList.remove('hidden');
    $('welcomeScreen').classList.add('hidden');
    renderMessages();
    renderThreads();
    if (window.innerWidth <= 768) $('sidebar').classList.add('collapsed');
    
    // If the last message is from the user, the AI is still generating.
    if (t.messages && t.messages.length > 0 && t.messages[t.messages.length - 1].role === 'user') {
      showTypingIndicator();
      pollThread(id);
    }
  } catch(e) {
    console.error('openThread', e);
    currentThread = null;
    $('chatMessages').innerHTML = '<div class="msg assistant"><div class="msg-avatar">M</div><div class="msg-bubble">\u26a0\ufe0f Failed to load thread: ' + escHtml(e.message) + '</div></div>';
    $('chatMessages').classList.remove('hidden');
    $('welcomeScreen').classList.add('hidden');
  }
};

window.deleteThread = async function(id) {
  if (!confirm('Delete this thread?')) return;
  try {
    await api('/api/threads/' + id, { method: 'DELETE' });
    if (currentThread && currentThread.id === id) {
      currentThread = null;
      $('headerTitle').textContent = 'New thread';
      $('chatMessages').classList.add('hidden');
      $('welcomeScreen').classList.remove('hidden');
    }
    loadThreads();
  } catch(e) { console.error(e); }
};

function renderAttachments(atts) {
  if (!atts || !atts.length) return '';
  return '<div class="msg-attachments">' + atts.map(a => {
    if (a.is_image) return '<a href="' + a.url + '" target="_blank"><img class="msg-img" src="' + a.url + '" alt="' + escHtml(a.name) + '"></a>';
    return '<a class="msg-file" href="' + a.url + '" download><span class="msg-file-icon">\ud83d\udcce</span>' + escHtml(a.name) + '</a>';
  }).join('') + '</div>';
}

function renderTrace(trace) {
  if (!trace || !trace.length) return '';
  const traceId = 'trace-' + Math.random().toString(36).slice(2, 8);
  let items = trace.map(t => {
    const icon = t.type === 'reasoning' ? '🧠' : t.type === 'tool_call' ? '🛠️' : '📄';
    const label = t.type === 'reasoning' ? 'Reasoning' : t.type === 'tool_call' ? ('Called: ' + (t.name || '')) : ('Result: ' + (t.name || ''));
    let content = '';
    if (t.type === 'reasoning') {
      content = '<div class="trace-text">' + escHtml(t.content || '') + '</div>';
    } else if (t.type === 'tool_call') {
      content = '<div class="trace-text">' + escHtml(t.name || '') + '</div>';
      if (t.arguments && Object.keys(t.arguments).length) {
        content += '<div class="trace-args">' + escHtml(JSON.stringify(t.arguments, null, 2)) + '</div>';
      }
    } else if (t.type === 'tool_result') {
      content = '<div class="trace-text">' + escHtml(t.result || '') + '</div>';
    }
    return `<div class="trace-item ${t.type}">
      <div class="trace-icon">${icon}</div>
      <div class="trace-content">
        <div class="trace-label">${escHtml(label)}</div>
        ${content}
      </div>
    </div>`;
  }).join('');

  return `<div class="trace-container">
    <button class="trace-toggle" onclick="toggleTrace('${traceId}', this)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
      ${trace.length} agent step${trace.length !== 1 ? 's' : ''}
    </button>
    <div class="trace-items" id="${traceId}">${items}</div>
  </div>`;
}

window.toggleTrace = function(id, btn) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle('open');
  btn.classList.toggle('open');
};

function renderMessages() {
  if (!currentThread) return;
  const el = $('chatMessages');
  el.innerHTML = currentThread.messages.map(m => `
    <div class="msg ${m.role}">
      <div class="msg-avatar">${m.role === 'user' ? 'U' : 'M'}</div>
      <div class="msg-bubble">${renderMd(m.content)}${renderAttachments(m.attachments)}${m.role === 'assistant' ? renderTrace(m.trace) : ''}</div>
    </div>
  `).join('');
  el.scrollTop = el.scrollHeight;
}

// ── Chat ──
window.handleKey = function(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
};

window.autoResize = function(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 150) + 'px';
};

let pollTimer = null;
async function pollThread(id) {
  if (pollTimer) clearTimeout(pollTimer);
  const check = async () => {
    if (!currentThread || currentThread.id !== id) return;
    try {
      const t = await api('/api/threads/' + id);
      if (t && t.messages && t.messages.length > currentThread.messages.length) {
        const ti = $('typingMsg');
        if (ti) ti.remove();
        currentThread = t;
        renderMessages();
        loadThreads();
        return; // done polling
      }
    } catch(e) {}
    pollTimer = setTimeout(check, 2000);
  };
  pollTimer = setTimeout(check, 2000);
}

function showTypingIndicator() {
  const el = $('chatMessages');
  if ($('typingMsg')) return;
  const typing = document.createElement('div');
  typing.className = 'msg assistant';
  typing.id = 'typingMsg';
  typing.innerHTML = '<div class="msg-avatar">M</div><div class="msg-bubble"><div class="typing-indicator"><div class="typing-dot"></div><div class="typing-dot"></div><div class="typing-dot"></div></div></div>';
  el.appendChild(typing);
  el.scrollTop = el.scrollHeight;
}

window.sendMessage = async function() {
  const text = $('chatInput').value.trim();
  if (!text && !pendingAttachments.length) return;

  // Create thread if needed
  if (!currentThread) {
    try {
      const t = await api('/api/threads', {
        method: 'POST',
        body: JSON.stringify({ title: (text || 'File upload').slice(0, 50) })
      });
      currentThread = { ...t, messages: [] };
      $('headerTitle').textContent = t.title;
      $('chatMessages').classList.remove('hidden');
      $('welcomeScreen').classList.add('hidden');
      loadThreads();
    } catch(e) { console.error(e); return; }
  }

  // Upload attachments first
  let uploadedAtts = null;
  if (pendingAttachments.length) {
    try {
      const fd = new FormData();
      for (const f of pendingAttachments) fd.append('file', f);
      const upRes = await api('/api/chat/upload', { method: 'POST', body: fd });
      uploadedAtts = upRes.attachments || [];
    } catch(e) {
      console.error('Upload failed:', e);
      uploadedAtts = [];
    }
  }

  // Add user message to UI
  const userMsg = { role: 'user', content: text || '(files)', id: 'temp' };
  if (uploadedAtts && uploadedAtts.length) userMsg.attachments = uploadedAtts;
  currentThread.messages.push(userMsg);
  renderMessages();
  $('chatInput').value = '';
  $('chatInput').style.height = 'auto';
  $('sendBtn').disabled = true;
  $('charCount').textContent = '';
  pendingAttachments = [];
  renderAttachmentPreview();

  // Show typing indicator
  showTypingIndicator();

  // Send to API
  try {
    const payload = { thread_id: currentThread.id, message: text || '(files attached)' };
    if (uploadedAtts && uploadedAtts.length) payload.attachments = uploadedAtts;
    const r = await api('/api/chat', {
      method: 'POST',
      body: JSON.stringify(payload)
    });
    
    if (r.status === "processing") {
      pollThread(currentThread.id);
    } else {
      const ti = $('typingMsg');
      if (ti) ti.remove();
      if (r.assistant_message) {
        currentThread.messages.push(r.assistant_message);
      }
      renderMessages();
      loadThreads();
    }
  } catch(e) {
    const ti = $('typingMsg');
    if (ti) ti.remove();
    currentThread.messages.push({ role: 'assistant', content: '\u26a0\ufe0f Failed to get response: ' + e.message });
    renderMessages();
  }
};

// "New thread" via nav button
$('navChat').addEventListener('click', () => {
  if (currentView === 'chat') {
    currentThread = null;
    $('headerTitle').textContent = 'New thread';
    $('chatMessages').classList.add('hidden');
    $('chatMessages').innerHTML = '';
    $('welcomeScreen').classList.remove('hidden');
    pendingAttachments = [];
    renderAttachmentPreview();
    renderThreads();
  }
});

// ── Attachments ──
window.addFiles = function() {
  const inp = $('chatFileInput');
  if (inp) inp.click();
};
window.handleChatFiles = function(input) {
  if (!input.files) return;
  for (const f of input.files) pendingAttachments.push(f);
  input.value = '';
  renderAttachmentPreview();
  $('sendBtn').disabled = !($('chatInput').value.trim() || pendingAttachments.length);
};
window.removeAttachment = function(idx) {
  pendingAttachments.splice(idx, 1);
  renderAttachmentPreview();
  $('sendBtn').disabled = !($('chatInput').value.trim() || pendingAttachments.length);
};
function renderAttachmentPreview() {
  const el = $('attachmentPreview');
  if (!el) return;
  if (!pendingAttachments.length) { el.innerHTML = ''; el.style.display = 'none'; return; }
  el.style.display = 'flex';
  el.innerHTML = pendingAttachments.map((f, i) => {
    const isImg = f.type.startsWith('image/');
    const thumb = isImg ? '<img src="' + URL.createObjectURL(f) + '" class="att-thumb">' : '<span class="att-icon">\ud83d\udcce</span>';
    return '<div class="att-chip">' + thumb + '<span class="att-name">' + escHtml(f.name) + '</span><button class="att-remove" onclick="removeAttachment(' + i + ')">\u00d7</button></div>';
  }).join('');
}

// ── Models ──
async function loadModels() {
  try {
    models = await api('/api/models');
    document.querySelectorAll('.model-value').forEach(el => {
      const role = el.dataset.role;
      if (models[role]) el.textContent = models[role];
    });
    // Populate model selector
    const sel = $('modelSelect');
    sel.innerHTML = '';
    const agentModel = models.agent || models.coder || 'z-ai/glm5';
    [agentModel, 'z-ai/glm5', 'meta/llama3-70b-instruct', 'deepseek-ai/deepseek-v3.2'].forEach(m => {
      const o = document.createElement('option');
      o.value = m; o.textContent = m.split('/').pop();
      sel.appendChild(o);
    });
  } catch(e) { console.error('loadModels', e); }
}

window.editModel = function(el) {
  const role = el.dataset.role;
  const current = el.textContent;
  const input = document.createElement('input');
  input.className = 'model-input';
  input.value = current;
  el.replaceWith(input);
  input.focus();
  input.select();

  const save = async () => {
    const val = input.value.trim();
    if (val && val !== current) {
      try {
        await api('/api/models', {
          method: 'POST',
          body: JSON.stringify({ [role]: val })
        });
      } catch(e) { console.error(e); }
    }
    const span = document.createElement('span');
    span.className = 'model-value';
    span.dataset.role = role;
    span.textContent = val || current;
    span.onclick = () => editModel(span);
    input.replaceWith(span);
    loadModels();
  };

  input.addEventListener('blur', save);
  input.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); save(); } if (e.key === 'Escape') { input.value = current; save(); } });
};

// ── Skills ──
async function loadSkills() {
  try {
    const skills = await api('/api/skills');
    const el = $('skillsList');
    if (!skills.length) { el.innerHTML = '<div style="font-size:12px;color:var(--text-ter)">No skills loaded</div>'; return; }
    el.innerHTML = skills.map(s => `
      <div class="skill-item">
        <div class="skill-icon">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>
        </div>
        <div class="skill-info">
          <div class="skill-name">${escHtml(s.name)}</div>
          <div class="skill-desc">${escHtml(s.description)}</div>
        </div>
      </div>
    `).join('');
  } catch(e) { console.error('loadSkills', e); }
}

// ── Status ──
async function loadStatus() {
  try {
    const s = await api('/api/status');
    $('sUptime').textContent = s.uptime || '—';
    $('sCpu').textContent = s.cpu_percent != null ? s.cpu_percent + '%' : '—';
    if (s.memory) {
      $('sMem').textContent = s.memory.used_mb + ' / ' + s.memory.total_mb + ' MB';
      $('sMemBar').style.width = s.memory.percent + '%';
    }
    if (s.disk) {
      $('sDisk').textContent = s.disk.used_gb + ' / ' + s.disk.total_gb + ' GB';
      $('sDiskBar').style.width = s.disk.percent + '%';
    }
    if (s.tunnel && s.tunnel.api_key) {
      $('apiKeyDisplay').textContent = s.tunnel.api_key;
    }
  } catch(e) { console.error('loadStatus', e); }
}

// ── Terminal ──
window.handleTerminalKey = function(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    const cmd = $('terminalInput').value.trim();
    if (!cmd) return;
    terminalHistory.push(cmd);
    terminalHistoryIdx = terminalHistory.length;
    $('terminalInput').value = '';
    runTerminalCommand(cmd);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (terminalHistoryIdx > 0) {
      terminalHistoryIdx--;
      $('terminalInput').value = terminalHistory[terminalHistoryIdx];
    }
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (terminalHistoryIdx < terminalHistory.length - 1) {
      terminalHistoryIdx++;
      $('terminalInput').value = terminalHistory[terminalHistoryIdx];
    } else {
      terminalHistoryIdx = terminalHistory.length;
      $('terminalInput').value = '';
    }
  }
};

async function runTerminalCommand(cmd) {
  const output = $('terminalOutput');
  // Show the command
  output.innerHTML += '<div class="cmd-line">' + escHtml(cmd) + '</div>';
  output.scrollTop = output.scrollHeight;

  try {
    const r = await api('/api/terminal/exec', {
      method: 'POST',
      body: JSON.stringify({ command: cmd })
    });

    if (r.stdout) {
      output.innerHTML += '<div class="stdout">' + escHtml(r.stdout) + '</div>';
    }
    if (r.stderr) {
      output.innerHTML += '<div class="stderr">' + escHtml(r.stderr) + '</div>';
    }
    const exitColor = r.exit_code === 0 ? '#10b981' : '#ef4444';
    output.innerHTML += '<div class="exit-code" style="color:' + exitColor + '">exit ' + r.exit_code + '</div>';
  } catch(e) {
    output.innerHTML += '<div class="stderr">Error: ' + escHtml(e.message) + '</div>';
    output.innerHTML += '<div class="exit-code" style="color:#ef4444">failed</div>';
  }

  output.scrollTop = output.scrollHeight;
}

// ── MCP Servers ──
async function loadMCPServers() {
  try {
    const r = await api('/api/mcp/servers');
    renderMCPServers(r.servers || []);
  } catch(e) { console.error('loadMCPServers', e); }
}

function renderMCPServers(servers) {
  const el = $('mcpServersList');
  if (!servers.length) {
    el.innerHTML = '<div style="font-size:12px;color:var(--text-ter)">No MCP servers configured</div>';
    return;
  }
  el.innerHTML = servers.map(s => `
    <div class="mcp-server">
      <div class="mcp-status ${s.connected ? 'connected' : 'disconnected'}"></div>
      <div class="mcp-info">
        <div class="mcp-name">${escHtml(s.name)}</div>
        <div class="mcp-detail">${s.connected ? s.tool_count + ' tools' : escHtml(s.command || s.transport)}</div>
      </div>
      <div class="mcp-actions">
        ${s.connected
          ? '<button class="mcp-btn" onclick="mcpDisconnect(\'' + s.id + '\')">Disconnect</button>'
          : '<button class="mcp-btn" onclick="mcpConnect(\'' + s.id + '\')">Connect</button>'
        }
        <button class="mcp-btn danger" onclick="mcpRemove('${s.id}')">✕</button>
      </div>
    </div>
  `).join('');
}

window.mcpConnect = async function(id) {
  try {
    await api('/api/mcp/servers/' + id + '/connect', { method: 'POST' });
    loadMCPServers();
  } catch(e) { alert('Connection failed: ' + e.message); }
};

window.mcpDisconnect = async function(id) {
  try {
    await api('/api/mcp/servers/' + id + '/disconnect', { method: 'POST' });
    loadMCPServers();
  } catch(e) { alert('Disconnect failed: ' + e.message); }
};

window.mcpRemove = async function(id) {
  if (!confirm('Remove this MCP server?')) return;
  try {
    await api('/api/mcp/servers/' + id, { method: 'DELETE' });
    loadMCPServers();
  } catch(e) { alert('Remove failed: ' + e.message); }
};

window.openAddMCP = function() {
  $('modalTitle').textContent = 'Add MCP Server';
  $('modalInput').value = '';
  $('modalInput').placeholder = 'Server name...';
  $('modalConfirm').textContent = 'Add';

  $('modalExtraFields').innerHTML = `
    <input class="modal-input" id="mcpCommand" placeholder="Command (e.g. npx, python)...">
    <input class="modal-input" id="mcpArgs" placeholder="Arguments (comma-separated)...">
  `;

  modalCallback = async (name) => {
    if (!name) return;
    const cmd = $('mcpCommand').value.trim();
    const argsStr = $('mcpArgs').value.trim();
    const args = argsStr ? argsStr.split(',').map(a => a.trim()) : [];
    try {
      await api('/api/mcp/servers', {
        method: 'POST',
        body: JSON.stringify({ name, command: cmd, args })
      });
      loadMCPServers();
    } catch(e) { alert('Add failed: ' + e.message); }
  };

  $('modalOverlay').classList.add('active');
  setTimeout(() => $('modalInput').focus(), 100);
};

// ── Providers ──
async function loadProviders() {
  try {
    providers = await api('/api/providers');
    renderProviders();
  } catch(e) { console.error('loadProviders', e); }
}

function renderProviders() {
  const el = $('providersList');
  if (!providers.length) {
    el.innerHTML = '<div style="font-size:12px;color:var(--text-ter)">No providers configured</div>';
    return;
  }
  el.innerHTML = providers.map(p => `
    <div class="provider-item" style="border-left-color:${p.color || '#7c3aed'}">
      <div class="provider-color" style="background:${p.color || '#7c3aed'}" onclick="openColorPickerForProvider('${p.id}')" title="Change color"></div>
      <div class="provider-info">
        <div class="provider-name">${escHtml(p.name || 'Unnamed')}</div>
        <div class="provider-url">${escHtml(p.base_url || '')}</div>
        <div class="provider-key">${p.api_key ? '••••' + p.api_key.slice(-6) : 'No key'}</div>
      </div>
      <div class="provider-actions">
        <button class="mcp-btn" onclick="editProvider('${p.id}')">Edit</button>
        <button class="mcp-btn danger" onclick="removeProvider('${p.id}')">✕</button>
      </div>
    </div>
  `).join('');

  // Update model color dots to match providers
  updateModelColorDots();
}

function updateModelColorDots() {
  // Match model values to provider colors
  document.querySelectorAll('.model-color-dot').forEach(dot => {
    const role = dot.dataset.role;
    const modelEl = document.querySelector('.model-value[data-role="' + role + '"]');
    if (!modelEl) return;
    const modelName = modelEl.textContent;
    // Find a provider whose color matches or whose models include this one
    const matched = providers.find(p => p.models && p.models.includes(modelName));
    if (matched && matched.color) {
      dot.style.background = matched.color;
    }
  });
}

window.openAddProvider = function() {
  $('modalTitle').textContent = 'Add Provider';
  $('modalInput').value = '';
  $('modalInput').placeholder = 'Provider name (e.g. OpenRouter)...';
  $('modalConfirm').textContent = 'Add';

  $('modalExtraFields').innerHTML = `
    <input class="modal-input" id="providerUrl" placeholder="Base URL (e.g. https://openrouter.ai/api/v1)...">
    <input class="modal-input" id="providerKey" type="password" placeholder="API Key...">
  `;

  modalCallback = async (name) => {
    if (!name) return;
    const base_url = $('providerUrl').value.trim();
    const api_key = $('providerKey').value.trim();
    const color = PRESET_COLORS[Math.floor(Math.random() * PRESET_COLORS.length)];
    try {
      await api('/api/providers', {
        method: 'POST',
        body: JSON.stringify({ name, base_url, api_key, color })
      });
      loadProviders();
    } catch(e) { alert('Add failed: ' + e.message); }
  };

  $('modalOverlay').classList.add('active');
  setTimeout(() => $('modalInput').focus(), 100);
};

window.editProvider = function(id) {
  const p = providers.find(x => x.id === id);
  if (!p) return;

  $('modalTitle').textContent = 'Edit Provider';
  $('modalInput').value = p.name || '';
  $('modalInput').placeholder = 'Provider name...';
  $('modalConfirm').textContent = 'Save';

  $('modalExtraFields').innerHTML = `
    <input class="modal-input" id="providerUrl" placeholder="Base URL..." value="${escHtml(p.base_url || '')}">
    <input class="modal-input" id="providerKey" type="password" placeholder="API Key..." value="${escHtml(p.api_key || '')}">
  `;

  modalCallback = async (name) => {
    if (!name) return;
    const base_url = $('providerUrl').value.trim();
    const api_key = $('providerKey').value.trim();
    try {
      await api('/api/providers', {
        method: 'POST',
        body: JSON.stringify({ id: p.id, name, base_url, api_key, color: p.color })
      });
      loadProviders();
    } catch(e) { alert('Save failed: ' + e.message); }
  };

  $('modalOverlay').classList.add('active');
  setTimeout(() => $('modalInput').focus(), 100);
};

window.removeProvider = async function(id) {
  if (!confirm('Remove this provider?')) return;
  try {
    await api('/api/providers/' + id, { method: 'DELETE' });
    loadProviders();
  } catch(e) { alert('Remove failed: ' + e.message); }
};

// ── Color Picker ──
function initColorPicker() {
  const grid = $('colorGrid');
  grid.innerHTML = PRESET_COLORS.map(c =>
    '<div class="color-swatch" style="background:' + c + '" data-color="' + c + '" onclick="selectColor(\'' + c + '\')"></div>'
  ).join('');
}

window.openColorPicker = function(dotEl) {
  colorPickerTarget = { type: 'model', element: dotEl, role: dotEl.dataset.role };
  const current = dotEl.style.background;
  highlightSwatch(current);
  $('colorPickerOverlay').classList.add('active');
};

window.openColorPickerForProvider = function(providerId) {
  const p = providers.find(x => x.id === providerId);
  if (!p) return;
  colorPickerTarget = { type: 'provider', providerId, currentColor: p.color };
  highlightSwatch(p.color);
  $('colorPickerOverlay').classList.add('active');
};

function highlightSwatch(color) {
  document.querySelectorAll('.color-swatch').forEach(s => {
    s.classList.toggle('selected', s.dataset.color === color);
  });
}

window.selectColor = async function(color) {
  if (!colorPickerTarget) return;

  if (colorPickerTarget.type === 'model') {
    colorPickerTarget.element.style.background = color;
    // Save model color preference (stored in providers or localStorage)
    try {
      localStorage.setItem('model_color_' + colorPickerTarget.role, color);
    } catch(e) {}
  } else if (colorPickerTarget.type === 'provider') {
    // Update the provider color
    try {
      const p = providers.find(x => x.id === colorPickerTarget.providerId);
      if (p) {
        await api('/api/providers', {
          method: 'POST',
          body: JSON.stringify({ ...p, color })
        });
        loadProviders();
      }
    } catch(e) { console.error(e); }
  }

  closeColorPicker();
};

window.closeColorPicker = function() {
  $('colorPickerOverlay').classList.remove('active');
  colorPickerTarget = null;
};

$('colorPickerOverlay').addEventListener('click', e => {
  if (e.target === $('colorPickerOverlay')) closeColorPicker();
});

// ── Cloud Storage ──
async function loadFiles() {
  try {
    const r = await api('/api/files?path=' + encodeURIComponent(cloudPath));
    renderBreadcrumb();
    renderFileList(r.items || []);
    const bytes = r.total_storage_bytes || 0;
    $('storageText').textContent = formatBytes(bytes) + ' used';
    // Assume 10GB soft limit for bar visual
    $('storageFill').style.width = Math.min(bytes / (10*1073741824) * 100, 100) + '%';
  } catch(e) { console.error('loadFiles', e); }
}

function renderBreadcrumb() {
  const el = $('breadcrumb');
  const parts = cloudPath ? cloudPath.split('/') : [];
  let html = '<span class="breadcrumb-item ' + (!cloudPath ? 'current' : '') + '" onclick="navigateCloud(\'\')">Cloud</span>';
  let acc = '';
  parts.forEach((p, i) => {
    acc += (acc ? '/' : '') + p;
    const isCurrent = i === parts.length - 1;
    html += '<span class="breadcrumb-sep">/</span>';
    html += '<span class="breadcrumb-item ' + (isCurrent ? 'current' : '') + '" onclick="navigateCloud(\'' + escHtml(acc) + '\')">' + escHtml(p) + '</span>';
  });
  el.innerHTML = html;
}

function renderFileList(items) {
  const el = $('fileList');
  if (!items.length) {
    el.innerHTML = '<div class="empty-state"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg><p>This folder is empty</p></div>';
    return;
  }
  el.innerHTML = items.map(f => `
    <div class="file-item" ondblclick="${f.is_dir ? "navigateCloud('" + escHtml(f.path) + "')" : "downloadFile('" + escHtml(f.path) + "')"}">
      <div class="file-icon ${f.is_dir ? 'folder' : 'file'}">
        ${f.is_dir
          ? '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>'
          : '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><path d="M14 2v6h6"/></svg>'}
      </div>
      <div class="file-info">
        <div class="file-name">${escHtml(f.name)}</div>
        <div class="file-meta">${f.is_dir ? (f.children_count || 0) + ' items' : formatBytes(f.size)}</div>
      </div>
      <div class="file-actions">
        ${!f.is_dir ? '<button class="file-action-btn" onclick="event.stopPropagation();downloadFile(\'' + escHtml(f.path) + '\')" title="Download"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></button>' : ''}
        <button class="file-action-btn" onclick="event.stopPropagation();renameFile('${escHtml(f.path)}','${escHtml(f.name)}')" title="Rename">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
        <button class="file-action-btn delete" onclick="event.stopPropagation();deleteFile('${escHtml(f.path)}')" title="Delete">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
        </button>
      </div>
    </div>
  `).join('');
}

window.navigateCloud = function(path) {
  cloudPath = path;
  loadFiles();
};

window.downloadFile = function(path) {
  const a = document.createElement('a');
  a.href = '/api/files/download?path=' + encodeURIComponent(path);
  a.download = '';
  a.click();
};

window.deleteFile = async function(path) {
  if (!confirm('Delete "' + path.split('/').pop() + '"?')) return;
  try {
    await api('/api/files/delete', { method: 'POST', body: JSON.stringify({ path }) });
    loadFiles();
  } catch(e) { alert('Delete failed'); }
};

window.renameFile = function(path, name) {
  openModal('Rename', name, 'Rename', async (newName) => {
    if (!newName || newName === name) return;
    try {
      await api('/api/files/rename', { method: 'POST', body: JSON.stringify({ path, new_name: newName }) });
      loadFiles();
    } catch(e) { alert('Rename failed'); }
  });
};

window.createFolder = function() {
  openModal('New Folder', '', 'Create', async (name) => {
    if (!name) return;
    try {
      await api('/api/files/folder', { method: 'POST', body: JSON.stringify({ path: cloudPath, name }) });
      loadFiles();
    } catch(e) { alert('Create folder failed'); }
  });
};

window.uploadFiles = async function(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  fd.append('path', cloudPath);
  for (const f of files) fd.append('file', f);
  try {
    await fetch('/api/files/upload', { method: 'POST', body: fd });
    loadFiles();
  } catch(e) { alert('Upload failed'); }
  $('fileUpload').value = '';
};

// Drag and drop
function setupDragDrop() {
  const cv = $('cloudView');
  const overlay = $('dropOverlay');
  let dragCounter = 0;
  cv.addEventListener('dragenter', e => { e.preventDefault(); dragCounter++; overlay.classList.add('active'); });
  cv.addEventListener('dragleave', e => { e.preventDefault(); dragCounter--; if (dragCounter <= 0) { dragCounter = 0; overlay.classList.remove('active'); } });
  cv.addEventListener('dragover', e => e.preventDefault());
  cv.addEventListener('drop', e => {
    e.preventDefault(); dragCounter = 0; overlay.classList.remove('active');
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
}

// ── Tunnel ──
async function loadTunnelStatus() {
  try {
    const s = await api('/api/tunnel/status');
    updateTunnelUI(s);
  } catch(e) {}
}

function updateTunnelUI(s) {
  tunnelRunning = s.running;
  $('tStatus').innerHTML = s.running ? '<span class="status-dot"></span>Active' : 'Offline';
  const btn = $('tunnelToggle');
  btn.querySelector('span').textContent = s.running ? 'Stop Tunnel' : 'Start Tunnel';
  if (s.full_url && s.running) {
    $('tunnelBanner').style.display = 'flex';
    $('tunnelUrl').textContent = s.full_url;
  } else {
    $('tunnelBanner').style.display = 'none';
  }
}

window.toggleTunnel = async function() {
  const btn = $('tunnelToggle');
  btn.disabled = true;
  btn.querySelector('span').textContent = tunnelRunning ? 'Stopping...' : 'Starting...';
  try {
    if (tunnelRunning) {
      await api('/api/tunnel/stop', { method: 'POST' });
    } else {
      await api('/api/tunnel/start', { method: 'POST' });
    }
  } catch(e) { console.error(e); }
  btn.disabled = false;
  loadTunnelStatus();
  loadStatus();
};

window.copyTunnelUrl = function() {
  const url = $('tunnelUrl').textContent;
  navigator.clipboard.writeText(url).then(() => {
    const orig = $('tunnelUrl').textContent;
    $('tunnelUrl').textContent = '✓ Copied!';
    setTimeout(() => $('tunnelUrl').textContent = orig, 1500);
  });
};

// ── Modal ──
function openModal(title, value, confirmText, cb) {
  $('modalTitle').textContent = title;
  $('modalInput').value = value || '';
  $('modalInput').placeholder = 'Name...';
  $('modalConfirm').textContent = confirmText || 'OK';
  $('modalExtraFields').innerHTML = '';
  modalCallback = cb;
  $('modalOverlay').classList.add('active');
  setTimeout(() => $('modalInput').focus(), 100);
}
window.closeModal = function() {
  $('modalOverlay').classList.remove('active');
  $('modalExtraFields').innerHTML = '';
  modalCallback = null;
};
window.confirmModal = function() {
  const val = $('modalInput').value.trim();
  if (modalCallback) modalCallback(val);
  closeModal();
};
$('modalOverlay').addEventListener('click', e => { if (e.target === $('modalOverlay')) closeModal(); });

// ── Boot ──
checkAuth();

})();
