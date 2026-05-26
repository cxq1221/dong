# Claude Code 系统提示词与 dong 对比分析

## 概览

| 维度 | dong (default_agent_define.md) | CC 1.x (cc-prompt.txt) | CC 2.0 (cc20-prompt.txt) |
|------|------|------|------|
| 行数 | 268 | 191 | 1150 |
| 模型 | 通用 | Sonnet 4 | Sonnet 4.5 |
| 工具数 | 7 | 13 | 17+ |
| 语言 | 中英混合，默认中文输出 | 纯英文 | 纯英文 |
| 风格 | 亲切、协作 | 专业、极简 | 专业、极简、严谨 |

---

## 结构对比

### dong 独有的章节
- **前置说明消息**：要求在工具调用前发送简短的"即将做什么"消息，语气轻松友好。这是 dong 最具特色的设计之一，CC 系列完全没有此概念。
- **雄心与精确**：区分全新项目 vs 存量代码库的处理方式——前者鼓励创造力，后者要求外科手术式精确。
- **项目规则规范**：详细介绍 DONG.md / .dong/DONG.md 的加载机制和优先级。

### CC 2.0 独有的章节
- **Ethical guidelines / Professional objectivity**：强调客观、超然、不讨好用户、避免主观判断。
- **Plan mode vs Act mode**：通过 `ExitPlanMode` 工具实现纯规划模式，用户确认后才执行。
- **Git/PR 工作流**：详细的 commit message 格式（仅允许 `feat/fix/chore/docs`）、rebase、PR 流程（`gh pr create`）。
- **App 安装与测试**：支持测试 artifact 工具（React/Vue/Svelte 等前端应用）、截图对比等。
- **Task subagent**：通过 `Task` 工具启动子代理处理复杂多步骤任务。
- **斜杠命令框架**：`SlashCommand` 工具支持自定义命令扩展。

---

## 工具差异

### dong 工具集（7 个）
`read` `write` `edit` `bash` `grep` `fetch` `update_plan`

### CC 1.x 额外拥有的工具
`Glob` `LS` `Task` `TodoWrite` `WebSearch` `MultiEdit` `NotebookEdit` `BashOutput` `KillBash`

### CC 2.0 进一步新增
`ExitPlanMode` `SlashCommand`

### 关键差异点
1. **文件搜索**：dong 依赖 shell `rg`，CC 有原生 `Glob`（通配符文件匹配）和 `LS`（目录列表）工具，模型侧更可控。
2. **批量编辑**：CC 有 `MultiEdit`，可一次调用修改多处；dong 只能逐次 `edit`。
3. **子代理**：CC 有 `Task` 工具实现子代理委托，dong 无此能力。
4. **计划工具**：dong 用 `update_plan`（暴露给用户），CC 用 `TodoWrite`（内部追踪）。CC 2.0 额外增加 `ExitPlanMode` 实现前置规划。
5. **Web 能力**：CC 有 `WebSearch`（搜索），dong 只有 `fetch`（直接 GET）。
6. **终端控制**：CC 有 `BashOutput` `KillBash` 进行后台终端管理，dong 只有同步 `bash`。

---

## 风格与语气对比

### dong
- 默认中文输出，中文思维贯穿始终
- 详细的结构化输出指南（**区块标题**、项目符号规范、等宽文本规则、文件引用格式）
- 鼓励"队友式"的协作语气，允许轻量的个性化表达
- 有大量详细的 do/don't 清单

### CC 系列
- 纯英文，追求极致简洁
- "Be as brief as possible. One word answers are best."
- CC 2.0 增加"专业客观性"："Do not hype, cheerlead, or pander."
- 强调"冷淡直接"：避免热情、赞美、讨好式语言
- 对输出长度有硬约束："Respond in 1-3 sentences for simple confirmations"

---

## 核心哲学差异

### 信息透明度
- **dong** 要求详尽的进度分享（前置说明消息、进度更新），让用户始终了解代理在做什么
- **CC 2.0** 强调"Don't use echo for communicating"，要求工具调用本身传递信息，"act fast — don't talk"

### 用户关系
- **dong** 定位为"协作搭档"，友好、温暖、有人情味
- **CC** 定位为高效工具，客观、专业、有边界感

### 规则加载
- **dong** 使用 `DONG.md` / `.dong/DONG.md` 约定
- **CC** 使用 `CLAUDE.md` / `.claude/CLAUDE.md` 约定（目的相同，命名不同）

### 计划执行
- **dong** 的计划是运行时与用户可见的进度追踪（`update_plan`）
- **CC 2.0** 增加了前置规划模式——先输出计划不执行，用户确认后再统一执行

---

## dong 值得借鉴的 CC 设计

1. **`ExitPlanMode` 规划模式**：复杂任务可先规划后执行，减少不可逆错误，用户体验更好
2. **`Task` 子代理**：处理长任务时可将子任务委托给独立代理，减少上下文膨胀
3. **`Glob` / `LS` 工具**：原生文件发现工具比 shell `ls`/`find` 更可靠、跨平台且无需处理 shell 转义
4. **`MultiEdit` 批量编辑**：减少多次 `edit` 调用的延迟和 token 开销
5. **`BashOutput` / `KillBash`**：支持长时间后台任务（如 dev server）的输出获取与终止
6. **`WebSearch`**：补全只有 `fetch` 的短板，支持搜索式信息获取
7. **Git/PR 工作流规范**：commit 类型锁定（`feat/fix/chore/docs`）能减少混乱；dong 可以加类似选项
8. **专业客观性**：CC 2.0 的"不过度热情"设计值得参考，避免 AI 显得谄媚或过度讨好

## dong 独特且应保留的优势

1. **默认中文输出**：对中文用户友好，也是差异化竞争力
2. **前置说明消息**：让用户知道代理在做什么，减少等待焦虑；这种"过程可视化"是 dong 特色
3. **雄心与精确**：明确区分全新项目 vs 存量项目的策略，比 CC 的通用指令更可操作
4. **详细格式规范**：dong 的输出结构指南比 CC 更系统、更适合 CLI 渲染
5. **紧凑编辑模型**：dong 的 `edit`（唯一匹配替换）比 CC 的 `Edit` 更可靠，不容易误替换

---

## 总结

dong 的系统提示词约为 CC 1.x 的 1.4 倍、CC 2.0 的 23%，在简洁性上保持了良好平衡。核心差异在于：dong 重视"过程透明度"和"中文体验"，而 CC 重视"极致效率"和"专业边界"。两种取向都是有意为之的设计选择，但 CC 2.0 在工具丰富度（子代理、规划模式、批量编辑、后台终端）上明显领先，这些是 dong 可以考虑借鉴的方向。
