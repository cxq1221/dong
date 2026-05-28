"""本地附件文本提取、OCR prompt 组装和公开识别入口测试。"""

import zipfile

from dong.ocr import (
    OcrLine,
    OcrResult,
    attachment_marker,
    build_ocr_prompt,
    expand_image_markers_to_ocr_prompt,
    extract_text_from_attachment,
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


def test_extract_text_from_attachment_reads_pdf_pages_with_onnxocr(tmp_path, monkeypatch) -> None:
    """PDF 附件应先转页面图片，再按页 OCR。"""
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"fake pdf")

    monkeypatch.setattr("dong.ocr._pdf_to_images", lambda path: [object()])

    class FakeModel:
        """模拟 PDF 页面 OCR 返回结果。"""

        @staticmethod
        def ocr(image: object, *, cls: bool) -> list[list[list[object]]]:
            assert image is not None
            assert cls is False
            return [[[[[0, 0], [1, 0], [1, 1], [0, 1]], ("PDF 行", 0.9)]]]

    monkeypatch.setattr("dong.ocr._onnxocr_model", lambda: FakeModel())

    result = extract_text_from_attachment(str(pdf_path))

    assert [line.text for line in result.lines] == ["第 1 页：", "PDF 行"]


def test_extract_text_from_attachment_reads_xlsx(tmp_path) -> None:
    """Excel 附件应提取工作表名称和单元格文本。"""
    from openpyxl import Workbook

    xlsx_path = tmp_path / "report.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "概览"
    sheet.append(["指标", "值"])
    sheet.append(["并发", 12])
    workbook.save(xlsx_path)

    result = extract_text_from_attachment(str(xlsx_path))

    assert "工作表：概览" in result.text
    assert "指标\t值" in result.text
    assert "并发\t12" in result.text


def test_extract_text_from_attachment_reads_docx_and_pptx(tmp_path) -> None:
    """Word/PPT 附件应从 OOXML zip 中提取文本节点。"""
    docx_path = tmp_path / "summary.docx"
    with zipfile.ZipFile(docx_path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'>"
            "<w:body><w:p><w:r><w:t>Word 文本</w:t></w:r></w:p></w:body></w:document>",
        )

    pptx_path = tmp_path / "deck.pptx"
    with zipfile.ZipFile(pptx_path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            "<p:sld xmlns:a='http://schemas.openxmlformats.org/drawingml/2006/main' "
            "xmlns:p='http://schemas.openxmlformats.org/presentationml/2006/main'>"
            "<p:cSld><p:spTree><a:t>PPT 文本</a:t></p:spTree></p:cSld></p:sld>",
        )

    assert extract_text_from_attachment(str(docx_path)).text == "Word 文本"
    assert extract_text_from_attachment(str(pptx_path)).text == "PPT 文本"


def test_build_ocr_prompt_keeps_image_as_text_context() -> None:
    """OCR prompt 应明确禁止模型假装直接看原始文件。"""
    result = OcrResult(
        image_path="/tmp/example.png",
        lines=(OcrLine("hello"), OcrLine("world")),
    )

    prompt = build_ocr_prompt(result, "翻译成中文")

    assert "不要假装直接看到了原始文件" in prompt
    assert "hello\nworld" in prompt
    assert "翻译成中文" in prompt
    assert "/tmp/example.png" not in prompt
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

    monkeypatch.setattr("dong.ocr.extract_text_from_attachment", fake_extract)

    prompt = expand_image_markers_to_ocr_prompt(
        f"{image_marker(image_path)} 这个错误怎么处理"
    )

    assert prompt is not None
    assert "截图里的错误" in prompt
    assert "这个错误怎么处理" in prompt
    assert "不要假装直接看到了原始文件" in prompt


def test_expand_attachment_markers_accepts_file_alias(monkeypatch) -> None:
    """通用附件占位符也应走同一条文本提取链路。"""
    file_path = "/tmp/report.docx"

    def fake_extract(path: str) -> OcrResult:
        assert path == file_path
        return OcrResult(image_path=path, lines=(OcrLine("文档内容"),))

    monkeypatch.setattr("dong.ocr.extract_text_from_attachment", fake_extract)

    prompt = expand_image_markers_to_ocr_prompt(
        f"{attachment_marker(file_path)} 总结一下"
    )

    assert prompt is not None
    assert "文档内容" in prompt
    assert "总结一下" in prompt


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
