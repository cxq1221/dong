"""Tests for the write tool in dong/tools.py."""
import pytest
from dong.tools import execute, _validate_path, ToolResult, WriteInput


def _execute_write(cwd, filepath, content):
    """Helper to call execute('write', ...) and return ToolResult."""
    return execute("write", {"filepath": filepath, "content": content}, cwd)


class TestWriteSuccess:
    """Happy-path tests for the write tool."""

    def test_write_new_file(self, tmp_path):
        """Writing to a new file creates it with the given content."""
        result = _execute_write(str(tmp_path), "hello.txt", "Hello, world!")
        assert result.success is True
        assert "hello.txt" in result.summary
        assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "Hello, world!"

    def test_write_empty_content(self, tmp_path):
        """Writing empty string creates an empty file."""
        result = _execute_write(str(tmp_path), "empty.txt", "")
        assert result.success is True
        assert (tmp_path / "empty.txt").read_text(encoding="utf-8") == ""

    def test_write_multiple_lines(self, tmp_path):
        """Writing content with multiple newlines works correctly."""
        content = "line one\nline two\nline three\n"
        result = _execute_write(str(tmp_path), "multi.txt", content)
        assert result.success is True
        assert (tmp_path / "multi.txt").read_text(encoding="utf-8") == content

    def test_write_unicode_content(self, tmp_path):
        """Unicode characters are preserved when writing."""
        content = "héllo wörld\n😊 emoji\n"
        result = _execute_write(str(tmp_path), "unicode.txt", content)
        assert result.success is True
        assert (tmp_path / "unicode.txt").read_text(encoding="utf-8") == content

    def test_write_overwrites_existing(self, tmp_path):
        """Writing to an existing file overwrites its content."""
        f = tmp_path / "existing.txt"
        f.write_text("old content")
        result = _execute_write(str(tmp_path), "existing.txt", "new content")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "new content"

    def test_write_large_content(self, tmp_path):
        """Writing a large block of text succeeds."""
        content = "A" * 100_000
        result = _execute_write(str(tmp_path), "large.txt", content)
        assert result.success is True
        assert len((tmp_path / "large.txt").read_text(encoding="utf-8")) == 100_000


class TestWriteSubdirectory:
    """Tests for writing files in nested directories."""

    def test_write_file_in_subdirectory(self, tmp_path):
        """Writing to a file in a subdirectory creates parent dirs automatically."""
        result = _execute_write(str(tmp_path), "sub/dir/nested.txt", "deep content")
        assert result.success is True
        assert (tmp_path / "sub" / "dir" / "nested.txt").read_text(encoding="utf-8") == "deep content"

    def test_write_file_in_deeply_nested_path(self, tmp_path):
        """Deeply nested directories are created automatically."""
        result = _execute_write(str(tmp_path), "a/b/c/d/e/f/g/deep.txt", "very deep")
        assert result.success is True
        assert (tmp_path / "a" / "b" / "c" / "d" / "e" / "f" / "g" / "deep.txt").exists()


class TestWriteErrors:
    """Error-case tests for the write tool."""

    def test_path_traversal_denied(self, tmp_path):
        """Path with ../ escapes is rejected."""
        result = _execute_write(str(tmp_path), "../outside.txt", "content")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_absolute_path_outside_cwd_denied(self, tmp_path):
        """Absolute path outside cwd is rejected."""
        result = _execute_write(str(tmp_path), "/etc/passwd", "content")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_invalid_input_type_filepath(self, tmp_path):
        """Passing non-string filepath returns invalid input error."""
        result = execute("write", {"filepath": 123, "content": "text"}, str(tmp_path))
        assert result.success is False
        assert "Invalid input" in result.error

    def test_invalid_input_type_content(self, tmp_path):
        """Passing non-string content returns invalid input error."""
        result = execute("write", {"filepath": "f.txt", "content": 42}, str(tmp_path))
        assert result.success is False
        assert "Invalid input" in result.error
