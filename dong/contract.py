"""契约压力 Module：记录复杂任务信号并判断是否启用软契约压力。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

CONTRACT_VERSION = 1
TOOL_THRESHOLD = 5
VERIFY_COMMAND_KEYWORDS = ("pytest", "ruff", "mypy", "test", "lint", "build", "uv run")
FILE_CHANGE_TOOLS = {"write", "edit"}
BEST_PRACTICES_RELPATH = ".dong/contracts/best-practices.md"
SCOREBOARD_RELPATH = ".dong/scoreboard.json"
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


@dataclass(frozen=True)
class ContractSignature:
    """契约签名结果；记录证据 hash、nonce、难度和本地工作量证明 hash。"""

    evidence_hash: str
    nonce: int
    difficulty: int
    elapsed_ms: int
    signature_hash: str


@dataclass(frozen=True)
class ContractEvidence:
    """契约证据包；汇总交付目标、工具轨迹、文件变更、验证证据和风险。"""

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
        """把证据包转换为普通 dict，方便规范化 JSON 和后续持久化。"""

        return asdict(self)


@dataclass(frozen=True)
class RuleFloor:
    """规则底座；用确定性证据规则限制 scorer 可给出的最高分。"""

    base_score_ceiling: int
    required_deductions: list[str]
    evidence_gaps: list[str]
    signature_valid: bool


@dataclass(frozen=True)
class ScorerResult:
    """scorer 结果；保存最终分数、扣分原因、风险标记和本轮教训。"""

    score: int
    deductions: list[str]
    risk_flags: list[str]
    lesson_for_session: str
    workspace_summary: str


@dataclass(frozen=True)
class Scoreboard:
    """评分表；跨 session 维护近期分数、压力等级和常见扣分原因。"""

    version: int
    average_score: float | None
    recent_scores: list[int]
    pressure_level: str
    common_deductions: dict[str, int]
    sessions: dict[str, dict]

    def to_dict(self) -> dict:
        """转换为普通 dict，供 JSON 持久化使用。"""

        return asdict(self)


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


def build_evidence_hash(evidence: ContractEvidence) -> str:
    """使用规范化 JSON 生成证据包 hash，签名和 scorer 结果不参与证据自身 hash。"""

    evidence_dict = evidence.to_dict()
    evidence_dict["signature"] = None
    evidence_dict["scorer_result"] = None
    canonical_payload = json.dumps(
        evidence_dict,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def _signature_hash(
    *,
    session_id: str,
    evidence_hash: str,
    contract_version: int,
    nonce: int,
    difficulty: int,
) -> str:
    """基于会话、证据和 nonce 生成单次签名尝试的 hash。"""

    payload = {
        "contract_version": contract_version,
        "difficulty": difficulty,
        "evidence_hash": evidence_hash,
        "nonce": nonce,
        "session_id": session_id,
    }
    canonical_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


def sign_evidence(
    evidence: ContractEvidence,
    difficulty: int,
    max_attempts: int = 5_000_000,
) -> ContractSignature:
    """为证据包执行本地工作量证明签名，找到满足前导 0 难度的 nonce。"""

    if difficulty < 0:
        raise ValueError("difficulty must be non-negative")

    started_at = time.perf_counter()
    evidence_hash = build_evidence_hash(evidence)
    prefix = "0" * difficulty
    for nonce in range(max_attempts):
        candidate_hash = _signature_hash(
            session_id=evidence.session_id,
            evidence_hash=evidence_hash,
            contract_version=evidence.contract_version,
            nonce=nonce,
            difficulty=difficulty,
        )
        if candidate_hash.startswith(prefix):
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
            return ContractSignature(
                evidence_hash=evidence_hash,
                nonce=nonce,
                difficulty=difficulty,
                elapsed_ms=elapsed_ms,
                signature_hash=candidate_hash,
            )

    raise TimeoutError("contract signature proof-of-work exhausted max_attempts")


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


def verify_signature(evidence: ContractEvidence, signature: ContractSignature) -> bool:
    """校验证据 hash、签名 hash 和难度前缀是否全部匹配。"""

    evidence_hash = build_evidence_hash(evidence)
    if evidence_hash != signature.evidence_hash:
        return False

    expected_hash = _signature_hash(
        session_id=evidence.session_id,
        evidence_hash=evidence_hash,
        contract_version=evidence.contract_version,
        nonce=signature.nonce,
        difficulty=signature.difficulty,
    )
    prefix = "0" * signature.difficulty
    return expected_hash == signature.signature_hash and expected_hash.startswith(prefix)


def build_rule_floor(evidence: ContractEvidence, signature_valid: bool) -> RuleFloor:
    """根据证据包生成确定性规则底座，防止 scorer 给缺证据交付过高分。"""

    ceiling = 100
    required_deductions: list[str] = []
    evidence_gaps: list[str] = []

    if evidence.file_changes and not evidence.verification_evidence:
        ceiling = min(ceiling, 60)
        required_deductions.append("missing_verification")
        evidence_gaps.append("missing_verification")

    if evidence.file_changes and not evidence.known_risks and not evidence.unverified_items:
        ceiling = min(ceiling, 85)
        required_deductions.append("missing_risk_disclosure")
        evidence_gaps.append("missing_risk_disclosure")

    if not signature_valid:
        ceiling = min(ceiling, 50)
        required_deductions.append("invalid_signature")

    has_failed_tool = any(item.get("success") is False for item in evidence.tool_summary)
    if has_failed_tool and not _discloses_failure(evidence.final_answer):
        ceiling = min(ceiling, 70)
        required_deductions.append("undisclosed_failure")
        evidence_gaps.append("undisclosed_failure")

    return RuleFloor(
        base_score_ceiling=ceiling,
        required_deductions=sorted(set(required_deductions)),
        evidence_gaps=sorted(set(evidence_gaps)),
        signature_valid=signature_valid,
    )


def validate_scorer_result(raw: dict, rule_floor: RuleFloor) -> ScorerResult:
    """校验并归一化 scorer 输出，强制应用规则底座的分数上限和必需扣分。"""

    score = _clamp_score(raw.get("score", 0), rule_floor.base_score_ceiling)
    deductions = _parse_string_list(raw.get("deductions"))
    for deduction in rule_floor.required_deductions:
        if deduction not in deductions:
            deductions.append(deduction)

    return ScorerResult(
        score=score,
        deductions=deductions,
        risk_flags=_parse_string_list(raw.get("risk_flags")),
        lesson_for_session=_parse_string(raw.get("lesson_for_session")),
        workspace_summary=_parse_string(raw.get("workspace_summary")),
    )


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


def load_scoreboard(workdir: str) -> Scoreboard:
    """读取工作区评分表；不存在时返回空评分表。"""

    path = Path(workdir) / SCOREBOARD_RELPATH
    if not path.exists():
        return _empty_scoreboard()

    payload = json.loads(path.read_text(encoding="utf-8"))
    return Scoreboard(
        version=int(payload.get("version", 1)),
        average_score=payload.get("average_score"),
        recent_scores=[_clamp_score(score, 100) for score in payload.get("recent_scores", [])],
        pressure_level=_parse_string(payload.get("pressure_level")) or "normal",
        common_deductions={
            _parse_string(key): int(value)
            for key, value in payload.get("common_deductions", {}).items()
        },
        sessions={
            _parse_string(key): value for key, value in payload.get("sessions", {}).items()
        },
    )


def apply_score(
    workdir: str,
    scoreboard: Scoreboard,
    session_id: str,
    result: ScorerResult,
) -> Scoreboard:
    """把本轮 scorer 结果写入评分表，并更新平均分、压力等级和扣分计数。"""

    recent_scores = [*scoreboard.recent_scores, result.score][-20:]
    average_score = round(sum(recent_scores) / len(recent_scores), 2)
    common_deductions = dict(scoreboard.common_deductions)
    for deduction in result.deductions:
        common_deductions[deduction] = common_deductions.get(deduction, 0) + 1

    sessions = dict(scoreboard.sessions)
    sessions[session_id] = {
        "score": result.score,
        "deductions": result.deductions,
        "risk_flags": result.risk_flags,
        "lesson_for_session": result.lesson_for_session,
        "workspace_summary": result.workspace_summary,
    }

    updated = Scoreboard(
        version=scoreboard.version,
        average_score=average_score,
        recent_scores=recent_scores,
        pressure_level=_pressure_level(average_score),
        common_deductions=common_deductions,
        sessions=sessions,
    )
    path = Path(workdir) / SCOREBOARD_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(updated.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return updated


def _empty_scoreboard() -> Scoreboard:
    """创建空评分表；无历史分时默认压力等级为 normal。"""

    return Scoreboard(
        version=1,
        average_score=None,
        recent_scores=[],
        pressure_level="normal",
        common_deductions={},
        sessions={},
    )


def _clamp_score(raw_score: object, ceiling: int) -> int:
    """把任意输入转换为 0 到规则上限之间的整数分。"""

    try:
        score = int(raw_score)
    except (TypeError, ValueError):
        score = 0
    return max(0, min(100, ceiling, score))


def _parse_string(raw_value: object) -> str:
    """把 scorer 字段安全转换为字符串，空值归一化为空字符串。"""

    if raw_value is None:
        return ""
    if isinstance(raw_value, str):
        return raw_value
    return str(raw_value)


def _parse_string_list(raw_value: object) -> list[str]:
    """把 scorer 列表字段归一化为字符串列表，兼容单字符串输入。"""

    if raw_value is None:
        return []
    if isinstance(raw_value, str):
        return [raw_value] if raw_value else []
    if isinstance(raw_value, list):
        return [_parse_string(item) for item in raw_value if _parse_string(item)]
    return [_parse_string(raw_value)]


def _pressure_level(average_score: float) -> str:
    """按平均分转换压力等级：75 以上正常，50 到 74.99 观察，低于 50 留校。"""

    if average_score >= 75:
        return "normal"
    if average_score >= 50:
        return "watch"
    return "probation"


def _discloses_failure(final_answer: str) -> bool:
    """检查最终答复是否用中文或英文明确披露失败。"""

    answer = final_answer.lower()
    failure_markers = (
        "失败",
        "未成功",
        "报错",
        "错误",
        "fail",
        "failed",
        "failure",
        "error",
        "not successful",
    )
    return any(marker in answer for marker in failure_markers)


def ensure_best_practices(workdir: str) -> Path:
    """确保契约最佳实践材料存在；已有自定义文件必须原样保留。"""

    path = Path(workdir) / BEST_PRACTICES_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_BEST_PRACTICES, encoding="utf-8")
    return path


def pressure_summary(
    controller: ContractController,
    average_score: float | None,
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
    score_text = "暂无历史分" if average_score is None else f"{average_score:.1f}"

    summary = (
        "[Contract Pressure | 契约压力] "
        f"level={pressure_level}; average_score={score_text}; "
        f"trigger_reasons={reasons}; "
        "交付后会被第三方审计，低分会降低本轮声誉并要求补齐验证证据。"
    )
    if lesson_for_session:
        summary = f"{summary} session教训：{lesson_for_session}"
    return summary


__all__ = [
    "BEST_PRACTICES_RELPATH",
    "CONTRACT_VERSION",
    "ContractController",
    "ContractEvidence",
    "ContractMode",
    "ContractSignature",
    "ContractSignal",
    "DEFAULT_BEST_PRACTICES",
    "FILE_CHANGE_TOOLS",
    "RuleFloor",
    "SCOREBOARD_RELPATH",
    "Scoreboard",
    "ScorerResult",
    "TOOL_THRESHOLD",
    "TriggerReason",
    "VERIFY_COMMAND_KEYWORDS",
    "apply_score",
    "build_evidence_hash",
    "build_rule_floor",
    "ensure_best_practices",
    "load_scoreboard",
    "pressure_summary",
    "scorer_instructions",
    "scorer_user_payload",
    "sign_evidence",
    "validate_scorer_result",
    "verify_signature",
    "write_contract_artifact",
]
