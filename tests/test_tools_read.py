"""read 工具测试：覆盖路径校验、成功读取和错误返回。"""
import pytest
from dong.tools import execute, _validate_path


class TestValidatePath:
    """内部 _validate_path 辅助函数的路径安全测试。"""

    def test_valid_path(self, tmp_path):
        """cwd 内的普通相对路径应能正确解析。"""
        child = tmp_path / "sub" / "file.txt"
        child.parent.mkdir(parents=True)
        child.write_text("hello")
        resolved = _validate_path(str(tmp_path), "sub/file.txt")
        assert resolved == str(child.resolve())

    def test_path_traversal_denied(self, tmp_path):
        """路径逃逸 cwd 时应抛出 PermissionError。"""
        with pytest.raises(PermissionError, match="Path traversal denied"):
            _validate_path(str(tmp_path), "../etc/passwd")

    def test_absolute_outside_cwd_denied(self, tmp_path):
        """cwd 外部的绝对路径应被拒绝。"""
        with pytest.raises(PermissionError, match="Path traversal denied"):
            _validate_path(str(tmp_path), "/etc/passwd")

    def test_symlink_traversal_denied(self, tmp_path):
        """指向 cwd 外部的符号链接应被拒绝。"""
        target = tmp_path / "outside_target.txt"
        target.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(target, target_is_directory=False)
        # 这个 link 实际仍解析到 tmp_path 内部，所以应当允许。
        # 真正要验证的是指向兄弟目录的坏链接。
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("out")
        link2 = tmp_path / "bad_link.txt"
        link2.symlink_to(outside, target_is_directory=False)
        with pytest.raises(PermissionError, match="Path traversal denied"):
            _validate_path(str(tmp_path), "bad_link.txt")


def _execute_read(cwd, filepath):
    """调用 execute('read', ...) 并返回 ToolResult 的测试辅助函数。"""
    return execute("read", {"filepath": filepath}, cwd)


class TestReadSuccess:
    """read 工具的成功路径测试。"""

    def test_read_existing_file(self, tmp_path):
        """读取已有文件，并返回带行号的内容。"""
        f = tmp_path / "hello.txt"
        f.write_text("line one\nline two\nline three\n")
        result = _execute_read(str(tmp_path), "hello.txt")
        assert result.success is True
        assert "hello.txt" in result.summary
        assert "3 lines" in result.summary
        assert " 1|line one" in result.detail
        assert " 2|line two" in result.detail
        assert " 3|line three" in result.detail

    def test_read_single_line_file(self, tmp_path):
        """读取无结尾换行的单行文件。"""
        f = tmp_path / "single.txt"
        f.write_text("just one line")
        result = _execute_read(str(tmp_path), "single.txt")
        assert result.success is True
        assert "1 lines" in result.summary or "1 line" in result.summary.replace("lines", "line")
        assert " 1|just one line" in result.detail

    def test_read_empty_file(self, tmp_path):
        """读取空文件时应成功，并显示 0 行。"""
        f = tmp_path / "empty.txt"
        f.write_text("")
        result = _execute_read(str(tmp_path), "empty.txt")
        assert result.success is True
        assert "0 lines" in result.summary
        assert result.detail == ""

    def test_read_file_with_unicode(self, tmp_path):
        """读取时应保留 Unicode 字符。"""
        content = "héllo wörld\n😊 emoji\n"
        f = tmp_path / "unicode.txt"
        f.write_text(content, encoding="utf-8")
        result = _execute_read(str(tmp_path), "unicode.txt")
        assert result.success is True
        assert "héllo wörld" in result.detail
        assert "😊 emoji" in result.detail


class TestReadErrors:
    """read 工具的错误路径测试。"""

    def test_file_not_found(self, tmp_path):
        """不存在的文件应返回失败和未找到错误。"""
        result = _execute_read(str(tmp_path), "nonexistent.txt")
        assert result.success is False
        assert "File not found" in result.error or "not found" in result.error

    def test_path_traversal(self, tmp_path):
        """尝试使用 ../ 逃逸目录时应返回权限错误。"""
        result = _execute_read(str(tmp_path), "../etc/passwd")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_absolute_path_outside_cwd(self, tmp_path):
        """cwd 外部的绝对路径应被拒绝。"""
        result = _execute_read(str(tmp_path), "/etc/passwd")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_invalid_input_type(self, tmp_path):
        """非字符串 filepath 应返回输入校验错误。"""
        result = execute("read", {"filepath": 123}, str(tmp_path))
        assert result.success is False
        assert "Invalid input" in result.error

    def test_read_directory(self, tmp_path):
        """读取目录时应返回错误，因为目标不是普通文件。"""
        d = tmp_path / "adir"
        d.mkdir()
        result = _execute_read(str(tmp_path), "adir")
        assert result.success is False
        # 不同系统可能返回 PermissionError 或 FileNotFoundError，这里只要求有错误。
        assert result.error != ""


class TestReadSubdirectory:
    """读取嵌套目录中文件的测试。"""

    def test_read_file_in_subdirectory(self, tmp_path):
        """应能读取子目录中的文件。"""
        nested = tmp_path / "sub" / "dir"
        nested.mkdir(parents=True)
        f = nested / "nested.txt"
        f.write_text("deep\ncontent\n")
        result = _execute_read(str(tmp_path), "sub/dir/nested.txt")
        assert result.success is True
        assert "2 lines" in result.summary
        assert "deep" in result.detail
        assert "content" in result.detail
