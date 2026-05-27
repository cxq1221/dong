"""契约机制测试：验证复杂任务压力、签名、评分表和 scorer 结果。"""

from __future__ import annotations

from dong.contract import (
    ContractController,
    ContractMode,
    ContractSignal,
    TriggerReason,
)


def test_contract_controller_auto_triggers_for_complex_signals(tmp_path) -> None:
    """发生写文件、验证命令或工具调用阈值时，应自动进入契约压力模式。"""
    controller = ContractController(workdir=str(tmp_path))

    assert controller.is_active() is False

    controller.record_signal(ContractSignal.tool_call("read"))
    controller.record_signal(ContractSignal.tool_call("grep"))
    controller.record_signal(ContractSignal.tool_call("bash"))
    controller.record_signal(ContractSignal.tool_call("read"))
    assert controller.is_active() is False

    controller.record_signal(ContractSignal.tool_call("edit"))

    assert controller.is_active() is True
    assert TriggerReason.TOOL_THRESHOLD in controller.trigger_reasons
    assert TriggerReason.FILE_CHANGE in controller.trigger_reasons


def test_contract_controller_manual_off_records_bypass(tmp_path) -> None:
    """用户关闭契约时，本轮不注入压力，但关闭原因会留给 scorer。"""
    controller = ContractController(workdir=str(tmp_path))

    controller.set_mode(ContractMode.OFF)
    controller.record_signal(ContractSignal.file_change("write", "a.py"))

    assert controller.is_active() is False
    assert controller.mode is ContractMode.OFF
    assert controller.bypassed is True
    assert TriggerReason.FILE_CHANGE in controller.trigger_reasons


def test_contract_controller_manual_on_forces_active(tmp_path) -> None:
    """用户强制开启后，即使没有复杂信号，也应进入契约压力模式。"""
    controller = ContractController(workdir=str(tmp_path))

    controller.set_mode(ContractMode.ON)

    assert controller.is_active() is True
    assert TriggerReason.MANUAL_ON in controller.trigger_reasons


def test_contract_controller_file_change_path_triggers_file_change(tmp_path) -> None:
    """文件变更信号里的 name 可以是路径，也应触发文件变更压力。"""
    controller = ContractController(workdir=str(tmp_path))

    controller.record_signal(ContractSignal.file_change("a.py"))

    assert controller.is_active() is True
    assert TriggerReason.FILE_CHANGE in controller.trigger_reasons


def test_contract_controller_verify_bash_command_triggers_pressure(tmp_path) -> None:
    """bash 执行验证命令时，应触发验证命令压力。"""
    controller = ContractController(workdir=str(tmp_path))

    controller.record_signal(ContractSignal.tool_call("bash", "uv run pytest -q"))

    assert controller.is_active() is True
    assert TriggerReason.VERIFY_COMMAND in controller.trigger_reasons


def test_contract_controller_compaction_triggers_pressure(tmp_path) -> None:
    """发生上下文压缩时，应触发压缩压力。"""
    controller = ContractController(workdir=str(tmp_path))

    controller.record_signal(ContractSignal.compaction("compact-1.md"))

    assert controller.is_active() is True
    assert TriggerReason.COMPACTION in controller.trigger_reasons
