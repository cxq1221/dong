# dong 核心架构决策

本文档记录 dong CLI coding agent 的整体架构决策，涵盖 Module 划分、数据流、工具框架、日志、UI、Skills 等方面的设计选择。

---

## 1. Module 划分

### 决策：按职责拆分 9 个 Module，每个文件单一职责

```
dong/
├── __init__.py          # 空 Module，暴露公共 API
├── __main__.py          # python -m dong 入口，薄转发到 cli.main()
├── cli.py               # CLI 主入口 + Agent 循环 + Skill 管理 (~450L)
├── llm.py               # OpenAI Responses / ChatCompletions / Anthropic Messages 请求适配 (~250L)
├── tools.py             # 内置工具实现（read/write/edit/bash/grep/fetch/update_plan）(~450L)
├── tool.py              # 工具框架：注册/校验/执行/结构化结果 (~150L)
├── ui.py                # 终端 UI 适配层（Rich + prompt_toolkit）(~325L)
├── logging_config.py    # 文件日志配置 / 结构化事件 (~200L)
└── log_viewer.py        # dong logs 子命令实现 (~185L)
```

**理由**：
- `tools.py` 已膨胀到 450 行，抽出 `tool.py` 作为框架层，实现工具注册与执行解耦，方便单独测试
- `ui.py` 隔离所有 Rich/prompt_toolkit 依赖，允许未来切换 Textual 或其他渲染引擎
- `logging_config.py` 和 `log_viewer.py` 独立于主循环，通过子命令 `dong logs` 串联
- `cli.py` 承担 Agent 循环和 Skill 加载，暂未进一步拆分以保持迭代速度

### 后果
- 各 Module 通过明确的 import 边界交互：`cli → llm + tools + ui + tool`，`tools → tool`，`cli → logging_config`
- 每个 Module 可独立测试；ui、logging_config、log_viewer 不依赖其他业务 Module

---

## 2. Agent 主循环设计

### 决策：同步 while 循环 + 固定工具调用轮次上限

`cli.py` 中的 `run_loop()` 是核心 Agent 循环：

1. 拼接 system prompt：内置 system + DONG.md（若存在）+ 已加载 Skills
2. while 循环（最多 20 轮工具调用）：
   - 调用 `llm.chat()` 传入 messages 和 tool definitions
   - 模型返回 tool_calls → 逐个执行 → 追加 tool result 消息
   - 模型返回空 tool_calls → 输出文本 → 循环结束
3. 循环结束后将最后一轮 assistant 消息添加到历史

**理由**：
- 同步模型匹配 OpenAI Python SDK 的同步调用模式，简单可靠
- 20 轮上限防止无限工具调用循环导致 token 耗尽
- 工具结果以 `ToolResult.to_message()` 统一封装后追加到消息历史

### 后果
- 非流式：每次请求等待完整响应后才展示工具调用和最终文本
- 不支持中断工具链后继续对话（未来可加）

---

## 3. 工具框架（tool.py）

### 决策：Pydantic 驱动的工具注册 + 结构化结果

```python
class ToolResult(BaseModel):
    success: bool
    summary: str = ""
    detail: str = ""
    error: str = ""

class ToolRegistry:
    def register(self, name, description) -> decorator
    def execute(self, name, raw_args, cwd) -> ToolResult
```

- 每个工具函数第一个参数 `args: SomeInputModel` 必须是 Pydantic BaseModel
- `ToolRegistry` 自动从 type hints 推导 schema，生成 OpenAI function-calling JSON Schema
- `execute()` 统一完成 JSON 解析、参数校验、异常捕获和日志记录

**理由**：
- Pydantic model_json_schema() 直接输出 OpenAI 兼容 schema，避免手写
- 参数校验在入口处完成，工具实现函数拿到的已经是校验后的对象
- `ToolResult` 统一封装成功/失败/摘要，UI 和日志层可一致消费

### 可选 strict mode
- 环境变量 `DONG_TOOL_STRICT=1` 时在 function schema 中加 `strict: true`
- 默认关闭以保持兼容

### 后果
- 新增工具只需定义 Pydantic 入参 + 实现函数 + `@registry.register()` 装饰
- 工具执行的所有错误都会被 `ToolResult.error` 捕获，不会中断 Agent 循环

