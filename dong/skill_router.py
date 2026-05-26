"""自动 skill 路由模块：根据用户意图高精度地临时选择匹配 skill。"""

from __future__ import annotations

import logging
import os
import re
import string
from dataclasses import dataclass

from dong.logging_config import get_logger, log_event
from dong.skills import SkillCatalogEntry, skill_catalog

LOGGER = get_logger(__name__)
DISABLE_VALUES = {"0", "false", "no", "off"}
MIN_SELECT_SCORE = 12
MIN_SCORE_MARGIN = 5
MIN_SCORE_RATIO = 1.35
TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
GENERIC_KEYWORDS = {"code", "代码", "test", "测试", "review", "检查", "问题"}


@dataclass(frozen=True)
class SkillRouteCandidate:
    """单个 skill 的匹配分数和可解释命中信息。"""

    name: str
    score: int
    reason: str
    matched_terms: tuple[str, ...]


@dataclass(frozen=True)
class SkillRouteDecision:
    """一次自动 skill 路由的结果，selected 为空表示保持普通 prompt。"""

    selected: tuple[str, ...]
    candidates: tuple[SkillRouteCandidate, ...]
    confidence: str
    reason: str


def route_skills(
    prompt: str,
    workdir: str,
    *,
    loaded_skills: list[str],
    max_auto_skills: int = 1,
) -> SkillRouteDecision:
    """按 prompt 自动选择 skill；只在置信度足够高时返回 selected。"""

    if _auto_skill_disabled():
        return SkillRouteDecision((), (), "off", "disabled")

    loaded = set(loaded_skills)
    query = prompt.strip()
    if not query:
        return SkillRouteDecision((), (), "none", "empty prompt")

    candidates = []
    for entry in skill_catalog(workdir):
        if entry.name in loaded or entry.entry_name in loaded:
            continue
        candidate = _score_entry(query, entry)
        if candidate.score > 0:
            candidates.append(candidate)

    candidates.sort(key=lambda candidate: (-candidate.score, candidate.name))
    selected = _select_candidates(candidates, max_auto_skills=max_auto_skills)
    confidence = "high" if selected and candidates[0].score >= 22 else "medium" if selected else "none"
    reason = candidates[0].reason if selected else _no_selection_reason(candidates)

    log_event(
        LOGGER,
        logging.DEBUG,
        "auto_skill_routed",
        selected=list(selected),
        confidence=confidence,
        reason=reason,
        candidates=[
            {
                "name": candidate.name,
                "score": candidate.score,
                "matched_terms": list(candidate.matched_terms),
            }
            for candidate in candidates[:5]
        ],
    )
    return SkillRouteDecision(
        selected=tuple(selected),
        candidates=tuple(candidates),
        confidence=confidence,
        reason=reason,
    )


def canonical_skill_token(value: str) -> str:
    """把 skill 名、关键词和用户词归一化，保留中日韩字符用于中文匹配。"""

    chars = []
    for char in value.lower():
        if char.isascii():
            if char.isalnum():
                chars.append(char)
        elif char not in string.whitespace and char not in string.punctuation:
            chars.append(char)
    token = "".join(chars)
    if token.endswith("skill"):
        token = token.removesuffix("skill")
    return token


def _auto_skill_disabled() -> bool:
    """读取环境开关，便于测试或用户临时关闭自动路由。"""

    return os.environ.get("DONG_AUTO_SKILL", "1").strip().lower() in DISABLE_VALUES


def _score_entry(query: str, entry: SkillCatalogEntry) -> SkillRouteCandidate:
    """对单个 skill 打分；只使用名称、描述、标题和 keywords 等可解释元数据。"""

    query_lower = query.lower()
    query_tokens = _query_tokens(query)
    query_canonical = {canonical_skill_token(token) for token in query_tokens}
    query_canonical.discard("")
    entry_name = canonical_skill_token(entry.name)
    entry_alias = canonical_skill_token(entry.entry_name)
    search_text = entry.search_text.lower()
    search_canonical = canonical_skill_token(entry.search_text)

    score = 0
    matches: list[str] = []

    if entry_name and entry_name in query_canonical:
        score += 20
        matches.append(entry.name)
    if entry_alias and entry_alias != entry_name and entry_alias in query_canonical:
        score += 18
        matches.append(entry.entry_name)
    if entry_name and entry_name in canonical_skill_token(query):
        score += 10
        matches.append(entry.name)

    for keyword in entry.keywords:
        keyword_lower = keyword.lower()
        keyword_token = canonical_skill_token(keyword)
        if not keyword_token:
            continue
        keyword_score = 8 if keyword_token in GENERIC_KEYWORDS else 12
        if keyword_lower in query_lower:
            score += keyword_score
            matches.append(keyword)
        elif keyword_token in query_canonical:
            score += max(6, keyword_score - 4)
            matches.append(keyword)

    for token in query_tokens:
        token_lower = token.lower()
        token_canonical = canonical_skill_token(token)
        if not token_canonical or len(token_canonical) < 3:
            continue
        if token_lower in search_text or token_canonical in search_canonical:
            score += 3
            matches.append(token)

    matched_terms = tuple(dict.fromkeys(matches))
    reason = "matched: " + ", ".join(matched_terms[:4]) if matched_terms else "metadata overlap"
    return SkillRouteCandidate(
        name=entry.name,
        score=score,
        reason=reason,
        matched_terms=matched_terms,
    )


def _query_tokens(query: str) -> tuple[str, ...]:
    """从用户输入中提取轻量 token；中文短句保留整段供关键词子串匹配。"""

    raw_tokens = TOKEN_RE.findall(query)
    tokens = [token for token in raw_tokens if token]
    if query.strip():
        tokens.append(query.strip())
    return tuple(dict.fromkeys(tokens))


def _select_candidates(
    candidates: list[SkillRouteCandidate],
    *,
    max_auto_skills: int,
) -> tuple[str, ...]:
    """只有第一名明显领先时才自动选择，避免误触发泛用 skill。"""

    if not candidates or candidates[0].score < MIN_SELECT_SCORE:
        return ()
    if len(candidates) > 1:
        top = candidates[0].score
        second = candidates[1].score
        if top - second < MIN_SCORE_MARGIN or top < int(second * MIN_SCORE_RATIO):
            return ()
    return tuple(candidate.name for candidate in candidates[:max_auto_skills])


def _no_selection_reason(candidates: list[SkillRouteCandidate]) -> str:
    """给未自动选择的情况返回可记录原因。"""

    if not candidates:
        return "no matching skill"
    if candidates[0].score < MIN_SELECT_SCORE:
        return "low score"
    return "ambiguous"
