/* ============================================
   render.js — Message_Renderer
   消息渲染：气泡、Markdown、工具指示器、滚动
   ============================================ */

/**
 * 获取 #messages 容器
 */
function getMessagesContainer() {
  return document.getElementById('messages');
}

/**
 * 自动滚动到最新消息
 * 仅当用户本来就在底部附近（≤80px）时才滚动，
 * 避免用户往上翻阅时被强制拉回底部。
 */
function scrollToBottom() {
  var container = getMessagesContainer();
  if (!container) return;
  var distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
  if (distanceFromBottom <= 80) {
    container.scrollTop = container.scrollHeight;
  }
}

/**
 * 强制滚动到底部（不检测当前位置，用于刷新/加载历史后）
 */
var _stickBottomRaf = 0;

function forceScrollToBottom() {
  var container = getMessagesContainer();
  if (!container) return;
  container.style.scrollBehavior = 'auto';
  container.scrollTop = container.scrollHeight;
  container.style.scrollBehavior = '';
}

function stickToBottomFor(durationMs) {
  var container = getMessagesContainer();
  if (!container) return;
  if (_stickBottomRaf) {
    cancelAnimationFrame(_stickBottomRaf);
    _stickBottomRaf = 0;
  }
  var endAt = Date.now() + (durationMs || 1200);
  var tick = function () {
    forceScrollToBottom();
    if (Date.now() < endAt) {
      _stickBottomRaf = requestAnimationFrame(tick);
    } else {
      _stickBottomRaf = 0;
      forceScrollToBottom();
    }
  };
  tick();
}

/* ---------- sessionStorage 消息缓存（防刷新丢失） ---------- */

var _persistTimer = null;

/**
 * 节流保存 #messages 的 innerHTML 到 sessionStorage
 * 最多 500ms 写一次，避免高频流式 token 造成过多 IO
 */
function _persistMessagesHTML() {
  if (_persistTimer) return;
  _persistTimer = setTimeout(function () {
    _persistTimer = null;
    var threadId = typeof getThreadId === 'function' ? getThreadId() : null;
    var container = document.getElementById('messages');
    if (container && threadId) {
      try {
        sessionStorage.setItem('msg_html::' + threadId, container.innerHTML);
      } catch (e) { /* quota exceeded — ignore */ }
    }
  }, 500);
}

/**
 * 尝试从 sessionStorage 恢复消息 HTML
 * @returns {boolean} 是否恢复成功
 */
function restoreCachedMessages() {
  var threadId = typeof getThreadId === 'function' ? getThreadId() : null;
  if (!threadId) return false;
  var html = sessionStorage.getItem('msg_html::' + threadId);
  if (html) {
    var container = document.getElementById('messages');
    if (container) {
      container.innerHTML = html;
      return true;
    }
  }
  return false;
}

/**
 * Markdown + LaTeX 渲染（marked.js v4 + KaTeX）
 *
 * 处理流程：
 *  1. 保护代码围栏（```...```）→ HTML 注释占位，防止其中 $ 被误判
 *  2. 提取 display math（$$...$$ / \[...\]）→ 占位
 *  3. 提取 inline math（$...$ / \(...\)）→ 占位
 *  4. 还原代码围栏
 *  5. marked.parse — 处理标题/列表/表格/粗体/斜体/链接等
 *  6. 用 KaTeX 还原数学占位
 *
 * 若 CDN 库未加载，自动降级到 _renderMarkdownFallback()。
 */
