---
tool: write_file
description: 把整段文本写入 workspace 内的文件（替代 cat>EOF / python -c open().write）
---

# write_file Tool — SKILL.md

## 概览
`write_file` 用于**把整段文本原样写入 workspace 内的文件**。适合：
- 新建源码文件（如 `image_spider.py`、`requirements.txt`）
- 整文件覆盖一个已有文件
- 在末尾追加内容

**这是写新文件 / 整文件覆盖的首选工具**——比 shell 重定向 / 内联 Python 安全得多。

---

## ⛔ 硬约束

### 1. 路径只能落 workspace
`path` 必须是**相对当前 workspace 的相对路径**：
- ✅ `image_spider.py`
- ✅ `pkg/utils.py`
- ❌ `/Users/.../foo.py`（绝对路径）
- ❌ `../escape.py`（越界）

父目录不存在会自动创建。

### 2. 三种 mode 选一
| mode | 行为 |
|---|---|
| `create` | 仅当文件**不存在**时写入；已存在直接报错（防误覆盖） |
| `overwrite` | 不论是否存在，都用 `content` 整文件覆盖 |
| `append` | 追加到文件末尾；文件不存在则新建 |

不确定就先用 `create`；明确要重写历史文件再用 `overwrite`。

### 3. 写过的文件必须登记到 file_changes
任何用 `write_file` 写过 / 改过的文件，都要在最终 CoderReport 的 `file_changes`
里出现（`action='create'` 或 `'modify'`），否则上层 **lint gate 不会跑语法检查**，
你写错的语法不会被自动打回。

---

## 📐 推荐工作流

**新建一个 Python 脚本 + 它的依赖：**
```
1. write_file(path='requirements.txt', content='requests\nbeautifulsoup4\nlxml\n', mode='create')
2. write_file(path='image_spider.py', content='<完整脚本文本>', mode='create')
3. terminal: python -m py_compile image_spider.py    # 自检语法
4. （写 CoderReport，把这两个文件都登记到 file_changes）
```

**注意：**
- `content` 是**原样**写入，**不要**手工对换行 / 引号做转义；直接传 Python 字符串字面量即可。
- 单次调用就把整文件内容传完；不要分多次 append 拼接同一个新文件。

---

## ❌ 反模式（看到自己在写就停下来）

| 反模式 | 改用 |
|---|---|
| `terminal: cat > x.py << 'EOF' ...` | `write_file('x.py', content, 'create')` |
| `terminal: python3 -c "open('x.py','w').write('...')"` | 同上 |
| `terminal: echo '...' > x.py` | 同上 |
| `terminal: sed -i 's/A/B/' x.py` 大段重写 | `write_file('x.py', new_content, 'overwrite')` |
| 多次 `append` 拼出新文件 | 一次 `create`/`overwrite` 传完整文本 |

---

## 🔁 错误处理

| 返回 | 含义 | 处理 |
|---|---|---|
| `create 失败：文件已存在` | 用 `create` 写一个已有文件 | 改 `mode='overwrite'`，或换 path |
| `path 必须是相对路径` | 传了绝对路径 | 改成 workspace 内的相对路径 |
| `path 越界 workspace` | 用 `../` 跳出去了 | 留在 workspace 内 |
| `Tool call limit reached` | 调用预算耗尽 | 停止写入，直接产出 CoderReport |

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
...
```