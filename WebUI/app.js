/* ============================================================
   app.js — 应用入口（单线程模式）
   初始化、视图切换、消息发送、停止流
   ============================================================ */

/** 当前活跃的工具指示器映射：toolName → HTMLElement */
var _toolIndicators = {};

/** 当前 AI 消息气泡引用 */
var _currentAIBubble = null;

/** 当前流式请求的 AbortController；为 null 表示空闲 */
var _currentStreamController = null;

/** 最近一次发送的用户文本，用于「重试 / 重新生成」 */
var _lastUserText = '';

/** 当前轮次的 loading 指示器引用（在文字/工具段落之间反复出现） */
var _loaderEl = null;

/**
 * 在界面顶部显示错误横幅；可选地带一个「重试」按钮。
 * 带 onRetry 时不自动消失，由用户决定；无 onRetry 时 5 秒后淡出。
 * @param {string} message — 错误信息
 * @param {Function} [onRetry] — 点击重试的回调
 */
function showErrorBanner(message, onRetry) {
  var existing = document.querySelector('.error-banner');
  if (existing && existing.parentNode) {
    existing.parentNode.removeChild(existing);
  }

  var banner = document.createElement('div');
  banner.className = 'error-banner';

  var text = document.createElement('span');
  text.className = 'error-banner-text';
  text.textContent = message;
  banner.appendChild(text);

  function dismiss() {
    banner.classList.add('hiding');
    banner.addEventListener('animationend', function () {
      if (banner.parentNode) banner.parentNode.removeChild(banner);
    });
  }

  if (typeof onRetry === 'function') {
    var retryBtn = document.createElement('button');
    retryBtn.className = 'error-banner-retry';
    retryBtn.textContent = '重试';
    retryBtn.addEventListener('click', function () {
      dismiss();
      onRetry();
    });
    banner.appendChild(retryBtn);
  }

  document.body.appendChild(banner);

  if (typeof onRetry !== 'function') {
    setTimeout(dismiss, 5000);
  }
}

/**
 * 应用初始化
 */
function init() {
  initAuth();
  _bindSendMessage();
  _bindStopButton();
  _bindLogout();
  _bindNewThread();
  _setupAutoResize();
  _setupInputStateTracking();
  _setupScrollButton();
  _setupKeyboardShortcuts();
}

/**
 * 全局键盘快捷键：
 *   Esc — 正在流式则停止生成；否则如果文件面板开着则关闭它
 */
function _setupKeyboardShortcuts() {
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    // 正在生成：优先停止流
    if (_currentStreamController) {
      var stopBtn = document.getElementById('stop-btn');
      if (stopBtn) stopBtn.click();
      return;
    }
    // 其次：关闭文件面板
    var panel = document.getElementById('file-panel');
    if (panel && panel.classList.contains('open') && typeof closeFilePanel === 'function') {
      closeFilePanel();
    }
  });
}

/**
 * 切换视图：auth（认证界面）/ chat（聊天界面）
 * @param {'auth'|'chat'} view
 */
function showView(view) {
  var authView = document.getElementById('auth-view');
  var chatView = document.getElementById('chat-view');

  if (view === 'auth') {
    if (authView) authView.style.display = '';
    if (chatView) chatView.style.display = 'none';
  } else if (view === 'chat') {
    if (authView) authView.style.display = 'none';
    if (chatView) chatView.style.display = '';
    // 进入聊天视图时加载当前线程历史
    if (typeof loadThreadHistory === 'function') {
      loadThreadHistory();
    }
    var input = document.getElementById('message-input');
    if (input) input.focus();
  }
}

/**
 * 绑定发送消息事件（按钮点击 + Enter 发送，Shift+Enter 换行）
 */
function _bindSendMessage() {
  var sendBtn = document.getElementById('send-btn');
  var input = document.getElementById('message-input');

  if (sendBtn) {
    sendBtn.addEventListener('click', handleSendMessage);
  }

  if (input) {
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSendMessage();
      }
    });
  }
}

/**
 * 处理发送消息的完整流程
 */
function handleSendMessage() {
  var input = document.getElementById('message-input');
  var sendBtn = document.getElementById('send-btn');
  if (!input) return;

  // 正在流式：忽略重复发送
  if (_currentStreamController) return;
  if (sendBtn && sendBtn.disabled) return;

  var text = input.value.trim();
  if (!text) return;

  // 清理欢迎态
  if (typeof clearEmptyState === 'function') clearEmptyState();

  // 显示用户消息
  appendUserMessage(text);

  // 清空输入框并重置高度
  input.value = '';
  input.style.height = 'auto';
  _updateSendButtonState();

  _runAgent(text);
}

