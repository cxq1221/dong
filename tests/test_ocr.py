"""本地 OCR prompt 组装和公开识别入口测试。"""

from dong.ocr import (
    OcrLine,
    OcrResult,
    build_ocr_prompt,
    expand_image_markers_to_ocr_prompt,
    extract_text_from_image,
    image_marker,
)


def test_extract_text_from_image_parses_onnxocr_shape(tmp_path, monkeypatch) -> None:
    """公开 OCR 入口应把 OnnxOCR 的嵌套结果提取为顺序文本行。"""
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fake image")

    class FakeCv2:
        """模拟 OpenCV 读取图片，避免单测依赖本机 OCR 运行环境。"""

        @staticmethod
        def imread(path: str) -> object:
            assert path == str(image_path)
            return object()

    class FakeModel:
        """模拟 OnnxOCR 模型返回 PaddleOCR 兼容结构。"""

        @staticmethod
        def ocr(image: object, *, cls: bool) -> list[list[list[object]]]:
            assert image is not None
            assert cls is False
            return [
                [
                    [[[0, 0], [1, 0], [1, 1], [0, 1]], ("第一行", 0.99)],
                    [[[0, 2], [1, 2], [1, 3], [0, 3]], ("第二行", 0.95)],
                ]
            ]

    monkeypatch.setattr("dong.ocr._load_cv2", lambda: FakeCv2)
    monkeypatch.setattr("dong.ocr._onnxocr_model", lambda: FakeModel())

    result = extract_text_from_image(str(image_path))

    assert [line.text for line in result.lines] == ["第一行", "第二行"]
    assert result.lines[0].score == 0.99


def test_build_ocr_prompt_keeps_image_as_text_context() -> None:
    """OCR prompt 应明确禁止模型假装直接看图。"""
    result = OcrResult(
        image_path="/tmp/example.png",
        lines=(OcrLine("hello"), OcrLine("world")),
    )

    prompt = build_ocr_prompt(result, "翻译成中文")

    assert "不要假装直接看到了原图" in prompt
    assert "hello\nworld" in prompt
    assert "翻译成中文" in prompt
    assert "请整理图片中的文字" not in prompt


def test_expand_image_markers_to_ocr_prompt_uses_visible_attachment(monkeypatch) -> None:
    """输入框图片占位符应在提交时展开成本地 OCR 文本 prompt。"""
    image_path = "/tmp/dong-clipboard.png"

    def fake_extract(path: str) -> OcrResult:
        assert path == image_path
        return OcrResult(
            image_path=path,
            lines=(OcrLine("截图里的错误"),),
        )

    monkeypatch.setattr("dong.ocr.extract_text_from_image", fake_extract)

    prompt = expand_image_markers_to_ocr_prompt(
        f"{image_marker(image_path)} 这个错误怎么处理"
    )

    assert prompt is not None
    assert "截图里的错误" in prompt
    assert "这个错误怎么处理" in prompt
    assert "不要假装直接看到了原图" in prompt


def test_build_ocr_prompt_without_question_has_no_synthetic_task() -> None:
    """用户只贴图片时，OCR prompt 不应替用户追加默认任务。"""
    result = OcrResult(
        image_path="/tmp/example.png",
        lines=(OcrLine("only text"),),
    )

    prompt = build_ocr_prompt(result)

    assert "only text" in prompt
    assert "用户输入" not in prompt
    assert "请整理图片中的文字" not in prompt
