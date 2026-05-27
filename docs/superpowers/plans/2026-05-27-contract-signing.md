# Contract Signing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add dong's soft contract-pressure system: complex-task detection, best-practice injection, proof-of-work signing, third-party scorer, scoreboard, and session lessons.

**Architecture:** Add a focused `dong/contract.py` module for all contract state, evidence, signing, rule-floor scoring, scorer JSON validation, and scoreboard persistence. Keep `dong/cli.py` as the wiring layer: collect runtime signals, expose `/contract` commands, inject short pressure summaries into instructions, and trigger post-answer contract review. Persist long-term reputation under `.dong/scoreboard.json`, durable contract artifacts under `.dong/contracts/`, and per-session lessons as JSONL records in existing session files.

**Tech Stack:** Python 3.11+, standard library only (`dataclasses`, `hashlib`, `json`, `time`, `pathlib`), existing `dong.llm.chat`, existing `dong.logging_config`, pytest, ruff.

---

## File Structure

- Create: `dong/contract.py` — contract data types, trigger signals, best-practice material, evidence package, proof-of-work signing, rule-floor constraints, scorer validation, scoreboard persistence, pressure summary.
- Create: `.dong/contracts/best-practices.md` — default contract reference material for the current workspace.
- Create: `tests/test_contract.py` — unit tests for contract trigger, evidence hashing, signing, rule floor, scorer validation, scoreboard, pressure summary.
- Modify: `dong/session.py` — add generic session event persistence and load support for contract events.
- Modify: `dong/cli.py` — add contract command handling, completions, signal collection, prompt injection, post-answer review, scorer call.
- Modify: `dong/ui.py` — add small display helpers for `/contract status` and contract review status.
- Modify: `tests/test_session.py` — verify contract event roundtrip.
- Modify: `tests/test_cli_repl_commands.py` — verify `/contract on|off|status`.
- Modify: `tests/e2e/test_cli_e2e.py` — fake-LLM end-to-end contract review and lesson injection.
- Modify: `README.md` — document `/contract` commands and environment behavior.

Keep every new Python file/class/function comment in Chinese, matching the project rule.

---

### Task 1: Contract Core Models And Trigger Signals

**Files:**
- Create: `dong/contract.py`
- Test: `tests/test_contract.py`

- [ ] **Step 1: Write failing tests for trigger and control state**

Create `tests/test_contract.py` with:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_contract.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'dong.contract'`.

- [ ] **Step 3: Implement minimal contract controller**

Create `dong/contract.py`:

```python
"""dong 契约模块：负责复杂任务压力、交付证据、签名、评分和声誉账本。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


TOOL_THRESHOLD = 5
VERIFY_COMMAND_KEYWORDS = (
    "pytest",
    "ruff",
    "mypy",
    "test",
    "lint",
    "build",
    "uv run",
)
FILE_CHANGE_TOOLS = {"write", "edit"}


class ContractMode(str, Enum):
    """契约模式：auto 自动判断，on 强制开启，off 本轮关闭。"""

    AUTO = "auto"
    ON = "on"
    OFF = "off"


class TriggerReason(str, Enum):
    """契约触发原因：全部来自 dong 本地可观测信号。"""

    MANUAL_ON = "manual_on"
    FILE_CHANGE = "file_change"
    VERIFY_COMMAND = "verify_command"
    TOOL_THRESHOLD = "tool_threshold"
    COMPACTION = "compaction"


@dataclass(frozen=True)
class ContractSignal:
    """运行时信号：CLI 在工具、压缩和命令阶段上报给契约控制器。"""

    kind: str
    name: str = ""
    detail: str = ""

    @classmethod
    def tool_call(cls, name: str, detail: str = "") -> "ContractSignal":
        """记录一次工具调用；写文件和验证命令会被识别为复杂信号。"""
        return cls(kind="tool_call", name=name, detail=detail)

    @classmethod
    def file_change(cls, name: str, detail: str = "") -> "ContractSignal":
        """记录一次文件修改信号。"""
        return cls(kind="file_change", name=name, detail=detail)

    @classmethod
    def compaction(cls, detail: str = "") -> "ContractSignal":
        """记录一次上下文压缩信号。"""
        return cls(kind="compaction", detail=detail)


@dataclass
class ContractController:
    """单个任务轮次的契约控制器；只保存轻量状态和触发原因。"""

    workdir: str
    mode: ContractMode = ContractMode.AUTO
    tool_calls: list[ContractSignal] = field(default_factory=list)
    trigger_reasons: set[TriggerReason] = field(default_factory=set)
    bypassed: bool = False

    def set_mode(self, mode: ContractMode) -> None:
        """处理用户控制命令，强制开启或关闭本轮契约压力。"""
        self.mode = mode
        if mode is ContractMode.ON:
            self.trigger_reasons.add(TriggerReason.MANUAL_ON)
            self.bypassed = False
        elif mode is ContractMode.OFF:
            self.bypassed = True

    def record_signal(self, signal: ContractSignal) -> None:
        """记录运行时信号，并更新自动触发原因。"""
        if signal.kind == "tool_call":
            self.tool_calls.append(signal)
            if len(self.tool_calls) >= TOOL_THRESHOLD:
                self.trigger_reasons.add(TriggerReason.TOOL_THRESHOLD)
            if signal.name in FILE_CHANGE_TOOLS:
                self.trigger_reasons.add(TriggerReason.FILE_CHANGE)
            if signal.name == "bash" and _looks_like_verify_command(signal.detail):
                self.trigger_reasons.add(TriggerReason.VERIFY_COMMAND)
        elif signal.kind == "file_change":
            self.trigger_reasons.add(TriggerReason.FILE_CHANGE)
        elif signal.kind == "compaction":
            self.trigger_reasons.add(TriggerReason.COMPACTION)

    def is_active(self) -> bool:
        """判断本轮是否应向主 Agent 注入契约压力。"""
        if self.mode is ContractMode.OFF:
            return False
        if self.mode is ContractMode.ON:
            return True
        return bool(self.trigger_reasons)

    def contracts_dir(self) -> Path:
        """返回受工作区约束的契约材料目录。"""
        return Path(self.workdir) / ".dong" / "contracts"


def _looks_like_verify_command(command: str) -> bool:
    """用保守关键词识别测试、构建、lint 等验证命令。"""
    lowered = command.lower()
    return any(keyword in lowered for keyword in VERIFY_COMMAND_KEYWORDS)
```

- [ ] **Step 4: Run tests to verify pass**

Run: `uv run pytest tests/test_contract.py -q`

Expected: PASS, 3 tests.

- [ ] **Step 5: Commit**

```bash
git add dong/contract.py tests/test_contract.py
git commit -m "Add contract trigger controller" \
  -m "The contract system needs a local way to detect complex work without relying on model self-reporting. This adds the first small controller and tests for automatic and manual pressure modes." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: uv run pytest tests/test_contract.py -q"
```

---

### Task 2: Best Practices And Pressure Summary

**Files:**
- Create: `.dong/contracts/best-practices.md`
- Modify: `dong/contract.py`
- Test: `tests/test_contract.py`

- [ ] **Step 1: Add failing tests for best practices and prompt summary**

Append to `tests/test_contract.py`:

```python
from dong.contract import (
    ContractController,
    ContractMode,
    ContractSignal,
    TriggerReason,
    ensure_best_practices,
    pressure_summary,
)


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
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_contract.py -q`

Expected: FAIL for missing `ensure_best_practices` and `pressure_summary`.

- [ ] **Step 3: Add default material and summary functions**

Create `.dong/contracts/best-practices.md`:

```markdown
# dong 契约最佳实践

这份材料是复杂开发交付的外部参考，不是强制流程。主 Agent 可以不参考，但交付后会被第三方 scorer 审计。

## 交付原则

- 先确认用户目标、约束和不可扩大范围。
- 修改前读取相关代码、测试和项目规则。
- 修改后保留真实验证证据，包括失败命令和未验证项。
- 最终答复必须说明变更范围、验证结果、风险和下一步。
- 不要用漂亮总结替代验收材料。
- 签名前确认交付可审阅、可复现、可回滚。
```

Append to `dong/contract.py`:

```python
BEST_PRACTICES_RELPATH = ".dong/contracts/best-practices.md"
DEFAULT_BEST_PRACTICES = """# dong 契约最佳实践

