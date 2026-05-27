# 🐉 dong —— 极简终端编码代理

**dong** 是一个开源的终端编码代理（CLI Coding Agent），由 LS 主导开发。你只需要用自然语言描述需求，它就能自动读取文件、编写代码、搜索项目、执行命令，直到任务完成。

**设计哲学**：不到 4000 行 Python，只做一件事——把你说的话变成可执行的编码动作。不做代码库索引、不做记忆系统、不做项目地图。

---

## 项目架构

```
dong/
├── __init__.py          # 包入口（1 行）
├── __main__.py          # python -m dong 入口（5 行）
├── cli.py               # 主入口层：CLI 解析、REPL 对话循环、agent 调度、skill 管理、上下文压缩（1382 行）
├── llm.py               # LLM 抽象层：多 provider 适配、流式调用、Messages/Responses/Chat 格式转换（830 行）
├── tools.py             # 内置工具集：read / write / edit / bash / grep / fetch / update_plan（349 行）
├── tool.py              # 工具注册框架：装饰器注册、Pydantic 参数校验、安全路径验证（149 行）
├── ui.py                # 终端 UI 适配层：Rich 渲染、prompt_toolkit 输入、补全、确认面板（491 行）
├── mcp.py               # MCP stdio 客户端：发现外部工具、转发调用、管理 server 生命周期（438 行）
├── logging_config.py    # 结构化日志：JSON 事件日志、payload 掩码、模块级 logger 工厂（202 行）
└── log_viewer.py        # 日志查看器：dong logs 子命令，按级别/事件/关键词过滤（184 行）

tests/
├── e2e/
│   ├── test_cli_e2e.py  # CLI 单次 prompt/run_loop 端到端自动化回归测试
│   ├── test_cli_tty_e2e.py  # TTY 模式下 CLI 端到端测试
│   ├── test_cli_operational_e2e.py  # logs/MCP 子命令与多工具链端到端测试
│   └── test_session_business_e2e.py  # session 业务端到端测试
├── test_cli_repl_commands.py  # 交互模式命令测试（exit/clear/dir）
├── test_cli_skills.py   # Skill 发现、加载、别名解析测试
├── test_llm.py           # LLM 适配层单元测试（含 prompt 构建、流式、JSON 模式）
├── test_tools_*.py       # 各内置工具单元测试（read/write/edit/bash/grep/plan）
├── test_tool_schema.py   # 工具注册与 schema 生成测试
├── test_ui.py            # UI 渲染行为兼容性测试
├── test_logging.py       # 日志系统测试
├── test_log_viewer.py    # 日志查看器测试
├── test_mcp.py           # MCP 客户端测试
└── mcp_helpers.py        # MCP 测试辅助工具
```

### 核心模块职责

**`cli.py`** —— 程序入口与 agent 调度中心
- argparse 命令行解析（`dong` / `dong "prompt"` / `dong logs` / `dong mcp`）
- `REPL` 类：交互对话循环，管理对话历史、skill 栈、上下文压缩
- `AgentLoop` 类：单次 prompt 的完整执行流程（组装 system prompt → 调用 LLM → 执行工具 → 回传结果 → 循环）
- 上下文压缩：当消息长度超过阈值时，本地自动摘要旧消息（不调用外部 LLM）
- Skill 发现：扫描 `.dong/skills/`（本地优先）+ `~/.codex/skills/`（Codex 兼容）

**`llm.py`** —— LLM 多 provider 抽象
- 统一接口 `instructions + input + tools`，由 provider adapter 转换为对应 API 格式
- Provider 支持：
  - `anthropic`（默认）：走 Anthropic Messages API，instructions → 顶层 `system`，对话 → `messages`，工具调用 → `tool_use` / `tool_result`
  - `responses`：OpenAI Responses API，instructions → `instructions` 字段，对话 → `input`
  - `chat`：OpenAI Chat Completions API，instructions → 首个 `system` message
  - `auto`：兼容探测，先试 Responses，不支持则回退 Chat Completions
