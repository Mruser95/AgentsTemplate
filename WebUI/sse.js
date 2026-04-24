/* ============================================================
   sse.js — SSE_Client
   使用 fetch + ReadableStream 手动解析 SSE 事件流
   支持 POST + 自定义 Header，AbortController 取消
   ============================================================ */

/**
 * 解析 SSE 文本流为事件对象数组
 * 处理 event: 和 data: 行，以空行分隔事件
 *
 * @param {string} chunk — 原始 SSE 文本（可能包含多个事件）
 * @returns {Array<{event: string, data: string}>}
 */
function parseSSE(chunk) {
  var events = [];
  var lines = chunk.split('\n');
  var currentEvent = '';
  var currentData = '';
  var hasData = false;

  for (var i = 0; i < lines.length; i++) {
    var line = lines[i];

    if (line.indexOf('event:') === 0) {
      currentEvent = line.substring(6).trim();
    } else if (line.indexOf('data:') === 0) {
      currentData = line.substring(5).trim();
      hasData = true;
    } else if (line === '') {
      // 空行表示事件结束
      if (hasData) {
        events.push({ event: currentEvent || 'message', data: currentData });
      }
      currentEvent = '';
      currentData = '';
      hasData = false;
    }
  }

  return events;
}

/**
 * 发送消息并建立 SSE 连接
 * 使用 fetch + ReadableStream 手动解析 SSE（支持 POST + 自定义 Header）
 *
 * @param {string} threadId — 线程 ID
 * @param {string} message — 用户消息
 * @param {{
 *   onToken: function(string): void,
 *   onToolStart: function({name: string, args: *}): void,
 *   onToolEnd: function({name: string, output: string}): void,
 *   onDone: function(): void,
 *   onError: function(string): void
 * }} callbacks — 事件回调
 * @returns {AbortController} — 可用于取消连接
 */
function sendMessage(threadId, message, callbacks) {
  var controller = new AbortController();

  _streamSSE(threadId, message, callbacks, controller);

  return controller;
}

/**
 * 内部：执行 SSE 流式请求
 */
async function _streamSSE(threadId, message, callbacks, controller) {
  var buffer = '';

  try {
    var response;
    try {
      response = await fetch(API_BASE + '/chat/' + threadId, {
        method: 'POST',
        headers: _buildHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ message: message }),
        signal: controller.signal
      });
    } catch (fetchErr) {
      if (fetchErr.name === 'AbortError') return;
      // 网络不可达 — 显示错误横幅
      if (typeof showErrorBanner === 'function') {
        showErrorBanner('无法连接到服务器');
      }
      callbacks.onError('无法连接到服务器');
      return;
    }

    if (!response.ok) {
      if (response.status === 401) {
        _handle401(response);
      }
      callbacks.onError('HTTP ' + response.status);
      return;
    }

    var reader = response.body.getReader();
    var decoder = new TextDecoder();

    while (true) {
      var result = await reader.read();
      if (result.done) break;

      buffer += decoder.decode(result.value, { stream: true });

      // 只处理完整事件（以 \n\n 结尾）
      var lastDoubleNewline = buffer.lastIndexOf('\n\n');
      if (lastDoubleNewline === -1) continue;

      var complete = buffer.substring(0, lastDoubleNewline + 2);
      buffer = buffer.substring(lastDoubleNewline + 2);

      var events = parseSSE(complete);
      for (var i = 0; i < events.length; i++) {
        _dispatchSSEEvent(events[i], callbacks);
      }
    }

    // 处理 buffer 中剩余数据
    if (buffer.trim()) {
      var remaining = parseSSE(buffer + '\n\n');
      for (var j = 0; j < remaining.length; j++) {
        _dispatchSSEEvent(remaining[j], callbacks);
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      // 用户主动取消，不做额外处理
      return;
    }
    // 流式传输过程中连接中断
    callbacks.onError('⚠ 连接中断');
  }
}

/**
 * 分发单个 SSE 事件到对应回调
 */
function _dispatchSSEEvent(sseEvent, callbacks) {
  var eventType = sseEvent.event;
  var rawData = sseEvent.data;

  try {
    if (eventType === 'token') {
      // token data 是 JSON 编码的字符串，需要 JSON.parse 获取实际文本
      var text = JSON.parse(rawData);
      callbacks.onToken(text);
    } else if (eventType === 'tool_start') {
      var toolData = JSON.parse(rawData);
      callbacks.onToolStart(toolData);
    } else if (eventType === 'tool_end') {
      var toolEndData = JSON.parse(rawData);
      callbacks.onToolEnd(toolEndData);
    } else if (eventType === 'done') {
      callbacks.onDone();
    } else if (eventType === 'error') {
      var errorData = JSON.parse(rawData);
      callbacks.onError(errorData.error || 'Unknown error');
    }
  } catch (e) {
    // JSON 解析失败时，将原始数据作为错误传递
    callbacks.onError(rawData || e.message);
  }
}
