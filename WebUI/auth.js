/* ============================================================
   auth.js — Auth_Module
   API Key 认证：输入、验证、存储、清除
   ============================================================ */

/**
 * 初始化认证模块
 * 检查 localStorage 中是否有 API Key，决定显示认证界面或聊天界面
 */
function initAuth() {
  if (getApiKey()) {
    // 已有 Key，直接进入聊天
    if (typeof showView === 'function') {
      showView('chat');
    }
  } else {
    if (typeof showView === 'function') {
      showView('auth');
    }
  }

  // 绑定提交按钮
  var submitBtn = document.getElementById('auth-submit-btn');
  if (submitBtn) {
    submitBtn.addEventListener('click', function () {
      var input = document.getElementById('api-key-input');
      if (input) {
        submitApiKey(input.value);
      }
    });
  }

  // 绑定 Enter 键提交
  var input = document.getElementById('api-key-input');
  if (input) {
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitApiKey(input.value);
      }
    });
  }
}

/**
 * 提交 API Key 进行验证
 * 空白校验 → 调用 GET /threads/{threadId}/messages 验证（该端点带鉴权依赖）→ 存储到 localStorage
 * 注意：使用受保护接口而非 /health，因为 /health 无鉴权，错误 key 也会通过
 * @param {string} key
 * @returns {Promise<{success: boolean, error?: string}>}
 */
async function submitApiKey(key) {
  var errorEl = document.getElementById('auth-error');
  var submitBtn = document.getElementById('auth-submit-btn');

  // 空白校验
  if (!key || !key.trim()) {
    if (errorEl) errorEl.textContent = '请输入 API Key';
    return { success: false, error: '请输入 API Key' };
  }

  // 防止连击：按钮进入 loading 态
  if (submitBtn && submitBtn.disabled) return { success: false, error: 'busy' };
  var originalText = submitBtn ? submitBtn.textContent : '';
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.textContent = '连接中…';
  }
  if (errorEl) errorEl.textContent = '';

  // 先临时存储 Key 以便 apiGet 能注入 header
  setApiKey(key.trim());

  var threadId = typeof getThreadId === 'function' ? getThreadId() : 'probe';

  var finish = function () {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.textContent = originalText || '连接';
    }
  };

  try {
    var response = await apiGet('/threads/' + threadId + '/messages');
    if (response.ok) {
      if (errorEl) errorEl.textContent = '';
      if (typeof showView === 'function') showView('chat');
      finish();
      return { success: true };
    }
    // 401/其他：清除 key，给出明确错误提示
    clearApiKey();
    if (errorEl) {
      errorEl.textContent = response.status === 401
        ? 'API Key 无效，请检查后重试'
        : '验证失败 (HTTP ' + response.status + ')';
    }
    finish();
    return { success: false, error: 'auth failed' };
  } catch (err) {
    clearApiKey();
    if (errorEl) errorEl.textContent = '无法连接到服务器';
    finish();
    return { success: false, error: '无法连接到服务器' };
  }
}

/**
 * 退出登录：清除 API Key，切换到认证视图
 */
function logout() {
  clearApiKey();
  // 清空错误提示
  var errorEl = document.getElementById('auth-error');
  if (errorEl) {
    errorEl.textContent = '';
  }
  // 清空输入框
  var input = document.getElementById('api-key-input');
  if (input) {
    input.value = '';
  }
  if (typeof showView === 'function') {
    showView('auth');
  }
}

/**
 * 检查是否已认证
 * @returns {boolean}
 */
function isAuthenticated() {
  return !!getApiKey();
}
