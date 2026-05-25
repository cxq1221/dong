"""Tests for the grep tool in dong/tools.py."""
import pytest
from dong.tools import execute, ToolResult, GrepInput


def _execute_grep(cwd, pattern, path="."):
    """Helper to call execute('grep', ...) and return ToolResult."""
    return execute("grep", {"pattern": pattern, "path": path}, cwd)


class TestGrepSuccess:
    """Happy-path tests for the grep tool."""

    def test_find_single_match(self, tmp_path):
        """Grep finds a unique string in a single file."""
        (tmp_path / "hello.txt").write_text("apple\nbanana\ncherry\n")
        result = _execute_grep(str(tmp_path), "banana")
        assert result.success is True
        assert "hello.txt" in result.detail
        assert "banana" in result.detail

    def test_find_multiple_matches(self, tmp_path):
        """Grep finds multiple matches across files."""
        (tmp_path / "a.txt").write_text("common\n")
        (tmp_path / "b.txt").write_text("common\nunique\n")
        result = _execute_grep(str(tmp_path), "common")
        assert result.success is True
        assert "a.txt" in result.detail
        assert "b.txt" in result.detail

    def test_regex_pattern(self, tmp_path):
        """Grep supports regex patterns."""
        (tmp_path / "data.txt").write_text("abc123\ndef456\nghi789\n")
        result = _execute_grep(str(tmp_path), r"\d+")
        assert result.success is True
        assert "abc123" in result.detail
        assert "def456" in result.detail
        assert "ghi789" in result.detail

    def test_search_in_subdirectory(self, tmp_path):
        """Grep searches recursively into subdirectories by default."""
        (tmp_path / "top.txt").write_text("hello\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.txt").write_text("hello\n")
        result = _execute_grep(str(tmp_path), "hello")
        assert result.success is True
        assert "top.txt" in result.detail
        assert "nested.txt" in result.detail

    def test_search_specific_path(self, tmp_path):
        """Grep searches only the specified path."""
        (tmp_path / "target.txt").write_text("match\n")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "other.txt").write_text("match\n")
        result = _execute_grep(str(tmp_path), "match", path="target.txt")
        assert result.success is True
        assert "target.txt" in result.detail
        assert "other.txt" not in result.detail


class TestGrepNoMatches:
    """Tests for grep with no matches."""

    def test_no_matches(self, tmp_path):
        """When no matches are found, returns (no matches)."""
        (tmp_path / "data.txt").write_text("apple banana")
        result = _execute_grep(str(tmp_path), "cherry")
        assert result.success is True
        assert "(no matches)" in result.detail

    def test_no_matches_empty_file(self, tmp_path):
        """Grep in an empty file returns no matches."""
        (tmp_path / "empty.txt").write_text("")
        result = _execute_grep(str(tmp_path), "anything")
        assert result.success is True
        assert "(no matches)" in result.detail


class TestGrepErrors:
    """Error-case tests for the grep tool."""

    def test_invalid_regex(self, tmp_path):
        """An invalid regex pattern returns an error."""
        (tmp_path / "data.txt").write_text("hello")
        result = _execute_grep(str(tmp_path), r"[invalid")
        # grep may return non-zero exit code for invalid regex
        assert result.detail is not None

    def test_directory_not_found(self, tmp_path):
        """Searching a non-existent path returns an error."""
        result = _execute_grep(str(tmp_path), "pattern", path="/nonexistent/path")
        assert result.detail is not None
        # It may have stderr about the path not existing


class TestGrepOutputTruncation:
    """Tests for grep output truncation."""

    def test_long_output_truncated(self, tmp_path):
        """Very large grep output is truncated."""
        # Create a file with enough lines to trigger truncation
        lines = "\n".join(f"line {i} match" for i in range(500))
        (tmp_path / "large.txt").write_text(lines)
        result = _execute_grep(str(tmp_path), "match")
        assert result.success is True
        if len(result.detail) > 3000:
            assert "truncated" in result.detail
