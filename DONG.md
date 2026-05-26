# dong 项目架构与开发指南

## 概述

dong 是一个基于终端的 AI 编码代理（CLI），由 LS 主导的开源项目。它通过 LLM + 工具调用循环，让 AI 在用户的项目环境中执行编码任务。

## 项目结构

```
dong/
├── __init__.py           # 包入口，含版本号
├── __main__.py           # `python -m dong` 入口 → 委托给 cli.main()
├── cli.py                # 核心：CLI 入口、agent 循环、skill 管理、REPL、上下文裁剪
├── llm.py                # LLM API 抽象层（OpenAI Chat/Responses、Anthropic Messages）
├── ui.py                 # 终端 UI 层（rich 渲染 + prompt_toolkit 交互）
├── tool.py               # 工具框架：注册、校验、ToolResult
├── tools.py              # 内置工具实现：bash, read, write, edit, grep, fetch, update_plan
├── logging_config.py     # 日志配置（按日轮转）
├── log_viewer.py         # `dong logs` 子命令
├── mcp.py                # MCP 协议支持
└── default_agent_define.md  # 默认系统提示词
```

## 核心流程

### 1. 启动路径

- `dong <prompt>` → `cli.main()` → 单次对话模式
- `dong`（无参数）→ `cli.main()` → 交互式 REPL
- `dong logs` → `log_viewer.main()` → 日志查看子命令
- `dong mcp list` → `cli.main()` → MCP 子命令

### 2. Agent 循环（`cli.run_loop`）

1. `build_agent_prompt()` 组装系统提示词：
   - 加载 `default_agent_define.md`（内置指令）
   - 加载 `DONG.md` / `.dong/DONG.md`（项目规则）
   - 加载已启用的 skill 文件
2. 用户消息追加到 `working` 列表
3. 调用 LLM（`llm.chat()`）获取响应
4. LLM 返回文本或 `tool_calls`
5. 工具调用通过 `tools.execute()` 执行
6. 工具结果追加到 `working`，回到步骤 3
7. 当 LLM 返回纯文本（无 tool_calls）时结束循环

### 3. 上下文管理（`cli.trim_context`）

- 阈值：`DEFAULT_CONTEXT_MAX_MESSAGES=14` 条消息，或 `DEFAULT_CONTEXT_MAX_CHARS=24000` 字符
- 压缩旧消息为摘要，写入 `.dong/context/compact-*.md`
- 保留 tool_call/tool_result 配对不被拆散
- 仅用本地计算，不调用额外 LLM

## 模块职责

### `dong/llm.py` — LLM 抽象层

- 支持三种 API 模式：`anthropic`、`chat`、`responses`、`auto`
- 默认使用 Anthropic Messages API（DeepSeek 端点）
- 负责将 dong 内部消息格式转换为各 provider 格式
- 支持流式输出和 thinking/reasoning 内容
- 重试机制：通过 `DONG_MAX_RETRIES` 环境变量配置

### `dong/tool.py` — 工具框架

- `Tool` 类：封装工具名、描述、Pydantic schema、执行函数
- `@registry.register()` 装饰器注册工具
- `ToolResult` 数据类：统一返回 `success` / `summary` / `detail` / `error`
- 环境变量 `DONG_TOOL_STRICT=1` 启用 DeepSeek strict tool calling

### `dong/tools.py` — 内置工具

| 工具 | 功能 | 安全措施 |
|------|------|----------|
| `bash` | 执行 shell 命令 | 危险命令确认、超时控制、路径安全 |
| `read` | 读取文件（带行号） | 路径遍历防护 |
| `write` | 覆盖写入文件 | 路径遍历防护 |
| `edit` | 唯一匹配查找替换 | 路径遍历防护、歧义检测 |
| `grep` | 正则搜索文本 | 无额外风险 |
| `fetch` | HTTP GET 请求 | 超时控制 |
| `update_plan` | 更新任务计划 | 无额外风险 |

### `dong/ui.py` — 终端 UI

- Rich 库用于 Markdown 渲染和样式化输出
- prompt_toolkit 用于交互式 REPL（支持自动补全）
- 流式输出助手和 working 状态显示（含耗时）
- 危险命令确认提示

### `dong/cli.py` — CLI 主控

- 参数解析（argparse）
- REPL 两种模式：
  - **交互式**（TTY）：输入队列 + worker 线程，允许在 AI 工作时排队输入
  - **非交互式**（pipe）：同步 REPL 循环
- Skill 管理系统：加载 `.dong/skills/` 或 `~/.codex/skills/` 下的 Markdown 文件

## 测试

### 测试结构

```
tests/
├── conftest.py              # Pytest fixtures（创建时自动加载）
├── test_tool_schema.py      # 工具 schema strict 模式开关
├── test_tools_read.py       # read 工具测试
├── test_tools_write.py      # write 工具测试
├── test_tools_edit.py       # edit 工具测试
├── test_tools_bash.py       # bash 工具测试
├── test_tools_grep.py       # grep 工具测试
├── test_tools_fetch.py      # fetch 工具测试
├── test_tools_plan.py       # update_plan 工具测试
├── test_llm.py              # LLM 模块测试（mock API 响应）
├── test_ui.py               # UI 模块测试
├── test_cli_e2e.py          # CLI 端到端测试（实际 LLM 调用）
├── test_cli_skills.py       # Skill 功能测试
├── test_cli_repl_commands.py# REPL 命令测试
├── test_cli_tty_e2e.py      # TTY 模式端到端测试
├── test_mcp.py              # MCP 模块测试
├── test_mcp_helpers.py      # MCP 测试辅助
├── test_log_viewer.py       # 日志查看器测试
├── test_logging.py          # 日志配置测试
└── test_logging_config.py   # 日志配置单元测试
```

### 测试模式与约定

- **工具测试**：使用 `tmp_path` fixture，直接调用 `tools.execute()`，检查 `ToolResult.success` / `.summary` / `.detail` / `.error`
- **路径安全测试**：覆盖 `../` 逃逸、绝对路径、符号链接遍历等场景
- **LLM 测试**：mock API 响应，验证请求/响应转换逻辑
- **E2E 测试**：需要 `DONG_E2E_MODEL` 环境变量，默认跳过
- **UI 测试**：mock console 验证 rich/progress 输出

### 运行测试

```bash
# 运行所有测试（跳过 E2E）
pytest

# 运行特定模块
pytest tests/test_tools_read.py

# 包含 E2E
DONG_E2E_MODEL=deepseek-chat pytest tests/test_cli_e2e.py

# 代码格式化
ruff format dong/ tests/
ruff check dong/ tests/
```

## 编码约定

- Python 3.9+，使用 `from __future__ import annotations`
- 类型标注使用 Pydantic BaseModel 或标准 typing
- 工具函数签名：`(args: InputModel, cwd: str) -> ToolResult | None`
- 错误处理：工具返回 `ToolResult(success=False, error=...)` 而非抛出异常
- 路径安全：所有文件操作必须通过 `_validate_path()` 校验
- 日志：使用 `logging.getLogger(__name__)`，配置通过 `logging_config.py`
- 格式化：ruff
