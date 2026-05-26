# Claude Code 关键工具实现分析

## 1. Task（子代理）— CC 最核心差异点

### 做什么
允许主代理动态启动一个独立的子代理来处理复杂、多步骤任务。子代理拥有独立工具集，执行完成后返回最终报告给主代理。

### 实现细节

**参数设计：**
- `description`：3-5 词的简短任务描述（用于 UI 展示和日志）
- `prompt`：超详细的任务描述，因为子代理是无状态的，必须在单次交互中完成
- `subagent_type`：选择代理类型，不同代理有不同工具权限

**内置代理类型：**
| 类型 | 工具 | 用途 |
|------|------|------|
| `general-purpose` | * (所有工具) | 搜索代码、研究问题、执行多步骤 |
| `statusline-setup` | Read, Edit | 配置状态栏 |
| `output-style-setup` | Read, Write, Edit, Glob, LS, Grep | 创建输出样式 |

**关键设计约束（来自描述）：**
- "Each agent invocation is stateless" — 子代理无状态，必须自包含
- 主代理不会看到子代理的中间过程，只有一个最终返回消息
- 可以并行启动多个子代理（"Launch multiple agents concurrently"）
- 子代理的输出应对用户不可见，需要主代理自己总结

**为什么重要：**
这是 CC 最抽象、最偏"AI-native"的设计。它不是暴露一个 OS 原语，而是提供一个**代理级别的抽象**：用代理调用代理。dong 目前完全没有这个能力，所有工作都由主代理完成。

**实现方案猜测（CC 闭源）：**
1. 服务端维护一个 agent session pool
2. 收到 `Task` 调用时，fork 一个新会话（共享文件系统但独立消息历史）
3. 新会话在受限工具集下运行，完成后将最终消息推回主会话
4. 主代理收到 `tool_result`，内容就是子代理的报告

---

## 2. ExitPlanMode — "先规划，再执行"

### 设计意图
CC 有两种模式：**Plan Mode** 和 **执行 Mode**。在 Plan Mode 中，代理只能做研究（Read/Grep/Glob/WebFetch），不能修改文件。当代理认为规划完成，调用 `ExitPlanMode` 输出计划，等待用户确认后退出 Plan Mode。

### 参数
- `plan`：markdown 格式的计划文本，展示给用户审批

### 实现逻辑
1. 系统在 plan mode 时，Write/Edit/Bash 等写入工具被禁用
2. 代理调用 ExitPlanMode → 框架展示计划给用户
3. 用户确认 → 框架退出 plan mode → 代理恢复所有工具
4. 代理按计划执行

### 对 dong 的借鉴
dong 的 `update_plan` 是运行时的并行计划跟踪，CC 的 ExitPlanMode 是一个**阶段门控**：在动手之前先获得用户对方案的首肯。两者可以互补：先做阶段门控（CC），再在实现时跟踪步骤（dong）。

---

## 3. 文件发现三件套：Grep / Glob / LS

与 dong 不同，CC 把文件发现拆成了三个独立的工具，并有非常细致的使用规则。

### Glob — 快速文件名匹配
```
pattern: "**/*.ts"   → 按修改时间排序返回匹配文件
path: dir             → 可选搜索目录
```
- 只做文件名/路径匹配，不读内容
- 结果按修改时间排序（不是字母序），优先展示最近改动的文件
- 规则：当你知道要搜什么目录时用 Glob；开放式搜索用 Task

### Grep — ripgrep 内容搜索
这是 CC 最精心设计的工具之一，参数完整对应 `rg` 的能力：

```
pattern: "regex"           # 支持完整正则
path: "."                  # 搜索目录
glob: "*.ts"               # 文件过滤（rg --glob）
output_mode: "content" | "files_with_matches" | "count"
-A/-B/-C: N                # 上下文行数
-n: bool                   # 显示行号
-i: bool                   # 大小写不敏感
type: "js"                 # rg --type
head_limit: N              # 输出截断
multiline: bool            # 多行模式（rg -U --multiline-dotall）
```

**设计亮点：**
- `output_mode` 三选一，覆盖三大典型场景：看内容/找文件/统计
- `head_limit` 防范大型结果集
- `type` 比 `glob` 更高效（利用 rg 内置类型映射）
- 描述中明确警告：**禁止在 Bash 里调用 grep 或 rg**

**为什么拆成三个：**
- Glob（文件名）和 Grep（内容）的底层实现完全不同（lstat vs 全文索引）
- LS（目录列表）是最弱的原语，只有当不知道搜什么时才用
- 规则明确指导模型选择：能 Glob 就别 LS，能 Grep 就别在 Bash 里 rg

---

## 4. Bash — 持久化 Shell 与后台运行

### 核心设计
CC 的 Bash 不是一次性命令执行，而是维护一个**持久化 shell session**。

```json
{
  "command": "...",
  "timeout": 600000,         // 最长 10 分钟
  "description": "...",     // 5-10 词简短描述
  "run_in_background": true // 后台运行
}
```

### 后台运行机制
这是 dong 完全不具备的能力：

1. **`run_in_background: true`** → 命令在后台持续运行
2. **`BashOutput`** 工具 → 轮询读取增量输出
   - `bash_id`: shell 标识
   - `filter`: 正则过滤输出行
   - 只返回自上次检查以来的新输出
