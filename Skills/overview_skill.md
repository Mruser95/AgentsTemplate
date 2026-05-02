---
tool: repo_map, grep, glob
description: 先 overview 再 read —— 用 AST repo map / glob / grep 把"该读哪一段"问题答清楚
---

# overview Tools — SKILL.md

三个互相独立的"先看再读"工具，目的都是 **不让 LLM 一次吞整个项目**：

| 工具 | 解决什么 | 输出形态 |
|---|---|---|
| `repo_map` | 我刚进项目，结构是什么？哪个文件最核心？ | AST 抽签名 + PageRank 排序的骨架 |
| `glob` | 哪些文件名 / 路径符合某模式？ | 路径列表（带上限） |
| `grep` | 这个符号 / 字符串出现在哪儿？ | `path:line:content`（带上限） |

> **核心原则**：读是便宜的，但**整文件 read 是最贵的**。先 overview，定位到 ≤30 行的小段，再 `read_file`。

---

## 推荐流程

```
1. repo_map(top_n=10)            # 摸清项目骨架，看 PageRank 找核心模块
2. glob(pattern='Tools/**/*.py') # 收窄到关心的目录
3. grep(pattern='class Edit',
        glob='*.py')             # 找符号定义 / 调用点
4. read_file(path, start, end)   # 只读那一小段
5. edit(... mode='str_replace')  # 最小修改
```

---

## ⛔ 硬约束

- **路径只能落 workspace**：`path` / `glob root` 必须是 workspace 内相对路径；
  绝对路径、`../` 越界都会被拒。
- **每个工具都有调用预算**：`repo_map`/调用 20 次、`grep`/`glob` 各 30 次；
  返回里会显示剩余次数，用完就停。
- **每个工具都有输出上限**：超出会截断并提示，**不要靠多调几次拼回全量**——
  正确做法是收窄 `path` / `glob` / `pattern`。

---

## `repo_map`

```python
repo_map(path='', top_n=20, max_symbols_per_file=25)
```

- `path`：要扫的子目录，空=workspace 根；只看 `.py` 文件。
- `top_n`：按 PageRank 取前 N 个文件**展开签名**，其余只列路径 + 排名。
- `max_symbols_per_file`：单文件最多列出的类 / 函数签名数。

输出长这样：
```
## Tools/utils.py  (rank=0.0618)
  def workspace_dir(thread_id: str)
  def venv_dir(thread_id: str)
  ...
- Tools/edit.py  (rank=0.0070, 10 symbols)
```

**适合**：第一次进项目；找该读哪个文件。
**不适合**：看具体实现（只有签名，没有函数体）。

---

## `glob`

```python
glob(pattern='**/*.py', path='', max_results=200)
```

- 自动跳过 `.venv` / `__pycache__` / `node_modules` / `.git` / `dist` / 二进制后缀。
- `pattern` 同时匹配**相对 root 的路径**和**纯文件名**。

**适合**：把搜索范围从「整个 workspace」收窄到一组文件，再喂给 `grep`。

---

## `grep`

```python
grep(pattern, path='', glob='', regex=False, ignore_case=False,
     max_results=80, max_per_file=10)
```

- 默认按字面量搜；`regex=True` 则按 Python `re` 解析 `pattern`。
- `glob` 字段先过滤文件名（如 `'*.py'`）。
- 单文件命中超过 `max_per_file` 会截断该文件（提示在结尾）。
- 总命中超过 `max_results` 会停止扫描（提示在结尾）。

**适合**：找符号定义、调用点、错误信息字符串字面量。
**不适合**：看上下文——返回的只有命中行本身。要看上下文请 `read_file` 那一段。

---

## ❌ 反模式

| 反模式 | 改用 |
|---|---|
| 一上来就 `read_file` 一个 500 行的文件 | 先 `repo_map` 看签名，定位到大概行号再 `read_file` 切片 |
| `grep` 不带 `glob` / `path` 直接搜整个 workspace | 先 `glob` 收窄文件集 |
| 命中被截断时改大 `max_results` 凑齐 | 收窄 `path` / `glob`，或加严 `pattern` |
| 用 `repo_map` 看实现细节 | `repo_map` 只给签名；要看实现 → `grep` 找位置 → `read_file` |
| `terminal: rg / ag / find` | 直接用 `grep` / `glob`，输出已带上限和路径过滤 |

---

## 探索经验
```
1. 应该避免做..., 否则会导致..., 应该做...
2. ...
...
```