function renderMarkdown(text) {
  if (!text) return '';

  if (typeof marked === 'undefined' || typeof katex === 'undefined') {
    return _renderMarkdownFallback(text);
  }

  var mathBlocks  = [];
  var mathInlines = [];
  var codeFences  = [];

  // Step 1: 保护代码围栏
  var src = text.replace(/```[\s\S]*?```/g, function(m) {
    var idx = codeFences.length;
    codeFences.push(m);
    return '<!-- _CF' + idx + '_ -->';
  });

  // Step 2: Display math  $$...$$ 和 \[...\]
  src = src.replace(/\$\$([\s\S]*?)\$\$/g, function(_, m) {
    var idx = mathBlocks.length;
    mathBlocks.push(m);
    return '\n<!-- _KB' + idx + '_ -->\n';
  });
  src = src.replace(/\\\[([\s\S]*?)\\\]/g, function(_, m) {
    var idx = mathBlocks.length;
    mathBlocks.push(m);
    return '\n<!-- _KB' + idx + '_ -->\n';
  });

  // Step 3: Inline math  $...$ 和 \(...\)
  src = src.replace(/(?<!\$)\$(?!\$)([^\n$]+?)(?<!\$)\$(?!\$)/g, function(_, m) {
    var idx = mathInlines.length;
    mathInlines.push(m);
    return '<!-- _KI' + idx + '_ -->';
  });
  src = src.replace(/\\\(([\s\S]*?)\\\)/g, function(_, m) {
    var idx = mathInlines.length;
    mathInlines.push(m);
    return '<!-- _KI' + idx + '_ -->';
  });

  // Step 4: 还原代码围栏（交给 marked 渲染）
  src = src.replace(/<!-- _CF(\d+)_ -->/g, function(_, i) {
    return codeFences[+i];
  });

  // Step 5: marked 解析（自定义代码块渲染，保留复制按钮）
  var _renderer = new marked.Renderer();
  _renderer.code = function(code, lang) {
    var c = (typeof code === 'object') ? (code.text || '') : (code || '');
    var l = (typeof code === 'object') ? (code.lang || '') : (lang || '');
    var escaped = escapeHtml(c);
    var langAttr = l ? ' data-lang="' + escapeHtml(l.trim()) + '"' : '';
    return '<pre' + langAttr + '><code>' + escaped + '</code>'
      + '<button class="code-copy-btn" onclick="copyCodeBlock(this)">复制</button></pre>';
  };

  var html = marked.parse(src, {
    renderer: _renderer,
    gfm: true,
    breaks: true,
    mangle: false,
    headerIds: false,
  });

  // Step 6a: 还原 display math
  html = html.replace(/<!-- _KB(\d+)_ -->/g, function(_, i) {
    try {
      return katex.renderToString(mathBlocks[+i], { displayMode: true, throwOnError: false });
    } catch (e) {
      return '<code>' + escapeHtml(mathBlocks[+i]) + '</code>';
    }
  });

  // Step 6b: 还原 inline math
  html = html.replace(/<!-- _KI(\d+)_ -->/g, function(_, i) {
    try {
      return katex.renderToString(mathInlines[+i], { displayMode: false, throwOnError: false });
    } catch (e) {
      return '<code>' + escapeHtml(mathInlines[+i]) + '</code>';
    }
  });

  return html;
}

/**
 * 基础 Markdown 渲染（降级 fallback，不依赖任何外部库）
 * 支持：代码块、行内代码、粗体、斜体、无序/有序列表
 */
