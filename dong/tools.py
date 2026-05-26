"""内置工具实现：每个工具函数都通过 @registry.register() 暴露给模型。"""
import logging
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from dong.logging_config import get_logger, log_event, preview_payload
from dong.tool import ToolResult, registry

LOGGER = get_logger(__name__)


# ═══════════════════════════════════════
#  Input models
# ═══════════════════════════════════════

class ReadInput(BaseModel):
    """read 工具入参：读取项目目录下的一个文件。"""
    filepath: str = Field(description="Path to file, relative to project root")

class WriteInput(BaseModel):
    """write 工具入参：写入指定文件内容。"""
    filepath: str
    content: str

class EditInput(BaseModel):
    """edit 工具入参：用唯一匹配的旧字符串替换成新字符串。"""
    filepath: str
    old_string: str = Field(description="Text to find (must be unique)")
    new_string: str

class BashInput(BaseModel):
    """bash 工具入参：要在项目目录执行的 shell 命令。"""
    command: str

class GrepInput(BaseModel):
    """grep 工具入参：正则模式和搜索路径。"""
    pattern: str
    path: str = "."

class FetchInput(BaseModel):
    """fetch 工具入参：要 GET 的 URL 和超时时间。"""
    url: str
    timeout: int = Field(default=15, gt=0)

class PlanItem(BaseModel):
    """update_plan 单个步骤：描述任务和当前状态。"""
    step: str = Field(description="One short step in the plan")
    status: Literal["pending", "in_progress", "completed"] = Field(
        description="Current status for this step",
    )

class UpdatePlanInput(BaseModel):
    """update_plan 工具入参：一次性提交完整最新计划。"""
    explanation: str = Field(
        default="",
        description="Optional short explanation for why the plan changed",
    )
    plan: list[PlanItem] = Field(
        min_length=1,
        description="Full current plan, not a partial patch",
    )

    @model_validator(mode="after")
    def validate_plan_shape(self) -> "UpdatePlanInput":
        """保持 Codex 风格约束：最多一个 in_progress，步骤不能为空。"""
        in_progress_count = sum(1 for item in self.plan if item.status == "in_progress")
        if in_progress_count > 1:
            raise ValueError("at most one plan item may be in_progress")
        if any(not item.step.strip() for item in self.plan):
            raise ValueError("plan steps must be non-empty")
        return self


# ═══════════════════════════════════════
#  Security helpers
# ═══════════════════════════════════════

def _validate_path(cwd, filepath):
    """解析文件路径，并阻止访问 cwd 之外的位置。"""
    full = Path(cwd).resolve() / filepath
    resolved = full.resolve()
    cwd_resolved = Path(cwd).resolve()
    if not str(resolved).startswith(str(cwd_resolved)):
        log_event(
            LOGGER,
            logging.WARNING,
            "path_traversal_denied",
            cwd=str(cwd_resolved),
            filepath=str(filepath),
            resolved=str(resolved),
        )
        raise PermissionError(f"Path traversal denied: {filepath} → {resolved}")
    return str(resolved)


def _is_dangerous(cmd: str) -> bool:
    """用保守前缀规则判断 bash 命令是否看起来危险。"""
    return any(cmd.strip().startswith(x) for x in ("rm ", "mv ", "sudo ", ">", "|"))


# ═══════════════════════════════════════
#  Tool implementations
#  Each: @registry.register(name, desc)
#        def tool_name(args: InputModel, cwd: str) -> ToolResult:
# ═══════════════════════════════════════

@registry.register("read", "Read a file with line numbers")
def read_tool(args: ReadInput, cwd: str) -> ToolResult:
    """读取文件并在返回内容中附带行号，方便模型精确引用。"""
    path = _validate_path(cwd, args.filepath)
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    n = len(lines)
    log_event(
        LOGGER,
        logging.INFO,
        "file_read",
        filepath=args.filepath,
        lines=n,
        bytes=Path(path).stat().st_size,
    )
    display = "\n".join(f"{i+1:4d}|{line}" for i, line in enumerate(lines))
    return ToolResult(
        success=True,
        summary=f"📄 {args.filepath} ({n} lines)",
        detail=display,
    )