/**
 * 重新生成：移除最后一条用户消息之后的所有内容，然后重新跳起生成。
 */
function regenerateLast() {
  if (_currentStreamController) return;
  var container = document.getElementById('messages');
  if (!container) return;
  var users = container.querySelectorAll('.message.user');
  if (!users.length) return;
  var lastUser = users[users.length - 1];
  var text = lastUser.textContent || _lastUserText;
  if (!text) return;
  // 清掉该用户消息之后的所有节点（旧的 AI 回复 / 工具盒 / 操作栏）
  while (lastUser.nextSibling) container.removeChild(lastUser.nextSibling);
  _runAgent(text);
}

/**
 * 点击空状态示例 prompt：填入输入框并直接发送
 */
function useExample(text) {
  var input = document.getElementById('message-input');
  if (!input) return;
  input.value = text;
  input.dispatchEvent(new Event('input'));
  handleSendMessage();
}

/**
 * 启动一轮 Agent 流式跳起（不负责渲染用户气泡，以便发送/重生复用）。
 * 关键：采用「懒创建气泡 + 遇工具封口」策略，让 文字→工具→文字
 * 按真实时间顺序交替出现，而不是全部文字在上、工具盒在最下。
 */
function _runAgent(text) {
  _lastUserText = text;
  var input = document.getElementById('message-input');
  var threadId = getThreadId();

  // 切换 UI 为 streaming 态
  _setStreamingUI(true);

  // 初始「思考中」跳点
  _showLoader();
  _currentAIBubble = null;
  _toolIndicators = {};

  _currentStreamController = sendMessage(threadId, text, {
    onToken: function (token) {
      _hideLoader();
      // 懒创建气泡：第一个 token 或工具之后的新段落才创建
      if (!_currentAIBubble) {
        _currentAIBubble = createAIMessageBubble();
        _currentAIBubble.classList.add('streaming');
      }
      appendToAIMessage(_currentAIBubble, token);
    },
    onToolStart: function (data) {
      _hideLoader();
      // 封口当前气泡，使之后的文字另起一个气泡（接在工具盒之后）
      if (_currentAIBubble) _currentAIBubble.classList.remove('streaming');
      _currentAIBubble = null;
      _toolIndicators[data.name] = renderToolStart(data.name, data.args);
      _pinLoaderAfterContent();
    },
    onToolEnd: function (data) {
      var indicator = _toolIndicators[data.name];
      if (indicator) renderToolEnd(indicator, data.name, data.output);
      // 工具结束后还可能有后续文字——重新显示「思考中」跳点
      _showLoader();
    },
    onDone: function () {
      _finishStreaming(input);
    },
    onError: function (error) {
      var isConn = /连接|服务器|中断/.test(error);
      if (error === '⚠ 连接中断' && _currentAIBubble) {
        var lostSpan = document.createElement('span');
        lostSpan.className = 'connection-lost';
        lostSpan.textContent = '⚠ 连接中断';
        _currentAIBubble.appendChild(lostSpan);
        scrollToBottom();
        showErrorBanner('连接中断', regenerateLast);
      } else if (isConn) {
        showErrorBanner(error, regenerateLast);
      } else {
        renderError(error);
      }
      _finishStreaming(input);
    }
  });
}

/**
 * 显示「思考中」跳点（3 个跳动圆点），始终置于消息区底部
 */
function _showLoader() {
  var container = document.getElementById('messages');
  if (!container) return;
  if (!_loaderEl) {
    _loaderEl = document.createElement('div');
    _loaderEl.className = 'loading-indicator';
    for (var i = 0; i < 3; i++) {
      var dot = document.createElement('span');
      dot.className = 'dot';
      _loaderEl.appendChild(dot);
    }
  }
  container.appendChild(_loaderEl); // appendChild 会把已存节点移到末尾
  scrollToBottom();
}

/** 移除「思考中」跳点 */
function _hideLoader() {
  if (_loaderEl && _loaderEl.parentNode) {
    _loaderEl.parentNode.removeChild(_loaderEl);
  }
}

/** 若跳点正显示，把它重新钉到最末尾（新内容插入后保持在底） */
function _pinLoaderAfterContent() {
  if (_loaderEl && _loaderEl.parentNode) {
    _loaderEl.parentNode.appendChild(_loaderEl);
  }
}

/**
 * 流式响应结束后恢复 UI 状态
 */
function _finishStreaming(input) {
  _hideLoader();
  if (_currentAIBubble) _currentAIBubble.classList.remove('streaming');
  _setStreamingUI(false);
  if (input) input.focus();
  _currentAIBubble = null;
  _toolIndicators = {};
  _currentStreamController = null;
  if (typeof decorateAIBubbles === 'function') decorateAIBubbles();
}

