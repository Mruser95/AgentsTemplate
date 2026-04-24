import asyncio
import base64
import json
import sys
from typing import Type, Literal, Any
from pathlib import Path
import yaml
from pydantic import BaseModel, Field, PrivateAttr
from langchain_core.tools import BaseTool
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / '.env')

from Tools._context import bump_budget, current_thread_id  # noqa: E402

with open(PROJECT_ROOT / 'config.yaml', 'r', encoding='utf-8') as file:
    config = yaml.safe_load(file)

browser_count_limit: int = config.get('browser_count_limit', 15)
browser_headless: bool = config.get('browser_headless', True)
browser_timeout: int = config.get('browser_timeout', 30000)  # ms
browser_allowed_domains: list[str] = config.get('browser_allowed_domains', [])


# Per-instance browser session ===========================================


class BrowserSession:
    def __init__(self, headless: bool, timeout: int):
        self.headless = headless
        self.timeout = timeout
        self._playwright = None
        self._browser = None
        self._page = None
        self._lock = asyncio.Lock()

    async def page(self):
        async with self._lock:
            if self._page and not self._page.is_closed():
                return self._page
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._page = await self._browser.new_page()
            self._page.set_default_timeout(self.timeout)
            return self._page

    async def close(self):
        async with self._lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
            self._playwright = self._browser = self._page = None


def check_domain(url: str) -> tuple[bool, str]:
    if not browser_allowed_domains:
        return True, ""
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    for domain in browser_allowed_domains:
        if host == domain or host.endswith("." + domain):
            return True, ""
    return False, f"Domain '{host}' is not in the allowed list: {browser_allowed_domains}"


# Action implementations ================================================


async def _navigate(session: BrowserSession, url: str, **_) -> str:
    ok, reason = check_domain(url)
    if not ok:
        return f"Navigation denied: {reason}"
    page = await session.page()
    resp = await page.goto(url, wait_until="domcontentloaded")
    status = resp.status if resp else "unknown"
    title = await page.title()
    return f"Navigated to {url} (status={status}, title=\"{title}\")"


async def _click(session: BrowserSession, selector: str, **_) -> str:
    page = await session.page()
    await page.click(selector)
    title = await page.title()
    return f"Clicked '{selector}'. Current page title: \"{title}\""


async def _type(session: BrowserSession, selector: str, text: str = "", **_) -> str:
    page = await session.page()
    await page.fill(selector, text)
    return f"Typed into '{selector}'."


async def _get_text(session: BrowserSession, selector: str = "body", **_) -> str:
    page = await session.page()
    el = page.locator(selector)
    text = await el.inner_text()
    if len(text) > 5000:
        text = text[:5000] + "\n...[truncated]"
    return text


async def _screenshot(session: BrowserSession, **_) -> str:
    page = await session.page()
    buf = await page.screenshot(full_page=False)
    b64 = base64.b64encode(buf).decode()
    title = await page.title()
    url = page.url
    return (
        f"Screenshot taken (page: \"{title}\", url: {url}).\n"
        f"[base64_png, {len(buf)} bytes]\n{b64[:200]}..."
    )


async def _get_html(session: BrowserSession, selector: str = "body", **_) -> str:
    page = await session.page()
    el = page.locator(selector)
    html = await el.inner_html()
    if len(html) > 8000:
        html = html[:8000] + "\n...[truncated]"
    return html


async def _scroll(session: BrowserSession, direction: str = "down", **_) -> str:
    page = await session.page()
    delta = 600 if direction == "down" else -600
    await page.mouse.wheel(0, delta)
    await page.wait_for_timeout(300)
    return f"Scrolled {direction}."


async def _select(session: BrowserSession, selector: str, text: str = "", **_) -> str:
    page = await session.page()
    await page.select_option(selector, label=text)
    return f"Selected option '{text}' in '{selector}'."


async def _wait(session: BrowserSession, selector: str, **_) -> str:
    page = await session.page()
    await page.wait_for_selector(selector)
    return f"Element '{selector}' appeared."


async def _eval_js(session: BrowserSession, text: str = "", **_) -> str:
    page = await session.page()
    result = await page.evaluate(text)
    try:
        out = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        out = repr(result)
    if len(out) > 5000:
        out = out[:5000] + "\n...[truncated]"
    return f"eval_js result: {out}"


async def _press_key(session: BrowserSession, text: str = "", selector: str = "", **_) -> str:
    page = await session.page()
    if not text:
        return "press_key requires 'text' (e.g. 'Enter', 'Control+L')."
    if selector:
        await page.locator(selector).press(text)
        return f"Pressed '{text}' on '{selector}'."
    await page.keyboard.press(text)
    return f"Pressed '{text}'."


