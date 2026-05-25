"""Tests for the edit tool in dong/tools.py."""
import pytest
from dong.tools import execute, _validate_path, ToolResult, EditInput


def _execute_edit(cwd, filepath, old_string, new_string):
    """Helper to call execute('edit', ...) and return ToolResult."""
    return execute(
        "edit",
        {"filepath": filepath, "old_string": old_string, "new_string": new_string},
        cwd,
    )


class TestEditSuccess:
    """Happy-path tests for the edit tool."""

    def test_replace_single_occurrence(self, tmp_path):
        """Replacing a single unique occurrence in a file works."""
        f = tmp_path / "hello.txt"
        f.write_text("Hello, world!")
        result = _execute_edit(str(tmp_path), "hello.txt", "world", "there")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "Hello, there!"

    def test_replace_at_beginning(self, tmp_path):
        """Replacing text at the start of the file."""
        f = tmp_path / "begin.txt"
        f.write_text("start middle end")
        result = _execute_edit(str(tmp_path), "begin.txt", "start", "BEGIN")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "BEGIN middle end"

    def test_replace_at_end(self, tmp_path):
        """Replacing text at the end of the file."""
        f = tmp_path / "end.txt"
        f.write_text("start middle end")
        result = _execute_edit(str(tmp_path), "end.txt", "end", "END")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "start middle END"

    def test_replace_with_empty_new_string(self, tmp_path):
        """Replacing with an empty string removes the old text."""
        f = tmp_path / "remove.txt"
        f.write_text("remove this word")
        result = _execute_edit(str(tmp_path), "remove.txt", " this", "")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "remove word"

    def test_replace_empty_old_string_with_new(self, tmp_path):
        """Replacing an empty old_string with text — edge case."""
        f = tmp_path / "insert.txt"
        f.write_text("hello")
        result = _execute_edit(str(tmp_path), "insert.txt", "", "prefix")
        # Empty string will always be 'found' — but if the content has no empty string...
        # Actually, '' is in every string, so content.count('') returns len(content)+1
        # This should hit the ambiguous case. Let's verify it's ambiguous.
        assert result.success is False
        assert "Ambiguous" in result.error

    def test_replace_unicode_content(self, tmp_path):
        """Unicode characters in replacement work correctly."""
        f = tmp_path / "unicode.txt"
        f.write_text("héllo wörld")
        result = _execute_edit(str(tmp_path), "unicode.txt", "wörld", "värld")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "héllo värld"

    def test_replace_with_same_string(self, tmp_path):
        """Replacing a string with itself is a no-op."""
        f = tmp_path / "same.txt"
        f.write_text("unchanged content")
        result = _execute_edit(str(tmp_path), "same.txt", "unchanged", "unchanged")
        assert result.success is True
        assert f.read_text(encoding="utf-8") == "unchanged content"


class TestEditErrors:
    """Error-case tests for the edit tool."""

    def test_old_string_not_found(self, tmp_path):
        """When old_string is not in the file, returns failure."""
        f = tmp_path / "notfound.txt"
        f.write_text("some content here")
        result = _execute_edit(str(tmp_path), "notfound.txt", "nonexistent", "replacement")
        assert result.success is False
        assert "not found" in result.error

    def test_old_string_ambiguous(self, tmp_path):
        """When old_string appears multiple times, returns failure."""
        f = tmp_path / "ambiguous.txt"
        f.write_text("repeat repeat repeat")
        result = _execute_edit(str(tmp_path), "ambiguous.txt", "repeat", "once")
        assert result.success is False
        assert "Ambiguous" in result.error
        # File should be unchanged
        assert f.read_text(encoding="utf-8") == "repeat repeat repeat"

    def test_path_traversal_denied(self, tmp_path):
        """Path with ../ escapes is rejected before any edits."""
        result = _execute_edit(str(tmp_path), "../outside.txt", "old", "new")
        assert result.success is False
        assert "Permission denied" in result.error or "Path traversal" in result.error

    def test_file_not_found(self, tmp_path):
        """Edit on a non-existent file returns failure."""
        result = _execute_edit(str(tmp_path), "nonexistent.txt", "old", "new")
        assert result.success is False
        assert "not found" in result.error or "No such file" in result.error

    def test_invalid_input_type(self, tmp_path):
        """Passing invalid types for filepath returns invalid input error."""
        result = execute(
            "edit",
            {"filepath": 123, "old_string": "old", "new_string": "new"},
            str(tmp_path),
        )
        assert result.success is False
        assert "Invalid input" in result.error
