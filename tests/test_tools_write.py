"""write 工具测试：覆盖写入、覆盖、建目录和错误返回。"""
from dong.tools import execute


def _execute_write(cwd, filepath, content):
    """调用 execute('write', ...) 并返回 ToolResult 的测试辅助函数。"""
    return execute("write", {"filepath": filepath, "content": content}, cwd)


class TestWriteSuccess:
    """write 工具的成功路径测试。"""

    def test_write_new_file(self, tmp_path):
        """写入新文件时应创建文件并保存指定内容。"""
        result = _execute_write(str(tmp_path), "hello.txt", "Hello, world!")
        assert result.success is True
        assert "hello.txt" in result.summary
        assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "Hello, world!"

    def test_write_empty_content(self, tmp_path):
        """写入空字符串时应创建空文件。"""
        result = _execute_write(str(tmp_path), "empty.txt", "")
        assert result.success is True
        assert (tmp_path / "empty.txt").read_text(encoding="utf-8") == ""

    def test_write_multiple_lines(self, tmp_path):
        """包含多行换行的内容应完整写入。"""
        content = "line one\nline two\nline three\n"
        result = _execute_write(str(tmp_path), "multi.txt", content)
        assert result.success is True
        assert (tmp_path / "multi.txt").read_text(encoding="utf-8") == content

    def test_write_unicode_content(self, tmp_path):
        """写入时应保留 Unicode 字符。"""
        content = "héllo wörld\n😊 emoji\n"
        result = _execute_write(str(tmp_path), "unicode.txt", content)
        assert result.success is True
        assert (tmp_path / "unicode.txt").read_text(encoding="utf-8") == content

    def test_write_overwrites_existing(self, tmp_path):
        """写入已有文件时应覆盖原内容。"""
        f = tmp_path / "existing.txt"
        f.write_text("old content")
        result = _execute_write(str(tmp_path), "existing.txt", "new content")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "new content"

    def test_write_large_content(self, tmp_path):
        """大块文本内容应能正常写入。"""
        content = "A" * 100_000
        result = _execute_write(str(tmp_path), "large.txt", content)
        assert result.success is True
        assert len((tmp_path / "large.txt").read_text(encoding="utf-8")) == 100_000


class TestWriteSubdirectory:
    """写入嵌套目录中文件的测试。"""

    def test_write_file_in_subdirectory(self, tmp_path):
        """写入子目录文件时应自动创建父目录。"""
        result = _execute_write(str(tmp_path), "sub/dir/nested.txt", "deep content")
        assert result.success is True
        assert (tmp_path / "sub" / "dir" / "nested.txt").read_text(encoding="utf-8") == "deep content"

    def test_write_file_in_deeply_nested_path(self, tmp_path):
        """多层嵌套父目录也应自动创建。"""
        result = _execute_write(str(tmp_path), "a/b/c/d/e/f/g/deep.txt", "very deep")
        assert result.success is True
        assert (tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "deep.txt").exists()


class TestWriteErrors:
    """write 工具的错误路径测试。"""

    def test_path_traversal_denied(self, tmp_path):
        """带 ../ 的路径逃逸应被拒绝。"""
        result = _execute_write(str(tmp_path), "../outside.txt", "content")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_absolute_path_outside_cwd_denied(self, tmp_path):
        """cwd 外部的绝对路径应被拒绝。"""
        result = _execute_write(str(tmp_path), "/etc/passwd", "content")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_invalid_input_type_filepath(self, tmp_path):
        """非字符串 filepath 应返回输入校验错误。"""
        result = execute("write", {"filepath": 123, "content": "text"}, str(tmp_path))
        assert result.success is False
        assert "Invalid input" in result.error

    def test_invalid_input_type_content(self, tmp_path):
        """非字符串 content 应返回输入校验错误。"""
        result = execute("write", {"filepath": "f.txt", "content": 42}, str(tmp_path))
        assert result.success is False
        assert "Invalid input" in result.error
