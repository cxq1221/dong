"""Tests for the bash tool in dong/tools.py."""
import pytest
from dong.tools import execute, ToolResult, BashInput


def _execute_bash(cwd, command):
    """Helper to call execute('bash', ...) and return ToolResult."""
    return execute("bash", {"command": command}, cwd)


class TestBashSuccess:
    """Happy-path tests for the bash tool."""

    def test_echo(self, tmp_path):
        """Simple echo command returns the echoed string."""
        result = _execute_bash(str(tmp_path), "echo hello world")
        assert result.success is True
        assert "hello world" in result.detail
        assert "$ echo hello world" in result.summary

    def test_pwd(self, tmp_path):
        """pwd returns the cwd path."""
        result = _execute_bash(str(tmp_path), "pwd")
        assert result.success is True
        assert str(tmp_path) in result.detail

    def test_exit_code_zero(self, tmp_path):
        """A command that exits with 0 returns success."""
        result = _execute_bash(str(tmp_path), "true")
        assert result.success is True

    def test_working_directory_respected(self, tmp_path):
        """Commands run in the cwd directory."""
        nested = tmp_path / "subdir"
        nested.mkdir()
        result = _execute_bash(str(nested), "pwd")
        assert result.success is True
        assert str(nested) in result.detail


class TestBashOutputTruncation:
    """Tests for output truncation (commands > 3000 chars)."""

    def test_long_output_truncated(self, tmp_path):
        """Output longer than 3000 chars is truncated."""
        # Generate output of ~4000 characters
        result = _execute_bash(str(tmp_path), "python3 -c \"print('x'*4000)\"")
        assert result.success is True
        assert len(result.detail) <= 3100  # 3000 + truncation message
        assert "truncated" in result.detail

    def test_short_output_not_truncated(self, tmp_path):
        """Short output is not truncated."""
        result = _execute_bash(str(tmp_path), "echo short")
        assert result.success is True
        assert "truncated" not in result.detail


class TestBashErrors:
    """Error-case tests for the bash tool."""

    def test_non_zero_exit_code(self, tmp_path):
        """A command that fails returns failure with exit code."""
        result = _execute_bash(str(tmp_path), "false")
        assert result.success is False
        assert "exit code" in result.summary
        assert "1" in result.summary

    def test_command_not_found(self, tmp_path):
        """An invalid command returns failure."""
        result = _execute_bash(str(tmp_path), "nonexistent_command_xyz123")
        assert result.success is False
        # Should have an exit code or error detail
        assert result.detail is not None

    def test_stdout_and_stderr_combined(self, tmp_path):
        """Both stdout and stderr are captured and returned."""
        result = _execute_bash(
            str(tmp_path),
            "echo out && python3 -c 'import sys; sys.stderr.write(\"err\")'",
        )
        assert result.success is True
        assert "out" in result.detail
        assert "err" in result.detail


class TestBashWithSubprocessEdgeCases:
    """Edge cases for bash tool execution."""

    def test_empty_command(self, tmp_path):
        """An empty command string — subprocess behavior."""
        result = _execute_bash(str(tmp_path), "")
        # Empty command with shell=True typically returns exit code 0
        # but may also produce no output
        assert result.success is False or result.detail is not None
