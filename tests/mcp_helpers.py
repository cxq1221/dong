"""MCP 测试辅助：生成可通过 stdio 通信的最小 fake MCP server。"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def write_fake_mcp_server(path: Path) -> None:
    """写入一个支持 initialize/tools/list/tools/call 的测试 server 脚本。"""

    path.write_text(
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
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1.0.0"},
            },
        })
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo a value",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    }
                ]
            },
        })
    elif method == "tools/call":
        arguments = message["params"].get("arguments", {})
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": "echo: " + arguments.get("value", "")}]
            },
        })
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": request_id, "result": None})
        break
    else:
        send({
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "method not found"},
        })
'''.lstrip(),
        encoding="utf-8",
    )


def write_mcp_config(workdir: Path, server_script: Path) -> None:
    """按 dong 的 `.dong/mcp.json` schema 写入测试配置。"""

    config_dir = workdir / ".dong"
    config_dir.mkdir(parents=True)
    (config_dir / "mcp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "demo": {
                        "command": sys.executable,
                        "args": [str(server_script)],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
