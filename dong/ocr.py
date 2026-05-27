"""dong 的本地 OCR 适配层，负责把图片转成可发送给文本模型的纯文本。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

IMAGE_MARKER_RE = re.compile(r"\[image:(?P<path>[^\]\r\n]+)\]")


class OcrError(RuntimeError):
    """OCR 识别失败时抛出的用户可读异常。"""


@dataclass(frozen=True)
class OcrLine:
    """单行 OCR 结果，保留文本、置信度和原始框信息。"""

    text: str
    score: float | None = None
    box: Any | None = None


@dataclass(frozen=True)
class OcrResult:
    """一次图片 OCR 的结构化结果。"""

    image_path: str
    lines: tuple[OcrLine, ...]

    @property
    def text(self) -> str:
        """按识别顺序拼接文本，供后续 LLM prompt 使用。"""
        return "\n".join(line.text for line in self.lines if line.text.strip())


def build_ocr_prompt(result: OcrResult, question: str = "") -> str:
    """把 OCR 文本包装成普通文本 prompt，避免依赖模型原生图片能力。"""
    return build_ocr_prompt_for_results((result,), question)


def build_ocr_prompt_for_results(
    results: tuple[OcrResult, ...],
    question: str = "",
) -> str:
    """把一张或多张图片的 OCR 文本包装成普通文本 prompt。"""
    user_question = question.strip()
    image_sections = []
    for index, result in enumerate(results, start=1):
        ocr_text = result.text.strip() or "(OCR 未识别到文字)"
        image_sections.append(
            "\n".join([
                f"图片 {index} 路径：{result.image_path}",
                "OCR 文本：",
                ocr_text,
            ])
        )

    parts = [
        "用户粘贴了图片。下面是本地 OCR 识别出的图片文字，供你理解图片中的文字内容；不要假装直接看到了原图。",
        *image_sections,
    ]
    if user_question:
        parts.extend(["用户输入：", user_question])
    return "\n\n".join(parts)


def image_marker(path: str | Path) -> str:
    """生成输入框中可见的图片占位符。"""
    return f"[image:{Path(path).expanduser()}]"


def expand_image_markers_to_ocr_prompt(text: str) -> str | None:
    """把输入中的图片占位符转成 OCR prompt；没有图片时返回 None。"""
    paths = tuple(match.group("path").strip() for match in IMAGE_MARKER_RE.finditer(text))
    if not paths:
        return None

    question = IMAGE_MARKER_RE.sub(" ", text).strip()
    results = tuple(extract_text_from_image(path) for path in paths)
    return build_ocr_prompt_for_results(results, question)


def extract_text_from_image(path: str) -> OcrResult:
    """使用 OnnxOCR 在 CPU 上识别图片文字。"""
    image_path = Path(path).expanduser().resolve()
    if not image_path.is_file():
        raise OcrError(f"图片文件不存在：{image_path}")

    cv2 = _load_cv2()
    image = cv2.imread(str(image_path))
    if image is None:
        raise OcrError(f"无法读取图片文件：{image_path}")

    raw_result = _onnxocr_model().ocr(image, cls=False)
    lines = tuple(_iter_ocr_lines(raw_result))
    return OcrResult(image_path=str(image_path), lines=lines)


def _load_cv2():
    """延迟导入 OpenCV，让未使用 OCR 的启动路径不受可选依赖影响。"""
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OcrError(
            "缺少 OCR 运行依赖 opencv-python-headless；"
            "请先执行：uv pip install onnxocr"
        ) from exc
    return cv2


@lru_cache(maxsize=1)
def _onnxocr_model():
    """懒加载 OnnxOCR 模型，避免每条命令重复初始化 ONNXRuntime。"""
    try:
        from onnxocr.onnx_paddleocr import ONNXPaddleOcr  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OcrError("缺少 OnnxOCR；请先执行：uv pip install onnxocr") from exc

    use_angle_cls = os.getenv("DONG_OCR_USE_ANGLE_CLS", "").lower() in {
        "1",
        "true",
        "yes",
    }
    return ONNXPaddleOcr(use_angle_cls=use_angle_cls, use_gpu=False)


def _iter_ocr_lines(raw_result: Any):
    """兼容 OnnxOCR/PaddleOCR 常见嵌套结构并提取文本行。"""
    if not raw_result:
        return

    pages = raw_result if isinstance(raw_result, list) else [raw_result]
    for page in pages:
        if not page:
            continue
        for item in page:
            line = _parse_line(item)
            if line is not None and line.text.strip():
                yield line


def _parse_line(item: Any) -> OcrLine | None:
    """解析单条 OCR 结果；无法识别的结构直接跳过。"""
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return None

    box = item[0]
    rec = item[1]
    if isinstance(rec, (list, tuple)) and rec:
        text = str(rec[0] or "")
        score = _to_float(rec[1]) if len(rec) > 1 else None
        return OcrLine(text=text, score=score, box=box)
    if isinstance(rec, str):
        return OcrLine(text=rec, box=box)
    return None


def _to_float(value: Any) -> float | None:
    """把 numpy 标量等置信度值安全转换为 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