---

## 4. 内置工具设计（tools.py）

### 核心工具

| 工具 | 功能 | 安全考虑 |
|------|------|----------|
| `read` | 读文件，返回带行号的文本 | 纯读，无副作用 |
| `write` | 覆盖写入文件 | 无条件覆盖，由模型判断 |
| `edit` | 唯一匹配的字符串替换 | 避免误替换；匹配不到时失败 |
| `bash` | 执行 shell 命令，30s 超时 | 危险命令需用户确认；在 cwd 下执行 |
| `grep` | 正则搜索文件内容 | 纯读，无副作用 |
| `fetch` | HTTP GET URL 内容，默认 15s 超时 | 只读取指定 URL，长响应会截断 |
| `update_plan` | 更新当前任务计划 | 只返回计划摘要，不改项目文件 |

### 决策：cwd 作为工具共享上下文

每个工具函数签名为 `(args: InputModel, cwd: str) -> ToolResult`，`cwd` 由 Agent 循环传入当前工作目录。工具所有文件/命令操作均在 `cwd` 下进行。

**理由**：
- 支持 `dir=<path>` 动态切换工作目录
- 避免工具函数自行推断 cwd 导致不一致

### bash 危险命令确认
- `cli.py` 中维护危险命令列表（如 `rm -rf /`）
- 执行前通过 `ui.confirm_dangerous_command()` 请求用户确认，默认拒绝

---

## 5. LLM 层（llm.py）

### 决策：内部接口保持 instructions，provider adapter 转换请求形状

```python
def chat(messages, tools, *, model=None, instructions="") -> ChatCompletionMessageLike
```

- OpenAI 基 URL 由 `DONG_BASE_URL` 环境变量控制，方便接入兼容 API
- OpenAI API Key 从 `DONG_API_KEY` 或 `OPENAI_API_KEY` 读取
- Anthropic 基 URL 由 `DONG_ANTHROPIC_BASE_URL` 控制，默认 `https://api.deepseek.com/anthropic`
- Anthropic API Key 从 `DONG_ANTHROPIC_API_KEY` 读取，未设置时复用 `DONG_API_KEY`
- 默认系统提示词由 `dong/default_agent_define.md` 提供，并通过 `instructions` 传给 LLM
- `DONG_LLM_API=anthropic` 是默认请求形状，使用 Anthropic Messages API；`instructions` 放到顶层 `system`
- DeepSeek Anthropic 兼容接口默认通过 `DONG_ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` 接入
- `DONG_LLM_API=chat` 使用 OpenAI 兼容 ChatCompletions，并把 `instructions` 兼容为首个 `system` message
- `DONG_LLM_API=responses` 使用 OpenAI Responses API；`DONG_LLM_API=auto` 保留为探测模式，Responses 不支持时回退到 ChatCompletions
- 支持 DeepSeek V4 ChatCompletions 特有参数（thinking、reasoning_effort）
- 返回 CLI 主循环可消费的 assistant message 形状（含 content、tool_calls、reasoning_content）

### 后果
- 流式输出：ChatCompletions 和 Anthropic Messages Adapter 都会把文本 delta 交给 UI，同时累积完整 assistant message 供工具循环使用
- 默认请求形状贴近 Codex 的 `instructions + input + tools` Interface
- DeepSeek 等 OpenAI 兼容服务仍可通过 `chat` 模式运行，但不再把 Responses 失败探测作为推荐路径
- Anthropic / DeepSeek Anthropic provider 的工具调用会在 adapter 中转换为 `tool_use` / `tool_result`
- `DONG_RESPONSE_FORMAT=json_object` 只映射 OpenAI Responses / ChatCompletions 请求参数；Anthropic provider 暂依赖 prompt 约束
- reasoning_content 由 UI 层单独渲染为 thinking 面板

---

## 6. UI 层（ui.py）

### 决策：Rich + prompt_toolkit，非全屏

参见 [ADR-0001](./0001-rich-prompt-toolkit-inline-repl.md)。