@registry.register("write", "Write content to a file (overwrites existing)")
def write_tool(args: WriteInput, cwd: str) -> ToolResult:
    """写入文件内容；父目录不存在时自动创建。"""
    path = _validate_path(cwd, args.filepath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(args.content)
    log_event(
        LOGGER,
        logging.INFO,
        "file_written",
        filepath=args.filepath,
        chars=len(args.content),
    )
    return ToolResult(
        success=True,
        summary=f"✅ Written {len(args.content)} chars to {args.filepath}",
    )


@registry.register("edit", "Find-and-replace one occurrence in a file")
def edit_tool(args: EditInput, cwd: str) -> ToolResult:
    """只替换唯一出现的一处文本，避免误改多处匹配。"""
    path = _validate_path(cwd, args.filepath)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    old, new = args.old_string, args.new_string
    if old not in content:
        log_event(
            LOGGER,
            logging.WARNING,
            "file_edit_miss",
            filepath=args.filepath,
            old_chars=len(old),
        )
        return ToolResult(success=False, error=f"old_string not found in {args.filepath}")
    if content.count(old) > 1:
        log_event(
            LOGGER,
            logging.WARNING,
            "file_edit_ambiguous",
            filepath=args.filepath,
            old_chars=len(old),
            matches=content.count(old),
        )
        return ToolResult(
            success=False,
            error=f"Ambiguous: '{old[:40]}' appears {content.count(old)} times",
        )
    content = content.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    log_event(
        LOGGER,
        logging.INFO,
        "file_edited",
        filepath=args.filepath,
        old_chars=len(old),
        new_chars=len(new),
    )
    return ToolResult(success=True, summary=f"✅ Edited {args.filepath}")


@registry.register("bash", "Run a shell command in the project directory")
def bash_tool(args: BashInput, cwd: str) -> ToolResult:
    """在项目目录运行 shell 命令，并把 stdout/stderr 合并返回。"""
    log_event(
        LOGGER,
        logging.INFO,
        "bash_started",
        cwd=cwd,
        command_chars=len(args.command),
        command_preview=preview_payload(args.command),
    )
    try:
        r = subprocess.run(
            args.command, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=30,
        )
        out = r.stdout + r.stderr
        log_event(
            LOGGER,
            logging.INFO if r.returncode == 0 else logging.WARNING,
            "bash_finished",
            returncode=r.returncode,
            stdout_chars=len(r.stdout),
            stderr_chars=len(r.stderr),
        )
        if r.returncode != 0:
            return ToolResult(
                success=False,
                summary=f"exit code {r.returncode}",
                detail=out[:3000] if out else "(no output)",
            )
    except subprocess.TimeoutExpired:
        log_event(LOGGER, logging.WARNING, "bash_timeout", timeout_seconds=30)
        return ToolResult(success=False, error="Timeout after 30s")
    if len(out) > 3000:
        out = out[:3000] + f"\n... (truncated, {len(out)} total chars)"
    return ToolResult(success=True, summary=f"$ {args.command}", detail=out or "(no output)")


@registry.register("grep", "Search for text in files using regex")
def grep_tool(args: GrepInput, cwd: str) -> ToolResult:
    """调用系统 grep 递归搜索文本，返回匹配摘要。"""
    try:
        r = subprocess.run(
            ["grep", "-rn", args.pattern, args.path],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip() or "(no matches)"
        if r.stderr:
            out += f"\n(stderr: {r.stderr.strip()[:200]})"
    except Exception as e:
        log_event(
            LOGGER,
            logging.WARNING,
            "grep_failed",
            path=args.path,
            pattern_chars=len(args.pattern),
            error=type(e).__name__,
        )
        return ToolResult(success=False, error=str(e))
    log_event(
        LOGGER,
        logging.INFO,
        "grep_finished",
        path=args.path,
        pattern_chars=len(args.pattern),
        returncode=r.returncode,
        output_chars=len(out),
    )
    if len(out) > 3000:
        out = out[:3000] + "\n... (truncated)"
    return ToolResult(success=True, summary=f"🔍 '{args.pattern}'", detail=out)


@registry.register("fetch", "GET a URL and return its content")
def fetch_tool(args: FetchInput, cwd: str) -> ToolResult:
    """发起简单 HTTP GET，并截断过长响应以保护上下文长度。"""
    parsed = urllib.parse.urlparse(args.url)
    log_event(
        LOGGER,
        logging.INFO,
        "fetch_started",
        scheme=parsed.scheme,
        host=parsed.netloc,
        path=parsed.path,
        timeout=args.timeout,
    )
    req = urllib.request.Request(
        args.url,
        headers={"User-Agent": "dong/0.1"},
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    content_type = resp.headers.get("Content-Type", "?")
    size = len(text)
    log_event(
        LOGGER,
        logging.INFO,
        "fetch_finished",
        scheme=parsed.scheme,
        host=parsed.netloc,
        status=getattr(resp, "status", None),
        content_type=content_type,
        chars=size,
    )
    if size > 5000:
        text = text[:5000] + f"\n... (truncated, {size} total chars)"
    return ToolResult(
        success=True,
        summary=f"🌐 GET {args.url} ({content_type}, {size} chars)",
        detail=text,
    )


@registry.register("update_plan", "Update the visible task plan")
def update_plan_tool(args: UpdatePlanInput, cwd: str) -> ToolResult:
    """更新当前任务计划，并返回适合展示给用户和模型继续引用的摘要。"""
    status_labels = {
        "pending": "pending",
        "in_progress": "in_progress",
        "completed": "completed",
    }
    lines = []
    if args.explanation.strip():
        lines.append(args.explanation.strip())
        lines.append("")
    for index, item in enumerate(args.plan, start=1):
        lines.append(f"{index}. [{status_labels[item.status]}] {item.step.strip()}")
    log_event(
        LOGGER,
        logging.INFO,
        "plan_updated",
        items=len(args.plan),
        in_progress=sum(1 for item in args.plan if item.status == "in_progress"),
        completed=sum(1 for item in args.plan if item.status == "completed"),
    )
    return ToolResult(
        success=True,
        summary=f"Updated plan ({len(args.plan)} steps)",
        detail="\n".join(lines),
    )


# ═══════════════════════════════════════
#  Exports (what cli.py imports)
# ═══════════════════════════════════════

# 从注册表自动导出，避免手工维护工具列表和实现之间的重复配置。
TOOL_DEFS = registry.definitions
execute = registry.execute