/**
 * 切换 streaming UI 状态：
 *   streaming=true  → 隐藏发送、显示停止、禁用新建对话
 *   streaming=false → 相反，并根据输入内容恢复发送按钮 disabled 状态
 */
function _setStreamingUI(streaming) {
  var sendBtn = document.getElementById('send-btn');
  var stopBtn = document.getElementById('stop-btn');
  var newThreadBtn = document.getElementById('new-thread-btn');

  if (streaming) {
    if (sendBtn) sendBtn.style.display = 'none';
    if (stopBtn) stopBtn.style.display = '';
    if (newThreadBtn) newThreadBtn.disabled = true;
  } else {
    if (sendBtn) sendBtn.style.display = '';
    if (stopBtn) stopBtn.style.display = 'none';
    if (newThreadBtn) newThreadBtn.disabled = false;
    _updateSendButtonState();
  }
}

/**
 * 绑定停止按钮：中断当前 SSE 流
 */
function _bindStopButton() {
  var stopBtn = document.getElementById('stop-btn');
  if (!stopBtn) return;
  stopBtn.addEventListener('click', function () {
    if (_currentStreamController && typeof _currentStreamController.abort === 'function') {
      _currentStreamController.abort();
    }
    // 在当前气泡追加停止提示
    if (_currentAIBubble) {
      var stopped = document.createElement('span');
      stopped.className = 'connection-lost';
      stopped.textContent = '⏹ 已停止';
      _currentAIBubble.appendChild(stopped);
      scrollToBottom();
    }
    var input = document.getElementById('message-input');
    _finishStreaming(input);
  });
}

/**
 * 绑定退出登录按钮
 */
function _bindLogout() {
  var btn = document.getElementById('logout-btn');
  if (btn) btn.addEventListener('click', function () { logout(); });
}

/**
 * 绑定"新建对话"按钮
 * - 若当前有对话内容：弹出确认
 * - 重置 thread_id，清屏，显示空对话态
 */
function _bindNewThread() {
  var btn = document.getElementById('new-thread-btn');
  if (!btn) return;
  btn.addEventListener('click', function () {
    if (btn.disabled) return;
    var messagesEl = document.getElementById('messages');
    var hasContent = messagesEl && messagesEl.querySelector('.message');
    if (hasContent && !window.confirm('新建对话将清除当前对话上下文，确定继续？')) return;
    resetThreadId();
    if (messagesEl) messagesEl.innerHTML = '';
    renderEmptyState();
    var input = document.getElementById('message-input');
    if (input) input.focus();
  });
}

/**
 * 设置 textarea 自动调整高度
 */
function _setupAutoResize() {
  var input = document.getElementById('message-input');
  if (!input) return;
  input.addEventListener('input', function () {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 140) + 'px';
  });
}

/**
 * 输入框内容变化时，更新发送按钮 disabled 状态
 */
function _setupInputStateTracking() {
  var input = document.getElementById('message-input');
  if (!input) return;
  input.addEventListener('input', _updateSendButtonState);
}

function _updateSendButtonState() {
  var input = document.getElementById('message-input');
  var sendBtn = document.getElementById('send-btn');
  if (!input || !sendBtn) return;
  sendBtn.disabled = !input.value.trim();
}

/**
 * 创建「↓ 回到底部」浮动按钮：用户往上翻阅时出现，点击平滑回到底部
 */
function _setupScrollButton() {
  var container = document.getElementById('messages');
  var chatView = document.getElementById('chat-view');
  if (!container || !chatView) return;

  var btn = document.createElement('button');
  btn.id = 'scroll-bottom-btn';
  btn.setAttribute('aria-label', '回到底部');
  btn.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';
  chatView.appendChild(btn);

  btn.addEventListener('click', function () {
    container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
  });

  var _raf = 0;
  function update() {
    var dist = container.scrollHeight - container.scrollTop - container.clientHeight;
    btn.classList.toggle('show', dist > 240);
  }
  function scheduleUpdate() {
    if (_raf) return;
    _raf = requestAnimationFrame(function () {
      _raf = 0;
      update();
    });
  }
  container.addEventListener('scroll', scheduleUpdate, { passive: true });
  // 内容变化时也重算（rAF 防抖，避免流式期间同步读布局造成抖动）
  var mo = new MutationObserver(scheduleUpdate);
  mo.observe(container, { childList: true, subtree: true });
  scheduleUpdate();
}

/* --- 页面加载时初始化 --- */
document.addEventListener('DOMContentLoaded', init);