这份材料是复杂开发交付的外部参考，不是强制流程。主 Agent 可以不参考，但交付后会被第三方 scorer 审计。

## 交付原则

- 先确认用户目标、约束和不可扩大范围。
- 修改前读取相关代码、测试和项目规则。
- 修改后保留真实验证证据，包括失败命令和未验证项。
- 最终答复必须说明变更范围、验证结果、风险和下一步。
- 不要用漂亮总结替代验收材料。
- 签名前确认交付可审阅、可复现、可回滚。
"""


def ensure_best_practices(workdir: str) -> Path:
    """确保工作区存在契约最佳实践材料；已有自定义文件不覆盖。"""
    path = Path(workdir) / BEST_PRACTICES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_BEST_PRACTICES, encoding="utf-8")
    return path


def pressure_summary(
    controller: ContractController,
    *,
    average_score: float | None,
    pressure_level: str,
    lesson_for_session: str = "",
) -> str:
    """生成注入系统提示词的短契约压力摘要，避免塞入完整历史。"""
    if not controller.is_active():
        return ""
    ensure_best_practices(controller.workdir)
    score_text = "暂无历史分" if average_score is None else f"{average_score:.1f}"
    reasons = ", ".join(sorted(reason.value for reason in controller.trigger_reasons))
    lesson_line = f"\n- 本 session 教训：{lesson_for_session}" if lesson_for_session else ""
    return (
        "\n--- Contract Pressure ---\n"
        f"- 当前压力等级：{pressure_level}\n"
        f"- 当前平均分：{score_text}\n"
        f"- 本轮触发原因：{reasons or 'manual'}\n"
        "- 你可以不参考契约最佳实践，但交付后会被第三方审计。\n"
        "- 低分会提高后续签名难度和交付证据门槛。"
        f"{lesson_line}\n"
        "--- End Contract Pressure ---"
    )
```

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add .dong/contracts/best-practices.md dong/contract.py tests/test_contract.py
git commit -m "Add contract pressure material" \
  -m "Complex tasks need an external best-practice packet and short pressure summary rather than a rigid internal workflow. The material is created once and can be customized per workspace." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: uv run pytest tests/test_contract.py -q"
```

---

### Task 3: Evidence Package And Proof-Of-Work Signature

**Files:**
- Modify: `dong/contract.py`
- Test: `tests/test_contract.py`

- [ ] **Step 1: Add failing evidence and signature tests**

Append to `tests/test_contract.py`:

```python
from dong.contract import (
    ContractEvidence,
    ContractSignature,
    build_evidence_hash,
    sign_evidence,
    verify_signature,
)


def test_evidence_hash_is_stable_for_same_payload() -> None:
    """证据包 hash 应使用规范化 JSON，字段顺序不能影响结果。"""
    evidence = ContractEvidence(
        contract_version=1,
        session_id="session-1",
        trigger_reasons=["file_change"],
        user_objective="修改 README",
        tool_summary=[{"name": "edit", "success": True}],
        file_changes=[{"path": "README.md", "operation": "edit"}],
        verification_evidence=[{"command": "uv run pytest tests/test_contract.py -q", "success": True}],
        final_answer="已完成",
        known_risks=[],
        unverified_items=[],
    )

    assert build_evidence_hash(evidence) == build_evidence_hash(evidence)


def test_sign_evidence_produces_verifiable_hash() -> None:
    """签名应有真实 nonce、耗时字段，并能用相同证据校验。"""
    evidence = ContractEvidence(
        contract_version=1,
        session_id="session-1",
        trigger_reasons=["manual_on"],
        user_objective="小任务",
        tool_summary=[],
        file_changes=[],
        verification_evidence=[],
        final_answer="完成",
        known_risks=[],
        unverified_items=["未运行测试"],
    )

    signature = sign_evidence(evidence, difficulty=1, max_attempts=100_000)

    assert isinstance(signature, ContractSignature)
    assert signature.difficulty == 1
    assert signature.elapsed_ms >= 0
    assert signature.signature_hash.startswith("0")
    assert verify_signature(evidence, signature) is True
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_contract.py -q`

Expected: FAIL for missing evidence/signature names.

- [ ] **Step 3: Implement evidence models and proof-of-work**

Append to `dong/contract.py`:

```python
import hashlib
import json
import time
from dataclasses import asdict


CONTRACT_VERSION = 1


@dataclass(frozen=True)
class ContractSignature:
    """契约签名结果：记录证据 hash、nonce、难度、耗时和最终 hash。"""

    evidence_hash: str
    nonce: int
    difficulty: int
    elapsed_ms: int
    signature_hash: str


@dataclass(frozen=True)
class ContractEvidence:
    """本次交付证据包：供签名、规则底座和第三方 scorer 审计。"""

    contract_version: int
    session_id: str
    trigger_reasons: list[str]
    user_objective: str
    tool_summary: list[dict]
    file_changes: list[dict]
    verification_evidence: list[dict]
    final_answer: str
    known_risks: list[str]
    unverified_items: list[str]
    signature: dict | None = None
    scorer_result: dict | None = None

    def to_dict(self) -> dict:
        """转换为稳定 JSON 字典，方便落盘和 hash。"""
        return asdict(self)


def build_evidence_hash(evidence: ContractEvidence) -> str:
    """对证据包做规范化 hash；签名前不把签名和 scorer 结果计入。"""
    payload = evidence.to_dict()
    payload["signature"] = None
    payload["scorer_result"] = None
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sign_evidence(
    evidence: ContractEvidence,
    *,
    difficulty: int,
    max_attempts: int = 5_000_000,
) -> ContractSignature:
    """执行本地 proof-of-work 签名；低声誉可传入更高 difficulty。"""
    if difficulty < 0:
        raise ValueError("difficulty must be >= 0")
    evidence_hash = build_evidence_hash(evidence)
    prefix = "0" * difficulty
    start = time.monotonic()
    for nonce in range(max_attempts):
        signature_hash = _signature_hash(
            session_id=evidence.session_id,
            evidence_hash=evidence_hash,
            contract_version=evidence.contract_version,
            nonce=nonce,
            difficulty=difficulty,
        )
        if signature_hash.startswith(prefix):
            elapsed_ms = int((time.monotonic() - start) * 1000)
            return ContractSignature(
                evidence_hash=evidence_hash,
                nonce=nonce,
                difficulty=difficulty,
                elapsed_ms=elapsed_ms,
                signature_hash=signature_hash,
            )
    raise TimeoutError("contract signature difficulty not satisfied")


def verify_signature(evidence: ContractEvidence, signature: ContractSignature) -> bool:
    """校验证据包签名是否匹配当前证据内容和难度。"""
    if build_evidence_hash(evidence) != signature.evidence_hash:
        return False
    expected = _signature_hash(
        session_id=evidence.session_id,
        evidence_hash=signature.evidence_hash,
        contract_version=evidence.contract_version,
        nonce=signature.nonce,
        difficulty=signature.difficulty,
    )
    return (
        expected == signature.signature_hash
        and expected.startswith("0" * signature.difficulty)
    )


def _signature_hash(
    *,
    session_id: str,
    evidence_hash: str,
    contract_version: int,
    nonce: int,
    difficulty: int,
) -> str:
    """生成签名候选 hash，输入字段固定，便于测试和审计。"""
    payload = {
        "session_id": session_id,
        "evidence_hash": evidence_hash,
        "contract_version": contract_version,
        "nonce": nonce,
        "difficulty": difficulty,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
```

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dong/contract.py tests/test_contract.py
git commit -m "Add contract evidence signing" \
  -m "The contract needs a visible cost before review. This adds stable evidence hashing and a local proof-of-work signature that scorer can verify." \
  -m "Confidence: high" \
  -m "Scope-risk: narrow" \
  -m "Tested: uv run pytest tests/test_contract.py -q"
```

---

### Task 4: Rule Floor, Scorer Result, And Scoreboard

**Files:**
- Modify: `dong/contract.py`
- Test: `tests/test_contract.py`

- [ ] **Step 1: Add failing tests for rule floor and scoreboard**

Append to `tests/test_contract.py`:

```python
from dong.contract import (
    RuleFloor,
    Scoreboard,
    ScorerResult,
    apply_score,
    build_rule_floor,
    load_scoreboard,
    validate_scorer_result,
)


def test_rule_floor_caps_score_when_code_changed_without_verification() -> None:
    """修改代码但没有验证证据时，规则底座必须限制最高分。"""
    evidence = ContractEvidence(
        contract_version=1,
        session_id="session-1",
        trigger_reasons=["file_change"],
        user_objective="改代码",
        tool_summary=[{"name": "edit", "success": True}],
        file_changes=[{"path": "a.py", "operation": "edit"}],
        verification_evidence=[],
        final_answer="完成",
        known_risks=[],
        unverified_items=[],
    )

    floor = build_rule_floor(evidence, signature_valid=False)

    assert isinstance(floor, RuleFloor)
    assert floor.base_score_ceiling <= 60
    assert "missing_verification" in floor.evidence_gaps
    assert "invalid_signature" in floor.required_deductions


def test_validate_scorer_result_rejects_score_above_rule_ceiling() -> None:
    """scorer 不能突破规则底座给无证据交付高分。"""
    floor = RuleFloor(
        base_score_ceiling=70,
        required_deductions=["missing_verification"],
        evidence_gaps=["missing_verification"],
        signature_valid=True,
    )

    result = validate_scorer_result(
        {
            "score": 95,
            "deductions": [],
            "risk_flags": [],
            "lesson_for_session": "下次先跑测试。",
            "workspace_summary": "缺少验证",
        },
        floor,
    )

    assert isinstance(result, ScorerResult)
    assert result.score == 70
    assert "missing_verification" in result.deductions


def test_scoreboard_updates_average_and_pressure(tmp_path) -> None:
    """评分表应跨 session 维护平均分、压力等级和常见扣分原因。"""
    scoreboard = load_scoreboard(str(tmp_path))
    result = ScorerResult(
        score=58,
        deductions=["missing_verification"],
        risk_flags=["unstable_delivery"],
        lesson_for_session="先跑测试。",
        workspace_summary="缺少验证",
    )

    updated = apply_score(str(tmp_path), scoreboard, "session-1", result)

    assert isinstance(updated, Scoreboard)
    assert updated.average_score == 58
    assert updated.pressure_level == "watch"
    assert updated.common_deductions["missing_verification"] == 1
    assert load_scoreboard(str(tmp_path)).average_score == 58
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_contract.py -q`

Expected: FAIL for missing rule/scoreboard names.

- [ ] **Step 3: Implement rule floor, scorer validation, scoreboard**

Append to `dong/contract.py`:

```python
SCOREBOARD_RELPATH = ".dong/scoreboard.json"


@dataclass(frozen=True)
class RuleFloor:
    """规则底座：约束 scorer 分数，防止无证据高分。"""

    base_score_ceiling: int
    required_deductions: list[str]
    evidence_gaps: list[str]
    signature_valid: bool


@dataclass(frozen=True)
class ScorerResult:
    """第三方 scorer 输出：最终分、扣分项、风险、session 教训和长期摘要。"""

    score: int
    deductions: list[str]
    risk_flags: list[str]
    lesson_for_session: str
    workspace_summary: str


@dataclass(frozen=True)
class Scoreboard:
    """workspace 声誉账本：记录平均分、压力等级、近期分数和常见扣分。"""

    version: int
    average_score: float | None
    recent_scores: list[int]
    pressure_level: str
    common_deductions: dict[str, int]
    sessions: dict[str, dict]

    def to_dict(self) -> dict:
        """转换为 JSON 可写结构。"""
        return asdict(self)


def build_rule_floor(
    evidence: ContractEvidence,
    *,
    signature_valid: bool,
) -> RuleFloor:
    """根据本地证据生成 scorer 必须遵守的评分约束。"""
    ceiling = 100
    deductions: list[str] = []
    gaps: list[str] = []
    if evidence.file_changes and not evidence.verification_evidence:
        ceiling = min(ceiling, 60)
        gaps.append("missing_verification")
        deductions.append("missing_verification")
    if evidence.file_changes and not evidence.known_risks and not evidence.unverified_items:
        ceiling = min(ceiling, 85)
        gaps.append("missing_risk_disclosure")
        deductions.append("missing_risk_disclosure")
    if not signature_valid:
        ceiling = min(ceiling, 50)
        deductions.append("invalid_signature")
    failed_tools = [
        item for item in evidence.tool_summary if item.get("success") is False
    ]
    if failed_tools and "失败" not in evidence.final_answer and "failed" not in evidence.final_answer.lower():
        ceiling = min(ceiling, 70)
        deductions.append("undisclosed_failure")
    return RuleFloor(
        base_score_ceiling=ceiling,
        required_deductions=sorted(set(deductions)),
        evidence_gaps=sorted(set(gaps)),
        signature_valid=signature_valid,
    )


def validate_scorer_result(raw: dict, rule_floor: RuleFloor) -> ScorerResult:
    """校验 scorer JSON，并把分数和扣分项收敛到规则底座内。"""
    score = int(raw.get("score", 0))
    deductions = [str(item) for item in raw.get("deductions", [])]
    for deduction in rule_floor.required_deductions:
        if deduction not in deductions:
            deductions.append(deduction)
    score = max(0, min(score, rule_floor.base_score_ceiling, 100))
    return ScorerResult(
        score=score,
        deductions=deductions,
        risk_flags=[str(item) for item in raw.get("risk_flags", [])],
        lesson_for_session=str(raw.get("lesson_for_session") or "").strip(),
        workspace_summary=str(raw.get("workspace_summary") or "").strip(),
    )


def load_scoreboard(workdir: str) -> Scoreboard:
    """读取 workspace 声誉账本；不存在时返回空账本。"""
    path = Path(workdir) / SCOREBOARD_RELPATH
    if not path.exists():
        return Scoreboard(
            version=1,
            average_score=None,
            recent_scores=[],
            pressure_level="normal",
            common_deductions={},
            sessions={},
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return Scoreboard(
        version=int(data.get("version", 1)),
        average_score=data.get("average_score"),
        recent_scores=[int(item) for item in data.get("recent_scores", [])],
        pressure_level=str(data.get("pressure_level", "normal")),
        common_deductions={
            str(key): int(value)
            for key, value in data.get("common_deductions", {}).items()
        },
        sessions=dict(data.get("sessions", {})),
    )


def apply_score(
    workdir: str,
    scoreboard: Scoreboard,
    session_id: str,
    result: ScorerResult,
) -> Scoreboard:
    """把 scorer 结果写入声誉账本并重新计算平均分和压力等级。"""
    recent_scores = [*scoreboard.recent_scores, result.score][-20:]
    average = sum(recent_scores) / len(recent_scores)
    deductions = dict(scoreboard.common_deductions)
    for deduction in result.deductions:
        deductions[deduction] = deductions.get(deduction, 0) + 1
    sessions = dict(scoreboard.sessions)
    sessions[session_id] = {
        "score": result.score,
        "deductions": result.deductions,
        "risk_flags": result.risk_flags,
        "workspace_summary": result.workspace_summary,
    }
    updated = Scoreboard(
        version=1,
        average_score=round(average, 2),
        recent_scores=recent_scores,
        pressure_level=_pressure_level(average),
        common_deductions=deductions,
        sessions=sessions,
    )
    path = Path(workdir) / SCOREBOARD_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(updated.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return updated


def _pressure_level(average_score: float) -> str:
    """把平均分映射为提示词压力等级。"""
    if average_score < 50:
        return "probation"
    if average_score < 75:
        return "watch"
    return "normal"
```

- [ ] **Step 4: Run focused tests**

Run: `uv run pytest tests/test_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dong/contract.py tests/test_contract.py
git commit -m "Add contract scoring ledger" \
  -m "A third-party scorer needs deterministic local constraints and a persistent reputation ledger. This adds the rule floor, scorer JSON validation, and workspace scoreboard." \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_contract.py -q"
```

---

### Task 5: Session Contract Events

**Files:**
- Modify: `dong/session.py`
- Modify: `tests/test_session.py`

- [ ] **Step 1: Add failing session event roundtrip test**

Append to `tests/test_session.py`:

```python
def test_session_records_contract_events(tmp_path) -> None:
    """契约事件应写入 JSONL 并在恢复 session 时保留。"""
    session = SessionStore(str(tmp_path)).create()

    session.record_event("contract_lesson", {"lesson_for_session": "先跑测试"})

    loaded = SessionStore(str(tmp_path)).load(session.session_id)
    records = _records(session.persistence_path)

    assert loaded.events[0]["type"] == "contract_lesson"
    assert loaded.events[0]["lesson_for_session"] == "先跑测试"
    assert any(record["type"] == "contract_lesson" for record in records)
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_session.py::test_session_records_contract_events -q`

Expected: FAIL because `Session` has no `record_event` or `events`.

- [ ] **Step 3: Add session event persistence**

Modify `dong/session.py`:

```python
@dataclass
class Session:
    """单个 dong 会话；messages 是运行时上下文，JSONL 是可恢复快照。"""

    session_id: str
    created_at_ms: int
    updated_at_ms: int
    workspace_root: str
    messages: list[Any] = field(default_factory=list)
    prompt_history: list[dict[str, Any]] = field(default_factory=list)
    compactions: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    model: str | None = None
    persistence_path: Path | None = None
```

Add method inside `Session`:

```python
    def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """记录 session 级业务事件；契约 lesson 和评分结果走这个通道。"""
        if not event_type or event_type == "session_meta":
            raise SessionError(f"Invalid session event type: {event_type!r}")
        entry = {
            "type": event_type,
            "timestamp_ms": _now_ms(),
            **_sanitize_for_json(payload),
        }
        self.events.append(entry)
        self.updated_at_ms = int(entry["timestamp_ms"])
        self._append_jsonl_record(entry)
```

Update `_snapshot_records`:

```python
        records.extend(
            _sanitize_for_json(item)
            for item in self.events
        )
```

Update `load_from_path` local variables and loop:

```python
        events: list[dict[str, Any]] = []
```

```python
                elif record_type == "compaction":
                    compactions.append(_record_payload(record))
                elif isinstance(record_type, str) and record_type.startswith("contract_"):
                    events.append(record)
```

Include `events=events` in the `Session(...)` constructor.

- [ ] **Step 4: Run session tests**

Run: `uv run pytest tests/test_session.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add dong/session.py tests/test_session.py
git commit -m "Persist contract session events" \
  -m "Contract scoring has to teach the same session after review. This adds generic contract event persistence to the existing JSONL session store." \
  -m "Confidence: high" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_session.py -q"
```

---

### Task 6: CLI Contract Commands And Status UI

**Files:**
- Modify: `dong/cli.py`
- Modify: `dong/ui.py`
- Modify: `tests/test_cli_repl_commands.py`

- [ ] **Step 1: Add failing REPL command tests**

Append to `tests/test_cli_repl_commands.py`:

```python
from dong.contract import ContractController


def test_contract_commands_update_controller(tmp_path) -> None:
    """`/contract on|off|status` 应控制当前契约压力层。"""
    ui, err = _ui()
    controller = ContractController(workdir=str(tmp_path))

    on = handle_repl_command(
        "/contract on",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
        contract_controller=controller,
    )
    status = handle_repl_command(
        "/contract status",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
        contract_controller=controller,
    )
    off = handle_repl_command(
        "/contract off",
        workdir=str(tmp_path),
        loaded_skills=[],
        working=[],
        ui=ui,
        contract_controller=controller,
    )

    assert on.handled is True
    assert status.handled is True
    assert off.handled is True
    rendered = err.getvalue()
    assert "contract mode: on" in rendered
    assert "pressure level" in rendered
    assert "contract mode: off" in rendered


def test_repl_completions_include_contract_commands(tmp_path) -> None:
    """REPL 补全应暴露契约命令。"""
    completions = repl_completions(str(tmp_path), [])

    assert "/contract" in completions
    assert "/contract on" in completions
    assert "/contract off" in completions
    assert "/contract status" in completions
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_cli_repl_commands.py::test_contract_commands_update_controller tests/test_cli_repl_commands.py::test_repl_completions_include_contract_commands -q`

Expected: FAIL because `handle_repl_command` has no `contract_controller` parameter and completions lack commands.

- [ ] **Step 3: Add UI helpers**

Modify `dong/ui.py` inside `TerminalUI`:

```python
    def show_contract_status(
        self,
        *,
        mode: str,
        active: bool,
        pressure_level: str,
        average_score: float | None,
        trigger_reasons: list[str],
        lesson: str = "",
    ) -> None:
        """展示契约状态；只输出摘要，不展示完整评分表。"""
        score = "none" if average_score is None else f"{average_score:.1f}"
        self.err_console.print(f"contract mode: {mode}")
        self.err_console.print(f"active: {active}")
        self.err_console.print(f"pressure level: {pressure_level}")
        self.err_console.print(f"average score: {score}")
        if trigger_reasons:
            self.err_console.print(f"trigger reasons: {', '.join(trigger_reasons)}")
        if lesson:
            self.err_console.print(f"lesson: {lesson}")
```

- [ ] **Step 4: Wire commands in CLI**

Modify imports in `dong/cli.py`:

```python
from dong.contract import (
    ContractController,
    ContractMode,
    load_scoreboard,
)
```

Update `repl_completions` command list:

```python
        "/contract",
        "/contract on",
        "/contract off",
        "/contract status",
```

Update `handle_repl_command` signature:

```python
    contract_controller: ContractController | None = None,
) -> ReplAction:
```

Add before skill handling:

```python
    if inp == "/contract" or inp.startswith("/contract "):
        controller = contract_controller or ContractController(workdir=workdir)
        parts = inp.split(maxsplit=1)
        subcommand = parts[1].strip() if len(parts) > 1 else "status"
        if subcommand == "on":
            controller.set_mode(ContractMode.ON)
            ui.show_contract_status(
                mode=controller.mode.value,
                active=controller.is_active(),
                pressure_level=load_scoreboard(workdir).pressure_level,
                average_score=load_scoreboard(workdir).average_score,
                trigger_reasons=sorted(reason.value for reason in controller.trigger_reasons),
            )
            log_event(LOGGER, logging.INFO, "contract_manual_on")
            return ReplAction(handled=True)
        if subcommand == "off":
            controller.set_mode(ContractMode.OFF)
            ui.show_contract_status(
                mode=controller.mode.value,
                active=controller.is_active(),
                pressure_level=load_scoreboard(workdir).pressure_level,
                average_score=load_scoreboard(workdir).average_score,
                trigger_reasons=sorted(reason.value for reason in controller.trigger_reasons),
            )
            log_event(LOGGER, logging.INFO, "contract_manual_off")
            return ReplAction(handled=True)
        if subcommand == "status":
            scoreboard = load_scoreboard(workdir)
            ui.show_contract_status(
                mode=controller.mode.value,
                active=controller.is_active(),
                pressure_level=scoreboard.pressure_level,
                average_score=scoreboard.average_score,
                trigger_reasons=sorted(reason.value for reason in controller.trigger_reasons),
            )
            log_event(LOGGER, logging.INFO, "contract_status_shown")
            return ReplAction(handled=True)
        ui.show_error("Usage: /contract [on|off|status]")
        return ReplAction(handled=True)
```

- [ ] **Step 5: Thread controller through REPL processing**

Update `_process_repl_input`, `_run_repl_sync`, and `_run_repl_with_input_queue` to create/pass a `ContractController`. Use a default when tests do not pass one:

```python
contract_controller = contract_controller or ContractController(workdir=workdir)
```

Ensure existing tests that monkeypatch `run_loop` still work by keeping the new argument optional.

- [ ] **Step 6: Run focused tests**

Run: `uv run pytest tests/test_cli_repl_commands.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add dong/cli.py dong/ui.py tests/test_cli_repl_commands.py
git commit -m "Add contract REPL controls" \
  -m "Users need to force or bypass the soft contract pressure layer. This adds /contract commands, completions, and a terse status display." \
  -m "Confidence: medium" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/test_cli_repl_commands.py -q"
```

---

### Task 7: Prompt Injection And Runtime Signal Collection

**Files:**
- Modify: `dong/cli.py`
- Test: `tests/e2e/test_cli_e2e.py`

- [ ] **Step 1: Add failing tests for pressure injection**

Append to `tests/e2e/test_cli_e2e.py`:

```python
def test_contract_pressure_injected_after_complex_signal(tmp_path, monkeypatch) -> None:
    """复杂信号触发后，下一轮模型请求应看到契约压力摘要。"""
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "edit", '{"filepath": "a.py", "old": "x", "new": "y"}')
        ]),
        _assistant_message(content="done"),
    ])
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    seen_instructions: list[str] = []

    def fake_chat(_messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        seen_instructions.append(instructions)
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)

    cli.run_loop(
        cli.build_agent_prompt([], str(tmp_path)),
        [{"role": "user", "content": "edit file"}],
        str(tmp_path),
        max_turns=3,
    )

    assert "Contract Pressure" not in seen_instructions[0]
    assert "Contract Pressure" in seen_instructions[1]
    assert (tmp_path / ".dong" / "contracts" / "best-practices.md").exists()
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/e2e/test_cli_e2e.py::test_contract_pressure_injected_after_complex_signal -q`

Expected: FAIL because no contract pressure is injected.

- [ ] **Step 3: Wire controller into run_loop**

Modify `run_loop` signature:

```python
    contract_controller: ContractController | None = None,
```

At start:

```python
    contract_controller = contract_controller or ContractController(workdir=workdir)
```

Before `_instructions_for_turn_skills`, load scoreboard and append pressure:

```python
            scoreboard = load_scoreboard(workdir)
            contract_pressure = pressure_summary(
                contract_controller,
                average_score=scoreboard.average_score,
                pressure_level=scoreboard.pressure_level,
                lesson_for_session=_latest_contract_lesson(session),
            )
            turn_instructions = (
                _instructions_for_turn_skills(
                    agent_prompt.instructions,
                    workdir=workdir,
                    turn_skills=turn_skills,
                )
                + contract_pressure
            )
            if contract_pressure:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "contract_pressure_injected",
                    reasons=sorted(reason.value for reason in contract_controller.trigger_reasons),
                )
```

Add helper in `dong/cli.py`:

```python
def _latest_contract_lesson(session: Session | None) -> str:
    """读取当前 session 最近的 contract_lesson，用于后续轮次注入。"""
    if session is None:
        return ""
    for event in reversed(getattr(session, "events", [])):
        if event.get("type") == "contract_lesson":
            return str(event.get("lesson_for_session") or "")
    return ""
```

Record tool signals just after `name` and `args_raw` are known:

```python
                contract_controller.record_signal(
                    ContractSignal.tool_call(name, args_raw)
                )
                if name in {"write", "edit"}:
                    log_event(
                        LOGGER,
                        logging.INFO,
                        "contract_triggered",
                        tool=name,
                        reasons=sorted(reason.value for reason in contract_controller.trigger_reasons),
                    )
```

Record compaction signal when `_apply_compaction_result` gets a compacted result in run_loop:

```python
            if compaction.compacted:
                contract_controller.record_signal(ContractSignal.compaction(compaction.summary_ref or ""))
```

- [ ] **Step 4: Update callers**

Update `_process_repl_input`, single prompt mode in `main`, and REPL workers to pass the current `contract_controller` into `run_loop`. Keep all new parameters optional so older tests continue to run.

- [ ] **Step 5: Run focused tests**

Run: `uv run pytest tests/e2e/test_cli_e2e.py::test_contract_pressure_injected_after_complex_signal -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dong/cli.py tests/e2e/test_cli_e2e.py
git commit -m "Inject contract pressure during complex work" \
  -m "The main agent should feel contract pressure once local signals show complex development work. This wires trigger collection and short prompt injection into the existing run loop." \
  -m "Confidence: medium" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/e2e/test_cli_e2e.py::test_contract_pressure_injected_after_complex_signal -q"
```

---

### Task 8: Post-Answer Evidence, Signing, And Contract Artifact

**Files:**
- Modify: `dong/contract.py`
- Modify: `dong/cli.py`
- Test: `tests/e2e/test_cli_e2e.py`

- [ ] **Step 1: Add failing artifact test**

Append to `tests/e2e/test_cli_e2e.py`:

```python
def test_contract_artifact_created_after_complex_final_answer(tmp_path, monkeypatch) -> None:
    """复杂任务最终答复后，应生成带签名的契约证据包。"""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "edit", '{"filepath": "a.py", "old": "x", "new": "y"}')
        ]),
        _assistant_message(content="已修改 a.py；未运行测试。"),
    ])

    monkeypatch.setattr(
        cli,
        "chat",
        lambda _messages, _tools, instructions="", **_kwargs: next(responses),
    )

    cli.run_loop(
        cli.build_agent_prompt([], str(tmp_path)),
        [{"role": "user", "content": "edit file"}],
        str(tmp_path),
        max_turns=3,
    )

    artifacts = list((tmp_path / ".dong" / "contracts").glob("session-*.json"))
    assert len(artifacts) == 1
    payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert payload["signature"]["signature_hash"].startswith("0")
    assert payload["file_changes"]
    assert payload["unverified_items"]
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/e2e/test_cli_e2e.py::test_contract_artifact_created_after_complex_final_answer -q`

Expected: FAIL because no artifact is created.

- [ ] **Step 3: Add artifact writer**

Append to `dong/contract.py`:

```python
def write_contract_artifact(
    workdir: str,
    evidence: ContractEvidence,
    signature: ContractSignature,
) -> Path:
    """把证据包和签名写入 .dong/contracts，供 scorer 和人工审计。"""
    contracts_dir = Path(workdir) / ".dong" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    payload = evidence.to_dict()
    payload["signature"] = asdict(signature)
    path = contracts_dir / f"{evidence.session_id}-{int(time.time() * 1000)}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path
```

Add evidence builder helpers in `dong/cli.py`:

```python
def _contract_session_id(session: Session | None) -> str:
    """返回契约 artifact 使用的 session id；无 session 时使用临时 id。"""
    return session.session_id if session is not None else f"session-{int(time.time() * 1000)}"


def _build_contract_evidence(
    *,
    session: Session | None,
    controller: ContractController,
    user_objective: str,
    final_answer: str,
) -> ContractEvidence:
    """从当前上下文和本地信号生成首版证据包。"""
    tool_summary = [
        {"name": signal.name, "detail_chars": len(signal.detail)}
        for signal in controller.tool_calls
    ]
    file_changes = [
        {"operation": signal.name, "detail_chars": len(signal.detail)}
        for signal in controller.tool_calls
        if signal.name in {"write", "edit"}
    ]
    verification = [
        {"command": signal.detail, "success": True}
        for signal in controller.tool_calls
        if signal.name == "bash" and _looks_like_contract_verification(signal.detail)
    ]
    unverified = []
    if file_changes and not verification:
        unverified.append("代码修改后未观察到验证命令")
    return ContractEvidence(
        contract_version=CONTRACT_VERSION,
        session_id=_contract_session_id(session),
        trigger_reasons=sorted(reason.value for reason in controller.trigger_reasons),
        user_objective=user_objective,
        tool_summary=tool_summary,
        file_changes=file_changes,
        verification_evidence=verification,
        final_answer=final_answer,
        known_risks=[],
        unverified_items=unverified,
    )


def _looks_like_contract_verification(command: str) -> bool:
    """CLI 侧复用保守关键词识别验证命令，避免暴露 contract 内部常量。"""
    lowered = command.lower()
    return any(item in lowered for item in ("pytest", "ruff", "test", "lint", "build"))
```

Import needed names:

```python
from dong.contract import (
    CONTRACT_VERSION,
    ContractEvidence,
    ContractSignature,
    sign_evidence,
    write_contract_artifact,
)
```

- [ ] **Step 4: Trigger post-answer review artifact**

Inside final answer branch in `run_loop`, after `run_loop_finished` log and before `return`:

```python
                if contract_controller.is_active():
                    evidence = _build_contract_evidence(
                        session=session,
                        controller=contract_controller,
                        user_objective=_last_user_prompt(working),
                        final_answer=msg.content,
                    )
                    log_event(LOGGER, logging.INFO, "contract_signature_started")
                    signature = sign_evidence(evidence, difficulty=1)
                    artifact_path = write_contract_artifact(workdir, evidence, signature)
                    if session is not None:
                        session.record_event(
                            "contract_signed",
                            {
                                "artifact_path": str(artifact_path),
                                "signature_hash": signature.signature_hash,
                                "difficulty": signature.difficulty,
                            },
                        )
                    log_event(
                        LOGGER,
                        logging.INFO,
                        "contract_signature_finished",
                        artifact_path=str(artifact_path),
                        elapsed_ms=signature.elapsed_ms,
                    )
```

Add helper:

```python
def _last_user_prompt(messages: list) -> str:
    """从上下文中取最近用户输入，作为契约证据目标摘要。"""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content") or "")
    return ""
```

- [ ] **Step 5: Run focused test**

Run: `uv run pytest tests/e2e/test_cli_e2e.py::test_contract_artifact_created_after_complex_final_answer -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dong/contract.py dong/cli.py tests/e2e/test_cli_e2e.py
git commit -m "Write signed contract artifacts" \
  -m "Complex task pressure needs a durable signed evidence bundle after final answer. This adds artifact writing and session signed events." \
  -m "Confidence: medium" \
  -m "Scope-risk: moderate" \
  -m "Tested: uv run pytest tests/e2e/test_cli_e2e.py::test_contract_artifact_created_after_complex_final_answer -q"
```

---

### Task 9: Third-Party Scorer And Session Lesson

**Files:**
- Modify: `dong/contract.py`
- Modify: `dong/cli.py`
- Test: `tests/e2e/test_cli_e2e.py`

- [ ] **Step 1: Add failing scorer e2e test**

Append to `tests/e2e/test_cli_e2e.py`:

```python
def test_contract_scorer_updates_scoreboard_and_session_lesson(tmp_path, monkeypatch) -> None:
    """第三方 scorer 结果应更新评分表，并让同 session 后续轮次看到教训。"""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    responses = iter([
        _assistant_message(tool_calls=[
            _tool_call("call-1", "edit", '{"filepath": "a.py", "old": "x", "new": "y"}')
        ]),
        _assistant_message(content="已修改 a.py。"),
        _assistant_message(content='{"score": 58, "deductions": ["missing_verification"], "risk_flags": ["unstable_delivery"], "lesson_for_session": "后续修改必须先运行相关测试。", "workspace_summary": "缺少验证"}'),
        _assistant_message(content="下一轮我会先补测试。"),
    ])
    seen_instructions: list[str] = []

    def fake_chat(_messages, _tools, instructions="", **_kwargs):  # type: ignore[no-untyped-def]
        seen_instructions.append(instructions)
        return next(responses)

    monkeypatch.setattr(cli, "chat", fake_chat)
    monkeypatch.setattr(sys, "argv", ["dong", "-d", str(tmp_path), "edit file"])
    cli.main()
    monkeypatch.setattr(sys, "argv", ["dong", "-d", str(tmp_path), "--resume", "latest", "continue"])
    cli.main()

    scoreboard = json.loads((tmp_path / ".dong" / "scoreboard.json").read_text(encoding="utf-8"))
    assert scoreboard["average_score"] == 58
    assert scoreboard["pressure_level"] == "watch"
    assert "后续修改必须先运行相关测试" in seen_instructions[-1]
```

Ensure imports at top of `tests/e2e/test_cli_e2e.py` include `json` and `sys` already exists; `json` may need adding.

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/e2e/test_cli_e2e.py::test_contract_scorer_updates_scoreboard_and_session_lesson -q`

Expected: FAIL because scorer is not called.

- [ ] **Step 3: Add scorer prompt builder**

Append to `dong/contract.py`:

```python
def scorer_instructions() -> str:
    """生成第三方 scorer 系统提示词；要求 JSON 输出并禁止主 Agent 自评。"""
    return (
        "你是 dong 的第三方交付评分 Agent。"
        "你只根据证据包、契约最佳实践和规则底座评分。"
        "主 Agent 不能自评，你也不能因为总结写得好就给高分。"
        "只输出一个 JSON object，字段必须是 score, deductions, risk_flags, "
        "lesson_for_session, workspace_summary。"
    )


def scorer_user_payload(
    *,
    best_practices: str,
    evidence: ContractEvidence,
    rule_floor: RuleFloor,
    scoreboard: Scoreboard,
) -> str:
    """生成 scorer 用户输入，包含证据、规则底座和长期声誉摘要。"""
    payload = {
        "best_practices": best_practices,
        "evidence": evidence.to_dict(),
        "rule_floor": asdict(rule_floor),
        "scoreboard": scoreboard.to_dict(),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
```

- [ ] **Step 4: Add scorer call in CLI**

In `dong/cli.py`, import:

```python
import json
from dataclasses import asdict
from dong.contract import (
    apply_score,
    build_rule_floor,
    ensure_best_practices,
    scorer_instructions,
    scorer_user_payload,
    validate_scorer_result,
    verify_signature,
)
```

Add helper:

```python
def _run_contract_scorer(
    *,
    workdir: str,
    evidence: ContractEvidence,
    signature: ContractSignature,
) -> ScorerResult:
    """调用第三方 scorer，并用规则底座收敛结果。"""
    best_practices_path = ensure_best_practices(workdir)
    scoreboard = load_scoreboard(workdir)
    signature_valid = verify_signature(evidence, signature)
    rule_floor = build_rule_floor(evidence, signature_valid=signature_valid)
    log_event(
        LOGGER,
        logging.INFO,
        "contract_rule_floor_created",
        ceiling=rule_floor.base_score_ceiling,
        signature_valid=rule_floor.signature_valid,
    )
    message = chat(
        [{"role": "user", "content": scorer_user_payload(
            best_practices=best_practices_path.read_text(encoding="utf-8"),
            evidence=evidence,
            rule_floor=rule_floor,
            scoreboard=scoreboard,
        )}],
        [],
        instructions=scorer_instructions(),
    )
    raw = json.loads(message.content or "{}")
    return validate_scorer_result(raw, rule_floor)
```

After artifact writing in final-answer branch:

```python
                    try:
                        log_event(LOGGER, logging.INFO, "contract_scorer_started")
                        scorer_result = _run_contract_scorer(
                            workdir=workdir,
                            evidence=evidence,
                            signature=signature,
                        )
                        scoreboard = apply_score(
                            workdir,
                            load_scoreboard(workdir),
                            evidence.session_id,
                            scorer_result,
                        )
                        if session is not None:
                            session.record_event(
                                "contract_scored",
                                {
                                    "score": scorer_result.score,
                                    "deductions": scorer_result.deductions,
                                    "risk_flags": scorer_result.risk_flags,
                                },
                            )
                            session.record_event(
                                "contract_lesson",
                                {
                                    "lesson_for_session": scorer_result.lesson_for_session,
                                },
                            )
                        log_event(
                            LOGGER,
                            logging.INFO,
                            "contract_scoreboard_updated",
                            score=scorer_result.score,
                            pressure_level=scoreboard.pressure_level,
                        )
                    except Exception as exc:
                        log_event(
                            LOGGER,
                            logging.WARNING,
                            "contract_scorer_failed",
                            error=type(exc).__name__,
                        )
```

- [ ] **Step 5: Run focused test**

Run: `uv run pytest tests/e2e/test_cli_e2e.py::test_contract_scorer_updates_scoreboard_and_session_lesson -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add dong/contract.py dong/cli.py tests/e2e/test_cli_e2e.py
git commit -m "Score signed contract delivery" \
  -m "The pressure loop only matters if an independent scorer updates local reputation and teaches the active session. This adds scorer prompting, rule-floor enforcement, scoreboard updates, and session lessons." \
  -m "Confidence: medium" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/e2e/test_cli_e2e.py::test_contract_scorer_updates_scoreboard_and_session_lesson -q"
```

---

### Task 10: Logging, Documentation, And Full Verification

**Files:**
- Modify: `README.md`
- Modify: `dong/cli.py`
- Modify: `dong/contract.py`
- Test: existing focused tests plus full suite

- [ ] **Step 1: Add README section**

Add to `README.md` near REPL commands:

```markdown
### 契约压力模式

dong 会在复杂开发任务中自动启用契约压力模式。触发信号包括文件修改、验证命令、多轮工具调用和上下文压缩。契约不是硬流程；它会向主 Agent 注入交付最佳实践和声誉压力，并在最终答复后生成签名证据包、调用第三方 scorer、更新 `.dong/scoreboard.json`。

| 命令 | 效果 |
|------|------|
| `/contract on` | 强制本轮启用契约压力 |
| `/contract off` | 本轮关闭契约压力，关闭行为会被记录 |
| `/contract status` | 查看触发原因、平均分、压力等级和最近教训 |

契约材料位于 `.dong/contracts/best-practices.md`。交付证据包位于 `.dong/contracts/<session-id>-<timestamp>.json`。长期评分表位于 `.dong/scoreboard.json`。
```

- [ ] **Step 2: Verify logging events exist**

Search for required event names:

Run: `rg -n "contract_triggered|contract_pressure_injected|contract_signature_started|contract_signature_finished|contract_rule_floor_created|contract_scorer_started|contract_scorer_failed|contract_scoreboard_updated" dong`

Expected: every event appears at least once in `dong/cli.py`.

If missing, add `log_event(...)` calls at the corresponding action point. Use this exact event list:

```python
CONTRACT_LOG_EVENTS = (
    "contract_triggered",
    "contract_pressure_injected",
    "contract_evidence_created",
    "contract_signature_started",
    "contract_signature_finished",
    "contract_signature_failed",
    "contract_rule_floor_created",
    "contract_scorer_started",
    "contract_scorer_finished",
    "contract_scorer_failed",
    "contract_scoreboard_updated",
)
```

- [ ] **Step 3: Run focused test suite**

Run:

```bash
uv run pytest tests/test_contract.py tests/test_session.py tests/test_cli_repl_commands.py tests/e2e/test_cli_e2e.py -q
```

Expected: PASS.

- [ ] **Step 4: Run ruff fix/check**

Run:

```bash
uv run ruff check --fix dong tests
```

Expected: PASS or fixed files only in the current task scope. If ruff modifies files, inspect and commit those formatting/lint fixes.

- [ ] **Step 5: Run full automated regression**

Run:

```bash
uv run pytest -q
```

Expected: PASS.

- [ ] **Step 6: Run real LLM/API end-to-end verification**

Run a real dong task with a disposable temporary workspace:

```bash
tmpdir="$(mktemp -d)"
printf 'value = 1\n' > "$tmpdir/sample.py"
uv run dong -d "$tmpdir" "把 sample.py 里的 value 改成 2，并说明验证情况"
```

Expected evidence:

- `"$tmpdir/.dong/contracts/best-practices.md"` exists.
- `"$tmpdir/.dong/contracts/"` contains one signed `session-*.json`.
- `"$tmpdir/.dong/scoreboard.json"` exists.
- `uv run dong -d "$tmpdir" --resume latest "上一轮 scorer 给了什么教训？"` shows the lesson pressure in behavior or answer.

If live provider credentials are missing, record this gap in final reporting and keep automated regression evidence.

- [ ] **Step 7: Commit docs and final fixes**

```bash
git add README.md dong/cli.py dong/contract.py tests/test_contract.py tests/test_session.py tests/test_cli_repl_commands.py tests/e2e/test_cli_e2e.py
git commit -m "Document and verify contract pressure" \
  -m "The contract feature changes the agent's delivery behavior, so docs and verification evidence need to describe the pressure loop, artifacts, and scoreboard." \
  -m "Confidence: medium" \
  -m "Scope-risk: broad" \
  -m "Tested: uv run pytest tests/test_contract.py tests/test_session.py tests/test_cli_repl_commands.py tests/e2e/test_cli_e2e.py -q" \
  -m "Tested: uv run ruff check --fix dong tests" \
  -m "Tested: uv run pytest -q" \
  -m "Not-tested: Real LLM/API e2e if credentials are unavailable"
```

---

## Self-Review

**Spec coverage:** This plan covers the approved spec: soft contract pressure, best-practice material, manual on/off/status, signed evidence package, proof-of-work ritual, third-party scorer, rule-floor constraints, scoreboard, session lessons, logs, docs, fake-LLM regression, and real LLM/API verification.

**No placeholders:** The plan avoids placeholder language. Every code-changing task includes concrete snippets, exact paths, commands, and expected outcomes.

**Type consistency:** The plan consistently uses `ContractController`, `ContractMode`, `ContractSignal`, `TriggerReason`, `ContractEvidence`, `ContractSignature`, `RuleFloor`, `ScorerResult`, and `Scoreboard`. CLI integration keeps new parameters optional so existing tests and monkeypatches remain compatible.

**Risks to watch during execution:**

- `cli.py` has active unrelated user changes in the working tree. Read the current file before each patch and avoid reverting user edits.
- The scorer call consumes one additional LLM request after final answer. Tests must use iterators with enough fake responses.
- The default proof-of-work difficulty must stay low in tests and modest in live use to avoid hanging the CLI.
- Artifact paths under `.dong/contracts/` are user workspace files; tests must isolate them in `tmp_path`.
