"""Tool framework: registry + ToolResult."""
import json, inspect
from typing import Dict, get_type_hints
from pydantic import BaseModel


class ToolResult(BaseModel):
    """Structured output for every tool call."""
    success: bool
    summary: str = ""
    detail: str = ""
    error: str = ""

    def to_message(self) -> dict:
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
    """A registered tool."""
    def __init__(self, name: str, description: str, input_model: type[BaseModel], fn):
        self.name = name
        self.description = description
        self.input_model = input_model
        self.fn = fn

    @property
    def schema(self) -> dict:
        """OpenAI function-calling schema, auto-generated from Pydantic model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_model.model_json_schema(),
            }
        }


class ToolRegistry:
    """Central registry. Tools self-register via @registry.register()."""
    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, name: str, description: str = ""):
        """Decorator: register a function as a tool.

        The function must have `args: SomeInputModel` as its first parameter,
        where SomeInputModel is a Pydantic BaseModel.

        Usage:
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
            return fn
        return decorator

    @property
    def definitions(self) -> list:
        """List of OpenAI-compatible tool schemas."""
        return [t.schema for t in self._tools.values()]

    def execute(self, name: str, raw_args: str, cwd: str) -> ToolResult:
        """Execute a tool by name with raw JSON args."""
        tool = self._tools.get(name)
        if not tool:
            return ToolResult(success=False, error=f"Unknown tool: {name}")
        try:
            args_dict = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            validated = tool.input_model(**args_dict)
        except Exception as e:
            return ToolResult(success=False, error=f"Invalid input for {name}: {e}")
        try:
            return tool.fn(validated, cwd)
        except FileNotFoundError as e:
            return ToolResult(success=False, error=f"File not found: {e}")
        except PermissionError as e:
            return ToolResult(success=False, error=f"Permission denied: {e}")
        except Exception as e:
            return ToolResult(success=False, error=f"{type(e).__name__}: {e}")


# Singleton registry (imported by tools.py)
registry = ToolRegistry()
