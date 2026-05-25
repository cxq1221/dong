"""bash 工具测试：覆盖命令执行、输出截断和错误返回。"""
from dong.tools import execute


def _execute_bash(cwd, command):
    """调用 execute('bash', ...) 并返回 ToolResult 的测试辅助函数。"""
    return execute("bash", {"command": command}, cwd)


class TestBashSuccess:
    """bash 工具的成功路径测试。"""

    def test_echo(self, tmp_path):
        """简单 echo 命令应返回输出字符串。"""
        result = _execute_bash(str(tmp_path), "echo hello world")
        assert result.success is True
        assert "hello world" in result.detail
        assert "$ echo hello world" in result.summary

    def test_pwd(self, tmp_path):
        """pwd 应返回当前工作目录。"""
        result = _execute_bash(str(tmp_path), "pwd")
        assert result.success is True
        assert str(tmp_path) in result.detail

    def test_exit_code_zero(self, tmp_path):
        """退出码为 0 的命令应返回成功。"""
        result = _execute_bash(str(tmp_path), "true")
        assert result.success is True

    def test_working_directory_respected(self, tmp_path):
        """命令应在传入的 cwd 目录中执行。"""
        nested = tmp_path / "subdir"
        nested.mkdir()
        result = _execute_bash(str(nested), "pwd")
        assert result.success is True
        assert str(nested) in result.detail


class TestBashOutputTruncation:
    """bash 输出截断测试。"""

    def test_long_output_truncated(self, tmp_path):
        """超过 3000 字符的输出应被截断。"""
        # 生成约 4000 字符输出，用于触发截断分支。
        result = _execute_bash(str(tmp_path), "python3 -c \"print('x'*4000)\"")
        assert result.success is True
        assert len(result.detail) <= 3100  # 3000 + truncation message
        assert "truncated" in result.detail

    def test_short_output_not_truncated(self, tmp_path):
        """短输出不应被截断。"""
        result = _execute_bash(str(tmp_path), "echo short")
        assert result.success is True
        assert "truncated" not in result.detail


class TestBashErrors:
    """bash 工具的错误路径测试。"""

    def test_non_zero_exit_code(self, tmp_path):
        """非零退出码命令应返回失败并带退出码摘要。"""
        result = _execute_bash(str(tmp_path), "false")
        assert result.success is False
        assert "exit code" in result.summary
        assert "1" in result.summary

    def test_command_not_found(self, tmp_path):
        """不存在的命令应返回失败。"""
        result = _execute_bash(str(tmp_path), "nonexistent_command_xyz123")
        assert result.success is False
        # 至少应包含退出码或错误明细。
        assert result.detail is not None

    def test_stdout_and_stderr_combined(self, tmp_path):
        """stdout 和 stderr 应被合并返回。"""
        result = _execute_bash(
            str(tmp_path),
            "echo out && python3 -c 'import sys; sys.stderr.write(\"err\")'",
        )
        assert result.success is True
        assert "out" in result.detail
        assert "err" in result.detail


class TestBashWithSubprocessEdgeCases:
    """bash 工具底层 subprocess 行为的边界测试。"""

    def test_empty_command(self, tmp_path):
        """空命令字符串的行为由 subprocess 决定。"""
        result = _execute_bash(str(tmp_path), "")
        # shell=True 的空命令通常返回 0，但也可能没有任何输出。
        assert result.success is False or result.detail is not None
