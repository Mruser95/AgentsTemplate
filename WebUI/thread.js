/* ============================================================
   thread.js — Thread 管理（按 API Key 分桶）
   每个 API Key 在 localStorage 中独立维护一个 thread_id：
     - 输入 key A → 进入 A 的会话
     - 切换到 key B → 进入 B 的会话
     - 同一 key 再次登录 → 恢复上次那段会话
   ============================================================ */

/** 每个 key 的 thread_id 存储键前缀；完整形如 agent_webui_thread_id::<fp> */
var THREAD_ID_STORAGE_PREFIX = 'agent_webui_thread_id::';

/** 旧版全局 thread_id 键（存在会跨 key 串数据，仅清理不迁移） */
var LEGACY_GLOBAL_THREAD_KEY = 'agent_webui_thread_id';
/** 更旧的多线程存储键 */
var LEGACY_THREADS_KEY = 'agent_webui_threads';

/**
 * 生成 4 位随机 hex
 */
function _random4() {
  return Math.floor((1 + Math.random()) * 0x10000).toString(16).substring(1);
}

/**
 * 生成新的 thread_id
 */
function _generateThreadId() {
  return 'thread_' + Date.now() + '_' + _random4();
}

/**
 * 对 API Key 做一个稳定的短指纹（FNV-1a 32bit → 8 位 hex）
 * 仅用于 localStorage 分桶，不用于任何安全场景
 * @param {string} key
 * @returns {string}
 */
function _keyFingerprint(key) {
  var h = 0x811c9dc5;
  for (var i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return ('00000000' + h.toString(16)).slice(-8);
}

/**
 * 根据当前 API Key 取 thread_id 的 localStorage 存储键
 * 若尚未登录（无 key），返回临时 probe 槽位，避免调用点崩溃
 * @returns {string}
 */
function _currentThreadStorageKey() {
  var key = typeof getApiKey === 'function' ? getApiKey() : null;
  var fp = key ? _keyFingerprint(key) : 'probe';
  return THREAD_ID_STORAGE_PREFIX + fp;
}

/**
 * 清理旧版全局存储，避免把别人的历史当作当前 key 的历史
 * 仅在模块加载时执行一次
 */
(function _cleanupLegacyKeysOnce() {
  try { localStorage.removeItem(LEGACY_GLOBAL_THREAD_KEY); } catch (e) {}
  try { localStorage.removeItem(LEGACY_THREADS_KEY); } catch (e) {}
})();

/**
 * 获取当前 API Key 对应的 thread_id
 *   - 该 key 已有：直接返回（同一 key 下次登录继续这段会话）
 *   - 该 key 尚无：生成新的并持久化
 * @returns {string}
 */
function getThreadId() {
  var storageKey = _currentThreadStorageKey();
  var id = localStorage.getItem(storageKey);
  if (id) return id;
  id = _generateThreadId();
  localStorage.setItem(storageKey, id);
  return id;
}

/**
 * 重置当前 API Key 对应的 thread_id（新建对话）
 * 只影响当前登录的 key，不影响其它 key
 * @returns {string} 新的 thread_id
 */
function resetThreadId() {
  var id = _generateThreadId();
  localStorage.setItem(_currentThreadStorageKey(), id);
  return id;
}

/**
 * 清除当前 API Key 对应的 thread_id
 * 一般不主动调用——logout 默认不清，保留的 thread_id 便于同 key 再次登录恢复会话
 */
function clearThreadId() {
  localStorage.removeItem(_currentThreadStorageKey());
}

/**
 * 延迟强制滚到底部，等 DOM 布局完成
 */
function _deferScrollToBottom() {
  var fn = typeof stickToBottomFor === 'function' ? stickToBottomFor : null;
  if (!fn) return;
  setTimeout(function () { fn(1400); }, 0);
  setTimeout(function () { fn(1400); }, 180);
}

/**
 * 加载当前 thread 的历史消息并渲染
 *   - 有消息：调用 renderHistory
 *   - 空对话：渲染欢迎态
 *   - 失败：显示错误提示（401 由 api.js 统一处理，已切回认证视图）
 */
async function loadThreadHistory() {
  var threadId = getThreadId();
  var messagesEl = document.getElementById('messages');
  if (messagesEl) messagesEl.innerHTML = '';

  try {
    var response = await apiGet('/threads/' + threadId + '/messages');
    if (!response.ok) {
      // 401 已在 api.js 中被 _handle401 处理，这里静默返回
      if (response.status === 401) return;
      throw new Error('HTTP ' + response.status);
    }
    var result = await response.json();
    var msgs = result && result.messages;
    if (msgs && msgs.length && typeof renderHistory === 'function') {
      renderHistory(msgs);
      _deferScrollToBottom();
    } else if (typeof restoreCachedMessages === 'function' && restoreCachedMessages()) {
      // 服务端尚无 checkpoint（生成中刷新），从 sessionStorage 缓存恢复
      _deferScrollToBottom();
    } else {
      renderEmptyState();
    }
  } catch (e) {
    if (messagesEl) {
      var errorDiv = document.createElement('div');
      errorDiv.className = 'history-error';
      errorDiv.textContent = '加载历史失败';
      messagesEl.appendChild(errorDiv);
    }
  }
}

/**
 * 渲染空对话引导页
 */
function renderEmptyState() {
  var container = document.getElementById('messages');
  if (!container) return;
  if (container.querySelector('.empty-state')) return;
  var wrap = document.createElement('div');
  wrap.className = 'empty-state';
  wrap.innerHTML =
    '<div class="empty-title">开始对话</div>' +
    '<div class="empty-hint">在下方输入消息，开始与 Agent 对话</div>';
  container.appendChild(wrap);
}

/**
 * 移除空对话引导页
 */
function clearEmptyState() {
  var el = document.querySelector('.empty-state');
  if (el && el.parentNode) el.parentNode.removeChild(el);
}
