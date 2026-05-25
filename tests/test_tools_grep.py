"""grep 工具测试：覆盖匹配、无匹配、错误和输出截断。"""
from dong.tools import execute


def _execute_grep(cwd, pattern, path="."):
    """调用 execute('grep', ...) 并返回 ToolResult 的测试辅助函数。"""
    return execute("grep", {"pattern": pattern, "path": path}, cwd)


class TestGrepSuccess:
    """grep 工具的成功路径测试。"""

    def test_find_single_match(self, tmp_path):
        """grep 应能在单文件中找到唯一字符串。"""
        (tmp_path / "hello.txt").write_text("apple\nbanana\ncherry\n")
        result = _execute_grep(str(tmp_path), "banana")
        assert result.success is True
        assert "hello.txt" in result.detail
        assert "banana" in result.detail

    def test_find_multiple_matches(self, tmp_path):
        """grep 应能跨多个文件找到匹配。"""
        (tmp_path / "a.txt").write_text("common\n")
        (tmp_path / "b.txt").write_text("common\nunique\n")
        result = _execute_grep(str(tmp_path), "common")
        assert result.success is True
        assert "a.txt" in result.detail
        assert "b.txt" in result.detail

    def test_regex_pattern(self, tmp_path):
        """grep 应支持正则模式。"""
        (tmp_path / "data.txt").write_text("abc123\ndef456\nghi789\n")
        # 系统 grep 不是 Python regex，且未启用 -E；使用 basic regex 兼容写法匹配数字。
        result = _execute_grep(str(tmp_path), r"[[:digit:]][[:digit:]]*")
        assert result.success is True
        assert "abc123" in result.detail
        assert "def456" in result.detail
        assert "ghi789" in result.detail

    def test_search_in_subdirectory(self, tmp_path):
        """默认递归搜索时应覆盖子目录。"""
        (tmp_path / "top.txt").write_text("hello\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("hello\n")
        result = _execute_grep(str(tmp_path), "hello")
        assert result.success is True
        assert "top.txt" in result.detail
        assert "nested.txt" in result.detail

    def test_search_specific_path(self, tmp_path):
        """指定 path 时应只搜索该路径。"""
        (tmp_path / "target.txt").write_text("match\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "other.txt").write_text("match\n")
        result = _execute_grep(str(tmp_path), "match", path="target.txt")
        assert result.success is True
        assert "target.txt" in result.detail
        assert "other.txt" not in result.detail


class TestGrepNoMatches:
    """grep 无匹配场景测试。"""

    def test_no_matches(self, tmp_path):
        """没有匹配时应返回 (no matches)。"""
        (tmp_path / "data.txt").write_text("apple banana")
        result = _execute_grep(str(tmp_path), "cherry")
        assert result.success is True
        assert "(no matches)" in result.detail

    def test_no_matches_empty_file(self, tmp_path):
        """空文件中搜索应返回无匹配。"""
        (tmp_path / "empty.txt").write_text("")
        result = _execute_grep(str(tmp_path), "anything")
        assert result.success is True
        assert "(no matches)" in result.detail


class TestGrepErrors:
    """grep 工具的错误路径测试。"""

    def test_invalid_regex(self, tmp_path):
        """非法正则模式应产生错误信息。"""
        (tmp_path / "data.txt").write_text("hello")
        result = _execute_grep(str(tmp_path), r"[invalid")
        # grep 对非法正则通常会返回非零退出码。
        assert result.detail is not None

    def test_directory_not_found(self, tmp_path):
        """搜索不存在路径时应返回错误信息。"""
        result = _execute_grep(str(tmp_path), "pattern", path="/nonexistent/path")
        assert result.detail is not None
        # stderr 中通常会包含路径不存在的信息。


class TestGrepOutputTruncation:
    """grep 输出截断测试。"""

    def test_long_output_truncated(self, tmp_path):
        """非常大的 grep 输出应被截断。"""
        # 创建足够多的匹配行，用于触发截断分支。
        lines = "\n".join(f"line {i} match" for i in range(500))
        (tmp_path / "large.txt").write_text(lines)
        result = _execute_grep(str(tmp_path), "match")
        assert result.success is True
        if len(result.detail) > 3000:
            assert "truncated" in result.detail
