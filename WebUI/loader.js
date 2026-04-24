/* ============================================================
   loader.js — 动态加载 marked.js + KaTeX，多 CDN 降级
   优先本地 lib/ → bootcdn（国内友好） → jsdelivr → unpkg
   加载完成后自动重渲染 fallback 渲染的 AI 消息
   ============================================================ */
(function () {
  /* ---------- CDN 源列表 ---------- */
  var KATEX_CSS = [
    'lib/katex.min.css',
    'https://cdn.bootcdn.net/ajax/libs/KaTeX/0.16.9/katex.min.css',
    'https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css',
    'https://unpkg.com/katex@0.16.9/dist/katex.min.css',
  ];
  var MARKED_JS = [
    'lib/marked.min.js',
    'https://cdn.bootcdn.net/ajax/libs/marked/4.3.0/marked.min.js',
    'https://cdn.jsdelivr.net/npm/marked@4.3.0/marked.min.js',
    'https://unpkg.com/marked@4.3.0/marked.min.js',
  ];
  var KATEX_JS = [
    'lib/katex.min.js',
    'https://cdn.bootcdn.net/ajax/libs/KaTeX/0.16.9/katex.min.js',
    'https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js',
    'https://unpkg.com/katex@0.16.9/dist/katex.min.js',
  ];

  /* ---------- 工具函数 ---------- */

  /** 依次尝试加载 CSS，第一个成功即停止 */
  function tryLoadCSS(urls, idx) {
    if (idx >= urls.length) {
      console.warn('[loader] KaTeX CSS: all sources failed');
      return;
    }
    var link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = urls[idx];
    link.onload = function () {
      console.log('[loader] KaTeX CSS ←', urls[idx]);
    };
    link.onerror = function () {
      if (link.parentNode) link.parentNode.removeChild(link);
      tryLoadCSS(urls, idx + 1);
    };
    document.head.appendChild(link);
  }

  /** 依次尝试加载 JS，第一个成功即停止，回调 cb(true/false) */
  function tryLoadScript(urls, idx, cb) {
    if (idx >= urls.length) { cb(false); return; }
    var s = document.createElement('script');
    s.src = urls[idx];
    s.onload = function () {
      console.log('[loader] Loaded:', urls[idx]);
      cb(true);
    };
    s.onerror = function () {
      if (s.parentNode) s.parentNode.removeChild(s);
      tryLoadScript(urls, idx + 1, cb);
    };
    document.head.appendChild(s);
  }

  /* ---------- 加载完成后重渲染 ---------- */

  var _ready = 0;

  function _onLibLoaded() {
    _ready++;
    if (_ready < 2) return;                     // 等 marked + katex 都到齐
    console.log('[loader] marked + KaTeX ready — re-rendering');
    _reRenderAI();
  }

  /** 重渲染所有 AI 消息（用 data-raw 里保存的原始 Markdown） */
  function _reRenderAI() {
    if (typeof renderMarkdown !== 'function') {
      // render.js 可能还没加载完，等一会儿再试
      setTimeout(_reRenderAI, 80);
      return;
    }
    var msgs = document.querySelectorAll('.message.ai[data-raw]');
    for (var i = 0; i < msgs.length; i++) {
      var raw = msgs[i].getAttribute('data-raw');
      if (raw) {
        msgs[i].innerHTML = renderMarkdown(raw);
      }
    }
    if (typeof stickToBottomFor === 'function') {
      stickToBottomFor(1200);
    } else if (typeof forceScrollToBottom === 'function') {
      forceScrollToBottom();
    }
  }

  /* ---------- 开始异步加载 ---------- */
  tryLoadCSS(KATEX_CSS, 0);

  tryLoadScript(MARKED_JS, 0, function (ok) {
    if (!ok) console.warn('[loader] marked.js: all sources failed');
    _onLibLoaded();
  });

  tryLoadScript(KATEX_JS, 0, function (ok) {
    if (!ok) console.warn('[loader] katex.js: all sources failed');
    _onLibLoaded();
  });
})();
