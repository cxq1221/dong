# 🐉 dong

极简 CLI coding agent。核心就三个文件 + 一个主循环。

## 快速开始

```bash
# 设置 API Key
export DONG_API_KEY=sk-xxx
# 可选：切换模型
export DONG_MODEL=gpt-4o
# 可选：DeepSeek V4 thinking/tool 高级参数
export DONG_THINKING=enabled          # enabled / disabled
export DONG_REASONING_EFFORT=high     # high / max
export DONG_TOOL_STRICT=0             # 1 时给 function tools 加 strict=true
export DONG_RESPONSE_FORMAT=text      # text / json_object

# 单次模式
dong "add a fibonacci function to fib.py"

# 单次模式加载 skill
dong --skill python-test "add tests for fib.py"

# 交互模式（不用引号）
dong
```

## 内置工具

| 工具 | 作用 |
|------|------|
| `read` | 读文件（带行号） |
| `write` | 写文件（覆盖） |
| `edit` | 查找替换 |
| `bash` | 执行 shell 命令 |
| `grep` | 搜索文本 |

## 交互模式命令

- `exit` / `quit` — 退出
- `clear` — 清空对话上下文
- `dir=/some/path` — 切换工作目录
- `/skill` / `/skills` — 列出本地和 Codex 全局 skills
- `/skill <name>` — 加载 skill（本地优先，其次 Codex 全局）
- `/<name> <prompt>` — 加载对应 skill 并用后面的 prompt 执行一轮对话
- `/unskill <name>` — 卸载当前会话里的 skill

## 日志规范

dong 默认把运行诊断日志写入当前工作目录的 `logs/dong.log`。日志按单行事件输出，格式包含时间、级别、进程、logger 名称、稳定事件名和 JSON 字段：

```text
2026-05-25 12:00:00,000 INFO pid=12345 dong.cli event=run_loop_started fields={"workdir": "..."}
```

可控项：

- `DONG_LOG_ENABLED=0` — 关闭文件日志
- `DONG_LOG_LEVEL=DEBUG|INFO|WARNING|ERROR` — 控制日志级别，默认 `INFO`
- `DONG_LOG_DIR=logs/debug` — 修改日志目录，默认 `<workdir>/logs`，最终路径必须位于 `<workdir>` 内
- `DONG_LOG_FILE=logs/dong-debug.log` — 修改日志文件；相对路径按 `<workdir>` 解析，最终路径必须位于 `<workdir>` 内
- `DONG_LOG_PAYLOADS=1` — 显式允许记录 prompt、命令等正文预览；默认关闭，只记录长度和结果

事件命名使用小写蛇形，例如 `cli_started`、`llm_request_finished`、`tool_executed`、`file_read`。默认日志不改变终端输出，也不记录完整 prompt、工具参数或工具结果正文。

## DeepSeek V4 参数

dong 通过 OpenAI ChatCompletions 兼容接口调用模型，并支持以下可选环境变量：

- `DONG_THINKING=enabled|disabled` — 传入 `extra_body={"thinking": {"type": ...}}`
- `DONG_REASONING_EFFORT=high|max` — 控制 thinking effort
- `DONG_TOOL_STRICT=1` — 为 function tool schema 加 `strict: true`
- `DONG_RESPONSE_FORMAT=json_object` — 启用 JSON Output；使用时 prompt 里也要明确要求 JSON

模型返回 `reasoning_content` 时，CLI 会以 `thinking` 区块展示；工具调用轮次也会继续保留该字段，确保 DeepSeek V4 thinking + tool-use 的后续请求仍能接上模型推理上下文。

查看和过滤日志：

```bash
dong logs
dong logs --limit 50
dong logs --level WARNING
dong logs --event tool_executed
dong logs --logger dong.tools --contains file_read
dong logs --json --event file_read
dong logs --follow --event tool_executed
```

## Skills

dong 支持两类 skill：

- 本地：`.dong/skills/<name>.md`
- Codex 全局：`${CODEX_HOME:-~/.codex}/skills/<name>/SKILL.md`

同名时本地 skill 优先。dong 原样注入 `SKILL.md`，不会解析或合并 frontmatter。

示例：

```text
/agent-browser 查看我现在打开了多少个页面
```

## 架构

```
dong/
├── dong/
│   ├── __init__.py         # 空 Module
│   ├── __main__.py         # python -m dong 入口
│   ├── cli.py              # CLI 主入口 + Agent 循环 + Skill 管理 (~450L)
│   ├── llm.py              # OpenAI ChatCompletions 封装 (~130L)
│   ├── tools.py            # 内置工具实现 (read/write/edit/bash/grep/fetch) (~450L)
│   ├── tool.py             # 工具框架：注册/校验/执行/结构化结果 (~150L)
│   ├── ui.py               # 终端 UI 适配层 (Rich + prompt_toolkit) (~325L)
│   ├── logging_config.py   # 文件日志配置 + 结构化事件 (~200L)
│   └── log_viewer.py       # dong logs 子命令实现 (~185L)
├── docs/
│   └── adr/                # 架构决策记录
├── pyproject.toml
└── .env.example
```

核心约 2000 行，按职责拆分为 9 个 Module。详细架构决策见 [ADR](docs/adr/)。
