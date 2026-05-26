"""MCP stdio 客户端：负责读取项目配置、发现远端工具并转发工具调用。"""

from __future__ import annotations

import json
import logging
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dong.logging_config import get_logger, log_event
from dong.tool import ToolResult, _tool_strict_enabled
from dong.tools import _validate_path

LOGGER = get_logger(__name__)

MCP_CONFIG_RELPATH = ".dong/mcp.json"
MCP_PROTOCOL_VERSION = "2024-11-05"
_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]+")


class McpError(RuntimeError):
    """MCP 启动、协议或工具调用失败时使用的统一异常。"""


class McpServerConfig(BaseModel):
    """单个 MCP server 的项目配置；当前只执行 stdio transport。"""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    transport: str = "stdio"
    startup_timeout_seconds: float = Field(default=10.0, gt=0)
    tool_timeout_seconds: float = Field(default=30.0, gt=0)


class McpConfigFile(BaseModel):
    """`.dong/mcp.json` 的顶层结构，按 server 名组织配置。"""

    model_config = ConfigDict(extra="forbid")

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)


@dataclass(frozen=True)
class McpToolRoute:
    """一个暴露给模型的 MCP tool 与原始 server/tool 名的映射。"""

    server_name: str
    raw_tool_name: str


def normalize_mcp_name(value: str) -> str:
    """把 server/tool 名转换成 OpenAI function tool 可接受的安全名称。"""

    normalized = _SAFE_NAME_RE.sub("_", value.strip()).strip("_")
    return normalized or "unnamed"


def mcp_tool_name(server_name: str, tool_name: str) -> str:
    """生成 dong 暴露给模型的 MCP tool 名称。"""

    return f"mcp__{normalize_mcp_name(server_name)}__{normalize_mcp_name(tool_name)}"


def load_mcp_config(workdir: str) -> McpConfigFile:
    """从项目 `.dong/mcp.json` 读取 MCP 配置；不存在时返回空配置。"""

    path = _validate_path(workdir, MCP_CONFIG_RELPATH)
    if not os.path.exists(path):
        return McpConfigFile()
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    config = McpConfigFile.model_validate(data)
    for name, server in config.servers.items():
        if normalize_mcp_name(name) != name:
            raise McpError(f"Invalid MCP server name: {name!r}")
        if server.transport != "stdio":
            raise McpError(f"MCP server {name!r} uses unsupported transport: {server.transport}")
    return config