function _renderMarkdownFallback(text) {
  if (!text) return '';
  var html = text;
  var codeBlocks = [];
  html = html.replace(/```([^\n]*)\n([\s\S]*?)```/g, function(match, lang, code) {
    var index = codeBlocks.length;
    var escaped = escapeHtml(code.replace(/\n$/, ''));
    var langLabel = lang ? ' data-lang="' + escapeHtml(lang.trim()) + '"' : '';
    var block = '<pre' + langLabel + '><code>' + escaped + '</code>'
      + '<button class="code-copy-btn" onclick="copyCodeBlock(this)">复制</button></pre>';
    codeBlocks.push(block);
    return '\n\x00CODEBLOCK_' + index + '\x00\n';
  });
  var inlineCodes = [];
  html = html.replace(/`([^`\n]+)`/g, function(match, code) {
    var index = inlineCodes.length;
    inlineCodes.push('<code>' + escapeHtml(code) + '</code>');
    return '\x00INLINE_' + index + '\x00';
  });
  html = html.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/(?<!\*)\*(?!\*)([\s\S]+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
  var lines = html.split('\n');
  var result = [];
  var inUl = false, inOl = false;
  for (var i = 0; i < lines.length; i++) {
    var line = lines[i].trimRight();
    var ulMatch = line.match(/^[-*]\s+(.+)$/);
    var olMatch = line.match(/^\d+\.\s+(.+)$/);
    if (ulMatch) {
      if (!inUl) { result.push('<ul>'); inUl = true; }
      if (inOl)  { result.push('</ol>'); inOl = false; }
      result.push('<li>' + ulMatch[1] + '</li>');
    } else if (olMatch) {
      if (!inOl) { result.push('<ol>'); inOl = true; }
      if (inUl)  { result.push('</ul>'); inUl = false; }
      result.push('<li>' + olMatch[1] + '</li>');
    } else {
      if (inUl) { result.push('</ul>'); inUl = false; }
      if (inOl) { result.push('</ol>'); inOl = false; }
      result.push(line === '' ? '' : line);
    }
  }
  if (inUl) result.push('</ul>');
  if (inOl) result.push('</ol>');
  html = result.join('\n');
  html = html.replace(/(<\/?(?:ul|ol|li)[^>]*>)\n/g, '$1');
  html = html.replace(/\n(<\/?(?:ul|ol|li)[^>]*>)/g, '$1');
  html = html.replace(/\n/g, '<br>');
  html = html.replace(/\x00INLINE_(\d+)\x00/g, function(match, index) { return inlineCodes[parseInt(index, 10)]; });
  html = html.replace(/<br>\x00CODEBLOCK_(\d+)\x00<br>/g, function(match, index) { return codeBlocks[parseInt(index, 10)]; });
  html = html.replace(/\x00CODEBLOCK_(\d+)\x00/g, function(match, index) { return codeBlocks[parseInt(index, 10)]; });
  return html;
}

/**
 * HTML 转义
 */
function escapeHtml(text) {
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}

/**
 * 代码块一键复制
 */
function copyCodeBlock(btn) {
  var pre = btn.parentElement;
  var code = pre.querySelector('code');
  if (!code) return;

  var text = code.textContent || code.innerText;
  navigator.clipboard.writeText(text).then(function() {
    btn.textContent = '已复制';
    btn.classList.add('copied');
    setTimeout(function() {
      btn.textContent = '复制';
      btn.classList.remove('copied');
    }, 2000);
  }).catch(function() {
    // Fallback: select and copy
    var range = document.createRange();
    range.selectNodeContents(code);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    try {
      document.execCommand('copy');
      btn.textContent = '已复制';
      btn.classList.add('copied');
      setTimeout(function() {
        btn.textContent = '复制';
        btn.classList.remove('copied');
      }, 2000);
    } catch (e) { /* ignore */ }
    sel.removeAllRanges();
  });
}

/**
 * 为已完成的 AI 气泡添加操作栏（复制整条回答 / 重新生成）。
 * 复制按钮加到每一条 AI 气泡；重生成只加到最后一条。
 */
function decorateAIBubbles() {
  var container = getMessagesContainer();
  if (!container) return;
  var bubbles = container.querySelectorAll('.message.ai');
  bubbles.forEach(function (bubble, idx) {
    var isLast = (idx === bubbles.length - 1);
    var existing = bubble.querySelector('.msg-actions');
    if (existing) existing.parentNode.removeChild(existing);
    // 正在流式的气泡不加操作栏
    if (bubble.classList.contains('streaming')) return;

    var bar = document.createElement('div');
    bar.className = 'msg-actions';

    var copyBtn = document.createElement('button');
    copyBtn.className = 'msg-action-btn';
    copyBtn.title = '复制回答';
    copyBtn.innerHTML = _iconCopy() + '<span>复制</span>';
    copyBtn.addEventListener('click', function () { copyAnswer(bubble, copyBtn); });
    bar.appendChild(copyBtn);

    if (isLast && typeof regenerateLast === 'function') {
      var regenBtn = document.createElement('button');
      regenBtn.className = 'msg-action-btn';
      regenBtn.title = '重新生成';
      regenBtn.innerHTML = _iconRegen() + '<span>重新生成</span>';
      regenBtn.addEventListener('click', function () { regenerateLast(); });
      bar.appendChild(regenBtn);
    }

    bubble.appendChild(bar);
  });
}

function _iconCopy() {
  return '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
}
function _iconRegen() {
  return '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/></svg>';
}

/**
 * 复制整条 AI 回答（优先用 data-raw 原始 Markdown）
 */
function copyAnswer(bubble, btn) {
  var text = bubble.getAttribute('data-raw') || bubble.textContent || '';
  var done = function () {
    if (!btn) return;
    var span = btn.querySelector('span');
    var orig = span ? span.textContent : '';
    btn.classList.add('copied');
    if (span) span.textContent = '已复制';
    setTimeout(function () {
      btn.classList.remove('copied');
      if (span) span.textContent = orig;
    }, 1800);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(done);
  } else {
    done();
  }
}

/**
 * 添加用户消息气泡（右对齐）
 */
function appendUserMessage(content) {
  var container = getMessagesContainer();
  var div = document.createElement('div');
  div.className = 'message user';
  div.textContent = content;
  container.appendChild(div);
  scrollToBottom();
  _persistMessagesHTML();
  return div;
}

/**
 * 创建 AI 回复消息气泡（空），返回引用以便后续追加
 */
function createAIMessageBubble() {
  var container = getMessagesContainer();
  var div = document.createElement('div');
  div.className = 'message ai';
  div.setAttribute('data-raw', '');
  container.appendChild(div);
  scrollToBottom();
  return div;
}

/**
 * 向 AI 消息气泡追加文本（打字机效果）
 * 累积原始文本到 data-raw 属性，每次重新渲染 Markdown
 */
function appendToAIMessage(bubble, text) {
  var raw = bubble.getAttribute('data-raw') || '';
  raw += text;
  bubble.setAttribute('data-raw', raw);
  bubble.innerHTML = renderMarkdown(raw);
  scrollToBottom();
  _persistMessagesHTML();
}

/**
 * 取得（或创建）当前"工具调用收纳盒"：
 * 同一轮回答内连续的多个工具调用会被放进同一个 <details> 收纳盒：
 *   - 折叠时：只显示一条消息条高度的 summary（名称/计数/整体状态）
 *   - 展开时：body 显示固定高度、可滚动的工具列表，每条仍可单独点开看详情
 *
 * 若最近一次 append 的不是 tool-group，则新开一个组，保证不同轮次的工具调用互相独立。
 * 返回 { group, body } 供调用方使用。
 */
function _getOrCreateToolGroup() {
  var container = getMessagesContainer();
  var last = container.lastElementChild;
  if (last && last.classList && last.classList.contains('tool-group')) {
    return { group: last, body: last.querySelector('.tool-group-body') };
  }

  var group = document.createElement('details');
  group.className = 'tool-group';
  // 运行中默认展开，让用户看到 agent 正在做什么；全部完成后自动收起
  group.open = true;

  var summary = document.createElement('summary');
  summary.className = 'tool-group-summary';

  var title = document.createElement('span');
  title.className = 'tool-group-title';
  title.textContent = '调用工具';

  var count = document.createElement('span');
  count.className = 'tool-group-count';
  count.textContent = '0';

  var status = document.createElement('span');
  status.className = 'tool-group-status running';
  status.textContent = '准备中…';

  summary.appendChild(title);
  summary.appendChild(count);
  summary.appendChild(status);

  var body = document.createElement('div');
  body.className = 'tool-group-body';

  group.appendChild(summary);
  group.appendChild(body);
  container.appendChild(group);
  return { group: group, body: body };
}

/**
 * 刷新工具收纳盒头部的计数与整体状态：
 *   - 存在运行中的条目 → "N 运行中…"（橙黄脉动点）
 *   - 全部完成         → "全部完成"（绿色 ✓）
 */
function _updateToolGroupHeader(group) {
  if (!group) return;
  var body = group.querySelector('.tool-group-body');
  var items = body ? body.querySelectorAll('.tool-indicator') : [];
  var total = items.length;
  var running = 0;
  var names = [];
  for (var i = 0; i < items.length; i++) {
    var s = items[i].querySelector('.tool-status');
    if (s && s.classList.contains('running')) running++;
    var n = items[i].querySelector('.tool-name');
    if (n && names.indexOf(n.textContent) === -1) names.push(n.textContent);
  }
  var countEl = group.querySelector('.tool-group-count');
  if (countEl) countEl.textContent = String(total);

  // 标题直接显示工具名（多个时“首个 +N”），一眼可见 agent 在干什么
  var titleEl = group.querySelector('.tool-group-title');
  if (titleEl) {
    if (names.length === 0) {
      titleEl.textContent = '调用工具';
    } else if (names.length === 1) {
      titleEl.textContent = names[0];
    } else {
      titleEl.textContent = names[0] + '  +' + (names.length - 1);
    }
  }

  var statusEl = group.querySelector('.tool-group-status');
  if (!statusEl) return;
  if (running > 0) {
    statusEl.className = 'tool-group-status running';
    statusEl.textContent = running + ' 运行中…';
    group.removeAttribute('data-autocollapse');
  } else {
    statusEl.className = 'tool-group-status done';
    statusEl.textContent = total > 0 ? '全部完成' : '';
    // 全部完成：短暂停顿后自动收起，保持会话清爽（仅自动收一次，不干扰用户手动展开）
    if (total > 0 && !group.hasAttribute('data-autocollapse')) {
      group.setAttribute('data-autocollapse', '1');
      setTimeout(function () {
        if (group.getAttribute('data-autocollapse') === '1') group.open = false;
      }, 1100);
    }
  }
}

/**
 * 渲染工具调用开始指示器（可折叠 <details>）
 * 返回指示器元素引用
 */
function renderToolStart(name, args) {
  var grp = _getOrCreateToolGroup();
  var details = document.createElement('details');
  details.className = 'tool-indicator';

  var summary = document.createElement('summary');
  var nameSpan = document.createElement('span');
  nameSpan.className = 'tool-name';
  nameSpan.textContent = name;

  var statusSpan = document.createElement('span');
  statusSpan.className = 'tool-status running';
  statusSpan.textContent = '运行中…';

  summary.appendChild(nameSpan);
  summary.appendChild(statusSpan);
  details.appendChild(summary);

  // 展示参数
  if (args) {
    var body = document.createElement('div');
    body.className = 'tool-body';
    body.textContent = typeof args === 'string' ? args : JSON.stringify(args, null, 2);
    details.appendChild(body);
  }

  grp.body.appendChild(details);
  // 展开状态下自动滚到最新一条
  grp.body.scrollTop = grp.body.scrollHeight;
  _updateToolGroupHeader(grp.group);
  scrollToBottom();
  _persistMessagesHTML();
  return details;
}

/**
 * 更新工具调用指示器为完成状态
 */
function renderToolEnd(indicator, name, output) {
  if (!indicator) return;

  // 更新状态标签
  var statusSpan = indicator.querySelector('.tool-status');
  if (statusSpan) {
    statusSpan.className = 'tool-status done';
    statusSpan.textContent = '完成';
  }

  // 添加或更新输出内容
  var body = indicator.querySelector('.tool-body');
  if (!body) {
    body = document.createElement('div');
    body.className = 'tool-body';
    indicator.appendChild(body);
  }
  body.textContent = output || '';

  // 同步更新外层收纳盒头部的聚合状态
  var group = indicator.closest ? indicator.closest('.tool-group') : null;
  if (group) _updateToolGroupHeader(group);

  scrollToBottom();
  _persistMessagesHTML();
}

/**
 * 渲染错误消息气泡（红色）
 */
function renderError(message) {
  var container = getMessagesContainer();
  var div = document.createElement('div');
  div.className = 'message error';
  div.textContent = message;
  container.appendChild(div);
  scrollToBottom();
}

/**
 * 加载并渲染历史消息
 * messages: Array<{
 *   type: "human" | "ai" | "tool",
 *   content: string,
 *   tool_calls?: Array<{id: string, name: string, args: any}>,  // ai only
 *   tool_call_id?: string,                                      // tool only
 *   name?: string                                               // tool only
 * }>
 *
 * 行为：
 * - human → 用户气泡
 * - ai → 若有 content 才渲染 AI 气泡；若有 tool_calls 则依次渲染 .tool-indicator
 *        到当前 .tool-group（与实时流式一致，支持点击展开）
 * - tool → 按 tool_call_id 匹配并填充对应指示器的输出，标记为已完成
 */
function renderHistory(messages) {
  var container = getMessagesContainer();
  container.innerHTML = '';

  if (!messages || !messages.length) return;

  // tool_call_id → { indicator, name } 映射，用于把 ToolMessage 的输出挂到对应指示器
  var toolById = {};

  for (var i = 0; i < messages.length; i++) {
    var msg = messages[i];

    if (msg.type === 'human') {
      appendUserMessage(msg.content || '');
      continue;
    }

    if (msg.type === 'ai') {
      // 1) 只有非空文本才创建气泡，避免"纯 tool_calls 的 AI 消息"显示成空白条
      var text = msg.content || '';
      if (text && text.trim()) {
        var bubble = createAIMessageBubble();
        bubble.setAttribute('data-raw', text);
        bubble.innerHTML = renderMarkdown(text);
      }
      // 2) 渲染工具调用（进入当前 .tool-group 收纳盒）
      var tcs = msg.tool_calls || [];
      for (var t = 0; t < tcs.length; t++) {
        var tc = tcs[t] || {};
        var indicator = renderToolStart(tc.name || 'tool', tc.args);
        if (tc.id) {
          toolById[tc.id] = { indicator: indicator, name: tc.name || 'tool' };
        }
      }
      continue;
    }

    if (msg.type === 'tool') {
      var entry = msg.tool_call_id ? toolById[msg.tool_call_id] : null;
      if (entry && entry.indicator) {
        renderToolEnd(entry.indicator, entry.name, msg.content || '');
      } else {
        // 没匹配到（通常是 checkpoint 缺上游 AI 消息）— 兜底单独渲一条已完成项
        var fallback = renderToolStart(msg.name || 'tool', null);
        renderToolEnd(fallback, msg.name || 'tool', msg.content || '');
      }
      continue;
    }

    // 其他类型（system 等）忽略
  }
  scrollToBottom();
  if (typeof decorateAIBubbles === 'function') decorateAIBubbles();
  _persistMessagesHTML();
}