- 流式调用支持，逐字符输出并通过 `on_content` 回调实时推送到 UI

**`tools.py`** —— 七个内置工具
| 工具 | 功能 | 安全约束 |
|------|------|----------|
| `read` | 读取文件内容（含行号） | `_validate_path()` 防目录遍历 |
| `write` | 创建或覆盖文件 | `_validate_path()` 防目录遍历 |
| `edit` | 精确查找替换（唯一匹配） | `_validate_path()` 防目录遍历 |
| `bash` | 执行 shell 命令（含超时） | 禁止 `rm`/`sudo`/`mv` 等危险操作 |
| `grep` | 正则搜索文件内容 | 仅读取，无写入风险 |
| `fetch` | GET 指定 URL | 超时 15s，仅 GET |
| `update_plan` | 维护任务计划（步骤 + 状态） | 纯内存操作 |

**`tool.py`** —— 工具注册框架
- `@registry.register()` 装饰器模式注册工具
- Pydantic v2 自动生成 JSON Schema，通过类型注解推断参数类型
- `_validate_path()` 统一路径安全检查（拒绝 `..`、符号链接逃逸、绝对路径越界）

**`ui.py`** —— 终端 UI 适配层
- Rich 渲染：Markdown 消息、工具结果行、启动通知、确认面板
- prompt_toolkit 输入：多行编辑、历史记录、命令/路径自动补全、粘贴保护
- Interrupt 兼容：`Ctrl+C` 中断模型调用但不退出程序

**`mcp.py`** —— MCP stdio 客户端
- 读取 `.dong/mcp.json` 项目配置，管理 stdio transport 的 MCP server
- server 生命周期：启动 → `tools/list` 发现工具 → 运行时转发 `tools/call` → 退出时关闭
- 工具命名：`mcp__<server>__<tool>`，注入模型后由模型主动调用

**`logging_config.py`** —— 结构化日志
- 一行一条 JSON 事件，写入 `logs/dong.log`
- 事件类型：`ai_message_received` / `ai_tool_call_requested` / `ai_tool_result_received` / `tool_executed` 等
- 默认只记录 AI/工具交互的长度、状态和摘要；`DONG_LOG_PAYLOADS=1` 时才记录脱敏截断预览
- 超过 5MiB 后自动轮转分片

**`log_viewer.py`** —— `dong logs` 子命令
- 支持 `--limit` / `--level` / `--event` / `--logger` / `--follow` 过滤
- 支持 `--text` 全文关键词搜索

---

## 工作流程

一次典型的对话请求：

```
用户输入 "在 fib.py 里写一个 fibonacci 函数"
  ↓
CLI 组装 AgentPrompt:
  ├── instructions: 系统提示词（角色 + 规则 + skill 内容）
  ├── context_messages: 对话历史
  └── tools: 七个内置工具（+ MCP 工具）
  ↓
LLM 返回: {tool_use: write, params: {filepath: "fib.py", content: "..."}}
  ↓
CLI 执行 write 工具 → _validate_path() → 写入文件
  ↓
工具结果回传 LLM
  ↓
LLM 返回最终文本: "已创建 fib.py..."
  ↓
UI 渲染给用户
```

---

## 安装

```bash
git clone <仓库地址>
cd dong
uv sync
```

依赖（`pyproject.toml`）：
- `anthropic` ≥ 0.104.1 —— Anthropic Messages API
- `openai` ≥ 2.38.0 —— OpenAI Responses / Chat Completions API
- `prompt-toolkit` ≥ 3.0.52 —— 终端输入
- `pydantic` ≥ 2.13.4 —— 工具参数校验与 Schema 生成
- `rich` ≥ 15.0.0 —— 终端渲染

---

## 配置

在项目根目录创建 `.env`：

```bash
DONG_API_KEY=sk-你的API密钥
```

