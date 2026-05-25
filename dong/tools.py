"""Tool implementations — each tool is a function with @registry.register()."""
import subprocess, os, urllib.request, urllib.error, json
from pathlib import Path
from pydantic import BaseModel, Field

from dong.tool import ToolResult, registry


# ═══════════════════════════════════════
#  Input models
# ═══════════════════════════════════════

class ReadInput(BaseModel):
    filepath: str = Field(description="Path to file, relative to project root")

class WriteInput(BaseModel):
    filepath: str
    content: str

class EditInput(BaseModel):
    filepath: str
    old_string: str = Field(description="Text to find (must be unique)")
    new_string: str

class BashInput(BaseModel):
    command: str

class GrepInput(BaseModel):
    pattern: str
    path: str = "."

class FetchInput(BaseModel):
    url: str
    timeout: int = 15


# ═══════════════════════════════════════
#  Security helpers
# ═══════════════════════════════════════

def _validate_path(cwd, filepath):
    """Resolve path and prevent traversal outside cwd."""
    full = Path(cwd).resolve() / filepath
    resolved = full.resolve()
    cwd_resolved = Path(cwd).resolve()
    if not str(resolved).startswith(str(cwd_resolved)):
        raise PermissionError(f"Path traversal denied: {filepath} → {resolved}")
    return str(resolved)


def _is_dangerous(cmd: str) -> bool:
    """Check if a bash command looks dangerous."""
    return any(cmd.strip().startswith(x) for x in ("rm ", "mv ", "sudo ", ">", "|"))


# ═══════════════════════════════════════
#  Tool implementations
#  Each: @registry.register(name, desc)
#        def tool_name(args: InputModel, cwd: str) -> ToolResult:
# ═══════════════════════════════════════

@registry.register("read", "Read a file with line numbers")
def read_tool(args: ReadInput, cwd: str) -> ToolResult:
    path = _validate_path(cwd, args.filepath)
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    n = len(lines)
    display = "\n".join(f"{i+1:4d}|{l}" for i, l in enumerate(lines))
    return ToolResult(
        success=True,
        summary=f"📄 {args.filepath} ({n} lines)",
        detail=display,
    )


@registry.register("write", "Write content to a file (overwrites existing)")
def write_tool(args: WriteInput, cwd: str) -> ToolResult:
    path = _validate_path(cwd, args.filepath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(args.content)
    return ToolResult(
        success=True,
        summary=f"✅ Written {len(args.content)} chars to {args.filepath}",
    )


@registry.register("edit", "Find-and-replace one occurrence in a file")
def edit_tool(args: EditInput, cwd: str) -> ToolResult:
    path = _validate_path(cwd, args.filepath)
    with open(path, encoding="utf-8") as f:
        content = f.read()
    old, new = args.old_string, args.new_string
    if old not in content:
        return ToolResult(success=False, error=f"old_string not found in {args.filepath}")
    if content.count(old) > 1:
        return ToolResult(
            success=False,
            error=f"Ambiguous: '{old[:40]}' appears {content.count(old)} times",
        )
    content = content.replace(old, new, 1)
    with open(path, "w") as f:
        f.write(content)
    return ToolResult(success=True, summary=f"✅ Edited {args.filepath}")


@registry.register("bash", "Run a shell command in the project directory")
def bash_tool(args: BashInput, cwd: str) -> ToolResult:
    try:
        r = subprocess.run(
            args.command, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=30,
        )
        out = r.stdout + r.stderr
        if r.returncode != 0:
            return ToolResult(
                success=False,
                summary=f"exit code {r.returncode}",
                detail=out[:3000] if out else "(no output)",
            )
    except subprocess.TimeoutExpired:
        return ToolResult(success=False, error="Timeout after 30s")
    if len(out) > 3000:
        out = out[:3000] + f"\n... (truncated, {len(out)} total chars)"
    return ToolResult(success=True, summary=f"$ {args.command}", detail=out or "(no output)")


@registry.register("grep", "Search for text in files using regex")
def grep_tool(args: GrepInput, cwd: str) -> ToolResult:
    try:
        r = subprocess.run(
            ["grep", "-rn", args.pattern, args.path],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        out = r.stdout.strip() or "(no matches)"
        if r.stderr:
            out += f"\n(stderr: {r.stderr.strip()[:200]})"
    except Exception as e:
        return ToolResult(success=False, error=str(e))
    if len(out) > 3000:
        out = out[:3000] + f"\n... (truncated)"
    return ToolResult(success=True, summary=f"🔍 '{args.pattern}'", detail=out)


@registry.register("fetch", "GET a URL and return its content")
def fetch_tool(args: FetchInput, cwd: str) -> ToolResult:
    req = urllib.request.Request(
        args.url,
        headers={"User-Agent": "dong/0.1"},
    )
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    content_type = resp.headers.get("Content-Type", "?")
    size = len(text)
    if size > 5000:
        text = text[:5000] + f"\n... (truncated, {size} total chars)"
    return ToolResult(
        success=True,
        summary=f"🌐 GET {args.url} ({content_type}, {size} chars)",
        detail=text,
    )


# ═══════════════════════════════════════
#  Exports (what cli.py imports)
# ═══════════════════════════════════════

# Auto-generated from registry — no manual lists to maintain
TOOL_DEFS = registry.definitions
execute = registry.execute
