/* ============================================================
   files.js — File Panel
   右侧抽屉：展示当前 thread 的可下载文件（会话文件 + 编码产物）
   ============================================================ */

var _filePanelOpen = false;

/**
 * 初始化文件面板的所有事件绑定
 */
function initFilePanel() {
  var btn = document.getElementById('files-btn');
  var closeBtn = document.getElementById('file-panel-close');
  var refreshBtn = document.getElementById('file-panel-refresh');

  if (btn) btn.addEventListener('click', toggleFilePanel);
  if (closeBtn) closeBtn.addEventListener('click', closeFilePanel);
  if (refreshBtn) refreshBtn.addEventListener('click', refreshFilePanel);

  // 点击面板外侧关闭
  document.addEventListener('click', function (e) {
    if (!_filePanelOpen) return;
    var panel = document.getElementById('file-panel');
    var filesBtn = document.getElementById('files-btn');
    if (panel && !panel.contains(e.target) && e.target !== filesBtn) {
      closeFilePanel();
    }
  });
}

function toggleFilePanel() {
  if (_filePanelOpen) {
    closeFilePanel();
  } else {
    openFilePanel();
  }
}

function openFilePanel() {
  var panel = document.getElementById('file-panel');
  if (panel) panel.classList.add('open');
  _filePanelOpen = true;
  refreshFilePanel();
}

function closeFilePanel() {
  var panel = document.getElementById('file-panel');
  if (panel) panel.classList.remove('open');
  _filePanelOpen = false;
}

/**
 * 从后端拉取文件列表并重新渲染
 */
async function refreshFilePanel() {
  var body = document.getElementById('file-panel-body');
  if (!body) return;

  var threadId = typeof getThreadId === 'function' ? getThreadId() : null;
  if (!threadId) {
    body.innerHTML = '<div class="file-panel-empty">暂无活跃会话</div>';
    return;
  }

  body.innerHTML = '<div class="file-panel-loading">加载中…</div>';

  try {
    var response = await apiGet('/threads/' + threadId + '/files');
    if (!response.ok) {
      body.innerHTML = '<div class="file-panel-empty">加载失败（' + response.status + '）</div>';
      return;
    }
    var result = await response.json();
    _renderFileList(result.files || [], body, threadId);
  } catch (e) {
    body.innerHTML = '<div class="file-panel-empty">加载失败</div>';
  }
}

/**
 * 渲染文件列表
 */
function _renderFileList(files, container, threadId) {
  container.innerHTML = '';

  if (!files.length) {
    container.innerHTML = '<div class="file-panel-empty">当前会话暂无文件</div>';
    return;
  }

  // 按来源分组：代码产物在前，会话文件在后
  files.sort(function (a, b) {
    if (a.source === b.source) return a.name.localeCompare(b.name);
    return a.source === 'code' ? -1 : 1;
  });

  for (var i = 0; i < files.length; i++) {
    container.appendChild(_createFileItem(files[i], threadId, i));
  }
}

/**
 * 创建单个文件卡片 DOM
 */
function _createFileItem(file, threadId, index) {
  var item = document.createElement('div');
  item.className = 'file-item';
  item.style.animationDelay = (index * 40) + 'ms';

  var sourceBadge = file.source === 'code'
    ? '<span class="file-source-badge">代码</span>'
    : '<span class="file-source-badge">会话</span>';

  item.innerHTML =
    '<span class="file-icon">' + _fileIcon(file.name) + '</span>' +
    '<div class="file-info">' +
      '<div class="file-name">' + _escapeHtml(file.name) + sourceBadge + '</div>' +
      '<div class="file-meta">' + _formatSize(file.size) + ' · ' + _escapeHtml(file.path) + '</div>' +
    '</div>' +
    '<button class="file-dl-btn">下载</button>';

  var dlBtn = item.querySelector('.file-dl-btn');
  dlBtn.addEventListener('click', function () {
    _downloadFile(threadId, file.path, file.name, dlBtn);
  });

  return item;
}

/**
 * 下载文件（经由认证 header 的 fetch + Blob URL）
 */
function _downloadFile(threadId, path, filename, btn) {
  var url = (typeof API_BASE !== 'undefined' ? API_BASE : '') +
    '/threads/' + encodeURIComponent(threadId) + '/files/' + encodeURI(path);

  var headers = typeof _buildHeaders === 'function' ? _buildHeaders({}) : {};

  var origText = btn.textContent;
  btn.textContent = '…';
  btn.disabled = true;

  fetch(url, { headers: headers })
    .then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.blob();
    })
    .then(function (blob) {
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      setTimeout(function () {
        URL.revokeObjectURL(a.href);
        if (a.parentNode) a.parentNode.removeChild(a);
      }, 1000);
      btn.textContent = '✓';
      setTimeout(function () {
        btn.textContent = origText;
        btn.disabled = false;
      }, 2000);
    })
    .catch(function (e) {
      btn.textContent = origText;
      btn.disabled = false;
      alert('下载失败: ' + e.message);
    });
}

/**
 * 按文件扩展名返回 emoji 图标
 */
function _fileIcon(name) {
  var ext = (name.split('.').pop() || '').toLowerCase();
  var icons = {
    py: '🐍', js: '📄', ts: '📄', json: '📋',
    md: '📝', txt: '📝', html: '🌐', css: '🎨',
    yaml: '⚙️', yml: '⚙️', sh: '💻', bat: '💻',
    csv: '📊', png: '🖼️', jpg: '🖼️', jpeg: '🖼️',
    gif: '🖼️', svg: '🖼️', pdf: '📑', zip: '📦',
    gz: '📦', tar: '📦', go: '🔵', rs: '🦀',
    java: '☕', cpp: '⚙️', c: '⚙️', toml: '⚙️',
  };
  return icons[ext] || '📄';
}

/**
 * 文件大小人性化格式
 */
function _formatSize(bytes) {
  if (bytes == null || bytes < 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}

/**
 * HTML 转义（安全渲染文件名/路径）
 */
function _escapeHtml(text) {
  var d = document.createElement('div');
  d.appendChild(document.createTextNode(text || ''));
  return d.innerHTML;
}

document.addEventListener('DOMContentLoaded', initFilePanel);