### 环境变量参考

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `DONG_API_KEY` | API 密钥（所有 provider 通用） | 必填 |
| `DONG_MODEL` | 模型名称 | `deepseek-v4-pro` |
| `DONG_LLM_API` | 请求接口类型 | `anthropic` |
| `DONG_BASE_URL` | OpenAI 兼容 API 地址 | `https://api.openai.com/v1` |
| `DONG_ANTHROPIC_BASE_URL` | Anthropic API 地址 | `https://api.deepseek.com/anthropic` |
| `DONG_ANTHROPIC_API_KEY` | Anthropic 专用 Key | 未设置时复用 `DONG_API_KEY` |
| `DONG_MAX_TOKENS` | 最大输出 token | `4096` |
| `DONG_CONTEXT_AUTO_COMPACT_PERCENT` | 模型窗口自动压缩百分比 | `80` |
| `DONG_CONTEXT_MAX_TOKENS` | 覆盖模型感知压缩 token 阈值 | 不设置 |
| `DONG_DEEPSEEK_TOKENIZER_DIR` | DeepSeek 官方 tokenizer 本地目录 | `dong/tokenizers/deepseek_v3`（存在时） |
| `DONG_THINKING` | DeepSeek thinking 模式 | `disabled` |
| `DONG_REASONING_EFFORT` | 思考深度 | 不设置 |
| `DONG_RESPONSE_FORMAT` | 响应格式 | `text` |
| `DONG_TOOL_STRICT` | 工具严格模式 | `0`（关闭） |
| `DONG_LOG_MAX_BYTES` | 单个日志文件轮转大小 | `5242880`（5MiB） |
| `DONG_LOG_BACKUP_COUNT` | 保留的日志分片数量 | `5` |
| `DONG_LOG_PAYLOADS` | AI/工具正文和参数的脱敏预览开关 | 不设置（预览隐藏） |

### Provider 配置示例

**DeepSeek Anthropic 兼容接口（默认）**：
```bash
DONG_LLM_API=anthropic
DONG_ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
DONG_ANTHROPIC_API_KEY=sk-你的DeepSeek密钥
DONG_MODEL=deepseek-v4-pro
```

**Claude 官方接口**：
```bash
DONG_LLM_API=anthropic
DONG_ANTHROPIC_BASE_URL=https://api.anthropic.com
DONG_ANTHROPIC_API_KEY=sk-ant-你的Claude密钥
DONG_MODEL=claude-sonnet-4-20250514
```

**OpenAI 兼容接口**：
```bash
DONG_LLM_API=chat
DONG_BASE_URL=https://api.openai.com/v1
DONG_API_KEY=sk-你的OpenAI密钥
DONG_MODEL=gpt-4o
```

---

## 使用方式

### 单次模式

```bash
dong "你的需求"
```

说完这句，dong 立即开始执行，直到任务完成。

### 交互模式

```bash
dong
```

进入 REPL 对话界面。支持以下命令：

| 输入 | 效果 |
|------|------|
| `exit` / `quit` | 退出程序 |
| `clear` | 清空对话上下文 |
| `dir=/path/to/project` | 切换到另一个项目目录 |
| `/skill <name>` | 加载指定 skill |
| `/unskill <name>` | 卸载指定 skill |
| `/<skill-name>` | 快捷方式：加载 skill 并以后续内容为 prompt |

输入 `/` 时自动弹出命令和 skill 补全菜单。

---

## 内置工具

| 工具 | 用途 |
|------|------|
| `read` | 读取文件内容（含行号） |
| `write` | 创建或覆盖文件 |
| `edit` | 精准唯一匹配查找替换 |
| `bash` | 执行 shell 命令（带超时和安全限制） |
| `grep` | 正则搜索项目文件内容 |
| `fetch` | GET 获取 URL 内容 |
| `update_plan` | 创建/更新多步骤任务计划 |

所有文件操作均通过 `_validate_path()` 做路径安全校验，防止目录遍历攻击。`bash` 工具禁止 `rm`、`sudo`、`mv` 等危险操作。

---

## MCP（外部工具扩展）

通过项目级 `.dong/mcp.json` 配置 stdio MCP server：

```json
{
  "servers": {
    "demo": {
      "command": "python",
      "args": ["server.py"],
      "env": {},
      "enabled": true
    }
  }
}
```