`TerminalUI` 类封装：
- `show_startup()` — 启动面板（模型、工作目录、工具列表）
- `show_assistant_message()` — Markdown 渲染到 stdout
- `show_reasoning_message()` — thinking 面板渲染到 stderr
- `show_tool_result()` — 工具调用结果状态行到 stderr
- `show_tool_cancelled()` — 工具取消状态行
- `read_prompt()` — 带补全和历史的 REPL 输入
- `confirm_dangerous_command()` — 危险命令确认
- Skill 相关方法：`show_loaded_skill()`、`show_skill_error()` 等

### 补全设计（_SlashAwareCompleter）
- `/` 开头弹出 slash 命令（/skill, /unskill）和已加载 skill 快捷项
- `/skill <name>` 子补全：列出所有可用 skill 名
- `/unskill <name>` 子补全：列出当前已加载 skill 名
- 非 `/` 开头补全普通命令（exit、clear）

### 后果
- 所有终端渲染集中在 ui.py，CLI 循环只调用 TerminalUI 方法
- 未来可添加 `--tui` 模式切换到 Textual 全屏 UI

---

## 7. 日志系统（logging_config.py）

### 决策：结构化单行 JSON 事件日志

- 日志写入 `<workdir>/logs/dong.log`（可通过环境变量调整）
- 格式：`时间 级别 pid=xxx logger名称 event=事件名 fields={JSON}`
- AI 思考、回复、工具调用参数和工具结果默认只记录长度、状态和摘要
- `DONG_LOG_PAYLOADS=1` 时才记录脱敏截断预览
- 单个日志文件默认超过 5MiB 后轮转成 `dong.log.1` 等分片
- 日志目录/文件强制限制在 workdir 内，防止路径穿越

