"""契约压力 Module：记录复杂任务信号并判断是否启用软契约压力。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

TOOL_THRESHOLD = 5
VERIFY_COMMAND_KEYWORDS = ("pytest", "ruff", "mypy", "test", "lint", "build", "uv run")
FILE_CHANGE_TOOLS = {"write", "edit"}
BEST_PRACTICES_RELPATH = ".dong/contracts/best-practices.md"
DEFAULT_BEST_PRACTICES = """# dong 契约最佳实践

这份材料是复杂开发交付的外部参考，不是强制流程。主 Agent 可以不参考，但交付后会被第三方 scorer 审计。

## 交付原则

交付目标：签名前确认复杂开发交付具备可审阅证据。

- 先确认用户目标、约束和不可扩大范围。
- 修改前读取相关代码、测试和项目规则。
- 修改后保留真实验证证据，包括失败命令和未验证项。
- 最终答复必须说明变更范围、验证结果、风险和下一步。
- 不要用漂亮总结替代验收材料。
- 签名前确认交付可审阅、可复现、可回滚。
"""


class ContractMode(str, Enum):
    """契约压力模式；AUTO 根据信号自动判断，ON/OFF 由用户显式控制。"""

    AUTO = "auto"
    ON = "on"
    OFF = "off"


class TriggerReason(str, Enum):
    """契约压力触发原因；后续 scorer 可复用这些原因做解释。"""

    MANUAL_ON = "manual_on"
    FILE_CHANGE = "file_change"
    VERIFY_COMMAND = "verify_command"
    TOOL_THRESHOLD = "tool_threshold"
    COMPACTION = "compaction"


@dataclass(frozen=True)
class ContractSignal:
    """一次契约相关信号，kind 表示类别，name/detail 保留轻量上下文。"""

    kind: str
    name: str = ""
    detail: str = ""

    @classmethod
    def tool_call(cls, name: str, detail: str = "") -> ContractSignal:
        """记录一次工具调用；detail 可放 bash 命令等补充信息。"""

        return cls(kind="tool_call", name=name, detail=detail)

    @classmethod
    def file_change(cls, name: str, detail: str = "") -> ContractSignal:
        """记录一次文件变更类动作；name 通常是 write/edit。"""

        return cls(kind="file_change", name=name, detail=detail)

    @classmethod
    def compaction(cls, detail: str = "") -> ContractSignal:
        """记录一次上下文压缩信号。"""

        return cls(kind="compaction", detail=detail)


@dataclass
class ContractController:
    """契约压力控制器；只负责内存态信号收集和是否激活的判断。"""

    workdir: str
    mode: ContractMode = ContractMode.AUTO
    tool_calls: list[ContractSignal] = field(default_factory=list)
    trigger_reasons: set[TriggerReason] = field(default_factory=set)
    bypassed: bool = False

    def set_mode(self, mode: ContractMode) -> None:
        """设置用户显式模式；ON 强制激活，OFF 记录本轮绕过。"""

        self.mode = mode
        if mode is ContractMode.ON:
            self.bypassed = False
            self.trigger_reasons.add(TriggerReason.MANUAL_ON)
        elif mode is ContractMode.OFF:
            self.bypassed = True

    def record_signal(self, signal: ContractSignal) -> None:
        """记录单个触发信号，并把复杂任务特征归并为触发原因。"""

        if signal.kind == "tool_call":
            self.tool_calls.append(signal)
            if len(self.tool_calls) >= TOOL_THRESHOLD:
                self.trigger_reasons.add(TriggerReason.TOOL_THRESHOLD)
            if signal.name in FILE_CHANGE_TOOLS:
                self.trigger_reasons.add(TriggerReason.FILE_CHANGE)
            if signal.name == "bash" and _looks_like_verify_command(signal.detail):
                self.trigger_reasons.add(TriggerReason.VERIFY_COMMAND)
            return

        if signal.kind == "file_change":
            self.trigger_reasons.add(TriggerReason.FILE_CHANGE)
            return

        if signal.kind == "compaction":
            self.trigger_reasons.add(TriggerReason.COMPACTION)

    def is_active(self) -> bool:
        """判断当前是否应注入契约压力；OFF 永远关闭，ON 永远开启。"""

        if self.mode is ContractMode.OFF:
            return False
        if self.mode is ContractMode.ON:
            return True
        return bool(self.trigger_reasons)

    def contracts_dir(self) -> Path:
        """返回契约文件目录；本任务只定义路径，不创建或持久化文件。"""

        return Path(self.workdir) / ".dong" / "contracts"


def _looks_like_verify_command(command: str) -> bool:
    """用保守关键词识别验证类 bash 命令。"""

    command_lower = command.lower()
    return any(keyword in command_lower for keyword in VERIFY_COMMAND_KEYWORDS)


def ensure_best_practices(workdir: str) -> Path:
    """确保契约最佳实践材料存在；已有自定义文件必须原样保留。"""

    path = Path(workdir) / BEST_PRACTICES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_BEST_PRACTICES, encoding="utf-8")
    return path


def pressure_summary(
    controller: ContractController,
    average_score: float,
    pressure_level: str,
    lesson_for_session: str = "",
) -> str:
    """生成短契约压力摘要；只在控制器激活时提醒交付风险。"""

    if not controller.is_active():
        return ""

    ensure_best_practices(controller.workdir)
    reasons = ", ".join(sorted(reason.value for reason in controller.trigger_reasons))
    if not reasons:
        reasons = "manual"

    summary = (
        "[Contract Pressure | 契约压力] "
        f"level={pressure_level}; average_score={average_score:.1f}; "
        f"trigger_reasons={reasons}; "
        "交付后会被第三方审计，低分会降低本轮声誉并要求补齐验证证据。"
    )
    if lesson_for_session:
        summary = f"{summary} session教训：{lesson_for_session}"
    return summary


__all__ = [
    "BEST_PRACTICES_RELPATH",
    "ContractController",
    "ContractMode",
    "ContractSignal",
    "DEFAULT_BEST_PRACTICES",
    "FILE_CHANGE_TOOLS",
    "TOOL_THRESHOLD",
    "TriggerReason",
    "VERIFY_COMMAND_KEYWORDS",
    "ensure_best_practices",
    "pressure_summary",
]
