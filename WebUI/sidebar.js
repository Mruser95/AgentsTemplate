/* ============================================================
   sidebar.js — 左侧会话列表
   列出 SessionDB 中所有线程（全局，不按 key 区分），支持切换 / 新建。
   ============================================================ */

var _threadListLoading = false;

/* 侧栏宽度：右边缘可拖拽调整，连同折叠状态一起持久化到 localStorage */
var SIDEBAR_W_KEY = 'agent_webui_sidebar_w';
var SIDEBAR_COLLAPSED_KEY = 'agent_webui_sidebar_collapsed';
var SIDEBAR_W_MIN = 200;
var SIDEBAR_W_MAX = 520;
var SIDEBAR_W_DEFAULT = 264;

/** 应用侧栏宽度（夹在 MIN~MAX 之间），通过 CSS 变量 --sidebar-w 驱动；返回实际生效值 */
function _applySidebarWidth(px) {
  var view = document.getElementById('chat-view');
  if (!view) return;
  var w = Math.max(SIDEBAR_W_MIN, Math.min(SIDEBAR_W_MAX, Math.round(px)));
  view.style.setProperty('--sidebar-w', w + 'px');
  return w;
}

/** 是否处于移动端（侧栏为浮层抽屉） */
function _isNarrow() {
  return !!(window.matchMedia && window.matchMedia('(max-width: 900px)').matches);
}

/**
 * 初始化侧栏：绑定开合、新建、遮罩点击；移动端默认收起
 */
function initSidebar() {
  var toggle = document.getElementById('sidebar-toggle');
  if (toggle) toggle.addEventListener('click', toggleSidebar);

  var scrim = document.getElementById('sidebar-scrim');
  if (scrim) scrim.addEventListener('click', collapseSidebar);

  var newBtn = document.getElementById('sidebar-new-btn');
  if (newBtn) {
    newBtn.addEventListener('click', function () {
      if (typeof startNewThread === 'function') startNewThread();
    });
  }

  _initSidebarResize();

  // 移动端默认收起；桌面端恢复上次的折叠状态
  if (_isNarrow()) {
    collapseSidebar();
  } else if (localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1') {
    collapseSidebar();
  }
}

function toggleSidebar() {
  var v = document.getElementById('chat-view');
  if (!v) return;
  var collapsed = v.classList.toggle('sidebar-collapsed');
  if (!_isNarrow()) localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? '1' : '0');
}

function collapseSidebar() {
  var v = document.getElementById('chat-view');
  if (v) v.classList.add('sidebar-collapsed');
}

/**
 * 初始化右边缘拖拽手柄：拖动调宽（持久化）、双击复位、移动端禁用
 */
function _initSidebarResize() {
  var resizer = document.getElementById('sidebar-resizer');
  var view = document.getElementById('chat-view');
  var sidebar = document.getElementById('sidebar');
  if (!resizer || !view || !sidebar) return;

  // 恢复上次保存的宽度
  var saved = parseInt(localStorage.getItem(SIDEBAR_W_KEY), 10);
  if (!isNaN(saved)) _applySidebarWidth(saved);

  var startX = 0, startW = 0, dragging = false;

  function onMove(e) {
    if (!dragging) return;
    _applySidebarWidth(startW + (e.clientX - startX));
    e.preventDefault();
  }
  function onUp() {
    if (!dragging) return;
    dragging = false;
    view.classList.remove('resizing');
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', onUp);
    var px = parseInt(view.style.getPropertyValue('--sidebar-w'), 10);
    if (!isNaN(px)) localStorage.setItem(SIDEBAR_W_KEY, String(px));
  }

  resizer.addEventListener('pointerdown', function (e) {
    if (_isNarrow()) return;        // 抽屉模式不支持拖拽
    dragging = true;
    startX = e.clientX;
    startW = sidebar.getBoundingClientRect().width;
    view.classList.add('resizing');
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
    e.preventDefault();
  });

  // 双击复位到默认宽度
  resizer.addEventListener('dblclick', function () {
    var w = _applySidebarWidth(SIDEBAR_W_DEFAULT);
    if (w) localStorage.setItem(SIDEBAR_W_KEY, String(w));
  });
}

/** 移动端选中/新建后自动收起抽屉，桌面端保持常驻 */
function _autoCollapseOnNarrow() {
  if (_isNarrow()) collapseSidebar();
}

/**
 * 拉取线程列表并渲染（未登录时跳过）
 */
async function refreshThreadList() {
  var listEl = document.getElementById('thread-list');
  if (!listEl) return;
  if (typeof getApiKey === 'function' && !getApiKey()) return;
  if (_threadListLoading) return;
  _threadListLoading = true;

  try {
    var resp = await apiGet('/threads');
    if (!resp.ok) {
      if (resp.status === 401) return; // 已由 api.js 切回认证视图
      listEl.innerHTML = '<div class="thread-list-empty">加载失败</div>';
      return;
    }
    var data = await resp.json();
    _renderThreads((data && data.threads) || []);
  } catch (e) {
    listEl.innerHTML = '<div class="thread-list-empty">加载失败</div>';
  } finally {
    _threadListLoading = false;
  }
}

/**
 * 渲染线程列表；把"当前活跃但服务端尚无记录（新对话未发消息）"的线程并入顶部
 */
function _renderThreads(threads) {
  var listEl = document.getElementById('thread-list');
  if (!listEl) return;

  var active = typeof getThreadId === 'function' ? getThreadId() : null;
  var items = threads.slice();
  var hasActive = items.some(function (t) { return t.thread_id === active; });
  if (active && !hasActive) {
    items.unshift({ thread_id: active, title: '', message_count: 0, updated_at: '' });
  }

  if (!items.length) {
    listEl.innerHTML = '<div class="thread-list-empty">暂无会话</div>';
    return;
  }

  listEl.innerHTML = '';
  for (var i = 0; i < items.length; i++) {
    listEl.appendChild(_createThreadItem(items[i], active, i));
  }
}

/**
 * 创建单条会话卡片
 */
function _createThreadItem(t, active, index) {
  var el = document.createElement('button');
  el.className = 'thread-item' + (t.thread_id === active ? ' active' : '');
  el.style.animationDelay = (index * 30) + 'ms';

  var title = (t.title && t.title.trim()) ? t.title : '新对话';
  var meta = t.message_count
    ? (t.message_count + ' 条 · ' + _formatThreadTime(t.updated_at))
    : '尚未开始';

  var titleEl = document.createElement('span');
  titleEl.className = 'thread-item-title';
  titleEl.textContent = title;

  var metaEl = document.createElement('span');
  metaEl.className = 'thread-item-meta';
  metaEl.textContent = meta;

  el.appendChild(titleEl);
  el.appendChild(metaEl);
  el.addEventListener('click', function () { selectThread(t.thread_id); });
  return el;
}

/**
 * 时间格式：今天显示时:分，否则显示月/日
 */
function _formatThreadTime(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  var now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  }
  return (d.getMonth() + 1) + '/' + d.getDate();
}

/**
 * 切换到某条会话：流式中禁止；点当前会话仅收起抽屉
 */
function selectThread(id) {
  if (!id) return;
  if (typeof _currentStreamController !== 'undefined' && _currentStreamController) return;
  if (typeof getThreadId === 'function' && id === getThreadId()) {
    _autoCollapseOnNarrow();
    return;
  }
  if (typeof setThreadId === 'function') setThreadId(id);
  if (typeof loadThreadHistory === 'function') loadThreadHistory();
  refreshThreadList();
  _autoCollapseOnNarrow();
}

document.addEventListener('DOMContentLoaded', initSidebar);