async def _get_links(session: BrowserSession, selector: str = "", **_) -> str:
    page = await session.page()
    scope_js = f"document.querySelector({json.dumps(selector)})" if selector else "document"
    js = f"""
    (() => {{
      const root = {scope_js};
      if (!root) return [];
      const q = 'a[href], button, input, textarea, select, [role=button], [role=link]';
      const nodes = Array.from(root.querySelectorAll(q));
      const visible = (el) => {{
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
      }};
      return nodes.filter(visible).slice(0, 80).map((el, i) => {{
        const tag = el.tagName.toLowerCase();
        const id = el.id ? '#' + el.id : '';
        const name = el.getAttribute('name');
        const type = el.getAttribute('type');
        const href = el.getAttribute('href');
        const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 80);
        let sel = id || (name ? `${{tag}}[name=\"${{name}}\"]` : '') || (href ? `${{tag}}[href=\"${{href}}\"]` : '') || `${{tag}}:nth-of-type(${{i + 1}})`;
        return {{ tag, type, selector: sel, text: label, href }};
      }});
    }})()
    """
    items = await page.evaluate(js)
    out = json.dumps(items, ensure_ascii=False, indent=2)
    if len(out) > 6000:
        out = out[:6000] + "\n...[truncated]"
    return f"{len(items)} interactive element(s):\n{out}"


async def _close(session: BrowserSession, **_) -> str:
    await session.close()
    return "Browser closed."


ACTIONS = {
    "navigate": _navigate,
    "click": _click,
    "type": _type,
    "get_text": _get_text,
    "screenshot": _screenshot,
    "get_html": _get_html,
    "scroll": _scroll,
    "select": _select,
    "wait": _wait,
    "eval_js": _eval_js,
    "press_key": _press_key,
    "get_links": _get_links,
    "close": _close,
}


# LangChain Tool definition ==============================================


class BrowserInput(BaseModel):
    action: Literal[
        "navigate", "click", "type", "get_text",
        "screenshot", "get_html", "scroll", "select", "wait",
        "eval_js", "press_key", "get_links", "close",
    ] = Field(description="The browser action to perform.")
    url: str = Field(default="", description="URL for 'navigate' action.")
    selector: str = Field(
        default="",
        description=(
            "CSS selector. Used by click/type/get_text/get_html/select/wait/get_links; "
            "optional scoping selector for press_key."
        ),
    )
    text: str = Field(
        default="",
        description=(
            "Text for 'type'; option label for 'select'; JS expression for 'eval_js'; "
            "key or combo (e.g. 'Enter', 'Control+A') for 'press_key'."
        ),
    )
    direction: str = Field(default="down", description="'up' or 'down' for 'scroll' action.")


async def _execute_action(session: BrowserSession, action: str, **kwargs) -> str:
    fn = ACTIONS.get(action)
    if fn is None:
        return f"Unknown action: {action}. Available: {list(ACTIONS.keys())}"
    try:
        return await fn(session, **kwargs)
    except Exception as e:
        return f"Browser action '{action}' failed: {e!r}"


def _run_coro_sync(coro: Any) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import threading
    result: dict = {}
    def runner():
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as e:
            result["error"] = e
    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


class Browser(BaseTool):
    name: str = "browser"
    description: str = (
        "Control a headless browser via Playwright. "
        "Actions: navigate, click, type, get_text, screenshot, get_html, "
        "scroll, select, wait, eval_js, press_key, get_links, close."
    )
    args_schema: Type[BaseModel] = BrowserInput
    max_tool_calls: int = Field(default=browser_count_limit)
    _call_counts: dict[str, int] = PrivateAttr(default_factory=dict)
    _sessions: dict[str, BrowserSession] = PrivateAttr(default_factory=dict)

    def _get_session(self, thread_id: str) -> BrowserSession:
        sess = self._sessions.get(thread_id)
        if sess is None:
            sess = BrowserSession(headless=browser_headless, timeout=browser_timeout)
            self._sessions[thread_id] = sess
        return sess

    def reset(self):
        self._call_counts.clear()

    def _budget_response(self, tid: str) -> str:
        return (
            f"Tool call limit reached ({self.max_tool_calls}) for thread {tid}. "
            "Stop using tools and respond directly."
        )

    def _run(
        self, action: str, url: str = "", selector: str = "", text: str = "", direction: str = "down"
    ) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        sess = self._get_session(tid)
        result = _run_coro_sync(
            _execute_action(sess, action, url=url, selector=selector, text=text, direction=direction)
        )
        if action == "close":
            self._sessions.pop(tid, None)
        return f"{result}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"

    async def _arun(
        self, action: str, url: str = "", selector: str = "", text: str = "", direction: str = "down"
    ) -> str:
        tid = current_thread_id()
        ok, n, rem = bump_budget(self._call_counts, tid, self.max_tool_calls)
        if not ok:
            return self._budget_response(tid)
        sess = self._get_session(tid)
        result = await _execute_action(
            sess, action, url=url, selector=selector, text=text, direction=direction
        )
        if action == "close":
            self._sessions.pop(tid, None)
        return f"{result}\n\n[Tool call {n}/{self.max_tool_calls}, remaining: {rem}]"
