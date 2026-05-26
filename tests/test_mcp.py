"""MCP stdio 支持测试：覆盖配置加载、工具发现、工具调用和 CLI 查看。"""

from __future__ import annotations

import json
import sys

from dong import cli
from dong.mcp import McpError, McpManager, configured_server_names, load_mcp_config, mcp_tool_name

from mcp_helpers import write_fake_mcp_server, write_mcp_config


def test_mcp_manager_discovers_and_calls_stdio_tool(tmp_path) -> None:
    """McpManager 应能发现 fake server 的 tool，并把调用转发给 MCP server。"""

    server_script = tmp_path / "fake_mcp.py"
    write_fake_mcp_server(server_script)
    write_mcp_config(tmp_path, server_script)

    manager = McpManager.from_workdir(str(tmp_path))
    manager.start()
    try:
        tool_name = mcp_tool_name("demo", "echo")

        assert configured_server_names(str(tmp_path)) == ["demo"]
        assert tool_name in {tool["function"]["name"] for tool in manager.tool_definitions}

        result = manager.execute(tool_name, '{"value": "hello"}')

        assert result.success is True
        assert "echo: hello" in result.detail
    finally:
        manager.close()


def test_mcp_list_cli_prints_server_and_tools(tmp_path, capsys) -> None:
    """`dong mcp list` 应展示 server 连接状态和发现到的 MCP tool。"""

    server_script = tmp_path / "fake_mcp.py"
    write_fake_mcp_server(server_script)
    write_mcp_config(tmp_path, server_script)

    cli.run_mcp_cli(["list", "-d", str(tmp_path), "--tools"])

    captured = capsys.readouterr()
    assert "demo\tconnected" in captured.out
    assert mcp_tool_name("demo", "echo") in captured.out


def test_main_routes_mcp_subcommand(tmp_path, monkeypatch, capsys) -> None:
    """CLI 主入口应把 `dong mcp list` 路由到 MCP 查看命令。"""

    server_script = tmp_path / "fake_mcp.py"
    write_fake_mcp_server(server_script)
    write_mcp_config(tmp_path, server_script)
    monkeypatch.setattr(sys, "argv", ["dong", "mcp", "list", "-d", str(tmp_path), "--tools"])

    cli.main()

    assert "demo\tconnected" in capsys.readouterr().out


def test_mcp_list_without_tools_does_not_start_server(tmp_path, capsys) -> None:
    """`dong mcp list` 默认只列配置，不应执行项目里的 server command。"""

    marker = tmp_path / "started.txt"
    server_script = tmp_path / "marker.py"
    server_script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('started', encoding='utf-8')\n",
        encoding="utf-8",
    )
    write_mcp_config(tmp_path, server_script)

    cli.run_mcp_cli(["list", "-d", str(tmp_path)])

    assert "demo\tconfigured" in capsys.readouterr().out
    assert not marker.exists()


def test_mcp_tool_error_returns_tool_result(tmp_path) -> None:
    """MCP 工具参数错误应降级成 ToolResult 失败结果，而不是让 agent loop 崩溃。"""

    server_script = tmp_path / "fake_mcp.py"
    write_fake_mcp_server(server_script)
    write_mcp_config(tmp_path, server_script)
    manager = McpManager.from_workdir(str(tmp_path))
    manager.start()
    try:
        result = manager.execute(mcp_tool_name("demo", "echo"), '["not-object"]')
    finally:
        manager.close()

    assert result.success is False
    assert "arguments must be a JSON object" in result.error


def test_mcp_config_rejects_unsafe_server_name(tmp_path) -> None:
    """server 名必须能安全进入 `mcp__server__tool` 工具名和路径提示。"""

    config_dir = tmp_path / ".dong"
    config_dir.mkdir()
    (config_dir / "mcp.json").write_text(
        json.dumps({"servers": {"bad/name": {"command": sys.executable}}}),
        encoding="utf-8",
    )

    try:
        load_mcp_config(str(tmp_path))
    except McpError as exc:
        assert "Invalid MCP server name" in str(exc)
    else:
        raise AssertionError("expected invalid MCP server name")


def test_mcp_tool_name_collision_marks_server_failed(tmp_path) -> None:
    """同一 server 下归一化后重名的工具应失败，避免最后写入覆盖路由。"""

    server_script = tmp_path / "collision_mcp.py"
    server_script.write_text(
        r'''
import json
import sys


def read_message():
    content_length = None
    while True:
        line = sys.stdin.buffer.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode("ascii").partition(":")
        if key.lower() == "content-length":
            content_length = int(value.strip())
    if content_length is None:
        return None
    return json.loads(sys.stdin.buffer.read(content_length).decode("utf-8"))


def send(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
            {"name": "a/b", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "a b", "inputSchema": {"type": "object", "properties": {}}},
        ]}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": request_id, "result": None})
        break
'''.lstrip(),
        encoding="utf-8",
    )
    write_mcp_config(tmp_path, server_script)

    manager = McpManager.from_workdir(str(tmp_path))
    manager.start()
    try:
        assert "demo" in manager.startup_errors
        assert "collision" in manager.startup_errors["demo"]
        assert "demo" not in manager.clients
        assert manager.routes == {}
        assert manager.tool_definitions == []
    finally:
        manager.close()


def test_mcp_server_does_not_inherit_api_key(tmp_path, monkeypatch) -> None:
    """MCP 子进程只继承最小环境，避免把 DONG_API_KEY 自动泄露给项目配置命令。"""

    monkeypatch.setenv("DONG_API_KEY", "secret-value")
    server_script = tmp_path / "env_mcp.py"
    server_script.write_text(
        r'''
import json
import os
import sys


def read_message():
    content_length = None
    while True:
        line = sys.stdin.buffer.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        key, _, value = line.decode("ascii").partition(":")
        if key.lower() == "content-length":
            content_length = int(value.strip())
    if content_length is None:
        return None
    return json.loads(sys.stdin.buffer.read(content_length).decode("utf-8"))


def send(payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


while True:
    message = read_message()
    if message is None:
        break
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}}})
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "env", "inputSchema": {"type": "object", "properties": {}}}]}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": os.getenv("DONG_API_KEY", "")}]}})
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": request_id, "result": None})
        break
'''.lstrip(),
        encoding="utf-8",
    )
    write_mcp_config(tmp_path, server_script)
    manager = McpManager.from_workdir(str(tmp_path))
    manager.start()
    try:
        result = manager.execute(mcp_tool_name("demo", "env"), "{}")
    finally:
        manager.close()

    assert result.success is True
    assert "secret-value" not in result.detail
