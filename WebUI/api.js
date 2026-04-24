/* ============================================================
   api.js — API 封装层
   统一请求方法、API Key 管理、401 自动处理
   ============================================================ */

/**
 * API 基础 URL
 *   - 同源部署（前端由后端一起提供）：留空，走相对路径
 *   - 分离部署（前端单独静态托管）：指向后端绝对地址，如 http://127.0.0.1:8973
 * 这里默认指向本地 8973；生产同源部署时改成空串即可
 */
var API_BASE = 'http://127.0.0.1:8973';

var API_KEY_STORAGE_KEY = 'agent_webui_api_key';

/**
 * 从 localStorage 获取已存储的 API Key
 * @returns {string|null}
 */
function getApiKey() {
  return localStorage.getItem(API_KEY_STORAGE_KEY);
}

/**
 * 将 API Key 存储到 localStorage
 * @param {string} key
 */
function setApiKey(key) {
  localStorage.setItem(API_KEY_STORAGE_KEY, key);
}

/**
 * 清除 localStorage 中的 API Key
 */
function clearApiKey() {
  localStorage.removeItem(API_KEY_STORAGE_KEY);
}

/**
 * 构建带有 x-api-key 的请求 headers
 * @returns {Object}
 */
function _buildHeaders(extra) {
  var headers = {};
  var key = getApiKey();
  if (key) {
    headers['x-api-key'] = key;
  }
  if (extra) {
    for (var k in extra) {
      if (extra.hasOwnProperty(k)) {
        headers[k] = extra[k];
      }
    }
  }
  return headers;
}

/**
 * 处理 401 响应：清除 API Key，切换到认证视图，显示"认证已过期"提示
 * @param {Response} response
 */
function _handle401(response) {
  if (response.status === 401) {
    clearApiKey();
    // 切换到认证视图（如果 showView 已定义）
    if (typeof showView === 'function') {
      showView('auth');
    }
    // 在认证界面显示过期提示
    var errorEl = document.getElementById('auth-error');
    if (errorEl) {
      errorEl.textContent = '认证已过期，请重新输入 API Key';
    }
  }
}

/**
 * 通用 GET 请求，自动注入 x-api-key header
 * 网络不可达时显示错误横幅
 * @param {string} path — 相对路径，如 "/health"
 * @returns {Promise<Response>}
 */
async function apiGet(path) {
  var response;
  try {
    response = await fetch(API_BASE + path, {
      method: 'GET',
      headers: _buildHeaders()
    });
  } catch (err) {
    if (typeof showErrorBanner === 'function') {
      showErrorBanner('无法连接到服务器');
    }
    throw err;
  }
  _handle401(response);
  return response;
}

/**
 * 通用 POST 请求，自动注入 x-api-key header
 * 网络不可达时显示错误横幅
 * @param {string} path — 相对路径，如 "/chat/thread_1"
 * @param {Object} body — 请求体对象
 * @returns {Promise<Response>}
 */
async function apiPost(path, body) {
  var response;
  try {
    response = await fetch(API_BASE + path, {
      method: 'POST',
      headers: _buildHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body)
    });
  } catch (err) {
    if (typeof showErrorBanner === 'function') {
      showErrorBanner('无法连接到服务器');
    }
    throw err;
  }
  _handle401(response);
  return response;
}
