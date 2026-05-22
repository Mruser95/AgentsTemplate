---
tool: edit
description: 写 / 修改 workspace 内的文件（替代 cat>EOF / python -c open().write / sed -i）
---

# edit Tool — SKILL.md

## 概览
`edit` 用于**写 / 修改 workspace 内的文件**，比 shell 重定向 / 内联 Python / `sed` 安全得多。

**核心原则：读是便宜的，写要精准。** 先 `terminal: cat / sed -n` 之类定位，再用
**最小范围的 `str_replace`** 改；只有新建或大规模重写才用全文写入。

---

## ⛔ 硬约束

### 1. 路径只能落 workspace
`path` 必须是**相对当前 workspace 的相对路径**：
- ✅ `image_spider.py`、`pkg/utils.py`
- ❌ `/Users/.../foo.py`（绝对路径）、`../escape.py`（越界）

父目录不存在会自动创建。

### 2. 四种 mode
| mode | 用途 | 必填字段 |
|---|---|---|
| `str_replace` | **局部修改（最高频）**：把文件中**唯一出现**的 `old_str` 改成 `new_str` | `old_str` + `new_str` |
| `insert` | 在第 `insert_line` 行之后插入 `new_str`（0=最前；=总行数=追加到末尾） | `insert_line` + `new_str` |
| `create` | 仅当文件**不存在**时写入；已存在直接报错（防误覆盖） | `content` |
| `overwrite` | 不论是否存在，都用 `content` 整文件覆盖 | `content` |

`str_replace` 的 `old_str` **必须在文件中唯一匹配**（含空白与缩进）——匹配 0 次或
>1 次都会报错。所以请带上**足够的上下文**（前后多带几行 / 多带函数签名）。

### 3. 写过的文件必须登记到 file_changes
任何用 `edit` 写过 / 改过的文件，都要在最终 CoderReport 的 `file_changes` 里出现
（`action='create'` 或 `'modify'`），否则上层 **lint gate 不会跑语法检查**，你写错
的语法不会被自动打回。

---

## 📐 推荐工作流

**改已有文件里的某个函数：**
```
1. terminal: sed -n '40,80p' service.py     # 先看清楚要改的片段
2. edit(path='service.py', mode='str_replace',
        old_str='def handle(req):\n    return req.body',
        new_str='def handle(req):\n    log.info("hit")\n    return req.body')
3. terminal: python -m py_compile service.py  # 自检
4. （CoderReport.file_changes 登记 service.py: modify）
```

**新建一个脚本 + 依赖：**
```
1. edit(path='requirements.txt', content='requests\nbeautifulsoup4\n', mode='create')
2. edit(path='spider.py', content='<完整脚本文本>', mode='create')
3. terminal: python -m py_compile spider.py
```

**注意：**
- `content` / `old_str` / `new_str` 都是**原样**写入，**不要**手工转义换行 / 引号。
- 单次调用就把内容传完；不要把同一个新文件拆成多次 `insert` 拼接。

---

## ❌ 反模式（看到自己在写就停下来）

| 反模式 | 改用 |
|---|---|
| `terminal: cat > x.py << 'EOF' ...` | `edit('x.py', content, 'create')` |
| `terminal: python3 -c "open('x.py','w').write('...')"` | 同上 |
| `terminal: echo '...' > x.py` | 同上 |
| `terminal: sed -i 's/A/B/' x.py` 局部改 | `edit(... mode='str_replace')` |
| 改 5 行就 `overwrite` 整文件 | 用 `str_replace` 只动那 5 行 |
| `old_str` 太短匹配多处 → 反复试 | 加更多上下文让 `old_str` 唯一 |

---

## 🔁 错误处理

| 返回 | 含义 | 处理 |
|---|---|---|
| `str_replace 失败：old_str 匹配 0 处` | 空白 / 换行 / 缩进对不上 | 重新读文件，原样复制 |
| `str_replace 失败：old_str 匹配 N 处` | 上下文不够 | 加更多前后行让它唯一 |
| `create 失败：文件已存在` | 想新建但已有 | 改 `mode='overwrite'` 或换路径 |
| `insert 失败：insert_line 越界` | 行号超过总行数 | 先看文件行数 |
| `path 必须是相对路径` / `path 越界 workspace` | 路径不合法 | 改成 workspace 内的相对路径 |
| `Tool call limit reached` | 调用预算耗尽 | 停止写入，直接产出 CoderReport |

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
...
```