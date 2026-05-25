"""Tests for the read tool in dong/tools.py."""
import os
import pytest
from dong.tools import execute, _validate_path, ToolResult, ReadInput


class TestValidatePath:
    """Tests for the internal _validate_path helper."""

    def test_valid_path(self, tmp_path):
        """A normal filepath within cwd resolves correctly."""
        child = tmp_path / "sub" / "file.txt"
        child.parent.mkdir(parents=True)
        child.write_text("hello")
        resolved = _validate_path(str(tmp_path), "sub/file.txt")
        assert resolved == str(child.resolve())

    def test_path_traversal_denied(self, tmp_path):
        """Raises PermissionError when path escapes cwd."""
        with pytest.raises(PermissionError, match="Path traversal denied"):
            _validate_path(str(tmp_path), "../etc/passwd")

    def test_absolute_outside_cwd_denied(self, tmp_path):
        """An absolute path outside cwd is rejected."""
        with pytest.raises(PermissionError, match="Path traversal denied"):
            _validate_path(str(tmp_path), "/etc/passwd")

    def test_symlink_traversal_denied(self, tmp_path):
        """Symlink pointing outside cwd is rejected."""
        target = tmp_path / "outside_target.txt"
        target.write_text("secret")
        link = tmp_path / "link.txt"
        link.symlink_to(target, target_is_directory=False)
        # The link resolves to a path outside tmp_path/scope — actually
        # in this case it's still inside tmp_path, so it should be fine.
        # Let's test a symlink that points to a *sibling* outside.
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("out")
        link2 = tmp_path / "bad_link.txt"
        link2.symlink_to(outside, target_is_directory=False)
        with pytest.raises(PermissionError, match="Path traversal denied"):
            _validate_path(str(tmp_path), "bad_link.txt")


def _execute_read(cwd, filepath):
    """Helper to call execute('read', ...) and return ToolResult."""
    return execute("read", {"filepath": filepath}, cwd)


class TestReadSuccess:
    """Happy-path tests for the read tool."""

    def test_read_existing_file(self, tmp_path):
        """Reads a file and returns line-numbered content."""
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
        """Reads a one-line file (no trailing newline)."""
        f = tmp_path / "single.txt"
        f.write_text("just one line")
        result = _execute_read(str(tmp_path), "single.txt")
        assert result.success is True
        assert "1 lines" in result.summary or "1 line" in result.summary.replace("lines", "line")
        assert " 1|just one line" in result.detail

    def test_read_empty_file(self, tmp_path):
        """Reading an empty file returns success with 0 lines."""
        f = tmp_path / "empty.txt"
        f.write_text("")
        result = _execute_read(str(tmp_path), "empty.txt")
        assert result.success is True
        assert "0 lines" in result.summary
        assert result.detail == ""

    def test_read_file_with_unicode(self, tmp_path):
        """Unicode (non-ASCII) characters are preserved."""
        content = "héllo wörld\n😊 emoji\n"
        f = tmp_path / "unicode.txt"
        f.write_text(content, encoding="utf-8")
        result = _execute_read(str(tmp_path), "unicode.txt")
        assert result.success is True
        assert "héllo wörld" in result.detail
        assert "😊 emoji" in result.detail


class TestReadErrors:
    """Error-case tests for the read tool."""

    def test_file_not_found(self, tmp_path):
        """Non-existent file returns failure with FileNotFound message."""
        result = _execute_read(str(tmp_path), "nonexistent.txt")
        assert result.success is False
        assert "File not found" in result.error or "not found" in result.error

    def test_path_traversal(self, tmp_path):
        """Attempting ../ returns PermissionError."""
        result = _execute_read(str(tmp_path), "../etc/passwd")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_absolute_path_outside_cwd(self, tmp_path):
        """Absolute path outside cwd is rejected."""
        result = _execute_read(str(tmp_path), "/etc/passwd")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_invalid_input_type(self, tmp_path):
        """Passing non-string filepath returns invalid input error."""
        result = execute("read", {"filepath": 123}, str(tmp_path))
        assert result.success is False
        assert "Invalid input" in result.error

    def test_read_directory(self, tmp_path):
        """Reading a directory returns an error (not a file)."""
        d = tmp_path / "adir"
        d.mkdir()
        result = _execute_read(str(tmp_path), "adir")
        assert result.success is False
        # It could be PermissionError or FileNotFoundError depending on OS
        assert result.error != ""


class TestReadSubdirectory:
    """Tests for reading files in nested directories."""

    def test_read_file_in_subdirectory(self, tmp_path):
        """Read a file in a subdirectory."""
        nested = tmp_path / "sub" / "dir"
        nested.mkdir(parents=True)
        f = nested / "nested.txt"
        f.write_text("deep\ncontent\n")
        result = _execute_read(str(tmp_path), "sub/dir/nested.txt")
        assert result.success is True
        assert "2 lines" in result.summary
        assert "deep" in result.detail
        assert "content" in result.detail
