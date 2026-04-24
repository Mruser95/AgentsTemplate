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

/**
 * 在界面顶部显示红色错误横幅，5 秒后自动隐藏
 * @param {string} message — 错误信息
 */
function showErrorBanner(message) {
  var existing = document.querySelector('.error-banner');
  if (existing && existing.parentNode) {
    existing.parentNode.removeChild(existing);
  }

  var banner = document.createElement('div');
  banner.className = 'error-banner';
  banner.textContent = message;
  document.body.appendChild(banner);

  setTimeout(function () {
    banner.classList.add('hiding');
    banner.addEventListener('animationend', function () {
      if (banner.parentNode) banner.parentNode.removeChild(banner);
    });
  }, 5000);
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

  var threadId = getThreadId();

  // 清理欢迎态
  if (typeof clearEmptyState === 'function') clearEmptyState();

  // 显示用户消息
  appendUserMessage(text);

  // 清空输入框并重置高度
  input.value = '';
  input.style.height = 'auto';
  _updateSendButtonState();

  // 切换 UI 为 streaming 态（隐藏发送、显示停止、禁用新建对话）
  _setStreamingUI(true);

  // 加载指示器 + AI 气泡
  var loadingEl = _createLoadingIndicator();
  _currentAIBubble = createAIMessageBubble();
  _toolIndicators = {};

  _currentStreamController = sendMessage(threadId, text, {
    onToken: function (token) {
      if (_currentAIBubble) {
        appendToAIMessage(_currentAIBubble, token);
      }
    },
    onToolStart: function (data) {
      _toolIndicators[data.name] = renderToolStart(data.name, data.args);
    },
    onToolEnd: function (data) {
      var indicator = _toolIndicators[data.name];
      if (indicator) renderToolEnd(indicator, data.name, data.output);
    },
    onDone: function () {
      _finishStreaming(loadingEl, input);
    },
    onError: function (error) {
      if (error === '⚠ 连接中断' && _currentAIBubble) {
        var lostSpan = document.createElement('span');
        lostSpan.className = 'connection-lost';
        lostSpan.textContent = '⚠ 连接中断';
        _currentAIBubble.appendChild(lostSpan);
        scrollToBottom();
      } else {
        renderError(error);
      }
      _finishStreaming(loadingEl, input);
    }
  });
}

/**
 * 创建加载指示器（3 个跳动圆点）并插入消息区域
 */
function _createLoadingIndicator() {
  var container = document.getElementById('messages');
  var el = document.createElement('div');
  el.className = 'loading-indicator';
  for (var i = 0; i < 3; i++) {
    var dot = document.createElement('span');
    dot.className = 'dot';
    el.appendChild(dot);
  }
  if (container) container.appendChild(el);
  scrollToBottom();
  return el;
}

/**
 * 流式响应结束后恢复 UI 状态
 */
function _finishStreaming(loadingEl, input) {
  if (loadingEl && loadingEl.parentNode) {
    loadingEl.parentNode.removeChild(loadingEl);
  }
  _setStreamingUI(false);
  if (input) input.focus();
  _currentAIBubble = null;
  _toolIndicators = {};
  _currentStreamController = null;
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
    var loadingEl = document.querySelector('.loading-indicator');
    var input = document.getElementById('message-input');
    _finishStreaming(loadingEl, input);
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

/* --- 页面加载时初始化 --- */
document.addEventListener('DOMContentLoaded', init);