class McpStdioClient:
    """单个 stdio MCP server 的阻塞 JSON-RPC 客户端。"""

    def __init__(self, server_name: str, config: McpServerConfig, workdir: str):
        self.server_name = server_name
        self.config = config
        self.workdir = workdir
        self.process: subprocess.Popen[bytes] | None = None
        self._next_request_id = 1

    def start(self) -> None:
        """启动 server 进程并完成 MCP initialize 握手。"""

        env = {}
        for key in ("PATH", "HOME", "SHELL", "LANG", "LC_ALL", "TMPDIR"):
            if key in os.environ:
                env[key] = os.environ[key]
        env.update(self.config.env)
        argv = [self.config.command, *self.config.args]
        self.process = subprocess.Popen(
            argv,
            cwd=self.workdir,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        result = self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "dong", "version": "0.1.0"},
            },
            timeout=self.config.startup_timeout_seconds,
        )
        if not isinstance(result, dict):
            raise McpError(f"MCP server {self.server_name!r} returned invalid initialize result")
        self.notify("notifications/initialized", {})
        log_event(LOGGER, logging.INFO, "mcp_server_started", server=self.server_name)

    def list_tools(self) -> list[dict[str, Any]]:
        """分页读取 server 暴露的 tools。"""

        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.request(
                "tools/list",
                params,
                timeout=self.config.startup_timeout_seconds,
            )
            if not isinstance(result, dict):
                raise McpError(f"MCP server {self.server_name!r} returned invalid tools/list result")
            tools.extend(tool for tool in result.get("tools", []) if isinstance(tool, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用原始 MCP tool，并返回 server 的 `tools/call` 结果。"""

        result = self.request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=self.config.tool_timeout_seconds,
        )
        if not isinstance(result, dict):
            raise McpError(f"MCP tool {tool_name!r} returned invalid result")
        return result

    def request(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        """发送 JSON-RPC request，并等待同 id response。"""

        request_id = self._next_request_id
        self._next_request_id += 1
        self._send({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        })
        deadline = time.monotonic() + timeout
        while True:
            message = self._read_message(deadline)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise McpError(f"MCP {method} failed: {message['error']}")
            return message.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """发送 JSON-RPC notification，不等待响应。"""

        self._send({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        })

    def close(self) -> None:
        """尽量按 MCP shutdown/exit 关闭 server，失败时终止进程。"""

        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                self.request("shutdown", {}, timeout=1.0)
                self.notify("exit", {})
        except Exception:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        self.process = None
        log_event(LOGGER, logging.INFO, "mcp_server_stopped", server=self.server_name)

    def _send(self, payload: dict[str, Any]) -> None:
        """按 MCP stdio header + JSON body 格式写入一条消息。"""

        process = self._ensure_process()
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        assert process.stdin is not None
        process.stdin.write(header + body)
        process.stdin.flush()

    def _read_message(self, deadline: float) -> dict[str, Any]:
        """从 stdout 读取一条 MCP 消息，并解析 JSON body。"""

        content_length: int | None = None
        while True:
            line = self._read_line(deadline)
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("ascii", errors="replace").partition(":")
            if key.lower() == "content-length":
                content_length = int(value.strip())
        if content_length is None:
            raise McpError(f"MCP server {self.server_name!r} response missing Content-Length")
        body = self._read_exact(content_length, deadline)
        return json.loads(body.decode("utf-8"))

    def _read_line(self, deadline: float) -> bytes:
        """带超时读取 stdout 的一行 header。"""

        chunks: list[bytes] = []
        while True:
            chunk = self._read_available(1, deadline)
            if chunk == b"":
                raise McpError(f"MCP server {self.server_name!r} closed stdout")
            chunks.append(chunk)
            if chunk == b"\n":
                return b"".join(chunks)

    def _read_exact(self, length: int, deadline: float) -> bytes:
        """带超时读取指定长度 body，避免协议半包时无限阻塞。"""

        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self._read_available(remaining, deadline)
            if chunk == b"":
                raise McpError(f"MCP server {self.server_name!r} closed stdout")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_available(self, max_bytes: int, deadline: float) -> bytes:
        """等待 stdout 可读后用 os.read 读取原始 bytes。"""

        process = self._ensure_process()
        assert process.stdout is not None
        timeout = max(0.0, deadline - time.monotonic())
        if timeout == 0:
            raise McpError(f"MCP server {self.server_name!r} timed out")
        readable, _, _ = select.select([process.stdout.fileno()], [], [], timeout)
        if not readable:
            raise McpError(f"MCP server {self.server_name!r} timed out")
        return os.read(process.stdout.fileno(), max_bytes)

    def _ensure_process(self) -> subprocess.Popen[bytes]:
        """确认 server 进程仍存在，便于在协议错误时返回明确异常。"""

        if self.process is None:
            raise McpError(f"MCP server {self.server_name!r} is not started")
        if self.process.poll() is not None:
            raise McpError(f"MCP server {self.server_name!r} exited early")
        return self.process


class McpManager:
    """一轮 dong agent loop 内的 MCP server 管理器。"""

    def __init__(self, workdir: str, config: McpConfigFile):
        self.workdir = workdir
        self.config = config
        self.clients: dict[str, McpStdioClient] = {}
        self.routes: dict[str, McpToolRoute] = {}
        self.tool_definitions: list[dict[str, Any]] = []
        self.startup_errors: dict[str, str] = {}

    @classmethod
    def from_workdir(cls, workdir: str) -> "McpManager":
        """根据项目目录创建 manager；配置缺失时返回空 manager。"""

        return cls(workdir, load_mcp_config(workdir))

    def start(self) -> None:
        """启动所有 enabled server，并缓存可注入模型的 tool definitions。"""

        for server_name, server_config in self.config.servers.items():
            if not server_config.enabled:
                continue
            client: McpStdioClient | None = None
            try:
                client = McpStdioClient(server_name, server_config, self.workdir)
                client.start()
                server_routes: dict[str, McpToolRoute] = {}
                server_tool_definitions: list[dict[str, Any]] = []
                for tool in client.list_tools():
                    self._build_tool_route(
                        server_name,
                        tool,
                        routes=server_routes,
                        tool_definitions=server_tool_definitions,
                    )
                self.clients[server_name] = client
                self.routes.update(server_routes)
                self.tool_definitions.extend(server_tool_definitions)
            except Exception as exc:
                if client is not None:
                    client.close()
                self.startup_errors[server_name] = str(exc)
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "mcp_server_start_failed",
                    server=server_name,
                    error=type(exc).__name__,
                )

    def close(self) -> None:
        """关闭本轮启动的所有 MCP server。"""

        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def has_tool(self, tool_name: str) -> bool:
        """判断模型请求的工具名是否属于 MCP route。"""

        return tool_name in self.routes

    def execute(self, tool_name: str, raw_args: str | dict[str, Any]) -> ToolResult:
        """把模型 tool call 参数转发给对应 MCP server。"""

        route = self.routes.get(tool_name)
        if route is None:
            return ToolResult(success=False, error=f"Unknown MCP tool: {tool_name}")
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            if not isinstance(arguments, dict):
                raise ValueError("MCP tool arguments must be a JSON object")
            result = self.clients[route.server_name].call_tool(route.raw_tool_name, arguments)
            return _mcp_result_to_tool_result(route.server_name, route.raw_tool_name, result)
        except Exception as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "mcp_tool_call_failed",
                tool=tool_name,
                server=route.server_name,
                error=type(exc).__name__,
            )
            return ToolResult(success=False, error=f"{type(exc).__name__}: {exc}")

    def _build_tool_route(
        self,
        server_name: str,
        tool: dict[str, Any],
        *,
        routes: dict[str, McpToolRoute],
        tool_definitions: list[dict[str, Any]],
    ) -> None:
        """把 MCP tools/list 里的单个 tool 转成局部 route，成功后再提交。"""

        raw_name = str(tool.get("name") or "")
        if not raw_name:
            return
        exposed_name = mcp_tool_name(server_name, raw_name)
        if exposed_name in routes or exposed_name in self.routes:
            raise McpError(f"MCP tool name collision: {exposed_name}")
        parameters = tool.get("inputSchema")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}
        function = {
            "name": exposed_name,
            "description": f"[MCP:{server_name}] {tool.get('description') or raw_name}",
            "parameters": parameters,
        }
        if _tool_strict_enabled():
            function["strict"] = True
        routes[exposed_name] = McpToolRoute(server_name, raw_name)
        tool_definitions.append({"type": "function", "function": function})
        log_event(LOGGER, logging.INFO, "mcp_tool_registered", server=server_name, tool=exposed_name)


def _mcp_result_to_tool_result(server_name: str, tool_name: str, result: dict[str, Any]) -> ToolResult:
    """把 MCP `tools/call` 结果压成 dong 已有 ToolResult 格式。"""

    content = result.get("content") or []
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    if "structuredContent" in result:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    detail = "\n".join(part for part in parts if part)
    is_error = bool(result.get("isError"))
    return ToolResult(
        success=not is_error,
        summary=f"MCP {server_name}.{tool_name}",
        detail=detail,
        error=detail if is_error else "",
    )


def configured_server_names(workdir: str) -> list[str]:
    """返回项目已配置的 MCP server 名称，供 CLI 展示使用。"""

    return sorted(load_mcp_config(workdir).servers)


def config_path(workdir: str) -> Path:
    """返回项目 MCP 配置文件路径，便于错误提示展示。"""

    return Path(_validate_path(workdir, MCP_CONFIG_RELPATH))