MCP server 默认不会自动启动。通过 `--mcp` 显式启用：

```bash
dong --mcp "使用已配置的 MCP 工具完成任务"
```

MCP 工具以 `mcp__<server>__<tool>` 命名注入模型。查看配置：

```bash
dong mcp list -d .          # 列出配置的 server
dong mcp list -d . --tools  # 连接并列出发现的工具
```

---

## Skills（技能包）

Skill 是一份 Markdown 文件，向 dong 注入领域知识、约束和规范。

**目录结构**：
```
.dong/skills/
├── python-test.md         # Python 测试规范
├── code-review.md         # 代码审查规范
└── diagnose/
    └── SKILL.md            # 目录形式的 skill
```

**使用 skill**：
```bash
dong --skill python-test "给 utils.py 写测试"     # 单次模式
/skill python-test                                 # 交互模式
/<skill-name> 后续内容                              # 交互模式快捷方式
```

- 同名 skill 本地优先（`.dong/skills/` > `~/.codex/skills/`）
- 兼容 Codex 全局 skill 目录
- 解析 `name` / `description` frontmatter 用于发现和别名

---

## 日志

dong 运行时写入结构化日志到 `logs/dong.log`。

```bash
dong logs --limit 50              # 最近 50 条
dong logs --level WARNING         # 只看错误
dong logs --event tool_executed   # 只看工具调用
dong logs --follow --event tool_executed  # 实时监控
```

AI 的回复长度、思考长度、工具参数长度和工具结果长度会写入 `ai_message_received`、`ai_tool_call_requested`、`ai_tool_result_received` 事件。需要排查正文时显式设置 `DONG_LOG_PAYLOADS=1`，日志只写脱敏截断预览。日志文件默认超过 5MiB 后轮转为 `dong.log.1` 等分片。

---

## 开发

```bash
uv sync --group dev     # 安装开发依赖
uv run pytest           # 运行全部测试
uv run ruff check --fix # 代码风格检查
```

### 测试分层

| 层级 | 说明 | 运行方式 |
|------|------|----------|
| 单元测试 | 工具、LLM 适配、日志等模块独立测试 | `pytest tests/test_tools_*.py tests/test_llm.py` |
| CLI/REPL 自动化回归 | CLI 命令、REPL 行为、skill 管理 | `pytest tests/test_cli_*.py` |
| 端到端自动化回归 | 从 CLI/TTY/session 用户入口贯穿 agent loop、工具和持久化边界 | `pytest tests/e2e` |
| 真实 LLM/API 端到端验证 | 用真实 API 配置完成一轮完整对话 | 手动执行：`uv run dong "简单任务"` |

### 编码约定

- Python 3.11+，f-string 优先，完整类型标注
- Import 顺序：stdlib → third-party → local
- 工具通过 `@registry.register()` 装饰器注册
- 所有文件 I/O 必须经过 `_validate_path()`
- 模块级测试尽量与模块一一对应：`dong/xxx.py` → `tests/test_xxx.py`
- 端到端自动化测试统一放在 `tests/e2e/`，避免和模块级回归混在一起
- 所有代码需添加中文注释

---

## 对比

| | dong | Claude Code | Codex | Aider |
|------|------|-------------|-------|-------|
| 代码规模 | ~4000 行 | 大型项目 | 大型项目 | 大型项目 |
| 核心理念 | 极简 · 中文友好 | 功能全面 | Claude 原生 | Git 地图式 |
| 模型支持 | Anthropic / OpenAI / 兼容接口 | Claude 系列 | Claude 系列 | 广泛 |
| Skill 系统 | ✅ Markdown 文件 | ✅ | ✅ | ❌ |
| MCP 支持 | ✅ stdio transport | ✅ | ✅ | ❌ |
| 结构化日志 | ✅ JSON 事件 | ✅ | ✅ | ✅ |
| 上下文压缩 | ✅ 本地算法 | ✅ | ✅ | ✅ |

---

## 许可

MIT
