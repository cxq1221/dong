"""工具框架：负责工具注册、参数校验和结构化结果封装。"""
import json
import logging
import time
from typing import Dict, get_type_hints

from pydantic import BaseModel

from dong.logging_config import get_logger, log_event

LOGGER = get_logger(__name__)


class ToolResult(BaseModel):
    """每次工具调用的统一结构化输出。"""
    success: bool
    summary: str = ""
    detail: str = ""
    error: str = ""

    def to_message(self) -> dict:
        """把结构化结果转换成可追加回模型上下文的消息内容。"""
        text = f"[{'✓' if self.success else '✗'}] {self.summary}"
        if self.detail:
            text += f"\n---\n{self.detail}"
        if self.error:
            text += f"\n---\nerror: {self.error}"
        return {
            "content": text,
            "structured": self.model_dump(),
        }


class Tool:
    """已经注册到工具表中的单个工具。"""
    def __init__(self, name: str, description: str, input_model: type[BaseModel], fn):
        self.name = name
        self.description = description
        self.input_model = input_model
        self.fn = fn

    @property
    def schema(self) -> dict:
        """根据 Pydantic 入参模型生成 OpenAI function-calling schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            }
        }


class ToolRegistry:
    """中心注册表；工具通过 @registry.register() 自注册。"""
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, name: str, description: str = ""):
        """装饰器：把函数注册成一个可被模型调用的工具。

        函数第一个参数必须标注为 `args: SomeInputModel`，
        且 SomeInputModel 必须继承 Pydantic BaseModel。

        用法示例：
            @registry.register("read", "Read a file")
            def read_tool(args: ReadInput, cwd: str) -> ToolResult:
                ...
        """
        def decorator(fn):
            hints = get_type_hints(fn)
            input_model = hints.get('args')
            if input_model is None or not (
                isinstance(input_model, type) and issubclass(input_model, BaseModel)
            ):
                raise ValueError(
                    f"Tool '{name}' requires 'args' parameter with a Pydantic "
                    f"BaseModel type hint. Got: {input_model}"
                )
            desc = description or (fn.__doc__ or "").strip()
            self._tools[name] = Tool(name, desc, input_model, fn)
            log_event(
                LOGGER,
                logging.DEBUG,
                "tool_registered",
                tool=name,
                input_model=input_model.__name__,
            )
            return fn
        return decorator

    @property
    def definitions(self) -> list:
        """返回 OpenAI 兼容的工具 schema 列表。"""
        return [t.schema for t in self._tools.values()]

    def execute(self, name: str, raw_args: str, cwd: str) -> ToolResult:
        """按工具名执行工具，并在入口处完成 JSON 解析和参数校验。"""
        tool = self._tools.get(name)
        if not tool:
            log_event(LOGGER, logging.WARNING, "tool_unknown", tool=name, cwd=cwd)
            return ToolResult(success=False, error=f"Unknown tool: {name}")
        started = time.monotonic()
        try:
            args_dict = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            validated = tool.input_model(**args_dict)
        except Exception as e:
            log_event(
                LOGGER,
                logging.WARNING,
                "tool_invalid_input",
                tool=name,
                error=type(e).__name__,
            )
            return ToolResult(success=False, error=f"Invalid input for {name}: {e}")
        try:
            result = tool.fn(validated, cwd)
        except FileNotFoundError as e:
            result = ToolResult(success=False, error=f"File not found: {e}")
        except PermissionError as e:
            result = ToolResult(success=False, error=f"Permission denied: {e}")
        except Exception as e:
            result = ToolResult(success=False, error=f"{type(e).__name__}: {e}")
        duration_ms = int((time.monotonic() - started) * 1000)
        log_event(
            LOGGER,
            logging.INFO if result.success else logging.WARNING,
            "tool_executed",
            tool=name,
            success=result.success,
            duration_ms=duration_ms,
            summary=result.summary,
            error=result.error,
        )
        return result


# 单例注册表供 tools.py 导入，保证所有工具进入同一张注册表。
registry = ToolRegistry()