3. **`KillBash`** 工具 → 终止后台 shell
   - `shell_id`: 通过 `/bashes` 命令查询
4. 后台 shell ID 通过 `/bashes` 斜杠命令查看

**典型场景：**
- 启动开发服务器，然后在后台读取日志
- 运行长时间测试套件，间歇检查进度
- 并行执行多个独立命令

### Git 工作流集成
Bash 描述中包含完整的 git commit 和 PR 流程指导：

- **Commit 流程：** git status + git diff + git log → 分析 → git add + git commit（带 🤖 署名）
- **PR 流程：** git status + git diff + git log → 分析 → gh pr create（带模板）
- 禁止交互式 git（`git rebase -i`、`git add -i`）

### 安全约定
- 必须用双引号包裹含空格的路径
- 避免 `cd`，用绝对路径
- 多条命令用 `;` 或 `&&`，禁止换行
- **禁止**在 Bash 中用 `find`、`grep`、`cat`、`head`、`tail`、`ls` — 必须用对应工具

---

## 5. MultiEdit — 单文件批量精确编辑

### 与 Edit 的区别

| 维度 | Edit | MultiEdit |
|------|------|-----------|
| 操作数 | 1 次 | N 次（数组） |
| 原子性 | 无 | 全部成功或全部回滚 |
| 顺序性 | 无 | 按数组顺序依次应用 |
| 互相影响 | 无 | 前一次结果影响下一次匹配 |

```json
{
  "file_path": "/path/to/file",
  "edits": [
    {"old_string": "foo", "new_string": "bar", "replace_all": true},
    {"old_string": "baz", "new_string": "qux"}
  ]
}
```

### 设计要点
- "Plan your edits carefully to avoid conflicts between sequential operations"
- 因为顺序应用，编辑 1 改变了编辑 2 需要匹配的文本时，编辑 2 会失败
- 如果 `old_string` 为空 → 创建新文件（首 edit 写入全内容）
- 支持 `replace_all` 做全局重命名

### 为什么 dong 没有
dong 通过 LLM 的 "use Edit over Write where possible" 哲学，期望模型自己做精确定位。MultiEdit 实际上是一种 **batch optimization** — 减少模型与文件系统之间的交互轮次。

---

## 6. WebFetch — URL 获取 + AI 二次处理

这是 CC 最不"工具"、最偏 AI 增强的一个工具。

```json
{
  "url": "https://...",
  "prompt": "从这页文档中提取所有 API 接口名称"
}
```

### 执行流程（猜测）
1. 框架 GET URL，HTML → Markdown 转换
2. 用一个**小型快速模型**（description 说 "small, fast model"）对 Markdown 内容执行 prompt
3. 返回小模型的响应，而不是原始内容

**设计动机：**
- 避免把大量网页内容塞进主模型的上下文窗口
- 小模型做信息提取，主模型做推理决策
- 15 分钟自清理缓存，减少重复请求

### WebSearch
更简单的设计 — 传查询词 + 可选域名过滤，框架处理搜索 API 调用，返回搜索结果块。

---

## 7. 其他值得注意的工具

### NotebookEdit
专门为 Jupyter .ipynb 设计：
- 按 `cell_id` 定位单元格
- 三种 `edit_mode`：`replace`、`insert`（在指定 cell 后）、`delete`
- 支持 `cell_type`：`code` 或 `markdown`
- CC 认为这足够重要，值得一个独立工具而非通用 Edit

### TodoWrite
与 dong 的 `update_plan` 功能相同：
- `todos` 数组，每项有 `content`、`status`、`id`
- 状态：`pending`/`in_progress`/`completed`
- CC 版本有极详细的"何时用/何时不用"指南
- 差异：CC 用 `id` 做独立标识（dong 无 id），可以在不改变描述的情况下更新状态

### Read 的多模态能力
- 可以读图片（PNG/JPG 等），作为多模态 LLM 解析
- 可以读 PDF（逐页提取文本+视觉）
- 可以读 `.ipynb`（返回所有 cell + 输出）
- 这是 dong 目前完全缺失的 — dong 的 Read 只是纯文本

---

## 8. 对 dong 的关键启发

**立即可借鉴的（高价值/低复杂度）：**

1. **Bash 后台运行 + BashOutput/KillBash**
   — 解决长时间命令阻塞问题，dong 当前只能等命令结束

2. **Grep 的 `output_mode` + `head_limit`**
   — dong 已有 grep 工具，但缺少输出控制和截断

3. **Glob 独立为工具**
   — 现在 dong 用 grep 做文件发现，语义不清晰

4. **Read 的多模态支持**
   — 图片和 PDF 读取是实用性大提升

**中期可考虑的（高价值/中复杂度）：**

5. **Task 子代理**
   — CC 最核心的差异化能力，但需要架构调整（agent session pool）

6. **MultiEdit**
   — 批量编辑优化，需要原子性编辑引擎支持

7. **WebFetch 的 AI 二次处理**
   — 用小模型过滤网页内容再交给主模型

**可暂缓的：**

8. **ExitPlanMode** — dong 的 plan 是运行时并行的，CC 的阶段门控是另一种范式
9. **NotebookEdit** — 如果用户群不涉及数据科学则优先级低
