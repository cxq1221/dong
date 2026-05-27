"""契约机制测试：验证复杂任务压力、签名、评分表和 scorer 结果。"""

from __future__ import annotations

from dong.contract import (
    ContractController,
    ContractMode,
    ContractSignal,
    TriggerReason,
    ensure_best_practices,
    pressure_summary,
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


def test_best_practices_material_is_created_once(tmp_path) -> None:
    """契约最佳实践应落在工作区，并且已有文件不被覆盖。"""
    path = ensure_best_practices(str(tmp_path))
    original = path.read_text(encoding="utf-8")
    path.write_text("custom contract\n", encoding="utf-8")

    second = ensure_best_practices(str(tmp_path))

    assert second == path
    assert "custom contract" in second.read_text(encoding="utf-8")
    assert "交付目标" in original


def test_pressure_summary_includes_reputation_and_lesson(tmp_path) -> None:
    """提示词压力摘要应短而具体，包含声誉、触发原因和 session 教训。"""
    controller = ContractController(workdir=str(tmp_path))
    controller.set_mode(ContractMode.ON)
    controller.record_signal(ContractSignal.tool_call("edit", "a.py"))

    summary = pressure_summary(
        controller,
        average_score=61.2,
        pressure_level="watch",
        lesson_for_session="上次因为没有运行测试被扣分。",
    )

    assert "契约压力" in summary
    assert "watch" in summary
    assert "61.2" in summary
    assert "上次因为没有运行测试被扣分" in summary
    assert "第三方审计" in summary
    assert "声誉" in summary


def test_pressure_summary_handles_absent_historical_score(tmp_path) -> None:
    """没有历史分时，压力摘要应保留可读的无历史分提示。"""
    controller = ContractController(workdir=str(tmp_path))
    controller.set_mode(ContractMode.ON)

    summary = pressure_summary(
        controller,
        average_score=None,
        pressure_level="watch",
    )

    assert "契约压力" in summary
    assert "暂无历史分" in summary