### 可控环境变量
| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DONG_LOG_ENABLED` | 1 | 关闭=0 |
| `DONG_LOG_LEVEL` | INFO | DEBUG/INFO/WARNING/ERROR |
| `DONG_LOG_DIR` | logs | 日志目录 |
| `DONG_LOG_FILE` | dong.log | 日志文件名 |
| `DONG_LOG_MAX_BYTES` | 5242880 | 单文件轮转大小 |
| `DONG_LOG_BACKUP_COUNT` | 5 | 保留的历史分片数量 |
| `DONG_LOG_PAYLOADS` | 0 | AI/工具正文和参数的脱敏预览开关 |

### log_event() 辅助函数
```python
log_event(logger, logging.INFO, "event_name", key=value, ...)
```
- 自动序列化所有 kw 字段为 JSON
- 通过 `dong_event` extra 字段传递事件名

### 后果
- 可通过 `dong logs` 子命令按事件名/级别/logger 过滤
- 排查模型行为时可按 `ai_message_received` / `ai_tool_call_requested` / `ai_tool_result_received` 检索长度、状态和可选脱敏预览

---

## 8. 日志查看器（log_viewer.py）

### 决策：本地解析 + 过滤 + 可选 --follow 模式

`dong logs` 子命令实现：
- 解析 dong 日志格式为 `ParsedLogLine` 数据结构
- 支持过滤：`--level`、`--event`、`--logger`、`--contains`
- `--limit N` 控制返回行数
- `--json` 输出 JSON 行
- `--follow` 持续轮询新日志（类似 `tail -f`）

### 后果
- 无需外部依赖（如 jq），纯 Python 实现
- 日志解析正则只匹配 dong 特定格式，非该格式行作为原始字符串保留

---

## 9. Skills 系统

### 决策：本地 + Codex 全局双源，同名时本地优先

加载顺序：
1. `.dong/skills/<name>.md`（本地）
2. `${CODEX_HOME:-~/.codex}/skills/<name>/SKILL.md`（Codex 全局）

加载时原样注入 SKILL.md 内容到 system prompt，不解析 frontmatter。

### CLI 集成
- `/skill` — 列出所有可用 skills
- `/skill <name>` — 加载 skill 到当前会话
- `/<name> <prompt>` — 加载对应 skill 并用 prompt 执行一轮对话
- `/unskill <name>` — 卸载 skill
- `--skill <name>` — CLI 参数，单次模式加载指定 skill

### 后果
- Skill 本质是追加到 system prompt 的指令文本
- 同名冲突时本地 `.dong/skills/` 优先，允许项目级覆盖

---

## 10. 对话历史与上下文管理

### 决策：在 cli.py 中以 messages 列表管理

- REPL 模式：跨轮对话保持 messages 列表（单次模式不保持）
- `clear` 命令重置 messages 为初始 system prompt
- `--keep` 标志允许单次模式保留上下文
- 工具调用结果消息追加到 messages，模型可看到完整工具链

### json_output 模式
- `DONG_RESPONSE_FORMAT=json_object` 时，在 request 中设置 `response_format={"type": "json_object"}`
- 用户 prompt 中需包含 "json" 字样以满足 OpenAI API 要求

### 后果
- messages 列表无持久化，进程退出后历史丢失
- 无 token 计数和上下文窗口管理

---

## 11. CLI 入口设计（cli.py）

### 决策：单一入口函数 `main()` 处理所有子命令

```
dong <prompt>             # 单次模式
dong --skill <name> ...   # 单次加载 skill
dong                      # REPL 交互模式
dong logs [选项]          # 日志查看
```

### 参数解析
- 使用 `argparse`（非第三方库）
- 支持 `--model`、`--skill`、`--keep`、`--yes`（跳过危险确认）

### 后果
- 无插件系统，所有子命令硬编码在 cli.py 的 `main()` 中解析

---

## 12. 依赖策略

### 运行时依赖
- `anthropic` — Anthropic Messages API 调用
- `openai` — LLM API 调用
- `playwright` — fetch 工具使用（HTTP GET，通过 playwright 的 sync_api）
- `prompt-toolkit` — REPL 输入补全与历史
- `pydantic` — 工具参数校验和 schema 生成
- `rich` — 终端 Markdown 渲染和面板

### 开发依赖
- `pytest` — 测试
- `ruff` — Lint / 格式化

### 决策：最小化依赖，只用成熟库

- 不用 LangChain/AutoGen 等重型 Agent 框架
- 不用 Typer/Click 等 CLI 框架（argparse 足够）
- 不用 Textual 等全屏 TUI（Phase 1 保持 inline REPL）

### 后果
- 总依赖数 ~5 个运行时库，安装体积小
- 所有核心逻辑（Agent 循环、工具执行、补全）均为自研，可控性强

---

## 10. 上下文压缩与文件化留痕

### 决策：预算感知压缩 + 最近消息原样保留

`dong/context_compaction.py` 的上下文压缩 Module 不再只依赖固定消息条数。它会同时检查：

- 最近消息条数预算：默认保留 20 条 working 消息
- 模型感知 token 预算：已知模型按上下文窗口的 `DONG_CONTEXT_AUTO_COMPACT_PERCENT`（默认 80%）触发
- 手动 token 阈值：显式设置 `DONG_CONTEXT_MAX_TOKENS` 时直接使用该阈值；未知模型未配置时使用保守 token 阈值

当任一预算超限时，dong 会把旧消息压缩成一条合成 `system` 摘要，并保留最近消息原文继续对话。摘要生成是本地确定性规则，不额外调用 LLM。请求体 token 估算由 `dong/token_counter.py` 统一负责：DeepSeek 优先使用官方 tokenizer，Claude 优先使用 Anthropic `count_tokens`，其他非官方路径退到 `tiktoken` 近似并写 approximate 日志；估算覆盖 instructions、messages、tools 和输出预算。

### 文件存储

每次发生压缩时，完整摘要写入项目内：

```text
.dong/context/compact-YYYYMMDDTHHMMSSZ.md
```

运行时上下文只引用这份摘要文件路径和短摘要内容。这样可以满足“上下文优化有留痕、但不引入数据库”的约束。

### Tool 调用边界

压缩边界会回退到安全点，避免保留孤立 `tool` 消息。也就是说，如果最近窗口从工具结果开始，dong 会把对应的 assistant `tool_call` 一起保留下来，避免 OpenAI 兼容接口因为 tool/result 配对断裂返回 400。

### 参考来源

该策略参考了 `../Projects/claw-code/rust/crates/runtime/src/compact.rs` 与 runtime 自动压缩流程的核心做法：旧上下文摘要化、最近消息原样保留、tool-use/tool-result 边界保护、模型窗口预算、重复压缩时避免摘要无限嵌套。dong 采用更小的实现：轻量 token 估算、摘要文件落盘，不做跨进程会话恢复。
